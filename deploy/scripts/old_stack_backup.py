#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable


BACKUP_FORMAT = "b1-ai-hub-old-stack-backup/v1"
SCOPE_FORMAT = "b1-ai-hub-old-stack-backup-scope/v1"
INVENTORY_FORMAT = "b1-ai-hub-host-inventory/v1"
BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DOCKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
SENSITIVE_HINTS = (
    ".env",
    "apikey",
    "api-key",
    "api_key",
    "bearer",
    "credential",
    "key",
    "oauth",
    "password",
    "passwd",
    "secret",
    "token",
    "webui.db",
    "database.sqlite",
)
SENSITIVE_JSON_KEY_HINTS = (
    "apikey",
    "api-key",
    "api_key",
    "auth",
    "bearer",
    "credential",
    "key",
    "oauth",
    "password",
    "passwd",
    "secret",
    "token",
)
SECRET_TEXT_PATTERNS = (
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]+\b"),
    re.compile(r"\bb1(?:k|adm|rt)_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)(=|:)[^\s,;\"']+"),
)
FORBIDDEN_PATHS = (
    Path("/"),
    Path("/dev"),
    Path("/proc"),
    Path("/sys"),
    Path("/run"),
    Path("/var/run"),
)


class OldStackBackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveFile:
    archive_path: str
    source_path: str
    source_type: str
    size_bytes: int
    sha256: str
    sensitive: bool


CommandRunner = Callable[[list[str]], dict[str, Any]]


@dataclass(frozen=True)
class DockerInspectPayload:
    raw: bytes
    redacted: bytes


def utc_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")


def run_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    return {
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
    }


def validate_label(label: str) -> str:
    if not BACKUP_NAME_RE.fullmatch(label):
        raise OldStackBackupError("backup label must be 1-128 characters of letters, numbers, dot, dash, or underscore")
    return label


def validate_docker_name(name: str) -> str:
    if not DOCKER_NAME_RE.fullmatch(name):
        raise OldStackBackupError(f"unsafe Docker resource name: {name}")
    return name


def safe_archive_path(value: str) -> str:
    if "\x00" in value:
        raise OldStackBackupError("archive path contains NUL byte")
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or not relative.parts:
        raise OldStackBackupError(f"archive path escapes payload root: {value}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise OldStackBackupError(f"archive path escapes payload root: {value}")
    return relative.as_posix()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sensitive_path(path: str) -> bool:
    lowered = path.lower()
    return any(hint in lowered for hint in SENSITIVE_HINTS)


def source_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


def assert_safe_source_path(path: Path) -> Path:
    if not path.is_absolute():
        raise OldStackBackupError(f"include path must be absolute: {path}")
    if path.is_symlink():
        raise OldStackBackupError(f"refusing to back up symlink source path: {path}")
    resolved = path.resolve(strict=False)
    if resolved == Path("/") or any(resolved == forbidden or forbidden in resolved.parents for forbidden in FORBIDDEN_PATHS if forbidden != Path("/")):
        raise OldStackBackupError(f"refusing to back up unsafe top-level path: {resolved}")
    return resolved


def iter_regular_files(source: Path) -> list[Path]:
    if source.is_symlink():
        raise OldStackBackupError(f"refusing to back up symlink: {source}")
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise OldStackBackupError(f"backup source is not a regular file or directory: {source}")
    files: list[Path] = []
    for current in sorted(source.rglob("*")):
        if current.is_symlink():
            raise OldStackBackupError(f"refusing to back up symlink: {current}")
        if current.is_file():
            files.append(current)
        elif current.is_dir():
            continue
        else:
            raise OldStackBackupError(f"refusing to back up special file: {current}")
    return files


def add_bytes_member(
    archive: tarfile.TarFile,
    archive_path: str,
    content: bytes,
    mode: int = 0o600,
    *,
    sensitive: bool = False,
) -> ArchiveFile:
    archive_path = safe_archive_path(archive_path)
    info = tarfile.TarInfo(archive_path)
    info.size = len(content)
    info.mode = mode
    info.mtime = int(datetime.now(tz=UTC).timestamp())
    archive.addfile(info, io.BytesIO(content))
    return ArchiveFile(
        archive_path=archive_path,
        source_path=f"generated:{archive_path}",
        source_type="generated",
        size_bytes=len(content),
        sha256=sha256_bytes(content),
        sensitive=sensitive,
    )


def add_file_member(archive: tarfile.TarFile, source: Path, archive_path: str, source_type: str) -> ArchiveFile:
    if source.is_symlink() or not source.is_file():
        raise OldStackBackupError(f"refusing to archive non-regular file: {source}")
    archive_path = safe_archive_path(archive_path)
    size = source.stat().st_size
    digest = sha256_file(source)
    archive.add(source, arcname=archive_path, recursive=False)
    return ArchiveFile(
        archive_path=archive_path,
        source_path=str(source),
        source_type=source_type,
        size_bytes=size,
        sha256=digest,
        sensitive=is_sensitive_path(str(source)) or is_sensitive_path(archive_path),
    )


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OldStackBackupError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OldStackBackupError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OldStackBackupError(f"{path} must contain a JSON object")
    return data


def load_inventory(path: Path) -> dict[str, Any]:
    inventory = load_json_file(path)
    if inventory.get("format") != INVENTORY_FORMAT:
        raise OldStackBackupError("unsupported inventory format")
    return inventory


def redact_secret_text_match(match: re.Match[str]) -> str:
    if match.lastindex == 1:
        return match.group(1) + "<redacted>"
    if match.lastindex and match.lastindex >= 2:
        return f"{match.group(1)}{match.group(2)}<redacted>"
    return "<redacted>"


def redact_secret_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(redact_secret_text_match, redacted)
    return redacted


def is_sensitive_json_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_JSON_KEY_HINTS)


