import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import * as Tabs from "@radix-ui/react-tabs";
import {
  Activity,
  Archive,
  CheckCircle2,
  Boxes,
  Clipboard,
  Database,
  Download,
  Gauge,
  HardDrive,
  KeyRound,
  ListChecks,
  LogOut,
  PauseCircle,
  PlayCircle,
  RefreshCw,
  Rocket,
  RotateCcw,
  ScrollText,
  ShieldCheck,
  TerminalSquare,
  Trash2,
  Upload,
  Workflow
} from "lucide-react";
import "./styles.css";

type RuntimeMap = Record<string, string>;

type SchedulerLease = {
  owner?: string;
  lease_expires_at?: string;
  acquired?: boolean;
};

type RuntimeState = {
  runtime: string;
  status: string;
  stage: string;
  active_model?: string | null;
  model_alias?: string | null;
  resolved_model_version?: string | null;
  job_id?: string | null;
  updated_at?: string;
};

type AdmissionPolicyValues = {
  max_queued_jobs_per_owner: number;
  max_active_jobs_per_owner: number;
  max_jobs_per_hour_per_owner: number;
  max_queued_jobs_global: number;
  artifact_storage_max_bytes: number;
  artifact_storage_reserve_bytes: number;
};

type AdminStatus = {
  service: string;
  resource_policy: Record<string, number>;
  resource_policy_source?: string;
  admission?: AdmissionReport;
  maintenance?: MaintenanceState;
  external_providers_enabled: boolean;
  gpu_default_idle_timeout_seconds?: number;
  runtimes: RuntimeMap;
  scheduler_lease?: SchedulerLease | null;
  runtime_states?: RuntimeState[];
  queue?: { state: string; count: number }[];
};

type AdmissionReport = {
  policy: AdmissionPolicyValues;
  policy_source?: string;
  bounds?: Record<keyof AdmissionPolicyValues, { minimum: number; maximum: number }>;
  queue?: {
    owner_id: string;
    owner_queued_jobs: number;
    owner_active_jobs: number;
    owner_jobs_last_hour: number;
    global_queued_jobs: number;
  } | null;
  storage: {
    root: string;
    exists: boolean;
    artifact_bytes?: number | null;
    disk_total_bytes?: number | null;
    disk_used_bytes?: number | null;
    disk_free_bytes?: number | null;
    artifact_storage_max_bytes: number;
    artifact_storage_reserve_bytes: number;
  };
};

type MetricSummary = {
  count: number;
  min?: number | null;
  avg?: number | null;
  p50?: number | null;
  p95?: number | null;
  max?: number | null;
};

type AdminMetrics = {
  generated_at: string;
  queue: {
    depth_total: number;
    active_total: number;
    by_state: Record<string, number>;
    by_priority: Record<string, number>;
    oldest_wait_seconds?: number | null;
    wait_seconds: MetricSummary;
  };
  jobs: {
    sample_size: number;
    recent_sample_size: number;
    completed_last_hour: number;
    failed_last_hour: number;
    cancelled_last_hour: number;
    recovery_required_last_hour: number;
    load_seconds: MetricSummary;
    run_seconds: MetricSummary;
    peak_vram_mib: MetricSummary;
    peak_ram_mib: MetricSummary;
  };
  model_switches_per_hour: {
    last_hour: number;
    sampled_started_jobs: number;
  };
  runtimes: {
    total: number;
    by_status: Record<string, number>;
    active: RuntimeState[];
  };
  runtime_agent: {
    available: boolean;
    error?: string | null;
  };
  gpu: {
    available: boolean;
    error?: string | null;
    device_count: number;
    memory_total_mib?: number | null;
    memory_used_mib?: number | null;
    memory_free_mib?: number | null;
    utilization_gpu_percent_avg?: number | null;
    utilization_gpu_percent_max?: number | null;
    temperature_c_max?: number | null;
    power_watts_total?: number | null;
  };
  host: {
    available: boolean;
    cpu?: Record<string, number | boolean | null>;
    memory?: {
      available?: boolean;
      total_bytes?: number;
      used_bytes?: number;
      available_bytes?: number;
      swap_used_bytes?: number;
      swap_total_bytes?: number;
    };
    storage?: {
      total_bytes?: number | null;
      used_bytes?: number | null;
      free_bytes?: number | null;
    };
  };
};

type MaintenanceState = {
  id: string;
  source: string;
  enabled: boolean;
  reason: string;
  started_at?: string | null;
  ended_at?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
};

type ResourcePolicyValues = {
  gpu_total_vram_gib: number;
  gpu_usable_vram_gib: number;
  gpu_reserve_vram_gib: number;
  gpu_max_active_pipelines: number;
  host_total_ram_gib: number;
  host_reserve_ram_gib: number;
  llm_default_context: number;
  llm_maximum_context: number;
  llm_default_parallel_requests: number;
  comfyui_maximum_parallel_jobs: number;
  comfyui_maximum_batch_size: number;
};

type ResourcePolicyPayload = {
  source: string;
  effective: ResourcePolicyValues;
  default: ResourcePolicyValues;
  bounds: Record<keyof ResourcePolicyValues, { minimum: number; maximum: number }>;
};

type AdmissionPolicyPayload = {
  source: string;
  effective: AdmissionPolicyValues;
  default: AdmissionPolicyValues;
  bounds: Record<keyof AdmissionPolicyValues, { minimum: number; maximum: number }>;
};

type NetworkPolicyValues = {
  cors_allow_origins: string[];
  trusted_proxy_cidrs: string[];
};

type NetworkPolicyPayload = {
  id: string;
  source: string;
  effective: NetworkPolicyValues;
  environment: NetworkPolicyValues;
  updated_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type BackupSummary = {
  name: string;
  created_at?: string;
  archive?: { size_bytes?: number; sha256?: string };
  file_count?: number;
  contains_sensitive_data?: boolean;
  postgres_dump_included?: boolean;
  postgres_dumps?: { kind?: string; format?: string; path?: string }[];
  postgres_native_dump?: { kind?: string; format?: string; path?: string } | null;
  archive_encryption?: { mode?: string; scheme?: string; file?: string } | null;
  status?: string;
};

type BackupManifestFile = {
  path: string;
  size_bytes: number;
  sha256?: string;
  sensitive?: boolean;
};

type BackupPostgresDump = {
  kind?: string;
  format?: string;
  path?: string;
  tool?: string;
  verified?: boolean;
  sensitive?: boolean;
};

type BackupManifest = {
  format: string;
  name: string;
  created_at?: string;
  files: BackupManifestFile[];
  archive?: { file?: string; size_bytes?: number; sha256?: string };
  contains_sensitive_data?: boolean;
  postgres_dump_included?: boolean;
  postgres_dump?: BackupPostgresDump | null;
  postgres_dumps?: BackupPostgresDump[];
  postgres_native_dump?: BackupPostgresDump | null;
  archive_encryption?: { mode?: string; scheme?: string; file?: string; size_bytes?: number; sha256?: string; key_id?: string } | null;
  preserve_old_stack?: boolean;
};

type BackupRetentionPlan = {
  status: string;
  policy: { keep_last: number; delete_older_than_days?: number | null; cutoff?: string | null };
  candidate_count: number;
  kept_count: number;
  invalid_preserved_count: number;
  total_reclaimable_bytes: number;
  candidates: { name: string; created_at?: string; total_size_bytes?: number; reason: string }[];
  kept: { name: string; reason: string }[];
  deleted?: { name: string; total_size_bytes?: number }[];
  deleted_count?: number;
};

type ArtifactRetentionPlan = {
  status: string;
  policy: { delete_older_than_days: number; cutoff?: string | null; namespaces?: string[]; limit?: number };
  candidate_count: number;
  kept_count: number;
  invalid_preserved_count: number;
  total_reclaimable_bytes: number;
  candidates: { job_id?: string; path: string; namespace?: string; size_bytes?: number; reason: string }[];
  kept: { job_id?: string; path?: string; reason: string }[];
  invalid_preserved: { job_id?: string; path?: string; reason: string }[];
  deleted?: { job_id?: string; path: string; size_bytes?: number; status?: string }[];
  deleted_count?: number;
  job_update_count?: number;
};

type ModelQuarantineRetentionPlan = {
  status: string;
  policy: { delete_older_than_days: number; cutoff?: string | null; limit?: number };
  root: string;
  candidate_count: number;
  kept_count: number;
  invalid_preserved_count: number;
  total_reclaimable_bytes: number;
  truncated?: boolean;
  candidates: { model_id?: string; version?: string; quarantine_set?: string; path: string; size_bytes?: number; reason: string }[];
  kept: { model_id?: string; version?: string; quarantine_set?: string; path: string; reason: string }[];
  invalid_preserved: { path: string; reason: string }[];
  deleted?: { model_id?: string; version?: string; quarantine_set?: string; path: string; size_bytes?: number; status?: string }[];
  deleted_count?: number;
};

type BackupSchedule = {
  source: string;
  enabled: boolean;
  interval_hours: number;
  keep_last: number;
  delete_older_than_days?: number | null;
  label_prefix: string;
  next_run_at?: string | null;
  last_started_at?: string | null;
  last_completed_at?: string | null;
  last_status?: string;
  last_backup_name?: string | null;
  failure_message?: string | null;
};

type UpdatePlan = {
  id: string;
  target_version: string;
  source_url: string;
  status: string;
  stage: string;
  image_refs: { service: string; image: string }[];
  image_stage?: { service?: string; image?: string; status?: string; action?: string; message?: string; error?: string }[];
  compose_override?: { path?: string; ready_for_promotion?: boolean; requires_image_pull_before_promotion?: boolean; not_pulled_services?: string[] };
  backup_name?: string | null;
  self_test?: { status?: string; checks?: unknown[] };
  promotion_result?: {
    status?: string;
    promotion_command?: { shell?: string; argv?: string[] };
    compose_override?: { sha256?: string; services?: string[] };
    error?: string;
  };
  rollback_result?: Record<string, unknown>;
  notes?: string;
  failure_message?: string | null;
  created_at?: string;
  updated_at?: string;
};

type SelfTestCheck = {
  name: string;
  status: string;
  detail: string;
  data?: Record<string, unknown>;
};

type SelfTestResult = {
  status: string;
  checks: SelfTestCheck[];
};

type AcceptanceReportSummary = {
  id: string;
  label?: string;
  generated_at?: string;
  created_by?: string;
  status: string;
  runtime_deployment_mode?: string;
  operator_handoff_ready: boolean;
  operator_evidence_ready?: boolean;
  cutover_preservation_ready?: boolean;
  smoke_evidence_ready?: boolean;
  gpu_evidence_ready?: boolean;
  localai_evidence_ready?: boolean;
  installed_workflows_evidence_ready?: boolean;
  native_comfyui_evidence_ready?: boolean;
  remote_nodes_evidence_ready?: boolean;
  modelhub_evidence_ready?: boolean;
  voicebox_evidence_ready?: boolean;
  security_evidence_ready?: boolean;
  restart_reconciliation_evidence_ready?: boolean;
  backup_migration_rollback_evidence_ready?: boolean;
  live_evidence_ready?: boolean;
  acceptance_blockers: string[];
  files?: {
    directory?: string;
    json?: string;
    markdown?: string;
    sha256sums?: string;
  };
};

type AcceptanceReportDetail = {
  summary: AcceptanceReportSummary;
  report: Record<string, unknown>;
};

type AcceptanceReportFileName = "report.json" | "report.md" | "SHA256SUMS";

type RollbackRehearsalStatus = {
  format: string;
  backup_root: string;
  cutover_plan: {
    available: boolean;
    path?: string;
    name?: string;
    sha256?: string;
    resource_count?: number;
    rollback_command_count?: number;
    rollback_operator_action_count?: number;
    reason?: string;
  };
  report: {
    available: boolean;
    path?: string;
    status?: string;
    generated_at?: string;
    cutover_plan?: string;
    cutover_plan_name?: string;
    cutover_plan_sha256?: string;
    rehearsed_by?: string;
    resource_count?: number;
    reason?: string;
  };
};

type RollbackRehearsalCreateResult = {
  status: string;
  output: string;
  report: {
    generated_at?: string;
    cutover_plan_name?: string;
    cutover_plan_sha256?: string;
    rehearsed_by?: string;
    checks?: Record<string, unknown>;
  };
};

type AcceptanceEvidenceDetail = {
  key: string;
  label: string;
  passed: boolean;
  note: string;
};

type LiveEvidenceDetail = {
  key: string;
  label: string;
  available: boolean;
  status: string;
  sourcePath: string;
  missingChecks: string[];
  samples: string[];
};

type PreservedResourceDetail = {
  key: string;
  label: string;
  items: string[];
};

const ACCEPTANCE_EVIDENCE_ITEMS = [
  ["live_stack_smoke", "Live stack smoke"],
  ["rtx3060_acceptance", "RTX 3060 acceptance"],
  ["chat_tts_image_video", "Chat/TTS/image/video"],
  ["native_comfyui_compatibility", "Native ComfyUI API"],
  ["remote_nodes_non_comfy", "Remote nodes non-Comfy"],
  ["modelhub_sync", "Model Hub sync"],
  ["voicebox_remote", "Voicebox remote"],
  ["backup_verified", "Backups verified"],
  ["restore_rehearsed", "Restore rehearsal"],
  ["migration_rehearsed", "Migration rehearsal"],
  ["restart_reconciliation", "Restart reconciliation"],
  ["rollback_rehearsed", "Rollback rehearsal"],
  ["security_review", "Security review"]
] as const;

const ACCEPTANCE_LIVE_EVIDENCE_SECTIONS = [
  ["live_stack_smoke", "Live stack smoke"],
  ["gpu_acceptance", "RTX 3060 GPU"],
  ["localai_runtime", "LocalAI runtime"],
  ["installed_workflows", "Installed workflows"],
  ["native_comfyui_compatibility", "Native ComfyUI"],
  ["remote_nodes_non_comfy", "Remote nodes"],
  ["modelhub_client_sync", "Model Hub sync"],
  ["voicebox_remote", "Voicebox remote"],
  ["security_acceptance", "Security acceptance"],
  ["restart_reconciliation", "Restart reconciliation"]
] as const;

const ACCEPTANCE_PRESERVED_RESOURCE_SECTIONS = [
  ["containers_to_restart_for_rollback", "Rollback containers"],
  ["containers_to_stop_during_cutover", "Cutover stop list"],
  ["docker_volumes_preserved", "Docker volumes"],
  ["host_paths_preserved", "Host paths"]
] as const;

type AcceptanceEvidenceKey = typeof ACCEPTANCE_EVIDENCE_ITEMS[number][0];
type AcceptanceEvidenceState = Record<AcceptanceEvidenceKey, boolean>;
const EMPTY_ACCEPTANCE_EVIDENCE: AcceptanceEvidenceState = Object.fromEntries(
  ACCEPTANCE_EVIDENCE_ITEMS.map(([key]) => [key, false])
) as AcceptanceEvidenceState;

type AuditEvent = {
  id: string;
  created_at: string;
  actor_id?: string;
  actor_role?: string;
  event_type: string;
  target_type?: string;
  target_id?: string;
  summary: string;
};

type ServiceLogPayload = {
  service: string;
  lines: number;
  entries: string[];
};

type AuthStatus = {
  configured: boolean;
  setup_required: boolean;
  authenticated: boolean;
  subject_id?: string | null;
  role?: string | null;
  scopes: string[];
  csrf_token?: string | null;
  session_id?: string | null;
};

type ApiClient = {
  id: string;
  display_name: string;
  role: string;
  scopes: string[];
  key_prefix: string;
  cidr_allowlist: string[];
  created_at: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
};

type SecretCategory = "remote-provider" | "model-download" | "runtime" | "integration" | "other";

type EncryptedSecret = {
  name: string;
  display_name: string;
  category: SecretCategory;
  description?: string;
  scheme?: string;
  key_id?: string;
  encrypted: boolean;
  created_at?: string;
  updated_at?: string;
  deleted_at?: string | null;
};

type SecretMasterKeyStatus = {
  configured: boolean;
  usable: boolean;
  scheme: string;
  key_id?: string | null;
  error?: string;
};

type ModelHubClient = {
  id: string;
  display_name: string;
  owner_id: string;
  api_client_id: string;
  key_prefix: string;
  allowed_models: string[];
  cidr_allowlist: string[];
  allow_downloads: boolean;
  created_at: string;
  revoked_at?: string | null;
};

type ModelAlias = {
  id: string;
  modality: string;
  status: string;
  enabled?: boolean;
  preferred_runtime: string;
  preferred_runtime_override?: string | null;
  runtimes?: string[];
  idle_timeout_seconds?: number | null;
  visibility_roles?: string[];
  alias_policy_source?: string;
  notes?: string;
  resource_label: string;
  cpu_resident_candidate?: boolean;
  resolved_model?: { id: string; version: string; display_name: string } | null;
};

type ModelAliasPolicyForm = {
  enabled: boolean;
  preferred_runtime: string;
  idle_timeout_seconds: string;
  visibility_roles: string[];
  notes: string;
};

type ModelRecord = {
  id: string;
  version: string;
  display_name: string;
  modality: string;
  preferred_runtime: string;
  status: string;
  resource_label: string;
  updated_at?: string;
  runtime_views?: { runtime: string; host_path: string; container_path: string }[];
};

type CatalogModel = {
  id: string;
  version: string;
  display_name: string;
  modality: string;
  status: string;
  preferred_runtime: string;
  aliases?: string[];
  operations?: string[];
  resource_label: string;
  downloadable?: boolean;
  execution_modes?: string[];
  license?: { name: string; redistribution: string };
};

type RuntimeAdapterStatus = {
  name: string;
  status: string;
  requires_gpu: boolean;
  openai_compatible?: boolean;
  native_api?: boolean;
  external?: boolean;
  error?: string;
  details?: unknown;
  capabilities?: RuntimeAdapterCapabilities;
  adapter_contract?: RuntimeAdapterContract;
  runtime_state?: RuntimeState | null;
};

type RuntimeAdapterCapabilities = {
  modalities?: string[];
  operations?: string[];
  requires_gpu?: boolean;
  external?: boolean;
  native_api?: boolean;
  openai_compatible?: boolean;
  configured?: boolean;
};

type RuntimeAdapterContract = {
  version: string;
  surfaces?: Record<string, string>;
  methods?: Record<string, string>;
};

type JobArtifact = {
  url?: string;
  mime_type?: string;
  bytes?: number;
  sha256?: string;
};

type AccessSnippet = {
  id: string;
  label: string;
  code: string;
};

type ExternalRuntimeConfig = {
  runtime: string;
  source: string;
  enabled: boolean;
  configured: boolean;
  eligible: boolean;
  status: string;
  base_url: string;
  api_key_secret_name?: string | null;
  api_key_configured: boolean;
  external_data_acknowledged: boolean;
  configuration_error?: string | null;
  notes: string;
  warning: string;
  updated_at?: string | null;
};

type ExternalRuntimeConfigForm = {
  enabled: boolean;
  base_url: string;
  api_key_secret_name: string;
  confirm_external_data: boolean;
  notes: string;
};

const detailRecord = (value: unknown): Record<string, unknown> => {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
};

const booleanLabel = (value: unknown): string => {
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  return String(value);
};

const statusPillClass = (status: string): string => {
  if (["ok", "ready", "healthy", "eligible", "completed", "configured"].includes(status)) return "status-pill ok";
  if (["invalid", "failed", "error"].some((item) => status.includes(item))) return "status-pill invalid";
  if (["warning", "disabled", "pending", "degraded"].some((item) => status.includes(item))) return "status-pill warning";
  return "status-pill";
};

const capabilitySummary = (value: unknown): string => {
  const capabilities = detailRecord(value);
  return Object.entries(capabilities)
    .map(([name, enabled]) => `${name}: ${booleanLabel(enabled)}`)
    .join(", ");
};

const runtimeCapabilitySummary = (capabilities?: RuntimeAdapterCapabilities): string => {
  if (!capabilities) return "";
  const modalities = capabilities.modalities?.length ? capabilities.modalities.join(", ") : "none";
  const operations = capabilities.operations?.length ? capabilities.operations.join(", ") : "manifest-defined";
  return `${modalities} / ${operations}`;
};

const capabilityListLabel = (values?: string[]): string => {
  return values?.length ? values.join(", ") : "none";
};

const runtimeCapabilityFlags = (capabilities: RuntimeAdapterCapabilities): string => {
  const flags = [
    capabilities.configured === false ? "unconfigured" : "configured",
    capabilities.requires_gpu ? "GPU lease" : "CPU/no GPU lease",
    capabilities.openai_compatible ? "OpenAI API" : "",
    capabilities.native_api ? "native API" : "",
    capabilities.external ? "external data" : "local LAN"
  ].filter(Boolean);
  return flags.join(" / ");
};

const contractMethodSummary = (methods?: Record<string, string>): string => {
  if (!methods) return "";
  return Object.entries(methods)
    .slice(0, 8)
    .map(([name, status]) => `${name.replaceAll("_", " ")}: ${status}`)
    .join(" / ");
};

type VoiceProfile = {
  id: string;
  display_name: string;
  owner_id: string;
  runtime: "voicebox" | "audio-cpu";
  engine: string;
  model_alias: string;
  profile_type: "preset" | "reference" | "clone";
  status: string;
  visibility_roles: string[];
  metadata: Record<string, unknown>;
  sample_artifacts: { url: string; sha256: string; mime_type: string; bytes: number }[];
  created_at?: string;
  updated_at?: string;
  deleted_at?: string | null;
};

type ModelInstallPlan = {
  model_ref: string;
  status: string;
  can_install: boolean;
  blockers: string[];
  requires_license_acceptance: boolean;
  total_size_bytes: number;
  resource_decision: { label: string; reason: string };
  model: { display_name: string; source: { url: string; revision: string }; license: { name: string; redistribution: string } };
  files: { path: string; status: string; size_bytes: number }[];
  archive_inspections: {
    path: string;
    archive_format: string;
    inspection_status: string;
    reason?: string;
    error?: string;
    summary?: { member_count: number; file_count: number; total_uncompressed_bytes: number };
  }[];
  runtime_views: { runtime: string; host_path: string; container_path: string }[];
};

type ModelDownloadPlan = {
  model_ref: string;
  status: string;
  can_download: boolean;
  already_available: boolean;
  blockers: string[];
  requires_license_acceptance: boolean;
  license_accepted: boolean;
  model: { display_name: string; source: { url: string; revision: string }; license: { name: string; redistribution: string } };
  source_url: string;
  target_sha256: string;
  target_size_bytes: number;
  existing_partial_bytes: number;
  file_count: number;
  files: { path: string; source_url: string; status?: string; target_size_bytes: number; existing_partial_bytes: number; already_available: boolean; blockers: string[] }[];
};

type ModelDownloadRecord = {
  id: string;
  model_id: string;
  model_version: string;
  status: string;
  stage: string;
  credential_secret_name?: string | null;
  authenticated?: boolean;
  target_size_bytes: number;
  bytes_downloaded: number;
  progress_percent: number;
  file_count: number;
  error_category?: string | null;
  error_message?: string | null;
};

type ModelBlobQuarantinePlan = {
  model_ref: string;
  model_status: string;
  status: string;
  can_quarantine: boolean;
  blockers: string[];
  total_size_bytes: number;
  active_jobs?: unknown[];
  dependent_workflows?: unknown[];
  moved?: { path: string; sha256: string; quarantine_path: string; size_bytes: number }[];
  blobs: {
    path: string;
    sha256: string;
    blob_path?: string;
    quarantine_path?: string;
    status: string;
    size_bytes: number;
    can_quarantine: boolean;
    referenced_by?: string[];
    blockers?: string[];
  }[];
};

type JobRecord = {
  id: string;
  correlation_id: string;
  owner_id: string;
  modality: string;
  operation: string;
  model_alias: string;
  resolved_model_version: string;
  runtime: string;
  priority: string;
  state: string;
  stage?: string;
  progress?: number;
  retry_count?: number;
  native_prompt_id?: string | null;
  failure_category?: string | null;
  failure_message?: string | null;
  artifacts?: JobArtifact[];
  redacted_request?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  completed_at?: string | null;
  load_time_ms?: number | null;
  run_time_ms?: number | null;
  peak_vram_mib?: number | null;
  peak_ram_mib?: number | null;
};

type RuntimeReservationRecord = {
  id: string;
  owner_id: string;
  runtime: string;
  model_alias: string;
  resolved_model_version: string;
  duration_seconds: number;
  reason?: string;
  status: string;
  created_at?: string;
  updated_at?: string;
  expires_at?: string;
  cancelled_at?: string | null;
};

type WorkflowDependency = {
  type: string;
  id: string;
  version?: string;
  ready?: boolean;
  status?: string;
  reason?: string;
};

type PublishedWorkflow = {
  id: string;
  version: string;
  display_name: string;
  description?: string;
  modality: string;
  operation: string;
  model_alias: string;
  backend_policy: string;
  runtime_policy: string;
  output_mime_types: string[];
  resource_class: string;
  dependencies: WorkflowDependency[];
  visibility_roles: string[];
  limits: Record<string, number>;
  status: string;
  publishable: boolean;
  dependency_status?: {
    ready?: boolean;
    dependencies?: WorkflowDependency[];
  };
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  workflow_json: Record<string, unknown>;
};

const API_BASE = import.meta.env.VITE_B1_API_BASE ?? "https://api.ai.b1.germering";
const CSRF_STORAGE_KEY = "b1_ai_hub_csrf";

function apiUrl(path: string): string {
  return path.startsWith("http://") || path.startsWith("https://") ? path : `${API_BASE}${path}`;
}

function accessSnippets(): AccessSnippet[] {
  const unifiedBase = `${API_BASE}/v1`;
  return [
    {
      id: "curl",
      label: "curl",
      code: `curl -s ${unifiedBase}/chat/completions \\
  -H "Authorization: Bearer <B1_API_KEY>" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"chat-default","messages":[{"role":"user","content":"ping"}]}'`
    },
    {
      id: "open-webui",
      label: "Open WebUI",
      code: `OpenAI API base URL: ${unifiedBase}
OpenAI API key: <B1_API_KEY>
Default model: chat-default`
    },
    {
      id: "external-comfyui",
      label: "External ComfyUI",
      code: `export B1_API_BASE=${API_BASE}
export B1_API_KEY=<B1_API_KEY>
python -m pip install ./integrations/comfyui-b1-remote-nodes`
    },
    {
      id: "modelhub-sync",
      label: "Model Hub Sync",
      code: `python -m pip install ./integrations/b1-model-client
export B1_MODELHUB_URL=https://models.ai.b1.germering
export B1_MODELHUB_TOKEN=<B1_MODELHUB_KEY>
b1-model-client plan --cache ~/.cache/b1-ai-hub/models chat-default
b1-model-client sync --cache ~/.cache/b1-ai-hub/models chat-default`
    },
    {
      id: "voicebox",
      label: "Voicebox",
      code: `Server URL: https://voice.ai.b1.germering
Authorization: Bearer <B1_API_KEY>
Speech endpoint: ${unifiedBase}/audio/speech`
    },
    {
      id: "python",
      label: "Python",
      code: `import requests

response = requests.post(
    "${unifiedBase}/responses",
    headers={"Authorization": "Bearer <B1_API_KEY>"},
    json={"model": "chat-default", "input": "ping"},
    timeout=60,
)
print(response.json())`
    },
    {
      id: "javascript",
      label: "JavaScript",
      code: `const response = await fetch("${unifiedBase}/responses", {
  method: "POST",
  headers: {
    "Authorization": "Bearer <B1_API_KEY>",
    "Content-Type": "application/json"
  },
  body: JSON.stringify({ model: "chat-default", input: "ping" })
});
console.log(await response.json());`
    }
  ];
}

function storeAuthStatus(status: AuthStatus | null): void {
  if (status?.csrf_token) {
    window.sessionStorage.setItem(CSRF_STORAGE_KEY, status.csrf_token);
  } else {
    window.sessionStorage.removeItem(CSRF_STORAGE_KEY);
  }
}

function errorMessageFromBody(parsed: any, response: Response): string {
  const detail = parsed?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  if (Array.isArray(detail)) return detail.map((item) => item.msg ?? item.message ?? String(item)).join("; ");
  return `${response.status} ${response.statusText}`;
}

function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method) && !headers.has("X-B1-CSRF")) {
    const csrf = window.sessionStorage.getItem(CSRF_STORAGE_KEY);
    if (csrf) headers.set("X-B1-CSRF", csrf);
  }
  return fetch(apiUrl(path), { ...init, credentials: "include", headers });
}

