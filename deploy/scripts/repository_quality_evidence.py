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
QUALITY_TEST_RESULTS = (
    ("compose_config", "compose", "make compose-config"),
    (
        "production_compose_config",
        "compose",
        "make production-localai-compose-config production-comfyui-compose-config "
        "production-voicebox-compose-config production-env-compose-config",
    ),
    ("caddy_config", "gateway", "make caddy-config"),
    ("python_compile", "python", "python -m compileall -q services deploy integrations tests"),
    ("backend_unit_tests", "python", "python -m unittest discover -s tests/unit -v"),
    ("openapi_schema_drift", "api", "python deploy/scripts/generate_openapi.py --output docs/openapi.json --check"),
    ("openapi_client_drift", "api", "python deploy/scripts/generate_openapi_client.py --check"),
    ("compatibility_offline_tests", "python", "python -m unittest discover -s tests/compatibility -v"),
    ("security_offline_tests", "python", "python -m unittest discover -s tests/security -v"),
    ("frontend_control_center_build", "frontend", "npm --prefix web/control-center run build"),
    ("frontend_control_center_audit", "frontend", "npm --prefix web/control-center audit --omit=dev --audit-level=high"),
    ("frontend_media_studio_build", "frontend", "npm --prefix web/media-studio run build"),
    ("frontend_media_studio_audit", "frontend", "npm --prefix web/media-studio audit --omit=dev --audit-level=high"),
)
SECRET_SCAN_TEST_RESULTS = (
    ("source_secret_scan", "security", "python3 deploy/scripts/secret_scan.py"),
)


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


def result_summary(
    *,
    passed: bool,
    required_by_target: str,
    test_results: tuple[tuple[str, str, str], ...],
) -> dict[str, Any]:
    status = "passed" if passed else "unverified"
    verification_method = "make prerequisite completed before evidence writer" if passed else "not asserted"
    return {
        "outcome": status,
        "exit_code": 0 if passed else None,
        "verification_method": verification_method,
        "required_by_target": required_by_target,
        "test_result_count": len(test_results),
        "test_results": [
            {
                "label": label,
                "kind": kind,
                "command": command,
                "status": status,
            }
            for label, kind, command in test_results
        ],
    }


def check_record(
    command: str,
    coverage: tuple[str, ...],
    generated_at: str,
    *,
    passed: bool,
    test_results: tuple[tuple[str, str, str], ...],
) -> dict[str, Any]:
    return {
        "status": "ok" if passed else "unverified",
        "command": command,
        "recorded_at": generated_at,
        "coverage": list(coverage),
        "result_summary": result_summary(
            passed=passed,
            required_by_target="repository-quality-evidence",
            test_results=test_results,
        ),
    }


def build_evidence(
    *,
    repo_root: Path,
    quality_command: str,
    secret_scan_command: str,
    generated_at: datetime | None = None,
    quality_passed: bool = False,
    secret_scan_passed: bool = False,
) -> dict[str, Any]:
    timestamp = (generated_at or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
    source = source_state(repo_root)
    checks = {
        "quality_container": check_record(
            quality_command,
            QUALITY_COVERAGE,
            timestamp,
            passed=quality_passed,
            test_results=QUALITY_TEST_RESULTS,
        ),
        "secret_scan": check_record(
            secret_scan_command,
            SECRET_SCAN_COVERAGE,
            timestamp,
            passed=secret_scan_passed,
            test_results=SECRET_SCAN_TEST_RESULTS,
        ),
    }
    status = "ok"
    if source["source_dirty"] or any(check["status"] != "ok" for check in checks.values()):
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
                "verified_check_count": sum(1 for check in checks.values() if check["status"] == "ok"),
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
    parser = argparse.ArgumentParser(
        description="Write B1 AI Hub repository quality gate evidence after successful checks."
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quality-command", default="make quality-container")
    parser.add_argument("--secret-scan-command", default="make secret-scan")
    parser.add_argument(
        "--quality-passed",
        action="store_true",
        help="Assert the quality command completed successfully before this script ran.",
    )
    parser.add_argument(
        "--secret-scan-passed",
        action="store_true",
        help="Assert the secret-scan command completed successfully before this script ran.",
    )
    parser.add_argument("--allow-dirty", action="store_true", help="Write incomplete rehearsal evidence even when the repository is dirty.")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    payload = build_evidence(
        repo_root=repo_root,
        quality_command=args.quality_command,
        secret_scan_command=args.secret_scan_command,
        quality_passed=args.quality_passed,
        secret_scan_passed=args.secret_scan_passed,
    )
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    missing_assertions = sorted(
        str(name)
        for name, check in checks.items()
        if isinstance(check, dict) and check.get("status") != "ok"
    )
    if missing_assertions:
        raise RepositoryQualityEvidenceError(
            "quality gate pass assertions missing for "
            + ", ".join(missing_assertions)
            + "; use make repository-quality-evidence or pass the explicit --quality-passed/--secret-scan-passed flags only after successful commands"
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
