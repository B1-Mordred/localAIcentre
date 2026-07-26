# LocalAI Deployment

LocalAI is the first GPU runtime with a production Compose override. The base `compose.yaml` keeps a mock `localai` runtime for development, while `compose.production-localai.yaml` replaces it with the B1 wrapper image:

```text
b1-ai-hub/localai:v4.7.1-b1
```

The wrapper image is intentionally thin. It starts the official upstream CUDA 12 image on a private in-container listener and exposes a small stdlib proxy on `:8080` for native LocalAI/OpenAI-compatible traffic plus B1 scheduler lifecycle hooks. The upstream base image is:

```text
localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973
```

The verified linux/amd64 platform manifest for that image is:

```text
sha256:1b27b2469dcd78b21c33034eb3503efcb07330380b9ade00c14c48b2b09b641d
```

The upstream image config digest inspected during pinning is:

```text
sha256:b471c58b8d8897369346189e774ff7e21dfcd51e35dd8f5a5da14300ac44586a
```

Use it with:

```bash
docker compose -f compose.yaml -f compose.production-localai.yaml up -d
```

The override uses Docker Compose `!reset` tags to remove the inherited mock `B1_RUNTIME_KIND` and `B1_RUNTIME_NAME` fields. Validate the merged service before starting:

```bash
docker compose -f compose.yaml -f compose.production-localai.yaml config --quiet
```

## Mounts

LocalAI does not receive the authoritative model blob library. It receives only the runtime-specific model view:

```text
$B1_DATA_ROOT/models/runtime-views/localai:/srv/b1-ai-hub/models:ro
```

At startup, and again before scheduler lifecycle hooks such as `load`, `warm`, and `smoke`, the B1 wrapper scans read-only `manifest.b1.json` files in that runtime view and writes deterministic managed LocalAI YAML configs into `LOCALAI_CONFIG_DIR`. It also writes a combined `b1-managed-models.yaml`, and the production Compose override points `LOCALAI_MODELS_CONFIG_FILE` at that combined file. This lets a Model Hub installed GGUF expose the manifest ID as the LocalAI model name without giving LocalAI write access to the authoritative model library. The generated GGUF backend defaults to `llama`; the wrapper image bakes in the matching CUDA 12 `llama-cpp` backend from `quay.io/go-skynet/local-ai-backends@sha256:af63c83aea1761b9ae37e2932981b9078e2a41a20f98c687216dd1bb0b59e163` and exposes it through `LOCALAI_BACKENDS_SYSTEM_PATH`. Set `B1_LOCALAI_MANAGED_LLAMA_BACKEND` only when validating another backend on the target image. The wrapper only creates or removes files named `b1-managed-*.yaml` that contain the B1 managed marker; operator-created LocalAI config files are left untouched.

Bootstrap creates the writable state used by the official container:

```text
$B1_DATA_ROOT/data/localai/configuration
$B1_DATA_ROOT/data/localai/backends
$B1_DATA_ROOT/data/localai/data
$B1_DATA_ROOT/cache/localai
```

These paths map to `LOCALAI_CONFIG_DIR`, `LOCALAI_BACKENDS_PATH`, `LOCALAI_DATA_PATH`, and cache storage. Bootstrap marks them writable for the B1 application UID/GID, and the wrapper switches to `B1_LOCALAI_UID:B1_LOCALAI_GID` before starting LocalAI and the B1 proxy. Model installation remains controlled by the B1 Model Hub; the LocalAI container should read already validated model views.

## Runtime Policy

The control plane is the authoritative scheduler. LocalAI also receives conservative internal guard rails as a second line of defence:

```text
LOCALAI_MAX_ACTIVE_BACKENDS=1
LOCALAI_WATCHDOG_IDLE=true
LOCALAI_WATCHDOG_IDLE_TIMEOUT=5m
LOCALAI_WATCHDOG_INTERVAL=1s
LOCALAI_FORCE_EVICTION_WHEN_BUSY=false
```

