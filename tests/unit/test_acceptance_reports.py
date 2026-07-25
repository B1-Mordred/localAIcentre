from __future__ import annotations

import asyncio
import hashlib
import json
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


def complete_operator_evidence() -> dict[str, bool]:
    return {item["key"]: True for item in acceptance.required_operator_evidence_items()}


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
            "docker_volumes_preserved": ["open-webui-data"],
            "host_paths_preserved": ["/srv/old-ai/docker-compose.yaml"],
        },
        "resource_count": 3,
    }
    payload.update(overrides)
    return payload


def sample_live_evidence(**overrides: Any) -> dict[str, Any]:
    payload = {
        "live_stack_smoke": {
            "available": True,
            "format": "b1-ai-hub-live-smoke/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/live-smoke.json",
            "generated_at": "2026-07-24T12:25:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "healthz_ok",
                "models_listed",
                "tts_media_job_completed",
                "job_events_streamed",
                "artifact_downloaded",
            ],
            "missing_checks": [],
            "checks": {
                "healthz_ok": {"status": "ok", "recorded_at": "2026-07-24T12:20:00+00:00"},
                "models_listed": {"status": "ok", "recorded_at": "2026-07-24T12:21:00+00:00"},
                "tts_media_job_completed": {"status": "ok", "recorded_at": "2026-07-24T12:22:00+00:00"},
                "job_events_streamed": {"status": "ok", "recorded_at": "2026-07-24T12:23:00+00:00"},
                "artifact_downloaded": {"status": "ok", "recorded_at": "2026-07-24T12:24:00+00:00"},
            },
            "sample_count": 5,
            "sample_labels": ["healthz", "models", "tts-job", "job-events", "artifact-download"],
        },
        "gpu_acceptance": {
            "available": True,
            "format": "b1-ai-hub-cross-runtime-gpu-acceptance/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/cross-runtime-gpu.json",
            "generated_at": "2026-07-24T12:30:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": ["resource_policy_and_runtime_readiness", "localai_comfyui_voicebox_switch"],
            "missing_checks": [],
            "checks": {
                "resource_policy_and_runtime_readiness": {"status": "ok", "recorded_at": "2026-07-24T12:29:00+00:00"},
                "localai_comfyui_voicebox_switch": {"status": "ok", "recorded_at": "2026-07-24T12:30:00+00:00"},
            },
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
            "checks": {
                "streaming_chat_completed": {"status": "ok", "recorded_at": "2026-07-24T12:30:10+00:00"},
                "single_backend_enforced": {"status": "ok", "recorded_at": "2026-07-24T12:30:20+00:00"},
                "graceful_unload_verified": {"status": "ok", "recorded_at": "2026-07-24T12:30:30+00:00"},
            },
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
            ],
            "missing_checks": [],
            "checks": {
                "chat_completed": {"status": "ok", "recorded_at": "2026-07-24T12:26:00+00:00"},
                "tts_completed": {"status": "ok", "recorded_at": "2026-07-24T12:27:00+00:00"},
                "stt_completed": {"status": "ok", "recorded_at": "2026-07-24T12:28:00+00:00"},
                "cpu_audio_does_not_take_gpu_lease": {"status": "ok", "recorded_at": "2026-07-24T12:28:30+00:00"},
                "image_generation_completed": {"status": "ok", "recorded_at": "2026-07-24T12:29:00+00:00"},
                "image_edit_completed": {"status": "ok", "recorded_at": "2026-07-24T12:30:00+00:00"},
                "short_video_completed": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
            },
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
                "queue_delete_accessible",
                "interrupt_accessible",
                "view_artifact_accessible",
            ],
            "missing_checks": [],
            "checks": {
                "object_info_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "object_info_node_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "system_stats_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "models_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "queue_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "upload_image_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "upload_mask_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "prompt_submission": {"status": "ok", "recorded_at": "2026-07-24T12:32:00+00:00"},
                "prompt_idempotency_replay": {"status": "ok", "recorded_at": "2026-07-24T12:32:00+00:00"},
                "websocket_events": {"status": "ok", "recorded_at": "2026-07-24T12:32:00+00:00"},
                "history_listing_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:33:00+00:00"},
                "history_available": {"status": "ok", "recorded_at": "2026-07-24T12:33:00+00:00"},
                "queue_delete_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:33:00+00:00"},
                "interrupt_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:33:00+00:00"},
                "view_artifact_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:33:00+00:00"},
            },
            "sample_count": 10,
            "sample_labels": [
                "object-info",
                "object-info-node",
                "upload-image",
                "upload-mask",
                "prompt-submission",
                "prompt-idempotency-replay",
                "websocket-completed",
                "history-listing",
                "queue-delete",
                "targeted-interrupt",
                "view-artifact",
            ],
        },
        "remote_nodes_non_comfy": {
            "available": True,
            "format": "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/remote-nodes-non-comfy.json",
            "generated_at": "2026-07-24T12:35:00+00:00",
            "base_url": "https://api.ai.b1.germering",
            "status": "ok",
            "required_checks": ["server_side_comfyui_stopped", "non_comfy_tts_completed", "artifact_downloaded"],
            "missing_checks": [],
            "checks": {
                "server_side_comfyui_stopped": {"status": "ok", "recorded_at": "2026-07-24T12:34:00+00:00"},
                "non_comfy_tts_completed": {"status": "ok", "recorded_at": "2026-07-24T12:35:00+00:00"},
                "artifact_downloaded": {"status": "ok", "recorded_at": "2026-07-24T12:35:00+00:00"},
            },
            "sample_count": 1,
            "sample_labels": ["tts-fast-non-comfy"],
        },
        "modelhub_client_sync": {
            "available": True,
            "format": "b1-ai-hub-modelhub-client-sync/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/modelhub-client-sync.json",
            "generated_at": "2026-07-24T12:40:00+00:00",
            "base_url": "https://models.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "catalog_visible",
                "download_plan_created",
                "range_resume_downloaded",
                "cache_state_managed",
                "dry_run_prune_safe",
                "inference_only_download_blocked",
            ],
            "missing_checks": [],
            "checks": {
                "catalog_visible": {"status": "ok", "recorded_at": "2026-07-24T12:36:00+00:00"},
                "download_plan_created": {"status": "ok", "recorded_at": "2026-07-24T12:37:00+00:00"},
                "range_resume_downloaded": {"status": "ok", "recorded_at": "2026-07-24T12:38:00+00:00"},
                "cache_state_managed": {"status": "ok", "recorded_at": "2026-07-24T12:39:00+00:00"},
                "dry_run_prune_safe": {"status": "ok", "recorded_at": "2026-07-24T12:39:00+00:00"},
                "inference_only_download_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:40:00+00:00"},
            },
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
                "native_http_proxy_accessible",
                "profile_lifecycle_validated",
                "speech_or_limitation_recorded",
                "websocket_or_limitation_recorded",
            ],
            "missing_checks": [],
            "checks": {
                "native_http_proxy_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:41:00+00:00"},
                "profile_lifecycle_validated": {"status": "ok", "recorded_at": "2026-07-24T12:42:00+00:00"},
                "speech_or_limitation_recorded": {"status": "ok", "recorded_at": "2026-07-24T12:43:00+00:00"},
                "websocket_or_limitation_recorded": {"status": "ok", "recorded_at": "2026-07-24T12:44:00+00:00"},
            },
            "sample_count": 3,
            "sample_labels": ["voicebox-native-http", "voice-profile-lifecycle", "voicebox-speech"],
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
                "artifact_traversal_blocked",
                "artifact_authorization_enforced",
                "runtime_agent_mutation_guard",
                "logs_redacted",
            ],
            "missing_checks": [],
            "checks": {
                "unauthenticated_requests_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:46:00+00:00"},
                "under_scoped_requests_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:47:00+00:00"},
                "cors_credentials_not_wildcard": {"status": "ok", "recorded_at": "2026-07-24T12:47:00+00:00"},
                "csrf_browser_mutation_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:48:00+00:00"},
                "comfyui_management_routes_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:48:00+00:00"},
                "import_ssrf_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "artifact_traversal_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "artifact_authorization_enforced": {"status": "ok", "recorded_at": "2026-07-24T12:49:30+00:00"},
                "runtime_agent_mutation_guard": {"status": "ok", "recorded_at": "2026-07-24T12:50:00+00:00"},
                "logs_redacted": {"status": "ok", "recorded_at": "2026-07-24T12:50:00+00:00"},
            },
            "sample_count": 10,
            "sample_labels": [
                "unauthenticated-admin",
                "under-scoped-admin",
                "cors-denied-origin",
                "csrf-missing-token",
                "comfyui-manager-denied",
                "manifest-ssrf-denied",
                "artifact-traversal-denied",
                "artifact-authorization-denied",
                "runtime-agent-mutation-guard",
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
            ],
            "missing_checks": [],
            "checks": {
                "control_plane_restarted": {"status": "ok", "recorded_at": "2026-07-24T12:51:00+00:00"},
                "cpu_runner_reconciled": {"status": "ok", "recorded_at": "2026-07-24T12:52:00+00:00"},
                "gpu_runner_reconciled": {"status": "ok", "recorded_at": "2026-07-24T12:52:00+00:00"},
                "waiting_jobs_requeued": {"status": "ok", "recorded_at": "2026-07-24T12:53:00+00:00"},
                "active_jobs_marked_recovery_required": {"status": "ok", "recorded_at": "2026-07-24T12:54:00+00:00"},
            },
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
            "checks": {
                "b1_backup_created": {"status": "ok", "recorded_at": "2026-07-24T12:45:00+00:00"},
                "b1_backup_verified": {"status": "ok", "recorded_at": "2026-07-24T12:46:00+00:00"},
                "b1_restore_rehearsed": {"status": "ok", "recorded_at": "2026-07-24T12:47:00+00:00"},
                "old_stack_inventory_reviewed": {"status": "ok", "recorded_at": "2026-07-24T12:48:00+00:00"},
                "old_stack_backup_verified": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "open_webui_migration_plan_reviewed": {"status": "ok", "recorded_at": "2026-07-24T12:50:00+00:00"},
                "cutover_plan_reviewed": {"status": "ok", "recorded_at": "2026-07-24T12:51:00+00:00"},
                "rollback_rehearsed": {"status": "ok", "recorded_at": "2026-07-24T12:55:00+00:00"},
                "old_resources_preserved": {"status": "ok", "recorded_at": "2026-07-24T12:56:00+00:00"},
            },
            "sample_count": 4,
            "sample_labels": ["b1-backup", "restore-test", "old-stack-backup", "rollback-runbook"],
        },
    }
    payload.update(overrides)
    return payload


