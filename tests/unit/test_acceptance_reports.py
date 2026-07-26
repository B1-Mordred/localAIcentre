from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import acceptance  # noqa: E402

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def sample_repository_quality_check(check_name: str, command: str) -> dict[str, Any]:
    coverage = list(acceptance.REPOSITORY_QUALITY_REQUIRED_COVERAGE[check_name])
    return {
        "status": "ok",
        "command": command,
        "recorded_at": "2026-07-24T12:10:00+00:00",
        "coverage": coverage,
        "result_summary": {
            "outcome": "passed",
            "exit_code": 0,
            "verification_method": "make prerequisite completed before evidence writer",
            "required_by_target": "repository-quality-evidence",
            "test_result_count": len(coverage),
            "test_results": [
                {
                    "label": label,
                    "kind": "quality",
                    "command": f"{command} [{label}]",
                    "status": "passed",
                }
                for label in coverage
            ],
        },
    }


def complete_operator_evidence() -> dict[str, bool]:
    return {item["key"]: True for item in acceptance.required_operator_evidence_items()}


def sample_model_measurement(alias: str, runtime: str, *, model_id: str | None = None, version: str = "1.0.0") -> dict[str, Any]:
    resolved_model_id = model_id or alias.replace("_", "-")
    resolved = f"{resolved_model_id}@{version}"
    return {
        "alias": alias,
        "status": "installed",
        "modality": "tts" if alias.startswith("tts") else "image" if alias.startswith("image") else "llm",
        "preferred_runtime": runtime,
        "runtime": runtime,
        "runtimes": [runtime],
        "resource_label": "expected",
        "resolved_model_version": resolved,
        "model_id": resolved_model_id,
        "model_version": version,
        "display_name": alias,
        "measurement_available": True,
        "ok_run_count": 1,
        "measurements_updated_at": "2026-07-24T12:00:00+00:00",
        "latest_resource_estimate": {
            "vram_gib": 0 if runtime == "audio-cpu" else 6.5,
            "ram_gib": 1.0 if runtime == "audio-cpu" else 8.0,
            "disk_gib": 4.0,
        },
        "latest_ok_run": {
            "id": f"modelsmoke-{alias}",
            "type": "install-smoke",
            "status": "ok",
            "runtime": runtime,
            "model_alias": alias,
            "resolved_model_version": resolved,
            "started_at": "2026-07-24T12:00:00+00:00",
            "completed_at": "2026-07-24T12:00:05+00:00",
            "duration_ms": 5000,
            "load_time_ms": 1000,
            "run_time_ms": 2000,
            "peak_vram_mib": 0 if runtime == "audio-cpu" else 6144,
            "peak_ram_mib": 768 if runtime == "audio-cpu" else 8192,
        },
    }


def sample_remote_node_surface_check() -> dict[str, Any]:
    required_classes = list(acceptance.REMOTE_NODES_REQUIRED_NODE_CLASSES)
    return {
        "status": "ok",
        "recorded_at": "2026-07-24T12:33:55+00:00",
        "required_node_count": len(required_classes),
        "registered_node_count": len(required_classes),
        "required_node_classes": required_classes,
        "registered_node_classes": required_classes,
        "missing_node_classes": [],
        "missing_display_names": [],
        "invalid_node_classes": [],
        "inspected_workflow_count": 3,
        "inspected_workflows": [
            "all-modalities.reference.workflow.json",
            "text-to-image.async.workflow.json",
            "tts-fast.non-comfy.workflow.json",
        ],
        "example_node_types": required_classes,
        "missing_example_node_types": [],
    }


def sample_model_measurement_coverage(**overrides: Any) -> dict[str, Any]:
    groups = [
        {
            "id": "localai_runtime",
            "label": "LocalAI runtime acceptance",
            "status": "ok",
            "required_aliases": ["chat-default"],
            "missing_aliases": [],
            "measurements": [
                {
                    **sample_model_measurement("chat-default", "localai", model_id="b1-chat-default"),
                    "expected_runtime": "localai",
                    "ready": True,
                    "blockers": [],
                }
            ],
        },
        {
            "id": "gpu_acceptance",
            "label": "RTX 3060 GPU acceptance",
            "status": "ok",
            "required_aliases": ["chat-default", "image-default", "tts-quality"],
            "missing_aliases": [],
            "measurements": [
                {
                    **sample_model_measurement("chat-default", "localai", model_id="b1-chat-default"),
                    "expected_runtime": "localai",
                    "ready": True,
                    "blockers": [],
                },
                {
                    **sample_model_measurement("image-default", "comfyui", model_id="b1-image-default"),
                    "expected_runtime": "comfyui",
                    "ready": True,
                    "blockers": [],
                },
                {
                    **sample_model_measurement("tts-quality", "voicebox", model_id="b1-tts-quality"),
                    "expected_runtime": "voicebox",
                    "ready": True,
                    "blockers": [],
                },
            ],
        },
        {
            "id": "installed_workflows",
            "label": "Installed workflow acceptance",
            "status": "ok",
            "required_aliases": ["chat-default", "tts-fast", "stt-default", "image-default", "image-edit", "video-text"],
            "missing_aliases": [],
            "measurements": [
                {
                    **sample_model_measurement("chat-default", "localai", model_id="b1-chat-default"),
                    "expected_runtime": "localai",
                    "ready": True,
                    "blockers": [],
                },
                {
                    **sample_model_measurement("tts-fast", "audio-cpu", model_id="b1-tts-fast"),
                    "expected_runtime": "audio-cpu",
                    "ready": True,
                    "blockers": [],
                },
                {
                    **sample_model_measurement("stt-default", "audio-cpu", model_id="b1-stt-default"),
                    "expected_runtime": "audio-cpu",
                    "ready": True,
                    "blockers": [],
                },
                {
                    **sample_model_measurement("image-default", "comfyui", model_id="b1-image-default"),
                    "expected_runtime": "comfyui",
                    "ready": True,
                    "blockers": [],
                },
                {
                    **sample_model_measurement("image-edit", "comfyui", model_id="b1-image-edit"),
                    "expected_runtime": "comfyui",
                    "ready": True,
                    "blockers": [],
                },
                {
                    **sample_model_measurement("video-text", "comfyui", model_id="b1-video-text"),
                    "expected_runtime": "comfyui",
                    "ready": True,
                    "blockers": [],
                },
            ],
        },
    ]
    payload = {
        "status": "ok",
        "required_aliases": ["chat-default", "image-default", "tts-quality", "tts-fast", "stt-default", "image-edit", "video-text"],
        "missing_aliases": [],
        "groups": groups,
    }
    payload.update(overrides)
    return payload


def sample_modelhub_cache_file_evidence(blob: str = "a" * 64, size: int = 12) -> dict[str, Any]:
    return {
        "cache_root": "/tmp/b1-modelhub-compat/cache",
        "cached_blob_relative_path": f"blobs/{blob}",
        "path_within_cache_root": True,
        "cached_blob_size": size,
        "cached_blob_file_sha256": blob,
        "cached_blob_is_regular_file": True,
        "cached_blob_is_symlink": False,
        "partial_removed": True,
        "expected_size_matches_file": True,
        "expected_sha256_matches_file": True,
        "posix_mode_checked": True,
        "cache_root_mode": "0o700",
        "blob_dir_mode": "0o700",
        "cached_blob_mode": "0o600",
        "state_file_mode": "0o600",
        "cache_root_private": True,
        "blob_dir_private": True,
        "cached_blob_private": True,
        "state_file_private": True,
    }


def sample_modelhub_checks(blob: str = "a" * 64, size: int = 12) -> dict[str, Any]:
    cache_file_evidence = sample_modelhub_cache_file_evidence(blob, size)
    return {
        "catalog_visible": {"status": "ok", "recorded_at": "2026-07-24T12:36:00+00:00"},
        "download_plan_created": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:37:00+00:00",
            "blob": blob,
            "expected_size": size,
        },
        "head_metadata_validated": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:37:30+00:00",
            "blob": blob,
            "expected_size": size,
            "etag": f'"sha256:{blob}"',
            "checksum": blob,
            "accept_ranges": "bytes",
        },
        "etag_if_none_match_validated": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:37:45+00:00",
            "blob": blob,
            "etag": f'"sha256:{blob}"',
            "checksum": blob,
        },
        "range_resume_downloaded": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:38:00+00:00",
            "blob": blob,
            "expected_size": size,
            "partial_size": 5,
            "final_size": size,
            "final_sha256": blob,
            "partial_removed": True,
            "cached_blob_relative_path": f"blobs/{blob}",
            "path_within_cache_root": True,
            "cached_blob_is_regular_file": True,
            "cached_blob_is_symlink": False,
        },
        "cache_state_managed": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:39:00+00:00",
            "managed_blob_count": 1,
            "managed_blob_sha256": blob,
            "managed_blob_size": size,
            "managed_entry_sha256": blob,
            "managed_entry_size": size,
            **cache_file_evidence,
        },
        "dry_run_prune_safe": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:39:00+00:00",
            "unmanaged_files_ignored": True,
        },
        "inference_only_download_blocked": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:40:00+00:00",
            "action_count": 1,
        },
    }


def sample_modelhub_client_sync_payload(blob: str = "a" * 64, size: int = 12) -> dict[str, Any]:
    return {
        "format": "b1-ai-hub-modelhub-client-sync/v1",
        "generated_at": "2026-07-24T12:40:00+00:00",
        "base_url": "https://models.ai.b1.germering",
        "status": "ok",
        "checks": sample_modelhub_checks(blob, size),
        "samples": [
            {
                "label": "modelhub-client-sync",
                "synced_blob": blob,
                "synced_size": size,
                "cached_blob_file_sha256": blob,
                "cached_blob_size": size,
                "cached_blob_relative_path": f"blobs/{blob}",
                "path_within_cache_root": True,
                "partial_removed": True,
            }
        ],
    }


def sample_artifact_proof(url: str, byte_count: int, sha256: str, mime_type: str, *, index: int = 0, kind: str = "") -> dict[str, Any]:
    artifact_kind = kind or mime_type.split("/", 1)[0]
    return {
        "artifact_index": index,
        "artifact_url": url,
        "artifact_id": f"artifact-{index}",
        "artifact_kind": artifact_kind,
        "artifact_mime_type": mime_type,
        "artifact_bytes": byte_count,
        "artifact_sha256": sha256,
        "download_bytes": byte_count,
        "download_sha256": sha256,
        "download_content_type": mime_type,
        "download_content_length": str(byte_count),
        "download_etag": '"sha256:' + sha256 + '"',
        "download_accept_ranges": "bytes",
    }


def sample_artifact_collection(job_id: str, proofs: list[dict[str, Any]]) -> dict[str, Any]:
    first = proofs[0]
    return {
        "job_id": job_id,
        "artifact_count": len(proofs),
        "verified_artifact_count": len(proofs),
        "total_downloaded_bytes": sum(int(proof["download_bytes"]) for proof in proofs),
        "artifact_proofs": proofs,
        "artifacts": proofs,
        "artifact_url": first["artifact_url"],
        "artifact_id": first["artifact_id"],
        "artifact_kind": first["artifact_kind"],
        "artifact_mime_type": first["artifact_mime_type"],
        "artifact_bytes": first["artifact_bytes"],
        "artifact_sha256": first["artifact_sha256"],
        "download_content_type": first["download_content_type"],
        "download_content_length": first["download_content_length"],
        "download_etag": first["download_etag"],
        "download_accept_ranges": first["download_accept_ranges"],
    }


def sample_view_artifact_proof(
    filename: str,
    byte_count: int,
    sha256: str,
    content_type: str,
    *,
    index: int = 0,
    output_key: str = "images",
) -> dict[str, Any]:
    return {
        "artifact_index": index,
        "node_id": "12",
        "output_key": output_key,
        "filename": filename,
        "subfolder": "",
        "type": "output",
        "byte_count": byte_count,
        "content_type": content_type,
        "download_sha256": sha256,
    }


def ok_checks(names: tuple[str, ...]) -> dict[str, dict[str, str]]:
    return {name: {"status": "ok"} for name in names}


def sample_preflight_checks() -> dict[str, dict[str, Any]]:
    return {
        name: {
            "name": name,
            "status": "ok",
            "detail": f"{name} validated",
        }
        for name in acceptance.PREFLIGHT_REQUIRED_CHECKS
    }


def sample_legacy_comfy_payload() -> dict[str, Any]:
    return {
        "format": "b1-ai-hub-legacy-comfyui-listener/v1",
        "generated_at": "2026-07-24T12:34:00+00:00",
        "base_url": "http://ai.b1.germering:8188",
        "status": "ok",
        "listener_policy": {
            "compose_profile": "legacy-comfy",
            "base_url": "http://ai.b1.germering:8188",
            "scheme": "http",
            "port": 8188,
            "bind_host": "",
            "publish_mapping": "8188:8188",
            "listen_port": "8188",
            "allow_cidrs": ["192.168.2.0/24", "100.64.0.0/10"],
            "compatibility_marker": "comfyui-legacy-8188",
            "gateway_target": "control-plane:8000",
            "direct_comfyui_backend": False,
            "authorization_headers_stripped": True,
            "cookie_headers_stripped": True,
            "csrf_headers_stripped": True,
            "bearer_auth_required": False,
        },
        "checks": {
            "object_info_without_auth": {
                "status": "ok",
                "recorded_at": "2026-07-24T12:34:00+00:00",
                "path": "/object_info",
                "url": "http://ai.b1.germering:8188/object_info",
                "authorization_header_sent": False,
                "cookie_header_sent": False,
                "csrf_header_sent": False,
                "type": "object",
                "keys": ["CheckpointLoaderSimple"],
            },
            "system_stats_without_auth": {
                "status": "ok",
                "recorded_at": "2026-07-24T12:34:00+00:00",
                "path": "/system_stats",
                "url": "http://ai.b1.germering:8188/system_stats",
                "authorization_header_sent": False,
                "cookie_header_sent": False,
                "csrf_header_sent": False,
                "type": "object",
                "keys": ["system", "devices"],
            },
            "websocket_without_auth": {
                "status": "ok",
                "recorded_at": "2026-07-24T12:34:00+00:00",
                "websocket_url": "ws://ai.b1.germering:8188/ws?clientId=b1-legacy-comfyui-test",
                "websocket_path": "/ws",
                "websocket_scheme": "ws",
                "client_id": "b1-legacy-comfyui-test",
                "authorization_header_sent": False,
                "cookie_header_sent": False,
                "csrf_header_sent": False,
                "received_initial_message": True,
                "initial_event_type": "status",
            },
        },
        "samples": [{"label": "object_info"}, {"label": "system_stats"}, {"label": "websocket"}],
    }


def sample_security_checks() -> dict[str, dict[str, Any]]:
    return {
        "unauthenticated_requests_rejected": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:46:00+00:00",
            "path": "/admin/self-test",
            "http_status": 401,
        },
        "under_scoped_requests_rejected": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:47:00+00:00",
            "auth_status": True,
            "rejected_path": "/admin/self-test",
            "http_status": 403,
            "temporary_client": True,
        },
        "cors_credentials_not_wildcard": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:47:00+00:00",
            "blocked_origin": "https://evil.example",
            "http_status": 400,
            "allow_origin": "",
            "allow_credentials": "",
            "wildcard_credentials": False,
        },
        "csrf_browser_mutation_rejected": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:48:00+00:00",
            "path": "/admin/network-policy/validate",
            "http_status": 403,
        },
        "comfyui_management_routes_blocked": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:48:00+00:00",
            "path": "/api/manager/install",
            "http_status": 403,
        },
        "import_ssrf_blocked": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:49:00+00:00",
            "path": "/admin/models/download-plan",
            "http_status": 422,
            "policy_case": "loopback-ssrf",
            "rejected_scheme": "http",
            "rejected_host": "127.0.0.1",
        },
        "import_metadata_ssrf_blocked": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:49:00+00:00",
            "path": "/admin/models/download-plan",
            "http_status": 422,
            "policy_case": "link-local-metadata",
            "rejected_scheme": "https",
            "rejected_host": "169.254.169.254",
        },
        "import_private_network_blocked": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:49:00+00:00",
            "path": "/admin/models/download-plan",
            "http_status": 422,
            "policy_case": "private-network",
            "rejected_scheme": "https",
            "rejected_host": "172.17.0.1",
        },
        "import_plain_http_blocked": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:49:00+00:00",
            "path": "/admin/models/download-plan",
            "http_status": 422,
            "policy_case": "plain-http",
            "rejected_scheme": "http",
            "rejected_host": "example.com",
        },
        "artifact_traversal_blocked": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:49:00+00:00",
            "path": "/artifacts/%2e%2e/secrets/master_encryption_key",
            "http_status": 403,
            "response_bytes": 96,
        },
        "artifact_authorization_enforced": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:49:30+00:00",
            "path": "/artifacts/audio-cpu/job_123/speech.wav",
            "authorized_status": 206,
            "authorized_byte_count": 1,
            "authorized_sha256": "9" * 64,
            "authorized_content_range": "bytes 0-0/4096",
            "authorized_content_length": "1",
            "authorized_content_type": "audio/wav",
            "authorized_etag": '"sha256:' + "2" * 64 + '"',
            "unauthenticated_status": 401,
            "under_scoped_status": 403,
            "other_owner_status": 403,
            "temporary_reader_client": True,
        },
        "runtime_agent_mutation_guard": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:50:00+00:00",
            "path": "/admin/self-test",
            "http_status": 200,
            "auth_configured": True,
            "allow_missing_auth": False,
            "mtls_enabled": True,
            "client_cert_required": True,
            "mutation_rate_limit_per_minute": 12,
            "allowed_service_count": 8,
            "runtime_action_service_count": 4,
        },
        "runtime_agent_arbitrary_runtime_rejected": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:50:00+00:00",
            "runtime": "postgres",
            "http_status": 404,
        },
        "runtime_agent_arbitrary_logs_rejected": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:50:00+00:00",
            "service": "postgres",
            "http_status": 404,
        },
        "logs_redacted": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:50:00+00:00",
            "service": "control-plane",
            "http_status": 200,
            "line_count": 42,
            "secret_values_checked": 2,
            "github_pat_absent": True,
            "bearer_tokens_redacted": True,
        },
    }


def sample_restart_reconciliation_checks() -> dict[str, dict[str, Any]]:
    return {
        "control_plane_restarted": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:51:00+00:00",
            "started_at": "2026-07-24T12:51:10+00:00",
            "expected_after": "2026-07-24T12:50:55+00:00",
            "api_status": "ok",
            "required_runners": ["cpu-job-runner", "gpu-job-runner"],
            "missing_required_runners": [],
            "record_count": 2,
        },
        "cpu_runner_reconciled": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:52:00+00:00",
            "runner": "cpu-job-runner",
            "runner_status": "ok",
            "required_runner_present": True,
            "runtime_names": ["audio-cpu"],
            "started_at": "2026-07-24T12:51:11+00:00",
            "completed_at": "2026-07-24T12:51:12+00:00",
            "marked_recovery_required": 0,
            "requeued": 1,
            "requeued_job_ids": ["job_cpu_waiting_1"],
            "recovery_required_job_ids": [],
        },
        "gpu_runner_reconciled": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:52:00+00:00",
            "runner": "gpu-job-runner",
            "runner_status": "ok",
            "required_runner_present": True,
            "runtime_names": ["localai", "comfyui", "voicebox"],
            "started_at": "2026-07-24T12:51:11+00:00",
            "completed_at": "2026-07-24T12:51:13+00:00",
            "marked_recovery_required": 1,
            "requeued": 1,
            "requeued_job_ids": ["job_gpu_waiting_1"],
            "recovery_required_job_ids": ["job_gpu_active_1"],
        },
        "waiting_jobs_requeued": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:53:00+00:00",
            "observed": 2,
            "minimum": 1,
        },
        "active_jobs_marked_recovery_required": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:54:00+00:00",
            "observed": 1,
            "minimum": 1,
        },
        "interrupted_job_ids_recorded": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:54:10+00:00",
            "requeued_job_ids": ["job_cpu_waiting_1", "job_gpu_waiting_1"],
            "recovery_required_job_ids": ["job_gpu_active_1"],
            "requeued_sample_count": 2,
            "recovery_required_sample_count": 1,
        },
        "resumable_comfyui_native_prompts_reattached": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:54:30+00:00",
            "observed": 1,
            "minimum": 1,
            "checked": 1,
            "skipped": 0,
            "resumed_job_ids": ["job_native_1"],
            "native_prompt_ids": ["prompt_native_1"],
        },
    }


def sample_target_identity_readiness(**overrides: Any) -> dict[str, Any]:
    payload = {
        "available": True,
        "hostname_authority": "system-hostname",
        "expected_target_host": "ai.b1.germering",
        "expected_short_hostname": "ai",
        "observed_hostname": "ai",
        "observed_fqdn": "ai.b1.germering",
        "observed_platform_node": "ai",
        "hostname_matches_expected": True,
        "fqdn_matches_expected": True,
        "platform_node_matches_expected": True,
        "accepted": True,
        "operator_must_review_target_identity": False,
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def sample_backup_migration_rollback_checks() -> dict[str, dict[str, Any]]:
    b1_archive_sha = "e" * 64
    old_archive_sha = "f" * 64
    cutover_plan_sha = "c" * 64
    open_webui_domains = {
        "all_readable": {
            "accounts": {"database_count": 1, "tables": ["user"], "known_row_count": 1},
            "chats": {"database_count": 1, "tables": ["chat"], "known_row_count": 2},
            "settings": {"database_count": 1, "tables": ["config"], "known_row_count": 1},
            "documents_rag": {"database_count": 1, "tables": ["document", "file"], "known_row_count": 2},
        },
        "backed_up_readable": {
            "accounts": {"database_count": 1, "tables": ["user"], "known_row_count": 1},
            "chats": {"database_count": 1, "tables": ["chat"], "known_row_count": 2},
            "settings": {"database_count": 1, "tables": ["config"], "known_row_count": 1},
            "documents_rag": {"database_count": 1, "tables": ["document", "file"], "known_row_count": 2},
        },
        "content_rows_read": False,
    }
    resources = {
        "containers_to_restart_for_rollback": ["old-open-webui"],
        "systemd_services_to_restart_for_rollback": ["ollama.service"],
        "docker_volumes_preserved": ["open-webui-data"],
        "host_paths_preserved": ["/srv/old-ai/docker-compose.yaml"],
    }
    resource_counts_by_type = {
        "containers_to_restart_for_rollback": 1,
        "systemd_services_to_restart_for_rollback": 1,
        "docker_volumes_preserved": 1,
        "host_paths_preserved": 1,
    }
    resources_sha = acceptance._preserved_resources_sha256(resources)
    rollback_actions_sha = "d" * 64
    return {
        "b1_backup_created": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:45:00+00:00",
            "backup": "/srv/b1-ai-hub/backups/b1-20260724-124500",
            "created_at": "2026-07-24T12:44:30+00:00",
            "file_count": 12,
            "postgres_dump_included": True,
        },
        "b1_backup_verified": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:46:00+00:00",
            "backup": "/srv/b1-ai-hub/backups/b1-20260724-124500",
            "files_verified": 12,
            "archive_sha256": b1_archive_sha,
            "postgres_native_dump_verified": True,
        },
        "b1_restore_rehearsed": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:47:00+00:00",
            "restore_report": "/srv/b1-ai-hub/restore-tests/b1-20260724-124500/restore-report.json",
            "target": "/srv/b1-ai-hub/restore-tests/b1-20260724-124500",
            "files_verified": 12,
            "postgres_native_dump_verified": True,
        },
        "old_stack_inventory_reviewed": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:48:00+00:00",
            "path": "/srv/b1-ai-hub/backups/inventory-20260724-120000.json",
            "container_classification_count": 3,
            "target_identity": sample_target_identity_readiness(),
        },
        "old_stack_backup_verified": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:49:00+00:00",
            "backup": "/srv/b1-ai-hub/backups/old-stack-20260724-121500",
            "files_verified": 4,
            "archive_sha256": old_archive_sha,
            "contains_sensitive_data": True,
        },
        "open_webui_migration_plan_reviewed": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:50:00+00:00",
            "path": "/srv/b1-ai-hub/backups/open-webui-migration-plan.json",
            "recommended_strategy": "preserve-backed-up-sqlite-and-test-supported-open-webui-import",
            "readable_database_count": 1,
            "data_domains": open_webui_domains,
        },
        "cutover_plan_reviewed": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:51:00+00:00",
            "path": "/srv/b1-ai-hub/backups/cutover-plan.json",
            "resource_count": 4,
            "resource_counts_by_type": resource_counts_by_type,
            "resources_sha256": resources_sha,
            "resources": resources,
            "dns_readiness": {
                "all_hosts_resolve": True,
                "all_hosts_share_gateway_address": True,
                "operator_must_review_dns": False,
                "reference_host": "ai.b1.germering",
                "common_addresses": ["192.168.2.100"],
                "missing_hosts": [],
                "divergent_hosts": [],
                "optional_missing_hosts": ["monitoring.ai.b1.germering"],
                "optional_divergent_hosts": [],
            },
            "target_identity_readiness": sample_target_identity_readiness(),
            "networking_readiness": {
                "available": True,
                "hostname_authority": "system-hostname",
                "hostname_source": "system-hostname",
                "network_property_source": "host-dhcp-client",
                "b1_manages_host_networking": False,
                "b1_static_ip_configures": False,
                "non_loopback_address_count": 1,
                "default_route_interfaces": ["eno1"],
                "default_route_address_count": 1,
                "default_route_count": 1,
                "default_route_protocols": ["dhcp"],
                "has_dhcp_default_route": True,
                "dns_record_count": 7,
                "operator_must_review_networking": False,
                "warnings": [],
            },
            "hardware_readiness": {
                "available": True,
                "accepted": True,
                "operator_must_review_hardware": False,
                "profile": "rtx3060-32gb-initial",
                "largest_gpu_vram_mib": 12288,
                "minimum_gpu_vram_mib": 12288,
                "host_total_ram_mib": 32168,
                "minimum_host_ram_mib": 32000,
                "warnings": [],
            },
            "gpu_runtime_readiness": {
                "available": True,
                "accepted": True,
                "operator_must_review_gpu_runtime": False,
                "nvidia_smi_available": True,
                "detected_gpu_count": 1,
                "docker_nvidia_runtime_available": True,
                "nvidia_container_toolkit_available": True,
                "nvidia_container_toolkit_returncode": 0,
                "nvidia_container_toolkit_version": "NVIDIA Container Toolkit CLI version 1.17.8",
                "warnings": [],
            },
            "runtime_agent_socket_readiness": {
                "available": True,
                "runtime_agent_group_access_ready": True,
                "operator_must_review_runtime_agent_socket": False,
                "path": "/var/run/docker.sock",
                "gid": 998,
                "configured_gid": "998",
                "configured_gid_matches": True,
                "mode_octal": "0660",
                "warnings": [],
            },
            "open_webui_preservation": {
                "plan_supplied": True,
                "operator_must_review_open_webui": False,
                "recommended_strategy": "preserve-backed-up-sqlite-and-test-supported-open-webui-import",
                "compatibility_status": "source-version-recorded-temporary-validation-required",
                "requires_temporary_instance_validation": True,
                "data_domains": open_webui_domains,
                "plan_warnings": [],
            },
            "reviewed_by": "operator",
        },
        "rollback_rehearsed": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:55:00+00:00",
            "report": "/srv/b1-ai-hub/backups/rollback-rehearsal.json",
            "rehearsed_by": "operator",
            "generated_at": "2026-07-24T12:54:00+00:00",
            "cutover_plan_sha256": cutover_plan_sha,
            "command_count": 1,
            "operator_action_count": 2,
            "rollback_actions_sha256": rollback_actions_sha,
        },
        "old_resources_preserved": {
            "status": "ok",
            "recorded_at": "2026-07-24T12:56:00+00:00",
            "report": "/srv/b1-ai-hub/backups/rollback-rehearsal.json",
            "resource_count": 4,
            "rehearsal_resource_count": 4,
            "resource_counts_by_type": resource_counts_by_type,
            "resources_sha256": resources_sha,
            "resources": resources,
        },
    }


