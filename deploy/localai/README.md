# LocalAI Deployment

LocalAI is the first GPU runtime with a production Compose override. The base `compose.yaml` keeps a mock `localai` runtime for development, while `compose.production-localai.yaml` replaces it with the official upstream CUDA 12 image:

```text
localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973
```

The verified linux/amd64 platform manifest for that image is:

```text
sha256:1b27b2469dcd78b21c33034eb3503efcb07330380b9ade00c14c48b2b09b641d
```

Use it with:

```bash
docker compose -f compose.yaml -f compose.production-localai.yaml up -d
```

The override uses Docker Compose `!reset` tags to remove the inherited mock `build`, `B1_RUNTIME_KIND`, and `B1_RUNTIME_NAME` fields. Validate the merged service before starting:

```bash
docker compose -f compose.yaml -f compose.production-localai.yaml config --quiet
```

## Mounts

LocalAI does not receive the authoritative model blob library. It receives only the runtime-specific model view:

```text
$B1_DATA_ROOT/models/runtime-views/localai:/srv/b1-ai-hub/models:ro
```

Bootstrap creates the writable state used by the official container:

```text
$B1_DATA_ROOT/data/localai/configuration
$B1_DATA_ROOT/data/localai/backends
$B1_DATA_ROOT/data/localai/data
$B1_DATA_ROOT/cache/localai
```

These paths map to `LOCALAI_CONFIG_DIR`, `LOCALAI_BACKENDS_PATH`, `LOCALAI_DATA_PATH`, and cache storage. Model installation remains controlled by the B1 Model Hub; the LocalAI container should read already validated model views.

## Runtime Policy

The control plane is the authoritative scheduler. LocalAI also receives conservative internal guard rails as a second line of defence:

```text
LOCALAI_MAX_ACTIVE_BACKENDS=1
LOCALAI_WATCHDOG_IDLE=true
LOCALAI_WATCHDOG_IDLE_TIMEOUT=5m
LOCALAI_WATCHDOG_INTERVAL=1s
LOCALAI_FORCE_EVICTION_WHEN_BUSY=false
```

The web UI and CORS are disabled by default through `LOCALAI_DISABLE_WEBUI=true` and `LOCALAI_CORS=false`, because normal administration is through B1 Control Center and all LAN access should enter through Caddy and the unified API.

## Health And Ports

The production override points the control plane at `http://localai:8080` and health-checks:

```text
GET http://127.0.0.1:8080/readyz
```

No LocalAI port is published to the LAN. All user and external-client traffic must enter through the gateway and control-plane scheduler.

## Adapter Hooks

The control plane may call optional internal B1 runtime hooks before GPU media submission:

- `POST /b1/runtime/load`
- `POST /b1/runtime/warm`
- `POST /b1/runtime/smoke`
- `POST /b1/runtime/unload`

The placeholder service implements these hooks for smoke testing. A real LocalAI image must either implement model-aware load/warm/smoke behavior or return 404/405 so the control plane treats the hooks as unsupported.

The control-plane adapter currently submits:

- `/v1/images/generations` as JSON for text-to-image
- `/v1/images/edits` as multipart for image edits
- `/v1/videos/generations` as JSON for text-to-video
- `/v1/videos/image-to-video` as multipart with an `image` file field for image-to-video

Media responses are expected to use an OpenAI-style `data` array with `b64_json`, `b64`, `base64`, `data:` URL, or same-origin `url` entries plus an optional `mime_type`.

Current limitation: the official LocalAI image is not modified by B1 yet, so it does not implement the optional B1 load/warm/smoke hooks. The scheduler still controls submissions and the adapter treats 404/405 hook responses as unsupported, but production acceptance still needs real model-specific smoke, unload, and measured VRAM tests before cutover.
