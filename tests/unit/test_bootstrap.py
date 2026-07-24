from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_PATH = ROOT / "deploy" / "scripts" / "bootstrap.py"
spec = importlib.util.spec_from_file_location("b1_bootstrap", BOOTSTRAP_PATH)
bootstrap = importlib.util.module_from_spec(spec)
sys.modules["b1_bootstrap"] = bootstrap
assert spec.loader is not None
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    REQUIRED_PLAN_DIRS = {
        "data/postgres",
        "data/redis",
        "data/open-webui",
        "data/control-plane",
        "data/voicebox",
        "models/llm",
        "models/vision",
        "models/embeddings",
        "models/diffusion/checkpoints",
        "models/diffusion/diffusion_models",
        "models/diffusion/text_encoders",
        "models/diffusion/vae",
        "models/diffusion/loras",
        "models/diffusion/controlnet",
        "models/diffusion/upscale_models",
        "models/video",
        "models/tts",
        "models/stt",
        "models/blobs",
        "workflows",
        "artifacts/images",
        "artifacts/audio",
        "artifacts/video",
        "artifacts/temporary",
        "cache",
        "secrets",
        "logs",
        "backups",
    }
    REQUIRED_B1_MANAGED_DIRS = {
        "models/blobs/.partial",
        "models/runtime-views/localai",
        "models/runtime-views/comfyui",
        "models/runtime-views/voicebox",
        "models/runtime-views/audio-cpu",
        "models/quarantine/runtime-views",
        "models/quarantine/blobs",
        "restore-tests",
    }

    def test_bootstrap_generates_runtime_agent_token_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = bootstrap.bootstrap(root)
            second = bootstrap.bootstrap(root)
            token_path = root / "secrets" / "runtime_agent_token"
            artifact_token_path = root / "secrets" / "artifact_server_token"
            open_webui_key_path = root / "secrets" / "open_webui_api_key"
            self.assertIn("runtime_agent_token", first["created_secrets"])
            self.assertIn("artifact_server_token", first["created_secrets"])
            self.assertIn("open_webui_api_key", first["created_secrets"])
            self.assertIn("runtime_agent_mtls_ca.crt", first["created_secrets"])
            self.assertIn("runtime_agent_server.crt", first["created_secrets"])
            self.assertIn("runtime_agent_client.crt", first["created_secrets"])
            self.assertNotIn("runtime_agent_token", second["created_secrets"])
            self.assertNotIn("artifact_server_token", second["created_secrets"])
            self.assertNotIn("open_webui_api_key", second["created_secrets"])
            self.assertNotIn("runtime_agent_mtls_ca.crt", second["created_secrets"])
            self.assertTrue(token_path.read_text(encoding="utf-8").startswith("b1rt_"))
            self.assertTrue(artifact_token_path.read_text(encoding="utf-8").startswith("b1art_"))
            open_webui_key = open_webui_key_path.read_text(encoding="utf-8").strip()
            self.assertTrue(open_webui_key.startswith("b1k_"))
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(artifact_token_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(open_webui_key_path.stat().st_mode & 0o777, 0o640)
            self.assertTrue((root / "secrets" / "caddy-certs").is_dir())
            self.assertEqual((root / "secrets" / "caddy-certs").stat().st_mode & 0o777, 0o750)
            for filename in bootstrap.RUNTIME_AGENT_MTLS_FILES.values():
                cert_path = root / "secrets" / filename
                self.assertTrue(cert_path.exists(), filename)
                self.assertEqual(cert_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual((root / "backups").stat().st_mode & 0o777, 0o770)
            self.assertEqual((root / "restore-tests").stat().st_mode & 0o777, 0o770)
            for relative in ("configuration", "backends", "data"):
                path = root / "data" / "localai" / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for relative in ("input", "user"):
                path = root / "data" / "comfyui" / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for relative in ("comfyui-output", "comfyui-temp"):
                path = root / "artifacts" / "temporary" / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            for relative in ("generations", "profiles", "captures", "cache"):
                path = root / "data" / "voicebox" / relative
                self.assertTrue(path.is_dir(), relative)
                self.assertEqual(path.stat().st_mode & 0o777, 0o775)
            self.assertTrue((root / "cache" / "voicebox").is_dir())
            self.assertEqual((root / "cache" / "voicebox").stat().st_mode & 0o777, 0o775)
            for runtime in ("localai", "comfyui", "voicebox", "audio-cpu"):
                self.assertTrue((root / "models" / "runtime-views" / runtime).is_dir())
                self.assertEqual((root / "models" / "runtime-views" / runtime).stat().st_mode & 0o777, 0o775)
            self.assertTrue((root / "models" / "quarantine" / "runtime-views").is_dir())

    def test_bootstrap_creates_complete_external_data_tree_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = bootstrap.bootstrap(root)

            missing = sorted(
                relative
                for relative in self.REQUIRED_PLAN_DIRS | self.REQUIRED_B1_MANAGED_DIRS
                if not (root / relative).is_dir()
            )
            self.assertEqual(missing, [])
            covered_dirs = set(bootstrap.DIRS)
            for relative in bootstrap.DIRS:
                parent = Path(relative).parent
                while str(parent) != ".":
                    covered_dirs.add(str(parent))
                    parent = parent.parent
            self.assertTrue(self.REQUIRED_PLAN_DIRS <= covered_dirs)
            self.assertTrue(self.REQUIRED_B1_MANAGED_DIRS <= covered_dirs)
            created_relative = {str(Path(path).relative_to(root)) for path in result["created_dirs"]}
            self.assertEqual(created_relative, set(bootstrap.DIRS))


if __name__ == "__main__":
    unittest.main()
