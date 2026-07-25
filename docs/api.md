# API

The unified API is served at `https://api.ai.b1.germering/`.

Implemented scaffold endpoints:

```text
GET  /auth/status
POST /auth/setup
POST /auth/login
POST /auth/logout
GET  /admin/status
GET  /admin/metrics
GET  /admin/metrics.prometheus
GET  /admin/admission
GET  /admin/admission-policy
POST /admin/admission-policy/validate
PUT  /admin/admission-policy
DELETE /admin/admission-policy
GET  /admin/network-policy
POST /admin/network-policy/validate
PUT  /admin/network-policy
DELETE /admin/network-policy
GET  /admin/maintenance
PUT  /admin/maintenance
GET  /admin/updates
POST /admin/updates
GET  /admin/updates/{update_id}
POST /admin/updates/{update_id}/stage
POST /admin/updates/{update_id}/promote
POST /admin/updates/{update_id}/health-check
POST /admin/updates/{update_id}/rollback
GET  /admin/api-clients
POST /admin/api-clients
PUT  /admin/api-clients/{client_id}/cidr-allowlist
DELETE /admin/api-clients/{client_id}
GET  /admin/scheduler/lease
POST /admin/scheduler/lease
GET  /admin/runtime-reservations
GET  /admin/runtimes
POST /admin/runtimes/{runtime}/recover
POST /admin/runtimes/{runtime}/unload
GET  /admin/voicebox/profiles
POST /admin/voicebox/profiles
GET  /admin/voicebox/profiles/{profile_id}
PATCH /admin/voicebox/profiles/{profile_id}
POST /admin/voicebox/profiles/{profile_id}/export
DELETE /admin/voicebox/profiles/{profile_id}
GET  /admin/self-test
GET  /admin/scheduler/reconciliation
GET  /admin/acceptance-reports
POST /admin/acceptance-reports
GET  /admin/acceptance-reports/{report_id}
GET  /admin/migration/open-webui-plan
POST /admin/migration/open-webui-plan
GET  /admin/migration/rollback-rehearsal
POST /admin/migration/rollback-rehearsal
GET  /admin/migration/backup-migration-rollback-evidence
POST /admin/migration/backup-migration-rollback-evidence
GET  /admin/audit-log
POST /admin/artifacts/retention-plan
POST /admin/artifacts/cleanup
POST /admin/models/quarantine/retention-plan
POST /admin/models/quarantine/cleanup
GET  /admin/jobs
GET  /admin/jobs/{job_id}
GET  /admin/jobs/{job_id}/events
POST /admin/jobs/{job_id}/priority
POST /admin/jobs/{job_id}/cancel
POST /admin/jobs/{job_id}/retry
GET  /admin/models
POST /admin/models/install-plan
POST /admin/models/download-plan
GET  /admin/models/downloads
GET  /admin/models/downloads/{download_id}
POST /admin/models/downloads
DELETE /admin/models/downloads/{download_id}
POST /admin/models/downloads/{download_id}/pause
POST /admin/models/downloads/{download_id}/resume
POST /admin/models/downloads/{download_id}/retry
POST /admin/models/install
POST /admin/models/quarantine/retention-plan
POST /admin/models/quarantine/cleanup
DELETE /admin/models/{id}/versions/{version}
GET  /admin/models/{id}/versions/{version}/blob-quarantine-plan
POST /admin/models/{id}/versions/{version}/blobs/quarantine
GET  /admin/comfyui/node-pins
POST /admin/comfyui/node-pins
PATCH /admin/comfyui/node-pins/{node_id}/commits/{commit}
GET  /workflows/v1/published
GET  /workflows/v1/published/{id}
GET  /workflows/v1/published/{id}/versions/{version}
POST /workflows/v1/validate
POST /workflows/v1/published
DELETE /workflows/v1/published/{id}/versions/{version}
GET  /v1/models
POST /v1/chat/completions
POST /v1/responses
POST /v1/embeddings
POST /v1/audio/speech
POST /v1/audio/transcriptions
POST /v1/images/generations
POST /v1/images/edits
POST /v1/media/uploads
POST /v1/media/jobs
GET  /v1/media/jobs
GET  /v1/media/jobs/{job_id}
DELETE /v1/media/jobs/{job_id}
GET  /v1/media/jobs/{job_id}/events
GET  /v1/media/jobs/{job_id}/artifacts
GET  /artifacts/{artifact_path}
HEAD /artifacts/{artifact_path}
POST /v1/runtime-reservations
GET  /v1/runtime-reservations/{id}
DELETE /v1/runtime-reservations/{id}
GET  /modelhub/v1/catalog
GET  /modelhub/v1/models/{id}
GET  /modelhub/v1/models/{id}/versions
GET  /modelhub/v1/blobs/{sha256}
HEAD /modelhub/v1/blobs/{sha256}
POST /modelhub/v1/sync/plan
GET  /modelhub/v1/clients
POST /modelhub/v1/clients
PUT  /modelhub/v1/clients/{id}/cidr-allowlist
PUT  /modelhub/v1/clients/{id}/policy
DELETE /modelhub/v1/clients/{id}
```

## Self-Test and Metrics

`GET /admin/self-test` requires `admin:read` and returns an overall `ok`, `degraded`, or `failed` status plus individual checks for PostgreSQL, Redis, storage permissions, runtime health, runtime production-readiness, runtime-agent status, runtime-agent metrics, GPU metric availability, TLS gateway routing and Caddy security headers, a tiny embedding inference, runtime-agent unload capability, and artifact-server Range delivery.

`GET /admin/metrics?limit=500` requires `admin:read` and returns the lightweight in-app observability report used by the Dashboard. It combines durable PostgreSQL job records, scheduler ownership, runtime state rows, and runtime-agent telemetry into queue depth/wait summaries, recent completed/failed/cancelled job counts, load/run-time summaries, peak RAM/VRAM summaries, model-switch counts from started jobs, active runtime status, host memory/storage, and normalized GPU utilization, temperature, power, and memory values. The endpoint remains reachable when runtime-agent metrics are unavailable; the response then sets `runtime_agent.available=false` and includes the error instead of failing the dashboard.

`GET /admin/metrics.prometheus?limit=500` requires the same `admin:read` scope and returns the same observability snapshot as Prometheus text exposition with `text/plain; version=0.0.4`. It exposes queue depth/waits by state and priority, recent job outcome/load/run/resource summaries, model switches, runtime-state counts, runtime-agent availability, GPU telemetry, host memory/swap, CPU load, and configured storage counters. Labels are restricted to operational categories such as state, priority, runtime, status, stage, and summary statistic; prompts, model identifiers, owner IDs, job IDs, artifacts, paths, and credentials are not emitted as labels.

`GET /admin/admission` requires `admin:read` and returns the current admission policy, queue counters, and artifact-storage headroom used before accepting new media jobs or uploads. The optional `owner_id` query parameter is restricted to administrators and operators; other authenticated subjects receive their own counters. `/admin/status` embeds the authenticated subject's same admission report for Dashboard display.

Runtime production-readiness is controlled by `B1_RUNTIME_DEPLOYMENT_MODE` and `B1_RUNTIME_PRODUCTION_REQUIRED`. In `development` mode, placeholders or unhealthy required runtimes create a warning. In `production` mode, they fail the self-test so cutover cannot treat mock LocalAI, mock ComfyUI, mock Voicebox, or scaffold CPU audio as accepted production backends.

Stable acceptance-oriented check names include `database`, `redis`, `runtimes`, `runtime-agent:status`, `runtime-agent:mutation-guard`, `runtime-agent:metrics`, `gpu:nvml`, `tls:routing`, `inference:tiny`, `runtime-agent:unload`, and `artifact:delivery`. The TLS routing check fails if a configured HTTPS route is unreachable, returns an error status, or omits the expected Caddy security headers. The unload check uses `dry_run=true`, so it proves the authenticated runtime-agent path without restarting containers.

`GET /admin/scheduler/reconciliation` requires `runtimes:read` and returns the current control-plane start timestamp plus enabled CPU, GPU, and model-download runner startup reconciliation records. Each enabled job runner records whether startup reconciliation completed, which runtime names it covered, how many interrupted `waiting_for_gpu` jobs were requeued, and how many active preparation/execution jobs were marked `recovery_required`. When the model-download runner is enabled, the same report includes resumable interrupted downloads requeued from `running` plus downloads settled from `pausing` to `paused` and from `cancelling` to `cancelled`.

