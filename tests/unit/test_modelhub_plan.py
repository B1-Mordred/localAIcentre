from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.catalog import load_catalog  # noqa: E402
from app.modelhub import (  # noqa: E402
    build_sync_plan,
    client_ip_allowed_by_cidr,
    downloadable_records_for_blob,
    downloadable_versions_for,
    model_allowed_by_allowed_set,
    parse_accepted_license_refs,
    validate_allowed_models,
    validate_cidr_allowlist,
)
from app.scheduler import ResourcePolicy  # noqa: E402


DOWNLOADABLE_SHA = "a" * 64
INFERENCE_ONLY_SHA = "b" * 64


def write_catalog(root: Path) -> None:
    seed = root / "seed"
    seed.mkdir(parents=True)
    (seed / "aliases.json").write_text(
        json.dumps(
            {
                "aliases": [
                    {
                        "alias": "chat-default",
                        "modality": "llm",
                        "preferred_runtime": "localai",
                        "status": "installed",
                    },
                    {
                        "alias": "tts-fast",
                        "modality": "tts",
                        "preferred_runtime": "audio-cpu",
                        "status": "installed",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    common = {
        "source": {
            "type": "catalog",
            "url": "https://models.ai.b1.germering/internal/test?token=secret",
            "revision": "test",
        },
        "resource_estimate": {"vram_gib": 1, "ram_gib": 1, "disk_gib": 1},
        "visibility_roles": ["admin", "service"],
    }
    (seed / "downloadable.manifest.json").write_text(
        json.dumps(
            {
                **common,
                "id": "downloadable-llm",
                "version": "1.0.0",
                "display_name": "Downloadable LLM",
                "modality": "llm",
                "operations": ["chat"],
                "files": [{"path": "llm/model.gguf", "sha256": DOWNLOADABLE_SHA, "size_bytes": 12}],
                "runtimes": ["localai"],
                "preferred_runtime": "localai",
                "license": {
                    "name": "Test",
                    "url": "https://license.example.test/model",
                    "redistribution": "downloadable",
                    "attribution": "Test attribution",
                    "acceptance_required": True,
                },
                "execution_modes": ["hosted-inference", "downloadable"],
                "aliases": ["chat-default"],
            }
        ),
        encoding="utf-8",
    )
    (seed / "inference-only.manifest.json").write_text(
        json.dumps(
            {
                **common,
                "id": "inference-only-tts",
                "version": "1.0.0",
                "display_name": "Inference-Only TTS",
                "modality": "tts",
                "operations": ["text-to-speech"],
                "files": [{"path": "tts/model.bin", "sha256": INFERENCE_ONLY_SHA, "size_bytes": 6}],
                "runtimes": ["audio-cpu"],
                "preferred_runtime": "audio-cpu",
                "license": {"name": "Test", "redistribution": "inference-only"},
                "execution_modes": ["hosted-inference"],
                "aliases": ["tts-fast"],
            }
        ),
        encoding="utf-8",
    )


class ModelHubPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        catalog_root = Path(self.tempdir.name)
        write_catalog(catalog_root)
        self.catalog = load_catalog(catalog_root, ResourcePolicy())

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_sync_plan_downloads_missing_blob_and_keeps_verified_blob(self) -> None:
        first = build_sync_plan(self.catalog, ["chat-default"], {})
        self.assertEqual(first["status"], "planned")
        self.assertEqual(first["total_download_bytes"], 12)
        self.assertEqual(first["actions"][0]["action"], "download")
        self.assertEqual(first["actions"][0]["blob"], DOWNLOADABLE_SHA)
        self.assertTrue(first["actions"][0]["requires_license_acceptance"])
        self.assertEqual(first["actions"][0]["license"]["name"], "Test")
        self.assertEqual(first["actions"][0]["resource_estimate"], {"vram_gib": 1.0, "ram_gib": 1.0, "disk_gib": 1.0})

        second = build_sync_plan(self.catalog, ["chat-default"], {DOWNLOADABLE_SHA: 12})
        self.assertEqual(second["actions"][0]["action"], "keep")
        self.assertEqual(second["total_download_bytes"], 0)

    def test_sync_plan_skips_inference_only_model(self) -> None:
        response = build_sync_plan(self.catalog, ["tts-fast"], {})
        self.assertEqual(response["actions"][0]["model"], "tts-fast")
        self.assertEqual(response["actions"][0]["action"], "skip")
        self.assertEqual(response["actions"][0]["reason"], "model is not downloadable")
        self.assertEqual(response["actions"][0]["license"]["redistribution"], "inference-only")
        self.assertFalse(response["actions"][0]["requires_license_acceptance"])

    def test_sync_plan_redacts_source_metadata_for_external_clients(self) -> None:
        response = build_sync_plan(self.catalog, ["chat-default"], {})
        action = response["actions"][0]
        self.assertEqual(action["source"]["url"], "https://models.ai.b1.germering/internal/test")
        self.assertTrue(action["source"]["url_redacted"])
        self.assertEqual(action["model_metadata"]["source"]["url"], "https://models.ai.b1.germering/internal/test")

    def test_blob_policy_only_lists_downloadable_catalog_records(self) -> None:
        self.assertEqual(downloadable_records_for_blob(self.catalog, DOWNLOADABLE_SHA)[0]["id"], "downloadable-llm")
        self.assertEqual(downloadable_records_for_blob(self.catalog, INFERENCE_ONLY_SHA), [])

    def test_modelhub_client_allowlist_matches_alias_or_manifest_id(self) -> None:
        record = downloadable_versions_for(self.catalog, "chat-default")[0]
        self.assertTrue(model_allowed_by_allowed_set(["chat-default"], "downloadable-llm", record))
        self.assertFalse(model_allowed_by_allowed_set(["chat-default"], "tts-fast", {"id": "inference-only-tts", "aliases": ["tts-fast"]}))

    def test_allowed_models_validation_rejects_unknown_or_mixed_wildcard(self) -> None:
        self.assertEqual(validate_allowed_models(["chat-default"], self.catalog.model_or_alias_record), ["chat-default"])
        with self.assertRaisesRegex(ValueError, "cannot be mixed"):
            validate_allowed_models(["*", "chat-default"], self.catalog.model_or_alias_record)
        with self.assertRaisesRegex(ValueError, "model not found"):
            validate_allowed_models(["missing"], self.catalog.model_or_alias_record)

    def test_cidr_allowlist_validation_canonicalizes_and_rejects_invalid_entries(self) -> None:
        self.assertEqual(
            validate_cidr_allowlist(["192.168.2.12/24", "192.168.2.0/24", "2001:db8::1/64"]),
            ["192.168.2.0/24", "2001:db8::/64"],
        )
        with self.assertRaisesRegex(ValueError, "invalid CIDR"):
            validate_cidr_allowlist(["not-a-network"])
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            validate_cidr_allowlist([""])

    def test_cidr_allowlist_matches_ipv4_and_ipv4_mapped_addresses(self) -> None:
        self.assertTrue(client_ip_allowed_by_cidr(["192.168.2.0/24"], "192.168.2.44"))
        self.assertTrue(client_ip_allowed_by_cidr(["192.168.2.0/24"], "::ffff:192.168.2.44"))
        self.assertFalse(client_ip_allowed_by_cidr(["192.168.2.0/24"], "192.168.3.44"))
        self.assertFalse(client_ip_allowed_by_cidr(["192.168.2.0/24"], None))
        self.assertFalse(client_ip_allowed_by_cidr(["192.168.2.0/24"], "not-an-ip"))
        self.assertTrue(client_ip_allowed_by_cidr([], "not-an-ip"))

    def test_license_acceptance_header_parser_requires_model_version_refs(self) -> None:
        self.assertEqual(
            parse_accepted_license_refs("downloadable-llm@1.0.0, other-model@2026-07-23"),
            {"downloadable-llm@1.0.0", "other-model@2026-07-23"},
        )
        with self.assertRaisesRegex(ValueError, "invalid accepted licence reference"):
            parse_accepted_license_refs("downloadable-llm")


if __name__ == "__main__":
    unittest.main()
