# External Consumers

External services can consume B1 AI Hub in two ways:

1. Call the unified API at `https://api.ai.b1.germering/`.
2. Synchronise permitted model blobs from Model Hub into a local cache.

The `integrations/b1-model-client` package supports `list`, `plan`, `pin`, `unpin`, `sync`, `prune`, and `daemon` commands. It never stores plaintext tokens in its cache state and never removes unmanaged local files.

Linux or macOS shell example:

```bash
export B1_MODELHUB_URL=https://models.ai.b1.germering
export B1_MODELHUB_TOKEN=...

b1-model-client list
b1-model-client pin chat-default image-default --cache ~/.cache/b1-ai-hub/models
b1-model-client plan --cache ~/.cache/b1-ai-hub/models
b1-model-client sync --cache ~/.cache/b1-ai-hub/models --dry-run
b1-model-client sync --cache ~/.cache/b1-ai-hub/models
b1-model-client prune --cache ~/.cache/b1-ai-hub/models --dry-run
```

Windows PowerShell example:

```powershell
$env:B1_MODELHUB_URL = "https://models.ai.b1.germering"
$env:B1_MODELHUB_TOKEN = "..."

b1-model-client.exe pin chat-default --cache "$env:LOCALAPPDATA\B1 AI Hub\models"
b1-model-client.exe sync --cache "$env:LOCALAPPDATA\B1 AI Hub\models"
```

When no model arguments are supplied, `plan`, `sync`, and `daemon` use locally pinned models. If no pins exist yet, they ask the server catalog for default public aliases. `pin` validates the model or alias against Model Hub but records only model metadata in `CACHE/b1-model-client-state.json`, not the bearer token. `unpin` removes local intent only; it does not delete blobs. `prune --dry-run` shows managed blobs that are no longer required by the selected or pinned models, and `prune` deletes only SHA-256-named files recorded in the local managed-blob state. Unmanaged files in the cache are ignored.

Downloaded blobs are stored under `CACHE/blobs/{sha256}` and are written through `CACHE/blobs/{sha256}.partial` before atomic publication. The client sends the local blob inventory to `POST /modelhub/v1/sync/plan`, keeps existing verified blobs, resumes partial files with `Range`, validates `ETag`, `X-Checksum-SHA256`, `Content-Length`, `Content-Range`, final byte count, and final SHA-256, then records the blob as managed. Inference-only or restricted models are skipped by the client and must be consumed through hosted inference.

The same package can run as a small containerized daemon on an external machine:

```bash
docker build -t b1-model-client integrations/b1-model-client
docker volume create b1-model-cache
docker run -d --name b1-model-client \
  -e B1_MODELHUB_URL=https://models.ai.b1.germering \
  -e B1_MODELHUB_TOKEN="$B1_MODELHUB_TOKEN" \
  -e B1_MODEL_CLIENT_INTERVAL_SECONDS=3600 \
  -v b1-model-cache:/cache \
  b1-model-client daemon --cache /cache --prune
```

For cron or scheduled tasks, use `daemon --once` to run exactly one sync cycle. Use `daemon --dry-run` first when testing a new workstation or allowlist.

Administrators can create a dedicated Model Hub client key for a workstation:

```bash
curl -s https://models.ai.b1.germering/modelhub/v1/clients \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"artist-workstation","allowed_models":["image-default"],"cidr_allowlist":["192.168.2.0/24"]}'
```

The returned key is shown once and is scoped only for catalog reads and blob synchronisation. Server-side sync plans return `keep`, `download`, `replace`, and `skip` actions using the client's local blob inventory, so external tools can preview storage/network impact before downloading. Use explicit `allowed_models` values for workstation keys unless the machine is trusted to cache every downloadable alias.

The `integrations/comfyui-b1-remote-nodes` package provides external ComfyUI nodes that call the B1 unified API. Credentials must come from environment or ComfyUI server settings, not workflow JSON:

```bash
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY=...
export B1_AI_HUB_DOWNLOAD_DIR=/path/to/external/comfyui/output/b1-ai-hub
```

The package currently registers nodes for listing/selecting model aliases, chat/text, vision request shaping, embeddings, text-to-image, image-to-image, text-to-video, image-to-video, TTS, STT, generic media-job submit/wait/cancel, media upload by base64, job artifact listing, and artifact download. Artifact downloads are constrained to `B1_AI_HUB_DOWNLOAD_DIR` and filenames are sanitized. Media-reference fields reject local filesystem paths; use staged-upload JSON, `/artifacts/...` URLs, data URLs, or HTTP(S) URLs.

These nodes intentionally call `/v1/*` and `/artifacts/...` on `api.ai.b1.germering`; they do not call the native server-side ComfyUI `/prompt` or `/ws` compatibility endpoint. The `integrations/comfyui-b1-remote-nodes/examples/tts-fast.non-comfy.workflow.json` sketch is the default non-Comfy proof path: stop the server-side B1 ComfyUI container, keep the API/control plane and CPU audio runtime running, then run the external `B1 Text To Speech` node with `model=tts-fast` and `runtime_policy=non_comfy_only`.

For stock local ComfyUI execution, use a synchronized local cache and `extra_model_paths.yaml` rather than live network-mounted model loading.

## Optional External Runtimes

B1 AI Hub does not silently use cloud or partner runtimes. Remote providers remain disabled until an administrator sets `B1_ALLOW_EXTERNAL_PROVIDERS=true`, supplies a safe external adapter base URL, and acknowledges that requests may leave the LAN. Prefer the Control Center Runtimes tab or `/admin/runtimes/external-config` for day-to-day configuration; database-managed rows override the environment fallback. For OpenAI-compatible providers, store the bearer credential as an encrypted secret in category `remote-provider` and reference that secret name from the runtime configuration. `B1_OPENAI_COMPATIBLE_BASE_URL`, `B1_OPENAI_COMPATIBLE_API_KEY_FILE`, and `B1_GENERIC_HTTP_BASE_URL` remain bootstrap fallbacks. The URL validator accepts HTTPS public hosts only, rejects credentials in the URL, query strings, fragments, relative path segments, malformed ports, and private/loopback/link-local/reserved IP literals. If the URL is absent or unsafe, `/admin/runtimes` shows the adapter as `unconfigured` and alias resolution will not select it.
