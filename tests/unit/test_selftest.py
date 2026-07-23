from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SELFTEST_PATH = ROOT / "services" / "control-plane" / "app" / "selftest.py"
spec = importlib.util.spec_from_file_location("control_plane_selftest", SELFTEST_PATH)
selftest = importlib.util.module_from_spec(spec)
sys.modules["control_plane_selftest"] = selftest
assert spec.loader is not None
spec.loader.exec_module(selftest)


class SelfTestTests(unittest.TestCase):
    def test_summarize_statuses(self) -> None:
        self.assertEqual(selftest.summarize([selftest.check("a", "ok", "ok")]), "ok")
        self.assertEqual(selftest.summarize([selftest.check("a", "warning", "warn")]), "degraded")
        self.assertEqual(selftest.summarize([selftest.check("a", "failed", "fail")]), "failed")

    def test_directory_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(selftest.check_directory_exists("root", root)["status"], "ok")
            self.assertEqual(selftest.check_writable_directory("root", root)["status"], "ok")
            self.assertEqual(selftest.check_directory_exists("missing", root / "missing")["status"], "failed")

    def test_http_result_check(self) -> None:
        self.assertEqual(selftest.check_http_result("agent", {"ok": True})["status"], "ok")
        self.assertEqual(selftest.check_http_result("agent", None, "timeout")["status"], "degraded")

    def test_gateway_security_header_failures_require_caddy_headers(self) -> None:
        self.assertEqual(
            selftest.gateway_security_header_failures(
                {
                    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                }
            ),
            [],
        )

        failures = selftest.gateway_security_header_failures(
            {
                "Strict-Transport-Security": "max-age=31536000",
                "X-Content-Type-Options": "nosniff",
            }
        )

        self.assertIn("strict-transport-security missing includesubdomains", failures)
        self.assertIn("missing x-frame-options", failures)
        self.assertIn("missing permissions-policy", failures)

    def test_runtime_production_readiness_warns_for_development_placeholders(self) -> None:
        result = selftest.runtime_production_readiness_check(
            [
                {"name": "localai", "status": "ok", "details": {"kind": "placeholder-localai", "placeholder": True}},
                {"name": "comfyui", "status": "ok", "details": {"kind": "placeholder-comfyui", "placeholder": True}},
                {"name": "audio-cpu", "status": "ok", "details": {"engine": "scaffold", "placeholder": True}},
            ],
            "development",
            ("localai", "comfyui", "audio-cpu"),
        )

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["data"]["required_placeholders"], ["localai", "comfyui", "audio-cpu"])

    def test_runtime_production_readiness_fails_for_production_placeholders(self) -> None:
        result = selftest.runtime_production_readiness_check(
            [
                {"name": "localai", "status": "ok", "details": {"kind": "placeholder-localai", "placeholder": True}},
                {"name": "comfyui", "status": "unreachable", "details": {}},
            ],
            "production",
            ("localai", "comfyui", "audio-cpu"),
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("localai", result["data"]["required_placeholders"])
        self.assertEqual(
            result["data"]["required_unhealthy"],
            [{"runtime": "comfyui", "status": "unreachable"}, {"runtime": "audio-cpu", "status": "missing"}],
        )

    def test_runtime_production_readiness_passes_for_required_real_runtimes(self) -> None:
        result = selftest.runtime_production_readiness_check(
            [
                {"name": "localai", "status": "ok", "details": {"version": "pinned"}},
                {"name": "comfyui", "status": "ok", "details": {"version": "pinned"}},
                {"name": "audio-cpu", "status": "ok", "details": {"engine": "piper"}},
            ],
            "production",
            ("localai", "comfyui", "audio-cpu"),
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["placeholder_runtimes"], [])


if __name__ == "__main__":
    unittest.main()
