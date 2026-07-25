from __future__ import annotations

import re
from typing import Any


VOICE_PROFILE_ID_RE = re.compile(r"^vp_[A-Za-z0-9._-]{1,96}$")
VOICE_PROFILE_REQUEST_FIELDS = {"voice_profile", "voice_profile_id", "b1_voice_profile", "b1_voice_profile_id"}
VOICE_PROFILE_UPSTREAM_METADATA_FIELDS = {
    "upstream_voice": "voice",
    "upstream_voice_id": "voice",
    "upstream_profile": "profile",
    "upstream_profile_id": "profile_id",
    "upstream_speaker": "speaker",
    "upstream_speaker_id": "speaker_id",
    "language": "language",
    "style": "style",
    "speed": "speed",
}
VOICE_PROFILE_UPSTREAM_METADATA_LABELS = {
    "upstream_voice": "Upstream voice",
    "upstream_voice_id": "Voice ID",
    "upstream_profile": "Profile",
    "upstream_profile_id": "Profile ID",
    "upstream_speaker": "Speaker",
    "upstream_speaker_id": "Speaker ID",
    "language": "Language",
    "style": "Style",
    "speed": "Speed",
}
VOICE_PROFILE_NUMERIC_METADATA_FIELDS = {"speed"}
VOICE_PROFILE_SAFE_METADATA_KEYS = frozenset(VOICE_PROFILE_UPSTREAM_METADATA_FIELDS)
VOICE_PROFILE_METADATA_STRING_MAX_LENGTH = 256


def valid_voice_profile_id(value: str) -> bool:
    return bool(VOICE_PROFILE_ID_RE.fullmatch(value.strip()))


def requested_voice_profile_id(payload: dict[str, Any]) -> str | None:
    for key in ("voice_profile_id", "b1_voice_profile_id", "voice_profile"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            profile_id = value.strip()
            if not valid_voice_profile_id(profile_id):
                raise ValueError("voice profile id is malformed")
            return profile_id
    voice = payload.get("voice")
    if isinstance(voice, str) and voice.strip().startswith("vp_"):
        profile_id = voice.strip()
        if not valid_voice_profile_id(profile_id):
            raise ValueError("voice profile id is malformed")
        return profile_id
    return None


def subject_can_use_profile(row: dict[str, Any], *, subject_id: str, role: str, scopes: frozenset[str]) -> bool:
    if "*" in scopes:
        return True
    if row.get("owner_id") == subject_id:
        return True
    visibility = row.get("visibility_roles")
    return isinstance(visibility, list) and role in {str(item) for item in visibility}


def safe_runtime_metadata_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped or len(stripped) > VOICE_PROFILE_METADATA_STRING_MAX_LENGTH:
        return False
    lowered = stripped.lower()
    if lowered.startswith(("data:", "file:", "http://", "https://")):
        return False
    if any(ord(character) < 32 for character in stripped):
        return False
    return True


def metadata_policy_fields() -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for key, runtime_key in VOICE_PROFILE_UPSTREAM_METADATA_FIELDS.items():
        fields.append(
            {
                "key": key,
                "label": VOICE_PROFILE_UPSTREAM_METADATA_LABELS[key],
                "runtime_field": runtime_key,
                "input_mode": "decimal" if key in VOICE_PROFILE_NUMERIC_METADATA_FIELDS else "text",
                "accepted_json_types": ["string", "number", "boolean"],
                "max_string_length": VOICE_PROFILE_METADATA_STRING_MAX_LENGTH,
            }
        )
    return fields


def voice_profile_upstream_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    upstream: dict[str, Any] = {}
    for key in sorted(VOICE_PROFILE_UPSTREAM_METADATA_FIELDS):
        value = metadata.get(key)
        if safe_runtime_metadata_value(value):
            upstream[key] = value
    return upstream


def normalized_sample_artifacts(row: dict[str, Any]) -> list[dict[str, Any]]:
    samples = row.get("sample_artifacts")
    if not isinstance(samples, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in samples:
        if not isinstance(item, dict):
            continue
        sample = {
            "url": item.get("url"),
            "sha256": item.get("sha256"),
            "mime_type": item.get("mime_type"),
            "bytes": item.get("bytes"),
        }
        if all(sample.get(key) is not None for key in ("url", "sha256", "mime_type", "bytes")):
            normalized.append(sample)
    return normalized


def runtime_voice_profile_envelope(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "runtime": str(row.get("runtime") or ""),
        "engine": str(row.get("engine") or ""),
        "model_alias": str(row.get("model_alias") or ""),
        "profile_type": str(row.get("profile_type") or ""),
        "upstream": voice_profile_upstream_metadata(row),
        "sample_artifacts": normalized_sample_artifacts(row),
    }


def apply_voice_profile_to_payload(payload: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    forwarded = {
        key: value
        for key, value in payload.items()
        if key not in VOICE_PROFILE_REQUEST_FIELDS and not key.startswith("b1_voice_profile")
    }
    envelope = runtime_voice_profile_envelope(row)
    for metadata_key, runtime_key in VOICE_PROFILE_UPSTREAM_METADATA_FIELDS.items():
        if metadata_key in envelope["upstream"]:
            forwarded[runtime_key] = envelope["upstream"][metadata_key]
    forwarded.setdefault("voice", envelope["id"])
    forwarded["b1_voice_profile"] = envelope
    return forwarded
