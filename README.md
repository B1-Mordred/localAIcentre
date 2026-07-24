# B1 AI Hub

B1 AI Hub is the replacement local AI appliance for `ai.b1.germering`. The target is a reproducible, LAN-internal Docker Compose project that provides one web-managed control plane for chat, RAG, speech, image, video, native ComfyUI compatibility, Voicebox integration, and centrally managed model distribution.

The implementation plan is tracked in [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md). This repository is being built to satisfy that plan without deleting or overwriting the existing AI stack during development or cutover.

## Current State

This repository currently contains the first runnable project slice:

- pinned, non-`latest` Compose topology
- Caddy gateway as the only published normal entry point
- Open WebUI wired to the unified API through a generated internal scoped service key
- FastAPI control plane scaffold with catalog-backed model aliases, job, Model Hub, and ComfyUI compatibility endpoints
- LocalAI/OpenAI-compatible forwarding for installed chat, vision-language responses, and embedding aliases, gated by the global GPU scheduler lease when the selected runtime requires GPU
- versioned runtime-adapter contract metadata in `/admin/runtimes`, covering capability discovery, scheduler surface, submission/event surface, lifecycle hooks, unload/recovery control, metrics, and external-runtime limits
- native ComfyUI `POST /prompt` admission through the global GPU lease with durable `native_prompt_id` recording, background history tracking, bidirectional `/ws` text/binary bridging, progress parsing, and ComfyUI output ingestion into authenticated artifact storage
- ComfyUI-backed media jobs that submit native prompt payloads or mapped published workflow JSON through the same GPU runner, record `native_prompt_id`, poll native history, ingest `/view` outputs, and release the scheduler lease after completion
- LocalAI-backed image-generation media jobs that call OpenAI-compatible `/v1/images/generations`, persist returned `b64_json`, `data:` URL, or same-origin runtime URL media, and fail to `recovery_required` when no media is returned
- audio-cpu-backed async TTS/STT jobs and model smoke hooks that exercise CPU speech/transcription/embedding paths without taking the GPU lease
- database-backed model install planning from catalog IDs, uploaded manifests, or bounded HTTPS manifest URLs; licence-gated resumable direct-url and Hugging Face repository blob downloads with Control Center pause/resume and retry/requeue; verified staged-blob publication; hardlinked or safely extracted per-runtime read-only model views; recoverable record/blob quarantine with confirmed quarantine retention cleanup; and catalog overlay refresh
- runtime-agent with default-on internal mTLS, fail-closed token-protected allowlisted Docker status, bounded redacted logs surfaced through Control Center, disabled-by-default service mutations, and predefined runtime recover/unload actions
- runtime-agent CPU/memory/disk/GPU metrics and Control Center system self-test with TLS route, tiny inference, dry-run unload, and artifact delivery probes
- durable Control Center acceptance reports under `$B1_BACKUP_ROOT/acceptance/`, capturing self-test, metrics, resource policy, scheduler state, runtime reservations, runtime-agent service/image inventory, recent update image refs, source commit metadata, structured operator evidence for live tests/backups/migration/restart reconciliation/rollback/security, machine-readable RTX GPU, LocalAI streaming/unload, installed workflow, native ComfyUI REST/WebSocket compatibility, remote-node compatibility, Model Hub client sync, Voicebox remote/server, deployed security including artifact authorization, restart reconciliation, and backup/migration/rollback evidence, preserved old resources from the reviewed cutover plan, Markdown handoff output, and checksums for cutover review
- lightweight Control Center observability backed by `GET /admin/metrics`, showing queue waits, recent job timing/resource summaries, model switches, runtime-agent availability, GPU telemetry, and host memory/storage without requiring Prometheus or Grafana for the base appliance
- PostgreSQL-backed audit log for administrative changes with recursive metadata redaction and Control Center visibility
- AES-GCM encrypted configuration-secret storage for provider credentials, download tokens, runtime credentials, and integrations, backed by the generated master key outside Git and exposed through redacted admin UI/API controls
- persisted Control Center configuration for optional external runtimes, with remote-provider secret references, explicit external-data acknowledgement, SSRF-resistant URL validation, and fail-closed alias resolution
- persisted, administrator-editable RTX 3060/32 GB resource policy with hard bounds, dry-run validation, catalog admission refresh, and live GPU-runner reserve updates
- persisted administrator-controlled maintenance mode that blocks new inference/media/reservation work, admin job retries, and queued runner claims while leaving cancellation, backups, audit, and inspection paths available
- persisted controlled-update planning with pinned image digest validation, maintenance-gated staging backups, runtime-agent pinned-image staging, generated Compose image override artifacts, self-test recording, promotion handoff validation, and predefined runtime-agent rollback dry-run/execute metadata
- browser first-admin setup, scrypt password hashes, HttpOnly session cookies, CSRF-protected UI mutations, and web-managed CORS/trusted-proxy allowlists for LAN UI origins
- CPU and GPU job runners that persist durable job state, recover interrupted work, try graceful runtime unload hooks before restart fallback, verify runtime-agent VRAM metrics before GPU execution when available, cancel long LocalAI/Voicebox runtime calls cooperatively, and fail unsupported runtime/job shapes without writing placeholder success artifacts
- Alembic-backed control-plane database migration runner that executes before Uvicorn and startup schema verification that fails closed on missing tables/columns
- durable Voicebox profile registry with Control Center create/export/delete flows, audit events, backup coverage, TTS alias/runtime validation, and artifact-only voice sample references
- external `comfyui-b1-remote-nodes` package with unified-API nodes for model selection, chat, vision request shaping, embeddings, media jobs, uploads, artifacts, TTS, and STT without using the server-side ComfyUI endpoint
- external `b1-model-client` CLI/container daemon with Model Hub sync-plan support, pin/unpin, resumable verified blob sync, and managed-only safe prune for workstation model caches
- Model Hub blob delivery through the authenticated control-plane proxy with downloadable-policy checks, per-client allowlists, CIDR enforcement, a generated internal artifact-server bearer token, Range/ETag/checksum support, and per-subject rate-limit headers
- committed OpenAPI 3.1 schema generated from the FastAPI app with a CI drift check
- GitHub Actions quality gates for backend tests, Compose validation, Alembic packaging, frontend builds, NPM audit, source secret scanning, CycloneDX SBOM generation/validation, Python dependency audit, service/frontend/integration container builds, strict Trivy scans for the smaller B1-owned images, and uploaded Trivy inventories for upstream-heavy Open WebUI, LocalAI, ComfyUI, and Voicebox images
- artifact-server and CPU audio service scaffolds
- placeholder internal `localai`, `comfyui`, and `voicebox` runtimes for scheduler/adapter development
- production LocalAI Compose override that replaces the placeholder `localai` service with a B1 wrapper image built from the official pinned CUDA 12 LocalAI image, mounts only the read-only LocalAI model view plus LocalAI writable state, exposes LocalAI's native API through the internal `:8080` proxy, and adds scheduler lifecycle hooks for load/warm/smoke/unload probes
- production ComfyUI Compose override and reproducible B1 image build from upstream ComfyUI `v0.3.77` commit `59afc3984868289f808d02fa5cd180edfb2de240`, with native `:8188` API behind the scheduler-aware proxy, read-only runtime model views, disabled API/cloud nodes, explicit input/output/temp/user dirs, RTX 3060 VRAM reserve defaults, and B1 lifecycle hooks installed as a custom-node route package
- production Voicebox Compose override and reproducible B1 image build from Jamie Pine Voicebox `v0.5.0` commit `2bcb98d1a8b6fe05e15fbc1559e3085669e4035d`, with a B1 proxy on native `:17493` that forwards REST/web/WebSocket traffic to loopback upstream, mounts read-only runtime model views, stores B1-managed profile/generation/cache data, and exposes conservative scheduler lifecycle hooks for load/warm/smoke/unload probes
- React/TypeScript Control Center with model lifecycle, workflow validation/publication, backup, self-test, audit, encrypted-secret, and external-client management surfaces plus schema-rendered Media Studio job submission/history with server-side workflow validation, SSE progress, and authenticated artifact downloads
- bootstrap, structured read-only migration inventory, operator-reviewed old-stack backup scope/verification tooling, non-destructive cutover/rollback plan generation, checksumed B1 backup/alternate-restore scripts, Control Center manual/scheduled backup and retention operations, chunked AES-GCM archive encryption for off-host copies, and control-plane PostgreSQL logical plus native custom-format dumps
- Control Center generated-artifact retention planning and confirmed cleanup for old terminal job artifacts, with Voicebox sample protection, symlink/path-scope refusal, job metadata marking, and HTTP 410 for reclaimed artifact downloads
- configurable media-job queue/rate admission and artifact-storage headroom checks exposed through `GET /admin/admission`, persisted admin policy APIs, Dashboard, Storage, and System tab editing, returning HTTP 429 or 507 before work is accepted when limits would be exceeded
- runtime reservation fleet visibility for administrators/operators with Jobs tab creation/cancellation controls and current GPU scheduler lease inspection
- opt-in live smoke tests for deployed health, authenticated model listing, async TTS media jobs, SSE events, artifact download, optional admin self-test, LocalAI streaming/unload acceptance, an RTX 3060 cross-runtime GPU acceptance sequence for LocalAI -> ComfyUI -> Voicebox switching, and a restart reconciliation drill that proves waiting jobs are requeued while interrupted active jobs become `recovery_required`