The B1 wrapper exposes those guard rails through the authenticated internal `POST /b1/runtime/status` hook. The status payload reports the configured one-backend limit, idle watchdog settings, force-eviction policy, safe lifecycle capabilities, and a redacted upstream `/v1/models` probe containing only status and model count, not model filenames. `GET` or `POST /b1/runtime/build-info` reports the B1 wrapper version plus pinned upstream LocalAI image, version, and commit metadata.

The web UI and CORS are disabled by default through `LOCALAI_DISABLE_WEBUI=true` and `LOCALAI_CORS=false`, because normal administration is through B1 Control Center and all LAN access should enter through Caddy and the unified API.

## Health And Ports

The production override points the control plane at `http://localai:8080` and health-checks:

```text
GET http://127.0.0.1:8080/readyz
```

No LocalAI port is published to the LAN. All user and external-client traffic must enter through the gateway and control-plane scheduler.

## Adapter Hooks

The B1 wrapper handles optional internal runtime hooks before GPU submission:

- `POST /b1/runtime/load`
- `POST /b1/runtime/status`
- `GET /b1/runtime/build-info`
- `POST /b1/runtime/warm`
- `POST /b1/runtime/smoke`
- `POST /b1/runtime/unload`

Production Compose mounts `$B1_DATA_ROOT/secrets/runtime_control_token` read-only and sets `B1_RUNTIME_CONTROL_REQUIRE_AUTH=true`. The control plane sends this token as `Authorization: Bearer ...` for all `/b1/runtime/*` hook calls. If the token is missing or wrong, the wrapper rejects lifecycle actions instead of forwarding them to LocalAI.

Normal LocalAI requests are proxied unchanged to the private upstream listener. Hook behavior is intentionally bounded and does not log request prompts or user media:

- `status` reports the LocalAI one-backend and idle-watchdog guard rails plus a redacted upstream health/model-count probe.
- `build-info` reports pinned wrapper/upstream identity and lifecycle capabilities.
- `load` verifies `/v1/models` when available and returns `unconfirmed` because LocalAI loads models lazily on first inference.
- `warm` can run the same tiny operation-specific smoke request when `B1_LOCALAI_HOOK_WARM_ENABLED=true`; otherwise it returns `unconfirmed`.
- `smoke` runs a tiny chat, embedding, image, or TTS request only when `B1_LOCALAI_HOOK_SMOKE_ENABLED=true`. Video smoke remains disabled unless `B1_LOCALAI_VIDEO_SMOKE_ENABLED=true`.
- `unload` calls LocalAI's native `POST /backend/shutdown` with the resolved model name.

Set `B1_LOCALAI_HOOK_STRICT_MODEL_LIST=true` for cutover acceptance so a requested model missing from `/v1/models` becomes a hard preparation failure instead of an `unconfirmed` staging result.

Run the LocalAI acceptance harness after a real chat alias is installed:

```bash
B1_LOCALAI_ACCEPTANCE_API_KEY=... \
B1_LOCALAI_ACCEPTANCE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/localai-runtime.json \
make localai-acceptance
```

The harness verifies streamed chat through the unified API, one reported LocalAI GPU-resident backend, and confirmed unload through the guarded admin runtime route.

The control-plane adapter currently submits:

- `/v1/images/generations` as JSON for text-to-image
- `/v1/images/edits` as multipart for image edits
- `/v1/videos/generations` as JSON for text-to-video
- `/v1/videos/image-to-video` as multipart with an `image` file field for image-to-video

Media responses are expected to use an OpenAI-style `data` array with `b64_json`, `b64`, `base64`, `data:` URL, or same-origin `url` entries plus an optional `mime_type`.

Production acceptance still needs model-specific smoke results and measured VRAM data for the exact installed model manifests. The wrapper provides the hook surface for those tests; it does not bundle weights or guarantee that every LocalAI backend implements every OpenAI-compatible media route.