def redact_env_assignment(value: str) -> str:
    if "=" not in value:
        return "<redacted>"
    name, _raw = value.split("=", 1)
    return f"{name}=<redacted>"


def redact_docker_inspect_json(value: Any, *, parent_key: str = "") -> Any:
    if parent_key == "Env" and isinstance(value, list):
        return [redact_env_assignment(item) if isinstance(item, str) else "<redacted>" for item in value]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_json_key(str(key)):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_docker_inspect_json(item, parent_key=str(key))
        return redacted
    if isinstance(value, list):
        return [redact_docker_inspect_json(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def normalize_scope_items(raw: Any, key: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise OldStackBackupError(f"{key} must be a list")
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            normalized.append({"name" if key.startswith("include_docker") or key == "include_containers" else "path": item})
        elif isinstance(item, dict):
            normalized.append(item)
        else:
            raise OldStackBackupError(f"{key} entries must be strings or objects")
    return normalized


def load_scope(path: Path) -> dict[str, Any]:
    scope = load_json_file(path)
    if scope.get("format") != SCOPE_FORMAT:
        raise OldStackBackupError("unsupported old-stack backup scope format")
    if scope.get("operator_reviewed") is not True:
        raise OldStackBackupError("scope must set operator_reviewed=true before backup")
    reviewed_by = str(scope.get("reviewed_by") or "").strip()
    if not reviewed_by:
        raise OldStackBackupError("scope must include reviewed_by before backup")
    return {
        **scope,
        "include_paths": normalize_scope_items(scope.get("include_paths"), "include_paths"),
        "include_docker_volumes": normalize_scope_items(scope.get("include_docker_volumes"), "include_docker_volumes"),
        "include_containers": normalize_scope_items(scope.get("include_containers"), "include_containers"),
    }


def candidate_names(items: Any, name_key: str) -> list[str]:
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(name_key)
        if isinstance(value, str) and value not in names:
            names.append(value)
    return sorted(names)


def existing_candidate_paths(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    paths: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("path")
        if isinstance(value, str) and item.get("exists") and value not in paths:
            paths.append(value)
    return sorted(paths)


def unreadable_candidate_paths(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    paths: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("path")
        if isinstance(value, str) and item.get("exists") is None and value not in paths:
            paths.append(value)
    return sorted(paths)


def merge_candidate_paths(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for value in group:
            if value not in merged:
                merged.append(value)
    return sorted(merged)


def items_with_classification(items: Any, classification: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("classification") == classification]


def build_scope_template(inventory: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    classification = inventory.get("classification") if isinstance(inventory.get("classification"), dict) else {}
    paths = inventory.get("paths") if isinstance(inventory.get("paths"), dict) else {}
    readiness = inventory.get("migration_readiness") if isinstance(inventory.get("migration_readiness"), dict) else {}
    created_at = (now or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
    return {
        "format": SCOPE_FORMAT,
        "created_at": created_at,
        "inventory_created_at": inventory.get("created_at"),
        "operator_reviewed": False,
        "reviewed_by": "",
        "review_notes": "Fill include_* arrays only with resources confirmed to belong to the old AI stack. Preserve unrelated and unknown resources.",
        "include_paths": [],
        "include_docker_volumes": [],
        "include_containers": [],
        "candidates": {
            "old_ai_stack_container_names": candidate_names(classification.get("old_ai_stack_candidates", []), "container"),
            "old_ai_stack_compose_projects": candidate_names(
                items_with_classification(classification.get("compose_projects"), "candidate-old-ai-stack-review-required"),
                "project",
            ),
            "ai_hint_volume_names": candidate_names(classification.get("volumes_with_ai_hints", []), "Name"),
            "compose_file_paths": existing_candidate_paths(paths.get("compose_file_candidates", [])),
            "open_webui_data_paths": merge_candidate_paths(
                existing_candidate_paths(paths.get("open_webui_data_candidates", [])),
                existing_candidate_paths(paths.get("open_webui_data_roots", [])),
            ),
            "open_webui_unreadable_data_roots": unreadable_candidate_paths(paths.get("open_webui_data_roots", [])),
            "open_webui_database_paths": existing_candidate_paths(paths.get("open_webui_database_candidates", [])),
            "model_directory_paths": existing_candidate_paths(paths.get("model_directories", [])),
        },
        "inventory_review": {
            "port_review": readiness.get("port_review", {}),
            "model_storage": readiness.get("model_storage", {}),
            "open_webui": readiness.get("open_webui", {}),
            "open_webui_data_roots": readiness.get("open_webui_data_roots", {}),
        },
        "safety": {
            "nothing_is_selected_automatically": True,
            "unknown_preserve_by_default": True,
            "backup_is_read_only": True,
            "old_stack_deletion_allowed": False,
        },
    }


def docker_inspect_container(name: str, runner: CommandRunner = run_command) -> DockerInspectPayload:
    name = validate_docker_name(name)
    result = runner(["docker", "inspect", name])
    if result.get("returncode") != 0:
        raise OldStackBackupError(f"docker inspect failed for container {name}: {result.get('stderr', '').strip()}")
    payload = result.get("stdout", "")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise OldStackBackupError(f"docker inspect returned invalid JSON for {name}") from exc
    raw = (json.dumps(parsed, indent=2, sort_keys=True) + "\n").encode("utf-8")
    redacted = (json.dumps(redact_docker_inspect_json(parsed), indent=2, sort_keys=True) + "\n").encode("utf-8")
    return DockerInspectPayload(raw=raw, redacted=redacted)


def docker_volume_mountpoint(name: str, runner: CommandRunner = run_command) -> Path:
    name = validate_docker_name(name)
    result = runner(["docker", "volume", "inspect", name])
    if result.get("returncode") != 0:
        raise OldStackBackupError(f"docker volume inspect failed for {name}: {result.get('stderr', '').strip()}")
    try:
        parsed = json.loads(result.get("stdout", ""))
    except json.JSONDecodeError as exc:
        raise OldStackBackupError(f"docker volume inspect returned invalid JSON for {name}") from exc
    if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
        raise OldStackBackupError(f"docker volume inspect returned no data for {name}")
    mountpoint = parsed[0].get("Mountpoint")
    if not isinstance(mountpoint, str) or not mountpoint:
        raise OldStackBackupError(f"docker volume inspect returned no mountpoint for {name}")
    return assert_safe_source_path(Path(mountpoint))


def path_member_prefix(source: Path) -> str:
    return f"host-paths/{source_id(source)}"


def archive_path_for_source(source: Path, file_path: Path) -> str:
    prefix = path_member_prefix(source)
    if source.is_file():
        return f"{prefix}/{source.name}"
    relative = file_path.relative_to(source).as_posix()
    return f"{prefix}/{relative}"


def archive_path_for_volume(volume_name: str, mountpoint: Path, file_path: Path) -> str:
    relative = file_path.relative_to(mountpoint).as_posix()
    return f"docker-volumes/{volume_name}/{relative}"


def backup_old_stack(
    *,
    scope_path: Path,
    output_root: Path,
    label: str | None = None,
    command_runner: CommandRunner = run_command,
    now: datetime | None = None,
) -> Path:
    scope = load_scope(scope_path)
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    name = validate_label(label) if label else f"old-stack-{utc_stamp()}"
    backup_dir = output_root / name
    suffix = 1
    while backup_dir.exists():
        backup_dir = output_root / f"{name}-{suffix}"
        suffix += 1
    backup_dir.mkdir(mode=0o700)
    archive_path = backup_dir / "payload.tar.gz"
    created_at = (now or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
    records: list[ArchiveFile] = []
    source_index: list[dict[str, Any]] = []

    with tarfile.open(archive_path, "w:gz") as archive:
        scope_content = json.dumps(scope, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        records.append(add_bytes_member(archive, "scope.json", scope_content))

        for item in scope["include_paths"]:
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise OldStackBackupError("include_paths entries require path")
            source = assert_safe_source_path(Path(raw_path))
            if not source.exists():
                raise OldStackBackupError(f"include path does not exist: {source}")
            files = iter_regular_files(source)
            source_index.append(
                {
                    "type": "host_path",
                    "source_path": str(source),
                    "archive_prefix": path_member_prefix(source),
                    "file_count": len(files),
                    "reason": item.get("reason"),
                }
            )
            for file_path in files:
                records.append(add_file_member(archive, file_path, archive_path_for_source(source, file_path), "host_path"))

        for item in scope["include_docker_volumes"]:
            name_value = item.get("name")
            if not isinstance(name_value, str) or not name_value:
                raise OldStackBackupError("include_docker_volumes entries require name")
            volume_name = validate_docker_name(name_value)
            mountpoint = docker_volume_mountpoint(volume_name, command_runner)
            if not mountpoint.exists():
                raise OldStackBackupError(f"Docker volume mountpoint does not exist: {mountpoint}")
            files = iter_regular_files(mountpoint)
            source_index.append(
                {
                    "type": "docker_volume",
                    "name": volume_name,
                    "mountpoint": str(mountpoint),
                    "archive_prefix": f"docker-volumes/{volume_name}",
                    "file_count": len(files),
                    "reason": item.get("reason"),
                }
            )
            for file_path in files:
                records.append(add_file_member(archive, file_path, archive_path_for_volume(volume_name, mountpoint, file_path), "docker_volume"))

        for item in scope["include_containers"]:
            name_value = item.get("name")
            if not isinstance(name_value, str) or not name_value:
                raise OldStackBackupError("include_containers entries require name")
            container_name = validate_docker_name(name_value)
            inspect_payload = docker_inspect_container(container_name, command_runner)
            archive_name = f"docker-inspect/containers/{container_name}.json"
            review_archive_name = f"docker-inspect-redacted/containers/{container_name}.json"
            records.append(add_bytes_member(archive, archive_name, inspect_payload.raw, mode=0o600, sensitive=True))
            records.append(add_bytes_member(archive, review_archive_name, inspect_payload.redacted, mode=0o600))
            source_index.append(
                {
                    "type": "docker_container_metadata",
                    "name": container_name,
                    "archive_path": archive_name,
                    "redacted_review_archive_path": review_archive_name,
                    "raw_metadata_may_include_environment_secrets": True,
                    "reason": item.get("reason"),
                }
            )

    manifest = {
        "format": BACKUP_FORMAT,
        "created_at": created_at,
        "scope_file": str(scope_path.resolve()),
        "reviewed_by": scope["reviewed_by"],
        "review_notes": scope.get("review_notes"),
        "archive": {
            "file": archive_path.name,
            "size_bytes": archive_path.stat().st_size,
            "sha256": sha256_file(archive_path),
        },
        "files": [record.__dict__ for record in records],
        "sources": source_index,
        "contains_sensitive_data": any(record.sensitive for record in records),
        "safety": {
            "read_only_backup": True,
            "operator_scope_required": True,
            "old_stack_deletion_allowed": False,
            "unknown_resources_preserved": True,
        },
    }
    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_path.chmod(0o600)
    archive_path.chmod(0o600)
    return backup_dir


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    manifest = load_json_file(backup_dir / "manifest.json")
    if manifest.get("format") != BACKUP_FORMAT:
        raise OldStackBackupError("unsupported old-stack backup format")
    archive_path = backup_dir / manifest["archive"]["file"]
    if not archive_path.is_file():
        raise OldStackBackupError("missing backup archive")
    actual_archive_digest = sha256_file(archive_path)
    if actual_archive_digest != manifest["archive"]["sha256"]:
        raise OldStackBackupError("archive checksum mismatch")
    expected = {item["archive_path"]: item for item in manifest.get("files", [])}
    seen: set[str] = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            member_path = safe_archive_path(member.name)
            if member_path in seen:
                raise OldStackBackupError(f"duplicate archive member: {member.name}")
            if not member.isfile():
                raise OldStackBackupError(f"unsupported archive member type: {member.name}")
            record = expected.get(member_path)
            if record is None:
                raise OldStackBackupError(f"unmanifested archive member: {member.name}")
            if member.size != record["size_bytes"]:
                raise OldStackBackupError(f"archive member size mismatch: {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise OldStackBackupError(f"cannot read archive member: {member.name}")
            digest = hashlib.sha256()
            read_bytes = 0
            with source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    read_bytes += len(chunk)
                    digest.update(chunk)
            if read_bytes != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
                raise OldStackBackupError(f"archive member checksum mismatch: {member.name}")
            seen.add(member_path)
    missing = sorted(set(expected) - seen)
    if missing:
        raise OldStackBackupError(f"archive missing manifest members: {', '.join(missing[:5])}")
    return {
        "status": "verified",
        "backup": str(backup_dir.resolve()),
        "files_verified": len(seen),
        "contains_sensitive_data": bool(manifest.get("contains_sensitive_data")),
    }


def write_scope_template(inventory_path: Path, output: Path) -> Path:
    inventory = load_inventory(inventory_path)
    template = build_scope_template(inventory)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(template, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan and create explicit old-stack migration backups.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Write an operator scope template from an inventory report.")
    plan_parser.add_argument("--inventory", required=True)
    plan_parser.add_argument("--output", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create a backup from an operator-reviewed scope file.")
    backup_parser.add_argument("--scope", required=True)
    backup_parser.add_argument("--output-root", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub") + "/backups")
    backup_parser.add_argument("--label", default=None)

    verify_parser = subparsers.add_parser("verify", help="Verify an old-stack backup archive against its manifest.")
    verify_parser.add_argument("--backup", required=True)

    args = parser.parse_args()
    try:
        if args.command == "plan":
            output = write_scope_template(Path(args.inventory), Path(args.output))
            print(f"wrote old-stack scope template: {output}")
            return
        if args.command == "backup":
            backup_dir = backup_old_stack(scope_path=Path(args.scope), output_root=Path(args.output_root), label=args.label)
            report = verify_backup(backup_dir)
            print(f"created old-stack backup: {backup_dir}")
            print(json.dumps(report, indent=2, sort_keys=True))
            return
        if args.command == "verify":
            print(json.dumps(verify_backup(Path(args.backup)), indent=2, sort_keys=True))
            return
    except OldStackBackupError as exc:
        raise SystemExit(f"old-stack backup failed: {exc}") from exc


if __name__ == "__main__":
    main()
