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

    def test_compose_selection_check_passes_when_required_overlays_are_selected(self) -> None:
        result = selftest.compose_selection_check(
            {
                "format": "b1-ai-hub-compose-selection/v1",
                "status": "ok",
                "raw_compose_file": "compose.yaml:compose.production-localai.yaml:compose.production-comfyui.yaml",
                "raw_compose_profiles": "",
                "selected_file_basenames": [
                    "compose.yaml",
                    "compose.production-comfyui.yaml",
                    "compose.production-localai.yaml",
                ],
                "selected_profiles": [],
                "production_required_runtimes": ["localai", "comfyui", "audio-cpu"],
                "required_files": [
                    "compose.yaml",
                    "compose.production-comfyui.yaml",
                    "compose.production-localai.yaml",
                ],
                "required_profiles": [],
                "missing_files": [],
                "missing_profiles": [],
            },
            "production",
        )

        self.assertEqual(result["status"], "ok")
        self.assertIn("Compose files and profiles are selected", result["detail"])

    def test_compose_selection_check_fails_missing_production_overlay(self) -> None:
        result = selftest.compose_selection_check(
            {
                "format": "b1-ai-hub-compose-selection/v1",
                "status": "blocked",
                "raw_compose_file": "compose.yaml:compose.production-localai.yaml",
                "raw_compose_profiles": "",
                "selected_file_basenames": ["compose.yaml", "compose.production-localai.yaml"],
                "selected_profiles": [],
                "production_required_runtimes": ["localai", "comfyui", "voicebox"],
                "required_files": [
                    "compose.yaml",
                    "compose.production-comfyui.yaml",
                    "compose.production-localai.yaml",
                    "compose.production-voicebox.yaml",
                ],
                "required_profiles": ["voicebox"],
                "missing_files": ["compose.production-comfyui.yaml", "compose.production-voicebox.yaml"],
                "missing_profiles": ["voicebox"],
            },
            "production",
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("compose.production-comfyui.yaml", result["detail"])
        self.assertIn("missing Compose profiles: voicebox", result["detail"])
        self.assertEqual(result["data"]["missing_profiles"], ["voicebox"])

    def test_compose_selection_check_warns_for_development_missing_overlay(self) -> None:
        result = selftest.compose_selection_check(
            {
                "format": "b1-ai-hub-compose-selection/v1",
                "status": "blocked",
                "selected_file_basenames": ["compose.yaml"],
                "selected_profiles": [],
                "production_required_runtimes": ["comfyui"],
                "required_files": ["compose.yaml", "compose.production-comfyui.yaml"],
                "required_profiles": [],
                "missing_files": ["compose.production-comfyui.yaml"],
                "missing_profiles": [],
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
        localai = next(item for item in result["data"]["runtime_readiness"] if item["runtime"] == "localai")
        self.assertIn("runtime-agent service inventory unavailable", localai["blockers"])
        self.assertIn("runtime health payload reports a placeholder runtime", localai["placeholder_reasons"])

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
        self.assertFalse(result["data"]["service_inventory_available"])

    def test_runtime_production_readiness_fails_for_placeholder_service_inventory(self) -> None:
        result = selftest.runtime_production_readiness_check(
            [
                {"name": "localai", "status": "ok", "details": {"version": "pinned"}},
                {"name": "comfyui", "status": "ok", "details": {"version": "pinned"}},
            ],
            "production",
            ("localai", "comfyui"),
            {
                "services": [
                    {
                        "name": "localai",
                        "containers": [
                            {
                                "name": "b1-ai-hub-localai-1",
                                "image": "b1-ai-hub/mock-runtime:dev",
                                "state": "running",
                                "labels": {
                                    "b1.ai-hub.runtime": "localai",
                                    "b1.ai-hub.runtime.kind": "placeholder-localai",
                                    "b1.ai-hub.placeholder": "true",
                                },
                            }
                        ],
                    },
                    {
                        "name": "comfyui",
                        "containers": [
                            {
                                "name": "b1-ai-hub-comfyui-1",
                                "image": "b1-ai-hub/comfyui:v0.3.77-b1",
                                "state": "running",
                                "labels": {
                                    "b1.ai-hub.runtime": "comfyui",
                                    "b1.ai-hub.runtime.kind": "comfyui",
                                    "b1.ai-hub.placeholder": "false",
                                },
                            }
                        ],
                    },
                ]
            },
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["data"]["required_placeholders"], ["localai"])
        self.assertEqual(result["data"]["placeholder_runtimes"], ["localai"])
        self.assertIn("container label b1.ai-hub.placeholder=true", result["data"]["service_placeholder_reasons"]["localai"])
        localai = next(item for item in result["data"]["runtime_readiness"] if item["runtime"] == "localai")
        self.assertEqual(localai["container_images"], ["b1-ai-hub/mock-runtime:dev"])
        self.assertIn("runtime is still using placeholder evidence", localai["blockers"])

    def test_runtime_production_readiness_ignores_stopped_placeholder_inventory(self) -> None:
        result = selftest.runtime_production_readiness_check(
            [{"name": "localai", "status": "ok", "details": {"version": "pinned"}}],
            "production",
            ("localai",),
            {
                "services": [
                    {
                        "name": "localai",
                        "containers": [
                            {
                                "name": "b1-ai-hub-localai-old",
                                "image": "b1-ai-hub/mock-runtime:dev",
                                "state": "exited",
                                "labels": {"b1.ai-hub.placeholder": "true"},
                            },
                            {
                                "name": "b1-ai-hub-localai-1",
                                "image": "b1-ai-hub/localai:v4.7.1-b1",
                                "state": "running",
                                "labels": {"b1.ai-hub.placeholder": "false"},
                            },
                        ],
                    }
                ]
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["required_placeholders"], [])
        localai = result["data"]["runtime_readiness"][0]
        self.assertTrue(localai["ready"])
        self.assertEqual(localai["container_names"], ["b1-ai-hub-localai-1"])

    def test_runtime_production_readiness_passes_for_required_real_runtimes(self) -> None:
        result = selftest.runtime_production_readiness_check(
            [
                {"name": "localai", "status": "ok", "details": {"version": "pinned"}},
                {"name": "comfyui", "status": "ok", "details": {"version": "pinned"}},
                {"name": "audio-cpu", "status": "ok", "details": {"engine": "piper"}},
            ],
            "production",
            ("localai", "comfyui", "audio-cpu"),
            {
                "services": [
                    {
                        "name": "localai",
                        "containers": [
                            {
                                "name": "b1-ai-hub-localai-1",
                                "image": "b1-ai-hub/localai:v4.7.1-b1",
                                "state": "running",
                                "labels": {"b1.ai-hub.placeholder": "false"},
                            }
                        ],
                    },
                    {
                        "name": "comfyui",
                        "containers": [
                            {
                                "name": "b1-ai-hub-comfyui-1",
                                "image": "b1-ai-hub/comfyui:v0.3.77-b1",
                                "state": "running",
                                "labels": {"b1.ai-hub.placeholder": "false"},
                            }
                        ],
                    },
                    {
                        "name": "audio-cpu",
                        "containers": [
                            {
                                "name": "b1-ai-hub-audio-cpu-1",
                                "image": "b1-ai-hub/audio-cpu:test",
                                "state": "running",
                                "labels": {"b1.ai-hub.placeholder": "false"},
                            }
                        ],
                    },
                ]
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["placeholder_runtimes"], [])
        self.assertTrue(all(item["ready"] for item in result["data"]["runtime_readiness"] if item["required"]))

    def test_runtime_production_readiness_fails_when_inventory_is_unavailable(self) -> None:
        result = selftest.runtime_production_readiness_check(
            [{"name": "localai", "status": "ok", "details": {"version": "pinned"}}],
            "production",
            ("localai",),
        )

        self.assertEqual(result["status"], "failed")
        localai = result["data"]["runtime_readiness"][0]
        self.assertEqual(localai["runtime"], "localai")
        self.assertIn("runtime-agent service inventory unavailable", localai["blockers"])
        self.assertIn("start runtime-agent with Docker socket access", " ".join(localai["remediation"]))


if __name__ == "__main__":
    unittest.main()
