from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import backup_restore
from . import rollback_rehearsal


EVIDENCE_FORMAT = "b1-ai-hub-backup-migration-rollback-acceptance/v1"
OLD_STACK_BACKUP_FORMAT = "b1-ai-hub-old-stack-backup/v1"
INVENTORY_FORMAT = "b1-ai-hub-host-inventory/v1"
OPEN_WEBUI_PLAN_FORMAT = "b1-ai-hub-open-webui-migration-plan/v1"
CUTOVER_PLAN_FORMAT = "b1-ai-hub-cutover-plan/v1"
ROLLBACK_REHEARSAL_FORMAT = rollback_rehearsal.ROLLBACK_REHEARSAL_FORMAT
EVIDENCE_FILE_NAME = "backup-migration-rollback.json"
ACCEPTANCE_DIR_NAME = "acceptance"
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_archive_path(value: str) -> str:
    if "\x00" in value:
        raise EvidenceError("archive path contains NUL byte")
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or not relative.parts:
        raise EvidenceError(f"archive path escapes payload root: {value}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise EvidenceError(f"archive path escapes payload root: {value}")
    return relative.as_posix()


def record_check(status: str = "ok", **data: Any) -> dict[str, Any]:
    return {"status": status, "recorded_at": utc_now(), **data}


def validate_format(payload: dict[str, Any], expected_format: str, label: str) -> None:
    if payload.get("format") != expected_format:
        raise EvidenceError(f"{label} has unsupported format")


def _same_resolved_path(value: Any, expected: Path) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    return str(Path(raw).resolve()) == str(expected.resolve())


def verify_b1_backup(path: Path, backup_encryption_key: str | None = None) -> dict[str, Any]:
    backup_dir = path.resolve()
    if not backup_dir.is_dir():
        raise EvidenceError(f"B1 backup is not a directory: {backup_dir}")
    try:
        manifest = backup_restore.load_manifest(backup_dir)
    except backup_restore.BackupError as exc:
        raise EvidenceError(str(exc)) from exc
    if manifest.get("postgres_dump_included") is not True:
        raise EvidenceError("B1 backup manifest does not include PostgreSQL dump coverage")
    try:
        report = backup_restore.verify_backup(backup_dir.parent, backup_dir.name, backup_encryption_key)
    except backup_restore.BackupError as exc:
        raise EvidenceError(str(exc)) from exc
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


def verify_old_stack_backup(backup_dir: Path) -> dict[str, Any]:
    backup_dir = backup_dir.resolve()
    manifest = load_json_file(backup_dir / "manifest.json")
    validate_format(manifest, OLD_STACK_BACKUP_FORMAT, "old-stack backup")
    archive_record = manifest.get("archive") if isinstance(manifest.get("archive"), dict) else {}
    archive_name = str(archive_record.get("file") or "")
    if not archive_name:
        raise EvidenceError("old-stack backup manifest is missing archive.file")
    archive_path = backup_dir / archive_name
    if not archive_path.is_file():
        raise EvidenceError("missing old-stack backup archive")
    actual_archive_digest = sha256_file(archive_path)
    if actual_archive_digest != archive_record.get("sha256"):
        raise EvidenceError("old-stack archive checksum mismatch")

    expected: dict[str, dict[str, Any]] = {}
    for item in manifest.get("files") or []:
        if not isinstance(item, dict) or not item.get("archive_path"):
            raise EvidenceError("old-stack backup manifest contains an invalid file record")
        archive_item_path = safe_archive_path(str(item["archive_path"]))
        if archive_item_path in expected:
            raise EvidenceError(f"duplicate old-stack backup manifest member: {archive_item_path}")
        expected[archive_item_path] = item

    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                member_path = safe_archive_path(member.name)
                if member_path in seen:
                    raise EvidenceError(f"duplicate old-stack archive member: {member.name}")
                if not member.isfile():
                    raise EvidenceError(f"unsupported old-stack archive member type: {member.name}")
                record = expected.get(member_path)
                if record is None:
                    raise EvidenceError(f"unmanifested old-stack archive member: {member.name}")
                if member.size != record.get("size_bytes"):
                    raise EvidenceError(f"old-stack archive member size mismatch: {member.name}")
                source = archive.extractfile(member)
                if source is None:
                    raise EvidenceError(f"cannot read old-stack archive member: {member.name}")
                digest = hashlib.sha256()
                read_bytes = 0
                with source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        read_bytes += len(chunk)
                        digest.update(chunk)
                if read_bytes != record.get("size_bytes") or digest.hexdigest() != record.get("sha256"):
                    raise EvidenceError(f"old-stack archive member checksum mismatch: {member.name}")
                seen.add(member_path)
    except tarfile.TarError as exc:
        raise EvidenceError(f"cannot read old-stack backup archive: {exc}") from exc
    missing = sorted(set(expected) - seen)
    if missing:
        raise EvidenceError(f"old-stack archive missing manifest members: {', '.join(missing[:5])}")
    return {
        "status": "verified",
        "backup": str(backup_dir),
        "files_verified": len(seen),
        "archive_sha256": actual_archive_digest,
        "contains_sensitive_data": bool(manifest.get("contains_sensitive_data")),
    }


def verify_open_webui_plan(path: Path, inventory_path: Path, old_stack_backup_path: Path) -> dict[str, Any]:
    payload = load_json_file(path)
    validate_format(payload, OPEN_WEBUI_PLAN_FORMAT, "Open WebUI migration plan")
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    if not _same_resolved_path(inputs.get("inventory"), inventory_path):
        raise EvidenceError("Open WebUI migration plan does not match the inventory path")
    old_stack_input = inputs.get("old_stack_backup") or inputs.get("backup")
    if not _same_resolved_path(old_stack_input, old_stack_backup_path):
        raise EvidenceError("Open WebUI migration plan does not match the old-stack backup path")
    if payload.get("warnings"):
        raise EvidenceError("Open WebUI migration plan still has warnings")
    open_webui = payload.get("open_webui") if isinstance(payload.get("open_webui"), dict) else {}
    return {
        "path": str(path.resolve()),
        "recommended_strategy": open_webui.get("recommended_strategy"),
        "readable_database_count": open_webui.get("readable_database_count"),
        "data_domains": open_webui.get("data_domains") if isinstance(open_webui.get("data_domains"), dict) else {},
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float)) and str(item)]


