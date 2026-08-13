from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from starlette.datastructures import FormData, Headers, UploadFile


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

    def __init__(self, payload: dict[str, Any] | None = None, *, body: bytes | None = None, headers: dict[str, str] | None = None) -> None:
        self._body = body if body is not None else json.dumps(payload or {}).encode("utf-8")
        self.headers = headers or {"content-type": "application/json"}

    async def body(self) -> bytes:
        return self._body

    async def stream(self):
        yield self._body


class FakeMultipartRequest(FakeRequest):
    def __init__(self, form: FormData) -> None:
        super().__init__(body=b"", headers={"content-type": "multipart/form-data; boundary=b1"})
        self._form = form

    async def form(self) -> FormData:
        return self._form


WAV_BYTES = (
    b"RIFF"
    + (38).to_bytes(4, "little")
    + b"WAVE"
    + b"fmt "
    + (16).to_bytes(4, "little")
    + (1).to_bytes(2, "little")
    + (1).to_bytes(2, "little")
    + (16000).to_bytes(4, "little")
    + (32000).to_bytes(4, "little")
    + (2).to_bytes(2, "little")
    + (16).to_bytes(2, "little")
    + b"data"
    + (2).to_bytes(4, "little")
    + b"\x00\x00"
)


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
            cpu_resident_allowed=runtime == "audio-cpu",
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
                        "b1_cpu_residency_allowed": True,
                        "b1_model_alias": "tts-fast",
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
                cpu_resident_allowed=True,
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
                        "b1_cpu_residency_allowed": True,
                        "b1_model_alias": "stt-default",
                        "b1_resolved_model_version": "b1-vosk-small-en-us-0.15@0.15",
                        "model": "b1-vosk-small-en-us-0.15",
                    },
                    "timeout_seconds": 120.0,
                    "extra_headers": {"content-type": "application/json"},
                }
            ],
        )

    def test_raw_audio_transcription_sniffs_upload_mime_type(self) -> None:
        request = FakeRequest(
            body=WAV_BYTES,
            headers={"content-type": "application/octet-stream", "X-B1-Filename": "../sample.wav"},
        )

        payload = asyncio.run(main.transcription_input_from_request(request))

        self.assertEqual(payload["audio_mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(payload["audio"].encode("ascii")), WAV_BYTES)
        self.assertEqual(payload["filename"], "../sample.wav")

    def test_multipart_audio_transcription_preserves_named_wav_part(self) -> None:
        upload = UploadFile(
            filename="dialogue.wav",
            file=io.BytesIO(WAV_BYTES),
            headers=Headers({"content-type": "audio/wav"}),
        )
        request = FakeMultipartRequest(
            FormData(
                [
                    ("file", upload),
                    ("model", "stt-default"),
                    ("language", "de"),
                    ("response_format", "verbose_json"),
                ]
            )
        )

        payload = asyncio.run(main.transcription_input_from_request(request))

        self.assertEqual(payload["audio_mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(payload["audio"].encode("ascii")), WAV_BYTES)
        self.assertEqual(payload["filename"], "dialogue.wav")
        self.assertEqual(payload["model"], "stt-default")
        self.assertEqual(payload["language"], "de")
        self.assertEqual(payload["response_format"], "verbose_json")

    def test_raw_audio_transcription_accepts_valid_riff_with_trailing_bytes(self) -> None:
        trailing_wav = WAV_BYTES + b"B1-CAPTURE-METADATA"
        request = FakeRequest(body=trailing_wav, headers={"content-type": "application/octet-stream"})

        payload = asyncio.run(main.transcription_input_from_request(request))

        self.assertEqual(payload["audio_mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(payload["audio"].encode("ascii")), trailing_wav)

    def test_raw_audio_transcription_accepts_streaming_wav_with_unknown_riff_size(self) -> None:
        # Streaming TTS can emit a RIFF/WAVE header before its final byte size
        # is known. The bounded received file still contains valid PCM chunks.
        streaming_wav = WAV_BYTES[:4] + b"\xff\xff\xff\xff" + WAV_BYTES[8:40] + b"\xff\xff\xff\xff" + WAV_BYTES[44:]
        request = FakeRequest(body=streaming_wav, headers={"content-type": "audio/wav"})

        payload = asyncio.run(main.transcription_input_from_request(request))

        self.assertEqual(payload["audio_mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(payload["audio"].encode("ascii")), streaming_wav)

    def test_native_voicebox_stream_resolves_managed_profile_to_bridge(self) -> None:
        profile = {
            "id": "vp_managed_voice",
            "runtime": "voicebox",
            "engine": "chatterbox",
            "model_alias": "tts-quality",
            "profile_type": "clone",
            "metadata": {},
            "sample_artifacts": [],
        }
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))

        async def voice_profile_for_inference(payload: dict[str, Any], received_auth: Any) -> dict[str, Any]:
            self.assertEqual(payload, {"voice_profile_id": "vp_managed_voice"})
            self.assertIs(received_auth, auth)
            return profile

        self.patch_attr("voice_profile_for_inference", voice_profile_for_inference)
        upstream_path, upstream_body = asyncio.run(
            main.prepare_native_voicebox_request(
                "generate/stream",
                FakeRequest({"profile_id": "vp_managed_voice", "text": "Hallo", "engine": "chatterbox"}),
                json.dumps({"profile_id": "vp_managed_voice", "text": "Hallo", "engine": "chatterbox"}).encode("utf-8"),
                auth,
            )
        )

        self.assertEqual(upstream_path, "v1/audio/speech")
        forwarded = json.loads(upstream_body)
        self.assertEqual(forwarded["voice"], "vp_managed_voice")
        self.assertEqual(forwarded["b1_voice_profile"]["id"], "vp_managed_voice")
        self.assertNotIn("profile_id", forwarded)
        self.assertEqual(forwarded["text"], "Hallo")

    def test_native_voicebox_stream_preserves_upstream_profile_uuid(self) -> None:
        request = FakeRequest({"profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c", "text": "Hallo"})
        body = asyncio.run(request.body())
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))

        upstream_path, upstream_body = asyncio.run(main.prepare_native_voicebox_request("generate/stream", request, body, auth))

        self.assertEqual(upstream_path, "generate/stream")
        self.assertEqual(json.loads(upstream_body), {"profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c", "text": "Hallo"})
        self.assertTrue(main.is_native_voicebox_speech_request("generate/stream", "POST"))

    def test_raw_audio_transcription_rejects_spoofed_audio_content_type(self) -> None:
        for body in (b"not really audio", b"\x89PNG\r\n\x1a\n"):
            request = FakeRequest(body=body, headers={"content-type": "audio/wav"})

            with self.subTest(body=body):
                with self.assertRaises(main.HTTPException) as raised:
                    asyncio.run(main.transcription_input_from_request(request))

            self.assertEqual(raised.exception.status_code, 415)


if __name__ == "__main__":
    unittest.main()
