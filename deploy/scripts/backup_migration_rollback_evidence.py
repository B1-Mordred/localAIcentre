#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from app import backup_restore  # noqa: E402
import old_stack_backup  # noqa: E402


EVIDENCE_FORMAT = "b1-ai-hub-backup-migration-rollback-acceptance/v1"
ROLLBACK_REHEARSAL_FORMAT = "b1-ai-hub-rollback-rehearsal/v1"
INVENTORY_FORMAT = old_stack_backup.INVENTORY_FORMAT
OPEN_WEBUI_PLAN_FORMAT = "b1-ai-hub-open-webui-migration-plan/v1"
CUTOVER_PLAN_FORMAT = "b1-ai-hub-cutover-plan/v1"
REQUIRED_CHECKS = (
    "b1_backup_created",
    "b1_backup_verified",
    "b1_restore_rehearsed",
    "old_stack_inventory_reviewed",
    "old_stack_backup_verified",
    "open_webui_migration_plan_reviewed",
    "cutover_plan_reviewed",
    "rollback_rehearsed",
    "old_resources_preserved",
)


class EvidenceError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def read_key_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvidenceError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"{path} must contain a JSON object")
    return payload


def record_check(status: str = "ok", **data: Any) -> dict[str, Any]:
    return {"status": status, "recorded_at": utc_now(), **data}


def validate_format(payload: dict[str, Any], expected_format: str, label: str) -> None:
    if payload.get("format") != expected_format:
        raise EvidenceError(f"{label} has unsupported format")


def verify_b1_backup(path: Path, backup_encryption_key: str | None = None) -> dict[str, Any]:
    backup_dir = path.resolve()
    if not backup_dir.is_dir():
        raise EvidenceError(f"B1 backup is not a directory: {backup_dir}")
    manifest = backup_restore.load_manifest(backup_dir)
    if manifest.get("postgres_dump_included") is not True:
        raise EvidenceError("B1 backup manifest does not include PostgreSQL dump coverage")
    report = backup_restore.verify_backup(backup_dir.parent, backup_dir.name, backup_encryption_key)
    if report.get("status") != "verified":
        raise EvidenceError("B1 backup verification did not report verified")
    return {
        "manifest": manifest,
        "verification": report,
    }


def verify_restore_report(path: Path, b1_backup: Path, b1_manifest: dict[str, Any]) -> dict[str, Any]:
    report = load_json_file(path)
    if report.get("status") != "restored":
        raise EvidenceError("restore report status is not restored")
    reported_backup = str(report.get("backup") or "")
    backup_dir = str(b1_backup.resolve())
    if reported_backup not in {backup_dir, b1_backup.name}:
        raise EvidenceError("restore report does not reference the verified B1 backup")
    file_count = len(b1_manifest.get("files") or [])
    if int(report.get("files_verified") or 0) != file_count:
        raise EvidenceError("restore report verified file count does not match B1 backup manifest")
    if b1_manifest.get("postgres_dump_included") and report.get("postgres_dump_included") is not True:
        raise EvidenceError("restore report does not preserve PostgreSQL dump coverage")
    return report


def verify_inventory(path: Path) -> dict[str, Any]:
    payload = load_json_file(path)
    validate_format(payload, INVENTORY_FORMAT, "inventory")
    classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
    return {
        "path": str(path.resolve()),
        "container_classification_count": len(classification.get("containers") or []),
    }


def verify_open_webui_plan(path: Path, inventory_path: Path, old_stack_backup_path: Path) -> dict[str, Any]:
    payload = load_json_file(path)
    validate_format(payload, OPEN_WEBUI_PLAN_FORMAT, "Open WebUI migration plan")
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    if str(Path(str(inputs.get("inventory") or "")).resolve()) != str(inventory_path.resolve()):
        raise EvidenceError("Open WebUI migration plan does not match the inventory path")
    if str(Path(str(inputs.get("old_stack_backup") or inputs.get("backup") or "")).resolve()) != str(old_stack_backup_path.resolve()):
        raise EvidenceError("Open WebUI migration plan does not match the old-stack backup path")
    if payload.get("warnings"):
        raise EvidenceError("Open WebUI migration plan still has warnings")
    open_webui = payload.get("open_webui") if isinstance(payload.get("open_webui"), dict) else {}
    return {
        "path": str(path.resolve()),
        "recommended_strategy": open_webui.get("recommended_strategy"),
        "readable_database_count": open_webui.get("readable_database_count"),
    }


