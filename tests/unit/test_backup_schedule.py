from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import backup_schedule  # noqa: E402

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


class BackupSchedulePolicyTests(unittest.TestCase):
    def test_next_run_label_and_validation(self) -> None:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

        self.assertEqual(backup_schedule.next_run_after(now, 6), now + timedelta(hours=6))
        self.assertEqual(backup_schedule.backup_label("scheduled", now), "scheduled-20260722-120000")
        self.assertEqual(backup_schedule.validate_schedule_values(enabled=True, interval_hours=24, keep_last=7, delete_older_than_days=30, label_prefix="nightly")["label_prefix"], "nightly")

        with self.assertRaises(backup_schedule.BackupScheduleError):
            backup_schedule.validate_schedule_values(enabled=True, interval_hours=0, keep_last=7, delete_older_than_days=30, label_prefix="nightly")
        with self.assertRaises(backup_schedule.BackupScheduleError):
            backup_schedule.validate_schedule_values(enabled=True, interval_hours=24, keep_last=0, delete_older_than_days=30, label_prefix="nightly")
        with self.assertRaises(backup_schedule.BackupScheduleError):
            backup_schedule.backup_label("../escape", now)


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class BackupScheduleApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_backup_restore_attr(self, name: str, value: Any) -> None:
        original = getattr(main.backup_restore, name)
        setattr(main.backup_restore, name, value)
        self.addCleanup(lambda: setattr(main.backup_restore, name, original))

    def patch_common(self, store: dict[str, dict[str, Any]], audit_events: list[dict[str, Any]], *, scopes: frozenset[str] = frozenset({"*"})) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=scopes)

        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        async def fake_get_backup_schedule(schedule_id: str = "default") -> dict[str, Any] | None:
            return store.get(schedule_id)

        async def fake_upsert_backup_schedule(payload: dict[str, Any], schedule_id: str = "default") -> dict[str, Any]:
            now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
            existing = store.get(schedule_id)
            row = {
                "id": schedule_id,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
                "last_started_at": existing.get("last_started_at") if existing else None,
                "last_completed_at": existing.get("last_completed_at") if existing else None,
                "last_status": existing.get("last_status", "idle") if existing else "idle",
                "last_backup_name": existing.get("last_backup_name") if existing else None,
                "failure_message": existing.get("failure_message") if existing else None,
                **payload,
            }
            store[schedule_id] = row
            return row

        async def fake_update_backup_schedule_after_run(
            schedule_id: str = "default",
            *,
            status: str,
            next_run_at: datetime | None,
            backup_name: str | None = None,
            failure_message: str | None = None,
            completed_at: datetime | None = None,
        ) -> dict[str, Any] | None:
            row = store[schedule_id]
            row.update(
                {
                    "last_status": status,
                    "last_completed_at": completed_at or datetime(2026, 7, 22, 12, 1, tzinfo=UTC),
                    "next_run_at": next_run_at,
                    "failure_message": failure_message,
                }
            )
            if backup_name is not None:
                row["last_backup_name"] = backup_name
            return row

        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("database", type("FakeDatabase", (), {
            "get_backup_schedule": staticmethod(fake_get_backup_schedule),
            "upsert_backup_schedule": staticmethod(fake_upsert_backup_schedule),
            "update_backup_schedule_after_run": staticmethod(fake_update_backup_schedule_after_run),
        }))

    def test_create_control_plane_backup_includes_logical_and_native_dumps(self) -> None:
        calls: dict[str, Any] = {}

        class FakeDatabase:
            @staticmethod
            async def export_logical_dump(path: Path) -> dict[str, Any]:
                calls["logical_path"] = path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"format":"logical"}', encoding="utf-8")
                return {"path": str(path), "format": "b1-ai-hub-postgres-logical-export/v1"}

        def fake_export_native(database_url: str, output_path: Path) -> dict[str, Any]:
            calls["database_url"] = database_url
            calls["native_path"] = output_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"native")
            return {"path": str(output_path), "format": "postgresql-custom", "tool": "pg_dump", "verified": True}

        def fake_create_backup(root: Path, backup_root: Path, label: str | None = None, **kwargs: Any) -> dict[str, Any]:
            calls["create_backup"] = {"root": root, "backup_root": backup_root, "label": label, **kwargs}
            return {
                "name": label or "unit",
                "file_count": 2,
                "contains_sensitive_data": True,
                "postgres_dump_included": True,
                "postgres_native_dump": {"path": "data/control-plane/postgres-native.dump"},
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.patch_attr("database", FakeDatabase)
            self.patch_attr("data_root_path", lambda: root)
            self.patch_attr("backup_root_path", lambda: root / "backups")
            self.patch_attr(
                "settings",
                replace(
                    main.settings,
                    database_url="postgresql+asyncpg://b1:pass@postgres:5432/b1_ai_hub",
                    master_key="m" * 64,
                    backup_encryption_mode="copy",
                ),
            )
            self.patch_backup_restore_attr("export_native_postgres_dump", fake_export_native)
            self.patch_backup_restore_attr("create_backup", fake_create_backup)

            result = asyncio.run(main.create_control_plane_backup("manual"))

            self.assertEqual(result["postgres_native_dump"]["path"], "data/control-plane/postgres-native.dump")
            self.assertEqual(calls["database_url"], "postgresql+asyncpg://b1:pass@postgres:5432/b1_ai_hub")
            self.assertEqual(calls["create_backup"]["postgres_dump_path"], calls["logical_path"])
            self.assertEqual(calls["create_backup"]["postgres_extra_dumps"][0]["kind"], "native")
            self.assertEqual(calls["create_backup"]["backup_encryption_key"], "m" * 64)
            self.assertEqual(calls["create_backup"]["backup_encryption_mode"], "copy")

    def test_schedule_update_run_and_scope_checks(self) -> None:
        store: dict[str, dict[str, Any]] = {}
        audit_events: list[dict[str, Any]] = []
        self.patch_common(store, audit_events)

        async def fake_create_control_plane_backup(label: str | None = None) -> dict[str, Any]:
            return {
                "name": label or "manual",
                "file_count": 3,
                "contains_sensitive_data": True,
                "postgres_dump_included": True,
                "archive": {"size_bytes": 123},
            }

        def fake_apply_retention_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "status": "applied",
                "policy": {"keep_last": kwargs["keep_last"], "delete_older_than_days": kwargs.get("delete_older_than_days")},
                "candidate_count": 0,
                "kept_count": 1,
                "invalid_preserved_count": 0,
                "total_reclaimable_bytes": 0,
                "candidates": [],
                "kept": [],
                "deleted": [],
                "deleted_count": 0,
            }

        self.patch_attr("create_control_plane_backup", fake_create_control_plane_backup)
        self.patch_backup_restore_attr("apply_backup_retention_plan", fake_apply_retention_plan)

        updated = asyncio.run(
            main.admin_backup_schedule_update(
                main.BackupScheduleUpdateRequest(
                    enabled=True,
                    interval_hours=12,
                    keep_last=5,
                    delete_older_than_days=60,
                    label_prefix="nightly",
                    run_immediately=True,
                )
            )
        )

        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["source"], "database")
        self.assertEqual(updated["label_prefix"], "nightly")
        self.assertEqual(audit_events[0]["event_type"], "backup_schedule.updated")

        run = asyncio.run(main.admin_backup_schedule_run())

        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["backup"]["name"].startswith("nightly-"))
        self.assertEqual(store["default"]["last_status"], "completed")
        self.assertEqual(store["default"]["last_backup_name"], run["backup"]["name"])
        self.assertEqual(audit_events[1]["event_type"], "backup_schedule.run")

        self.patch_common(store, audit_events, scopes=frozenset({"storage:read"}))
        with self.assertRaises(HTTPException) as exc:
            asyncio.run(main.admin_backup_schedule_update(main.BackupScheduleUpdateRequest()))
        self.assertEqual(exc.exception.status_code, 403)

    def test_runner_claims_due_schedule_and_audits_success(self) -> None:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        store = {
            "default": {
                "id": "default",
                "enabled": True,
                "interval_hours": 24,
                "keep_last": 2,
                "delete_older_than_days": 30,
                "label_prefix": "scheduled",
                "next_run_at": now,
                "last_started_at": None,
                "last_completed_at": None,
                "last_status": "idle",
                "last_backup_name": None,
                "failure_message": None,
                "updated_by": "admin_1",
                "created_at": now,
                "updated_at": now,
            }
        }
        audit_events: list[dict[str, Any]] = []

        async def fake_claim_due_backup_schedule(current: datetime, schedule_id: str = "default") -> dict[str, Any] | None:
            row = store[schedule_id]
            row["last_status"] = "running"
            row["last_started_at"] = current
            return dict(row)

        async def fake_update_backup_schedule_after_run(
            schedule_id: str = "default",
            *,
            status: str,
            next_run_at: datetime | None,
            backup_name: str | None = None,
            failure_message: str | None = None,
            completed_at: datetime | None = None,
        ) -> dict[str, Any] | None:
            store[schedule_id].update(
                {
                    "last_status": status,
                    "next_run_at": next_run_at,
                    "last_backup_name": backup_name,
                    "failure_message": failure_message,
                }
            )
            return store[schedule_id]

        async def fake_insert_audit_event(payload: dict[str, Any]) -> dict[str, Any]:
            audit_events.append(payload)
            return payload

        async def fake_create_control_plane_backup(label: str | None = None) -> dict[str, Any]:
            return {"name": label, "file_count": 2, "contains_sensitive_data": False, "postgres_dump_included": True, "archive": {"size_bytes": 50}}

        def fake_apply_retention_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"deleted_count": 1}

        self.patch_attr("settings", replace(main.settings, backup_scheduler_enabled=True))
        self.patch_attr("create_control_plane_backup", fake_create_control_plane_backup)
        self.patch_backup_restore_attr("apply_backup_retention_plan", fake_apply_retention_plan)
        self.patch_attr("backup_root_path", lambda: Path("/srv/b1-ai-hub/backups"))
        self.patch_attr("database", type("FakeDatabase", (), {
            "claim_due_backup_schedule": staticmethod(fake_claim_due_backup_schedule),
            "update_backup_schedule_after_run": staticmethod(fake_update_backup_schedule_after_run),
            "insert_audit_event": staticmethod(fake_insert_audit_event),
        }))

        processed = asyncio.run(main.BackupScheduleRunner().run_once(now))

        self.assertTrue(processed)
        self.assertEqual(store["default"]["last_status"], "completed")
        self.assertEqual(store["default"]["next_run_at"], now + timedelta(hours=24))
        self.assertEqual(audit_events[0]["event_type"], "backup.scheduled")


if __name__ == "__main__":
    unittest.main()
