# External Consumers

External services can consume B1 AI Hub in two ways:

1. Call the unified API at `https://api.ai.b1.germering/`.
2. Synchronise permitted model blobs from Model Hub into a local cache.

The `integrations/b1-model-client` package supports `list`, `plan`, `pin`, `unpin`, `sync`, `prune`, and `daemon` commands. It never stores plaintext tokens in its cache state, creates POSIX cache directories/files privately, and never removes unmanaged local files.

Linux or macOS shell example:

```bash
python -m pip install ./integrations/b1-model-client

export B1_MODELHUB_URL=https://models.ai.b1.germering
export B1_MODELHUB_CA_FILE=/path/to/b1-caddy-root.crt
install -d -m 0700 ~/.config/b1-ai-hub
printf '%s' "$B1_MODELHUB_TOKEN" > ~/.config/b1-ai-hub/modelhub-token
chmod 0600 ~/.config/b1-ai-hub/modelhub-token
unset B1_MODELHUB_TOKEN
export B1_MODELHUB_TOKEN_FILE=~/.config/b1-ai-hub/modelhub-token

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
$env:B1_MODELHUB_CA_FILE = "C:\Path\To\b1-caddy-root.crt"
$tokenPath = "$env:APPDATA\B1 AI Hub\modelhub-token"
New-Item -ItemType Directory -Force -Path (Split-Path $tokenPath)
Set-Content -NoNewline -Path $tokenPath -Value $env:B1_MODELHUB_TOKEN
Remove-Item Env:\B1_MODELHUB_TOKEN
$env:B1_MODELHUB_TOKEN_FILE = $tokenPath

b1-model-client.exe pin chat-default --cache "$env:LOCALAPPDATA\B1 AI Hub\models"
b1-model-client.exe sync --cache "$env:LOCALAPPDATA\B1 AI Hub\models"
```

Prefer `B1_MODELHUB_TOKEN_FILE` or `--token-file` for workstation and daemon deployments. POSIX token files must be private to the current user, for example mode `0600`; the client refuses group/world-readable token files. Inline `--token` and `B1_MODELHUB_TOKEN` remain available for short-lived interactive runs, but the client refuses to start when both token sources are set. `B1_MODELHUB_URL` and `--base-url` must be an HTTP(S) URL without credentials, query strings, fragments, encoded traversal, or unsafe path segments. When a bearer token is configured, plain HTTP Model Hub URLs are refused by default; use HTTPS for LAN clients. `B1_MODEL_CLIENT_ALLOW_INSECURE_HTTP=true` exists only for isolated local development harnesses. If the gateway uses Caddy's internal CA and the workstation does not trust it globally, set `B1_MODELHUB_CA_FILE` or `--ca-file` to the exported root certificate from `$B1_DATA_ROOT/data/caddy/pki/authorities/local/root.crt`.

When no model arguments are supplied, `plan`, `sync`, and `daemon` use locally pinned models. If no pins exist yet, they ask the server catalog for default public aliases. Model IDs and aliases accepted by the client must be simple B1 identifiers: start with an ASCII letter or digit, contain only ASCII letters, digits, `.`, `_`, `+`, and `-`, and be at most 128 characters. The client rejects slashes, whitespace, URL escapes, query/fragment controls, traversal, and malformed catalog aliases before building Model Hub request paths. Model Hub base URLs and request paths also reject malformed percent escapes and invalid percent-encoded UTF-8 before any network call is made. `pin` validates the model or alias against Model Hub but records only model metadata in `CACHE/b1-model-client-state.json`, not the bearer token. On POSIX systems the cache root, blob directory, state file, partial blobs, and final blobs are forced to private owner-only modes (`0700` for directories and `0600` for files), including existing verified blobs kept during sync. Digest-named cache entries and partial blobs must be regular files; symlinks, directories, and special files are ignored for inventory and refused for sync writes. `unpin` removes local intent only; it does not delete blobs. `prune --dry-run` shows managed blobs that are no longer required by the selected or pinned models, and `prune` deletes only regular SHA-256-named files recorded in the local managed-blob state. Unmanaged files in the cache are ignored.

