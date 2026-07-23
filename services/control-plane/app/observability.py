from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any


PENDING_JOB_STATES = {"created", "validated", "queued", "waiting_for_gpu"}
ACTIVE_JOB_STATES = {
    "unloading",
    "verifying_vram",
    "loading",
    "warming",
    "running",
    "saving",
    "cancelling",
}
TERMINAL_JOB_STATES = {"completed", "cancelled", "failed", "expired"}


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def age_seconds(value: Any, now: datetime) -> float | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def milliseconds_to_seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number / 1000.0


def numeric_values(values: list[Any]) -> list[float]:
    cleaned: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            cleaned.append(number)
    return cleaned


def round_metric(value: float | None) -> int | float | None:
    if value is None:
        return None
    rounded = round(value, 2)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def percentile(sorted_values: list[float], rank: float) -> float | None:
    if not sorted_values:
        return None
    index = max(0, min(len(sorted_values) - 1, ceil(rank * len(sorted_values)) - 1))
    return sorted_values[index]


def summarize_numbers(values: list[Any]) -> dict[str, Any]:
    cleaned = sorted(numeric_values(values))
    if not cleaned:
        return {"count": 0, "min": None, "avg": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(cleaned),
        "min": round_metric(cleaned[0]),
        "avg": round_metric(sum(cleaned) / len(cleaned)),
        "p50": round_metric(percentile(cleaned, 0.50)),
        "p95": round_metric(percentile(cleaned, 0.95)),
        "max": round_metric(cleaned[-1]),
    }


