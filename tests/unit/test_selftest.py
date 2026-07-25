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

    def test_hardware_resource_policy_passes_when_metrics_satisfy_policy(self) -> None:
        result = selftest.hardware_resource_policy_check(
            {
                "gpu": {
                    "available": True,
                    "devices": [
                        {
                            "name": "RTX 3060",
                            "memory_total_mib": 12288,
                            "memory_free_mib": 11264,
                        }
                    ],
                },
                "memory": {
                    "total_bytes": 32 * 1024**3,
                    "available_bytes": 24 * 1024**3,
                },
            },
            {
                "gpu_total_vram_gib": 12.0,
                "gpu_reserve_vram_gib": 1.5,
                "host_total_ram_gib": 32.0,
                "host_reserve_ram_gib": 6.0,
            },
            "production",
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["observed"]["largest_gpu_memory_total_mib"], 12288)

    def test_hardware_resource_policy_fails_undersized_production_host(self) -> None:
        result = selftest.hardware_resource_policy_check(
            {
                "gpu": {
                    "available": True,
                    "devices": [
                        {
                            "name": "RTX 3060 Laptop GPU",
                            "memory_total_mib": 6144,
                            "memory_free_mib": 4096,
                        }
                    ],
                },
                "memory": {
                    "total_bytes": 31 * 1024**3,
                    "available_bytes": 5 * 1024**3,
                },
            },
            {
                "gpu_total_vram_gib": 12.0,
                "gpu_reserve_vram_gib": 1.5,
                "host_total_ram_gib": 32.0,
                "host_reserve_ram_gib": 6.0,
            },
            "production",
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("largest GPU VRAM is 6144 MiB", result["detail"])
        self.assertIn("host RAM is 31744 MiB", result["detail"])
        self.assertIn("host available RAM is 5120 MiB", result["detail"])

    def test_hardware_resource_policy_warns_in_development(self) -> None:
        result = selftest.hardware_resource_policy_check(
            None,
            {
                "gpu_total_vram_gib": 12.0,
                "gpu_reserve_vram_gib": 1.5,
                "host_total_ram_gib": 32.0,
                "host_reserve_ram_gib": 6.0,
            },
            "development",
        )

        self.assertEqual(result["status"], "warning")
        self.assertIn("development mode permits bootstrapping only", result["detail"])

    def test_runtime_agent_mutation_guard_passes_for_hardened_status(self) -> None:
        result = selftest.runtime_agent_mutation_guard_check(
            {
                "auth_configured": True,
                "allow_missing_auth": False,
                "mtls_enabled": True,
                "client_cert_required": True,
                "mutations_enabled": False,
                "mutation_rate_limit_per_minute": 12,
                "allowed_services": ["control-plane", "localai", "comfyui"],
                "runtime_action_services": ["localai", "comfyui"],
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["mutation_rate_limit_per_minute"], 12)

    def test_runtime_agent_mutation_guard_fails_open_auth_or_missing_rate_limit(self) -> None:
        result = selftest.runtime_agent_mutation_guard_check(
            {
                "auth_configured": False,
                "allow_missing_auth": True,
                "mtls_enabled": False,
                "client_cert_required": False,
                "mutation_rate_limit_per_minute": 0,
                "allowed_services": ["localai"],
                "runtime_action_services": ["localai"],
            }
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("bearer token is not configured", result["detail"])
        self.assertIn("missing-auth bypass is enabled", result["detail"])
        self.assertIn("mutation rate limit is not a positive integer", result["detail"])

    def test_runtime_agent_mutation_guard_requires_runtime_actions_inside_service_allowlist(self) -> None:
        result = selftest.runtime_agent_mutation_guard_check(
            {
                "auth_configured": True,
                "allow_missing_auth": False,
                "mtls_enabled": True,
                "client_cert_required": True,
                "mutation_rate_limit_per_minute": 12,
                "allowed_services": ["localai"],
                "runtime_action_services": ["comfyui"],
            }
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("runtime-action allowlist contains services outside the service allowlist", result["detail"])

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
        self.assertEqual(
            selftest.gateway_security_header_failures(
                {
                    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=()",
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

        capture_failures = selftest.gateway_security_header_failures(
            {
                "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=*, microphone=(self), geolocation=()",
            }
        )

        self.assertIn("permissions-policy has unsafe camera directive", capture_failures)

        duplicate_capture_failures = selftest.gateway_security_header_failures(
            {
                "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), camera=*, microphone=(self), geolocation=()",
            }
        )

        self.assertIn("permissions-policy has unsafe camera directive", duplicate_capture_failures)

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