async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init);
  const body = await response.text();
  const parsed = body ? JSON.parse(body) : null;
  if (!response.ok) {
    throw new Error(errorMessageFromBody(parsed, response));
  }
  return parsed as T;
}

function artifactFilename(artifact: JobArtifact, index: number): string {
  const fallback = `job-artifact-${index + 1}`;
  const raw = artifact.url?.split("?", 1)[0] ?? "";
  const last = raw.split("/").filter(Boolean).pop() ?? fallback;
  try {
    return decodeURIComponent(last).replace(/[\\/:*?"<>|]/g, "_").slice(0, 160) || fallback;
  } catch {
    return fallback;
  }
}

async function downloadJobArtifact(artifact: JobArtifact, index: number): Promise<string> {
  if (!artifact.url) throw new Error("artifact URL is missing");
  const response = await apiFetch(artifact.url, { method: "GET" });
  if (!response.ok) {
    const text = await response.text();
    let parsed: any = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = null;
    }
    throw new Error(errorMessageFromBody(parsed, response));
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const filename = artifactFilename(artifact, index);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  return filename;
}

async function downloadAcceptanceReportFile(reportId: string, filename: AcceptanceReportFileName): Promise<string> {
  const response = await apiFetch(`/admin/acceptance-reports/${encodeURIComponent(reportId)}/files/${encodeURIComponent(filename)}`, { method: "GET" });
  if (!response.ok) {
    const text = await response.text();
    let parsed: any = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = null;
    }
    throw new Error(errorMessageFromBody(parsed, response));
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const downloadName = `${reportId}-${filename}`;
  const link = document.createElement("a");
  link.href = url;
  link.download = downloadName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  return downloadName;
}

function parseSseEvent(raw: string): { event: string; data: string } | null {
  let event = "message";
  const data: string[] = [];
  raw.split("\n").forEach((line) => {
    if (!line || line.startsWith(":")) return;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  });
  return data.length ? { event, data: data.join("\n") } : null;
}

async function streamAdminJobEvents(
  jobId: string,
  signal: AbortSignal,
  onJob: (job: JobRecord) => void
): Promise<void> {
  const response = await apiFetch(`/admin/jobs/${encodeURIComponent(jobId)}/events`, {
    headers: { Accept: "text/event-stream" },
    signal
  });
  if (!response.ok || !response.body) {
    const text = await response.text();
    let parsed: any = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = null;
    }
    throw new Error(errorMessageFromBody(parsed, response));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const chunks = buffer.replace(/\r\n/g, "\n").split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const parsed = parseSseEvent(chunk);
      if (!parsed) continue;
      if (parsed.event === "job") {
        onJob(JSON.parse(parsed.data) as JobRecord);
      } else if (parsed.event === "error" || parsed.event === "timeout") {
        const payload = JSON.parse(parsed.data);
        throw new Error(payload.error ?? parsed.event);
      }
    }
    if (done) break;
  }
}

function formatCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "unavailable";
}

function formatSeconds(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "none";
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  return `${Math.round(value / 3600)}h`;
}

function formatGibFromMib(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "unavailable";
  return `${(value / 1024).toFixed(1)} GiB`;
}

