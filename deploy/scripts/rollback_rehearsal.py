#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CUTOVER_PLAN_FORMAT = "b1-ai-hub-cutover-plan/v1"
ROLLBACK_REHEARSAL_FORMAT = "b1-ai-hub-rollback-rehearsal/v1"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a non-destructive rollback rehearsal report for B1 AI Hub acceptance.")
    parser.add_argument("--cutover-plan", required=True, help="Reviewed cutover-plan JSON with no unresolved warnings.")
    parser.add_argument("--output", required=True, help="Output rollback rehearsal report JSON.")
    parser.add_argument("--rehearsed-by", required=True, help="Operator name or identifier for the rehearsal.")
    parser.add_argument("--rollback-commands-tested", action="store_true", help="Confirm rollback commands/actions were rehearsed.")
    parser.add_argument("--old-resources-preserved", action="store_true", help="Confirm old-stack resources are still preserved.")
    parser.add_argument("--notes", default=None, help="Optional operator notes.")
    args = parser.parse_args()
    try:
        report = build_report(
            cutover_plan_path=Path(args.cutover_plan),
            rehearsed_by=args.rehearsed_by,
            rollback_commands_tested=args.rollback_commands_tested,
            old_resources_preserved=args.old_resources_preserved,
            notes=args.notes,
        )
        output = write_report(Path(args.output), report)
    except RollbackRehearsalError as exc:
        raise SystemExit(f"rollback rehearsal report failed: {exc}") from exc
    print(json.dumps({"status": report["status"], "output": str(output)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
