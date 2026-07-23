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
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
    from app.executor import GpuJobRunner  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    GpuJobRunner = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ResourcePolicyApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def policy_request(self, **overrides: Any) -> Any:
        values = {
            "gpu_total_vram_gib": 12.0,
            "gpu_usable_vram_gib": 10.0,
            "gpu_reserve_vram_gib": 2.0,
            "gpu_max_active_pipelines": 1,
            "host_total_ram_gib": 32.0,
            "host_reserve_ram_gib": 6.0,
            "llm_default_context": 8192,
            "llm_maximum_context": 16384,
            "llm_default_parallel_requests": 1,
            "comfyui_maximum_parallel_jobs": 1,
            "comfyui_maximum_batch_size": 1,
            **overrides,
        }
        return main.ResourcePolicyUpdateRequest(**values)

    def patch_common(
        self,
        store: dict[str, dict[str, Any]],
        audit_events: list[dict[str, Any]],
        *,
        role: Any = None,
        scopes: frozenset[str] = frozenset({"*"}),
    ) -> GpuJobRunner:
        role = role or Role.ADMIN

        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=role, scopes=scopes)

        async def fake_record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        async def fake_refresh_catalog_cache() -> dict[str, Any]:
            return {"status": "refreshed"}

        async def fake_get_resource_policy_record(policy_id: str = "default") -> dict[str, Any] | None:
            return store.get(policy_id)

        async def fake_upsert_resource_policy_record(payload: dict[str, Any], policy_id: str = "default") -> dict[str, Any]:
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

        async def fake_delete_resource_policy_record(policy_id: str = "default") -> dict[str, Any] | None:
            return store.pop(policy_id, None)

        runner = GpuJobRunner(Path(tempfile.gettempdir()), reserve_vram_gib=1.5)
        self.patch_attr("settings", replace(main.settings, gpu_total_vram_gib=12.0, host_total_ram_gib=32.0))
        self.patch_attr("resource_policy_override", None)
        self.patch_attr("job_runners", [runner])
        self.patch_attr("authenticate", fake_authenticate)
        self.patch_attr("record_audit_event", fake_record_audit_event)
        self.patch_attr("refresh_catalog_cache", fake_refresh_catalog_cache)
        self.patch_attr("database", type("FakeDatabase", (), {
            "get_resource_policy_record": staticmethod(fake_get_resource_policy_record),
            "upsert_resource_policy_record": staticmethod(fake_upsert_resource_policy_record),
            "delete_resource_policy_record": staticmethod(fake_delete_resource_policy_record),
        }))
        return runner

    def test_validate_update_and_reset_resource_policy(self) -> None:
        store: dict[str, dict[str, Any]] = {}
        audit_events: list[dict[str, Any]] = []
        runner = self.patch_common(store, audit_events)

        validation = asyncio.run(main.admin_resource_policy_validate(self.policy_request()))
        self.assertTrue(validation["accepted"])

        updated = asyncio.run(main.admin_resource_policy_update(self.policy_request()))
        self.assertEqual(updated["source"], "database")
        self.assertEqual(store["default"]["updated_by"], "admin_1")
        self.assertEqual(runner.reserve_vram_mib, 2048)

        rejected = asyncio.run(main.admin_resource_policy_validate(self.policy_request(gpu_total_vram_gib=13.0)))
        self.assertFalse(rejected["accepted"])
        self.assertTrue(any("gpu_total_vram_gib" in error for error in rejected["errors"]))

        with self.assertRaises(HTTPException) as exc:
            asyncio.run(main.admin_resource_policy_update(self.policy_request(gpu_max_active_pipelines=2)))
        self.assertEqual(exc.exception.status_code, 422)

        reset = asyncio.run(main.admin_resource_policy_reset())
        self.assertEqual(reset["source"], "environment")
        self.assertNotIn("default", store)
        self.assertEqual(runner.reserve_vram_mib, 1536)
        self.assertEqual([item["event_type"] for item in audit_events], ["resource_policy.updated", "resource_policy.reset"])

    def test_resource_policy_requires_admin_role(self) -> None:
        store: dict[str, dict[str, Any]] = {}
        audit_events: list[dict[str, Any]] = []
        self.patch_common(store, audit_events, role=Role.OPERATOR, scopes=frozenset({"admin:read"}))

        with self.assertRaises(HTTPException) as exc:
            asyncio.run(main.admin_resource_policy_get())

        self.assertEqual(exc.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
