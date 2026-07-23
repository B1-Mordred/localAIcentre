from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import artifacts as artifact_policy


TERMINAL_STATES = {"completed", "cancelled", "failed", "expired"}
DISALLOWED_NAMESPACES = {"inputs", "temporary", "backups", "secrets"}


class ArtifactRetentionError(ValueError):
    pass


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def safe_segment(value: Any) -> str:
    raw = str(value or "")
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {".", "_", "-"}) else "_" for ch in raw)
    return cleaned.strip("._-")[:120]


def normalize_namespaces(namespaces: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for namespace in namespaces or []:
        item = safe_segment(namespace)
        if not item:
            raise ArtifactRetentionError("artifact namespace cannot be empty")
        if "/" in item or item in {".", ".."}:
            raise ArtifactRetentionError(f"invalid artifact namespace: {namespace}")
        if item in DISALLOWED_NAMESPACES:
            raise ArtifactRetentionError(f"artifact namespace is not generated output storage: {item}")
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def artifact_relative_path(artifact: dict[str, Any]) -> tuple[str | None, str | None]:
    url = artifact.get("url")
    path = artifact.get("path")
    if isinstance(url, str) and url.startswith("/artifacts/"):
        relative = url.removeprefix("/artifacts/")
    elif isinstance(path, str):
        relative = path
    else:
        return None, "artifact has no internal /artifacts URL"
    if "?" in relative or "#" in relative:
        return None, "artifact URL must not contain query or fragment"
    try:
        normalized_url = artifact_policy.artifact_url_for_path(relative)
    except artifact_policy.ArtifactAccessError as exc:
        return None, str(exc)
    normalized_relative = normalized_url.removeprefix("/artifacts/")
    if isinstance(url, str) and url != normalized_url:
        return None, "artifact URL is not normalized"
    return normalized_relative, None


def artifact_path(artifact_root: Path, relative_path: str) -> Path:
    artifact_policy.artifact_url_for_path(relative_path)
    root = artifact_root.resolve()
    path = root / relative_path
    parent = path.parent.resolve()
    if root not in parent.parents and parent != root:
        raise ArtifactRetentionError("artifact path escapes artifact root")
    return path


def path_has_symlink_parent(path: Path, artifact_root: Path) -> bool:
    root = artifact_root.resolve()
    current = path
    while current != root and root in current.parents:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def artifact_matches_job_scope(relative_path: str, artifact: dict[str, Any], job: dict[str, Any]) -> bool:
    identifiers = {safe_segment(job.get("id")), safe_segment(job.get("native_prompt_id"))}
    identifiers = {item for item in identifiers if item}
    if not identifiers:
        return False
    path_parts = relative_path.split("/")
    return any(part in identifiers or any(part.startswith(f"{identifier}-") for identifier in identifiers) for part in path_parts)


def public_artifact_entry(job: dict[str, Any], artifact: dict[str, Any], relative_path: str, reason: str, target: Path | None = None) -> dict[str, Any]:
    size = None
    if target is not None and target.exists() and target.is_file() and not target.is_symlink():
        size = target.stat().st_size
    if size is None and isinstance(artifact.get("bytes"), int) and artifact["bytes"] >= 0:
        size = int(artifact["bytes"])
    return {
        "job_id": job.get("id"),
        "owner_id": job.get("owner_id"),
        "state": job.get("state"),
        "completed_at": parse_datetime(job.get("completed_at")).isoformat() if parse_datetime(job.get("completed_at")) else None,
        "artifact_id": artifact.get("id"),
        "url": artifact.get("url") or f"/artifacts/{relative_path}",
        "path": relative_path,
        "namespace": relative_path.split("/", 1)[0],
        "size_bytes": size or 0,
        "mime_type": artifact.get("mime_type"),
        "reason": reason,
    }


def artifact_decision(
    artifact_root: Path,
    job: dict[str, Any],
    artifact: dict[str, Any],
    protected_urls: set[str],
    namespaces: list[str],
    cutoff: datetime,
) -> tuple[str, dict[str, Any]]:
    relative, error = artifact_relative_path(artifact)
    if error or relative is None:
        return "invalid", {"job_id": job.get("id"), "artifact_id": artifact.get("id"), "reason": error or "invalid artifact path"}

    url = f"/artifacts/{relative}"
    namespace = relative.split("/", 1)[0]
    if artifact.get("deleted_at") or artifact.get("retention_status") == "deleted":
        return "kept", public_artifact_entry(job, artifact, relative, "already_deleted")
    if url in protected_urls:
        return "kept", public_artifact_entry(job, artifact, relative, "protected_by_voice_profile")
    if namespace in DISALLOWED_NAMESPACES:
        return "kept", public_artifact_entry(job, artifact, relative, "not_generated_output_storage")
    if namespaces and namespace not in namespaces:
        return "kept", public_artifact_entry(job, artifact, relative, "outside_selected_namespaces")
    if str(job.get("state")) not in TERMINAL_STATES:
        return "kept", public_artifact_entry(job, artifact, relative, "job_not_terminal")
    completed_at = parse_datetime(job.get("completed_at"))
    if completed_at is None or completed_at >= cutoff:
        return "kept", public_artifact_entry(job, artifact, relative, "job_newer_than_retention_cutoff")
    if not artifact_matches_job_scope(relative, artifact, job):
        return "invalid", public_artifact_entry(job, artifact, relative, "artifact_path_not_scoped_to_job")

    try:
        target = artifact_path(artifact_root, relative)
    except ArtifactRetentionError as exc:
        return "invalid", public_artifact_entry(job, artifact, relative, str(exc))
    if path_has_symlink_parent(target, artifact_root):
        return "invalid", public_artifact_entry(job, artifact, relative, "artifact path contains a symlink")
    if not target.exists():
        return "invalid", public_artifact_entry(job, artifact, relative, "artifact file is missing")
    if target.is_symlink():
        return "invalid", public_artifact_entry(job, artifact, relative, "artifact file is a symlink")
    if not target.is_file():
        return "invalid", public_artifact_entry(job, artifact, relative, "artifact path is not a file")
    expected_size = artifact.get("bytes")
    if isinstance(expected_size, int) and expected_size >= 0 and expected_size != target.stat().st_size:
        return "invalid", public_artifact_entry(job, artifact, relative, "artifact size does not match metadata", target)
    return "candidate", public_artifact_entry(job, artifact, relative, "terminal_job_older_than_retention_cutoff", target)


def build_artifact_retention_plan(
    artifact_root: Path,
    jobs: list[dict[str, Any]],
    *,
    delete_older_than_days: int,
    protected_urls: set[str] | None = None,
    namespaces: list[str] | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if delete_older_than_days < 1:
        raise ArtifactRetentionError("delete_older_than_days must be at least one day")
    normalized_namespaces = normalize_namespaces(namespaces)
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    cutoff = current - timedelta(days=delete_older_than_days)
    bounded_jobs = jobs[: max(1, int(limit))] if limit is not None else jobs
    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    protected = protected_urls or set()

    for job in bounded_jobs:
        artifacts = job.get("artifacts") or []
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                invalid.append({"job_id": job.get("id"), "reason": "artifact metadata is not an object"})
                continue
            category, entry = artifact_decision(artifact_root, job, artifact, protected, normalized_namespaces, cutoff)
            if category == "candidate":
                candidates.append(entry)
            elif category == "invalid":
                invalid.append(entry)
            else:
                kept.append(entry)

    return {
        "status": "planned",
        "policy": {
            "delete_older_than_days": delete_older_than_days,
            "cutoff": cutoff.isoformat(),
            "namespaces": normalized_namespaces,
            "limit": limit,
        },
        "candidate_count": len(candidates),
        "kept_count": len(kept),
        "invalid_preserved_count": len(invalid),
        "total_reclaimable_bytes": sum(int(item.get("size_bytes") or 0) for item in candidates),
        "candidates": candidates,
        "kept": kept,
        "invalid_preserved": invalid,
    }


def mark_deleted_job_artifacts(
    jobs: list[dict[str, Any]],
    deleted: list[dict[str, Any]],
    *,
    deleted_at: datetime,
    reason: str,
) -> list[dict[str, Any]]:
    deleted_by_job: dict[str, set[str]] = {}
    deleted_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in deleted:
        job_id = str(entry.get("job_id") or "")
        url = str(entry.get("url") or "")
        if not job_id or not url:
            continue
        deleted_by_job.setdefault(job_id, set()).add(url)
        deleted_metadata[(job_id, url)] = entry

    updates: list[dict[str, Any]] = []
    timestamp = deleted_at.astimezone(UTC).isoformat()
    for job in jobs:
        job_id = str(job.get("id") or "")
        urls = deleted_by_job.get(job_id)
        if not urls:
            continue
        changed = False
        artifacts: list[dict[str, Any]] = []
        for artifact in job.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            url = artifact.get("url")
            if isinstance(url, str) and url in urls:
                metadata = deleted_metadata.get((job_id, url), {})
                artifacts.append(
                    {
                        **artifact,
                        "deleted_at": timestamp,
                        "retention_status": "deleted",
                        "retention_reason": reason,
                        "deleted_size_bytes": metadata.get("size_bytes", artifact.get("bytes")),
                    }
                )
                changed = True
            else:
                artifacts.append(artifact)
        if changed:
            updates.append({"job_id": job_id, "artifacts": artifacts})
    return updates


def apply_artifact_retention_plan(
    artifact_root: Path,
    jobs: list[dict[str, Any]],
    *,
    delete_older_than_days: int,
    protected_urls: set[str] | None = None,
    namespaces: list[str] | None = None,
    confirmed: bool = False,
    now: datetime | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    plan = build_artifact_retention_plan(
        artifact_root,
        jobs,
        delete_older_than_days=delete_older_than_days,
        protected_urls=protected_urls,
        namespaces=namespaces,
        now=now,
        limit=limit,
    )
    if not confirmed:
        raise ArtifactRetentionError("artifact cleanup requires explicit confirmation")
    deleted: list[dict[str, Any]] = []
    for candidate in plan["candidates"]:
        target = artifact_path(artifact_root, candidate["path"])
        if path_has_symlink_parent(target, artifact_root) or target.is_symlink() or not target.is_file():
            raise ArtifactRetentionError(f"artifact candidate changed before deletion: {candidate['path']}")
        target.unlink()
        deleted.append({**candidate, "status": "deleted"})

    deleted_at = (now or datetime.now(tz=UTC)).astimezone(UTC)
    job_updates = mark_deleted_job_artifacts(
        jobs,
        deleted,
        deleted_at=deleted_at,
        reason=f"older_than_{delete_older_than_days}_days",
    )
    return {
        **plan,
        "status": "applied" if deleted else "noop",
        "deleted_count": len(deleted),
        "deleted": deleted,
        "job_update_count": len(job_updates),
        "job_updates": job_updates,
    }
