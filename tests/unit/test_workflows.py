from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.workflows import (  # noqa: E402
    NodePinError,
    WorkflowError,
    dependency_report,
    load_workflows,
    parse_node_pin_registry,
    parse_workflow,
    validate_workflow_job_request,
    visible_to_role,
    workflow_record,
)


BASE_WORKFLOW = {
    "id": "test-workflow",
    "version": "1.0.0",
    "display_name": "Test Workflow",
    "modality": "image",
    "operation": "generation",
    "model_alias": "image-default",
    "backend_policy": "comfyui-only",
    "runtime_policy": "any",
    "output_mime_types": ["image/png"],
    "workflow_json": {"1": {"class_type": "CheckpointLoaderSimple"}},
    "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
    "output_schema": {"type": "object", "properties": {"images": {"type": "array"}}},
    "resource_class": "rtx3060-32gb",
    "dependencies": [{"type": "runtime", "id": "comfyui"}, {"type": "model", "id": "image-default"}],
    "limits": {"max_width": 1024, "max_height": 1024, "max_batch_size": 1},
    "visibility_roles": ["admin", "creator", "user"],
}


def model_lookup(model_id: str) -> dict[str, object] | None:
    if model_id == "image-default":
        return {"id": "image-default", "status": "uninstalled", "resolved_model": None}
    if model_id == "tts-fast":
        return {"id": "tts-fast", "status": "installed", "resolved_model": {"id": "tts-model"}}
    return None


def installed_model_lookup(model_id: str) -> dict[str, object] | None:
    if model_id == "image-default":
        return {"id": "image-default", "status": "installed", "resolved_model": {"id": "sdxl"}}
    return model_lookup(model_id)


