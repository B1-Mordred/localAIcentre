from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
BACKEND_POLICIES = {"comfyui-only", "non-comfy-only", "either"}
RESOURCE_CLASSES = {"rtx3060-32gb", "requires-upgrade"}
DEPENDENCY_TYPES = {"model", "node", "runtime"}
ROLES = {"admin", "operator", "creator", "user", "service"}
LIMIT_KEYS = {"max_width", "max_height", "max_frames", "max_steps", "max_batch_size", "max_duration_seconds"}
MODALITIES = {"image", "video", "tts", "stt"}
RUNTIME_POLICIES = {"any", "non_comfy_only"}
MIME_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,127}$")
RUNTIME_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PARAMETER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
UPLOAD_ID_RE = re.compile(r"^upload_[a-f0-9]{32}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
GIT_COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
DEFAULT_MAX_INLINE_MEDIA_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_STAGED_MEDIA_BYTES = 256 * 1024 * 1024
LIMIT_TO_PARAMETER_NAMES = {
    "max_width": ("width",),
    "max_height": ("height",),
    "max_frames": ("frames",),
    "max_steps": ("steps",),
    "max_batch_size": ("batch_size", "batch"),
    "max_duration_seconds": ("duration_seconds", "max_seconds"),
}


class WorkflowError(ValueError):
    pass


class NodePinError(WorkflowError):
    pass


@dataclass(frozen=True)
class WorkflowDependency:
    type: str
    id: str
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value is not None}


@dataclass(frozen=True)
class ApprovedNodePin:
    id: str
    commit: str
    repository_url: str
    display_name: str | None = None
    status: str = "approved"
    approved_by: str | None = None
    approved_at: str | None = None
    dependency_lock_sha256: str | None = None
    allowed_route_prefixes: list[str] = field(default_factory=list)
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value is not None and value != []}


@dataclass(frozen=True)
class PublishedWorkflow:
    id: str
    version: str
    display_name: str
    modality: str
    operation: str
    model_alias: str
    backend_policy: str
    runtime_policy: str
    output_mime_types: list[str]
    workflow_json: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    resource_class: str
    dependencies: list[WorkflowDependency]
    limits: dict[str, int]
    comfyui_parameter_mappings: list[dict[str, Any]] = field(default_factory=list)
    runtime_parameter_mappings: list[dict[str, Any]] = field(default_factory=list)
    description: str | None = None
    visibility_roles: list[str] = field(default_factory=lambda: ["admin"])

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "modality": self.modality,
            "operation": self.operation,
            "model_alias": self.model_alias,
            "backend_policy": self.backend_policy,
            "runtime_policy": self.runtime_policy,
            "output_mime_types": list(self.output_mime_types),
            "workflow_json": self.workflow_json,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "resource_class": self.resource_class,
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
            "limits": dict(self.limits),
            "comfyui_parameter_mappings": [dict(mapping) for mapping in self.comfyui_parameter_mappings],
            "runtime_parameter_mappings": [dict(mapping) for mapping in self.runtime_parameter_mappings],
            "visibility_roles": list(self.visibility_roles),
        }
        return {key: value for key, value in data.items() if value is not None and value != []}


