from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import backup_migration_rollback
from .private_files import PrivateFileError, write_private_json


PLAN_FORMAT = "b1-ai-hub-open-webui-migration-plan/v1"
OPEN_WEBUI_DB_NAMES = {"webui.db", "database.sqlite", "db.sqlite"}
OPEN_WEBUI_HINTS = ("open-webui", "open_webui")
FLOATING_IMAGE_TAGS = {"latest", "main", "master", "dev", "nightly", "edge", "stable"}
OPEN_WEBUI_DATA_DOMAIN_TABLES: dict[str, tuple[str, ...]] = {
    "accounts": ("user", "auth", "api_key"),
    "chats": ("chat", "message", "message_reaction", "tag", "folder", "chatidtag", "pinned_chat"),
    "settings": ("config", "setting", "settings", "user_setting", "user_settings"),
    "documents_rag": (
        "document",
        "file",
        "files",
        "knowledge",
        "knowledge_file",
        "collection",
        "embedding",
        "embeddings",
        "vector",
        "memory",
    ),
    "models_prompts_tools": ("model", "prompt", "tool", "function"),
    "feedback": ("feedback", "rating"),
}
INVENTORY_FORMAT = backup_migration_rollback.INVENTORY_FORMAT
OLD_STACK_BACKUP_FORMAT = backup_migration_rollback.OLD_STACK_BACKUP_FORMAT
PLAN_PATTERN = "open-webui-migration-plan*.json"
INVENTORY_PATTERN = "inventory*.json"
RESTORE_SUBDIR = "open-webui-migration"


class OpenWebUiMigrationError(RuntimeError):
    pass


def utc_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OpenWebUiMigrationError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OpenWebUiMigrationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OpenWebUiMigrationError(f"{path} must contain a JSON object")
    return payload


def load_inventory(path: Path) -> dict[str, Any]:
    inventory = load_json_file(path)
    if inventory.get("format") != INVENTORY_FORMAT:
        raise OpenWebUiMigrationError("unsupported inventory format")
    return inventory


