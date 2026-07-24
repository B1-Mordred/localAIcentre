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
REPORT_FILE_NAMES = frozenset({"report.json", "report.md", "SHA256SUMS"})
SUMMARY_LIMIT = 200
MAX_CUTOVER_PLAN_BYTES = 2 * 1024 * 1024
CUTOVER_PLAN_FORMAT = "b1-ai-hub-cutover-plan/v1"
MAX_LIVE_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_LIVE_EVIDENCE_AGE_SECONDS = 72 * 60 * 60
MAX_LIVE_EVIDENCE_FUTURE_SKEW_SECONDS = 10 * 60
GPU_ACCEPTANCE_EVIDENCE_FORMAT = "b1-ai-hub-cross-runtime-gpu-acceptance/v1"
GPU_ACCEPTANCE_REQUIRED_CHECKS = ("resource_policy_and_runtime_readiness", "localai_comfyui_voicebox_switch")
SMOKE_EVIDENCE_FORMAT = "b1-ai-hub-live-smoke/v1"
SMOKE_REQUIRED_CHECKS = (
    "healthz_ok",
    "models_listed",
    "tts_media_job_completed",
    "job_events_streamed",
    "artifact_downloaded",
)
LOCALAI_EVIDENCE_FORMAT = "b1-ai-hub-localai-runtime-acceptance/v1"
LOCALAI_REQUIRED_CHECKS = ("streaming_chat_completed", "single_backend_enforced", "graceful_unload_verified")
INSTALLED_WORKFLOWS_EVIDENCE_FORMAT = "b1-ai-hub-installed-workflows-acceptance/v1"
INSTALLED_WORKFLOWS_REQUIRED_CHECKS = (
    "chat_completed",
    "tts_completed",
    "stt_completed",
    "cpu_audio_does_not_take_gpu_lease",
    "image_generation_completed",
    "image_edit_completed",
    "short_video_completed",
)
REMOTE_NODES_EVIDENCE_FORMAT = "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1"
REMOTE_NODES_REQUIRED_CHECKS = ("server_side_comfyui_stopped", "non_comfy_tts_completed", "artifact_downloaded")
MODELHUB_EVIDENCE_FORMAT = "b1-ai-hub-modelhub-client-sync/v1"
MODELHUB_REQUIRED_CHECKS = (
    "catalog_visible",
    "download_plan_created",
    "range_resume_downloaded",
    "cache_state_managed",
    "dry_run_prune_safe",
    "inference_only_download_blocked",
)
NATIVE_COMFYUI_EVIDENCE_FORMAT = "b1-ai-hub-native-comfyui-compatibility/v1"
NATIVE_COMFYUI_REQUIRED_CHECKS = (
    "object_info_accessible",
    "system_stats_accessible",
    "models_accessible",
    "queue_accessible",
    "upload_image_accessible",
    "upload_mask_accessible",
    "prompt_submission",
    "websocket_events",
    "history_available",
    "view_artifact_accessible",
)
VOICEBOX_EVIDENCE_FORMAT = "b1-ai-hub-voicebox-remote-compatibility/v1"
VOICEBOX_REQUIRED_CHECKS = (
    "native_http_proxy_accessible",
    "profile_lifecycle_validated",
    "speech_or_limitation_recorded",
    "websocket_or_limitation_recorded",
)
SECURITY_EVIDENCE_FORMAT = "b1-ai-hub-security-acceptance/v1"
SECURITY_REQUIRED_CHECKS = (
    "unauthenticated_requests_rejected",
    "under_scoped_requests_rejected",
    "cors_credentials_not_wildcard",
    "csrf_browser_mutation_rejected",
    "comfyui_management_routes_blocked",
    "import_ssrf_blocked",
    "artifact_traversal_blocked",
    "artifact_authorization_enforced",
    "runtime_agent_mutation_guard",
    "logs_redacted",
)
RESTART_RECONCILIATION_EVIDENCE_FORMAT = "b1-ai-hub-restart-reconciliation-acceptance/v1"
RESTART_RECONCILIATION_REQUIRED_CHECKS = (
    "control_plane_restarted",
    "cpu_runner_reconciled",
    "gpu_runner_reconciled",
    "waiting_jobs_requeued",
    "active_jobs_marked_recovery_required",
)
REQUIRED_OPERATOR_EVIDENCE: tuple[tuple[str, str], ...] = (
    ("live_stack_smoke", "Live stack smoke tests passed through the gateway"),
    ("rtx3060_acceptance", "RTX 3060/32 GB cross-runtime acceptance completed with measured reserves"),
    ("chat_tts_image_video", "Chat, TTS/STT, image/edit, and short video workflows completed with installed models"),
    ("native_comfyui_compatibility", "Native ComfyUI REST and WebSocket compatibility was validated externally"),
    ("remote_nodes_non_comfy", "External ComfyUI remote nodes completed a non-Comfy operation with server ComfyUI stopped"),
    ("modelhub_sync", "External Model Hub client synced, resumed, verified, and enforced download policy"),
    ("voicebox_remote", "Voicebox remote/server integration was validated or a pinned upstream limitation was recorded"),
    ("backup_verified", "B1 and old-stack backups were created and verified"),
    ("restore_rehearsed", "Restore-to-alternate-directory rehearsal completed"),
    ("migration_rehearsed", "Old-stack inventory, Open WebUI migration plan, and cutover plan were reviewed"),
    ("restart_reconciliation", "Control-plane restart reconciliation requeued waiting jobs and marked interrupted active jobs for recovery"),
    ("rollback_rehearsed", "Rollback procedure was tested and old resources remain preserved"),
    ("security_review", "LAN-only, TLS, secrets, logs, CORS/CSRF, and runtime-agent security checks passed"),
)
LIVE_EVIDENCE_LABELS: tuple[tuple[str, str], ...] = (
    ("live_stack_smoke", "live stack smoke"),
    ("gpu_acceptance", "RTX 3060 GPU acceptance"),
    ("localai_runtime", "LocalAI runtime acceptance"),
    ("installed_workflows", "installed workflow acceptance"),
    ("native_comfyui_compatibility", "native ComfyUI compatibility"),
    ("remote_nodes_non_comfy", "remote-node non-Comfy compatibility"),
    ("modelhub_client_sync", "Model Hub client sync"),
    ("voicebox_remote", "Voicebox remote compatibility"),
    ("security_acceptance", "security acceptance"),
    ("restart_reconciliation", "restart reconciliation"),
)


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