Downloaded blobs are stored under `CACHE/blobs/{sha256}` and are written through `CACHE/blobs/{sha256}.partial` before atomic publication. The client sends a SHA-256-verified local blob inventory to `POST /modelhub/v1/sync/plan`, rejects plan actions whose blob IDs are not 64-character SHA-256 digests, recomputes local write paths from the verified digest instead of trusting server-supplied paths, keeps existing verified blobs, resumes partial files with `Range`, validates `ETag`, `X-Checksum-SHA256`, exact resumed `Content-Length` and `Content-Range`, final byte count, and final SHA-256, then records the blob as managed. Model Hub blob URLs are content-addressed; the control plane canonicalizes uppercase hex and rejects malformed digest paths before policy lookup, rate limiting, or artifact-server proxying. If a stale partial causes the server to reject the resume range with HTTP 416, the client deletes only that partial and retries once as a full verified download. Model Hub blob responses also include `X-RateLimit-*` headers from the server's per-client download window; slow or split large syncs if a workstation key exhausts its configured rate. Inference-only or restricted models are skipped by the client and must be consumed through hosted inference.

Model Hub catalog, model, version, and sync-plan responses expose only redacted manifest source URLs. Custom clients should use the returned Model Hub blob URLs, ETags, hashes, and size metadata for synchronization, not upstream source URLs. Sync plans include the resolved immutable model version, licence terms, attribution where declared, redacted source URL, execution modes, and resource estimate for each action. If a manifest declares `license.acceptance_required=true`, `b1-model-client sync` and `daemon` refuse to synchronize the model until the operator reviews `b1-model-client plan` and reruns with `--accept-license` or `B1_MODEL_CLIENT_ACCEPT_LICENSES=true`. Accepted blob downloads include `X-B1-Accept-License: model_id@version`; custom clients must send the same header when Model Hub returns HTTP 428 with `required_model_refs`.

The same package can run as a small containerized daemon on an external machine:

```bash
docker build -t b1-model-client integrations/b1-model-client
docker volume create b1-model-cache
docker run -d --name b1-model-client \
  -e B1_MODELHUB_URL=https://models.ai.b1.germering \
  -e B1_MODELHUB_CA_FILE=/run/secrets/b1_caddy_root_ca \
  -e B1_MODELHUB_TOKEN_FILE=/run/secrets/b1_modelhub_token \
  -e B1_MODEL_CLIENT_INTERVAL_SECONDS=3600 \
  -v "$HOME/.config/b1-ai-hub/b1-caddy-root.crt:/run/secrets/b1_caddy_root_ca:ro" \
  -v "$HOME/.config/b1-ai-hub/modelhub-token:/run/secrets/b1_modelhub_token:ro" \
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
export B1_AI_HUB_CA_FILE=/path/to/b1-caddy-root.crt
export B1_AI_HUB_API_KEY=...
export B1_AI_HUB_DOWNLOAD_DIR=/path/to/external/comfyui/output/b1-ai-hub
```

Environment variables take precedence. As a file-based fallback, copy `integrations/comfyui-b1-remote-nodes/config.example.json` to `~/.config/b1-ai-hub/comfyui-remote-nodes.json` on Linux or macOS, write the scoped B1 API key to the referenced `api_key_file`, and protect both files with mode `0600`. On Windows, use `%APPDATA%\\B1 AI Hub\\comfyui-remote-nodes.json`. `api_key_file` and `ca_file` paths in the JSON may be relative to the config file. Inline `api_key` is still supported for compatibility, but POSIX clients reject group/world-accessible config files that contain a token. `B1_AI_HUB_API_KEY_FILE=/path/to/key` is also supported as an environment override. `B1_AI_HUB_CONFIG_FILE=/path/to/file.json` selects a different path, and an empty `B1_AI_HUB_CONFIG_FILE=` disables config-file lookup. If the gateway uses Caddy's internal CA and the external ComfyUI process does not trust it globally, set `B1_AI_HUB_CA_FILE` or `ca_file` to the exported root certificate from `$B1_DATA_ROOT/data/caddy/pki/authorities/local/root.crt`. Requests with an API key refuse plain HTTP unless `B1_AI_HUB_ALLOW_INSECURE_HTTP=true` is set for an isolated development harness.

