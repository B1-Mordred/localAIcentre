# ComfyUI Deployment

This directory contains the B1-managed ComfyUI production image build. There is no official upstream Docker image suitable for the production Compose contract, so B1 builds a minimal image from a pinned upstream source archive and a pinned PyTorch CUDA runtime base.

Pinned source:

```text
Comfy-Org/ComfyUI v0.3.77
commit 59afc3984868289f808d02fa5cd180edfb2de240
archive sha256 0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1
```

Pinned base:

```text
pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime@sha256:e05438443ae3c407e8d04447091a959dbb6757b6290b128770c3c787d4bd442b
```

Use it with:

```bash
docker compose -f compose.yaml -f compose.production-comfyui.yaml up -d
```

Native clients must reach ComfyUI only through the scheduler-aware compatibility proxy.

## Runtime Layout

The override resets the development mock environment and volume list. The ComfyUI container receives only:

```text
$B1_DATA_ROOT/models/runtime-views/comfyui:/srv/b1-ai-hub/models:ro
$B1_DATA_ROOT/data/comfyui/input:/srv/b1-ai-hub/comfyui/input
$B1_DATA_ROOT/data/comfyui/user:/srv/b1-ai-hub/comfyui/user
$B1_DATA_ROOT/artifacts/temporary/comfyui-output:/srv/b1-ai-hub/comfyui/output
$B1_DATA_ROOT/artifacts/temporary/comfyui-temp:/srv/b1-ai-hub/comfyui/temp
$B1_DATA_ROOT/cache/comfyui:/srv/b1-ai-hub/cache
```

The image uses `extra_model_paths.yaml` to map B1 model views into ComfyUI folder classes such as checkpoints, diffusion models, text encoders, VAE, LoRA, ControlNet, upscale models, and embeddings. The authoritative model blob library is not mounted.

## Startup Policy

The entrypoint starts:

```text
python main.py --listen 0.0.0.0 --port 8188 --disable-auto-launch --log-stdout --extra-model-paths-config /opt/b1/comfyui/extra_model_paths.yaml --reserve-vram 1.0 --max-upload-size 256 --disable-api-nodes --cache-none --lowvram
```

`--disable-api-nodes` keeps built-in API/cloud nodes from communicating with the internet. `HF_HUB_DISABLE_TELEMETRY=1` and `DO_NOT_TRACK=1` are also set. `--cache-none`, `--lowvram`, and `--reserve-vram 1.0` are conservative defaults for the current RTX 3060 Laptop 6 GB / 32 GB profile; tune them only after measured runs.

Health checks use native `GET /system_stats` on internal port `8188`. The control plane uses `COMFYUI_URL=http://comfyui:8188`.

Approved custom-node pins are tracked in `workflows/approved-node-pins.json`, not by editing workflow manifests alone. A published workflow that declares `{"type":"node","id":"...","version":"<commit>"}` becomes dependency-ready only when that exact commit appears in the registry with `status: approved`. Keep repository URLs HTTPS-only and record dependency-lock SHA-256 values when a node brings Python package changes. If a pinned node exposes a required mutating HTTP API, add the audited path prefixes to that pin's `allowed_route_prefixes` and to `B1_COMFYUI_TRUSTED_ROUTE_PREFIXES`; the compatibility proxy requires both before forwarding custom mutating routes.

## B1 Runtime Hooks

The image installs the B1-owned `b1_runtime_hooks` custom-node package. It registers internal lifecycle routes directly in ComfyUI's native aiohttp server, so native REST, `/ws`, binary previews, uploads, `/history`, `/view`, and future safe ComfyUI routes are not wrapped by a second proxy.

The GPU runner can call internal B1 hooks on the selected runtime before submission:

- `POST /b1/runtime/load`
- `POST /b1/runtime/status`
- `POST /b1/runtime/warm`
- `POST /b1/runtime/smoke`
- `POST /b1/runtime/unload`
- `POST /b1/runtime/build-info`

Production Compose mounts `$B1_DATA_ROOT/secrets/runtime_control_token` read-only and sets `B1_RUNTIME_CONTROL_REQUIRE_AUTH=true`. The control plane sends this token as `Authorization: Bearer ...` for all `/b1/runtime/*` hook calls. If the token is missing or wrong, the hook route rejects lifecycle actions before touching ComfyUI's queue or model-management APIs.

Hook behavior is intentionally bounded:

- `load` checks ComfyUI-visible model folders when possible and returns `unconfirmed` because the native prompt/workflow still performs final dependency validation and lazy model loading.
- `status` returns scheduler-facing queue counts, a VRAM/memory snapshot, model-folder file counts, lifecycle capabilities, and the same pinned build metadata returned by `build-info`. It does not expose model filenames, prompts, uploads, or node inputs.
- `warm` returns `unconfirmed` unless `B1_COMFYUI_HOOK_WARM_ENABLED=true`.
- `smoke` returns `unconfirmed` unless `B1_COMFYUI_HOOK_SMOKE_ENABLED=true`.
- enabled `warm`/`smoke` run a manifest-provided native API-format prompt from `runtime_smoke.comfyui.prompt` when present; this uses ComfyUI's own validation, queue, history, and lazy model loading, and reports bounded hook measurements back to the control plane.
- when no native prompt is configured, enabled `warm`/`smoke` fall back to a tiny B1 no-op output node through ComfyUI's native queue; this proves the ComfyUI execution loop is responsive, but it is not a model-specific workflow acceptance test.
- `B1RuntimeTinyImage` is available for deterministic native compatibility smoke prompts that need a viewable image artifact without loading model weights.
- `unload` sets ComfyUI's native `unload_models` and `free_memory` flags and, when the queue is idle, immediately calls native model/cache cleanup.
- `build-info` returns the B1 hook package version, `Comfy-Org/ComfyUI` upstream version, pinned upstream commit, and source archive SHA-256. `/admin/self-test` calls it and `status` over the internal runtime URL and fails in production when ComfyUI is required but the deployed runtime does not report pinned metadata or lifecycle/status evidence.

Use `B1_COMFYUI_HOOK_STRICT_MODEL_LIST=true` only when installed manifests resolve to filenames or relative paths visible in ComfyUI model folders. Override `B1_COMFYUI_HOOK_MODEL_FOLDERS` only if approved custom nodes introduce additional model folder keys that should participate in lifecycle checks. Production acceptance should install ComfyUI-backed manifests with `runtime_smoke.schema=b1-ai-hub-runtime-smoke/v1` and a small model-specific `runtime_smoke.comfyui.prompt`, then persist measured VRAM data for the exact published workflows and model manifests on the target RTX 3060 Laptop 6 GB / 32 GB host.
