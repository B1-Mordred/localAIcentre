# comfyui-b1-remote-nodes

This package provides ComfyUI custom nodes that call the unified B1 AI Hub API. They do not call the server-side native ComfyUI endpoint and must not store API credentials inside workflow JSON. Media-reference inputs accept staged B1 upload JSON, authenticated `/artifacts/...` paths, or bounded data URLs; they reject local filesystem paths and arbitrary external URLs.

Install by copying or cloning this directory into the external ComfyUI `custom_nodes/` directory, then restart that ComfyUI instance.

Configure credentials through environment variables or a local JSON config file. Do not put keys in workflow JSON:

```bash
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY=...
export B1_AI_HUB_DOWNLOAD_DIR=/path/to/comfyui/output/b1-ai-hub
```

Environment variables take precedence. As a local ComfyUI-process config fallback, copy `config.example.json` to a private path such as `~/.config/b1-ai-hub/comfyui-remote-nodes.json`, write the scoped B1 API key to the referenced `api_key_file`, and set both files to mode `0600` on Linux/macOS. `api_key_file` paths in the JSON may be relative to the config file. Inline `api_key` is still supported for compatibility, but on POSIX the config file must be private or the nodes fail closed. You can also point at another file with `B1_AI_HUB_CONFIG_FILE=/path/to/comfyui-remote-nodes.json`. Set `B1_AI_HUB_CONFIG_FILE=` to disable config-file lookup.

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

The nodes use only `https://api.ai.b1.germering` unified API routes such as `/v1/chat/completions`, `/v1/embeddings`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/media/uploads`, `/v1/media/jobs`, and `/artifacts/...`. They never call `https://comfy.ai.b1.germering`, `/prompt`, `/ws`, or the server-side native ComfyUI container.

Media inputs are accepted as validated staged-upload JSON, authenticated `/artifacts/...` paths, or base64 media data URLs capped by `B1_AI_HUB_MAX_DATA_URL_BYTES`. Local filesystem paths, arbitrary external URLs, artifact paths with query/fragment data, and arbitrary JSON objects are rejected by the media-reference nodes. `B1 Upload Media Base64` accepts only the server-supported media upload types PNG, JPEG, WebP, GIF, WAV, MP3, Ogg, MP4, and WebM; it normalizes MIME types, validates explicit `image`/`audio`/`video` field MIME families, sanitizes upload filenames, and returns a direct `reference_json` value that can be connected to image-to-image and image-to-video nodes, plus the full `upload_json` wrapper for inspection. Media-job nodes accept both shapes. Job IDs used by wait/cancel/artifact-list nodes are validated as opaque B1 identifiers before they are added to request paths. Vision analysis currently requires an internal artifact path or data URL because it uses the OpenAI-compatible image URL field. `B1 Speech To Text` sends the selected model alias and audio as an OpenAI-style multipart `/v1/audio/transcriptions` request, using a sanitized uploaded filename and no private B1 model header. Artifact downloads are written only under `B1_AI_HUB_DOWNLOAD_DIR` or the default local `b1-artifacts/` directory; requested filenames are sanitized, and traversal, encoded traversal, query strings, and fragments are refused.

Examples are in `examples/`. `tts-fast.non-comfy.workflow.json` is the smoke path for proving an external ComfyUI can use a hosted non-Comfy backend while the server-side B1 ComfyUI container is stopped. It uses `B1 Text To Speech` with the `tts-fast` alias, which resolves to the CPU audio path when the default placeholder profile is installed. The matching live compatibility test can stop and restore the B1 `comfyui` service when run with `B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose`.
