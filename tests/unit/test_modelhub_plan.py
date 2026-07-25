from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


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
    public_modelhub_metadata,
    role_allowed_by_manifest_permissions,
    validate_allowed_models,
    validate_cidr_allowlist,
    validate_model_identifier,
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

    def test_sync_plan_filters_versions_before_selecting_downloadable_manifest(self) -> None:
        admin_sha = "c" * 64
        service_sha = "d" * 64
        admin_only = {
            "id": "downloadable-llm",
            "version": "2.0.0",
            "downloadable": True,
            "files": [{"path": "admin.gguf", "sha256": admin_sha, "size_bytes": 20}],
            "license": {"name": "Admin", "redistribution": "downloadable"},
            "execution_modes": ["downloadable"],
            "permissions": {"downloadable_by": ["admin"]},
        }
        service_allowed = {
            "id": "downloadable-llm",
            "version": "1.0.0",
            "downloadable": True,
            "files": [{"path": "service.gguf", "sha256": service_sha, "size_bytes": 12}],
            "license": {"name": "Service", "redistribution": "downloadable"},
            "execution_modes": ["downloadable"],
            "permissions": {"downloadable_by": ["service"]},
        }
        catalog = SimpleNamespace(
            model_or_alias_record=lambda model_id: {"id": "downloadable-llm", "version": "2.0.0"},
            versions_for=lambda model_id: [admin_only, service_allowed],
        )

        response = build_sync_plan(
            catalog,
            ["chat-default"],
            {},
            record_filter=lambda model_id, record: "service" in (record.get("permissions") or {}).get("downloadable_by", []),
        )

        self.assertEqual(len(response["actions"]), 1)
        self.assertEqual(response["actions"][0]["version"], "1.0.0")
        self.assertEqual(response["actions"][0]["blob"], service_sha)
        self.assertEqual(response["actions"][0]["download_bytes"], 12)
        self.assertEqual(response["total_download_bytes"], 12)

    def test_sync_plan_refuses_when_filter_allows_no_versions(self) -> None:
        record = {
            "id": "downloadable-llm",
            "version": "1.0.0",
            "downloadable": True,
            "files": [{"path": "model.gguf", "sha256": "e" * 64, "size_bytes": 12}],
            "license": {"name": "Admin", "redistribution": "downloadable"},
            "execution_modes": ["downloadable"],
            "permissions": {"downloadable_by": ["admin"]},
        }
        catalog = SimpleNamespace(
            model_or_alias_record=lambda model_id: record,
            versions_for=lambda model_id: [record],
        )

        with self.assertRaisesRegex(ValueError, "not permitted"):
            build_sync_plan(catalog, ["chat-default"], {}, record_filter=lambda model_id, version: False)

    def test_sync_plan_redacts_source_metadata_for_external_clients(self) -> None:
        response = build_sync_plan(self.catalog, ["chat-default"], {})
        action = response["actions"][0]
        self.assertEqual(action["source"]["url"], "https://models.ai.b1.germering/internal/test")
        self.assertTrue(action["source"]["url_redacted"])
        self.assertEqual(action["model_metadata"]["source"]["url"], "https://models.ai.b1.germering/internal/test")

    def test_public_modelhub_metadata_redacts_source_urls_without_mutating_input(self) -> None:
        payload = {
            "id": "downloadable-llm",
            "source": {
                "type": "direct-url",
                "url": "https://user:secret@downloads.example.test:8443/models/model.gguf?token=secret#fragment",
                "revision": "1.0.0",
            },
            "model_metadata": {
                "source": {
                    "type": "huggingface",
                    "url": "https://huggingface.co/org/model?download_token=secret",
                    "revision": "main",
                }
            },
        }

        public = public_modelhub_metadata(payload)

        self.assertEqual(public["source"]["url"], "https://downloads.example.test:8443/models/model.gguf")
        self.assertTrue(public["source"]["url_redacted"])
        self.assertEqual(public["model_metadata"]["source"]["url"], "https://huggingface.co/org/model")
        self.assertTrue(public["model_metadata"]["source"]["url_redacted"])
        self.assertEqual(payload["source"]["url"], "https://user:secret@downloads.example.test:8443/models/model.gguf?token=secret#fragment")

    def test_blob_policy_only_lists_downloadable_catalog_records(self) -> None:
        self.assertEqual(downloadable_records_for_blob(self.catalog, DOWNLOADABLE_SHA)[0]["id"], "downloadable-llm")
        self.assertEqual(downloadable_records_for_blob(self.catalog, INFERENCE_ONLY_SHA), [])

    def test_modelhub_client_allowlist_matches_alias_or_manifest_id(self) -> None:
        record = downloadable_versions_for(self.catalog, "chat-default")[0]
        self.assertTrue(model_allowed_by_allowed_set(["chat-default"], "downloadable-llm", record))
        self.assertFalse(model_allowed_by_allowed_set(["chat-default"], "tts-fast", {"id": "inference-only-tts", "aliases": ["tts-fast"]}))

    def test_manifest_permissions_gate_read_download_install_and_inference_roles(self) -> None:
        record = {
            "id": "governed-model",
            "visibility_roles": ["admin", "operator"],
            "permissions": {
                "downloadable_by": ["admin"],
                "installable_by": ["admin", "operator"],
                "inference_roles": ["admin", "service"],
            },
        }

        self.assertTrue(role_allowed_by_manifest_permissions(record, "operator", "read"))
        self.assertFalse(role_allowed_by_manifest_permissions(record, "service", "read"))
        self.assertTrue(role_allowed_by_manifest_permissions(record, "admin", "download"))
        self.assertFalse(role_allowed_by_manifest_permissions(record, "operator", "download"))
        self.assertTrue(role_allowed_by_manifest_permissions(record, "operator", "install"))
        self.assertFalse(role_allowed_by_manifest_permissions(record, "user", "install"))
        self.assertTrue(role_allowed_by_manifest_permissions(record, "service", "inference"))
        self.assertFalse(role_allowed_by_manifest_permissions(record, "creator", "inference"))
        self.assertTrue(role_allowed_by_manifest_permissions({"id": "unrestricted"}, "user", "download"))

    def test_allowed_models_validation_rejects_unknown_or_mixed_wildcard(self) -> None:
        self.assertEqual(validate_allowed_models(["chat-default"], self.catalog.model_or_alias_record), ["chat-default"])
        with self.assertRaisesRegex(ValueError, "cannot be mixed"):
            validate_allowed_models(["*", "chat-default"], self.catalog.model_or_alias_record)
        with self.assertRaisesRegex(ValueError, "model not found"):
            validate_allowed_models(["missing"], self.catalog.model_or_alias_record)

    def test_model_identifier_validation_rejects_unsafe_values_before_lookup(self) -> None:
        self.assertEqual(validate_model_identifier("chat-default"), "chat-default")

        calls: list[str] = []

        def record_provider(model_id: str) -> dict[str, Any] | None:
            calls.append(model_id)
            raise AssertionError("unsafe model identifiers must not reach catalog lookup")

        with self.assertRaisesRegex(ValueError, "invalid model id or alias"):
            validate_allowed_models(["../secret"], record_provider)
        self.assertEqual(calls, [])

    def test_sync_plan_rejects_unsafe_model_identifier_before_catalog_lookup(self) -> None:
        catalog = SimpleNamespace(
            model_or_alias_record=lambda model_id: (_ for _ in ()).throw(
                AssertionError("unsafe model identifiers must not reach catalog lookup")
            ),
            versions_for=lambda model_id: [],
        )

        with self.assertRaisesRegex(ValueError, "invalid model id or alias"):
            build_sync_plan(catalog, ["bad%2Fmodel"], {})

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
