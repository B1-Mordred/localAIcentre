## Mission

Replace the existing local AI application stack on `ai.b1.germering` with a reproducible, LAN-internal, Docker Compose–deployed AI appliance named **B1 AI Hub**. It must provide one coherent web-managed platform for:

- LLM/chat and vision-language inference
- embeddings and RAG support
- speech-to-text
- fast and high-quality text-to-speech, including voice cloning where supported
- text-to-image and image-to-image
- text-to-video and image-to-video
- native ComfyUI workflows and API compatibility
- centrally managed models that can also be consumed by external ComfyUI, Voicebox, or other AI services
- OpenAI-compatible and asynchronous media APIs
- automatic loading/unloading with at most one GPU-resident model or pipeline at a time

The target machine is the existing LAN server `ai.b1.germering`, currently an RTX 3060 12 GB / 32 GB RAM–class system. Treat 32 GB RAM and 12 GB VRAM as hard initial constraints. The implementation must work now and make a later RAM/GPU upgrade easy.

## Execution contract for Codex

Work autonomously through discovery, implementation, tests, documentation, and a safe deployment/migration plan. Do not stop after scaffolding or a design document. Deliver a runnable repository.

Before changing the target host or existing deployment:

1. Inspect the repository and obey all `AGENTS.md` files.
2. Inventory the current Docker containers, Compose projects, volumes, networks, model directories, Open WebUI database, exposed ports, GPU driver, NVIDIA Container Toolkit, free RAM, disks, mount points, and DNS assumptions.
3. Identify which existing services belong to the old AI stack. Do not assume Hermes, Yggdrasil, Discord integrations, unrelated containers, or other user data are in scope.
4. Never delete or overwrite old volumes, model files, databases, or configuration during development or cutover.
5. Produce backups and a tested rollback path before replacing the old stack.
6. Use pinned image versions or immutable digests. Do not use floating `latest` tags in the production Compose file.
7. Prefer official upstream images. If an official image is unavailable or unsuitable, build a minimal reproducible image from a pinned release/commit and document why.
8. Make only required host changes. The application stack belongs in Docker; the NVIDIA driver, Docker Engine, Compose v2, DNS, and NVIDIA Container Toolkit remain host prerequisites.
9. Treat the appliance hostname/FQDN as target-system identity. The host OS may set or validate the hostname, but IP address, gateway, routes, resolver settings, and other network properties must be acquired by the host DHCP client or a DHCP reservation. The repository, bootstrap, Compose files, and cutover tooling must not configure a static host IP.

Ask the user only when an action is destructive, a required secret/domain choice is unavailable, or two materially different behaviours cannot safely be inferred.

## Non-negotiable requirements

1. A fresh installation is started with one documented command:

   ```bash
   docker compose up -d
   ```

   An optional bootstrap helper may generate secrets and validate prerequisites, but it must ultimately invoke the same Compose project.

2. After bootstrap, all normal operation is manageable from web UIs. Shell editing must not be required for model installation/removal, model aliases, runtime selection, idle timeouts, API keys, queues, workflow publication, storage cleanup, service health, logs, or application updates.

3. Only the gateway is normally exposed to the LAN. Backend ports for LocalAI, ComfyUI, Voicebox, PostgreSQL, and Redis must not be published.

4. The GPU scheduler must enforce one GPU-resident model/pipeline at a time across every managed GPU runtime. An image/video workflow counts as one pipeline even if it temporarily uses a denoiser/transformer, text encoder, VAE, vision encoder, LoRA, or ControlNet components.

5. Lightweight CPU-only models may remain resident only when enabled by policy and when the reserved host RAM remains available. Default candidates are a small embedding model and fast CPU TTS/STT. The UI must make this distinction visible.

6. External clients must be able to use the full native ComfyUI REST/WebSocket API through a transparent scheduler-aware compatibility endpoint.

7. External services must also be able to use models without invoking the server-side ComfyUI runtime, either by calling another hosted inference backend or by downloading/synchronising centrally managed weights for local execution.

8. Every long-running media operation must be asynchronous, cancellable, observable, and recoverable after a control-plane restart.

9. The production system must be LAN-internal by default and must not silently use cloud models or send prompts, media, voices, telemetry, or metadata outside the LAN.

10. Preserve current Open WebUI accounts/chats/settings where safely possible. If a direct database migration is unsafe, implement export/import guidance and keep the old data recoverable.

## Intended URLs

Support these virtual hosts; make them configurable through bootstrap values:

