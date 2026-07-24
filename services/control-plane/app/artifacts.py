from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote


class ArtifactAccessError(ValueError):
    pass


def _has_control_character(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _validate_artifact_segment(segment: str) -> str:
    decoded = unquote(segment)
    if decoded in {"", ".", ".."} or "/" in decoded or "\\" in decoded or "?" in decoded or "#" in decoded or _has_control_character(decoded):
        raise ArtifactAccessError("artifact path must not contain traversal segments")
    return segment


def artifact_url_for_path(artifact_path: str) -> str:
    raw_parts = artifact_path.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ArtifactAccessError("artifact path must not contain traversal segments")
    normalized = PurePosixPath("/".join(_validate_artifact_segment(part) for part in raw_parts))
    if normalized.is_absolute() or not normalized.parts:
        raise ArtifactAccessError("artifact path must be relative")
    return f"/artifacts/{normalized.as_posix()}"


def job_has_artifact_url(job: dict[str, Any], artifact_url: str) -> bool:
    artifacts = job.get("artifacts") or []
    return any(isinstance(artifact, dict) and artifact.get("url") == artifact_url for artifact in artifacts)


def subject_can_read_job_artifact(subject_id: str, scopes: frozenset[str], job: dict[str, Any], role: str | None = None) -> bool:
    if "*" in scopes:
        return True
    if role in {"admin", "operator"}:
        return True
    return job.get("owner_id") == subject_id
