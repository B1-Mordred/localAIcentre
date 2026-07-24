# Installation

## Host Prerequisites

- Docker Engine
- Docker Compose v2
- NVIDIA driver and NVIDIA Container Toolkit for GPU runtime work
- LAN DNS records for the configured virtual hosts
- sufficient storage under `B1_DATA_ROOT`, default `/srv/b1-ai-hub`

Validate host basics:

```bash
docker --version
docker compose version
nvidia-smi
stat -c '%g' /var/run/docker.sock
```

Set `B1_DOCKER_GID` in `.env` to the final command's value when runtime-agent should read Docker service status and bounded logs while remaining non-root.

## First Boot

For local development and repository validation, start the lightweight topology:

```bash
cp .env.example .env
make bootstrap
docker compose up -d
```

For the production appliance path on `ai.b1.germering`, start from the production template instead:

```bash
cp .env.production.example .env
make bootstrap
docker compose up -d
```

The production template sets `COMPOSE_FILE=compose.yaml:compose.production-localai.yaml:compose.production-comfyui.yaml:compose.production-voicebox.yaml` and `COMPOSE_PROFILES=voicebox`, so Docker Compose selects the real-runtime overlays while the operator start command remains `docker compose up -d`. It also sets `B1_RUNTIME_DEPLOYMENT_MODE=production`, makes LocalAI, ComfyUI, Voicebox, and audio-cpu required for production readiness, disables CPU scaffold responses, and leaves external/cloud providers and Docker mutations disabled. Remove the Voicebox overlay/profile and remove `voicebox` from `B1_RUNTIME_PRODUCTION_REQUIRED` only when intentionally operating without managed Voicebox.

Bootstrap creates external data directories and generated secrets under `/srv/b1-ai-hub` by default. It does not delete or overwrite existing stack data. The generated secrets include the runtime-agent bearer token plus a private runtime-agent mTLS CA, server certificate, and control-plane client certificate used for the internal `https://runtime-agent:8443` API.

On startup, the `control-plane` container runs Alembic migrations with `python -m app.migrate upgrade head` before starting Uvicorn. This keeps the documented fresh-install command as `docker compose up -d` while creating a durable `alembic_version` record for future non-destructive schema upgrades. Set `B1_DB_MIGRATIONS_ENABLED=false` only for an externally managed deployment where migrations are applied separately; the application startup verifies required tables and columns and fails closed if the schema is not current.

Manual migration inspection uses the same Compose service and secrets:

```bash
make db-current
make db-migrate
```

Bootstrap also creates `$B1_DATA_ROOT/secrets/open_webui_api_key`. The B1 Open WebUI wrapper image reads that mounted file at container startup and exports it as `OPENAI_API_KEY` for Open WebUI. The control plane reads the same mounted secret and stores only the matching hashed scoped `service` API client in PostgreSQL. Do not paste the administrator bootstrap key into Open WebUI.

Open `https://control.ai.b1.germering/` after the services are healthy. On first boot, Control Center shows the initial-admin setup form. Enter the generated bootstrap key from `$B1_DATA_ROOT/secrets/admin_bootstrap_key`, choose an administrator username, and set a password of at least 12 characters using at least three character classes. The browser receives an HttpOnly session cookie and stores the returned CSRF token for Control Center and Media Studio requests. After the first administrator exists, normal sign-in uses username and password; scoped service/API clients are created from the External Access tab.

For direct local HTTP development only, set `B1_SESSION_COOKIE_SECURE=false` and keep `B1_DEV_AUTH_BYPASS=false` unless you explicitly want to bypass every role check.

After creating a scoped API client with `models:read`, `jobs:read`, and `jobs:write`, run the opt-in live smoke suite from the repository root:

```bash
B1_SMOKE_LIVE_TEST=1 B1_AI_HUB_API_KEY=... make smoke
```

The smoke suite checks gateway health, authenticated model listing, an async `tts-fast` media job, SSE job events, artifact download, and optional `/admin/self-test` when `B1_SMOKE_ADMIN_API_KEY` is set. With the default Caddy internal CA, set `B1_SMOKE_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt` or trust that root certificate on the test machine. See `tests/smoke/README.md` for temporary-host and TLS options.

