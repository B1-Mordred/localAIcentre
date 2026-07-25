import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import * as Tabs from "@radix-ui/react-tabs";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileAudio,
  FileImage,
  FileVideo,
  KeyRound,
  LogOut,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
  Upload,
  Wand2
} from "lucide-react";
import "./styles.css";

type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

type StagedUploadReference = {
  source: "staged_upload";
  id: string;
  field: string;
  kind: string;
  mime_type: string;
  filename: string;
  path: string;
  bytes: number;
  sha256: string;
};

type UploadPreview = {
  url: string;
  mime: string;
  name: string;
};

type JsonSchemaProperty = {
  type?: string | string[];
  enum?: Array<string | number | boolean>;
  default?: JsonValue;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  contentEncoding?: string;
  contentMediaType?: string;
  description?: string;
  title?: string;
};

type PublishedWorkflow = {
  id: string;
  version: string;
  display_name: string;
  description?: string;
  modality: "image" | "video" | "tts" | "stt";
  operation: string;
  model_alias: string;
  backend_policy: "comfyui-only" | "non-comfy-only" | "either";
  runtime_policy: "any" | "non_comfy_only";
  output_mime_types: string[];
  input_schema: {
    required?: string[];
    properties?: Record<string, JsonSchemaProperty>;
  };
  resource_class: string;
  limits: Record<string, number>;
  status: string;
  dependency_status?: {
    ready?: boolean;
    dependencies?: Array<{ type: string; id: string; ready?: boolean; status?: string; reason?: string }>;
  };
  publishable: boolean;
};

type MediaJob = {
  id: string;
  state: string;
  stage?: string;
  progress?: number;
  modality: string;
  operation: string;
  model_alias: string;
  runtime: string;
  resolved_model_version: string;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  completed_at?: string | null;
  load_time_ms?: number | null;
  run_time_ms?: number | null;
  peak_vram_mib?: number | null;
  peak_ram_mib?: number | null;
  artifacts?: Artifact[];
  links?: MediaJobLinks;
  failure_category?: string | null;
  failure_message?: string | null;
  redacted_request?: Record<string, JsonValue>;
};

type MediaJobLinks = {
  self?: string;
  events?: string;
  artifacts?: string;
  cancel?: string;
};

type Artifact = {
  id: string;
  kind: string;
  mime_type: string;
  url: string;
  bytes?: number;
};

type WorkflowResponse = {
  data?: PublishedWorkflow[];
};

type AuthStatus = {
  configured: boolean;
  setup_required: boolean;
  authenticated: boolean;
  subject_id?: string | null;
  role?: string | null;
  scopes: string[];
  csrf_token?: string | null;
};

const API_BASE = import.meta.env.VITE_B1_API_BASE ?? "https://api.ai.b1.germering";
const API_TOKEN = import.meta.env.VITE_B1_API_TOKEN ?? "";
const CSRF_STORAGE_KEY = "b1_ai_hub_csrf";
const TERMINAL_STATES = new Set(["completed", "cancelled", "failed", "expired", "recovery_required"]);

function apiUrl(path: string): string {
  return path.startsWith("http://") || path.startsWith("https://") ? path : `${API_BASE}${path}`;
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

function attachAuthHeaders(headers: Headers, method: string): void {
  if (API_TOKEN) {
    headers.set("Authorization", `Bearer ${API_TOKEN}`);
    return;
  }
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method) && !headers.has("X-B1-CSRF")) {
    const csrf = window.sessionStorage.getItem(CSRF_STORAGE_KEY);
    if (csrf) headers.set("X-B1-CSRF", csrf);
  }
}

async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  attachAuthHeaders(headers, (init.method ?? "GET").toUpperCase());
  const response = await fetch(apiUrl(path), {
    ...init,
    credentials: "include",
    headers
  });
  const body = await response.text();
  const parsed = body ? JSON.parse(body) : null;
  if (!response.ok) {
    throw new Error(errorMessageFromBody(parsed, response));
  }
  return parsed as T;
}

function safeHeaderValue(value: string): string {
  return value.replace(/[^\x20-\x7e]/g, "_").slice(0, 160);
}

function isStagedUploadReference(value: JsonValue | undefined): value is StagedUploadReference {
  return Boolean(value && typeof value === "object" && !Array.isArray(value) && value.source === "staged_upload");
}

async function uploadMediaInput(file: File, fieldName: string): Promise<StagedUploadReference> {
  const headers = new Headers();
  headers.set("Content-Type", file.type || "application/octet-stream");
  headers.set("X-B1-Field", fieldName);
  headers.set("X-B1-Filename", safeHeaderValue(file.name || fieldName));
  attachAuthHeaders(headers, "POST");
  const response = await fetch(apiUrl("/v1/media/uploads"), {
    method: "POST",
    credentials: "include",
    headers,
    body: file
  });
  const text = await response.text();
  const parsed = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error(errorMessageFromBody(parsed, response));
  const reference = parsed?.input ?? parsed?.reference;
  if (!reference || reference.source !== "staged_upload") throw new Error("upload response did not include a staged reference");
  return reference as StagedUploadReference;
}