def parse_workflow(data: dict[str, Any], context: str = "workflow") -> PublishedWorkflow:
    required = {
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
    allowed = required | {
        "description",
        "workflow_json",
        "visibility_roles",
        "runtime_policy",
        "comfyui_parameter_mappings",
        "runtime_parameter_mappings",
    }
    extra = set(data) - allowed
    if extra:
        raise WorkflowError(f"{context} has unsupported keys: {', '.join(sorted(extra))}")
    missing = required - set(data)
    if missing:
        raise WorkflowError(f"{context} is missing required keys: {', '.join(sorted(missing))}")
    workflow_id = _id(_string(data, "id", context), f"{context}.id")
    backend_policy = _string(data, "backend_policy", context)
    if backend_policy not in BACKEND_POLICIES:
        raise WorkflowError(f"{context}.backend_policy is unsupported: {backend_policy}")
    modality = _string(data, "modality", context)
    if modality not in MODALITIES:
        raise WorkflowError(f"{context}.modality is unsupported: {modality}")
    operation = _id(_string(data, "operation", context), f"{context}.operation")
    model_alias = _id(_string(data, "model_alias", context), f"{context}.model_alias")
    runtime_policy = _string({"runtime_policy": data.get("runtime_policy", "any")}, "runtime_policy", context)
    if runtime_policy not in RUNTIME_POLICIES:
        raise WorkflowError(f"{context}.runtime_policy is unsupported: {runtime_policy}")
    if backend_policy == "comfyui-only" and runtime_policy == "non_comfy_only":
        raise WorkflowError(f"{context}.runtime_policy conflicts with comfyui-only backend_policy")
    resource_class = _string(data, "resource_class", context)
    if resource_class not in RESOURCE_CLASSES:
        raise WorkflowError(f"{context}.resource_class is unsupported: {resource_class}")
    workflow_json = _dict(data.get("workflow_json", {}), f"{context}.workflow_json")
    input_schema = _json_schema_object(_dict(data["input_schema"], f"{context}.input_schema"), f"{context}.input_schema")
    output_schema = _json_schema_object(_dict(data["output_schema"], f"{context}.output_schema"), f"{context}.output_schema")
    comfyui_parameter_mappings = _comfyui_parameter_mappings(
        data.get("comfyui_parameter_mappings", []),
        input_schema,
        f"{context}.comfyui_parameter_mappings",
    )
    runtime_parameter_mappings = _runtime_parameter_mappings(
        data.get("runtime_parameter_mappings", []),
        input_schema,
        f"{context}.runtime_parameter_mappings",
    )
    dependencies = _dependencies(data["dependencies"], f"{context}.dependencies")
    if not any(dependency.type == "model" and dependency.id == model_alias for dependency in dependencies):
        raise WorkflowError(f"{context}.model_alias must be declared as a model dependency")
    limits = _limits(data["limits"], f"{context}.limits")
    visibility_roles = _roles(data.get("visibility_roles", ["admin"]), f"{context}.visibility_roles")
    output_mime_types = _mime_types(data["output_mime_types"], f"{context}.output_mime_types")
    return PublishedWorkflow(
        id=workflow_id,
        version=_string(data, "version", context),
        display_name=_string(data, "display_name", context),
        description=_optional_string(data, "description", context),
        modality=modality,
        operation=operation,
        model_alias=model_alias,
        backend_policy=backend_policy,
        runtime_policy=runtime_policy,
        output_mime_types=output_mime_types,
        workflow_json=workflow_json,
        input_schema=input_schema,
        output_schema=output_schema,
        resource_class=resource_class,
        dependencies=dependencies,
        limits=limits,
        comfyui_parameter_mappings=comfyui_parameter_mappings,
        runtime_parameter_mappings=runtime_parameter_mappings,
        visibility_roles=visibility_roles,
    )


def load_workflows(directory: Path) -> list[PublishedWorkflow]:
    if not directory.exists():
        return []
    workflows: list[PublishedWorkflow] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            workflow = parse_workflow(json.loads(path.read_text(encoding="utf-8")), str(path))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"invalid workflow file {path}: {exc}") from exc
        key = (workflow.id, workflow.version)
        if key in seen:
            raise WorkflowError(f"duplicate workflow version: {workflow.id}@{workflow.version}")
        seen.add(key)
        workflows.append(workflow)
    return workflows


def load_node_pins(path: Path) -> dict[tuple[str, str], ApprovedNodePin]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NodePinError(f"invalid approved node pin registry {path}: {exc}") from exc
    return parse_node_pin_registry(data, str(path))


def parse_node_pin_registry(data: dict[str, Any], context: str = "node_pin_registry") -> dict[tuple[str, str], ApprovedNodePin]:
    if not isinstance(data, dict):
        raise NodePinError(f"{context} must be an object")
    extra = set(data) - {"schema_version", "nodes"}
    if extra:
        raise NodePinError(f"{context} has unsupported keys: {', '.join(sorted(extra))}")
    schema_version = data.get("schema_version")
    if schema_version != 1:
        raise NodePinError(f"{context}.schema_version must be 1")
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        raise NodePinError(f"{context}.nodes must be a list")
    registry: dict[tuple[str, str], ApprovedNodePin] = {}
    for index, item in enumerate(nodes):
        pin = _node_pin(item, f"{context}.nodes[{index}]")
        key = (pin.id, pin.commit)
        if key in registry:
            raise NodePinError(f"{context}.nodes[{index}] duplicates node pin {pin.id}@{pin.commit}")
        registry[key] = pin
    return registry