function formatHostBytes(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "unavailable";
  const gib = value / (1024 ** 3);
  if (gib >= 1) return `${gib.toFixed(1)} GiB`;
  const mib = value / (1024 ** 2);
  return `${mib.toFixed(0)} MiB`;
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function SectionTitle({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="section-title">
      {icon}
      <h2>{title}</h2>
    </div>
  );
}

const RESOURCE_POLICY_FIELDS: { key: keyof ResourcePolicyValues; label: string; step: string }[] = [
  { key: "gpu_total_vram_gib", label: "GPU total", step: "0.1" },
  { key: "gpu_usable_vram_gib", label: "GPU usable", step: "0.1" },
  { key: "gpu_reserve_vram_gib", label: "GPU reserve", step: "0.1" },
  { key: "gpu_max_active_pipelines", label: "GPU pipelines", step: "1" },
  { key: "host_total_ram_gib", label: "RAM total", step: "0.5" },
  { key: "host_reserve_ram_gib", label: "RAM reserve", step: "0.5" },
  { key: "llm_default_context", label: "LLM context", step: "512" },
  { key: "llm_maximum_context", label: "LLM max context", step: "512" },
  { key: "llm_default_parallel_requests", label: "LLM parallel", step: "1" },
  { key: "comfyui_maximum_parallel_jobs", label: "Comfy jobs", step: "1" },
  { key: "comfyui_maximum_batch_size", label: "Comfy batch", step: "1" }
];

const INTEGER_POLICY_FIELDS = new Set<keyof ResourcePolicyValues>([
  "gpu_max_active_pipelines",
  "llm_default_context",
  "llm_maximum_context",
  "llm_default_parallel_requests",
  "comfyui_maximum_parallel_jobs",
  "comfyui_maximum_batch_size"
]);

const ADMISSION_POLICY_FIELDS: { key: keyof AdmissionPolicyValues; label: string; step: string; formatter?: (value: number) => string }[] = [
  { key: "max_queued_jobs_per_owner", label: "Owner queued", step: "1" },
  { key: "max_active_jobs_per_owner", label: "Owner active", step: "1" },
  { key: "max_jobs_per_hour_per_owner", label: "Jobs/hour", step: "1" },
  { key: "max_queued_jobs_global", label: "Global queued", step: "1" },
  { key: "artifact_storage_max_bytes", label: "Artifact cap", step: "1048576", formatter: formatHostBytes },
  { key: "artifact_storage_reserve_bytes", label: "Disk reserve", step: "1048576", formatter: formatHostBytes }
];

const UPDATE_IMAGE_REFS_TEMPLATE = JSON.stringify(
  [
    {
      service: "control-plane",
      image: "ghcr.io/b1/b1-ai-hub-control-plane:0.2.0@sha256:0000000000000000000000000000000000000000000000000000000000000000"
    }
  ],
  null,
  2
);

const SERVICE_LOG_OPTIONS = ["control-plane", "localai", "comfyui", "voicebox", "audio-cpu", "artifact-server", "open-webui", "gateway"];
const RUNTIME_OPTIONS = ["localai", "comfyui", "voicebox", "audio-cpu", "openai-compatible", "generic-http"];
const ROLE_OPTIONS = ["admin", "operator", "creator", "user", "service"];

function Dashboard({ status, metrics }: { status: AdminStatus | null; metrics: AdminMetrics | null }) {
  const policy = status?.resource_policy ?? {};
  const lease = status?.scheduler_lease;
  const leaseOwner = lease?.owner ?? "idle";
  const leaseDetail = lease?.lease_expires_at ? `expires ${new Date(lease.lease_expires_at).toLocaleTimeString()}` : "one pipeline policy active";
  const queueByState = metrics?.queue.by_state ?? Object.fromEntries((status?.queue ?? []).map((item) => [item.state, item.count]));
  const queuedTotal = metrics?.queue.depth_total ?? ((queueByState.created ?? 0) + (queueByState.validated ?? 0) + (queueByState.queued ?? 0) + (queueByState.waiting_for_gpu ?? 0));
  const activeTotal = metrics?.queue.active_total ?? ((queueByState.unloading ?? 0) + (queueByState.verifying_vram ?? 0) + (queueByState.loading ?? 0) + (queueByState.warming ?? 0) + (queueByState.running ?? 0) + (queueByState.saving ?? 0) + (queueByState.cancelling ?? 0));
  const runningTotal = queueByState.running ?? 0;
  const recoveryTotal = (queueByState.recovery_required ?? 0) + (queueByState.cancelling ?? 0);
  const gpu = metrics?.gpu;
  const hostMemory = metrics?.host.memory;
  const admission = status?.admission;
  const runtimeStateByName = Object.fromEntries((status?.runtime_states ?? []).map((runtime) => [runtime.runtime, runtime]));
  const gpuDetail = gpu?.available
    ? `${formatCount(gpu.utilization_gpu_percent_max)}% util, ${formatCount(gpu.temperature_c_max)}C max`
    : (gpu?.error ?? metrics?.runtime_agent.error ?? "runtime-agent metrics unavailable");
  const hostMemoryDetail = hostMemory?.available
    ? `${formatHostBytes(hostMemory.available_bytes)} available, ${formatHostBytes(hostMemory.swap_used_bytes)} swap used`
    : "runtime-agent metrics unavailable";
  return (
    <div className="panel-grid">
      <section className="panel wide">
        <SectionTitle icon={<Activity size={18} />} title="Dashboard" />
        <div className="metrics">
          <Metric label="GPU lease" value={leaseOwner} detail={leaseDetail} />
          <Metric label="GPU memory" value={gpu?.available ? `${formatGibFromMib(gpu.memory_used_mib)} / ${formatGibFromMib(gpu.memory_total_mib)}` : "unavailable"} detail={gpuDetail} />
          <Metric label="Queue depth" value={formatCount(queuedTotal)} detail={`${formatCount(activeTotal)} active, oldest wait ${formatSeconds(metrics?.queue.oldest_wait_seconds)}`} />
          <Metric label="Jobs last hour" value={`${formatCount(metrics?.jobs.completed_last_hour)} done`} detail={`${formatCount(metrics?.jobs.failed_last_hour)} failed, ${formatCount(metrics?.jobs.recovery_required_last_hour)} recovery, ${formatCount(metrics?.jobs.cancelled_last_hour)} cancelled`} />
          <Metric label="Model switches" value={formatCount(metrics?.model_switches_per_hour.last_hour)} detail={`${formatCount(metrics?.model_switches_per_hour.sampled_started_jobs)} started jobs sampled`} />
          <Metric label="Peak VRAM" value={formatGibFromMib(metrics?.jobs.peak_vram_mib.max)} detail={`peak RAM ${formatGibFromMib(metrics?.jobs.peak_ram_mib.max)}`} />
          <Metric label="Host memory" value={hostMemory?.available ? `${formatHostBytes(hostMemory.used_bytes)} used` : "unavailable"} detail={hostMemoryDetail} />
          <Metric label="VRAM usable" value={`${policy.gpu_usable_vram_gib ?? 10.5} GiB`} detail={`${policy.gpu_reserve_vram_gib ?? 1.5} GiB reserved`} />
          <Metric label="Admission" value={`${formatCount(admission?.queue?.owner_queued_jobs)} queued`} detail={`${formatCount(admission?.queue?.owner_jobs_last_hour)} jobs/hour, ${formatCount(admission?.queue?.global_queued_jobs)} global`} />
          <Metric label="Artifact headroom" value={formatHostBytes(admission?.storage.disk_free_bytes)} detail={`${formatHostBytes(admission?.policy.artifact_storage_reserve_bytes)} reserved`} />
          <Metric label="Idle unload" value={`${status?.gpu_default_idle_timeout_seconds ?? 300}s`} detail="blank alias policy uses this default" />
          <Metric label="External providers" value={status?.external_providers_enabled ? "enabled" : "disabled"} detail="LAN-local default" />
        </div>
      </section>
      <section className="panel">
        <SectionTitle icon={<ListChecks size={18} />} title="Queue" />
        <table>
          <tbody>
            <tr><td>Queued</td><td>{queuedTotal}</td></tr>
            <tr><td>Running</td><td>{runningTotal}</td></tr>
            <tr><td>Recovery</td><td>{recoveryTotal}</td></tr>
            <tr><td>Oldest wait</td><td>{formatSeconds(metrics?.queue.oldest_wait_seconds)}</td></tr>
            <tr><td>Recent run p95</td><td>{formatSeconds(metrics?.jobs.run_seconds.p95)}</td></tr>
          </tbody>
        </table>
      </section>
      <section className="panel">
        <SectionTitle icon={<Database size={18} />} title="Services" />
        <ul className="runtime-list">
          {Object.entries(status?.runtimes ?? {}).map(([name, url]) => {
            const runtimeState = runtimeStateByName[name];
            const stateLabel = runtimeState ? `${runtimeState.status} / ${runtimeState.stage}` : "unreported";
            return <li key={name}><span>{name}</span><code>{url} ({stateLabel})</code></li>;
          })}
        </ul>
      </section>
    </div>
  );
}

function Models() {
  const [aliases, setAliases] = useState<ModelAlias[]>([]);
  const [aliasForms, setAliasForms] = useState<Record<string, ModelAliasPolicyForm>>({});
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [records, setRecords] = useState<ModelRecord[]>([]);
  const [downloads, setDownloads] = useState<ModelDownloadRecord[]>([]);
  const [downloadSecrets, setDownloadSecrets] = useState<EncryptedSecret[]>([]);
  const [downloadCredentialSecretName, setDownloadCredentialSecretName] = useState("");
  const [manifestUrl, setManifestUrl] = useState("");
  const [plan, setPlan] = useState<ModelInstallPlan | null>(null);
  const [downloadPlan, setDownloadPlan] = useState<ModelDownloadPlan | null>(null);
  const [blobPlan, setBlobPlan] = useState<ModelBlobQuarantinePlan | null>(null);
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);

  const modelVersionPath = (record: ModelRecord) => `${encodeURIComponent(record.id)}/versions/${encodeURIComponent(record.version)}`;
  const aliasFormFromAlias = (alias: ModelAlias): ModelAliasPolicyForm => ({
    enabled: alias.enabled ?? alias.status !== "disabled",
    preferred_runtime: alias.preferred_runtime_override ?? "",
    idle_timeout_seconds: alias.idle_timeout_seconds ? String(alias.idle_timeout_seconds) : "",
    visibility_roles: alias.visibility_roles ?? [],
    notes: alias.notes ?? ""
  });

  const loadModels = () => {
    setMessage("loading");
    Promise.all([
      apiFetch(`/admin/models`).then((response) => response.ok ? response.json() : Promise.reject(new Error(`models ${response.status}`))),
      apiFetch(`/admin/models/downloads?limit=20`).then((response) => response.ok ? response.json() : Promise.reject(new Error(`downloads ${response.status}`))),
      apiFetch(`/admin/secrets?category=model-download`).then((response) => {
        if (response.ok) return response.json();
        if (response.status === 403) return { data: [] };
        return Promise.reject(new Error(`download secrets ${response.status}`));
      })
    ])
      .then(([modelPayload, downloadPayload, secretPayload]) => {
        const loadedAliases = modelPayload.aliases ?? [];
        setAliases(loadedAliases);
        setAliasForms(Object.fromEntries(loadedAliases.map((alias: ModelAlias) => [alias.id, aliasFormFromAlias(alias)])));
        setCatalog(modelPayload.catalog ?? []);
        setRecords(modelPayload.records ?? []);
        setDownloads(downloadPayload.data ?? []);
        setDownloadSecrets(secretPayload.data ?? []);
        setMessage("ready");
      })
      .catch((err: Error) => setMessage(err.message));
  };

  useEffect(loadModels, []);

  const updateAliasForm = <K extends keyof ModelAliasPolicyForm>(aliasId: string, key: K, value: ModelAliasPolicyForm[K]) => {
    setAliasForms((current) => ({
      ...current,
      [aliasId]: {
        ...(current[aliasId] ?? { enabled: true, preferred_runtime: "", idle_timeout_seconds: "", visibility_roles: [], notes: "" }),
        [key]: value
      }
    }));
  };

  const toggleAliasRole = (aliasId: string, role: string) => {
    const currentRoles = aliasForms[aliasId]?.visibility_roles ?? [];
    updateAliasForm(
      aliasId,
      "visibility_roles",
      currentRoles.includes(role) ? currentRoles.filter((item) => item !== role) : [...currentRoles, role]
    );
  };

  const saveAliasPolicy = (alias: ModelAlias) => {
    const form = aliasForms[alias.id] ?? aliasFormFromAlias(alias);
    setBusy(true);
    setMessage("saving alias policy");
    apiFetch(`/admin/models/aliases/${encodeURIComponent(alias.id)}/policy`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: form.enabled,
        preferred_runtime: form.preferred_runtime || null,
        idle_timeout_seconds: form.idle_timeout_seconds ? Number(form.idle_timeout_seconds) : null,
        visibility_roles: form.visibility_roles,
        notes: form.notes
      })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setAliases((current) => current.map((item) => item.id === alias.id ? payload.alias : item));
        setAliasForms((current) => ({ ...current, [alias.id]: aliasFormFromAlias(payload.alias) }));
        setMessage(`saved ${alias.id}`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const resetAliasPolicy = (alias: ModelAlias) => {
    setBusy(true);
    setMessage("resetting alias policy");
    apiFetch(`/admin/models/aliases/${encodeURIComponent(alias.id)}/policy`, { method: "DELETE" })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setAliases((current) => current.map((item) => item.id === alias.id ? payload.alias : item));
        setAliasForms((current) => ({ ...current, [alias.id]: aliasFormFromAlias(payload.alias) }));
        setMessage(`reset ${alias.id}`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const modelRequestBody = (model?: string) => {
    if (model) return { model };
    const url = manifestUrl.trim();
    return url ? { manifest_url: url } : null;
  };

  const planInstall = (model?: string) => {
    const body = modelRequestBody(model);
    if (!body) {
      setMessage("manifest URL required");
      return;
    }
    setBusy(true);
    setMessage("planning");
    apiFetch(`/admin/models/install-plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setPlan(payload);
        setMessage(payload.status);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const installModel = (model?: string) => {
    const body = modelRequestBody(model);
    if (!body) {
      setMessage("manifest URL required");
      return;
    }
    setBusy(true);
    setMessage("installing");
    apiFetch(`/admin/models/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, confirm: true, accept_license: Boolean(plan?.requires_license_acceptance) })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setPlan(payload.plan);
        setMessage(`installed ${payload.model.id}@${payload.model.version}`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const planDownload = (model?: string) => {
    const body = modelRequestBody(model);
    if (!body) {
      setMessage("manifest URL required");
      return;
    }
    setBusy(true);
    setMessage("planning download");
    apiFetch(`/admin/models/download-plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setDownloadPlan(payload);
        setMessage(payload.status);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const queueDownload = (model?: string) => {
    const body = modelRequestBody(model);
    if (!body) {
      setMessage("manifest URL required");
      return;
    }
    setBusy(true);
    setMessage("queueing download");
    apiFetch(`/admin/models/downloads`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...body,
        confirm: true,
        accept_license: Boolean(downloadPlan?.requires_license_acceptance),
        credential_secret_name: downloadCredentialSecretName.trim() || null
      })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setDownloadPlan(payload.plan);
        setMessage(`${payload.download.status} ${payload.download.id}`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const cancelDownload = (download: ModelDownloadRecord) => {
    setBusy(true);
    setMessage("cancelling download");
    apiFetch(`/admin/models/downloads/${download.id}`, { method: "DELETE" })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then(() => {
        setMessage(`cancelled ${download.id}`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const retryDownload = (download: ModelDownloadRecord) => {
    setBusy(true);
    setMessage("retrying download");
    apiFetch(`/admin/models/downloads/${download.id}/retry`, { method: "POST" })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload: ModelDownloadRecord) => {
        setMessage(`queued ${payload.id}`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const pauseDownload = (download: ModelDownloadRecord) => {
    setBusy(true);
    setMessage("pausing download");
    apiFetch(`/admin/models/downloads/${download.id}/pause`, { method: "POST" })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload: ModelDownloadRecord) => {
        setMessage(`${payload.status} ${payload.id}`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const resumeDownload = (download: ModelDownloadRecord) => {
    setBusy(true);
    setMessage("resuming download");
    apiFetch(`/admin/models/downloads/${download.id}/resume`, { method: "POST" })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload: ModelDownloadRecord) => {
        setMessage(`queued ${payload.id}`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const quarantineModel = (record: ModelRecord) => {
    setBusy(true);
    setMessage("quarantining");
    apiFetch(`/admin/models/${modelVersionPath(record)}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then(() => {
        setMessage(`quarantined ${record.id}@${record.version}`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const planBlobQuarantine = (record: ModelRecord) => {
    setBusy(true);
    setMessage("planning blob quarantine");
    apiFetch(`/admin/models/${modelVersionPath(record)}/blob-quarantine-plan`)
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setBlobPlan(payload);
        setMessage(`blob quarantine ${payload.status}`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const quarantineBlobs = (record: ModelRecord) => {
    setBusy(true);
    setMessage("quarantining blobs");
    apiFetch(`/admin/models/${modelVersionPath(record)}/blobs/quarantine`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setBlobPlan(payload);
        setMessage(`${payload.moved?.length ?? 0} blob${payload.moved?.length === 1 ? "" : "s"} quarantined`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  return (
    <section className="panel wide">
      <SectionTitle icon={<Boxes size={18} />} title="Models" />
      <div className="toolbar">
        <button title="Refresh catalog" onClick={loadModels} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <label>
          Download token
          {downloadSecrets.length ? (
            <select value={downloadCredentialSecretName} onChange={(event) => setDownloadCredentialSecretName(event.target.value)}>
              <option value="">none</option>
              {downloadSecrets.map((secret) => <option key={secret.name} value={secret.name}>{secret.display_name || secret.name}</option>)}
            </select>
          ) : (
            <input value={downloadCredentialSecretName} onChange={(event) => setDownloadCredentialSecretName(event.target.value)} maxLength={128} placeholder="model-download secret" />
          )}
        </label>
        <label>
          Manifest URL
          <input value={manifestUrl} onChange={(event) => setManifestUrl(event.target.value)} maxLength={2048} placeholder="https://..." />
        </label>
        <button title="Plan remote manifest install" onClick={() => planInstall()} disabled={busy || !manifestUrl.trim()}><ListChecks size={16} /></button>
        <button title="Install remote manifest" onClick={() => installModel()} disabled={busy || !manifestUrl.trim()}><Archive size={16} /></button>
        <button title="Plan remote manifest download" onClick={() => planDownload()} disabled={busy || !manifestUrl.trim()}><Download size={16} /></button>
        <button title="Queue remote manifest download" onClick={() => queueDownload()} disabled={busy || !manifestUrl.trim()}><Download size={16} /></button>
        <span className="toolbar-status">{message}</span>
      </div>
      {plan && (
        <div className="one-time-key">
          <strong>{plan.model_ref} {plan.status}</strong>
          <span>{plan.model.display_name} / {plan.model.license.name} / {formatBytes(plan.total_size_bytes)}</span>
          <small>{plan.resource_decision.label}: {plan.resource_decision.reason}</small>
          <small>{plan.runtime_views.map((view) => `${view.runtime}: ${view.container_path}`).join(" / ")}</small>
          {Boolean(plan.archive_inspections?.length) && <small>{plan.archive_inspections.map(formatArchiveInspection).join(" / ")}</small>}
          {plan.blockers.length > 0 && <small>{plan.blockers.join("; ")}</small>}
        </div>
      )}
      {downloadPlan && (
        <div className="one-time-key">
          <strong>{downloadPlan.model_ref} download {downloadPlan.status}</strong>
          <span>{formatBytes(downloadPlan.existing_partial_bytes)} staged / {formatBytes(downloadPlan.target_size_bytes)} total</span>
          <small>{downloadPlan.model.license.name} / {downloadPlan.model.license.redistribution}{downloadPlan.requires_license_acceptance ? ` / licence ${downloadPlan.license_accepted ? "accepted" : "acceptance required"}` : ""}</small>
          <small>{downloadPlan.file_count} file{downloadPlan.file_count === 1 ? "" : "s"} from {downloadPlan.source_url}</small>
          {downloadPlan.files.length > 1 && <small>{downloadPlan.files.map((file) => file.path).join(" / ")}</small>}
          {downloadPlan.blockers.length > 0 && <small>{downloadPlan.blockers.join("; ")}</small>}
        </div>
      )}
      {blobPlan && (
        <div className="one-time-key">
          <strong>{blobPlan.model_ref} blob quarantine {blobPlan.status}</strong>
          <span>{formatBytes(blobPlan.total_size_bytes)} recoverable cleanup candidate / {blobPlan.model_status}</span>
          <small>{blobPlan.blobs.map((blob) => `${blob.path}: ${blob.status}`).join(" / ")}</small>
          {Boolean(blobPlan.active_jobs?.length) && <small>{blobPlan.active_jobs?.length} active job reference{blobPlan.active_jobs?.length === 1 ? "" : "s"}</small>}
          {Boolean(blobPlan.dependent_workflows?.length) && <small>{blobPlan.dependent_workflows?.length} dependent workflow{blobPlan.dependent_workflows?.length === 1 ? "" : "s"}</small>}
          {Boolean(blobPlan.moved?.length) && <small>{blobPlan.moved?.map((blob) => `${blob.sha256.slice(0, 12)} -> ${blob.quarantine_path}`).join(" / ")}</small>}
          {blobPlan.blockers.length > 0 && <small>{blobPlan.blockers.join("; ")}</small>}
        </div>
      )}
      <table>
        <thead><tr><th>Alias</th><th>Status</th><th>Runtime</th><th>Policy</th><th>Actions</th></tr></thead>
        <tbody>
          {aliases.map((alias) => {
            const form = aliasForms[alias.id] ?? aliasFormFromAlias(alias);
            return (
              <tr key={alias.id}>
                <td><code>{alias.id}</code><small>{alias.resolved_model ? `${alias.resolved_model.id}@${alias.resolved_model.version}` : "no manifest"}</small></td>
                <td>
                  <label className="inline-check">
                    <input type="checkbox" checked={form.enabled} onChange={(event) => updateAliasForm(alias.id, "enabled", event.target.checked)} />
                    <span>{alias.status}</span>
                  </label>
                  <small>{alias.modality}</small>
                </td>
                <td>
                  <select value={form.preferred_runtime} onChange={(event) => updateAliasForm(alias.id, "preferred_runtime", event.target.value)}>
                    <option value="">manifest default</option>
                    {RUNTIME_OPTIONS.map((runtime) => (
                      <option key={runtime} value={runtime}>{runtime}</option>
                    ))}
                  </select>
                  <small>{alias.preferred_runtime}{alias.runtimes?.length ? ` / ${alias.runtimes.join(", ")}` : ""}</small>
                </td>
                <td>
                  <div className="alias-policy-cell">
                    <input
                      className="compact-number"
                      type="number"
                      min="30"
                      max="86400"
                      placeholder="idle seconds"
                      value={form.idle_timeout_seconds}
                      onChange={(event) => updateAliasForm(alias.id, "idle_timeout_seconds", event.target.value)}
                    />
                    <input
                      className="compact-text"
                      placeholder="operator notes"
                      value={form.notes}
                      onChange={(event) => updateAliasForm(alias.id, "notes", event.target.value)}
                    />
                    <div className="role-chips">
                      {ROLE_OPTIONS.map((role) => (
                        <label key={role} className="role-chip">
                          <input type="checkbox" checked={form.visibility_roles.includes(role)} onChange={() => toggleAliasRole(alias.id, role)} />
                          <span>{role}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                  <small>{alias.resource_label}{alias.cpu_resident_candidate ? " / CPU" : ""} / {alias.alias_policy_source ?? "seed"}</small>
                </td>
                <td>
                  <div className="table-actions">
                    <button title={`Save alias policy for ${alias.id}`} onClick={() => saveAliasPolicy(alias)} disabled={busy}><CheckCircle2 size={16} /></button>
                    <button title={`Reset alias policy for ${alias.id}`} onClick={() => resetAliasPolicy(alias)} disabled={busy || alias.alias_policy_source !== "database"}><RotateCcw size={16} /></button>
                    <button title={`Plan install for ${alias.id}`} onClick={() => planInstall(alias.id)} disabled={busy || !alias.resolved_model}><ListChecks size={16} /></button>
                    <button title={`Install ${alias.id}`} onClick={() => installModel(alias.id)} disabled={busy || !alias.resolved_model}><Archive size={16} /></button>
                    <button title={`Plan download for ${alias.id}`} onClick={() => planDownload(alias.id)} disabled={busy || !alias.resolved_model}><Download size={16} /></button>
                    <button title={`Queue download for ${alias.id}`} onClick={() => queueDownload(alias.id)} disabled={busy || !alias.resolved_model}><Download size={16} /></button>
                  </div>
                </td>
              </tr>
            );
          })}
          {!aliases.length && <tr><td colSpan={5}>No model aliases loaded</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <Boxes size={16} />
        <h3>Catalog Recommendations</h3>
      </div>
      <table>
        <thead><tr><th>Model</th><th>Status</th><th>Runtime</th><th>Policy</th><th>Actions</th></tr></thead>
        <tbody>
          {catalog.map((model) => (
            <tr key={`${model.id}@${model.version}`}>
              <td>
                <code>{model.display_name}</code>
                <small>{model.id}@{model.version}{model.aliases?.length ? ` / ${model.aliases.join(", ")}` : ""}</small>
              </td>
              <td>{model.status}<small>{model.modality}{model.operations?.length ? ` / ${model.operations.join(", ")}` : ""}</small></td>
              <td>{model.preferred_runtime}</td>
              <td>{model.resource_label}<small>{model.license?.name ?? "licence unknown"} / {model.license?.redistribution ?? "redistribution unknown"}</small></td>
              <td>
                <div className="table-actions">
                  <button title={`Plan install for ${model.display_name}`} onClick={() => planInstall(model.id)} disabled={busy || model.status === "installed"}><ListChecks size={16} /></button>
                  <button title={`Install ${model.display_name}`} onClick={() => installModel(model.id)} disabled={busy || model.status === "installed"}><Archive size={16} /></button>
                  <button title={`Plan download for ${model.display_name}`} onClick={() => planDownload(model.id)} disabled={busy || model.status === "installed"}><Download size={16} /></button>
                  <button title={`Queue download for ${model.display_name}`} onClick={() => queueDownload(model.id)} disabled={busy || model.status === "installed"}><Download size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!catalog.length && <tr><td colSpan={5}>No catalog recommendations loaded</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <Database size={16} />
        <h3>Installed Records</h3>
      </div>
      <table>
        <thead><tr><th>Model</th><th>Status</th><th>Runtime</th><th>Views</th><th>Policy</th><th>Actions</th></tr></thead>
        <tbody>
          {records.map((record) => (
            <tr key={`${record.id}@${record.version}`}>
              <td><code>{record.display_name}</code><small>{record.id}@{record.version}</small></td>
              <td>{record.status}<small>{record.modality}</small></td>
              <td>{record.preferred_runtime}</td>
              <td>{(record.runtime_views ?? []).map((view) => view.runtime).join(", ") || "none"}</td>
              <td>{record.resource_label}</td>
              <td>
                <div className="table-actions">
                  <button title={`Quarantine model record for ${record.display_name}`} onClick={() => quarantineModel(record)} disabled={busy || record.status !== "installed"}><Trash2 size={16} /></button>
                  <button title={`Plan authoritative blob quarantine for ${record.display_name}`} onClick={() => planBlobQuarantine(record)} disabled={busy || record.status === "installed"}><Database size={16} /></button>
                  <button title={`Quarantine authoritative blobs for ${record.display_name}`} onClick={() => quarantineBlobs(record)} disabled={busy || record.status === "installed"}><HardDrive size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!records.length && <tr><td colSpan={6}>No installed model records</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <Download size={16} />
        <h3>Downloads</h3>
      </div>
      <table>
        <thead><tr><th>Download</th><th>Status</th><th>Progress</th><th>Error</th><th>Actions</th></tr></thead>
        <tbody>
          {downloads.map((download) => (
            <tr key={download.id}>
              <td>
                <code>{download.model_id}@{download.model_version}</code>
                <small>{download.id}{download.authenticated ? ` / authenticated ${download.credential_secret_name ?? ""}` : ""}</small>
              </td>
              <td>{download.status}<small>{download.stage}</small></td>
              <td>{download.progress_percent}%<small>{formatBytes(download.bytes_downloaded)} / {formatBytes(download.target_size_bytes)} across {download.file_count || 1} file{(download.file_count || 1) === 1 ? "" : "s"}</small></td>
              <td>{download.error_category ?? ""}<small>{download.error_message ?? ""}</small></td>
              <td><div className="table-actions">
                <button title={`Pause ${download.id}`} onClick={() => pauseDownload(download)} disabled={busy || !["queued", "running"].includes(download.status)}><PauseCircle size={16} /></button>
                <button title={`Resume ${download.id}`} onClick={() => resumeDownload(download)} disabled={busy || download.status !== "paused"}><PlayCircle size={16} /></button>
                <button title={`Retry ${download.id}`} onClick={() => retryDownload(download)} disabled={busy || !["failed", "cancelled"].includes(download.status)}><RotateCcw size={16} /></button>
                <button title={`Cancel ${download.id}`} onClick={() => cancelDownload(download)} disabled={busy || ["completed", "failed", "cancelled"].includes(download.status)}><Trash2 size={16} /></button>
              </div></td>
            </tr>
          ))}
          {!downloads.length && <tr><td colSpan={5}>No model downloads recorded</td></tr>}
        </tbody>
      </table>
    </section>
  );
}

function Runtimes() {
  const [health, setHealth] = useState<RuntimeAdapterStatus[]>([]);
  const [readiness, setReadiness] = useState<SelfTestCheck | null>(null);
  const [deploymentMode, setDeploymentMode] = useState("unknown");
  const [productionRequired, setProductionRequired] = useState<string[]>([]);
  const [externalConfigs, setExternalConfigs] = useState<ExternalRuntimeConfig[]>([]);
  const [externalConfigForms, setExternalConfigForms] = useState<Record<string, ExternalRuntimeConfigForm>>({});
  const [externalProvidersAllowed, setExternalProvidersAllowed] = useState(false);
  const [externalConfigMessage, setExternalConfigMessage] = useState("idle");
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [profileExport, setProfileExport] = useState("");
  const [profileMessage, setProfileMessage] = useState("idle");
  const [profileForm, setProfileForm] = useState({
    display_name: "",
    runtime: "voicebox",
    engine: "voicebox",
    model_alias: "tts-quality",
    profile_type: "preset",
    status: "active",
    visibility_roles: "admin,operator"
  });
  const [profileMetadata, setProfileMetadata] = useState("{}");
  const [profileArtifacts, setProfileArtifacts] = useState("[]");
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);

  const loadRuntimes = () => {
    setMessage("loading");
    apiFetch(`/admin/runtimes`)
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setHealth(payload.health ?? []);
        setReadiness(payload.readiness ?? null);
        setDeploymentMode(payload.runtime_deployment_mode ?? "unknown");
        setProductionRequired(Array.isArray(payload.production_required_runtimes) ? payload.production_required_runtimes : []);
        setMessage("ready");
      })
      .catch((err: Error) => setMessage(err.message));
  };

  const formFromExternalConfig = (config: ExternalRuntimeConfig): ExternalRuntimeConfigForm => ({
    enabled: config.enabled,
    base_url: config.base_url,
    api_key_secret_name: config.api_key_secret_name ?? "",
    confirm_external_data: config.external_data_acknowledged,
    notes: config.notes ?? ""
  });

  const loadExternalConfigs = () => {
    setExternalConfigMessage("loading");
    apiJson<{ object: string; allow_external_providers: boolean; data: ExternalRuntimeConfig[] }>(`/admin/runtimes/external-config`)
      .then((payload) => {
        const data = payload.data ?? [];
        setExternalConfigs(data);
        setExternalProvidersAllowed(Boolean(payload.allow_external_providers));
        setExternalConfigForms(Object.fromEntries(data.map((config) => [config.runtime, formFromExternalConfig(config)])));
        setExternalConfigMessage("ready");
      })
      .catch((error: Error) => setExternalConfigMessage(error.message));
  };

  const loadVoiceProfiles = () => {
    setProfileMessage("loading");
    apiJson<{ object: string; data: VoiceProfile[] }>(`/admin/voicebox/profiles`)
      .then((payload) => {
        setProfiles(payload.data ?? []);
        setProfileMessage("ready");
      })
      .catch((error: Error) => setProfileMessage(error.message));
  };

  useEffect(() => {
    loadRuntimes();
    loadExternalConfigs();
    loadVoiceProfiles();
  }, []);

  const runtimeAction = (runtime: string, action: "unload" | "recover") => {
    setBusy(true);
    setMessage(`${action} ${runtime}`);
    apiFetch(`/admin/runtimes/${runtime}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: `Control Center ${action}`, timeout_seconds: 10 })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setMessage(`${payload.action} ${payload.runtime}: ${payload.runtime_agent?.status ?? "requested"}`);
        loadRuntimes();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const updateExternalConfigForm = (runtime: string, changes: Partial<ExternalRuntimeConfigForm>) => {
    setExternalConfigForms((current) => ({
      ...current,
      [runtime]: {
        ...(current[runtime] ?? {
          enabled: false,
          base_url: "",
          api_key_secret_name: "",
          confirm_external_data: false,
          notes: ""
        }),
        ...changes
      }
    }));
  };

  const saveExternalConfig = (runtime: string) => {
    const form = externalConfigForms[runtime];
    if (!form) return;
    setBusy(true);
    setExternalConfigMessage(`saving ${runtime}`);
    apiJson<ExternalRuntimeConfig>(`/admin/runtimes/external-config/${encodeURIComponent(runtime)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: form.enabled,
        base_url: form.base_url,
        api_key_secret_name: form.api_key_secret_name.trim() || null,
        confirm_external_data: form.confirm_external_data,
        notes: form.notes
      })
    })
      .then((config) => {
        setExternalConfigs((current) => current.map((item) => item.runtime === config.runtime ? config : item));
        setExternalConfigForms((current) => ({ ...current, [config.runtime]: formFromExternalConfig(config) }));
        setExternalConfigMessage(`${config.runtime} ${config.status}`);
        loadRuntimes();
      })
      .catch((error: Error) => setExternalConfigMessage(error.message))
      .finally(() => setBusy(false));
  };

  const updateProfileForm = (field: string, value: string) => {
    setProfileForm((current) => ({ ...current, [field]: value }));
  };

  const createVoiceProfile = () => {
    setBusy(true);
    setProfileMessage("creating");
    try {
      const metadata = profileMetadata.trim() ? JSON.parse(profileMetadata) : {};
      const sample_artifacts = profileArtifacts.trim() ? JSON.parse(profileArtifacts) : [];
      if (!metadata || Array.isArray(metadata) || typeof metadata !== "object") throw new Error("metadata must be an object");
      if (!Array.isArray(sample_artifacts)) throw new Error("sample artifacts must be an array");
      apiJson<VoiceProfile>(`/admin/voicebox/profiles`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...profileForm,
          visibility_roles: profileForm.visibility_roles.split(",").map((role) => role.trim()).filter(Boolean),
          metadata,
          sample_artifacts
        })
      })
        .then((profile) => {
          setProfileMessage(`created ${profile.id}`);
          setProfileExport("");
          setProfileForm((current) => ({ ...current, display_name: "" }));
          loadVoiceProfiles();
        })
        .catch((error: Error) => setProfileMessage(error.message))
        .finally(() => setBusy(false));
    } catch (error) {
      setProfileMessage(error instanceof Error ? error.message : "invalid profile payload");
      setBusy(false);
    }
  };

  const exportVoiceProfile = (profile: VoiceProfile) => {
    setBusy(true);
    setProfileMessage(`export ${profile.id}`);
    apiJson<Record<string, unknown>>(`/admin/voicebox/profiles/${encodeURIComponent(profile.id)}/export`, { method: "POST" })
      .then((payload) => {
        setProfileExport(JSON.stringify(payload, null, 2));
        setProfileMessage(`exported ${profile.id}`);
      })
      .catch((error: Error) => setProfileMessage(error.message))
      .finally(() => setBusy(false));
  };

  const deleteVoiceProfile = (profile: VoiceProfile) => {
    setBusy(true);
    setProfileMessage(`delete ${profile.id}`);
    apiJson<VoiceProfile>(`/admin/voicebox/profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE" })
      .then((payload) => {
        setProfileMessage(`${payload.id} ${payload.status}`);
        loadVoiceProfiles();
      })
      .catch((error: Error) => setProfileMessage(error.message))
      .finally(() => setBusy(false));
  };

  const runtimes = health.length ? health : [
    { name: "localai", status: "pending", requires_gpu: true },
    { name: "comfyui", status: "pending", requires_gpu: true, native_api: true },
    { name: "voicebox", status: "pending", requires_gpu: true, native_api: true },
    { name: "audio-cpu", status: "pending", requires_gpu: false }
  ];

  return (
    <div className="panel-grid">
      <section className="panel wide">
        <SectionTitle icon={<TerminalSquare size={18} />} title="Runtimes" />
        <div className="toolbar">
          <button title="Refresh runtimes" onClick={() => { loadRuntimes(); loadExternalConfigs(); }} disabled={busy}><RefreshCw size={16} />Refresh</button>
          <span className="toolbar-status">{message}</span>
        </div>
        {readiness && (
          <div className="one-time-key">
            <strong>Runtime readiness: {readiness.status}</strong>
            <small>{readiness.detail}</small>
            <small>mode: {deploymentMode} / required: {productionRequired.length ? productionRequired.join(", ") : "none"}</small>
          </div>
        )}
      </section>
      {runtimes.map((runtime) => {
        const details = detailRecord(runtime.details);
        const reportedCapabilities = runtime.capabilities ?? (details.capabilities as RuntimeAdapterCapabilities | undefined);
        const capabilities = runtimeCapabilitySummary(reportedCapabilities) || capabilitySummary(details.capabilities);
        const contract = runtime.adapter_contract;
        const surfaces = contract?.surfaces;
        return (
          <section className="panel" key={runtime.name}>
            <SectionTitle icon={<TerminalSquare size={18} />} title={runtime.name} />
            <table>
              <tbody>
                <tr><td>Health</td><td>{runtime.status}</td></tr>
                <tr><td>Active model</td><td>{runtime.runtime_state?.active_model ?? "unknown"}</td></tr>
                <tr><td>Scheduler state</td><td>{runtime.runtime_state ? `${runtime.runtime_state.stage} / ${runtime.runtime_state.status}` : "not observed"}</td></tr>
                {runtime.runtime_state?.job_id && <tr><td>Last job</td><td>{runtime.runtime_state.job_id}</td></tr>}
                <tr><td>GPU lease</td><td>{runtime.requires_gpu ? "required" : "not required"}</td></tr>
                <tr><td>API</td><td>{runtime.openai_compatible ? "OpenAI" : runtime.native_api ? "native" : "internal"}</td></tr>
                {contract && <tr><td>Contract</td><td>{contract.version}<small>{surfaces ? `${surfaces.scheduler ?? "scheduler"} / ${surfaces.submit ?? "submit"} / ${surfaces.events ?? "events"}` : ""}</small></td></tr>}
                {details.engine !== undefined && <tr><td>Engine</td><td>{String(details.engine)}</td></tr>}
                {details.placeholder !== undefined && <tr><td>Placeholder</td><td>{booleanLabel(details.placeholder)}</td></tr>}
                {details.placeholder_enabled !== undefined && <tr><td>Placeholder enabled</td><td>{booleanLabel(details.placeholder_enabled)}</td></tr>}
                {reportedCapabilities ? (
                  <>
                    <tr><td>Modalities</td><td>{capabilityListLabel(reportedCapabilities.modalities)}</td></tr>
                    <tr><td>Operations</td><td>{capabilityListLabel(reportedCapabilities.operations)}</td></tr>
                    <tr><td>Capability flags</td><td>{runtimeCapabilityFlags(reportedCapabilities)}</td></tr>
                  </>
                ) : capabilities && <tr><td>Capabilities</td><td>{capabilities}</td></tr>}
                {contract?.methods && <tr><td>Methods</td><td>{contractMethodSummary(contract.methods)}</td></tr>}
                {runtime.error && <tr><td>Error</td><td>{runtime.error}</td></tr>}
              </tbody>
            </table>
            <div className="toolbar">
              <button title={`Unload ${runtime.name}`} onClick={() => runtimeAction(runtime.name, "unload")} disabled={busy || Boolean(runtime.external)}><PauseCircle size={16} />Unload</button>
              <button title={`Recover ${runtime.name}`} onClick={() => runtimeAction(runtime.name, "recover")} disabled={busy || Boolean(runtime.external)}><RotateCcw size={16} />Recover</button>
            </div>
          </section>
        );
      })}
      <section className="panel wide">
        <SectionTitle icon={<ShieldCheck size={18} />} title="External Runtime Configuration" />
        <div className="toolbar">
          <button title="Refresh external runtime configuration" onClick={loadExternalConfigs} disabled={busy}><RefreshCw size={16} />Refresh</button>
          <span className="toolbar-status">{externalConfigMessage}</span>
          <span className={statusPillClass(externalProvidersAllowed ? "eligible" : "disabled")}>global {externalProvidersAllowed ? "enabled" : "disabled"}</span>
        </div>
        <div className="table-scroll">
          <table className="config-table">
            <thead><tr><th>Runtime</th><th>Enabled</th><th>Base URL</th><th>Secret</th><th>Acknowledgement</th><th>Notes</th><th>Actions</th></tr></thead>
            <tbody>
              {externalConfigs.map((config) => {
                const form = externalConfigForms[config.runtime] ?? formFromExternalConfig(config);
                return (
                  <tr key={config.runtime}>
                    <td>
                      <code>{config.runtime}</code>
                      <small>{config.source} / <span className={statusPillClass(config.status)}>{config.status}</span></small>
                      {config.configuration_error && <small>{config.configuration_error}</small>}
                    </td>
                    <td>
                      <label className="inline-check">
                        <input type="checkbox" checked={form.enabled} onChange={(event) => updateExternalConfigForm(config.runtime, { enabled: event.target.checked })} />
                        Enabled
                      </label>
                    </td>
                    <td>
                      <input value={form.base_url} onChange={(event) => updateExternalConfigForm(config.runtime, { base_url: event.target.value })} maxLength={2048} placeholder="https://provider.example/v1" />
                    </td>
                    <td>
                      <input value={form.api_key_secret_name} onChange={(event) => updateExternalConfigForm(config.runtime, { api_key_secret_name: event.target.value })} maxLength={128} placeholder="remote-provider secret" />
                      <small>{config.api_key_configured ? "configured" : "none"}</small>
                    </td>
                    <td>
                      <label className="inline-check">
                        <input type="checkbox" checked={form.confirm_external_data} onChange={(event) => updateExternalConfigForm(config.runtime, { confirm_external_data: event.target.checked })} />
                        External data
                      </label>
                      <small>{config.warning}</small>
                    </td>
                    <td>
                      <textarea value={form.notes} onChange={(event) => updateExternalConfigForm(config.runtime, { notes: event.target.value })} maxLength={2000} />
                    </td>
                    <td>
                      <div className="table-actions">
                        <button title={`Save ${config.runtime} configuration`} onClick={() => saveExternalConfig(config.runtime)} disabled={busy}><CheckCircle2 size={16} /></button>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {!externalConfigs.length && <tr><td colSpan={7}>No external runtime adapters available</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
      <section className="panel wide">
        <SectionTitle icon={<TerminalSquare size={18} />} title="Voicebox Profiles" />
        <div className="toolbar">
          <button title="Refresh voice profiles" onClick={loadVoiceProfiles} disabled={busy}><RefreshCw size={16} />Refresh</button>
          <span className="toolbar-status">{profileMessage}</span>
        </div>
        <div className="split voice-profile-admin">
          <div className="stack">
            <label>Display name<input value={profileForm.display_name} onChange={(event) => updateProfileForm("display_name", event.target.value)} /></label>
            <label>Runtime
              <select value={profileForm.runtime} onChange={(event) => updateProfileForm("runtime", event.target.value)}>
                <option value="voicebox">voicebox</option>
                <option value="audio-cpu">audio-cpu</option>
              </select>
            </label>
            <label>Engine<input value={profileForm.engine} onChange={(event) => updateProfileForm("engine", event.target.value)} /></label>
            <label>Model alias<input value={profileForm.model_alias} onChange={(event) => updateProfileForm("model_alias", event.target.value)} /></label>
            <label>Profile type
              <select value={profileForm.profile_type} onChange={(event) => updateProfileForm("profile_type", event.target.value)}>
                <option value="preset">preset</option>
                <option value="reference">reference</option>
                <option value="clone">clone</option>
              </select>
            </label>
            <label>Status
              <select value={profileForm.status} onChange={(event) => updateProfileForm("status", event.target.value)}>
                <option value="active">active</option>
                <option value="disabled">disabled</option>
              </select>
            </label>
            <label>Visibility roles<input value={profileForm.visibility_roles} onChange={(event) => updateProfileForm("visibility_roles", event.target.value)} /></label>
            <label>Metadata JSON<textarea value={profileMetadata} onChange={(event) => setProfileMetadata(event.target.value)} /></label>
            <label>Sample artifacts JSON<textarea value={profileArtifacts} onChange={(event) => setProfileArtifacts(event.target.value)} /></label>
            <button title="Create voice profile" onClick={createVoiceProfile} disabled={busy || !profileForm.display_name.trim()}><Upload size={16} />Create</button>
          </div>
          <div className="stack">
            <table>
              <thead><tr><th>Profile</th><th>Runtime</th><th>Type</th><th>Roles</th><th>Samples</th><th>Actions</th></tr></thead>
              <tbody>
                {profiles.map((profile) => (
                  <tr key={profile.id}>
                    <td>{profile.display_name}<small>{profile.id}</small></td>
                    <td>{profile.runtime}<small>{profile.engine} / {profile.model_alias}</small></td>
                    <td>{profile.profile_type}<small>{profile.status}</small></td>
                    <td>{profile.visibility_roles.join(", ")}</td>
                    <td>{profile.sample_artifacts.length}</td>
                    <td><div className="table-actions">
                      <button title={`Export ${profile.id}`} onClick={() => exportVoiceProfile(profile)} disabled={busy}><Download size={16} /></button>
                      <button title={`Delete ${profile.id}`} onClick={() => deleteVoiceProfile(profile)} disabled={busy}><Trash2 size={16} /></button>
                    </div></td>
                  </tr>
                ))}
                {!profiles.length && <tr><td colSpan={6}>No voice profiles registered</td></tr>}
              </tbody>
            </table>
            {profileExport && <pre className="profile-export">{profileExport}</pre>}
          </div>
        </div>
      </section>
    </div>
  );
}

const JOB_STATES = ["", "created", "validated", "queued", "waiting_for_gpu", "unloading", "verifying_vram", "loading", "warming", "running", "saving", "completed", "cancelling", "cancelled", "failed", "expired", "recovery_required"];
const JOB_PRIORITIES = ["chat", "interactive_audio", "single_image", "image_batch", "video", "batch"];
const RESERVATION_STATUSES = ["", "active", "cancelled", "expired"];
const TERMINAL_JOB_STATES = new Set(["completed", "cancelled", "failed", "expired", "recovery_required"]);
const RETRYABLE_JOB_STATES = new Set(["failed", "cancelled", "expired", "recovery_required"]);
const PRIORITIZABLE_JOB_STATES = new Set(["created", "validated", "queued", "waiting_for_gpu"]);

function formatDateTime(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "";
}

function formatMs(value?: number | null) {
  if (typeof value !== "number") return "pending";
  if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
  return `${value}ms`;
}

type JobReproducibilityEntry = {
  label: string;
  value: string;
};

function objectOrNull(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string | number | boolean => ["string", "number", "boolean"].includes(typeof item))
    .map((item) => String(item).trim())
    .filter(Boolean);
}

function acceptanceOperatorEvidenceRows(report: Record<string, unknown>): AcceptanceEvidenceDetail[] {
  const rows = Array.isArray(report.operator_evidence) ? report.operator_evidence : [];
  return rows
    .map((item) => objectOrNull(item))
    .filter((item): item is Record<string, unknown> => item !== null)
    .map((item) => ({
      key: String(item.key ?? ""),
      label: String(item.label ?? item.key ?? ""),
      passed: item.passed === true,
      note: typeof item.note === "string" ? item.note : ""
    }))
    .filter((item) => item.key || item.label);
}

function acceptanceLiveEvidenceRows(report: Record<string, unknown>): LiveEvidenceDetail[] {
  const liveEvidence = objectOrNull(report.live_evidence) ?? {};
  return ACCEPTANCE_LIVE_EVIDENCE_SECTIONS.map(([key, label]) => {
    const snapshot = objectOrNull(liveEvidence[key]) ?? {};
    const reason = typeof snapshot.reason === "string" ? snapshot.reason : "";
    const status = typeof snapshot.status === "string" ? snapshot.status : reason || "missing";
    return {
      key,
      label,
      available: snapshot.available === true,
      status,
      sourcePath: typeof snapshot.source_path === "string" ? snapshot.source_path : "",
      missingChecks: stringList(snapshot.missing_checks),
      samples: stringList(snapshot.sample_labels)
    };
  });
}

function acceptancePreservedResourceRows(report: Record<string, unknown>): PreservedResourceDetail[] {
  const preservation = objectOrNull(report.cutover_preservation) ?? {};
  const resources = objectOrNull(preservation.resources) ?? {};
  return ACCEPTANCE_PRESERVED_RESOURCE_SECTIONS.map(([key, label]) => ({
    key,
    label,
    items: stringList(resources[key])
  }));
}

function labelFromKey(value: string): string {
  return value.replaceAll("_", " ").replaceAll("-", " ");
}

function compactText(value: string, limit = 110): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > limit ? `${normalized.slice(0, limit - 1)}...` : normalized;
}

function compactReproducibilityValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "empty";
  if (typeof value === "string") return compactText(value);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `[array: ${value.length}]`;
  const record = objectOrNull(value);
  if (record) {
    const keys = Object.keys(record);
    return keys.length ? `[object: ${keys.slice(0, 4).join(", ")}${keys.length > 4 ? ", ..." : ""}]` : "[object]";
  }
  return compactText(String(value));
}

function addJobReproducibilityEntry(entries: JobReproducibilityEntry[], label: string, value: unknown): void {
  if (value === undefined || value === null || value === "") return;
  entries.push({ label, value: compactReproducibilityValue(value) });
}

function jobReproducibilityEntries(job: JobRecord): JobReproducibilityEntry[] {
  const request = job.redacted_request ?? {};
  const input = objectOrNull(request.input);
  const parameters = objectOrNull(input?.parameters);
  const entries: JobReproducibilityEntry[] = [];
  addJobReproducibilityEntry(entries, "model", request.model ?? job.model_alias);
  addJobReproducibilityEntry(entries, "resolved model", job.resolved_model_version);
  addJobReproducibilityEntry(entries, "runtime", job.runtime);
  addJobReproducibilityEntry(entries, "runtime policy", request.runtime_policy);
  addJobReproducibilityEntry(entries, "priority", request.priority ?? job.priority);
  addJobReproducibilityEntry(entries, "workflow", input?.workflow_id);
  addJobReproducibilityEntry(entries, "workflow version", input?.workflow_version);
  Object.entries(parameters ?? {}).slice(0, 10).forEach(([name, value]) => {
    addJobReproducibilityEntry(entries, labelFromKey(name), value);
  });
  return entries;
}

function JobReproducibilitySummary({ job }: { job: JobRecord }) {
  const entries = jobReproducibilityEntries(job);
  return (
    <section className="job-reproducibility">
      <h3>Reproducibility</h3>
      <dl>
        {entries.map((entry) => (
          <div key={entry.label}>
            <dt>{entry.label}</dt>
            <dd>{entry.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Jobs() {
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [reservations, setReservations] = useState<RuntimeReservationRecord[]>([]);
  const [schedulerLease, setSchedulerLease] = useState<SchedulerLease | null>(null);
  const [selected, setSelected] = useState<JobRecord | null>(null);
  const [stateFilter, setStateFilter] = useState("");
  const [runtimeFilter, setRuntimeFilter] = useState("");
  const [modalityFilter, setModalityFilter] = useState("");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [reservationStatusFilter, setReservationStatusFilter] = useState("active");
  const [reservationRuntime, setReservationRuntime] = useState("localai");
  const [reservationModel, setReservationModel] = useState("chat-default");
  const [reservationDuration, setReservationDuration] = useState("300");
  const [reservationReason, setReservationReason] = useState("");
  const [priorityByJob, setPriorityByJob] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);

  const loadJobs = () => {
    setMessage("loading");
    const params = new URLSearchParams({ limit: "100" });
    if (stateFilter) params.set("state", stateFilter);
    if (runtimeFilter.trim()) params.set("runtime", runtimeFilter.trim());
    if (modalityFilter.trim()) params.set("modality", modalityFilter.trim());
    if (ownerFilter.trim()) params.set("owner_id", ownerFilter.trim());
    apiJson<{ object: string; data: JobRecord[] }>(`/admin/jobs?${params.toString()}`)
      .then((payload) => {
        const rows = payload.data ?? [];
        setJobs(rows);
        setPriorityByJob(Object.fromEntries(rows.map((job) => [job.id, job.priority])));
        if (selected) {
          setSelected(rows.find((job) => job.id === selected.id) ?? rows[0] ?? null);
        } else {
          setSelected(rows[0] ?? null);
        }
        setMessage("ready");
      })
      .catch((error: Error) => setMessage(error.message));
  };

  const loadReservations = () => {
    const params = new URLSearchParams({ limit: "50" });
    if (reservationStatusFilter) params.set("status", reservationStatusFilter);
    if (runtimeFilter.trim()) params.set("runtime", runtimeFilter.trim());
    if (ownerFilter.trim()) params.set("owner_id", ownerFilter.trim());
    Promise.all([
      apiJson<{ object: string; data: RuntimeReservationRecord[] }>(`/admin/runtime-reservations?${params.toString()}`),
      apiJson<{ lease?: SchedulerLease | null }>(`/admin/scheduler/lease`)
    ])
      .then(([reservationPayload, leasePayload]) => {
        setReservations(reservationPayload.data ?? []);
        setSchedulerLease(leasePayload.lease ?? null);
      })
      .catch((error: Error) => setMessage(error.message));
  };

  useEffect(() => {
    loadJobs();
    loadReservations();
  }, []);

  useEffect(() => {
    if (!selected || TERMINAL_JOB_STATES.has(selected.state)) return;
    const controller = new AbortController();
    setMessage(`watching ${selected.id}`);
    streamAdminJobEvents(selected.id, controller.signal, (job) => {
      setSelected(job);
      setJobs((current) => current.map((row) => (row.id === job.id ? job : row)));
      setMessage(`${job.id} ${job.state}`);
      if (TERMINAL_JOB_STATES.has(job.state)) {
        loadJobs();
        loadReservations();
      }
    }).catch((error: Error) => {
      if (controller.signal.aborted) return;
      setMessage(error.message);
      loadJobs();
    });
    return () => controller.abort();
  }, [selected?.id]);

  const mutateJob = (job: JobRecord, action: "cancel" | "retry") => {
    setBusy(true);
    setMessage(`${action} ${job.id}`);
    apiJson<JobRecord>(`/admin/jobs/${encodeURIComponent(job.id)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: `Control Center ${action}` })
    })
      .then((payload) => {
        setSelected(payload);
        setMessage(`${payload.id} ${payload.state}`);
        loadJobs();
      })
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const updatePriority = (job: JobRecord) => {
    const priority = priorityByJob[job.id] ?? job.priority;
    setBusy(true);
    setMessage(`priority ${job.id}`);
    apiJson<JobRecord>(`/admin/jobs/${encodeURIComponent(job.id)}/priority`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ priority, reason: "Control Center priority update" })
    })
      .then((payload) => {
        setSelected(payload);
        setMessage(`${payload.id} priority ${payload.priority}`);
        loadJobs();
      })
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const createReservation = (event: React.FormEvent) => {
    event.preventDefault();
    const duration = Math.max(30, Math.min(7200, Number.parseInt(reservationDuration, 10) || 300));
    setReservationDuration(String(duration));
    setBusy(true);
    setMessage("creating reservation");
    apiJson<RuntimeReservationRecord>(`/v1/runtime-reservations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        runtime: reservationRuntime.trim(),
        model: reservationModel.trim(),
        duration_seconds: duration,
        reason: reservationReason.trim()
      })
    })
      .then((payload) => {
        setMessage(`${payload.id} ${payload.status}`);
        setReservationReason("");
        loadReservations();
      })
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const cancelReservation = (reservation: RuntimeReservationRecord) => {
    setBusy(true);
    setMessage(`cancel ${reservation.id}`);
    apiJson<RuntimeReservationRecord>(`/v1/runtime-reservations/${encodeURIComponent(reservation.id)}`, { method: "DELETE" })
      .then((payload) => {
        setMessage(`${payload.id} ${payload.status}`);
        loadReservations();
      })
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const downloadSelectedArtifact = (artifact: JobArtifact, index: number) => {
    setBusy(true);
    setMessage(`downloading artifact ${index + 1}`);
    downloadJobArtifact(artifact, index)
      .then((filename) => setMessage(`downloaded ${filename}`))
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  return (
    <section className="panel wide">
      <SectionTitle icon={<ListChecks size={18} />} title="Jobs" />
      <div className="toolbar job-filters">
        <select aria-label="State filter" value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}>
          {JOB_STATES.map((state) => <option key={state || "all"} value={state}>{state || "all states"}</option>)}
        </select>
        <input aria-label="Runtime filter" placeholder="runtime" value={runtimeFilter} onChange={(event) => setRuntimeFilter(event.target.value)} />
        <input aria-label="Modality filter" placeholder="modality" value={modalityFilter} onChange={(event) => setModalityFilter(event.target.value)} />
        <input aria-label="Owner filter" placeholder="owner" value={ownerFilter} onChange={(event) => setOwnerFilter(event.target.value)} />
        <button title="Refresh jobs" onClick={loadJobs} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <span className="toolbar-status">{message}</span>
      </div>
      <div className="subsection-title">
        <Gauge size={16} />
        <h3>Reservations</h3>
      </div>
      <form className="toolbar job-filters" onSubmit={createReservation}>
        <select aria-label="Reservation status filter" value={reservationStatusFilter} onChange={(event) => setReservationStatusFilter(event.target.value)}>
          {RESERVATION_STATUSES.map((status) => <option key={status || "all"} value={status}>{status || "all reservations"}</option>)}
        </select>
        <input aria-label="Reservation runtime" placeholder="runtime" value={reservationRuntime} onChange={(event) => setReservationRuntime(event.target.value)} />
        <input aria-label="Reservation model" placeholder="model alias" value={reservationModel} onChange={(event) => setReservationModel(event.target.value)} />
        <input aria-label="Reservation duration seconds" inputMode="numeric" placeholder="seconds" value={reservationDuration} onChange={(event) => setReservationDuration(event.target.value)} />
        <input aria-label="Reservation reason" placeholder="reason" value={reservationReason} onChange={(event) => setReservationReason(event.target.value)} maxLength={500} />
        <button title="Create runtime reservation" type="submit" disabled={busy || !reservationRuntime.trim() || !reservationModel.trim()}><Upload size={16} />Reserve</button>
        <button title="Refresh reservations" type="button" onClick={loadReservations} disabled={busy}><RefreshCw size={16} />Refresh</button>
      </form>
      <div className="one-time-key">
        <strong>GPU lease {schedulerLease?.owner ?? "idle"}</strong>
        <small>{schedulerLease?.lease_expires_at ? `expires ${new Date(schedulerLease.lease_expires_at).toLocaleString()}` : "no active scheduler owner recorded"}</small>
      </div>
      <table>
        <thead><tr><th>Reservation</th><th>Status</th><th>Runtime</th><th>Model</th><th>Expires</th><th>Actions</th></tr></thead>
        <tbody>
          {reservations.map((reservation) => (
            <tr key={reservation.id}>
              <td><code>{reservation.id}</code><small>owner {reservation.owner_id}</small></td>
              <td><span className={`status-pill ${reservation.status}`}>{reservation.status}</span><small>{reservation.reason ?? ""}</small></td>
              <td>{reservation.runtime}</td>
              <td>{reservation.model_alias}<small>{reservation.resolved_model_version}</small></td>
              <td>{formatDateTime(reservation.expires_at)}<small>{reservation.duration_seconds}s</small></td>
              <td>
                <div className="table-actions">
                  <button title={`Cancel ${reservation.id}`} onClick={() => cancelReservation(reservation)} disabled={busy || reservation.status !== "active"}><Trash2 size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!reservations.length && <tr><td colSpan={6}>No runtime reservations match the current filters</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <ListChecks size={16} />
        <h3>Job Queue</h3>
      </div>
      <table>
        <thead><tr><th>Job</th><th>State</th><th>Runtime</th><th>Priority</th><th>Measured</th><th>Actions</th></tr></thead>
        <tbody>
          {jobs.map((job) => {
            const priority = priorityByJob[job.id] ?? job.priority;
            return (
              <tr key={job.id}>
                <td><code>{job.id}</code><small>{job.modality} / {job.operation} / {formatDateTime(job.created_at)}</small></td>
                <td><span className={`status-pill ${job.state}`}>{job.state}</span><small>{job.stage ?? ""} / {job.progress ?? 0}% / retry {job.retry_count ?? 0}</small></td>
                <td>{job.runtime}<small>{job.model_alias} / {job.resolved_model_version}</small></td>
                <td>
                  <select aria-label={`Priority for ${job.id}`} value={priority} onChange={(event) => setPriorityByJob((current) => ({ ...current, [job.id]: event.target.value }))} disabled={busy || !PRIORITIZABLE_JOB_STATES.has(job.state)}>
                    {JOB_PRIORITIES.map((item) => <option key={item} value={item}>{item}</option>)}
                  </select>
                </td>
                <td>{formatMs(job.run_time_ms)}<small>load {formatMs(job.load_time_ms)} / VRAM {job.peak_vram_mib ?? "pending"} MiB</small></td>
                <td>
                  <div className="table-actions">
                    <button title={`Inspect ${job.id}`} onClick={() => setSelected(job)} disabled={busy}><ScrollText size={16} /></button>
                    <button title={`Apply priority for ${job.id}`} onClick={() => updatePriority(job)} disabled={busy || !PRIORITIZABLE_JOB_STATES.has(job.state) || priority === job.priority}><ListChecks size={16} /></button>
                    <button title={`Cancel ${job.id}`} onClick={() => mutateJob(job, "cancel")} disabled={busy || TERMINAL_JOB_STATES.has(job.state)}><Trash2 size={16} /></button>
                    <button title={`Retry ${job.id}`} onClick={() => mutateJob(job, "retry")} disabled={busy || !RETRYABLE_JOB_STATES.has(job.state)}><RotateCcw size={16} /></button>
                  </div>
                </td>
              </tr>
            );
          })}
          {!jobs.length && <tr><td colSpan={6}>No jobs match the current filters</td></tr>}
        </tbody>
      </table>
      {selected && (
        <div className="job-detail">
          <div>
            <strong>{selected.id}</strong>
            <small>{selected.correlation_id} / owner {selected.owner_id}</small>
          </div>
          <div>
            <strong>{selected.state}</strong>
            <small>{selected.failure_category ?? "no failure"} {selected.failure_message ?? ""}</small>
          </div>
          <div>
            <strong>{selected.native_prompt_id ?? "no native prompt"}</strong>
            <small>started {formatDateTime(selected.started_at)} / completed {formatDateTime(selected.completed_at)}</small>
          </div>
          <div>
            <strong>{(selected.artifacts ?? []).length} artifact{(selected.artifacts ?? []).length === 1 ? "" : "s"}</strong>
            <small>{(selected.artifacts ?? []).map((artifact) => artifact.url).filter(Boolean).join(" / ") || "none"}</small>
          </div>
          {Boolean((selected.artifacts ?? []).length) && (
            <section className="job-artifacts">
              <h3>Artifacts</h3>
              <div className="job-artifact-actions">
                {(selected.artifacts ?? []).map((artifact, index) => (
                  <button key={`${artifact.url ?? "artifact"}-${index}`} type="button" title={`Download artifact ${index + 1}`} onClick={() => downloadSelectedArtifact(artifact, index)} disabled={busy || !artifact.url}>
                    <Download size={16} />
                    <span>{artifact.url ?? `artifact ${index + 1}`}</span>
                    <small>{artifact.mime_type ?? "unknown"} / {formatBytes(artifact.bytes)}</small>
                  </button>
                ))}
              </div>
            </section>
          )}
          <JobReproducibilitySummary job={selected} />
          <section className="job-redacted-request">
            <h3>Redacted Request</h3>
            <pre>{JSON.stringify(selected.redacted_request ?? {}, null, 2)}</pre>
          </section>
        </div>
      )}
    </section>
  );
}

function parseCsv(value: string, fallback: string[] = []) {
  const items = value.split(",").map((item) => item.trim()).filter(Boolean);
  return items.length ? items : fallback;
}

function ExternalAccess() {
  const [apiClients, setApiClients] = useState<ApiClient[]>([]);
  const [modelHubClients, setModelHubClients] = useState<ModelHubClient[]>([]);
  const [encryptedSecrets, setEncryptedSecrets] = useState<EncryptedSecret[]>([]);
  const [secretMasterKey, setSecretMasterKey] = useState<SecretMasterKeyStatus | null>(null);
  const [apiClientsAdminOnly, setApiClientsAdminOnly] = useState(false);
  const [modelHubClientsAdminOnly, setModelHubClientsAdminOnly] = useState(false);
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);
  const [apiDisplayName, setApiDisplayName] = useState("");
  const [apiRole, setApiRole] = useState("service");
  const [apiScopes, setApiScopes] = useState("jobs:read,jobs:write,models:read,inference:write");
  const [apiCidrs, setApiCidrs] = useState("");
  const [hubDisplayName, setHubDisplayName] = useState("");
  const [hubAllowedModels, setHubAllowedModels] = useState("chat-default");
  const [hubCidrs, setHubCidrs] = useState("192.168.2.0/24");
  const [hubAllowDownloads, setHubAllowDownloads] = useState(true);
  const [apiClientCidrs, setApiClientCidrs] = useState<Record<string, string>>({});
  const [modelHubClientCidrs, setModelHubClientCidrs] = useState<Record<string, string>>({});
  const [modelHubClientAllowedModels, setModelHubClientAllowedModels] = useState<Record<string, string>>({});
  const [modelHubClientDownloads, setModelHubClientDownloads] = useState<Record<string, boolean>>({});
  const [secretName, setSecretName] = useState("");
  const [secretDisplayName, setSecretDisplayName] = useState("");
  const [secretCategory, setSecretCategory] = useState<SecretCategory>("other");
  const [secretDescription, setSecretDescription] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [oneTimeKey, setOneTimeKey] = useState<{ label: string; value: string } | null>(null);
  const [copiedSnippet, setCopiedSnippet] = useState("");

  const adminOnlyClientPayload = { data: [], admin_only: true };
  const secretsAdminOnly = secretMasterKey?.error === "administrator only";

  const loadClients = () => {
    setMessage("loading");
    Promise.all([
      apiFetch(`/admin/api-clients`).then((response) => {
        if (response.ok) return response.json();
        if (response.status === 403) return adminOnlyClientPayload;
        return Promise.reject(new Error(`api clients ${response.status}`));
      }),
      apiFetch(`/modelhub/v1/clients`).then((response) => {
        if (response.ok) return response.json();
        if (response.status === 403) return adminOnlyClientPayload;
        return Promise.reject(new Error(`model hub clients ${response.status}`));
      }),
      apiFetch(`/admin/secrets`).then((response) => {
        if (response.ok) return response.json();
        if (response.status === 403) return { data: [], master_key: { configured: false, usable: false, scheme: "", error: "administrator only" } };
        return Promise.reject(new Error(`encrypted secrets ${response.status}`));
      })
    ])
      .then(([apiPayload, hubPayload, secretPayload]) => {
        const apiData = Array.isArray(apiPayload) ? apiPayload : apiPayload.data ?? [];
        const hubData = hubPayload.data ?? [];
        setApiClientsAdminOnly(!Array.isArray(apiPayload) && Boolean(apiPayload.admin_only));
        setModelHubClientsAdminOnly(Boolean(hubPayload.admin_only));
        setApiClients(apiData);
        setModelHubClients(hubData);
        setApiClientCidrs(Object.fromEntries(apiData.map((client: ApiClient) => [client.id, (client.cidr_allowlist ?? []).join(", ")])));
        setModelHubClientCidrs(Object.fromEntries(hubData.map((client: ModelHubClient) => [client.id, (client.cidr_allowlist ?? []).join(", ")])));
        setModelHubClientAllowedModels(Object.fromEntries(hubData.map((client: ModelHubClient) => [client.id, (client.allowed_models ?? []).join(", ")])));
        setModelHubClientDownloads(Object.fromEntries(hubData.map((client: ModelHubClient) => [client.id, Boolean(client.allow_downloads)])));
        setEncryptedSecrets(secretPayload.data ?? []);
        setSecretMasterKey(secretPayload.master_key ?? null);
        setMessage("ready");
      })
      .catch((err: Error) => setMessage(err.message));
  };

  useEffect(loadClients, []);

  const createApiClient = (event: React.FormEvent) => {
    event.preventDefault();
    if (apiClientsAdminOnly) {
      setMessage("API client management requires administrator role");
      return;
    }
    setBusy(true);
    setMessage("creating");
    apiFetch(`/admin/api-clients`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        display_name: apiDisplayName,
        role: apiRole,
        scopes: apiScopes.trim() ? parseCsv(apiScopes) : null,
        cidr_allowlist: parseCsv(apiCidrs)
      })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setOneTimeKey({ label: payload.display_name, value: payload.api_key });
        setApiDisplayName("");
        setApiCidrs("");
        loadClients();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const createModelHubClient = (event: React.FormEvent) => {
    event.preventDefault();
    if (modelHubClientsAdminOnly) {
      setMessage("Model Hub client management requires administrator role");
      return;
    }
    setBusy(true);
    setMessage("creating");
    apiFetch(`/modelhub/v1/clients`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        display_name: hubDisplayName,
        allowed_models: parseCsv(hubAllowedModels, ["*"]),
        cidr_allowlist: parseCsv(hubCidrs),
        allow_downloads: hubAllowDownloads
      })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setOneTimeKey({ label: payload.display_name, value: payload.api_key });
        setHubDisplayName("");
        loadClients();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const updateClientCidrs = (kind: "api" | "modelhub", id: string) => {
    if ((kind === "api" && apiClientsAdminOnly) || (kind === "modelhub" && modelHubClientsAdminOnly)) {
      setMessage("credential management requires administrator role");
      return;
    }
    setBusy(true);
    setMessage("updating CIDR allowlist");
    const value = kind === "api" ? apiClientCidrs[id] ?? "" : modelHubClientCidrs[id] ?? "";
    const url = kind === "api" ? `/admin/api-clients/${id}/cidr-allowlist` : `/modelhub/v1/clients/${id}/cidr-allowlist`;
    apiJson(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cidr_allowlist: parseCsv(value) })
    })
      .then(() => {
        setMessage("CIDR allowlist updated");
        loadClients();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const updateModelHubClientPolicy = (id: string) => {
    if (modelHubClientsAdminOnly) {
      setMessage("Model Hub client management requires administrator role");
      return;
    }
    setBusy(true);
    setMessage("updating Model Hub policy");
    apiJson(`/modelhub/v1/clients/${id}/policy`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        allowed_models: parseCsv(modelHubClientAllowedModels[id] ?? "", ["*"]),
        allow_downloads: modelHubClientDownloads[id] ?? false
      })
    })
      .then(() => {
        setMessage("Model Hub policy updated");
        loadClients();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const saveSecret = (event: React.FormEvent) => {
    event.preventDefault();
    if (secretsAdminOnly) {
      setMessage("encrypted secret management requires administrator role");
      return;
    }
    setBusy(true);
    setMessage("storing encrypted value");
    apiJson<EncryptedSecret>(`/admin/secrets/${encodeURIComponent(secretName.trim())}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        display_name: secretDisplayName,
        category: secretCategory,
        description: secretDescription,
        value: secretValue
      })
    })
      .then(() => {
        setSecretName("");
        setSecretDisplayName("");
        setSecretDescription("");
        setSecretValue("");
        setMessage("encrypted value stored");
        loadClients();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const verifySecret = (name: string) => {
    if (secretsAdminOnly) {
      setMessage("encrypted secret management requires administrator role");
      return;
    }
    setBusy(true);
    setMessage("verifying encrypted value");
    apiJson(`/admin/secrets/${encodeURIComponent(name)}/verify`, { method: "POST" })
      .then(() => setMessage("verified"))
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const deleteSecret = (name: string) => {
    if (secretsAdminOnly) {
      setMessage("encrypted secret management requires administrator role");
      return;
    }
    setBusy(true);
    setMessage("deleting encrypted value");
    apiJson(`/admin/secrets/${encodeURIComponent(name)}`, { method: "DELETE" })
      .then(() => {
        setMessage("deleted");
        loadClients();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const revoke = (kind: "api" | "modelhub", id: string) => {
    if ((kind === "api" && apiClientsAdminOnly) || (kind === "modelhub" && modelHubClientsAdminOnly)) {
      setMessage("credential management requires administrator role");
      return;
    }
    setBusy(true);
    const url = kind === "api" ? `${API_BASE}/admin/api-clients/${id}` : `${API_BASE}/modelhub/v1/clients/${id}`;
    apiFetch(url, { method: "DELETE" })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then(() => {
        setMessage("revoked");
        loadClients();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const copySnippet = (snippet: AccessSnippet) => {
    if (!navigator.clipboard) {
      setMessage("clipboard is unavailable");
      return;
    }
    navigator.clipboard.writeText(snippet.code)
      .then(() => {
        setCopiedSnippet(snippet.id);
        setMessage(`copied ${snippet.label}`);
      })
      .catch((err: Error) => setMessage(err.message));
  };

  return (
    <section className="panel wide">
      <SectionTitle icon={<KeyRound size={18} />} title="External Access" />
      <div className="toolbar">
        <button title="Refresh clients" onClick={loadClients} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <span className="toolbar-status">{message}</span>
      </div>
      {oneTimeKey && (
        <div className="one-time-key">
          <strong>One-time key for {oneTimeKey.label}</strong>
          <code>{oneTimeKey.value}</code>
        </div>
      )}
      {(apiClientsAdminOnly || modelHubClientsAdminOnly || secretsAdminOnly) && (
        <div className="access-limited">
          <ShieldCheck size={16} />
          <span>Credential management requires an administrator role.</span>
        </div>
      )}
      <div className="split">
        <form className="stack" onSubmit={createApiClient}>
          <h3>API Clients</h3>
          <label>Display name<input value={apiDisplayName} onChange={(event) => setApiDisplayName(event.target.value)} required maxLength={256} disabled={apiClientsAdminOnly} /></label>
          <label>Role
            <select value={apiRole} onChange={(event) => setApiRole(event.target.value)} disabled={apiClientsAdminOnly}>
              {["service", "user", "creator", "operator", "admin"].map((role) => <option key={role} value={role}>{role}</option>)}
            </select>
          </label>
          <label>Scopes<input value={apiScopes} onChange={(event) => setApiScopes(event.target.value)} disabled={apiClientsAdminOnly} /></label>
          <label>CIDR allowlist<input value={apiCidrs} onChange={(event) => setApiCidrs(event.target.value)} placeholder="192.168.2.0/24" disabled={apiClientsAdminOnly} /></label>
          <button title="Create API client" disabled={busy || apiClientsAdminOnly || !apiDisplayName.trim()}><KeyRound size={16} />Create</button>
        </form>
        <form className="stack" onSubmit={createModelHubClient}>
          <h3>Model Hub Clients</h3>
          <label>Display name<input value={hubDisplayName} onChange={(event) => setHubDisplayName(event.target.value)} required maxLength={256} disabled={modelHubClientsAdminOnly} /></label>
          <label>Allowed models<input value={hubAllowedModels} onChange={(event) => setHubAllowedModels(event.target.value)} disabled={modelHubClientsAdminOnly} /></label>
          <label>CIDR allowlist<input value={hubCidrs} onChange={(event) => setHubCidrs(event.target.value)} disabled={modelHubClientsAdminOnly} /></label>
          <label className="inline-check"><input type="checkbox" checked={hubAllowDownloads} onChange={(event) => setHubAllowDownloads(event.target.checked)} disabled={modelHubClientsAdminOnly} />Downloads</label>
          <button title="Create Model Hub client" disabled={busy || modelHubClientsAdminOnly || !hubDisplayName.trim()}><Archive size={16} />Create</button>
        </form>
      </div>
      <div className="subsection-title">
        <TerminalSquare size={16} />
        <h3>Client Snippets</h3>
      </div>
      <div className="snippet-grid">
        {accessSnippets().map((snippet) => (
          <section className="snippet-card" key={snippet.id}>
            <div className="snippet-title">
              <h4>{snippet.label}</h4>
              <button title={`Copy ${snippet.label} snippet`} type="button" onClick={() => copySnippet(snippet)}>
                <Clipboard size={16} />{copiedSnippet === snippet.id ? "Copied" : "Copy"}
              </button>
            </div>
            <pre>{snippet.code}</pre>
          </section>
        ))}
      </div>
      <div className="subsection-title">
        <ShieldCheck size={16} />
        <h3>Encrypted Secrets</h3>
      </div>
      <div className="toolbar">
        <span className="toolbar-status">
          master key {secretMasterKey?.usable ? `ready ${secretMasterKey.key_id}` : secretMasterKey?.error ?? "not ready"}
        </span>
      </div>
      <form className="inline-form" onSubmit={saveSecret}>
        <input placeholder="name" value={secretName} onChange={(event) => setSecretName(event.target.value)} required maxLength={128} disabled={secretsAdminOnly} />
        <input placeholder="display name" value={secretDisplayName} onChange={(event) => setSecretDisplayName(event.target.value)} required maxLength={256} disabled={secretsAdminOnly} />
        <select value={secretCategory} onChange={(event) => setSecretCategory(event.target.value as SecretCategory)} disabled={secretsAdminOnly}>
          {["remote-provider", "model-download", "runtime", "integration", "other"].map((category) => <option key={category} value={category}>{category}</option>)}
        </select>
        <input placeholder="description" value={secretDescription} onChange={(event) => setSecretDescription(event.target.value)} maxLength={2048} disabled={secretsAdminOnly} />
        <input type="password" placeholder="value" value={secretValue} onChange={(event) => setSecretValue(event.target.value)} required maxLength={65536} disabled={secretsAdminOnly} />
        <button title="Store encrypted value" disabled={busy || secretsAdminOnly || !secretName.trim() || !secretDisplayName.trim() || !secretValue}><ShieldCheck size={16} />Store</button>
      </form>
      <table>
        <thead><tr><th>Name</th><th>Category</th><th>Envelope</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          {encryptedSecrets.map((item) => (
            <tr key={item.name}>
              <td><code>{item.display_name}</code><small>{item.name}{item.description ? ` / ${item.description}` : ""}</small></td>
              <td>{item.category}</td>
              <td>{item.scheme ?? "unknown"}<small>{item.key_id ?? "no key id"}</small></td>
              <td>{item.deleted_at ? "deleted" : item.encrypted ? "encrypted" : "empty"}<small>{item.updated_at ? new Date(item.updated_at).toLocaleString() : ""}</small></td>
              <td>
                <div className="table-actions">
                  <button title={`Verify ${item.display_name}`} onClick={() => verifySecret(item.name)} disabled={busy || secretsAdminOnly || Boolean(item.deleted_at)}><CheckCircle2 size={16} /></button>
                  <button title={`Delete ${item.display_name}`} onClick={() => deleteSecret(item.name)} disabled={busy || secretsAdminOnly || Boolean(item.deleted_at)}><Trash2 size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!encryptedSecrets.length && <tr><td colSpan={5}>{secretsAdminOnly ? "Administrator role required to view encrypted values" : "No encrypted values recorded"}</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <KeyRound size={16} />
        <h3>API Clients</h3>
      </div>
      <table>
        <thead><tr><th>Name</th><th>Role</th><th>Scopes</th><th>CIDR</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          {apiClients.map((client) => (
            <tr key={client.id}>
              <td><code>{client.display_name}</code><small>{client.id} / {client.key_prefix}</small></td>
              <td>{client.role}</td>
              <td>{client.scopes.join(", ")}</td>
              <td>
                <div className="inline-form">
                  <input
                    value={apiClientCidrs[client.id] ?? ""}
                    onChange={(event) => setApiClientCidrs((current) => ({ ...current, [client.id]: event.target.value }))}
                    disabled={busy || apiClientsAdminOnly || Boolean(client.revoked_at)}
                  />
                  <button title={`Save CIDR allowlist for ${client.display_name}`} onClick={() => updateClientCidrs("api", client.id)} disabled={busy || apiClientsAdminOnly || Boolean(client.revoked_at)}><CheckCircle2 size={16} /></button>
                </div>
              </td>
              <td>{client.revoked_at ? "revoked" : "active"}<small>{client.last_used_at ? `last ${new Date(client.last_used_at).toLocaleString()}` : ""}</small></td>
              <td><div className="table-actions"><button title={`Revoke ${client.display_name}`} onClick={() => revoke("api", client.id)} disabled={busy || apiClientsAdminOnly || Boolean(client.revoked_at)}><Trash2 size={16} /></button></div></td>
            </tr>
          ))}
          {!apiClients.length && <tr><td colSpan={6}>{apiClientsAdminOnly ? "Administrator role required to view API clients" : "No API clients recorded"}</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <Archive size={16} />
        <h3>Model Hub Clients</h3>
      </div>
      <table>
        <thead><tr><th>Name</th><th>Allowed Models</th><th>Downloads</th><th>CIDR</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          {modelHubClients.map((client) => (
            <tr key={client.id}>
              <td><code>{client.display_name}</code><small>{client.id} / {client.key_prefix}</small></td>
              <td>
                <div className="inline-form">
                  <input
                    value={modelHubClientAllowedModels[client.id] ?? ""}
                    onChange={(event) => setModelHubClientAllowedModels((current) => ({ ...current, [client.id]: event.target.value }))}
                    disabled={busy || modelHubClientsAdminOnly || Boolean(client.revoked_at)}
                  />
                  <button title={`Save Model Hub policy for ${client.display_name}`} onClick={() => updateModelHubClientPolicy(client.id)} disabled={busy || modelHubClientsAdminOnly || Boolean(client.revoked_at)}><CheckCircle2 size={16} /></button>
                </div>
              </td>
              <td>
                <label className="inline-check">
                  <input
                    type="checkbox"
                    checked={modelHubClientDownloads[client.id] ?? false}
                    onChange={(event) => setModelHubClientDownloads((current) => ({ ...current, [client.id]: event.target.checked }))}
                    disabled={busy || modelHubClientsAdminOnly || Boolean(client.revoked_at)}
                  />
                  enabled
                </label>
              </td>
              <td>
                <div className="inline-form">
                  <input
                    value={modelHubClientCidrs[client.id] ?? ""}
                    onChange={(event) => setModelHubClientCidrs((current) => ({ ...current, [client.id]: event.target.value }))}
                    disabled={busy || modelHubClientsAdminOnly || Boolean(client.revoked_at)}
                  />
                  <button title={`Save CIDR allowlist for ${client.display_name}`} onClick={() => updateClientCidrs("modelhub", client.id)} disabled={busy || modelHubClientsAdminOnly || Boolean(client.revoked_at)}><CheckCircle2 size={16} /></button>
                </div>
              </td>
              <td>{client.revoked_at ? "revoked" : "active"}<small>{client.allow_downloads ? "downloads" : "catalog only"}</small></td>
              <td><div className="table-actions"><button title={`Revoke ${client.display_name}`} onClick={() => revoke("modelhub", client.id)} disabled={busy || modelHubClientsAdminOnly || Boolean(client.revoked_at)}><Trash2 size={16} /></button></div></td>
            </tr>
          ))}
          {!modelHubClients.length && <tr><td colSpan={6}>{modelHubClientsAdminOnly ? "Administrator role required to view Model Hub clients" : "No Model Hub clients recorded"}</td></tr>}
        </tbody>
      </table>
    </section>
  );
}

function formatBytes(value?: number) {
  if (!value) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatArchiveInspection(inspection: ModelInstallPlan["archive_inspections"][number]) {
  const summary = inspection.summary;
  if (inspection.inspection_status === "safe" && summary) {
    return `${inspection.path}: ${inspection.archive_format} safe, ${summary.file_count} files, ${formatBytes(summary.total_uncompressed_bytes)}`;
  }
  return `${inspection.path}: ${inspection.archive_format} ${inspection.inspection_status}${inspection.error ? ` (${inspection.error})` : ""}${inspection.reason ? ` (${inspection.reason})` : ""}`;
}

function Storage() {
  const [backups, setBackups] = useState<BackupSummary[]>([]);
  const [selectedBackupManifest, setSelectedBackupManifest] = useState<BackupManifest | null>(null);
  const [retentionPlan, setRetentionPlan] = useState<BackupRetentionPlan | null>(null);
  const [artifactRetentionPlan, setArtifactRetentionPlan] = useState<ArtifactRetentionPlan | null>(null);
  const [modelQuarantinePlan, setModelQuarantinePlan] = useState<ModelQuarantineRetentionPlan | null>(null);
  const [admissionReport, setAdmissionReport] = useState<AdmissionReport | null>(null);
  const [backupSchedule, setBackupSchedule] = useState<BackupSchedule | null>(null);
  const [keepLast, setKeepLast] = useState("5");
  const [deleteOlderThanDays, setDeleteOlderThanDays] = useState("");
  const [artifactDeleteOlderThanDays, setArtifactDeleteOlderThanDays] = useState("30");
  const [artifactNamespaces, setArtifactNamespaces] = useState("");
  const [artifactLimit, setArtifactLimit] = useState("5000");
  const [modelQuarantineDeleteOlderThanDays, setModelQuarantineDeleteOlderThanDays] = useState("30");
  const [modelQuarantineLimit, setModelQuarantineLimit] = useState("5000");
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleIntervalHours, setScheduleIntervalHours] = useState("24");
  const [scheduleKeepLast, setScheduleKeepLast] = useState("7");
  const [scheduleDeleteOlderThanDays, setScheduleDeleteOlderThanDays] = useState("30");
  const [scheduleLabelPrefix, setScheduleLabelPrefix] = useState("scheduled");
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);

  const applySchedule = (payload: BackupSchedule) => {
    setBackupSchedule(payload);
    setScheduleEnabled(payload.enabled);
    setScheduleIntervalHours(String(payload.interval_hours));
    setScheduleKeepLast(String(payload.keep_last));
    setScheduleDeleteOlderThanDays(payload.delete_older_than_days ? String(payload.delete_older_than_days) : "");
    setScheduleLabelPrefix(payload.label_prefix);
  };

  const loadBackups = () => {
    apiFetch(`/admin/backups`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`${response.status}`)))
      .then((payload) => setBackups(payload.data ?? []))
      .catch((err: Error) => setMessage(`backup list ${err.message}`));
  };

  const loadSchedule = () => {
    apiJson<BackupSchedule>(`/admin/backups/schedule`)
      .then(applySchedule)
      .catch((err: Error) => setMessage(`schedule ${err.message}`));
  };

  const loadAdmission = () => {
    apiJson<AdmissionReport>(`/admin/admission`)
      .then(setAdmissionReport)
      .catch((err: Error) => setMessage(`admission ${err.message}`));
  };

  useEffect(() => {
    loadBackups();
    loadSchedule();
    loadAdmission();
  }, []);

  const runAction = (action: "create" | "verify" | "restore-test" | "postgres-import", backup?: BackupSummary) => {
    setBusy(true);
    setMessage(action);
    const url = action === "create"
      ? `${API_BASE}/admin/backups`
      : `${API_BASE}/admin/backups/${encodeURIComponent(backup?.name ?? "")}/${action}`;
    const body = action === "create"
      ? {}
      : action === "postgres-import"
        ? { apply: false }
        : { force: false };
    apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setMessage(payload.status ? `${payload.status} ${payload.backup ?? payload.name}` : `created ${payload.name}`);
        loadBackups();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const inspectBackupManifest = (backup: BackupSummary) => {
    setBusy(true);
    setMessage(`loading ${backup.name} manifest`);
    apiJson<BackupManifest>(`/admin/backups/${encodeURIComponent(backup.name)}/manifest`)
      .then((payload) => {
        setSelectedBackupManifest(payload);
        setMessage(`loaded ${backup.name} manifest`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const retentionPayload = (confirm: boolean) => {
    const parsedKeepLast = Number.parseInt(keepLast, 10);
    const parsedAge = deleteOlderThanDays.trim() ? Number.parseInt(deleteOlderThanDays, 10) : null;
    return {
      keep_last: Number.isFinite(parsedKeepLast) ? parsedKeepLast : 5,
      delete_older_than_days: parsedAge && Number.isFinite(parsedAge) ? parsedAge : null,
      confirm
    };
  };

  const runRetention = (apply: boolean) => {
    setBusy(true);
    setMessage(apply ? "cleanup" : "retention plan");
    apiFetch(`/admin/backups/${apply ? "cleanup" : "retention-plan"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(retentionPayload(apply))
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setRetentionPlan(payload);
        setMessage(`${payload.status} ${payload.candidate_count ?? payload.deleted_count ?? 0} candidate${(payload.candidate_count ?? payload.deleted_count) === 1 ? "" : "s"}`);
        loadBackups();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const artifactRetentionPayload = (confirm: boolean) => {
    const age = Number.parseInt(artifactDeleteOlderThanDays, 10);
    const limit = Number.parseInt(artifactLimit, 10);
    return {
      delete_older_than_days: Number.isFinite(age) ? age : 30,
      namespaces: artifactNamespaces.split(",").map((item) => item.trim()).filter(Boolean),
      limit: Number.isFinite(limit) ? limit : 5000,
      confirm
    };
  };

  const runArtifactRetention = (apply: boolean) => {
    setBusy(true);
    setMessage(apply ? "artifact cleanup" : "artifact retention plan");
    apiFetch(`/admin/artifacts/${apply ? "cleanup" : "retention-plan"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(artifactRetentionPayload(apply))
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload: ArtifactRetentionPlan) => {
        setArtifactRetentionPlan(payload);
        setMessage(`${payload.status} ${payload.candidate_count ?? payload.deleted_count ?? 0} artifact candidate${(payload.candidate_count ?? payload.deleted_count) === 1 ? "" : "s"}`);
        loadAdmission();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const modelQuarantineRetentionPayload = (confirm: boolean) => {
    const age = Number.parseInt(modelQuarantineDeleteOlderThanDays, 10);
    const limit = Number.parseInt(modelQuarantineLimit, 10);
    return {
      delete_older_than_days: Number.isFinite(age) ? age : 30,
      limit: Number.isFinite(limit) ? limit : 5000,
      confirm
    };
  };

  const runModelQuarantineRetention = (apply: boolean) => {
    setBusy(true);
    setMessage(apply ? "model quarantine cleanup" : "model quarantine plan");
    apiFetch(`/admin/models/quarantine/${apply ? "cleanup" : "retention-plan"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(modelQuarantineRetentionPayload(apply))
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload: ModelQuarantineRetentionPlan) => {
        setModelQuarantinePlan(payload);
        setMessage(`${payload.status} ${payload.candidate_count ?? payload.deleted_count ?? 0} quarantine candidate${(payload.candidate_count ?? payload.deleted_count) === 1 ? "" : "s"}`);
        loadAdmission();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const schedulePayload = (runImmediately = false) => {
    const interval = Number.parseInt(scheduleIntervalHours, 10);
    const keep = Number.parseInt(scheduleKeepLast, 10);
    const age = scheduleDeleteOlderThanDays.trim() ? Number.parseInt(scheduleDeleteOlderThanDays, 10) : null;
    return {
      enabled: scheduleEnabled,
      interval_hours: Number.isFinite(interval) ? interval : 24,
      keep_last: Number.isFinite(keep) ? keep : 7,
      delete_older_than_days: age && Number.isFinite(age) ? age : null,
      label_prefix: scheduleLabelPrefix,
      run_immediately: runImmediately
    };
  };

  const saveSchedule = (runImmediately = false) => {
    setBusy(true);
    setMessage(runImmediately ? "saving schedule" : "saving schedule");
    apiJson<BackupSchedule>(`/admin/backups/schedule`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(schedulePayload(runImmediately))
    })
      .then((payload) => {
        applySchedule(payload);
        setMessage(payload.enabled ? "schedule saved" : "schedule disabled");
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const runScheduleNow = () => {
    setBusy(true);
    setMessage("scheduled backup");
    apiJson<{ status: string; backup: BackupSummary; retention: BackupRetentionPlan; schedule: BackupSchedule }>(`/admin/backups/schedule/run`, { method: "POST" })
      .then((payload) => {
        applySchedule(payload.schedule);
        setRetentionPlan(payload.retention);
        setMessage(`created ${payload.backup.name}`);
        loadBackups();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const manifestFiles = selectedBackupManifest?.files ?? [];
  const sensitiveManifestFiles = manifestFiles.filter((file) => file.sensitive).slice(0, 10);
  const largestManifestFiles = [...manifestFiles].sort((left, right) => (right.size_bytes ?? 0) - (left.size_bytes ?? 0)).slice(0, 10);
  const manifestPostgresDumps = selectedBackupManifest
    ? selectedBackupManifest.postgres_dumps?.length
      ? selectedBackupManifest.postgres_dumps
      : selectedBackupManifest.postgres_dump
        ? [selectedBackupManifest.postgres_dump]
        : []
    : [];

  return (
    <section className="panel wide">
      <SectionTitle icon={<HardDrive size={18} />} title="Storage" />
      <div className="toolbar job-filters">
        <button title="Refresh backups" onClick={loadBackups} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <button title="Refresh admission limits" onClick={loadAdmission} disabled={busy}><Gauge size={16} />Limits</button>
        <button title="Create backup" onClick={() => runAction("create")} disabled={busy}><Archive size={16} />Backup</button>
        <label>Keep<input aria-label="Backups to keep" inputMode="numeric" value={keepLast} onChange={(event) => setKeepLast(event.target.value)} /></label>
        <label>Older than<input aria-label="Delete older than days" inputMode="numeric" placeholder="days" value={deleteOlderThanDays} onChange={(event) => setDeleteOlderThanDays(event.target.value)} /></label>
        <button title="Plan backup cleanup" onClick={() => runRetention(false)} disabled={busy}><ListChecks size={16} />Plan</button>
        <button title="Apply backup cleanup" onClick={() => runRetention(true)} disabled={busy || !retentionPlan?.candidate_count}><Trash2 size={16} />Cleanup</button>
        <span className="toolbar-status">{message}</span>
      </div>
      <div className="subsection-title">
        <RefreshCw size={16} />
        <h3>Backup Schedule</h3>
      </div>
      <div className="toolbar job-filters">
        <label className="inline-check"><input type="checkbox" checked={scheduleEnabled} onChange={(event) => setScheduleEnabled(event.target.checked)} />Enabled</label>
        <label>Every<input aria-label="Schedule interval hours" inputMode="numeric" value={scheduleIntervalHours} onChange={(event) => setScheduleIntervalHours(event.target.value)} /></label>
        <label>Keep<input aria-label="Scheduled backups to keep" inputMode="numeric" value={scheduleKeepLast} onChange={(event) => setScheduleKeepLast(event.target.value)} /></label>
        <label>Older than<input aria-label="Scheduled cleanup age" inputMode="numeric" placeholder="days" value={scheduleDeleteOlderThanDays} onChange={(event) => setScheduleDeleteOlderThanDays(event.target.value)} /></label>
        <label>Prefix<input aria-label="Scheduled backup prefix" value={scheduleLabelPrefix} onChange={(event) => setScheduleLabelPrefix(event.target.value)} maxLength={64} /></label>
        <button title="Save backup schedule" onClick={() => saveSchedule(false)} disabled={busy}><ShieldCheck size={16} />Save</button>
        <button title="Run scheduled backup now" onClick={runScheduleNow} disabled={busy}><Archive size={16} />Run</button>
        <span className="toolbar-status">
          {backupSchedule ? `${backupSchedule.source} / ${backupSchedule.last_status ?? "idle"}${backupSchedule.next_run_at ? ` / next ${new Date(backupSchedule.next_run_at).toLocaleString()}` : ""}` : "schedule loading"}
        </span>
      </div>
      {backupSchedule?.failure_message && <div className="one-time-key"><strong>Schedule failure</strong><small>{backupSchedule.failure_message}</small></div>}
      {retentionPlan && (
        <div className="one-time-key">
          <strong>Backup retention {retentionPlan.status}</strong>
          <span>{retentionPlan.candidate_count} candidate{retentionPlan.candidate_count === 1 ? "" : "s"} / {formatBytes(retentionPlan.total_reclaimable_bytes)} reclaimable</span>
          <small>Keep {retentionPlan.policy.keep_last}{retentionPlan.policy.delete_older_than_days ? ` / older than ${retentionPlan.policy.delete_older_than_days} days` : ""}</small>
          {retentionPlan.candidates.length > 0 && <small>{retentionPlan.candidates.map((candidate) => `${candidate.name}: ${candidate.reason}`).join(" / ")}</small>}
          {Boolean(retentionPlan.deleted?.length) && <small>{retentionPlan.deleted?.map((item) => `deleted ${item.name}`).join(" / ")}</small>}
          {retentionPlan.invalid_preserved_count > 0 && <small>{retentionPlan.invalid_preserved_count} invalid entr{retentionPlan.invalid_preserved_count === 1 ? "y" : "ies"} preserved</small>}
        </div>
      )}
      <div className="subsection-title">
        <Trash2 size={16} />
        <h3>Generated Artifacts</h3>
      </div>
      {admissionReport && (
        <div className="metrics compact">
          <Metric label="Owner queued" value={formatCount(admissionReport.queue?.owner_queued_jobs)} detail={`limit ${formatCount(admissionReport.policy.max_queued_jobs_per_owner)}`} />
          <Metric label="Owner active" value={formatCount(admissionReport.queue?.owner_active_jobs)} detail={`limit ${formatCount(admissionReport.policy.max_active_jobs_per_owner)}`} />
          <Metric label="Jobs/hour" value={formatCount(admissionReport.queue?.owner_jobs_last_hour)} detail={`limit ${formatCount(admissionReport.policy.max_jobs_per_hour_per_owner)}`} />
          <Metric label="Global queued" value={formatCount(admissionReport.queue?.global_queued_jobs)} detail={`limit ${formatCount(admissionReport.policy.max_queued_jobs_global)}`} />
          <Metric label="Artifact bytes" value={formatHostBytes(admissionReport.storage.artifact_bytes)} detail={admissionReport.policy.artifact_storage_max_bytes ? `cap ${formatHostBytes(admissionReport.policy.artifact_storage_max_bytes)}` : "cap disabled"} />
          <Metric label="Filesystem free" value={formatHostBytes(admissionReport.storage.disk_free_bytes)} detail={`reserve ${formatHostBytes(admissionReport.policy.artifact_storage_reserve_bytes)}`} />
        </div>
      )}
      <div className="toolbar job-filters">
        <label>Older than<input aria-label="Delete artifacts older than days" inputMode="numeric" value={artifactDeleteOlderThanDays} onChange={(event) => setArtifactDeleteOlderThanDays(event.target.value)} /></label>
        <label>Namespaces<input aria-label="Artifact namespaces" placeholder="localai,comfyui" value={artifactNamespaces} onChange={(event) => setArtifactNamespaces(event.target.value)} /></label>
        <label>Scan<input aria-label="Artifact cleanup scan limit" inputMode="numeric" value={artifactLimit} onChange={(event) => setArtifactLimit(event.target.value)} /></label>
        <button title="Plan artifact cleanup" onClick={() => runArtifactRetention(false)} disabled={busy}><ListChecks size={16} />Plan</button>
        <button title="Apply artifact cleanup" onClick={() => runArtifactRetention(true)} disabled={busy || artifactRetentionPlan?.status !== "planned" || !artifactRetentionPlan?.candidate_count}><Trash2 size={16} />Cleanup</button>
      </div>
      {artifactRetentionPlan && (
        <div className="one-time-key">
          <strong>Artifact retention {artifactRetentionPlan.status}</strong>
          <span>{artifactRetentionPlan.candidate_count} candidate{artifactRetentionPlan.candidate_count === 1 ? "" : "s"} / {formatBytes(artifactRetentionPlan.total_reclaimable_bytes)} reclaimable</span>
          <small>Older than {artifactRetentionPlan.policy.delete_older_than_days} days{artifactRetentionPlan.policy.namespaces?.length ? ` / ${artifactRetentionPlan.policy.namespaces.join(", ")}` : ""}</small>
          {artifactRetentionPlan.candidates.length > 0 && <small>{artifactRetentionPlan.candidates.slice(0, 6).map((candidate) => `${candidate.path}: ${formatBytes(candidate.size_bytes)}`).join(" / ")}</small>}
          {Boolean(artifactRetentionPlan.deleted?.length) && <small>{artifactRetentionPlan.deleted?.slice(0, 6).map((item) => `deleted ${item.path}`).join(" / ")}</small>}
          {artifactRetentionPlan.invalid_preserved_count > 0 && <small>{artifactRetentionPlan.invalid_preserved_count} invalid artifact entr{artifactRetentionPlan.invalid_preserved_count === 1 ? "y" : "ies"} preserved</small>}
          {artifactRetentionPlan.job_update_count !== undefined && <small>{artifactRetentionPlan.job_update_count} job record{artifactRetentionPlan.job_update_count === 1 ? "" : "s"} updated</small>}
        </div>
      )}
      <div className="subsection-title">
        <Database size={16} />
        <h3>Model Quarantine</h3>
      </div>
      <div className="toolbar job-filters">
        <label>Older than<input aria-label="Delete model quarantine older than days" inputMode="numeric" value={modelQuarantineDeleteOlderThanDays} onChange={(event) => setModelQuarantineDeleteOlderThanDays(event.target.value)} /></label>
        <label>Scan<input aria-label="Model quarantine cleanup scan limit" inputMode="numeric" value={modelQuarantineLimit} onChange={(event) => setModelQuarantineLimit(event.target.value)} /></label>
        <button title="Plan model quarantine cleanup" onClick={() => runModelQuarantineRetention(false)} disabled={busy}><ListChecks size={16} />Plan</button>
        <button title="Apply model quarantine cleanup" onClick={() => runModelQuarantineRetention(true)} disabled={busy || modelQuarantinePlan?.status !== "planned" || !modelQuarantinePlan?.candidate_count}><Trash2 size={16} />Cleanup</button>
      </div>
      {modelQuarantinePlan && (
        <div className="one-time-key">
          <strong>Model quarantine {modelQuarantinePlan.status}</strong>
          <span>{modelQuarantinePlan.candidate_count} candidate{modelQuarantinePlan.candidate_count === 1 ? "" : "s"} / {formatBytes(modelQuarantinePlan.total_reclaimable_bytes)} reclaimable</span>
          <small>Older than {modelQuarantinePlan.policy.delete_older_than_days} days / root {modelQuarantinePlan.root}</small>
          {modelQuarantinePlan.candidates.length > 0 && <small>{modelQuarantinePlan.candidates.slice(0, 6).map((candidate) => `${candidate.model_id ?? "model"} ${candidate.quarantine_set ?? candidate.path}: ${formatBytes(candidate.size_bytes)}`).join(" / ")}</small>}
          {Boolean(modelQuarantinePlan.deleted?.length) && <small>{modelQuarantinePlan.deleted?.slice(0, 6).map((item) => `deleted ${item.quarantine_set ?? item.path}`).join(" / ")}</small>}
          {modelQuarantinePlan.invalid_preserved_count > 0 && <small>{modelQuarantinePlan.invalid_preserved_count} invalid quarantine entr{modelQuarantinePlan.invalid_preserved_count === 1 ? "y" : "ies"} preserved</small>}
          {modelQuarantinePlan.truncated && <small>Scan limit reached; increase the limit and plan again for remaining entries</small>}
        </div>
      )}
      <table>
        <thead><tr><th>Backup</th><th>Files</th><th>Archive</th><th>Flags</th><th>Actions</th></tr></thead>
        <tbody>
          {backups.map((backup) => (
            <tr key={backup.name}>
              <td><code>{backup.name}</code><small>{backup.created_at ?? backup.status ?? ""}</small></td>
              <td>{backup.file_count ?? 0}</td>
              <td>{formatBytes(backup.archive?.size_bytes)}</td>
              <td>
                {backup.contains_sensitive_data ? "sensitive" : "standard"} / {backup.postgres_dump_included ? "db dump" : "fs only"}
                <small>{backup.postgres_native_dump ? "native pg_dump" : (backup.postgres_dumps?.length ? backup.postgres_dumps.map((dump) => dump.kind ?? dump.format).join(", ") : "")}</small>
                {backup.archive_encryption && <small>{backup.archive_encryption.mode === "encrypted-only" ? "encrypted-only archive" : "encrypted archive copy"}</small>}
              </td>
              <td>
                <div className="table-actions">
                  <button title={`Inspect manifest for ${backup.name}`} onClick={() => inspectBackupManifest(backup)} disabled={busy || backup.status === "invalid"}><ScrollText size={16} /></button>
                  <button title={`Verify ${backup.name}`} onClick={() => runAction("verify", backup)} disabled={busy || backup.status === "invalid"}><CheckCircle2 size={16} /></button>
                  <button title={`Restore-test ${backup.name}`} onClick={() => runAction("restore-test", backup)} disabled={busy || backup.status === "invalid"}><RotateCcw size={16} /></button>
                  <button title={`Plan DB import for ${backup.name}`} onClick={() => runAction("postgres-import", backup)} disabled={busy || backup.status === "invalid" || !backup.postgres_dump_included}><Database size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!backups.length && <tr><td colSpan={5}>No backups recorded</td></tr>}
        </tbody>
      </table>
      {selectedBackupManifest && (
        <div className="backup-manifest-detail">
          <div className="subsection-title">
            <ScrollText size={16} />
            <h3>Backup Manifest Detail</h3>
          </div>
          <div className="backup-manifest-grid">
            <div><strong>Backup</strong><small>{selectedBackupManifest.name}</small></div>
            <div><strong>Created</strong><small>{formatDateTime(selectedBackupManifest.created_at)}</small></div>
            <div><strong>Files</strong><small>{manifestFiles.length}</small></div>
            <div><strong>Archive</strong><small>{formatBytes(selectedBackupManifest.archive?.size_bytes)} / {selectedBackupManifest.archive?.sha256 ?? "no checksum"}</small></div>
            <div><strong>Sensitive data</strong><small>{selectedBackupManifest.contains_sensitive_data ? "present" : "not flagged"}</small></div>
            <div><strong>PostgreSQL dumps</strong><small>{manifestPostgresDumps.length}</small></div>
            <div><strong>Archive encryption</strong><small>{selectedBackupManifest.archive_encryption?.mode ?? "none"}</small></div>
            <div><strong>Old stack</strong><small>{selectedBackupManifest.preserve_old_stack === false ? "not included" : "preservation enabled"}</small></div>
          </div>
          <div className="manifest-section">
            <h4>PostgreSQL Dumps</h4>
            <table>
              <thead><tr><th>Kind</th><th>Format</th><th>Path</th><th>Status</th></tr></thead>
              <tbody>
                {manifestPostgresDumps.map((dump, index) => (
                  <tr key={`${dump.path ?? "dump"}-${index}`}>
                    <td>{dump.kind ?? "logical"}</td>
                    <td>{dump.format ?? "unknown"}</td>
                    <td>{dump.path ?? "not recorded"}</td>
                    <td>{dump.verified === false ? "unverified" : "recorded"}{dump.sensitive ? <small>sensitive</small> : null}</td>
                  </tr>
                ))}
                {!manifestPostgresDumps.length && <tr><td colSpan={4}>No PostgreSQL dump metadata recorded</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="manifest-section">
            <h4>Sensitive Files</h4>
            <table>
              <thead><tr><th>Path</th><th>Size</th><th>Checksum</th></tr></thead>
              <tbody>
                {sensitiveManifestFiles.map((file) => (
                  <tr key={file.path}>
                    <td>{file.path}</td>
                    <td>{formatBytes(file.size_bytes)}</td>
                    <td><code>{file.sha256 ?? ""}</code></td>
                  </tr>
                ))}
                {!sensitiveManifestFiles.length && <tr><td colSpan={3}>No sensitive file entries flagged</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="manifest-section">
            <h4>Largest Files</h4>
            <table>
              <thead><tr><th>Path</th><th>Size</th><th>Flags</th></tr></thead>
              <tbody>
                {largestManifestFiles.map((file) => (
                  <tr key={file.path}>
                    <td>{file.path}</td>
                    <td>{formatBytes(file.size_bytes)}</td>
                    <td>{file.sensitive ? "sensitive" : "standard"}</td>
                  </tr>
                ))}
                {!largestManifestFiles.length && <tr><td colSpan={3}>No file entries recorded</td></tr>}
              </tbody>
            </table>
          </div>
          <details className="manifest-raw">
            <summary>Raw Manifest JSON</summary>
            <pre>{JSON.stringify(selectedBackupManifest, null, 2)}</pre>
          </details>
        </div>
      )}
    </section>
  );
}

function System() {
  const [result, setResult] = useState<SelfTestResult | null>(null);
  const [acceptanceReports, setAcceptanceReports] = useState<AcceptanceReportSummary[]>([]);
  const [selectedAcceptanceReport, setSelectedAcceptanceReport] = useState<AcceptanceReportDetail | null>(null);
  const [acceptanceLabel, setAcceptanceLabel] = useState("");
  const [acceptanceNotes, setAcceptanceNotes] = useState("");
  const [acceptanceEvidence, setAcceptanceEvidence] = useState<AcceptanceEvidenceState>({ ...EMPTY_ACCEPTANCE_EVIDENCE });
  const [rollbackRehearsal, setRollbackRehearsal] = useState<RollbackRehearsalStatus | null>(null);
  const [rollbackRehearsedBy, setRollbackRehearsedBy] = useState("");
  const [rollbackNotes, setRollbackNotes] = useState("");
  const [rollbackCommandsTested, setRollbackCommandsTested] = useState(false);
  const [rollbackResourcesPreserved, setRollbackResourcesPreserved] = useState(false);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [resourcePolicy, setResourcePolicy] = useState<ResourcePolicyPayload | null>(null);
  const [policyForm, setPolicyForm] = useState<Record<string, string>>({});
  const [admissionPolicy, setAdmissionPolicy] = useState<AdmissionPolicyPayload | null>(null);
  const [admissionPolicyForm, setAdmissionPolicyForm] = useState<Record<string, string>>({});
  const [networkPolicy, setNetworkPolicy] = useState<NetworkPolicyPayload | null>(null);
  const [networkCorsOrigins, setNetworkCorsOrigins] = useState("");
  const [networkTrustedProxyCidrs, setNetworkTrustedProxyCidrs] = useState("");
  const [maintenance, setMaintenance] = useState<MaintenanceState | null>(null);
  const [maintenanceReason, setMaintenanceReason] = useState("");
  const [updates, setUpdates] = useState<UpdatePlan[]>([]);
  const [updateVersion, setUpdateVersion] = useState("0.2.0");
  const [updateSourceUrl, setUpdateSourceUrl] = useState("");
  const [updateNotes, setUpdateNotes] = useState("");
  const [updateImageRefs, setUpdateImageRefs] = useState(UPDATE_IMAGE_REFS_TEMPLATE);
  const [serviceLogService, setServiceLogService] = useState("control-plane");
  const [serviceLogLines, setServiceLogLines] = useState("100");
  const [serviceLogs, setServiceLogs] = useState<ServiceLogPayload | null>(null);
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);

  const setPolicyPayload = (payload: ResourcePolicyPayload) => {
    setResourcePolicy(payload);
    setPolicyForm(Object.fromEntries(RESOURCE_POLICY_FIELDS.map((field) => [field.key, String(payload.effective[field.key])])));
  };

  const loadResourcePolicy = () => {
    apiJson<ResourcePolicyPayload>(`/admin/resource-policy`)
      .then(setPolicyPayload)
      .catch(() => setResourcePolicy(null));
  };

  const setAdmissionPolicyPayload = (payload: AdmissionPolicyPayload) => {
    setAdmissionPolicy(payload);
    setAdmissionPolicyForm(Object.fromEntries(ADMISSION_POLICY_FIELDS.map((field) => [field.key, String(payload.effective[field.key])])));
  };

  const loadAdmissionPolicy = () => {
    apiJson<AdmissionPolicyPayload>(`/admin/admission-policy`)
      .then(setAdmissionPolicyPayload)
      .catch(() => setAdmissionPolicy(null));
  };

  const setNetworkPolicyPayload = (payload: NetworkPolicyPayload) => {
    setNetworkPolicy(payload);
    setNetworkCorsOrigins(payload.effective.cors_allow_origins.join(", "));
    setNetworkTrustedProxyCidrs(payload.effective.trusted_proxy_cidrs.join(", "));
  };

  const loadNetworkPolicy = () => {
    apiJson<NetworkPolicyPayload>(`/admin/network-policy`)
      .then(setNetworkPolicyPayload)
      .catch(() => setNetworkPolicy(null));
  };

  const loadMaintenance = () => {
    apiJson<MaintenanceState>(`/admin/maintenance`)
      .then((payload) => {
        setMaintenance(payload);
        setMaintenanceReason(payload.reason ?? "");
      })
      .catch(() => setMaintenance(null));
  };

  const loadAudit = () => {
    apiFetch(`/admin/audit-log?limit=20`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`${response.status}`)))
      .then((payload) => setAuditEvents(payload.data ?? []))
      .catch(() => setAuditEvents([]));
  };

  const loadUpdates = () => {
    apiJson<{ object: string; data: UpdatePlan[] }>(`/admin/updates?limit=10`)
      .then((payload) => setUpdates(payload.data ?? []))
      .catch(() => setUpdates([]));
  };

  const loadServiceLogs = () => {
    const parsedLines = Math.max(1, Math.min(500, Number.parseInt(serviceLogLines, 10) || 100));
    setServiceLogLines(String(parsedLines));
    setBusy(true);
    setMessage(`loading ${serviceLogService} logs`);
    apiJson<ServiceLogPayload>(`/admin/services/${encodeURIComponent(serviceLogService)}/logs?lines=${parsedLines}`)
      .then((payload) => {
        setServiceLogs(payload);
        setMessage(`${payload.service} logs ready`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const runSelfTest = () => {
    setBusy(true);
    setMessage("running");
    apiFetch(`/admin/self-test`)
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setResult(payload);
        setMessage(payload.status);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const loadAcceptanceReports = () => {
    apiJson<{ object: string; data: AcceptanceReportSummary[] }>(`/admin/acceptance-reports?limit=10`)
      .then((payload) => setAcceptanceReports(payload.data ?? []))
      .catch(() => setAcceptanceReports([]));
  };

  const loadRollbackRehearsal = () => {
    apiJson<RollbackRehearsalStatus>(`/admin/migration/rollback-rehearsal`)
      .then(setRollbackRehearsal)
      .catch(() => setRollbackRehearsal(null));
  };

  const inspectAcceptanceReport = (reportId: string) => {
    setBusy(true);
    setMessage(`loading ${reportId}`);
    apiJson<AcceptanceReportDetail>(`/admin/acceptance-reports/${encodeURIComponent(reportId)}`)
      .then((payload) => {
        setSelectedAcceptanceReport({ summary: payload.summary, report: detailRecord(payload.report) });
        setMessage(`loaded ${reportId}`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const downloadAcceptanceReport = (reportId: string, filename: AcceptanceReportFileName) => {
    setBusy(true);
    setMessage(`downloading ${filename}`);
    downloadAcceptanceReportFile(reportId, filename)
      .then((downloadName) => setMessage(`downloaded ${downloadName}`))
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const createAcceptanceReport = () => {
    setBusy(true);
    setMessage("creating acceptance report");
    apiJson<{ summary: AcceptanceReportSummary; report: unknown }>(`/admin/acceptance-reports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: acceptanceLabel.trim(),
        notes: acceptanceNotes.trim(),
        operator_evidence: acceptanceEvidence
      })
    })
      .then((payload) => {
        setMessage(`acceptance ${payload.summary.status}`);
        setAcceptanceReports((current) => [payload.summary, ...current.filter((item) => item.id !== payload.summary.id)].slice(0, 10));
        setSelectedAcceptanceReport({ summary: payload.summary, report: detailRecord(payload.report) });
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const createRollbackRehearsal = () => {
    setBusy(true);
    setMessage("creating rollback rehearsal");
    apiJson<RollbackRehearsalCreateResult>(`/admin/migration/rollback-rehearsal`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cutover_plan_name: rollbackRehearsal?.cutover_plan?.name ?? null,
        rehearsed_by: rollbackRehearsedBy.trim(),
        rollback_commands_tested: rollbackCommandsTested,
        old_resources_preserved: rollbackResourcesPreserved,
        notes: rollbackNotes.trim()
      })
    })
      .then((payload) => {
        setMessage(`rollback rehearsal ${payload.status}`);
        setRollbackCommandsTested(false);
        setRollbackResourcesPreserved(false);
        loadRollbackRehearsal();
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const resourcePolicyBody = (): ResourcePolicyValues => {
    const entries = RESOURCE_POLICY_FIELDS.map((field) => {
      const raw = policyForm[field.key] ?? "";
      const parsed = Number.parseFloat(raw);
      return [field.key, INTEGER_POLICY_FIELDS.has(field.key) ? Math.trunc(parsed) : parsed];
    });
    return Object.fromEntries(entries) as ResourcePolicyValues;
  };

  const runPolicyAction = (action: "validate" | "save" | "reset") => {
    setBusy(true);
    setMessage(action === "reset" ? "resetting policy" : `${action} policy`);
    const path = action === "validate" ? "/admin/resource-policy/validate" : "/admin/resource-policy";
    const init: RequestInit = action === "reset"
      ? { method: "DELETE" }
      : {
          method: action === "save" ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(resourcePolicyBody())
        };
    apiJson<any>(path, init)
      .then((payload) => {
        if (action === "validate") {
          setMessage(payload.accepted ? "policy accepted" : payload.errors.join("; "));
          return;
        }
        setPolicyPayload(payload);
        setMessage(`policy ${action === "save" ? "saved" : "reset"}`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const admissionPolicyBody = (): AdmissionPolicyValues => {
    return Object.fromEntries(
      ADMISSION_POLICY_FIELDS.map((field) => {
        const raw = admissionPolicyForm[field.key] ?? "";
        return [field.key, Math.trunc(Number.parseFloat(raw))];
      })
    ) as AdmissionPolicyValues;
  };

  const runAdmissionPolicyAction = (action: "validate" | "save" | "reset") => {
    setBusy(true);
    setMessage(action === "reset" ? "resetting admission" : `${action} admission`);
    const path = action === "validate" ? "/admin/admission-policy/validate" : "/admin/admission-policy";
    const init: RequestInit = action === "reset"
      ? { method: "DELETE" }
      : {
          method: action === "save" ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(admissionPolicyBody())
        };
    apiJson<any>(path, init)
      .then((payload) => {
        if (action === "validate") {
          setMessage(payload.accepted ? "admission accepted" : payload.errors.join("; "));
          return;
        }
        setAdmissionPolicyPayload(payload);
        setMessage(`admission ${action === "save" ? "saved" : "reset"}`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const networkPolicyBody = (): NetworkPolicyValues => ({
    cors_allow_origins: parseCsv(networkCorsOrigins),
    trusted_proxy_cidrs: parseCsv(networkTrustedProxyCidrs)
  });

  const runNetworkPolicyAction = (action: "validate" | "save" | "reset") => {
    setBusy(true);
    setMessage(action === "reset" ? "resetting network" : `${action} network`);
    const path = action === "validate" ? "/admin/network-policy/validate" : "/admin/network-policy";
    const init: RequestInit = action === "reset"
      ? { method: "DELETE" }
      : {
          method: action === "save" ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(networkPolicyBody())
        };
    apiJson<any>(path, init)
      .then((payload) => {
        if (action === "validate") {
          setMessage(payload.accepted ? "network accepted" : payload.errors.join("; "));
          return;
        }
        setNetworkPolicyPayload(payload);
        setMessage(`network ${action === "save" ? "saved" : "reset"}`);
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const setMaintenanceMode = (enabled: boolean) => {
    setBusy(true);
    setMessage(enabled ? "enabling maintenance" : "disabling maintenance");
    apiJson<MaintenanceState>(`/admin/maintenance`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, reason: maintenanceReason.trim() })
    })
      .then((payload) => {
        setMaintenance(payload);
        setMaintenanceReason(payload.reason ?? "");
        setMessage(payload.enabled ? "maintenance active" : "maintenance disabled");
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const parseUpdateImageRefs = (): { service: string; image: string }[] => {
    const parsed = JSON.parse(updateImageRefs);
    if (!Array.isArray(parsed)) {
      throw new Error("image refs must be a JSON array");
    }
    return parsed;
  };

  const createUpdatePlan = () => {
    let image_refs: { service: string; image: string }[];
    try {
      image_refs = parseUpdateImageRefs();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "invalid image refs");
      return;
    }
    setBusy(true);
    setMessage("planning update");
    apiJson<UpdatePlan>(`/admin/updates`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_version: updateVersion.trim(), source_url: updateSourceUrl.trim(), image_refs, notes: updateNotes.trim() })
    })
      .then((payload) => {
        setMessage(`update ${payload.status}`);
        loadUpdates();
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const updateAction = (update: UpdatePlan, action: "stage" | "health-check" | "promote" | "rollback") => {
    setBusy(true);
    setMessage(`${action} update`);
    apiJson<UpdatePlan>(`/admin/updates/${encodeURIComponent(update.id)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: `${action} from Control Center`, timeout_seconds: 30 })
    })
      .then((payload) => {
        setMessage(`${payload.id} ${payload.status}`);
        loadUpdates();
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  useEffect(() => {
    runSelfTest();
    loadAcceptanceReports();
    loadRollbackRehearsal();
    loadAudit();
    loadResourcePolicy();
    loadAdmissionPolicy();
    loadNetworkPolicy();
    loadMaintenance();
    loadUpdates();
  }, []);

  const selectedReport = selectedAcceptanceReport?.report ?? {};
  const selectedSummary = selectedAcceptanceReport?.summary;
  const selectedFiles = selectedSummary?.files ?? {};
  const selectedSourceControl = detailRecord(selectedReport.source_control);
  const selectedCutover = detailRecord(selectedReport.cutover_preservation);
  const selectedOperatorEvidence = acceptanceOperatorEvidenceRows(selectedReport);
  const selectedLiveEvidence = acceptanceLiveEvidenceRows(selectedReport);
  const selectedPreservedResources = acceptancePreservedResourceRows(selectedReport);

  return (
    <section className="panel wide">
      <SectionTitle icon={<ShieldCheck size={18} />} title="System" />
      <div className="toolbar">
        <button title="Run self-test" onClick={runSelfTest} disabled={busy}><RefreshCw size={16} />Self-test</button>
        <button title="Refresh audit log" onClick={loadAudit} disabled={busy}><ScrollText size={16} />Audit</button>
        <span className={`status-pill ${result?.status ?? "idle"}`}>{message}</span>
      </div>
      <div className="subsection-title">
        <Gauge size={16} />
        <h3>Resource Policy</h3>
      </div>
      <div className="toolbar">
        <button title="Validate resource policy" onClick={() => runPolicyAction("validate")} disabled={busy || !resourcePolicy}><ListChecks size={16} />Validate</button>
        <button title="Save resource policy" onClick={() => runPolicyAction("save")} disabled={busy || !resourcePolicy}><ShieldCheck size={16} />Save</button>
        <button title="Reset resource policy" onClick={() => runPolicyAction("reset")} disabled={busy || !resourcePolicy}><RotateCcw size={16} />Reset</button>
        <span className="toolbar-status">{resourcePolicy ? `${resourcePolicy.source} profile` : "policy unavailable"}</span>
      </div>
      {resourcePolicy && (
        <div className="policy-grid">
          {RESOURCE_POLICY_FIELDS.map((field) => {
            const bounds = resourcePolicy.bounds[field.key];
            return (
              <label key={field.key}>
                {field.label}
                <input
                  type="number"
                  min={bounds.minimum}
                  max={bounds.maximum}
                  step={field.step}
                  value={policyForm[field.key] ?? ""}
                  onChange={(event) => setPolicyForm((current) => ({ ...current, [field.key]: event.target.value }))}
                />
                <small>{bounds.minimum}..{bounds.maximum}</small>
              </label>
            );
          })}
        </div>
      )}
      <div className="subsection-title">
        <ListChecks size={16} />
        <h3>Admission Policy</h3>
      </div>
      <div className="toolbar">
        <button title="Validate admission policy" onClick={() => runAdmissionPolicyAction("validate")} disabled={busy || !admissionPolicy}><ListChecks size={16} />Validate</button>
        <button title="Save admission policy" onClick={() => runAdmissionPolicyAction("save")} disabled={busy || !admissionPolicy}><ShieldCheck size={16} />Save</button>
        <button title="Reset admission policy" onClick={() => runAdmissionPolicyAction("reset")} disabled={busy || !admissionPolicy}><RotateCcw size={16} />Reset</button>
        <span className="toolbar-status">{admissionPolicy ? `${admissionPolicy.source} limits` : "admission unavailable"}</span>
      </div>
      {admissionPolicy && (
        <div className="policy-grid">
          {ADMISSION_POLICY_FIELDS.map((field) => {
            const bounds = admissionPolicy.bounds[field.key];
            const value = Number.parseFloat(admissionPolicyForm[field.key] ?? "");
            return (
              <label key={field.key}>
                {field.label}
                <input
                  type="number"
                  min={bounds.minimum}
                  max={bounds.maximum}
                  step={field.step}
                  value={admissionPolicyForm[field.key] ?? ""}
                  onChange={(event) => setAdmissionPolicyForm((current) => ({ ...current, [field.key]: event.target.value }))}
                />
                <small>{bounds.minimum}..{bounds.maximum}{field.formatter && Number.isFinite(value) ? ` / ${field.formatter(value)}` : ""}</small>
              </label>
            );
          })}
        </div>
      )}
      <div className="subsection-title">
        <ShieldCheck size={16} />
        <h3>Network Policy</h3>
      </div>
      <div className="toolbar">
        <button title="Validate network policy" onClick={() => runNetworkPolicyAction("validate")} disabled={busy || !networkPolicy}><ListChecks size={16} />Validate</button>
        <button title="Save network policy" onClick={() => runNetworkPolicyAction("save")} disabled={busy || !networkPolicy}><ShieldCheck size={16} />Save</button>
        <button title="Reset network policy" onClick={() => runNetworkPolicyAction("reset")} disabled={busy || !networkPolicy}><RotateCcw size={16} />Reset</button>
        <span className="toolbar-status">{networkPolicy ? `${networkPolicy.source} network` : "network unavailable"}</span>
      </div>
      {networkPolicy && (
        <div className="policy-grid">
          <label>
            CORS origins
            <input value={networkCorsOrigins} onChange={(event) => setNetworkCorsOrigins(event.target.value)} />
            <small>{networkPolicy.effective.cors_allow_origins.length} active</small>
          </label>
          <label>
            Trusted proxy CIDRs
            <input value={networkTrustedProxyCidrs} onChange={(event) => setNetworkTrustedProxyCidrs(event.target.value)} />
            <small>{networkPolicy.effective.trusted_proxy_cidrs.length} active</small>
          </label>
        </div>
      )}
      <div className="subsection-title">
        <PauseCircle size={16} />
        <h3>Maintenance</h3>
      </div>
      <div className="toolbar">
        <button title="Enable maintenance mode" onClick={() => setMaintenanceMode(true)} disabled={busy || !maintenance || !maintenanceReason.trim()}><PauseCircle size={16} />Enable</button>
        <button title="Disable maintenance mode" onClick={() => setMaintenanceMode(false)} disabled={busy || !maintenance}><CheckCircle2 size={16} />Disable</button>
        <button title="Refresh maintenance state" onClick={loadMaintenance} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <span className={`status-pill ${maintenance?.enabled ? "warning" : "ok"}`}>{maintenance?.enabled ? "active" : "disabled"}</span>
        <span className="toolbar-status">
          {maintenance ? `${maintenance.source}${maintenance.updated_at ? ` / updated ${new Date(maintenance.updated_at).toLocaleString()}` : ""}` : "maintenance unavailable"}
        </span>
      </div>
      <div className="stack">
        <label>
          Reason
          <textarea rows={3} value={maintenanceReason} onChange={(event) => setMaintenanceReason(event.target.value)} maxLength={500} />
        </label>
      </div>
      {maintenance?.enabled && <div className="one-time-key"><strong>Maintenance active</strong><small>{maintenance.reason}</small></div>}
      <div className="subsection-title">
        <Archive size={16} />
        <h3>Updates</h3>
      </div>
      <div className="stack">
        <div className="split">
          <label>Target version<input value={updateVersion} onChange={(event) => setUpdateVersion(event.target.value)} /></label>
          <label>Release URL<input value={updateSourceUrl} onChange={(event) => setUpdateSourceUrl(event.target.value)} placeholder="https://..." /></label>
        </div>
        <label>Notes<input value={updateNotes} onChange={(event) => setUpdateNotes(event.target.value)} /></label>
        <label>Pinned image refs JSON<textarea rows={5} value={updateImageRefs} onChange={(event) => setUpdateImageRefs(event.target.value)} /></label>
      </div>
      <div className="toolbar">
        <button title="Create update plan" onClick={createUpdatePlan} disabled={busy || !updateVersion.trim()}><ListChecks size={16} />Plan</button>
        <button title="Refresh update plans" onClick={loadUpdates} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <span className="toolbar-status">Stage, promote, health-check, and rollback require maintenance mode</span>
      </div>
      <table>
        <thead><tr><th>Update</th><th>Status</th><th>Backup</th><th>Images</th><th>Actions</th></tr></thead>
        <tbody>
          {updates.map((update) => (
            <tr key={update.id}>
              <td><code>{update.target_version}</code><small>{update.id}</small></td>
              <td><span className={`status-pill ${["validated", "staged", "promotion_ready"].includes(update.status) ? "ok" : update.status.includes("failed") ? "failed" : "planned"}`}>{update.status}</span><small>{update.stage}</small>{update.failure_message && <small>{update.failure_message}</small>}</td>
              <td>{update.backup_name ?? "none"}<small>{update.self_test?.status ? `self-test ${update.self_test.status}` : ""}</small></td>
              <td>
                {update.image_refs.length}
                <small>{update.image_refs.map((item) => item.service).join(", ")}</small>
                {!!update.image_stage?.length && (
                  <small>
                    staged: {update.image_stage.map((item) => `${item.service ?? "image"}=${item.status ?? "unknown"}`).join(", ")}
                  </small>
                )}
                {update.compose_override?.path && (
                  <small>
                    override: {update.compose_override.ready_for_promotion ? "ready" : "pending pull"} / {update.compose_override.path}
                  </small>
                )}
                {update.promotion_result?.status && (
                  <small>
                    promotion: {update.promotion_result.status}
                    {update.promotion_result.error ? ` / ${update.promotion_result.error}` : ""}
                  </small>
                )}
                {update.promotion_result?.promotion_command?.shell && (
                  <small><code>{update.promotion_result.promotion_command.shell}</code></small>
                )}
              </td>
              <td>
                <div className="table-actions">
                  <button title={`Stage ${update.id}`} onClick={() => updateAction(update, "stage")} disabled={busy || !maintenance?.enabled}><Upload size={16} /></button>
                  <button title={`Health-check ${update.id}`} onClick={() => updateAction(update, "health-check")} disabled={busy || !maintenance?.enabled}><CheckCircle2 size={16} /></button>
                  <button title={`Prepare promotion ${update.id}`} onClick={() => updateAction(update, "promote")} disabled={busy || !maintenance?.enabled}><Rocket size={16} /></button>
                  <button title={`Rollback ${update.id}`} onClick={() => updateAction(update, "rollback")} disabled={busy || !maintenance?.enabled}><RotateCcw size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!updates.length && <tr><td colSpan={5}>No update plans recorded</td></tr>}
        </tbody>
      </table>
      <table>
        <thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>
        <tbody>
          {(result?.checks ?? []).map((item) => (
            <tr key={item.name}>
              <td><code>{item.name}</code></td>
              <td><span className={`status-pill ${item.status}`}>{item.status}</span></td>
              <td>{item.detail}</td>
            </tr>
          ))}
          {!result?.checks?.length && <tr><td colSpan={3}>No self-test results</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <RotateCcw size={16} />
        <h3>Rollback Rehearsal</h3>
      </div>
      <div className="metrics compact">
        <Metric
          label="Cutover plan"
          value={rollbackRehearsal?.cutover_plan.available ? "ready" : "missing"}
          detail={rollbackRehearsal?.cutover_plan.name ?? rollbackRehearsal?.cutover_plan.reason ?? "not loaded"}
        />
        <Metric
          label="Rollback actions"
          value={formatCount(rollbackRehearsal?.cutover_plan.rollback_operator_action_count ?? 0)}
          detail={`${formatCount(rollbackRehearsal?.cutover_plan.rollback_command_count ?? 0)} command${(rollbackRehearsal?.cutover_plan.rollback_command_count ?? 0) === 1 ? "" : "s"}`}
        />
        <Metric
          label="Preserved resources"
          value={formatCount(rollbackRehearsal?.cutover_plan.resource_count ?? rollbackRehearsal?.report.resource_count ?? 0)}
          detail={rollbackRehearsal?.report.available ? `report ${rollbackRehearsal.report.status ?? "recorded"}` : "report missing"}
        />
        <Metric
          label="Report"
          value={rollbackRehearsal?.report.available ? "recorded" : "missing"}
          detail={rollbackRehearsal?.report.generated_at ? formatDateTime(rollbackRehearsal.report.generated_at) : rollbackRehearsal?.report.reason ?? "not loaded"}
        />
      </div>
      <div className="stack">
        <div className="split">
          <label>Rehearsed by<input value={rollbackRehearsedBy} onChange={(event) => setRollbackRehearsedBy(event.target.value)} maxLength={128} placeholder="current admin" /></label>
          <label>Cutover plan<input value={rollbackRehearsal?.cutover_plan.name ?? "unavailable"} readOnly /></label>
        </div>
        <label>Notes<textarea rows={2} value={rollbackNotes} onChange={(event) => setRollbackNotes(event.target.value)} maxLength={2000} /></label>
        <div className="evidence-grid">
          <label className="evidence-item">
            <input type="checkbox" checked={rollbackCommandsTested} onChange={(event) => setRollbackCommandsTested(event.target.checked)} />
            <span>Rollback commands/actions rehearsed</span>
          </label>
          <label className="evidence-item">
            <input type="checkbox" checked={rollbackResourcesPreserved} onChange={(event) => setRollbackResourcesPreserved(event.target.checked)} />
            <span>Old resources preserved</span>
          </label>
        </div>
      </div>
      <div className="toolbar">
        <button title="Refresh rollback rehearsal status" onClick={loadRollbackRehearsal} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <button title="Generate rollback rehearsal report" onClick={createRollbackRehearsal} disabled={busy || !rollbackRehearsal?.cutover_plan.available || !rollbackCommandsTested || !rollbackResourcesPreserved}><RotateCcw size={16} />Generate</button>
        <span className="toolbar-status">{rollbackRehearsal?.report.path ?? rollbackRehearsal?.backup_root ?? "rollback status unavailable"}</span>
      </div>
      <div className="subsection-title">
        <Archive size={16} />
        <h3>Acceptance Reports</h3>
      </div>
      <div className="stack">
        <div className="split">
          <label>Label<input value={acceptanceLabel} onChange={(event) => setAcceptanceLabel(event.target.value)} maxLength={120} /></label>
          <label>Status<input value={result?.status ?? "not run"} readOnly /></label>
        </div>
        <label>Notes<textarea rows={3} value={acceptanceNotes} onChange={(event) => setAcceptanceNotes(event.target.value)} maxLength={4000} /></label>
        <div className="evidence-grid">
          {ACCEPTANCE_EVIDENCE_ITEMS.map(([key, label]) => (
            <label key={key} className="evidence-item">
              <input
                type="checkbox"
                checked={acceptanceEvidence[key]}
                onChange={(event) => setAcceptanceEvidence((current) => ({ ...current, [key]: event.target.checked }))}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
      </div>
      <div className="toolbar">
        <button title="Create acceptance report" onClick={createAcceptanceReport} disabled={busy}><Archive size={16} />Create</button>
        <button title="Refresh acceptance reports" onClick={loadAcceptanceReports} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <span className="toolbar-status">{acceptanceReports.length ? `${acceptanceReports.length} reports` : "no reports"}</span>
      </div>
      <table>
        <thead><tr><th>Report</th><th>Status</th><th>Mode</th><th>Files</th><th>Blockers</th><th>Actions</th></tr></thead>
        <tbody>
          {acceptanceReports.map((report) => (
            <tr key={report.id}>
              <td><code>{report.id}</code><small>{report.generated_at ? new Date(report.generated_at).toLocaleString() : ""}</small></td>
              <td>
                <span className={statusPillClass(report.status)}>{report.status}</span>
                <small>{report.operator_handoff_ready ? "handoff ready" : !report.operator_evidence_ready ? "evidence missing" : !report.smoke_evidence_ready ? "smoke proof missing" : !report.gpu_evidence_ready ? "GPU proof missing" : !report.localai_evidence_ready ? "LocalAI proof missing" : !report.installed_workflows_evidence_ready ? "workflow proof missing" : !report.native_comfyui_evidence_ready ? "ComfyUI proof missing" : !report.remote_nodes_evidence_ready ? "remote-node proof missing" : !report.modelhub_evidence_ready ? "Model Hub proof missing" : !report.voicebox_evidence_ready ? "Voicebox proof missing" : !report.security_evidence_ready ? "security proof missing" : !report.restart_reconciliation_evidence_ready ? "restart proof missing" : !report.backup_migration_rollback_evidence_ready ? "backup/rollback proof missing" : !report.cutover_preservation_ready ? "rollback preservation missing" : "system blockers"}</small>
              </td>
              <td>{report.runtime_deployment_mode ?? "unknown"}</td>
              <td>
                {report.files?.markdown ? <small>{report.files.markdown}</small> : "pending"}
                {report.files?.sha256sums ? <small>{report.files.sha256sums}</small> : null}
              </td>
              <td>{report.acceptance_blockers.length ? report.acceptance_blockers.slice(0, 3).join("; ") : "none"}</td>
              <td>
                <div className="table-actions">
                  <button title={`Inspect ${report.id}`} onClick={() => inspectAcceptanceReport(report.id)} disabled={busy}><ScrollText size={16} /></button>
                  <button title={`Download Markdown for ${report.id}`} onClick={() => downloadAcceptanceReport(report.id, "report.md")} disabled={busy || !report.files?.markdown}><Download size={16} /></button>
                  <button title={`Download checksums for ${report.id}`} onClick={() => downloadAcceptanceReport(report.id, "SHA256SUMS")} disabled={busy || !report.files?.sha256sums}><KeyRound size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!acceptanceReports.length && <tr><td colSpan={6}>No acceptance reports recorded</td></tr>}
        </tbody>
      </table>
      {selectedAcceptanceReport && selectedSummary && (
        <div className="acceptance-detail">
          <div className="subsection-title">
            <ScrollText size={16} />
            <h3>Acceptance Report Detail</h3>
          </div>
          <div className="acceptance-detail-grid">
            <div><strong>Report</strong><small>{selectedSummary.id}</small></div>
            <div><strong>Status</strong><small>{selectedSummary.status}</small></div>
            <div><strong>Mode</strong><small>{selectedSummary.runtime_deployment_mode ?? "unknown"}</small></div>
            <div><strong>Generated</strong><small>{formatDateTime(selectedSummary.generated_at)}</small></div>
            <div><strong>Handoff</strong><small>{selectedSummary.operator_handoff_ready ? "ready" : "blocked"}</small></div>
            <div><strong>Source commit</strong><small>{String(selectedSourceControl.source_commit ?? selectedSourceControl.source_ref ?? "unavailable")}</small></div>
            <div><strong>Cutover resources</strong><small>{String(selectedCutover.resource_count ?? 0)}</small></div>
            <div><strong>Report files</strong><small>{selectedFiles.markdown ?? selectedFiles.json ?? "not written"}</small></div>
          </div>
          <div className="toolbar">
            <button title={`Download Markdown for ${selectedSummary.id}`} onClick={() => downloadAcceptanceReport(selectedSummary.id, "report.md")} disabled={busy || !selectedFiles.markdown}><Download size={16} />Markdown</button>
            <button title={`Download JSON for ${selectedSummary.id}`} onClick={() => downloadAcceptanceReport(selectedSummary.id, "report.json")} disabled={busy || !selectedFiles.json}><Download size={16} />JSON</button>
            <button title={`Download checksums for ${selectedSummary.id}`} onClick={() => downloadAcceptanceReport(selectedSummary.id, "SHA256SUMS")} disabled={busy || !selectedFiles.sha256sums}><KeyRound size={16} />Checksums</button>
            <span className="toolbar-status">Downloads use the authenticated admin report file endpoint</span>
          </div>
          <div className="acceptance-detail-section">
            <h4>Acceptance Blockers</h4>
            {selectedSummary.acceptance_blockers.length ? (
              <ul className="acceptance-blockers">
                {selectedSummary.acceptance_blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
              </ul>
            ) : (
              <p>None</p>
            )}
          </div>
          <div className="acceptance-detail-section">
            <h4>Operator Evidence</h4>
            <table>
              <thead><tr><th>Evidence</th><th>Status</th><th>Note</th></tr></thead>
              <tbody>
                {selectedOperatorEvidence.map((item) => (
                  <tr key={item.key}>
                    <td>{item.label}<small>{item.key}</small></td>
                    <td><span className={statusPillClass(item.passed ? "ok" : "warning")}>{item.passed ? "passed" : "missing"}</span></td>
                    <td>{item.note || "none"}</td>
                  </tr>
                ))}
                {!selectedOperatorEvidence.length && <tr><td colSpan={3}>No operator evidence recorded</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="acceptance-detail-section">
            <h4>Live Evidence</h4>
            <table>
              <thead><tr><th>Evidence</th><th>Status</th><th>Missing Checks</th><th>Source</th></tr></thead>
              <tbody>
                {selectedLiveEvidence.map((item) => (
                  <tr key={item.key}>
                    <td>{item.label}<small>{item.samples.length ? item.samples.join(", ") : item.key}</small></td>
                    <td><span className={statusPillClass(item.available && item.status === "ok" ? "ok" : item.status)}>{item.status}</span></td>
                    <td>{item.missingChecks.length ? item.missingChecks.join(", ") : "none"}</td>
                    <td>{item.sourcePath || "not recorded"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="acceptance-detail-section">
            <h4>Preserved Rollback Resources</h4>
            <table>
              <thead><tr><th>Resource</th><th>Count</th><th>Items</th></tr></thead>
              <tbody>
                {selectedPreservedResources.map((item) => (
                  <tr key={item.key}>
                    <td>{item.label}<small>{item.key}</small></td>
                    <td>{item.items.length}</td>
                    <td>{item.items.length ? item.items.join(", ") : "none"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details className="acceptance-raw">
            <summary>Raw JSON</summary>
            <pre>{JSON.stringify(selectedReport, null, 2)}</pre>
          </details>
        </div>
      )}
      <div className="subsection-title">
        <ScrollText size={16} />
        <h3>Service Logs</h3>
      </div>
      <div className="toolbar job-filters">
        <label>Service
          <select value={serviceLogService} onChange={(event) => setServiceLogService(event.target.value)}>
            {SERVICE_LOG_OPTIONS.map((service) => <option key={service} value={service}>{service}</option>)}
          </select>
        </label>
        <label>Lines
          <input inputMode="numeric" value={serviceLogLines} onChange={(event) => setServiceLogLines(event.target.value)} />
        </label>
        <button title="Fetch service logs" onClick={loadServiceLogs} disabled={busy}><ScrollText size={16} />Logs</button>
        <span className="toolbar-status">{serviceLogs ? `${serviceLogs.service} / ${serviceLogs.entries.length} entries` : "no log snapshot"}</span>
      </div>
      <pre className="log-output">{serviceLogs?.entries.join("\n") || "No logs loaded"}</pre>
      <div className="subsection-title">
        <ScrollText size={16} />
        <h3>Recent Audit</h3>
      </div>
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Actor</th><th>Target</th><th>Summary</th></tr></thead>
        <tbody>
          {auditEvents.map((item) => (
            <tr key={item.id}>
              <td><code>{new Date(item.created_at).toLocaleString()}</code></td>
              <td><code>{item.event_type}</code></td>
              <td>{item.actor_role ?? "unknown"}<small>{item.actor_id ?? ""}</small></td>
              <td>{item.target_type ?? ""}<small>{item.target_id ?? ""}</small></td>
              <td>{item.summary}</td>
            </tr>
          ))}
          {!auditEvents.length && <tr><td colSpan={5}>No audit events recorded</td></tr>}
        </tbody>
      </table>
    </section>
  );
}

const WORKFLOW_TEMPLATE = JSON.stringify(
  {
    id: "custom-workflow",
    version: "0.1.0",
    display_name: "Custom Workflow",
    description: "Draft workflow imported from native ComfyUI API JSON.",
    modality: "image",
    operation: "generation",
    model_alias: "image-default",
    backend_policy: "comfyui-only",
    runtime_policy: "any",
    output_mime_types: ["image/png"],
    workflow_json: {},
    input_schema: {
      type: "object",
      required: ["prompt"],
      properties: {
        prompt: { type: "string", minLength: 1, maxLength: 2000 },
        steps: { type: "integer", minimum: 1, maximum: 40, default: 20 },
        seed: { type: "integer", minimum: 0, default: 0 }
      },
      additionalProperties: false
    },
    output_schema: {
      type: "object",
      properties: {
        images: { type: "array", items: { type: "string" } }
      }
    },
    resource_class: "rtx3060-32gb",
    dependencies: [
      { type: "runtime", id: "comfyui" },
      { type: "model", id: "image-default" }
    ],
    limits: {
      max_width: 1024,
      max_height: 1024,
      max_steps: 40,
      max_batch_size: 1
    },
    visibility_roles: ["admin", "operator", "creator"]
  },
  null,
  2
);

function Workflows() {
  const [workflows, setWorkflows] = useState<PublishedWorkflow[]>([]);
  const [selected, setSelected] = useState<PublishedWorkflow | null>(null);
  const [draft, setDraft] = useState(WORKFLOW_TEMPLATE);
  const [validation, setValidation] = useState<PublishedWorkflow | null>(null);
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);

  const loadWorkflows = () => {
    setMessage("loading");
    apiFetch(`/workflows/v1/published`)
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        const rows = payload.data ?? [];
        setWorkflows(rows);
        if (!selected && rows.length) {
          setSelected(rows[0]);
        }
        setMessage("ready");
      })
      .catch((err: Error) => setMessage(err.message));
  };

  useEffect(loadWorkflows, []);

  const parseDraft = () => {
    try {
      return JSON.parse(draft);
    } catch (error) {
      throw new Error(error instanceof Error ? `invalid JSON: ${error.message}` : "invalid JSON");
    }
  };

  const validateDraft = () => {
    setBusy(true);
    setMessage("validating");
    let workflow: unknown;
    try {
      workflow = parseDraft();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "invalid JSON");
      setBusy(false);
      return;
    }
    apiFetch(`/workflows/v1/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setValidation(payload);
        setMessage(`${payload.id}@${payload.version} ${payload.status}`);
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const publishDraft = () => {
    setBusy(true);
    setMessage("publishing");
    let workflow: unknown;
    try {
      workflow = parseDraft();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "invalid JSON");
      setBusy(false);
      return;
    }
    apiFetch(`/workflows/v1/published`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setSelected(payload);
        setValidation(payload);
        setMessage(`published ${payload.id}@${payload.version}`);
        loadWorkflows();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const unpublishWorkflow = (workflow: PublishedWorkflow) => {
    setBusy(true);
    setMessage("unpublishing");
    apiFetch(`/workflows/v1/published/${encodeURIComponent(workflow.id)}/versions/${encodeURIComponent(workflow.version)}`, { method: "DELETE" })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setMessage(`unpublished ${payload.id}@${payload.version}`);
        setSelected(null);
        loadWorkflows();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const editWorkflow = (workflow: PublishedWorkflow) => {
    setSelected(workflow);
    setDraft(JSON.stringify(
      {
        id: workflow.id,
        version: workflow.version,
        display_name: workflow.display_name,
        description: workflow.description,
        modality: workflow.modality,
        operation: workflow.operation,
        model_alias: workflow.model_alias,
        backend_policy: workflow.backend_policy,
        runtime_policy: workflow.runtime_policy,
        output_mime_types: workflow.output_mime_types,
        workflow_json: workflow.workflow_json,
        input_schema: workflow.input_schema,
        output_schema: workflow.output_schema,
        resource_class: workflow.resource_class,
        dependencies: workflow.dependencies.map((dependency) => ({
          type: dependency.type,
          id: dependency.id,
          ...(dependency.version ? { version: dependency.version } : {})
        })),
        limits: workflow.limits,
        visibility_roles: workflow.visibility_roles
      },
      null,
      2
    ));
    setValidation(null);
  };

  const loadDraftFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    file.text()
      .then((text) => {
        setDraft(text);
        setValidation(null);
        setMessage(`loaded ${file.name}`);
      })
      .catch((err: Error) => setMessage(err.message));
  };

  const dependencyRows = (validation?.dependency_status?.dependencies ?? selected?.dependency_status?.dependencies ?? []);

  return (
    <section className="panel wide">
      <SectionTitle icon={<Workflow size={18} />} title="Workflows" />
      <div className="toolbar">
        <button title="Refresh workflows" onClick={loadWorkflows} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <button title="Validate draft" onClick={validateDraft} disabled={busy}><ListChecks size={16} />Validate</button>
        <button title="Publish draft" onClick={publishDraft} disabled={busy}><Archive size={16} />Publish</button>
        <label className="file-button">
          <Upload size={16} />Import JSON
          <input type="file" accept="application/json,.json" onChange={loadDraftFile} disabled={busy} />
        </label>
        <span className="toolbar-status">{message}</span>
      </div>

      {validation && (
        <div className="one-time-key">
          <strong>{validation.id}@{validation.version} {validation.status}</strong>
          <span>{validation.display_name} / {validation.modality} / {validation.model_alias}</span>
          <small>{validation.backend_policy} / {validation.runtime_policy} / {validation.output_mime_types.join(", ")}</small>
        </div>
      )}

      <div className="split workflow-admin">
        <div className="stack">
          <h3>Published Workflows</h3>
          <table>
            <thead><tr><th>Workflow</th><th>Status</th><th>Backend</th><th>Actions</th></tr></thead>
            <tbody>
              {workflows.map((workflow) => (
                <tr key={`${workflow.id}@${workflow.version}`}>
                  <td><code>{workflow.display_name}</code><small>{workflow.id}@{workflow.version}</small></td>
                  <td><span className={`status-pill ${workflow.status}`}>{workflow.status}</span><small>{workflow.publishable ? "ready" : "needs dependencies"}</small></td>
                  <td>{workflow.backend_policy}<small>{workflow.modality} / {workflow.model_alias}</small></td>
                  <td>
                    <div className="table-actions">
                      <button title={`Edit ${workflow.id}`} onClick={() => editWorkflow(workflow)} disabled={busy}><Workflow size={16} /></button>
                      <button title={`Unpublish ${workflow.id}`} onClick={() => unpublishWorkflow(workflow)} disabled={busy}><Trash2 size={16} /></button>
                    </div>
                  </td>
                </tr>
              ))}
              {!workflows.length && <tr><td colSpan={4}>No published workflows recorded</td></tr>}
            </tbody>
          </table>
          <div className="subsection-title">
            <ListChecks size={16} />
            <h3>Dependencies</h3>
          </div>
          <table>
            <thead><tr><th>Type</th><th>ID</th><th>Status</th><th>Reason</th></tr></thead>
            <tbody>
              {dependencyRows.map((dependency) => (
                <tr key={`${dependency.type}-${dependency.id}`}>
                  <td>{dependency.type}</td>
                  <td><code>{dependency.id}</code><small>{dependency.version ?? ""}</small></td>
                  <td><span className={`status-pill ${dependency.ready ? "ok" : "warning"}`}>{dependency.status ?? (dependency.ready ? "ready" : "blocked")}</span></td>
                  <td>{dependency.reason ?? ""}</td>
                </tr>
              ))}
              {!dependencyRows.length && <tr><td colSpan={4}>No dependency report selected</td></tr>}
            </tbody>
          </table>
        </div>

        <div className="stack">
          <h3>Workflow Draft JSON</h3>
          <textarea className="json-editor" value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} />
        </div>
      </div>
    </section>
  );
}

function AuthGate({ children }: { children: (auth: AuthStatus, logout: () => void) => React.ReactNode }) {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [bootstrapKey, setBootstrapKey] = useState("");
  const [message, setMessage] = useState("checking session");
  const [busy, setBusy] = useState(false);

  const refreshAuth = () => {
    apiJson<AuthStatus>("/auth/status")
      .then((payload) => {
        storeAuthStatus(payload);
        setAuth(payload);
        setMessage(payload.authenticated ? "authenticated" : payload.setup_required ? "setup required" : "login required");
      })
      .catch((error: Error) => {
        storeAuthStatus(null);
        setAuth(null);
        setMessage(error.message);
      });
  };

  useEffect(refreshAuth, []);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setMessage(auth?.setup_required ? "creating administrator" : "logging in");
    apiJson<AuthStatus>(auth?.setup_required ? "/auth/setup" : "/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        password,
        ...(auth?.setup_required ? { bootstrap_key: bootstrapKey } : {})
      })
    })
      .then((payload) => {
        storeAuthStatus(payload);
        setAuth(payload);
        setPassword("");
        setBootstrapKey("");
        setMessage("authenticated");
      })
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const logout = () => {
    setBusy(true);
    apiJson<{ status: string }>("/auth/logout", { method: "POST" })
      .catch(() => ({ status: "offline" }))
      .finally(() => {
        storeAuthStatus(null);
        setAuth({ configured: true, setup_required: false, authenticated: false, scopes: [] });
        setPassword("");
        setBootstrapKey("");
        setBusy(false);
        setMessage("logged out");
      });
  };

  if (auth?.authenticated) {
    return <>{children(auth, logout)}</>;
  }

  return (
    <main>
      <header className="topbar">
        <div>
          <h1>B1 AI Control Center</h1>
          <span>{message}</span>
        </div>
        <div className="status-strip">
          <ShieldCheck size={16} /> LAN internal
        </div>
      </header>
      <section className="auth-shell">
        <form className="panel auth-panel" onSubmit={submit}>
          <SectionTitle icon={<KeyRound size={18} />} title={auth?.setup_required ? "Initial Admin" : "Sign In"} />
          <label>Username<input value={username} onChange={(event) => setUsername(event.target.value)} required autoComplete="username" /></label>
          <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required autoComplete={auth?.setup_required ? "new-password" : "current-password"} /></label>
          {auth?.setup_required && (
            <label>Bootstrap key<input type="password" value={bootstrapKey} onChange={(event) => setBootstrapKey(event.target.value)} required autoComplete="one-time-code" /></label>
          )}
          <button title={auth?.setup_required ? "Create administrator" : "Sign in"} disabled={busy || !username.trim() || !password}>
            <KeyRound size={16} />{auth?.setup_required ? "Create Admin" : "Sign In"}
          </button>
          <span className="toolbar-status">{message}</span>
        </form>
      </section>
    </main>
  );
}

function ControlCenterApp({ auth, onLogout }: { auth: AuthStatus; onLogout: () => void }) {
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [metrics, setMetrics] = useState<AdminMetrics | null>(null);
  const [error, setError] = useState<string>("");
  const tabs = useMemo(() => ["dashboard", "models", "runtimes", "jobs", "workflows", "external", "storage", "system"], []);

  useEffect(() => {
    apiFetch(`/admin/status`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`${response.status}`)))
      .then(setStatus)
      .catch((err: Error) => setError(err.message));
    apiFetch(`/admin/metrics?limit=500`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error(`${response.status}`)))
      .then(setMetrics)
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <main>
      <header className="topbar">
        <div>
          <h1>B1 AI Control Center</h1>
          <span>{status?.service ?? "control-plane"} {error ? `status ${error}` : "ready for setup"}</span>
        </div>
        <div className="status-strip">
          <ShieldCheck size={16} /> {auth.role ?? "session"}
          <button title="Sign out" onClick={onLogout}><LogOut size={16} /></button>
        </div>
      </header>

      <Tabs.Root defaultValue="dashboard" className="tabs">
        <Tabs.List className="tab-list" aria-label="Control Center sections">
          {tabs.map((tab) => <Tabs.Trigger key={tab} value={tab}>{tab}</Tabs.Trigger>)}
        </Tabs.List>
        <Tabs.Content value="dashboard"><Dashboard status={status} metrics={metrics} /></Tabs.Content>
        <Tabs.Content value="models"><Models /></Tabs.Content>
        <Tabs.Content value="runtimes"><Runtimes /></Tabs.Content>
        <Tabs.Content value="jobs"><Jobs /></Tabs.Content>
        <Tabs.Content value="workflows"><Workflows /></Tabs.Content>
        <Tabs.Content value="external"><ExternalAccess /></Tabs.Content>
        <Tabs.Content value="storage"><Storage /></Tabs.Content>
        <Tabs.Content value="system"><System /></Tabs.Content>
      </Tabs.Root>
    </main>
  );
}

function App() {
  return (
    <AuthGate>
      {(auth, logout) => <ControlCenterApp auth={auth} onLogout={logout} />}
    </AuthGate>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
