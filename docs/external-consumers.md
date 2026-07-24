# External Consumers

External services can consume B1 AI Hub in two ways:

1. Call the unified API at `https://api.ai.b1.germering/`.
2. Synchronise permitted model blobs from Model Hub into a local cache.

The `integrations/b1-model-client` package supports `list`, `plan`, `pin`, `unpin`, `sync`, `prune`, and `daemon` commands. It never stores plaintext tokens in its cache state and never removes unmanaged local files.

Linux or macOS shell example:

```bash
python -m pip install ./integrations/b1-model-client

export B1_MODELHUB_URL=https://models.ai.b1.germering
export B1_MODELHUB_TOKEN=...

b1-model-client list
b1-model-client pin chat-default image-default --cache ~/.cache/b1-ai-hub/models
b1-model-client plan --cache ~/.cache/b1-ai-hub/models
b1-model-client sync --cache ~/.cache/b1-ai-hub/models --dry-run
b1-model-client sync --cache ~/.cache/b1-ai-hub/models
b1-model-client sync --cache ~/.cache/b1-ai-hub/models --accept-license
b1-model-client prune --cache ~/.cache/b1-ai-hub/models --dry-run
```

Windows PowerShell example:

```powershell
python -m pip install .\integrations\b1-model-client

$env:B1_MODELHUB_URL = "https://models.ai.b1.germering"
$env:B1_MODELHUB_TOKEN = "..."

b1-model-client.exe pin chat-default --cache "$env:LOCALAPPDATA\B1 AI Hub\models"
b1-model-client.exe sync --cache "$env:LOCALAPPDATA\B1 AI Hub\models"
```

When no model arguments are supplied, `plan`, `sync`, and `daemon` use locally pinned models. If no pins exist yet, they ask the server catalog for default public aliases. `pin` validates the model or alias against Model Hub but records only model metadata in `CACHE/b1-model-client-state.json`, not the bearer token. `unpin` removes local intent only; it does not delete blobs. `prune --dry-run` shows managed blobs that are no longer required by the selected or pinned models, and `prune` deletes only SHA-256-named files recorded in the local managed-blob state. Unmanaged files in the cache are ignored.

Downloaded blobs are stored under `CACHE/blobs/{sha256}` and are written through `CACHE/blobs/{sha256}.partial` before atomic publication. The client sends a SHA-256-verified local blob inventory to `POST /modelhub/v1/sync/plan`, rejects plan actions whose blob IDs are not 64-character SHA-256 digests, recomputes local write paths from the verified digest instead of trusting server-supplied paths, keeps existing verified blobs, resumes partial files with `Range`, validates `ETag`, `X-Checksum-SHA256`, exact resumed `Content-Length` and `Content-Range`, final byte count, and final SHA-256, then records the blob as managed. If a stale partial causes the server to reject the resume range with HTTP 416, the client deletes only that partial and retries once as a full verified download. Model Hub blob responses also include `X-RateLimit-*` headers from the server's per-client download window; slow or split large syncs if a workstation key exhausts its configured rate. Inference-only or restricted models are skipped by the client and must be consumed through hosted inference.

Model Hub catalog, model, version, and sync-plan responses expose only redacted manifest source URLs. Custom clients should use the returned Model Hub blob URLs, ETags, hashes, and size metadata for synchronization, not upstream source URLs. Sync plans include the resolved immutable model version, licence terms, attribution where declared, redacted source URL, execution modes, and resource estimate for each action. If a manifest declares `license.acceptance_required=true`, `b1-model-client sync` and `daemon` refuse to synchronize the model until the operator reviews `b1-model-client plan` and reruns with `--accept-license` or `B1_MODEL_CLIENT_ACCEPT_LICENSES=true`. Accepted blob downloads include `X-B1-Accept-License: model_id@version`; custom clients must send the same header when Model Hub returns HTTP 428 with `required_model_refs`.

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

For cron or scheduled tasks, use `daemon --once` to run exactly one sync cycle. Use `daemon --dry-run` first when testing a new workstation or allowlist. Only set `B1_MODEL_CLIENT_ACCEPT_LICENSES=true` for unattended daemon runs after the administrator has confirmed that the selected pinned models' terms allow that workstation cache.

Administrators can create a dedicated Model Hub client key for a workstation:

```bash
curl -s https://models.ai.b1.germering/modelhub/v1/clients \
  -H "Authorization: Bearer $B1_ADMIN_BOOTSTRAP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"artist-workstation","allowed_models":["image-default"],"cidr_allowlist":["192.168.2.0/24"]}'
```

The returned key is shown once and is scoped only for catalog reads and blob synchronisation. Server-side sync plans return `keep`, `download`, `replace`, and `skip` actions using the client's local blob inventory, so external tools can preview storage/network impact before downloading. Use explicit `allowed_models` values for workstation keys unless the machine is trusted to cache every downloadable alias.

Administrators can later change a workstation key's allowed model list or switch it to catalog-only mode from the Control Center External Access tab, or through `PUT /modelhub/v1/clients/{id}/policy`. CIDR allowlists are edited separately through `PUT /modelhub/v1/clients/{id}/cidr-allowlist`, so network changes do not require issuing a new secret.

