from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse, urlunparse

from .catalog import ModelManifest, parse_manifest_payload
from .scheduler import ResourcePolicy, classify_resource_fit
from .security import is_safe_public_import_url


class ModelLifecycleError(ValueError):
    pass


SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:$")
SUPPORTED_ARCHIVE_FORMATS = {"zip", "tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tar.xz", "txz"}
ARCHIVE_FORMAT_BY_SUFFIX = {
    ".zip": "zip",
    ".tar": "tar",
    ".tar.gz": "tar.gz",
    ".tgz": "tgz",
    ".tar.bz2": "tar.bz2",
    ".tbz2": "tbz2",
    ".tar.xz": "tar.xz",
    ".txz": "txz",
}
DEFAULT_ARCHIVE_MAX_MEMBERS = 10_000
DEFAULT_ARCHIVE_MAX_FILE_SIZE_BYTES = 128 * 1024 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_TOTAL_SIZE_BYTES = 512 * 1024 * 1024 * 1024
DENIED_ARCHIVE_FILE_SUFFIXES = {
    ".app",
    ".bat",
    ".bash",
    ".binpkg",
    ".cjs",
    ".class",
    ".cmd",
    ".com",
    ".desktop",
    ".dll",
    ".dylib",
    ".exe",
    ".fish",
    ".jar",
    ".js",
    ".mjs",
    ".msi",
    ".ps1",
    ".py",
    ".pyc",
    ".pyd",
    ".scr",
    ".service",
    ".sh",
    ".so",
    ".vbs",
    ".wsf",
    ".zsh",
}


def parse_uploaded_manifest(payload: dict[str, Any]) -> ModelManifest:
    return parse_manifest_payload(payload, "uploaded_manifest")


def model_root(data_root: Path) -> Path:
    return data_root / "models"


def blob_root(data_root: Path) -> Path:
    return model_root(data_root) / "blobs"


def blob_partial_root(data_root: Path) -> Path:
    return blob_root(data_root) / ".partial"


def blob_path_for(data_root: Path, sha256: str) -> Path:
    return blob_root(data_root) / sha256.lower()


def blob_partial_path_for(data_root: Path, sha256: str) -> Path:
    return blob_partial_root(data_root) / f"{sha256.lower()}.partial"


def runtime_view_root(data_root: Path) -> Path:
    return model_root(data_root) / "runtime-views"


def view_quarantine_root(data_root: Path) -> Path:
    return model_root(data_root) / "quarantine" / "runtime-views"


def blob_quarantine_root(data_root: Path) -> Path:
    return model_root(data_root) / "quarantine" / "blobs"


def safe_component(value: str, context: str) -> str:
    if not SAFE_COMPONENT_RE.match(value):
        raise ModelLifecycleError(f"{context} has unsafe path component: {value}")
    return value


def runtime_manifest_view_root(data_root: Path, runtime: str, manifest: ModelManifest) -> Path:
    return (
        runtime_view_root(data_root)
        / safe_component(runtime, "runtime")
        / safe_component(manifest.id, "model id")
        / safe_component(manifest.version, "model version")
    )


def runtime_manifest_container_path(runtime: str, manifest: ModelManifest) -> str:
    return f"/srv/b1-ai-hub/models/{safe_component(manifest.id, 'model id')}/{safe_component(manifest.version, 'model version')}"


