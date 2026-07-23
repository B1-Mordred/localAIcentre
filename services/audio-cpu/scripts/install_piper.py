#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath


DEFAULT_RELEASE = "2023.11.14-2"
DEFAULT_ASSET = "piper_linux_x86_64.tar.gz"
DEFAULT_SHA256 = "a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992"
RELEASE_URL = "https://github.com/rhasspy/piper/releases/download/{release}/{asset}"


class UnsafeArchiveError(ValueError):
    pass


def validate_archive_member(member: tarfile.TarInfo, root: str = "piper") -> None:
    name = PurePosixPath(member.name)
    if not member.name or name.is_absolute() or ".." in name.parts:
        raise UnsafeArchiveError(f"unsafe Piper archive member path: {member.name!r}")
    if not name.parts or name.parts[0] != root:
        raise UnsafeArchiveError(f"Piper archive member is outside {root}/: {member.name!r}")
    if not (member.isfile() or member.isdir() or member.issym()):
        raise UnsafeArchiveError(f"unsupported Piper archive member type: {member.name!r}")
    if member.issym():
        link = PurePosixPath(member.linkname)
        if not member.linkname or link.is_absolute() or ".." in link.parts:
            raise UnsafeArchiveError(f"unsafe Piper symlink target: {member.name!r} -> {member.linkname!r}")


def validate_archive(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            validate_archive_member(member)


def download(url: str, output: Path) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response, output.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def install_piper(release: str, asset: str, expected_sha256: str, install_dir: Path, archive_path: Path) -> None:
    url = RELEASE_URL.format(release=release, asset=asset)
    actual_sha256 = download(url, archive_path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"piper checksum mismatch: expected {expected_sha256}, got {actual_sha256}")

    validate_archive(archive_path)
    install_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(install_dir, filter="data")

    binary = install_dir / "piper" / "piper"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise FileNotFoundError(f"installed Piper binary is not executable: {binary}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a checksum-pinned Piper release asset.")
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--sha256", default=DEFAULT_SHA256)
    parser.add_argument("--install-dir", type=Path, default=Path("/opt"))
    parser.add_argument("--archive-path", type=Path, default=Path("/tmp/piper.tar.gz"))
    args = parser.parse_args()

    try:
        install_piper(args.release, args.asset, args.sha256, args.install_dir, args.archive_path)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