def dependency_report(
    workflow: PublishedWorkflow,
    model_lookup: Callable[[str], dict[str, Any] | None],
    available_runtimes: set[str],
    node_pin_lookup: Callable[[str, str | None], ApprovedNodePin | dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    dependencies: list[dict[str, Any]] = []
    ready = True
    for dependency in workflow.dependencies:
        item = dependency.to_dict()
        if dependency.type == "model":
            model = model_lookup(dependency.id)
            if model is None:
                item.update({"status": "missing", "ready": False, "reason": "model or alias is unknown"})
            elif model.get("resolved_model") is None and model.get("status") in {"uninstalled", "cpu-placeholder"}:
                item.update({"status": model.get("status"), "ready": False, "reason": "model alias is not backed by installable production weights"})
            else:
                item.update({"status": model.get("status", "installed"), "ready": True})
        elif dependency.type == "runtime":
            if dependency.id in available_runtimes:
                item.update({"status": "available", "ready": True})
            else:
                item.update({"status": "missing", "ready": False, "reason": "runtime adapter is not configured"})
        else:
            node_status = node_dependency_status(dependency, node_pin_lookup)
            item.update(node_status)
        ready = ready and bool(item["ready"])
        dependencies.append(item)
    return {"ready": ready, "dependencies": dependencies}


def node_dependency_status(
    dependency: WorkflowDependency,
    node_pin_lookup: Callable[[str, str | None], ApprovedNodePin | dict[str, Any] | None] | None,
) -> dict[str, Any]:
    if dependency.version is None:
        return {"status": "unpinned", "ready": False, "reason": "custom node dependency must pin an approved commit"}
    if node_pin_lookup is None:
        return {"status": "unapproved", "ready": False, "reason": "custom node pin registry is not configured"}
    pin = node_pin_lookup(dependency.id, dependency.version)
    if pin is None:
        return {"status": "unapproved", "ready": False, "reason": "custom node commit is not in the approved pin registry"}
    pin_record = pin.to_dict() if isinstance(pin, ApprovedNodePin) else dict(pin)
    status = pin_record.get("status", "approved")
    result = {
        "status": status,
        "ready": status == "approved",
        "repository_url": pin_record.get("repository_url"),
        "commit": pin_record.get("commit"),
        "approved_by": pin_record.get("approved_by"),
        "approved_at": pin_record.get("approved_at"),
        "dependency_lock_sha256": pin_record.get("dependency_lock_sha256"),
        "allowed_route_prefixes": pin_record.get("allowed_route_prefixes"),
    }
    if status != "approved":
        result["reason"] = f"custom node pin is {status}, not approved"
    return {key: value for key, value in result.items() if value is not None}


def workflow_record(
    workflow: PublishedWorkflow,
    model_lookup: Callable[[str], dict[str, Any] | None],
    available_runtimes: set[str],
    node_pin_lookup: Callable[[str, str | None], ApprovedNodePin | dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    report = dependency_report(workflow, model_lookup, available_runtimes, node_pin_lookup)
    return {
        **workflow.to_dict(),
        "status": "published" if report["ready"] else "needs_dependencies",
        "dependency_status": report,
        "publishable": report["ready"],
    }


def visible_to_role(record: dict[str, Any], role: str, scopes: frozenset[str]) -> bool:
    if "*" in scopes:
        return True
    return role in set(record.get("visibility_roles") or [])


def validate_workflow_job_request(
    workflow: dict[str, Any],
    request_payload: dict[str, Any],
    *,
    max_inline_media_bytes: int = DEFAULT_MAX_INLINE_MEDIA_BYTES,
    max_staged_media_bytes: int = DEFAULT_MAX_STAGED_MEDIA_BYTES,
) -> dict[str, Any]:
    input_payload = _dict(request_payload.get("input"), "media_job.input")
    workflow_id = _string(input_payload, "workflow_id", "media_job.input")
    workflow_version = _string(input_payload, "workflow_version", "media_job.input")
    if workflow_id != workflow.get("id") or workflow_version != workflow.get("version"):
        raise WorkflowError("media job workflow_id/workflow_version do not match the selected published workflow")
    expected = {
        "modality": workflow.get("modality"),
        "operation": workflow.get("operation"),
        "model": workflow.get("model_alias"),
        "runtime_policy": workflow.get("runtime_policy", "any"),
    }
    for field, expected_value in expected.items():
        if request_payload.get(field) != expected_value:
            raise WorkflowError(f"media job {field} must match workflow value {expected_value}")
    parameters = _dict(input_payload.get("parameters"), "media_job.input.parameters")
    validate_input_parameters(
        workflow.get("input_schema") or {},
        parameters,
        workflow.get("limits") or {},
        max_inline_media_bytes=max_inline_media_bytes,
        max_staged_media_bytes=max_staged_media_bytes,
    )
    return parameters


def validate_input_parameters(
    input_schema: dict[str, Any],
    parameters: dict[str, Any],
    limits: dict[str, int],
    *,
    max_inline_media_bytes: int = DEFAULT_MAX_INLINE_MEDIA_BYTES,
    max_staged_media_bytes: int = DEFAULT_MAX_STAGED_MEDIA_BYTES,
) -> None:
    schema = _json_schema_object(_dict(input_schema, "workflow.input_schema"), "workflow.input_schema")
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise WorkflowError("workflow.input_schema.properties must be an object")
    required = schema.get("required") or []
    if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
        raise WorkflowError("workflow.input_schema.required must be a list of strings")
    missing = [name for name in required if name not in parameters or parameters[name] is None or parameters[name] == ""]
    if missing:
        raise WorkflowError(f"media job parameters are missing required fields: {', '.join(sorted(missing))}")
    if schema.get("additionalProperties") is False:
        extra = sorted(set(parameters) - set(properties))
        if extra:
            raise WorkflowError(f"media job parameters include unsupported fields: {', '.join(extra)}")
    for name, value in parameters.items():
        if name in properties:
            _validate_parameter_value(
                name,
                value,
                _dict(properties[name], f"workflow.input_schema.properties.{name}"),
                max_inline_media_bytes,
                max_staged_media_bytes,
            )
    _enforce_limits(parameters, limits)


def _string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise WorkflowError(f"{context}.{key} must be a non-empty string")
    return value


def _optional_string(data: dict[str, Any], key: str, context: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    return _string(data, key, context)


def _id(value: str, context: str) -> str:
    if not ID_RE.match(value):
        raise WorkflowError(f"{context} has invalid identifier: {value}")
    return value


def _dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(f"{context} must be an object")
    return dict(value)


def _json_schema_object(value: dict[str, Any], context: str) -> dict[str, Any]:
    schema_type = value.get("type")
    if schema_type is not None and schema_type != "object":
        raise WorkflowError(f"{context}.type must be object when present")
    properties = value.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise WorkflowError(f"{context}.properties must be an object")
    return value


def _dependencies(value: Any, context: str) -> list[WorkflowDependency]:
    if not isinstance(value, list):
        raise WorkflowError(f"{context} must be a list")
    parsed: list[WorkflowDependency] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise WorkflowError(f"{context}[{index}] must be an object")
        extra = set(item) - {"type", "id", "version"}
        if extra:
            raise WorkflowError(f"{context}[{index}] has unsupported keys: {', '.join(sorted(extra))}")
        dependency_type = _string(item, "type", f"{context}[{index}]")
        if dependency_type not in DEPENDENCY_TYPES:
            raise WorkflowError(f"{context}[{index}].type is unsupported: {dependency_type}")
        version = _optional_string(item, "version", f"{context}[{index}]")
        if dependency_type == "node":
            if version is None:
                raise WorkflowError(f"{context}[{index}].version is required for custom node dependencies")
            if not GIT_COMMIT_RE.match(version):
                raise WorkflowError(f"{context}[{index}].version must be a 40 character lowercase git commit")
        parsed.append(
            WorkflowDependency(
                type=dependency_type,
                id=_id(_string(item, "id", f"{context}[{index}]"), f"{context}[{index}].id"),
                version=version,
            )
        )
    return parsed


def _node_pin(value: Any, context: str) -> ApprovedNodePin:
    if not isinstance(value, dict):
        raise NodePinError(f"{context} must be an object")
    allowed = {
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
    extra = set(value) - allowed
    if extra:
        raise NodePinError(f"{context} has unsupported keys: {', '.join(sorted(extra))}")
    missing = {"id", "commit", "repository_url"} - set(value)
    if missing:
        raise NodePinError(f"{context} is missing required keys: {', '.join(sorted(missing))}")
    node_id = _id(_string(value, "id", context), f"{context}.id")
    commit = _string(value, "commit", context)
    if not GIT_COMMIT_RE.match(commit):
        raise NodePinError(f"{context}.commit must be a 40 character lowercase git commit")
    repository_url = _https_git_url(_string(value, "repository_url", context), f"{context}.repository_url")
    status = value.get("status", "approved")
    if status not in {"approved", "disabled", "superseded"}:
        raise NodePinError(f"{context}.status is unsupported: {status}")
    dependency_lock_sha256 = value.get("dependency_lock_sha256")
    if dependency_lock_sha256 is not None:
        if not isinstance(dependency_lock_sha256, str) or not SHA256_RE.match(dependency_lock_sha256):
            raise NodePinError(f"{context}.dependency_lock_sha256 must be a lowercase SHA-256 digest")
    allowed_route_prefixes = _route_prefixes(value.get("allowed_route_prefixes", []), f"{context}.allowed_route_prefixes")
    return ApprovedNodePin(
        id=node_id,
        commit=commit,
        repository_url=repository_url,
        display_name=_optional_string(value, "display_name", context),
        status=status,
        approved_by=_optional_string(value, "approved_by", context),
        approved_at=_optional_string(value, "approved_at", context),
        dependency_lock_sha256=dependency_lock_sha256,
        allowed_route_prefixes=allowed_route_prefixes,
        notes=_optional_string(value, "notes", context),
    )


def _route_prefixes(value: Any, context: str) -> list[str]:
    if not isinstance(value, list):
        raise NodePinError(f"{context} must be a list")
    prefixes: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise NodePinError(f"{context}[{index}] must be a string")
        prefix = _route_prefix(item, f"{context}[{index}]")
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def _route_prefix(value: str, context: str) -> str:
    if value != value.strip() or not value.strip():
        raise NodePinError(f"{context} must be a non-empty relative route prefix")
    if "\\" in value or "\x00" in value or any(ch.isspace() for ch in value):
        raise NodePinError(f"{context} contains unsafe route characters")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise NodePinError(f"{context} must be a relative route prefix without query or fragment")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise NodePinError(f"{context} must not contain empty or traversal path segments")
    return "/".join(parts).lower()


def _https_git_url(value: str, context: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise NodePinError(f"{context} must be an HTTPS URL")
    if parsed.username or parsed.password:
        raise NodePinError(f"{context} must not include credentials")
    if parsed.query or parsed.fragment:
        raise NodePinError(f"{context} must not include query strings or fragments")
    return value


def _limits(value: Any, context: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise WorkflowError(f"{context} must be an object")
    extra = set(value) - LIMIT_KEYS
    if extra:
        raise WorkflowError(f"{context} has unsupported keys: {', '.join(sorted(extra))}")
    parsed: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(item, int) or item < 1:
            raise WorkflowError(f"{context}.{key} must be a positive integer")
        parsed[key] = item
    return parsed


def _comfyui_parameter_mappings(value: Any, input_schema: dict[str, Any], context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WorkflowError(f"{context} must be a list")
    properties = input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    parsed: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for index, item in enumerate(value):
        item_context = f"{context}[{index}]"
        if not isinstance(item, dict):
            raise WorkflowError(f"{item_context} must be an object")
        extra = set(item) - {"parameter", "path"}
        if extra:
            raise WorkflowError(f"{item_context} has unsupported keys: {', '.join(sorted(extra))}")
        parameter = _string(item, "parameter", item_context)
        if parameter not in properties:
            raise WorkflowError(f"{item_context}.parameter must reference an input_schema property")
        path = item.get("path")
        if not isinstance(path, list) or not path or any(not isinstance(segment, str) or not segment for segment in path):
            raise WorkflowError(f"{item_context}.path must be a non-empty list of strings")
        if len(path) > 12:
            raise WorkflowError(f"{item_context}.path is too deep")
        blocked = {"__proto__", "prototype", "constructor"}
        if any(segment in blocked for segment in path):
            raise WorkflowError(f"{item_context}.path contains an unsafe segment")
        path_tuple = tuple(path)
        if path_tuple in seen_paths:
            raise WorkflowError(f"{item_context}.path duplicates another mapping")
        seen_paths.add(path_tuple)
        parsed.append({"parameter": parameter, "path": list(path_tuple)})
    return parsed


def _runtime_parameter_mappings(value: Any, input_schema: dict[str, Any], context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WorkflowError(f"{context} must be a list")
    properties = input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    parsed: list[dict[str, Any]] = []
    seen_targets: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        item_context = f"{context}[{index}]"
        if not isinstance(item, dict):
            raise WorkflowError(f"{item_context} must be an object")
        extra = set(item) - {"runtime", "parameter", "target", "keep_source"}
        if extra:
            raise WorkflowError(f"{item_context} has unsupported keys: {', '.join(sorted(extra))}")
        runtime = _string(item, "runtime", item_context)
        if not RUNTIME_NAME_RE.match(runtime):
            raise WorkflowError(f"{item_context}.runtime has invalid runtime name")
        parameter = _string(item, "parameter", item_context)
        if parameter not in properties:
            raise WorkflowError(f"{item_context}.parameter must reference an input_schema property")
        target = _string(item, "target", item_context)
        if not PARAMETER_NAME_RE.match(target):
            raise WorkflowError(f"{item_context}.target has invalid adapter parameter name")
        keep_source = item.get("keep_source", False)
        if not isinstance(keep_source, bool):
            raise WorkflowError(f"{item_context}.keep_source must be a boolean")
        target_key = (runtime, target)
        if target_key in seen_targets:
            raise WorkflowError(f"{item_context}.target duplicates another mapping for runtime {runtime}")
        seen_targets.add(target_key)
        parsed.append({"runtime": runtime, "parameter": parameter, "target": target, "keep_source": keep_source})
    return parsed


def _roles(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise WorkflowError(f"{context} must be a list of strings")
    if len(value) != len(set(value)):
        raise WorkflowError(f"{context} items must be unique")
    unknown = set(value) - ROLES
    if unknown:
        raise WorkflowError(f"{context} has unsupported roles: {', '.join(sorted(unknown))}")
    return list(value)


def _mime_types(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise WorkflowError(f"{context} must be a non-empty list of MIME type strings")
    if len(value) != len(set(value)):
        raise WorkflowError(f"{context} items must be unique")
    invalid = [item for item in value if not MIME_RE.match(item)]
    if invalid:
        raise WorkflowError(f"{context} has invalid MIME types: {', '.join(invalid)}")
    return list(value)


def _schema_types(schema: dict[str, Any], context: str) -> set[str]:
    raw_type = schema.get("type", "string")
    if isinstance(raw_type, str):
        return {raw_type}
    if isinstance(raw_type, list) and raw_type and all(isinstance(item, str) and item for item in raw_type):
        return set(raw_type)
    raise WorkflowError(f"{context}.type must be a string or list of strings")


def _validate_parameter_value(name: str, value: Any, schema: dict[str, Any], max_inline_media_bytes: int, max_staged_media_bytes: int) -> None:
    context = f"media job parameter {name}"
    allowed_types = _schema_types(schema, f"workflow.input_schema.properties.{name}")
    if value is None:
        if "null" in allowed_types:
            return
        raise WorkflowError(f"{context} must not be null")
    if schema.get("contentEncoding") == "base64" and isinstance(value, dict):
        _validate_staged_media_reference(name, value, schema, max_staged_media_bytes)
        return
    if not any(_value_matches_schema_type(value, schema_type) for schema_type in allowed_types if schema_type != "null"):
        raise WorkflowError(f"{context} has the wrong type")
    enum_values = schema.get("enum")
    if enum_values is not None:
        if not isinstance(enum_values, list):
            raise WorkflowError(f"workflow.input_schema.properties.{name}.enum must be a list")
        if value not in enum_values:
            raise WorkflowError(f"{context} must be one of: {', '.join(str(item) for item in enum_values)}")
    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if min_length is not None and len(value) < _nonnegative_int(min_length, f"workflow.input_schema.properties.{name}.minLength"):
            raise WorkflowError(f"{context} is shorter than minLength")
        if max_length is not None and len(value) > _nonnegative_int(max_length, f"workflow.input_schema.properties.{name}.maxLength"):
            raise WorkflowError(f"{context} exceeds maxLength")
        if schema.get("contentEncoding") == "base64":
            _validate_base64_media(name, value, max_inline_media_bytes)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < _number_value(minimum, f"workflow.input_schema.properties.{name}.minimum"):
            raise WorkflowError(f"{context} is below minimum")
        if maximum is not None and value > _number_value(maximum, f"workflow.input_schema.properties.{name}.maximum"):
            raise WorkflowError(f"{context} exceeds maximum")


def _value_matches_schema_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    return False


def _validate_base64_media(name: str, value: str, max_inline_media_bytes: int) -> None:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise WorkflowError(f"media job parameter {name} must be base64 encoded") from exc
    if len(decoded) > max_inline_media_bytes:
        raise WorkflowError(f"media job parameter {name} exceeds inline media limit")


def _validate_staged_media_reference(name: str, value: dict[str, Any], schema: dict[str, Any], max_staged_media_bytes: int) -> None:
    context = f"media job parameter {name}"
    if value.get("source") != "staged_upload":
        raise WorkflowError(f"{context} staged reference has unsupported source")
    upload_id = value.get("id")
    if not isinstance(upload_id, str) or not UPLOAD_ID_RE.match(upload_id):
        raise WorkflowError(f"{context} staged reference has invalid id")
    path = value.get("path")
    if not isinstance(path, str) or not path.startswith("inputs/"):
        raise WorkflowError(f"{context} staged reference path is invalid")
    if any(part in {"", ".", ".."} for part in path.replace("\\", "/").split("/")):
        raise WorkflowError(f"{context} staged reference path is invalid")
    mime_type = value.get("mime_type")
    if not isinstance(mime_type, str) or not MIME_RE.match(mime_type):
        raise WorkflowError(f"{context} staged reference has invalid MIME type")
    expected_mime = schema.get("contentMediaType")
    if isinstance(expected_mime, str) and expected_mime and mime_type != expected_mime:
        raise WorkflowError(f"{context} staged reference MIME type must be {expected_mime}")
    bytes_value = value.get("bytes")
    if not isinstance(bytes_value, int) or bytes_value < 1:
        raise WorkflowError(f"{context} staged reference has invalid byte count")
    if bytes_value > max_staged_media_bytes:
        raise WorkflowError(f"{context} staged reference exceeds staged media limit")
    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.match(sha256):
        raise WorkflowError(f"{context} staged reference has invalid sha256")
    kind = value.get("kind")
    expected_kind = expected_mime.split("/", 1)[0] if isinstance(expected_mime, str) and "/" in expected_mime else mime_type.split("/", 1)[0]
    if not isinstance(kind, str) or kind != expected_kind:
        raise WorkflowError(f"{context} staged reference kind must be {expected_kind}")


def _enforce_limits(parameters: dict[str, Any], limits: dict[str, int]) -> None:
    for limit_key, parameter_names in LIMIT_TO_PARAMETER_NAMES.items():
        limit = limits.get(limit_key)
        if limit is None:
            continue
        for parameter_name in parameter_names:
            value = parameters.get(parameter_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > limit:
                raise WorkflowError(f"media job parameter {parameter_name} exceeds workflow limit {limit_key}={limit}")


def _nonnegative_int(value: Any, context: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise WorkflowError(f"{context} must be a non-negative integer")
    return value


def _number_value(value: Any, context: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WorkflowError(f"{context} must be a number")
    return float(value)