| URL | Purpose |
|---|---|
| `https://ai.b1.germering/` | Open WebUI for chat, RAG, voice, and ordinary users |
| `https://control.ai.b1.germering/` | B1 AI Control Center |
| `https://media.ai.b1.germering/` | Simplified image/audio/video Media Studio |
| `https://comfy.ai.b1.germering/` | Native ComfyUI editor and complete native API |
| `https://voice.ai.b1.germering/` | Managed Voicebox server/web mode, if enabled |
| `https://models.ai.b1.germering/` | Model Hub catalog and authenticated blob distribution |
| `https://api.ai.b1.germering/` | Unified inference and job API |

Use Caddy as the sole TLS/reverse-proxy entry point. Support a Caddy internal CA for LAN DNS. Document how to distribute/trust its root certificate. Make externally supplied certificates possible without source changes.

The LAN virtual hosts must resolve to the DHCP-assigned address for the target system hostname. `B1_EXPECTED_TARGET_HOST` validates the observed system hostname/FQDN during inventory, cutover, and acceptance; it must not be used to configure static host networking.

Some legacy ComfyUI clients require `http://host:8188` and cannot set an API prefix or authentication header. Provide an optional, disabled-by-default legacy listener that terminates at the scheduler-aware compatibility proxy, never directly at ComfyUI. It must be controlled by an explicit Compose override/profile and restricted by IP/CIDR allowlists.

## Repository deliverables

Create a maintainable monorepo resembling:

```text
b1-ai-hub/
├── README.md
├── LICENSES.md
├── SECURITY.md
├── CHANGELOG.md
├── compose.yaml
├── compose.legacy-comfy.yaml
├── .env.example
├── Makefile
├── docs/
│   ├── architecture.md
│   ├── installation.md
│   ├── administration.md
│   ├── api.md
│   ├── model-management.md
│   ├── comfyui-compatibility.md
│   ├── voicebox.md
│   ├── external-consumers.md
│   ├── security.md
│   ├── backup-restore.md
│   ├── migration.md
│   ├── troubleshooting.md
│   └── resource-profile-rtx3060-32gb.md
├── deploy/
│   ├── caddy/
│   ├── postgres/
│   ├── localai/
│   ├── comfyui/
│   ├── voicebox/
│   └── scripts/
├── services/
│   ├── control-plane/
│   ├── runtime-agent/
│   ├── artifact-server/
│   └── audio-cpu/
├── web/
│   ├── control-center/
│   └── media-studio/
├── integrations/
│   ├── comfyui-b1-remote-nodes/
│   ├── b1-model-client/
│   └── examples/
├── model-catalog/
│   ├── schemas/
│   └── seed/
├── workflows/
│   ├── schemas/
│   └── approved/
└── tests/
    ├── unit/
    ├── integration/
    ├── compatibility/
    ├── security/
    └── smoke/
```

Use generated directories outside the Git repository, under a configurable root such as `/srv/b1-ai-hub`:

```text
/srv/b1-ai-hub/
├── data/
│   ├── postgres/
│   ├── redis/
│   ├── open-webui/
│   ├── control-plane/
│   └── voicebox/
├── models/
│   ├── llm/
│   ├── vision/
│   ├── embeddings/
│   ├── diffusion/
│   │   ├── checkpoints/
│   │   ├── diffusion_models/
│   │   ├── text_encoders/
│   │   ├── vae/
│   │   ├── loras/
│   │   ├── controlnet/
│   │   └── upscale_models/
│   ├── video/
│   ├── tts/
│   ├── stt/
│   └── blobs/
├── workflows/
├── artifacts/
│   ├── images/
│   ├── audio/
│   ├── video/
│   └── temporary/
├── cache/
├── secrets/
├── logs/
└── backups/
```

Models must be mounted read-only into inference containers wherever practical. Only the Model Hub/download worker may mutate the authoritative model library.

## Docker Compose services

Implement and health-check at least these services:

| Service | Responsibility |
|---|---|
| `gateway` | Caddy TLS, routing, WebSockets, request limits, security headers |
| `open-webui` | Daily chat/RAG/voice user experience; calls only the unified API |
| `control-plane` | Admin API, model registry, scheduler, job service, compatibility proxies |
| `control-center` | React/TypeScript administration UI |
| `media-studio` | React/TypeScript simplified media-generation UI |
| `runtime-agent` | Narrow, allowlisted Docker/runtime operations and NVML metrics |
| `localai` | Primary modular inference backend with one active LocalAI backend |
| `comfyui` | Native ComfyUI engine/editor; lazy model loading |
| `voicebox` | Optional managed Voicebox server/runtime |
| `audio-cpu` | Lightweight CPU TTS/STT endpoint where selected |
| `postgres` | Durable system state; enable pgvector if used for shared embeddings/RAG |
| `redis` | Durable-enough queue coordination, leases, pub/sub/SSE state |
| `artifact-server` | Authenticated download/stream/range access to generated results |
| `bootstrap` | Idempotent directories, permissions, secrets, DB creation/migrations |

