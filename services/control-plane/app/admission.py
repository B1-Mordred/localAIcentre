from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


QUEUE_STATES = frozenset({"created", "validated", "queued", "waiting_for_gpu"})
ACTIVE_STATES = frozenset({"unloading", "verifying_vram", "loading", "warming", "running", "saving", "cancelling"})


@dataclass(frozen=True)
class AdmissionPolicy:
    max_queued_jobs_per_owner: int
    max_active_jobs_per_owner: int
    max_jobs_per_hour_per_owner: int
    max_queued_jobs_global: int
    artifact_storage_max_bytes: int
    artifact_storage_reserve_bytes: int


@dataclass(frozen=True)
class QueueAdmissionSnapshot:
    owner_id: str
    owner_queued_jobs: int
    owner_active_jobs: int
    owner_jobs_last_hour: int
    global_queued_jobs: int


@dataclass(frozen=True)
class ArtifactStorageSnapshot:
    root: str
    exists: bool
    artifact_bytes: int | None
    disk_total_bytes: int | None
    disk_used_bytes: int | None
    disk_free_bytes: int | None
    artifact_storage_max_bytes: int
    artifact_storage_reserve_bytes: int


class AdmissionDeniedError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 429, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


def policy_from_settings(settings: Any) -> AdmissionPolicy:
    return AdmissionPolicy(
        max_queued_jobs_per_owner=max(0, int(getattr(settings, "max_queued_jobs_per_owner", 20))),
        max_active_jobs_per_owner=max(0, int(getattr(settings, "max_active_jobs_per_owner", 3))),
        max_jobs_per_hour_per_owner=max(0, int(getattr(settings, "max_jobs_per_hour_per_owner", 60))),
        max_queued_jobs_global=max(0, int(getattr(settings, "max_queued_jobs_global", 100))),
        artifact_storage_max_bytes=max(0, int(getattr(settings, "artifact_storage_max_bytes", 0))),
        artifact_storage_reserve_bytes=max(0, int(getattr(settings, "artifact_storage_reserve_bytes", 10 * 1024**3))),
    )


