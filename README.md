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
- native ComfyUI `POST /prompt` admission through the global GPU lease with durable `native_prompt_id` recording, background history tracking, bidirectional `/ws` text/binary bridging, progress parsing, ComfyUI output ingestion into authenticated artifact storage, rejected-prompt idle cleanup, and `recovery_required` handling when native history produces no B1-manageable media output
- ComfyUI-backed media jobs that submit native prompt payloads or mapped published workflow JSON through the same GPU runner, record `native_prompt_id`, poll native history, ingest `/view` outputs, and release the scheduler lease after completion
- PostgreSQL-backed ComfyUI custom-node approval registry with seed-file merge, administrator-only approval/update APIs, Control Center node-pin management, workflow dependency refresh, mutating-route prefix enforcement, audit records, and backup/restore coverage
- LocalAI-backed image-generation media jobs that call OpenAI-compatible `/v1/images/generations`, persist returned `b64_json`, `data:` URL, or same-origin runtime URL media, and fail to `recovery_required` when no media is returned
- audio-cpu-backed async TTS/STT jobs and model smoke hooks that exercise CPU speech/transcription/embedding paths without taking the GPU lease
- database-backed model install planning from catalog IDs, uploaded manifests, or bounded HTTPS manifest URLs; licence-gated resumable direct-url and Hugging Face repository blob downloads with Control Center pause/resume and retry/requeue; verified staged-blob publication; hardlinked or safely extracted per-runtime read-only model views; acceptance model-smoke coverage and a per-alias model handoff action plan for required handoff aliases; profile-aware recoverable record/blob quarantine with confirmed quarantine retention cleanup; and catalog overlay refresh
- validated required model profiles for every public alias class, exposed in the Control Center with target runtime, resource estimate, default safety limits, selection guidance, and install/download-plan compatibility enforcement while exact GPU model choices remain replaceable pinned manifests
- runtime-agent with default-on internal mTLS, fail-closed token-protected allowlisted Docker status, bounded redacted logs surfaced through Control Center, disabled-by-default service mutations, and predefined runtime recover/unload actions
- runtime-agent CPU/memory/disk/GPU metrics and Control Center system self-test with all required gateway TLS routes, observed hardware/resource-policy fit, tiny inference, dry-run unload, and artifact delivery probes
- durable Control Center acceptance reports under `$B1_BACKUP_ROOT/acceptance/`, capturing self-test, metrics, resource policy, scheduler state, runtime reservations, runtime-agent service/image inventory, production Compose overlay/profile selection, recent update image refs, deployment-pin manifest integrity, source commit metadata, current database model-smoke coverage for required handoff aliases, structured operator evidence for live tests/backups/migration/restart reconciliation/rollback/security, machine-readable RTX GPU, LocalAI streaming/unload, installed workflow, native ComfyUI REST/WebSocket compatibility, remote-node compatibility, Model Hub client sync, Voicebox remote/server, deployed security including artifact authorization, restart reconciliation, web-managed backup/migration/rollback evidence, persisted model-smoke measurements for every required acceptance alias, preserved old resources from the reviewed cutover plan, Markdown handoff output, and checksums for cutover review
- lightweight Control Center observability backed by `GET /admin/metrics`, showing queue waits, recent job timing/resource summaries, model switches, runtime-agent availability, GPU telemetry, and host memory/storage without requiring Prometheus or Grafana for the base appliance
- optional `compose.monitoring.yaml` profile with pinned Prometheus and Grafana images, a metrics-only generated scrape token, internal-only backend services, and the gateway-routed `https://monitoring.ai.b1.germering/` Grafana host
- PostgreSQL-backed audit log for administrative changes with recursive metadata redaction and Control Center visibility
- AES-GCM encrypted configuration-secret storage for provider credentials, download tokens, runtime credentials, and integrations, backed by the generated master key outside Git and exposed through redacted admin UI/API controls
- persisted Control Center configuration for optional external runtimes, with remote-provider secret references, explicit external-data acknowledgement, SSRF-resistant URL validation, and fail-closed alias resolution
- persisted, administrator-editable RTX 3060/32 GB resource policy with hard bounds, dry-run validation, catalog admission refresh, and live GPU-runner reserve updates
- persisted administrator-controlled maintenance mode that blocks new inference/media/reservation work, admin job retries, and queued runner claims while leaving cancellation, backups, audit, and inspection paths available
- persisted controlled-update planning with pinned image digest validation, maintenance-gated staging backups, runtime-agent pinned-image staging, generated Compose image override artifacts, self-test recording, promotion handoff validation, and predefined runtime-agent rollback dry-run/execute metadata
- browser first-admin setup, scrypt password hashes, HttpOnly session cookies, CSRF-protected UI mutations, and web-managed CORS/trusted-proxy allowlists for LAN UI origins
- CPU and GPU job runners that persist durable job state, recover interrupted work, try graceful runtime unload hooks before restart fallback, verify runtime-agent VRAM metrics before GPU execution when available, cancel long audio-cpu/LocalAI/Voicebox runtime calls cooperatively, and fail unsupported runtime/job shapes without writing placeholder success artifacts
- Alembic-backed control-plane database migration runner that executes before Uvicorn and startup schema verification that fails closed on missing tables/columns
- durable Voicebox profile registry with Control Center create/export/delete flows, audit events, backup coverage, TTS alias/runtime validation, artifact-only voice sample references, and acceptance proof that sample IDs, URLs, hashes, exports, and audit targets remain consistent
- external `comfyui-b1-remote-nodes` package with unified-API nodes for model selection, chat, vision request shaping, embeddings, media jobs, uploads, artifacts, TTS, and STT without using the server-side ComfyUI endpoint
- external `b1-model-client` CLI/container daemon with Model Hub sync-plan support, pin/unpin, resumable verified blob sync, and managed-only safe prune for workstation model caches
- Model Hub blob delivery through the authenticated control-plane proxy with digest validation, downloadable-policy checks, per-client allowlists, CIDR enforcement, a generated internal artifact-server bearer token, Range/ETag/checksum support, and per-subject rate-limit headers
- committed OpenAPI 3.1 schema generated from the FastAPI app, plus generated TypeScript clients used by both web UIs, with CI drift checks
- GitHub Actions quality gates using fixed Ubuntu runner labels, immutable action commits, and scanner image digests, covering backend tests with all B1-owned service Python dependencies installed, Compose validation, Python source compilation, offline compatibility/security harnesses, Alembic packaging, frontend builds, NPM audit, source secret scanning, CycloneDX SBOM generation/validation, strict Python dependency audit for B1-owned services, uploaded Voicebox dependency audit inventory, service/frontend/integration container builds, strict Trivy scans for the smaller B1-owned images, and uploaded Trivy inventories for upstream-heavy Open WebUI, LocalAI, ComfyUI, and Voicebox images
- artifact-server and CPU audio service scaffolds
- placeholder internal `localai`, `comfyui`, and `voicebox` runtimes for scheduler/adapter development
- production LocalAI Compose override that replaces the placeholder `localai` service with a B1 wrapper image built from the official pinned CUDA 12 LocalAI image, mounts only the read-only LocalAI model view plus LocalAI writable state, exposes LocalAI's native API through the internal `:8080` proxy, and adds scheduler lifecycle hooks for load/warm/smoke/unload probes
- production ComfyUI Compose override and reproducible B1 image build from upstream ComfyUI `v0.3.77` commit `59afc3984868289f808d02fa5cd180edfb2de240`, with native `:8188` API behind the scheduler-aware proxy, read-only runtime model views, disabled API/cloud nodes, explicit input/output/temp/user dirs, RTX 3060 VRAM reserve defaults, and B1 lifecycle hooks installed as a custom-node route package
- production Voicebox Compose override and reproducible B1 image build from Jamie Pine Voicebox `v0.5.0` commit `2bcb98d1a8b6fe05e15fbc1559e3085669e4035d`, with a B1 proxy on native `:17493` that forwards REST/web/WebSocket traffic to loopback upstream, reports pinned proxy/upstream/source metadata on `/b1/runtime/build-info`, mounts read-only runtime model views, stores B1-managed profile/generation/cache data, and exposes conservative scheduler lifecycle hooks for load/warm/smoke/unload probes
- React/TypeScript Control Center with model lifecycle, workflow validation/publication, backup, self-test, audit, encrypted-secret, and external-client management surfaces plus schema-rendered Media Studio job submission/history with safe workflow presets, server-side workflow validation, SSE progress, and authenticated artifact downloads
- bootstrap, structured read-only migration inventory with NVIDIA/Docker GPU runtime readiness, Docker socket GID/runtime-agent access readiness, and redacted Open WebUI account/chat/settings/document-RAG table-domain counts, operator-reviewed old-stack backup scope/verification tooling, web-managed Open WebUI preservation-plan generation, non-destructive cutover/rollback plan generation, checksumed B1 backup/alternate-restore scripts, Control Center manual/scheduled backup, restore-import, and retention operations, chunked AES-GCM archive encryption for off-host copies, and control-plane PostgreSQL logical plus native custom-format dumps
- Control Center generated-artifact retention planning and confirmed cleanup for old terminal job artifacts, with Voicebox sample protection, symlink/path-scope refusal, job metadata marking, and HTTP 410 for reclaimed artifact downloads
- Control Center rollback rehearsal status and report generation from reviewed cutover plans, with fixed backup-root output and cutover-plan checksum binding
- configurable media-job queue/rate admission, artifact-storage headroom checks, and production GPU hardware/resource-policy admission for media jobs, native ComfyUI prompt jobs, GPU model smoke tests, runtime reservations, and synchronous GPU inference, exposed through `GET /admin/admission`, persisted admin policy APIs, Dashboard, Storage, and System tab editing, returning HTTP 429, 503, or 507 before work is accepted when limits or safety policy would be exceeded
- runtime reservation fleet visibility for administrators/operators with Jobs tab creation/cancellation controls and current GPU scheduler lease inspection
- opt-in live smoke tests for deployed API health, Open WebUI chat-host health/security-header proof, authenticated model listing, async TTS media jobs, non-placeholder TTS proof for every returned artifact, resolved runtime/model evidence, terminal SSE events, per-artifact download/metadata integrity, optional admin self-test, LocalAI streaming/unload acceptance, remote-node non-Comfy proof that server-side ComfyUI remains stopped after execution with registered node-surface/example coverage and file-backed artifact integrity, an RTX 3060 cross-runtime GPU acceptance sequence for LocalAI -> non-tiny ComfyUI prompt/native artifact proof -> Voicebox switching plus bounded runtime recovery, and a restart reconciliation drill that proves sampled waiting-job IDs are requeued, sampled non-resumable active-job IDs become `recovery_required`, and resumable native ComfyUI jobs are reattached by native prompt ID

