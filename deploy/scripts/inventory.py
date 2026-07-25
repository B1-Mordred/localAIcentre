#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


CORE_INTENDED_HOSTS = (
    "ai.b1.germering",
    "control.ai.b1.germering",
    "media.ai.b1.germering",
    "comfy.ai.b1.germering",
    "voice.ai.b1.germering",
    "models.ai.b1.germering",
    "api.ai.b1.germering",
)
OPTIONAL_INTENDED_HOSTS = ("monitoring.ai.b1.germering",)
INTENDED_HOSTS = CORE_INTENDED_HOSTS + OPTIONAL_INTENDED_HOSTS

COMMANDS = {
    "docker_ps_all": ["docker", "ps", "-a", "--format", "json"],
    "docker_compose_ls": ["docker", "compose", "ls", "--format", "json"],
    "docker_volume_ls": ["docker", "volume", "ls", "--format", "json"],
    "docker_network_ls": ["docker", "network", "ls", "--format", "json"],
    "docker_info": ["docker", "info", "--format", "{{json .}}"],
    "docker_version": ["docker", "version", "--format", "{{json .}}"],
    "systemd_services": ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"],
    "listening_tcp": ["ss", "-ltnp"],
    "nvidia_smi": [
        "nvidia-smi",
        "--query-gpu=index,name,driver_version,memory.total,memory.used,temperature.gpu,utilization.gpu",
        "--format=csv,noheader,nounits",
    ],
    "nvidia_container_toolkit": ["nvidia-ctk", "--version"],
    "free": ["free", "-m"],
    "df": ["df", "-PT"],
    "mounts": ["findmnt", "--json"],
    "dns_hosts": ["getent", "hosts", *INTENDED_HOSTS],
}

AI_NAME_HINTS = (
    "ollama",
    "open-webui",
    "open_webui",
    "comfy",
    "localai",
    "voicebox",
    "stable-diffusion",
    "stable_diffusion",
    "sd-webui",
    "automatic1111",
    "invokeai",
    "a1111",
)
PRESERVE_HINTS = ("hermes", "yggy", "yggdrasil", "discord", "technitium", "n8n", "mysql", "mariadb", "bragi")
B1_HINTS = ("b1-ai-hub", "b1_ai_hub", "b1-control-plane", "b1-runtime-agent")
SECRET_PATTERNS = [
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(r"\bb1k_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bb1adm_[A-Za-z0-9_-]+\b"),
    re.compile(r"\bb1rt_[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)(=|:)[^\s,;\"']+"),
]
MODEL_FILE_SUFFIXES = {
    ".bin",
    ".ckpt",
    ".engine",
    ".ggml",
    ".gguf",
    ".model",
    ".onnx",
    ".pb",
    ".pt",
    ".pth",
    ".safetensors",
    ".tflite",
}
OPEN_WEBUI_TABLE_COUNT_CANDIDATES = ("user", "chat", "document", "file", "folder", "tag", "model", "config", "feedback")
OPEN_WEBUI_MOUNT_DESTINATION_HINTS = ("/app/backend/data", "/data/open-webui", "/open-webui")
DOCKER_COMPOSE_LABEL_PREFIX = "com.docker.compose."
DOCKER_LABEL_ALLOWLIST = {
    "org.opencontainers.image.title",
    "org.opencontainers.image.version",
    "org.opencontainers.image.source",
}
REVIEW_PORTS = {
    80: "production HTTP gateway",
    443: "production HTTPS gateway",
    3000: "common Open WebUI host port",
    7860: "common Stable Diffusion WebUI host port",
    8000: "common AI/API host port",
    8080: "common web application host port",
    8188: "ComfyUI native or legacy listener",
    8443: "alternate HTTPS gateway",
    11434: "Ollama native API",
    11438: "Ollama/OpenAI-compatible proxy",
}
INITIAL_PROFILE_NAME = "rtx3060-32gb-initial"
MIN_INITIAL_GPU_VRAM_MIB = 12 * 1024
MIN_INITIAL_HOST_RAM_MIB = 32000
DOCKER_SOCKET_PATH = Path("/var/run/docker.sock")
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(redact_match, redacted)
    return redacted


def redact_match(match: re.Match[str]) -> str:
    if match.lastindex == 1:
        return match.group(1) + "<redacted>"
    if match.lastindex and match.lastindex >= 2:
        return f"{match.group(1)}{match.group(2)}<redacted>"
    return "<redacted>"