The placeholder runtime containers are intentional at this stage. They keep `docker compose up -d` runnable on a small host without downloading model weights while the scheduler, registry, adapters, and UI flows are implemented. They are service-compatible placeholders to be replaced by pinned upstream runtime images/builds as each adapter reaches production readiness. The control plane now reports `runtimes:production-readiness` in `/admin/self-test` and `/admin/runtimes`: default `B1_RUNTIME_DEPLOYMENT_MODE=development` degrades when required runtimes are placeholders, while `B1_RUNTIME_DEPLOYMENT_MODE=production` fails self-test until `B1_RUNTIME_PRODUCTION_REQUIRED` runtimes are real, healthy, and non-placeholder.

LocalAI, ComfyUI, and Voicebox now have production runtime overrides:

```bash
cp .env.production.example .env
make bootstrap
docker compose up -d
```

The base Compose file remains the lightweight development topology, but the production env template sets `COMPOSE_FILE` and `COMPOSE_PROFILES` so the normal Compose command selects the real LocalAI, ComfyUI, and Voicebox overlays. The explicit `-f` commands remain useful for partial runtime validation. See [deploy/localai/README.md](./deploy/localai/README.md), [deploy/comfyui/README.md](./deploy/comfyui/README.md), [deploy/voicebox/README.md](./deploy/voicebox/README.md), and [docs/installation.md](./docs/installation.md).

