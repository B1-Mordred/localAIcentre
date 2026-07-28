# Integration Tests

Integration tests are opt-in live checks against a running B1 AI Hub deployment. They are skipped by default.

Run live integration checks with an administrator/operator key that has `runtimes:read`:

```bash
export B1_INTEGRATION_LIVE_TEST=1
export B1_INTEGRATION_API_BASE=https://api.ai.b1.germering
export B1_INTEGRATION_API_KEY=...
python3 -m unittest discover -s tests/integration -v
```

Or through Make:

```bash
B1_INTEGRATION_LIVE_TEST=1 B1_INTEGRATION_API_KEY=... make integration
```

The first live integration path checks `/admin/runtimes` adapter-contract, health, and production-readiness shape. It verifies that required runtime adapters remain visible without assuming a particular installed model inventory.

## Installed Workflow Acceptance

Run the installed workflow suite on the target host after the public aliases are backed by real installed models, each alias has a successful persisted model-smoke measurement with `ok` runtime-hook proof for the same alias/runtime/immutable model version, and the required workflows are approved. It exercises the user-facing paths for chat, synchronous TTS, synchronous STT, CPU-audio GPU-lease isolation, image generation, image edit, and short video, rejects placeholder or unmarked `audio-cpu` output by default, follows the accepted media job's advertised polling/artifact links, downloads generated artifacts, and verifies artifact metadata, `Content-Length`, `ETag`, byte count, and SHA-256 before writing evidence for the Control Center handoff report. The API key must include normal inference scopes plus `models:read` and `runtimes:read` so the harness can verify installed model measurements and read `/admin/scheduler/lease` before and after the CPU-audio probe.

Compose bootstrap copies editable job templates once to `/srv/b1-ai-hub/workflows/acceptance/`. It never overwrites existing files, so review and edit those external data-root copies to match installed aliases, checkpoint filenames, uploaded edit inputs, and published workflow IDs before collecting production evidence.

```bash
export B1_WORKFLOWS_API_BASE=https://api.ai.b1.germering
export B1_WORKFLOWS_API_KEY=...
export B1_WORKFLOWS_CA_FILE=/srv/b1-ai-hub/data/control-plane/caddy-root.crt
export B1_WORKFLOWS_IMAGE_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/image-generation-job.json
export B1_WORKFLOWS_IMAGE_EDIT_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/image-edit-job.json
export B1_WORKFLOWS_VIDEO_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/short-video-job.json
export B1_WORKFLOWS_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/installed-workflows.json
make installed-workflows-acceptance
```

The image, edit, and video job files should be full `POST /v1/media/jobs` JSON bodies that reference installed aliases and either published workflow IDs or native ComfyUI/LocalAI inputs validated for the RTX 3060 profile. The harness provides simple prompt defaults only to keep dry runs ergonomic; production acceptance should use explicit job files so the operator can review exact model/workflow dependencies. Set `B1_WORKFLOWS_CPU_TTS_MODEL` and `B1_WORKFLOWS_CPU_STT_MODEL` when CPU lease validation should use aliases different from the ordinary `tts-fast` and `stt-default` defaults. Set `B1_WORKFLOWS_ALLOW_PLACEHOLDER=1` only for a labelled development dry run; the harness continues collecting samples but writes incomplete evidence so placeholder TTS/STT output cannot satisfy handoff.

## LocalAI Runtime Acceptance

Run the LocalAI runtime suite on the target host after a real `chat-default` or configured `B1_LOCALAI_ACCEPTANCE_CHAT_MODEL` alias is installed and the production LocalAI override is enabled. It sends a streamed OpenAI-compatible chat request through `api.ai.b1.germering`, verifies scheduler runtime state reports only LocalAI as GPU-resident, then calls the operator unload route and verifies the persisted LocalAI state is cleared.

```bash
export B1_LOCALAI_ACCEPTANCE_API_BASE=https://api.ai.b1.germering
export B1_LOCALAI_ACCEPTANCE_API_KEY=...
export B1_LOCALAI_ACCEPTANCE_CA_FILE=/srv/b1-ai-hub/data/control-plane/caddy-root.crt
export B1_LOCALAI_ACCEPTANCE_CHAT_MODEL=chat-default
export B1_LOCALAI_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/localai-runtime.json
make localai-acceptance
```