Use separate internal Docker networks for edge, application, data, and runtime traffic where useful. Apply `read_only`, dropped capabilities, non-root users, `no-new-privileges`, tmpfs, health checks, restart policies, resource limits, and explicit mounts per service. Do not use privileged containers unless an unavoidable, documented host integration requires it.

The GPU runtimes may see the same physical GPU, but only the control plane may submit inference work. Bind their APIs only to internal networks. Configure LocalAI with a maximum of one active backend and an idle watchdog as a second line of defence.

## Technology choices

Unless the existing repository establishes compatible alternatives, use:

- Python 3.12+ FastAPI, Pydantic, SQLAlchemy, Alembic, async PostgreSQL, and redis-py for `control-plane`.
- React, TypeScript, Vite, a well-maintained accessible component library, and generated OpenAPI clients for the web UIs.
- Go for `runtime-agent` if it materially reduces attack surface; otherwise a small separately packaged Python service is acceptable. The API surface must remain strictly allowlisted.
- PostgreSQL as the source of truth. Redis must never be the only store of job/model state.
- Server-Sent Events for ordinary job/admin progress and a WebSocket bridge for native ComfyUI events.
- OpenAPI 3.1 generated from code and committed or reproducibly generated in CI.
- Structured JSON logs with request/job correlation IDs.

## Control-plane responsibilities

The control plane is the authoritative coordinator and must implement:

1. Authentication for Control Center and Media Studio.
2. Roles: `admin`, `operator`, `creator`, `user`, and service/API clients.
3. Hashed API keys with prefixes and one-time display of the full key.
4. Model catalog, installation, validation, activation, aliasing, and removal.
5. Runtime adapters and health/status discovery.
6. Exclusive GPU lease and scheduling.
7. Durable job creation, state transitions, cancellation, progress, artifacts, retries, and recovery.
8. Transparent native ComfyUI REST/WebSocket proxying.
9. Unified OpenAI-compatible endpoints and asynchronous media endpoints.
10. Model Hub catalog/blob/synchronisation endpoints.
11. Workflow registry/publication and dependency checks.
12. Configuration persistence and encrypted secret values.
13. Update staging, maintenance mode, health validation, and rollback hooks.
14. Audit logs for administrative changes and model/workflow operations.
15. Resource admission control for the RTX 3060/32 GB profile.

Generate a master encryption key during bootstrap and store it as a file/Docker secret outside Git. Encrypt remote-provider credentials and other sensitive configuration at rest with an authenticated encryption scheme. Never log secrets, bearer tokens, voice samples, prompts, uploaded documents, or model-download credentials.

## Job model and state machine

Persist jobs in PostgreSQL with at least:

```text
created -> validated -> queued -> waiting_for_gpu
        -> unloading -> verifying_vram -> loading -> warming
        -> running -> saving -> completed
```

Also support `cancelling`, `cancelled`, `failed`, `expired`, and `recovery_required`.

Every job records:

- job ID and correlation ID
- owner/service client
- modality and operation
- requested public model alias
- resolved immutable model version and runtime
- priority and queue timestamps
- request parameters and a redacted audit representation
- progress and current stage
- ComfyUI native prompt ID when applicable
- retry count and failure category
- output artifact references
- measured load time, run time, peak VRAM/RAM, and final status

Requests must be idempotent when an `Idempotency-Key` is supplied.

## GPU scheduler

Implement a scheduler designed for one RTX 3060:

- One cross-runtime GPU lease.
- Never preempt an active LLM stream or a non-checkpointable generation step.
- A different-model request triggers a switch as soon as the current safe unit finishes.
- Same-model requests may be grouped; allow parallel LLM requests only after measured VRAM proves it safe. Default concurrency is one.
- ComfyUI image/video execution is sequential initially.
- Interactive priority order: chat, interactive TTS/STT, single image/edit, image batch, video, batch automation.
- Apply priority aging so background jobs cannot starve.
- Video workflows should expose safe yield points between short clips/scenes.
- Support an optional time-bounded runtime reservation for external batch clients.
- On switch: drain current work, request graceful unload, verify actual VRAM with NVML, restart the runtime if memory remains above threshold, load/warm the target, then run.
- Reconcile active runtimes, GPU memory, queued jobs, and leases after any control-plane restart.
- Prevent split-brain scheduling through an expiring Redis lease plus a PostgreSQL scheduler epoch/ownership record.

