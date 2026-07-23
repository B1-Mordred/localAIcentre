# Voicebox Deployment

This directory contains the B1-managed Jamie Pine Voicebox production image build. There is no published upstream image with immutable base/runtime pins and the B1 Compose hardening contract, so B1 builds from the upstream source archive and patches only dependency references needed for reproducible Git checkouts. Upstream backend dependencies use broad version ranges; `constraints.txt` records the resolved PyPI dependency graph from the validated B1 build.

Pinned upstream:

```text
jamiepine/voicebox v0.5.0
commit: 2bcb98d1a8b6fe05e15fbc1559e3085669e4035d
source archive SHA-256: d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083
frontend base: oven/bun:1.3.8@sha256:371d30538b69303ced927bb5915697ac7e2fa8cb409ee332c66009de64de5aa3
backend/runtime base: python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93
Qwen3-TTS commit: 022e286b98fbec7e1e916cb940cdf532cd9f488e
LinaCodec commit: c0ae7c7285e121475c27592cfbb600624b714290
LuxTTS commit: 28ae6a61151684fffc9d1a7aa15eafa02286fe0b
```

Start it with the optional production profile:

```bash
docker compose -f compose.yaml -f compose.production-voicebox.yaml --profile voicebox up -d
```

Voicebox remains internal. External clients use `https://voice.ai.b1.germering/` through Caddy and B1 access policy; raw `17493` is not published.

## Runtime Layout

The production override resets the development placeholder environment and volume list. The Voicebox container receives only:

- `/srv/b1-ai-hub/voicebox`, backed by `$B1_DATA_ROOT/data/voicebox`, writable
- `/srv/b1-ai-hub/models`, backed by `$B1_DATA_ROOT/models/runtime-views/voicebox`, read-only
- `/srv/b1-ai-hub/cache`, backed by `$B1_DATA_ROOT/cache/voicebox`, writable
- `/tmp`, a Compose tmpfs inherited from the service defaults

Voice profiles, reference samples, captures, generated audio, and Voicebox's SQLite database are sensitive user data and belong under `$B1_DATA_ROOT/data/voicebox`, not in Git. Model weights remain controlled by B1 Model Hub; Voicebox receives only the runtime-specific read-only model view.

The image defaults to `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, so it does not silently download weights at runtime. Model Hub must prepare a Voicebox-compatible runtime view before a profile or engine is enabled.

## Scheduler Contract

The control plane is still the only service that may submit managed inference work. GPU Voicebox jobs are routed through the global lease and use `VOICEBOX_URL=http://voicebox:17493` when this override is active.

The upstream Voicebox server exposes its native REST, web UI, and MCP HTTP server. It does not currently implement the optional B1 runtime hooks:

- `POST /b1/runtime/load`
- `POST /b1/runtime/warm`
- `POST /b1/runtime/smoke`
- `POST /b1/runtime/unload`

The B1 adapter treats missing optional hooks as unsupported and relies on scheduler admission plus runtime-agent bounded restart/unload recovery. Production acceptance still requires measured smoke tests for the selected engine/profile on `ai.b1.germering`, including profile backup/export/delete and verification that a GPU Voicebox request cannot overlap with LocalAI or ComfyUI.