def _preserved_resource_count(resources: dict[str, list[str]]) -> int:
    return sum(len(resources.get(key, [])) for key in rollback_rehearsal.PRESERVED_RESOURCE_KEYS)


def verify_cutover_dns_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    dns = payload.get("dns_readiness") if isinstance(payload.get("dns_readiness"), dict) else {}
    if not dns:
        raise EvidenceError("cutover DNS readiness is missing")
    missing_hosts = _string_list(dns.get("missing_hosts"))
    divergent_hosts = _string_list(dns.get("divergent_hosts"))
    optional_missing_hosts = _string_list(dns.get("optional_missing_hosts"))
    optional_divergent_hosts = _string_list(dns.get("optional_divergent_hosts"))
    if dns.get("all_hosts_resolve") is not True or missing_hosts:
        raise EvidenceError("cutover DNS readiness is missing core host records")
    if dns.get("all_hosts_share_gateway_address") is not True or divergent_hosts:
        raise EvidenceError("cutover DNS readiness does not share a common gateway address")
    if dns.get("operator_must_review_dns") is True or optional_divergent_hosts:
        raise EvidenceError("cutover DNS readiness requires operator review")
    return {
        "all_hosts_resolve": True,
        "all_hosts_share_gateway_address": True,
        "operator_must_review_dns": False,
        "reference_host": dns.get("reference_host") or "",
        "common_addresses": _string_list(dns.get("common_addresses")),
        "missing_hosts": missing_hosts,
        "divergent_hosts": divergent_hosts,
        "optional_missing_hosts": optional_missing_hosts,
        "optional_divergent_hosts": optional_divergent_hosts,
    }