Initial configurable RTX 3060 resource policy:

```yaml
gpu:
  total_vram_gib: 12
  usable_vram_gib: 10.5
  reserve_vram_gib: 1.5
  maximum_active_pipelines: 1
host:
  total_ram_gib: 32
  reserve_ram_gib: 6
llm:
  default_context: 8192
  maximum_context: 16384
  default_parallel_requests: 1
comfyui:
  maximum_parallel_jobs: 1
  maximum_batch_size: 1
```

Make these UI-editable within hard safety bounds. The admission controller must reject or require explicit admin override for profiles whose estimated RAM/VRAM/disk needs exceed policy. Label models `recommended`, `expected`, `offload-required`, `experimental`, or `incompatible`. Replace estimates with measured values after successful runs.

## Runtime adapter contract

Define a versioned adapter interface with:

- capability discovery
- model listing and health
- validate request/profile
- load/warm model
- submit/stream inference
- progress/events
- cancel/interrupt
- unload/free memory
- active/queued work discovery
- metrics and failure classification
- graceful and forced recovery

Implement these adapters:

1. `LocalAIAdapter` for LLM, VLM, embeddings, supported TTS/STT, image, and video backends.
2. `ComfyUIAdapter` for native workflows and complete API compatibility.
3. `VoiceboxAdapter` for Voicebox-native TTS/voice functionality and remote clients.
4. `CpuAudioAdapter` for fast CPU-only TTS/STT.
5. `OpenAICompatibleAdapter` for optional remote providers. Remote execution is disabled by default and visibly labelled `external`; enabling it must warn that data leaves the LAN.
6. `GenericHttpAdapter` only if needed for future runtimes; restrict destinations against SSRF and require admin-approved base URLs.

A model can list multiple supported runtimes and one preferred runtime. A request may explicitly forbid ComfyUI, for example `runtime_policy: non_comfy_only`. If no compatible non-Comfy backend exists, return a clear capability error rather than silently using ComfyUI.

## Model registry and Model Hub

Use content-addressed immutable blobs plus human-readable model manifests. A model version must include:

- ID, display name, modality, operations, description
- source URL/repository/revision and SHA-256
- file format and quantization
- size and required companion files
- compatible runtime adapters and versions
- preferred runtime
- expected VRAM/RAM/context/resolution/frame limits
- licence, redistribution policy, attribution, and acceptance requirements
- execution modes: `hosted-inference`, `downloadable`, `network-share`
- installation/validation status
- aliases and visibility/role permissions
- measured performance and resource data
- deprecation/replacement information

Model installation must:

1. Resolve a manifest from an approved catalog, direct URL, or upload.
2. Validate URL scheme/host policy and prevent SSRF.
3. Display licence/source/size/resource impact before confirmation.
4. Download to a `.partial` staging area with progress, cancellation, and resume.
5. Verify size and SHA-256 before atomic publication.
6. Refuse archive path traversal, symlink escape, device nodes, and unsafe file types.
7. Validate runtime compatibility without loading incompatible weights.
8. Register dependencies and create runtime-specific read-only views/symlinks.
9. Run an optional smoke inference and store measurements.

Removal must refuse active/in-use models, calculate dependent workflows/profiles, require confirmation, and move blobs to a recoverable quarantine before permanent cleanup.

Implement Model Hub APIs:

```text
GET    /modelhub/v1/catalog
GET    /modelhub/v1/models/{id}
GET    /modelhub/v1/models/{id}/versions
GET    /modelhub/v1/blobs/{sha256}
HEAD   /modelhub/v1/blobs/{sha256}
POST   /modelhub/v1/sync/plan
POST   /modelhub/v1/clients
DELETE /modelhub/v1/clients/{id}
```

Blob downloads must support Range, ETag, `If-None-Match`, `Content-Length`, resume, per-client authorization, rate limiting, and checksum verification. Enforce licence policy: some models may be inference-only and must never be downloadable.

Create `b1-model-client`, usable as a CLI and small container/daemon on external machines. It must list/plan/sync/pin/prune models into a local cache, verify hashes, never remove unmanaged files, and support dry runs. Provide Linux and Windows-oriented documentation.