The placeholder runtime containers are intentional at this stage. They keep `docker compose up -d` runnable on a small host without downloading model weights while the scheduler, registry, adapters, and UI flows are implemented. They are service-compatible placeholders to be replaced by pinned upstream runtime images/builds as each adapter reaches production readiness. The control plane now reports `deployment:compose-selection` and `runtimes:production-readiness` in `/admin/self-test` and `/admin/runtimes`: default `B1_RUNTIME_DEPLOYMENT_MODE=development` degrades when required runtime overlays or real services are missing, while `B1_RUNTIME_DEPLOYMENT_MODE=production` fails self-test until `B1_RUNTIME_PRODUCTION_REQUIRED` runtimes have their production Compose overlays/profiles selected and are real, healthy, non-placeholder, and verifiable through runtime-agent service inventory. The readiness check combines runtime health with runtime-agent service labels/image references, so a mock service cannot pass production mode merely by returning an `ok` health payload. The Control Center Runtimes tab renders a per-runtime production triage table with health status, active container count, image refs, placeholder reasons, blockers, and next actions before operators rerun the full live acceptance suite.

LocalAI, ComfyUI, and Voicebox now have production runtime overrides:

```bash
make prepare-production-env
docker compose up -d
```

The base Compose file remains the lightweight development topology, but the production env template sets `COMPOSE_FILE` and `COMPOSE_PROFILES` so the normal Compose command selects the real LocalAI, ComfyUI, and Voicebox overlays. `make prepare-production-env` copies or updates `.env` from that template and sets `B1_DOCKER_GID` from the host Docker socket so runtime-agent group access is ready for acceptance. `make bootstrap` is an optional preflight; the required fresh-install start command remains `docker compose up -d` because the Compose `bootstrap` service runs the same idempotent setup first. The explicit `-f` commands remain useful for partial runtime validation. See [deploy/localai/README.md](./deploy/localai/README.md), [deploy/comfyui/README.md](./deploy/comfyui/README.md), [deploy/voicebox/README.md](./deploy/voicebox/README.md), and [docs/installation.md](./docs/installation.md).

