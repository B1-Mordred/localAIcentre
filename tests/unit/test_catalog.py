from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.catalog import CatalogError, load_catalog, parse_manifest_payload, runtime_smoke_summary_for_manifest  # noqa: E402
from app.scheduler import ResourcePolicy  # noqa: E402


def recommendation_measurements(
    *,
    model_id: str = "embedding-model",
    version: str = "1.0.0",
    alias: str = "embedding-default",
    runtime: str = "audio-cpu",
    estimate: dict[str, float | int] | None = None,
    source: str = "local docker smoke on b1-ai-hub-audio-cpu validation build",
) -> dict[str, object]:
    resource = estimate or {"vram_gib": 0, "ram_gib": 1, "disk_gib": 1}
    return {
        "schema": "b1-ai-hub-model-measurements/v1",
        "updated_at": "2026-07-23T00:00:00+00:00",
        "source": source,
        "original_resource_estimate": dict(resource),
        "latest_resource_estimate": dict(resource),
        "runs": [
            {
                "id": f"{model_id}-local-smoke-20260723",
                "type": "catalog-smoke",
                "status": "ok",
                "runtime": runtime,
                "model_alias": alias,
                "resolved_model_version": f"{model_id}@{version}",
                "peak_vram_mib": 0,
            }
        ],
    }


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
            self.assertTrue(aliases[alias]["cpu_resident_allowed"], alias)
            self.assertIn("eligible", aliases[alias]["cpu_resident_reason"])
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
        profiles = {profile["id"]: profile for profile in self.catalog.to_catalog()["profiles"]}
        self.assertEqual(profiles["everyday-llm-7-9b-q4"]["aliases"], ["chat-default", "chat-fast"])
        self.assertEqual(profiles["everyday-llm-7-9b-q4"]["resource_label"], "recommended")
        self.assertEqual(profiles["quality-llm-12-14b-q4"]["resource_label"], "offload-required")
        self.assertEqual(profiles["short-video-12gb-workflow"]["resource_label"], "expected")
        self.assertEqual(profiles["fast-cpu-tts"]["candidate_manifest_ids"], ["b1-piper-en-us-amy-low"])

    def test_cpu_residency_policy_controls_alias_projection(self) -> None:
        catalog = load_catalog(ROOT / "model-catalog", ResourcePolicy(cpu_residency_enabled=False))
        aliases = {model["id"]: model for model in catalog.to_openai_list()["data"]}

        self.assertTrue(aliases["tts-fast"]["cpu_resident_candidate"])
        self.assertFalse(aliases["tts-fast"]["cpu_resident_allowed"])
        self.assertEqual(aliases["tts-fast"]["cpu_resident_reason"], "CPU residency is disabled by policy")

    def test_installed_cpu_recommendation_promotes_placeholder_alias_status(self) -> None:
        seed_catalog = load_catalog(ROOT / "model-catalog", self.policy)
        embedding = seed_catalog.get_manifest("b1-minilm-l6-v2-onnx-q4")
        self.assertIsNotNone(embedding)

        installed = load_catalog(ROOT / "model-catalog", self.policy, extra_manifests=[embedding.to_dict()])
        aliases = {model["id"]: model for model in installed.to_openai_list()["data"]}

        self.assertEqual(aliases["embedding-default"]["status"], "installed")
        self.assertEqual(aliases["embedding-default"]["resolved_model"]["id"], "b1-minilm-l6-v2-onnx-q4")

    def test_manifest_schema_declares_seed_and_governance_fields(self) -> None:
        schema = json.loads((ROOT / "model-catalog" / "schemas" / "model-manifest.schema.json").read_text(encoding="utf-8"))
        properties = set(schema["properties"])
        expected_governance = {
            "runtime_adapter_versions",
            "companion_files",
            "permissions",
            "deprecation",
            "runtime_smoke",
            "measurements",
        }

        self.assertTrue(expected_governance <= properties)
        for manifest_path in sorted((ROOT / "model-catalog" / "seed").glob("*.manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest) - properties, set(), manifest_path.name)
            measurements = manifest.get("measurements", {})
            self.assertNotIn(":latest", measurements.get("source", ""), manifest_path.name)
            if manifest.get("installation_status") == "available":
                self.assertTrue(measurements, manifest_path.name)
                self.assertIn("updated_at", measurements, manifest_path.name)
                self.assertIn("latest_resource_estimate", measurements, manifest_path.name)
                self.assertGreaterEqual(len(measurements.get("runs", [])), 1, manifest_path.name)
                self.assertIn("url", manifest["license"], manifest_path.name)
                self.assertIn("attribution", manifest["license"], manifest_path.name)
                self.assertIn("acceptance_required", manifest["license"], manifest_path.name)

    def test_model_profile_schema_declares_seed_contract(self) -> None:
        schema = json.loads((ROOT / "model-catalog" / "schemas" / "model-profiles.schema.json").read_text(encoding="utf-8"))
        seed = json.loads((ROOT / "model-catalog" / "seed" / "model-profiles.json").read_text(encoding="utf-8"))
        profile_properties = set(schema["$defs"]["modelProfile"]["properties"])
        expected_controls = {
            "aliases",
            "preferred_runtimes",
            "target_class",
            "selection_guidance",
            "resource_estimate",
            "target_resource_label",
            "runtime_policy",
            "default_limits",
            "candidate_manifest_ids",
        }

        self.assertEqual(seed["schema"], "b1-ai-hub-model-profiles/v1")
        self.assertTrue(expected_controls <= profile_properties)
        for profile in seed["profiles"]:
            self.assertEqual(set(profile) - profile_properties, set(), profile["id"])
            self.assertNotRegex(json.dumps(profile), r"\b(latest|nightly)\b", profile["id"])

    def test_model_profiles_validate_alias_runtime_operation_and_candidates(self) -> None:
        def write_catalog(root: Path, profile: dict[str, object]) -> None:
            seed = root / "seed"
            seed.mkdir()
            (seed / "aliases.json").write_text(
                json.dumps({"aliases": [{"alias": "chat-default", "modality": "llm", "preferred_runtime": "localai", "status": "uninstalled"}]}),
                encoding="utf-8",
            )
            (seed / "model-profiles.json").write_text(json.dumps({"schema": "b1-ai-hub-model-profiles/v1", "profiles": [profile]}), encoding="utf-8")

        base_profile = {
            "id": "chat-profile",
            "display_name": "Chat Profile",
            "aliases": ["chat-default"],
            "modality": "llm",
            "operations": ["chat"],
            "preferred_runtimes": ["localai"],
            "target_class": "7B Q4 chat model",
            "selection_guidance": "Install only pinned manifests with smoke measurements.",
            "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 4},
            "target_resource_label": "recommended",
            "runtime_policy": "gpu-exclusive",
            "default_limits": {"context_tokens": 8192},
        }
        cases = [
            ({"aliases": ["missing-default"]}, "unknown aliases"),
            ({"modality": "tts", "operations": ["text-to-speech"]}, "modality mismatch"),
            ({"operations": ["image-generation"]}, "unsupported llm operation"),
            ({"preferred_runtimes": ["comfyui"]}, "does not include alias defaults"),
            ({"runtime_policy": "cpu-resident"}, "requires audio-cpu"),
            ({"candidate_manifest_ids": ["missing-model"]}, "unknown manifests"),
        ]
        for overlay, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_catalog(root, {**base_profile, **overlay})
                with self.assertRaisesRegex(CatalogError, message):
                    load_catalog(root, self.policy)

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
                            "updated_at": "2026-07-23T00:00:00+00:00",
                            "source": "local docker smoke on b1-ai-hub-audio-cpu:latest",
                            "original_resource_estimate": {"vram_gib": 0, "ram_gib": 1, "disk_gib": 1},
                            "latest_resource_estimate": {"vram_gib": 0, "ram_gib": 1, "disk_gib": 1},
                            "runs": [
                                {
                                    "id": "embedding-model-local-smoke-20260723",
                                    "type": "catalog-smoke",
                                    "status": "ok",
                                    "runtime": "audio-cpu",
                                    "model_alias": "embedding-default",
                                    "resolved_model_version": "embedding-model@1.0.0",
                                    "peak_vram_mib": 0,
                                }
                            ],
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

    def test_manifest_runtime_smoke_contract_round_trips_and_validates_comfyui_prompt(self) -> None:
        prompt = {
            "1": {
                "class_type": "B1RuntimeTinyImage",
                "inputs": {"width": 64, "height": 64},
            }
        }
        manifest = parse_manifest_payload(
            {
                "id": "image-small",
                "version": "1.0.0",
                "display_name": "Image Small",
                "modality": "image",
                "operations": ["image-generation"],
                "source": {"type": "catalog", "url": "https://models.ai.b1.germering/image", "revision": "1.0.0"},
                "files": [{"path": "image-small.safetensors", "sha256": "1" * 64, "size_bytes": 12}],
                "runtimes": ["comfyui"],
                "preferred_runtime": "comfyui",
                "resource_estimate": {"vram_gib": 6, "ram_gib": 5, "disk_gib": 4},
                "license": {"name": "test", "redistribution": "downloadable"},
                "execution_modes": ["hosted-inference"],
                "aliases": ["image-default"],
                "runtime_smoke": {
                    "schema": "b1-ai-hub-runtime-smoke/v1",
                    "description": "Tiny native queue prompt for model installation smoke.",
                    "comfyui": {"prompt": prompt, "timeout_seconds": 90},
                },
            }
        )

        self.assertEqual(manifest.runtime_smoke["comfyui"]["prompt"], prompt)
        self.assertEqual(manifest.to_dict()["runtime_smoke"]["comfyui"]["timeout_seconds"], 90)
        summary = runtime_smoke_summary_for_manifest(manifest)
        self.assertTrue(summary["configured"])
        self.assertEqual(summary["configured_runtimes"], ["comfyui"])
        self.assertTrue(summary["preferred_runtime_configured"])
        self.assertEqual(summary["runtimes"]["comfyui"]["prompt_node_count"], 1)
        self.assertNotIn("B1RuntimeTinyImage", json.dumps(summary))

    def test_manifest_runtime_smoke_contract_is_validated(self) -> None:
        base_manifest = {
            "id": "image-small",
            "version": "1.0.0",
            "display_name": "Image Small",
            "modality": "image",
            "operations": ["image-generation"],
            "source": {"type": "catalog", "url": "https://models.ai.b1.germering/image", "revision": "1.0.0"},
            "files": [{"path": "image-small.safetensors", "sha256": "1" * 64, "size_bytes": 12}],
            "runtimes": ["comfyui"],
            "preferred_runtime": "comfyui",
            "resource_estimate": {"vram_gib": 6, "ram_gib": 5, "disk_gib": 4},
            "license": {"name": "test", "redistribution": "downloadable"},
            "execution_modes": ["hosted-inference"],
            "aliases": ["image-default"],
        }
        cases = [
            ({"schema": "wrong", "comfyui": {"prompt": {"1": {"class_type": "B1RuntimeTinyImage", "inputs": {}}}}}, "schema is unsupported"),
            ({"schema": "b1-ai-hub-runtime-smoke/v1"}, "must configure at least one runtime"),
            ({"schema": "b1-ai-hub-runtime-smoke/v1", "localai": {"request": {}}}, "must also be listed in runtimes"),
            ({"schema": "b1-ai-hub-runtime-smoke/v1", "comfyui": {"request": {}}}, "prompt must be a non-empty ComfyUI prompt object"),
            ({"schema": "b1-ai-hub-runtime-smoke/v1", "comfyui": {"prompt": {"1": {"inputs": {}}}}}, "class_type must be a non-empty string"),
            ({"schema": "b1-ai-hub-runtime-smoke/v1", "comfyui": {"prompt": {"1": {"class_type": "B1RuntimeTinyImage"}}, "timeout_seconds": 0}}, "timeout_seconds"),
        ]
        for runtime_smoke, message in cases:
            with self.subTest(runtime_smoke=runtime_smoke), self.assertRaisesRegex(CatalogError, message):
                parse_manifest_payload({**base_manifest, "runtime_smoke": runtime_smoke})

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
            ({"files": [{"path": "weights/%2e%2e/chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/safe%2Fchat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/safe%5Cchat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/safe%3Ftoken.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/safe%23fragment.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/%00chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/%/chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/%2/chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/%zz/chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "weights/%ffchat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "encoded path-control"),
            ({"files": [{"path": "C:/chat-small.gguf", "sha256": "1" * 64, "size_bytes": 12}]}, "traversal"),
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
                "license": {
                    "name": "test",
                    "url": "https://licenses.example.org/test",
                    "redistribution": "downloadable",
                    "attribution": "Synthetic catalog recommendation for parser tests.",
                    "acceptance_required": False,
                },
                "execution_modes": ["hosted-inference", "downloadable"],
                "installation_status": "available",
                "aliases": ["chat-default"],
                "measurements": recommendation_measurements(
                    model_id="chat-candidate",
                    version="1.0.0",
                    alias="chat-default",
                    runtime="localai",
                    estimate={"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
                ),
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

    def test_available_manifest_requires_recommendation_evidence(self) -> None:
        base_manifest = {
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
            "license": {
                "name": "test",
                "url": "https://licenses.example.org/test",
                "redistribution": "downloadable",
                "attribution": "Synthetic catalog recommendation for parser tests.",
                "acceptance_required": False,
            },
            "execution_modes": ["hosted-inference", "downloadable"],
            "installation_status": "available",
            "aliases": ["chat-default"],
        }
        cases = [
            ({}, "measurements is required"),
            ({"license": {"name": "test", "redistribution": "downloadable", "attribution": "missing URL", "acceptance_required": False}}, "license.url is required"),
            (
                {
                    "measurements": recommendation_measurements(
                        model_id="different-model",
                        version="1.0.0",
                        alias="chat-default",
                        runtime="localai",
                        estimate={"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
                    )
                },
                "resolved_model_version must be chat-candidate@1.0.0",
            ),
        ]
        for overlay, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                seed = root / "seed"
                seed.mkdir()
                (seed / "aliases.json").write_text(
                    json.dumps({"aliases": [{"alias": "chat-default", "modality": "llm", "preferred_runtime": "localai", "status": "uninstalled"}]}),
                    encoding="utf-8",
                )
                manifest = {**base_manifest, **overlay}
                (seed / "candidate.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaisesRegex(CatalogError, message):
                    load_catalog(root, self.policy)

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
