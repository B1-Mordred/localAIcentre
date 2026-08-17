from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
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
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class BackupMigrationRollbackEvidenceApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_policy_attr(self, name: str, value: Any) -> None:
        original = getattr(main.backup_migration_rollback, name)
        setattr(main.backup_migration_rollback, name, value)
        self.addCleanup(lambda: setattr(main.backup_migration_rollback, name, original))

    def patch_common(self, backup_root: Path, restore_root: Path, auth: Any, audit_events: list[dict[str, Any]]) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        async def fake_record_audit_event(auth_context: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("backup_root_path", lambda: backup_root)
        self.patch_attr("restore_test_root_path", lambda: restore_root)
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("settings", main.settings.__class__(**{**main.settings.__dict__, "master_key": "k" * 64}))

    def test_admin_can_inspect_and_create_backup_migration_rollback_evidence(self) -> None:
        audit_events: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "backups"
            restore_root = root / "restore-tests"
            auth = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:read", "admin:write"}))
            self.patch_common(backup_root, restore_root, auth, audit_events)

            def fake_status(status_backup_root: Path, status_restore_root: Path) -> dict[str, Any]:
                calls.append({"status_backup_root": status_backup_root, "status_restore_root": status_restore_root})
                return {"format": "b1-ai-hub-backup-migration-rollback-evidence-status/v1", "ready": True}

            def fake_build_and_write_evidence(**kwargs: Any) -> dict[str, Any]:
                calls.append(kwargs)
                return {
                    "status": "ok",
                    "output": str(backup_root / "acceptance" / "backup-migration-rollback.json"),
                    "evidence": {
                        "status": "ok",
                        "required_checks": ["b1_backup_created"],
                    },
                    "inputs": {
                        "b1_backup": {"available": True, "path": str(backup_root / "b1-unit")},
                    },
                }

            self.patch_policy_attr("status", fake_status)
            self.patch_policy_attr("build_and_write_evidence", fake_build_and_write_evidence)

            status = asyncio.run(main.admin_backup_migration_rollback_evidence_status())
            result = asyncio.run(
                main.admin_backup_migration_rollback_evidence_create(
                    main.BackupMigrationRollbackEvidenceCreate(confirm_reviewed=True)
                )
            )

        self.assertTrue(status["ready"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls[0]["status_backup_root"], backup_root)
        self.assertEqual(calls[0]["status_restore_root"], restore_root)
        self.assertEqual(calls[1]["backup_root"], backup_root)
        self.assertEqual(calls[1]["restore_root"], restore_root)
        self.assertEqual(calls[1]["backup_encryption_key"], "k" * 64)
        self.assertEqual(audit_events[0]["event_type"], "backup_migration_rollback_evidence.created")
        self.assertEqual(audit_events[0]["metadata"]["status"], "ok")

    def test_create_requires_admin_write_scope_and_review_confirmation(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            read_only = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:read"}))
            self.patch_common(root / "backups", root / "restore-tests", read_only, audit_events)

            with self.assertRaises(HTTPException) as scope_denied:
                asyncio.run(
                    main.admin_backup_migration_rollback_evidence_create(
                        main.BackupMigrationRollbackEvidenceCreate(confirm_reviewed=True)
                    )
                )

        self.assertEqual(scope_denied.exception.status_code, 403)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admin = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:write"}))
            self.patch_common(root / "backups", root / "restore-tests", admin, audit_events)

            with self.assertRaises(HTTPException) as confirmation_required:
                asyncio.run(
                    main.admin_backup_migration_rollback_evidence_create(
                        main.BackupMigrationRollbackEvidenceCreate(confirm_reviewed=False)
                    )
                )

        self.assertEqual(confirmation_required.exception.status_code, 400)
        self.assertIn("confirm_reviewed", confirmation_required.exception.detail)
        self.assertEqual(audit_events, [])


if __name__ == "__main__":
    unittest.main()