Optionally provide a disabled-by-default read-only SMB model-share profile for trusted LAN clients. Do not make live network-mounted loading the recommended path; recommend local synchronised caches for performance and resilience.

## Native ComfyUI compatibility

Keep ComfyUI running without loaded weights so metadata and uploads remain responsive. Expose it only through a transparent compatibility proxy at `comfy.ai.b1.germering`.

The proxy must preserve native behaviour for at least:

- `POST /prompt`
- `/ws` including client IDs, progress events, binary previews, execution errors, and completion messages
- `/history` and `/history/{prompt_id}`
- `/queue`, queue deletion, and interrupt
- `/upload/image`, `/upload/mask`, `/view`, and metadata
- `/object_info` and node-specific metadata
- `/system_stats`
- `/models` and model-folder discovery
- installed trusted custom-node routes
- future/unknown safe routes through a deliberate passthrough policy

Execution handling:

1. Non-inference metadata/upload/view requests pass immediately.
2. `POST /prompt` requests acquire or wait for a ComfyUI GPU lease before forwarding.
3. Once forwarded, use the real native ComfyUI validation, queue number, and prompt ID; do not replace them with proprietary IDs.
4. Bridge native WebSocket traffic unchanged and map it to the durable job/audit record.
5. Hold the ComfyUI lease while its native queue is running, plus a configurable short idle grace period.
6. Forward interrupt/cancel immediately.
7. Free models with the native memory-release API, verify NVML, and restart only when graceful cleanup fails.

If a prompt submission has to wait for another active runtime, keep the HTTP request pending up to a configurable compatibility timeout and send appropriate proxy timeouts/keepalives. Also support an optional reservation API for clients that can cooperate. Document that no system can provide simultaneous execution on the single GPU.

Do not expose unrestricted custom-node installation to ordinary users. Pin approved node repositories/commits, scan dependency changes, show them in the UI, and require administrator approval. Provide a safe maintenance workflow for editing/testing workflows.

## Published workflows and Media Studio

Administrators/creators may build workflows in native ComfyUI and publish versioned API-format workflows to the Media Studio. A published workflow defines:

- immutable workflow JSON and version
- input/output JSON Schema
- required node versions and model dependencies
- allowed values/ranges and server-enforced limits
- supported RTX 3060 resource class
- output MIME types
- role visibility
- maximum resolution, frames, steps, batch size, and duration
- whether it may run through ComfyUI, a non-Comfy backend, or either

Ship safe starter workflow definitions/placeholders for:

- text-to-image
- image-to-image
- inpainting/outpainting
- background removal
- upscaling
- text-to-video
- image-to-video
- frame interpolation
- TTS
- transcription

Do not bundle model weights. Seed only manifests and workflows that are legally redistributable, and make uninstalled dependencies visible rather than failing mysteriously.

## External ComfyUI integration without server-side ComfyUI

Create a separately installable custom-node package `comfyui-b1-remote-nodes`. It must call the unified B1 API, not the native server-side ComfyUI endpoint, and provide nodes for:

- list/select B1 models and aliases
- chat/text generation
- vision analysis
- embeddings
- text-to-image
- image-to-image
- text-to-video
- image-to-video
- TTS and STT
- submit job, wait/poll job, cancel job
- download image/audio/video artifact

Support API base URL and credential configuration without storing secrets inside workflow JSON. Use environment/config files or ComfyUI server settings. Validate uploads/downloads, provide useful node errors, and include example workflows.

When these nodes call a model whose manifest selects LocalAI, Voicebox, CPU audio, or another non-Comfy adapter, the server-side ComfyUI container must remain unused. Add an integration test proving this by stopping the server-side ComfyUI container and successfully completing a supported remote-node operation.

Also document stock external ComfyUI local execution using a synchronized model cache and `extra_model_paths.yaml`.

## Voicebox integration

Support the current Jamie Pine Voicebox project as an optional managed runtime/server, pinned to a tested release. External Voicebox UIs must be able to connect to `voice.ai.b1.germering` through the gateway while the raw backend remains internal.

Requirements:

- preserve native Voicebox API/WebSocket behaviour required by its remote/external-server mode
- proxy through HTTPS and access policy
- register Voicebox models/engines and voice profiles in the Control Center
- route GPU Voicebox inference through the global lease
- allow explicitly CPU-capable profiles to run under CPU residency policy
- expose an OpenAI-compatible `/v1/audio/speech` route through the unified API where semantics permit
- protect voice reference samples and cloned profiles as sensitive user data
- allow backup/export/delete with an audit record
- do not claim model compatibility between LocalAI and Voicebox unless validated

