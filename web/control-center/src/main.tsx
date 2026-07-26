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
  Eye,
  Gauge,
  HardDrive,
  KeyRound,
  ListChecks,
  LogOut,
  PauseCircle,
  Pencil,
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
import { B1ApiClient, type B1ApiRequestPath } from "./generated/b1-api-client";
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
  resource_policy: ResourcePolicyValues;
  resource_policy_source?: string;
  admission?: AdmissionReport;
  acceptance_model_measurements?: ModelAcceptanceMeasurementCoverage;
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
  cpu_residency_enabled: boolean;
  cpu_residency_max_ram_gib: number;
  cpu_resident_aliases: string[];
};

type ResourcePolicyBounds = Partial<Record<keyof ResourcePolicyValues, { minimum?: number; maximum?: number; type?: string; max_items?: number; pattern?: string }>>;

type ResourcePolicyPayload = {
  source: string;
  effective: ResourcePolicyValues;
  default: ResourcePolicyValues;
  bounds: ResourcePolicyBounds;
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

type CaddyCaStatus = {
  object: "caddy_internal_ca";
  status: string;
  path: string;
  tls_mode: string;
  required: boolean;
  download_url?: string | null;
  available: boolean;
  readable: boolean;
  regular_file: boolean;
  symlink: boolean;
  size_bytes?: number | null;
  sha256?: string | null;
  fingerprint_sha256?: string | null;
  modified_at?: string | null;
  hosts: Record<string, string>;
  trust_guidance?: string[];
  blockers: string[];
  blocker_count: number;
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

type VoiceboxSampleRetentionPlan = {
  status: string;
  root: string;
  policy: { delete_older_than_days: number; cutoff?: string | null; limit?: number };
  candidate_count: number;
  kept_count: number;
  invalid_preserved_count: number;
  total_reclaimable_bytes: number;
  total_reclaimed_bytes?: number;
  truncated?: boolean;
  candidates: { path: string; url?: string; size_bytes?: number; modified_at?: string | null; reason: string }[];
  kept: { path?: string; url?: string; reason: string }[];
  invalid_preserved: { path?: string; reason: string }[];
  deleted?: { path: string; size_bytes?: number; status?: string }[];
  deleted_count?: number;
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

type BackupPostgresImportPlan = {
  format?: string;
  created_at?: string;
  source?: string;
  status: string;
  apply_supported?: boolean;
  table_count?: number;
  row_counts?: Record<string, number>;
  applied_row_counts?: Record<string, number> | null;
  operations?: { table: string; mode: string; primary_key?: string[]; row_count: number }[];
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
  update_health_gate?: {
    ready?: boolean;
    status?: string;
    blockers?: string[];
    missing_checks?: string[];
    non_ok_checks?: { name?: string; status?: string; detail?: string }[];
  };
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

type RuntimeLifecycleBundle = {
  runtime: string;
  required: boolean;
  aggregateStatus: string;
  checks: SelfTestCheck[];
  buildCheck?: SelfTestCheck;
  statusCheck?: SelfTestCheck;
  highlights: string[];
  blockers: string[];
};

type RuntimeReadinessRow = {
  runtime: string;
  required: boolean;
  ready: boolean;
  healthStatus: string;
  healthPlaceholder: boolean;
  serviceObserved: boolean;
  activeContainerCount: number;
  containerImages: string[];
  placeholderReasons: string[];
  blockers: string[];
  remediation: string[];
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
  deployment_pins_ready?: boolean;
  compose_selection_ready?: boolean;
  model_measurement_coverage_ready?: boolean;
  operator_evidence_ready?: boolean;
  cutover_preservation_ready?: boolean;
  cutover_dns_ready?: boolean;
  repository_quality_evidence_ready?: boolean;
  operator_preflight_evidence_ready?: boolean;
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
    resource_counts_by_type?: Record<string, number>;
    resources_sha256?: string;
    rollback_command_count?: number;
    rollback_operator_action_count?: number;
    rollback_actions_sha256?: string;
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
    resource_counts_by_type?: Record<string, number>;
    resources_sha256?: string;
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

const BACKUP_ROLLBACK_INPUT_LABELS = [
  ["b1_backup", "B1 backup"],
  ["restore_report", "Restore report"],
  ["inventory", "Old-stack inventory"],
  ["old_stack_backup", "Old-stack backup"],
  ["open_webui_plan", "Open WebUI plan"],
  ["cutover_plan", "Cutover plan"],
  ["rollback_report", "Rollback rehearsal"]
] as const;

type BackupRollbackInputKey = typeof BACKUP_ROLLBACK_INPUT_LABELS[number][0];

const OPEN_WEBUI_PLAN_INPUT_LABELS = [
  ["inventory", "Old-stack inventory"],
  ["old_stack_backup", "Old-stack backup"],
  ["restore_target", "Restore target"],
  ["current_plan", "Current plan"]
] as const;

type OpenWebUiPlanInputKey = typeof OPEN_WEBUI_PLAN_INPUT_LABELS[number][0];

type BackupRollbackArtifactStatus = {
  available: boolean;
  path?: string;
  name?: string;
  reason?: string;
  status?: string;
  generated_at?: string;
  missing_checks?: string[];
  check_count?: number;
  required_check_count?: number;
};

type OpenWebUiPlanArtifactStatus = BackupRollbackArtifactStatus & {
  recommended_strategy?: string;
  warning_count?: number;
  warnings?: string[];
  readable_database_count?: number;
  backed_up_database_candidate_count?: number;
  compatibility_status?: string;
};

type OpenWebUiMigrationPlanStatus = {
  format: string;
  backup_root: string;
  restore_root: string;
  ready_to_generate: boolean;
  inputs: Record<OpenWebUiPlanInputKey, OpenWebUiPlanArtifactStatus>;
};

type OpenWebUiMigrationPlanCreateResult = {
  status: string;
  path: string;
  name: string;
  summary: OpenWebUiPlanArtifactStatus;
};

type BackupMigrationRollbackEvidenceStatus = {
  format: string;
  backup_root: string;
  restore_root: string;
  ready: boolean;
  blockers: string[];
  inputs: Record<BackupRollbackInputKey, BackupRollbackArtifactStatus> & {
    output?: BackupRollbackArtifactStatus;
  };
  evidence: BackupRollbackArtifactStatus;
};

type BackupMigrationRollbackEvidenceCreateResult = {
  status: string;
  output: string;
  evidence: {
    generated_at?: string;
    required_checks?: string[];
    checks?: Record<string, unknown>;
  };
  inputs: BackupMigrationRollbackEvidenceStatus["inputs"];
};

type AcceptanceEvidenceDetail = {
  key: string;
  label: string;
  passed: boolean;
  note: string;
};

type HandoffCommandDetail = {
  key: string;
  command: string;
};

type LiveEvidenceDetail = {
  key: string;
  label: string;
  available: boolean;
  status: string;
  sourcePath: string;
  missingChecks: string[];
  details: string[];
  samples: string[];
};

type ReportModelMeasurementDetail = {
  key: string;
  group: string;
  alias: string;
  ready: boolean;
  status: string;
  runtime: string;
  resolvedModel: string;
  okRuns: string;
  hookProof: string;
  blockers: string[];
};

type PreservedResourceDetail = {
  key: string;
  label: string;
  items: string[];
};

type CutoverNetworkDetail = {
  key: string;
  label: string;
  status: string;
  detail: string;
  warnings: string[];
};

type DeploymentPinDetail = {
  key: string;
  subject: string;
  reference: string;
  pin: string;
  detail: string;
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
  ["repository_quality", "Repository quality"],
  ["operator_preflight", "Operator preflight"],
  ["live_stack_smoke", "Live stack smoke"],
  ["gpu_acceptance", "RTX 3060 GPU"],
  ["localai_runtime", "LocalAI runtime"],
  ["installed_workflows", "Installed workflows"],
  ["native_comfyui_compatibility", "Native ComfyUI"],
  ["legacy_comfyui_listener", "Legacy :8188"],
  ["remote_nodes_non_comfy", "Remote nodes"],
  ["modelhub_client_sync", "Model Hub sync"],
  ["voicebox_remote", "Voicebox remote"],
  ["security_acceptance", "Security acceptance"],
  ["restart_reconciliation", "Restart reconciliation"],
  ["backup_migration_rollback", "Backup/migration/rollback"]
] as const;

const ACCEPTANCE_LIVE_EVIDENCE_DETAIL_FIELDS = [
  ["missing_quality_evidence", "quality proof"],
  ["missing_preflight_evidence", "preflight proof"],
  ["missing_smoke_evidence", "smoke proof"],
  ["missing_gpu_evidence", "GPU proof"],
  ["missing_localai_evidence", "LocalAI proof"],
  ["missing_installed_workflow_evidence", "workflow proof"],
  ["missing_model_measurements", "model measurements"],
  ["missing_compatibility_evidence", "compatibility proof"],
  ["missing_integrity_evidence", "integrity proof"],
  ["missing_security_evidence", "security proof"],
  ["missing_reconciliation_evidence", "restart proof"],
  ["missing_backup_migration_rollback_evidence", "backup/rollback proof"],
  ["failed_checks", "failed checks"],
  ["warning_checks", "warnings"]
] as const;

const ACCEPTANCE_PRESERVED_RESOURCE_SECTIONS = [
  ["containers_to_restart_for_rollback", "Rollback containers"],
  ["containers_to_stop_during_cutover", "Cutover stop list"],
  ["systemd_services_to_restart_for_rollback", "Systemd services"],
  ["docker_volumes_preserved", "Docker volumes"],
  ["host_paths_preserved", "Host paths"]
] as const;

type AcceptanceEvidenceKey = typeof ACCEPTANCE_EVIDENCE_ITEMS[number][0];
type AcceptanceEvidenceState = Record<AcceptanceEvidenceKey, boolean>;
type AcceptanceEvidenceNoteState = Record<AcceptanceEvidenceKey, string>;
const EMPTY_ACCEPTANCE_EVIDENCE: AcceptanceEvidenceState = Object.fromEntries(
  ACCEPTANCE_EVIDENCE_ITEMS.map(([key]) => [key, false])
) as AcceptanceEvidenceState;
const EMPTY_ACCEPTANCE_EVIDENCE_NOTES: AcceptanceEvidenceNoteState = Object.fromEntries(
  ACCEPTANCE_EVIDENCE_ITEMS.map(([key]) => [key, ""])
) as AcceptanceEvidenceNoteState;

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
  cpu_resident_allowed?: boolean;
  cpu_resident_reason?: string;
  resolved_model?: { id: string; version: string; display_name: string } | null;
};

type ModelAliasPolicyForm = {
  enabled: boolean;
  preferred_runtime: string;
  idle_timeout_seconds: string;
  visibility_roles: string[];
  notes: string;
};

type RuntimeSmokeRuntimeSummary = {
  configured?: boolean;
  has_prompt?: boolean;
  prompt_node_count?: number | null;
  has_request?: boolean;
  request_key_count?: number | null;
  has_payload?: boolean;
  payload_key_count?: number | null;
  timeout_seconds?: number | null;
};

type RuntimeSmokeSummary = {
  configured?: boolean;
  schema?: string;
  description?: string;
  configured_runtimes?: string[];
  preferred_runtime?: string;
  preferred_runtime_configured?: boolean;
  runtimes?: Record<string, RuntimeSmokeRuntimeSummary>;
};

type RuntimeSmokeCarrier = {
  preferred_runtime?: string;
  runtime_smoke?: unknown;
  runtime_smoke_summary?: RuntimeSmokeSummary | null;
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
  runtime_smoke_summary?: RuntimeSmokeSummary | null;
  manifest?: RuntimeSmokeCarrier;
};

type ModelSmokeHookProof = {
  status?: string | null;
  reason?: string | null;
  action?: string | null;
  strategy?: string | null;
  runtime?: string | null;
  message?: string | null;
  model?: string | null;
  model_alias?: string | null;
  resolved_model_version?: string | null;
  engine?: string | null;
  placeholder?: boolean | string | number | null;
  gpu_lease_required?: boolean | null;
  measurements?: Record<string, string | number | boolean | null>;
};

type ModelSmokeTestRun = {
  id: string;
  status: string;
  runtime: string;
  model_alias?: string;
  resolved_model_version?: string;
  started_at?: string;
  completed_at?: string;
  duration_ms?: number;
  load_time_ms?: number | null;
  run_time_ms?: number | null;
  peak_vram_mib?: number | null;
  peak_ram_mib?: number | null;
  error?: string;
  reason?: string;
  hook?: ModelSmokeHookProof | null;
};

type ModelSmokeTestResult = {
  model: ModelRecord;
  smoke_test: ModelSmokeTestRun;
  measurement?: {
    resource_label?: string;
    resource_decision?: { label?: string; reason?: string };
  } | null;
  persisted: boolean;
};

type ModelAcceptanceMeasurementEntry = {
  alias: string;
  ready: boolean;
  status?: string;
  modality?: string;
  expected_runtime?: string | null;
  preferred_runtime?: string;
  runtime?: string;
  resource_label?: string;
  resolved_model_version?: string;
  display_name?: string;
  measurement_available?: boolean;
  ok_run_count?: number;
  measurements_updated_at?: string;
  latest_ok_run?: {
    peak_vram_mib?: number | null;
    peak_ram_mib?: number | null;
    run_time_ms?: number | null;
    completed_at?: string;
    hook?: ModelSmokeHookProof | null;
  };
  blockers?: string[];
};

type ModelAcceptanceMeasurementGroup = {
  id: string;
  label: string;
  status: string;
  required_aliases: string[];
  missing_aliases: string[];
  measurements: ModelAcceptanceMeasurementEntry[];
};

type ModelAcceptanceNextAction = {
  suite: string;
  suite_label?: string;
  alias: string;
  expected_runtime?: string | null;
  status?: string;
  resolved_model_version?: string;
  blockers?: string[];
  action: string;
};

type ModelAcceptanceBlockerSummary = {
  blocker: string;
  aliases: string[];
  count: number;
};

type ModelAcceptanceHandoffPlan = {
  ready: boolean;
  ready_aliases: string[];
  blocked_aliases: string[];
  ready_count: number;
  blocked_count: number;
  next_actions: ModelAcceptanceNextAction[];
  blocker_summary: ModelAcceptanceBlockerSummary[];
};

type ModelAcceptanceMeasurementCoverage = {
  status: string;
  required_aliases: string[];
  missing_aliases: string[];
  ready_aliases?: string[];
  ready_count?: number;
  blocked_count?: number;
  handoff_plan?: ModelAcceptanceHandoffPlan;
  next_actions?: ModelAcceptanceNextAction[];
  blocker_summary?: ModelAcceptanceBlockerSummary[];
  groups: ModelAcceptanceMeasurementGroup[];
};

type ModelResourceEstimate = {
  vram_gib: number;
  ram_gib: number;
  disk_gib: number;
  context_tokens?: number | null;
  max_resolution?: string | null;
  max_frames?: number | null;
};

type ModelProfile = {
  id: string;
  display_name: string;
  aliases: string[];
  modality: string;
  operations: string[];
  preferred_runtimes: string[];
  target_class: string;
  selection_guidance: string;
  resource_estimate: ModelResourceEstimate;
  target_resource_label: string;
  resource_label: string;
  resource_decision?: { label?: string; reason?: string };
  runtime_policy: string;
  default_limits?: Record<string, number | string | boolean>;
  candidate_manifest_ids?: string[];
  notes?: string[];
};

type ProfileCompatibilityReport = {
  profile_id: string;
  display_name: string;
  target_class: string;
  matched_aliases: string[];
  preferred_runtimes: string[];
  runtime_policy: string;
  target_resource_label: string;
  resource_label: string;
  status: string;
  blockers: string[];
  warnings: string[];
};

type ModelProfileDependency = {
  id: string;
  display_name?: string;
  matched_aliases: string[];
  target_class?: string;
  runtime_policy?: string;
  target_resource_label?: string;
  resource_label?: string;
  candidate_manifest?: boolean;
};

type ModelVoiceProfileDependency = {
  id: string;
  display_name?: string;
  runtime?: string;
  engine?: string;
  model_alias?: string;
  profile_type?: string;
  status?: string;
};

type ModelRuntimeReservationDependency = {
  id: string;
  owner_id?: string;
  runtime?: string;
  model_alias?: string;
  resolved_model_version?: string;
  expires_at?: string;
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
  runtime_smoke_summary?: RuntimeSmokeSummary | null;
  runtime_smoke?: unknown;
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

type NativeComfyUiJobSummary = {
  compatibility_job?: boolean;
  native_prompt_id?: string | null;
  native_prompt_recorded?: boolean;
  tracker_terminal?: boolean;
  state?: string;
  stage?: string;
  progress?: number;
  prompt?: {
    available?: boolean;
    client_id_present?: boolean;
    body_hash_present?: boolean;
    has_prompt?: boolean;
    has_client_id?: boolean;
    node_count?: number;
    class_type_count?: number;
    malformed_node_count?: number;
    unknown_top_level_key_count?: number;
    class_type_digest_present?: boolean;
    node_id_digest_present?: boolean;
    known_top_level_keys?: string[];
  };
  artifacts?: {
    artifact_count?: number;
    stored_artifact_count?: number;
    failed_ingest_count?: number;
    pending_view_artifact_count?: number;
    artifact_kinds?: string[];
    stored_bytes?: number;
  };
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

type VoiceSampleArtifact = {
  url: string;
  sha256: string;
  mime_type: string;
  bytes: number;
  filename?: string;
  sample_id?: string;
};

type VoiceProfileForm = {
  display_name: string;
  runtime: "voicebox" | "audio-cpu";
  engine: string;
  model_alias: string;
  profile_type: "preset" | "reference" | "clone";
  status: "active" | "disabled";
  visibility_roles: string;
};

const defaultVoiceProfileForm = (): VoiceProfileForm => ({
  display_name: "",
  runtime: "voicebox",
  engine: "voicebox",
  model_alias: "tts-quality",
  profile_type: "preset",
  status: "active",
  visibility_roles: "admin,operator"
});

type VoiceProfileMetadataField = {
  key: string;
  label: string;
  runtime_field?: string;
  inputMode?: React.HTMLAttributes<HTMLInputElement>["inputMode"];
  input_mode?: React.HTMLAttributes<HTMLInputElement>["inputMode"];
  accepted_json_types?: string[];
  max_string_length?: number;
};

const VOICE_PROFILE_METADATA_FIELDS: VoiceProfileMetadataField[] = [
  { key: "upstream_voice", label: "Upstream voice" },
  { key: "upstream_voice_id", label: "Voice ID" },
  { key: "upstream_profile", label: "Profile" },
  { key: "upstream_profile_id", label: "Profile ID" },
  { key: "upstream_speaker", label: "Speaker" },
  { key: "upstream_speaker_id", label: "Speaker ID" },
  { key: "language", label: "Language" },
  { key: "style", label: "Style" },
  { key: "speed", label: "Speed", inputMode: "decimal" }
] as const;

const voiceProfileMetadataInputMode = (field: VoiceProfileMetadataField): React.HTMLAttributes<HTMLInputElement>["inputMode"] => (
  field.inputMode ?? field.input_mode ?? (field.key === "speed" ? "decimal" : "text")
);

type ModelInstallPlan = {
  model_ref: string;
  status: string;
  can_install: boolean;
  blockers: string[];
  requires_license_acceptance: boolean;
  total_size_bytes: number;
  resource_decision: { label: string; reason: string };
  model: RuntimeSmokeCarrier & { display_name: string; source: { url: string; revision: string }; license: { name: string; redistribution: string } };
  files: { path: string; status: string; size_bytes: number }[];
  profile_compatibility?: ProfileCompatibilityReport[];
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
  model: RuntimeSmokeCarrier & { display_name: string; source: { url: string; revision: string }; license: { name: string; redistribution: string } };
  source_url: string;
  resource_decision: { label: string; reason: string };
  resource_override?: boolean;
  profile_compatibility?: ProfileCompatibilityReport[];
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
  model_ref?: string;
  status: string;
  stage: string;
  credential_secret_name?: string | null;
  authenticated?: boolean;
  install_ready?: boolean;
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
  active_jobs?: number;
  dependent_workflows?: unknown[];
  dependent_model_profiles?: ModelProfileDependency[];
  dependent_voice_profiles?: ModelVoiceProfileDependency[];
  active_voice_profiles?: ModelVoiceProfileDependency[];
  active_runtime_reservations?: ModelRuntimeReservationDependency[];
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

type ModelRemovalPlan = {
  model_ref: string;
  model_status: string;
  status: string;
  can_quarantine: boolean;
  blockers: string[];
  quarantine: string;
  active_jobs: number;
  dependent_workflows: unknown[];
  dependent_model_profiles: ModelProfileDependency[];
  dependent_voice_profiles: ModelVoiceProfileDependency[];
  active_voice_profiles: ModelVoiceProfileDependency[];
  active_runtime_reservations: ModelRuntimeReservationDependency[];
  model?: ModelRecord;
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
  native_comfyui?: NativeComfyUiJobSummary | null;
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
  reason_provided?: boolean;
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
  preferred_runtime?: string;
  runtimes?: string[];
  resource_label?: string;
  enabled?: boolean;
  resolved_model?: { id?: string; version?: string; display_name?: string };
};

type ComfyUiNodePin = {
  id: string;
  commit: string;
  repository_url: string;
  display_name?: string;
  status: string;
  approved_by?: string;
  approved_at?: string;
  dependency_lock_sha256?: string;
  allowed_route_prefixes?: string[];
  notes?: string;
  source: string;
  created_at?: string;
  updated_at?: string;
};

type WorkflowPreset = {
  id: string;
  display_name: string;
  description?: string;
  values: Record<string, unknown>;
};

type WorkflowExecutionSummary = {
  ready?: boolean;
  backend_policy?: string;
  runtime_policy?: string;
  runtime_candidates?: string[];
  selected_runtime?: string | null;
  locality?: string;
  server_side_comfyui_required?: boolean;
  server_side_comfyui_allowed?: boolean;
  non_comfy_allowed?: boolean;
  external_runtime_possible?: boolean;
  requires_gpu_lease?: boolean;
  queue_class?: string;
  workflow_json_node_count?: number;
  input_parameter_count?: number;
  required_parameter_count?: number;
  comfyui_parameter_mapping_count?: number;
  runtime_parameter_mapping_count?: number;
  model_dependency_count?: number;
  runtime_dependency_count?: number;
  node_dependency_count?: number;
  blocker_count?: number;
  blockers?: WorkflowDependency[];
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
  execution_summary?: WorkflowExecutionSummary;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  workflow_json: Record<string, unknown>;
  presets?: WorkflowPreset[];
  comfyui_parameter_mappings?: Array<Record<string, unknown>>;
  runtime_parameter_mappings?: Array<Record<string, unknown>>;
  created_at?: string;
  updated_at?: string;
  unpublished_at?: string | null;
};

type WorkflowTestResult = {
  status: string;
  can_submit: boolean;
  workflow: PublishedWorkflow;
  request: {
    modality: string;
    operation: string;
    model: string;
    runtime_policy: string;
    priority: string;
    input: {
      workflow_id: string;
      workflow_version: string;
      parameter_names: string[];
      parameter_count: number;
    };
  };
  dependency_status?: {
    ready?: boolean;
    dependencies?: WorkflowDependency[];
  };
  execution_summary?: WorkflowExecutionSummary;
};

const API_BASE = import.meta.env.VITE_B1_API_BASE ?? "https://api.ai.b1.germering";
const CSRF_STORAGE_KEY = "b1_ai_hub_csrf";
const apiClient = new B1ApiClient({
  baseUrl: API_BASE,
  credentials: "include",
  getCsrfToken: () => window.sessionStorage.getItem(CSRF_STORAGE_KEY)
});

function apiUrl(path: string): string {
  return apiClient.url(path as B1ApiRequestPath);
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
      code: `mkdir -p ~/.config/b1-ai-hub
install -m 600 /dev/null ~/.config/b1-ai-hub/comfyui-remote-nodes.key
# paste the scoped API key into ~/.config/b1-ai-hub/comfyui-remote-nodes.key
export B1_AI_HUB_API_BASE=${API_BASE}
export B1_AI_HUB_API_KEY_FILE=~/.config/b1-ai-hub/comfyui-remote-nodes.key
export B1_AI_HUB_DOWNLOAD_DIR=~/ComfyUI/output/b1-ai-hub
# optional when Caddy's internal CA is not trusted globally:
# export B1_AI_HUB_CA_FILE=~/.config/b1-ai-hub/b1-caddy-root.crt
python -m pip install ./integrations/comfyui-b1-remote-nodes`
    },
    {
      id: "modelhub-sync",
      label: "Model Hub Sync",
      code: `mkdir -p ~/.config/b1-ai-hub
install -m 600 /dev/null ~/.config/b1-ai-hub/modelhub-client.key
# paste the one-time Model Hub client key into ~/.config/b1-ai-hub/modelhub-client.key
python -m pip install ./integrations/b1-model-client
export B1_MODELHUB_URL=https://models.ai.b1.germering
export B1_MODELHUB_TOKEN_FILE=~/.config/b1-ai-hub/modelhub-client.key
b1-model-client plan --cache ~/.cache/b1-ai-hub/models chat-default
# add --accept-license only after reviewing gated licences in the plan
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
  return apiClient.fetch(path as B1ApiRequestPath, init);
}

async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  return apiClient.json<T>(path as B1ApiRequestPath, init);
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

async function downloadAuthenticatedFile(path: string, downloadName: string): Promise<string> {
  const response = await apiFetch(path, { method: "GET" });
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
  const link = document.createElement("a");
  link.href = url;
  link.download = downloadName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  return downloadName;
}

async function downloadAcceptanceReportFile(reportId: string, filename: AcceptanceReportFileName): Promise<string> {
  return downloadAuthenticatedFile(
    `/admin/acceptance-reports/${encodeURIComponent(reportId)}/files/${encodeURIComponent(filename)}`,
    `${reportId}-${filename}`
  );
}

async function downloadCaddyRootCertificate(): Promise<string> {
  return downloadAuthenticatedFile("/admin/tls/caddy-ca/root.crt", "b1-ai-hub-caddy-root.crt");
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

function formatGibValue(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "unavailable";
  return `${value.toFixed(value >= 10 || value === 0 ? 0 : 1)} GiB`;
}

function formatModelEstimate(estimate?: ModelResourceEstimate): string {
  if (!estimate) return "no estimate";
  const extras = [
    estimate.context_tokens ? `${estimate.context_tokens} ctx` : "",
    estimate.max_resolution ? `${estimate.max_resolution}` : "",
    estimate.max_frames ? `${estimate.max_frames} frames` : ""
  ].filter(Boolean);
  return `VRAM ${formatGibValue(estimate.vram_gib)} / RAM ${formatGibValue(estimate.ram_gib)} / disk ${formatGibValue(estimate.disk_gib)}${extras.length ? ` / ${extras.join(" / ")}` : ""}`;
}

function formatProfileLimits(limits?: ModelProfile["default_limits"]): string {
  if (!limits || !Object.keys(limits).length) return "no default limits";
  return Object.entries(limits)
    .map(([key, value]) => `${key.split("_").join(" ")} ${String(value)}`)
    .join(" / ");
}

function formatProfileCompatibility(reports?: ProfileCompatibilityReport[]): string {
  if (!reports?.length) return "no matched required profile";
  return reports.map((report) => {
    const notes = [
      `${report.display_name} ${report.status}`,
      `aliases ${report.matched_aliases.join(", ")}`,
      `resource ${report.resource_label}/${report.target_resource_label}`,
      report.blockers.length ? `${report.blockers.length} blocker${report.blockers.length === 1 ? "" : "s"}` : "",
      report.warnings.length ? `${report.warnings.length} warning${report.warnings.length === 1 ? "" : "s"}` : ""
    ].filter(Boolean);
    return notes.join(" / ");
  }).join(" ; ");
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

type NumericResourcePolicyKey = Exclude<keyof ResourcePolicyValues, "cpu_residency_enabled" | "cpu_resident_aliases">;

const RESOURCE_POLICY_FIELDS: { key: NumericResourcePolicyKey; label: string; step: string }[] = [
  { key: "gpu_total_vram_gib", label: "GPU total", step: "0.1" },
  { key: "gpu_usable_vram_gib", label: "GPU usable", step: "0.1" },
  { key: "gpu_reserve_vram_gib", label: "GPU reserve", step: "0.1" },
  { key: "gpu_max_active_pipelines", label: "GPU pipelines", step: "1" },
  { key: "host_total_ram_gib", label: "RAM total", step: "0.5" },
  { key: "host_reserve_ram_gib", label: "RAM reserve", step: "0.5" },
  { key: "cpu_residency_max_ram_gib", label: "CPU resident RAM", step: "0.1" },
  { key: "llm_default_context", label: "LLM context", step: "512" },
  { key: "llm_maximum_context", label: "LLM max context", step: "512" },
  { key: "llm_default_parallel_requests", label: "LLM parallel", step: "1" },
  { key: "comfyui_maximum_parallel_jobs", label: "Comfy jobs", step: "1" },
  { key: "comfyui_maximum_batch_size", label: "Comfy batch", step: "1" }
];

const INTEGER_POLICY_FIELDS = new Set<NumericResourcePolicyKey>([
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
const DEFAULT_CPU_RESIDENT_ALIASES = ["embedding-default", "tts-fast", "stt-default"];

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function runtimeSmokeConfigSummary(config: unknown): RuntimeSmokeRuntimeSummary {
  if (!isObjectRecord(config)) return { configured: false };
  const prompt = isObjectRecord(config.prompt) ? config.prompt : null;
  const request = isObjectRecord(config.request) ? config.request : null;
  const payload = isObjectRecord(config.payload) ? config.payload : null;
  const timeout = typeof config.timeout_seconds === "number" && config.timeout_seconds > 0 ? config.timeout_seconds : null;
  return {
    configured: true,
    has_prompt: Boolean(prompt),
    prompt_node_count: prompt ? Object.keys(prompt).length : null,
    has_request: Boolean(request),
    request_key_count: request ? Object.keys(request).length : null,
    has_payload: Boolean(payload),
    payload_key_count: payload ? Object.keys(payload).length : null,
    timeout_seconds: timeout
  };
}

function runtimeSmokeSummaryFromCarrier(carrier?: RuntimeSmokeCarrier | null): RuntimeSmokeSummary | null {
  if (!carrier) return null;
  if (carrier.runtime_smoke_summary) return carrier.runtime_smoke_summary;
  const raw = carrier.runtime_smoke;
  if (!isObjectRecord(raw)) return null;
  const configuredRuntimes = RUNTIME_OPTIONS.filter((runtime) => isObjectRecord(raw[runtime]));
  const runtimeSummaries = Object.fromEntries(
    configuredRuntimes.map((runtime) => [runtime, runtimeSmokeConfigSummary(raw[runtime])])
  );
  return {
    configured: configuredRuntimes.length > 0,
    schema: typeof raw.schema === "string" ? raw.schema : undefined,
    description: typeof raw.description === "string" ? raw.description : undefined,
    configured_runtimes: configuredRuntimes,
    preferred_runtime: carrier.preferred_runtime,
    preferred_runtime_configured: Boolean(carrier.preferred_runtime && configuredRuntimes.includes(carrier.preferred_runtime)),
    runtimes: runtimeSummaries
  };
}

function runtimeSmokeLine(carrier?: RuntimeSmokeCarrier | null): string {
  const summary = runtimeSmokeSummaryFromCarrier(carrier);
  if (!summary?.configured) return "no runtime smoke probe";
  const runtimes = summary.configured_runtimes ?? [];
  const preferred = summary.preferred_runtime;
  const selected = preferred && summary.preferred_runtime_configured ? preferred : runtimes[0];
  const detail = selected ? summary.runtimes?.[selected] : undefined;
  const shape = detail?.has_prompt
    ? `${detail.prompt_node_count ?? 0} native prompt node${detail.prompt_node_count === 1 ? "" : "s"}`
    : detail?.has_request
      ? `${detail.request_key_count ?? 0} request key${detail.request_key_count === 1 ? "" : "s"}`
      : detail?.has_payload
        ? `${detail.payload_key_count ?? 0} payload key${detail.payload_key_count === 1 ? "" : "s"}`
        : "probe payload";
  const runtimeText = summary.preferred_runtime_configured
    ? `${selected} smoke probe`
    : `smoke probe for ${runtimes.join(", ") || "runtime"}`;
  return `${runtimeText} / ${shape}${detail?.timeout_seconds ? ` / ${detail.timeout_seconds}s timeout` : ""}`;
}

function modelAcceptanceMeasurementLine(entry: ModelAcceptanceMeasurementEntry): string {
  if (!entry.measurement_available) return entry.blockers?.[0] ?? "measurement missing";
  const run = entry.latest_ok_run ?? {};
  const resource = [
    run.peak_vram_mib !== undefined && run.peak_vram_mib !== null ? `VRAM ${run.peak_vram_mib} MiB` : "",
    run.peak_ram_mib !== undefined && run.peak_ram_mib !== null ? `RAM ${run.peak_ram_mib} MiB` : "",
    run.run_time_ms !== undefined && run.run_time_ms !== null ? `run ${formatMs(run.run_time_ms)}` : ""
  ].filter(Boolean).join(" / ");
  return [
    entry.resolved_model_version || "unresolved",
    entry.runtime || "runtime unknown",
    resource,
    entry.ok_run_count ? `${entry.ok_run_count} ok run${entry.ok_run_count === 1 ? "" : "s"}` : ""
  ].filter(Boolean).join(" / ");
}

function modelSmokeHookProofLine(hook?: ModelSmokeHookProof | null): string {
  if (!hook) return "hook proof missing";
  const measurements = hook.measurements ?? {};
  const placeholder = hook.placeholder ?? measurements.placeholder;
  return [
    hook.status ? `hook ${hook.status}` : "hook status missing",
    hook.engine ? `engine ${hook.engine}` : "",
    hook.runtime ? `runtime ${hook.runtime}` : "",
    placeholder !== undefined && placeholder !== null ? `placeholder ${booleanLabel(placeholder)}` : "placeholder not reported",
    hook.strategy ? `strategy ${hook.strategy}` : "",
    hook.reason ? `reason ${hook.reason}` : ""
  ].filter(Boolean).join(" / ");
}

function Dashboard({ status, metrics }: { status: AdminStatus | null; metrics: AdminMetrics | null }) {
  const policy: Partial<ResourcePolicyValues> = status?.resource_policy ?? {};
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
  const modelSmokeCoverage = status?.acceptance_model_measurements;
  const modelSmokeMissing = modelSmokeCoverage?.missing_aliases ?? [];
  const modelSmokeDetail = modelSmokeCoverage
    ? (modelSmokeMissing.length
      ? `${modelSmokeCoverage.ready_count ?? 0}/${modelSmokeCoverage.required_aliases.length} ready, missing ${modelSmokeMissing.join(", ")}`
      : `${modelSmokeCoverage.required_aliases.length} required aliases measured`)
    : "not loaded";
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
          <Metric label="CPU residency" value={policy.cpu_residency_enabled ? "enabled" : "disabled"} detail={`${policy.cpu_resident_aliases?.length ?? 0} aliases, ${policy.cpu_residency_max_ram_gib ?? 2} GiB cap`} />
          <Metric label="Admission" value={`${formatCount(admission?.queue?.owner_queued_jobs)} queued`} detail={`${formatCount(admission?.queue?.owner_jobs_last_hour)} jobs/hour, ${formatCount(admission?.queue?.global_queued_jobs)} global`} />
          <Metric label="Model smoke" value={modelSmokeCoverage?.status ?? "unavailable"} detail={modelSmokeDetail} />
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
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [records, setRecords] = useState<ModelRecord[]>([]);
  const [downloads, setDownloads] = useState<ModelDownloadRecord[]>([]);
  const [downloadSecrets, setDownloadSecrets] = useState<EncryptedSecret[]>([]);
  const [downloadCredentialSecretName, setDownloadCredentialSecretName] = useState("");
  const [manifestUrl, setManifestUrl] = useState("");
  const [allowResourceOverride, setAllowResourceOverride] = useState(false);
  const [plan, setPlan] = useState<ModelInstallPlan | null>(null);
  const [downloadPlan, setDownloadPlan] = useState<ModelDownloadPlan | null>(null);
  const [removalPlan, setRemovalPlan] = useState<ModelRemovalPlan | null>(null);
  const [blobPlan, setBlobPlan] = useState<ModelBlobQuarantinePlan | null>(null);
  const [smokeResult, setSmokeResult] = useState<ModelSmokeTestResult | null>(null);
  const [acceptanceCoverage, setAcceptanceCoverage] = useState<ModelAcceptanceMeasurementCoverage | null>(null);
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);

  const modelVersionPath = (record: ModelRecord) => `${encodeURIComponent(record.id)}/versions/${encodeURIComponent(record.version)}`;
  const profilesByAlias = useMemo(() => {
    const byAlias: Record<string, ModelProfile> = {};
    for (const profile of profiles) {
      for (const alias of profile.aliases ?? []) {
        byAlias[alias] = profile;
      }
    }
    return byAlias;
  }, [profiles]);
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
        setProfiles(modelPayload.profiles ?? []);
        setCatalog(modelPayload.catalog ?? []);
        setRecords(modelPayload.records ?? []);
        setAcceptanceCoverage(modelPayload.acceptance_model_measurements ?? null);
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
      body: JSON.stringify({ ...body, allow_resource_override: allowResourceOverride })
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
      body: JSON.stringify({ ...body, confirm: true, accept_license: Boolean(plan?.requires_license_acceptance), allow_resource_override: allowResourceOverride })
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
      body: JSON.stringify({ ...body, allow_resource_override: allowResourceOverride })
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
        allow_resource_override: allowResourceOverride,
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

  const installDownloadedModel = (download: ModelDownloadRecord) => {
    setBusy(true);
    setMessage("installing downloaded model");
    apiFetch(`/admin/models/downloads/${download.id}/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirm: true,
        accept_license: true,
        smoke_test: false
      })
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

  const planModelRemoval = (record: ModelRecord) => {
    setBusy(true);
    setMessage("planning model quarantine");
    apiFetch(`/admin/models/${modelVersionPath(record)}/removal-plan`)
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload: ModelRemovalPlan) => {
        setRemovalPlan(payload);
        setMessage(`record quarantine ${payload.status}`);
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
      .then((payload: ModelRemovalPlan) => {
        setRemovalPlan(payload);
        setMessage(`quarantined ${record.id}@${record.version}`);
        loadModels();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const smokeTestModel = (record: ModelRecord) => {
    setBusy(true);
    setMessage(`smoke testing ${record.id}@${record.version}`);
    apiFetch(`/admin/models/${modelVersionPath(record)}/smoke-test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persist: true })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload: ModelSmokeTestResult) => {
        setSmokeResult(payload);
        setRecords((current) => current.map((item) => item.id === payload.model.id && item.version === payload.model.version ? payload.model : item));
        setMessage(`${payload.model.id}@${payload.model.version} smoke ${payload.smoke_test.status}`);
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

  const acceptanceHandoffPlan = acceptanceCoverage?.handoff_plan;
  const acceptanceNextActions = acceptanceCoverage?.next_actions ?? acceptanceHandoffPlan?.next_actions ?? [];
  const acceptanceBlockerSummary = acceptanceCoverage?.blocker_summary ?? acceptanceHandoffPlan?.blocker_summary ?? [];
  const readyAliasCount = acceptanceCoverage?.ready_count ?? acceptanceHandoffPlan?.ready_count ?? 0;
  const blockedAliasCount = acceptanceCoverage?.blocked_count ?? acceptanceHandoffPlan?.blocked_count ?? acceptanceCoverage?.missing_aliases.length ?? 0;

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
        <label className="inline-check" title="Allow an explicit admin override for resource/profile envelope exceptions">
          <input type="checkbox" checked={allowResourceOverride} onChange={(event) => setAllowResourceOverride(event.target.checked)} />
          <span>Resource override</span>
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
          <small>Profiles: {formatProfileCompatibility(plan.profile_compatibility)}</small>
          <small>{plan.runtime_views.map((view) => `${view.runtime}: ${view.container_path}`).join(" / ")}</small>
          {runtimeSmokeSummaryFromCarrier(plan.model)?.configured && <small>Smoke: {runtimeSmokeLine(plan.model)}</small>}
          {Boolean(plan.archive_inspections?.length) && <small>{plan.archive_inspections.map(formatArchiveInspection).join(" / ")}</small>}
          {plan.blockers.length > 0 && <small>{plan.blockers.join("; ")}</small>}
        </div>
      )}
      {downloadPlan && (
        <div className="one-time-key">
          <strong>{downloadPlan.model_ref} download {downloadPlan.status}</strong>
          <span>{formatBytes(downloadPlan.existing_partial_bytes)} staged / {formatBytes(downloadPlan.target_size_bytes)} total</span>
          <small>{downloadPlan.resource_decision.label}: {downloadPlan.resource_decision.reason}{downloadPlan.resource_override ? " / override requested" : ""}</small>
          <small>Profiles: {formatProfileCompatibility(downloadPlan.profile_compatibility)}</small>
          <small>{downloadPlan.model.license.name} / {downloadPlan.model.license.redistribution}{downloadPlan.requires_license_acceptance ? ` / licence ${downloadPlan.license_accepted ? "accepted" : "acceptance required"}` : ""}</small>
          <small>{downloadPlan.file_count} file{downloadPlan.file_count === 1 ? "" : "s"} from {downloadPlan.source_url}</small>
          {runtimeSmokeSummaryFromCarrier(downloadPlan.model)?.configured && <small>Smoke: {runtimeSmokeLine(downloadPlan.model)}</small>}
          {downloadPlan.files.length > 1 && <small>{downloadPlan.files.map((file) => file.path).join(" / ")}</small>}
          {downloadPlan.blockers.length > 0 && <small>{downloadPlan.blockers.join("; ")}</small>}
        </div>
      )}
      {removalPlan && (
        <div className="one-time-key">
          <strong>{removalPlan.model_ref} record quarantine {removalPlan.status}</strong>
          <span>{removalPlan.can_quarantine ? "ready for confirmed quarantine" : "blocked"} / {removalPlan.model_status}</span>
          <small>{removalPlan.quarantine}</small>
          {removalPlan.active_jobs > 0 && <small>{removalPlan.active_jobs} active job reference{removalPlan.active_jobs === 1 ? "" : "s"}</small>}
          {Boolean(removalPlan.dependent_model_profiles?.length) && <small>{removalPlan.dependent_model_profiles.map((profile) => `${profile.display_name ?? profile.id}: ${profile.matched_aliases.join(", ")}`).join(" / ")}</small>}
          {Boolean(removalPlan.dependent_workflows?.length) && <small>{removalPlan.dependent_workflows.length} dependent workflow{removalPlan.dependent_workflows.length === 1 ? "" : "s"}</small>}
          {Boolean(removalPlan.dependent_voice_profiles?.length) && <small>{removalPlan.dependent_voice_profiles.length} dependent voice profile{removalPlan.dependent_voice_profiles.length === 1 ? "" : "s"}</small>}
          {Boolean(removalPlan.active_runtime_reservations?.length) && <small>{removalPlan.active_runtime_reservations.length} active runtime reservation{removalPlan.active_runtime_reservations.length === 1 ? "" : "s"}</small>}
          {removalPlan.blockers.length > 0 && <small>{removalPlan.blockers.join("; ")}</small>}
        </div>
      )}
      {blobPlan && (
        <div className="one-time-key">
          <strong>{blobPlan.model_ref} blob quarantine {blobPlan.status}</strong>
          <span>{formatBytes(blobPlan.total_size_bytes)} recoverable cleanup candidate / {blobPlan.model_status}</span>
          <small>{blobPlan.blobs.map((blob) => `${blob.path}: ${blob.status}`).join(" / ")}</small>
          {(blobPlan.active_jobs ?? 0) > 0 && <small>{blobPlan.active_jobs ?? 0} active job reference{blobPlan.active_jobs === 1 ? "" : "s"}</small>}
          {Boolean(blobPlan.dependent_model_profiles?.length) && <small>{blobPlan.dependent_model_profiles?.map((profile) => `${profile.display_name ?? profile.id}: ${profile.matched_aliases.join(", ")}`).join(" / ")}</small>}
          {Boolean(blobPlan.dependent_workflows?.length) && <small>{blobPlan.dependent_workflows?.length} dependent workflow{blobPlan.dependent_workflows?.length === 1 ? "" : "s"}</small>}
          {Boolean(blobPlan.dependent_voice_profiles?.length) && <small>{blobPlan.dependent_voice_profiles?.length} dependent voice profile{blobPlan.dependent_voice_profiles?.length === 1 ? "" : "s"}</small>}
          {Boolean(blobPlan.active_runtime_reservations?.length) && <small>{blobPlan.active_runtime_reservations?.length} active runtime reservation{blobPlan.active_runtime_reservations?.length === 1 ? "" : "s"}</small>}
          {Boolean(blobPlan.moved?.length) && <small>{blobPlan.moved?.map((blob) => `${blob.sha256.slice(0, 12)} -> ${blob.quarantine_path}`).join(" / ")}</small>}
          {blobPlan.blockers.length > 0 && <small>{blobPlan.blockers.join("; ")}</small>}
        </div>
      )}
      {smokeResult && (
        <div className="one-time-key">
          <strong>{smokeResult.model.id}@{smokeResult.model.version} smoke {smokeResult.smoke_test.status}</strong>
          <span>
            runtime {smokeResult.smoke_test.runtime}
            {smokeResult.smoke_test.resolved_model_version ? ` / ${smokeResult.smoke_test.resolved_model_version}` : ""}
          </span>
          <small>
            load {formatMs(smokeResult.smoke_test.load_time_ms)} / run {formatMs(smokeResult.smoke_test.run_time_ms)} / VRAM {smokeResult.smoke_test.peak_vram_mib ?? "pending"} MiB / RAM {smokeResult.smoke_test.peak_ram_mib ?? "pending"} MiB
          </small>
          <small>
            {smokeResult.persisted ? "measurements persisted" : "measurements not persisted"}
            {smokeResult.measurement?.resource_label ? ` / ${smokeResult.measurement.resource_label}` : ""}
            {smokeResult.measurement?.resource_decision?.reason ? ` / ${smokeResult.measurement.resource_decision.reason}` : ""}
          </small>
          <small>Configured smoke: {runtimeSmokeLine(smokeResult.model)}</small>
          <small>Hook proof: {modelSmokeHookProofLine(smokeResult.smoke_test.hook)}</small>
          {(smokeResult.smoke_test.error || smokeResult.smoke_test.reason) && <small>{smokeResult.smoke_test.error ?? smokeResult.smoke_test.reason}</small>}
        </div>
      )}
      {acceptanceCoverage && (
        <>
          <div className="subsection-title">
            <ListChecks size={16} />
            <h3>Acceptance Measurements</h3>
          </div>
          <div className="one-time-key">
            <strong>Model-smoke coverage: {acceptanceCoverage.status}</strong>
            <small>{acceptanceCoverage.required_aliases.length} required aliases / {readyAliasCount} ready / {blockedAliasCount} blocked</small>
            <small>{acceptanceCoverage.missing_aliases.length ? `missing ${acceptanceCoverage.missing_aliases.join(", ")}` : "all measured"}</small>
            {acceptanceBlockerSummary.length > 0 && <small>{acceptanceBlockerSummary.map((item) => `${item.blocker}: ${item.aliases.join(", ")}`).join(" / ")}</small>}
          </div>
          {acceptanceNextActions.length > 0 && (
            <>
              <div className="subsection-title">
                <ListChecks size={16} />
                <h3>Model Handoff Plan</h3>
              </div>
              <table>
                <thead><tr><th>Suite</th><th>Alias</th><th>Expected Runtime</th><th>Blockers</th><th>Next Action</th></tr></thead>
                <tbody>
                  {acceptanceNextActions.map((item) => (
                    <tr key={`${item.suite}:${item.alias}:${item.action}`}>
                      <td>{item.suite_label || item.suite}</td>
                      <td><code>{item.alias}</code><small>{item.resolved_model_version || item.status || "unresolved"}</small></td>
                      <td>{item.expected_runtime ?? "any"}</td>
                      <td>{item.blockers?.length ? item.blockers.join("; ") : "none"}</td>
                      <td>{item.action}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <table>
            <thead><tr><th>Suite</th><th>Status</th><th>Alias</th><th>Measurement</th><th>Hook Proof</th><th>Blockers</th></tr></thead>
            <tbody>
              {acceptanceCoverage.groups.flatMap((group) => group.measurements.map((entry) => (
                <tr key={`${group.id}:${entry.alias}`}>
                  <td>{group.label}<small>{group.id}</small></td>
                  <td><span className={statusPillClass(entry.ready ? "ok" : "warning")}>{entry.ready ? "ready" : "missing"}</span><small>expects {entry.expected_runtime ?? "any runtime"}</small></td>
                  <td><code>{entry.alias}</code><small>{entry.status ?? "unknown"} / {entry.display_name ?? "unresolved"}</small></td>
                  <td>{modelAcceptanceMeasurementLine(entry)}{entry.measurements_updated_at && <small>updated {formatDateTime(entry.measurements_updated_at)}</small>}</td>
                  <td>{modelSmokeHookProofLine(entry.latest_ok_run?.hook)}</td>
                  <td>{entry.blockers?.length ? entry.blockers.join("; ") : "none"}</td>
                </tr>
              )))}
              {!acceptanceCoverage.groups.length && <tr><td colSpan={6}>No acceptance measurement groups loaded</td></tr>}
            </tbody>
          </table>
        </>
      )}
      <table>
        <thead><tr><th>Alias</th><th>Status</th><th>Runtime</th><th>Policy</th><th>Actions</th></tr></thead>
        <tbody>
          {aliases.map((alias) => {
            const form = aliasForms[alias.id] ?? aliasFormFromAlias(alias);
            const profile = profilesByAlias[alias.id];
            return (
              <tr key={alias.id}>
                <td>
                  <code>{alias.id}</code>
                  <small>{alias.resolved_model ? `${alias.resolved_model.id}@${alias.resolved_model.version}` : "no manifest"}</small>
                  {profile && <small>{profile.target_class}</small>}
                </td>
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
                  <small title={alias.cpu_resident_reason ?? ""}>
                    {alias.resource_label}
                    {profile ? ` / profile ${profile.resource_label}` : ""}
                    {alias.cpu_resident_candidate ? ` / ${alias.cpu_resident_allowed ? "CPU resident" : "CPU on-demand"}` : ""}
                    {" / "}
                    {alias.alias_policy_source ?? "seed"}
                  </small>
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
        <ListChecks size={16} />
        <h3>Required Profiles</h3>
      </div>
      <table>
        <thead><tr><th>Profile</th><th>Aliases</th><th>Runtime</th><th>Resource</th><th>Guidance</th></tr></thead>
        <tbody>
          {profiles.map((profile) => (
            <tr key={profile.id}>
              <td>
                <code>{profile.display_name}</code>
                <small>{profile.id}</small>
                <small>{profile.target_class}</small>
              </td>
              <td>{profile.aliases.join(", ")}<small>{profile.modality} / {profile.operations.join(", ")}</small></td>
              <td>{profile.preferred_runtimes.join(", ")}<small>{profile.runtime_policy}</small></td>
              <td>
                {profile.resource_label}
                <small>target {profile.target_resource_label}</small>
                <small>{formatModelEstimate(profile.resource_estimate)}</small>
              </td>
              <td>
                {profile.selection_guidance}
                <small>{formatProfileLimits(profile.default_limits)}</small>
                {Boolean(profile.candidate_manifest_ids?.length) && <small>candidate manifests: {profile.candidate_manifest_ids?.join(", ")}</small>}
                {Boolean(profile.notes?.length) && <small>{profile.notes?.join(" / ")}</small>}
              </td>
            </tr>
          ))}
          {!profiles.length && <tr><td colSpan={5}>No model profiles loaded</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <Boxes size={16} />
        <h3>Catalog Recommendations</h3>
      </div>
      <table>
        <thead><tr><th>Model</th><th>Status</th><th>Runtime</th><th>Smoke</th><th>Policy</th><th>Actions</th></tr></thead>
        <tbody>
          {catalog.map((model) => (
            <tr key={`${model.id}@${model.version}`}>
              <td>
                <code>{model.display_name}</code>
                <small>{model.id}@{model.version}{model.aliases?.length ? ` / ${model.aliases.join(", ")}` : ""}</small>
              </td>
              <td>{model.status}<small>{model.modality}{model.operations?.length ? ` / ${model.operations.join(", ")}` : ""}</small></td>
              <td>{model.preferred_runtime}</td>
              <td>{runtimeSmokeLine(model)}</td>
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
          {!catalog.length && <tr><td colSpan={6}>No catalog recommendations loaded</td></tr>}
        </tbody>
      </table>
      <div className="subsection-title">
        <Database size={16} />
        <h3>Installed Records</h3>
      </div>
      <table>
        <thead><tr><th>Model</th><th>Status</th><th>Runtime</th><th>Views</th><th>Smoke</th><th>Policy</th><th>Actions</th></tr></thead>
        <tbody>
          {records.map((record) => (
            <tr key={`${record.id}@${record.version}`}>
              <td><code>{record.display_name}</code><small>{record.id}@{record.version}</small></td>
              <td>{record.status}<small>{record.modality}</small></td>
              <td>{record.preferred_runtime}</td>
              <td>{(record.runtime_views ?? []).map((view) => view.runtime).join(", ") || "none"}</td>
              <td>{runtimeSmokeLine(record)}{runtimeSmokeSummaryFromCarrier(record)?.description && <small>{runtimeSmokeSummaryFromCarrier(record)?.description}</small>}</td>
              <td>{record.resource_label}</td>
              <td>
                <div className="table-actions">
                  <button title={`Run smoke test for ${record.display_name}`} onClick={() => smokeTestModel(record)} disabled={busy || record.status !== "installed"}><PlayCircle size={16} /></button>
                  <button title={`Plan model record quarantine for ${record.display_name}`} onClick={() => planModelRemoval(record)} disabled={busy || record.status !== "installed"}><ListChecks size={16} /></button>
                  <button title={`Quarantine model record for ${record.display_name}`} onClick={() => quarantineModel(record)} disabled={busy || record.status !== "installed"}><Trash2 size={16} /></button>
                  <button title={`Plan authoritative blob quarantine for ${record.display_name}`} onClick={() => planBlobQuarantine(record)} disabled={busy || record.status === "installed"}><Database size={16} /></button>
                  <button title={`Quarantine authoritative blobs for ${record.display_name}`} onClick={() => quarantineBlobs(record)} disabled={busy || record.status === "installed"}><HardDrive size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!records.length && <tr><td colSpan={7}>No installed model records</td></tr>}
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
                <button title={`Install ${download.model_ref ?? `${download.model_id}@${download.model_version}`}`} onClick={() => installDownloadedModel(download)} disabled={busy || !download.install_ready}><CheckCircle2 size={16} /></button>
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
  const [composeReadiness, setComposeReadiness] = useState<SelfTestCheck | null>(null);
  const [lifecycleChecks, setLifecycleChecks] = useState<SelfTestCheck[]>([]);
  const [composeSelection, setComposeSelection] = useState<Record<string, unknown> | null>(null);
  const [deploymentMode, setDeploymentMode] = useState("unknown");
  const [productionRequired, setProductionRequired] = useState<string[]>([]);
  const [externalConfigs, setExternalConfigs] = useState<ExternalRuntimeConfig[]>([]);
  const [externalConfigForms, setExternalConfigForms] = useState<Record<string, ExternalRuntimeConfigForm>>({});
  const [externalProvidersAllowed, setExternalProvidersAllowed] = useState(false);
  const [externalConfigMessage, setExternalConfigMessage] = useState("idle");
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [profileExport, setProfileExport] = useState("");
  const [profileMessage, setProfileMessage] = useState("idle");
  const [profilePolicyMessage, setProfilePolicyMessage] = useState("policy fallback");
  const [profileMetadataFields, setProfileMetadataFields] = useState<VoiceProfileMetadataField[]>(VOICE_PROFILE_METADATA_FIELDS);
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null);
  const [profileForm, setProfileForm] = useState<VoiceProfileForm>(defaultVoiceProfileForm);
  const [profileMetadata, setProfileMetadata] = useState("{}");
  const [profileArtifacts, setProfileArtifacts] = useState("[]");
  const [profileSampleFile, setProfileSampleFile] = useState<File | null>(null);
  const [profileSampleInputKey, setProfileSampleInputKey] = useState(0);
  const [message, setMessage] = useState("idle");
  const [busy, setBusy] = useState(false);
  const profileMetadataObject = useMemo<Record<string, unknown>>(() => {
    try {
      const parsed = profileMetadata.trim() ? JSON.parse(profileMetadata) : {};
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
    } catch {
      return {};
    }
  }, [profileMetadata]);

  const loadRuntimes = () => {
    setMessage("loading");
    apiFetch(`/admin/runtimes`)
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setHealth(payload.health ?? []);
        setReadiness(payload.readiness ?? null);
        setComposeReadiness(payload.compose_readiness ?? null);
        setLifecycleChecks(Array.isArray(payload.lifecycle_checks) ? payload.lifecycle_checks : []);
        setComposeSelection(objectOrNull(payload.compose_selection));
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

  const loadVoiceProfilePolicy = () => {
    setProfilePolicyMessage("policy loading");
    apiJson<{ object: string; safe_metadata_fields?: VoiceProfileMetadataField[] }>(`/admin/voicebox/profile-policy`)
      .then((payload) => {
        const fields = (payload.safe_metadata_fields ?? []).filter((field) => field.key && field.label);
        setProfileMetadataFields(fields.length ? fields : VOICE_PROFILE_METADATA_FIELDS);
        setProfilePolicyMessage(`${fields.length || VOICE_PROFILE_METADATA_FIELDS.length} selectors`);
      })
      .catch((error: Error) => {
        setProfileMetadataFields(VOICE_PROFILE_METADATA_FIELDS);
        setProfilePolicyMessage(`policy fallback: ${error.message}`);
      });
  };

  useEffect(() => {
    loadRuntimes();
    loadExternalConfigs();
    loadVoiceProfilePolicy();
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

  const updateProfileForm = <K extends keyof VoiceProfileForm>(field: K, value: VoiceProfileForm[K]) => {
    setProfileForm((current) => ({ ...current, [field]: value }));
  };

  const profileMetadataFieldValue = (key: string): string => {
    const value = profileMetadataObject[key];
    if (value === undefined || value === null) return "";
    return String(value);
  };

  const updateProfileMetadataField = (key: string, value: string) => {
    const next = { ...profileMetadataObject };
    const trimmed = value.trim();
    if (!trimmed) {
      delete next[key];
    } else if (key === "speed") {
      const numeric = Number(trimmed);
      next[key] = Number.isFinite(numeric) ? numeric : trimmed;
    } else {
      next[key] = trimmed;
    }
    setProfileMetadata(JSON.stringify(next, null, 2));
  };

  const profileFormFromProfile = (profile: VoiceProfile): VoiceProfileForm => ({
    display_name: profile.display_name,
    runtime: profile.runtime,
    engine: profile.engine,
    model_alias: profile.model_alias,
    profile_type: profile.profile_type,
    status: profile.status === "disabled" ? "disabled" : "active",
    visibility_roles: profile.visibility_roles.join(",")
  });

  const profilePayloadFromForm = () => {
    const metadata = profileMetadata.trim() ? JSON.parse(profileMetadata) : {};
    const sample_artifacts = profileArtifacts.trim() ? JSON.parse(profileArtifacts) : [];
    if (!metadata || Array.isArray(metadata) || typeof metadata !== "object") throw new Error("metadata must be an object");
    if (!Array.isArray(sample_artifacts)) throw new Error("sample artifacts must be an array");
    return {
      ...profileForm,
      display_name: profileForm.display_name.trim(),
      engine: profileForm.engine.trim(),
      model_alias: profileForm.model_alias.trim(),
      visibility_roles: profileForm.visibility_roles.split(",").map((role) => role.trim()).filter(Boolean),
      metadata,
      sample_artifacts
    };
  };

  const appendProfileSampleArtifact = (artifact: VoiceSampleArtifact) => {
    const sample_artifacts = profileArtifacts.trim() ? JSON.parse(profileArtifacts) : [];
    if (!Array.isArray(sample_artifacts)) throw new Error("sample artifacts must be an array");
    sample_artifacts.push({
      url: artifact.url,
      sha256: artifact.sha256,
      mime_type: artifact.mime_type,
      bytes: artifact.bytes
    });
    setProfileArtifacts(JSON.stringify(sample_artifacts, null, 2));
  };

  const resetVoiceProfileForm = () => {
    setSelectedProfileId(null);
    setProfileForm(defaultVoiceProfileForm());
    setProfileMetadata("{}");
    setProfileArtifacts("[]");
    setProfileSampleFile(null);
    setProfileSampleInputKey((current) => current + 1);
  };

  const editVoiceProfile = (profile: VoiceProfile) => {
    setSelectedProfileId(profile.id);
    setProfileForm(profileFormFromProfile(profile));
    setProfileMetadata(JSON.stringify(profile.metadata ?? {}, null, 2));
    setProfileArtifacts(JSON.stringify(profile.sample_artifacts ?? [], null, 2));
    setProfileExport("");
    setProfileMessage(`editing ${profile.id}`);
  };

  const saveVoiceProfile = (profileId: string | null) => {
    setBusy(true);
    setProfileMessage(profileId ? `updating ${profileId}` : "creating");
    try {
      const payload = profilePayloadFromForm();
      apiJson<VoiceProfile>(profileId ? `/admin/voicebox/profiles/${encodeURIComponent(profileId)}` : `/admin/voicebox/profiles`, {
        method: profileId ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      })
        .then((profile) => {
          setProfileMessage(`${profileId ? "updated" : "created"} ${profile.id}`);
          setProfileExport("");
          resetVoiceProfileForm();
          loadVoiceProfiles();
        })
        .catch((error: Error) => setProfileMessage(error.message))
        .finally(() => setBusy(false));
    } catch (error) {
      setProfileMessage(error instanceof Error ? error.message : "invalid profile payload");
      setBusy(false);
    }
  };

  const createVoiceProfile = () => {
    saveVoiceProfile(null);
  };

  const updateVoiceProfile = () => {
    saveVoiceProfile(selectedProfileId);
  };

  const uploadVoiceSampleArtifact = () => {
    if (!profileSampleFile) return;
    const file = profileSampleFile;
    setBusy(true);
    setProfileMessage(`uploading ${file.name}`);
    const headers = new Headers();
    headers.set("Content-Type", file.type || "application/octet-stream");
    headers.set("X-B1-Filename", file.name.replace(/[^\x20-\x7e]/g, "_"));
    apiFetch(`/admin/voicebox/sample-artifacts`, {
      method: "POST",
      headers,
      body: file
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload: { artifact: VoiceSampleArtifact }) => {
        appendProfileSampleArtifact(payload.artifact);
        setProfileMessage(`uploaded ${payload.artifact.filename ?? file.name}`);
        setProfileSampleFile(null);
        setProfileSampleInputKey((current) => current + 1);
      })
      .catch((error: Error) => setProfileMessage(error.message))
      .finally(() => setBusy(false));
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
        if (profile.id === selectedProfileId) resetVoiceProfileForm();
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
  const composeFiles = stringList(composeSelection?.selected_file_basenames);
  const composeProfiles = stringList(composeSelection?.selected_profiles);
  const composeMissing = stringList(composeSelection?.missing_files)
    .concat(stringList(composeSelection?.missing_profiles).map((item) => `profile:${item}`));
  const readinessRows = runtimeReadinessRows(readiness);
  const lifecycleGroups = lifecycleBundles(lifecycleChecks, productionRequired);
  const lifecycleByRuntime = new Map(lifecycleGroups.map((bundle) => [bundle.runtime, bundle]));

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
        {composeReadiness && (
          <div className="one-time-key">
            <strong>Compose selection: {composeReadiness.status}</strong>
            <small>{composeReadiness.detail}</small>
            <small>files: {composeFiles.length ? composeFiles.join(", ") : "none"} / profiles: {composeProfiles.length ? composeProfiles.join(", ") : "none"}</small>
            {composeMissing.length ? <small>missing: {composeMissing.join(", ")}</small> : <small>all required runtime overlays selected</small>}
          </div>
        )}
        {lifecycleGroups.length > 0 && (
          <div className="acceptance-detail-section">
            <h4>Lifecycle Readiness</h4>
            <div className="runtime-lifecycle-grid">
              {lifecycleGroups.map((bundle) => (
                <div className="runtime-lifecycle-card" key={bundle.runtime}>
                  <div className="runtime-lifecycle-heading">
                    <code>{bundle.runtime}</code>
                    <span className={statusPillClass(bundle.aggregateStatus)}>{bundle.aggregateStatus}</span>
                    <small>{bundle.required ? "production required" : "policy optional"}</small>
                  </div>
                  <div className="runtime-lifecycle-checks">
                    <div>
                      <span>Build</span>
                      <strong className={statusPillClass(bundle.buildCheck?.status ?? "missing")}>{bundle.buildCheck?.status ?? "missing"}</strong>
                    </div>
                    <div>
                      <span>Status</span>
                      <strong className={statusPillClass(bundle.statusCheck?.status ?? "missing")}>{bundle.statusCheck?.status ?? "missing"}</strong>
                    </div>
                  </div>
                  <ul className="runtime-evidence-list">
                    {(bundle.highlights.length ? bundle.highlights : ["no lifecycle payload highlights reported"]).slice(0, 6).map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                  {bundle.blockers.length > 0 && (
                    <ul className="runtime-evidence-list runtime-blocker-list">
                      {bundle.blockers.slice(0, 4).map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
        {readinessRows.length > 0 && (
          <div className="acceptance-detail-section">
            <h4>Production Runtime Triage</h4>
            <table>
              <thead><tr><th>Runtime</th><th>Health</th><th>Service</th><th>Images</th><th>Blockers</th><th>Next Action</th></tr></thead>
              <tbody>
                {readinessRows.filter((row) => row.required).map((row) => (
                  <tr key={row.runtime}>
                    <td><code>{row.runtime}</code></td>
                    <td>
                      <span className={statusPillClass(row.ready ? "ok" : row.healthStatus)}>{row.healthStatus}</span>
                      {row.healthPlaceholder && <small>placeholder health</small>}
                    </td>
                    <td>{row.serviceObserved ? `${row.activeContainerCount} active container${row.activeContainerCount === 1 ? "" : "s"}` : "not observed"}</td>
                    <td>{row.containerImages.length ? row.containerImages.join(", ") : "none"}</td>
                    <td>{row.blockers.length ? row.blockers.concat(row.placeholderReasons).join("; ") : "none"}</td>
                    <td>{row.remediation.length ? row.remediation.join("; ") : "ready"}</td>
                  </tr>
                ))}
                {!readinessRows.some((row) => row.required) && <tr><td colSpan={6}>No production-required runtime rows recorded</td></tr>}
              </tbody>
            </table>
          </div>
        )}
        {lifecycleChecks.length > 0 && (
          <div className="acceptance-detail-section">
            <h4>Lifecycle Hook Evidence</h4>
            <table>
              <thead><tr><th>Check</th><th>Status</th><th>Runtime</th><th>Required</th><th>Detail</th></tr></thead>
              <tbody>
                {lifecycleChecks.map((check) => {
                  const data = objectOrNull(check.data);
                  const runtime = typeof data?.runtime === "string" ? data.runtime : check.name.replace(/^runtime:/, "").split("-")[0];
                  const required = typeof data?.required === "boolean" ? data.required : productionRequired.includes(runtime);
                  return (
                    <tr key={check.name}>
                      <td><code>{check.name}</code></td>
                      <td><span className={statusPillClass(check.status)}>{check.status}</span></td>
                      <td>{runtime}</td>
                      <td>{required ? "yes" : "no"}</td>
                      <td>{check.detail}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {runtimes.map((runtime) => {
        const details = detailRecord(runtime.details);
        const reportedCapabilities = runtime.capabilities ?? (details.capabilities as RuntimeAdapterCapabilities | undefined);
        const capabilities = runtimeCapabilitySummary(reportedCapabilities) || capabilitySummary(details.capabilities);
        const contract = runtime.adapter_contract;
        const surfaces = contract?.surfaces;
        const lifecycle = lifecycleByRuntime.get(runtime.name);
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
                {lifecycle && (
                  <tr>
                    <td>Lifecycle</td>
                    <td>
                      <span className={statusPillClass(lifecycle.aggregateStatus)}>{lifecycle.aggregateStatus}</span>
                      <small>{lifecycle.checks.map((check) => `${check.name.replace(/^runtime:/, "")} ${check.status}`).join(" / ")}</small>
                    </td>
                  </tr>
                )}
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
          <button title="Refresh voice profiles" onClick={() => { loadVoiceProfilePolicy(); loadVoiceProfiles(); }} disabled={busy}><RefreshCw size={16} />Refresh</button>
          <span className="toolbar-status">{profileMessage}</span>
          <span className="toolbar-status">{profilePolicyMessage}</span>
        </div>
        <div className="split voice-profile-admin">
          <div className="stack">
            <label>Display name<input value={profileForm.display_name} onChange={(event) => updateProfileForm("display_name", event.target.value)} /></label>
            <label>Runtime
              <select value={profileForm.runtime} onChange={(event) => updateProfileForm("runtime", event.target.value as VoiceProfileForm["runtime"])}>
                <option value="voicebox">voicebox</option>
                <option value="audio-cpu">audio-cpu</option>
              </select>
            </label>
            <label>Engine<input value={profileForm.engine} onChange={(event) => updateProfileForm("engine", event.target.value)} /></label>
            <label>Model alias<input value={profileForm.model_alias} onChange={(event) => updateProfileForm("model_alias", event.target.value)} /></label>
            <label>Profile type
              <select value={profileForm.profile_type} onChange={(event) => updateProfileForm("profile_type", event.target.value as VoiceProfileForm["profile_type"])}>
                <option value="preset">preset</option>
                <option value="reference">reference</option>
                <option value="clone">clone</option>
              </select>
            </label>
            <label>Status
              <select value={profileForm.status} onChange={(event) => updateProfileForm("status", event.target.value as VoiceProfileForm["status"])}>
                <option value="active">active</option>
                <option value="disabled">disabled</option>
              </select>
            </label>
            <label>Visibility roles<input value={profileForm.visibility_roles} onChange={(event) => updateProfileForm("visibility_roles", event.target.value)} /></label>
            <div className="metadata-grid">
              {profileMetadataFields.map((field) => (
                <label key={field.key}>{field.label}
                  <input
                    inputMode={voiceProfileMetadataInputMode(field)}
                    value={profileMetadataFieldValue(field.key)}
                    onChange={(event) => updateProfileMetadataField(field.key, event.target.value)}
                  />
                </label>
              ))}
            </div>
            <label>Metadata JSON<textarea value={profileMetadata} onChange={(event) => setProfileMetadata(event.target.value)} /></label>
            <label>Sample upload<input key={profileSampleInputKey} type="file" accept="audio/wav,audio/mpeg,audio/ogg,audio/*" onChange={(event) => setProfileSampleFile(event.target.files?.[0] ?? null)} /></label>
            <button title="Upload voice reference sample" onClick={uploadVoiceSampleArtifact} disabled={busy || !profileSampleFile}><Upload size={16} />Upload Sample</button>
            <label>Sample artifacts JSON<textarea value={profileArtifacts} onChange={(event) => setProfileArtifacts(event.target.value)} /></label>
            <div className="table-actions">
              {selectedProfileId ? (
                <>
                  <button title={`Save voice profile ${selectedProfileId}`} onClick={updateVoiceProfile} disabled={busy || !profileForm.display_name.trim()}><CheckCircle2 size={16} />Save</button>
                  <button title="Cancel voice profile edit" onClick={resetVoiceProfileForm} disabled={busy}><RotateCcw size={16} />Cancel</button>
                </>
              ) : (
                <button title="Create voice profile" onClick={createVoiceProfile} disabled={busy || !profileForm.display_name.trim()}><Upload size={16} />Create</button>
              )}
            </div>
            {selectedProfileId && <small>Editing {selectedProfileId}</small>}
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
                      <button title={`Edit ${profile.id}`} onClick={() => editVoiceProfile(profile)} disabled={busy}><Pencil size={16} /></button>
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

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function shortRef(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  const digest = text.match(/sha256:([0-9a-f]{64})$/i);
  if (digest) return `sha256:${digest[1].slice(0, 12)}`;
  if (/^[0-9a-f]{40,64}$/i.test(text)) return text.slice(0, 12);
  return text.length > 48 ? `${text.slice(0, 45)}...` : text;
}

function checkRuntimeName(check: SelfTestCheck): string {
  const data = objectOrNull(check.data);
  if (typeof data?.runtime === "string" && data.runtime.trim()) return data.runtime.trim();
  const name = check.name.replace(/^runtime:/, "");
  const matched = RUNTIME_OPTIONS.find((runtime) => name.startsWith(`${runtime}-`));
  return matched ?? name.split("-")[0] ?? "runtime";
}

function lifecyclePayload(check: SelfTestCheck | undefined, key: "build_info" | "status"): Record<string, unknown> | null {
  return objectOrNull(objectOrNull(check?.data)?.[key]);
}

function aggregateLifecycleStatus(checks: SelfTestCheck[]): string {
  const statuses = checks.map((check) => check.status);
  if (statuses.some((status) => ["failed", "invalid", "error"].some((marker) => status.includes(marker)))) return "failed";
  if (statuses.some((status) => ["warning", "disabled", "pending", "degraded"].some((marker) => status.includes(marker)))) return "warning";
  if (statuses.length && statuses.every((status) => ["ok", "ready", "healthy", "eligible", "configured"].includes(status))) return "ok";
  return statuses.length ? "mixed" : "missing";
}

function operationStatusLine(operations: Record<string, unknown> | null): string {
  if (!operations) return "";
  const enabled = Object.entries(operations)
    .filter(([, value]) => value === true)
    .map(([name]) => name);
  const disabled = Object.entries(operations)
    .filter(([, value]) => value === false)
    .map(([name]) => name);
  return [
    enabled.length ? `operations enabled: ${enabled.join(", ")}` : "",
    disabled.length ? `disabled: ${disabled.join(", ")}` : ""
  ].filter(Boolean).join(" / ");
}

function lifecycleActionsLine(statusPayload: Record<string, unknown> | null): string {
  const capabilities = objectOrNull(statusPayload?.capabilities);
  const actions = stringList(capabilities?.actions);
  return actions.length ? `hooks: ${actions.join(", ")}` : "";
}

function buildInfoLine(buildInfo: Record<string, unknown> | null): string {
  if (!buildInfo) return "";
  const parts = [
    buildInfo.pinned !== undefined ? `pinned ${booleanLabel(buildInfo.pinned)}` : "",
    typeof buildInfo.runtime_version === "string" ? `runtime ${shortRef(buildInfo.runtime_version)}` : "",
    typeof buildInfo.hook_version === "string" ? `hook ${shortRef(buildInfo.hook_version)}` : "",
    typeof buildInfo.upstream_version === "string" ? `upstream ${shortRef(buildInfo.upstream_version)}` : "",
    typeof buildInfo.upstream_commit === "string" ? `commit ${shortRef(buildInfo.upstream_commit)}` : "",
    typeof buildInfo.source_archive_sha256 === "string" ? `source ${shortRef(buildInfo.source_archive_sha256)}` : "",
    typeof buildInfo.base_image === "string" ? `base ${shortRef(buildInfo.base_image)}` : "",
    typeof buildInfo.upstream_image === "string" ? `image ${shortRef(buildInfo.upstream_image)}` : "",
    typeof buildInfo.piper_release === "string" ? `Piper ${shortRef(buildInfo.piper_release)}` : "",
    typeof buildInfo.piper_asset_sha256 === "string" ? `Piper asset ${shortRef(buildInfo.piper_asset_sha256)}` : ""
  ].filter(Boolean);
  return parts.length ? `build: ${parts.join(" / ")}` : "build: metadata reported";
}

function audioCpuLifecycleLines(statusPayload: Record<string, unknown>): string[] {
  const capabilities = objectOrNull(statusPayload.capabilities);
  const engines = objectOrNull(statusPayload.engines);
  const placeholder = objectOrNull(statusPayload.placeholder);
  const residency = objectOrNull(statusPayload.cpu_residency);
  const headroom = objectOrNull(residency?.headroom);
  const engineLine = engines
    ? Object.entries(engines)
      .map(([operation, raw]) => {
        const probe = objectOrNull(raw);
        const engine = typeof probe?.engine === "string" ? probe.engine : "unknown";
        const availability = probe?.available === true ? "ready" : "unavailable";
        const marker = probe?.placeholder === true ? "placeholder" : availability;
        return `${operation} ${engine} ${marker}`;
      })
      .join(" / ")
    : "";
  return [
    statusPayload.gpu_lease_required === false ? "lease: CPU path, no GPU lease" : "lease: GPU requirement unclear",
    operationStatusLine(objectOrNull(capabilities?.operations)),
    engineLine ? `engines: ${engineLine}` : "",
    placeholder ? `placeholder: ${placeholder.enabled === true ? "enabled" : "disabled"}${stringList(placeholder.operations).length ? ` for ${stringList(placeholder.operations).join(", ")}` : ""}` : "",
    residency ? `CPU residency: ${residency.enabled === true ? "enabled" : "disabled"} / headroom ${headroom?.ok === true ? "ok" : "not ready"}${numberOrNull(headroom?.reserve_ram_gib) !== null ? ` / reserve ${numberOrNull(headroom?.reserve_ram_gib)} GiB` : ""}` : ""
  ].filter(Boolean);
}

function localAiLifecycleLines(statusPayload: Record<string, unknown>): string[] {
  const guardrails = objectOrNull(statusPayload.guardrails);
  const modelProbe = objectOrNull(statusPayload.model_probe);
  return [
    guardrails ? `guardrails: ${guardrails.status ?? "unknown"} / max backends ${guardrails.max_active_backends ?? "unknown"} / watchdog ${booleanLabel(guardrails.watchdog_idle)}` : "",
    modelProbe ? `model probe: ${modelProbe.status ?? "unknown"} / ${modelProbe.model_count ?? 0} model${modelProbe.model_count === 1 ? "" : "s"}` : "",
    lifecycleActionsLine(statusPayload)
  ].filter(Boolean);
}

function comfyUiLifecycleLines(statusPayload: Record<string, unknown>): string[] {
  const queue = objectOrNull(statusPayload.queue);
  const memory = objectOrNull(statusPayload.memory);
  const modelFolders = objectOrNull(statusPayload.model_folders);
  return [
    queue ? `queue: ${queue.running ?? 0} running / ${queue.queued ?? 0} queued / ${queue.tasks_remaining ?? 0} remaining` : "",
    memory ? `memory probe: ${memory.available === true ? "available" : "unavailable"}` : "",
    modelFolders ? `model folders: ${modelFolders.folder_count ?? 0} folders / ${modelFolders.file_count ?? 0} files` : "",
    lifecycleActionsLine(statusPayload)
  ].filter(Boolean);
}

function voiceboxLifecycleLines(statusPayload: Record<string, unknown>): string[] {
  const process = objectOrNull(statusPayload.process);
  const inventory = objectOrNull(statusPayload.model_inventory);
  return [
    process ? `process: ${process.running === true ? "running" : "stopped"}` : "",
    typeof statusPayload.active_requests === "number" ? `active requests: ${statusPayload.active_requests}` : "",
    inventory ? `model inventory: ${inventory.entry_count ?? 0} entries / ${inventory.available_root_count ?? 0} roots` : "",
    lifecycleActionsLine(statusPayload)
  ].filter(Boolean);
}

function runtimeLifecycleHighlights(runtime: string, buildInfo: Record<string, unknown> | null, statusPayload: Record<string, unknown> | null): string[] {
  const lines = [buildInfoLine(buildInfo)].filter(Boolean);
  if (!statusPayload) return lines;
  if (runtime === "audio-cpu") return lines.concat(audioCpuLifecycleLines(statusPayload));
  if (runtime === "localai") return lines.concat(localAiLifecycleLines(statusPayload));
  if (runtime === "comfyui") return lines.concat(comfyUiLifecycleLines(statusPayload));
  if (runtime === "voicebox") return lines.concat(voiceboxLifecycleLines(statusPayload));
  return lines.concat(lifecycleActionsLine(statusPayload)).filter(Boolean);
}

function lifecycleBundles(checks: SelfTestCheck[], requiredRuntimes: string[]): RuntimeLifecycleBundle[] {
  const byRuntime = new Map<string, SelfTestCheck[]>();
  checks.forEach((check) => {
    const runtime = checkRuntimeName(check);
    byRuntime.set(runtime, [...(byRuntime.get(runtime) ?? []), check]);
  });
  return Array.from(byRuntime.entries())
    .map(([runtime, runtimeChecks]) => {
      const buildCheck = runtimeChecks.find((check) => check.name.includes("build-info"));
      const statusCheck = runtimeChecks.find((check) => check.name.endsWith("-status"));
      const required = requiredRuntimes.includes(runtime) || runtimeChecks.some((check) => objectOrNull(check.data)?.required === true);
      const blockers = runtimeChecks
        .filter((check) => !["ok", "ready", "healthy", "eligible", "configured"].includes(check.status))
        .map((check) => `${check.name.replace(/^runtime:/, "")}: ${check.detail}`);
      if (required && !buildCheck) blockers.push("build-info hook missing");
      if (required && !statusCheck) blockers.push("status hook missing");
      return {
        runtime,
        required,
        aggregateStatus: aggregateLifecycleStatus(runtimeChecks),
        checks: runtimeChecks,
        buildCheck,
        statusCheck,
        highlights: runtimeLifecycleHighlights(runtime, lifecyclePayload(buildCheck, "build_info"), lifecyclePayload(statusCheck, "status")),
        blockers
      };
    })
    .sort((a, b) => {
      const requiredOrder = Number(b.required) - Number(a.required);
      return requiredOrder || RUNTIME_OPTIONS.indexOf(a.runtime) - RUNTIME_OPTIONS.indexOf(b.runtime) || a.runtime.localeCompare(b.runtime);
    });
}

function runtimeReadinessRows(check: SelfTestCheck | null): RuntimeReadinessRow[] {
  const data = objectOrNull(check?.data) ?? {};
  const rows = Array.isArray(data.runtime_readiness) ? data.runtime_readiness : [];
  return rows
    .map((item) => objectOrNull(item))
    .filter((item): item is Record<string, unknown> => item !== null)
    .map((item) => ({
      runtime: String(item.runtime ?? ""),
      required: item.required === true,
      ready: item.ready === true,
      healthStatus: String(item.health_status ?? "unknown"),
      healthPlaceholder: item.health_placeholder === true,
      serviceObserved: item.service_observed === true,
      activeContainerCount: typeof item.active_container_count === "number" ? item.active_container_count : 0,
      containerImages: stringList(item.container_images),
      placeholderReasons: stringList(item.placeholder_reasons),
      blockers: stringList(item.blockers),
      remediation: stringList(item.remediation)
    }))
    .filter((item) => item.runtime);
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

function acceptanceHandoffCommandRows(report: Record<string, unknown>): HandoffCommandDetail[] {
  const handoff = objectOrNull(report.handoff) ?? {};
  const commands = Array.isArray(handoff.commands) ? handoff.commands : [];
  return commands
    .map((item) => objectOrNull(item))
    .filter((item): item is Record<string, unknown> => item !== null)
    .map((item) => ({
      key: String(item.key ?? ""),
      command: String(item.command ?? "")
    }))
    .filter((item) => item.key || item.command);
}

function liveEvidenceProofDetails(snapshot: Record<string, unknown>): string[] {
  const details: string[] = [];
  for (const [field, label] of ACCEPTANCE_LIVE_EVIDENCE_DETAIL_FIELDS) {
    for (const value of stringList(snapshot[field])) {
      details.push(`${label}: ${value}`);
    }
  }
  return details;
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
      details: liveEvidenceProofDetails(snapshot),
      samples: stringList(snapshot.sample_labels)
    };
  });
}

function acceptanceModelMeasurementRows(report: Record<string, unknown>): ReportModelMeasurementDetail[] {
  const coverage = objectOrNull(report.model_measurement_coverage) ?? {};
  const groups = Array.isArray(coverage.groups) ? coverage.groups : [];
  const rows: ReportModelMeasurementDetail[] = [];
  groups.forEach((groupValue, groupIndex) => {
    const group = objectOrNull(groupValue);
    if (!group) return;
    const groupLabel = typeof group.label === "string" && group.label ? group.label : String(group.id ?? `group-${groupIndex + 1}`);
    const measurements = Array.isArray(group.measurements) ? group.measurements : [];
    measurements.forEach((entryValue, index) => {
      const entry = objectOrNull(entryValue);
      if (!entry) return;
      const alias = String(entry.alias ?? "");
      const runtime = String(entry.runtime ?? entry.expected_runtime ?? entry.preferred_runtime ?? "unknown");
      const latestRun = objectOrNull(entry.latest_ok_run) ?? {};
      const hookProof = objectOrNull(latestRun.hook) as ModelSmokeHookProof | null;
      rows.push({
        key: `${groupLabel}:${alias || index}`,
        group: groupLabel,
        alias: alias || "unknown",
        ready: entry.ready === true,
        status: entry.ready === true ? "ready" : String(entry.status ?? "blocked"),
        runtime,
        resolvedModel: String(entry.resolved_model_version ?? "not resolved"),
        okRuns: String(entry.ok_run_count ?? 0),
        hookProof: modelSmokeHookProofLine(hookProof),
        blockers: stringList(entry.blockers)
      });
    });
  });
  return rows;
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

function acceptanceCutoverNetworkRows(report: Record<string, unknown>): CutoverNetworkDetail[] {
  const preservation = objectOrNull(report.cutover_preservation) ?? {};
  const targetIdentity = objectOrNull(preservation.target_identity_readiness) ?? {};
  const networking = objectOrNull(preservation.networking_readiness) ?? {};

  const targetWarnings = stringList(targetIdentity.warnings);
  const targetMatches = ["hostname_matches_expected", "fqdn_matches_expected", "platform_node_matches_expected"]
    .some((key) => targetIdentity[key] === true);
  const observedNames = [
    targetIdentity.observed_hostname,
    targetIdentity.observed_fqdn,
    targetIdentity.observed_platform_node
  ]
    .map((value) => String(value ?? "").trim())
    .filter(Boolean);
  const targetReady = (
    targetIdentity.available === true
    && targetIdentity.accepted === true
    && targetIdentity.hostname_authority === "b1-appliance-config"
    && targetIdentity.operator_must_review_target_identity !== true
    && targetMatches
    && targetWarnings.length === 0
  );

  const directDhcpRoute = networking.has_dhcp_default_route === true;
  const reservationPlan = objectOrNull(networking.dhcp_reservation_plan);
  const hasDhcpNetworkProof = directDhcpRoute
    || networking.has_dhcp_network_proof === true
    || reservationPlan?.ready === true;
  const networkWarnings = stringList(networking.warnings).filter((warning) => {
    if (directDhcpRoute || !hasDhcpNetworkProof) return true;
    const lower = warning.toLowerCase();
    return !lower.includes("dhcp-owned default route") && !lower.includes("dhcp default-route evidence");
  });
  const networkReady = (
    networking.available === true
    && networking.hostname_authority === "b1-appliance-config"
    && networking.hostname_source === "system-hostname"
    && networking.network_property_source === "host-dhcp-client"
    && networking.b1_manages_host_networking === false
    && networking.b1_static_ip_configures === false
    && Number(networking.non_loopback_address_count ?? 0) > 0
    && Number(networking.default_route_address_count ?? 0) > 0
    && Number(networking.default_route_count ?? 0) > 0
    && hasDhcpNetworkProof
    && networking.operator_must_review_networking !== true
    && networkWarnings.length === 0
  );

  return [
    {
      key: "target-host",
      label: "Target hostname",
      status: targetReady ? "ready" : "review required",
      detail: [
        `authority ${String(targetIdentity.hostname_authority ?? "unknown")}`,
        `expected ${String(targetIdentity.expected_target_host ?? "unknown")}`,
        `observed ${observedNames.join(", ") || "unknown"}`
      ].join(" / "),
      warnings: targetWarnings
    },
    {
      key: "dhcp-networking",
      label: "DHCP networking",
      status: networkReady ? "ready" : "review required",
      detail: [
        `hostname source ${String(networking.hostname_source ?? "unknown")}`,
        `network properties ${String(networking.network_property_source ?? "unknown")}`,
        `static host IP ${booleanLabel(networking.b1_static_ip_configures)}`,
        `DHCP default route ${booleanLabel(networking.has_dhcp_default_route)}`,
        `DHCP proof ${String(networking.network_proof ?? (directDhcpRoute ? "direct-dhcp-default-route" : "unknown"))}`,
        `${String(networking.non_loopback_address_count ?? 0)} non-loopback address${Number(networking.non_loopback_address_count ?? 0) === 1 ? "" : "es"}`
      ].join(" / "),
      warnings: networkWarnings
    }
  ];
}

function deploymentPinValue(item: Record<string, unknown>, key: string): string {
  return typeof item[key] === "string" && item[key] ? String(item[key]) : "";
}

function acceptanceDeploymentPinRows(report: Record<string, unknown>): DeploymentPinDetail[] {
  const pins = objectOrNull(report.deployment_pins) ?? {};
  const rows: DeploymentPinDetail[] = [];
  const composeImages = Array.isArray(pins.compose_images) ? pins.compose_images : [];
  composeImages.forEach((entry, index) => {
    const item = objectOrNull(entry);
    if (!item) return;
    const service = deploymentPinValue(item, "service") || `compose-${index + 1}`;
    rows.push({
      key: `compose:${service}:${index}`,
      subject: service,
      reference: deploymentPinValue(item, "default_image") || deploymentPinValue(item, "image"),
      pin: deploymentPinValue(item, "pin_type") || "unknown",
      detail: [deploymentPinValue(item, "file"), deploymentPinValue(item, "profile")].filter(Boolean).join(" / ")
    });
  });
  const dockerfileBases = Array.isArray(pins.dockerfile_bases) ? pins.dockerfile_bases : [];
  dockerfileBases.forEach((entry, index) => {
    const item = objectOrNull(entry);
    if (!item) return;
    const component = deploymentPinValue(item, "component") || `base-${index + 1}`;
    const stage = deploymentPinValue(item, "stage") || "final";
    rows.push({
      key: `base:${component}:${stage}:${index}`,
      subject: `${component}:${stage}`,
      reference: deploymentPinValue(item, "default_image") || deploymentPinValue(item, "image"),
      pin: deploymentPinValue(item, "pin_type") || "unknown",
      detail: deploymentPinValue(item, "file")
    });
  });
  const runtimeSources = Array.isArray(pins.runtime_sources) ? pins.runtime_sources : [];
  runtimeSources.forEach((entry, index) => {
    const item = objectOrNull(entry);
    if (!item) return;
    const runtime = deploymentPinValue(item, "runtime") || `runtime-${index + 1}`;
    const version = deploymentPinValue(item, "upstream_version") || deploymentPinValue(item, "upstream_release") || deploymentPinValue(item, "engine");
    const commits = Object.entries(item)
      .filter(([key, value]) => key.endsWith("_commit") && typeof value === "string" && value)
      .map(([key, value]) => `${key}=${value}`);
    const hashes = Object.entries(item)
      .filter(([key, value]) => key.endsWith("_sha256") && typeof value === "string" && value)
      .map(([key, value]) => `${key}=${value}`);
    rows.push({
      key: `runtime:${runtime}:${index}`,
      subject: runtime,
      reference: version,
      pin: commits.length || hashes.length ? "source-pinned" : "missing-source-pin",
      detail: [...commits, ...hashes].join(" / ") || "none"
    });
  });
  return rows;
}

function acceptanceDeploymentPinFindings(report: Record<string, unknown>): string[] {
  const pins = objectOrNull(report.deployment_pins) ?? {};
  const integrity = objectOrNull(pins.integrity) ?? {};
  return ["floating_latest_refs", "unpinned_refs", "missing_runtime_pins", "missing_sections"]
    .flatMap((key) => stringList(integrity[key]).map((value) => `${labelFromKey(key)}: ${value}`));
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

function nativeComfyUiSummaryLine(summary?: NativeComfyUiJobSummary | null): string {
  if (!summary?.compatibility_job) return "";
  const prompt = summary.prompt ?? {};
  const artifacts = summary.artifacts ?? {};
  const promptState = summary.native_prompt_recorded ? (summary.native_prompt_id ?? "native prompt recorded") : "native prompt pending";
  const nodes = prompt.node_count ?? 0;
  const stored = artifacts.stored_artifact_count ?? 0;
  const total = artifacts.artifact_count ?? 0;
  const failed = artifacts.failed_ingest_count ?? 0;
  return `${promptState} / ${nodes} graph node${nodes === 1 ? "" : "s"} / ${stored}/${total} artifact${total === 1 ? "" : "s"} stored${failed ? ` / ${failed} failed ingest` : ""}`;
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
  addJobReproducibilityEntry(entries, "native ComfyUI", nativeComfyUiSummaryLine(job.native_comfyui));
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
              <td><span className={`status-pill ${reservation.status}`}>{reservation.status}</span><small>{reservation.reason_provided ? "reason recorded" : "no reason recorded"}</small></td>
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
                <td>{job.runtime}<small>{job.model_alias} / {job.resolved_model_version}</small>{job.native_comfyui && <small>{nativeComfyUiSummaryLine(job.native_comfyui)}</small>}</td>
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
          {selected.native_comfyui && (
            <section className="job-native-comfyui">
              <h3>Native ComfyUI</h3>
              <dl>
                <div><dt>Prompt</dt><dd>{nativeComfyUiSummaryLine(selected.native_comfyui)}</dd></div>
                <div><dt>Audit</dt><dd>{selected.native_comfyui.prompt?.body_hash_present ? "body hash recorded" : "body hash missing"} / {selected.native_comfyui.prompt?.client_id_present ? "client ID recorded" : "client ID absent"} / {selected.native_comfyui.prompt?.unknown_top_level_key_count ?? 0} unknown top-level key{selected.native_comfyui.prompt?.unknown_top_level_key_count === 1 ? "" : "s"}</dd></div>
                <div><dt>Artifacts</dt><dd>{selected.native_comfyui.artifacts?.artifact_kinds?.join(", ") || "none"} / {formatBytes(selected.native_comfyui.artifacts?.stored_bytes)}</dd></div>
              </dl>
            </section>
          )}
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
  const [voiceboxSampleRetentionPlan, setVoiceboxSampleRetentionPlan] = useState<VoiceboxSampleRetentionPlan | null>(null);
  const [modelQuarantinePlan, setModelQuarantinePlan] = useState<ModelQuarantineRetentionPlan | null>(null);
  const [admissionReport, setAdmissionReport] = useState<AdmissionReport | null>(null);
  const [backupSchedule, setBackupSchedule] = useState<BackupSchedule | null>(null);
  const [postgresImportPlan, setPostgresImportPlan] = useState<BackupPostgresImportPlan | null>(null);
  const [postgresImportBackup, setPostgresImportBackup] = useState("");
  const [postgresImportConfirm, setPostgresImportConfirm] = useState("");
  const [keepLast, setKeepLast] = useState("5");
  const [deleteOlderThanDays, setDeleteOlderThanDays] = useState("");
  const [artifactDeleteOlderThanDays, setArtifactDeleteOlderThanDays] = useState("30");
  const [artifactNamespaces, setArtifactNamespaces] = useState("");
  const [artifactLimit, setArtifactLimit] = useState("5000");
  const [voiceboxSampleDeleteOlderThanDays, setVoiceboxSampleDeleteOlderThanDays] = useState("30");
  const [voiceboxSampleLimit, setVoiceboxSampleLimit] = useState("5000");
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

  const runAction = (action: "create" | "verify" | "restore-test", backup?: BackupSummary) => {
    setBusy(true);
    setMessage(action);
    const url = action === "create"
      ? `${API_BASE}/admin/backups`
      : `${API_BASE}/admin/backups/${encodeURIComponent(backup?.name ?? "")}/${action}`;
    const body = action === "create"
      ? {}
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

  const runPostgresImport = (backup: BackupSummary, apply: boolean) => {
    setBusy(true);
    setMessage(apply ? "applying PostgreSQL import" : "planning PostgreSQL import");
    apiJson<BackupPostgresImportPlan>(`/admin/backups/${encodeURIComponent(backup.name)}/postgres-import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(apply ? { apply: true, confirm_backup_name: postgresImportConfirm.trim() } : { apply: false })
    })
      .then((payload) => {
        setPostgresImportPlan(payload);
        setPostgresImportBackup(backup.name);
        if (!apply) setPostgresImportConfirm("");
        setMessage(`${payload.status} PostgreSQL import for ${backup.name}`);
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

  const voiceboxSampleRetentionPayload = (confirm: boolean) => {
    const age = Number.parseInt(voiceboxSampleDeleteOlderThanDays, 10);
    const limit = Number.parseInt(voiceboxSampleLimit, 10);
    return {
      delete_older_than_days: Number.isFinite(age) ? age : 30,
      limit: Number.isFinite(limit) ? limit : 5000,
      confirm
    };
  };

  const runVoiceboxSampleRetention = (apply: boolean) => {
    setBusy(true);
    setMessage(apply ? "Voicebox sample cleanup" : "Voicebox sample retention plan");
    apiFetch(`/admin/voicebox/sample-artifacts/${apply ? "cleanup" : "retention-plan"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(voiceboxSampleRetentionPayload(apply))
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload: VoiceboxSampleRetentionPlan) => {
        setVoiceboxSampleRetentionPlan(payload);
        setMessage(`${payload.status} ${payload.candidate_count ?? payload.deleted_count ?? 0} Voicebox sample candidate${(payload.candidate_count ?? payload.deleted_count) === 1 ? "" : "s"}`);
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
  const postgresImportOperations = postgresImportPlan?.operations ?? [];
  const postgresImportRows = postgresImportPlan?.row_counts
    ? Object.entries(postgresImportPlan.row_counts).reduce((total, [, count]) => total + count, 0)
    : postgresImportOperations.reduce((total, operation) => total + operation.row_count, 0);
  const postgresImportAppliedRows = postgresImportPlan?.applied_row_counts
    ? Object.entries(postgresImportPlan.applied_row_counts).reduce((total, [, count]) => total + count, 0)
    : null;
  const postgresImportBackupSummary = backups.find((backup) => backup.name === postgresImportBackup);

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
        <Trash2 size={16} />
        <h3>Voicebox Samples</h3>
      </div>
      <div className="toolbar job-filters">
        <label>Older than<input aria-label="Delete Voicebox samples older than days" inputMode="numeric" value={voiceboxSampleDeleteOlderThanDays} onChange={(event) => setVoiceboxSampleDeleteOlderThanDays(event.target.value)} /></label>
        <label>Scan<input aria-label="Voicebox sample cleanup scan limit" inputMode="numeric" value={voiceboxSampleLimit} onChange={(event) => setVoiceboxSampleLimit(event.target.value)} /></label>
        <button title="Plan Voicebox sample cleanup" onClick={() => runVoiceboxSampleRetention(false)} disabled={busy}><ListChecks size={16} />Plan</button>
        <button title="Apply Voicebox sample cleanup" onClick={() => runVoiceboxSampleRetention(true)} disabled={busy || voiceboxSampleRetentionPlan?.status !== "planned" || !voiceboxSampleRetentionPlan?.candidate_count}><Trash2 size={16} />Cleanup</button>
      </div>
      {voiceboxSampleRetentionPlan && (
        <div className="one-time-key">
          <strong>Voicebox sample retention {voiceboxSampleRetentionPlan.status}</strong>
          <span>{voiceboxSampleRetentionPlan.candidate_count} candidate{voiceboxSampleRetentionPlan.candidate_count === 1 ? "" : "s"} / {formatBytes(voiceboxSampleRetentionPlan.total_reclaimable_bytes)} reclaimable</span>
          <small>Older than {voiceboxSampleRetentionPlan.policy.delete_older_than_days} days / root {voiceboxSampleRetentionPlan.root}</small>
          {voiceboxSampleRetentionPlan.candidates.length > 0 && <small>{voiceboxSampleRetentionPlan.candidates.slice(0, 6).map((candidate) => `${candidate.path}: ${formatBytes(candidate.size_bytes)}`).join(" / ")}</small>}
          {Boolean(voiceboxSampleRetentionPlan.deleted?.length) && <small>{voiceboxSampleRetentionPlan.deleted?.slice(0, 6).map((item) => `deleted ${item.path}`).join(" / ")}</small>}
          {voiceboxSampleRetentionPlan.invalid_preserved_count > 0 && <small>{voiceboxSampleRetentionPlan.invalid_preserved_count} invalid Voicebox sample entr{voiceboxSampleRetentionPlan.invalid_preserved_count === 1 ? "y" : "ies"} preserved</small>}
          {voiceboxSampleRetentionPlan.truncated && <small>Scan limit reached; increase the limit and plan again for remaining entries</small>}
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
                  <button title={`Plan DB import for ${backup.name}`} onClick={() => runPostgresImport(backup, false)} disabled={busy || backup.status === "invalid" || !backup.postgres_dump_included}><Database size={16} /></button>
                </div>
              </td>
            </tr>
          ))}
          {!backups.length && <tr><td colSpan={5}>No backups recorded</td></tr>}
        </tbody>
      </table>
      {postgresImportPlan && (
        <div className="backup-manifest-detail">
          <div className="subsection-title">
            <Database size={16} />
            <h3>PostgreSQL Import</h3>
          </div>
          <div className="backup-manifest-grid">
            <div><strong>Backup</strong><small>{postgresImportBackup}</small></div>
            <div><strong>Status</strong><small>{postgresImportPlan.status}</small></div>
            <div><strong>Tables</strong><small>{formatCount(postgresImportPlan.table_count ?? postgresImportOperations.length)}</small></div>
            <div><strong>Rows</strong><small>{formatCount(postgresImportAppliedRows ?? postgresImportRows)}</small></div>
            <div><strong>Created</strong><small>{formatDateTime(postgresImportPlan.created_at)}</small></div>
            <div><strong>Apply</strong><small>{postgresImportPlan.apply_supported ? "supported" : "unavailable"}</small></div>
          </div>
          <div className="toolbar job-filters">
            <label>Confirm backup<input aria-label="Confirm PostgreSQL import backup name" value={postgresImportConfirm} onChange={(event) => setPostgresImportConfirm(event.target.value)} /></label>
            <button
              title={`Apply PostgreSQL import for ${postgresImportBackup}`}
              onClick={() => postgresImportBackupSummary && runPostgresImport(postgresImportBackupSummary, true)}
              disabled={busy || !postgresImportPlan.apply_supported || !postgresImportBackupSummary || postgresImportConfirm !== postgresImportBackup}
            >
              <Database size={16} />Apply
            </button>
            <span className="toolbar-status">{postgresImportAppliedRows === null ? "dry-run plan" : `${formatCount(postgresImportAppliedRows)} rows applied`}</span>
          </div>
          <table>
            <thead><tr><th>Table</th><th>Mode</th><th>Rows</th><th>Primary Key</th></tr></thead>
            <tbody>
              {postgresImportOperations.map((operation) => (
                <tr key={operation.table}>
                  <td><code>{operation.table}</code></td>
                  <td>{operation.mode}</td>
                  <td>{formatCount(operation.row_count)}</td>
                  <td>{operation.primary_key?.join(", ") ?? ""}</td>
                </tr>
              ))}
              {!postgresImportOperations.length && <tr><td colSpan={4}>No import operations reported</td></tr>}
            </tbody>
          </table>
          <details className="manifest-raw">
            <summary>Raw Import JSON</summary>
            <pre>{JSON.stringify(postgresImportPlan, null, 2)}</pre>
          </details>
        </div>
      )}
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
  const [selectedAcceptanceReportIsPreview, setSelectedAcceptanceReportIsPreview] = useState(false);
  const [acceptanceLabel, setAcceptanceLabel] = useState("");
  const [acceptanceNotes, setAcceptanceNotes] = useState("");
  const [acceptanceEvidence, setAcceptanceEvidence] = useState<AcceptanceEvidenceState>({ ...EMPTY_ACCEPTANCE_EVIDENCE });
  const [acceptanceEvidenceNotes, setAcceptanceEvidenceNotes] = useState<AcceptanceEvidenceNoteState>({ ...EMPTY_ACCEPTANCE_EVIDENCE_NOTES });
  const [rollbackRehearsal, setRollbackRehearsal] = useState<RollbackRehearsalStatus | null>(null);
  const [openWebUiPlan, setOpenWebUiPlan] = useState<OpenWebUiMigrationPlanStatus | null>(null);
  const [openWebUiPlanNotes, setOpenWebUiPlanNotes] = useState("");
  const [backupRollbackEvidence, setBackupRollbackEvidence] = useState<BackupMigrationRollbackEvidenceStatus | null>(null);
  const [backupRollbackReviewed, setBackupRollbackReviewed] = useState(false);
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
  const [caddyCa, setCaddyCa] = useState<CaddyCaStatus | null>(null);
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
    setPolicyForm({
      ...Object.fromEntries(RESOURCE_POLICY_FIELDS.map((field) => [field.key, String(payload.effective[field.key])])),
      cpu_residency_enabled: payload.effective.cpu_residency_enabled ? "true" : "false",
      cpu_resident_aliases: (payload.effective.cpu_resident_aliases ?? DEFAULT_CPU_RESIDENT_ALIASES).join(", ")
    });
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

  const loadCaddyCa = () => {
    apiJson<CaddyCaStatus>(`/admin/tls/caddy-ca`)
      .then(setCaddyCa)
      .catch(() => setCaddyCa(null));
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

  const loadOpenWebUiPlan = () => {
    apiJson<OpenWebUiMigrationPlanStatus>(`/admin/migration/open-webui-plan`)
      .then(setOpenWebUiPlan)
      .catch(() => setOpenWebUiPlan(null));
  };

  const loadBackupRollbackEvidence = () => {
    apiJson<BackupMigrationRollbackEvidenceStatus>(`/admin/migration/backup-migration-rollback-evidence`)
      .then(setBackupRollbackEvidence)
      .catch(() => setBackupRollbackEvidence(null));
  };

  const inspectAcceptanceReport = (reportId: string) => {
    setBusy(true);
    setMessage(`loading ${reportId}`);
    apiJson<AcceptanceReportDetail>(`/admin/acceptance-reports/${encodeURIComponent(reportId)}`)
      .then((payload) => {
        setSelectedAcceptanceReport({ summary: payload.summary, report: detailRecord(payload.report) });
        setSelectedAcceptanceReportIsPreview(false);
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

  const downloadCaddyCa = () => {
    setBusy(true);
    setMessage("downloading Caddy root");
    downloadCaddyRootCertificate()
      .then((downloadName) => setMessage(`downloaded ${downloadName}`))
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const previewAcceptanceReport = () => {
    setBusy(true);
    setMessage("previewing acceptance report");
    apiJson<{ summary: AcceptanceReportSummary; report: unknown }>(`/admin/acceptance-reports/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: acceptanceLabel.trim(),
        notes: acceptanceNotes.trim(),
        operator_evidence: acceptanceEvidence,
        operator_evidence_notes: acceptanceEvidenceNotes
      })
    })
      .then((payload) => {
        setSelectedAcceptanceReport({ summary: payload.summary, report: detailRecord(payload.report) });
        setSelectedAcceptanceReportIsPreview(true);
        setMessage(payload.summary.operator_handoff_ready ? "preview handoff ready" : "preview blocked");
      })
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
        operator_evidence: acceptanceEvidence,
        operator_evidence_notes: acceptanceEvidenceNotes
      })
    })
      .then((payload) => {
        setMessage(`acceptance ${payload.summary.status}`);
        setAcceptanceReports((current) => [payload.summary, ...current.filter((item) => item.id !== payload.summary.id)].slice(0, 10));
        setSelectedAcceptanceReport({ summary: payload.summary, report: detailRecord(payload.report) });
        setSelectedAcceptanceReportIsPreview(false);
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
        loadBackupRollbackEvidence();
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const createOpenWebUiPlan = () => {
    setBusy(true);
    setMessage("creating Open WebUI migration plan");
    apiJson<OpenWebUiMigrationPlanCreateResult>(`/admin/migration/open-webui-plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes: openWebUiPlanNotes.trim() })
    })
      .then((payload) => {
        setMessage(`Open WebUI plan ${payload.summary.status ?? payload.status}`);
        setOpenWebUiPlanNotes("");
        loadOpenWebUiPlan();
        loadBackupRollbackEvidence();
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const createBackupRollbackEvidence = () => {
    setBusy(true);
    setMessage("creating backup/migration/rollback evidence");
    apiJson<BackupMigrationRollbackEvidenceCreateResult>(`/admin/migration/backup-migration-rollback-evidence`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_reviewed: backupRollbackReviewed })
    })
      .then((payload) => {
        setMessage(`backup/migration/rollback evidence ${payload.status}`);
        setBackupRollbackReviewed(false);
        loadBackupRollbackEvidence();
        loadAudit();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const resourcePolicyBody = (): ResourcePolicyValues => {
    const entries = RESOURCE_POLICY_FIELDS.map<[NumericResourcePolicyKey, number]>((field) => {
      const raw = policyForm[field.key] ?? "";
      const parsed = Number.parseFloat(raw);
      return [field.key, INTEGER_POLICY_FIELDS.has(field.key) ? Math.trunc(parsed) : parsed];
    });
    return {
      ...(Object.fromEntries(entries) as Pick<ResourcePolicyValues, NumericResourcePolicyKey>),
      cpu_residency_enabled: (policyForm.cpu_residency_enabled ?? "true") === "true",
      cpu_resident_aliases: parseCsv(policyForm.cpu_resident_aliases ?? "", DEFAULT_CPU_RESIDENT_ALIASES)
    };
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
    loadOpenWebUiPlan();
    loadRollbackRehearsal();
    loadBackupRollbackEvidence();
    loadAudit();
    loadResourcePolicy();
    loadAdmissionPolicy();
    loadNetworkPolicy();
    loadCaddyCa();
    loadMaintenance();
    loadUpdates();
  }, []);

  const selectedReport = selectedAcceptanceReport?.report ?? {};
  const selectedSummary = selectedAcceptanceReport?.summary;
  const selectedFiles = selectedSummary?.files ?? {};
  const selectedSourceControl = detailRecord(selectedReport.source_control);
  const selectedCutover = detailRecord(selectedReport.cutover_preservation);
  const selectedHandoffCommands = acceptanceHandoffCommandRows(selectedReport);
  const selectedOperatorEvidence = acceptanceOperatorEvidenceRows(selectedReport);
  const selectedLiveEvidence = acceptanceLiveEvidenceRows(selectedReport);
  const selectedModelMeasurementCoverage = detailRecord(selectedReport.model_measurement_coverage);
  const selectedModelMeasurementRows = acceptanceModelMeasurementRows(selectedReport);
  const selectedPreservedResources = acceptancePreservedResourceRows(selectedReport);
  const selectedCutoverNetworkRows = acceptanceCutoverNetworkRows(selectedReport);
  const selectedTargetHostReadiness = selectedCutoverNetworkRows.find((item) => item.key === "target-host");
  const selectedDhcpNetworkReadiness = selectedCutoverNetworkRows.find((item) => item.key === "dhcp-networking");
  const selectedDeploymentPinRows = acceptanceDeploymentPinRows(selectedReport);
  const selectedDeploymentPinFindings = acceptanceDeploymentPinFindings(selectedReport);
  const selectedDeploymentPins = detailRecord(selectedReport.deployment_pins);
  const selectedComposeSelection = detailRecord(selectedReport.compose_selection);
  const openWebUiPlanReadyCount = OPEN_WEBUI_PLAN_INPUT_LABELS.filter(([key]) => openWebUiPlan?.inputs[key]?.available).length;
  const openWebUiCurrent = openWebUiPlan?.inputs.current_plan;
  const backupRollbackInputReadyCount = BACKUP_ROLLBACK_INPUT_LABELS.filter(([key]) => backupRollbackEvidence?.inputs[key]?.available).length;
  const backupRollbackRequiredCount = BACKUP_ROLLBACK_INPUT_LABELS.length;
  const backupRollbackEvidenceReady = Boolean(backupRollbackEvidence?.ready);
  const backupRollbackEvidenceDetail = backupRollbackEvidence?.evidence.generated_at
    ? formatDateTime(backupRollbackEvidence.evidence.generated_at)
    : backupRollbackEvidence?.evidence.reason ?? "not generated";

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
            const bounds = resourcePolicy.bounds[field.key] ?? {};
            return (
              <label key={field.key}>
                {field.label}
                <input
                  type="number"
                  min={bounds.minimum ?? 0}
                  max={bounds.maximum}
                  step={field.step}
                  value={policyForm[field.key] ?? ""}
                  onChange={(event) => setPolicyForm((current) => ({ ...current, [field.key]: event.target.value }))}
                />
                <small>{bounds.minimum ?? 0}..{bounds.maximum ?? "unbounded"}</small>
              </label>
            );
          })}
          <label className="inline-check">
            <input
              type="checkbox"
              checked={(policyForm.cpu_residency_enabled ?? "true") === "true"}
              onChange={(event) => setPolicyForm((current) => ({ ...current, cpu_residency_enabled: event.target.checked ? "true" : "false" }))}
            />
            <span>CPU residency</span>
            <small>{(policyForm.cpu_residency_enabled ?? "true") === "true" ? "enabled" : "disabled"}</small>
          </label>
          <label>
            CPU resident aliases
            <input
              value={policyForm.cpu_resident_aliases ?? ""}
              onChange={(event) => setPolicyForm((current) => ({ ...current, cpu_resident_aliases: event.target.value }))}
            />
            <small>{resourcePolicy.bounds.cpu_resident_aliases?.max_items ?? 32} max</small>
          </label>
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
        <KeyRound size={16} />
        <h3>LAN TLS CA</h3>
      </div>
      <div className="toolbar">
        <button title="Refresh Caddy CA status" onClick={loadCaddyCa} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <button title="Download Caddy root certificate" onClick={downloadCaddyCa} disabled={busy || !caddyCa?.available}><Download size={16} />Download Root</button>
        <span className={`status-pill ${caddyCa?.available || caddyCa?.status === "not_required" ? "ok" : caddyCa?.status === "blocked" ? "failed" : "warning"}`}>{caddyCa?.status ?? "unknown"}</span>
        <span className="toolbar-status">{caddyCa?.path ?? "Caddy CA status unavailable"}</span>
      </div>
      <div className="metric-grid">
        <Metric
          label="Root"
          value={caddyCa?.available ? "ready" : caddyCa?.required === false ? "not required" : "missing"}
          detail={caddyCa?.modified_at ? formatDateTime(caddyCa.modified_at) : caddyCa?.blockers[0] ?? "not loaded"}
        />
        <Metric
          label="Fingerprint"
          value={caddyCa?.sha256 ? caddyCa.sha256.slice(0, 12) : "none"}
          detail={caddyCa?.fingerprint_sha256 ?? "no fingerprint"}
        />
        <Metric
          label="Size"
          value={formatBytes(caddyCa?.size_bytes ?? 0)}
          detail={caddyCa?.regular_file ? "regular file" : caddyCa?.symlink ? "symlink blocked" : "not a regular file"}
        />
        <Metric
          label="Hosts"
          value={formatCount(Object.keys(caddyCa?.hosts ?? {}).length)}
          detail={Object.entries(caddyCa?.hosts ?? {}).map(([key, host]) => `${key}:${host}`).join(", ") || "no hosts"}
        />
      </div>
      {!!caddyCa?.blockers.length && (
        <ul className="acceptance-blockers">
          {caddyCa.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
        </ul>
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
              <td>
                {update.backup_name ?? "none"}
                <small>{update.self_test?.status ? `self-test ${update.self_test.status}` : ""}</small>
                <small>
                  update health gate: {update.update_health_gate?.ready ? "ready" : "blocked"}
                  {update.update_health_gate?.blockers?.[0] ? ` / ${update.update_health_gate.blockers[0]}` : ""}
                </small>
              </td>
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
        <Database size={16} />
        <h3>Open WebUI Migration Plan</h3>
      </div>
      <div className="metric-grid">
        <Metric
          label="Inputs"
          value={`${openWebUiPlanReadyCount}/${OPEN_WEBUI_PLAN_INPUT_LABELS.length}`}
          detail={openWebUiPlan?.ready_to_generate ? "ready to generate" : "missing required artifacts"}
        />
        <Metric
          label="Current plan"
          value={openWebUiCurrent?.available ? openWebUiCurrent.status ?? "recorded" : "missing"}
          detail={openWebUiCurrent?.name ?? openWebUiCurrent?.reason ?? "not loaded"}
        />
        <Metric
          label="Strategy"
          value={openWebUiCurrent?.recommended_strategy ?? "unknown"}
          detail={openWebUiCurrent?.compatibility_status ?? "compatibility unknown"}
        />
        <Metric
          label="Warnings"
          value={formatCount(openWebUiCurrent?.warning_count ?? 0)}
          detail={`${formatCount(openWebUiCurrent?.readable_database_count ?? 0)} readable DB candidate${(openWebUiCurrent?.readable_database_count ?? 0) === 1 ? "" : "s"}`}
        />
      </div>
      <table>
        <thead><tr><th>Artifact</th><th>Status</th><th>Selected path</th></tr></thead>
        <tbody>
          {OPEN_WEBUI_PLAN_INPUT_LABELS.map(([key, label]) => {
            const item = openWebUiPlan?.inputs[key];
            return (
              <tr key={key}>
                <td>{label}</td>
                <td>{item?.available ? "ready" : "missing"}<small>{item?.name ?? item?.reason ?? item?.status ?? "not loaded"}</small></td>
                <td><code>{item?.path ?? item?.reason ?? "not loaded"}</code></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="stack">
        <label>Notes<textarea rows={2} value={openWebUiPlanNotes} onChange={(event) => setOpenWebUiPlanNotes(event.target.value)} maxLength={2000} /></label>
      </div>
      <div className="toolbar">
        <button title="Refresh Open WebUI migration plan status" onClick={loadOpenWebUiPlan} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <button title="Generate Open WebUI migration plan" onClick={createOpenWebUiPlan} disabled={busy || !openWebUiPlan?.ready_to_generate}><Database size={16} />Generate</button>
        <span className="toolbar-status">{openWebUiCurrent?.path ?? openWebUiPlan?.backup_root ?? "Open WebUI migration plan status unavailable"}</span>
      </div>
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
        <CheckCircle2 size={16} />
        <h3>Backup/Migration/Rollback Evidence</h3>
      </div>
      <div className="metric-grid">
        <Metric
          label="Inputs"
          value={`${backupRollbackInputReadyCount}/${backupRollbackRequiredCount}`}
          detail={backupRollbackEvidenceReady ? "ready" : `${formatCount(backupRollbackEvidence?.blockers.length ?? 0)} blocker${(backupRollbackEvidence?.blockers.length ?? 0) === 1 ? "" : "s"}`}
        />
        <Metric
          label="Evidence"
          value={backupRollbackEvidence?.evidence.available ? backupRollbackEvidence.evidence.status ?? "recorded" : "missing"}
          detail={backupRollbackEvidenceDetail}
        />
        <Metric
          label="Checks"
          value={formatCount(backupRollbackEvidence?.evidence.check_count ?? 0)}
          detail={`${formatCount(backupRollbackEvidence?.evidence.required_check_count ?? 9)} required`}
        />
      </div>
      <table>
        <thead><tr><th>Artifact</th><th>Status</th><th>Selected path</th></tr></thead>
        <tbody>
          {BACKUP_ROLLBACK_INPUT_LABELS.map(([key, label]) => {
            const item = backupRollbackEvidence?.inputs[key];
            return (
              <tr key={key}>
                <td>{label}</td>
                <td>{item?.available ? "ready" : "missing"}<small>{item?.name ?? item?.reason ?? "not loaded"}</small></td>
                <td><code>{item?.path ?? item?.reason ?? "not loaded"}</code></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="stack">
        <label className="evidence-item">
          <input type="checkbox" checked={backupRollbackReviewed} onChange={(event) => setBackupRollbackReviewed(event.target.checked)} />
          <span>Backup, restore, migration, cutover, and rollback artifacts reviewed</span>
        </label>
      </div>
      <div className="toolbar">
        <button title="Refresh backup/migration/rollback evidence status" onClick={loadBackupRollbackEvidence} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <button title="Generate backup/migration/rollback evidence" onClick={createBackupRollbackEvidence} disabled={busy || !backupRollbackEvidenceReady || !backupRollbackReviewed}><CheckCircle2 size={16} />Generate</button>
        <span className="toolbar-status">{backupRollbackEvidence?.evidence.path ?? backupRollbackEvidence?.backup_root ?? "evidence status unavailable"}</span>
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
            <div key={key} className="evidence-item acceptance-evidence-item">
              <label className="evidence-check">
                <input
                  type="checkbox"
                  checked={acceptanceEvidence[key]}
                  onChange={(event) => setAcceptanceEvidence((current) => ({ ...current, [key]: event.target.checked }))}
                />
                <span>{label}</span>
              </label>
              <input
                aria-label={`${label} evidence note`}
                className="evidence-note"
                value={acceptanceEvidenceNotes[key]}
                onChange={(event) => setAcceptanceEvidenceNotes((current) => ({ ...current, [key]: event.target.value }))}
                maxLength={1000}
                placeholder="source, run ID, or limitation"
              />
            </div>
          ))}
        </div>
      </div>
      <div className="toolbar">
        <button title="Preview acceptance readiness without writing report files" onClick={previewAcceptanceReport} disabled={busy}><Eye size={16} />Preview</button>
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
                <small>{report.operator_handoff_ready ? "handoff ready" : !report.deployment_pins_ready ? "deployment pins missing" : !report.compose_selection_ready ? "Compose selection missing" : !report.model_measurement_coverage_ready ? "model smoke coverage missing" : !report.operator_evidence_ready ? "evidence missing" : !report.repository_quality_evidence_ready ? "repository quality proof missing" : !report.operator_preflight_evidence_ready ? "preflight proof missing" : !report.smoke_evidence_ready ? "smoke proof missing" : !report.gpu_evidence_ready ? "GPU proof missing" : !report.localai_evidence_ready ? "LocalAI proof missing" : !report.installed_workflows_evidence_ready ? "workflow proof missing" : !report.native_comfyui_evidence_ready ? "ComfyUI proof missing" : !report.remote_nodes_evidence_ready ? "remote-node proof missing" : !report.modelhub_evidence_ready ? "Model Hub proof missing" : !report.voicebox_evidence_ready ? "Voicebox proof missing" : !report.security_evidence_ready ? "security proof missing" : !report.restart_reconciliation_evidence_ready ? "restart proof missing" : !report.backup_migration_rollback_evidence_ready ? "backup/rollback proof missing" : !report.cutover_dns_ready ? "DNS readiness missing" : !report.cutover_preservation_ready ? "rollback preservation missing" : "system blockers"}</small>
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
            <h3>{selectedAcceptanceReportIsPreview ? "Acceptance Preview Detail" : "Acceptance Report Detail"}</h3>
          </div>
          <div className="acceptance-detail-grid">
            <div><strong>Report</strong><small>{selectedSummary.id}</small></div>
            <div><strong>Status</strong><small>{selectedSummary.status}</small></div>
            <div><strong>Mode</strong><small>{selectedSummary.runtime_deployment_mode ?? "unknown"}</small></div>
            <div><strong>Generated</strong><small>{formatDateTime(selectedSummary.generated_at)}</small></div>
            <div><strong>Handoff</strong><small>{selectedSummary.operator_handoff_ready ? "ready" : "blocked"}</small></div>
            <div><strong>Cutover DNS</strong><small>{selectedSummary.cutover_dns_ready ? "ready" : "review required"}</small></div>
            <div><strong>Deployment Pins</strong><small>{selectedSummary.deployment_pins_ready ? "clean" : String(selectedDeploymentPins.status ?? "blocked")}</small></div>
            <div><strong>Compose Files</strong><small>{selectedSummary.compose_selection_ready ? "production overlays selected" : String(selectedComposeSelection.status ?? "blocked")}</small></div>
            <div><strong>Model smoke</strong><small>{selectedSummary.model_measurement_coverage_ready ? "all required aliases measured" : String(selectedModelMeasurementCoverage.status ?? "missing")}</small></div>
            <div><strong>Target Host</strong><small>{selectedTargetHostReadiness?.status ?? "review required"}</small></div>
            <div><strong>DHCP Networking</strong><small>{selectedDhcpNetworkReadiness?.status ?? "review required"}</small></div>
            <div><strong>Repository quality</strong><small>{selectedSummary.repository_quality_evidence_ready ? "quality gates recorded" : "proof missing"}</small></div>
            <div><strong>Source commit</strong><small>{String(selectedSourceControl.source_commit ?? selectedSourceControl.source_ref ?? "unavailable")}</small></div>
            <div><strong>Cutover resources</strong><small>{String(selectedCutover.resource_count ?? 0)}</small></div>
            <div><strong>Report files</strong><small>{selectedAcceptanceReportIsPreview ? "preview only" : selectedFiles.markdown ?? selectedFiles.json ?? "not written"}</small></div>
          </div>
          <div className="toolbar">
            <button title={`Download Markdown for ${selectedSummary.id}`} onClick={() => downloadAcceptanceReport(selectedSummary.id, "report.md")} disabled={busy || !selectedFiles.markdown}><Download size={16} />Markdown</button>
            <button title={`Download JSON for ${selectedSummary.id}`} onClick={() => downloadAcceptanceReport(selectedSummary.id, "report.json")} disabled={busy || !selectedFiles.json}><Download size={16} />JSON</button>
            <button title={`Download checksums for ${selectedSummary.id}`} onClick={() => downloadAcceptanceReport(selectedSummary.id, "SHA256SUMS")} disabled={busy || !selectedFiles.sha256sums}><KeyRound size={16} />Checksums</button>
            <span className="toolbar-status">{selectedAcceptanceReportIsPreview ? "Preview did not write report files or audit records" : "Downloads use the authenticated admin report file endpoint"}</span>
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
            <h4>Handoff Commands</h4>
            <table>
              <thead><tr><th>Step</th><th>Command</th></tr></thead>
              <tbody>
                {selectedHandoffCommands.map((item) => (
                  <tr key={`${item.key}:${item.command}`}>
                    <td>{item.key}</td>
                    <td><code>{item.command}</code></td>
                  </tr>
                ))}
                {!selectedHandoffCommands.length && <tr><td colSpan={2}>No handoff commands recorded</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="acceptance-detail-section">
            <h4>Target Host and DHCP</h4>
            <table>
              <thead><tr><th>Scope</th><th>Status</th><th>Evidence</th><th>Warnings</th></tr></thead>
              <tbody>
                {selectedCutoverNetworkRows.map((item) => (
                  <tr key={item.key}>
                    <td>{item.label}</td>
                    <td><span className={statusPillClass(item.status)}>{item.status}</span></td>
                    <td>{item.detail}</td>
                    <td>{item.warnings.length ? item.warnings.join("; ") : "none"}</td>
                  </tr>
                ))}
                {!selectedCutoverNetworkRows.length && <tr><td colSpan={4}>No target-host or DHCP readiness recorded</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="acceptance-detail-section">
            <h4>Deployment Pins</h4>
            <div className="acceptance-pin-summary">
              <span className={statusPillClass(selectedSummary.deployment_pins_ready ? "ok" : "warning")}>{selectedSummary.deployment_pins_ready ? "clean" : "blocked"}</span>
              <small>{String(selectedDeploymentPins.source ?? "unknown")} / {selectedDeploymentPinRows.length} pin rows</small>
              {selectedDeploymentPinFindings.length ? <small>{selectedDeploymentPinFindings.join("; ")}</small> : <small>no pin findings</small>}
              <small>Compose {String(selectedComposeSelection.status ?? "unknown")}: {stringList(selectedComposeSelection.selected_file_basenames).join(", ") || "none"}</small>
              {stringList(selectedComposeSelection.missing_files).length || stringList(selectedComposeSelection.missing_profiles).length ? <small>missing {stringList(selectedComposeSelection.missing_files).concat(stringList(selectedComposeSelection.missing_profiles).map((item) => `profile:${item}`)).join(", ")}</small> : <small>all required Compose overlays selected</small>}
            </div>
            <table>
              <thead><tr><th>Subject</th><th>Reference</th><th>Pin</th><th>Detail</th></tr></thead>
              <tbody>
                {selectedDeploymentPinRows.map((item) => (
                  <tr key={item.key}>
                    <td>{item.subject}</td>
                    <td><code>{item.reference}</code></td>
                    <td><span className={statusPillClass(item.pin === "digest" || item.pin === "source-pinned" || item.pin === "versioned-local-build" ? "ok" : "warning")}>{item.pin}</span></td>
                    <td>{item.detail || "none"}</td>
                  </tr>
                ))}
                {!selectedDeploymentPinRows.length && <tr><td colSpan={4}>No deployment pins recorded</td></tr>}
              </tbody>
            </table>
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
            <h4>Database Model-Smoke Coverage</h4>
            <div className="acceptance-pin-summary">
              <span className={statusPillClass(selectedSummary.model_measurement_coverage_ready ? "ok" : String(selectedModelMeasurementCoverage.status ?? "warning"))}>{String(selectedModelMeasurementCoverage.status ?? "missing")}</span>
              <small>{stringList(selectedModelMeasurementCoverage.required_aliases).length} required aliases</small>
              <small>{stringList(selectedModelMeasurementCoverage.missing_aliases).length ? `missing ${stringList(selectedModelMeasurementCoverage.missing_aliases).join(", ")}` : "no missing aliases recorded"}</small>
            </div>
            <table>
              <thead><tr><th>Suite</th><th>Alias</th><th>Status</th><th>Runtime</th><th>Resolved Model</th><th>OK Runs</th><th>Hook Proof</th><th>Blockers</th></tr></thead>
              <tbody>
                {selectedModelMeasurementRows.map((item) => (
                  <tr key={item.key}>
                    <td>{item.group}</td>
                    <td><code>{item.alias}</code></td>
                    <td><span className={statusPillClass(item.ready ? "ok" : item.status)}>{item.status}</span></td>
                    <td>{item.runtime}</td>
                    <td><code>{item.resolvedModel}</code></td>
                    <td>{item.okRuns}</td>
                    <td>{item.hookProof}</td>
                    <td>{item.blockers.length ? item.blockers.join("; ") : "none"}</td>
                  </tr>
                ))}
                {!selectedModelMeasurementRows.length && <tr><td colSpan={8}>No database model-smoke coverage recorded</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="acceptance-detail-section">
            <h4>Live Evidence</h4>
            <table>
              <thead><tr><th>Evidence</th><th>Status</th><th>Missing Checks</th><th>Details</th><th>Source</th></tr></thead>
              <tbody>
                {selectedLiveEvidence.map((item) => (
                  <tr key={item.key}>
                    <td>{item.label}<small>{item.samples.length ? item.samples.join(", ") : item.key}</small></td>
                    <td><span className={statusPillClass(item.available && item.status === "ok" ? "ok" : item.status)}>{item.status}</span></td>
                    <td>{item.missingChecks.length ? item.missingChecks.join(", ") : "none"}</td>
                    <td>{item.details.length ? item.details.join("; ") : "none"}</td>
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
    presets: [
      {
        id: "balanced",
        display_name: "Balanced",
        description: "Default bounded settings for a single local image job.",
        values: {
          prompt: "a clean product-style scene with natural lighting",
          steps: 20,
          seed: 0
        }
      }
    ],
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

function defaultWorkflowTestParameters(workflow: PublishedWorkflow | Record<string, unknown>): string {
  const presets = Array.isArray(workflow.presets) ? workflow.presets : [];
  const firstPreset = presets.find((preset) => preset && typeof preset === "object" && "values" in preset);
  if (firstPreset && typeof firstPreset === "object" && firstPreset.values && typeof firstPreset.values === "object") {
    return JSON.stringify(firstPreset.values, null, 2);
  }
  return "{}";
}

function workflowDraftPayload(workflow: PublishedWorkflow): Record<string, unknown> {
  return {
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
    presets: workflow.presets ?? [],
    output_schema: workflow.output_schema,
    resource_class: workflow.resource_class,
    dependencies: workflow.dependencies.map((dependency) => ({
      type: dependency.type,
      id: dependency.id,
      ...(dependency.version ? { version: dependency.version } : {})
    })),
    limits: workflow.limits,
    comfyui_parameter_mappings: workflow.comfyui_parameter_mappings ?? [],
    runtime_parameter_mappings: workflow.runtime_parameter_mappings ?? [],
    visibility_roles: workflow.visibility_roles
  };
}

function workflowExecutionLine(workflow?: PublishedWorkflow | null): string {
  const summary = workflow?.execution_summary;
  if (!summary) return workflow ? `${workflow.backend_policy} / ${workflow.runtime_policy}` : "execution unresolved";
  const runtime = summary.selected_runtime ?? summary.runtime_candidates?.join(", ") ?? "runtime unresolved";
  const backing = summary.server_side_comfyui_required
    ? "ComfyUI required"
    : summary.server_side_comfyui_allowed && summary.non_comfy_allowed
      ? "ComfyUI or non-Comfy"
      : "non-Comfy required";
  const lease = summary.requires_gpu_lease ? "GPU lease" : "no GPU lease";
  return `${summary.locality ?? "local"} / ${runtime} / ${backing} / ${lease}`;
}

function workflowExecutionStats(workflow?: PublishedWorkflow | null): string {
  const summary = workflow?.execution_summary;
  if (!summary) return "no execution summary";
  const nodes = summary.workflow_json_node_count ?? 0;
  const parameters = summary.input_parameter_count ?? 0;
  const required = summary.required_parameter_count ?? 0;
  const mappings = (summary.comfyui_parameter_mapping_count ?? 0) + (summary.runtime_parameter_mapping_count ?? 0);
  const blockers = summary.blocker_count ?? 0;
  return `${summary.queue_class ?? "queue"} / ${nodes} graph node${nodes === 1 ? "" : "s"} / ${parameters} input${parameters === 1 ? "" : "s"} (${required} required) / ${mappings} mapping${mappings === 1 ? "" : "s"} / ${blockers} blocker${blockers === 1 ? "" : "s"}`;
}

function workflowDependencyDetail(dependency: WorkflowDependency): string {
  if (dependency.type === "model") {
    const resolved = dependency.resolved_model?.version
      ? `${dependency.resolved_model.id ?? dependency.id}@${dependency.resolved_model.version}`
      : dependency.resolved_model?.id;
    const runtime = dependency.preferred_runtime ?? dependency.runtimes?.join(", ");
    return [resolved, runtime, dependency.resource_label].filter(Boolean).join(" / ");
  }
  if (dependency.type === "node") {
    return dependency.version ? dependency.version.slice(0, 12) : "";
  }
  return dependency.reason ?? "";
}

function Workflows() {
  const [workflows, setWorkflows] = useState<PublishedWorkflow[]>([]);
  const [workflowVersions, setWorkflowVersions] = useState<PublishedWorkflow[]>([]);
  const [nodePins, setNodePins] = useState<ComfyUiNodePin[]>([]);
  const [selected, setSelected] = useState<PublishedWorkflow | null>(null);
  const [selectedNodePin, setSelectedNodePin] = useState<ComfyUiNodePin | null>(null);
  const [draft, setDraft] = useState(WORKFLOW_TEMPLATE);
  const [testParameters, setTestParameters] = useState(defaultWorkflowTestParameters(JSON.parse(WORKFLOW_TEMPLATE)));
  const [workflowTest, setWorkflowTest] = useState<WorkflowTestResult | null>(null);
  const [nodePinForm, setNodePinForm] = useState({
    id: "",
    commit: "",
    repository_url: "",
    display_name: "",
    status: "approved",
    dependency_lock_sha256: "",
    allowed_route_prefixes: "",
    notes: ""
  });
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

  const loadNodePins = () => {
    apiFetch(`/admin/comfyui/node-pins`)
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => setNodePins(payload.data ?? []))
      .catch((err: Error) => setMessage(err.message));
  };

  const refreshWorkflowAdmin = () => {
    loadWorkflows();
    loadNodePins();
  };

  useEffect(refreshWorkflowAdmin, []);

  const parseDraft = () => {
    try {
      return JSON.parse(draft);
    } catch (error) {
      throw new Error(error instanceof Error ? `invalid JSON: ${error.message}` : "invalid JSON");
    }
  };

  const parseTestParameters = () => {
    try {
      const parsed = testParameters.trim() ? JSON.parse(testParameters) : {};
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("parameters must be a JSON object");
      return parsed as Record<string, unknown>;
    } catch (error) {
      throw new Error(error instanceof Error ? `invalid test parameters: ${error.message}` : "invalid test parameters");
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

  const testWorkflow = (workflow?: PublishedWorkflow) => {
    setBusy(true);
    setMessage(workflow ? `testing ${workflow.id}@${workflow.version}` : "testing draft");
    let parameters: Record<string, unknown>;
    let draftWorkflow: unknown;
    try {
      parameters = parseTestParameters();
      draftWorkflow = workflow ? null : parseDraft();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "invalid workflow test input");
      setBusy(false);
      return;
    }
    apiFetch(`/workflows/v1/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(workflow ? {
        workflow_id: workflow.id,
        workflow_version: workflow.version,
        parameters
      } : {
        workflow: draftWorkflow,
        parameters
      })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload: WorkflowTestResult) => {
        setWorkflowTest(payload);
        setValidation(payload.workflow);
        setMessage(`${payload.workflow.id}@${payload.workflow.version} test ${payload.status}`);
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

  const loadWorkflowVersions = (workflow: PublishedWorkflow, options: { silent?: boolean } = {}) => {
    if (!options.silent) {
      setBusy(true);
      setMessage(`loading versions ${workflow.id}`);
    }
    return apiFetch(`/workflows/v1/published/${encodeURIComponent(workflow.id)}/versions`)
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload) => {
        setWorkflowVersions(payload.data ?? []);
        setSelected(workflow);
        if (!options.silent) {
          setMessage(`loaded ${(payload.data ?? []).length} version records`);
        }
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => {
        if (!options.silent) {
          setBusy(false);
        }
      });
  };

  const restoreWorkflowVersion = (workflow: PublishedWorkflow) => {
    setBusy(true);
    setMessage(`restoring ${workflow.id}@${workflow.version}`);
    apiFetch(`/workflows/v1/published/${encodeURIComponent(workflow.id)}/versions/${encodeURIComponent(workflow.version)}/restore`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true, reason: "Control Center workflow rollback" })
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail ?? `${response.status}`))))
      .then((payload: PublishedWorkflow) => {
        setSelected(payload);
        setDraft(JSON.stringify(workflowDraftPayload(payload), null, 2));
        setTestParameters(defaultWorkflowTestParameters(payload));
        setMessage(`restored ${payload.id}@${payload.version}`);
        loadWorkflows();
        void loadWorkflowVersions(payload, { silent: true });
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
    setTestParameters(defaultWorkflowTestParameters(workflow));
    setWorkflowTest(null);
    setDraft(JSON.stringify(workflowDraftPayload(workflow), null, 2));
    setValidation(null);
  };

  const loadDraftFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    file.text()
      .then((text) => {
        setDraft(text);
        try {
          setTestParameters(defaultWorkflowTestParameters(JSON.parse(text)));
        } catch {
          setTestParameters("{}");
        }
        setValidation(null);
        setWorkflowTest(null);
        setMessage(`loaded ${file.name}`);
      })
      .catch((err: Error) => setMessage(err.message));
  };

  const editNodePin = (pin: ComfyUiNodePin) => {
    setSelectedNodePin(pin);
    setNodePinForm({
      id: pin.id,
      commit: pin.commit,
      repository_url: pin.repository_url,
      display_name: pin.display_name ?? "",
      status: pin.status,
      dependency_lock_sha256: pin.dependency_lock_sha256 ?? "",
      allowed_route_prefixes: (pin.allowed_route_prefixes ?? []).join(", "),
      notes: pin.notes ?? ""
    });
  };

  const resetNodePinForm = () => {
    setSelectedNodePin(null);
    setNodePinForm({
      id: "",
      commit: "",
      repository_url: "",
      display_name: "",
      status: "approved",
      dependency_lock_sha256: "",
      allowed_route_prefixes: "",
      notes: ""
    });
  };

  const nodePinPayload = () => ({
    id: nodePinForm.id.trim(),
    commit: nodePinForm.commit.trim(),
    repository_url: nodePinForm.repository_url.trim(),
    display_name: nodePinForm.display_name.trim() || null,
    status: nodePinForm.status,
    dependency_lock_sha256: nodePinForm.dependency_lock_sha256.trim() || null,
    allowed_route_prefixes: nodePinForm.allowed_route_prefixes
      .split(/[,\n]/)
      .map((item) => item.trim())
      .filter(Boolean),
    notes: nodePinForm.notes.trim() || null
  });

  const saveNodePin = () => {
    setBusy(true);
    const payload = nodePinPayload();
    const path = selectedNodePin
      ? `/admin/comfyui/node-pins/${encodeURIComponent(selectedNodePin.id)}/commits/${encodeURIComponent(selectedNodePin.commit)}`
      : `/admin/comfyui/node-pins`;
    const body = selectedNodePin
      ? {
          repository_url: payload.repository_url,
          display_name: payload.display_name,
          status: payload.status,
          dependency_lock_sha256: payload.dependency_lock_sha256,
          allowed_route_prefixes: payload.allowed_route_prefixes,
          notes: payload.notes
        }
      : payload;
    apiFetch(path, {
      method: selectedNodePin ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then((response) => response.ok ? response.json() : response.json().then((body) => Promise.reject(new Error(body.detail?.message ?? body.detail ?? `${response.status}`))))
      .then((payload) => {
        setMessage(`saved node pin ${payload.pin.id}@${payload.pin.commit.slice(0, 12)}`);
        loadNodePins();
        loadWorkflows();
      })
      .catch((err: Error) => setMessage(err.message))
      .finally(() => setBusy(false));
  };

  const dependencyRows = (validation?.dependency_status?.dependencies ?? selected?.dependency_status?.dependencies ?? []);

  return (
    <section className="panel wide">
      <SectionTitle icon={<Workflow size={18} />} title="Workflows" />
      <div className="toolbar">
        <button title="Refresh workflows and node pins" onClick={refreshWorkflowAdmin} disabled={busy}><RefreshCw size={16} />Refresh</button>
        <button title="Validate draft" onClick={validateDraft} disabled={busy}><ListChecks size={16} />Validate</button>
        <button title="Test draft parameters" onClick={() => testWorkflow()} disabled={busy}><PlayCircle size={16} />Test</button>
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
          <small>{workflowExecutionLine(validation)}</small>
          <small>{workflowExecutionStats(validation)}</small>
        </div>
      )}

      {workflowTest && (
        <div className="one-time-key">
          <strong>{workflowTest.workflow.id}@{workflowTest.workflow.version} test {workflowTest.status}</strong>
          <span>{workflowTest.can_submit ? "ready to submit" : "blocked by dependencies"} / {workflowTest.request.model} / {workflowTest.request.runtime_policy}</span>
          <small>{workflowTest.request.input.parameter_count} parameter{workflowTest.request.input.parameter_count === 1 ? "" : "s"} checked: {workflowTest.request.input.parameter_names.join(", ") || "none"}</small>
          <small>{workflowExecutionLine(workflowTest.workflow)}</small>
          <small>{workflowExecutionStats(workflowTest.workflow)}</small>
        </div>
      )}

      <div className="split workflow-admin">
        <div className="stack">
          <h3>Published Workflows</h3>
          <table>
            <thead><tr><th>Workflow</th><th>Status</th><th>Backend</th><th>Execution</th><th>Actions</th></tr></thead>
            <tbody>
              {workflows.map((workflow) => (
                <tr key={`${workflow.id}@${workflow.version}`}>
                  <td><code>{workflow.display_name}</code><small>{workflow.id}@{workflow.version}</small></td>
                  <td><span className={`status-pill ${workflow.status}`}>{workflow.status}</span><small>{workflow.publishable ? "ready" : "needs dependencies"}</small></td>
                  <td>{workflow.backend_policy}<small>{workflow.modality} / {workflow.model_alias}</small></td>
                  <td>{workflowExecutionLine(workflow)}<small>{workflowExecutionStats(workflow)}</small></td>
                  <td>
                    <div className="table-actions">
                      <button title={`Test ${workflow.id}`} onClick={() => testWorkflow(workflow)} disabled={busy}><PlayCircle size={16} /></button>
                      <button title={`Show versions for ${workflow.id}`} onClick={() => loadWorkflowVersions(workflow)} disabled={busy}><Eye size={16} /></button>
                      <button title={`Edit ${workflow.id}`} onClick={() => editWorkflow(workflow)} disabled={busy}><Workflow size={16} /></button>
                      <button title={`Unpublish ${workflow.id}`} onClick={() => unpublishWorkflow(workflow)} disabled={busy}><Trash2 size={16} /></button>
                    </div>
                  </td>
                </tr>
              ))}
              {!workflows.length && <tr><td colSpan={5}>No published workflows recorded</td></tr>}
            </tbody>
          </table>
          {Boolean(workflowVersions.length) && (
            <>
              <div className="subsection-title">
                <RotateCcw size={16} />
                <h3>Workflow Versions</h3>
              </div>
              <table>
                <thead><tr><th>Version</th><th>Status</th><th>Execution</th><th>Updated</th><th>Actions</th></tr></thead>
                <tbody>
                  {workflowVersions.map((workflow) => (
                    <tr key={`version-${workflow.id}@${workflow.version}`}>
                      <td><code>{workflow.id}@{workflow.version}</code><small>{workflow.display_name}</small></td>
                      <td><span className={`status-pill ${workflow.unpublished_at ? "warning" : "ok"}`}>{workflow.status}</span><small>{workflow.publishable ? "ready" : "needs dependencies"}</small></td>
                      <td>{workflowExecutionLine(workflow)}<small>{workflowExecutionStats(workflow)}</small></td>
                      <td>{workflow.updated_at ? formatDateTime(workflow.updated_at) : "unknown"}</td>
                      <td>
                        <div className="table-actions">
                          <button title={`Edit ${workflow.id}@${workflow.version}`} onClick={() => editWorkflow(workflow)} disabled={busy}><Workflow size={16} /></button>
                          <button title={`Restore ${workflow.id}@${workflow.version}`} onClick={() => restoreWorkflowVersion(workflow)} disabled={busy || !workflow.unpublished_at}><RotateCcw size={16} /></button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <div className="subsection-title">
            <ShieldCheck size={16} />
            <h3>ComfyUI Node Pins</h3>
          </div>
          <table>
            <thead><tr><th>Node</th><th>Status</th><th>Routes</th><th>Actions</th></tr></thead>
            <tbody>
              {nodePins.map((pin) => (
                <tr key={`${pin.id}@${pin.commit}`}>
                  <td><code>{pin.display_name || pin.id}</code><small>{pin.id}@{pin.commit.slice(0, 12)} / {pin.source}</small></td>
                  <td><span className={`status-pill ${pin.status === "approved" ? "ok" : "warning"}`}>{pin.status}</span><small>{pin.approved_by ?? ""}</small></td>
                  <td>{(pin.allowed_route_prefixes ?? []).join(", ") || "none"}</td>
                  <td>
                    <div className="table-actions">
                      <button title={`Edit node pin ${pin.id}`} onClick={() => editNodePin(pin)} disabled={busy}><Workflow size={16} /></button>
                    </div>
                  </td>
                </tr>
              ))}
              {!nodePins.length && <tr><td colSpan={4}>No ComfyUI node pins recorded</td></tr>}
            </tbody>
          </table>
          <div className="subsection-title">
            <ListChecks size={16} />
            <h3>Dependencies</h3>
          </div>
          <table>
            <thead><tr><th>Type</th><th>ID</th><th>Status</th><th>Detail</th><th>Reason</th></tr></thead>
            <tbody>
              {dependencyRows.map((dependency) => (
                <tr key={`${dependency.type}-${dependency.id}`}>
                  <td>{dependency.type}</td>
                  <td><code>{dependency.id}</code><small>{dependency.version ?? ""}</small></td>
                  <td><span className={`status-pill ${dependency.ready ? "ok" : "warning"}`}>{dependency.status ?? (dependency.ready ? "ready" : "blocked")}</span></td>
                  <td>{workflowDependencyDetail(dependency)}</td>
                  <td>{dependency.reason ?? ""}</td>
                </tr>
              ))}
              {!dependencyRows.length && <tr><td colSpan={5}>No dependency report selected</td></tr>}
            </tbody>
          </table>
        </div>

        <div className="stack">
          <h3>{selectedNodePin ? "Edit ComfyUI Node Pin" : "Approve ComfyUI Node Pin"}</h3>
          <div className="node-pin-form">
            <input placeholder="node id" value={nodePinForm.id} onChange={(event) => setNodePinForm({ ...nodePinForm, id: event.target.value })} disabled={Boolean(selectedNodePin) || busy} />
            <input placeholder="40-character commit" value={nodePinForm.commit} onChange={(event) => setNodePinForm({ ...nodePinForm, commit: event.target.value })} disabled={Boolean(selectedNodePin) || busy} />
            <input placeholder="https:// repository URL" value={nodePinForm.repository_url} onChange={(event) => setNodePinForm({ ...nodePinForm, repository_url: event.target.value })} disabled={busy} />
            <input placeholder="display name" value={nodePinForm.display_name} onChange={(event) => setNodePinForm({ ...nodePinForm, display_name: event.target.value })} disabled={busy} />
            <select value={nodePinForm.status} onChange={(event) => setNodePinForm({ ...nodePinForm, status: event.target.value })} disabled={busy}>
              <option value="approved">approved</option>
              <option value="disabled">disabled</option>
              <option value="superseded">superseded</option>
            </select>
            <input placeholder="dependency lock SHA-256" value={nodePinForm.dependency_lock_sha256} onChange={(event) => setNodePinForm({ ...nodePinForm, dependency_lock_sha256: event.target.value })} disabled={busy} />
            <input placeholder="allowed route prefixes" value={nodePinForm.allowed_route_prefixes} onChange={(event) => setNodePinForm({ ...nodePinForm, allowed_route_prefixes: event.target.value })} disabled={busy} />
            <textarea placeholder="notes" value={nodePinForm.notes} onChange={(event) => setNodePinForm({ ...nodePinForm, notes: event.target.value })} disabled={busy} />
            <div className="form-actions">
              <button title="Save ComfyUI node pin" onClick={saveNodePin} disabled={busy}><ShieldCheck size={16} />Save Pin</button>
              <button title="Reset ComfyUI node pin form" onClick={resetNodePinForm} disabled={busy}><RotateCcw size={16} />Reset</button>
            </div>
          </div>
          <h3>Workflow Draft JSON</h3>
          <textarea className="json-editor" value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} />
          <h3>Workflow Test Parameters</h3>
          <textarea className="json-editor compact" value={testParameters} onChange={(event) => setTestParameters(event.target.value)} spellCheck={false} />
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