The API key must include chat/inference access plus `models:read`, `runtimes:read`, and `runtimes:write`. Before sending chat, the harness verifies that the selected chat alias is installed and has a successful persisted model-smoke measurement for the immutable model version, including an `ok` runtime hook correlated to the same alias/runtime/model version. The unload check is intentional: it exercises the same guarded admin route operators use to clear LocalAI residency. Keep `B1_LOCALAI_ACCEPTANCE_REQUIRE_PRODUCTION=true` for handoff evidence; disable it only for a labelled temporary-hostname dry run.

## Model Tool Acceptance

Run this suite after at least one non-Comfy chat alias is installed and the model-tool policy allows the built-in web tools:

```bash
export B1_MODEL_TOOLS_API_BASE=https://api.ai.b1.germering
export B1_MODEL_TOOLS_API_KEY=...
export B1_MODEL_TOOLS_CA_FILE=/srv/b1-ai-hub/data/control-plane/caddy-root.crt
export B1_MODEL_TOOLS_CHAT_MODEL=chat-default
export B1_MODEL_TOOLS_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/model-tools.json
make model-tools-acceptance
```

The suite verifies `/v1/tools`, proves live chat use of `web_fetch` and `web_search`, then temporarily creates a unique admin-managed `http-json` tool, executes it manually, uses it through `/v1/chat/completions`, restores the original policy, and deletes the temporary tool. Evidence uses `b1-ai-hub-model-tools-acceptance/v1`, records only bounded excerpts and B1 tool response headers, and is ingested by the operator handoff report. Set `B1_MODEL_TOOLS_CUSTOM_URL` only when the default `https://postman-echo.com/post` endpoint is unsuitable for the acceptance network.

## RTX 3060 Cross-Runtime GPU Acceptance

Run the GPU acceptance suite only on the target host, or during an equivalent maintenance window with the real LocalAI, ComfyUI, and Voicebox runtime overlays enabled. It sends live inference work and handoff evidence requires a bounded predefined runtime recovery probe.

```bash
export B1_GPU_ACCEPTANCE_API_BASE=https://api.ai.b1.germering
export B1_GPU_ACCEPTANCE_API_KEY=...
export B1_GPU_ACCEPTANCE_CA_FILE=/srv/b1-ai-hub/data/control-plane/caddy-root.crt
export B1_GPU_ACCEPTANCE_CHAT_MODEL=chat-default
export B1_GPU_ACCEPTANCE_COMFY_MODEL=image-default
export B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json
export B1_GPU_ACCEPTANCE_VOICEBOX_MODEL=tts-quality
export B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1
export B1_GPU_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/cross-runtime-gpu.json
make gpu-acceptance
```

