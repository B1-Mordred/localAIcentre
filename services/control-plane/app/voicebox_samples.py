from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import artifact_retention
from . import artifacts as artifact_policy


class VoiceboxSampleRetentionError(ValueError):
    pass


def utc_from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def sample_root(artifact_root: Path) -> Path:
    return artifact_root.resolve(strict=False) / "voicebox" / "references"


def voicebox_sample_relative_path(artifact_root: Path, path: Path) -> str:
    root = artifact_root.resolve(strict=False)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise VoiceboxSampleRetentionError("sample path escapes artifact root") from exc
    url = artifact_policy.artifact_url_for_path(relative)
    if not url.startswith("/artifacts/voicebox/references/"):
        raise VoiceboxSampleRetentionError("sample path is outside Voicebox reference storage")
    return relative


def sample_entry(path: Path, relative: str, reason: str, *, stat_result: os.stat_result | None = None) -> dict[str, Any]:
    stat = stat_result
    if stat is None:
        try:
            stat = path.stat()
        except OSError:
            stat = None
    return {
        "path": relative,
        "url": f"/artifacts/{relative}",
        "size_bytes": int(stat.st_size) if stat is not None else 0,
        "modified_at": utc_from_timestamp(stat.st_mtime).isoformat() if stat is not None else None,
        "reason": reason,
    }


def iter_sample_paths(artifact_root: Path, limit: int) -> tuple[list[Path], list[dict[str, Any]], bool]:
    root = artifact_root.resolve(strict=False)
    references = sample_root(root)
    if not references.exists():
        return [], [], False
    if references.is_symlink():
        return [], [{"path": "voicebox/references", "reason": "sample root is a symlink"}], False
    if not references.is_dir():
        return [], [{"path": "voicebox/references", "reason": "sample root is not a directory"}], False

    paths: list[Path] = []
    invalid: list[dict[str, Any]] = []
    truncated = False
    for directory, dirnames, filenames in os.walk(references, topdown=True, followlinks=False):
        base = Path(directory)
        allowed_dirnames: list[str] = []
        for dirname in dirnames:
            candidate = base / dirname
            try:
                relative = voicebox_sample_relative_path(root, candidate)
            except (artifact_policy.ArtifactAccessError, VoiceboxSampleRetentionError) as exc:
                invalid.append({"path": candidate.as_posix(), "reason": str(exc)})
                continue
            if candidate.is_symlink():
                invalid.append(sample_entry(candidate, relative, "sample directory is a symlink"))
            else:
                allowed_dirnames.append(dirname)
        dirnames[:] = allowed_dirnames

        for filename in filenames:
            candidate = base / filename
            paths.append(candidate)
            if len(paths) >= limit:
                truncated = True
                return paths, invalid, truncated
    return paths, invalid, truncated


def sample_decision(artifact_root: Path, path: Path, protected_urls: set[str], cutoff: datetime) -> tuple[str, dict[str, Any]]:
    root = artifact_root.resolve(strict=False)
    try:
        relative = voicebox_sample_relative_path(root, path)
    except (artifact_policy.ArtifactAccessError, VoiceboxSampleRetentionError) as exc:
        return "invalid", {"path": path.as_posix(), "reason": str(exc)}
    url = f"/artifacts/{relative}"
    if url in protected_urls:
        return "kept", sample_entry(path, relative, "protected_by_voice_profile")
    if path.is_symlink():
        return "invalid", sample_entry(path, relative, "sample file is a symlink")
    if artifact_retention.path_has_symlink_parent(path, root):
        return "invalid", sample_entry(path, relative, "sample path contains a symlink")
    if not path.exists():
        return "invalid", sample_entry(path, relative, "sample file is missing")
    if not path.is_file():
        return "invalid", sample_entry(path, relative, "sample path is not a file")
    stat = path.stat()
    modified_at = utc_from_timestamp(stat.st_mtime)
    if modified_at >= cutoff:
        return "kept", sample_entry(path, relative, "sample_newer_than_retention_cutoff", stat_result=stat)
    return "candidate", sample_entry(path, relative, "unreferenced_sample_older_than_retention_cutoff", stat_result=stat)


def build_voicebox_sample_retention_plan(
    artifact_root: Path,
    *,
    delete_older_than_days: int,
    protected_urls: set[str] | None = None,
    now: datetime | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    if delete_older_than_days < 1:
        raise VoiceboxSampleRetentionError("delete_older_than_days must be at least one day")
    if limit < 1:
        raise VoiceboxSampleRetentionError("limit must be at least one")
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    cutoff = current - timedelta(days=delete_older_than_days)
    protected = protected_urls or set()
    paths, invalid_from_scan, truncated = iter_sample_paths(artifact_root, limit)

    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    invalid = list(invalid_from_scan)
    for path in paths:
        category, entry = sample_decision(artifact_root, path, protected, cutoff)
        if category == "candidate":
            candidates.append(entry)
        elif category == "invalid":
            invalid.append(entry)
        else:
            kept.append(entry)

    return {
        "status": "planned",
        "root": sample_root(artifact_root).as_posix(),
        "policy": {"delete_older_than_days": delete_older_than_days, "cutoff": cutoff.isoformat(), "limit": limit},
        "candidate_count": len(candidates),
        "kept_count": len(kept),
        "invalid_preserved_count": len(invalid),
        "total_reclaimable_bytes": sum(int(item.get("size_bytes") or 0) for item in candidates),
        "truncated": truncated,
        "candidates": candidates,
        "kept": kept,
        "invalid_preserved": invalid,
    }


def apply_voicebox_sample_retention_plan(
    artifact_root: Path,
    *,
    delete_older_than_days: int,
    protected_urls: set[str] | None = None,
    now: datetime | None = None,
    limit: int = 5000,
    confirmed: bool = False,
) -> dict[str, Any]:
    if not confirmed:
        raise VoiceboxSampleRetentionError("confirm=true is required to delete Voicebox sample artifacts")
    plan = build_voicebox_sample_retention_plan(
        artifact_root,
        delete_older_than_days=delete_older_than_days,
        protected_urls=protected_urls,
        now=now,
        limit=limit,
    )
    deleted: list[dict[str, Any]] = []
    for candidate in plan["candidates"]:
        relative = str(candidate.get("path") or "")
        target = artifact_retention.artifact_path(artifact_root, relative)
        if target.is_symlink() or artifact_retention.path_has_symlink_parent(target, artifact_root.resolve(strict=False)) or not target.is_file():
            continue
        target.unlink()
        deleted.append({**candidate, "status": "deleted"})
    return {
        **plan,
        "status": "applied",
        "deleted": deleted,
        "deleted_count": len(deleted),
        "total_reclaimed_bytes": sum(int(item.get("size_bytes") or 0) for item in deleted),
    }
