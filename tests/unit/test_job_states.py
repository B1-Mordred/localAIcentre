from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.job_events import format_sse_event, job_event_id  # noqa: E402
from app.job_states import (  # noqa: E402
    ACTIVE_JOB_STATES,
    MODEL_BLOCKING_JOB_STATES,
    PENDING_JOB_STATES,
    PRIORITIZABLE_JOB_STATES,
    RETRYABLE_JOB_STATES,
    TERMINAL_JOB_STATES,
    is_terminal_job_state,
)
from app.scheduler import JobState  # noqa: E402


class JobStateTests(unittest.TestCase):
    def test_recovery_required_is_terminal_retryable_and_model_blocking(self) -> None:
        self.assertIn(JobState.RECOVERY_REQUIRED.value, TERMINAL_JOB_STATES)
        self.assertIn(JobState.RECOVERY_REQUIRED.value, RETRYABLE_JOB_STATES)
        self.assertIn(JobState.RECOVERY_REQUIRED.value, MODEL_BLOCKING_JOB_STATES)
        self.assertNotIn(JobState.RECOVERY_REQUIRED.value, PENDING_JOB_STATES)
        self.assertNotIn(JobState.RECOVERY_REQUIRED.value, ACTIVE_JOB_STATES)
        self.assertNotIn(JobState.RECOVERY_REQUIRED.value, PRIORITIZABLE_JOB_STATES)

    def test_terminal_state_predicate_accepts_enum_and_string_values(self) -> None:
        self.assertTrue(is_terminal_job_state(JobState.COMPLETED))
        self.assertTrue(is_terminal_job_state("recovery_required"))
        self.assertFalse(is_terminal_job_state("running"))

    def test_job_event_id_uses_job_id_and_update_stamp(self) -> None:
        event_id = job_event_id({"id": "job_1", "updated_at": datetime(2026, 7, 23, 12, 0, tzinfo=UTC)})

        self.assertEqual(event_id, "job_1:2026-07-23T12:00:00+00:00")

    def test_format_sse_event_writes_stable_fields_and_compact_json(self) -> None:
        event = format_sse_event(
            "job",
            {"id": "job_1", "state": "recovery_required"},
            event_id="job_1:recovery_required",
            retry_ms=1000,
        )

        self.assertEqual(
            event,
            'id: job_1:recovery_required\nretry: 1000\nevent: job\ndata: {"id":"job_1","state":"recovery_required"}\n\n',
        )


if __name__ == "__main__":
    unittest.main()
