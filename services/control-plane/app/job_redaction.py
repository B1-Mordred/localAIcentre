from __future__ import annotations

import re
from datetime import datetime
from typing import Any


REDACTED = "<redacted>"
MAX_DEPTH = 6
MAX_LIST_ITEMS = 20
MAX_STRING_LENGTH = 512

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
    "key_hash",
    "key_salt",
    "media",
    "messages",
    "password",
    "header",
    "path",
    "prompt",
    "sample",
    "secret",
    "session",
    "sha256",
    "token",
    "audio",
    "video",
    "voice",
}

SENSITIVE_EXACT_KEYS = {
    "input_text",
    "text",
}

SAFE_CONTAINER_KEYS = {
    "input",
    "parameters",
}


def normalized_key(key: str) -> str:
    return key.lower().replace("-", "_")


def is_sensitive_request_key(key: str, value: Any) -> bool:
    normalized = normalized_key(key)
    if normalized in SAFE_CONTAINER_KEYS:
        return not isinstance(value, dict)
    if normalized in SENSITIVE_EXACT_KEYS:
        return True
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_request_string(value: str) -> str:
    redacted = value
    for pattern in SECRET_STRING_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + REDACTED if match.lastindex else REDACTED, redacted)
    if len(redacted) > MAX_STRING_LENGTH:
        return redacted[:MAX_STRING_LENGTH] + "...<truncated>"
    return redacted


def redact_request_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if key and is_sensitive_request_key(key, value):
        return REDACTED
    if depth > MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for item_key, item_value in value.items():
            text_key = str(item_key)
            redacted[text_key] = redact_request_value(item_value, key=text_key, depth=depth + 1)
        return redacted
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        output = [redact_request_value(item, depth=depth + 1) for item in items[:MAX_LIST_ITEMS]]
        if len(items) > MAX_LIST_ITEMS:
            output.append({"truncated_items": len(items) - MAX_LIST_ITEMS})
        return output
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return redact_request_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_request_string(str(value))


def redact_request(payload: dict[str, Any]) -> dict[str, Any]:
    return redact_request_value(payload) if isinstance(payload, dict) else {}
