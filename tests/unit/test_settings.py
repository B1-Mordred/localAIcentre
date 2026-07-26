from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "services" / "control-plane" / "app" / "settings.py"
spec = importlib.util.spec_from_file_location("control_plane_settings", SETTINGS_PATH)
settings_module = importlib.util.module_from_spec(spec)
sys.modules["control_plane_settings"] = settings_module
assert spec.loader is not None
spec.loader.exec_module(settings_module)


class SettingsTests(unittest.TestCase):
    def patch_env(self, **values: str | None) -> None:
        original = {name: os.environ.get(name) for name in values}
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.addCleanup(self.restore_env, original)

    @staticmethod
    def restore_env(original: dict[str, str | None]) -> None:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_default_self_test_tls_urls_cover_required_public_hosts(self) -> None:
        self.patch_env(
            B1_SELF_TEST_TLS_URLS="",
            B1_HOST_CHAT="ai.test.lan",
            B1_HOST_CONTROL="control.test.lan",
            B1_HOST_MEDIA="media.test.lan",
            B1_HOST_COMFY="comfy.test.lan",
            B1_HOST_VOICE="voice.test.lan",
            B1_HOST_MODELS="models.test.lan",
            B1_HOST_API="api.test.lan",
        )

        settings = settings_module.load_settings()

        self.assertEqual(
            settings.self_test_tls_urls,
            (
                "https://ai.test.lan/",
                "https://control.test.lan/",
                "https://media.test.lan/",
                "https://comfy.test.lan/healthz",
                "https://voice.test.lan/healthz",
                "https://models.test.lan/healthz",
                "https://api.test.lan/healthz",
            ),
        )

    def test_explicit_self_test_tls_urls_override_default_host_set(self) -> None:
        self.patch_env(B1_SELF_TEST_TLS_URLS="https://api.override.test/healthz,https://control.override.test/")

        settings = settings_module.load_settings()

        self.assertEqual(
            settings.self_test_tls_urls,
            ("https://api.override.test/healthz", "https://control.override.test/"),
        )

    def test_caddy_internal_ca_file_is_separate_from_self_test_ca(self) -> None:
        self.patch_env(
            B1_CADDY_INTERNAL_CA_FILE="/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt",
            B1_SELF_TEST_TLS_CA_FILE="/etc/ssl/certs/custom-test-bundle.pem",
        )

        settings = settings_module.load_settings()

        self.assertEqual(settings.caddy_internal_ca_file, "/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt")
        self.assertEqual(settings.self_test_tls_ca_file, "/etc/ssl/certs/custom-test-bundle.pem")


if __name__ == "__main__":
    unittest.main()
