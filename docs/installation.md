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

```bash
cp .env.example .env
make bootstrap
docker compose up -d
```

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

Model storage under `$B1_DATA_ROOT/models` has three distinct responsibilities:

- `blobs/` is the authoritative content-addressed library.
- `runtime-views/{localai,comfyui,voicebox,audio-cpu}/` contains hardlinked per-runtime read-only views created by the control plane.
- `quarantine/runtime-views/` stores removed runtime views for recovery while leaving blobs untouched.

## Production LocalAI Override

The default `docker compose up -d` topology intentionally uses the lightweight mock `localai` service so a fresh repository can boot before GPU models are installed. For real LocalAI runtime validation, start the stack with the production override:

```bash
docker compose -f compose.yaml -f compose.production-localai.yaml up -d
```

The override:

- replaces the mock build with the official pinned CUDA 12 image `localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973`
- switches `LOCALAI_URL` from `http://localai:8000` to LocalAI's native `http://localai:8080`
- keeps LocalAI on the internal `runtime` network with no published backend port
- mounts `$B1_DATA_ROOT/models/runtime-views/localai` read-only at `/srv/b1-ai-hub/models`
- creates writable LocalAI state at `$B1_DATA_ROOT/data/localai/{configuration,backends,data}` and `$B1_DATA_ROOT/cache/localai`
- reserves one GPU by default through `B1_LOCALAI_GPU_DRIVER=nvidia.com/gpu` and `B1_LOCALAI_GPU_COUNT=1`
- sets LocalAI's own backend guard rails with `LOCALAI_MAX_ACTIVE_BACKENDS=1`, `LOCALAI_WATCHDOG_IDLE=true`, `LOCALAI_WATCHDOG_IDLE_TIMEOUT=5m`, `LOCALAI_WATCHDOG_INTERVAL=1s`, and `LOCALAI_FORCE_EVICTION_WHEN_BUSY=false`
- disables LocalAI's web UI and CORS by default because B1 gateway/control-plane UIs are the managed surfaces
- health-checks `http://127.0.0.1:8080/readyz`

Use `B1_LOCALAI_GPU_DRIVER=nvidia` only if the installed NVIDIA Container Toolkit still requires the legacy Compose driver name. The first LocalAI startup can take a long time while the image initializes or downloads runtime backends; the health check allows a one-hour start period for that reason.

Do not set `B1_RUNTIME_DEPLOYMENT_MODE=production` for cutover until LocalAI is healthy through the gateway, at least one installed model smoke test has passed, unload/recovery has been exercised through runtime-agent, and the remaining required runtimes are also real rather than placeholders.

## Production ComfyUI Override

For native ComfyUI validation, start the stack with the production ComfyUI override:

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
- health-checks `http://127.0.0.1:8188/system_stats`

Native clients still use `https://comfy.ai.b1.germering/` through the control-plane compatibility proxy. Do not publish ComfyUI `8188` directly; use `compose.legacy-comfy.yaml` only for the optional restricted legacy listener that still terminates at the scheduler-aware proxy.

## Production Voicebox Override

Voicebox is optional in the base topology. For managed Voicebox server/web/API validation, start the stack with the production Voicebox override and the `voicebox` profile:

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

External Voicebox UIs and REST/MCP clients must use `https://voice.ai.b1.germering/` through Caddy and the control-plane access policy. Do not publish `17493` directly. Add `voicebox` to `B1_RUNTIME_PRODUCTION_REQUIRED` only after the selected Voicebox engine/profile has passed a scheduler-managed smoke request, backup/export/delete tests for voice profiles, and unload/recovery validation on the target host.

## Caddy Internal CA

The default Caddyfile uses `tls internal`. Export the Caddy root certificate from `$B1_DATA_ROOT/data/caddy/pki/authorities/local/root.crt` after first boot and install it only on trusted LAN clients.

The control-plane self-test uses the same root certificate path by default for its `https://$B1_HOST_API/healthz` routing probe. If you replace Caddy certificates or test through a different internal URL, set `B1_SELF_TEST_TLS_URLS` and `B1_SELF_TEST_TLS_CA_FILE` in `.env` before restarting the control plane.

Externally supplied certificates can be used by replacing the Caddy TLS directives through configuration, without changing application source code.
