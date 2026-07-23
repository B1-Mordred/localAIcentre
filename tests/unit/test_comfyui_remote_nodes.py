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
    def __init__(self, **values: str) -> None:
        self.values = values
        self.original: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self.values.items():
            self.original[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        for key, value in self.original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ComfyUiRemoteNodesTests(unittest.TestCase):
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

    def test_text_to_speech_calls_unified_audio_endpoint_not_comfyui(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_request_bytes(path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> tuple[bytes, dict[str, str]]:
            calls.append({"path": path, "payload": payload, **kwargs})
            return b"RIFF....WAVEaudio", {"content-type": "audio/wav"}

        self.patch_attr("request_bytes", fake_request_bytes)
        with tempfile.TemporaryDirectory() as tmp, EnvPatch(B1_AI_HUB_DOWNLOAD_DIR=tmp):
            file_path, byte_count, digest = nodes.B1TextToSpeech().run("tts-fast", "hello", "default")
            self.assertTrue(Path(file_path).is_file())

        self.assertEqual(calls[0]["path"], "/v1/audio/speech")
        self.assertNotIn("comfy", calls[0]["path"])
        self.assertEqual(calls[0]["payload"]["model"], "tts-fast")
        self.assertEqual(byte_count, len(b"RIFF....WAVEaudio"))
        self.assertEqual(digest, nodes.hashlib.sha256(b"RIFF....WAVEaudio").hexdigest())

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

    def test_artifact_download_rejects_external_url_and_sanitizes_filename(self) -> None:
        def fake_request_bytes(path: str, **kwargs: Any) -> tuple[bytes, dict[str, str]]:
            self.assertEqual(path, "/artifacts/runtime/job/0.png")
            return b"\x89PNG\r\n\x1a\n", {"content-type": "image/png"}

        self.patch_attr("request_bytes", fake_request_bytes)
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.B1DownloadArtifact().run("https://api.test.local/artifacts/runtime/job/0.png", "bad.png")

        with tempfile.TemporaryDirectory() as tmp, EnvPatch(B1_AI_HUB_DOWNLOAD_DIR=tmp):
            file_path, byte_count, digest = nodes.B1DownloadArtifact().run("/artifacts/runtime/job/0.png", "../../unsafe name.png")

        self.assertEqual(byte_count, len(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(digest, nodes.hashlib.sha256(b"\x89PNG\r\n\x1a\n").hexdigest())
        self.assertEqual(Path(file_path).parent, Path(tmp).resolve())
        self.assertFalse(".." in Path(file_path).name)

    def test_media_references_reject_local_paths(self) -> None:
        with self.assertRaises(nodes.B1RemoteNodeError):
            nodes.require_media_reference("/home/user/private.png", "image")
        self.assertEqual(nodes.require_media_reference("/artifacts/images/job/0.png", "image"), "/artifacts/images/job/0.png")
        self.assertEqual(nodes.require_media_reference("data:image/png;base64,AAAA", "image"), "data:image/png;base64,AAAA")

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
            "B1SubmitMediaJob",
            "B1WaitMediaJob",
            "B1CancelMediaJob",
            "B1DownloadArtifact",
        ]:
            self.assertIn(name, nodes.NODE_CLASS_MAPPINGS)
            self.assertIn(name, nodes.NODE_DISPLAY_NAME_MAPPINGS)


if __name__ == "__main__":
    unittest.main()