After installing real GPU model manifests and publishing at least one target-host ComfyUI API prompt/workflow, run the cross-runtime RTX acceptance suite during a maintenance validation window:

```bash
export B1_GPU_ACCEPTANCE_API_BASE=https://api.ai.b1.germering
export B1_GPU_ACCEPTANCE_API_KEY=...
export B1_GPU_ACCEPTANCE_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_GPU_ACCEPTANCE_COMFY_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json
export B1_GPU_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/cross-runtime-gpu.json
make gpu-acceptance
```

The test uses the configured aliases `B1_GPU_ACCEPTANCE_CHAT_MODEL`, `B1_GPU_ACCEPTANCE_COMFY_MODEL`, and `B1_GPU_ACCEPTANCE_VOICEBOX_MODEL`, defaulting to `chat-default`, `image-default`, and `tts-quality`. It verifies production readiness, runtime-agent GPU metrics, LocalAI -> ComfyUI -> Voicebox switching, one reported GPU-resident pipeline at a time, and sampled VRAM within the configured reserve. Leave `B1_GPU_ACCEPTANCE_REQUIRE_PRODUCTION=true` for cutover evidence; disable it only for an explicitly labelled dry run.

Model storage under `$B1_DATA_ROOT/models` has three distinct responsibilities:

- `blobs/` is the authoritative content-addressed library.
- `runtime-views/{localai,comfyui,voicebox,audio-cpu}/` contains hardlinked per-runtime read-only views created by the control plane.
- `quarantine/runtime-views/` stores removed runtime views for recovery while leaving blobs untouched.

## Production LocalAI Override

The development `.env.example` path intentionally uses the lightweight mock `localai` service so a fresh repository can boot before GPU models are installed. The production `.env.production.example` path includes this override automatically through `COMPOSE_FILE`. For isolated LocalAI validation, start the stack with only the production LocalAI override:

```bash
docker compose -f compose.yaml -f compose.production-localai.yaml up -d
```

The override:

- replaces the mock build with the B1 LocalAI wrapper image `b1-ai-hub/localai:v4.7.1-b1`, built from official LocalAI CUDA 12 image `localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973`
- switches `LOCALAI_URL` from `http://localai:8000` to the B1 wrapper on `http://localai:8080`, which forwards normal LocalAI API traffic to the private upstream LocalAI listener on `127.0.0.1:18080`
- keeps LocalAI on the internal `runtime` network with no published backend port
- mounts `$B1_DATA_ROOT/models/runtime-views/localai` read-only at `/srv/b1-ai-hub/models`
- creates writable LocalAI state at `$B1_DATA_ROOT/data/localai/{configuration,backends,data}` and `$B1_DATA_ROOT/cache/localai`
- reserves one GPU by default through `B1_LOCALAI_GPU_DRIVER=nvidia.com/gpu` and `B1_LOCALAI_GPU_COUNT=1`
- sets LocalAI's own backend guard rails with `LOCALAI_MAX_ACTIVE_BACKENDS=1`, `LOCALAI_WATCHDOG_IDLE=true`, `LOCALAI_WATCHDOG_IDLE_TIMEOUT=5m`, `LOCALAI_WATCHDOG_INTERVAL=1s`, and `LOCALAI_FORCE_EVICTION_WHEN_BUSY=false`
- disables LocalAI's web UI and CORS by default because B1 gateway/control-plane UIs are the managed surfaces
- implements `POST /b1/runtime/load`, `POST /b1/runtime/warm`, `POST /b1/runtime/smoke`, and `POST /b1/runtime/unload` for scheduler-aware readiness and unload probes
- health-checks `http://127.0.0.1:8080/readyz`

Use `B1_LOCALAI_GPU_DRIVER=nvidia` only if the installed NVIDIA Container Toolkit still requires the legacy Compose driver name. The first LocalAI startup can take a long time while the image initializes or downloads runtime backends; the health check allows a one-hour start period for that reason.

By default the B1 lifecycle hooks are conservative: model-list misses return `unconfirmed`, warm and smoke inference are disabled, and video smoke is disabled. For acceptance testing with installed model manifests, set `B1_LOCALAI_HOOK_STRICT_MODEL_LIST=true` and `B1_LOCALAI_HOOK_SMOKE_ENABLED=true`; set `B1_LOCALAI_HOOK_WARM_ENABLED=true` only when a tiny warm inference is acceptable before the real request. The unload hook calls LocalAI's native `/backend/shutdown` endpoint for the resolved model.