def verify_cutover_hardware_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    hardware = payload.get("hardware_readiness") if isinstance(payload.get("hardware_readiness"), dict) else {}
    if not hardware:
        raise EvidenceError("cutover hardware readiness is missing")
    warnings = _string_list(hardware.get("warnings"))
    if hardware.get("available") is not True:
        raise EvidenceError("cutover hardware readiness is unavailable")
    if hardware.get("accepted") is not True or hardware.get("operator_must_review_hardware") is True or warnings:
        raise EvidenceError("cutover hardware readiness requires operator review")
    return {
        "available": True,
        "accepted": True,
        "operator_must_review_hardware": False,
        "profile": hardware.get("profile") or "",
        "largest_gpu_vram_mib": hardware.get("largest_gpu_vram_mib"),
        "minimum_gpu_vram_mib": hardware.get("minimum_gpu_vram_mib"),
        "host_total_ram_mib": hardware.get("host_total_ram_mib"),
        "minimum_host_ram_mib": hardware.get("minimum_host_ram_mib"),
        "warnings": [],
    }


def verify_cutover_gpu_runtime_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    gpu_runtime = payload.get("gpu_runtime_readiness") if isinstance(payload.get("gpu_runtime_readiness"), dict) else {}
    if not gpu_runtime:
        raise EvidenceError("cutover GPU container runtime readiness is missing")
    warnings = _string_list(gpu_runtime.get("warnings"))
    if gpu_runtime.get("available") is not True:
        raise EvidenceError("cutover GPU container runtime readiness is unavailable")
    if gpu_runtime.get("accepted") is not True or gpu_runtime.get("operator_must_review_gpu_runtime") is True or warnings:
        raise EvidenceError("cutover GPU container runtime readiness requires operator review")
    return {
        "available": True,
        "accepted": True,
        "operator_must_review_gpu_runtime": False,
        "nvidia_smi_available": gpu_runtime.get("nvidia_smi_available"),
        "detected_gpu_count": gpu_runtime.get("detected_gpu_count"),
        "docker_nvidia_runtime_available": gpu_runtime.get("docker_nvidia_runtime_available"),
        "nvidia_container_toolkit_available": gpu_runtime.get("nvidia_container_toolkit_available"),
        "nvidia_container_toolkit_returncode": gpu_runtime.get("nvidia_container_toolkit_returncode"),
        "nvidia_container_toolkit_version": gpu_runtime.get("nvidia_container_toolkit_version") or "",
        "warnings": [],
    }


def verify_cutover_runtime_agent_socket_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    socket = payload.get("runtime_agent_socket_readiness") if isinstance(payload.get("runtime_agent_socket_readiness"), dict) else {}
    if not socket:
        raise EvidenceError("cutover runtime-agent Docker socket readiness is missing")
    warnings = _string_list(socket.get("warnings"))
    if socket.get("available") is not True:
        raise EvidenceError("cutover runtime-agent Docker socket readiness is unavailable")
    if (
        socket.get("runtime_agent_group_access_ready") is not True
        or socket.get("operator_must_review_runtime_agent_socket") is True
        or warnings
    ):
        raise EvidenceError("cutover runtime-agent Docker socket readiness requires operator review")
    return {
        "available": True,
        "runtime_agent_group_access_ready": True,
        "operator_must_review_runtime_agent_socket": False,
        "path": socket.get("path") or "",
        "gid": socket.get("gid"),
        "configured_gid": socket.get("configured_gid"),
        "configured_gid_matches": socket.get("configured_gid_matches"),
        "mode_octal": socket.get("mode_octal"),
        "warnings": [],
    }


