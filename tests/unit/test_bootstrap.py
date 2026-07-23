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
    def test_bootstrap_generates_runtime_agent_token_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = bootstrap.bootstrap(root)
            second = bootstrap.bootstrap(root)
            token_path = root / "secrets" / "runtime_agent_token"
            open_webui_key_path = root / "secrets" / "open_webui_api_key"
            self.assertIn("runtime_agent_token", first["created_secrets"])
            self.assertIn("open_webui_api_key", first["created_secrets"])
            self.assertIn("runtime_agent_mtls_ca.crt", first["created_secrets"])
            self.assertIn("runtime_agent_server.crt", first["created_secrets"])
            self.assertIn("runtime_agent_client.crt", first["created_secrets"])
            self.assertNotIn("runtime_agent_token", second["created_secrets"])
            self.assertNotIn("open_webui_api_key", second["created_secrets"])
            self.assertNotIn("runtime_agent_mtls_ca.crt", second["created_secrets"])
            self.assertTrue(token_path.read_text(encoding="utf-8").startswith("b1rt_"))
            open_webui_key = open_webui_key_path.read_text(encoding="utf-8").strip()
            self.assertTrue(open_webui_key.startswith("b1k_"))
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(open_webui_key_path.stat().st_mode & 0o777, 0o640)
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


if __name__ == "__main__":
    unittest.main()
