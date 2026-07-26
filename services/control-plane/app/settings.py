from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_secret(path: str | None, fallback: str = "") -> str:
    if not path:
        return fallback
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return fallback
    return value


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _words(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raw = default
    return tuple(value.strip() for value in raw.replace(",", " ").split() if value.strip())


def _default_self_test_tls_urls(
    *,
    host_chat: str,
    host_control: str,
    host_media: str,
    host_comfy: str,
    host_voice: str,
    host_models: str,
    host_api: str,
) -> str:
    routes = (
        (host_chat, "/"),
        (host_control, "/"),
        (host_media, "/"),
        (host_comfy, "/healthz"),
        (host_voice, "/healthz"),
        (host_models, "/healthz"),
        (host_api, "/healthz"),
    )
    return " ".join(f"https://{host}{path}" for host, path in routes if host.strip())


@dataclass(frozen=True)
class Settings:
    service_name: str
    dev_auth_bypass: bool
    admin_bootstrap_key: str
    open_webui_api_key: str
    master_key: str
    database_url: str
    redis_url: str
    localai_url: str
    comfyui_url: str
    voicebox_url: str
    audio_cpu_url: str
    artifact_base_url: str
    artifact_server_token: str
    runtime_control_token: str
    runtime_agent_url: str
    runtime_agent_token: str
    runtime_agent_tls_ca_file: str
    runtime_agent_tls_client_cert_file: str
    runtime_agent_tls_client_key_file: str
    runtime_agent_tls_verify: bool
    prometheus_scrape_token: str
    openai_compatible_base_url: str
    openai_compatible_api_key: str
    generic_http_base_url: str
    host_chat: str
    host_control: str
    host_media: str
    host_comfy: str
    host_voice: str
    host_models: str
    host_api: str
    data_root: str
    backup_root: str
    restore_test_root: str
    backup_encryption_mode: str
    artifact_root: str
    upload_max_bytes: int
    max_queued_jobs_per_owner: int
    max_active_jobs_per_owner: int
    max_jobs_per_hour_per_owner: int
    max_queued_jobs_global: int
    artifact_storage_max_bytes: int
    artifact_storage_reserve_bytes: int
    modelhub_blob_requests_per_minute: int
    model_catalog_dir: str
    workflow_seed_dir: str
    comfyui_node_pin_registry: str
    comfyui_trusted_route_prefixes: tuple[str, ...]
    job_runner_enabled: bool
    job_runner_interval_seconds: int
    gpu_job_runner_enabled: bool
    gpu_job_runner_interval_seconds: int
    gpu_job_runner_lease_ttl_seconds: int
    gpu_default_idle_timeout_seconds: int
    sync_inference_lease_ttl_seconds: int
    comfyui_prompt_wait_timeout_seconds: int
    comfyui_prompt_lease_ttl_seconds: int
    comfyui_prompt_poll_seconds: int
    comfyui_prompt_completion_timeout_seconds: int
    comfyui_prompt_idle_grace_seconds: int
    model_download_runner_enabled: bool
    model_download_runner_interval_seconds: int
    backup_scheduler_enabled: bool
    backup_scheduler_interval_seconds: int
    allow_external_providers: bool
    runtime_deployment_mode: str
    runtime_production_required: tuple[str, ...]
    gpu_total_vram_gib: float
    gpu_usable_vram_gib: float
    gpu_reserve_vram_gib: float
    gpu_max_active_pipelines: int
    host_total_ram_gib: float
    host_reserve_ram_gib: float
    llm_default_context: int
    llm_maximum_context: int
    llm_default_parallel_requests: int
    comfyui_maximum_parallel_jobs: int
    comfyui_maximum_batch_size: int
    cpu_residency_enabled: bool
    cpu_residency_max_ram_gib: float
    cpu_resident_aliases: tuple[str, ...]
    session_cookie_name: str
    session_cookie_secure: bool
    session_ttl_seconds: int
    cors_allow_origins: tuple[str, ...]
    trusted_proxy_cidrs: tuple[str, ...]
    caddy_tls_args: str
    caddy_internal_ca_file: str
    self_test_tls_urls: tuple[str, ...]
    self_test_tls_ca_file: str
    self_test_tls_verify: bool
    self_test_tiny_inference_enabled: bool
    self_test_unload_runtime: str


def load_settings() -> Settings:
    postgres_password = _read_secret(os.getenv("POSTGRES_PASSWORD_FILE"), os.getenv("POSTGRES_PASSWORD", ""))
    postgres_user = os.getenv("POSTGRES_USER", "b1_ai_hub")
    postgres_db = os.getenv("POSTGRES_DB", "b1_ai_hub")
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        database_url = f"postgresql+asyncpg://{postgres_user}:{postgres_password}@postgres:5432/{postgres_db}"

    host_chat = os.getenv("B1_HOST_CHAT", "ai.b1.germering")
    host_control = os.getenv("B1_HOST_CONTROL", "control.ai.b1.germering")
    host_media = os.getenv("B1_HOST_MEDIA", "media.ai.b1.germering")
    host_comfy = os.getenv("B1_HOST_COMFY", "comfy.ai.b1.germering")
    host_voice = os.getenv("B1_HOST_VOICE", "voice.ai.b1.germering")
    host_models = os.getenv("B1_HOST_MODELS", "models.ai.b1.germering")
    host_api = os.getenv("B1_HOST_API", "api.ai.b1.germering")

    cors_env = os.getenv("B1_CORS_ALLOW_ORIGINS", "").strip()
    if cors_env:
        cors_allow_origins = tuple(origin.strip() for origin in cors_env.split(",") if origin.strip())
    else:
        cors_allow_origins = tuple(
            f"https://{host}"
            for host in {
                host_chat,
                host_control,
                host_media,
                host_comfy,
                host_voice,
                host_models,
                host_api,
            }
            if host
        )

    return Settings(
        service_name=os.getenv("B1_SERVICE_NAME", "control-plane"),
        dev_auth_bypass=_bool("B1_DEV_AUTH_BYPASS", False),
        admin_bootstrap_key=_read_secret(os.getenv("B1_ADMIN_BOOTSTRAP_KEY_FILE"), os.getenv("B1_ADMIN_BOOTSTRAP_KEY", "")),
        open_webui_api_key=_read_secret(os.getenv("B1_OPEN_WEBUI_API_KEY_FILE"), os.getenv("B1_OPEN_WEBUI_API_KEY", "")),
        master_key=_read_secret(os.getenv("B1_MASTER_KEY_FILE"), os.getenv("B1_MASTER_KEY", "")),
        database_url=database_url,
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        localai_url=os.getenv("LOCALAI_URL", "http://localai:8000"),
        comfyui_url=os.getenv("COMFYUI_URL", "http://comfyui:8000"),
        voicebox_url=os.getenv("VOICEBOX_URL", "http://voicebox:8000"),
        audio_cpu_url=os.getenv("AUDIO_CPU_URL", "http://audio-cpu:8000"),
        artifact_base_url=os.getenv("ARTIFACT_BASE_URL", "http://artifact-server:8000"),
        artifact_server_token=_read_secret(os.getenv("B1_ARTIFACT_SERVER_TOKEN_FILE"), os.getenv("B1_ARTIFACT_SERVER_TOKEN", "")),
        runtime_control_token=_read_secret(os.getenv("B1_RUNTIME_CONTROL_TOKEN_FILE"), os.getenv("B1_RUNTIME_CONTROL_TOKEN", "")),
        runtime_agent_url=os.getenv("RUNTIME_AGENT_URL", "https://runtime-agent:8443"),
        runtime_agent_token=_read_secret(os.getenv("B1_RUNTIME_AGENT_TOKEN_FILE"), os.getenv("B1_RUNTIME_AGENT_TOKEN", "")),
        runtime_agent_tls_ca_file=os.getenv("B1_RUNTIME_AGENT_TLS_CA_FILE", "/run/secrets/runtime_agent_mtls_ca.crt"),
        runtime_agent_tls_client_cert_file=os.getenv("B1_RUNTIME_AGENT_TLS_CLIENT_CERT_FILE", "/run/secrets/runtime_agent_client.crt"),
        runtime_agent_tls_client_key_file=os.getenv("B1_RUNTIME_AGENT_TLS_CLIENT_KEY_FILE", "/run/secrets/runtime_agent_client.key"),
        runtime_agent_tls_verify=_bool("B1_RUNTIME_AGENT_TLS_VERIFY", True),
        prometheus_scrape_token=_read_secret(os.getenv("B1_PROMETHEUS_SCRAPE_TOKEN_FILE"), os.getenv("B1_PROMETHEUS_SCRAPE_TOKEN", "")),
        openai_compatible_base_url=os.getenv("B1_OPENAI_COMPATIBLE_BASE_URL", ""),
        openai_compatible_api_key=_read_secret(os.getenv("B1_OPENAI_COMPATIBLE_API_KEY_FILE"), os.getenv("B1_OPENAI_COMPATIBLE_API_KEY", "")),
        generic_http_base_url=os.getenv("B1_GENERIC_HTTP_BASE_URL", ""),
        host_chat=host_chat,
        host_control=host_control,
        host_media=host_media,
        host_comfy=host_comfy,
        host_voice=host_voice,
        host_models=host_models,
        host_api=host_api,
        data_root=os.getenv("B1_DATA_ROOT", "/srv/b1-ai-hub"),
        backup_root=os.getenv("B1_BACKUP_ROOT", "/srv/b1-ai-hub/backups"),
        restore_test_root=os.getenv("B1_RESTORE_TEST_ROOT", "/srv/b1-ai-hub/restore-tests"),
        backup_encryption_mode=os.getenv("B1_BACKUP_ENCRYPTION_MODE", "copy").strip().lower(),
        artifact_root=os.getenv("B1_ARTIFACT_ROOT", "/srv/b1-ai-hub/artifacts"),
        upload_max_bytes=_int("B1_UPLOAD_MAX_BYTES", 256 * 1024 * 1024),
        max_queued_jobs_per_owner=_int("B1_MAX_QUEUED_JOBS_PER_OWNER", 20),
        max_active_jobs_per_owner=_int("B1_MAX_ACTIVE_JOBS_PER_OWNER", 3),
        max_jobs_per_hour_per_owner=_int("B1_MAX_JOBS_PER_HOUR_PER_OWNER", 60),
        max_queued_jobs_global=_int("B1_MAX_QUEUED_JOBS_GLOBAL", 100),
        artifact_storage_max_bytes=_int("B1_ARTIFACT_STORAGE_MAX_BYTES", 0),
        artifact_storage_reserve_bytes=_int("B1_ARTIFACT_STORAGE_RESERVE_BYTES", 10 * 1024 * 1024 * 1024),
        modelhub_blob_requests_per_minute=_int("B1_MODELHUB_BLOB_REQUESTS_PER_MINUTE", 120),
        model_catalog_dir=os.getenv("B1_MODEL_CATALOG_DIR", "/opt/b1/model-catalog"),
        workflow_seed_dir=os.getenv("B1_WORKFLOW_SEED_DIR", "/opt/b1/workflows/approved"),
        comfyui_node_pin_registry=os.getenv("B1_COMFYUI_NODE_PIN_REGISTRY", "/opt/b1/workflows/approved-node-pins.json"),
        comfyui_trusted_route_prefixes=_words("B1_COMFYUI_TRUSTED_ROUTE_PREFIXES", ""),
        job_runner_enabled=_bool("B1_JOB_RUNNER_ENABLED", True),
        job_runner_interval_seconds=_int("B1_JOB_RUNNER_INTERVAL_SECONDS", 1),
        gpu_job_runner_enabled=_bool("B1_GPU_JOB_RUNNER_ENABLED", True),
        gpu_job_runner_interval_seconds=_int("B1_GPU_JOB_RUNNER_INTERVAL_SECONDS", 1),
        gpu_job_runner_lease_ttl_seconds=_int("B1_GPU_JOB_RUNNER_LEASE_TTL_SECONDS", 300),
        gpu_default_idle_timeout_seconds=_int("B1_GPU_DEFAULT_IDLE_TIMEOUT_SECONDS", 300),
        sync_inference_lease_ttl_seconds=_int("B1_SYNC_INFERENCE_LEASE_TTL_SECONDS", 7200),
        comfyui_prompt_wait_timeout_seconds=_int("B1_COMFY_PROMPT_WAIT_TIMEOUT_SECONDS", 120),
        comfyui_prompt_lease_ttl_seconds=_int("B1_COMFY_PROMPT_LEASE_TTL_SECONDS", 7200),
        comfyui_prompt_poll_seconds=_int("B1_COMFY_PROMPT_POLL_SECONDS", 2),
        comfyui_prompt_completion_timeout_seconds=_int("B1_COMFY_PROMPT_COMPLETION_TIMEOUT_SECONDS", 7200),
        comfyui_prompt_idle_grace_seconds=_int("B1_COMFY_PROMPT_IDLE_GRACE_SECONDS", 5),
        model_download_runner_enabled=_bool("B1_MODEL_DOWNLOAD_RUNNER_ENABLED", True),
        model_download_runner_interval_seconds=_int("B1_MODEL_DOWNLOAD_RUNNER_INTERVAL_SECONDS", 2),
        backup_scheduler_enabled=_bool("B1_BACKUP_SCHEDULER_ENABLED", True),
        backup_scheduler_interval_seconds=_int("B1_BACKUP_SCHEDULER_INTERVAL_SECONDS", 60),
        allow_external_providers=_bool("B1_ALLOW_EXTERNAL_PROVIDERS", False),
        runtime_deployment_mode=os.getenv("B1_RUNTIME_DEPLOYMENT_MODE", "development").strip().lower(),
        runtime_production_required=_words("B1_RUNTIME_PRODUCTION_REQUIRED", "localai comfyui audio-cpu"),
        gpu_total_vram_gib=_float("B1_GPU_TOTAL_VRAM_GIB", 12.0),
        gpu_usable_vram_gib=_float("B1_GPU_USABLE_VRAM_GIB", 10.5),
        gpu_reserve_vram_gib=_float("B1_GPU_RESERVE_VRAM_GIB", 1.5),
        gpu_max_active_pipelines=_int("B1_GPU_MAX_ACTIVE_PIPELINES", 1),
        host_total_ram_gib=_float("B1_HOST_TOTAL_RAM_GIB", 32.0),
        host_reserve_ram_gib=_float("B1_HOST_RESERVE_RAM_GIB", 6.0),
        llm_default_context=_int("B1_LLM_DEFAULT_CONTEXT", 8192),
        llm_maximum_context=_int("B1_LLM_MAXIMUM_CONTEXT", 16384),
        llm_default_parallel_requests=_int("B1_LLM_DEFAULT_PARALLEL_REQUESTS", 1),
        comfyui_maximum_parallel_jobs=_int("B1_COMFYUI_MAXIMUM_PARALLEL_JOBS", 1),
        comfyui_maximum_batch_size=_int("B1_COMFYUI_MAXIMUM_BATCH_SIZE", 1),
        cpu_residency_enabled=_bool("B1_CPU_RESIDENCY_ENABLED", True),
        cpu_residency_max_ram_gib=_float("B1_CPU_RESIDENCY_MAX_RAM_GIB", 2.0),
        cpu_resident_aliases=_words("B1_CPU_RESIDENT_ALIASES", "embedding-default tts-fast stt-default"),
        session_cookie_name=os.getenv("B1_SESSION_COOKIE_NAME", "b1_ai_hub_session"),
        session_cookie_secure=_bool("B1_SESSION_COOKIE_SECURE", True),
        session_ttl_seconds=_int("B1_SESSION_TTL_SECONDS", 8 * 60 * 60),
        cors_allow_origins=cors_allow_origins,
        trusted_proxy_cidrs=_words("B1_TRUSTED_PROXY_CIDRS", "127.0.0.1/32 ::1/128 172.16.0.0/12 fd00::/8"),
        caddy_tls_args=os.getenv("B1_CADDY_TLS_ARGS", "internal").strip(),
        caddy_internal_ca_file=os.getenv("B1_CADDY_INTERNAL_CA_FILE", "/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt"),
        self_test_tls_urls=_words(
            "B1_SELF_TEST_TLS_URLS",
            _default_self_test_tls_urls(
                host_chat=host_chat,
                host_control=host_control,
                host_media=host_media,
                host_comfy=host_comfy,
                host_voice=host_voice,
                host_models=host_models,
                host_api=host_api,
            ),
        ),
        self_test_tls_ca_file=os.getenv("B1_SELF_TEST_TLS_CA_FILE", "/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt"),
        self_test_tls_verify=_bool("B1_SELF_TEST_TLS_VERIFY", True),
        self_test_tiny_inference_enabled=_bool("B1_SELF_TEST_TINY_INFERENCE_ENABLED", True),
        self_test_unload_runtime=os.getenv("B1_SELF_TEST_UNLOAD_RUNTIME", "localai").strip().lower(),
    )