The `integrations/comfyui-b1-remote-nodes` package provides external ComfyUI nodes that call the B1 unified API. Credentials must come from the external ComfyUI process environment or a local config file, not workflow JSON:

```bash
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY=...
export B1_AI_HUB_DOWNLOAD_DIR=/path/to/external/comfyui/output/b1-ai-hub
```

Environment variables take precedence. As a file-based fallback, copy `integrations/comfyui-b1-remote-nodes/config.example.json` to `~/.config/b1-ai-hub/comfyui-remote-nodes.json` on Linux or macOS, write the scoped B1 API key to the referenced `api_key_file`, and protect both files with mode `0600`. On Windows, use `%APPDATA%\\B1 AI Hub\\comfyui-remote-nodes.json`. `api_key_file` paths in the JSON may be relative to the config file. Inline `api_key` is still supported for compatibility, but POSIX clients reject group/world-accessible config files that contain a token. `B1_AI_HUB_API_KEY_FILE=/path/to/key` is also supported as an environment override. `B1_AI_HUB_CONFIG_FILE=/path/to/file.json` selects a different path, and an empty `B1_AI_HUB_CONFIG_FILE=` disables config-file lookup.

The package currently registers nodes for listing/selecting model aliases, chat/text, vision request shaping, embeddings, text-to-image, image-to-image, text-to-video, image-to-video, TTS, STT, generic media-job submit/wait/cancel, media upload by base64, job artifact listing, and artifact download. `B1 Speech To Text` calls the public OpenAI-compatible multipart transcription shape with `model` and `file` fields, so external ComfyUI workflows do not rely on private B1 headers. Artifact downloads are constrained to `B1_AI_HUB_DOWNLOAD_DIR`, filenames are sanitized, and traversal, encoded traversal, query strings, and fragments are refused. Job IDs used by wait/cancel/artifact-list nodes are validated as opaque B1 identifiers before they are added to request paths. Media-reference fields reject local filesystem paths, arbitrary external URLs, artifact paths with query/fragment data, and arbitrary JSON objects; upload media through `B1 Upload Media Base64`, connect its `reference_json` output directly to media-job nodes, use authenticated `/artifacts/...` paths, or pass base64 media data URLs capped by `B1_AI_HUB_MAX_DATA_URL_BYTES`. The upload node accepts only the server-supported media upload types PNG, JPEG, WebP, GIF, WAV, MP3, Ogg, MP4, and WebM. It normalizes MIME types, validates explicit `image`/`audio`/`video` field MIME families, sanitizes upload filenames, and returns the full upload response as `upload_json` for workflows that need to inspect metadata. Media-job nodes accept either that wrapper or the direct staged reference. Vision-analysis requests currently require an internal artifact path or data URL because they use the OpenAI-compatible image URL field.

These nodes intentionally call `/v1/*` and `/artifacts/...` on `api.ai.b1.germering`; they do not call the native server-side ComfyUI `/prompt` or `/ws` compatibility endpoint. The `integrations/comfyui-b1-remote-nodes/examples/tts-fast.non-comfy.workflow.json` sketch is the default non-Comfy proof path: stop the server-side B1 ComfyUI container, keep the API/control plane and CPU audio runtime running, then run the external `B1 Text To Speech` node with `model=tts-fast` and `runtime_policy=non_comfy_only`.

The opt-in compatibility harness can enforce that proof against the local Compose deployment:

```bash
export B1_REMOTE_NODES_LIVE_TEST=1
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY=...
export B1_AI_HUB_DOWNLOAD_DIR=/tmp/b1-remote-node-output
export B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose
python3 -m unittest tests.compatibility.test_remote_nodes_non_comfy
```

`docker-compose` mode stops the B1 `comfyui` service before invoking the node and starts it again afterward if it was running. Use `manual` only when another runbook has already stopped the service and you want the test to avoid mutating Compose.

For stock local ComfyUI execution, use a synchronized local cache and `extra_model_paths.yaml` rather than live network-mounted model loading.

## Optional External Runtimes

B1 AI Hub does not silently use cloud or partner runtimes. Remote providers remain disabled until an administrator sets `B1_ALLOW_EXTERNAL_PROVIDERS=true`, supplies a safe external adapter base URL, and acknowledges that requests may leave the LAN. Prefer the Control Center Runtimes tab or `/admin/runtimes/external-config` for day-to-day configuration; database-managed rows override the environment fallback. For OpenAI-compatible providers, store the bearer credential as an encrypted secret in category `remote-provider` and reference that secret name from the runtime configuration. `B1_OPENAI_COMPATIBLE_BASE_URL`, `B1_OPENAI_COMPATIBLE_API_KEY_FILE`, and `B1_GENERIC_HTTP_BASE_URL` remain bootstrap fallbacks. The URL validator accepts HTTPS public hosts only, rejects credentials in the URL, query strings, fragments, relative path segments, malformed ports, and private/loopback/link-local/reserved IP literals. If the URL is absent or unsafe, `/admin/runtimes` shows the adapter as `unconfigured` and alias resolution will not select it.
