from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    HTTPException = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def job_row(**overrides: Any) -> dict[str, Any]:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    row = {
        "id": "job_1",
        "correlation_id": "corr_1",
        "idempotency_key": None,
        "owner_id": "client_1",
        "modality": "image",
        "operation": "generation",
        "model_alias": "image-default",
        "resolved_model_version": "image-model@1.0.0",
        "runtime": "comfyui",
        "priority": "single_image",
        "state": "queued",
        "request_params": {"prompt": "sensitive"},
        "redacted_request": {"prompt": "[redacted]"},
        "progress": 10,
        "stage": "queued",
        "native_prompt_id": None,
        "artifacts": [],
        "retry_count": 0,
        "failure_category": None,
        "failure_message": None,
        "started_at": None,
        "completed_at": None,
        "load_time_ms": None,
        "run_time_ms": None,
        "peak_vram_mib": None,
        "peak_ram_mib": None,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


class FakeJobsDatabase:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row or job_row()
        self.list_kwargs: dict[str, Any] | None = None
        self.cancelled: list[str] = []
        self.priority_updates: list[tuple[str, str]] = []
        self.retried: list[str] = []

    async def list_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.list_kwargs = dict(kwargs)
        return [dict(self.row)]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        if self.row and self.row["id"] == job_id:
            return dict(self.row)
        return None

    async def request_job_cancel(self, job_id: str) -> dict[str, Any] | None:
        job = await self.get_job(job_id)
        if job is None:
            return None
        self.cancelled.append(job_id)
        if job["state"] in main.TERMINAL_JOB_STATES:
            return job
        return {**job, "state": "cancelled", "stage": "cancelled", "progress": 100}

    async def update_job_priority(self, job_id: str, priority: str) -> dict[str, Any] | None:
        job = await self.get_job(job_id)
        if job is None:
            return None
        if job["state"] == "running":
            raise ValueError("job state running cannot be reprioritized")
        self.priority_updates.append((job_id, priority))
        return {**job, "priority": priority}

    async def retry_job(self, job_id: str) -> dict[str, Any] | None:
        job = await self.get_job(job_id)
        if job is None:
            return None
        if job["state"] not in {"failed", "cancelled", "expired", "recovery_required"}:
            raise ValueError(f"job state {job['state']} cannot be retried")
        self.retried.append(job_id)
        return {
            **job,
            "state": "queued",
            "stage": "queued",
            "progress": 10,
            "retry_count": int(job.get("retry_count") or 0) + 1,
            "failure_category": None,
            "failure_message": None,
            "artifacts": [],
        }


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class AdminJobsApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_auth(self, auth: Any) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        self.patch_attr("authenticate", fake_authenticate)

    def patch_audit(self, audit_events: list[dict[str, Any]]) -> None:
        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("record_audit_event", fake_record_audit_event)

    def test_public_media_job_listing_is_owner_scoped(self) -> None:
        fake_database = FakeJobsDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        rows = asyncio.run(main.media_jobs(limit=25))

        self.assertEqual(rows[0]["id"], "job_1")
        self.assertNotIn("request_params", rows[0])
        self.assertNotIn("idempotency_key", rows[0])
        self.assertEqual(rows[0]["redacted_request"], {"prompt": "[redacted]"})
        self.assertEqual(fake_database.list_kwargs, {"limit": 25, "owner_id": "client_1", "state": None, "runtime": None, "modality": None})

    def test_public_media_job_listing_passes_owner_scoped_filters(self) -> None:
        fake_database = FakeJobsDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        rows = asyncio.run(main.media_jobs(limit=25, state=main.JobState.QUEUED, runtime="comfyui", modality="image"))

        self.assertEqual(rows[0]["id"], "job_1")
        self.assertEqual(
            fake_database.list_kwargs,
            {"limit": 25, "owner_id": "client_1", "state": "queued", "runtime": "comfyui", "modality": "image"},
        )

    def test_public_media_job_listing_filters_owner_scoped_native_prompt_id(self) -> None:
        fake_database = FakeJobsDatabase(job_row(native_prompt_id="prompt_native_1", operation="comfyui-prompt", model_alias="comfyui-native"))
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        rows = asyncio.run(main.media_jobs(limit=10, runtime="comfyui", native_prompt_id="prompt_native_1"))

        self.assertEqual(rows[0]["native_prompt_id"], "prompt_native_1")
        self.assertEqual(
            fake_database.list_kwargs,
            {"limit": 10, "owner_id": "client_1", "state": None, "runtime": "comfyui", "modality": None, "native_prompt_id": "prompt_native_1"},
        )

    def test_public_media_job_listing_allows_wildcard_admin_to_read_all(self) -> None:
        fake_database = FakeJobsDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"*"})))

        rows = asyncio.run(main.media_jobs(limit=10))

        self.assertEqual(rows[0]["id"], "job_1")
        self.assertEqual(fake_database.list_kwargs, {"limit": 10, "owner_id": None, "state": None, "runtime": None, "modality": None})

    def test_public_media_job_get_returns_redacted_job_payload(self) -> None:
        self.patch_attr(
            "database",
            FakeJobsDatabase(job_row(idempotency_key="idem_sensitive", request_params={"prompt": "secret prompt"}, redacted_request={"prompt": "[redacted]"})),
        )
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        result = asyncio.run(main.media_job_get("job_1"))

        self.assertEqual(result["id"], "job_1")
        self.assertEqual(result["redacted_request"], {"prompt": "[redacted]"})
        self.assertNotIn("request_params", result)
        self.assertNotIn("idempotency_key", result)
        self.assertNotIn("secret prompt", str(result))
        self.assertNotIn("idem_sensitive", str(result))
        self.assertEqual(
            result["links"],
            {
                "self": "/v1/media/jobs/job_1",
                "events": "/v1/media/jobs/job_1/events",
                "artifacts": "/v1/media/jobs/job_1/artifacts",
                "cancel": "/v1/media/jobs/job_1",
            },
        )

    def test_public_media_job_get_rejects_other_owner(self) -> None:
        self.patch_attr("database", FakeJobsDatabase(job_row(owner_id="other_client")))
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.media_job_get("job_1"))

        self.assertEqual(caught.exception.status_code, 403)

    def test_job_sanitizer_falls_back_to_redacting_raw_request(self) -> None:
        result = main.public_job(
            job_row(
                idempotency_key="idem_secret",
                request_params={"input": {"workflow_id": "text-to-image", "parameters": {"prompt": "raw secret", "seed": 12}}},
                redacted_request=None,
            )
        )

        self.assertEqual(
            result["redacted_request"],
            {"input": {"workflow_id": "text-to-image", "parameters": {"prompt": "<redacted>", "seed": 12}}},
        )
        self.assertNotIn("request_params", result)
        self.assertNotIn("idempotency_key", result)
        self.assertNotIn("raw secret", str(result))
        self.assertNotIn("idem_secret", str(result))

    def test_public_job_links_percent_encode_job_id_path_segments(self) -> None:
        result = main.public_job(job_row(id="job/one two"))

        self.assertEqual(
            result["links"],
            {
                "self": "/v1/media/jobs/job%2Fone%20two",
                "events": "/v1/media/jobs/job%2Fone%20two/events",
                "artifacts": "/v1/media/jobs/job%2Fone%20two/artifacts",
                "cancel": "/v1/media/jobs/job%2Fone%20two",
            },
        )

    def test_public_native_comfyui_job_includes_redacted_compatibility_summary(self) -> None:
        result = main.public_job(
            job_row(
                operation="comfyui-prompt",
                model_alias="comfyui-native",
                native_prompt_id="prompt_native_1",
                state="completed",
                stage="completed",
                progress=100,
                request_params={
                    "input": {
                        "client_id": "native-client-1",
                        "native_prompt_hash": "f" * 64,
                        "prompt_summary": {
                            "known_top_level_keys": ["client_id", "prompt"],
                            "unknown_top_level_key_count": 1,
                            "has_client_id": True,
                            "has_prompt": True,
                            "node_count": 3,
                            "class_type_count": 2,
                            "class_type_digest": "a" * 64,
                            "node_id_digest": "b" * 64,
                            "malformed_node_count": 0,
                        },
                    }
                },
                redacted_request=None,
                artifacts=[
                    {"kind": "image", "source": "artifact_store", "ingest_status": "stored", "bytes": 12, "url": "/artifacts/comfyui/prompt_native_1/0.png"},
                    {"kind": "video", "source": "comfyui_view", "ingest_status": "failed", "bytes": None, "url": "/artifacts/comfyui/prompt_native_1/1.mp4"},
                    {"kind": "audio", "source": "comfyui_view", "url": "/artifacts/comfyui/prompt_native_1/2.wav"},
                ],
            )
        )

        self.assertNotIn("request_params", result)
        self.assertNotIn("idempotency_key", result)
        self.assertEqual(
            result["redacted_request"]["input"]["native_comfyui"]["known_top_level_keys"],
            ["client_id", "prompt"],
        )
        self.assertEqual(result["native_comfyui"]["native_prompt_id"], "prompt_native_1")
        self.assertTrue(result["native_comfyui"]["native_prompt_recorded"])
        self.assertTrue(result["native_comfyui"]["tracker_terminal"])
        self.assertTrue(result["native_comfyui"]["prompt"]["body_hash_present"])
        self.assertTrue(result["native_comfyui"]["prompt"]["class_type_digest_present"])
        self.assertTrue(result["native_comfyui"]["prompt"]["node_id_digest_present"])
        self.assertEqual(result["native_comfyui"]["prompt"]["node_count"], 3)
        self.assertEqual(result["native_comfyui"]["prompt"]["known_top_level_keys"], ["client_id", "prompt"])
        self.assertEqual(result["native_comfyui"]["artifacts"]["artifact_count"], 3)
        self.assertEqual(result["native_comfyui"]["artifacts"]["stored_artifact_count"], 1)
        self.assertEqual(result["native_comfyui"]["artifacts"]["failed_ingest_count"], 1)
        self.assertEqual(result["native_comfyui"]["artifacts"]["pending_view_artifact_count"], 1)
        self.assertEqual(result["native_comfyui"]["artifacts"]["artifact_kinds"], ["audio", "image", "video"])
        self.assertEqual(result["native_comfyui"]["artifacts"]["stored_bytes"], 12)
        self.assertNotIn("f" * 64, str(result))
        self.assertNotIn("a" * 64, str(result))
        self.assertNotIn("b" * 64, str(result))
        self.assertNotIn("native-client-1", str(result))

    def test_admin_job_listing_requires_admin_or_operator_role(self) -> None:
        fake_database = FakeJobsDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_jobs(limit=10))

        self.assertEqual(caught.exception.status_code, 403)
        self.assertIsNone(fake_database.list_kwargs)

    def test_operator_admin_job_listing_passes_filters(self) -> None:
        fake_database = FakeJobsDatabase()
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:read"})))

        result = asyncio.run(main.admin_jobs(limit=10, state=main.JobState.QUEUED, runtime="comfyui", modality="image", owner_id="client_1"))

        self.assertEqual(result["object"], "list")
        self.assertNotIn("request_params", result["data"][0])
        self.assertNotIn("idempotency_key", result["data"][0])
        self.assertEqual(result["data"][0]["redacted_request"], {"prompt": "[redacted]"})
        self.assertEqual(
            fake_database.list_kwargs,
            {"limit": 10, "owner_id": "client_1", "state": "queued", "runtime": "comfyui", "modality": "image"},
        )

    def test_operator_admin_job_listing_filters_native_prompt_id(self) -> None:
        fake_database = FakeJobsDatabase(job_row(native_prompt_id="prompt_native_1", operation="comfyui-prompt", model_alias="comfyui-native"))
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:read"})))

        result = asyncio.run(main.admin_jobs(limit=10, runtime="comfyui", native_prompt_id="prompt_native_1"))

        self.assertEqual(result["data"][0]["native_prompt_id"], "prompt_native_1")
        self.assertEqual(
            fake_database.list_kwargs,
            {"limit": 10, "owner_id": None, "state": None, "runtime": "comfyui", "modality": None, "native_prompt_id": "prompt_native_1"},
        )

    def test_admin_job_get_returns_redacted_job_payload(self) -> None:
        self.patch_attr(
            "database",
            FakeJobsDatabase(job_row(idempotency_key="admin_idem", request_params={"input": {"prompt": "admin visible secret"}}, redacted_request={"input": "<redacted>"})),
        )
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:read"})))

        result = asyncio.run(main.admin_job_get("job_1"))

        self.assertEqual(result["redacted_request"], {"input": "<redacted>"})
        self.assertNotIn("request_params", result)
        self.assertNotIn("idempotency_key", result)
        self.assertNotIn("admin visible secret", str(result))
        self.assertNotIn("admin_idem", str(result))

    def test_admin_priority_update_records_audit(self) -> None:
        fake_database = FakeJobsDatabase()
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:write"})))
        self.patch_audit(audit_events)

        result = asyncio.run(
            main.admin_job_priority_update(
                "job_1",
                main.JobPriorityUpdateRequest(
                    priority=main.PriorityClass.VIDEO,
                    reason="raise video test with pasted prompt: secret image and Authorization: Bearer secret-token",
                ),
            )
        )

        self.assertEqual(result["priority"], "video")
        self.assertNotIn("request_params", result)
        self.assertNotIn("idempotency_key", result)
        self.assertEqual(fake_database.priority_updates, [("job_1", "video")])
        self.assertEqual(audit_events[0]["event_type"], "job.priority_updated")
        self.assertEqual(audit_events[0]["metadata"]["previous_priority"], "single_image")
        self.assertTrue(audit_events[0]["metadata"]["reason_provided"])
        self.assertNotIn("reason", audit_events[0]["metadata"])
        self.assertNotIn("secret image", str(audit_events[0]["metadata"]))
        self.assertNotIn("secret-token", str(audit_events[0]["metadata"]))

    def test_admin_retry_requeues_retryable_job(self) -> None:
        fake_database = FakeJobsDatabase(job_row(state="failed", stage="failed", retry_count=2, failure_category="runtime_error", artifacts=[{"url": "/artifacts/old"}]))
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:write"})))
        self.patch_audit(audit_events)

        result = asyncio.run(main.admin_job_retry("job_1", main.JobMutationRequest(reason="rerun after model fix")))

        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["retry_count"], 3)
        self.assertIsNone(result["failure_category"])
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(fake_database.retried, ["job_1"])
        self.assertEqual(audit_events[0]["event_type"], "job.retried")
        self.assertTrue(audit_events[0]["metadata"]["reason_provided"])
        self.assertNotIn("reason", audit_events[0]["metadata"])

    def test_admin_retry_rejects_active_job(self) -> None:
        self.patch_attr("database", FakeJobsDatabase(job_row(state="running", stage="running")))
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:write"})))
        self.patch_audit([])

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_job_retry("job_1", main.JobMutationRequest(reason="bad retry")))

        self.assertEqual(caught.exception.status_code, 409)

    def test_public_cancel_requires_owner_and_uses_cancel_transition(self) -> None:
        fake_database = FakeJobsDatabase()
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:write"})))
        self.patch_audit(audit_events)

        result = asyncio.run(main.media_job_cancel("job_1"))

        self.assertEqual(result["state"], "cancelled")
        self.assertNotIn("request_params", result)
        self.assertNotIn("idempotency_key", result)
        self.assertEqual(fake_database.cancelled, ["job_1"])
        self.assertEqual(audit_events[0]["event_type"], "job.cancelled")

    def test_admin_cancel_records_reason_presence_without_reason_text(self) -> None:
        fake_database = FakeJobsDatabase()
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:write"})))
        self.patch_audit(audit_events)

        result = asyncio.run(main.admin_job_cancel("job_1", main.JobMutationRequest(reason="cancel pasted prompt secret")))

        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(audit_events[0]["event_type"], "job.cancelled")
        self.assertTrue(audit_events[0]["metadata"]["reason_provided"])
        self.assertNotIn("reason", audit_events[0]["metadata"])
        self.assertNotIn("pasted prompt secret", str(audit_events[0]["metadata"]))

    def test_admin_cancel_terminal_job_is_idempotent_without_audit(self) -> None:
        fake_database = FakeJobsDatabase(job_row(state="completed", stage="completed", progress=100))
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:write"})))
        self.patch_audit(audit_events)

        result = asyncio.run(main.admin_job_cancel("job_1", main.JobMutationRequest(reason="already done")))

        self.assertEqual(result["state"], "completed")
        self.assertEqual(audit_events, [])

    def test_admin_cancel_recovery_required_job_is_idempotent_without_audit(self) -> None:
        fake_database = FakeJobsDatabase(job_row(state="recovery_required", stage="recovery_required", progress=0))
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:write"})))
        self.patch_audit(audit_events)

        result = asyncio.run(main.admin_job_cancel("job_1", main.JobMutationRequest(reason="already needs recovery")))

        self.assertEqual(result["state"], "recovery_required")
        self.assertEqual(audit_events, [])

    def test_public_events_end_immediately_for_recovery_required_job(self) -> None:
        fake_database = FakeJobsDatabase(job_row(state="recovery_required", stage="recovery_required", progress=0))
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        async def collect_events() -> str:
            response = await main.media_job_events("job_1")
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        body = asyncio.run(collect_events())

        self.assertIn("event: job", body)
        self.assertIn("retry: 1000", body)
        self.assertIn('"state":"recovery_required"', body)
        self.assertNotIn("request_params", body)
        self.assertNotIn("idempotency_key", body)
        self.assertNotIn("sensitive", body)
        self.assertNotIn("event: timeout", body)

    def test_event_stream_keeps_active_jobs_observable_without_backend_timeout(self) -> None:
        fake_database = FakeJobsDatabase(job_row(state="running", stage="running", progress=70))
        self.patch_attr("database", fake_database)

        async def collect_events() -> str:
            response = main.job_event_stream("job_1", lambda _: True, poll_interval_seconds=0, max_events=3)
            self.assertEqual(response.headers["cache-control"], "no-cache")
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        body = asyncio.run(collect_events())

        self.assertEqual(body.count("event: job"), 3)
        self.assertIn('"state":"running"', body)
        self.assertNotIn("event: timeout", body)
        self.assertNotIn("job event stream timed out", body)

    def test_event_stream_resumes_after_last_event_id_without_duplicate_snapshot(self) -> None:
        first_updated_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        second_updated_at = datetime(2026, 7, 22, 12, 1, tzinfo=UTC)
        first = job_row(state="running", stage="running", progress=70, updated_at=first_updated_at)
        second = job_row(state="running", stage="saving", progress=90, updated_at=second_updated_at)

        class SequenceDatabase(FakeJobsDatabase):
            def __init__(self) -> None:
                super().__init__(first)
                self.rows = [first, second]

            async def get_job(self, job_id: str) -> dict[str, Any] | None:
                if job_id != "job_1":
                    return None
                if len(self.rows) > 1:
                    return dict(self.rows.pop(0))
                return dict(self.rows[0])

        self.patch_attr("database", SequenceDatabase())

        async def collect_events() -> str:
            response = main.job_event_stream(
                "job_1",
                lambda _: True,
                poll_interval_seconds=0,
                max_events=1,
                last_event_id=main.job_event_id(first),
            )
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        body = asyncio.run(collect_events())

        self.assertIn(": heartbeat", body)
        self.assertEqual(body.count("event: job"), 1)
        self.assertNotIn('"progress":70', body)
        self.assertIn('"progress":90', body)
        self.assertIn("id: job_1:2026-07-22T12:01:00+00:00", body)

    def test_event_stream_still_emits_terminal_job_when_last_event_id_matches(self) -> None:
        row = job_row(state="completed", stage="completed", progress=100)
        self.patch_attr("database", FakeJobsDatabase(row))

        async def collect_events() -> str:
            response = main.job_event_stream(
                "job_1",
                lambda _: True,
                poll_interval_seconds=0,
                last_event_id=main.job_event_id(row),
            )
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        body = asyncio.run(collect_events())

        self.assertNotIn(": heartbeat", body)
        self.assertEqual(body.count("event: job"), 1)
        self.assertIn('"state":"completed"', body)

    def test_event_stream_runs_until_job_reaches_terminal_state(self) -> None:
        class SequenceDatabase(FakeJobsDatabase):
            def __init__(self) -> None:
                super().__init__(job_row(state="queued", stage="queued", progress=10))
                self.rows = [
                    job_row(state="queued", stage="queued", progress=10),
                    job_row(state="running", stage="running", progress=70),
                    job_row(state="completed", stage="completed", progress=100),
                ]

            async def get_job(self, job_id: str) -> dict[str, Any] | None:
                if job_id != "job_1":
                    return None
                if len(self.rows) > 1:
                    return dict(self.rows.pop(0))
                return dict(self.rows[0])

        self.patch_attr("database", SequenceDatabase())

        async def collect_events() -> str:
            response = main.job_event_stream("job_1", lambda _: True, poll_interval_seconds=0)
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        body = asyncio.run(collect_events())

        self.assertEqual(body.count("event: job"), 3)
        self.assertIn('"state":"queued"', body)
        self.assertIn('"state":"running"', body)
        self.assertIn('"state":"completed"', body)
        self.assertNotIn("event: timeout", body)

    def test_admin_events_allow_operator_to_stream_other_owner_job(self) -> None:
        fake_database = FakeJobsDatabase(job_row(owner_id="client_1", state="recovery_required", stage="recovery_required", progress=0))
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:read"})))

        async def collect_events() -> str:
            response = await main.admin_job_events("job_1")
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        body = asyncio.run(collect_events())

        self.assertIn("event: job", body)
        self.assertIn('"owner_id":"client_1"', body)
        self.assertIn('"state":"recovery_required"', body)
        self.assertNotIn("request_params", body)
        self.assertNotIn("idempotency_key", body)
        self.assertNotIn("sensitive", body)
        self.assertNotIn("belongs to a different owner", body)

    def test_public_events_accept_last_event_id_header_parameter(self) -> None:
        row = job_row(state="completed", stage="completed", progress=100)
        fake_database = FakeJobsDatabase(row)
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        async def collect_events() -> str:
            response = await main.media_job_events("job_1", last_event_id=main.job_event_id(row))
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
            return "".join(chunks)

        body = asyncio.run(collect_events())

        self.assertIn("event: job", body)
        self.assertIn('"state":"completed"', body)

    def test_admin_events_requires_queue_admin_role(self) -> None:
        fake_database = FakeJobsDatabase(job_row(state="recovery_required", stage="recovery_required", progress=0))
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_job_events("job_1"))

        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
