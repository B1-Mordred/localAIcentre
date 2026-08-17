from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
    from app.workflows import parse_workflow  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy"}:
        raise
    main = None
    HTTPException = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    parse_workflow = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


BASE_WORKFLOW = {
    "id": "media-workflow",
    "version": "1.0.0",
    "display_name": "Media Workflow",
    "modality": "image",
    "operation": "generation",
    "model_alias": "image-default",
    "backend_policy": "comfyui-only",
    "runtime_policy": "any",
    "output_mime_types": ["image/png"],
    "workflow_json": {"1": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}}},
    "input_schema": {
        "type": "object",
        "required": ["prompt", "steps"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 100},
            "steps": {"type": "integer", "minimum": 1, "maximum": 40},
        },
        "additionalProperties": False,
    },
    "output_schema": {"type": "object", "properties": {"images": {"type": "array"}}},
    "resource_class": "rtx3060-32gb",
    "dependencies": [{"type": "runtime", "id": "comfyui"}, {"type": "model", "id": "image-default"}],
    "limits": {"max_steps": 40, "max_batch_size": 1},
    "visibility_roles": ["admin", "creator", "user", "service"],
}

PERFORMANCE_PLAN = {
    "schema_version": "dialecticore.character_performance.v1",
    "on_camera_energy": "engaged",
    "gaze_style": "responsive",
    "head_motion": "subtle",
    "expression_range": "warm",
    "gesture_frequency": "occasional",
    "signature_habit": "brief attentive head tilt",
    "variation_seed": 2104,
}


class FakeWorkflowDatabase:
    def __init__(self, row: dict[str, Any] | list[dict[str, Any]] | None) -> None:
        if isinstance(row, list):
            self.rows = row
        elif row is None:
            self.rows = []
        else:
            self.rows = [row]

    async def get_workflow(self, workflow_id: str, version: str | None = None) -> dict[str, Any] | None:
        for row in self.rows:
            if row["id"] == workflow_id and row["version"] == version:
                return dict(row)
        return None

    async def list_workflows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]


def workflow_row(*, ready: bool = True) -> dict[str, Any]:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    manifest = parse_workflow(BASE_WORKFLOW).to_dict()
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "display_name": manifest["display_name"],
        "backend_policy": manifest["backend_policy"],
        "resource_class": manifest["resource_class"],
        "status": "published" if ready else "needs_dependencies",
        "visibility_roles": manifest["visibility_roles"],
        "manifest": manifest,
        "dependency_status": {"ready": ready, "dependencies": []},
        "created_at": now,
        "updated_at": now,
        "unpublished_at": None,
    }


def media_payload(**overrides: Any) -> Any:
    payload = {
        "modality": "image",
        "operation": "generation",
        "model": "image-default",
        "runtime_policy": "any",
        "priority": "single_image",
        "input": {
            "workflow_id": "media-workflow",
            "workflow_version": "1.0.0",
            "parameters": {"prompt": "make image", "steps": 20},
        },
    }
    payload.update(overrides)
    return main.MediaJobCreate(**payload)


