from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

from test_live_stack import LiveApiClient  # noqa: E402


RESTART_RECONCILIATION_EVIDENCE_FORMAT = "b1-ai-hub-restart-reconciliation-acceptance/v1"
RESTART_RECONCILIATION_REQUIRED_CHECKS = (
    "control_plane_restarted",
    "cpu_runner_reconciled",
    "gpu_runner_reconciled",
    "waiting_jobs_requeued",
    "active_jobs_marked_recovery_required",
    "interrupted_job_ids_recorded",
    "resumable_comfyui_native_prompts_reattached",
)


def int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@unittest.skipUnless(
    os.getenv("B1_RESTART_RECONCILIATION_LIVE_TEST") == "1",
    "set B1_RESTART_RECONCILIATION_LIVE_TEST=1 to run restart reconciliation acceptance",
)
class LiveRestartReconciliationAcceptanceTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        api_key = (
            os.getenv("B1_RESTART_RECONCILIATION_API_KEY")
            or os.getenv("B1_SMOKE_ADMIN_API_KEY")
            or os.getenv("B1_AI_HUB_API_KEY")
            or ""
        )
        if not api_key:
            raise unittest.SkipTest("set B1_RESTART_RECONCILIATION_API_KEY, B1_SMOKE_ADMIN_API_KEY, or B1_AI_HUB_API_KEY")
        started_after = os.getenv("B1_RESTART_RECONCILIATION_STARTED_AFTER", "").strip()
        if not started_after:
            raise AssertionError("set B1_RESTART_RECONCILIATION_STARTED_AFTER to the UTC timestamp captured before restarting control-plane")
        cls.started_after = parse_timestamp(started_after)
        tls_verify = os.getenv("B1_RESTART_RECONCILIATION_TLS_VERIFY", os.getenv("B1_SMOKE_TLS_VERIFY", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        cls.client = LiveApiClient(
            os.getenv("B1_RESTART_RECONCILIATION_API_BASE")
            or os.getenv("B1_SMOKE_API_BASE")
            or os.getenv("B1_AI_HUB_API_BASE")
            or "https://api.ai.b1.germering",
            api_key=api_key,
            host_header=os.getenv("B1_RESTART_RECONCILIATION_HOST_HEADER") or os.getenv("B1_SMOKE_HOST_HEADER", ""),
            timeout_seconds=float(os.getenv("B1_RESTART_RECONCILIATION_HTTP_TIMEOUT_SECONDS", os.getenv("B1_SMOKE_HTTP_TIMEOUT_SECONDS", "15"))),
            tls_verify=tls_verify,
            ca_file=os.getenv("B1_RESTART_RECONCILIATION_CA_FILE") or os.getenv("B1_SMOKE_CA_FILE", ""),
        )
        cls.minimum_requeued = int_env("B1_RESTART_RECONCILIATION_MIN_REQUEUED", 1)
        cls.minimum_marked_recovery_required = int_env("B1_RESTART_RECONCILIATION_MIN_MARKED_RECOVERY_REQUIRED", 1)
        cls.minimum_resumed_comfyui_native = int_env("B1_RESTART_RECONCILIATION_MIN_RESUMED_COMFYUI_NATIVE", 1)

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_RESTART_RECONCILIATION_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in RESTART_RECONCILIATION_REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
                {
                    "format": RESTART_RECONCILIATION_EVIDENCE_FORMAT,
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": cls.client.base_url,
                    "status": status,
                    "required_checks": list(RESTART_RECONCILIATION_REQUIRED_CHECKS),
                    "checks": cls.checks,
                    "samples": cls.samples,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def ok_record_by_runner(self, records: list[dict[str, Any]], runner: str) -> dict[str, Any]:
        for record in records:
            if record.get("runner") == runner:
                self.assertEqual(record.get("status"), "ok", record)
                self.assertTrue(record.get("started_at"), record)
                self.assertTrue(record.get("completed_at"), record)
                return record
        raise AssertionError(f"{runner} startup reconciliation record is absent")

    def test_control_plane_restart_reconciled_interrupted_jobs(self) -> None:
        status, _, payload = self.client.json_request("GET", "/admin/scheduler/reconciliation", require_auth=True)
        if status == 403:
            self.skipTest("provided restart reconciliation key lacks runtimes:read scope")
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)

        started_at = parse_timestamp(str(payload.get("control_plane_started_at") or ""))
        self.assertGreater(started_at, self.started_after)
        self.record_check(
            "control_plane_restarted",
            started_at=started_at.isoformat(),
            expected_after=self.started_after.isoformat(),
        )

        records = payload.get("records")
        self.assertIsInstance(records, list, payload)
        typed_records = [record for record in records if isinstance(record, dict)]
        required_runners = payload.get("required_runners") if isinstance(payload.get("required_runners"), list) else []
        self.assertIn("cpu-job-runner", required_runners, payload)
        self.assertIn("gpu-job-runner", required_runners, payload)

        cpu = self.ok_record_by_runner(typed_records, "cpu-job-runner")
        gpu = self.ok_record_by_runner(typed_records, "gpu-job-runner")
        self.record_check(
            "cpu_runner_reconciled",
            marked_recovery_required=int(cpu.get("marked_recovery_required") or 0),
            requeued=int(cpu.get("requeued") or 0),
            runtime_names=cpu.get("runtime_names"),
        )
        self.record_check(
            "gpu_runner_reconciled",
            marked_recovery_required=int(gpu.get("marked_recovery_required") or 0),
            requeued=int(gpu.get("requeued") or 0),
            runtime_names=gpu.get("runtime_names"),
        )

        total_requeued = sum(int(record.get("requeued") or 0) for record in typed_records)
        total_marked_recovery = sum(int(record.get("marked_recovery_required") or 0) for record in typed_records)
        self.assertGreaterEqual(total_requeued, self.minimum_requeued, typed_records)
        self.record_check(
            "waiting_jobs_requeued",
            observed=total_requeued,
            minimum=self.minimum_requeued,
        )
        self.assertGreaterEqual(total_marked_recovery, self.minimum_marked_recovery_required, typed_records)
        self.record_check(
            "active_jobs_marked_recovery_required",
            observed=total_marked_recovery,
            minimum=self.minimum_marked_recovery_required,
        )
        requeued_job_ids = [
            str(job_id)
            for record in typed_records
            for job_id in (record.get("requeued_job_ids") if isinstance(record.get("requeued_job_ids"), list) else [])
            if str(job_id)
        ]
        recovery_required_job_ids = [
            str(job_id)
            for record in typed_records
            for job_id in (
                record.get("recovery_required_job_ids") if isinstance(record.get("recovery_required_job_ids"), list) else []
            )
            if str(job_id)
        ]
        if self.minimum_requeued:
            self.assertGreaterEqual(len(requeued_job_ids), min(self.minimum_requeued, 50), typed_records)
        if self.minimum_marked_recovery_required:
            self.assertGreaterEqual(len(recovery_required_job_ids), min(self.minimum_marked_recovery_required, 50), typed_records)
        self.record_check(
            "interrupted_job_ids_recorded",
            requeued_job_ids=requeued_job_ids[:50],
            recovery_required_job_ids=recovery_required_job_ids[:50],
            requeued_sample_count=len(requeued_job_ids),
            recovery_required_sample_count=len(recovery_required_job_ids),
        )
        resume = payload.get("comfyui_native_prompt_resume")
        self.assertIsInstance(resume, dict, payload)
        resumed_comfyui_native = int(resume.get("resumed") or 0)
        self.assertGreaterEqual(resumed_comfyui_native, self.minimum_resumed_comfyui_native, resume)
        self.record_check(
            "resumable_comfyui_native_prompts_reattached",
            observed=resumed_comfyui_native,
            minimum=self.minimum_resumed_comfyui_native,
            checked=int(resume.get("checked") or 0),
            skipped=int(resume.get("skipped") or 0),
        )
        self.samples.append(
            {
                "label": "startup-reconciliation",
                "status": payload.get("status"),
                "control_plane_started_at": started_at.isoformat(),
                "required_runners": required_runners,
                "comfyui_native_prompt_resume": resume,
                "records": typed_records,
                "requeued_job_ids": requeued_job_ids[:50],
                "recovery_required_job_ids": recovery_required_job_ids[:50],
            }
        )
        self.samples.append(
            {
                "label": "recovered-job-counts",
                "total_requeued": total_requeued,
                "total_marked_recovery_required": total_marked_recovery,
                "resumed_comfyui_native_prompts": resumed_comfyui_native,
            }
        )


if __name__ == "__main__":
    unittest.main()
