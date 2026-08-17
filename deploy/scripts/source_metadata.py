from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping


COMMIT_RE = re.compile(r"^[a-fA-F0-9]{40}$")
TRUE_VALUES = {"1", "true", "yes", "y", "on", "dirty"}
FALSE_VALUES = {"0", "false", "no", "n", "off", "clean"}


def _bool_env(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def _int_env(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _repo_root(root: Path) -> Path:
    top_level = _git(root, "rev-parse", "--show-toplevel")
    return Path(top_level).resolve()


def _metadata_from_env(env: Mapping[str, str]) -> dict[str, Any]:
    commit = (env.get("B1_SOURCE_COMMIT") or env.get("GIT_COMMIT") or "").strip().lower()
    if not commit:
        return {}
    source_ref = (env.get("B1_SOURCE_REF") or env.get("GIT_BRANCH") or "").strip()
    source_dirty = _bool_env(env.get("B1_SOURCE_DIRTY") or env.get("GIT_DIRTY"))
    dirty_path_count = _int_env(
        env.get("B1_SOURCE_DIRTY_PATH_COUNT") or env.get("B1_DIRTY_PATH_COUNT") or env.get("GIT_DIRTY_PATH_COUNT")
    )
    metadata: dict[str, Any] = {
        "source": "environment",
        "source_commit": commit,
        "short_commit": commit[:12],
    }
    if source_ref:
        metadata["source_ref"] = source_ref
        metadata["source_branch"] = source_ref.removeprefix("refs/heads/")
    if source_dirty is not None:
        metadata["source_dirty"] = source_dirty
    if dirty_path_count is not None:
        metadata["dirty_path_count"] = dirty_path_count
    return metadata


def _metadata_from_git(root: Path) -> dict[str, Any]:
    repo_root = _repo_root(root)
    commit = _git(repo_root, "rev-parse", "HEAD").lower()
    if not COMMIT_RE.fullmatch(commit):
        return {}
    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    status_lines = [line for line in _git(repo_root, "status", "--porcelain", "--untracked-files=normal").splitlines() if line.strip()]
    metadata: dict[str, Any] = {
        "source": "git",
        "source_commit": commit,
        "short_commit": commit[:12],
        "source_dirty": bool(status_lines),
        "dirty_path_count": len(status_lines),
    }
    if branch and branch != "HEAD":
        metadata["source_ref"] = branch
        metadata["source_branch"] = branch
    return metadata


def source_metadata(
    *,
    environ: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    metadata = _metadata_from_env(env)
    if metadata:
        return metadata
    root_value = env.get("B1_EVIDENCE_SOURCE_ROOT") or env.get("B1_SOURCE_ROOT") or ""
    root = repo_root or Path(root_value).expanduser() if root_value else repo_root or Path.cwd()
    try:
        return _metadata_from_git(root)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}


def stamp_source_metadata(
    payload: dict[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if _bool_env((environ if environ is not None else os.environ).get("B1_EVIDENCE_SOURCE_METADATA")) is False:
        return dict(payload)
    stamped = dict(payload)
    if not force and "source_commit" in stamped:
        return stamped
    for key, value in source_metadata(environ=environ, repo_root=repo_root).items():
        if force or key not in stamped:
            stamped[key] = value
    return stamped
