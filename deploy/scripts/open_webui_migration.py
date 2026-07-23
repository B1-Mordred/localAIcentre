#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import old_stack_backup


PLAN_FORMAT = "b1-ai-hub-open-webui-migration-plan/v1"
OPEN_WEBUI_DB_NAMES = {"webui.db", "database.sqlite", "db.sqlite"}
OPEN_WEBUI_HINTS = ("open-webui", "open_webui")
FLOATING_IMAGE_TAGS = {"latest", "main", "master", "dev", "nightly", "edge", "stable"}


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
    if inventory.get("format") != old_stack_backup.INVENTORY_FORMAT:
        raise OpenWebUiMigrationError("unsupported inventory format")
    return inventory


def load_verified_backup_manifest(backup_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    verification = old_stack_backup.verify_backup(backup_dir)
    manifest = load_json_file(backup_dir / "manifest.json")
    if manifest.get("format") != old_stack_backup.BACKUP_FORMAT:
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


def database_candidates(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in inventory_paths(inventory, "open_webui_database_candidates"):
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        sqlite = item.get("sqlite") if isinstance(item.get("sqlite"), dict) else {}
        candidates.append(
            {
                "path": path,
                "exists": bool(item.get("exists")),
                "type": item.get("type"),
                "readable_sqlite": bool(sqlite.get("readable")),
                "table_counts": sqlite.get("table_counts", {}),
                "tables": sqlite.get("tables", []),
                "content_rows_read": False,
            }
        )
    return candidates


def data_path_candidates(inventory: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in inventory_paths(inventory, "open_webui_data_candidates"):
        path = item.get("path")
        if isinstance(path, str) and item.get("exists") and path not in paths:
            paths.append(path)
    return sorted(paths)


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
    readable = [item for item in databases if item["readable_sqlite"]]
    backed = [item for item in databases if item["backup_coverage"] == "covered"]
    readable_not_backed = [item for item in readable if item["backup_coverage"] != "covered"]
    warnings: list[str] = []
    if not readable:
        warnings.append("inventory did not contain a readable Open WebUI SQLite database; preserve the old stack for manual export")
    if readable_not_backed:
        warnings.append(
            "readable Open WebUI database candidates are not covered by the verified old-stack backup: "
            + ", ".join(item["path"] for item in readable_not_backed)
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
            "database_candidates": databases,
            "backup_database_artifacts": artifacts,
            "readable_database_count": len(readable),
            "backed_up_database_candidate_count": len(backed),
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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a non-destructive Open WebUI preservation and migration plan.")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--output", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub") + f"/backups/open-webui-migration-plan-{utc_stamp()}.json")
    parser.add_argument("--restore-target", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub") + "/restore-tests/open-webui-migration")
    args = parser.parse_args()
    try:
        plan = build_plan(
            inventory_path=Path(args.inventory),
            backup_dir=Path(args.backup),
            restore_target=args.restore_target,
        )
        output = write_plan(plan, Path(args.output))
    except (OpenWebUiMigrationError, old_stack_backup.OldStackBackupError) as exc:
        raise SystemExit(f"Open WebUI migration plan failed: {exc}") from exc
    print(f"wrote Open WebUI migration plan: {output}")
    print(json.dumps({"format": plan["format"], "strategy": plan["open_webui"]["recommended_strategy"], "warnings": plan["warnings"]}, indent=2))


if __name__ == "__main__":
    main()
