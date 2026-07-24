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

Run the installed workflow suite on the target host after the public aliases are backed by real installed models and approved workflows. It exercises the user-facing paths for chat, synchronous TTS, synchronous STT, CPU-audio GPU-lease isolation, image generation, image edit, and short video, rejects placeholder CPU audio output by default, downloads generated artifacts, and writes evidence for the Control Center handoff report. The API key must include normal inference scopes plus `runtimes:read` so the harness can read `/admin/scheduler/lease` before and after the CPU-audio probe.

```bash
export B1_WORKFLOWS_API_BASE=https://api.ai.b1.germering
export B1_WORKFLOWS_API_KEY=...
export B1_WORKFLOWS_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_WORKFLOWS_IMAGE_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/image-generation-job.json
export B1_WORKFLOWS_IMAGE_EDIT_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/image-edit-job.json
export B1_WORKFLOWS_VIDEO_JOB_FILE=/srv/b1-ai-hub/workflows/acceptance/short-video-job.json
export B1_WORKFLOWS_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/installed-workflows.json
B1_WORKFLOWS_LIVE_TEST=1 python3 -m unittest tests.integration.test_live_installed_workflows
```

The image, edit, and video job files should be full `POST /v1/media/jobs` JSON bodies that reference installed aliases and either published workflow IDs or native ComfyUI/LocalAI inputs validated for the RTX 3060 profile. The harness provides simple prompt defaults only to keep dry runs ergonomic; production acceptance should use explicit job files so the operator can review exact model/workflow dependencies. Set `B1_WORKFLOWS_CPU_TTS_MODEL` and `B1_WORKFLOWS_CPU_STT_MODEL` when CPU lease validation should use aliases different from the ordinary `tts-fast` and `stt-default` defaults. Set `B1_WORKFLOWS_ALLOW_PLACEHOLDER=1` only for a labelled development dry run; handoff evidence should leave it unset so placeholder TTS/STT output fails.

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

The API key must include chat/inference access plus `runtimes:read` and `runtimes:write`. The unload check is intentional: it exercises the same guarded admin route operators use to clear LocalAI residency. Keep `B1_LOCALAI_ACCEPTANCE_REQUIRE_PRODUCTION=true` for handoff evidence; disable it only for a labelled temporary-hostname dry run.

## RTX 3060 Cross-Runtime GPU Acceptance

Run the GPU acceptance suite only on the target host, or during an equivalent maintenance window with the real LocalAI, ComfyUI, and Voicebox runtime overlays enabled. It sends live inference work and may trigger bounded runtime recovery when explicitly requested.

```bash
export B1_GPU_ACCEPTANCE_API_BASE=https://api.ai.b1.germering
export B1_GPU_ACCEPTANCE_API_KEY=...
export B1_GPU_ACCEPTANCE_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_GPU_ACCEPTANCE_CHAT_MODEL=chat-default
export B1_GPU_ACCEPTANCE_COMFY_MODEL=image-default
export B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json
export B1_GPU_ACCEPTANCE_VOICEBOX_MODEL=tts-quality
export B1_GPU_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/cross-runtime-gpu.json
make gpu-acceptance
```

The suite verifies the persisted RTX 3060 resource policy, production runtime readiness when `B1_GPU_ACCEPTANCE_REQUIRE_PRODUCTION` is left enabled, runtime-agent GPU/NVML metrics, and the live switch sequence LocalAI chat -> ComfyUI media job -> Voicebox speech. After each step it checks that at most one managed GPU runtime reports a resident model/pipeline and that sampled VRAM stays within the configured total-minus-reserve policy. When `B1_GPU_ACCEPTANCE_EVIDENCE` is set, the output JSON includes `status`, `required_checks`, per-check records, and runtime/metric samples; Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` file and block handoff if the required checks are absent or incomplete. Set `B1_GPU_ACCEPTANCE_SKIP_VOICEBOX=1` only for a documented no-Voicebox deployment. Set `B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1` only during a maintenance acceptance window to exercise the predefined runtime recovery endpoint.

## Restart Reconciliation Acceptance

Run the restart reconciliation suite during a maintenance validation window after deliberately creating at least one interrupted `waiting_for_gpu` job and one interrupted active preparation/execution job. The recommended drill is to enable maintenance mode immediately after the interrupted state is present, capture the timestamp, restart only `control-plane`, and then run the harness before clearing or retrying the drill jobs. The harness reads `GET /admin/scheduler/reconciliation` and writes evidence proving that the restarted control plane requeued waiting jobs while marking interrupted active jobs `recovery_required` for explicit operator retry.

```bash
export B1_RESTART_RECONCILIATION_API_BASE=https://api.ai.b1.germering
export B1_RESTART_RECONCILIATION_API_KEY=...
export B1_RESTART_RECONCILIATION_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_RESTART_RECONCILIATION_STARTED_AFTER="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker compose restart control-plane
export B1_RESTART_RECONCILIATION_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/restart-reconciliation.json
make restart-reconciliation-acceptance
```

By default the harness requires at least one requeued waiting job and at least one active job marked `recovery_required`. Override `B1_RESTART_RECONCILIATION_MIN_REQUEUED` or `B1_RESTART_RECONCILIATION_MIN_MARKED_RECOVERY_REQUIRED` only for a labelled development dry run; production handoff should keep both at their default value of `1`.

Use the same TLS helper variables as smoke tests when testing through the Caddy internal CA or temporary hostnames:

- `B1_INTEGRATION_CA_FILE`
- `B1_INTEGRATION_TLS_VERIFY=0`
- `B1_INTEGRATION_HOST_HEADER`

Remaining integration coverage will expand to more native ComfyUI REST/WebSocket client behaviours, Voicebox external-server scenarios, and Model Hub blob semantics.