The suite verifies the persisted RTX 3060 resource policy, production runtime readiness when `B1_GPU_ACCEPTANCE_REQUIRE_PRODUCTION` is left enabled, runtime-agent GPU/NVML metrics, persisted model-smoke measurements with hook proof for the selected LocalAI, ComfyUI, and Voicebox aliases, and the live switch sequence LocalAI chat -> ComfyUI media job -> Voicebox speech. The ComfyUI leg records prompt source/name, node/class-type counts, the native prompt ID, verified B1 artifact downloads, and rejects the bundled `B1RuntimeTinyImage` route-level smoke prompt for handoff. Set `B1_GPU_ACCEPTANCE_ALLOW_COMFY_TINY_SMOKE=1` only for a labelled dry run; the evidence and final report remain incomplete with that prompt. After each step it records an `after_runtime_state` proof from `/admin/runtimes`, checks that at most one managed GPU runtime reports a resident model/pipeline, verifies the expected runtime is the only active GPU runtime, confirms physical GPU VRAM is at least the configured policy total, and samples VRAM against the configured total-minus-reserve policy. When `B1_GPU_ACCEPTANCE_EVIDENCE` is set, the output JSON includes `status`, `required_checks`, per-check records, runtime/metric samples, required model aliases, compact measurement summaries, the ordered `runtime_state_sequence`, labelled VRAM samples, and ComfyUI prompt/native-artifact proof; Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` file and block handoff if the resource-policy check, LocalAI exclusive residency, ComfyUI switch, Voicebox switch, combined LocalAI -> ComfyUI -> Voicebox switch proof, per-switch runtime-state proof, VRAM-reserve proof, bounded runtime recovery action, measured model runs, model-smoke hook proof, non-tiny ComfyUI prompt metadata, native prompt ID, or verified ComfyUI artifacts are absent or incomplete. Set `B1_GPU_ACCEPTANCE_SKIP_VOICEBOX=1` only for a documented no-Voicebox dry run; handoff evidence remains incomplete without `voicebox_switch_completed`. Set `B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1` only during a maintenance acceptance window, and leave it enabled for final handoff evidence. Final handoff requires the recovery action to return runtime-agent `status=ok`; if the deployment intentionally keeps `B1_ENABLE_MUTATIONS=false`, set `B1_GPU_ACCEPTANCE_ALLOW_RECOVERY_DRY_RUN=1` only for a rehearsal and expect the evidence status to remain incomplete.

## Restart Reconciliation Acceptance

Run the restart reconciliation suite during a maintenance validation window. By default the Makefile target self-seeds a bounded synthetic drill: it creates one interrupted `waiting_for_gpu` CPU job, one interrupted `waiting_for_gpu` GPU job, one interrupted active GPU job without a resumable native prompt, and one resumable native ComfyUI compatibility job with a persisted native `prompt_id`. It then restarts only `control-plane`, reads `GET /admin/scheduler/reconciliation`, writes evidence, cancels the synthetic drill rows, and revokes the temporary `runtimes:read` API client when one was created. The evidence proves that the restarted control plane requeued waiting jobs, marked non-resumable active jobs `recovery_required` for explicit operator retry, recorded bounded sampled durable job IDs for those interrupted rows, and reattached resumable native ComfyUI prompt trackers with sampled durable job IDs and native prompt IDs.

```bash
export B1_RESTART_RECONCILIATION_API_BASE=https://api.ai.b1.germering
export B1_RESTART_RECONCILIATION_CA_FILE=/srv/b1-ai-hub/data/control-plane/caddy-root.crt
export B1_RESTART_RECONCILIATION_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/restart-reconciliation.json
make restart-reconciliation-acceptance
```

If a scoped API key is not supplied through `B1_RESTART_RECONCILIATION_API_KEY`, `B1_SMOKE_ADMIN_API_KEY`, or `B1_AI_HUB_API_KEY`, the auto drill creates and later revokes a temporary `operator` API client scoped to `runtimes:read`. To run a manual drill instead, set `B1_RESTART_RECONCILIATION_AUTO_DRILL=0`, deliberately create the interrupted job states, capture `B1_RESTART_RECONCILIATION_STARTED_AFTER`, restart only `control-plane`, and then run the same target.

By default the harness requires at least one requeued waiting job, at least one sampled requeued job ID, at least one active job marked `recovery_required`, at least one sampled recovery-required job ID, and at least one native ComfyUI prompt tracker reattached with sampled durable job and native prompt IDs. Override `B1_RESTART_RECONCILIATION_MIN_REQUEUED`, `B1_RESTART_RECONCILIATION_MIN_MARKED_RECOVERY_REQUIRED`, or `B1_RESTART_RECONCILIATION_MIN_RESUMED_COMFYUI_NATIVE` only for a labelled development dry run; production handoff should keep all three at their default value of `1`. Control Center acceptance reports reject shallow restart evidence that only marks the required checks `ok`; handoff-ready evidence must include the healthy reconciliation API status, required CPU/GPU runner inventory with no missing required runners, control-plane and runner timestamps after the drill's captured pre-restart boundary, runner runtime coverage, observed/minimum counts, interrupted job ID samples, and reattached native ComfyUI prompt ID samples.

## Backup, Migration, and Rollback Evidence

Backup, restore, migration, and rollback proof is generated from reviewed files rather than through a live API harness. After creating and verifying a B1 backup, restoring it to an alternate directory, verifying the old-stack backup, and reviewing the Open WebUI migration plan and cutover plan, generate the rollback rehearsal report:

```bash
make rollback-rehearsal-report \
  CUTOVER_PLAN=/srv/b1-ai-hub/backups/cutover-plan.json \
  REHEARSED_BY=operator-name \
  ROLLBACK_COMMANDS_TESTED=1 \
  OLD_RESOURCES_PRESERVED=1