def _artifact_tree_size(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            path = base / dirname
            if not path.is_symlink():
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            path = base / filename
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                total += path.stat().st_size
            except OSError:
                continue
    return total


def artifact_storage_snapshot(root: Path, policy: AdmissionPolicy) -> ArtifactStorageSnapshot:
    root = root.resolve()
    exists = root.exists()
    usage_path = root if exists else root.parent
    while not usage_path.exists() and usage_path != usage_path.parent:
        usage_path = usage_path.parent
    try:
        usage = shutil.disk_usage(usage_path)
        disk_total = int(usage.total)
        disk_used = int(usage.used)
        disk_free = int(usage.free)
    except OSError:
        disk_total = None
        disk_used = None
        disk_free = None
    artifact_bytes = _artifact_tree_size(root) if exists and policy.artifact_storage_max_bytes > 0 else None
    return ArtifactStorageSnapshot(
        root=str(root),
        exists=exists,
        artifact_bytes=artifact_bytes,
        disk_total_bytes=disk_total,
        disk_used_bytes=disk_used,
        disk_free_bytes=disk_free,
        artifact_storage_max_bytes=policy.artifact_storage_max_bytes,
        artifact_storage_reserve_bytes=policy.artifact_storage_reserve_bytes,
    )


def enforce_queue_admission(policy: AdmissionPolicy, snapshot: QueueAdmissionSnapshot) -> None:
    if policy.max_queued_jobs_per_owner and snapshot.owner_queued_jobs >= policy.max_queued_jobs_per_owner:
        raise AdmissionDeniedError(
            "owner_queue_limit",
            "owner queued job limit reached",
            details={"limit": policy.max_queued_jobs_per_owner, "current": snapshot.owner_queued_jobs, "owner_id": snapshot.owner_id},
        )
    if policy.max_active_jobs_per_owner and snapshot.owner_active_jobs >= policy.max_active_jobs_per_owner:
        raise AdmissionDeniedError(
            "owner_active_limit",
            "owner active job limit reached",
            details={"limit": policy.max_active_jobs_per_owner, "current": snapshot.owner_active_jobs, "owner_id": snapshot.owner_id},
        )
    if policy.max_jobs_per_hour_per_owner and snapshot.owner_jobs_last_hour >= policy.max_jobs_per_hour_per_owner:
        raise AdmissionDeniedError(
            "owner_rate_limit",
            "owner hourly media job limit reached",
            details={"limit": policy.max_jobs_per_hour_per_owner, "current": snapshot.owner_jobs_last_hour, "owner_id": snapshot.owner_id},
        )
    if policy.max_queued_jobs_global and snapshot.global_queued_jobs >= policy.max_queued_jobs_global:
        raise AdmissionDeniedError(
            "global_queue_limit",
            "global queued job limit reached",
            details={"limit": policy.max_queued_jobs_global, "current": snapshot.global_queued_jobs},
        )


def enforce_artifact_storage_admission(policy: AdmissionPolicy, snapshot: ArtifactStorageSnapshot, incoming_bytes: int = 0) -> None:
    incoming = max(0, int(incoming_bytes))
    if policy.artifact_storage_max_bytes and snapshot.artifact_bytes is not None:
        projected = snapshot.artifact_bytes + incoming
        if projected > policy.artifact_storage_max_bytes:
            raise AdmissionDeniedError(
                "artifact_storage_limit",
                "artifact storage limit reached",
                status_code=507,
                details={
                    "limit": policy.artifact_storage_max_bytes,
                    "current": snapshot.artifact_bytes,
                    "incoming_bytes": incoming,
                    "projected": projected,
                },
            )
    if policy.artifact_storage_reserve_bytes and snapshot.disk_free_bytes is not None:
        projected_free = snapshot.disk_free_bytes - incoming
        if projected_free < policy.artifact_storage_reserve_bytes:
            raise AdmissionDeniedError(
                "artifact_storage_reserve",
                "artifact storage reserve would be breached",
                status_code=507,
                details={
                    "reserve_bytes": policy.artifact_storage_reserve_bytes,
                    "free_bytes": snapshot.disk_free_bytes,
                    "incoming_bytes": incoming,
                    "projected_free_bytes": projected_free,
                },
            )


def public_report(
    policy: AdmissionPolicy,
    *,
    queue: QueueAdmissionSnapshot | None,
    storage: ArtifactStorageSnapshot,
) -> dict[str, Any]:
    return {
        "policy": {
            "max_queued_jobs_per_owner": policy.max_queued_jobs_per_owner,
            "max_active_jobs_per_owner": policy.max_active_jobs_per_owner,
            "max_jobs_per_hour_per_owner": policy.max_jobs_per_hour_per_owner,
            "max_queued_jobs_global": policy.max_queued_jobs_global,
            "artifact_storage_max_bytes": policy.artifact_storage_max_bytes,
            "artifact_storage_reserve_bytes": policy.artifact_storage_reserve_bytes,
        },
        "queue": None
        if queue is None
        else {
            "owner_id": queue.owner_id,
            "owner_queued_jobs": queue.owner_queued_jobs,
            "owner_active_jobs": queue.owner_active_jobs,
            "owner_jobs_last_hour": queue.owner_jobs_last_hour,
            "global_queued_jobs": queue.global_queued_jobs,
        },
        "storage": {
            "root": storage.root,
            "exists": storage.exists,
            "artifact_bytes": storage.artifact_bytes,
            "disk_total_bytes": storage.disk_total_bytes,
            "disk_used_bytes": storage.disk_used_bytes,
            "disk_free_bytes": storage.disk_free_bytes,
            "artifact_storage_max_bytes": storage.artifact_storage_max_bytes,
            "artifact_storage_reserve_bytes": storage.artifact_storage_reserve_bytes,
        },
    }
