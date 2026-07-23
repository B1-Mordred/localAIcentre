from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPORT_FORMAT = "b1-ai-hub-acceptance-report/v1"
REPORT_ID_RE = re.compile(r"acceptance-[0-9]{8}t[0-9]{6}z-[a-f0-9]{8}")
SUMMARY_LIMIT = 200


class AcceptanceReportError(ValueError):
    pass


def new_report_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(tz=UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ").lower()
    return f"acceptance-{stamp}-{uuid.uuid4().hex[:8]}"


def acceptance_root(backup_root: Path) -> Path:
    return backup_root / "acceptance"


def validate_report_id(report_id: str) -> str:
    normalized = report_id.strip().lower()
    if not REPORT_ID_RE.fullmatch(normalized):
        raise AcceptanceReportError("invalid acceptance report id")
    return normalized


def report_directory(root: Path, report_id: str) -> Path:
    normalized = validate_report_id(report_id)
    base = root.resolve()
    target = (base / normalized).resolve()
    if target.parent != base:
        raise AcceptanceReportError("acceptance report path escapes the report root")
    return target


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_atomic(path: Path, payload: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _check_by_name(self_test: dict[str, Any], name: str) -> dict[str, Any] | None:
    for check in self_test.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == name:
            return check
    return None


def _acceptance_blockers(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if report.get("status") != "ok":
        blockers.append(f"self-test status is {report.get('status', 'unknown')}")
    if report.get("runtime_deployment_mode") != "production":
        blockers.append("runtime deployment mode is not production")
    production = _check_by_name(report.get("self_test") or {}, "runtimes:production-readiness")
    if production and production.get("status") != "ok":
        blockers.append("required runtimes are not production-ready")
    gpu_check = _check_by_name(report.get("self_test") or {}, "gpu:nvml")
    if not gpu_check:
        blockers.append("GPU/NVML check is absent")
    elif gpu_check.get("status") != "ok":
        blockers.append(f"GPU/NVML check is {gpu_check.get('status', 'unknown')}")
    metrics_gpu = ((report.get("metrics") or {}).get("gpu") or {})
    if metrics_gpu and metrics_gpu.get("available") is not True:
        blockers.append("runtime-agent GPU metrics are unavailable")
    return blockers


def build_report(
    *,
    report_id: str,
    created_by: str,
    label: str,
    notes: str,
    generated_at: datetime,
    runtime_deployment_mode: str,
    resource_policy: dict[str, Any],
    maintenance: dict[str, Any] | None,
    self_test: dict[str, Any],
    metrics: dict[str, Any],
    admission: dict[str, Any],
    scheduler_lease: dict[str, Any] | None,
    runtime_states: list[dict[str, Any]],
    runtime_reservations: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_id = validate_report_id(report_id)
    status = str(self_test.get("status") or "unknown")
    report = {
        "format": REPORT_FORMAT,
        "id": normalized_id,
        "label": label.strip()[:120],
        "notes": notes.strip()[:4000],
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "created_by": created_by,
        "status": status,
        "runtime_deployment_mode": runtime_deployment_mode,
        "resource_policy": resource_policy,
        "maintenance": maintenance or {},
        "self_test": self_test,
        "metrics": metrics,
        "admission": admission,
        "scheduler_lease": scheduler_lease or {},
        "runtime_states": runtime_states,
        "runtime_reservations": runtime_reservations,
    }
    report["acceptance_blockers"] = _acceptance_blockers(report)
    report["operator_handoff_ready"] = status == "ok" and not report["acceptance_blockers"]
    return report


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = rows[0]
    output = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    output.extend("| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |" for row in rows[1:])
    return "\n".join(output)


def _format_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def markdown_report(report: dict[str, Any]) -> str:
    checks = report.get("self_test", {}).get("checks") or []
    check_rows = [["Check", "Status", "Detail"]]
    for check in checks:
        if isinstance(check, dict):
            check_rows.append([
                _format_value(check.get("name")),
                _format_value(check.get("status")),
                _format_value(check.get("detail")),
            ])

    resource_policy = report.get("resource_policy") or {}
    resource_rows = [["Policy", "Value"]]
    for key in sorted(resource_policy):
        resource_rows.append([key, _format_value(resource_policy[key])])

    runtime_rows = [["Runtime", "Status", "Stage", "Model"]]
    for runtime in report.get("runtime_states") or []:
        if isinstance(runtime, dict):
            runtime_rows.append([
                _format_value(runtime.get("runtime")),
                _format_value(runtime.get("status")),
                _format_value(runtime.get("stage")),
                _format_value(runtime.get("resolved_model_version") or runtime.get("active_model")),
            ])

    reservation_rows = [["Reservation", "Runtime", "Model", "Expires"]]
    for reservation in report.get("runtime_reservations") or []:
        if isinstance(reservation, dict):
            reservation_rows.append([
                _format_value(reservation.get("id")),
                _format_value(reservation.get("runtime")),
                _format_value(reservation.get("resolved_model_version") or reservation.get("model_alias")),
                _format_value(reservation.get("expires_at")),
            ])

    metrics_gpu = (report.get("metrics") or {}).get("gpu") or {}
    metrics_jobs = (report.get("metrics") or {}).get("jobs") or {}
    metric_rows = [["Metric", "Value"]]
    for key in (
        "available",
        "device_count",
        "memory_total_mib",
        "memory_used_mib",
        "memory_free_mib",
        "utilization_gpu_percent_max",
        "temperature_c_max",
        "power_watts_total",
    ):
        if key in metrics_gpu:
            metric_rows.append([f"gpu.{key}", _format_value(metrics_gpu[key])])
    for key in ("completed_last_hour", "failed_last_hour", "cancelled_last_hour", "recovery_required_last_hour"):
        if key in metrics_jobs:
            metric_rows.append([f"jobs.{key}", _format_value(metrics_jobs[key])])

    blockers = report.get("acceptance_blockers") or []
    blocker_lines = "\n".join(f"- {item}" for item in blockers) if blockers else "- none"
    notes = report.get("notes") or "none"

    return "\n\n".join(
        [
            f"# B1 AI Hub Acceptance Report {report['id']}",
            "\n".join(
                [
                    f"- Generated: {report.get('generated_at')}",
                    f"- Created by: {report.get('created_by')}",
                    f"- Label: {report.get('label') or 'none'}",
                    f"- Status: {report.get('status')}",
                    f"- Runtime deployment mode: {report.get('runtime_deployment_mode')}",
                    f"- Operator handoff ready: {_format_value(report.get('operator_handoff_ready'))}",
                ]
            ),
            "## Acceptance Blockers\n\n" + blocker_lines,
            "## Operator Notes\n\n" + notes,
            "## Resource Policy\n\n" + _table(resource_rows),
            "## Self-Test Checks\n\n" + _table(check_rows),
            "## Runtime Metrics\n\n" + _table(metric_rows),
            "## Runtime State\n\n" + (_table(runtime_rows) if len(runtime_rows) > 1 else "No runtime state rows recorded."),
            "## Active Runtime Reservations\n\n" + (_table(reservation_rows) if len(reservation_rows) > 1 else "No active runtime reservations recorded."),
            "",
        ]
    )


def write_report(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    report_id = validate_report_id(str(report.get("id") or ""))
    root.mkdir(parents=True, exist_ok=True)
    target = report_directory(root, report_id)
    try:
        target.mkdir(mode=0o750)
    except FileExistsError as exc:
        raise AcceptanceReportError("acceptance report already exists") from exc
    if target.is_symlink():
        raise AcceptanceReportError("acceptance report path is a symlink")

    json_payload = _json_bytes(report)
    markdown_payload = markdown_report(report).encode("utf-8")
    checksums = {
        "report.json": _sha256_bytes(json_payload),
        "report.md": _sha256_bytes(markdown_payload),
    }
    checksum_payload = "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())).encode("utf-8")
    _write_atomic(target / "report.json", json_payload)
    _write_atomic(target / "report.md", markdown_payload)
    _write_atomic(target / "SHA256SUMS", checksum_payload)
    return public_report_summary(report, target)


def load_report(root: Path, report_id: str) -> dict[str, Any]:
    target = report_directory(root, report_id)
    path = target / "report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AcceptanceReportError("acceptance report not found") from exc
    if not isinstance(report, dict) or report.get("format") != REPORT_FORMAT:
        raise AcceptanceReportError("invalid acceptance report format")
    return report


def public_report_summary(report: dict[str, Any], report_dir: Path | None = None) -> dict[str, Any]:
    summary = {
        "id": report.get("id"),
        "format": report.get("format"),
        "label": report.get("label") or "",
        "generated_at": report.get("generated_at"),
        "created_by": report.get("created_by"),
        "status": report.get("status"),
        "runtime_deployment_mode": report.get("runtime_deployment_mode"),
        "operator_handoff_ready": bool(report.get("operator_handoff_ready")),
        "acceptance_blockers": list(report.get("acceptance_blockers") or []),
    }
    if report_dir is not None:
        summary["files"] = {
            "directory": str(report_dir),
            "json": str(report_dir / "report.json"),
            "markdown": str(report_dir / "report.md"),
            "sha256sums": str(report_dir / "SHA256SUMS"),
        }
    return summary


def list_reports(root: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    bounded = max(1, min(int(limit), SUMMARY_LIMIT))
    summaries: list[dict[str, Any]] = []
    for item in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        if len(summaries) >= bounded:
            break
        if not item.is_dir() or item.is_symlink():
            continue
        if not REPORT_ID_RE.fullmatch(item.name):
            continue
        try:
            report = load_report(root, item.name)
        except (AcceptanceReportError, json.JSONDecodeError, OSError):
            continue
        summaries.append(public_report_summary(report, item))
    return summaries
