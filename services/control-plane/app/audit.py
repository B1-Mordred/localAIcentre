from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any


REDACTED = "<redacted>"
MAX_STRING_LENGTH = 512
MAX_LIST_ITEMS = 50
MAX_DEPTH = 6

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
        if len(value) > MAX_STRING_LENGTH:
            return value[:MAX_STRING_LENGTH] + "...<truncated>"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


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
