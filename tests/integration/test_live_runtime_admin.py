from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

from test_live_stack import LiveApiClient  # noqa: E402


@unittest.skipUnless(os.getenv("B1_INTEGRATION_LIVE_TEST") == "1", "set B1_INTEGRATION_LIVE_TEST=1 to run live integration tests")
class LiveRuntimeAdminIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        api_key = os.getenv("B1_INTEGRATION_API_KEY") or os.getenv("B1_SMOKE_ADMIN_API_KEY") or os.getenv("B1_AI_HUB_API_KEY") or ""
        if not api_key:
            raise unittest.SkipTest("set B1_INTEGRATION_API_KEY or B1_SMOKE_ADMIN_API_KEY for live admin integration tests")
        tls_verify = os.getenv("B1_INTEGRATION_TLS_VERIFY", os.getenv("B1_SMOKE_TLS_VERIFY", "1")).strip().lower() not in {"0", "false", "no"}
        cls.client = LiveApiClient(
            os.getenv("B1_INTEGRATION_API_BASE") or os.getenv("B1_SMOKE_API_BASE") or os.getenv("B1_AI_HUB_API_BASE") or "https://api.ai.b1.germering",
            api_key=api_key,
            host_header=os.getenv("B1_INTEGRATION_HOST_HEADER") or os.getenv("B1_SMOKE_HOST_HEADER", ""),
            timeout_seconds=float(os.getenv("B1_INTEGRATION_HTTP_TIMEOUT_SECONDS", os.getenv("B1_SMOKE_HTTP_TIMEOUT_SECONDS", "10"))),
            tls_verify=tls_verify,
            ca_file=os.getenv("B1_INTEGRATION_CA_FILE") or os.getenv("B1_SMOKE_CA_FILE", ""),
        )

    def test_admin_runtime_contract_and_readiness_shape(self) -> None:
        status, _, payload = self.client.json_request("GET", "/admin/runtimes", require_auth=True)
        if status == 403:
            self.skipTest("provided integration key lacks runtimes:read/admin runtime access")
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)
        self.assertIn(payload.get("runtime_deployment_mode"), {"development", "production"})
        self.assertIsInstance(payload.get("readiness"), dict)
        self.assertIsInstance(payload.get("adapters"), list)
        self.assertIsInstance(payload.get("health"), list)

        adapter_names = {adapter.get("name") for adapter in payload["adapters"] if isinstance(adapter, dict)}
        for required in {"localai", "comfyui", "voicebox", "audio-cpu"}:
            self.assertIn(required, adapter_names)

        health_by_name: dict[str, dict[str, Any]] = {
            item["name"]: item for item in payload["health"] if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        for required in {"localai", "comfyui", "audio-cpu"}:
            self.assertIn(required, health_by_name)
            self.assertIsInstance(health_by_name[required].get("adapter_contract"), dict)


if __name__ == "__main__":
    unittest.main()
