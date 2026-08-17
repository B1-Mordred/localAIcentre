from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.observability import build_observability_report, observability_report_to_prometheus, parse_datetime, summarize_numbers  # noqa: E402


class ObservabilityTests(unittest.TestCase):
    def test_parse_datetime_accepts_common_api_shapes(self) -> None:
        self.assertEqual(parse_datetime("2026-07-23T10:00:00Z"), datetime(2026, 7, 23, 10, 0, tzinfo=UTC))
        self.assertEqual(parse_datetime(datetime(2026, 7, 23, 10, 0)), datetime(2026, 7, 23, 10, 0, tzinfo=UTC))
        self.assertIsNone(parse_datetime(""))
        self.assertIsNone(parse_datetime("not a timestamp"))

    def test_summarize_numbers_ignores_missing_and_negative_values(self) -> None:
        self.assertEqual(
            summarize_numbers([100, None, -1, "200", "bad", 300]),
            {"count": 3, "min": 100, "avg": 200, "p50": 200, "p95": 300, "max": 300},
        )
        self.assertEqual(summarize_numbers([None, "bad"])["count"], 0)

    def test_report_summarizes_queue_timing_and_priority(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        jobs = [
            {
                "id": "job_queued_chat",
                "state": "queued",
                "priority": "chat",
                "created_at": now - timedelta(seconds=120),
            },
            {
                "id": "job_waiting_image",
                "state": "waiting_for_gpu",
                "priority": "single_image",
                "created_at": now - timedelta(seconds=30),
            },
            {
                "id": "job_running",
                "state": "running",
                "priority": "video",
                "created_at": now - timedelta(seconds=300),
            },
        ]
        report = build_observability_report(
            jobs,
            state_counts=[
                {"state": "queued", "count": 1},
                {"state": "waiting_for_gpu", "count": 1},
                {"state": "running", "count": 1},
            ],
            runtime_states=[],
            scheduler_lease=None,
            agent_metrics=None,
            agent_error="runtime-agent offline",
            now=now,
        )

        self.assertEqual(report["queue"]["depth_total"], 2)
        self.assertEqual(report["queue"]["active_total"], 1)
        self.assertEqual(report["queue"]["by_priority"], {"chat": 1, "single_image": 1})
        self.assertEqual(report["queue"]["oldest_wait_seconds"], 120)
        self.assertEqual(report["queue"]["wait_seconds_by_priority"]["chat"]["max"], 120)
        self.assertFalse(report["runtime_agent"]["available"])

    def test_report_counts_recovery_required_as_terminal_recovery_outcome(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        jobs = [
            {
                "id": "job_recovery",
                "state": "recovery_required",
                "runtime": "comfyui",
                "priority": "single_image",
                "created_at": now - timedelta(minutes=15),
                "completed_at": now - timedelta(minutes=14),
                "updated_at": now - timedelta(minutes=14),
            }
        ]
        report = build_observability_report(
            jobs,
            state_counts=[{"state": "recovery_required", "count": 1}],
            runtime_states=[],
            scheduler_lease=None,
            agent_metrics=None,
            agent_error=None,
            now=now,
        )

        self.assertEqual(report["queue"]["depth_total"], 0)
        self.assertEqual(report["queue"]["active_total"], 0)
        self.assertEqual(report["jobs"]["recovery_required_last_hour"], 1)
        self.assertEqual(report["jobs"]["by_runtime"]["comfyui"]["recovery_required"], 1)

    def test_report_summarizes_recent_jobs_switches_runtime_and_gpu(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        jobs = [
            {
                "id": "job_localai_1",
                "state": "completed",
                "runtime": "localai",
                "resolved_model_version": "llm:v1",
                "created_at": now - timedelta(minutes=25),
                "started_at": now - timedelta(minutes=24),
                "completed_at": now - timedelta(minutes=23),
                "load_time_ms": 2000,
                "run_time_ms": 5000,
                "peak_vram_mib": 4096,
                "peak_ram_mib": 2048,
            },
            {
                "id": "job_localai_2",
                "state": "completed",
                "runtime": "localai",
                "resolved_model_version": "llm:v1",
                "created_at": now - timedelta(minutes=20),
                "started_at": now - timedelta(minutes=19),
                "completed_at": now - timedelta(minutes=18),
                "load_time_ms": 1000,
                "run_time_ms": 3000,
                "peak_vram_mib": 5120,
                "peak_ram_mib": 3072,
            },
            {
                "id": "job_comfy",
                "state": "failed",
                "runtime": "comfyui",
                "resolved_model_version": "sdxl:v1",
                "created_at": now - timedelta(minutes=10),
                "started_at": now - timedelta(minutes=9),
                "completed_at": now - timedelta(minutes=8),
                "load_time_ms": 8000,
                "run_time_ms": 12000,
                "peak_vram_mib": 9216,
                "peak_ram_mib": 6144,
            },
        ]
        report = build_observability_report(
            jobs,
            state_counts=[{"state": "completed", "count": 2}, {"state": "failed", "count": 1}],
            runtime_states=[
                {
                    "runtime": "localai",
                    "status": "idle",
                    "stage": "idle",
                    "active_model": "llm:v1",
                    "model_alias": "chat-default",
                    "job_id": None,
                    "updated_at": now.isoformat(),
                },
                {"runtime": "comfyui", "status": "unknown", "stage": "unknown"},
            ],
            scheduler_lease={"owner": "gpu-runner", "epoch": 3},
            agent_metrics={
                "cpu": {"available": True, "cpu_count": 12, "load1": 0.75},
                "memory": {"available": True, "total_bytes": 32_000, "used_bytes": 10_000, "available_bytes": 22_000},
                "disks": [
                    {"path": "/srv/b1-ai-hub", "available": True, "total_bytes": 1000, "used_bytes": 400, "free_bytes": 600}
                ],
                "gpu": {
                    "available": True,
                    "devices": [
                        {
                            "index": 0,
                            "name": "RTX 3060",
                            "temperature_c": 61,
                            "power_watts": 95.5,
                            "utilization_gpu_percent": 72,
                            "memory_total_mib": 12288,
                            "memory_used_mib": 5120,
                            "memory_free_mib": 7168,
                        }
                    ],
                },
            },
            agent_error=None,
            now=now,
        )

        self.assertEqual(report["jobs"]["completed_last_hour"], 2)
        self.assertEqual(report["jobs"]["failed_last_hour"], 1)
        self.assertEqual(report["jobs"]["load_seconds"]["p95"], 8)
        self.assertEqual(report["jobs"]["run_seconds"]["avg"], 6.67)
        self.assertEqual(report["jobs"]["peak_vram_mib"]["max"], 9216)
        self.assertEqual(report["jobs"]["by_runtime"]["localai"]["completed"], 2)
        self.assertEqual(report["model_switches_per_hour"]["last_hour"], 1)
        self.assertEqual(report["runtimes"]["active"][0]["runtime"], "localai")
        self.assertEqual(report["gpu"]["memory_used_mib"], 5120)
        self.assertEqual(report["gpu"]["utilization_gpu_percent_max"], 72)
        self.assertEqual(report["host"]["storage"]["free_bytes"], 600)

    def test_prometheus_export_formats_observability_report_without_sensitive_labels(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        report = build_observability_report(
            [
                {
                    "id": "job_1",
                    "state": "completed",
                    "runtime": "localai",
                    "resolved_model_version": "llm:v1",
                    "priority": "chat",
                    "created_at": now - timedelta(minutes=2),
                    "started_at": now - timedelta(minutes=1),
                    "completed_at": now,
                    "load_time_ms": 1000,
                    "run_time_ms": 2500,
                    "peak_vram_mib": 4096,
                    "peak_ram_mib": 2048,
                },
                {
                    "id": "job_2",
                    "state": "queued",
                    "priority": "single_image",
                    "created_at": now - timedelta(seconds=45),
                },
            ],
            state_counts=[{"state": "completed", "count": 1}, {"state": "queued", "count": 1}],
            runtime_states=[{"runtime": 'localai"\n', "status": "idle", "stage": "idle", "active_model": "secret-model-name"}],
            scheduler_lease={"owner": "gpu-runner"},
            agent_metrics={
                "cpu": {"available": True, "cpu_count": 12, "load1": 0.5},
                "memory": {
                    "available": True,
                    "total_bytes": 32_000,
                    "used_bytes": 12_000,
                    "available_bytes": 20_000,
                    "swap_total_bytes": 1000,
                    "swap_used_bytes": 100,
                    "swap_free_bytes": 900,
                },
                "disks": [{"path": "/srv/b1-ai-hub", "available": True, "total_bytes": 1000, "used_bytes": 250, "free_bytes": 750}],
                "gpu": {
                    "available": True,
                    "devices": [
                        {
                            "memory_total_mib": 12288,
                            "memory_used_mib": 4096,
                            "memory_free_mib": 8192,
                            "utilization_gpu_percent": 50,
                            "temperature_c": 60,
                            "power_watts": 90,
                        }
                    ],
                },
            },
            agent_error=None,
            now=now,
        )

        text = observability_report_to_prometheus(report)

        self.assertIn("# HELP b1_ai_hub_queue_depth_total", text)
        self.assertIn('b1_ai_hub_queue_state_total{state="queued"} 1', text)
        self.assertIn('b1_ai_hub_jobs_last_hour_total{status="completed"} 1', text)
        self.assertIn('b1_ai_hub_job_run_seconds{stat="max"} 2.5', text)
        self.assertIn("b1_ai_hub_gpu_memory_used_mib 4096", text)
        self.assertIn('b1_ai_hub_host_memory_bytes{kind="swap_used"} 100', text)
        self.assertIn('b1_ai_hub_runtime_active{runtime="localai\\"\\n",stage="idle",status="idle"} 1', text)
        self.assertNotIn("secret-model-name", text)


if __name__ == "__main__":
    unittest.main()
