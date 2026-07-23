from __future__ import annotations

import asyncio
import sys
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


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class AdmissionPolicyApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def policy_request(self, **overrides: Any) -> Any:
        values = {
            "max_queued_jobs_per_owner": 20,
            "max_active_jobs_per_owner": 3,
            "max_jobs_per_hour_per_owner": 60,
            "max_queued_jobs_global": 100,
            "artifact_storage_max_bytes": 0,
            "artifact_storage_reserve_bytes": 10 * 1024**3,
            **overrides,
        }
        return main.AdmissionPolicyUpdateRequest(**values)

    def patch_common(
        self,
        store: dict[str, dict[str, Any]],
        audit_events: list[dict[str, Any]],
        *,
        role: Any = None,
        scopes: frozenset[str] = frozenset({"*"}),
    ) -> None:
        role = role or Role.ADMIN

        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=role, scopes=scopes)

        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        async def fake_get_admission_policy_record(policy_id: str = "default") -> dict[str, Any] | None:
            return store.get(policy_id)

        async def fake_upsert_admission_policy_record(payload: dict[str, Any], policy_id: str = "default") -> dict[str, Any]:
            now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
            existing = store.get(policy_id)
            row = {
                "id": policy_id,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
                **payload,
            }
            store[policy_id] = row
            return row

        async def fake_delete_admission_policy_record(policy_id: str = "default") -> dict[str, Any] | None:
            return store.pop(policy_id, None)

        self.patch_attr(
            "settings",
            replace(
                main.settings,
                max_queued_jobs_per_owner=20,
                max_active_jobs_per_owner=3,
                max_jobs_per_hour_per_owner=60,
                max_queued_jobs_global=100,
                artifact_storage_max_bytes=0,
                artifact_storage_reserve_bytes=10 * 1024**3,
            ),
        )
        self.patch_attr("admission_policy_override", None)
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr(
            "database",
            type(
                "FakeDatabase",
                (),
                {
                    "get_admission_policy_record": staticmethod(fake_get_admission_policy_record),
                    "upsert_admission_policy_record": staticmethod(fake_upsert_admission_policy_record),
                    "delete_admission_policy_record": staticmethod(fake_delete_admission_policy_record),
                },
            ),
        )

    def test_validate_update_and_reset_admission_policy(self) -> None:
        store: dict[str, dict[str, Any]] = {}
        audit_events: list[dict[str, Any]] = []
        self.patch_common(store, audit_events)

        validation = asyncio.run(main.admin_admission_policy_validate(self.policy_request()))
        self.assertTrue(validation["accepted"])

        updated = asyncio.run(main.admin_admission_policy_update(self.policy_request(max_queued_jobs_per_owner=25, max_queued_jobs_global=125)))
        self.assertEqual(updated["source"], "database")
        self.assertEqual(updated["effective"]["max_queued_jobs_per_owner"], 25)
        self.assertEqual(store["default"]["updated_by"], "admin_1")

        rejected = asyncio.run(main.admin_admission_policy_validate(self.policy_request(max_active_jobs_per_owner=21)))
        self.assertFalse(rejected["accepted"])
        self.assertTrue(any("max_active_jobs_per_owner" in error for error in rejected["errors"]))

        with self.assertRaises(HTTPException) as exc:
            asyncio.run(main.admin_admission_policy_update(self.policy_request(max_queued_jobs_per_owner=1001)))
        self.assertEqual(exc.exception.status_code, 422)

        reset = asyncio.run(main.admin_admission_policy_reset())
        self.assertEqual(reset["source"], "environment")
        self.assertNotIn("default", store)
        self.assertEqual([item["event_type"] for item in audit_events], ["admission_policy.updated", "admission_policy.reset"])

    def test_get_loads_existing_policy_override(self) -> None:
        store = {
            "default": {
                "id": "default",
                "max_queued_jobs_per_owner": 15,
                "max_active_jobs_per_owner": 2,
                "max_jobs_per_hour_per_owner": 30,
                "max_queued_jobs_global": 80,
                "artifact_storage_max_bytes": 0,
                "artifact_storage_reserve_bytes": 8 * 1024**3,
                "updated_by": "admin_1",
                "created_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            }
        }
        self.patch_common(store, [])

        payload = asyncio.run(main.admin_admission_policy_get())

        self.assertEqual(payload["source"], "database")
        self.assertEqual(payload["effective"]["max_active_jobs_per_owner"], 2)
        self.assertEqual(main.admission_policy().max_active_jobs_per_owner, 2)

    def test_admission_policy_requires_admin_role(self) -> None:
        self.patch_common({}, [], role=Role.OPERATOR, scopes=frozenset({"admin:read"}))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_admission_policy_get())

        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