def ensure_directory_inside(path: Path, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve(strict=True)
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ModelLifecycleError(f"path escapes runtime view root: {path}") from exc
    current = root_resolved
    relative_parts = candidate.relative_to(root_resolved).parts
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise ModelLifecycleError(f"runtime view path contains symlink: {current}")
        if current.exists() and not current.is_dir():
            raise ModelLifecycleError(f"runtime view path is not a directory: {current}")
        current.mkdir(exist_ok=True)


def safe_view_file_path(view_root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative.replace("\\", "/"))
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ModelLifecycleError(f"unsafe runtime view file path: {relative}")
    candidate = view_root.joinpath(*rel.parts)
    view_resolved = view_root.resolve(strict=True)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(view_resolved)
    except ValueError as exc:
        raise ModelLifecycleError(f"runtime view file escapes model directory: {relative}") from exc
    return candidate


def is_internal_placeholder_file(file_record: dict[str, Any], source_type: str) -> bool:
    return (
        source_type == "catalog"
        and file_record.get("format") == "internal"
        and file_record.get("sha256") == "0" * 64
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_format_for_path(path: Path | str) -> str | None:
    name = Path(path).name.lower()
    for suffix, archive_format in sorted(ARCHIVE_FORMAT_BY_SUFFIX.items(), key=lambda item: len(item[0]), reverse=True):
        if name.endswith(suffix):
            return archive_format
    return None


def file_record_archive_format(file_record: Any) -> str | None:
    file_format = str(getattr(file_record, "format", "") or "").lower()
    if file_format in SUPPORTED_ARCHIVE_FORMATS:
        return file_format
    return archive_format_for_path(getattr(file_record, "path", ""))


def safe_archive_member_path(name: str) -> PurePosixPath:
    if "\x00" in name:
        raise ModelLifecycleError("archive member path contains NUL byte")
    normalized = name.replace("\\", "/")
    if len(normalized) > 4096:
        raise ModelLifecycleError(f"archive member path is too long: {name[:120]}")
    rel = PurePosixPath(normalized)
    if rel.is_absolute():
        raise ModelLifecycleError(f"archive member uses an absolute path: {name}")
    if not rel.parts:
        raise ModelLifecycleError("archive member path is empty")
    if WINDOWS_DRIVE_RE.match(rel.parts[0]):
        raise ModelLifecycleError(f"archive member uses a Windows drive path: {name}")
    if any(part in {"", ".", ".."} for part in rel.parts):
        raise ModelLifecycleError(f"archive member uses an unsafe relative path: {name}")
    return rel


def validate_archive_file_type(relative_path: PurePosixPath) -> None:
    suffixes = {suffix.lower() for suffix in relative_path.suffixes}
    denied = sorted(suffixes.intersection(DENIED_ARCHIVE_FILE_SUFFIXES))
    if denied:
        raise ModelLifecycleError(f"archive member has a denied file type: {relative_path}")


def _archive_target_path(root: Path, relative_path: PurePosixPath) -> Path:
    if root.is_symlink():
        raise ModelLifecycleError(f"archive extraction root is a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve(strict=True)
    candidate = root.joinpath(*relative_path.parts)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ModelLifecycleError(f"archive member escapes extraction root: {relative_path}") from exc
    return candidate


def _zip_member_kind(info: zipfile.ZipInfo) -> str:
    mode = (info.external_attr >> 16) & 0o170000
    if info.is_dir():
        return "directory"
    if mode == 0 or mode == stat.S_IFREG:
        return "file"
    if mode == stat.S_IFLNK:
        raise ModelLifecycleError(f"zip archive member is a symlink: {info.filename}")
    raise ModelLifecycleError(f"zip archive member is not a regular file or directory: {info.filename}")


def _validate_archive_member(
    *,
    name: str,
    kind: str,
    size_bytes: int,
    seen_paths: dict[str, str],
    max_file_size_bytes: int,
) -> tuple[PurePosixPath, dict[str, Any]]:
    relative_path = safe_archive_member_path(name.rstrip("/"))
    path_key = relative_path.as_posix()
    previous_kind = seen_paths.get(path_key)
    if previous_kind and not (previous_kind == "directory" and kind == "directory"):
        raise ModelLifecycleError(f"archive contains duplicate member path: {path_key}")
    seen_paths[path_key] = kind
    if kind == "file":
        validate_archive_file_type(relative_path)
        if size_bytes < 0:
            raise ModelLifecycleError(f"archive member has a negative size: {path_key}")
        if size_bytes > max_file_size_bytes:
            raise ModelLifecycleError(f"archive member exceeds maximum file size: {path_key}")
    return relative_path, {"path": path_key, "kind": kind, "size_bytes": size_bytes if kind == "file" else 0}


def _validate_archive_summary(
    *,
    archive_path: Path,
    archive_format: str,
    members: list[dict[str, Any]],
    total_size_bytes: int,
    max_members: int,
    max_total_size_bytes: int,
) -> dict[str, Any]:
    if len(members) > max_members:
        raise ModelLifecycleError(f"archive contains too many members: {len(members)} > {max_members}")
    if total_size_bytes > max_total_size_bytes:
        raise ModelLifecycleError(f"archive uncompressed size exceeds limit: {total_size_bytes} > {max_total_size_bytes}")
    file_count = sum(1 for member in members if member["kind"] == "file")
    directory_count = sum(1 for member in members if member["kind"] == "directory")
    return {
        "archive_path": str(archive_path),
        "format": archive_format,
        "member_count": len(members),
        "file_count": file_count,
        "directory_count": directory_count,
        "total_uncompressed_bytes": total_size_bytes,
        "members": members,
    }


def inspect_zip_archive(
    archive_path: Path,
    *,
    max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS,
    max_file_size_bytes: int = DEFAULT_ARCHIVE_MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = DEFAULT_ARCHIVE_MAX_TOTAL_SIZE_BYTES,
) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    seen_paths: dict[str, str] = {}
    total_size_bytes = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                kind = _zip_member_kind(info)
                if info.flag_bits & 0x1:
                    raise ModelLifecycleError(f"zip archive member is encrypted: {info.filename}")
                relative_path, member = _validate_archive_member(
                    name=info.filename,
                    kind=kind,
                    size_bytes=int(info.file_size),
                    seen_paths=seen_paths,
                    max_file_size_bytes=max_file_size_bytes,
                )
                total_size_bytes += member["size_bytes"]
                if len(members) + 1 > max_members:
                    raise ModelLifecycleError(f"archive contains too many members: {len(members) + 1} > {max_members}")
                if total_size_bytes > max_total_size_bytes:
                    raise ModelLifecycleError(f"archive uncompressed size exceeds limit: {total_size_bytes} > {max_total_size_bytes}")
                members.append(member)
    except zipfile.BadZipFile as exc:
        raise ModelLifecycleError(f"invalid zip archive: {archive_path}") from exc
    return _validate_archive_summary(
        archive_path=archive_path,
        archive_format="zip",
        members=members,
        total_size_bytes=total_size_bytes,
        max_members=max_members,
        max_total_size_bytes=max_total_size_bytes,
    )


def inspect_tar_archive(
    archive_path: Path,
    *,
    archive_format: str,
    max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS,
    max_file_size_bytes: int = DEFAULT_ARCHIVE_MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = DEFAULT_ARCHIVE_MAX_TOTAL_SIZE_BYTES,
) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    seen_paths: dict[str, str] = {}
    total_size_bytes = 0
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for info in archive:
                if info.isdir():
                    kind = "directory"
                elif info.isfile():
                    kind = "file"
                else:
                    raise ModelLifecycleError(f"tar archive member is not a regular file or directory: {info.name}")
                _relative_path, member = _validate_archive_member(
                    name=info.name,
                    kind=kind,
                    size_bytes=int(info.size),
                    seen_paths=seen_paths,
                    max_file_size_bytes=max_file_size_bytes,
                )
                total_size_bytes += member["size_bytes"]
                if len(members) + 1 > max_members:
                    raise ModelLifecycleError(f"archive contains too many members: {len(members) + 1} > {max_members}")
                if total_size_bytes > max_total_size_bytes:
                    raise ModelLifecycleError(f"archive uncompressed size exceeds limit: {total_size_bytes} > {max_total_size_bytes}")
                members.append(member)
    except tarfile.TarError as exc:
        raise ModelLifecycleError(f"invalid tar archive: {archive_path}") from exc
    return _validate_archive_summary(
        archive_path=archive_path,
        archive_format=archive_format,
        members=members,
        total_size_bytes=total_size_bytes,
        max_members=max_members,
        max_total_size_bytes=max_total_size_bytes,
    )


def inspect_archive(
    archive_path: Path,
    *,
    archive_format: str | None = None,
    max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS,
    max_file_size_bytes: int = DEFAULT_ARCHIVE_MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = DEFAULT_ARCHIVE_MAX_TOTAL_SIZE_BYTES,
) -> dict[str, Any]:
    detected_format = archive_format or archive_format_for_path(archive_path)
    if detected_format not in SUPPORTED_ARCHIVE_FORMATS:
        raise ModelLifecycleError(f"unsupported archive format: {detected_format or archive_path.name}")
    if detected_format == "zip":
        return inspect_zip_archive(
            archive_path,
            max_members=max_members,
            max_file_size_bytes=max_file_size_bytes,
            max_total_size_bytes=max_total_size_bytes,
        )
    return inspect_tar_archive(
        archive_path,
        archive_format=detected_format,
        max_members=max_members,
        max_file_size_bytes=max_file_size_bytes,
        max_total_size_bytes=max_total_size_bytes,
    )


def _prepare_archive_destination(root: Path, relative_path: PurePosixPath, kind: str, *, overwrite: bool) -> Path:
    candidate = _archive_target_path(root, relative_path)
    if kind == "directory":
        ensure_directory_inside(candidate, root)
        candidate.chmod(0o755)
        return candidate
    ensure_directory_inside(candidate.parent, root)
    if candidate.is_symlink():
        raise ModelLifecycleError(f"archive extraction refuses symlink destination: {candidate}")
    if candidate.exists() and not overwrite:
        raise ModelLifecycleError(f"archive extraction destination already exists: {candidate}")
    if candidate.exists() and not candidate.is_file():
        raise ModelLifecycleError(f"archive extraction destination is not a regular file: {candidate}")
    return candidate


def safe_extract_zip_archive(
    archive_path: Path,
    target_dir: Path,
    *,
    overwrite: bool = False,
    max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS,
    max_file_size_bytes: int = DEFAULT_ARCHIVE_MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = DEFAULT_ARCHIVE_MAX_TOTAL_SIZE_BYTES,
) -> dict[str, Any]:
    summary = inspect_zip_archive(
        archive_path,
        max_members=max_members,
        max_file_size_bytes=max_file_size_bytes,
        max_total_size_bytes=max_total_size_bytes,
    )
    members_by_path = {member["path"]: member for member in summary["members"]}
    extracted_files: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            relative_path = safe_archive_member_path(info.filename.rstrip("/"))
            member = members_by_path[relative_path.as_posix()]
            destination = _prepare_archive_destination(target_dir, relative_path, member["kind"], overwrite=overwrite)
            if member["kind"] == "directory":
                continue
            bytes_written = 0
            with archive.open(info, "r") as source, destination.open("wb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    bytes_written += len(chunk)
                    if bytes_written > member["size_bytes"]:
                        raise ModelLifecycleError(f"zip member exceeded inspected size while extracting: {member['path']}")
                    target.write(chunk)
            if bytes_written != member["size_bytes"]:
                raise ModelLifecycleError(f"zip member size changed while extracting: {member['path']}")
            destination.chmod(0o644)
            extracted_files.append({"path": member["path"], "size_bytes": bytes_written, "destination": str(destination)})
    return {**summary, "extraction_root": str(target_dir), "extracted_files": extracted_files}


def safe_extract_tar_archive(
    archive_path: Path,
    target_dir: Path,
    *,
    archive_format: str,
    overwrite: bool = False,
    max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS,
    max_file_size_bytes: int = DEFAULT_ARCHIVE_MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = DEFAULT_ARCHIVE_MAX_TOTAL_SIZE_BYTES,
) -> dict[str, Any]:
    summary = inspect_tar_archive(
        archive_path,
        archive_format=archive_format,
        max_members=max_members,
        max_file_size_bytes=max_file_size_bytes,
        max_total_size_bytes=max_total_size_bytes,
    )
    members_by_path = {member["path"]: member for member in summary["members"]}
    extracted_files: list[dict[str, Any]] = []
    with tarfile.open(archive_path, "r:*") as archive:
        for info in archive:
            relative_path = safe_archive_member_path(info.name.rstrip("/"))
            member = members_by_path[relative_path.as_posix()]
            destination = _prepare_archive_destination(target_dir, relative_path, member["kind"], overwrite=overwrite)
            if member["kind"] == "directory":
                continue
            source = archive.extractfile(info)
            if source is None:
                raise ModelLifecycleError(f"tar member could not be read as a regular file: {member['path']}")
            bytes_written = 0
            with source, destination.open("wb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    bytes_written += len(chunk)
                    if bytes_written > member["size_bytes"]:
                        raise ModelLifecycleError(f"tar member exceeded inspected size while extracting: {member['path']}")
                    target.write(chunk)
            if bytes_written != member["size_bytes"]:
                raise ModelLifecycleError(f"tar member size changed while extracting: {member['path']}")
            destination.chmod(0o644)
            extracted_files.append({"path": member["path"], "size_bytes": bytes_written, "destination": str(destination)})
    return {**summary, "extraction_root": str(target_dir), "extracted_files": extracted_files}


def safe_extract_archive(
    archive_path: Path,
    target_dir: Path,
    *,
    archive_format: str | None = None,
    overwrite: bool = False,
    max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS,
    max_file_size_bytes: int = DEFAULT_ARCHIVE_MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = DEFAULT_ARCHIVE_MAX_TOTAL_SIZE_BYTES,
) -> dict[str, Any]:
    detected_format = archive_format or archive_format_for_path(archive_path)
    if detected_format not in SUPPORTED_ARCHIVE_FORMATS:
        raise ModelLifecycleError(f"unsupported archive format: {detected_format or archive_path.name}")
    if target_dir.is_symlink():
        raise ModelLifecycleError(f"archive extraction root is a symlink: {target_dir}")
    if detected_format == "zip":
        return safe_extract_zip_archive(
            archive_path,
            target_dir,
            overwrite=overwrite,
            max_members=max_members,
            max_file_size_bytes=max_file_size_bytes,
            max_total_size_bytes=max_total_size_bytes,
        )
    return safe_extract_tar_archive(
        archive_path,
        target_dir,
        archive_format=detected_format,
        overwrite=overwrite,
        max_members=max_members,
        max_file_size_bytes=max_file_size_bytes,
        max_total_size_bytes=max_total_size_bytes,
    )


def verify_manifest_files(manifest: ModelManifest, data_root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for file in manifest.files:
        file_record = file.to_dict()
        digest = file.sha256.lower()
        expected_size = int(file.size_bytes)
        path = blob_path_for(data_root, digest)
        if is_internal_placeholder_file(file_record, manifest.source.type):
            files.append(
                {
                    **file_record,
                    "status": "internal-placeholder",
                    "blob_path": None,
                    "verified": True,
                }
            )
            continue
        if path.is_symlink():
            files.append({**file_record, "status": "symlink-refused", "blob_path": str(path), "verified": False})
            continue
        if not path.exists():
            files.append({**file_record, "status": "missing", "blob_path": str(path), "verified": False})
            continue
        if not path.is_file():
            files.append({**file_record, "status": "not-a-file", "blob_path": str(path), "verified": False})
            continue
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            files.append(
                {
                    **file_record,
                    "status": "size-mismatch",
                    "blob_path": str(path),
                    "actual_size_bytes": actual_size,
                    "verified": False,
                }
            )
            continue
        actual_sha = sha256_file(path)
        if actual_sha != digest:
            files.append({**file_record, "status": "sha256-mismatch", "blob_path": str(path), "actual_sha256": actual_sha, "verified": False})
            continue
        files.append({**file_record, "status": "verified", "blob_path": str(path), "verified": True})
    return files


def inspect_manifest_archive_files(manifest: ModelManifest, data_root: Path, file_status: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    status_by_path = {item["path"]: item for item in file_status}
    inspections: list[dict[str, Any]] = []
    blockers: list[str] = []
    for file in manifest.files:
        archive_format = file_record_archive_format(file)
        if not archive_format:
            continue
        file_record = file.to_dict()
        status = status_by_path.get(file.path, {})
        inspection = {**file_record, "archive_format": archive_format}
        if not status.get("verified") or not status.get("blob_path"):
            inspections.append({**inspection, "inspection_status": "unavailable", "reason": "blob is not verified"})
            continue
        try:
            summary = inspect_archive(Path(status["blob_path"]), archive_format=archive_format)
        except ModelLifecycleError as exc:
            blockers.append(f"{file.path}: {exc}")
            inspections.append({**inspection, "inspection_status": "blocked", "error": str(exc)})
            continue
        inspections.append({**inspection, "inspection_status": "safe", "summary": summary})
    return inspections, blockers


def runtime_view_plan(manifest: ModelManifest, data_root: Path) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for runtime in manifest.runtimes:
        root = runtime_manifest_view_root(data_root, runtime, manifest)
        files = []
        for file in manifest.files:
            archive_format = file_record_archive_format(file)
            view_path = root / file.path
            extraction_root = view_path.parent
            if archive_format:
                files.append(
                    {
                        "path": file.path,
                        "view_path": str(extraction_root),
                        "blob_path": str(blob_path_for(data_root, file.sha256)),
                        "link_type": "safe-archive-extract",
                        "archive_format": archive_format,
                    }
                )
                continue
            files.append(
                {
                    "path": file.path,
                    "view_path": str(view_path),
                    "blob_path": str(blob_path_for(data_root, file.sha256)) if file.sha256 != "0" * 64 else None,
                    "link_type": "internal-placeholder" if is_internal_placeholder_file(file.to_dict(), manifest.source.type) else "hardlink",
                }
            )
        views.append(
            {
                "runtime": runtime,
                "host_path": str(root),
                "container_path": runtime_manifest_container_path(runtime, manifest),
                "files": files,
            }
        )
    return views


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(path.name + ".partial")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def hardlink_blob_into_view(target: Path, link_path: Path, view_root: Path) -> dict[str, Any]:
    ensure_directory_inside(link_path.parent, view_root)
    if link_path.is_symlink():
        raise ModelLifecycleError(f"runtime view refuses symlinked file path: {link_path}")
    if link_path.exists():
        try:
            if os.path.samefile(target, link_path):
                return {"view_path": str(link_path), "blob_path": str(target), "link_type": "hardlink", "status": "already-linked"}
        except OSError as exc:
            raise ModelLifecycleError(f"could not compare existing runtime view file: {link_path}") from exc
        raise ModelLifecycleError(f"runtime view file already exists with different content: {link_path}")
    try:
        os.link(target, link_path)
    except OSError as exc:
        raise ModelLifecycleError(f"failed to link blob into runtime view: {exc}") from exc
    return {"view_path": str(link_path), "blob_path": str(target), "link_type": "hardlink", "status": "linked"}


def create_runtime_views(manifest: ModelManifest, data_root: Path) -> list[dict[str, Any]]:
    file_status = verify_manifest_files(manifest, data_root)
    if not all(item["verified"] for item in file_status):
        raise ModelLifecycleError("cannot create runtime views until every manifest file verifies")
    verified_by_sha = {item["sha256"]: item for item in file_status}
    created: list[dict[str, Any]] = []
    root = runtime_view_root(data_root)
    for runtime in manifest.runtimes:
        view_root = runtime_manifest_view_root(data_root, runtime, manifest)
        ensure_directory_inside(view_root, root)
        linked_files: list[dict[str, Any]] = []
        for file in manifest.files:
            file_record = file.to_dict()
            if is_internal_placeholder_file(file_record, manifest.source.type):
                linked_files.append({"path": file.path, "link_type": "internal-placeholder", "status": "skipped"})
                continue
            status = verified_by_sha[file.sha256]
            if not status.get("blob_path"):
                raise ModelLifecycleError(f"verified file has no blob path: {file.path}")
            archive_format = file_record_archive_format(file)
            if archive_format:
                archive_marker_path = safe_view_file_path(view_root, file.path)
                extraction_root = archive_marker_path.parent
                summary = safe_extract_archive(Path(status["blob_path"]), extraction_root, archive_format=archive_format)
                linked_files.append(
                    {
                        "path": file.path,
                        "blob_path": status["blob_path"],
                        "view_path": str(extraction_root),
                        "link_type": "safe-archive-extract",
                        "archive_format": archive_format,
                        "status": "extracted",
                        "file_count": summary["file_count"],
                        "total_uncompressed_bytes": summary["total_uncompressed_bytes"],
                        "extracted_files": summary["extracted_files"],
                    }
                )
                continue
            link_path = safe_view_file_path(view_root, file.path)
            linked_files.append(
                {
                    "path": file.path,
                    **hardlink_blob_into_view(Path(status["blob_path"]), link_path, view_root),
                }
            )
        write_json_atomic(
            view_root / "manifest.b1.json",
            {
                "format": "b1-ai-hub-runtime-view/v1",
                "created_at": datetime.now(tz=UTC).isoformat(),
                "runtime": runtime,
                "model_ref": f"{manifest.id}@{manifest.version}",
                "manifest": manifest.to_dict(),
                "files": linked_files,
            },
        )
        created.append(
            {
                "runtime": runtime,
                "host_path": str(view_root),
                "container_path": runtime_manifest_container_path(runtime, manifest),
                "files": linked_files,
            }
        )
    return created


def quarantine_runtime_views(manifest: ModelManifest, data_root: Path, timestamp: datetime | None = None) -> list[dict[str, Any]]:
    now = timestamp or datetime.now(tz=UTC)
    suffix = now.strftime("%Y%m%dT%H%M%SZ")
    moved: list[dict[str, Any]] = []
    source_root = runtime_view_root(data_root)
    quarantine_root = view_quarantine_root(data_root)
    for runtime in manifest.runtimes:
        source = runtime_manifest_view_root(data_root, runtime, manifest)
        destination = (
            quarantine_root
            / safe_component(runtime, "runtime")
            / safe_component(manifest.id, "model id")
            / f"{safe_component(manifest.version, 'model version')}-{suffix}"
        )
        if source.is_symlink():
            raise ModelLifecycleError(f"runtime view root is a symlink and will not be moved: {source}")
        if not source.exists():
            moved.append({"runtime": runtime, "source": str(source), "status": "missing"})
            continue
        if not source.is_dir():
            raise ModelLifecycleError(f"runtime view root is not a directory: {source}")
        ensure_directory_inside(destination.parent, quarantine_root)
        if destination.exists():
            raise ModelLifecycleError(f"runtime view quarantine destination already exists: {destination}")
        shutil.move(str(source), str(destination))
        moved.append({"runtime": runtime, "source": str(source), "quarantine_path": str(destination), "status": "quarantined"})
        # Recreate the runtime/model parent so read-only runtime mounts remain stable.
        ensure_directory_inside(source.parent, source_root)
    return moved


def referenced_blob_records(model_records: list[dict[str, Any]], *, exclude_model_ref: str) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    for row in model_records:
        model_ref = f"{row.get('id')}@{row.get('version')}"
        if model_ref == exclude_model_ref:
            continue
        manifest = row.get("manifest") or {}
        for file in manifest.get("files") or []:
            sha = str(file.get("sha256", "")).lower()
            if sha and sha != "0" * 64:
                references.setdefault(sha, []).append(model_ref)
    return references


def build_blob_quarantine_plan(
    manifest: ModelManifest,
    data_root: Path,
    model_records: list[dict[str, Any]],
    *,
    model_status: str,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    now = timestamp or datetime.now(tz=UTC)
    suffix = now.strftime("%Y%m%dT%H%M%SZ")
    model_ref = f"{manifest.id}@{manifest.version}"
    references = referenced_blob_records(model_records, exclude_model_ref=model_ref)
    blockers: list[str] = []
    if model_status == "installed":
        blockers.append("model record must be quarantined before authoritative blobs can be quarantined")
    blobs: list[dict[str, Any]] = []
    for file in manifest.files:
        file_record = file.to_dict()
        if is_internal_placeholder_file(file_record, manifest.source.type):
            blobs.append({**file_record, "status": "internal-placeholder", "can_quarantine": False, "reason": "no authoritative blob"})
            continue
        sha = file.sha256.lower()
        path = blob_path_for(data_root, sha)
        destination = (
            blob_quarantine_root(data_root)
            / safe_component(manifest.id, "model id")
            / f"{safe_component(manifest.version, 'model version')}-{suffix}"
            / sha
        )
        blob_blockers: list[str] = []
        referenced_by = sorted(references.get(sha, []))
        if referenced_by:
            blob_blockers.append(f"blob is referenced by other model records: {', '.join(referenced_by)}")
        if path.is_symlink():
            blob_blockers.append("authoritative blob path is a symlink")
        elif not path.exists():
            blob_blockers.append("authoritative blob is missing")
        elif not path.is_file():
            blob_blockers.append("authoritative blob path is not a file")
        else:
            actual_size = path.stat().st_size
            if actual_size != file.size_bytes:
                blob_blockers.append(f"size mismatch: {actual_size} != {file.size_bytes}")
            elif sha256_file(path) != sha:
                blob_blockers.append("SHA-256 mismatch")
        if destination.exists():
            blob_blockers.append("quarantine destination already exists")
        blockers.extend(f"{file.path}: {blocker}" for blocker in blob_blockers)
        blobs.append(
            {
                **file_record,
                "blob_path": str(path),
                "quarantine_path": str(destination),
                "referenced_by": referenced_by,
                "status": "quarantine-ready" if not blob_blockers else "blocked",
                "can_quarantine": not blob_blockers,
                "blockers": blob_blockers,
            }
        )
    ready = [blob for blob in blobs if blob.get("can_quarantine")]
    return {
        "model_ref": model_ref,
        "model_status": model_status,
        "status": "quarantine-ready" if ready and not blockers else "blocked",
        "can_quarantine": bool(ready and not blockers),
        "blockers": blockers,
        "blobs": blobs,
        "total_size_bytes": sum(int(blob["size_bytes"]) for blob in ready),
    }


def quarantine_authoritative_blobs(
    manifest: ModelManifest,
    data_root: Path,
    model_records: list[dict[str, Any]],
    *,
    model_status: str,
    confirmed: bool,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    plan = build_blob_quarantine_plan(manifest, data_root, model_records, model_status=model_status, timestamp=timestamp)
    if not confirmed:
        raise ModelLifecycleError("blob quarantine requires explicit confirmation")
    if not plan["can_quarantine"]:
        raise ModelLifecycleError("; ".join(plan["blockers"]) or "no blobs can be quarantined")
    root = blob_quarantine_root(data_root)
    moved: list[dict[str, Any]] = []
    for blob in plan["blobs"]:
        if not blob.get("can_quarantine"):
            continue
        source = Path(blob["blob_path"])
        destination = Path(blob["quarantine_path"])
        ensure_directory_inside(destination.parent, root)
        if source.is_symlink() or destination.exists() or not source.is_file():
            raise ModelLifecycleError(f"unsafe blob quarantine path for {blob['sha256']}")
        actual_size = source.stat().st_size
        if actual_size != int(blob["size_bytes"]) or sha256_file(source) != blob["sha256"]:
            raise ModelLifecycleError(f"authoritative blob changed before quarantine move: {blob['sha256']}")
        shutil.move(str(source), str(destination))
        moved.append({**blob, "status": "quarantined"})
    return {**plan, "status": "quarantined", "can_quarantine": False, "moved": moved}


def source_url_allowed(manifest: ModelManifest) -> bool:
    if manifest.source.type == "upload":
        return True
    parsed = urlparse(manifest.source.url)
    if parsed.username or parsed.password:
        return False
    sensitive_query_keys = {"token", "api_key", "apikey", "key", "signature", "sig", "credential", "access_token"}
    if any(key.lower() in sensitive_query_keys for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        return False
    return is_safe_public_import_url(manifest.source.url)


def direct_download_files(manifest: ModelManifest) -> list[Any]:
    if manifest.source.type != "direct-url":
        raise ModelLifecycleError("download worker currently supports only direct-url manifests")
    return list(manifest.files)


def direct_download_source_url(manifest: ModelManifest, file: Any) -> str:
    if len(manifest.files) == 1:
        return manifest.source.url
    parsed = urlparse(manifest.source.url)
    if not parsed.path.endswith("/"):
        raise ModelLifecycleError("multi-file direct-url manifests require source.url to end with /")
    if parsed.query or parsed.fragment:
        raise ModelLifecycleError("multi-file direct-url manifests cannot use query strings or fragments on source.url")
    encoded_path = "/".join(quote(part, safe="") for part in PurePosixPath(file.path).parts)
    return urlunparse((parsed.scheme, parsed.netloc, f"{parsed.path}{encoded_path}", "", "", ""))


def download_file_plan(manifest: ModelManifest, file: Any, verification: dict[str, Any], data_root: Path) -> dict[str, Any]:
    target_path = blob_path_for(data_root, file.sha256)
    partial_path = blob_partial_path_for(data_root, file.sha256)
    existing_partial_bytes = partial_path.stat().st_size if partial_path.is_file() and not partial_path.is_symlink() else 0
    already_available = bool(verification.get("verified") and verification.get("status") == "verified")
    blockers: list[str] = []
    if target_path.is_symlink():
        blockers.append("target blob path is a symlink")
    elif target_path.exists() and not already_available:
        blockers.append("target blob exists but does not verify")
    if partial_path.is_symlink():
        blockers.append("partial blob path is a symlink")
    return {
        **file.to_dict(),
        "source_url": direct_download_source_url(manifest, file),
        "target_sha256": file.sha256,
        "target_size_bytes": file.size_bytes,
        "target_path": str(target_path),
        "partial_path": str(partial_path),
        "existing_partial_bytes": existing_partial_bytes,
        "already_available": already_available,
        "blockers": blockers,
        "verification": verification,
    }


def build_download_plan(manifest: ModelManifest, data_root: Path) -> dict[str, Any]:
    files = direct_download_files(manifest)
    source_allowed = source_url_allowed(manifest)
    verification_by_path = {item["path"]: item for item in verify_manifest_files(manifest, data_root)}
    blockers: list[str] = []
    if not source_allowed:
        blockers.append("source URL is not allowed by import policy")
    duplicate_hashes = sorted({file.sha256 for file in files if sum(1 for item in files if item.sha256 == file.sha256) > 1})
    if duplicate_hashes:
        blockers.append(f"direct-url downloads do not support duplicate target SHA-256 entries: {', '.join(duplicate_hashes)}")
    file_plans: list[dict[str, Any]] = []
    try:
        for file in files:
            file_plan = download_file_plan(manifest, file, verification_by_path[file.path], data_root)
            file_plans.append(file_plan)
            blockers.extend(f"{file.path}: {blocker}" for blocker in file_plan["blockers"])
    except ModelLifecycleError as exc:
        blockers.append(str(exc))
    already_available = bool(file_plans) and all(item["already_available"] for item in file_plans)
    total_size_bytes = sum(file.size_bytes for file in files)
    existing_partial_bytes = sum(
        int(item["target_size_bytes"] if item["already_available"] else item["existing_partial_bytes"])
        for item in file_plans
    )
    first_file = file_plans[0] if file_plans else None
    return {
        "model": manifest.to_dict(),
        "model_ref": f"{manifest.id}@{manifest.version}",
        "status": "available" if already_available else "downloadable" if source_allowed and not blockers else "blocked",
        "can_download": bool(source_allowed and not blockers),
        "already_available": already_available,
        "blockers": blockers,
        "source_url": manifest.source.url,
        "target_sha256": first_file["target_sha256"] if first_file else None,
        "target_size_bytes": total_size_bytes,
        "target_path": first_file["target_path"] if first_file else None,
        "partial_path": first_file["partial_path"] if first_file else None,
        "existing_partial_bytes": existing_partial_bytes,
        "file": first_file,
        "files": file_plans,
        "file_count": len(files),
        "verification": [item["verification"] for item in file_plans],
    }


def manifest_aliases_are_known(manifest: ModelManifest, known_aliases: set[str]) -> list[str]:
    return sorted(alias for alias in manifest.aliases if alias not in known_aliases)


def build_install_plan(
    manifest: ModelManifest,
    data_root: Path,
    policy: ResourcePolicy,
    *,
    known_aliases: set[str],
    allow_resource_override: bool = False,
    accept_license: bool = False,
) -> dict[str, Any]:
    estimate = manifest.resource_estimate.to_scheduler_estimate(requires_gpu=manifest.preferred_runtime != "audio-cpu")
    decision = classify_resource_fit(policy, estimate)
    file_status = verify_manifest_files(manifest, data_root)
    archive_inspections, archive_blockers = inspect_manifest_archive_files(manifest, data_root, file_status)
    source_allowed = source_url_allowed(manifest)
    unknown_aliases = manifest_aliases_are_known(manifest, known_aliases)
    requires_license_acceptance = bool(manifest.license.acceptance_required)
    files_verified = all(item["verified"] for item in file_status)
    resource_allowed = bool(decision.accepted or allow_resource_override)
    archives_safe = not archive_blockers
    can_install = (
        source_allowed
        and not unknown_aliases
        and files_verified
        and archives_safe
        and resource_allowed
        and (not requires_license_acceptance or accept_license)
    )
    blockers: list[str] = []
    if not source_allowed:
        blockers.append("source URL is not allowed by import policy")
    if unknown_aliases:
        blockers.append(f"manifest aliases are not defined in the public alias seed: {', '.join(unknown_aliases)}")
    if not files_verified:
        blockers.append("one or more content-addressed blobs are missing or failed verification")
    blockers.extend(archive_blockers)
    if not resource_allowed:
        blockers.append(decision.reason)
    if requires_license_acceptance and not accept_license:
        blockers.append("licence acceptance is required")
    return {
        "model": manifest.to_dict(),
        "model_ref": f"{manifest.id}@{manifest.version}",
        "status": "installable" if can_install else "blocked",
        "can_install": can_install,
        "blockers": blockers,
        "source_allowed": source_allowed,
        "requires_confirmation": True,
        "requires_license_acceptance": requires_license_acceptance,
        "license_accepted": accept_license,
        "files": file_status,
        "archive_inspections": archive_inspections,
        "runtime_views": runtime_view_plan(manifest, data_root),
        "resource_decision": asdict(decision),
        "resource_override": allow_resource_override,
        "total_size_bytes": sum(file.size_bytes for file in manifest.files),
    }


def require_installable(plan: dict[str, Any], *, confirmed: bool) -> None:
    if not confirmed:
        raise ModelLifecycleError("installation requires explicit confirmation")
    if not plan["can_install"]:
        raise ModelLifecycleError("; ".join(plan["blockers"]) or "model cannot be installed")
