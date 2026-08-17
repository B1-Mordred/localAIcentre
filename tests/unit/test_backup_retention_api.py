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
    backup_restore = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class BackupRetentionApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def make_root(self, root: Path) -> None:
        (root / "data" / "open-webui").mkdir(parents=True)
        (root / "data" / "open-webui" / "db.sqlite").write_text("webui", encoding="utf-8")
        (root / "secrets").mkdir()
        (root / "secrets" / "admin_bootstrap_key").write_text("secret", encoding="utf-8")

    def set_backup_created_at(self, backup_dir: Path, created_at: datetime) -> None:
        manifest_path = backup_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = created_at.isoformat()
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    def patch_common(self, backup_root: Path, audit_events: list[dict[str, Any]], *, scopes: frozenset[str] = frozenset({"*"})) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=scopes)

        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("backup_root_path", lambda: backup_root)
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)

    def test_retention_plan_and_cleanup_endpoint_require_confirmation_and_audit(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            for label, created_at in [
                ("old", datetime(2026, 7, 1, tzinfo=UTC)),
                ("new", datetime(2026, 7, 20, tzinfo=UTC)),
            ]:
                backup_restore.create_backup(root, label=label)
                self.set_backup_created_at(root / "backups" / label, created_at)
            self.patch_common(root / "backups", audit_events)

            plan = asyncio.run(main.admin_backup_retention_plan(main.BackupRetentionRequest(keep_last=1)))

            self.assertEqual(plan["status"], "planned")
            self.assertEqual([item["name"] for item in plan["candidates"]], ["old"])
            with self.assertRaises(HTTPException) as exc:
                asyncio.run(main.admin_backup_cleanup(main.BackupRetentionRequest(keep_last=1, confirm=False)))
            self.assertEqual(exc.exception.status_code, 400)

            result = asyncio.run(main.admin_backup_cleanup(main.BackupRetentionRequest(keep_last=1, confirm=True)))

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["deleted_count"], 1)
            self.assertFalse((root / "backups" / "old").exists())
            self.assertTrue((root / "backups" / "new").is_dir())
            self.assertEqual(audit_events[0]["event_type"], "backup.cleanup")
            self.assertEqual(audit_events[0]["metadata"]["deleted_names"], ["old"])

    def test_retention_plan_requires_storage_read_scope(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_root(root)
            self.patch_common(root / "backups", audit_events, scopes=frozenset({"jobs:read"}))

            with self.assertRaises(HTTPException) as exc:
                asyncio.run(main.admin_backup_retention_plan(main.BackupRetentionRequest()))

            self.assertEqual(exc.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
