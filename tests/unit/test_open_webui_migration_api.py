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
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


class FakeOpenWebUiMigration:
    class OpenWebUiMigrationError(RuntimeError):
        pass

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def status(self, backup_root: Path, restore_root: Path) -> dict[str, Any]:
        self.calls.append({"method": "status", "backup_root": backup_root, "restore_root": restore_root})
        return {
            "format": "b1-ai-hub-open-webui-migration-plan-status/v1",
            "backup_root": str(backup_root),
            "restore_root": str(restore_root),
            "ready_to_generate": True,
            "inputs": {
                "inventory": {"available": True, "path": str(backup_root / "inventory.json")},
                "old_stack_backup": {"available": True, "path": str(backup_root / "old-stack")},
                "restore_target": {"available": True, "path": str(restore_root / "open-webui-migration")},
                "current_plan": {"available": False, "reason": "not generated"},
            },
        }

    def build_and_write_plan(self, backup_root: Path, restore_root: Path) -> dict[str, Any]:
        self.calls.append({"method": "build_and_write_plan", "backup_root": backup_root, "restore_root": restore_root})
        return {
            "status": "created",
            "path": str(backup_root / "open-webui-migration-plan-20260724-120000.json"),
            "name": "open-webui-migration-plan-20260724-120000.json",
            "summary": {
                "status": "ready",
                "recommended_strategy": "preserve-backed-up-sqlite-and-test-supported-open-webui-import",
                "warning_count": 0,
            },
            "plan": {"format": "b1-ai-hub-open-webui-migration-plan/v1"},
        }


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class OpenWebUiMigrationPlanApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(self, root: Path, auth: Any, fake_policy: FakeOpenWebUiMigration, audit_events: list[dict[str, Any]]) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        async def fake_record_audit_event(auth_context: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("backup_root_path", lambda: root / "backups")
        self.patch_attr("restore_test_root_path", lambda: root / "restore-tests")
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("open_webui_migration", fake_policy)

    def test_admin_can_inspect_and_create_open_webui_plan(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_policy = FakeOpenWebUiMigration()
            auth = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:read", "admin:write"}))
            self.patch_common(root, auth, fake_policy, audit_events)

            status = asyncio.run(main.admin_open_webui_migration_plan_status())
            result = asyncio.run(main.admin_open_webui_migration_plan_create(main.OpenWebUiMigrationPlanCreate(notes="reviewed")))

        self.assertTrue(status["ready_to_generate"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["summary"]["warning_count"], 0)
        self.assertEqual([call["method"] for call in fake_policy.calls], ["status", "build_and_write_plan"])
        self.assertEqual(audit_events[0]["event_type"], "open_webui_migration_plan.created")
        self.assertEqual(audit_events[0]["metadata"]["recommended_strategy"], "preserve-backed-up-sqlite-and-test-supported-open-webui-import")
        self.assertEqual(audit_events[0]["metadata"]["notes"], "reviewed")

    def test_open_webui_plan_generation_requires_admin_write(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_policy = FakeOpenWebUiMigration()
            operator = AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"admin:write"}))
            self.patch_common(root, operator, fake_policy, audit_events)

            with self.assertRaises(HTTPException) as denied:
                asyncio.run(main.admin_open_webui_migration_plan_create(main.OpenWebUiMigrationPlanCreate()))

        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(fake_policy.calls, [])
        self.assertEqual(audit_events, [])


if __name__ == "__main__":
    unittest.main()
