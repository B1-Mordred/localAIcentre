from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit

try:  # pragma: no cover - covered in the dependency-complete container test environment
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError:  # pragma: no cover - lets lightweight stdlib tests import the module
    InvalidTag = ValueError  # type: ignore[assignment]
    AESGCM = None  # type: ignore[assignment]


BACKUP_FORMAT = "b1-ai-hub-backup/v1"
LOGICAL_POSTGRES_DUMP_FORMAT = "b1-ai-hub-postgres-logical-export/v1"
NATIVE_POSTGRES_DUMP_FORMAT = "postgresql-custom"
BACKUP_ENCRYPTION_SCHEME = "b1-backup-aesgcm-chunks-sha256/v1"
BACKUP_ENCRYPTION_CHUNK_SIZE = 4 * 1024 * 1024
BACKUP_ENCRYPTION_MAGIC = b"B1AIHUB-BACKUP-AESGCM\n"
BACKUP_ENCRYPTION_MODES = {"none", "copy", "encrypted-only"}
BACKUP_ENCRYPTION_KEY_MIN_BYTES = 32
DEFAULT_INCLUDE_PATHS = [
    "data/open-webui",
    "data/control-plane",
    "data/voicebox",
    "data/caddy",
    "models",
    "workflows",
    "artifacts",
    "secrets",
    "logs/control-plane",
    "logs/runtime-agent",
]
DEFAULT_EXCLUDES = [
    "models/blobs",
    "models/runtime-views",
    "models/quarantine/runtime-views",
    "cache",
    "backups",
    "restore-tests",
    "data/postgres",
    "data/redis",
]
SENSITIVE_PREFIXES = ("secrets/", "data/caddy/pki/", "data/control-plane/postgres-")
BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


class BackupError(RuntimeError):
    pass


class RestoreError(RuntimeError):
    pass


class BackupEncryptionError(BackupError):
    pass


@dataclass(frozen=True)
class PostgresDumpSpec:
    path: Path
    format: str
    kind: str
    tool: str | None = None
    verified: bool | None = None


@dataclass(frozen=True)
class BackupFile:
    path: str
    size_bytes: int
    sha256: str
    sensitive: bool = False


def utc_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")


def validate_backup_name(name: str) -> str:
    if not BACKUP_NAME_RE.fullmatch(name):
        raise BackupError("backup name must be 1-128 characters of letters, numbers, dot, dash, or underscore")
    return name


def ensure_under(root: Path, candidate: Path, label: str) -> Path:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve()
    if candidate_resolved != root_resolved and root_resolved not in candidate_resolved.parents:
        raise BackupError(f"{label} escapes B1 data root")
    return candidate_resolved


def resolve_under(root: Path, relative: str) -> Path:
    return ensure_under(root, root / relative.strip("/"), relative)


