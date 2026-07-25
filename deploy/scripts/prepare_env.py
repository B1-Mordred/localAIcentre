#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


class PrepareEnvError(RuntimeError):
    pass


def docker_socket_gid(path: Path) -> int:
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise PrepareEnvError(f"Docker socket is missing: {path}") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise PrepareEnvError(f"Docker socket path is not a Unix socket: {path}")
    return info.st_gid


def render_env(source: str, values: dict[str, str]) -> tuple[str, list[str]]:
    updated: list[str] = []
    seen: set[str] = set()
    lines: list[str] = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in line:
            lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in values:
            lines.append(f"{key}={values[key]}")
            seen.add(key)
            updated.append(key)
            continue
        lines.append(line)

    missing = [key for key in values if key not in seen]
    if missing and lines and lines[-1].strip():
        lines.append("")
    for key in missing:
        lines.append(f"{key}={values[key]}")
        updated.append(key)
    return "\n".join(lines) + "\n", updated


def assert_writable_target(path: Path, *, update_existing: bool) -> None:
    if path.exists():
        if path.is_symlink():
            raise PrepareEnvError(f"refusing to write through symlink: {path}")
        if not path.is_file():
            raise PrepareEnvError(f"refusing to overwrite non-regular file: {path}")
        if not update_existing:
            raise PrepareEnvError(f"{path} already exists; rerun with --update-existing to update managed keys")
    current = path.parent
    while current != current.parent:
        if current.is_symlink():
            raise PrepareEnvError(f"refusing to write inside symlinked directory: {current}")
        current = current.parent


def write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o640)
        os.replace(tmp_path, path)
        path.chmod(0o640)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def prepare_production_env(
    *,
    template: Path,
    output: Path,
    docker_socket: Path,
    update_existing: bool = False,
) -> dict[str, Any]:
    template = template.resolve(strict=True)
    output = output if output.is_absolute() else Path.cwd() / output
    docker_socket = docker_socket if docker_socket.is_absolute() else Path.cwd() / docker_socket
    assert_writable_target(output, update_existing=update_existing)
    gid = docker_socket_gid(docker_socket)
    existing = output.exists()
    source_path = output if existing and update_existing else template
    source = source_path.read_text(encoding="utf-8")
    rendered, updated = render_env(source, {"B1_DOCKER_GID": str(gid)})
    write_private_text(output, rendered)
    return {
        "output": str(output),
        "source": str(source_path),
        "created": not existing,
        "updated_keys": sorted(set(updated)),
        "docker_socket": str(docker_socket),
        "docker_socket_gid": gid,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a B1 AI Hub production .env file from the pinned template.")
    parser.add_argument("--template", default=".env.production.example", help="Production env template to read.")
    parser.add_argument("--output", default=".env", help="Env file to create or update.")
    parser.add_argument("--docker-socket", default="/var/run/docker.sock", help="Host Docker socket used to derive B1_DOCKER_GID.")
    parser.add_argument("--update-existing", action="store_true", help="Update managed keys in an existing output file.")
    args = parser.parse_args()
    result = prepare_production_env(
        template=Path(args.template),
        output=Path(args.output),
        docker_socket=Path(args.docker_socket),
        update_existing=args.update_existing,
    )
    action = "created" if result["created"] else "updated"
    keys = ", ".join(result["updated_keys"])
    print(f"{action} {result['output']}")
    print(f"set {keys} from {result['docker_socket']} gid {result['docker_socket_gid']}")


if __name__ == "__main__":
    main()
