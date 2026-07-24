from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime, timedelta
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


class FakeMetricsDatabase:
    def __init__(self) -> None:
        now = datetime.now(tz=UTC)
        self.jobs = [
            {
                "id": "job_1",
                "state": "completed",
                "priority": "chat",
                "runtime": "localai",
                "resolved_model_version": "llm:v1",
                "created_at": now - timedelta(minutes=15),
                "started_at": now - timedelta(minutes=14),
                "completed_at": now - timedelta(minutes=13),
                "load_time_ms": 1000,
                "run_time_ms": 2000,
                "peak_vram_mib": 4096,
                "peak_ram_mib": 1024,
            },
            {
                "id": "job_2",
                "state": "queued",
                "priority": "single_image",
                "runtime": "comfyui",
                "resolved_model_version": "sdxl:v1",
                "created_at": now - timedelta(seconds=90),
            },
        ]

    async def list_jobs(self, limit: int = 500) -> list[dict[str, Any]]:
        return self.jobs[:limit]

    async def job_counts_by_state(self) -> list[dict[str, Any]]:
        return [{"state": "completed", "count": 1}, {"state": "queued", "count": 1}]

    async def list_runtime_states(self) -> list[dict[str, Any]]:
        return [{"runtime": "localai", "status": "idle", "stage": "idle", "active_model": "llm:v1"}]

    async def get_scheduler_owner(self) -> dict[str, Any]:
        return {"owner": "gpu-runner", "epoch": 1}


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class AdminMetricsApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_auth(self, auth: Any) -> None:
        async def authenticate(_: str | None = None) -> Any:
            return auth

        self.patch_attr("authenticate", authenticate)

    def test_admin_metrics_returns_database_and_runtime_agent_observability(self) -> None:
        self.patch_auth(AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:read"})))
        self.patch_attr("database", FakeMetricsDatabase())

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any] | None, str | None]:
            self.assertEqual(path, "/v1/metrics")
            return {
                "gpu": {
                    "available": True,
                    "devices": [
                        {
                            "memory_total_mib": 12288,
                            "memory_used_mib": 4096,
                            "memory_free_mib": 8192,
                            "utilization_gpu_percent": 55,
                            "temperature_c": 59,
                            "power_watts": 80,
                        }
                    ],
                },
                "memory": {"available": True, "total_bytes": 1024, "used_bytes": 256, "available_bytes": 768},
                "disks": [{"path": "/srv/b1-ai-hub", "available": True, "total_bytes": 100, "used_bytes": 10, "free_bytes": 90}],
                "cpu": {"available": True, "cpu_count": 12},
            }, None

        self.patch_attr("runtime_agent_get", runtime_agent_get)

        result = asyncio.run(main.admin_metrics(authorization="Bearer key", limit=500))

        self.assertEqual(result["queue"]["depth_total"], 1)
        self.assertEqual(result["jobs"]["completed_last_hour"], 1)
        self.assertEqual(result["jobs"]["peak_vram_mib"]["max"], 4096)
        self.assertEqual(result["scheduler_lease"]["owner"], "gpu-runner")
        self.assertTrue(result["runtime_agent"]["available"])
        self.assertEqual(result["gpu"]["memory_used_mib"], 4096)
        self.assertEqual(result["host"]["storage"]["free_bytes"], 90)

    def test_admin_metrics_prometheus_returns_authenticated_text_exposition(self) -> None:
        self.patch_auth(AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"admin:read"})))
        self.patch_attr("database", FakeMetricsDatabase())

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any] | None, str | None]:
            self.assertEqual(path, "/v1/metrics")
            return {
                "gpu": {"available": False, "devices": []},
                "memory": {"available": True, "total_bytes": 1024, "used_bytes": 256, "available_bytes": 768},
                "disks": [],
                "cpu": {"available": True, "cpu_count": 12},
            }, None

        self.patch_attr("runtime_agent_get", runtime_agent_get)

        response = asyncio.run(main.admin_metrics_prometheus(authorization="Bearer key", limit=500))
        body = response.body.decode("utf-8")

        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("# TYPE b1_ai_hub_queue_depth_total gauge", body)
        self.assertIn('b1_ai_hub_jobs_last_hour_total{status="completed"} 1', body)
        self.assertIn("b1_ai_hub_gpu_available 0", body)

    def test_admin_metrics_requires_admin_read_scope(self) -> None:
        self.patch_auth(AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"jobs:read"})))
        self.patch_attr("database", FakeMetricsDatabase())

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.admin_metrics(authorization="Bearer key", limit=500))
        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
