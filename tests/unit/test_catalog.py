from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.catalog import CatalogError, load_catalog  # noqa: E402
from app.scheduler import ResourcePolicy  # noqa: E402


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ResourcePolicy()
        self.catalog = load_catalog(ROOT / "model-catalog", self.policy)

    def test_seed_aliases_load_from_manifests(self) -> None:
        models = self.catalog.to_openai_list()["data"]
        aliases = {model["id"]: model for model in models}
        self.assertIn("chat-default", aliases)
        self.assertEqual(aliases["chat-default"]["status"], "uninstalled")
        self.assertEqual(aliases["chat-default"]["resource_label"], "uninstalled")

        for alias in ("embedding-default", "tts-fast", "stt-default"):
            self.assertTrue(aliases[alias]["cpu_resident_candidate"], alias)
            self.assertEqual(aliases[alias]["preferred_runtime"], "audio-cpu")
            self.assertEqual(aliases[alias]["status"], "cpu-placeholder")
            self.assertIsNotNone(aliases[alias]["resolved_model"])
        catalog_models = {model["id"]: model for model in self.catalog.to_catalog()["models"]}
        self.assertEqual(catalog_models["b1-piper-en-us-amy-low"]["status"], "available")
        self.assertEqual(catalog_models["b1-piper-en-us-amy-low"]["installation_status"], "available")
        self.assertFalse(catalog_models["b1-piper-en-us-amy-low"]["downloadable"])
        self.assertIn("tts-fast", catalog_models["b1-piper-en-us-amy-low"]["aliases"])
        self.assertEqual(catalog_models["b1-minilm-l6-v2-onnx-q4"]["status"], "available")
        self.assertEqual(catalog_models["b1-minilm-l6-v2-onnx-q4"]["installation_status"], "available")
        self.assertFalse(catalog_models["b1-minilm-l6-v2-onnx-q4"]["downloadable"])
        self.assertIn("embedding-default", catalog_models["b1-minilm-l6-v2-onnx-q4"]["aliases"])
        self.assertEqual(catalog_models["b1-vosk-small-en-us-0.15"]["status"], "available")
        self.assertEqual(catalog_models["b1-vosk-small-en-us-0.15"]["installation_status"], "available")
        self.assertFalse(catalog_models["b1-vosk-small-en-us-0.15"]["downloadable"])
        self.assertIn("stt-default", catalog_models["b1-vosk-small-en-us-0.15"]["aliases"])

    def test_manifest_schema_declares_seed_and_governance_fields(self) -> None:
        schema = json.loads((ROOT / "model-catalog" / "schemas" / "model-manifest.schema.json").read_text(encoding="utf-8"))
        properties = set(schema["properties"])
        expected_governance = {
            "runtime_adapter_versions",
            "companion_files",
            "permissions",
            "deprecation",
            "measurements",
        }

        self.assertTrue(expected_governance <= properties)
        for manifest_path in sorted((ROOT / "model-catalog" / "seed").glob("*.manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest) - properties, set(), manifest_path.name)
            measurements = manifest.get("measurements", {})
            self.assertNotIn(":latest", measurements.get("source", ""), manifest_path.name)

    def test_manifest_measurements_reject_floating_latest_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "embedding-default", "modality": "embedding", "preferred_runtime": "audio-cpu", "status": "installed"}]}),
                encoding="utf-8",
            )
            (seed / "embedding.manifest.json").write_text(
                json.dumps(
                    {
                        "id": "embedding-model",
                        "version": "1.0.0",
                        "display_name": "Embedding Model",
                        "modality": "embedding",
                        "operations": ["embedding"],
                        "source": {"type": "catalog", "url": "https://models.ai.b1.germering/embedding", "revision": "1.0.0"},
                        "files": [{"path": "embedding.onnx", "sha256": "1" * 64, "size_bytes": 12}],
                        "runtimes": ["audio-cpu"],
                        "preferred_runtime": "audio-cpu",
                        "resource_estimate": {"vram_gib": 0, "ram_gib": 1, "disk_gib": 1},
                        "license": {"name": "test", "redistribution": "downloadable"},
                        "execution_modes": ["hosted-inference"],
                        "aliases": ["embedding-default"],
                        "measurements": {
                            "schema": "b1-ai-hub-model-measurements/v1",
                            "source": "local docker smoke on b1-ai-hub-audio-cpu:latest",
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CatalogError, "floating latest tags"):
                load_catalog(root, self.policy)

    def test_modelhub_versions_resolve_from_alias(self) -> None:
        versions = self.catalog.versions_for("tts-fast")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["id"], "b1-cpu-placeholder-tts")
        self.assertFalse(versions[0]["downloadable"])

        piper_versions = self.catalog.versions_for("b1-piper-en-us-amy-low")
        self.assertEqual(piper_versions[0]["status"], "available")
        self.assertFalse(piper_versions[0]["downloadable"])

        embedding_versions = self.catalog.versions_for("b1-minilm-l6-v2-onnx-q4")
        self.assertEqual(embedding_versions[0]["status"], "available")
        self.assertFalse(embedding_versions[0]["downloadable"])

        stt_versions = self.catalog.versions_for("b1-vosk-small-en-us-0.15")
        self.assertEqual(stt_versions[0]["status"], "available")
        self.assertFalse(stt_versions[0]["downloadable"])

    def test_inference_only_manifest_cannot_be_downloadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "bad-default", "modality": "tts", "preferred_runtime": "audio-cpu", "status": "installed"}]}),
                encoding="utf-8",
            )
            (seed / "bad.manifest.json").write_text(
                json.dumps(
                    {
                        "id": "bad-placeholder",
                        "version": "0.1.0",
                        "display_name": "Bad Placeholder",
                        "modality": "tts",
                        "operations": ["text-to-speech"],
                        "source": {"type": "catalog", "url": "https://models.ai.b1.germering/bad", "revision": "0.1.0"},
                        "files": [
                            {
                                "path": "bad.bin",
                                "sha256": "0" * 64,
                                "size_bytes": 1,
                            }
                        ],
                        "runtimes": ["audio-cpu"],
                        "preferred_runtime": "audio-cpu",
                        "resource_estimate": {"vram_gib": 0, "ram_gib": 0.1, "disk_gib": 0.001},
                        "license": {"name": "internal", "redistribution": "inference-only"},
                        "execution_modes": ["hosted-inference", "downloadable"],
                        "aliases": ["bad-default"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(CatalogError):
                load_catalog(root, self.policy)

    def test_manifest_alias_modality_must_match_seed_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "mixed-default", "modality": "stt", "preferred_runtime": "audio-cpu", "status": "installed"}]}),
                encoding="utf-8",
            )
            (seed / "mixed.manifest.json").write_text(
                json.dumps(
                    {
                        "id": "mixed-placeholder",
                        "version": "0.1.0",
                        "display_name": "Mixed Placeholder",
                        "modality": "tts",
                        "operations": ["text-to-speech"],
                        "source": {"type": "catalog", "url": "https://models.ai.b1.germering/mixed", "revision": "0.1.0"},
                        "files": [{"path": "mixed.bin", "sha256": "0" * 64, "size_bytes": 1}],
                        "runtimes": ["audio-cpu"],
                        "preferred_runtime": "audio-cpu",
                        "resource_estimate": {"vram_gib": 0, "ram_gib": 0.1, "disk_gib": 0.001},
                        "license": {"name": "internal", "redistribution": "inference-only"},
                        "execution_modes": ["hosted-inference"],
                        "aliases": ["mixed-default"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(CatalogError):
                load_catalog(root, self.policy)

    def test_manifest_operations_are_canonicalized_by_modality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "image-default", "modality": "image", "preferred_runtime": "comfyui", "status": "installed"}]}),
                encoding="utf-8",
            )
            (seed / "image.manifest.json").write_text(
                json.dumps(
                    {
                        "id": "image-model",
                        "version": "1.0.0",
                        "display_name": "Image Model",
                        "modality": "image",
                        "operations": ["text-to-image", "image-to-image"],
                        "source": {"type": "catalog", "url": "https://models.ai.b1.germering/image", "revision": "1.0.0"},
                        "files": [{"path": "image.safetensors", "sha256": "1" * 64, "size_bytes": 12}],
                        "runtimes": ["comfyui"],
                        "preferred_runtime": "comfyui",
                        "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
                        "license": {"name": "test", "redistribution": "downloadable"},
                        "execution_modes": ["hosted-inference"],
                        "aliases": ["image-default"],
                    }
                ),
                encoding="utf-8",
            )

            catalog = load_catalog(root, self.policy)
            alias = catalog.require_alias("image-default")

            self.assertEqual(alias.operations, ["image-generation", "image-edit"])

    def test_manifest_operations_reject_wrong_modality_and_duplicate_aliases(self) -> None:
        base_manifest = {
            "id": "tts-model",
            "version": "1.0.0",
            "display_name": "TTS Model",
            "modality": "tts",
            "operations": ["text-to-speech"],
            "source": {"type": "catalog", "url": "https://models.ai.b1.germering/tts", "revision": "1.0.0"},
            "files": [{"path": "tts.onnx", "sha256": "1" * 64, "size_bytes": 12}],
            "runtimes": ["audio-cpu"],
            "preferred_runtime": "audio-cpu",
            "resource_estimate": {"vram_gib": 0, "ram_gib": 1, "disk_gib": 1},
            "license": {"name": "test", "redistribution": "downloadable"},
            "execution_modes": ["hosted-inference"],
            "aliases": ["tts-fast"],
        }
        cases = [
            (["transcription"], "unsupported tts operation transcription"),
            (["speech", "text-to-speech"], "duplicate operation after normalization"),
        ]
        for operations, message in cases:
            with self.subTest(operations=operations), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                seed = root / "seed"
                seed.mkdir()
                (seed / "aliases.json").write_text(
                    json.dumps({"aliases": [{"alias": "tts-fast", "modality": "tts", "preferred_runtime": "audio-cpu", "status": "installed"}]}),
                    encoding="utf-8",
                )
                manifest = {**base_manifest, "operations": operations}
                (seed / "tts.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaisesRegex(CatalogError, message):
                    load_catalog(root, self.policy)

    def test_manifest_governance_metadata_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "chat-default", "modality": "llm", "preferred_runtime": "localai", "status": "installed"}]}),
                encoding="utf-8",
            )
            manifest = {
                "id": "chat-small",
                "version": "1.0.0",
                "display_name": "Chat Small",
                "modality": "llm",
                "operations": ["chat"],
                "source": {"type": "catalog", "url": "https://models.ai.b1.germering/test", "revision": "1.0.0"},
                "files": [{"path": "chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}],
                "runtimes": ["localai"],
                "preferred_runtime": "localai",
                "runtime_adapter_versions": {"localai": ">=4.7.1 <5"},
                "companion_files": [
                    {
                        "path": "tokenizer.json",
                        "required": True,
                        "description": "Tokenizer companion file required by this profile.",
                        "sha256": "2" * 64,
                        "size_bytes": 128,
                        "format": "tokenizer-json",
                    }
                ],
                "permissions": {
                    "visible_to": ["admin", "operator"],
                    "installable_by": ["admin"],
                    "downloadable_by": ["admin", "service"],
                    "inference_roles": ["admin", "service"],
                },
                "deprecation": {
                    "status": "replaced",
                    "deprecated_at": "2026-07-24T00:00:00+00:00",
                    "message": "Use chat-next for new aliases.",
                    "replacement_model": "chat-next",
                    "replacement_version": "2.0.0",
                },
                "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
                "license": {"name": "test", "redistribution": "downloadable"},
                "execution_modes": ["hosted-inference", "downloadable"],
                "aliases": ["chat-default"],
            }
            (seed / "chat.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            catalog = load_catalog(root, self.policy)
            record = catalog.versions_for("chat-default")[0]

            self.assertEqual(record["runtime_adapter_versions"], {"localai": ">=4.7.1 <5"})
            self.assertEqual(record["companion_files"][0]["path"], "tokenizer.json")
            self.assertEqual(record["permissions"]["downloadable_by"], ["admin", "service"])
            self.assertEqual(record["deprecation"]["replacement_model"], "chat-next")

    def test_manifest_governance_metadata_is_validated(self) -> None:
        base_manifest = {
            "id": "chat-small",
            "version": "1.0.0",
            "display_name": "Chat Small",
            "modality": "llm",
            "operations": ["chat"],
            "source": {"type": "catalog", "url": "https://models.ai.b1.germering/test", "revision": "1.0.0"},
            "files": [{"path": "chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}],
            "runtimes": ["localai"],
            "preferred_runtime": "localai",
            "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
            "license": {"name": "test", "redistribution": "downloadable"},
            "execution_modes": ["hosted-inference", "downloadable"],
            "aliases": ["chat-default"],
        }
        cases = [
            ({"runtime_adapter_versions": {"comfyui": ">=0.3.77"}}, "must also be listed in runtimes"),
            ({"companion_files": [{"path": "../escape"}]}, "must not contain traversal"),
            ({"permissions": {"downloadable_by": ["guest"]}}, "unsupported values"),
            ({"deprecation": {"status": "replaced"}}, "replacement_model is required"),
        ]
        for overlay, message in cases:
            with self.subTest(overlay=overlay), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                seed = root / "seed"
                seed.mkdir()
                (seed / "aliases.json").write_text(
                    json.dumps({"aliases": [{"alias": "chat-default", "modality": "llm", "preferred_runtime": "localai", "status": "installed"}]}),
                    encoding="utf-8",
                )
                (seed / "chat.manifest.json").write_text(json.dumps({**base_manifest, **overlay}), encoding="utf-8")

                with self.assertRaisesRegex(CatalogError, message):
                    load_catalog(root, self.policy)

    def test_extra_installed_manifest_activates_uninstalled_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "chat-default", "modality": "llm", "preferred_runtime": "localai", "status": "uninstalled"}]}),
                encoding="utf-8",
            )
            manifest = {
                "id": "chat-small",
                "version": "1.0.0",
                "display_name": "Chat Small",
                "modality": "llm",
                "operations": ["chat"],
                "source": {"type": "catalog", "url": "https://models.ai.b1.germering/test", "revision": "1.0.0"},
                "files": [{"path": "chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}],
                "runtimes": ["localai"],
                "preferred_runtime": "localai",
                "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
                "license": {"name": "test", "redistribution": "downloadable"},
                "execution_modes": ["hosted-inference", "downloadable"],
                "aliases": ["chat-default"],
            }

            catalog = load_catalog(root, self.policy, extra_manifests=[manifest])
            alias = catalog.require_alias("chat-default")

            self.assertEqual(alias.status, "installed")
            self.assertEqual(alias.manifest.id, "chat-small")
            self.assertEqual(alias.manifest.installation_status, "installed")

    def test_available_manifest_does_not_activate_alias_until_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "chat-default", "modality": "llm", "preferred_runtime": "localai", "status": "uninstalled"}]}),
                encoding="utf-8",
            )
            manifest = {
                "id": "chat-candidate",
                "version": "1.0.0",
                "display_name": "Chat Candidate",
                "modality": "llm",
                "operations": ["chat"],
                "source": {"type": "direct-url", "url": "https://downloads.example.org/models/", "revision": "1.0.0"},
                "files": [{"path": "chat-candidate.gguf", "sha256": "1" * 64, "size_bytes": 12}],
                "runtimes": ["localai"],
                "preferred_runtime": "localai",
                "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
                "license": {"name": "test", "redistribution": "downloadable"},
                "execution_modes": ["hosted-inference", "downloadable"],
                "installation_status": "available",
                "aliases": ["chat-default"],
            }
            (seed / "candidate.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            catalog = load_catalog(root, self.policy)
            alias = catalog.require_alias("chat-default")
            candidate = catalog.versions_for("chat-candidate")[0]

            self.assertEqual(alias.status, "uninstalled")
            self.assertIsNone(alias.manifest)
            self.assertEqual(candidate["status"], "available")
            self.assertFalse(candidate["downloadable"])

            installed = load_catalog(root, self.policy, extra_manifests=[manifest])
            installed_alias = installed.require_alias("chat-default")
            installed_candidate = installed.versions_for("chat-default")[0]

            self.assertEqual(installed_alias.status, "installed")
            self.assertEqual(installed_alias.manifest.id, "chat-candidate")
            self.assertEqual(installed_alias.manifest.installation_status, "installed")
            self.assertEqual(installed_candidate["status"], "installed")
            self.assertTrue(installed_candidate["downloadable"])

    def test_installed_manifest_exposes_smoke_measurements_and_measured_resource_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "chat-default", "modality": "llm", "preferred_runtime": "localai", "status": "uninstalled"}]}),
                encoding="utf-8",
            )
            manifest = {
                "id": "chat-small",
                "version": "1.0.0",
                "display_name": "Chat Small",
                "modality": "llm",
                "operations": ["chat"],
                "source": {"type": "catalog", "url": "https://models.ai.b1.germering/test", "revision": "1.0.0"},
                "files": [{"path": "chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}],
                "runtimes": ["localai"],
                "preferred_runtime": "localai",
                "resource_estimate": {"vram_gib": 11.0, "ram_gib": 8.0, "disk_gib": 1},
                "license": {"name": "test", "redistribution": "downloadable"},
                "execution_modes": ["hosted-inference", "downloadable"],
                "aliases": ["chat-default"],
                "measurements": {
                    "schema": "b1-ai-hub-model-measurements/v1",
                    "updated_at": "2026-07-23T00:00:00+00:00",
                    "source": "control-plane",
                    "original_resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
                    "latest_resource_estimate": {"vram_gib": 11.0, "ram_gib": 8.0, "disk_gib": 1},
                    "runs": [
                        {
                            "id": "modelsmoke_test",
                            "type": "install-smoke",
                            "status": "ok",
                            "runtime": "localai",
                            "model_alias": "chat-default",
                            "resolved_model_version": "chat-small@1.0.0",
                            "duration_ms": 1200,
                            "peak_vram_mib": 11264,
                            "peak_ram_mib": 8192,
                            "resource_estimate": {"vram_gib": 11.0, "ram_gib": 8.0, "disk_gib": 1},
                        }
                    ],
                },
            }

            catalog = load_catalog(root, self.policy, extra_manifests=[manifest])
            alias = catalog.require_alias("chat-default")
            record = catalog.versions_for("chat-default")[0]

            self.assertEqual(alias.decision.label, "offload-required")
            self.assertEqual(alias.manifest.measurements["runs"][0]["id"], "modelsmoke_test")
            self.assertEqual(record["measurements"]["latest_resource_estimate"]["vram_gib"], 11.0)

    def test_alias_policy_overrides_status_runtime_visibility_and_timeout(self) -> None:
        catalog = load_catalog(
            ROOT / "model-catalog",
            self.policy,
            alias_policies=[
                {
                    "alias": "chat-default",
                    "enabled": False,
                    "preferred_runtime": "localai",
                    "idle_timeout_seconds": 900,
                    "visibility_roles": ["admin", "operator"],
                    "notes": "maintenance window",
                }
            ],
        )
        alias = catalog.require_alias("chat-default")
        public = alias.to_openai_model()

        self.assertEqual(alias.status, "disabled")
        self.assertFalse(public["enabled"])
        self.assertEqual(public["preferred_runtime"], "localai")
        self.assertEqual(public["idle_timeout_seconds"], 900)
        self.assertEqual(public["visibility_roles"], ["admin", "operator"])
        self.assertEqual(public["alias_policy_source"], "database")
        self.assertEqual(public["notes"], "maintenance window")


if __name__ == "__main__":
    unittest.main()