Seeded aliases are visible on first boot, but direct inference only runs for aliases backed by an installed manifest. The initial installed manifests are CPU-only development placeholders for embeddings, fast TTS, and STT; the scaffold CPU embedding endpoint returns deterministic local hash vectors for smoke testing, not semantic RAG quality. The catalog also includes `b1-piper-en-us-amy-low` as an available, checksum-pinned Piper voice recommendation for `tts-fast`, `b1-minilm-l6-v2-onnx-q4` as an available, checksum-pinned ONNX embedding recommendation for `embedding-default`, and `b1-vosk-small-en-us-0.15` as an available, checksum-pinned Vosk recommendation for `stt-default`; installing any of them creates a read-only `audio-cpu` runtime view and requests carry the immutable model ref to the runtime. The bundled CPU runtime marks scaffold responses with `b1_placeholder=true` or `X-B1-Placeholder: true` and can be disabled with `B1_CPU_AUDIO_ENABLE_PLACEHOLDER=false`, in which case scaffold endpoints return HTTP 503 until a real engine is configured. The default audio-cpu image installs checksum-pinned rhasspy/piper `2023.11.14-2` for real CPU TTS when `B1_CPU_AUDIO_ENGINE=piper`, includes pinned ONNX Runtime/tokenizer dependencies for real local embeddings when `B1_CPU_EMBEDDING_ENGINE=onnx`, and includes pinned Vosk bindings for real local PCM WAV transcription when `B1_CPU_STT_ENGINE=vosk`. GPU aliases remain visible as uninstalled until real validated model manifests are added.

