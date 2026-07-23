from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .scheduler import AdmissionDecision, ResourceEstimate, ResourcePolicy, classify_resource_fit


RUNTIME_NAMES = {"localai", "comfyui", "voicebox", "audio-cpu", "openai-compatible", "generic-http"}
MODALITIES = {"llm", "vlm", "embedding", "tts", "stt", "image", "video", "workflow"}
EXECUTION_MODES = {"hosted-inference", "downloadable", "network-share"}
INSTALLATION_STATUSES = {"available", "installed", "quarantined", "failed"}
ROLES = {"admin", "operator", "creator", "user", "service"}
REDISTRIBUTION_POLICIES = {"downloadable", "inference-only", "restricted"}
SOURCE_TYPES = {"catalog", "huggingface", "direct-url", "upload"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")


class CatalogError(ValueError):
    pass


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
    measurements: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
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
            "cpu_resident_candidate": self.preferred_runtime == "audio-cpu",
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
            self.aliases[alias.alias] = CatalogAlias(alias=alias, manifest=manifest, decision=self._decision_for(manifest))

    def _decision_for(self, manifest: ModelManifest | None) -> AdmissionDecision:
        if manifest is None:
            return AdmissionDecision(False, "uninstalled", "alias has no installed model manifest")
        estimate = manifest.resource_estimate.to_scheduler_estimate(requires_gpu=manifest.preferred_runtime != "audio-cpu")
        return classify_resource_fit(self.policy, estimate)

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


def _parse_measurements(data: Any, context: str) -> dict[str, Any]:
    if data is None:
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
    schema = data.get("schema")
    if schema is not None and schema != "b1-ai-hub-model-measurements/v1":
        raise CatalogError(f"{context}.schema is unsupported: {schema}")
    for key in ("updated_at", "source"):
        if key in data and not isinstance(data[key], str):
            raise CatalogError(f"{context}.{key} must be a string")
    for key in ("original_resource_estimate", "latest_resource_estimate"):
        if key in data:
            _parse_resource(data[key], f"{context}.{key}")
    runs = data.get("runs", [])
    if not isinstance(runs, list):
        raise CatalogError(f"{context}.runs must be a list")
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
        if not isinstance(run, dict):
            raise CatalogError(f"{context}.runs[{index}] must be an object")
        _forbid_extra_keys(run, run_allowed, f"{context}.runs[{index}]")
        for key in ("id", "type", "status", "runtime", "model_alias", "resolved_model_version", "started_at", "completed_at", "error"):
            if key in run and not isinstance(run[key], str):
                raise CatalogError(f"{context}.runs[{index}].{key} must be a string")
        for key in ("duration_ms", "load_time_ms", "run_time_ms", "peak_vram_mib", "peak_ram_mib"):
            if key in run and (not isinstance(run[key], int) or run[key] < 0):
                raise CatalogError(f"{context}.runs[{index}].{key} must be a non-negative integer")
        if "resource_estimate" in run:
            _parse_resource(run["resource_estimate"], f"{context}.runs[{index}].resource_estimate")
        if "hook" in run and not isinstance(run["hook"], dict):
            raise CatalogError(f"{context}.runs[{index}].hook must be an object")
    return dict(data)


def _parse_manifest(data: dict[str, Any], context: str) -> ModelManifest:
    required = {"id", "version", "display_name", "modality", "operations", "source", "files", "runtimes", "preferred_runtime", "resource_estimate", "license", "execution_modes"}
    allowed = required | {"description", "installation_status", "aliases", "visibility_roles", "measurements"}
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
    return ModelManifest(
        id=model_id,
        version=_string(data, "version", context),
        display_name=_string(data, "display_name", context),
        description=_optional_string(data, "description", context),
        modality=modality,
        operations=_string_list(data, "operations", context, min_items=1),
        source=_parse_source(data["source"], f"{context}.source"),
        files=[_parse_file(item, f"{context}.files[{index}]") for index, item in enumerate(files)],
        runtimes=runtimes,
        preferred_runtime=preferred_runtime,
        resource_estimate=_parse_resource(data["resource_estimate"], f"{context}.resource_estimate"),
        license=license_info,
        execution_modes=execution_modes,
        installation_status=installation_status,
        aliases=[_validate_id(alias, f"{context}.aliases[]") for alias in _string_list(data, "aliases", context) if alias] if "aliases" in data else [],
        visibility_roles=_string_list(data, "visibility_roles", context, ROLES) if "visibility_roles" in data else [],
        measurements=_parse_measurements(data.get("measurements"), f"{context}.measurements"),
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
        seed_manifests = [_parse_manifest(_read_json(path), str(path)) for path in sorted(seed_dir.glob("*.manifest.json"))]
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
