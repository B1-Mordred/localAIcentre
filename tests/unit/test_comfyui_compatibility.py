from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import Any


from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

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


class FakeRequest:
    method = "POST"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"content-type": "application/json"}
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
            "sec-websocket-key": "browser-key",
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

    async def get_job_by_idempotency_key(self, owner: str, idempotency_key: str) -> dict[str, Any] | None:
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

    async def acquire_scheduler_owner(self, owner: str, ttl_seconds: int) -> dict[str, Any]:
        row = {"owner": owner, "ttl_seconds": ttl_seconds, "acquired": self.lease_acquired}
        self.leases.append(row)
        return row

    async def release_scheduler_owner(self, owner: str) -> dict[str, Any]:
        self.releases.append(owner)
        return {"owner": owner, "released": True}


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
        main.settings = replace(
            main.settings,
            comfyui_prompt_wait_timeout_seconds=0,
            comfyui_prompt_lease_ttl_seconds=60,
            comfyui_prompt_poll_seconds=1,
            comfyui_prompt_completion_timeout_seconds=30,
            comfyui_prompt_idle_grace_seconds=0,
        )

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
        request = FakeRequest({"client_id": "client-1", "prompt": {"1": {"class_type": "CheckpointLoaderSimple"}}})

        response = asyncio.run(main.comfy_prompt(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(forwarded["path"], "/prompt")
        self.assertEqual(forwarded["body"], awaitable_body(request))
        self.assertEqual(len(fake.leases), 1)
        self.assertEqual(fake.releases, [])
        job = next(iter(fake.jobs.values()))
        self.assertEqual(job["owner_id"], "comfy-client:client-1")
        self.assertEqual(job["runtime"], "comfyui")
        self.assertEqual(job["native_prompt_id"], "prompt_native_1")
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
        self.assertNotIn("authorization", {key.lower() for key in connected["additional_headers"]})
        self.assertNotIn("sec-websocket-key", {key.lower() for key in connected["additional_headers"]})

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
                "sec-websocket-key": "browser-key",
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
        self.assertNotIn("authorization", {key.lower() for key in connected["additional_headers"]})
        self.assertNotIn("sec-websocket-key", {key.lower() for key in connected["additional_headers"]})
        self.assertEqual(websocket.sent_bytes, [b"voicebox-event"])

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

        async def proxy(base_url: str, path: str, request: FakeArtifactRequest, body: bytes | None = None, timeout_seconds: float = 120.0) -> Response:
            proxied.update({"base_url": base_url, "path": path, "body": body})
            return Response(content=b"image", media_type="image/png", status_code=206)

        main.authenticate = authenticate  # type: ignore[assignment]
        main.proxy_http_bytes = proxy  # type: ignore[assignment]

        response = asyncio.run(main.artifact_download("comfyui/prompt_native_1/0-result.png", FakeArtifactRequest()))

        self.assertEqual(response.status_code, 206)
        self.assertEqual(proxied["base_url"], "http://artifact-server:8000")
        self.assertEqual(proxied["path"], "/artifacts/comfyui/prompt_native_1/0-result.png")
        self.assertIsNone(proxied["body"])


def awaitable_body(request: FakeRequest) -> bytes:
    return asyncio.run(request.body())


if __name__ == "__main__":
    unittest.main()