`POST /admin/acceptance-reports` requires administrator `admin:write`, reruns the same self-test, snapshots `GET /admin/metrics`, the effective resource policy, admission headroom, maintenance state, scheduler lease, runtime state rows, active runtime reservations, runtime-agent `/v1/services` deployment inventory, recent controlled-update records, available source commit metadata, submitted operator evidence, the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` live GPU, LocalAI runtime, installed workflow, native ComfyUI compatibility, remote-node compatibility, Model Hub client sync, Voicebox remote compatibility, deployed security, restart reconciliation, and backup/migration/rollback evidence files, and the latest supported direct-child `$B1_BACKUP_ROOT/cutover-plan*.json` preservation summary, then writes a JSON report, Markdown handoff report, and `SHA256SUMS` under `$B1_BACKUP_ROOT/acceptance/<report_id>/`. `GET /admin/acceptance-reports` lists summaries and `GET /admin/acceptance-reports/{report_id}` returns the stored report. A report is marked `operator_handoff_ready=true` only when the self-test is `ok`, runtime deployment mode is `production`, runtime production-readiness is `ok`, GPU/NVML evidence is present and healthy, deployment image evidence is available, source-control evidence includes a valid 40-character commit, every required operator-evidence item is checked, the live RTX evidence file reports `status=ok` with required cross-runtime checks present, the LocalAI runtime evidence file reports `status=ok` with streamed chat, single-backend, and confirmed unload checks present, the installed workflow evidence file reports `status=ok` with chat, TTS, STT, CPU-audio no-GPU-lease proof, image generation, image edit, and short-video checks present, the native ComfyUI evidence file reports `status=ok` with required REST/WebSocket checks plus `Idempotency-Key` prompt replay present, the remote-node non-Comfy evidence file reports `status=ok` with required external-consumer checks present, the Model Hub client evidence file reports `status=ok` with range/resume, cache, prune, and inference-only checks present, the Voicebox evidence file reports `status=ok` with native HTTP, profile lifecycle, speech-or-limitation, and WebSocket-or-limitation checks present, the security evidence file reports `status=ok` with unauthenticated, under-scoped, CORS, CSRF, ComfyUI route-policy, SSRF, traversal, artifact authorization, runtime-agent mutation-guard, and log-redaction checks present, the restart reconciliation evidence file reports `status=ok` with control-plane restart, CPU runner, GPU runner, waiting-job requeue, and active-job recovery-required checks present, the backup/migration/rollback evidence file reports `status=ok` with B1 backup creation/verification, B1 restore rehearsal, old-stack inventory review, old-stack backup verification, Open WebUI migration-plan review, cutover-plan review, rollback rehearsal, and old-resource preservation checks present, and the cutover plan lists preserved rollback resources with verified old-stack backup evidence, accepted hardware readiness, resolved Open WebUI preservation, and no unresolved cutover warnings. Required operator evidence covers live stack smoke, RTX 3060 acceptance, installed-model chat/TTS/STT/image/video validation plus CPU-audio GPU-lease isolation, native ComfyUI compatibility, remote-node non-Comfy execution, Model Hub sync, Voicebox remote/server validation or documented pinned-upstream limitation, backup verification, restore rehearsal, migration/cutover review, restart reconciliation, rollback rehearsal with old resources preserved, and security review. The deployment inventory includes service image references plus resolved Docker image IDs when runtime-agent can read the Docker API; recent update records preserve digest-pinned image refs used for controlled promotion.

`GET /admin/migration/open-webui-plan` returns readiness for the Open WebUI preservation plan: latest valid direct-child old-stack `inventory*.json`, old-stack backup directory, fixed restore-test target, and current generated `open-webui-migration-plan*.json` under `$B1_BACKUP_ROOT`. `POST /admin/migration/open-webui-plan` requires administrator `admin:write`, verifies the selected old-stack backup, writes only a generated `$B1_BACKUP_ROOT/open-webui-migration-plan-*.json`, and audits the selected strategy and warning count. It does not read Open WebUI row contents, import data, modify the old stack, or accept arbitrary host paths.

`GET /admin/migration/rollback-rehearsal` returns the latest generated `cutover-plan*.json` status and the current `$B1_BACKUP_ROOT/rollback-rehearsal.json` status. `POST /admin/migration/rollback-rehearsal` requires administrator `admin:write`, accepts only a generated cutover-plan file name under `$B1_BACKUP_ROOT`, requires explicit `rollback_commands_tested=true` and `old_resources_preserved=true`, and writes a fixed `rollback-rehearsal.json` report with the cutover-plan SHA-256. It does not execute rollback commands or accept arbitrary host paths.

`GET /admin/migration/backup-migration-rollback-evidence` returns readiness for the final backup/migration/rollback evidence bundle: latest valid B1 backup, matching restore report, old-stack inventory, old-stack backup, Open WebUI migration plan, cutover plan, rollback rehearsal report, and current output file. `POST /admin/migration/backup-migration-rollback-evidence` requires administrator `admin:write` and `confirm_reviewed=true`, verifies the selected artifacts, and writes only `$B1_BACKUP_ROOT/acceptance/backup-migration-rollback.json`. The endpoint auto-selects known artifacts from configured roots and never accepts arbitrary host paths.

Native ComfyUI compatibility evidence includes `/object_info`, `/object_info/{node}`, `/system_stats`, `/models`, `/queue`, `/upload/image`, `/upload/mask`, `POST /prompt`, same-body `Idempotency-Key` replay returning the native `prompt_id`, `/ws`, `/history`, `/history/{prompt_id}`, non-destructive `POST /queue` deletion and targeted `POST /interrupt` after completion, and `/view` retrieval for at least one generated image, video, GIF, or audio artifact.

The runtime-agent internal `GET /v1/metrics` endpoint is protected by the internal runtime-agent mTLS channel and bearer token, and returns CPU/load, host memory/swap, configured disk paths, and GPU telemetry from `nvidia-smi` when available. It does not accept command, path, or device parameters from callers.

`GET /admin/services/{service}/logs?lines=100` requires `runtimes:read` and administrator or operator role. `service` is restricted to the B1 services exposed by the runtime-agent log allowlist: `control-plane`, `localai`, `comfyui`, `voicebox`, `audio-cpu`, `artifact-server`, `open-webui`, and `gateway`. `lines` is bounded to `1..500`. The control plane calls runtime-agent through the internal mTLS/token channel, applies a second redaction/truncation pass, and returns `{"service":"control-plane","lines":100,"entries":["..."]}`. This is the API behind the Control Center System log viewer.

Runtime recovery actions are exposed to administrators through `POST /admin/runtimes/{runtime}/recover` and `POST /admin/runtimes/{runtime}/unload`:

```bash
curl -s https://api.ai.b1.germering/admin/runtimes/localai/recover \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"reason":"failed unload after validation run","timeout_seconds":10}'
```

The control plane validates that the selected adapter is configured and non-external, then calls the mTLS/token-protected runtime-agent predefined action endpoint. The runtime-agent only accepts services in `B1_RUNTIME_ACTION_SERVICES` and implements the current forced unload/recovery strategy as a bounded restart of that allowlisted runtime service. With the default `B1_ENABLE_MUTATIONS=false`, these endpoints return a dry-run response.

Runtime-agent `/v1/images/{service}/inspect` and `/v1/images/{service}/pull` are internal update-staging endpoints. Both require an allowlisted service name and an image reference pinned with `@sha256:<digest>`; `latest` and missing digests are rejected. Pull is a mutation and returns `status=dry_run` unless `B1_ENABLE_MUTATIONS=true`. Runtime-agent `/v1/rollback` is intentionally internal and predefined. It restarts only the static `B1_ROLLBACK_SERVICES` sequence after validating every entry against `B1_ALLOWED_SERVICES`; it accepts only the common `reason` and `timeout_seconds` payload. With mutations disabled it returns the rollback plan as a dry run.

## Network Policy

Administrators manage browser CORS origins and trusted reverse-proxy CIDRs through the System tab or these endpoints:

```text
GET    /admin/network-policy
POST   /admin/network-policy/validate
PUT    /admin/network-policy
DELETE /admin/network-policy
```

`PUT` persists a single default policy in PostgreSQL and updates the live control-plane cache immediately:

```bash
curl -X PUT https://api.ai.b1.germering/admin/network-policy \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"cors_allow_origins":["https://control.ai.b1.germering","https://media.ai.b1.germering"],"trusted_proxy_cidrs":["127.0.0.1/32","172.16.0.0/12"]}'
```

CORS origins are canonicalized to `scheme://host[:port]`; wildcard and `null` origins are rejected because browser credentials are allowed only for explicit origins. `trusted_proxy_cidrs` controls when the control plane trusts `X-Forwarded-For` or `Forwarded` headers for API-client and Model Hub CIDR checks. `DELETE` returns to the environment defaults from `B1_CORS_ALLOW_ORIGINS` and `B1_TRUSTED_PROXY_CIDRS`.

## Model Hub Clients

Administrators manage external sync clients through `GET`, `POST`, `PUT`, and `DELETE` under `/modelhub/v1/clients`. The create response is the only time the full API key is returned. CIDR allowlists can be updated separately from policy so a workstation can move networks without changing its download permissions:

```bash
curl -X PUT https://models.ai.b1.germering/modelhub/v1/clients/mhc_123/policy \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"allowed_models":["image-default","image-upscale"],"allow_downloads":true}'
```

`allowed_models` accepts explicit aliases/model IDs or `["*"]`. Dedicated Model Hub clients see only permitted aliases and manifest records in `/modelhub/v1/catalog`, and the same policy gates model detail, sync-plan, and blob access. `allow_downloads=false` leaves the client able to read permitted catalog metadata while blocking sync plans and blob downloads. Policy and CIDR changes write audit records; revoked clients cannot be modified.

## Maintenance Mode

Maintenance mode is controlled by administrators:

```text
GET /admin/maintenance
PUT /admin/maintenance
```

`GET` requires `admin:read`; `PUT` requires `admin:write`. Both require the `admin` role. Enabling requires a non-empty reason and persists the state in PostgreSQL:

```bash
curl -X PUT https://api.ai.b1.germering/admin/maintenance \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"reason":"staged update and backup validation"}'
```

While enabled, new `/v1/chat/completions`, `/v1/responses`, `/v1/embeddings`, `/v1/audio/*`, `/v1/images/*`, `/v1/media/uploads`, `/v1/media/jobs`, `/v1/runtime-reservations`, native ComfyUI `POST /prompt`, and admin job retry requests return `503` with `detail.message=maintenance mode is active`. CPU, GPU, and model-download runners do not claim queued work while the flag is active. Read, cancel, backup, audit, model-management, and runtime-inspection APIs remain available.

