from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations" / "b1-model-client"))

from b1_model_client import __main__ as client  # noqa: E402


class ModelClientTests(unittest.TestCase):
    def force_local_planner(self) -> None:
        original = client.sync_plan_from_server

        def unavailable(*args: object, **kwargs: object) -> list[dict[str, Any]]:
            raise urllib.error.HTTPError("http://modelhub/modelhub/v1/sync/plan", 404, "missing", {}, None)

        client.sync_plan_from_server = unavailable
        self.addCleanup(lambda: setattr(client, "sync_plan_from_server", original))

    def test_planned_actions_skip_inference_only_models(self) -> None:
        self.force_local_planner()
        original = client.model_record
        try:
            client.model_record = lambda base_url, token, model_id: {
                "id": "tts-fast",
                "downloadable": False,
                "files": [],
                "license": {"name": "Test", "redistribution": "inference-only"},
                "execution_modes": ["hosted-inference"],
            }
            with tempfile.TemporaryDirectory() as tmp:
                actions = client.planned_actions("http://modelhub", None, Path(tmp), ["tts-fast"])
        finally:
            client.model_record = original
        self.assertEqual(actions[0]["model"], "tts-fast")
        self.assertEqual(actions[0]["action"], "skip")
        self.assertEqual(actions[0]["reason"], "model is not downloadable")
        self.assertEqual(actions[0]["license"]["redistribution"], "inference-only")

    def test_planned_actions_keep_verified_blob_and_download_missing_blob(self) -> None:
        self.force_local_planner()
        payload = b"model-blob"
        digest = hashlib.sha256(payload).hexdigest()
        missing_digest = hashlib.sha256(b"missing").hexdigest()

        def fake_model_record(base_url: str, token: str | None, model_id: str) -> dict[str, object]:
            return {
                "id": model_id,
                "version": "1.0.0",
                "display_name": "Downloadable Model",
                "downloadable": True,
                "source": {
                    "type": "direct-url",
                    "url": "https://models.example.test/model.gguf?token=secret",
                    "revision": "test",
                },
                "license": {"name": "Test", "redistribution": "downloadable", "acceptance_required": False},
                "resource_estimate": {"vram_gib": 1, "ram_gib": 1, "disk_gib": 1},
                "execution_modes": ["hosted-inference", "downloadable"],
                "files": [
                    {"sha256": digest, "size_bytes": len(payload)},
                    {"sha256": missing_digest, "size_bytes": 7},
                ],
            }

        original = client.model_record
        try:
            client.model_record = fake_model_record
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                (cache / "blobs").mkdir()
                (cache / "blobs" / digest).write_bytes(payload)
                actions = client.planned_actions("http://modelhub", None, cache, ["downloadable-model"])
        finally:
            client.model_record = original

        by_blob = {action["blob"]: action for action in actions}
        self.assertEqual(by_blob[digest]["action"], "keep")
        self.assertEqual(by_blob[missing_digest]["action"], "download")
        self.assertEqual(by_blob[missing_digest]["expected_size"], 7)
        self.assertEqual(by_blob[missing_digest]["source"]["url"], "https://models.example.test/model.gguf")
        self.assertFalse(by_blob[missing_digest]["requires_license_acceptance"])

    def test_planned_actions_prefers_server_sync_plan_and_adds_local_paths(self) -> None:
        digest = "a" * 64

        def fake_request_json(base_url: str, path: str, token: str | None, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
            self.assertEqual(path, "/modelhub/v1/sync/plan")
            self.assertEqual(method, "POST")
            self.assertEqual(payload, {"models": ["chat-default"], "installed_blobs": []})
            return {"actions": [{"model": "chat-default", "blob": digest, "action": "download", "expected_size": 12}]}

        original = client.request_json
        try:
            client.request_json = fake_request_json
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                actions = client.planned_actions("http://modelhub", "token", cache, ["chat-default"])
        finally:
            client.request_json = original

        self.assertEqual(actions[0]["blob"], digest)
        self.assertEqual(actions[0]["path"], str(cache / "blobs" / digest))
        self.assertEqual(actions[0]["resume_from"], 0)

    def test_pin_and_unpin_persist_local_state_without_plaintext_tokens(self) -> None:
        original = client.model_record
        try:
            client.model_record = lambda base_url, token, model_id: {"display_name": f"Model {model_id}", "downloadable": True}
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                pin_args = argparse.Namespace(base_url="http://modelhub", token="secret-token", cache=str(cache), model=["chat-default"])
                self.assertEqual(client.pin(pin_args), 0)
                state = client.load_state(cache)
                self.assertIn("chat-default", state["pins"])
                self.assertNotIn("secret-token", (cache / "b1-model-client-state.json").read_text(encoding="utf-8"))

                unpin_args = argparse.Namespace(cache=str(cache), model=["chat-default"])
                self.assertEqual(client.unpin(unpin_args), 0)
                self.assertEqual(client.load_state(cache)["pins"], {})
        finally:
            client.model_record = original

    def test_prune_removes_only_managed_unpinned_blobs_and_ignores_unmanaged_files(self) -> None:
        kept_digest = "1" * 64
        removed_digest = "2" * 64
        unmanaged_name = "unmanaged-file"

        def fake_plan(base_url: str, token: str | None, cache: Path, models: list[str]) -> list[dict[str, Any]]:
            return [{"action": "keep", "blob": kept_digest, "expected_size": 4}]

        original = client.sync_plan_from_server
        try:
            client.sync_plan_from_server = fake_plan
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                blobs = cache / "blobs"
                blobs.mkdir(parents=True)
                (blobs / kept_digest).write_bytes(b"keep")
                (blobs / removed_digest).write_bytes(b"drop")
                (blobs / unmanaged_name).write_bytes(b"do-not-touch")
                state = client.empty_state()
                state["pins"] = {"chat-default": {"model": "chat-default"}}
                state["managed_blobs"] = {
                    kept_digest: {"size_bytes": 4},
                    removed_digest: {"size_bytes": 4},
                    "../escape": {"size_bytes": 1},
                }
                client.save_state(cache, state)

                dry_plan = client.prune_plan("http://modelhub", None, cache, ["chat-default"])
                self.assertEqual([item["blob"] for item in dry_plan["candidates"]], [removed_digest])
                self.assertTrue((blobs / removed_digest).is_file())

                prune_args = argparse.Namespace(base_url="http://modelhub", token=None, cache=str(cache), model=["chat-default"], dry_run=False)
                self.assertEqual(client.prune(prune_args), 0)

                self.assertTrue((blobs / kept_digest).is_file())
                self.assertFalse((blobs / removed_digest).exists())
                self.assertTrue((blobs / unmanaged_name).is_file())
                self.assertNotIn(removed_digest, client.load_state(cache)["managed_blobs"])
        finally:
            client.sync_plan_from_server = original

    def test_daemon_once_syncs_pinned_models_and_prunes_managed_cache(self) -> None:
        kept_digest = "3" * 64
        removed_digest = "4" * 64

        def fake_plan(base_url: str, token: str | None, cache: Path, models: list[str]) -> list[dict[str, Any]]:
            self.assertEqual(models, ["chat-default"])
            return [{"action": "keep", "blob": kept_digest, "expected_size": 4, "path": str(cache / "blobs" / kept_digest)}]

        original = client.sync_plan_from_server
        try:
            client.sync_plan_from_server = fake_plan
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                blobs = cache / "blobs"
                blobs.mkdir(parents=True)
                (blobs / kept_digest).write_bytes(b"keep")
                (blobs / removed_digest).write_bytes(b"drop")
                state = client.empty_state()
                state["pins"] = {"chat-default": {"model": "chat-default"}}
                state["managed_blobs"] = {
                    kept_digest: {"size_bytes": 4},
                    removed_digest: {"size_bytes": 4},
                }
                client.save_state(cache, state)

                args = argparse.Namespace(
                    base_url="http://modelhub",
                    token=None,
                    cache=str(cache),
                    model=[],
                    dry_run=False,
                    prune=True,
                    once=True,
                    interval_seconds=1,
                )
                self.assertEqual(client.daemon(args), 0)

                self.assertTrue((blobs / kept_digest).is_file())
                self.assertFalse((blobs / removed_digest).exists())
                self.assertNotIn(removed_digest, client.load_state(cache)["managed_blobs"])
        finally:
            client.sync_plan_from_server = original

    def test_sync_once_downloads_with_supplied_endpoint_and_records_managed_blob(self) -> None:
        digest = "5" * 64
        seen: dict[str, object] = {}

        def fake_actions(base_url: str, token: str | None, cache: Path, models: list[str]) -> list[dict[str, Any]]:
            self.assertEqual(base_url, "http://modelhub")
            self.assertEqual(token, "sync-token")
            return [
                {
                    "action": "download",
                    "blob": digest,
                    "expected_size": 7,
                    "path": str(cache / "blobs" / digest),
                }
            ]

        def fake_download(
            base_url: str,
            token: str | None,
            sha256: str,
            expected_size: int,
            target: Path,
            *,
            source: dict[str, Any] | None = None,
            accept_licenses: bool = False,
        ) -> dict[str, Any]:
            seen.update({"base_url": base_url, "token": token, "sha256": sha256, "expected_size": expected_size, "target": target})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"payload")
            return {"blob": sha256, "status": "downloaded", "path": str(target), "size_bytes": expected_size}

        original_plan = client.planned_actions
        original_download = client.download_blob
        try:
            client.planned_actions = fake_actions
            client.download_blob = fake_download
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                payload = client.sync_once("http://modelhub", "sync-token", cache, ["chat-default"])
                state = client.load_state(cache)
        finally:
            client.planned_actions = original_plan
            client.download_blob = original_download

        self.assertEqual(payload["changes"][0]["status"], "downloaded")
        self.assertEqual(seen["base_url"], "http://modelhub")
        self.assertEqual(seen["token"], "sync-token")
        self.assertIn(digest, state["managed_blobs"])

    def test_sync_once_requires_explicit_license_acceptance(self) -> None:
        digest = "6" * 64
        seen: dict[str, object] = {}

        def fake_actions(base_url: str, token: str | None, cache: Path, models: list[str]) -> list[dict[str, Any]]:
            return [
                {
                    "action": "download",
                    "model": "chat-default",
                    "blob": digest,
                    "expected_size": 7,
                    "path": str(cache / "blobs" / digest),
                    "requires_license_acceptance": True,
                    "model_metadata": {
                        "id": "licenced-model",
                        "version": "1.0.0",
                        "display_name": "Licenced Model",
                        "license": {"name": "Example", "acceptance_required": True},
                    },
                }
            ]

        def fake_download(
            base_url: str,
            token: str | None,
            sha256: str,
            expected_size: int,
            target: Path,
            *,
            source: dict[str, Any] | None = None,
            accept_licenses: bool = False,
        ) -> dict[str, Any]:
            self.assertTrue(accept_licenses)
            seen["downloaded"] = True
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"payload")
            return {"blob": sha256, "status": "downloaded", "path": str(target), "size_bytes": expected_size}

        original_plan = client.planned_actions
        original_download = client.download_blob
        try:
            client.planned_actions = fake_actions
            client.download_blob = fake_download
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                with self.assertRaisesRegex(RuntimeError, "licence acceptance required"):
                    client.sync_once("http://modelhub", "sync-token", cache, ["chat-default"])
                self.assertNotIn("downloaded", seen)
                payload = client.sync_once("http://modelhub", "sync-token", cache, ["chat-default"], accept_licenses=True)
        finally:
            client.planned_actions = original_plan
            client.download_blob = original_download

        self.assertEqual(payload["changes"][0]["status"], "downloaded")
        self.assertTrue(seen["downloaded"])

    def test_download_blob_validates_etag_content_length_and_range_metadata(self) -> None:
        payload = b"hello-world"
        digest = hashlib.sha256(payload).hexdigest()

        class FakeResponse:
            status = 200

            def __init__(self, body: bytes, headers: dict[str, str]) -> None:
                self.body = body
                self.headers = headers
                self.offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                if self.offset >= len(self.body):
                    return b""
                end = len(self.body) if size < 0 else min(len(self.body), self.offset + size)
                chunk = self.body[self.offset:end]
                self.offset = end
                return chunk

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            return FakeResponse(
                payload,
                {
                    "ETag": f'"sha256:{digest}"',
                    "X-Checksum-SHA256": digest,
                    "Content-Length": str(len(payload)),
                },
            )

        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                result = client.download_blob("http://modelhub", None, digest, len(payload), target)
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(target.read_bytes(), payload)
        finally:
            client.urllib.request.urlopen = original

    def test_download_blob_sends_license_acceptance_header_when_enabled(self) -> None:
        payload = b"accepted-model"
        digest = hashlib.sha256(payload).hexdigest()
        seen_headers: dict[str, str] = {}

        class FakeResponse:
            status = 200
            headers = {
                "ETag": f'"sha256:{digest}"',
                "X-Checksum-SHA256": digest,
                "Content-Length": str(len(payload)),
            }

            def __init__(self) -> None:
                self.offset = 0

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, size: int = -1) -> bytes:
                if self.offset >= len(payload):
                    return b""
                end = len(payload) if size < 0 else min(len(payload), self.offset + size)
                chunk = payload[self.offset:end]
                self.offset = end
                return chunk

        def fake_urlopen(request: object, timeout: int = 120) -> FakeResponse:
            seen_headers.update({key.lower(): value for key, value in request.header_items()})
            return FakeResponse()

        source = {
            "requires_license_acceptance": True,
            "model_metadata": {"id": "licenced-model", "version": "1.0.0"},
        }
        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = fake_urlopen
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "blobs" / digest
                result = client.download_blob(
                    "http://modelhub",
                    None,
                    digest,
                    len(payload),
                    target,
                    source=source,
                    accept_licenses=True,
                )
        finally:
            client.urllib.request.urlopen = original

        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(seen_headers["x-b1-accept-license"], "licenced-model@1.0.0")

    def test_load_state_rejects_malformed_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "b1-model-client-state.json").write_text(
                '{"format":"b1-model-client-cache/v1","pins":[],"managed_blobs":{}}',
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                client.load_state(cache)


if __name__ == "__main__":
    unittest.main()