## Unified API

Implement, document, and test:

```text
GET  /v1/models
POST /v1/chat/completions
POST /v1/responses
POST /v1/embeddings
POST /v1/audio/speech
POST /v1/audio/transcriptions
POST /v1/images/generations
POST /v1/images/edits
```

Implement asynchronous media endpoints:

```text
POST   /v1/media/jobs
GET    /v1/media/jobs/{job_id}
DELETE /v1/media/jobs/{job_id}
GET    /v1/media/jobs/{job_id}/events
GET    /v1/media/jobs/{job_id}/artifacts
```

Implement optional runtime reservations:

```text
POST   /v1/runtime-reservations
GET    /v1/runtime-reservations/{id}
DELETE /v1/runtime-reservations/{id}
```

Use stable public aliases such as:

```text
chat-default
chat-fast
chat-quality
chat-coding
vision-default
embedding-default
image-default
image-edit
image-upscale
video-text
video-image
tts-fast
tts-quality
stt-default
```

Return capability errors when an alias cannot satisfy input modality, runtime policy, resource policy, or installed dependencies. Never silently fall back to an external/cloud provider.

## Control Center UI

Build a responsive web UI with these sections:

### Dashboard

- active runtime/model/pipeline and lease owner
- GPU utilization, temperature, power, VRAM used/free
- host RAM, swap, CPU, disk, model/artifact storage
- current job, queue, recent jobs, estimated wait/load times
- service health and version
- warnings, OOMs, failed unloads, update availability

### Models

- catalog search/filter by modality/runtime/status/size/licence
- install/upload/import with progress, pause/resume/cancel
- compatibility and resource assessment before installation
- enable/disable, test, alias, permissions, idle timeout, context/resolution limits
- dependencies, installed files, hashes, source, licence, measured performance
- safe removal/quarantine and storage reclamation

### Runtimes

- health, supported capabilities, versions, active model, unload/restart/test
- LocalAI backend settings
- ComfyUI node/workflow state
- Voicebox engine/profile state
- CPU residency policy
- remote provider configuration with explicit external-data warning

### Queue and jobs

- filter, inspect, prioritize within role policy, cancel, retry, view logs/artifacts
- reservations and current GPU lease
- live progress through SSE

### Workflows

- import native API workflow JSON
- validate nodes/models/schema/resources
- version, test, publish/unpublish, roll back
- configure Media Studio form and permissions

### External access

- create/revoke API clients and keys
- CIDR/CORS allowlists
- ComfyUI compatibility status and legacy listener instructions
- Model Hub clients and download permissions
- snippets for curl, Open WebUI, external ComfyUI, Voicebox, Python, and JavaScript

### Storage

- models/blobs/cache/artifacts/backups usage
- retention policies and dry-run cleanup
- duplicate/orphan detection
- recoverable quarantine

### System

- hostname/TLS status, time zone, resource policy, backups, maintenance mode
- application update check/stage/apply/health-check/rollback
- audit log and redacted structured logs

The web UI must never offer arbitrary shell commands, arbitrary Docker operations, arbitrary host paths, or unvalidated download destinations.

## Media Studio UI

Provide a clean non-node interface that renders forms from published workflow schemas. Include uploads, previews, progress, cancellation, job history, artifact download, reproducibility metadata, and safe parameter presets. Clearly mark whether a job is local, external, ComfyUI-backed, or non-Comfy-backed.

## Runtime-agent security

The web-facing control plane must not mount `/var/run/docker.sock`.

Only `runtime-agent` may access the container runtime, and it must expose a narrow mutually authenticated internal API. Allow only:

- status and approved metrics
- start/stop/restart for an immutable allowlist of B1 AI Hub services
- inspect/pull an approved pinned image reference
- fetch bounded/redacted logs
- run predefined health/smoke actions
- apply a predefined rollback

Forbid arbitrary images, commands, environment variables, host paths, mounts, container creation, exec, file writes, or Docker API passthrough. Validate all service names as enums. Rate-limit mutations and audit them.

## Security requirements

