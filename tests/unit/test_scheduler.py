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
    classify_resource_fit,
    select_next_job,
    validate_transition,
)


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

    def test_priority_aging_prevents_permanent_starvation(self) -> None:
        now = datetime.now(tz=UTC)
        old_batch = QueueItem("batch", PriorityClass.BATCH, now - timedelta(hours=5), "video-text")
        new_chat = QueueItem("chat", PriorityClass.CHAT, now, "chat-default")
        self.assertEqual(select_next_job([old_batch, new_chat], now), old_batch)


if __name__ == "__main__":
    unittest.main()
