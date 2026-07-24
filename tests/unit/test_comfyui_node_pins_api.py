from __future__ import annotations

import asyncio
import json
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


class FakeNodePinDatabase:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

    async def list_comfyui_node_pins(self) -> list[dict[str, Any]]:
        return [dict(row) for _key, row in sorted(self.rows.items())]

    async def get_comfyui_node_pin(self, node_id: str, commit: str) -> dict[str, Any] | None:
        row = self.rows.get((node_id, commit))
        return dict(row) if row else None

    async def upsert_comfyui_node_pin(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = (payload["node_id"], payload["commit"])
        existing = self.rows.get(key)
        row = {
            "created_at": existing["created_at"] if existing else self.now,
            "updated_at": self.now,
            "display_name": None,
            "status": "approved",
            "approved_by": None,
            "approved_at": None,
            "dependency_lock_sha256": None,
            "allowed_route_prefixes": [],
            "notes": None,
            **payload,
        }
        self.rows[key] = row
        return dict(row)


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ComfyUiNodePinsApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_common(self, registry_path: Path, fake_database: FakeNodePinDatabase, auth: Any, audit_events: list[dict[str, Any]]) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return auth

        async def fake_record_audit_event(auth_context: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        async def fake_refresh_workflow_dependency_statuses() -> dict[str, Any]:
            return {"refreshed": ["wf@1.0.0"], "count": 1}

        self.patch_attr("settings", replace(main.settings, comfyui_node_pin_registry=str(registry_path)))
        self.patch_attr("database", fake_database)
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("refresh_workflow_dependency_statuses", fake_refresh_workflow_dependency_statuses)
        self.patch_attr("approved_node_pins", None)

    def write_seed_registry(self, path: Path, *, commit: str = "a" * 40) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "comfyui-impact-pack",
                            "commit": commit,
                            "repository_url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                            "status": "approved",
                            "dependency_lock_sha256": "b" * 64,
                            "allowed_route_prefixes": ["impact/wildcards"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_lists_seed_and_database_pins_with_database_override(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "pins.json"
            commit = "a" * 40
            self.write_seed_registry(registry_path, commit=commit)
            fake_database = FakeNodePinDatabase()
            fake_database.rows[("comfyui-impact-pack", commit)] = {
                "node_id": "comfyui-impact-pack",
                "commit": commit,
                "repository_url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                "display_name": "Impact Pack",
                "status": "disabled",
                "approved_by": "admin_1",
                "approved_at": datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
                "dependency_lock_sha256": None,
                "allowed_route_prefixes": [],
                "notes": "disabled after review",
                "created_at": datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
            }
            auth = AuthContext(subject_id="creator_1", role=Role.CREATOR, scopes=frozenset({"workflows:read"}))
            self.patch_common(registry_path, fake_database, auth, audit_events)

            payload = asyncio.run(main.admin_comfyui_node_pins())

        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(payload["data"][0]["source"], "database")
        self.assertEqual(payload["data"][0]["status"], "disabled")
        self.assertEqual(main.approved_node_pin("comfyui-impact-pack", commit).status, "disabled")

    def test_admin_can_create_node_pin_and_refresh_workflows(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "pins.json"
            registry_path.write_text('{"schema_version":1,"nodes":[]}', encoding="utf-8")
            fake_database = FakeNodePinDatabase()
            auth = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"workflows:write"}))
            self.patch_common(registry_path, fake_database, auth, audit_events)

            payload = asyncio.run(
                main.admin_comfyui_node_pin_create(
                    main.ComfyUiNodePinCreate(
                        id="comfyui-impact-pack",
                        commit="b" * 40,
                        repository_url="https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                        dependency_lock_sha256="c" * 64,
                        allowed_route_prefixes=["impact/wildcards"],
                    )
                )
            )

        self.assertEqual(payload["pin"]["source"], "database")
        self.assertEqual(payload["dependency_refresh"]["count"], 1)
        self.assertIn(("comfyui-impact-pack", "b" * 40), fake_database.rows)
        self.assertEqual(audit_events[0]["event_type"], "comfyui_node_pin.approved")
        self.assertEqual(main.approved_node_pin("comfyui-impact-pack", "b" * 40).allowed_route_prefixes, ["impact/wildcards"])

    def test_create_requires_administrator_role_and_strict_validation(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "pins.json"
            registry_path.write_text('{"schema_version":1,"nodes":[]}', encoding="utf-8")
            fake_database = FakeNodePinDatabase()
            operator = AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"workflows:write"}))
            self.patch_common(registry_path, fake_database, operator, audit_events)

            with self.assertRaises(HTTPException) as denied:
                asyncio.run(
                    main.admin_comfyui_node_pin_create(
                        main.ComfyUiNodePinCreate(
                            id="comfyui-impact-pack",
                            commit="b" * 40,
                            repository_url="https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                        )
                    )
                )

        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(fake_database.rows, {})

        audit_events = []
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "pins.json"
            registry_path.write_text('{"schema_version":1,"nodes":[]}', encoding="utf-8")
            fake_database = FakeNodePinDatabase()
            admin = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"workflows:write"}))
            self.patch_common(registry_path, fake_database, admin, audit_events)

            with self.assertRaises(HTTPException) as invalid:
                asyncio.run(
                    main.admin_comfyui_node_pin_create(
                        main.ComfyUiNodePinCreate(
                            id="comfyui-impact-pack",
                            commit="b" * 40,
                            repository_url="http://github.com/ltdrdata/ComfyUI-Impact-Pack",
                        )
                    )
                )

        self.assertEqual(invalid.exception.status_code, 422)
        self.assertIn("HTTPS URL", invalid.exception.detail["message"])
        self.assertEqual(fake_database.rows, {})

    def test_create_rejects_approved_route_prefix_without_dependency_lock(self) -> None:
        audit_events: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "pins.json"
            registry_path.write_text('{"schema_version":1,"nodes":[]}', encoding="utf-8")
            fake_database = FakeNodePinDatabase()
            admin = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"workflows:write"}))
            self.patch_common(registry_path, fake_database, admin, audit_events)

            with self.assertRaises(HTTPException) as invalid:
                asyncio.run(
                    main.admin_comfyui_node_pin_create(
                        main.ComfyUiNodePinCreate(
                            id="comfyui-custom-api",
                            commit="b" * 40,
                            repository_url="https://github.com/example/comfyui-custom-api",
                            allowed_route_prefixes=["custom/api"],
                        )
                    )
                )

        self.assertEqual(invalid.exception.status_code, 422)
        self.assertIn("dependency_lock_sha256 is required", invalid.exception.detail["message"])
        self.assertEqual(fake_database.rows, {})
        self.assertEqual(audit_events, [])


if __name__ == "__main__":
    unittest.main()
