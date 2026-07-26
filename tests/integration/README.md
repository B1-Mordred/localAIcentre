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

Run the installed workflow suite on the target host after the public aliases are backed by real installed models, each alias has a successful persisted model-smoke measurement, and the required workflows are approved. It exercises the user-facing paths for chat, synchronous TTS, synchronous STT, CPU-audio GPU-lease isolation, image generation, image edit, and short video, rejects placeholder or unmarked `audio-cpu` output by default, follows the accepted media job's advertised polling/artifact links, downloads generated artifacts, and verifies artifact metadata, `Content-Length`, `ETag`, byte count, and SHA-256 before writing evidence for the Control Center handoff report. The API key must include normal inference scopes plus `models:read` and `runtimes:read` so the harness can verify installed model measurements and read `/admin/scheduler/lease` before and after the CPU-audio probe.

Compose bootstrap copies editable job templates once to `/srv/b1-ai-hub/workflows/acceptance/`. It never overwrites existing files, so review and edit those external data-root copies to match installed aliases, checkpoint filenames, uploaded edit inputs, and published workflow IDs before collecting production evidence.

```bash
export B1_WORKFLOWS_API_BASE=https://api.ai.b1.germering
export B1_WORKFLOWS_API_KEY=...
export B1_WORKFLOWS_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
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
export B1_LOCALAI_ACCEPTANCE_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_LOCALAI_ACCEPTANCE_CHAT_MODEL=chat-default
export B1_LOCALAI_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/localai-runtime.json
make localai-acceptance
```

The API key must include chat/inference access plus `models:read`, `runtimes:read`, and `runtimes:write`. Before sending chat, the harness verifies that the selected chat alias is installed and has a successful persisted model-smoke measurement for the immutable model version. The unload check is intentional: it exercises the same guarded admin route operators use to clear LocalAI residency. Keep `B1_LOCALAI_ACCEPTANCE_REQUIRE_PRODUCTION=true` for handoff evidence; disable it only for a labelled temporary-hostname dry run.

## RTX 3060 Cross-Runtime GPU Acceptance

Run the GPU acceptance suite only on the target host, or during an equivalent maintenance window with the real LocalAI, ComfyUI, and Voicebox runtime overlays enabled. It sends live inference work and handoff evidence requires a bounded predefined runtime recovery probe.

```bash
export B1_GPU_ACCEPTANCE_API_BASE=https://api.ai.b1.germering
export B1_GPU_ACCEPTANCE_API_KEY=...
export B1_GPU_ACCEPTANCE_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_GPU_ACCEPTANCE_CHAT_MODEL=chat-default
export B1_GPU_ACCEPTANCE_COMFY_MODEL=image-default
export B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json
export B1_GPU_ACCEPTANCE_VOICEBOX_MODEL=tts-quality
export B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1
export B1_GPU_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/cross-runtime-gpu.json
make gpu-acceptance
```