def report_file_path(root: Path, report_id: str, filename: str) -> Path:
    name = filename.strip()
    if name not in REPORT_FILE_NAMES:
        raise AcceptanceReportError("invalid acceptance report filename")
    directory = report_directory(root, report_id)
    raw_target = directory / name
    if raw_target.is_symlink():
        raise AcceptanceReportError("acceptance report file path is a symlink")
    target = raw_target.resolve()
    directory_resolved = directory.resolve()
    if target.parent != directory_resolved:
        raise AcceptanceReportError("acceptance report file path escapes the report directory")
    if not target.is_file():
        raise AcceptanceReportError("acceptance report file not found")
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


def _parse_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _live_evidence_freshness_failures(report: dict[str, Any]) -> dict[str, str]:
    report_generated_at = _parse_utc_datetime(report.get("generated_at"))
    if report_generated_at is None:
        return {"report": "acceptance report generated_at is missing or invalid"}
    live_evidence = report.get("live_evidence") if isinstance(report.get("live_evidence"), dict) else {}
    failures: dict[str, str] = {}
    max_age_hours = MAX_LIVE_EVIDENCE_AGE_SECONDS // 3600
    for key, label in LIVE_EVIDENCE_LABELS:
        evidence = live_evidence.get(key) if isinstance(live_evidence.get(key), dict) else {}
        if evidence.get("available") is not True:
            continue
        evidence_generated_at = _parse_utc_datetime(evidence.get("generated_at"))
        if evidence_generated_at is None:
            failures[key] = f"{label} evidence generated_at is missing or invalid"
            continue
        age_seconds = (report_generated_at - evidence_generated_at).total_seconds()
        if age_seconds > MAX_LIVE_EVIDENCE_AGE_SECONDS:
            age_hours = int(age_seconds // 3600)
            failures[key] = f"{label} evidence is stale ({age_hours}h old; rerun within {max_age_hours}h of handoff report)"
        elif age_seconds < -MAX_LIVE_EVIDENCE_FUTURE_SKEW_SECONDS:
            failures[key] = f"{label} evidence generated_at is after the handoff report time"
    return failures


def required_operator_evidence_items() -> list[dict[str, str]]:
    return [{"key": key, "label": label} for key, label in REQUIRED_OPERATOR_EVIDENCE]


def normalize_operator_evidence(evidence: dict[str, Any] | None = None, notes: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    supplied = evidence or {}
    supplied_notes = notes or {}
    rows: list[dict[str, Any]] = []
    for key, label in REQUIRED_OPERATOR_EVIDENCE:
        note = supplied_notes.get(key)
        rows.append(
            {
                "key": key,
                "label": label,
                "passed": bool(supplied.get(key)),
                "note": str(note).strip()[:1000] if note is not None else "",
            }
        )
    return rows


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float)) and str(item)]


