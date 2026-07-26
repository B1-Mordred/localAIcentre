#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


EVIDENCE_FORMAT = "b1-ai-hub-repository-quality-evidence/v1"
DEFAULT_REQUIRED_CHECKS = ("quality_container", "secret_scan")
QUALITY_COVERAGE = (
    "compose_config",
    "production_compose_config",
    "caddy_config",
    "python_compile",
    "backend_unit_tests",
    "openapi_schema_drift",
    "openapi_client_drift",
    "compatibility_offline_tests",
    "security_offline_tests",
    "frontend_control_center_build",
    "frontend_control_center_audit",
    "frontend_media_studio_build",
    "frontend_media_studio_audit",
)
SECRET_SCAN_COVERAGE = ("source_secret_scan",)


class RepositoryQualityEvidenceError(RuntimeError):
    pass


def run_git(repo_root: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RepositoryQualityEvidenceError(f"git {' '.join(args)} failed") from exc
    return result.stdout.strip()


def source_state(repo_root: Path) -> dict[str, Any]:
    commit = run_git(repo_root, ["rev-parse", "HEAD"]).lower()
    branch = run_git(repo_root, ["branch", "--show-current"]) or "detached"
    status = run_git(repo_root, ["status", "--porcelain"])
    return {
        "source_commit": commit,
        "source_branch": branch,
        "source_dirty": bool(status),
        "dirty_path_count": len([line for line in status.splitlines() if line.strip()]),
    }


def check_record(command: str, coverage: tuple[str, ...], generated_at: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "command": command,
        "recorded_at": generated_at,
        "coverage": list(coverage),
    }


def build_evidence(
    *,
    repo_root: Path,
    quality_command: str,
    secret_scan_command: str,
    generated_at: datetime | None = None,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    timestamp = (generated_at or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
    source = source_state(repo_root)
    checks = {
        "quality_container": check_record(quality_command, QUALITY_COVERAGE, timestamp),
        "secret_scan": check_record(secret_scan_command, SECRET_SCAN_COVERAGE, timestamp),
    }
    status = "ok"
    if source["source_dirty"] and not allow_dirty:
        status = "incomplete"
    return {
        "format": EVIDENCE_FORMAT,
        "generated_at": timestamp,
        "status": status,
        "repo_root": str(repo_root),
        "required_checks": list(DEFAULT_REQUIRED_CHECKS),
        **source,
        "checks": checks,
        "samples": [
            {
                "label": "repository-quality",
                "source_commit": source["source_commit"],
                "source_branch": source["source_branch"],
                "source_dirty": source["source_dirty"],
                "command_count": len(checks),
            }
        ],
    }


def assert_writable_output(path: Path, *, force: bool) -> None:
    if path.exists():
        if path.is_symlink():
            raise RepositoryQualityEvidenceError(f"refusing to write through symlink: {path}")
        if not path.is_file():
            raise RepositoryQualityEvidenceError(f"refusing to overwrite non-regular file: {path}")
        if not force:
            raise RepositoryQualityEvidenceError(f"{path} already exists; rerun with --force to overwrite")
    current = path.parent
    while current != current.parent:
        if current.is_symlink():
            raise RepositoryQualityEvidenceError(f"refusing to write inside symlinked directory: {current}")
        current = current.parent


def write_private_json(path: Path, payload: dict[str, Any], *, force: bool = False) -> None:
    assert_writable_output(path, force=force)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o640)
        os.replace(tmp_path, path)
        path.chmod(0o640)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Write B1 AI Hub repository quality gate evidence after successful checks.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quality-command", default="make quality-container")
    parser.add_argument("--secret-scan-command", default="make secret-scan")
    parser.add_argument("--allow-dirty", action="store_true", help="Write incomplete rehearsal evidence even when the repository is dirty.")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    payload = build_evidence(
        repo_root=repo_root,
        quality_command=args.quality_command,
        secret_scan_command=args.secret_scan_command,
        allow_dirty=args.allow_dirty,
    )
    if payload["status"] != "ok" and not args.allow_dirty:
        raise RepositoryQualityEvidenceError(
            f"repository worktree is dirty ({payload['dirty_path_count']} paths); commit or clean before handoff evidence"
        )
    output = args.output if args.output.is_absolute() else Path.cwd() / args.output
    write_private_json(output, payload, force=args.force)
    print(f"wrote repository quality evidence to {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepositoryQualityEvidenceError as exc:
        print(f"repository quality evidence error: {exc}", file=sys.stderr)
        raise SystemExit(2)
