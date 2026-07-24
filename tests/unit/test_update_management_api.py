from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main, update_policy  # noqa: E402
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


GOOD_DIGEST = "a" * 64


class FakeUpdateDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []

    async def insert_update_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        row = {
            "created_at": now,
            "updated_at": now,
            "image_stage": [],
            "compose_override": {},
            "self_test": {},
            "promotion_result": {},
            "rollback_result": {},
            "promotion_requested_at": None,
            **payload,
        }
        self.rows[row["id"]] = dict(row)
        return dict(row)

    async def get_update_plan(self, update_id: str) -> dict[str, Any] | None:
        row = self.rows.get(update_id)
        return dict(row) if row else None

    async def list_update_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.rows.values())[:limit]

    async def update_update_plan(self, update_id: str, **changes: Any) -> dict[str, Any] | None:
        row = self.rows.get(update_id)
        if row is None:
            return None
        row.update(changes)
        row["updated_at"] = datetime(2026, 7, 22, 12, 1, tzinfo=UTC)
        return dict(row)

    async def insert_audit_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {"id": f"audit_{len(self.audit_events) + 1}", **payload}
        self.audit_events.append(row)
        return row


def create_payload(image: str | None = None) -> Any:
    return main.UpdatePlanCreateRequest(
        target_version="0.2.0",
        source_url="https://github.com/B1-Mordred/localAIcentre/releases/tag/v0.2.0",
        image_refs=[
            main.UpdateImageReference(
                service="control-plane",
                image=image or f"ghcr.io/b1/b1-ai-hub-control-plane:0.2.0@sha256:{GOOD_DIGEST}",
            )
        ],
        notes="staged release",
    )


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class UpdateManagementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        original_resolver = update_policy.resolve_hostname_addresses
        update_policy.resolve_hostname_addresses = lambda hostname, port: ["93.184.216.34"]
        self.addCleanup(lambda: setattr(update_policy, "resolve_hostname_addresses", original_resolver))

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

    def enable_maintenance(self) -> None:
        self.patch_attr(
            "maintenance_state_cache",
            {
                "id": "default",
                "enabled": True,
                "reason": "update window",
                "started_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "ended_at": None,
                "updated_by": "admin_1",
                "created_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            },
        )

    def test_create_update_plan_requires_admin_and_pinned_images(self) -> None:
        fake = FakeUpdateDatabase()
        self.patch_attr("database", fake)
        self.patch_auth(scopes={"admin:write"})

        created = asyncio.run(main.admin_update_plan_create(create_payload(), authorization="Bearer key"))
        self.assertEqual(created["target_version"], "0.2.0")
        self.assertEqual(created["status"], "planned")
        self.assertTrue(created["preflight"]["pinned_images"])
        self.assertEqual(fake.audit_events[0]["event_type"], "update.planned")

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_update_plan_create(create_payload("ghcr.io/b1/control-plane:latest"), authorization="Bearer key"))
        self.assertEqual(caught.exception.status_code, 422)

    def test_stage_requires_maintenance_then_records_backup_and_self_test(self) -> None:
        fake = FakeUpdateDatabase()
        self.patch_attr("database", fake)
        self.patch_auth(scopes={"admin:write"})
        created = asyncio.run(main.admin_update_plan_create(create_payload(), authorization="Bearer key"))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_update_stage(created["id"], main.UpdateActionRequest(reason="stage"), authorization="Bearer key"))
        self.assertEqual(caught.exception.status_code, 409)

        self.enable_maintenance()

        async def create_backup(label: str | None = None) -> dict[str, Any]:
            return {"name": label or "backup", "file_count": 1, "contains_sensitive_data": True, "postgres_dump_included": True}

        async def self_test(subject_id: str) -> dict[str, Any]:
            return {"status": "ok", "subject_id": subject_id, "checks": []}

        image_stage_calls: list[dict[str, Any]] = []

        async def runtime_agent_post(path: str, payload: dict[str, Any], timeout_seconds: float = 30.0) -> tuple[dict[str, Any] | None, str | None]:
            image_stage_calls.append({"path": path, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status": "dry_run", "service": "control-plane", "action": "pull", "image": payload["image"]}, None

        self.patch_attr("create_control_plane_backup", create_backup)
        self.patch_attr("build_self_test_report", self_test)
        self.patch_attr("runtime_agent_post", runtime_agent_post)

        with tempfile.TemporaryDirectory() as tmp:
            self.patch_attr("settings", replace(main.settings, data_root=tmp))
            staged = asyncio.run(main.admin_update_stage(created["id"], main.UpdateActionRequest(reason="stage"), authorization="Bearer key"))
            override_path = Path(staged["compose_override"]["path"])
            override_content = override_path.read_text(encoding="utf-8")

        self.assertEqual(staged["status"], "staged")
        self.assertTrue(staged["backup_name"].startswith("update-"))
        self.assertEqual(staged["stage"], "backup_images_and_self_test_completed")
        self.assertEqual(staged["image_stage"][0]["status"], "dry_run")
        self.assertEqual(image_stage_calls[0]["path"], "/v1/images/control-plane/pull")
        self.assertEqual(staged["self_test"]["status"], "ok")
        self.assertEqual(staged["compose_override"]["format"], "b1-ai-hub-compose-image-override/v1")
        self.assertFalse(staged["compose_override"]["ready_for_promotion"])
        self.assertEqual(staged["compose_override"]["not_pulled_services"], ["control-plane"])
        self.assertIn("build: null", override_content)
        self.assertIn(f"@sha256:{GOOD_DIGEST}", override_content)

    def test_health_check_and_rollback_use_existing_control_plane_surfaces(self) -> None:
        fake = FakeUpdateDatabase()
        self.patch_attr("database", fake)
        self.patch_auth(scopes={"admin:write"})
        self.enable_maintenance()
        created = asyncio.run(main.admin_update_plan_create(create_payload(), authorization="Bearer key"))
        fake.rows[created["id"]]["status"] = "staged"
        fake.rows[created["id"]]["stage"] = "backup_images_and_self_test_completed"

        async def self_test(subject_id: str) -> dict[str, Any]:
            return {"status": "degraded", "subject_id": subject_id, "checks": [{"name": "gpu:nvml", "status": "warning", "detail": "dev host"}]}

        rollback_calls: list[dict[str, Any]] = []

        async def runtime_agent_post(path: str, payload: dict[str, Any], timeout_seconds: float = 30.0) -> tuple[dict[str, Any] | None, str | None]:
            rollback_calls.append({"path": path, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status": "dry_run", "services": ["gateway"], "action": "rollback"}, None

        self.patch_attr("build_self_test_report", self_test)
        self.patch_attr("runtime_agent_post", runtime_agent_post)

        checked = asyncio.run(main.admin_update_health_check(created["id"], main.UpdateActionRequest(reason="check"), authorization="Bearer key"))
        self.assertEqual(checked["status"], "validated")
        rolled_back = asyncio.run(main.admin_update_rollback(created["id"], main.UpdateActionRequest(reason="rollback", timeout_seconds=7), authorization="Bearer key"))

        self.assertEqual(rolled_back["status"], "rollback_dry_run")
        self.assertEqual(rollback_calls[0]["path"], "/v1/rollback")
        self.assertEqual(rollback_calls[0]["payload"]["timeout_seconds"], 7)

    def test_promote_requires_validated_ready_override_and_inspects_images(self) -> None:
        fake = FakeUpdateDatabase()
        self.patch_attr("database", fake)
        self.patch_auth(scopes={"admin:write"})
        self.enable_maintenance()
        created = asyncio.run(main.admin_update_plan_create(create_payload(), authorization="Bearer key"))
        image_stage = [{"service": "control-plane", "status": "ok"}]

        inspect_calls: list[dict[str, Any]] = []

        async def runtime_agent_post(path: str, payload: dict[str, Any], timeout_seconds: float = 30.0) -> tuple[dict[str, Any] | None, str | None]:
            inspect_calls.append({"path": path, "payload": payload, "timeout_seconds": timeout_seconds})
            return {
                "status": "ok",
                "service": "control-plane",
                "action": "inspect",
                "result": {"present": True, "digest_verified": True},
            }, None

        self.patch_attr("runtime_agent_post", runtime_agent_post)

        with tempfile.TemporaryDirectory() as tmp:
            self.patch_attr("settings", replace(main.settings, data_root=tmp))
            compose_override = main.compose_override_policy.write_compose_image_override(
                data_root=Path(tmp),
                update_id=created["id"],
                target_version=created["target_version"],
                image_refs=created["image_refs"],
                image_stage=image_stage,
                created_at=datetime(2026, 7, 22, 12, 30, tzinfo=UTC),
            )
            fake.rows[created["id"]].update(
                {
                    "status": "validated",
                    "stage": "health_check_completed",
                    "image_stage": image_stage,
                    "compose_override": compose_override,
                }
            )

            promoted = asyncio.run(main.admin_update_promote(created["id"], main.UpdateActionRequest(reason="promote"), authorization="Bearer key"))

        self.assertEqual(promoted["status"], "promotion_ready")
        self.assertEqual(promoted["stage"], "promotion_handoff_ready")
        self.assertEqual(promoted["promotion_result"]["format"], "b1-ai-hub-update-promotion/v1")
        self.assertEqual(promoted["promotion_result"]["status"], "operator_action_required")
        self.assertIn("compose.images.yaml", promoted["promotion_result"]["promotion_command"]["shell"])
        self.assertEqual(promoted["promotion_result"]["image_inspect"][0]["status"], "ok")
        self.assertEqual(inspect_calls[0]["path"], "/v1/images/control-plane/inspect")
        self.assertEqual(fake.audit_events[-1]["event_type"], "update.promotion_ready")


if __name__ == "__main__":
    unittest.main()
