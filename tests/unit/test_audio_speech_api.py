from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


class FakeUrl:
    query = ""


class FakeRequest:
    method = "POST"
    url = FakeUrl()
    headers = {"content-type": "application/json"}

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    async def body(self) -> bytes:
        return self._body


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class AudioSpeechApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_settings(self, **changes: Any) -> None:
        original = main.settings
        main.settings = replace(main.settings, **changes)
        self.addCleanup(lambda: setattr(main, "settings", original))

    def resolution(self, runtime: str, model_id: str, requires_gpu: bool) -> Any:
        return main.RuntimeResolution(
            public_alias="tts-quality" if runtime == "voicebox" else "tts-fast",
            model_id=model_id,
            model_version="1.0.0",
            resolved_model_version=f"{model_id}@1.0.0",
            runtime=runtime,
            preferred_runtime=runtime,
            requires_gpu=requires_gpu,
            resource_label="expected",
            runtime_policy="any",
        )

    def patch_auth(self) -> None:
        async def authenticate(_: str | None = None) -> Any:
            return AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))

        self.patch_attr("authenticate", authenticate)

    def test_audio_speech_routes_audio_cpu_without_gpu_lease_and_rewrites_model(self) -> None:
        self.patch_auth()
        self.patch_settings(audio_cpu_url="http://audio-cpu")
        calls: list[dict[str, Any]] = []

        def resolve_catalog_alias(model: str, modality: str, runtime_policy: str = "any", operation: str | None = None) -> Any:
            self.assertEqual((model, modality, runtime_policy, operation), ("tts-fast", "tts", "any", "text-to-speech"))
            return self.resolution("audio-cpu", "b1-cpu-placeholder-tts", requires_gpu=False)

        async def proxy_http_bytes(base_url: str, path: str, request: Any, body: bytes | None = None, timeout_seconds: float = 120.0) -> Any:
            calls.append({"base_url": base_url, "path": path, "payload": json.loads(body or b"{}"), "timeout_seconds": timeout_seconds})
            return main.Response(content=b"cpu-wav", media_type="audio/wav")

        async def acquire_inference_lease(resolution: Any, operation: str, owner_id: str | None = None) -> str | None:
            raise AssertionError("CPU speech must not acquire the GPU lease")

        self.patch_attr("resolve_catalog_alias", resolve_catalog_alias)
        self.patch_attr("proxy_http_bytes", proxy_http_bytes)
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)

        response = asyncio.run(main.audio_speech(FakeRequest({"model": "tts-fast", "input": "hello"}), authorization="Bearer key"))

        self.assertEqual(response.body, b"cpu-wav")
        self.assertEqual(
            calls,
            [
                {
                    "base_url": "http://audio-cpu",
                    "path": "/v1/audio/speech",
                    "payload": {
                        "b1_resolved_model_version": "b1-cpu-placeholder-tts@1.0.0",
                        "input": "hello",
                        "model": "b1-cpu-placeholder-tts",
                    },
                    "timeout_seconds": 120.0,
                }
            ],
        )

    def test_proxy_preserves_b1_placeholder_headers(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeProxiedResponse:
            content = b"cpu-wav"
            status_code = 200
            headers = {
                "content-type": "audio/wav",
                "x-b1-placeholder": "true",
                "x-b1-cpu-audio-engine": "scaffold",
                "x-b1-gpu-lease-required": "false",
                "authorization": "must-not-leak",
            }

        class FakeAsyncClient:
            def __init__(self, timeout: float) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def request(self, method: str, url: str, content: bytes, headers: dict[str, str]) -> FakeProxiedResponse:
                calls.append({"method": method, "url": url, "content": content, "headers": headers, "timeout": self.timeout})
                return FakeProxiedResponse()

        original = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original))

        request = FakeRequest({"input": "hello"})
        request.headers = {
            "content-type": "application/json",
            "cookie": "b1_session=browser-secret",
            "x-b1-csrf": "csrf-secret",
            "x-forwarded-for": "203.0.113.8",
        }

        response = asyncio.run(main.proxy_http_bytes("http://audio-cpu", "/v1/audio/speech", request))

        self.assertEqual(response.body, b"cpu-wav")
        self.assertEqual(response.headers["x-b1-placeholder"], "true")
        self.assertEqual(response.headers["x-b1-cpu-audio-engine"], "scaffold")
        self.assertEqual(response.headers["x-b1-gpu-lease-required"], "false")
        self.assertNotIn("authorization", response.headers)
        self.assertEqual(calls[0]["url"], "http://audio-cpu/v1/audio/speech")
        self.assertEqual(calls[0]["headers"]["content-type"], "application/json")
        self.assertNotIn("cookie", calls[0]["headers"])
        self.assertNotIn("x-b1-csrf", calls[0]["headers"])
        self.assertNotIn("x-forwarded-for", calls[0]["headers"])

    def test_audio_speech_routes_voicebox_under_gpu_lease_and_rewrites_model(self) -> None:
        self.patch_auth()
        self.patch_settings(voicebox_url="http://voicebox")
        calls: list[dict[str, Any]] = []

        def resolve_catalog_alias(model: str, modality: str, runtime_policy: str = "any", operation: str | None = None) -> Any:
            self.assertEqual((model, modality, runtime_policy, operation), ("tts-quality", "tts", "any", "text-to-speech"))
            return self.resolution("voicebox", "voicebox-quality", requires_gpu=True)

        async def proxy_http_bytes(base_url: str, path: str, request: Any, body: bytes | None = None, timeout_seconds: float = 120.0) -> Any:
            calls.append({"base_url": base_url, "path": path, "payload": json.loads(body or b"{}"), "timeout_seconds": timeout_seconds})
            return main.Response(content=b"voicebox-wav", media_type="audio/wav")

        async def acquire_inference_lease(resolution: Any, operation: str, owner_id: str | None = None) -> str | None:
            calls.append({"lease": "acquire", "runtime": resolution.runtime, "operation": operation, "owner_id": owner_id})
            return "lease_voicebox"

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"lease": "release", "owner": owner})

        async def prepare_sync_gpu_runtime(resolution: Any, operation: str) -> None:
            calls.append({"prepare": "runtime", "runtime": resolution.runtime, "operation": operation})

        self.patch_attr("resolve_catalog_alias", resolve_catalog_alias)
        self.patch_attr("proxy_http_bytes", proxy_http_bytes)
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("release_inference_lease", release_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)

        response = asyncio.run(main.audio_speech(FakeRequest({"model": "tts-quality", "input": "hello", "runtime_policy": "any"}), authorization="Bearer key"))

        self.assertEqual(response.body, b"voicebox-wav")
        self.assertEqual(
            calls,
            [
                {"lease": "acquire", "runtime": "voicebox", "operation": "audio-speech", "owner_id": "client_1"},
                {"prepare": "runtime", "runtime": "voicebox", "operation": "audio-speech"},
                {
                    "base_url": "http://voicebox",
                    "path": "/v1/audio/speech",
                    "payload": {
                        "b1_resolved_model_version": "voicebox-quality@1.0.0",
                        "input": "hello",
                        "model": "voicebox-quality",
                    },
                    "timeout_seconds": 1800.0,
                },
                {"lease": "release", "owner": "lease_voicebox"},
            ],
        )

    def test_audio_speech_resolves_b1_voice_profile_for_voicebox_payload(self) -> None:
        self.patch_auth()
        self.patch_settings(voicebox_url="http://voicebox")
        calls: list[dict[str, Any]] = []

        class FakeDatabase:
            async def get_voice_profile(self, profile_id: str) -> dict[str, Any] | None:
                self.__class__.lookups.append(profile_id)
                return {
                    "id": "vp_narrator",
                    "owner_id": "client_1",
                    "runtime": "voicebox",
                    "engine": "chatterbox",
                    "model_alias": "tts-quality",
                    "profile_type": "clone",
                    "status": "active",
                    "visibility_roles": ["user"],
                    "metadata": {"upstream_voice": "native-narrator", "notes": "not forwarded"},
                    "sample_artifacts": [
                        {"url": "/artifacts/voicebox/references/narrator.wav", "sha256": "a" * 64, "mime_type": "audio/wav", "bytes": 123}
                    ],
                }

        FakeDatabase.lookups = []  # type: ignore[attr-defined]

        def resolve_catalog_alias(model: str, modality: str, runtime_policy: str = "any", operation: str | None = None) -> Any:
            self.assertEqual((model, modality, runtime_policy, operation), ("tts-quality", "tts", "any", "text-to-speech"))
            return self.resolution("voicebox", "voicebox-quality", requires_gpu=True)

        async def proxy_http_bytes(base_url: str, path: str, request: Any, body: bytes | None = None, timeout_seconds: float = 120.0) -> Any:
            calls.append({"base_url": base_url, "path": path, "payload": json.loads(body or b"{}"), "timeout_seconds": timeout_seconds})
            return main.Response(content=b"voicebox-wav", media_type="audio/wav")

        async def acquire_inference_lease(resolution: Any, operation: str, owner_id: str | None = None) -> str | None:
            calls.append({"lease": "acquire", "runtime": resolution.runtime, "operation": operation, "owner_id": owner_id})
            return "lease_voicebox"

        async def release_inference_lease(owner: str | None) -> None:
            calls.append({"lease": "release", "owner": owner})

        async def prepare_sync_gpu_runtime(resolution: Any, operation: str) -> None:
            calls.append({"prepare": "runtime", "runtime": resolution.runtime, "operation": operation})

        self.patch_attr("database", FakeDatabase())
        self.patch_attr("resolve_catalog_alias", resolve_catalog_alias)
        self.patch_attr("proxy_http_bytes", proxy_http_bytes)
        self.patch_attr("acquire_inference_lease", acquire_inference_lease)
        self.patch_attr("release_inference_lease", release_inference_lease)
        self.patch_attr("prepare_sync_gpu_runtime", prepare_sync_gpu_runtime)

        response = asyncio.run(
            main.audio_speech(
                FakeRequest(
                    {
                        "voice": "vp_narrator",
                        "input": "hello",
                        "b1_voice_profile": {"id": "client-spoof"},
                        "b1_resolved_model_version": "client-spoof@9.9.9",
                    }
                ),
                authorization="Bearer key",
            )
        )

        self.assertEqual(response.body, b"voicebox-wav")
        self.assertEqual(FakeDatabase.lookups, ["vp_narrator"])  # type: ignore[attr-defined]
        forwarded = calls[2]["payload"]
        self.assertEqual(forwarded["model"], "voicebox-quality")
        self.assertEqual(forwarded["voice"], "native-narrator")
        self.assertEqual(forwarded["b1_resolved_model_version"], "voicebox-quality@1.0.0")
        self.assertEqual(forwarded["b1_voice_profile"]["id"], "vp_narrator")
        self.assertEqual(forwarded["b1_voice_profile"]["engine"], "chatterbox")
        self.assertEqual(forwarded["b1_voice_profile"]["profile_type"], "clone")
        self.assertEqual(forwarded["b1_voice_profile"]["upstream"], {"upstream_voice": "native-narrator"})
        self.assertEqual(forwarded["b1_voice_profile"]["sample_artifacts"][0]["url"], "/artifacts/voicebox/references/narrator.wav")
        self.assertNotIn("notes", json.dumps(forwarded, sort_keys=True))
        self.assertNotIn("client-spoof", json.dumps(forwarded, sort_keys=True))

    def test_audio_speech_does_not_forward_b1_internal_metadata_to_external_runtime(self) -> None:
        self.patch_auth()
        calls: list[dict[str, Any]] = []
        external_adapter = main.RuntimeAdapter(
            name="openai-compatible",
            base_url="https://api.example.com/v1",
            modalities=("tts",),
            operations=("text-to-speech",),
            requires_gpu=False,
            external=True,
            openai_compatible=True,
            api_key="provider-secret",
        )

        class FakeRegistry:
            def adapter(self, runtime: str) -> Any:
                self.__class__.calls.append(runtime)
                return external_adapter

        FakeRegistry.calls = []  # type: ignore[attr-defined]

        def resolve_catalog_alias(model: str, modality: str, runtime_policy: str = "any", operation: str | None = None) -> Any:
            self.assertEqual((model, modality, runtime_policy, operation), ("tts-external", "tts", "any", "text-to-speech"))
            return main.RuntimeResolution(
                public_alias="tts-external",
                model_id="external-voice",
                model_version="1.0.0",
                resolved_model_version="external-voice@1.0.0",
                runtime="openai-compatible",
                preferred_runtime="openai-compatible",
                requires_gpu=False,
                resource_label="external",
                runtime_policy=runtime_policy,
            )

        async def proxy_http_bytes_to_url(
            url: str,
            request: Any,
            body: bytes | None = None,
            timeout_seconds: float = 120.0,
            extra_headers: dict[str, str] | None = None,
        ) -> Any:
            calls.append({"url": url, "payload": json.loads(body or b"{}"), "timeout_seconds": timeout_seconds, "extra_headers": extra_headers})
            return main.Response(content=b"external-wav", media_type="audio/wav")

        self.patch_attr("resolve_catalog_alias", resolve_catalog_alias)
        self.patch_attr("runtime_registry_snapshot", lambda: FakeRegistry())
        self.patch_attr("proxy_http_bytes_to_url", proxy_http_bytes_to_url)

        response = asyncio.run(
            main.audio_speech(
                FakeRequest(
                    {
                        "model": "tts-external",
                        "input": "hello",
                        "runtime_policy": "any",
                        "b1_resolved_model_version": "client-spoof@9.9.9",
                        "b1_internal_note": "must-not-forward",
                    }
                ),
                authorization="Bearer key",
            )
        )

        self.assertEqual(response.body, b"external-wav")
        self.assertEqual(FakeRegistry.calls, ["openai-compatible"])  # type: ignore[attr-defined]
        self.assertEqual(
            calls,
            [
                {
                    "url": "https://api.example.com/v1/audio/speech",
                    "payload": {
                        "input": "hello",
                        "model": "external-voice",
                    },
                    "timeout_seconds": 1800.0,
                    "extra_headers": {"Authorization": "Bearer provider-secret"},
                }
            ],
        )

    def test_audio_transcription_routes_audio_cpu_and_rewrites_model(self) -> None:
        self.patch_auth()
        self.patch_settings(audio_cpu_url="http://audio-cpu")
        calls: list[dict[str, Any]] = []

        def resolve_catalog_alias(model: str, modality: str, runtime_policy: str = "any", operation: str | None = None) -> Any:
            self.assertEqual((model, modality, runtime_policy, operation), ("stt-default", "stt", "any", "transcription"))
            return main.RuntimeResolution(
                public_alias="stt-default",
                model_id="b1-vosk-small-en-us-0.15",
                model_version="0.15",
                resolved_model_version="b1-vosk-small-en-us-0.15@0.15",
                runtime="audio-cpu",
                preferred_runtime="audio-cpu",
                requires_gpu=False,
                resource_label="expected",
                runtime_policy="any",
            )

        async def proxy_http_bytes(
            base_url: str,
            path: str,
            request: Any,
            body: bytes | None = None,
            timeout_seconds: float = 120.0,
            extra_headers: dict[str, str] | None = None,
        ) -> Any:
            calls.append(
                {
                    "base_url": base_url,
                    "path": path,
                    "payload": json.loads(body or b"{}"),
                    "timeout_seconds": timeout_seconds,
                    "extra_headers": extra_headers,
                }
            )
            return main.Response(content=b'{"text":"hello"}', media_type="application/json")

        self.patch_attr("resolve_catalog_alias", resolve_catalog_alias)
        self.patch_attr("proxy_http_bytes", proxy_http_bytes)

        response = asyncio.run(
            main.audio_transcriptions(
                FakeRequest(
                    {
                        "model": "stt-default",
                        "audio": "UklGRg==",
                        "runtime_policy": "any",
                        "b1_resolved_model_version": "client-spoof@9.9.9",
                        "b1_internal_note": "must-not-forward",
                    }
                ),
                authorization="Bearer key",
            )
        )

        self.assertEqual(response.body, b'{"text":"hello"}')
        self.assertEqual(
            calls,
            [
                {
                    "base_url": "http://audio-cpu",
                    "path": "/v1/audio/transcriptions",
                    "payload": {
                        "audio": "UklGRg==",
                        "b1_resolved_model_version": "b1-vosk-small-en-us-0.15@0.15",
                        "model": "b1-vosk-small-en-us-0.15",
                    },
                    "timeout_seconds": 120.0,
                    "extra_headers": {"content-type": "application/json"},
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
