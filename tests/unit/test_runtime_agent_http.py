from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.runtime_agent_http import runtime_agent_httpx_kwargs  # noqa: E402


class RuntimeAgentHttpTests(unittest.TestCase):
    def test_plain_http_uses_no_tls_kwargs(self) -> None:
        self.assertEqual(runtime_agent_httpx_kwargs("http://runtime-agent:8000", ca_file="/ca.crt"), {})

    def test_https_uses_ca_and_client_certificate_pair(self) -> None:
        kwargs = runtime_agent_httpx_kwargs(
            "https://runtime-agent:8443",
            ca_file="/run/secrets/ca.crt",
            client_cert_file="/run/secrets/client.crt",
            client_key_file="/run/secrets/client.key",
            verify_tls=True,
        )

        self.assertEqual(kwargs["verify"], "/run/secrets/ca.crt")
        self.assertEqual(kwargs["cert"], ("/run/secrets/client.crt", "/run/secrets/client.key"))

    def test_https_can_disable_server_verification_for_development(self) -> None:
        kwargs = runtime_agent_httpx_kwargs("https://runtime-agent:8443", verify_tls=False)

        self.assertFalse(kwargs["verify"])
        self.assertNotIn("cert", kwargs)


if __name__ == "__main__":
    unittest.main()
