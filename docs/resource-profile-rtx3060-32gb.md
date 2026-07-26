# RTX 3060 / 32 GB Resource Profile

Initial policy:

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

The scheduler must keep at most one GPU-resident model or pipeline active. CPU-only embedding, TTS, or STT models may remain resident only when enabled by policy and when host RAM reserve remains available.

Target-host acceptance evidence is produced with:

```bash
B1_GPU_ACCEPTANCE_LIVE_TEST=1 \
B1_GPU_ACCEPTANCE_API_KEY=... \
B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json \
B1_GPU_ACCEPTANCE_RUN_RECOVERY_ACTION=1 \
make gpu-acceptance
```

The acceptance suite samples `/admin/status`, `/admin/runtimes`, and `/admin/metrics` while running LocalAI chat, ComfyUI media generation, Voicebox speech, and the predefined runtime recovery action in sequence. Each LocalAI, ComfyUI, and Voicebox leg records an `after_runtime_state` proof with all managed GPU runtime rows, and the combined switch record preserves the ordered `runtime_state_sequence`. Control Center handoff reports derive the active GPU runtimes from those state rows, reject mismatches with the reported active-runtime list, and fail split-brain evidence before considering the switch complete. The suite also fails when more than one managed GPU runtime reports a resident model/pipeline or when runtime-agent GPU metrics show VRAM above the configured total-minus-reserve limit. Final evidence requires runtime-agent recovery to return `status=ok`; dry-run recovery rehearsals do not satisfy the handoff gate.

Recommended first hardware upgrade: system RAM from 32 GB to at least 64 GB.