The suite verifies the persisted RTX 3060 resource policy, production runtime readiness when `B1_GPU_ACCEPTANCE_REQUIRE_PRODUCTION` is left enabled, runtime-agent GPU/NVML metrics, persisted model-smoke measurements for the selected LocalAI, ComfyUI, and Voicebox aliases, and the live switch sequence LocalAI chat -> ComfyUI media job -> Voicebox speech. After each step it checks that at most one managed GPU runtime reports a resident model/pipeline, that physical GPU VRAM is at least the configured policy total, and that sampled VRAM stays within the configured total-minus-reserve policy. When `B1_GPU_ACCEPTANCE_EVIDENCE` is set, the output JSON includes `status`, `required_checks`, per-check records, runtime/metric samples, required model aliases, and compact measurement summaries; Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` file and block handoff if the resource-policy check, LocalAI exclusive residency, ComfyUI switch, Voicebox switch, combined LocalAI -> ComfyUI -> Voicebox switch proof, VRAM-reserve proof, bounded runtime recovery action, or measured model runs are absent or incomplete. Set `B1_GPU_ACCEPTANCE_SKIP_VOICEBOX=1` only for a documented no-Voicebox dry run; handoff evidence remains incomplete without `voicebox_switch_completed`. Set `B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1` only during a maintenance acceptance window, and leave it enabled for final handoff evidence. Final handoff requires the recovery action to return runtime-agent `status=ok`; if the deployment intentionally keeps `B1_ENABLE_MUTATIONS=false`, set `B1_GPU_ACCEPTANCE_ALLOW_RECOVERY_DRY_RUN=1` only for a rehearsal and expect the evidence status to remain incomplete.

## Restart Reconciliation Acceptance

Run the restart reconciliation suite during a maintenance validation window after deliberately creating at least one interrupted `waiting_for_gpu` job, one interrupted active preparation/execution job without a resumable native prompt, and one native ComfyUI compatibility `POST /prompt` job that has already persisted its native `prompt_id`. The recommended drill is to enable maintenance mode immediately after those states are present, capture the timestamp, restart only `control-plane`, and then run the harness before clearing or retrying the drill jobs. The harness reads `GET /admin/scheduler/reconciliation` and writes evidence proving that the restarted control plane requeued waiting jobs, marked non-resumable active jobs `recovery_required` for explicit operator retry, recorded bounded sampled durable job IDs for those interrupted rows, and reattached resumable native ComfyUI prompt trackers with sampled durable job IDs and native prompt IDs.

```bash
export B1_RESTART_RECONCILIATION_API_BASE=https://api.ai.b1.germering
export B1_RESTART_RECONCILIATION_API_KEY=...
export B1_RESTART_RECONCILIATION_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_RESTART_RECONCILIATION_STARTED_AFTER="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker compose restart control-plane
export B1_RESTART_RECONCILIATION_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/restart-reconciliation.json
make restart-reconciliation-acceptance
```

By default the harness requires at least one requeued waiting job, at least one sampled requeued job ID, at least one active job marked `recovery_required`, at least one sampled recovery-required job ID, and at least one native ComfyUI prompt tracker reattached with sampled durable job and native prompt IDs. Override `B1_RESTART_RECONCILIATION_MIN_REQUEUED`, `B1_RESTART_RECONCILIATION_MIN_MARKED_RECOVERY_REQUIRED`, or `B1_RESTART_RECONCILIATION_MIN_RESUMED_COMFYUI_NATIVE` only for a labelled development dry run; production handoff should keep all three at their default value of `1`. Control Center acceptance reports reject shallow restart evidence that only marks the required checks `ok`; handoff-ready evidence must include the healthy reconciliation API status, required CPU/GPU runner inventory with no missing required runners, control-plane and runner timestamps after the captured pre-restart timestamp, runner runtime coverage, observed/minimum counts, interrupted job ID samples, and reattached native ComfyUI prompt ID samples.

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

The generator writes `b1-ai-hub-backup-migration-rollback-acceptance/v1` evidence under `$B1_BACKUP_ROOT/acceptance/` and Control Center blocks handoff if any required check is missing or incomplete. Handoff-ready evidence must also include detailed backup, restore, old-stack archive, Open WebUI preservation, cutover readiness, preserved-resource, and rollback checksum/action proof; a JSON file that only marks the required checks `ok` is treated as incomplete.

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

The file contains default LAN URLs, including the Open WebUI chat-host smoke URL, Caddy internal CA paths, prompt/job template paths, evidence output paths, and explicit blank values for the restart reconciliation timestamp, Model Hub sync aliases, browser session/password, and any split-scope API keys. It contains no secrets by default and the generator refuses to overwrite an existing file unless rerun directly with `--force`. The preflight is deliberately local and non-networked: it parses the env file safely instead of executing shell, checks that final handoff values and safety gates are set, verifies Caddy CA/evidence paths, rejects unsafe API and Open WebUI smoke URLs, rejects unedited prompt/job JSON before the live suite starts, and writes `$B1_BACKUP_ROOT/acceptance/operator-preflight.json`.

When every required live-test environment variable is configured and the restart-reconciliation drill state has already been prepared, `make operator-live-acceptance` runs preflight first, then the live smoke, installed workflow, LocalAI, GPU, external compatibility, security, and restart-reconciliation harnesses with their default evidence paths under `$B1_BACKUP_ROOT/acceptance/`. It does not generate backup/migration/rollback evidence; that remains a reviewed artifact flow.