def counts_from_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("state") or "unknown")
        try:
            count = int(row.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        counts[state] = counts.get(state, 0) + max(0, count)
    return counts


def build_queue_metrics(jobs: list[dict[str, Any]], state_counts: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    by_state = counts_from_rows(state_counts)
    sampled_pending = [job for job in jobs if str(job.get("state")) in PENDING_JOB_STATES]
    pending_depth = sum(by_state.get(state, 0) for state in PENDING_JOB_STATES)
    if pending_depth == 0 and sampled_pending:
        pending_depth = len(sampled_pending)

    by_priority: dict[str, int] = {}
    waits_by_priority: dict[str, list[float]] = {}
    waits: list[float] = []
    for job in sampled_pending:
        priority = str(job.get("priority") or "unknown")
        by_priority[priority] = by_priority.get(priority, 0) + 1
        wait = age_seconds(job.get("created_at"), now)
        if wait is None:
            continue
        waits.append(wait)
        waits_by_priority.setdefault(priority, []).append(wait)

    active_depth = sum(by_state.get(state, 0) for state in ACTIVE_JOB_STATES)
    return {
        "depth_total": pending_depth,
        "active_total": active_depth,
        "by_state": by_state,
        "by_priority": by_priority,
        "oldest_wait_seconds": round_metric(max(waits) if waits else None),
        "wait_seconds": summarize_numbers(waits),
        "wait_seconds_by_priority": {priority: summarize_numbers(values) for priority, values in sorted(waits_by_priority.items())},
        "sampled_pending_jobs": len(sampled_pending),
    }


def is_recent(job: dict[str, Any], cutoff: datetime) -> bool:
    for key in ("completed_at", "updated_at", "started_at", "created_at"):
        parsed = parse_datetime(job.get(key))
        if parsed is not None and parsed >= cutoff:
            return True
    return False


def build_job_metrics(jobs: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(hours=1)
    recent = [job for job in jobs if is_recent(job, cutoff)]
    completed = [job for job in recent if job.get("state") == "completed"]
    failed = [job for job in recent if job.get("state") == "failed"]
    cancelled = [job for job in recent if job.get("state") == "cancelled"]
    load_seconds = [milliseconds_to_seconds(job.get("load_time_ms")) for job in recent]
    run_seconds = [milliseconds_to_seconds(job.get("run_time_ms")) for job in recent]
    peak_vram = [job.get("peak_vram_mib") for job in recent]
    peak_ram = [job.get("peak_ram_mib") for job in recent]

    by_runtime: dict[str, dict[str, Any]] = {}
    for job in recent:
        runtime = str(job.get("runtime") or "unassigned")
        bucket = by_runtime.setdefault(
            runtime,
            {
                "total": 0,
                "completed": 0,
                "failed": 0,
                "_load_seconds": [],
                "_run_seconds": [],
                "_peak_vram_mib": [],
                "_peak_ram_mib": [],
            },
        )
        bucket["total"] += 1
        if job.get("state") == "completed":
            bucket["completed"] += 1
        if job.get("state") == "failed":
            bucket["failed"] += 1
        bucket["_load_seconds"].append(milliseconds_to_seconds(job.get("load_time_ms")))
        bucket["_run_seconds"].append(milliseconds_to_seconds(job.get("run_time_ms")))
        bucket["_peak_vram_mib"].append(job.get("peak_vram_mib"))
        bucket["_peak_ram_mib"].append(job.get("peak_ram_mib"))

    runtime_public: dict[str, dict[str, Any]] = {}
    for runtime, bucket in sorted(by_runtime.items()):
        runtime_public[runtime] = {
            "total": bucket["total"],
            "completed": bucket["completed"],
            "failed": bucket["failed"],
            "load_seconds": summarize_numbers(bucket["_load_seconds"]),
            "run_seconds": summarize_numbers(bucket["_run_seconds"]),
            "peak_vram_mib": summarize_numbers(bucket["_peak_vram_mib"]),
            "peak_ram_mib": summarize_numbers(bucket["_peak_ram_mib"]),
        }

    return {
        "sample_size": len(jobs),
        "recent_sample_size": len(recent),
        "window_seconds": 3600,
        "completed_last_hour": len(completed),
        "failed_last_hour": len(failed),
        "cancelled_last_hour": len(cancelled),
        "load_seconds": summarize_numbers(load_seconds),
        "run_seconds": summarize_numbers(run_seconds),
        "peak_vram_mib": summarize_numbers(peak_vram),
        "peak_ram_mib": summarize_numbers(peak_ram),
        "by_runtime": runtime_public,
    }


def build_model_switch_metrics(jobs: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(hours=1)
    started_jobs: list[tuple[datetime, dict[str, Any]]] = []
    for job in jobs:
        started_at = parse_datetime(job.get("started_at"))
        if started_at is not None and started_at >= cutoff:
            started_jobs.append((started_at, job))
    started_jobs.sort(key=lambda item: item[0])

    previous_key: tuple[str, str] | None = None
    switches = 0
    transitions: list[dict[str, Any]] = []
    for started_at, job in started_jobs:
        key = (str(job.get("runtime") or "unassigned"), str(job.get("resolved_model_version") or job.get("model_alias") or "unresolved"))
        if previous_key is not None and key != previous_key:
            switches += 1
            transitions.append(
                {
                    "at": started_at.isoformat(),
                    "from_runtime": previous_key[0],
                    "from_model": previous_key[1],
                    "to_runtime": key[0],
                    "to_model": key[1],
                }
            )
        previous_key = key
    return {
        "last_hour": switches,
        "sampled_started_jobs": len(started_jobs),
        "source": "job_history",
        "recent_transitions": transitions[-10:],
    }


def build_runtime_metrics(runtime_states: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    active: list[dict[str, Any]] = []
    for state in runtime_states:
        status = str(state.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        if state.get("active_model") or state.get("job_id") or status not in {"idle", "unknown", "unavailable"}:
            active.append(
                {
                    "runtime": state.get("runtime"),
                    "status": state.get("status"),
                    "stage": state.get("stage"),
                    "active_model": state.get("active_model"),
                    "model_alias": state.get("model_alias"),
                    "job_id": state.get("job_id"),
                    "updated_at": parse_datetime(state.get("updated_at")).isoformat() if parse_datetime(state.get("updated_at")) else None,
                }
            )
    return {"total": len(runtime_states), "by_status": by_status, "active": active}


def summarize_gpu(gpu: dict[str, Any] | None) -> dict[str, Any]:
    if not gpu:
        return {"available": False, "error": "runtime-agent metrics unavailable", "device_count": 0, "devices": []}
    devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
    memory_total = numeric_values([device.get("memory_total_mib") for device in devices if isinstance(device, dict)])
    memory_used = numeric_values([device.get("memory_used_mib") for device in devices if isinstance(device, dict)])
    memory_free = numeric_values([device.get("memory_free_mib") for device in devices if isinstance(device, dict)])
    utilization = numeric_values([device.get("utilization_gpu_percent") for device in devices if isinstance(device, dict)])
    temperatures = numeric_values([device.get("temperature_c") for device in devices if isinstance(device, dict)])
    power = numeric_values([device.get("power_watts") for device in devices if isinstance(device, dict)])
    return {
        "available": bool(gpu.get("available")),
        "error": gpu.get("error"),
        "device_count": len(devices),
        "memory_total_mib": round_metric(sum(memory_total)) if memory_total else None,
        "memory_used_mib": round_metric(sum(memory_used)) if memory_used else None,
        "memory_free_mib": round_metric(sum(memory_free)) if memory_free else None,
        "utilization_gpu_percent_avg": round_metric(sum(utilization) / len(utilization)) if utilization else None,
        "utilization_gpu_percent_max": round_metric(max(utilization)) if utilization else None,
        "temperature_c_max": round_metric(max(temperatures)) if temperatures else None,
        "power_watts_total": round_metric(sum(power)) if power else None,
        "devices": devices,
    }


def summarize_host(agent_metrics: dict[str, Any] | None) -> dict[str, Any]:
    if not agent_metrics:
        return {"available": False}
    memory = agent_metrics.get("memory") if isinstance(agent_metrics.get("memory"), dict) else {}
    disks = agent_metrics.get("disks") if isinstance(agent_metrics.get("disks"), list) else []
    storage_total = numeric_values([disk.get("total_bytes") for disk in disks if isinstance(disk, dict) and disk.get("available")])
    storage_used = numeric_values([disk.get("used_bytes") for disk in disks if isinstance(disk, dict) and disk.get("available")])
    storage_free = numeric_values([disk.get("free_bytes") for disk in disks if isinstance(disk, dict) and disk.get("available")])
    return {
        "available": bool(agent_metrics),
        "cpu": agent_metrics.get("cpu") if isinstance(agent_metrics.get("cpu"), dict) else {},
        "memory": memory,
        "storage": {
            "paths": disks,
            "total_bytes": int(sum(storage_total)) if storage_total else None,
            "used_bytes": int(sum(storage_used)) if storage_used else None,
            "free_bytes": int(sum(storage_free)) if storage_free else None,
        },
    }


def build_observability_report(
    jobs: list[dict[str, Any]],
    state_counts: list[dict[str, Any]],
    runtime_states: list[dict[str, Any]],
    scheduler_lease: dict[str, Any] | None,
    agent_metrics: dict[str, Any] | None,
    agent_error: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    gpu_metrics = agent_metrics.get("gpu") if isinstance(agent_metrics, dict) and isinstance(agent_metrics.get("gpu"), dict) else None
    return {
        "generated_at": current.isoformat(),
        "queue": build_queue_metrics(jobs, state_counts, current),
        "jobs": build_job_metrics(jobs, current),
        "model_switches_per_hour": build_model_switch_metrics(jobs, current),
        "runtimes": build_runtime_metrics(runtime_states),
        "scheduler_lease": scheduler_lease,
        "runtime_agent": {
            "available": agent_metrics is not None and agent_error is None,
            "error": agent_error,
        },
        "gpu": summarize_gpu(gpu_metrics),
        "host": summarize_host(agent_metrics),
    }
