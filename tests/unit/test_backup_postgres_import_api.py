from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import backup_restore, main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    backup_restore = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


class FakeImportDatabase:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def import_logical_dump(self, input_path: Path, apply: bool = False) -> dict[str, Any]:
        self.calls.append({"input_path": input_path, "apply": apply})
        return {
            "status": "applied" if apply else "planned",
            "row_counts": {"b1_jobs": 1},
            "applied_row_counts": {"b1_jobs": 1} if apply else None,
        }


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class BackupPostgresImportApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(self, root: Path, fake_database: FakeImportDatabase, auth: Any, audit_events: list[dict[str, Any]]) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        async def fake_record_audit_event(auth_context: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("backup_root_path", lambda: root / "backups")
        self.patch_attr("restore_test_root_path", lambda: root / "restore-tests")
        self.patch_attr("database", fake_database)
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("maintenance_state_cache", None)

    def write_restore_test(self, root: Path, backup_name: str = "unit-backup") -> Path:
        dump_relative = "data/control-plane/postgres-logical-export.json"
        backup_dir = root / "backups" / backup_name
        backup_dir.mkdir(parents=True)
        manifest = {
            "format": backup_restore.BACKUP_FORMAT,
            "created_at": datetime(2026, 7, 24, tzinfo=UTC).isoformat(),
            "postgres_dump_included": True,
            "postgres_dump": {
                "path": dump_relative,
                "format": backup_restore.LOGICAL_POSTGRES_DUMP_FORMAT,
                "kind": "logical",
            },
            "files": [],
            "archive": {},
        }
        (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        dump_path = root / "restore-tests" / backup_name / dump_relative
        dump_path.parent.mkdir(parents=True)
        dump_path.write_text('{"format":"b1-ai-hub-postgres-logical-export/v1","tables":[]}', encoding="utf-8")
        return dump_path

    def test_operator_can_plan_postgres_import_without_maintenance(self) -> None:
        audit_events: list[dict[str, Any]] = []
        fake_database = FakeImportDatabase()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump_path = self.write_restore_test(root)
            auth = AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"storage:write"}))
            self.patch_common(root, fake_database, auth, audit_events)

            result = asyncio.run(
                main.admin_backup_postgres_import(
                    "unit-backup",
                    main.BackupPostgresImportRequest(apply=False),
                )
            )

        self.assertEqual(result["status"], "planned")
        self.assertEqual(fake_database.calls, [{"input_path": dump_path, "apply": False}])
        self.assertEqual(audit_events[0]["event_type"], "backup.postgres_import")
        self.assertFalse(audit_events[0]["metadata"]["apply"])

    def test_postgres_import_apply_requires_admin_and_maintenance_mode(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_restore_test(root)
            fake_database = FakeImportDatabase()
            operator_auth = AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"storage:write"}))
            self.patch_common(root, fake_database, operator_auth, audit_events)
            self.patch_attr(
                "maintenance_state_cache",
                {
                    "id": "default",
                    "enabled": True,
                    "reason": "restore rehearsal",
                    "started_at": datetime(2026, 7, 24, tzinfo=UTC),
                    "updated_by": "admin_1",
                },
            )

            with self.assertRaises(HTTPException) as operator_denied:
                asyncio.run(
                    main.admin_backup_postgres_import(
                        "unit-backup",
                        main.BackupPostgresImportRequest(apply=True, confirm_backup_name="unit-backup"),
                    )
                )

        self.assertEqual(operator_denied.exception.status_code, 403)
        self.assertEqual(operator_denied.exception.detail, "PostgreSQL backup import apply requires administrator role")
        self.assertEqual(fake_database.calls, [])

        audit_events = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_restore_test(root)
            fake_database = FakeImportDatabase()
            admin_auth = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"storage:write"}))
            self.patch_common(root, fake_database, admin_auth, audit_events)

            with self.assertRaises(HTTPException) as maintenance_required:
                asyncio.run(
                    main.admin_backup_postgres_import(
                        "unit-backup",
                        main.BackupPostgresImportRequest(apply=True, confirm_backup_name="unit-backup"),
                    )
                )

            self.assertEqual(maintenance_required.exception.status_code, 409)
            self.assertEqual(
                maintenance_required.exception.detail["message"],
                "maintenance mode must be enabled before backup postgres-import apply",
            )
            self.patch_attr(
                "maintenance_state_cache",
                {
                    "id": "default",
                    "enabled": True,
                    "reason": "restore rehearsal",
                    "started_at": datetime(2026, 7, 24, tzinfo=UTC),
                    "updated_by": "admin_1",
                },
            )

            applied = asyncio.run(
                main.admin_backup_postgres_import(
                    "unit-backup",
                    main.BackupPostgresImportRequest(apply=True, confirm_backup_name="unit-backup"),
                )
            )

        self.assertEqual(applied["status"], "applied")
        self.assertEqual([call["apply"] for call in fake_database.calls], [True])
        self.assertEqual(audit_events[0]["event_type"], "backup.postgres_import")
        self.assertTrue(audit_events[0]["metadata"]["apply"])


if __name__ == "__main__":
    unittest.main()
