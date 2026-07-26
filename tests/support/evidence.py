from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class EvidenceWriteError(RuntimeError):
    pass


def assert_safe_output_path(path: Path) -> None:
    if path.exists():
        if path.is_symlink():
            raise EvidenceWriteError(f"refusing to write evidence through symlink: {path}")
        if not path.is_file():
            raise EvidenceWriteError(f"refusing to overwrite non-regular evidence path: {path}")
    current = path.parent
    parents: list[Path] = []
    while current != current.parent:
        parents.append(current)
        current = current.parent
    for parent in reversed(parents):
        if parent.exists() and parent.is_symlink():
            raise EvidenceWriteError(f"refusing to write evidence inside symlinked directory: {parent}")


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    assert_safe_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o640)
        os.replace(tmp_path, path)
        path.chmod(0o640)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
