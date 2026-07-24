from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CUTOVER_PLAN_FORMAT = "b1-ai-hub-cutover-plan/v1"
ROLLBACK_REHEARSAL_FORMAT = "b1-ai-hub-rollback-rehearsal/v1"
CUTOVER_PLAN_NAME_RE = re.compile(r"cutover-plan(?:-[0-9]{8}[-t]?[0-9]{6}z?)?\.json", re.IGNORECASE)
ROLLBACK_REPORT_NAME = "rollback-rehearsal.json"


class RollbackRehearsalError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RollbackRehearsalError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RollbackRehearsalError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RollbackRehearsalError(f"{path} must contain a JSON object")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def coerce_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RollbackRehearsalError(f"{label} must be a list")
    return value


def resolve_backup_child(root: Path, name: str, *, expected: str) -> Path:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise RollbackRehearsalError(f"invalid {expected} name")
    path = (root / name).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RollbackRehearsalError(f"{expected} path escapes the backup root") from exc
    if not path.is_file():
        raise RollbackRehearsalError(f"{expected} file does not exist: {name}")
    return path


def resolve_cutover_plan(backup_root: Path, cutover_plan_name: str | None = None) -> Path:
    root = backup_root.resolve()
    if cutover_plan_name:
        if not CUTOVER_PLAN_NAME_RE.fullmatch(cutover_plan_name):
            raise RollbackRehearsalError("cutover plan name must be a generated cutover-plan*.json file")
        return resolve_backup_child(root, cutover_plan_name, expected="cutover plan")
    candidates = [
        path
        for path in root.glob("cutover-plan*.json")
        if path.is_file() and CUTOVER_PLAN_NAME_RE.fullmatch(path.name)
    ]
    if not candidates:
        raise RollbackRehearsalError("no generated cutover-plan*.json file exists under the backup root")
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name)).resolve()


def validate_cutover_plan(plan: dict[str, Any], cutover_plan_path: Path) -> dict[str, Any]:
    if plan.get("format") != CUTOVER_PLAN_FORMAT:
        raise RollbackRehearsalError("cutover plan has unsupported format")
    warnings = coerce_list(plan.get("warnings"), "cutover plan warnings")
    if warnings:
        raise RollbackRehearsalError("cutover plan still has unresolved warnings")

    safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
    if safety.get("deletes_nothing") is not True:
        raise RollbackRehearsalError("cutover plan does not guarantee deletes_nothing")
    if safety.get("old_stack_deletion_allowed") is not False:
        raise RollbackRehearsalError("cutover plan does not forbid old-stack deletion")

    old_scope = plan.get("old_stack_scope") if isinstance(plan.get("old_stack_scope"), dict) else {}
    resources = {
        "containers_to_restart_for_rollback": coerce_list(
            old_scope.get("containers_to_restart_for_rollback"),
            "old_stack_scope.containers_to_restart_for_rollback",
        ),
        "docker_volumes_preserved": coerce_list(
            old_scope.get("docker_volumes_preserved"),
            "old_stack_scope.docker_volumes_preserved",
        ),
        "host_paths_preserved": coerce_list(
            old_scope.get("host_paths_preserved"),
            "old_stack_scope.host_paths_preserved",
        ),
    }
    resource_count = sum(len(items) for items in resources.values())
    if resource_count <= 0:
        raise RollbackRehearsalError("cutover plan lists no preserved rollback resources")

    phases = coerce_list(plan.get("phases"), "phases")
    rollback_phase = next(
        (phase for phase in phases if isinstance(phase, dict) and phase.get("name") == "rollback"),
        None,
    )
    if not rollback_phase:
        raise RollbackRehearsalError("cutover plan does not include a rollback phase")
    commands = coerce_list(rollback_phase.get("commands"), "rollback.commands")
    operator_actions = coerce_list(rollback_phase.get("operator_actions"), "rollback.operator_actions")
    if not commands and not operator_actions:
        raise RollbackRehearsalError("rollback phase contains neither commands nor operator actions")

    return {
        "cutover_plan": str(cutover_plan_path.resolve()),
        "cutover_plan_name": cutover_plan_path.name,
        "cutover_plan_sha256": sha256_file(cutover_plan_path),
        "resources": resources,
        "resource_count": resource_count,
        "rollback_commands": commands,
        "rollback_operator_actions": operator_actions,
    }


