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

Use the same TLS helper variables as smoke tests when testing through the Caddy internal CA or temporary hostnames:

- `B1_INTEGRATION_CA_FILE`
- `B1_INTEGRATION_TLS_VERIFY=0`
- `B1_INTEGRATION_HOST_HEADER`

Remaining integration coverage will expand to LocalAI streaming/unload internals, more native ComfyUI REST/WebSocket client behaviours, Voicebox external-server scenarios, CPU audio, Model Hub blob semantics, artifact authorization, and restart reconciliation.
