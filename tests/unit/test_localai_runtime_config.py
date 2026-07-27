from __future__ import annotations

import importlib.util
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "localai" / "b1_localai_config.py"
SPEC = importlib.util.spec_from_file_location("b1_localai_config", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
b1_localai_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b1_localai_config)


class LocalAIManagedConfigTests(unittest.TestCase):
    def write_minimal_gguf(self, path: Path, metadata: dict[str, str]) -> None:
        payload = bytearray()
        payload.extend(b"GGUF")
        payload.extend(struct.pack("<I", 3))
        payload.extend(struct.pack("<Q", 0))
        payload.extend(struct.pack("<Q", len(metadata)))
        for key, value in metadata.items():
            key_bytes = key.encode("utf-8")
            value_bytes = value.encode("utf-8")
            payload.extend(struct.pack("<Q", len(key_bytes)))
            payload.extend(key_bytes)
            payload.extend(struct.pack("<I", 8))
            payload.extend(struct.pack("<Q", len(value_bytes)))
            payload.extend(value_bytes)
        path.write_bytes(bytes(payload))

    def write_runtime_view(self, root: Path, *, model_id: str = "b1-chat", version: str = "1.0.0") -> Path:
        view_root = root / "models" / model_id / version
        view_root.mkdir(parents=True)
        (view_root / "model.gguf").write_bytes(b"gguf")
        manifest = {
            "id": model_id,
            "version": version,
            "modality": "llm",
            "runtimes": ["localai"],
            "preferred_runtime": "localai",
            "resource_estimate": {"context_tokens": 8192},
            "files": [{"path": "model.gguf", "format": "gguf"}],
        }
        (view_root / "manifest.b1.json").write_text(json.dumps({"manifest": manifest}), encoding="utf-8")
        return view_root

    def test_sync_generates_localai_yaml_from_runtime_view_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_runtime_view(root)
            config_dir = root / "configuration"

            result = b1_localai_config.sync_managed_configs(root / "models", config_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["managed_count"], 1)
            config = config_dir / "b1-managed-b1-chat-1.0.0.yaml"
            body = config.read_text(encoding="utf-8")
            combined = (config_dir / "b1-managed-models.yaml").read_text(encoding="utf-8")
            self.assertIn("# b1-ai-hub managed localai model config", body)
            self.assertIn("# prompt_family: tokenizer-template", body)
            self.assertIn("name: 'b1-chat'", body)
            self.assertIn("backend: 'llama'", body)
            self.assertIn("model: 'b1-chat/1.0.0/model.gguf'", body)
            self.assertIn("context_size: 4096", body)
            self.assertIn("parallel:1", body)
            self.assertIn("batch: 128", body)
            self.assertIn("gpu_layers: 99999999", body)
            self.assertIn("mmap: true", body)
            self.assertIn("mmlock: false", body)
            self.assertIn("low_vram: true", body)
            self.assertIn("f16: true", body)
            self.assertIn("- name: 'b1-chat'", combined)
            self.assertIn("  parameters:", combined)
            self.assertIn("    model: 'b1-chat/1.0.0/model.gguf'", combined)

    def test_sync_removes_only_stale_managed_configs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_dir = root / "configuration"
            config_dir.mkdir()
            stale = config_dir / "b1-managed-old-1.yaml"
            stale.write_text("# b1-ai-hub managed localai model config\nname: old\n", encoding="utf-8")
            unmanaged = config_dir / "b1-managed-human.yaml"
            unmanaged.write_text("# not managed\nname: human\n", encoding="utf-8")

            result = b1_localai_config.sync_managed_configs(root / "models", config_dir)

            self.assertEqual(result["managed_count"], 0)
            self.assertEqual(result["removed"], ["b1-managed-old-1.yaml"])
            self.assertFalse(stale.exists())
            self.assertTrue(unmanaged.exists())
            self.assertTrue((config_dir / "b1-managed-models.yaml").exists())

    def test_sync_ignores_non_localai_or_missing_gguf_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view_root = root / "models" / "b1-image" / "1"
            view_root.mkdir(parents=True)
            (view_root / "manifest.b1.json").write_text(
                json.dumps(
                    {
                        "manifest": {
                            "id": "b1-image",
                            "version": "1",
                            "runtimes": ["comfyui"],
                            "files": [{"path": "model.safetensors", "format": "safetensors"}],
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = b1_localai_config.sync_managed_configs(root / "models", root / "configuration")

            self.assertEqual(result["managed_count"], 0)
            self.assertEqual((root / "configuration" / "b1-managed-models.yaml").read_text(encoding="utf-8"), "# b1-ai-hub managed localai model config\n[]\n")

    def test_context_size_can_be_capped_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_runtime_view(root)
            old = os.environ.get("B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE")
            os.environ["B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE"] = "2048"
            try:
                b1_localai_config.sync_managed_configs(root / "models", root / "configuration")
            finally:
                if old is None:
                    os.environ.pop("B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE", None)
                else:
                    os.environ["B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE"] = old
            body = (root / "configuration" / "b1-managed-b1-chat-1.0.0.yaml").read_text(encoding="utf-8")
            self.assertIn("context_size: 2048", body)

    def test_large_gguf_uses_conservative_gpu_layer_and_context_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view_root = root / "models" / "b1-large-chat" / "1.0.0"
            view_root.mkdir(parents=True)
            (view_root / "model.gguf").write_bytes(b"gguf")
            manifest = {
                "id": "b1-large-chat",
                "version": "1.0.0",
                "modality": "llm",
                "runtimes": ["localai"],
                "preferred_runtime": "localai",
                "resource_estimate": {"context_tokens": 8192},
                "files": [{"path": "model.gguf", "format": "gguf", "size_bytes": 7 * 1024**3}],
            }
            (view_root / "manifest.b1.json").write_text(json.dumps({"manifest": manifest}), encoding="utf-8")
            old_values = {
                key: os.environ.get(key)
                for key in (
                    "B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE",
                    "B1_LOCALAI_MANAGED_LARGE_GPU_LAYERS",
                    "B1_LOCALAI_MANAGED_BATCH",
                    "B1_LOCALAI_MANAGED_LOW_VRAM",
                )
            }
            os.environ["B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE"] = "2048"
            os.environ["B1_LOCALAI_MANAGED_LARGE_GPU_LAYERS"] = "18"
            os.environ["B1_LOCALAI_MANAGED_BATCH"] = "96"
            os.environ["B1_LOCALAI_MANAGED_LOW_VRAM"] = "true"
            try:
                b1_localai_config.sync_managed_configs(root / "models", root / "configuration")
            finally:
                for key, value in old_values.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            body = (root / "configuration" / "b1-managed-b1-large-chat-1.0.0.yaml").read_text(encoding="utf-8")
            self.assertIn("context_size: 2048", body)
            self.assertIn("gpu_layers: 18", body)
            self.assertIn("batch: 96", body)
            self.assertIn("low_vram: true", body)

    def test_gemma4_gguf_uses_localai_prompt_template_and_card_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view_root = root / "models" / "b1-gemma4-chat" / "1.0.0"
            view_root.mkdir(parents=True)
            self.write_minimal_gguf(view_root / "model.gguf", {"general.architecture": "gemma4"})
            manifest = {
                "id": "b1-gemma4-chat",
                "version": "1.0.0",
                "modality": "llm",
                "runtimes": ["localai"],
                "preferred_runtime": "localai",
                "resource_estimate": {"context_tokens": 2048},
                "files": [{"path": "model.gguf", "format": "gguf", "size_bytes": 7 * 1024**3}],
            }
            (view_root / "manifest.b1.json").write_text(json.dumps({"manifest": manifest}), encoding="utf-8")

            b1_localai_config.sync_managed_configs(root / "models", root / "configuration")

            body = (root / "configuration" / "b1-managed-b1-gemma4-chat-1.0.0.yaml").read_text(encoding="utf-8")
            self.assertIn("# prompt_family: gemma4", body)
            self.assertIn("temperature: 1.0", body)
            self.assertIn("top_p: 0.95", body)
            self.assertIn("top_k: 64", body)
            self.assertIn("use_jinja:false", body)
            self.assertIn("template:", body)
            self.assertIn("<|turn>user", body)
            self.assertIn("<|turn>model", body)
            self.assertIn("join_chat_messages_by_character: ''", body)
            self.assertIn("enable_thinking: false", body)
            self.assertIn("disable_reasoning: true", body)
            self.assertIn("stopwords:", body)

    def test_gemma4_e4b_uses_quality_profile_without_raising_global_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            view_root = root / "models" / "b1-unsloth-gemma-4-e4b-it-gguf-q6_k-localai" / "1.0.0"
            view_root.mkdir(parents=True)
            self.write_minimal_gguf(view_root / "gemma-4-E4B-it-Q6_K.gguf", {"general.architecture": "gemma4"})
            manifest = {
                "id": "b1-unsloth-gemma-4-e4b-it-gguf-q6_k-localai",
                "version": "1.0.0",
                "display_name": "Unsloth Gemma 4 E4B IT Q6_K GGUF",
                "modality": "llm",
                "runtimes": ["localai"],
                "preferred_runtime": "localai",
                "resource_estimate": {"context_tokens": 8192},
                "files": [{"path": "gemma-4-E4B-it-Q6_K.gguf", "format": "gguf", "size_bytes": 7 * 1024**3}],
            }
            (view_root / "manifest.b1.json").write_text(json.dumps({"manifest": manifest}), encoding="utf-8")
            old_values = {
                key: os.environ.get(key)
                for key in (
                    "B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE",
                    "B1_LOCALAI_MANAGED_GEMMA4_E4B_CONTEXT_SIZE",
                    "B1_LOCALAI_MANAGED_GEMMA4_E4B_GPU_LAYERS",
                )
            }
            os.environ["B1_LOCALAI_MANAGED_MAX_CONTEXT_SIZE"] = "2048"
            os.environ.pop("B1_LOCALAI_MANAGED_GEMMA4_E4B_CONTEXT_SIZE", None)
            os.environ.pop("B1_LOCALAI_MANAGED_GEMMA4_E4B_GPU_LAYERS", None)
            try:
                b1_localai_config.sync_managed_configs(root / "models", root / "configuration")
            finally:
                for key, value in old_values.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            body = (
                root
                / "configuration"
                / "b1-managed-b1-unsloth-gemma-4-e4b-it-gguf-q6_k-localai-1.0.0.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn("# prompt_family: gemma4", body)
            self.assertIn("context_size: 4096", body)
            self.assertIn("gpu_layers: 32", body)


if __name__ == "__main__":
    unittest.main()
