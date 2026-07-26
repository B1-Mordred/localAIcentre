from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import Any


from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))
from tests.compatibility import test_native_comfyui_compatibility as native_comfyui_live  # noqa: E402

try:
    from fastapi import Response  # noqa: E402
    from app import main  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "websockets"}:
        raise
    main = None
    Response = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


class NativeComfyUiLiveHarnessHelperTests(unittest.TestCase):
    def test_live_harness_defaults_to_bundled_tiny_smoke_prompt_metadata(self) -> None:
        original_json = os.environ.pop("B1_NATIVE_COMFYUI_PROMPT_JSON", None)
        original_file = os.environ.pop("B1_NATIVE_COMFYUI_PROMPT_FILE", None)
        try:
            payload, metadata = native_comfyui_live.load_prompt_payload_with_metadata()
        finally:
            if original_json is not None:
                os.environ["B1_NATIVE_COMFYUI_PROMPT_JSON"] = original_json
            if original_file is not None:
                os.environ["B1_NATIVE_COMFYUI_PROMPT_FILE"] = original_file

        self.assertIn("prompt", payload)
        self.assertEqual(metadata["source"], "default-smoke-file")
        self.assertEqual(metadata["file_name"], "native-comfyui-smoke-prompt.json")
        self.assertTrue(metadata["route_level_smoke"])
        self.assertTrue(metadata["default_prompt_file"])
        self.assertIn("B1RuntimeTinyImage", metadata["class_types"])
        self.assertIn("SaveImage", metadata["class_types"])

    def test_prompt_metadata_marks_operator_prompt_as_handoff_candidate(self) -> None:
        payload = {"prompt": {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}}, "2": {"class_type": "SaveImage", "inputs": {}}}}

        metadata = native_comfyui_live.prompt_metadata(payload, source="env-file", file_path="/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json")

        self.assertEqual(metadata["file_name"], "text-to-image-api-prompt.json")
        self.assertFalse(metadata["route_level_smoke"])
        self.assertFalse(metadata["default_prompt_file"])
        self.assertEqual(metadata["node_count"], 2)
        self.assertEqual(metadata["class_type_count"], 2)

    def test_upload_save_prompt_is_handoff_candidate_without_model_weights(self) -> None:
        path = ROOT / "workflows" / "acceptance" / "native-comfyui-upload-save-prompt.json"
        payload = json.loads(path.read_text(encoding="utf-8"))

        metadata = native_comfyui_live.prompt_metadata(payload, source="env-file", file_path=str(path))

        self.assertEqual(metadata["file_name"], "native-comfyui-upload-save-prompt.json")
        self.assertFalse(metadata["route_level_smoke"])
        self.assertFalse(metadata["default_prompt_file"])
        self.assertEqual(metadata["node_count"], 2)
        self.assertEqual(metadata["class_type_count"], 2)
        self.assertEqual(metadata["class_types"], ["LoadImage", "SaveImage"])
        self.assertEqual(native_comfyui_live.prompt_load_image_filenames(payload), ["b1-native-comfyui-acceptance-input.png"])

    def test_prompt_load_image_filename_validation_rejects_paths(self) -> None:
        for filename in ("../secret.png", "nested/input.png", "nested\\input.png", "bad.txt", " input.png"):
            with self.subTest(filename=filename):
                payload = {"prompt": {"1": {"class_type": "LoadImage", "inputs": {"image": filename}}}}

                with self.assertRaises(AssertionError):
                    native_comfyui_live.prompt_load_image_filenames(payload)

    def test_multipart_form_data_builds_upload_image_body_without_credentials(self) -> None:
        body, content_type = native_comfyui_live.multipart_form_data(
            {"type": "input", "overwrite": "true"},
            {"image": ("upload.png", "image/png", native_comfyui_live.TINY_PNG_BYTES)},
        )

        self.assertTrue(content_type.startswith("multipart/form-data; boundary=b1-comfyui-"))
        self.assertIn(b'name="type"', body)
        self.assertIn(b"input", body)
        self.assertIn(b'name="image"; filename="upload.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(native_comfyui_live.TINY_PNG_BYTES, body)
        self.assertNotIn(b"Authorization", body)
        self.assertNotIn(b"Bearer", body)

    def test_multipart_form_data_builds_upload_mask_body_with_original_ref_without_credentials(self) -> None:
        original_ref = '{"filename":"upload.png","subfolder":"","type":"input"}'
        body, content_type = native_comfyui_live.multipart_form_data(
            {"type": "input", "overwrite": "true", "original_ref": original_ref},
            {"image": ("mask.png", "image/png", native_comfyui_live.TINY_PNG_BYTES)},
        )

        self.assertTrue(content_type.startswith("multipart/form-data; boundary=b1-comfyui-"))
        self.assertIn(b'name="original_ref"', body)
        self.assertIn(original_ref.encode("utf-8"), body)
        self.assertIn(b'name="image"; filename="mask.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertNotIn(b"Authorization", body)
        self.assertNotIn(b"Bearer", body)

    def test_view_artifact_check_uses_native_view_route(self) -> None:
        harness = native_comfyui_live.NativeComfyUiCompatibilityTests(methodName="test_native_rest_websocket_prompt_history_and_metadata")
        harness.checks = {}
        harness.samples = []
        calls: list[tuple[str, str]] = []

        def fake_request_bytes(method: str, path: str, timeout: float | None = None) -> tuple[bytes, dict[str, str], int]:
            calls.append((method, path))
            return b"image-bytes", {"content-type": "image/png"}, 200

        harness.request_bytes = fake_request_bytes  # type: ignore[method-assign]
        history = {
            "prompt_native_1": {
                "outputs": {
                    "7": {
                        "images": [
                            {"filename": "output.png", "subfolder": "acceptance", "type": "output"},
                        ]
                    }
                }
            }
        }

        harness.verify_view_artifact("prompt_native_1", history)

        self.assertEqual(calls, [("GET", "/view?filename=output.png&subfolder=acceptance&type=output")])
        self.assertEqual(harness.checks["view_artifact_accessible"]["status"], "ok")
        self.assertEqual(harness.samples[-1]["label"], "view-artifact")

    def test_durable_job_check_uses_b1_media_job_and_artifact_routes(self) -> None:
        harness = native_comfyui_live.NativeComfyUiCompatibilityTests(methodName="test_native_rest_websocket_prompt_history_and_metadata")
        harness.checks = {}
        harness.samples = []
        harness.timeout_seconds = 1
        json_calls: list[tuple[str, str]] = []
        byte_calls: list[tuple[str, str]] = []
        artifact_body = b"artifact-bytes"
        artifact_record = {
            "url": "/artifacts/comfyui/prompt_native_1/output.png",
            "source": "artifact_store",
            "mime_type": "image/png",
            "bytes": len(artifact_body),
            "sha256": hashlib.sha256(artifact_body).hexdigest(),
        }

        def fake_request_api_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
            json_calls.append((method, path))
            if path.startswith("/v1/media/jobs?"):
                return [
                    {
                        "id": "job_native_1",
                        "native_prompt_id": "prompt_native_1",
                        "runtime": "comfyui",
                        "model_alias": "comfyui-native",
                        "operation": "comfyui-prompt",
                        "state": "completed",
                        "stage": "completed",
                        "progress": 100,
                        "native_comfyui": {
                            "native_prompt_id": "prompt_native_1",
                            "native_prompt_recorded": True,
                            "prompt": {"node_count": 1, "class_type_count": 1, "body_hash_present": True, "client_id_present": True},
                            "artifacts": {"stored_artifact_count": 1, "failed_ingest_count": 0},
                        },
                        "artifacts": [artifact_record],
                    }
                ]
            if path == "/v1/media/jobs/job_native_1/artifacts":
                return {"job_id": "job_native_1", "artifacts": [artifact_record]}
            raise AssertionError(f"unexpected API JSON path {path}")

        def fake_request_api_bytes(method: str, path_or_url: str, timeout: float | None = None) -> tuple[bytes, dict[str, str], int]:
            byte_calls.append((method, path_or_url))
            return (
                artifact_body,
                {
                    "content-type": "image/png",
                    "content-length": str(len(artifact_body)),
                    "etag": '"artifact-etag"',
                    "accept-ranges": "bytes",
                },
                200,
            )

        harness.request_api_json = fake_request_api_json  # type: ignore[method-assign]
        harness.request_api_bytes = fake_request_api_bytes  # type: ignore[method-assign]

        harness.verify_durable_job_and_artifacts("prompt_native_1")

        self.assertEqual(json_calls[0][0], "GET")
        self.assertIn("native_prompt_id=prompt_native_1", json_calls[0][1])
        self.assertEqual(json_calls[1], ("GET", "/v1/media/jobs/job_native_1/artifacts"))
        self.assertEqual(byte_calls, [("GET", "/artifacts/comfyui/prompt_native_1/output.png")])
        self.assertEqual(harness.checks["native_summary_observable"]["node_count"], 1)
        self.assertTrue(harness.checks["native_summary_observable"]["body_hash_present"])
        self.assertEqual(harness.checks["durable_job_observable"]["job_id"], "job_native_1")
        self.assertEqual(harness.checks["durable_artifacts_observable"]["byte_count"], 14)
        self.assertEqual(harness.checks["durable_artifacts_observable"]["artifact_count"], 1)
        self.assertEqual(harness.checks["durable_artifacts_observable"]["verified_artifact_count"], 1)
        self.assertEqual(
            harness.checks["durable_artifacts_observable"]["artifact_proofs"][0]["download_sha256"],
            hashlib.sha256(artifact_body).hexdigest(),
        )
        self.assertEqual(harness.samples[-1]["label"], "durable-job-artifact")


class FakeRequest:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        method: str = "POST",
        compatibility: str | None = "comfyui-native",
        authorization: str | None = "Bearer test",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.method = method
        self._body = json.dumps(payload).encode("utf-8")
        request_headers = {"content-type": "application/json"}
        if compatibility is not None:
            request_headers["x-b1-compatibility"] = compatibility
        if authorization is not None:
            request_headers["authorization"] = authorization
        if headers:
            request_headers.update(headers)
        self.headers = request_headers
        self.url = SimpleNamespace(query="")

    async def body(self) -> bytes:
        return self._body


class FakeArtifactRequest:
    method = "GET"

    def __init__(self) -> None:
        self.headers = {"range": "bytes=0-10"}
        self.url = SimpleNamespace(query="")

    async def body(self) -> bytes:
        return b""


class FakeWebSocket:
    def __init__(
        self,
        *,
        query: str = "clientId=client-1",
        headers: dict[str, str] | None = None,
        receive_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.url = SimpleNamespace(query=query)
        self.headers = headers or {
            "host": "comfy.ai.b1.germering",
            "authorization": "Bearer secret",
            "cookie": "b1_session=browser-secret",
            "forwarded": "for=203.0.113.8",
            "x-b1-csrf": "csrf-secret",
            "x-b1-compatibility": "comfyui-native",
            "x-forwarded-for": "203.0.113.8",
            "x-real-ip": "203.0.113.8",
            "sec-websocket-key": "browser-key",
            "upgrade": "websocket",
            "user-agent": "native-client",
            "x-b1-test": "forwarded",
        }
        self.receive_messages = list(receive_messages or [])
        self.accepted = False
        self.closed: list[int] = []
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, Any]:
        if self.receive_messages:
            return self.receive_messages.pop(0)
        await asyncio.sleep(30)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_text(self, message: str) -> None:
        self.sent_text.append(message)

    async def send_bytes(self, message: bytes) -> None:
        self.sent_bytes.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


class FakeUpstream:
    def __init__(self, incoming: list[str | bytes] | None = None, *, block_when_empty: bool = True) -> None:
        self.incoming = list(incoming or [])
        self.block_when_empty = block_when_empty
        self.sent: list[str | bytes] = []
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str | bytes:
        if self.incoming:
            return self.incoming.pop(0)
        if not self.block_when_empty:
            raise StopAsyncIteration
        while not self.closed:
            await asyncio.sleep(0.01)
        raise StopAsyncIteration


class FakeConnect:
    def __init__(self, upstream: FakeUpstream) -> None:
        self.upstream = upstream

    async def __aenter__(self) -> FakeUpstream:
        return self.upstream

    async def __aexit__(self, *_: Any) -> bool:
        return False


class FakeStreamResponse:
    status_code = 200

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class FakeStreamContext:
    def __init__(self, response: FakeStreamResponse) -> None:
        self.response = response

    async def __aenter__(self) -> FakeStreamResponse:
        return self.response

    async def __aexit__(self, *_: Any) -> bool:
        return False


class FakeAsyncClient:
    requested: list[tuple[str, str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.response = FakeStreamResponse([b"image-", b"bytes"])

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def stream(self, method: str, url: str) -> FakeStreamContext:
        self.requested.append((method, url))
        return FakeStreamContext(self.response)


class FakeDatabase:
    def __init__(self, *, lease_acquired: bool = True) -> None:
        self.lease_acquired = lease_acquired
        self.jobs: dict[str, dict[str, Any]] = {}
        self.updates: list[dict[str, Any]] = []
        self.leases: list[dict[str, Any]] = []
        self.releases: list[str] = []
        self.reservation_gates: list[dict[str, Any]] = []

    async def get_job_by_idempotency_key(self, owner: str, idempotency_key: str) -> dict[str, Any] | None:
        for row in self.jobs.values():
            if row.get("owner_id") == owner and row.get("idempotency_key") == idempotency_key:
                return dict(row)
        return None

    async def count_jobs(
        self,
        *,
        owner_id: str | None = None,
        states: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
        created_after: Any | None = None,
    ) -> int:
        return 0

    async def insert_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {"state": "created", "stage": "created", "progress": 0, "native_prompt_id": None, **payload}
        self.jobs[row["id"]] = row
        return dict(row)

    async def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        self.jobs[job_id].update(changes)
        self.updates.append({"job_id": job_id, **changes})
        return dict(self.jobs[job_id])

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.jobs.get(job_id)
        return dict(row) if row else None

    async def get_job_by_native_prompt_id(self, prompt_id: str) -> dict[str, Any] | None:
        for row in self.jobs.values():
            if row.get("native_prompt_id") == prompt_id:
                return dict(row)
        return None

    async def get_job_by_artifact_url(self, artifact_url: str) -> dict[str, Any] | None:
        for row in self.jobs.values():
            if any(artifact.get("url") == artifact_url for artifact in row.get("artifacts", [])):
                return dict(row)
        return None

    async def list_jobs(self, limit: int = 50, **filters: Any) -> list[dict[str, Any]]:
        rows = list(self.jobs.values())
        for key, value in filters.items():
            if value is not None:
                rows = [row for row in rows if row.get(key) == value]
        return [dict(row) for row in rows[:limit]]

    async def list_resumable_comfyui_native_jobs(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.jobs.values()
            if row.get("runtime") == "comfyui"
            and row.get("model_alias") == "comfyui-native"
            and isinstance(row.get("native_prompt_id"), str)
            and row.get("native_prompt_id")
            and row.get("state") in {"running", "saving", "cancelling"}
        ]
        return [dict(row) for row in rows[:limit]]

    async def request_job_cancel(self, job_id: str) -> dict[str, Any] | None:
        row = self.jobs.get(job_id)
        if row is None:
            return None
        if row["state"] in main.TERMINAL_JOB_STATES:
            return dict(row)
        if row["state"] in {"created", "validated", "queued", "waiting_for_gpu"}:
            row.update({"state": "cancelled", "stage": "cancelled", "progress": 100})
        else:
            row.update({"state": "cancelling", "stage": "cancelling"})
        self.updates.append({"job_id": job_id, "state": row["state"], "stage": row["stage"]})
        return dict(row)

    async def acquire_scheduler_owner(self, owner: str, ttl_seconds: int) -> dict[str, Any]:
        row = {"owner": owner, "ttl_seconds": ttl_seconds, "acquired": self.lease_acquired}
        self.leases.append(row)
        return row

    async def release_scheduler_owner(self, owner: str) -> dict[str, Any]:
        self.releases.append(owner)
        return {"owner": owner, "released": True}

    async def runtime_reservation_gate(
        self,
        owner_id: str,
        runtime: str,
        resolved_model_version: str,
        gpu_runtimes: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    ) -> dict[str, Any]:
        row = {
            "owner_id": owner_id,
            "runtime": runtime,
            "resolved_model_version": resolved_model_version,
            "gpu_runtimes": sorted(gpu_runtimes),
            "allowed": True,
        }
        self.reservation_gates.append(row)
        return row


class FakeRuntimeControlRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.jobs: list[dict[str, Any]] = []
        self.idle: list[dict[str, Any]] = []
        self.fail_on: str | None = None
        self.results: dict[str, dict[str, Any]] = {}

    async def _record(self, action: str, job: dict[str, Any]) -> None:
        self.calls.append(action)
        self.jobs.append(dict(job))
        if self.fail_on == action:
            raise RuntimeError(f"{action} failed")

    async def unload_other_gpu_runtimes(self, job: dict[str, Any]) -> list[dict[str, Any] | None]:
        await self._record("unload_other_gpu_runtimes", job)
        return [{"status": "ok"}]

    async def verify_vram_or_recover(self, job: dict[str, Any]) -> None:
        await self._record("verify_vram_or_recover", job)

    async def load_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
        await self._record("load_runtime_model", job)
        return self.results.get("load_runtime_model", {"status": "ok"})

    async def warm_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
        await self._record("warm_runtime_model", job)
        return self.results.get("warm_runtime_model", {"status": "ok"})

    async def record_runtime_idle_for_job(self, job: dict[str, Any], details: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append("record_runtime_idle_for_job")
        record = {"job": dict(job), "details": details or {}}
        self.idle.append(record)
        return record


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ComfyUiCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_database = main.database
        self.original_settings = main.settings
        self.original_authenticate = main.authenticate
        self.original_proxy = main.proxy_http_bytes
        self.original_websocket_connect = main.websocket_connect
        self.original_schedule = main.schedule_comfyui_prompt_tracker
        self.original_ingest = main.ingest_comfyui_artifacts
        self.original_fetch_history = main.fetch_comfyui_history
        self.original_history = main.comfyui_history_contains_prompt
        self.original_runtime_control_runner = main.runtime_control_runner
        self.original_approved_node_pins = main.approved_node_pins
        main.settings = replace(
            main.settings,
            comfyui_prompt_wait_timeout_seconds=0,
            comfyui_prompt_lease_ttl_seconds=60,
            comfyui_prompt_poll_seconds=1,
            comfyui_prompt_completion_timeout_seconds=30,
            comfyui_prompt_idle_grace_seconds=0,
        )
        async def authenticate(_: str | None = None) -> Any:
            return main.AuthContext("client_1", main.Role.SERVICE, frozenset({"jobs:read", "jobs:write"}))

        main.authenticate = authenticate  # type: ignore[assignment]

    def tearDown(self) -> None:
        main.database = self.original_database
        main.settings = self.original_settings
        main.authenticate = self.original_authenticate
        main.proxy_http_bytes = self.original_proxy
        main.websocket_connect = self.original_websocket_connect
        main.schedule_comfyui_prompt_tracker = self.original_schedule
        main.ingest_comfyui_artifacts = self.original_ingest
        main.fetch_comfyui_history = self.original_fetch_history
        main.comfyui_history_contains_prompt = self.original_history
        main.runtime_control_runner = self.original_runtime_control_runner
        main.approved_node_pins = self.original_approved_node_pins

    def test_resume_comfyui_native_prompt_trackers_restarts_persisted_native_jobs(self) -> None:
        fake = FakeDatabase()
        fake.jobs["job_1"] = {
            "id": "job_1",
            "runtime": "comfyui",
            "model_alias": "comfyui-native",
            "state": "running",
            "native_prompt_id": "prompt_native_1",
        }
        fake.jobs["job_2"] = {
            "id": "job_2",
            "runtime": "comfyui",
            "model_alias": "comfyui-native",
            "state": "cancelling",
            "native_prompt_id": "prompt_native_2",
        }
        fake.jobs["job_3"] = {
            "id": "job_3",
            "runtime": "comfyui",
            "model_alias": "image-default",
            "state": "running",
            "native_prompt_id": "prompt_native_3",
        }
        fake.jobs["job_4"] = {
            "id": "job_4",
            "runtime": "comfyui",
            "model_alias": "comfyui-native",
            "state": "completed",
            "native_prompt_id": "prompt_native_4",
        }
        main.database = fake
        scheduled: list[dict[str, str]] = []

        def schedule(job_id: str, prompt_id: str, lease_owner: str) -> None:
            scheduled.append({"job_id": job_id, "prompt_id": prompt_id, "lease_owner": lease_owner})

        main.schedule_comfyui_prompt_tracker = schedule  # type: ignore[assignment]

        result = asyncio.run(main.resume_comfyui_native_prompt_trackers())

        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["resumed"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["resumed_job_ids"], ["job_1", "job_2"])
        self.assertEqual(result["native_prompt_ids"], ["prompt_native_1", "prompt_native_2"])
        self.assertEqual(
            scheduled,
            [
                {"job_id": "job_1", "prompt_id": "prompt_native_1", "lease_owner": "comfyui-prompt-job_1"},
                {"job_id": "job_2", "prompt_id": "prompt_native_2", "lease_owner": "comfyui-prompt-job_2"},
            ],
        )

    def test_stock_load_save_prompt_is_model_free_native_but_not_route_smoke(self) -> None:
        payload = {
            "prompt": {
                "1": {"class_type": "LoadImage", "inputs": {"image": "b1-native-comfyui-acceptance-input.png"}},
                "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "b1-native-comfyui-upload-save", "images": ["1", 0]}},
            }
        }

        kind = main.comfyui_native_prompt_model_free_kind(payload)
        resolution = main.native_comfyui_prompt_resolution(model_free_kind=kind)

        self.assertEqual(kind, "model-free-stock-io")
        self.assertFalse(main.comfyui_native_prompt_is_model_free_smoke(payload))
        self.assertFalse(resolution.requires_gpu)
        self.assertEqual(resolution.resolved_model_version, "comfyui-native-workflow@model-free-stock-io")
        self.assertIsNone(main.comfyui_native_prompt_model_free_kind({"prompt": {"1": {"class_type": "KSampler", "inputs": {}}}}))

    def test_interrupted_gpu_sweep_exempts_resumable_native_comfyui_prompts(self) -> None:
        source = (ROOT / "services" / "control-plane" / "app" / "database.py").read_text(encoding="utf-8")

        self.assertIn("COMFYUI_NATIVE_RESUMABLE_STATES", source)
        self.assertIn("list_resumable_comfyui_native_jobs", source)
        self.assertIn("~resumable_comfyui_native", source)

    def test_prompt_acquires_lease_forwards_native_body_and_records_prompt_id(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]
        scheduled: list[dict[str, str]] = []
        forwarded: dict[str, Any] = {}

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            forwarded.update({"base_url": base_url, "path": path, "body": body})
            return Response(
                content=b'{"prompt_id":"prompt_native_1","number":0,"node_errors":{}}',
                media_type="application/json",
                status_code=200,
            )

        def schedule(job_id: str, prompt_id: str, lease_owner: str) -> None:
            scheduled.append({"job_id": job_id, "prompt_id": prompt_id, "lease_owner": lease_owner})

        main.proxy_http_bytes = proxy  # type: ignore[assignment]
        main.schedule_comfyui_prompt_tracker = schedule  # type: ignore[assignment]
        request = FakeRequest(
            {
                "client_id": "client-1",
                "prompt": {
                    "1": {
                        "class_type": "CheckpointLoaderSimple",
                        "inputs": {"text": "private native prompt text", "ckpt_name": "private-model.safetensors"},
                    }
                },
            }
        )

        response = asyncio.run(main.comfy_prompt(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(forwarded["path"], "/prompt")
        self.assertEqual(forwarded["body"], awaitable_body(request))
        self.assertEqual(len(fake.leases), 1)
        self.assertEqual(fake.releases, [])
        job = next(iter(fake.jobs.values()))
        self.assertEqual(job["owner_id"], "client_1")
        self.assertEqual(job["runtime"], "comfyui")
        self.assertEqual(job["native_prompt_id"], "prompt_native_1")
        request_input = job["request_params"]["input"]
        self.assertEqual(request_input["client_id"], "client-1")
        self.assertEqual(request_input["native_prompt_hash"], hashlib.sha256(awaitable_body(request)).hexdigest())
        self.assertEqual(request_input["prompt_summary"]["node_count"], 1)
        self.assertEqual(request_input["prompt_summary"]["class_type_count"], 1)
        self.assertEqual(request_input["prompt_summary"]["malformed_node_count"], 0)
        self.assertNotIn("private native prompt text", json.dumps(job["request_params"]))
        self.assertNotIn("private-model.safetensors", json.dumps(job["request_params"]))
        self.assertEqual(
            runner.calls,
            ["unload_other_gpu_runtimes", "verify_vram_or_recover", "load_runtime_model", "warm_runtime_model"],
        )
        self.assertTrue(all(call_job["runtime"] == "comfyui" for call_job in runner.jobs))
        self.assertTrue(all(call_job["model_alias"] == "comfyui-native" for call_job in runner.jobs))
        self.assertTrue(all(call_job["resolved_model_version"] == "comfyui-native-workflow@native" for call_job in runner.jobs))
        states = [update["state"] for update in fake.updates if "state" in update]
        self.assertEqual(
            states,
            [
                "validated",
                "queued",
                "waiting_for_gpu",
                "unloading",
                "verifying_vram",
                "loading",
                "warming",
                "running",
            ],
        )
        self.assertEqual(scheduled[0]["prompt_id"], "prompt_native_1")

    def test_prompt_rejection_after_prepare_marks_runtime_idle_and_releases_lease(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            return Response(
                content=b'{"error":"validation failed"}',
                media_type="application/json",
                status_code=400,
            )

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        response = asyncio.run(main.comfy_prompt(FakeRequest({"client_id": "client-1", "prompt": {"1": {"class_type": "MissingNode"}}})))

        job = next(iter(fake.jobs.values()))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["stage"], "comfyui_prompt_rejected")
        self.assertEqual(job["failure_category"], "comfyui_validation_error")
        self.assertEqual(fake.releases, [f"comfyui-prompt-{job['id']}"])
        self.assertEqual(
            runner.calls,
            [
                "unload_other_gpu_runtimes",
                "verify_vram_or_recover",
                "load_runtime_model",
                "warm_runtime_model",
                "record_runtime_idle_for_job",
            ],
        )
        self.assertEqual(runner.idle[0]["job"]["runtime"], "comfyui")
        self.assertEqual(runner.idle[0]["job"]["model_alias"], "comfyui-native")
        self.assertEqual(runner.idle[0]["details"]["source"], "comfyui_native_compatibility")
        self.assertEqual(runner.idle[0]["details"]["prompt_id"], "unsubmitted")
        self.assertEqual(runner.idle[0]["details"]["last_state"], "failed")

    def test_prompt_idempotency_replay_returns_existing_native_prompt_without_forwarding(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        request = FakeRequest({"client_id": "client-1", "prompt": {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}})
        body = awaitable_body(request)
        payload = main.comfyui_native_job_payload("client-1", hashlib.sha256(body).hexdigest(), main.comfyui_native_prompt_audit_summary(json.loads(body)))
        fake.jobs["job_existing"] = {
            "id": "job_existing",
            "owner_id": "client_1",
            "idempotency_key": "prompt_1",
            "modality": "workflow",
            "operation": "comfyui-prompt",
            "model_alias": "comfyui-native",
            "priority": "single_image",
            "runtime": "comfyui",
            "resolved_model_version": "comfyui-native-workflow@native",
            "request_params": payload.model_dump(),
            "native_prompt_id": "prompt_native_1",
            "state": "running",
            "stage": "comfyui_prompt_submitted",
            "progress": 70,
        }

        async def fail_proxy(*_: Any, **__: Any) -> Response:
            raise AssertionError("idempotent prompt replay must not forward to ComfyUI")

        main.proxy_http_bytes = fail_proxy  # type: ignore[assignment]
        response = asyncio.run(main.comfy_prompt(request, idempotency_key="prompt_1"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-b1-idempotent-replay"], "true")
        self.assertEqual(response.headers["x-b1-job-id"], "job_existing")
        self.assertEqual(json.loads(response.body), {"prompt_id": "prompt_native_1", "node_errors": {}})
        self.assertEqual(fake.leases, [])
        self.assertEqual(len(fake.jobs), 1)

    def test_prompt_idempotency_replay_rejects_different_native_body_without_forwarding(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        original_request = FakeRequest({"client_id": "client-1", "prompt": {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}})
        original_body = awaitable_body(original_request)
        payload = main.comfyui_native_job_payload(
            "client-1",
            hashlib.sha256(original_body).hexdigest(),
            main.comfyui_native_prompt_audit_summary(json.loads(original_body)),
        )
        fake.jobs["job_existing"] = {
            "id": "job_existing",
            "owner_id": "client_1",
            "idempotency_key": "prompt_1",
            "modality": "workflow",
            "operation": "comfyui-prompt",
            "model_alias": "comfyui-native",
            "priority": "single_image",
            "runtime": "comfyui",
            "resolved_model_version": "comfyui-native-workflow@native",
            "request_params": payload.model_dump(),
            "native_prompt_id": "prompt_native_1",
            "state": "running",
            "stage": "comfyui_prompt_submitted",
            "progress": 70,
        }

        async def fail_proxy(*_: Any, **__: Any) -> Response:
            raise AssertionError("idempotency conflict must not forward to ComfyUI")

        main.proxy_http_bytes = fail_proxy  # type: ignore[assignment]
        replay_request = FakeRequest({"client_id": "client-1", "prompt": {"1": {"class_type": "KSampler", "inputs": {"seed": 2}}}})
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.comfy_prompt(replay_request, idempotency_key="prompt_1"))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "idempotency_key_conflict")
        self.assertIn("request_params", caught.exception.detail["mismatched_fields"])
        self.assertEqual(fake.leases, [])

    def test_prompt_idempotency_replay_pending_native_prompt_fails_closed(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        request = FakeRequest({"client_id": "client-1", "prompt": {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}})
        body = awaitable_body(request)
        payload = main.comfyui_native_job_payload("client-1", hashlib.sha256(body).hexdigest(), main.comfyui_native_prompt_audit_summary(json.loads(body)))
        fake.jobs["job_existing"] = {
            "id": "job_existing",
            "owner_id": "client_1",
            "idempotency_key": "prompt_1",
            "modality": "workflow",
            "operation": "comfyui-prompt",
            "model_alias": "comfyui-native",
            "priority": "single_image",
            "runtime": "comfyui",
            "resolved_model_version": "comfyui-native-workflow@native",
            "request_params": payload.model_dump(),
            "native_prompt_id": None,
            "state": "waiting_for_gpu",
            "stage": "waiting_for_gpu",
            "progress": 20,
        }

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.comfy_prompt(request, idempotency_key="prompt_1"))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "comfyui_prompt_replay_pending")
        self.assertEqual(caught.exception.headers["Retry-After"], "1")
        self.assertEqual(fake.leases, [])

    def test_prompt_requires_gateway_compatibility_header_before_job_creation(self) -> None:
        fake = FakeDatabase()
        main.database = fake

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.comfy_prompt(FakeRequest({"prompt": {}}, compatibility=None)))

        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(fake.jobs, {})

    def test_prompt_rejects_unauthenticated_normal_compatibility_before_job_creation(self) -> None:
        fake = FakeDatabase()
        main.database = fake

        async def authenticate(_: str | None = None) -> Any:
            raise main.HTTPException(status_code=401, detail="missing bearer token or browser session")

        main.authenticate = authenticate  # type: ignore[assignment]

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.comfy_prompt(FakeRequest({"prompt": {}}, authorization=None)))

        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(fake.jobs, {})

    def test_prompt_rejects_production_hardware_policy_before_job_creation(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        main.settings = replace(
            main.settings,
            runtime_deployment_mode="production",
            gpu_total_vram_gib=12.0,
            gpu_reserve_vram_gib=1.5,
            host_total_ram_gib=32.0,
            host_reserve_ram_gib=6.0,
        )
        original_runtime_agent_get = main.runtime_agent_get

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any], str | None]:
            self.assertEqual(path, "/v1/metrics")
            return (
                {
                    "gpu": {
                        "available": True,
                        "devices": [
                            {
                                "name": "NVIDIA GeForce RTX 3060 Laptop GPU",
                                "memory_total_mib": 6144,
                                "memory_free_mib": 4096,
                            }
                        ],
                    },
                    "memory": {
                        "total_bytes": 31 * 1024**3,
                        "available_bytes": 8 * 1024**3,
                    },
                },
                None,
            )

        async def fail_proxy(*_: Any, **__: Any) -> Response:
            raise AssertionError("hardware admission failure must not forward to ComfyUI")

        main.runtime_agent_get = runtime_agent_get  # type: ignore[assignment]
        main.proxy_http_bytes = fail_proxy  # type: ignore[assignment]
        request = FakeRequest({"client_id": "client-1", "prompt": {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}})

        try:
            with self.assertRaises(main.HTTPException) as caught:
                asyncio.run(main.comfy_prompt(request))
        finally:
            main.runtime_agent_get = original_runtime_agent_get

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "hardware_resource_policy")
        check = caught.exception.detail["hardware_resource_policy"]
        self.assertEqual(check["name"], "hardware:resource-policy")
        self.assertEqual(check["status"], "failed")
        self.assertIn("largest GPU VRAM is 6144 MiB", check["detail"])
        self.assertEqual(fake.jobs, {})
        self.assertEqual(fake.leases, [])

    def test_model_free_smoke_prompt_bypasses_hardware_admission_but_keeps_scheduler_lease(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]
        main.settings = replace(
            main.settings,
            runtime_deployment_mode="production",
            gpu_total_vram_gib=12.0,
            gpu_reserve_vram_gib=1.5,
            host_total_ram_gib=32.0,
            host_reserve_ram_gib=6.0,
        )
        original_runtime_agent_get = main.runtime_agent_get
        runtime_agent_calls: list[str] = []
        forwarded: dict[str, Any] = {}
        scheduled: list[dict[str, str]] = []

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any], str | None]:
            runtime_agent_calls.append(path)
            return (
                {
                    "gpu": {
                        "available": True,
                        "devices": [
                            {
                                "name": "NVIDIA GeForce RTX 3060 Laptop GPU",
                                "memory_total_mib": 6144,
                                "memory_free_mib": 4096,
                            }
                        ],
                    },
                    "memory": {
                        "total_bytes": 31 * 1024**3,
                        "available_bytes": 8 * 1024**3,
                    },
                },
                None,
            )

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            forwarded.update({"base_url": base_url, "path": path, "body": body})
            return Response(
                content=b'{"prompt_id":"prompt_native_smoke","number":0,"node_errors":{}}',
                media_type="application/json",
                status_code=200,
            )

        def schedule(job_id: str, prompt_id: str, lease_owner: str) -> None:
            scheduled.append({"job_id": job_id, "prompt_id": prompt_id, "lease_owner": lease_owner})

        main.runtime_agent_get = runtime_agent_get  # type: ignore[assignment]
        main.proxy_http_bytes = proxy  # type: ignore[assignment]
        main.schedule_comfyui_prompt_tracker = schedule  # type: ignore[assignment]
        request = FakeRequest(
            {
                "client_id": "client-1",
                "prompt": {
                    "1": {"class_type": "B1RuntimeTinyImage", "inputs": {"width": 64, "height": 64}},
                    "2": {
                        "class_type": "SaveImage",
                        "inputs": {"filename_prefix": "b1-native-comfyui-smoke", "images": ["1", 0]},
                    },
                },
            }
        )

        try:
            response = asyncio.run(main.comfy_prompt(request))
        finally:
            main.runtime_agent_get = original_runtime_agent_get

        self.assertEqual(response.status_code, 200)
        self.assertEqual(forwarded["path"], "/prompt")
        self.assertEqual(forwarded["body"], awaitable_body(request))
        self.assertEqual(runtime_agent_calls, [])
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(fake.leases), 1)
        self.assertEqual(fake.releases, [])
        job = next(iter(fake.jobs.values()))
        self.assertEqual(job["runtime"], "comfyui")
        self.assertEqual(job["model_alias"], "comfyui-native")
        self.assertEqual(job["resolved_model_version"], "comfyui-native-workflow@model-free-smoke")
        self.assertEqual(job["native_prompt_id"], "prompt_native_smoke")
        request_input = job["request_params"]["input"]
        self.assertTrue(request_input["model_free_smoke"])
        self.assertTrue(request_input["prompt_summary"]["model_free_smoke"])
        self.assertEqual(request_input["prompt_summary"]["class_type_count"], 2)
        self.assertEqual(
            request_input["prompt_summary"]["class_type_digest"],
            hashlib.sha256(b"B1RuntimeTinyImage\nSaveImage").hexdigest(),
        )
        states = [update["state"] for update in fake.updates if "state" in update]
        self.assertEqual(states, ["validated", "queued", "waiting_for_gpu", "running"])
        self.assertEqual(scheduled[0]["prompt_id"], "prompt_native_smoke")
        self.assertEqual(scheduled[0]["lease_owner"], f"comfyui-prompt-{job['id']}")

    def test_model_free_stock_io_prompt_bypasses_hardware_admission_but_keeps_scheduler_lease(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]
        main.settings = replace(
            main.settings,
            runtime_deployment_mode="production",
            gpu_total_vram_gib=12.0,
            gpu_reserve_vram_gib=1.5,
            host_total_ram_gib=32.0,
            host_reserve_ram_gib=6.0,
        )
        original_runtime_agent_get = main.runtime_agent_get
        runtime_agent_calls: list[str] = []
        forwarded: dict[str, Any] = {}
        scheduled: list[dict[str, str]] = []

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any], str | None]:
            runtime_agent_calls.append(path)
            return (
                {
                    "gpu": {
                        "available": True,
                        "devices": [
                            {
                                "name": "NVIDIA GeForce RTX 3060 Laptop GPU",
                                "memory_total_mib": 6144,
                                "memory_free_mib": 4096,
                            }
                        ],
                    },
                    "memory": {
                        "total_bytes": 31 * 1024**3,
                        "available_bytes": 8 * 1024**3,
                    },
                },
                None,
            )

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            forwarded.update({"base_url": base_url, "path": path, "body": body})
            return Response(
                content=b'{"prompt_id":"prompt_native_stock_io","number":0,"node_errors":{}}',
                media_type="application/json",
                status_code=200,
            )

        def schedule(job_id: str, prompt_id: str, lease_owner: str) -> None:
            scheduled.append({"job_id": job_id, "prompt_id": prompt_id, "lease_owner": lease_owner})

        main.runtime_agent_get = runtime_agent_get  # type: ignore[assignment]
        main.proxy_http_bytes = proxy  # type: ignore[assignment]
        main.schedule_comfyui_prompt_tracker = schedule  # type: ignore[assignment]
        request = FakeRequest(
            {
                "client_id": "client-1",
                "prompt": {
                    "1": {"class_type": "LoadImage", "inputs": {"image": "b1-native-comfyui-acceptance-input.png"}},
                    "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "b1-native-comfyui-upload-save", "images": ["1", 0]}},
                },
            }
        )

        try:
            response = asyncio.run(main.comfy_prompt(request))
        finally:
            main.runtime_agent_get = original_runtime_agent_get

        self.assertEqual(response.status_code, 200)
        self.assertEqual(forwarded["path"], "/prompt")
        self.assertEqual(forwarded["body"], awaitable_body(request))
        self.assertEqual(runtime_agent_calls, [])
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(fake.leases), 1)
        self.assertEqual(fake.releases, [])
        job = next(iter(fake.jobs.values()))
        self.assertEqual(job["runtime"], "comfyui")
        self.assertEqual(job["model_alias"], "comfyui-native")
        self.assertEqual(job["resolved_model_version"], "comfyui-native-workflow@model-free-stock-io")
        self.assertEqual(job["native_prompt_id"], "prompt_native_stock_io")
        request_input = job["request_params"]["input"]
        self.assertTrue(request_input["model_free_native"])
        self.assertEqual(request_input["model_free_kind"], "model-free-stock-io")
        self.assertNotIn("model_free_smoke", request_input)
        self.assertTrue(request_input["prompt_summary"]["model_free_native"])
        self.assertEqual(request_input["prompt_summary"]["model_free_kind"], "model-free-stock-io")
        self.assertNotIn("model_free_smoke", request_input["prompt_summary"])
        states = [update["state"] for update in fake.updates if "state" in update]
        self.assertEqual(states, ["validated", "queued", "waiting_for_gpu", "running"])
        self.assertEqual(scheduled[0]["prompt_id"], "prompt_native_stock_io")
        self.assertEqual(scheduled[0]["lease_owner"], f"comfyui-prompt-{job['id']}")

    def test_prompt_prepare_failure_fails_without_forwarding_to_comfyui(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        runner = FakeRuntimeControlRunner()
        runner.fail_on = "verify_vram_or_recover"
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]
        forwarded = False

        async def proxy(*_: Any, **__: Any) -> Response:
            nonlocal forwarded
            forwarded = True
            return Response(content=b"{}", media_type="application/json", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.comfy_prompt(FakeRequest({"prompt": {}})))

        self.assertEqual(caught.exception.status_code, 503)
        self.assertFalse(forwarded)
        self.assertEqual(runner.calls, ["unload_other_gpu_runtimes", "verify_vram_or_recover"])
        job = next(iter(fake.jobs.values()))
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["stage"], "comfyui_runtime_prepare_failed")
        self.assertEqual(job["failure_category"], "runtime_prepare_failed")
        self.assertEqual(fake.releases, [f"comfyui-prompt-{job['id']}"])

    def test_prompt_hook_failure_returns_classified_prepare_error(self) -> None:
        fake = FakeDatabase()
        main.database = fake

        class HookFailureRunner(FakeRuntimeControlRunner):
            async def load_runtime_model(self, job: dict[str, Any]) -> dict[str, Any]:
                await self._record("load_runtime_model", job)
                raise main.RuntimePreparationError("comfyui runtime load hook reported failed: model_missing")

        runner = HookFailureRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]
        forwarded = False

        async def proxy(*_: Any, **__: Any) -> Response:
            nonlocal forwarded
            forwarded = True
            return Response(content=b"{}", media_type="application/json", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.comfy_prompt(FakeRequest({"prompt": {}})))

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["type"], "runtime_prepare_failed")
        self.assertEqual(caught.exception.detail["runtime"], "comfyui")
        self.assertEqual(caught.exception.detail["public_model"], "comfyui-native")
        self.assertIn("model_missing", caught.exception.detail["message"])
        self.assertFalse(forwarded)
        self.assertEqual(runner.calls, ["unload_other_gpu_runtimes", "verify_vram_or_recover", "load_runtime_model"])
        job = next(iter(fake.jobs.values()))
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["stage"], "comfyui_runtime_prepare_failed")
        self.assertEqual(job["failure_category"], "runtime_prepare_failed")
        self.assertEqual(fake.releases, [f"comfyui-prompt-{job['id']}"])

    def test_prompt_timeout_fails_without_forwarding(self) -> None:
        fake = FakeDatabase(lease_acquired=False)
        main.database = fake
        forwarded = False

        async def proxy(*_: Any, **__: Any) -> Response:
            nonlocal forwarded
            forwarded = True
            return Response(content=b"{}", media_type="application/json", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.comfy_prompt(FakeRequest({"prompt": {}})))

        self.assertEqual(caught.exception.status_code, 503)
        self.assertFalse(forwarded)
        job = next(iter(fake.jobs.values()))
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["failure_category"], "scheduler_unavailable")

    def test_tracker_marks_completed_and_releases_lease_after_history(self) -> None:
        fake = FakeDatabase()
        fake.jobs["job_1"] = {
            "id": "job_1",
            "state": "running",
            "runtime": "comfyui",
            "model_alias": "comfyui-native",
            "resolved_model_version": "comfyui-native-workflow@native",
            "artifacts": [],
            "native_prompt_id": "prompt_native_1",
        }
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]

        async def fetch_history(_: str) -> dict[str, Any]:
            return {
                "prompt_native_1": {
                    "status": {"completed": True},
                    "outputs": {
                        "save_image": {
                            "images": [
                                {"filename": "result.png", "subfolder": "b1", "type": "output"},
                            ]
                        }
                    },
                }
            }

        main.fetch_comfyui_history = fetch_history  # type: ignore[assignment]

        async def ingest(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    **artifact,
                    "source": "artifact_store",
                    "storage": "artifact-server",
                    "bytes": 11,
                    "sha256": "a" * 64,
                    "ingest_status": "stored",
                }
                for artifact in artifacts
            ]

        main.ingest_comfyui_artifacts = ingest  # type: ignore[assignment]

        asyncio.run(main.track_comfyui_prompt_completion("job_1", "prompt_native_1", "lease-owner"))

        states = [update["state"] for update in fake.updates if "state" in update]
        self.assertEqual(states, ["saving", "completed"])
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["url"], "/artifacts/comfyui/prompt_native_1/0-result.png")
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["source"], "artifact_store")
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["bytes"], 11)
        self.assertIsInstance(fake.jobs["job_1"]["run_time_ms"], int)
        self.assertEqual(fake.releases, ["lease-owner"])
        self.assertGreaterEqual(len(fake.leases), 1)
        self.assertEqual(runner.calls, ["record_runtime_idle_for_job"])
        self.assertEqual(runner.idle[0]["job"]["runtime"], "comfyui")
        self.assertEqual(runner.idle[0]["job"]["model_alias"], "comfyui-native")
        self.assertEqual(runner.idle[0]["details"]["source"], "comfyui_native_compatibility")
        self.assertEqual(runner.idle[0]["details"]["last_state"], "completed")

    def test_tracker_marks_history_without_media_outputs_recovery_required(self) -> None:
        fake = FakeDatabase()
        fake.jobs["job_1"] = {
            "id": "job_1",
            "state": "running",
            "runtime": "comfyui",
            "model_alias": "comfyui-native",
            "resolved_model_version": "comfyui-native-workflow@native",
            "artifacts": [],
            "native_prompt_id": "prompt_native_1",
        }
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]
        ingest_called = False

        async def fetch_history(_: str) -> dict[str, Any]:
            return {"prompt_native_1": {"status": {"completed": True}, "outputs": {"noop": {}}}}

        async def ingest(_: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal ingest_called
            ingest_called = True
            return []

        main.fetch_comfyui_history = fetch_history  # type: ignore[assignment]
        main.ingest_comfyui_artifacts = ingest  # type: ignore[assignment]

        asyncio.run(main.track_comfyui_prompt_completion("job_1", "prompt_native_1", "lease-owner"))

        self.assertFalse(ingest_called)
        self.assertEqual(fake.jobs["job_1"]["state"], "recovery_required")
        self.assertEqual(fake.jobs["job_1"]["stage"], "comfyui_no_media_artifacts")
        self.assertEqual(fake.jobs["job_1"]["failure_category"], "comfyui_no_media_artifacts")
        self.assertEqual(fake.jobs["job_1"]["artifacts"], [])
        self.assertIn("completed without image, video, GIF, or audio outputs", fake.jobs["job_1"]["failure_message"])
        self.assertIsInstance(fake.jobs["job_1"]["run_time_ms"], int)
        self.assertEqual(fake.releases, ["lease-owner"])
        self.assertGreaterEqual(len(fake.leases), 1)
        self.assertEqual(runner.calls, ["record_runtime_idle_for_job"])
        self.assertEqual(runner.idle[0]["job"]["runtime"], "comfyui")
        self.assertEqual(runner.idle[0]["details"]["source"], "comfyui_native_compatibility")
        self.assertEqual(runner.idle[0]["details"]["last_state"], "recovery_required")

    def test_tracker_marks_terminal_native_job_idle_before_releasing_lease(self) -> None:
        fake = FakeDatabase()
        fake.jobs["job_1"] = {
            "id": "job_1",
            "state": "failed",
            "runtime": "comfyui",
            "model_alias": "comfyui-native",
            "resolved_model_version": "comfyui-native-workflow@native",
            "artifacts": [],
            "native_prompt_id": "prompt_native_1",
        }
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]

        asyncio.run(main.track_comfyui_prompt_completion("job_1", "prompt_native_1", "lease-owner"))

        self.assertEqual(fake.releases, ["lease-owner"])
        self.assertEqual(runner.calls, ["record_runtime_idle_for_job"])
        self.assertEqual(runner.idle[0]["job"]["runtime"], "comfyui")
        self.assertEqual(runner.idle[0]["details"]["source"], "comfyui_native_compatibility")
        self.assertEqual(runner.idle[0]["details"]["last_state"], "failed")

    def test_websocket_bridge_filters_sensitive_headers_and_preserves_query(self) -> None:
        websocket = FakeWebSocket(receive_messages=[{"type": "websocket.disconnect", "code": 1000}])
        upstream = FakeUpstream()
        connected: dict[str, Any] = {}

        def connect(url: str, **kwargs: Any) -> FakeConnect:
            connected.update({"url": url, **kwargs})
            return FakeConnect(upstream)

        main.websocket_connect = connect  # type: ignore[assignment]

        asyncio.run(main.bridge_comfyui_websocket(websocket))

        self.assertTrue(websocket.accepted)
        self.assertEqual(connected["url"], "ws://comfyui:8000/ws?clientId=client-1")
        self.assertEqual(connected["additional_headers"]["user-agent"], "native-client")
        self.assertEqual(connected["additional_headers"]["x-b1-test"], "forwarded")
        forwarded_headers = {key.lower() for key in connected["additional_headers"]}
        self.assertNotIn("authorization", forwarded_headers)
        self.assertNotIn("cookie", forwarded_headers)
        self.assertNotIn("forwarded", forwarded_headers)
        self.assertNotIn("upgrade", forwarded_headers)
        self.assertNotIn("sec-websocket-key", forwarded_headers)
        self.assertNotIn("x-b1-compatibility", forwarded_headers)
        self.assertNotIn("x-b1-csrf", forwarded_headers)
        self.assertNotIn("x-forwarded-for", forwarded_headers)
        self.assertNotIn("x-real-ip", forwarded_headers)

    def test_websocket_bridge_forwards_browser_text_and_binary_to_runtime(self) -> None:
        websocket = FakeWebSocket(
            receive_messages=[
                {"type": "websocket.receive", "text": "hello"},
                {"type": "websocket.receive", "bytes": b"preview-request"},
                {"type": "websocket.disconnect", "code": 1000},
            ]
        )
        upstream = FakeUpstream()

        def connect(url: str, **_: Any) -> FakeConnect:
            return FakeConnect(upstream)

        main.websocket_connect = connect  # type: ignore[assignment]

        asyncio.run(main.bridge_comfyui_websocket(websocket))

        self.assertTrue(websocket.accepted)
        self.assertEqual(upstream.sent, ["hello", b"preview-request"])
        self.assertTrue(upstream.closed)

    def test_websocket_bridge_forwards_runtime_text_and_binary_to_browser(self) -> None:
        websocket = FakeWebSocket(receive_messages=[])
        upstream = FakeUpstream(['{"type":"status"}', b"\x00\x01"], block_when_empty=False)

        def connect(url: str, **_: Any) -> FakeConnect:
            return FakeConnect(upstream)

        main.websocket_connect = connect  # type: ignore[assignment]

        asyncio.run(main.bridge_comfyui_websocket(websocket))

        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.sent_text, ['{"type":"status"}'])
        self.assertEqual(websocket.sent_bytes, [b"\x00\x01"])
        self.assertIn(1000, websocket.closed)

    def test_voicebox_websocket_bridge_preserves_path_query_and_filters_headers(self) -> None:
        websocket = FakeWebSocket(
            query="session=abc",
            headers={
                "host": "voice.ai.b1.germering",
                "authorization": "Bearer secret",
                "cookie": "b1_session=browser-secret",
                "forwarded": "for=203.0.113.8",
                "x-b1-csrf": "csrf-secret",
                "x-b1-compatibility": "voicebox-native",
                "x-forwarded-for": "203.0.113.8",
                "x-real-ip": "203.0.113.8",
                "sec-websocket-key": "browser-key",
                "upgrade": "websocket",
                "user-agent": "voicebox-client",
                "x-b1-test": "forwarded",
            },
            receive_messages=[{"type": "websocket.disconnect", "code": 1000}],
        )
        upstream = FakeUpstream([b"voicebox-event"], block_when_empty=False)
        connected: dict[str, Any] = {}
        main.settings = replace(main.settings, voicebox_url="http://voicebox:17493")

        def connect(url: str, **kwargs: Any) -> FakeConnect:
            connected.update({"url": url, **kwargs})
            return FakeConnect(upstream)

        main.websocket_connect = connect  # type: ignore[assignment]

        asyncio.run(main.bridge_voicebox_websocket(websocket, "/api/ws"))

        self.assertTrue(websocket.accepted)
        self.assertEqual(connected["url"], "ws://voicebox:17493/api/ws?session=abc")
        self.assertEqual(connected["additional_headers"]["user-agent"], "voicebox-client")
        self.assertEqual(connected["additional_headers"]["x-b1-test"], "forwarded")
        forwarded_headers = {key.lower() for key in connected["additional_headers"]}
        self.assertNotIn("authorization", forwarded_headers)
        self.assertNotIn("cookie", forwarded_headers)
        self.assertNotIn("forwarded", forwarded_headers)
        self.assertNotIn("upgrade", forwarded_headers)
        self.assertNotIn("sec-websocket-key", forwarded_headers)
        self.assertNotIn("x-b1-compatibility", forwarded_headers)
        self.assertNotIn("x-b1-csrf", forwarded_headers)
        self.assertNotIn("x-forwarded-for", forwarded_headers)
        self.assertNotIn("x-real-ip", forwarded_headers)
        self.assertEqual(websocket.sent_bytes, [b"voicebox-event"])

    def test_voicebox_native_speech_passthrough_uses_scheduler_lease_and_preserves_body(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]
        main.settings = replace(main.settings, voicebox_url="http://voicebox:17493", sync_inference_lease_ttl_seconds=90)
        forwarded: dict[str, Any] = {}

        async def proxy(
            base_url: str,
            path: str,
            request: FakeRequest,
            body: bytes | None = None,
            timeout_seconds: float = 120.0,
        ) -> Response:
            forwarded.update(
                {
                    "base_url": base_url,
                    "path": path,
                    "method": request.method,
                    "body": body,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return Response(content=b"voicebox-audio", media_type="audio/wav", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]
        payload = {
            "model": "upstream-native-model",
            "input": "private native speech text",
            "voice": "native-voice",
        }

        response = asyncio.run(
            main.compatibility_passthrough(
                "v1/audio/speech",
                FakeRequest(payload, method="POST", compatibility="voicebox-native"),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"voicebox-audio")
        self.assertEqual(fake.reservation_gates[0]["owner_id"], "client_1")
        self.assertEqual(fake.reservation_gates[0]["runtime"], "voicebox")
        self.assertEqual(fake.reservation_gates[0]["resolved_model_version"], "voicebox-native-speech@native")
        self.assertEqual(len(fake.leases), 1)
        self.assertTrue(fake.leases[0]["owner"].startswith("sync-voicebox-native-speech-"))
        self.assertEqual(fake.leases[0]["ttl_seconds"], 90)
        self.assertEqual(fake.releases, [fake.leases[0]["owner"]])
        self.assertEqual(
            runner.calls,
            [
                "unload_other_gpu_runtimes",
                "verify_vram_or_recover",
                "load_runtime_model",
                "warm_runtime_model",
                "record_runtime_idle_for_job",
            ],
        )
        first_job = runner.jobs[0]
        self.assertEqual(first_job["runtime"], "voicebox")
        self.assertEqual(first_job["model_alias"], "voicebox-native")
        self.assertEqual(first_job["resolved_model_version"], "voicebox-native-speech@native")
        self.assertEqual(first_job["operation"], "voicebox-native-speech")
        self.assertEqual(first_job["modality"], "tts")
        self.assertEqual(forwarded["base_url"], "http://voicebox:17493")
        self.assertEqual(forwarded["path"], "v1/audio/speech")
        self.assertEqual(forwarded["method"], "POST")
        self.assertEqual(forwarded["timeout_seconds"], 90.0)
        self.assertEqual(json.loads(forwarded["body"].decode("utf-8")), payload)

    def test_voicebox_native_read_passthrough_does_not_take_gpu_lease(self) -> None:
        fake = FakeDatabase()
        main.database = fake
        runner = FakeRuntimeControlRunner()
        main.runtime_control_runner = lambda lease_ttl_seconds=None: runner  # type: ignore[assignment]
        main.settings = replace(main.settings, voicebox_url="http://voicebox:17493")
        proxied: list[dict[str, Any]] = []

        async def proxy(
            base_url: str,
            path: str,
            request: FakeRequest,
            body: bytes | None = None,
            timeout_seconds: float = 120.0,
        ) -> Response:
            proxied.append({"base_url": base_url, "path": path, "method": request.method})
            return Response(content=b'{"ok":true}', media_type="application/json", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        response = asyncio.run(
            main.compatibility_passthrough(
                "b1/runtime/build-info",
                FakeRequest({}, method="GET", compatibility="voicebox-native"),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxied, [{"base_url": "http://voicebox:17493", "path": "b1/runtime/build-info", "method": "GET"}])
        self.assertEqual(fake.reservation_gates, [])
        self.assertEqual(fake.leases, [])
        self.assertEqual(fake.releases, [])
        self.assertEqual(runner.calls, [])

    def test_websocket_events_update_job_progress_and_artifacts(self) -> None:
        fake = FakeDatabase()
        fake.jobs["job_1"] = {
            "id": "job_1",
            "state": "running",
            "native_prompt_id": "prompt_native_1",
            "artifacts": [],
        }
        main.database = fake
        ingested_calls: list[list[dict[str, Any]]] = []

        async def ingest(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
            ingested_calls.append(artifacts)
            return [
                {
                    **artifact,
                    "source": "artifact_store",
                    "storage": "artifact-server",
                    "bytes": 10,
                    "sha256": "b" * 64,
                    "ingest_status": "stored",
                }
                for artifact in artifacts
            ]

        main.ingest_comfyui_artifacts = ingest  # type: ignore[assignment]

        asyncio.run(
            main.persist_comfyui_ws_event(
                json.dumps({"type": "progress", "data": {"prompt_id": "prompt_native_1", "value": 5, "max": 10}})
            )
        )
        self.assertEqual(fake.jobs["job_1"]["stage"], "comfyui_progress")
        self.assertEqual(fake.jobs["job_1"]["progress"], 79)

        asyncio.run(
            main.persist_comfyui_ws_event(
                json.dumps(
                    {
                        "type": "executed",
                        "data": {
                            "prompt_id": "prompt_native_1",
                            "node": "save",
                            "output": {"images": [{"filename": "live.png", "subfolder": "", "type": "output"}]},
                        },
                    }
                )
            )
        )
        self.assertEqual(fake.jobs["job_1"]["stage"], "comfyui_node_executed")
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["url"], "/artifacts/comfyui/prompt_native_1/0-live.png")
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["source"], "artifact_store")
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["bytes"], 10)
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["sha256"], "b" * 64)
        self.assertEqual(ingested_calls[0][0]["source"], "comfyui_view")

    def test_live_artifact_ingest_replaces_existing_view_reference(self) -> None:
        fake = FakeDatabase()
        existing = {
            "url": "/artifacts/comfyui/prompt_native_1/0-live.png",
            "path": "comfyui/prompt_native_1/0-live.png",
            "source": "comfyui_view",
            "ingest_status": "failed",
            "comfyui": {"filename": "live.png", "subfolder": "", "type": "output"},
        }
        fake.jobs["job_1"] = {
            "id": "job_1",
            "state": "running",
            "native_prompt_id": "prompt_native_1",
            "artifacts": [existing],
        }
        main.database = fake

        async def ingest(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    **artifact,
                    "source": "artifact_store",
                    "storage": "artifact-server",
                    "bytes": 10,
                    "sha256": "c" * 64,
                    "ingest_status": "stored",
                }
                for artifact in artifacts
            ]

        main.ingest_comfyui_artifacts = ingest  # type: ignore[assignment]

        asyncio.run(
            main.persist_comfyui_ws_event(
                json.dumps(
                    {
                        "type": "executed",
                        "data": {
                            "prompt_id": "prompt_native_1",
                            "node": "save",
                            "output": {"images": [{"filename": "live.png", "subfolder": "", "type": "output"}]},
                        },
                    }
                )
            )
        )

        self.assertEqual(len(fake.jobs["job_1"]["artifacts"]), 1)
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["source"], "artifact_store")
        self.assertEqual(fake.jobs["job_1"]["artifacts"][0]["sha256"], "c" * 64)

    def test_websocket_execution_error_marks_job_failed(self) -> None:
        fake = FakeDatabase()
        fake.jobs["job_1"] = {"id": "job_1", "state": "running", "native_prompt_id": "prompt_native_1", "artifacts": []}
        main.database = fake

        asyncio.run(
            main.persist_comfyui_ws_event(
                json.dumps(
                    {
                        "type": "execution_error",
                        "data": {
                            "prompt_id": "prompt_native_1",
                            "exception_type": "RuntimeError",
                            "exception_message": "model dependency missing",
                        },
                    }
                )
            )
        )

        self.assertEqual(fake.jobs["job_1"]["state"], "failed")
        self.assertEqual(fake.jobs["job_1"]["failure_category"], "comfyui_execution_error")
        self.assertEqual(fake.jobs["job_1"]["failure_message"], "model dependency missing")

    def test_comfyui_passthrough_allows_native_read_and_core_mutating_routes(self) -> None:
        proxied: list[dict[str, Any]] = []

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            proxied.append({"base_url": base_url, "path": path, "method": request.method})
            return Response(content=b'{"ok":true}', media_type="application/json", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        read_response = asyncio.run(main.proxy_comfyui_compatibility("models/checkpoints", FakeRequest({}, method="GET")))
        upload_response = asyncio.run(main.proxy_comfyui_compatibility("upload/image", FakeRequest({}, method="POST")))
        user_data_response = asyncio.run(main.proxy_comfyui_compatibility("api/userdata/workflows/example.json", FakeRequest({}, method="POST")))
        encoded_read_response = asyncio.run(main.proxy_comfyui_compatibility("models/%63heckpoints", FakeRequest({}, method="GET")))

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(upload_response.status_code, 200)
        self.assertEqual(user_data_response.status_code, 200)
        self.assertEqual(encoded_read_response.status_code, 200)
        self.assertEqual(
            [item["path"] for item in proxied],
            ["models/checkpoints", "upload/image", "api/userdata/workflows/example.json", "models/checkpoints"],
        )

    def test_comfyui_catchall_registers_options_for_native_preflight_passthrough(self) -> None:
        matching_routes = [route for route in main.app.routes if getattr(route, "path", None) == "/{path:path}"]
        method_sets = [set(getattr(route, "methods", set()) or set()) for route in matching_routes]

        self.assertTrue(any("OPTIONS" in methods for methods in method_sets), method_sets)

    def test_compatibility_passthrough_requires_gateway_header_and_auth(self) -> None:
        async def proxy(*_: Any, **__: Any) -> Response:
            raise AssertionError("unauthorized or non-compatibility requests must not be proxied")

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        with self.assertRaises(main.HTTPException) as missing_header:
            asyncio.run(main.compatibility_passthrough("object_info", FakeRequest({}, method="GET", compatibility=None)))
        self.assertEqual(missing_header.exception.status_code, 404)

        async def authenticate(_: str | None = None) -> Any:
            raise main.HTTPException(status_code=401, detail="missing bearer token or browser session")

        main.authenticate = authenticate  # type: ignore[assignment]

        with self.assertRaises(main.HTTPException) as unauthenticated:
            asyncio.run(main.compatibility_passthrough("object_info", FakeRequest({}, method="GET", authorization=None)))
        self.assertEqual(unauthenticated.exception.status_code, 401)

    def test_legacy_comfy_passthrough_allows_cidr_checked_listener_without_auth(self) -> None:
        proxied: list[dict[str, Any]] = []

        async def authenticate(_: str | None = None) -> Any:
            raise AssertionError("legacy ComfyUI listener uses the gateway CIDR allowlist instead of bearer auth")

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            proxied.append({"base_url": base_url, "path": path, "method": request.method})
            return Response(content=b'{"ok":true}', media_type="application/json", status_code=200)

        main.authenticate = authenticate  # type: ignore[assignment]
        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        response = asyncio.run(
            main.compatibility_passthrough(
                "object_info",
                FakeRequest({}, method="GET", compatibility="comfyui-legacy-8188", authorization=None),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxied[0]["path"], "object_info")

    def test_legacy_comfy_websocket_allows_core_ws_without_auth_and_strips_gateway_marker(self) -> None:
        async def authenticate(_: str | None = None) -> Any:
            raise AssertionError("legacy ComfyUI WebSocket access uses the gateway CIDR allowlist instead of bearer auth")

        upstream = FakeUpstream()
        connected: dict[str, Any] = {}

        def connect(url: str, **kwargs: Any) -> FakeConnect:
            connected.update({"url": url, **kwargs})
            return FakeConnect(upstream)

        main.authenticate = authenticate  # type: ignore[assignment]
        main.websocket_connect = connect  # type: ignore[assignment]
        websocket = FakeWebSocket(
            headers={
                "host": "legacy-client",
                "cookie": "b1_session=browser-secret",
                "forwarded": "for=203.0.113.8",
                "x-b1-compatibility": "comfyui-legacy-8188",
                "x-b1-csrf": "csrf-secret",
                "x-forwarded-for": "203.0.113.8",
                "x-real-ip": "203.0.113.8",
                "sec-websocket-key": "legacy-browser-key",
                "upgrade": "websocket",
                "user-agent": "legacy-comfy-client",
                "x-b1-test": "forwarded",
            },
            receive_messages=[{"type": "websocket.disconnect", "code": 1000}],
        )

        asyncio.run(main.native_ws(websocket))

        self.assertTrue(websocket.accepted)
        self.assertEqual(connected["url"], "ws://comfyui:8000/ws?clientId=client-1")
        self.assertEqual(connected["additional_headers"]["user-agent"], "legacy-comfy-client")
        self.assertEqual(connected["additional_headers"]["x-b1-test"], "forwarded")
        forwarded_headers = {key.lower() for key in connected["additional_headers"]}
        self.assertNotIn("authorization", forwarded_headers)
        self.assertNotIn("cookie", forwarded_headers)
        self.assertNotIn("forwarded", forwarded_headers)
        self.assertNotIn("upgrade", forwarded_headers)
        self.assertNotIn("sec-websocket-key", forwarded_headers)
        self.assertNotIn("x-b1-compatibility", forwarded_headers)
        self.assertNotIn("x-b1-csrf", forwarded_headers)
        self.assertNotIn("x-forwarded-for", forwarded_headers)
        self.assertNotIn("x-real-ip", forwarded_headers)

    def test_legacy_comfy_websocket_still_blocks_management_routes_without_auth(self) -> None:
        async def authenticate(_: str | None = None) -> Any:
            raise AssertionError("blocked legacy ComfyUI WebSocket routes must not require bearer auth")

        def connect(*_: Any, **__: Any) -> FakeConnect:
            raise AssertionError("blocked legacy ComfyUI WebSocket route must not be proxied")

        main.authenticate = authenticate  # type: ignore[assignment]
        main.websocket_connect = connect  # type: ignore[assignment]
        websocket = FakeWebSocket(headers={"x-b1-compatibility": "comfyui-legacy-8188"})

        asyncio.run(main.compatibility_ws("manager/ws", websocket))

        self.assertFalse(websocket.accepted)
        self.assertEqual(websocket.closed, [1008])

    def test_normal_comfy_websocket_rejects_missing_auth(self) -> None:
        async def authenticate(_: str | None = None) -> Any:
            raise main.HTTPException(status_code=401, detail="missing bearer token or browser session")

        main.authenticate = authenticate  # type: ignore[assignment]
        websocket = FakeWebSocket(headers={"x-b1-compatibility": "comfyui-native"})

        asyncio.run(main.native_ws(websocket))

        self.assertFalse(websocket.accepted)
        self.assertEqual(websocket.closed, [1008])

    def test_comfyui_websocket_catchall_blocks_management_routes_before_proxying(self) -> None:
        def connect(*_: Any, **__: Any) -> FakeConnect:
            raise AssertionError("blocked ComfyUI WebSocket route must not be proxied")

        main.websocket_connect = connect  # type: ignore[assignment]

        for path in ("manager/ws", "customnode/events", "api/customnode/ws", "b1/runtime/ws"):
            with self.subTest(path=path):
                websocket = FakeWebSocket(headers={"x-b1-compatibility": "comfyui-native", "authorization": "Bearer test"})

                asyncio.run(main.compatibility_ws(path, websocket))

                self.assertFalse(websocket.accepted)
                self.assertEqual(websocket.closed, [1008])

    def test_comfyui_custom_websocket_requires_write_scope_trusted_prefix_and_pin(self) -> None:
        async def read_only_authenticate(_: str | None = None) -> Any:
            return main.AuthContext("client_1", main.Role.SERVICE, frozenset({"jobs:read"}))

        main.authenticate = read_only_authenticate  # type: ignore[assignment]
        main.settings = replace(main.settings, comfyui_trusted_route_prefixes=("trusted/custom",))
        main.approved_node_pins = {}
        websocket = FakeWebSocket(headers={"x-b1-compatibility": "comfyui-native", "authorization": "Bearer test"})

        asyncio.run(main.compatibility_ws("trusted/custom/ws", websocket))

        self.assertFalse(websocket.accepted)
        self.assertEqual(websocket.closed, [1008])

        main.approved_node_pins = {
            ("comfyui-custom-ws", "a" * 40): main.ApprovedNodePin(
                id="comfyui-custom-ws",
                repository_url="https://github.com/example/comfyui-custom-ws",
                commit="a" * 40,
                allowed_route_prefixes=["trusted/custom"],
            )
        }
        websocket = FakeWebSocket(headers={"x-b1-compatibility": "comfyui-native", "authorization": "Bearer test"})

        asyncio.run(main.compatibility_ws("trusted/custom/ws", websocket))

        self.assertFalse(websocket.accepted)
        self.assertEqual(websocket.closed, [1008])

        async def read_write_authenticate(_: str | None = None) -> Any:
            return main.AuthContext("client_1", main.Role.SERVICE, frozenset({"jobs:read", "jobs:write"}))

        main.authenticate = read_write_authenticate  # type: ignore[assignment]
        websocket = FakeWebSocket(headers={"x-b1-compatibility": "comfyui-native", "authorization": "Bearer test"})

        asyncio.run(main.compatibility_ws("trusted/custom/ws", websocket))

        self.assertFalse(websocket.accepted)
        self.assertEqual(websocket.closed, [1008])

        main.approved_node_pins = {
            ("comfyui-custom-ws", "a" * 40): main.ApprovedNodePin(
                id="comfyui-custom-ws",
                repository_url="https://github.com/example/comfyui-custom-ws",
                commit="a" * 40,
                dependency_lock_sha256="b" * 64,
                allowed_route_prefixes=["trusted/custom"],
            )
        }
        connected: dict[str, Any] = {}
        upstream = FakeUpstream(['{"type":"custom_status"}'], block_when_empty=False)

        def connect(url: str, **kwargs: Any) -> FakeConnect:
            connected.update({"url": url, **kwargs})
            return FakeConnect(upstream)

        main.websocket_connect = connect  # type: ignore[assignment]
        websocket = FakeWebSocket(
            query="clientId=client-1",
            headers={
                "x-b1-compatibility": "comfyui-native",
                "authorization": "Bearer test",
                "user-agent": "custom-node-client",
            },
        )

        asyncio.run(main.compatibility_ws("trusted/custom/ws", websocket))

        self.assertTrue(websocket.accepted)
        self.assertEqual(connected["url"], "ws://comfyui:8000/trusted/custom/ws?clientId=client-1")
        self.assertEqual(websocket.sent_text, ['{"type":"custom_status"}'])

    def test_comfyui_passthrough_blocks_internal_and_custom_node_management_routes(self) -> None:
        async def proxy(*_: Any, **__: Any) -> Response:
            raise AssertionError("blocked ComfyUI route must not be proxied")

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        for path in ("b1", "b1/runtime/unload", "manager/queue/start", "customnode/install", "api/customnode/update"):
            with self.subTest(path=path):
                with self.assertRaises(main.HTTPException) as raised:
                    asyncio.run(main.proxy_comfyui_compatibility(path, FakeRequest({}, method="POST")))
                self.assertEqual(raised.exception.status_code, 403)
                self.assertEqual(raised.exception.detail["code"], "comfyui_route_denied")

    def test_comfyui_passthrough_blocks_encoded_path_control_before_proxying(self) -> None:
        async def proxy(*_: Any, **__: Any) -> Response:
            raise AssertionError("blocked ComfyUI route must not be proxied")

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        for path in (
            "models/%2e%2e/system_stats",
            "models/%2Fsecret",
            "models/%5csecret",
            "models/%00secret",
            "models/%3fsecret",
            "models/%23secret",
            "models/%",
            "models/%2",
            "models/%zz",
        ):
            with self.subTest(path=path):
                with self.assertRaises(main.HTTPException) as raised:
                    asyncio.run(main.proxy_comfyui_compatibility(path, FakeRequest({}, method="GET")))
                self.assertEqual(raised.exception.status_code, 403)
                self.assertEqual(raised.exception.detail["code"], "comfyui_route_denied")

    def test_comfyui_websocket_blocks_encoded_path_control_before_proxying(self) -> None:
        def connect(*_: Any, **__: Any) -> FakeConnect:
            raise AssertionError("blocked ComfyUI WebSocket route must not be proxied")

        main.websocket_connect = connect  # type: ignore[assignment]

        for path in ("trusted/%2e%2e/ws", "trusted/%2fws", "trusted/%5cws", "trusted/%00ws", "trusted/%/ws", "trusted/%zz/ws"):
            with self.subTest(path=path):
                websocket = FakeWebSocket(headers={"x-b1-compatibility": "comfyui-native", "authorization": "Bearer test"})

                asyncio.run(main.compatibility_ws(path, websocket))

                self.assertFalse(websocket.accepted)
                self.assertEqual(websocket.closed, [1008])

    def test_comfyui_passthrough_blocks_unknown_mutation_unless_prefix_is_configured_and_pinned(self) -> None:
        proxied: list[str] = []

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            proxied.append(path)
            return Response(content=b'{"ok":true}', media_type="application/json", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        with self.assertRaises(main.HTTPException) as raised:
            asyncio.run(main.proxy_comfyui_compatibility("trusted/custom/render", FakeRequest({}, method="POST")))
        self.assertEqual(raised.exception.status_code, 403)

        main.settings = replace(main.settings, comfyui_trusted_route_prefixes=("trusted/custom",))
        main.approved_node_pins = {}
        with self.assertRaises(main.HTTPException) as unpinned:
            asyncio.run(main.proxy_comfyui_compatibility("trusted/custom/render", FakeRequest({}, method="POST")))
        self.assertEqual(unpinned.exception.status_code, 403)

        main.approved_node_pins = {
            ("comfyui-custom-api", "a" * 40): main.ApprovedNodePin(
                id="comfyui-custom-api",
                repository_url="https://github.com/example/comfyui-custom-api",
                commit="a" * 40,
                allowed_route_prefixes=["trusted/custom"],
            )
        }
        with self.assertRaises(main.HTTPException) as unlocked:
            asyncio.run(main.proxy_comfyui_compatibility("trusted/custom/render", FakeRequest({}, method="POST")))
        self.assertEqual(unlocked.exception.status_code, 403)

        main.approved_node_pins = {
            ("comfyui-custom-api", "a" * 40): main.ApprovedNodePin(
                id="comfyui-custom-api",
                repository_url="https://github.com/example/comfyui-custom-api",
                commit="a" * 40,
                dependency_lock_sha256="b" * 64,
                allowed_route_prefixes=["trusted/custom"],
            )
        }
        response = asyncio.run(main.proxy_comfyui_compatibility("trusted/%63ustom/render", FakeRequest({}, method="POST")))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxied, ["trusted/custom/render"])

    def test_queue_delete_forwards_normalized_native_request_and_marks_matching_job_cancelling(self) -> None:
        fake = FakeDatabase()
        fake.jobs["job_1"] = {"id": "job_1", "state": "running", "runtime": "comfyui", "native_prompt_id": "prompt_native_1", "artifacts": []}
        fake.jobs["job_2"] = {"id": "job_2", "state": "running", "runtime": "comfyui", "native_prompt_id": "prompt_native_2", "artifacts": []}
        fake.jobs["job_3"] = {"id": "job_3", "state": "running", "runtime": "localai", "native_prompt_id": "prompt_native_1", "artifacts": []}
        main.database = fake
        proxied: dict[str, Any] = {}

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            proxied.update({"base_url": base_url, "path": path, "body": body, "method": request.method})
            return Response(content=b'{"ok":true}', media_type="application/json", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]
        request = FakeRequest({"delete": ["prompt_native_1"]}, method="POST")

        response = asyncio.run(main.proxy_comfyui_compatibility("que%75e", request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxied["base_url"], "http://comfyui:8000")
        self.assertEqual(proxied["path"], "queue")
        self.assertEqual(proxied["body"], awaitable_body(request))
        self.assertEqual(fake.jobs["job_1"]["state"], "cancelling")
        self.assertEqual(fake.jobs["job_2"]["state"], "running")
        self.assertEqual(fake.jobs["job_3"]["state"], "running")

    def test_interrupt_forwards_first_and_marks_all_active_native_jobs_cancelling(self) -> None:
        fake = FakeDatabase()
        fake.jobs["job_1"] = {"id": "job_1", "state": "running", "runtime": "comfyui", "native_prompt_id": "prompt_native_1", "artifacts": []}
        fake.jobs["job_2"] = {"id": "job_2", "state": "waiting_for_gpu", "runtime": "comfyui", "native_prompt_id": "prompt_native_2", "artifacts": []}
        fake.jobs["job_3"] = {"id": "job_3", "state": "completed", "runtime": "comfyui", "native_prompt_id": "prompt_native_3", "artifacts": []}
        main.database = fake
        proxied: dict[str, Any] = {}

        async def proxy(base_url: str, path: str, request: FakeRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            proxied.update({"base_url": base_url, "path": path, "body": body, "method": request.method})
            self.assertEqual(fake.jobs["job_1"]["state"], "running")
            return Response(content=b'{"ok":true}', media_type="application/json", status_code=200)

        main.proxy_http_bytes = proxy  # type: ignore[assignment]
        request = FakeRequest({}, method="POST")

        response = asyncio.run(main.proxy_comfyui_compatibility("interrupt", request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proxied["path"], "interrupt")
        self.assertEqual(fake.jobs["job_1"]["state"], "cancelling")
        self.assertEqual(fake.jobs["job_2"]["state"], "cancelled")
        self.assertEqual(fake.jobs["job_3"]["state"], "completed")

    def test_ingest_comfyui_artifact_streams_view_bytes_to_artifact_store(self) -> None:
        original_client = main.httpx.AsyncClient
        FakeAsyncClient.requested = []
        main.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))
        with tempfile.TemporaryDirectory() as tmp:
            main.settings = replace(main.settings, artifact_root=tmp)
            artifact = {
                "url": "/artifacts/comfyui/prompt_native_1/0-result.png",
                "path": "comfyui/prompt_native_1/0-result.png",
                "source": "comfyui_view",
                "source_url": "/view?filename=result.png&subfolder=b1&type=output",
                "comfyui": {"filename": "result.png", "subfolder": "b1", "type": "output"},
            }

            [stored] = asyncio.run(main.ingest_comfyui_artifacts([artifact]))

            expected = b"image-bytes"
            self.assertEqual(stored["source"], "artifact_store")
            self.assertEqual(stored["storage"], "artifact-server")
            self.assertEqual(stored["bytes"], len(expected))
            self.assertEqual(stored["sha256"], hashlib.sha256(expected).hexdigest())
            self.assertEqual(stored["ingest_status"], "stored")
            self.assertEqual((Path(tmp) / "comfyui" / "prompt_native_1" / "0-result.png").read_bytes(), expected)
            self.assertEqual(
                FakeAsyncClient.requested,
                [("GET", "http://comfyui:8000/view?filename=result.png&subfolder=b1&type=output")],
            )

    def test_comfyui_artifact_download_falls_back_to_recorded_view_metadata(self) -> None:
        fake = FakeDatabase()
        artifact = {
            "url": "/artifacts/comfyui/prompt_native_1/0-result.png",
            "source": "comfyui_view",
            "comfyui": {"filename": "result.png", "subfolder": "b1", "type": "output"},
        }
        fake.jobs["job_1"] = {"id": "job_1", "owner_id": "client_1", "artifacts": [artifact]}
        main.database = fake
        proxied: dict[str, Any] = {}

        async def authenticate(_: str | None = None) -> Any:
            return main.AuthContext("client_1", main.Role.SERVICE, frozenset({"jobs:read"}))

        async def proxy(base_url: str, path: str, request: FakeArtifactRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            proxied.update({"base_url": base_url, "path": path, "body": body, "range": request.headers["range"]})
            return Response(content=b"image", media_type="image/png", status_code=206)

        main.authenticate = authenticate  # type: ignore[assignment]
        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        response = asyncio.run(main.artifact_download("comfyui/prompt_native_1/0-result.png", FakeArtifactRequest()))

        self.assertEqual(response.status_code, 206)
        self.assertEqual(proxied["path"], "/view?filename=result.png&subfolder=b1&type=output")
        self.assertEqual(proxied["body"], b"")
        self.assertEqual(proxied["range"], "bytes=0-10")

    def test_comfyui_artifact_download_prefers_artifact_store_after_ingestion(self) -> None:
        fake = FakeDatabase()
        artifact = {
            "url": "/artifacts/comfyui/prompt_native_1/0-result.png",
            "path": "comfyui/prompt_native_1/0-result.png",
            "source": "artifact_store",
            "storage": "artifact-server",
            "comfyui": {"filename": "result.png", "subfolder": "b1", "type": "output"},
        }
        fake.jobs["job_1"] = {"id": "job_1", "owner_id": "client_1", "artifacts": [artifact]}
        main.database = fake
        proxied: dict[str, Any] = {}

        async def authenticate(_: str | None = None) -> Any:
            return main.AuthContext("client_1", main.Role.SERVICE, frozenset({"jobs:read"}))

        async def proxy(
            base_url: str,
            path: str,
            request: FakeArtifactRequest,
            body: bytes | None = None,
            timeout_seconds: float = 120.0,
            extra_headers: dict[str, str] | None = None,
        ) -> Response:
            proxied.update({"base_url": base_url, "path": path, "body": body, "extra_headers": extra_headers})
            return Response(content=b"image", media_type="image/png", status_code=206)

        main.settings = replace(main.settings, artifact_server_token="service-token")
        main.authenticate = authenticate  # type: ignore[assignment]
        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        response = asyncio.run(main.artifact_download("comfyui/prompt_native_1/0-result.png", FakeArtifactRequest()))

        self.assertEqual(response.status_code, 206)
        self.assertEqual(proxied["base_url"], "http://artifact-server:8000")
        self.assertEqual(proxied["path"], "/artifacts/comfyui/prompt_native_1/0-result.png")
        self.assertIsNone(proxied["body"])
        self.assertEqual(proxied["extra_headers"], {"Authorization": "Bearer service-token"})


def awaitable_body(request: FakeRequest) -> bytes:
    return asyncio.run(request.body())


if __name__ == "__main__":
    unittest.main()
