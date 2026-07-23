from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import update_policy  # noqa: E402


GOOD_DIGEST = "a" * 64


class UpdatePolicyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
