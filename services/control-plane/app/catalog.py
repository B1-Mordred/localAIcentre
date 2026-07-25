from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from .scheduler import AdmissionDecision, CpuResidencyDecision, ResourceEstimate, ResourcePolicy, classify_cpu_residency, classify_resource_fit


RUNTIME_NAMES = {"localai", "comfyui", "voicebox", "audio-cpu", "openai-compatible", "generic-http"}
MODALITIES = {"llm", "vlm", "embedding", "tts", "stt", "image", "video", "workflow"}
EXECUTION_MODES = {"hosted-inference", "downloadable", "network-share"}
INSTALLATION_STATUSES = {"available", "installed", "quarantined", "failed"}
ROLES = {"admin", "operator", "creator", "user", "service"}
REDISTRIBUTION_POLICIES = {"downloadable", "inference-only", "restricted"}
SOURCE_TYPES = {"catalog", "huggingface", "direct-url", "upload"}
DEPRECATION_STATUSES = {"active", "deprecated", "replaced", "removed"}
MEASUREMENT_SCHEMA = "b1-ai-hub-model-measurements/v1"
MEASUREMENT_RUN_STATUSES = {"ok", "warning", "failed", "skipped", "unconfirmed"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:$")
BAD_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")
OPERATION_ALIASES: dict[str, dict[str, set[str]]] = {
    "llm": {
        "chat": {"chat", "chat-completion", "chat-completions", "completion", "completions", "responses", "text-generation"},
    },
    "vlm": {
        "chat": {"chat", "chat-completion", "chat-completions", "completion", "completions", "responses", "vision", "vision-analysis"},
    },
    "embedding": {
        "embedding": {"embedding", "embeddings"},
    },
    "tts": {
        "text-to-speech": {"audio-speech", "speech", "text-to-speech", "tts"},
        "voice-cloning": {"clone", "voice-clone", "voice-cloning"},
    },
    "stt": {
        "transcription": {"audio-transcription", "audio-transcriptions", "speech-to-text", "stt", "transcription", "transcriptions"},
    },
    "image": {
        "image-generation": {"generation", "image-generation", "text-to-image"},
        "image-edit": {"edit", "image-edit", "image-to-image", "inpaint", "inpainting", "outpaint", "outpainting", "inpainting-outpainting"},
        "background-removal": {"background-removal", "remove-background"},
        "upscaling": {"upscale", "upscaling", "image-upscale"},
    },
    "video": {
        "video-generation": {"generation", "text-to-video", "video-generation"},
        "image-to-video": {"image-to-video", "image-video", "video-image"},
        "frame-interpolation": {"frame-interpolation", "interpolation"},
    },
    "workflow": {
        "workflow": {"comfyui-prompt", "native-workflow", "workflow"},
    },
}


class CatalogError(ValueError):
    pass


def normalize_operation(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def canonical_operation(operation: str, modality: str) -> str | None:
    normalized = normalize_operation(operation)
    for canonical, aliases in OPERATION_ALIASES.get(modality, {}).items():
        if normalized == canonical or normalized in aliases:
            return canonical
    return None


def operation_alias_set(operation: str, modality: str) -> set[str]:
    canonical = canonical_operation(operation, modality)
    if canonical is None:
        return {normalize_operation(operation)}
    return OPERATION_ALIASES.get(modality, {}).get(canonical, {canonical}) | {canonical}


def operation_is_supported(requested: str, supported: list[str] | tuple[str, ...], modality: str) -> bool:
    if not supported:
        return True
    requested_group = operation_alias_set(requested, modality)
    return any(requested_group & operation_alias_set(operation, modality) for operation in supported)


def supported_operations_for_modality(modality: str) -> list[str]:
    return sorted(OPERATION_ALIASES.get(modality, {}))


@dataclass(frozen=True)
class ManifestSource:
    type: str
    url: str
    revision: str
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _without_none(asdict(self))


@dataclass(frozen=True)
class ManifestFile:
    path: str
    sha256: str
    size_bytes: int
    format: str | None = None
    quantization: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _without_none(asdict(self))


@dataclass(frozen=True)
class ModelResourceEstimate:
    vram_gib: float
    ram_gib: float
    disk_gib: float
    context_tokens: int | None = None
    max_resolution: str | None = None
    max_frames: int | None = None

    def to_scheduler_estimate(self, requires_gpu: bool) -> ResourceEstimate:
        return ResourceEstimate(
            vram_gib=self.vram_gib,
            ram_gib=self.ram_gib,
            disk_gib=self.disk_gib,
            requires_gpu=requires_gpu,
        )

    def to_dict(self) -> dict[str, Any]:
        return _without_none(asdict(self))


@dataclass(frozen=True)
class ModelLicense:
    name: str
    redistribution: str
    url: str | None = None
    attribution: str | None = None
    acceptance_required: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return _without_none(asdict(self))


@dataclass(frozen=True)
class ModelCompanionFile:
    path: str
    required: bool = True
    description: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    format: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _without_none(asdict(self))


@dataclass(frozen=True)
class ModelPermissions:
    visible_to: list[str] = field(default_factory=list)
    installable_by: list[str] = field(default_factory=list)
    downloadable_by: list[str] = field(default_factory=list)
    inference_roles: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value}


@dataclass(frozen=True)
class ModelDeprecation:
    status: str = "active"
    deprecated_at: str | None = None
    message: str | None = None
    replacement_model: str | None = None
    replacement_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = _without_none(asdict(self))
        if data == {"status": "active"}:
            return {}
        return data


@dataclass(frozen=True)
class ModelManifest:
    id: str
    version: str
    display_name: str
    modality: str
    operations: list[str]
    source: ManifestSource
    files: list[ManifestFile]
    runtimes: list[str]
    preferred_runtime: str
    resource_estimate: ModelResourceEstimate
    license: ModelLicense
    execution_modes: list[str]
    installation_status: str = "installed"
    description: str | None = None
    aliases: list[str] = field(default_factory=list)
    visibility_roles: list[str] = field(default_factory=list)
    permissions: ModelPermissions = field(default_factory=ModelPermissions)
    runtime_adapter_versions: dict[str, str] = field(default_factory=dict)
    companion_files: list[ModelCompanionFile] = field(default_factory=list)
    deprecation: ModelDeprecation = field(default_factory=ModelDeprecation)
    measurements: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        permissions = self.permissions.to_dict()
        deprecation = self.deprecation.to_dict()
        return _without_none(
            {
                "id": self.id,
                "version": self.version,
                "display_name": self.display_name,
                "description": self.description,
                "modality": self.modality,
                "operations": list(self.operations),
                "source": self.source.to_dict(),
                "files": [item.to_dict() for item in self.files],
                "runtimes": list(self.runtimes),
                "preferred_runtime": self.preferred_runtime,
                "resource_estimate": self.resource_estimate.to_dict(),
                "license": self.license.to_dict(),
                "execution_modes": list(self.execution_modes),
                "installation_status": self.installation_status,
                "aliases": list(self.aliases),
                "visibility_roles": list(self.visibility_roles),
                "permissions": permissions if permissions else None,
                "runtime_adapter_versions": dict(self.runtime_adapter_versions) if self.runtime_adapter_versions else None,
                "companion_files": [item.to_dict() for item in self.companion_files] if self.companion_files else None,
                "deprecation": deprecation if deprecation else None,
                "measurements": dict(self.measurements) if self.measurements else None,
            }
        )


@dataclass(frozen=True)
class AliasDefinition:
    alias: str
    modality: str
    preferred_runtime: str
    status: str = "uninstalled"
    enabled: bool = True
    preferred_runtime_override: str | None = None
    idle_timeout_seconds: int | None = None
    visibility_roles: tuple[str, ...] = ()
    policy_source: str = "seed"
    notes: str = ""


@dataclass(frozen=True)
class CatalogAlias:
    alias: AliasDefinition
    manifest: ModelManifest | None
    decision: AdmissionDecision
    cpu_residency: CpuResidencyDecision

    @property
    def status(self) -> str:
        if not self.alias.enabled:
            return "disabled"
        if self.manifest is None:
            return self.alias.status
        if self.alias.status == "uninstalled":
            return "installed"
        return self.alias.status

    @property
    def preferred_runtime(self) -> str:
        if self.alias.preferred_runtime_override and (self.manifest is None or self.alias.preferred_runtime_override in self.manifest.runtimes):
            return self.alias.preferred_runtime_override
        return self.manifest.preferred_runtime if self.manifest else self.alias.preferred_runtime

    @property
    def runtimes(self) -> list[str]:
        return list(self.manifest.runtimes) if self.manifest else [self.preferred_runtime]

    @property
    def operations(self) -> list[str]:
        return list(self.manifest.operations) if self.manifest else []

    @property
    def requires_gpu(self) -> bool:
        return self.preferred_runtime != "audio-cpu" and (self.manifest is None or self.manifest.resource_estimate.vram_gib > 0)

    def to_openai_model(self) -> dict[str, Any]:
        resolved = None
        if self.manifest is not None:
            resolved = {
                "id": self.manifest.id,
                "version": self.manifest.version,
                "display_name": self.manifest.display_name,
            }
        return {
            "id": self.alias.alias,
            "object": "model",
            "owned_by": "b1-ai-hub",
            "root": self.manifest.id if self.manifest else self.alias.alias,
            "parent": "",
            "modality": self.alias.modality,
            "status": self.status,
            "enabled": self.alias.enabled,
            "preferred_runtime": self.preferred_runtime,
            "runtimes": self.runtimes,
            "operations": self.operations,
            "resource_label": self.decision.label,
            "resource_decision": asdict(self.decision),
            "resolved_model": resolved,
            "cpu_resident_candidate": self.cpu_residency.candidate,
            "cpu_resident_allowed": self.cpu_residency.allowed,
            "cpu_resident_reason": self.cpu_residency.reason,
            "preferred_runtime_override": self.alias.preferred_runtime_override,
            "idle_timeout_seconds": self.alias.idle_timeout_seconds,
            "visibility_roles": list(self.alias.visibility_roles),
            "alias_policy_source": self.alias.policy_source,
            "notes": self.alias.notes,
        }


class ModelCatalog:
    def __init__(self, aliases: list[AliasDefinition], manifests: list[ModelManifest], policy: ResourcePolicy) -> None:
        self.policy = policy
        self.aliases_by_id = {alias.alias: alias for alias in aliases}
        if len(self.aliases_by_id) != len(aliases):
            raise CatalogError("duplicate alias definitions")

        self.manifests_by_id: dict[str, list[ModelManifest]] = {}
        manifest_by_alias: dict[str, ModelManifest] = {}
        for manifest in manifests:
            self.manifests_by_id.setdefault(manifest.id, []).append(manifest)
            if manifest.installation_status != "installed":
                continue
            for alias in manifest.aliases:
                if alias in manifest_by_alias:
                    raise CatalogError(f"duplicate manifest alias: {alias}")
                manifest_by_alias[alias] = manifest

        self.aliases: dict[str, CatalogAlias] = {}
        for alias in aliases:
            manifest = manifest_by_alias.get(alias.alias)
            if manifest is not None and manifest.modality != alias.modality:
                raise CatalogError(f"manifest {manifest.id} modality {manifest.modality} does not match alias {alias.alias} modality {alias.modality}")
            self.aliases[alias.alias] = CatalogAlias(
                alias=alias,
                manifest=manifest,
                decision=self._decision_for(manifest),
                cpu_residency=self._cpu_residency_for(alias, manifest),
            )

    def _decision_for(self, manifest: ModelManifest | None) -> AdmissionDecision:
        if manifest is None:
            return AdmissionDecision(False, "uninstalled", "alias has no installed model manifest")
        estimate = manifest.resource_estimate.to_scheduler_estimate(requires_gpu=manifest.preferred_runtime != "audio-cpu")
        return classify_resource_fit(self.policy, estimate)

    def _cpu_residency_for(self, alias: AliasDefinition, manifest: ModelManifest | None) -> CpuResidencyDecision:
        preferred_runtime = alias.preferred_runtime_override if alias.preferred_runtime_override else alias.preferred_runtime
        if manifest is not None:
            preferred_runtime = manifest.preferred_runtime
            if alias.preferred_runtime_override and alias.preferred_runtime_override in manifest.runtimes:
                preferred_runtime = alias.preferred_runtime_override
        requires_gpu = preferred_runtime != "audio-cpu"
        estimate = (
            manifest.resource_estimate.to_scheduler_estimate(requires_gpu=requires_gpu)
            if manifest is not None
            else ResourceEstimate(vram_gib=0.0, ram_gib=0.0, disk_gib=0.0, requires_gpu=requires_gpu)
        )
        return classify_cpu_residency(self.policy, alias.alias, estimate)

    def list_aliases(self) -> list[CatalogAlias]:
        return [self.aliases[name] for name in sorted(self.aliases)]

    def list_manifests(self) -> list[ModelManifest]:
        return sorted((manifest for manifests in self.manifests_by_id.values() for manifest in manifests), key=lambda item: (item.id, item.version))

    def get_alias(self, alias_id: str) -> CatalogAlias | None:
        return self.aliases.get(alias_id)

    def require_alias(self, alias_id: str) -> CatalogAlias:
        alias = self.get_alias(alias_id)
        if alias is None:
            raise CatalogError(f"unknown model alias: {alias_id}")
        return alias

    def get_manifest(self, model_id: str) -> ModelManifest | None:
        versions = self.manifests_by_id.get(model_id, [])
        if not versions:
            return None
        return sorted(versions, key=lambda item: item.version, reverse=True)[0]

    def manifest_record(self, manifest: ModelManifest) -> dict[str, Any]:
        estimate = manifest.resource_estimate.to_scheduler_estimate(requires_gpu=manifest.preferred_runtime != "audio-cpu")
        decision = classify_resource_fit(self.policy, estimate)
        return {
            **manifest.to_dict(),
            "status": manifest.installation_status,
            "resource_label": decision.label,
            "resource_decision": asdict(decision),
            "downloadable": (
                manifest.installation_status == "installed"
                and manifest.license.redistribution == "downloadable"
                and "downloadable" in manifest.execution_modes
            ),
        }

    def model_or_alias_record(self, model_or_alias_id: str) -> dict[str, Any] | None:
        alias = self.get_alias(model_or_alias_id)
        if alias is not None:
            return alias.to_openai_model()
        manifest = self.get_manifest(model_or_alias_id)
        if manifest is None:
            return None
        return self.manifest_record(manifest)

    def versions_for(self, model_or_alias_id: str) -> list[dict[str, Any]]:
        alias = self.get_alias(model_or_alias_id)
        model_id = alias.manifest.id if alias and alias.manifest else model_or_alias_id
        versions = self.manifests_by_id.get(model_id, [])
        return [self.manifest_record(manifest) for manifest in sorted(versions, key=lambda item: item.version, reverse=True)]

    def to_openai_list(self) -> dict[str, Any]:
        return {"object": "list", "data": [alias.to_openai_model() for alias in self.list_aliases()]}

    def to_catalog(self) -> dict[str, Any]:
        return {
            "object": "catalog",
            "aliases": [alias.to_openai_model() for alias in self.list_aliases()],
            "models": [self.manifest_record(manifest) for manifest in self.list_manifests()],
        }


def _without_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_keys(data: dict[str, Any], keys: set[str], context: str) -> None:
    missing = keys - set(data)
    if missing:
        raise CatalogError(f"{context} is missing required keys: {', '.join(sorted(missing))}")


def _forbid_extra_keys(data: dict[str, Any], allowed: set[str], context: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise CatalogError(f"{context} has unsupported keys: {', '.join(sorted(extra))}")


def _string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{context}.{key} must be a non-empty string")
    return value


def _number(data: dict[str, Any], key: str, context: str) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or value < 0:
        raise CatalogError(f"{context}.{key} must be a non-negative number")
    return float(value)


def _integer(data: dict[str, Any], key: str, context: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or value < 0:
        raise CatalogError(f"{context}.{key} must be a non-negative integer")
    return value


def _optional_string(data: dict[str, Any], key: str, context: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    return _string(data, key, context)


def _string_list(data: dict[str, Any], key: str, context: str, allowed: set[str] | None = None, min_items: int = 0) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or len(value) < min_items or any(not isinstance(item, str) or not item for item in value):
        raise CatalogError(f"{context}.{key} must be a list of strings")
    if len(value) != len(set(value)):
        raise CatalogError(f"{context}.{key} items must be unique")
    if allowed is not None:
        unknown = set(value) - allowed
        if unknown:
            raise CatalogError(f"{context}.{key} has unsupported values: {', '.join(sorted(unknown))}")
    return list(value)


def _operation_list(data: dict[str, Any], key: str, modality: str, context: str) -> list[str]:
    raw = _string_list(data, key, context, min_items=1)
    normalized: list[str] = []
    seen: set[str] = set()
    allowed = supported_operations_for_modality(modality)
    for operation in raw:
        canonical = canonical_operation(operation, modality)
        if canonical is None:
            raise CatalogError(f"{context}.{key} has unsupported {modality} operation {operation}; supported operations: {', '.join(allowed)}")
        if canonical in seen:
            raise CatalogError(f"{context}.{key} has duplicate operation after normalization: {canonical}")
        seen.add(canonical)
        normalized.append(canonical)
    return normalized


def _validate_id(value: str, context: str) -> str:
    if not ID_PATTERN.match(value):
        raise CatalogError(f"{context} has invalid identifier: {value}")
    return value


def _validate_sha256(value: str, context: str) -> str:
    if not SHA256_PATTERN.match(value):
        raise CatalogError(f"{context} must be a SHA-256 hex digest")
    return value.lower()


def _safe_relative_path(value: str, context: str) -> str:
    normalized = PurePosixPath(value.replace("\\", "/"))
    if normalized.is_absolute() or not normalized.parts:
        raise CatalogError(f"{context} must be a relative path")
    if any(part in {"", ".", ".."} for part in normalized.parts):
        raise CatalogError(f"{context} must not contain traversal segments")
    if WINDOWS_DRIVE_PATTERN.match(normalized.parts[0]):
        raise CatalogError(f"{context} must not contain traversal segments")
    for part in normalized.parts:
        if BAD_PERCENT_ESCAPE_PATTERN.search(part):
            raise CatalogError(f"{context} must not contain encoded path-control segments")
        try:
            decoded = unquote(part, errors="strict")
        except UnicodeDecodeError as exc:
            raise CatalogError(f"{context} must not contain encoded path-control segments") from exc
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded or "?" in decoded or "#" in decoded:
            raise CatalogError(f"{context} must not contain encoded path-control segments")
        if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
            raise CatalogError(f"{context} must not contain encoded path-control segments")
    return normalized.as_posix()


def _parse_source(data: dict[str, Any], context: str) -> ManifestSource:
    _require_keys(data, {"type", "url", "revision"}, context)
    _forbid_extra_keys(data, {"type", "url", "revision", "sha256"}, context)
    source_type = _string(data, "type", context)
    if source_type not in SOURCE_TYPES:
        raise CatalogError(f"{context}.type is unsupported: {source_type}")
    sha = data.get("sha256")
    return ManifestSource(
        type=source_type,
        url=_string(data, "url", context),
        revision=_string(data, "revision", context),
        sha256=_validate_sha256(sha, f"{context}.sha256") if sha is not None else None,
    )


def _parse_file(data: dict[str, Any], context: str) -> ManifestFile:
    _require_keys(data, {"path", "sha256", "size_bytes"}, context)
    _forbid_extra_keys(data, {"path", "sha256", "size_bytes", "format", "quantization"}, context)
    size = data["size_bytes"]
    if not isinstance(size, int) or size < 1:
        raise CatalogError(f"{context}.size_bytes must be a positive integer")
    return ManifestFile(
        path=_safe_relative_path(_string(data, "path", context), f"{context}.path"),
        sha256=_validate_sha256(_string(data, "sha256", context), f"{context}.sha256"),
        size_bytes=size,
        format=_optional_string(data, "format", context),
        quantization=_optional_string(data, "quantization", context),
    )


def _parse_resource(data: dict[str, Any], context: str) -> ModelResourceEstimate:
    _require_keys(data, {"vram_gib", "ram_gib", "disk_gib"}, context)
    _forbid_extra_keys(data, {"vram_gib", "ram_gib", "disk_gib", "context_tokens", "max_resolution", "max_frames"}, context)
    return ModelResourceEstimate(
        vram_gib=_number(data, "vram_gib", context),
        ram_gib=_number(data, "ram_gib", context),
        disk_gib=_number(data, "disk_gib", context),
        context_tokens=_integer(data, "context_tokens", context) if "context_tokens" in data else None,
        max_resolution=_optional_string(data, "max_resolution", context),
        max_frames=_integer(data, "max_frames", context) if "max_frames" in data else None,
    )


def _parse_license(data: dict[str, Any], context: str) -> ModelLicense:
    _require_keys(data, {"name", "redistribution"}, context)
    _forbid_extra_keys(data, {"name", "url", "redistribution", "attribution", "acceptance_required"}, context)
    redistribution = _string(data, "redistribution", context)
    if redistribution not in REDISTRIBUTION_POLICIES:
        raise CatalogError(f"{context}.redistribution is unsupported: {redistribution}")
    acceptance = data.get("acceptance_required")
    if acceptance is not None and not isinstance(acceptance, bool):
        raise CatalogError(f"{context}.acceptance_required must be boolean")
    return ModelLicense(
        name=_string(data, "name", context),
        url=_optional_string(data, "url", context),
        redistribution=redistribution,
        attribution=_optional_string(data, "attribution", context),
        acceptance_required=acceptance,
    )


def _parse_runtime_adapter_versions(data: Any, runtimes: list[str], context: str) -> dict[str, str]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CatalogError(f"{context} must be an object")
    parsed: dict[str, str] = {}
    runtime_set = set(runtimes)
    for runtime, version in data.items():
        if not isinstance(runtime, str) or runtime not in RUNTIME_NAMES:
            raise CatalogError(f"{context} has unsupported runtime: {runtime}")
        if runtime not in runtime_set:
            raise CatalogError(f"{context}.{runtime} must also be listed in runtimes")
        if not isinstance(version, str) or not version.strip():
            raise CatalogError(f"{context}.{runtime} must be a non-empty version string")
        parsed[runtime] = version.strip()
    return parsed


def _parse_companion_files(data: Any, context: str) -> list[ModelCompanionFile]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise CatalogError(f"{context} must be a list")
    parsed: list[ModelCompanionFile] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        item_context = f"{context}[{index}]"
        if not isinstance(item, dict):
            raise CatalogError(f"{item_context} must be an object")
        _require_keys(item, {"path"}, item_context)
        _forbid_extra_keys(item, {"path", "description", "required", "sha256", "size_bytes", "format"}, item_context)
        path = _safe_relative_path(_string(item, "path", item_context), f"{item_context}.path")
        if path in seen:
            raise CatalogError(f"{context} has duplicate path: {path}")
        seen.add(path)
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise CatalogError(f"{item_context}.required must be boolean")
        size_bytes = item.get("size_bytes")
        if size_bytes is not None and (not isinstance(size_bytes, int) or size_bytes < 1):
            raise CatalogError(f"{item_context}.size_bytes must be a positive integer")
        sha256 = item.get("sha256")
        parsed.append(
            ModelCompanionFile(
                path=path,
                required=required,
                description=_optional_string(item, "description", item_context),
                sha256=_validate_sha256(sha256, f"{item_context}.sha256") if sha256 is not None else None,
                size_bytes=size_bytes,
                format=_optional_string(item, "format", item_context),
            )
        )
    return parsed


def _parse_permissions(data: Any, context: str) -> ModelPermissions:
    if data is None:
        return ModelPermissions()
    if not isinstance(data, dict):
        raise CatalogError(f"{context} must be an object")
    _forbid_extra_keys(data, {"visible_to", "installable_by", "downloadable_by", "inference_roles"}, context)
    return ModelPermissions(
        visible_to=_string_list(data, "visible_to", context, ROLES) if "visible_to" in data else [],
        installable_by=_string_list(data, "installable_by", context, ROLES) if "installable_by" in data else [],
        downloadable_by=_string_list(data, "downloadable_by", context, ROLES) if "downloadable_by" in data else [],
        inference_roles=_string_list(data, "inference_roles", context, ROLES) if "inference_roles" in data else [],
    )


def _parse_deprecation(data: Any, context: str) -> ModelDeprecation:
    if data is None:
        return ModelDeprecation()
    if not isinstance(data, dict):
        raise CatalogError(f"{context} must be an object")
    _require_keys(data, {"status"}, context)
    _forbid_extra_keys(data, {"status", "deprecated_at", "message", "replacement_model", "replacement_version"}, context)
    status = _string(data, "status", context)
    if status not in DEPRECATION_STATUSES:
        raise CatalogError(f"{context}.status is unsupported: {status}")
    replacement_model = data.get("replacement_model")
    replacement_version = data.get("replacement_version")
    if replacement_model is not None:
        replacement_model = _validate_id(_string(data, "replacement_model", context), f"{context}.replacement_model")
    if replacement_version is not None:
        replacement_version = _string(data, "replacement_version", context)
    if status == "replaced" and replacement_model is None:
        raise CatalogError(f"{context}.replacement_model is required when status is replaced")
    return ModelDeprecation(
        status=status,
        deprecated_at=_optional_string(data, "deprecated_at", context),
        message=_optional_string(data, "message", context),
        replacement_model=replacement_model,
        replacement_version=replacement_version,
    )


def _validate_timestamp(value: str, context: str) -> None:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogError(f"{context} must be an ISO-8601 timestamp") from exc


def _parse_measurements(
    data: Any,
    context: str,
    *,
    require_complete: bool = False,
    manifest_id: str | None = None,
    manifest_version: str | None = None,
    runtimes: list[str] | None = None,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    if data is None:
        if require_complete:
            raise CatalogError(f"{context} is required for available model recommendations")
        return {}
    if not isinstance(data, dict):
        raise CatalogError(f"{context} must be an object")
    allowed = {
        "schema",
        "updated_at",
        "source",
        "original_resource_estimate",
        "latest_resource_estimate",
        "runs",
    }
    _forbid_extra_keys(data, allowed, context)
    try:
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"{context} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > 65536:
        raise CatalogError(f"{context} exceeds 65536 bytes")
    source = data.get("source")
    if isinstance(source, str) and ":latest" in source:
        raise CatalogError(f"{context}.source must not reference floating latest tags")
    _require_keys(data, allowed, context)
    schema = data.get("schema")
    if schema is not None and schema != MEASUREMENT_SCHEMA:
        raise CatalogError(f"{context}.schema is unsupported: {schema}")
    for key in ("updated_at", "source"):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise CatalogError(f"{context}.{key} must be a non-empty string")
    if isinstance(data.get("updated_at"), str):
        _validate_timestamp(data["updated_at"], f"{context}.updated_at")
    for key in ("original_resource_estimate", "latest_resource_estimate"):
        if key in data:
            _parse_resource(data[key], f"{context}.{key}")
    runs = data.get("runs", [])
    if not isinstance(runs, list):
        raise CatalogError(f"{context}.runs must be a list")
    if not runs:
        raise CatalogError(f"{context}.runs must contain at least one measured run")
    if len(runs) > 100:
        raise CatalogError(f"{context}.runs may keep at most 100 entries")
    run_allowed = {
        "id",
        "type",
        "status",
        "runtime",
        "model_alias",
        "resolved_model_version",
        "started_at",
        "completed_at",
        "duration_ms",
        "load_time_ms",
        "run_time_ms",
        "peak_vram_mib",
        "peak_ram_mib",
        "resource_estimate",
        "hook",
        "error",
    }
    for index, run in enumerate(runs):
        run_context = f"{context}.runs[{index}]"
        if not isinstance(run, dict):
            raise CatalogError(f"{run_context} must be an object")
        _forbid_extra_keys(run, run_allowed, run_context)
        _require_keys(run, {"id", "type", "status", "runtime", "model_alias", "resolved_model_version", "peak_vram_mib"}, run_context)
        for key in ("id", "type", "status", "runtime", "model_alias", "resolved_model_version", "started_at", "completed_at", "error"):
            if key in run and (not isinstance(run[key], str) or not run[key].strip()):
                raise CatalogError(f"{run_context}.{key} must be a non-empty string")
        if "status" in run and run["status"] not in MEASUREMENT_RUN_STATUSES:
            raise CatalogError(f"{run_context}.status is unsupported: {run['status']}")
        if "runtime" in run and run["runtime"] not in RUNTIME_NAMES:
            raise CatalogError(f"{run_context}.runtime is unsupported: {run['runtime']}")
        if require_complete and runtimes is not None and run.get("runtime") not in set(runtimes):
            raise CatalogError(f"{run_context}.runtime must be listed in manifest runtimes")
        if require_complete and aliases and run.get("model_alias") not in set(aliases):
            raise CatalogError(f"{run_context}.model_alias must be listed in manifest aliases")
        if require_complete and manifest_id and manifest_version:
            expected = f"{manifest_id}@{manifest_version}"
            if run.get("resolved_model_version") != expected:
                raise CatalogError(f"{run_context}.resolved_model_version must be {expected}")
        for key in ("duration_ms", "load_time_ms", "run_time_ms", "peak_vram_mib", "peak_ram_mib"):
            if key in run and (not isinstance(run[key], int) or run[key] < 0):
                raise CatalogError(f"{run_context}.{key} must be a non-negative integer")
        for key in ("started_at", "completed_at"):
            if isinstance(run.get(key), str):
                _validate_timestamp(run[key], f"{run_context}.{key}")
        if "resource_estimate" in run:
            _parse_resource(run["resource_estimate"], f"{run_context}.resource_estimate")
        if "hook" in run and not isinstance(run["hook"], dict):
            raise CatalogError(f"{run_context}.hook must be an object")
    if require_complete and not any(run.get("status") == "ok" for run in runs):
        raise CatalogError(f"{context}.runs must contain at least one ok measurement")
    return dict(data)


def _validate_available_recommendation(
    context: str,
    *,
    source: ManifestSource,
    license_info: ModelLicense,
    aliases: list[str],
) -> None:
    if source.type == "upload":
        raise CatalogError(f"{context}.source.type upload cannot be an available catalog recommendation")
    if not source.url.strip() or not source.revision.strip():
        raise CatalogError(f"{context}.source must declare a download URL and immutable revision")
    if not aliases:
        raise CatalogError(f"{context}.aliases must name at least one public alias for an available recommendation")
    if not license_info.url:
        raise CatalogError(f"{context}.license.url is required for available model recommendations")
    if not license_info.attribution:
        raise CatalogError(f"{context}.license.attribution is required for available model recommendations")
    if license_info.acceptance_required is None:
        raise CatalogError(f"{context}.license.acceptance_required must be declared for available model recommendations")


def _parse_manifest(
    data: dict[str, Any],
    context: str,
    *,
    require_available_recommendation_metadata: bool = False,
) -> ModelManifest:
    required = {"id", "version", "display_name", "modality", "operations", "source", "files", "runtimes", "preferred_runtime", "resource_estimate", "license", "execution_modes"}
    allowed = required | {
        "description",
        "installation_status",
        "aliases",
        "visibility_roles",
        "permissions",
        "runtime_adapter_versions",
        "companion_files",
        "deprecation",
        "measurements",
    }
    _require_keys(data, required, context)
    _forbid_extra_keys(data, allowed, context)

    model_id = _validate_id(_string(data, "id", context), f"{context}.id")
    modality = _string(data, "modality", context)
    if modality not in MODALITIES:
        raise CatalogError(f"{context}.modality is unsupported: {modality}")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise CatalogError(f"{context}.files must contain at least one file")
    runtimes = _string_list(data, "runtimes", context, RUNTIME_NAMES, min_items=1)
    preferred_runtime = _string(data, "preferred_runtime", context)
    if preferred_runtime not in runtimes:
        raise CatalogError(f"{context}.preferred_runtime must be listed in runtimes")
    execution_modes = _string_list(data, "execution_modes", context, EXECUTION_MODES, min_items=1)
    installation_status = data.get("installation_status", "installed")
    if not isinstance(installation_status, str) or installation_status not in INSTALLATION_STATUSES:
        raise CatalogError(f"{context}.installation_status is unsupported: {installation_status}")
    license_info = _parse_license(data["license"], f"{context}.license")
    if license_info.redistribution == "inference-only" and set(execution_modes) - {"hosted-inference"}:
        raise CatalogError(f"{context} inference-only models cannot be downloadable or network-shared")
    version = _string(data, "version", context)
    source = _parse_source(data["source"], f"{context}.source")
    files_parsed = [_parse_file(item, f"{context}.files[{index}]") for index, item in enumerate(files)]
    resource_estimate = _parse_resource(data["resource_estimate"], f"{context}.resource_estimate")
    aliases = [_validate_id(alias, f"{context}.aliases[]") for alias in _string_list(data, "aliases", context) if alias] if "aliases" in data else []
    if installation_status == "available" and require_available_recommendation_metadata:
        _validate_available_recommendation(context, source=source, license_info=license_info, aliases=aliases)
    measurements = _parse_measurements(
        data.get("measurements"),
        f"{context}.measurements",
        require_complete=installation_status == "available" and require_available_recommendation_metadata,
        manifest_id=model_id,
        manifest_version=version,
        runtimes=runtimes,
        aliases=aliases,
    )
    return ModelManifest(
        id=model_id,
        version=version,
        display_name=_string(data, "display_name", context),
        description=_optional_string(data, "description", context),
        modality=modality,
        operations=_operation_list(data, "operations", modality, context),
        source=source,
        files=files_parsed,
        runtimes=runtimes,
        preferred_runtime=preferred_runtime,
        resource_estimate=resource_estimate,
        license=license_info,
        execution_modes=execution_modes,
        installation_status=installation_status,
        aliases=aliases,
        visibility_roles=_string_list(data, "visibility_roles", context, ROLES) if "visibility_roles" in data else [],
        permissions=_parse_permissions(data.get("permissions"), f"{context}.permissions"),
        runtime_adapter_versions=_parse_runtime_adapter_versions(data.get("runtime_adapter_versions"), runtimes, f"{context}.runtime_adapter_versions"),
        companion_files=_parse_companion_files(data.get("companion_files"), f"{context}.companion_files"),
        deprecation=_parse_deprecation(data.get("deprecation"), f"{context}.deprecation"),
        measurements=measurements,
    )


def _parse_alias(data: dict[str, Any], context: str) -> AliasDefinition:
    _require_keys(data, {"alias", "modality", "preferred_runtime"}, context)
    _forbid_extra_keys(data, {"alias", "modality", "preferred_runtime", "status"}, context)
    alias = _validate_id(_string(data, "alias", context), f"{context}.alias")
    modality = _string(data, "modality", context)
    if modality not in MODALITIES:
        raise CatalogError(f"{context}.modality is unsupported: {modality}")
    preferred_runtime = _string(data, "preferred_runtime", context)
    if preferred_runtime not in RUNTIME_NAMES:
        raise CatalogError(f"{context}.preferred_runtime is unsupported: {preferred_runtime}")
    status = data.get("status", "uninstalled")
    if not isinstance(status, str) or not status:
        raise CatalogError(f"{context}.status must be a non-empty string")
    return AliasDefinition(alias=alias, modality=modality, preferred_runtime=preferred_runtime, status=status)


def _parse_aliases(data: dict[str, Any], context: str) -> list[AliasDefinition]:
    _require_keys(data, {"aliases"}, context)
    _forbid_extra_keys(data, {"aliases"}, context)
    aliases = data["aliases"]
    if not isinstance(aliases, list):
        raise CatalogError(f"{context}.aliases must be a list")
    parsed = [_parse_alias(item, f"{context}.aliases[{index}]") for index, item in enumerate(aliases)]
    names = [item.alias for item in parsed]
    if len(names) != len(set(names)):
        raise CatalogError("alias names must be unique")
    return parsed


def _parse_alias_policy(data: dict[str, Any], context: str) -> dict[str, Any]:
    _require_keys(data, {"alias"}, context)
    _forbid_extra_keys(
        data,
        {
            "alias",
            "enabled",
            "preferred_runtime",
            "idle_timeout_seconds",
            "visibility_roles",
            "notes",
            "updated_by",
            "created_at",
            "updated_at",
        },
        context,
    )
    alias = _validate_id(_string(data, "alias", context), f"{context}.alias")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise CatalogError(f"{context}.enabled must be a boolean")
    preferred_runtime = data.get("preferred_runtime")
    if preferred_runtime in {"", None}:
        preferred_runtime = None
    if preferred_runtime is not None:
        if not isinstance(preferred_runtime, str) or preferred_runtime not in RUNTIME_NAMES:
            raise CatalogError(f"{context}.preferred_runtime is unsupported: {preferred_runtime}")
    idle_timeout_seconds = data.get("idle_timeout_seconds")
    if idle_timeout_seconds is not None:
        if not isinstance(idle_timeout_seconds, int) or idle_timeout_seconds < 30 or idle_timeout_seconds > 86400:
            raise CatalogError(f"{context}.idle_timeout_seconds must be between 30 and 86400 seconds")
    visibility_roles = _string_list(data, "visibility_roles", context, ROLES) if "visibility_roles" in data else []
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise CatalogError(f"{context}.notes must be a string")
    return {
        "alias": alias,
        "enabled": enabled,
        "preferred_runtime": preferred_runtime,
        "idle_timeout_seconds": idle_timeout_seconds,
        "visibility_roles": tuple(visibility_roles),
        "notes": notes,
    }


def _apply_alias_policies(aliases: list[AliasDefinition], alias_policies: list[dict[str, Any]] | None) -> list[AliasDefinition]:
    if not alias_policies:
        return aliases
    policies: dict[str, dict[str, Any]] = {}
    for index, policy in enumerate(alias_policies):
        parsed = _parse_alias_policy(policy, f"alias_policies[{index}]")
        policies[parsed["alias"]] = parsed
    overridden: list[AliasDefinition] = []
    for alias in aliases:
        policy = policies.get(alias.alias)
        if policy is None:
            overridden.append(alias)
            continue
        overridden.append(
            AliasDefinition(
                alias=alias.alias,
                modality=alias.modality,
                preferred_runtime=alias.preferred_runtime,
                status=alias.status,
                enabled=policy["enabled"],
                preferred_runtime_override=policy["preferred_runtime"],
                idle_timeout_seconds=policy["idle_timeout_seconds"],
                visibility_roles=policy["visibility_roles"],
                policy_source="database",
                notes=policy["notes"],
            )
        )
    return overridden


def parse_manifest_payload(data: dict[str, Any], context: str = "manifest") -> ModelManifest:
    return _parse_manifest(data, context)


def load_catalog(
    catalog_dir: Path,
    policy: ResourcePolicy,
    extra_manifests: list[dict[str, Any]] | None = None,
    alias_policies: list[dict[str, Any]] | None = None,
) -> ModelCatalog:
    seed_dir = catalog_dir / "seed"
    aliases_path = seed_dir / "aliases.json"
    if not aliases_path.exists():
        raise CatalogError(f"missing alias seed: {aliases_path}")
    try:
        aliases = _parse_aliases(_read_json(aliases_path), str(aliases_path))
        aliases = _apply_alias_policies(aliases, alias_policies)
        seed_manifests = [
            _parse_manifest(_read_json(path), str(path), require_available_recommendation_metadata=True)
            for path in sorted(seed_dir.glob("*.manifest.json"))
        ]
        installed_manifests = [
            replace(_parse_manifest(item, f"installed_manifests[{index}]"), installation_status="installed")
            for index, item in enumerate(extra_manifests or [])
        ]
        installed_keys = {(manifest.id, manifest.version) for manifest in installed_manifests}
        installed_aliases = {alias for manifest in installed_manifests for alias in manifest.aliases}
        manifests = [
            manifest
            for manifest in seed_manifests
            if (manifest.id, manifest.version) not in installed_keys
            and not (manifest.installation_status == "installed" and set(manifest.aliases) & installed_aliases)
        ]
        manifests.extend(installed_manifests)
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"invalid model catalog under {catalog_dir}: {exc}") from exc
    return ModelCatalog(aliases, manifests, policy)
