from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import ssl
import stat
import uuid
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Callable, Literal
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

import httpx
import redis.asyncio as redis
from fastapi import Body, FastAPI, Header, HTTPException, Path as ApiPath, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from . import admission
from . import acceptance
from . import database
from . import artifact_retention
from . import audit as audit_policy
from . import artifacts as artifact_policy
from . import backup_restore
from . import backup_migration_rollback
from . import backup_schedule
from . import compose_override as compose_override_policy
from . import open_webui_migration
from . import rollback_rehearsal
from .job_events import format_sse_comment, format_sse_event, job_event_id, should_emit_job_snapshot
from .job_states import ACTIVE_JOB_STATES, TERMINAL_JOB_STATES
from .job_redaction import redact_request
from . import media_artifacts
from . import model_lifecycle
from . import model_tools
from . import modelhub as modelhub_policy
from . import secret_store
from . import selftest as selftest_policy
from . import update_policy
from . import voice_profiles as voice_profile_policy
from . import voicebox_samples
from .adapters import RuntimeAdapter, RuntimeRegistry, RuntimeResolution, RuntimeResolutionError, build_runtime_registry, validate_external_runtime_base_url
from .auth import (
    AuthContext,
    Role,
    generate_csrf_token,
    generate_api_key,
    generate_session_token,
    hash_api_key,
    hash_password,
    hash_session_token,
    key_prefix_from_token,
    password_policy_errors,
    scopes_for_role,
    verify_api_key,
    verify_password,
)
from .catalog import ID_PATTERN, MODALITIES, RUNTIME_NAMES, CatalogAlias, CatalogError, ModelCatalog, load_catalog, runtime_smoke_summary_for_manifest
from .executor import (
    GPU_RUNTIMES,
    GPU_STATE_STEPS,
    CpuJobRunner,
    GpuJobRunner,
    ModelDownloadRunner,
    RuntimePreparationError,
)
from .observability import build_observability_report, observability_report_to_prometheus
from .runtime_agent_http import runtime_agent_httpx_kwargs
from .scheduler import CPU_RESIDENT_DEFAULT_ALIASES, JobState, PriorityClass, ResourceEstimate, ResourcePolicy, classify_resource_fit
from .settings import Settings, load_settings
from .workflows import ApprovedNodePin, WorkflowError, load_node_pins, load_workflows, parse_node_pin, parse_workflow, validate_workflow_job_request, visible_to_role, workflow_execution_summary, workflow_record


LOG = logging.getLogger("b1.control-plane")
logging.basicConfig(level=logging.INFO, format="%(message)s")

settings: Settings = load_settings()
redis_client: redis.Redis | None = None
model_catalog: ModelCatalog | None = None
runtime_registry: RuntimeRegistry | None = None
runtime_configuration_cache: dict[str, dict[str, Any]] | None = None
resource_policy_override: ResourcePolicy | None = None
admission_policy_override: admission.AdmissionPolicy | None = None
maintenance_state_cache: dict[str, Any] | None = None
network_policy_cache: dict[str, Any] | None = None
approved_node_pins: dict[tuple[str, str], ApprovedNodePin] | None = None
job_runners: list[Any] = []
job_runner_tasks: list[asyncio.Task[None]] = []
control_plane_started_at: datetime | None = None
comfyui_native_prompt_resume: dict[str, Any] | None = None
backup_operation_lock = asyncio.Lock()
current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar("b1_current_request", default=None)
modelhub_blob_rate_windows: dict[str, tuple[int, float]] = {}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_EXEMPT_PATHS = {"/auth/login", "/auth/setup"}
OPEN_WEBUI_CLIENT_ID = "client_open_webui_internal"
OPEN_WEBUI_SCOPES = ["models:read", "inference:write", "jobs:read", "jobs:write", "workflows:read"]
COMFYUI_OUTPUT_KEYS = {
    "images": "image",
    "gifs": "video",
    "videos": "video",
    "audio": "audio",
}
EXTERNAL_RUNTIME_NAMES = {"openai-compatible", "generic-http"}
SERVICE_LOG_NAMES = {"control-plane", "localai", "comfyui", "voicebox", "audio-cpu", "artifact-server", "open-webui", "gateway"}
SERVICE_LOG_LINE_MAX_BYTES = 4096
SERVICE_LOG_SECRET_PATTERNS = [
    (re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"\bb1k_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "<redacted>"),
    (re.compile(r"\bb1adm_[A-Za-z0-9_-]+\b"), "<redacted>"),
]
ACCEPTANCE_MODEL_MEASUREMENT_GROUP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "localai_runtime",
        "label": "LocalAI runtime acceptance",
        "aliases": (("chat-default", "localai", "localai"),),
    },
    {
        "id": "gpu_acceptance",
        "label": "RTX 3060 GPU acceptance",
        "aliases": (
            ("chat-default", "localai", "localai"),
            ("image-default", "comfyui", "comfyui"),
            ("tts-quality", "voicebox", "voicebox"),
        ),
    },
    {
        "id": "installed_workflows",
        "label": "Installed workflow acceptance",
        "aliases": (
            ("chat-default", "localai", "localai"),
            ("tts-fast", "audio-cpu", "audio-cpu"),
            ("stt-default", "audio-cpu", "audio-cpu"),
            ("image-default", "comfyui", "comfyui"),
            ("image-edit", "comfyui", "comfyui"),
            ("video-text", "comfyui", "comfyui"),
        ),
    },
)
ACCEPTANCE_MODEL_MEASUREMENT_GROUPS = ACCEPTANCE_MODEL_MEASUREMENT_GROUP_SPECS
COMFYUI_QUEUE_CANCEL_KEYS = {"delete", "cancel", "prompt_id", "prompt_ids"}
COMFYUI_PROMPT_KNOWN_TOP_LEVEL_KEYS = {"client_id", "extra_data", "front", "number", "prompt"}
COMFYUI_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
COMFYUI_NODE_PIN_LOCK_DIGEST = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_RESERVATION_RUNTIME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
RUNTIME_RESERVATION_REASON_MAX_LENGTH = 500
COMFYUI_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
COMFYUI_MUTATING_CORE_ROUTES: dict[str, set[str]] = {
    "interrupt": {"POST"},
    "queue": {"POST", "PUT", "PATCH", "DELETE"},
    "upload/image": {"POST"},
    "upload/mask": {"POST"},
    "api/userdata": {"POST", "PUT", "PATCH", "DELETE"},
}
COMFYUI_MUTATING_CORE_PREFIXES: dict[str, set[str]] = {
    "api/userdata/": {"POST", "PUT", "PATCH", "DELETE"},
}
COMFYUI_DENIED_PREFIXES = (
    "b1",
    "b1/",
    "manager",
    "manager/",
    "customnode",
    "customnode/",
    "custom-node",
    "custom-node/",
    "custom_nodes",
    "custom_nodes/",
    "api/manager",
    "api/manager/",
    "api/customnode",
    "api/customnode/",
)
COMFYUI_DENIED_MUTATION_TOKENS = {
    "git",
    "install",
    "pip",
    "requirements",
    "snapshot",
    "uninstall",
    "update",
    "upgrade",
}
RUNTIME_PROXY_FORBIDDEN_REQUEST_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "cookie",
    "forwarded",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-b1-compatibility",
    "x-b1-csrf",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
}
MODELHUB_BLOB_RATE_WINDOW_SECONDS = 60
MODELHUB_BLOB_RATE_FALLBACK_MAX_SUBJECTS = 4096
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = [
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "X-B1-CSRF",
    "X-Request-Id",
    "X-B1-Owner",
    "X-B1-Field",
    "X-B1-Filename",
    "X-B1-Accept-License",
]
CORS_EXPOSE_HEADERS = [
    "X-B1-Runtime",
    "X-B1-Resolved-Model",
    "X-B1-Public-Model",
    "X-B1-Tools",
    "X-B1-Tool-Iterations",
]
IDEMPOTENCY_KEY_MAX_LENGTH = 256
IDEMPOTENCY_IDENTITY_FIELDS = (
    "modality",
    "operation",
    "model_alias",
    "priority",
)

app = FastAPI(
    title="B1 AI Hub Control Plane",
    version="0.1.0",
    description="Authoritative scheduler, registry, job, compatibility, and unified API service for B1 AI Hub.",
)

class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "chat-default"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    runtime_policy: str = "any"
    b1_tools: list[str] | None = Field(default=None, max_length=16)
    b1_tool_max_iterations: int = Field(default=4, ge=1, le=8)


class EmbeddingRequest(BaseModel):
    model: str = "embedding-default"
    input: str | list[str]
    dimensions: int | None = Field(default=None, ge=1, le=4096)


class MediaJobCreate(BaseModel):
    modality: str
    operation: str
    model: str
    input: dict[str, Any] = Field(default_factory=dict)
    priority: str = "single_image"
    runtime_policy: str = "any"


class RuntimeReservationCreate(BaseModel):
    runtime: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    duration_seconds: int = Field(default=300, ge=30, le=7200)
    reason: str = Field(default="", max_length=RUNTIME_RESERVATION_REASON_MAX_LENGTH)


class AcceptanceReportCreate(BaseModel):
    label: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=4000)
    operator_evidence: dict[str, bool] = Field(default_factory=dict)
    operator_evidence_notes: dict[str, str] = Field(default_factory=dict)


class RollbackRehearsalCreate(BaseModel):
    cutover_plan_name: str | None = Field(default=None, max_length=160)
    rehearsed_by: str = Field(default="", max_length=128)
    rollback_commands_tested: bool = False
    old_resources_preserved: bool = False
    notes: str = Field(default="", max_length=2000)


class BackupMigrationRollbackEvidenceCreate(BaseModel):
    confirm_reviewed: bool = False


class OpenWebUiMigrationPlanCreate(BaseModel):
    notes: str = Field(default="", max_length=2000)


class RuntimeActionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    timeout_seconds: int = Field(default=10, ge=1, le=120)


class RuntimeExternalConfigRequest(BaseModel):
    enabled: bool = False
    base_url: str = Field(default="", max_length=2048)
    api_key_secret_name: str | None = Field(default=None, max_length=128)
    confirm_external_data: bool = False
    notes: str = Field(default="", max_length=2000)


class ModelAliasPolicyRequest(BaseModel):
    modality: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    preferred_runtime: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=64)
    idle_timeout_seconds: int | None = Field(default=None, ge=30, le=86400)
    visibility_roles: list[Role] = Field(default_factory=list)
    notes: str = Field(default="", max_length=2000)


class JobMutationRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class JobPriorityUpdateRequest(JobMutationRequest):
    priority: PriorityClass


class ApiClientCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=256)
    role: Role = Role.SERVICE
    scopes: list[str] | None = None
    cidr_allowlist: list[str] = Field(default_factory=list)
    default_b1_tools: list[str] = Field(default_factory=list, max_length=16)


class CidrAllowlistUpdateRequest(BaseModel):
    cidr_allowlist: list[str] = Field(default_factory=list)


class ApiClientDefaultB1ToolsUpdateRequest(BaseModel):
    default_b1_tools: list[str] = Field(default_factory=list, max_length=16)


class NetworkPolicyUpdateRequest(BaseModel):
    cors_allow_origins: list[str] = Field(default_factory=list)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)


class BlobState(BaseModel):
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    size_bytes: int = Field(ge=0)


class ModelHubSyncPlanRequest(BaseModel):
    models: list[str] = Field(min_length=1)
    installed_blobs: list[BlobState] = Field(default_factory=list)


class ModelHubClientCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=256)
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    cidr_allowlist: list[str] = Field(default_factory=list)
    allow_downloads: bool = True


class ModelHubClientPolicyUpdate(BaseModel):
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    allow_downloads: bool = True


class VoiceProfileSampleArtifact(BaseModel):
    url: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    mime_type: str = Field(pattern=r"^audio/[A-Za-z0-9.+-]+$")
    bytes: int = Field(ge=0)
    reference_text: str | None = Field(default=None, min_length=1, max_length=1000)


class VoiceSampleArtifactUpload(BaseModel):
    url: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    mime_type: str = Field(pattern=r"^audio/[A-Za-z0-9.+-]+$")
    bytes: int = Field(ge=0)
    filename: str = Field(min_length=1, max_length=256)
    sample_id: str = Field(pattern=r"^sample_[a-f0-9]{32}$")


class VoiceSampleArtifactUploadResponse(BaseModel):
    object: Literal["voicebox.sample_artifact"]
    artifact: VoiceSampleArtifactUpload


class VoiceProfileCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=256)
    runtime: Literal["voicebox", "audio-cpu"] = "voicebox"
    engine: str = Field(default="voicebox", pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    model_alias: str = Field(default="tts-quality", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
    profile_type: Literal["preset", "reference", "clone"] = "preset"
    status: Literal["active", "disabled"] = "active"
    visibility_roles: list[Role] = Field(default_factory=lambda: [Role.ADMIN, Role.OPERATOR])
    metadata: dict[str, Any] = Field(default_factory=dict)
    sample_artifacts: list[VoiceProfileSampleArtifact] = Field(default_factory=list)


class VoiceProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    runtime: Literal["voicebox", "audio-cpu"] | None = None
    engine: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    model_alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
    profile_type: Literal["preset", "reference", "clone"] | None = None
    status: Literal["active", "disabled"] | None = None
    visibility_roles: list[Role] | None = None
    metadata: dict[str, Any] | None = None
    sample_artifacts: list[VoiceProfileSampleArtifact] | None = None


class VoiceProfileMetadataPolicyField(BaseModel):
    key: str
    label: str
    runtime_field: str
    input_mode: Literal["text", "decimal"]
    accepted_json_types: list[str]
    max_string_length: int


class VoiceProfilePolicyResponse(BaseModel):
    object: Literal["voicebox.profile_policy"]
    metadata_max_bytes: int
    metadata_string_max_length: int
    safe_metadata_fields: list[VoiceProfileMetadataPolicyField]
    forbidden_metadata_key_fragments: list[str]
    sample_artifact_url_prefix: str


class ModelInstallPlanRequest(BaseModel):
    model: str | None = Field(default=None, max_length=128)
    manifest: dict[str, Any] | None = None
    manifest_url: str | None = Field(default=None, max_length=2048)
    accept_license: bool = False
    allow_resource_override: bool = False
    allow_download_override: bool = False


class HuggingFaceGgufManifestDraftRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    aliases: list[str] | None = Field(default=None, max_length=4)
    model_id: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=256)
    license_name: str = Field(default="review-required", min_length=1, max_length=128)
    license_url: str | None = Field(default=None, max_length=2048)
    attribution: str | None = Field(default=None, max_length=512)
    acceptance_required: bool = True
    context_tokens: int = Field(default=4096, ge=512, le=16384)
    sha256: str | None = Field(default=None, max_length=64)
    size_bytes: int | None = Field(default=None, ge=1)


class ModelInstallRequest(ModelInstallPlanRequest):
    confirm: bool = False
    smoke_test: bool = False


class ModelDownloadCreate(ModelInstallPlanRequest):
    confirm: bool = False
    credential_secret_name: str | None = Field(default=None, max_length=128)


class ModelDownloadInstallRequest(BaseModel):
    confirm: bool = False
    accept_license: bool = False
    allow_resource_override: bool = False
    smoke_test: bool = False


class ModelSmokeTestRequest(BaseModel):
    persist: bool = True
    model_alias: str | None = Field(default=None, max_length=128)


class ModelToolPolicyRequest(BaseModel):
    enabled: bool = True
    allowed_tools: list[str] = Field(default_factory=lambda: ["web_search", "web_fetch"], max_length=64)
    allow_private_network: bool = False
    allowed_hosts: list[str] = Field(default_factory=list, max_length=128)
    max_result_chars: int = Field(default=12000, ge=256, le=200000)
    max_search_results: int = Field(default=5, ge=1, le=25)
    timeout_seconds: float = Field(default=12.0, ge=1.0, le=120.0)
    search_endpoint_template: str = Field(default="https://duckduckgo.com/html/?q={query}", min_length=1, max_length=2048)
    notes: str = Field(default="", max_length=2048)


class ModelToolDefinitionRequest(BaseModel):
    enabled: bool = True
    kind: Literal["http-json"] = "http-json"
    display_name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=2048)
    parameters_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": True})
    config: dict[str, Any] = Field(default_factory=dict)
    visibility_roles: list[str] = Field(default_factory=list, max_length=8)
    notes: str = Field(default="", max_length=2048)


class ModelToolExecuteRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelRemoveRequest(BaseModel):
    confirm: bool = False


class ModelBlobQuarantineRequest(BaseModel):
    confirm: bool = False


class SchedulerLeaseRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=30, ge=5, le=300)


class WorkflowPublishRequest(BaseModel):
    workflow: dict[str, Any]


class WorkflowTestRequest(BaseModel):
    workflow: dict[str, Any] | None = None
    workflow_id: str | None = Field(default=None, min_length=1, max_length=128)
    workflow_version: str | None = Field(default=None, min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)
    runtime_policy: str | None = Field(default=None, min_length=1, max_length=64)
    priority: str = Field(default="single_image", min_length=1, max_length=64)


class WorkflowRestoreRequest(BaseModel):
    confirm: bool = False
    reason: str | None = Field(default=None, max_length=512)


class ComfyUiNodePinCreate(BaseModel):
    id: str = Field(min_length=2, max_length=128)
    commit: str = Field(min_length=40, max_length=40)
    repository_url: str = Field(min_length=1, max_length=2048)
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    status: Literal["approved", "disabled", "superseded"] = "approved"
    dependency_lock_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    allowed_route_prefixes: list[str] = Field(default_factory=list, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class ComfyUiNodePinUpdate(BaseModel):
    repository_url: str | None = Field(default=None, min_length=1, max_length=2048)
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    status: Literal["approved", "disabled", "superseded"] | None = None
    dependency_lock_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    allowed_route_prefixes: list[str] | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class BackupCreateRequest(BaseModel):
    label: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class BackupRestoreTestRequest(BaseModel):
    force: bool = False


class BackupRetentionRequest(BaseModel):
    keep_last: int = Field(default=5, ge=0, le=365)
    delete_older_than_days: int | None = Field(default=None, ge=1, le=3650)
    confirm: bool = False


class ArtifactRetentionRequest(BaseModel):
    delete_older_than_days: int = Field(default=30, ge=1, le=3650)
    namespaces: list[str] = Field(default_factory=list, max_length=16)
    limit: int = Field(default=5000, ge=1, le=50000)
    confirm: bool = False


class VoiceboxSampleRetentionRequest(BaseModel):
    delete_older_than_days: int = Field(default=30, ge=1, le=3650)
    limit: int = Field(default=5000, ge=1, le=50000)
    confirm: bool = False


class ModelQuarantineRetentionRequest(BaseModel):
    delete_older_than_days: int = Field(default=30, ge=1, le=3650)
    limit: int = Field(default=5000, ge=1, le=50000)
    confirm: bool = False


class BackupScheduleUpdateRequest(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(default=24, ge=1, le=720)
    keep_last: int = Field(default=7, ge=1, le=365)
    delete_older_than_days: int | None = Field(default=30, ge=1, le=3650)
    label_prefix: str = Field(default="scheduled", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    run_immediately: bool = False


class MaintenanceUpdateRequest(BaseModel):
    enabled: bool
    reason: str = Field(default="", max_length=500)


class UpdateImageReference(BaseModel):
    service: str = Field(min_length=1, max_length=64)
    image: str = Field(min_length=1, max_length=512)


class UpdatePlanCreateRequest(BaseModel):
    target_version: str = Field(min_length=1, max_length=128)
    source_url: str = Field(default="", max_length=1024)
    image_refs: list[UpdateImageReference] = Field(min_length=1)
    notes: str = Field(default="", max_length=4096)


class UpdateActionRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class ResourcePolicyUpdateRequest(BaseModel):
    gpu_total_vram_gib: float = Field(gt=0)
    gpu_usable_vram_gib: float = Field(gt=0)
    gpu_reserve_vram_gib: float = Field(ge=0)
    gpu_max_active_pipelines: int = Field(ge=1)
    host_total_ram_gib: float = Field(gt=0)
    host_reserve_ram_gib: float = Field(ge=0)
    llm_default_context: int = Field(ge=512)
    llm_maximum_context: int = Field(ge=512)
    llm_default_parallel_requests: int = Field(ge=1)
    comfyui_maximum_parallel_jobs: int = Field(ge=1)
    comfyui_maximum_batch_size: int = Field(ge=1)
    cpu_residency_enabled: bool = True
    cpu_residency_max_ram_gib: float = Field(default=2.0, ge=0)
    cpu_resident_aliases: list[str] = Field(default_factory=lambda: list(CPU_RESIDENT_DEFAULT_ALIASES), max_length=32)


class AdmissionPolicyUpdateRequest(BaseModel):
    max_queued_jobs_per_owner: int = Field(ge=0)
    max_active_jobs_per_owner: int = Field(ge=0)
    max_jobs_per_hour_per_owner: int = Field(ge=0)
    max_queued_jobs_global: int = Field(ge=0)
    artifact_storage_max_bytes: int = Field(ge=0)
    artifact_storage_reserve_bytes: int = Field(ge=0)


SecretCategory = Literal["remote-provider", "model-download", "runtime", "integration", "other"]


class EncryptedSecretSetRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=256)
    category: SecretCategory = "other"
    description: str = Field(default="", max_length=2048)
    value: str = Field(min_length=1, max_length=65536)


class BackupPostgresImportRequest(BaseModel):
    apply: bool = False
    confirm_backup_name: str | None = None


class AuthSetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{2,127}$")
    password: str = Field(min_length=1, max_length=256)
    bootstrap_key: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=256)


class AuthLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def elapsed_milliseconds(start: float) -> int:
    return max(0, int((monotonic() - start) * 1000))


def log_event(event: str, **fields: Any) -> None:
    safe_fields = audit_policy.redact_log_fields(fields)
    LOG.info(json.dumps({**safe_fields, "event": event, "service": settings.service_name, "ts": now_iso()}, default=str))


def rate_limit_headers(limit: int, remaining: int, reset_seconds: int) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(0, remaining)),
        "X-RateLimit-Reset": str(max(0, reset_seconds)),
    }


def modelhub_blob_rate_subject(auth: AuthContext) -> str:
    remote_addr = client_host(current_request.get()) or "unknown"
    subject = f"{auth.subject_id}:{auth.key_prefix or auth.role.value}:{remote_addr}"
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def prune_modelhub_blob_rate_windows(now: float) -> None:
    expired = [subject for subject, (_count, expires_at) in modelhub_blob_rate_windows.items() if now >= expires_at]
    for subject in expired:
        modelhub_blob_rate_windows.pop(subject, None)
    while len(modelhub_blob_rate_windows) >= MODELHUB_BLOB_RATE_FALLBACK_MAX_SUBJECTS:
        oldest_subject = next(iter(modelhub_blob_rate_windows))
        modelhub_blob_rate_windows.pop(oldest_subject, None)


def modelhub_blob_rate_limit_fallback(subject: str, limit: int) -> dict[str, str]:
    now = monotonic()
    prune_modelhub_blob_rate_windows(now)
    count, expires_at = modelhub_blob_rate_windows.get(subject, (0, now + MODELHUB_BLOB_RATE_WINDOW_SECONDS))
    if now >= expires_at:
        count = 0
        expires_at = now + MODELHUB_BLOB_RATE_WINDOW_SECONDS
    count += 1
    modelhub_blob_rate_windows[subject] = (count, expires_at)
    reset_seconds = max(1, int(expires_at - now))
    headers = rate_limit_headers(limit, limit - count, reset_seconds)
    if count > limit:
        raise HTTPException(status_code=429, detail="Model Hub blob download rate limit exceeded", headers=headers)
    return headers


async def enforce_modelhub_blob_rate_limit(auth: AuthContext) -> dict[str, str]:
    limit = int(settings.modelhub_blob_requests_per_minute)
    if limit <= 0:
        return {}
    subject = modelhub_blob_rate_subject(auth)
    key = f"b1:modelhub:blob-rate:{subject}"
    if redis_client is None:
        return modelhub_blob_rate_limit_fallback(subject, limit)
    try:
        count = int(await redis_client.incr(key))
        if count == 1:
            await redis_client.expire(key, MODELHUB_BLOB_RATE_WINDOW_SECONDS)
        ttl = int(await redis_client.ttl(key))
    except Exception as exc:  # pragma: no cover - Redis failures fall back to local process protection
        log_event("modelhub_blob_rate_limit_redis_failed", error=exc.__class__.__name__)
        return modelhub_blob_rate_limit_fallback(subject, limit)
    if ttl < 0:
        ttl = MODELHUB_BLOB_RATE_WINDOW_SECONDS
        with suppress(Exception):
            await redis_client.expire(key, MODELHUB_BLOB_RATE_WINDOW_SECONDS)
    headers = rate_limit_headers(limit, limit - count, ttl)
    if count > limit:
        raise HTTPException(status_code=429, detail="Model Hub blob download rate limit exceeded", headers=headers)
    return headers


async def record_audit_event(
    auth: AuthContext,
    event_type: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    summary: str = "",
    metadata: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    remote_addr: str | None = None,
) -> None:
    try:
        row = await database.insert_audit_event(
            {
                "actor_id": auth.subject_id,
                "actor_role": auth.role.value,
                "actor_key_prefix": auth.key_prefix,
                "event_type": event_type,
                "target_type": target_type,
                "target_id": target_id,
                "summary": summary,
                "metadata": metadata or {},
                "correlation_id": correlation_id,
                "remote_addr": remote_addr,
            }
        )
    except Exception as exc:  # pragma: no cover - audit failure must not leak or break the primary action
        log_event("audit_record_failed", event_type=event_type, target_type=target_type, target_id=target_id, error=exc.__class__.__name__)
        return
    log_event("audit_recorded", audit_id=row["id"], event_type=event_type, target_type=target_type, target_id=target_id)


def require_api_client_network_allowed(client: dict[str, Any], request: Request | None) -> None:
    cidr_allowlist = client.get("cidr_allowlist") or []
    if not cidr_allowlist:
        return
    remote_addr = client_host(request)
    if not modelhub_policy.client_ip_allowed_by_cidr(cidr_allowlist, remote_addr):
        raise HTTPException(status_code=403, detail="API client is not permitted from this network")


def normalize_cors_origin(raw_origin: str) -> str:
    value = raw_origin.strip()
    if not value:
        raise ValueError("CORS origin entries cannot be empty")
    if value in {"*", "null"}:
        raise ValueError("CORS origin wildcard/null values are not allowed with credentials")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid CORS origin: {raw_origin}") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"CORS origin must be an http(s) origin: {raw_origin}")
    if parts.username or parts.password:
        raise ValueError("CORS origins cannot contain credentials")
    if parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError("CORS origins must not include a path, query, or fragment")
    host = parts.hostname.lower()
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default_port = (parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)
    netloc = host_part if port is None or default_port else f"{host_part}:{port}"
    return f"{parts.scheme}://{netloc}"


def validate_cors_allow_origins(origins: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for origin in origins:
        try:
            value = normalize_cors_origin(origin)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def validate_network_policy_payload(payload: NetworkPolicyUpdateRequest) -> dict[str, list[str]]:
    return {
        "cors_allow_origins": validate_cors_allow_origins(payload.cors_allow_origins),
        "trusted_proxy_cidrs": validate_modelhub_cidr_allowlist(payload.trusted_proxy_cidrs),
    }


def environment_network_policy_payload() -> dict[str, list[str]]:
    cors_allow_origins: list[str] = []
    for origin in settings.cors_allow_origins:
        try:
            normalized = normalize_cors_origin(origin)
        except ValueError:
            continue
        if normalized not in cors_allow_origins:
            cors_allow_origins.append(normalized)
    try:
        trusted_proxy_cidrs = validate_modelhub_cidr_allowlist(list(settings.trusted_proxy_cidrs))
    except HTTPException:
        trusted_proxy_cidrs = list(settings.trusted_proxy_cidrs)
    return {
        "cors_allow_origins": cors_allow_origins,
        "trusted_proxy_cidrs": trusted_proxy_cidrs,
    }


def effective_network_policy_values() -> dict[str, list[str]]:
    if network_policy_cache is not None:
        return {
            "cors_allow_origins": list(network_policy_cache.get("cors_allow_origins") or []),
            "trusted_proxy_cidrs": list(network_policy_cache.get("trusted_proxy_cidrs") or []),
        }
    return environment_network_policy_payload()


def public_network_policy_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    environment = environment_network_policy_payload()
    effective = {
        "cors_allow_origins": list(row["cors_allow_origins"]) if row is not None else environment["cors_allow_origins"],
        "trusted_proxy_cidrs": list(row["trusted_proxy_cidrs"]) if row is not None else environment["trusted_proxy_cidrs"],
    }
    return jsonable_encoder(
        {
            "id": "default",
            "source": "database" if row is not None else "environment",
            "effective": effective,
            "environment": environment,
            "updated_by": row.get("updated_by") if row is not None else None,
            "created_at": row.get("created_at") if row is not None else None,
            "updated_at": row.get("updated_at") if row is not None else None,
        }
    )


async def load_network_policy_cache() -> dict[str, Any] | None:
    row = await database.get_network_policy_record()
    globals()["network_policy_cache"] = row
    return row


def cors_origin_allowed(origin: str | None) -> str | None:
    if not origin:
        return None
    try:
        normalized = normalize_cors_origin(origin)
    except ValueError:
        return None
    return normalized if normalized in effective_network_policy_values()["cors_allow_origins"] else None


def add_cors_headers(response: Response, allowed_origin: str) -> None:
    response.headers["Access-Control-Allow-Origin"] = allowed_origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = ", ".join(CORS_ALLOW_METHODS)
    response.headers["Access-Control-Allow-Headers"] = ", ".join(CORS_ALLOW_HEADERS)
    response.headers["Access-Control-Expose-Headers"] = ", ".join(CORS_EXPOSE_HEADERS)
    vary = response.headers.get("Vary")
    if not vary:
        response.headers["Vary"] = "Origin"
    elif "origin" not in {part.strip().lower() for part in vary.split(",")}:
        response.headers["Vary"] = f"{vary}, Origin"


def client_host(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    direct_host = modelhub_policy.normalized_ip_literal(request.client.host) or request.client.host
    if modelhub_policy.client_ip_allowed_by_cidr(effective_network_policy_values()["trusted_proxy_cidrs"], request.client.host):
        forwarded_host = forwarded_client_host(request)
        if forwarded_host is not None:
            return forwarded_host
    return direct_host


def forwarded_client_host(request: Request) -> str | None:
    header = request.headers.get("x-forwarded-for", "")
    if header:
        return normalized_forwarded_host(header.split(",", 1)[0].strip())
    forwarded = request.headers.get("forwarded", "")
    if not forwarded:
        return None
    first_entry = forwarded.split(",", 1)[0]
    for part in first_entry.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.lower() == "for":
            return normalized_forwarded_host(value.strip().strip('"'))
    return None


def normalized_forwarded_host(value: str) -> str | None:
    if value.startswith("["):
        host, separator, _port = value[1:].partition("]")
        return modelhub_policy.normalized_ip_literal(host) if separator else None
    if value.count(":") == 1 and "." in value:
        host, _separator, port = value.partition(":")
        if port.isdigit():
            return modelhub_policy.normalized_ip_literal(host)
    return modelhub_policy.normalized_ip_literal(value)


def auth_public_payload(auth: AuthContext | None, *, setup_required: bool, csrf_token: str | None = None) -> dict[str, Any]:
    return {
        "configured": not setup_required,
        "setup_required": setup_required,
        "authenticated": auth is not None,
        "subject_id": auth.subject_id if auth else None,
        "role": auth.role.value if auth else None,
        "scopes": sorted(auth.scopes) if auth else [],
        "csrf_token": csrf_token,
        "session_id": auth.session_id if auth else None,
    }


def set_browser_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_browser_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


async def create_browser_session(user: dict[str, Any], request: Request | None) -> tuple[str, dict[str, Any]]:
    session_token = generate_session_token()
    csrf_token = generate_csrf_token()
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=max(300, settings.session_ttl_seconds))
    session = await database.insert_browser_session(
        {
            "id": f"session_{uuid.uuid4().hex}",
            "user_id": user["id"],
            "session_hash": hash_session_token(session_token),
            "csrf_token": csrf_token,
            "user_agent": (request.headers.get("user-agent", "")[:1000] if request else ""),
            "remote_addr": client_host(request),
            "expires_at": expires_at,
        }
    )
    return session_token, session


async def authenticate_bearer_token(token: str) -> AuthContext:
    if settings.admin_bootstrap_key and hmac.compare_digest(token, settings.admin_bootstrap_key):
        return AuthContext(subject_id="bootstrap-admin", role=Role.ADMIN, scopes=frozenset({"*"}))
    key_prefix = key_prefix_from_token(token)
    if not key_prefix:
        raise HTTPException(status_code=403, detail="invalid or under-scoped token")
    client = await database.get_api_client_by_prefix(key_prefix)
    if client is None or not verify_api_key(token, client["key_salt"], client["key_hash"]):
        raise HTTPException(status_code=403, detail="invalid or under-scoped token")
    require_api_client_network_allowed(client, current_request.get())
    role = Role(client["role"])
    return AuthContext(
        subject_id=client["id"],
        role=role,
        scopes=frozenset(client["scopes"]),
        key_prefix=client["key_prefix"],
        default_b1_tools=tuple(str(tool) for tool in client.get("default_b1_tools") or [] if str(tool).strip()),
    )


async def authenticate_browser_session(request: Request | None) -> AuthContext | None:
    if request is None:
        return None
    session_token = request.cookies.get(settings.session_cookie_name)
    if not session_token:
        return None
    session = await database.get_browser_session_by_hash(hash_session_token(session_token))
    if session is None:
        return None
    user = await database.get_user(session["user_id"])
    if user is None or user.get("disabled_at") is not None:
        raise HTTPException(status_code=403, detail="invalid browser session")
    role = Role(user["role"])
    return AuthContext(
        subject_id=user["id"],
        role=role,
        scopes=scopes_for_role(role),
        session_id=session["id"],
        csrf_token=session["csrf_token"],
    )


async def authenticate(authorization: str | None = Header(default=None)) -> AuthContext:
    if not isinstance(authorization, str):
        authorization = None
    if settings.dev_auth_bypass:
        return AuthContext(subject_id="dev-admin", role=Role.ADMIN, scopes=frozenset({"*"}))
    if not authorization or not authorization.startswith("Bearer "):
        session_auth = await authenticate_browser_session(current_request.get())
        if session_auth is not None:
            return session_auth
        raise HTTPException(status_code=401, detail="missing bearer token or browser session")
    token = authorization.removeprefix("Bearer ").strip()
    return await authenticate_bearer_token(token)


async def optional_authenticate(authorization: str | None = Header(default=None)) -> AuthContext | None:
    try:
        return await authenticate(authorization)
    except HTTPException:
        return None


async def csrf_failure_response(request: Request) -> Response | None:
    if request.method.upper() in SAFE_METHODS or request.url.path in CSRF_EXEMPT_PATHS:
        return None
    if request.headers.get("authorization", "").startswith("Bearer "):
        return None
    session_token = request.cookies.get(settings.session_cookie_name)
    if not session_token:
        return None
    session = await database.get_browser_session_by_hash(hash_session_token(session_token))
    if session is None:
        return JSONResponse(status_code=403, content={"detail": "invalid browser session"})
    provided = request.headers.get("X-B1-CSRF", "")
    expected = session["csrf_token"]
    if not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse(status_code=403, content={"detail": "missing or invalid CSRF token"})
    return None


@app.middleware("http")
async def request_context_and_csrf_middleware(request: Request, call_next):
    token = current_request.set(request)
    try:
        allowed_origin = cors_origin_allowed(request.headers.get("origin"))
        is_cors_preflight = request.method.upper() == "OPTIONS" and bool(request.headers.get("access-control-request-method"))
        if is_cors_preflight:
            if allowed_origin is None:
                return JSONResponse(status_code=400, content={"detail": "CORS origin is not allowed"})
            response = Response(status_code=204)
            add_cors_headers(response, allowed_origin)
            return response
        failure = await csrf_failure_response(request)
        if failure is not None:
            if allowed_origin is not None:
                add_cors_headers(failure, allowed_origin)
            return failure
        response = await call_next(request)
        if allowed_origin is not None:
            add_cors_headers(response, allowed_origin)
        return response
    finally:
        current_request.reset(token)


def require_scope(auth: AuthContext, scope: str) -> None:
    if not auth.has_scope(scope):
        raise HTTPException(status_code=403, detail=f"missing required scope: {scope}")


def prometheus_scrape_token_allowed(authorization: str | None) -> bool:
    if not settings.prometheus_scrape_token or not isinstance(authorization, str):
        return False
    if not authorization.startswith("Bearer "):
        return False
    token = authorization.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, settings.prometheus_scrape_token)


def compatibility_header_value(headers: Any) -> str:
    value = headers.get("x-b1-compatibility", "") if headers is not None else ""
    return value.strip().lower() if isinstance(value, str) else ""


def compatibility_is_legacy_comfy(compatibility: str) -> bool:
    return compatibility == "comfyui-legacy-8188"


def compatibility_scope_for_method(method: str) -> str:
    return "jobs:read" if method.upper() in SAFE_METHODS else "jobs:write"


async def require_http_compatibility_access(request: Request, compatibility: str, scope: str) -> AuthContext | None:
    if compatibility_is_legacy_comfy(compatibility):
        return None
    auth = await authenticate(request.headers.get("authorization"))
    require_scope(auth, scope)
    return auth


async def require_websocket_compatibility_access(websocket: WebSocket, compatibility: str, scope: str) -> bool:
    if compatibility_is_legacy_comfy(compatibility):
        return True
    try:
        auth = await authenticate(websocket.headers.get("authorization"))
        require_scope(auth, scope)
    except HTTPException:
        await websocket.close(code=1008)
        return False
    return True


def require_queue_admin(auth: AuthContext) -> None:
    if auth.has_scope("*") or auth.role in {Role.ADMIN, Role.OPERATOR}:
        return
    raise HTTPException(status_code=403, detail="admin job queue access requires admin or operator role")


def require_runtime_admin(auth: AuthContext) -> None:
    if auth.has_scope("*") or auth.role in {Role.ADMIN, Role.OPERATOR}:
        return
    raise HTTPException(status_code=403, detail="runtime administration requires admin or operator role")


def require_model_admin(auth: AuthContext) -> None:
    if auth.has_scope("*") or auth.role in {Role.ADMIN, Role.OPERATOR}:
        return
    raise HTTPException(status_code=403, detail="model administration requires admin or operator role")


def require_workflow_governance_reader(auth: AuthContext) -> None:
    if auth.has_scope("*") or auth.role in {Role.ADMIN, Role.OPERATOR, Role.CREATOR}:
        return
    raise HTTPException(status_code=403, detail="workflow governance access requires admin, operator, or creator role")


def subject_can_read_job(auth: AuthContext, job: dict[str, Any]) -> bool:
    return auth.has_scope("*") or job.get("owner_id") == auth.subject_id


def require_job_owner_or_admin(auth: AuthContext, job: dict[str, Any]) -> None:
    if not subject_can_read_job(auth, job):
        raise HTTPException(status_code=403, detail="job belongs to a different owner")


def subject_can_read_runtime_reservation(auth: AuthContext, reservation: dict[str, Any]) -> bool:
    return auth.has_scope("*") or auth.role in {Role.ADMIN, Role.OPERATOR} or reservation.get("owner_id") == auth.subject_id


def require_runtime_reservation_owner_or_admin(auth: AuthContext, reservation: dict[str, Any]) -> None:
    if not subject_can_read_runtime_reservation(auth, reservation):
        raise HTTPException(status_code=403, detail="runtime reservation belongs to a different owner")


def job_mutation_conflict(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


async def proxy_http_bytes(
    base_url: str,
    path: str,
    request: Request,
    body: bytes | None = None,
    timeout_seconds: float = 120.0,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return await proxy_http_bytes_to_url(url, request, body=body, timeout_seconds=timeout_seconds, extra_headers=extra_headers)


async def proxy_http_bytes_to_url(
    url: str,
    request: Request,
    body: bytes | None = None,
    timeout_seconds: float = 120.0,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    request_body = await request.body() if body is None else body
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in RUNTIME_PROXY_FORBIDDEN_REQUEST_HEADERS
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            proxied = await client.request(request.method, url, content=request_body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"runtime proxy failure: {exc.__class__.__name__}") from exc
    response_headers = {
        key: value
        for key, value in proxied.headers.items()
        if key.lower()
        in {
            "accept-ranges",
            "cache-control",
            "content-disposition",
            "content-length",
            "content-range",
            "content-type",
            "etag",
            "last-modified",
            "x-checksum-sha256",
            "x-b1-cpu-audio-engine",
            "x-b1-gpu-lease-required",
            "x-b1-placeholder",
        }
    }
    return Response(content=proxied.content, status_code=proxied.status_code, headers=response_headers)


async def proxy_http(base_url: str, path: str, request: Request, extra_headers: dict[str, str] | None = None) -> Response:
    if extra_headers is None:
        return await proxy_http_bytes(base_url, path, request)
    return await proxy_http_bytes(base_url, path, request, extra_headers=extra_headers)


def artifact_server_auth_headers() -> dict[str, str]:
    token = settings.artifact_server_token.strip()
    if not token:
        raise HTTPException(status_code=503, detail="artifact-server service token is not configured")
    return {"Authorization": f"Bearer {token}"}


async def read_bounded_request_body(request: Request, max_size_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_size_bytes:
            raise HTTPException(status_code=413, detail=f"upload exceeds {max_size_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_bounded_upload_file(upload: UploadFile, max_size_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size_bytes:
                raise HTTPException(status_code=413, detail=f"upload exceeds {max_size_bytes} bytes")
            chunks.append(chunk)
    finally:
        with suppress(Exception):
            await upload.close()
    return b"".join(chunks)


def staged_input_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "exceeds" in message:
        return HTTPException(status_code=413, detail=message)
    if "unsupported media input type" in message or "unsupported audio input type" in message:
        return HTTPException(status_code=415, detail=message)
    return HTTPException(status_code=422, detail=message)


def sniff_transcription_audio(content: bytes, declared_mime_type: str | None = None) -> str:
    if not content:
        raise HTTPException(status_code=422, detail="audio transcription upload is empty")
    try:
        return media_artifacts.require_allowed_audio_input_mime_type(content, declared_mime_type)
    except ValueError as exc:
        raise staged_input_error(exc) from exc


def admission_error_response(exc: admission.AdmissionDeniedError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "code": exc.code,
            "message": str(exc),
            **exc.details,
        },
    )


def base_admission_policy() -> admission.AdmissionPolicy:
    return admission.policy_from_settings(settings)


def admission_policy() -> admission.AdmissionPolicy:
    return admission_policy_override or base_admission_policy()


def admission_policy_from_record(row: dict[str, Any]) -> admission.AdmissionPolicy:
    fields = admission.AdmissionPolicy.__dataclass_fields__
    return admission.AdmissionPolicy(**{name: int(row[name]) for name in fields})


def admission_policy_dict(policy: admission.AdmissionPolicy) -> dict[str, Any]:
    return {name: getattr(policy, name) for name in admission.AdmissionPolicy.__dataclass_fields__}


def admission_policy_hard_bounds() -> dict[str, dict[str, int]]:
    base = base_admission_policy()
    tib = 1024**4
    return {
        "max_queued_jobs_per_owner": {"minimum": 0, "maximum": max(base.max_queued_jobs_per_owner, 1000)},
        "max_active_jobs_per_owner": {"minimum": 0, "maximum": max(base.max_active_jobs_per_owner, 128)},
        "max_jobs_per_hour_per_owner": {"minimum": 0, "maximum": max(base.max_jobs_per_hour_per_owner, 10000)},
        "max_queued_jobs_global": {"minimum": 0, "maximum": max(base.max_queued_jobs_global, 10000)},
        "artifact_storage_max_bytes": {"minimum": 0, "maximum": max(base.artifact_storage_max_bytes, 8 * tib)},
        "artifact_storage_reserve_bytes": {"minimum": 0, "maximum": max(base.artifact_storage_reserve_bytes, 2 * tib)},
    }


def validate_admission_policy_candidate(policy: admission.AdmissionPolicy) -> list[str]:
    bounds = admission_policy_hard_bounds()
    values = admission_policy_dict(policy)
    errors: list[str] = []
    for field, value in values.items():
        minimum = bounds[field]["minimum"]
        maximum = bounds[field]["maximum"]
        if value < minimum or value > maximum:
            errors.append(f"{field} must be between {minimum} and {maximum}")
    if policy.max_queued_jobs_per_owner and policy.max_queued_jobs_global and policy.max_queued_jobs_global < policy.max_queued_jobs_per_owner:
        errors.append("max_queued_jobs_global must be zero or at least max_queued_jobs_per_owner")
    if policy.max_active_jobs_per_owner and policy.max_queued_jobs_per_owner and policy.max_active_jobs_per_owner > policy.max_queued_jobs_per_owner:
        errors.append("max_active_jobs_per_owner must be zero or no greater than max_queued_jobs_per_owner")
    return errors


def admission_policy_from_update(payload: AdmissionPolicyUpdateRequest) -> admission.AdmissionPolicy:
    return admission.AdmissionPolicy(**payload.model_dump())


async def load_admission_policy_override() -> admission.AdmissionPolicy | None:
    global admission_policy_override
    row = await database.get_admission_policy_record()
    admission_policy_override = admission_policy_from_record(row) if row else None
    return admission_policy_override


def public_admission_policy_payload(row: dict[str, Any] | None = None) -> dict[str, Any]:
    effective = admission_policy()
    default = base_admission_policy()
    return {
        "id": "default",
        "source": "database" if admission_policy_override is not None else "environment",
        "effective": admission_policy_dict(effective),
        "default": admission_policy_dict(default),
        "bounds": admission_policy_hard_bounds(),
        "override": jsonable_encoder(row) if row else None,
    }


def artifact_storage_snapshot(policy: admission.AdmissionPolicy | None = None) -> admission.ArtifactStorageSnapshot:
    effective_policy = policy or admission_policy()
    return admission.artifact_storage_snapshot(Path(settings.artifact_root), effective_policy)


def enforce_artifact_storage_headroom(incoming_bytes: int = 0) -> None:
    policy = admission_policy()
    try:
        admission.enforce_artifact_storage_admission(policy, artifact_storage_snapshot(policy), incoming_bytes=incoming_bytes)
    except admission.AdmissionDeniedError as exc:
        raise admission_error_response(exc) from exc


async def hardware_resource_policy_snapshot() -> dict[str, Any]:
    agent_metrics, metrics_error = await runtime_agent_get("/v1/metrics")
    result = selftest_policy.hardware_resource_policy_check(
        agent_metrics,
        resource_policy_dict(resource_policy()),
        settings.runtime_deployment_mode,
    )
    if metrics_error:
        result = {
            **result,
            "data": {
                **(result.get("data") if isinstance(result.get("data"), dict) else {}),
                "runtime_agent_error": metrics_error,
            },
        }
    return result


async def enforce_gpu_hardware_admission(resolution: RuntimeResolution | None) -> None:
    if resolution is None or not resolution.requires_gpu:
        return
    if settings.runtime_deployment_mode != "production":
        return
    check = await hardware_resource_policy_snapshot()
    if check.get("status") == "ok":
        return
    raise admission_error_response(
        admission.AdmissionDeniedError(
            "hardware_resource_policy",
            "GPU admission blocked by hardware resource policy",
            status_code=503,
            details={"hardware_resource_policy": check},
        )
    )


async def queue_admission_snapshot(owner_id: str) -> admission.QueueAdmissionSnapshot:
    since = datetime.now(tz=UTC) - timedelta(hours=1)
    owner_queued, owner_active, owner_recent, global_queued = await asyncio.gather(
        database.count_jobs(owner_id=owner_id, states=admission.QUEUE_STATES),
        database.count_jobs(owner_id=owner_id, states=admission.ACTIVE_STATES),
        database.count_jobs(owner_id=owner_id, created_after=since),
        database.count_jobs(states=admission.QUEUE_STATES),
    )
    return admission.QueueAdmissionSnapshot(
        owner_id=owner_id,
        owner_queued_jobs=owner_queued,
        owner_active_jobs=owner_active,
        owner_jobs_last_hour=owner_recent,
        global_queued_jobs=global_queued,
    )


async def admission_report(owner_id: str | None = None) -> dict[str, Any]:
    policy = admission_policy()
    queue = await queue_admission_snapshot(owner_id) if owner_id else None
    report = admission.public_report(policy, queue=queue, storage=artifact_storage_snapshot(policy))
    report["policy_source"] = "database" if admission_policy_override is not None else "environment"
    report["bounds"] = admission_policy_hard_bounds()
    report["hardware_resource_policy"] = await hardware_resource_policy_snapshot()
    return report


async def enforce_queue_admission(owner_id: str) -> None:
    policy = admission_policy()
    try:
        admission.enforce_queue_admission(policy, await queue_admission_snapshot(owner_id))
    except admission.AdmissionDeniedError as exc:
        raise admission_error_response(exc) from exc


def stage_media_input(
    auth: AuthContext,
    *,
    field_name: str,
    content: bytes,
    declared_mime_type: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    enforce_artifact_storage_headroom(len(content))
    try:
        return media_artifacts.write_staged_input_bytes(
            Path(settings.artifact_root),
            owner_id=auth.subject_id,
            field_name=field_name,
            content=content,
            declared_mime_type=declared_mime_type,
            filename=filename,
            max_size_bytes=settings.upload_max_bytes,
        )
    except ValueError as exc:
        raise staged_input_error(exc) from exc


def append_payload_value(payload: dict[str, Any], key: str, value: Any) -> None:
    if key in payload:
        existing = payload[key]
        if isinstance(existing, list):
            existing.append(value)
        else:
            payload[key] = [existing, value]
        return
    payload[key] = value


def is_upload_file_value(value: Any) -> bool:
    return isinstance(value, UploadFile) or (hasattr(value, "filename") and hasattr(value, "read") and hasattr(value, "close"))


async def media_image_input_from_request(request: Request, auth: AuthContext) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="image edit body must be a JSON object")
        return payload
    if content_type == "multipart/form-data":
        try:
            form = await request.form()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid multipart body: {exc.__class__.__name__}") from exc
        payload: dict[str, Any] = {}
        for key, value in form.multi_items():
            if is_upload_file_value(value):
                content = await read_bounded_upload_file(value, settings.upload_max_bytes)
                append_payload_value(
                    payload,
                    key,
                    stage_media_input(
                        auth,
                        field_name=key,
                        content=content,
                        declared_mime_type=value.content_type,
                        filename=value.filename,
                    ),
                )
            else:
                append_payload_value(payload, key, str(value))
        return payload
    body = await read_bounded_request_body(request, settings.upload_max_bytes)
    return {
        "image": stage_media_input(
            auth,
            field_name="image",
            content=body,
            declared_mime_type=request.headers.get("content-type"),
            filename=request.headers.get("X-B1-Filename"),
        )
    }


async def image_edit_input_from_request(request: Request, auth: AuthContext) -> dict[str, Any]:
    return await media_image_input_from_request(request, auth)


async def transcription_input_from_request(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        body = await request.body()
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="audio transcription body must be a JSON object")
        return payload
    if content_type == "multipart/form-data":
        try:
            form = await request.form()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid multipart body: {exc.__class__.__name__}") from exc
        payload: dict[str, Any] = {}
        uploaded = False
        for key, value in form.multi_items():
            if is_upload_file_value(value):
                if uploaded:
                    raise HTTPException(status_code=422, detail="audio transcription accepts one upload file")
                declared_mime_type = value.content_type or "application/octet-stream"
                content = await read_bounded_upload_file(value, settings.upload_max_bytes)
                mime_type = sniff_transcription_audio(content, declared_mime_type)
                payload["audio"] = base64.b64encode(content).decode("ascii")
                payload["audio_mime_type"] = mime_type
                if value.filename:
                    payload["filename"] = value.filename
                uploaded = True
            else:
                append_payload_value(payload, key, str(value))
        return payload
    body = await read_bounded_request_body(request, settings.upload_max_bytes)
    if not body:
        return {}
    mime_type = sniff_transcription_audio(body, request.headers.get("content-type"))
    return {
        "audio": base64.b64encode(body).decode("ascii"),
        "audio_mime_type": mime_type,
        "filename": request.headers.get("X-B1-Filename"),
    }


def websocket_runtime_url(base_url: str, path: str, query: str = "") -> str:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    parts = urlsplit(url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, query, ""))


def websocket_forward_headers(websocket: WebSocket) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in websocket.headers.items():
        lowered = key.lower()
        if lowered in RUNTIME_PROXY_FORBIDDEN_REQUEST_HEADERS or lowered.startswith("sec-websocket"):
            continue
        headers[key] = value
    return headers


def safe_artifact_segment(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {".", "_", "-"}) else "_" for ch in value)
    cleaned = cleaned.strip("._-")
    return cleaned[:120] or fallback


def prefer_artifact_update(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    current_stored = current.get("source") == "artifact_store" or current.get("ingest_status") == "stored"
    candidate_stored = candidate.get("source") == "artifact_store" or candidate.get("ingest_status") == "stored"
    if candidate_stored and not current_stored:
        return candidate
    if candidate_stored and current.get("bytes") is None and candidate.get("bytes") is not None:
        return candidate
    if current.get("ingest_status") == "failed" and candidate.get("ingest_status") != "failed":
        return candidate
    return current


def merge_job_artifacts(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = list(existing)
    by_url = {artifact.get("url"): index for index, artifact in enumerate(merged) if isinstance(artifact, dict) and artifact.get("url")}
    for artifact in additions:
        url = artifact.get("url")
        if url and url in by_url:
            existing_index = by_url[url]
            existing_artifact = merged[existing_index]
            if isinstance(existing_artifact, dict):
                merged[existing_index] = prefer_artifact_update(existing_artifact, artifact)
            continue
        if url:
            merged.append(artifact)
            by_url[url] = len(merged) - 1
    return merged


def comfyui_history_record(prompt_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(payload.get(prompt_id), dict):
        return payload[prompt_id]
    if isinstance(payload.get("outputs"), dict):
        return payload
    return None


def comfyui_artifacts_from_outputs(prompt_id: str, outputs: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for node_id, output in outputs.items():
        if not isinstance(output, dict):
            continue
        for output_key, kind in COMFYUI_OUTPUT_KEYS.items():
            items = output.get(output_key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    continue
                index = len(artifacts)
                filename = item["filename"]
                subfolder = item.get("subfolder") if isinstance(item.get("subfolder"), str) else ""
                file_type = item.get("type") if isinstance(item.get("type"), str) else "output"
                prompt_segment = safe_artifact_segment(prompt_id, "prompt")
                file_segment = safe_artifact_segment(filename, f"{kind}-{index}")
                relative = f"comfyui/{prompt_segment}/{index}-{file_segment}"
                mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                query = urlencode({"filename": filename, "subfolder": subfolder, "type": file_type})
                artifacts.append(
                    {
                        "id": f"artifact_{prompt_segment}_{index}",
                        "kind": kind,
                        "mime_type": mime_type,
                        "path": relative,
                        "url": f"/artifacts/{relative}",
                        "runtime": "comfyui",
                        "source": "comfyui_view",
                        "source_url": f"/view?{query}",
                        "bytes": None,
                        "sha256": None,
                        "comfyui": {
                            "prompt_id": prompt_id,
                            "node_id": str(node_id),
                            "output_key": output_key,
                            "filename": filename,
                            "subfolder": subfolder,
                            "type": file_type,
                        },
                    }
                )
    return artifacts


def comfyui_artifacts_from_history(prompt_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    record = comfyui_history_record(prompt_id, payload)
    outputs = record.get("outputs") if record else None
    return comfyui_artifacts_from_outputs(prompt_id, outputs) if isinstance(outputs, dict) else []


def job_artifact_by_url(job: dict[str, Any], artifact_url: str) -> dict[str, Any] | None:
    for artifact in job.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("url") == artifact_url:
            return artifact
    return None


def comfyui_view_path_for_artifact(artifact: dict[str, Any]) -> str | None:
    if artifact.get("source") != "comfyui_view":
        return None
    comfyui = artifact.get("comfyui")
    if not isinstance(comfyui, dict):
        return None
    filename = comfyui.get("filename")
    if not isinstance(filename, str) or not filename:
        return None
    subfolder = comfyui.get("subfolder") if isinstance(comfyui.get("subfolder"), str) else ""
    file_type = comfyui.get("type") if isinstance(comfyui.get("type"), str) else "output"
    return f"/view?{urlencode({'filename': filename, 'subfolder': subfolder, 'type': file_type})}"


def artifact_store_path(relative_path: str) -> Path:
    return media_artifacts.artifact_store_path(Path(settings.artifact_root), relative_path)


async def ingest_comfyui_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    if artifact.get("source") != "comfyui_view":
        return artifact
    relative = artifact.get("path")
    if not isinstance(relative, str) or not relative:
        return {**artifact, "ingest_status": "failed", "ingest_error": "missing artifact path"}
    view_path = comfyui_view_path_for_artifact(artifact)
    if not view_path:
        return {**artifact, "ingest_status": "failed", "ingest_error": "missing ComfyUI view metadata"}

    try:
        target = artifact_store_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = artifact_store_path(relative)
    except (OSError, ValueError) as exc:
        return {**artifact, "ingest_status": "failed", "ingest_error": exc.__class__.__name__}
    digest = hashlib.sha256()
    byte_count = 0
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    url = f"{settings.comfyui_url.rstrip('/')}/{view_path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return {**artifact, "ingest_status": "failed", "ingest_error": f"ComfyUI /view returned HTTP {response.status_code}"}
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        digest.update(chunk)
                        byte_count += len(chunk)
        target = artifact_store_path(relative)
        media_artifacts.ensure_regular_file_replace_target(target)
        temporary.replace(target)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        with suppress(OSError):
            temporary.unlink()
        return {**artifact, "ingest_status": "failed", "ingest_error": exc.__class__.__name__}

    return {
        **artifact,
        "source": "artifact_store",
        "storage": "artifact-server",
        "source_url": artifact.get("source_url"),
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
        "ingest_status": "stored",
    }


async def ingest_comfyui_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ingested: list[dict[str, Any]] = []
    for artifact in artifacts:
        ingested.append(await ingest_comfyui_artifact(artifact))
    return ingested


def comfyui_prompt_id_from_event(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("prompt_id"), str):
        return data["prompt_id"]
    if isinstance(payload.get("prompt_id"), str):
        return payload["prompt_id"]
    return None


def comfyui_event_job_update(payload: dict[str, Any]) -> dict[str, Any] | None:
    event_type = payload.get("type")
    data = payload.get("data")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    if event_type == "execution_start":
        return {"state": JobState.RUNNING.value, "stage": "comfyui_execution_start", "progress": 70}
    if event_type == "progress":
        value = data.get("value")
        maximum = data.get("max")
        if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
            bounded = max(0.0, min(1.0, float(value) / float(maximum)))
            return {"state": JobState.RUNNING.value, "stage": "comfyui_progress", "progress": 70 + int(bounded * 18)}
        return {"state": JobState.RUNNING.value, "stage": "comfyui_progress", "progress": 72}
    if event_type == "executing":
        node = data.get("node")
        if node is None:
            return {"state": JobState.RUNNING.value, "stage": "comfyui_execution_finishing", "progress": 89}
        return {"state": JobState.RUNNING.value, "stage": f"comfyui_executing_{str(node)[:80]}", "progress": 75}
    if event_type == "executed":
        return {"state": JobState.RUNNING.value, "stage": "comfyui_node_executed", "progress": 82}
    if event_type == "execution_error":
        message = data.get("exception_message") or data.get("exception_type") or "ComfyUI execution error"
        return {
            "state": JobState.FAILED.value,
            "stage": "comfyui_execution_error",
            "progress": 100,
            "failure_category": "comfyui_execution_error",
            "failure_message": str(message)[:500],
        }
    return None


async def persist_comfyui_ws_event(message: str) -> None:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    prompt_id = comfyui_prompt_id_from_event(payload)
    if not prompt_id:
        return
    job = await database.get_job_by_native_prompt_id(prompt_id)
    if job is None or job.get("state") in TERMINAL_JOB_STATES:
        return
    update = comfyui_event_job_update(payload) or {}
    data = payload.get("data")
    if payload.get("type") == "executed" and isinstance(data, dict) and isinstance(data.get("output"), dict):
        artifacts = comfyui_artifacts_from_outputs(prompt_id, {str(data.get("node") or "unknown"): data["output"]})
        if artifacts:
            try:
                artifacts = await ingest_comfyui_artifacts(artifacts)
            except Exception as exc:
                artifacts = [{**artifact, "ingest_status": "failed", "ingest_error": exc.__class__.__name__} for artifact in artifacts]
            update["artifacts"] = merge_job_artifacts(job.get("artifacts") or [], artifacts)
    if update:
        await database.update_job(job["id"], **update)


async def bridge_runtime_websocket(
    websocket: WebSocket,
    *,
    base_url: str,
    path: str,
    log_name: str,
    text_event_handler: Any | None = None,
) -> None:
    upstream_url = websocket_runtime_url(base_url, path, websocket.url.query)
    upstream_headers = websocket_forward_headers(websocket)

    async def close_browser(code: int) -> None:
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=code)

    try:
        async with websocket_connect(upstream_url, additional_headers=upstream_headers, max_size=None, open_timeout=10.0) as upstream:
            await websocket.accept()
            log_event(f"{log_name}_ws_connected", upstream_url=upstream_url)

            async def browser_to_runtime() -> None:
                try:
                    while True:
                        message = await websocket.receive()
                        message_type = message.get("type")
                        if message_type == "websocket.disconnect":
                            await upstream.close()
                            return
                        if message_type != "websocket.receive":
                            continue
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])
                except (WebSocketDisconnect, ConnectionClosed):
                    with suppress(Exception):
                        await upstream.close()

            async def runtime_to_browser() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            text = str(message)
                            await websocket.send_text(text)
                            if text_event_handler is not None:
                                await text_event_handler(text)
                except (WebSocketDisconnect, ConnectionClosed):
                    return
                finally:
                    await close_browser(1000)

            tasks = {
                asyncio.create_task(browser_to_runtime(), name=f"b1-{log_name}-ws-browser-to-runtime"),
                asyncio.create_task(runtime_to_browser(), name=f"b1-{log_name}-ws-runtime-to-browser"),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                with suppress(asyncio.CancelledError, ConnectionClosed, WebSocketDisconnect):
                    await task
    except Exception as exc:
        log_event(f"{log_name}_ws_bridge_failed", upstream_url=upstream_url, error=exc.__class__.__name__)
        await close_browser(1011)


async def bridge_comfyui_websocket(websocket: WebSocket) -> None:
    await bridge_runtime_websocket(
        websocket,
        base_url=settings.comfyui_url,
        path="/ws",
        log_name="comfyui",
        text_event_handler=persist_comfyui_ws_event,
    )


async def bridge_voicebox_websocket(websocket: WebSocket, path: str) -> None:
    await bridge_runtime_websocket(
        websocket,
        base_url=settings.voicebox_url,
        path=path,
        log_name="voicebox",
    )


def base_resource_policy() -> ResourcePolicy:
    return ResourcePolicy(
        gpu_total_vram_gib=settings.gpu_total_vram_gib,
        gpu_usable_vram_gib=settings.gpu_usable_vram_gib,
        gpu_reserve_vram_gib=settings.gpu_reserve_vram_gib,
        gpu_max_active_pipelines=settings.gpu_max_active_pipelines,
        host_total_ram_gib=settings.host_total_ram_gib,
        host_reserve_ram_gib=settings.host_reserve_ram_gib,
        llm_default_context=settings.llm_default_context,
        llm_maximum_context=settings.llm_maximum_context,
        llm_default_parallel_requests=settings.llm_default_parallel_requests,
        comfyui_maximum_parallel_jobs=settings.comfyui_maximum_parallel_jobs,
        comfyui_maximum_batch_size=settings.comfyui_maximum_batch_size,
        cpu_residency_enabled=settings.cpu_residency_enabled,
        cpu_residency_max_ram_gib=settings.cpu_residency_max_ram_gib,
        cpu_resident_aliases=tuple(settings.cpu_resident_aliases),
    )


def resource_policy() -> ResourcePolicy:
    return resource_policy_override or base_resource_policy()


def resource_policy_from_record(row: dict[str, Any]) -> ResourcePolicy:
    fields = ResourcePolicy.__dataclass_fields__
    defaults = base_resource_policy()
    values: dict[str, Any] = {}
    for name in fields:
        value = row.get(name, getattr(defaults, name))
        if name == "cpu_resident_aliases":
            value = tuple(str(item) for item in (value or ()))
        values[name] = value
    return ResourcePolicy(**values)


def resource_policy_dict(policy: ResourcePolicy) -> dict[str, Any]:
    payload = {name: getattr(policy, name) for name in ResourcePolicy.__dataclass_fields__}
    payload["cpu_resident_aliases"] = list(policy.cpu_resident_aliases)
    return payload


def resource_policy_hard_bounds() -> dict[str, dict[str, Any]]:
    base = base_resource_policy()
    return {
        "gpu_total_vram_gib": {"minimum": 1.0, "maximum": base.gpu_total_vram_gib},
        "gpu_usable_vram_gib": {"minimum": 1.0, "maximum": base.gpu_total_vram_gib},
        "gpu_reserve_vram_gib": {"minimum": 0.5, "maximum": max(0.5, base.gpu_total_vram_gib - 1.0)},
        "gpu_max_active_pipelines": {"minimum": 1, "maximum": 1},
        "host_total_ram_gib": {"minimum": 8.0, "maximum": base.host_total_ram_gib},
        "host_reserve_ram_gib": {"minimum": 4.0, "maximum": max(4.0, base.host_total_ram_gib - 4.0)},
        "llm_default_context": {"minimum": 512, "maximum": base.llm_maximum_context},
        "llm_maximum_context": {"minimum": 512, "maximum": base.llm_maximum_context},
        "llm_default_parallel_requests": {"minimum": 1, "maximum": max(1, base.llm_default_parallel_requests)},
        "comfyui_maximum_parallel_jobs": {"minimum": 1, "maximum": 1},
        "comfyui_maximum_batch_size": {"minimum": 1, "maximum": 1},
        "cpu_residency_enabled": {"type": "boolean"},
        "cpu_residency_max_ram_gib": {"minimum": 0.0, "maximum": max(0.0, base.host_usable_ram_gib)},
        "cpu_resident_aliases": {"type": "string-list", "max_items": 32, "pattern": r"^[a-z0-9][a-z0-9._-]{1,127}$"},
    }


def validate_resource_policy_candidate(policy: ResourcePolicy) -> list[str]:
    bounds = resource_policy_hard_bounds()
    errors: list[str] = []
    values = resource_policy_dict(policy)
    for field, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        minimum = bounds[field]["minimum"]
        maximum = bounds[field]["maximum"]
        if value < minimum or value > maximum:
            errors.append(f"{field} must be between {minimum} and {maximum}")
    if policy.gpu_usable_vram_gib + policy.gpu_reserve_vram_gib > policy.gpu_total_vram_gib:
        errors.append("gpu_usable_vram_gib plus gpu_reserve_vram_gib must not exceed gpu_total_vram_gib")
    if policy.host_reserve_ram_gib >= policy.host_total_ram_gib:
        errors.append("host_reserve_ram_gib must be lower than host_total_ram_gib")
    if policy.llm_default_context > policy.llm_maximum_context:
        errors.append("llm_default_context must not exceed llm_maximum_context")
    if policy.gpu_max_active_pipelines != 1:
        errors.append("gpu_max_active_pipelines must remain 1 for the single-GPU safety profile")
    if policy.comfyui_maximum_parallel_jobs != 1:
        errors.append("comfyui_maximum_parallel_jobs must remain 1 for the RTX 3060 safety profile")
    if policy.comfyui_maximum_batch_size != 1:
        errors.append("comfyui_maximum_batch_size must remain 1 for the RTX 3060 safety profile")
    if policy.cpu_residency_max_ram_gib > policy.host_usable_ram_gib:
        errors.append("cpu_residency_max_ram_gib must fit outside the protected host RAM reserve")
    if len(policy.cpu_resident_aliases) > 32:
        errors.append("cpu_resident_aliases must contain at most 32 aliases")
    if len(policy.cpu_resident_aliases) != len(set(policy.cpu_resident_aliases)):
        errors.append("cpu_resident_aliases must not contain duplicates")
    alias_pattern = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
    if any(not alias_pattern.match(alias) for alias in policy.cpu_resident_aliases):
        errors.append("cpu_resident_aliases must contain only model-alias identifiers")
    return errors


def resource_policy_from_update(payload: ResourcePolicyUpdateRequest) -> ResourcePolicy:
    values = payload.model_dump()
    values["cpu_resident_aliases"] = tuple(values.get("cpu_resident_aliases") or ())
    return ResourcePolicy(**values)


def apply_resource_policy_to_live_runners(policy: ResourcePolicy) -> None:
    reserve_mib = int(max(0.0, policy.gpu_reserve_vram_gib) * 1024)
    for runner in job_runners:
        if isinstance(runner, GpuJobRunner):
            runner.reserve_vram_mib = reserve_mib


async def load_resource_policy_override() -> ResourcePolicy | None:
    global resource_policy_override
    row = await database.get_resource_policy_record()
    resource_policy_override = resource_policy_from_record(row) if row else None
    apply_resource_policy_to_live_runners(resource_policy())
    return resource_policy_override


def public_resource_policy_payload(row: dict[str, Any] | None = None) -> dict[str, Any]:
    effective = resource_policy()
    default = base_resource_policy()
    return {
        "id": "default",
        "source": "database" if resource_policy_override is not None else "environment",
        "effective": resource_policy_dict(effective),
        "default": resource_policy_dict(default),
        "bounds": resource_policy_hard_bounds(),
        "override": jsonable_encoder(row) if row else None,
    }


def default_maintenance_state() -> dict[str, Any]:
    return {
        "id": "default",
        "enabled": False,
        "reason": "",
        "started_at": None,
        "ended_at": None,
        "updated_by": None,
        "created_at": None,
        "updated_at": None,
    }


def public_maintenance_state(row: dict[str, Any] | None = None) -> dict[str, Any]:
    state = default_maintenance_state()
    if row:
        state.update(row)
    return {
        **jsonable_encoder(state),
        "source": "database" if row else "default",
    }


def current_maintenance_state() -> dict[str, Any]:
    return public_maintenance_state(maintenance_state_cache)


async def load_maintenance_state_cache() -> dict[str, Any]:
    global maintenance_state_cache
    maintenance_state_cache = await database.get_maintenance_state()
    return current_maintenance_state()


def require_not_in_maintenance(operation: str) -> None:
    state = current_maintenance_state()
    if not state["enabled"]:
        return
    raise HTTPException(
        status_code=503,
        detail={
            "message": "maintenance mode is active",
            "operation": operation,
            "reason": state.get("reason") or "administrative maintenance",
            "started_at": state.get("started_at"),
        },
    )


def queued_runner_pause_active() -> bool:
    return bool(current_maintenance_state()["enabled"])


def require_maintenance_enabled_for_update(action: str) -> None:
    require_maintenance_enabled("update", action)


def require_maintenance_enabled_for_restore(action: str) -> None:
    require_maintenance_enabled("backup", action)


def require_maintenance_enabled(category: str, action: str) -> None:
    state = current_maintenance_state()
    if state["enabled"]:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "message": f"maintenance mode must be enabled before {category} {action}",
            "action": action,
        },
    )


def data_root_path() -> Path:
    return Path(settings.data_root)


def backup_root_path() -> Path:
    return Path(settings.backup_root)


def restore_test_root_path() -> Path:
    return Path(settings.restore_test_root)


def artifact_root_path() -> Path:
    return Path(settings.artifact_root)


async def protected_artifact_urls() -> set[str]:
    protected: set[str] = set()
    for profile in await database.list_voice_profiles(include_deleted=True):
        for sample in profile.get("sample_artifacts") or []:
            if not isinstance(sample, dict) or not isinstance(sample.get("url"), str):
                continue
            with suppress(HTTPException):
                protected.add(validate_voice_profile_artifact_url(sample["url"]))
    return protected


def catalog_snapshot() -> ModelCatalog:
    global model_catalog
    if model_catalog is None:
        model_catalog = load_catalog(Path(settings.model_catalog_dir), resource_policy())
    return model_catalog


async def refresh_catalog_cache() -> ModelCatalog:
    global model_catalog
    records = await database.list_model_records(status="installed")
    alias_policies = await database.list_model_alias_policies()
    model_catalog = load_catalog(
        Path(settings.model_catalog_dir),
        resource_policy(),
        extra_manifests=[row["manifest"] for row in records],
        alias_policies=alias_policies,
    )
    return model_catalog


def env_external_runtime_inputs(settings_obj: Settings) -> dict[str, Any]:
    return {
        "openai_compatible_base_url": settings_obj.openai_compatible_base_url,
        "openai_compatible_api_key": settings_obj.openai_compatible_api_key,
        "openai_compatible_configuration_error": None,
        "generic_http_base_url": settings_obj.generic_http_base_url,
        "generic_http_configuration_error": None,
    }


def runtime_registry_external_inputs(settings_obj: Settings) -> dict[str, Any]:
    inputs = env_external_runtime_inputs(settings_obj)
    configs = runtime_configuration_cache or {}

    openai_config = configs.get("openai-compatible")
    if openai_config is not None:
        if openai_config.get("enabled"):
            inputs["openai_compatible_base_url"] = str(openai_config.get("base_url") or "")
            inputs["openai_compatible_api_key"] = str(openai_config.get("decrypted_api_key") or "")
            inputs["openai_compatible_configuration_error"] = openai_config.get("configuration_error")
        else:
            inputs["openai_compatible_base_url"] = ""
            inputs["openai_compatible_api_key"] = ""
            inputs["openai_compatible_configuration_error"] = None

    generic_config = configs.get("generic-http")
    if generic_config is not None:
        if generic_config.get("enabled"):
            inputs["generic_http_base_url"] = str(generic_config.get("base_url") or "")
            inputs["generic_http_configuration_error"] = generic_config.get("configuration_error")
        else:
            inputs["generic_http_base_url"] = ""
            inputs["generic_http_configuration_error"] = None

    return inputs


async def load_runtime_configuration_cache() -> dict[str, dict[str, Any]]:
    rows = await database.list_runtime_configurations()
    configs: dict[str, dict[str, Any]] = {}
    for row in rows:
        runtime = str(row.get("runtime") or "")
        if runtime not in EXTERNAL_RUNTIME_NAMES:
            continue
        cached = dict(row)
        cached["decrypted_api_key"] = ""
        cached["configuration_error"] = None
        secret_name = cached.get("api_key_secret_name")
        if cached.get("enabled") and isinstance(secret_name, str) and secret_name:
            secret_row = await database.get_encrypted_secret(secret_name)
            if secret_row is None:
                cached["configuration_error"] = "configured API-key secret was not found"
            else:
                try:
                    cached["decrypted_api_key"] = secret_store.decrypt_value(
                        require_master_encryption_key(),
                        secret_name,
                        secret_row.get("secret_envelope") or {},
                    )
                except (HTTPException, secret_store.SecretStoreError) as exc:
                    cached["configuration_error"] = str(getattr(exc, "detail", exc))
        configs[runtime] = cached
    return configs


async def refresh_runtime_configuration_cache() -> dict[str, dict[str, Any]]:
    global runtime_configuration_cache, runtime_registry
    runtime_configuration_cache = await load_runtime_configuration_cache()
    runtime_registry = None
    return runtime_configuration_cache


def runtime_registry_snapshot() -> RuntimeRegistry:
    global runtime_registry
    if runtime_registry is None:
        runtime_registry = build_runtime_registry(
            localai_url=settings.localai_url,
            comfyui_url=settings.comfyui_url,
            voicebox_url=settings.voicebox_url,
            audio_cpu_url=settings.audio_cpu_url,
            allow_external=settings.allow_external_providers,
            **runtime_registry_external_inputs(settings),
        )
    return runtime_registry


def workflow_runtime_names() -> set[str]:
    return set(runtime_registry_snapshot().adapters)


def node_pin_registry_snapshot() -> dict[tuple[str, str], ApprovedNodePin]:
    global approved_node_pins
    if approved_node_pins is None:
        approved_node_pins = load_node_pins(Path(settings.comfyui_node_pin_registry))
    return approved_node_pins


def datetime_to_api_string(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def database_node_pin_to_approved(row: dict[str, Any]) -> ApprovedNodePin:
    record = {
        "id": row["node_id"],
        "commit": row["commit"],
        "repository_url": row["repository_url"],
        "display_name": row.get("display_name"),
        "status": row.get("status") or "approved",
        "approved_by": row.get("approved_by"),
        "approved_at": datetime_to_api_string(row.get("approved_at")),
        "dependency_lock_sha256": row.get("dependency_lock_sha256"),
        "allowed_route_prefixes": row.get("allowed_route_prefixes") or [],
        "notes": row.get("notes"),
    }
    return parse_node_pin(record, f"database node pin {row['node_id']}@{row['commit']}")


def node_pin_database_payload(pin: ApprovedNodePin, *, approved_at: datetime | None = None) -> dict[str, Any]:
    return {
        "node_id": pin.id,
        "commit": pin.commit,
        "repository_url": pin.repository_url,
        "display_name": pin.display_name,
        "status": pin.status,
        "approved_by": pin.approved_by,
        "approved_at": approved_at,
        "dependency_lock_sha256": pin.dependency_lock_sha256,
        "allowed_route_prefixes": list(pin.allowed_route_prefixes),
        "notes": pin.notes,
    }


def validate_node_pin_record(record: dict[str, Any], context: str) -> ApprovedNodePin:
    try:
        return parse_node_pin(record, context)
    except WorkflowError as exc:
        raise HTTPException(status_code=422, detail={"code": "comfyui_node_pin_invalid", "message": str(exc)}) from exc


def public_comfyui_node_pin(pin: ApprovedNodePin, *, source: str, created_at: Any = None, updated_at: Any = None) -> dict[str, Any]:
    return jsonable_encoder(
        {
            **pin.to_dict(),
            "source": source,
            "created_at": created_at,
            "updated_at": updated_at,
        }
    )


def node_pin_create_record(payload: ComfyUiNodePinCreate, auth: AuthContext, approved_at: datetime) -> ApprovedNodePin:
    record = payload.model_dump()
    record["approved_by"] = auth.subject_id
    record["approved_at"] = approved_at.isoformat()
    return validate_node_pin_record(record, "comfyui_node_pin")


def node_pin_base_record(pin: ApprovedNodePin) -> dict[str, Any]:
    return {
        "id": pin.id,
        "commit": pin.commit,
        "repository_url": pin.repository_url,
        "display_name": pin.display_name,
        "status": pin.status,
        "approved_by": pin.approved_by,
        "approved_at": pin.approved_at,
        "dependency_lock_sha256": pin.dependency_lock_sha256,
        "allowed_route_prefixes": list(pin.allowed_route_prefixes),
        "notes": pin.notes,
    }


async def node_pin_update_record(node_id: str, commit: str, payload: ComfyUiNodePinUpdate, auth: AuthContext, approved_at: datetime) -> ApprovedNodePin:
    existing_row = await database.get_comfyui_node_pin(node_id, commit)
    if existing_row is not None:
        base = node_pin_base_record(database_node_pin_to_approved(existing_row))
    else:
        seed_pin = load_node_pins(Path(settings.comfyui_node_pin_registry)).get((node_id, commit))
        if seed_pin is None:
            raise HTTPException(status_code=404, detail="ComfyUI node pin not found")
        base = node_pin_base_record(seed_pin)
    updates = payload.model_dump(exclude_unset=True)
    base.update(updates)
    base["approved_by"] = auth.subject_id
    base["approved_at"] = approved_at.isoformat()
    return validate_node_pin_record(base, f"comfyui_node_pin {node_id}@{commit}")


async def refresh_node_pin_registry() -> dict[tuple[str, str], ApprovedNodePin]:
    global approved_node_pins
    registry = dict(load_node_pins(Path(settings.comfyui_node_pin_registry)))
    for row in await database.list_comfyui_node_pins():
        pin = database_node_pin_to_approved(row)
        registry[(pin.id, pin.commit)] = pin
    approved_node_pins = registry
    return registry


async def merged_node_pin_registry_for_api() -> tuple[dict[tuple[str, str], ApprovedNodePin], dict[tuple[str, str], str], dict[tuple[str, str], dict[str, Any]]]:
    seed_pins = load_node_pins(Path(settings.comfyui_node_pin_registry))
    registry = dict(seed_pins)
    sources = {key: "seed" for key in seed_pins}
    timestamps: dict[tuple[str, str], dict[str, Any]] = {}
    for row in await database.list_comfyui_node_pins():
        pin = database_node_pin_to_approved(row)
        key = (pin.id, pin.commit)
        registry[key] = pin
        sources[key] = "database"
        timestamps[key] = {"created_at": row.get("created_at"), "updated_at": row.get("updated_at")}
    globals()["approved_node_pins"] = registry
    return registry, sources, timestamps


def approved_node_pin(node_id: str, commit: str | None) -> ApprovedNodePin | None:
    if commit is None:
        return None
    return node_pin_registry_snapshot().get((node_id, commit))


def workflow_record_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        workflow = parse_workflow(payload)
        return workflow_record(workflow, catalog_snapshot().model_or_alias_record, workflow_runtime_names(), approved_node_pin)
    except WorkflowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def db_workflow_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "version": record["version"],
        "display_name": record["display_name"],
        "backend_policy": record["backend_policy"],
        "resource_class": record["resource_class"],
        "status": record["status"],
        "visibility_roles": record.get("visibility_roles", ["admin"]),
        "manifest": {key: value for key, value in record.items() if key not in {"status", "dependency_status", "execution_summary", "publishable"}},
        "dependency_status": record["dependency_status"],
    }


def workflow_test_media_job_payload(workflow: dict[str, Any], payload: WorkflowTestRequest) -> dict[str, Any]:
    return {
        "modality": workflow.get("modality"),
        "operation": workflow.get("operation"),
        "model": workflow.get("model_alias"),
        "runtime_policy": payload.runtime_policy or workflow.get("runtime_policy", "any"),
        "priority": payload.priority,
        "input": {
            "workflow_id": workflow.get("id"),
            "workflow_version": workflow.get("version"),
            "parameters": payload.parameters,
        },
    }


def workflow_test_result(workflow: dict[str, Any], payload: WorkflowTestRequest) -> dict[str, Any]:
    request_payload = workflow_test_media_job_payload(workflow, payload)
    try:
        validate_workflow_job_request(workflow, request_payload, max_staged_media_bytes=settings.upload_max_bytes)
    except WorkflowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    dependency_ready = bool((workflow.get("dependency_status") or {}).get("ready"))
    return {
        "status": "ready" if dependency_ready else "needs_dependencies",
        "can_submit": dependency_ready,
        "workflow": workflow,
        "request": {
            "modality": request_payload["modality"],
            "operation": request_payload["operation"],
            "model": request_payload["model"],
            "runtime_policy": request_payload["runtime_policy"],
            "priority": request_payload["priority"],
            "input": {
                "workflow_id": request_payload["input"]["workflow_id"],
                "workflow_version": request_payload["input"]["workflow_version"],
                "parameter_names": sorted(payload.parameters),
                "parameter_count": len(payload.parameters),
            },
        },
        "dependency_status": workflow.get("dependency_status") or {},
        "execution_summary": workflow.get("execution_summary") or workflow_execution_summary(workflow, workflow.get("dependency_status") or {}),
    }


def public_workflow(row: dict[str, Any]) -> dict[str, Any]:
    manifest = dict(row["manifest"])
    dependency_status = row["dependency_status"]
    return jsonable_encoder(
        {
            **manifest,
            "status": row["status"],
            "dependency_status": dependency_status,
            "execution_summary": workflow_execution_summary(manifest, dependency_status),
            "publishable": bool(dependency_status.get("ready")),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "unpublished_at": row.get("unpublished_at"),
        }
    )


async def enforce_workflow_backed_media_job(auth: AuthContext, payload: MediaJobCreate) -> dict[str, Any] | None:
    workflow_id = payload.input.get("workflow_id")
    workflow_version = payload.input.get("workflow_version")
    if workflow_id is None and workflow_version is None:
        return None
    if not isinstance(workflow_id, str) or not workflow_id or not isinstance(workflow_version, str) or not workflow_version:
        raise HTTPException(status_code=422, detail="workflow-backed media jobs require string workflow_id and workflow_version")
    row = await database.get_workflow(workflow_id, workflow_version)
    if row is None:
        raise HTTPException(status_code=404, detail="published workflow not found")
    workflow = public_workflow(row)
    if not visible_to_role(workflow, auth.role.value, auth.scopes):
        raise HTTPException(status_code=403, detail="workflow is not visible to this role")
    if not workflow.get("publishable"):
        raise HTTPException(
            status_code=424,
            detail={
                "message": "workflow dependencies are not ready",
                "workflow_id": workflow_id,
                "workflow_version": workflow_version,
                "dependency_status": workflow.get("dependency_status"),
            },
        )
    try:
        validate_workflow_job_request(workflow, payload.model_dump(), max_staged_media_bytes=settings.upload_max_bytes)
    except WorkflowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return workflow


def enforce_workflow_backend_policy(workflow: dict[str, Any] | None, resolution: RuntimeResolution) -> None:
    if workflow is None:
        return
    backend_policy = workflow.get("backend_policy")
    if backend_policy == "comfyui-only" and resolution.runtime != "comfyui":
        raise HTTPException(
            status_code=422,
            detail={
                "message": "workflow backend_policy requires ComfyUI runtime",
                "workflow_id": workflow.get("id"),
                "workflow_version": workflow.get("version"),
                "resolved_runtime": resolution.runtime,
            },
        )
    if backend_policy == "non-comfy-only" and resolution.runtime == "comfyui":
        raise HTTPException(
            status_code=422,
            detail={
                "message": "workflow backend_policy forbids ComfyUI runtime",
                "workflow_id": workflow.get("id"),
                "workflow_version": workflow.get("version"),
                "resolved_runtime": resolution.runtime,
            },
        )


async def seed_workflows_from_directory(seed_dir: Path) -> dict[str, Any]:
    seeded: list[str] = []
    for workflow in load_workflows(seed_dir):
        record = workflow_record(workflow, catalog_snapshot().model_or_alias_record, workflow_runtime_names(), approved_node_pin)
        row = await database.upsert_workflow(db_workflow_payload(record))
        seeded.append(f"{row['id']}@{row['version']}")
    return {"seeded": seeded, "count": len(seeded)}


async def refresh_workflow_dependency_statuses() -> dict[str, Any]:
    await refresh_node_pin_registry()
    refreshed: list[str] = []
    for row in await database.list_workflows():
        try:
            record = workflow_record_from_payload(row["manifest"])
        except HTTPException:
            continue
        updated = await database.upsert_workflow(db_workflow_payload(record))
        refreshed.append(f"{updated['id']}@{updated['version']}")
    return {"refreshed": refreshed, "count": len(refreshed)}


def alias_visible_to_auth(alias: CatalogAlias, auth: AuthContext | None) -> bool:
    if auth is None or auth.has_scope("*"):
        return True
    roles = set(alias.alias.visibility_roles)
    return not roles or auth.role.value in roles


def alias_manifest_allows_inference(alias: CatalogAlias, auth: AuthContext | None) -> bool:
    if auth is None or auth.has_scope("*") or alias.manifest is None:
        return True
    return modelhub_policy.role_allowed_by_manifest_permissions(alias.manifest.to_dict(), auth.role.value, "inference")


def manifest_allows_role_action(manifest: Any, auth: AuthContext | None, action: str) -> bool:
    if auth is None or auth.has_scope("*"):
        return True
    manifest_dict = manifest.to_dict() if hasattr(manifest, "to_dict") else manifest
    if not isinstance(manifest_dict, dict):
        return True
    return modelhub_policy.role_allowed_by_manifest_permissions(manifest_dict, auth.role.value, action)


def require_manifest_role_action(manifest: Any, auth: AuthContext | None, action: str) -> None:
    if manifest_allows_role_action(manifest, auth, action):
        return
    model_id = getattr(manifest, "id", None)
    version = getattr(manifest, "version", None)
    model_ref = f"{model_id}@{version}" if model_id and version else "model"
    raise HTTPException(status_code=403, detail=f"{model_ref} cannot be used for {action} by this role")


def require_catalog_alias(
    model_id: str,
    required_modality: str | None = None,
    runtime_policy: str = "any",
    auth: AuthContext | None = None,
) -> CatalogAlias:
    try:
        alias = catalog_snapshot().require_alias(model_id)
    except CatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not alias.alias.enabled:
        raise HTTPException(status_code=403, detail=f"alias {model_id} is disabled by administrator policy")
    if not alias_visible_to_auth(alias, auth):
        raise HTTPException(status_code=403, detail=f"alias {model_id} is not visible to this role")
    if required_modality and alias.alias.modality != required_modality:
        raise HTTPException(
            status_code=422,
            detail=f"alias {model_id} is {alias.alias.modality}, not {required_modality}",
        )
    if runtime_policy == "non_comfy_only" and not any(runtime != "comfyui" for runtime in alias.runtimes):
        raise HTTPException(status_code=422, detail="requested alias has no compatible non-Comfy runtime")
    if alias.manifest is None:
        raise HTTPException(status_code=424, detail=f"alias {model_id} is not backed by an installed model manifest")
    if not alias_manifest_allows_inference(alias, auth):
        raise HTTPException(status_code=403, detail=f"alias {model_id} cannot be used for inference by this role")
    return alias


def resolve_catalog_alias(
    model_id: str,
    required_modality: str | None = None,
    runtime_policy: str = "any",
    operation: str | None = None,
    auth: AuthContext | None = None,
) -> RuntimeResolution:
    alias = require_catalog_alias(model_id, required_modality, runtime_policy, auth=auth)
    try:
        return runtime_registry_snapshot().resolve(alias, operation=operation, runtime_policy=runtime_policy)
    except RuntimeResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def resolve_catalog_alias_for_auth(
    model_id: str,
    required_modality: str | None,
    auth: AuthContext,
    runtime_policy: str = "any",
    operation: str | None = None,
) -> RuntimeResolution:
    try:
        return resolve_catalog_alias(model_id, required_modality, runtime_policy, operation=operation, auth=auth)
    except TypeError as exc:
        if "unexpected keyword argument 'auth'" not in str(exc):
            raise
        return resolve_catalog_alias(model_id, required_modality, runtime_policy, operation=operation)


def resolve_catalog_alias_for_modalities(
    model_id: str,
    required_modalities: set[str],
    runtime_policy: str = "any",
    operation: str | None = None,
    auth: AuthContext | None = None,
) -> RuntimeResolution:
    alias = require_catalog_alias(model_id, None, runtime_policy, auth=auth)
    if alias.alias.modality not in required_modalities:
        expected = " or ".join(sorted(required_modalities))
        raise HTTPException(status_code=422, detail=f"alias {model_id} is {alias.alias.modality}, not {expected}")
    try:
        return runtime_registry_snapshot().resolve(alias, operation=operation, runtime_policy=runtime_policy)
    except RuntimeResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def resolve_catalog_alias_for_modalities_auth(
    model_id: str,
    required_modalities: set[str],
    auth: AuthContext,
    runtime_policy: str = "any",
    operation: str | None = None,
) -> RuntimeResolution:
    try:
        return resolve_catalog_alias_for_modalities(model_id, required_modalities, runtime_policy, operation=operation, auth=auth)
    except TypeError as exc:
        if "unexpected keyword argument 'auth'" not in str(exc):
            raise
        return resolve_catalog_alias_for_modalities(model_id, required_modalities, runtime_policy, operation=operation)


def openai_runtime_adapter(resolution: RuntimeResolution) -> RuntimeAdapter | None:
    adapter = runtime_registry_snapshot().adapter(resolution.runtime)
    if adapter is None or not adapter.openai_compatible:
        return None
    return adapter


def require_openai_forwarding(resolution: RuntimeResolution, operation: str) -> None:
    if openai_runtime_adapter(resolution) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"runtime {resolution.runtime} does not support OpenAI-compatible {operation} forwarding "
                f"for alias {resolution.public_alias}"
            ),
        )


async def acquire_inference_lease(resolution: RuntimeResolution, operation: str, owner_id: str | None = None) -> str | None:
    if not resolution.requires_gpu:
        return None
    await enforce_gpu_hardware_admission(resolution)
    gate = await database.runtime_reservation_gate(owner_id or "", resolution.runtime, resolution.resolved_model_version, GPU_RUNTIMES)
    if not gate.get("allowed"):
        active = gate.get("active_reservation") or {}
        raise HTTPException(
            status_code=409,
            detail={
                "message": "GPU runtime is reserved for another owner/model",
                "reservation": {
                    "id": active.get("id"),
                    "owner_id": active.get("owner_id"),
                    "runtime": active.get("runtime"),
                    "model_alias": active.get("model_alias"),
                    "resolved_model_version": active.get("resolved_model_version"),
                    "expires_at": jsonable_encoder(active.get("expires_at")),
                },
            },
        )
    owner = f"sync-{operation}-{uuid.uuid4().hex}"
    lease = await database.acquire_scheduler_owner(owner, max(30, settings.sync_inference_lease_ttl_seconds))
    if not lease.get("acquired"):
        raise HTTPException(status_code=409, detail={"message": "GPU scheduler lease is held by another owner", "lease": jsonable_encoder(lease)})
    return owner


async def renew_inference_lease(owner: str | None) -> bool:
    if owner is None:
        return True
    lease = await database.acquire_scheduler_owner(owner, max(30, settings.sync_inference_lease_ttl_seconds))
    return bool(lease.get("acquired"))


async def await_with_inference_lease_renewal(owner: str | None, operation: str, awaitable: Any) -> Any:
    if owner is None:
        return await awaitable

    task = asyncio.create_task(awaitable)
    renew_interval = max(15, min(60, settings.sync_inference_lease_ttl_seconds // 3))
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=renew_interval)
            if task in done:
                return await task
            if not await renew_inference_lease(owner):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "GPU scheduler lease was lost during synchronous inference",
                        "type": "scheduler_lease_lost",
                        "operation": operation,
                    },
                )
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def release_inference_lease(owner: str | None) -> None:
    if owner is not None:
        await database.release_scheduler_owner(owner)


def scheduler_runtime_urls() -> dict[str, str]:
    return {
        "localai": settings.localai_url,
        "comfyui": settings.comfyui_url,
        "voicebox": settings.voicebox_url,
    }


def runtime_control_runner(lease_ttl_seconds: int | None = None) -> GpuJobRunner:
    return GpuJobRunner(
        Path(settings.artifact_root),
        interval_seconds=settings.gpu_job_runner_interval_seconds,
        lease_ttl_seconds=lease_ttl_seconds or settings.sync_inference_lease_ttl_seconds,
        runtime_agent_url=settings.runtime_agent_url,
        runtime_agent_token=settings.runtime_agent_token,
        runtime_agent_tls_ca_file=settings.runtime_agent_tls_ca_file,
        runtime_agent_tls_client_cert_file=settings.runtime_agent_tls_client_cert_file,
        runtime_agent_tls_client_key_file=settings.runtime_agent_tls_client_key_file,
        runtime_agent_tls_verify=settings.runtime_agent_tls_verify,
        runtime_control_token=settings.runtime_control_token,
        reserve_vram_gib=settings.gpu_reserve_vram_gib,
        default_idle_timeout_seconds=settings.gpu_default_idle_timeout_seconds,
        runtime_urls=scheduler_runtime_urls(),
    )


def sync_runtime_modality(operation: str) -> str:
    return {
        "chat": "llm",
        "responses": "llm",
        "embeddings": "embedding",
        "audio-speech": "tts",
        "voicebox-native-speech": "tts",
    }.get(operation, "inference")


def sync_runtime_job(resolution: RuntimeResolution, operation: str) -> dict[str, Any]:
    return {
        "id": f"sync_{operation}_{uuid.uuid4().hex}",
        "runtime": resolution.runtime,
        "model_alias": resolution.public_alias,
        "resolved_model_version": resolution.resolved_model_version,
        "modality": sync_runtime_modality(operation),
        "operation": operation,
        "request_params": {},
        "artifacts": [],
        "durable_job": False,
    }


async def prepare_sync_gpu_runtime(resolution: RuntimeResolution, operation: str) -> bool:
    if not resolution.requires_gpu:
        return False
    runner = runtime_control_runner(settings.sync_inference_lease_ttl_seconds)
    job = sync_runtime_job(resolution, operation)
    await runner.unload_other_gpu_runtimes(job)
    await runner.verify_vram_or_recover(job)
    await runner.load_runtime_model(job)
    await runner.warm_runtime_model(job)
    return True


async def mark_sync_gpu_runtime_idle(resolution: RuntimeResolution, operation: str) -> None:
    if not resolution.requires_gpu:
        return
    runner = runtime_control_runner(settings.sync_inference_lease_ttl_seconds)
    await runner.record_runtime_idle_for_job(sync_runtime_job(resolution, operation), {"source": "sync_inference"})


def native_comfyui_resolution() -> RuntimeResolution:
    return RuntimeResolution(
        public_alias="comfyui-native",
        model_id="comfyui-native-workflow",
        model_version="native",
        resolved_model_version="comfyui-native-workflow@native",
        runtime="comfyui",
        preferred_runtime="comfyui",
        requires_gpu=True,
        resource_label="expected",
        runtime_policy="comfyui_native",
    )


COMFYUI_MODEL_FREE_SMOKE_CLASS_TYPES = frozenset({"B1RuntimeSmoke", "B1RuntimeTinyImage", "SaveImage", "PreviewImage"})
COMFYUI_MODEL_FREE_STOCK_IO_CLASS_TYPES = frozenset({"LoadImage", "SaveImage", "PreviewImage"})
COMFYUI_MODEL_FREE_OUTPUT_CLASS_TYPES = frozenset({"SaveImage", "PreviewImage"})


def comfyui_native_prompt_class_types(payload: Any) -> set[str] | None:
    if not isinstance(payload, dict):
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, dict) or not prompt:
        return None
    class_types: set[str] = set()
    for node in prompt.values():
        if not isinstance(node, dict):
            return None
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type.strip():
            return None
        class_types.add(class_type.strip())
    return class_types


def comfyui_native_prompt_model_free_kind(payload: Any) -> str | None:
    class_types = comfyui_native_prompt_class_types(payload)
    if not class_types:
        return None
    if "B1RuntimeTinyImage" in class_types and class_types.issubset(COMFYUI_MODEL_FREE_SMOKE_CLASS_TYPES):
        return "model-free-smoke"
    if (
        "LoadImage" in class_types
        and class_types.intersection(COMFYUI_MODEL_FREE_OUTPUT_CLASS_TYPES)
        and class_types.issubset(COMFYUI_MODEL_FREE_STOCK_IO_CLASS_TYPES)
    ):
        return "model-free-stock-io"
    return None


def comfyui_native_prompt_is_model_free_smoke(payload: Any) -> bool:
    return comfyui_native_prompt_model_free_kind(payload) == "model-free-smoke"


def native_comfyui_prompt_resolution(*, model_free_smoke: bool = False, model_free_kind: str | None = None) -> RuntimeResolution:
    kind = "model-free-smoke" if model_free_smoke else model_free_kind
    if kind not in {"model-free-smoke", "model-free-stock-io"}:
        return native_comfyui_resolution()
    resolution = native_comfyui_resolution()
    return RuntimeResolution(
        **{
            **asdict(resolution),
            "model_version": kind,
            "resolved_model_version": f"comfyui-native-workflow@{kind}",
            "requires_gpu": False,
            "resource_label": "recommended",
        }
    )


def native_voicebox_resolution() -> RuntimeResolution:
    return RuntimeResolution(
        public_alias="voicebox-native",
        model_id="voicebox-native-speech",
        model_version="native",
        resolved_model_version="voicebox-native-speech@native",
        runtime="voicebox",
        preferred_runtime="voicebox",
        requires_gpu=True,
        resource_label="expected",
        runtime_policy="voicebox_native",
    )


def is_native_voicebox_speech_request(path: str, method: str) -> bool:
    return method.upper() == "POST" and path.strip("/") == "v1/audio/speech"


async def proxy_voicebox_compatibility(path: str, request: Request, auth: AuthContext) -> Response:
    if not is_native_voicebox_speech_request(path, request.method):
        return await proxy_http(settings.voicebox_url, path, request)

    require_not_in_maintenance("voicebox/native-audio-speech")
    body = await request.body()
    resolution = native_voicebox_resolution()
    lease_owner: str | None = None
    runtime_prepared = False
    try:
        lease_owner = await acquire_inference_lease(resolution, "voicebox-native-speech", owner_id=auth.subject_id)
        runtime_prepared = await prepare_sync_gpu_runtime(resolution, "voicebox-native-speech")
        return await await_with_inference_lease_renewal(
            lease_owner,
            "voicebox-native-speech",
            proxy_http_bytes(
                settings.voicebox_url,
                path,
                request,
                body=body,
                timeout_seconds=float(settings.sync_inference_lease_ttl_seconds),
            ),
        )
    except RuntimePreparationError as exc:
        raise HTTPException(
            status_code=503,
            detail=runtime_prepare_error_detail(exc, resolution, "voicebox-native-speech"),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        if runtime_prepared:
            with suppress(Exception):
                await mark_sync_gpu_runtime_idle(resolution, "voicebox-native-speech")
        await release_inference_lease(lease_owner)


def comfyui_native_runtime_job(job: dict[str, Any]) -> dict[str, Any]:
    resolution = native_comfyui_resolution()
    runtime_job = dict(job)
    runtime_job["runtime"] = resolution.runtime
    if not runtime_job.get("model_alias"):
        runtime_job["model_alias"] = resolution.public_alias
    if not runtime_job.get("resolved_model_version"):
        runtime_job["resolved_model_version"] = resolution.resolved_model_version
    if not runtime_job.get("modality"):
        runtime_job["modality"] = "workflow"
    if not runtime_job.get("operation"):
        runtime_job["operation"] = "comfyui-prompt"
    runtime_job.setdefault("request_params", {})
    runtime_job.setdefault("artifacts", [])
    runtime_job["durable_job"] = True
    return runtime_job


async def prepare_comfyui_native_runtime(job: dict[str, Any]) -> None:
    runner = runtime_control_runner(settings.comfyui_prompt_lease_ttl_seconds)
    runtime_job = comfyui_native_runtime_job(job)
    for state, stage, progress in GPU_STATE_STEPS:
        if state == JobState.RUNNING:
            break
        await database.update_job(str(runtime_job["id"]), state=state.value, stage=stage, progress=progress)
        if state == JobState.UNLOADING:
            await runner.unload_other_gpu_runtimes(runtime_job)
        if state == JobState.VERIFYING_VRAM:
            await runner.verify_vram_or_recover(runtime_job)
        if state == JobState.LOADING:
            await runner.load_runtime_model(runtime_job)
        if state == JobState.WARMING:
            await runner.warm_runtime_model(runtime_job)


async def mark_comfyui_native_runtime_idle(job: dict[str, Any], prompt_id: str, last_state: str) -> None:
    runner = runtime_control_runner(settings.comfyui_prompt_lease_ttl_seconds)
    await runner.record_runtime_idle_for_job(
        comfyui_native_runtime_job(job),
        {"source": "comfyui_native_compatibility", "prompt_id": prompt_id, "last_state": last_state},
    )


async def acquire_comfyui_prompt_lease(job_id: str) -> tuple[str, dict[str, Any]]:
    owner = f"comfyui-prompt-{job_id}"
    deadline = monotonic() + max(0, settings.comfyui_prompt_wait_timeout_seconds)
    ttl_seconds = max(30, settings.comfyui_prompt_lease_ttl_seconds)
    last_lease: dict[str, Any] | None = None
    while True:
        lease = await database.acquire_scheduler_owner(owner, ttl_seconds)
        last_lease = lease
        if lease.get("acquired"):
            return owner, lease
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "GPU scheduler lease is held by another owner",
                    "retryable": True,
                    "lease": jsonable_encoder(last_lease),
                },
            )
        await asyncio.sleep(min(1.0, max(0.1, remaining)))


def comfyui_prompt_id_from_response(response: Response) -> str | None:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("prompt_id"), str):
        return payload["prompt_id"]
    return None


def comfyui_native_prompt_audit_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__, "has_prompt": False, "node_count": 0}
    prompt = payload.get("prompt")
    top_level_keys = {str(key) for key in payload}
    summary: dict[str, Any] = {
        "known_top_level_keys": sorted(top_level_keys & COMFYUI_PROMPT_KNOWN_TOP_LEVEL_KEYS),
        "unknown_top_level_key_count": len(top_level_keys - COMFYUI_PROMPT_KNOWN_TOP_LEVEL_KEYS),
        "has_client_id": isinstance(payload.get("client_id"), str),
        "has_prompt": isinstance(prompt, dict),
    }
    if not isinstance(prompt, dict):
        summary.update({"prompt_shape": "missing" if "prompt" not in payload else type(prompt).__name__, "node_count": 0})
        return summary

    node_ids = sorted(str(key) for key in prompt)
    class_types: set[str] = set()
    malformed_nodes = 0
    for node in prompt.values():
        if not isinstance(node, dict):
            malformed_nodes += 1
            continue
        class_type = node.get("class_type")
        if isinstance(class_type, str) and class_type.strip():
            class_types.add(class_type.strip())
        else:
            malformed_nodes += 1
    class_type_material = "\n".join(sorted(class_types)).encode("utf-8")
    node_id_material = "\n".join(node_ids).encode("utf-8")
    summary.update(
        {
            "node_count": len(prompt),
            "class_type_count": len(class_types),
            "class_type_digest": hashlib.sha256(class_type_material).hexdigest() if class_types else None,
            "node_id_digest": hashlib.sha256(node_id_material).hexdigest() if node_ids else None,
            "malformed_node_count": malformed_nodes,
        }
    )
    return summary


async def fetch_comfyui_history(prompt_id: str) -> dict[str, Any] | None:
    url = f"{settings.comfyui_url.rstrip('/')}/history/{prompt_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) and comfyui_history_record(prompt_id, payload) is not None else None


async def comfyui_history_contains_prompt(prompt_id: str) -> bool:
    return await fetch_comfyui_history(prompt_id) is not None


async def interrupt_comfyui_prompt() -> None:
    url = f"{settings.comfyui_url.rstrip('/')}/interrupt"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url)
    except httpx.HTTPError:
        return


def add_comfyui_prompt_ids(value: Any, prompt_ids: set[str]) -> None:
    if isinstance(value, str) and value.strip():
        prompt_ids.add(value.strip())
        return
    if isinstance(value, list):
        for item in value:
            add_comfyui_prompt_ids(item, prompt_ids)
        return
    if isinstance(value, dict):
        for key in ("prompt_id", "id"):
            add_comfyui_prompt_ids(value.get(key), prompt_ids)


def comfyui_queue_cancel_prompt_ids(body: bytes) -> set[str]:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    prompt_ids: set[str] = set()
    for key in COMFYUI_QUEUE_CANCEL_KEYS:
        add_comfyui_prompt_ids(payload.get(key), prompt_ids)
    return prompt_ids


def comfyui_native_cancel_target(path: str, method: str, body: bytes) -> tuple[bool, set[str] | None]:
    normalized_path = path.strip("/")
    normalized_method = method.upper()
    if normalized_path == "interrupt" and normalized_method in {"GET", "POST"}:
        return True, None
    if normalized_path != "queue" or normalized_method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False, set()
    prompt_ids = comfyui_queue_cancel_prompt_ids(body)
    if prompt_ids:
        return True, prompt_ids
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, set()
    if isinstance(payload, dict) and any(payload.get(key) is True for key in ("clear", "clear_queue", "interrupt", "cancel_all")):
        return True, None
    return False, set()


def normalize_comfyui_passthrough_path(path: str) -> str:
    if any(character in path for character in ("\\", "\x00", "?", "#")):
        raise HTTPException(status_code=403, detail={"code": "comfyui_route_denied", "message": "ComfyUI compatibility path is not allowed"})
    decoded_parts: list[str] = []
    for part in path.strip("/").split("/"):
        if not part:
            continue
        if COMFYUI_BAD_PERCENT_ESCAPE.search(part):
            raise HTTPException(status_code=403, detail={"code": "comfyui_route_denied", "message": "ComfyUI compatibility path is not allowed"})
        try:
            decoded = unquote(part, errors="strict")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=403, detail={"code": "comfyui_route_denied", "message": "ComfyUI compatibility path is not allowed"}) from exc
        if decoded in {".", ".."}:
            raise HTTPException(status_code=403, detail={"code": "comfyui_route_denied", "message": "ComfyUI compatibility path is not allowed"})
        if any(character in decoded for character in ("/", "\\", "\x00", "?", "#")):
            raise HTTPException(status_code=403, detail={"code": "comfyui_route_denied", "message": "ComfyUI compatibility path is not allowed"})
        if any(ord(character) < 32 for character in decoded):
            raise HTTPException(status_code=403, detail={"code": "comfyui_route_denied", "message": "ComfyUI compatibility path is not allowed"})
        decoded_parts.append(decoded)
    return "/".join(decoded_parts)


def path_matches_prefix(normalized_path: str, prefix: str) -> bool:
    normalized_prefix = normalize_comfyui_passthrough_path(prefix).lower()
    path_lower = normalized_path.lower()
    if not normalized_prefix:
        return False
    return path_lower == normalized_prefix or path_lower.startswith(f"{normalized_prefix}/")


def path_is_or_under_prefix(normalized_path: str, prefix: str) -> bool:
    normalized_prefix = prefix.strip("/").lower()
    path_lower = normalized_path.lower()
    if not normalized_prefix:
        return False
    return path_lower == normalized_prefix or path_lower.startswith(f"{normalized_prefix}/")


def approved_comfyui_route_prefixes() -> set[str]:
    try:
        pins = node_pin_registry_snapshot()
    except WorkflowError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "comfyui_node_pin_registry_invalid", "message": str(exc)},
        ) from exc
    prefixes: set[str] = set()
    for pin in pins.values():
        if pin.status != "approved":
            continue
        if pin.allowed_route_prefixes and not (
            isinstance(pin.dependency_lock_sha256, str) and COMFYUI_NODE_PIN_LOCK_DIGEST.match(pin.dependency_lock_sha256)
        ):
            log_event("comfyui_route_pin_missing_dependency_lock", node_id=pin.id, commit=pin.commit)
            continue
        for prefix in pin.allowed_route_prefixes:
            prefixes.add(normalize_comfyui_passthrough_path(prefix).lower())
    return prefixes


def require_comfyui_passthrough_allowed(path: str, method: str) -> str:
    normalized_path = normalize_comfyui_passthrough_path(path)
    path_lower = normalized_path.lower()
    method_upper = method.upper()
    if any(path_is_or_under_prefix(path_lower, prefix) for prefix in COMFYUI_DENIED_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail={"code": "comfyui_route_denied", "message": "ComfyUI compatibility route is blocked by policy", "path": normalized_path},
    )
    if method_upper in COMFYUI_READ_METHODS:
        return normalized_path
    if method_upper in COMFYUI_MUTATING_CORE_ROUTES.get(path_lower, set()):
        return normalized_path
    if any(method_upper in methods and path_lower.startswith(prefix) for prefix, methods in COMFYUI_MUTATING_CORE_PREFIXES.items()):
        return normalized_path
    tokens = {part for part in path_lower.replace("-", "/").replace("_", "/").split("/") if part}
    if tokens & COMFYUI_DENIED_MUTATION_TOKENS:
        raise HTTPException(
            status_code=403,
            detail={"code": "comfyui_route_denied", "message": "ComfyUI custom-node management route is blocked by policy", "path": normalized_path},
        )
    approved_prefixes = approved_comfyui_route_prefixes()
    for prefix in settings.comfyui_trusted_route_prefixes:
        if not path_matches_prefix(normalized_path, prefix):
            continue
        if any(path_matches_prefix(normalized_path, approved_prefix) for approved_prefix in approved_prefixes):
            return normalized_path
    raise HTTPException(
        status_code=403,
        detail={"code": "comfyui_route_denied", "message": "ComfyUI mutating compatibility route is not approved", "path": normalized_path},
    )


def require_comfyui_websocket_allowed(path: str) -> str:
    normalized_path = normalize_comfyui_passthrough_path(path)
    path_lower = normalized_path.lower()
    if path_lower == "ws":
        return normalized_path
    if any(path_is_or_under_prefix(path_lower, prefix) for prefix in COMFYUI_DENIED_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail={"code": "comfyui_route_denied", "message": "ComfyUI WebSocket route is blocked by policy", "path": normalized_path},
        )
    tokens = {part for part in path_lower.replace("-", "/").replace("_", "/").split("/") if part}
    if tokens & COMFYUI_DENIED_MUTATION_TOKENS:
        raise HTTPException(
            status_code=403,
            detail={"code": "comfyui_route_denied", "message": "ComfyUI custom-node management WebSocket route is blocked by policy", "path": normalized_path},
        )
    approved_prefixes = approved_comfyui_route_prefixes()
    for prefix in settings.comfyui_trusted_route_prefixes:
        if not path_matches_prefix(normalized_path, prefix):
            continue
        if any(path_matches_prefix(normalized_path, approved_prefix) for approved_prefix in approved_prefixes):
            return normalized_path
    raise HTTPException(
        status_code=403,
        detail={"code": "comfyui_route_denied", "message": "ComfyUI WebSocket compatibility route is not approved", "path": normalized_path},
    )


def comfyui_websocket_scope_for_path(path: str) -> str:
    return "jobs:read" if normalize_comfyui_passthrough_path(path).lower() == "ws" else "jobs:write"


async def record_comfyui_native_cancel_request(prompt_ids: set[str] | None, reason: str) -> dict[str, Any]:
    rows = await database.list_jobs(limit=500, runtime="comfyui")
    matched: list[dict[str, Any]] = []
    for row in rows:
        if row.get("state") in TERMINAL_JOB_STATES:
            continue
        native_prompt_id = row.get("native_prompt_id")
        if not isinstance(native_prompt_id, str) or not native_prompt_id:
            continue
        if prompt_ids is not None and native_prompt_id not in prompt_ids:
            continue
        updated = await database.request_job_cancel(str(row["id"]))
        if updated is not None:
            matched.append(updated)
    log_event(
        "comfyui_native_cancel_tracked",
        reason=reason,
        matched_jobs=len(matched),
        prompt_ids=sorted(prompt_ids) if prompt_ids is not None else "all-active",
    )
    return {
        "matched_jobs": len(matched),
        "job_ids": [str(row["id"]) for row in matched],
        "prompt_ids": sorted(prompt_ids) if prompt_ids is not None else None,
    }


async def proxy_comfyui_compatibility(path: str, request: Request) -> Response:
    normalized_path = require_comfyui_passthrough_allowed(path, request.method)
    body = await request.body()
    should_track_cancel, prompt_ids = comfyui_native_cancel_target(normalized_path, request.method, body)
    response = await proxy_http_bytes(settings.comfyui_url, normalized_path, request, body=body)
    if should_track_cancel and response.status_code < 400:
        try:
            await record_comfyui_native_cancel_request(prompt_ids, f"{request.method.upper()} /{normalized_path}")
        except Exception as exc:
            log_event("comfyui_native_cancel_tracking_failed", path=path, error=exc.__class__.__name__)
    return response


async def track_comfyui_prompt_completion(job_id: str, prompt_id: str, lease_owner: str) -> None:
    run_started = monotonic()
    deadline = monotonic() + max(30, settings.comfyui_prompt_completion_timeout_seconds)
    poll_seconds = max(1, settings.comfyui_prompt_poll_seconds)
    ttl_seconds = max(30, settings.comfyui_prompt_lease_ttl_seconds)
    try:
        while monotonic() < deadline:
            current = await database.get_job(job_id)
            if current is None:
                return
            if current["state"] in TERMINAL_JOB_STATES:
                with suppress(Exception):
                    await mark_comfyui_native_runtime_idle(current, prompt_id, str(current["state"]))
                return
            if current["state"] in {JobState.CANCELLING.value, JobState.CANCELLED.value}:
                await interrupt_comfyui_prompt()
                cancelled = await database.update_job(
                    job_id,
                    state=JobState.CANCELLED.value,
                    stage="cancelled",
                    progress=100,
                    run_time_ms=elapsed_milliseconds(run_started),
                )
                with suppress(Exception):
                    await mark_comfyui_native_runtime_idle(cancelled, prompt_id, JobState.CANCELLED.value)
                return
            lease = await database.acquire_scheduler_owner(lease_owner, ttl_seconds)
            if not lease.get("acquired"):
                await database.update_job(
                    job_id,
                    state=JobState.RECOVERY_REQUIRED.value,
                    stage="scheduler_lease_lost",
                    run_time_ms=elapsed_milliseconds(run_started),
                    failure_category="scheduler_lease_lost",
                    failure_message="ComfyUI native prompt lost the GPU scheduler lease before completion",
                )
                return
            history = await fetch_comfyui_history(prompt_id)
            if history is not None:
                idle_grace = max(0, settings.comfyui_prompt_idle_grace_seconds)
                if idle_grace:
                    await asyncio.sleep(idle_grace)
                artifacts = merge_job_artifacts(current.get("artifacts") or [], comfyui_artifacts_from_history(prompt_id, history))
                if not artifacts:
                    no_media = await database.update_job(
                        job_id,
                        state=JobState.RECOVERY_REQUIRED.value,
                        stage="comfyui_no_media_artifacts",
                        progress=90,
                        artifacts=[],
                        run_time_ms=elapsed_milliseconds(run_started),
                        failure_category="comfyui_no_media_artifacts",
                        failure_message=f"ComfyUI native prompt {prompt_id} completed without image, video, GIF, or audio outputs",
                    )
                    with suppress(Exception):
                        await mark_comfyui_native_runtime_idle(no_media, prompt_id, JobState.RECOVERY_REQUIRED.value)
                    return
                artifacts = await ingest_comfyui_artifacts(artifacts)
                failed_ingests = [artifact for artifact in artifacts if artifact.get("ingest_status") == "failed"]
                if failed_ingests:
                    failed = await database.update_job(
                        job_id,
                        state=JobState.RECOVERY_REQUIRED.value,
                        stage="artifact_ingest_failed",
                        progress=90,
                        artifacts=artifacts,
                        run_time_ms=elapsed_milliseconds(run_started),
                        failure_category="artifact_ingest_failed",
                        failure_message=f"{len(failed_ingests)} ComfyUI artifact(s) could not be stored",
                    )
                    with suppress(Exception):
                        await mark_comfyui_native_runtime_idle(failed, prompt_id, JobState.RECOVERY_REQUIRED.value)
                    return
                await database.update_job(job_id, state=JobState.SAVING.value, stage="comfyui_history_recorded", progress=90, artifacts=artifacts)
                completed = await database.update_job(
                    job_id,
                    state=JobState.COMPLETED.value,
                    stage="completed",
                    progress=100,
                    artifacts=artifacts,
                    run_time_ms=elapsed_milliseconds(run_started),
                )
                with suppress(Exception):
                    await mark_comfyui_native_runtime_idle(completed, prompt_id, JobState.COMPLETED.value)
                return
            await asyncio.sleep(poll_seconds)
        await database.update_job(
            job_id,
            state=JobState.RECOVERY_REQUIRED.value,
            stage="comfyui_history_timeout",
            run_time_ms=elapsed_milliseconds(run_started),
            failure_category="comfyui_history_timeout",
            failure_message=f"ComfyUI native prompt {prompt_id} did not reach history before timeout",
        )
    finally:
        await database.release_scheduler_owner(lease_owner)


def schedule_comfyui_prompt_tracker(job_id: str, prompt_id: str, lease_owner: str) -> None:
    task = asyncio.create_task(track_comfyui_prompt_completion(job_id, prompt_id, lease_owner), name=f"b1-comfyui-prompt-{job_id}")
    job_runner_tasks.append(task)

    def discard(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log_event("comfyui_prompt_tracker_failed", job_id=job_id, prompt_id=prompt_id, error=exc.__class__.__name__)
        try:
            job_runner_tasks.remove(done)
        except ValueError:
            pass

    task.add_done_callback(discard)


async def resume_comfyui_native_prompt_trackers(limit: int = 500) -> dict[str, Any]:
    rows = await database.list_resumable_comfyui_native_jobs(limit=limit)
    resumed = 0
    skipped = 0
    resumed_job_ids: list[str] = []
    native_prompt_ids: list[str] = []
    for row in rows:
        job_id = str(row.get("id") or "")
        prompt_id = row.get("native_prompt_id")
        if not job_id or not isinstance(prompt_id, str) or not prompt_id.strip():
            skipped += 1
            continue
        normalized_prompt_id = prompt_id.strip()
        schedule_comfyui_prompt_tracker(job_id, normalized_prompt_id, f"comfyui-prompt-{job_id}")
        resumed += 1
        if len(resumed_job_ids) < 50:
            resumed_job_ids.append(job_id)
            native_prompt_ids.append(normalized_prompt_id)
    return {
        "checked": len(rows),
        "resumed": resumed,
        "skipped": skipped,
        "resumed_job_ids": resumed_job_ids,
        "native_prompt_ids": native_prompt_ids,
    }


def annotate_runtime_response(body: Any, resolution: RuntimeResolution) -> Any:
    return body


def runtime_response_headers(resolution: RuntimeResolution) -> dict[str, str]:
    return {
        "X-B1-Runtime": resolution.runtime,
        "X-B1-Resolved-Model": resolution.resolved_model_version,
        "X-B1-Public-Model": resolution.public_alias,
    }


def model_tool_settings(policy_row: dict[str, Any] | None = None) -> model_tools.ModelToolSettings:
    if policy_row is not None:
        return model_tools.ModelToolSettings(
            enabled=bool(policy_row.get("enabled", True)),
            allowed_tools=tuple(str(item) for item in policy_row.get("allowed_tools") or [] if str(item).strip()),
            allow_private_network=bool(policy_row.get("allow_private_network", False)),
            allowed_hosts=tuple(str(item) for item in policy_row.get("allowed_hosts") or [] if str(item).strip()),
            max_result_chars=max(256, int(policy_row.get("max_result_chars") or 12000)),
            max_search_results=max(1, int(policy_row.get("max_search_results") or 5)),
            timeout_seconds=max(1.0, float(policy_row.get("timeout_seconds") or 12.0)),
            search_endpoint_template=str(policy_row.get("search_endpoint_template") or "https://duckduckgo.com/html/?q={query}"),
        )
    return model_tools.ModelToolSettings(
        enabled=settings.model_tools_enabled,
        allowed_tools=settings.model_tools_allowed,
        allow_private_network=settings.model_tools_allow_private_network,
        allowed_hosts=settings.model_tools_allowed_hosts,
        max_result_chars=max(256, settings.model_tools_max_result_chars),
        max_search_results=max(1, settings.model_tools_max_search_results),
        timeout_seconds=max(1.0, settings.model_tools_timeout_seconds),
        search_endpoint_template=settings.model_tools_search_endpoint_template or "https://duckduckgo.com/html/?q={query}",
    )


def model_tool_visible_to_auth(definition: model_tools.ModelToolDefinition, auth: AuthContext | None) -> bool:
    if not definition.visibility_roles:
        return True
    if auth is None:
        return False
    return auth.role.value in set(definition.visibility_roles)


async def effective_model_tool_policy_row() -> dict[str, Any] | None:
    return await database.get_model_tool_policy()


async def effective_model_tool_definitions(auth: AuthContext | None = None, *, include_disabled: bool = False) -> list[model_tools.ModelToolDefinition]:
    rows = await database.list_model_tool_definitions()
    definitions = [model_tools.tool_definition_from_row(row) for row in rows]
    return [definition for definition in definitions if (include_disabled or definition.enabled) and model_tool_visible_to_auth(definition, auth)]


async def model_tool_registry_for_auth(auth: AuthContext | None = None) -> model_tools.ModelToolRegistry:
    policy = await effective_model_tool_policy_row()
    return model_tools.ModelToolRegistry(
        model_tool_settings(policy),
        definitions=await effective_model_tool_definitions(auth),
        header_provider=model_tool_secret_headers,
    )


def public_model_tool_policy(policy_row: dict[str, Any] | None) -> dict[str, Any]:
    source = "database" if policy_row is not None else "environment"
    tool_settings = model_tool_settings(policy_row)
    return {
        "source": source,
        "enabled": tool_settings.enabled,
        "allowed_tools": list(tool_settings.allowed_tools),
        "allow_private_network": tool_settings.allow_private_network,
        "allowed_hosts": list(tool_settings.allowed_hosts),
        "max_result_chars": tool_settings.max_result_chars,
        "max_search_results": tool_settings.max_search_results,
        "timeout_seconds": tool_settings.timeout_seconds,
        "search_endpoint_template": tool_settings.search_endpoint_template,
        "notes": str(policy_row.get("notes") or "") if policy_row else "",
        "updated_by": policy_row.get("updated_by") if policy_row else None,
        "created_at": policy_row.get("created_at") if policy_row else None,
        "updated_at": policy_row.get("updated_at") if policy_row else None,
    }


MODEL_TOOL_HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
MODEL_TOOL_FORBIDDEN_SECRET_HEADER_NAMES = {
    "accept",
    "accept-encoding",
    "connection",
    "content-length",
    "content-type",
    "cookie",
    "host",
    "origin",
    "proxy-authorization",
    "referer",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "user-agent",
}


def public_model_tool_config(config: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in config.items():
        lowered = key.lower()
        if key != "auth" and ("token" in lowered or "secret" in lowered):
            continue
        if key == "auth" and isinstance(value, dict):
            auth_type = str(value.get("type") or "none")
            public_auth: dict[str, Any] = {
                "type": auth_type,
                "secret_name": str(value.get("secret_name") or ""),
                "secret_configured": bool(value.get("secret_name")),
            }
            if auth_type == "header":
                public_auth["header_name"] = str(value.get("header_name") or "")
            payload["auth"] = public_auth
            continue
        payload[key] = value
    return payload


def public_model_tool_definition(definition: model_tools.ModelToolDefinition | dict[str, Any]) -> dict[str, Any]:
    row = definition if isinstance(definition, dict) else {}
    item = model_tools.tool_definition_from_row(definition) if isinstance(definition, dict) else definition
    payload = {
        "name": item.name,
        "enabled": item.enabled,
        "kind": item.kind,
        "display_name": item.display_name,
        "description": item.description,
        "parameters_schema": item.parameters_schema,
        "config": public_model_tool_config(item.config),
        "visibility_roles": list(item.visibility_roles),
        "notes": item.notes,
    }
    if row:
        payload.update({"updated_by": row.get("updated_by"), "created_at": row.get("created_at"), "updated_at": row.get("updated_at")})
    return jsonable_encoder(payload)


def normalize_model_tool_name(name: str) -> str:
    value = name.strip().lower()
    if not model_tools.MODEL_TOOL_NAME_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail="tool name must start with a lowercase letter or digit and contain only lowercase letters, digits, dots, underscores, and hyphens")
    if value in model_tools.BUILTIN_TOOL_NAMES:
        raise HTTPException(status_code=422, detail="built-in model tools cannot be redefined")
    return value


def normalize_model_tool_names(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in values:
        value = str(item).strip().lower()
        if not model_tools.MODEL_TOOL_NAME_RE.fullmatch(value):
            raise HTTPException(status_code=422, detail=f"invalid tool name: {item}")
        if value not in normalized:
            normalized.append(value)
    return normalized


def normalize_model_tool_execution_name(name: str) -> str:
    value = str(name).strip().lower()
    if not model_tools.MODEL_TOOL_NAME_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"invalid tool name: {name}")
    return value


def normalize_model_tool_hosts(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in values:
        host = str(item).strip().lower().rstrip(".")
        if not host:
            continue
        if "/" in host or "\\" in host or "@" in host:
            raise HTTPException(status_code=422, detail=f"invalid model-tool host allowlist entry: {item}")
        if host == "localhost" or host.endswith(".localhost"):
            raise HTTPException(status_code=422, detail="model-tool host allowlist must not include localhost")
        if host not in normalized:
            normalized.append(host)
    return normalized


def validate_static_model_tool_url(value: str, *, allow_private_network: bool = False) -> str:
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "http"}:
        raise HTTPException(status_code=422, detail="model-tool URL must use http or https")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="model-tool URL must not contain credentials")
    if parsed.fragment:
        raise HTTPException(status_code=422, detail="model-tool URL must not contain a fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="model-tool URL has an invalid port") from exc
    hostname = parsed.hostname.lower().rstrip(".") if parsed.hostname else ""
    if not hostname:
        raise HTTPException(status_code=422, detail="model-tool URL must include a hostname")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise HTTPException(status_code=422, detail="model-tool URL must not target localhost")
    if model_tools.BAD_PERCENT_ESCAPE_RE.search(parsed.path):
        raise HTTPException(status_code=422, detail="model-tool URL path contains an invalid percent escape")
    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="model-tool URL path is not valid UTF-8") from exc
    if any(part in {".", ".."} for part in decoded_path.split("/") if part):
        raise HTTPException(status_code=422, detail="model-tool URL path must not contain relative path segments")
    with suppress(ValueError):
        ip = ipaddress.ip_address(hostname)
        if not model_tools.tool_ip_is_public(ip, allow_private_network=allow_private_network):
            raise HTTPException(status_code=422, detail="model-tool URL targets a blocked network range")
    return raw


def validate_model_tool_url_template(value: str, *, allow_private_network: bool = False) -> tuple[str, list[str]]:
    raw = value.strip()
    parsed = urlsplit(raw)
    if "{" in parsed.scheme or "}" in parsed.scheme or "{" in parsed.netloc or "}" in parsed.netloc:
        raise HTTPException(status_code=422, detail="model-tool URL templates may not modify scheme, host, port, or credentials")
    variables = list(model_tools.url_template_variables(raw))
    if ("{" in raw or "}" in raw) and not variables:
        raise HTTPException(status_code=422, detail="model-tool URL template placeholders must look like {argument_name}")
    expanded = raw
    if variables:
        try:
            expanded, _ = model_tools.expand_url_template(raw, {name: "template-test" for name in variables})
        except model_tools.ModelToolError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "{" in expanded or "}" in expanded:
        raise HTTPException(status_code=422, detail="model-tool URL template contains an invalid placeholder")
    validate_static_model_tool_url(expanded, allow_private_network=allow_private_network)
    return raw, variables


def validate_model_tool_policy_payload(payload: ModelToolPolicyRequest) -> dict[str, Any]:
    allowed_tools = normalize_model_tool_names(payload.allowed_tools)
    allowed_hosts = normalize_model_tool_hosts(payload.allowed_hosts)
    if "{query}" not in payload.search_endpoint_template:
        raise HTTPException(status_code=422, detail="search_endpoint_template must include {query}")
    validate_static_model_tool_url(payload.search_endpoint_template.replace("{query}", "test"), allow_private_network=payload.allow_private_network)
    return {
        "enabled": payload.enabled,
        "allowed_tools": allowed_tools,
        "allow_private_network": payload.allow_private_network,
        "allowed_hosts": allowed_hosts,
        "max_result_chars": payload.max_result_chars,
        "max_search_results": payload.max_search_results,
        "timeout_seconds": payload.timeout_seconds,
        "search_endpoint_template": payload.search_endpoint_template.strip(),
        "notes": payload.notes.strip(),
    }


def validate_model_tool_definition_payload(name: str, payload: ModelToolDefinitionRequest) -> dict[str, Any]:
    tool_name = normalize_model_tool_name(name)
    roles = []
    for role in payload.visibility_roles:
        value = str(role).strip().lower()
        if value not in {item.value for item in Role}:
            raise HTTPException(status_code=422, detail=f"unsupported visibility role: {role}")
        if value not in roles:
            roles.append(value)
    config = dict(payload.config)
    url, url_template_arguments = validate_model_tool_url_template(str(config.get("url") or ""))
    method = str(config.get("method") or "POST").strip().upper()
    if method not in {"GET", "POST"}:
        raise HTTPException(status_code=422, detail="http-json tool method must be GET or POST")
    config["url"] = url
    config["method"] = method
    if url_template_arguments:
        config["url_template_arguments"] = url_template_arguments
    else:
        config.pop("url_template_arguments", None)
    if "max_result_chars" in config:
        try:
            max_result_chars = int(config["max_result_chars"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="max_result_chars must be an integer") from exc
        if max_result_chars < 256 or max_result_chars > 200000:
            raise HTTPException(status_code=422, detail="max_result_chars must be between 256 and 200000")
        config["max_result_chars"] = max_result_chars
    if "json_result_path" in config:
        try:
            json_result_path = model_tools.validate_json_result_path(str(config.get("json_result_path") or ""))
        except model_tools.ModelToolError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if json_result_path:
            config["json_result_path"] = json_result_path
        else:
            config.pop("json_result_path", None)
    if "include_raw_json" in config and not isinstance(config["include_raw_json"], bool):
        raise HTTPException(status_code=422, detail="include_raw_json must be a boolean")
    if config.get("include_raw_json") is False and not config.get("json_result_path"):
        raise HTTPException(status_code=422, detail="include_raw_json=false requires json_result_path")
    if "headers" in config:
        raise HTTPException(status_code=422, detail="custom model tools do not accept arbitrary headers; use auth.type bearer or header with an encrypted integration secret")
    normalize_model_tool_auth_config(config)
    return {
        "name": tool_name,
        "enabled": payload.enabled,
        "kind": payload.kind,
        "display_name": payload.display_name.strip(),
        "description": payload.description.strip(),
        "parameters_schema": model_tools.safe_parameters_schema(payload.parameters_schema),
        "config": config,
        "visibility_roles": roles,
        "notes": payload.notes.strip(),
    }


async def public_model_tool_registry(auth: AuthContext | None = None, *, include_disabled_custom: bool = False) -> dict[str, Any]:
    policy = await effective_model_tool_policy_row()
    definitions = await effective_model_tool_definitions(auth, include_disabled=include_disabled_custom)
    enabled_definitions = [definition for definition in definitions if definition.enabled]
    registry = model_tools.ModelToolRegistry(model_tool_settings(policy), definitions=enabled_definitions)
    tool_names = sorted(registry.tool_names())
    return {
        "object": "b1.model_tools",
        "enabled": registry.settings.enabled,
        "allowed_tools": tool_names,
        "request_field": "b1_tools",
        "max_iterations_field": "b1_tool_max_iterations",
        "default_max_iterations": 4,
        # Tool iterations are completed before the final answer is emitted,
        # while the public chat endpoint still exposes standard OpenAI SSE.
        "streaming_supported": True,
        "streaming_mode": "final_answer_after_tool_execution",
        "allow_private_network": registry.settings.allow_private_network,
        "allowed_hosts": list(registry.settings.allowed_hosts),
        "max_result_chars": registry.settings.max_result_chars,
        "max_search_results": registry.settings.max_search_results,
        "search_endpoint_configured": bool(registry.settings.search_endpoint_template),
        "policy": public_model_tool_policy(policy),
        "custom_tools": [public_model_tool_definition(definition) for definition in definitions],
        "definitions": registry.definitions(tool_names),
        "security": {
            "explicit_request_required": True,
            "private_network_blocked_by_default": True,
            "credentials_in_urls_rejected": True,
            "bounded_output": True,
        },
    }


def chat_request_has_explicit_b1_tools(payload: ChatCompletionRequest) -> bool:
    return "b1_tools" in payload.model_fields_set


def chat_request_explicit_b1_tool_names(payload: ChatCompletionRequest) -> list[str]:
    if not chat_request_has_explicit_b1_tools(payload):
        return []
    return list(payload.b1_tools or [])


async def requested_b1_model_tools(payload: ChatCompletionRequest, auth: AuthContext | None = None) -> tuple[list[str], model_tools.ModelToolRegistry]:
    requested = list(payload.b1_tools or [])
    if not chat_request_has_explicit_b1_tools(payload) and not requested and auth is not None:
        requested = list(auth.default_b1_tools)
    if not requested:
        return [], model_tools.ModelToolRegistry(model_tools.ModelToolSettings(enabled=False, allowed_tools=()))
    registry = await model_tool_registry_for_auth(auth)
    if "*" in requested:
        return sorted(registry.tool_names()), registry
    return [tool for tool in requested if tool in registry.tool_names()], registry


def strip_b1_chat_fields(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    cleaned.pop("b1_tools", None)
    cleaned.pop("b1_tool_max_iterations", None)
    return cleaned


def strip_b1_response_fields(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    cleaned.pop("b1_tools", None)
    cleaned.pop("b1_tool_max_iterations", None)
    return cleaned


def merge_openai_tool_definitions(payload: dict[str, Any], definitions: list[dict[str, Any]]) -> dict[str, Any]:
    if not definitions:
        return payload
    merged = dict(payload)
    existing = merged.get("tools")
    tools = list(existing) if isinstance(existing, list) else []
    existing_names = {
        str(item.get("function", {}).get("name"))
        for item in tools
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    for definition in definitions:
        name = str(definition.get("function", {}).get("name")) if isinstance(definition.get("function"), dict) else ""
        if name and name not in existing_names:
            tools.append(definition)
            existing_names.add(name)
    merged["tools"] = tools
    return merged


def b1_text_tool_instruction(definitions: list[dict[str, Any]]) -> dict[str, str]:
    tool_summaries: list[dict[str, Any]] = []
    for definition in definitions:
        function = definition.get("function") if isinstance(definition, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        tool_summaries.append(
            {
                "name": name,
                "description": str(function.get("description") or "")[:500],
                "parameters": function.get("parameters") if isinstance(function.get("parameters"), dict) else {"type": "object"},
            }
        )
    content = (
        "B1 AI Hub tools are available for this request. If you need one, reply with only a JSON object in this exact shape: "
        '{"b1_tool_call":{"name":"tool_name","arguments":{}}}. '
        "Use only one tool call per message. Use the tool result in the next turn to answer the user. "
        "Do not claim tools are unavailable. Available B1 tools: "
        f"{json.dumps(tool_summaries, ensure_ascii=False, sort_keys=True)}"
    )
    return {"role": "system", "content": content}


def inject_b1_text_tool_instruction(payload: dict[str, Any], definitions: list[dict[str, Any]]) -> dict[str, Any]:
    if not definitions:
        return payload
    updated = dict(payload)
    messages = list(updated.get("messages") or [])
    messages.insert(0, b1_text_tool_instruction(definitions))
    updated["messages"] = messages
    return updated


def parse_b1_text_tool_call(content: Any, enabled_tools: set[str]) -> dict[str, Any] | None:
    if not isinstance(content, str) or not content.strip() or len(content) > 20000:
        return None
    candidate = content.strip()
    fence_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.IGNORECASE | re.DOTALL)
    if fence_match is not None:
        candidate = fence_match.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    tool_call = parsed.get("b1_tool_call")
    if not isinstance(tool_call, dict):
        return None
    name = str(tool_call.get("name") or "").strip()
    if name not in enabled_tools:
        return None
    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        return None
    return {
        "id": f"b1-text-tool-{uuid.uuid4().hex}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False, sort_keys=True)},
    }


def b1_text_tool_result_instruction(name: str, result: dict[str, Any]) -> dict[str, str]:
    compact_result = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if len(compact_result) > 6000:
        compact_result = f"{compact_result[:6000]}...[truncated]"
    return {
        "role": "system",
        "content": (
            f"B1 tool {name} returned this JSON result: {compact_result}\n"
            "Use the result to answer the user directly. Do not call another tool unless the result is insufficient."
        ),
    }


def b1_tool_call_signature(tool_call: dict[str, Any]) -> tuple[str, str]:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    name = str(function.get("name") or "")
    arguments = model_tools.parse_tool_arguments(function.get("arguments"))
    return name, json.dumps(arguments, ensure_ascii=False, sort_keys=True)


def force_b1_tool_answer_payload(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    forced = dict(payload)
    forced.pop("tools", None)
    forced["tool_choice"] = "none"
    messages = [
        message
        for message in list(forced.get("messages") or [])
        if not (
            isinstance(message, dict)
            and message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and message["content"].startswith("B1 AI Hub tools are available for this request.")
        )
    ]
    messages.append(
        {
            "role": "system",
            "content": (
                f"B1 tool use is now closed for this response because {reason}. "
                "Answer the original user from the existing tool result messages. "
                "Do not include JSON, tool call objects, tool names, or raw tool-result dumps in the final answer."
            ),
        }
    )
    messages.append({"role": "user", "content": "Return only the final answer to my original request."})
    forced["messages"] = messages
    return forced


def b1_last_user_text(payload: ChatCompletionRequest) -> str:
    for message in reversed(payload.messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts)
    return ""


def b1_user_wants_title_only(text: str) -> bool:
    normalized = text.lower()
    return "title only" in normalized or "page title" in normalized or "answer with the title" in normalized


def b1_tool_result_excerpt(value: Any, *, max_chars: int = 1600) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def find_json_scalar_key(value: Any, key_name: str, *, max_depth: int = 6) -> str | None:
    if max_depth < 0:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == key_name.lower() and isinstance(item, (str, int, float, bool)):
                return str(item)
        for item in value.values():
            found = find_json_scalar_key(item, key_name, max_depth=max_depth - 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value[:20]:
            found = find_json_scalar_key(item, key_name, max_depth=max_depth - 1)
            if found is not None:
                return found
    return None


def synthesize_custom_json_answer(user_text: str, result: dict[str, Any]) -> str | None:
    normalized = user_text.lower()
    if "message" in normalized or "echo" in normalized:
        for container_key in ("json", "data"):
            container = result.get(container_key)
            found = find_json_scalar_key(container, "message")
            if found:
                return b1_tool_result_excerpt(found, max_chars=1000)
        found = find_json_scalar_key(result, "message")
        if found:
            return b1_tool_result_excerpt(found, max_chars=1000)
    return None


def synthesize_b1_tool_answer_content(payload: ChatCompletionRequest, executed_results: list[dict[str, Any]]) -> str | None:
    if not executed_results:
        return None
    user_text = b1_last_user_text(payload)
    latest = executed_results[-1]
    name = str(latest.get("name") or "")
    result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
    if not isinstance(result, dict):
        return None
    if not result.get("ok"):
        error = result.get("error") or "tool execution failed"
        return f"The requested tool call failed: {b1_tool_result_excerpt(error, max_chars=500)}"
    if name == "web_fetch" or result.get("tool") == "web_fetch":
        title = str(result.get("title") or "").strip()
        text = str(result.get("text") or "").strip()
        url = str(result.get("url") or "").strip()
        if b1_user_wants_title_only(user_text) and title:
            return title
        if b1_user_wants_title_only(user_text) and text:
            first_words = text.split()
            if len(first_words) >= 2 and first_words[: len(first_words) // 2] == first_words[len(first_words) // 2 :]:
                return " ".join(first_words[: len(first_words) // 2])
            return " ".join(first_words[:8]).strip()
        source = f" from {url}" if url else ""
        if title and text:
            return f"{title}\n\nFetched{source}: {b1_tool_result_excerpt(text)}"
        if text:
            return f"Fetched{source}: {b1_tool_result_excerpt(text)}"
    if name == "web_search" or result.get("tool") == "web_search":
        results = result.get("results")
        if isinstance(results, list) and results:
            lines = []
            for index, item in enumerate(results[:5], start=1):
                if not isinstance(item, dict):
                    continue
                title = b1_tool_result_excerpt(item.get("title") or "Untitled", max_chars=240)
                url = b1_tool_result_excerpt(item.get("url") or "", max_chars=500)
                lines.append(f"{index}. {title} - {url}" if url else f"{index}. {title}")
            if lines:
                return "\n".join(lines)
        return "No search results were returned."
    custom_json = synthesize_custom_json_answer(user_text, result)
    if custom_json is not None:
        return custom_json
    if "text" in result:
        return b1_tool_result_excerpt(result["text"])
    if "json" in result:
        return b1_tool_result_excerpt(result["json"])
    return b1_tool_result_excerpt(result)


def synthesize_b1_tool_response(
    payload: ChatCompletionRequest,
    resolution: RuntimeResolution,
    executed_results: list[dict[str, Any]],
    *,
    iterations: int,
    requested_tools: list[str],
    stop_reason: str,
) -> JSONResponse | None:
    content = synthesize_b1_tool_answer_content(payload, executed_results)
    if content is None:
        return None
    body = annotate_runtime_response(
        {
            "id": f"chatcmpl_b1_tool_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(datetime.now(tz=UTC).timestamp()),
            "model": payload.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "b1_tool_answer_synthesized": True,
            "b1_tool_stop_reason": stop_reason,
        },
        resolution,
    )
    return JSONResponse(
        content=jsonable_encoder(body),
        headers={
            **runtime_response_headers(resolution),
            "X-B1-Tools": ",".join(requested_tools),
            "X-B1-Tool-Iterations": str(iterations),
            "X-B1-Tool-Stop-Reason": stop_reason,
            "X-B1-Tool-Answer": "synthesized",
        },
    )


def first_chat_message_tool_calls(body: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(body, dict):
        return None, []
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, []
    first = choices[0]
    if not isinstance(first, dict):
        return None, []
    message = first.get("message")
    if not isinstance(message, dict):
        return None, []
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return message, []
    return message, [item for item in tool_calls if isinstance(item, dict)]


def json_response_body(response: JSONResponse) -> Any:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def response_content_part_to_chat(part: Any) -> Any:
    if not isinstance(part, dict):
        return part
    part_type = str(part.get("type") or "")
    if part_type in {"input_text", "output_text"} and isinstance(part.get("text"), str):
        return {"type": "text", "text": part["text"]}
    if part_type == "input_image":
        image_url = part.get("image_url")
        if isinstance(image_url, str):
            return {"type": "image_url", "image_url": {"url": image_url}}
        if isinstance(image_url, dict):
            return {"type": "image_url", "image_url": image_url}
    return part


def response_message_to_chat(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        return {"role": "user", "content": item}
    if not isinstance(item, dict):
        return None
    role = str(item.get("role") or "user")
    content = item.get("content")
    if isinstance(content, list):
        converted = [response_content_part_to_chat(part) for part in content]
    else:
        converted = content if content is not None else ""
    return {"role": role, "content": converted}


def responses_payload_to_chat_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    response_input = payload.get("input", "")
    if isinstance(response_input, str):
        messages.append({"role": "user", "content": response_input})
    elif isinstance(response_input, dict):
        message = response_message_to_chat(response_input)
        if message is not None:
            messages.append(message)
    elif isinstance(response_input, list):
        for item in response_input:
            message = response_message_to_chat(item)
            if message is not None:
                messages.append(message)
    if not messages:
        raise HTTPException(status_code=422, detail="Responses tool calls require convertible text or message input")
    chat_payload: dict[str, Any] = {
        "model": payload.get("model", "chat-default"),
        "messages": messages,
        "runtime_policy": payload.get("runtime_policy", "any"),
        "stream": bool(payload.get("stream", False)),
    }
    if "temperature" in payload:
        chat_payload["temperature"] = payload["temperature"]
    if "max_output_tokens" in payload:
        chat_payload["max_tokens"] = payload["max_output_tokens"]
    elif "max_tokens" in payload:
        chat_payload["max_tokens"] = payload["max_tokens"]
    for key in ("top_p", "frequency_penalty", "presence_penalty", "stop", "user"):
        if key in payload:
            chat_payload[key] = payload[key]
    if "b1_tools" in payload:
        chat_payload["b1_tools"] = payload.get("b1_tools")
    if "b1_tool_max_iterations" in payload:
        chat_payload["b1_tool_max_iterations"] = payload.get("b1_tool_max_iterations")
    return chat_payload


def chat_tool_response_to_responses_response(original_payload: dict[str, Any], chat_response: JSONResponse) -> Response:
    if chat_response.status_code >= 400:
        return chat_response
    body = json_response_body(chat_response)
    if not isinstance(body, dict):
        return chat_response
    choices = body.get("choices")
    message: dict[str, Any] = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        candidate = choices[0].get("message")
        if isinstance(candidate, dict):
            message = candidate
    content = message.get("content")
    if content is None:
        output_text = ""
    elif isinstance(content, str):
        output_text = content
    else:
        output_text = json.dumps(content, ensure_ascii=False, sort_keys=True)
    created = body.get("created")
    if not isinstance(created, int):
        created = int(datetime.now(tz=UTC).timestamp())
    wrapped: dict[str, Any] = {
        "id": f"resp_b1_tool_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": original_payload.get("model", body.get("model", "chat-default")),
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": output_text, "annotations": []}],
            }
        ],
        "output_text": output_text,
    }
    if "usage" in body:
        wrapped["usage"] = body["usage"]
    for key in ("b1_tool_answer_synthesized", "b1_tool_stop_reason", "b1_runtime", "b1_resolved_model", "b1_public_model"):
        if key in body:
            wrapped[key] = body[key]
    return JSONResponse(
        content=jsonable_encoder(wrapped),
        status_code=chat_response.status_code,
        headers={key: value for key, value in chat_response.headers.items() if key.lower().startswith("x-b1-")},
    )


async def execute_b1_tool_call(registry: model_tools.ModelToolRegistry, tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return {"ok": False, "error": "tool call is missing a function object"}
    name = str(function.get("name") or "")
    arguments = model_tools.parse_tool_arguments(function.get("arguments"))
    try:
        return await registry.execute(name, arguments)
    except model_tools.ModelToolError as exc:
        return {"ok": False, "tool": name, "error": str(exc)}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "tool": name, "error": f"HTTP {exc.response.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "tool": name, "error": exc.__class__.__name__}


async def call_chat_with_b1_tools(
    payload: ChatCompletionRequest,
    runtime_payload: dict[str, Any],
    resolution: RuntimeResolution,
    requested_tools: list[str],
    registry: model_tools.ModelToolRegistry,
    owner_id: str | None,
) -> JSONResponse:
    if payload.b1_tools and not requested_tools:
        raise HTTPException(status_code=422, detail="no requested B1 model tools are enabled")
    tool_definitions = registry.definitions(requested_tools)
    loop_payload = inject_b1_text_tool_instruction(
        merge_openai_tool_definitions(strip_b1_chat_fields(runtime_payload), tool_definitions),
        tool_definitions,
    )
    loop_payload["messages"] = list(loop_payload.get("messages") or [])
    enabled_tool_set = set(requested_tools)
    seen_tool_signatures: set[tuple[str, str]] = set()
    executed_results: list[dict[str, Any]] = []
    for iteration in range(payload.b1_tool_max_iterations):
        response: JSONResponse | None = None
        # LocalAI may briefly return 502/503/504 while replacing its single
        # backend process during a model switch. Retry only those transient
        # statuses; never replay a successful or client-error request.
        for attempt in range(3):
            response = await call_openai_runtime_json(
                "/v1/chat/completions",
                loop_payload,
                resolution,
                "chat",
                owner_id=owner_id,
            )
            if response is None or response.status_code not in {502, 503, 504} or attempt == 2:
                break
            await asyncio.sleep(0.5 * (2**attempt))
        if response is None:
            break
        if response.status_code >= 400:
            return response
        body = json_response_body(response)
        assistant_message, tool_calls = first_chat_message_tool_calls(body)
        if not tool_calls and assistant_message is not None:
            text_tool_call = parse_b1_text_tool_call(assistant_message.get("content"), enabled_tool_set)
            if text_tool_call is not None:
                tool_calls = [text_tool_call]
                assistant_message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                }
        if not tool_calls:
            response.headers["X-B1-Tool-Iterations"] = str(iteration)
            response.headers["X-B1-Tools"] = ",".join(requested_tools)
            return response
        if assistant_message is not None:
            loop_payload["messages"].append(assistant_message)
        duplicate_tool_requested = False
        for tool_call in tool_calls:
            tool_call_id = str(tool_call.get("id") or f"b1-tool-{uuid.uuid4().hex}")
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            name = str(function.get("name") or "")
            signature = b1_tool_call_signature(tool_call)
            if signature in seen_tool_signatures:
                duplicate_tool_requested = True
                continue
            seen_tool_signatures.add(signature)
            result = await execute_b1_tool_call(registry, tool_call)
            executed_results.append({"name": name, "result": result})
            loop_payload["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False, sort_keys=True),
                }
            )
            loop_payload["messages"].append(b1_text_tool_result_instruction(name, result))
        if duplicate_tool_requested:
            synthesized = synthesize_b1_tool_response(
                payload,
                resolution,
                executed_results,
                iterations=iteration,
                requested_tools=requested_tools,
                stop_reason="duplicate_tool_call",
            )
            if synthesized is not None:
                return synthesized
            response = await call_openai_runtime_json(
                "/v1/chat/completions",
                force_b1_tool_answer_payload(loop_payload, "the model repeated an identical tool call"),
                resolution,
                "chat",
                owner_id=owner_id,
            )
            if response is not None:
                response.headers["X-B1-Tool-Iterations"] = str(iteration)
                response.headers["X-B1-Tools"] = ",".join(requested_tools)
                response.headers["X-B1-Tool-Stop-Reason"] = "duplicate_tool_call"
                return response
    synthesized = synthesize_b1_tool_response(
        payload,
        resolution,
        executed_results,
        iterations=payload.b1_tool_max_iterations,
        requested_tools=requested_tools,
        stop_reason="max_iterations",
    )
    if synthesized is not None:
        return synthesized
    response = await call_openai_runtime_json(
        "/v1/chat/completions",
        force_b1_tool_answer_payload(loop_payload, "the configured B1 tool iteration limit was reached"),
        resolution,
        "chat",
        owner_id=owner_id,
    )
    if response is not None:
        response.headers["X-B1-Tool-Iterations"] = str(payload.b1_tool_max_iterations)
        response.headers["X-B1-Tools"] = ",".join(requested_tools)
        response.headers["X-B1-Tool-Stop-Reason"] = "max_iterations"
        return response
    raise HTTPException(status_code=502, detail=f"runtime {resolution.runtime} did not produce a proxied chat response")


def chat_tool_response_to_stream(chat_response: JSONResponse) -> Response:
    """Expose a completed tool-loop answer as an OpenAI chat SSE stream."""
    if chat_response.status_code >= 400:
        return chat_response
    body = json_response_body(chat_response)
    if not isinstance(body, dict):
        return chat_response
    choices = body.get("choices")
    first_choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = first_choice.get("message") if isinstance(first_choice, dict) else {}
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    if content is None:
        text_content = ""
    elif isinstance(content, str):
        text_content = content
    else:
        text_content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    response_id = str(body.get("id") or f"chatcmpl_b1_tool_{uuid.uuid4().hex}")
    model = str(body.get("model") or "chat-default")
    created = body.get("created")
    if not isinstance(created, int):
        created = int(datetime.now(tz=UTC).timestamp())
    finish_reason = first_choice.get("finish_reason") if isinstance(first_choice, dict) else None
    if not isinstance(finish_reason, str):
        finish_reason = "stop"
    chunks: list[dict[str, Any]] = [
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]
    if text_content:
        for offset in range(0, len(text_content), 256):
            chunks.append(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": text_content[offset : offset + 256]}, "finish_reason": None}],
                }
            )
    final_chunk: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    if isinstance(body.get("usage"), dict):
        final_chunk["usage"] = body["usage"]
    chunks.append(final_chunk)

    async def events():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    headers = {key: value for key, value in chat_response.headers.items() if key.lower().startswith("x-b1-")}
    headers.update({"Cache-Control": "no-cache", "Connection": "keep-alive"})
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


def runtime_prepare_error_detail(exc: RuntimePreparationError, resolution: RuntimeResolution, operation: str) -> dict[str, Any]:
    return {
        "message": str(exc),
        "type": "runtime_prepare_failed",
        "operation": operation,
        "runtime": resolution.runtime,
        "resolved_model": resolution.resolved_model_version,
        "public_model": resolution.public_alias,
        "retryable": False,
    }


async def call_openai_runtime_json(
    path: str,
    payload: dict[str, Any],
    resolution: RuntimeResolution,
    operation: str,
    owner_id: str | None = None,
) -> JSONResponse | None:
    adapter = openai_runtime_adapter(resolution)
    if adapter is None:
        return None
    forwarded = adapter.openai_payload(payload, resolution)
    owner = await acquire_inference_lease(resolution, operation, owner_id=owner_id)
    prepared = False
    try:
        prepared = await prepare_sync_gpu_runtime(resolution, operation)
        status_code, headers, body = await await_with_inference_lease_renewal(
            owner,
            operation,
            adapter.post_openai_json(path, forwarded),
        )
    except RuntimePreparationError as exc:
        raise HTTPException(status_code=503, detail=runtime_prepare_error_detail(exc, resolution, operation)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{resolution.runtime} runtime request failed: {exc.__class__.__name__}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        if prepared:
            with suppress(Exception):
                await mark_sync_gpu_runtime_idle(resolution, operation)
        await release_inference_lease(owner)
    safe_headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
    safe_headers.update(runtime_response_headers(resolution))
    return JSONResponse(status_code=status_code, content=jsonable_encoder(annotate_runtime_response(body, resolution)), headers=safe_headers)


async def call_openai_runtime_stream(
    path: str,
    payload: dict[str, Any],
    resolution: RuntimeResolution,
    operation: str,
    owner_id: str | None = None,
) -> StreamingResponse | None:
    adapter = openai_runtime_adapter(resolution)
    if adapter is None:
        return None
    forwarded = adapter.openai_payload(payload, resolution)
    owner = await acquire_inference_lease(resolution, operation, owner_id=owner_id)
    prepared = False
    try:
        prepared = await prepare_sync_gpu_runtime(resolution, operation)
    except RuntimePreparationError as exc:
        await release_inference_lease(owner)
        raise HTTPException(status_code=503, detail=runtime_prepare_error_detail(exc, resolution, operation)) from exc
    except RuntimeError as exc:
        await release_inference_lease(owner)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception:
        await release_inference_lease(owner)
        raise

    finalized = False
    finalize_lock = asyncio.Lock()

    async def finalize_stream() -> None:
        nonlocal finalized
        async with finalize_lock:
            if finalized:
                return
            finalized = True
            if prepared:
                with suppress(Exception):
                    await mark_sync_gpu_runtime_idle(resolution, operation)
            await release_inference_lease(owner)

    async def chunks():
        last_renewed = datetime.now(tz=UTC)
        renew_interval = max(15, min(60, settings.sync_inference_lease_ttl_seconds // 3))
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", adapter.openai_url(path), json=forwarded, headers={"Accept": "text/event-stream"}) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        error = {
                            "error": {
                                "message": body[:1000],
                                "type": "runtime_error",
                                "code": response.status_code,
                            },
                            "b1_runtime": resolution.runtime,
                            "b1_resolved_model": resolution.resolved_model_version,
                            "b1_public_model": resolution.public_alias,
                        }
                        yield f"data: {json.dumps(error)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            now = datetime.now(tz=UTC)
                            if owner is not None and (now - last_renewed).total_seconds() >= renew_interval:
                                if not await renew_inference_lease(owner):
                                    error = {
                                        "error": {
                                            "message": "GPU scheduler lease was lost during streaming response",
                                            "type": "scheduler_lease_lost",
                                        },
                                        "b1_runtime": resolution.runtime,
                                        "b1_resolved_model": resolution.resolved_model_version,
                                        "b1_public_model": resolution.public_alias,
                                    }
                                    yield f"data: {json.dumps(error)}\n\n"
                                    yield "data: [DONE]\n\n"
                                    return
                                last_renewed = now
                            yield chunk
        except httpx.HTTPError as exc:
            error = {
                "error": {
                    "message": f"{resolution.runtime} runtime request failed: {exc.__class__.__name__}",
                    "type": "runtime_proxy_error",
                },
                "b1_runtime": resolution.runtime,
                "b1_resolved_model": resolution.resolved_model_version,
                "b1_public_model": resolution.public_alias,
            }
            yield f"data: {json.dumps(error)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            await finalize_stream()

    return StreamingResponse(chunks(), media_type="text/event-stream", background=BackgroundTask(finalize_stream))


def media_job_links(job_id: str) -> dict[str, str]:
    encoded_job_id = quote(str(job_id), safe="")
    base = f"/v1/media/jobs/{encoded_job_id}"
    return {
        "self": base,
        "events": f"{base}/events",
        "artifacts": f"{base}/artifacts",
        "cancel": base,
    }


def is_native_comfyui_job(job: dict[str, Any]) -> bool:
    return (
        job.get("runtime") == "comfyui"
        and (
            job.get("operation") == "comfyui-prompt"
            or job.get("model_alias") == "comfyui-native"
            or isinstance(job.get("native_prompt_id"), str)
        )
    )


def public_native_prompt_summary(raw_request: Any) -> dict[str, Any]:
    if not isinstance(raw_request, dict):
        return {"available": False}
    request_input = raw_request.get("input")
    if not isinstance(request_input, dict):
        return {"available": False}
    prompt_summary = request_input.get("prompt_summary")
    if not isinstance(prompt_summary, dict):
        return {
            "available": True,
            "client_id_present": isinstance(request_input.get("client_id"), str) and bool(str(request_input.get("client_id")).strip()),
            "body_hash_present": isinstance(request_input.get("native_prompt_hash"), str) and bool(str(request_input.get("native_prompt_hash")).strip()),
        }
    safe_summary: dict[str, Any] = {
        "available": True,
        "client_id_present": isinstance(request_input.get("client_id"), str) and bool(str(request_input.get("client_id")).strip()),
        "body_hash_present": isinstance(request_input.get("native_prompt_hash"), str) and bool(str(request_input.get("native_prompt_hash")).strip()),
    }
    for key in (
        "has_prompt",
        "has_client_id",
        "node_count",
        "class_type_count",
        "malformed_node_count",
        "unknown_top_level_key_count",
        "prompt_shape",
        "payload_type",
    ):
        value = prompt_summary.get(key)
        if isinstance(value, (str, bool, int, float)) or value is None:
            safe_summary[key] = value
    known_keys = prompt_summary.get("known_top_level_keys")
    if isinstance(known_keys, list):
        safe_summary["known_top_level_keys"] = sorted(str(item) for item in known_keys if isinstance(item, str))[:20]
    safe_summary["class_type_digest_present"] = isinstance(prompt_summary.get("class_type_digest"), str) and bool(prompt_summary.get("class_type_digest"))
    safe_summary["node_id_digest_present"] = isinstance(prompt_summary.get("node_id_digest"), str) and bool(prompt_summary.get("node_id_digest"))
    return safe_summary


def public_native_comfyui_artifact_summary(job: dict[str, Any]) -> dict[str, Any]:
    artifacts = [artifact for artifact in job.get("artifacts") or [] if isinstance(artifact, dict)]
    stored = [artifact for artifact in artifacts if artifact.get("source") == "artifact_store" or artifact.get("ingest_status") == "stored"]
    failed = [artifact for artifact in artifacts if artifact.get("ingest_status") == "failed"]
    view_pending = [artifact for artifact in artifacts if artifact.get("source") == "comfyui_view" and artifact.get("ingest_status") not in {"stored", "failed"}]
    kinds = sorted({str(artifact.get("kind")) for artifact in artifacts if isinstance(artifact.get("kind"), str) and artifact.get("kind")})
    total_bytes = sum(int(artifact["bytes"]) for artifact in stored if isinstance(artifact.get("bytes"), int) and artifact["bytes"] >= 0)
    return {
        "artifact_count": len(artifacts),
        "stored_artifact_count": len(stored),
        "failed_ingest_count": len(failed),
        "pending_view_artifact_count": len(view_pending),
        "artifact_kinds": kinds,
        "stored_bytes": total_bytes,
    }


def native_comfyui_public_job_summary(job: dict[str, Any], raw_request: Any) -> dict[str, Any] | None:
    if not is_native_comfyui_job(job):
        return None
    native_prompt_id = job.get("native_prompt_id") if isinstance(job.get("native_prompt_id"), str) else None
    summary = {
        "compatibility_job": True,
        "native_prompt_id": native_prompt_id,
        "native_prompt_recorded": bool(native_prompt_id),
        "tracker_terminal": job.get("state") in TERMINAL_JOB_STATES,
        "state": job.get("state"),
        "stage": job.get("stage"),
        "progress": job.get("progress"),
        "prompt": public_native_prompt_summary(raw_request),
        "artifacts": public_native_comfyui_artifact_summary(job),
    }
    return {key: value for key, value in summary.items() if value is not None}


def redact_native_comfyui_public_request(redacted_request: dict[str, Any], raw_request: Any) -> dict[str, Any]:
    request = dict(redacted_request)
    request["input"] = {"native_comfyui": public_native_prompt_summary(raw_request)}
    return request


def public_job(row: dict[str, Any]) -> dict[str, Any]:
    job = dict(row)
    raw_request = job.pop("request_params", None)
    job.pop("idempotency_key", None)
    redacted_request = job.get("redacted_request")
    if not isinstance(redacted_request, dict):
        redacted_request = redact_request(raw_request) if isinstance(raw_request, dict) else {}
    if is_native_comfyui_job(job):
        redacted_request = redact_native_comfyui_public_request(redacted_request, raw_request)
    job["redacted_request"] = redacted_request
    native_comfyui = native_comfyui_public_job_summary(job, raw_request)
    if native_comfyui is not None:
        job["native_comfyui"] = native_comfyui
    if job.get("id"):
        job["links"] = media_job_links(str(job["id"]))
    return jsonable_encoder(job)


def public_jobs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [public_job(row) for row in rows]


def optional_query_value(value: Any) -> Any | None:
    if getattr(value, "default", object()) is None:
        return None
    return value


def normalize_idempotency_key(idempotency_key: Any) -> str | None:
    if idempotency_key is None:
        return None
    if not isinstance(idempotency_key, str):
        if getattr(idempotency_key, "default", object()) is None:
            return None
        raise HTTPException(status_code=422, detail={"code": "invalid_idempotency_key", "message": "Idempotency-Key must be a string"})
    normalized = idempotency_key.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail={"code": "invalid_idempotency_key", "message": "Idempotency-Key must not be empty"})
    if len(normalized) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_idempotency_key",
                "message": f"Idempotency-Key must be {IDEMPOTENCY_KEY_MAX_LENGTH} characters or fewer",
            },
        )
    if any(ord(character) < 33 or ord(character) > 126 for character in normalized):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_idempotency_key", "message": "Idempotency-Key must contain only visible ASCII header characters"},
        )
    return normalized


def expected_job_identity(request_payload: MediaJobCreate) -> dict[str, Any]:
    return {
        "modality": request_payload.modality,
        "operation": request_payload.operation,
        "model_alias": request_payload.model,
        "priority": request_payload.priority,
        "request_params": jsonable_encoder(request_payload.model_dump()),
    }


def ensure_idempotent_job_matches(existing: dict[str, Any], request_payload: MediaJobCreate, resolution: RuntimeResolution | None = None) -> None:
    expected = expected_job_identity(request_payload)
    mismatched_fields = [field for field in IDEMPOTENCY_IDENTITY_FIELDS if existing.get(field) != expected[field]]
    if existing.get("request_params") != expected["request_params"]:
        mismatched_fields.append("request_params")
    if not mismatched_fields:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "idempotency_key_conflict",
            "message": "Idempotency-Key was already used for a different job request",
            "job_id": existing.get("id"),
            "mismatched_fields": mismatched_fields,
        },
    )


def ensure_existing_job_endpoint(existing: dict[str, Any], *, modality: str, operation: str) -> None:
    mismatched_fields = [
        field
        for field, expected in (("modality", modality), ("operation", operation))
        if existing.get(field) != expected
    ]
    if not mismatched_fields:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "idempotency_key_conflict",
            "message": "Idempotency-Key was already used for a different job request",
            "job_id": existing.get("id"),
            "mismatched_fields": mismatched_fields,
        },
    )


def expected_runtime_reservation_identity(payload: RuntimeReservationCreate) -> dict[str, Any]:
    runtime = payload.runtime.strip()
    model_alias = payload.model.strip()
    reason = payload.reason.strip()
    if not runtime or not RUNTIME_RESERVATION_RUNTIME_PATTERN.fullmatch(runtime):
        raise HTTPException(status_code=422, detail="runtime must be a safe runtime adapter identifier")
    if not model_alias:
        raise HTTPException(status_code=422, detail="model must be a safe model alias")
    if len(reason) > RUNTIME_RESERVATION_REASON_MAX_LENGTH:
        raise HTTPException(status_code=422, detail=f"reason must be at most {RUNTIME_RESERVATION_REASON_MAX_LENGTH} characters")
    model_alias = validate_modelhub_model_identifier(model_alias)
    return {
        "runtime": runtime,
        "model_alias": model_alias,
        "duration_seconds": payload.duration_seconds,
        "reason": reason,
    }


def stored_runtime_reservation_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime": str(row.get("runtime") or "").strip(),
        "model_alias": str(row.get("model_alias") or "").strip(),
        "duration_seconds": row.get("duration_seconds"),
        "reason": str(row.get("reason") or "").strip(),
    }


def ensure_idempotent_runtime_reservation_matches(existing: dict[str, Any], payload: RuntimeReservationCreate) -> None:
    expected = expected_runtime_reservation_identity(payload)
    stored = stored_runtime_reservation_identity(existing)
    mismatched_fields = [field for field, value in expected.items() if stored.get(field) != value]
    if not mismatched_fields:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "idempotency_key_conflict",
            "message": "Idempotency-Key was already used for a different runtime reservation request",
            "reservation_id": existing.get("id"),
            "mismatched_fields": mismatched_fields,
        },
    )


def openai_image_job_response(job: dict[str, Any]) -> dict[str, Any]:
    links = media_job_links(str(job["id"]))
    return {
        "object": "b1.async_job",
        "created": int(datetime.now(tz=UTC).timestamp()),
        "b1_job_id": job["id"],
        "b1_status": job.get("state"),
        "b1_job_url": links["self"],
        "b1_events_url": links["events"],
        "b1_artifacts_url": links["artifacts"],
        "b1_cancel_url": links["cancel"],
        "data": [],
    }


def comfyui_native_job_payload(
    client_id: str | None,
    native_prompt_hash: str,
    prompt_summary: dict[str, Any],
    *,
    model_free_smoke: bool = False,
    model_free_kind: str | None = None,
) -> MediaJobCreate:
    kind = "model-free-smoke" if model_free_smoke else model_free_kind
    input_payload = {"client_id": client_id, "native_prompt_hash": native_prompt_hash, "prompt_summary": prompt_summary}
    if kind in {"model-free-smoke", "model-free-stock-io"}:
        input_payload["model_free_native"] = True
        input_payload["model_free_kind"] = kind
    if kind == "model-free-smoke":
        input_payload["model_free_smoke"] = True
    return MediaJobCreate(
        modality="workflow",
        operation="comfyui-prompt",
        model="comfyui-native",
        input=input_payload,
        priority="single_image",
        runtime_policy="comfyui_native",
    )


def comfyui_idempotent_prompt_response(job: dict[str, Any]) -> Response:
    prompt_id = job.get("native_prompt_id")
    if isinstance(prompt_id, str) and prompt_id:
        return Response(
            content=json.dumps({"prompt_id": prompt_id, "node_errors": {}}, separators=(",", ":")),
            media_type="application/json",
            headers={"X-B1-Idempotent-Replay": "true", "X-B1-Job-Id": str(job.get("id") or "")},
        )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "comfyui_prompt_replay_pending",
            "message": "Idempotency-Key matches a ComfyUI prompt that has not returned a native prompt_id yet",
            "job_id": job.get("id"),
            "state": job.get("state"),
            "stage": job.get("stage"),
        },
        headers={"Retry-After": "1"},
    )


def media_job_string_extension(payload: dict[str, Any], key: str, default: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


async def create_job_record(
    owner: str,
    request_payload: MediaJobCreate,
    idempotency_key: str | None = None,
    resolution: RuntimeResolution | None = None,
) -> dict[str, Any]:
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
    if normalized_idempotency_key:
        existing = await database.get_job_by_idempotency_key(owner, normalized_idempotency_key)
        if existing is not None:
            ensure_idempotent_job_matches(existing, request_payload, resolution)
            return existing
    require_not_in_maintenance(f"{request_payload.modality}/{request_payload.operation}")
    await enforce_gpu_hardware_admission(resolution)
    await enforce_queue_admission(owner)
    enforce_artifact_storage_headroom(0)
    job_id = f"job_{uuid.uuid4().hex}"
    payload = {
        "id": job_id,
        "correlation_id": uuid.uuid4().hex,
        "idempotency_key": normalized_idempotency_key,
        "owner_id": owner,
        "modality": request_payload.modality,
        "operation": request_payload.operation,
        "model_alias": request_payload.model,
        "priority": request_payload.priority,
        "resolved_model_version": resolution.resolved_model_version if resolution else "unresolved",
        "runtime": resolution.runtime if resolution else "unassigned",
        "request_params": request_payload.model_dump(),
        "redacted_request": redact_request(request_payload.model_dump()),
    }
    job = await database.insert_job(payload)
    if job["id"] != job_id:
        ensure_idempotent_job_matches(job, request_payload, resolution)
        return job
    await database.update_job(job_id, state=JobState.VALIDATED.value, stage="validated", progress=5)
    queued_job = await database.update_job(job_id, state=JobState.QUEUED.value, stage="queued", progress=10)
    log_event("job_created", job_id=job_id, modality=request_payload.modality, operation=request_payload.operation, model=request_payload.model)
    return queued_job or job


@app.on_event("startup")
async def startup() -> None:
    global redis_client, job_runners, job_runner_tasks, control_plane_started_at, comfyui_native_prompt_resume
    control_plane_started_at = datetime.now(tz=UTC)
    settings_for_startup = load_settings()
    globals()["settings"] = settings_for_startup
    database.configure_engine(settings_for_startup.database_url)
    await database.verify_schema_current()
    await load_network_policy_cache()
    await load_maintenance_state_cache()
    await load_resource_policy_override()
    await refresh_runtime_configuration_cache()
    expired_sessions = await database.revoke_expired_browser_sessions()
    open_webui_client = await ensure_open_webui_api_client()
    globals()["model_catalog"] = await refresh_catalog_cache()
    await load_admission_policy_override()
    globals()["runtime_registry"] = build_runtime_registry(
        localai_url=settings_for_startup.localai_url,
        comfyui_url=settings_for_startup.comfyui_url,
        voicebox_url=settings_for_startup.voicebox_url,
        audio_cpu_url=settings_for_startup.audio_cpu_url,
        allow_external=settings_for_startup.allow_external_providers,
        **runtime_registry_external_inputs(settings_for_startup),
    )
    await refresh_node_pin_registry()
    seeded_workflows = await seed_workflows_from_directory(Path(settings_for_startup.workflow_seed_dir))
    redis_client = redis.from_url(settings_for_startup.redis_url, decode_responses=True)
    database.configure_scheduler_redis(redis_client)
    job_runners = []
    job_runner_tasks = []
    comfyui_native_prompt_resume = await resume_comfyui_native_prompt_trackers()
    if settings_for_startup.job_runner_enabled:
        cpu_runner = CpuJobRunner(
            Path(settings_for_startup.artifact_root),
            settings_for_startup.job_runner_interval_seconds,
            audio_cpu_url=settings_for_startup.audio_cpu_url,
            pause_check=queued_runner_pause_active,
            resource_policy_provider=resource_policy,
        )
        job_runners.append(cpu_runner)
        job_runner_tasks.append(asyncio.create_task(cpu_runner.run_forever(), name="b1-cpu-job-runner"))
    if settings_for_startup.gpu_job_runner_enabled:
        gpu_runner = GpuJobRunner(
            Path(settings_for_startup.artifact_root),
            settings_for_startup.gpu_job_runner_interval_seconds,
            lease_ttl_seconds=settings_for_startup.gpu_job_runner_lease_ttl_seconds,
            runtime_agent_url=settings_for_startup.runtime_agent_url,
            runtime_agent_token=settings_for_startup.runtime_agent_token,
            runtime_agent_tls_ca_file=settings_for_startup.runtime_agent_tls_ca_file,
            runtime_agent_tls_client_cert_file=settings_for_startup.runtime_agent_tls_client_cert_file,
            runtime_agent_tls_client_key_file=settings_for_startup.runtime_agent_tls_client_key_file,
            runtime_agent_tls_verify=settings_for_startup.runtime_agent_tls_verify,
            runtime_control_token=settings_for_startup.runtime_control_token,
            reserve_vram_gib=resource_policy().gpu_reserve_vram_gib,
            default_idle_timeout_seconds=settings_for_startup.gpu_default_idle_timeout_seconds,
            runtime_urls={
                "localai": settings_for_startup.localai_url,
                "comfyui": settings_for_startup.comfyui_url,
                "voicebox": settings_for_startup.voicebox_url,
            },
            comfyui_poll_seconds=settings_for_startup.comfyui_prompt_poll_seconds,
            comfyui_completion_timeout_seconds=settings_for_startup.comfyui_prompt_completion_timeout_seconds,
            pause_check=queued_runner_pause_active,
        )
        job_runners.append(gpu_runner)
        job_runner_tasks.append(asyncio.create_task(gpu_runner.run_forever(), name="b1-gpu-job-runner"))
    if settings_for_startup.model_download_runner_enabled:
        model_download_runner = ModelDownloadRunner(
            Path(settings_for_startup.data_root),
            settings_for_startup.model_download_runner_interval_seconds,
            master_key=settings_for_startup.master_key,
            pause_check=queued_runner_pause_active,
        )
        job_runners.append(model_download_runner)
        job_runner_tasks.append(asyncio.create_task(model_download_runner.run_forever(), name="b1-model-download-runner"))
    if settings_for_startup.backup_scheduler_enabled:
        backup_runner = BackupScheduleRunner(settings_for_startup.backup_scheduler_interval_seconds)
        job_runners.append(backup_runner)
        job_runner_tasks.append(asyncio.create_task(backup_runner.run_forever(), name="b1-backup-scheduler"))
    log_event(
        "started",
        workflows_seeded=seeded_workflows["count"],
        expired_browser_sessions_revoked=expired_sessions,
        open_webui_client_ready=bool(open_webui_client),
        resumed_comfyui_native_prompts=comfyui_native_prompt_resume["resumed"],
        skipped_comfyui_native_prompts=comfyui_native_prompt_resume["skipped"],
        backup_scheduler_enabled=settings_for_startup.backup_scheduler_enabled,
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    for runner in job_runners:
        runner.stop()
    for task in job_runner_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if redis_client is not None:
        await redis_client.aclose()
    database.configure_scheduler_redis(None)
    log_event("stopped")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": settings.service_name, "time": now_iso()}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    db_ok = await database.ping()
    redis_ok = False
    if redis_client is not None:
        redis_ok = bool(await redis_client.ping())
    if not db_ok or not redis_ok:
        raise HTTPException(status_code=503, detail={"database": db_ok, "redis": redis_ok})
    return {"status": "ready", "database": db_ok, "redis": redis_ok}


@app.get("/auth/status")
async def auth_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    setup_required = await database.active_user_count(role=Role.ADMIN.value) == 0
    auth = await optional_authenticate(authorization)
    return auth_public_payload(auth, setup_required=setup_required, csrf_token=auth.csrf_token if auth else None)


@app.post("/auth/setup")
async def auth_setup(payload: AuthSetupRequest) -> Response:
    if await database.active_user_count(role=Role.ADMIN.value) > 0:
        raise HTTPException(status_code=409, detail="initial administrator is already configured")
    if settings.admin_bootstrap_key and not hmac.compare_digest(payload.bootstrap_key or "", settings.admin_bootstrap_key):
        raise HTTPException(status_code=403, detail="invalid bootstrap key")
    password_errors = password_policy_errors(payload.password)
    if password_errors:
        raise HTTPException(status_code=422, detail={"message": "password does not meet policy", "errors": password_errors})
    request = current_request.get()
    user = await database.insert_user(
        {
            "id": f"user_{uuid.uuid4().hex}",
            "username": payload.username,
            "display_name": payload.display_name or payload.username,
            "role": Role.ADMIN.value,
            "password_hash": hash_password(payload.password),
        }
    )
    await database.mark_user_login(user["id"])
    session_token, session = await create_browser_session(user, request)
    auth = AuthContext(
        subject_id=user["id"],
        role=Role.ADMIN,
        scopes=scopes_for_role(Role.ADMIN),
        session_id=session["id"],
        csrf_token=session["csrf_token"],
    )
    await record_audit_event(
        auth,
        "auth.initial_admin_created",
        target_type="user",
        target_id=user["id"],
        summary=f"Created initial administrator {user['username']}",
        metadata={"username": user["username"], "role": user["role"]},
        remote_addr=client_host(request),
    )
    response = JSONResponse(auth_public_payload(auth, setup_required=False, csrf_token=session["csrf_token"]))
    set_browser_session_cookie(response, session_token)
    return response


@app.post("/auth/login")
async def auth_login(payload: AuthLoginRequest) -> Response:
    request = current_request.get()
    user = await database.get_user_by_username(payload.username)
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=403, detail="invalid username or password")
    await database.mark_user_login(user["id"])
    session_token, session = await create_browser_session(user, request)
    role = Role(user["role"])
    auth = AuthContext(
        subject_id=user["id"],
        role=role,
        scopes=scopes_for_role(role),
        session_id=session["id"],
        csrf_token=session["csrf_token"],
    )
    await record_audit_event(
        auth,
        "auth.login",
        target_type="user",
        target_id=user["id"],
        summary=f"Browser login for {user['username']}",
        metadata={"username": user["username"], "role": user["role"]},
        remote_addr=client_host(request),
    )
    response = JSONResponse(auth_public_payload(auth, setup_required=False, csrf_token=session["csrf_token"]))
    set_browser_session_cookie(response, session_token)
    return response


@app.post("/auth/logout")
async def auth_logout() -> Response:
    request = current_request.get()
    auth = await optional_authenticate()
    session_token = request.cookies.get(settings.session_cookie_name) if request else None
    if session_token:
        await database.revoke_browser_session_by_hash(hash_session_token(session_token))
    if auth is not None:
        await record_audit_event(
            auth,
            "auth.logout",
            target_type="session",
            target_id=auth.session_id,
            summary="Browser logout",
            remote_addr=client_host(request),
        )
    response = JSONResponse({"status": "ok"})
    clear_browser_session_cookie(response)
    return response


@app.get("/admin/status")
async def admin_status(
    authorization: str | None = Header(default=None),
    _: str = Header(default="", alias="X-Request-Id"),
    owner: str = Header(default="system", alias="X-B1-Owner"),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    policy = resource_policy()
    return {
        "service": settings.service_name,
        "subject_id": auth.subject_id,
        "role": auth.role.value,
        "owner_hint": owner,
        "resource_policy": resource_policy_dict(policy),
        "resource_policy_source": "database" if resource_policy_override is not None else "environment",
        "external_providers_enabled": settings.allow_external_providers,
        "job_runner_enabled": settings.job_runner_enabled,
        "gpu_job_runner_enabled": settings.gpu_job_runner_enabled,
        "gpu_default_idle_timeout_seconds": settings.gpu_default_idle_timeout_seconds,
        "sync_inference_lease_ttl_seconds": settings.sync_inference_lease_ttl_seconds,
        "model_download_runner_enabled": settings.model_download_runner_enabled,
        "maintenance": current_maintenance_state(),
        "admission": await admission_report(auth.subject_id),
        "acceptance_model_measurements": await acceptance_model_measurement_coverage_snapshot(),
        "queue": await database.job_counts_by_state(),
        "scheduler_lease": jsonable_encoder(await database.get_scheduler_owner()),
        "runtime_states": jsonable_encoder(await database.list_runtime_states()),
        "runtimes": {
            "localai": settings.localai_url,
            "comfyui": settings.comfyui_url,
            "voicebox": settings.voicebox_url,
            "audio_cpu": settings.audio_cpu_url,
        },
    }


async def build_admin_metrics_payload(limit: int = 500) -> dict[str, Any]:
    jobs = await database.list_jobs(limit=limit)
    state_counts = await database.job_counts_by_state()
    runtime_states = await database.list_runtime_states()
    scheduler_lease = await database.get_scheduler_owner()
    agent_metrics, agent_error = await runtime_agent_get("/v1/metrics")
    return jsonable_encoder(
        build_observability_report(
            jobs=jobs,
            state_counts=state_counts,
            runtime_states=runtime_states,
            scheduler_lease=scheduler_lease,
            agent_metrics=agent_metrics,
            agent_error=agent_error,
        )
    )


@app.get("/admin/metrics")
async def admin_metrics(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=500, ge=1, le=500),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    return await build_admin_metrics_payload(limit=limit)


@app.get("/admin/metrics.prometheus", response_class=PlainTextResponse)
async def admin_metrics_prometheus(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=500, ge=1, le=500),
) -> Response:
    if not prometheus_scrape_token_allowed(authorization):
        auth = await authenticate(authorization)
        require_scope(auth, "admin:read")
    payload = await build_admin_metrics_payload(limit=limit)
    return Response(
        content=observability_report_to_prometheus(payload),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/admin/admission")
async def admin_admission(
    authorization: str | None = Header(default=None),
    owner_id: str | None = Query(default=None, max_length=128),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    if owner_id and not (auth.has_scope("*") or auth.role in {Role.ADMIN, Role.OPERATOR}):
        raise HTTPException(status_code=403, detail="owner-specific admission inspection requires admin or operator role")
    return jsonable_encoder(await admission_report(owner_id or auth.subject_id))


@app.get("/admin/admission-policy")
async def admin_admission_policy_get(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "admission policy management requires administrator role")
    row = await database.get_admission_policy_record()
    if row is not None and admission_policy_override is None:
        globals()["admission_policy_override"] = admission_policy_from_record(row)
    return public_admission_policy_payload(row)


@app.post("/admin/admission-policy/validate")
async def admin_admission_policy_validate(payload: AdmissionPolicyUpdateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "admission policy management requires administrator role")
    policy = admission_policy_from_update(payload)
    errors = validate_admission_policy_candidate(policy)
    return {
        "status": "accepted" if not errors else "rejected",
        "accepted": not errors,
        "errors": errors,
        "candidate": admission_policy_dict(policy),
        "bounds": admission_policy_hard_bounds(),
    }


@app.put("/admin/admission-policy")
async def admin_admission_policy_update(payload: AdmissionPolicyUpdateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "admission policy management requires administrator role")
    policy = admission_policy_from_update(payload)
    errors = validate_admission_policy_candidate(policy)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "admission policy violates hard safety bounds", "errors": errors})
    row = await database.upsert_admission_policy_record({**admission_policy_dict(policy), "updated_by": auth.subject_id})
    globals()["admission_policy_override"] = admission_policy_from_record(row)
    await record_audit_event(
        auth,
        "admission_policy.updated",
        target_type="admission_policy",
        target_id="default",
        summary="Updated admission policy",
        metadata={
            "source": "database",
            "effective": admission_policy_dict(admission_policy()),
            "bounds": admission_policy_hard_bounds(),
        },
    )
    return public_admission_policy_payload(row)


@app.delete("/admin/admission-policy")
async def admin_admission_policy_reset(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "admission policy management requires administrator role")
    previous = await database.delete_admission_policy_record()
    globals()["admission_policy_override"] = None
    await record_audit_event(
        auth,
        "admission_policy.reset",
        target_type="admission_policy",
        target_id="default",
        summary="Reset admission policy to environment defaults",
        metadata={"previous": admission_policy_dict(admission_policy_from_record(previous)) if previous else None, "source": "environment"},
    )
    return public_admission_policy_payload(None)


@app.get("/admin/network-policy")
async def admin_network_policy_get(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "network policy management requires administrator role")
    row = await load_network_policy_cache()
    return public_network_policy_payload(row)


@app.post("/admin/network-policy/validate")
async def admin_network_policy_validate(payload: NetworkPolicyUpdateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "network policy management requires administrator role")
    normalized = validate_network_policy_payload(payload)
    return {
        "status": "accepted",
        "accepted": True,
        "errors": [],
        "candidate": normalized,
    }


@app.put("/admin/network-policy")
async def admin_network_policy_update(payload: NetworkPolicyUpdateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "network policy management requires administrator role")
    normalized = validate_network_policy_payload(payload)
    row = await database.upsert_network_policy_record({**normalized, "updated_by": auth.subject_id})
    globals()["network_policy_cache"] = row
    await record_audit_event(
        auth,
        "network_policy.updated",
        target_type="network_policy",
        target_id="default",
        summary="Updated network policy",
        metadata={
            "source": "database",
            "cors_allow_origins": normalized["cors_allow_origins"],
            "trusted_proxy_cidrs": normalized["trusted_proxy_cidrs"],
        },
    )
    return public_network_policy_payload(row)


@app.delete("/admin/network-policy")
async def admin_network_policy_reset(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "network policy management requires administrator role")
    previous = await database.delete_network_policy_record()
    globals()["network_policy_cache"] = None
    await record_audit_event(
        auth,
        "network_policy.reset",
        target_type="network_policy",
        target_id="default",
        summary="Reset network policy to environment defaults",
        metadata={"previous": public_network_policy_payload(previous)["effective"] if previous else None, "source": "environment"},
    )
    return public_network_policy_payload(None)


@app.get("/admin/maintenance")
async def admin_maintenance_get(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "maintenance mode management requires administrator role")
    row = await database.get_maintenance_state()
    globals()["maintenance_state_cache"] = row
    return public_maintenance_state(row)


@app.put("/admin/maintenance")
async def admin_maintenance_update(payload: MaintenanceUpdateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "maintenance mode management requires administrator role")
    reason = payload.reason.strip()
    if payload.enabled and not reason:
        raise HTTPException(status_code=422, detail="maintenance reason is required when enabling maintenance mode")
    previous_raw = maintenance_state_cache or {}
    previous = current_maintenance_state()
    previous_enabled = bool(previous_raw.get("enabled", False))
    previous_started_at = previous_raw.get("started_at")
    now = datetime.now(tz=UTC)
    row = await database.upsert_maintenance_state(
        {
            "enabled": payload.enabled,
            "reason": reason,
            "started_at": now if payload.enabled and not previous_enabled else (previous_started_at if payload.enabled else None),
            "ended_at": None if payload.enabled else now,
            "updated_by": auth.subject_id,
        }
    )
    globals()["maintenance_state_cache"] = row
    event_type = "maintenance.enabled" if payload.enabled else "maintenance.disabled"
    await record_audit_event(
        auth,
        event_type,
        target_type="maintenance",
        target_id="default",
        summary="Enabled maintenance mode" if payload.enabled else "Disabled maintenance mode",
        metadata={
            "enabled": payload.enabled,
            "previous_enabled": previous["enabled"],
            "reason_set": bool(reason),
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
        },
    )
    return public_maintenance_state(row)


@app.get("/admin/resource-policy")
async def admin_resource_policy_get(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "resource policy management requires administrator role")
    row = await database.get_resource_policy_record()
    if row is not None and resource_policy_override is None:
        globals()["resource_policy_override"] = resource_policy_from_record(row)
        apply_resource_policy_to_live_runners(resource_policy())
    return public_resource_policy_payload(row)


@app.post("/admin/resource-policy/validate")
async def admin_resource_policy_validate(payload: ResourcePolicyUpdateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "resource policy management requires administrator role")
    policy = resource_policy_from_update(payload)
    errors = validate_resource_policy_candidate(policy)
    return {
        "status": "accepted" if not errors else "rejected",
        "accepted": not errors,
        "errors": errors,
        "candidate": resource_policy_dict(policy),
        "bounds": resource_policy_hard_bounds(),
    }


@app.put("/admin/resource-policy")
async def admin_resource_policy_update(payload: ResourcePolicyUpdateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "resource policy management requires administrator role")
    policy = resource_policy_from_update(payload)
    errors = validate_resource_policy_candidate(policy)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "resource policy violates hard safety bounds", "errors": errors})
    row = await database.upsert_resource_policy_record({**resource_policy_dict(policy), "updated_by": auth.subject_id})
    globals()["resource_policy_override"] = resource_policy_from_record(row)
    apply_resource_policy_to_live_runners(resource_policy())
    await refresh_catalog_cache()
    await record_audit_event(
        auth,
        "resource_policy.updated",
        target_type="resource_policy",
        target_id="default",
        summary="Updated resource policy",
        metadata={
            "source": "database",
            "effective": resource_policy_dict(resource_policy()),
            "bounds": resource_policy_hard_bounds(),
        },
    )
    return public_resource_policy_payload(row)


@app.delete("/admin/resource-policy")
async def admin_resource_policy_reset(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "resource policy management requires administrator role")
    previous = await database.delete_resource_policy_record()
    globals()["resource_policy_override"] = None
    apply_resource_policy_to_live_runners(resource_policy())
    await refresh_catalog_cache()
    await record_audit_event(
        auth,
        "resource_policy.reset",
        target_type="resource_policy",
        target_id="default",
        summary="Reset resource policy to environment defaults",
        metadata={"previous": resource_policy_dict(resource_policy_from_record(previous)) if previous else None, "source": "environment"},
    )
    return public_resource_policy_payload(None)


def public_api_client(row: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(row)
    redacted.pop("key_hash", None)
    redacted.pop("key_salt", None)
    redacted.setdefault("cidr_allowlist", [])
    redacted.setdefault("default_b1_tools", [])
    return jsonable_encoder(redacted)


def require_administrator(auth: AuthContext, detail: str = "administrator role required") -> None:
    if auth.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail=detail)


def require_secret_admin(auth: AuthContext) -> None:
    require_administrator(auth, "encrypted secret management requires administrator role")


def require_credential_admin(auth: AuthContext) -> None:
    require_administrator(auth, "credential management requires administrator role")


def secret_master_key_status() -> dict[str, Any]:
    raw = settings.master_key.strip()
    status = {
        "configured": bool(raw),
        "usable": False,
        "scheme": secret_store.ENVELOPE_SCHEME,
        "key_id": None,
    }
    if not raw:
        return status
    try:
        status["key_id"] = secret_store.master_key_fingerprint(raw)
        status["usable"] = True
    except secret_store.SecretStoreError as exc:
        status["error"] = str(exc)
    return status


def require_master_encryption_key() -> str:
    raw = settings.master_key.strip()
    if not raw:
        raise HTTPException(status_code=503, detail="master encryption key is not configured")
    try:
        secret_store.master_key_fingerprint(raw)
    except secret_store.SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return raw


def public_encrypted_secret(row: dict[str, Any]) -> dict[str, Any]:
    return jsonable_encoder(secret_store.public_secret_record(row))


def normalize_external_runtime_name(runtime: str) -> str:
    normalized = runtime.strip().lower()
    if normalized not in EXTERNAL_RUNTIME_NAMES:
        raise HTTPException(status_code=404, detail="external runtime adapter is not configured")
    return normalized


def normalize_service_log_name(service: str) -> str:
    normalized = service.strip().lower()
    if normalized not in SERVICE_LOG_NAMES:
        raise HTTPException(status_code=404, detail="service logs are not available for this service")
    return normalized


def redact_service_log_line(line: Any) -> str:
    text = redact_service_log_json_fragment(str(line))
    for pattern, replacement in SERVICE_LOG_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    encoded = text.encode("utf-8")
    if len(encoded) <= SERVICE_LOG_LINE_MAX_BYTES:
        return text
    return encoded[:SERVICE_LOG_LINE_MAX_BYTES].decode("utf-8", errors="ignore") + "...<truncated>"


def redact_service_log_json_fragment(text: str) -> str:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "{[":
            continue
        try:
            payload, end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        redacted = audit_policy.redact_log_value(payload)
        encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
        return text[:index] + encoded + text[index + end :]
    return text


def external_runtime_env_base_url(runtime: str) -> str:
    if runtime == "openai-compatible":
        return settings.openai_compatible_base_url
    if runtime == "generic-http":
        return settings.generic_http_base_url
    return ""


def external_runtime_env_api_key_configured(runtime: str) -> bool:
    return runtime == "openai-compatible" and bool(settings.openai_compatible_api_key.strip())


def public_external_runtime_configuration(runtime: str, row: dict[str, Any] | None, cache: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    source = "database" if row is not None else "environment"
    enabled = bool(row["enabled"]) if row is not None else bool(external_runtime_env_base_url(runtime).strip())
    base_url = str(row["base_url"] if row is not None else external_runtime_env_base_url(runtime)).strip()
    api_key_secret_name = row.get("api_key_secret_name") if row is not None else None
    api_key_configured = bool(api_key_secret_name) if row is not None else external_runtime_env_api_key_configured(runtime)
    acknowledged = bool(row.get("external_data_acknowledged")) if row is not None else False
    notes = str(row.get("notes") or "") if row is not None else ""
    normalized_url, validation_error = validate_external_runtime_base_url(base_url) if enabled else ("", None)
    cached_error = None
    if cache is not None and runtime in cache:
        cached_error = cache[runtime].get("configuration_error")
    configuration_error = cached_error or validation_error
    configured = enabled and configuration_error is None
    eligible = configured and settings.allow_external_providers
    status = "disabled"
    if enabled and not configured:
        status = "invalid"
    elif enabled and not settings.allow_external_providers:
        status = "configured-global-disabled"
    elif eligible:
        status = "eligible"
    return jsonable_encoder(
        {
            "runtime": runtime,
            "source": source,
            "enabled": enabled,
            "configured": configured,
            "eligible": eligible,
            "status": status,
            "base_url": normalized_url if configured else base_url,
            "api_key_secret_name": api_key_secret_name,
            "api_key_configured": api_key_configured,
            "external_data_acknowledged": acknowledged,
            "configuration_error": configuration_error,
            "notes": notes,
            "updated_by": row.get("updated_by") if row is not None else None,
            "created_at": row.get("created_at") if row is not None else None,
            "updated_at": row.get("updated_at") if row is not None else None,
            "warning": "enabled external runtimes can receive prompts, media, voices, documents, and metadata outside the LAN",
        }
    )


def normalize_secret_name_or_422(name: str) -> str:
    try:
        return secret_store.validate_secret_name(name)
    except secret_store.SecretStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def validate_model_download_secret_name(secret_name: str | None) -> str | None:
    normalized = (secret_name or "").strip()
    if not normalized:
        return None
    try:
        normalized = secret_store.validate_secret_name(normalized)
    except secret_store.SecretStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    secret_row = await database.get_encrypted_secret(normalized)
    if secret_row is None:
        raise HTTPException(status_code=422, detail="credential_secret_name does not reference an active encrypted secret")
    if secret_row.get("category") != "model-download":
        raise HTTPException(status_code=422, detail="credential_secret_name must reference an encrypted secret in category model-download")
    return normalized


def normalize_model_tool_auth_config(config: dict[str, Any]) -> None:
    auth = config.get("auth")
    if auth in (None, "", False):
        config.pop("auth", None)
        return
    if not isinstance(auth, dict):
        raise HTTPException(status_code=422, detail="model-tool auth must be an object")
    auth_type = str(auth.get("type") or "none").strip().lower()
    if auth_type == "none":
        config.pop("auth", None)
        return
    if auth_type not in {"bearer", "header"}:
        raise HTTPException(status_code=422, detail="model-tool auth type must be none, bearer, or header")
    secret_name = normalize_secret_name_or_422(str(auth.get("secret_name") or ""))
    if auth_type == "bearer":
        config["auth"] = {"type": "bearer", "secret_name": secret_name}
        return
    header_name = str(auth.get("header_name") or "").strip()
    header_key = header_name.lower()
    if not MODEL_TOOL_HEADER_NAME_RE.fullmatch(header_name):
        raise HTTPException(status_code=422, detail="model-tool auth header_name must start with a letter and contain only letters, digits, or hyphens")
    if header_key in MODEL_TOOL_FORBIDDEN_SECRET_HEADER_NAMES or header_key.startswith("sec-"):
        raise HTTPException(status_code=422, detail="model-tool auth header_name is not allowed")
    config["auth"] = {"type": "header", "secret_name": secret_name, "header_name": header_name}


async def validate_model_tool_secret_references(config: dict[str, Any]) -> None:
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return
    secret_name = str(auth.get("secret_name") or "").strip()
    if not secret_name:
        return
    secret_row = await database.get_encrypted_secret(secret_name)
    if secret_row is None:
        raise HTTPException(status_code=422, detail="model-tool auth secret_name does not reference an active encrypted secret")
    if secret_row.get("category") != "integration":
        raise HTTPException(status_code=422, detail="model-tool auth secret_name must reference an encrypted secret in category integration")


async def model_tool_secret_headers(definition: model_tools.ModelToolDefinition) -> dict[str, str]:
    auth = definition.config.get("auth")
    if not isinstance(auth, dict):
        return {}
    auth_type = str(auth.get("type") or "none").strip().lower()
    if auth_type == "none":
        return {}
    try:
        secret_name = secret_store.validate_secret_name(str(auth.get("secret_name") or ""))
    except secret_store.SecretStoreError as exc:
        raise model_tools.ModelToolError("model-tool auth secret name is invalid") from exc
    secret_row = await database.get_encrypted_secret(secret_name)
    if secret_row is None or secret_row.get("category") != "integration":
        raise model_tools.ModelToolError("model-tool auth secret is not available")
    try:
        secret_value = secret_store.decrypt_value(require_master_encryption_key(), secret_name, secret_row.get("secret_envelope") or {}).strip()
    except (HTTPException, secret_store.SecretStoreError) as exc:
        raise model_tools.ModelToolError("model-tool auth secret could not be decrypted") from exc
    if not secret_value:
        raise model_tools.ModelToolError("model-tool auth secret is empty")
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {secret_value}"}
    if auth_type == "header":
        header_name = str(auth.get("header_name") or "").strip()
        header_key = header_name.lower()
        if not MODEL_TOOL_HEADER_NAME_RE.fullmatch(header_name) or header_key in MODEL_TOOL_FORBIDDEN_SECRET_HEADER_NAMES or header_key.startswith("sec-"):
            raise model_tools.ModelToolError("model-tool auth header is not allowed")
        return {header_name: secret_value}
    raise model_tools.ModelToolError("model-tool auth type is unsupported")


async def ensure_open_webui_api_client() -> dict[str, Any] | None:
    api_key = settings.open_webui_api_key.strip()
    if not api_key:
        log_event("open_webui_api_key_missing")
        return None
    key_prefix = key_prefix_from_token(api_key)
    if not key_prefix:
        log_event("open_webui_api_key_invalid")
        return None
    try:
        default_b1_tools = normalize_model_tool_names(list(settings.open_webui_default_b1_tools))
    except HTTPException as exc:
        log_event("open_webui_default_b1_tools_invalid", detail=str(exc.detail))
        default_b1_tools = []
    salt, key_hash = hash_api_key(api_key)
    row = await database.upsert_api_client(
        {
            "id": OPEN_WEBUI_CLIENT_ID,
            "display_name": "Open WebUI internal",
            "role": Role.SERVICE.value,
            "scopes": list(scopes_for_role(Role.SERVICE, OPEN_WEBUI_SCOPES)),
            "key_prefix": key_prefix,
            "key_salt": salt,
            "key_hash": key_hash,
            "default_b1_tools": default_b1_tools,
        }
    )
    log_event("open_webui_api_client_ready", client_id=row["id"], key_prefix=key_prefix, scopes=row["scopes"], default_b1_tools=row.get("default_b1_tools") or [])
    return row


def public_runtime_reservation(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    public.pop("idempotency_key", None)
    reason = public.pop("reason", "")
    public["reason_provided"] = bool(str(reason).strip())
    if public.get("status") == "active" and public["expires_at"] <= datetime.now(tz=UTC):
        public["status"] = "expired"
    return jsonable_encoder(public)


def public_modelhub_client(row: dict[str, Any]) -> dict[str, Any]:
    return jsonable_encoder(row)


VOICE_PROFILE_FORBIDDEN_METADATA_KEYS = {
    "audio",
    "audio_data",
    "base64",
    "blob",
    "bytes",
    "file",
    "file_data",
    "path",
    "prompt",
    "sample",
    "sample_data",
    "secret",
    "token",
    "upload",
    "voice",
    "voice_sample",
}
VOICE_PROFILE_MAX_METADATA_BYTES = 16 * 1024


def public_voice_profile(row: dict[str, Any]) -> dict[str, Any]:
    return jsonable_encoder(row)


def validate_voice_profile_roles(roles: list[Role]) -> list[str]:
    normalized: list[str] = []
    for role in roles:
        value = role.value if isinstance(role, Role) else str(role)
        if value not in {item.value for item in Role}:
            raise HTTPException(status_code=422, detail=f"unknown visibility role: {value}")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise HTTPException(status_code=422, detail="visibility_roles must contain at least one role")
    return normalized


def validate_voice_profile_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    except TypeError as exc:
        raise HTTPException(status_code=422, detail="metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > VOICE_PROFILE_MAX_METADATA_BYTES:
        raise HTTPException(status_code=422, detail="metadata exceeds the voice profile limit")

    def scan(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                normalized_key = key_text.lower().replace("-", "_")
                if normalized_key in voice_profile_policy.VOICE_PROFILE_SAFE_METADATA_KEYS:
                    if not voice_profile_policy.safe_runtime_metadata_value(child):
                        raise HTTPException(status_code=422, detail=f"metadata field {path}.{key_text} is not a safe upstream Voicebox selector")
                elif any(part in normalized_key for part in VOICE_PROFILE_FORBIDDEN_METADATA_KEYS):
                    raise HTTPException(status_code=422, detail=f"metadata field {path}.{key_text} may not contain sensitive voice/audio payload data")
                scan(child, f"{path}.{key_text}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                scan(child, f"{path}[{index}]")
        elif isinstance(value, str):
            lowered = value.lower().strip()
            if lowered.startswith("data:audio/") or lowered.startswith("data:application/octet-stream") or lowered.startswith("file:"):
                raise HTTPException(status_code=422, detail=f"metadata field {path} contains inline or local audio data")

    scan(metadata, "metadata")
    return metadata


def validate_voice_profile_artifact_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise HTTPException(status_code=422, detail="sample artifact URLs must be internal /artifacts paths")
    if not parsed.path.startswith("/artifacts/"):
        raise HTTPException(status_code=422, detail="sample artifact URLs must start with /artifacts/")
    relative = parsed.path.removeprefix("/artifacts/")
    try:
        normalized = artifact_policy.artifact_url_for_path(relative)
    except artifact_policy.ArtifactAccessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if normalized != parsed.path:
        raise HTTPException(status_code=422, detail="sample artifact URL is not normalized")
    return normalized


def validate_voice_profile_artifacts(artifacts: list[VoiceProfileSampleArtifact]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for artifact in artifacts:
        item = artifact.model_dump(exclude_none=True)
        item["url"] = validate_voice_profile_artifact_url(item["url"])
        item["sha256"] = item["sha256"].lower()
        normalized.append(item)
    return normalized


def write_voice_profile_sample_artifact(
    *,
    owner_id: str,
    content: bytes,
    declared_mime_type: str | None,
    filename: str | None,
) -> dict[str, Any]:
    if not content:
        raise ValueError("media input is empty")
    mime_type = media_artifacts.require_allowed_input_mime_type(content, declared_mime_type)
    if not mime_type.startswith("audio/"):
        raise ValueError(f"unsupported media input type: {mime_type}")
    owner_segment = media_artifacts.safe_artifact_segment(owner_id, "owner")
    sample_id = f"sample_{uuid.uuid4().hex}"
    extension = media_artifacts.extension_for_mime_type(mime_type, ".audio")
    stored_name = media_artifacts.safe_filename(filename, f"voice-sample{extension}")
    if not stored_name.endswith(extension):
        stored_name = f"{stored_name}{extension}"
    relative = f"voicebox/references/{owner_segment}/{sample_id}/{stored_name}"
    target = media_artifacts.artifact_store_path(Path(settings.artifact_root), relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = media_artifacts.artifact_store_path(Path(settings.artifact_root), relative)
    digest = hashlib.sha256(content).hexdigest()
    media_artifacts.write_regular_file_bytes(target, content)
    return {
        "url": f"/artifacts/{relative}",
        "sha256": digest,
        "mime_type": mime_type,
        "bytes": len(content),
        "filename": stored_name,
        "sample_id": sample_id,
    }


def validate_voice_profile_alias(model_alias: str, runtime: str) -> CatalogAlias:
    try:
        alias = catalog_snapshot().require_alias(model_alias)
    except CatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if alias.alias.modality != "tts":
        raise HTTPException(status_code=422, detail=f"alias {model_alias} is {alias.alias.modality}, not tts")
    if runtime not in alias.runtimes:
        raise HTTPException(status_code=422, detail=f"alias {model_alias} is not compatible with runtime {runtime}")
    return alias


def voice_profile_insert_payload(payload: VoiceProfileCreate, auth: AuthContext) -> dict[str, Any]:
    validate_voice_profile_alias(payload.model_alias, payload.runtime)
    return {
        "id": f"vp_{uuid.uuid4().hex}",
        "display_name": payload.display_name,
        "owner_id": auth.subject_id,
        "runtime": payload.runtime,
        "engine": payload.engine,
        "model_alias": payload.model_alias,
        "profile_type": payload.profile_type,
        "status": payload.status,
        "visibility_roles": validate_voice_profile_roles(payload.visibility_roles),
        "metadata": validate_voice_profile_metadata(payload.metadata),
        "sample_artifacts": validate_voice_profile_artifacts(payload.sample_artifacts),
    }


def voice_profile_update_payload(payload: VoiceProfileUpdate, current: dict[str, Any]) -> dict[str, Any]:
    fields = payload.model_fields_set
    changes: dict[str, Any] = {}
    for field in ("display_name", "runtime", "engine", "model_alias", "profile_type", "status"):
        if field in fields:
            changes[field] = getattr(payload, field)
    runtime = changes.get("runtime", current["runtime"])
    model_alias = changes.get("model_alias", current["model_alias"])
    validate_voice_profile_alias(model_alias, runtime)
    if "visibility_roles" in fields:
        roles = payload.visibility_roles
        if roles is None:
            raise HTTPException(status_code=422, detail="visibility_roles cannot be null")
        changes["visibility_roles"] = validate_voice_profile_roles(roles)
    if "metadata" in fields:
        if payload.metadata is None:
            raise HTTPException(status_code=422, detail="metadata cannot be null")
        changes["metadata"] = validate_voice_profile_metadata(payload.metadata)
    if "sample_artifacts" in fields:
        if payload.sample_artifacts is None:
            raise HTTPException(status_code=422, detail="sample_artifacts cannot be null")
        changes["sample_artifacts"] = validate_voice_profile_artifacts(payload.sample_artifacts)
    return changes


def voice_profile_audit_metadata(row: dict[str, Any], *, include_status: bool = True) -> dict[str, Any]:
    metadata = {
        "display_name": row["display_name"],
        "runtime": row["runtime"],
        "engine": row["engine"],
        "model_alias": row["model_alias"],
        "profile_type": row["profile_type"],
        "visibility_roles": row.get("visibility_roles") or [],
        "sample_artifact_count": len(row.get("sample_artifacts") or []),
    }
    if include_status:
        metadata["status"] = row.get("status")
    return metadata


def voice_profile_export_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "b1-ai-hub-voice-profile/v1",
        "contains_sensitive_data": bool(row.get("sample_artifacts")) or row.get("profile_type") in {"reference", "clone"},
        "profile": public_voice_profile(row),
        "note": "sample_artifacts are internal references; raw voice sample bytes are exported only by the backup/artifact workflow.",
    }


def voice_profile_id_from_payload(payload: dict[str, Any]) -> str | None:
    try:
        return voice_profile_policy.requested_voice_profile_id(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def voice_profile_for_inference(payload: dict[str, Any], auth: AuthContext) -> dict[str, Any] | None:
    profile_id = voice_profile_id_from_payload(payload)
    if not profile_id:
        return None
    row = await database.get_voice_profile(profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    if row.get("status") != "active":
        raise HTTPException(status_code=409, detail="voice profile is not active")
    if not voice_profile_policy.subject_can_use_profile(row, subject_id=auth.subject_id, role=auth.role.value, scopes=auth.scopes):
        raise HTTPException(status_code=403, detail="voice profile is not visible to this subject")
    return row


def enforce_voice_profile_matches_resolution(profile: dict[str, Any] | None, resolution: RuntimeResolution) -> None:
    if profile is None:
        return
    if profile.get("runtime") != resolution.runtime:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "voice profile runtime does not match the resolved runtime",
                "profile_id": profile.get("id"),
                "profile_runtime": profile.get("runtime"),
                "resolved_runtime": resolution.runtime,
            },
        )
    if profile.get("model_alias") != resolution.public_alias:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "voice profile model alias does not match the requested model",
                "profile_id": profile.get("id"),
                "profile_model_alias": profile.get("model_alias"),
                "requested_model": resolution.public_alias,
            },
        )
    if resolution.runtime not in {"voicebox", "audio-cpu"}:
        raise HTTPException(status_code=422, detail=f"runtime {resolution.runtime} does not support B1 voice profile routing")


def apply_voice_profile_to_runtime_payload(payload: dict[str, Any], profile: dict[str, Any] | None) -> dict[str, Any]:
    if profile is None:
        return {key: value for key, value in payload.items() if key not in voice_profile_policy.VOICE_PROFILE_REQUEST_FIELDS}
    return voice_profile_policy.apply_voice_profile_to_payload(payload, profile)


def public_model_record(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    try:
        manifest = model_lifecycle.parse_uploaded_manifest(public["manifest"])
        public["runtime_views"] = model_lifecycle.runtime_view_plan(manifest, data_root_path())
        public["runtime_smoke_summary"] = runtime_smoke_summary_for_manifest(manifest)
    except (CatalogError, ValueError, model_lifecycle.ModelLifecycleError) as exc:
        public["runtime_views_error"] = str(exc)
    return jsonable_encoder(public)


def compact_model_smoke_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: run.get(key)
        for key in (
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
        )
        if key in run
    }


def latest_ok_model_smoke_run(manifest_payload: dict[str, Any], resolved_model_version: str, alias: str) -> tuple[dict[str, Any] | None, int]:
    measurements = manifest_payload.get("measurements") if isinstance(manifest_payload.get("measurements"), dict) else {}
    runs = measurements.get("runs") if isinstance(measurements.get("runs"), list) else []
    manifest_aliases = manifest_payload.get("aliases") if isinstance(manifest_payload.get("aliases"), list) else []
    exact_runs = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("status") == "ok"
        and run.get("resolved_model_version") == resolved_model_version
        and run.get("model_alias") == alias
    ]
    if exact_runs:
        return compact_model_smoke_run(exact_runs[-1]), len(exact_runs)
    ok_runs = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("status") == "ok"
        and run.get("resolved_model_version") == resolved_model_version
        and alias in manifest_aliases
    ]
    return (compact_model_smoke_run(ok_runs[-1]) if ok_runs else None, len(ok_runs))


def model_measurement_coverage_for_alias(
    alias_id: str,
    aliases_by_id: dict[str, dict[str, Any]],
    records_by_ref: dict[str, dict[str, Any]],
    expected_runtime: str | None = None,
) -> dict[str, Any]:
    alias_record = aliases_by_id.get(alias_id)
    if not isinstance(alias_record, dict):
        return {
            "alias": alias_id,
            "ready": False,
            "status": "missing_alias",
            "expected_runtime": expected_runtime,
            "blockers": [f"alias {alias_id} is not present"],
        }
    blockers: list[str] = []
    if alias_record.get("status") != "installed":
        blockers.append("alias is not backed by an installed model manifest")
    resolved = alias_record.get("resolved_model") if isinstance(alias_record.get("resolved_model"), dict) else {}
    model_id = str(resolved.get("id") or "")
    model_version = str(resolved.get("version") or "")
    resolved_model_version = f"{model_id}@{model_version}" if model_id and model_version else ""
    if not resolved_model_version:
        blockers.append("alias lacks resolved immutable model evidence")
    model_record = records_by_ref.get(resolved_model_version)
    if resolved_model_version and model_record is None:
        blockers.append("resolved model is absent from installed model records")
    manifest = model_record.get("manifest") if isinstance(model_record, dict) and isinstance(model_record.get("manifest"), dict) else {}
    measurements = manifest.get("measurements") if isinstance(manifest.get("measurements"), dict) else {}
    latest_ok_run, ok_run_count = latest_ok_model_smoke_run(manifest, resolved_model_version, alias_id)
    runtime = str((latest_ok_run or {}).get("runtime") or alias_record.get("preferred_runtime") or (model_record or {}).get("preferred_runtime") or "")
    if latest_ok_run is None:
        blockers.append("no persisted ok model smoke measurement exists")
    if expected_runtime and runtime != expected_runtime:
        blockers.append(f"measurement runtime {runtime or 'unknown'} does not match expected runtime {expected_runtime}")
    entry = {
        "alias": alias_id,
        "status": alias_record.get("status"),
        "modality": alias_record.get("modality"),
        "expected_runtime": expected_runtime,
        "preferred_runtime": alias_record.get("preferred_runtime"),
        "runtime": runtime,
        "runtimes": alias_record.get("runtimes") or [],
        "resource_label": alias_record.get("resource_label") or (model_record or {}).get("resource_label"),
        "resolved_model_version": resolved_model_version,
        "model_id": model_id or None,
        "model_version": model_version or None,
        "display_name": resolved.get("display_name") or (model_record or {}).get("display_name"),
        "measurement_available": latest_ok_run is not None,
        "ok_run_count": ok_run_count,
        "measurements_updated_at": measurements.get("updated_at") if isinstance(measurements, dict) else "",
        "latest_resource_estimate": measurements.get("latest_resource_estimate") if isinstance(measurements, dict) else {},
        "latest_ok_run": latest_ok_run or {},
        "blockers": blockers,
    }
    for blocker in acceptance.model_measurement_blockers(alias_id, entry, expected_runtime=expected_runtime or ""):
        if blocker not in blockers:
            blockers.append(blocker)
    entry["ready"] = not blockers
    entry["blockers"] = blockers
    return entry


def model_measurement_next_action(entry: dict[str, Any]) -> str:
    alias = str(entry.get("alias") or "the alias")
    expected_runtime = str(entry.get("expected_runtime") or "").strip()
    resolved = str(entry.get("resolved_model_version") or "").strip()
    blockers = [str(item) for item in entry.get("blockers") or []]
    blocker_text = " | ".join(blockers)
    if "is not present" in blocker_text:
        return f"Restore the seeded catalog/profile entry for {alias}, then choose a compatible model manifest."
    if "alias is not backed by an installed model manifest" in blocker_text:
        return f"Install a compatible model manifest for {alias} from the catalog, upload, or Model Hub, then rerun model smoke."
    if "lacks resolved immutable model evidence" in blocker_text or "resolved model is absent" in blocker_text:
        return f"Repair or reinstall {alias} so it resolves to an installed immutable model record."
    if "measurement runtime" in blocker_text:
        runtime_label = expected_runtime or "the expected runtime"
        return f"Select or install a {runtime_label}-compatible model for {alias}, then persist a new ok model-smoke run."
    if "no persisted ok model smoke measurement exists" in blocker_text:
        target = resolved or alias
        return f"Run the Control Center model smoke action for {target} and persist an ok measurement before live acceptance."
    return f"Resolve the listed blockers for {alias}, then rerun model smoke."


def model_measurement_handoff_plan(groups: list[dict[str, Any]], required_aliases: set[str], missing_aliases: set[str]) -> dict[str, Any]:
    blocker_aliases: dict[str, set[str]] = {}
    next_actions: list[dict[str, Any]] = []
    for group in groups:
        group_id = str(group.get("id") or "")
        group_label = str(group.get("label") or group_id)
        for entry in group.get("measurements") or []:
            if not isinstance(entry, dict) or entry.get("ready") is True:
                continue
            alias = str(entry.get("alias") or "")
            blockers = [str(item) for item in entry.get("blockers") or [] if str(item).strip()]
            for blocker in blockers:
                blocker_aliases.setdefault(blocker, set()).add(alias)
            next_actions.append(
                {
                    "suite": group_id,
                    "suite_label": group_label,
                    "alias": alias,
                    "expected_runtime": entry.get("expected_runtime"),
                    "status": entry.get("status"),
                    "resolved_model_version": entry.get("resolved_model_version") or "",
                    "blockers": blockers,
                    "action": model_measurement_next_action(entry),
                }
            )
    return {
        "ready": not missing_aliases,
        "ready_aliases": sorted(required_aliases - missing_aliases),
        "blocked_aliases": sorted(missing_aliases),
        "ready_count": len(required_aliases - missing_aliases),
        "blocked_count": len(missing_aliases),
        "next_actions": next_actions,
        "blocker_summary": [
            {"blocker": blocker, "aliases": sorted(aliases), "count": len(aliases)}
            for blocker, aliases in sorted(blocker_aliases.items(), key=lambda item: (len(item[1]), item[0]), reverse=True)
        ],
    }


def acceptance_model_measurement_groups(required_runtimes: tuple[str, ...] | list[str] | set[str] | None = None) -> tuple[dict[str, Any], ...]:
    runtime_set = {
        str(runtime).strip().lower()
        for runtime in (required_runtimes if required_runtimes is not None else settings.runtime_production_required)
        if str(runtime).strip()
    }
    groups: list[dict[str, Any]] = []
    for group in ACCEPTANCE_MODEL_MEASUREMENT_GROUP_SPECS:
        aliases: list[tuple[str, str]] = []
        for alias_id, expected_runtime, runtime_gate in group["aliases"]:
            if runtime_gate not in runtime_set:
                continue
            aliases.append((alias_id, expected_runtime))
        if aliases:
            groups.append(
                {
                    "id": group["id"],
                    "label": group["label"],
                    "aliases": tuple(aliases),
                }
            )
    return tuple(groups)


def acceptance_model_measurement_coverage(
    aliases: list[dict[str, Any]],
    records: list[dict[str, Any]],
    required_runtimes: tuple[str, ...] | list[str] | set[str] | None = None,
) -> dict[str, Any]:
    aliases_by_id = {str(item.get("id")): item for item in aliases if isinstance(item, dict) and item.get("id")}
    records_by_ref = {
        f"{item.get('id')}@{item.get('version')}": item
        for item in records
        if isinstance(item, dict) and item.get("id") and item.get("version")
    }
    effective_required_runtimes = tuple(
        sorted(
            {
                str(runtime).strip().lower()
                for runtime in (required_runtimes if required_runtimes is not None else settings.runtime_production_required)
                if str(runtime).strip()
            }
        )
    )
    measurement_groups = acceptance_model_measurement_groups(effective_required_runtimes)
    groups: list[dict[str, Any]] = []
    all_required_aliases: set[str] = set()
    all_missing_aliases: set[str] = set()
    for group in measurement_groups:
        entries: list[dict[str, Any]] = []
        for alias_id, expected_runtime in group["aliases"]:
            all_required_aliases.add(alias_id)
            entry = model_measurement_coverage_for_alias(alias_id, aliases_by_id, records_by_ref, expected_runtime)
            if not entry["ready"]:
                all_missing_aliases.add(alias_id)
            entries.append(entry)
        missing = [entry["alias"] for entry in entries if not entry["ready"]]
        groups.append(
            {
                "id": group["id"],
                "label": group["label"],
                "status": "ok" if not missing else "incomplete",
                "required_aliases": [alias for alias, _runtime in group["aliases"]],
                "missing_aliases": missing,
                "measurements": entries,
            }
        )
    handoff_plan = model_measurement_handoff_plan(groups, all_required_aliases, all_missing_aliases)
    return {
        "status": "ok" if not all_missing_aliases else "incomplete",
        "required_runtimes": list(effective_required_runtimes),
        "required_aliases": sorted(all_required_aliases),
        "missing_aliases": sorted(all_missing_aliases),
        "handoff_plan": handoff_plan,
        "next_actions": handoff_plan["next_actions"],
        "blocker_summary": handoff_plan["blocker_summary"],
        "ready_aliases": handoff_plan["ready_aliases"],
        "ready_count": handoff_plan["ready_count"],
        "blocked_count": handoff_plan["blocked_count"],
        "groups": groups,
    }


async def acceptance_model_measurement_coverage_snapshot() -> dict[str, Any]:
    try:
        catalog_payload = catalog_snapshot().to_catalog()
    except CatalogError as exc:
        return {
            "status": "unavailable",
            "required_aliases": [],
            "missing_aliases": [],
            "handoff_plan": {
                "ready": False,
                "ready_aliases": [],
                "blocked_aliases": [],
                "ready_count": 0,
                "blocked_count": 0,
                "next_actions": [],
                "blocker_summary": [],
            },
            "next_actions": [],
            "blocker_summary": [],
            "ready_aliases": [],
            "ready_count": 0,
            "blocked_count": 0,
            "groups": [],
            "reason": str(exc),
        }
    records = [public_model_record(row) for row in await database.list_model_records()]
    return acceptance_model_measurement_coverage(catalog_payload["aliases"], records)


def public_model_alias_policy(row: dict[str, Any]) -> dict[str, Any]:
    return jsonable_encoder(
        {
            "alias": row["alias"],
            "modality": row.get("modality"),
            "enabled": bool(row.get("enabled", True)),
            "preferred_runtime": row.get("preferred_runtime"),
            "status": row.get("status"),
            "idle_timeout_seconds": row.get("idle_timeout_seconds"),
            "visibility_roles": row.get("visibility_roles") or [],
            "notes": row.get("notes") or "",
            "updated_by": row.get("updated_by"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
    )


def validate_model_alias_policy_payload(alias_id: str, payload: ModelAliasPolicyRequest) -> dict[str, Any]:
    alias_id = alias_id.strip()
    if not ID_PATTERN.fullmatch(alias_id):
        raise HTTPException(status_code=422, detail="alias must start with a lowercase letter or digit and contain only lowercase letters, digits, dots, underscores, and hyphens")
    alias = catalog_snapshot().get_alias(alias_id)
    requested_modality = (payload.modality or "").strip() or None
    if requested_modality is not None and requested_modality not in MODALITIES:
        raise HTTPException(status_code=422, detail=f"unsupported alias modality: {requested_modality}")
    preferred_runtime = (payload.preferred_runtime or "").strip() or None
    if preferred_runtime is not None and preferred_runtime not in RUNTIME_NAMES:
        raise HTTPException(status_code=422, detail=f"unsupported preferred runtime: {preferred_runtime}")
    requested_status = (payload.status or "").strip() or None
    if requested_status is not None and not ID_PATTERN.fullmatch(requested_status):
        raise HTTPException(status_code=422, detail="alias status must use lowercase identifier characters")
    if alias is None:
        if requested_modality is None:
            raise HTTPException(status_code=422, detail="new aliases require a modality")
        if preferred_runtime is None:
            raise HTTPException(status_code=422, detail="new aliases require a preferred runtime")
        modality = requested_modality
        status = requested_status or "uninstalled"
    else:
        if requested_modality is not None and requested_modality != alias.alias.modality:
            raise HTTPException(status_code=422, detail=f"alias {alias_id} is {alias.alias.modality}, not {requested_modality}")
        modality = requested_modality or alias.alias.modality
        status = requested_status or alias.alias.status
        if preferred_runtime is None and alias.alias.policy_source == "database-custom":
            preferred_runtime = alias.alias.preferred_runtime
    if preferred_runtime is not None and alias is not None and alias.manifest is not None and preferred_runtime not in alias.runtimes:
        raise HTTPException(
            status_code=422,
            detail=f"runtime {preferred_runtime} is not listed by installed model {alias.manifest.id}@{alias.manifest.version}",
        )
    return {
        "alias": alias_id,
        "modality": modality,
        "enabled": payload.enabled,
        "preferred_runtime": preferred_runtime,
        "status": status,
        "idle_timeout_seconds": payload.idle_timeout_seconds,
        "visibility_roles": [role.value for role in payload.visibility_roles],
        "notes": payload.notes.strip(),
    }


REMOTE_MANIFEST_MAX_BYTES = 2 * 1024 * 1024
REMOTE_MANIFEST_CONTENT_TYPES = {
    "",
    "application/json",
    "application/manifest+json",
    "application/octet-stream",
    "application/schema+json",
    "text/json",
    "text/plain",
}
FLOATING_HUGGINGFACE_REVISIONS = {"main", "master", "dev", "develop", "latest"}
HUGGINGFACE_METADATA_MAX_REDIRECTS = 5
GGUF_QUANTIZATION_RE = re.compile(r"(?i)(?:^|[._-])((?:IQ|Q)?[2-8](?:_[A-Z0-9]+)+|Q[2-8]_K_[A-Z]+|BF16|F16|F32)(?=[._-]|$)")


def _strip_http_etag(value: str | None) -> str:
    candidate = (value or "").strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    return candidate.strip('"').lower()


def _header_sha256(headers: Any) -> str | None:
    for name in ("x-linked-etag", "etag", "x-b1-sha256"):
        candidate = _strip_http_etag(headers.get(name))
        if model_lifecycle.SHA256_RE.match(candidate):
            return candidate
    return None


def _header_size_bytes(headers: Any) -> int | None:
    for name in ("x-linked-size", "content-length"):
        value = headers.get(name)
        if value:
            with suppress(ValueError):
                parsed = int(value)
                if parsed > 0:
                    return parsed
    content_range = headers.get("content-range", "")
    match = re.search(r"/(\d+)\s*$", content_range)
    if match:
        with suppress(ValueError):
            parsed = int(match.group(1))
            if parsed > 0:
                return parsed
    return None


def _header_repo_commit(headers: Any) -> str | None:
    candidate = (headers.get("x-repo-commit") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", candidate):
        return candidate.lower()
    return None


async def fetch_huggingface_gguf_metadata(resolve_url: str) -> dict[str, Any]:
    source_url = resolve_url
    request_url = resolve_url
    redirect_count = 0
    sha256: str | None = None
    size_bytes: int | None = None
    repo_commit: str | None = None
    final_url = resolve_url
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        while True:
            try:
                request_url = model_lifecycle.download_request_url_allowed(source_url, request_url, source_type="huggingface")
            except model_lifecycle.ModelLifecycleError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            response = await client.request("HEAD", request_url, headers={"Accept": "application/octet-stream"})
            sha256 = sha256 or _header_sha256(response.headers)
            size_bytes = size_bytes or _header_size_bytes(response.headers)
            repo_commit = repo_commit or _header_repo_commit(response.headers)
            if response.status_code in model_lifecycle.DOWNLOAD_REDIRECT_STATUS_CODES:
                redirect_count += 1
                if redirect_count > HUGGINGFACE_METADATA_MAX_REDIRECTS:
                    raise HTTPException(status_code=422, detail="Hugging Face file URL exceeded maximum redirect count")
                try:
                    request_url = model_lifecycle.redirect_url_allowed(
                        source_url,
                        request_url,
                        response.headers.get("location", ""),
                        source_type="huggingface",
                    )
                except model_lifecycle.ModelLifecycleError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                continue
            if response.status_code == 405:
                response = await client.request("GET", request_url, headers={"Accept": "application/octet-stream", "Range": "bytes=0-0"})
                sha256 = sha256 or _header_sha256(response.headers)
                size_bytes = size_bytes or _header_size_bytes(response.headers)
                repo_commit = repo_commit or _header_repo_commit(response.headers)
            if response.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Hugging Face file URL returned HTTP {response.status_code}")
            final_url = str(response.url)
            break
    return {"sha256": sha256, "size_bytes": size_bytes, "repo_commit": repo_commit, "final_url": final_url}


def _slug_identifier(value: str, *, prefix: str = "", max_length: int = 128) -> str:
    slug = re.sub(r"[^a-z0-9._+-]+", "-", value.lower()).strip("-._+")
    slug = re.sub(r"[-_]{2,}", "-", slug)
    if not slug or not slug[0].isalnum():
        slug = f"model-{slug}".strip("-")
    if prefix and not slug.startswith(prefix):
        slug = f"{prefix}{slug}"
    slug = slug[:max_length].rstrip("-._+")
    if len(slug) < 2:
        slug = f"{slug or 'm'}1"
    return slug


def _infer_gguf_quantization(file_path: str) -> str | None:
    match = GGUF_QUANTIZATION_RE.search(PurePosixPath(file_path).name)
    return match.group(1).upper() if match else None


def _estimate_llm_resources(size_bytes: int, context_tokens: int) -> dict[str, Any]:
    size_gib = size_bytes / (1024**3)
    context_factor = max(1.0, context_tokens / 4096)
    vram_gib = max(1.0, (size_gib * 1.25) + (0.35 * context_factor))
    ram_gib = max(2.0, size_gib + 2.0)
    return {
        "vram_gib": round(vram_gib, 2),
        "ram_gib": round(ram_gib, 2),
        "disk_gib": round(max(0.01, size_gib), 2),
        "context_tokens": context_tokens,
    }


def _validate_llm_aliases_for_manifest_draft(aliases: list[str]) -> list[str]:
    catalog = catalog_snapshot()
    normalized: list[str] = []
    for alias in aliases:
        candidate = alias.strip()
        if not candidate:
            continue
        try:
            catalog_alias = catalog.require_alias(candidate)
        except CatalogError as exc:
            if not ID_PATTERN.fullmatch(candidate):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"invalid custom model alias for LLM draft: {candidate}; "
                        "aliases must start with a lowercase letter or digit and contain only lowercase letters, digits, dots, underscores, and hyphens"
                    ),
                ) from exc
            catalog_alias = None
        if catalog_alias is not None and catalog_alias.alias.modality != "llm":
            raise HTTPException(status_code=422, detail=f"alias {candidate} is {catalog_alias.alias.modality}, not llm")
        if candidate not in normalized:
            normalized.append(candidate)
    if not normalized:
        raise HTTPException(status_code=422, detail="at least one LLM alias is required")
    return normalized


def build_huggingface_gguf_manifest_draft(
    payload: HuggingFaceGgufManifestDraftRequest,
    source: dict[str, str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    sha256 = (payload.sha256 or metadata.get("sha256") or "").lower()
    if not model_lifecycle.SHA256_RE.match(sha256):
        raise HTTPException(status_code=422, detail="Hugging Face metadata did not expose a SHA-256 ETag; provide sha256 explicitly")
    size_bytes = payload.size_bytes or metadata.get("size_bytes")
    if not isinstance(size_bytes, int) or size_bytes <= 0:
        raise HTTPException(status_code=422, detail="Hugging Face metadata did not expose a positive file size; provide size_bytes explicitly")
    revision = str(metadata.get("repo_commit") or source["revision"])
    warnings: list[str] = []
    if source["revision"] in FLOATING_HUGGINGFACE_REVISIONS:
        if metadata.get("repo_commit"):
            warnings.append(f"resolved floating revision {source['revision']} to immutable commit {revision}")
        else:
            raise HTTPException(status_code=422, detail="Hugging Face file URL uses a floating revision and no x-repo-commit header was available")
    quantization = _infer_gguf_quantization(source["file_path"])
    stem = PurePosixPath(source["file_path"]).name.removesuffix(".gguf")
    aliases = _validate_llm_aliases_for_manifest_draft(payload.aliases or [_slug_identifier(stem)])
    model_id = _slug_identifier(payload.model_id or f"b1-{source['repo_id']}-{stem}-localai", max_length=128)
    display_name = payload.display_name or stem.replace("_", " ").replace("-", " ")
    license_url = payload.license_url or source["repo_url"]
    attribution = payload.attribution or f"{source['repo_id']} / {source['file_path']}"
    if payload.license_name == "review-required":
        warnings.append("license name is a review placeholder; verify the model card before installation")
    warnings.append("resource estimate is derived from file size; run model smoke after install to persist measured RAM/VRAM")
    manifest = {
        "id": model_id,
        "version": revision,
        "display_name": display_name,
        "description": f"Generated LocalAI GGUF LLM manifest draft from {source['repo_url']}. Review licence, aliases, and resource estimate before installation.",
        "modality": "llm",
        "operations": ["chat"],
        "source": {"type": "huggingface", "url": source["repo_url"], "revision": revision},
        "files": [
            {
                "path": source["file_path"],
                "sha256": sha256,
                "size_bytes": size_bytes,
                "format": "gguf",
                **({"quantization": quantization} if quantization else {}),
            }
        ],
        "runtimes": ["localai"],
        "preferred_runtime": "localai",
        "resource_estimate": _estimate_llm_resources(size_bytes, payload.context_tokens),
        "license": {
            "name": payload.license_name,
            "url": license_url,
            "redistribution": "downloadable",
            "attribution": attribution,
            "acceptance_required": payload.acceptance_required,
        },
        "execution_modes": ["hosted-inference", "downloadable"],
        "aliases": aliases,
        "visibility_roles": ["admin", "operator", "creator", "user", "service"],
        "runtime_adapter_versions": {"localai": "b1-runtime-adapter/v1alpha1"},
        "runtime_smoke": {
            "schema": "b1-ai-hub-runtime-smoke/v1",
            "description": "Tiny non-streaming LocalAI chat completion through the generated LocalAI GGUF config.",
            "localai": {
                "request": {"path": "/v1/chat/completions", "method": "POST"},
                "timeout_seconds": 300,
            },
        },
    }
    try:
        parsed = model_lifecycle.parse_uploaded_manifest(manifest)
    except (CatalogError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"generated manifest is invalid: {exc}") from exc
    return {
        "object": "model_manifest_draft",
        "source": source,
        "metadata": {
            "sha256": sha256,
            "size_bytes": size_bytes,
            "repo_commit": metadata.get("repo_commit"),
            "final_url": metadata.get("final_url"),
            "quantization": quantization,
        },
        "manifest": parsed.to_dict(),
        "warnings": warnings,
    }


def validate_remote_manifest_url(manifest_url: str | None) -> str:
    url = (manifest_url or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="manifest_url is required")
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="manifest_url must not contain credentials")
    if parsed.fragment:
        raise HTTPException(status_code=422, detail="manifest_url must not contain a fragment")
    if model_lifecycle.has_credential_query_parameter(parsed.query):
        raise HTTPException(status_code=422, detail="manifest_url must not contain credential query parameters")
    if not model_lifecycle.is_safe_public_import_url(url):
        raise HTTPException(status_code=422, detail="manifest_url must be a public HTTPS URL allowed by import policy")
    return url


async def fetch_remote_manifest_payload(manifest_url: str | None) -> dict[str, Any]:
    source_url = validate_remote_manifest_url(manifest_url)
    request_url = source_url
    redirect_count = 0
    body = bytearray()
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        while True:
            try:
                request_url = model_lifecycle.download_request_url_allowed(source_url, request_url, source_type="direct-url")
            except model_lifecycle.ModelLifecycleError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            async with client.stream("GET", request_url, headers={"Accept": "application/json, application/schema+json;q=0.9, text/plain;q=0.5"}) as response:
                if response.status_code in model_lifecycle.DOWNLOAD_REDIRECT_STATUS_CODES:
                    redirect_count += 1
                    if redirect_count > 5:
                        raise HTTPException(status_code=422, detail="manifest_url exceeded maximum redirect count")
                    try:
                        request_url = model_lifecycle.redirect_url_allowed(
                            source_url,
                            request_url,
                            response.headers.get("location", ""),
                            source_type="direct-url",
                        )
                    except model_lifecycle.ModelLifecycleError as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
                    continue
                if response.status_code >= 400:
                    raise HTTPException(status_code=502, detail=f"manifest_url returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in REMOTE_MANIFEST_CONTENT_TYPES and not content_type.endswith("+json"):
                    raise HTTPException(status_code=422, detail=f"manifest_url returned unsupported content type {content_type}")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > REMOTE_MANIFEST_MAX_BYTES:
                            raise HTTPException(status_code=413, detail="manifest_url response is too large")
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail="manifest_url returned invalid content-length") from exc
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > REMOTE_MANIFEST_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="manifest_url response is too large")
                break
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="manifest_url did not return valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="manifest_url must return a JSON object")
    return parsed


async def manifest_for_install_request(payload: ModelInstallPlanRequest) -> Any:
    provided = sum(1 for value in (payload.model, payload.manifest, payload.manifest_url) if value is not None)
    if provided > 1:
        raise HTTPException(status_code=422, detail="provide only one of model, manifest, or manifest_url")
    if payload.manifest is not None:
        try:
            return model_lifecycle.parse_uploaded_manifest(payload.manifest)
        except (CatalogError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.manifest_url is not None:
        try:
            return model_lifecycle.parse_uploaded_manifest(await fetch_remote_manifest_payload(payload.manifest_url))
        except (CatalogError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not payload.model:
        raise HTTPException(status_code=422, detail="model, manifest, or manifest_url is required")
    catalog = catalog_snapshot()
    alias = catalog.get_alias(payload.model)
    manifest = alias.manifest if alias is not None else catalog.get_manifest(payload.model)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"model {payload.model} is not backed by a catalog manifest")
    return manifest


def install_plan_for_manifest(manifest: Any, payload: ModelInstallPlanRequest) -> dict[str, Any]:
    catalog = catalog_snapshot()
    return model_lifecycle.build_install_plan(
        manifest,
        data_root_path(),
        resource_policy(),
        known_aliases=set(catalog.aliases_by_id),
        model_profiles=catalog.list_profiles(),
        allow_resource_override=payload.allow_resource_override,
        accept_license=payload.accept_license,
    )


def download_plan_for_manifest(
    manifest: Any,
    *,
    accept_license: bool = False,
    allow_resource_override: bool = False,
    allow_download_override: bool = False,
) -> dict[str, Any]:
    catalog = catalog_snapshot()
    try:
        return model_lifecycle.build_download_plan(
            manifest,
            data_root_path(),
            accept_license=accept_license,
            policy=resource_policy(),
            known_aliases=set(catalog.aliases_by_id),
            model_profiles=catalog.list_profiles(),
            allow_resource_override=allow_resource_override,
            allow_download_override=allow_download_override,
        )
    except model_lifecycle.ModelLifecycleError as exc:
        total_size_bytes = sum(file.size_bytes for file in manifest.files)
        estimate = manifest.resource_estimate.to_scheduler_estimate(requires_gpu=manifest.preferred_runtime != "audio-cpu")
        decision = classify_resource_fit(resource_policy(), estimate)
        return {
            "model": manifest.to_dict(),
            "model_ref": f"{manifest.id}@{manifest.version}",
            "status": "blocked",
            "can_download": False,
            "already_available": False,
            "blockers": [str(exc)],
            "warnings": [],
            "requires_license_acceptance": bool(manifest.license.acceptance_required),
            "license_accepted": accept_license,
            "source_url": manifest.source.url,
            "resource_decision": asdict(decision),
            "resource_override": allow_resource_override,
            "download_override": allow_download_override,
            "profile_compatibility": [],
            "target_sha256": manifest.files[0].sha256 if manifest.files else None,
            "target_size_bytes": total_size_bytes,
            "target_path": None,
            "partial_path": None,
            "existing_partial_bytes": 0,
            "file_count": len(manifest.files),
            "files": [],
            "verification": [],
        }


def public_model_download(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    manifest = public.get("manifest") or {}
    target_size = int(public.get("target_size_bytes") or 0)
    bytes_downloaded = int(public.get("bytes_downloaded") or 0)
    public["progress_percent"] = round((bytes_downloaded / target_size) * 100, 2) if target_size > 0 else 0
    public["file_count"] = len(manifest.get("files") or [])
    public["model_ref"] = f"{public.get('model_id')}@{public.get('model_version')}"
    public["authenticated"] = bool(public.get("credential_secret_name"))
    license_info = manifest.get("license") if isinstance(manifest.get("license"), dict) else {}
    public["requires_license_acceptance"] = bool(license_info.get("acceptance_required"))
    public["license_accepted"] = bool(public.get("license_accepted"))
    public["install_ready"] = public.get("status") == "completed"
    return jsonable_encoder(public)


def parse_model_record_manifest(row: dict[str, Any]) -> Any:
    try:
        return model_lifecycle.parse_uploaded_manifest(row["manifest"])
    except (CatalogError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"stored manifest is invalid: {exc}") from exc


def parse_download_manifest(row: dict[str, Any]) -> Any:
    try:
        return model_lifecycle.parse_uploaded_manifest(row["manifest"])
    except (CatalogError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"stored download manifest is invalid: {exc}") from exc


def model_smoke_alias(manifest: Any, requested_alias: str | None = None) -> str:
    if requested_alias is None or not requested_alias.strip():
        return manifest.aliases[0] if manifest.aliases else manifest.id
    candidate = requested_alias.strip()
    allowed = {manifest.id, *list(manifest.aliases or [])}
    if candidate not in allowed:
        raise ValueError(f"model smoke alias is not declared by manifest: {candidate}")
    return candidate


def model_smoke_runtime_job(manifest: Any, auth: AuthContext | None = None, model_alias: str | None = None) -> dict[str, Any]:
    selected_alias = model_smoke_alias(manifest, model_alias)
    request_params: dict[str, Any] = {"source": "admin_model_smoke_test"}
    runtime_smoke = getattr(manifest, "runtime_smoke", None)
    if isinstance(runtime_smoke, dict) and runtime_smoke:
        request_params["runtime_smoke"] = runtime_smoke
    return {
        "id": f"modelsmoke_{uuid.uuid4().hex}",
        "runtime": manifest.preferred_runtime,
        "model_alias": selected_alias,
        "resolved_model_version": f"{manifest.id}@{manifest.version}",
        "modality": manifest.modality,
        "operation": "model-smoke",
        "request_params": request_params,
        "artifacts": [],
        "owner_id": auth.subject_id if auth is not None else "",
        "durable_job": False,
    }


def model_smoke_resolution(manifest: Any, model_alias: str | None = None) -> RuntimeResolution:
    selected_alias = model_smoke_alias(manifest, model_alias)
    requires_gpu = manifest.preferred_runtime in GPU_RUNTIMES and manifest.resource_estimate.vram_gib > 0
    return RuntimeResolution(
        public_alias=selected_alias,
        model_id=manifest.id,
        model_version=manifest.version,
        resolved_model_version=f"{manifest.id}@{manifest.version}",
        runtime=manifest.preferred_runtime,
        preferred_runtime=manifest.preferred_runtime,
        requires_gpu=requires_gpu,
        resource_label="smoke-test",
        runtime_policy="model_smoke",
    )


def runtime_urls_for_smoke() -> dict[str, str]:
    return {
        **scheduler_runtime_urls(),
        "audio-cpu": settings.audio_cpu_url,
    }


def runtime_control_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = settings.runtime_control_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def compact_smoke_hook_result(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "unconfirmed"}
    compact: dict[str, Any] = {}
    for key in (
        "status",
        "reason",
        "action",
        "strategy",
        "runtime",
        "message",
        "model",
        "model_alias",
        "resolved_model_version",
        "engine",
        "placeholder",
        "gpu_lease_required",
    ):
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            compact[key] = value
    measurements = result.get("measurements")
    if isinstance(measurements, dict):
        safe_measurements: dict[str, Any] = {}
        for key, value in measurements.items():
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)) and len(key) <= 128:
                safe_measurements[key] = value
        if safe_measurements:
            compact["measurements"] = safe_measurements
    return compact


def hook_int_measurement(result: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(result, dict):
        return None
    candidates = [result.get(key)]
    measurements = result.get("measurements")
    if isinstance(measurements, dict):
        candidates.append(measurements.get(key))
    for value in candidates:
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0:
            return int(value)
    return None


def peak_vram_mib_from_metrics(runner: GpuJobRunner, metrics_items: list[dict[str, Any] | None]) -> int | None:
    values: list[int] = []
    for metrics in metrics_items:
        item_values = runner.gpu_memory_used_mib(metrics)
        if item_values:
            values.extend(item_values)
    return max(values) if values else None


def peak_ram_mib_from_metrics(runner: GpuJobRunner, metrics_items: list[dict[str, Any] | None]) -> int | None:
    values = [runner.host_ram_used_mib(metrics) for metrics in metrics_items]
    concrete = [value for value in values if value is not None]
    return max(concrete) if concrete else None


def measured_resource_estimate(manifest: Any, peak_vram_mib: int | None, peak_ram_mib: int | None) -> dict[str, Any]:
    estimate = manifest.resource_estimate.to_dict()
    if peak_vram_mib is not None:
        estimate["vram_gib"] = round(max(float(estimate["vram_gib"]), peak_vram_mib / 1024), 3)
    if peak_ram_mib is not None:
        estimate["ram_gib"] = round(max(float(estimate["ram_gib"]), peak_ram_mib / 1024), 3)
    return estimate


def append_model_smoke_measurement(manifest_payload: dict[str, Any], run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = model_lifecycle.parse_uploaded_manifest(manifest_payload)
    updated_manifest = manifest.to_dict()
    existing_measurements = dict(manifest.measurements or {})
    runs = existing_measurements.get("runs") if isinstance(existing_measurements.get("runs"), list) else []
    runs = [item for item in runs if isinstance(item, dict)]
    original_estimate = existing_measurements.get("original_resource_estimate")
    if not isinstance(original_estimate, dict):
        original_estimate = dict(manifest_payload.get("resource_estimate") or manifest.resource_estimate.to_dict())
    latest_estimate = existing_measurements.get("latest_resource_estimate") if isinstance(existing_measurements.get("latest_resource_estimate"), dict) else manifest.resource_estimate.to_dict()
    if run.get("status") == "ok":
        latest_estimate = measured_resource_estimate(manifest, run.get("peak_vram_mib"), run.get("peak_ram_mib"))
        run = {**run, "resource_estimate": latest_estimate}
        updated_manifest["resource_estimate"] = latest_estimate
    measurements = {
        "schema": "b1-ai-hub-model-measurements/v1",
        "source": "control-plane",
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "original_resource_estimate": original_estimate,
        "latest_resource_estimate": latest_estimate,
        "runs": [*runs, run][-100:],
    }
    updated_manifest["measurements"] = measurements
    parsed = model_lifecycle.parse_uploaded_manifest(updated_manifest)
    decision = classify_resource_fit(
        resource_policy(),
        ResourceEstimate(
            vram_gib=parsed.resource_estimate.vram_gib,
            ram_gib=parsed.resource_estimate.ram_gib,
            disk_gib=parsed.resource_estimate.disk_gib,
            requires_gpu=parsed.preferred_runtime != "audio-cpu",
        ),
    )
    return updated_manifest, {"resource_label": decision.label, "resource_decision": jsonable_encoder(decision)}


async def post_cpu_runtime_smoke(runtime: str, job: dict[str, Any]) -> dict[str, Any]:
    runtime_url = runtime_urls_for_smoke().get(runtime, "").rstrip("/")
    if not runtime_url:
        return {"status": "unsupported", "reason": "runtime_url_missing", "runtime": runtime, "action": "smoke"}
    try:
        async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
            response = await client.post(f"{runtime_url}/b1/runtime/smoke", json=runtime_control_payload_for_smoke(job), headers=runtime_control_headers())
    except httpx.HTTPError:
        return {"status": "unsupported", "reason": "runtime_control_unreachable", "runtime": runtime, "action": "smoke"}
    if response.status_code in {404, 405}:
        return {"status": "unsupported", "reason": f"http_{response.status_code}", "runtime": runtime, "action": "smoke"}
    if response.status_code >= 400:
        raise RuntimeError(f"{runtime} runtime smoke hook returned HTTP {response.status_code}")
    if not response.content:
        return {"status": "ok", "runtime": runtime, "action": "smoke"}
    try:
        body = response.json()
    except ValueError:
        return {"status": "ok", "runtime": runtime, "action": "smoke"}
    return body if isinstance(body, dict) else {"status": "ok", "runtime": runtime, "action": "smoke"}


def runtime_control_payload_for_smoke(job: dict[str, Any]) -> dict[str, Any]:
    resolved = str(job.get("resolved_model_version") or "")
    model = resolved.split("@", 1)[0] if "@" in resolved else str(job.get("model_alias") or resolved)
    runtime = str(job.get("runtime") or "")
    payload = {
        "job_id": str(job["id"]),
        "runtime": runtime,
        "model": model,
        "model_alias": str(job.get("model_alias") or ""),
        "resolved_model_version": resolved,
        "modality": str(job.get("modality") or ""),
        "operation": str(job.get("operation") or ""),
    }
    request_params = job.get("request_params")
    runtime_smoke = request_params.get("runtime_smoke") if isinstance(request_params, dict) else None
    if isinstance(runtime_smoke, dict):
        payload["runtime_smoke"] = runtime_smoke
        runtime_config = runtime_smoke.get(runtime)
        if isinstance(runtime_config, dict):
            payload["runtime_smoke_config"] = runtime_config
    return payload


async def run_model_runtime_smoke(manifest: Any, auth: AuthContext, *, model_alias: str | None = None) -> dict[str, Any]:
    job = model_smoke_runtime_job(manifest, auth, model_alias=model_alias)
    resolution = model_smoke_resolution(manifest, model_alias=model_alias)
    started = monotonic()
    started_at = datetime.now(tz=UTC)
    load_time_ms: int | None = None
    run_time_ms: int | None = None
    metrics: list[dict[str, Any] | None] = []
    hook_result: dict[str, Any] | None = None
    owner = await acquire_inference_lease(resolution, "model-smoke", owner_id=auth.subject_id)
    try:
        if resolution.requires_gpu:
            runner = runtime_control_runner(settings.sync_inference_lease_ttl_seconds)
            metrics.append(await runner.runtime_agent_get("/v1/metrics"))
            load_started = monotonic()
            await runner.unload_other_gpu_runtimes(job)
            await runner.verify_vram_or_recover(job)
            await runner.load_runtime_model(job)
            await runner.warm_runtime_model(job)
            load_time_ms = elapsed_milliseconds(load_started)
            run_started = monotonic()
            hook_result = await runner.smoke_runtime_model(job)
            run_time_ms = elapsed_milliseconds(run_started)
            metrics.append(await runner.runtime_agent_get("/v1/metrics"))
            with suppress(Exception):
                await runner.record_runtime_idle_for_job(job, {"source": "model_smoke_test", "status": str((hook_result or {}).get("status") or "unknown")})
            vram_candidates = [peak_vram_mib_from_metrics(runner, metrics), hook_int_measurement(hook_result, "peak_vram_mib")]
            ram_candidates = [peak_ram_mib_from_metrics(runner, metrics), hook_int_measurement(hook_result, "peak_ram_mib")]
            peak_vram_mib = max(value for value in vram_candidates if value is not None) if any(value is not None for value in vram_candidates) else None
            peak_ram_mib = max(value for value in ram_candidates if value is not None) if any(value is not None for value in ram_candidates) else None
        else:
            load_time_ms = 0
            run_started = monotonic()
            hook_result = await post_cpu_runtime_smoke(manifest.preferred_runtime, job)
            run_time_ms = elapsed_milliseconds(run_started)
            peak_vram_mib = hook_int_measurement(hook_result, "peak_vram_mib")
            if peak_vram_mib is None:
                peak_vram_mib = 0
            peak_ram_mib = hook_int_measurement(hook_result, "peak_ram_mib")
    except Exception as exc:
        completed_at = datetime.now(tz=UTC)
        return {
            "id": job["id"],
            "type": "install-smoke",
            "status": "failed",
            "runtime": manifest.preferred_runtime,
            "model_alias": job["model_alias"],
            "resolved_model_version": job["resolved_model_version"],
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_ms": elapsed_milliseconds(started),
            "load_time_ms": load_time_ms,
            "run_time_ms": run_time_ms,
            "error": exc.__class__.__name__,
        }
    finally:
        await release_inference_lease(owner)
    completed_at = datetime.now(tz=UTC)
    hook_status = str((hook_result or {}).get("status") or "unconfirmed")
    return {
        "id": job["id"],
        "type": "install-smoke",
        "status": "ok" if hook_status == "ok" else hook_status,
        "runtime": manifest.preferred_runtime,
        "model_alias": job["model_alias"],
        "resolved_model_version": job["resolved_model_version"],
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": elapsed_milliseconds(started),
        "load_time_ms": load_time_ms,
        "run_time_ms": run_time_ms,
        "peak_vram_mib": peak_vram_mib,
        "peak_ram_mib": peak_ram_mib,
        "hook": compact_smoke_hook_result(hook_result),
    }


async def smoke_test_model_record(row: dict[str, Any], auth: AuthContext, *, persist: bool = True, model_alias: str | None = None) -> dict[str, Any]:
    manifest = parse_model_record_manifest(row)
    try:
        run = await run_model_runtime_smoke(manifest, auth, model_alias=model_alias)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    updated_row = row
    measurement_info: dict[str, Any] | None = None
    if persist and run.get("status") == "ok":
        updated_manifest, measurement_info = append_model_smoke_measurement(row["manifest"], run)
        updated_row = await database.upsert_model_record(
            {
                "id": row["id"],
                "version": row["version"],
                "display_name": row["display_name"],
                "modality": row["modality"],
                "preferred_runtime": row["preferred_runtime"],
                "status": row["status"],
                "resource_label": measurement_info["resource_label"],
                "manifest": updated_manifest,
            }
        )
        await refresh_catalog_cache()
        await refresh_workflow_dependency_statuses()
    await record_audit_event(
        auth,
        "model.smoke_tested" if run.get("status") == "ok" else "model.smoke_test_failed",
        target_type="model",
        target_id=f"{row['id']}@{row['version']}",
        summary=f"Model smoke test {run.get('status')} for {row['id']}@{row['version']}",
        metadata={
            "runtime": run.get("runtime"),
            "status": run.get("status"),
            "resource_label": measurement_info.get("resource_label") if measurement_info else row.get("resource_label"),
            "persisted": bool(persist and run.get("status") == "ok"),
        },
    )
    return {"smoke_test": run, "model_record": updated_row, "measurement": measurement_info, "persisted": bool(persist and run.get("status") == "ok")}


async def installed_model_alias_conflicts(manifest: Any) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    requested_aliases = set(manifest.aliases)
    if not requested_aliases:
        return conflicts
    requested_ref = f"{manifest.id}@{manifest.version}"
    for row in await database.list_model_records(status="installed"):
        installed_ref = f"{row['id']}@{row['version']}"
        if installed_ref == requested_ref:
            continue
        row_manifest = row.get("manifest") or {}
        installed_aliases = row_manifest.get("aliases") if isinstance(row_manifest, dict) else []
        if not isinstance(installed_aliases, list):
            continue
        for alias in sorted(requested_aliases.intersection(str(item) for item in installed_aliases)):
            conflicts.append({"alias": alias, "installed_model_ref": installed_ref})
    return conflicts


def runtime_views_include(runtime_views: list[dict[str, Any]], runtime: str) -> bool:
    return any(str(view.get("runtime") or "") == runtime for view in runtime_views)


LOCALAI_RELOAD_SAFE_RUNTIME_STAGES = {"idle", "idle_unloaded", "idle_unload_unconfirmed"}
LOCALAI_RELOAD_SAFE_RUNTIME_STATUSES = {"idle", "unload_ok"}
LOCALAI_RELOAD_BUSY_RUNTIME_STAGES = {
    JobState.UNLOADING.value,
    JobState.VERIFYING_VRAM.value,
    JobState.LOADING.value,
    JobState.WARMING.value,
    JobState.RUNNING.value,
    JobState.SAVING.value,
    JobState.CANCELLING.value,
    "smoke",
}
LOCALAI_RELOAD_BUSY_RUNTIME_STATUSES = set(ACTIVE_JOB_STATES) | {"loaded", "ready"}


def compact_localai_reload_runtime_state(state: dict[str, Any]) -> dict[str, Any]:
    updated_at = state.get("updated_at")
    if isinstance(updated_at, datetime):
        encoded_updated_at: str | None = updated_at.isoformat()
    elif isinstance(updated_at, str):
        encoded_updated_at = updated_at
    else:
        encoded_updated_at = None
    return {
        "runtime": str(state.get("runtime") or ""),
        "status": str(state.get("status") or ""),
        "stage": str(state.get("stage") or ""),
        "active_model": str(state.get("active_model") or ""),
        "model_alias": str(state.get("model_alias") or ""),
        "resolved_model_version": str(state.get("resolved_model_version") or ""),
        "job_id": str(state.get("job_id") or ""),
        "updated_at": encoded_updated_at,
    }


def compact_localai_reload_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(job.get("id") or ""),
        "state": str(job.get("state") or ""),
        "stage": str(job.get("stage") or ""),
        "model_alias": str(job.get("model_alias") or ""),
        "resolved_model_version": str(job.get("resolved_model_version") or ""),
    }


async def active_localai_jobs_for_config_reload() -> list[dict[str, Any]]:
    list_jobs = getattr(database, "list_jobs", None)
    if list_jobs is None:
        return []
    active_jobs: list[dict[str, Any]] = []
    for state in sorted(ACTIVE_JOB_STATES):
        rows = await list_jobs(limit=20, runtime="localai", state=state)
        active_jobs.extend(row for row in rows if isinstance(row, dict) and str(row.get("state") or "") in ACTIVE_JOB_STATES)
    return active_jobs


async def localai_config_reload_blocker() -> dict[str, Any] | None:
    active_jobs = await active_localai_jobs_for_config_reload()
    if active_jobs:
        return {
            "reason": "localai_jobs_active",
            "active_jobs": [compact_localai_reload_job(job) for job in active_jobs[:5]],
            "active_job_count": len(active_jobs),
        }
    state = await current_runtime_state("localai")
    if state is None:
        return None
    status = str(state.get("status") or "").strip().lower()
    stage = str(state.get("stage") or "").strip().lower()
    if status in LOCALAI_RELOAD_SAFE_RUNTIME_STATUSES or stage in LOCALAI_RELOAD_SAFE_RUNTIME_STAGES:
        return None
    if status in LOCALAI_RELOAD_BUSY_RUNTIME_STATUSES or stage in LOCALAI_RELOAD_BUSY_RUNTIME_STAGES:
        return {
            "reason": "localai_runtime_active",
            "runtime_state": compact_localai_reload_runtime_state(state),
        }
    return None


async def wait_for_localai_ready_after_restart(timeout_seconds: float = 60.0) -> dict[str, Any]:
    url = f"{settings.localai_url.rstrip('/')}/readyz"
    deadline = monotonic() + max(1.0, timeout_seconds)
    attempts = 0
    last_error = "timeout"
    while monotonic() < deadline:
        attempts += 1
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
            if response.status_code < 400:
                return {"status": "ok", "url": url, "attempts": attempts, "http_status": response.status_code}
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = exc.__class__.__name__
        await asyncio.sleep(1.0)
    return {"status": "failed", "url": url, "attempts": attempts, "error": last_error}


async def reload_localai_model_config_after_install(model_ref: str, runtime_views: list[dict[str, Any]]) -> dict[str, Any]:
    if not runtime_views_include(runtime_views, "localai"):
        return {"status": "not_required", "service": "localai", "action": "restart", "reason": "model_has_no_localai_runtime_view"}
    if not settings.runtime_agent_token:
        return {"status": "skipped", "service": "localai", "action": "restart", "reason": "runtime_agent_token_missing"}
    blocker = await localai_config_reload_blocker()
    if blocker is not None:
        return {
            "status": "deferred",
            "service": "localai",
            "action": "restart",
            "strategy": "restart_service",
            "model_ref": model_ref,
            **blocker,
        }
    result, error = await runtime_agent_post(
        "/v1/services/localai/restart",
        {
            "reason": operational_reason_label("reload LocalAI model configuration after model install", None),
            "timeout_seconds": 30,
        },
        timeout_seconds=45.0,
    )
    if error is not None:
        return {
            "status": "failed",
            "service": "localai",
            "action": "restart",
            "strategy": "restart_service",
            "model_ref": model_ref,
            "error": error[:500],
        }
    status = runtime_action_status(result)
    payload = {
        "status": status,
        "service": "localai",
        "action": "restart",
        "strategy": "restart_service",
        "model_ref": model_ref,
        "runtime_agent": compact_runtime_action_result(result),
    }
    if status != "ok":
        return payload
    readiness = await wait_for_localai_ready_after_restart()
    payload["readiness"] = readiness
    if readiness.get("status") != "ok":
        payload["status"] = "degraded"
    return payload


async def sync_comfyui_model_view_after_install(model_ref: str, runtime_views: list[dict[str, Any]]) -> dict[str, Any]:
    if not runtime_views_include(runtime_views, "comfyui"):
        return {"status": "not_required", "service": "comfyui", "action": "model-view-sync", "reason": "model_has_no_comfyui_runtime_view"}
    url = f"{settings.comfyui_url.rstrip('/')}/b1/runtime/status"
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            response = await client.post(url, json={"reason": "model_install", "model_ref": model_ref}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return {
            "status": "failed",
            "service": "comfyui",
            "action": "model-view-sync",
            "model_ref": model_ref,
            "reason": "runtime_control_unreachable",
            "error": exc.__class__.__name__,
        }
    if response.status_code >= 400:
        return {
            "status": "failed",
            "service": "comfyui",
            "action": "model-view-sync",
            "model_ref": model_ref,
            "reason": f"http_{response.status_code}",
        }
    try:
        body = response.json()
    except ValueError:
        body = {}
    model_folders = body.get("model_folders") if isinstance(body, dict) else {}
    runtime_view_sync = model_folders.get("runtime_view_sync") if isinstance(model_folders, dict) else {}
    return {
        "status": "ok" if isinstance(runtime_view_sync, dict) and runtime_view_sync.get("status") == "ok" else "unconfirmed",
        "service": "comfyui",
        "action": "model-view-sync",
        "model_ref": model_ref,
        "runtime_view_sync": runtime_view_sync if isinstance(runtime_view_sync, dict) else {},
        "model_file_count": model_folders.get("file_count") if isinstance(model_folders, dict) else None,
    }


async def install_model_manifest(
    *,
    manifest: Any,
    payload: ModelInstallRequest,
    auth: AuthContext,
    source_download: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_manifest_role_action(manifest, auth, "install")
    plan = install_plan_for_manifest(manifest, payload)
    alias_conflicts = await installed_model_alias_conflicts(manifest)
    if alias_conflicts:
        conflict_text = ", ".join(f"{item['alias']} -> {item['installed_model_ref']}" for item in alias_conflicts)
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"model aliases are already assigned to installed models: {conflict_text}",
                "alias_conflicts": alias_conflicts,
                "plan": plan,
            },
        )
    try:
        model_lifecycle.require_installable(plan, confirmed=payload.confirm)
        runtime_views = model_lifecycle.create_runtime_views(manifest, data_root_path())
    except model_lifecycle.ModelLifecycleError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "plan": plan}) from exc
    installed_manifest = {**manifest.to_dict(), "installation_status": "installed"}
    row = await database.upsert_model_record(
        {
            "id": manifest.id,
            "version": manifest.version,
            "display_name": manifest.display_name,
            "modality": manifest.modality,
            "preferred_runtime": manifest.preferred_runtime,
            "status": "installed",
            "resource_label": plan["resource_decision"]["label"],
            "manifest": installed_manifest,
        }
    )
    await refresh_catalog_cache()
    workflows = await refresh_workflow_dependency_statuses()
    localai_config_reload = await reload_localai_model_config_after_install(plan["model_ref"], runtime_views)
    comfyui_model_view_sync = await sync_comfyui_model_view_after_install(plan["model_ref"], runtime_views)
    smoke_result: dict[str, Any] | None = None
    if payload.smoke_test:
        smoke_result = await smoke_test_model_record(row, auth, persist=True)
        row = smoke_result["model_record"]
    metadata: dict[str, Any] = {
        "aliases": manifest.aliases,
        "preferred_runtime": manifest.preferred_runtime,
        "resource_label": row["resource_label"],
        "total_size_bytes": plan["total_size_bytes"],
        "workflow_dependencies_refreshed": workflows["count"],
        "runtime_views": [{"runtime": view["runtime"], "host_path": view["host_path"], "container_path": view["container_path"]} for view in runtime_views],
        "localai_config_reload": localai_config_reload,
        "comfyui_model_view_sync": comfyui_model_view_sync,
        "smoke_test": smoke_result["smoke_test"] if smoke_result else None,
    }
    if source_download is not None:
        metadata["source_download_id"] = source_download["id"]
        metadata["download_authenticated"] = bool(source_download.get("credential_secret_name"))
    await record_audit_event(
        auth,
        "model.installed",
        target_type="model",
        target_id=plan["model_ref"],
        summary=f"Installed model {plan['model_ref']}",
        metadata=metadata,
    )
    return {
        "model": public_model_record(row),
        "plan": plan,
        "runtime_views": runtime_views,
        "workflow_refresh": workflows,
        "localai_config_reload": localai_config_reload,
        "comfyui_model_view_sync": comfyui_model_view_sync,
        "smoke_test": smoke_result,
    }


async def dependent_workflows_for_model(model_id: str, aliases: list[str]) -> list[dict[str, str]]:
    candidates = {model_id, *aliases}
    dependencies: list[dict[str, str]] = []
    for row in await database.list_workflows():
        manifest = row.get("manifest") or {}
        for dependency in manifest.get("dependencies") or []:
            if dependency.get("type") == "model" and dependency.get("id") in candidates:
                dependencies.append({"id": row["id"], "version": row["version"], "dependency": dependency["id"]})
                break
    return dependencies


async def dependent_voice_profiles_for_model(model_id: str, aliases: list[str]) -> list[dict[str, Any]]:
    candidates = {model_id, *aliases}
    profiles: list[dict[str, Any]] = []
    for row in await database.list_voice_profiles(include_deleted=False):
        model_alias = row.get("model_alias")
        if model_alias not in candidates:
            continue
        profiles.append(
            {
                "id": row["id"],
                "display_name": row.get("display_name"),
                "runtime": row.get("runtime"),
                "engine": row.get("engine"),
                "model_alias": model_alias,
                "profile_type": row.get("profile_type"),
                "status": row.get("status"),
            }
        )
    return profiles


def dependent_model_profiles_for_model(model_id: str, aliases: list[str]) -> list[dict[str, Any]]:
    alias_set = set(aliases)
    profiles: list[dict[str, Any]] = []
    catalog = catalog_snapshot()
    for profile in catalog.list_profiles():
        matched_aliases = sorted(alias_set.intersection(profile.aliases))
        if not matched_aliases:
            continue
        record = catalog.profile_record(profile)
        profiles.append(
            {
                "id": record["id"],
                "display_name": record["display_name"],
                "matched_aliases": matched_aliases,
                "modality": record["modality"],
                "operations": record["operations"],
                "preferred_runtimes": record["preferred_runtimes"],
                "target_class": record["target_class"],
                "runtime_policy": record["runtime_policy"],
                "target_resource_label": record["target_resource_label"],
                "resource_label": record["resource_label"],
                "resource_decision": record.get("resource_decision"),
                "candidate_manifest": model_id in (record.get("candidate_manifest_ids") or []),
            }
        )
    return profiles


MODEL_RECORD_QUARANTINE_DESCRIPTION = (
    "database record will be marked quarantined; runtime views will be moved to recoverable quarantine; "
    "blobs remain recoverable under the authoritative blob store"
)


async def model_dependency_context_for_record(row: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    manifest = parse_model_record_manifest(row)
    model_ref = f"{row['id']}@{row['version']}"
    dependent_workflows = await dependent_workflows_for_model(row["id"], manifest.aliases)
    dependent_model_profiles = dependent_model_profiles_for_model(row["id"], manifest.aliases)
    dependent_voice_profiles = await dependent_voice_profiles_for_model(row["id"], manifest.aliases)
    active_voice_profiles = active_voice_profile_dependencies(dependent_voice_profiles)
    active_runtime_reservations = await active_runtime_reservations_for_model(model_ref, manifest.aliases)
    return manifest, {
        "model_ref": model_ref,
        "active_jobs": await database.count_active_jobs_for_model(model_ref, manifest.aliases),
        "dependent_workflows": dependent_workflows,
        "dependent_model_profiles": dependent_model_profiles,
        "dependent_voice_profiles": dependent_voice_profiles,
        "active_voice_profiles": active_voice_profiles,
        "active_runtime_reservations": active_runtime_reservations,
    }


def model_record_quarantine_plan(row: dict[str, Any], dependencies: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    if row.get("status") != "installed":
        blockers.append("model record is not installed")
    if dependencies["active_jobs"]:
        blockers.append("model is referenced by active jobs")
    if dependencies["active_voice_profiles"]:
        blockers.append("model is referenced by active voice profiles")
    if dependencies["active_runtime_reservations"]:
        blockers.append("model is referenced by active runtime reservations")
    return {
        "model_ref": dependencies["model_ref"],
        "model_status": row.get("status"),
        "status": "blocked" if blockers else "ready",
        "can_quarantine": not blockers,
        "blockers": blockers,
        "quarantine": MODEL_RECORD_QUARANTINE_DESCRIPTION,
        **dependencies,
    }


def public_runtime_reservation_dependency(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row.get("owner_id"),
        "runtime": row.get("runtime"),
        "model_alias": row.get("model_alias"),
        "resolved_model_version": row.get("resolved_model_version"),
        "expires_at": row.get("expires_at"),
    }


async def active_runtime_reservations_for_model(model_ref: str, aliases: list[str]) -> list[dict[str, Any]]:
    alias_set = set(aliases)
    reservations: list[dict[str, Any]] = []
    for row in await database.list_active_runtime_reservations(GPU_RUNTIMES):
        resolved_model_version = row.get("resolved_model_version")
        matches_immutable_model = resolved_model_version == model_ref
        matches_legacy_alias_only = not resolved_model_version and row.get("model_alias") in alias_set
        if not (matches_immutable_model or matches_legacy_alias_only):
            continue
        reservations.append(public_runtime_reservation_dependency(row))
    return reservations


async def active_runtime_reservations_for_alias(alias: str) -> list[dict[str, Any]]:
    reservations: list[dict[str, Any]] = []
    for row in await database.list_active_runtime_reservations(GPU_RUNTIMES):
        if row.get("model_alias") != alias:
            continue
        reservations.append(public_runtime_reservation_dependency(row))
    return reservations


def normalized_visibility_roles(roles: list[str] | tuple[str, ...]) -> list[str]:
    return sorted(set(roles))


def alias_resolution_policy_snapshot(alias: CatalogAlias) -> dict[str, Any]:
    return {
        "preferred_runtime": alias.preferred_runtime,
        "visibility_roles": normalized_visibility_roles(alias.alias.visibility_roles),
    }


def alias_resolution_snapshot_from_policy(alias: CatalogAlias, policy: dict[str, Any]) -> dict[str, Any]:
    preferred_runtime = policy.get("preferred_runtime")
    if preferred_runtime is None:
        preferred_runtime = alias.manifest.preferred_runtime if alias.manifest else alias.alias.preferred_runtime
    return {
        "preferred_runtime": preferred_runtime,
        "visibility_roles": normalized_visibility_roles(policy.get("visibility_roles") or []),
    }


def alias_resolution_reset_snapshot(alias: CatalogAlias) -> dict[str, Any]:
    preferred_runtime = alias.manifest.preferred_runtime if alias.manifest else alias.alias.preferred_runtime
    return {"preferred_runtime": preferred_runtime, "visibility_roles": []}


def alias_resolution_policy_changes(current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for key in ("preferred_runtime", "visibility_roles"):
        if current.get(key) == proposed.get(key):
            continue
        changes[key] = {"current": current.get(key), "proposed": proposed.get(key)}
    return changes


def raise_alias_active_reservation_policy_change(
    alias: str,
    active_runtime_reservations: list[dict[str, Any]],
    changes: dict[str, dict[str, Any]],
) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "message": "alias has active runtime reservations and resolution policy cannot be changed",
            "alias": alias,
            "resolution_changes": changes,
            "active_runtime_reservations": active_runtime_reservations,
        },
    )


def active_voice_profile_dependencies(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [profile for profile in profiles if profile.get("status") == "active"]


def manifest_is_downloadable(record: dict[str, Any]) -> bool:
    return modelhub_policy.manifest_is_downloadable(record)


def downloadable_versions_for(model_id: str) -> list[dict[str, Any]]:
    return modelhub_policy.downloadable_versions_for(catalog_snapshot(), model_id)


def downloadable_records_for_blob(sha256: str) -> list[dict[str, Any]]:
    return modelhub_policy.downloadable_records_for_blob(catalog_snapshot(), sha256)


def validate_modelhub_blob_sha256(sha256: str) -> str:
    try:
        return modelhub_policy.validate_blob_sha256(sha256)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def validate_modelhub_model_identifier(model_id: str) -> str:
    try:
        return modelhub_policy.validate_model_identifier(model_id)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def model_allowed_by_client(client: dict[str, Any] | None, model_id: str, record: dict[str, Any] | None = None) -> bool:
    if client is None:
        return True
    candidate = record or modelhub_model_record(model_id)
    return modelhub_policy.model_allowed_by_allowed_set(client.get("allowed_models") or [], model_id, candidate)


def model_allowed_by_auth_permissions(auth: AuthContext | None, model_id: str, record: dict[str, Any], action: str) -> bool:
    if auth is None or auth.has_scope("*"):
        return True
    if not modelhub_policy.role_allowed_by_manifest_permissions(record, auth.role.value, action):
        return False
    resolved = record.get("resolved_model") if isinstance(record.get("resolved_model"), dict) else None
    if resolved is None:
        return True
    versions_for = getattr(catalog_snapshot(), "versions_for", None)
    if not callable(versions_for):
        return True
    versions = versions_for(model_id)
    if not versions:
        return True
    return any(modelhub_policy.role_allowed_by_manifest_permissions(version, auth.role.value, action) for version in versions)


def modelhub_record_is_alias(record: dict[str, Any]) -> bool:
    return record.get("object") == "model" and isinstance(record.get("resolved_model"), dict) and "root" in record


def modelhub_record_allowed(auth: AuthContext, client: dict[str, Any] | None, model_id: str, record: dict[str, Any], action: str) -> bool:
    if client is not None and not model_allowed_by_client(client, model_id, record):
        return False
    return model_allowed_by_auth_permissions(auth, model_id, record, action)


async def authorized_modelhub_model_view(
    auth: AuthContext,
    model_id: str,
    *,
    for_download: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    model_id = validate_modelhub_model_identifier(model_id)
    client = await modelhub_client_for_auth(auth)
    if client is not None and for_download and not client.get("allow_downloads", True):
        raise HTTPException(status_code=403, detail="Model Hub client is not permitted to download blobs")
    catalog = catalog_snapshot()
    model_record = catalog.model_or_alias_record(model_id)
    if model_record is None:
        raise HTTPException(status_code=404, detail="model not found")
    action = "download" if for_download else "read"
    model_record_allowed = modelhub_record_allowed(auth, client, model_id, model_record, action)
    version_records = catalog.versions_for(model_id)
    if modelhub_record_is_alias(model_record):
        if not model_record_allowed:
            raise HTTPException(status_code=403, detail=f"Model Hub client is not permitted to access {model_id}")
        allowed_versions = [record for record in version_records if modelhub_record_allowed(auth, client, model_id, record, action)]
        return model_record, allowed_versions, client
    candidate_records = version_records or [model_record]
    allowed_versions = [record for record in candidate_records if modelhub_record_allowed(auth, client, model_id, record, action)]
    if not allowed_versions:
        raise HTTPException(status_code=403, detail=f"Model Hub client is not permitted to access {model_id}")
    selected_record = model_record if model_record_allowed else allowed_versions[0]
    return selected_record, allowed_versions, client


def modelhub_catalog_for_client(client: dict[str, Any] | None, auth: AuthContext | None = None) -> dict[str, Any]:
    catalog = catalog_snapshot().to_catalog()
    if client is None:
        if auth is None or auth.has_scope("*"):
            return modelhub_policy.public_modelhub_metadata(catalog)
        aliases = [
            record
            for record in catalog.get("aliases", [])
            if isinstance(record, dict) and model_allowed_by_auth_permissions(auth, str(record.get("id") or ""), record, "read")
        ]
        models = [
            record
            for record in catalog.get("models", [])
            if isinstance(record, dict) and model_allowed_by_auth_permissions(auth, str(record.get("id") or ""), record, "read")
        ]
        return modelhub_policy.public_modelhub_metadata({**catalog, "aliases": aliases, "models": models})
    aliases = [
        record
        for record in catalog.get("aliases", [])
        if (
            isinstance(record, dict)
            and model_allowed_by_client(client, str(record.get("id") or ""), record)
            and model_allowed_by_auth_permissions(auth, str(record.get("id") or ""), record, "read")
        )
    ]
    models = [
        record
        for record in catalog.get("models", [])
        if (
            isinstance(record, dict)
            and model_allowed_by_client(client, str(record.get("id") or ""), record)
            and model_allowed_by_auth_permissions(auth, str(record.get("id") or ""), record, "read")
        )
    ]
    return modelhub_policy.public_modelhub_metadata({**catalog, "aliases": aliases, "models": models})


def require_modelhub_client_network_allowed(client: dict[str, Any] | None, request: Request | None) -> None:
    if client is None:
        return
    cidr_allowlist = client.get("cidr_allowlist") or []
    if not cidr_allowlist:
        return
    remote_addr = client_host(request)
    if not modelhub_policy.client_ip_allowed_by_cidr(cidr_allowlist, remote_addr):
        raise HTTPException(status_code=403, detail="Model Hub client is not permitted from this network")


async def modelhub_client_for_auth(auth: AuthContext) -> dict[str, Any] | None:
    if auth.has_scope("*"):
        return None
    client = await database.get_modelhub_client_by_api_client(auth.subject_id)
    require_modelhub_client_network_allowed(client, current_request.get())
    return client


async def require_modelhub_model_authorized(auth: AuthContext, model_id: str, *, for_download: bool) -> dict[str, Any] | None:
    _, _, client = await authorized_modelhub_model_view(auth, model_id, for_download=for_download)
    return client


async def require_modelhub_blob_authorized(auth: AuthContext, sha256: str, accepted_license_refs: set[str] | None = None) -> None:
    digest = validate_modelhub_blob_sha256(sha256)
    records = downloadable_records_for_blob(digest)
    if not records:
        raise HTTPException(status_code=403, detail="blob is not downloadable by catalog policy")
    client = await modelhub_client_for_auth(auth)
    if client is not None and not client.get("allow_downloads", True):
        raise HTTPException(status_code=403, detail="Model Hub client is not permitted to download blobs")
    allowed_records = records
    if client is not None:
        allowed_records = [record for record in records if model_allowed_by_client(client, record["id"], record)]
    allowed_records = [record for record in allowed_records if model_allowed_by_auth_permissions(auth, record["id"], record, "download")]
    if not allowed_records:
        raise HTTPException(status_code=403, detail="Model Hub client is not permitted to download this blob")
    accepted = accepted_license_refs or set()
    if not any(modelhub_policy.record_license_acceptance_satisfied(record, accepted) for record in allowed_records):
        required_refs = sorted(filter(None, (modelhub_policy.model_ref_for_record(record) for record in allowed_records)))
        raise HTTPException(
            status_code=428,
            detail={
                "error": "licence acceptance is required before downloading this blob",
                "accepted_header": "X-B1-Accept-License",
                "required_model_refs": required_refs,
            },
        )


def validate_modelhub_allowed_models(allowed_models: list[str]) -> list[str]:
    try:
        return modelhub_policy.validate_allowed_models(allowed_models, catalog_snapshot().model_or_alias_record)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def validate_modelhub_cidr_allowlist(cidr_allowlist: list[str]) -> list[str]:
    try:
        return modelhub_policy.validate_cidr_allowlist(cidr_allowlist)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def backup_error_response(exc: Exception) -> HTTPException:
    if isinstance(exc, database.LogicalDumpError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, backup_restore.RestoreError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def artifact_retention_error_response(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def voicebox_sample_retention_error_response(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def model_lifecycle_error_response(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def backup_schedule_from_payload(payload: BackupScheduleUpdateRequest, auth: AuthContext, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(tz=UTC)
    try:
        values = backup_schedule.validate_schedule_values(
            enabled=payload.enabled,
            interval_hours=payload.interval_hours,
            keep_last=payload.keep_last,
            delete_older_than_days=payload.delete_older_than_days,
            label_prefix=payload.label_prefix,
        )
    except backup_schedule.BackupScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    values["next_run_at"] = current if payload.enabled and payload.run_immediately else backup_schedule.next_run_after(current, payload.interval_hours) if payload.enabled else None
    values["updated_by"] = auth.subject_id
    return values


def public_backup_schedule(row: dict[str, Any] | None) -> dict[str, Any]:
    source = "database" if row is not None else "default"
    public = dict(row or backup_schedule.default_schedule())
    return {
        "id": public["id"],
        "source": source,
        "enabled": public["enabled"],
        "interval_hours": public["interval_hours"],
        "keep_last": public["keep_last"],
        "delete_older_than_days": public.get("delete_older_than_days"),
        "label_prefix": public["label_prefix"],
        "next_run_at": public.get("next_run_at"),
        "last_started_at": public.get("last_started_at"),
        "last_completed_at": public.get("last_completed_at"),
        "last_status": public.get("last_status", "idle"),
        "last_backup_name": public.get("last_backup_name"),
        "failure_message": public.get("failure_message"),
        "updated_by": public.get("updated_by"),
        "created_at": public.get("created_at"),
        "updated_at": public.get("updated_at"),
    }


def public_update_plan(row: dict[str, Any]) -> dict[str, Any]:
    self_test = row.get("self_test") or {}
    return {
        "id": row["id"],
        "target_version": row["target_version"],
        "source_url": row.get("source_url") or "",
        "status": row["status"],
        "stage": row["stage"],
        "image_refs": row.get("image_refs") or [],
        "preflight": row.get("preflight") or {},
        "image_stage": row.get("image_stage") or [],
        "compose_override": row.get("compose_override") or {},
        "backup_name": row.get("backup_name"),
        "self_test": self_test,
        "update_health_gate": update_policy.update_health_gate(self_test),
        "promotion_result": row.get("promotion_result") or {},
        "rollback_result": row.get("rollback_result") or {},
        "notes": row.get("notes") or "",
        "failure_message": row.get("failure_message"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "staged_at": row.get("staged_at"),
        "health_checked_at": row.get("health_checked_at"),
        "promotion_requested_at": row.get("promotion_requested_at"),
        "rolled_back_at": row.get("rolled_back_at"),
    }


def update_plan_conflict(message: str, row: dict[str, Any] | None = None) -> HTTPException:
    detail: dict[str, Any] = {"message": message}
    if row is not None:
        detail["update"] = public_update_plan(row)
    return HTTPException(status_code=409, detail=detail)


def freeform_reason_metadata(reason: str | None) -> dict[str, bool]:
    return audit_policy.freeform_audit_field_summary("reason", reason)


def operational_reason_label(action: str, freeform_reason: str | None) -> str:
    normalized_action = " ".join(action.split()) or "administrative action"
    if (freeform_reason or "").strip():
        return f"{normalized_action} (operator reason provided)"
    return normalized_action


async def stage_update_images(row: dict[str, Any], payload: UpdateActionRequest) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for image_ref in row.get("image_refs") or []:
        service = str(image_ref.get("service") or "")
        image = str(image_ref.get("image") or "")
        agent_payload = {
            "image": image,
            "reason": operational_reason_label(f"stage update {row['id']}", payload.reason),
        }
        result, error = await runtime_agent_post(
            f"/v1/images/{service}/pull",
            agent_payload,
            timeout_seconds=max(30.0, float(payload.timeout_seconds)),
        )
        entry: dict[str, Any] = {
            "service": service,
            "image": image,
            "requested_at": datetime.now(tz=UTC).isoformat(),
        }
        if error is not None:
            entry.update({"status": "failed", "error": error[:500]})
            results.append(entry)
            continue
        entry.update(result or {"status": "unknown"})
        results.append(entry)
    return results


async def inspect_update_images_for_promotion(row: dict[str, Any], payload: UpdateActionRequest) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for image_ref in row.get("image_refs") or []:
        service = str(image_ref.get("service") or "")
        image = str(image_ref.get("image") or "")
        agent_payload = {
            "image": image,
            "reason": operational_reason_label(f"promote update {row['id']}", payload.reason),
        }
        result, error = await runtime_agent_post(
            f"/v1/images/{service}/inspect",
            agent_payload,
            timeout_seconds=max(30.0, float(payload.timeout_seconds)),
        )
        entry: dict[str, Any] = {
            "service": service,
            "image": image,
            "requested_at": datetime.now(tz=UTC).isoformat(),
        }
        if error is not None:
            entry.update({"status": "failed", "error": error[:500]})
            results.append(entry)
            continue
        entry.update(result or {"status": "unknown"})
        inspect_result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        if entry.get("status") != "ok" or not inspect_result.get("present") or not inspect_result.get("digest_verified"):
            entry["status"] = "failed"
            entry["error"] = "image is not present locally with the requested digest"
        results.append(entry)
    return results


async def create_control_plane_backup(label: str | None = None) -> dict[str, Any]:
    async with backup_operation_lock:
        postgres_dump = await database.export_logical_dump(data_root_path() / "data" / "control-plane" / "postgres-logical-export.json")
        native_dump = await asyncio.to_thread(
            backup_restore.export_native_postgres_dump,
            settings.database_url,
            data_root_path() / "data" / "control-plane" / "postgres-native.dump",
        )
        return await asyncio.to_thread(
            backup_restore.create_backup,
            data_root_path(),
            backup_root_path(),
            label,
            postgres_dump_path=Path(postgres_dump["path"]),
            postgres_dump_format=postgres_dump["format"],
            postgres_extra_dumps=[{**native_dump, "kind": "native", "verified": True}],
            backup_encryption_key=settings.master_key,
            backup_encryption_mode=settings.backup_encryption_mode,
        )


class BackupScheduleRunner:
    def __init__(self, interval_seconds: int = 60) -> None:
        self.interval_seconds = max(5, interval_seconds)
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # pragma: no cover - loop guard
                log_event("backup_schedule_runner_failed", error=exc.__class__.__name__)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def run_once(self, now: datetime | None = None) -> bool:
        if not settings.backup_scheduler_enabled:
            return False
        current = now or datetime.now(tz=UTC)
        schedule = await database.claim_due_backup_schedule(current)
        if schedule is None:
            return False
        label = backup_schedule.backup_label(schedule["label_prefix"], current)
        try:
            summary = await create_control_plane_backup(label)
            retention = await asyncio.to_thread(
                backup_restore.apply_backup_retention_plan,
                backup_root_path(),
                keep_last=int(schedule["keep_last"]),
                delete_older_than_days=schedule.get("delete_older_than_days"),
                confirmed=True,
            )
        except Exception as exc:
            next_run = backup_schedule.next_run_after(current, int(schedule["interval_hours"]))
            await database.update_backup_schedule_after_run(
                backup_schedule.SCHEDULE_ID,
                status="failed",
                next_run_at=next_run,
                failure_message=f"{exc.__class__.__name__}: {str(exc)[:500]}",
            )
            await database.insert_audit_event(
                {
                    "actor_id": "system:backup-scheduler",
                    "actor_role": Role.SERVICE.value,
                    "event_type": "backup.schedule_failed",
                    "target_type": "backup_schedule",
                    "target_id": backup_schedule.SCHEDULE_ID,
                    "summary": "Scheduled backup failed",
                    "metadata": {"error": exc.__class__.__name__, "next_run_at": next_run.isoformat()},
                }
            )
            return True
        next_run = backup_schedule.next_run_after(current, int(schedule["interval_hours"]))
        await database.update_backup_schedule_after_run(
            backup_schedule.SCHEDULE_ID,
            status="completed",
            next_run_at=next_run,
            backup_name=summary["name"],
        )
        await database.insert_audit_event(
            {
                "actor_id": "system:backup-scheduler",
                "actor_role": Role.SERVICE.value,
                "event_type": "backup.scheduled",
                "target_type": "backup",
                "target_id": summary["name"],
                "summary": f"Created scheduled backup {summary['name']}",
                "metadata": {
                    "file_count": summary["file_count"],
                    "postgres_dump_included": summary.get("postgres_dump_included"),
                    "postgres_native_dump_included": bool(summary.get("postgres_native_dump")),
                    "retention_deleted_count": retention.get("deleted_count"),
                    "next_run_at": next_run.isoformat(),
                },
            }
        )
        log_event("backup_scheduled", backup=summary["name"], next_run_at=next_run.isoformat())
        return True


async def runtime_agent_get(path: str) -> tuple[dict[str, Any] | None, str | None]:
    headers = {"Authorization": f"Bearer {settings.runtime_agent_token}"} if settings.runtime_agent_token else {}
    url = f"{settings.runtime_agent_url.rstrip('/')}/{path.lstrip('/')}"
    client_kwargs = runtime_agent_httpx_kwargs(
        settings.runtime_agent_url,
        ca_file=settings.runtime_agent_tls_ca_file,
        client_cert_file=settings.runtime_agent_tls_client_cert_file,
        client_key_file=settings.runtime_agent_tls_client_key_file,
        verify_tls=settings.runtime_agent_tls_verify,
    )
    try:
        async with httpx.AsyncClient(timeout=5.0, **client_kwargs) as client:
            response = await client.get(url, headers=headers)
        if response.status_code >= 400:
            return None, f"runtime-agent {path} returned HTTP {response.status_code}"
        return response.json(), None
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"runtime-agent {path} failed: {exc.__class__.__name__}"


async def runtime_agent_post(path: str, payload: dict[str, Any], timeout_seconds: float = 30.0) -> tuple[dict[str, Any] | None, str | None]:
    headers = {"Authorization": f"Bearer {settings.runtime_agent_token}"} if settings.runtime_agent_token else {}
    url = f"{settings.runtime_agent_url.rstrip('/')}/{path.lstrip('/')}"
    client_kwargs = runtime_agent_httpx_kwargs(
        settings.runtime_agent_url,
        ca_file=settings.runtime_agent_tls_ca_file,
        client_cert_file=settings.runtime_agent_tls_client_cert_file,
        client_key_file=settings.runtime_agent_tls_client_key_file,
        verify_tls=settings.runtime_agent_tls_verify,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, **client_kwargs) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            detail: Any
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            return None, f"runtime-agent {path} returned HTTP {response.status_code}: {detail}"
        return response.json(), None
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"runtime-agent {path} failed: {exc.__class__.__name__}"


@app.get("/admin/api-clients")
async def admin_api_clients(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_credential_admin(auth)
    return [public_api_client(client) for client in await database.list_api_clients()]


@app.post("/admin/api-clients")
async def admin_api_client_create(payload: ApiClientCreate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_credential_admin(auth)
    try:
        scopes = scopes_for_role(payload.role, payload.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cidr_allowlist = validate_modelhub_cidr_allowlist(payload.cidr_allowlist)
    default_b1_tools = normalize_model_tool_names(payload.default_b1_tools)
    key_prefix, api_key = generate_api_key()
    key_salt, key_digest = hash_api_key(api_key)
    row = await database.insert_api_client(
        {
            "id": f"client_{uuid.uuid4().hex}",
            "display_name": payload.display_name,
            "role": payload.role.value,
            "scopes": sorted(scopes),
            "key_prefix": key_prefix,
            "key_salt": key_salt,
            "key_hash": key_digest,
            "cidr_allowlist": cidr_allowlist,
            "default_b1_tools": default_b1_tools,
        }
    )
    log_event("api_client_created", client_id=row["id"], role=payload.role.value, key_prefix=key_prefix)
    await record_audit_event(
        auth,
        "api_client.created",
        target_type="api_client",
        target_id=row["id"],
        summary=f"Created API client {payload.display_name}",
        metadata={
            "display_name": payload.display_name,
            "role": payload.role.value,
            "scopes": sorted(scopes),
            "key_prefix": key_prefix,
            "cidr_allowlist": cidr_allowlist,
            "default_b1_tools": default_b1_tools,
        },
    )
    return {**public_api_client(row), "api_key": api_key, "one_time_display": True}


@app.put("/admin/api-clients/{client_id}/cidr-allowlist")
async def admin_api_client_cidr_update(
    client_id: str,
    payload: CidrAllowlistUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_credential_admin(auth)
    cidr_allowlist = validate_modelhub_cidr_allowlist(payload.cidr_allowlist)
    row = await database.update_api_client_cidr_allowlist(client_id, cidr_allowlist)
    if row is None:
        raise HTTPException(status_code=404, detail="API client not found")
    if row.get("revoked_at") is not None:
        raise HTTPException(status_code=409, detail="revoked API clients cannot be modified")
    log_event("api_client_cidr_updated", client_id=client_id, cidr_count=len(cidr_allowlist))
    await record_audit_event(
        auth,
        "api_client.cidr_allowlist_updated",
        target_type="api_client",
        target_id=client_id,
        summary=f"Updated API client CIDR allowlist for {row['display_name']}",
        metadata={
            "display_name": row["display_name"],
            "role": row["role"],
            "key_prefix": row["key_prefix"],
            "cidr_allowlist": cidr_allowlist,
        },
    )
    return public_api_client(row)


@app.put("/admin/api-clients/{client_id}/default-b1-tools")
async def admin_api_client_default_b1_tools_update(
    client_id: str,
    payload: ApiClientDefaultB1ToolsUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_credential_admin(auth)
    default_b1_tools = normalize_model_tool_names(payload.default_b1_tools)
    row = await database.update_api_client_default_b1_tools(client_id, default_b1_tools)
    if row is None:
        raise HTTPException(status_code=404, detail="API client not found")
    if row.get("revoked_at") is not None:
        raise HTTPException(status_code=409, detail="revoked API clients cannot be modified")
    log_event("api_client_default_b1_tools_updated", client_id=client_id, tool_count=len(default_b1_tools))
    await record_audit_event(
        auth,
        "api_client.default_b1_tools_updated",
        target_type="api_client",
        target_id=client_id,
        summary=f"Updated default B1 tools for API client {row['display_name']}",
        metadata={
            "display_name": row["display_name"],
            "role": row["role"],
            "key_prefix": row["key_prefix"],
            "default_b1_tools": default_b1_tools,
        },
    )
    return public_api_client(row)


@app.delete("/admin/api-clients/{client_id}")
async def admin_api_client_revoke(client_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_credential_admin(auth)
    row = await database.revoke_api_client(client_id)
    if row is None:
        raise HTTPException(status_code=404, detail="API client not found")
    log_event("api_client_revoked", client_id=client_id)
    await record_audit_event(
        auth,
        "api_client.revoked",
        target_type="api_client",
        target_id=client_id,
        summary=f"Revoked API client {row['display_name']}",
        metadata={"display_name": row["display_name"], "role": row["role"], "key_prefix": row["key_prefix"]},
    )
    return public_api_client(row)


@app.get("/admin/secrets")
async def admin_encrypted_secrets(
    authorization: str | None = Header(default=None),
    category: SecretCategory | None = Query(default=None),
    include_deleted: bool = Query(default=False),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_secret_admin(auth)
    rows = await database.list_encrypted_secrets(category=category, include_deleted=include_deleted)
    return {
        "object": "list",
        "master_key": secret_master_key_status(),
        "data": [public_encrypted_secret(row) for row in rows],
    }


@app.get("/admin/secrets/{name}")
async def admin_encrypted_secret_get(name: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_secret_admin(auth)
    normalized_name = normalize_secret_name_or_422(name)
    row = await database.get_encrypted_secret(normalized_name)
    if row is None:
        raise HTTPException(status_code=404, detail="encrypted secret not found")
    return public_encrypted_secret(row)


@app.put("/admin/secrets/{name}")
async def admin_encrypted_secret_set(
    name: str,
    payload: EncryptedSecretSetRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_secret_admin(auth)
    normalized_name = normalize_secret_name_or_422(name)
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="display_name cannot be blank")
    try:
        category = secret_store.validate_secret_category(payload.category)
        envelope = secret_store.encrypt_value(require_master_encryption_key(), normalized_name, payload.value)
    except secret_store.SecretStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = await database.upsert_encrypted_secret(
        {
            "name": normalized_name,
            "display_name": display_name,
            "category": category,
            "description": payload.description.strip(),
            "secret_envelope": envelope,
        }
    )
    public = public_encrypted_secret(row)
    await record_audit_event(
        auth,
        "encrypted_value.upserted",
        target_type="encrypted_value",
        target_id=normalized_name,
        summary=f"Stored encrypted value {normalized_name}",
        metadata={
            "name": normalized_name,
            "display_name": display_name,
            "category": category,
            "scheme": public.get("scheme"),
            "key_id": public.get("key_id"),
        },
    )
    return public


@app.post("/admin/secrets/{name}/verify")
async def admin_encrypted_secret_verify(name: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_secret_admin(auth)
    normalized_name = normalize_secret_name_or_422(name)
    row = await database.get_encrypted_secret(normalized_name)
    if row is None:
        raise HTTPException(status_code=404, detail="encrypted secret not found")
    try:
        secret_store.decrypt_value(require_master_encryption_key(), normalized_name, row.get("secret_envelope") or {})
    except secret_store.SecretStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    public = public_encrypted_secret(row)
    return {"status": "verified", **public}


@app.delete("/admin/secrets/{name}")
async def admin_encrypted_secret_delete(name: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_secret_admin(auth)
    normalized_name = normalize_secret_name_or_422(name)
    row = await database.delete_encrypted_secret(normalized_name)
    if row is None:
        raise HTTPException(status_code=404, detail="encrypted secret not found")
    public = public_encrypted_secret(row)
    await record_audit_event(
        auth,
        "encrypted_value.deleted",
        target_type="encrypted_value",
        target_id=normalized_name,
        summary=f"Deleted encrypted value {normalized_name}",
        metadata={
            "name": normalized_name,
            "display_name": public.get("display_name"),
            "category": public.get("category"),
            "deleted_at": public.get("deleted_at"),
        },
    )
    return public


def runner_reconciliation_name(runner: Any) -> str:
    if isinstance(runner, CpuJobRunner):
        return "cpu-job-runner"
    if isinstance(runner, GpuJobRunner):
        return "gpu-job-runner"
    if isinstance(runner, ModelDownloadRunner):
        return "model-download-runner"
    return runner.__class__.__name__


def scheduler_reconciliation_report() -> dict[str, Any]:
    required_runners: list[str] = []
    if settings.job_runner_enabled:
        required_runners.append("cpu-job-runner")
    if settings.gpu_job_runner_enabled:
        required_runners.append("gpu-job-runner")
    if settings.model_download_runner_enabled:
        required_runners.append("model-download-runner")

    records: list[dict[str, Any]] = []
    for runner in job_runners:
        record = getattr(runner, "startup_reconciliation", None)
        if not isinstance(record, dict):
            continue
        records.append({"runner": runner_reconciliation_name(runner), **record})

    recorded_names = {str(record.get("runner")) for record in records}
    missing_required = [name for name in required_runners if name not in recorded_names]
    incomplete = [
        str(record.get("runner"))
        for record in records
        if str(record.get("runner")) in required_runners and record.get("status") != "ok"
    ]
    status = "ok" if not missing_required and not incomplete else "degraded"
    return {
        "status": status,
        "control_plane_started_at": control_plane_started_at,
        "required_runners": required_runners,
        "missing_required_runners": missing_required,
        "comfyui_native_prompt_resume": comfyui_native_prompt_resume
        or {"checked": 0, "resumed": 0, "skipped": 0, "resumed_job_ids": [], "native_prompt_ids": []},
        "records": records,
    }


@app.get("/admin/scheduler/lease")
async def admin_scheduler_lease_get(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    row = await database.get_scheduler_owner()
    return {"lease": jsonable_encoder(row)}


@app.get("/admin/scheduler/reconciliation")
async def admin_scheduler_reconciliation_get(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    return jsonable_encoder(scheduler_reconciliation_report())


@app.post("/admin/scheduler/lease")
async def admin_scheduler_lease_acquire(payload: SchedulerLeaseRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    row = await database.acquire_scheduler_owner(payload.owner, payload.ttl_seconds)
    if not row.get("acquired"):
        return JSONResponse(status_code=409, content={"lease": jsonable_encoder(row), "message": "GPU scheduler lease is held by another owner"})
    await record_audit_event(
        auth,
        "scheduler_lease.acquired",
        target_type="scheduler_lease",
        target_id="gpu",
        summary=f"Acquired GPU scheduler lease for {payload.owner}",
        metadata={"owner": payload.owner, "ttl_seconds": payload.ttl_seconds, "epoch": row.get("epoch"), "lease_expires_at": row.get("lease_expires_at")},
    )
    return {"lease": jsonable_encoder(row)}


@app.get("/admin/runtime-reservations")
async def admin_runtime_reservations(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None, max_length=64),
    runtime: str | None = Query(default=None, max_length=64),
    owner_id: str | None = Query(default=None, max_length=128),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    require_runtime_admin(auth)
    rows = await database.list_runtime_reservations(limit=limit, owner_id=owner_id, status=status, runtime=runtime)
    return {"object": "list", "data": [public_runtime_reservation(row) for row in rows]}


@app.get("/admin/runtimes/external-config")
async def admin_external_runtime_configurations(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    require_administrator(auth, "external runtime configuration requires administrator role")
    cache = await refresh_runtime_configuration_cache()
    rows = {row["runtime"]: row for row in await database.list_runtime_configurations() if row["runtime"] in EXTERNAL_RUNTIME_NAMES}
    return {
        "object": "list",
        "allow_external_providers": settings.allow_external_providers,
        "data": [
            public_external_runtime_configuration(runtime, rows.get(runtime), cache)
            for runtime in sorted(EXTERNAL_RUNTIME_NAMES)
        ],
    }


@app.put("/admin/runtimes/external-config/{runtime}")
async def admin_external_runtime_configuration_set(
    runtime: str,
    payload: RuntimeExternalConfigRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    require_administrator(auth, "external runtime configuration requires administrator role")
    runtime_name = normalize_external_runtime_name(runtime)
    base_url = payload.base_url.strip()
    secret_name = payload.api_key_secret_name.strip() if isinstance(payload.api_key_secret_name, str) else ""
    if payload.enabled:
        if not payload.confirm_external_data:
            raise HTTPException(status_code=422, detail="confirm_external_data is required before enabling an external runtime")
        normalized_url, validation_error = validate_external_runtime_base_url(base_url)
        if validation_error:
            raise HTTPException(status_code=422, detail=validation_error)
        base_url = normalized_url
    else:
        base_url = ""

    if secret_name:
        try:
            secret_name = secret_store.validate_secret_name(secret_name)
        except secret_store.SecretStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        secret_row = await database.get_encrypted_secret(secret_name)
        if secret_row is None:
            raise HTTPException(status_code=422, detail="api_key_secret_name does not reference an active encrypted secret")
        if secret_row.get("category") != "remote-provider":
            raise HTTPException(status_code=422, detail="api_key_secret_name must reference an encrypted secret in category remote-provider")

    row = await database.upsert_runtime_configuration(
        {
            "runtime": runtime_name,
            "enabled": payload.enabled,
            "base_url": base_url,
            "api_key_secret_name": secret_name or None,
            "external_data_acknowledged": bool(payload.enabled and payload.confirm_external_data),
            "notes": payload.notes.strip(),
            "updated_by": auth.subject_id,
        }
    )
    cache = await refresh_runtime_configuration_cache()
    public = public_external_runtime_configuration(runtime_name, row, cache)
    await record_audit_event(
        auth,
        "runtime_external_config.upserted",
        target_type="runtime",
        target_id=runtime_name,
        summary=f"Updated external runtime configuration for {runtime_name}",
        metadata={
            "runtime": runtime_name,
            "enabled": public["enabled"],
            "configured": public["configured"],
            "eligible": public["eligible"],
            "status": public["status"],
            "api_key_configured": public["api_key_configured"],
            "external_data_acknowledged": public["external_data_acknowledged"],
            "configuration_error": public.get("configuration_error"),
        },
    )
    return public


@app.get("/admin/runtimes")
async def admin_runtimes(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    registry = runtime_registry_snapshot()
    adapters = [
        adapter
        for adapter in registry.adapters.values()
        if settings.allow_external_providers or not adapter.external
    ]
    health = await asyncio.gather(*(adapter.health() for adapter in adapters))
    runtime_states = {row["runtime"]: jsonable_encoder(row) for row in await database.list_runtime_states()}
    service_inventory, service_error = await runtime_agent_get("/v1/services")
    compose_selection = acceptance.compose_selection_snapshot()
    compose_readiness = selftest_policy.compose_selection_check(
        compose_selection,
        settings.runtime_deployment_mode,
    )
    lifecycle_checks = await asyncio.gather(
        self_test_localai_build_info(),
        self_test_localai_status(),
        self_test_audio_cpu_build_info(),
        self_test_audio_cpu_status(),
        self_test_voicebox_build_info(),
        self_test_voicebox_status(),
        self_test_comfyui_build_info(),
        self_test_comfyui_status(),
    )
    readiness = selftest_policy.runtime_production_readiness_check(
        health,
        settings.runtime_deployment_mode,
        settings.runtime_production_required,
        service_inventory,
    )
    return {
        "allow_external_providers": settings.allow_external_providers,
        "runtime_deployment_mode": settings.runtime_deployment_mode,
        "production_required_runtimes": list(settings.runtime_production_required),
        "readiness": readiness,
        "compose_selection": compose_selection,
        "compose_readiness": compose_readiness,
        "lifecycle_checks": lifecycle_checks,
        "runtime_agent_services": service_inventory or {"services": [], "error": service_error or "runtime-agent service inventory unavailable"},
        "adapters": registry.public_adapters(),
        "runtime_states": runtime_states,
        "health": sorted(
            [{**item, "runtime_state": runtime_states.get(item["name"])} for item in health],
            key=lambda item: item["name"],
        ),
    }


@app.get("/admin/services/{service}/logs")
async def admin_service_logs(
    service: str,
    authorization: str | None = Header(default=None),
    lines: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    require_runtime_admin(auth)
    service_name = normalize_service_log_name(service)
    query = urlencode({"lines": str(lines)})
    result, error = await runtime_agent_get(f"/v1/services/{service_name}/logs?{query}")
    if error is not None:
        raise HTTPException(status_code=502, detail=error)
    body = result or {}
    entries = body.get("entries")
    if not isinstance(entries, list):
        entries = []
    returned_lines = body.get("lines")
    if not isinstance(returned_lines, int):
        returned_lines = lines
    return {
        "service": service_name,
        "lines": max(1, min(500, returned_lines)),
        "entries": [redact_service_log_line(entry) for entry in entries[-lines:]],
    }


async def current_runtime_state(runtime: str) -> dict[str, Any] | None:
    list_states = getattr(database, "list_runtime_states", None)
    if list_states is None:
        return None
    rows = await list_states()
    for row in rows:
        if isinstance(row, dict) and row.get("runtime") == runtime:
            return dict(row)
    return None


def compact_runtime_action_result(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "unconfirmed"}
    allowed_keys = {"status", "reason", "action", "strategy", "runtime_action", "service", "runtime", "message", "error", "code"}
    return {key: value for key, value in result.items() if key in allowed_keys}


def runtime_action_status(result: dict[str, Any] | None) -> str:
    return str((result or {}).get("status") or "unconfirmed").strip().lower()


async def admin_graceful_runtime_unload(runtime: str) -> dict[str, Any] | None:
    state = await current_runtime_state(runtime)
    if not state or not (state.get("active_model") or state.get("resolved_model_version")):
        return {"status": "unconfirmed", "runtime": runtime, "action": "unload", "reason": "runtime_state_model_missing"}
    runner = runtime_control_runner()
    try:
        return await runner.post_runtime_control(
            runtime,
            "unload",
            runner.runtime_unload_payload(runtime, {"id": state.get("job_id") or f"admin-unload-{runtime}", "runtime": runtime}, state),
        )
    except Exception as exc:
        return {"status": "failed", "runtime": runtime, "action": "unload", "reason": "runtime_hook_failed", "error": exc.__class__.__name__}


async def record_confirmed_runtime_unload(runtime: str, result: dict[str, Any] | None, auth: AuthContext, reason: str) -> None:
    if runtime_action_status(result) != "ok":
        return
    await database.upsert_runtime_state(
        {
            "runtime": runtime,
            "status": "unload_ok",
            "stage": "idle_unloaded",
            "active_model": None,
            "model_alias": None,
            "resolved_model_version": None,
            "job_id": None,
            "details": {
                "source": "admin_runtime_action",
                "requested_by": auth.subject_id,
                **freeform_reason_metadata(reason),
                "hook": compact_runtime_action_result(result),
            },
        }
    )


async def admin_runtime_action(
    runtime: str,
    action: str,
    payload: RuntimeActionRequest,
    authorization: str | None,
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    adapter = runtime_registry_snapshot().adapter(runtime)
    if adapter is None:
        raise HTTPException(status_code=404, detail="runtime adapter is not configured")
    if adapter.external:
        raise HTTPException(status_code=422, detail="external runtime adapters cannot be mutated by runtime-agent")
    graceful_result: dict[str, Any] | None = None
    if action == "unload":
        graceful_result = await admin_graceful_runtime_unload(runtime)
    if action == "unload" and runtime_action_status(graceful_result) == "ok":
        agent_result, agent_error = graceful_result, None
    else:
        agent_result, agent_error = await runtime_agent_post(
            f"/v1/runtime-actions/{runtime}/{action}",
            {"reason": operational_reason_label(f"runtime {action} {runtime}", payload.reason), "timeout_seconds": payload.timeout_seconds},
            timeout_seconds=max(30.0, float(payload.timeout_seconds + 5)),
        )
        if agent_error is not None:
            raise HTTPException(status_code=502, detail=agent_error)
    if action == "unload":
        await record_confirmed_runtime_unload(runtime, agent_result, auth, payload.reason)
    await record_audit_event(
        auth,
        f"runtime.{action}_requested",
        target_type="runtime",
        target_id=runtime,
        summary=f"Requested {action} for runtime {runtime}",
        metadata={
            "runtime": runtime,
            "action": action,
            "timeout_seconds": payload.timeout_seconds,
            "runtime_agent_status": (agent_result or {}).get("status"),
            "strategy": (agent_result or {}).get("strategy"),
            "graceful_runtime_status": (graceful_result or {}).get("status") if graceful_result is not None else None,
            **freeform_reason_metadata(payload.reason),
        },
    )
    response = {"runtime": runtime, "action": action, "runtime_agent": agent_result}
    if graceful_result is not None:
        response["graceful_runtime"] = compact_runtime_action_result(graceful_result)
    return response


@app.post("/admin/runtimes/{runtime}/recover")
async def admin_runtime_recover(runtime: str, payload: RuntimeActionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return await admin_runtime_action(runtime, "recover", payload, authorization)


@app.post("/admin/runtimes/{runtime}/unload")
async def admin_runtime_unload(runtime: str, payload: RuntimeActionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return await admin_runtime_action(runtime, "unload", payload, authorization)


@app.get("/admin/voicebox/profiles")
async def admin_voice_profiles(
    authorization: str | None = Header(default=None),
    include_deleted: bool = Query(default=False),
    runtime: Literal["voicebox", "audio-cpu"] | None = Query(default=None),
    status: str | None = Query(default=None, max_length=64),
    owner_id: str | None = Query(default=None, max_length=128),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    require_runtime_admin(auth)
    rows = await database.list_voice_profiles(include_deleted=include_deleted, owner_id=owner_id, runtime=runtime, status=status)
    return {"object": "list", "data": [public_voice_profile(row) for row in rows]}


@app.get("/admin/voicebox/profile-policy")
async def admin_voice_profile_policy(authorization: str | None = Header(default=None)) -> VoiceProfilePolicyResponse:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    require_runtime_admin(auth)
    return {
        "object": "voicebox.profile_policy",
        "metadata_max_bytes": VOICE_PROFILE_MAX_METADATA_BYTES,
        "metadata_string_max_length": voice_profile_policy.VOICE_PROFILE_METADATA_STRING_MAX_LENGTH,
        "safe_metadata_fields": voice_profile_policy.metadata_policy_fields(),
        "forbidden_metadata_key_fragments": sorted(VOICE_PROFILE_FORBIDDEN_METADATA_KEYS),
        "sample_artifact_url_prefix": "/artifacts/voicebox/references/",
    }


@app.post(
    "/admin/voicebox/sample-artifacts",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "audio/wav": {"schema": {"type": "string", "format": "binary"}},
                "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
                "audio/ogg": {"schema": {"type": "string", "format": "binary"}},
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def admin_voicebox_sample_artifact_upload(
    request: Request,
    authorization: str | None = Header(default=None),
    x_b1_filename: str | None = Header(default=None),
) -> VoiceSampleArtifactUploadResponse:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    require_runtime_admin(auth)
    body = await read_bounded_request_body(request, settings.upload_max_bytes)
    enforce_artifact_storage_headroom(len(body))
    try:
        artifact = write_voice_profile_sample_artifact(
            owner_id=auth.subject_id,
            content=body,
            declared_mime_type=request.headers.get("content-type"),
            filename=x_b1_filename,
        )
    except ValueError as exc:
        raise staged_input_error(exc) from exc
    await record_audit_event(
        auth,
        "voice_profile.sample_uploaded",
        target_type="voice_profile_sample",
        target_id=artifact["sample_id"],
        summary="Uploaded Voicebox reference sample",
        metadata={
            "mime_type": artifact["mime_type"],
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
        },
    )
    return {"object": "voicebox.sample_artifact", "artifact": artifact}


@app.post("/admin/voicebox/sample-artifacts/retention-plan")
async def admin_voicebox_sample_retention_plan(payload: VoiceboxSampleRetentionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:read")
    require_runtime_admin(auth)
    try:
        return await asyncio.to_thread(
            voicebox_samples.build_voicebox_sample_retention_plan,
            artifact_root_path(),
            delete_older_than_days=payload.delete_older_than_days,
            protected_urls=await protected_artifact_urls(),
            limit=payload.limit,
        )
    except voicebox_samples.VoiceboxSampleRetentionError as exc:
        raise voicebox_sample_retention_error_response(exc) from exc


@app.post("/admin/voicebox/sample-artifacts/cleanup")
async def admin_voicebox_sample_cleanup(payload: VoiceboxSampleRetentionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    require_runtime_admin(auth)
    try:
        report = await asyncio.to_thread(
            voicebox_samples.apply_voicebox_sample_retention_plan,
            artifact_root_path(),
            delete_older_than_days=payload.delete_older_than_days,
            protected_urls=await protected_artifact_urls(),
            limit=payload.limit,
            confirmed=payload.confirm,
        )
    except voicebox_samples.VoiceboxSampleRetentionError as exc:
        raise voicebox_sample_retention_error_response(exc) from exc
    await record_audit_event(
        auth,
        "voice_profile.sample_cleanup",
        target_type="voice_profile_sample",
        summary="Applied Voicebox reference sample cleanup",
        metadata={
            "deleted_count": report.get("deleted_count"),
            "candidate_count": report.get("candidate_count"),
            "total_reclaimed_bytes": report.get("total_reclaimed_bytes"),
            "deleted_paths": [item.get("path") for item in (report.get("deleted") or [])[:50]],
        },
    )
    return report


@app.post("/admin/voicebox/profiles")
async def admin_voice_profile_create(payload: VoiceProfileCreate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    require_runtime_admin(auth)
    row = await database.insert_voice_profile(voice_profile_insert_payload(payload, auth))
    await record_audit_event(
        auth,
        "voice_profile.created",
        target_type="voice_profile",
        target_id=row["id"],
        summary=f"Created voice profile {row['display_name']}",
        metadata=voice_profile_audit_metadata(row),
    )
    return public_voice_profile(row)


@app.get("/admin/voicebox/profiles/{profile_id}")
async def admin_voice_profile_get(profile_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    require_runtime_admin(auth)
    row = await database.get_voice_profile(profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    return public_voice_profile(row)


@app.patch("/admin/voicebox/profiles/{profile_id}")
async def admin_voice_profile_update(
    profile_id: str,
    payload: VoiceProfileUpdate,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    require_runtime_admin(auth)
    current = await database.get_voice_profile(profile_id)
    if current is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    changes = voice_profile_update_payload(payload, current)
    row = await database.update_voice_profile(profile_id, changes)
    if row is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    await record_audit_event(
        auth,
        "voice_profile.updated",
        target_type="voice_profile",
        target_id=profile_id,
        summary=f"Updated voice profile {row['display_name']}",
        metadata={**voice_profile_audit_metadata(row), "changed_fields": sorted(changes)},
    )
    return public_voice_profile(row)


@app.post("/admin/voicebox/profiles/{profile_id}/export")
async def admin_voice_profile_export(profile_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    require_runtime_admin(auth)
    row = await database.get_voice_profile(profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    await record_audit_event(
        auth,
        "voice_profile.exported",
        target_type="voice_profile",
        target_id=profile_id,
        summary=f"Exported voice profile {row['display_name']}",
        metadata=voice_profile_audit_metadata(row),
    )
    return voice_profile_export_payload(row)


@app.delete("/admin/voicebox/profiles/{profile_id}")
async def admin_voice_profile_delete(profile_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    require_runtime_admin(auth)
    row = await database.delete_voice_profile(profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="voice profile not found")
    await record_audit_event(
        auth,
        "voice_profile.deleted",
        target_type="voice_profile",
        target_id=profile_id,
        summary=f"Deleted voice profile {row['display_name']}",
        metadata=voice_profile_audit_metadata(row),
    )
    return public_voice_profile(row)


def self_test_tls_verify_value() -> bool | str:
    if not settings.self_test_tls_verify:
        return False
    ca_path = Path(settings.self_test_tls_ca_file)
    if ca_path.is_file():
        return str(ca_path)
    return True


def self_test_tls_context(verify_value: bool | str) -> ssl.SSLContext:
    if verify_value is False:
        return ssl._create_unverified_context()
    if isinstance(verify_value, str):
        return ssl.create_default_context(cafile=verify_value)
    return ssl.create_default_context()


def self_test_http_target(parsed: Any) -> str:
    path = parsed.path or "/"
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


async def self_test_gateway_tls_probe(url: str, verify_value: bool | str) -> tuple[int, dict[str, str]]:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if parsed.scheme != "https" or not parsed.netloc or not hostname:
        raise ValueError("self-test route must be an HTTPS URL")
    connect_host = settings.self_test_tls_gateway_host.strip() or hostname
    connect_port = settings.self_test_tls_gateway_port if settings.self_test_tls_gateway_host.strip() else parsed.port or 443
    if connect_port <= 0 or connect_port > 65535:
        raise ValueError("self-test gateway port is invalid")

    context = self_test_tls_context(verify_value)
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(connect_host, connect_port, ssl=context, server_hostname=hostname),
            timeout=5.0,
        )
        request = (
            f"GET {self_test_http_target(parsed)} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(request.encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout=5.0)
        raw = b""
        while b"\r\n\r\n" not in raw and len(raw) <= 65536:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            if not chunk:
                break
            raw += chunk
        header_block, separator, _body = raw.partition(b"\r\n\r\n")
        if not separator:
            raise ValueError("TLS gateway response did not include complete HTTP headers")
        lines = header_block.decode("iso-8859-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)
        if len(status_parts) < 2 or not status_parts[1].isdigit():
            raise ValueError("TLS gateway response did not include a valid HTTP status")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, delimiter, value = line.partition(":")
            if not delimiter:
                continue
            normalized_name = name.strip()
            normalized_value = value.strip()
            if normalized_name in headers:
                headers[normalized_name] = f"{headers[normalized_name]}, {normalized_value}"
            else:
                headers[normalized_name] = normalized_value
        return int(status_parts[1]), headers
    finally:
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


async def self_test_httpx_tls_probe(client: httpx.AsyncClient, url: str) -> tuple[int, httpx.Headers]:
    response = await client.get(url, headers={"Accept": "application/json"})
    return response.status_code, response.headers


SELF_TEST_PUBLIC_HOST_KEYS = ("chat", "control", "media", "comfy", "voice", "models", "api")
ARTIFACT_DELIVERY_SELF_TEST_PAYLOAD = b"b1 artifact delivery self-test\n"
CADDY_CA_DOWNLOAD_FILENAME = "b1-ai-hub-caddy-root.crt"
CADDY_CA_MAX_BYTES = 1024 * 1024


def normalized_public_host(value: str) -> str:
    if not value.strip():
        return ""
    parsed = urlsplit(value if "://" in value else f"//{value}")
    hostname = parsed.hostname or value
    return hostname.strip().lower().rstrip(".")


def self_test_public_host_map() -> dict[str, str]:
    hosts = {
        "chat": settings.host_chat,
        "control": settings.host_control,
        "media": settings.host_media,
        "comfy": settings.host_comfy,
        "voice": settings.host_voice,
        "models": settings.host_models,
        "api": settings.host_api,
    }
    return {key: normalized_public_host(value) for key, value in hosts.items() if normalized_public_host(value)}


def caddy_internal_ca_path() -> Path:
    return Path(settings.caddy_internal_ca_file)


def caddy_tls_mode() -> str:
    return "internal" if settings.caddy_tls_args.strip().lower() == "internal" else "external-certificates"


def caddy_internal_ca_required() -> bool:
    return caddy_tls_mode() == "internal"


def read_regular_file_no_symlink(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("not a regular file")
        if file_stat.st_size > max_bytes:
            raise OSError("file exceeds maximum export size")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            content = handle.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(content) > max_bytes:
        raise OSError("file exceeds maximum export size")
    return content


def caddy_internal_ca_status_payload() -> dict[str, Any]:
    path = caddy_internal_ca_path()
    blockers: list[str] = []
    payload: dict[str, Any] = {
        "object": "caddy_internal_ca",
        "status": "missing" if caddy_internal_ca_required() else "not_required",
        "path": str(path),
        "tls_mode": caddy_tls_mode(),
        "required": caddy_internal_ca_required(),
        "download_url": None,
        "available": False,
        "readable": False,
        "regular_file": False,
        "symlink": False,
        "size_bytes": None,
        "sha256": None,
        "fingerprint_sha256": None,
        "modified_at": None,
        "hosts": self_test_public_host_map(),
        "trust_guidance": [
            "Download this root certificate only from the authenticated Control Center or admin API.",
            "Install it only on trusted LAN clients that need to reach the B1 AI Hub HTTPS virtual hosts.",
            "Compare the SHA-256 fingerprint before trusting the certificate.",
            "Do not install the root certificate on machines outside the LAN trust boundary.",
        ],
        "blockers": blockers,
    }
    if not caddy_internal_ca_required():
        payload["trust_guidance"] = [
            "Externally supplied Caddy certificates are configured, so B1 AI Hub does not offer an internal CA root for client trust.",
            "Distribute the certificate authority or public certificate chain through your existing LAN certificate-management process.",
        ]
        payload["blocker_count"] = 0
        return payload
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        blockers.append("Caddy internal CA root certificate has not been generated yet; start the gateway once with tls internal.")
        payload["blocker_count"] = len(blockers)
        return payload
    except OSError as exc:
        blockers.append(f"Caddy internal CA root certificate cannot be inspected: {exc.__class__.__name__}")
        payload["status"] = "blocked"
        payload["blocker_count"] = len(blockers)
        return payload
    payload["symlink"] = stat.S_ISLNK(stat_result.st_mode)
    if payload["symlink"]:
        blockers.append("Caddy internal CA root certificate path is a symlink; refusing browser/API export.")
    regular_file = stat.S_ISREG(stat_result.st_mode) and not payload["symlink"]
    payload["regular_file"] = regular_file
    if not regular_file:
        blockers.append("Caddy internal CA root certificate path is not a regular file.")
    payload["size_bytes"] = stat_result.st_size
    payload["modified_at"] = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC).isoformat()
    if regular_file and stat_result.st_size > CADDY_CA_MAX_BYTES:
        blockers.append("Caddy internal CA root certificate exceeds the maximum export size.")
    if regular_file and stat_result.st_size <= CADDY_CA_MAX_BYTES:
        try:
            content = read_regular_file_no_symlink(path, max_bytes=CADDY_CA_MAX_BYTES)
        except OSError as exc:
            blockers.append(f"Caddy internal CA root certificate is not readable: {exc.__class__.__name__}")
        else:
            digest = hashlib.sha256(content).hexdigest()
            payload.update(
                {
                    "status": "ok",
                    "available": True,
                    "readable": True,
                    "sha256": digest,
                    "fingerprint_sha256": ":".join(digest[index : index + 2].upper() for index in range(0, len(digest), 2)),
                    "download_url": "/admin/tls/caddy-ca/root.crt",
                }
            )
    if blockers:
        payload["status"] = "blocked"
        payload["available"] = False
        payload["download_url"] = None
    payload["blocker_count"] = len(blockers)
    return payload


async def self_test_caddy_internal_ca() -> dict[str, Any]:
    status = await asyncio.to_thread(caddy_internal_ca_status_payload)
    if not status.get("required"):
        return selftest_policy.check(
            "tls:caddy-ca",
            "ok",
            "external Caddy certificates configured; internal CA export is not required",
            status,
        )
    if status.get("available"):
        return selftest_policy.check("tls:caddy-ca", "ok", "Caddy internal CA root is exportable for trusted LAN clients", status)
    severity = "failed" if settings.runtime_deployment_mode == "production" else "warning"
    detail = "; ".join(status.get("blockers") or ["Caddy internal CA root is not exportable"])
    if severity == "warning":
        detail = f"{detail}; development mode permits bootstrapping only"
    return selftest_policy.check("tls:caddy-ca", severity, detail, status)


def self_test_route_keys(url: str) -> list[str]:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        return []
    host_map = self_test_public_host_map()
    return [key for key in SELF_TEST_PUBLIC_HOST_KEYS if host_map.get(key) == hostname]


async def self_test_tls_routing() -> dict[str, Any]:
    routes: list[dict[str, Any]] = []
    urls = list(settings.self_test_tls_urls)
    if not urls:
        return selftest_policy.check("tls:routing", "warning", "no TLS routing self-test URLs are configured")
    verify_value = self_test_tls_verify_value()
    gateway_host = settings.self_test_tls_gateway_host.strip()
    async with httpx.AsyncClient(timeout=5.0, verify=verify_value) as client:
        for url in urls:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.netloc:
                routes.append({"url": url, "status": "failed", "detail": "self-test route must be an HTTPS URL"})
                continue
            try:
                if gateway_host:
                    status_code, headers = await self_test_gateway_tls_probe(url, verify_value)
                else:
                    status_code, headers = await self_test_httpx_tls_probe(client, url)
            except (OSError, ValueError, TimeoutError, ssl.SSLError, httpx.HTTPError, asyncio.TimeoutError) as exc:
                routes.append({"url": url, "status": "failed", "detail": exc.__class__.__name__})
                continue
            header_failures = selftest_policy.gateway_security_header_failures(headers)
            route_ok = status_code < 400 and not header_failures
            route: dict[str, Any] = {
                "url": url,
                "route_keys": self_test_route_keys(url),
                "status": "ok" if route_ok else "failed",
                "http_status": status_code,
                "security_headers": "ok" if not header_failures else "failed",
            }
            if header_failures:
                route["header_failures"] = header_failures
            routes.append(route)
    failed = [route for route in routes if route["status"] != "ok"]
    return selftest_policy.check(
        "tls:routing",
        "failed" if failed else "ok",
        "TLS gateway routes and security headers checked" if not failed else "one or more TLS gateway route or security-header checks failed",
        {
            "routes": routes,
            "expected_route_keys": list(SELF_TEST_PUBLIC_HOST_KEYS),
            "verify_tls": settings.self_test_tls_verify,
            "gateway_connect_host": gateway_host or None,
            "gateway_connect_port": settings.self_test_tls_gateway_port if gateway_host else None,
            "ca_file": settings.self_test_tls_ca_file if Path(settings.self_test_tls_ca_file).is_file() else None,
        },
    )


async def self_test_tiny_inference(subject_id: str) -> dict[str, Any]:
    if not settings.self_test_tiny_inference_enabled:
        return selftest_policy.check("inference:tiny", "warning", "tiny inference self-test is disabled")
    try:
        resolution = resolve_catalog_alias("embedding-default", "embedding", operation="embeddings")
        response = await call_openai_runtime_json(
            "/v1/embeddings",
            {"model": "embedding-default", "input": "b1 self-test", "dimensions": 8},
            resolution,
            "self-test-embeddings",
            owner_id=subject_id,
        )
    except HTTPException as exc:
        return selftest_policy.check(
            "inference:tiny",
            "failed",
            f"tiny embedding inference failed with HTTP {exc.status_code}",
            {"detail": jsonable_encoder(exc.detail)},
        )
    if response is None:
        return selftest_policy.check("inference:tiny", "failed", "embedding runtime is not OpenAI-compatible")
    try:
        body = json.loads(response.body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return selftest_policy.check("inference:tiny", "failed", f"tiny embedding inference returned invalid JSON: {exc.__class__.__name__}")
    data = body.get("data") if isinstance(body, dict) else None
    embedding = data[0].get("embedding") if isinstance(data, list) and data and isinstance(data[0], dict) else None
    ok = response.status_code < 400 and isinstance(embedding, list) and len(embedding) == 8
    return selftest_policy.check(
        "inference:tiny",
        "ok" if ok else "failed",
        "tiny embedding inference completed" if ok else "tiny embedding inference did not return the expected vector",
        {
            "http_status": response.status_code,
            "runtime": resolution.runtime,
            "model": resolution.resolved_model_version,
            "placeholder": body.get("b1_placeholder") if isinstance(body, dict) else None,
        },
    )


async def self_test_runtime_unload() -> dict[str, Any]:
    runtime = settings.self_test_unload_runtime
    if not runtime:
        return selftest_policy.check("runtime-agent:unload", "warning", "runtime unload self-test is disabled")
    payload = {
        "reason": "self-test dry-run unload capability probe",
        "timeout_seconds": 5,
        "dry_run": True,
    }
    result, error = await runtime_agent_post(f"/v1/runtime-actions/{runtime}/unload", payload, timeout_seconds=10.0)
    if error:
        return selftest_policy.check("runtime-agent:unload", "degraded", error)
    if not result:
        return selftest_policy.check("runtime-agent:unload", "degraded", "runtime-agent returned no unload payload")
    ok = result.get("status") in {"dry_run", "ok"} and result.get("action") == "unload" and result.get("service") == runtime
    return selftest_policy.check(
        "runtime-agent:unload",
        "ok" if ok else "failed",
        "runtime unload action accepted in dry-run mode" if ok else "runtime-agent unload payload was unexpected",
        result,
    )


def comfyui_build_info_required() -> bool:
    return settings.runtime_deployment_mode == "production" and "comfyui" in set(settings.runtime_production_required)


def voicebox_build_info_required() -> bool:
    return settings.runtime_deployment_mode == "production" and "voicebox" in set(settings.runtime_production_required)


def localai_build_info_required() -> bool:
    return settings.runtime_deployment_mode == "production" and "localai" in set(settings.runtime_production_required)


def audio_cpu_build_info_required() -> bool:
    return settings.runtime_deployment_mode == "production" and "audio-cpu" in set(settings.runtime_production_required)


def voicebox_build_info_failure_status() -> str:
    return "failed" if voicebox_build_info_required() else "warning"


def localai_build_info_failure_status() -> str:
    return "failed" if localai_build_info_required() else "warning"


def audio_cpu_build_info_failure_status() -> str:
    return "failed" if audio_cpu_build_info_required() else "warning"


def comfyui_build_info_failure_status() -> str:
    return "failed" if comfyui_build_info_required() else "warning"


def localai_build_info_pinned(payload: dict[str, Any]) -> bool:
    upstream_commit = str(payload.get("upstream_commit") or "").strip().lower()
    upstream_image = str(payload.get("upstream_image") or "").strip().lower()
    return (
        payload.get("status") == "ok"
        and payload.get("runtime") == "localai"
        and payload.get("action") == "build-info"
        and payload.get("upstream") == "localai/localai"
        and payload.get("pinned") is True
        and re.fullmatch(r"[0-9a-f]{40}", upstream_commit) is not None
        and re.fullmatch(r".+@sha256:[0-9a-f]{64}", upstream_image) is not None
    )


def voicebox_build_info_pinned(payload: dict[str, Any]) -> bool:
    upstream_commit = str(payload.get("upstream_commit") or "").strip().lower()
    source_archive_sha256 = str(payload.get("source_archive_sha256") or "").strip().lower()
    return (
        payload.get("status") == "ok"
        and payload.get("runtime") == "voicebox"
        and payload.get("action") == "build-info"
        and payload.get("proxy") == "b1-voicebox-proxy"
        and payload.get("upstream_repository") == "jamiepine/voicebox"
        and payload.get("pinned") is True
        and re.fullmatch(r"[0-9a-f]{40}", upstream_commit) is not None
        and re.fullmatch(r"[0-9a-f]{64}", source_archive_sha256) is not None
    )


def audio_cpu_build_info_pinned(payload: dict[str, Any]) -> bool:
    base_image = str(payload.get("base_image") or "").strip().lower()
    piper_asset_sha256 = str(payload.get("piper_asset_sha256") or "").strip().lower()
    return (
        payload.get("status") == "ok"
        and payload.get("runtime") == "audio-cpu"
        and payload.get("action") == "build-info"
        and payload.get("component") == "b1-audio-cpu"
        and payload.get("pinned") is True
        and re.fullmatch(r".+@sha256:[0-9a-f]{64}", base_image) is not None
        and re.fullmatch(r"[0-9a-f]{64}", piper_asset_sha256) is not None
        and bool(str(payload.get("runtime_version") or "").strip())
        and bool(str(payload.get("piper_release") or "").strip())
    )


def comfyui_build_info_pinned(payload: dict[str, Any]) -> bool:
    upstream_commit = str(payload.get("upstream_commit") or "").strip().lower()
    source_archive_sha256 = str(payload.get("source_archive_sha256") or "").strip().lower()
    return (
        payload.get("status") == "ok"
        and payload.get("runtime") == "comfyui"
        and payload.get("action") == "build-info"
        and payload.get("pinned") is True
        and re.fullmatch(r"[0-9a-f]{40}", upstream_commit) is not None
        and re.fullmatch(r"[0-9a-f]{64}", source_archive_sha256) is not None
    )


def localai_status_payload_ok(payload: dict[str, Any]) -> bool:
    if payload.get("status") != "ok" or payload.get("runtime") != "localai" or payload.get("action") != "status":
        return False
    guardrails = payload.get("guardrails")
    model_probe = payload.get("model_probe")
    build_info = payload.get("build_info")
    capabilities = payload.get("capabilities")
    if not isinstance(guardrails, dict) or not isinstance(model_probe, dict):
        return False
    if guardrails.get("status") != "ok":
        return False
    if guardrails.get("max_active_backends") != 1:
        return False
    if guardrails.get("watchdog_idle") is not True or guardrails.get("force_eviction_when_busy") is not False:
        return False
    blockers = guardrails.get("blockers")
    if not isinstance(blockers, list) or blockers:
        return False
    if model_probe.get("status") not in {"ok", "unconfigured"}:
        return False
    if "model_count" not in model_probe:
        return False
    model_count = model_probe.get("model_count")
    if model_count is not None and (not isinstance(model_count, int) or model_count < 0):
        return False
    if any(key in model_probe for key in ("data", "models")):
        return False
    if not isinstance(build_info, dict) or not localai_build_info_pinned(build_info):
        return False
    if not isinstance(capabilities, dict):
        return False
    actions = capabilities.get("actions")
    if not isinstance(actions, list):
        return False
    action_names = {item for item in actions if isinstance(item, str)}
    return {"status", "build-info", "load", "warm", "smoke", "unload"}.issubset(action_names)


def voicebox_status_payload_ok(payload: dict[str, Any]) -> bool:
    if payload.get("status") != "ok" or payload.get("runtime") != "voicebox" or payload.get("action") != "status":
        return False
    process = payload.get("process")
    model_inventory = payload.get("model_inventory")
    build_info = payload.get("build_info")
    capabilities = payload.get("capabilities")
    active_requests = payload.get("active_requests")
    if not isinstance(process, dict) or process.get("running") is not True:
        return False
    if not isinstance(active_requests, int) or active_requests < 0:
        return False
    if not isinstance(model_inventory, dict):
        return False
    for key in ("root_count", "available_root_count", "entry_count"):
        value = model_inventory.get(key)
        if not isinstance(value, int) or value < 0:
            return False
    if any(key in model_inventory for key in ("entries", "models", "paths", "files")):
        return False
    if not isinstance(build_info, dict) or not voicebox_build_info_pinned(build_info):
        return False
    if not isinstance(capabilities, dict):
        return False
    actions = capabilities.get("actions")
    if not isinstance(actions, list):
        return False
    action_names = {item for item in actions if isinstance(item, str)}
    return {"status", "build-info", "load", "warm", "smoke", "unload"}.issubset(action_names)


def audio_cpu_status_payload_ok(payload: dict[str, Any]) -> bool:
    if payload.get("status") != "ok" or payload.get("runtime") != "audio-cpu" or payload.get("action") != "status":
        return False
    if payload.get("gpu_lease_required") is not False:
        return False
    capabilities = payload.get("capabilities")
    engines = payload.get("engines")
    placeholder = payload.get("placeholder")
    cpu_residency = payload.get("cpu_residency")
    build_info = payload.get("build_info")
    if not isinstance(capabilities, dict) or not isinstance(engines, dict) or not isinstance(placeholder, dict):
        return False
    actions = capabilities.get("actions")
    operations = capabilities.get("operations")
    if not isinstance(actions, list) or not isinstance(operations, dict):
        return False
    action_names = {item for item in actions if isinstance(item, str)}
    if not {"status", "build-info", "smoke", "unload"}.issubset(action_names):
        return False
    if operations.get("speech") is not True or operations.get("embeddings") is not True or operations.get("transcription") is not True:
        return False
    if placeholder.get("enabled") is not False:
        return False
    placeholder_operations = placeholder.get("operations")
    if not isinstance(placeholder_operations, list) or placeholder_operations:
        return False
    expected_engines = {"speech": "piper", "embeddings": "onnx", "transcription": "vosk"}
    forbidden_path_keys = {"binary", "model_path", "config_path", "tokenizer_path", "model_root"}
    for operation, expected_engine in expected_engines.items():
        probe = engines.get(operation)
        if not isinstance(probe, dict):
            return False
        if probe.get("engine") != expected_engine or probe.get("available") is not True or probe.get("placeholder") is True:
            return False
        if forbidden_path_keys.intersection(probe):
            return False
    if not isinstance(cpu_residency, dict):
        return False
    headroom = cpu_residency.get("headroom")
    if not isinstance(headroom, dict) or headroom.get("ok") is not True:
        return False
    if not isinstance(build_info, dict) or not audio_cpu_build_info_pinned(build_info):
        return False
    return True


def comfyui_status_payload_ok(payload: dict[str, Any]) -> bool:
    if payload.get("status") != "ok" or payload.get("runtime") != "comfyui" or payload.get("action") != "status":
        return False
    queue = payload.get("queue")
    memory = payload.get("memory")
    model_folders = payload.get("model_folders")
    build_info = payload.get("build_info")
    capabilities = payload.get("capabilities")
    if not isinstance(queue, dict) or not isinstance(memory, dict) or not isinstance(model_folders, dict):
        return False
    for key in ("running", "queued", "tasks_remaining"):
        value = queue.get(key)
        if not isinstance(value, int) or value < 0:
            return False
    if "available" not in memory or not isinstance(memory.get("available"), bool):
        return False
    folders = model_folders.get("folders")
    if not isinstance(folders, list):
        return False
    folder_count = model_folders.get("folder_count")
    file_count = model_folders.get("file_count")
    if not isinstance(folder_count, int) or folder_count < 0 or not isinstance(file_count, int) or file_count < 0:
        return False
    if folder_count != len(folders):
        return False
    if not isinstance(build_info, dict) or not comfyui_build_info_pinned(build_info):
        return False
    if not isinstance(capabilities, dict):
        return False
    actions = capabilities.get("actions")
    if not isinstance(actions, list):
        return False
    action_names = {item for item in actions if isinstance(item, str)}
    return {"status", "load", "warm", "smoke", "unload", "build-info"}.issubset(action_names)


async def self_test_voicebox_build_info() -> dict[str, Any]:
    if settings.runtime_deployment_mode == "production" and "voicebox" not in set(settings.runtime_production_required):
        return selftest_policy.check(
            "runtime:voicebox-build-info",
            "ok",
            "Voicebox is not required by the production runtime policy",
            {"required": False, "runtime": "voicebox"},
        )
    url = f"{settings.voicebox_url.rstrip('/')}/b1/runtime/build-info"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json={}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return selftest_policy.check(
            "runtime:voicebox-build-info",
            voicebox_build_info_failure_status(),
            f"Voicebox build-info hook is unreachable: {exc.__class__.__name__}",
            {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url},
        )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code in {404, 405}:
        return selftest_policy.check(
            "runtime:voicebox-build-info",
            voicebox_build_info_failure_status(),
            f"Voicebox build-info hook is not supported by the deployed runtime image: HTTP {response.status_code}",
            {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url, "http_status": response.status_code},
        )
    if response.status_code >= 400:
        return selftest_policy.check(
            "runtime:voicebox-build-info",
            voicebox_build_info_failure_status(),
            f"Voicebox build-info hook returned HTTP {response.status_code}",
            {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url, "http_status": response.status_code, "payload": payload},
        )
    if isinstance(payload, dict) and voicebox_build_info_pinned(payload):
        return selftest_policy.check(
            "runtime:voicebox-build-info",
            "ok",
            "Voicebox runtime reports pinned B1 proxy metadata",
            {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url, "http_status": response.status_code, "build_info": payload},
        )
    return selftest_policy.check(
        "runtime:voicebox-build-info",
        voicebox_build_info_failure_status(),
        "Voicebox build-info hook did not report pinned upstream commit and source archive SHA-256",
        {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url, "http_status": response.status_code, "build_info": payload},
    )


async def self_test_voicebox_status() -> dict[str, Any]:
    if settings.runtime_deployment_mode == "production" and "voicebox" not in set(settings.runtime_production_required):
        return selftest_policy.check(
            "runtime:voicebox-status",
            "ok",
            "Voicebox is not required by the production runtime policy",
            {"required": False, "runtime": "voicebox"},
        )
    url = f"{settings.voicebox_url.rstrip('/')}/b1/runtime/status"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json={}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return selftest_policy.check(
            "runtime:voicebox-status",
            voicebox_build_info_failure_status(),
            f"Voicebox status hook is unreachable: {exc.__class__.__name__}",
            {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url},
        )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code in {404, 405}:
        return selftest_policy.check(
            "runtime:voicebox-status",
            voicebox_build_info_failure_status(),
            f"Voicebox status hook is not supported by the deployed runtime image: HTTP {response.status_code}",
            {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url, "http_status": response.status_code},
        )
    if response.status_code >= 400:
        return selftest_policy.check(
            "runtime:voicebox-status",
            voicebox_build_info_failure_status(),
            f"Voicebox status hook returned HTTP {response.status_code}",
            {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url, "http_status": response.status_code, "payload": payload},
        )
    if isinstance(payload, dict) and voicebox_status_payload_ok(payload):
        return selftest_policy.check(
            "runtime:voicebox-status",
            "ok",
            "Voicebox runtime reports process, native-request, redacted model-inventory, and lifecycle capability status",
            {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url, "http_status": response.status_code, "status": payload},
        )
    return selftest_policy.check(
        "runtime:voicebox-status",
        voicebox_build_info_failure_status(),
        "Voicebox status hook did not report required process, redacted model-inventory, lifecycle, and pinned build metadata",
        {"required": voicebox_build_info_required(), "runtime": "voicebox", "url": url, "http_status": response.status_code, "status": payload},
    )


async def self_test_audio_cpu_build_info() -> dict[str, Any]:
    if settings.runtime_deployment_mode == "production" and "audio-cpu" not in set(settings.runtime_production_required):
        return selftest_policy.check(
            "runtime:audio-cpu-build-info",
            "ok",
            "audio-cpu is not required by the production runtime policy",
            {"required": False, "runtime": "audio-cpu"},
        )
    url = f"{settings.audio_cpu_url.rstrip('/')}/b1/runtime/build-info"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json={}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return selftest_policy.check(
            "runtime:audio-cpu-build-info",
            audio_cpu_build_info_failure_status(),
            f"audio-cpu build-info hook is unreachable: {exc.__class__.__name__}",
            {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url},
        )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code in {404, 405}:
        return selftest_policy.check(
            "runtime:audio-cpu-build-info",
            audio_cpu_build_info_failure_status(),
            f"audio-cpu build-info hook is not supported by the deployed runtime image: HTTP {response.status_code}",
            {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url, "http_status": response.status_code},
        )
    if response.status_code >= 400:
        return selftest_policy.check(
            "runtime:audio-cpu-build-info",
            audio_cpu_build_info_failure_status(),
            f"audio-cpu build-info hook returned HTTP {response.status_code}",
            {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url, "http_status": response.status_code, "payload": payload},
        )
    if isinstance(payload, dict) and audio_cpu_build_info_pinned(payload):
        return selftest_policy.check(
            "runtime:audio-cpu-build-info",
            "ok",
            "audio-cpu runtime reports pinned B1 image and Piper metadata",
            {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url, "http_status": response.status_code, "build_info": payload},
        )
    return selftest_policy.check(
        "runtime:audio-cpu-build-info",
        audio_cpu_build_info_failure_status(),
        "audio-cpu build-info hook did not report pinned base image and Piper asset metadata",
        {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url, "http_status": response.status_code, "build_info": payload},
    )


async def self_test_audio_cpu_status() -> dict[str, Any]:
    if settings.runtime_deployment_mode == "production" and "audio-cpu" not in set(settings.runtime_production_required):
        return selftest_policy.check(
            "runtime:audio-cpu-status",
            "ok",
            "audio-cpu is not required by the production runtime policy",
            {"required": False, "runtime": "audio-cpu"},
        )
    url = f"{settings.audio_cpu_url.rstrip('/')}/b1/runtime/status"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json={}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return selftest_policy.check(
            "runtime:audio-cpu-status",
            audio_cpu_build_info_failure_status(),
            f"audio-cpu status hook is unreachable: {exc.__class__.__name__}",
            {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url},
        )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code in {404, 405}:
        return selftest_policy.check(
            "runtime:audio-cpu-status",
            audio_cpu_build_info_failure_status(),
            f"audio-cpu status hook is not supported by the deployed runtime image: HTTP {response.status_code}",
            {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url, "http_status": response.status_code},
        )
    if response.status_code >= 400:
        return selftest_policy.check(
            "runtime:audio-cpu-status",
            audio_cpu_build_info_failure_status(),
            f"audio-cpu status hook returned HTTP {response.status_code}",
            {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url, "http_status": response.status_code, "payload": payload},
        )
    if isinstance(payload, dict) and audio_cpu_status_payload_ok(payload):
        return selftest_policy.check(
            "runtime:audio-cpu-status",
            "ok",
            "audio-cpu runtime reports real CPU engines, redacted model probes, and residency headroom",
            {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url, "http_status": response.status_code, "status": payload},
        )
    return selftest_policy.check(
        "runtime:audio-cpu-status",
        audio_cpu_build_info_failure_status(),
        "audio-cpu status hook did not report real non-placeholder CPU engines, redacted probes, residency headroom, and pinned build metadata",
        {"required": audio_cpu_build_info_required(), "runtime": "audio-cpu", "url": url, "http_status": response.status_code, "status": payload},
    )


async def self_test_localai_build_info() -> dict[str, Any]:
    if settings.runtime_deployment_mode == "production" and "localai" not in set(settings.runtime_production_required):
        return selftest_policy.check(
            "runtime:localai-build-info",
            "ok",
            "LocalAI is not required by the production runtime policy",
            {"required": False, "runtime": "localai"},
        )
    url = f"{settings.localai_url.rstrip('/')}/b1/runtime/build-info"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json={}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return selftest_policy.check(
            "runtime:localai-build-info",
            localai_build_info_failure_status(),
            f"LocalAI build-info hook is unreachable: {exc.__class__.__name__}",
            {"required": localai_build_info_required(), "runtime": "localai", "url": url},
        )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code in {404, 405}:
        return selftest_policy.check(
            "runtime:localai-build-info",
            localai_build_info_failure_status(),
            f"LocalAI build-info hook is not supported by the deployed runtime image: HTTP {response.status_code}",
            {"required": localai_build_info_required(), "runtime": "localai", "url": url, "http_status": response.status_code},
        )
    if response.status_code >= 400:
        return selftest_policy.check(
            "runtime:localai-build-info",
            localai_build_info_failure_status(),
            f"LocalAI build-info hook returned HTTP {response.status_code}",
            {"required": localai_build_info_required(), "runtime": "localai", "url": url, "http_status": response.status_code, "payload": payload},
        )
    if isinstance(payload, dict) and localai_build_info_pinned(payload):
        return selftest_policy.check(
            "runtime:localai-build-info",
            "ok",
            "LocalAI runtime reports pinned B1 wrapper metadata",
            {"required": localai_build_info_required(), "runtime": "localai", "url": url, "http_status": response.status_code, "build_info": payload},
        )
    return selftest_policy.check(
        "runtime:localai-build-info",
        localai_build_info_failure_status(),
        "LocalAI build-info hook did not report pinned upstream commit and image digest metadata",
        {"required": localai_build_info_required(), "runtime": "localai", "url": url, "http_status": response.status_code, "build_info": payload},
    )


async def self_test_localai_status() -> dict[str, Any]:
    if settings.runtime_deployment_mode == "production" and "localai" not in set(settings.runtime_production_required):
        return selftest_policy.check(
            "runtime:localai-status",
            "ok",
            "LocalAI is not required by the production runtime policy",
            {"required": False, "runtime": "localai"},
        )
    url = f"{settings.localai_url.rstrip('/')}/b1/runtime/status"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json={}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return selftest_policy.check(
            "runtime:localai-status",
            localai_build_info_failure_status(),
            f"LocalAI status hook is unreachable: {exc.__class__.__name__}",
            {"required": localai_build_info_required(), "runtime": "localai", "url": url},
        )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code in {404, 405}:
        return selftest_policy.check(
            "runtime:localai-status",
            localai_build_info_failure_status(),
            f"LocalAI status hook is not supported by the deployed runtime image: HTTP {response.status_code}",
            {"required": localai_build_info_required(), "runtime": "localai", "url": url, "http_status": response.status_code},
        )
    if response.status_code >= 400:
        return selftest_policy.check(
            "runtime:localai-status",
            localai_build_info_failure_status(),
            f"LocalAI status hook returned HTTP {response.status_code}",
            {"required": localai_build_info_required(), "runtime": "localai", "url": url, "http_status": response.status_code, "payload": payload},
        )
    if isinstance(payload, dict) and localai_status_payload_ok(payload):
        return selftest_policy.check(
            "runtime:localai-status",
            "ok",
            "LocalAI runtime reports one-backend guardrails, redacted model-count probing, and lifecycle capability status",
            {"required": localai_build_info_required(), "runtime": "localai", "url": url, "http_status": response.status_code, "status": payload},
        )
    return selftest_policy.check(
        "runtime:localai-status",
        localai_build_info_failure_status(),
        "LocalAI status hook did not report required one-backend guardrails, redacted model-count probing, lifecycle capabilities, and pinned build metadata",
        {"required": localai_build_info_required(), "runtime": "localai", "url": url, "http_status": response.status_code, "status": payload},
    )


async def self_test_comfyui_build_info() -> dict[str, Any]:
    if settings.runtime_deployment_mode == "production" and "comfyui" not in set(settings.runtime_production_required):
        return selftest_policy.check(
            "runtime:comfyui-build-info",
            "ok",
            "ComfyUI is not required by the production runtime policy",
            {"required": False, "runtime": "comfyui"},
        )
    url = f"{settings.comfyui_url.rstrip('/')}/b1/runtime/build-info"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json={}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return selftest_policy.check(
            "runtime:comfyui-build-info",
            comfyui_build_info_failure_status(),
            f"ComfyUI build-info hook is unreachable: {exc.__class__.__name__}",
            {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url},
        )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code in {404, 405}:
        return selftest_policy.check(
            "runtime:comfyui-build-info",
            comfyui_build_info_failure_status(),
            f"ComfyUI build-info hook is not supported by the deployed runtime image: HTTP {response.status_code}",
            {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url, "http_status": response.status_code},
        )
    if response.status_code >= 400:
        return selftest_policy.check(
            "runtime:comfyui-build-info",
            comfyui_build_info_failure_status(),
            f"ComfyUI build-info hook returned HTTP {response.status_code}",
            {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url, "http_status": response.status_code, "payload": payload},
        )
    if isinstance(payload, dict) and comfyui_build_info_pinned(payload):
        return selftest_policy.check(
            "runtime:comfyui-build-info",
            "ok",
            "ComfyUI runtime reports pinned B1 build metadata",
            {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url, "http_status": response.status_code, "build_info": payload},
        )
    return selftest_policy.check(
        "runtime:comfyui-build-info",
        comfyui_build_info_failure_status(),
        "ComfyUI build-info hook did not report pinned upstream commit and source archive SHA-256",
        {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url, "http_status": response.status_code, "build_info": payload},
    )


async def self_test_comfyui_status() -> dict[str, Any]:
    if settings.runtime_deployment_mode == "production" and "comfyui" not in set(settings.runtime_production_required):
        return selftest_policy.check(
            "runtime:comfyui-status",
            "ok",
            "ComfyUI is not required by the production runtime policy",
            {"required": False, "runtime": "comfyui"},
        )
    url = f"{settings.comfyui_url.rstrip('/')}/b1/runtime/status"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json={}, headers=runtime_control_headers())
    except httpx.HTTPError as exc:
        return selftest_policy.check(
            "runtime:comfyui-status",
            comfyui_build_info_failure_status(),
            f"ComfyUI status hook is unreachable: {exc.__class__.__name__}",
            {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url},
        )
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    if response.status_code in {404, 405}:
        return selftest_policy.check(
            "runtime:comfyui-status",
            comfyui_build_info_failure_status(),
            f"ComfyUI status hook is not supported by the deployed runtime image: HTTP {response.status_code}",
            {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url, "http_status": response.status_code},
        )
    if response.status_code >= 400:
        return selftest_policy.check(
            "runtime:comfyui-status",
            comfyui_build_info_failure_status(),
            f"ComfyUI status hook returned HTTP {response.status_code}",
            {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url, "http_status": response.status_code, "payload": payload},
        )
    if isinstance(payload, dict) and comfyui_status_payload_ok(payload):
        return selftest_policy.check(
            "runtime:comfyui-status",
            "ok",
            "ComfyUI runtime reports queue, memory, model-folder, and lifecycle capability status",
            {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url, "http_status": response.status_code, "status": payload},
        )
    return selftest_policy.check(
        "runtime:comfyui-status",
        comfyui_build_info_failure_status(),
        "ComfyUI status hook did not report the required queue, memory, model-folder, lifecycle, and pinned build metadata",
        {"required": comfyui_build_info_required(), "runtime": "comfyui", "url": url, "http_status": response.status_code, "status": payload},
    )


async def self_test_artifact_delivery() -> dict[str, Any]:
    artifact_root = Path(settings.artifact_root)
    relative_path = f"temporary/self-test-{uuid.uuid4().hex}.txt"
    target = artifact_root / relative_path
    payload = ARTIFACT_DELIVERY_SELF_TEST_PAYLOAD
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    expected_etag = f'"sha256:{payload_sha256}"'
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        url = f"{settings.artifact_base_url.rstrip('/')}/artifacts/{relative_path}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers={"Range": "bytes=0-1", **artifact_server_auth_headers()})
    except HTTPException as exc:
        detail = jsonable_encoder(exc.detail)
        return selftest_policy.check(
            "artifact:delivery",
            "failed",
            f"artifact delivery probe failed with HTTP {exc.status_code}: {detail}",
            {"detail": detail},
        )
    except (OSError, httpx.HTTPError) as exc:
        return selftest_policy.check("artifact:delivery", "failed", f"artifact delivery probe failed: {exc.__class__.__name__}")
    finally:
        with suppress(FileNotFoundError, OSError):
            target.unlink()
    response_etag = response.headers.get("etag")
    response_checksum = response.headers.get("x-checksum-sha256")
    content_range = response.headers.get("content-range")
    content_length = response.headers.get("content-length")
    etag_matches = response_etag == expected_etag
    checksum_matches = response_checksum == payload_sha256
    ok = response.status_code == 206 and response.content == payload[:2] and etag_matches and checksum_matches
    return selftest_policy.check(
        "artifact:delivery",
        "ok" if ok else "failed",
        "artifact-server range delivery and checksum contract completed"
        if ok
        else "artifact-server range delivery returned an unexpected response or checksum metadata",
        {
            "http_status": response.status_code,
            "content_range": content_range,
            "content_length": content_length,
            "probe_sha256": payload_sha256,
            "expected_etag": expected_etag,
            "response_etag": response_etag,
            "response_checksum_sha256": response_checksum,
            "etag_matches_sha256": etag_matches,
            "checksum_matches_sha256": checksum_matches,
        },
    )


async def self_test_published_workflows() -> dict[str, Any]:
    try:
        rows = await database.list_workflows()
        workflows = [public_workflow(row) for row in rows]
    except Exception as exc:
        severity = "failed" if settings.runtime_deployment_mode == "production" else "warning"
        detail = f"published workflow readiness could not be inspected: {exc.__class__.__name__}"
        if severity == "warning":
            detail += "; development mode permits bootstrapping only"
        return selftest_policy.check("workflows:starter-readiness", severity, detail)
    return selftest_policy.starter_workflow_readiness_check(workflows, settings.runtime_deployment_mode)


async def run_operator_self_test_probes(subject_id: str) -> list[dict[str, Any]]:
    return await asyncio.gather(
        self_test_caddy_internal_ca(),
        self_test_tls_routing(),
        self_test_tiny_inference(subject_id),
        self_test_published_workflows(),
        self_test_runtime_unload(),
        self_test_localai_build_info(),
        self_test_localai_status(),
        self_test_audio_cpu_build_info(),
        self_test_audio_cpu_status(),
        self_test_voicebox_build_info(),
        self_test_voicebox_status(),
        self_test_comfyui_build_info(),
        self_test_comfyui_status(),
        self_test_artifact_delivery(),
    )


async def build_self_test_report(subject_id: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    try:
        checks.append(selftest_policy.check("database", "ok" if await database.ping() else "failed", "PostgreSQL ping completed"))
    except Exception as exc:
        checks.append(selftest_policy.check("database", "failed", f"PostgreSQL ping failed: {exc.__class__.__name__}"))

    try:
        redis_ok = redis_client is not None and bool(await redis_client.ping())
        checks.append(selftest_policy.check("redis", "ok" if redis_ok else "failed", "Redis ping completed" if redis_ok else "Redis client is not ready"))
    except Exception as exc:
        checks.append(selftest_policy.check("redis", "failed", f"Redis ping failed: {exc.__class__.__name__}"))

    storage_paths = {
        "data-root": data_root_path(),
        "control-plane-data": data_root_path() / "data" / "control-plane",
        "artifact-temporary": Path(settings.artifact_root) / "temporary",
        "backup-root": backup_root_path(),
        "restore-test-root": restore_test_root_path(),
    }
    checks.append(await asyncio.to_thread(selftest_policy.check_directory_exists, "storage:data-root", storage_paths["data-root"]))
    for name in ("control-plane-data", "artifact-temporary", "backup-root", "restore-test-root"):
        checks.append(await asyncio.to_thread(selftest_policy.check_writable_directory, f"storage:{name}", storage_paths[name]))

    registry = runtime_registry_snapshot()
    adapters = [
        adapter
        for adapter in registry.adapters.values()
        if settings.allow_external_providers or not adapter.external
    ]
    runtime_health = await asyncio.gather(*(adapter.health() for adapter in adapters))
    runtime_status = "ok" if all(item.get("status") == "ok" for item in runtime_health) else "degraded"
    checks.append(selftest_policy.check("runtimes", runtime_status, "runtime health probes completed", {"health": runtime_health}))
    agent_status, agent_error = await runtime_agent_get("/v1/status")
    service_inventory, service_error = await runtime_agent_get("/v1/services")
    checks.append(selftest_policy.check_http_result("runtime-agent:status", agent_status, agent_error))
    checks.append(selftest_policy.runtime_agent_mutation_guard_check(agent_status))
    checks.append(selftest_policy.check_http_result("runtime-agent:services", service_inventory, service_error))
    checks.append(
        selftest_policy.compose_selection_check(
            acceptance.compose_selection_snapshot(),
            settings.runtime_deployment_mode,
        )
    )
    checks.append(
        selftest_policy.runtime_production_readiness_check(
            runtime_health,
            settings.runtime_deployment_mode,
            settings.runtime_production_required,
            service_inventory,
        )
    )

    agent_metrics, metrics_error = await runtime_agent_get("/v1/metrics")
    checks.append(selftest_policy.check_http_result("runtime-agent:metrics", agent_metrics, metrics_error))
    if agent_metrics and not agent_metrics.get("gpu", {}).get("available", False):
        checks.append(selftest_policy.check("gpu:nvml", "warning", agent_metrics.get("gpu", {}).get("error", "GPU metrics are unavailable")))
    elif agent_metrics:
        checks.append(selftest_policy.check("gpu:nvml", "ok", "GPU metrics are available", agent_metrics.get("gpu", {})))
    checks.append(
        selftest_policy.hardware_resource_policy_check(
            agent_metrics,
            resource_policy_dict(resource_policy()),
            settings.runtime_deployment_mode,
        )
    )

    checks.extend(await run_operator_self_test_probes(subject_id))

    return {
        "status": selftest_policy.summarize(checks),
        "subject_id": subject_id,
        "checks": checks,
    }


def acceptance_report_root_path() -> Path:
    return acceptance.acceptance_root(backup_root_path())


async def build_acceptance_report_snapshot(auth: AuthContext, payload: AcceptanceReportCreate) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    service_inventory, service_error = await runtime_agent_get("/v1/services")
    deployment = service_inventory or {"services": [], "error": service_error or "runtime-agent service inventory unavailable"}
    backup_root = backup_root_path()
    cutover_preservation, live_evidence = await asyncio.gather(
        asyncio.to_thread(acceptance.latest_cutover_preservation_snapshot, backup_root),
        asyncio.to_thread(acceptance.latest_live_evidence_snapshot, backup_root),
    )
    report = acceptance.build_report(
        report_id=acceptance.new_report_id(now),
        created_by=auth.subject_id,
        label=payload.label,
        notes=payload.notes,
        generated_at=now,
        runtime_deployment_mode=settings.runtime_deployment_mode,
        resource_policy=resource_policy_dict(resource_policy()),
        maintenance=current_maintenance_state(),
        self_test=await build_self_test_report(auth.subject_id),
        metrics=await build_admin_metrics_payload(limit=500),
        admission=await admission_report(auth.subject_id),
        scheduler_lease=jsonable_encoder(await database.get_scheduler_owner()),
        runtime_states=jsonable_encoder(await database.list_runtime_states()),
        runtime_reservations=[
            public_runtime_reservation(row)
            for row in await database.list_runtime_reservations(limit=50, status="active")
        ],
        deployment=deployment,
        deployment_pins=acceptance.deployment_pins_snapshot(Path.cwd()),
        compose_selection=acceptance.compose_selection_snapshot(),
        recent_updates=[public_update_plan(row) for row in await database.list_update_plans(limit=5)],
        source_control=acceptance.source_control_snapshot(Path.cwd()),
        operator_evidence=payload.operator_evidence,
        operator_evidence_notes=payload.operator_evidence_notes,
        model_measurement_coverage=await acceptance_model_measurement_coverage_snapshot(),
        cutover_preservation=cutover_preservation,
        live_evidence=live_evidence,
        handoff=acceptance.build_handoff_context(
            hosts={
                "chat": settings.host_chat,
                "control": settings.host_control,
                "media": settings.host_media,
                "comfy": settings.host_comfy,
                "voice": settings.host_voice,
                "models": settings.host_models,
                "api": settings.host_api,
            },
            data_root=settings.data_root,
        ),
    )
    return jsonable_encoder(report)


@app.get("/admin/self-test")
async def admin_self_test(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    return await build_self_test_report(auth.subject_id)


@app.get("/admin/tls/caddy-ca")
async def admin_caddy_internal_ca_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    return await asyncio.to_thread(caddy_internal_ca_status_payload)


@app.get(
    "/admin/tls/caddy-ca/root.crt",
    response_class=Response,
    responses={
        200: {
            "description": "Caddy internal CA root certificate",
            "content": {"application/x-x509-ca-cert": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def admin_caddy_internal_ca_root(authorization: str | None = Header(default=None)) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    status = await asyncio.to_thread(caddy_internal_ca_status_payload)
    if not status.get("available"):
        detail = "; ".join(status.get("blockers") or ["Caddy internal CA root certificate is not available"])
        raise HTTPException(status_code=404 if status.get("status") == "missing" else 409, detail=detail)
    path = caddy_internal_ca_path()
    try:
        content = await asyncio.to_thread(read_regular_file_no_symlink, path, max_bytes=CADDY_CA_MAX_BYTES)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"Caddy internal CA root certificate cannot be read: {exc.__class__.__name__}") from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != status.get("sha256"):
        raise HTTPException(status_code=409, detail="Caddy internal CA root certificate changed during export; retry the download")
    return Response(
        content=content,
        media_type="application/x-x509-ca-cert",
        headers={
            "Content-Disposition": f'attachment; filename="{CADDY_CA_DOWNLOAD_FILENAME}"',
            "ETag": f'"sha256:{digest}"',
            "Cache-Control": "private, no-store",
            "X-B1-SHA256": digest,
        },
    )


@app.get("/admin/acceptance-reports")
async def admin_acceptance_reports(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "acceptance reports require administrator role")
    return {"object": "list", "data": acceptance.list_reports(acceptance_report_root_path(), limit=limit)}


@app.post("/admin/acceptance-reports/preview")
async def admin_acceptance_report_preview(
    payload: AcceptanceReportCreate,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "acceptance report preview requires administrator role")
    report = await build_acceptance_report_snapshot(auth, payload)
    return {"summary": acceptance.public_report_summary(report), "report": report}


@app.get("/admin/acceptance-reports/{report_id}")
async def admin_acceptance_report_get(report_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "acceptance reports require administrator role")
    try:
        report = acceptance.load_report(acceptance_report_root_path(), report_id)
    except acceptance.AcceptanceReportError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404 if "not found" in detail else 400, detail=detail) from exc
    return {
        "summary": acceptance.public_report_summary(report, acceptance.report_directory(acceptance_report_root_path(), report_id)),
        "report": report,
    }


@app.get("/admin/acceptance-reports/{report_id}/files/{filename}")
async def admin_acceptance_report_file(report_id: str, filename: str, authorization: str | None = Header(default=None)) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "acceptance report files require administrator role")
    try:
        path = acceptance.report_file_path(acceptance_report_root_path(), report_id, filename)
        content = await asyncio.to_thread(path.read_bytes)
    except acceptance.AcceptanceReportError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404 if "not found" in detail else 400, detail=detail) from exc
    media_type = {
        "report.json": "application/json",
        "report.md": "text/markdown; charset=utf-8",
        "SHA256SUMS": "text/plain; charset=utf-8",
    }[path.name]
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@app.post("/admin/acceptance-reports")
async def admin_acceptance_report_create(payload: AcceptanceReportCreate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "acceptance reports require administrator role")
    report = await build_acceptance_report_snapshot(auth, payload)
    try:
        summary = await asyncio.to_thread(acceptance.write_report, acceptance_report_root_path(), report)
    except acceptance.AcceptanceReportError as exc:
        raise HTTPException(status_code=409 if "already exists" in str(exc) else 400, detail=str(exc)) from exc
    await record_audit_event(
        auth,
        "acceptance_report.created",
        target_type="acceptance_report",
        target_id=report["id"],
        summary=f"Created acceptance report {report['id']}",
        metadata={
            "label": report.get("label"),
            "status": report.get("status"),
            "runtime_deployment_mode": report.get("runtime_deployment_mode"),
            "operator_handoff_ready": report.get("operator_handoff_ready"),
            "acceptance_blockers": report.get("acceptance_blockers"),
            "files": summary.get("files"),
        },
    )
    return {"summary": summary, "report": report}


@app.get("/admin/migration/rollback-rehearsal")
async def admin_rollback_rehearsal_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "rollback rehearsal status requires administrator role")
    return await asyncio.to_thread(rollback_rehearsal.status, backup_root_path())


@app.post("/admin/migration/rollback-rehearsal")
async def admin_rollback_rehearsal_create(payload: RollbackRehearsalCreate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "rollback rehearsal generation requires administrator role")
    try:
        result = await asyncio.to_thread(
            rollback_rehearsal.build_and_write_report,
            backup_root=backup_root_path(),
            cutover_plan_name=payload.cutover_plan_name,
            rehearsed_by=payload.rehearsed_by.strip() or auth.subject_id,
            rollback_commands_tested=payload.rollback_commands_tested,
            old_resources_preserved=payload.old_resources_preserved,
            notes=payload.notes.strip() or None,
        )
    except rollback_rehearsal.RollbackRehearsalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    report = result["report"]
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    preserved = checks.get("old_resources_preserved") if isinstance(checks.get("old_resources_preserved"), dict) else {}
    await record_audit_event(
        auth,
        "rollback_rehearsal.created",
        target_type="rollback_rehearsal",
        target_id=str(report.get("cutover_plan_name") or ""),
        summary="Created rollback rehearsal report",
        metadata={
            "cutover_plan_name": report.get("cutover_plan_name"),
            "cutover_plan_sha256": report.get("cutover_plan_sha256"),
            "output": result.get("output"),
            "resource_count": preserved.get("resource_count"),
        },
    )
    return result


@app.get("/admin/migration/open-webui-plan")
async def admin_open_webui_migration_plan_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "Open WebUI migration plan status requires administrator role")
    return await asyncio.to_thread(open_webui_migration.status, backup_root_path(), restore_test_root_path())


@app.post("/admin/migration/open-webui-plan")
async def admin_open_webui_migration_plan_create(payload: OpenWebUiMigrationPlanCreate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "Open WebUI migration plan generation requires administrator role")
    try:
        result = await asyncio.to_thread(
            open_webui_migration.build_and_write_plan,
            backup_root_path(),
            restore_test_root_path(),
        )
    except open_webui_migration.OpenWebUiMigrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await record_audit_event(
        auth,
        "open_webui_migration_plan.created",
        target_type="open_webui_migration_plan",
        target_id=result.get("name"),
        summary="Created Open WebUI migration plan",
        metadata={
            "path": result.get("path"),
            "recommended_strategy": result.get("summary", {}).get("recommended_strategy"),
            "warning_count": result.get("summary", {}).get("warning_count"),
            "notes": payload.notes,
        },
    )
    return result


@app.get("/admin/migration/backup-migration-rollback-evidence")
async def admin_backup_migration_rollback_evidence_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "backup/migration/rollback evidence status requires administrator role")
    return await asyncio.to_thread(backup_migration_rollback.status, backup_root_path(), restore_test_root_path())


@app.post("/admin/migration/backup-migration-rollback-evidence")
async def admin_backup_migration_rollback_evidence_create(
    payload: BackupMigrationRollbackEvidenceCreate,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "backup/migration/rollback evidence generation requires administrator role")
    if payload.confirm_reviewed is not True:
        raise HTTPException(status_code=400, detail="confirm_reviewed=true is required after reviewing backup, migration, cutover, and rollback artifacts")
    try:
        result = await asyncio.to_thread(
            backup_migration_rollback.build_and_write_evidence,
            backup_root=backup_root_path(),
            restore_root=restore_test_root_path(),
            backup_encryption_key=settings.master_key,
        )
    except backup_migration_rollback.EvidenceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    evidence = result["evidence"]
    await record_audit_event(
        auth,
        "backup_migration_rollback_evidence.created",
        target_type="backup_migration_rollback_evidence",
        target_id=Path(str(result.get("output") or "")).name,
        summary="Created backup/migration/rollback acceptance evidence",
        metadata={
            "status": evidence.get("status"),
            "output": result.get("output"),
            "required_checks": evidence.get("required_checks"),
            "inputs": result.get("inputs"),
        },
    )
    return result


@app.get("/admin/updates")
async def admin_updates(authorization: str | None = Header(default=None), limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "update management requires administrator role")
    rows = await database.list_update_plans(limit=limit)
    return {"object": "list", "data": [public_update_plan(row) for row in rows]}


@app.get("/admin/updates/{update_id}")
async def admin_update_get(update_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_administrator(auth, "update management requires administrator role")
    row = await database.get_update_plan(update_id)
    if row is None:
        raise HTTPException(status_code=404, detail="update plan not found")
    return public_update_plan(row)


@app.post("/admin/updates")
async def admin_update_plan_create(payload: UpdatePlanCreateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "update management requires administrator role")
    try:
        preflight = update_policy.build_update_preflight(
            payload.target_version,
            [item.model_dump() for item in payload.image_refs],
            payload.source_url,
        )
    except update_policy.UpdatePolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    update_id = f"update_{uuid.uuid4().hex}"
    row = await database.insert_update_plan(
        {
            "id": update_id,
            "target_version": preflight["target_version"],
            "source_url": preflight["source_url"],
            "status": "planned",
            "stage": "preflight_validated",
            "image_refs": preflight["image_refs"],
            "preflight": preflight,
            "notes": payload.notes.strip(),
            "created_by": auth.subject_id,
        }
    )
    await record_audit_event(
        auth,
        "update.planned",
        target_type="update",
        target_id=update_id,
        summary=f"Created update plan for {preflight['target_version']}",
        metadata={
            "target_version": preflight["target_version"],
            "image_count": preflight["image_count"],
            "source_url": preflight["source_url"],
            "pinned_images": preflight["pinned_images"],
        },
    )
    return public_update_plan(row)


@app.post("/admin/updates/{update_id}/stage")
async def admin_update_stage(update_id: str, payload: UpdateActionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "update management requires administrator role")
    require_maintenance_enabled_for_update("staging")
    row = await database.get_update_plan(update_id)
    if row is None:
        raise HTTPException(status_code=404, detail="update plan not found")
    if row["status"] not in {"planned", "stage_failed", "validated"}:
        raise update_plan_conflict(f"update plan state {row['status']} cannot be staged", row)
    backup_summary: dict[str, Any] | None = None
    image_stage_results: list[dict[str, Any]] = []
    compose_override: dict[str, Any] = {}
    self_test: dict[str, Any] | None = None
    try:
        backup_summary = await create_control_plane_backup(f"update-{update_id[-12:]}")
        image_stage_results = await stage_update_images(row, payload)
        failed_image_stage = next((item for item in image_stage_results if item.get("status") == "failed"), None)
        if failed_image_stage is not None:
            raise RuntimeError(f"image staging failed for {failed_image_stage['service']}: {failed_image_stage.get('error', 'unknown error')}")
        compose_override = compose_override_policy.write_compose_image_override(
            data_root=Path(settings.data_root),
            update_id=update_id,
            target_version=str(row["target_version"]),
            image_refs=row.get("image_refs") or [],
            image_stage=image_stage_results,
        )
        self_test = await build_self_test_report(auth.subject_id)
    except Exception as exc:
        updated = await database.update_update_plan(
            update_id,
            status="stage_failed",
            stage="stage_failed",
            image_stage=image_stage_results,
            compose_override=compose_override,
            failure_message=f"{exc.__class__.__name__}: {str(exc)[:500]}",
        )
        await record_audit_event(
            auth,
            "update.stage_failed",
            target_type="update",
            target_id=update_id,
            summary=f"Update staging failed for {update_id}",
            metadata={
                "error": exc.__class__.__name__,
                "backup_name": (backup_summary or {}).get("name"),
                "image_stage_count": len(image_stage_results),
                "compose_override_path": compose_override.get("path"),
            },
        )
        raise HTTPException(status_code=502, detail={"message": "update staging failed", "update": public_update_plan(updated or row)}) from exc
    status = "staged" if self_test["status"] != "failed" else "stage_failed"
    updated = await database.update_update_plan(
        update_id,
        status=status,
        stage="backup_images_and_self_test_completed" if status == "staged" else "self_test_failed",
        backup_name=backup_summary["name"],
        image_stage=image_stage_results,
        compose_override=compose_override,
        self_test=self_test,
        staged_at=datetime.now(tz=UTC),
        failure_message=None if status == "staged" else "self-test failed during update staging",
    )
    await record_audit_event(
        auth,
        "update.staged" if status == "staged" else "update.stage_failed",
        target_type="update",
        target_id=update_id,
        summary=f"Staged update {update_id}",
        metadata={
            "target_version": row["target_version"],
            "backup_name": backup_summary["name"],
            "image_stage_count": len(image_stage_results),
            "image_stage_statuses": [str(item.get("status") or "unknown") for item in image_stage_results],
            "compose_override_path": compose_override.get("path"),
            "compose_override_ready_for_promotion": compose_override.get("ready_for_promotion"),
            "self_test_status": self_test["status"],
            **freeform_reason_metadata(payload.reason),
        },
    )
    return public_update_plan(updated or row)


@app.post("/admin/updates/{update_id}/health-check")
async def admin_update_health_check(update_id: str, payload: UpdateActionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "update management requires administrator role")
    require_maintenance_enabled_for_update("health-check")
    row = await database.get_update_plan(update_id)
    if row is None:
        raise HTTPException(status_code=404, detail="update plan not found")
    if row["status"] not in {"staged", "validated", "promotion_ready", "health_failed"}:
        raise update_plan_conflict(f"update plan state {row['status']} cannot run health-check", row)
    self_test = await build_self_test_report(auth.subject_id)
    health_gate = update_policy.update_health_gate(self_test)
    status = "validated" if health_gate["ready"] else "health_failed"
    updated = await database.update_update_plan(
        update_id,
        status=status,
        stage="health_check_completed" if status == "validated" else "health_check_failed",
        self_test=self_test,
        health_checked_at=datetime.now(tz=UTC),
        failure_message=None
        if status == "validated"
        else f"update health-check gate blocked: {'; '.join(health_gate['blockers'])[:450]}",
    )
    await record_audit_event(
        auth,
        "update.health_checked",
        target_type="update",
        target_id=update_id,
        summary=f"Ran update health-check for {update_id}",
        metadata={
            "status": status,
            "self_test_status": self_test["status"],
            "update_health_ready": health_gate["ready"],
            "update_health_blocker_count": len(health_gate["blockers"]),
            **freeform_reason_metadata(payload.reason),
        },
    )
    return public_update_plan(updated or row)


@app.post("/admin/updates/{update_id}/promote")
async def admin_update_promote(update_id: str, payload: UpdateActionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "update management requires administrator role")
    require_maintenance_enabled_for_update("promotion")
    row = await database.get_update_plan(update_id)
    if row is None:
        raise HTTPException(status_code=404, detail="update plan not found")
    if row["status"] not in {"validated", "promotion_failed", "promotion_ready"}:
        raise update_plan_conflict(f"update plan state {row['status']} cannot be promoted", row)
    health_gate = update_policy.update_health_gate(row.get("self_test") or {})
    if not health_gate["ready"]:
        updated = await database.update_update_plan(
            update_id,
            status="promotion_failed",
            stage="promotion_health_gate_failed",
            promotion_result={
                "format": compose_override_policy.PROMOTION_FORMAT,
                "status": "failed",
                "error": "update health-check gate blocked",
                "update_health_gate": health_gate,
            },
            failure_message=f"update health-check gate blocked: {'; '.join(health_gate['blockers'])[:450]}",
        )
        await record_audit_event(
            auth,
            "update.promotion_failed",
            target_type="update",
            target_id=update_id,
            summary=f"Update promotion health gate failed for {update_id}",
            metadata={
                "update_health_ready": False,
                "update_health_blocker_count": len(health_gate["blockers"]),
                **freeform_reason_metadata(payload.reason),
            },
        )
        raise HTTPException(status_code=409, detail={"message": "update health-check gate blocked", "update": public_update_plan(updated or row)})

    image_inspect_results: list[dict[str, Any]] = []
    try:
        verified_override = compose_override_policy.verify_compose_image_override(
            data_root=Path(settings.data_root),
            update_id=update_id,
            image_refs=row.get("image_refs") or [],
            image_stage=row.get("image_stage") or [],
            metadata=row.get("compose_override") or {},
        )
        image_inspect_results = await inspect_update_images_for_promotion(row, payload)
        failed_image = next((item for item in image_inspect_results if item.get("status") == "failed"), None)
        if failed_image is not None:
            raise RuntimeError(f"image promotion preflight failed for {failed_image['service']}: {failed_image.get('error', 'unknown error')}")
        promotion_result = compose_override_policy.build_promotion_handoff(
            update_id=update_id,
            target_version=str(row["target_version"]),
            compose_override=verified_override,
            reason=operational_reason_label(f"promote update {update_id}", payload.reason),
            requested_by=auth.subject_id,
        )
        promotion_result["image_inspect"] = image_inspect_results
    except compose_override_policy.ComposeOverrideError as exc:
        updated = await database.update_update_plan(
            update_id,
            status="promotion_failed",
            stage="promotion_preflight_failed",
            promotion_result={
                "format": compose_override_policy.PROMOTION_FORMAT,
                "status": "failed",
                "error": str(exc)[:500],
                "image_inspect": image_inspect_results,
            },
            failure_message=str(exc)[:500],
        )
        await record_audit_event(
            auth,
            "update.promotion_failed",
            target_type="update",
            target_id=update_id,
            summary=f"Update promotion preflight failed for {update_id}",
            metadata={"error": str(exc)[:500], **freeform_reason_metadata(payload.reason)},
        )
        raise HTTPException(status_code=409, detail={"message": str(exc), "update": public_update_plan(updated or row)}) from exc
    except Exception as exc:
        updated = await database.update_update_plan(
            update_id,
            status="promotion_failed",
            stage="promotion_preflight_failed",
            promotion_result={
                "format": compose_override_policy.PROMOTION_FORMAT,
                "status": "failed",
                "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
                "image_inspect": image_inspect_results,
            },
            failure_message=f"{exc.__class__.__name__}: {str(exc)[:500]}",
        )
        await record_audit_event(
            auth,
            "update.promotion_failed",
            target_type="update",
            target_id=update_id,
            summary=f"Update promotion preflight failed for {update_id}",
            metadata={"error": exc.__class__.__name__, **freeform_reason_metadata(payload.reason)},
        )
        raise HTTPException(status_code=502, detail={"message": "update promotion preflight failed", "update": public_update_plan(updated or row)}) from exc

    updated = await database.update_update_plan(
        update_id,
        status="promotion_ready",
        stage="promotion_handoff_ready",
        promotion_result=promotion_result,
        promotion_requested_at=datetime.now(tz=UTC),
        failure_message=None,
    )
    await record_audit_event(
        auth,
        "update.promotion_ready",
        target_type="update",
        target_id=update_id,
        summary=f"Prepared promotion handoff for update {update_id}",
        metadata={
            "target_version": row["target_version"],
            "services": promotion_result["compose_override"]["services"],
            "compose_override_sha256": promotion_result["compose_override"]["sha256"],
            **freeform_reason_metadata(payload.reason),
        },
    )
    return public_update_plan(updated or row)


@app.post("/admin/updates/{update_id}/rollback")
async def admin_update_rollback(update_id: str, payload: UpdateActionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_administrator(auth, "update management requires administrator role")
    require_maintenance_enabled_for_update("rollback")
    row = await database.get_update_plan(update_id)
    if row is None:
        raise HTTPException(status_code=404, detail="update plan not found")
    agent_result, agent_error = await runtime_agent_post(
        "/v1/rollback",
        {"reason": operational_reason_label(f"rollback update {update_id}", payload.reason), "timeout_seconds": payload.timeout_seconds},
        timeout_seconds=max(30.0, float(payload.timeout_seconds + 5)),
    )
    if agent_error is not None:
        updated = await database.update_update_plan(
            update_id,
            status="rollback_failed",
            stage="rollback_failed",
            failure_message=agent_error[:500],
        )
        await record_audit_event(
            auth,
            "update.rollback_failed",
            target_type="update",
            target_id=update_id,
            summary=f"Update rollback failed for {update_id}",
            metadata={"error": agent_error[:500], **freeform_reason_metadata(payload.reason)},
        )
        raise HTTPException(status_code=502, detail={"message": agent_error, "update": public_update_plan(updated or row)})
    agent_status = str((agent_result or {}).get("status") or "unknown")
    status = "rollback_dry_run" if agent_status == "dry_run" else "rolled_back" if agent_status == "ok" else "rollback_failed"
    updated = await database.update_update_plan(
        update_id,
        status=status,
        stage="rollback_completed" if status in {"rollback_dry_run", "rolled_back"} else "rollback_failed",
        rollback_result=agent_result or {},
        rolled_back_at=datetime.now(tz=UTC),
        failure_message=None if status in {"rollback_dry_run", "rolled_back"} else f"runtime-agent rollback returned {agent_status}",
    )
    await record_audit_event(
        auth,
        "update.rollback_requested",
        target_type="update",
        target_id=update_id,
        summary=f"Requested rollback for update {update_id}",
        metadata={
            "runtime_agent_status": agent_status,
            "status": status,
            "services": (agent_result or {}).get("services"),
            **freeform_reason_metadata(payload.reason),
        },
    )
    return public_update_plan(updated or row)


@app.get("/admin/audit-log")
async def admin_audit_log(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    event_type: str | None = Query(default=None, max_length=128),
    actor_id: str | None = Query(default=None, max_length=128),
    target_type: str | None = Query(default=None, max_length=128),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    rows = await database.list_audit_events(limit=limit, event_type=event_type, actor_id=actor_id, target_type=target_type)
    return {"object": "list", "data": [audit_policy.public_audit_event(row) for row in rows]}


@app.get("/admin/jobs")
async def admin_jobs(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    state: JobState | None = Query(default=None),
    runtime: str | None = Query(default=None, max_length=64),
    modality: str | None = Query(default=None, max_length=64),
    owner_id: str | None = Query(default=None, max_length=128),
    native_prompt_id: str | None = Query(default=None, min_length=1, max_length=128),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:read")
    require_queue_admin(auth)
    state = optional_query_value(state)
    runtime = optional_query_value(runtime)
    modality = optional_query_value(modality)
    owner_id = optional_query_value(owner_id)
    native_prompt_id = optional_query_value(native_prompt_id)
    filters: dict[str, Any] = {
        "limit": limit,
        "owner_id": owner_id,
        "state": state.value if state else None,
        "runtime": runtime,
        "modality": modality,
    }
    if native_prompt_id:
        filters["native_prompt_id"] = native_prompt_id
    rows = await database.list_jobs(**filters)
    return {"object": "list", "data": public_jobs(rows)}


@app.get("/admin/jobs/{job_id}")
async def admin_job_get(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:read")
    require_queue_admin(auth)
    job = await database.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return public_job(job)


@app.get(
    "/admin/jobs/{job_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def admin_job_events(
    job_id: str,
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:read")
    require_queue_admin(auth)
    job = await database.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job_event_stream(job_id, lambda current: True, last_event_id=last_event_id)


@app.post("/admin/jobs/{job_id}/priority")
async def admin_job_priority_update(
    job_id: str,
    payload: JobPriorityUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:write")
    require_queue_admin(auth)
    before = await database.get_job(job_id)
    if before is None:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        updated = await database.update_job_priority(job_id, payload.priority.value)
    except ValueError as exc:
        raise job_mutation_conflict(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="job not found")
    await record_audit_event(
        auth,
        "job.priority_updated",
        target_type="job",
        target_id=job_id,
        summary=f"Updated job {job_id} priority",
        metadata={
            "previous_priority": before.get("priority"),
            "priority": updated.get("priority"),
            "state": updated.get("state"),
            "runtime": updated.get("runtime"),
            "model_alias": updated.get("model_alias"),
            **freeform_reason_metadata(payload.reason),
        },
        correlation_id=updated.get("correlation_id"),
    )
    return public_job(updated)


@app.post("/admin/jobs/{job_id}/cancel")
async def admin_job_cancel(
    job_id: str,
    payload: JobMutationRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:write")
    require_queue_admin(auth)
    before = await database.get_job(job_id)
    if before is None:
        raise HTTPException(status_code=404, detail="job not found")
    updated = await database.request_job_cancel(job_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="job not found")
    if before.get("state") not in TERMINAL_JOB_STATES:
        await record_audit_event(
            auth,
            "job.cancelled" if updated["state"] == JobState.CANCELLED.value else "job.cancelling",
            target_type="job",
            target_id=job_id,
            summary=f"Requested cancellation for job {job_id}",
            metadata={
                "previous_state": before.get("state"),
                "state": updated.get("state"),
                "runtime": updated.get("runtime"),
                "model_alias": updated.get("model_alias"),
                **freeform_reason_metadata(payload.reason),
            },
            correlation_id=updated.get("correlation_id"),
        )
    return public_job(updated)


@app.post("/admin/jobs/{job_id}/retry")
async def admin_job_retry(
    job_id: str,
    payload: JobMutationRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:write")
    require_queue_admin(auth)
    require_not_in_maintenance("job/retry")
    before = await database.get_job(job_id)
    if before is None:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        updated = await database.retry_job(job_id)
    except ValueError as exc:
        raise job_mutation_conflict(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="job not found")
    await record_audit_event(
        auth,
        "job.retried",
        target_type="job",
        target_id=job_id,
        summary=f"Retried job {job_id}",
        metadata={
            "previous_state": before.get("state"),
            "state": updated.get("state"),
            "retry_count": updated.get("retry_count"),
            "runtime": updated.get("runtime"),
            "model_alias": updated.get("model_alias"),
            **freeform_reason_metadata(payload.reason),
        },
        correlation_id=updated.get("correlation_id"),
    )
    return public_job(updated)


@app.get("/admin/backups")
async def admin_backups(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:read")
    return {
        "object": "list",
        "data_root": str(data_root_path()),
        "backup_root": str(backup_root_path()),
        "restore_test_root": str(restore_test_root_path()),
        "default_include_paths": backup_restore.DEFAULT_INCLUDE_PATHS,
        "default_excluded_paths": backup_restore.DEFAULT_EXCLUDES,
        "data": backup_restore.list_backups(backup_root_path()),
    }


@app.post("/admin/backups/retention-plan")
async def admin_backup_retention_plan(payload: BackupRetentionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:read")
    try:
        return await asyncio.to_thread(
            backup_restore.build_backup_retention_plan,
            backup_root_path(),
            keep_last=payload.keep_last,
            delete_older_than_days=payload.delete_older_than_days,
        )
    except backup_restore.BackupError as exc:
        raise backup_error_response(exc) from exc


@app.post("/admin/backups/cleanup")
async def admin_backup_cleanup(payload: BackupRetentionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    try:
        report = await asyncio.to_thread(
            backup_restore.apply_backup_retention_plan,
            backup_root_path(),
            keep_last=payload.keep_last,
            delete_older_than_days=payload.delete_older_than_days,
            confirmed=payload.confirm,
        )
    except backup_restore.BackupError as exc:
        raise backup_error_response(exc) from exc
    log_event("backup_cleanup_completed", deleted=report["deleted_count"], reclaimable=report["total_reclaimable_bytes"])
    await record_audit_event(
        auth,
        "backup.cleanup",
        target_type="backup",
        summary="Applied backup retention cleanup",
        metadata={
            "policy": report["policy"],
            "deleted_count": report["deleted_count"],
            "deleted_names": [item["name"] for item in report["deleted"]],
            "total_reclaimable_bytes": report["total_reclaimable_bytes"],
            "invalid_preserved_count": report["invalid_preserved_count"],
        },
    )
    return report


@app.post("/admin/artifacts/retention-plan")
async def admin_artifact_retention_plan(payload: ArtifactRetentionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:read")
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=payload.delete_older_than_days)
    jobs = await database.list_artifact_retention_jobs(cutoff, limit=payload.limit)
    try:
        return await asyncio.to_thread(
            artifact_retention.build_artifact_retention_plan,
            artifact_root_path(),
            jobs,
            delete_older_than_days=payload.delete_older_than_days,
            protected_urls=await protected_artifact_urls(),
            namespaces=payload.namespaces,
            now=now,
            limit=payload.limit,
        )
    except artifact_retention.ArtifactRetentionError as exc:
        raise artifact_retention_error_response(exc) from exc


@app.post("/admin/artifacts/cleanup")
async def admin_artifact_cleanup(payload: ArtifactRetentionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=payload.delete_older_than_days)
    jobs = await database.list_artifact_retention_jobs(cutoff, limit=payload.limit)
    try:
        report = await asyncio.to_thread(
            artifact_retention.apply_artifact_retention_plan,
            artifact_root_path(),
            jobs,
            delete_older_than_days=payload.delete_older_than_days,
            protected_urls=await protected_artifact_urls(),
            namespaces=payload.namespaces,
            confirmed=payload.confirm,
            now=now,
            limit=payload.limit,
        )
    except artifact_retention.ArtifactRetentionError as exc:
        raise artifact_retention_error_response(exc) from exc
    for update in report.get("job_updates", []):
        if isinstance(update, dict) and update.get("job_id") and isinstance(update.get("artifacts"), list):
            await database.update_job(str(update["job_id"]), artifacts=update["artifacts"])
    public_report = {key: value for key, value in report.items() if key != "job_updates"}
    log_event("artifact_cleanup_completed", deleted=public_report["deleted_count"], reclaimable=public_report["total_reclaimable_bytes"])
    await record_audit_event(
        auth,
        "artifact.cleanup",
        target_type="artifact",
        summary="Applied generated artifact retention cleanup",
        metadata={
            "policy": public_report["policy"],
            "deleted_count": public_report["deleted_count"],
            "deleted_paths": [item["path"] for item in public_report["deleted"]],
            "total_reclaimable_bytes": public_report["total_reclaimable_bytes"],
            "invalid_preserved_count": public_report["invalid_preserved_count"],
            "job_update_count": public_report.get("job_update_count"),
        },
    )
    return public_report


@app.post("/admin/models/quarantine/retention-plan")
async def admin_model_quarantine_retention_plan(payload: ModelQuarantineRetentionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:read")
    require_model_admin(auth)
    try:
        return await asyncio.to_thread(
            model_lifecycle.build_blob_quarantine_retention_plan,
            data_root_path(),
            delete_older_than_days=payload.delete_older_than_days,
            limit=payload.limit,
        )
    except model_lifecycle.ModelLifecycleError as exc:
        raise model_lifecycle_error_response(exc) from exc


@app.post("/admin/models/quarantine/cleanup")
async def admin_model_quarantine_cleanup(payload: ModelQuarantineRetentionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    require_model_admin(auth)
    try:
        report = await asyncio.to_thread(
            model_lifecycle.apply_blob_quarantine_retention_plan,
            data_root_path(),
            delete_older_than_days=payload.delete_older_than_days,
            confirmed=payload.confirm,
            limit=payload.limit,
        )
    except model_lifecycle.ModelLifecycleError as exc:
        raise model_lifecycle_error_response(exc) from exc
    log_event("model_quarantine_cleanup_completed", deleted=report["deleted_count"], reclaimable=report["total_reclaimable_bytes"])
    await record_audit_event(
        auth,
        "model_quarantine.cleanup",
        target_type="model_quarantine",
        summary="Applied model blob quarantine retention cleanup",
        metadata={
            "policy": report["policy"],
            "deleted_count": report["deleted_count"],
            "deleted_paths": [item["path"] for item in report["deleted"]],
            "total_reclaimable_bytes": report["total_reclaimable_bytes"],
            "invalid_preserved_count": report["invalid_preserved_count"],
            "truncated": report.get("truncated"),
        },
    )
    return report


@app.post("/admin/backups")
async def admin_backup_create(payload: BackupCreateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    try:
        summary = await create_control_plane_backup(payload.label)
    except (backup_restore.BackupError, RuntimeError, ValueError) as exc:
        raise backup_error_response(exc) from exc
    log_event("backup_created", backup=summary["name"], files=summary["file_count"], sensitive=summary["contains_sensitive_data"])
    await record_audit_event(
        auth,
        "backup.created",
        target_type="backup",
        target_id=summary["name"],
        summary=f"Created backup {summary['name']}",
        metadata={
            "file_count": summary["file_count"],
            "contains_sensitive_data": summary["contains_sensitive_data"],
            "postgres_dump_included": summary.get("postgres_dump_included"),
            "postgres_native_dump_included": bool(summary.get("postgres_native_dump")),
            "archive_size_bytes": (summary.get("archive") or {}).get("size_bytes"),
        },
    )
    return summary


@app.get("/admin/backups/schedule")
async def admin_backup_schedule_get(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:read")
    row = await database.get_backup_schedule()
    return public_backup_schedule(row)


@app.put("/admin/backups/schedule")
async def admin_backup_schedule_update(payload: BackupScheduleUpdateRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    values = backup_schedule_from_payload(payload, auth)
    row = await database.upsert_backup_schedule(values)
    await record_audit_event(
        auth,
        "backup_schedule.updated",
        target_type="backup_schedule",
        target_id=backup_schedule.SCHEDULE_ID,
        summary="Updated backup schedule",
        metadata={
            "enabled": row["enabled"],
            "interval_hours": row["interval_hours"],
            "keep_last": row["keep_last"],
            "delete_older_than_days": row.get("delete_older_than_days"),
            "next_run_at": row.get("next_run_at"),
        },
    )
    return public_backup_schedule(row)


@app.post("/admin/backups/schedule/run")
async def admin_backup_schedule_run(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    row = await database.get_backup_schedule()
    schedule = row or backup_schedule.default_schedule()
    current = datetime.now(tz=UTC)
    try:
        summary = await create_control_plane_backup(backup_schedule.backup_label(schedule["label_prefix"], current))
        retention = await asyncio.to_thread(
            backup_restore.apply_backup_retention_plan,
            backup_root_path(),
            keep_last=int(schedule["keep_last"]),
            delete_older_than_days=schedule.get("delete_older_than_days"),
            confirmed=True,
        )
    except (backup_restore.BackupError, RuntimeError, ValueError) as exc:
        raise backup_error_response(exc) from exc
    if row is not None:
        await database.update_backup_schedule_after_run(
            backup_schedule.SCHEDULE_ID,
            status="completed",
            next_run_at=backup_schedule.next_run_after(current, int(schedule["interval_hours"])) if schedule.get("enabled") else schedule.get("next_run_at"),
            backup_name=summary["name"],
        )
    await record_audit_event(
        auth,
        "backup_schedule.run",
        target_type="backup",
        target_id=summary["name"],
        summary=f"Ran scheduled backup policy for {summary['name']}",
        metadata={
            "file_count": summary["file_count"],
            "postgres_dump_included": summary.get("postgres_dump_included"),
            "postgres_native_dump_included": bool(summary.get("postgres_native_dump")),
            "retention_deleted_count": retention.get("deleted_count"),
            "schedule_source": "database" if row is not None else "default",
        },
    )
    return {"status": "completed", "backup": summary, "retention": retention, "schedule": public_backup_schedule(await database.get_backup_schedule())}


@app.get("/admin/backups/{backup_name}/manifest")
async def admin_backup_manifest(backup_name: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:read")
    try:
        return backup_restore.load_manifest(backup_restore.backup_dir_for_name(backup_root_path(), backup_name))
    except backup_restore.BackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/admin/backups/{backup_name}/verify")
async def admin_backup_verify(backup_name: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:read")
    try:
        report = await asyncio.to_thread(backup_restore.verify_backup, backup_root_path(), backup_name, settings.master_key)
    except (backup_restore.BackupError, backup_restore.RestoreError) as exc:
        raise backup_error_response(exc) from exc
    await record_audit_event(
        auth,
        "backup.verified",
        target_type="backup",
        target_id=backup_name,
        summary=f"Verified backup {backup_name}",
        metadata={"status": report.get("status"), "files_verified": report.get("files_verified")},
    )
    return report


@app.post("/admin/backups/{backup_name}/restore-test")
async def admin_backup_restore_test(
    backup_name: str,
    payload: BackupRestoreTestRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    try:
        report = await asyncio.to_thread(
            backup_restore.restore_backup_to_alternate,
            backup_root_path(),
            backup_name,
            restore_test_root_path(),
            payload.force,
            settings.master_key,
        )
    except (backup_restore.BackupError, backup_restore.RestoreError) as exc:
        raise backup_error_response(exc) from exc
    log_event("backup_restore_test_completed", backup=backup_name, target=report["target"], files=report["files_verified"])
    await record_audit_event(
        auth,
        "backup.restore_test_completed",
        target_type="backup",
        target_id=backup_name,
        summary=f"Restore-tested backup {backup_name}",
        metadata={"target": report["target"], "files_verified": report["files_verified"], "status": report.get("status")},
    )
    return report


@app.post("/admin/backups/{backup_name}/postgres-import")
async def admin_backup_postgres_import(
    backup_name: str,
    payload: BackupPostgresImportRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "storage:write")
    if payload.apply:
        require_administrator(auth, "PostgreSQL backup import apply requires administrator role")
        require_maintenance_enabled_for_restore("postgres-import apply")
        if payload.confirm_backup_name != backup_name:
            raise HTTPException(status_code=422, detail="confirm_backup_name must match backup_name when apply=true")
    try:
        manifest = backup_restore.load_manifest(backup_restore.backup_dir_for_name(backup_root_path(), backup_name))
        dump = manifest.get("postgres_dump")
        if not dump or not dump.get("path"):
            raise HTTPException(status_code=424, detail="backup does not contain a PostgreSQL logical export")
        dump_path = restore_test_root_path() / backup_name / dump["path"]
        if not dump_path.is_file():
            raise HTTPException(status_code=424, detail="run restore-test before PostgreSQL import planning or apply")
        result = await database.import_logical_dump(dump_path, apply=payload.apply)
    except (backup_restore.BackupError, database.LogicalDumpError, RuntimeError, ValueError) as exc:
        raise backup_error_response(exc) from exc
    log_event("backup_postgres_import", backup=backup_name, apply=payload.apply, status=result["status"])
    await record_audit_event(
        auth,
        "backup.postgres_import",
        target_type="backup",
        target_id=backup_name,
        summary=f"{'Applied' if payload.apply else 'Planned'} PostgreSQL import for {backup_name}",
        metadata={"apply": payload.apply, "status": result["status"], "row_counts": result.get("row_counts"), "applied_row_counts": result.get("applied_row_counts")},
    )
    return result


@app.get("/workflows/v1/published")
async def workflows_published(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:read")
    rows = await database.list_workflows()
    visible = [public_workflow(row) for row in rows if visible_to_role(public_workflow(row), auth.role.value, auth.scopes)]
    return {"object": "list", "data": visible}


@app.get("/workflows/v1/published/{workflow_id}")
async def workflow_published_get(workflow_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:read")
    row = await database.get_workflow(workflow_id)
    if row is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    public = public_workflow(row)
    if not visible_to_role(public, auth.role.value, auth.scopes):
        raise HTTPException(status_code=403, detail="workflow is not visible to this role")
    return public


@app.get("/workflows/v1/published/{workflow_id}/versions")
async def workflow_published_versions(workflow_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:read")
    require_workflow_governance_reader(auth)
    rows = [row for row in await database.list_workflows(include_unpublished=True) if row.get("id") == workflow_id]
    if not rows:
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"object": "list", "data": [public_workflow(row) for row in rows]}


@app.get("/workflows/v1/published/{workflow_id}/versions/{version}")
async def workflow_published_version_get(workflow_id: str, version: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:read")
    row = await database.get_workflow(workflow_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    public = public_workflow(row)
    if not visible_to_role(public, auth.role.value, auth.scopes):
        raise HTTPException(status_code=403, detail="workflow is not visible to this role")
    return public


@app.get("/admin/comfyui/node-pins")
async def admin_comfyui_node_pins(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:read")
    require_workflow_governance_reader(auth)
    try:
        registry, sources, timestamps = await merged_node_pin_registry_for_api()
    except WorkflowError as exc:
        raise HTTPException(status_code=503, detail={"code": "comfyui_node_pin_registry_invalid", "message": str(exc)}) from exc
    data = [
        public_comfyui_node_pin(
            pin,
            source=sources.get(key, "database"),
            created_at=timestamps.get(key, {}).get("created_at"),
            updated_at=timestamps.get(key, {}).get("updated_at"),
        )
        for key, pin in sorted(registry.items())
    ]
    return {"object": "list", "data": data}


@app.post("/admin/comfyui/node-pins")
async def admin_comfyui_node_pin_create(payload: ComfyUiNodePinCreate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:write")
    require_administrator(auth, "ComfyUI node pin approval requires administrator role")
    approved_at = datetime.now(tz=UTC)
    pin = node_pin_create_record(payload, auth, approved_at)
    row = await database.upsert_comfyui_node_pin(node_pin_database_payload(pin, approved_at=approved_at))
    await refresh_node_pin_registry()
    dependency_refresh = await refresh_workflow_dependency_statuses()
    await record_audit_event(
        auth,
        "comfyui_node_pin.approved",
        target_type="comfyui_node_pin",
        target_id=f"{pin.id}@{pin.commit}",
        summary=f"Approved ComfyUI node pin {pin.id}@{pin.commit}",
        metadata={"status": pin.status, "repository_url": pin.repository_url, "allowed_route_prefixes": pin.allowed_route_prefixes},
    )
    return {"pin": public_comfyui_node_pin(database_node_pin_to_approved(row), source="database", created_at=row.get("created_at"), updated_at=row.get("updated_at")), "dependency_refresh": dependency_refresh}


@app.patch("/admin/comfyui/node-pins/{node_id}/commits/{commit}")
async def admin_comfyui_node_pin_update(
    node_id: str = ApiPath(min_length=2, max_length=128),
    commit: str = ApiPath(min_length=40, max_length=40),
    payload: ComfyUiNodePinUpdate = Body(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:write")
    require_administrator(auth, "ComfyUI node pin approval requires administrator role")
    approved_at = datetime.now(tz=UTC)
    pin = await node_pin_update_record(node_id, commit, payload, auth, approved_at)
    row = await database.upsert_comfyui_node_pin(node_pin_database_payload(pin, approved_at=approved_at))
    await refresh_node_pin_registry()
    dependency_refresh = await refresh_workflow_dependency_statuses()
    await record_audit_event(
        auth,
        "comfyui_node_pin.updated",
        target_type="comfyui_node_pin",
        target_id=f"{pin.id}@{pin.commit}",
        summary=f"Updated ComfyUI node pin {pin.id}@{pin.commit}",
        metadata={"status": pin.status, "repository_url": pin.repository_url, "allowed_route_prefixes": pin.allowed_route_prefixes},
    )
    return {"pin": public_comfyui_node_pin(database_node_pin_to_approved(row), source="database", created_at=row.get("created_at"), updated_at=row.get("updated_at")), "dependency_refresh": dependency_refresh}


@app.post("/workflows/v1/validate")
async def workflow_validate(payload: WorkflowPublishRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:write")
    await refresh_node_pin_registry()
    return jsonable_encoder(workflow_record_from_payload(payload.workflow))


@app.post("/workflows/v1/test")
async def workflow_test(payload: WorkflowTestRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:write")
    if payload.workflow is not None and (payload.workflow_id or payload.workflow_version):
        raise HTTPException(status_code=422, detail="workflow test accepts either workflow or workflow_id/workflow_version, not both")
    if payload.workflow is None:
        if not payload.workflow_id or not payload.workflow_version:
            raise HTTPException(status_code=422, detail="workflow test requires workflow or workflow_id/workflow_version")
        row = await database.get_workflow(payload.workflow_id, payload.workflow_version)
        if row is None:
            raise HTTPException(status_code=404, detail="published workflow not found")
        workflow = public_workflow(row)
        if not visible_to_role(workflow, auth.role.value, auth.scopes):
            raise HTTPException(status_code=403, detail="workflow is not visible to this role")
        draft = False
    else:
        await refresh_node_pin_registry()
        workflow = jsonable_encoder(workflow_record_from_payload(payload.workflow))
        draft = True
    result = workflow_test_result(workflow, payload)
    await record_audit_event(
        auth,
        "workflow.tested",
        target_type="workflow",
        target_id=f"{workflow['id']}@{workflow['version']}",
        summary=f"Tested workflow {workflow['id']}@{workflow['version']}",
        metadata={
            "workflow_id": workflow["id"],
            "version": workflow["version"],
            "status": result["status"],
            "can_submit": result["can_submit"],
            "draft": draft,
            "parameter_names": result["request"]["input"]["parameter_names"],
        },
    )
    return result


@app.post("/workflows/v1/published")
async def workflow_publish(payload: WorkflowPublishRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:write")
    await refresh_node_pin_registry()
    record = workflow_record_from_payload(payload.workflow)
    row = await database.upsert_workflow(db_workflow_payload(record))
    log_event("workflow_published", workflow_id=row["id"], version=row["version"], status=row["status"])
    await record_audit_event(
        auth,
        "workflow.published",
        target_type="workflow",
        target_id=f"{row['id']}@{row['version']}",
        summary=f"Published workflow {row['id']}@{row['version']}",
        metadata={"workflow_id": row["id"], "version": row["version"], "status": row["status"], "dependency_ready": row["dependency_status"].get("ready")},
    )
    return public_workflow(row)


@app.post("/workflows/v1/published/{workflow_id}/versions/{version}/restore")
async def workflow_restore(
    workflow_id: str,
    version: str,
    payload: WorkflowRestoreRequest = Body(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:write")
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required before restoring a workflow version")
    row = await database.get_workflow(workflow_id, version, include_unpublished=True)
    if row is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    await refresh_node_pin_registry()
    record = workflow_record_from_payload(row["manifest"])
    restored = await database.restore_workflow(db_workflow_payload(record))
    if restored is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    log_event("workflow_restored", workflow_id=workflow_id, version=version, status=restored["status"])
    await record_audit_event(
        auth,
        "workflow.restored",
        target_type="workflow",
        target_id=f"{workflow_id}@{version}",
        summary=f"Restored workflow {workflow_id}@{version}",
        metadata={
            "workflow_id": workflow_id,
            "version": version,
            "status": restored["status"],
            "dependency_ready": restored["dependency_status"].get("ready"),
            "reason_provided": bool(payload.reason),
        },
    )
    return public_workflow(restored)


@app.delete("/workflows/v1/published/{workflow_id}/versions/{version}")
async def workflow_unpublish(workflow_id: str, version: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "workflows:write")
    row = await database.unpublish_workflow(workflow_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    log_event("workflow_unpublished", workflow_id=workflow_id, version=version)
    await record_audit_event(
        auth,
        "workflow.unpublished",
        target_type="workflow",
        target_id=f"{workflow_id}@{version}",
        summary=f"Unpublished workflow {workflow_id}@{version}",
        metadata={"workflow_id": workflow_id, "version": version, "status": row["status"]},
    )
    return public_workflow(row)


@app.get("/admin/models")
async def admin_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    catalog = catalog_snapshot()
    catalog_payload = catalog.to_catalog()
    aliases = catalog_payload["aliases"]
    records = [public_model_record(row) for row in await database.list_model_records()]
    return {
        "object": "list",
        "aliases": aliases,
        "profiles": catalog_payload["profiles"],
        "catalog": catalog_payload["models"],
        "alias_policies": [public_model_alias_policy(row) for row in await database.list_model_alias_policies()],
        "records": records,
        "acceptance_model_measurements": acceptance_model_measurement_coverage(aliases, records),
    }


@app.put("/admin/models/aliases/{alias_id}/policy")
async def admin_model_alias_policy_update(
    alias_id: str,
    payload: ModelAliasPolicyRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    policy = validate_model_alias_policy_payload(alias_id, payload)
    alias_id = policy["alias"]
    current_alias = catalog_snapshot().get_alias(alias_id)
    if not policy["enabled"]:
        active_jobs = await database.count_active_jobs_for_model(f"alias-policy:{alias_id}", [alias_id])
        active_runtime_reservations = await active_runtime_reservations_for_alias(alias_id)
        if active_jobs > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "alias has active jobs and cannot be disabled",
                    "active_jobs": active_jobs,
                    "active_runtime_reservations": active_runtime_reservations,
                },
            )
        if active_runtime_reservations:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "alias has active runtime reservations and cannot be disabled",
                    "active_jobs": active_jobs,
                    "active_runtime_reservations": active_runtime_reservations,
                },
            )
    else:
        active_runtime_reservations = await active_runtime_reservations_for_alias(alias_id)
        if current_alias is not None and active_runtime_reservations:
            resolution_changes = alias_resolution_policy_changes(
                alias_resolution_policy_snapshot(current_alias),
                alias_resolution_snapshot_from_policy(current_alias, policy),
            )
            if resolution_changes:
                raise_alias_active_reservation_policy_change(alias_id, active_runtime_reservations, resolution_changes)
    row = await database.upsert_model_alias_policy({**policy, "updated_by": auth.subject_id})
    await refresh_catalog_cache()
    await refresh_workflow_dependency_statuses()
    await record_audit_event(
        auth,
        "model_alias_policy.updated",
        target_type="model_alias",
        target_id=alias_id,
        summary=f"Updated model alias policy {alias_id}",
        metadata={
            "enabled": row["enabled"],
            "modality": row.get("modality"),
            "preferred_runtime": row.get("preferred_runtime"),
            "status": row.get("status"),
            "idle_timeout_seconds": row.get("idle_timeout_seconds"),
            "visibility_roles": row.get("visibility_roles") or [],
        },
    )
    alias = catalog_snapshot().require_alias(alias_id)
    return {"policy": public_model_alias_policy(row), "alias": alias.to_openai_model()}


@app.delete("/admin/models/aliases/{alias_id}/policy")
async def admin_model_alias_policy_delete(alias_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    alias_id = alias_id.strip()
    if not ID_PATTERN.fullmatch(alias_id):
        raise HTTPException(status_code=422, detail="alias must start with a lowercase letter or digit and contain only lowercase letters, digits, dots, underscores, and hyphens")
    current_alias = catalog_snapshot().get_alias(alias_id)
    if current_alias is None:
        row = await database.get_model_alias_policy(alias_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown model alias: {alias_id}")
    active_runtime_reservations = await active_runtime_reservations_for_alias(alias_id)
    if current_alias is not None and active_runtime_reservations:
        resolution_changes = alias_resolution_policy_changes(
            alias_resolution_policy_snapshot(current_alias),
            alias_resolution_reset_snapshot(current_alias),
        )
        if resolution_changes:
            raise_alias_active_reservation_policy_change(alias_id, active_runtime_reservations, resolution_changes)
    row = await database.delete_model_alias_policy(alias_id)
    await refresh_catalog_cache()
    await refresh_workflow_dependency_statuses()
    await record_audit_event(
        auth,
        "model_alias_policy.reset",
        target_type="model_alias",
        target_id=alias_id,
        summary=f"Reset model alias policy {alias_id}",
        metadata={"had_policy": row is not None},
    )
    alias = catalog_snapshot().get_alias(alias_id)
    return {
        "deleted": row is not None,
        "alias": alias.to_openai_model() if alias is not None else None,
        "previous_policy": public_model_alias_policy(row) if row is not None else None,
    }


@app.post("/admin/models/install-plan")
async def admin_model_install_plan(payload: ModelInstallPlanRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    manifest = await manifest_for_install_request(payload)
    require_manifest_role_action(manifest, auth, "install")
    return install_plan_for_manifest(manifest, payload)


@app.post("/admin/models/manifest-draft/huggingface-gguf")
async def admin_model_huggingface_gguf_manifest_draft(
    payload: HuggingFaceGgufManifestDraftRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    try:
        source = model_lifecycle.huggingface_file_source(payload.url)
    except model_lifecycle.ModelLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    metadata = await fetch_huggingface_gguf_metadata(source["resolve_url"])
    return build_huggingface_gguf_manifest_draft(payload, source, metadata)


@app.post("/admin/models/download-plan")
async def admin_model_download_plan(payload: ModelInstallPlanRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    manifest = await manifest_for_install_request(payload)
    require_manifest_role_action(manifest, auth, "install")
    return download_plan_for_manifest(
        manifest,
        accept_license=payload.accept_license,
        allow_resource_override=payload.allow_resource_override,
        allow_download_override=payload.allow_download_override,
    )


@app.get("/admin/models/downloads")
async def admin_model_downloads(authorization: str | None = Header(default=None), limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    return {"object": "list", "data": [public_model_download(row) for row in await database.list_model_downloads(limit=limit)]}


@app.get("/admin/models/downloads/{download_id}")
async def admin_model_download_get(download_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    row = await database.get_model_download(download_id)
    if row is None:
        raise HTTPException(status_code=404, detail="model download not found")
    return public_model_download(row)


@app.post("/admin/models/downloads")
async def admin_model_download_create(payload: ModelDownloadCreate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    manifest = await manifest_for_install_request(payload)
    require_manifest_role_action(manifest, auth, "install")
    plan = download_plan_for_manifest(
        manifest,
        accept_license=payload.accept_license,
        allow_resource_override=payload.allow_resource_override,
        allow_download_override=payload.allow_download_override,
    )
    credential_secret_name = await validate_model_download_secret_name(payload.credential_secret_name)
    if not payload.confirm:
        raise HTTPException(status_code=409, detail={"message": "download requires explicit confirmation", "plan": plan})
    if not plan.get("can_download"):
        raise HTTPException(status_code=409, detail={"message": "; ".join(plan.get("blockers") or []) or "download is blocked", "plan": plan})
    status = "completed" if plan.get("already_available") else "queued"
    download_id = f"modeldl_{uuid.uuid4().hex}"
    row = await database.insert_model_download(
        {
            "id": download_id,
            "owner_id": auth.subject_id,
            "model_id": manifest.id,
            "model_version": manifest.version,
            "source_url": plan["source_url"],
            "target_sha256": plan["target_sha256"],
            "target_size_bytes": plan["target_size_bytes"],
            "bytes_downloaded": plan["target_size_bytes"] if status == "completed" else plan.get("existing_partial_bytes", 0),
            "status": status,
            "stage": "already_available" if status == "completed" else "queued",
            "manifest": manifest.to_dict(),
            "credential_secret_name": credential_secret_name,
            "license_accepted": bool(plan.get("license_accepted")),
        }
    )
    await record_audit_event(
        auth,
        "model_download.created",
        target_type="model_download",
        target_id=download_id,
        summary=f"Queued model download {manifest.id}@{manifest.version}",
        metadata={
            "model_ref": f"{manifest.id}@{manifest.version}",
            "source_type": manifest.source.type,
            "target_sha256": plan["target_sha256"],
            "target_size_bytes": plan["target_size_bytes"],
            "already_available": plan.get("already_available", False),
            "authenticated": bool(credential_secret_name),
            "resource_override": bool(plan.get("resource_override")),
            "download_override": bool(plan.get("download_override")),
            "warning_count": len(plan.get("warnings") or []),
        },
    )
    return {"download": public_model_download(row), "plan": plan}


@app.delete("/admin/models/downloads/{download_id}")
async def admin_model_download_cancel(download_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    row = await database.request_model_download_cancel(download_id)
    if row is None:
        raise HTTPException(status_code=404, detail="model download not found")
    await record_audit_event(
        auth,
        "model_download.cancel_requested",
        target_type="model_download",
        target_id=download_id,
        summary=f"Requested cancellation for model download {download_id}",
        metadata={"status": row["status"], "model_ref": f"{row['model_id']}@{row['model_version']}"},
    )
    return public_model_download(row)


@app.post("/admin/models/downloads/{download_id}/pause")
async def admin_model_download_pause(download_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    try:
        row = await database.request_model_download_pause(download_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="model download not found")
    await record_audit_event(
        auth,
        "model_download.pause_requested",
        target_type="model_download",
        target_id=download_id,
        summary=f"Requested pause for model download {download_id}",
        metadata={"status": row["status"], "model_ref": f"{row['model_id']}@{row['model_version']}", "bytes_downloaded": row.get("bytes_downloaded")},
    )
    return public_model_download(row)


@app.post("/admin/models/downloads/{download_id}/resume")
async def admin_model_download_resume(download_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    try:
        row = await database.resume_model_download(download_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="model download not found")
    await record_audit_event(
        auth,
        "model_download.resume_requested",
        target_type="model_download",
        target_id=download_id,
        summary=f"Resumed model download {download_id}",
        metadata={"status": row["status"], "model_ref": f"{row['model_id']}@{row['model_version']}", "bytes_downloaded": row.get("bytes_downloaded")},
    )
    return public_model_download(row)


@app.post("/admin/models/downloads/{download_id}/retry")
async def admin_model_download_retry(download_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    try:
        row = await database.retry_model_download(download_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="model download not found")
    await record_audit_event(
        auth,
        "model_download.retry_requested",
        target_type="model_download",
        target_id=download_id,
        summary=f"Requeued model download {download_id}",
        metadata={
            "status": row["status"],
            "model_ref": f"{row['model_id']}@{row['model_version']}",
            "bytes_downloaded": row.get("bytes_downloaded"),
            "target_size_bytes": row.get("target_size_bytes"),
        },
    )
    return public_model_download(row)


@app.post("/admin/models/downloads/{download_id}/install")
async def admin_model_download_install(
    download_id: str,
    payload: ModelDownloadInstallRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    row = await database.get_model_download(download_id)
    if row is None:
        raise HTTPException(status_code=404, detail="model download not found")
    if row.get("status") != "completed":
        raise HTTPException(status_code=409, detail={"message": "model download is not completed", "download": public_model_download(row)})
    manifest = parse_download_manifest(row)
    install_payload = ModelInstallRequest(
        manifest=manifest.to_dict(),
        confirm=payload.confirm,
        accept_license=payload.accept_license,
        allow_resource_override=payload.allow_resource_override,
        smoke_test=payload.smoke_test,
    )
    return await install_model_manifest(
        manifest=manifest,
        payload=install_payload,
        auth=auth,
        source_download=row,
    )


@app.post("/admin/models/install")
async def admin_model_install(payload: ModelInstallRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    manifest = await manifest_for_install_request(payload)
    return await install_model_manifest(manifest=manifest, payload=payload, auth=auth)


@app.post("/admin/models/{model_id}/versions/{version}/smoke-test")
async def admin_model_smoke_test(
    model_id: str,
    version: str,
    payload: ModelSmokeTestRequest | None = Body(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    request = payload or ModelSmokeTestRequest()
    row = await database.get_model_record(model_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="installed model record not found")
    result = await smoke_test_model_record(row, auth, persist=request.persist, model_alias=request.model_alias)
    return {
        "model": public_model_record(result["model_record"]),
        "smoke_test": result["smoke_test"],
        "measurement": result["measurement"],
        "persisted": result["persisted"],
    }


@app.get("/admin/models/{model_id}/versions/{version}/removal-plan")
async def admin_model_removal_plan(model_id: str, version: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    row = await database.get_model_record(model_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="installed model record not found")
    _, dependencies = await model_dependency_context_for_record(row)
    return model_record_quarantine_plan(row, dependencies)


@app.delete("/admin/models/{model_id}/versions/{version}")
async def admin_model_remove(
    model_id: str,
    version: str,
    payload: ModelRemoveRequest | None = Body(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    request = payload or ModelRemoveRequest()
    row = await database.get_model_record(model_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="installed model record not found")
    manifest, dependencies = await model_dependency_context_for_record(row)
    plan = model_record_quarantine_plan(row, dependencies)
    if dependencies["active_jobs"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "model is referenced by active jobs",
                **plan,
            },
        )
    if dependencies["active_voice_profiles"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "model is referenced by active voice profiles",
                **plan,
            },
        )
    if dependencies["active_runtime_reservations"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "model is referenced by active runtime reservations",
                **plan,
            },
        )
    if not request.confirm:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "model removal requires explicit confirmation",
                **plan,
            },
        )
    try:
        view_quarantine = model_lifecycle.quarantine_runtime_views(manifest, data_root_path())
    except model_lifecycle.ModelLifecycleError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), **plan}) from exc
    updated = await database.quarantine_model_record(model_id, version)
    if updated is None:
        raise HTTPException(status_code=404, detail="installed model record not found")
    await refresh_catalog_cache()
    workflows = await refresh_workflow_dependency_statuses()
    await record_audit_event(
        auth,
        "model.quarantined",
        target_type="model",
        target_id=dependencies["model_ref"],
        summary=f"Quarantined model {dependencies['model_ref']}",
        metadata={
            "aliases": manifest.aliases,
            "dependent_workflows": dependencies["dependent_workflows"],
            "dependent_model_profiles": dependencies["dependent_model_profiles"],
            "dependent_voice_profiles": dependencies["dependent_voice_profiles"],
            "active_runtime_reservations": dependencies["active_runtime_reservations"],
            "runtime_view_quarantine": view_quarantine,
            "workflow_dependencies_refreshed": workflows["count"],
        },
    )
    return {
        **plan,
        "status": "quarantined",
        "can_quarantine": True,
        "blockers": [],
        "model_status": updated.get("status"),
        "model": public_model_record(updated),
        "runtime_view_quarantine": view_quarantine,
        "workflow_refresh": workflows,
    }


@app.get("/admin/models/{model_id}/versions/{version}/blob-quarantine-plan")
async def admin_model_blob_quarantine_plan(model_id: str, version: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    row = await database.get_model_record(model_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="model record not found")
    manifest, dependencies = await model_dependency_context_for_record(row)
    records = await database.list_model_records()
    plan = model_lifecycle.build_blob_quarantine_plan(manifest, data_root_path(), records, model_status=row["status"])
    if dependencies["active_jobs"]:
        plan = {
            **plan,
            "status": "blocked",
            "can_quarantine": False,
            "blockers": [*plan.get("blockers", []), "model is referenced by active jobs"],
        }
    if dependencies["active_voice_profiles"]:
        plan = {
            **plan,
            "status": "blocked",
            "can_quarantine": False,
            "blockers": [*plan.get("blockers", []), "model is referenced by active voice profiles"],
        }
    if dependencies["active_runtime_reservations"]:
        plan = {
            **plan,
            "status": "blocked",
            "can_quarantine": False,
            "blockers": [*plan.get("blockers", []), "model is referenced by active runtime reservations"],
        }
    return {
        **jsonable_encoder(plan),
        "active_jobs": dependencies["active_jobs"],
        "dependent_workflows": dependencies["dependent_workflows"],
        "dependent_model_profiles": dependencies["dependent_model_profiles"],
        "dependent_voice_profiles": dependencies["dependent_voice_profiles"],
        "active_voice_profiles": dependencies["active_voice_profiles"],
        "active_runtime_reservations": dependencies["active_runtime_reservations"],
    }


@app.post("/admin/models/{model_id}/versions/{version}/blobs/quarantine")
async def admin_model_blob_quarantine(
    model_id: str,
    version: str,
    payload: ModelBlobQuarantineRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    row = await database.get_model_record(model_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="model record not found")
    manifest, dependencies = await model_dependency_context_for_record(row)
    if dependencies["active_jobs"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "model is referenced by active jobs",
                **dependencies,
            },
        )
    if dependencies["active_voice_profiles"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "model is referenced by active voice profiles",
                **dependencies,
            },
        )
    if dependencies["active_runtime_reservations"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "model is referenced by active runtime reservations",
                **dependencies,
            },
        )
    records = await database.list_model_records()
    try:
        result = model_lifecycle.quarantine_authoritative_blobs(
            manifest,
            data_root_path(),
            records,
            model_status=row["status"],
            confirmed=payload.confirm,
        )
    except model_lifecycle.ModelLifecycleError as exc:
        plan = model_lifecycle.build_blob_quarantine_plan(manifest, data_root_path(), records, model_status=row["status"])
        raise HTTPException(status_code=409, detail={"message": str(exc), "plan": {**jsonable_encoder(plan), **dependencies}}) from exc
    await record_audit_event(
        auth,
        "model.blobs_quarantined",
        target_type="model",
        target_id=dependencies["model_ref"],
        summary=f"Quarantined authoritative blobs for {dependencies['model_ref']}",
        metadata={
            "model_status": row["status"],
            "moved": [{"sha256": item["sha256"], "quarantine_path": item["quarantine_path"], "size_bytes": item["size_bytes"]} for item in result["moved"]],
            "total_size_bytes": result["total_size_bytes"],
            "dependent_workflows": dependencies["dependent_workflows"],
            "dependent_model_profiles": dependencies["dependent_model_profiles"],
            "dependent_voice_profiles": dependencies["dependent_voice_profiles"],
            "active_runtime_reservations": dependencies["active_runtime_reservations"],
        },
    )
    return {
        **jsonable_encoder(result),
        "active_jobs": dependencies["active_jobs"],
        "dependent_workflows": dependencies["dependent_workflows"],
        "dependent_model_profiles": dependencies["dependent_model_profiles"],
        "dependent_voice_profiles": dependencies["dependent_voice_profiles"],
        "active_voice_profiles": dependencies["active_voice_profiles"],
        "active_runtime_reservations": dependencies["active_runtime_reservations"],
    }


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    catalog = catalog_snapshot()
    return {
        "object": "list",
        "data": [
            alias.to_openai_model()
            for alias in catalog.list_aliases()
            if alias.alias.enabled and alias_visible_to_auth(alias, auth)
        ],
    }


@app.get("/v1/tools")
async def list_model_tools(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    return await public_model_tool_registry(auth)


@app.get("/admin/model-tools")
async def admin_model_tools(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:read")
    require_model_admin(auth)
    return await public_model_tool_registry(auth, include_disabled_custom=True)


@app.put("/admin/model-tools/policy")
async def admin_model_tool_policy_update(payload: ModelToolPolicyRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_administrator(auth, "model tool policy changes require administrator role")
    normalized = validate_model_tool_policy_payload(payload)
    row = await database.upsert_model_tool_policy({**normalized, "updated_by": auth.subject_id})
    public = await public_model_tool_registry(auth, include_disabled_custom=True)
    await record_audit_event(
        auth,
        "model_tool_policy.updated",
        target_type="model_tool_policy",
        target_id="default",
        summary="Updated model tool policy",
        metadata={
            "enabled": bool(row.get("enabled")),
            "allowed_tools": row.get("allowed_tools") or [],
            "allow_private_network": bool(row.get("allow_private_network")),
            "allowed_host_count": len(row.get("allowed_hosts") or []),
            "max_result_chars": row.get("max_result_chars"),
            "max_search_results": row.get("max_search_results"),
        },
    )
    return public


@app.put("/admin/model-tools/{name}")
async def admin_model_tool_definition_update(name: str, payload: ModelToolDefinitionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_administrator(auth, "model tool definition changes require administrator role")
    normalized = validate_model_tool_definition_payload(name, payload)
    await validate_model_tool_secret_references(normalized["config"])
    row = await database.upsert_model_tool_definition({**normalized, "updated_by": auth.subject_id})
    auth_config = row.get("config", {}).get("auth") if isinstance(row.get("config"), dict) else None
    await record_audit_event(
        auth,
        "model_tool_definition.upserted",
        target_type="model_tool",
        target_id=row["name"],
        summary=f"Updated model tool definition {row['name']}",
        metadata={
            "name": row["name"],
            "enabled": bool(row.get("enabled")),
            "kind": row.get("kind"),
            "visibility_roles": row.get("visibility_roles") or [],
            "method": (row.get("config") or {}).get("method"),
            "url_configured": bool((row.get("config") or {}).get("url")),
            "auth_type": str(auth_config.get("type") or "none") if isinstance(auth_config, dict) else "none",
            "auth_secret_configured": bool(auth_config.get("secret_name")) if isinstance(auth_config, dict) else False,
            "auth_header": str(auth_config.get("header_name") or "") if isinstance(auth_config, dict) and auth_config.get("type") == "header" else "",
        },
    )
    return {"tool": public_model_tool_definition(row), "registry": await public_model_tool_registry(auth, include_disabled_custom=True)}


@app.post("/admin/model-tools/{name}/execute")
async def admin_model_tool_execute(name: str, payload: ModelToolExecuteRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_model_admin(auth)
    tool_name = normalize_model_tool_execution_name(name)
    registry = await model_tool_registry_for_auth(auth)
    if tool_name not in registry.tool_names():
        raise HTTPException(status_code=404, detail="model tool is not enabled or not visible to this role")
    result = await registry.execute(tool_name, payload.arguments)
    await record_audit_event(
        auth,
        "model_tool.executed",
        target_type="model_tool",
        target_id=tool_name,
        summary=f"Executed model tool {tool_name}",
        metadata={
            "name": tool_name,
            "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
            "argument_keys": sorted(str(key) for key in payload.arguments.keys()),
        },
    )
    return {"tool": tool_name, "result": jsonable_encoder(result)}


@app.delete("/admin/model-tools/{name}")
async def admin_model_tool_definition_delete(name: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "models:write")
    require_administrator(auth, "model tool definition changes require administrator role")
    tool_name = normalize_model_tool_name(name)
    row = await database.delete_model_tool_definition(tool_name)
    if row is None:
        raise HTTPException(status_code=404, detail="model tool definition not found")
    await record_audit_event(
        auth,
        "model_tool_definition.deleted",
        target_type="model_tool",
        target_id=tool_name,
        summary=f"Deleted model tool definition {tool_name}",
        metadata={"name": tool_name, "kind": row.get("kind"), "enabled": bool(row.get("enabled"))},
    )
    return {"deleted": public_model_tool_definition(row), "registry": await public_model_tool_registry(auth, include_disabled_custom=True)}


@app.post("/v1/chat/completions")
async def chat_completions(payload: ChatCompletionRequest, authorization: str | None = Header(default=None)) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    require_not_in_maintenance("chat/completions")
    resolution = resolve_catalog_alias_for_modalities_auth(payload.model, {"llm", "vlm"}, auth, payload.runtime_policy, operation="chat")
    require_openai_forwarding(resolution, "chat")
    runtime_payload = strip_b1_chat_fields(payload.model_dump(exclude_none=True))
    requested_tools, tool_registry = await requested_b1_model_tools(payload, auth)
    if chat_request_explicit_b1_tool_names(payload) and not requested_tools:
        raise HTTPException(status_code=422, detail="no requested B1 model tools are enabled")
    if requested_tools:
        tool_loop_payload = dict(runtime_payload)
        # Intermediate tool calls must be complete JSON messages. The public
        # response is converted to SSE after the loop finishes.
        tool_loop_payload["stream"] = False
        tool_response = await call_chat_with_b1_tools(
            payload,
            tool_loop_payload,
            resolution,
            requested_tools,
            tool_registry,
            owner_id=auth.subject_id,
        )
        return chat_tool_response_to_stream(tool_response) if payload.stream else tool_response
    if payload.stream:
        proxied_stream = await call_openai_runtime_stream("/v1/chat/completions", runtime_payload, resolution, "chat", owner_id=auth.subject_id)
        if proxied_stream is not None:
            return proxied_stream
    else:
        proxied = await call_openai_runtime_json("/v1/chat/completions", runtime_payload, resolution, "chat", owner_id=auth.subject_id)
        if proxied is not None:
            return proxied
    raise HTTPException(status_code=502, detail=f"runtime {resolution.runtime} did not produce a proxied chat response")


@app.post("/v1/responses")
async def responses(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    require_not_in_maintenance("responses")
    model = payload.get("model", "chat-default")
    resolution = resolve_catalog_alias_for_modalities_auth(model, {"llm", "vlm"}, auth, payload.get("runtime_policy", "any"), operation="responses")
    require_openai_forwarding(resolution, "responses")
    if payload.get("b1_tools") or ("b1_tools" not in payload and auth.default_b1_tools):
        chat_payload = responses_payload_to_chat_request_payload({"model": model, **payload})
        chat_request = ChatCompletionRequest(**chat_payload)
        requested_tools, tool_registry = await requested_b1_model_tools(chat_request, auth)
        if chat_request_explicit_b1_tool_names(chat_request) and not requested_tools:
            raise HTTPException(status_code=422, detail="no requested B1 model tools are enabled")
        if not requested_tools:
            proxied = await call_openai_runtime_json(
                "/v1/responses",
                strip_b1_response_fields({"model": model, **payload}),
                resolution,
                "responses",
                owner_id=auth.subject_id,
            )
            if proxied is not None:
                return proxied
            raise HTTPException(status_code=502, detail=f"runtime {resolution.runtime} did not produce a proxied responses response")
        if chat_request.stream:
            raise HTTPException(status_code=422, detail="B1 model tools currently require non-streaming responses")
        chat_response = await call_chat_with_b1_tools(
            chat_request,
            strip_b1_chat_fields(chat_request.model_dump(exclude_none=True)),
            resolution,
            requested_tools,
            tool_registry,
            owner_id=auth.subject_id,
        )
        return chat_tool_response_to_responses_response(payload, chat_response)
    proxied = await call_openai_runtime_json(
        "/v1/responses",
        strip_b1_response_fields({"model": model, **payload}),
        resolution,
        "responses",
        owner_id=auth.subject_id,
    )
    if proxied is not None:
        return proxied
    raise HTTPException(status_code=502, detail=f"runtime {resolution.runtime} did not produce a proxied responses response")


@app.post("/v1/embeddings")
async def embeddings(payload: EmbeddingRequest, authorization: str | None = Header(default=None)) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    require_not_in_maintenance("embeddings")
    resolution = resolve_catalog_alias_for_auth(payload.model, "embedding", auth, operation="embeddings")
    require_openai_forwarding(resolution, "embeddings")
    proxied = await call_openai_runtime_json("/v1/embeddings", payload.model_dump(exclude_none=True), resolution, "embeddings", owner_id=auth.subject_id)
    if proxied is not None:
        return proxied
    raise HTTPException(status_code=502, detail=f"runtime {resolution.runtime} did not produce a proxied embeddings response")


@app.post("/v1/audio/speech")
async def audio_speech(request: Request, authorization: str | None = Header(default=None)) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    require_not_in_maintenance("audio/speech")
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="audio speech request body must be a JSON object") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="audio speech request body must be a JSON object")
    voice_profile = await voice_profile_for_inference(body, auth)
    explicit_model = body.get("model") if isinstance(body.get("model"), str) and body.get("model") else ""
    model = explicit_model or (str(voice_profile.get("model_alias") or "") if voice_profile else "") or "tts-fast"
    runtime_policy = body.get("runtime_policy") if isinstance(body.get("runtime_policy"), str) else "any"
    resolution = resolve_catalog_alias_for_auth(model, "tts", auth, runtime_policy, operation="text-to-speech")
    enforce_voice_profile_matches_resolution(voice_profile, resolution)
    forwarded = {key: value for key, value in body.items() if key != "runtime_policy" and not key.startswith("b1_") and value is not None}
    forwarded["model"] = resolution.model_id
    forwarded = apply_voice_profile_to_runtime_payload(forwarded, voice_profile)
    if resolution.runtime == "audio-cpu":
        forwarded["b1_resolved_model_version"] = resolution.resolved_model_version
        forwarded["b1_model_alias"] = resolution.public_alias
        forwarded["b1_cpu_residency_allowed"] = resolution.cpu_resident_allowed
        forwarded_body = json.dumps(forwarded, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return await proxy_http_bytes(settings.audio_cpu_url, "/v1/audio/speech", request, body=forwarded_body)
    adapter = runtime_registry_snapshot().adapter(resolution.runtime)
    if resolution.runtime == "voicebox":
        base_url = settings.voicebox_url
    elif adapter is not None and adapter.openai_compatible:
        base_url = adapter.base_url
    else:
        raise HTTPException(status_code=422, detail=f"runtime {resolution.runtime} does not support OpenAI-compatible speech forwarding")
    if adapter is None or not adapter.external:
        forwarded["b1_resolved_model_version"] = resolution.resolved_model_version
    forwarded_body = json.dumps(forwarded, separators=(",", ":"), sort_keys=True).encode("utf-8")
    lease_owner = await acquire_inference_lease(resolution, "audio-speech", owner_id=auth.subject_id)
    prepared = False
    try:
        prepared = await prepare_sync_gpu_runtime(resolution, "audio-speech")
        if adapter is not None and adapter.openai_compatible:
            return await await_with_inference_lease_renewal(
                lease_owner,
                "audio-speech",
                proxy_http_bytes_to_url(
                    adapter.openai_url("/v1/audio/speech"),
                    request,
                    body=forwarded_body,
                    timeout_seconds=1800.0,
                    extra_headers=adapter.request_headers(),
                ),
            )
        return await await_with_inference_lease_renewal(
            lease_owner,
            "audio-speech",
            proxy_http_bytes(
                base_url,
                "/v1/audio/speech",
                request,
                body=forwarded_body,
                timeout_seconds=1800.0,
            )
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        if prepared:
            with suppress(Exception):
                await mark_sync_gpu_runtime_idle(resolution, "audio-speech")
        await release_inference_lease(lease_owner)


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request, authorization: str | None = Header(default=None)) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    require_not_in_maintenance("audio/transcriptions")
    payload = await transcription_input_from_request(request)
    model = payload.get("model") if isinstance(payload.get("model"), str) and payload.get("model") else request.headers.get("X-B1-Model", "stt-default")
    runtime_policy = payload.get("runtime_policy") if isinstance(payload.get("runtime_policy"), str) else "any"
    resolution = resolve_catalog_alias_for_auth(model, "stt", auth, runtime_policy, operation="transcription")
    if resolution.runtime != "audio-cpu":
        raise HTTPException(status_code=422, detail=f"runtime {resolution.runtime} does not support CPU transcription forwarding")
    forwarded = {key: value for key, value in payload.items() if key != "runtime_policy" and not key.startswith("b1_") and value is not None}
    forwarded["model"] = resolution.model_id
    forwarded["b1_resolved_model_version"] = resolution.resolved_model_version
    forwarded["b1_model_alias"] = resolution.public_alias
    forwarded["b1_cpu_residency_allowed"] = resolution.cpu_resident_allowed
    forwarded_body = json.dumps(forwarded, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return await proxy_http_bytes(
        settings.audio_cpu_url,
        "/v1/audio/transcriptions",
        request,
        body=forwarded_body,
        extra_headers={"content-type": "application/json"},
    )


@app.post("/v1/images/generations", status_code=202)
async def image_generations(
    payload: dict[str, Any],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    model = media_job_string_extension(payload, "model", "image-default")
    runtime_policy = media_job_string_extension(payload, "runtime_policy", "any")
    priority = media_job_string_extension(payload, "priority", "single_image")
    job_payload = MediaJobCreate(
        modality="image",
        operation="generation",
        model=model,
        input=payload,
        priority=priority,
        runtime_policy=runtime_policy,
    )
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
    if normalized_idempotency_key:
        existing = await database.get_job_by_idempotency_key(auth.subject_id, normalized_idempotency_key)
        if existing is not None:
            ensure_idempotent_job_matches(existing, job_payload)
            return openai_image_job_response(existing)
    require_not_in_maintenance("images/generations")
    resolution = resolve_catalog_alias_for_auth(model, "image", auth, runtime_policy, operation="image-generation")
    job = await create_job_record(
        auth.subject_id,
        job_payload,
        idempotency_key=normalized_idempotency_key,
        resolution=resolution,
    )
    return openai_image_job_response(job)


@app.post("/v1/images/edits", status_code=202)
async def image_edits(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
    if normalized_idempotency_key:
        existing = await database.get_job_by_idempotency_key(auth.subject_id, normalized_idempotency_key)
        if existing is not None:
            ensure_existing_job_endpoint(existing, modality="image", operation="edit")
            return openai_image_job_response(existing)
    require_not_in_maintenance("images/edits")
    payload = await image_edit_input_from_request(request, auth)
    model = payload.get("model") if isinstance(payload.get("model"), str) and payload.get("model") else "image-edit"
    runtime_policy = payload.get("runtime_policy") if isinstance(payload.get("runtime_policy"), str) else "any"
    priority = media_job_string_extension(payload, "priority", "single_image")
    resolution = resolve_catalog_alias_for_auth(model, "image", auth, runtime_policy, operation="image-edit")
    job = await create_job_record(
        auth.subject_id,
        MediaJobCreate(modality="image", operation="edit", model=model, input=payload, priority=priority, runtime_policy=runtime_policy),
        idempotency_key=normalized_idempotency_key,
        resolution=resolution,
    )
    return openai_image_job_response(job)


@app.post("/v1/videos/generations", status_code=202)
async def video_generations(
    payload: dict[str, Any],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    model = media_job_string_extension(payload, "model", "video-text")
    runtime_policy = media_job_string_extension(payload, "runtime_policy", "any")
    priority = media_job_string_extension(payload, "priority", "video")
    job_payload = MediaJobCreate(
        modality="video",
        operation="text-to-video",
        model=model,
        input=payload,
        priority=priority,
        runtime_policy=runtime_policy,
    )
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
    if normalized_idempotency_key:
        existing = await database.get_job_by_idempotency_key(auth.subject_id, normalized_idempotency_key)
        if existing is not None:
            ensure_idempotent_job_matches(existing, job_payload)
            return openai_image_job_response(existing)
    require_not_in_maintenance("videos/generations")
    resolution = resolve_catalog_alias_for_auth(model, "video", auth, runtime_policy, operation="text-to-video")
    job = await create_job_record(
        auth.subject_id,
        job_payload,
        idempotency_key=normalized_idempotency_key,
        resolution=resolution,
    )
    return openai_image_job_response(job)


@app.post("/v1/videos/image-to-video", status_code=202)
async def image_to_video(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "inference:write")
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
    if normalized_idempotency_key:
        existing = await database.get_job_by_idempotency_key(auth.subject_id, normalized_idempotency_key)
        if existing is not None:
            ensure_existing_job_endpoint(existing, modality="video", operation="image-to-video")
            return openai_image_job_response(existing)
    require_not_in_maintenance("videos/image-to-video")
    payload = await media_image_input_from_request(request, auth)
    model = payload.get("model") if isinstance(payload.get("model"), str) and payload.get("model") else "video-image"
    runtime_policy = payload.get("runtime_policy") if isinstance(payload.get("runtime_policy"), str) else "any"
    priority = media_job_string_extension(payload, "priority", "video")
    resolution = resolve_catalog_alias_for_auth(model, "video", auth, runtime_policy, operation="image-to-video")
    job = await create_job_record(
        auth.subject_id,
        MediaJobCreate(modality="video", operation="image-to-video", model=model, input=payload, priority=priority, runtime_policy=runtime_policy),
        idempotency_key=normalized_idempotency_key,
        resolution=resolution,
    )
    return openai_image_job_response(job)


@app.post("/v1/media/uploads")
async def media_upload_create(
    request: Request,
    authorization: str | None = Header(default=None),
    x_b1_field: str | None = Header(default=None, alias="X-B1-Field"),
    x_b1_filename: str | None = Header(default=None, alias="X-B1-Filename"),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:write")
    require_not_in_maintenance("media/upload")
    body = await read_bounded_request_body(request, settings.upload_max_bytes)
    reference = stage_media_input(
        auth,
        field_name=x_b1_field or "media",
        content=body,
        declared_mime_type=request.headers.get("content-type"),
        filename=x_b1_filename,
    )
    return {"input": reference, "reference": reference}


@app.post("/v1/media/jobs", status_code=202)
async def media_job_create(
    payload: MediaJobCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:write")
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
    if normalized_idempotency_key:
        existing = await database.get_job_by_idempotency_key(auth.subject_id, normalized_idempotency_key)
        if existing is not None:
            ensure_idempotent_job_matches(existing, payload)
            return public_job(existing)
    require_not_in_maintenance("media/jobs")
    workflow = await enforce_workflow_backed_media_job(auth, payload)
    resolution = resolve_catalog_alias_for_auth(payload.model, payload.modality, auth, payload.runtime_policy, operation=payload.operation)
    enforce_workflow_backend_policy(workflow, resolution)
    if payload.modality == "tts" and payload.operation in {"speech", "text-to-speech", "tts"}:
        enforce_voice_profile_matches_resolution(await voice_profile_for_inference(payload.input, auth), resolution)
    job = await create_job_record(auth.subject_id, payload, idempotency_key=normalized_idempotency_key, resolution=resolution)
    return public_job(job)


@app.get("/v1/media/jobs")
async def media_jobs(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    state: JobState | None = Query(default=None),
    runtime: str | None = Query(default=None, max_length=64),
    modality: str | None = Query(default=None, max_length=64),
    native_prompt_id: str | None = Query(default=None, min_length=1, max_length=128),
) -> list[dict[str, Any]]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:read")
    owner_id = None if auth.has_scope("*") else auth.subject_id
    state = optional_query_value(state)
    runtime = optional_query_value(runtime)
    modality = optional_query_value(modality)
    native_prompt_id = optional_query_value(native_prompt_id)
    filters: dict[str, Any] = {
        "limit": limit,
        "owner_id": owner_id,
        "state": state.value if state else None,
        "runtime": runtime,
        "modality": modality,
    }
    if native_prompt_id:
        filters["native_prompt_id"] = native_prompt_id
    return public_jobs(await database.list_jobs(**filters))


@app.get("/v1/media/jobs/{job_id}")
async def media_job_get(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:read")
    job = await database.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    require_job_owner_or_admin(auth, job)
    return public_job(job)


@app.delete("/v1/media/jobs/{job_id}")
async def media_job_cancel(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:write")
    job = await database.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    require_job_owner_or_admin(auth, job)
    updated = await database.request_job_cancel(job_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.get("state") not in TERMINAL_JOB_STATES:
        await record_audit_event(
            auth,
            "job.cancelled" if updated["state"] == JobState.CANCELLED.value else "job.cancelling",
            target_type="job",
            target_id=job_id,
            summary=f"Requested cancellation for job {job_id}",
            metadata={"previous_state": job["state"], "state": updated["state"], "runtime": updated.get("runtime"), "model_alias": updated.get("model_alias")},
            correlation_id=updated.get("correlation_id"),
        )
    return public_job(updated)


@app.get(
    "/v1/media/jobs/{job_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def media_job_events(
    job_id: str,
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:read")
    initial_job = await database.get_job(job_id)
    if initial_job is None:
        raise HTTPException(status_code=404, detail="job not found")
    require_job_owner_or_admin(auth, initial_job)
    return job_event_stream(job_id, lambda current: subject_can_read_job(auth, current), last_event_id=last_event_id)


def job_event_stream(
    job_id: str,
    can_read_job: Callable[[dict[str, Any]], bool],
    *,
    poll_interval_seconds: float = 1.0,
    max_events: int | None = None,
    last_event_id: str | None = None,
) -> StreamingResponse:
    resume_after_event_id = str(last_event_id or "").strip()

    async def events():
        emitted = 0
        while True:
            job = await database.get_job(job_id)
            if job is None:
                yield format_sse_event("error", {"error": "job not found"})
                return
            if not can_read_job(job):
                yield format_sse_event("error", {"error": "job belongs to a different owner"})
                return
            event_id = job_event_id(job)
            if not should_emit_job_snapshot(job, resume_after_event_id, TERMINAL_JOB_STATES):
                yield format_sse_comment("heartbeat")
                await asyncio.sleep(max(0.0, poll_interval_seconds))
                continue
            yield format_sse_event("job", public_job(job), event_id=event_id, retry_ms=1000)
            emitted += 1
            if job["state"] in TERMINAL_JOB_STATES:
                return
            if max_events is not None and emitted >= max_events:
                return
            await asyncio.sleep(max(0.0, poll_interval_seconds))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/v1/media/jobs/{job_id}/artifacts")
async def media_job_artifacts(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:read")
    job = await database.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    require_job_owner_or_admin(auth, job)
    return {"job_id": job_id, "artifacts": job.get("artifacts", [])}


@app.get("/artifacts/{artifact_path:path}")
@app.head("/artifacts/{artifact_path:path}")
async def artifact_download(artifact_path: str, request: Request, authorization: str | None = Header(default=None)) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "jobs:read")
    try:
        artifact_url = artifact_policy.artifact_url_for_path(artifact_path)
    except artifact_policy.ArtifactAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = await database.get_job_by_artifact_url(artifact_url)
    if job is None or not artifact_policy.job_has_artifact_url(job, artifact_url):
        raise HTTPException(status_code=404, detail="artifact not found")
    if not artifact_policy.subject_can_read_job_artifact(auth.subject_id, auth.scopes, job, auth.role.value):
        raise HTTPException(status_code=403, detail="artifact belongs to a different owner")
    artifact = job_artifact_by_url(job, artifact_url)
    if artifact and (artifact.get("deleted_at") or artifact.get("retention_status") == "deleted"):
        raise HTTPException(status_code=410, detail="artifact was removed by retention cleanup")
    comfyui_view_path = comfyui_view_path_for_artifact(artifact or {})
    if comfyui_view_path:
        return await proxy_http_bytes(settings.comfyui_url, comfyui_view_path, request, b"")
    return await proxy_http(settings.artifact_base_url, artifact_url, request, extra_headers=artifact_server_auth_headers())


@app.post("/v1/runtime-reservations")
async def runtime_reservation_create(
    payload: RuntimeReservationCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
    if normalized_idempotency_key:
        existing = await database.get_runtime_reservation_by_idempotency_key(auth.subject_id, normalized_idempotency_key)
        if existing is not None:
            ensure_idempotent_runtime_reservation_matches(existing, payload)
            return public_runtime_reservation(existing)
    identity = expected_runtime_reservation_identity(payload)
    runtime = identity["runtime"]
    model_alias = identity["model_alias"]
    reason = identity["reason"]
    require_not_in_maintenance("runtime/reservation")
    alias = require_catalog_alias(model_alias, auth=auth)
    registry = runtime_registry_snapshot()
    if runtime not in registry.adapters:
        raise HTTPException(status_code=422, detail=f"unknown runtime adapter: {runtime}")
    adapter = registry.adapters[runtime]
    if not adapter.configured:
        raise HTTPException(status_code=422, detail=f"runtime adapter {runtime} is not configured")
    if runtime not in GPU_RUNTIMES or not adapter.requires_gpu:
        raise HTTPException(status_code=422, detail=f"runtime {runtime} does not participate in GPU scheduler reservations")
    if runtime not in alias.runtimes:
        raise HTTPException(status_code=422, detail=f"model alias {model_alias} is not compatible with runtime {runtime}")
    if adapter.external and not settings.allow_external_providers:
        raise HTTPException(status_code=422, detail=f"runtime {runtime} is external and external providers are disabled")
    resolved_model_version = f"{alias.manifest.id}@{alias.manifest.version}"
    await enforce_gpu_hardware_admission(
        RuntimeResolution(
            public_alias=model_alias,
            model_id=alias.manifest.id,
            model_version=alias.manifest.version,
            resolved_model_version=resolved_model_version,
            runtime=runtime,
            preferred_runtime=getattr(alias, "preferred_runtime", runtime),
            requires_gpu=True,
            resource_label=getattr(getattr(alias, "decision", None), "label", "unknown"),
            runtime_policy="reservation",
        )
    )
    gate = await database.runtime_reservation_gate(auth.subject_id, runtime, resolved_model_version, GPU_RUNTIMES)
    if not gate.get("allowed"):
        active = gate.get("active_reservation") or {}
        raise HTTPException(
            status_code=409,
            detail={
                "message": "GPU runtime is already reserved for another owner/model",
                "reservation": {
                    "id": active.get("id"),
                    "owner_id": active.get("owner_id"),
                    "runtime": active.get("runtime"),
                    "model_alias": active.get("model_alias"),
                    "resolved_model_version": active.get("resolved_model_version"),
                    "expires_at": jsonable_encoder(active.get("expires_at")),
                },
            },
        )
    reservation_id = f"reservation_{uuid.uuid4().hex}"
    row = await database.insert_runtime_reservation(
        {
            "id": reservation_id,
            "owner_id": auth.subject_id,
            "runtime": runtime,
            "model_alias": model_alias,
            "resolved_model_version": resolved_model_version,
            "duration_seconds": payload.duration_seconds,
            "reason": reason,
            "idempotency_key": normalized_idempotency_key,
            "expires_at": datetime.now(tz=UTC) + timedelta(seconds=payload.duration_seconds),
        }
    )
    log_event("runtime_reservation_created", reservation_id=reservation_id, runtime=runtime, model=model_alias)
    await record_audit_event(
        auth,
        "runtime_reservation.created",
        target_type="runtime_reservation",
        target_id=reservation_id,
        summary=f"Created runtime reservation {reservation_id}",
        metadata={
            "runtime": runtime,
            "model": model_alias,
            "resolved_model_version": row["resolved_model_version"],
            "duration_seconds": payload.duration_seconds,
            "expires_at": row["expires_at"],
            **freeform_reason_metadata(reason),
        },
    )
    return public_runtime_reservation(row)


@app.get("/v1/runtime-reservations/{id}")
async def runtime_reservation_get(reservation_id: str = ApiPath(alias="id"), authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:read")
    row = await database.get_runtime_reservation(reservation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="runtime reservation not found")
    require_runtime_reservation_owner_or_admin(auth, row)
    return public_runtime_reservation(row)


@app.delete("/v1/runtime-reservations/{id}")
async def runtime_reservation_delete(reservation_id: str = ApiPath(alias="id"), authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "runtimes:write")
    existing = await database.get_runtime_reservation(reservation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="runtime reservation not found")
    require_runtime_reservation_owner_or_admin(auth, existing)
    row = await database.cancel_runtime_reservation(reservation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="runtime reservation not found")
    if existing.get("status") != "cancelled" and row.get("status") == "cancelled":
        log_event("runtime_reservation_cancelled", reservation_id=reservation_id)
        await record_audit_event(
            auth,
            "runtime_reservation.cancelled",
            target_type="runtime_reservation",
            target_id=reservation_id,
            summary=f"Cancelled runtime reservation {reservation_id}",
            metadata={"runtime": row["runtime"], "model": row["model_alias"], "status": row["status"], "cancelled_at": row.get("cancelled_at")},
        )
    return public_runtime_reservation(row)


@app.get("/modelhub/v1/catalog")
async def modelhub_catalog(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "modelhub:read")
    client = await modelhub_client_for_auth(auth)
    return modelhub_catalog_for_client(client, auth)


def modelhub_model_record(model_id: str) -> dict[str, Any]:
    model_id = validate_modelhub_model_identifier(model_id)
    model = catalog_snapshot().model_or_alias_record(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    return model


@app.get("/modelhub/v1/models/{id}")
async def modelhub_model(model_id: str = ApiPath(alias="id"), authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "modelhub:read")
    record, _, _ = await authorized_modelhub_model_view(auth, model_id, for_download=False)
    return modelhub_policy.public_modelhub_metadata(record)


@app.get("/modelhub/v1/models/{id}/versions")
async def modelhub_versions(model_id: str = ApiPath(alias="id"), authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "modelhub:read")
    _, records, _ = await authorized_modelhub_model_view(auth, model_id, for_download=False)
    return modelhub_policy.public_modelhub_metadata({"model_id": model_id, "versions": records})


@app.get("/modelhub/v1/blobs/{sha256}")
@app.head("/modelhub/v1/blobs/{sha256}")
async def modelhub_blob(
    sha256: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_b1_accept_license: str | None = Header(default=None, alias="X-B1-Accept-License"),
) -> Response:
    auth = await authenticate(authorization)
    require_scope(auth, "modelhub:sync")
    digest = validate_modelhub_blob_sha256(sha256)
    try:
        accepted_license_refs = modelhub_policy.parse_accepted_license_refs(x_b1_accept_license)
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await require_modelhub_blob_authorized(auth, digest, accepted_license_refs)
    rate_headers = await enforce_modelhub_blob_rate_limit(auth)
    response = await proxy_http(settings.artifact_base_url, f"/modelhub/v1/blobs/{digest}", request, extra_headers=artifact_server_auth_headers())
    for key, value in rate_headers.items():
        response.headers[key] = value
    return response


@app.post("/modelhub/v1/sync/plan")
async def modelhub_sync_plan(payload: ModelHubSyncPlanRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "modelhub:sync")
    models = [validate_modelhub_model_identifier(model_id) for model_id in payload.models]
    installed = {blob.sha256.lower(): blob.size_bytes for blob in payload.installed_blobs}
    client = await modelhub_client_for_auth(auth)
    for model_id in models:
        await require_modelhub_model_authorized(auth, model_id, for_download=True)

    def record_allowed(model_id: str, record: dict[str, Any]) -> bool:
        if client is not None and not model_allowed_by_client(client, model_id, record):
            return False
        return model_allowed_by_auth_permissions(auth, model_id, record, "download")

    try:
        return modelhub_policy.build_sync_plan(catalog_snapshot(), models, installed, record_filter=record_allowed)
    except CatalogError as exc:
        status_code = 403 if "not permitted by Model Hub policy" in str(exc) else 404
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/modelhub/v1/clients")
async def modelhub_clients(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:read")
    require_credential_admin(auth)
    return {"object": "list", "data": [public_modelhub_client(row) for row in await database.list_modelhub_clients()]}


@app.post("/modelhub/v1/clients")
async def modelhub_client_create(payload: ModelHubClientCreate, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_credential_admin(auth)
    allowed_models = validate_modelhub_allowed_models(payload.allowed_models)
    cidr_allowlist = validate_modelhub_cidr_allowlist(payload.cidr_allowlist)
    scopes = scopes_for_role(Role.SERVICE, ["modelhub:read", "modelhub:sync"])
    key_prefix, api_key = generate_api_key()
    key_salt, key_digest = hash_api_key(api_key)
    api_client_id = f"client_{uuid.uuid4().hex}"
    modelhub_client_id = f"mhc_{uuid.uuid4().hex}"
    rows = await database.insert_modelhub_client_with_api_client(
        {
            "id": api_client_id,
            "display_name": f"Model Hub: {payload.display_name}",
            "role": Role.SERVICE.value,
            "scopes": sorted(scopes),
            "key_prefix": key_prefix,
            "key_salt": key_salt,
            "key_hash": key_digest,
            "cidr_allowlist": cidr_allowlist,
        },
        {
            "id": modelhub_client_id,
            "display_name": payload.display_name,
            "owner_id": auth.subject_id,
            "api_client_id": api_client_id,
            "key_prefix": key_prefix,
            "allowed_models": allowed_models,
            "cidr_allowlist": cidr_allowlist,
            "allow_downloads": payload.allow_downloads,
        },
    )
    log_event("modelhub_client_created", client_id=modelhub_client_id, api_client_id=api_client_id, key_prefix=key_prefix)
    await record_audit_event(
        auth,
        "modelhub_client.created",
        target_type="modelhub_client",
        target_id=modelhub_client_id,
        summary=f"Created Model Hub client {payload.display_name}",
        metadata={
            "display_name": payload.display_name,
            "api_client_id": api_client_id,
            "key_prefix": key_prefix,
            "allowed_models": allowed_models,
            "cidr_allowlist": cidr_allowlist,
            "allow_downloads": payload.allow_downloads,
        },
    )
    return {
        **public_modelhub_client(rows["modelhub_client"]),
        "api_client_id": api_client_id,
        "api_key": api_key,
        "scopes": sorted(scopes),
        "one_time_display": True,
    }


@app.put("/modelhub/v1/clients/{id}/cidr-allowlist")
async def modelhub_client_cidr_update(
    client_id: str = ApiPath(alias="id"),
    payload: CidrAllowlistUpdateRequest = Body(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_credential_admin(auth)
    cidr_allowlist = validate_modelhub_cidr_allowlist(payload.cidr_allowlist)
    row = await database.update_modelhub_client_cidr_allowlist(client_id, cidr_allowlist)
    if row is None:
        raise HTTPException(status_code=404, detail="Model Hub client not found")
    if row.get("revoked_at") is not None:
        raise HTTPException(status_code=409, detail="revoked Model Hub clients cannot be modified")
    log_event("modelhub_client_cidr_updated", client_id=client_id, cidr_count=len(cidr_allowlist))
    await record_audit_event(
        auth,
        "modelhub_client.cidr_allowlist_updated",
        target_type="modelhub_client",
        target_id=client_id,
        summary=f"Updated Model Hub client CIDR allowlist for {row['display_name']}",
        metadata={
            "display_name": row["display_name"],
            "api_client_id": row["api_client_id"],
            "key_prefix": row["key_prefix"],
            "cidr_allowlist": cidr_allowlist,
        },
    )
    return public_modelhub_client(row)


@app.put("/modelhub/v1/clients/{id}/policy")
async def modelhub_client_policy_update(
    client_id: str = ApiPath(alias="id"),
    payload: ModelHubClientPolicyUpdate = Body(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_credential_admin(auth)
    existing = await database.get_modelhub_client(client_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Model Hub client not found")
    if existing.get("revoked_at") is not None:
        raise HTTPException(status_code=409, detail="revoked Model Hub clients cannot be modified")
    allowed_models = validate_modelhub_allowed_models(payload.allowed_models)
    row = await database.update_modelhub_client_policy(
        client_id,
        allowed_models=allowed_models,
        allow_downloads=payload.allow_downloads,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Model Hub client not found")
    log_event("modelhub_client_policy_updated", client_id=client_id, allowed_model_count=len(allowed_models), allow_downloads=payload.allow_downloads)
    await record_audit_event(
        auth,
        "modelhub_client.policy_updated",
        target_type="modelhub_client",
        target_id=client_id,
        summary=f"Updated Model Hub client policy for {row['display_name']}",
        metadata={
            "display_name": row["display_name"],
            "api_client_id": row["api_client_id"],
            "key_prefix": row["key_prefix"],
            "allowed_models": allowed_models,
            "allow_downloads": payload.allow_downloads,
        },
    )
    return public_modelhub_client(row)


@app.delete("/modelhub/v1/clients/{id}")
async def modelhub_client_delete(client_id: str = ApiPath(alias="id"), authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth = await authenticate(authorization)
    require_scope(auth, "admin:write")
    require_credential_admin(auth)
    row = await database.revoke_modelhub_client(client_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model Hub client not found")
    log_event("modelhub_client_revoked", client_id=client_id)
    await record_audit_event(
        auth,
        "modelhub_client.revoked",
        target_type="modelhub_client",
        target_id=client_id,
        summary=f"Revoked Model Hub client {row['display_name']}",
        metadata={"display_name": row["display_name"], "api_client_id": row["api_client_id"], "key_prefix": row["key_prefix"]},
    )
    return public_modelhub_client(row)


@app.post("/prompt")
async def comfy_prompt(request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Response:
    compatibility = compatibility_header_value(request.headers)
    if not compatibility.startswith("comfyui"):
        raise HTTPException(status_code=404, detail="route not found")
    auth = await require_http_compatibility_access(request, compatibility, "jobs:write")
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="ComfyUI prompt body must be valid JSON") from exc
    client_id = body.get("client_id") if isinstance(body, dict) and isinstance(body.get("client_id"), str) else None
    prompt_summary = comfyui_native_prompt_audit_summary(body)
    model_free_kind = comfyui_native_prompt_model_free_kind(body)
    model_free_native = model_free_kind is not None
    model_free_smoke = model_free_kind == "model-free-smoke"
    if model_free_native:
        prompt_summary["model_free_native"] = True
        prompt_summary["model_free_kind"] = model_free_kind
    if model_free_smoke:
        prompt_summary["model_free_smoke"] = True
    native_prompt_hash = hashlib.sha256(body_bytes).hexdigest()
    owner = auth.subject_id if auth is not None else (f"comfy-client:{client_id[:80]}" if client_id else "comfy-client")
    resolution = native_comfyui_prompt_resolution(model_free_kind=model_free_kind)
    job_payload = comfyui_native_job_payload(client_id, native_prompt_hash, prompt_summary, model_free_kind=model_free_kind)
    normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
    if normalized_idempotency_key:
        existing = await database.get_job_by_idempotency_key(owner, normalized_idempotency_key)
        if existing is not None:
            ensure_idempotent_job_matches(existing, job_payload, resolution)
            return comfyui_idempotent_prompt_response(existing)
    job = await create_job_record(
        owner,
        job_payload,
        idempotency_key=normalized_idempotency_key,
        resolution=resolution,
    )
    job_id = job["id"]
    await database.update_job(job_id, state=JobState.WAITING_FOR_GPU.value, stage="waiting_for_gpu", progress=20)
    lease_owner: str | None = None
    release_lease = True
    load_started: float | None = None
    runtime_prepared = False
    try:
        lease_owner, _ = await acquire_comfyui_prompt_lease(job_id)
        load_started = monotonic()
        if not model_free_native:
            await prepare_comfyui_native_runtime(job)
            runtime_prepared = True
        await database.update_job(job_id, load_time_ms=elapsed_milliseconds(load_started))
        response = await proxy_http_bytes(settings.comfyui_url, "/prompt", request, body_bytes)
        prompt_id = comfyui_prompt_id_from_response(response)
        if response.status_code >= 400:
            failed = await database.update_job(
                job_id,
                state=JobState.FAILED.value,
                stage="comfyui_prompt_rejected",
                progress=100,
                run_time_ms=0,
                failure_category="comfyui_validation_error",
                failure_message=f"ComfyUI returned HTTP {response.status_code}",
            )
            if runtime_prepared:
                with suppress(Exception):
                    await mark_comfyui_native_runtime_idle(failed, "unsubmitted", JobState.FAILED.value)
            return response
        if not prompt_id:
            await database.update_job(
                job_id,
                state=JobState.RECOVERY_REQUIRED.value,
                stage="comfyui_prompt_id_missing",
                progress=70,
                run_time_ms=0,
                failure_category="comfyui_prompt_id_missing",
                failure_message="ComfyUI accepted the prompt without returning a native prompt_id",
            )
            return response
        await database.update_job(
            job_id,
            state=JobState.RUNNING.value,
            stage="comfyui_native_running",
            progress=70,
            started_at=datetime.now(tz=UTC),
            native_prompt_id=prompt_id,
        )
        schedule_comfyui_prompt_tracker(job_id, prompt_id, lease_owner)
        release_lease = False
        return response
    except HTTPException as exc:
        scheduler_failure = lease_owner is None
        await database.update_job(
            job_id,
            state=JobState.FAILED.value,
            stage="comfyui_prompt_not_forwarded" if scheduler_failure else "comfyui_prompt_proxy_failed",
            progress=100,
            load_time_ms=elapsed_milliseconds(load_started) if load_started is not None else None,
            failure_category="scheduler_unavailable" if scheduler_failure else "comfyui_proxy_error",
            failure_message=str(exc.detail)[:500],
        )
        raise
    except RuntimePreparationError as exc:
        await database.update_job(
            job_id,
            state=JobState.FAILED.value,
            stage="comfyui_runtime_prepare_failed",
            progress=100,
            load_time_ms=elapsed_milliseconds(load_started) if load_started is not None else None,
            failure_category="runtime_prepare_failed",
            failure_message=str(exc)[:500],
        )
        raise HTTPException(status_code=503, detail=runtime_prepare_error_detail(exc, native_comfyui_resolution(), "comfyui-prompt")) from exc
    except RuntimeError as exc:
        await database.update_job(
            job_id,
            state=JobState.FAILED.value,
            stage="comfyui_runtime_prepare_failed",
            progress=100,
            load_time_ms=elapsed_milliseconds(load_started) if load_started is not None else None,
            failure_category="runtime_prepare_failed",
            failure_message=str(exc)[:500],
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        await database.update_job(
            job_id,
            state=JobState.FAILED.value,
            stage="comfyui_prompt_proxy_failed",
            progress=100,
            load_time_ms=elapsed_milliseconds(load_started) if load_started is not None else None,
            failure_category="comfyui_proxy_error",
            failure_message=exc.__class__.__name__,
        )
        raise
    finally:
        if release_lease and lease_owner is not None:
            await database.release_scheduler_owner(lease_owner)


@app.websocket("/ws")
async def native_ws(websocket: WebSocket) -> None:
    compatibility = compatibility_header_value(websocket.headers)
    if compatibility.startswith("voicebox"):
        if not await require_websocket_compatibility_access(websocket, compatibility, "jobs:read"):
            return
        await bridge_voicebox_websocket(websocket, "/ws")
        return
    if compatibility.startswith("comfyui"):
        if not await require_websocket_compatibility_access(websocket, compatibility, "jobs:read"):
            return
        await bridge_comfyui_websocket(websocket)
        return
    await websocket.close(code=1008)


@app.websocket("/{path:path}")
async def compatibility_ws(path: str, websocket: WebSocket) -> None:
    compatibility = compatibility_header_value(websocket.headers)
    if compatibility.startswith("voicebox"):
        if not await require_websocket_compatibility_access(websocket, compatibility, "jobs:read"):
            return
        await bridge_voicebox_websocket(websocket, f"/{path}")
        return
    if compatibility.startswith("comfyui"):
        try:
            normalized_path = require_comfyui_websocket_allowed(path)
            required_scope = comfyui_websocket_scope_for_path(normalized_path)
        except HTTPException:
            await websocket.close(code=1008)
            return
        if not await require_websocket_compatibility_access(websocket, compatibility, required_scope):
            return
        await bridge_runtime_websocket(
            websocket,
            base_url=settings.comfyui_url,
            path=f"/{normalized_path}",
            log_name="comfyui",
            text_event_handler=persist_comfyui_ws_event,
        )
        return
    await websocket.close(code=1008)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], include_in_schema=False)
async def compatibility_passthrough(path: str, request: Request) -> Response:
    compatibility = compatibility_header_value(request.headers)
    if compatibility.startswith("comfyui"):
        await require_http_compatibility_access(request, compatibility, compatibility_scope_for_method(request.method))
        return await proxy_comfyui_compatibility(path, request)
    if compatibility.startswith("voicebox"):
        auth = await require_http_compatibility_access(request, compatibility, compatibility_scope_for_method(request.method))
        return await proxy_voicebox_compatibility(path, request, auth)
    raise HTTPException(status_code=404, detail="route not found")