def verify_cutover_plan(path: Path, inventory_path: Path, old_stack_backup_path: Path, open_webui_plan_path: Path) -> dict[str, Any]:
    payload = load_json_file(path)
    validate_format(payload, CUTOVER_PLAN_FORMAT, "cutover plan")
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    if str(Path(str(inputs.get("inventory") or "")).resolve()) != str(inventory_path.resolve()):
        raise EvidenceError("cutover plan does not match the inventory path")
    backup_verification = inputs.get("old_stack_backup_verification") if isinstance(inputs.get("old_stack_backup_verification"), dict) else {}
    backup_input = inputs.get("old_stack_backup") or backup_verification.get("backup")
    if str(Path(str(backup_input or "")).resolve()) != str(old_stack_backup_path.resolve()):
        raise EvidenceError("cutover plan does not match the old-stack backup path")
    if str(Path(str(inputs.get("open_webui_migration_plan") or "")).resolve()) != str(open_webui_plan_path.resolve()):
        raise EvidenceError("cutover plan does not match the Open WebUI plan path")
    if payload.get("warnings"):
        raise EvidenceError("cutover plan still has warnings")
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    if safety.get("deletes_nothing") is not True or safety.get("old_stack_deletion_allowed") is not False:
        raise EvidenceError("cutover plan safety invariants are incomplete")
    old_scope = payload.get("old_stack_scope") if isinstance(payload.get("old_stack_scope"), dict) else {}
    resources = {
        "containers_to_restart_for_rollback": old_scope.get("containers_to_restart_for_rollback") or [],
        "docker_volumes_preserved": old_scope.get("docker_volumes_preserved") or [],
        "host_paths_preserved": old_scope.get("host_paths_preserved") or [],
    }
    resource_count = sum(len(value) for value in resources.values() if isinstance(value, list))
    if resource_count <= 0:
        raise EvidenceError("cutover plan lists no old resources for rollback")
    return {
        "path": str(path.resolve()),
        "resources": resources,
        "resource_count": resource_count,
        "reviewed_by": old_scope.get("reviewed_by"),
    }


def verify_rollback_rehearsal(path: Path, cutover_plan_path: Path) -> dict[str, Any]:
    payload = load_json_file(path)
    validate_format(payload, ROLLBACK_REHEARSAL_FORMAT, "rollback rehearsal report")
    if payload.get("status") != "ok":
        raise EvidenceError("rollback rehearsal report status is not ok")
    if str(Path(str(payload.get("cutover_plan") or "")).resolve()) != str(cutover_plan_path.resolve()):
        raise EvidenceError("rollback rehearsal report does not reference the cutover plan")
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    rollback_check = checks.get("rollback_commands_tested") if isinstance(checks.get("rollback_commands_tested"), dict) else {}
    preserved_check = checks.get("old_resources_preserved") if isinstance(checks.get("old_resources_preserved"), dict) else {}
    if rollback_check.get("status") != "ok":
        raise EvidenceError("rollback rehearsal report is missing rollback_commands_tested=ok")
    if preserved_check.get("status") != "ok":
        raise EvidenceError("rollback rehearsal report is missing old_resources_preserved=ok")
    return payload


