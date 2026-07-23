from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, Index, Integer, MetaData, PrimaryKeyConstraint, String, Table, Text, and_, func, inspect as sa_inspect, or_, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql import insert, select, update

from . import audit
from .scheduler import JobState, PriorityClass, QueueItem, select_next_job

metadata = MetaData()
engine: AsyncEngine | None = None
scheduler_redis_client: Any | None = None
SCHEDULER_REDIS_LEASE_KEY = "b1-ai-hub:scheduler:gpu"
TERMINAL_JOB_STATES = {state.value for state in {JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED, JobState.EXPIRED}}
CANCEL_IMMEDIATE_STATES = {JobState.CREATED.value, JobState.VALIDATED.value, JobState.QUEUED.value, JobState.WAITING_FOR_GPU.value}
RETRYABLE_JOB_STATES = {JobState.FAILED.value, JobState.CANCELLED.value, JobState.EXPIRED.value, JobState.RECOVERY_REQUIRED.value}
PRIORITIZABLE_JOB_STATES = {JobState.CREATED.value, JobState.VALIDATED.value, JobState.QUEUED.value, JobState.WAITING_FOR_GPU.value}
VALID_JOB_PRIORITIES = {priority.value for priority in PriorityClass}

jobs = Table(
    "b1_jobs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("correlation_id", String(64), nullable=False),
    Column("idempotency_key", String(256), nullable=True),
    Column("owner_id", String(128), nullable=False),
    Column("modality", String(64), nullable=False),
    Column("operation", String(64), nullable=False),
    Column("model_alias", String(128), nullable=False),
    Column("resolved_model_version", String(256), nullable=False, default="unresolved"),
    Column("runtime", String(64), nullable=False, default="unassigned"),
    Column("priority", String(64), nullable=False, default="chat"),
    Column("state", String(64), nullable=False, default="created"),
    Column("request_params", JSONB, nullable=False, default=dict),
    Column("redacted_request", JSONB, nullable=False, default=dict),
    Column("progress", Integer, nullable=False, default=0),
    Column("stage", String(128), nullable=False, default="created"),
    Column("native_prompt_id", String(128), nullable=True),
    Column("artifacts", JSONB, nullable=False, default=list),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("failure_category", String(128), nullable=True),
    Column("failure_message", Text, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("load_time_ms", Integer, nullable=True),
    Column("run_time_ms", Integer, nullable=True),
    Column("peak_vram_mib", Integer, nullable=True),
    Column("peak_ram_mib", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

models = Table(
    "b1_models",
    metadata,
    Column("id", String(128), nullable=False),
    Column("version", String(256), nullable=False),
    Column("display_name", String(256), nullable=False),
    Column("modality", String(64), nullable=False),
    Column("preferred_runtime", String(64), nullable=False),
    Column("status", String(64), nullable=False),
    Column("resource_label", String(64), nullable=False),
    Column("manifest", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("id", "version"),
)

model_alias_policies = Table(
    "b1_model_alias_policies",
    metadata,
    Column("alias", String(128), primary_key=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("preferred_runtime", String(64), nullable=True),
    Column("idle_timeout_seconds", Integer, nullable=True),
    Column("visibility_roles", JSONB, nullable=False, default=list),
    Column("notes", Text, nullable=False, default=""),
    Column("updated_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

model_downloads = Table(
    "b1_model_downloads",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("owner_id", String(128), nullable=False),
    Column("model_id", String(128), nullable=False),
    Column("model_version", String(256), nullable=False),
    Column("source_url", Text, nullable=False),
    Column("target_sha256", String(64), nullable=False),
    Column("target_size_bytes", BigInteger, nullable=False),
    Column("bytes_downloaded", BigInteger, nullable=False, default=0),
    Column("status", String(64), nullable=False),
    Column("stage", String(128), nullable=False),
    Column("manifest", JSONB, nullable=False, default=dict),
    Column("credential_secret_name", String(128), nullable=True),
    Column("error_category", String(128), nullable=True),
    Column("error_message", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
)

scheduler_owner = Table(
    "b1_scheduler_owner",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(128), nullable=False),
    Column("epoch", Integer, nullable=False, default=0),
    Column("lease_expires_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

runtime_state = Table(
    "b1_runtime_state",
    metadata,
    Column("runtime", String(64), primary_key=True),
    Column("status", String(64), nullable=False, default="unknown"),
    Column("stage", String(128), nullable=False, default="unknown"),
    Column("active_model", String(128), nullable=True),
    Column("model_alias", String(128), nullable=True),
    Column("resolved_model_version", String(256), nullable=True),
    Column("job_id", String(64), nullable=True),
    Column("details", JSONB, nullable=False, default=dict),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

users = Table(
    "b1_users",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("username", String(128), nullable=False),
    Column("username_normalized", String(128), nullable=False),
    Column("display_name", String(256), nullable=False),
    Column("role", String(64), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_login_at", DateTime(timezone=True), nullable=True),
    Column("disabled_at", DateTime(timezone=True), nullable=True),
)

browser_sessions = Table(
    "b1_browser_sessions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("session_hash", String(64), nullable=False),
    Column("csrf_token", String(128), nullable=False),
    Column("user_agent", Text, nullable=False, default=""),
    Column("remote_addr", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

api_clients = Table(
    "b1_api_clients",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("display_name", String(256), nullable=False),
    Column("role", String(64), nullable=False),
    Column("scopes", JSONB, nullable=False, default=list),
    Column("key_prefix", String(64), nullable=False),
    Column("key_salt", String(128), nullable=False),
    Column("key_hash", String(256), nullable=False),
    Column("cidr_allowlist", JSONB, nullable=False, default=list),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

runtime_reservations = Table(
    "b1_runtime_reservations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("owner_id", String(128), nullable=False),
    Column("runtime", String(64), nullable=False),
    Column("model_alias", String(128), nullable=False),
    Column("resolved_model_version", String(256), nullable=False),
    Column("duration_seconds", Integer, nullable=False),
    Column("reason", Text, nullable=False, default=""),
    Column("status", String(64), nullable=False, default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
)

modelhub_clients = Table(
    "b1_modelhub_clients",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("display_name", String(256), nullable=False),
    Column("owner_id", String(128), nullable=False),
    Column("api_client_id", String(64), nullable=False),
    Column("key_prefix", String(64), nullable=False),
    Column("allowed_models", JSONB, nullable=False, default=list),
    Column("cidr_allowlist", JSONB, nullable=False, default=list),
    Column("allow_downloads", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

voice_profiles = Table(
    "b1_voice_profiles",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("display_name", String(256), nullable=False),
    Column("owner_id", String(128), nullable=False),
    Column("runtime", String(64), nullable=False, default="voicebox"),
    Column("engine", String(128), nullable=False),
    Column("model_alias", String(128), nullable=False),
    Column("profile_type", String(64), nullable=False),
    Column("status", String(64), nullable=False, default="active"),
    Column("visibility_roles", JSONB, nullable=False, default=list),
    Column("metadata", JSONB, nullable=False, default=dict),
    Column("sample_artifacts", JSONB, nullable=False, default=list),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
)

workflows = Table(
    "b1_workflows",
    metadata,
    Column("id", String(128), nullable=False),
    Column("version", String(128), nullable=False),
    Column("display_name", String(256), nullable=False),
    Column("backend_policy", String(64), nullable=False),
    Column("resource_class", String(64), nullable=False),
    Column("status", String(64), nullable=False),
    Column("visibility_roles", JSONB, nullable=False, default=list),
    Column("manifest", JSONB, nullable=False, default=dict),
    Column("dependency_status", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("unpublished_at", DateTime(timezone=True), nullable=True),
    PrimaryKeyConstraint("id", "version"),
)

encrypted_secrets = Table(
    "b1_encrypted_secrets",
    metadata,
    Column("name", String(128), primary_key=True),
    Column("display_name", String(256), nullable=False),
    Column("category", String(64), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("secret_envelope", JSONB, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
)

runtime_configurations = Table(
    "b1_runtime_configurations",
    metadata,
    Column("runtime", String(64), primary_key=True),
    Column("enabled", Boolean, nullable=False, default=False),
    Column("base_url", Text, nullable=False, default=""),
    Column("api_key_secret_name", String(128), nullable=True),
    Column("external_data_acknowledged", Boolean, nullable=False, default=False),
    Column("notes", Text, nullable=False, default=""),
    Column("updated_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

resource_policies = Table(
    "b1_resource_policies",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("gpu_total_vram_gib", Float, nullable=False),
    Column("gpu_usable_vram_gib", Float, nullable=False),
    Column("gpu_reserve_vram_gib", Float, nullable=False),
    Column("gpu_max_active_pipelines", Integer, nullable=False),
    Column("host_total_ram_gib", Float, nullable=False),
    Column("host_reserve_ram_gib", Float, nullable=False),
    Column("llm_default_context", Integer, nullable=False),
    Column("llm_maximum_context", Integer, nullable=False),
    Column("llm_default_parallel_requests", Integer, nullable=False),
    Column("comfyui_maximum_parallel_jobs", Integer, nullable=False),
    Column("comfyui_maximum_batch_size", Integer, nullable=False),
    Column("updated_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

admission_policies = Table(
    "b1_admission_policies",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("max_queued_jobs_per_owner", Integer, nullable=False),
    Column("max_active_jobs_per_owner", Integer, nullable=False),
    Column("max_jobs_per_hour_per_owner", Integer, nullable=False),
    Column("max_queued_jobs_global", Integer, nullable=False),
    Column("artifact_storage_max_bytes", BigInteger, nullable=False),
    Column("artifact_storage_reserve_bytes", BigInteger, nullable=False),
    Column("updated_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

backup_schedules = Table(
    "b1_backup_schedules",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("enabled", Boolean, nullable=False, default=False),
    Column("interval_hours", Integer, nullable=False, default=24),
    Column("keep_last", Integer, nullable=False, default=7),
    Column("delete_older_than_days", Integer, nullable=True),
    Column("label_prefix", String(64), nullable=False, default="scheduled"),
    Column("next_run_at", DateTime(timezone=True), nullable=True),
    Column("last_started_at", DateTime(timezone=True), nullable=True),
    Column("last_completed_at", DateTime(timezone=True), nullable=True),
    Column("last_status", String(64), nullable=False, default="idle"),
    Column("last_backup_name", String(128), nullable=True),
    Column("failure_message", Text, nullable=True),
    Column("updated_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

maintenance_state = Table(
    "b1_maintenance_state",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("enabled", Boolean, nullable=False, default=False),
    Column("reason", Text, nullable=False, default=""),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("updated_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

update_plans = Table(
    "b1_update_plans",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("target_version", String(128), nullable=False),
    Column("source_url", Text, nullable=False, default=""),
    Column("status", String(64), nullable=False, default="planned"),
    Column("stage", String(128), nullable=False, default="planned"),
    Column("image_refs", JSONB, nullable=False, default=list),
    Column("preflight", JSONB, nullable=False, default=dict),
    Column("image_stage", JSONB, nullable=False, default=list),
    Column("compose_override", JSONB, nullable=False, default=dict),
    Column("backup_name", String(128), nullable=True),
    Column("self_test", JSONB, nullable=False, default=dict),
    Column("promotion_result", JSONB, nullable=False, default=dict),
    Column("rollback_result", JSONB, nullable=False, default=dict),
    Column("notes", Text, nullable=False, default=""),
    Column("failure_message", Text, nullable=True),
    Column("created_by", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("staged_at", DateTime(timezone=True), nullable=True),
    Column("health_checked_at", DateTime(timezone=True), nullable=True),
    Column("promotion_requested_at", DateTime(timezone=True), nullable=True),
    Column("rolled_back_at", DateTime(timezone=True), nullable=True),
)

audit_events = Table(
    "b1_audit_events",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("actor_id", String(128), nullable=True),
    Column("actor_role", String(64), nullable=True),
    Column("actor_key_prefix", String(64), nullable=True),
    Column("event_type", String(128), nullable=False),
    Column("target_type", String(128), nullable=True),
    Column("target_id", String(256), nullable=True),
    Column("summary", Text, nullable=False, default=""),
    Column("metadata", JSONB, nullable=False, default=dict),
    Column("correlation_id", String(64), nullable=True),
    Column("remote_addr", String(128), nullable=True),
)

Index(
    "b1_jobs_owner_idempotency_key_uq",
    jobs.c.owner_id,
    jobs.c.idempotency_key,
    unique=True,
    postgresql_where=jobs.c.idempotency_key.isnot(None),
)
Index("b1_jobs_native_prompt_id_idx", jobs.c.native_prompt_id)
Index("b1_api_clients_key_prefix_uq", api_clients.c.key_prefix, unique=True)
Index("b1_users_username_normalized_uq", users.c.username_normalized, unique=True)
Index("b1_users_role_idx", users.c.role)
Index("b1_browser_sessions_hash_uq", browser_sessions.c.session_hash, unique=True)
Index("b1_browser_sessions_user_idx", browser_sessions.c.user_id)
Index("b1_browser_sessions_expiry_idx", browser_sessions.c.expires_at)
Index("b1_model_downloads_status_idx", model_downloads.c.status)
Index("b1_model_downloads_model_idx", model_downloads.c.model_id, model_downloads.c.model_version)
Index("b1_model_alias_policies_enabled_idx", model_alias_policies.c.enabled)
Index("b1_runtime_reservations_status_idx", runtime_reservations.c.status)
Index("b1_runtime_state_status_idx", runtime_state.c.status)
Index("b1_modelhub_clients_api_client_id_uq", modelhub_clients.c.api_client_id, unique=True)
Index("b1_voice_profiles_status_idx", voice_profiles.c.status)
Index("b1_voice_profiles_owner_idx", voice_profiles.c.owner_id)
Index("b1_voice_profiles_runtime_idx", voice_profiles.c.runtime)
Index("b1_workflows_status_idx", workflows.c.status)
Index("b1_encrypted_secrets_category_idx", encrypted_secrets.c.category)
Index("b1_encrypted_secrets_deleted_at_idx", encrypted_secrets.c.deleted_at)
Index("b1_runtime_configurations_enabled_idx", runtime_configurations.c.enabled)
Index("b1_backup_schedules_next_run_idx", backup_schedules.c.enabled, backup_schedules.c.next_run_at)
Index("b1_maintenance_state_enabled_idx", maintenance_state.c.enabled)
Index("b1_update_plans_status_idx", update_plans.c.status)
Index("b1_update_plans_created_at_idx", update_plans.c.created_at)
Index("b1_audit_events_created_at_idx", audit_events.c.created_at)
Index("b1_audit_events_event_type_idx", audit_events.c.event_type)
Index("b1_audit_events_actor_id_idx", audit_events.c.actor_id)
Index("b1_audit_events_target_idx", audit_events.c.target_type, audit_events.c.target_id)

EXPORT_TABLES = [
    jobs,
    models,
    model_alias_policies,
    model_downloads,
    scheduler_owner,
    users,
    api_clients,
    runtime_reservations,
    modelhub_clients,
    voice_profiles,
    workflows,
    encrypted_secrets,
    runtime_configurations,
    resource_policies,
    admission_policies,
    backup_schedules,
    maintenance_state,
    update_plans,
    audit_events,
]
EXPORT_TABLES_BY_NAME = {table.name: table for table in EXPORT_TABLES}
LOGICAL_EXPORT_FORMAT = "b1-ai-hub-postgres-logical-export/v1"
SCHEMA_COMPATIBILITY_SQL = [
    "ALTER TABLE b1_jobs ADD COLUMN IF NOT EXISTS idempotency_key varchar(256)",
    "ALTER TABLE b1_jobs ADD COLUMN IF NOT EXISTS native_prompt_id varchar(128)",
    "ALTER TABLE b1_jobs ADD COLUMN IF NOT EXISTS started_at timestamp with time zone",
    "ALTER TABLE b1_jobs ADD COLUMN IF NOT EXISTS completed_at timestamp with time zone",
    "ALTER TABLE b1_jobs ADD COLUMN IF NOT EXISTS load_time_ms integer",
    "ALTER TABLE b1_jobs ADD COLUMN IF NOT EXISTS run_time_ms integer",
    "ALTER TABLE b1_jobs ADD COLUMN IF NOT EXISTS peak_vram_mib integer",
    "ALTER TABLE b1_jobs ADD COLUMN IF NOT EXISTS peak_ram_mib integer",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS b1_jobs_owner_idempotency_key_uq "
        "ON b1_jobs (owner_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    ),
    "CREATE INDEX IF NOT EXISTS b1_jobs_native_prompt_id_idx ON b1_jobs (native_prompt_id)",
    "ALTER TABLE b1_api_clients ADD COLUMN IF NOT EXISTS cidr_allowlist jsonb NOT NULL DEFAULT '[]'::jsonb",
    "CREATE UNIQUE INDEX IF NOT EXISTS b1_api_clients_key_prefix_uq ON b1_api_clients (key_prefix)",
    "CREATE UNIQUE INDEX IF NOT EXISTS b1_users_username_normalized_uq ON b1_users (username_normalized)",
    "CREATE INDEX IF NOT EXISTS b1_users_role_idx ON b1_users (role)",
    "CREATE UNIQUE INDEX IF NOT EXISTS b1_browser_sessions_hash_uq ON b1_browser_sessions (session_hash)",
    "CREATE INDEX IF NOT EXISTS b1_browser_sessions_user_idx ON b1_browser_sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS b1_browser_sessions_expiry_idx ON b1_browser_sessions (expires_at)",
    "CREATE INDEX IF NOT EXISTS b1_model_downloads_status_idx ON b1_model_downloads (status)",
    "CREATE INDEX IF NOT EXISTS b1_model_downloads_model_idx ON b1_model_downloads (model_id, model_version)",
    "ALTER TABLE b1_model_downloads ADD COLUMN IF NOT EXISTS credential_secret_name varchar(128)",
    (
        "CREATE TABLE IF NOT EXISTS b1_model_alias_policies ("
        "alias varchar(128) PRIMARY KEY, "
        "enabled boolean NOT NULL DEFAULT true, "
        "preferred_runtime varchar(64), "
        "idle_timeout_seconds integer, "
        "visibility_roles jsonb NOT NULL DEFAULT '[]'::jsonb, "
        "notes text NOT NULL DEFAULT '', "
        "updated_by varchar(128), "
        "created_at timestamp with time zone NOT NULL, "
        "updated_at timestamp with time zone NOT NULL"
        ")"
    ),
    "CREATE INDEX IF NOT EXISTS b1_model_alias_policies_enabled_idx ON b1_model_alias_policies (enabled)",
    "CREATE INDEX IF NOT EXISTS b1_runtime_reservations_status_idx ON b1_runtime_reservations (status)",
    "CREATE INDEX IF NOT EXISTS b1_runtime_state_status_idx ON b1_runtime_state (status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS b1_modelhub_clients_api_client_id_uq ON b1_modelhub_clients (api_client_id)",
    "CREATE INDEX IF NOT EXISTS b1_voice_profiles_status_idx ON b1_voice_profiles (status)",
    "CREATE INDEX IF NOT EXISTS b1_voice_profiles_owner_idx ON b1_voice_profiles (owner_id)",
    "CREATE INDEX IF NOT EXISTS b1_voice_profiles_runtime_idx ON b1_voice_profiles (runtime)",
    "CREATE INDEX IF NOT EXISTS b1_workflows_status_idx ON b1_workflows (status)",
    "CREATE INDEX IF NOT EXISTS b1_encrypted_secrets_category_idx ON b1_encrypted_secrets (category)",
    "CREATE INDEX IF NOT EXISTS b1_encrypted_secrets_deleted_at_idx ON b1_encrypted_secrets (deleted_at)",
    (
        "CREATE TABLE IF NOT EXISTS b1_runtime_configurations ("
        "runtime varchar(64) PRIMARY KEY, "
        "enabled boolean NOT NULL DEFAULT false, "
        "base_url text NOT NULL DEFAULT '', "
        "api_key_secret_name varchar(128), "
        "external_data_acknowledged boolean NOT NULL DEFAULT false, "
        "notes text NOT NULL DEFAULT '', "
        "updated_by varchar(128), "
        "created_at timestamp with time zone NOT NULL, "
        "updated_at timestamp with time zone NOT NULL"
        ")"
    ),
    "CREATE INDEX IF NOT EXISTS b1_runtime_configurations_enabled_idx ON b1_runtime_configurations (enabled)",
    (
        "CREATE TABLE IF NOT EXISTS b1_admission_policies ("
        "id varchar(64) PRIMARY KEY, "
        "max_queued_jobs_per_owner integer NOT NULL, "
        "max_active_jobs_per_owner integer NOT NULL, "
        "max_jobs_per_hour_per_owner integer NOT NULL, "
        "max_queued_jobs_global integer NOT NULL, "
        "artifact_storage_max_bytes bigint NOT NULL, "
        "artifact_storage_reserve_bytes bigint NOT NULL, "
        "updated_by varchar(128), "
        "created_at timestamp with time zone NOT NULL, "
        "updated_at timestamp with time zone NOT NULL"
        ")"
    ),
    "CREATE INDEX IF NOT EXISTS b1_backup_schedules_next_run_idx ON b1_backup_schedules (enabled, next_run_at)",
    "CREATE INDEX IF NOT EXISTS b1_maintenance_state_enabled_idx ON b1_maintenance_state (enabled)",
    "ALTER TABLE b1_update_plans ADD COLUMN IF NOT EXISTS image_stage jsonb NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE b1_update_plans ADD COLUMN IF NOT EXISTS compose_override jsonb NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE b1_update_plans ADD COLUMN IF NOT EXISTS promotion_result jsonb NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE b1_update_plans ADD COLUMN IF NOT EXISTS promotion_requested_at timestamp with time zone",
    "CREATE INDEX IF NOT EXISTS b1_update_plans_status_idx ON b1_update_plans (status)",
    "CREATE INDEX IF NOT EXISTS b1_update_plans_created_at_idx ON b1_update_plans (created_at)",
    "CREATE INDEX IF NOT EXISTS b1_audit_events_created_at_idx ON b1_audit_events (created_at)",
    "CREATE INDEX IF NOT EXISTS b1_audit_events_event_type_idx ON b1_audit_events (event_type)",
    "CREATE INDEX IF NOT EXISTS b1_audit_events_actor_id_idx ON b1_audit_events (actor_id)",
    "CREATE INDEX IF NOT EXISTS b1_audit_events_target_idx ON b1_audit_events (target_type, target_id)",
]


class LogicalDumpError(RuntimeError):
    pass


class SchemaNotReadyError(RuntimeError):
    pass


def priority_class_from_value(value: str | None) -> PriorityClass:
    try:
        return PriorityClass(value or "")
    except ValueError:
        return PriorityClass.BATCH


def claim_queue_item_for_row(row: dict[str, Any]) -> QueueItem:
    queued_at = row.get("created_at")
    if not isinstance(queued_at, datetime):
        queued_at = datetime.now(tz=UTC)
    return QueueItem(
        job_id=str(row["id"]),
        priority=priority_class_from_value(row.get("priority")),
        queued_at=queued_at,
        model_alias=str(row.get("model_alias") or ""),
    )


def select_claim_candidate(rows: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any] | None:
    by_id = {str(row["id"]): row for row in rows}
    selected = select_next_job((claim_queue_item_for_row(row) for row in rows), now=now)
    return by_id.get(selected.job_id) if selected else None


def configure_engine(database_url: str) -> None:
    global engine
    engine = create_async_engine(database_url, pool_pre_ping=True)


def configure_scheduler_redis(client: Any | None) -> None:
    global scheduler_redis_client
    scheduler_redis_client = client


async def init_schema() -> None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        for statement in SCHEMA_COMPATIBILITY_SQL:
            await conn.execute(text(statement))


def expected_schema_snapshot() -> dict[str, set[str]]:
    return {
        table.name: {column.name for column in table.columns}
        for table in metadata.sorted_tables
        if table.name.startswith("b1_")
    }


async def verify_schema_current() -> None:
    if engine is None:
        raise RuntimeError("database engine is not configured")

    def inspect_schema(sync_conn: Any) -> tuple[list[str], dict[str, list[str]]]:
        inspector = sa_inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        expected = expected_schema_snapshot()
        missing_tables = sorted(table_name for table_name in expected if table_name not in existing_tables)
        missing_columns: dict[str, list[str]] = {}
        for table_name, columns in expected.items():
            if table_name in missing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            missing = sorted(column for column in columns if column not in existing_columns)
            if missing:
                missing_columns[table_name] = missing
        return missing_tables, missing_columns

    async with engine.connect() as conn:
        missing_tables, missing_columns = await conn.run_sync(inspect_schema)
    if missing_tables or missing_columns:
        raise SchemaNotReadyError(
            "database schema is not current; run `python -m app.migrate upgrade head` before starting the control plane "
            f"(missing_tables={missing_tables}, missing_columns={missing_columns})"
        )


async def ping() -> bool:
    if engine is None:
        return False
    async with engine.connect() as conn:
        await conn.execute(select(1))
    return True


async def insert_job(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "resolved_model_version": "unresolved",
        "runtime": "unassigned",
        "state": "created",
        "progress": 0,
        "stage": "created",
        "native_prompt_id": None,
        "artifacts": [],
        "retry_count": 0,
        "idempotency_key": None,
        "failure_category": None,
        "failure_message": None,
        "started_at": None,
        "completed_at": None,
        "load_time_ms": None,
        "run_time_ms": None,
        "peak_vram_mib": None,
        "peak_ram_mib": None,
        **payload,
    }
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(jobs).values(**row))
    except IntegrityError:
        if row["idempotency_key"]:
            existing = await get_job_by_idempotency_key(row["owner_id"], row["idempotency_key"])
            if existing is not None:
                return existing
        raise
    return row


async def upsert_model_record(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        **payload,
    }
    stmt = pg_insert(models).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[models.c.id, models.c.version],
        set_={
            "display_name": stmt.excluded.display_name,
            "modality": stmt.excluded.modality,
            "preferred_runtime": stmt.excluded.preferred_runtime,
            "status": stmt.excluded.status,
            "resource_label": stmt.excluded.resource_label,
            "manifest": stmt.excluded.manifest,
            "updated_at": now,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_model_record(row["id"], row["version"]) or row


async def get_model_record(model_id: str, version: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(models).where(and_(models.c.id == model_id, models.c.version == version)))
        row = result.mappings().first()
    return dict(row) if row else None


async def list_model_records(status: str | None = None) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        query = select(models)
        if status is not None:
            query = query.where(models.c.status == status)
        result = await conn.execute(query.order_by(models.c.id.asc(), models.c.version.desc()))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def upsert_model_alias_policy(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    existing = await get_model_alias_policy(payload["alias"])
    row = {
        "enabled": True,
        "preferred_runtime": None,
        "idle_timeout_seconds": None,
        "visibility_roles": [],
        "notes": "",
        "updated_by": None,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        **payload,
    }
    stmt = pg_insert(model_alias_policies).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[model_alias_policies.c.alias],
        set_={
            "enabled": stmt.excluded.enabled,
            "preferred_runtime": stmt.excluded.preferred_runtime,
            "idle_timeout_seconds": stmt.excluded.idle_timeout_seconds,
            "visibility_roles": stmt.excluded.visibility_roles,
            "notes": stmt.excluded.notes,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": now,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_model_alias_policy(row["alias"]) or row


async def get_model_alias_policy(alias: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(model_alias_policies).where(model_alias_policies.c.alias == alias))
        row = result.mappings().first()
    return dict(row) if row else None


async def list_model_alias_policies() -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(model_alias_policies).order_by(model_alias_policies.c.alias.asc()))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def delete_model_alias_policy(alias: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.begin() as conn:
        result = await conn.execute(select(model_alias_policies).where(model_alias_policies.c.alias == alias).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current = dict(row)
        await conn.execute(model_alias_policies.delete().where(model_alias_policies.c.alias == alias))
    return current


async def quarantine_model_record(model_id: str, version: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        await conn.execute(
            update(models)
            .where(and_(models.c.id == model_id, models.c.version == version))
            .values(status="quarantined", updated_at=now)
        )
    return await get_model_record(model_id, version)


async def upsert_encrypted_secret(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "description": "",
        "deleted_at": None,
        **payload,
    }
    stmt = pg_insert(encrypted_secrets).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[encrypted_secrets.c.name],
        set_={
            "display_name": stmt.excluded.display_name,
            "category": stmt.excluded.category,
            "description": stmt.excluded.description,
            "secret_envelope": stmt.excluded.secret_envelope,
            "updated_at": now,
            "deleted_at": None,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_encrypted_secret(row["name"]) or row


async def get_encrypted_secret(name: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        query = select(encrypted_secrets).where(encrypted_secrets.c.name == name)
        if not include_deleted:
            query = query.where(encrypted_secrets.c.deleted_at.is_(None))
        result = await conn.execute(query)
        row = result.mappings().first()
    return dict(row) if row else None


async def list_encrypted_secrets(
    *,
    category: str | None = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    filters = []
    if category:
        filters.append(encrypted_secrets.c.category == category)
    if not include_deleted:
        filters.append(encrypted_secrets.c.deleted_at.is_(None))
    query = select(encrypted_secrets)
    if filters:
        query = query.where(and_(*filters))
    query = query.order_by(encrypted_secrets.c.category.asc(), encrypted_secrets.c.name.asc())
    async with engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def delete_encrypted_secret(name: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(encrypted_secrets).where(encrypted_secrets.c.name == name).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current = dict(row)
        if current.get("deleted_at") is not None:
            return current
        values = {"deleted_at": now, "updated_at": now}
        await conn.execute(update(encrypted_secrets).where(encrypted_secrets.c.name == name).values(**values))
        return {**current, **values}


async def upsert_runtime_configuration(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "enabled": False,
        "base_url": "",
        "api_key_secret_name": None,
        "external_data_acknowledged": False,
        "notes": "",
        "updated_by": None,
        "created_at": now,
        "updated_at": now,
        **payload,
    }
    stmt = pg_insert(runtime_configurations).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[runtime_configurations.c.runtime],
        set_={
            "enabled": stmt.excluded.enabled,
            "base_url": stmt.excluded.base_url,
            "api_key_secret_name": stmt.excluded.api_key_secret_name,
            "external_data_acknowledged": stmt.excluded.external_data_acknowledged,
            "notes": stmt.excluded.notes,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": now,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_runtime_configuration(row["runtime"]) or row


async def get_runtime_configuration(runtime: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(runtime_configurations).where(runtime_configurations.c.runtime == runtime))
        row = result.mappings().first()
    return dict(row) if row else None


async def list_runtime_configurations() -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(runtime_configurations).order_by(runtime_configurations.c.runtime.asc()))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def get_resource_policy_record(policy_id: str = "default") -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(resource_policies).where(resource_policies.c.id == policy_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def upsert_resource_policy_record(payload: dict[str, Any], policy_id: str = "default") -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    existing = await get_resource_policy_record(policy_id)
    row = {
        "id": policy_id,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        **payload,
    }
    stmt = pg_insert(resource_policies).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[resource_policies.c.id],
        set_={
            "gpu_total_vram_gib": stmt.excluded.gpu_total_vram_gib,
            "gpu_usable_vram_gib": stmt.excluded.gpu_usable_vram_gib,
            "gpu_reserve_vram_gib": stmt.excluded.gpu_reserve_vram_gib,
            "gpu_max_active_pipelines": stmt.excluded.gpu_max_active_pipelines,
            "host_total_ram_gib": stmt.excluded.host_total_ram_gib,
            "host_reserve_ram_gib": stmt.excluded.host_reserve_ram_gib,
            "llm_default_context": stmt.excluded.llm_default_context,
            "llm_maximum_context": stmt.excluded.llm_maximum_context,
            "llm_default_parallel_requests": stmt.excluded.llm_default_parallel_requests,
            "comfyui_maximum_parallel_jobs": stmt.excluded.comfyui_maximum_parallel_jobs,
            "comfyui_maximum_batch_size": stmt.excluded.comfyui_maximum_batch_size,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": now,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_resource_policy_record(policy_id) or row


async def delete_resource_policy_record(policy_id: str = "default") -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    existing = await get_resource_policy_record(policy_id)
    if existing is None:
        return None
    async with engine.begin() as conn:
        await conn.execute(resource_policies.delete().where(resource_policies.c.id == policy_id))
    return existing


async def get_admission_policy_record(policy_id: str = "default") -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(admission_policies).where(admission_policies.c.id == policy_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def upsert_admission_policy_record(payload: dict[str, Any], policy_id: str = "default") -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    existing = await get_admission_policy_record(policy_id)
    row = {
        "id": policy_id,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        **payload,
    }
    stmt = pg_insert(admission_policies).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[admission_policies.c.id],
        set_={
            "max_queued_jobs_per_owner": stmt.excluded.max_queued_jobs_per_owner,
            "max_active_jobs_per_owner": stmt.excluded.max_active_jobs_per_owner,
            "max_jobs_per_hour_per_owner": stmt.excluded.max_jobs_per_hour_per_owner,
            "max_queued_jobs_global": stmt.excluded.max_queued_jobs_global,
            "artifact_storage_max_bytes": stmt.excluded.artifact_storage_max_bytes,
            "artifact_storage_reserve_bytes": stmt.excluded.artifact_storage_reserve_bytes,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": now,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_admission_policy_record(policy_id) or row


async def delete_admission_policy_record(policy_id: str = "default") -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    existing = await get_admission_policy_record(policy_id)
    if existing is None:
        return None
    async with engine.begin() as conn:
        await conn.execute(admission_policies.delete().where(admission_policies.c.id == policy_id))
    return existing


async def get_backup_schedule(schedule_id: str = "default") -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(backup_schedules).where(backup_schedules.c.id == schedule_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def upsert_backup_schedule(payload: dict[str, Any], schedule_id: str = "default") -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    existing = await get_backup_schedule(schedule_id)
    row = {
        "id": schedule_id,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        "last_started_at": existing.get("last_started_at") if existing else None,
        "last_completed_at": existing.get("last_completed_at") if existing else None,
        "last_status": existing.get("last_status", "idle") if existing else "idle",
        "last_backup_name": existing.get("last_backup_name") if existing else None,
        "failure_message": existing.get("failure_message") if existing else None,
        **payload,
    }
    stmt = pg_insert(backup_schedules).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[backup_schedules.c.id],
        set_={
            "enabled": stmt.excluded.enabled,
            "interval_hours": stmt.excluded.interval_hours,
            "keep_last": stmt.excluded.keep_last,
            "delete_older_than_days": stmt.excluded.delete_older_than_days,
            "label_prefix": stmt.excluded.label_prefix,
            "next_run_at": stmt.excluded.next_run_at,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": now,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_backup_schedule(schedule_id) or row


async def claim_due_backup_schedule(now: datetime | None = None, schedule_id: str = "default") -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    current = now or datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(backup_schedules).where(backup_schedules.c.id == schedule_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current_row = dict(row)
        next_run_at = current_row.get("next_run_at")
        if next_run_at is not None and next_run_at.tzinfo is None:
            next_run_at = next_run_at.replace(tzinfo=UTC)
        if not current_row.get("enabled") or next_run_at is None or next_run_at > current or current_row.get("last_status") == "running":
            return None
        values = {
            "last_status": "running",
            "last_started_at": current,
            "failure_message": None,
            "updated_at": current,
        }
        await conn.execute(update(backup_schedules).where(backup_schedules.c.id == schedule_id).values(**values))
        return {**current_row, **values}


async def update_backup_schedule_after_run(
    schedule_id: str = "default",
    *,
    status: str,
    next_run_at: datetime | None,
    backup_name: str | None = None,
    failure_message: str | None = None,
    completed_at: datetime | None = None,
) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = completed_at or datetime.now(tz=UTC)
    values = {
        "last_status": status,
        "last_completed_at": now,
        "next_run_at": next_run_at,
        "failure_message": failure_message,
        "updated_at": now,
    }
    if backup_name is not None:
        values["last_backup_name"] = backup_name
    async with engine.begin() as conn:
        await conn.execute(update(backup_schedules).where(backup_schedules.c.id == schedule_id).values(**values))
    return await get_backup_schedule(schedule_id)


async def get_maintenance_state(state_id: str = "default") -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(maintenance_state).where(maintenance_state.c.id == state_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def upsert_maintenance_state(payload: dict[str, Any], state_id: str = "default") -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    existing = await get_maintenance_state(state_id)
    row = {
        "id": state_id,
        "enabled": False,
        "reason": "",
        "started_at": None,
        "ended_at": None,
        "updated_by": None,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        **payload,
    }
    stmt = pg_insert(maintenance_state).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[maintenance_state.c.id],
        set_={
            "enabled": stmt.excluded.enabled,
            "reason": stmt.excluded.reason,
            "started_at": stmt.excluded.started_at,
            "ended_at": stmt.excluded.ended_at,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": now,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_maintenance_state(state_id) or row


async def insert_update_plan(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "source_url": "",
        "status": "planned",
        "stage": "planned",
        "image_refs": [],
        "preflight": {},
        "image_stage": [],
        "compose_override": {},
        "backup_name": None,
        "self_test": {},
        "promotion_result": {},
        "rollback_result": {},
        "notes": "",
        "failure_message": None,
        "created_by": None,
        "staged_at": None,
        "health_checked_at": None,
        "promotion_requested_at": None,
        "rolled_back_at": None,
        **payload,
    }
    async with engine.begin() as conn:
        await conn.execute(insert(update_plans).values(**row))
    return row


async def get_update_plan(update_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(update_plans).where(update_plans.c.id == update_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def list_update_plans(limit: int = 50) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    bounded_limit = max(1, min(int(limit), 500))
    async with engine.connect() as conn:
        result = await conn.execute(select(update_plans).order_by(update_plans.c.created_at.desc()).limit(bounded_limit))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def update_update_plan(update_id: str, **changes: Any) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    changes["updated_at"] = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        await conn.execute(update(update_plans).where(update_plans.c.id == update_id).values(**changes))
    return await get_update_plan(update_id)


async def insert_model_download(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "cancelled_at": None,
        "bytes_downloaded": 0,
        "status": "queued",
        "stage": "queued",
        "error_category": None,
        "error_message": None,
        **payload,
    }
    async with engine.begin() as conn:
        await conn.execute(insert(model_downloads).values(**row))
    return row


async def get_model_download(download_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(model_downloads).where(model_downloads.c.id == download_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def list_model_downloads(limit: int = 100) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    bounded_limit = max(1, min(int(limit), 500))
    async with engine.connect() as conn:
        result = await conn.execute(select(model_downloads).order_by(model_downloads.c.created_at.desc()).limit(bounded_limit))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def update_model_download(download_id: str, **changes: Any) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    changes["updated_at"] = datetime.now(tz=UTC)
    if changes.get("status") == "completed" and "completed_at" not in changes:
        changes["completed_at"] = changes["updated_at"]
    if changes.get("status") == "cancelled" and "cancelled_at" not in changes:
        changes["cancelled_at"] = changes["updated_at"]
    async with engine.begin() as conn:
        await conn.execute(update(model_downloads).where(model_downloads.c.id == download_id).values(**changes))
    return await get_model_download(download_id)


async def claim_next_model_download() -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(
            select(model_downloads)
            .where(model_downloads.c.status.in_(["queued", "running", "pausing", "cancelling"]))
            .order_by(model_downloads.c.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = result.mappings().first()
        if row is None:
            return None
        if row["status"] in {"pausing", "cancelling"}:
            values = {"stage": row["status"], "updated_at": now}
        else:
            values = {"status": "running", "stage": "downloading", "updated_at": now}
        await conn.execute(update(model_downloads).where(model_downloads.c.id == row["id"]).values(**values))
        return {**dict(row), **values}


async def request_model_download_cancel(download_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(model_downloads).where(model_downloads.c.id == download_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        if row["status"] in {"completed", "failed", "cancelled"}:
            return dict(row)
        if row["status"] in {"queued", "paused"}:
            await conn.execute(
                update(model_downloads)
                .where(model_downloads.c.id == download_id)
                .values(status="cancelled", stage="cancelled", cancelled_at=now, updated_at=now)
            )
            return {
                **dict(row),
                "status": "cancelled",
                "stage": "cancelled",
                "cancelled_at": now,
                "updated_at": now,
            }
        await conn.execute(
            update(model_downloads)
            .where(model_downloads.c.id == download_id)
            .values(status="cancelling", stage="cancelling", updated_at=now)
        )
    return await get_model_download(download_id)


async def request_model_download_pause(download_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(model_downloads).where(model_downloads.c.id == download_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current_status = str(row["status"])
        if current_status in {"completed", "failed", "cancelled", "paused"}:
            return dict(row)
        if current_status == "queued":
            await conn.execute(
                update(model_downloads)
                .where(model_downloads.c.id == download_id)
                .values(status="paused", stage="paused", updated_at=now)
            )
            return {
                **dict(row),
                "status": "paused",
                "stage": "paused",
                "updated_at": now,
            }
        if current_status in {"running", "pausing"}:
            await conn.execute(
                update(model_downloads)
                .where(model_downloads.c.id == download_id)
                .values(status="pausing", stage="pausing", updated_at=now)
            )
            return {
                **dict(row),
                "status": "pausing",
                "stage": "pausing",
                "updated_at": now,
            }
        raise ValueError(f"model download cannot be paused from status {current_status}")


async def resume_model_download(download_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(model_downloads).where(model_downloads.c.id == download_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current_status = str(row["status"])
        if current_status != "paused":
            raise ValueError(f"model download cannot be resumed from status {current_status}")
        await conn.execute(
            update(model_downloads)
            .where(model_downloads.c.id == download_id)
            .values(status="queued", stage="queued", updated_at=now)
        )
        return {
            **dict(row),
            "status": "queued",
            "stage": "queued",
            "updated_at": now,
        }


async def retry_model_download(download_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(model_downloads).where(model_downloads.c.id == download_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current_status = str(row["status"])
        if current_status not in {"failed", "cancelled"}:
            raise ValueError(f"model download cannot be retried from status {current_status}")
        await conn.execute(
            update(model_downloads)
            .where(model_downloads.c.id == download_id)
            .values(
                status="queued",
                stage="queued",
                error_category=None,
                error_message=None,
                cancelled_at=None,
                completed_at=None,
                updated_at=now,
            )
        )
        return {
            **dict(row),
            "status": "queued",
            "stage": "queued",
            "error_category": None,
            "error_message": None,
            "cancelled_at": None,
            "completed_at": None,
            "updated_at": now,
        }


async def get_job(job_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(jobs).where(jobs.c.id == job_id))
        row = result.mappings().first()
    return dict(row) if row else None


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


def table_export_schema(table: Table) -> dict[str, Any]:
    return {
        "name": table.name,
        "primary_key": [column.name for column in table.primary_key.columns],
        "columns": [
            {
                "name": column.name,
                "type": str(column.type),
                "nullable": bool(column.nullable),
                "primary_key": bool(column.primary_key),
            }
            for column in table.columns
        ],
    }


def logical_dump_payload(rows_by_table: dict[str, list[dict[str, Any]]], created_at: datetime | None = None) -> dict[str, Any]:
    created = created_at or datetime.now(tz=UTC)
    tables_payload: list[dict[str, Any]] = []
    for table in EXPORT_TABLES:
        rows = rows_by_table.get(table.name, [])
        tables_payload.append(
            {
                "schema": table_export_schema(table),
                "row_count": len(rows),
                "rows": json_safe(rows),
            }
        )
    return {
        "format": LOGICAL_EXPORT_FORMAT,
        "created_at": created.isoformat(),
        "source": "control-plane",
        "restore_contract": "Run migrations first, then import rows with primary-key upsert semantics.",
        "tables": tables_payload,
        "row_counts": {table.name: len(rows_by_table.get(table.name, [])) for table in EXPORT_TABLES},
    }


def load_logical_dump(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise LogicalDumpError(f"logical dump not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_logical_dump(payload)
    return payload


def payload_tables_by_name(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    for item in payload.get("tables", []):
        schema = item.get("schema") or {}
        name = schema.get("name")
        if not isinstance(name, str):
            raise LogicalDumpError("dump table is missing schema.name")
        if name in tables:
            raise LogicalDumpError(f"duplicate dump table: {name}")
        tables[name] = item
    return tables


def validate_logical_dump(payload: dict[str, Any]) -> None:
    if payload.get("format") != LOGICAL_EXPORT_FORMAT:
        raise LogicalDumpError("unsupported logical dump format")
    if not isinstance(payload.get("tables"), list):
        raise LogicalDumpError("logical dump must contain a tables list")
    tables = payload_tables_by_name(payload)
    unknown = sorted(set(tables) - set(EXPORT_TABLES_BY_NAME))
    if unknown:
        raise LogicalDumpError(f"logical dump contains unknown tables: {', '.join(unknown)}")
    for table_name, item in tables.items():
        rows = item.get("rows")
        if not isinstance(rows, list):
            raise LogicalDumpError(f"table {table_name} rows must be a list")
        if item.get("row_count") != len(rows):
            raise LogicalDumpError(f"table {table_name} row_count does not match rows length")
        table = EXPORT_TABLES_BY_NAME[table_name]
        allowed_columns = {column.name for column in table.columns}
        primary_keys = {column.name for column in table.primary_key.columns}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise LogicalDumpError(f"table {table_name} row {index} is not an object")
            unknown_columns = sorted(set(row) - allowed_columns)
            if unknown_columns:
                raise LogicalDumpError(f"table {table_name} row {index} has unknown columns: {', '.join(unknown_columns)}")
            missing_primary_keys = sorted(primary_keys - set(row))
            if missing_primary_keys:
                raise LogicalDumpError(f"table {table_name} row {index} is missing primary keys: {', '.join(missing_primary_keys)}")


def logical_dump_import_plan(payload: dict[str, Any]) -> dict[str, Any]:
    validate_logical_dump(payload)
    tables = payload_tables_by_name(payload)
    operations: list[dict[str, Any]] = []
    for table in EXPORT_TABLES:
        item = tables.get(table.name, {"rows": []})
        rows = item.get("rows", [])
        operations.append(
            {
                "table": table.name,
                "mode": "upsert",
                "primary_key": [column.name for column in table.primary_key.columns],
                "row_count": len(rows),
            }
        )
    return {
        "format": payload["format"],
        "created_at": payload.get("created_at"),
        "source": payload.get("source"),
        "status": "planned",
        "apply_supported": True,
        "table_count": len(operations),
        "row_counts": {operation["table"]: operation["row_count"] for operation in operations},
        "operations": operations,
    }


def coerce_import_value(column: Column[Any], value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def coerce_import_row(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    columns_by_name = {column.name: column for column in table.columns}
    return {
        key: coerce_import_value(columns_by_name[key], value)
        for key, value in row.items()
    }


async def export_logical_dump(output_path: Path) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    async with engine.connect() as conn:
        for table in EXPORT_TABLES:
            order_by = list(table.primary_key.columns) or [next(iter(table.columns))]
            result = await conn.execute(select(table).order_by(*order_by))
            rows_by_table[table.name] = [dict(row) for row in result.mappings().all()]
    payload = logical_dump_payload(rows_by_table)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".partial")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(output_path)
    output_path.chmod(0o600)
    return {
        "format": payload["format"],
        "path": str(output_path),
        "table_count": len(payload["tables"]),
        "row_counts": payload["row_counts"],
    }


async def import_logical_dump(input_path: Path, apply: bool = False) -> dict[str, Any]:
    payload = load_logical_dump(input_path)
    plan = logical_dump_import_plan(payload)
    if not apply:
        return plan
    if engine is None:
        raise RuntimeError("database engine is not configured")
    tables = payload_tables_by_name(payload)
    applied_row_counts: dict[str, int] = {}
    async with engine.begin() as conn:
        for table in EXPORT_TABLES:
            rows = [
                coerce_import_row(table, row)
                for row in tables.get(table.name, {"rows": []}).get("rows", [])
            ]
            if not rows:
                applied_row_counts[table.name] = 0
                continue
            stmt = pg_insert(table).values(rows)
            primary_key_columns = list(table.primary_key.columns)
            primary_key_names = {column.name for column in primary_key_columns}
            updated_columns = {
                column.name: getattr(stmt.excluded, column.name)
                for column in table.columns
                if column.name not in primary_key_names and any(column.name in row for row in rows)
            }
            if updated_columns:
                stmt = stmt.on_conflict_do_update(index_elements=primary_key_columns, set_=updated_columns)
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=primary_key_columns)
            await conn.execute(stmt)
            applied_row_counts[table.name] = len(rows)
    return {
        **plan,
        "status": "imported",
        "applied_row_counts": applied_row_counts,
    }


async def get_job_by_idempotency_key(owner_id: str, idempotency_key: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(
            select(jobs).where(and_(jobs.c.owner_id == owner_id, jobs.c.idempotency_key == idempotency_key)).limit(1)
        )
        row = result.mappings().first()
    return dict(row) if row else None


async def get_job_by_artifact_url(artifact_url: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(jobs).where(jobs.c.artifacts.contains([{"url": artifact_url}])).limit(1))
        row = result.mappings().first()
    return dict(row) if row else None


async def get_job_by_native_prompt_id(prompt_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(jobs).where(jobs.c.native_prompt_id == prompt_id).limit(1))
        row = result.mappings().first()
    return dict(row) if row else None


async def update_job(job_id: str, **changes: Any) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    changes["updated_at"] = datetime.now(tz=UTC)
    if changes.get("state") in {"completed", "cancelled", "failed", "expired"} and "completed_at" not in changes:
        changes["completed_at"] = changes["updated_at"]
    async with engine.begin() as conn:
        await conn.execute(update(jobs).where(jobs.c.id == job_id).values(**changes))
    return await get_job(job_id)


async def list_jobs(
    limit: int = 50,
    *,
    owner_id: str | None = None,
    state: str | None = None,
    runtime: str | None = None,
    modality: str | None = None,
) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    bounded_limit = max(1, min(int(limit), 500))
    filters = []
    if owner_id:
        filters.append(jobs.c.owner_id == owner_id)
    if state:
        filters.append(jobs.c.state == state)
    if runtime:
        filters.append(jobs.c.runtime == runtime)
    if modality:
        filters.append(jobs.c.modality == modality)
    query = select(jobs)
    if filters:
        query = query.where(and_(*filters))
    query = query.order_by(jobs.c.created_at.desc()).limit(bounded_limit)
    async with engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def count_jobs(
    *,
    owner_id: str | None = None,
    states: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    created_after: datetime | None = None,
) -> int:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    filters = []
    if owner_id:
        filters.append(jobs.c.owner_id == owner_id)
    if states:
        filters.append(jobs.c.state.in_(tuple(states)))
    if created_after is not None:
        filters.append(jobs.c.created_at >= created_after)
    query = select(func.count()).select_from(jobs)
    if filters:
        query = query.where(and_(*filters))
    async with engine.connect() as conn:
        result = await conn.execute(query)
        return int(result.scalar_one())


async def list_artifact_retention_jobs(cutoff: datetime, limit: int = 5000) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    bounded_limit = max(1, min(int(limit), 50000))
    query = (
        select(jobs)
        .where(and_(jobs.c.state.in_(TERMINAL_JOB_STATES), jobs.c.completed_at.is_not(None), jobs.c.completed_at < cutoff))
        .order_by(jobs.c.completed_at.asc(), jobs.c.created_at.asc())
        .limit(bounded_limit)
    )
    async with engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def list_recent_jobs(limit: int = 50) -> list[dict[str, Any]]:
    return await list_jobs(limit=limit)


async def request_job_cancel(job_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(jobs).where(jobs.c.id == job_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current = dict(row)
        if current["state"] in TERMINAL_JOB_STATES:
            return current
        if current["state"] in CANCEL_IMMEDIATE_STATES:
            values = {
                "state": JobState.CANCELLED.value,
                "stage": "cancelled",
                "progress": 100,
                "completed_at": now,
                "updated_at": now,
            }
        else:
            values = {
                "state": JobState.CANCELLING.value,
                "stage": "cancelling",
                "updated_at": now,
            }
        await conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))
        return {**current, **values}


async def update_job_priority(job_id: str, priority: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    if priority not in VALID_JOB_PRIORITIES:
        raise ValueError(f"unsupported job priority: {priority}")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(jobs).where(jobs.c.id == job_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current = dict(row)
        if current["state"] not in PRIORITIZABLE_JOB_STATES:
            raise ValueError(f"job state {current['state']} cannot be reprioritized")
        values = {"priority": priority, "updated_at": now}
        await conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))
        return {**current, **values}


async def retry_job(job_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(jobs).where(jobs.c.id == job_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current = dict(row)
        if current["state"] not in RETRYABLE_JOB_STATES:
            raise ValueError(f"job state {current['state']} cannot be retried")
        values = {
            "state": JobState.QUEUED.value,
            "stage": "queued",
            "progress": 10,
            "retry_count": int(current.get("retry_count") or 0) + 1,
            "failure_category": None,
            "failure_message": None,
            "native_prompt_id": None,
            "artifacts": [],
            "started_at": None,
            "completed_at": None,
            "load_time_ms": None,
            "run_time_ms": None,
            "peak_vram_mib": None,
            "peak_ram_mib": None,
            "updated_at": now,
        }
        await conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))
        return {**current, **values}


async def count_active_jobs_for_model(model_ref: str, aliases: list[str]) -> int:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    active_states = [
        "created",
        "validated",
        "queued",
        "waiting_for_gpu",
        "unloading",
        "verifying_vram",
        "loading",
        "warming",
        "running",
        "saving",
        "cancelling",
        "recovery_required",
    ]
    filters = [jobs.c.resolved_model_version == model_ref]
    if aliases:
        filters.append(jobs.c.model_alias.in_(aliases))
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.count().label("count"))
            .select_from(jobs)
            .where(and_(jobs.c.state.in_(active_states), or_(*filters)))
        )
        row = result.mappings().first()
    return int(row["count"] if row else 0)


async def insert_audit_event(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    row = audit.make_audit_event(
        event_type=payload["event_type"],
        actor_id=payload.get("actor_id"),
        actor_role=payload.get("actor_role"),
        actor_key_prefix=payload.get("actor_key_prefix"),
        target_type=payload.get("target_type"),
        target_id=payload.get("target_id"),
        summary=payload.get("summary", ""),
        metadata=payload.get("metadata") or {},
        correlation_id=payload.get("correlation_id"),
        remote_addr=payload.get("remote_addr"),
        created_at=payload.get("created_at"),
        event_id=payload.get("id"),
    )
    async with engine.begin() as conn:
        await conn.execute(insert(audit_events).values(**row))
    return row


async def list_audit_events(
    limit: int = 100,
    event_type: str | None = None,
    actor_id: str | None = None,
    target_type: str | None = None,
) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    bounded_limit = max(1, min(int(limit), 500))
    filters = []
    if event_type:
        filters.append(audit_events.c.event_type == event_type)
    if actor_id:
        filters.append(audit_events.c.actor_id == actor_id)
    if target_type:
        filters.append(audit_events.c.target_type == target_type)
    query = select(audit_events)
    if filters:
        query = query.where(and_(*filters))
    query = query.order_by(audit_events.c.created_at.desc()).limit(bounded_limit)
    async with engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def claim_next_job(
    runtime_names: list[str],
    claimed_state: str = "running",
    claimed_stage: str = "running",
    claimed_progress: int = 50,
    claimable_states: list[str] | None = None,
    respect_runtime_reservations: bool = False,
) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    if not runtime_names:
        return None
    states = claimable_states or ["queued"]
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        active_reservations: list[dict[str, Any]] = []
        if respect_runtime_reservations:
            await conn.execute(
                update(runtime_reservations)
                .where(and_(runtime_reservations.c.status == "active", runtime_reservations.c.expires_at <= now))
                .values(status="expired", updated_at=now)
            )
            reservation_result = await conn.execute(
                select(runtime_reservations)
                .where(and_(runtime_reservations.c.status == "active", runtime_reservations.c.expires_at > now, runtime_reservations.c.runtime.in_(runtime_names)))
                .order_by(runtime_reservations.c.created_at.asc())
            )
            active_reservations = [dict(row) for row in reservation_result.mappings().all()]
        filters = [jobs.c.state.in_(states), jobs.c.runtime.in_(runtime_names)]
        if active_reservations:
            filters.append(
                or_(
                    *[
                        and_(
                            jobs.c.owner_id == reservation["owner_id"],
                            jobs.c.runtime == reservation["runtime"],
                            jobs.c.resolved_model_version == reservation["resolved_model_version"],
                        )
                        for reservation in active_reservations
                    ]
                )
            )
        result = await conn.execute(
            select(jobs)
            .where(and_(*filters))
            .order_by(jobs.c.created_at.asc())
            .with_for_update(skip_locked=True)
        )
        candidates = [dict(row) for row in result.mappings().all()]
        row = select_claim_candidate(candidates, now=now)
        if row is None:
            return None
        values = {"state": claimed_state, "stage": claimed_stage, "progress": claimed_progress, "updated_at": now}
        await conn.execute(update(jobs).where(jobs.c.id == row["id"]).values(**values))
        return {**row, **values}


async def mark_interrupted_jobs_recovery_required(runtime_names: list[str]) -> int:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    if not runtime_names:
        return 0
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(
            update(jobs)
            .where(
                and_(
                    jobs.c.runtime.in_(runtime_names),
                    jobs.c.state.in_(
                        [
                            "waiting_for_gpu",
                            "unloading",
                            "verifying_vram",
                            "loading",
                            "warming",
                            "running",
                            "saving",
                            "cancelling",
                        ]
                    ),
                )
            )
            .values(state="recovery_required", stage="recovery_required", progress=0, updated_at=now)
        )
    return int(result.rowcount or 0)


async def requeue_recovery_jobs(runtime_names: list[str]) -> int:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    if not runtime_names:
        return 0
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(
            update(jobs)
            .where(and_(jobs.c.runtime.in_(runtime_names), jobs.c.state == "recovery_required"))
            .values(state="queued", stage="queued", progress=10, updated_at=now)
        )
    return int(result.rowcount or 0)


async def job_counts_by_state() -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT state, count(*) AS count FROM b1_jobs GROUP BY state ORDER BY state"))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def upsert_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "unpublished_at": None,
        **payload,
    }
    update_values = {
        key: value
        for key, value in row.items()
        if key not in {"id", "version", "created_at"}
    }
    update_values["updated_at"] = now
    async with engine.begin() as conn:
        existing = await conn.execute(select(workflows).where(and_(workflows.c.id == row["id"], workflows.c.version == row["version"])))
        if existing.mappings().first() is None:
            await conn.execute(insert(workflows).values(**row))
        else:
            await conn.execute(
                update(workflows)
                .where(and_(workflows.c.id == row["id"], workflows.c.version == row["version"]))
                .values(**update_values)
            )
    return await get_workflow(row["id"], row["version"]) or row


async def list_workflows(include_unpublished: bool = False) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        query = select(workflows)
        if not include_unpublished:
            query = query.where(workflows.c.unpublished_at.is_(None))
        result = await conn.execute(query.order_by(workflows.c.id.asc(), workflows.c.version.desc()))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def get_workflow(workflow_id: str, version: str | None = None) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        query = select(workflows).where(and_(workflows.c.id == workflow_id, workflows.c.unpublished_at.is_(None)))
        if version is not None:
            query = query.where(workflows.c.version == version)
        result = await conn.execute(query.order_by(workflows.c.version.desc()).limit(1))
        row = result.mappings().first()
    return dict(row) if row else None


async def unpublish_workflow(workflow_id: str, version: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        await conn.execute(
            update(workflows)
            .where(and_(workflows.c.id == workflow_id, workflows.c.version == version))
            .values(status="unpublished", unpublished_at=now, updated_at=now)
        )
    async with engine.connect() as conn:
        result = await conn.execute(select(workflows).where(and_(workflows.c.id == workflow_id, workflows.c.version == version)))
        row = result.mappings().first()
    return dict(row) if row else None


def normalize_username(username: str) -> str:
    return username.strip().casefold()


async def active_user_count(role: str | None = None) -> int:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    filters = [users.c.disabled_at.is_(None)]
    if role is not None:
        filters.append(users.c.role == role)
    async with engine.connect() as conn:
        result = await conn.execute(select(func.count().label("count")).select_from(users).where(and_(*filters)))
        row = result.mappings().first()
    return int(row["count"] if row else 0)


async def insert_user(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    username = payload["username"].strip()
    row = {
        "created_at": now,
        "updated_at": now,
        "last_login_at": None,
        "disabled_at": None,
        "display_name": username,
        **payload,
        "username": username,
        "username_normalized": normalize_username(username),
    }
    async with engine.begin() as conn:
        await conn.execute(insert(users).values(**row))
    return row


async def get_user(user_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(users).where(users.c.id == user_id).limit(1))
        row = result.mappings().first()
    return dict(row) if row else None


async def get_user_by_username(username: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    normalized = normalize_username(username)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(users).where(and_(users.c.username_normalized == normalized, users.c.disabled_at.is_(None))).limit(1)
        )
        row = result.mappings().first()
    return dict(row) if row else None


async def mark_user_login(user_id: str) -> None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        await conn.execute(update(users).where(users.c.id == user_id).values(last_login_at=now, updated_at=now))


async def insert_browser_session(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "revoked_at": None,
        "user_agent": "",
        "remote_addr": None,
        **payload,
    }
    async with engine.begin() as conn:
        await conn.execute(insert(browser_sessions).values(**row))
    return row


async def get_browser_session_by_hash(session_hash: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(
            select(browser_sessions)
            .where(
                and_(
                    browser_sessions.c.session_hash == session_hash,
                    browser_sessions.c.revoked_at.is_(None),
                    browser_sessions.c.expires_at > now,
                )
            )
            .limit(1)
        )
        row = result.mappings().first()
        if row:
            await conn.execute(
                update(browser_sessions)
                .where(browser_sessions.c.id == row["id"])
                .values(last_seen_at=now, updated_at=now)
            )
    return dict(row) if row else None


async def revoke_browser_session_by_hash(session_hash: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(browser_sessions).where(browser_sessions.c.session_hash == session_hash).limit(1))
        row = result.mappings().first()
        if row and row["revoked_at"] is None:
            await conn.execute(
                update(browser_sessions)
                .where(browser_sessions.c.id == row["id"])
                .values(revoked_at=now, updated_at=now)
            )
    return dict(row) if row else None


async def revoke_expired_browser_sessions() -> int:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(
            update(browser_sessions)
            .where(and_(browser_sessions.c.revoked_at.is_(None), browser_sessions.c.expires_at <= now))
            .values(revoked_at=now, updated_at=now)
        )
    return int(result.rowcount or 0)


async def insert_api_client(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "last_used_at": None,
        "revoked_at": None,
        "cidr_allowlist": [],
        **payload,
    }
    async with engine.begin() as conn:
        await conn.execute(insert(api_clients).values(**row))
    return row


async def upsert_api_client(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "last_used_at": None,
        "revoked_at": None,
        "cidr_allowlist": [],
        **payload,
    }
    stmt = pg_insert(api_clients).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[api_clients.c.id],
        set_={
            "display_name": stmt.excluded.display_name,
            "role": stmt.excluded.role,
            "scopes": stmt.excluded.scopes,
            "key_prefix": stmt.excluded.key_prefix,
            "key_salt": stmt.excluded.key_salt,
            "key_hash": stmt.excluded.key_hash,
            "cidr_allowlist": stmt.excluded.cidr_allowlist,
            "updated_at": now,
            "revoked_at": None,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return await get_api_client(row["id"]) or row


async def get_api_client(client_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(api_clients).where(api_clients.c.id == client_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def get_api_client_by_prefix(key_prefix: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.begin() as conn:
        result = await conn.execute(
            select(api_clients).where(and_(api_clients.c.key_prefix == key_prefix, api_clients.c.revoked_at.is_(None))).limit(1)
        )
        row = result.mappings().first()
        if row:
            await conn.execute(
                update(api_clients)
                .where(api_clients.c.id == row["id"])
                .values(last_used_at=datetime.now(tz=UTC), updated_at=datetime.now(tz=UTC))
            )
    return dict(row) if row else None


async def list_api_clients() -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(api_clients).order_by(api_clients.c.created_at.desc()))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def revoke_api_client(client_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        await conn.execute(
            update(api_clients)
            .where(api_clients.c.id == client_id)
            .values(revoked_at=now, updated_at=now)
        )
    async with engine.connect() as conn:
        result = await conn.execute(select(api_clients).where(api_clients.c.id == client_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def insert_runtime_reservation(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "cancelled_at": None,
        "status": "active",
        "reason": "",
        **payload,
    }
    async with engine.begin() as conn:
        await conn.execute(insert(runtime_reservations).values(**row))
    return row


def runtime_reservation_matches_request(
    reservation: dict[str, Any],
    owner_id: str,
    runtime: str,
    resolved_model_version: str,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(tz=UTC)
    return (
        reservation.get("status") == "active"
        and reservation.get("expires_at") > current_time
        and reservation.get("owner_id") == owner_id
        and reservation.get("runtime") == runtime
        and reservation.get("resolved_model_version") == resolved_model_version
    )


async def list_active_runtime_reservations(runtime_names: list[str] | None = None) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        await conn.execute(
            update(runtime_reservations)
            .where(and_(runtime_reservations.c.status == "active", runtime_reservations.c.expires_at <= now))
            .values(status="expired", updated_at=now)
        )
        query = select(runtime_reservations).where(and_(runtime_reservations.c.status == "active", runtime_reservations.c.expires_at > now))
        if runtime_names:
            query = query.where(runtime_reservations.c.runtime.in_(runtime_names))
        query = query.order_by(runtime_reservations.c.created_at.asc())
        result = await conn.execute(query)
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def list_runtime_reservations(
    limit: int = 50,
    *,
    owner_id: str | None = None,
    status: str | None = None,
    runtime: str | None = None,
) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    bounded_limit = max(1, min(int(limit), 500))
    filters = []
    if owner_id:
        filters.append(runtime_reservations.c.owner_id == owner_id)
    if status:
        filters.append(runtime_reservations.c.status == status)
    if runtime:
        filters.append(runtime_reservations.c.runtime == runtime)
    query = select(runtime_reservations)
    if filters:
        query = query.where(and_(*filters))
    query = query.order_by(runtime_reservations.c.created_at.desc()).limit(bounded_limit)
    async with engine.begin() as conn:
        await conn.execute(
            update(runtime_reservations)
            .where(and_(runtime_reservations.c.status == "active", runtime_reservations.c.expires_at <= now))
            .values(status="expired", updated_at=now)
        )
        result = await conn.execute(query)
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def runtime_reservation_gate(
    owner_id: str,
    runtime: str,
    resolved_model_version: str,
    runtime_names: list[str] | None = None,
) -> dict[str, Any]:
    reservations = await list_active_runtime_reservations(runtime_names)
    if not reservations:
        return {"allowed": True, "reservation": None, "active_reservation": None}
    now = datetime.now(tz=UTC)
    matching = next(
        (
            reservation
            for reservation in reservations
            if runtime_reservation_matches_request(reservation, owner_id, runtime, resolved_model_version, now=now)
        ),
        None,
    )
    return {
        "allowed": matching is not None,
        "reservation": matching,
        "active_reservation": reservations[0],
    }


async def get_runtime_reservation(reservation_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(runtime_reservations).where(runtime_reservations.c.id == reservation_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def cancel_runtime_reservation(reservation_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(runtime_reservations).where(runtime_reservations.c.id == reservation_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        if row["status"] == "active":
            await conn.execute(
                update(runtime_reservations)
                .where(runtime_reservations.c.id == reservation_id)
                .values(status="cancelled", cancelled_at=now, updated_at=now)
            )
    return await get_runtime_reservation(reservation_id)


async def insert_modelhub_client_with_api_client(api_payload: dict[str, Any], client_payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    api_row = {
        "created_at": now,
        "updated_at": now,
        "last_used_at": None,
        "revoked_at": None,
        **api_payload,
    }
    client_row = {
        "created_at": now,
        "updated_at": now,
        "revoked_at": None,
        "cidr_allowlist": [],
        "allow_downloads": True,
        **client_payload,
    }
    async with engine.begin() as conn:
        await conn.execute(insert(api_clients).values(**api_row))
        await conn.execute(insert(modelhub_clients).values(**client_row))
    return {"api_client": api_row, "modelhub_client": client_row}


async def get_modelhub_client(client_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(modelhub_clients).where(modelhub_clients.c.id == client_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def list_modelhub_clients() -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(modelhub_clients).order_by(modelhub_clients.c.created_at.desc()))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def get_modelhub_client_by_api_client(api_client_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(
            select(modelhub_clients)
            .where(and_(modelhub_clients.c.api_client_id == api_client_id, modelhub_clients.c.revoked_at.is_(None)))
            .limit(1)
        )
        row = result.mappings().first()
    return dict(row) if row else None


async def revoke_modelhub_client(client_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(modelhub_clients).where(modelhub_clients.c.id == client_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        await conn.execute(
            update(modelhub_clients)
            .where(modelhub_clients.c.id == client_id)
            .values(revoked_at=now, updated_at=now)
        )
        await conn.execute(
            update(api_clients)
            .where(api_clients.c.id == row["api_client_id"])
            .values(revoked_at=now, updated_at=now)
        )
    return await get_modelhub_client(client_id)


async def insert_voice_profile(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
        "status": "active",
        "visibility_roles": ["admin", "operator"],
        "metadata": {},
        "sample_artifacts": [],
        **payload,
    }
    async with engine.begin() as conn:
        await conn.execute(insert(voice_profiles).values(**row))
    return row


async def get_voice_profile(profile_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    query = select(voice_profiles).where(voice_profiles.c.id == profile_id)
    if not include_deleted:
        query = query.where(voice_profiles.c.deleted_at.is_(None))
    async with engine.connect() as conn:
        result = await conn.execute(query)
        row = result.mappings().first()
    return dict(row) if row else None


async def list_voice_profiles(
    include_deleted: bool = False,
    owner_id: str | None = None,
    runtime: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    filters = []
    if not include_deleted:
        filters.append(voice_profiles.c.deleted_at.is_(None))
    if owner_id:
        filters.append(voice_profiles.c.owner_id == owner_id)
    if runtime:
        filters.append(voice_profiles.c.runtime == runtime)
    if status:
        filters.append(voice_profiles.c.status == status)
    query = select(voice_profiles)
    if filters:
        query = query.where(and_(*filters))
    query = query.order_by(voice_profiles.c.updated_at.desc(), voice_profiles.c.created_at.desc())
    async with engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def update_voice_profile(profile_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    mutable_changes = {
        key: value
        for key, value in changes.items()
        if key
        in {
            "display_name",
            "runtime",
            "engine",
            "model_alias",
            "profile_type",
            "status",
            "visibility_roles",
            "metadata",
            "sample_artifacts",
        }
    }
    if not mutable_changes:
        return await get_voice_profile(profile_id)
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(voice_profiles).where(voice_profiles.c.id == profile_id).with_for_update())
        row = result.mappings().first()
        if row is None or row["deleted_at"] is not None:
            return None
        await conn.execute(
            update(voice_profiles)
            .where(voice_profiles.c.id == profile_id)
            .values(**mutable_changes, updated_at=now)
        )
    return await get_voice_profile(profile_id)


async def delete_voice_profile(profile_id: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(voice_profiles).where(voice_profiles.c.id == profile_id).with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        if row["deleted_at"] is None:
            await conn.execute(
                update(voice_profiles)
                .where(voice_profiles.c.id == profile_id)
                .values(status="deleted", deleted_at=now, updated_at=now)
            )
    return await get_voice_profile(profile_id, include_deleted=True)


async def upsert_runtime_state(payload: dict[str, Any]) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    row = {
        "runtime": payload["runtime"],
        "status": payload.get("status", "unknown"),
        "stage": payload.get("stage", "unknown"),
        "active_model": payload.get("active_model"),
        "model_alias": payload.get("model_alias"),
        "resolved_model_version": payload.get("resolved_model_version"),
        "job_id": payload.get("job_id"),
        "details": payload.get("details") or {},
        "updated_at": now,
    }
    stmt = pg_insert(runtime_state).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[runtime_state.c.runtime],
        set_={
            "status": stmt.excluded.status,
            "stage": stmt.excluded.stage,
            "active_model": stmt.excluded.active_model,
            "model_alias": stmt.excluded.model_alias,
            "resolved_model_version": stmt.excluded.resolved_model_version,
            "job_id": stmt.excluded.job_id,
            "details": stmt.excluded.details,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return row


async def list_runtime_states() -> list[dict[str, Any]]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(runtime_state).order_by(runtime_state.c.runtime.asc()))
        rows = result.mappings().all()
    return [dict(row) for row in rows]


def redis_lease_value(owner: str, epoch: int) -> str:
    return json.dumps({"owner": owner, "epoch": epoch}, separators=(",", ":"), sort_keys=True)


def redis_lease_owner(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    owner = payload.get("owner")
    return owner if isinstance(owner, str) and owner else None


async def acquire_redis_scheduler_owner(owner: str, epoch: int, ttl_seconds: int) -> dict[str, Any]:
    if scheduler_redis_client is None:
        return {"enabled": False, "acquired": True}
    lease_value = redis_lease_value(owner, epoch)
    ttl_ms = max(1, int(ttl_seconds * 1000))
    try:
        current = await scheduler_redis_client.get(SCHEDULER_REDIS_LEASE_KEY)
        if current is None:
            acquired = bool(await scheduler_redis_client.set(SCHEDULER_REDIS_LEASE_KEY, lease_value, px=ttl_ms, nx=True))
            if acquired:
                return {"enabled": True, "acquired": True, "owner": owner, "epoch": epoch, "renewed": False}
            current = await scheduler_redis_client.get(SCHEDULER_REDIS_LEASE_KEY)
        current_owner = redis_lease_owner(current)
        if current_owner == owner:
            await scheduler_redis_client.set(SCHEDULER_REDIS_LEASE_KEY, lease_value, px=ttl_ms)
            return {"enabled": True, "acquired": True, "owner": owner, "epoch": epoch, "renewed": True}
        return {"enabled": True, "acquired": False, "current_owner": current_owner, "current_value": current}
    except Exception as exc:  # pragma: no cover - exact Redis client exceptions vary by runtime
        return {"enabled": True, "acquired": False, "error": exc.__class__.__name__}


async def get_redis_scheduler_owner() -> dict[str, Any]:
    if scheduler_redis_client is None:
        return {"enabled": False}
    try:
        current = await scheduler_redis_client.get(SCHEDULER_REDIS_LEASE_KEY)
    except Exception as exc:  # pragma: no cover - exact Redis client exceptions vary by runtime
        return {"enabled": True, "reachable": False, "error": exc.__class__.__name__}
    return {
        "enabled": True,
        "reachable": True,
        "owner": redis_lease_owner(current),
        "value": current.decode("utf-8", errors="replace") if isinstance(current, bytes) else current,
    }


async def release_redis_scheduler_owner(owner: str) -> dict[str, Any]:
    if scheduler_redis_client is None:
        return {"enabled": False, "released": True}
    try:
        current = await scheduler_redis_client.get(SCHEDULER_REDIS_LEASE_KEY)
        current_owner = redis_lease_owner(current)
        if current_owner != owner:
            return {"enabled": True, "released": False, "current_owner": current_owner}
        await scheduler_redis_client.delete(SCHEDULER_REDIS_LEASE_KEY)
        return {"enabled": True, "released": True, "owner": owner}
    except Exception as exc:  # pragma: no cover - exact Redis client exceptions vary by runtime
        return {"enabled": True, "released": False, "error": exc.__class__.__name__}


async def acquire_postgres_scheduler_owner(owner: str, ttl_seconds: int) -> dict[str, Any]:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    expires = now + timedelta(seconds=ttl_seconds)
    async with engine.begin() as conn:
        result = await conn.execute(select(scheduler_owner).where(scheduler_owner.c.id == "gpu").with_for_update())
        row = result.mappings().first()
        if row is None:
            values = {"id": "gpu", "owner": owner, "epoch": 1, "lease_expires_at": expires, "updated_at": now}
            await conn.execute(insert(scheduler_owner).values(**values))
            return {**values, "acquired": True}
        current = dict(row)
        expired = current["lease_expires_at"] <= now
        same_owner = current["owner"] == owner
        if expired or same_owner:
            values = {
                "owner": owner,
                "epoch": int(current["epoch"]) + (0 if same_owner and not expired else 1),
                "lease_expires_at": expires,
                "updated_at": now,
            }
            await conn.execute(update(scheduler_owner).where(scheduler_owner.c.id == "gpu").values(**values))
            return {"id": "gpu", **values, "acquired": True}
        return {**current, "acquired": False}


async def acquire_scheduler_owner(owner: str, ttl_seconds: int) -> dict[str, Any]:
    row = await acquire_postgres_scheduler_owner(owner, ttl_seconds)
    if not row.get("acquired"):
        return row
    redis_lease = await acquire_redis_scheduler_owner(owner, int(row["epoch"]), ttl_seconds)
    if redis_lease.get("acquired"):
        return {**row, "redis_lease": redis_lease}
    postgres_release = await release_postgres_scheduler_owner(owner)
    return {
        **row,
        "acquired": False,
        "postgres_acquired": True,
        "redis_lease": redis_lease,
        "postgres_release": postgres_release,
        "message": "Redis scheduler lease is held by another owner or unavailable",
    }


async def get_scheduler_owner() -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    async with engine.connect() as conn:
        result = await conn.execute(select(scheduler_owner).where(scheduler_owner.c.id == "gpu"))
        row = result.mappings().first()
    if row is None:
        return None
    return {**dict(row), "redis_lease": await get_redis_scheduler_owner()}


async def release_postgres_scheduler_owner(owner: str) -> dict[str, Any] | None:
    if engine is None:
        raise RuntimeError("database engine is not configured")
    now = datetime.now(tz=UTC)
    async with engine.begin() as conn:
        result = await conn.execute(select(scheduler_owner).where(scheduler_owner.c.id == "gpu").with_for_update())
        row = result.mappings().first()
        if row is None:
            return None
        current = dict(row)
        if current["owner"] != owner:
            return {**current, "released": False}
        values = {"lease_expires_at": now, "updated_at": now}
        await conn.execute(update(scheduler_owner).where(scheduler_owner.c.id == "gpu").values(**values))
        return {**current, **values, "released": True}


async def release_scheduler_owner(owner: str) -> dict[str, Any] | None:
    row = await release_postgres_scheduler_owner(owner)
    if row is None:
        return None
    if row.get("released"):
        return {**row, "redis_lease": await release_redis_scheduler_owner(owner)}
    return row
