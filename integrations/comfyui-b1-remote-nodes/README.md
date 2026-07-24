# comfyui-b1-remote-nodes

This package provides ComfyUI custom nodes that call the unified B1 AI Hub API. They do not call the server-side native ComfyUI endpoint and must not store API credentials inside workflow JSON. Media-reference inputs accept staged B1 upload JSON, authenticated `/artifacts/...` paths, or bounded data URLs; they reject local filesystem paths and arbitrary external URLs.

Install by copying or cloning this directory into the external ComfyUI `custom_nodes/` directory, then restart that ComfyUI instance.

Configure credentials through environment variables or ComfyUI server settings. Do not put keys in workflow JSON:

```bash
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY=...
export B1_AI_HUB_DOWNLOAD_DIR=/path/to/comfyui/output/b1-ai-hub
```

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

Media inputs are accepted as staged-upload JSON, `/artifacts/...` URLs, data URLs, or HTTP(S) URLs. Local filesystem paths are rejected by the media-reference nodes. `B1 Speech To Text` sends the selected model alias and audio as an OpenAI-style multipart `/v1/audio/transcriptions` request, using a sanitized uploaded filename and no private B1 model header. Artifact downloads are written only under `B1_AI_HUB_DOWNLOAD_DIR` or the default local `b1-artifacts/` directory; requested filenames are sanitized and path traversal is refused.

Examples are in `examples/`. `tts-fast.non-comfy.workflow.json` is the smoke path for proving an external ComfyUI can use a hosted non-Comfy backend while the server-side B1 ComfyUI container is stopped. It uses `B1 Text To Speech` with the `tts-fast` alias, which resolves to the CPU audio path when the default placeholder profile is installed. The matching live compatibility test can stop and restore the B1 `comfyui` service when run with `B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose`.
