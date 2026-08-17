from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class PrivateFileError(RuntimeError):
    pass


def assert_safe_output_path(path: Path, *, label: str = "output") -> None:
    if path.exists():
        if path.is_symlink():
            raise PrivateFileError(f"refusing to write {label} through symlink: {path}")
        if not path.is_file():
            raise PrivateFileError(f"refusing to overwrite non-regular {label}: {path}")
    current = path.parent
    parents: list[Path] = []
    while current != current.parent:
        parents.append(current)
        current = current.parent
    for parent in reversed(parents):
        if parent.exists() and parent.is_symlink():
            raise PrivateFileError(f"refusing to write {label} inside symlinked directory: {parent}")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private_bytes(path: Path, payload: bytes, *, mode: int = 0o640, label: str = "output") -> Path:
    assert_safe_output_path(path, label=label)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_output_path(path, label=label)
    fd: int | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    except OSError as exc:
        raise PrivateFileError(f"cannot create {label}: {exc}") from exc
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        assert_safe_output_path(path, label=label)
        os.replace(tmp_path, path)
        path.chmod(mode)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise PrivateFileError(f"cannot write {label}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path.exists():
            tmp_path.unlink()
    return path


def write_private_json(path: Path, payload: dict[str, Any], *, mode: int = 0o640, label: str = "output") -> Path:
    serialized = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n"
    return write_private_bytes(path, serialized, mode=mode, label=label)