- LAN-only firewall guidance; no router port forwarding.
- HTTPS on normal endpoints.
- Strong password hashing and secure session cookies.
- CSRF protection for browser mutations.
- Role checks and per-key scopes.
- Rate, queue, upload-size, pixel/frame, context, token, output-size, and storage quotas.
- MIME sniffing and safe image/audio/video parsing.
- Filename/path canonicalization and traversal protection.
- SSRF prevention for model imports and generic runtime URLs.
- Archive extraction limits against zip bombs.
- SQL injection, command injection, and unsafe deserialization tests.
- CORS allowlist; never wildcard credentials.
- No sensitive payloads in logs or metrics.
- Dependency/SBOM generation and vulnerability scanning in CI.
- Pin and review ComfyUI custom nodes; ordinary users cannot install code.
- Disable optional cloud/partner nodes and telemetry by default.
- Backups encrypted or protected by documented filesystem controls.

## Observability

Expose a web dashboard and machine-readable metrics for:

- queue depth/wait time by class
- cold/warm model-load duration
- model switches per hour
- tokens/sec, time-to-first-token
- seconds/image and seconds/frame/clip
- TTS real-time factor
- peak VRAM/RAM
- OOM/unload/restart/error counts
- artifact/model storage
- GPU temperature/power/utilization

Do not require a heavy monitoring stack for the default 32 GB deployment. A lightweight in-app metrics store is acceptable. Provide an optional Compose profile for Prometheus/Grafana if useful.

## Updates and rollback

Do not deploy unattended Watchtower-style updates. Implement a controlled process:

1. Check signed/official sources for available pinned releases.
2. Display release/version information.
3. Enter maintenance mode.
4. Back up databases/configuration/workflows/voice profiles.
5. Pull/build staged images.
6. Run migrations and health checks.
7. Execute smoke inference without destroying the previous deployment.
8. Promote on success.
9. Roll back images and DB/config version when promotion fails.

Keep model updates separate from application updates.

## Backup and restore

Back up:

- PostgreSQL data/dumps
- Open WebUI data
- Control Center settings and encrypted-secret metadata
- model manifests and custom/non-redownloadable weights
- workflows and approved node pins
- Voicebox voice profiles/reference samples
- selected artifacts
- Caddy PKI and generated secrets where necessary for continuity

Do not waste backup capacity on reproducibly downloadable weights unless configured. Provide scheduled and manual backup operations, retention, verification, restore-to-alternate-directory, and a documented disaster-recovery test.

## Migration and cutover

Implement scripts and documentation that:

1. Produce a read-only inventory report of the old stack.
2. Back up old Compose files, environment, volumes, Open WebUI DB, models, and relevant configuration.
3. Start B1 AI Hub on temporary ports/hostnames without running two GPU inference stacks concurrently.
4. Import or safely reuse Open WebUI data using supported migrations.
5. Import model metadata or redownload through Model Hub; never assume Ollama blob layout is directly reusable by LocalAI.
6. Validate accounts/chats, chat inference, API, TTS, image, video, native ComfyUI API, Model Hub, and external integrations.
7. Stop only the identified old-stack services during the cutover window.
8. Activate production LAN DNS/routes to the DHCP-assigned appliance address without configuring a static host IP from the B1 repository.
9. Retain the old stack and volumes stopped/read-only for rollback.
10. Provide a tested command/procedure to revert DNS/routes and restart the old stack.

Do not remove the old stack automatically after successful migration.

## Testing requirements

### Unit tests

- model manifest/schema and licence/distribution validation
- alias resolution and runtime policy
- resource admission and safety bounds
- scheduler ordering, aging, grouping, cancellation, reservations
- job state transitions and restart reconciliation
- API-key scopes and role checks
- path, archive, URL, CIDR, and SSRF validation
- encrypted secret handling

### Integration tests

- Compose services reach healthy state on a GPU-capable host
- LocalAI chat streaming and immediate/graceful unloading
- one active LocalAI backend
- ComfyUI prompt, history, queue, interrupt, upload, view, and WebSocket events through proxy
- Voicebox external-server health and a supported speech request
- CPU audio path does not take the GPU lease
- Model Hub range/resume/ETag/hash behaviour
- b1-model-client sync and dry-run pruning
- artifact range/download/authorization
- application restart with queued/running job reconciliation

### Cross-runtime GPU tests

1. Run an LLM request and verify only its model is GPU-resident.
2. Submit a ComfyUI request; verify the LLM drains/unloads, VRAM falls, ComfyUI loads, and the job completes.
3. Submit a Voicebox GPU request; verify ComfyUI unloads first.
4. Force an unload failure and verify bounded recovery/restart without two active pipelines.
5. Verify actual GPU memory never exceeds configured reserve during supported workflows.

### External-consumer tests