## Controlled Updates

Controlled application update planning is administrator-only:

```text
GET  /admin/updates
POST /admin/updates
GET  /admin/updates/{update_id}
POST /admin/updates/{update_id}/stage
POST /admin/updates/{update_id}/promote
POST /admin/updates/{update_id}/health-check
POST /admin/updates/{update_id}/rollback
```

`POST /admin/updates` records a target version, optional HTTPS release URL, notes, and pinned image references. Every image reference must include an immutable `@sha256:<digest>` and `latest` is rejected:

```bash
curl -X POST https://api.ai.b1.germering/admin/updates \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target_version":"0.2.0","source_url":"https://github.com/B1-Mordred/localAIcentre/releases/tag/v0.2.0","image_refs":[{"service":"control-plane","image":"ghcr.io/b1/b1-ai-hub-control-plane:0.2.0@sha256:0000000000000000000000000000000000000000000000000000000000000000"}]}'
```

Staging, promotion, health-check, and rollback require maintenance mode. Staging creates a normal constrained backup, asks runtime-agent to stage each pinned service image through `/v1/images/{service}/pull`, stores the image staging result on `image_stage`, writes a digest-pinned Compose override to `$B1_DATA_ROOT/data/control-plane/updates/<update_id>/compose.images.yaml`, and records the override metadata plus the same `/admin/self-test` report on the update row. With default `B1_ENABLE_MUTATIONS=false`, image staging is recorded as a dry run and `compose_override.ready_for_promotion=false` until the pinned images are actually pulled.

`POST /admin/updates/{update_id}/promote` is allowed only after the update is `validated`. It verifies the generated override's expected relative path, SHA-256, service list, image-stage results, and runtime-agent `/v1/images/{service}/inspect` digest results. On success the row becomes `promotion_ready` and `promotion_result` contains a fixed operator handoff command such as:

```bash
docker compose -f compose.yaml -f /srv/b1-ai-hub/data/control-plane/updates/<update_id>/compose.images.yaml up -d --no-build control-plane gateway
```

Run that command from the repository root during maintenance, then call `POST /admin/updates/{update_id}/health-check` again. The control-plane container runs Alembic migrations before serving the promoted image; `B1_DB_MIGRATIONS_ENABLED=false` is only for externally managed deployments that apply `python -m app.migrate upgrade head` separately. Health-check refreshes the self-test report after promotion. Rollback calls only the runtime-agent predefined `/v1/rollback` plan; with default mutations disabled the result is stored as `rollback_dry_run`. This surface does not accept arbitrary commands, images without digests, host paths, environment changes, or Docker API passthrough.

## Voicebox Profiles

Voicebox profiles are managed by administrators and operators through Control Center or the admin API:

```bash
curl -s https://api.ai.b1.germering/admin/voicebox/profiles \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY"

curl -s https://api.ai.b1.germering/admin/voicebox/profiles \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"narrator","runtime":"voicebox","engine":"voicebox","model_alias":"tts-quality","profile_type":"clone","visibility_roles":["admin","operator"],"metadata":{"language":"en"},"sample_artifacts":[{"url":"/artifacts/voicebox/references/narrator.wav","sha256":"0000000000000000000000000000000000000000000000000000000000000000","mime_type":"audio/wav","bytes":4096}]}'
```

`GET /admin/voicebox/profiles` requires `runtimes:read` and an `admin` or `operator` role. It supports `include_deleted`, `runtime`, `status`, and `owner_id` filters. `POST` and `PATCH` require `runtimes:write`; `DELETE` soft-deletes the profile by marking it `deleted` and setting `deleted_at`, so backups and audits can still recover the record. `POST /admin/voicebox/profiles/{profile_id}/export` returns a portable profile metadata bundle and writes an audit event.

Profile creation validates that `model_alias` is a TTS alias compatible with `runtime`, which is currently either `voicebox` or `audio-cpu`. `metadata` is capped and may not contain inline audio, base64, local file paths, prompts, secrets, or voice sample fields. Reference samples and cloned-voice material must be stored as normal artifacts and linked through `sample_artifacts`; the profile table stores only artifact URLs, SHA-256 values, MIME type, and byte count.

## Audit Log

`GET /admin/audit-log` requires `admin:read` and returns recent durable audit events from PostgreSQL:

```bash
curl -s 'https://api.ai.b1.germering/admin/audit-log?limit=50' \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY"
```

Optional filters are `event_type`, `actor_id`, and `target_type`. Audit events record actor ID, role, API-key prefix where applicable, event type, target, summary, correlation ID when available, and redacted metadata. The metadata redactor recursively removes token, secret, prompt, message, upload, voice, image, audio, video, and document fields before storage, and it scans otherwise safe string fields for bearer tokens, B1 API keys, GitHub PAT-shaped strings, and credential query parameters. Free-form administrative reasons are recorded in audit metadata only as `reason_provided`; runtime-agent mutation labels and controlled-update handoff labels similarly record only the operation and whether an operator reason was supplied. The audit table is included in the control-plane logical PostgreSQL export and native PostgreSQL dump used by backups.

`POST /admin/backups` requires `storage:write` and creates a constrained backup under `$B1_BACKUP_ROOT`. Each backup includes the logical JSON export at `data/control-plane/postgres-logical-export.json` plus a native `pg_dump` custom-format file at `data/control-plane/postgres-native.dump`; public summaries expose `postgres_dump`, `postgres_dumps[]`, `postgres_native_dump`, and `archive_encryption` metadata but never database credentials or encryption keys. `POST /admin/backups/{backup_name}/verify` checks the archive manifest, decrypts encrypted-only payloads with the configured master key when required, and verifies the native dump archive member checksum. `POST /admin/backups/{backup_name}/restore-test` extracts to `$B1_RESTORE_TEST_ROOT/<backup_name>` and runs `pg_restore --list` against the extracted native dump when present. `POST /admin/backups/{backup_name}/postgres-import` with `apply=false` returns a dry-run logical import plan after a restore test. Applying rows with `apply=true` additionally requires administrator role, maintenance mode, and `confirm_backup_name` matching the path parameter.

Backup retention is available through `POST /admin/backups/retention-plan` with `storage:read` and `POST /admin/backups/cleanup` with `storage:write`. Both accept `keep_last` and optional `delete_older_than_days`; cleanup also requires `confirm=true`. The cleanup endpoint re-runs the plan and deletes only validated direct child backup directories under `$B1_BACKUP_ROOT`, while symlinked or invalid entries are reported and preserved.

Generated artifact retention is available through `POST /admin/artifacts/retention-plan` with `storage:read` and `POST /admin/artifacts/cleanup` with `storage:write`. Requests accept `delete_older_than_days`, optional generated-output `namespaces` such as `localai`, `comfyui`, `audio-cpu`, or `voicebox`, a bounded `limit`, and `confirm=true` for cleanup. The planner considers only artifacts recorded on terminal jobs older than the cutoff, refuses staged input/temporary/secret namespaces, preserves active or newer jobs, preserves Voicebox profile sample artifacts, rejects symlinks, missing files, non-files, metadata size mismatches, traversal, and artifacts whose path is not scoped to the job ID or native ComfyUI prompt ID. Cleanup re-runs the plan, unlinks only accepted files under `$B1_ARTIFACT_ROOT`, marks the affected job artifact metadata with `retention_status=deleted`, and writes an audit event. Later downloads of a deleted artifact return HTTP 410.

Recoverable model-blob quarantine retention is available through `POST /admin/models/quarantine/retention-plan` with `storage:read` and `POST /admin/models/quarantine/cleanup` with `storage:write`. Requests accept `delete_older_than_days`, a bounded `limit`, and `confirm=true` for cleanup. The planner scans only `$B1_DATA_ROOT/models/quarantine/blobs`, accepts quarantine set directories named with the existing `<version>-YYYYMMDDTHHMMSSZ` suffix, and preserves malformed entries, symlinks, nested directories, non-regular files, or non-SHA-256 filenames for operator review. Cleanup re-runs the plan and deletes only validated old quarantine set directories; it never deletes active authoritative blobs under `$B1_DATA_ROOT/models/blobs` or runtime views.

Scheduled backups are managed through:

```text
GET  /admin/backups/schedule
PUT  /admin/backups/schedule
POST /admin/backups/schedule/run
```

`GET` requires `storage:read`; update and run-now require `storage:write`. The schedule row persists `enabled`, `interval_hours`, `keep_last`, optional `delete_older_than_days`, `label_prefix`, next-run time, last-run status, and last backup name. The background scheduler is enabled by `B1_BACKUP_SCHEDULER_ENABLED=true`, polls at `B1_BACKUP_SCHEDULER_INTERVAL_SECONDS`, atomically claims due schedules in PostgreSQL, creates the same constrained backup with logical and native PostgreSQL dumps as `POST /admin/backups`, applies retention with explicit internal confirmation, advances `next_run_at`, and writes a system audit event.

```bash
curl -X PUT https://api.ai.b1.germering/admin/backups/schedule \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"interval_hours":24,"keep_last":7,"delete_older_than_days":30,"label_prefix":"scheduled","run_immediately":false}'
```

## Encrypted Secrets

Administrator-only encrypted value management is exposed at:

```text
GET    /admin/secrets
GET    /admin/secrets/{name}
PUT    /admin/secrets/{name}
POST   /admin/secrets/{name}/verify
DELETE /admin/secrets/{name}
```

