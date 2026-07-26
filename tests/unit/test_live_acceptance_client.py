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
    body = b'{"status":"ok"}'

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body


class FakePayloadResponse(FakeResponse):
    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self.status = status
        import json

        self.body = json.dumps(body).encode("utf-8")


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

    def test_native_comfyui_request_status_sends_idempotency_header(self) -> None:
        module = load_module("tests/compatibility/test_native_comfyui_compatibility.py", "native_comfyui_idempotency_header")
        cls = module.NativeComfyUiCompatibilityTests
        cls.base_url = "https://comfy.ai.b1.germering"
        cls.api_key = "b1k_public.secret"
        cls.host_header = ""
        cls.timeout_seconds = 1
        seen: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float = 0, context: Any | None = None) -> FakeResponse:
            seen["url"] = request.full_url
            seen["headers"] = {key.lower(): value for key, value in request.header_items()}
            seen["body"] = request.data
            return FakeResponse()

        original_urlopen = module.urllib.request.urlopen
        original_ssl_context = cls.ssl_context
        try:
            module.urllib.request.urlopen = fake_urlopen
            cls.ssl_context = classmethod(lambda inner_cls: None)
            body, _headers, status = cls.request_status(
                "POST",
                "/prompt",
                {"prompt": {"1": {"class_type": "CheckpointLoaderSimple"}}},
                extra_headers={"Idempotency-Key": "prompt-1"},
            )
        finally:
            module.urllib.request.urlopen = original_urlopen
            cls.ssl_context = original_ssl_context

        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"status":"ok"}')
        self.assertEqual(seen["url"], "https://comfy.ai.b1.germering/prompt")
        self.assertEqual(seen["headers"]["authorization"], "Bearer b1k_public.secret")
        self.assertEqual(seen["headers"]["idempotency-key"], "prompt-1")
        self.assertIn(b'"prompt"', seen["body"])

    def test_native_comfyui_api_requests_use_api_base_and_host_header(self) -> None:
        module = load_module("tests/compatibility/test_native_comfyui_compatibility.py", "native_comfyui_api_base")
        cls = module.NativeComfyUiCompatibilityTests
        cls.base_url = "https://comfy.ai.b1.germering"
        cls.api_base_url = "https://192.168.2.10"
        cls.api_key = "b1k_public.secret"
        cls.host_header = ""
        cls.api_host_header = "api.ai.b1.germering"
        cls.timeout_seconds = 1
        seen: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float = 0, context: Any | None = None) -> FakePayloadResponse:
            seen["url"] = request.full_url
            seen["headers"] = {key.lower(): value for key, value in request.header_items()}
            return FakePayloadResponse([{"id": "job_1", "native_prompt_id": "prompt_1"}])

        original_urlopen = module.urllib.request.urlopen
        original_ssl_context = cls.ssl_context
        try:
            module.urllib.request.urlopen = fake_urlopen
            cls.ssl_context = classmethod(lambda inner_cls: None)
            payload = cls.request_api_json("GET", "/v1/media/jobs?native_prompt_id=prompt_1")
        finally:
            module.urllib.request.urlopen = original_urlopen
            cls.ssl_context = original_ssl_context

        self.assertEqual(payload, [{"id": "job_1", "native_prompt_id": "prompt_1"}])
        self.assertEqual(seen["url"], "https://192.168.2.10/v1/media/jobs?native_prompt_id=prompt_1")
        self.assertEqual(seen["headers"]["authorization"], "Bearer b1k_public.secret")
        self.assertEqual(seen["headers"]["host"], "api.ai.b1.germering")

    def test_native_comfyui_artifact_url_must_stay_on_api_origin(self) -> None:
        module = load_module("tests/compatibility/test_native_comfyui_compatibility.py", "native_comfyui_artifact_origin")
        cls = module.NativeComfyUiCompatibilityTests
        cls.api_base_url = "https://api.ai.b1.germering"

        with self.assertRaisesRegex(AssertionError, "outside API origin"):
            cls.api_url("https://evil.example/artifacts/comfyui/prompt_1/output.png")

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

    def test_resolve_hosts_env_parses_comma_and_space_separated_entries(self) -> None:
        raw = "api.ai.b1.germering=127.0.0.1, ai.b1.germering=127.0.0.1\nCOMFY.AI.B1.GERMERING.=::1"

        self.assertEqual(
            live_stack.parse_resolve_hosts(raw),
            {
                "api.ai.b1.germering": "127.0.0.1",
                "ai.b1.germering": "127.0.0.1",
                "comfy.ai.b1.germering": "::1",
            },
        )

    def test_resolve_hosts_rejects_schemes_and_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "schemes or paths"):
            live_stack.parse_resolve_hosts("https://api.ai.b1.germering=127.0.0.1")
        with self.assertRaisesRegex(ValueError, "schemes or paths"):
            live_stack.parse_resolve_hosts("api.ai.b1.germering=127.0.0.1/healthz")

    def test_resolve_hosts_redirects_socket_lookup_without_rewriting_url(self) -> None:
        seen: dict[str, Any] = {}
        original_urlopen = live_stack.urlopen
        original_getaddrinfo = live_stack.socket.getaddrinfo

        def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
            seen["getaddrinfo_args"] = args
            return []

        def fake_urlopen(request: Any, timeout: float = 0, context: Any | None = None) -> FakeResponse:
            seen["url"] = request.full_url
            live_stack.socket.getaddrinfo("api.ai.b1.germering", 443)
            return FakeResponse()

        try:
            live_stack.urlopen = fake_urlopen
            live_stack.socket.getaddrinfo = fake_getaddrinfo
            client = live_stack.LiveApiClient(
                "https://api.ai.b1.germering",
                api_key="b1k_public.secret",
                tls_verify=False,
                resolve_hosts={"api.ai.b1.germering": "127.0.0.1"},
            )
            status, _headers, payload = client.json_request("GET", "/healthz")
        finally:
            live_stack.urlopen = original_urlopen
            live_stack.socket.getaddrinfo = original_getaddrinfo

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})
        self.assertEqual(seen["url"], "https://api.ai.b1.germering/healthz")
        self.assertEqual(seen["getaddrinfo_args"][0], "127.0.0.1")

    def test_media_job_link_uses_server_links_and_validates_shape(self) -> None:
        job = {
            "id": "job_1",
            "links": {
                "self": "/v1/media/jobs/job_1",
                "events": "/v1/media/jobs/job_1/events",
                "artifacts": "/v1/media/jobs/job_1/artifacts",
                "cancel": "/v1/media/jobs/job_1",
            },
        }

        live_stack.assert_media_job_links(self, job)
        self.assertEqual(live_stack.media_job_link(job, "events", "/events"), "/v1/media/jobs/job_1/events")
        self.assertEqual(live_stack.media_job_link(job, "artifacts", "/artifacts"), "/v1/media/jobs/job_1/artifacts")

    def test_media_job_link_fallback_percent_encodes_legacy_job_ids(self) -> None:
        job = {"id": "job/one two", "links": {"events": "https://evil.test/v1/media/jobs/job_1/events"}}

        self.assertEqual(live_stack.media_job_link(job, "events", "/events"), "/v1/media/jobs/job%2Fone%20two/events")
        with self.assertRaises(AssertionError):
            live_stack.assert_media_job_links(self, job)

    def test_measured_model_alias_compacts_admin_model_measurements(self) -> None:
        payload = {
            "aliases": [
                {
                    "id": "chat-default",
                    "status": "installed",
                    "modality": "llm",
                    "preferred_runtime": "localai",
                    "runtimes": ["localai"],
                    "resource_label": "expected",
                    "resolved_model": {"id": "b1-chat", "version": "1.0.0", "display_name": "B1 Chat"},
                }
            ],
            "records": [
                {
                    "id": "b1-chat",
                    "version": "1.0.0",
                    "display_name": "B1 Chat",
                    "preferred_runtime": "localai",
                    "resource_label": "expected",
                    "manifest": {
                        "aliases": ["chat-default"],
                        "measurements": {
                            "updated_at": "2026-07-24T12:00:00+00:00",
                            "latest_resource_estimate": {"vram_gib": 6.5, "ram_gib": 8.0, "disk_gib": 4.0},
                            "runs": [
                                {
                                    "id": "modelsmoke-1",
                                    "type": "install-smoke",
                                    "status": "ok",
                                    "runtime": "localai",
                                    "model_alias": "chat-default",
                                    "resolved_model_version": "b1-chat@1.0.0",
                                    "duration_ms": 5000,
                                    "run_time_ms": 2000,
                                    "peak_vram_mib": 6144,
                                    "peak_ram_mib": 8192,
                                    "hook": {
                                        "status": "ok",
                                        "runtime": "localai",
                                        "model_alias": "chat-default",
                                        "resolved_model_version": "b1-chat@1.0.0",
                                        "engine": "localai",
                                        "placeholder": None,
                                        "measurements": {
                                            "peak_vram_mib": 6144,
                                            "peak_ram_mib": 8192,
                                            "unsafe_nested": {"ignored": True},
                                        },
                                        "unsafe_extra": "ignored",
                                    },
                                    "unsafe_extra": "ignored",
                                }
                            ],
                        },
                    },
                }
            ],
        }

        original = live_stack.urlopen
        try:
            live_stack.urlopen = lambda *args, **kwargs: FakePayloadResponse(payload)  # type: ignore[assignment]
            client = live_stack.LiveApiClient("https://api.ai.b1.germering", api_key="b1k_public.secret")
            summary = live_stack.measured_model_alias(client, "chat-default", expected_runtime="localai")
        finally:
            live_stack.urlopen = original

        self.assertTrue(summary["measurement_available"])
        self.assertEqual(summary["resolved_model_version"], "b1-chat@1.0.0")
        self.assertEqual(summary["latest_ok_run"]["peak_vram_mib"], 6144)
        self.assertEqual(summary["latest_ok_run"]["hook"]["status"], "ok")
        self.assertEqual(summary["latest_ok_run"]["hook"]["engine"], "localai")
        self.assertNotIn("unsafe_extra", summary["latest_ok_run"]["hook"])
        self.assertNotIn("unsafe_nested", summary["latest_ok_run"]["hook"]["measurements"])
        self.assertNotIn("unsafe_extra", summary["latest_ok_run"])

    def test_measured_model_alias_requires_successful_smoke_measurement(self) -> None:
        payload = {
            "aliases": [
                {
                    "id": "chat-default",
                    "status": "installed",
                    "preferred_runtime": "localai",
                    "resolved_model": {"id": "b1-chat", "version": "1.0.0"},
                }
            ],
            "records": [{"id": "b1-chat", "version": "1.0.0", "manifest": {"aliases": ["chat-default"], "measurements": {"runs": []}}}],
        }

        original = live_stack.urlopen
        try:
            live_stack.urlopen = lambda *args, **kwargs: FakePayloadResponse(payload)  # type: ignore[assignment]
            client = live_stack.LiveApiClient("https://api.ai.b1.germering", api_key="b1k_public.secret")
            with self.assertRaisesRegex(AssertionError, "no persisted ok model smoke measurement"):
                live_stack.measured_model_alias(client, "chat-default")
        finally:
            live_stack.urlopen = original

    def test_measured_model_alias_requires_runtime_hook_proof(self) -> None:
        payload = {
            "aliases": [
                {
                    "id": "chat-default",
                    "status": "installed",
                    "preferred_runtime": "localai",
                    "resolved_model": {"id": "b1-chat", "version": "1.0.0"},
                }
            ],
            "records": [
                {
                    "id": "b1-chat",
                    "version": "1.0.0",
                    "preferred_runtime": "localai",
                    "manifest": {
                        "aliases": ["chat-default"],
                        "measurements": {
                            "runs": [
                                {
                                    "id": "modelsmoke-1",
                                    "type": "install-smoke",
                                    "status": "ok",
                                    "runtime": "localai",
                                    "model_alias": "chat-default",
                                    "resolved_model_version": "b1-chat@1.0.0",
                                    "duration_ms": 5000,
                                    "run_time_ms": 2000,
                                    "peak_vram_mib": 6144,
                                    "peak_ram_mib": 8192,
                                }
                            ]
                        },
                    },
                }
            ],
        }

        original = live_stack.urlopen
        try:
            live_stack.urlopen = lambda *args, **kwargs: FakePayloadResponse(payload)  # type: ignore[assignment]
            client = live_stack.LiveApiClient("https://api.ai.b1.germering", api_key="b1k_public.secret")
            with self.assertRaisesRegex(AssertionError, "runtime hook proof is missing"):
                live_stack.measured_model_alias(client, "chat-default", expected_runtime="localai")
        finally:
            live_stack.urlopen = original

    def test_measured_model_alias_rejects_cpu_placeholder_hook_proof(self) -> None:
        payload = {
            "aliases": [
                {
                    "id": "tts-fast",
                    "status": "installed",
                    "preferred_runtime": "audio-cpu",
                    "runtimes": ["audio-cpu"],
                    "resolved_model": {"id": "b1-tts-fast", "version": "1.0.0"},
                }
            ],
            "records": [
                {
                    "id": "b1-tts-fast",
                    "version": "1.0.0",
                    "preferred_runtime": "audio-cpu",
                    "manifest": {
                        "aliases": ["tts-fast"],
                        "measurements": {
                            "runs": [
                                {
                                    "id": "modelsmoke-tts",
                                    "type": "install-smoke",
                                    "status": "ok",
                                    "runtime": "audio-cpu",
                                    "model_alias": "tts-fast",
                                    "resolved_model_version": "b1-tts-fast@1.0.0",
                                    "duration_ms": 500,
                                    "run_time_ms": 100,
                                    "peak_vram_mib": 0,
                                    "peak_ram_mib": 256,
                                    "hook": {
                                        "status": "ok",
                                        "runtime": "audio-cpu",
                                        "model_alias": "tts-fast",
                                        "resolved_model_version": "b1-tts-fast@1.0.0",
                                        "engine": "scaffold",
                                        "placeholder": True,
                                        "measurements": {"peak_vram_mib": 0, "peak_ram_mib": 256, "placeholder": True},
                                    },
                                }
                            ]
                        },
                    },
                }
            ],
        }

        original = live_stack.urlopen
        try:
            live_stack.urlopen = lambda *args, **kwargs: FakePayloadResponse(payload)  # type: ignore[assignment]
            client = live_stack.LiveApiClient("https://api.ai.b1.germering", api_key="b1k_public.secret")
            with self.assertRaisesRegex(AssertionError, "reported placeholder output"):
                live_stack.measured_model_alias(client, "tts-fast", expected_runtime="audio-cpu")
        finally:
            live_stack.urlopen = original


if __name__ == "__main__":
    unittest.main()
