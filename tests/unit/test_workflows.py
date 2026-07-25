from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app.workflows import (  # noqa: E402
    BACKEND_POLICIES,
    LIMIT_KEYS,
    MODALITIES,
    NodePinError,
    RESOURCE_CLASSES,
    ROLES,
    RUNTIME_POLICIES,
    WorkflowError,
    dependency_report,
    load_workflows,
    parse_node_pin_registry,
    parse_workflow,
    validate_workflow_job_request,
    visible_to_role,
    workflow_record,
)


PARSER_WORKFLOW_REQUIRED_KEYS = {
    "id",
    "version",
    "display_name",
    "modality",
    "operation",
    "model_alias",
    "backend_policy",
    "output_mime_types",
    "input_schema",
    "output_schema",
    "resource_class",
    "dependencies",
    "limits",
}
PARSER_WORKFLOW_OPTIONAL_KEYS = {
    "description",
    "workflow_json",
    "visibility_roles",
    "runtime_policy",
    "presets",
    "comfyui_parameter_mappings",
    "runtime_parameter_mappings",
}
PARSER_NODE_PIN_KEYS = {
    "id",
    "commit",
    "repository_url",
    "display_name",
    "status",
    "approved_by",
    "approved_at",
    "dependency_lock_sha256",
    "allowed_route_prefixes",
    "notes",
}


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
        self.assertTrue(all(workflow.presets for workflow in workflows))

    def test_published_workflow_schema_matches_parser_and_seed_files(self) -> None:
        schema = json.loads((ROOT / "workflows" / "schemas" / "published-workflow.schema.json").read_text(encoding="utf-8"))
        properties = schema["properties"]

        self.assertTrue(PARSER_WORKFLOW_REQUIRED_KEYS.issubset(set(schema["required"])))
        self.assertEqual(set(properties), PARSER_WORKFLOW_REQUIRED_KEYS | PARSER_WORKFLOW_OPTIONAL_KEYS)
        self.assertEqual(set(properties["modality"]["enum"]), MODALITIES)
        self.assertEqual(set(properties["backend_policy"]["enum"]), BACKEND_POLICIES)
        self.assertEqual(set(properties["runtime_policy"]["enum"]), RUNTIME_POLICIES)
        self.assertEqual(set(properties["resource_class"]["enum"]), RESOURCE_CLASSES)
        self.assertEqual(set(properties["limits"]["properties"]), LIMIT_KEYS)
        self.assertEqual(set(properties["visibility_roles"]["items"]["enum"]), ROLES)
        self.assertEqual(set(properties["presets"]["items"]["required"]), {"id", "display_name", "values"})

        for path in sorted((ROOT / "workflows" / "approved").glob("*.json")):
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(set(payload) - set(properties), f"{path} contains keys not declared by the schema")
                self.assertTrue(PARSER_WORKFLOW_REQUIRED_KEYS.issubset(set(payload)))

    def test_approved_node_pin_schema_matches_parser_keys(self) -> None:
        schema = json.loads((ROOT / "workflows" / "schemas" / "approved-node-pins.schema.json").read_text(encoding="utf-8"))
        properties = schema["properties"]["nodes"]["items"]["properties"]

        self.assertEqual(set(properties), PARSER_NODE_PIN_KEYS)
        self.assertIn("allowed_route_prefixes", properties)

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
                        "allowed_route_prefixes": ["impact/wildcards"],
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
        self.assertEqual(by_id["comfyui-impact-pack"]["allowed_route_prefixes"], ["impact/wildcards"])

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
        for repository_url in (
            "https://github.com/example/%2e%2e/ComfyUI-Node",
            "https://github.com/example/safe%2FComfyUI-Node",
            "https://github.com/example/safe%5CComfyUI-Node",
            "https://github.com/example/%00ComfyUI-Node",
            "https://github.com/example/%ComfyUI-Node",
            "https://github.com/example/%2/ComfyUI-Node",
            "https://github.com/example/%zzComfyUI-Node",
        ):
            with self.subTest(repository_url=repository_url):
                with self.assertRaisesRegex(NodePinError, "path-control"):
                    parse_node_pin_registry(
                        {
                            "schema_version": 1,
                            "nodes": [
                                {
                                    "id": "comfyui-impact-pack",
                                    "repository_url": repository_url,
                                    "commit": "a" * 40,
                                }
                            ],
                        }
                    )

    def test_parse_node_pin_registry_validates_allowed_route_prefixes(self) -> None:
        commit = "a" * 40
        registry = parse_node_pin_registry(
            {
                "schema_version": 1,
                "nodes": [
                    {
                        "id": "comfyui-custom-api",
                        "repository_url": "https://github.com/example/comfyui-custom-api",
                        "commit": commit,
                        "dependency_lock_sha256": "b" * 64,
                        "allowed_route_prefixes": ["/Custom/API", "custom/api"],
                    }
                ],
            }
        )

        pin = registry[("comfyui-custom-api", commit)]
        self.assertEqual(pin.allowed_route_prefixes, ["custom/api"])
        self.assertEqual(pin.to_dict()["allowed_route_prefixes"], ["custom/api"])

        for prefix in (
            "",
            "../escape",
            "custom/%2e%2e/admin",
            "custom/safe%2Fadmin",
            "custom/safe%5Cadmin",
            "custom/%00admin",
            "custom/%",
            "custom/%2",
            "custom/%zz",
            "https://example.com/api",
            "custom api",
            "custom?install=1",
        ):
            with self.subTest(prefix=prefix):
                with self.assertRaises(NodePinError):
                    parse_node_pin_registry(
                        {
                            "schema_version": 1,
                            "nodes": [
                                {
                                    "id": "comfyui-custom-api",
                                    "repository_url": "https://github.com/example/comfyui-custom-api",
                                    "commit": commit,
                                    "dependency_lock_sha256": "b" * 64,
                                    "allowed_route_prefixes": [prefix],
                                }
                            ],
                        }
                    )

    def test_parse_node_pin_registry_requires_dependency_lock_for_approved_routes(self) -> None:
        commit = "a" * 40
        with self.assertRaisesRegex(NodePinError, "dependency_lock_sha256 is required"):
            parse_node_pin_registry(
                {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "comfyui-custom-api",
                            "repository_url": "https://github.com/example/comfyui-custom-api",
                            "commit": commit,
                            "allowed_route_prefixes": ["custom/api"],
                        }
                    ],
                }
            )

        registry = parse_node_pin_registry(
            {
                "schema_version": 1,
                "nodes": [
                    {
                        "id": "comfyui-custom-api",
                        "repository_url": "https://github.com/example/comfyui-custom-api",
                        "commit": commit,
                        "status": "disabled",
                        "allowed_route_prefixes": ["custom/api"],
                    }
                ],
            }
        )

        self.assertEqual(registry[("comfyui-custom-api", commit)].status, "disabled")

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

    def test_parse_workflow_accepts_safe_parameter_presets(self) -> None:
        workflow = parse_workflow(
            {
                **BASE_WORKFLOW,
                "input_schema": {
                    "type": "object",
                    "required": ["prompt"],
                    "properties": {
                        "prompt": {"type": "string", "minLength": 1, "maxLength": 120},
                        "steps": {"type": "integer", "minimum": 1, "maximum": 40},
                        "seed": {"type": "integer", "minimum": 0},
                    },
                    "additionalProperties": False,
                },
                "limits": {"max_steps": 40},
                "presets": [
                    {
                        "id": "balanced",
                        "display_name": "Balanced",
                        "description": "Safe default for a single local image job.",
                        "values": {"prompt": "clean local test", "steps": 20, "seed": 0},
                    }
                ],
            }
        )

        preset = workflow.presets[0]
        self.assertEqual(preset.id, "balanced")
        self.assertEqual(preset.values["steps"], 20)
        self.assertEqual(workflow.to_dict()["presets"][0]["display_name"], "Balanced")

    def test_parse_workflow_rejects_invalid_presets(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "input_schema property"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "presets": [{"id": "bad-field", "display_name": "Bad", "values": {"missing": 1}}],
                }
            )
        with self.assertRaisesRegex(WorkflowError, "media upload/base64"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "source_image": {"type": "string", "contentEncoding": "base64", "contentMediaType": "image/png"}
                        },
                    },
                    "presets": [{"id": "bad-media", "display_name": "Bad", "values": {"source_image": "aW1hZ2U="}}],
                }
            )
        with self.assertRaisesRegex(WorkflowError, "workflow limit max_steps=40"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "input_schema": {"type": "object", "properties": {"steps": {"type": "integer", "minimum": 1, "maximum": 80}}},
                    "limits": {"max_steps": 40},
                    "presets": [{"id": "too-many", "display_name": "Too Many", "values": {"steps": 41}}],
                }
            )
        with self.assertRaisesRegex(WorkflowError, "unsafe object key"):
            parse_workflow(
                {
                    **BASE_WORKFLOW,
                    "input_schema": {"type": "object", "properties": {"options": {"type": "object"}}},
                    "presets": [{"id": "unsafe", "display_name": "Unsafe", "values": {"options": {"__proto__": True}}}],
                }
            )

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
        for unsafe_path in (
            "inputs/client/%2e%2e/secret.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/safe%2Fsecret.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/safe%5Csecret.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/safe%3Ftoken.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/safe%23fragment.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/%00source.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/%/source.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/%2/source.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/%zz/source.png",
            "inputs/client/upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/%ffsource.png",
        ):
            with self.subTest(unsafe_path=unsafe_path):
                with self.assertRaisesRegex(WorkflowError, "path is invalid"):
                    validate_workflow_job_request(
                        workflow,
                        {**payload, "input": {**payload["input"], "parameters": {"source_image": {**base, "path": unsafe_path}, "transparent": True}}},
                    )
        with self.assertRaisesRegex(WorkflowError, "upload id"):
            validate_workflow_job_request(
                workflow,
                {
                    **payload,
                    "input": {
                        **payload["input"],
                        "parameters": {
                            "source_image": {**base, "path": "inputs/client/upload_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/source-source.png"},
                            "transparent": True,
                        },
                    },
                },
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