These routes require an `admin` role. Read routes require `admin:read`; store and delete require `admin:write`. Values are encrypted with the generated master key from `/run/secrets/master_encryption_key` before PostgreSQL storage. Responses never include plaintext, nonce, ciphertext, or the raw envelope; they expose only metadata such as name, category, envelope scheme, key fingerprint, timestamps, and soft-delete state.

Store or rotate a value:

```bash
curl -X PUT https://api.ai.b1.germering/admin/secrets/remote:openai \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"OpenAI optional provider","category":"remote-provider","description":"disabled by default","value":"secret-value"}'
```

Verify decryptability without exposing the value:

```bash
curl -X POST https://api.ai.b1.germering/admin/secrets/remote:openai/verify \
  -H "Authorization: Bearer $B1_ADMIN_KEY"
```

Store a bearer token for authenticated model downloads with category `model-download`:

```bash
curl -X PUT https://api.ai.b1.germering/admin/secrets/model-download:hf \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"Hugging Face read token","category":"model-download","description":"direct-url model downloads","value":"hf_..."}'
```

## Resource Policy

The effective RTX 3060/32 GB resource policy is exposed at:

```text
GET    /admin/resource-policy
POST   /admin/resource-policy/validate
PUT    /admin/resource-policy
DELETE /admin/resource-policy
```

These routes require an `admin` role. Read requires `admin:read`; validation, update, and reset require `admin:write`. `GET` returns the effective policy, environment default, hard bounds, source (`environment` or `database`), and the persisted override row when present. `POST /validate` returns `accepted=false` plus errors without storing anything. `PUT` persists the policy, refreshes catalog resource admission, updates the live GPU runner VRAM reserve, and audits the change. `DELETE` removes the override and returns to the environment profile.

The default hard bounds enforce the initial host constraints: no browser update can exceed the environment-defined 12 GiB GPU memory or 32 GiB host RAM profile, and the single-GPU controls remain fixed at one active GPU pipeline, one ComfyUI job, and ComfyUI batch size one.

```bash
curl -X PUT https://api.ai.b1.germering/admin/resource-policy \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"gpu_total_vram_gib":12,"gpu_usable_vram_gib":10,"gpu_reserve_vram_gib":2,"gpu_max_active_pipelines":1,"host_total_ram_gib":32,"host_reserve_ram_gib":6,"llm_default_context":8192,"llm_maximum_context":16384,"llm_default_parallel_requests":1,"comfyui_maximum_parallel_jobs":1,"comfyui_maximum_batch_size":1}'
```

## Admission Policy

The media queue, rate, and artifact-storage admission policy is exposed at:

```text
GET    /admin/admission-policy
POST   /admin/admission-policy/validate
PUT    /admin/admission-policy
DELETE /admin/admission-policy
GET    /admin/admission
```

These routes require an `admin` role for policy management. Read requires `admin:read`; validation, update, and reset require `admin:write`. `GET /admin/admission-policy` returns the effective policy, environment default, hard bounds, source (`environment` or `database`), and persisted override row when present. `POST /validate` returns `accepted=false` plus errors without storing anything. `PUT` persists the override, reloads the in-process policy, and audits the change. `DELETE` returns to the environment defaults.

`GET /admin/admission` returns the live counters and storage snapshot used before accepting new `/v1/media/jobs` and `/v1/media/uploads` requests. New media job admission is bounded by `max_queued_jobs_per_owner`, `max_active_jobs_per_owner`, `max_jobs_per_hour_per_owner`, and `max_queued_jobs_global`. Limit breaches return HTTP 429 with a structured `detail.code` such as `owner_queue_limit`, `owner_active_limit`, `owner_rate_limit`, or `global_queue_limit`. Idempotent repeats with an existing `Idempotency-Key` return the original job before admission checks.

New uploads and media jobs also check artifact headroom before accepting work. `artifact_storage_reserve_bytes` protects free space on the filesystem containing `$B1_ARTIFACT_ROOT`, and optional `artifact_storage_max_bytes` caps bytes under the artifact root. Storage breaches return HTTP 507 with `detail.code` of `artifact_storage_reserve` or `artifact_storage_limit`.

```bash
curl -X PUT https://api.ai.b1.germering/admin/admission-policy \
  -H "Authorization: Bearer $B1_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"max_queued_jobs_per_owner":20,"max_active_jobs_per_owner":3,"max_jobs_per_hour_per_owner":60,"max_queued_jobs_global":100,"artifact_storage_max_bytes":0,"artifact_storage_reserve_bytes":10737418240}'
```

## Authentication

Browser clients use `/auth/status`, `/auth/setup`, `/auth/login`, and `/auth/logout`. On first boot, `/auth/status` reports `setup_required=true`; create the initial administrator through Control Center using the generated `/srv/b1-ai-hub/secrets/admin_bootstrap_key`. The setup and login endpoints return an HttpOnly browser session cookie plus a CSRF token. Browser mutations using the session cookie must send `X-B1-CSRF`; the Control Center and Media Studio do this automatically.

Control-plane administrative, unified inference, job, reservation, and Model Hub routes also accept bearer tokens. Bootstrap still creates `/srv/b1-ai-hub/secrets/admin_bootstrap_key`; use that key only for first setup or emergency API-client creation, then rotate to scoped clients.

Open WebUI uses a generated internal service key from `/srv/b1-ai-hub/secrets/open_webui_api_key`. The B1 wrapper image mounts that file at `/run/secrets/open_webui_api_key`, exports it as `OPENAI_API_KEY` at container startup, and then starts Open WebUI. On startup, the control plane upserts `client_open_webui_internal` with the key prefix and only `models:read`, `inference:write`, `jobs:read`, `jobs:write`, and `workflows:read`. The raw key is not stored in PostgreSQL.

API clients are created with:

```bash
curl -s https://api.ai.b1.germering/admin/api-clients \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"external-comfy","role":"service","scopes":["jobs:read","jobs:write","models:read","modelhub:read","modelhub:sync","inference:write"],"cidr_allowlist":["192.168.2.0/24"]}'
```

Credential-management routes for `/admin/api-clients*` and `/modelhub/v1/clients*` require the `admin` role in addition to `admin:read` or `admin:write`. Operators can inspect external access status through Control Center where exposed, but creating, listing, changing, or revoking API keys and Model Hub sync clients is administrator-only. The full API key is returned once. The control plane stores only a salted PBKDF2 hash plus the public key prefix. Optional `cidr_allowlist` entries are canonicalized and enforced on bearer-token authentication using the trusted-proxy client-IP parser; leave the list empty only for keys that are intentionally unrestricted by source network. `PUT /admin/api-clients/{client_id}/cidr-allowlist` updates only that allowlist and refuses revoked clients. Local browser passwords are stored with scrypt hashes; browser session tokens are stored as server-side hashes, not plaintext.

Initial scope use:

- `admin:read` / `admin:write` for Control Center administration.
- `models:read` for `/v1/models`.
- `inference:write` for OpenAI-compatible chat, responses, embeddings, audio, and image endpoints.
- `jobs:read` / `jobs:write` for asynchronous media jobs and job events/artifacts.
- `runtimes:read` / `runtimes:write` for runtime reservations and scheduler lease diagnostics.
- `modelhub:read` / `modelhub:sync` for Model Hub catalog and blob/sync APIs.
- `workflows:read` / `workflows:write` for published workflow discovery, validation, publication, and unpublication.

## Idempotency

Long-running media operations are represented as durable jobs in PostgreSQL. `POST /v1/media/jobs`, `POST /v1/images/generations`, `POST /v1/images/edits`, and native ComfyUI `POST /prompt` support `Idempotency-Key`; repeated requests from the same API client with the same key return the original job or native prompt identity instead of creating a duplicate. Replays are looked up before admission, maintenance, workflow, or mutable alias checks, so a client can safely retry a submitted request even after catalog or workflow state changes. Keys are opaque visible header strings up to 256 characters. Reusing a key for a different job identity returns HTTP 409 with `detail.code=idempotency_key_conflict` and a list of mismatched public fields, never the original prompt, media, or other sensitive request values. Multipart image-edit replays short-circuit before upload staging when the existing key already belongs to an image-edit job. Native ComfyUI prompt replays compare only the stored native body SHA-256 and graph summary; once the original submission has a `native_prompt_id`, the replay returns a native-shaped JSON response with that prompt ID and `X-B1-Idempotent-Replay: true`, while a replay that arrives before ComfyUI acceptance returns HTTP 409 with `Retry-After: 1`.

Job records include `started_at`, `completed_at`, `load_time_ms`, `run_time_ms`, `peak_vram_mib`, and `peak_ram_mib`. Timing values are recorded by the CPU/GPU runners and native ComfyUI compatibility tracker; resource peaks are filled only when runtime-agent metrics are available, so they can be `null` on CPU-only development hosts or when NVIDIA telemetry is unavailable. PostgreSQL stores raw `request_params` for scheduler execution and idempotency matching, but public/admin job responses and SSE events omit `request_params` and `idempotency_key`; they expose only `redacted_request` so prompts, media inputs, voice details, and uploaded document references are not replayed into browsers, operator views, logs, or event streams.

OpenAI-compatible image endpoints accept B1 scheduler extensions in the request body. `runtime_policy` is used for alias resolution and persisted on the durable job envelope, while `priority` sets the initial scheduler priority before operators make later queue adjustments. These fields are removed before LocalAI-compatible runtime submission, so they do not leak into backend image-generation or image-edit calls.