def runtime_resolution(runtime: str) -> Any:
    return main.RuntimeResolution(
        public_alias="image-default",
        model_id="image-model",
        model_version="1.0.0",
        resolved_model_version="image-model@1.0.0",
        runtime=runtime,
        preferred_runtime=runtime,
        requires_gpu=runtime in {"comfyui", "localai", "voicebox"},
        resource_label="expected",
        runtime_policy="any",
    )


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class MediaJobWorkflowEnforcementTests(unittest.TestCase):
    def patch_database(self, fake_database: FakeWorkflowDatabase) -> None:
        original = main.database
        main.database = fake_database
        self.addCleanup(lambda: setattr(main, "database", original))

    def test_workflow_backed_media_job_is_validated_against_published_workflow(self) -> None:
        self.patch_database(FakeWorkflowDatabase(workflow_row()))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))

        workflow = asyncio.run(main.enforce_workflow_backed_media_job(auth, media_payload()))

        self.assertIsNotNone(workflow)
        self.assertEqual(workflow["id"], "media-workflow")

    def test_workflow_backed_media_job_rejects_unready_dependencies(self) -> None:
        self.patch_database(FakeWorkflowDatabase(workflow_row(ready=False)))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.enforce_workflow_backed_media_job(auth, media_payload()))

        self.assertEqual(caught.exception.status_code, 424)
        self.assertEqual(caught.exception.detail["message"], "workflow dependencies are not ready")

    def test_workflow_backed_media_job_rejects_metadata_tampering(self) -> None:
        self.patch_database(FakeWorkflowDatabase(workflow_row()))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(main.enforce_workflow_backed_media_job(auth, media_payload(model="image-edit")))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("model must match workflow value", caught.exception.detail)

    def test_workflow_backend_policy_rejects_non_comfy_runtime_for_comfy_only_workflow(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.enforce_workflow_backend_policy(
                {"id": "image-flow", "version": "1.0.0", "backend_policy": "comfyui-only"},
                runtime_resolution("localai"),
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["message"], "workflow backend_policy requires ComfyUI runtime")
        self.assertEqual(caught.exception.detail["resolved_runtime"], "localai")

    def test_workflow_backend_policy_rejects_comfy_runtime_for_non_comfy_workflow(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.enforce_workflow_backend_policy(
                {"id": "audio-flow", "version": "1.0.0", "backend_policy": "non-comfy-only"},
                runtime_resolution("comfyui"),
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["message"], "workflow backend_policy forbids ComfyUI runtime")
        self.assertEqual(caught.exception.detail["resolved_runtime"], "comfyui")

    def test_workflow_backend_policy_allows_either_and_matching_runtime(self) -> None:
        main.enforce_workflow_backend_policy(
            {"id": "image-flow", "version": "1.0.0", "backend_policy": "comfyui-only"},
            runtime_resolution("comfyui"),
        )
        main.enforce_workflow_backend_policy(
            {"id": "audio-flow", "version": "1.0.0", "backend_policy": "non-comfy-only"},
            runtime_resolution("audio-cpu"),
        )
        main.enforce_workflow_backend_policy(
            {"id": "flex-flow", "version": "1.0.0", "backend_policy": "either"},
            runtime_resolution("localai"),
        )

    def test_talking_head_lipsync_request_accepts_upload_ids_and_normalizes_priority(self) -> None:
        payload = main.MediaJobCreate(
            modality="video",
            operation="lip-sync",
            model="talking-head-lipsync",
            runtime_policy="comfyui",
            priority="single_video",
            input={
                "portrait_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                "audio_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                "audio_sha256": "a" * 64,
                "timing": {"audio_sha256": "a" * 64, "phoneme_timestamps": [], "viseme_timestamps": []},
                "width": 1280,
                "height": 720,
                "fps": 24,
                "duration_ms": 10600,
                "performance_plan": PERFORMANCE_PLAN,
            },
        )

        normalized = main.validate_talking_head_lipsync_job_request(payload)

        self.assertEqual(normalized.operation, "talking-head-lipsync")
        self.assertEqual(normalized.priority, "video")
        self.assertEqual(normalized.input["duration_ms"], 10600)
        self.assertEqual(normalized.input["performance_plan"], PERFORMANCE_PLAN)

    def test_talking_head_lipsync_request_rejects_unsupported_performance_plan(self) -> None:
        payload = main.MediaJobCreate(
            modality="video",
            operation="talking-head-lipsync",
            model="talking-head-lipsync",
            runtime_policy="comfyui",
            priority="video",
            input={
                "portrait_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                "audio_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                "audio_sha256": "a" * 64,
                "width": 512,
                "height": 512,
                "fps": 12,
                "duration_ms": 1500,
                "performance_plan": {**PERFORMANCE_PLAN, "head_motion": "wild"},
            },
        )

        with self.assertRaises(HTTPException) as caught:
            main.validate_talking_head_lipsync_job_request(payload)

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "invalid_talking_head_performance_plan")

    def test_talking_head_lipsync_request_rejects_mismatched_timing_sha(self) -> None:
        payload = main.MediaJobCreate(
            modality="video",
            operation="talking-head-lipsync",
            model="talking-head-lipsync",
            runtime_policy="comfyui",
            priority="video",
            input={
                "portrait_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                "audio_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                "audio_sha256": "a" * 64,
                "timing": {"audio_sha256": "b" * 64},
                "width": 1280,
                "height": 720,
                "fps": 24,
                "duration_ms": 1500,
            },
        )

        with self.assertRaises(HTTPException) as caught:
            main.validate_talking_head_lipsync_job_request(payload)

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["message"], "timing.audio_sha256 does not match audio_sha256")

    def test_talking_head_lipsync_scene_mode_requires_selected_speaker_region(self) -> None:
        payload = main.MediaJobCreate(
            modality="video",
            operation="talking-head-lipsync",
            model="talking-head-lipsync",
            runtime_policy="comfyui",
            priority="single_video",
            input={
                "audio_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                "audio_sha256": "a" * 64,
                "scene_artifact_id": "upload_00112233445566778899aabbccddeeff",
                "speaker_participant_id": "chatgpt",
                "camera_view": "speaker_medium",
                "seating_plan": {"chatgpt": 3, "claude": 1},
                "face_regions": [
                    {"participant_id": "chatgpt", "seat": 3, "face_region": {"x": 0.42, "y": 0.18, "width": 0.14, "height": 0.24}},
                    {"participant_id": "claude", "seat": 1, "face_region": {"x": 0.08, "y": 0.18, "width": 0.14, "height": 0.24}},
                ],
                "width": 1024,
                "height": 576,
                "fps": 12,
                "duration_ms": 1500,
            },
        )

        normalized = main.validate_talking_head_lipsync_job_request(payload)

        self.assertEqual(normalized.priority, "video")
        self.assertEqual(normalized.input["speaker_participant_id"], "chatgpt")

    def test_talking_head_scene_accepts_native_two_shot_contract(self) -> None:
        payload = main.MediaJobCreate(
            modality="video",
            operation="talking-head-lipsync",
            model="talking-head-lipsync",
            runtime_policy="comfyui",
            priority="single_video",
            input={
                "audio_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                "audio_sha256": "a" * 64,
                "scene_artifact_id": "upload_00112233445566778899aabbccddeeff",
                "speaker_participant_id": "grok",
                "framed_participant_ids": ["grok", "mistral"],
                "camera_view": "panel_two_shot",
                "camera": {"view": "panel_two_shot", "action": "cut", "composition": "native_scene_camera"},
                "seating_plan": {"grok": 5, "mistral": 6},
                "face_regions": [
                    {"participant_id": "grok", "seat": 5, "face_region": {"x": 0.58, "y": 0.45, "width": 0.08, "height": 0.15}},
                    {"participant_id": "mistral", "seat": 6, "face_region": {"x": 0.67, "y": 0.45, "width": 0.08, "height": 0.15}},
                ],
                "width": 1024,
                "height": 576,
                "fps": 12,
                "duration_ms": 1500,
            },
        )

        normalized = main.validate_talking_head_lipsync_job_request(payload)

        self.assertEqual(normalized.input["camera_view"], "panel_two_shot")
        self.assertEqual(normalized.input["framed_participant_ids"], ["grok", "mistral"])

    def test_talking_head_scene_rejects_low_resolution_native_camera(self) -> None:
        payload = main.MediaJobCreate(
            modality="video",
            operation="talking-head-lipsync",
            model="talking-head-lipsync",
            runtime_policy="comfyui",
            priority="video",
            input={
                "audio_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                "audio_sha256": "a" * 64,
                "scene_artifact_id": "upload_00112233445566778899aabbccddeeff",
                "speaker_participant_id": "chatgpt",
                "camera_view": "speaker_close",
                "seating_plan": {"chatgpt": 3},
                "face_regions": [{"participant_id": "chatgpt", "seat": 3, "face_region": {"x": 0.42, "y": 0.18, "width": 0.14, "height": 0.24}}],
                "width": 512,
                "height": 288,
                "fps": 12,
                "duration_ms": 1500,
            },
        )

        with self.assertRaises(HTTPException) as caught:
            main.validate_talking_head_lipsync_job_request(payload)

        self.assertEqual(caught.exception.detail["code"], "unsupported_camera_coverage")

    def test_studio_panel_seed_defaults_when_omitted(self) -> None:
        payload = main.MediaJobCreate(
            modality="image",
            operation="studio-panel-shot",
            model="studio-panel-shot",
            runtime_policy="comfyui",
            priority="single_image",
            input={
                "studio_reference_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                "participants": [{
                    "participant_id": "chatgpt",
                    "seat": 1,
                    "portrait_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                    "full_body_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                    "seated_reference_artifact_id": "upload_00112233445566778899aabbccddeeff",
                }],
                "stature_reference_participant_id": "chatgpt",
                "camera": {"view": "establishing_wide", "action": "cut"},
                "width": 512,
                "height": 288,
            },
        )
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write"}))

        original = main.private_upload_reference
        main.private_upload_reference = lambda _auth, value, _field, _types: {"id": value, "source": "staged_upload", "path": "inputs/test"}
        self.addCleanup(lambda: setattr(main, "private_upload_reference", original))
        normalized = main.resolve_studio_panel_shot_inputs(auth, payload)

        self.assertEqual(normalized.input["seed"], 20260802)
        self.assertEqual(
            normalized.input["stature_reference_participant_id"], "chatgpt"
        )

    def test_studio_panel_rejects_unknown_stature_reference(self) -> None:
        payload = main.MediaJobCreate(
            modality="image",
            operation="studio-panel-shot",
            model="studio-panel-shot",
            runtime_policy="comfyui",
            priority="single_image",
            input={
                "studio_reference_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                "participants": [{
                    "participant_id": "chatgpt",
                    "seat": 1,
                    "portrait_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                    "full_body_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                    "seated_reference_artifact_id": "upload_00112233445566778899aabbccddeeff",
                }],
                "stature_reference_participant_id": "claude",
                "camera": {"view": "establishing_wide", "action": "cut"},
                "width": 512,
                "height": 288,
            },
        )
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write"}))
        original = main.private_upload_reference
        main.private_upload_reference = lambda _auth, value, _field, _types: {"id": value, "source": "staged_upload", "path": "inputs/test"}
        self.addCleanup(lambda: setattr(main, "private_upload_reference", original))

        with self.assertRaises(HTTPException) as caught:
            main.resolve_studio_panel_shot_inputs(auth, payload)

        self.assertEqual(caught.exception.detail["field"], "input.stature_reference_participant_id")

    def test_studio_panel_requires_completed_seated_reference_field(self) -> None:
        payload = main.MediaJobCreate(
            modality="image",
            operation="studio-panel-shot",
            model="studio-panel-shot",
            runtime_policy="comfyui",
            priority="single_image",
            input={
                "studio_reference_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                "participants": [{
                    "participant_id": "chatgpt",
                    "seat": 1,
                    "portrait_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                    "full_body_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                }],
                "camera": {"view": "establishing_wide", "action": "cut"},
                "width": 512,
                "height": 288,
            },
        )
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write"}))
        original = main.private_upload_reference
        main.private_upload_reference = lambda _auth, value, _field, _types: {"id": value, "source": "staged_upload", "path": "inputs/test"}
        self.addCleanup(lambda: setattr(main, "private_upload_reference", original))

        with self.assertRaises(HTTPException) as caught:
            main.resolve_studio_panel_shot_inputs(auth, payload)

        self.assertEqual(caught.exception.detail["code"], "seated_reference_required")

    def test_talking_head_lipsync_scene_mode_rejects_non_16_by_9_output(self) -> None:
        payload = main.MediaJobCreate(
            modality="video",
            operation="talking-head-lipsync",
            model="talking-head-lipsync",
            runtime_policy="comfyui",
            priority="video",
            input={
                "portrait_artifact_id": "upload_0123456789abcdef0123456789abcdef",
                "audio_artifact_id": "upload_fedcba9876543210fedcba9876543210",
                "audio_sha256": "a" * 64,
                "scene_artifact_id": "upload_00112233445566778899aabbccddeeff",
                "speaker_participant_id": "chatgpt",
                "camera_view": "speaker_medium",
                "seating_plan": {"chatgpt": 3},
                "face_regions": [{"participant_id": "chatgpt", "seat": 3, "face_region": {"x": 0.42, "y": 0.18, "width": 0.14, "height": 0.24}}],
                "width": 512,
                "height": 512,
                "fps": 12,
                "duration_ms": 1500,
            },
        )

        with self.assertRaises(HTTPException) as caught:
            main.validate_talking_head_lipsync_job_request(payload)

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["code"], "invalid_talking_head_scene_input")

    def test_public_job_exposes_lipsync_evidence(self) -> None:
        result = main.public_job(
            {
                "id": "job_lipsync",
                "request_params": {},
                "redacted_request": {},
                "artifacts": [
                    {
                        "kind": "video",
                        "mime_type": "video/mp4",
                        "url": "/artifacts/talking-head-lipsync/job_lipsync/0.mp4",
                        "lip_sync": {
                            "mode": "audio_driven",
                            "audio_sha256": "a" * 64,
                            "timing_sha256": "b" * 64,
                            "measured_offset_ms": 0,
                            "duration_ms": 1500,
                            "fps": 24,
                        },
                        "performance": {
                            "mode": "audio_driven_character_performance",
                            "applied": True,
                            "plan_sha256": "c" * 64,
                            "variation_seed": 2104,
                            "backend": "b1-musetalk-protected-performance-compositor/v1",
                        },
                    }
                ],
            }
        )

        self.assertEqual(result["lip_sync"]["mode"], "audio_driven")
        self.assertEqual(result["lip_sync"]["duration_ms"], 1500)
        self.assertTrue(result["performance"]["applied"])
        self.assertEqual(result["performance"]["variation_seed"], 2104)

    def test_comfyui_managed_image_job_attaches_default_workflow(self) -> None:
        row = workflow_row()
        row["manifest"]["input_schema"]["properties"]["width"] = {"type": "integer", "minimum": 128, "maximum": 768, "default": 512}
        row["manifest"]["input_schema"]["properties"]["height"] = {"type": "integer", "minimum": 128, "maximum": 768, "default": 512}
        row["manifest"]["input_schema"]["properties"]["negative_prompt"] = {
            "type": "string",
            "maxLength": 100,
            "default": "text, watermark",
        }
        row["manifest"]["input_schema"]["properties"]["steps"]["default"] = 20
        self.patch_database(FakeWorkflowDatabase(row))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))
        payload = main.MediaJobCreate(
            modality="image",
            operation="image-generation",
            model="image-default",
            runtime_policy="comfyui",
            input={"model": "image-default", "prompt": "make image", "size": "128x128", "response_format": "url"},
        )

        rewritten, workflow = asyncio.run(main.attach_default_comfyui_workflow_if_needed(auth, payload, runtime_resolution("comfyui")))

        self.assertIsNotNone(workflow)
        self.assertEqual(rewritten.operation, "generation")
        self.assertEqual(
            rewritten.input,
            {
                "workflow_id": "media-workflow",
                "workflow_version": "1.0.0",
                "parameters": {
                    "prompt": "make image",
                    "negative_prompt": "text, watermark",
                    "steps": 20,
                    "width": 128,
                    "height": 128,
                },
            },
        )

    def test_comfyui_managed_image_job_skips_empty_legacy_workflow_for_service_client(self) -> None:
        legacy = workflow_row()
        legacy["version"] = "0.1.0"
        legacy["manifest"] = dict(legacy["manifest"], version="0.1.0", workflow_json={})
        current = workflow_row()
        current["version"] = "1.0.0"
        current["manifest"] = dict(current["manifest"], version="1.0.0")
        self.patch_database(FakeWorkflowDatabase([legacy, current]))
        auth = AuthContext(subject_id="svc", role=Role.SERVICE, scopes=frozenset({"jobs:write", "workflows:read", "inference:write"}))
        payload = main.MediaJobCreate(
            modality="image",
            operation="generation",
            model="image-default",
            runtime_policy="any",
            input={"model": "image-default", "prompt": "make image"},
        )

        rewritten, workflow = asyncio.run(main.attach_default_comfyui_workflow_if_needed(auth, payload, runtime_resolution("comfyui")))

        self.assertIsNotNone(workflow)
        self.assertEqual(workflow["version"], "1.0.0")
        self.assertEqual(rewritten.input["workflow_version"], "1.0.0")

    def test_managed_image_edit_matches_edit_workflow_operation(self) -> None:
        row = workflow_row()
        row["id"] = "image-to-image"
        row["model_alias"] = "image-edit"
        row["manifest"] = dict(row["manifest"], id="image-to-image", operation="edit", model_alias="image-edit")
        self.patch_database(FakeWorkflowDatabase(row))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))
        payload = main.MediaJobCreate(
            modality="image",
            operation="image-to-image",
            model="image-edit",
            runtime_policy="comfyui",
            input={"model": "image-edit", "prompt": "make image"},
        )

        rewritten, workflow = asyncio.run(main.attach_default_comfyui_workflow_if_needed(auth, payload, runtime_resolution("comfyui")))

        self.assertIsNotNone(workflow)
        self.assertEqual(workflow["operation"], "edit")
        self.assertEqual(rewritten.operation, "edit")

    def test_non_comfy_managed_image_job_is_not_rewritten(self) -> None:
        self.patch_database(FakeWorkflowDatabase(workflow_row()))
        auth = AuthContext(subject_id="creator", role=Role.CREATOR, scopes=frozenset({"jobs:write", "workflows:read"}))
        payload = main.MediaJobCreate(modality="image", operation="generation", model="image-default", input={"prompt": "make image"})

        rewritten, workflow = asyncio.run(main.attach_default_comfyui_workflow_if_needed(auth, payload, runtime_resolution("localai")))

        self.assertIsNone(workflow)
        self.assertIs(rewritten, payload)


if __name__ == "__main__":
    unittest.main()