Seeded aliases are visible on first boot, but direct inference only runs for aliases backed by an installed manifest. Required model profiles in `model-catalog/seed/model-profiles.json` describe the target class, preferred runtime, resource estimate, and default safety limits for each alias group without hard-coding untested GPU model releases. The initial installed manifests are CPU-only development placeholders for embeddings, fast TTS, and STT; the scaffold CPU embedding endpoint returns deterministic local hash vectors for smoke testing, not semantic RAG quality. The catalog also includes `b1-piper-en-us-amy-low` as an available, checksum-pinned Piper voice recommendation for `tts-fast`, `b1-minilm-l6-v2-onnx-q4` as an available, checksum-pinned ONNX embedding recommendation for `embedding-default`, and `b1-vosk-small-en-us-0.15` as an available, checksum-pinned Vosk recommendation for `stt-default`; installing any of them creates a read-only `audio-cpu` runtime view and requests carry the immutable model ref to the runtime. The bundled CPU runtime marks scaffold responses with `b1_placeholder=true` or `X-B1-Placeholder: true` and can be disabled with `B1_CPU_AUDIO_ENABLE_PLACEHOLDER=false`, in which case scaffold endpoints return HTTP 503 until a real engine is configured. The default audio-cpu image installs checksum-pinned rhasspy/piper `2023.11.14-2` for real CPU TTS when `B1_CPU_AUDIO_ENGINE=piper`, includes pinned ONNX Runtime/tokenizer dependencies for real local embeddings when `B1_CPU_EMBEDDING_ENGINE=onnx`, and includes pinned Vosk bindings for real local PCM WAV transcription when `B1_CPU_STT_ENGINE=vosk`. GPU aliases remain visible as uninstalled until real validated model manifests are added.