Do not set `B1_RUNTIME_DEPLOYMENT_MODE=production` for cutover until LocalAI is healthy through the gateway, at least one installed model smoke test has passed with strict hooks enabled, unload/recovery has been exercised through runtime-agent and the LocalAI hook, and the remaining required runtimes are also real rather than placeholders.

## Production ComfyUI Override

The production `.env.production.example` path includes this override automatically through `COMPOSE_FILE`. For isolated native ComfyUI validation, start the stack with only the production ComfyUI override:

```bash
docker compose -f compose.yaml -f compose.production-comfyui.yaml up -d
```

To validate cross-runtime scheduling with LocalAI and ComfyUI together, combine the overrides:

```bash
docker compose -f compose.yaml \
  -f compose.production-localai.yaml \
  -f compose.production-comfyui.yaml \
  up -d
```

The ComfyUI override:

- builds a B1 image from upstream ComfyUI `v0.3.77` commit `59afc3984868289f808d02fa5cd180edfb2de240`
- verifies the downloaded source archive SHA-256 `0758fc23e0a62202b48582fd47a59b811edc3b0e04e1c50d253332c03db4b5a1`
- uses pinned base image `pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime@sha256:e05438443ae3c407e8d04447091a959dbb6757b6290b128770c3c787d4bd442b`
- switches `COMFYUI_URL` from `http://comfyui:8000` to native `http://comfyui:8188`
- keeps ComfyUI on the internal `runtime` network with no published backend port
- mounts `$B1_DATA_ROOT/models/runtime-views/comfyui` read-only at `/srv/b1-ai-hub/models`
- maps ComfyUI input/user/output/temp/cache to B1-managed external paths
- starts with `--disable-api-nodes`, `--cache-none`, `--reserve-vram 1.5`, no browser auto-launch, no CORS flag, and `HF_HUB_DISABLE_TELEMETRY=1`
- installs B1 lifecycle hooks at `POST /b1/runtime/load`, `/warm`, `/smoke`, and `/unload` without proxying or wrapping native ComfyUI traffic
- health-checks `http://127.0.0.1:8188/system_stats`

The hook defaults are conservative. `load` checks ComfyUI-visible model folders when possible and otherwise returns `unconfirmed` because model dependencies are ultimately validated by the native workflow. `warm` and `smoke` return `unconfirmed` unless `B1_COMFYUI_HOOK_WARM_ENABLED=true` or `B1_COMFYUI_HOOK_SMOKE_ENABLED=true`; when enabled, they run a tiny B1 no-op output node through the native ComfyUI queue to prove the execution loop is alive, not to claim any model-specific workflow has passed. `unload` sets ComfyUI's native `unload_models` and `free_memory` flags and, when the queue is idle, immediately calls the native model/cache cleanup functions. Set `B1_COMFYUI_HOOK_STRICT_MODEL_LIST=true` only when manifests use ComfyUI-visible filenames or paths. Override `B1_COMFYUI_HOOK_MODEL_FOLDERS` only when approved custom nodes add additional model folder keys that should participate in lifecycle checks.

Native clients still use `https://comfy.ai.b1.germering/` through the control-plane compatibility proxy. Do not publish ComfyUI `8188` directly; use `compose.legacy-comfy.yaml` only for the optional restricted legacy listener that still terminates at the scheduler-aware proxy.

## Production Voicebox Override

Voicebox is optional in the base topology. The production `.env.production.example` path enables the `voicebox` profile and includes this override automatically through `COMPOSE_FILE`. For isolated managed Voicebox server/web/API validation, start the stack with the production Voicebox override and the `voicebox` profile:

```bash
docker compose -f compose.yaml -f compose.production-voicebox.yaml --profile voicebox up -d
```

To validate all current real GPU runtime overlays together:

```bash
docker compose -f compose.yaml \
  -f compose.production-localai.yaml \
  -f compose.production-comfyui.yaml \
  -f compose.production-voicebox.yaml \
  --profile voicebox \
  up -d
```

The Voicebox override:

- builds a B1 image from Jamie Pine Voicebox `v0.5.0` commit `2bcb98d1a8b6fe05e15fbc1559e3085669e4035d`
- verifies the downloaded source archive SHA-256 `d901d1e20f6a238830abff268ae5d8d60448b34b7ef0e65d9f0f88a10f1ee083`
- uses pinned base images `oven/bun:1.3.8@sha256:371d30538b69303ced927bb5915697ac7e2fa8cb409ee332c66009de64de5aa3` and `python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93`
- pins upstream Git dependencies used by the Docker build to `QwenLM/Qwen3-TTS@022e286b98fbec7e1e916cb940cdf532cd9f488e`, `ysharma3501/LinaCodec@c0ae7c7285e121475c27592cfbb600624b714290`, and `ysharma3501/LuxTTS@28ae6a61151684fffc9d1a7aa15eafa02286fe0b`
- constrains upstream's broad Python dependency ranges with `deploy/voicebox/constraints.txt`, generated from the validated B1 image resolution
- switches `VOICEBOX_URL` from `http://voicebox:8000` to native `http://voicebox:17493`
- keeps Voicebox on the internal `runtime` network with no published backend port
- mounts `$B1_DATA_ROOT/models/runtime-views/voicebox` read-only at `/srv/b1-ai-hub/models`
- maps `$B1_DATA_ROOT/data/voicebox` to `/srv/b1-ai-hub/voicebox` for the SQLite DB, voice profiles, captures, and generations
- maps `$B1_DATA_ROOT/cache/voicebox` to `/srv/b1-ai-hub/cache` for Hugging Face and application caches
- health-checks `http://127.0.0.1:17493/health`
- starts a B1 proxy on `:17493`, keeps upstream Voicebox on loopback `127.0.0.1:17494`, forwards native REST/web/WebSocket traffic, and implements `/b1/runtime/load`, `/warm`, `/smoke`, and `/unload`

The hook defaults are conservative. `load` checks Voicebox-visible model roots and otherwise returns `unconfirmed`; `warm` and `smoke` return `unconfirmed` unless `B1_VOICEBOX_HOOK_WARM_ENABLED=true` or `B1_VOICEBOX_HOOK_SMOKE_ENABLED=true`; `unload` restarts the loopback upstream process only while the proxy sees no active native requests. Set `B1_VOICEBOX_HOOK_STRICT_MODEL_LIST=true` only when manifests use Voicebox-visible filenames or directories.

External Voicebox UIs and REST/MCP clients must use `https://voice.ai.b1.germering/` through Caddy and the control-plane access policy. Do not publish `17493` directly. Add `voicebox` to `B1_RUNTIME_PRODUCTION_REQUIRED` only after the selected Voicebox engine/profile has passed a scheduler-managed smoke request, backup/export/delete tests for voice profiles, and unload/recovery validation on the target host.

## Caddy Internal CA

The default Caddyfile uses `B1_CADDY_TLS_ARGS=internal`, which expands to Caddy's `tls internal` mode for every B1 virtual host. Export the Caddy root certificate from `$B1_DATA_ROOT/data/caddy/pki/authorities/local/root.crt` after first boot and install it only on trusted LAN clients.

The control-plane self-test uses the same root certificate path by default for its `https://$B1_HOST_API/healthz` routing probe. If you replace Caddy certificates or test through a different internal URL, set `B1_SELF_TEST_TLS_URLS` and `B1_SELF_TEST_TLS_CA_FILE` in `.env` before restarting the control plane.

Externally supplied certificates can be used without source changes:

```bash
install -d -m 0750 /srv/b1-ai-hub/secrets/caddy-certs
install -m 0640 fullchain.pem /srv/b1-ai-hub/secrets/caddy-certs/fullchain.pem
install -m 0640 privkey.pem /srv/b1-ai-hub/secrets/caddy-certs/privkey.pem
```

Then set this in `.env` and restart only the gateway:

```env
B1_CADDY_TLS_ARGS="/etc/caddy/external-certs/fullchain.pem /etc/caddy/external-certs/privkey.pem"
```

```bash
docker compose up -d gateway
```

The gateway mounts `$B1_DATA_ROOT/secrets/caddy-certs` read-only at `/etc/caddy/external-certs`. Leave `B1_CADDY_TLS_ARGS=internal` when using the LAN internal CA.