def is_excluded(relative: str, excludes: Iterable[str]) -> bool:
    normalized = relative.strip("/")
    return any(normalized == item.strip("/") or normalized.startswith(item.strip("/") + "/") for item in excludes)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_stream(handle: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64decode(value: str, label: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise BackupEncryptionError(f"{label} contains invalid base64") from exc


def validate_backup_encryption_mode(mode: str | None) -> str:
    normalized = (mode or "none").strip().lower()
    if normalized not in BACKUP_ENCRYPTION_MODES:
        raise BackupEncryptionError(f"backup encryption mode must be one of: {', '.join(sorted(BACKUP_ENCRYPTION_MODES))}")
    return normalized


def require_backup_crypto() -> None:
    if AESGCM is None:
        raise BackupEncryptionError("cryptography is not installed in the control-plane image")


def backup_key_material(backup_encryption_key: str) -> bytes:
    raw = backup_encryption_key.strip().encode("utf-8")
    if len(raw) < BACKUP_ENCRYPTION_KEY_MIN_BYTES:
        raise BackupEncryptionError(f"backup encryption key must be at least {BACKUP_ENCRYPTION_KEY_MIN_BYTES} bytes")
    return hashlib.sha256(raw).digest()


def backup_key_fingerprint(backup_encryption_key: str) -> str:
    raw = backup_encryption_key.strip().encode("utf-8")
    if len(raw) < BACKUP_ENCRYPTION_KEY_MIN_BYTES:
        raise BackupEncryptionError(f"backup encryption key must be at least {BACKUP_ENCRYPTION_KEY_MIN_BYTES} bytes")
    return hashlib.sha256(raw).hexdigest()[:16]


def backup_encryption_aad(manifest: dict[str, Any], chunk_index: int) -> bytes:
    archive = manifest.get("archive") or {}
    return f"{BACKUP_ENCRYPTION_SCHEME}:{manifest.get('name', '')}:{archive.get('sha256', '')}:{chunk_index}".encode("utf-8")


def encrypt_archive(
    archive_path: Path,
    encrypted_path: Path,
    manifest: dict[str, Any],
    backup_encryption_key: str,
    *,
    chunk_size: int = BACKUP_ENCRYPTION_CHUNK_SIZE,
) -> dict[str, Any]:
    require_backup_crypto()
    if chunk_size < 1024 * 1024:
        raise BackupEncryptionError("backup encryption chunk size must be at least 1 MiB")
    key = backup_key_material(backup_encryption_key)
    base_nonce = secrets.token_bytes(8)
    tmp_path = encrypted_path.with_name(encrypted_path.name + ".partial")
    if tmp_path.exists():
        tmp_path.unlink()
    chunk_count = 0
    with archive_path.open("rb") as source, tmp_path.open("wb") as output:
        output.write(BACKUP_ENCRYPTION_MAGIC)
        output.write(json.dumps({"scheme": BACKUP_ENCRYPTION_SCHEME, "chunk_size": chunk_size}).encode("utf-8") + b"\n")
        while True:
            plaintext = source.read(chunk_size)
            if not plaintext:
                break
            nonce = base_nonce + chunk_count.to_bytes(4, "big")
            ciphertext = AESGCM(key).encrypt(nonce, plaintext, backup_encryption_aad(manifest, chunk_count))
            output.write(len(ciphertext).to_bytes(4, "big"))
            output.write(ciphertext)
            chunk_count += 1
    tmp_path.chmod(0o600)
    tmp_path.replace(encrypted_path)
    encrypted_path.chmod(0o600)
    return {
        "scheme": BACKUP_ENCRYPTION_SCHEME,
        "mode": "copy",
        "file": encrypted_path.name,
        "key_id": backup_key_fingerprint(backup_encryption_key),
        "base_nonce": b64encode(base_nonce),
        "chunk_size": chunk_size,
        "chunk_count": chunk_count,
        "size_bytes": encrypted_path.stat().st_size,
        "sha256": sha256_file(encrypted_path),
        "plaintext_file": archive_path.name,
        "plaintext_size_bytes": archive_path.stat().st_size,
        "plaintext_sha256": sha256_file(archive_path),
    }


def decrypt_archive_to_path(encrypted_path: Path, output_path: Path, manifest: dict[str, Any], backup_encryption_key: str) -> None:
    require_backup_crypto()
    encryption = manifest.get("archive_encryption") or {}
    if encryption.get("scheme") != BACKUP_ENCRYPTION_SCHEME:
        raise BackupEncryptionError("unsupported backup archive encryption scheme")
    if backup_key_fingerprint(backup_encryption_key) != encryption.get("key_id"):
        raise BackupEncryptionError("backup archive was encrypted with a different key")
    if not encrypted_path.is_file():
        raise BackupEncryptionError("encrypted backup archive not found")
    if sha256_file(encrypted_path) != encryption.get("sha256"):
        raise BackupEncryptionError("encrypted backup archive checksum mismatch")
    base_nonce = b64decode(str(encryption.get("base_nonce", "")), "backup archive nonce")
    if len(base_nonce) != 8:
        raise BackupEncryptionError("backup archive nonce has invalid length")
    key = backup_key_material(backup_encryption_key)
    tmp_path = output_path.with_name(output_path.name + ".partial")
    if tmp_path.exists():
        tmp_path.unlink()
    expected_chunk_count = int(encryption.get("chunk_count") or 0)
    chunk_index = 0
    try:
        with encrypted_path.open("rb") as source, tmp_path.open("wb") as output:
            if source.readline() != BACKUP_ENCRYPTION_MAGIC:
                raise BackupEncryptionError("encrypted backup archive has invalid magic")
            try:
                header = json.loads(source.readline().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupEncryptionError("encrypted backup archive header is invalid") from exc
            if header.get("scheme") != BACKUP_ENCRYPTION_SCHEME:
                raise BackupEncryptionError("encrypted backup archive header has unsupported scheme")
            while True:
                length_bytes = source.read(4)
                if not length_bytes:
                    break
                if len(length_bytes) != 4:
                    raise BackupEncryptionError("encrypted backup archive has a truncated chunk header")
                ciphertext_length = int.from_bytes(length_bytes, "big")
                if ciphertext_length < 17 or ciphertext_length > int(encryption.get("chunk_size") or BACKUP_ENCRYPTION_CHUNK_SIZE) + 16:
                    raise BackupEncryptionError("encrypted backup archive chunk length is invalid")
                ciphertext = source.read(ciphertext_length)
                if len(ciphertext) != ciphertext_length:
                    raise BackupEncryptionError("encrypted backup archive has a truncated chunk")
                nonce = base_nonce + chunk_index.to_bytes(4, "big")
                try:
                    plaintext = AESGCM(key).decrypt(nonce, ciphertext, backup_encryption_aad(manifest, chunk_index))
                except (InvalidTag, ValueError) as exc:
                    raise BackupEncryptionError("encrypted backup archive could not be decrypted") from exc
                output.write(plaintext)
                chunk_index += 1
        if chunk_index != expected_chunk_count:
            raise BackupEncryptionError("encrypted backup archive chunk count mismatch")
        tmp_path.chmod(0o600)
        tmp_path.replace(output_path)
        output_path.chmod(0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def verify_encrypted_archive_file(backup_dir: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    encryption = manifest.get("archive_encryption") or None
    if not encryption:
        return None
    if encryption.get("scheme") != BACKUP_ENCRYPTION_SCHEME:
        raise BackupEncryptionError("unsupported backup archive encryption scheme")
    encrypted_path = backup_dir / str(encryption.get("file") or "")
    if not encrypted_path.is_file():
        raise BackupEncryptionError("encrypted backup archive not found")
    if encrypted_path.stat().st_size != int(encryption.get("size_bytes") or -1):
        raise BackupEncryptionError("encrypted backup archive size mismatch")
    if sha256_file(encrypted_path) != encryption.get("sha256"):
        raise BackupEncryptionError("encrypted backup archive checksum mismatch")
    return {
        "status": "encrypted-archive-verified",
        "file": encryption.get("file"),
        "scheme": encryption.get("scheme"),
        "mode": encryption.get("mode"),
        "sha256": encryption.get("sha256"),
    }


def postgres_cli_connection(database_url: str) -> tuple[list[str], dict[str, str]]:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg", "postgres"}:
        raise BackupError("DATABASE_URL must use a PostgreSQL scheme for native dump")
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise BackupError("DATABASE_URL is missing a database name")
    args = ["--dbname", database]
    if parsed.hostname:
        args.extend(["--host", parsed.hostname])
    if parsed.port:
        args.extend(["--port", str(parsed.port)])
    if parsed.username:
        args.extend(["--username", unquote(parsed.username)])
    env = os.environ.copy()
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    query = {key: value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
    if query.get("sslmode"):
        env["PGSSLMODE"] = query["sslmode"]
    return args, env


def run_postgres_tool(argv: list[str], *, env: dict[str, str], timeout_seconds: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"{Path(argv[0]).name} is not installed in the control-plane image") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"{Path(argv[0]).name} timed out") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise BackupError(f"{Path(argv[0]).name} failed: {message[:500] or 'no error output'}")
    return result


def verify_native_postgres_dump(dump_path: Path, *, pg_restore_bin: str = "pg_restore") -> dict[str, Any]:
    dump_path = dump_path.resolve()
    if not dump_path.is_file():
        raise BackupError("native PostgreSQL dump file does not exist")
    result = run_postgres_tool([pg_restore_bin, "--list", str(dump_path)], env=os.environ.copy(), timeout_seconds=120)
    object_count = sum(1 for line in result.stdout.splitlines() if line.strip() and not line.lstrip().startswith(";"))
    return {
        "status": "verified",
        "format": NATIVE_POSTGRES_DUMP_FORMAT,
        "path": str(dump_path),
        "tool": Path(pg_restore_bin).name,
        "object_count": object_count,
    }


def export_native_postgres_dump(
    database_url: str,
    output_path: Path,
    *,
    pg_dump_bin: str = "pg_dump",
    pg_restore_bin: str = "pg_restore",
) -> dict[str, Any]:
    connection_args, env = postgres_cli_connection(database_url)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".partial")
    if tmp_path.exists():
        tmp_path.unlink()
    argv = [
        pg_dump_bin,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(tmp_path),
        *connection_args,
    ]
    run_postgres_tool(argv, env=env, timeout_seconds=600)
    if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
        raise BackupError("native PostgreSQL dump was not written")
    tmp_path.chmod(0o600)
    tmp_path.replace(output_path)
    output_path.chmod(0o600)
    verification = verify_native_postgres_dump(output_path, pg_restore_bin=pg_restore_bin)
    return {
        "format": NATIVE_POSTGRES_DUMP_FORMAT,
        "path": str(output_path),
        "tool": Path(pg_dump_bin).name,
        "size_bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "verification": verification,
    }


def iter_files(root: Path, include_paths: list[str] | None = None, excludes: list[str] | None = None) -> list[Path]:
    root = root.resolve()
    include_paths = include_paths or DEFAULT_INCLUDE_PATHS
    excludes = excludes or DEFAULT_EXCLUDES
    files: list[Path] = []
    for include in include_paths:
        start = resolve_under(root, include)
        if not start.exists():
            continue
        if start.is_symlink():
            raise BackupError(f"refusing to back up symlink include path: {include}")
        if start.is_file():
            relative = start.relative_to(root).as_posix()
            if not is_excluded(relative, excludes):
                files.append(start)
            continue
        for path in sorted(start.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if is_excluded(relative, excludes):
                continue
            if path.is_symlink():
                raise BackupError(f"refusing to back up symlink: {relative}")
            if path.is_file():
                files.append(path)
    unique: dict[str, Path] = {path.relative_to(root).as_posix(): path for path in files}
    return [unique[key] for key in sorted(unique)]


def file_record(root: Path, path: Path) -> BackupFile:
    relative = path.relative_to(root).as_posix()
    return BackupFile(
        path=relative,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        sensitive=relative.startswith(SENSITIVE_PREFIXES),
    )


def backup_dir_for_name(backup_root: Path, backup_name: str) -> Path:
    return backup_root.resolve() / validate_backup_name(backup_name)


def create_backup(
    root: Path,
    backup_root: Path | None = None,
    label: str | None = None,
    include_paths: list[str] | None = None,
    excludes: list[str] | None = None,
    postgres_dump_path: Path | None = None,
    postgres_dump_format: str | None = None,
    postgres_extra_dumps: list[dict[str, Any]] | None = None,
    backup_encryption_key: str | None = None,
    backup_encryption_mode: str = "none",
) -> dict[str, Any]:
    root = root.resolve()
    backup_root = ensure_under(root, backup_root or root / "backups", "backup root")
    include_paths = include_paths or DEFAULT_INCLUDE_PATHS
    excludes = excludes or DEFAULT_EXCLUDES
    encryption_mode = validate_backup_encryption_mode(backup_encryption_mode)
    postgres_dump_specs: list[PostgresDumpSpec] = []
    if postgres_dump_path is not None:
        postgres_dump_specs.append(
            PostgresDumpSpec(
                path=postgres_dump_path,
                format=postgres_dump_format or LOGICAL_POSTGRES_DUMP_FORMAT,
                kind="logical",
                tool="control-plane",
                verified=True,
            )
        )
    for item in postgres_extra_dumps or []:
        postgres_dump_specs.append(
            PostgresDumpSpec(
                path=Path(item["path"]),
                format=str(item.get("format") or ""),
                kind=str(item.get("kind") or "native"),
                tool=str(item.get("tool") or "") or None,
                verified=bool(item.get("verified", True)),
            )
        )
    normalized_dump_specs: list[tuple[PostgresDumpSpec, str]] = []
    for spec in postgres_dump_specs:
        dump_path = spec.path if spec.path.is_absolute() else root / spec.path
        dump_path = ensure_under(root, dump_path, "postgres dump path")
        if not dump_path.is_file():
            raise BackupError("postgres dump path does not exist")
        dump_relative = dump_path.relative_to(root).as_posix()
        if is_excluded(dump_relative, excludes):
            raise BackupError("postgres dump path is excluded from backups")
        if not any(dump_relative == include.strip("/") or dump_relative.startswith(include.strip("/") + "/") for include in include_paths):
            include_paths = [*include_paths, dump_relative]
        normalized_dump_specs.append((spec, dump_relative))
    backup_root.mkdir(parents=True, exist_ok=True)
    name = validate_backup_name(label) if label else utc_stamp()
    backup_dir = backup_root / name
    suffix = 1
    while backup_dir.exists():
        backup_dir = backup_root / f"{name}-{suffix}"
        suffix += 1
    backup_dir.mkdir(mode=0o700)
    archive_path = backup_dir / "payload.tar.gz"
    files = iter_files(root, include_paths, excludes)
    records = [file_record(root, path) for path in files]
    records_by_path = {record.path: record for record in records}
    postgres_dump_records = [
        {
            **records_by_path[dump_relative].__dict__,
            "format": spec.format,
            "kind": spec.kind,
            "tool": spec.tool,
            "verified": spec.verified,
        }
        for spec, dump_relative in normalized_dump_specs
        if dump_relative in records_by_path
    ]
    postgres_dump_record = postgres_dump_records[0] if postgres_dump_records else None
    postgres_native_dump_record = next((record for record in postgres_dump_records if record.get("kind") == "native"), None)
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    manifest = {
        "format": BACKUP_FORMAT,
        "name": backup_dir.name,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "root": str(root),
        "archive": {
            "file": archive_path.name,
            "size_bytes": archive_path.stat().st_size,
            "sha256": sha256_file(archive_path),
        },
        "include_paths": include_paths,
        "excluded_paths": excludes,
        "files": [record.__dict__ for record in records],
        "contains_sensitive_data": any(record.sensitive for record in records),
        "postgres_dump_included": postgres_dump_record is not None,
        "postgres_dump": postgres_dump_record,
        "postgres_dumps": postgres_dump_records,
        "postgres_native_dump": postgres_native_dump_record,
        "postgres_dump_note": (
            "control-plane PostgreSQL logical and native custom-format dumps included"
            if postgres_dump_record and postgres_native_dump_record
            else "control-plane PostgreSQL logical export included"
            if postgres_dump_record and postgres_dump_record.get("kind") == "logical"
            else "control-plane PostgreSQL native custom-format dump included"
            if postgres_native_dump_record
            else "offline filesystem snapshot only; use a database dump before production restore tests"
        ),
        "archive_encryption": None,
        "preserve_old_stack": True,
    }
    if encryption_mode != "none":
        if not backup_encryption_key:
            raise BackupEncryptionError("backup encryption is enabled but no encryption key is configured")
        encrypted_path = backup_dir / f"{archive_path.name}.aesgcm"
        encryption = encrypt_archive(archive_path, encrypted_path, manifest, backup_encryption_key)
        encryption["mode"] = encryption_mode
        manifest["archive_encryption"] = encryption
        if encryption_mode == "encrypted-only":
            archive_path.unlink()
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (backup_dir / "manifest.json").chmod(0o600)
    if archive_path.exists():
        archive_path.chmod(0o600)
    return manifest_summary(backup_dir, manifest)


def load_manifest(backup_dir: Path) -> dict[str, Any]:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError("backup manifest not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupError("unsupported backup format")
    return manifest


def manifest_summary(backup_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    archive = manifest.get("archive", {})
    return {
        "name": backup_dir.name,
        "created_at": manifest.get("created_at"),
        "archive": archive,
        "file_count": len(manifest.get("files", [])),
        "contains_sensitive_data": bool(manifest.get("contains_sensitive_data")),
        "postgres_dump_included": bool(manifest.get("postgres_dump_included")),
        "postgres_dump": manifest.get("postgres_dump"),
        "postgres_dumps": manifest.get("postgres_dumps") or ([] if manifest.get("postgres_dump") is None else [manifest.get("postgres_dump")]),
        "postgres_native_dump": manifest.get("postgres_native_dump"),
        "archive_encryption": manifest.get("archive_encryption"),
        "preserve_old_stack": bool(manifest.get("preserve_old_stack", True)),
    }


def list_backups(backup_root: Path) -> list[dict[str, Any]]:
    backup_root = backup_root.resolve()
    if not backup_root.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for path in sorted(item for item in backup_root.iterdir() if item.is_dir()):
        try:
            summaries.append(manifest_summary(path, load_manifest(path)))
        except (BackupError, json.JSONDecodeError):
            summaries.append({"name": path.name, "status": "invalid"})
    return sorted(summaries, key=lambda item: (item.get("created_at") or "", item["name"]), reverse=True)


def parse_manifest_datetime(value: str | None, fallback: datetime | None = None) -> datetime:
    if not value:
        return fallback or datetime.fromtimestamp(0, tz=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback or datetime.fromtimestamp(0, tz=UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def backup_directory_size(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        root = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (root / name).is_symlink()]
        for filename in filenames:
            candidate = root / filename
            try:
                if candidate.is_symlink():
                    total += candidate.lstat().st_size
                elif candidate.is_file():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def backup_retention_entries(backup_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    backup_root = backup_root.resolve()
    if not backup_root.exists():
        return [], []
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for path in sorted(item for item in backup_root.iterdir() if item.is_dir() or item.is_symlink()):
        if path.is_symlink():
            invalid.append({"name": path.name, "status": "invalid", "reason": "backup entry is a symlink"})
            continue
        try:
            validate_backup_name(path.name)
            manifest = load_manifest(path)
            summary = manifest_summary(path, manifest)
            created_dt = parse_manifest_datetime(summary.get("created_at"), datetime.fromtimestamp(path.stat().st_mtime, tz=UTC))
            valid.append(
                {
                    **summary,
                    "created_at": created_dt.isoformat(),
                    "total_size_bytes": backup_directory_size(path),
                    "_created_dt": created_dt,
                }
            )
        except (BackupError, json.JSONDecodeError, OSError) as exc:
            invalid.append({"name": path.name, "status": "invalid", "reason": str(exc)[:500]})
    valid.sort(key=lambda item: (item["_created_dt"], item["name"]), reverse=True)
    return valid, invalid


def public_retention_entry(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    public = {key: value for key, value in entry.items() if not key.startswith("_")}
    public["reason"] = reason
    return public


def build_backup_retention_plan(
    backup_root: Path,
    *,
    keep_last: int = 5,
    delete_older_than_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if keep_last < 0:
        raise BackupError("keep_last must be zero or greater")
    if delete_older_than_days is not None and delete_older_than_days < 1:
        raise BackupError("delete_older_than_days must be at least one day")
    now_dt = now or datetime.now(tz=UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    else:
        now_dt = now_dt.astimezone(UTC)
    cutoff = now_dt - timedelta(days=delete_older_than_days) if delete_older_than_days is not None else None
    valid, invalid = backup_retention_entries(backup_root)
    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for index, entry in enumerate(valid):
        protected_by_keep_last = index < keep_last
        older_than_cutoff = cutoff is None or entry["_created_dt"] < cutoff
        if not protected_by_keep_last and older_than_cutoff:
            reason = "outside_keep_last"
            if cutoff is not None:
                reason = f"{reason}, older_than_{delete_older_than_days}_days"
            candidates.append(public_retention_entry(entry, reason))
            continue
        if protected_by_keep_last:
            reason = "protected_by_keep_last"
        else:
            reason = f"newer_than_{delete_older_than_days}_day_threshold"
        kept.append(public_retention_entry(entry, reason))
    return {
        "status": "planned" if candidates else "noop",
        "backup_root": str(backup_root.resolve()),
        "policy": {
            "keep_last": keep_last,
            "delete_older_than_days": delete_older_than_days,
            "cutoff": cutoff.isoformat() if cutoff else None,
            "now": now_dt.isoformat(),
        },
        "candidate_count": len(candidates),
        "kept_count": len(kept),
        "invalid_preserved_count": len(invalid),
        "total_reclaimable_bytes": sum(int(item.get("total_size_bytes") or 0) for item in candidates),
        "candidates": candidates,
        "kept": kept,
        "invalid_preserved": invalid,
        "requires_confirmation": True,
    }


def ensure_backup_directory_deletable(backup_root: Path, backup_name: str) -> Path:
    root = backup_root.resolve(strict=True)
    path = root / validate_backup_name(backup_name)
    if path.is_symlink():
        raise BackupError(f"refusing to delete symlinked backup directory: {backup_name}")
    resolved = path.resolve(strict=True)
    if resolved.parent != root:
        raise BackupError(f"backup directory is not a direct child of backup root: {backup_name}")
    if not resolved.is_dir():
        raise BackupError(f"backup directory is not a directory: {backup_name}")
    for dirpath, dirnames, filenames in os.walk(resolved, followlinks=False):
        current = Path(dirpath)
        for name in [*dirnames, *filenames]:
            if (current / name).is_symlink():
                raise BackupError(f"refusing to delete backup with symlinked content: {backup_name}")
    return resolved


def apply_backup_retention_plan(
    backup_root: Path,
    *,
    keep_last: int = 5,
    delete_older_than_days: int | None = None,
    confirmed: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    plan = build_backup_retention_plan(
        backup_root,
        keep_last=keep_last,
        delete_older_than_days=delete_older_than_days,
        now=now,
    )
    if not confirmed:
        raise BackupError("backup cleanup requires explicit confirmation")
    deleted: list[dict[str, Any]] = []
    for candidate in plan["candidates"]:
        path = ensure_backup_directory_deletable(backup_root, candidate["name"])
        shutil.rmtree(path)
        deleted.append({**candidate, "status": "deleted"})
    return {
        **plan,
        "status": "applied" if deleted else "noop",
        "deleted_count": len(deleted),
        "deleted": deleted,
    }


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


@contextmanager
def readable_archive_path(backup_dir: Path, manifest: dict[str, Any], backup_encryption_key: str | None = None) -> Any:
    archive_path = backup_dir / manifest["archive"]["file"]
    if not archive_path.is_file():
        encryption = manifest.get("archive_encryption") or {}
        if not encryption:
            raise BackupError("backup archive not found")
        if not backup_encryption_key:
            raise BackupEncryptionError("backup archive is encrypted and requires the backup encryption key")
        encrypted_path = backup_dir / str(encryption.get("file") or "")
        handle = tempfile.NamedTemporaryFile(prefix="b1-backup-archive-", suffix=".tar.gz", delete=False)
        decrypted_path = Path(handle.name)
        handle.close()
        try:
            decrypt_archive_to_path(encrypted_path, decrypted_path, manifest, backup_encryption_key)
            actual_digest = sha256_file(decrypted_path)
            if actual_digest != manifest["archive"]["sha256"]:
                raise BackupEncryptionError("decrypted backup archive checksum mismatch")
            yield decrypted_path
        finally:
            if decrypted_path.exists():
                decrypted_path.unlink()
        return
    if sha256_file(archive_path) != manifest["archive"]["sha256"]:
        raise BackupError("archive checksum mismatch")
    yield archive_path


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
                raise BackupError(f"archive contains duplicate file: {member.name}")
            if member_path not in expected:
                raise BackupError(f"archive contains unmanifested file: {member.name}")
            if member.size != expected[member_path]["size_bytes"]:
                raise BackupError(f"archive member size mismatch: {member.name}")
            handle = archive.extractfile(member)
            if handle is None:
                raise BackupError(f"cannot read archive member: {member.name}")
            if sha256_stream(handle) != expected[member_path]["sha256"]:
                raise BackupError(f"archive member checksum mismatch: {member.name}")
            seen.add(member_path)
    missing = sorted(set(expected) - seen)
    if missing:
        raise BackupError(f"archive missing manifest files: {', '.join(missing[:5])}")


def verify_archive(backup_dir: Path, manifest: dict[str, Any], backup_encryption_key: str | None = None) -> Path:
    with readable_archive_path(backup_dir, manifest, backup_encryption_key) as archive_path:
        verify_archive_members(archive_path, manifest)
    return archive_path


def verify_backup(backup_root: Path, backup_name: str, backup_encryption_key: str | None = None) -> dict[str, Any]:
    backup_dir = backup_dir_for_name(backup_root, backup_name)
    manifest = load_manifest(backup_dir)
    postgres_native_dump = manifest.get("postgres_native_dump")
    native_verification: dict[str, Any] | None = None
    archive_encryption_verification = verify_encrypted_archive_file(backup_dir, manifest)
    with readable_archive_path(backup_dir, manifest, backup_encryption_key) as archive_path:
        verify_archive_members(archive_path, manifest)
        if postgres_native_dump and postgres_native_dump.get("path"):
            with tarfile.open(archive_path, "r:gz") as archive:
                member = archive.extractfile(postgres_native_dump["path"])
                if member is None:
                    raise BackupError("native PostgreSQL dump is missing from archive")
                digest = hashlib.sha256()
                for chunk in iter(lambda: member.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != postgres_native_dump.get("sha256"):
                    raise BackupError("native PostgreSQL dump checksum mismatch")
            native_verification = {"status": "archive-member-verified", "format": postgres_native_dump.get("format"), "path": postgres_native_dump.get("path")}
    return {
        "status": "verified",
        "backup": backup_name,
        "file_count": len(manifest.get("files", [])),
        "archive_sha256": manifest["archive"]["sha256"],
        "contains_sensitive_data": bool(manifest.get("contains_sensitive_data")),
        "postgres_dump_included": bool(manifest.get("postgres_dump_included")),
        "postgres_dump": manifest.get("postgres_dump"),
        "postgres_dumps": manifest.get("postgres_dumps") or ([] if manifest.get("postgres_dump") is None else [manifest.get("postgres_dump")]),
        "postgres_native_dump": postgres_native_dump,
        "postgres_native_dump_verification": native_verification,
        "archive_encryption": manifest.get("archive_encryption"),
        "archive_encryption_verification": archive_encryption_verification,
    }


def restore_file_mode(member_name: str, manifest: dict[str, Any]) -> int:
    expected = {item["path"]: item for item in manifest.get("files", [])}
    record = expected.get(safe_member_relative_path(member_name).as_posix(), {})
    return 0o600 if record.get("sensitive") else 0o644


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


def restore_backup_to_alternate(backup_root: Path, backup_name: str, restore_root: Path, force: bool = False, backup_encryption_key: str | None = None) -> dict[str, Any]:
    backup_dir = backup_dir_for_name(backup_root, backup_name)
    restore_root = restore_root.resolve()
    manifest = load_manifest(backup_dir)
    target = restore_root / backup_name
    if target.exists() and any(target.iterdir()) and not force:
        raise RestoreError("restore test target exists and is not empty")
    target.mkdir(parents=True, exist_ok=True)
    archive_encryption_verification = verify_encrypted_archive_file(backup_dir, manifest)
    with readable_archive_path(backup_dir, manifest, backup_encryption_key) as archive_path:
        verify_archive_members(archive_path, manifest)
        extract_verified_archive(archive_path, target, manifest)
    verified = verify_restored_files(target, manifest)
    report = {
        "status": "restored",
        "backup": backup_name,
        "target": str(target),
        "files_verified": len(verified),
        "contains_sensitive_data": bool(manifest.get("contains_sensitive_data")),
        "postgres_dump_included": bool(manifest.get("postgres_dump_included")),
        "postgres_dumps": manifest.get("postgres_dumps") or ([] if manifest.get("postgres_dump") is None else [manifest.get("postgres_dump")]),
        "postgres_native_dump": manifest.get("postgres_native_dump"),
        "archive_encryption": manifest.get("archive_encryption"),
        "archive_encryption_verification": archive_encryption_verification,
    }
    native_dump = manifest.get("postgres_native_dump")
    if native_dump and native_dump.get("path"):
        report["postgres_native_dump_verification"] = verify_native_postgres_dump(target / native_dump["path"])
    (target / "restore-report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