The CPU runner handles jobs resolved to `audio-cpu` without taking the GPU lease. It claims queued CPU jobs, records `started_at` and `run_time_ms`, marks interrupted active CPU jobs `recovery_required` on restart, and only requeues pre-runtime `waiting_for_gpu` claims through the shared reconciliation path. For `tts/speech` or `stt/transcription` jobs it submits JSON to the internal `audio-cpu` runtime. Cancellation is checked before and during blocking audio-cpu speech/transcription calls; when cancellation arrives mid-call, the runner cancels the pending client request and marks the durable job `cancelled`. TTS responses are stored under `$B1_ARTIFACT_ROOT/audio-cpu/...` as audio artifacts; transcription responses are stored as JSON transcript artifacts and include the returned `text` in artifact metadata. STT jobs may pass audio inline as base64 or as a staged upload reference. The development scaffold engine marks successful responses with `b1_placeholder=true` or `X-B1-Placeholder: true`; when `B1_CPU_AUDIO_ENABLE_PLACEHOLDER=false`, scaffold speech, transcription, and embedding calls fail with HTTP 503 and `code=b1_audio_cpu_engine_unavailable` until real engines are configured. Setting `B1_CPU_AUDIO_ENGINE=piper` enables real CPU speech when the default checksum-pinned Piper binary at `/opt/piper/piper` is present and either the request's immutable `b1_resolved_model_version` maps to a read-only runtime view with exactly one ONNX file plus a matching `.onnx.json` voice config or the optional `B1_PIPER_MODEL_PATH` fallback resolves under the model root. In that mode speech responses carry `X-B1-Placeholder: false`. Setting `B1_CPU_EMBEDDING_ENGINE=onnx` enables real CPU embeddings when the request's immutable `b1_resolved_model_version` maps to a read-only runtime view containing exactly one ONNX graph and one `tokenizer.json`; the endpoint tokenizes locally, runs ONNX Runtime on CPU, mean-pools and normalizes the output, and returns `b1_embedding_engine=onnxruntime` with `b1_placeholder=false`. Setting `B1_CPU_STT_ENGINE=vosk` enables real CPU transcription when the request's immutable `b1_resolved_model_version` maps to a read-only Vosk runtime view; the runtime accepts bounded mono 16-bit PCM WAV and returns `b1_stt_engine=vosk` with `b1_placeholder=false`. CPU model smoke calls `/b1/runtime/smoke`, returns zero VRAM use, and reports `status=unconfigured` when the requested modality has no configured engine. Unknown CPU job shapes fail with `failure_category=unsupported_audio_cpu_operation` and no artifact.

The GPU job runner is enabled by `B1_GPU_JOB_RUNNER_ENABLED=true`. It claims `localai`, `comfyui`, and `voicebox` media jobs, leaves them in `waiting_for_gpu` when another scheduler owner holds the lease, then acquires the global GPU scheduler-owner lease and records the full GPU state path. During `unloading`, it calls the other GPU runtimes' `/b1/runtime/unload` hooks first with the recorded active model state, then falls back to runtime-agent predefined `unload` actions only when the graceful hook is unavailable or unconfirmed. During `loading` and `warming`, it calls optional selected-runtime hooks at `/b1/runtime/load` and `/b1/runtime/warm` with the job ID, selected runtime, resolved model ID, public alias, immutable model version, modality, and operation; that admission phase is recorded as `load_time_ms`. Missing or unreachable hooks are treated as unsupported; hook HTTP errors and explicit failure-like hook statuses fail the job before inference with `failure_category=runtime_prepare_failed`. Scheduler-observed runtime states are stored in `b1_runtime_state` and returned by `GET /admin/status` and `GET /admin/runtimes`. During `verifying_vram` and after runtime execution, it reads runtime-agent GPU/RAM metrics when available and stores peak observed values on the job. If measured VRAM use exceeds the configured reserve, it calls the runtime-agent predefined `recover` action for the selected runtime and verifies again. Cancellation is checked between scheduler stages and during blocking LocalAI media, Voicebox speech, and ComfyUI prompt-submission calls; when a cancellation arrives mid-call, the runner cancels its client request, marks the job `cancelled`, records runtime cancellation state, and asks runtime-agent to recover the selected runtime before releasing the GPU lease. The job fails safely if VRAM remains above reserve, records `run_time_ms` when it reached the running phase, and the runner releases the scheduler owner after each processed job.

Database job claiming uses the same priority/aging policy as the scheduler module. Valid priority classes are `chat`, `interactive_audio`, `single_image`, `image_batch`, `video`, and `batch`; unknown priorities rank as `batch`. Reservation filtering runs before priority scoring.

`GET /v1/media/jobs` is owner-scoped for ordinary users and service clients and supports `limit`, `state`, `runtime`, and `modality` filters. A non-admin caller sees only jobs whose `owner_id` matches the authenticated subject; direct job reads, cancellation, SSE events, and artifact metadata use the same owner check. Wildcard admin credentials can read all jobs through the public route, but Control Center uses the administrative queue API.

`GET /v1/media/jobs/{job_id}/events` streams durable job snapshots as Server-Sent Events with stable event IDs derived from the job ID and `updated_at` timestamp plus a reconnect retry hint. The stream is long-lived and has no control-plane timeout for active work, so slow image/video jobs remain observable until the client disconnects or the job reaches a terminal state. The stream closes after `completed`, `cancelled`, `failed`, `expired`, or `recovery_required`; recovery-required jobs are terminal for event delivery and cancellation, but remain retryable through the admin job API.

`GET /admin/jobs` is available only to `admin` and `operator` roles with `jobs:read`. It supports `limit`, `state`, `runtime`, `modality`, and `owner_id` filters. `GET /admin/jobs/{job_id}/events` exposes the same durable, long-lived SSE snapshots as the public media job stream, but requires queue-admin role instead of owner matching so operators can observe any queued or running job from Control Center. `POST /admin/jobs/{job_id}/priority` requires `jobs:write`, accepts one of the scheduler priority classes, and only updates jobs still in `created`, `validated`, `queued`, or `waiting_for_gpu`. `POST /admin/jobs/{job_id}/cancel` marks queued/pre-run jobs `cancelled` immediately and marks active jobs `cancelling` for the runner/runtime adapter to interrupt. `POST /admin/jobs/{job_id}/retry` requeues `failed`, `cancelled`, `expired`, or `recovery_required` jobs, increments `retry_count`, clears prior failure fields, clears old prompt/artifact/runtime measurements, and preserves the original immutable runtime/model resolution. These admin mutations write audit events with public job identifiers and redacted metadata only.

Workflow-backed media jobs may target ComfyUI, non-Comfy runtimes, or either according to their published `backend_policy`. The job input may provide `input.comfyui_payload`, `input.comfyui_prompt`, `input.native_prompt`, `input.workflow_json`, or a `workflow_id`/`workflow_version` pair referencing a published workflow with non-empty `workflow_json`. Published workflow jobs are first validated against workflow visibility, dependency readiness, modality, operation, alias, runtime policy, schema, and resource limits; after alias resolution, the selected runtime must also satisfy the workflow `backend_policy` of `comfyui-only`, `non-comfy-only`, or `either`. Published workflows can define `comfyui_parameter_mappings`, where each entry writes a validated `input.parameters` value to a JSON path in the native graph before submission. `input.parameters` can also fill `{{parameter}}`, `{{parameters.name}}`, `$parameters.name`, or `$input.name` placeholders in that graph. ComfyUI-backed executions record `native_prompt_id`, poll `/history/{prompt_id}`, ingest returned outputs from `/view` into artifact storage, and complete the durable job with authenticated artifact URLs. If native history contains no image, video, GIF, or audio outputs, the job enters `recovery_required` with `failure_category=comfyui_no_media_artifacts` instead of completing without downloadable media.

`POST /v1/media/uploads` stores one bounded image, audio, or video input under `$B1_ARTIFACT_ROOT/inputs/...` and returns a staged upload reference. The same staging helper is used by raw or multipart `POST /v1/images/edits`, so uploaded image and mask bytes are preserved in the durable job input instead of being logged or discarded. The default limit is `B1_UPLOAD_MAX_BYTES=268435456`.

Image generation jobs resolved to `localai` submit an OpenAI-compatible JSON request to `/v1/images/generations` using the resolved immutable model ID. Image edit jobs resolved to `localai` submit staged `image` and optional `mask` inputs as multipart files to `/v1/images/edits`; JSON data URLs are also accepted for edit inputs. Workflow-backed LocalAI edit jobs can use `runtime_parameter_mappings` to translate form fields such as `source_image` and `mask_image` into adapter fields such as `image` and `mask` before multipart submission. Text-to-video jobs submit JSON to `/v1/videos/generations`; image-to-video jobs submit staged or data-URL `image` input as multipart files to `/v1/videos/image-to-video`, with workflow-backed `source_image` fields mapped explicitly through `runtime_parameter_mappings`. Returned `b64_json`, `b64`, `base64`, `data:` URL, and same-origin LocalAI runtime URL media are stored under `$B1_ARTIFACT_ROOT/localai/...` and attached to the durable job with authenticated `/artifacts/...` URLs. Video outputs default to `video/mp4` when the runtime does not provide a more specific MIME type. If LocalAI returns no downloadable media, the job enters `recovery_required` with `failure_category=localai_no_media_artifacts`. GPU jobs whose selected runtime does not implement the requested modality/operation fail with `failure_category=unsupported_gpu_operation` and no artifact; that includes non-speech Voicebox media jobs until their real media adapters are implemented.

