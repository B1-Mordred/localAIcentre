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
        self.assertEqual(fake_database.list_kwargs, {"limit": 25, "owner_id": "client_1"})

    def test_public_media_job_get_rejects_other_owner(self) -> None:
        self.patch_attr("database", FakeJobsDatabase(job_row(owner_id="other_client")))
        self.patch_auth(AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"jobs:read"})))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.media_job_get("job_1"))

        self.assertEqual(caught.exception.status_code, 403)

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
        self.assertEqual(
            fake_database.list_kwargs,
            {"limit": 10, "owner_id": "client_1", "state": "queued", "runtime": "comfyui", "modality": "image"},
        )

    def test_admin_priority_update_records_audit(self) -> None:
        fake_database = FakeJobsDatabase()
        audit_events: list[dict[str, Any]] = []
        self.patch_attr("database", fake_database)
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:write"})))
        self.patch_audit(audit_events)

        result = asyncio.run(
            main.admin_job_priority_update(
                "job_1",
                main.JobPriorityUpdateRequest(priority=main.PriorityClass.VIDEO, reason="raise video test"),
            )
        )

        self.assertEqual(result["priority"], "video")
        self.assertEqual(fake_database.priority_updates, [("job_1", "video")])
        self.assertEqual(audit_events[0]["event_type"], "job.priority_updated")
        self.assertEqual(audit_events[0]["metadata"]["previous_priority"], "single_image")
        self.assertEqual(audit_events[0]["metadata"]["reason"], "raise video test")

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
        self.assertEqual(fake_database.cancelled, ["job_1"])
        self.assertEqual(audit_events[0]["event_type"], "job.cancelled")

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
        self.assertNotIn("event: timeout", body)


if __name__ == "__main__":
    unittest.main()