With `B1_DEV_AUTH_BYPASS=false`, Control Center and Media Studio use browser sessions immediately after first-admin setup. Open WebUI reads a generated internal service key from `/run/secrets/open_webui_api_key`, and the control plane registers the matching hashed scoped API client on startup. The wrapper wires Open WebUI chat, RAG embeddings, TTS, and STT to the internal unified API by default and ignores Open WebUI's global provider config table so imported old settings cannot re-enable cloud/search routes; Open WebUI image generation stays disabled because B1 image/video operations are async Media Studio jobs.

## Quick Start

Review and copy the example environment:

```bash
cp .env.example .env
```

For the real-runtime appliance path, copy the production template instead:

```bash
make prepare-production-env
```

Optional preflight for host permissions, generated secrets, and the external data tree:

```bash
make bootstrap
```

Start the stack. The Compose `bootstrap` service runs the same idempotent setup first, so this is the required fresh-install start command:

```bash
docker compose up -d
```

Validate the repository, Compose configuration, Python source syntax, offline compatibility harnesses, and security policy checks:

```bash
make validate
```

Run the full local quality gate, including backend dependency installation in a temporary Python 3.12 venv, committed OpenAPI/schema-client drift checks, and both frontend builds/audits. If Python 3.12 is not installed locally, the backend Python checks run in the pinned Python 3.12 Docker image instead:

