from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import uuid
import zipfile
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

from .catalog import ModelManifest, ModelProfile, operation_is_supported, parse_manifest_payload
from .scheduler import ResourcePolicy, classify_resource_fit
from .security import has_credential_query_parameter, is_safe_public_import_url


class ModelLifecycleError(ValueError):
    pass


SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:$")
SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
QUARANTINE_SET_RE = re.compile(r"^(?P<version>.+)-(?P<timestamp>\d{8}T\d{6}Z)$")
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
DOWNLOAD_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
HUGGINGFACE_HOSTS = {"huggingface.co"}
HUGGINGFACE_CDN_HOSTS = {
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co",
    "cdn-lfs-eu-1.huggingface.co",
}
HUGGINGFACE_CDN_SUFFIXES = (".cdn.hf.co", ".hf.co", ".xethub.hf.co")
HUGGINGFACE_REPO_MARKERS = {"tree", "blob", "resolve"}
HUGGINGFACE_FILE_MARKERS = {"blob", "resolve"}
HUGGINGFACE_REPO_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HUGGINGFACE_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROFILE_RESOURCE_TOLERANCE = 1.15
PROFILE_LABEL_SCORE = {
    "recommended": 0,
    "expected": 1,
    "offload-required": 2,
    "experimental": 3,
    "incompatible": 4,
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


def runtime_manifest_staging_view_root(data_root: Path, runtime: str, manifest: ModelManifest, token: str) -> Path:
    return (
        runtime_view_root(data_root)
        / ".staging"
        / safe_component(runtime, "runtime")
        / safe_component(manifest.id, "model id")
        / safe_component(manifest.version, "model version")
        / safe_component(token, "runtime view staging token")
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


def safe_relative_parts(value: str, context: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/")
    rel = PurePosixPath(normalized)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise ModelLifecycleError(f"{context} uses an unsafe relative path")
    if WINDOWS_DRIVE_RE.match(rel.parts[0]):
        raise ModelLifecycleError(f"{context} uses an unsafe relative path")
    for part in rel.parts:
        if BAD_PERCENT_ESCAPE_RE.search(part):
            raise ModelLifecycleError(f"{context} uses an unsafe encoded path-control segment")
        try:
            decoded = unquote(part, errors="strict")
        except UnicodeDecodeError as exc:
            raise ModelLifecycleError(f"{context} uses an unsafe encoded path-control segment") from exc
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded or "?" in decoded or "#" in decoded:
            raise ModelLifecycleError(f"{context} uses an unsafe encoded path-control segment")
        if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
            raise ModelLifecycleError(f"{context} uses an unsafe encoded path-control segment")
    return tuple(rel.parts)


def safe_view_file_path(view_root: Path, relative: str) -> Path:
    parts = safe_relative_parts(relative, f"runtime view file path: {relative}")
    candidate = view_root.joinpath(*parts)
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
    if len(name.replace("\\", "/")) > 4096:
        raise ModelLifecycleError(f"archive member path is too long: {name[:120]}")
    parts = safe_relative_parts(name, f"archive member path: {name}")
    return PurePosixPath(*parts)


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
    for parent in relative_path.parents:
        if parent == PurePosixPath("."):
            continue
        parent_kind = seen_paths.get(parent.as_posix())
        if parent_kind == "file":
            raise ModelLifecycleError(f"archive member path is below a file member: {path_key}")
    if kind == "file":
        child_prefix = f"{path_key}/"
        for existing_path in seen_paths:
            if existing_path.startswith(child_prefix):
                raise ModelLifecycleError(f"archive file member conflicts with existing child member: {path_key}")
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
    if target.is_symlink() or not target.is_file():
        raise ModelLifecycleError(f"blob target is not a regular file: {target}")
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
        if exc.errno == errno.EXDEV:
            try:
                shutil.copyfile(target, link_path)
                copied_size = link_path.stat().st_size
                if copied_size != target.stat().st_size or sha256_file(link_path) != sha256_file(target):
                    raise ModelLifecycleError(f"copied runtime view file failed verification: {link_path}")
                link_path.chmod(0o644)
            except Exception:
                with suppress(FileNotFoundError):
                    link_path.unlink()
                raise
            return {"view_path": str(link_path), "blob_path": str(target), "link_type": "copy-exdev", "status": "copied"}
        raise ModelLifecycleError(f"failed to link blob into runtime view: {exc}") from exc
    return {"view_path": str(link_path), "blob_path": str(target), "link_type": "hardlink", "status": "linked"}


def _published_path_string(value: str, staging_root: Path, final_root: Path) -> str:
    path = Path(value)
    staging_resolved = staging_root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    try:
        relative = resolved.relative_to(staging_resolved)
    except ValueError:
        return value
    return str(final_root.joinpath(*relative.parts))


def _published_file_record(record: dict[str, Any], staging_root: Path, final_root: Path) -> dict[str, Any]:
    published = dict(record)
    for key in ("view_path", "destination"):
        value = published.get(key)
        if isinstance(value, str):
            published[key] = _published_path_string(value, staging_root, final_root)
    extracted = published.get("extracted_files")
    if isinstance(extracted, list):
        published["extracted_files"] = [
            _published_file_record(item, staging_root, final_root) if isinstance(item, dict) else item
            for item in extracted
        ]
    return published


def _published_file_records(records: list[dict[str, Any]], staging_root: Path, final_root: Path) -> list[dict[str, Any]]:
    return [_published_file_record(record, staging_root, final_root) for record in records]


def _remove_staged_runtime_view(path: Path, runtime_views_root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(runtime_views_root.resolve(strict=True))
    except ValueError as exc:
        raise ModelLifecycleError(f"staged runtime view path escapes root: {path}") from exc
    if path.is_symlink():
        raise ModelLifecycleError(f"staged runtime view is a symlink and will not be removed: {path}")
    if path.exists():
        shutil.rmtree(path)
    current = path.parent
    staging_root = runtime_views_root / ".staging"
    while current != runtime_views_root and current.exists():
        if current == staging_root:
            with suppress(OSError):
                current.rmdir()
            break
        with suppress(OSError):
            current.rmdir()
        current = current.parent


def _remove_published_runtime_view(path: Path, runtime_views_root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(runtime_views_root.resolve(strict=True))
    except ValueError as exc:
        raise ModelLifecycleError(f"published runtime view path escapes root: {path}") from exc
    if path.is_symlink() or not path.is_dir():
        raise ModelLifecycleError(f"published runtime view rollback path is unsafe: {path}")
    manifest_path = path / "manifest.b1.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ModelLifecycleError(f"published runtime view rollback missing manifest marker: {path}")
    shutil.rmtree(path)


def _build_runtime_view_in_directory(
    manifest: ModelManifest,
    data_root: Path,
    runtime: str,
    view_root: Path,
    final_view_root: Path,
    file_status_by_sha: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    linked_files: list[dict[str, Any]] = []
    for file in manifest.files:
        file_record = file.to_dict()
        if is_internal_placeholder_file(file_record, manifest.source.type):
            linked_files.append({"path": file.path, "link_type": "internal-placeholder", "status": "skipped"})
            continue
        status = file_status_by_sha[file.sha256]
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

    published_files = _published_file_records(linked_files, view_root, final_view_root)
    write_json_atomic(
        view_root / "manifest.b1.json",
        {
            "format": "b1-ai-hub-runtime-view/v1",
            "created_at": datetime.now(tz=UTC).isoformat(),
            "runtime": runtime,
            "model_ref": f"{manifest.id}@{manifest.version}",
            "manifest": manifest.to_dict(),
            "files": published_files,
        },
    )
    return published_files


def create_runtime_views(manifest: ModelManifest, data_root: Path) -> list[dict[str, Any]]:
    file_status = verify_manifest_files(manifest, data_root)
    if not all(item["verified"] for item in file_status):
        raise ModelLifecycleError("cannot create runtime views until every manifest file verifies")
    verified_by_sha = {item["sha256"]: item for item in file_status}
    root = runtime_view_root(data_root)
    token = uuid.uuid4().hex
    view_specs: list[dict[str, Any]] = []
    for runtime in manifest.runtimes:
        final_view_root = runtime_manifest_view_root(data_root, runtime, manifest)
        if final_view_root.is_symlink():
            raise ModelLifecycleError(f"runtime view root is a symlink: {final_view_root}")
        if final_view_root.exists():
            raise ModelLifecycleError(f"runtime view root already exists: {final_view_root}")
        ensure_directory_inside(final_view_root.parent, root)
        staging_view_root = runtime_manifest_staging_view_root(data_root, runtime, manifest, token)
        view_specs.append({"runtime": runtime, "final": final_view_root, "staging": staging_view_root})

    staged: list[dict[str, Any]] = []
    published_roots: list[Path] = []
    try:
        for spec in view_specs:
            staging_view_root = spec["staging"]
            final_view_root = spec["final"]
            ensure_directory_inside(staging_view_root, root)
            files = _build_runtime_view_in_directory(
                manifest,
                data_root,
                spec["runtime"],
                staging_view_root,
                final_view_root,
                verified_by_sha,
            )
            staged.append({**spec, "files": files})

        created: list[dict[str, Any]] = []
        for spec in staged:
            final_view_root = spec["final"]
            staging_view_root = spec["staging"]
            if final_view_root.exists() or final_view_root.is_symlink():
                raise ModelLifecycleError(f"runtime view root appeared before publication: {final_view_root}")
            staging_view_root.replace(final_view_root)
            published_roots.append(final_view_root)
            created.append(
                {
                    "runtime": spec["runtime"],
                    "host_path": str(final_view_root),
                    "container_path": runtime_manifest_container_path(spec["runtime"], manifest),
                    "files": spec["files"],
                }
            )
        return created
    except Exception:
        cleanup_errors: list[str] = []
        for published_root in reversed(published_roots):
            try:
                _remove_published_runtime_view(published_root, root)
            except ModelLifecycleError as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
        for spec in reversed(view_specs):
            try:
                _remove_staged_runtime_view(spec["staging"], root)
            except ModelLifecycleError as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
        if cleanup_errors:
            raise ModelLifecycleError(f"runtime view publication failed and cleanup was incomplete: {'; '.join(cleanup_errors)}")
        raise


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


def quarantine_set_timestamp(name: str) -> datetime | None:
    match = QUARANTINE_SET_RE.fullmatch(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (FileNotFoundError, ValueError):
        return False


def _invalid_quarantine_entry(path: Path, reason: str) -> dict[str, Any]:
    return {"path": str(path), "reason": reason}


def _quarantine_set_summary(set_dir: Path, root: Path, cutoff: datetime) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    if not _path_inside(set_dir, root):
        return None, None, _invalid_quarantine_entry(set_dir, "path escapes blob quarantine root")
    if set_dir.is_symlink():
        return None, None, _invalid_quarantine_entry(set_dir, "quarantine set is a symlink")
    if not set_dir.is_dir():
        return None, None, _invalid_quarantine_entry(set_dir, "quarantine set is not a directory")
    try:
        safe_component(set_dir.name, "quarantine set directory")
    except ModelLifecycleError as exc:
        return None, None, _invalid_quarantine_entry(set_dir, str(exc))
    timestamp = quarantine_set_timestamp(set_dir.name)
    if timestamp is None:
        return None, None, _invalid_quarantine_entry(set_dir, "quarantine set name does not include a valid timestamp")
    version = QUARANTINE_SET_RE.fullmatch(set_dir.name).group("version")  # type: ignore[union-attr]
    invalid_children: list[str] = []
    blob_count = 0
    total_size = 0
    for child in sorted(set_dir.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            invalid_children.append(f"{child.name}: symlink")
            continue
        if not child.is_file():
            invalid_children.append(f"{child.name}: not a regular file")
            continue
        if not SHA256_RE.fullmatch(child.name):
            invalid_children.append(f"{child.name}: not a sha256 blob name")
            continue
        blob_count += 1
        total_size += child.stat().st_size
    if invalid_children:
        return None, None, _invalid_quarantine_entry(set_dir, "; ".join(invalid_children))
    model_id = set_dir.parent.name
    record = {
        "model_id": model_id,
        "version": version,
        "quarantine_set": set_dir.name,
        "path": str(set_dir),
        "created_at": timestamp.isoformat(),
        "blob_count": blob_count,
        "size_bytes": total_size,
    }
    if timestamp >= cutoff:
        return None, {**record, "reason": "newer than retention cutoff"}, None
    return {**record, "reason": "older than retention cutoff"}, None, None


def build_blob_quarantine_retention_plan(
    data_root: Path,
    *,
    delete_older_than_days: int,
    now: datetime | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    current = now or datetime.now(tz=UTC)
    cutoff = current - timedelta(days=delete_older_than_days)
    root = blob_quarantine_root(data_root)
    policy = {
        "delete_older_than_days": delete_older_than_days,
        "cutoff": cutoff.isoformat(),
        "limit": limit,
    }
    if not root.exists():
        return {
            "status": "planned",
            "policy": policy,
            "root": str(root),
            "candidate_count": 0,
            "kept_count": 0,
            "invalid_preserved_count": 0,
            "total_reclaimable_bytes": 0,
            "candidates": [],
            "kept": [],
            "invalid_preserved": [],
            "truncated": False,
        }
    if root.is_symlink() or not root.is_dir():
        raise ModelLifecycleError(f"blob quarantine root is unsafe: {root}")

    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    scanned_sets = 0
    truncated = False
    root_resolved = root.resolve(strict=True)
    for model_dir in sorted(root.iterdir(), key=lambda item: item.name):
        if model_dir.is_symlink():
            invalid.append(_invalid_quarantine_entry(model_dir, "model quarantine directory is a symlink"))
            continue
        if not model_dir.is_dir():
            invalid.append(_invalid_quarantine_entry(model_dir, "model quarantine entry is not a directory"))
            continue
        try:
            safe_component(model_dir.name, "model quarantine directory")
        except ModelLifecycleError as exc:
            invalid.append(_invalid_quarantine_entry(model_dir, str(exc)))
            continue
        for set_dir in sorted(model_dir.iterdir(), key=lambda item: item.name):
            if scanned_sets >= limit:
                truncated = True
                break
            scanned_sets += 1
            candidate, keep, invalid_entry = _quarantine_set_summary(set_dir, root_resolved, cutoff)
            if candidate is not None:
                candidates.append(candidate)
            if keep is not None:
                kept.append(keep)
            if invalid_entry is not None:
                invalid.append(invalid_entry)
        if truncated:
            break
    return {
        "status": "planned",
        "policy": policy,
        "root": str(root),
        "candidate_count": len(candidates),
        "kept_count": len(kept),
        "invalid_preserved_count": len(invalid),
        "total_reclaimable_bytes": sum(int(candidate["size_bytes"]) for candidate in candidates),
        "candidates": candidates,
        "kept": kept,
        "invalid_preserved": invalid,
        "truncated": truncated,
    }


def _delete_quarantine_set(path: Path, root: Path) -> dict[str, Any]:
    root_resolved = root.resolve(strict=True)
    set_resolved = path.resolve(strict=True)
    if not _path_inside(set_resolved, root_resolved):
        raise ModelLifecycleError(f"quarantine cleanup path escapes root: {path}")
    if path.is_symlink() or not path.is_dir():
        raise ModelLifecycleError(f"quarantine cleanup path is unsafe: {path}")
    for child in path.iterdir():
        if child.is_symlink() or not child.is_file() or not SHA256_RE.fullmatch(child.name):
            raise ModelLifecycleError(f"quarantine cleanup path contains unsafe entry: {child}")
    shutil.rmtree(path)
    with suppress(OSError):
        parent = path.parent
        parent_resolved = parent.resolve(strict=True)
        if parent_resolved != root_resolved and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    return {"path": str(path), "status": "deleted"}


def apply_blob_quarantine_retention_plan(
    data_root: Path,
    *,
    delete_older_than_days: int,
    confirmed: bool,
    now: datetime | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    if not confirmed:
        raise ModelLifecycleError("model quarantine cleanup requires explicit confirmation")
    plan = build_blob_quarantine_retention_plan(
        data_root,
        delete_older_than_days=delete_older_than_days,
        now=now,
        limit=limit,
    )
    root = Path(plan["root"])
    deleted: list[dict[str, Any]] = []
    for candidate in plan["candidates"]:
        deleted.append(
            {
                **candidate,
                **_delete_quarantine_set(Path(candidate["path"]), root),
            }
        )
    return {
        **plan,
        "status": "cleaned",
        "deleted": deleted,
        "deleted_count": len(deleted),
    }


def source_url_allowed(manifest: ModelManifest) -> bool:
    if manifest.source.type == "upload":
        return True
    if manifest.source.type == "huggingface":
        try:
            huggingface_repo_source(manifest.source.url, manifest.source.revision)
        except ModelLifecycleError:
            return False
        return True
    parsed = urlparse(manifest.source.url)
    if parsed.username or parsed.password:
        return False
    if has_credential_query_parameter(parsed.query):
        return False
    return is_safe_public_import_url(manifest.source.url)


def downloadable_manifest_files(manifest: ModelManifest) -> list[Any]:
    if manifest.source.type not in {"direct-url", "huggingface"}:
        raise ModelLifecycleError("download worker currently supports only direct-url and huggingface manifests")
    return list(manifest.files)


def direct_download_source_url(manifest: ModelManifest, file: Any) -> str:
    if len(manifest.files) == 1:
        return manifest.source.url
    parsed = urlparse(manifest.source.url)
    if not parsed.path.endswith("/"):
        raise ModelLifecycleError("multi-file direct-url manifests require source.url to end with /")
    if parsed.query or parsed.fragment:
        raise ModelLifecycleError("multi-file direct-url manifests cannot use query strings or fragments on source.url")
    source_path = getattr(file, "source_path", None) or file.path
    encoded_path = "/".join(quote(part, safe="") for part in safe_relative_parts(source_path, f"direct-url source file path: {source_path}"))
    return urlunparse((parsed.scheme, parsed.netloc, f"{parsed.path}{encoded_path}", "", "", ""))


def _validate_huggingface_component(value: str, context: str) -> str:
    if not HUGGINGFACE_REPO_COMPONENT_RE.match(value):
        raise ModelLifecycleError(f"{context} has invalid Hugging Face repository component: {value}")
    if value.endswith(".git"):
        raise ModelLifecycleError(f"{context} must not end with .git")
    if "--" in value or ".." in value:
        raise ModelLifecycleError(f"{context} must not contain repeated separators")
    return value


def _validate_huggingface_revision(value: str) -> str:
    if not HUGGINGFACE_REVISION_RE.match(value):
        raise ModelLifecycleError("Hugging Face manifests require a simple branch, tag, or commit revision")
    if value in {".", ".."}:
        raise ModelLifecycleError("Hugging Face revision is unsafe")
    return value


def huggingface_repo_source(source_url: str, revision: str) -> dict[str, str]:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or hostname not in HUGGINGFACE_HOSTS:
        raise ModelLifecycleError("huggingface source must use https://huggingface.co")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelLifecycleError("huggingface source URL must not contain credentials, query, or fragment")
    segments = [part for part in parsed.path.split("/") if part]
    if not segments:
        raise ModelLifecycleError("huggingface source URL must include a repository")

    repo_type = "model"
    prefix: list[str] = []
    if segments[0] in {"datasets", "spaces"}:
        repo_type = segments[0][:-1]
        prefix = [segments[0]]
        segments = segments[1:]
    elif segments[0] == "models":
        segments = segments[1:]

    marker_index = next((index for index, segment in enumerate(segments) if segment in HUGGINGFACE_REPO_MARKERS), len(segments))
    repo_parts = segments[:marker_index]
    if not repo_parts or len(repo_parts) > 2:
        raise ModelLifecycleError("huggingface source URL must identify a model, dataset, or space repository")
    for index, part in enumerate(repo_parts):
        _validate_huggingface_component(part, f"huggingface repository component {index + 1}")
    validated_revision = _validate_huggingface_revision(revision)
    if marker_index < len(segments):
        marker = segments[marker_index]
        if marker != "tree":
            raise ModelLifecycleError("huggingface source URL must identify a repository, not a file")
        marker_tail = segments[marker_index + 1 :]
        if len(marker_tail) > 1:
            raise ModelLifecycleError("huggingface source URL tree path must not include file paths")
        if marker_tail and _validate_huggingface_revision(marker_tail[0]) != validated_revision:
            raise ModelLifecycleError("huggingface source URL revision conflicts with source.revision")
    repo_path = "/".join([*prefix, *repo_parts])
    return {"host": hostname, "repo_type": repo_type, "repo_id": "/".join(repo_parts), "repo_path": repo_path, "revision": validated_revision}


def _decode_huggingface_segment(value: str, context: str) -> str:
    if BAD_PERCENT_ESCAPE_RE.search(value):
        raise ModelLifecycleError(f"{context} uses an unsafe encoded segment")
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeDecodeError as exc:
        raise ModelLifecycleError(f"{context} uses an unsafe encoded segment") from exc
    if not decoded or decoded in {".", ".."}:
        raise ModelLifecycleError(f"{context} uses an unsafe segment")
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise ModelLifecycleError(f"{context} uses an unsafe segment")
    return decoded


def huggingface_file_source(source_url: str) -> dict[str, str]:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or hostname not in HUGGINGFACE_HOSTS:
        raise ModelLifecycleError("Hugging Face file URL must use https://huggingface.co")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelLifecycleError("Hugging Face file URL must not contain credentials, query, or fragment")
    raw_segments = [part for part in parsed.path.split("/") if part]
    segments = [_decode_huggingface_segment(part, f"Hugging Face URL segment {index + 1}") for index, part in enumerate(raw_segments)]
    if not segments:
        raise ModelLifecycleError("Hugging Face file URL must include a repository and file")
    if segments[0] in {"datasets", "spaces"}:
        raise ModelLifecycleError("Hugging Face GGUF draft helper supports only model repositories")
    if segments[0] == "models":
        segments = segments[1:]

    marker_index = next((index for index, segment in enumerate(segments) if segment in HUGGINGFACE_FILE_MARKERS), -1)
    if marker_index < 1:
        raise ModelLifecycleError("Hugging Face file URL must include /resolve/{revision}/ or /blob/{revision}/")
    repo_parts = segments[:marker_index]
    if len(repo_parts) > 2:
        raise ModelLifecycleError("Hugging Face file URL must identify a model repository")
    for index, part in enumerate(repo_parts):
        _validate_huggingface_component(part, f"huggingface repository component {index + 1}")
    marker = segments[marker_index]
    tail = segments[marker_index + 1 :]
    if len(tail) < 2:
        raise ModelLifecycleError("Hugging Face file URL must include a revision and file path")
    revision = _validate_huggingface_revision(tail[0])
    file_path = "/".join(safe_relative_parts("/".join(tail[1:]), "Hugging Face file path"))
    if not file_path.lower().endswith(".gguf"):
        raise ModelLifecycleError("Hugging Face GGUF draft helper accepts only .gguf files")
    repo_id = "/".join(repo_parts)
    repo_path = repo_id
    repo_url = f"https://huggingface.co/{repo_path}"
    encoded_file_path = "/".join(quote(part, safe="") for part in safe_relative_parts(file_path, f"Hugging Face source file path: {file_path}"))
    encoded_revision = quote(revision, safe="")
    resolve_url = f"https://huggingface.co/{repo_path}/resolve/{encoded_revision}/{encoded_file_path}"
    return {
        "host": hostname,
        "repo_type": "model",
        "repo_id": repo_id,
        "repo_path": repo_path,
        "repo_url": repo_url,
        "revision": revision,
        "file_path": file_path,
        "filename": PurePosixPath(file_path).name,
        "url_kind": marker,
        "resolve_url": resolve_url,
    }


def huggingface_download_source_url(manifest: ModelManifest, file: Any) -> str:
    source = huggingface_repo_source(manifest.source.url, manifest.source.revision)
    source_path = getattr(file, "source_path", None) or file.path
    encoded_file_path = "/".join(quote(part, safe="") for part in safe_relative_parts(source_path, f"Hugging Face source file path: {source_path}"))
    encoded_revision = quote(source["revision"], safe="")
    return f"https://huggingface.co/{source['repo_path']}/resolve/{encoded_revision}/{encoded_file_path}"


def download_source_url(manifest: ModelManifest, file: Any) -> str:
    if manifest.source.type == "direct-url":
        return direct_download_source_url(manifest, file)
    if manifest.source.type == "huggingface":
        return huggingface_download_source_url(manifest, file)
    raise ModelLifecycleError("download worker currently supports only direct-url and huggingface manifests")


def _download_hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _is_huggingface_download_host(hostname: str) -> bool:
    return (
        hostname in HUGGINGFACE_HOSTS
        or hostname in HUGGINGFACE_CDN_HOSTS
        or any(hostname.endswith(suffix) for suffix in HUGGINGFACE_CDN_SUFFIXES)
    )


def redirect_url_allowed(source_url: str, current_url: str, location: str, *, source_type: str) -> str:
    if not location:
        raise ModelLifecycleError("download redirect is missing a Location header")
    redirected = urljoin(current_url, location)
    return download_request_url_allowed(source_url, redirected, source_type=source_type)


def download_request_url_allowed(source_url: str, request_url: str, *, source_type: str) -> str:
    parsed = urlparse(request_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ModelLifecycleError("download request target is not a public HTTPS URL")
    if parsed.username or parsed.password:
        raise ModelLifecycleError("download request target contains credentials")
    hostname = _download_hostname(request_url)
    if not is_safe_public_import_url(request_url):
        raise ModelLifecycleError("download request target is not allowed by import policy")
    if source_type == "huggingface":
        if not _is_huggingface_download_host(hostname):
            raise ModelLifecycleError("huggingface download redirected to an unapproved host")
        return request_url
    source_hostname = _download_hostname(source_url)
    if hostname != source_hostname:
        raise ModelLifecycleError("download request target host is not the original source host")
    return request_url


def send_download_authorization(source_url: str, request_url: str) -> bool:
    return _download_hostname(source_url) == _download_hostname(request_url)


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
        "source_type": manifest.source.type,
        "source_url": download_source_url(manifest, file),
        "target_sha256": file.sha256,
        "target_size_bytes": file.size_bytes,
        "target_path": str(target_path),
        "partial_path": str(partial_path),
        "existing_partial_bytes": existing_partial_bytes,
        "already_available": already_available,
        "blockers": blockers,
        "verification": verification,
    }


def build_download_plan(
    manifest: ModelManifest,
    data_root: Path,
    *,
    accept_license: bool = False,
    policy: ResourcePolicy | None = None,
    known_aliases: set[str] | None = None,
    model_profiles: list[ModelProfile] | None = None,
    allow_resource_override: bool = False,
    allow_download_override: bool = False,
) -> dict[str, Any]:
    files = downloadable_manifest_files(manifest)
    effective_policy = policy or ResourcePolicy()
    estimate = manifest.resource_estimate.to_scheduler_estimate(requires_gpu=manifest.preferred_runtime != "audio-cpu")
    decision = classify_resource_fit(effective_policy, estimate)
    source_allowed = source_url_allowed(manifest)
    verification_by_path = {item["path"]: item for item in verify_manifest_files(manifest, data_root)}
    requires_license_acceptance = bool(manifest.license.acceptance_required)
    unknown_aliases = manifest_aliases_are_known(manifest, known_aliases) if known_aliases is not None else []
    profile_compatibility = profile_compatibility_for_manifest(
        manifest,
        model_profiles or [],
        effective_policy,
        allow_resource_override=allow_resource_override or allow_download_override,
    )
    profile_blockers = profile_blockers_for_reports(profile_compatibility)
    profile_warnings = profile_warnings_for_reports(profile_compatibility)
    resource_allowed = bool(decision.accepted or allow_resource_override or allow_download_override)
    blockers: list[str] = []
    warnings: list[str] = list(profile_warnings)
    if not source_allowed:
        blockers.append("source URL is not allowed by import policy")
    if unknown_aliases:
        message = f"manifest aliases are not defined in the public alias seed: {', '.join(unknown_aliases)}"
        if allow_download_override:
            warnings.append(message)
        else:
            blockers.append(message)
    if profile_blockers:
        if allow_download_override:
            warnings.extend(profile_blockers)
        else:
            blockers.extend(profile_blockers)
    if policy is not None and not resource_allowed:
        blockers.append(decision.reason)
    elif policy is not None and allow_download_override and not decision.accepted:
        warnings.append(decision.reason)
    if requires_license_acceptance and not accept_license:
        blockers.append("licence acceptance is required")
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
    can_download = bool(source_allowed and not blockers)
    return {
        "model": manifest.to_dict(),
        "model_ref": f"{manifest.id}@{manifest.version}",
        "status": "available" if already_available and can_download else "downloadable" if can_download else "blocked",
        "can_download": can_download,
        "already_available": already_available,
        "blockers": blockers,
        "warnings": warnings,
        "requires_license_acceptance": requires_license_acceptance,
        "license_accepted": accept_license,
        "source_url": manifest.source.url,
        "resource_decision": asdict(decision),
        "resource_override": allow_resource_override,
        "download_override": allow_download_override,
        "profile_compatibility": profile_compatibility,
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


def profile_resource_overages(manifest: ModelManifest, profile: ModelProfile, tolerance: float = PROFILE_RESOURCE_TOLERANCE) -> list[str]:
    overages: list[str] = []
    manifest_estimate = manifest.resource_estimate
    profile_estimate = profile.resource_estimate
    checks = [
        ("VRAM", manifest_estimate.vram_gib, profile_estimate.vram_gib, "GiB"),
        ("RAM", manifest_estimate.ram_gib, profile_estimate.ram_gib, "GiB"),
        ("disk", manifest_estimate.disk_gib, profile_estimate.disk_gib, "GiB"),
    ]
    for label, value, limit, unit in checks:
        if limit > 0 and value > limit * tolerance:
            overages.append(f"{label} estimate {value:g} {unit} exceeds profile envelope {limit:g} {unit}")
    return overages


def profile_compatibility_for_manifest(
    manifest: ModelManifest,
    profiles: list[ModelProfile],
    policy: ResourcePolicy,
    *,
    allow_resource_override: bool = False,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    manifest_aliases = set(manifest.aliases)
    estimate = manifest.resource_estimate.to_scheduler_estimate(requires_gpu=manifest.preferred_runtime != "audio-cpu")
    decision = classify_resource_fit(policy, estimate)
    manifest_label_score = PROFILE_LABEL_SCORE.get(decision.label, 99)
    for profile in profiles:
        matched_aliases = sorted(manifest_aliases.intersection(profile.aliases))
        if not matched_aliases:
            continue
        blockers: list[str] = []
        warnings: list[str] = []
        if manifest.modality != profile.modality:
            blockers.append(f"manifest modality {manifest.modality} does not match profile modality {profile.modality}")
        if manifest.preferred_runtime not in set(profile.preferred_runtimes):
            blockers.append(
                f"manifest preferred runtime {manifest.preferred_runtime} is not allowed by profile runtimes: {', '.join(profile.preferred_runtimes)}"
            )
        supported_profile_operations = [
            operation
            for operation in profile.operations
            if operation_is_supported(operation, manifest.operations, manifest.modality)
        ]
        unsupported_manifest_operations = [
            operation
            for operation in manifest.operations
            if not operation_is_supported(operation, profile.operations, manifest.modality)
        ]
        if not supported_profile_operations:
            blockers.append(f"manifest operations do not satisfy profile operations: {', '.join(profile.operations)}")
        elif len(supported_profile_operations) < len(profile.operations):
            warnings.append(
                "manifest supports only part of the profile operation set: "
                + ", ".join(supported_profile_operations)
            )
        if unsupported_manifest_operations:
            blockers.append(
                "manifest declares operations outside the profile operation set: "
                + ", ".join(sorted(unsupported_manifest_operations))
            )
        target_score = PROFILE_LABEL_SCORE.get(profile.target_resource_label, 99)
        if manifest_label_score > target_score:
            message = f"resource label {decision.label} exceeds profile target {profile.target_resource_label}: {decision.reason}"
            if allow_resource_override:
                warnings.append(message)
            else:
                blockers.append(message)
        overages = profile_resource_overages(manifest, profile)
        if overages:
            if allow_resource_override:
                warnings.extend(overages)
            else:
                blockers.extend(overages)
        reports.append(
            {
                "profile_id": profile.id,
                "display_name": profile.display_name,
                "target_class": profile.target_class,
                "aliases": list(profile.aliases),
                "matched_aliases": matched_aliases,
                "preferred_runtimes": list(profile.preferred_runtimes),
                "runtime_policy": profile.runtime_policy,
                "target_resource_label": profile.target_resource_label,
                "resource_label": decision.label,
                "status": "blocked" if blockers else "compatible",
                "blockers": blockers,
                "warnings": warnings,
            }
        )
    return reports


def profile_blockers_for_reports(reports: list[dict[str, Any]]) -> list[str]:
    return [
        f"profile {report['profile_id']}: {blocker}"
        for report in reports
        for blocker in report["blockers"]
    ]


def profile_warnings_for_reports(reports: list[dict[str, Any]]) -> list[str]:
    return [
        f"profile {report['profile_id']}: {warning}"
        for report in reports
        for warning in report["warnings"]
    ]


def build_install_plan(
    manifest: ModelManifest,
    data_root: Path,
    policy: ResourcePolicy,
    *,
    known_aliases: set[str],
    model_profiles: list[ModelProfile] | None = None,
    allow_resource_override: bool = False,
    accept_license: bool = False,
) -> dict[str, Any]:
    estimate = manifest.resource_estimate.to_scheduler_estimate(requires_gpu=manifest.preferred_runtime != "audio-cpu")
    decision = classify_resource_fit(policy, estimate)
    file_status = verify_manifest_files(manifest, data_root)
    archive_inspections, archive_blockers = inspect_manifest_archive_files(manifest, data_root, file_status)
    source_allowed = source_url_allowed(manifest)
    unknown_aliases = manifest_aliases_are_known(manifest, known_aliases)
    profile_compatibility = profile_compatibility_for_manifest(
        manifest,
        model_profiles or [],
        policy,
        allow_resource_override=allow_resource_override,
    )
    profile_blockers = profile_blockers_for_reports(profile_compatibility)
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
        and not profile_blockers
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
    blockers.extend(profile_blockers)
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
        "profile_compatibility": profile_compatibility,
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
