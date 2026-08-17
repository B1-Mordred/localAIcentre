from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    FASTAPI_AVAILABLE = False
else:
    FASTAPI_AVAILABLE = True


ROOT = Path(__file__).resolve().parents[2]


def tiny_wav(duration_ms: int = 1000, sample_rate: int = 48000) -> bytes:
    frames = int(sample_rate * duration_ms / 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\0\0" * frames)
    return buffer.getvalue()


def streamed_wav_header(content: bytes) -> bytes:
    data = bytearray(content)
    data_offset = data.find(b"data")
    if data_offset >= 0:
        data[data_offset + 4 : data_offset + 8] = b"\xff\xff\xff\xff"
    return bytes(data)


def load_proxy():
    path = ROOT / "deploy" / "voicebox" / "b1_voicebox_proxy.py"
    spec = importlib.util.spec_from_file_location("b1_voicebox_proxy_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Voicebox proxy")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeManager:
    def __init__(self) -> None:
        self.restarts: list[tuple[float, str]] = []

    def restart(self, timeout_seconds: float, reason: str) -> dict[str, object]:
        self.restarts.append((timeout_seconds, reason))
        return {"strategy": "upstream_process_restart", "pid": 123, "reason": reason}

    def status(self) -> dict[str, object]:
        return {"running": True, "pid": 123, "returncode": None}


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        payload: object | None = None,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {"content-type": "application/json"}

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeVoiceboxClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def get(self, url: str) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url})
        if url.endswith("/profiles"):
            return FakeResponse(payload=[])
        if "/profiles/" in url:
            return FakeResponse(status_code=404, payload={"detail": "not found"})
        return FakeResponse()

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        call: dict[str, object] = {"method": "POST", "url": url}
        if "json" in kwargs:
            call["json"] = kwargs["json"]
        if "data" in kwargs:
            call["data"] = kwargs["data"]
        if "files" in kwargs:
            file_tuple = kwargs["files"]["file"]  # type: ignore[index]
            call["file_name"] = file_tuple[0]
            call["file_content"] = file_tuple[1].read()
        self.calls.append(call)
        if url.endswith("/profiles"):
            return FakeResponse(payload={"id": "native-profile-1"})
        if url.endswith("/samples"):
            return FakeResponse(payload={"id": "sample-1"})
        if url.endswith("/generate/stream"):
            return FakeResponse(content=b"RIFFvoicebox", headers={"content-type": "audio/wav"})
        return FakeResponse(status_code=404, payload={"detail": "not found"})


class FailingSpeechVoiceboxClient(FakeVoiceboxClient):
    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, "json": kwargs.get("json")})
        if url.endswith("/generate/stream"):
            return FakeResponse(status_code=404, payload={"detail": "Profile not found"}, content=b"")
        return FakeResponse(status_code=404, payload={"detail": "not found"}, content=b"")


class SlowVoiceboxClient(FakeVoiceboxClient):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().post(url, **kwargs)
        finally:
            self.active -= 1