def sample_cutover_preservation(**overrides: Any) -> dict[str, Any]:
    payload = {
        "available": True,
        "format": "b1-ai-hub-cutover-plan/v1",
        "source_path": "/srv/b1-ai-hub/backups/cutover-plan.json",
        "created_at": "2026-07-24T11:30:00+00:00",
        "reviewed_by": "operator",
        "review_notes": "old AI resources only",
        "safety": {
            "read_only_plan": True,
            "stops_nothing_automatically": True,
            "deletes_nothing": True,
            "old_stack_deletion_allowed": False,
            "unknown_resources_preserved_by_default": True,
        },
        "old_stack_backup_verification_status": "verified",
        "dns_readiness": {
            "all_hosts_resolve": True,
            "all_hosts_share_gateway_address": True,
            "operator_must_review_dns": False,
            "reference_host": "ai.b1.germering",
            "common_addresses": ["192.168.2.100"],
            "missing_hosts": [],
            "divergent_hosts": [],
            "optional_missing_hosts": ["monitoring.ai.b1.germering"],
            "optional_divergent_hosts": [],
        },
        "target_identity_readiness": sample_target_identity_readiness(),
        "networking_readiness": {
            "available": True,
            "hostname_authority": "system-hostname",
            "hostname_source": "system-hostname",
            "network_property_source": "host-dhcp-client",
            "b1_manages_host_networking": False,
            "b1_static_ip_configures": False,
            "non_loopback_address_count": 1,
            "default_route_interfaces": ["eno1"],
            "default_route_address_count": 1,
            "default_route_count": 1,
            "default_route_protocols": ["dhcp"],
            "has_dhcp_default_route": True,
            "dns_record_count": 7,
            "operator_must_review_networking": False,
            "warnings": [],
        },
        "hardware_readiness": {
            "available": True,
            "profile": "rtx3060-32gb-initial",
            "accepted": True,
            "largest_gpu_vram_mib": 12288,
            "minimum_gpu_vram_mib": 12288,
            "host_total_ram_mib": 32168,
            "minimum_host_ram_mib": 32000,
            "operator_must_review_hardware": False,
            "warnings": [],
        },
        "gpu_runtime_readiness": {
            "available": True,
            "accepted": True,
            "nvidia_smi_available": True,
            "detected_gpu_count": 1,
            "docker_nvidia_runtime_available": True,
            "docker_runtimes": ["nvidia", "runc"],
            "docker_default_runtime": "runc",
            "nvidia_container_toolkit_available": True,
            "nvidia_container_toolkit_returncode": 0,
            "nvidia_container_toolkit_version": "NVIDIA Container Toolkit CLI version 1.17.8",
            "operator_must_review_gpu_runtime": False,
            "warnings": [],
        },
        "runtime_agent_socket_readiness": {
            "available": True,
            "path": "/var/run/docker.sock",
            "exists": True,
            "is_socket": True,
            "uid": 0,
            "gid": 998,
            "mode_octal": "0660",
            "group_readable": True,
            "group_writable": True,
            "configured_gid": "998",
            "configured_gid_valid": True,
            "configured_gid_matches": True,
            "runtime_agent_group_access_ready": True,
            "operator_must_review_runtime_agent_socket": False,
            "warnings": [],
        },
        "open_webui_preservation": {"plan_supplied": True, "operator_must_review_open_webui": False},
        "warnings": [],
        "resources": {
            "containers_to_stop_during_cutover": ["old-open-webui"],
            "containers_to_restart_for_rollback": ["old-open-webui"],
            "systemd_services_to_restart_for_rollback": ["ollama.service"],
            "docker_volumes_preserved": ["open-webui-data"],
            "host_paths_preserved": ["/srv/old-ai/docker-compose.yaml"],
        },
        "resource_count": 4,
    }
    payload.update(overrides)
    return payload


def sample_live_evidence(**overrides: Any) -> dict[str, Any]:
    native_prompt_id = "prompt_native_1"
    native_job_id = "job_native_1"
    native_artifact_sha = "8" * 64
    native_artifact_proofs = [sample_artifact_proof("/artifacts/native/output.png", 4096, native_artifact_sha, "image/png")]
    native_artifacts = sample_artifact_collection(native_job_id, native_artifact_proofs)
    native_view_artifacts = [sample_view_artifact_proof("native-output.png", 4096, native_artifact_sha, "image/png")]
    smoke_artifact_sha = "1" * 64
    smoke_artifact_proofs = [sample_artifact_proof("/artifacts/smoke/tts.wav", 4096, smoke_artifact_sha, "audio/wav")]
    smoke_artifacts = sample_artifact_collection("job_smoke_tts_1", smoke_artifact_proofs)
    gpu_comfy_prompt_id = "prompt_gpu_comfy_1"
    gpu_comfy_artifact_sha = "6" * 64
    gpu_comfy_artifact_proofs = [sample_artifact_proof("/artifacts/comfyui/prompt_gpu_comfy_1/0.png", 4096, gpu_comfy_artifact_sha, "image/png")]
    gpu_comfy_artifacts = sample_artifact_collection("job_gpu_comfy_1", gpu_comfy_artifact_proofs)
    gpu_comfy_prompt = {
        "source": "env-file",
        "file_path": "/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json",
        "file_name": "text-to-image-api-prompt.json",
        "node_count": 7,
        "class_type_count": 6,
        "class_types": [
            "CheckpointLoaderSimple",
            "CLIPTextEncode",
            "EmptyLatentImage",
            "KSampler",
            "SaveImage",
            "VAEDecode",
        ],
        "route_level_smoke": False,
    }
    workflow_tts_sha = "2" * 64
    workflow_image_sha = "3" * 64
    workflow_edit_sha = "4" * 64
    workflow_video_sha = "5" * 64
    workflow_image_proofs = [sample_artifact_proof("/artifacts/workflows/image.png", 4096, workflow_image_sha, "image/png")]
    workflow_edit_proofs = [sample_artifact_proof("/artifacts/workflows/edit.png", 4096, workflow_edit_sha, "image/png")]
    workflow_video_proofs = [sample_artifact_proof("/artifacts/workflows/video.mp4", 8192, workflow_video_sha, "video/mp4")]
    workflow_image_artifacts = sample_artifact_collection("job_workflow_image_1", workflow_image_proofs)
    workflow_edit_artifacts = sample_artifact_collection("job_workflow_edit_1", workflow_edit_proofs)
    workflow_video_artifacts = sample_artifact_collection("job_workflow_video_1", workflow_video_proofs)
    remote_artifact_sha = "b" * 64
    voicebox_profile_id = "vp_acceptance1"
    voicebox_sample_id = "sample_acceptance1"
    voicebox_sample_url = "/artifacts/voicebox/references/operator/sample_acceptance1/sample.wav"
    voicebox_sample_sha = "c" * 64
    voicebox_speech_sha = "d" * 64
    voicebox_identity = {
        "proxy_version": "b1-voicebox-proxy/v0.5.0-b1",
        "upstream_repository": "jamiepine/voicebox",
        "upstream_version": "v0.5.0",
        "upstream_commit": "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d",
        "source_archive_sha256": "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083",
    }
    payload = {
        "repository_quality": {
            "available": True,
            "format": "b1-ai-hub-repository-quality-evidence/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/repository-quality.json",
            "generated_at": "2026-07-24T12:10:00+00:00",
            "base_url": "",
            "status": "ok",
            "source_commit": "c" * 40,
            "source_branch": "agent/test",
            "source_dirty": False,
            "dirty_path_count": 0,
            "command_count": 2,
            "required_checks": list(acceptance.REPOSITORY_QUALITY_REQUIRED_CHECKS),
            "missing_checks": [],
            "missing_quality_evidence": [],
            "checks": {
                "quality_container": sample_repository_quality_check("quality_container", "make quality-container"),
                "secret_scan": sample_repository_quality_check("secret_scan", "make secret-scan"),
            },
            "sample_count": 1,
            "sample_labels": ["repository-quality"],
        },
        "operator_preflight": {
            "available": True,
            "format": "b1-ai-hub-operator-live-acceptance-preflight/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/operator-preflight.json",
            "generated_at": "2026-07-24T12:15:00+00:00",
            "base_url": "",
            "status": "ok",
            "required_checks": list(acceptance.PREFLIGHT_REQUIRED_CHECKS),
            "missing_checks": [],
            "checks": sample_preflight_checks(),
            "sample_count": 0,
            "sample_labels": [],
            "preflight_ok_count": len(acceptance.PREFLIGHT_REQUIRED_CHECKS),
            "preflight_warning_count": 0,
            "preflight_fail_count": 0,
            "warning_checks": [],
            "failed_checks": [],
            "missing_preflight_evidence": [],
        },
        "live_stack_smoke": {
            "available": True,
            "format": "b1-ai-hub-live-smoke/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/live-smoke.json",
            "generated_at": "2026-07-24T12:25:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "open_webui_base_url": "https://ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "healthz_ok",
                "open_webui_health_ok",
                "models_listed",
                "tts_media_job_completed",
                "tts_media_job_resolved_model_recorded",
                "tts_media_job_not_placeholder",
                "job_events_streamed",
                "job_events_terminal_state_observed",
                "artifact_downloaded",
                "artifact_metadata_verified",
            ],
            "missing_checks": [],
            "checks": {
                "healthz_ok": {"status": "ok", "recorded_at": "2026-07-24T12:20:00+00:00"},
                "open_webui_health_ok": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:20:30+00:00",
                    "base_url": "https://ai.b1.germering",
                    "status_code": 200,
                    "response_status": True,
                    "permissions_policy": "camera=(self), microphone=(self), geolocation=()",
                    "strict_transport_security": "max-age=31536000; includeSubDomains",
                    "x_content_type_options": "nosniff",
                },
                "models_listed": {"status": "ok", "recorded_at": "2026-07-24T12:21:00+00:00", "model_count": 4},
                "tts_media_job_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:22:00+00:00",
                    "job_id": "job_smoke_tts_1",
                    "model": "tts-fast",
                },
                "tts_media_job_resolved_model_recorded": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:22:05+00:00",
                    "job_id": "job_smoke_tts_1",
                    "model": "tts-fast",
                    "runtime": "audio-cpu",
                    "resolved_model_version": "b1-tts-fast@1.0.0",
                },
                "tts_media_job_not_placeholder": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:22:06+00:00",
                    "job_id": "job_smoke_tts_1",
                    "model": "tts-fast",
                    "runtime": "audio-cpu",
                    "resolved_model_version": "b1-tts-fast@1.0.0",
                    "artifact_count": 1,
                    "artifact_placeholders": [
                        {
                            "artifact_index": 0,
                            "placeholder": False,
                            "cpu_audio_engine": "piper",
                            "placeholder_failure": False,
                            "reasons": [],
                        }
                    ],
                    "placeholder_failure_count": 0,
                    "placeholder": False,
                    "cpu_audio_engine": "piper",
                },
                "job_events_streamed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:23:00+00:00",
                    "job_id": "job_smoke_tts_1",
                    "bytes": 768,
                    "link": "/v1/media/jobs/job_smoke_tts_1/events",
                },
                "job_events_terminal_state_observed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:23:05+00:00",
                    "job_id": "job_smoke_tts_1",
                    "state": "completed",
                    "event_count": 4,
                },
                "artifact_downloaded": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:24:00+00:00",
                    **smoke_artifacts,
                },
                "artifact_metadata_verified": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:24:05+00:00",
                    **smoke_artifacts,
                },
            },
            "smoke_tts_job_id": "job_smoke_tts_1",
            "smoke_tts_model": "tts-fast",
            "smoke_tts_runtime": "audio-cpu",
            "smoke_tts_resolved_model_version": "b1-tts-fast@1.0.0",
            "smoke_open_webui_base_url": "https://ai.b1.germering",
            "smoke_open_webui_status_code": 200,
            "smoke_artifact_count": 1,
            "smoke_verified_artifact_count": 1,
            "smoke_total_downloaded_bytes": 4096,
            "smoke_artifact_bytes": 4096,
            "smoke_artifact_sha256": smoke_artifact_sha,
            "missing_smoke_evidence": [],
            "sample_count": 6,
            "sample_labels": ["healthz", "open-webui-health", "models", "tts-job", "job-events", "artifact-download"],
        },
        "gpu_acceptance": {
            "available": True,
            "format": "b1-ai-hub-cross-runtime-gpu-acceptance/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/cross-runtime-gpu.json",
            "generated_at": "2026-07-24T12:30:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "resource_policy_and_runtime_readiness",
                "localai_exclusive_gpu_residency",
                "comfyui_switch_completed",
                "voicebox_switch_completed",
                "localai_comfyui_voicebox_switch",
                "vram_reserve_enforced",
                "bounded_runtime_recovery_action",
            ],
            "missing_checks": [],
            "required_model_aliases": ["chat-default", "image-default", "tts-quality"],
            "model_measurements": {
                "chat-default": sample_model_measurement("chat-default", "localai", model_id="b1-chat-default"),
                "image-default": sample_model_measurement("image-default", "comfyui", model_id="b1-image-default"),
                "tts-quality": sample_model_measurement("tts-quality", "voicebox", model_id="b1-tts-quality"),
            },
            "missing_model_measurements": [],
            "checks": {
                "resource_policy_and_runtime_readiness": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:29:00+00:00",
                    "runtime_deployment_mode": "production",
                    "readiness_status": "ok",
                },
                "localai_exclusive_gpu_residency": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:29:30+00:00",
                    "chat_model": "chat-default",
                    "chat_resolved_model_version": "b1-chat-default@1.0.0",
                },
                "comfyui_switch_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:30:00+00:00",
                    "comfyui_model": "image-default",
                    "comfyui_resolved_model_version": "b1-image-default@1.0.0",
                    "comfyui_job_id": "job_gpu_comfy_1",
                    "comfyui_native_prompt_id": gpu_comfy_prompt_id,
                    "comfyui_prompt": gpu_comfy_prompt,
                    "comfyui_artifacts": gpu_comfy_artifacts,
                    "comfyui_artifact_count": 1,
                    "comfyui_verified_artifact_count": 1,
                    "comfyui_first_artifact_url": "/artifacts/comfyui/prompt_gpu_comfy_1/0.png",
                    "comfyui_first_artifact_sha256": gpu_comfy_artifact_sha,
                },
                "voicebox_switch_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:30:30+00:00",
                    "voicebox_model": "tts-quality",
                    "voicebox_resolved_model_version": "b1-tts-quality@1.0.0",
                    "voicebox_job_id": "job_gpu_voicebox_1",
                },
                "vram_reserve_enforced": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:31:00+00:00",
                    "sample_count": 4,
                    "latest_sample": {
                        "label": "after-voicebox-job",
                        "gpu_memory_used_mib": 2048,
                        "gpu_memory_total_mib": 12288,
                        "reserve_mib": 1536,
                        "usable_mib": 10752,
                        "job_peak_vram_mib": 6144,
                    },
                },
                "bounded_runtime_recovery_action": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:31:30+00:00",
                    "runtime": "localai",
                    "result_status": "ok",
                    "strategy": "restart-service",
                },
                "localai_comfyui_voicebox_switch": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:30:00+00:00",
                    "runtime_order": ["localai", "comfyui", "voicebox"],
                    "chat_model": "chat-default",
                    "chat_resolved_model_version": "b1-chat-default@1.0.0",
                    "comfyui_model": "image-default",
                    "comfyui_resolved_model_version": "b1-image-default@1.0.0",
                    "voicebox_model": "tts-quality",
                    "voicebox_resolved_model_version": "b1-tts-quality@1.0.0",
                    "comfyui_job_id": "job_gpu_comfy_1",
                    "comfyui_native_prompt_id": gpu_comfy_prompt_id,
                    "comfyui_prompt": gpu_comfy_prompt,
                    "comfyui_artifact_count": 1,
                    "comfyui_verified_artifact_count": 1,
                    "voicebox_job_id": "job_gpu_voicebox_1",
                },
            },
            "gpu_runtime_order": ["localai", "comfyui", "voicebox"],
            "gpu_switch_resolved_models": {
                "localai": "b1-chat-default@1.0.0",
                "comfyui": "b1-image-default@1.0.0",
                "voicebox": "b1-tts-quality@1.0.0",
            },
            "gpu_comfyui_prompt": {
                "source": "env-file",
                "file_name": "text-to-image-api-prompt.json",
                "node_count": 7,
                "class_type_count": 6,
                "route_level_smoke": False,
            },
            "gpu_comfyui_native_prompt_id": gpu_comfy_prompt_id,
            "gpu_comfyui_artifact_count": 1,
            "gpu_comfyui_verified_artifact_count": 1,
            "gpu_vram_sample_count": 4,
            "gpu_recovery_runtime": "localai",
            "missing_gpu_evidence": [],
            "sample_count": 6,
            "sample_labels": ["initial-readiness", "after-localai-chat", "after-comfyui-job", "after-voicebox-job"],
        },
        "localai_runtime": {
            "available": True,
            "format": "b1-ai-hub-localai-runtime-acceptance/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/localai-runtime.json",
            "generated_at": "2026-07-24T12:31:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": ["streaming_chat_completed", "single_backend_enforced", "graceful_unload_verified"],
            "missing_checks": [],
            "required_model_aliases": ["chat-default"],
            "model_measurements": {
                "chat-default": sample_model_measurement("chat-default", "localai", model_id="b1-chat-default"),
            },
            "missing_model_measurements": [],
            "checks": {
                "streaming_chat_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:30:10+00:00",
                    "model": "chat-default",
                    "resolved_model_version": "b1-chat-default@1.0.0",
                    "event_count": 5,
                    "bytes": 1536,
                },
                "single_backend_enforced": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:30:20+00:00",
                    "active_gpu_runtimes": ["localai"],
                    "stage": "running",
                    "state_status": "ok",
                    "model_alias": "chat-default",
                    "resolved_model_version": "b1-chat-default@1.0.0",
                },
                "graceful_unload_verified": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:30:30+00:00",
                    "runtime_agent_status": "ok",
                    "strategy": "graceful-unload",
                    "state_status": "ok",
                    "state_stage": "idle_unloaded",
                },
            },
            "localai_chat_model": "chat-default",
            "localai_resolved_model_version": "b1-chat-default@1.0.0",
            "localai_stream_event_count": 5,
            "localai_stream_bytes": 1536,
            "localai_unload_stage": "idle_unloaded",
            "missing_localai_evidence": [],
            "sample_count": 3,
            "sample_labels": ["localai-stream-chat", "active-gpu-runtime-states", "localai-unload"],
        },
        "installed_workflows": {
            "available": True,
            "format": "b1-ai-hub-installed-workflows-acceptance/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/installed-workflows.json",
            "generated_at": "2026-07-24T12:32:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "chat_completed",
                "tts_completed",
                "stt_completed",
                "cpu_audio_does_not_take_gpu_lease",
                "image_generation_completed",
                "image_edit_completed",
                "short_video_completed",
                "media_artifacts_verified",
            ],
            "missing_checks": [],
            "required_model_aliases": ["chat-default", "tts-fast", "stt-default", "image-default", "image-edit", "video-text"],
            "model_measurements": {
                "chat-default": sample_model_measurement("chat-default", "localai", model_id="b1-chat-default"),
                "tts-fast": sample_model_measurement("tts-fast", "audio-cpu", model_id="b1-tts-fast"),
                "stt-default": sample_model_measurement("stt-default", "audio-cpu", model_id="b1-stt-default"),
                "image-default": sample_model_measurement("image-default", "comfyui", model_id="b1-image-default"),
                "image-edit": sample_model_measurement("image-edit", "comfyui", model_id="b1-image-edit"),
                "video-text": sample_model_measurement("video-text", "comfyui", model_id="b1-video-text"),
            },
            "missing_model_measurements": [],
            "checks": {
                "chat_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:26:00+00:00",
                    "model": "chat-default",
                    "resolved_model_version": "b1-chat-default@1.0.0",
                    "runtime": "localai",
                    "choice_count": 1,
                    "placeholder_proof": {"placeholder": None, "runtime": "localai", "placeholder_failure": False, "reasons": []},
                },
                "tts_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:27:00+00:00",
                    "model": "tts-fast",
                    "resolved_model_version": "b1-tts-fast@1.0.0",
                    "runtime": "audio-cpu",
                    "byte_count": 2048,
                    "sha256": workflow_tts_sha,
                    "placeholder_proof": {
                        "placeholder": False,
                        "runtime": "audio-cpu",
                        "cpu_audio_engine": "piper",
                        "placeholder_failure": False,
                        "reasons": [],
                    },
                },
                "stt_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:28:00+00:00",
                    "model": "stt-default",
                    "resolved_model_version": "b1-stt-default@1.0.0",
                    "runtime": "audio-cpu",
                    "text_length": 0,
                    "placeholder_proof": {
                        "placeholder": False,
                        "runtime": "audio-cpu",
                        "cpu_audio_engine": "piper",
                        "placeholder_failure": False,
                        "reasons": [],
                    },
                },
                "cpu_audio_does_not_take_gpu_lease": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:28:30+00:00",
                    "tts_model": "tts-fast",
                    "stt_model": "stt-default",
                    "tts_resolved_model_version": "b1-tts-fast@1.0.0",
                    "stt_resolved_model_version": "b1-stt-default@1.0.0",
                    "runtime_policy": "non_comfy_only",
                    "scheduler_owner_before": "none",
                    "scheduler_owner_after": "none",
                    "tts_gpu_lease_required": "false",
                    "stt_gpu_lease_required": False,
                    "tts_byte_count": 2048,
                    "stt_text_length": 0,
                    "tts_placeholder_proof": {
                        "placeholder": False,
                        "runtime": "audio-cpu",
                        "cpu_audio_engine": "piper",
                        "placeholder_failure": False,
                        "reasons": [],
                    },
                    "stt_placeholder_proof": {
                        "placeholder": False,
                        "runtime": "audio-cpu",
                        "cpu_audio_engine": "piper",
                        "placeholder_failure": False,
                        "reasons": [],
                    },
                },
                "image_generation_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:29:00+00:00",
                    "job_id": "job_workflow_image_1",
                    "model": "image-default",
                    "resolved_model_version": "b1-image-default@1.0.0",
                    "runtime": "comfyui",
                    "modality": "image",
                    "operation": "generation",
                    "artifact_count": 1,
                    "verified_artifact_count": 1,
                    "total_downloaded_bytes": 4096,
                    "artifact_proofs": workflow_image_proofs,
                    "first_artifact_bytes": 4096,
                    "first_artifact_sha256": workflow_image_sha,
                    "first_artifact_mime_type": "image/png",
                    "first_artifact_url": "/artifacts/workflows/image.png",
                    "content_type_header": "image/png",
                    "content_length_header": "4096",
                    "etag_header": '"sha256:' + workflow_image_sha + '"',
                    "accept_ranges_header": "bytes",
                },
                "image_edit_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:30:00+00:00",
                    "job_id": "job_workflow_edit_1",
                    "model": "image-edit",
                    "resolved_model_version": "b1-image-edit@1.0.0",
                    "runtime": "comfyui",
                    "modality": "image",
                    "operation": "edit",
                    "artifact_count": 1,
                    "verified_artifact_count": 1,
                    "total_downloaded_bytes": 4096,
                    "artifact_proofs": workflow_edit_proofs,
                    "first_artifact_bytes": 4096,
                    "first_artifact_sha256": workflow_edit_sha,
                    "first_artifact_mime_type": "image/png",
                    "first_artifact_url": "/artifacts/workflows/edit.png",
                    "content_type_header": "image/png",
                    "content_length_header": "4096",
                    "etag_header": '"sha256:' + workflow_edit_sha + '"',
                    "accept_ranges_header": "bytes",
                },
                "short_video_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:31:00+00:00",
                    "job_id": "job_workflow_video_1",
                    "model": "video-text",
                    "resolved_model_version": "b1-video-text@1.0.0",
                    "runtime": "comfyui",
                    "modality": "video",
                    "operation": "generation",
                    "artifact_count": 1,
                    "verified_artifact_count": 1,
                    "total_downloaded_bytes": 8192,
                    "artifact_proofs": workflow_video_proofs,
                    "first_artifact_bytes": 8192,
                    "first_artifact_sha256": workflow_video_sha,
                    "first_artifact_mime_type": "video/mp4",
                    "first_artifact_url": "/artifacts/workflows/video.mp4",
                    "content_type_header": "video/mp4",
                    "content_length_header": "8192",
                    "etag_header": '"sha256:' + workflow_video_sha + '"',
                    "accept_ranges_header": "bytes",
                },
                "media_artifacts_verified": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:31:30+00:00",
                    "workflow_labels": ["image-generation", "image-edit", "short-video"],
                    "artifact_count": 3,
                    "verified_artifact_count": 3,
                    "artifacts": {
                        "image-generation": workflow_image_artifacts,
                        "image-edit": workflow_edit_artifacts,
                        "short-video": workflow_video_artifacts,
                    },
                },
            },
            "installed_workflow_artifact_count": 3,
            "installed_workflow_labels": ["image-generation", "image-edit", "short-video"],
            "missing_installed_workflow_evidence": [],
            "sample_count": 7,
            "sample_labels": ["chat", "tts", "stt", "cpu-audio-no-gpu-lease", "image-generation", "image-edit", "short-video"],
        },
        "native_comfyui_compatibility": {
            "available": True,
            "format": "b1-ai-hub-native-comfyui-compatibility/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/native-comfyui.json",
            "generated_at": "2026-07-24T12:33:00+00:00",
            "base_url": "https://comfy.ai.b1.germering",
            "status": "ok",
            "prompt": {
                "source": "env-file",
                "file_path": "/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json",
                "file_name": "text-to-image-api-prompt.json",
                "node_count": 2,
                "class_type_count": 2,
                "class_types": ["CheckpointLoaderSimple", "SaveImage"],
                "route_level_smoke": False,
                "default_prompt_file": False,
            },
            "required_checks": [
                "object_info_accessible",
                "object_info_node_accessible",
                "system_stats_accessible",
                "models_accessible",
                "queue_accessible",
                "upload_image_accessible",
                "upload_mask_accessible",
                "prompt_submission",
                "prompt_idempotency_replay",
                "websocket_events",
                "history_listing_accessible",
                "history_available",
                "durable_job_observable",
                "native_summary_observable",
                "durable_artifacts_observable",
                "queue_delete_accessible",
                "interrupt_accessible",
                "view_artifact_accessible",
            ],
            "missing_checks": [],
            "native_prompt_id": native_prompt_id,
            "durable_job_id": native_job_id,
            "durable_artifact_count": 1,
            "durable_verified_artifact_count": 1,
            "durable_artifact_first_bytes": 4096,
            "view_artifact_count": 1,
            "view_verified_artifact_count": 1,
            "native_summary_node_count": 2,
            "native_summary_class_type_count": 2,
            "native_summary_stored_artifact_count": 1,
            "native_summary_failed_ingest_count": 0,
            "missing_compatibility_evidence": [],
            "checks": {
                "object_info_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "object_info_node_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "system_stats_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "models_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "queue_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "upload_image_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "upload_mask_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "prompt_submission": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:32:00+00:00",
                    "prompt_id": native_prompt_id,
                    "queue_number": 1,
                },
                "prompt_idempotency_replay": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:32:00+00:00",
                    "prompt_id": native_prompt_id,
                    "replay_header": "true",
                    "idempotency_key_length": 48,
                },
                "websocket_events": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:32:00+00:00",
                    "prompt_id": native_prompt_id,
                    "event_types": ["execution_start", "executing"],
                    "binary_messages": 0,
                    "completed": True,
                },
                "history_listing_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:33:00+00:00"},
                "history_available": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:33:00+00:00",
                    "prompt_id": native_prompt_id,
                    "history_keys": [native_prompt_id],
                },
                "durable_job_observable": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:33:00+00:00",
                    "prompt_id": native_prompt_id,
                    "job_id": native_job_id,
                    "state": "completed",
                    "artifact_count": 1,
                },
                "native_summary_observable": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:33:00+00:00",
                    "prompt_id": native_prompt_id,
                    "job_id": native_job_id,
                    "node_count": 2,
                    "class_type_count": 2,
                    "stored_artifact_count": 1,
                    "failed_ingest_count": 0,
                    "body_hash_present": True,
                    "client_id_present": True,
                },
                "durable_artifacts_observable": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:33:00+00:00",
                    "prompt_id": native_prompt_id,
                    **native_artifacts,
                    "byte_count": 4096,
                    "content_type": "image/png",
                },
                "queue_delete_accessible": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:33:00+00:00",
                    "prompt_id": native_prompt_id,
                    "http_status": 200,
                    "byte_count": 2,
                },
                "interrupt_accessible": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:33:00+00:00",
                    "prompt_id": native_prompt_id,
                    "http_status": 200,
                    "byte_count": 2,
                },
                "view_artifact_accessible": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:33:00+00:00",
                    "prompt_id": native_prompt_id,
                    "view_count": 1,
                    "verified_view_count": 1,
                    "total_byte_count": 4096,
                    "artifacts": native_view_artifacts,
                    "output_key": "images",
                    "filename": "native-output.png",
                    "byte_count": 4096,
                    "content_type": "image/png",
                    "download_sha256": native_artifact_sha,
                },
            },
            "sample_count": 13,
            "sample_labels": [
                "object-info",
                "object-info-node",
                "upload-image",
                "upload-mask",
                "prompt-submission",
                "prompt-idempotency-replay",
                "websocket-completed",
                "history-listing",
                "durable-job-artifact",
                "native-summary",
                "queue-delete",
                "targeted-interrupt",
                "view-artifact",
            ],
        },
        "legacy_comfyui_listener": {
            "available": False,
            "reason": "optional legacy listener not enabled",
            "root": "/srv/b1-ai-hub/backups/acceptance",
        },
        "remote_nodes_non_comfy": {
            "available": True,
            "format": "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/remote-nodes-non-comfy.json",
            "generated_at": "2026-07-24T12:35:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "server_side_comfyui_stopped",
                "server_side_comfyui_stop_verified",
                "node_surface_registered",
                "remote_models_listed",
                "model_alias_selected",
                "credentials_externalized",
                "non_comfy_tts_completed",
                "artifact_downloaded",
                "server_side_comfyui_still_stopped_after_operation",
            ],
            "missing_checks": [],
            "remote_selected_model": "tts-fast",
            "remote_tts_bytes": 2048,
            "remote_artifact_sha256": remote_artifact_sha,
            "remote_artifact_relative_path": "b1-remote-node-non-comfy.wav",
            "remote_comfyui_running_container_count_initial": 0,
            "remote_comfyui_running_container_count_after_operation": 0,
            "missing_compatibility_evidence": [],
            "checks": {
                "server_side_comfyui_stopped": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:34:00+00:00",
                    "stop_mode": "manual",
                },
                "server_side_comfyui_stop_verified": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:34:05+00:00",
                    "verified_by": "admin_runtimes_runtime_agent_services",
                    "running_container_count": 0,
                },
                "node_surface_registered": sample_remote_node_surface_check(),
                "remote_models_listed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:34:15+00:00",
                    "alias_count": 12,
                    "selected_model_visible": True,
                    "model": "tts-fast",
                },
                "model_alias_selected": {"status": "ok", "recorded_at": "2026-07-24T12:34:20+00:00", "model": "tts-fast"},
                "credentials_externalized": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:34:30+00:00",
                    "credential_source": "environment_file",
                    "inspected_workflow_count": 3,
                    "workflow_secret_findings": [],
                },
                "non_comfy_tts_completed": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:35:00+00:00",
                    "model": "tts-fast",
                    "runtime_policy": "non_comfy_only",
                    "byte_count": 2048,
                    "sha256": remote_artifact_sha,
                    "placeholder_proof": {
                        "placeholder": False,
                        "cpu_audio_engine": "piper",
                        "placeholder_failure": False,
                        "reasons": [],
                    },
                },
                "artifact_downloaded": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:35:00+00:00",
                    "filename": "b1-remote-node-non-comfy.wav",
                    "relative_path": "b1-remote-node-non-comfy.wav",
                    "byte_count": 2048,
                    "stat_size": 2048,
                    "sha256": remote_artifact_sha,
                    "file_sha256": remote_artifact_sha,
                    "path_within_download_dir": True,
                    "symlink": False,
                    "private_file_mode": True,
                    "file_mode": "0o600",
                },
                "server_side_comfyui_still_stopped_after_operation": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:35:05+00:00",
                    "verified_by": "admin_runtimes_runtime_agent_services",
                    "running_container_count": 0,
                },
            },
            "sample_count": 2,
            "sample_labels": ["remote-node-model-list", "tts-fast-non-comfy"],
        },
        "modelhub_client_sync": {
            "available": True,
            "format": "b1-ai-hub-modelhub-client-sync/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/modelhub-client-sync.json",
            "generated_at": "2026-07-24T12:40:00+00:00",
            "base_url": "https://models.ai.b1.germering",
            "status": "ok",
            "verified_blob": "a" * 64,
            "verified_size_bytes": 12,
            "required_checks": [
                "catalog_visible",
                "download_plan_created",
                "head_metadata_validated",
                "etag_if_none_match_validated",
                "range_resume_downloaded",
                "cache_state_managed",
                "dry_run_prune_safe",
                "inference_only_download_blocked",
            ],
            "missing_checks": [],
            "missing_integrity_evidence": [],
            "checks": sample_modelhub_checks(),
            "sample_count": 1,
            "sample_labels": ["modelhub-client-sync"],
        },
        "voicebox_remote": {
            "available": True,
            "format": "b1-ai-hub-voicebox-remote-compatibility/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/voicebox-remote.json",
            "generated_at": "2026-07-24T12:45:00+00:00",
            "base_url": "https://voice.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "proxy_build_info_validated",
                "native_http_proxy_accessible",
                "profile_lifecycle_validated",
                "sample_artifact_protected",
                "profile_export_validated",
                "profile_delete_audited",
                "speech_or_limitation_recorded",
                "websocket_or_limitation_recorded",
            ],
            "missing_checks": [],
            "voicebox_profile_id": voicebox_profile_id,
            "voicebox_proxy_version": voicebox_identity["proxy_version"],
            "voicebox_upstream_repository": voicebox_identity["upstream_repository"],
            "voicebox_upstream_version": voicebox_identity["upstream_version"],
            "voicebox_upstream_commit": voicebox_identity["upstream_commit"],
            "voicebox_sample_artifact_url": voicebox_sample_url,
            "voicebox_sample_artifact_sha256": voicebox_sample_sha,
            "voicebox_speech_mode": "speech_validated",
            "voicebox_websocket_mode": "websocket_validated",
            "missing_compatibility_evidence": [],
            "checks": {
                "proxy_build_info_validated": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:40:30+00:00",
                    "runtime": "voicebox",
                    "action": "build-info",
                    "proxy": "b1-voicebox-proxy",
                    "pinned": True,
                    **voicebox_identity,
                },
                "native_http_proxy_accessible": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:41:00+00:00",
                    "http_status": 200,
                    **voicebox_identity,
                },
                "profile_lifecycle_validated": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:42:00+00:00",
                    "profile_id": voicebox_profile_id,
                    "model_alias": "tts-quality",
                    "sample_artifact_count": 1,
                    "sample_artifact_url": voicebox_sample_url,
                    "sample_artifact_sha256": voicebox_sample_sha,
                    "fetched_sample_artifact_count": 1,
                    "fetched_sample_artifact_url": voicebox_sample_url,
                    "fetched_sample_artifact_sha256": voicebox_sample_sha,
                },
                "sample_artifact_protected": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:42:15+00:00",
                    "sample_id": voicebox_sample_id,
                    "sample_url_prefix": "/artifacts/voicebox/references/",
                    "sample_artifact_url": voicebox_sample_url,
                    "sample_artifact_bytes": 3244,
                    "sample_artifact_sha256": voicebox_sample_sha,
                    "sample_artifact_mime_type": "audio/wav",
                    "profile_metadata_has_sample_payload": False,
                    "export_contains_raw_sample_bytes": False,
                },
                "profile_export_validated": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:42:30+00:00",
                    "profile_id": voicebox_profile_id,
                    "export_format": "b1-ai-hub-voice-profile/v1",
                    "contains_sensitive_data": True,
                    "sample_artifact_count": 1,
                    "exported_sample_artifact_url": voicebox_sample_url,
                    "exported_sample_artifact_sha256": voicebox_sample_sha,
                },
                "profile_delete_audited": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:42:45+00:00",
                    "profile_id": voicebox_profile_id,
                    "deleted_status": "deleted",
                    "audit_event_types": ["voice_profile.deleted", "voice_profile.exported", "voice_profile.sample_uploaded"],
                    "sample_upload_audit_target_id": voicebox_sample_id,
                    "profile_export_audit_target_id": voicebox_profile_id,
                    "profile_delete_audit_target_id": voicebox_profile_id,
                    "sample_upload_audit_bytes": 3244,
                    "sample_upload_audit_sha256": voicebox_sample_sha,
                    "audit_metadata_redacted": True,
                },
                "speech_or_limitation_recorded": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:43:00+00:00",
                    "mode": "speech_validated",
                    "model": "tts-quality",
                    "byte_count": 4096,
                    "sha256": voicebox_speech_sha,
                    "content_type": "audio/wav",
                    **voicebox_identity,
                },
                "websocket_or_limitation_recorded": {
                    "status": "ok",
                    "recorded_at": "2026-07-24T12:44:00+00:00",
                    "mode": "websocket_validated",
                    "path": "/ws",
                    "received_type": "none",
                    **voicebox_identity,
                },
            },
            "sample_count": 5,
            "sample_labels": [
                "voicebox-proxy-build-info",
                "voicebox-native-http",
                "voice-profile-lifecycle",
                "voice-sample-artifact",
                "voicebox-speech",
            ],
        },
        "security_acceptance": {
            "available": True,
            "format": "b1-ai-hub-security-acceptance/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/security-acceptance.json",
            "generated_at": "2026-07-24T12:50:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "unauthenticated_requests_rejected",
                "under_scoped_requests_rejected",
                "cors_credentials_not_wildcard",
                "csrf_browser_mutation_rejected",
                "comfyui_management_routes_blocked",
                "import_ssrf_blocked",
                "import_metadata_ssrf_blocked",
                "import_private_network_blocked",
                "import_plain_http_blocked",
                "artifact_traversal_blocked",
                "artifact_authorization_enforced",
                "runtime_agent_mutation_guard",
                "runtime_agent_arbitrary_runtime_rejected",
                "runtime_agent_arbitrary_logs_rejected",
                "logs_redacted",
            ],
            "missing_checks": [],
            "missing_security_evidence": [],
            "security_rejection_check_count": 15,
            "security_log_lines_checked": 42,
            "checks": sample_security_checks(),
            "sample_count": 15,
            "sample_labels": [
                "unauthenticated-admin",
                "under-scoped-admin",
                "cors-denied-origin",
                "csrf-missing-token",
                "comfyui-manager-denied",
                "import-ssrf-blocked",
                "import-metadata-ssrf-blocked",
                "import-private-network-blocked",
                "import-plain-http-blocked",
                "artifact-traversal-denied",
                "artifact-authorization-denied",
                "runtime-agent-mutation-guard",
                "runtime-agent-arbitrary-runtime-denied",
                "runtime-agent-arbitrary-logs-denied",
                "service-logs-redacted",
            ],
        },
        "restart_reconciliation": {
            "available": True,
            "format": "b1-ai-hub-restart-reconciliation-acceptance/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/restart-reconciliation.json",
            "generated_at": "2026-07-24T12:55:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "control_plane_restarted",
                "cpu_runner_reconciled",
                "gpu_runner_reconciled",
                "waiting_jobs_requeued",
                "active_jobs_marked_recovery_required",
                "interrupted_job_ids_recorded",
                "resumable_comfyui_native_prompts_reattached",
            ],
            "missing_checks": [],
            "missing_reconciliation_evidence": [],
            "restart_control_plane_started_at": "2026-07-24T12:51:10+00:00",
            "restart_expected_after": "2026-07-24T12:50:55+00:00",
            "restart_requeued_waiting_count": 2,
            "restart_recovery_required_count": 1,
            "restart_resumed_comfyui_native_count": 1,
            "restart_requeued_job_ids": ["job_cpu_waiting_1", "job_gpu_waiting_1"],
            "restart_recovery_required_job_ids": ["job_gpu_active_1"],
            "restart_resumed_comfyui_native_job_ids": ["job_native_1"],
            "restart_native_prompt_ids": ["prompt_native_1"],
            "checks": sample_restart_reconciliation_checks(),
            "sample_count": 2,
            "sample_labels": ["startup-reconciliation", "recovered-job-counts"],
        },
        "backup_migration_rollback": {
            "available": True,
            "format": "b1-ai-hub-backup-migration-rollback-acceptance/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/backup-migration-rollback.json",
            "generated_at": "2026-07-24T12:56:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "b1_backup_created",
                "b1_backup_verified",
                "b1_restore_rehearsed",
                "old_stack_inventory_reviewed",
                "old_stack_backup_verified",
                "open_webui_migration_plan_reviewed",
                "cutover_plan_reviewed",
                "rollback_rehearsed",
                "old_resources_preserved",
            ],
            "missing_checks": [],
            "missing_backup_migration_rollback_evidence": [],
            "backup_b1_files_verified": 12,
            "backup_restore_files_verified": 12,
            "backup_old_stack_files_verified": 4,
            "backup_preserved_resource_count": 4,
            "backup_preserved_resource_counts_by_type": {
                "containers_to_restart_for_rollback": 1,
                "systemd_services_to_restart_for_rollback": 1,
                "docker_volumes_preserved": 1,
                "host_paths_preserved": 1,
            },
            "backup_systemd_services_preserved_count": 1,
            "backup_preserved_resources_sha256": acceptance._preserved_resources_sha256(
                {
                    "containers_to_restart_for_rollback": ["old-open-webui"],
                    "systemd_services_to_restart_for_rollback": ["ollama.service"],
                    "docker_volumes_preserved": ["open-webui-data"],
                    "host_paths_preserved": ["/srv/old-ai/docker-compose.yaml"],
                }
            ),
            "backup_b1_archive_sha256": "e" * 64,
            "backup_old_stack_archive_sha256": "f" * 64,
            "backup_rollback_cutover_plan_sha256": "c" * 64,
            "backup_rollback_actions_sha256": "d" * 64,
            "backup_open_webui_strategy": "preserve-backed-up-sqlite-and-test-supported-open-webui-import",
            "checks": sample_backup_migration_rollback_checks(),
            "sample_count": 4,
            "sample_labels": ["b1-backup", "restore-test", "old-stack-backup", "rollback-runbook"],
        },
    }
    payload.update(overrides)
    return payload


