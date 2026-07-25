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
    from app.executor import CpuJobRunner, GpuJobRunner, ModelDownloadRunner  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    CpuJobRunner = None  # type: ignore[assignment]
    GpuJobRunner = None  # type: ignore[assignment]
    ModelDownloadRunner = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class SchedulerReconciliationApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_auth(self, scopes: frozenset[str]) -> None:
        async def fake_authenticate(authorization: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=Role.OPERATOR, scopes=scopes)

        self.patch_attr("authenticate", fake_authenticate)

    def test_scheduler_reconciliation_endpoint_reports_required_runner_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cpu_runner = CpuJobRunner(root)
            gpu_runner = GpuJobRunner(root)
            download_runner = ModelDownloadRunner(root)
            cpu_runner.startup_reconciliation = {
                "status": "ok",
                "runtime_names": ["audio-cpu"],
                "started_at": "2026-07-24T12:00:00+00:00",
                "completed_at": "2026-07-24T12:00:01+00:00",
                "marked_recovery_required": 1,
                "requeued": 2,
                "recovery_required_job_ids": ["job_cpu_active"],
                "requeued_job_ids": ["job_cpu_waiting_1", "job_cpu_waiting_2"],
            }
            gpu_runner.startup_reconciliation = {
                "status": "ok",
                "runtime_names": ["localai", "comfyui", "voicebox"],
                "started_at": "2026-07-24T12:00:00+00:00",
                "completed_at": "2026-07-24T12:00:02+00:00",
                "marked_recovery_required": 3,
                "requeued": 4,
                "recovery_required_job_ids": ["job_gpu_active_1", "job_gpu_active_2", "job_gpu_active_3"],
                "requeued_job_ids": ["job_gpu_waiting_1", "job_gpu_waiting_2", "job_gpu_waiting_3", "job_gpu_waiting_4"],
            }
            download_runner.startup_reconciliation = {
                "status": "ok",
                "runtime_names": ["model-download"],
                "started_at": "2026-07-24T12:00:00+00:00",
                "completed_at": "2026-07-24T12:00:03+00:00",
                "marked_recovery_required": 0,
                "requeued": 5,
                "paused": 1,
                "cancelled": 2,
                "requeued_download_ids": ["modeldl_running_1"],
                "paused_download_ids": ["modeldl_pausing_1"],
                "cancelled_download_ids": ["modeldl_cancelling_1", "modeldl_cancelling_2"],
            }
            self.patch_attr(
                "settings",
                replace(main.settings, job_runner_enabled=True, gpu_job_runner_enabled=True, model_download_runner_enabled=True),
            )
            self.patch_attr("job_runners", [cpu_runner, gpu_runner, download_runner])
            self.patch_attr("control_plane_started_at", datetime(2026, 7, 24, 12, 0, tzinfo=UTC))
            self.patch_attr("comfyui_native_prompt_resume", {"checked": 2, "resumed": 2, "skipped": 0})
            self.patch_auth(frozenset({"runtimes:read"}))

            result = asyncio.run(main.admin_scheduler_reconciliation_get(authorization="Bearer key"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["control_plane_started_at"], "2026-07-24T12:00:00+00:00")
        self.assertEqual(result["required_runners"], ["cpu-job-runner", "gpu-job-runner", "model-download-runner"])
        self.assertEqual(result["missing_required_runners"], [])
        self.assertEqual(result["comfyui_native_prompt_resume"], {"checked": 2, "resumed": 2, "skipped": 0})
        records = {record["runner"]: record for record in result["records"]}
        self.assertEqual(records["cpu-job-runner"]["requeued"], 2)
        self.assertEqual(records["cpu-job-runner"]["requeued_job_ids"], ["job_cpu_waiting_1", "job_cpu_waiting_2"])
        self.assertEqual(records["gpu-job-runner"]["marked_recovery_required"], 3)
        self.assertEqual(records["gpu-job-runner"]["recovery_required_job_ids"], ["job_gpu_active_1", "job_gpu_active_2", "job_gpu_active_3"])
        self.assertEqual(records["model-download-runner"]["requeued"], 5)
        self.assertEqual(records["model-download-runner"]["paused"], 1)
        self.assertEqual(records["model-download-runner"]["cancelled"], 2)
        self.assertEqual(records["model-download-runner"]["cancelled_download_ids"], ["modeldl_cancelling_1", "modeldl_cancelling_2"])

    def test_scheduler_reconciliation_endpoint_reports_missing_required_runner(self) -> None:
        self.patch_attr(
            "settings",
            replace(main.settings, job_runner_enabled=True, gpu_job_runner_enabled=True, model_download_runner_enabled=True),
        )
        self.patch_attr("job_runners", [])
        self.patch_attr("control_plane_started_at", None)
        self.patch_auth(frozenset({"runtimes:read"}))

        result = asyncio.run(main.admin_scheduler_reconciliation_get(authorization="Bearer key"))

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["missing_required_runners"], ["cpu-job-runner", "gpu-job-runner", "model-download-runner"])
        self.assertEqual(result["comfyui_native_prompt_resume"], {"checked": 0, "resumed": 0, "skipped": 0})

    def test_scheduler_reconciliation_endpoint_requires_runtime_scope(self) -> None:
        self.patch_auth(frozenset({"models:read"}))

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(main.admin_scheduler_reconciliation_get(authorization="Bearer key"))

        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