function artifactFileName(artifact: Artifact): string {
  const fallback = `${artifact.kind || "artifact"}-${artifact.id || "download"}`;
  const path = artifact.url.split("?", 1)[0];
  const last = path.split("/").filter(Boolean).pop();
  try {
    return decodeURIComponent(last || fallback).replace(/[\\/:*?"<>|]/g, "_").slice(0, 160) || fallback;
  } catch {
    return fallback;
  }
}

async function downloadArtifact(artifact: Artifact): Promise<string> {
  const headers = new Headers();
  attachAuthHeaders(headers, "GET");
  const response = await fetch(apiUrl(artifact.url), {
    credentials: "include",
    headers
  });
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
  const filename = artifactFileName(artifact);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  return filename;
}

async function artifactPreviewSource(artifact: Artifact): Promise<{ src: string; mime: string }> {
  const headers = new Headers();
  attachAuthHeaders(headers, "GET");
  const response = await fetch(apiUrl(artifact.url), {
    credentials: "include",
    headers
  });
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
  return {
    src: URL.createObjectURL(blob),
    mime: blob.type || artifact.mime_type || "application/octet-stream"
  };
}

function trustedMediaJobLink(value: string | undefined): string | null {
  return value && value.startsWith("/v1/media/jobs/") ? value : null;
}

function jobRoute(job: MediaJob, linkName: keyof MediaJobLinks, fallbackSuffix = ""): string {
  const linked = trustedMediaJobLink(job.links?.[linkName]);
  if (linked) return linked;
  return `/v1/media/jobs/${encodeURIComponent(job.id)}${fallbackSuffix}`;
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

async function streamJobEvents(
  job: MediaJob,
  signal: AbortSignal,
  onJob: (job: MediaJob) => void
): Promise<void> {
  const headers = new Headers();
  headers.set("Accept", "text/event-stream");
  attachAuthHeaders(headers, "GET");
  const response = await fetch(apiUrl(jobRoute(job, "events", "/events")), {
    credentials: "include",
    headers,
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
    const normalized = buffer.replace(/\r\n/g, "\n");
    const chunks = normalized.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const parsed = parseSseEvent(chunk);
      if (!parsed) continue;
      if (parsed.event === "job") {
        onJob(JSON.parse(parsed.data) as MediaJob);
      } else if (parsed.event === "error" || parsed.event === "timeout") {
        const payload = JSON.parse(parsed.data);
        throw new Error(payload.error ?? parsed.event);
      }
    }
    if (done) break;
  }
}

function workflowIcon(workflow: PublishedWorkflow) {
  if (workflow.modality === "video") return <FileVideo size={17} />;
  if (workflow.modality === "tts" || workflow.modality === "stt") return <FileAudio size={17} />;
  return <FileImage size={17} />;
}

function backendLabel(workflow: PublishedWorkflow): string {
  if (workflow.backend_policy === "comfyui-only") return "local ComfyUI";
  if (workflow.backend_policy === "non-comfy-only") return "non-Comfy local";
  return "local selectable";
}

function jobBacking(job: MediaJob | null, workflow?: PublishedWorkflow | null): { locality: string; backend: string; label: string; className: string } {
  const runtime = (job?.runtime ?? "").toLowerCase();
  if (runtime === "comfyui") {
    return { locality: "local", backend: "ComfyUI-backed", label: "local / ComfyUI-backed", className: "local comfy" };
  }
  if (runtime === "openai-compatible" || runtime === "generic-http") {
    return { locality: "external", backend: "non-Comfy-backed", label: "external / non-Comfy-backed", className: "external non-comfy" };
  }
  if (runtime) {
    return { locality: "local", backend: "non-Comfy-backed", label: "local / non-Comfy-backed", className: "local non-comfy" };
  }
  if (workflow?.backend_policy === "comfyui-only") {
    return { locality: "local", backend: "ComfyUI-backed", label: "local / ComfyUI-backed", className: "local comfy" };
  }
  if (workflow?.backend_policy === "non-comfy-only") {
    return { locality: "local", backend: "non-Comfy-backed", label: "local / non-Comfy-backed", className: "local non-comfy" };
  }
  return { locality: "pending", backend: "runtime selected at execution", label: "pending / runtime selected at execution", className: "pending" };
}

function priorityFor(workflow: PublishedWorkflow): string {
  if (workflow.modality === "video") return "video";
  if (workflow.modality === "tts" || workflow.modality === "stt") return "interactive_audio";
  return "single_image";
}

function labelFor(name: string): string {
  return name.replaceAll("_", " ").replaceAll("-", " ");
}

function propertyType(schema: JsonSchemaProperty): string {
  return Array.isArray(schema.type) ? schema.type.find((item) => item !== "null") ?? "string" : schema.type ?? "string";
}

function defaultValue(schema: JsonSchemaProperty): JsonValue {
  if (schema.default !== undefined) return schema.default;
  const type = propertyType(schema);
  if (type === "boolean") return false;
  if (type === "integer" || type === "number") return schema.minimum ?? 0;
  return "";
}

function initialValues(workflow: PublishedWorkflow): Record<string, JsonValue> {
  const values: Record<string, JsonValue> = {};
  Object.entries(workflow.input_schema.properties ?? {}).forEach(([name, schema]) => {
    values[name] = defaultValue(schema);
  });
  return values;
}

function missingRequired(workflow: PublishedWorkflow, values: Record<string, JsonValue>): string[] {
  return (workflow.input_schema.required ?? []).filter((name) => values[name] === "" || values[name] === null || values[name] === undefined);
}

function formatBytes(bytes?: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function previewSource(
  workflow: PublishedWorkflow | null,
  values: Record<string, JsonValue>,
  previews: Record<string, UploadPreview>
): { src: string; mime: string } | null {
  if (!workflow) return null;
  for (const [name, schema] of Object.entries(workflow.input_schema.properties ?? {})) {
    if (schema.contentEncoding !== "base64") continue;
    if (typeof values[name] === "string" && values[name]) {
      return { src: `data:${schema.contentMediaType ?? "application/octet-stream"};base64,${values[name]}`, mime: schema.contentMediaType ?? "" };
    }
    if (isStagedUploadReference(values[name]) && previews[name]) {
      return { src: previews[name].url, mime: previews[name].mime };
    }
  }
  return null;
}

function firstPreviewArtifact(artifacts: Artifact[]): Artifact | null {
  return artifacts.find((artifact) => {
    const mime = artifact.mime_type || "";
    return mime.startsWith("image/") || mime.startsWith("audio/") || mime.startsWith("video/");
  }) ?? null;
}

type ReproducibilityEntry = {
  label: string;
  value: string;
};

function jsonObject(value: JsonValue | undefined): Record<string, JsonValue> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function shortText(value: string, limit = 96): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > limit ? `${normalized.slice(0, limit - 1)}...` : normalized;
}

function summarizeReproducibilityValue(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return "empty";
  if (typeof value === "string") return shortText(value);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `[array: ${value.length}]`;
  if (isStagedUploadReference(value)) return `${value.kind} upload / ${shortText(value.filename, 42)} / ${formatBytes(value.bytes)}`;
  const keys = Object.keys(value);
  return keys.length ? `[object: ${keys.slice(0, 3).join(", ")}${keys.length > 3 ? ", ..." : ""}]` : "[object]";
}

function addReproducibilityEntry(entries: ReproducibilityEntry[], label: string, value: JsonValue | undefined): void {
  if (value === undefined || value === null || value === "") return;
  entries.push({ label, value: summarizeReproducibilityValue(value) });
}

function reproducibilityEntries(job: MediaJob | null): ReproducibilityEntry[] {
  const request = job?.redacted_request;
  if (!request) return [];
  const input = jsonObject(request.input);
  const parameters = jsonObject(input?.parameters);
  const entries: ReproducibilityEntry[] = [];
  addReproducibilityEntry(entries, "model", request.model);
  addReproducibilityEntry(entries, "runtime policy", request.runtime_policy);
  addReproducibilityEntry(entries, "priority", request.priority);
  addReproducibilityEntry(entries, "workflow", input?.workflow_id);
  addReproducibilityEntry(entries, "workflow version", input?.workflow_version);
  Object.entries(parameters ?? {}).slice(0, 8).forEach(([name, value]) => {
    addReproducibilityEntry(entries, labelFor(name), value);
  });
  return entries;
}

function MediaPreview({ source, status }: { source: { src: string; mime: string } | null; status?: string }) {
  return (
    <div className={`preview ${source ? "" : "empty"}`}>
      {source?.mime.startsWith("image/") && <img src={source.src} alt="" />}
      {source?.mime.startsWith("audio/") && <audio src={source.src} controls />}
      {source?.mime.startsWith("video/") && <video src={source.src} controls />}
      {!source && (
        <>
          <Wand2 size={28} />
          {status && <small>{status}</small>}
        </>
      )}
    </div>
  );
}

function ArtifactPreview({
  artifact,
  fallbackSource
}: {
  artifact: Artifact | null;
  fallbackSource?: { src: string; mime: string } | null;
}) {
  const [loaded, setLoaded] = useState<{ src: string; mime: string } | null>(null);
  const [status, setStatus] = useState("");

  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    setLoaded(null);
    setStatus(artifact ? "loading output preview" : "");
    if (!artifact) return;
    artifactPreviewSource(artifact)
      .then((source) => {
        objectUrl = source.src;
        if (cancelled) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        setLoaded(source);
        setStatus("");
      })
      .catch((error: Error) => {
        if (!cancelled) setStatus(error.message);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [artifact?.id, artifact?.url]);

  return <MediaPreview source={loaded ?? fallbackSource ?? null} status={status || (artifact ? "preview unavailable" : "")} />;
}

function ReproducibilityMetadata({ job }: { job: MediaJob | null }) {
  const rows = reproducibilityEntries(job);
  return (
    <section className="reproducibility" aria-label="Redacted reproducibility metadata">
      <h3>Reproducibility</h3>
      {rows.length ? (
        <dl>
          {rows.map((row) => (
            <div key={row.label}>
              <dt>{row.label}</dt>
              <dd>{row.value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <span className="toolbar-status">No redacted request metadata recorded</span>
      )}
    </section>
  );
}

function DependencyPill({ workflow }: { workflow: PublishedWorkflow }) {
  const ready = Boolean(workflow.dependency_status?.ready);
  return (
    <span className={`status-pill ${ready ? "ready" : "blocked"}`}>
      {ready ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
      {workflow.status}
    </span>
  );
}

function SchemaField({
  name,
  schema,
  required,
  value,
  uploadStatus,
  onChange,
  onUploadFile
}: {
  name: string;
  schema: JsonSchemaProperty;
  required: boolean;
  value: JsonValue;
  uploadStatus?: string;
  onChange: (value: JsonValue) => void;
  onUploadFile: (file: File) => void;
}) {
  const type = propertyType(schema);
  const commonLabel = (
    <span>
      {labelFor(name)}
      {required && <strong>*</strong>}
    </span>
  );

  if (schema.contentEncoding === "base64") {
    const staged = isStagedUploadReference(value) ? value : null;
    return (
      <label className="field">
        {commonLabel}
        <div className="file-input">
          <Upload size={16} />
          <input
            type="file"
            accept={schema.contentMediaType}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (!file) return;
              onUploadFile(file);
            }}
          />
        </div>
        <small className="field-hint">{uploadStatus ?? (staged ? `${staged.filename} / ${formatBytes(staged.bytes)}` : "No file uploaded")}</small>
      </label>
    );
  }

  if (schema.enum) {
    return (
      <label className="field">
        {commonLabel}
        <select
          value={String(value)}
          onChange={(event) => {
            const selected = schema.enum?.find((item) => String(item) === event.target.value);
            onChange((selected ?? event.target.value) as JsonValue);
          }}
        >
          {schema.enum.map((item) => (
            <option key={String(item)} value={String(item)}>{String(item)}</option>
          ))}
        </select>
      </label>
    );
  }

  if (type === "boolean") {
    return (
      <label className="field checkbox-field">
        <input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} />
        {commonLabel}
      </label>
    );
  }

  if (type === "integer" || type === "number") {
    return (
      <label className="field">
        {commonLabel}
        <input
          type="number"
          value={String(value)}
          min={schema.minimum}
          max={schema.maximum}
          step={type === "integer" ? 1 : 0.05}
          onChange={(event) => onChange(event.target.value === "" ? "" : Number(event.target.value))}
        />
      </label>
    );
  }

  const longText = name.includes("prompt") || name === "text" || (schema.maxLength ?? 0) > 512;
  return (
    <label className="field">
      {commonLabel}
      {longText ? (
        <textarea
          rows={name === "text" ? 5 : 7}
          value={String(value)}
          minLength={schema.minLength}
          maxLength={schema.maxLength}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          value={String(value)}
          minLength={schema.minLength}
          maxLength={schema.maxLength}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </label>
  );
}

function WorkflowList({
  workflows,
  selectedId,
  onSelect
}: {
  workflows: PublishedWorkflow[];
  selectedId: string;
  onSelect: (workflow: PublishedWorkflow) => void;
}) {
  return (
    <div className="workflow-list">
      {workflows.map((workflow) => (
        <button key={`${workflow.id}@${workflow.version}`} className={selectedId === workflow.id ? "selected" : ""} onClick={() => onSelect(workflow)}>
          {workflowIcon(workflow)}
          <span>{workflow.display_name}</span>
          <small>{backendLabel(workflow)} / {workflow.model_alias}</small>
        </button>
      ))}
    </div>
  );
}

function JobSummary({
  workflow,
  job,
  artifacts,
  values,
  uploadPreviews,
  eventStatus,
  onCancel,
  onDownload
}: {
  workflow: PublishedWorkflow | null;
  job: MediaJob | null;
  artifacts: Artifact[];
  values: Record<string, JsonValue>;
  uploadPreviews: Record<string, UploadPreview>;
  eventStatus: string;
  onCancel: () => void;
  onDownload: (artifact: Artifact) => void;
}) {
  const preview = previewSource(workflow, values, uploadPreviews);
  const outputPreview = firstPreviewArtifact(artifacts);
  const backing = jobBacking(job, workflow);
  return (
    <aside className="resultpane">
      <div className="pane-title">
        <h2>Job</h2>
        <button title="Cancel job" type="button" onClick={onCancel} disabled={!job || TERMINAL_STATES.has(job.state)}>
          <Square size={16} />Cancel
        </button>
      </div>
      <div className={`backing-strip ${backing.className}`} aria-label="Job backing">
        <span>{backing.locality}</span>
        <span>{backing.backend}</span>
      </div>
      <ArtifactPreview artifact={outputPreview} fallbackSource={preview} />
      <dl>
        <div><dt>Status</dt><dd>{job?.state ?? "idle"}</dd></div>
        <div><dt>Progress</dt><dd>{job?.progress ?? 0}%</dd></div>
        <div><dt>Events</dt><dd>{eventStatus || "idle"}</dd></div>
        <div><dt>Backing</dt><dd>{backing.label}</dd></div>
        <div><dt>Runtime</dt><dd>{job?.runtime ?? workflow?.model_alias ?? "none"}</dd></div>
        <div><dt>Artifacts</dt><dd>{artifacts.length}</dd></div>
      </dl>
      <ReproducibilityMetadata job={job} />
      <div className="artifact-list">
        {artifacts.map((artifact) => (
          <button key={artifact.id} type="button" onClick={() => onDownload(artifact)}>
            <Download size={16} />{artifact.kind} / {formatBytes(artifact.bytes)}
          </button>
        ))}
      </div>
    </aside>
  );
}

function StudioForm({ onJobsLoaded }: { onJobsLoaded: (jobs: MediaJob[]) => void }) {
  const [workflows, setWorkflows] = useState<PublishedWorkflow[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [values, setValues] = useState<Record<string, JsonValue>>({});
  const [uploadStatus, setUploadStatus] = useState<Record<string, string>>({});
  const [uploadPreviews, setUploadPreviews] = useState<Record<string, UploadPreview>>({});
  const uploadPreviewsRef = useRef<Record<string, UploadPreview>>({});
  const [jobs, setJobs] = useState<MediaJob[]>([]);
  const [currentJob, setCurrentJob] = useState<MediaJob | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [message, setMessage] = useState("loading");
  const [eventStatus, setEventStatus] = useState("idle");
  const [busy, setBusy] = useState(false);

  const selectedWorkflow = useMemo(
    () => workflows.find((workflow) => workflow.id === selectedId) ?? workflows[0] ?? null,
    [selectedId, workflows]
  );

  const loadStudio = () => {
    setMessage("loading");
    Promise.all([
      apiJson<WorkflowResponse>("/workflows/v1/published"),
      apiJson<MediaJob[]>("/v1/media/jobs")
    ])
      .then(([workflowPayload, jobPayload]) => {
        const loaded = workflowPayload.data ?? [];
        setWorkflows(loaded);
        setJobs(jobPayload);
        onJobsLoaded(jobPayload);
        if (!selectedId && loaded[0]) {
          setSelectedId(loaded[0].id);
          setValues(initialValues(loaded[0]));
        }
        setMessage("ready");
      })
      .catch((error: Error) => setMessage(error.message));
  };

  useEffect(loadStudio, []);

  useEffect(() => {
    uploadPreviewsRef.current = uploadPreviews;
  }, [uploadPreviews]);

  useEffect(() => {
    return () => {
      Object.values(uploadPreviewsRef.current).forEach((preview) => URL.revokeObjectURL(preview.url));
    };
  }, []);

  useEffect(() => {
    if (!selectedWorkflow) return;
    setUploadPreviews((current) => {
      Object.values(current).forEach((preview) => URL.revokeObjectURL(preview.url));
      return {};
    });
    setValues(initialValues(selectedWorkflow));
    setUploadStatus({});
    setArtifacts([]);
    setEventStatus("idle");
  }, [selectedWorkflow?.id, selectedWorkflow?.version]);

  useEffect(() => {
    if (!currentJob || TERMINAL_STATES.has(currentJob.state)) return;
    const controller = new AbortController();
    setEventStatus("streaming");
    streamJobEvents(currentJob, controller.signal, (job) => {
      setCurrentJob(job);
      setArtifacts(job.artifacts ?? []);
      setMessage(`${job.state} ${job.id}`);
      if (TERMINAL_STATES.has(job.state)) {
        setEventStatus("complete");
        loadArtifacts(job);
        loadJobs();
      }
    }).catch((error: Error) => {
      if (controller.signal.aborted) return;
      setEventStatus("refreshing");
      apiJson<MediaJob>(jobRoute(currentJob, "self"))
        .then((job) => {
          setCurrentJob(job);
          setArtifacts(job.artifacts ?? []);
          if (TERMINAL_STATES.has(job.state)) {
            loadArtifacts(job);
            loadJobs();
          }
        })
        .catch((refreshError: Error) => setMessage(refreshError.message || error.message));
    });
    return () => controller.abort();
  }, [currentJob?.id]);

  const loadJobs = () => {
    apiJson<MediaJob[]>("/v1/media/jobs")
      .then((payload) => {
        setJobs(payload);
        onJobsLoaded(payload);
      })
      .catch((error: Error) => setMessage(error.message));
  };

  const loadArtifacts = (job: MediaJob) => {
    apiJson<{ artifacts: Artifact[] }>(jobRoute(job, "artifacts", "/artifacts"))
      .then((payload) => setArtifacts(payload.artifacts ?? []))
      .catch((error: Error) => setMessage(error.message));
  };

  const selectWorkflow = (workflow: PublishedWorkflow) => {
    setSelectedId(workflow.id);
    setCurrentJob(null);
    setArtifacts([]);
    setEventStatus("idle");
  };

  const uploadWorkflowFile = (name: string, schema: JsonSchemaProperty, file: File) => {
    const expected = schema.contentMediaType;
    if (expected && file.type && file.type !== expected) {
      setMessage(`${labelFor(name)} must be ${expected}`);
      return;
    }
    setBusy(true);
    setMessage(`uploading ${file.name}`);
    setUploadStatus((current) => ({ ...current, [name]: `uploading ${file.name}` }));
    const previewUrl = URL.createObjectURL(file);
    setUploadPreviews((current) => {
      if (current[name]) URL.revokeObjectURL(current[name].url);
      return { ...current, [name]: { url: previewUrl, mime: file.type || schema.contentMediaType || "", name: file.name } };
    });
    uploadMediaInput(file, name)
      .then((reference) => {
        setValues((current) => ({ ...current, [name]: reference as unknown as JsonValue }));
        setUploadStatus((current) => ({ ...current, [name]: `${reference.filename} / ${formatBytes(reference.bytes)}` }));
        setMessage(`uploaded ${reference.filename}`);
      })
      .catch((error: Error) => {
        setValues((current) => ({ ...current, [name]: "" }));
        setUploadPreviews((current) => {
          if (current[name]) URL.revokeObjectURL(current[name].url);
          const next = { ...current };
          delete next[name];
          return next;
        });
        setUploadStatus((current) => ({ ...current, [name]: error.message }));
        setMessage(error.message);
      })
      .finally(() => setBusy(false));
  };

  const submitJob = (event: React.FormEvent) => {
    event.preventDefault();
    if (!selectedWorkflow) return;
    const missing = missingRequired(selectedWorkflow, values);
    if (missing.length > 0) {
      setMessage(`missing required: ${missing.map(labelFor).join(", ")}`);
      return;
    }
    setBusy(true);
    setMessage("queueing job");
    apiJson<MediaJob>("/v1/media/jobs", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        modality: selectedWorkflow.modality,
        operation: selectedWorkflow.operation,
        model: selectedWorkflow.model_alias,
        runtime_policy: selectedWorkflow.runtime_policy,
        priority: priorityFor(selectedWorkflow),
        input: {
          workflow_id: selectedWorkflow.id,
          workflow_version: selectedWorkflow.version,
          parameters: values
        }
      })
    })
      .then((job) => {
        setCurrentJob(job);
        setArtifacts(job.artifacts ?? []);
        setEventStatus(TERMINAL_STATES.has(job.state) ? "complete" : "streaming");
        setMessage(`${job.state} ${job.id}`);
        loadJobs();
      })
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const cancelCurrentJob = () => {
    if (!currentJob) return;
    setBusy(true);
    apiJson<MediaJob>(jobRoute(currentJob, "cancel"), { method: "DELETE" })
      .then((job) => {
        setCurrentJob(job);
        setArtifacts(job.artifacts ?? []);
        setEventStatus(TERMINAL_STATES.has(job.state) ? "complete" : "streaming");
        setMessage(`${job.state} ${job.id}`);
        loadJobs();
      })
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const downloadCurrentArtifact = (artifact: Artifact) => {
    setBusy(true);
    setMessage(`downloading ${artifact.kind}`);
    downloadArtifact(artifact)
      .then((filename) => setMessage(`downloaded ${filename}`))
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setBusy(false));
  };

  const dependencies = selectedWorkflow?.dependency_status?.dependencies ?? [];

  return (
    <section className="workbench">
      <div className="toolpane">
        <div className="pane-title">
          <h2>Workflow</h2>
          <button title="Refresh" onClick={loadStudio} disabled={busy}><RefreshCw size={16} /></button>
        </div>
        <WorkflowList workflows={workflows} selectedId={selectedWorkflow?.id ?? ""} onSelect={selectWorkflow} />
      </div>

      <form className="formpane" onSubmit={submitJob}>
        <div className="form-header">
          <div>
            <h2>{selectedWorkflow?.display_name ?? "No workflows"}</h2>
            <span>{selectedWorkflow ? `${backendLabel(selectedWorkflow)} / ${selectedWorkflow.model_alias}` : "registry empty"}</span>
          </div>
          {selectedWorkflow && <DependencyPill workflow={selectedWorkflow} />}
          <div className="actions">
            <button type="submit" title="Run job" disabled={busy || !selectedWorkflow}><Play size={16} />Run</button>
          </div>
        </div>

        {selectedWorkflow && (
          <>
            <div className="metadata-strip">
              <span>{selectedWorkflow.modality}</span>
              <span>{selectedWorkflow.operation}</span>
              <span>{selectedWorkflow.output_mime_types.join(", ")}</span>
              <span>{selectedWorkflow.resource_class}</span>
            </div>
            <div className="schema-grid">
              {Object.entries(selectedWorkflow.input_schema.properties ?? {}).map(([name, schema]) => (
                <SchemaField
                  key={name}
                  name={name}
                  schema={schema}
                  required={(selectedWorkflow.input_schema.required ?? []).includes(name)}
                  value={values[name] ?? defaultValue(schema)}
                  uploadStatus={uploadStatus[name]}
                  onChange={(value) => setValues((current) => ({ ...current, [name]: value }))}
                  onUploadFile={(file) => uploadWorkflowFile(name, schema, file)}
                />
              ))}
            </div>
            <div className="dependency-list">
              {dependencies.map((dependency) => (
                <span key={`${dependency.type}-${dependency.id}`} className={dependency.ready ? "dependency ready" : "dependency blocked"}>
                  {dependency.type}:{dependency.id} {dependency.status ?? ""}
                </span>
              ))}
            </div>
          </>
        )}
        <span className="toolbar-status">{message}</span>
      </form>

      <JobSummary
        workflow={selectedWorkflow}
        job={currentJob}
        artifacts={artifacts}
        values={values}
        uploadPreviews={uploadPreviews}
        eventStatus={eventStatus}
        onCancel={cancelCurrentJob}
        onDownload={downloadCurrentArtifact}
      />
    </section>
  );
}

function History({ jobs, onRefresh, onSelect }: { jobs: MediaJob[]; onRefresh: () => void; onSelect: (job: MediaJob) => void }) {
  const formatMs = (value?: number | null) => {
    if (typeof value !== "number") return "pending";
    if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
    return `${value}ms`;
  };
  const formatPeak = (value?: number | null) => typeof value === "number" ? `${value} MiB` : "pending";
  return (
    <section className="history">
      <div className="pane-title">
        <h2>History</h2>
        <button title="Refresh history" onClick={onRefresh}><RefreshCw size={16} /></button>
      </div>
      <table>
        <thead><tr><th>Job</th><th>Model</th><th>Runtime</th><th>Status</th><th>Measured</th></tr></thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id} onClick={() => onSelect(job)}>
              <td><code>{job.id}</code><small>{job.modality} / {job.operation}</small></td>
              <td>{job.model_alias}<small>{job.resolved_model_version}</small></td>
              <td>{job.runtime}<small>{jobBacking(job).label}</small></td>
              <td>{job.state}<small>{job.stage ?? ""} / {job.progress ?? 0}%</small></td>
              <td>
                {formatMs(job.run_time_ms)}
                <small>load {formatMs(job.load_time_ms)} / VRAM {formatPeak(job.peak_vram_mib)}</small>
              </td>
            </tr>
          ))}
          {!jobs.length && <tr><td colSpan={5}>No media jobs yet</td></tr>}
        </tbody>
      </table>
    </section>
  );
}

function HistoryDetail({
  job,
  artifacts,
  status,
  onRefreshArtifacts,
  onDownload
}: {
  job: MediaJob | null;
  artifacts: Artifact[];
  status: string;
  onRefreshArtifacts: () => void;
  onDownload: (artifact: Artifact) => void;
}) {
  const formatMs = (value?: number | null) => {
    if (typeof value !== "number") return "pending";
    if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
    return `${value}ms`;
  };
  const formatPeak = (value?: number | null) => typeof value === "number" ? `${value} MiB` : "pending";
  const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString() : "pending";
  const outputPreview = firstPreviewArtifact(artifacts);
  const backing = jobBacking(job);

  if (!job) {
    return (
      <aside className="history-detail">
        <div className="pane-title">
          <h2>Job Details</h2>
        </div>
        <p className="empty-state">Select a history row to inspect artifacts and reproducibility metadata.</p>
      </aside>
    );
  }

  return (
    <aside className="history-detail">
      <div className="pane-title">
        <h2>Job Details</h2>
        <button title="Refresh artifacts" onClick={onRefreshArtifacts}><RefreshCw size={16} /></button>
      </div>
      <ArtifactPreview artifact={outputPreview} />
      <dl>
        <div><dt>Job</dt><dd><code>{job.id}</code></dd></div>
        <div><dt>Status</dt><dd>{job.state} / {job.stage ?? "unknown"} / {job.progress ?? 0}%</dd></div>
        <div><dt>Workflow</dt><dd>{job.modality} / {job.operation}</dd></div>
        <div><dt>Runtime</dt><dd>{job.runtime}</dd></div>
        <div><dt>Backing</dt><dd>{backing.label}</dd></div>
        <div><dt>Model</dt><dd>{job.model_alias}<small>{job.resolved_model_version}</small></dd></div>
        <div><dt>Created</dt><dd>{formatTime(job.created_at)}</dd></div>
        <div><dt>Completed</dt><dd>{formatTime(job.completed_at)}</dd></div>
        <div><dt>Measured</dt><dd>load {formatMs(job.load_time_ms)} / run {formatMs(job.run_time_ms)}</dd></div>
        <div><dt>Peak</dt><dd>VRAM {formatPeak(job.peak_vram_mib)} / RAM {formatPeak(job.peak_ram_mib)}</dd></div>
        {job.failure_category && <div><dt>Failure</dt><dd>{job.failure_category}<small>{job.failure_message ?? ""}</small></dd></div>}
      </dl>
      <ReproducibilityMetadata job={job} />
      <div className="artifact-list">
        {artifacts.map((artifact) => (
          <button key={artifact.id} type="button" onClick={() => onDownload(artifact)}>
            <Download size={16} />{artifact.kind} / {artifact.mime_type} / {formatBytes(artifact.bytes)}
          </button>
        ))}
        {!artifacts.length && <span className="toolbar-status">No artifacts recorded</span>}
      </div>
      <span className="toolbar-status">{status}</span>
    </aside>
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
      <header>
        <div>
          <h1>B1 AI Media Studio</h1>
          <span>{message}</span>
        </div>
        <div className="session-strip"><ShieldCheck size={16} />LAN internal</div>
      </header>
      <section className="auth-shell">
        <form className="auth-panel" onSubmit={submit}>
          <div className="pane-title">
            <h2>{auth?.setup_required ? "Initial Admin" : "Sign In"}</h2>
          </div>
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

function StudioApp({ auth, onLogout }: { auth: AuthStatus; onLogout: () => void }) {
  const [jobs, setJobs] = useState<MediaJob[]>([]);
  const [selectedHistoryJob, setSelectedHistoryJob] = useState<MediaJob | null>(null);
  const [historyArtifacts, setHistoryArtifacts] = useState<Artifact[]>([]);
  const [historyStatus, setHistoryStatus] = useState("select a job");

  const loadJobs = () => {
    apiJson<MediaJob[]>("/v1/media/jobs")
      .then((rows) => {
        setJobs(rows);
        setSelectedHistoryJob((current) => current ? rows.find((job) => job.id === current.id) ?? current : null);
      })
      .catch(() => setJobs([]));
  };

  useEffect(loadJobs, []);

  const loadHistoryArtifacts = (job: MediaJob) => {
    setSelectedHistoryJob(job);
    setHistoryArtifacts(job.artifacts ?? []);
    setHistoryStatus("loading artifacts");
    apiJson<{ artifacts: Artifact[] }>(jobRoute(job, "artifacts", "/artifacts"))
      .then((payload) => {
        setHistoryArtifacts(payload.artifacts ?? []);
        setHistoryStatus(`loaded ${(payload.artifacts ?? []).length} artifact(s)`);
      })
      .catch((error: Error) => setHistoryStatus(error.message));
  };

  const refreshSelectedHistoryArtifacts = () => {
    if (selectedHistoryJob) loadHistoryArtifacts(selectedHistoryJob);
  };

  const downloadHistoryArtifact = (artifact: Artifact) => {
    setHistoryStatus(`downloading ${artifact.kind}`);
    downloadArtifact(artifact)
      .then((filename) => setHistoryStatus(`downloaded ${filename}`))
      .catch((error: Error) => setHistoryStatus(error.message));
  };

  return (
    <main>
      <header>
        <div>
          <h1>B1 AI Media Studio</h1>
          <span>Media production</span>
        </div>
        <div className="session-strip">
          <ShieldCheck size={16} />{auth.role ?? "session"}
          <button title="Sign out" onClick={onLogout}><LogOut size={16} /></button>
        </div>
      </header>
      <Tabs.Root defaultValue="create">
        <Tabs.List className="tabs" aria-label="Media Studio views">
          <Tabs.Trigger value="create">create</Tabs.Trigger>
          <Tabs.Trigger value="history">history</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="create"><StudioForm onJobsLoaded={setJobs} /></Tabs.Content>
        <Tabs.Content value="history">
          <section className="history-shell">
            <History jobs={jobs} onRefresh={loadJobs} onSelect={loadHistoryArtifacts} />
            <HistoryDetail
              job={selectedHistoryJob}
              artifacts={historyArtifacts}
              status={historyStatus}
              onRefreshArtifacts={refreshSelectedHistoryArtifacts}
              onDownload={downloadHistoryArtifact}
            />
          </section>
        </Tabs.Content>
      </Tabs.Root>
    </main>
  );
}

function App() {
  return (
    <AuthGate>
      {(auth, logout) => <StudioApp auth={auth} onLogout={logout} />}
    </AuthGate>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