TTS jobs resolved to `voicebox` submit JSON to the internal Voicebox `/v1/audio/speech` endpoint after the GPU lease is held. Returned audio bytes are stored under `$B1_ARTIFACT_ROOT/voicebox/...` with SHA-256 and byte metadata. Empty Voicebox responses move the job to `recovery_required` with `failure_category=voicebox_empty_speech`.

Synchronous `POST /v1/audio/speech` resolves the public alias before forwarding. `audio-cpu` aliases proxy to the internal CPU runtime without a GPU lease and include both the resolved model ID and `b1_resolved_model_version` so Piper can select an installed runtime-view voice. If the default scaffold CPU engine is disabled by policy or the selected Piper model/binary is unavailable, the proxied call returns HTTP 503 with `b1_audio_cpu_engine_unavailable` rather than generating placeholder audio. GPU-backed Voicebox and internal OpenAI-compatible runtimes acquire the global scheduler lease when required, run the shared runtime preparation path, receive the resolved immutable model ID instead of the public alias, and receive only the control plane's authoritative `b1_resolved_model_version` marker. External OpenAI-compatible speech runtimes use the configured adapter URL and encrypted-secret bearer token, and receive neither caller-supplied nor control-plane `b1_*` fields. If runtime load/warm preparation explicitly fails, the endpoint returns HTTP 503 with `detail.type=runtime_prepare_failed` and does not forward the request. The request body must be JSON.

Synchronous `POST /v1/audio/transcriptions` resolves `model` from the JSON or multipart body, falling back to `X-B1-Model` and then `stt-default`. JSON requests may send `audio` as base64. Multipart requests may send one uploaded `file`; the control plane bounds the upload, base64-encodes it, preserves `audio_mime_type` and `filename`, rewrites the public alias to the resolved model ID, strips caller-supplied `b1_*` fields, adds only its authoritative `b1_resolved_model_version`, and forwards JSON to `audio-cpu`. Raw audio request bodies are also normalized to base64 with the request content type. The first real STT runtime is Vosk, and the internal runtime currently accepts mono signed 16-bit PCM WAV only. Unsupported media is rejected with capability or media errors rather than being sent to a cloud service.

Native ComfyUI `POST /prompt` requests create durable `workflow/comfyui-prompt` jobs as well. The proxy waits for the global GPU lease, asks runtime-agent to unload other GPU runtimes, verifies the configured VRAM reserve, calls ComfyUI's load/warm hooks when present, and only then forwards the unchanged body. The production ComfyUI image provides those hooks through the B1 `b1_runtime_hooks` custom-node package; non-production images may still report unsupported hooks. Explicit hook failures return HTTP 503 with `detail.type=runtime_prepare_failed`, and the durable job records `failure_category=runtime_prepare_failed` without submitting a native prompt. It records scheduler admission as `load_time_ms`, records the native ComfyUI `prompt_id` as `native_prompt_id`, and holds the lease through a background history tracker until `/history/{prompt_id}` appears or the configured completion timeout is reached. Native prompt submissions also honor `Idempotency-Key`; exact replays return the stored native prompt ID after acceptance and never submit a duplicate prompt. The tracker records `run_time_ms` on completion, cancellation, timeout, or recovery-required outcomes and marks ComfyUI idle after confirmed completion or cancellation so the idle-unload policy can later free the runtime. Native `/ws` traffic is bridged bidirectionally with text and binary messages preserved. Text events with a known `prompt_id` update durable job stage/progress, mark execution errors failed, and stream `executed` output references from internal ComfyUI `/view` into artifact storage immediately; the history tracker retries any non-stored references at completion before deciding whether artifact ingestion requires recovery.

## Job Artifacts

`GET /v1/media/jobs/{job_id}/artifacts` lists artifact metadata recorded on a durable job. Artifact URLs use `/artifacts/{artifact_path}` and are served by the control plane, not by exposing the internal artifact-server.

Artifact downloads require `jobs:read`. Ordinary callers can only download artifacts attached to their own job records; `admin` and `operator` roles can read artifacts for any recorded job they can inspect in Control Center. The internal artifact-server provides `GET`, `HEAD`, `Range`, `ETag`, `If-None-Match`, `Content-Length`, `Content-Range`, and private cache headers for generated artifacts. The control plane strips client `Authorization` before proxying and injects the generated `B1_ARTIFACT_SERVER_TOKEN_FILE` bearer token; direct internal artifact-server artifact/blob requests without that token fail closed. The storage service repeats path-control checks and refuses malformed percent escapes, invalid percent-encoded UTF-8, and symlink components before opening a file.

ComfyUI-native artifacts also use `/artifacts/...` URLs and the same owner/scope checks. The normal completion path ingests `/history/{prompt_id}` outputs from internal ComfyUI `/view` into `$B1_ARTIFACT_ROOT/comfyui/...`, records byte count and SHA-256 metadata, and serves the stored file through the internal artifact-server. Metadata-only `source=comfyui_view` artifacts are still accepted as a fallback for in-flight or recovery-required jobs.

## Catalog-Backed Aliases

`GET /v1/models` and Model Hub catalog routes are loaded from `model-catalog/seed/aliases.json` plus `*.manifest.json` files under `model-catalog/seed`. `/v1/models` applies the persisted alias policy table before returning results, so disabled aliases and aliases not visible to the caller's role are omitted.

Seeded aliases such as `chat-default`, `vision-default`, and `image-default` may exist before weights are installed. Inference and asynchronous media submission return a capability/dependency error when an alias is unknown, disabled by administrator policy, hidden from the caller role, has the wrong modality, violates `runtime_policy`, requests an operation not declared by the installed model manifest or selected runtime adapter, exceeds the resource policy, or is not backed by an installed manifest. Manifest load/install validates operations against the manifest modality and stores known synonyms under canonical operation names. Runtime checks normalize the documented endpoint names to the same model-operation taxonomy: for example `responses` is accepted for a `chat` model, `embeddings` for `embedding`, `speech` for `text-to-speech`, `audio-transcriptions` for `transcription`, and `image-generation` for `text-to-image`. The initial CPU placeholders for `embedding-default`, `tts-fast`, and `stt-default` are explicitly marked as CPU-resident candidates.

## Model Lifecycle

Administrative model management is exposed through:

```bash
curl -s https://api.ai.b1.germering/admin/models \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY"

curl -s https://api.ai.b1.germering/admin/models/install-plan \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-fast"}'
```

All `/admin/models*` routes require an `admin` or `operator` role in addition to the listed `models:*` or `storage:*` scope. Service, creator, and user API keys that hold `models:read` must use `/v1/models` for inference-facing alias discovery or the Model Hub routes for permitted external synchronization; they cannot inspect or mutate Control Center model-management state.

`POST /admin/models/install-plan` accepts exactly one of `{"model":"alias-or-model-id"}` for a manifest already known to the catalog, `{"manifest":{...}}` for an uploaded manifest object, or `{"manifest_url":"https://..."}` for a remote manifest JSON document. Remote manifest URLs must be public HTTPS URLs, must not contain credentials, fragments, or credential-like query parameters, are fetched with bounded redirects on the same host, and are capped at 2 MiB before JSON parsing and normal manifest validation. If a manifest declares `permissions.installable_by`, non-admin callers must hold one of those roles before install planning, source download staging, or final install can proceed. The plan reports source URL policy, licence acceptance requirement, file verification status under `$B1_DATA_ROOT/models/blobs/{sha256}`, total size, and the RTX 3060/32 GB resource decision.

`POST /admin/models/download-plan` uses the same model, uploaded-manifest, or remote-manifest-url payload and reports whether the current worker can stage the manifest's blobs. The worker supports HTTPS `direct-url` manifests and `huggingface` repository manifests. Direct-url manifests reject embedded username/password values and common credential query parameters such as `token`, `api_key`, or `signature`. Single-file direct-url manifests use `source.url` as the exact file URL. Multi-file direct-url manifests use `source.url` as a base URL ending in `/`; each safe manifest `files[].path` is appended and query strings/fragments on the base URL are rejected. Hugging Face manifests require `source.url` under `https://huggingface.co/...`, a simple branch/tag/commit `source.revision`, manifest file paths, and per-file SHA-256 values; the control plane derives `/resolve/{revision}/{path}` URLs instead of trusting arbitrary per-file URLs.

The download plan response includes aggregate `target_size_bytes`, aggregate `existing_partial_bytes`, `file_count`, `requires_license_acceptance`, `license_accepted`, and `files[]` entries with per-file source URL, target blob path, partial path, verification status, and blockers.

`POST /admin/models/downloads` requires `models:write` and `{"confirm":true}`. If the manifest declares `license.acceptance_required=true`, the same request must include `accept_license=true`; otherwise the queue request is rejected before any blob staging starts. Optional `credential_secret_name` must reference an active encrypted secret in category `model-download`. The queued record stores only the secret name and an `authenticated=true` marker in public responses. The background runner decrypts the token only when claiming the work, sends it as `Authorization: Bearer ...` only to the original source host, preserves `Range` when resuming, and never stores or logs the plaintext token. Direct-url redirects are bounded and must stay on the original host. Hugging Face redirects are bounded and allowed only to Hugging Face or known HF/CDN hosts; signed redirect URLs are not persisted. The runner writes each file to `$B1_DATA_ROOT/models/blobs/.partial/{sha256}.partial`, updates aggregate byte progress, verifies final size and SHA-256 per file, and atomically publishes blobs to `$B1_DATA_ROOT/models/blobs/{sha256}`. `GET /admin/models/downloads` lists recent records, `GET /admin/models/downloads/{download_id}` reads one record, and `DELETE /admin/models/downloads/{download_id}` cancels queued or paused work immediately or asks a running worker to stop at the next chunk boundary. `POST /admin/models/downloads/{download_id}/pause` pauses queued work immediately or marks running work `pausing` until the worker reaches the next chunk boundary; `POST /admin/models/downloads/{download_id}/resume` requeues a paused download without touching partial files. `POST /admin/models/downloads/{download_id}/retry` requeues only `failed` or `cancelled` downloads, preserves staged partial files and byte counters, clears the old error/cancellation fields, and lets the same worker resume and re-verify the manifest blobs.