def ensure_private_missing_parents(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        directory.mkdir(mode=PRIVATE_DIR_MODE)
        if os.name != "nt":
            directory.chmod(PRIVATE_DIR_MODE)


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_missing_parents(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        if os.name != "nt":
            os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    finally:
        if fd >= 0:
            os.close(fd)
    if os.name != "nt":
        path.chmod(PRIVATE_FILE_MODE)


def absolute_path_without_symlink_resolution(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(os.fspath(path)))


def run(command: list[str]) -> dict[str, Any]:
    if shutil.which(command[0]) is None:
        return {"available": False, "command": command, "stdout": "", "stderr": "command not found", "returncode": 127}
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    return {
        "available": True,
        "command": command,
        "stdout": redact_text(completed.stdout),
        "stderr": redact_text(completed.stderr),
        "returncode": completed.returncode,
    }


def parse_json_lines(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            rows.append({"raw": line})
            continue
        rows.append(item if isinstance(item, dict) else {"value": item})
    return rows


def parse_json_object(output: str) -> dict[str, Any]:
    try:
        item = json.loads(output)
    except json.JSONDecodeError:
        return {}
    return item if isinstance(item, dict) else {}


def parse_json_payload(output: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def classify_text(text: str) -> dict[str, Any]:
    lowered = text.lower()
    matched_b1 = [hint for hint in B1_HINTS if hint in lowered]
    matched_preserve = [hint for hint in PRESERVE_HINTS if hint in lowered]
    matched_ai = [hint for hint in AI_NAME_HINTS if hint in lowered]
    if matched_b1:
        return {
            "classification": "b1-ai-hub-current-preserve",
            "confidence": "high",
            "reasons": [f"matches current B1 AI Hub hint: {hint}" for hint in matched_b1],
        }
    if matched_preserve:
        return {
            "classification": "preserve-unrelated",
            "confidence": "high",
            "reasons": [f"matches explicit preserve hint: {hint}" for hint in matched_preserve],
        }
    if matched_ai:
        return {
            "classification": "candidate-old-ai-stack-review-required",
            "confidence": "medium",
            "reasons": [f"matches AI-stack hint: {hint}" for hint in matched_ai],
        }
    return {
        "classification": "unknown-preserve-by-default",
        "confidence": "low",
        "reasons": ["no AI-stack hint matched; preserve unless an operator explicitly marks it in scope"],
    }


def classify_container(row: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(row.get(key, "")) for key in ("Names", "Name", "Image", "Command", "Labels")).lower()
    result = classify_text(text)
    return {
        "container": row.get("Names") or row.get("Name") or row.get("ID") or row,
        "image": row.get("Image"),
        "ports": row.get("Ports"),
        **result,
    }


def classify_compose_project(row: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(row.get(key, "")) for key in ("Name", "Status", "ConfigFiles", "WorkingDir")).lower()
    return {
        "project": row.get("Name") or row,
        "status": row.get("Status"),
        "config_files": row.get("ConfigFiles"),
        **classify_text(text),
    }


def classify_systemd_service(row: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(row.get(key, "")) for key in ("unit", "description", "load_state", "active_state", "sub_state")).lower()
    return {
        "service": row.get("unit") or row,
        "description": row.get("description"),
        "load_state": row.get("load_state"),
        "active_state": row.get("active_state"),
        "sub_state": row.get("sub_state"),
        **classify_text(text),
    }


def parse_systemd_services(output: str) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.endswith("loaded units listed."):
            continue
        line = line.lstrip("●* ").strip()
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        description = parts[4] if len(parts) > 4 else ""
        services.append(
            {
                "unit": parts[0],
                "load_state": parts[1],
                "active_state": parts[2],
                "sub_state": parts[3],
                "description": redact_text(description),
            }
        )
    return services


def parse_systemctl_show(output: str) -> dict[str, Any]:
    allowed = {
        "Id": "id",
        "Names": "names",
        "Description": "description",
        "LoadState": "load_state",
        "ActiveState": "active_state",
        "SubState": "sub_state",
        "FragmentPath": "fragment_path",
        "DropInPaths": "drop_in_paths",
        "ExecStart": "exec_start",
        "User": "user",
        "Group": "group",
    }
    record: dict[str, Any] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        mapped = allowed.get(key)
        if mapped is None:
            continue
        redacted = redact_text(value.strip())[:1000]
        if mapped in {"names", "drop_in_paths"}:
            record[mapped] = [item for item in redacted.split() if item]
        else:
            record[mapped] = redacted
    return record


def inspect_systemd_services(rows: list[dict[str, Any]], command_runner: Callable[[list[str]], dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        classified = classify_systemd_service(row)
        if classified.get("classification") == "unknown-preserve-by-default":
            continue
        unit = row.get("unit")
        if not isinstance(unit, str) or not unit:
            continue
        result = command_runner(
            [
                "systemctl",
                "show",
                unit,
                "--property=Id,Names,Description,LoadState,ActiveState,SubState,FragmentPath,DropInPaths,ExecStart,User,Group",
                "--no-pager",
            ]
        )
        record: dict[str, Any] = {
            "service": unit,
            "available": bool(result.get("available")),
            "returncode": result.get("returncode"),
            "classification": classified.get("classification"),
            "confidence": classified.get("confidence"),
            "reasons": classified.get("reasons"),
        }
        if result.get("available") and result.get("returncode") == 0:
            record.update(parse_systemctl_show(str(result.get("stdout") or "")))
        else:
            record["error"] = redact_text(str(result.get("stderr") or "systemctl show unavailable"))[:500]
        records.append(record)
    return records


def parse_listening_tcp(output: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("State"):
            continue
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        local = parts[3]
        process = parts[5] if len(parts) > 5 else ""
        host, port = split_host_port(local)
        entries.append({"local_address": host, "port": port, "process": process})
    return entries


def split_host_port(value: str) -> tuple[str, int | None]:
    if value.startswith("[") and "]:" in value:
        host, raw_port = value.rsplit("]:", 1)
        return host.lstrip("["), parse_int(raw_port)
    if ":" in value:
        host, raw_port = value.rsplit(":", 1)
        return host, parse_int(raw_port)
    return value, None


def parse_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_nvidia_smi(output: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            devices.append({"raw": line})
            continue
        devices.append(
            {
                "index": parse_int(parts[0]),
                "name": parts[1],
                "driver_version": parts[2],
                "memory_total_mib": parse_int(parts[3]),
                "memory_used_mib": parse_int(parts[4]),
                "temperature_c": parse_int(parts[5]),
                "utilization_percent": parse_int(parts[6]),
            }
        )
    return devices


def parse_free_mib(output: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        label = parts[0].rstrip(":").lower()
        if label in {"mem", "swap"} and len(parts) >= 7:
            result[label] = {
                "total_mib": parse_int(parts[1]),
                "used_mib": parse_int(parts[2]),
                "free_mib": parse_int(parts[3]),
                "available_mib": parse_int(parts[6]) if label == "mem" else None,
            }
    return result


def parse_df(output: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        entries.append(
            {
                "filesystem": parts[0],
                "type": parts[1],
                "blocks_1k": parse_int(parts[2]),
                "used_1k": parse_int(parts[3]),
                "available_1k": parse_int(parts[4]),
                "use_percent": parts[5],
                "mountpoint": " ".join(parts[6:]),
            }
        )
    return entries


def parse_dns_hosts(output: str) -> dict[str, list[str]]:
    records: dict[str, list[str]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        address = parts[0]
        for host in parts[1:]:
            records.setdefault(host, []).append(address)
    return records


def parse_docker_info(output: str) -> dict[str, Any]:
    info = parse_json_object(output)
    runtimes = info.get("Runtimes") if isinstance(info.get("Runtimes"), dict) else {}
    return {
        "server_version": info.get("ServerVersion"),
        "operating_system": info.get("OperatingSystem"),
        "architecture": info.get("Architecture"),
        "driver": info.get("Driver"),
        "runtimes": sorted(runtimes.keys()),
        "nvidia_runtime_available": "nvidia" in runtimes,
        "default_runtime": info.get("DefaultRuntime"),
    }


def docker_resource_identifier(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def safe_docker_labels(labels: Any) -> dict[str, str]:
    if not isinstance(labels, dict):
        return {}
    safe: dict[str, str] = {}
    for key, value in labels.items():
        label = str(key)
        if label.startswith(DOCKER_COMPOSE_LABEL_PREFIX) or label in DOCKER_LABEL_ALLOWLIST:
            safe[label] = redact_text(str(value))[:500]
    return dict(sorted(safe.items()))


def safe_docker_mounts(mounts: Any) -> list[dict[str, Any]]:
    if not isinstance(mounts, list):
        return []
    safe: list[dict[str, Any]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        safe.append(
            {
                "type": mount.get("Type"),
                "name": mount.get("Name"),
                "source": mount.get("Source"),
                "destination": mount.get("Destination"),
                "driver": mount.get("Driver"),
                "mode": mount.get("Mode"),
                "rw": mount.get("RW"),
                "propagation": mount.get("Propagation"),
            }
        )
    return safe


def safe_container_inspect(item: dict[str, Any], identifier: str) -> dict[str, Any]:
    config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
    state = item.get("State") if isinstance(item.get("State"), dict) else {}
    network_settings = item.get("NetworkSettings") if isinstance(item.get("NetworkSettings"), dict) else {}
    networks = network_settings.get("Networks") if isinstance(network_settings.get("Networks"), dict) else {}
    return {
        "identifier": identifier,
        "id": item.get("Id"),
        "name": str(item.get("Name") or "").lstrip("/"),
        "image": config.get("Image") or item.get("Image"),
        "labels": safe_docker_labels(config.get("Labels")),
        "state": {
            "status": state.get("Status"),
            "running": state.get("Running"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
        },
        "mounts": safe_docker_mounts(item.get("Mounts")),
        "networks": sorted(str(name) for name in networks.keys()),
    }


def safe_volume_inspect(item: dict[str, Any], identifier: str) -> dict[str, Any]:
    return {
        "identifier": identifier,
        "name": item.get("Name"),
        "driver": item.get("Driver"),
        "mountpoint": item.get("Mountpoint"),
        "scope": item.get("Scope"),
        "labels": safe_docker_labels(item.get("Labels")),
    }


def parse_first_inspect_object(output: str) -> dict[str, Any]:
    payload = parse_json_payload(output)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return {}


def inspect_docker_containers(rows: list[dict[str, Any]], command_runner: Callable[[list[str]], dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        identifier = docker_resource_identifier(row, ("ID", "Names", "Name"))
        if not identifier:
            continue
        result = command_runner(["docker", "container", "inspect", identifier])
        record: dict[str, Any] = {
            "identifier": identifier,
            "available": bool(result.get("available")),
            "returncode": result.get("returncode"),
        }
        if result.get("available") and result.get("returncode") == 0:
            inspected = parse_first_inspect_object(str(result.get("stdout") or ""))
            record.update(safe_container_inspect(inspected, identifier) if inspected else {"error": "invalid inspect JSON"})
        else:
            record["error"] = redact_text(str(result.get("stderr") or "inspect unavailable"))[:500]
        records.append(record)
    return records


def inspect_docker_volumes(rows: list[dict[str, Any]], command_runner: Callable[[list[str]], dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        identifier = docker_resource_identifier(row, ("Name",))
        if not identifier:
            continue
        result = command_runner(["docker", "volume", "inspect", identifier])
        record: dict[str, Any] = {
            "identifier": identifier,
            "available": bool(result.get("available")),
            "returncode": result.get("returncode"),
        }
        if result.get("available") and result.get("returncode") == 0:
            inspected = parse_first_inspect_object(str(result.get("stdout") or ""))
            record.update(safe_volume_inspect(inspected, identifier) if inspected else {"error": "invalid inspect JSON"})
        else:
            record["error"] = redact_text(str(result.get("stderr") or "inspect unavailable"))[:500]
        records.append(record)
    return records


def path_type(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


def summarize_path(path: Path, *, sample_limit: int = 25) -> dict[str, Any]:
    summary: dict[str, Any] = {"path": str(path)}
    try:
        stat = path.lstat()
    except FileNotFoundError:
        summary["exists"] = False
        return summary
    except OSError as exc:
        summary.update({"exists": None, "error": f"{exc.__class__.__name__}: {exc}"})
        return summary
    summary["exists"] = True
    try:
        summary.update({"type": path_type(path), "mode": oct(stat.st_mode & 0o777), "uid": stat.st_uid, "gid": stat.st_gid})
        if path.is_symlink():
            summary["target"] = os.readlink(path)
        if path.is_dir():
            entries = sorted(path.iterdir(), key=lambda item: item.name.lower())
            summary["entry_count"] = len(entries)
            summary["sample_entries"] = [entry.name for entry in entries[:sample_limit]]
        elif path.is_file():
            summary["size_bytes"] = stat.st_size
    except OSError as exc:
        summary["error"] = f"{exc.__class__.__name__}: {exc}"
    return summary


def is_model_file(path: Path) -> bool:
    if path.suffix.lower() in MODEL_FILE_SUFFIXES:
        return True
    lowered = path.as_posix().lower()
    return "/blobs/sha256-" in lowered or lowered.endswith("/modelfile")


def summarize_model_directory(path: Path, *, max_depth: int = 8, max_files: int = 20000, sample_limit: int = 25) -> dict[str, Any]:
    summary = summarize_path(path, sample_limit=sample_limit)
    if not summary.get("exists") or summary.get("type") != "directory":
        return summary
    root_depth = len(path.parts)
    file_count = 0
    directory_count = 0
    symlink_count = 0
    special_count = 0
    total_size = 0
    model_file_count = 0
    model_size = 0
    suffix_counts: dict[str, int] = {}
    model_samples: list[dict[str, Any]] = []
    errors: list[str] = []
    truncated = False
    try:
        for current, dirs, files in os.walk(path, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            if depth >= max_depth:
                if dirs:
                    truncated = True
                dirs[:] = []
            safe_dirs: list[str] = []
            for dirname in dirs:
                child = current_path / dirname
                if child.is_symlink():
                    symlink_count += 1
                    continue
                safe_dirs.append(dirname)
                directory_count += 1
            dirs[:] = safe_dirs
            for filename in files:
                file_path = current_path / filename
                try:
                    if file_path.is_symlink():
                        symlink_count += 1
                        continue
                    if not file_path.is_file():
                        special_count += 1
                        continue
                    stat = file_path.stat()
                except OSError as exc:
                    errors.append(f"{file_path}: {exc.__class__.__name__}: {exc}")
                    continue
                file_count += 1
                total_size += stat.st_size
                suffix = file_path.suffix.lower() or "<none>"
                suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
                if is_model_file(file_path):
                    model_file_count += 1
                    model_size += stat.st_size
                    if len(model_samples) < sample_limit:
                        model_samples.append(
                            {
                                "relative_path": file_path.relative_to(path).as_posix(),
                                "size_bytes": stat.st_size,
                                "suffix": suffix,
                            }
                        )
                if file_count >= max_files:
                    truncated = True
                    dirs[:] = []
                    break
            if file_count >= max_files:
                break
    except OSError as exc:
        errors.append(f"{path}: {exc.__class__.__name__}: {exc}")
    summary.update(
        {
            "scan": {
                "max_depth": max_depth,
                "max_files": max_files,
                "truncated": truncated,
                "file_count": file_count,
                "directory_count": directory_count,
                "symlink_count": symlink_count,
                "special_count": special_count,
                "total_size_bytes": total_size,
                "model_file_count": model_file_count,
                "model_size_bytes": model_size,
                "suffix_counts": dict(sorted(suffix_counts.items())),
                "model_file_samples": model_samples,
                "errors": errors[:20],
            }
        }
    )
    return summary


def sqlite_readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def inspect_sqlite_database(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {"readable": False, "reason": "not_regular_file"}
    try:
        connection = sqlite3.connect(sqlite_readonly_uri(path), uri=True, timeout=1.0)
    except sqlite3.Error as exc:
        return {"readable": False, "error": f"{exc.__class__.__name__}: {exc}"}
    try:
        cursor = connection.cursor()
        user_version = cursor.execute("PRAGMA user_version").fetchone()[0]
        page_count = cursor.execute("PRAGMA page_count").fetchone()[0]
        page_size = cursor.execute("PRAGMA page_size").fetchone()[0]
        rows = cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = [str(row[0]) for row in rows if row and row[0] is not None]
        table_counts: dict[str, int] = {}
        for table in OPEN_WEBUI_TABLE_COUNT_CANDIDATES:
            if table not in tables:
                continue
            count_row = cursor.execute(f"SELECT COUNT(*) FROM {quote_sqlite_identifier(table)}").fetchone()
            table_counts[table] = int(count_row[0]) if count_row else 0
        return {
            "readable": True,
            "user_version": int(user_version),
            "page_count": int(page_count),
            "page_size": int(page_size),
            "estimated_size_bytes": int(page_count) * int(page_size),
            "tables": tables,
            "table_counts": table_counts,
            "content_rows_read": False,
        }
    except sqlite3.Error as exc:
        return {"readable": False, "error": f"{exc.__class__.__name__}: {exc}"}
    finally:
        connection.close()


def summarize_open_webui_database(path: Path) -> dict[str, Any]:
    summary = summarize_path(path, sample_limit=0)
    if summary.get("exists") and summary.get("type") == "file":
        summary["sqlite"] = inspect_sqlite_database(path)
    return summary


def analyze_listening_tcp(entries: list[dict[str, Any]]) -> dict[str, Any]:
    review: list[dict[str, Any]] = []
    production_gateway_listeners: list[dict[str, Any]] = []
    legacy_comfy_listeners: list[dict[str, Any]] = []
    ai_service_listeners: list[dict[str, Any]] = []
    for entry in entries:
        port = entry.get("port")
        process = str(entry.get("process") or "")
        classification = classify_text(process)
        purpose = REVIEW_PORTS.get(port)
        if port in {80, 443}:
            production_gateway_listeners.append(entry)
        if port == 8188:
            legacy_comfy_listeners.append(entry)
        if purpose or classification["classification"] == "candidate-old-ai-stack-review-required":
            row = {**entry, "purpose": purpose or "AI process hint", "classification": classification["classification"]}
            review.append(row)
            ai_service_listeners.append(row)
    return {
        "review_ports": REVIEW_PORTS,
        "ports_requiring_review": sorted({item["port"] for item in review if item.get("port") is not None}),
        "production_gateway_listeners": production_gateway_listeners,
        "legacy_comfy_listeners": legacy_comfy_listeners,
        "ai_service_listeners": ai_service_listeners,
    }


def summarize_model_storage(model_directories: list[dict[str, Any]]) -> dict[str, Any]:
    existing = [item for item in model_directories if item.get("exists") and item.get("type") == "directory"]
    total_model_size = 0
    total_model_files = 0
    truncated = False
    for item in existing:
        scan = item.get("scan") if isinstance(item.get("scan"), dict) else {}
        total_model_size += int(scan.get("model_size_bytes") or 0)
        total_model_files += int(scan.get("model_file_count") or 0)
        truncated = truncated or bool(scan.get("truncated"))
    return {
        "existing_directory_count": len(existing),
        "model_file_count": total_model_files,
        "model_size_bytes": total_model_size,
        "scan_truncated": truncated,
    }


def summarize_open_webui_inventory(databases: list[dict[str, Any]]) -> dict[str, Any]:
    existing = [item for item in databases if item.get("exists") and item.get("type") == "file"]
    readable = [item for item in existing if isinstance(item.get("sqlite"), dict) and item["sqlite"].get("readable")]
    filename_counts: dict[str, int] = {}
    for item in readable:
        if isinstance(item.get("path"), str):
            name = Path(item["path"]).name
            filename_counts[name] = filename_counts.get(name, 0) + 1
    known_table_counts: dict[str, dict[str, int]] = {}
    known_table_counts_by_path: dict[str, dict[str, int]] = {}
    for item in readable:
        if not isinstance(item.get("path"), str):
            continue
        path = item["path"]
        name = Path(path).name
        table_counts = item["sqlite"].get("table_counts", {})
        key = path if filename_counts.get(name, 0) > 1 else name
        known_table_counts[key] = table_counts
        known_table_counts_by_path[path] = table_counts
    return {
        "database_candidate_count": len(existing),
        "readable_sqlite_count": len(readable),
        "known_table_counts": known_table_counts,
        "known_table_counts_by_path": known_table_counts_by_path,
        "content_rows_read": False,
    }


def summarize_open_webui_data_roots(roots: list[dict[str, Any]]) -> dict[str, Any]:
    unreadable = [
        {
            "path": item.get("path"),
            "reason": item.get("error") or "not readable by current user",
        }
        for item in roots
        if item.get("exists") is None or (item.get("exists") is True and item.get("type") != "directory")
    ]
    existing_directories = [item for item in roots if item.get("exists") is True and item.get("type") == "directory"]
    return {
        "candidate_count": len(roots),
        "existing_directory_count": len(existing_directories),
        "unreadable_or_unscannable": unreadable,
    }


def summarize_hardware_profile(gpu_devices: list[dict[str, Any]], memory: dict[str, Any]) -> dict[str, Any]:
    gpu_totals = [item.get("memory_total_mib") for item in gpu_devices if isinstance(item.get("memory_total_mib"), int)]
    largest_gpu_vram_mib = max(gpu_totals) if gpu_totals else None
    mem = memory.get("mem") if isinstance(memory.get("mem"), dict) else {}
    host_total_ram_mib = mem.get("total_mib") if isinstance(mem.get("total_mib"), int) else None
    host_available_ram_mib = mem.get("available_mib") if isinstance(mem.get("available_mib"), int) else None
    gpu_ok = largest_gpu_vram_mib is not None and largest_gpu_vram_mib >= MIN_INITIAL_GPU_VRAM_MIB
    ram_ok = host_total_ram_mib is not None and host_total_ram_mib >= MIN_INITIAL_HOST_RAM_MIB
    warnings: list[str] = []
    if not gpu_ok:
        observed = "unavailable" if largest_gpu_vram_mib is None else f"{largest_gpu_vram_mib} MiB"
        warnings.append(f"largest detected GPU VRAM is {observed}; required initial profile needs at least {MIN_INITIAL_GPU_VRAM_MIB} MiB")
    if not ram_ok:
        observed = "unavailable" if host_total_ram_mib is None else f"{host_total_ram_mib} MiB"
        warnings.append(f"detected host RAM is {observed}; required initial profile needs at least {MIN_INITIAL_HOST_RAM_MIB} MiB")
    return {
        "profile": INITIAL_PROFILE_NAME,
        "accepted": gpu_ok and ram_ok,
        "minimum_gpu_vram_mib": MIN_INITIAL_GPU_VRAM_MIB,
        "minimum_host_ram_mib": MIN_INITIAL_HOST_RAM_MIB,
        "detected_gpu_count": len(gpu_devices),
        "largest_gpu_vram_mib": largest_gpu_vram_mib,
        "host_total_ram_mib": host_total_ram_mib,
        "host_available_ram_mib": host_available_ram_mib,
        "warnings": warnings,
    }


def summarize_gpu_container_runtime(
    *,
    gpu_devices: list[dict[str, Any]],
    nvidia_smi_available: bool,
    docker_info: dict[str, Any],
    nvidia_toolkit: dict[str, Any],
) -> dict[str, Any]:
    docker_nvidia_runtime_available = docker_info.get("nvidia_runtime_available") is True
    toolkit_available = nvidia_toolkit.get("available") is True and nvidia_toolkit.get("returncode") == 0
    detected_gpu_count = len(gpu_devices)
    warnings: list[str] = []
    if not nvidia_smi_available:
        warnings.append("nvidia-smi is unavailable or failed; NVIDIA driver/NVML readiness is not proven")
    if detected_gpu_count <= 0:
        warnings.append("no NVIDIA GPU was detected by nvidia-smi")
    if not docker_nvidia_runtime_available:
        warnings.append("Docker does not report an nvidia runtime; NVIDIA Container Toolkit is not wired into Docker")
    if not toolkit_available:
        reason = "command missing" if nvidia_toolkit.get("available") is not True else f"returncode {nvidia_toolkit.get('returncode')}"
        warnings.append(f"nvidia-ctk is not available for verification ({reason})")
    return {
        "available": True,
        "accepted": not warnings,
        "nvidia_smi_available": nvidia_smi_available,
        "detected_gpu_count": detected_gpu_count,
        "docker_nvidia_runtime_available": docker_nvidia_runtime_available,
        "docker_runtimes": docker_info.get("runtimes") if isinstance(docker_info.get("runtimes"), list) else [],
        "docker_default_runtime": docker_info.get("default_runtime"),
        "nvidia_container_toolkit_available": toolkit_available,
        "nvidia_container_toolkit_returncode": nvidia_toolkit.get("returncode"),
        "nvidia_container_toolkit_version": str(nvidia_toolkit.get("version") or ""),
        "operator_must_review_gpu_runtime": bool(warnings),
        "warnings": warnings,
    }


def parse_gid(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value, 10)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def inspect_docker_socket(path: Path = DOCKER_SOCKET_PATH, configured_gid: str | None = None) -> dict[str, Any]:
    configured = configured_gid if configured_gid is not None else os.getenv("B1_DOCKER_GID")
    configured_gid_int = parse_gid(configured)
    warnings: list[str] = []
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "is_socket": False,
        "uid": None,
        "gid": None,
        "mode_octal": None,
        "group_readable": False,
        "group_writable": False,
        "configured_gid": configured,
        "configured_gid_valid": configured_gid_int is not None,
        "configured_gid_matches": False,
        "runtime_agent_group_access_ready": False,
        "warnings": warnings,
    }
    try:
        st = path.stat()
    except FileNotFoundError:
        warnings.append(f"Docker socket {path} is missing; runtime-agent Docker status, logs, and recovery actions will be unavailable")
        return result
    except OSError as exc:
        warnings.append(f"Docker socket {path} could not be inspected: {exc.__class__.__name__}: {exc}")
        return result

    mode = stat.S_IMODE(st.st_mode)
    is_socket = stat.S_ISSOCK(st.st_mode)
    group_readable = bool(mode & stat.S_IRGRP)
    group_writable = bool(mode & stat.S_IWGRP)
    configured_matches = configured_gid_int == st.st_gid
    result.update(
        {
            "exists": True,
            "is_socket": is_socket,
            "uid": st.st_uid,
            "gid": st.st_gid,
            "mode_octal": f"{mode:04o}",
            "group_readable": group_readable,
            "group_writable": group_writable,
            "configured_gid_matches": configured_matches,
            "runtime_agent_group_access_ready": is_socket and group_readable and group_writable and configured_matches,
        }
    )
    if not is_socket:
        warnings.append(f"{path} exists but is not a Unix socket")
    if not group_readable or not group_writable:
        warnings.append(f"Docker socket {path} does not grant group read/write access; runtime-agent should not rely on group_add alone")
    if configured is None or not configured.strip():
        warnings.append(f"B1_DOCKER_GID is not set; set it to the Docker socket GID {st.st_gid} before target-host acceptance")
    elif configured_gid_int is None:
        warnings.append("B1_DOCKER_GID is not a non-negative numeric GID")
    elif not configured_matches:
        warnings.append(f"B1_DOCKER_GID={configured_gid_int} does not match Docker socket GID {st.st_gid}")
    return result


def default_model_path_candidates(b1_root: Path) -> list[Path]:
    candidates = [
        b1_root / "models",
        Path("/usr/share/ollama/.ollama/models"),
        Path("/var/lib/ollama/.ollama/models"),
        Path("/var/lib/ollama/models"),
        Path("/srv/models"),
        Path("/srv/comfyui/models"),
        Path("/opt/ComfyUI/models"),
        Path("/opt/stable-diffusion-webui/models"),
    ]
    home_root = Path("/home")
    if home_root.exists():
        for home in sorted(home_root.iterdir(), key=lambda item: item.name.lower()):
            candidates.append(home / ".ollama" / "models")
            candidates.append(home / "ComfyUI" / "models")
    return unique_paths(candidates)


def default_open_webui_candidates(b1_root: Path) -> list[Path]:
    return unique_paths(
        [
            b1_root / "data/open-webui",
            Path("/srv/open-webui"),
            Path("/srv/open-webui/data"),
            Path("/opt/open-webui"),
            Path("/app/backend/data"),
        ]
    )


def path_from_string(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.startswith("/"):
        return None
    return Path(value)


def record_text_for_mount_hints(record: dict[str, Any]) -> str:
    labels = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    return " ".join(
        [
            str(record.get("identifier") or ""),
            str(record.get("name") or ""),
            str(record.get("image") or ""),
            " ".join(f"{key}={value}" for key, value in labels.items()),
        ]
    ).lower()


def mount_text(mount: dict[str, Any]) -> str:
    return " ".join(str(mount.get(key) or "") for key in ("type", "name", "source", "destination")).lower()


def docker_open_webui_path_candidates(
    container_inspects: list[dict[str, Any]], volume_inspects: list[dict[str, Any]]
) -> list[Path]:
    candidates: list[Path] = []
    volume_mountpoints = {
        str(item.get("name") or item.get("identifier")): item.get("mountpoint")
        for item in volume_inspects
        if isinstance(item.get("mountpoint"), str)
    }
    for record in container_inspects:
        record_text = record_text_for_mount_hints(record)
        if "open-webui" not in record_text and "open_webui" not in record_text:
            continue
        for mount in record.get("mounts") if isinstance(record.get("mounts"), list) else []:
            if not isinstance(mount, dict):
                continue
            lowered = mount_text(mount)
            destination = str(mount.get("destination") or "").lower()
            if not (
                any(destination.startswith(hint) for hint in OPEN_WEBUI_MOUNT_DESTINATION_HINTS)
                or "open-webui" in lowered
                or "open_webui" in lowered
            ):
                continue
            source = path_from_string(mount.get("source"))
            if source is not None:
                candidates.append(source)
                continue
            mountpoint = path_from_string(volume_mountpoints.get(str(mount.get("name") or "")))
            if mountpoint is not None:
                candidates.append(mountpoint)
    for volume in volume_inspects:
        text = " ".join(str(volume.get(key) or "") for key in ("identifier", "name", "mountpoint")).lower()
        if "open-webui" in text or "open_webui" in text:
            mountpoint = path_from_string(volume.get("mountpoint"))
            if mountpoint is not None:
                candidates.append(mountpoint)
    return unique_paths(candidates)


def docker_model_path_candidates(container_inspects: list[dict[str, Any]], volume_inspects: list[dict[str, Any]]) -> list[Path]:
    candidates: list[Path] = []
    volume_mountpoints = {
        str(item.get("name") or item.get("identifier")): item.get("mountpoint")
        for item in volume_inspects
        if isinstance(item.get("mountpoint"), str)
    }
    for record in container_inspects:
        record_text = record_text_for_mount_hints(record)
        record_is_ai = classify_text(record_text)["classification"] in {
            "candidate-old-ai-stack-review-required",
            "b1-ai-hub-current-preserve",
        }
        for mount in record.get("mounts") if isinstance(record.get("mounts"), list) else []:
            if not isinstance(mount, dict):
                continue
            lowered = mount_text(mount)
            if "model" not in lowered and not (record_is_ai and "/models" in lowered):
                continue
            source = path_from_string(mount.get("source"))
            if source is not None:
                candidates.append(source)
                continue
            mountpoint = path_from_string(volume_mountpoints.get(str(mount.get("name") or "")))
            if mountpoint is not None:
                candidates.append(mountpoint)
    for volume in volume_inspects:
        text = " ".join(str(volume.get(key) or "") for key in ("identifier", "name", "mountpoint")).lower()
        if "model" in text:
            mountpoint = path_from_string(volume.get("mountpoint"))
            if mountpoint is not None:
                candidates.append(mountpoint)
    return unique_paths(candidates)


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def find_named_files(roots: list[Path], names: set[str], *, max_depth: int = 5, max_results: int = 200) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    ignored_dirs = {".cache", ".git", "node_modules", "__pycache__", "backups", "cache"}
    for root in roots:
        if len(matches) >= max_results or not root.exists() or not root.is_dir():
            continue
        root_depth = len(root.parts)
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            dirs[:] = [item for item in dirs if item not in ignored_dirs and depth < max_depth]
            for filename in files:
                if filename in names or any(filename.endswith(suffix[1:]) for suffix in names if suffix.startswith("*")):
                    matches.append(summarize_path(current_path / filename, sample_limit=0))
                    if len(matches) >= max_results:
                        return matches
    return matches


def read_resolv_conf() -> dict[str, Any]:
    path = Path("/etc/resolv.conf")
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        lines = [redact_text(line.strip()) for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError as exc:
        return {"path": str(path), "exists": True, "error": f"{exc.__class__.__name__}: {exc}"}
    return {"path": str(path), "exists": True, "lines": [line for line in lines if line and not line.startswith("#")]}


def build_inventory(
    *,
    b1_root: Path = Path("/srv/b1-ai-hub"),
    scan_roots: list[Path] | None = None,
    docker_socket_path: Path = DOCKER_SOCKET_PATH,
    configured_docker_gid: str | None = None,
    command_runner: Callable[[list[str]], dict[str, Any]] = run,
    now: datetime | None = None,
) -> dict[str, Any]:
    captured = {name: command_runner(command) for name, command in COMMANDS.items()}
    docker_rows = parse_json_lines(captured["docker_ps_all"]["stdout"])
    compose_rows = parse_json_lines(captured["docker_compose_ls"]["stdout"])
    volume_rows = parse_json_lines(captured["docker_volume_ls"]["stdout"])
    network_rows = parse_json_lines(captured["docker_network_ls"]["stdout"])
    systemd_rows = parse_systemd_services(captured["systemd_services"]["stdout"])
    container_inspects = inspect_docker_containers(docker_rows, command_runner)
    volume_inspects = inspect_docker_volumes(volume_rows, command_runner)
    systemd_service_inspects = inspect_systemd_services(systemd_rows, command_runner)
    roots = scan_roots if scan_roots is not None else [Path("/srv"), Path("/opt"), Path("/home")]
    created_at = now or datetime.now(tz=UTC)

    container_classifications = [classify_container(row) for row in docker_rows]
    compose_classifications = [classify_compose_project(row) for row in compose_rows]
    systemd_classifications = [classify_systemd_service(row) for row in systemd_rows]
    old_stack_candidates = [item for item in container_classifications if item["classification"] == "candidate-old-ai-stack-review-required"]
    old_stack_systemd_candidates = [
        item for item in systemd_classifications if item["classification"] == "candidate-old-ai-stack-review-required"
    ]
    explicit_preserve = [item for item in container_classifications if item["classification"] in {"preserve-unrelated", "unknown-preserve-by-default"}]
    explicit_systemd_preserve = [
        item for item in systemd_classifications if item["classification"] in {"preserve-unrelated", "unknown-preserve-by-default"}
    ]
    listening_tcp = parse_listening_tcp(captured["listening_tcp"]["stdout"])
    model_path_candidates = unique_paths(default_model_path_candidates(b1_root) + docker_model_path_candidates(container_inspects, volume_inspects))
    open_webui_path_candidates = unique_paths(default_open_webui_candidates(b1_root) + docker_open_webui_path_candidates(container_inspects, volume_inspects))
    open_webui_data_roots = [summarize_path(path) for path in open_webui_path_candidates]
    model_directories = [summarize_model_directory(path) for path in model_path_candidates]
    open_webui_databases = [summarize_open_webui_database(Path(item["path"])) for item in find_named_files(open_webui_path_candidates, {"webui.db", "database.sqlite", "*.db"})]

    gpu_devices = parse_nvidia_smi(captured["nvidia_smi"]["stdout"])
    host_memory = parse_free_mib(captured["free"]["stdout"])
    docker_info = parse_docker_info(captured["docker_info"]["stdout"])
    nvidia_smi_available = captured["nvidia_smi"]["available"] and captured["nvidia_smi"]["returncode"] == 0
    nvidia_toolkit = {
        "available": captured["nvidia_container_toolkit"]["available"],
        "returncode": captured["nvidia_container_toolkit"]["returncode"],
        "version": captured["nvidia_container_toolkit"]["stdout"].strip(),
    }
    docker_socket = inspect_docker_socket(docker_socket_path, configured_docker_gid)

    return {
        "created_at": created_at.astimezone(UTC).isoformat(),
        "format": "b1-ai-hub-host-inventory/v1",
        "warning": "read-only inventory; review classifications before any migration or cutover; unknown resources are preserved by default",
        "safety": {
            "read_only": True,
            "destructive_actions": False,
            "unknown_preserve_by_default": True,
            "old_stack_classification_requires_operator_review": True,
        },
        "commands": captured,
        "docker": {
            "containers": docker_rows,
            "compose_projects": compose_rows,
            "volumes": volume_rows,
            "networks": network_rows,
            "container_inspects": container_inspects,
            "volume_inspects": volume_inspects,
            "info": docker_info,
            "version": parse_json_object(captured["docker_version"]["stdout"]),
            "socket": docker_socket,
        },
        "host": {
            "systemd_services": systemd_rows,
            "systemd_service_inspects": systemd_service_inspects,
            "listening_tcp": listening_tcp,
            "gpu": {
                "devices": gpu_devices,
                "nvidia_smi_available": nvidia_smi_available,
            },
            "nvidia_container_toolkit": nvidia_toolkit,
            "memory": host_memory,
            "disks": parse_df(captured["df"]["stdout"]),
            "mounts": parse_json_object(captured["mounts"]["stdout"]),
            "dns": {
                "intended_hosts": list(INTENDED_HOSTS),
                "core_hosts": list(CORE_INTENDED_HOSTS),
                "optional_hosts": list(OPTIONAL_INTENDED_HOSTS),
                "records": parse_dns_hosts(captured["dns_hosts"]["stdout"]),
                "resolv_conf": read_resolv_conf(),
            },
        },
        "paths": {
            "b1_root": summarize_path(b1_root),
            "model_directories": model_directories,
            "open_webui_data_candidates": [summarize_path(path) for path in default_open_webui_candidates(b1_root)],
            "open_webui_data_roots": open_webui_data_roots,
            "compose_file_candidates": find_named_files(roots, {"compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"}),
            "open_webui_database_candidates": open_webui_databases,
        },
        "migration_readiness": {
            "port_review": analyze_listening_tcp(listening_tcp),
            "hardware_profile": summarize_hardware_profile(gpu_devices, host_memory),
            "gpu_container_runtime": summarize_gpu_container_runtime(
                gpu_devices=gpu_devices,
                nvidia_smi_available=nvidia_smi_available,
                docker_info=docker_info,
                nvidia_toolkit=nvidia_toolkit,
            ),
            "runtime_agent_docker_socket": docker_socket,
            "model_storage": summarize_model_storage(model_directories),
            "open_webui": summarize_open_webui_inventory(open_webui_databases),
            "open_webui_data_roots": summarize_open_webui_data_roots(open_webui_data_roots),
            "notes": [
                "Port listeners are review evidence only; do not stop services from the inventory report.",
                "SQLite metadata reads schema and aggregate counts only, not Open WebUI row contents.",
                "Model directory scans are bounded and preserve symlinks/special files for operator review.",
            ],
        },
        "classification": {
            "containers": container_classifications,
            "compose_projects": compose_classifications,
            "old_ai_stack_candidates": old_stack_candidates,
            "systemd_services": systemd_classifications,
            "systemd_old_ai_stack_candidates": old_stack_systemd_candidates,
            "preserve_by_default": explicit_preserve,
            "systemd_preserve_by_default": explicit_systemd_preserve,
            "volumes_with_ai_hints": [row for row in volume_rows if classify_text(json.dumps(row, sort_keys=True))["classification"] == "candidate-old-ai-stack-review-required"],
            "networks_with_ai_hints": [row for row in network_rows if classify_text(json.dumps(row, sort_keys=True))["classification"] == "candidate-old-ai-stack-review-required"],
        },
        "operator_next_steps": [
            "Review candidate-old-ai-stack-review-required containers and compose projects.",
            "Mark unrelated Hermes, Yggdrasil, Discord, DNS, database, and automation services as out of scope.",
            "Back up old Compose files, environment, volumes, Open WebUI data, model directories, and relevant configuration before cutover.",
            "Do not stop or delete anything from this report automatically.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Produce a read-only B1 AI Hub migration inventory.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--b1-root", default=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub"))
    parser.add_argument("--scan-root", action="append", default=None, help="Root to scan for Compose files. May be repeated.")
    args = parser.parse_args()
    output = absolute_path_without_symlink_resolution(args.output)
    scan_roots = [Path(item).resolve() for item in args.scan_root] if args.scan_root else None
    inventory = build_inventory(b1_root=Path(args.b1_root).resolve(), scan_roots=scan_roots)
    write_private_json(output, inventory)
    print(f"wrote inventory: {output}")


if __name__ == "__main__":
    main()
