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
python main.py --listen 0.0.0.0 --port 8188 --disable-auto-launch --log-stdout --extra-model-paths-config /opt/b1/comfyui/extra_model_paths.yaml --reserve-vram 1.5 --max-upload-size 256 --disable-api-nodes --cache-none
```

`--disable-api-nodes` keeps built-in API/cloud nodes from communicating with the internet. `HF_HUB_DISABLE_TELEMETRY=1` and `DO_NOT_TRACK=1` are also set. `--cache-none` and `--reserve-vram 1.5` are conservative defaults for the RTX 3060/32 GB profile; tune them only after measured runs.

Health checks use native `GET /system_stats` on internal port `8188`. The control plane uses `COMFYUI_URL=http://comfyui:8188`.

Approved custom-node pins are tracked in `workflows/approved-node-pins.json`, not by editing workflow manifests alone. A published workflow that declares `{"type":"node","id":"...","version":"<commit>"}` becomes dependency-ready only when that exact commit appears in the registry with `status: approved`. Keep repository URLs HTTPS-only and record dependency-lock SHA-256 values when a node brings Python package changes.

The GPU runner can call optional internal B1 hooks on the selected runtime before submission:

- `POST /b1/runtime/load`
- `POST /b1/runtime/warm`
- `POST /b1/runtime/smoke`
- `POST /b1/runtime/unload`

A pinned ComfyUI runtime can leave those hooks unimplemented and rely on native lazy loading, but production acceptance still requires measured smoke and unload behavior through native APIs or the runtime-agent bounded restart fallback.