With `B1_DEV_AUTH_BYPASS=false`, Control Center and Media Studio use browser sessions immediately after first-admin setup. Open WebUI reads a generated internal service key from `/run/secrets/open_webui_api_key`, and the control plane registers the matching hashed scoped API client on startup.

## Quick Start

Review and copy the example environment:

```bash
cp .env.example .env
```

For the real-runtime appliance path, copy the production template instead:

```bash
cp .env.production.example .env
```

Optional preflight for host permissions, generated secrets, and the external data tree:

```bash
make bootstrap
```

Start the stack. The Compose `bootstrap` service runs the same idempotent setup first, so this is the required fresh-install start command:

```bash
docker compose up -d
```

Validate the repository and Compose configuration:

```bash
make validate
```

Regenerate the committed API schema after endpoint changes:

```bash
make openapi
make openapi-check
```

Run source hygiene gates locally when touching dependencies, images, or security-sensitive code:

```bash
make secret-scan
make sbom
```

Run opt-in live smoke checks against a deployed stack:

```bash
B1_SMOKE_LIVE_TEST=1 \
B1_AI_HUB_API_KEY=... \
B1_SMOKE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/live-smoke.json \
make smoke
```

See [tests/smoke/README.md](./tests/smoke/README.md) for LAN TLS and temporary-host options.

Run LocalAI runtime acceptance after a real chat alias is installed, then run target-host cross-runtime GPU acceptance after real GPU models and a ComfyUI API prompt are installed:

```bash
B1_LOCALAI_ACCEPTANCE_API_KEY=... \
B1_LOCALAI_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/localai-runtime.json \
make localai-acceptance

B1_GPU_ACCEPTANCE_API_KEY=... \
B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json \
B1_GPU_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/cross-runtime-gpu.json \
make gpu-acceptance
```

Run installed workflow acceptance after chat, CPU TTS/STT, image, edit, and short-video aliases are backed by real installed models:

```bash
B1_WORKFLOWS_LIVE_TEST=1 \
B1_WORKFLOWS_API_KEY=... \
B1_WORKFLOWS_IMAGE_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/image-generation-job.json \
B1_WORKFLOWS_IMAGE_EDIT_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/image-edit-job.json \
B1_WORKFLOWS_VIDEO_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/short-video-job.json \
B1_WORKFLOWS_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/installed-workflows.json \
python3 -m unittest tests.integration.test_live_installed_workflows
```

Run native ComfyUI compatibility from an external client after a real API-format workflow is installed:

```bash
B1_NATIVE_COMFYUI_LIVE_TEST=1 \
B1_NATIVE_COMFYUI_BASE=https://comfy.ai.b1.germering \
B1_NATIVE_COMFYUI_API_KEY=... \
B1_NATIVE_COMFYUI_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json \
B1_NATIVE_COMFYUI_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/native-comfyui.json \
python3 -m unittest tests.compatibility.test_native_comfyui_compatibility
```

Run deployed security acceptance after production authentication and runtime-agent log access are configured:

