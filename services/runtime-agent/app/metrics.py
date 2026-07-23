from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def parse_metric_paths(value: str) -> list[Path]:
    paths = [Path(item.strip()) for item in value.split(",") if item.strip()]
    return paths or [Path("/srv/b1-ai-hub"), Path("/tmp")]


def parse_meminfo(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            amount = int(parts[0])
        except ValueError:
            continue
        multiplier = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
        values[key] = amount * multiplier
    return values


def memory_snapshot(meminfo_path: Path = Path("/proc/meminfo")) -> dict[str, Any]:
    try:
        meminfo = parse_meminfo(meminfo_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"available": False, "error": exc.__class__.__name__}
    total = meminfo.get("MemTotal", 0)
    available = meminfo.get("MemAvailable", 0)
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    return {
        "available": True,
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": max(0, total - available),
        "swap_total_bytes": swap_total,
        "swap_free_bytes": swap_free,
        "swap_used_bytes": max(0, swap_total - swap_free),
    }


def disk_snapshot(paths: list[Path]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
            snapshots.append(
                {
                    "path": str(path),
                    "available": True,
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                }
            )
        except OSError as exc:
            snapshots.append({"path": str(path), "available": False, "error": exc.__class__.__name__})
    return snapshots


def cpu_snapshot() -> dict[str, Any]:
    try:
        load1, load5, load15 = os.getloadavg()
        load = {"load1": load1, "load5": load5, "load15": load15}
    except OSError:
        load = {}
    return {
        "available": True,
        "cpu_count": os.cpu_count(),
        **load,
    }


def parse_nvidia_smi_csv(text: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    fields = [
        "index",
        "name",
        "uuid",
        "temperature_c",
        "power_watts",
        "utilization_gpu_percent",
        "memory_total_mib",
        "memory_used_mib",
        "memory_free_mib",
    ]
    for line in text.splitlines():
        if not line.strip():
            continue
        raw_values = [item.strip() for item in line.split(",")]
        device: dict[str, Any] = {}
        for key, value in zip(fields, raw_values, strict=False):
            if key in {"index", "temperature_c", "utilization_gpu_percent", "memory_total_mib", "memory_used_mib", "memory_free_mib"}:
                try:
                    device[key] = int(float(value))
                except ValueError:
                    device[key] = None
            elif key == "power_watts":
                try:
                    device[key] = float(value)
                except ValueError:
                    device[key] = None
            else:
                device[key] = value
        devices.append(device)
    return devices


def gpu_snapshot(command: str = "nvidia-smi") -> dict[str, Any]:
    executable = shutil.which(command)
    if executable is None:
        return {"available": False, "error": "nvidia-smi not found", "devices": []}
    query = (
        "--query-gpu=index,name,uuid,temperature.gpu,power.draw,utilization.gpu,"
        "memory.total,memory.used,memory.free"
    )
    try:
        result = subprocess.run(
            [executable, query, "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": exc.__class__.__name__, "devices": []}
    if result.returncode != 0:
        return {"available": False, "error": result.stderr.strip()[:200] or f"exit {result.returncode}", "devices": []}
    return {"available": True, "devices": parse_nvidia_smi_csv(result.stdout)}


def metrics_snapshot(paths: list[Path]) -> dict[str, Any]:
    return {
        "cpu": cpu_snapshot(),
        "memory": memory_snapshot(),
        "disks": disk_snapshot(paths),
        "gpu": gpu_snapshot(),
    }