class FakeCompletedProcess:
    def __init__(self, stdout: bytes = b"RIFF48k", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is required for Voicebox proxy tests")
class VoiceboxProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proxy = load_proxy()

    def test_model_matching_accepts_folder_path_basename_and_stem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engines" / "quality").mkdir(parents=True)
            (root / "engines" / "quality" / "voicebox-quality.safetensors").write_text("model", encoding="utf-8")

            entries = self.proxy.iter_model_entries((root,), max_entries=100)

        self.assertIn("engines/quality/voicebox-quality.safetensors", entries)
        self.assertTrue(self.proxy.model_available(["engines/quality/voicebox-quality.safetensors"], entries))
        self.assertTrue(self.proxy.model_available(["voicebox-quality.safetensors"], entries))
        self.assertTrue(self.proxy.model_available(["voicebox-quality"], entries))
        self.assertFalse(self.proxy.model_available(["other"], entries))

    def test_strict_model_list_missing_model_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"B1_VOICEBOX_HOOK_MODEL_ROOTS": tmp, "B1_VOICEBOX_HOOK_STRICT_MODEL_LIST": "true"},
                clear=False,
            ):
                result = self.proxy.handle_load({"model": "missing-model"})

        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["reason"], "model_not_listed")

    def test_warm_defaults_to_unconfirmed_without_synthesis_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {
                    "B1_VOICEBOX_HOOK_MODEL_ROOTS": tmp,
                    "B1_VOICEBOX_HOOK_WARM_ENABLED": "false",
                    "B1_VOICEBOX_HOOK_STRICT_MODEL_LIST": "false",
                },
                clear=False,
            ):
                result = asyncio.run(self.proxy.handle_warm({"model": "voicebox-quality"}))

        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["reason"], "warm_disabled")

    def test_unload_restarts_upstream_when_idle(self) -> None:
        manager = FakeManager()
        tracker = self.proxy.NativeRequestTracker()

        with patch.dict("os.environ", {"B1_VOICEBOX_HOOK_RESTART_ON_UNLOAD": "true", "B1_VOICEBOX_HOOK_UNLOAD_TIMEOUT_SECONDS": "7"}, clear=False):
            result = self.proxy.handle_unload({}, manager, tracker)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["strategy"], "upstream_process_restart")
        self.assertEqual(result["pid"], 123)
        self.assertEqual(manager.restarts, [(7.0, "b1_runtime_unload")])

    def test_unload_waits_when_native_requests_are_active(self) -> None:
        manager = FakeManager()
        tracker = self.proxy.NativeRequestTracker()
        tracker.begin()

        result = self.proxy.handle_unload({}, manager, tracker)

        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["reason"], "native_requests_active")
        self.assertEqual(result["active_requests"], 1)
        self.assertEqual(manager.restarts, [])

    def test_runtime_control_auth_requires_configured_bearer_token(self) -> None:
        with patch.dict(
            "os.environ",
            {"B1_RUNTIME_CONTROL_TOKEN": "hook-token", "B1_RUNTIME_CONTROL_REQUIRE_AUTH": "true"},
            clear=False,
        ):
            missing = self.proxy.runtime_control_auth_failure({})
            accepted = self.proxy.runtime_control_auth_failure({"authorization": "Bearer hook-token"})

        self.assertEqual(missing[0], 401)
        self.assertEqual(missing[1]["reason"], "runtime_control_token_required")
        self.assertIsNone(accepted)

    def test_build_info_reports_pinned_upstream_and_proxy_identity(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "B1_VOICEBOX_PROXY_VERSION": "b1-voicebox-proxy/v0.5.0-b1",
                "B1_VOICEBOX_UPSTREAM_VERSION": "v0.5.0",
                "B1_VOICEBOX_UPSTREAM_COMMIT": "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d",
                "B1_VOICEBOX_SOURCE_ARCHIVE_SHA256": "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083",
            },
            clear=False,
        ):
            info = self.proxy.voicebox_build_info()

        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["runtime"], "voicebox")
        self.assertEqual(info["action"], "build-info")
        self.assertEqual(info["proxy"], "b1-voicebox-proxy")
        self.assertEqual(info["proxy_version"], "b1-voicebox-proxy/v0.5.0-b1")
        self.assertEqual(info["upstream_repository"], "jamiepine/voicebox")
        self.assertEqual(info["upstream_version"], "v0.5.0")
        self.assertEqual(info["upstream_commit"], "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d")
        self.assertEqual(info["source_archive_sha256"], "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083")
        self.assertIs(info["pinned"], True)
        self.assertIn("status", info["capabilities"]["actions"])
        self.assertIn("unload", info["capabilities"]["actions"])

    def test_default_upstream_command_uses_configured_data_dir(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "B1_VOICEBOX_UPSTREAM_HOST": "127.0.0.1",
                "B1_VOICEBOX_UPSTREAM_PORT": "17494",
                "B1_VOICEBOX_DATA_DIR": "/srv/b1-ai-hub/voicebox",
            },
            clear=False,
        ):
            command = self.proxy.default_upstream_command()

        self.assertEqual(
            command,
            [
                "python",
                "-m",
                "backend.main",
                "--host",
                "127.0.0.1",
                "--port",
                "17494",
                "--data-dir",
                "/srv/b1-ai-hub/voicebox",
            ],
        )

    def test_build_info_fails_closed_when_pinned_commit_is_invalid(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "B1_VOICEBOX_PROXY_VERSION": "b1-voicebox-proxy/v0.5.0-b1",
                "B1_VOICEBOX_UPSTREAM_VERSION": "v0.5.0",
                "B1_VOICEBOX_UPSTREAM_COMMIT": "not-a-commit",
                "B1_VOICEBOX_SOURCE_ARCHIVE_SHA256": "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083",
            },
            clear=False,
        ):
            info = self.proxy.voicebox_build_info()

        self.assertEqual(info["status"], "unconfigured")
        self.assertEqual(info["upstream_commit"], "")

    def test_status_reports_process_capabilities_and_redacted_model_inventory(self) -> None:
        manager = FakeManager()
        tracker = self.proxy.NativeRequestTracker()
        tracker.begin()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "voicebox-secret-model.safetensors").write_text("model", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "B1_VOICEBOX_HOOK_MODEL_ROOTS": str(root),
                    "B1_VOICEBOX_STATUS_MODEL_LIST_MAX_ENTRIES": "100",
                    "B1_VOICEBOX_UPSTREAM_COMMIT": "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d",
                    "B1_VOICEBOX_SOURCE_ARCHIVE_SHA256": "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083",
                },
                clear=False,
            ):
                status = self.proxy.voicebox_status(manager, tracker)

        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["runtime"], "voicebox")
        self.assertEqual(status["action"], "status")
        self.assertEqual(status["process"]["running"], True)
        self.assertEqual(status["active_requests"], 1)
        self.assertEqual(status["model_inventory"]["root_count"], 1)
        self.assertEqual(status["model_inventory"]["available_root_count"], 1)
        self.assertEqual(status["model_inventory"]["entry_count"], 1)
        self.assertIn("build-info", status["capabilities"]["actions"])
        self.assertIn("unload", status["capabilities"]["actions"])
        self.assertEqual(status["build_info"]["status"], "ok")
        self.assertNotIn("voicebox-secret-model", str(status))

    def test_status_reports_unhealthy_when_upstream_process_is_not_running(self) -> None:
        class StoppedManager(FakeManager):
            def status(self) -> dict[str, object]:
                return {"running": False, "pid": None, "returncode": 1}

        status = self.proxy.voicebox_status(StoppedManager(), self.proxy.NativeRequestTracker())

        self.assertEqual(status["status"], "unhealthy")
        self.assertEqual(status["process"]["running"], False)

    def test_speech_profile_envelope_maps_to_upstream_fields_and_local_sample_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "voicebox" / "references" / "narrator.wav"
            sample.parent.mkdir(parents=True)
            sample.write_bytes(b"wav")
            payload = {
                "model": "voicebox-quality",
                "input": "hello",
                "voice": "vp_narrator",
                "b1_resolved_model_version": "voicebox-quality@1.0.0",
                "b1_voice_profile": {
                    "id": "vp_narrator",
                    "runtime": "voicebox",
                    "engine": "chatterbox",
                    "profile_type": "clone",
                    "model_alias": "tts-quality",
                    "upstream": {"upstream_voice": "native-narrator", "language": "en"},
                    "sample_artifacts": [
                        {"url": "/artifacts/voicebox/references/narrator.wav", "sha256": "a" * 64, "mime_type": "audio/wav", "bytes": 3}
                    ],
                },
            }
            with patch.dict("os.environ", {"B1_VOICEBOX_ARTIFACT_ROOT": str(root)}, clear=False):
                transformed = self.proxy.transform_voicebox_speech_body(
                    json.dumps(payload).encode("utf-8"),
                    "application/json",
                )

        body = json.loads(transformed)
        self.assertEqual(body["model"], "voicebox-quality")
        self.assertEqual(body["input"], "hello")
        self.assertEqual(body["voice"], "native-narrator")
        self.assertEqual(body["language"], "en")
        self.assertEqual(body["engine"], "chatterbox")
        self.assertTrue(body["reference_audio_path"].endswith("/voicebox/references/narrator.wav"))
        self.assertNotIn("b1_voice_profile", body)
        self.assertNotIn("b1_resolved_model_version", body)

    def test_speech_profile_sample_path_must_remain_under_voicebox_artifacts(self) -> None:
        payload = {
            "model": "voicebox-quality",
            "input": "hello",
            "b1_voice_profile": {
                "id": "vp_narrator",
                "runtime": "voicebox",
                "engine": "chatterbox",
                "profile_type": "clone",
                "model_alias": "tts-quality",
                "upstream": {},
                "sample_artifacts": [
                    {"url": "/artifacts/voicebox/%2e%2e/private.wav", "sha256": "a" * 64, "mime_type": "audio/wav", "bytes": 3}
                ],
            },
        }

        with self.assertRaises(ValueError):
            self.proxy.transform_voicebox_speech_body(json.dumps(payload).encode("utf-8"), "application/json")

    def test_openai_speech_bridge_creates_native_chatterbox_clone_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "voicebox" / "references" / "narrator.wav"
            sample.parent.mkdir(parents=True)
            sample.write_bytes(b"wav-reference")
            client = FakeVoiceboxClient()
            payload = {
                "input": "hello from b1",
                "language": "en",
                "max_chunk_chars": 1200,
                "b1_voice_profile": {
                    "id": "vp_narrator",
                    "runtime": "voicebox",
                    "engine": "chatterbox",
                    "profile_type": "clone",
                    "model_alias": "tts-quality",
                    "upstream": {"language": "en"},
                    "sample_artifacts": [
                        {
                            "url": "/artifacts/voicebox/references/narrator.wav",
                            "sha256": "a" * 64,
                            "mime_type": "audio/wav",
                            "bytes": 13,
                            "reference_text": "This is the narrator reference.",
                        }
                    ],
                },
            }
            with patch.dict(
                "os.environ",
                {
                    "B1_VOICEBOX_ARTIFACT_ROOT": str(root),
                    "B1_VOICEBOX_DATA_DIR": str(root / "data"),
                    "B1_VOICEBOX_UPSTREAM_URL": "http://voicebox-upstream",
                },
                clear=False,
            ):
                response = asyncio.run(
                    self.proxy.voicebox_openai_speech_bridge(
                        client,
                        json.dumps(payload).encode("utf-8"),
                        "application/json",
                    )
                )

            create_call = next(call for call in client.calls if call["url"] == "http://voicebox-upstream/profiles")
            upload_call = next(call for call in client.calls if str(call["url"]).endswith("/samples"))
            generation_call = next(call for call in client.calls if call["url"] == "http://voicebox-upstream/generate/stream")
            profile_map = json.loads((root / "data" / "b1-profile-map.json").read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"RIFFvoicebox")
        self.assertEqual(create_call["json"]["voice_type"], "cloned")
        self.assertEqual(create_call["json"]["default_engine"], "chatterbox")
        self.assertEqual(upload_call["data"], {"reference_text": "This is the narrator reference."})
        self.assertEqual(upload_call["file_content"], b"wav-reference")
        self.assertEqual(generation_call["json"]["profile_id"], "native-profile-1")
        self.assertEqual(generation_call["json"]["engine"], "chatterbox")
        self.assertEqual(generation_call["json"]["text"], "hello from b1")
        self.assertEqual(generation_call["json"]["max_chunk_chars"], 1200)
        self.assertEqual(profile_map["vp_narrator"]["native_profile_id"], "native-profile-1")
        self.assertEqual(profile_map["vp_narrator"]["sample_sha256s"], ["a" * 64])

    def test_broadcast_audio_policy_uses_normalize_true_and_clamps_peak(self) -> None:
        policy = self.proxy.broadcast_audio_policy(
            {"normalize": True, "b1_true_peak_dbtp": -0.1, "b1_loudness_lufs": -16, "b1_audio_sample_rate": 48000},
            "audio/wav",
        )

        self.assertIsNotNone(policy)
        self.assertEqual(policy["sample_rate"], 48000)
        self.assertEqual(policy["loudness_lufs"], -16.0)
        self.assertEqual(policy["true_peak_dbtp"], -1.5)

    def test_broadcast_audio_policy_preserves_raw_when_normalize_false(self) -> None:
        self.assertIsNone(self.proxy.broadcast_audio_policy({"normalize": False}, "audio/wav"))

    def test_openai_speech_bridge_postprocesses_broadcast_audio(self) -> None:
        client = FakeVoiceboxClient()
        payload = {"model": "voicebox-quality", "input": "hello", "voice": "native-profile-1", "normalize": True}
        with patch.dict("os.environ", {"B1_VOICEBOX_UPSTREAM_URL": "http://voicebox-upstream"}, clear=False), patch.object(
            self.proxy.subprocess,
            "run",
            return_value=FakeCompletedProcess(stdout=b"RIFF48k"),
        ) as run:
            response = asyncio.run(
                self.proxy.voicebox_openai_speech_bridge(
                    client,
                    json.dumps(payload).encode("utf-8"),
                    "application/json",
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"RIFF48k")
        self.assertEqual(response.headers["x-b1-audio-policy"], "broadcast")
        self.assertEqual(response.headers["x-b1-audio-sample-rate"], "48000")
        command = run.call_args.args[0]
        self.assertIn("-ar", command)
        self.assertIn("48000", command)
        self.assertIn("loudnorm=I=-18.0:TP=-1.5", " ".join(command))

    def test_voice_timing_payload_is_bound_to_final_wav(self) -> None:
        audio = tiny_wav(duration_ms=1200)
        payload = {
            "profile_id": "native-profile-1",
            "text": "Guten Morgen",
            "language": "de",
            "engine": "chatterbox",
        }

        metadata = self.proxy.build_voice_timing_payload(payload, audio)

        self.assertEqual(metadata["schema_version"], "b1_voice_timing.v1")
        self.assertEqual(metadata["profile_id"], "native-profile-1")
        self.assertEqual(metadata["engine"], "chatterbox")
        self.assertEqual(metadata["language"], "de")
        self.assertEqual(metadata["duration_ms"], 1200)
        self.assertEqual(metadata["audio_sample_rate"], 48000)
        self.assertEqual(metadata["audio_channels"], 1)
        self.assertEqual(metadata["audio_sha256"], self.proxy.hashlib.sha256(audio).hexdigest())
        self.assertEqual(metadata["phoneme_alphabet"], "ipa")
        self.assertEqual(metadata["timing_precision"], "estimated")
        self.assertEqual([item["word"] for item in metadata["word_timestamps"]], ["Guten", "Morgen"])
        self.assertGreaterEqual(len(metadata["phoneme_timestamps"]), 2)
        previous_end = 0
        for item in metadata["word_timestamps"]:
            self.assertGreaterEqual(item["start_ms"], previous_end)
            self.assertGreaterEqual(item["end_ms"], item["start_ms"])
            previous_end = item["end_ms"]
        self.assertEqual(previous_end, 1200)

    def test_voice_timing_duration_uses_actual_bytes_for_streamed_wav_header(self) -> None:
        audio = streamed_wav_header(tiny_wav(duration_ms=1200))
        payload = {"profile_id": "native-profile-1", "text": "Guten Morgen", "language": "de", "engine": "chatterbox"}

        metadata = self.proxy.build_voice_timing_payload(payload, audio)

        self.assertEqual(metadata["duration_ms"], 1200)
        self.assertEqual(metadata["word_timestamps"][-1]["end_ms"], 1200)

    def test_voice_timing_store_and_lookup_by_generation_or_checksum(self) -> None:
        audio = tiny_wav(duration_ms=500)
        payload = {"profile_id": "native-profile-1", "text": "hello world", "language": "en", "engine": "chatterbox"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"B1_VOICEBOX_DATA_DIR": tmp}, clear=False):
            metadata = self.proxy.build_voice_timing_payload(payload, audio)
            self.proxy.store_voice_timing(metadata)
            by_generation = self.proxy.load_voice_timing(metadata["generation_id"], "")
            by_checksum = self.proxy.load_voice_timing("", metadata["audio_sha256"])

        self.assertEqual(by_generation["generation_id"], metadata["generation_id"])
        self.assertEqual(by_checksum["audio_sha256"], metadata["audio_sha256"])

    def test_voice_timing_uses_forced_alignment_when_available(self) -> None:
        audio = tiny_wav(duration_ms=1000)
        payload = {"profile_id": "native-profile-1", "text": "hello world", "language": "en", "engine": "chatterbox"}
        alignment = {
            "word_timestamps": [
                {"word": "hello", "start_ms": 100, "end_ms": 420, "confidence": 0.9},
                {"word": "world", "start_ms": 520, "end_ms": 900, "confidence": 0.88},
            ],
            "character_timestamps": [
                {"character": "h", "start_ms": 100, "end_ms": 160, "word_index": 0, "confidence": 0.9}
            ],
            "method": "torchaudio-mms-fa",
            "device": "cpu",
        }
        with patch.object(self.proxy, "forced_align_words_mms", return_value=alignment):
            metadata = self.proxy.build_voice_timing_payload(payload, audio)

        self.assertEqual(metadata["timing_method"], "torchaudio-mms-fa")
        self.assertEqual(metadata["timing_precision"], "word-forced-aligned_phoneme-estimated")
        self.assertEqual(metadata["alignment_model"], "torchaudio.pipelines.MMS_FA")
        self.assertEqual(metadata["alignment_device"], "cpu")
        self.assertEqual(metadata["character_timestamps"], alignment["character_timestamps"])
        self.assertEqual(metadata["word_timestamps"][0]["start_ms"], 100)
        self.assertEqual(metadata["phoneme_timestamps"][0]["start_ms"], 100)

    def test_openai_speech_bridge_preserves_error_status_and_json_type(self) -> None:
        client = FailingSpeechVoiceboxClient()
        payload = {"model": "voicebox-quality", "input": "hello", "voice": "missing-native-profile"}
        with patch.dict("os.environ", {"B1_VOICEBOX_UPSTREAM_URL": "http://voicebox-upstream"}, clear=False):
            response = asyncio.run(
                self.proxy.voicebox_openai_speech_bridge(
                    client,
                    json.dumps(payload).encode("utf-8"),
                    "application/json",
                )
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.media_type, "application/json")
        self.assertIn(b"upstream_generation_failed", response.body)

    def test_native_generation_lock_serializes_concurrent_posts(self) -> None:
        async def run() -> SlowVoiceboxClient:
            client = SlowVoiceboxClient()
            lock = asyncio.Lock()
            await asyncio.gather(
                self.proxy.post_native_generation(client, {"text": "one"}, lock),
                self.proxy.post_native_generation(client, {"text": "two"}, lock),
            )
            return client

        client = asyncio.run(run())

        self.assertEqual(client.max_active, 1)


if __name__ == "__main__":
    unittest.main()
