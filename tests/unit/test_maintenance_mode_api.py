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


class FakeMaintenanceDatabase:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.audit_events: list[dict[str, Any]] = []
        self.inserted_jobs: list[dict[str, Any]] = []
        self.idempotency_row: dict[str, Any] | None = None

    async def get_maintenance_state(self) -> dict[str, Any] | None:
        return dict(self.row) if self.row else None

    async def upsert_maintenance_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        self.row = {
            "id": "default",
            "created_at": self.row.get("created_at", now) if self.row else now,
            "updated_at": now,
            **payload,
        }
        return dict(self.row)

    async def insert_audit_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {"id": f"audit_{len(self.audit_events) + 1}", **payload}
        self.audit_events.append(row)
        return row

    async def get_job_by_idempotency_key(self, owner_id: str, idempotency_key: str) -> dict[str, Any] | None:
        return self.idempotency_row

    async def insert_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.inserted_jobs.append(payload)
        return payload


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class MaintenanceModeApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def setUp(self) -> None:
        self.patch_attr("maintenance_state_cache", None)

    def patch_auth(self, role: Any = None, scopes: set[str] | None = None) -> None:
        async def authenticate(_: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=role or Role.ADMIN, scopes=frozenset(scopes or {"*"}))

        self.patch_attr("authenticate", authenticate)

    def test_default_maintenance_state_is_disabled(self) -> None:
        state = main.current_maintenance_state()

        self.assertFalse(state["enabled"])
        self.assertEqual(state["source"], "default")
        self.assertEqual(state["reason"], "")

    def test_queued_runner_pause_follows_cached_maintenance_state(self) -> None:
        self.assertFalse(main.queued_runner_pause_active())
        self.patch_attr(
            "maintenance_state_cache",
            {
                "id": "default",
                "enabled": True,
                "reason": "update",
                "started_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "ended_at": None,
                "updated_by": "admin_1",
                "created_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            },
        )

        self.assertTrue(main.queued_runner_pause_active())

    def test_admin_can_enable_and_disable_maintenance_mode(self) -> None:
        fake = FakeMaintenanceDatabase()
        self.patch_attr("database", fake)
        self.patch_auth(scopes={"admin:read", "admin:write"})

        enabled = asyncio.run(main.admin_maintenance_update(main.MaintenanceUpdateRequest(enabled=True, reason="update window"), authorization="Bearer key"))
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["reason"], "update window")
        self.assertEqual(main.current_maintenance_state()["enabled"], True)

        disabled = asyncio.run(main.admin_maintenance_update(main.MaintenanceUpdateRequest(enabled=False, reason="complete"), authorization="Bearer key"))
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["reason"], "complete")
        self.assertEqual([event["event_type"] for event in fake.audit_events], ["maintenance.enabled", "maintenance.disabled"])

    def test_enable_requires_reason(self) -> None:
        fake = FakeMaintenanceDatabase()
        self.patch_attr("database", fake)
        self.patch_auth(scopes={"admin:write"})

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_maintenance_update(main.MaintenanceUpdateRequest(enabled=True, reason=" "), authorization="Bearer key"))

        self.assertEqual(caught.exception.status_code, 422)

    def test_non_admin_cannot_change_maintenance_mode(self) -> None:
        fake = FakeMaintenanceDatabase()
        self.patch_attr("database", fake)
        self.patch_auth(role=Role.OPERATOR, scopes={"admin:write"})

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_maintenance_update(main.MaintenanceUpdateRequest(enabled=False), authorization="Bearer key"))

        self.assertEqual(caught.exception.status_code, 403)

    def test_new_job_creation_is_blocked_while_idempotent_existing_job_is_returned(self) -> None:
        fake = FakeMaintenanceDatabase(
            {
                "id": "default",
                "enabled": True,
                "reason": "update window",
                "started_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "ended_at": None,
                "updated_by": "admin_1",
                "created_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            }
        )
        self.patch_attr("database", fake)
        self.patch_attr("maintenance_state_cache", fake.row)
        payload = main.MediaJobCreate(modality="image", operation="generation", model="image-default")

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.create_job_record("creator_1", payload))

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(fake.inserted_jobs, [])

        fake.idempotency_row = {
            "id": "job_existing",
            "owner_id": "creator_1",
            "modality": "image",
            "operation": "generation",
            "model_alias": "image-default",
            "priority": "single_image",
            "resolved_model_version": "unresolved",
            "runtime": "unassigned",
            "request_params": payload.model_dump(),
        }
        existing = asyncio.run(main.create_job_record("creator_1", payload, idempotency_key="same-request"))
        self.assertEqual(existing["id"], "job_existing")

    def test_chat_inference_is_blocked_before_runtime_resolution(self) -> None:
        self.patch_auth(scopes={"inference:write"})
        self.patch_attr(
            "maintenance_state_cache",
            {
                "id": "default",
                "enabled": True,
                "reason": "backup",
                "started_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "ended_at": None,
                "updated_by": "admin_1",
                "created_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            },
        )

        def resolver(*_: Any, **__: Any) -> Any:
            raise AssertionError("maintenance mode must block before runtime resolution")

        self.patch_attr("resolve_catalog_alias_for_modalities", resolver)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.chat_completions(main.ChatCompletionRequest(messages=[]), authorization="Bearer key"))

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["operation"], "chat/completions")


if __name__ == "__main__":
    unittest.main()
