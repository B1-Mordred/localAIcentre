from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "control-plane"))
sys.path.insert(0, str(ROOT / "tests" / "smoke"))

from app.auth import generate_api_key, hash_api_key  # noqa: E402
from tests.support.evidence import write_private_json  # noqa: E402
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
DRILL_OWNER_ID = "restart-reconciliation-drill"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    drill_id: str = ""
    drill_job_ids: list[str] = []
    temporary_api_client_id: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.checks = {}
            cls.samples = []
            api_key = (
                os.getenv("B1_RESTART_RECONCILIATION_API_KEY")
                or os.getenv("B1_SMOKE_ADMIN_API_KEY")
                or os.getenv("B1_AI_HUB_API_KEY")
                or ""
            )
            auto_drill = env_flag("B1_RESTART_RECONCILIATION_AUTO_DRILL", False)
            if not api_key and auto_drill:
                api_key = cls.create_temporary_api_key()
            if not api_key:
                raise unittest.SkipTest("set B1_RESTART_RECONCILIATION_API_KEY, B1_SMOKE_ADMIN_API_KEY, or B1_AI_HUB_API_KEY")
            started_after = os.getenv("B1_RESTART_RECONCILIATION_STARTED_AFTER", "").strip()
            if started_after:
                cls.started_after = parse_timestamp(started_after)
            elif auto_drill:
                cls.started_after = cls.prepare_restart_drill()
            else:
                raise AssertionError(
                    "set B1_RESTART_RECONCILIATION_STARTED_AFTER to the UTC timestamp captured before restarting control-plane "
                    "or set B1_RESTART_RECONCILIATION_AUTO_DRILL=1"
                )
            tls_verify = (
                os.getenv("B1_RESTART_RECONCILIATION_TLS_VERIFY", os.getenv("B1_SMOKE_TLS_VERIFY", "1")).strip().lower()
                not in {
                    "0",
                    "false",
                    "no",
                }
            )
            cls.client = LiveApiClient(
                os.getenv("B1_RESTART_RECONCILIATION_API_BASE")
                or os.getenv("B1_SMOKE_API_BASE")
                or os.getenv("B1_AI_HUB_API_BASE")
                or "https://api.ai.b1.germering",
                api_key=api_key,
                host_header=os.getenv("B1_RESTART_RECONCILIATION_HOST_HEADER") or os.getenv("B1_SMOKE_HOST_HEADER", ""),
                timeout_seconds=float(
                    os.getenv("B1_RESTART_RECONCILIATION_HTTP_TIMEOUT_SECONDS", os.getenv("B1_SMOKE_HTTP_TIMEOUT_SECONDS", "15"))
                ),
                tls_verify=tls_verify,
                ca_file=os.getenv("B1_RESTART_RECONCILIATION_CA_FILE") or os.getenv("B1_SMOKE_CA_FILE", ""),
            )
            cls.minimum_requeued = int_env("B1_RESTART_RECONCILIATION_MIN_REQUEUED", 1)
            cls.minimum_marked_recovery_required = int_env("B1_RESTART_RECONCILIATION_MIN_MARKED_RECOVERY_REQUIRED", 1)
            cls.minimum_resumed_comfyui_native = int_env("B1_RESTART_RECONCILIATION_MIN_RESUMED_COMFYUI_NATIVE", 1)
        except Exception:
            cls.cleanup_restart_drill()
            raise

    @classmethod
    def run_compose(cls, args: list[str], *, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", *args]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"{' '.join(command)} failed with exit code {completed.returncode}\n"
                f"stdout:\n{completed.stdout[-4000:]}\n"
                f"stderr:\n{completed.stderr[-4000:]}"
            )
        return completed

    @classmethod
    def run_control_plane_python(cls, code: str, *, extra_env: dict[str, str] | None = None, timeout: int = 120) -> str:
        args = ["run", "--rm", "--no-deps", "--entrypoint", "python", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        for key, value in sorted((extra_env or {}).items()):
            args.extend(["-e", f"{key}={value}"])
        args.extend(["control-plane", "-"])
        completed = cls.run_compose(args, input_text=code, timeout=timeout)
        return completed.stdout.strip()

    @classmethod
    def create_temporary_api_key(cls) -> str:
        cls.temporary_api_client_id = f"restart-reconciliation-drill-{uuid.uuid4().hex[:12]}"
        key_prefix, api_key = generate_api_key("b1k_restart")
        key_salt, key_hash = hash_api_key(api_key)
        create_code = textwrap.dedent(
            """
            import asyncio
            import os

            from app import database
            from app.settings import load_settings

            async def main():
                settings = load_settings()
                database.configure_engine(settings.database_url)
                await database.insert_api_client(
                    {
                        "id": os.environ["B1_RESTART_TEMP_CLIENT_ID"],
                        "display_name": "Restart reconciliation drill",
                        "role": "operator",
                        "scopes": ["runtimes:read"],
                        "key_prefix": os.environ["B1_RESTART_TEMP_KEY_PREFIX"],
                        "key_salt": os.environ["B1_RESTART_TEMP_KEY_SALT"],
                        "key_hash": os.environ["B1_RESTART_TEMP_KEY_HASH"],
                        "cidr_allowlist": [],
                    }
                )

            asyncio.run(main())
            """
        )
        cls.run_control_plane_python(
            create_code,
            extra_env={
                "B1_RESTART_TEMP_CLIENT_ID": cls.temporary_api_client_id,
                "B1_RESTART_TEMP_KEY_PREFIX": key_prefix,
                "B1_RESTART_TEMP_KEY_SALT": key_salt,
                "B1_RESTART_TEMP_KEY_HASH": key_hash,
            },
            timeout=120,
        )
        return api_key

    @classmethod
    def prepare_restart_drill(cls) -> datetime:
        cls.drill_id = f"restart-drill-{uuid.uuid4().hex[:12]}"
        cls.drill_job_ids = [
            f"{cls.drill_id}-cpu-waiting",
            f"{cls.drill_id}-gpu-waiting",
            f"{cls.drill_id}-gpu-active",
            f"{cls.drill_id}-comfy-native",
        ]
        seed_code = textwrap.dedent(
            """
            import asyncio
            import json
            import os
            from datetime import UTC, datetime, timedelta

            from app import database
            from app.settings import load_settings

            async def main():
                settings = load_settings()
                database.configure_engine(settings.database_url)
                drill_id = os.environ["B1_RESTART_DRILL_ID"]
                owner_id = os.environ["B1_RESTART_DRILL_OWNER_ID"]
                now = datetime.now(tz=UTC)
                rows = [
                    {
                        "id": f"{drill_id}-cpu-waiting",
                        "correlation_id": f"{drill_id}-cpu-waiting",
                        "owner_id": owner_id,
                        "modality": "audio",
                        "operation": "speech",
                        "model_alias": "tts-fast",
                        "resolved_model_version": "b1-restart-drill-audio-cpu",
                        "runtime": "audio-cpu",
                        "priority": "interactive_audio",
                        "state": "waiting_for_gpu",
                        "stage": "waiting_for_gpu",
                        "progress": 20,
                    },
                    {
                        "id": f"{drill_id}-gpu-waiting",
                        "correlation_id": f"{drill_id}-gpu-waiting",
                        "owner_id": owner_id,
                        "modality": "image",
                        "operation": "text-to-image",
                        "model_alias": "image-default",
                        "resolved_model_version": "b1-restart-drill-localai",
                        "runtime": "localai",
                        "priority": "single_image",
                        "state": "waiting_for_gpu",
                        "stage": "waiting_for_gpu",
                        "progress": 20,
                    },
                    {
                        "id": f"{drill_id}-gpu-active",
                        "correlation_id": f"{drill_id}-gpu-active",
                        "owner_id": owner_id,
                        "modality": "image",
                        "operation": "text-to-image",
                        "model_alias": "image-default",
                        "resolved_model_version": "b1-restart-drill-localai",
                        "runtime": "localai",
                        "priority": "single_image",
                        "state": "running",
                        "stage": "running",
                        "progress": 50,
                        "started_at": now - timedelta(seconds=30),
                    },
                    {
                        "id": f"{drill_id}-comfy-native",
                        "correlation_id": f"{drill_id}-comfy-native",
                        "owner_id": owner_id,
                        "modality": "workflow",
                        "operation": "comfyui-prompt",
                        "model_alias": "comfyui-native",
                        "resolved_model_version": "comfyui-native",
                        "runtime": "comfyui",
                        "priority": "single_image",
                        "state": "running",
                        "stage": "running",
                        "progress": 50,
                        "native_prompt_id": f"{drill_id}-native-prompt",
                        "started_at": now - timedelta(seconds=30),
                    },
                ]
                for row in rows:
                    row["request_params"] = {"restart_reconciliation_drill": True, "drill_id": drill_id}
                    row["redacted_request"] = {"restart_reconciliation_drill": True, "drill_id": drill_id}
                    await database.insert_job(row)
                print(json.dumps({"drill_id": drill_id, "job_ids": [row["id"] for row in rows]}, sort_keys=True))

            asyncio.run(main())
            """
        )
        try:
            cls.run_compose(["stop", "control-plane"], timeout=60)
            cls.run_control_plane_python(
                seed_code,
                extra_env={"B1_RESTART_DRILL_ID": cls.drill_id, "B1_RESTART_DRILL_OWNER_ID": DRILL_OWNER_ID},
                timeout=120,
            )
            expected_after = datetime.now(tz=UTC)
            cls.run_compose(["up", "-d", "control-plane"], timeout=180)
            return expected_after
        except Exception:
            cls.run_compose(["up", "-d", "control-plane"], timeout=180)
            raise

    @classmethod
    def cleanup_restart_drill(cls) -> None:
        if not cls.drill_id and not cls.temporary_api_client_id:
            return
        cleanup_code = textwrap.dedent(
            """
            import asyncio
            import os

            from app import database
            from app.settings import load_settings

            async def main():
                settings = load_settings()
                database.configure_engine(settings.database_url)
                drill_id = os.environ["B1_RESTART_DRILL_ID"]
                owner_id = os.environ["B1_RESTART_DRILL_OWNER_ID"]
                if drill_id and os.environ.get("B1_RESTART_KEEP_DRILL_JOBS") != "1":
                    prefix = f"{drill_id}-"
                    for row in await database.list_jobs(limit=100, owner_id=owner_id):
                        job_id = str(row.get("id") or "")
                        if not job_id.startswith(prefix):
                            continue
                        await database.update_job(
                            job_id,
                            state="cancelled",
                            stage="restart_reconciliation_drill_cleaned_up",
                            progress=100,
                            failure_category="restart_reconciliation_drill_cleanup",
                            failure_message="Bounded restart reconciliation drill row cleaned up after evidence capture",
                        )
                client_id = os.environ.get("B1_RESTART_TEMP_CLIENT_ID", "")
                if client_id:
                    await database.revoke_api_client(client_id)

            asyncio.run(main())
            """
        )
        try:
            cls.run_control_plane_python(
                cleanup_code,
                extra_env={
                    "B1_RESTART_DRILL_ID": cls.drill_id,
                    "B1_RESTART_DRILL_OWNER_ID": DRILL_OWNER_ID,
                    "B1_RESTART_KEEP_DRILL_JOBS": "1" if env_flag("B1_RESTART_RECONCILIATION_KEEP_DRILL_JOBS", False) else "0",
                    "B1_RESTART_TEMP_CLIENT_ID": cls.temporary_api_client_id,
                },
                timeout=120,
            )
        except Exception as exc:  # pragma: no cover - cleanup should not hide evidence.
            cls.samples.append({"label": "restart-drill-cleanup", "status": "failed", "error": exc.__class__.__name__})

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_RESTART_RECONCILIATION_EVIDENCE", "").strip()
        try:
            if evidence_path:
                path = Path(evidence_path)
                status = (
                    "ok"
                    if all(cls.checks.get(name, {}).get("status") == "ok" for name in RESTART_RECONCILIATION_REQUIRED_CHECKS)
                    else "incomplete"
                )
                write_private_json(
                    path,
                    {
                        "format": RESTART_RECONCILIATION_EVIDENCE_FORMAT,
                        "generated_at": datetime.now(tz=UTC).isoformat(),
                        "base_url": cls.client.base_url,
                        "status": status,
                        "required_checks": list(RESTART_RECONCILIATION_REQUIRED_CHECKS),
                        "checks": cls.checks,
                        "samples": cls.samples,
                    },
                )
        finally:
            cls.cleanup_restart_drill()

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
                started_at = parse_timestamp(str(record["started_at"]))
                completed_at = parse_timestamp(str(record["completed_at"]))
                self.assertGreater(started_at, self.started_after, record)
                self.assertGreaterEqual(completed_at, started_at, record)
                return record
        raise AssertionError(f"{runner} startup reconciliation record is absent")

    def test_control_plane_restart_reconciled_interrupted_jobs(self) -> None:
        payload: dict[str, Any] = {}
        status = 0
        last_error = ""
        deadline = time.monotonic() + float(os.getenv("B1_RESTART_RECONCILIATION_READY_TIMEOUT_SECONDS", "45"))
        while True:
            try:
                status, _, response_payload = self.client.json_request("GET", "/admin/scheduler/reconciliation", require_auth=True)
                payload = response_payload if isinstance(response_payload, dict) else {"response": response_payload}
                last_error = ""
            except AssertionError as exc:
                status = 0
                payload = {}
                last_error = str(exc)
            if status == 403:
                self.skipTest("provided restart reconciliation key lacks runtimes:read scope")
            if status == 200 and payload.get("status") == "ok":
                records = payload.get("records") if isinstance(payload.get("records"), list) else []
                if all(isinstance(record, dict) and record.get("status") == "ok" for record in records):
                    break
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        if status != 200:
            self.fail(f"GET /admin/scheduler/reconciliation did not become ready; last_status={status} payload={payload} error={last_error}")
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("status"), "ok", payload)

        started_at = parse_timestamp(str(payload.get("control_plane_started_at") or ""))
        self.assertGreater(started_at, self.started_after)
        required_runners = payload.get("required_runners") if isinstance(payload.get("required_runners"), list) else []
        missing_required_runners = (
            payload.get("missing_required_runners") if isinstance(payload.get("missing_required_runners"), list) else []
        )
        self.assertIn("cpu-job-runner", required_runners, payload)
        self.assertIn("gpu-job-runner", required_runners, payload)
        self.assertEqual(missing_required_runners, [], payload)
        self.record_check(
            "control_plane_restarted",
            started_at=started_at.isoformat(),
            expected_after=self.started_after.isoformat(),
            api_status=payload.get("status"),
            required_runners=required_runners,
            missing_required_runners=missing_required_runners,
            record_count=len(payload.get("records") if isinstance(payload.get("records"), list) else []),
        )

        records = payload.get("records")
        self.assertIsInstance(records, list, payload)
        typed_records = [record for record in records if isinstance(record, dict)]

        cpu = self.ok_record_by_runner(typed_records, "cpu-job-runner")
        gpu = self.ok_record_by_runner(typed_records, "gpu-job-runner")
        self.record_check(
            "cpu_runner_reconciled",
            runner="cpu-job-runner",
            runner_status=cpu.get("status"),
            required_runner_present="cpu-job-runner" in required_runners,
            started_at=cpu.get("started_at"),
            completed_at=cpu.get("completed_at"),
            marked_recovery_required=int(cpu.get("marked_recovery_required") or 0),
            requeued=int(cpu.get("requeued") or 0),
            runtime_names=cpu.get("runtime_names"),
            requeued_job_ids=cpu.get("requeued_job_ids") if isinstance(cpu.get("requeued_job_ids"), list) else [],
            recovery_required_job_ids=(
                cpu.get("recovery_required_job_ids") if isinstance(cpu.get("recovery_required_job_ids"), list) else []
            ),
        )
        self.record_check(
            "gpu_runner_reconciled",
            runner="gpu-job-runner",
            runner_status=gpu.get("status"),
            required_runner_present="gpu-job-runner" in required_runners,
            started_at=gpu.get("started_at"),
            completed_at=gpu.get("completed_at"),
            marked_recovery_required=int(gpu.get("marked_recovery_required") or 0),
            requeued=int(gpu.get("requeued") or 0),
            runtime_names=gpu.get("runtime_names"),
            requeued_job_ids=gpu.get("requeued_job_ids") if isinstance(gpu.get("requeued_job_ids"), list) else [],
            recovery_required_job_ids=(
                gpu.get("recovery_required_job_ids") if isinstance(gpu.get("recovery_required_job_ids"), list) else []
            ),
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
        resumed_job_ids = [
            str(job_id)
            for job_id in (resume.get("resumed_job_ids") if isinstance(resume.get("resumed_job_ids"), list) else [])
            if str(job_id)
        ]
        native_prompt_ids = [
            str(prompt_id)
            for prompt_id in (resume.get("native_prompt_ids") if isinstance(resume.get("native_prompt_ids"), list) else [])
            if str(prompt_id)
        ]
        if self.minimum_resumed_comfyui_native:
            required_resumed_samples = min(self.minimum_resumed_comfyui_native, 50)
            self.assertGreaterEqual(len(resumed_job_ids), required_resumed_samples, resume)
            self.assertGreaterEqual(len(native_prompt_ids), required_resumed_samples, resume)
        self.record_check(
            "resumable_comfyui_native_prompts_reattached",
            observed=resumed_comfyui_native,
            minimum=self.minimum_resumed_comfyui_native,
            checked=int(resume.get("checked") or 0),
            skipped=int(resume.get("skipped") or 0),
            resumed_job_ids=resumed_job_ids[:50],
            native_prompt_ids=native_prompt_ids[:50],
        )
        self.samples.append(
            {
                "label": "restart-drill-seeded",
                "drill_id": self.drill_id,
                "job_ids": self.drill_job_ids,
                "auto_drill": bool(self.drill_id),
            }
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
