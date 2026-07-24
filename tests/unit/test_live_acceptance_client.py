from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

import test_live_stack as live_stack  # noqa: E402


def load_module(relative_path: str, module_name: str) -> Any:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

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

    def test_native_comfyui_http_api_key_is_rejected_before_network(self) -> None:
        module = load_module("tests/compatibility/test_native_comfyui_compatibility.py", "native_comfyui_transport_guard")
        cls = module.NativeComfyUiCompatibilityTests
        cls.base_url = "http://comfy.ai.b1.germering"
        cls.api_key = "b1k_public.secret"
        cls.host_header = ""
        cls.timeout_seconds = 1
        called = False

        def fake_urlopen(*args: Any, **kwargs: Any) -> FakeResponse:
            nonlocal called
            called = True
            return FakeResponse()

        original = module.urllib.request.urlopen
        try:
            module.urllib.request.urlopen = fake_urlopen
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(module.ALLOW_INSECURE_HTTP_ENV, None)
                with self.assertRaisesRegex(RuntimeError, "plain HTTP/WebSocket"):
                    cls.request_json("GET", "/object_info")
                with self.assertRaisesRegex(RuntimeError, "plain HTTP/WebSocket"):
                    cls.enforce_token_transport_security("ws://comfy.ai.b1.germering/ws")
        finally:
            module.urllib.request.urlopen = original

        self.assertFalse(called)

    def test_voicebox_http_api_key_is_rejected_before_network(self) -> None:
        module = load_module("tests/compatibility/test_voicebox_remote.py", "voicebox_transport_guard")
        cls = module.VoiceboxRemoteCompatibilityTests
        cls.api_base = "http://api.ai.b1.germering"
        cls.voice_base = "http://voice.ai.b1.germering"
        cls.api_key = "b1k_public.secret"
        cls.native_api_key = "b1k_native.secret"
        cls.api_host_header = ""
        cls.voice_host_header = ""
        cls.timeout_seconds = 1
        called = False

        def fake_urlopen(*args: Any, **kwargs: Any) -> FakeResponse:
            nonlocal called
            called = True
            return FakeResponse()

        original = module.urllib.request.urlopen
        try:
            module.urllib.request.urlopen = fake_urlopen
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(module.ALLOW_INSECURE_HTTP_ENV, None)
                with self.assertRaisesRegex(RuntimeError, "plain HTTP/WebSocket"):
                    cls.request_raw(cls.api_base, "GET", "/admin/voicebox/profiles")
                with self.assertRaisesRegex(RuntimeError, "plain HTTP/WebSocket"):
                    cls.enforce_token_transport_security("ws://voice.ai.b1.germering/ws", token=cls.native_api_key)
        finally:
            module.urllib.request.urlopen = original

        self.assertFalse(called)

    def test_security_acceptance_refuses_http_tokens_cookies_and_passwords(self) -> None:
        module = load_module("tests/security/test_live_security_acceptance.py", "security_transport_guard")
        cls = module.LiveSecurityAcceptanceTests
        cls.api_base = "http://api.ai.b1.germering"
        cls.comfy_base = "http://comfy.ai.b1.germering"
        cls.api_host_header = ""
        cls.comfy_host_header = ""
        cls.timeout_seconds = 1
        called = False

        def fake_urlopen(*args: Any, **kwargs: Any) -> FakeResponse:
            nonlocal called
            called = True
            return FakeResponse()

        original = module.urllib.request.urlopen
        try:
            module.urllib.request.urlopen = fake_urlopen
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(module.ALLOW_INSECURE_HTTP_ENV, None)
                with self.assertRaisesRegex(RuntimeError, "plain HTTP"):
                    cls.request_raw(cls.api_base, "GET", "/admin/self-test", token="b1k_public.secret")
                with self.assertRaisesRegex(RuntimeError, "plain HTTP"):
                    cls.request_raw(cls.api_base, "POST", "/auth/login", body={"username": "admin", "password": "secret"})
                with self.assertRaisesRegex(RuntimeError, "plain HTTP"):
                    cls.request_raw(cls.api_base, "POST", "/admin/network-policy/validate", cookie="b1_ai_hub_session=secret")
        finally:
            module.urllib.request.urlopen = original

        self.assertFalse(called)

    def test_raw_harness_http_api_key_development_opt_in_is_honored(self) -> None:
        module = load_module("tests/compatibility/test_voicebox_remote.py", "voicebox_transport_guard_opt_in")
        cls = module.VoiceboxRemoteCompatibilityTests
        cls.api_base = "http://api.ai.b1.germering"
        cls.voice_base = "http://voice.ai.b1.germering"
        cls.api_key = "b1k_public.secret"
        cls.native_api_key = "b1k_native.secret"
        cls.api_host_header = ""
        cls.voice_host_header = ""
        cls.timeout_seconds = 1
        called = False

        def fake_urlopen(request: Any, timeout: float = 0, context: Any | None = None) -> FakeResponse:
            nonlocal called
            called = True
            self.assertEqual(request.full_url, "http://api.ai.b1.germering/admin/voicebox/profiles")
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer b1k_public.secret")
            return FakeResponse()

        original = module.urllib.request.urlopen
        try:
            module.urllib.request.urlopen = fake_urlopen
            with patch.dict(os.environ, {module.ALLOW_INSECURE_HTTP_ENV: "true"}, clear=False):
                status, _headers, body = cls.request_raw(cls.api_base, "GET", "/admin/voicebox/profiles")
        finally:
            module.urllib.request.urlopen = original

        self.assertTrue(called)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"status":"ok"}')

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
