# comfyui-b1-remote-nodes

This package provides ComfyUI custom nodes that call the unified B1 AI Hub API. They do not call the server-side native ComfyUI endpoint and must not store API credentials inside workflow JSON. Media-reference inputs accept staged B1 upload JSON, authenticated `/artifacts/...` paths, or bounded data URLs; they reject local filesystem paths and arbitrary external URLs.

Install by copying or cloning this directory into the external ComfyUI `custom_nodes/` directory, then restart that ComfyUI instance.

Configure credentials through environment variables or a local JSON config file. Do not put keys in workflow JSON:

```bash
install -d -m 0700 ~/.config/b1-ai-hub
install -m 0600 /dev/null ~/.config/b1-ai-hub/comfyui-remote-nodes.key
# Paste the scoped B1 API key into ~/.config/b1-ai-hub/comfyui-remote-nodes.key.
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_CA_FILE=/path/to/b1-caddy-root.crt
export B1_AI_HUB_API_KEY_FILE=~/.config/b1-ai-hub/comfyui-remote-nodes.key
export B1_AI_HUB_DOWNLOAD_DIR=/path/to/comfyui/output/b1-ai-hub
```

Environment variables take precedence. `B1_AI_HUB_API_KEY_FILE` is preferred for workstations because the API key stays out of shell history. As a local ComfyUI-process config fallback, copy `config.example.json` to a private path such as `~/.config/b1-ai-hub/comfyui-remote-nodes.json`, write the scoped B1 API key to the referenced `api_key_file`, and set both files to mode `0600` on Linux/macOS. `api_key_file` and `ca_file` paths in the JSON may be relative to the config file. Inline `api_key` and `B1_AI_HUB_API_KEY` are still supported for compatibility, but on POSIX the config file must be private or the nodes fail closed. You can also point at another file with `B1_AI_HUB_CONFIG_FILE=/path/to/comfyui-remote-nodes.json`. Set `B1_AI_HUB_CONFIG_FILE=` to disable config-file lookup.

If the B1 gateway uses Caddy's internal CA and this external ComfyUI process does not trust it globally, set `B1_AI_HUB_CA_FILE` or `ca_file` to the exported root certificate from `$B1_DATA_ROOT/data/caddy/pki/authorities/local/root.crt`. Requests with an API key refuse plain HTTP unless `B1_AI_HUB_ALLOW_INSECURE_HTTP=true` is set for an isolated development harness.

Available nodes:

- `B1 List Models`
- `B1 Select Model Alias`
- `B1 Chat / Text`
- `B1 Vision Analysis`
- `B1 Embeddings`
- `B1 Text To Image`
- `B1 Image To Image`
- `B1 Text To Video`
- `B1 Image To Video`
- `B1 Text To Speech`
- `B1 Speech To Text`
- `B1 Upload Media Base64`
- `B1 Submit Media Job`
- `B1 Wait Media Job`
- `B1 Cancel Media Job`
- `B1 List Job Artifacts`
- `B1 Download Artifact`

The nodes use only `https://api.ai.b1.germering` unified API routes such as `/v1/chat/completions`, `/v1/embeddings`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/media/uploads`, `/v1/media/jobs`, and `/artifacts/...`. Generic request construction accepts only internal absolute paths without schemes, hosts, query strings, fragments, traversal segments, encoded slashes, or control characters, so server-provided links cannot redirect a node call to another origin or hidden route. They never call `https://comfy.ai.b1.germering`, `/prompt`, `/ws`, or the server-side native ComfyUI container.

Media inputs are accepted as validated staged-upload JSON, authenticated `/artifacts/...` paths, or base64 media data URLs capped by `B1_AI_HUB_MAX_DATA_URL_BYTES`. Local filesystem paths, arbitrary external URLs, artifact paths with query/fragment data, and arbitrary JSON objects are rejected by the media-reference nodes. `B1 Upload Media Base64` accepts only the server-supported media upload types PNG, JPEG, WebP, GIF, WAV, MP3, Ogg, MP4, and WebM; it normalizes MIME types, validates explicit `image`/`audio`/`video` field MIME families, sanitizes upload filenames, and returns a direct `reference_json` value that can be connected to image-to-image and image-to-video nodes, plus the full `upload_json` wrapper for inspection. Media-job nodes accept both shapes. Wait/cancel/artifact-list nodes accept either a bare B1 job ID, the full job JSON returned by `B1 Submit Media Job`, or a validated internal `/v1/media/jobs/...` route; when job JSON includes server-provided `links`, the nodes follow those links and reject external, query-bearing, malformed, or wrong-route links. Vision analysis currently requires an internal artifact path or data URL because it uses the OpenAI-compatible image URL field. `B1 Speech To Text` sends the selected model alias and audio as an OpenAI-style multipart `/v1/audio/transcriptions` request, using a sanitized uploaded filename and no private B1 model header. Artifact downloads are written only under `B1_AI_HUB_DOWNLOAD_DIR` or the default local `b1-artifacts/` directory; requested filenames are sanitized, collisions get a digest-suffixed filename instead of overwriting an existing file, POSIX outputs are created mode `0600`, and traversal, encoded traversal, malformed percent escapes, invalid percent-encoded UTF-8, query strings, and fragments are refused. `B1 Download Artifact` also accepts a single artifact JSON record or the JSON returned by `B1 List Job Artifacts`; when `bytes` or `sha256` metadata is present, the node verifies the downloaded content before writing it.

Examples are in `examples/`. `all-modalities.reference.workflow.json` is a credential-free reference sketch that exercises every registered B1 remote-node class. `tts-fast.non-comfy.workflow.json` is the smoke path for proving an external ComfyUI can use a hosted non-Comfy backend while the server-side B1 ComfyUI container is stopped. It uses `B1 Text To Speech` with the `tts-fast` alias; the default scaffold CPU audio profile is useful only for development dry runs, and live acceptance requires a real non-placeholder TTS engine such as the pinned Piper path. The matching live compatibility test can stop and restore the B1 `comfyui` service when run with `B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose`, records node-surface/example coverage, records runtime-agent proof that the B1 `comfyui` service still has zero running containers after the non-Comfy operation, and records the TTS placeholder/engine response headers plus file-backed artifact integrity so scaffold output or an unsafe local download cannot satisfy handoff evidence.