`POST /admin/models/install` uses the same payload plus `confirm=true` and optional `smoke_test=true`. If the manifest requires licence acceptance, `accept_license=true` is also required. The current implementation publishes only manifests whose required blobs are already present, size-matched, and SHA-256 verified. It creates runtime views under `$B1_DATA_ROOT/models/runtime-views/{runtime}/{model_id}/{version}`, hardlinks ordinary blobs, safely extracts supported archive-format blobs, stores the installed record in PostgreSQL, refreshes the catalog overlay, and refreshes workflow dependency readiness. When `smoke_test=true`, it immediately runs the installed model through the smoke-test endpoint path and returns that result.

`POST /admin/models/{id}/versions/{version}/smoke-test` requires `models:write` and accepts optional `{"persist":true}`. GPU models acquire the global scheduler lease, unload other GPU runtimes, verify VRAM, call the selected runtime's optional load/warm/smoke hooks, record peak runtime-agent RAM/VRAM where available, and mark the runtime idle after the smoke attempt. CPU `audio-cpu` models call the runtime smoke hook without a GPU lease and report `peak_vram_mib=0`. A successful runtime `status=ok` appends a bounded `measurements` section to the stored manifest and updates `resource_estimate` conservatively from measured peaks, which can make future catalog admission stricter. Unsupported, unconfigured, or failed smoke hooks return their status and write an audit event but do not replace the stored estimate.

`PUT /admin/models/aliases/{alias}/policy` requires `models:write` and accepts `enabled`, optional `preferred_runtime`, optional `idle_timeout_seconds` from 30 to 86400, optional `visibility_roles`, and `notes`. A preferred runtime override must name a known adapter and, when an installed manifest exists, must be listed by that manifest. Setting `enabled=false` refuses active jobs for that alias. A blank `idle_timeout_seconds` uses `B1_GPU_DEFAULT_IDLE_TIMEOUT_SECONDS`, which defaults to 300 seconds. The response returns the persisted policy plus the refreshed alias projection. `DELETE /admin/models/aliases/{alias}/policy` requires `models:write`, removes the override row, refreshes catalog/workflow dependency status, audits the reset, and returns the seed/default alias projection.

`DELETE /admin/models/{id}/versions/{version}` requires `models:write` and a JSON body `{"confirm":true}`. It refuses removal while active jobs or active Voicebox voice profiles reference the immutable model or one of its aliases, reports dependent workflows and voice profiles, moves runtime views to `$B1_DATA_ROOT/models/quarantine/runtime-views`, and marks the model record `quarantined`. It does not move authoritative blobs automatically.

After a model record is quarantined, `GET /admin/models/{id}/versions/{version}/blob-quarantine-plan` requires `models:read` and reports which content-addressed blobs can be moved out of the authoritative library. The plan refuses installed records, active job references, active Voicebox voice-profile references, blobs shared by any other model record, symlinked paths, missing files, size mismatches, SHA-256 mismatches, and pre-existing quarantine destinations.

`POST /admin/models/{id}/versions/{version}/blobs/quarantine` requires `models:write` and `{"confirm":true}`. It re-runs the same checks immediately before moving eligible blobs to `$B1_DATA_ROOT/models/quarantine/blobs/{model_id}/{version-timestamp}/{sha256}` and writes an audit event. This is recoverable storage staging, not permanent deletion.

## Runtime Resolution

The control plane has a versioned runtime-adapter registry for `localai`, `comfyui`, `voicebox`, `audio-cpu`, `openai-compatible`, and `generic-http`. `GET /admin/runtimes` returns current health plus a public adapter record with `capabilities` and `adapter_contract.version=b1-runtime-adapter/v1alpha1`. The public capabilities include both modalities and supported operations, and resolution requires the requested operation to match both the installed model manifest and the selected adapter. The contract names the scheduler surface, submission/event surface, lifecycle hook surface, runtime-agent unload/recovery control, metrics source, and method-level implementation status for capability discovery, model listing, health, validation, load/warm, submit/stream, progress/events, cancellation, unload, work discovery, metrics, failure classification, and recovery. If an adapter returns JSON with a body-level `status`, the control plane surfaces that status; for example, a policy-disabled scaffold `audio-cpu` runtime reports `unconfigured` with `details.capabilities` set to `false` for speech, transcription, and embeddings.

External adapters are disabled unless `B1_ALLOW_EXTERNAL_PROVIDERS=true` and remain non-selectable unless their configured base URL passes the external-runtime URL policy. The policy requires HTTPS, rejects embedded credentials, query strings, fragments, relative path segments, malformed ports, malformed percent escapes, invalid percent-encoded UTF-8, private/loopback/link-local/reserved IP literals, hostnames that resolve to non-public IP addresses, and hostnames that cannot be resolved safely. `B1_OPENAI_COMPATIBLE_BASE_URL` may point at either an API root or a `/v1` base; OpenAI-style forwarding avoids duplicating `/v1`. Public adapter records include `configured` and `configuration_error` but never expose API keys.

`GET /admin/runtimes/external-config` requires `runtimes:read` and administrator role. It returns database-managed or environment-fallback configuration records for `openai-compatible` and `generic-http`, the global `allow_external_providers` switch, status, validation errors, secret-name metadata, and the external-data warning. `PUT /admin/runtimes/external-config/{runtime}` requires `runtimes:write` and administrator role. The request body is `{"enabled":true,"base_url":"https://api.example.com/v1","api_key_secret_name":"remote:openai","confirm_external_data":true,"notes":"..."}`. `api_key_secret_name` is optional, but when present it must name an active encrypted secret in category `remote-provider`. Saving rebuilds the in-memory runtime registry and writes an audit event without logging the secret value.

When a request uses a public alias, the resolver checks administrator alias policy, caller role visibility, modality, resource admission, installed manifest, runtime policy, and external-provider policy before selecting a runtime. A persisted preferred-runtime override takes precedence over the manifest default only when that runtime is compatible with the installed manifest; stale incompatible overrides are shown in admin metadata but ignored during resolution. Durable media jobs store the selected runtime and immutable `model_id@version` in PostgreSQL. `runtime_policy: non_comfy_only` excludes the server-side ComfyUI adapter and fails clearly when no non-Comfy runtime is compatible.

For installed aliases resolved to an OpenAI-compatible runtime such as `localai` or the lightweight `audio-cpu` embedding path, `/v1/chat/completions`, `/v1/responses`, and `/v1/embeddings` forward to the selected internal runtime with `model` rewritten to the immutable manifest ID, `runtime_policy` stripped, and `b1_resolved_model_version` included so LAN runtimes can select the exact read-only model view. External OpenAI-compatible providers never receive that B1-internal field. Chat completions and Responses accept aliases whose modality is `llm` or `vlm`, so vision-language models can be used through the same OpenAI-style surfaces when the request payload supplies image content. Embeddings, audio, image, video, and media-job endpoints keep exact modality checks. GPU-backed synchronous calls acquire the global PostgreSQL scheduler lease, ask runtime-agent to unload other GPU runtimes, verify VRAM policy, call optional selected-runtime `/b1/runtime/load` and `/b1/runtime/warm` hooks, then release the lease after the response or stream completes. Streaming responses renew the lease periodically; the default synchronous inference lease TTL is `B1_SYNC_INFERENCE_LEASE_TTL_SECONDS=7200`. If a load or warm hook reports an explicit failure status, synchronous JSON and streaming requests return HTTP 503 with a structured detail body containing `type=runtime_prepare_failed`, `operation`, `runtime`, `resolved_model`, `public_model`, and `retryable=false`; the runtime request is not forwarded. Responses include `b1_runtime`, `b1_resolved_model`, and `b1_public_model` metadata. A non-OpenAI-compatible runtime on a synchronous OpenAI-style endpoint returns a capability error instead of a scaffold response.

After a queued GPU job or synchronous GPU-backed request finishes runtime preparation, the control plane records the selected runtime as idle with the public alias and immutable model version. The GPU job runner performs idle cleanup only when it has no claimable GPU job and can acquire the global scheduler lease. It then applies the alias idle timeout or the `B1_GPU_DEFAULT_IDLE_TIMEOUT_SECONDS` fallback, calls the runtime's `/b1/runtime/unload` hook, and falls back to runtime-agent `/v1/runtime-actions/{runtime}/unload` if the graceful hook does not confirm success. A confirmed `status=ok` clears the active model from runtime state; dry-run or unconfirmed unload attempts leave the model recorded for a later retry. The public admin unload route also clears persisted runtime state only after runtime-agent confirms `status=ok`.

## Workflows

Approved seed workflows under `workflows/approved` are imported into PostgreSQL on control-plane startup. The registry stores the immutable workflow JSON, input/output schemas, execution modality/operation/model alias, runtime policy, output MIME types, limits, visibility roles, backend policy, resource class, optional ComfyUI parameter mappings, optional runtime parameter mappings, and dependency readiness report.