TLS_ROUTE_HOSTS = {
    "chat": "ai.b1.germering",
    "control": "control.ai.b1.germering",
    "media": "media.ai.b1.germering",
    "comfy": "comfy.ai.b1.germering",
    "voice": "voice.ai.b1.germering",
    "models": "models.ai.b1.germering",
    "api": "api.ai.b1.germering",
}
TLS_ROUTE_PATHS = {
    "chat": "/",
    "control": "/",
    "media": "/",
    "comfy": "/healthz",
    "voice": "/healthz",
    "models": "/healthz",
    "api": "/healthz",
}


def sample_tls_routing_check(route_keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    selected = route_keys or ("chat", "control", "media", "comfy", "voice", "models", "api")
    return {
        "name": "tls:routing",
        "status": "ok",
        "detail": "TLS gateway routes and security headers checked",
        "data": {
            "routes": [
                {
                    "url": f"https://{TLS_ROUTE_HOSTS[key]}{TLS_ROUTE_PATHS[key]}",
                    "route_keys": [key],
                    "status": "ok",
                    "http_status": 200,
                    "security_headers": "ok",
                }
                for key in selected
            ],
            "expected_route_keys": ["chat", "control", "media", "comfy", "voice", "models", "api"],
            "verify_tls": True,
            "ca_file": "/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt",
        },
    }


def sample_caddy_ca_check(status: str = "ok") -> dict[str, Any]:
    return {
        "name": "tls:caddy-ca",
        "status": status,
        "detail": "Caddy internal CA root is exportable for trusted LAN clients" if status == "ok" else "Caddy internal CA root is not exportable",
        "data": {
            "object": "caddy_internal_ca",
            "status": "ok" if status == "ok" else "missing",
            "tls_mode": "internal",
            "required": True,
            "available": status == "ok",
            "path": "/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt",
            "sha256": "a" * 64 if status == "ok" else None,
            "fingerprint_sha256": ":".join(["AA"] * 32) if status == "ok" else None,
            "download_url": "/admin/tls/caddy-ca/root.crt" if status == "ok" else None,
            "blockers": [] if status == "ok" else ["Caddy internal CA root certificate has not been generated yet"],
        },
    }


def sample_comfyui_build_info_check(status: str = "ok") -> dict[str, Any]:
    return {
        "name": "runtime:comfyui-build-info",
        "status": status,
        "detail": "ComfyUI runtime reports pinned B1 build metadata" if status == "ok" else "ComfyUI build-info hook did not report pinned metadata",
        "data": {
            "required": True,
            "runtime": "comfyui",
            "build_info": {
                "status": "ok" if status == "ok" else "unconfigured",
                "runtime": "comfyui",
                "action": "build-info",
                "hook": "b1-comfyui-runtime-hooks",
                "hook_version": "b1-comfyui-hooks/v0.3.77-b1",
                "upstream_repository": "Comfy-Org/ComfyUI",
                "upstream_version": "v0.3.77",
                "upstream_commit": "59afc3984868289f808d02fa5cd180edfb2de240" if status == "ok" else "",
                "source_archive_sha256": "0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1" if status == "ok" else "",
                "pinned": status == "ok",
            },
        },
    }


def sample_comfyui_status_check(status: str = "ok") -> dict[str, Any]:
    return {
        "name": "runtime:comfyui-status",
        "status": status,
        "detail": "ComfyUI runtime reports lifecycle status" if status == "ok" else "ComfyUI status hook did not report lifecycle status",
        "data": {
            "required": True,
            "runtime": "comfyui",
            "status": {
                "status": "ok" if status == "ok" else "unconfigured",
                "runtime": "comfyui",
                "action": "status",
                "queue": {"running": 0, "queued": 0, "tasks_remaining": 0},
                "memory": {"available": True, "device": "cuda:0", "loaded_model_count": 0},
                "model_folders": {
                    "folder_count": 2,
                    "file_count": 3,
                    "folders": [
                        {"folder": "checkpoints", "available": True, "file_count": 2},
                        {"folder": "vae", "available": True, "file_count": 1},
                    ],
                },
                "build_info": sample_comfyui_build_info_check(status)["data"]["build_info"],
                "capabilities": {"actions": ["status", "build-info", "load", "warm", "smoke", "unload"]},
            },
        },
    }


def sample_compose_selection(**overrides: str) -> dict[str, Any]:
    env = {
        "B1_COMPOSE_FILE": "compose.yaml:compose.production-localai.yaml:compose.production-comfyui.yaml:compose.production-voicebox.yaml",
        "B1_COMPOSE_PROFILES": "voicebox",
        "B1_RUNTIME_PRODUCTION_REQUIRED": "localai,comfyui,audio-cpu,voicebox",
        **overrides,
    }
    return acceptance.compose_selection_snapshot(env)


def sample_report(**overrides: Any) -> dict[str, Any]:
    report_id = overrides.pop("report_id", "acceptance-20260724t120000z-deadbeef")
    self_test = overrides.pop(
        "self_test",
        {
            "status": "ok",
            "checks": [
                {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                sample_tls_routing_check(),
                sample_caddy_ca_check(),
                sample_comfyui_build_info_check(),
                sample_comfyui_status_check(),
                {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                {
                    "name": "hardware:resource-policy",
                    "status": "ok",
                    "detail": "observed GPU/RAM satisfy the effective resource policy and reserves",
                },
                {"name": "runtimes:production-readiness", "status": "ok", "detail": "required runtimes are production-ready"},
                {
                    "name": "runtime-agent:mutation-guard",
                    "status": "ok",
                    "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                },
            ],
        },
    )
    return acceptance.build_report(
        report_id=report_id,
        created_by="admin_1",
        label=overrides.pop("label", "cutover dry run"),
        notes=overrides.pop("notes", "operator notes"),
        generated_at=datetime(2026, 7, 24, 13, 0, tzinfo=UTC),
        runtime_deployment_mode=overrides.pop("runtime_deployment_mode", "production"),
        resource_policy=overrides.pop("resource_policy", {"gpu_total_vram_gib": 12.0, "host_total_ram_gib": 32.0}),
        maintenance=overrides.pop("maintenance", {"enabled": True, "reason": "cutover validation"}),
        self_test=self_test,
        metrics=overrides.pop(
            "metrics",
            {
                "gpu": {"available": True, "device_count": 1, "memory_total_mib": 12288, "memory_used_mib": 1024},
                "jobs": {"completed_last_hour": 4, "failed_last_hour": 0, "cancelled_last_hour": 0, "recovery_required_last_hour": 0},
            },
        ),
        admission=overrides.pop("admission", {"storage": {"root": "/srv/b1-ai-hub"}}),
        scheduler_lease=overrides.pop("scheduler_lease", {"owner": "idle"}),
        runtime_states=overrides.pop("runtime_states", [{"runtime": "comfyui", "status": "idle", "stage": "idle"}]),
        runtime_reservations=overrides.pop("runtime_reservations", []),
        deployment=overrides.pop(
            "deployment",
            {
                "services": [
                    {
                        "name": "control-plane",
                        "containers": [
                            {
                                "short_id": "abc123",
                                "state": "running",
                                "image": "ghcr.io/b1/control-plane:0.2.0@sha256:" + "a" * 64,
                                "image_id": "sha256:" + "b" * 64,
                            }
                        ],
                    }
                ]
            },
        ),
        deployment_pins=overrides.pop("deployment_pins", None),
        compose_selection=overrides.pop("compose_selection", sample_compose_selection()),
        recent_updates=overrides.pop(
            "recent_updates",
            [
                {
                    "id": "update_1",
                    "status": "validated",
                    "stage": "health_check_completed",
                    "image_refs": [{"service": "control-plane", "image": "ghcr.io/b1/control-plane:0.2.0@sha256:" + "a" * 64}],
                }
            ],
        ),
        operator_evidence=overrides.pop("operator_evidence", complete_operator_evidence()),
        operator_evidence_notes=overrides.pop("operator_evidence_notes", {}),
        model_measurement_coverage=overrides.pop("model_measurement_coverage", sample_model_measurement_coverage()),
        cutover_preservation=overrides.pop("cutover_preservation", sample_cutover_preservation()),
        live_evidence=overrides.pop("live_evidence", sample_live_evidence()),
        source_control=overrides.pop(
            "source_control",
            {
                "available": True,
                "source": "environment",
                "source_commit": "c" * 40,
                "short_commit": "c" * 12,
                "source_ref": "agent/test",
                "version": "0.2.0",
            },
        ),
        handoff=overrides.pop("handoff", None),
    )


class AcceptanceReportTests(unittest.TestCase):
    def symlink_or_skip(self, target: Path, link: Path, *, target_is_directory: bool = False) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink creation is unavailable")
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

    def test_report_writer_persists_json_markdown_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = sample_report()
            summary = acceptance.write_report(root, report)

            report_dir = root / report["id"]
            json_path = report_dir / "report.json"
            markdown_path = report_dir / "report.md"
            checksum_path = report_dir / "SHA256SUMS"

            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertTrue(checksum_path.is_file())
            self.assertEqual(summary["id"], report["id"])
            self.assertTrue(summary["operator_handoff_ready"])
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("Operator handoff ready: true", markdown)
            self.assertIn("## Handoff Quick Reference", markdown)
            self.assertIn("https://control.ai.b1.germering/", markdown)
            self.assertIn("| fresh_install | docker compose up -d |", markdown)
            self.assertIn("| production_env | make prepare-production-env |", markdown)
            self.assertIn("| optional_preflight | make bootstrap |", markdown)
            self.assertIn("/srv/b1-ai-hub/secrets/admin_bootstrap_key", markdown)
            self.assertIn("Upgrade system RAM from 32 GB to at least 64 GB first", markdown)
            self.assertIn("No known limitations recorded in this acceptance report.", markdown)
            self.assertIn("## Deployment Services", markdown)
            self.assertIn("ghcr.io/b1/control-plane", markdown)
            self.assertIn("## Recent Update Records", markdown)
            self.assertIn("## Source Control", markdown)
            self.assertIn("## Deployment Pins", markdown)
            self.assertIn("### Compose Selection", markdown)
            self.assertIn("compose.production-comfyui.yaml", markdown)
            self.assertIn("voicebox", markdown)
            self.assertIn("## Database Model-Smoke Coverage", markdown)
            self.assertIn("RTX 3060 GPU acceptance", markdown)
            self.assertIn("chat-default", markdown)
            self.assertIn("caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d", markdown)
            self.assertIn("localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973", markdown)
            self.assertIn("59afc3984868289f808d02fa5cd180edfb2de240", markdown)
            self.assertIn("d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083", markdown)
            self.assertIn("## Operator Evidence", markdown)
            self.assertIn("RTX 3060/32 GB cross-runtime acceptance", markdown)
            self.assertIn("## Live Acceptance Evidence", markdown)
            self.assertIn("Repository quality gates", markdown)
            self.assertIn("repository-quality.json", markdown)
            self.assertIn("quality_container", markdown)
            self.assertIn("make quality-container", markdown)
            self.assertIn("live-smoke.json", markdown)
            self.assertIn("Live stack smoke", markdown)
            self.assertIn("cross-runtime-gpu.json", markdown)
            self.assertIn("b1-chat-default@1.0.0", markdown)
            self.assertIn("| chat-default | b1-chat-default@1.0.0 | localai | 1 |", markdown)
            self.assertIn("installed-workflows.json", markdown)
            self.assertIn("Installed workflow acceptance", markdown)
            self.assertIn("native-comfyui.json", markdown)
            self.assertIn("Native ComfyUI compatibility", markdown)
            self.assertIn("Optional legacy ComfyUI listener", markdown)
            self.assertIn("remote-nodes-non-comfy.json", markdown)
            self.assertIn("Remote-node non-Comfy compatibility", markdown)
            self.assertIn("modelhub-client-sync.json", markdown)
            self.assertIn("Model Hub client sync", markdown)
            self.assertIn("voicebox-remote.json", markdown)
            self.assertIn("Voicebox remote compatibility", markdown)
            self.assertIn("security-acceptance.json", markdown)
            self.assertIn("Security acceptance", markdown)
            self.assertIn("restart-reconciliation.json", markdown)
            self.assertIn("Restart reconciliation", markdown)
            self.assertIn("## Old Resources Preserved For Rollback", markdown)
            self.assertIn("old-open-webui", markdown)
            self.assertIn("open-webui-data", markdown)

            checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
            checksums = dict(line.split("  ", 1)[::-1] for line in checksum_lines)
            self.assertEqual(checksums["report.json"], hashlib.sha256(json_path.read_bytes()).hexdigest())
            self.assertEqual(checksums["report.md"], hashlib.sha256(markdown_path.read_bytes()).hexdigest())
            self.assertEqual(acceptance.report_file_path(root, report["id"], "report.json"), json_path.resolve())
            self.assertEqual(acceptance.report_file_path(root, report["id"], "report.md"), markdown_path.resolve())
            self.assertEqual(acceptance.report_file_path(root, report["id"], "SHA256SUMS"), checksum_path.resolve())

            loaded = acceptance.load_report(root, report["id"])
            listed = acceptance.list_reports(root)

        self.assertEqual(loaded["format"], acceptance.REPORT_FORMAT)
        self.assertEqual(loaded["handoff"]["default_urls"][1]["url"], "https://control.ai.b1.germering/")
        self.assertEqual(loaded["handoff"]["commands"][0]["key"], "fresh_install")
        self.assertEqual(loaded["handoff"]["commands"][0]["command"], "docker compose up -d")
        self.assertTrue(loaded["deployment_pins"]["status"] == "ok")
        self.assertTrue(summary["deployment_pins_ready"])
        self.assertEqual([item["id"] for item in listed], [report["id"]])

    def test_report_blocks_handoff_when_fresh_install_command_drifts(self) -> None:
        report = sample_report(
            handoff={"commands": [{"key": "fresh_install", "command": "make bootstrap && docker compose up -d"}]},
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("handoff fresh-install command must be exactly: docker compose up -d", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_repository_quality_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["repository_quality"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["repository_quality_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("repository quality evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_dirty_repository_quality_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["repository_quality"] = {
            **live_evidence["repository_quality"],
            "status": "incomplete",
            "source_dirty": True,
            "dirty_path_count": 2,
            "missing_quality_evidence": ["source_dirty_false", "dirty_path_count_zero"],
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        blockers = "; ".join(report["acceptance_blockers"])
        self.assertIn("repository quality evidence status is incomplete", blockers)
        self.assertIn("repository quality evidence is missing detailed proof: source_dirty_false, dirty_path_count_zero", blockers)

    def test_repository_quality_snapshot_requires_result_summary(self) -> None:
        payload = sample_live_evidence()["repository_quality"]
        payload["checks"]["quality_container"] = {
            key: value
            for key, value in payload["checks"]["quality_container"].items()
            if key != "result_summary"
        }

        snapshot = acceptance.repository_quality_evidence_snapshot(payload)

        self.assertEqual(snapshot["status"], "ok")
        self.assertIn("quality_container.result_summary", snapshot["missing_quality_evidence"])

    def test_repository_quality_snapshot_requires_passed_result_details(self) -> None:
        payload = sample_live_evidence()["repository_quality"]
        summary = payload["checks"]["quality_container"]["result_summary"]
        summary["outcome"] = "unverified"
        summary["exit_code"] = None
        summary["test_results"][0]["status"] = "failed"

        snapshot = acceptance.repository_quality_evidence_snapshot(payload)

        self.assertIn("quality_container.result_summary.outcome_passed", snapshot["missing_quality_evidence"])
        self.assertIn("quality_container.result_summary.exit_code_zero", snapshot["missing_quality_evidence"])
        first_label = acceptance.REPOSITORY_QUALITY_REQUIRED_COVERAGE["quality_container"][0]
        self.assertIn(
            f"quality_container.result_summary.test_results.{first_label}.status",
            snapshot["missing_quality_evidence"],
        )

    def test_report_blocks_handoff_when_repository_quality_commit_mismatches_source(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["repository_quality"] = {
            **live_evidence["repository_quality"],
            "source_commit": "d" * 40,
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn(
            "repository quality gates evidence source provenance is not acceptable: source_commit does not match report source-control commit",
            report["acceptance_blockers"],
        )

    def test_deployment_pin_snapshot_reads_repository_and_matches_bundled_runtime_pins(self) -> None:
        repository = acceptance.deployment_pins_snapshot(ROOT)
        bundled = acceptance.deployment_pins_snapshot(Path("/tmp/b1-ai-hub-no-repo"))

        self.assertEqual(repository["source"], "repository")
        self.assertEqual(repository["status"], "ok")
        self.assertEqual(repository["integrity"]["floating_latest_refs"], [])
        self.assertEqual(repository["integrity"]["unpinned_refs"], [])
        self.assertEqual(repository["integrity"]["missing_build_pins"], [])
        self.assertEqual(repository["integrity"]["missing_runtime_pins"], [])
        self.assertEqual(repository["integrity"]["missing_sections"], [])
        self.assertEqual(bundled["source"], "bundled")
        self.assertEqual(bundled["status"], "ok")

        compose = {
            (item["file"], item["service"]): item["default_image"]
            for item in repository["compose_images"]
        }
        self.assertEqual(
            compose[("compose.yaml", "gateway")],
            "caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d",
        )
        self.assertEqual(
            compose[("compose.production-comfyui.yaml", "comfyui")],
            "b1-ai-hub/comfyui:v0.3.77-b1",
        )
        self.assertEqual(
            compose[("compose.monitoring.yaml", "prometheus")],
            "prom/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996",
        )
        builds = {
            (item["file"], item["service"]): (item["normalized_context"], item["component"])
            for item in repository["compose_builds"]
        }
        self.assertEqual(builds[("compose.yaml", "control-plane")], ("services/control-plane", "control-plane"))
        self.assertEqual(builds[("compose.yaml", "localai")], ("services/mock-runtime", "mock-runtime"))
        self.assertEqual(builds[("compose.production-comfyui.yaml", "comfyui")], ("deploy/comfyui", "comfyui"))

        bases = {
            (item["file"], item["component"], item["stage"]): item["default_image"]
            for item in repository["dockerfile_bases"]
        }
        self.assertEqual(
            bases[("deploy/localai/Dockerfile", "localai", "final")],
            "localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973",
        )
        self.assertEqual(
            bases[("deploy/comfyui/Dockerfile", "comfyui", "final")],
            "pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime@sha256:e05438443ae3c407e8d04447091a959dbb6757b6290b128770c3c787d4bd442b",
        )
        self.assertEqual(
            bases[("deploy/voicebox/Dockerfile", "voicebox", "backend-builder")],
            "python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93",
        )
        self.assertEqual(
            bases[("services/mock-runtime/Dockerfile", "mock-runtime", "final")],
            "python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7",
        )

        repository_runtimes = {item["runtime"]: item for item in repository["runtime_sources"]}
        bundled_runtimes = {item["runtime"]: item for item in bundled["runtime_sources"]}
        for runtime in ("localai", "comfyui", "voicebox", "audio-cpu"):
            with self.subTest(runtime=runtime):
                self.assertEqual(repository_runtimes[runtime], bundled_runtimes[runtime])

    def test_compose_selection_snapshot_requires_overlays_for_required_runtimes(self) -> None:
        snapshot = acceptance.compose_selection_snapshot(
            {
                "B1_COMPOSE_FILE": "compose.yaml:compose.production-localai.yaml:compose.production-comfyui.yaml",
                "B1_COMPOSE_PROFILES": "",
                "B1_RUNTIME_PRODUCTION_REQUIRED": "localai,comfyui,audio-cpu,voicebox",
            }
        )

        self.assertEqual(snapshot["status"], "blocked")
        self.assertEqual(snapshot["missing_files"], ["compose.production-voicebox.yaml"])
        self.assertEqual(snapshot["missing_profiles"], ["voicebox"])

    def test_report_blocks_handoff_when_production_compose_overlays_are_missing(self) -> None:
        report = sample_report(
            compose_selection=acceptance.compose_selection_snapshot(
                {
                    "B1_COMPOSE_FILE": "compose.yaml:compose.production-localai.yaml",
                    "B1_COMPOSE_PROFILES": "",
                    "B1_RUNTIME_PRODUCTION_REQUIRED": "localai,comfyui,audio-cpu,voicebox",
                }
            )
        )
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["compose_selection_ready"])
        blockers = "; ".join(report["acceptance_blockers"])
        self.assertIn("production Compose selection is incomplete", blockers)
        self.assertIn("compose.production-comfyui.yaml", blockers)
        self.assertIn("compose.production-voicebox.yaml", blockers)
        self.assertIn("voicebox", blockers)

    def test_report_blocks_handoff_for_unsafe_deployment_pin_manifest(self) -> None:
        report = sample_report(
            deployment_pins={
                "format": acceptance.DEPLOYMENT_PINS_FORMAT,
                "source": "test",
                "compose_images": [{"file": "compose.yaml", "service": "bad", "image": "example.invalid/bad:latest"}],
                "compose_builds": [{"file": "compose.yaml", "service": "bad-build", "context": "./unknown"}],
                "dockerfile_bases": [],
                "runtime_sources": [{"runtime": "comfyui", "upstream_commit": "f" * 40}],
            }
        )
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["deployment_pins_ready"])
        blockers = "; ".join(report["acceptance_blockers"])
        self.assertIn("deployment pin manifest contains floating latest image refs", blockers)
        self.assertIn("deployment pin manifest is missing Compose build pins", blockers)
        self.assertIn("deployment pin manifest is missing runtime source pins: comfyui.tarball_sha256", blockers)
        self.assertIn("deployment pin manifest is missing sections: dockerfile_bases", blockers)

    def test_report_file_path_rejects_unknown_names_traversal_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = sample_report()
            acceptance.write_report(root, report)

            for filename in ("../report.json", "report.json/../report.md", "notes.txt", ""):
                with self.subTest(filename=filename):
                    with self.assertRaises(acceptance.AcceptanceReportError):
                        acceptance.report_file_path(root, report["id"], filename)

            (root / report["id"] / "report.md").unlink()
            (root / report["id"] / "report.md").symlink_to(root / report["id"] / "report.json")
            with self.assertRaises(acceptance.AcceptanceReportError) as raised:
                acceptance.report_file_path(root, report["id"], "report.md")
            self.assertIn("symlink", str(raised.exception))

    def test_load_report_rejects_symlinked_json_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = sample_report()
            acceptance.write_report(root, report)
            report_dir = root / report["id"]
            outside = root / "outside.json"
            outside.write_text(json.dumps({"format": acceptance.REPORT_FORMAT, "id": "outside"}), encoding="utf-8")
            (report_dir / "report.json").unlink()
            self.symlink_or_skip(outside, report_dir / "report.json")

            with self.assertRaisesRegex(acceptance.AcceptanceReportError, "symlink"):
                acceptance.load_report(root, report["id"])

            self.assertEqual(json.loads(outside.read_text(encoding="utf-8"))["id"], "outside")

    def test_report_root_symlink_is_rejected_for_reads_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real_root = base / "acceptance-real"
            real_root.mkdir()
            linked_root = base / "acceptance-link"
            self.symlink_or_skip(real_root, linked_root, target_is_directory=True)

            report = sample_report()
            for operation in (
                lambda: acceptance.report_directory(linked_root, report["id"]),
                lambda: acceptance.list_reports(linked_root),
                lambda: acceptance.write_report(linked_root, report),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(acceptance.AcceptanceReportError, "root is a symlink"):
                        operation()

    def test_atomic_report_write_rejects_symlink_and_non_regular_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.json"
            outside.write_bytes(b"outside")
            linked = root / "report.json"
            self.symlink_or_skip(outside, linked)

            with self.assertRaisesRegex(acceptance.AcceptanceReportError, "symlink"):
                acceptance._write_atomic(linked, b"report")  # noqa: SLF001

            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertTrue(linked.is_symlink())

            directory_target = root / "report.md"
            directory_target.mkdir()
            with self.assertRaisesRegex(acceptance.AcceptanceReportError, "not a regular file"):
                acceptance._write_atomic(directory_target, b"markdown")  # noqa: SLF001

            self.assertTrue(directory_target.is_dir())

    def test_handoff_records_voicebox_upstream_limitations(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["voicebox_remote"]["checks"]["websocket_or_limitation_recorded"] = {
            "status": "ok",
            "recorded_at": "2026-07-24T12:44:00+00:00",
            "mode": "upstream_limitation",
            "proxy_version": "b1-voicebox-proxy/v0.5.0-b1",
            "upstream_repository": "jamiepine/voicebox",
            "upstream_version": "v0.5.0",
            "upstream_commit": "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d",
            "source_archive_sha256": "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083",
            "limitation": "Pinned upstream exposes no stable WebSocket route for this profile.",
        }

        report = sample_report(live_evidence=live_evidence)
        limitations = report["handoff"]["known_limitations"]

        self.assertTrue(report["operator_handoff_ready"])
        self.assertIn(
            {
                "source": "voicebox_remote.websocket_or_limitation_recorded",
                "detail": "Pinned upstream exposes no stable WebSocket route for this profile. (v0.5.0 2bcb98d1a8b6)",
            },
            limitations,
        )
        self.assertIn("Pinned upstream exposes no stable WebSocket route", acceptance.markdown_report(report))

    def test_voicebox_snapshot_requires_sample_export_delete_evidence(self) -> None:
        snapshot = acceptance.voicebox_evidence_snapshot(
            {
                "format": "b1-ai-hub-voicebox-remote-compatibility/v1",
                "generated_at": "2026-07-24T12:45:00+00:00",
                "base_url": "https://voice.ai.b1.germering",
                "status": "ok",
                "checks": {
                    "native_http_proxy_accessible": {"status": "ok"},
                    "profile_lifecycle_validated": {"status": "ok"},
                    "speech_or_limitation_recorded": {"status": "ok"},
                    "websocket_or_limitation_recorded": {"status": "ok"},
                },
                "samples": [{"label": "voice-profile-lifecycle"}],
            }
        )

        self.assertEqual(
            snapshot["missing_checks"],
            ["proxy_build_info_validated", "sample_artifact_protected", "profile_export_validated", "profile_delete_audited"],
        )

    def test_voicebox_snapshot_requires_profile_sample_and_runtime_details(self) -> None:
        snapshot = acceptance.voicebox_evidence_snapshot(
            {
                "format": "b1-ai-hub-voicebox-remote-compatibility/v1",
                "generated_at": "2026-07-24T12:45:00+00:00",
                "base_url": "https://voice.ai.b1.germering",
                "status": "ok",
                "checks": ok_checks(acceptance.VOICEBOX_REQUIRED_CHECKS),
                "samples": [{"label": "voice-profile-lifecycle"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], [])
        self.assertIn("proxy_build_info_validated.proxy_version", snapshot["missing_compatibility_evidence"])
        self.assertIn("proxy_build_info_validated.upstream_commit", snapshot["missing_compatibility_evidence"])
        self.assertIn("native_http_proxy_accessible.http_status", snapshot["missing_compatibility_evidence"])
        self.assertIn("native_http_proxy_accessible.upstream_commit", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_lifecycle_validated.profile_id", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_lifecycle_validated.sample_artifact_url", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_lifecycle_validated.sample_artifact_sha256", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_lifecycle_validated.fetched_sample_artifact_url", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_lifecycle_validated.fetched_sample_artifact_sha256", snapshot["missing_compatibility_evidence"])
        self.assertIn("sample_artifact_protected.sample_artifact_url", snapshot["missing_compatibility_evidence"])
        self.assertIn("sample_artifact_protected.sample_id", snapshot["missing_compatibility_evidence"])
        self.assertIn("sample_artifact_protected.sample_artifact_sha256", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_export_validated.export_format", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_export_validated.exported_sample_artifact_url", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_export_validated.exported_sample_artifact_sha256", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.deleted_status", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.audit_event_types", snapshot["missing_compatibility_evidence"])
        self.assertIn("speech_or_limitation_recorded.mode", snapshot["missing_compatibility_evidence"])
        self.assertIn("websocket_or_limitation_recorded.mode", snapshot["missing_compatibility_evidence"])

    def test_voicebox_snapshot_requires_sample_hash_url_and_audit_consistency(self) -> None:
        payload = sample_live_evidence()["voicebox_remote"]
        payload = {
            **payload,
            "checks": {
                **payload["checks"],
                "profile_lifecycle_validated": {
                    **payload["checks"]["profile_lifecycle_validated"],
                    "fetched_sample_artifact_url": "/artifacts/voicebox/references/other.wav",
                    "fetched_sample_artifact_sha256": "0" * 64,
                },
                "sample_artifact_protected": {
                    **payload["checks"]["sample_artifact_protected"],
                    "sample_artifact_url": "/artifacts/voicebox/references/mismatch.wav",
                    "sample_artifact_sha256": "1" * 64,
                    "sample_artifact_mime_type": "audio/mpeg",
                },
                "profile_export_validated": {
                    **payload["checks"]["profile_export_validated"],
                    "sample_artifact_count": 2,
                    "exported_sample_artifact_url": "/artifacts/voicebox/references/export-mismatch.wav",
                    "exported_sample_artifact_sha256": "2" * 64,
                },
                "profile_delete_audited": {
                    **payload["checks"]["profile_delete_audited"],
                    "audit_event_types": ["voice_profile.deleted"],
                    "sample_upload_audit_target_id": "sample_other",
                    "profile_export_audit_target_id": "vp_other",
                    "profile_delete_audit_target_id": "vp_other",
                    "sample_upload_audit_bytes": 1,
                    "sample_upload_audit_sha256": "3" * 64,
                    "audit_metadata_redacted": False,
                },
            },
        }

        snapshot = acceptance.voicebox_evidence_snapshot(payload)

        self.assertIn("profile_lifecycle_validated.fetched_sample_artifact_url_matches_created", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_lifecycle_validated.fetched_sample_artifact_sha256_matches_created", snapshot["missing_compatibility_evidence"])
        self.assertIn("sample_artifact_protected.sample_artifact_url_matches_lifecycle", snapshot["missing_compatibility_evidence"])
        self.assertIn("sample_artifact_protected.sample_artifact_sha256_matches_lifecycle", snapshot["missing_compatibility_evidence"])
        self.assertIn("sample_artifact_protected.sample_artifact_mime_type", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_export_validated.sample_artifact_count_matches_lifecycle", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_export_validated.exported_sample_artifact_url_matches_sample", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_export_validated.exported_sample_artifact_sha256_matches_sample", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.audit_event_types", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.sample_upload_audit_target_id_matches_sample", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.profile_export_audit_target_id_matches_profile", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.profile_delete_audit_target_id_matches_profile", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.sample_upload_audit_bytes_matches_sample", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.sample_upload_audit_sha256_matches_sample", snapshot["missing_compatibility_evidence"])
        self.assertIn("profile_delete_audited.audit_metadata_redacted", snapshot["missing_compatibility_evidence"])

    def test_voicebox_snapshot_rejects_limitations_without_proxy_build_match(self) -> None:
        evidence = sample_live_evidence()["voicebox_remote"]
        evidence["checks"]["speech_or_limitation_recorded"] = {
            **evidence["checks"]["speech_or_limitation_recorded"],
            "mode": "upstream_limitation",
            "limitation": "Pinned upstream does not support this speech surface.",
            "upstream_commit": "0" * 40,
        }

        snapshot = acceptance.voicebox_evidence_snapshot(evidence)

        self.assertIn("speech_or_limitation_recorded.upstream_commit_matches_build_info", snapshot["missing_compatibility_evidence"])

    def test_report_blocks_handoff_for_degraded_development_snapshot(self) -> None:
        report = sample_report(
            runtime_deployment_mode="development",
            self_test={
                "status": "degraded",
                "checks": [
                    {"name": "gpu:nvml", "status": "warning", "detail": "dev host"},
                    {"name": "runtimes:production-readiness", "status": "warning", "detail": "placeholder runtimes"},
                ],
            },
            metrics={"gpu": {"available": False}, "jobs": {}},
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("self-test status is degraded", report["acceptance_blockers"])
        self.assertIn("runtime deployment mode is not production", report["acceptance_blockers"])
        self.assertIn("required runtimes are not production-ready", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_tls_routing_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("TLS gateway routing check is absent", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_failed_tls_routing_check(self) -> None:
        failed_tls_check = sample_tls_routing_check()
        failed_tls_check["status"] = "failed"
        failed_tls_check["detail"] = "one or more TLS gateway route or security-header checks failed"
        failed_tls_check["data"]["routes"][0]["status"] = "failed"
        failed_tls_check["data"]["routes"][0]["security_headers"] = "failed"
        report = sample_report(
            self_test={
                "status": "failed",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    failed_tls_check,
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("TLS gateway routing check is failed", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_caddy_ca_readiness_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {
                        "name": "hardware:resource-policy",
                        "status": "ok",
                        "detail": "observed GPU/RAM satisfy the effective resource policy and reserves",
                    },
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("Caddy internal CA readiness check is absent", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_failed_caddy_ca_readiness_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "failed",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    sample_caddy_ca_check("failed"),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {
                        "name": "hardware:resource-policy",
                        "status": "ok",
                        "detail": "observed GPU/RAM satisfy the effective resource policy and reserves",
                    },
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("Caddy internal CA readiness check is failed", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_required_comfyui_build_info_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    sample_caddy_ca_check(),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {
                        "name": "hardware:resource-policy",
                        "status": "ok",
                        "detail": "observed GPU/RAM satisfy the effective resource policy and reserves",
                    },
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("ComfyUI build-info readiness check is absent", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_failed_required_comfyui_build_info_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "failed",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    sample_caddy_ca_check(),
                    sample_comfyui_build_info_check("failed"),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {
                        "name": "hardware:resource-policy",
                        "status": "ok",
                        "detail": "observed GPU/RAM satisfy the effective resource policy and reserves",
                    },
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("ComfyUI build-info readiness check is failed", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_required_comfyui_status_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    sample_caddy_ca_check(),
                    sample_comfyui_build_info_check(),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {
                        "name": "hardware:resource-policy",
                        "status": "ok",
                        "detail": "observed GPU/RAM satisfy the effective resource policy and reserves",
                    },
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("ComfyUI status readiness check is absent", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_failed_required_comfyui_status_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "failed",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    sample_caddy_ca_check(),
                    sample_comfyui_build_info_check(),
                    sample_comfyui_status_check("failed"),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {
                        "name": "hardware:resource-policy",
                        "status": "ok",
                        "detail": "observed GPU/RAM satisfy the effective resource policy and reserves",
                    },
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("ComfyUI status readiness check is failed", report["acceptance_blockers"])

    def test_report_does_not_require_comfyui_build_info_when_comfyui_is_not_required(self) -> None:
        report = sample_report(
            compose_selection=sample_compose_selection(B1_RUNTIME_PRODUCTION_REQUIRED="localai,audio-cpu"),
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    sample_caddy_ca_check(),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {
                        "name": "hardware:resource-policy",
                        "status": "ok",
                        "detail": "observed GPU/RAM satisfy the effective resource policy and reserves",
                    },
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            },
        )

        self.assertNotIn("ComfyUI build-info readiness check is absent", report["acceptance_blockers"])
        self.assertNotIn("ComfyUI status readiness check is absent", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_hardware_resource_policy_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("hardware resource policy check is absent", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_failed_hardware_resource_policy_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "failed",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "hardware:resource-policy", "status": "failed", "detail": "largest GPU VRAM is 6144 MiB"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("hardware resource policy check is failed", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_tls_routing_evidence_has_no_routes(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    {"name": "tls:routing", "status": "ok", "detail": "TLS checked", "data": {"routes": []}},
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("TLS gateway routing evidence lists no checked routes", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_tls_routing_evidence_misses_required_host(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(("chat", "control", "media", "comfy", "models", "api")),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "ok",
                        "detail": "runtime-agent mutation surface is authenticated, allowlisted, mTLS-protected, and rate-limited",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("TLS gateway routing evidence is missing required hosts: voice", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_runtime_agent_mutation_guard_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("runtime-agent mutation guard check is absent", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_failed_runtime_agent_mutation_guard_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "failed",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                    sample_tls_routing_check(),
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {
                        "name": "runtime-agent:mutation-guard",
                        "status": "failed",
                        "detail": "missing-auth bypass is enabled",
                    },
                ],
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("runtime-agent mutation guard check is failed", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_required_operator_evidence(self) -> None:
        report = sample_report(operator_evidence={"live_stack_smoke": True})

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(acceptance.public_report_summary(report)["operator_evidence_ready"])
        self.assertIn(
            "operator evidence missing: RTX 3060/32 GB cross-runtime acceptance completed with measured reserves",
            report["acceptance_blockers"],
        )
        self.assertIn(
            "operator evidence missing: Rollback procedure was tested and old resources remain preserved",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_without_database_model_smoke_coverage(self) -> None:
        report = sample_report(model_measurement_coverage=None)
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["model_measurement_coverage_ready"])
        self.assertIn("database model-smoke coverage is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_database_model_smoke_coverage(self) -> None:
        coverage = sample_model_measurement_coverage()
        coverage["status"] = "incomplete"
        coverage["missing_aliases"] = ["image-default"]
        coverage["groups"][1]["status"] = "incomplete"
        coverage["groups"][1]["missing_aliases"] = ["image-default"]
        coverage["groups"][1]["measurements"][1]["ready"] = False
        coverage["groups"][1]["measurements"][1]["blockers"] = ["no persisted ok model smoke measurement exists"]

        report = sample_report(model_measurement_coverage=coverage)
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["model_measurement_coverage_ready"])
        blockers = "; ".join(report["acceptance_blockers"])
        self.assertIn("database model-smoke coverage status is incomplete", blockers)
        self.assertIn("database model-smoke coverage is missing aliases: image-default", blockers)
        self.assertIn("database model-smoke coverage RTX 3060 GPU acceptance/image-default", blockers)
        self.assertIn("no persisted ok model smoke measurement exists", blockers)
        self.assertEqual(report["model_measurement_coverage"]["blocked_count"], 1)
        self.assertEqual(report["model_measurement_coverage"]["next_actions"], [])

    def test_database_model_smoke_coverage_rejects_runtime_mismatch(self) -> None:
        coverage = sample_model_measurement_coverage()
        image_measurement = coverage["groups"][1]["measurements"][1]
        image_measurement["runtime"] = "localai"
        image_measurement["latest_ok_run"]["runtime"] = "localai"

        report = sample_report(model_measurement_coverage=coverage)
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["model_measurement_coverage_ready"])
        normalized = report["model_measurement_coverage"]
        self.assertEqual(normalized["status"], "incomplete")
        self.assertIn("image-default", normalized["missing_aliases"])
        blockers = "; ".join(report["acceptance_blockers"])
        self.assertIn("database model-smoke coverage RTX 3060 GPU acceptance/image-default", blockers)
        self.assertIn("measurement runtime does not match expected runtime", blockers)
        self.assertEqual(normalized["next_actions"][0]["alias"], "image-default")

    def test_database_model_smoke_coverage_rejects_missing_resource_metrics(self) -> None:
        coverage = sample_model_measurement_coverage()
        image_measurement = coverage["groups"][1]["measurements"][1]
        image_measurement["latest_ok_run"].pop("peak_vram_mib")
        image_measurement["latest_resource_estimate"].pop("vram_gib")

        report = sample_report(model_measurement_coverage=coverage)

        self.assertFalse(report["operator_handoff_ready"])
        normalized = report["model_measurement_coverage"]
        self.assertEqual(normalized["status"], "incomplete")
        self.assertIn("image-default", normalized["missing_aliases"])
        blockers = "; ".join(report["acceptance_blockers"])
        self.assertIn("latest run peak_vram_mib is missing for GPU runtime", blockers)
        self.assertIn("latest resource estimate vram_gib is missing for GPU runtime", blockers)

    def test_report_preserves_database_model_smoke_handoff_plan(self) -> None:
        coverage = sample_model_measurement_coverage()
        coverage["status"] = "incomplete"
        coverage["missing_aliases"] = ["image-default"]
        coverage["ready_aliases"] = ["chat-default", "tts-quality", "tts-fast", "stt-default", "image-edit", "video-text"]
        coverage["ready_count"] = 6
        coverage["blocked_count"] = 1
        coverage["blocker_summary"] = [
            {"blocker": "no persisted ok model smoke measurement exists", "aliases": ["image-default"], "count": 1}
        ]
        coverage["next_actions"] = [
            {
                "suite": "gpu_acceptance",
                "suite_label": "RTX 3060 GPU acceptance",
                "alias": "image-default",
                "expected_runtime": "comfyui",
                "status": "installed",
                "resolved_model_version": "b1-image-default@1.0.0",
                "blockers": ["no persisted ok model smoke measurement exists"],
                "action": "Run the Control Center model smoke action for b1-image-default@1.0.0 and persist an ok measurement before live acceptance.",
            }
        ]

        report = sample_report(model_measurement_coverage=coverage)
        rendered = acceptance.markdown_report(report)

        plan = report["model_measurement_coverage"]["handoff_plan"]
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["blocked_aliases"], ["image-default"])
        self.assertEqual(plan["next_actions"][0]["alias"], "image-default")
        self.assertIn("Model-Smoke Coverage", rendered)
        self.assertIn("Run the Control Center model smoke action", rendered)

    def test_report_blocks_handoff_without_deployment_image_evidence(self) -> None:
        report = sample_report(deployment={"services": []}, recent_updates=[])

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("deployment image evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_source_control_evidence(self) -> None:
        report = sample_report(source_control={"available": False, "source": "unavailable", "reason": "git metadata not present"})

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["source_control_ready"])
        self.assertIn("source-control evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_invalid_source_commit(self) -> None:
        report = sample_report(
            source_control={
                "available": True,
                "source": "environment",
                "source_commit": "not-a-commit",
                "source_ref": "release/test",
            }
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["source_control_ready"])
        self.assertIn("source-control evidence lacks a valid 40-character commit", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_cutover_preservation(self) -> None:
        report = sample_report(cutover_preservation={"available": False, "reason": "no cutover plan"})

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertIn("cutover preservation plan is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_live_gpu_evidence(self) -> None:
        report = sample_report(live_evidence={"gpu_acceptance": {"available": False, "reason": "missing"}})

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("RTX 3060 GPU acceptance evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_operator_preflight_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["operator_preflight"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["operator_preflight_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("operator live-acceptance preflight evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_failed_operator_preflight_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["operator_preflight"] = {
            **live_evidence["operator_preflight"],
            "status": "fail",
            "missing_checks": ["api_keys"],
            "failed_checks": ["api_keys"],
            "missing_preflight_evidence": ["summary.fail", "failed_check.api_keys"],
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["operator_preflight_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("operator live-acceptance preflight evidence status is fail", report["acceptance_blockers"])
        self.assertIn(
            "operator live-acceptance preflight evidence is missing required checks: api_keys",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_when_operator_preflight_lacks_production_topology(self) -> None:
        old_required_checks = [
            name for name in acceptance.PREFLIGHT_REQUIRED_CHECKS if name != "production_topology"
        ]
        old_checks = [
            {"name": name, "status": "ok", "detail": "validated"}
            for name in old_required_checks
        ]
        snapshot = acceptance.preflight_evidence_snapshot(
            {
                "format": "b1-ai-hub-operator-live-acceptance-preflight/v1",
                "generated_at": "2026-07-24T12:15:00+00:00",
                "status": "ok",
                "summary": {"ok": len(old_checks), "warning": 0, "fail": 0},
                "checks": old_checks,
            }
        )
        live_evidence = sample_live_evidence(operator_preflight=snapshot)
        report = sample_report(live_evidence=live_evidence)
        summary = acceptance.public_report_summary(report)

        self.assertEqual(snapshot["missing_checks"], ["production_topology"])
        self.assertFalse(summary["operator_preflight_evidence_ready"])
        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn(
            "operator live-acceptance preflight evidence is missing required checks: production_topology",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_when_operator_preflight_lacks_backup_artifact_inputs(self) -> None:
        old_required_checks = [
            name for name in acceptance.PREFLIGHT_REQUIRED_CHECKS if name != "backup_migration_rollback_inputs"
        ]
        old_checks = [
            {"name": name, "status": "ok", "detail": "validated"}
            for name in old_required_checks
        ]
        snapshot = acceptance.preflight_evidence_snapshot(
            {
                "format": "b1-ai-hub-operator-live-acceptance-preflight/v1",
                "generated_at": "2026-07-24T12:15:00+00:00",
                "status": "ok",
                "summary": {"ok": len(old_checks), "warning": 0, "fail": 0},
                "checks": old_checks,
            }
        )
        live_evidence = sample_live_evidence(operator_preflight=snapshot)
        report = sample_report(live_evidence=live_evidence)
        summary = acceptance.public_report_summary(report)

        self.assertEqual(snapshot["missing_checks"], ["backup_migration_rollback_inputs"])
        self.assertFalse(summary["operator_preflight_evidence_ready"])
        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn(
            "operator live-acceptance preflight evidence is missing required checks: backup_migration_rollback_inputs",
            report["acceptance_blockers"],
        )

    def test_operator_preflight_snapshot_accepts_warnings_without_blocking_handoff(self) -> None:
        checks = [
            {"name": name, "status": "ok", "detail": "validated"}
            for name in acceptance.PREFLIGHT_REQUIRED_CHECKS
        ]
        checks[-1] = {"name": checks[-1]["name"], "status": "warning", "detail": "operator reviewed limitation"}
        snapshot = acceptance.preflight_evidence_snapshot(
            {
                "format": "b1-ai-hub-operator-live-acceptance-preflight/v1",
                "generated_at": "2026-07-24T12:15:00+00:00",
                "status": "warning",
                "summary": {"ok": len(checks) - 1, "warning": 1, "fail": 0},
                "checks": checks,
            }
        )
        live_evidence = sample_live_evidence(operator_preflight=snapshot)
        report = sample_report(live_evidence=live_evidence)
        summary = acceptance.public_report_summary(report)

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["missing_checks"], [])
        self.assertEqual(snapshot["preflight_warning_count"], 1)
        self.assertEqual(snapshot["missing_preflight_evidence"], [])
        self.assertTrue(summary["operator_preflight_evidence_ready"])
        self.assertTrue(summary["live_evidence_ready"])
        self.assertTrue(report["operator_handoff_ready"])

    def test_report_blocks_handoff_without_smoke_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["live_stack_smoke"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["smoke_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("live stack smoke evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_smoke_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["live_stack_smoke"] = {
            **live_evidence["live_stack_smoke"],
            "status": "incomplete",
            "missing_checks": ["artifact_downloaded", "artifact_metadata_verified"],
            "checks": {
                "healthz_ok": {"status": "ok", "recorded_at": "2026-07-24T12:20:00+00:00"},
                "models_listed": {"status": "ok", "recorded_at": "2026-07-24T12:21:00+00:00"},
                "tts_media_job_completed": {"status": "ok", "recorded_at": "2026-07-24T12:22:00+00:00"},
                "tts_media_job_resolved_model_recorded": {"status": "ok", "recorded_at": "2026-07-24T12:22:05+00:00"},
                "tts_media_job_not_placeholder": {"status": "ok", "recorded_at": "2026-07-24T12:22:06+00:00"},
                "job_events_streamed": {"status": "ok", "recorded_at": "2026-07-24T12:23:00+00:00"},
                "job_events_terminal_state_observed": {"status": "ok", "recorded_at": "2026-07-24T12:23:05+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["smoke_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("live stack smoke evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "live stack smoke evidence is missing required checks: artifact_downloaded, artifact_metadata_verified",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_when_smoke_summary_is_absent(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["live_stack_smoke"].pop("missing_smoke_evidence", None)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["smoke_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("live stack smoke evidence lacks detailed smoke summary", report["acceptance_blockers"])

    def test_smoke_snapshot_requires_resolution_terminal_event_and_artifact_metadata(self) -> None:
        snapshot = acceptance.smoke_evidence_snapshot(
            {
                "format": "b1-ai-hub-live-smoke/v1",
                "generated_at": "2026-07-24T12:25:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": {
                    "healthz_ok": {"status": "ok"},
                    "models_listed": {"status": "ok"},
                    "tts_media_job_completed": {"status": "ok"},
                    "job_events_streamed": {"status": "ok"},
                    "artifact_downloaded": {"status": "ok"},
                },
                "samples": [{"label": "tts-job"}],
            }
        )

        self.assertEqual(
            snapshot["missing_checks"],
            [
                "open_webui_health_ok",
                "tts_media_job_resolved_model_recorded",
                "tts_media_job_not_placeholder",
                "job_events_terminal_state_observed",
                "artifact_metadata_verified",
            ],
        )
        self.assertIn("samples.healthz", snapshot["missing_smoke_evidence"])
        self.assertIn("samples.open-webui-health", snapshot["missing_smoke_evidence"])
        self.assertIn("open_webui_health_ok.base_url", snapshot["missing_smoke_evidence"])
        self.assertIn("open_webui_health_ok.status_code", snapshot["missing_smoke_evidence"])
        self.assertIn("open_webui_health_ok.response_status_true", snapshot["missing_smoke_evidence"])
        self.assertIn("open_webui_health_ok.permissions_policy_media_capture", snapshot["missing_smoke_evidence"])
        self.assertIn("open_webui_health_ok.strict_transport_security", snapshot["missing_smoke_evidence"])
        self.assertIn("open_webui_health_ok.x_content_type_options", snapshot["missing_smoke_evidence"])
        self.assertIn("models_listed.model_count", snapshot["missing_smoke_evidence"])
        self.assertIn("tts_media_job_completed.job_id", snapshot["missing_smoke_evidence"])
        self.assertIn("tts_media_job_resolved_model_recorded.resolved_model_version", snapshot["missing_smoke_evidence"])
        self.assertIn("job_events_terminal_state_observed.completed_state", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_downloaded.artifact_count", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_downloaded.artifact_proofs", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_metadata_verified.artifact_count", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_metadata_verified.artifact_proofs", snapshot["missing_smoke_evidence"])

        report = sample_report(live_evidence=sample_live_evidence(live_stack_smoke=snapshot))
        summary = acceptance.public_report_summary(report)
        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["smoke_evidence_ready"])
        self.assertIn("live stack smoke evidence is missing detailed proof:", "\n".join(report["acceptance_blockers"]))

    def test_smoke_snapshot_requires_all_returned_artifacts_to_be_verified(self) -> None:
        smoke = json.loads(json.dumps(sample_live_evidence()["live_stack_smoke"]))
        smoke["checks"]["tts_media_job_not_placeholder"]["artifact_count"] = 2
        smoke["checks"]["artifact_downloaded"]["artifact_count"] = 2
        smoke["checks"]["artifact_downloaded"]["verified_artifact_count"] = 1
        smoke["checks"]["artifact_metadata_verified"]["artifact_count"] = 2
        smoke["checks"]["artifact_metadata_verified"]["verified_artifact_count"] = 1
        snapshot = acceptance.smoke_evidence_snapshot(smoke)

        self.assertIn("tts_media_job_not_placeholder.artifact_placeholders_complete", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_downloaded.verified_artifact_count", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_downloaded.artifact_proofs_complete", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_metadata_verified.verified_artifact_count", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_metadata_verified.artifact_proofs_complete", snapshot["missing_smoke_evidence"])

    def test_smoke_snapshot_requires_download_digest_for_each_artifact(self) -> None:
        smoke = json.loads(json.dumps(sample_live_evidence()["live_stack_smoke"]))
        smoke["checks"]["artifact_downloaded"]["artifact_proofs"][0].pop("download_sha256")
        smoke["checks"]["artifact_metadata_verified"]["artifact_proofs"][0].pop("download_bytes")
        snapshot = acceptance.smoke_evidence_snapshot(smoke)

        self.assertIn("artifact_downloaded.artifact_proofs.0.download_sha256", snapshot["missing_smoke_evidence"])
        self.assertIn("artifact_metadata_verified.artifact_proofs.0.download_bytes", snapshot["missing_smoke_evidence"])

    def test_smoke_snapshot_rejects_placeholder_markers_on_any_tts_artifact(self) -> None:
        smoke = json.loads(json.dumps(sample_live_evidence()["live_stack_smoke"]))
        smoke["checks"]["tts_media_job_not_placeholder"]["artifact_count"] = 2
        smoke["checks"]["tts_media_job_not_placeholder"]["artifact_placeholders"].append(
            {
                "artifact_index": 1,
                "placeholder": True,
                "cpu_audio_engine": "scaffold",
                "placeholder_failure": True,
                "reasons": ["explicit_placeholder_marker", "scaffold_cpu_audio_engine"],
            }
        )
        smoke["checks"]["tts_media_job_not_placeholder"]["placeholder_failure_count"] = 1
        snapshot = acceptance.smoke_evidence_snapshot(smoke)

        self.assertIn("tts_media_job_not_placeholder.placeholder_failure_count", snapshot["missing_smoke_evidence"])
        self.assertIn(
            "tts_media_job_not_placeholder.artifact_placeholders.1.non_placeholder_proof",
            snapshot["missing_smoke_evidence"],
        )
        self.assertIn(
            "tts_media_job_not_placeholder.artifact_placeholders.1.cpu_audio_engine_not_scaffold",
            snapshot["missing_smoke_evidence"],
        )

    def test_report_blocks_handoff_for_incomplete_live_gpu_evidence(self) -> None:
        report = sample_report(
            live_evidence=sample_live_evidence(
                gpu_acceptance={
                    **sample_live_evidence()["gpu_acceptance"],
                    "status": "incomplete",
                    "missing_checks": ["voicebox_switch_completed", "bounded_runtime_recovery_action"],
                    "checks": {
                        "resource_policy_and_runtime_readiness": {
                            "status": "ok",
                            "recorded_at": "2026-07-24T12:29:00+00:00",
                        },
                        "localai_exclusive_gpu_residency": {"status": "ok"},
                        "comfyui_switch_completed": {"status": "ok"},
                        "vram_reserve_enforced": {"status": "ok"},
                    },
                }
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("RTX 3060 GPU acceptance evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "RTX 3060 GPU acceptance evidence is missing required checks: voicebox_switch_completed, bounded_runtime_recovery_action",
            report["acceptance_blockers"],
        )

    def test_gpu_snapshot_requires_combined_switch_sequence_evidence(self) -> None:
        snapshot = acceptance.gpu_acceptance_evidence_snapshot(
            {
                "format": "b1-ai-hub-cross-runtime-gpu-acceptance/v1",
                "generated_at": "2026-07-24T12:30:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": {
                    "resource_policy_and_runtime_readiness": {"status": "ok"},
                    "localai_exclusive_gpu_residency": {"status": "ok"},
                    "comfyui_switch_completed": {"status": "ok"},
                    "voicebox_switch_completed": {"status": "ok"},
                    "vram_reserve_enforced": {"status": "ok"},
                    "bounded_runtime_recovery_action": {"status": "ok"},
                },
                "required_model_aliases": [],
                "model_measurements": {},
            }
        )

        self.assertEqual(snapshot["missing_checks"], ["localai_comfyui_voicebox_switch"])
        self.assertIn("resource_policy_and_runtime_readiness.runtime_deployment_mode", snapshot["missing_gpu_evidence"])
        self.assertIn("localai_exclusive_gpu_residency.chat_resolved_model_version", snapshot["missing_gpu_evidence"])
        self.assertIn("comfyui_switch_completed.comfyui_job_id", snapshot["missing_gpu_evidence"])
        self.assertIn("comfyui_switch_completed.comfyui_native_prompt_id", snapshot["missing_gpu_evidence"])
        self.assertIn("comfyui_switch_completed.comfyui_prompt", snapshot["missing_gpu_evidence"])
        self.assertIn("comfyui_switch_completed.comfyui_artifacts.artifact_count", snapshot["missing_gpu_evidence"])
        self.assertIn("voicebox_switch_completed.voicebox_job_id", snapshot["missing_gpu_evidence"])
        self.assertIn("localai_comfyui_voicebox_switch.runtime_order", snapshot["missing_gpu_evidence"])
        self.assertIn("localai_comfyui_voicebox_switch.comfyui_native_prompt_id", snapshot["missing_gpu_evidence"])
        self.assertIn("vram_reserve_enforced.latest_sample.gpu_memory_total_mib", snapshot["missing_gpu_evidence"])
        self.assertIn("bounded_runtime_recovery_action.result_status", snapshot["missing_gpu_evidence"])

        report = sample_report(live_evidence=sample_live_evidence(gpu_acceptance=snapshot))
        summary = acceptance.public_report_summary(report)
        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["gpu_evidence_ready"])
        self.assertIn("RTX 3060 GPU acceptance evidence is missing detailed proof:", "\n".join(report["acceptance_blockers"]))

    def test_report_blocks_handoff_for_missing_gpu_model_measurements(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["gpu_acceptance"] = {
            **live_evidence["gpu_acceptance"],
            "required_model_aliases": ["chat-default", "image-default", "tts-quality"],
            "model_measurements": {
                "chat-default": sample_model_measurement("chat-default", "localai", model_id="b1-chat-default"),
                "image-default": sample_model_measurement("image-default", "comfyui", model_id="b1-image-default"),
            },
            "missing_model_measurements": ["tts-quality"],
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["gpu_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn(
            "RTX 3060 GPU acceptance evidence is missing measured model runs for aliases: tts-quality",
            report["acceptance_blockers"],
        )

    def test_gpu_snapshot_rejects_model_smoke_runtime_mismatch(self) -> None:
        gpu = json.loads(json.dumps(sample_live_evidence()["gpu_acceptance"]))
        gpu["model_measurements"]["image-default"]["latest_ok_run"]["runtime"] = "localai"
        snapshot = acceptance.gpu_acceptance_evidence_snapshot(gpu)

        self.assertIn("image-default", snapshot["missing_model_measurements"])
        self.assertIn(
            "latest run runtime does not match measurement runtime",
            snapshot["model_measurement_blockers"]["image-default"],
        )
        report = sample_report(live_evidence=sample_live_evidence(gpu_acceptance=snapshot))
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["gpu_evidence_ready"])
        self.assertIn(
            "RTX 3060 GPU acceptance evidence has incomplete model-smoke proof:",
            "\n".join(report["acceptance_blockers"]),
        )

    def test_gpu_snapshot_rejects_route_level_comfyui_prompt_for_handoff(self) -> None:
        gpu = json.loads(json.dumps(sample_live_evidence()["gpu_acceptance"]))
        gpu["checks"]["comfyui_switch_completed"]["comfyui_prompt"] = {
            "source": "env-file",
            "file_name": "native-comfyui-smoke-prompt.json",
            "node_count": 2,
            "class_type_count": 2,
            "class_types": ["B1RuntimeTinyImage", "SaveImage"],
            "route_level_smoke": True,
        }
        gpu["checks"]["localai_comfyui_voicebox_switch"]["comfyui_prompt"] = gpu["checks"]["comfyui_switch_completed"]["comfyui_prompt"]
        snapshot = acceptance.gpu_acceptance_evidence_snapshot(gpu)

        self.assertIn(
            "comfyui_switch_completed.comfyui_prompt.route_level_smoke_not_handoff",
            snapshot["missing_gpu_evidence"],
        )
        self.assertIn(
            "localai_comfyui_voicebox_switch.comfyui_prompt.route_level_smoke_not_handoff",
            snapshot["missing_gpu_evidence"],
        )
        report = sample_report(live_evidence=sample_live_evidence(gpu_acceptance=snapshot))
        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("RTX 3060 GPU acceptance evidence is missing detailed proof:", "\n".join(report["acceptance_blockers"]))

    def test_gpu_snapshot_requires_native_prompt_and_verified_comfyui_artifacts(self) -> None:
        gpu = json.loads(json.dumps(sample_live_evidence()["gpu_acceptance"]))
        comfy = gpu["checks"]["comfyui_switch_completed"]
        comfy.pop("comfyui_native_prompt_id")
        comfy["comfyui_artifacts"]["artifact_count"] = 2
        comfy["comfyui_artifacts"]["verified_artifact_count"] = 1
        comfy["comfyui_artifacts"]["artifact_proofs"][0].pop("download_sha256")
        gpu["checks"]["localai_comfyui_voicebox_switch"].pop("comfyui_native_prompt_id")
        gpu["checks"]["localai_comfyui_voicebox_switch"]["comfyui_artifact_count"] = 0

        snapshot = acceptance.gpu_acceptance_evidence_snapshot(gpu)

        self.assertIn("comfyui_switch_completed.comfyui_native_prompt_id", snapshot["missing_gpu_evidence"])
        self.assertIn("comfyui_switch_completed.comfyui_artifacts.verified_artifact_count", snapshot["missing_gpu_evidence"])
        self.assertIn("comfyui_switch_completed.comfyui_artifacts.artifact_proofs_complete", snapshot["missing_gpu_evidence"])
        self.assertIn(
            "comfyui_switch_completed.comfyui_artifacts.artifact_proofs.0.download_sha256",
            snapshot["missing_gpu_evidence"],
        )
        self.assertIn("localai_comfyui_voicebox_switch.comfyui_native_prompt_id", snapshot["missing_gpu_evidence"])
        self.assertIn("localai_comfyui_voicebox_switch.comfyui_artifact_count", snapshot["missing_gpu_evidence"])

    def test_report_blocks_handoff_when_gpu_summary_is_absent(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["gpu_acceptance"].pop("missing_gpu_evidence", None)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["gpu_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("RTX 3060 GPU acceptance evidence lacks detailed GPU summary", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_stale_live_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["gpu_acceptance"] = {
            **live_evidence["gpu_acceptance"],
            "generated_at": "2026-07-20T00:00:00+00:00",
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["gpu_evidence_ready"])
        self.assertFalse(summary["live_evidence_freshness_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn(
            "RTX 3060 GPU acceptance evidence is stale (109h old; rerun within 72h of handoff report)",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_for_invalid_or_future_live_evidence_time(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["localai_runtime"] = {
            **live_evidence["localai_runtime"],
            "generated_at": "",
        }
        live_evidence["security_acceptance"] = {
            **live_evidence["security_acceptance"],
            "generated_at": "2026-07-24T14:00:00+00:00",
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["localai_evidence_ready"])
        self.assertFalse(summary["security_evidence_ready"])
        self.assertFalse(summary["live_evidence_freshness_ready"])
        self.assertIn("LocalAI runtime acceptance evidence generated_at is missing or invalid", report["acceptance_blockers"])
        self.assertIn("security acceptance evidence generated_at is after the handoff report time", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_live_evidence_source_mismatch(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["live_stack_smoke"] = {
            **live_evidence["live_stack_smoke"],
            "source_commit": "d" * 40,
            "source_dirty": False,
            "dirty_path_count": 0,
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["smoke_evidence_ready"])
        self.assertFalse(summary["live_evidence_source_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn(
            "live stack smoke evidence source provenance is not acceptable: source_commit does not match report source-control commit",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_for_dirty_live_evidence_source(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["gpu_acceptance"] = {
            **live_evidence["gpu_acceptance"],
            "source_commit": "c" * 40,
            "source_dirty": True,
            "dirty_path_count": 3,
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["gpu_evidence_ready"])
        self.assertFalse(summary["live_evidence_source_ready"])
        self.assertIn(
            "RTX 3060 GPU acceptance evidence source provenance is not acceptable: source_dirty is true; dirty_path_count is 3",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_without_localai_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["localai_runtime"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["localai_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("LocalAI runtime acceptance evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_localai_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["localai_runtime"] = {
            **live_evidence["localai_runtime"],
            "status": "incomplete",
            "missing_checks": ["graceful_unload_verified"],
            "checks": {
                "streaming_chat_completed": {"status": "ok", "recorded_at": "2026-07-24T12:30:10+00:00"},
                "single_backend_enforced": {"status": "ok", "recorded_at": "2026-07-24T12:30:20+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["localai_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("LocalAI runtime acceptance evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "LocalAI runtime acceptance evidence is missing required checks: graceful_unload_verified",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_for_missing_localai_model_measurement(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["localai_runtime"] = {
            **live_evidence["localai_runtime"],
            "required_model_aliases": ["chat-default"],
            "model_measurements": {},
            "missing_model_measurements": ["chat-default"],
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["localai_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn(
            "LocalAI runtime acceptance evidence is missing measured model runs for aliases: chat-default",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_when_localai_summary_is_absent(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["localai_runtime"].pop("missing_localai_evidence", None)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["localai_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("LocalAI runtime acceptance evidence lacks detailed LocalAI summary", report["acceptance_blockers"])

    def test_localai_snapshot_requires_stream_backend_and_unload_details(self) -> None:
        snapshot = acceptance.localai_evidence_snapshot(
            {
                "format": "b1-ai-hub-localai-runtime-acceptance/v1",
                "generated_at": "2026-07-24T12:31:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": ok_checks(acceptance.LOCALAI_REQUIRED_CHECKS),
                "required_model_aliases": [],
                "model_measurements": {},
                "samples": [{"label": "localai-stream-chat"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], [])
        self.assertIn("streaming_chat_completed.resolved_model_version", snapshot["missing_localai_evidence"])
        self.assertIn("streaming_chat_completed.event_count", snapshot["missing_localai_evidence"])
        self.assertIn("single_backend_enforced.active_gpu_runtimes", snapshot["missing_localai_evidence"])
        self.assertIn("graceful_unload_verified.runtime_agent_status", snapshot["missing_localai_evidence"])

        report = sample_report(live_evidence=sample_live_evidence(localai_runtime=snapshot))
        summary = acceptance.public_report_summary(report)
        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["localai_evidence_ready"])
        self.assertIn("LocalAI runtime acceptance evidence is missing detailed proof:", "\n".join(report["acceptance_blockers"]))

    def test_report_blocks_handoff_without_installed_workflow_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["installed_workflows"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["installed_workflows_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("installed workflow evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_installed_workflow_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["installed_workflows"] = {
            **live_evidence["installed_workflows"],
            "status": "incomplete",
            "missing_checks": ["image_edit_completed", "short_video_completed"],
            "checks": {
                "chat_completed": {"status": "ok", "recorded_at": "2026-07-24T12:26:00+00:00"},
                "tts_completed": {"status": "ok", "recorded_at": "2026-07-24T12:27:00+00:00"},
                "stt_completed": {"status": "ok", "recorded_at": "2026-07-24T12:28:00+00:00"},
                "image_generation_completed": {"status": "ok", "recorded_at": "2026-07-24T12:29:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["installed_workflows_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("installed workflow evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "installed workflow evidence is missing required checks: image_edit_completed, short_video_completed",
            report["acceptance_blockers"],
        )

    def test_installed_workflow_snapshot_requires_artifact_verification_evidence(self) -> None:
        snapshot = acceptance.installed_workflows_evidence_snapshot(
            {
                "format": "b1-ai-hub-installed-workflows-acceptance/v1",
                "generated_at": "2026-07-24T12:32:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": {
                    "chat_completed": {"status": "ok"},
                    "tts_completed": {"status": "ok"},
                    "stt_completed": {"status": "ok"},
                    "cpu_audio_does_not_take_gpu_lease": {"status": "ok"},
                    "image_generation_completed": {"status": "ok"},
                    "image_edit_completed": {"status": "ok"},
                    "short_video_completed": {"status": "ok"},
                },
                "required_model_aliases": [],
                "model_measurements": {},
                "samples": [{"label": "image-generation"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], ["media_artifacts_verified"])
        self.assertIn("chat_completed.resolved_model_version", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("tts_completed.sha256", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("stt_completed.placeholder_proof", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("cpu_audio_does_not_take_gpu_lease.runtime_policy", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("image_generation_completed.job_id", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("image_generation_completed.artifact_proofs", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("image_edit_completed.artifact_proofs", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("short_video_completed.artifact_proofs", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("media_artifacts_verified.artifacts.image-generation", snapshot["missing_installed_workflow_evidence"])

        report = sample_report(live_evidence=sample_live_evidence(installed_workflows=snapshot))
        summary = acceptance.public_report_summary(report)
        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["installed_workflows_evidence_ready"])
        self.assertIn("installed workflow evidence is missing detailed proof:", "\n".join(report["acceptance_blockers"]))

    def test_installed_workflow_snapshot_requires_all_returned_artifacts_to_be_verified(self) -> None:
        live_evidence = sample_live_evidence()
        workflows = live_evidence["installed_workflows"]
        first = sample_artifact_proof("/artifacts/workflows/image-0.png", 4096, "6" * 64, "image/png")
        second = sample_artifact_proof("/artifacts/workflows/image-1.png", 2048, "7" * 64, "image/png", index=1)
        incomplete = sample_artifact_collection("job_workflow_image_1", [first])
        workflows["checks"]["image_generation_completed"] = {
            **workflows["checks"]["image_generation_completed"],
            "artifact_count": 2,
            "verified_artifact_count": 1,
            "artifact_proofs": [first],
        }
        workflows["checks"]["media_artifacts_verified"] = {
            **workflows["checks"]["media_artifacts_verified"],
            "artifact_count": 4,
            "verified_artifact_count": 3,
            "artifacts": {
                **workflows["checks"]["media_artifacts_verified"]["artifacts"],
                "image-generation": incomplete,
            },
        }
        workflows["media_artifacts"] = {
            **workflows.get("media_artifacts", {}),
            "image-generation-expected-second": second,
        }
        snapshot = acceptance.installed_workflows_evidence_snapshot(workflows)

        self.assertIn("image_generation_completed.verified_artifact_count", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("image_generation_completed.artifact_proofs_complete", snapshot["missing_installed_workflow_evidence"])
        self.assertIn("media_artifacts_verified.verified_artifact_count", snapshot["missing_installed_workflow_evidence"])
        self.assertIn(
            "media_artifacts_verified.artifacts.image-generation.verified_artifact_count",
            snapshot["missing_installed_workflow_evidence"],
        )
        self.assertIn(
            "media_artifacts_verified.artifacts.image-generation.artifact_proofs_complete",
            snapshot["missing_installed_workflow_evidence"],
        )

    def test_installed_workflow_snapshot_requires_download_digest_for_each_artifact(self) -> None:
        live_evidence = sample_live_evidence()
        workflows = live_evidence["installed_workflows"]
        workflows["checks"]["image_generation_completed"]["artifact_proofs"][0].pop("download_sha256")
        workflows["checks"]["media_artifacts_verified"]["artifacts"]["image-generation"]["artifacts"][0].pop("download_bytes")
        snapshot = acceptance.installed_workflows_evidence_snapshot(workflows)

        self.assertIn(
            "image_generation_completed.artifact_proofs.0.download_sha256",
            snapshot["missing_installed_workflow_evidence"],
        )
        self.assertIn(
            "media_artifacts_verified.artifacts.image-generation.artifact_proofs.0.download_bytes",
            snapshot["missing_installed_workflow_evidence"],
        )

    def test_report_blocks_handoff_for_missing_installed_workflow_model_measurements(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["installed_workflows"] = {
            **live_evidence["installed_workflows"],
            "required_model_aliases": ["chat-default", "tts-fast", "stt-default", "image-default", "image-edit", "video-text"],
            "model_measurements": {
                key: value
                for key, value in live_evidence["installed_workflows"]["model_measurements"].items()
                if key != "video-text"
            },
            "missing_model_measurements": ["video-text"],
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["installed_workflows_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn(
            "installed workflow evidence is missing measured model runs for aliases: video-text",
            report["acceptance_blockers"],
        )

    def test_installed_workflow_snapshot_rejects_cpu_audio_vram_usage(self) -> None:
        workflows = json.loads(json.dumps(sample_live_evidence()["installed_workflows"]))
        workflows["model_measurements"]["tts-fast"]["latest_ok_run"]["peak_vram_mib"] = 64
        snapshot = acceptance.installed_workflows_evidence_snapshot(workflows)

        self.assertIn("tts-fast", snapshot["missing_model_measurements"])
        self.assertIn(
            "CPU-only measurement reported GPU VRAM usage",
            snapshot["model_measurement_blockers"]["tts-fast"],
        )
        report = sample_report(live_evidence=sample_live_evidence(installed_workflows=snapshot))
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["installed_workflows_evidence_ready"])
        self.assertIn(
            "installed workflow evidence has incomplete model-smoke proof:",
            "\n".join(report["acceptance_blockers"]),
        )

    def test_report_blocks_handoff_when_installed_workflow_summary_is_absent(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["installed_workflows"].pop("missing_installed_workflow_evidence", None)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["installed_workflows_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("installed workflow evidence lacks detailed workflow summary", report["acceptance_blockers"])

    def test_report_blocks_handoff_without_native_comfyui_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["native_comfyui_compatibility"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["native_comfyui_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("native ComfyUI compatibility evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_native_comfyui_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["native_comfyui_compatibility"] = {
            **live_evidence["native_comfyui_compatibility"],
            "status": "incomplete",
            "missing_checks": ["websocket_events", "history_available"],
            "checks": {
                "object_info_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "system_stats_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "models_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "queue_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "prompt_submission": {"status": "ok", "recorded_at": "2026-07-24T12:32:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["native_comfyui_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("native ComfyUI compatibility evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "native ComfyUI compatibility evidence is missing required checks: websocket_events, history_available",
            report["acceptance_blockers"],
        )

    def test_native_comfyui_snapshot_requires_metadata_upload_queue_and_view_evidence(self) -> None:
        snapshot = acceptance.native_comfyui_evidence_snapshot(
            {
                "format": "b1-ai-hub-native-comfyui-compatibility/v1",
                "generated_at": "2026-07-24T12:33:00+00:00",
                "base_url": "https://comfy.ai.b1.germering",
                "status": "ok",
                "checks": {
                    "object_info_accessible": {"status": "ok"},
                    "system_stats_accessible": {"status": "ok"},
                    "models_accessible": {"status": "ok"},
                    "queue_accessible": {"status": "ok"},
                    "prompt_submission": {"status": "ok"},
                    "websocket_events": {"status": "ok"},
                    "history_available": {"status": "ok"},
                },
                "samples": [{"label": "prompt-submission"}],
            }
        )

        self.assertIn("object_info_node_accessible", snapshot["required_checks"])
        self.assertIn("upload_image_accessible", snapshot["required_checks"])
        self.assertIn("upload_mask_accessible", snapshot["required_checks"])
        self.assertIn("history_listing_accessible", snapshot["required_checks"])
        self.assertIn("queue_delete_accessible", snapshot["required_checks"])
        self.assertIn("interrupt_accessible", snapshot["required_checks"])
        self.assertIn("view_artifact_accessible", snapshot["required_checks"])
        self.assertIn("prompt_idempotency_replay", snapshot["required_checks"])
        self.assertIn("durable_job_observable", snapshot["required_checks"])
        self.assertIn("native_summary_observable", snapshot["required_checks"])
        self.assertIn("durable_artifacts_observable", snapshot["required_checks"])
        self.assertEqual(
            snapshot["missing_checks"],
            [
                "object_info_node_accessible",
                "upload_image_accessible",
                "upload_mask_accessible",
                "prompt_idempotency_replay",
                "history_listing_accessible",
                "durable_job_observable",
                "native_summary_observable",
                "durable_artifacts_observable",
                "queue_delete_accessible",
                "interrupt_accessible",
                "view_artifact_accessible",
            ],
        )

    def test_native_comfyui_snapshot_requires_prompt_job_and_artifact_details(self) -> None:
        snapshot = acceptance.native_comfyui_evidence_snapshot(
            {
                "format": "b1-ai-hub-native-comfyui-compatibility/v1",
                "generated_at": "2026-07-24T12:33:00+00:00",
                "base_url": "https://comfy.ai.b1.germering",
                "status": "ok",
                "checks": ok_checks(acceptance.NATIVE_COMFYUI_REQUIRED_CHECKS),
                "samples": [{"label": "prompt-submission"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], [])
        self.assertIn("prompt_submission.prompt_id", snapshot["missing_compatibility_evidence"])
        self.assertIn("prompt_idempotency_replay.replay_header", snapshot["missing_compatibility_evidence"])
        self.assertIn("websocket_events.completed", snapshot["missing_compatibility_evidence"])
        self.assertIn("durable_job_observable.job_id", snapshot["missing_compatibility_evidence"])
        self.assertIn("native_summary_observable.job_id", snapshot["missing_compatibility_evidence"])
        self.assertIn("native_summary_observable.node_count", snapshot["missing_compatibility_evidence"])
        self.assertIn("native_summary_observable.body_hash_present", snapshot["missing_compatibility_evidence"])
        self.assertIn("durable_artifacts_observable.artifact_proofs", snapshot["missing_compatibility_evidence"])
        self.assertIn("queue_delete_accessible.http_status", snapshot["missing_compatibility_evidence"])
        self.assertIn("interrupt_accessible.http_status", snapshot["missing_compatibility_evidence"])
        self.assertIn("view_artifact_accessible.artifacts", snapshot["missing_compatibility_evidence"])
        self.assertIn("prompt.metadata", snapshot["missing_compatibility_evidence"])

    def test_native_comfyui_snapshot_rejects_route_level_smoke_prompt_for_handoff(self) -> None:
        native = json.loads(json.dumps(sample_live_evidence()["native_comfyui_compatibility"]))
        native["prompt"] = {
            "source": "default-smoke-file",
            "file_path": "/repo/workflows/acceptance/native-comfyui-smoke-prompt.json",
            "file_name": "native-comfyui-smoke-prompt.json",
            "node_count": 2,
            "class_type_count": 2,
            "class_types": ["B1RuntimeTinyImage", "SaveImage"],
            "route_level_smoke": True,
            "default_prompt_file": True,
        }
        snapshot = acceptance.native_comfyui_evidence_snapshot(native)

        self.assertIn("prompt.route_level_smoke_not_handoff", snapshot["missing_compatibility_evidence"])
        self.assertTrue(snapshot["prompt_route_level_smoke"])

    def test_report_blocks_handoff_for_route_level_native_comfyui_smoke_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        native = json.loads(json.dumps(live_evidence["native_comfyui_compatibility"]))
        native["prompt"]["source"] = "default-smoke-file"
        native["prompt"]["file_name"] = "native-comfyui-smoke-prompt.json"
        native["prompt"]["route_level_smoke"] = True
        native["prompt"]["default_prompt_file"] = True
        live_evidence["native_comfyui_compatibility"] = acceptance.native_comfyui_evidence_snapshot(native)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn(
            "native ComfyUI compatibility evidence is missing detailed proof: prompt.route_level_smoke_not_handoff",
            report["acceptance_blockers"],
        )

    def test_native_comfyui_snapshot_requires_all_returned_artifacts_to_be_verified(self) -> None:
        native = json.loads(json.dumps(sample_live_evidence()["native_comfyui_compatibility"]))
        native["checks"]["native_summary_observable"]["stored_artifact_count"] = 2
        native["checks"]["durable_artifacts_observable"]["artifact_count"] = 2
        native["checks"]["durable_artifacts_observable"]["verified_artifact_count"] = 1
        native["checks"]["view_artifact_accessible"]["view_count"] = 2
        native["checks"]["view_artifact_accessible"]["verified_view_count"] = 1
        snapshot = acceptance.native_comfyui_evidence_snapshot(native)

        self.assertIn("durable_artifacts_observable.verified_artifact_count", snapshot["missing_compatibility_evidence"])
        self.assertIn("durable_artifacts_observable.artifact_proofs_complete", snapshot["missing_compatibility_evidence"])
        self.assertIn("view_artifact_accessible.verified_view_count", snapshot["missing_compatibility_evidence"])
        self.assertIn("view_artifact_accessible.artifacts_complete", snapshot["missing_compatibility_evidence"])

    def test_native_comfyui_snapshot_requires_download_digest_for_each_artifact(self) -> None:
        native = json.loads(json.dumps(sample_live_evidence()["native_comfyui_compatibility"]))
        native["checks"]["durable_artifacts_observable"]["artifact_proofs"][0].pop("download_sha256")
        native["checks"]["view_artifact_accessible"]["artifacts"][0].pop("download_sha256")
        snapshot = acceptance.native_comfyui_evidence_snapshot(native)

        self.assertIn(
            "durable_artifacts_observable.artifact_proofs.0.download_sha256",
            snapshot["missing_compatibility_evidence"],
        )
        self.assertIn("view_artifact_accessible.artifacts.0.download_sha256", snapshot["missing_compatibility_evidence"])

    def test_native_comfyui_snapshot_accepts_empty_queue_and_interrupt_bodies(self) -> None:
        prompt_id = "prompt_native_empty_body"
        job_id = "job_native_empty_body"
        artifact_sha = "9" * 64
        durable_artifacts = sample_artifact_collection(
            job_id,
            [sample_artifact_proof("/artifacts/native/empty-body.png", 4096, artifact_sha, "image/png")],
        )
        view_artifacts = [sample_view_artifact_proof("native-output.png", 4096, artifact_sha, "image/png")]
        checks = ok_checks(acceptance.NATIVE_COMFYUI_REQUIRED_CHECKS)
        checks.update(
            {
                "prompt_submission": {"status": "ok", "prompt_id": prompt_id, "queue_number": 1},
                "prompt_idempotency_replay": {
                    "status": "ok",
                    "prompt_id": prompt_id,
                    "replay_header": "true",
                    "idempotency_key_length": 48,
                },
                "websocket_events": {
                    "status": "ok",
                    "prompt_id": prompt_id,
                    "event_types": ["execution_start", "executing"],
                    "binary_messages": 0,
                    "completed": True,
                },
                "history_available": {"status": "ok", "prompt_id": prompt_id, "history_keys": [prompt_id]},
                "durable_job_observable": {
                    "status": "ok",
                    "prompt_id": prompt_id,
                    "job_id": job_id,
                    "state": "completed",
                    "artifact_count": 1,
                },
                "native_summary_observable": {
                    "status": "ok",
                    "prompt_id": prompt_id,
                    "job_id": job_id,
                    "node_count": 1,
                    "class_type_count": 1,
                    "stored_artifact_count": 1,
                    "failed_ingest_count": 0,
                    "body_hash_present": True,
                    "client_id_present": True,
                },
                "durable_artifacts_observable": {
                    "status": "ok",
                    "prompt_id": prompt_id,
                    **durable_artifacts,
                    "byte_count": 4096,
                    "content_type": "image/png",
                },
                "queue_delete_accessible": {"status": "ok", "prompt_id": prompt_id, "http_status": 204, "byte_count": 0},
                "interrupt_accessible": {"status": "ok", "prompt_id": prompt_id, "http_status": 200, "byte_count": 0},
                "view_artifact_accessible": {
                    "status": "ok",
                    "prompt_id": prompt_id,
                    "view_count": 1,
                    "verified_view_count": 1,
                    "total_byte_count": 4096,
                    "artifacts": view_artifacts,
                    "output_key": "images",
                    "filename": "native-output.png",
                    "byte_count": 4096,
                    "content_type": "image/png",
                    "download_sha256": artifact_sha,
                },
            }
        )

        snapshot = acceptance.native_comfyui_evidence_snapshot(
            {
                "format": "b1-ai-hub-native-comfyui-compatibility/v1",
                "generated_at": "2026-07-24T12:33:00+00:00",
                "base_url": "https://comfy.ai.b1.germering",
                "status": "ok",
                "prompt": {
                    "source": "env-file",
                    "file_name": "text-to-image-api-prompt.json",
                    "node_count": 1,
                    "class_type_count": 1,
                    "class_types": ["SaveImage"],
                    "route_level_smoke": False,
                    "default_prompt_file": False,
                },
                "checks": checks,
                "samples": [{"label": "prompt-submission"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], [])
        self.assertEqual(snapshot["missing_compatibility_evidence"], [])
        self.assertEqual(snapshot["native_summary_node_count"], 1)
        self.assertEqual(snapshot["native_summary_stored_artifact_count"], 1)

    def test_absent_legacy_comfyui_evidence_is_non_blocking(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["legacy_comfyui_listener"] = {"available": False, "reason": "optional listener not enabled"}
        report = sample_report(live_evidence=live_evidence)

        summary = acceptance.public_report_summary(report)
        self.assertTrue(report["operator_handoff_ready"])
        self.assertTrue(summary["legacy_comfyui_evidence_ready"])
        self.assertTrue(summary["live_evidence_ready"])
        self.assertFalse(any("legacy ComfyUI listener" in blocker for blocker in report["acceptance_blockers"]))

    def test_report_blocks_handoff_for_incomplete_legacy_comfyui_evidence_when_present(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["legacy_comfyui_listener"] = {
            "available": True,
            "format": "b1-ai-hub-legacy-comfyui-listener/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/legacy-comfy-listener.json",
            "generated_at": "2026-07-24T12:34:00+00:00",
            "base_url": "http://ai.b1.germering:8188",
            "status": "incomplete",
            "required_checks": ["object_info_without_auth", "system_stats_without_auth", "websocket_without_auth"],
            "missing_checks": ["websocket_without_auth"],
            "checks": {
                "object_info_without_auth": {"status": "ok", "recorded_at": "2026-07-24T12:34:00+00:00"},
                "system_stats_without_auth": {"status": "ok", "recorded_at": "2026-07-24T12:34:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["legacy_comfyui_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("legacy ComfyUI listener evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "legacy ComfyUI listener evidence is missing required checks: websocket_without_auth",
            report["acceptance_blockers"],
        )

    def test_legacy_comfyui_snapshot_requires_all_legacy_listener_checks(self) -> None:
        snapshot = acceptance.legacy_comfyui_evidence_snapshot(
            {
                "format": "b1-ai-hub-legacy-comfyui-listener/v1",
                "generated_at": "2026-07-24T12:34:00+00:00",
                "base_url": "http://ai.b1.germering:8188",
                "status": "ok",
                "checks": {
                    "object_info_without_auth": {"status": "ok"},
                    "system_stats_without_auth": {"status": "ok"},
                },
                "samples": [{"label": "object-info"}],
            }
        )

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["required_checks"], list(acceptance.LEGACY_COMFYUI_REQUIRED_CHECKS))
        self.assertEqual(snapshot["missing_checks"], ["websocket_without_auth"])
        self.assertIn("listener_policy", snapshot["missing_legacy_evidence"])
        self.assertIn("samples.system_stats", snapshot["missing_legacy_evidence"])
        self.assertEqual(snapshot["sample_count"], 1)

    def test_legacy_comfyui_snapshot_accepts_scheduler_aware_listener_evidence(self) -> None:
        snapshot = acceptance.legacy_comfyui_evidence_snapshot(sample_legacy_comfy_payload())

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["missing_checks"], [])
        self.assertEqual(snapshot["missing_legacy_evidence"], [])
        self.assertEqual(snapshot["legacy_listener_gateway_target"], "control-plane:8000")
        self.assertEqual(snapshot["legacy_listener_allow_cidrs"], ["192.168.2.0/24", "100.64.0.0/10"])
        self.assertEqual(snapshot["legacy_websocket_client_id"], "b1-legacy-comfyui-test")

    def test_report_blocks_handoff_for_shallow_legacy_comfyui_evidence_when_present(self) -> None:
        live_evidence = sample_live_evidence()
        legacy_payload = sample_legacy_comfy_payload()
        legacy_payload.pop("listener_policy")
        for check in legacy_payload["checks"].values():
            check.pop("authorization_header_sent", None)
            check.pop("cookie_header_sent", None)
            check.pop("csrf_header_sent", None)
        live_evidence["legacy_comfyui_listener"] = acceptance.legacy_comfyui_evidence_snapshot(legacy_payload)
        report = sample_report(live_evidence=live_evidence)
        summary = acceptance.public_report_summary(report)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["legacy_comfyui_evidence_ready"])
        self.assertIn(
            "legacy ComfyUI listener evidence is missing detailed proof: ",
            " ".join(report["acceptance_blockers"]),
        )
        self.assertIn("listener_policy", live_evidence["legacy_comfyui_listener"]["missing_legacy_evidence"])

    def test_report_blocks_handoff_for_open_world_legacy_comfyui_allowlist(self) -> None:
        live_evidence = sample_live_evidence()
        legacy_payload = sample_legacy_comfy_payload()
        legacy_payload["listener_policy"]["allow_cidrs"] = ["0.0.0.0/0"]
        live_evidence["legacy_comfyui_listener"] = acceptance.legacy_comfyui_evidence_snapshot(legacy_payload)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn(
            "listener_policy.allow_cidrs_not_open_world",
            live_evidence["legacy_comfyui_listener"]["missing_legacy_evidence"],
        )

    def test_report_blocks_handoff_for_static_host_legacy_comfyui_publish_mapping(self) -> None:
        live_evidence = sample_live_evidence()
        legacy_payload = sample_legacy_comfy_payload()
        legacy_payload["listener_policy"]["publish_mapping"] = "192.168.2.100:8188:8188"
        live_evidence["legacy_comfyui_listener"] = acceptance.legacy_comfyui_evidence_snapshot(legacy_payload)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn(
            "listener_policy.publish_mapping_uses_static_host_ip",
            live_evidence["legacy_comfyui_listener"]["missing_legacy_evidence"],
        )


    def test_report_blocks_handoff_without_remote_node_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["remote_nodes_non_comfy"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["remote_nodes_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("remote-node non-Comfy compatibility evidence is unavailable", report["acceptance_blockers"])

    def test_remote_node_snapshot_requires_model_listing_and_credential_evidence(self) -> None:
        snapshot = acceptance.remote_nodes_evidence_snapshot(
            {
                "format": "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1",
                "generated_at": "2026-07-24T12:35:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": {
                    "server_side_comfyui_stopped": {"status": "ok"},
                    "server_side_comfyui_stop_verified": {"status": "ok"},
                    "node_surface_registered": sample_remote_node_surface_check(),
                    "non_comfy_tts_completed": {"status": "ok"},
                    "artifact_downloaded": {"status": "ok"},
                    "server_side_comfyui_still_stopped_after_operation": {"status": "ok"},
                },
                "samples": [{"label": "tts-fast-non-comfy"}],
            }
        )

        self.assertEqual(
            snapshot["missing_checks"],
            ["remote_models_listed", "model_alias_selected", "credentials_externalized"],
        )

    def test_remote_node_snapshot_requires_comfyui_stop_verification(self) -> None:
        snapshot = acceptance.remote_nodes_evidence_snapshot(
            {
                "format": "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1",
                "generated_at": "2026-07-24T12:35:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": {
                    "server_side_comfyui_stopped": {"status": "ok"},
                    "node_surface_registered": sample_remote_node_surface_check(),
                    "remote_models_listed": {"status": "ok"},
                    "model_alias_selected": {"status": "ok"},
                    "credentials_externalized": {"status": "ok"},
                    "non_comfy_tts_completed": {"status": "ok"},
                    "artifact_downloaded": {"status": "ok"},
                },
                "samples": [{"label": "tts-fast-non-comfy"}],
            }
        )

        self.assertEqual(
            snapshot["missing_checks"],
            ["server_side_comfyui_stop_verified", "server_side_comfyui_still_stopped_after_operation"],
        )

    def test_remote_node_snapshot_requires_stopped_non_comfy_and_artifact_details(self) -> None:
        snapshot = acceptance.remote_nodes_evidence_snapshot(
            {
                "format": "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1",
                "generated_at": "2026-07-24T12:35:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": ok_checks(acceptance.REMOTE_NODES_REQUIRED_CHECKS),
                "samples": [{"label": "tts-fast-non-comfy"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], [])
        self.assertIn("server_side_comfyui_stop_verified.verified_by", snapshot["missing_compatibility_evidence"])
        self.assertIn("server_side_comfyui_stop_verified.running_container_count_zero", snapshot["missing_compatibility_evidence"])
        self.assertIn("server_side_comfyui_still_stopped_after_operation.verified_by", snapshot["missing_compatibility_evidence"])
        self.assertIn(
            "server_side_comfyui_still_stopped_after_operation.running_container_count_zero",
            snapshot["missing_compatibility_evidence"],
        )
        self.assertIn("node_surface_registered.required_node_count", snapshot["missing_compatibility_evidence"])
        self.assertIn("node_surface_registered.registered_node_count", snapshot["missing_compatibility_evidence"])
        self.assertIn(
            "node_surface_registered.registered_node_classes.B1TextToSpeech",
            snapshot["missing_compatibility_evidence"],
        )
        self.assertIn(
            "node_surface_registered.example_node_types.B1DownloadArtifact",
            snapshot["missing_compatibility_evidence"],
        )
        self.assertIn("remote_models_listed.model", snapshot["missing_compatibility_evidence"])
        self.assertIn("credentials_externalized.credential_source", snapshot["missing_compatibility_evidence"])
        self.assertIn("non_comfy_tts_completed.runtime_policy", snapshot["missing_compatibility_evidence"])
        self.assertIn("non_comfy_tts_completed.sha256", snapshot["missing_compatibility_evidence"])
        self.assertIn("non_comfy_tts_completed.placeholder_proof", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.sha256", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.file_sha256", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.filename", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.relative_path", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.path_within_download_dir", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.private_file_mode", snapshot["missing_compatibility_evidence"])

    def test_remote_node_snapshot_requires_file_backed_artifact_integrity(self) -> None:
        payload = sample_live_evidence()["remote_nodes_non_comfy"]
        payload = {
            **payload,
            "checks": {
                **payload["checks"],
                "non_comfy_tts_completed": {
                    **payload["checks"]["non_comfy_tts_completed"],
                    "byte_count": 2048,
                    "sha256": "1" * 64,
                },
                "artifact_downloaded": {
                    **payload["checks"]["artifact_downloaded"],
                    "byte_count": 2048,
                    "stat_size": 1024,
                    "sha256": "2" * 64,
                    "file_sha256": "3" * 64,
                    "path_within_download_dir": False,
                    "symlink": True,
                    "private_file_mode": False,
                },
            },
        }

        snapshot = acceptance.remote_nodes_evidence_snapshot(payload)

        self.assertIn("artifact_downloaded.file_sha256_matches_download", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.sha256_matches_tts", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.stat_size_matches_byte_count", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.path_within_download_dir", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.not_symlink", snapshot["missing_compatibility_evidence"])
        self.assertIn("artifact_downloaded.private_file_mode", snapshot["missing_compatibility_evidence"])

    def test_remote_node_snapshot_requires_comfyui_to_remain_stopped_after_operation(self) -> None:
        payload = sample_live_evidence()["remote_nodes_non_comfy"]
        payload = {
            **payload,
            "checks": {
                **payload["checks"],
                "server_side_comfyui_still_stopped_after_operation": {
                    **payload["checks"]["server_side_comfyui_still_stopped_after_operation"],
                    "running_container_count": 1,
                },
            },
        }

        snapshot = acceptance.remote_nodes_evidence_snapshot(payload)

        self.assertIn(
            "server_side_comfyui_still_stopped_after_operation.running_container_count_zero",
            snapshot["missing_compatibility_evidence"],
        )

    def test_remote_node_snapshot_rejects_placeholder_tts_proof(self) -> None:
        payload = sample_live_evidence()["remote_nodes_non_comfy"]
        payload = {
            **payload,
            "checks": {
                **payload["checks"],
                "non_comfy_tts_completed": {
                    **payload["checks"]["non_comfy_tts_completed"],
                    "placeholder_proof": {
                        "placeholder": True,
                        "cpu_audio_engine": "scaffold",
                        "placeholder_failure": True,
                        "reasons": ["explicit_placeholder_marker", "scaffold_cpu_audio_engine"],
                    },
                },
            },
        }

        snapshot = acceptance.remote_nodes_evidence_snapshot(payload)

        self.assertIn("non_comfy_tts_completed.non_placeholder_proof", snapshot["missing_compatibility_evidence"])
        self.assertIn("non_comfy_tts_completed.cpu_audio_engine_not_scaffold", snapshot["missing_compatibility_evidence"])

    def test_remote_node_snapshot_rejects_unproven_tts_marker(self) -> None:
        payload = sample_live_evidence()["remote_nodes_non_comfy"]
        payload = {
            **payload,
            "checks": {
                **payload["checks"],
                "non_comfy_tts_completed": {
                    **payload["checks"]["non_comfy_tts_completed"],
                    "placeholder_proof": {
                        "placeholder": None,
                        "cpu_audio_engine": None,
                        "placeholder_failure": False,
                        "reasons": [],
                    },
                },
            },
        }

        snapshot = acceptance.remote_nodes_evidence_snapshot(payload)

        self.assertIn("non_comfy_tts_completed.non_placeholder_proof", snapshot["missing_compatibility_evidence"])

    def test_report_blocks_handoff_for_incomplete_remote_node_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["remote_nodes_non_comfy"] = {
            **live_evidence["remote_nodes_non_comfy"],
            "status": "incomplete",
            "missing_checks": ["artifact_downloaded"],
            "checks": {
                "server_side_comfyui_stopped": {"status": "ok", "recorded_at": "2026-07-24T12:34:00+00:00"},
                "server_side_comfyui_stop_verified": {"status": "ok", "recorded_at": "2026-07-24T12:34:05+00:00"},
                "remote_models_listed": {"status": "ok", "recorded_at": "2026-07-24T12:34:15+00:00"},
                "model_alias_selected": {"status": "ok", "recorded_at": "2026-07-24T12:34:20+00:00"},
                "credentials_externalized": {"status": "ok", "recorded_at": "2026-07-24T12:34:30+00:00"},
                "non_comfy_tts_completed": {"status": "ok", "recorded_at": "2026-07-24T12:35:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["remote_nodes_evidence_ready"])
        self.assertIn("remote-node non-Comfy compatibility evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "remote-node non-Comfy compatibility evidence is missing required checks: artifact_downloaded",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_without_modelhub_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["modelhub_client_sync"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["modelhub_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("Model Hub client sync evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_modelhub_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["modelhub_client_sync"] = {
            **live_evidence["modelhub_client_sync"],
            "status": "incomplete",
            "missing_checks": ["etag_if_none_match_validated"],
            "checks": {
                "catalog_visible": {"status": "ok", "recorded_at": "2026-07-24T12:36:00+00:00"},
                "download_plan_created": {"status": "ok", "recorded_at": "2026-07-24T12:37:00+00:00"},
                "head_metadata_validated": {"status": "ok", "recorded_at": "2026-07-24T12:37:30+00:00"},
                "range_resume_downloaded": {"status": "ok", "recorded_at": "2026-07-24T12:38:00+00:00"},
                "cache_state_managed": {"status": "ok", "recorded_at": "2026-07-24T12:39:00+00:00"},
                "dry_run_prune_safe": {"status": "ok", "recorded_at": "2026-07-24T12:39:00+00:00"},
                "inference_only_download_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:40:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["modelhub_evidence_ready"])
        self.assertIn("Model Hub client sync evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "Model Hub client sync evidence is missing required checks: etag_if_none_match_validated",
            report["acceptance_blockers"],
        )

    def test_modelhub_snapshot_requires_blob_integrity_metadata(self) -> None:
        payload = {
            "format": "b1-ai-hub-modelhub-client-sync/v1",
            "generated_at": "2026-07-24T12:40:00+00:00",
            "base_url": "https://models.ai.b1.germering",
            "status": "ok",
            "checks": {
                name: {"status": "ok", "recorded_at": "2026-07-24T12:40:00+00:00"}
                for name in acceptance.MODELHUB_REQUIRED_CHECKS
            },
            "samples": [{"label": "modelhub-client-sync"}],
        }
        snapshot = acceptance.modelhub_evidence_snapshot(payload)
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["missing_checks"], [])
        self.assertIn("download_plan_created.blob", snapshot["missing_integrity_evidence"])
        self.assertIn("head_metadata_validated.etag", snapshot["missing_integrity_evidence"])
        self.assertIn("range_resume_downloaded.final_size", snapshot["missing_integrity_evidence"])
        self.assertIn("range_resume_downloaded.final_sha256", snapshot["missing_integrity_evidence"])
        self.assertIn("cache_state_managed.cached_blob_file_sha256", snapshot["missing_integrity_evidence"])
        self.assertIn("cache_state_managed.cached_blob_relative_path", snapshot["missing_integrity_evidence"])
        self.assertIn("samples.modelhub-client-sync.cached_blob_file_proof", snapshot["missing_integrity_evidence"])

        live_evidence = sample_live_evidence(modelhub_client_sync=snapshot)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["modelhub_evidence_ready"])
        self.assertIn(
            "Model Hub client sync evidence is missing integrity evidence: "
            + ", ".join(str(item) for item in snapshot["missing_integrity_evidence"]),
            report["acceptance_blockers"],
        )

    def test_modelhub_snapshot_accepts_complete_cache_file_evidence(self) -> None:
        snapshot = acceptance.modelhub_evidence_snapshot(sample_modelhub_client_sync_payload())

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["missing_checks"], [])
        self.assertEqual(snapshot["missing_integrity_evidence"], [])
        self.assertEqual(snapshot["verified_blob"], "a" * 64)
        self.assertEqual(snapshot["verified_size_bytes"], 12)

    def test_modelhub_snapshot_rejects_mismatched_cached_blob_file(self) -> None:
        payload = sample_modelhub_client_sync_payload()
        mismatched_blob = "b" * 64
        payload["checks"]["range_resume_downloaded"]["final_sha256"] = mismatched_blob
        payload["checks"]["cache_state_managed"]["cached_blob_file_sha256"] = mismatched_blob
        payload["samples"][0]["cached_blob_file_sha256"] = mismatched_blob

        snapshot = acceptance.modelhub_evidence_snapshot(payload)

        self.assertIn("range_resume_downloaded.final_sha256_matches_blob", snapshot["missing_integrity_evidence"])
        self.assertIn("cache_state_managed.cached_blob_file_sha256_matches_blob", snapshot["missing_integrity_evidence"])
        self.assertIn("samples.modelhub-client-sync.cached_blob_file_proof", snapshot["missing_integrity_evidence"])

    def test_report_blocks_handoff_when_modelhub_integrity_summary_is_absent(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["modelhub_client_sync"].pop("missing_integrity_evidence", None)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["modelhub_evidence_ready"])
        self.assertIn("Model Hub client sync evidence lacks integrity validation summary", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_external_compatibility_summary_is_absent(self) -> None:
        cases = (
            (
                "native_comfyui_compatibility",
                "native_comfyui_evidence_ready",
                "native ComfyUI compatibility evidence lacks detailed compatibility summary",
            ),
            (
                "remote_nodes_non_comfy",
                "remote_nodes_evidence_ready",
                "remote-node non-Comfy compatibility evidence lacks detailed compatibility summary",
            ),
            (
                "voicebox_remote",
                "voicebox_evidence_ready",
                "Voicebox remote compatibility evidence lacks detailed compatibility summary",
            ),
        )
        for evidence_key, summary_key, blocker in cases:
            with self.subTest(evidence_key=evidence_key):
                live_evidence = sample_live_evidence()
                live_evidence[evidence_key].pop("missing_compatibility_evidence", None)
                report = sample_report(live_evidence=live_evidence)
                summary = acceptance.public_report_summary(report)

                self.assertFalse(report["operator_handoff_ready"])
                self.assertFalse(summary[summary_key])
                self.assertIn(blocker, report["acceptance_blockers"])

    def test_report_blocks_handoff_for_shallow_external_compatibility_evidence(self) -> None:
        cases = (
            (
                "native_comfyui_compatibility",
                acceptance.native_comfyui_evidence_snapshot,
                "b1-ai-hub-native-comfyui-compatibility/v1",
                acceptance.NATIVE_COMFYUI_REQUIRED_CHECKS,
                "https://comfy.ai.b1.germering",
                "native_comfyui_evidence_ready",
                "native ComfyUI compatibility evidence is missing detailed proof:",
            ),
            (
                "remote_nodes_non_comfy",
                acceptance.remote_nodes_evidence_snapshot,
                "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1",
                acceptance.REMOTE_NODES_REQUIRED_CHECKS,
                "https://api.ai.b1.germering",
                "remote_nodes_evidence_ready",
                "remote-node non-Comfy compatibility evidence is missing detailed proof:",
            ),
            (
                "voicebox_remote",
                acceptance.voicebox_evidence_snapshot,
                "b1-ai-hub-voicebox-remote-compatibility/v1",
                acceptance.VOICEBOX_REQUIRED_CHECKS,
                "https://voice.ai.b1.germering",
                "voicebox_evidence_ready",
                "Voicebox remote compatibility evidence is missing detailed proof:",
            ),
        )
        for evidence_key, snapshot_fn, evidence_format, required_checks, base_url, summary_key, blocker_prefix in cases:
            with self.subTest(evidence_key=evidence_key):
                live_evidence = sample_live_evidence()
                live_evidence[evidence_key] = snapshot_fn(
                    {
                        "format": evidence_format,
                        "generated_at": "2026-07-24T12:45:00+00:00",
                        "base_url": base_url,
                        "status": "ok",
                        "checks": ok_checks(required_checks),
                        "samples": [{"label": "shallow-proof"}],
                    }
                )

                report = sample_report(live_evidence=live_evidence)
                summary = acceptance.public_report_summary(report)

                self.assertFalse(report["operator_handoff_ready"])
                self.assertFalse(summary[summary_key])
                self.assertTrue(any(str(blocker).startswith(blocker_prefix) for blocker in report["acceptance_blockers"]))

    def test_report_blocks_handoff_without_voicebox_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["voicebox_remote"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["voicebox_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("Voicebox remote compatibility evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_voicebox_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["voicebox_remote"] = {
            **live_evidence["voicebox_remote"],
            "status": "incomplete",
            "missing_checks": ["websocket_or_limitation_recorded"],
            "checks": {
                "native_http_proxy_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:41:00+00:00"},
                "profile_lifecycle_validated": {"status": "ok", "recorded_at": "2026-07-24T12:42:00+00:00"},
                "sample_artifact_protected": {"status": "ok", "recorded_at": "2026-07-24T12:42:15+00:00"},
                "profile_export_validated": {"status": "ok", "recorded_at": "2026-07-24T12:42:30+00:00"},
                "profile_delete_audited": {"status": "ok", "recorded_at": "2026-07-24T12:42:45+00:00"},
                "speech_or_limitation_recorded": {"status": "ok", "recorded_at": "2026-07-24T12:43:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["voicebox_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("Voicebox remote compatibility evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "Voicebox remote compatibility evidence is missing required checks: websocket_or_limitation_recorded",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_without_security_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["security_acceptance"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["security_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("security acceptance evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_security_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["security_acceptance"] = {
            **live_evidence["security_acceptance"],
            "status": "incomplete",
            "missing_checks": ["import_private_network_blocked", "runtime_agent_arbitrary_logs_rejected"],
            "checks": {
                "unauthenticated_requests_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:46:00+00:00"},
                "under_scoped_requests_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:47:00+00:00"},
                "cors_credentials_not_wildcard": {"status": "ok", "recorded_at": "2026-07-24T12:47:00+00:00"},
                "csrf_browser_mutation_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:48:00+00:00"},
                "comfyui_management_routes_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:48:00+00:00"},
                "import_ssrf_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "import_metadata_ssrf_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "import_plain_http_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "artifact_traversal_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "artifact_authorization_enforced": {"status": "ok", "recorded_at": "2026-07-24T12:49:30+00:00"},
                "runtime_agent_mutation_guard": {"status": "ok", "recorded_at": "2026-07-24T12:50:00+00:00"},
                "runtime_agent_arbitrary_runtime_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:50:00+00:00"},
                "logs_redacted": {"status": "ok", "recorded_at": "2026-07-24T12:50:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["security_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("security acceptance evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "security acceptance evidence is missing required checks: import_private_network_blocked, runtime_agent_arbitrary_logs_rejected",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_when_security_summary_is_absent(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["security_acceptance"].pop("missing_security_evidence", None)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["security_evidence_ready"])
        self.assertIn("security acceptance evidence lacks detailed security summary", report["acceptance_blockers"])

    def test_security_snapshot_requires_detailed_security_proof(self) -> None:
        snapshot = acceptance.security_evidence_snapshot(
            {
                "format": "b1-ai-hub-security-acceptance/v1",
                "generated_at": "2026-07-24T12:50:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": ok_checks(acceptance.SECURITY_REQUIRED_CHECKS),
                "samples": [{"label": "shallow-security-proof"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], [])
        self.assertIn("unauthenticated_requests_rejected.http_status", snapshot["missing_security_evidence"])
        self.assertIn("under_scoped_requests_rejected.auth_status", snapshot["missing_security_evidence"])
        self.assertIn("cors_credentials_not_wildcard.wildcard_credentials_false", snapshot["missing_security_evidence"])
        self.assertIn("import_ssrf_blocked.policy_case", snapshot["missing_security_evidence"])
        self.assertIn("artifact_authorization_enforced.authorized_status", snapshot["missing_security_evidence"])
        self.assertIn("artifact_authorization_enforced.authorized_sha256", snapshot["missing_security_evidence"])
        self.assertIn("artifact_authorization_enforced.unauthenticated_status", snapshot["missing_security_evidence"])
        self.assertIn("runtime_agent_mutation_guard.mtls_enabled", snapshot["missing_security_evidence"])
        self.assertIn("logs_redacted.bearer_tokens_redacted", snapshot["missing_security_evidence"])

        live_evidence = sample_live_evidence(security_acceptance=snapshot)
        report = sample_report(live_evidence=live_evidence)
        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("security acceptance evidence is missing detailed proof:", "\n".join(report["acceptance_blockers"]))

    def test_security_snapshot_requires_authorized_artifact_range_proof(self) -> None:
        payload = {
            "format": "b1-ai-hub-security-acceptance/v1",
            "generated_at": "2026-07-24T12:50:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "checks": sample_security_checks(),
            "samples": [{"label": "artifact-authorization-denied"}],
        }
        payload["checks"]["artifact_authorization_enforced"] = {
            **payload["checks"]["artifact_authorization_enforced"],
            "authorized_status": 200,
            "authorized_byte_count": 0,
            "authorized_sha256": "",
            "authorized_content_range": "",
            "authorized_content_length": "0",
            "authorized_content_type": "",
            "authorized_etag": "",
        }

        snapshot = acceptance.security_evidence_snapshot(payload)

        self.assertIn("artifact_authorization_enforced.authorized_status", snapshot["missing_security_evidence"])
        self.assertIn("artifact_authorization_enforced.authorized_byte_count", snapshot["missing_security_evidence"])
        self.assertIn("artifact_authorization_enforced.authorized_sha256", snapshot["missing_security_evidence"])
        self.assertIn("artifact_authorization_enforced.authorized_content_range", snapshot["missing_security_evidence"])
        self.assertIn("artifact_authorization_enforced.authorized_content_type", snapshot["missing_security_evidence"])
        self.assertIn("artifact_authorization_enforced.authorized_etag", snapshot["missing_security_evidence"])

    def test_report_blocks_handoff_without_restart_reconciliation_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["restart_reconciliation"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["restart_reconciliation_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("restart reconciliation evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_restart_reconciliation_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["restart_reconciliation"] = {
            **live_evidence["restart_reconciliation"],
            "status": "incomplete",
            "missing_checks": [
                "waiting_jobs_requeued",
                "active_jobs_marked_recovery_required",
                "interrupted_job_ids_recorded",
                "resumable_comfyui_native_prompts_reattached",
            ],
            "checks": {
                "control_plane_restarted": {"status": "ok", "recorded_at": "2026-07-24T12:51:00+00:00"},
                "cpu_runner_reconciled": {"status": "ok", "recorded_at": "2026-07-24T12:52:00+00:00"},
                "gpu_runner_reconciled": {"status": "ok", "recorded_at": "2026-07-24T12:52:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["restart_reconciliation_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("restart reconciliation evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "restart reconciliation evidence is missing required checks: waiting_jobs_requeued, active_jobs_marked_recovery_required, interrupted_job_ids_recorded, resumable_comfyui_native_prompts_reattached",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_when_restart_reconciliation_summary_is_absent(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["restart_reconciliation"].pop("missing_reconciliation_evidence", None)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["restart_reconciliation_evidence_ready"])
        self.assertIn("restart reconciliation evidence lacks detailed reconciliation summary", report["acceptance_blockers"])

    def test_restart_reconciliation_snapshot_requires_detailed_recovery_proof(self) -> None:
        snapshot = acceptance.restart_reconciliation_evidence_snapshot(
            {
                "format": "b1-ai-hub-restart-reconciliation-acceptance/v1",
                "generated_at": "2026-07-24T12:55:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": ok_checks(acceptance.RESTART_RECONCILIATION_REQUIRED_CHECKS),
                "samples": [{"label": "shallow-restart-proof"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], [])
        self.assertIn("control_plane_restarted.started_at", snapshot["missing_reconciliation_evidence"])
        self.assertIn("control_plane_restarted.expected_after", snapshot["missing_reconciliation_evidence"])
        self.assertIn("control_plane_restarted.api_status", snapshot["missing_reconciliation_evidence"])
        self.assertIn("control_plane_restarted.required_runners", snapshot["missing_reconciliation_evidence"])
        self.assertIn("cpu_runner_reconciled.runtime_names", snapshot["missing_reconciliation_evidence"])
        self.assertIn("cpu_runner_reconciled.runner", snapshot["missing_reconciliation_evidence"])
        self.assertIn("cpu_runner_reconciled.runner_status", snapshot["missing_reconciliation_evidence"])
        self.assertIn("gpu_runner_reconciled.runtime_names", snapshot["missing_reconciliation_evidence"])
        self.assertIn("gpu_runner_reconciled.runner", snapshot["missing_reconciliation_evidence"])
        self.assertIn("gpu_runner_reconciled.runner_status", snapshot["missing_reconciliation_evidence"])
        self.assertIn("waiting_jobs_requeued.observed", snapshot["missing_reconciliation_evidence"])
        self.assertIn("active_jobs_marked_recovery_required.observed", snapshot["missing_reconciliation_evidence"])
        self.assertIn("interrupted_job_ids_recorded.requeued_job_ids", snapshot["missing_reconciliation_evidence"])
        self.assertIn("interrupted_job_ids_recorded.recovery_required_job_ids", snapshot["missing_reconciliation_evidence"])
        self.assertIn(
            "resumable_comfyui_native_prompts_reattached.native_prompt_ids",
            snapshot["missing_reconciliation_evidence"],
        )

        live_evidence = sample_live_evidence(restart_reconciliation=snapshot)
        report = sample_report(live_evidence=live_evidence)
        summary = acceptance.public_report_summary(report)
        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["restart_reconciliation_evidence_ready"])
        self.assertIn(
            "restart reconciliation evidence is missing detailed proof:",
            "\n".join(report["acceptance_blockers"]),
        )

    def test_restart_reconciliation_snapshot_requires_fresh_runner_windows(self) -> None:
        payload = {
            "format": "b1-ai-hub-restart-reconciliation-acceptance/v1",
            "generated_at": "2026-07-24T12:55:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "checks": sample_restart_reconciliation_checks(),
            "samples": [{"label": "startup-reconciliation"}],
        }
        payload["checks"]["cpu_runner_reconciled"] = {
            **payload["checks"]["cpu_runner_reconciled"],
            "started_at": "2026-07-24T12:50:00+00:00",
            "completed_at": "2026-07-24T12:49:59+00:00",
        }

        snapshot = acceptance.restart_reconciliation_evidence_snapshot(payload)

        self.assertIn("cpu_runner_reconciled.started_after_expected_after", snapshot["missing_reconciliation_evidence"])
        self.assertIn("cpu_runner_reconciled.completed_after_started_at", snapshot["missing_reconciliation_evidence"])

    def test_restart_reconciliation_snapshot_requires_interrupted_job_id_evidence(self) -> None:
        snapshot = acceptance.restart_reconciliation_evidence_snapshot(
            {
                "format": "b1-ai-hub-restart-reconciliation-acceptance/v1",
                "generated_at": "2026-07-24T12:55:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": {
                    "control_plane_restarted": {"status": "ok"},
                    "cpu_runner_reconciled": {"status": "ok"},
                    "gpu_runner_reconciled": {"status": "ok"},
                    "waiting_jobs_requeued": {"status": "ok"},
                    "active_jobs_marked_recovery_required": {"status": "ok"},
                    "resumable_comfyui_native_prompts_reattached": {"status": "ok"},
                },
                "samples": [{"label": "startup-reconciliation"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], ["interrupted_job_ids_recorded"])
        self.assertIn("interrupted_job_ids_recorded.requeued_job_ids", snapshot["missing_reconciliation_evidence"])

    def test_report_blocks_handoff_without_backup_migration_rollback_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["backup_migration_rollback"] = {"available": False, "reason": "missing"}
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["backup_migration_rollback_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("backup, migration, and rollback evidence is unavailable", report["acceptance_blockers"])

    def test_report_blocks_handoff_for_incomplete_backup_migration_rollback_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["backup_migration_rollback"] = {
            **live_evidence["backup_migration_rollback"],
            "status": "incomplete",
            "missing_checks": ["b1_restore_rehearsed", "rollback_rehearsed"],
            "checks": {
                "b1_backup_created": {"status": "ok", "recorded_at": "2026-07-24T12:45:00+00:00"},
                "b1_backup_verified": {"status": "ok", "recorded_at": "2026-07-24T12:46:00+00:00"},
                "old_stack_inventory_reviewed": {"status": "ok", "recorded_at": "2026-07-24T12:48:00+00:00"},
                "old_stack_backup_verified": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "open_webui_migration_plan_reviewed": {"status": "ok", "recorded_at": "2026-07-24T12:50:00+00:00"},
                "cutover_plan_reviewed": {"status": "ok", "recorded_at": "2026-07-24T12:51:00+00:00"},
                "old_resources_preserved": {"status": "ok", "recorded_at": "2026-07-24T12:56:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["backup_migration_rollback_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("backup, migration, and rollback evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "backup, migration, and rollback evidence is missing required checks: b1_restore_rehearsed, rollback_rehearsed",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_when_backup_migration_rollback_summary_is_absent(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["backup_migration_rollback"].pop("missing_backup_migration_rollback_evidence", None)
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["backup_migration_rollback_evidence_ready"])
        self.assertIn(
            "backup, migration, and rollback evidence lacks detailed backup/migration/rollback summary",
            report["acceptance_blockers"],
        )

    def test_backup_migration_rollback_snapshot_requires_detailed_proof(self) -> None:
        snapshot = acceptance.backup_migration_rollback_evidence_snapshot(
            {
                "format": "b1-ai-hub-backup-migration-rollback-acceptance/v1",
                "generated_at": "2026-07-24T12:56:00+00:00",
                "base_url": "https://api.ai.b1.germering",
                "status": "ok",
                "checks": ok_checks(acceptance.BACKUP_MIGRATION_ROLLBACK_REQUIRED_CHECKS),
                "samples": [{"label": "shallow-backup-proof"}],
            }
        )

        self.assertEqual(snapshot["missing_checks"], [])
        self.assertIn("b1_backup_created.backup", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("b1_backup_verified.archive_sha256", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("b1_restore_rehearsed.restore_report", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("old_stack_inventory_reviewed.target_identity.accepted", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("old_stack_backup_verified.archive_sha256", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn(
            "open_webui_migration_plan_reviewed.backed_up_readable.accounts.database_count",
            snapshot["missing_backup_migration_rollback_evidence"],
        )
        self.assertIn("cutover_plan_reviewed.dns_readiness.common_addresses", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("cutover_plan_reviewed.target_identity_readiness.accepted", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("cutover_plan_reviewed.networking_readiness.has_dhcp_default_route", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("cutover_plan_reviewed.resources_sha256", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("rollback_rehearsed.cutover_plan_sha256", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("rollback_rehearsed.rollback_actions_sha256", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("old_resources_preserved.resources", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("old_resources_preserved.resources_sha256", snapshot["missing_backup_migration_rollback_evidence"])

        live_evidence = sample_live_evidence(backup_migration_rollback=snapshot)
        report = sample_report(live_evidence=live_evidence)
        summary = acceptance.public_report_summary(report)
        self.assertFalse(report["operator_handoff_ready"])
        self.assertFalse(summary["backup_migration_rollback_evidence_ready"])
        self.assertIn(
            "backup, migration, and rollback evidence is missing detailed proof:",
            "\n".join(report["acceptance_blockers"]),
        )

    def test_backup_migration_rollback_snapshot_requires_preserved_resource_match(self) -> None:
        checks = sample_backup_migration_rollback_checks()
        checks["old_resources_preserved"] = {
            **checks["old_resources_preserved"],
            "resources": {
                **checks["old_resources_preserved"]["resources"],
                "systemd_services_to_restart_for_rollback": [],
            },
        }

        snapshot = acceptance.backup_migration_rollback_evidence_snapshot(
            {
                "format": "b1-ai-hub-backup-migration-rollback-acceptance/v1",
                "generated_at": "2026-07-24T12:56:00+00:00",
                "status": "ok",
                "checks": checks,
                "samples": [{"label": "rollback-runbook"}],
            }
        )

        self.assertIn("old_resources_preserved.resource_count_matches_resources", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("old_resources_preserved.resources_match_cutover", snapshot["missing_backup_migration_rollback_evidence"])
        self.assertIn("old_resources_preserved.resources_sha256_matches_resources", snapshot["missing_backup_migration_rollback_evidence"])

    def test_report_blocks_handoff_when_cutover_plan_has_no_rollback_resources(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                resources={
                    "containers_to_stop_during_cutover": [],
                    "containers_to_restart_for_rollback": [],
                    "systemd_services_to_restart_for_rollback": [],
                    "docker_volumes_preserved": [],
                    "host_paths_preserved": [],
                },
                resource_count=0,
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        self.assertIn("cutover preservation plan lists no old rollback resources", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_cutover_hardware_requires_review(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                hardware_readiness={
                    "available": True,
                    "profile": "rtx3060-32gb-initial",
                    "accepted": False,
                    "largest_gpu_vram_mib": 6144,
                    "minimum_gpu_vram_mib": 12288,
                    "host_total_ram_mib": 31833,
                    "minimum_host_ram_mib": 32000,
                    "operator_must_review_hardware": True,
                    "warnings": ["largest detected GPU VRAM is 6144 MiB; required initial profile needs at least 12288 MiB"],
                }
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertFalse(summary["cutover_hardware_ready"])
        self.assertIn("cutover hardware readiness requires operator review", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_gpu_runtime_requires_review(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                gpu_runtime_readiness={
                    "available": True,
                    "accepted": False,
                    "nvidia_smi_available": True,
                    "detected_gpu_count": 1,
                    "docker_nvidia_runtime_available": False,
                    "nvidia_container_toolkit_available": False,
                    "operator_must_review_gpu_runtime": True,
                    "warnings": ["Docker does not report an nvidia runtime"],
                }
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertFalse(summary["gpu_runtime_ready"])
        self.assertIn("cutover GPU container runtime readiness requires operator review", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_runtime_agent_socket_requires_review(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                runtime_agent_socket_readiness={
                    "available": True,
                    "path": "/var/run/docker.sock",
                    "gid": 998,
                    "configured_gid": "0",
                    "configured_gid_matches": False,
                    "runtime_agent_group_access_ready": False,
                    "operator_must_review_runtime_agent_socket": True,
                    "warnings": ["B1_DOCKER_GID=0 does not match Docker socket GID 998"],
                }
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertFalse(summary["runtime_agent_socket_ready"])
        self.assertIn("cutover runtime-agent Docker socket readiness requires operator review", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_cutover_dns_requires_review(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                dns_readiness={
                    "all_hosts_resolve": True,
                    "all_hosts_share_gateway_address": True,
                    "operator_must_review_dns": True,
                    "reference_host": "ai.b1.germering",
                    "common_addresses": ["192.168.2.100"],
                    "missing_hosts": [],
                    "divergent_hosts": [],
                    "optional_missing_hosts": [],
                    "optional_divergent_hosts": ["monitoring.ai.b1.germering"],
                }
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertFalse(summary["cutover_dns_ready"])
        self.assertIn("cutover DNS readiness requires operator review", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_cutover_target_identity_requires_review(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                target_identity_readiness=sample_target_identity_readiness(
                    observed_hostname="b1-5",
                    observed_fqdn="b1-5",
                    observed_platform_node="b1-5",
                    hostname_matches_expected=False,
                    fqdn_matches_expected=False,
                    platform_node_matches_expected=False,
                    accepted=False,
                    operator_must_review_target_identity=True,
                    warnings=["Inventory host identity does not match expected target ai.b1.germering"],
                )
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertFalse(summary["cutover_target_identity_ready"])
        self.assertIn("cutover target host identity readiness requires operator review", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_cutover_networking_requires_review(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                networking_readiness={
                    "available": True,
                    "hostname_authority": "system-hostname",
                    "hostname_source": "system-hostname",
                    "network_property_source": "host-dhcp-client",
                    "b1_manages_host_networking": False,
                    "b1_static_ip_configures": False,
                    "non_loopback_address_count": 1,
                    "default_route_interfaces": ["eno1"],
                    "default_route_address_count": 1,
                    "default_route_count": 1,
                    "default_route_protocols": ["static"],
                    "has_dhcp_default_route": False,
                    "dns_record_count": 7,
                    "operator_must_review_networking": True,
                    "warnings": ["Inventory did not prove a DHCP-owned default route"],
                }
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertFalse(summary["cutover_networking_ready"])
        self.assertIn("cutover host DHCP/networking readiness requires operator review", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_open_webui_preservation_requires_review(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                open_webui_preservation={
                    "plan_supplied": True,
                    "operator_must_review_open_webui": True,
                    "recommended_strategy": "back-up-open-webui-before-cutover",
                    "plan_warnings": ["readable Open WebUI database was not preserved"],
                }
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertFalse(summary["open_webui_preservation_ready"])
        self.assertIn("Open WebUI preservation requires operator review before handoff", report["acceptance_blockers"])

    def test_report_blocks_handoff_when_cutover_plan_has_unresolved_warnings(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                warnings=["production HTTPS port is occupied by an unreviewed listener"]
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["cutover_preservation_ready"])
        self.assertFalse(summary["cutover_warnings_ready"])
        self.assertIn("cutover plan has unresolved warnings", report["acceptance_blockers"])

    def test_latest_cutover_preservation_snapshot_reads_only_direct_supported_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ignored = root / "cutover-plan-old.json"
            ignored.write_text(json.dumps({"format": "unknown"}), encoding="utf-8")
            plan = {
                "format": "b1-ai-hub-cutover-plan/v1",
                "created_at": "2026-07-24T11:30:00+00:00",
                "safety": sample_cutover_preservation()["safety"],
                "inputs": {"old_stack_backup_verification": {"status": "verified"}},
                "old_stack_scope": {
                    "reviewed_by": "operator",
                    "review_notes": "reviewed",
                    "containers_to_stop_during_cutover": ["old-open-webui"],
                    "containers_to_restart_for_rollback": ["old-open-webui"],
                    "systemd_services_to_restart_for_rollback": ["ollama.service"],
                    "docker_volumes_preserved": ["open-webui-data"],
                    "host_paths_preserved": ["/srv/old-ai/docker-compose.yaml"],
                },
                "dns_readiness": sample_cutover_preservation()["dns_readiness"],
                "target_identity_readiness": sample_cutover_preservation()["target_identity_readiness"],
                "networking_readiness": sample_cutover_preservation()["networking_readiness"],
                "hardware_readiness": sample_cutover_preservation()["hardware_readiness"],
                "gpu_runtime_readiness": sample_cutover_preservation()["gpu_runtime_readiness"],
                "runtime_agent_socket_readiness": sample_cutover_preservation()["runtime_agent_socket_readiness"],
                "open_webui_preservation": {"plan_supplied": True, "operator_must_review_open_webui": False},
                "warnings": ["review DNS"],
            }
            current = root / "cutover-plan.json"
            current.write_text(json.dumps(plan), encoding="utf-8")

            snapshot = acceptance.latest_cutover_preservation_snapshot(root)

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["source_path"], str(current.resolve()))
        self.assertEqual(snapshot["old_stack_backup_verification_status"], "verified")
        self.assertEqual(snapshot["dns_readiness"]["optional_missing_hosts"], ["monitoring.ai.b1.germering"])
        self.assertTrue(snapshot["target_identity_readiness"]["accepted"])
        self.assertTrue(snapshot["networking_readiness"]["has_dhcp_default_route"])
        self.assertTrue(snapshot["hardware_readiness"]["accepted"])
        self.assertTrue(snapshot["gpu_runtime_readiness"]["accepted"])
        self.assertTrue(snapshot["runtime_agent_socket_readiness"]["runtime_agent_group_access_ready"])
        self.assertFalse(snapshot["open_webui_preservation"]["operator_must_review_open_webui"])
        self.assertEqual(snapshot["resources"]["containers_to_restart_for_rollback"], ["old-open-webui"])
        self.assertEqual(snapshot["resources"]["systemd_services_to_restart_for_rollback"], ["ollama.service"])
        self.assertEqual(snapshot["resource_count"], 4)

    def test_latest_live_evidence_snapshot_reads_latest_direct_supported_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_root = root / "acceptance"
            evidence_root.mkdir()
            native_prompt_id = "prompt_native_1"
            native_job_id = "job_native_1"
            remote_artifact_sha = "e" * 64
            voicebox_profile_id = "vp_disk1"
            voicebox_sample_id = "sample_disk1"
            voicebox_sample_url = "/artifacts/voicebox/references/operator/sample_disk1/sample.wav"
            voicebox_sample_sha = "c" * 64
            voicebox_speech_sha = "f" * 64
            voicebox_identity = {
                "proxy_version": "b1-voicebox-proxy/v0.5.0-b1",
                "upstream_repository": "jamiepine/voicebox",
                "upstream_version": "v0.5.0",
                "upstream_commit": "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d",
                "source_archive_sha256": "d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083",
            }
            ignored = evidence_root / "older.json"
            ignored.write_text(json.dumps({"format": "unknown"}), encoding="utf-8")
            repository_quality = evidence_root / "repository-quality.json"
            repository_quality_payload = sample_live_evidence()["repository_quality"]
            repository_quality_payload["samples"] = [{"label": label} for label in repository_quality_payload["sample_labels"]]
            repository_quality.write_text(json.dumps(repository_quality_payload), encoding="utf-8")
            preflight = evidence_root / "operator-preflight.json"
            preflight_payload = sample_live_evidence()["operator_preflight"]
            preflight_payload["checks"] = list(preflight_payload["checks"].values())
            preflight.write_text(json.dumps(preflight_payload), encoding="utf-8")
            smoke = evidence_root / "live-smoke.json"
            smoke_payload = sample_live_evidence()["live_stack_smoke"]
            smoke_payload["samples"] = [{"label": label} for label in smoke_payload["sample_labels"]]
            smoke_payload["source_commit"] = "c" * 40
            smoke_payload["source_ref"] = "agent/test"
            smoke_payload["source_dirty"] = False
            smoke_payload["dirty_path_count"] = 0
            smoke.write_text(json.dumps(smoke_payload), encoding="utf-8")
            current = evidence_root / "cross-runtime-gpu.json"
            gpu_payload = sample_live_evidence()["gpu_acceptance"]
            gpu_payload["samples"] = [{"label": label} for label in gpu_payload["sample_labels"]]
            current.write_text(json.dumps(gpu_payload), encoding="utf-8")
            localai = evidence_root / "localai-runtime.json"
            localai_payload = sample_live_evidence()["localai_runtime"]
            localai_payload["samples"] = [{"label": label} for label in localai_payload["sample_labels"]]
            localai.write_text(json.dumps(localai_payload), encoding="utf-8")
            installed = evidence_root / "installed-workflows.json"
            installed_payload = sample_live_evidence()["installed_workflows"]
            installed_payload["samples"] = [{"label": label} for label in installed_payload["sample_labels"]]
            installed.write_text(json.dumps(installed_payload), encoding="utf-8")
            remote = evidence_root / "remote-nodes-non-comfy.json"
            remote.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1",
                        "generated_at": "2026-07-24T12:35:00+00:00",
                        "base_url": "https://api.ai.b1.germering",
                        "status": "ok",
                        "checks": {
                            "server_side_comfyui_stopped": {"status": "ok", "stop_mode": "manual"},
                            "server_side_comfyui_stop_verified": {
                                "status": "ok",
                                "verified_by": "admin_runtimes_runtime_agent_services",
                                "running_container_count": 0,
                            },
                            "node_surface_registered": sample_remote_node_surface_check(),
                            "server_side_comfyui_still_stopped_after_operation": {
                                "status": "ok",
                                "verified_by": "admin_runtimes_runtime_agent_services",
                                "running_container_count": 0,
                            },
                            "remote_models_listed": {
                                "status": "ok",
                                "alias_count": 12,
                                "selected_model_visible": True,
                                "model": "tts-fast",
                            },
                            "model_alias_selected": {"status": "ok", "model": "tts-fast"},
                            "credentials_externalized": {
                                "status": "ok",
                                "credential_source": "environment_file",
                                "inspected_workflow_count": 3,
                                "workflow_secret_findings": [],
                            },
                            "non_comfy_tts_completed": {
                                "status": "ok",
                                "model": "tts-fast",
                                "runtime_policy": "non_comfy_only",
                                "byte_count": 2048,
                                "sha256": remote_artifact_sha,
                                "placeholder_proof": {
                                    "placeholder": False,
                                    "cpu_audio_engine": "piper",
                                    "placeholder_failure": False,
                                    "reasons": [],
                                },
                            },
                            "artifact_downloaded": {
                                "status": "ok",
                                "filename": "b1-remote-node-non-comfy.wav",
                                "relative_path": "b1-remote-node-non-comfy.wav",
                                "byte_count": 2048,
                                "stat_size": 2048,
                                "sha256": remote_artifact_sha,
                                "file_sha256": remote_artifact_sha,
                                "path_within_download_dir": True,
                                "symlink": False,
                                "private_file_mode": True,
                            },
                        },
                        "samples": [{"label": "remote-node-model-list"}, {"label": "tts-fast-non-comfy"}],
                    }
                ),
                encoding="utf-8",
            )
            native_comfyui = evidence_root / "native-comfyui.json"
            native_payload = sample_live_evidence()["native_comfyui_compatibility"]
            native_payload["samples"] = [{"label": label} for label in native_payload["sample_labels"]]
            native_comfyui.write_text(json.dumps(native_payload), encoding="utf-8")
            legacy_comfyui = evidence_root / "legacy-comfy-listener.json"
            legacy_comfyui.write_text(
                json.dumps(sample_legacy_comfy_payload()),
                encoding="utf-8",
            )
            modelhub = evidence_root / "modelhub-client-sync.json"
            modelhub.write_text(
                json.dumps(sample_modelhub_client_sync_payload()),
                encoding="utf-8",
            )
            voicebox = evidence_root / "voicebox-remote.json"
            voicebox.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-voicebox-remote-compatibility/v1",
                        "generated_at": "2026-07-24T12:45:00+00:00",
                        "base_url": "https://voice.ai.b1.germering",
                        "status": "ok",
                        "checks": {
                            "proxy_build_info_validated": {
                                "status": "ok",
                                "runtime": "voicebox",
                                "action": "build-info",
                                "proxy": "b1-voicebox-proxy",
                                "pinned": True,
                                **voicebox_identity,
                            },
                            "native_http_proxy_accessible": {
                                "status": "ok",
                                "http_status": 200,
                                **voicebox_identity,
                            },
                            "profile_lifecycle_validated": {
                                "status": "ok",
                                "profile_id": voicebox_profile_id,
                                "model_alias": "tts-quality",
                                "sample_artifact_count": 1,
                                "sample_artifact_url": voicebox_sample_url,
                                "sample_artifact_sha256": voicebox_sample_sha,
                                "fetched_sample_artifact_count": 1,
                                "fetched_sample_artifact_url": voicebox_sample_url,
                                "fetched_sample_artifact_sha256": voicebox_sample_sha,
                            },
                            "sample_artifact_protected": {
                                "status": "ok",
                                "sample_id": voicebox_sample_id,
                                "sample_url_prefix": "/artifacts/voicebox/references/",
                                "sample_artifact_url": voicebox_sample_url,
                                "sample_artifact_bytes": 3244,
                                "sample_artifact_sha256": voicebox_sample_sha,
                                "sample_artifact_mime_type": "audio/wav",
                                "profile_metadata_has_sample_payload": False,
                                "export_contains_raw_sample_bytes": False,
                            },
                            "profile_export_validated": {
                                "status": "ok",
                                "profile_id": voicebox_profile_id,
                                "export_format": "b1-ai-hub-voice-profile/v1",
                                "contains_sensitive_data": True,
                                "sample_artifact_count": 1,
                                "exported_sample_artifact_url": voicebox_sample_url,
                                "exported_sample_artifact_sha256": voicebox_sample_sha,
                            },
                            "profile_delete_audited": {
                                "status": "ok",
                                "profile_id": voicebox_profile_id,
                                "deleted_status": "deleted",
                                "audit_event_types": ["voice_profile.deleted", "voice_profile.exported", "voice_profile.sample_uploaded"],
                                "sample_upload_audit_target_id": voicebox_sample_id,
                                "profile_export_audit_target_id": voicebox_profile_id,
                                "profile_delete_audit_target_id": voicebox_profile_id,
                                "sample_upload_audit_bytes": 3244,
                                "sample_upload_audit_sha256": voicebox_sample_sha,
                                "audit_metadata_redacted": True,
                            },
                            "speech_or_limitation_recorded": {
                                "status": "ok",
                                "mode": "speech_validated",
                                "model": "tts-quality",
                                "byte_count": 4096,
                                "sha256": voicebox_speech_sha,
                                "content_type": "audio/wav",
                                **voicebox_identity,
                            },
                            "websocket_or_limitation_recorded": {
                                "status": "ok",
                                "mode": "websocket_validated",
                                "path": "/ws",
                                "received_type": "none",
                                **voicebox_identity,
                            },
                        },
                        "samples": [
                            {"label": "voicebox-proxy-build-info"},
                            {"label": "voicebox-native-http"},
                            {"label": "voice-profile-lifecycle"},
                            {"label": "voice-sample-artifact"},
                            {"label": "voicebox-speech"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            security = evidence_root / "security-acceptance.json"
            security.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-security-acceptance/v1",
                        "generated_at": "2026-07-24T12:50:00+00:00",
                        "base_url": "https://api.ai.b1.germering",
                        "status": "ok",
                        "checks": sample_security_checks(),
                        "samples": [
                            {"label": "unauthenticated-admin"},
                            {"label": "under-scoped-admin"},
                            {"label": "cors-denied-origin"},
                            {"label": "csrf-missing-token"},
                            {"label": "comfyui-manager-denied"},
                            {"label": "import-ssrf-blocked"},
                            {"label": "import-metadata-ssrf-blocked"},
                            {"label": "import-private-network-blocked"},
                            {"label": "import-plain-http-blocked"},
                            {"label": "artifact-traversal-denied"},
                            {"label": "artifact-authorization-denied"},
                            {"label": "runtime-agent-mutation-guard"},
                            {"label": "runtime-agent-arbitrary-runtime-denied"},
                            {"label": "runtime-agent-arbitrary-logs-denied"},
                            {"label": "service-logs-redacted"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            restart_reconciliation = evidence_root / "restart-reconciliation.json"
            restart_reconciliation.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-restart-reconciliation-acceptance/v1",
                        "generated_at": "2026-07-24T12:55:00+00:00",
                        "base_url": "https://api.ai.b1.germering",
                        "status": "ok",
                        "checks": sample_restart_reconciliation_checks(),
                        "samples": [
                            {"label": "startup-reconciliation"},
                            {"label": "recovered-job-counts"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            backup_rollback = evidence_root / "backup-migration-rollback.json"
            backup_rollback.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-backup-migration-rollback-acceptance/v1",
                        "generated_at": "2026-07-24T12:56:00+00:00",
                        "base_url": "https://api.ai.b1.germering",
                        "status": "ok",
                        "checks": sample_backup_migration_rollback_checks(),
                        "samples": [
                            {"label": "b1-backup"},
                            {"label": "restore-test"},
                            {"label": "old-stack-backup"},
                            {"label": "rollback-runbook"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = acceptance.latest_live_evidence_snapshot(root)

        quality_snapshot = snapshot["repository_quality"]
        self.assertTrue(quality_snapshot["available"])
        self.assertEqual(quality_snapshot["source_path"], str(repository_quality.resolve()))
        self.assertEqual(quality_snapshot["status"], "ok")
        self.assertEqual(quality_snapshot["missing_checks"], [])
        self.assertEqual(quality_snapshot["missing_quality_evidence"], [])
        self.assertEqual(quality_snapshot["source_commit"], "c" * 40)
        self.assertFalse(quality_snapshot["source_dirty"])
        self.assertEqual(quality_snapshot["command_count"], 2)
        self.assertEqual(
            quality_snapshot["quality_result_detail_count"],
            sum(len(items) for items in acceptance.REPOSITORY_QUALITY_REQUIRED_COVERAGE.values()),
        )
        preflight_snapshot = snapshot["operator_preflight"]
        self.assertTrue(preflight_snapshot["available"])
        self.assertEqual(preflight_snapshot["source_path"], str(preflight.resolve()))
        self.assertEqual(preflight_snapshot["status"], "ok")
        self.assertEqual(preflight_snapshot["missing_checks"], [])
        self.assertEqual(preflight_snapshot["missing_preflight_evidence"], [])
        self.assertEqual(preflight_snapshot["preflight_fail_count"], 0)
        self.assertEqual(preflight_snapshot["checks"]["api_keys"]["status"], "ok")
        smoke_snapshot = snapshot["live_stack_smoke"]
        self.assertTrue(smoke_snapshot["available"])
        self.assertEqual(smoke_snapshot["source_path"], str(smoke.resolve()))
        self.assertEqual(smoke_snapshot["status"], "ok")
        self.assertEqual(smoke_snapshot["missing_checks"], [])
        self.assertEqual(smoke_snapshot["missing_smoke_evidence"], [])
        self.assertEqual(smoke_snapshot["smoke_artifact_count"], 1)
        self.assertEqual(smoke_snapshot["smoke_verified_artifact_count"], 1)
        self.assertEqual(smoke_snapshot["smoke_total_downloaded_bytes"], 4096)
        self.assertEqual(smoke_snapshot["smoke_artifact_bytes"], 4096)
        self.assertEqual(smoke_snapshot["smoke_open_webui_base_url"], "https://ai.b1.germering")
        self.assertEqual(smoke_snapshot["smoke_open_webui_status_code"], 200)
        self.assertEqual(smoke_snapshot["sample_count"], 6)
        self.assertEqual(smoke_snapshot["source_commit"], "c" * 40)
        self.assertEqual(smoke_snapshot["source_ref"], "agent/test")
        self.assertFalse(smoke_snapshot["source_dirty"])
        self.assertEqual(smoke_snapshot["dirty_path_count"], 0)
        gpu = snapshot["gpu_acceptance"]
        self.assertTrue(gpu["available"])
        self.assertEqual(gpu["source_path"], str(current.resolve()))
        self.assertEqual(gpu["status"], "ok")
        self.assertEqual(gpu["missing_checks"], [])
        self.assertEqual(gpu["missing_model_measurements"], [])
        self.assertEqual(gpu["missing_gpu_evidence"], [])
        self.assertEqual(gpu["gpu_runtime_order"], ["localai", "comfyui", "voicebox"])
        self.assertEqual(gpu["model_measurements"]["chat-default"]["resolved_model_version"], "b1-chat-default@1.0.0")
        self.assertEqual(gpu["sample_count"], 4)
        localai_snapshot = snapshot["localai_runtime"]
        self.assertTrue(localai_snapshot["available"])
        self.assertEqual(localai_snapshot["source_path"], str(localai.resolve()))
        self.assertEqual(localai_snapshot["status"], "ok")
        self.assertEqual(localai_snapshot["missing_checks"], [])
        self.assertEqual(localai_snapshot["missing_model_measurements"], [])
        self.assertEqual(localai_snapshot["missing_localai_evidence"], [])
        self.assertEqual(localai_snapshot["model_measurements"]["chat-default"]["runtime"], "localai")
        self.assertEqual(localai_snapshot["localai_unload_stage"], "idle_unloaded")
        self.assertEqual(localai_snapshot["sample_count"], 3)
        workflows = snapshot["installed_workflows"]
        self.assertTrue(workflows["available"])
        self.assertEqual(workflows["source_path"], str(installed.resolve()))
        self.assertEqual(workflows["status"], "ok")
        self.assertEqual(workflows["missing_checks"], [])
        self.assertEqual(workflows["missing_model_measurements"], [])
        self.assertEqual(workflows["missing_installed_workflow_evidence"], [])
        self.assertEqual(workflows["installed_workflow_artifact_count"], 3)
        self.assertEqual(workflows["model_measurements"]["video-text"]["runtime"], "comfyui")
        self.assertEqual(workflows["sample_count"], 7)
        native = snapshot["native_comfyui_compatibility"]
        self.assertTrue(native["available"])
        self.assertEqual(native["source_path"], str(native_comfyui.resolve()))
        self.assertEqual(native["status"], "ok")
        self.assertEqual(native["missing_checks"], [])
        self.assertEqual(native["missing_compatibility_evidence"], [])
        self.assertEqual(native["native_prompt_id"], native_prompt_id)
        self.assertEqual(native["durable_job_id"], native_job_id)
        self.assertEqual(native["durable_artifact_count"], 1)
        self.assertEqual(native["durable_verified_artifact_count"], 1)
        self.assertEqual(native["durable_artifact_first_bytes"], 4096)
        self.assertEqual(native["view_artifact_count"], 1)
        self.assertEqual(native["view_verified_artifact_count"], 1)
        self.assertEqual(native["native_summary_node_count"], 2)
        self.assertEqual(native["native_summary_stored_artifact_count"], 1)
        self.assertEqual(native["sample_count"], 13)
        legacy = snapshot["legacy_comfyui_listener"]
        self.assertTrue(legacy["available"])
        self.assertEqual(legacy["source_path"], str(legacy_comfyui.resolve()))
        self.assertEqual(legacy["status"], "ok")
        self.assertEqual(legacy["missing_checks"], [])
        self.assertEqual(legacy["missing_legacy_evidence"], [])
        self.assertEqual(legacy["sample_count"], 3)
        remote_nodes = snapshot["remote_nodes_non_comfy"]
        self.assertTrue(remote_nodes["available"])
        self.assertEqual(remote_nodes["source_path"], str(remote.resolve()))
        self.assertEqual(remote_nodes["status"], "ok")
        self.assertEqual(remote_nodes["missing_checks"], [])
        self.assertEqual(remote_nodes["missing_compatibility_evidence"], [])
        self.assertEqual(remote_nodes["remote_required_node_count"], len(acceptance.REMOTE_NODES_REQUIRED_NODE_CLASSES))
        self.assertEqual(remote_nodes["remote_registered_node_count"], len(acceptance.REMOTE_NODES_REQUIRED_NODE_CLASSES))
        self.assertEqual(remote_nodes["remote_example_workflow_count"], 3)
        self.assertEqual(remote_nodes["remote_selected_model"], "tts-fast")
        self.assertEqual(remote_nodes["remote_tts_bytes"], 2048)
        self.assertEqual(remote_nodes["sample_count"], 2)
        modelhub_sync = snapshot["modelhub_client_sync"]
        self.assertTrue(modelhub_sync["available"])
        self.assertEqual(modelhub_sync["source_path"], str(modelhub.resolve()))
        self.assertEqual(modelhub_sync["status"], "ok")
        self.assertEqual(modelhub_sync["missing_checks"], [])
        self.assertEqual(modelhub_sync["sample_count"], 1)
        voicebox_remote = snapshot["voicebox_remote"]
        self.assertTrue(voicebox_remote["available"])
        self.assertEqual(voicebox_remote["source_path"], str(voicebox.resolve()))
        self.assertEqual(voicebox_remote["status"], "ok")
        self.assertEqual(voicebox_remote["missing_checks"], [])
        self.assertEqual(voicebox_remote["missing_compatibility_evidence"], [])
        self.assertEqual(voicebox_remote["voicebox_profile_id"], voicebox_profile_id)
        self.assertEqual(voicebox_remote["voicebox_proxy_version"], "b1-voicebox-proxy/v0.5.0-b1")
        self.assertEqual(voicebox_remote["voicebox_upstream_commit"], "2bcb98d1a8b6fe05e15fbc1559e3085669e4035d")
        self.assertEqual(voicebox_remote["voicebox_speech_mode"], "speech_validated")
        self.assertEqual(voicebox_remote["voicebox_websocket_mode"], "websocket_validated")
        self.assertEqual(voicebox_remote["sample_count"], 5)
        security_acceptance = snapshot["security_acceptance"]
        self.assertTrue(security_acceptance["available"])
        self.assertEqual(security_acceptance["source_path"], str(security.resolve()))
        self.assertEqual(security_acceptance["status"], "ok")
        self.assertEqual(security_acceptance["missing_checks"], [])
        self.assertEqual(security_acceptance["missing_security_evidence"], [])
        self.assertEqual(security_acceptance["security_log_lines_checked"], 42)
        self.assertEqual(security_acceptance["sample_count"], 15)
        restart = snapshot["restart_reconciliation"]
        self.assertTrue(restart["available"])
        self.assertEqual(restart["source_path"], str(restart_reconciliation.resolve()))
        self.assertEqual(restart["status"], "ok")
        self.assertEqual(restart["missing_checks"], [])
        self.assertEqual(restart["missing_reconciliation_evidence"], [])
        self.assertEqual(restart["restart_requeued_waiting_count"], 2)
        self.assertEqual(restart["restart_recovery_required_count"], 1)
        self.assertEqual(restart["restart_resumed_comfyui_native_count"], 1)
        self.assertEqual(restart["restart_native_prompt_ids"], ["prompt_native_1"])
        self.assertEqual(restart["sample_count"], 2)
        backup = snapshot["backup_migration_rollback"]
        self.assertTrue(backup["available"])
        self.assertEqual(backup["source_path"], str(backup_rollback.resolve()))
        self.assertEqual(backup["status"], "ok")
        self.assertEqual(backup["missing_checks"], [])
        self.assertEqual(backup["missing_backup_migration_rollback_evidence"], [])
        self.assertEqual(backup["backup_b1_files_verified"], 12)
        self.assertEqual(backup["backup_restore_files_verified"], 12)
        self.assertEqual(backup["backup_old_stack_files_verified"], 4)
        self.assertEqual(backup["backup_preserved_resource_count"], 4)
        self.assertEqual(backup["backup_systemd_services_preserved_count"], 1)
        self.assertEqual(backup["backup_preserved_resource_counts_by_type"]["systemd_services_to_restart_for_rollback"], 1)
        self.assertEqual(len(backup["backup_preserved_resources_sha256"]), 64)
        self.assertEqual(backup["backup_rollback_cutover_plan_sha256"], "c" * 64)
        self.assertEqual(backup["backup_rollback_actions_sha256"], "d" * 64)
        self.assertEqual(backup["sample_count"], 4)

    def test_latest_live_evidence_snapshot_ignores_symlinked_evidence_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_root = root / "acceptance"
            evidence_root.mkdir()
            outside = root / "outside-smoke.json"
            smoke_payload = sample_live_evidence()["live_stack_smoke"]
            smoke_payload["samples"] = [{"label": label} for label in smoke_payload["sample_labels"]]
            outside.write_text(json.dumps(smoke_payload), encoding="utf-8")
            self.symlink_or_skip(outside, evidence_root / "live-smoke.json")

            snapshot = acceptance.latest_live_evidence_snapshot(root)

        smoke = snapshot["live_stack_smoke"]
        self.assertFalse(smoke["available"])
        self.assertEqual(smoke["reason"], "no supported live acceptance evidence found")

    def test_report_id_rejects_traversal(self) -> None:
        with self.assertRaises(acceptance.AcceptanceReportError):
            acceptance.report_directory(Path("/tmp/b1"), "../escape")

    def test_source_control_snapshot_reads_git_head_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git_dir = root / ".git"
            ref_dir = git_dir / "refs" / "heads"
            ref_dir.mkdir(parents=True)
            (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (ref_dir / "main").write_text("d" * 40 + "\n", encoding="utf-8")

            snapshot = acceptance.source_control_snapshot(root, environ={})

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["source"], "git")
        self.assertEqual(snapshot["source_commit"], "d" * 40)
        self.assertEqual(snapshot["source_ref"], "main")

    def test_source_control_snapshot_prefers_valid_environment_commit(self) -> None:
        snapshot = acceptance.source_control_snapshot(
            Path("/tmp/not-used"),
            environ={"B1_SOURCE_COMMIT": "E" * 40, "B1_SOURCE_REF": "release/v1", "B1_APP_VERSION": "1.2.3"},
        )

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["source"], "environment")
        self.assertEqual(snapshot["source_commit"], "e" * 40)
        self.assertEqual(snapshot["version"], "1.2.3")


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class AcceptanceReportApiTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def patch_auth(self, role: Any = None, scopes: frozenset[str] = frozenset({"*"})) -> None:
        async def authenticate(_: str | None = None) -> Any:
            return AuthContext(subject_id="admin_1", role=role or Role.ADMIN, scopes=scopes)

        self.patch_attr("authenticate", authenticate)

    def test_create_list_and_get_acceptance_report_endpoint(self) -> None:
        audit_events: list[dict[str, Any]] = []

        async def build_snapshot(auth: Any, payload: Any) -> dict[str, Any]:
            return sample_report(label=payload.label, notes=payload.notes)

        async def record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_auth()
        self.patch_attr("build_acceptance_report_snapshot", build_snapshot)
        self.patch_attr("record_audit_event", record_audit_event)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.patch_attr("acceptance_report_root_path", lambda: root)

            created = asyncio.run(
                main.admin_acceptance_report_create(
                    main.AcceptanceReportCreate(label="cutover", notes="validated on temporary hostnames"),
                    authorization="Bearer key",
                )
            )
            listed = asyncio.run(main.admin_acceptance_reports(authorization="Bearer key", limit=10))
            fetched = asyncio.run(main.admin_acceptance_report_get(created["report"]["id"], authorization="Bearer key"))
            markdown_file = asyncio.run(
                main.admin_acceptance_report_file(created["report"]["id"], "report.md", authorization="Bearer key")
            )

        self.assertEqual(created["summary"]["status"], "ok")
        self.assertEqual(created["report"]["label"], "cutover")
        self.assertEqual(listed["data"][0]["id"], created["report"]["id"])
        self.assertEqual(fetched["report"]["id"], created["report"]["id"])
        self.assertIn(b"# B1 AI Hub Acceptance Report", markdown_file.body)
        self.assertEqual(markdown_file.headers["content-disposition"], 'attachment; filename="report.md"')
        self.assertEqual(markdown_file.media_type, "text/markdown; charset=utf-8")
        self.assertEqual(audit_events[0]["event_type"], "acceptance_report.created")
        self.assertEqual(audit_events[0]["target_type"], "acceptance_report")

    def test_acceptance_report_preview_does_not_write_or_audit(self) -> None:
        audit_events: list[dict[str, Any]] = []

        async def build_snapshot(auth: Any, payload: Any) -> dict[str, Any]:
            return sample_report(label=payload.label, notes=payload.notes)

        async def record_audit_event(auth: Any, event_type: str, **kwargs: Any) -> None:
            audit_events.append({"event_type": event_type, **kwargs})

        self.patch_auth(scopes=frozenset({"admin:read"}))
        self.patch_attr("build_acceptance_report_snapshot", build_snapshot)
        self.patch_attr("record_audit_event", record_audit_event)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.patch_attr("acceptance_report_root_path", lambda: root)

            preview = asyncio.run(
                main.admin_acceptance_report_preview(
                    main.AcceptanceReportCreate(label="cutover preview", notes="checking evidence before handoff"),
                    authorization="Bearer key",
                )
            )
            listed = asyncio.run(main.admin_acceptance_reports(authorization="Bearer key", limit=10))

        self.assertEqual(preview["summary"]["label"], "cutover preview")
        self.assertNotIn("files", preview["summary"])
        self.assertEqual(preview["report"]["label"], "cutover preview")
        self.assertEqual(listed["data"], [])
        self.assertEqual(audit_events, [])

    def test_acceptance_report_create_requires_admin_write_scope(self) -> None:
        self.patch_auth(scopes=frozenset({"admin:read"}))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                main.admin_acceptance_report_create(
                    main.AcceptanceReportCreate(),
                    authorization="Bearer key",
                )
            )

        self.assertEqual(caught.exception.status_code, 403)

    def test_snapshot_captures_runtime_agent_services_and_recent_updates(self) -> None:
        class FakeDatabase:
            async def get_scheduler_owner(self) -> dict[str, Any]:
                return {"owner": "idle"}

            async def list_runtime_states(self) -> list[dict[str, Any]]:
                return [{"runtime": "localai", "status": "idle", "stage": "idle"}]

            async def list_runtime_reservations(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
                return []

            async def list_model_records(self) -> list[dict[str, Any]]:
                return []

            async def list_update_plans(self, limit: int = 5) -> list[dict[str, Any]]:
                return [
                    {
                        "id": "update_1",
                        "target_version": "0.2.0",
                        "source_url": "https://example.invalid/releases/0.2.0",
                        "status": "validated",
                        "stage": "health_check_completed",
                        "image_refs": [{"service": "control-plane", "image": "ghcr.io/b1/control-plane:0.2.0@sha256:" + "a" * 64}],
                        "image_stage": [],
                        "compose_override": {},
                        "backup_name": "backup_1",
                        "self_test": {},
                        "promotion_result": {},
                        "rollback_result": {},
                        "notes": "",
                        "failure_message": None,
                    }
                ][:limit]

        async def self_test(subject_id: str) -> dict[str, Any]:
            return {
                "status": "ok",
                "subject_id": subject_id,
                "checks": [
                    {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
                    {"name": "runtimes:production-readiness", "status": "ok", "detail": "ready"},
                    {"name": "runtime-agent:mutation-guard", "status": "ok", "detail": "ready"},
                ],
            }

        async def metrics(limit: int = 500) -> dict[str, Any]:
            return {"gpu": {"available": True}, "jobs": {}}

        async def admission_report(subject_id: str) -> dict[str, Any]:
            return {"storage": {"root": "/srv/b1-ai-hub"}}

        async def runtime_agent_get(path: str) -> tuple[dict[str, Any] | None, str | None]:
            self.assertEqual(path, "/v1/services")
            return {"services": [{"name": "control-plane", "containers": [{"short_id": "abc123", "image_id": "sha256:" + "b" * 64}]}]}, None

        class FakeCatalog:
            def to_catalog(self) -> dict[str, Any]:
                return {"aliases": [], "profiles": [], "models": []}

        self.patch_auth()
        self.patch_attr("database", FakeDatabase())
        self.patch_attr("build_self_test_report", self_test)
        self.patch_attr("build_admin_metrics_payload", metrics)
        self.patch_attr("admission_report", admission_report)
        self.patch_attr("runtime_agent_get", runtime_agent_get)
        self.patch_attr("catalog_snapshot", lambda: FakeCatalog())
        self.patch_attr(
            "settings",
            main.Settings(
                **{
                    **main.settings.__dict__,
                    "runtime_deployment_mode": "production",
                    "host_control": "control.test.lan",
                    "host_api": "api.test.lan",
                    "data_root": "/data/b1-ai-hub",
                }
            ),
        )

        snapshot = asyncio.run(
            main.build_acceptance_report_snapshot(
                AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"*"})),
                main.AcceptanceReportCreate(label="cutover"),
            )
        )

        self.assertEqual(snapshot["deployment"]["services"][0]["name"], "control-plane")
        self.assertEqual(snapshot["recent_updates"][0]["id"], "update_1")
        self.assertIn("source", snapshot["source_control"])
        urls = {item["key"]: item["url"] for item in snapshot["handoff"]["default_urls"]}
        self.assertEqual(urls["control"], "https://control.test.lan/")
        self.assertEqual(urls["api"], "https://api.test.lan/")
        self.assertIn("/data/b1-ai-hub/secrets/admin_bootstrap_key", snapshot["handoff"]["admin_onboarding"][2]["action"])
        self.assertEqual(snapshot["model_measurement_coverage"]["status"], "incomplete")
        self.assertFalse(snapshot["operator_handoff_ready"])
        blockers = "; ".join(snapshot["acceptance_blockers"])
        self.assertIn("operator evidence missing", blockers)
        self.assertIn("database model-smoke coverage status is incomplete", blockers)


if __name__ == "__main__":
    unittest.main()
