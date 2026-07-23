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

Recommended first hardware upgrade: system RAM from 32 GB to at least 64 GB.
