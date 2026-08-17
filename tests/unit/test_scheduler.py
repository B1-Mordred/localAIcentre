from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.scheduler import (  # noqa: E402
    JobState,
    PriorityClass,
    QueueItem,
    ResourceEstimate,
    ResourcePolicy,
    classify_cpu_residency,
    classify_resource_fit,
    select_next_job,
    validate_transition,
)

try:
    from app import database  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name != "sqlalchemy":
        raise
    database = None


class SchedulerTests(unittest.TestCase):
    def test_job_state_transitions_include_required_path(self) -> None:
        path = [
            JobState.CREATED,
            JobState.VALIDATED,
            JobState.QUEUED,
            JobState.WAITING_FOR_GPU,
            JobState.UNLOADING,
            JobState.VERIFYING_VRAM,
            JobState.LOADING,
            JobState.WARMING,
            JobState.RUNNING,
            JobState.SAVING,
            JobState.COMPLETED,
        ]
        for current, target in zip(path, path[1:]):
            self.assertTrue(validate_transition(current, target), f"{current} -> {target}")

    def test_cpu_only_job_path_can_skip_gpu_states(self) -> None:
        path = [
            JobState.CREATED,
            JobState.VALIDATED,
            JobState.QUEUED,
            JobState.RUNNING,
            JobState.SAVING,
            JobState.COMPLETED,
        ]
        for current, target in zip(path, path[1:]):
            self.assertTrue(validate_transition(current, target), f"{current} -> {target}")

    def test_terminal_state_does_not_transition(self) -> None:
        self.assertFalse(validate_transition(JobState.COMPLETED, JobState.QUEUED))

    def test_resource_labels_for_rtx3060_profile(self) -> None:
        policy = ResourcePolicy()
        self.assertEqual(classify_resource_fit(policy, ResourceEstimate(vram_gib=6, ram_gib=8)).label, "recommended")
        self.assertEqual(classify_resource_fit(policy, ResourceEstimate(vram_gib=11, ram_gib=8)).label, "offload-required")
        self.assertFalse(classify_resource_fit(policy, ResourceEstimate(vram_gib=13, ram_gib=8)).accepted)

    def test_cpu_residency_respects_policy_allowlist_and_host_reserve(self) -> None:
        policy = ResourcePolicy()
        allowed = classify_cpu_residency(policy, "tts-fast", ResourceEstimate(vram_gib=0, ram_gib=0.4, requires_gpu=False))
        self.assertTrue(allowed.candidate)
        self.assertTrue(allowed.allowed)

        not_listed = classify_cpu_residency(policy, "custom-cpu-tts", ResourceEstimate(vram_gib=0, ram_gib=0.4, requires_gpu=False))
        self.assertTrue(not_listed.candidate)
        self.assertFalse(not_listed.allowed)
        self.assertIn("allowlist", not_listed.reason)

        too_large = classify_cpu_residency(policy, "tts-fast", ResourceEstimate(vram_gib=0, ram_gib=30, requires_gpu=False))
        self.assertFalse(too_large.allowed)
        self.assertIn("protected host reserve", too_large.reason)

    def test_priority_aging_prevents_permanent_starvation(self) -> None:
        now = datetime.now(tz=UTC)
        old_batch = QueueItem("batch", PriorityClass.BATCH, now - timedelta(hours=5), "video-text")
        new_chat = QueueItem("chat", PriorityClass.CHAT, now, "chat-default")
        self.assertEqual(select_next_job([old_batch, new_chat], now), old_batch)

    def test_same_model_grouping_prefers_loaded_model_within_fairness_window(self) -> None:
        now = datetime.now(tz=UTC)
        slightly_older_image = QueueItem(
            "image-different",
            PriorityClass.SINGLE_IMAGE,
            now - timedelta(minutes=5),
            "image-edit",
            runtime="comfyui",
            resolved_model_version="sdxl-edit@1",
        )
        loaded_model_image = QueueItem(
            "image-loaded",
            PriorityClass.SINGLE_IMAGE,
            now,
            "image-default",
            runtime="comfyui",
            resolved_model_version="sdxl-default@1",
        )

        selected = select_next_job(
            [slightly_older_image, loaded_model_image],
            now,
            active_runtime_model_refs={("comfyui", "sdxl-default@1")},
        )

        self.assertEqual(selected, loaded_model_image)

    def test_same_model_grouping_does_not_override_interactive_priority(self) -> None:
        now = datetime.now(tz=UTC)
        loaded_model_batch = QueueItem(
            "batch-loaded",
            PriorityClass.BATCH,
            now,
            "image-default",
            runtime="comfyui",
            resolved_model_version="sdxl-default@1",
        )
        chat = QueueItem(
            "chat",
            PriorityClass.CHAT,
            now,
            "chat-default",
            runtime="localai",
            resolved_model_version="llm@1",
        )

        selected = select_next_job(
            [loaded_model_batch, chat],
            now,
            active_runtime_model_refs={("comfyui", "sdxl-default@1")},
        )

        self.assertEqual(selected, chat)

    def test_same_model_grouping_respects_priority_aging(self) -> None:
        now = datetime.now(tz=UTC)
        much_older_image = QueueItem(
            "image-different",
            PriorityClass.SINGLE_IMAGE,
            now - timedelta(minutes=40),
            "image-edit",
            runtime="comfyui",
            resolved_model_version="sdxl-edit@1",
        )
        loaded_model_image = QueueItem(
            "image-loaded",
            PriorityClass.SINGLE_IMAGE,
            now,
            "image-default",
            runtime="comfyui",
            resolved_model_version="sdxl-default@1",
        )

        selected = select_next_job(
            [much_older_image, loaded_model_image],
            now,
            active_runtime_model_refs={("comfyui", "sdxl-default@1")},
        )

        self.assertEqual(selected, much_older_image)


@unittest.skipIf(database is None, "SQLAlchemy is not installed in this lightweight test environment")
class DatabaseQueueClaimTests(unittest.TestCase):
    def test_runtime_state_model_refs_only_uses_idle_resident_models(self) -> None:
        refs = database.runtime_state_model_refs(
            [
                {
                    "runtime": "comfyui",
                    "status": "idle",
                    "stage": "idle",
                    "resolved_model_version": "sdxl-default@1",
                },
                {
                    "runtime": "localai",
                    "status": "load_ok",
                    "stage": "loading",
                    "resolved_model_version": "llm@1",
                },
                {
                    "runtime": "voicebox",
                    "status": "idle",
                    "stage": "idle",
                    "resolved_model_version": "",
                },
            ]
        )

        self.assertEqual(refs, {("comfyui", "sdxl-default@1")})

    def test_select_claim_candidate_uses_active_runtime_model_refs(self) -> None:
        now = datetime.now(tz=UTC)
        rows = [
            {
                "id": "image-different",
                "priority": "single_image",
                "created_at": now - timedelta(minutes=5),
                "model_alias": "image-edit",
                "runtime": "comfyui",
                "resolved_model_version": "sdxl-edit@1",
            },
            {
                "id": "image-loaded",
                "priority": "single_image",
                "created_at": now,
                "model_alias": "image-default",
                "runtime": "comfyui",
                "resolved_model_version": "sdxl-default@1",
            },
        ]

        selected = database.select_claim_candidate(
            rows,
            now=now,
            active_runtime_model_refs={("comfyui", "sdxl-default@1")},
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["id"], "image-loaded")


if __name__ == "__main__":
    unittest.main()
