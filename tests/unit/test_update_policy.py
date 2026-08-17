from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import update_policy  # noqa: E402


GOOD_DIGEST = "a" * 64


def healthy_self_test() -> dict[str, Any]:
    return {
        "status": "ok",
        "checks": [
            {"name": name, "status": "ok", "detail": "ok"}
            for name in update_policy.UPDATE_HEALTH_REQUIRED_CHECKS
        ],
    }


class UpdatePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patch_resolver(["93.184.216.34"])

    def patch_resolver(self, addresses: list[str], *, raises: OSError | None = None) -> None:
        original = update_policy.resolve_hostname_addresses

        def fake_resolver(hostname: str, port: int | None) -> list[str]:
            self.resolver_calls.append({"hostname": hostname, "port": port})
            if raises is not None:
                raise raises
            return list(addresses)

        self.resolver_calls: list[dict[str, Any]] = []
        update_policy.resolve_hostname_addresses = fake_resolver
        self.addCleanup(lambda: setattr(update_policy, "resolve_hostname_addresses", original))

    def test_preflight_requires_pinned_digest_images(self) -> None:
        plan = update_policy.build_update_preflight(
            "0.2.0",
            [{"service": "control-plane", "image": f"ghcr.io/b1/b1-ai-hub-control-plane:0.2.0@sha256:{GOOD_DIGEST}"}],
            "https://github.com/B1-Mordred/localAIcentre/releases/tag/v0.2.0",
        )

        self.assertEqual(plan["target_version"], "0.2.0")
        self.assertTrue(plan["pinned_images"])
        self.assertTrue(plan["requires_image_stage"])
        self.assertEqual(plan["runtime_agent_image_action"], "pinned_image_pull")
        self.assertEqual(plan["image_refs"][0]["service"], "control-plane")

    def test_preflight_rejects_latest_missing_digest_and_duplicate_services(self) -> None:
        with self.assertRaises(update_policy.UpdatePolicyError):
            update_policy.build_update_preflight("0.2.0", [{"service": "gateway", "image": "caddy:latest"}])
        with self.assertRaises(update_policy.UpdatePolicyError):
            update_policy.build_update_preflight("0.2.0", [{"service": "gateway", "image": "caddy:2.10.2"}])
        with self.assertRaises(update_policy.UpdatePolicyError):
            update_policy.build_update_preflight(
                "0.2.0",
                [
                    {"service": "gateway", "image": f"caddy:2.10.2@sha256:{GOOD_DIGEST}"},
                    {"service": "gateway", "image": f"caddy:2.10.3@sha256:{'b' * 64}"},
                ],
            )

    def test_source_url_must_be_https_without_credentials(self) -> None:
        with self.assertRaises(update_policy.UpdatePolicyError):
            update_policy.build_update_preflight("0.2.0", [{"service": "gateway", "image": f"caddy@sha256:{GOOD_DIGEST}"}], "http://example.org/release")
        with self.assertRaises(update_policy.UpdatePolicyError):
            update_policy.build_update_preflight("0.2.0", [{"service": "gateway", "image": f"caddy@sha256:{GOOD_DIGEST}"}], "https://user:pass@example.org/release")

    def test_source_url_rejects_internal_targets_and_secret_bearing_components(self) -> None:
        image_refs = [{"service": "gateway", "image": f"caddy@sha256:{GOOD_DIGEST}"}]
        unsafe_urls = [
            "https://127.0.0.1/release",
            "https://[::1]/release",
            "https://192.168.2.10/release",
            "https://169.254.169.254/latest/meta-data",
            "https://localhost/release",
            "https://updates.localhost/release",
            "https://example.org/release?token=secret",
            "https://example.org/release#sha256",
            "https://example.org/../release",
            "https://example.org/releases/%2e%2e/admin",
            "https://example.org/releases/safe%2Fadmin",
            "https://example.org/releases/safe%5Cadmin",
            "https://example.org/releases/%00admin",
            "https://example.org/releases/%",
            "https://example.org/releases/%2",
            "https://example.org/releases/%zz",
            "https://example.org/releases/%ffadmin",
            "https://example.org:badport/release",
        ]

        for source_url in unsafe_urls:
            with self.subTest(source_url=source_url):
                with self.assertRaises(update_policy.UpdatePolicyError):
                    update_policy.build_update_preflight("0.2.0", image_refs, source_url)

    def test_source_url_rejects_private_dns_answers_and_dns_failures(self) -> None:
        image_refs = [{"service": "gateway", "image": f"caddy@sha256:{GOOD_DIGEST}"}]
        self.patch_resolver(["93.184.216.34", "10.0.0.5"])

        with self.assertRaisesRegex(update_policy.UpdatePolicyError, "hostname must not resolve"):
            update_policy.build_update_preflight("0.2.0", image_refs, "https://updates.example.org/release")
        self.assertEqual(self.resolver_calls[-1], {"hostname": "updates.example.org", "port": None})

        self.patch_resolver([], raises=OSError("dns unavailable"))
        with self.assertRaisesRegex(update_policy.UpdatePolicyError, "could not be resolved safely"):
            update_policy.build_update_preflight("0.2.0", image_refs, "https://updates.example.org/release")

    def test_update_health_gate_requires_all_ok_checks(self) -> None:
        gate = update_policy.update_health_gate(healthy_self_test())

        self.assertTrue(gate["ready"])
        self.assertEqual(gate["status"], "ready")
        self.assertEqual(gate["missing_checks"], [])
        self.assertEqual(gate["non_ok_checks"], [])

    def test_update_health_gate_blocks_degraded_missing_or_non_ok_proof(self) -> None:
        payload = healthy_self_test()
        payload["status"] = "degraded"
        payload["checks"] = [
            item
            for item in payload["checks"]
            if item["name"] not in {"tls:routing", "inference:tiny"}
        ]
        payload["checks"][0] = {"name": "database", "status": "warning", "detail": "read-only replica"}

        gate = update_policy.update_health_gate(payload)

        self.assertFalse(gate["ready"])
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("self-test status is degraded", gate["blockers"])
        self.assertEqual(gate["missing_checks"], ["tls:routing", "inference:tiny"])
        self.assertEqual(gate["non_ok_checks"][0]["name"], "database")
        self.assertEqual(gate["non_ok_checks"][0]["status"], "warning")


if __name__ == "__main__":
    unittest.main()