- Unmodified native ComfyUI client using `comfy.ai.b1.germering` with REST and `/ws`.
- Optional legacy client via the scheduler-aware `:8188` listener.
- External ComfyUI using `comfyui-b1-remote-nodes` for an operation while the server-side ComfyUI container is stopped; the operation must succeed through a non-Comfy backend.
- External model client downloads, resumes, verifies, and installs a permitted model into a local cache.
- Inference-only model cannot be downloaded.
- External Voicebox UI/backend connection through `voice.ai.b1.germering` where upstream supports it.

### Security tests

- unauthenticated/under-scoped requests rejected
- arbitrary Docker/runtime-agent operations impossible
- path traversal, symlink escape, archive bomb, malicious filename blocked
- import URL cannot reach loopback, metadata services, private Docker services, or unapproved hosts
- CORS/CSRF/session behaviours correct
- custom node install unavailable to non-admins
- secrets/redacted payloads absent from logs

### RTX 3060/32 GB acceptance test

Run the default lightweight profile under representative chat, image, TTS, and short-video loads. The host must remain responsive, retain the configured RAM/VRAM reserves, avoid unbounded swap thrashing, and recover cleanly from OOM or cancelled jobs.

## Initial model/profile policy

Do not hard-code rapidly changing model names as architectural dependencies. Seed configurable role profiles and show compatible catalog choices. Initial target classes:

- everyday LLM: 7–9B Q4
- optional quality LLM: 12–14B Q4 with conservative context/offload
- VLM: roughly 4-8B quantized
- small CPU embedding model
- fast CPU TTS such as a Piper/Kokoro-class engine
- scheduled quality/voice-cloning TTS
- an SDXL-class or similarly capable low-VRAM image workflow
- small/quantized 12 GB–validated video workflows with batch 1 and short clips

Any seeded recommendation must show its exact tested version, licence, download source, measured hardware result, and date. The web UI must allow replacement without client reconfiguration because clients use aliases.

## Documentation and operator experience

Write complete, command-tested documentation for:

- host prerequisites and NVIDIA validation
- DNS and internal CA trust
- first boot/admin setup
- web administration
- APIs and authentication
- model lifecycle and licences
- native ComfyUI API and legacy compatibility
- external ComfyUI remote nodes and local model cache
- Voicebox
- backups/restores
- update/rollback
- migration/cutover
- troubleshooting OOM, failed unload, broken custom node, failed download, WebSocket, TLS, and permissions

Include diagrams, example curl requests, Python/JavaScript clients, and a machine-readable OpenAPI specification. Provide an admin-visible system self-test that checks storage permissions, DB, Redis, GPU/NVML, runtimes, TLS routing, a tiny inference, unload, and artifact delivery.

## CI and quality gates

Add CI for formatting, linting, type checking, unit tests, API-schema drift, frontend tests/build, container builds, SBOMs, vulnerability scanning, secret scanning, and Compose validation. GPU tests may run in a separately labelled environment but must have a documented local command.

Use migrations; never rely on recreating production databases. Make bootstrap and migration scripts idempotent. Ensure a failed partial deployment does not corrupt the existing environment.

## Definition of done

The goal is complete only when:

1. The repository builds and starts as one Docker Compose project.
2. All required UIs and APIs are reachable through the configured gateway and pass health checks.
3. Normal operations, including model management, are web-manageable.
4. The GPU scheduler demonstrably maintains one GPU pipeline across LocalAI, ComfyUI, and Voicebox.
5. Chat, TTS/STT, image generation/editing, and at least one hardware-suitable short video workflow work.
6. Full native ComfyUI REST/WebSocket access works for external services through the compatibility endpoint.
7. External ComfyUI can use a supported hosted model through B1 remote nodes while server-side ComfyUI is stopped.
8. External machines can securely synchronize permitted model weights from Model Hub.
9. Voicebox remote/server integration is implemented to the extent supported by the pinned upstream version and any limitation is explicitly documented and tested.
10. Security, resource, backup, recovery, and migration acceptance tests pass.
11. The old stack has not been deleted and a documented rollback has been tested.
12. Documentation is sufficient for another administrator to install, operate, update, restore, and troubleshoot the appliance without reading source code.

At handoff, provide:

- exact pinned versions/digests
- installation and migration commands
- default URLs
- generated administrator onboarding procedure
- test results, measured RTX 3060 performance, and known limitations
- a list of old resources deliberately preserved for rollback
- recommended next hardware upgrade, expected to be system RAM from 32 GB to at least 64 GB
