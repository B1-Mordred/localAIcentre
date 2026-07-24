from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations" / "comfyui-b1-remote-nodes"))

from comfyui_b1_remote_nodes import nodes  # noqa: E402


class FakeResponse:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.headers = headers or {"content-type": "application/json"}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class EnvPatch:
    def __init__(self, **values: str | None) -> None:
        self.values = values
        self.original: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self.values.items():
            self.original[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        for key, value in self.original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def staged_reference(kind: str = "image", mime_type: str = "image/png") -> dict[str, Any]:
    return {
        "source": "staged_upload",
        "id": "upload_" + "a" * 32,
        "field": kind,
        "kind": kind,
        "mime_type": mime_type,
        "filename": f"input.{mime_type.split('/', 1)[1]}",
        "path": f"inputs/user/upload_{'a' * 32}/{kind}-input.{mime_type.split('/', 1)[1]}",
        "bytes": 8,
        "sha256": "b" * 64,
    }


def chmod_private(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def chmod_public(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o644)


class ComfyUiRemoteNodesTests(unittest.TestCase):
    def setUp(self) -> None:
        original_config_file = os.environ.get(nodes.CONFIG_FILE_ENV)
        os.environ[nodes.CONFIG_FILE_ENV] = ""

        def restore_config_file() -> None:
            if original_config_file is None:
                os.environ.pop(nodes.CONFIG_FILE_ENV, None)
            else:
                os.environ[nodes.CONFIG_FILE_ENV] = original_config_file

        self.addCleanup(restore_config_file)

    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(nodes, name)
        setattr(nodes, name, value)
        self.addCleanup(lambda: setattr(nodes, name, original))

    def test_request_json_uses_configured_unified_api_and_bearer_token(self) -> None:
        seen: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["authorization"] = request.get_header("Authorization")
            seen["body"] = request.data
            seen["timeout"] = timeout
            return FakeResponse(json.dumps({"ok": True}).encode("utf-8"))

        self.patch_attr("urllib", nodes.urllib)
        original_urlopen = nodes.urllib.request.urlopen
        nodes.urllib.request.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(nodes.urllib.request, "urlopen", original_urlopen))

        with EnvPatch(B1_AI_HUB_API_BASE="https://api.test.local/", B1_AI_HUB_API_KEY="b1k_public.secret"):
            result = nodes.request_json("/v1/models", {"probe": True}, method="POST", timeout_seconds=9)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["url"], "https://api.test.local/v1/models")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["authorization"], "Bearer b1k_public.secret")
        self.assertEqual(json.loads(seen["body"].decode("utf-8")), {"probe": True})
        self.assertEqual(seen["timeout"], 9)

    def test_local_config_file_supplies_api_key_base_download_dir_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "b1-remote-nodes.json"
            key_path = Path(tmp) / "api-key"
            key_path.write_text("b1k_config.secret\n", encoding="utf-8")
            chmod_private(key_path)
            download_dir = Path(tmp) / "downloads"
            config_path.write_text(
                json.dumps(
                    {
                        "api_base": "http://api.test.local/base/",
                        "api_key_file": "api-key",
                        "download_dir": str(download_dir),
                        "max_data_url_bytes": 2,
                    }
                ),
                encoding="utf-8",
            )
            with EnvPatch(
                B1_AI_HUB_CONFIG_FILE=str(config_path),
                B1_AI_HUB_API_BASE=None,
                B1_AI_HUB_API_KEY=None,
                B1_AI_HUB_DOWNLOAD_DIR=None,
                B1_AI_HUB_MAX_DATA_URL_BYTES=None,
            ):
                request = nodes.build_request("/v1/models")
                self.assertEqual(request.full_url, "http://api.test.local/base/v1/models")
                self.assertEqual(request.get_header("Authorization"), "Bearer b1k_config.secret")
                self.assertEqual(nodes.configured_download_dir(), download_dir)
                with self.assertRaises(nodes.B1RemoteNodeError):
                    nodes.require_media_reference("data:image/png;base64,QUFB", "image")

    def test_environment_values_override_local_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "b1-remote-nodes.json"
            config_path.write_text(json.dumps({"api_base": "http://config.test", "api_key": "config-key"}), encoding="utf-8")
            chmod_public(config_path)
            with EnvPatch(
                B1_AI_HUB_CONFIG_FILE=str(config_path),
                B1_AI_HUB_API_BASE="https://env.test.local/",
                B1_AI_HUB_API_KEY="b1k_env.secret",
            ):
                request = nodes.build_request("/v1/models")

        self.assertEqual(request.full_url, "https://env.test.local/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer b1k_env.secret")

    def test_config_file_can_be_disabled_and_explicit_missing_file_fails(self) -> None:
        with EnvPatch(
            B1_AI_HUB_CONFIG_FILE="",
            B1_AI_HUB_API_BASE=None,
            B1_AI_HUB_API_KEY=None,
            B1_AI_HUB_DOWNLOAD_DIR=None,
        ):
            self.assertEqual(nodes.api_base(), "https://api.ai.b1.germering")
            self.assertEqual(nodes.api_key(), "")
            self.assertEqual(nodes.configured_download_dir(), Path(nodes.DEFAULT_OUTPUT_DIR))
        with EnvPatch(B1_AI_HUB_CONFIG_FILE="/tmp/b1-ai-hub-missing-config.json", B1_AI_HUB_API_BASE=None):
            with self.assertRaises(nodes.B1RemoteNodeError):
                nodes.api_base()

    def test_api_key_file_env_reads_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "api-key"
            key_path.write_text("b1k_file.secret\n", encoding="utf-8")
            chmod_private(key_path)
            with EnvPatch(B1_AI_HUB_CONFIG_FILE="", B1_AI_HUB_API_KEY=None, B1_AI_HUB_API_KEY_FILE=str(key_path)):
                self.assertEqual(nodes.api_key(), "b1k_file.secret")

    def test_api_key_file_rejects_group_or_world_accessible_file(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permission bits are not portable on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "api-key"
            key_path.write_text("b1k_file.secret\n", encoding="utf-8")
            chmod_public(key_path)
            with EnvPatch(B1_AI_HUB_CONFIG_FILE="", B1_AI_HUB_API_KEY=None, B1_AI_HUB_API_KEY_FILE=str(key_path)):
                with self.assertRaises(nodes.B1RemoteNodeError):
                    nodes.api_key()

    def test_inline_config_api_key_requires_private_config_file(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permission bits are not portable on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "b1-remote-nodes.json"
            config_path.write_text(json.dumps({"api_key": "b1k_inline.secret"}), encoding="utf-8")
            chmod_public(config_path)
            with EnvPatch(B1_AI_HUB_CONFIG_FILE=str(config_path), B1_AI_HUB_API_KEY=None, B1_AI_HUB_API_KEY_FILE=None):
                with self.assertRaises(nodes.B1RemoteNodeError):
                    nodes.api_key()
                chmod_private(config_path)
                self.assertEqual(nodes.api_key(), "b1k_inline.secret")

    def test_config_rejects_ambiguous_api_key_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "b1-remote-nodes.json"
            key_path = Path(tmp) / "api-key"
            key_path.write_text("b1k_file.secret\n", encoding="utf-8")
            chmod_private(key_path)
            config_path.write_text(json.dumps({"api_key": "b1k_inline.secret", "api_key_file": str(key_path)}), encoding="utf-8")
            chmod_private(config_path)
            with EnvPatch(B1_AI_HUB_CONFIG_FILE=str(config_path), B1_AI_HUB_API_KEY=None, B1_AI_HUB_API_KEY_FILE=None):
                with self.assertRaises(nodes.B1RemoteNodeError):
                    nodes.api_key()

    def test_api_base_rejects_credentials_query_and_non_http_schemes(self) -> None:
        for value in [
            "file:///tmp/api",
            "https://user:pass@api.test.local",
            "https://api.test.local?token=secret",
            "https://api.test.local/#fragment",
        ]:
            with self.subTest(value=value), EnvPatch(B1_AI_HUB_CONFIG_FILE="", B1_AI_HUB_API_BASE=value):
                with self.assertRaises(nodes.B1RemoteNodeError):
                    nodes.api_base()

    def test_request_url_rejects_paths_that_are_not_api_routes(self) -> None:
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.request_url("v1/models")

    def test_text_to_speech_calls_unified_audio_endpoint_not_comfyui(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_request_bytes(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> tuple[bytes, dict[str, str]]:
            calls.append({"path": path, "payload": payload, **kwargs})
            return b"RIFF....WAVEaudio", {"content-type": "audio/wav"}

        self.patch_attr("request_bytes", fake_request_bytes)
        with tempfile.TemporaryDirectory() as tmp, EnvPatch(B1_AI_HUB_DOWNLOAD_DIR=tmp):
            file_path, byte_count, digest = nodes.B1TextToSpeech().run("tts-fast", "hello", "default", runtime_policy="non_comfy_only")
            self.assertTrue(Path(file_path).is_file())

        self.assertEqual(calls[0]["path"], "/v1/audio/speech")
        self.assertNotIn("comfy", calls[0]["path"])
        self.assertEqual(calls[0]["payload"]["model"], "tts-fast")
        self.assertEqual(calls[0]["payload"]["runtime_policy"], "non_comfy_only")
        self.assertEqual(byte_count, len(b"RIFF....WAVEaudio"))
        self.assertEqual(digest, nodes.hashlib.sha256(b"RIFF....WAVEaudio").hexdigest())

    def test_speech_to_text_uses_openai_style_multipart_model_and_file(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_request_json(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append({"path": path, "payload": payload, **kwargs})
            return {"text": "hello", "b1_placeholder": False}

        self.patch_attr("request_json", fake_request_json)
        text, raw = nodes.B1SpeechToText().run(
            "stt-default",
            nodes.base64.b64encode(b"RIFF....WAVEaudio").decode("ascii"),
            language="en",
            runtime_policy="non_comfy_only",
            audio_mime_type="audio/wav",
            filename="../sample.wav",
        )

        self.assertEqual(text, "hello")
        self.assertEqual(json.loads(raw)["text"], "hello")
        self.assertEqual(calls[0]["path"], "/v1/audio/transcriptions")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertIsNone(calls[0]["payload"])
        self.assertNotIn("X-B1-Model", calls[0]["headers"])
        content_type = calls[0]["headers"]["Content-Type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        body = calls[0]["data"]
        self.assertIn(b'name="model"\r\n\r\nstt-default\r\n', body)
        self.assertIn(b'name="language"\r\n\r\nen\r\n', body)
        self.assertIn(b'name="runtime_policy"\r\n\r\nnon_comfy_only\r\n', body)
        self.assertIn(b'name="file"; filename="sample.wav"', body)
        self.assertIn(b"Content-Type: audio/wav", body)
        self.assertIn(b"RIFF....WAVEaudio", body)
        self.assertNotIn(b"X-B1-Model", body)

    def test_multipart_builder_rejects_unsafe_names_and_header_values(self) -> None:
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.multipart_form_data({"bad\r\nname": "value"}, [])
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.multipart_form_data({}, [("file", "audio.wav", "audio/wav\r\nX-Bad: yes", b"audio")])

    def test_submit_job_node_posts_parsed_input_json(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_request_json(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append({"path": path, "payload": payload, **kwargs})
            return {"id": "job_1", "state": "queued"}

        self.patch_attr("request_json", fake_request_json)
        job_id, raw = nodes.B1SubmitMediaJob().run("image", "generation", "image-default", "{\"prompt\":\"x\"}")

        self.assertEqual(job_id, "job_1")
        self.assertEqual(calls[0]["path"], "/v1/media/jobs")
        self.assertEqual(calls[0]["payload"]["input"], {"prompt": "x"})
        self.assertEqual(json.loads(raw)["state"], "queued")

    def test_wait_job_polls_until_terminal_state(self) -> None:
        responses = [{"id": "job_1", "state": "running"}, {"id": "job_1", "state": "completed"}]

        def fake_request_json(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            self.assertEqual(path, "/v1/media/jobs/job_1")
            return responses.pop(0)

        self.patch_attr("request_json", fake_request_json)
        self.patch_attr("time", type("FakeTime", (), {"monotonic": staticmethod(lambda: 0), "sleep": staticmethod(lambda seconds: None)})())

        state, raw = nodes.B1WaitMediaJob().run("job_1", 10, 0.25)

        self.assertEqual(state, "completed")
        self.assertEqual(json.loads(raw)["id"], "job_1")

    def test_job_id_path_segments_are_validated_for_job_routes(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_request_json(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append({"path": path, "payload": payload, **kwargs})
            if kwargs.get("method") == "DELETE":
                return {"id": "job_1", "state": "cancelled"}
            if path.endswith("/artifacts"):
                return {"data": []}
            return {"id": "job_1", "state": "completed"}

        self.patch_attr("request_json", fake_request_json)
        self.patch_attr("time", type("FakeTime", (), {"monotonic": staticmethod(lambda: 0), "sleep": staticmethod(lambda seconds: None)})())

        nodes.B1WaitMediaJob().run(" job_1 ", 10, 0.25)
        nodes.B1CancelMediaJob().run("job_1")
        nodes.B1ListJobArtifacts().run("job_1")

        self.assertEqual(
            [call["path"] for call in calls],
            ["/v1/media/jobs/job_1", "/v1/media/jobs/job_1", "/v1/media/jobs/job_1/artifacts"],
        )

        for value in ["", "job_1/../../admin", "job_1%2Fsecret", "job_1?x=1", "../job_1", "job_1\nx"]:
            with self.subTest(value=value):
                with self.assertRaises(nodes.B1RemoteNodeError):
                    nodes.B1CancelMediaJob().run(value)

    def test_artifact_download_rejects_external_url_and_sanitizes_filename(self) -> None:
        def fake_request_bytes(path: str, **kwargs: Any) -> tuple[bytes, dict[str, str]]:
            self.assertEqual(path, "/artifacts/runtime/job/0.png")
            return b"\x89PNG\r\n\x1a\n", {"content-type": "image/png"}

        self.patch_attr("request_bytes", fake_request_bytes)
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1DownloadArtifact().run("https://api.test.local/artifacts/runtime/job/0.png", "bad.png")
        for value in [
            "/artifacts/runtime/job/0.png?token=secret",
            "/artifacts/runtime/job/0.png#fragment",
            "/artifacts/runtime/../secret.png",
            "/artifacts/runtime/%2e%2e/secret.png",
            "/artifacts/runtime/%2Fsecret.png",
        ]:
            with self.subTest(value=value):
                with self.assertRaises(nodes.B1RemoteNodeError):
                    nodes.B1DownloadArtifact().run(value, "bad.png")

        with tempfile.TemporaryDirectory() as tmp, EnvPatch(B1_AI_HUB_DOWNLOAD_DIR=tmp):
            file_path, byte_count, digest = nodes.B1DownloadArtifact().run("/artifacts/runtime/job/0.png", "../../unsafe name.png")

        self.assertEqual(byte_count, len(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(digest, nodes.hashlib.sha256(b"\x89PNG\r\n\x1a\n").hexdigest())
        self.assertEqual(Path(file_path).parent, Path(tmp).resolve())
        self.assertFalse(".." in Path(file_path).name)

    def test_media_references_reject_local_paths(self) -> None:
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.require_media_reference("/home/user/private.png", "image")
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.require_media_reference("https://example.test/image.png", "image")
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.require_media_reference("{\"path\":\"/home/user/private.png\"}", "image")
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.require_media_reference("/artifacts/images/job/0.png?token=secret", "image")
        self.assertEqual(nodes.require_media_reference("/artifacts/images/job/0.png", "image"), "/artifacts/images/job/0.png")
        self.assertEqual(nodes.require_media_reference("data:image/png;base64,AAAA", "image"), "data:image/png;base64,AAAA")
        reference = staged_reference()
        self.assertEqual(nodes.require_media_reference(json.dumps(reference), "image"), reference)
        self.assertEqual(nodes.require_media_reference(json.dumps({"reference": reference, "input": reference}), "image"), reference)

    def test_media_reference_data_urls_must_be_bounded_base64_media(self) -> None:
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.require_media_reference("data:image/png,not-base64", "image")
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.require_media_reference("data:text/plain;base64,AAAA", "image")
        with EnvPatch(B1_AI_HUB_MAX_DATA_URL_BYTES="2"):
            with self.assertRaises(nodes.B1RemoteNodeError):
                nodes.require_media_reference("data:image/png;base64,QUFB", "image")

    def test_staged_media_references_validate_shape_and_media_kind(self) -> None:
        reference = staged_reference()
        for key, value in [
            ("source", "other"),
            ("id", "upload_bad"),
            ("path", "inputs/user/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/../private.png"),
            ("mime_type", "text/plain"),
            ("kind", "audio"),
            ("bytes", 0),
            ("sha256", "not-a-sha"),
        ]:
            invalid = dict(reference)
            invalid[key] = value
            with self.subTest(key=key):
                with self.assertRaises(nodes.B1RemoteNodeError):
                    nodes.require_media_reference(json.dumps(invalid), "image")

    def test_upload_media_returns_direct_reference_and_full_response(self) -> None:
        reference = staged_reference()
        upload = {"input": reference, "reference": reference}
        calls: list[dict[str, Any]] = []

        def fake_request_json(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append({"path": path, "payload": payload, **kwargs})
            return upload

        self.patch_attr("request_json", fake_request_json)
        reference_json, upload_json = nodes.B1UploadMediaBase64().run(
            "image",
            "image/png",
            "../input.png",
            nodes.base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
        )

        self.assertEqual(json.loads(reference_json), reference)
        self.assertEqual(json.loads(upload_json), upload)
        self.assertEqual(calls[0]["path"], "/v1/media/uploads")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["data"], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(calls[0]["headers"]["Content-Type"], "image/png")
        self.assertEqual(calls[0]["headers"]["X-B1-Field"], "image")
        self.assertEqual(calls[0]["headers"]["X-B1-Filename"], "input.png")

    def test_upload_media_normalizes_headers_and_rejects_unsafe_values(self) -> None:
        reference = staged_reference()
        calls: list[dict[str, Any]] = []

        def fake_request_json(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append({"path": path, "payload": payload, **kwargs})
            return {"reference": reference}

        self.patch_attr("request_json", fake_request_json)
        nodes.B1UploadMediaBase64().run(
            "image",
            "IMAGE/PNG; charset=utf-8",
            "../../unsafe name.png",
            nodes.base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
        )

        self.assertEqual(calls[0]["headers"]["Content-Type"], "image/png")
        self.assertEqual(calls[0]["headers"]["X-B1-Field"], "image")
        self.assertEqual(calls[0]["headers"]["X-B1-Filename"], "unsafe_name.png")

        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1UploadMediaBase64().run("bad\r\nfield", "image/png", "input.png", "AAAA")
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1UploadMediaBase64().run("image", "image/png\r\nX-Bad: yes", "input.png", "AAAA")
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1UploadMediaBase64().run("image", "text/plain", "input.png", "AAAA")
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1UploadMediaBase64().run("media", "application/octet-stream", "input.bin", "AAAA")
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1UploadMediaBase64().run("image", "image/bmp", "input.bmp", "AAAA")

    def test_upload_media_validates_custom_field_response_kind_from_mime(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_request_json(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append({"path": path, "payload": payload, **kwargs})
            return {"reference": staged_reference(kind="image", mime_type="image/webp")}

        self.patch_attr("request_json", fake_request_json)
        reference_json, _ = nodes.B1UploadMediaBase64().run(
            "source_image",
            "image/webp",
            "input.webp",
            nodes.base64.b64encode(b"RIFFxxxxWEBP").decode("ascii"),
        )
        self.assertEqual(json.loads(reference_json)["kind"], "image")
        self.assertEqual(calls[0]["headers"]["X-B1-Field"], "source_image")

        self.patch_attr("request_json", lambda *args, **kwargs: {"reference": staged_reference(kind="audio", mime_type="audio/wav")})
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1UploadMediaBase64().run(
                "source_image",
                "image/png",
                "input.png",
                nodes.base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
            )

    def test_image_to_image_submits_staged_reference_object(self) -> None:
        reference = staged_reference()
        calls: list[dict[str, Any]] = []

        def fake_submit_media_job(modality: str, operation: str, model: str, input_payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            calls.append({"modality": modality, "operation": operation, "model": model, "input": input_payload, **kwargs})
            return {"id": "job_1", "state": "queued"}

        self.patch_attr("submit_media_job", fake_submit_media_job)
        job_id, raw = nodes.B1ImageToImage().run("image-edit", "repair", json.dumps({"reference": reference}))

        self.assertEqual(job_id, "job_1")
        self.assertEqual(json.loads(raw)["state"], "queued")
        self.assertEqual(calls[0]["modality"], "image")
        self.assertEqual(calls[0]["operation"], "edit")
        self.assertEqual(calls[0]["input"]["image"], reference)

    def test_vision_analysis_rejects_staged_json_reference(self) -> None:
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1VisionAnalysis().run("vision-default", "describe", json.dumps(staged_reference()))

    def test_required_node_classes_are_registered(self) -> None:
        for name in [
            "B1ListModels",
            "B1SelectModelAlias",
            "B1ChatText",
            "B1VisionAnalysis",
            "B1Embeddings",
            "B1TextToImage",
            "B1ImageToImage",
            "B1TextToVideo",
            "B1ImageToVideo",
            "B1TextToSpeech",
            "B1SpeechToText",
            "B1UploadMediaBase64",
            "B1SubmitMediaJob",
            "B1WaitMediaJob",
            "B1CancelMediaJob",
            "B1ListJobArtifacts",
            "B1DownloadArtifact",
        ]:
            self.assertIn(name, nodes.NODE_CLASS_MAPPINGS)
            self.assertIn(name, nodes.NODE_DISPLAY_NAME_MAPPINGS)


if __name__ == "__main__":
    unittest.main()