The package currently registers nodes for listing/selecting model aliases, chat/text, vision request shaping, embeddings, text-to-image, image-to-image, text-to-video, image-to-video, TTS, STT, generic media-job submit/wait/cancel, media upload by base64, job artifact listing, and artifact download. `B1 Speech To Text` calls the public OpenAI-compatible multipart transcription shape with `model` and `file` fields, so external ComfyUI workflows do not rely on private B1 headers. Artifact downloads are constrained to `B1_AI_HUB_DOWNLOAD_DIR`, filenames are sanitized, and traversal, encoded traversal, malformed percent escapes, invalid percent-encoded UTF-8, query strings, and fragments are refused. The download node accepts a raw internal `/artifacts/...` path, a single artifact record, or a `B1 List Job Artifacts` response; when the record includes `bytes` or `sha256`, the node verifies the downloaded content before writing it. Job IDs used by wait/cancel/artifact-list nodes are validated as opaque B1 identifiers before they are added to request paths. Media-reference fields reject local filesystem paths, arbitrary external URLs, artifact paths with query/fragment data, and arbitrary JSON objects; upload media through `B1 Upload Media Base64`, connect its `reference_json` output directly to media-job nodes, use authenticated `/artifacts/...` paths, or pass base64 media data URLs capped by `B1_AI_HUB_MAX_DATA_URL_BYTES`. The upload node accepts only the server-supported media upload types PNG, JPEG, WebP, GIF, WAV, MP3, Ogg, MP4, and WebM. It normalizes MIME types, validates explicit `image`/`audio`/`video` field MIME families, sanitizes upload filenames, and returns the full upload response as `upload_json` for workflows that need to inspect metadata. Media-job nodes accept either that wrapper or the direct staged reference. Vision-analysis requests currently require an internal artifact path or data URL because they use the OpenAI-compatible image URL field.

These nodes intentionally call `/v1/*` and `/artifacts/...` on `api.ai.b1.germering`; they do not call the native server-side ComfyUI `/prompt` or `/ws` compatibility endpoint. The `integrations/comfyui-b1-remote-nodes/examples/tts-fast.non-comfy.workflow.json` sketch is the default non-Comfy proof path: stop the server-side B1 ComfyUI container, keep the API/control plane and CPU audio runtime running, then run the external `B1 Text To Speech` node with `model=tts-fast` and `runtime_policy=non_comfy_only`.

The opt-in compatibility harness can enforce that proof against the local Compose deployment:

```bash
export B1_REMOTE_NODES_LIVE_TEST=1
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY=...
export B1_AI_HUB_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_AI_HUB_DOWNLOAD_DIR=/tmp/b1-remote-node-output
export B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose
python3 -m unittest tests.compatibility.test_remote_nodes_non_comfy
```

`docker-compose` mode stops the B1 `comfyui` service before invoking the node and starts it again afterward if it was running. Use `manual` only when another runbook has already stopped the service and you want the test to avoid mutating Compose.

For stock local ComfyUI execution, use a synchronized local cache and `extra_model_paths.yaml` rather than live network-mounted model loading.

## Optional External Runtimes

B1 AI Hub does not silently use cloud or partner runtimes. Remote providers remain disabled until an administrator sets `B1_ALLOW_EXTERNAL_PROVIDERS=true`, supplies a safe external adapter base URL, and acknowledges that requests may leave the LAN. Prefer the Control Center Runtimes tab or `/admin/runtimes/external-config` for day-to-day configuration; database-managed rows override the environment fallback. For OpenAI-compatible providers, store the bearer credential as an encrypted secret in category `remote-provider` and reference that secret name from the runtime configuration. `B1_OPENAI_COMPATIBLE_BASE_URL`, `B1_OPENAI_COMPATIBLE_API_KEY_FILE`, and `B1_GENERIC_HTTP_BASE_URL` remain bootstrap fallbacks. The URL validator accepts HTTPS public hosts only, rejects credentials in the URL, query strings, fragments, literal or percent-encoded relative path segments, encoded path separators, malformed percent escapes, invalid percent-encoded UTF-8, control characters, malformed ports, and private/loopback/link-local/reserved IP literals. If the URL is absent or unsafe, `/admin/runtimes` shows the adapter as `unconfigured` and alias resolution will not select it.
