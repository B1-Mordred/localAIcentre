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
        "native_comfyui_compatibility": {
            "available": True,
            "format": "b1-ai-hub-native-comfyui-compatibility/v1",
            "source_path": "/srv/b1-ai-hub/backups/acceptance/native-comfyui.json",
            "generated_at": "2026-07-24T12:33:00+00:00",
            "base_url": "https://comfy.ai.b1.germering",
            "status": "ok",
            "required_checks": [
                "object_info_accessible",
                "system_stats_accessible",
                "models_accessible",
                "queue_accessible",
                "prompt_submission",
                "websocket_events",
                "history_available",
            ],
            "missing_checks": [],
            "checks": {
                "object_info_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "system_stats_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "models_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "queue_accessible": {"status": "ok", "recorded_at": "2026-07-24T12:31:00+00:00"},
                "prompt_submission": {"status": "ok", "recorded_at": "2026-07-24T12:32:00+00:00"},
                "websocket_events": {"status": "ok", "recorded_at": "2026-07-24T12:32:00+00:00"},
                "history_available": {"status": "ok", "recorded_at": "2026-07-24T12:33:00+00:00"},
            },
            "sample_count": 3,
            "sample_labels": ["object-info", "prompt-submission", "websocket-completed"],
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
        }
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
            ],
        },
    )
    return acceptance.build_report(
        report_id=report_id,
        created_by="admin_1",
        label=overrides.pop("label", "cutover dry run"),
        notes=overrides.pop("notes", "operator notes"),
        generated_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
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
            self.assertIn("## Deployment Services", markdown)
            self.assertIn("ghcr.io/b1/control-plane", markdown)
            self.assertIn("## Recent Update Records", markdown)
            self.assertIn("## Source Control", markdown)
            self.assertIn("## Operator Evidence", markdown)
            self.assertIn("RTX 3060/32 GB cross-runtime acceptance", markdown)
            self.assertIn("## Live Acceptance Evidence", markdown)
            self.assertIn("cross-runtime-gpu.json", markdown)
            self.assertIn("native-comfyui.json", markdown)
            self.assertIn("Native ComfyUI compatibility", markdown)
            self.assertIn("remote-nodes-non-comfy.json", markdown)
            self.assertIn("Remote-node non-Comfy compatibility", markdown)
            self.assertIn("modelhub-client-sync.json", markdown)
            self.assertIn("Model Hub client sync", markdown)
            self.assertIn("## Old Resources Preserved For Rollback", markdown)
            self.assertIn("old-open-webui", markdown)
            self.assertIn("open-webui-data", markdown)

            checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
            checksums = dict(line.split("  ", 1)[::-1] for line in checksum_lines)
            self.assertEqual(checksums["report.json"], hashlib.sha256(json_path.read_bytes()).hexdigest())
            self.assertEqual(checksums["report.md"], hashlib.sha256(markdown_path.read_bytes()).hexdigest())

            loaded = acceptance.load_report(root, report["id"])
            listed = acceptance.list_reports(root)

        self.assertEqual(loaded["format"], acceptance.REPORT_FORMAT)
        self.assertEqual([item["id"] for item in listed], [report["id"]])

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
                "open_webui_preservation": {"plan_supplied": True},
                "warnings": ["review DNS"],
            }
            current = root / "cutover-plan.json"
            current.write_text(json.dumps(plan), encoding="utf-8")

            snapshot = acceptance.latest_cutover_preservation_snapshot(root)

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["source_path"], str(current.resolve()))
        self.assertEqual(snapshot["old_stack_backup_verification_status"], "verified")
        self.assertEqual(snapshot["resources"]["containers_to_restart_for_rollback"], ["old-open-webui"])
        self.assertEqual(snapshot["resource_count"], 3)

    def test_latest_live_evidence_snapshot_reads_latest_direct_supported_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_root = root / "acceptance"
            evidence_root.mkdir()
            ignored = evidence_root / "older.json"
            ignored.write_text(json.dumps({"format": "unknown"}), encoding="utf-8")
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
                            "system_stats_accessible": {"status": "ok"},
                            "models_accessible": {"status": "ok"},
                            "queue_accessible": {"status": "ok"},
                            "prompt_submission": {"status": "ok"},
                            "websocket_events": {"status": "ok"},
                            "history_available": {"status": "ok"},
                        },
                        "samples": [
                            {"label": "object-info"},
                            {"label": "prompt-submission"},
                            {"label": "websocket-completed"},
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

            snapshot = acceptance.latest_live_evidence_snapshot(root)

        gpu = snapshot["gpu_acceptance"]
        self.assertTrue(gpu["available"])
        self.assertEqual(gpu["source_path"], str(current.resolve()))
        self.assertEqual(gpu["status"], "ok")
        self.assertEqual(gpu["missing_checks"], [])
        self.assertEqual(gpu["sample_count"], 2)
        native = snapshot["native_comfyui_compatibility"]
        self.assertTrue(native["available"])
        self.assertEqual(native["source_path"], str(native_comfyui.resolve()))
        self.assertEqual(native["status"], "ok")
        self.assertEqual(native["missing_checks"], [])
        self.assertEqual(native["sample_count"], 3)
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

        self.assertEqual(created["summary"]["status"], "ok")
        self.assertEqual(created["report"]["label"], "cutover")
        self.assertEqual(listed["data"][0]["id"], created["report"]["id"])
        self.assertEqual(fetched["report"]["id"], created["report"]["id"])
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
        self.patch_attr("settings", main.Settings(**{**main.settings.__dict__, "runtime_deployment_mode": "production"}))

        snapshot = asyncio.run(
            main.build_acceptance_report_snapshot(
                AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"*"})),
                main.AcceptanceReportCreate(label="cutover"),
            )
        )

        self.assertEqual(snapshot["deployment"]["services"][0]["name"], "control-plane")
        self.assertEqual(snapshot["recent_updates"][0]["id"], "update_1")
        self.assertIn("source", snapshot["source_control"])
        self.assertFalse(snapshot["operator_handoff_ready"])
        self.assertIn("operator evidence missing", "; ".join(snapshot["acceptance_blockers"]))


if __name__ == "__main__":
    unittest.main()