def build_evidence(
    *,
    b1_backup: Path,
    restore_report: Path,
    inventory: Path,
    old_stack_backup_path: Path,
    open_webui_plan: Path,
    cutover_plan: Path,
    rollback_report: Path,
    backup_encryption_key: str | None = None,
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    b1 = verify_b1_backup(b1_backup, backup_encryption_key)
    b1_manifest = b1["manifest"]
    b1_verification = b1["verification"]
    checks["b1_backup_created"] = record_check(
        backup=str(b1_backup.resolve()),
        created_at=b1_manifest.get("created_at"),
        file_count=len(b1_manifest.get("files") or []),
        postgres_dump_included=bool(b1_manifest.get("postgres_dump_included")),
    )
    checks["b1_backup_verified"] = record_check(
        backup=str(b1_backup.resolve()),
        files_verified=b1_verification.get("file_count"),
        archive_sha256=b1_verification.get("archive_sha256"),
        postgres_native_dump_verified=bool(b1_verification.get("postgres_native_dump_verification")),
    )
    samples.append({"label": "b1-backup", "backup": str(b1_backup.resolve()), "files": len(b1_manifest.get("files") or [])})

    restore = verify_restore_report(restore_report, b1_backup, b1_manifest)
    checks["b1_restore_rehearsed"] = record_check(
        restore_report=str(restore_report.resolve()),
        target=restore.get("target"),
        files_verified=restore.get("files_verified"),
        postgres_native_dump_verified=bool(restore.get("postgres_native_dump_verification")),
    )
    samples.append({"label": "restore-test", "target": restore.get("target"), "files_verified": restore.get("files_verified")})

    inventory_summary = verify_inventory(inventory)
    checks["old_stack_inventory_reviewed"] = record_check(**inventory_summary)

    old_stack = old_stack_backup.verify_backup(old_stack_backup_path.resolve())
    checks["old_stack_backup_verified"] = record_check(
        backup=str(old_stack_backup_path.resolve()),
        files_verified=old_stack.get("files_verified"),
        contains_sensitive_data=old_stack.get("contains_sensitive_data"),
    )
    samples.append({"label": "old-stack-backup", "backup": str(old_stack_backup_path.resolve()), "files_verified": old_stack.get("files_verified")})

    open_webui = verify_open_webui_plan(open_webui_plan, inventory, old_stack_backup_path)
    checks["open_webui_migration_plan_reviewed"] = record_check(**open_webui)

    cutover = verify_cutover_plan(cutover_plan, inventory, old_stack_backup_path, open_webui_plan)
    checks["cutover_plan_reviewed"] = record_check(**cutover)

    rollback = verify_rollback_rehearsal(rollback_report, cutover_plan)
    checks["rollback_rehearsed"] = record_check(
        report=str(rollback_report.resolve()),
        rehearsed_by=rollback.get("rehearsed_by"),
        generated_at=rollback.get("generated_at"),
    )
    checks["old_resources_preserved"] = record_check(
        report=str(rollback_report.resolve()),
        resource_count=cutover["resource_count"],
        resources=cutover["resources"],
    )
    samples.append({"label": "rollback-runbook", "report": str(rollback_report.resolve()), "resource_count": cutover["resource_count"]})

    return {
        "format": EVIDENCE_FORMAT,
        "generated_at": utc_now(),
        "status": "ok" if all(checks.get(name, {}).get("status") == "ok" for name in REQUIRED_CHECKS) else "incomplete",
        "required_checks": list(REQUIRED_CHECKS),
        "checks": checks,
        "samples": samples,
    }


def write_evidence(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate B1 backup, migration, and rollback acceptance evidence.")
    parser.add_argument("--b1-backup", required=True, help="B1 AI Hub backup directory.")
    parser.add_argument("--restore-report", required=True, help="restore-report.json from a restore-to-alternate-directory rehearsal.")
    parser.add_argument("--inventory", required=True, help="Reviewed old-stack inventory JSON.")
    parser.add_argument("--old-stack-backup", required=True, help="Verified old-stack backup directory.")
    parser.add_argument("--open-webui-plan", required=True, help="Reviewed Open WebUI migration plan JSON.")
    parser.add_argument("--cutover-plan", required=True, help="Reviewed cutover/rollback plan JSON with no unresolved warnings.")
    parser.add_argument("--rollback-report", required=True, help="Rollback rehearsal report JSON.")
    parser.add_argument("--output", required=True, help="Output evidence JSON under $B1_BACKUP_ROOT/acceptance.")
    parser.add_argument("--backup-encryption-key-file", default=None, help="Key file for encrypted-only B1 backup archives.")
    args = parser.parse_args()
    try:
        evidence = build_evidence(
            b1_backup=Path(args.b1_backup),
            restore_report=Path(args.restore_report),
            inventory=Path(args.inventory),
            old_stack_backup_path=Path(args.old_stack_backup),
            open_webui_plan=Path(args.open_webui_plan),
            cutover_plan=Path(args.cutover_plan),
            rollback_report=Path(args.rollback_report),
            backup_encryption_key=read_key_file(args.backup_encryption_key_file) or None,
        )
        output = write_evidence(Path(args.output), evidence)
    except (EvidenceError, backup_restore.BackupError, backup_restore.RestoreError, old_stack_backup.OldStackBackupError) as exc:
        raise SystemExit(f"backup/migration/rollback evidence failed: {exc}") from exc
    print(json.dumps({"status": evidence["status"], "output": str(output)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
