from __future__ import annotations

import asyncio
import json
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
class RollbackRehearsalApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(self, backup_root: Path, auth: Any, audit_events: list[dict[str, Any]]) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        async def fake_record_audit_event(auth_context: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_attr("backup_root_path", lambda: backup_root)
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)

    def write_cutover_plan(self, root: Path) -> Path:
        plan = {
            "format": "b1-ai-hub-cutover-plan/v1",
            "warnings": [],
            "safety": {
                "read_only_plan": True,
                "stops_nothing_automatically": True,
                "deletes_nothing": True,
                "old_stack_deletion_allowed": False,
            },
            "old_stack_scope": {
                "containers_to_restart_for_rollback": ["old-open-webui"],
                "docker_volumes_preserved": ["open-webui-data"],
                "host_paths_preserved": [str(root / "old-stack" / "docker-compose.yaml")],
            },
            "phases": [
                {
                    "name": "rollback",
                    "commands": ["docker compose -p old-ai up -d old-open-webui"],
                    "operator_actions": ["Revert DNS and restart only scoped old-stack containers."],
                },
            ],
        }
        path = root / "cutover-plan.json"
        path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def test_admin_can_inspect_and_create_rollback_rehearsal_report(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_cutover_plan(root)
            auth = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:read", "admin:write"}))
            self.patch_common(root, auth, audit_events)

            status = asyncio.run(main.admin_rollback_rehearsal_status())
            result = asyncio.run(
                main.admin_rollback_rehearsal_create(
                    main.RollbackRehearsalCreate(
                        rollback_commands_tested=True,
                        old_resources_preserved=True,
                        notes="non-destructive staging rehearsal",
                    )
                )
            )

        self.assertTrue(status["cutover_plan"]["available"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["report"]["rehearsed_by"], "admin_1")
        self.assertEqual(result["report"]["cutover_plan_name"], "cutover-plan.json")
        self.assertEqual(audit_events[0]["event_type"], "rollback_rehearsal.created")
        self.assertEqual(audit_events[0]["metadata"]["resource_count"], 3)

    def test_create_requires_admin_write_scope_and_confirmations(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_cutover_plan(root)
            read_only = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:read"}))
            self.patch_common(root, read_only, audit_events)

            with self.assertRaises(HTTPException) as scope_denied:
                asyncio.run(
                    main.admin_rollback_rehearsal_create(
                        main.RollbackRehearsalCreate(rollback_commands_tested=True, old_resources_preserved=True)
                    )
                )

        self.assertEqual(scope_denied.exception.status_code, 403)

        audit_events = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_cutover_plan(root)
            admin = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:write"}))
            self.patch_common(root, admin, audit_events)

            with self.assertRaises(HTTPException) as confirmation_required:
                asyncio.run(
                    main.admin_rollback_rehearsal_create(
                        main.RollbackRehearsalCreate(rollback_commands_tested=False, old_resources_preserved=True)
                    )
                )

        self.assertEqual(confirmation_required.exception.status_code, 409)
        self.assertIn("rollback_commands_tested", confirmation_required.exception.detail)
        self.assertEqual(audit_events, [])


if __name__ == "__main__":
    unittest.main()