def load_verified_backup_manifest(backup_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        verification = backup_migration_rollback.verify_old_stack_backup(backup_dir)
    except backup_migration_rollback.EvidenceError as exc:
        raise OpenWebUiMigrationError(str(exc)) from exc
    manifest = load_json_file(backup_dir / "manifest.json")
    if manifest.get("format") != OLD_STACK_BACKUP_FORMAT:
        raise OpenWebUiMigrationError("unsupported old-stack backup format")
    return verification, manifest


def normalize_path(value: str) -> str:
    return os.path.normpath(value)


def is_same_or_child(path: str, parent: str) -> bool:
    normalized_path = normalize_path(path)
    normalized_parent = normalize_path(parent)
    return normalized_path == normalized_parent or normalized_path.startswith(normalized_parent.rstrip(os.sep) + os.sep)


def inventory_paths(inventory: dict[str, Any], key: str) -> list[dict[str, Any]]:
    paths = inventory.get("paths") if isinstance(inventory.get("paths"), dict) else {}
    items = paths.get(key) if isinstance(paths.get(key), list) else []
    return [dict(item) for item in items if isinstance(item, dict)]


def open_webui_data_domain_summary(tables: list[str], table_counts: dict[str, int]) -> dict[str, Any]:
    table_set = set(tables)
    summary: dict[str, Any] = {}
    for domain, candidates in OPEN_WEBUI_DATA_DOMAIN_TABLES.items():
        present = sorted(table for table in candidates if table in table_set)
        row_counts = {
            table: int(table_counts[table])
            for table in present
            if isinstance(table_counts.get(table), int)
        }
        summary[domain] = {
            "tables": present,
            "table_count": len(present),
            "row_counts": row_counts,
            "known_row_count": sum(row_counts.values()),
            "content_rows_read": False,
        }
    return summary


def aggregate_open_webui_data_domains(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, dict[str, Any]] = {
        domain: {"database_count": 0, "tables": [], "known_row_count": 0}
        for domain in OPEN_WEBUI_DATA_DOMAIN_TABLES
    }
    for candidate in candidates:
        domains = candidate.get("data_domains") if isinstance(candidate.get("data_domains"), dict) else {}
        for domain, domain_data in domains.items():
            if not isinstance(domain_data, dict):
                continue
            tables = [str(table) for table in domain_data.get("tables", []) if isinstance(table, str)]
            if not tables:
                continue
            total = totals.setdefault(domain, {"database_count": 0, "tables": [], "known_row_count": 0})
            total["database_count"] = int(total.get("database_count") or 0) + 1
            total["tables"] = sorted(set([*total.get("tables", []), *tables]))
            total["known_row_count"] = int(total.get("known_row_count") or 0) + int(domain_data.get("known_row_count") or 0)
    return totals


def open_webui_data_domain_coverage_gaps(readable: list[dict[str, Any]], backed_readable: list[dict[str, Any]]) -> list[str]:
    all_domains = aggregate_open_webui_data_domains(readable)
    backed_domains = aggregate_open_webui_data_domains(backed_readable)
    gaps: list[str] = []
    for domain in OPEN_WEBUI_DATA_DOMAIN_TABLES:
        all_domain = all_domains.get(domain) or {}
        backed_domain = backed_domains.get(domain) or {}
        if int(all_domain.get("database_count") or 0) <= 0:
            continue
        if (
            int(backed_domain.get("database_count") or 0) < int(all_domain.get("database_count") or 0)
            or int(backed_domain.get("known_row_count") or 0) < int(all_domain.get("known_row_count") or 0)
        ):
            gaps.append(domain)
    return gaps


def database_candidates(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in inventory_paths(inventory, "open_webui_database_candidates"):
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        sqlite = item.get("sqlite") if isinstance(item.get("sqlite"), dict) else {}
        table_counts = sqlite.get("table_counts", {}) if isinstance(sqlite.get("table_counts"), dict) else {}
        tables = [str(table) for table in sqlite.get("tables", []) if isinstance(table, str)]
        data_domains = sqlite.get("data_domains") if isinstance(sqlite.get("data_domains"), dict) else None
        if data_domains is None:
            data_domains = open_webui_data_domain_summary(tables, table_counts)
        candidates.append(
            {
                "path": path,
                "exists": bool(item.get("exists")),
                "type": item.get("type"),
                "readable_sqlite": bool(sqlite.get("readable")),
                "table_counts": table_counts,
                "tables": tables,
                "data_domains": data_domains,
                "content_rows_read": False,
            }
        )
    return candidates


def data_path_candidates(inventory: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in inventory_paths(inventory, "open_webui_data_candidates") + inventory_paths(inventory, "open_webui_data_roots"):
        path = item.get("path")
        if isinstance(path, str) and item.get("exists") and path not in paths:
            paths.append(path)
    return sorted(paths)


def unreadable_data_roots(inventory: dict[str, Any]) -> list[dict[str, str]]:
    roots: list[dict[str, str]] = []
    readiness = inventory.get("migration_readiness") if isinstance(inventory.get("migration_readiness"), dict) else {}
    root_readiness = readiness.get("open_webui_data_roots") if isinstance(readiness.get("open_webui_data_roots"), dict) else {}
    for item in root_readiness.get("unreadable_or_unscannable") or []:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path:
            roots.append({"path": path, "reason": str(item.get("reason") or "not readable by current user")})
    seen = {item["path"] for item in roots}
    for item in inventory_paths(inventory, "open_webui_data_roots"):
        path = item.get("path")
        if isinstance(path, str) and item.get("exists") is None and path not in seen:
            roots.append({"path": path, "reason": str(item.get("error") or "not readable by current user")})
            seen.add(path)
    return sorted(roots, key=lambda item: item["path"])


def looks_like_open_webui(*values: Any) -> bool:
    text = " ".join(str(value).lower() for value in values if value is not None)
    return any(hint in text for hint in OPEN_WEBUI_HINTS)


def image_reference_metadata(image: str) -> dict[str, Any]:
    without_digest, separator, digest = image.partition("@")
    last_slash = without_digest.rfind("/")
    last_colon = without_digest.rfind(":")
    tag: str | None = None
    repository = without_digest
    if last_colon > last_slash:
        tag = without_digest[last_colon + 1 :]
        repository = without_digest[:last_colon]
    digest_present = bool(separator and digest)
    tag_is_floating = tag is not None and tag.lower() in FLOATING_IMAGE_TAGS
    missing_tag = tag is None
    floating = not digest_present and (missing_tag or tag_is_floating)
    return {
        "image": image,
        "repository": repository,
        "tag": tag,
        "digest_present": digest_present,
        "digest": digest or None,
        "tag_is_floating": tag_is_floating,
        "missing_tag": missing_tag,
        "floating_or_missing_tag": floating,
        "version_hint": None if floating else tag or digest,
    }


def open_webui_container_version_evidence(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    classification = inventory.get("classification") if isinstance(inventory.get("classification"), dict) else {}
    docker = inventory.get("docker") if isinstance(inventory.get("docker"), dict) else {}
    sources = (
        ("inventory.classification.containers", classification.get("containers")),
        ("inventory.docker.containers", docker.get("containers")),
    )
    for source, rows in sources:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            container = row.get("container") or row.get("Names") or row.get("Name") or row.get("ID")
            image = row.get("image") or row.get("Image")
            if not isinstance(image, str) or not image or not looks_like_open_webui(container, image, row.get("Labels")):
                continue
            container_name = str(container or "")
            key = (container_name, image)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                {
                    "source": source,
                    "container": container_name,
                    **image_reference_metadata(image),
                }
            )
    return sorted(evidence, key=lambda item: (item["container"], item["image"]))


def backed_up_open_webui_container_metadata(manifest: dict[str, Any], version_evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = {item["container"].lstrip("/") for item in version_evidence if isinstance(item.get("container"), str) and item.get("container")}
    backed: list[dict[str, Any]] = []
    for source in manifest.get("sources", []):
        if not isinstance(source, dict) or source.get("type") != "docker_container_metadata":
            continue
        name = source.get("name")
        if not isinstance(name, str):
            continue
        if names and name.lstrip("/") not in names:
            continue
        if names or looks_like_open_webui(name, source.get("archive_path")):
            backed.append({"container": name, "archive_path": source.get("archive_path"), "reason": source.get("reason")})
    return sorted(backed, key=lambda item: str(item.get("container") or ""))


def version_compatibility_status(version_evidence: list[dict[str, Any]]) -> str:
    if not version_evidence:
        return "source-version-evidence-missing"
    if any(item.get("floating_or_missing_tag") for item in version_evidence):
        return "manual-source-version-review-required"
    return "source-version-recorded-temporary-validation-required"


def source_covers_candidate(candidate_path: str, source: dict[str, Any]) -> bool:
    source_type = source.get("type")
    if source_type == "host_path":
        source_path = source.get("source_path")
        return isinstance(source_path, str) and is_same_or_child(candidate_path, source_path)
    if source_type == "docker_volume":
        mountpoint = source.get("mountpoint")
        return isinstance(mountpoint, str) and is_same_or_child(candidate_path, mountpoint)
    return False


def file_covers_candidate(candidate_path: str, file_record: dict[str, Any]) -> bool:
    source_path = file_record.get("source_path")
    return isinstance(source_path, str) and normalize_path(source_path) == normalize_path(candidate_path)


def backup_database_artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for file_record in manifest.get("files", []):
        if not isinstance(file_record, dict):
            continue
        archive_path = str(file_record.get("archive_path") or "")
        source_path = str(file_record.get("source_path") or "")
        archive_name = Path(archive_path).name.lower()
        source_name = Path(source_path).name.lower()
        if archive_name in OPEN_WEBUI_DB_NAMES or source_name in OPEN_WEBUI_DB_NAMES:
            artifacts.append(
                {
                    "archive_path": archive_path,
                    "source_path": source_path,
                    "source_type": file_record.get("source_type"),
                    "size_bytes": file_record.get("size_bytes"),
                    "sha256": file_record.get("sha256"),
                    "sensitive": bool(file_record.get("sensitive")),
                }
            )
    return sorted(artifacts, key=lambda item: item["archive_path"])


def annotate_database_coverage(candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [item for item in manifest.get("sources", []) if isinstance(item, dict)]
    files = [item for item in manifest.get("files", []) if isinstance(item, dict)]
    annotated: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_path = candidate["path"]
        covering_sources = [source for source in sources if source_covers_candidate(candidate_path, source)]
        covering_files = [file_record for file_record in files if file_covers_candidate(candidate_path, file_record)]
        covered = bool(covering_sources or covering_files)
        annotated.append(
            {
                **candidate,
                "backup_coverage": "covered" if covered else "not_covered",
                "covering_sources": covering_sources,
                "covering_archive_paths": [item["archive_path"] for item in covering_files if isinstance(item.get("archive_path"), str)],
            }
        )
    return annotated


def choose_strategy(databases: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> str:
    if any(item["readable_sqlite"] and item["backup_coverage"] == "covered" for item in databases):
        return "preserve-backed-up-sqlite-and-test-supported-open-webui-import"
    if artifacts:
        return "extract-backed-up-database-and-plan-manual-open-webui-export-import"
    if any(item["readable_sqlite"] for item in databases):
        return "back-up-open-webui-before-cutover"
    return "no-readable-open-webui-database-found-preserve-old-stack"


def build_plan(
    *,
    inventory_path: Path,
    backup_dir: Path,
    restore_target: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    inventory = load_inventory(inventory_path)
    backup_verification, manifest = load_verified_backup_manifest(backup_dir)
    created_at = (now or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
    databases = annotate_database_coverage(database_candidates(inventory), manifest)
    artifacts = backup_database_artifacts(manifest)
    version_evidence = open_webui_container_version_evidence(inventory)
    backed_container_metadata = backed_up_open_webui_container_metadata(manifest, version_evidence)
    unreadable_roots = unreadable_data_roots(inventory)
    readable = [item for item in databases if item["readable_sqlite"]]
    backed = [item for item in databases if item["backup_coverage"] == "covered"]
    readable_not_backed = [item for item in readable if item["backup_coverage"] != "covered"]
    backed_readable = [item for item in readable if item["backup_coverage"] == "covered"]
    domain_coverage_gaps = open_webui_data_domain_coverage_gaps(readable, backed_readable)
    warnings: list[str] = []
    if not readable:
        warnings.append("inventory did not contain a readable Open WebUI SQLite database; preserve the old stack for manual export")
    if unreadable_roots:
        warnings.append(
            "Open WebUI data roots were discovered but could not be scanned by the inventory user; include the Docker volume in the reviewed old-stack backup or rerun inventory with read access: "
            + ", ".join(item["path"] for item in unreadable_roots)
        )
    if readable_not_backed:
        warnings.append(
            "readable Open WebUI database candidates are not covered by the verified old-stack backup: "
            + ", ".join(item["path"] for item in readable_not_backed)
        )
    if domain_coverage_gaps:
        warnings.append(
            "Open WebUI data domains found in readable databases are not fully covered by the verified old-stack backup: "
            + ", ".join(domain_coverage_gaps)
        )
    if artifacts and not backed:
        warnings.append("backup contains Open WebUI-like database artifacts that were not matched to inventory candidates; review source paths before import")
    if not version_evidence:
        warnings.append("inventory did not contain Open WebUI container image/version evidence; verify source and target Open WebUI versions manually")
    if any(item.get("floating_or_missing_tag") for item in version_evidence):
        warnings.append(
            "Open WebUI container image evidence uses a floating or missing tag; record the exact old image digest or version before testing migration"
        )

    return {
        "format": PLAN_FORMAT,
        "created_at": created_at,
        "inputs": {
            "inventory": str(inventory_path.resolve()),
            "old_stack_backup": str(backup_dir.resolve()),
            "old_stack_backup_verification": backup_verification,
        },
        "safety": {
            "read_only_plan": True,
            "does_not_modify_old_stack": True,
            "does_not_import_automatically": True,
            "does_not_read_database_rows": True,
            "old_stack_deletion_allowed": False,
            "operator_must_review_before_execution": True,
        },
        "open_webui": {
            "data_path_candidates": data_path_candidates(inventory),
            "unreadable_data_roots": unreadable_roots,
            "database_candidates": databases,
            "backup_database_artifacts": artifacts,
            "readable_database_count": len(readable),
            "backed_up_database_candidate_count": len(backed),
            "data_domains": {
                "all_readable": aggregate_open_webui_data_domains(readable),
                "backed_up_readable": aggregate_open_webui_data_domains(backed_readable),
                "content_rows_read": False,
            },
            "recommended_strategy": choose_strategy(databases, artifacts),
            "version_evidence": {
                "source_containers": version_evidence,
                "backed_up_container_metadata": backed_container_metadata,
                "compatibility_status": version_compatibility_status(version_evidence),
                "direct_database_reuse_approved_by_plan": False,
                "requires_supported_open_webui_migration_path": True,
                "requires_temporary_instance_validation": True,
            },
        },
        "commands": {
            "verify_old_stack_backup": {
                "argv": ["python3", "deploy/scripts/old_stack_backup.py", "verify", "--backup", str(backup_dir)],
            },
            "restore_backup_to_alternate_directory": {
                "argv": ["python3", "deploy/scripts/restore.py", "--backup", str(backup_dir), "--target", restore_target],
            },
        },
        "operator_actions": [
            "Restore the verified old-stack backup to the alternate target before testing any Open WebUI migration.",
            "Inspect Open WebUI version compatibility and use supported Open WebUI migration/export/import paths when available.",
            "Do not point production Open WebUI at an old database until a temporary B1 instance has started and validated accounts, chats, files, settings, and rollback.",
            "If direct database reuse is unsafe, keep the old database recoverable and use Open WebUI UI/API export-import guidance instead.",
        ],
        "warnings": warnings,
    }


def write_plan(plan: dict[str, Any], output: Path) -> Path:
    try:
        return write_private_json(output, plan, mode=0o600, label="Open WebUI migration plan")
    except PrivateFileError as exc:
        raise OpenWebUiMigrationError(str(exc)) from exc


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


def _find_latest_file(root: Path, pattern: str, expected_format: str, label: str) -> tuple[Path | None, str | None]:
    if not root.is_dir():
        return None, f"{label} root does not exist"
    invalid_reason: str | None = None
    candidates: list[Path] = []
    for path in root.glob(pattern):
        if not path.is_file() or path.parent.resolve() != root.resolve():
            continue
        try:
            payload = load_json_file(path)
        except OpenWebUiMigrationError as exc:
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


def _find_latest_dir_with_manifest(root: Path, expected_format: str, label: str) -> tuple[Path | None, str | None]:
    if not root.is_dir():
        return None, f"{label} root does not exist"
    invalid_reason: str | None = None
    candidates: list[Path] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name == backup_migration_rollback.ACCEPTANCE_DIR_NAME:
            continue
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_json_file(manifest_path)
        except OpenWebUiMigrationError as exc:
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


def select_inputs(backup_root: Path, restore_root: Path) -> dict[str, dict[str, Any]]:
    root = backup_root.resolve()
    restore_target = (restore_root.resolve() / RESTORE_SUBDIR)
    inventory, inventory_reason = _find_latest_file(root, INVENTORY_PATTERN, INVENTORY_FORMAT, "old-stack inventory")
    old_stack_backup, backup_reason = _find_latest_dir_with_manifest(root, OLD_STACK_BACKUP_FORMAT, "old-stack backup")
    latest_plan, latest_plan_reason = _find_latest_file(root, PLAN_PATTERN, PLAN_FORMAT, "Open WebUI migration plan")
    return {
        "inventory": _selected(inventory is not None, path=inventory, reason=inventory_reason),
        "old_stack_backup": _selected(old_stack_backup is not None, path=old_stack_backup, reason=backup_reason),
        "restore_target": _selected(True, path=restore_target),
        "current_plan": _selected(latest_plan is not None, path=latest_plan, reason=latest_plan_reason, extra=summarize_plan(latest_plan) if latest_plan else None),
    }


def _require_selected(inputs: dict[str, dict[str, Any]], key: str) -> Path:
    item = inputs[key]
    if not item.get("available") or not item.get("path"):
        reason = item.get("reason") or f"{key} is unavailable"
        raise OpenWebUiMigrationError(str(reason))
    return Path(str(item["path"])).resolve()


def summarize_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing"}
    payload = load_json_file(path)
    if payload.get("format") != PLAN_FORMAT:
        return {"status": "unsupported"}
    open_webui = payload.get("open_webui") if isinstance(payload.get("open_webui"), dict) else {}
    version = open_webui.get("version_evidence") if isinstance(open_webui.get("version_evidence"), dict) else {}
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    return {
        "status": "ready" if not warnings else "review_required",
        "created_at": payload.get("created_at"),
        "recommended_strategy": open_webui.get("recommended_strategy"),
        "warning_count": len(warnings),
        "warnings": warnings,
        "readable_database_count": open_webui.get("readable_database_count"),
        "backed_up_database_candidate_count": open_webui.get("backed_up_database_candidate_count"),
        "data_domains": open_webui.get("data_domains") if isinstance(open_webui.get("data_domains"), dict) else {},
        "compatibility_status": version.get("compatibility_status"),
    }


def status(backup_root: Path, restore_root: Path) -> dict[str, Any]:
    inputs = select_inputs(backup_root, restore_root)
    return {
        "format": "b1-ai-hub-open-webui-migration-plan-status/v1",
        "backup_root": str(backup_root.resolve()),
        "restore_root": str(restore_root.resolve()),
        "inputs": inputs,
        "ready_to_generate": bool(inputs["inventory"].get("available") and inputs["old_stack_backup"].get("available")),
    }


def build_and_write_plan(backup_root: Path, restore_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    inputs = select_inputs(backup_root, restore_root)
    inventory_path = _require_selected(inputs, "inventory")
    backup_dir = _require_selected(inputs, "old_stack_backup")
    restore_target = _require_selected(inputs, "restore_target")
    generated_at = now or datetime.now(tz=UTC)
    plan = build_plan(
        inventory_path=inventory_path,
        backup_dir=backup_dir,
        restore_target=str(restore_target),
        now=generated_at,
    )
    output = backup_root.resolve() / f"open-webui-migration-plan-{generated_at.strftime('%Y%m%d-%H%M%S')}.json"
    write_plan(plan, output)
    return {
        "status": "created",
        "path": str(output.resolve()),
        "name": output.name,
        "plan": plan,
        "summary": summarize_plan(output),
    }
