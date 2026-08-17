#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_APP = ROOT / "services" / "control-plane"
sys.path.insert(0, str(CONTROL_PLANE_APP))

from app import backup_restore  # noqa: E402


DEFAULT_INCLUDE_PATHS = backup_restore.DEFAULT_INCLUDE_PATHS
DEFAULT_EXCLUDES = backup_restore.DEFAULT_EXCLUDES
sha256_file = backup_restore.sha256_file


def read_key_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def configured_backup_encryption_key(key_file: str | None = None) -> str:
    return (
        read_key_file(key_file)
        or read_key_file(os.getenv("B1_BACKUP_ENCRYPTION_KEY_FILE"))
        or os.getenv("B1_BACKUP_ENCRYPTION_KEY", "").strip()
        or read_key_file(os.getenv("B1_MASTER_KEY_FILE"))
        or os.getenv("B1_MASTER_KEY", "").strip()
    )


def backup(
    root: Path,
    include_paths: list[str] | None = None,
    excludes: list[str] | None = None,
    label: str | None = None,
    postgres_dump_path: Path | None = None,
    postgres_dump_format: str | None = None,
    postgres_extra_dumps: list[dict[str, Any]] | None = None,
    backup_root: Path | None = None,
    backup_encryption_key: str | None = None,
    backup_encryption_mode: str | None = None,
) -> Path:
    root = root.resolve()
    backup_root = backup_root or root / "backups"
    summary = backup_restore.create_backup(
        root,
        backup_root,
        label,
        include_paths=include_paths or DEFAULT_INCLUDE_PATHS,
        excludes=excludes or DEFAULT_EXCLUDES,
        postgres_dump_path=postgres_dump_path,
        postgres_dump_format=postgres_dump_format,
        postgres_extra_dumps=postgres_extra_dumps,
        backup_encryption_key=backup_encryption_key,
        backup_encryption_mode=backup_encryption_mode or os.getenv("B1_BACKUP_ENCRYPTION_MODE", "none"),
    )
    return backup_restore.backup_dir_for_name(backup_root, summary["name"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a B1 AI Hub filesystem backup archive with checksums.")
    parser.add_argument("--root", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub"))
    parser.add_argument("--backup-root", default=os.getenv("B1_BACKUP_ROOT", ""), help="Backup directory. Defaults to ROOT/backups.")
    parser.add_argument("--label", default=None, help="Optional backup directory name under the backup root.")
    parser.add_argument("--include", action="append", default=None, help="Relative path to include. Repeatable. Defaults to B1 state paths.")
    parser.add_argument("--exclude", action="append", default=None, help="Relative path to exclude. Repeatable. Defaults skip cache/blobs/backups.")
    parser.add_argument("--postgres-dump-file", default=None, help="Optional logical PostgreSQL dump file under ROOT to mark and include.")
    parser.add_argument("--postgres-dump-format", default=backup_restore.LOGICAL_POSTGRES_DUMP_FORMAT, help="Format label for --postgres-dump-file.")
    parser.add_argument("--postgres-native-dump-file", default=None, help="Optional native custom-format PostgreSQL dump file under ROOT to mark and include.")
    parser.add_argument("--backup-encryption-mode", default=os.getenv("B1_BACKUP_ENCRYPTION_MODE", "none"), choices=sorted(backup_restore.BACKUP_ENCRYPTION_MODES))
    parser.add_argument("--backup-encryption-key-file", default=None, help="Key file for encrypted payload archives. Defaults to B1_BACKUP_ENCRYPTION_KEY_FILE or B1_MASTER_KEY_FILE.")
    args = parser.parse_args()

    native_dump: list[dict[str, Any]] = []
    if args.postgres_native_dump_file:
        native_dump.append(
            {
                "path": str(Path(args.postgres_native_dump_file)),
                "format": backup_restore.NATIVE_POSTGRES_DUMP_FORMAT,
                "kind": "native",
                "tool": "pg_dump",
                "verified": False,
            }
        )

    try:
        backup_dir = backup(
            Path(args.root),
            backup_root=Path(args.backup_root) if args.backup_root else None,
            include_paths=args.include or DEFAULT_INCLUDE_PATHS,
            excludes=args.exclude or DEFAULT_EXCLUDES,
            label=args.label,
            postgres_dump_path=Path(args.postgres_dump_file) if args.postgres_dump_file else None,
            postgres_dump_format=args.postgres_dump_format,
            postgres_extra_dumps=native_dump,
            backup_encryption_key=configured_backup_encryption_key(args.backup_encryption_key_file),
            backup_encryption_mode=args.backup_encryption_mode,
        )
    except backup_restore.BackupError as exc:
        raise SystemExit(f"backup failed: {exc}") from exc

    manifest = backup_restore.load_manifest(backup_dir)
    print(f"created backup: {backup_dir}")
    print(f"manifest: {backup_dir / 'manifest.json'}")
    if manifest.get("archive_encryption"):
        print(json.dumps({"archive_encryption": manifest["archive_encryption"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
