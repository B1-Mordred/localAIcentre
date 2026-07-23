from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any


SCHEDULE_ID = "default"
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_KEEP_LAST = 7
DEFAULT_LABEL_PREFIX = "scheduled"
LABEL_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class BackupScheduleError(ValueError):
    pass


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def next_run_after(reference: datetime | None = None, interval_hours: int = DEFAULT_INTERVAL_HOURS) -> datetime:
    current = normalize_datetime(reference) or datetime.now(tz=UTC)
    return current + timedelta(hours=max(1, int(interval_hours)))


def backup_label(label_prefix: str, when: datetime | None = None) -> str:
    prefix = validate_label_prefix(label_prefix)
    current = normalize_datetime(when) or datetime.now(tz=UTC)
    return f"{prefix}-{current.strftime('%Y%m%d-%H%M%S')}"


def validate_label_prefix(value: str) -> str:
    prefix = value.strip()
    if not LABEL_PREFIX_RE.fullmatch(prefix):
        raise BackupScheduleError("label_prefix must be 1-64 characters of letters, numbers, dot, dash, or underscore")
    return prefix


def default_schedule(now: datetime | None = None) -> dict[str, Any]:
    current = normalize_datetime(now) or datetime.now(tz=UTC)
    return {
        "id": SCHEDULE_ID,
        "enabled": False,
        "interval_hours": DEFAULT_INTERVAL_HOURS,
        "keep_last": DEFAULT_KEEP_LAST,
        "delete_older_than_days": 30,
        "label_prefix": DEFAULT_LABEL_PREFIX,
        "next_run_at": None,
        "last_started_at": None,
        "last_completed_at": None,
        "last_status": "idle",
        "last_backup_name": None,
        "failure_message": None,
        "updated_by": None,
        "created_at": current,
        "updated_at": current,
    }


def validate_schedule_values(
    *,
    enabled: bool,
    interval_hours: int,
    keep_last: int,
    delete_older_than_days: int | None,
    label_prefix: str,
) -> dict[str, Any]:
    if interval_hours < 1 or interval_hours > 24 * 30:
        raise BackupScheduleError("interval_hours must be between 1 and 720")
    if keep_last < 1 or keep_last > 365:
        raise BackupScheduleError("keep_last must be between 1 and 365")
    if delete_older_than_days is not None and (delete_older_than_days < 1 or delete_older_than_days > 3650):
        raise BackupScheduleError("delete_older_than_days must be between 1 and 3650")
    return {
        "enabled": bool(enabled),
        "interval_hours": int(interval_hours),
        "keep_last": int(keep_last),
        "delete_older_than_days": int(delete_older_than_days) if delete_older_than_days is not None else None,
        "label_prefix": validate_label_prefix(label_prefix),
    }
