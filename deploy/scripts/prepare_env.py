#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class PrepareEnvError(RuntimeError):
    pass


NORMALIZED_LIST_KEYS = {"B1_RUNTIME_PRODUCTION_REQUIRED"}
HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
TRUE_VALUES = {"1", "true", "yes", "y", "on", "dirty"}
FALSE_VALUES = {"0", "false", "no", "n", "off", "clean"}


def docker_socket_gid(path: Path) -> int:
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise PrepareEnvError(f"Docker socket is missing: {path}") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise PrepareEnvError(f"Docker socket path is not a Unix socket: {path}")
    return info.st_gid


def bool_env(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def int_env(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def run_git(repo_root: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def source_metadata_values(*, repo_root: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    env = environ if environ is not None else dict(os.environ)
    env_commit = (env.get("B1_SOURCE_COMMIT") or env.get("GIT_COMMIT") or "").strip().lower()
    env_source_ref = (env.get("B1_SOURCE_REF") or env.get("GIT_BRANCH") or "").strip()
    env_dirty = bool_env(env.get("B1_SOURCE_DIRTY") or env.get("GIT_DIRTY"))
    env_dirty_path_count = int_env(
        env.get("B1_SOURCE_DIRTY_PATH_COUNT")
        or env.get("B1_DIRTY_PATH_COUNT")
        or env.get("GIT_DIRTY_PATH_COUNT")
    )
    try:
        top_level = Path(run_git(repo_root, ["rev-parse", "--show-toplevel"])).resolve()
        commit = run_git(top_level, ["rev-parse", "HEAD"]).lower()
        source_ref = run_git(top_level, ["rev-parse", "--abbrev-ref", "HEAD"])
        status = run_git(top_level, ["status", "--porcelain", "--untracked-files=normal"])
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if not env_commit:
            raise PrepareEnvError(
                "source metadata is unavailable; run from a Git checkout or set B1_SOURCE_COMMIT before prepare-production-env"
            ) from exc
    else:
        if not COMMIT_RE.fullmatch(commit):
            raise PrepareEnvError("Git HEAD is not a valid 40-character commit SHA")
        dirty_paths = [line for line in status.splitlines() if line.strip()]
        if source_ref == "HEAD":
            source_ref = ""
        return {
            "B1_SOURCE_COMMIT": commit,
            "B1_SOURCE_REF": source_ref,
            "B1_SOURCE_DIRTY": "true" if dirty_paths else "false",
            "B1_SOURCE_DIRTY_PATH_COUNT": str(len(dirty_paths)),
        }
    if env_commit:
        if not COMMIT_RE.fullmatch(env_commit):
            raise PrepareEnvError("B1_SOURCE_COMMIT/GIT_COMMIT must be a 40-character Git commit SHA")
        if env_dirty is None:
            env_dirty = False
        if env_dirty_path_count is None:
            env_dirty_path_count = 0
        return {
            "B1_SOURCE_COMMIT": env_commit,
            "B1_SOURCE_REF": env_source_ref,
            "B1_SOURCE_DIRTY": "true" if env_dirty else "false",
            "B1_SOURCE_DIRTY_PATH_COUNT": str(env_dirty_path_count),
        }
    raise PrepareEnvError(
        "source metadata is unavailable; run from a Git checkout or set B1_SOURCE_COMMIT before prepare-production-env"
    )


def normalize_env_value(key: str, value: str) -> str:
    if key in NORMALIZED_LIST_KEYS:
        items = [item.strip() for item in value.replace(",", " ").split() if item.strip()]
        if items:
            return ",".join(items)
    return value


def normalize_expected_target_host(value: str) -> str:
    raw = value.strip().lower().rstrip(".")
    if not raw:
        raise PrepareEnvError("expected target host must not be empty")
    if "://" in raw or "/" in raw or ":" in raw:
        raise PrepareEnvError("expected target host must be a hostname or FQDN without scheme, path, or port")
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        pass
    else:
        raise PrepareEnvError("expected target host must be a system hostname/FQDN, not an IP address")
    labels = raw.split(".")
    if not all(HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise PrepareEnvError("expected target host contains invalid hostname labels")
    return raw


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
        original_value = line.split("=", 1)[1]
        normalized_value = normalize_env_value(key, original_value)
        if normalized_value != original_value:
            lines.append(f"{key}={normalized_value}")
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
    appliance_hostname: str | None = None,
    expected_target_host: str | None = None,
    update_existing: bool = False,
    stamp_source: bool = False,
    source_root: Path | None = None,
) -> dict[str, Any]:
    template = template.resolve(strict=True)
    output = output if output.is_absolute() else Path.cwd() / output
    docker_socket = docker_socket if docker_socket.is_absolute() else Path.cwd() / docker_socket
    assert_writable_target(output, update_existing=update_existing)
    gid = docker_socket_gid(docker_socket)
    existing = output.exists()
    source_path = output if existing and update_existing else template
    source = source_path.read_text(encoding="utf-8")
    values = {"B1_DOCKER_GID": str(gid)}
    source_values: dict[str, str] = {}
    if stamp_source:
        source_values = source_metadata_values(repo_root=(source_root or Path.cwd()).resolve())
        values.update(source_values)
    normalized_target_host = None
    if appliance_hostname is not None or expected_target_host is not None:
        normalized_appliance_hostname = (
            normalize_expected_target_host(appliance_hostname)
            if appliance_hostname is not None
            else None
        )
        normalized_expected_target = (
            normalize_expected_target_host(expected_target_host)
            if expected_target_host is not None
            else None
        )
        normalized_target_host = normalized_appliance_hostname or normalized_expected_target
        if (
            normalized_appliance_hostname is not None
            and normalized_expected_target is not None
            and normalized_appliance_hostname != normalized_expected_target
        ):
            raise PrepareEnvError("B1_APPLIANCE_HOSTNAME and B1_EXPECTED_TARGET_HOST must match")
        values["B1_APPLIANCE_HOSTNAME"] = normalized_target_host
        values["B1_EXPECTED_TARGET_HOST"] = normalized_target_host
    rendered, updated = render_env(source, values)
    write_private_text(output, rendered)
    return {
        "output": str(output),
        "source": str(source_path),
        "created": not existing,
        "updated_keys": sorted(set(updated)),
        "docker_socket": str(docker_socket),
        "docker_socket_gid": gid,
        "appliance_hostname": normalized_target_host,
        "expected_target_host": normalized_target_host,
        "hostname_authority": "b1-appliance-config",
        "network_property_source": "host-dhcp-client",
        "b1_static_ip_configures": False,
        "source_control": {
            "available": bool(source_values),
            "source_commit": source_values.get("B1_SOURCE_COMMIT", ""),
            "source_ref": source_values.get("B1_SOURCE_REF", ""),
            "source_dirty": bool_env(source_values.get("B1_SOURCE_DIRTY")) if source_values else None,
            "dirty_path_count": int_env(source_values.get("B1_SOURCE_DIRTY_PATH_COUNT")) if source_values else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a B1 AI Hub production .env file from the pinned template.")
    parser.add_argument("--template", default=".env.production.example", help="Production env template to read.")
    parser.add_argument("--output", default=".env", help="Env file to create or update.")
    parser.add_argument("--docker-socket", default="/var/run/docker.sock", help="Host Docker socket used to derive B1_DOCKER_GID.")
    parser.add_argument(
        "--appliance-hostname",
        default=os.getenv("B1_APPLIANCE_HOSTNAME"),
        help="B1-defined appliance system hostname/FQDN. IP/gateway/DNS properties remain DHCP-owned.",
    )
    parser.add_argument(
        "--expected-target-host",
        default=os.getenv("B1_EXPECTED_TARGET_HOST"),
        help="Backward-compatible alias for the appliance hostname used in migration and cutover evidence.",
    )
    parser.add_argument("--update-existing", action="store_true", help="Update managed keys in an existing output file.")
    parser.add_argument(
        "--stamp-source",
        action="store_true",
        help="Stamp B1_SOURCE_* metadata from this Git checkout or existing B1_SOURCE_* environment values.",
    )
    parser.add_argument("--source-root", default=".", help="Git checkout used when --stamp-source is set.")
    args = parser.parse_args()
    result = prepare_production_env(
        template=Path(args.template),
        output=Path(args.output),
        docker_socket=Path(args.docker_socket),
        appliance_hostname=args.appliance_hostname,
        expected_target_host=args.expected_target_host,
        update_existing=args.update_existing,
        stamp_source=args.stamp_source,
        source_root=Path(args.source_root),
    )
    action = "created" if result["created"] else "updated"
    keys = ", ".join(result["updated_keys"])
    print(f"{action} {result['output']}")
    print(f"set {keys} from {result['docker_socket']} gid {result['docker_socket_gid']}")
    if result["appliance_hostname"]:
        print(
            f"B1 appliance hostname is {result['appliance_hostname']}; "
            "host IP/gateway/resolver properties remain acquired by DHCP"
        )
    source_control = result["source_control"]
    if source_control["available"]:
        dirty = "dirty" if source_control["source_dirty"] else "clean"
        print(
            f"stamped source {source_control['source_commit'][:12]} "
            f"{source_control['source_ref'] or '(detached)'} ({dirty}, "
            f"{source_control['dirty_path_count']} changed paths)"
        )


if __name__ == "__main__":
    main()