def cutover_preservation_snapshot(plan: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if plan.get("format") != CUTOVER_PLAN_FORMAT:
        return {"available": False, "reason": "unsupported cutover plan format"}
    old_scope = plan.get("old_stack_scope") if isinstance(plan.get("old_stack_scope"), dict) else {}
    safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
    inputs = plan.get("inputs") if isinstance(plan.get("inputs"), dict) else {}
    verification = inputs.get("old_stack_backup_verification") if isinstance(inputs.get("old_stack_backup_verification"), dict) else {}
    resources = {
        "containers_to_stop_during_cutover": _as_string_list(old_scope.get("containers_to_stop_during_cutover")),
        "containers_to_restart_for_rollback": _as_string_list(old_scope.get("containers_to_restart_for_rollback")),
        "docker_volumes_preserved": _as_string_list(old_scope.get("docker_volumes_preserved")),
        "host_paths_preserved": _as_string_list(old_scope.get("host_paths_preserved")),
    }
    resource_count = (
        len(resources["containers_to_restart_for_rollback"])
        + len(resources["docker_volumes_preserved"])
        + len(resources["host_paths_preserved"])
    )
    return {
        "available": True,
        "format": plan.get("format"),
        "source_path": str(source_path) if source_path else "",
        "created_at": str(plan.get("created_at") or ""),
        "reviewed_by": str(old_scope.get("reviewed_by") or ""),
        "review_notes": str(old_scope.get("review_notes") or "")[:1000],
        "safety": {
            "read_only_plan": bool(safety.get("read_only_plan")),
            "stops_nothing_automatically": bool(safety.get("stops_nothing_automatically")),
            "deletes_nothing": bool(safety.get("deletes_nothing")),
            "old_stack_deletion_allowed": bool(safety.get("old_stack_deletion_allowed")),
            "unknown_resources_preserved_by_default": bool(safety.get("unknown_resources_preserved_by_default")),
        },
        "old_stack_backup_verification_status": str(verification.get("status") or ""),
        "open_webui_preservation": plan.get("open_webui_preservation") if isinstance(plan.get("open_webui_preservation"), dict) else {},
        "warnings": _as_string_list(plan.get("warnings")),
        "resources": resources,
        "resource_count": resource_count,
    }


def latest_cutover_preservation_snapshot(backup_root: Path) -> dict[str, Any]:
    root = backup_root.resolve()
    if not root.exists():
        return {"available": False, "reason": "backup root does not exist", "root": str(root)}
    if not root.is_dir() or root.is_symlink():
        return {"available": False, "reason": "backup root is not a directory", "root": str(root)}
    candidates: list[tuple[float, str, Path]] = []
    for path in root.glob("cutover-plan*.json"):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.parent != root or path.is_symlink() or not path.is_file():
            continue
        try:
            stat_result = path.stat()
            if stat_result.st_size > MAX_CUTOVER_PLAN_BYTES:
                continue
        except OSError:
            continue
        candidates.append((stat_result.st_mtime, path.name, path))
    if not candidates:
        return {"available": False, "reason": "no cutover-plan*.json found", "root": str(root)}
    candidates.sort(reverse=True)
    for _, _, path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("format") == CUTOVER_PLAN_FORMAT:
            return cutover_preservation_snapshot(payload, path.resolve())
    return {"available": False, "reason": "no supported cutover plan found", "root": str(root)}


def gpu_acceptance_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != GPU_ACCEPTANCE_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported GPU acceptance evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in GPU_ACCEPTANCE_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(GPU_ACCEPTANCE_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def smoke_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != SMOKE_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported live smoke evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in SMOKE_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(SMOKE_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def localai_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != LOCALAI_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported LocalAI runtime acceptance evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in LOCALAI_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(LOCALAI_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def installed_workflows_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != INSTALLED_WORKFLOWS_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported installed workflow acceptance evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in INSTALLED_WORKFLOWS_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(INSTALLED_WORKFLOWS_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def remote_nodes_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != REMOTE_NODES_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported remote-node compatibility evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in REMOTE_NODES_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(REMOTE_NODES_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def modelhub_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != MODELHUB_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported Model Hub client sync evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in MODELHUB_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(MODELHUB_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def native_comfyui_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != NATIVE_COMFYUI_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported native ComfyUI compatibility evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in NATIVE_COMFYUI_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(NATIVE_COMFYUI_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def voicebox_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != VOICEBOX_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported Voicebox remote compatibility evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in VOICEBOX_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(VOICEBOX_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def security_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != SECURITY_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported security acceptance evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in SECURITY_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(SECURITY_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def restart_reconciliation_evidence_snapshot(payload: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    if payload.get("format") != RESTART_RECONCILIATION_EVIDENCE_FORMAT:
        return {"available": False, "reason": "unsupported restart reconciliation acceptance evidence format"}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    missing_checks = [
        name
        for name in RESTART_RECONCILIATION_REQUIRED_CHECKS
        if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok"
    ]
    sample_labels = [
        str(sample.get("label"))
        for sample in samples
        if isinstance(sample, dict) and isinstance(sample.get("label"), str)
    ]
    return {
        "available": True,
        "format": payload.get("format"),
        "source_path": str(source_path) if source_path else "",
        "generated_at": str(payload.get("generated_at") or ""),
        "base_url": str(payload.get("base_url") or ""),
        "status": str(payload.get("status") or "unknown"),
        "required_checks": list(RESTART_RECONCILIATION_REQUIRED_CHECKS),
        "missing_checks": missing_checks,
        "checks": checks,
        "sample_count": len(samples),
        "sample_labels": sample_labels[:100],
    }


def _unavailable_live_evidence(reason: str, root: Path) -> dict[str, Any]:
    return {
        "live_stack_smoke": {"available": False, "reason": reason, "root": str(root)},
        "gpu_acceptance": {"available": False, "reason": reason, "root": str(root)},
        "localai_runtime": {"available": False, "reason": reason, "root": str(root)},
        "installed_workflows": {"available": False, "reason": reason, "root": str(root)},
        "native_comfyui_compatibility": {"available": False, "reason": reason, "root": str(root)},
        "remote_nodes_non_comfy": {"available": False, "reason": reason, "root": str(root)},
        "modelhub_client_sync": {"available": False, "reason": reason, "root": str(root)},
        "voicebox_remote": {"available": False, "reason": reason, "root": str(root)},
        "security_acceptance": {"available": False, "reason": reason, "root": str(root)},
        "restart_reconciliation": {"available": False, "reason": reason, "root": str(root)},
    }


def latest_live_evidence_snapshot(backup_root: Path) -> dict[str, Any]:
    root = acceptance_root(backup_root).resolve()
    if not root.exists():
        return _unavailable_live_evidence("acceptance evidence root does not exist", root)
    if not root.is_dir() or root.is_symlink():
        return _unavailable_live_evidence("acceptance evidence root is not a directory", root)
    snapshots = _unavailable_live_evidence("no supported live acceptance evidence found", root)
    candidates: list[tuple[float, str, Path]] = []
    for path in root.glob("*.json"):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.parent != root or path.is_symlink() or not path.is_file():
            continue
        try:
            stat_result = path.stat()
            if stat_result.st_size > MAX_LIVE_EVIDENCE_BYTES:
                continue
        except OSError:
            continue
        candidates.append((stat_result.st_mtime, path.name, path))
    candidates.sort(reverse=True)
    found: set[str] = set()
    for _, _, path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("format") == SMOKE_EVIDENCE_FORMAT and "live_stack_smoke" not in found:
            snapshots["live_stack_smoke"] = smoke_evidence_snapshot(payload, path.resolve())
            found.add("live_stack_smoke")
        elif payload.get("format") == GPU_ACCEPTANCE_EVIDENCE_FORMAT and "gpu_acceptance" not in found:
            snapshots["gpu_acceptance"] = gpu_acceptance_evidence_snapshot(payload, path.resolve())
            found.add("gpu_acceptance")
        elif payload.get("format") == LOCALAI_EVIDENCE_FORMAT and "localai_runtime" not in found:
            snapshots["localai_runtime"] = localai_evidence_snapshot(payload, path.resolve())
            found.add("localai_runtime")
        elif payload.get("format") == INSTALLED_WORKFLOWS_EVIDENCE_FORMAT and "installed_workflows" not in found:
            snapshots["installed_workflows"] = installed_workflows_evidence_snapshot(payload, path.resolve())
            found.add("installed_workflows")
        elif payload.get("format") == NATIVE_COMFYUI_EVIDENCE_FORMAT and "native_comfyui_compatibility" not in found:
            snapshots["native_comfyui_compatibility"] = native_comfyui_evidence_snapshot(payload, path.resolve())
            found.add("native_comfyui_compatibility")
        elif payload.get("format") == REMOTE_NODES_EVIDENCE_FORMAT and "remote_nodes_non_comfy" not in found:
            snapshots["remote_nodes_non_comfy"] = remote_nodes_evidence_snapshot(payload, path.resolve())
            found.add("remote_nodes_non_comfy")
        elif payload.get("format") == MODELHUB_EVIDENCE_FORMAT and "modelhub_client_sync" not in found:
            snapshots["modelhub_client_sync"] = modelhub_evidence_snapshot(payload, path.resolve())
            found.add("modelhub_client_sync")
        elif payload.get("format") == VOICEBOX_EVIDENCE_FORMAT and "voicebox_remote" not in found:
            snapshots["voicebox_remote"] = voicebox_evidence_snapshot(payload, path.resolve())
            found.add("voicebox_remote")
        elif payload.get("format") == SECURITY_EVIDENCE_FORMAT and "security_acceptance" not in found:
            snapshots["security_acceptance"] = security_evidence_snapshot(payload, path.resolve())
            found.add("security_acceptance")
        elif payload.get("format") == RESTART_RECONCILIATION_EVIDENCE_FORMAT and "restart_reconciliation" not in found:
            snapshots["restart_reconciliation"] = restart_reconciliation_evidence_snapshot(payload, path.resolve())
            found.add("restart_reconciliation")
        if found == {
            "live_stack_smoke",
            "gpu_acceptance",
            "localai_runtime",
            "installed_workflows",
            "native_comfyui_compatibility",
            "remote_nodes_non_comfy",
            "modelhub_client_sync",
            "voicebox_remote",
            "security_acceptance",
            "restart_reconciliation",
        }:
            break
    return snapshots


def _read_git_head(repo_root: Path) -> dict[str, Any]:
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return {"available": False, "reason": "git metadata not present"}
    if git_dir.is_file():
        content = git_dir.read_text(encoding="utf-8", errors="replace").strip()
        if not content.startswith("gitdir:"):
            return {"available": False, "reason": "unsupported git metadata file"}
        git_dir = (repo_root / content.split(":", 1)[1].strip()).resolve()
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return {"available": False, "reason": "git HEAD is unreadable"}
    branch = ""
    commit = head
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        branch = ref.removeprefix("refs/heads/")
        ref_path = git_dir / ref
        try:
            commit = ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            return {"available": False, "reason": "git branch ref is unreadable", "branch": branch}
    if not re.fullmatch(r"[a-fA-F0-9]{40}", commit):
        return {"available": False, "reason": "git commit ref is invalid", "branch": branch}
    return {
        "available": True,
        "commit": commit.lower(),
        "short_commit": commit[:12].lower(),
        "branch": branch,
    }


def source_control_snapshot(repo_root: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else dict(os.environ)
    snapshot: dict[str, Any] = {
        "version": env.get("B1_APP_VERSION") or env.get("B1_VERSION") or "",
        "image_revision": env.get("B1_IMAGE_REVISION") or "",
        "source_commit": env.get("B1_SOURCE_COMMIT") or env.get("GIT_COMMIT") or "",
        "source_ref": env.get("B1_SOURCE_REF") or env.get("GIT_BRANCH") or "",
    }
    if snapshot["source_commit"]:
        snapshot["source_commit"] = snapshot["source_commit"].lower()
        snapshot["short_commit"] = snapshot["source_commit"][:12]
        snapshot["available"] = bool(re.fullmatch(r"[a-f0-9]{40}", snapshot["source_commit"]))
        snapshot["source"] = "environment"
        return snapshot
    git_snapshot = _read_git_head(repo_root or Path.cwd())
    snapshot.update(git_snapshot)
    snapshot["source"] = "git" if git_snapshot.get("available") else "unavailable"
    if git_snapshot.get("commit"):
        snapshot["source_commit"] = git_snapshot["commit"]
        snapshot["source_ref"] = git_snapshot.get("branch", "")
    return snapshot


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
    mutation_guard = _check_by_name(report.get("self_test") or {}, "runtime-agent:mutation-guard")
    if not mutation_guard:
        blockers.append("runtime-agent mutation guard check is absent")
    elif mutation_guard.get("status") != "ok":
        blockers.append(f"runtime-agent mutation guard check is {mutation_guard.get('status', 'unknown')}")
    metrics_gpu = ((report.get("metrics") or {}).get("gpu") or {})
    if metrics_gpu and metrics_gpu.get("available") is not True:
        blockers.append("runtime-agent GPU metrics are unavailable")
    deployment = report.get("deployment") or {}
    if deployment.get("error"):
        blockers.append("runtime-agent service inventory is unavailable")
    services = deployment.get("services") if isinstance(deployment.get("services"), list) else []
    service_containers = [
        container
        for service in services
        if isinstance(service, dict)
        for container in (service.get("containers") or [])
        if isinstance(container, dict)
    ]
    recent_image_refs = [
        item.get("image")
        for update in (report.get("recent_updates") or [])
        if isinstance(update, dict)
        for item in (update.get("image_refs") or [])
        if isinstance(item, dict)
    ]
    if not service_containers and not recent_image_refs:
        blockers.append("deployment image evidence is unavailable")
    elif not any(container.get("image_id") or "@sha256:" in str(container.get("image") or "") for container in service_containers) and not any(
        "@sha256:" in str(image) for image in recent_image_refs
    ):
        blockers.append("deployment image evidence lacks image IDs or pinned digests")
    for item in report.get("operator_evidence") or []:
        if isinstance(item, dict) and not item.get("passed"):
            blockers.append(f"operator evidence missing: {item.get('label') or item.get('key')}")
    blockers.extend(_live_evidence_freshness_failures(report).values())
    live_evidence = report.get("live_evidence") if isinstance(report.get("live_evidence"), dict) else {}
    smoke_evidence = live_evidence.get("live_stack_smoke") if isinstance(live_evidence.get("live_stack_smoke"), dict) else {}
    if smoke_evidence.get("available") is not True:
        blockers.append("live stack smoke evidence is unavailable")
    else:
        if smoke_evidence.get("status") != "ok":
            blockers.append(f"live stack smoke evidence status is {smoke_evidence.get('status', 'unknown')}")
        missing_checks = smoke_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append("live stack smoke evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks))
    gpu_evidence = live_evidence.get("gpu_acceptance") if isinstance(live_evidence.get("gpu_acceptance"), dict) else {}
    if gpu_evidence.get("available") is not True:
        blockers.append("RTX 3060 GPU acceptance evidence is unavailable")
    else:
        if gpu_evidence.get("status") != "ok":
            blockers.append(f"RTX 3060 GPU acceptance evidence status is {gpu_evidence.get('status', 'unknown')}")
        missing_checks = gpu_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append("RTX 3060 GPU acceptance evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks))
    localai_evidence = live_evidence.get("localai_runtime") if isinstance(live_evidence.get("localai_runtime"), dict) else {}
    if localai_evidence.get("available") is not True:
        blockers.append("LocalAI runtime acceptance evidence is unavailable")
    else:
        if localai_evidence.get("status") != "ok":
            blockers.append(f"LocalAI runtime acceptance evidence status is {localai_evidence.get('status', 'unknown')}")
        missing_checks = localai_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append("LocalAI runtime acceptance evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks))
    installed_workflows_evidence = live_evidence.get("installed_workflows") if isinstance(live_evidence.get("installed_workflows"), dict) else {}
    if installed_workflows_evidence.get("available") is not True:
        blockers.append("installed workflow evidence is unavailable")
    else:
        if installed_workflows_evidence.get("status") != "ok":
            blockers.append(f"installed workflow evidence status is {installed_workflows_evidence.get('status', 'unknown')}")
        missing_checks = installed_workflows_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append("installed workflow evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks))
    native_comfyui_evidence = (
        live_evidence.get("native_comfyui_compatibility") if isinstance(live_evidence.get("native_comfyui_compatibility"), dict) else {}
    )
    if native_comfyui_evidence.get("available") is not True:
        blockers.append("native ComfyUI compatibility evidence is unavailable")
    else:
        if native_comfyui_evidence.get("status") != "ok":
            blockers.append(f"native ComfyUI compatibility evidence status is {native_comfyui_evidence.get('status', 'unknown')}")
        missing_checks = native_comfyui_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append("native ComfyUI compatibility evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks))
    remote_nodes_evidence = live_evidence.get("remote_nodes_non_comfy") if isinstance(live_evidence.get("remote_nodes_non_comfy"), dict) else {}
    if remote_nodes_evidence.get("available") is not True:
        blockers.append("remote-node non-Comfy compatibility evidence is unavailable")
    else:
        if remote_nodes_evidence.get("status") != "ok":
            blockers.append(f"remote-node non-Comfy compatibility evidence status is {remote_nodes_evidence.get('status', 'unknown')}")
        missing_checks = remote_nodes_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append(
                "remote-node non-Comfy compatibility evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks)
            )
    modelhub_evidence = live_evidence.get("modelhub_client_sync") if isinstance(live_evidence.get("modelhub_client_sync"), dict) else {}
    if modelhub_evidence.get("available") is not True:
        blockers.append("Model Hub client sync evidence is unavailable")
    else:
        if modelhub_evidence.get("status") != "ok":
            blockers.append(f"Model Hub client sync evidence status is {modelhub_evidence.get('status', 'unknown')}")
        missing_checks = modelhub_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append("Model Hub client sync evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks))
    voicebox_evidence = live_evidence.get("voicebox_remote") if isinstance(live_evidence.get("voicebox_remote"), dict) else {}
    if voicebox_evidence.get("available") is not True:
        blockers.append("Voicebox remote compatibility evidence is unavailable")
    else:
        if voicebox_evidence.get("status") != "ok":
            blockers.append(f"Voicebox remote compatibility evidence status is {voicebox_evidence.get('status', 'unknown')}")
        missing_checks = voicebox_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append("Voicebox remote compatibility evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks))
    security_evidence = live_evidence.get("security_acceptance") if isinstance(live_evidence.get("security_acceptance"), dict) else {}
    if security_evidence.get("available") is not True:
        blockers.append("security acceptance evidence is unavailable")
    else:
        if security_evidence.get("status") != "ok":
            blockers.append(f"security acceptance evidence status is {security_evidence.get('status', 'unknown')}")
        missing_checks = security_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append("security acceptance evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks))
    restart_reconciliation_evidence = (
        live_evidence.get("restart_reconciliation") if isinstance(live_evidence.get("restart_reconciliation"), dict) else {}
    )
    if restart_reconciliation_evidence.get("available") is not True:
        blockers.append("restart reconciliation evidence is unavailable")
    else:
        if restart_reconciliation_evidence.get("status") != "ok":
            blockers.append(
                f"restart reconciliation evidence status is {restart_reconciliation_evidence.get('status', 'unknown')}"
            )
        missing_checks = restart_reconciliation_evidence.get("missing_checks")
        if isinstance(missing_checks, list) and missing_checks:
            blockers.append(
                "restart reconciliation evidence is missing required checks: " + ", ".join(str(item) for item in missing_checks)
            )
    preservation = report.get("cutover_preservation") if isinstance(report.get("cutover_preservation"), dict) else {}
    if preservation.get("available") is not True:
        blockers.append("cutover preservation plan is unavailable")
    else:
        safety = preservation.get("safety") if isinstance(preservation.get("safety"), dict) else {}
        if safety.get("read_only_plan") is not True or safety.get("stops_nothing_automatically") is not True or safety.get("deletes_nothing") is not True:
            blockers.append("cutover preservation plan safety invariants are incomplete")
        if safety.get("old_stack_deletion_allowed") is not False:
            blockers.append("cutover preservation plan permits old-stack deletion")
        if preservation.get("old_stack_backup_verification_status") != "verified":
            blockers.append("old-stack backup verification evidence is missing from cutover plan")
        if int(preservation.get("resource_count") or 0) <= 0:
            blockers.append("cutover preservation plan lists no old rollback resources")
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
    deployment: dict[str, Any] | None = None,
    recent_updates: list[dict[str, Any]] | None = None,
    source_control: dict[str, Any] | None = None,
    operator_evidence: dict[str, Any] | None = None,
    operator_evidence_notes: dict[str, Any] | None = None,
    cutover_preservation: dict[str, Any] | None = None,
    live_evidence: dict[str, Any] | None = None,
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
        "deployment": deployment or {},
        "recent_updates": recent_updates or [],
        "source_control": source_control or {},
        "operator_evidence": normalize_operator_evidence(operator_evidence, operator_evidence_notes),
        "cutover_preservation": cutover_preservation or {"available": False, "reason": "not supplied"},
        "live_evidence": live_evidence
        or {
            "live_stack_smoke": {"available": False, "reason": "not supplied"},
            "gpu_acceptance": {"available": False, "reason": "not supplied"},
            "localai_runtime": {"available": False, "reason": "not supplied"},
            "installed_workflows": {"available": False, "reason": "not supplied"},
            "native_comfyui_compatibility": {"available": False, "reason": "not supplied"},
            "remote_nodes_non_comfy": {"available": False, "reason": "not supplied"},
            "modelhub_client_sync": {"available": False, "reason": "not supplied"},
            "voicebox_remote": {"available": False, "reason": "not supplied"},
            "security_acceptance": {"available": False, "reason": "not supplied"},
            "restart_reconciliation": {"available": False, "reason": "not supplied"},
        },
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


def _live_evidence_markdown(label: str, evidence: dict[str, Any], no_checks_message: str) -> str:
    summary_rows = [["Field", "Value"]]
    for key in (
        "available",
        "source_path",
        "generated_at",
        "base_url",
        "status",
        "sample_count",
    ):
        summary_rows.append([key, _format_value(evidence.get(key))])
    missing_checks = evidence.get("missing_checks")
    if isinstance(missing_checks, list) and missing_checks:
        summary_rows.append(["missing_checks", ", ".join(str(item) for item in missing_checks)])
    check_rows = [["Check", "Status", "Recorded"]]
    checks = evidence.get("checks") if isinstance(evidence.get("checks"), dict) else {}
    for name in sorted(checks):
        check = checks.get(name)
        if isinstance(check, dict):
            check_rows.append([
                _format_value(name),
                _format_value(check.get("status")),
                _format_value(check.get("recorded_at")),
            ])
    return (
        label
        + "\n\n"
        + _table(summary_rows)
        + "\n\n"
        + (_table(check_rows) if len(check_rows) > 1 else _format_value(evidence.get("reason") or no_checks_message))
    )


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

    evidence_rows = [["Evidence", "Passed", "Note"]]
    for item in report.get("operator_evidence") or []:
        if isinstance(item, dict):
            evidence_rows.append([
                _format_value(item.get("label") or item.get("key")),
                _format_value(bool(item.get("passed"))),
                _format_value(item.get("note") or ""),
            ])

    preservation = report.get("cutover_preservation") if isinstance(report.get("cutover_preservation"), dict) else {}
    preserved_rows = [["Class", "Value"]]
    resources = preservation.get("resources") if isinstance(preservation.get("resources"), dict) else {}
    for key, label in (
        ("containers_to_restart_for_rollback", "container"),
        ("docker_volumes_preserved", "docker volume"),
        ("host_paths_preserved", "host path"),
    ):
        for value in resources.get(key) or []:
            preserved_rows.append([label, _format_value(value)])
    preservation_summary_rows = [["Field", "Value"]]
    for key in (
        "available",
        "source_path",
        "created_at",
        "reviewed_by",
        "old_stack_backup_verification_status",
        "resource_count",
    ):
        preservation_summary_rows.append([key, _format_value(preservation.get(key))])

    live_evidence = report.get("live_evidence") if isinstance(report.get("live_evidence"), dict) else {}
    smoke_evidence = live_evidence.get("live_stack_smoke") if isinstance(live_evidence.get("live_stack_smoke"), dict) else {}
    gpu_evidence = live_evidence.get("gpu_acceptance") if isinstance(live_evidence.get("gpu_acceptance"), dict) else {}
    localai_evidence = live_evidence.get("localai_runtime") if isinstance(live_evidence.get("localai_runtime"), dict) else {}
    installed_workflows_evidence = live_evidence.get("installed_workflows") if isinstance(live_evidence.get("installed_workflows"), dict) else {}
    native_comfyui_evidence = (
        live_evidence.get("native_comfyui_compatibility") if isinstance(live_evidence.get("native_comfyui_compatibility"), dict) else {}
    )
    remote_nodes_evidence = live_evidence.get("remote_nodes_non_comfy") if isinstance(live_evidence.get("remote_nodes_non_comfy"), dict) else {}
    modelhub_evidence = live_evidence.get("modelhub_client_sync") if isinstance(live_evidence.get("modelhub_client_sync"), dict) else {}
    voicebox_evidence = live_evidence.get("voicebox_remote") if isinstance(live_evidence.get("voicebox_remote"), dict) else {}
    security_evidence = live_evidence.get("security_acceptance") if isinstance(live_evidence.get("security_acceptance"), dict) else {}
    restart_reconciliation_evidence = (
        live_evidence.get("restart_reconciliation") if isinstance(live_evidence.get("restart_reconciliation"), dict) else {}
    )

    source_control = report.get("source_control") or {}
    source_rows = [["Field", "Value"]]
    for key in ("source", "version", "source_ref", "source_commit", "image_revision"):
        if source_control.get(key):
            source_rows.append([key, _format_value(source_control.get(key))])
    if len(source_rows) == 1:
        source_rows.append(["source", _format_value(source_control.get("reason") or "unavailable")])

    deployment = report.get("deployment") or {}
    service_rows = [["Service", "Container", "State", "Image", "Image ID"]]
    for service in deployment.get("services") or []:
        if not isinstance(service, dict):
            continue
        for container in service.get("containers") or []:
            if isinstance(container, dict):
                service_rows.append([
                    _format_value(service.get("name")),
                    _format_value(container.get("short_id") or container.get("name")),
                    _format_value(container.get("state")),
                    _format_value(container.get("image")),
                    _format_value(container.get("image_id")),
                ])
    update_rows = [["Update", "Status", "Stage", "Images"]]
    for update in report.get("recent_updates") or []:
        if isinstance(update, dict):
            update_rows.append([
                _format_value(update.get("id") or update.get("target_version")),
                _format_value(update.get("status")),
                _format_value(update.get("stage")),
                ", ".join(
                    f"{item.get('service')}={item.get('image')}"
                    for item in update.get("image_refs") or []
                    if isinstance(item, dict)
                ),
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
            "## Source Control\n\n" + _table(source_rows),
            "## Deployment Services\n\n" + (_table(service_rows) if len(service_rows) > 1 else _format_value(deployment.get("error") or "No runtime-agent service inventory recorded.")),
            "## Recent Update Records\n\n" + (_table(update_rows) if len(update_rows) > 1 else "No recent controlled update records captured."),
            "## Self-Test Checks\n\n" + _table(check_rows),
            "## Operator Evidence\n\n" + _table(evidence_rows),
            "## Live Acceptance Evidence\n\n"
            + _live_evidence_markdown("Live stack smoke", smoke_evidence, "No live stack smoke checks recorded.")
            + "\n\n"
            + _live_evidence_markdown("GPU acceptance", gpu_evidence, "No live GPU acceptance checks recorded.")
            + "\n\n"
            + _live_evidence_markdown("LocalAI runtime acceptance", localai_evidence, "No LocalAI runtime acceptance checks recorded.")
            + "\n\n"
            + _live_evidence_markdown(
                "Installed workflow acceptance",
                installed_workflows_evidence,
                "No installed workflow acceptance checks recorded.",
            )
            + "\n\n"
            + _live_evidence_markdown(
                "Native ComfyUI compatibility",
                native_comfyui_evidence,
                "No native ComfyUI compatibility checks recorded.",
            )
            + "\n\n"
            + _live_evidence_markdown(
                "Remote-node non-Comfy compatibility",
                remote_nodes_evidence,
                "No remote-node non-Comfy compatibility checks recorded.",
            )
            + "\n\n"
            + _live_evidence_markdown("Model Hub client sync", modelhub_evidence, "No Model Hub client sync checks recorded.")
            + "\n\n"
            + _live_evidence_markdown("Voicebox remote compatibility", voicebox_evidence, "No Voicebox remote compatibility checks recorded.")
            + "\n\n"
            + _live_evidence_markdown("Security acceptance", security_evidence, "No security acceptance checks recorded.")
            + "\n\n"
            + _live_evidence_markdown(
                "Restart reconciliation",
                restart_reconciliation_evidence,
                "No restart reconciliation checks recorded.",
            ),
            "## Old Resources Preserved For Rollback\n\n" + _table(preservation_summary_rows) + "\n\n" + (_table(preserved_rows) if len(preserved_rows) > 1 else _format_value(preservation.get("reason") or "No preserved old resources recorded.")),
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
    operator_evidence = [item for item in report.get("operator_evidence") or [] if isinstance(item, dict)]
    preservation = report.get("cutover_preservation") if isinstance(report.get("cutover_preservation"), dict) else {}
    live_evidence = report.get("live_evidence") if isinstance(report.get("live_evidence"), dict) else {}
    smoke_evidence = live_evidence.get("live_stack_smoke") if isinstance(live_evidence.get("live_stack_smoke"), dict) else {}
    gpu_evidence = live_evidence.get("gpu_acceptance") if isinstance(live_evidence.get("gpu_acceptance"), dict) else {}
    localai_evidence = live_evidence.get("localai_runtime") if isinstance(live_evidence.get("localai_runtime"), dict) else {}
    installed_workflows_evidence = live_evidence.get("installed_workflows") if isinstance(live_evidence.get("installed_workflows"), dict) else {}
    native_comfyui_evidence = (
        live_evidence.get("native_comfyui_compatibility") if isinstance(live_evidence.get("native_comfyui_compatibility"), dict) else {}
    )
    remote_nodes_evidence = live_evidence.get("remote_nodes_non_comfy") if isinstance(live_evidence.get("remote_nodes_non_comfy"), dict) else {}
    modelhub_evidence = live_evidence.get("modelhub_client_sync") if isinstance(live_evidence.get("modelhub_client_sync"), dict) else {}
    voicebox_evidence = live_evidence.get("voicebox_remote") if isinstance(live_evidence.get("voicebox_remote"), dict) else {}
    security_evidence = live_evidence.get("security_acceptance") if isinstance(live_evidence.get("security_acceptance"), dict) else {}
    restart_reconciliation_evidence = (
        live_evidence.get("restart_reconciliation") if isinstance(live_evidence.get("restart_reconciliation"), dict) else {}
    )
    freshness_failures = _live_evidence_freshness_failures(report)
    smoke_evidence_ready = (
        smoke_evidence.get("available") is True
        and smoke_evidence.get("status") == "ok"
        and not smoke_evidence.get("missing_checks")
        and "live_stack_smoke" not in freshness_failures
    )
    gpu_evidence_ready = (
        gpu_evidence.get("available") is True
        and gpu_evidence.get("status") == "ok"
        and not gpu_evidence.get("missing_checks")
        and "gpu_acceptance" not in freshness_failures
    )
    localai_evidence_ready = (
        localai_evidence.get("available") is True
        and localai_evidence.get("status") == "ok"
        and not localai_evidence.get("missing_checks")
        and "localai_runtime" not in freshness_failures
    )
    installed_workflows_evidence_ready = (
        installed_workflows_evidence.get("available") is True
        and installed_workflows_evidence.get("status") == "ok"
        and not installed_workflows_evidence.get("missing_checks")
        and "installed_workflows" not in freshness_failures
    )
    native_comfyui_evidence_ready = (
        native_comfyui_evidence.get("available") is True
        and native_comfyui_evidence.get("status") == "ok"
        and not native_comfyui_evidence.get("missing_checks")
        and "native_comfyui_compatibility" not in freshness_failures
    )
    remote_nodes_evidence_ready = (
        remote_nodes_evidence.get("available") is True
        and remote_nodes_evidence.get("status") == "ok"
        and not remote_nodes_evidence.get("missing_checks")
        and "remote_nodes_non_comfy" not in freshness_failures
    )
    modelhub_evidence_ready = (
        modelhub_evidence.get("available") is True
        and modelhub_evidence.get("status") == "ok"
        and not modelhub_evidence.get("missing_checks")
        and "modelhub_client_sync" not in freshness_failures
    )
    voicebox_evidence_ready = (
        voicebox_evidence.get("available") is True
        and voicebox_evidence.get("status") == "ok"
        and not voicebox_evidence.get("missing_checks")
        and "voicebox_remote" not in freshness_failures
    )
    security_evidence_ready = (
        security_evidence.get("available") is True
        and security_evidence.get("status") == "ok"
        and not security_evidence.get("missing_checks")
        and "security_acceptance" not in freshness_failures
    )
    restart_reconciliation_evidence_ready = (
        restart_reconciliation_evidence.get("available") is True
        and restart_reconciliation_evidence.get("status") == "ok"
        and not restart_reconciliation_evidence.get("missing_checks")
        and "restart_reconciliation" not in freshness_failures
    )
    summary = {
        "id": report.get("id"),
        "format": report.get("format"),
        "label": report.get("label") or "",
        "generated_at": report.get("generated_at"),
        "created_by": report.get("created_by"),
        "status": report.get("status"),
        "runtime_deployment_mode": report.get("runtime_deployment_mode"),
        "operator_handoff_ready": bool(report.get("operator_handoff_ready")),
        "operator_evidence_ready": bool(operator_evidence) and all(bool(item.get("passed")) for item in operator_evidence),
        "cutover_preservation_ready": preservation.get("available") is True and int(preservation.get("resource_count") or 0) > 0,
        "smoke_evidence_ready": smoke_evidence_ready,
        "gpu_evidence_ready": gpu_evidence_ready,
        "localai_evidence_ready": localai_evidence_ready,
        "installed_workflows_evidence_ready": installed_workflows_evidence_ready,
        "native_comfyui_evidence_ready": native_comfyui_evidence_ready,
        "remote_nodes_evidence_ready": remote_nodes_evidence_ready,
        "modelhub_evidence_ready": modelhub_evidence_ready,
        "voicebox_evidence_ready": voicebox_evidence_ready,
        "security_evidence_ready": security_evidence_ready,
        "restart_reconciliation_evidence_ready": restart_reconciliation_evidence_ready,
        "live_evidence_freshness_ready": not freshness_failures,
        "live_evidence_ready": (
            not freshness_failures
            and smoke_evidence_ready
            and gpu_evidence_ready
            and localai_evidence_ready
            and installed_workflows_evidence_ready
            and native_comfyui_evidence_ready
            and remote_nodes_evidence_ready
            and modelhub_evidence_ready
            and voicebox_evidence_ready
            and security_evidence_ready
            and restart_reconciliation_evidence_ready
        ),
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
