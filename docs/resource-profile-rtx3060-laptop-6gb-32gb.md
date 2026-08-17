# RTX 3060 Laptop 6 GB / 32 GB Resource Profile

Current `ai.b1.germering` inventory reports an NVIDIA GeForce RTX 3060 Laptop GPU with 6144 MiB VRAM and 32 GB class host RAM. This reduced profile is the active production envelope for the existing machine. It keeps the same one-GPU scheduler invariant as the original 12 GB profile, but it narrows model choices, context size, and ComfyUI startup options so the appliance can pass hardware admission on this host.

Initial current-host policy:

```yaml
gpu:
  total_vram_gib: 6
  usable_vram_gib: 5
  reserve_vram_gib: 1
  maximum_active_pipelines: 1
host:
  total_ram_gib: 32
  reserve_ram_gib: 6
llm:
  default_context: 4096
  maximum_context: 8192
  default_parallel_requests: 1
comfyui:
  maximum_parallel_jobs: 1
  maximum_batch_size: 1
```

Production `.env` should carry:

```text
B1_HARDWARE_PROFILE=rtx3060-laptop-6gb-32gb
B1_HARDWARE_MIN_GPU_VRAM_MIB=6144
B1_HARDWARE_MIN_HOST_RAM_MIB=31744
B1_GPU_TOTAL_VRAM_GIB=6
B1_GPU_USABLE_VRAM_GIB=5
B1_GPU_RESERVE_VRAM_GIB=1
B1_LLM_DEFAULT_CONTEXT=4096
B1_LLM_MAXIMUM_CONTEXT=8192
B1_COMFYUI_RESERVE_VRAM_GIB=1.0
B1_COMFYUI_LOWVRAM=true
```

Model and workflow admission should prefer CPU embeddings/STT/TTS, 7B-class or smaller quantized chat models, low-VRAM image workflows, and short video workflows only after measured target-host runs. Any model that previously fit the original 12 GB RTX 3060 envelope still needs a fresh smoke measurement here; clients should keep using stable aliases so replacements do not require reconfiguration.

Hardware inventory and cutover evidence treat this as a selected profile, not as passing the original 12 GB baseline. A later upgrade should switch `B1_HARDWARE_PROFILE`, `B1_HARDWARE_MIN_GPU_VRAM_MIB`, and the resource-policy environment values before raising browser-editable bounds through Control Center.

Recommended first upgrade remains system RAM from 32 GB to at least 64 GB. A larger VRAM GPU should follow if video, large VLM, or higher-context chat workloads dominate.
