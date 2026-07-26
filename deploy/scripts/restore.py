#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.private_files import PrivateFileError, write_private_json  # noqa: E402

try:  # pragma: no cover - exercised in dependency-complete environments
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError:  # pragma: no cover - keeps basic restore importable on minimal hosts
    InvalidTag = ValueError  # type: ignore[assignment]
    AESGCM = None  # type: ignore[assignment]


class RestoreError(RuntimeError):
    pass


WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:$")
BACKUP_ENCRYPTION_SCHEME = "b1-backup-aesgcm-chunks-sha256/v1"
BACKUP_ENCRYPTION_MAGIC = b"B1AIHUB-BACKUP-AESGCM\n"
BACKUP_ENCRYPTION_KEY_MIN_BYTES = 32


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_key_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def default_backup_encryption_key() -> str:
    return (
        read_key_file(os.getenv("B1_BACKUP_ENCRYPTION_KEY_FILE"))
        or os.getenv("B1_BACKUP_ENCRYPTION_KEY", "").strip()
        or read_key_file(os.getenv("B1_MASTER_KEY_FILE"))
        or os.getenv("B1_MASTER_KEY", "").strip()
    )


def b64decode(value: str, label: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise RestoreError(f"{label} contains invalid base64") from exc


def require_crypto() -> None:
    if AESGCM is None:
        raise RestoreError("cryptography is required to restore an encrypted backup archive")


def backup_key_material(backup_encryption_key: str) -> bytes:
    raw = backup_encryption_key.strip().encode("utf-8")
    if len(raw) < BACKUP_ENCRYPTION_KEY_MIN_BYTES:
        raise RestoreError(f"backup encryption key must be at least {BACKUP_ENCRYPTION_KEY_MIN_BYTES} bytes")
    return hashlib.sha256(raw).digest()


def backup_key_fingerprint(backup_encryption_key: str) -> str:
    raw = backup_encryption_key.strip().encode("utf-8")
    if len(raw) < BACKUP_ENCRYPTION_KEY_MIN_BYTES:
        raise RestoreError(f"backup encryption key must be at least {BACKUP_ENCRYPTION_KEY_MIN_BYTES} bytes")
    return hashlib.sha256(raw).hexdigest()[:16]


def backup_encryption_aad(manifest: dict[str, Any], chunk_index: int) -> bytes:
    archive = manifest.get("archive") or {}
    return f"{BACKUP_ENCRYPTION_SCHEME}:{manifest.get('name', '')}:{archive.get('sha256', '')}:{chunk_index}".encode("utf-8")


def decrypt_archive_to_path(encrypted_path: Path, output_path: Path, manifest: dict[str, Any], backup_encryption_key: str) -> None:
    require_crypto()
    encryption = manifest.get("archive_encryption") or {}
    if encryption.get("scheme") != BACKUP_ENCRYPTION_SCHEME:
        raise RestoreError("unsupported backup archive encryption scheme")
    if backup_key_fingerprint(backup_encryption_key) != encryption.get("key_id"):
        raise RestoreError("backup archive was encrypted with a different key")
    if not encrypted_path.is_file():
        raise RestoreError(f"missing encrypted archive: {encrypted_path}")
    if sha256_file(encrypted_path) != encryption.get("sha256"):
        raise RestoreError("encrypted archive checksum mismatch")
    base_nonce = b64decode(str(encryption.get("base_nonce", "")), "backup archive nonce")
    if len(base_nonce) != 8:
        raise RestoreError("backup archive nonce has invalid length")
    key = backup_key_material(backup_encryption_key)
    tmp_path = output_path.with_name(output_path.name + ".partial")
    if tmp_path.exists():
        tmp_path.unlink()
    expected_chunk_count = int(encryption.get("chunk_count") or 0)
    chunk_index = 0
    try:
        with encrypted_path.open("rb") as source, tmp_path.open("wb") as output:
            if source.readline() != BACKUP_ENCRYPTION_MAGIC:
                raise RestoreError("encrypted archive has invalid magic")
            try:
                header = json.loads(source.readline().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RestoreError("encrypted archive header is invalid") from exc
            if header.get("scheme") != BACKUP_ENCRYPTION_SCHEME:
                raise RestoreError("encrypted archive header has unsupported scheme")
            while True:
                length_bytes = source.read(4)
                if not length_bytes:
                    break
                if len(length_bytes) != 4:
                    raise RestoreError("encrypted archive has a truncated chunk header")
                ciphertext_length = int.from_bytes(length_bytes, "big")
                if ciphertext_length < 17 or ciphertext_length > int(encryption.get("chunk_size") or 0) + 16:
                    raise RestoreError("encrypted archive chunk length is invalid")
                ciphertext = source.read(ciphertext_length)
                if len(ciphertext) != ciphertext_length:
                    raise RestoreError("encrypted archive has a truncated chunk")
                nonce = base_nonce + chunk_index.to_bytes(4, "big")
                try:
                    plaintext = AESGCM(key).decrypt(nonce, ciphertext, backup_encryption_aad(manifest, chunk_index))
                except (InvalidTag, ValueError) as exc:
                    raise RestoreError("encrypted archive could not be decrypted") from exc
                output.write(plaintext)
                chunk_index += 1
        if chunk_index != expected_chunk_count:
            raise RestoreError("encrypted archive chunk count mismatch")
        tmp_path.chmod(0o600)
        tmp_path.replace(output_path)
        output_path.chmod(0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def load_manifest(backup_dir: Path) -> dict[str, Any]:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RestoreError(f"missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "b1-ai-hub-backup/v1":
        raise RestoreError("unsupported backup format")
    return manifest


def safe_member_relative_path(member_name: str) -> PurePosixPath:
    if "\x00" in member_name:
        raise RestoreError("archive member contains NUL byte")
    normalized = member_name.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or not relative.parts:
        raise RestoreError(f"archive member escapes target: {member_name}")
    if WINDOWS_DRIVE_RE.match(relative.parts[0]):
        raise RestoreError(f"archive member escapes target: {member_name}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise RestoreError(f"archive member escapes target: {member_name}")
    return relative


def safe_member_target(member_name: str, target: Path) -> Path:
    relative = safe_member_relative_path(member_name)
    target_root = target.resolve()
    return target_root.joinpath(*relative.parts)


def validate_archive_member_tree(member_path: str, kind: str, seen_paths: dict[str, str]) -> None:
    previous_kind = seen_paths.get(member_path)
    if previous_kind == kind:
        return
    if previous_kind == "file":
        raise RestoreError(f"archive directory member conflicts with existing file member: {member_path}")
    if previous_kind == "directory" and kind == "file":
        raise RestoreError(f"archive file member conflicts with existing directory member: {member_path}")

    relative = PurePosixPath(member_path)
    for parent in relative.parents:
        if parent == PurePosixPath("."):
            continue
        if seen_paths.get(parent.as_posix()) == "file":
            raise RestoreError(f"archive member path is below a file member: {member_path}")

    if kind == "file":
        child_prefix = f"{member_path}/"
        for existing_path in seen_paths:
            if existing_path.startswith(child_prefix):
                raise RestoreError(f"archive file member conflicts with existing child member: {member_path}")

    seen_paths[member_path] = kind


def ensure_restore_directory(path: Path, target: Path) -> None:
    if target.is_symlink():
        raise RestoreError(f"restore target is a symlink: {target}")
    target.mkdir(parents=True, exist_ok=True)
    target_root = target.resolve(strict=True)
    candidate = path if path.is_absolute() else target_root / path
    try:
        relative_parts = candidate.relative_to(target_root).parts
    except ValueError as exc:
        raise RestoreError(f"restore path escapes target: {path}") from exc
    current = target_root
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise RestoreError(f"restore path contains symlink: {current}")
        if current.exists() and not current.is_dir():
            raise RestoreError(f"restore path is not a directory: {current}")
        current.mkdir(exist_ok=True)


def restore_file_mode(member_name: str, manifest: dict[str, Any]) -> int:
    expected = {item["path"]: item for item in manifest.get("files", [])}
    record = expected.get(safe_member_relative_path(member_name).as_posix(), {})
    return 0o600 if record.get("sensitive") else 0o644


def verify_archive_members(archive_path: Path, manifest: dict[str, Any]) -> None:
    expected = {item["path"]: item for item in manifest.get("files", [])}
    seen: set[str] = set()
    seen_paths: dict[str, str] = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            member_path = safe_member_relative_path(member.name).as_posix()
            if member.isdir():
                validate_archive_member_tree(member_path, "directory", seen_paths)
                continue
            if not member.isfile():
                raise RestoreError(f"unsupported archive member type: {member.name}")
            safe_member_target(member.name, Path("/tmp/b1-archive-verify"))
            validate_archive_member_tree(member_path, "file", seen_paths)
            if member_path in seen:
                raise RestoreError(f"archive contains duplicate file: {member.name}")
            if member_path not in expected:
                raise RestoreError(f"archive contains unmanifested file: {member.name}")
            if member.size != expected[member_path]["size_bytes"]:
                raise RestoreError(f"archive member size mismatch: {member.name}")
            handle = archive.extractfile(member)
            if handle is None:
                raise RestoreError(f"cannot read archive member: {member.name}")
            digest = hashlib.sha256()
            with handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected[member_path]["sha256"]:
                raise RestoreError(f"archive member checksum mismatch: {member.name}")
            seen.add(member_path)
    missing = sorted(set(expected) - seen)
    if missing:
        raise RestoreError(f"archive missing manifest files: {', '.join(missing[:5])}")


def extract_verified_archive(archive_path: Path, target: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if target.is_symlink():
        raise RestoreError(f"restore target is a symlink: {target}")
    target.mkdir(parents=True, exist_ok=True)
    expected = {item["path"]: item for item in manifest.get("files", [])}
    seen: set[str] = set()
    seen_paths: dict[str, str] = {}
    extracted: list[dict[str, Any]] = []
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            member_path = safe_member_relative_path(member.name).as_posix()
            destination = safe_member_target(member.name, target)
            if member.isdir():
                validate_archive_member_tree(member_path, "directory", seen_paths)
                ensure_restore_directory(destination, target)
                destination.chmod(0o755)
                continue
            if not member.isfile():
                raise RestoreError(f"unsupported archive member type: {member.name}")
            validate_archive_member_tree(member_path, "file", seen_paths)
            if member_path in seen:
                raise RestoreError(f"archive contains duplicate file: {member.name}")
            if member_path not in expected:
                raise RestoreError(f"archive contains unmanifested file: {member.name}")
            if member.size != expected[member_path]["size_bytes"]:
                raise RestoreError(f"archive member size mismatch: {member.name}")
            ensure_restore_directory(destination.parent, target)
            if destination.is_symlink():
                raise RestoreError(f"restore destination is a symlink: {destination}")
            if destination.exists() and not destination.is_file():
                raise RestoreError(f"restore destination is not a regular file: {destination}")
            source = archive.extractfile(member)
            if source is None:
                raise RestoreError(f"cannot read archive member: {member.name}")
            bytes_written = 0
            with source, destination.open("wb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    bytes_written += len(chunk)
                    if bytes_written > member.size:
                        raise RestoreError(f"archive member exceeded manifest size while restoring: {member.name}")
                    output.write(chunk)
            if bytes_written != member.size:
                raise RestoreError(f"archive member size changed while restoring: {member.name}")
            destination.chmod(restore_file_mode(member.name, manifest))
            extracted.append({"path": member_path, "size_bytes": bytes_written})
            seen.add(member_path)
    return extracted


@contextmanager
def readable_archive_path(backup_dir: Path, manifest: dict[str, Any], backup_encryption_key: str | None = None) -> Any:
    archive_path = backup_dir / manifest["archive"]["file"]
    if not archive_path.is_file():
        encryption = manifest.get("archive_encryption") or {}
        if not encryption:
            raise RestoreError(f"missing archive: {archive_path}")
        key = backup_encryption_key or default_backup_encryption_key()
        if not key:
            raise RestoreError("backup archive is encrypted and requires --backup-encryption-key-file")
        encrypted_path = backup_dir / str(encryption.get("file") or "")
        handle = tempfile.NamedTemporaryFile(prefix="b1-backup-archive-", suffix=".tar.gz", delete=False)
        decrypted_path = Path(handle.name)
        handle.close()
        try:
            decrypt_archive_to_path(encrypted_path, decrypted_path, manifest, key)
            if sha256_file(decrypted_path) != manifest["archive"]["sha256"]:
                raise RestoreError("decrypted archive checksum mismatch")
            yield decrypted_path
        finally:
            if decrypted_path.exists():
                decrypted_path.unlink()
        return
    if sha256_file(archive_path) != manifest["archive"]["sha256"]:
        raise RestoreError("archive checksum mismatch")
    yield archive_path


def verify_archive(backup_dir: Path, manifest: dict[str, Any], backup_encryption_key: str | None = None) -> Path:
    with readable_archive_path(backup_dir, manifest, backup_encryption_key) as archive_path:
        verify_archive_members(archive_path, manifest)
        return archive_path


def verify_restored_files(target: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for item in manifest.get("files", []):
        path = target / item["path"]
        if not path.is_file():
            raise RestoreError(f"missing restored file: {item['path']}")
        size = path.stat().st_size
        digest = sha256_file(path)
        if size != item["size_bytes"] or digest != item["sha256"]:
            raise RestoreError(f"restored file verification failed: {item['path']}")
        verified.append({"path": item["path"], "size_bytes": size, "sha256": digest})
    return verified


def restore(backup_dir: Path, target: Path, force: bool = False, backup_encryption_key: str | None = None) -> dict[str, Any]:
    backup_dir = backup_dir.resolve()
    target = target.resolve()
    manifest = load_manifest(backup_dir)
    if target.exists() and any(target.iterdir()) and not force:
        raise RestoreError("target directory exists and is not empty; use --force only for an alternate test directory")
    target.mkdir(parents=True, exist_ok=True)
    with readable_archive_path(backup_dir, manifest, backup_encryption_key) as archive_path:
        verify_archive_members(archive_path, manifest)
        extract_verified_archive(archive_path, target, manifest)
    verified = verify_restored_files(target, manifest)
    report = {
        "status": "restored",
        "backup": str(backup_dir),
        "target": str(target),
        "files_verified": len(verified),
        "contains_sensitive_data": manifest.get("contains_sensitive_data", False),
        "postgres_dump_included": manifest.get("postgres_dump_included", False),
        "archive_encryption": manifest.get("archive_encryption"),
    }
    try:
        write_private_json(target / "restore-report.json", report, mode=0o600, label="restore report")
    except PrivateFileError as exc:
        raise RestoreError(str(exc)) from exc
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and restore a B1 AI Hub backup into an alternate directory.")
    parser.add_argument("--backup", required=True, help="Backup directory containing manifest.json and a plaintext or encrypted payload archive.")
    parser.add_argument("--target", required=True, help="Alternate restore directory.")
    parser.add_argument("--force", action="store_true", help="Allow restoring into a non-empty alternate directory.")
    parser.add_argument("--backup-encryption-key-file", default=None, help="Key file for encrypted-only payload archives. Defaults to B1_BACKUP_ENCRYPTION_KEY_FILE or B1_MASTER_KEY_FILE.")
    args = parser.parse_args()
    try:
        report = restore(Path(args.backup), Path(args.target), force=args.force, backup_encryption_key=read_key_file(args.backup_encryption_key_file) or None)
    except RestoreError as exc:
        raise SystemExit(f"restore failed: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
