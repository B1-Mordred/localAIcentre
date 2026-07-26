from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "localai" / "b1_localai_config.py"
SPEC = importlib.util.spec_from_file_location("b1_localai_config", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
b1_localai_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b1_localai_config)


class LocalAIManagedConfigTests(unittest.TestCase):
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
            self.assertIn("name: 'b1-chat'", body)
            self.assertIn("backend: 'llama'", body)
            self.assertIn("model: 'b1-chat/1.0.0/model.gguf'", body)
            self.assertIn("context_size: 4096", body)
            self.assertIn("parallel:1", body)
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


if __name__ == "__main__":
    unittest.main()