def build_report(
    *,
    cutover_plan_path: Path,
    rehearsed_by: str,
    rollback_commands_tested: bool,
    old_resources_preserved: bool,
    notes: str | None = None,
) -> dict[str, Any]:
    operator = rehearsed_by.strip()
    if not operator:
        raise RollbackRehearsalError("rehearsed_by is required")
    if rollback_commands_tested is not True:
        raise RollbackRehearsalError("rollback_commands_tested must be explicitly confirmed")
    if old_resources_preserved is not True:
        raise RollbackRehearsalError("old_resources_preserved must be explicitly confirmed")

    resolved_plan = cutover_plan_path.resolve()
    plan = load_json_file(resolved_plan)
    summary = validate_cutover_plan(plan, resolved_plan)

    checks = {
        "rollback_commands_tested": {
            "status": "ok",
            "command_count": len(summary["rollback_commands"]),
            "operator_action_count": len(summary["rollback_operator_actions"]),
        },
        "old_resources_preserved": {
            "status": "ok",
            "resource_count": summary["resource_count"],
            "resources": summary["resources"],
        },
    }
    payload: dict[str, Any] = {
        "format": ROLLBACK_REHEARSAL_FORMAT,
        "generated_at": utc_now(),
        "status": "ok",
        "cutover_plan": summary["cutover_plan"],
        "cutover_plan_name": summary["cutover_plan_name"],
        "cutover_plan_sha256": summary["cutover_plan_sha256"],
        "rehearsed_by": operator,
        "checks": checks,
        "rollback": {
            "commands": summary["rollback_commands"],
            "operator_actions": summary["rollback_operator_actions"],
        },
    }
    if notes:
        payload["notes"] = notes
    return payload


def write_report(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def build_and_write_report(
    *,
    backup_root: Path,
    cutover_plan_name: str | None,
    rehearsed_by: str,
    rollback_commands_tested: bool,
    old_resources_preserved: bool,
    notes: str | None = None,
) -> dict[str, Any]:
    plan_path = resolve_cutover_plan(backup_root, cutover_plan_name)
    report = build_report(
        cutover_plan_path=plan_path,
        rehearsed_by=rehearsed_by,
        rollback_commands_tested=rollback_commands_tested,
        old_resources_preserved=old_resources_preserved,
        notes=notes,
    )
    output = write_report(backup_root.resolve() / ROLLBACK_REPORT_NAME, report)
    return {"status": report["status"], "output": str(output), "report": report}


def summarize_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"available": False, "path": str(path.resolve()), "reason": "rollback rehearsal report not found"}
    payload = load_json_file(path)
    if payload.get("format") != ROLLBACK_REHEARSAL_FORMAT:
        return {"available": False, "path": str(path.resolve()), "reason": "unsupported rollback rehearsal report format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    return {
        "available": True,
        "path": str(path.resolve()),
        "status": payload.get("status"),
        "generated_at": payload.get("generated_at"),
        "cutover_plan": payload.get("cutover_plan"),
        "cutover_plan_name": payload.get("cutover_plan_name"),
        "cutover_plan_sha256": payload.get("cutover_plan_sha256"),
        "rehearsed_by": payload.get("rehearsed_by"),
        "resource_count": (checks.get("old_resources_preserved") or {}).get("resource_count") if isinstance(checks.get("old_resources_preserved"), dict) else None,
    }


def status(backup_root: Path) -> dict[str, Any]:
    root = backup_root.resolve()
    report_path = root / ROLLBACK_REPORT_NAME
    try:
        plan_path = resolve_cutover_plan(root)
        plan = load_json_file(plan_path)
        cutover_plan = validate_cutover_plan(plan, plan_path)
        cutover_status: dict[str, Any] = {
            "available": True,
            "path": cutover_plan["cutover_plan"],
            "name": cutover_plan["cutover_plan_name"],
            "sha256": cutover_plan["cutover_plan_sha256"],
            "resource_count": cutover_plan["resource_count"],
            "rollback_command_count": len(cutover_plan["rollback_commands"]),
            "rollback_operator_action_count": len(cutover_plan["rollback_operator_actions"]),
        }
    except RollbackRehearsalError as exc:
        cutover_status = {"available": False, "reason": str(exc)}
    try:
        report_status = summarize_report(report_path)
    except RollbackRehearsalError as exc:
        report_status = {"available": False, "path": str(report_path.resolve()), "reason": str(exc)}
    return {
        "format": "b1-ai-hub-rollback-rehearsal-status/v1",
        "backup_root": str(root),
        "cutover_plan": cutover_status,
        "report": report_status,
    }