class WorkflowTests(unittest.TestCase):
    def test_parse_seed_workflows(self) -> None:
        workflows = load_workflows(ROOT / "workflows" / "approved")
        self.assertEqual(
            {workflow.id for workflow in workflows},
            {
                "background-removal",
                "frame-interpolation",
                "image-to-image",
                "image-to-video",
                "inpainting-outpainting",
                "transcription",
                "tts",
                "text-to-image",
                "text-to-video",
                "upscaling",
            },
        )
        self.assertEqual(workflows[0].resource_class, "rtx3060-32gb")
        self.assertTrue(all(workflow.model_alias for workflow in workflows))
        self.assertTrue(all(workflow.output_mime_types for workflow in workflows))

    def test_dependency_report_marks_uninstalled_models_not_ready(self) -> None:
        workflow = parse_workflow(BASE_WORKFLOW)
        report = dependency_report(workflow, model_lookup, {"comfyui"})
        self.assertFalse(report["ready"])
        by_id = {item["id"]: item for item in report["dependencies"]}
        self.assertTrue(by_id["comfyui"]["ready"])
        self.assertFalse(by_id["image-default"]["ready"])

    def test_dependency_report_marks_approved_custom_node_ready(self) -> None:
        commit = "a" * 40
        registry = parse_node_pin_registry(
            {
                "schema_version": 1,
                "nodes": [
                    {
                        "id": "comfyui-impact-pack",
                        "repository_url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                        "commit": commit,
                        "approved_by": "admin",
                        "approved_at": "2026-07-22",
                        "dependency_lock_sha256": "b" * 64,
                    }
                ],
            }
        )
        workflow = parse_workflow(
            {
                **BASE_WORKFLOW,
                "dependencies": [
                    {"type": "runtime", "id": "comfyui"},
                    {"type": "model", "id": "image-default"},
                    {"type": "node", "id": "comfyui-impact-pack", "version": commit},
                ],
            }
        )

        report = dependency_report(workflow, installed_model_lookup, {"comfyui"}, lambda node_id, commit_value: registry.get((node_id, commit_value or "")))

        self.assertTrue(report["ready"])
        by_id = {item["id"]: item for item in report["dependencies"]}
        self.assertEqual(by_id["comfyui-impact-pack"]["status"], "approved")
        self.assertEqual(by_id["comfyui-impact-pack"]["repository_url"], "https://github.com/ltdrdata/ComfyUI-Impact-Pack")
        self.assertEqual(by_id["comfyui-impact-pack"]["dependency_lock_sha256"], "b" * 64)

    def test_dependency_report_rejects_unapproved_custom_node(self) -> None:
        commit = "a" * 40
        workflow = parse_workflow(
            {
                **BASE_WORKFLOW,
                "dependencies": [
                    {"type": "runtime", "id": "comfyui"},
                    {"type": "model", "id": "image-default"},
                    {"type": "node", "id": "comfyui-impact-pack", "version": commit},
                ],
            }
        )

        record = workflow_record(workflow, installed_model_lookup, {"comfyui"}, lambda _node_id, _commit: None)

        self.assertFalse(record["publishable"])
        node = next(item for item in record["dependency_status"]["dependencies"] if item["type"] == "node")
        self.assertEqual(node["status"], "unapproved")
        self.assertIn("approved pin registry", node["reason"])

    def test_dependency_report_rejects_disabled_custom_node_pin(self) -> None:
        commit = "a" * 40
        registry = parse_node_pin_registry(
            {
                "schema_version": 1,
                "nodes": [
                    {
                        "id": "comfyui-impact-pack",
                        "repository_url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack",
                        "commit": commit,
                        "status": "disabled",
                    }
                ],
            }
        )
        workflow = parse_workflow(
            {
                **BASE_WORKFLOW,
                "dependencies": [
                    {"type": "runtime", "id": "comfyui"},
                    {"type": "model", "id": "image-default"},
                    {"type": "node", "id": "comfyui-impact-pack", "version": commit},
                ],
            }
        )

        report = dependency_report(workflow, installed_model_lookup, {"comfyui"}, lambda node_id, commit_value: registry.get((node_id, commit_value or "")))

        self.assertFalse(report["ready"])
        node = next(item for item in report["dependencies"] if item["type"] == "node")
        self.assertEqual(node["status"], "disabled")
        self.assertIn("not approved", node["reason"])

    def test_parse_workflow_requires_node_commit_pin(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "version is required"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "dependencies": [
                        {"type": "runtime", "id": "comfyui"},
                        {"type": "model", "id": "image-default"},
                        {"type": "node", "id": "comfyui-impact-pack"},
                    ],
                }
            )
        with self.assertRaisesRegex(WorkflowError, "lowercase git commit"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "dependencies": [
                        {"type": "runtime", "id": "comfyui"},
                        {"type": "model", "id": "image-default"},
                        {"type": "node", "id": "comfyui-impact-pack", "version": "A" * 40},
                    ],
                }
            )

    def test_parse_node_pin_registry_rejects_unsafe_source_url(self) -> None:
        with self.assertRaisesRegex(NodePinError, "HTTPS URL"):
            parse_node_pin_registry(
                {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "comfyui-impact-pack",
                            "repository_url": "http://github.com/ltdrdata/ComfyUI-Impact-Pack",
                            "commit": "a" * 40,
                        }
                    ],
                }
            )
        with self.assertRaisesRegex(NodePinError, "credentials"):
            parse_node_pin_registry(
                {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "comfyui-impact-pack",
                            "repository_url": "https://token@github.com/ltdrdata/ComfyUI-Impact-Pack",
                            "commit": "a" * 40,
                        }
                    ],
                }
            )

    def test_workflow_record_is_publishable_when_dependencies_ready(self) -> None:
        payload = {
            **BASE_WORKFLOW,
            "id": "tts-workflow",
            "modality": "tts",
            "operation": "speech",
            "model_alias": "tts-fast",
            "backend_policy": "non-comfy-only",
            "runtime_policy": "non_comfy_only",
            "output_mime_types": ["audio/wav"],
            "dependencies": [{"type": "runtime", "id": "audio-cpu"}, {"type": "model", "id": "tts-fast"}],
        }
        record = workflow_record(parse_workflow(payload), model_lookup, {"audio-cpu"})
        self.assertTrue(record["publishable"])
        self.assertEqual(record["status"], "published")
        self.assertEqual(record["model_alias"], "tts-fast")

    def test_parse_workflow_accepts_comfyui_parameter_mappings(self) -> None:
        workflow = parse_workflow(
            {
                **BASE_WORKFLOW,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "steps": {"type": "integer", "minimum": 1, "maximum": 40},
                    },
                },
                "workflow_json": {
                    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                    "3": {"class_type": "KSampler", "inputs": {"steps": 20}},
                },
                "comfyui_parameter_mappings": [
                    {"parameter": "prompt", "path": ["6", "inputs", "text"]},
                    {"parameter": "steps", "path": ["3", "inputs", "steps"]},
                ],
            }
        )

        mappings = workflow.to_dict()["comfyui_parameter_mappings"]
        self.assertEqual(mappings[0]["path"], ["6", "inputs", "text"])
        self.assertEqual(mappings[1]["parameter"], "steps")

    def test_parse_workflow_accepts_runtime_parameter_mappings(self) -> None:
        workflow = parse_workflow(
            {
                **BASE_WORKFLOW,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source_image": {"type": "string", "contentEncoding": "base64", "contentMediaType": "image/png"},
                        "mask_image": {"type": "string", "contentEncoding": "base64", "contentMediaType": "image/png"},
                        "prompt": {"type": "string"},
                    },
                },
                "runtime_parameter_mappings": [
                    {"runtime": "localai", "parameter": "source_image", "target": "image"},
                    {"runtime": "localai", "parameter": "mask_image", "target": "mask", "keep_source": True},
                ],
            }
        )

        mappings = workflow.to_dict()["runtime_parameter_mappings"]
        self.assertEqual(mappings[0], {"runtime": "localai", "parameter": "source_image", "target": "image", "keep_source": False})
        self.assertEqual(mappings[1]["target"], "mask")
        self.assertTrue(mappings[1]["keep_source"])

    def test_parse_workflow_rejects_invalid_runtime_parameter_mappings(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "input_schema property"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "runtime_parameter_mappings": [
                        {"runtime": "localai", "parameter": "missing", "target": "image"},
                    ],
                }
            )
        with self.assertRaisesRegex(WorkflowError, "adapter parameter"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "runtime_parameter_mappings": [
                        {"runtime": "localai", "parameter": "prompt", "target": "../image"},
                    ],
                }
            )

    def test_parse_workflow_rejects_invalid_comfyui_parameter_mappings(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "input_schema property"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "comfyui_parameter_mappings": [
                        {"parameter": "missing", "path": ["6", "inputs", "text"]},
                    ],
                }
            )
        with self.assertRaisesRegex(WorkflowError, "unsafe segment"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "comfyui_parameter_mappings": [
                        {"parameter": "prompt", "path": ["6", "__proto__", "text"]},
                    ],
                }
            )

    def test_visibility_follows_roles_and_admin_wildcard(self) -> None:
        workflow = parse_workflow(BASE_WORKFLOW)
        record = workflow_record(workflow, model_lookup, {"comfyui"})
        self.assertTrue(visible_to_role(record, "user", frozenset({"workflows:read"})))
        self.assertFalse(visible_to_role(record, "service", frozenset({"workflows:read"})))
        self.assertTrue(visible_to_role(record, "service", frozenset({"*"})))

    def test_validation_rejects_unsafe_shapes(self) -> None:
        with self.assertRaises(WorkflowError):
            parse_workflow({**BASE_WORKFLOW, "id": "../escape"})
        with self.assertRaises(WorkflowError):
            parse_workflow({**BASE_WORKFLOW, "input_schema": {"type": "array"}})
        with self.assertRaises(WorkflowError):
            parse_workflow({**BASE_WORKFLOW, "limits": {"max_width": 0}})
        with self.assertRaises(WorkflowError):
            parse_workflow({**BASE_WORKFLOW, "model_alias": "missing-model"})
        with self.assertRaises(WorkflowError):
            parse_workflow({**BASE_WORKFLOW, "output_mime_types": ["not-a-mime"]})

    def test_validate_workflow_job_request_accepts_schema_conforming_payload(self) -> None:
        workflow = parse_workflow(
            {
                **BASE_WORKFLOW,
                "input_schema": {
                    "type": "object",
                    "required": ["prompt"],
                    "properties": {
                        "prompt": {"type": "string", "minLength": 1, "maxLength": 20},
                        "width": {"type": "integer", "minimum": 64, "maximum": 1024, "default": 512},
                        "steps": {"type": "integer", "minimum": 1, "maximum": 40, "default": 20},
                    },
                    "additionalProperties": False,
                },
                "limits": {"max_width": 1024, "max_steps": 40, "max_batch_size": 1},
            }
        ).to_dict()
        parameters = validate_workflow_job_request(
            workflow,
            {
                "modality": "image",
                "operation": "generation",
                "model": "image-default",
                "runtime_policy": "any",
                "input": {
                    "workflow_id": "test-workflow",
                    "workflow_version": "1.0.0",
                    "parameters": {"prompt": "make image", "width": 512, "steps": 20},
                },
            },
        )

        self.assertEqual(parameters["prompt"], "make image")

    def test_validate_workflow_job_request_rejects_tampering_and_schema_violations(self) -> None:
        workflow = parse_workflow(
            {
                **BASE_WORKFLOW,
                "input_schema": {
                    "type": "object",
                    "required": ["prompt"],
                    "properties": {
                        "prompt": {"type": "string", "minLength": 1},
                        "steps": {"type": "integer", "minimum": 1, "maximum": 40},
                    },
                    "additionalProperties": False,
                },
                "limits": {"max_steps": 40},
            }
        ).to_dict()
        payload = {
            "modality": "image",
            "operation": "generation",
            "model": "image-default",
            "runtime_policy": "any",
            "input": {"workflow_id": "test-workflow", "workflow_version": "1.0.0", "parameters": {"prompt": "ok", "steps": 20}},
        }

        with self.assertRaisesRegex(WorkflowError, "model must match"):
            validate_workflow_job_request(workflow, {**payload, "model": "image-edit"})
        with self.assertRaisesRegex(WorkflowError, "unsupported fields"):
            validate_workflow_job_request(
                workflow,
                {
                    **payload,
                    "input": {"workflow_id": "test-workflow", "workflow_version": "1.0.0", "parameters": {"prompt": "ok", "extra": True}},
                },
            )
        with self.assertRaisesRegex(WorkflowError, "exceeds maximum"):
            validate_workflow_job_request(
                workflow,
                {
                    **payload,
                    "input": {"workflow_id": "test-workflow", "workflow_version": "1.0.0", "parameters": {"prompt": "ok", "steps": 41}},
                },
            )

    def test_validate_workflow_job_request_checks_base64_media(self) -> None:
        workflow = parse_workflow(json.loads((ROOT / "workflows" / "approved" / "background-removal.placeholder.json").read_text(encoding="utf-8"))).to_dict()
        valid = {
            "modality": "image",
            "operation": "background-removal",
            "model": "image-edit",
            "runtime_policy": "any",
            "input": {"workflow_id": "background-removal", "workflow_version": "0.1.0", "parameters": {"source_image": "aW1n", "transparent": True}},
        }

        self.assertEqual(validate_workflow_job_request(workflow, valid)["source_image"], "aW1n")
        with self.assertRaisesRegex(WorkflowError, "base64 encoded"):
            validate_workflow_job_request(
                workflow,
                {
                    **valid,
                    "input": {"workflow_id": "background-removal", "workflow_version": "0.1.0", "parameters": {"source_image": "not base64!", "transparent": True}},
                },
            )
        with self.assertRaisesRegex(WorkflowError, "inline media limit"):
            validate_workflow_job_request(
                workflow,
                valid,
                max_inline_media_bytes=1,
            )

    def test_validate_workflow_job_request_accepts_staged_media_reference(self) -> None:
        workflow = parse_workflow(json.loads((ROOT / "workflows" / "approved" / "background-removal.placeholder.json").read_text(encoding="utf-8"))).to_dict()
        staged = {
            "source": "staged_upload",
            "id": "upload_" + "a" * 32,
            "field": "source_image",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "source.png",
            "path": "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/source-source.png",
            "bytes": 128,
            "sha256": "b" * 64,
        }
        payload = {
            "modality": "image",
            "operation": "background-removal",
            "model": "image-edit",
            "runtime_policy": "any",
            "input": {"workflow_id": "background-removal", "workflow_version": "0.1.0", "parameters": {"source_image": staged, "transparent": True}},
        }

        self.assertEqual(validate_workflow_job_request(workflow, payload)["source_image"], staged)

    def test_validate_workflow_job_request_rejects_bad_staged_media_reference(self) -> None:
        workflow = parse_workflow(json.loads((ROOT / "workflows" / "approved" / "background-removal.placeholder.json").read_text(encoding="utf-8"))).to_dict()
        base = {
            "source": "staged_upload",
            "id": "upload_" + "a" * 32,
            "field": "source_image",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "source.png",
            "path": "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/source-source.png",
            "bytes": 128,
            "sha256": "b" * 64,
        }
        payload = {
            "modality": "image",
            "operation": "background-removal",
            "model": "image-edit",
            "runtime_policy": "any",
            "input": {"workflow_id": "background-removal", "workflow_version": "0.1.0", "parameters": {"source_image": base, "transparent": True}},
        }

        with self.assertRaisesRegex(WorkflowError, "MIME type must be image/png"):
            validate_workflow_job_request(
                workflow,
                {**payload, "input": {**payload["input"], "parameters": {"source_image": {**base, "mime_type": "image/jpeg"}, "transparent": True}}},
            )
        with self.assertRaisesRegex(WorkflowError, "path is invalid"):
            validate_workflow_job_request(
                workflow,
                {**payload, "input": {**payload["input"], "parameters": {"source_image": {**base, "path": "inputs/client/../secret.png"}, "transparent": True}}},
            )
        with self.assertRaisesRegex(WorkflowError, "staged media limit"):
            validate_workflow_job_request(
                workflow,
                payload,
                max_staged_media_bytes=1,
            )

    def test_load_workflows_rejects_duplicate_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.json").write_text(json.dumps(BASE_WORKFLOW), encoding="utf-8")
            (root / "b.json").write_text(json.dumps(BASE_WORKFLOW), encoding="utf-8")
            with self.assertRaises(WorkflowError):
                load_workflows(root)


if __name__ == "__main__":
    unittest.main()