Custom ComfyUI node dependencies must use `{"type":"node","id":"...","version":"<40-char commit>"}`. The control plane checks those dependencies against the merged node-pin registry: `$B1_COMFYUI_NODE_PIN_REGISTRY`, defaulting to `/opt/b1/workflows/approved-node-pins.json`, plus the PostgreSQL `b1_comfyui_node_pins` table. Database rows override matching seed rows so operators can disable or supersede seed approvals without editing repository files. A node dependency is publishable only when the exact `(id, commit)` exists with `status: approved`; missing, disabled, superseded, or unpinned nodes produce `needs_dependencies` with the repository URL and approval metadata where available.

`GET /admin/comfyui/node-pins` requires `workflows:read` and an administrator, operator, or creator role. It returns the merged seed/database registry with a `source` field for each pin. `POST /admin/comfyui/node-pins` requires administrator `workflows:write`, validates the exact node id, lowercase 40-character commit, credential-free HTTPS repository URL, optional SHA-256 dependency lock, and safe relative route prefixes, then writes the approval to PostgreSQL, refreshes workflow dependency status, and audits the mutation. An approved pin that exposes any `allowed_route_prefixes` must include `dependency_lock_sha256` so mutating custom-node APIs cannot be enabled without dependency-review evidence. `PATCH /admin/comfyui/node-pins/{node_id}/commits/{commit}` has the same administrator requirement and writes a PostgreSQL overlay for an existing seed or database pin. Logical backups include `b1_comfyui_node_pins`; restore-import preserves the approvals along with published workflows.

Read endpoints require `workflows:read` and filter records by `visibility_roles` unless the caller has administrative wildcard scope. `POST /workflows/v1/validate` and `POST /workflows/v1/published` require `workflows:write`. Publication records are allowed to persist with `status: needs_dependencies`; this keeps placeholder workflows visible while making uninstalled models, missing runtimes, or unapproved custom nodes explicit through `dependency_status`. Control Center exposes the same operations as JSON import/edit, validate, publish, dependency inspection, and unpublish actions.

The repository seeds starter placeholder workflows for text-to-image, image-to-image, inpainting/outpainting, background removal, upscaling, text-to-video, image-to-video, frame interpolation, TTS, and transcription. These seed files do not bundle weights; dependency readiness points operators to the required runtime/model aliases before execution.

When `POST /v1/media/jobs` includes `input.workflow_id` and `input.workflow_version`, the control plane treats it as a published-workflow job. It requires the workflow to be visible to the caller and dependency-ready, then verifies that the requested modality, operation, public model alias, and runtime policy match the workflow record. It also validates `input.parameters` against the workflow input schema, rejects unsupported fields when `additionalProperties=false`, enforces enum/string/number/boolean constraints, checks inline base64 media fields or staged-upload media references, and applies workflow resource limits such as `max_steps`, `max_frames`, `max_width`, `max_height`, `max_batch_size`, and `max_duration_seconds`. Adapter-specific workflow fields are translated only through explicit `runtime_parameter_mappings`; the control plane does not infer backend parameter names from UI labels.

## Model Hub Blobs

Blob downloads are authenticated through the control plane and served by the internal artifact-server. `GET` and `HEAD /modelhub/v1/blobs/{sha256}` require `modelhub:sync`, and the requested SHA-256 must belong to a catalog manifest whose licence and execution mode mark it downloadable. If every allowed catalog record for the blob sets `license.acceptance_required=true`, the request must include `X-B1-Accept-License: model_id@version`; otherwise the control plane returns HTTP 428 with the required model refs. Each authenticated subject is limited by the fixed-window `B1_MODELHUB_BLOB_REQUESTS_PER_MINUTE` policy before the request is proxied to storage with the generated internal artifact-server bearer token; exhausted windows return HTTP 429 with `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`. The internal storage service accepts only exact 64-character SHA-256 hex blob names, refuses symlinked blob paths, and re-hashes each blob before returning it.

Supported blob response behaviour:

- `ETag: "sha256:{digest}"`
- `X-Checksum-SHA256`
- `Content-Length`
- `Accept-Ranges: bytes`
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`
- single-range `Range: bytes=start-end`, `bytes=start-`, and `bytes=-suffix`
- `206 Partial Content` with `Content-Range`
- `304 Not Modified` for matching `If-None-Match`
- `416 Range Not Satisfiable` with `Content-Range: bytes */{size}`

Inference-only model manifests remain visible in the catalog but are not downloadable. The Model Hub blob endpoint rejects unknown or non-downloadable catalog blobs before proxying to storage, returns `404` for allowed-but-absent blobs, and returns `409` if an on-disk blob checksum does not match its content-address.

Model Hub catalog, model, version, and sync-plan responses redact manifest `source.url` fields before returning them to external consumers. The public value keeps only scheme, host, optional port, and path; usernames, passwords, query strings, fragments, signed URLs, and credential parameters are omitted and `url_redacted=true` is included when the URL changed. Administrators can still inspect the authoritative manifest source through the admin model-management APIs.

## Model Hub Sync Plans

`POST /modelhub/v1/sync/plan` accepts a requested model/alias list and the caller's local blob inventory:

```json
{
  "models": ["chat-default"],
  "installed_blobs": [
    { "sha256": "0123...", "size_bytes": 123456 }
  ]
}
```

The response contains deterministic `keep`, `download`, `replace`, or `skip` actions. Download actions include the blob URL, expected byte size, ETag, licence metadata, redacted source metadata, execution modes, resource estimate, and the resolved immutable model version. The planner filters candidate versions through the caller's Model Hub client allowlist and manifest role permissions before selecting a downloadable version, so a client never receives a sync action for a version it cannot download. Inference-only models return a `skip` action instead of a blob URL, with the same policy metadata so clients can explain why the model must be consumed through hosted inference.

Official `b1-model-client sync` runs refuse `keep`, `download`, or `replace` actions whose manifest sets `license.acceptance_required=true` unless the operator reruns with `--accept-license` or `B1_MODEL_CLIENT_ACCEPT_LICENSES=true` after reviewing `b1-model-client plan`. When accepted, the client sends `X-B1-Accept-License` with the resolved immutable model refs on each blob request that needs it.

## Model Hub Clients

Administrators can create dedicated Model Hub clients:

```bash
curl -s https://models.ai.b1.germering/modelhub/v1/clients \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY"

curl -s https://models.ai.b1.germering/modelhub/v1/clients \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"workstation-1","allowed_models":["chat-default"],"cidr_allowlist":["192.168.2.0/24"]}'
```

The response includes a one-time API key scoped to `modelhub:read` and `modelhub:sync`. The control plane stores the backing API key as a salted PBKDF2 hash, records the Model Hub allowlist, and revokes both records when `DELETE /modelhub/v1/clients/{id}` is called. CIDR allowlists are canonicalized when the client is created or updated through `PUT /modelhub/v1/clients/{id}/cidr-allowlist`, then enforced on the backing API key plus Model Hub catalog, model, sync-plan, and blob requests. An empty CIDR list means no network restriction for that client; use explicit CIDRs for workstation keys.

The control plane derives the effective client IP from the direct peer address unless the peer matches the active network policy's trusted proxy CIDRs. Only trusted proxy peers may supply `X-Forwarded-For` or `Forwarded` client addresses. The environment default comes from `B1_TRUSTED_PROXY_CIDRS`, and administrators can persist a live override through `/admin/network-policy` or the System tab.

## Scheduler Lease

`POST /admin/scheduler/lease` records the active GPU scheduler owner in PostgreSQL with an epoch and expiry time. At runtime the control plane also coordinates an expiring Redis owner key. A GPU lease is usable only when PostgreSQL grants the durable owner/epoch and Redis grants or renews the matching live owner key; if Redis is configured but unavailable or held by another owner, acquisition fails closed and the PostgreSQL claim is released.

Runtime reservations created through `/v1/runtime-reservations` are durable scheduler intent records for the GPU scheduler. Creation validates that the requested alias is installed, compatible with the requested runtime, and targets a configured GPU runtime such as LocalAI, ComfyUI, or Voicebox; CPU-only and external runtimes are rejected because they do not participate in the one-GPU lease. Creation returns HTTP 409 when another active reservation already owns the single-GPU window for a different owner/runtime/immutable model. Accepted reservations store the immutable resolved model version, expiry, owner, and reason, and support owner-scoped read/cancel operations. Active reservations gate actual GPU execution: queued GPU jobs must match an active reservation's owner/runtime/immutable model when any reservation is active, and synchronous GPU inference requests are rejected before lease acquisition when they do not match the active reservation set. Expired reservations are marked `expired` during scheduler checks.

`GET /admin/runtime-reservations` requires `runtimes:read` plus administrator or operator role. It returns a bounded, filterable fleet view for Control Center with optional `status`, `runtime`, and `owner_id` query parameters. The Jobs tab also reads `GET /admin/scheduler/lease` so operators can compare queued work, reservations, and the current GPU scheduler owner from one screen.

## OpenAPI

The committed OpenAPI 3.1 contract is [openapi.json](./openapi.json). It is generated from the FastAPI application, not hand-edited:

```bash
make openapi
make openapi-check
```

`make openapi-check` compares the committed file against a fresh generated schema and is run by CI. Install `services/control-plane/requirements.txt` before running it outside the Compose development environment. The deliberate future-route compatibility catch-all is excluded from the OpenAPI document; stable public/admin routes and explicit compatibility routes such as `/prompt` are included.