def verify_cutover_open_webui_preservation(payload: dict[str, Any]) -> dict[str, Any]:
    preservation = payload.get("open_webui_preservation") if isinstance(payload.get("open_webui_preservation"), dict) else {}
    if not preservation:
        raise EvidenceError("cutover Open WebUI preservation is missing")
    plan_warnings = _string_list(preservation.get("plan_warnings"))
    if preservation.get("plan_supplied") is not True:
        raise EvidenceError("cutover Open WebUI preservation plan is missing")
    if preservation.get("operator_must_review_open_webui") is True or plan_warnings:
        raise EvidenceError("cutover Open WebUI preservation requires operator review")
    return {
        "plan_supplied": True,
        "operator_must_review_open_webui": False,
        "recommended_strategy": preservation.get("recommended_strategy") or "",
        "compatibility_status": preservation.get("compatibility_status") or "",
        "requires_temporary_instance_validation": preservation.get("requires_temporary_instance_validation"),
        "data_domains": preservation.get("data_domains") if isinstance(preservation.get("data_domains"), dict) else {},
        "plan_warnings": [],
    }


def verify_cutover_plan(path: Path, inventory_path: Path, old_stack_backup_path: Path, open_webui_plan_path: Path) -> dict[str, Any]:
    payload = load_json_file(path)
    validate_format(payload, CUTOVER_PLAN_FORMAT, "cutover plan")
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    if not _same_resolved_path(inputs.get("inventory"), inventory_path):
        raise EvidenceError("cutover plan does not match the inventory path")
    backup_verification = inputs.get("old_stack_backup_verification") if isinstance(inputs.get("old_stack_backup_verification"), dict) else {}
    backup_input = inputs.get("old_stack_backup") or backup_verification.get("backup")
    if not _same_resolved_path(backup_input, old_stack_backup_path):
        raise EvidenceError("cutover plan does not match the old-stack backup path")
    if not _same_resolved_path(inputs.get("open_webui_migration_plan"), open_webui_plan_path):
        raise EvidenceError("cutover plan does not match the Open WebUI plan path")
    if payload.get("warnings"):
        raise EvidenceError("cutover plan still has warnings")
    dns_readiness = verify_cutover_dns_readiness(payload)
    hardware_readiness = verify_cutover_hardware_readiness(payload)
    gpu_runtime_readiness = verify_cutover_gpu_runtime_readiness(payload)
    runtime_agent_socket_readiness = verify_cutover_runtime_agent_socket_readiness(payload)
    open_webui_preservation = verify_cutover_open_webui_preservation(payload)
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    if safety.get("deletes_nothing") is not True or safety.get("old_stack_deletion_allowed") is not False:
        raise EvidenceError("cutover plan safety invariants are incomplete")
    old_scope = payload.get("old_stack_scope") if isinstance(payload.get("old_stack_scope"), dict) else {}
    try:
        resources = rollback_rehearsal.normalized_preserved_resources(old_scope)
    except rollback_rehearsal.RollbackRehearsalError as exc:
        raise EvidenceError(str(exc)) from exc
    resource_count = _preserved_resource_count(resources)
    if resource_count <= 0:
        raise EvidenceError("cutover plan lists no old resources for rollback")
    rollback_phase = next(
        (
            phase
            for phase in payload.get("phases") or []
            if isinstance(phase, dict) and phase.get("name") == "rollback"
        ),
        {},
    )
    rollback_commands = rollback_phase.get("commands") if isinstance(rollback_phase.get("commands"), list) else []
    rollback_operator_actions = (
        rollback_phase.get("operator_actions") if isinstance(rollback_phase.get("operator_actions"), list) else []
    )
    if not rollback_commands and not rollback_operator_actions:
        raise EvidenceError("cutover plan rollback phase contains no commands or operator actions")
    rollback_actions_sha256 = rollback_rehearsal.rollback_actions_sha256(rollback_commands, rollback_operator_actions)
    return {
        "path": str(path.resolve()),
        "resources": resources,
        "resource_count": resource_count,
        "resource_counts_by_type": rollback_rehearsal.preserved_resource_counts(resources),
        "resources_sha256": rollback_rehearsal.preserved_resources_sha256(resources),
        "rollback_command_count": len(rollback_commands),
        "rollback_operator_action_count": len(rollback_operator_actions),
        "rollback_actions_sha256": rollback_actions_sha256,
        "dns_readiness": dns_readiness,
        "hardware_readiness": hardware_readiness,
        "gpu_runtime_readiness": gpu_runtime_readiness,
        "runtime_agent_socket_readiness": runtime_agent_socket_readiness,
        "open_webui_preservation": open_webui_preservation,
        "reviewed_by": old_scope.get("reviewed_by"),
        "_rollback_actions": {
            "commands": rollback_commands,
            "operator_actions": rollback_operator_actions,
            "sha256": rollback_actions_sha256,
        },
    }


