from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.runtime_agent_http import runtime_agent_httpx_kwargs  # noqa: E402


class RuntimeAgentHttpTests(unittest.TestCase):
    def test_plain_http_uses_no_tls_kwargs(self) -> None:
        self.assertEqual(runtime_agent_httpx_kwargs("http://runtime-agent:8000", ca_file="/ca.crt"), {})

    def test_https_uses_ca_and_client_certificate_pair(self) -> None:
        context = Mock()
        with patch("app.runtime_agent_http.ssl.create_default_context", return_value=context) as create_context:
            kwargs = runtime_agent_httpx_kwargs(
                "https://runtime-agent:8443",
                ca_file="/run/secrets/ca.crt",
                client_cert_file="/run/secrets/client.crt",
                client_key_file="/run/secrets/client.key",
                verify_tls=True,
            )

        create_context.assert_called_once_with(cafile="/run/secrets/ca.crt")
        context.load_cert_chain.assert_called_once_with("/run/secrets/client.crt", "/run/secrets/client.key")
        self.assertIs(kwargs["verify"], context)
        self.assertFalse(kwargs["trust_env"])
        self.assertNotIn("cert", kwargs)

    def test_https_can_disable_server_verification_for_development(self) -> None:
        context = Mock()
        with patch("app.runtime_agent_http.ssl._create_unverified_context", return_value=context) as create_context:
            kwargs = runtime_agent_httpx_kwargs("https://runtime-agent:8443", verify_tls=False)

        create_context.assert_called_once_with()
        self.assertIs(kwargs["verify"], context)
        self.assertFalse(kwargs["trust_env"])
        self.assertNotIn("cert", kwargs)


if __name__ == "__main__":
    unittest.main()
