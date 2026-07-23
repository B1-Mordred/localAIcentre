from __future__ import annotations

from typing import Any

from .scheduler import JobState


PENDING_JOB_STATES = frozenset(
    {
        JobState.CREATED.value,
        JobState.VALIDATED.value,
        JobState.QUEUED.value,
        JobState.WAITING_FOR_GPU.value,
    }
)

ACTIVE_JOB_STATES = frozenset(
    {
        JobState.UNLOADING.value,
        JobState.VERIFYING_VRAM.value,
        JobState.LOADING.value,
        JobState.WARMING.value,
        JobState.RUNNING.value,
        JobState.SAVING.value,
        JobState.CANCELLING.value,
    }
)

TERMINAL_JOB_STATES = frozenset(
    {
        JobState.COMPLETED.value,
        JobState.CANCELLED.value,
        JobState.FAILED.value,
        JobState.EXPIRED.value,
        JobState.RECOVERY_REQUIRED.value,
    }
)

CANCEL_IMMEDIATE_STATES = PENDING_JOB_STATES
PRIORITIZABLE_JOB_STATES = PENDING_JOB_STATES
RETRYABLE_JOB_STATES = frozenset(
    {
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.EXPIRED.value,
        JobState.RECOVERY_REQUIRED.value,
    }
)

# Recovery-required jobs are terminal for streaming/cancellation, but still block
# model cleanup until an operator retries, fails, cancels, or expires them.
MODEL_BLOCKING_JOB_STATES = PENDING_JOB_STATES | ACTIVE_JOB_STATES | frozenset({JobState.RECOVERY_REQUIRED.value})


def normalize_job_state(value: Any) -> str:
    if isinstance(value, JobState):
        return value.value
    return str(value or "")


def is_terminal_job_state(value: Any) -> bool:
    return normalize_job_state(value) in TERMINAL_JOB_STATES