```bash
B1_SECURITY_API_KEY=... \
B1_SECURITY_CREATE_TEMP_UNDERSCOPED_CLIENT=1 \
B1_SECURITY_BROWSER_USERNAME=admin \
B1_SECURITY_BROWSER_PASSWORD=... \
B1_SECURITY_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/security-acceptance.json \
make security-acceptance
```

Generate backup, migration, and rollback handoff evidence after B1 backup verification, alternate-directory restore rehearsal, old-stack migration review, and rollback rehearsal:

```bash
make backup-migration-rollback-evidence \
  B1_BACKUP_DIR=/srv/b1-ai-hub/backups/20260722-130000 \
  RESTORE_REPORT=/srv/b1-ai-hub/restore-tests/20260722-130000/restore-report.json \
  INVENTORY=/srv/b1-ai-hub/backups/inventory-20260722-120000.json \
  OLD_STACK_BACKUP=/srv/b1-ai-hub/backups/old-stack-20260722-120000 \
  OPEN_WEBUI_PLAN=/srv/b1-ai-hub/backups/open-webui-migration-plan.json \
  CUTOVER_PLAN=/srv/b1-ai-hub/backups/cutover-plan.json \
  ROLLBACK_REPORT=/srv/b1-ai-hub/backups/rollback-rehearsal.json
```

See [tests/integration/README.md](./tests/integration/README.md), [tests/compatibility/README.md](./tests/compatibility/README.md), and [tests/security/README.md](./tests/security/README.md) for the full acceptance environment and evidence options.

## Default URLs

These virtual hosts are configurable through `.env`:

| URL | Purpose |
|---|---|
| `https://ai.b1.germering/` | Open WebUI |
| `https://control.ai.b1.germering/` | B1 AI Control Center |
| `https://media.ai.b1.germering/` | Media Studio |
| `https://comfy.ai.b1.germering/` | Native ComfyUI compatibility endpoint |
| `https://voice.ai.b1.germering/` | Voicebox compatibility endpoint |
| `https://models.ai.b1.germering/` | Model Hub and blobs |
| `https://api.ai.b1.germering/` | Unified API |

For LAN TLS, the default Caddy configuration uses an internal CA. See [docs/installation.md](./docs/installation.md) and [docs/security.md](./docs/security.md) before trusting the generated root certificate.

## Safety

Do not run migration or cutover scripts against the existing host until the inventory report has been reviewed and backups have been verified. The old stack, volumes, model directories, and Open WebUI data must remain recoverable throughout development and cutover.

The migration command sequence is intentionally staged:

```bash
make inventory
make old-stack-scope INVENTORY=/srv/b1-ai-hub/backups/inventory-20260722-120000.json
make old-stack-backup SCOPE=/srv/b1-ai-hub/backups/old-stack-scope.json
make old-stack-backup-verify BACKUP=/srv/b1-ai-hub/backups/old-stack-20260722-120000
make open-webui-migration-plan \
  INVENTORY=/srv/b1-ai-hub/backups/inventory-20260722-120000.json \
  BACKUP=/srv/b1-ai-hub/backups/old-stack-20260722-120000
make cutover-plan \
  INVENTORY=/srv/b1-ai-hub/backups/inventory-20260722-120000.json \
  SCOPE=/srv/b1-ai-hub/backups/old-stack-scope.json \
  BACKUP=/srv/b1-ai-hub/backups/old-stack-20260722-120000 \
  OPEN_WEBUI_PLAN=/srv/b1-ai-hub/backups/open-webui-migration-plan.json
```

These commands write generated migration artifacts to `B1_BACKUP_ROOT`, which defaults to `/srv/b1-ai-hub/backups`; set `B1_BACKUP_ROOT` to a protected writable directory when discovery is run before `/srv/b1-ai-hub` permissions are established.

`make cutover-plan` writes a runbook only. It does not stop containers, change DNS, delete data, or mark old resources removable.
