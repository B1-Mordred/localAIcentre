from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

import test_live_stack as live_stack  # noqa: E402


class FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"status":"ok"}'


class LiveAcceptanceClientTests(unittest.TestCase):
    def patch_urlopen(self, seen: dict[str, Any]) -> None:
        original = live_stack.urlopen

        def fake_urlopen(request: Any, timeout: float = 0, context: Any | None = None) -> FakeResponse:
            seen["called"] = True
            seen["url"] = request.full_url
            seen["headers"] = {key.lower(): value for key, value in request.header_items()}
            seen["timeout"] = timeout
            seen["context"] = context
            return FakeResponse()

        live_stack.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(live_stack, "urlopen", original))

    def test_plain_http_without_api_key_is_allowed_for_unauthenticated_checks(self) -> None:
        seen: dict[str, Any] = {}
        self.patch_urlopen(seen)
        client = live_stack.LiveApiClient("http://api.ai.b1.germering", api_key="")

        status, _headers, payload = client.json_request("GET", "/healthz")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})
        self.assertEqual(seen["url"], "http://api.ai.b1.germering/healthz")
        self.assertNotIn("authorization", seen["headers"])

    def test_plain_http_with_api_key_is_rejected_before_network(self) -> None:
        seen: dict[str, Any] = {}
        self.patch_urlopen(seen)
        client = live_stack.LiveApiClient("http://api.ai.b1.germering", api_key="b1k_public.secret")

        with self.assertRaisesRegex(RuntimeError, "plain HTTP"):
            client.json_request("GET", "/v1/models", require_auth=True)

        self.assertNotIn("called", seen)

    def test_plain_http_with_api_key_requires_explicit_development_opt_in(self) -> None:
        seen: dict[str, Any] = {}
        self.patch_urlopen(seen)
        with patch.dict(os.environ, {live_stack.ALLOW_INSECURE_HTTP_ENV: "true"}, clear=False):
            client = live_stack.LiveApiClient("http://127.0.0.1:8080", api_key="b1k_public.secret")
            status, _headers, payload = client.json_request("GET", "/v1/models", require_auth=True)

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})
        self.assertEqual(seen["url"], "http://127.0.0.1:8080/v1/models")
        self.assertEqual(seen["headers"]["authorization"], "Bearer b1k_public.secret")

    def test_constructor_can_disable_environment_http_opt_in(self) -> None:
        seen: dict[str, Any] = {}
        self.patch_urlopen(seen)
        with patch.dict(os.environ, {live_stack.ALLOW_INSECURE_HTTP_ENV: "true"}, clear=False):
            client = live_stack.LiveApiClient(
                "http://127.0.0.1:8080",
                api_key="b1k_public.secret",
                allow_insecure_http=False,
            )
            with self.assertRaisesRegex(RuntimeError, "plain HTTP"):
                client.json_request("GET", "/v1/models", require_auth=True)

        self.assertNotIn("called", seen)


if __name__ == "__main__":
    unittest.main()