def sample_report(**overrides: Any) -> dict[str, Any]:
    report_id = overrides.pop("report_id", "acceptance-20260724t120000z-deadbeef")
    self_test = overrides.pop(
        "self_test",
        {
            "status": "ok",
            "checks": [
                {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
                {"name": "gpu:nvml", "status": "ok", "detail": "GPU metrics are available"},
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
    )


class AcceptanceReportTests(unittest.TestCase):
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
            self.assertIn("cp .env.production.example .env && docker compose up -d", markdown)
            self.assertIn("/srv/b1-ai-hub/secrets/admin_bootstrap_key", markdown)
            self.assertIn("Upgrade system RAM from 32 GB to at least 64 GB first", markdown)
            self.assertIn("No known limitations recorded in this acceptance report.", markdown)
            self.assertIn("## Deployment Services", markdown)
            self.assertIn("ghcr.io/b1/control-plane", markdown)
            self.assertIn("## Recent Update Records", markdown)
            self.assertIn("## Source Control", markdown)
            self.assertIn("## Operator Evidence", markdown)
            self.assertIn("RTX 3060/32 GB cross-runtime acceptance", markdown)
            self.assertIn("## Live Acceptance Evidence", markdown)
            self.assertIn("live-smoke.json", markdown)
            self.assertIn("Live stack smoke", markdown)
            self.assertIn("cross-runtime-gpu.json", markdown)
            self.assertIn("installed-workflows.json", markdown)
            self.assertIn("Installed workflow acceptance", markdown)
            self.assertIn("native-comfyui.json", markdown)
            self.assertIn("Native ComfyUI compatibility", markdown)
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
        self.assertEqual([item["id"] for item in listed], [report["id"]])

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

    def test_handoff_records_voicebox_upstream_limitations(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["voicebox_remote"]["checks"]["websocket_or_limitation_recorded"] = {
            "status": "ok",
            "recorded_at": "2026-07-24T12:44:00+00:00",
            "mode": "upstream_limitation",
            "upstream_version": "Jamie Pine Voicebox v0.5.0",
            "limitation": "Pinned upstream exposes no stable WebSocket route for this profile.",
        }

        report = sample_report(live_evidence=live_evidence)
        limitations = report["handoff"]["known_limitations"]

        self.assertTrue(report["operator_handoff_ready"])
        self.assertIn(
            {
                "source": "voicebox_remote.websocket_or_limitation_recorded",
                "detail": "Pinned upstream exposes no stable WebSocket route for this profile. (Jamie Pine Voicebox v0.5.0)",
            },
            limitations,
        )
        self.assertIn("Pinned upstream exposes no stable WebSocket route", acceptance.markdown_report(report))

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

    def test_report_blocks_handoff_without_runtime_agent_mutation_guard_check(self) -> None:
        report = sample_report(
            self_test={
                "status": "ok",
                "checks": [
                    {"name": "database", "status": "ok", "detail": "PostgreSQL ping completed"},
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
            "missing_checks": ["artifact_downloaded"],
            "checks": {
                "healthz_ok": {"status": "ok", "recorded_at": "2026-07-24T12:20:00+00:00"},
                "models_listed": {"status": "ok", "recorded_at": "2026-07-24T12:21:00+00:00"},
                "tts_media_job_completed": {"status": "ok", "recorded_at": "2026-07-24T12:22:00+00:00"},
                "job_events_streamed": {"status": "ok", "recorded_at": "2026-07-24T12:23:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["smoke_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("live stack smoke evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "live stack smoke evidence is missing required checks: artifact_downloaded",
            report["acceptance_blockers"],
        )

    def test_report_blocks_handoff_for_incomplete_live_gpu_evidence(self) -> None:
        report = sample_report(
            live_evidence=sample_live_evidence(
                gpu_acceptance={
                    **sample_live_evidence()["gpu_acceptance"],
                    "status": "incomplete",
                    "missing_checks": ["localai_comfyui_voicebox_switch"],
                    "checks": {
                        "resource_policy_and_runtime_readiness": {
                            "status": "ok",
                            "recorded_at": "2026-07-24T12:29:00+00:00",
                        }
                    },
                }
            )
        )

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("RTX 3060 GPU acceptance evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "RTX 3060 GPU acceptance evidence is missing required checks: localai_comfyui_voicebox_switch",
            report["acceptance_blockers"],
        )

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
        self.assertEqual(
            snapshot["missing_checks"],
            [
                "object_info_node_accessible",
                "upload_image_accessible",
                "upload_mask_accessible",
                "prompt_idempotency_replay",
                "history_listing_accessible",
                "queue_delete_accessible",
                "interrupt_accessible",
                "view_artifact_accessible",
            ],
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

    def test_report_blocks_handoff_for_incomplete_remote_node_evidence(self) -> None:
        live_evidence = sample_live_evidence()
        live_evidence["remote_nodes_non_comfy"] = {
            **live_evidence["remote_nodes_non_comfy"],
            "status": "incomplete",
            "missing_checks": ["artifact_downloaded"],
            "checks": {
                "server_side_comfyui_stopped": {"status": "ok", "recorded_at": "2026-07-24T12:34:00+00:00"},
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
            "missing_checks": ["range_resume_downloaded"],
            "checks": {
                "catalog_visible": {"status": "ok", "recorded_at": "2026-07-24T12:36:00+00:00"},
                "download_plan_created": {"status": "ok", "recorded_at": "2026-07-24T12:37:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["modelhub_evidence_ready"])
        self.assertIn("Model Hub client sync evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "Model Hub client sync evidence is missing required checks: range_resume_downloaded",
            report["acceptance_blockers"],
        )

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
            "missing_checks": ["csrf_browser_mutation_rejected", "logs_redacted"],
            "checks": {
                "unauthenticated_requests_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:46:00+00:00"},
                "under_scoped_requests_rejected": {"status": "ok", "recorded_at": "2026-07-24T12:47:00+00:00"},
                "cors_credentials_not_wildcard": {"status": "ok", "recorded_at": "2026-07-24T12:47:00+00:00"},
                "comfyui_management_routes_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:48:00+00:00"},
                "import_ssrf_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "artifact_traversal_blocked": {"status": "ok", "recorded_at": "2026-07-24T12:49:00+00:00"},
                "artifact_authorization_enforced": {"status": "ok", "recorded_at": "2026-07-24T12:49:30+00:00"},
                "runtime_agent_mutation_guard": {"status": "ok", "recorded_at": "2026-07-24T12:50:00+00:00"},
            },
        }
        report = sample_report(live_evidence=live_evidence)

        self.assertFalse(report["operator_handoff_ready"])
        summary = acceptance.public_report_summary(report)
        self.assertFalse(summary["security_evidence_ready"])
        self.assertFalse(summary["live_evidence_ready"])
        self.assertIn("security acceptance evidence status is incomplete", report["acceptance_blockers"])
        self.assertIn(
            "security acceptance evidence is missing required checks: csrf_browser_mutation_rejected, logs_redacted",
            report["acceptance_blockers"],
        )

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
            "missing_checks": ["waiting_jobs_requeued", "active_jobs_marked_recovery_required"],
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
            "restart reconciliation evidence is missing required checks: waiting_jobs_requeued, active_jobs_marked_recovery_required",
            report["acceptance_blockers"],
        )

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

    def test_report_blocks_handoff_when_cutover_plan_has_no_rollback_resources(self) -> None:
        report = sample_report(
            cutover_preservation=sample_cutover_preservation(
                resources={
                    "containers_to_stop_during_cutover": [],
                    "containers_to_restart_for_rollback": [],
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
                    "docker_volumes_preserved": ["open-webui-data"],
                    "host_paths_preserved": ["/srv/old-ai/docker-compose.yaml"],
                },
                "dns_readiness": sample_cutover_preservation()["dns_readiness"],
                "hardware_readiness": sample_cutover_preservation()["hardware_readiness"],
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
        self.assertTrue(snapshot["hardware_readiness"]["accepted"])
        self.assertTrue(snapshot["runtime_agent_socket_readiness"]["runtime_agent_group_access_ready"])
        self.assertFalse(snapshot["open_webui_preservation"]["operator_must_review_open_webui"])
        self.assertEqual(snapshot["resources"]["containers_to_restart_for_rollback"], ["old-open-webui"])
        self.assertEqual(snapshot["resource_count"], 3)

    def test_latest_live_evidence_snapshot_reads_latest_direct_supported_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_root = root / "acceptance"
            evidence_root.mkdir()
            ignored = evidence_root / "older.json"
            ignored.write_text(json.dumps({"format": "unknown"}), encoding="utf-8")
            smoke = evidence_root / "live-smoke.json"
            smoke.write_text(
                json.dumps(
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
                        "samples": [
                            {"label": "healthz"},
                            {"label": "models"},
                            {"label": "tts-job"},
                            {"label": "job-events"},
                            {"label": "artifact-download"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            current = evidence_root / "cross-runtime-gpu.json"
            current.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-cross-runtime-gpu-acceptance/v1",
                        "generated_at": "2026-07-24T12:30:00+00:00",
                        "base_url": "https://api.ai.b1.germering",
                        "status": "ok",
                        "checks": {
                            "resource_policy_and_runtime_readiness": {"status": "ok"},
                            "localai_comfyui_voicebox_switch": {"status": "ok"},
                        },
                        "samples": [
                            {"label": "initial-readiness"},
                            {"label": "after-localai-chat"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            localai = evidence_root / "localai-runtime.json"
            localai.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-localai-runtime-acceptance/v1",
                        "generated_at": "2026-07-24T12:31:00+00:00",
                        "base_url": "https://api.ai.b1.germering",
                        "status": "ok",
                        "checks": {
                            "streaming_chat_completed": {"status": "ok"},
                            "single_backend_enforced": {"status": "ok"},
                            "graceful_unload_verified": {"status": "ok"},
                        },
                        "samples": [
                            {"label": "localai-stream-chat"},
                            {"label": "active-gpu-runtime-states"},
                            {"label": "localai-unload"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            installed = evidence_root / "installed-workflows.json"
            installed.write_text(
                json.dumps(
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
                        "samples": [
                            {"label": "chat"},
                            {"label": "tts"},
                            {"label": "stt"},
                            {"label": "cpu-audio-no-gpu-lease"},
                            {"label": "image-generation"},
                            {"label": "image-edit"},
                            {"label": "short-video"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            remote = evidence_root / "remote-nodes-non-comfy.json"
            remote.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-remote-nodes-non-comfy-compatibility/v1",
                        "generated_at": "2026-07-24T12:35:00+00:00",
                        "base_url": "https://api.ai.b1.germering",
                        "status": "ok",
                        "checks": {
                            "server_side_comfyui_stopped": {"status": "ok"},
                            "non_comfy_tts_completed": {"status": "ok"},
                            "artifact_downloaded": {"status": "ok"},
                        },
                        "samples": [{"label": "tts-fast-non-comfy"}],
                    }
                ),
                encoding="utf-8",
            )
            native_comfyui = evidence_root / "native-comfyui.json"
            native_comfyui.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-native-comfyui-compatibility/v1",
                        "generated_at": "2026-07-24T12:33:00+00:00",
                        "base_url": "https://comfy.ai.b1.germering",
                        "status": "ok",
                        "checks": {
                            "object_info_accessible": {"status": "ok"},
                            "object_info_node_accessible": {"status": "ok"},
                            "system_stats_accessible": {"status": "ok"},
                            "models_accessible": {"status": "ok"},
                            "queue_accessible": {"status": "ok"},
                            "upload_image_accessible": {"status": "ok"},
                            "upload_mask_accessible": {"status": "ok"},
                            "prompt_submission": {"status": "ok"},
                            "prompt_idempotency_replay": {"status": "ok"},
                            "websocket_events": {"status": "ok"},
                            "history_listing_accessible": {"status": "ok"},
                            "history_available": {"status": "ok"},
                            "queue_delete_accessible": {"status": "ok"},
                            "interrupt_accessible": {"status": "ok"},
                            "view_artifact_accessible": {"status": "ok"},
                        },
                        "samples": [
                            {"label": "object-info"},
                            {"label": "object-info-node"},
                            {"label": "upload-image"},
                            {"label": "upload-mask"},
                            {"label": "prompt-submission"},
                            {"label": "prompt-idempotency-replay"},
                            {"label": "websocket-completed"},
                            {"label": "history-listing"},
                            {"label": "queue-delete"},
                            {"label": "targeted-interrupt"},
                            {"label": "view-artifact"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            modelhub = evidence_root / "modelhub-client-sync.json"
            modelhub.write_text(
                json.dumps(
                    {
                        "format": "b1-ai-hub-modelhub-client-sync/v1",
                        "generated_at": "2026-07-24T12:40:00+00:00",
                        "base_url": "https://models.ai.b1.germering",
                        "status": "ok",
                        "checks": {
                            "catalog_visible": {"status": "ok"},
                            "download_plan_created": {"status": "ok"},
                            "range_resume_downloaded": {"status": "ok"},
                            "cache_state_managed": {"status": "ok"},
                            "dry_run_prune_safe": {"status": "ok"},
                            "inference_only_download_blocked": {"status": "ok"},
                        },
                        "samples": [{"label": "modelhub-client-sync"}],
                    }
                ),
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
                            "native_http_proxy_accessible": {"status": "ok"},
                            "profile_lifecycle_validated": {"status": "ok"},
                            "speech_or_limitation_recorded": {"status": "ok"},
                            "websocket_or_limitation_recorded": {"status": "ok"},
                        },
                        "samples": [
                            {"label": "voicebox-native-http"},
                            {"label": "voice-profile-lifecycle"},
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
                        "checks": {
                            "unauthenticated_requests_rejected": {"status": "ok"},
                            "under_scoped_requests_rejected": {"status": "ok"},
                            "cors_credentials_not_wildcard": {"status": "ok"},
                            "csrf_browser_mutation_rejected": {"status": "ok"},
                            "comfyui_management_routes_blocked": {"status": "ok"},
                            "import_ssrf_blocked": {"status": "ok"},
                            "artifact_traversal_blocked": {"status": "ok"},
                            "artifact_authorization_enforced": {"status": "ok"},
                            "runtime_agent_mutation_guard": {"status": "ok"},
                            "logs_redacted": {"status": "ok"},
                        },
                        "samples": [
                            {"label": "unauthenticated-admin"},
                            {"label": "under-scoped-admin"},
                            {"label": "cors-denied-origin"},
                            {"label": "csrf-missing-token"},
                            {"label": "comfyui-manager-denied"},
                            {"label": "manifest-ssrf-denied"},
                            {"label": "artifact-traversal-denied"},
                            {"label": "artifact-authorization-denied"},
                            {"label": "runtime-agent-mutation-guard"},
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
                        "checks": {
                            "control_plane_restarted": {"status": "ok"},
                            "cpu_runner_reconciled": {"status": "ok"},
                            "gpu_runner_reconciled": {"status": "ok"},
                            "waiting_jobs_requeued": {"status": "ok"},
                            "active_jobs_marked_recovery_required": {"status": "ok"},
                        },
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
                        "checks": {
                            "b1_backup_created": {"status": "ok"},
                            "b1_backup_verified": {"status": "ok"},
                            "b1_restore_rehearsed": {"status": "ok"},
                            "old_stack_inventory_reviewed": {"status": "ok"},
                            "old_stack_backup_verified": {"status": "ok"},
                            "open_webui_migration_plan_reviewed": {"status": "ok"},
                            "cutover_plan_reviewed": {"status": "ok"},
                            "rollback_rehearsed": {"status": "ok"},
                            "old_resources_preserved": {"status": "ok"},
                        },
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

        smoke_snapshot = snapshot["live_stack_smoke"]
        self.assertTrue(smoke_snapshot["available"])
        self.assertEqual(smoke_snapshot["source_path"], str(smoke.resolve()))
        self.assertEqual(smoke_snapshot["status"], "ok")
        self.assertEqual(smoke_snapshot["missing_checks"], [])
        self.assertEqual(smoke_snapshot["sample_count"], 5)
        gpu = snapshot["gpu_acceptance"]
        self.assertTrue(gpu["available"])
        self.assertEqual(gpu["source_path"], str(current.resolve()))
        self.assertEqual(gpu["status"], "ok")
        self.assertEqual(gpu["missing_checks"], [])
        self.assertEqual(gpu["sample_count"], 2)
        localai_snapshot = snapshot["localai_runtime"]
        self.assertTrue(localai_snapshot["available"])
        self.assertEqual(localai_snapshot["source_path"], str(localai.resolve()))
        self.assertEqual(localai_snapshot["status"], "ok")
        self.assertEqual(localai_snapshot["missing_checks"], [])
        self.assertEqual(localai_snapshot["sample_count"], 3)
        workflows = snapshot["installed_workflows"]
        self.assertTrue(workflows["available"])
        self.assertEqual(workflows["source_path"], str(installed.resolve()))
        self.assertEqual(workflows["status"], "ok")
        self.assertEqual(workflows["missing_checks"], [])
        self.assertEqual(workflows["sample_count"], 7)
        native = snapshot["native_comfyui_compatibility"]
        self.assertTrue(native["available"])
        self.assertEqual(native["source_path"], str(native_comfyui.resolve()))
        self.assertEqual(native["status"], "ok")
        self.assertEqual(native["missing_checks"], [])
        self.assertEqual(native["sample_count"], 11)
        remote_nodes = snapshot["remote_nodes_non_comfy"]
        self.assertTrue(remote_nodes["available"])
        self.assertEqual(remote_nodes["source_path"], str(remote.resolve()))
        self.assertEqual(remote_nodes["status"], "ok")
        self.assertEqual(remote_nodes["missing_checks"], [])
        self.assertEqual(remote_nodes["sample_count"], 1)
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
        self.assertEqual(voicebox_remote["sample_count"], 3)
        security_acceptance = snapshot["security_acceptance"]
        self.assertTrue(security_acceptance["available"])
        self.assertEqual(security_acceptance["source_path"], str(security.resolve()))
        self.assertEqual(security_acceptance["status"], "ok")
        self.assertEqual(security_acceptance["missing_checks"], [])
        self.assertEqual(security_acceptance["sample_count"], 10)
        restart = snapshot["restart_reconciliation"]
        self.assertTrue(restart["available"])
        self.assertEqual(restart["source_path"], str(restart_reconciliation.resolve()))
        self.assertEqual(restart["status"], "ok")
        self.assertEqual(restart["missing_checks"], [])
        self.assertEqual(restart["sample_count"], 2)
        backup = snapshot["backup_migration_rollback"]
        self.assertTrue(backup["available"])
        self.assertEqual(backup["source_path"], str(backup_rollback.resolve()))
        self.assertEqual(backup["status"], "ok")
        self.assertEqual(backup["missing_checks"], [])
        self.assertEqual(backup["sample_count"], 4)

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

        self.patch_auth()
        self.patch_attr("database", FakeDatabase())
        self.patch_attr("build_self_test_report", self_test)
        self.patch_attr("build_admin_metrics_payload", metrics)
        self.patch_attr("admission_report", admission_report)
        self.patch_attr("runtime_agent_get", runtime_agent_get)
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
        self.assertFalse(snapshot["operator_handoff_ready"])
        self.assertIn("operator evidence missing", "; ".join(snapshot["acceptance_blockers"]))


if __name__ == "__main__":
    unittest.main()
