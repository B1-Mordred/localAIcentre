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

The production image starts a B1 proxy on `:17493` and starts upstream Voicebox on loopback `127.0.0.1:17494` by default. The proxy forwards native REST, web, MCP HTTP, and WebSocket traffic to upstream Voicebox while handling B1 scheduler lifecycle routes itself. This avoids patching upstream Voicebox source and keeps raw upstream traffic inside the container.

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

The B1 proxy implements these runtime hooks:

- `POST /b1/runtime/load`
- `POST /b1/runtime/warm`
- `POST /b1/runtime/smoke`
- `POST /b1/runtime/unload`

Production Compose mounts `$B1_DATA_ROOT/secrets/runtime_control_token` read-only and sets `B1_RUNTIME_CONTROL_REQUIRE_AUTH=true`. The control plane sends this token as `Authorization: Bearer ...` for all `/b1/runtime/*` hook calls. If the token is missing or wrong, the proxy rejects lifecycle actions before touching the managed Voicebox process.

Hook behavior is intentionally bounded:

- `load` checks Voicebox-visible runtime model roots and returns `unconfirmed` because upstream engine/profile selection performs final validation and lazy model loading.
- `warm` returns `unconfirmed` unless `B1_VOICEBOX_HOOK_WARM_ENABLED=true`.
- `smoke` returns `unconfirmed` unless `B1_VOICEBOX_HOOK_SMOKE_ENABLED=true`.
- enabled `warm`/`smoke` send a small `/v1/audio/speech` probe to upstream using `B1_VOICEBOX_HOOK_SMOKE_TEXT`, `B1_VOICEBOX_HOOK_SMOKE_VOICE`, and `B1_VOICEBOX_HOOK_SMOKE_ENDPOINT`; enable this only after the selected engine/profile has safe smoke parameters.
- `unload` refuses to restart while native proxy requests are active; when idle and `B1_VOICEBOX_HOOK_RESTART_ON_UNLOAD=true`, it restarts the loopback upstream process to release model memory.

Use `B1_VOICEBOX_HOOK_STRICT_MODEL_LIST=true` only when installed manifests resolve to filenames or directories visible under `B1_VOICEBOX_HOOK_MODEL_ROOTS`. Production acceptance still requires measured smoke tests for the selected engine/profile on `ai.b1.germering`, including profile backup/export/delete and verification that a GPU Voicebox request cannot overlap with LocalAI or ComfyUI.
