import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "deploy" / "open-webui" / "b1-open-webui-entrypoint.sh"


class OpenWebUiWrapperTests(unittest.TestCase):
    def run_wrapper(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        keys = [
            "B1_OPEN_WEBUI_API_BASE_URL",
            "OPENAI_API_BASE_URL",
            "OPENAI_API_BASE_URLS",
            "RAG_OPENAI_API_BASE_URL",
            "IMAGES_OPENAI_API_BASE_URL",
            "IMAGES_EDIT_OPENAI_API_BASE_URL",
            "AUDIO_TTS_OPENAI_API_BASE_URL",
            "AUDIO_STT_OPENAI_API_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_API_KEYS",
            "RAG_OPENAI_API_KEY",
            "IMAGES_OPENAI_API_KEY",
            "IMAGES_EDIT_OPENAI_API_KEY",
            "AUDIO_TTS_OPENAI_API_KEY",
            "AUDIO_STT_OPENAI_API_KEY",
        ]
        script = "import json, os; keys = %r; print(json.dumps({k: os.environ.get(k) for k in keys}, sort_keys=True))" % keys
        wrapper_env = os.environ.copy()
        wrapper_env.update(env)
        return subprocess.run(
            ["bash", str(ENTRYPOINT), sys.executable, "-c", script],
            check=False,
            env=wrapper_env,
            text=True,
            capture_output=True,
        )

    def test_exports_generated_key_to_openai_compatible_subsystems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "open_webui_api_key"
            secret_key_file = Path(tmp) / "open_webui_secret_key"
            key_file.write_text("b1k_openwebui.unit-test\n", encoding="utf-8")
            secret_key_file.write_text("webui-secret-unit-test\n", encoding="utf-8")
            result = self.run_wrapper(
                {
                    "B1_OPEN_WEBUI_API_KEY_FILE": str(key_file),
                    "B1_OPEN_WEBUI_SECRET_KEY_FILE": str(secret_key_file),
                    "B1_OPEN_WEBUI_API_BASE_URL": "http://control-plane:8000/v1/",
                    "OPENAI_API_KEY": "should-be-overridden",
                    "RAG_OPENAI_API_KEY": "should-also-be-overridden",
                }
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        for key in (
            "OPENAI_API_KEY",
            "OPENAI_API_KEYS",
            "RAG_OPENAI_API_KEY",
            "IMAGES_OPENAI_API_KEY",
            "IMAGES_EDIT_OPENAI_API_KEY",
            "AUDIO_TTS_OPENAI_API_KEY",
            "AUDIO_STT_OPENAI_API_KEY",
        ):
            self.assertEqual(payload[key], "b1k_openwebui.unit-test")
        for key in (
            "B1_OPEN_WEBUI_API_BASE_URL",
            "OPENAI_API_BASE_URL",
            "OPENAI_API_BASE_URLS",
            "RAG_OPENAI_API_BASE_URL",
            "IMAGES_OPENAI_API_BASE_URL",
            "IMAGES_EDIT_OPENAI_API_BASE_URL",
            "AUDIO_TTS_OPENAI_API_BASE_URL",
            "AUDIO_STT_OPENAI_API_BASE_URL",
        ):
            self.assertEqual(payload[key], "http://control-plane:8000/v1")

    def test_accepts_environment_key_for_local_wrapper_tests(self) -> None:
        result = self.run_wrapper(
            {
                "OPENAI_API_KEY": "b1k_openwebui.env-only",
                "WEBUI_SECRET_KEY": "webui-secret-env-only",
                "OPENAI_API_BASE_URL": "http://control-plane:8000/v1/",
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["OPENAI_API_KEYS"], "b1k_openwebui.env-only")
        self.assertEqual(payload["AUDIO_STT_OPENAI_API_KEY"], "b1k_openwebui.env-only")
        self.assertEqual(payload["OPENAI_API_BASE_URL"], "http://control-plane:8000/v1")

    def test_fails_without_any_key(self) -> None:
        env = os.environ.copy()
        for key in list(env):
            if key.endswith("OPENAI_API_KEY") or key.endswith("OPENAI_API_KEYS") or key.startswith("B1_OPEN_WEBUI_"):
                env.pop(key, None)
        result = subprocess.run(
            ["bash", str(ENTRYPOINT), sys.executable, "-c", "print('unreachable')"],
            check=False,
            env=env,
            text=True,
            capture_output=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("B1 Open WebUI", result.stderr)
        self.assertNotIn("unreachable", result.stdout)


if __name__ == "__main__":
    unittest.main()
