from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def _clean_sse_field(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def job_event_id(job: dict[str, Any]) -> str | None:
    job_id = job.get("id")
    if not job_id:
        return None
    updated_at = job.get("updated_at")
    if isinstance(updated_at, datetime):
        suffix = updated_at.isoformat()
    elif updated_at is not None:
        suffix = str(updated_at)
    else:
        suffix = str(job.get("state") or "unknown")
    return _clean_sse_field(f"{job_id}:{suffix}")


def format_sse_event(event: str, data: Any, *, event_id: str | None = None, retry_ms: int | None = None) -> str:
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {_clean_sse_field(event_id)}")
    if retry_ms is not None:
        lines.append(f"retry: {max(0, int(retry_ms))}")
    lines.append(f"event: {_clean_sse_field(event)}")
    encoded = json.dumps(data, separators=(",", ":"))
    for line in encoded.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def format_sse_comment(comment: str) -> str:
    return f": {_clean_sse_field(comment)}\n\n"


def should_emit_job_snapshot(job: dict[str, Any], last_event_id: str | None, terminal_states: set[str] | frozenset[str]) -> bool:
    event_id = job_event_id(job)
    if not event_id or event_id != str(last_event_id or "").strip():
        return True
    return str(job.get("state") or "") in terminal_states