```bash
make quality
```

Regenerate the committed API schema and generated web clients after endpoint changes:

```bash
make openapi
make openapi-check
make openapi-client
make openapi-client-check
```

Run source hygiene gates locally when touching dependencies, images, or security-sensitive code:

```bash
make secret-scan
make sbom
make voicebox-audit-inventory
```

Enable optional Prometheus/Grafana monitoring only when needed. Bootstrap generates `$B1_DATA_ROOT/secrets/prometheus_scrape_token` and `$B1_DATA_ROOT/secrets/grafana_admin_password`; Grafana is then reachable only through the gateway host configured by `B1_HOST_MONITORING`:

```bash
COMPOSE_PROFILES=monitoring docker compose -f compose.yaml -f compose.monitoring.yaml up -d
```

Run opt-in live smoke checks against a deployed stack:

```bash
B1_SMOKE_LIVE_TEST=1 \
B1_SMOKE_OPEN_WEBUI_BASE=https://ai.b1.germering \
B1_AI_HUB_API_KEY=... \
B1_SMOKE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/live-smoke.json \
make live-smoke-acceptance
```

See [tests/smoke/README.md](./tests/smoke/README.md) for LAN TLS, temporary-host options, and the development-only placeholder override. Handoff smoke evidence requires chat-host Open WebUI health/security-header proof and real, non-placeholder TTS output.

Compose bootstrap seeds editable acceptance templates once under `/srv/b1-ai-hub/workflows/acceptance/`. Existing files there are preserved, so bind real checkpoint names, uploaded image names, and published workflow IDs in the external data-root copies before final handoff runs.

Generate a sourceable live-acceptance environment file with all default URLs, CA paths, prompt/job files, and evidence outputs:

```bash
make acceptance-env
```

The generated `$B1_BACKUP_ROOT/acceptance/operator-live-acceptance.env` intentionally contains no secrets and refuses to overwrite an existing file. It fills the default LAN URLs from the configured hosts, including `B1_SMOKE_OPEN_WEBUI_BASE` for the Open WebUI chat-host proof. Review it, fill the blank API-key, Model Hub, browser-session, and restart-drill values, then source it before running the live acceptance targets:

```bash
export B1_ACCEPTANCE_API_KEY=...
. /srv/b1-ai-hub/backups/acceptance/operator-live-acceptance.env
```

Before starting the full live group, run the non-network preflight. It parses the generated env file safely, checks required scoped keys and final handoff values, verifies Caddy CA and evidence paths, rejects unedited acceptance templates such as `REPLACE_WITH_*` placeholders or the tiny ComfyUI smoke prompt, and writes `$B1_BACKUP_ROOT/acceptance/operator-preflight.json` for the handoff report:

```bash
make acceptance-preflight
```

Record repository quality evidence only after the source tree is clean and the broad local gates pass:

```bash
make repository-quality-evidence
```

That target runs `make quality-container` and `make secret-scan`, then writes `$B1_BACKUP_ROOT/acceptance/repository-quality.json` with the exact source commit, clean/dirty state, explicit pass assertions for those prerequisite gates, and per-coverage test-result summaries consumed by the final handoff report. Running the evidence script directly without those Make prerequisites leaves the checks unverified and is rejected for handoff.

Run LocalAI runtime acceptance after a real chat alias is installed and smoke-tested, then run target-host cross-runtime GPU acceptance after real GPU models, persisted model-smoke measurements, and a ComfyUI API prompt are installed:

```bash
B1_LOCALAI_ACCEPTANCE_API_KEY=... \
B1_LOCALAI_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/localai-runtime.json \
make localai-acceptance

B1_GPU_ACCEPTANCE_API_KEY=... \
B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json \
B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1 \
B1_GPU_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/cross-runtime-gpu.json \
make gpu-acceptance
```

