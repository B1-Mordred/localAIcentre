from __future__ import annotations

import uuid
import re
from datetime import UTC, datetime
from typing import Any


REDACTED = "<redacted>"
MAX_STRING_LENGTH = 512
MAX_LIST_ITEMS = 50
MAX_DEPTH = 6
LOG_MAX_STRING_LENGTH = 1024

SECRET_STRING_PATTERNS = (
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"']+", re.IGNORECASE),
    re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bb1k_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bb1adm_[A-Za-z0-9_-]+\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]+\b"),
    re.compile(
        r"([?&](?:[^=&\s]*(?:api[_-]?key|authorization|bearer|credential|password|secret|token)[^=&\s]*)=)[^&#\s]+",
        re.IGNORECASE,
    ),
)

SENSITIVE_KEY_FRAGMENTS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "document",
    "file",
    "image",
    "input",
    "key_hash",
    "key_salt",
    "media",
    "messages",
    "password",
    "prompt",
    "sample",
    "secret",
    "session",
    "token",
    "audio",
    "video",
    "voice",
}


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_sensitive_string_patterns(value: str) -> str:
    redacted = value
    for pattern in SECRET_STRING_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + REDACTED if match.lastindex else REDACTED, redacted)
    return redacted


def freeform_audit_field_summary(field_name: str, value: str | None) -> dict[str, bool]:
    normalized = field_name.strip().lower().replace("-", "_")
    if not normalized or is_sensitive_key(normalized):
        raise ValueError("field_name must be a non-sensitive audit metadata key")
    return {f"{normalized}_provided": bool((value or "").strip())}


def redact_audit_string(value: str) -> str:
    redacted = redact_sensitive_string_patterns(value)
    if len(redacted) > MAX_STRING_LENGTH:
        return redacted[:MAX_STRING_LENGTH] + "...<truncated>"
    return redacted


def redact_audit_metadata(value: Any, *, _depth: int = 0) -> Any:
    if _depth > MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            redacted[text_key] = REDACTED if is_sensitive_key(text_key) else redact_audit_metadata(item, _depth=_depth + 1)
        return redacted
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        output = [redact_audit_metadata(item, _depth=_depth + 1) for item in items[:MAX_LIST_ITEMS]]
        if len(items) > MAX_LIST_ITEMS:
            output.append({"truncated_items": len(items) - MAX_LIST_ITEMS})
        return output
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return redact_audit_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_audit_string(str(value))


def redact_log_string(value: str) -> str:
    redacted = redact_sensitive_string_patterns(value)
    if len(redacted) > LOG_MAX_STRING_LENGTH:
        return redacted[:LOG_MAX_STRING_LENGTH] + "...<truncated>"
    return redacted


def redact_log_value(value: Any, *, _depth: int = 0) -> Any:
    if _depth > MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            redacted[text_key] = REDACTED if is_sensitive_key(text_key) else redact_log_value(item, _depth=_depth + 1)
        return redacted
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        output = [redact_log_value(item, _depth=_depth + 1) for item in items[:MAX_LIST_ITEMS]]
        if len(items) > MAX_LIST_ITEMS:
            output.append({"truncated_items": len(items) - MAX_LIST_ITEMS})
        return output
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return redact_log_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_log_string(str(value))


def redact_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): REDACTED if is_sensitive_key(str(key)) else redact_log_value(value)
        for key, value in fields.items()
    }


def make_audit_event(
    *,
    event_type: str,
    actor_id: str | None,
    actor_role: str | None,
    actor_key_prefix: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    summary: str = "",
    metadata: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    remote_addr: str | None = None,
    created_at: datetime | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    if not event_type.strip():
        raise ValueError("event_type is required")
    now = created_at or datetime.now(tz=UTC)
    return {
        "id": event_id or f"audit_{uuid.uuid4().hex}",
        "created_at": now,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "actor_key_prefix": actor_key_prefix,
        "event_type": event_type,
        "target_type": target_type,
        "target_id": target_id,
        "summary": summary,
        "metadata": redact_audit_metadata(metadata or {}),
        "correlation_id": correlation_id,
        "remote_addr": remote_addr,
    }


def public_audit_event(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    created_at = public.get("created_at")
    if isinstance(created_at, datetime):
        public["created_at"] = created_at.isoformat()
    public["metadata"] = redact_audit_metadata(public.get("metadata") or {})
    return public