```

Then run:

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

The generator writes `b1-ai-hub-backup-migration-rollback-acceptance/v1` evidence under `$B1_BACKUP_ROOT/acceptance/` and Control Center blocks handoff if any required check is missing or incomplete. Handoff-ready evidence must also include detailed backup, restore, old-stack archive, Open WebUI preservation, cutover readiness, preserved-resource, and rollback checksum/action proof; a JSON file that only marks the required checks `ok` is treated as incomplete. Backup manifests, restore reports, Open WebUI migration plans, cutover plans, rollback rehearsal reports, old-stack scope templates, and final backup/migration/rollback evidence are written atomically with private `0600` permissions and symlink refusal.

Use the same TLS helper variables as smoke tests when testing through the Caddy internal CA or temporary hostnames:

- `B1_INTEGRATION_CA_FILE`
- `B1_INTEGRATION_TLS_VERIFY=0`
- `B1_INTEGRATION_HOST_HEADER`

All harnesses that use the shared live API client refuse to send bearer keys over plain HTTP unless `B1_ACCEPTANCE_ALLOW_INSECURE_HTTP=true` is set for an isolated development run. Handoff evidence should use HTTPS and either trust the Caddy internal CA or set the relevant `*_CA_FILE`.

Native ComfyUI REST/WebSocket compatibility, optional legacy ComfyUI listener checks, external ComfyUI remote-node execution, Model Hub blob semantics, and Voicebox remote/server scenarios live under `tests/compatibility/`; deployed security acceptance lives under `tests/security/`.

To avoid rebuilding this environment by hand from each section, generate the reviewed target-host acceptance env file:

```bash
make acceptance-env
export B1_ACCEPTANCE_API_KEY=...
. /srv/b1-ai-hub/backups/acceptance/operator-live-acceptance.env
make acceptance-preflight
```

The file contains default LAN URLs, including the Open WebUI chat-host smoke URL, Caddy internal CA paths, prompt/job template paths, evidence output paths, inherited production Compose/profile values, non-scaffold CPU runtime selections, and explicit blank values for the restart reconciliation timestamp, Model Hub sync aliases, backup/migration/rollback artifact paths, browser session/password, and any split-scope API keys. It contains no secrets by default and the generator refuses to overwrite an existing file unless rerun directly with `--force`. The preflight is deliberately local and non-networked: it parses the env file safely instead of executing shell, checks that final handoff values and safety gates are set, verifies Caddy CA/evidence paths, verifies reviewed backup/migration/rollback artifact paths and machine-readable formats, rejects unsafe API and Open WebUI smoke URLs, rejects development/mock runtime topology or scaffold CPU engines, rejects unedited prompt/job JSON before the live suite starts, and writes `$B1_BACKUP_ROOT/acceptance/operator-preflight.json`.

When every required live-test environment variable is configured, `make operator-live-acceptance` runs repository-quality evidence first, then preflight, then backup/migration/rollback evidence generation followed by the live smoke, installed workflow, LocalAI, GPU, external compatibility, security, and restart-reconciliation harnesses with their default evidence paths under `$B1_BACKUP_ROOT/acceptance/`. Repository-quality evidence runs `make quality-container` and `make secret-scan`, records the exact source commit, and refuses dirty final handoff evidence. The preflight verifies the reviewed backup/migration/rollback artifact inputs before the final backup/migration/rollback evidence writer consumes them.

After the live group passes, `make acceptance-report-preview` checks the final Control Center handoff report without writing server-side files or audit events, and `make operator-handoff-report` creates the durable report through `POST /admin/acceptance-reports`. Both targets use environment/key-file authentication rather than command-line bearer tokens. The create target exits nonzero when the generated report is not handoff-ready, while still writing the response JSON for blocker review.

All live harness evidence writers use an atomic private JSON writer with `0640` output permissions. They refuse evidence paths that are symlinks or under symlinked directories; use normal files under the reviewed `$B1_BACKUP_ROOT/acceptance/` directory.