The GPU acceptance prompt must be a real installed ComfyUI API workflow. The bundled `native-comfyui-smoke-prompt.json` route-level smoke prompt can be allowed with `B1_GPU_ACCEPTANCE_ALLOW_COMFY_TINY_SMOKE=1` for a labelled dry run, but final handoff evidence remains incomplete unless the ComfyUI leg records non-tiny prompt metadata, the native prompt ID, and verified B1 artifact downloads.

Run installed workflow acceptance after chat, CPU TTS/STT, image, edit, and short-video aliases are backed by real installed models with persisted successful model-smoke measurements:

```bash
B1_WORKFLOWS_LIVE_TEST=1 \
B1_WORKFLOWS_API_KEY=... \
B1_WORKFLOWS_IMAGE_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/image-generation-job.json \
B1_WORKFLOWS_IMAGE_EDIT_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/image-edit-job.json \
B1_WORKFLOWS_VIDEO_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/short-video-job.json \
B1_WORKFLOWS_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/installed-workflows.json \
make installed-workflows-acceptance
```

Run native ComfyUI compatibility from an external client. By default, `make native-comfyui-compatibility` uses the checked-in `workflows/acceptance/native-comfyui-smoke-prompt.json` to produce a deterministic tiny image through the B1 ComfyUI hook; that is useful for fast route-level rehearsals without model weights. Production handoff must override `B1_NATIVE_COMFYUI_PROMPT_FILE` with a real prompt such as the edited text-to-image template, and acceptance reports reject evidence generated from the tiny smoke prompt.

```bash
B1_NATIVE_COMFYUI_LIVE_TEST=1 \
B1_NATIVE_COMFYUI_BASE=https://comfy.ai.b1.germering \
B1_NATIVE_COMFYUI_API_KEY=... \
B1_NATIVE_COMFYUI_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json \
B1_NATIVE_COMFYUI_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/native-comfyui.json \
make native-comfyui-compatibility
```

Run the external compatibility group after the native ComfyUI prompt file, remote-node credentials, Model Hub sync client, and Voicebox settings are configured:

```bash
make external-compatibility-acceptance
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

After all required live-test environment variables are set and the restart-reconciliation drill state has been prepared, the operator can run the live evidence group:

```bash
make acceptance-preflight
make operator-live-acceptance
```

`make operator-live-acceptance` also depends on `repository-quality-evidence` and `acceptance-preflight`, so a dirty source tree, failed local quality gate, stale environment, or unsafe local handoff setting stops before the live API/GPU tests run. The group writes repository-quality, operator-preflight, live smoke, installed workflow, LocalAI, GPU, compatibility, security, and restart-reconciliation evidence files under `$B1_BACKUP_ROOT/acceptance/`. Backup, migration, cutover, and rollback evidence is still generated from reviewed backup and runbook artifacts.

Generate backup, migration, and rollback handoff evidence after B1 backup verification, alternate-directory restore rehearsal, old-stack migration review, and rollback rehearsal:

```text
Control Center -> System -> Backup/Migration/Rollback Evidence -> Generate
```

The web action auto-selects the latest valid direct-child artifacts under `$B1_BACKUP_ROOT`, pairs the B1 backup with `$B1_RESTORE_TEST_ROOT/<backup-name>/restore-report.json`, writes `$B1_BACKUP_ROOT/acceptance/backup-migration-rollback.json`, and never accepts arbitrary host paths.

```bash
make rollback-rehearsal-report \
  CUTOVER_PLAN=/srv/b1-ai-hub/backups/cutover-plan.json \
  REHEARSED_BY=operator-name \
  ROLLBACK_COMMANDS_TESTED=1 \
  OLD_RESOURCES_PRESERVED=1

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

For LAN TLS, the default Caddy configuration uses an internal CA. Control Center -> System -> LAN TLS CA shows the root certificate fingerprint and authenticated download action. See [docs/installation.md](./docs/installation.md) and [docs/security.md](./docs/security.md) before trusting the generated root certificate.

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