def verify_rollback_rehearsal(
    path: Path,
    cutover_plan_path: Path,
    *,
    expected_resources: dict[str, list[str]],
    expected_resource_count: int,
    expected_resources_sha256: str,
    expected_rollback_actions: dict[str, Any],
) -> dict[str, Any]:
    payload = load_json_file(path)
    validate_format(payload, ROLLBACK_REHEARSAL_FORMAT, "rollback rehearsal report")
    if payload.get("status") != "ok":
        raise EvidenceError("rollback rehearsal report status is not ok")
    if not _same_resolved_path(payload.get("cutover_plan"), cutover_plan_path):
        raise EvidenceError("rollback rehearsal report does not reference the cutover plan")
    reported_sha256 = str(payload.get("cutover_plan_sha256") or "")
    if not reported_sha256:
        raise EvidenceError("rollback rehearsal report is missing cutover_plan_sha256")
    if reported_sha256 != sha256_file(cutover_plan_path):
        raise EvidenceError("rollback rehearsal report cutover_plan_sha256 does not match the cutover plan")
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    rollback_check = checks.get("rollback_commands_tested") if isinstance(checks.get("rollback_commands_tested"), dict) else {}
    preserved_check = checks.get("old_resources_preserved") if isinstance(checks.get("old_resources_preserved"), dict) else {}
    if rollback_check.get("status") != "ok":
        raise EvidenceError("rollback rehearsal report is missing rollback_commands_tested=ok")
    if preserved_check.get("status") != "ok":
        raise EvidenceError("rollback rehearsal report is missing old_resources_preserved=ok")
    if rollback_check.get("rollback_actions_sha256") != expected_rollback_actions.get("sha256"):
        raise EvidenceError("rollback rehearsal report rollback_actions_sha256 does not match the cutover plan")
    if int(rollback_check.get("command_count") or 0) != len(expected_rollback_actions.get("commands") or []):
        raise EvidenceError("rollback rehearsal report command_count does not match the cutover plan")
    if int(rollback_check.get("operator_action_count") or 0) != len(expected_rollback_actions.get("operator_actions") or []):
        raise EvidenceError("rollback rehearsal report operator_action_count does not match the cutover plan")
    rollback = payload.get("rollback") if isinstance(payload.get("rollback"), dict) else {}
    if rollback.get("actions_sha256") != expected_rollback_actions.get("sha256"):
        raise EvidenceError("rollback rehearsal report action digest does not match the cutover plan")
    if rollback.get("commands") != expected_rollback_actions.get("commands"):
        raise EvidenceError("rollback rehearsal report commands do not match the cutover plan")
    if rollback.get("operator_actions") != expected_rollback_actions.get("operator_actions"):
        raise EvidenceError("rollback rehearsal report operator actions do not match the cutover plan")
    if int(preserved_check.get("resource_count") or 0) != expected_resource_count:
        raise EvidenceError("rollback rehearsal report resource_count does not match the cutover plan")
    if preserved_check.get("resources_sha256") != expected_resources_sha256:
        raise EvidenceError("rollback rehearsal report resources_sha256 does not match the cutover plan")
    reported_resources_raw = preserved_check.get("resources") if isinstance(preserved_check.get("resources"), dict) else {}
    try:
        reported_resources = {
            key: rollback_rehearsal.coerce_text_list(reported_resources_raw.get(key), f"old_resources_preserved.resources.{key}")
            for key in rollback_rehearsal.PRESERVED_RESOURCE_KEYS
        }
    except rollback_rehearsal.RollbackRehearsalError as exc:
        raise EvidenceError(str(exc)) from exc
    if reported_resources != expected_resources:
        raise EvidenceError("rollback rehearsal report resources do not match the cutover plan")
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

    old_stack = verify_old_stack_backup(old_stack_backup_path.resolve())
    checks["old_stack_backup_verified"] = record_check(
        backup=str(old_stack_backup_path.resolve()),
        files_verified=old_stack.get("files_verified"),
        archive_sha256=old_stack.get("archive_sha256"),
        contains_sensitive_data=old_stack.get("contains_sensitive_data"),
    )
    samples.append({"label": "old-stack-backup", "backup": str(old_stack_backup_path.resolve()), "files_verified": old_stack.get("files_verified")})

    open_webui = verify_open_webui_plan(open_webui_plan, inventory, old_stack_backup_path)
    checks["open_webui_migration_plan_reviewed"] = record_check(**open_webui)

    cutover = verify_cutover_plan(cutover_plan, inventory, old_stack_backup_path, open_webui_plan)
    rollback_actions = cutover.pop("_rollback_actions")
    checks["cutover_plan_reviewed"] = record_check(**cutover)

    rollback = verify_rollback_rehearsal(
        rollback_report,
        cutover_plan,
        expected_resources=cutover["resources"],
        expected_resource_count=cutover["resource_count"],
        expected_resources_sha256=cutover["resources_sha256"],
        expected_rollback_actions=rollback_actions,
    )
    rollback_checks = rollback.get("checks") if isinstance(rollback.get("checks"), dict) else {}
    rollback_commands = rollback_checks.get("rollback_commands_tested") if isinstance(rollback_checks.get("rollback_commands_tested"), dict) else {}
    rollback_preserved = rollback_checks.get("old_resources_preserved") if isinstance(rollback_checks.get("old_resources_preserved"), dict) else {}
    checks["rollback_rehearsed"] = record_check(
        report=str(rollback_report.resolve()),
        rehearsed_by=rollback.get("rehearsed_by"),
        generated_at=rollback.get("generated_at"),
        cutover_plan_sha256=rollback.get("cutover_plan_sha256"),
        command_count=rollback_commands.get("command_count"),
        operator_action_count=rollback_commands.get("operator_action_count"),
        rollback_actions_sha256=rollback_commands.get("rollback_actions_sha256"),
    )
    checks["old_resources_preserved"] = record_check(
        report=str(rollback_report.resolve()),
        resource_count=cutover["resource_count"],
        rehearsal_resource_count=rollback_preserved.get("resource_count"),
        resource_counts_by_type=cutover["resource_counts_by_type"],
        resources_sha256=cutover["resources_sha256"],
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


def _latest(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    return max(paths, key=lambda path: (path.stat().st_mtime_ns, path.name)).resolve()


def _selected(available: bool, *, path: Path | None = None, reason: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"available": available}
    if path is not None:
        payload["path"] = str(path.resolve())
        payload["name"] = path.name
    if reason:
        payload["reason"] = reason
    if extra:
        payload.update(extra)
    return payload


def _find_latest_dir_with_manifest(root: Path, expected_format: str, label: str, *, exclude_names: set[str] | None = None) -> tuple[Path | None, str | None]:
    if not root.is_dir():
        return None, f"{label} root does not exist"
    invalid_reason: str | None = None
    candidates: list[Path] = []
    for path in root.iterdir():
        if exclude_names and path.name in exclude_names:
            continue
        if not path.is_dir():
            continue
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_json_file(manifest_path)
        except EvidenceError as exc:
            invalid_reason = str(exc)
            continue
        if manifest.get("format") == expected_format:
            candidates.append(path)
        else:
            invalid_reason = f"latest candidates include unsupported {label} format"
    latest = _latest(candidates)
    if latest is None:
        return None, invalid_reason or f"no valid {label} directory found"
    return latest, None


def _find_latest_file(root: Path, patterns: tuple[str, ...], expected_format: str, label: str, *, exclude_dirs: set[str] | None = None) -> tuple[Path | None, str | None]:
    if not root.is_dir():
        return None, f"{label} root does not exist"
    invalid_reason: str | None = None
    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path in seen or not path.is_file():
                continue
            if exclude_dirs and any(parent.name in exclude_dirs for parent in path.parents if parent != root):
                continue
            seen.add(path)
            try:
                payload = load_json_file(path)
            except EvidenceError as exc:
                invalid_reason = str(exc)
                continue
            if payload.get("format") == expected_format:
                candidates.append(path)
            else:
                invalid_reason = f"latest candidates include unsupported {label} format"
    latest = _latest(candidates)
    if latest is None:
        return None, invalid_reason or f"no valid {label} file found"
    return latest, None


def select_inputs(backup_root: Path, restore_root: Path) -> dict[str, dict[str, Any]]:
    backup_root = backup_root.resolve()
    restore_root = restore_root.resolve()
    output = backup_root / ACCEPTANCE_DIR_NAME / EVIDENCE_FILE_NAME

    b1_backup, b1_reason = _find_latest_dir_with_manifest(
        backup_root,
        backup_restore.BACKUP_FORMAT,
        "B1 backup",
        exclude_names={ACCEPTANCE_DIR_NAME},
    )
    restore_report: Path | None = None
    restore_reason: str | None = None
    if b1_backup:
        restore_candidate = restore_root / b1_backup.name / "restore-report.json"
        if restore_candidate.is_file():
            restore_report = restore_candidate.resolve()
        else:
            restore_reason = f"restore report not found for selected B1 backup: {restore_candidate}"
    else:
        restore_reason = "select a valid B1 backup before matching a restore report"

    inventory, inventory_reason = _find_latest_file(
        backup_root,
        ("inventory*.json",),
        INVENTORY_FORMAT,
        "old-stack inventory",
        exclude_dirs={ACCEPTANCE_DIR_NAME},
    )
    old_stack_backup_path, old_stack_reason = _find_latest_dir_with_manifest(
        backup_root,
        OLD_STACK_BACKUP_FORMAT,
        "old-stack backup",
        exclude_names={ACCEPTANCE_DIR_NAME},
    )
    open_webui_plan, open_webui_reason = _find_latest_file(
        backup_root,
        ("open-webui-migration-plan*.json",),
        OPEN_WEBUI_PLAN_FORMAT,
        "Open WebUI migration plan",
        exclude_dirs={ACCEPTANCE_DIR_NAME},
    )
    try:
        cutover_plan = rollback_rehearsal.resolve_cutover_plan(backup_root)
        cutover_reason = None
    except rollback_rehearsal.RollbackRehearsalError as exc:
        cutover_plan = None
        cutover_reason = str(exc)
    rollback_report = (backup_root / rollback_rehearsal.ROLLBACK_REPORT_NAME).resolve()
    rollback_reason = None if rollback_report.is_file() else "rollback rehearsal report not found"

    return {
        "b1_backup": _selected(b1_backup is not None, path=b1_backup, reason=b1_reason),
        "restore_report": _selected(restore_report is not None, path=restore_report, reason=restore_reason),
        "inventory": _selected(inventory is not None, path=inventory, reason=inventory_reason),
        "old_stack_backup": _selected(old_stack_backup_path is not None, path=old_stack_backup_path, reason=old_stack_reason),
        "open_webui_plan": _selected(open_webui_plan is not None, path=open_webui_plan, reason=open_webui_reason),
        "cutover_plan": _selected(cutover_plan is not None, path=cutover_plan, reason=cutover_reason),
        "rollback_report": _selected(rollback_report.is_file(), path=rollback_report, reason=rollback_reason),
        "output": _selected(output.is_file(), path=output, reason=None if output.is_file() else "acceptance evidence has not been generated"),
    }


def _require_selected(inputs: dict[str, dict[str, Any]], key: str) -> Path:
    item = inputs[key]
    if not item.get("available") or not item.get("path"):
        reason = item.get("reason") or f"{key} is unavailable"
        raise EvidenceError(str(reason))
    return Path(str(item["path"])).resolve()


def summarize_evidence(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"available": False, "path": str(path.resolve()), "reason": "backup/migration/rollback evidence not found"}
    payload = load_json_file(path)
    if payload.get("format") != EVIDENCE_FORMAT:
        return {"available": False, "path": str(path.resolve()), "reason": "unsupported backup/migration/rollback evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    missing = [name for name in REQUIRED_CHECKS if (checks.get(name) or {}).get("status") != "ok"]
    return {
        "available": True,
        "path": str(path.resolve()),
        "status": payload.get("status"),
        "generated_at": payload.get("generated_at"),
        "missing_checks": missing,
        "check_count": len([name for name in REQUIRED_CHECKS if (checks.get(name) or {}).get("status") == "ok"]),
        "required_check_count": len(REQUIRED_CHECKS),
    }


def _input_blockers(inputs: dict[str, dict[str, Any]]) -> list[str]:
    blockers: list[str] = []
    for key in ("b1_backup", "restore_report", "inventory", "old_stack_backup", "open_webui_plan", "cutover_plan", "rollback_report"):
        item = inputs[key]
        if not item.get("available"):
            blockers.append(f"{key}: {item.get('reason') or 'unavailable'}")
    return blockers


def status(backup_root: Path, restore_root: Path) -> dict[str, Any]:
    backup_root = backup_root.resolve()
    restore_root = restore_root.resolve()
    inputs = select_inputs(backup_root, restore_root)
    evidence_path = backup_root / ACCEPTANCE_DIR_NAME / EVIDENCE_FILE_NAME
    try:
        evidence = summarize_evidence(evidence_path)
    except EvidenceError as exc:
        evidence = {"available": False, "path": str(evidence_path.resolve()), "reason": str(exc)}
    blockers = _input_blockers(inputs)
    return {
        "format": "b1-ai-hub-backup-migration-rollback-evidence-status/v1",
        "backup_root": str(backup_root),
        "restore_root": str(restore_root),
        "inputs": inputs,
        "evidence": evidence,
        "ready": not blockers,
        "blockers": blockers,
    }


def build_and_write_evidence(
    *,
    backup_root: Path,
    restore_root: Path,
    backup_encryption_key: str | None = None,
    output_writer: Callable[[Path, dict[str, Any]], Path] = write_evidence,
) -> dict[str, Any]:
    backup_root = backup_root.resolve()
    restore_root = restore_root.resolve()
    inputs = select_inputs(backup_root, restore_root)
    payload = build_evidence(
        b1_backup=_require_selected(inputs, "b1_backup"),
        restore_report=_require_selected(inputs, "restore_report"),
        inventory=_require_selected(inputs, "inventory"),
        old_stack_backup_path=_require_selected(inputs, "old_stack_backup"),
        open_webui_plan=_require_selected(inputs, "open_webui_plan"),
        cutover_plan=_require_selected(inputs, "cutover_plan"),
        rollback_report=_require_selected(inputs, "rollback_report"),
        backup_encryption_key=backup_encryption_key,
    )
    output = output_writer(backup_root / ACCEPTANCE_DIR_NAME / EVIDENCE_FILE_NAME, payload)
    inputs["output"] = _selected(True, path=output)
    return {"status": payload["status"], "output": str(output), "evidence": payload, "inputs": inputs}
