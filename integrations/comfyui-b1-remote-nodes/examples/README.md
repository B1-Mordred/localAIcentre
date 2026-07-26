# B1 Remote Node Examples

These examples are lightweight workflow sketches for an external ComfyUI installation with `comfyui-b1-remote-nodes` installed.

- `tts-fast.non-comfy.workflow.json` submits `B1 Text To Speech` against the `tts-fast` alias. This is the default smoke path for proving the external ComfyUI nodes call the unified API and do not require the server-side B1 ComfyUI runtime.
- `text-to-image.async.workflow.json` submits an asynchronous `image-default` job, waits for completion, lists artifacts, and downloads the generated artifact through the authenticated `/artifacts/...` route.
- `all-modalities.reference.workflow.json` is a broad credential-free reference sketch that includes every B1 remote-node class for package-surface validation. It is not the lightweight smoke path; use it as a template library and edit aliases, parameters, and uploads for real workflows.

Set `B1_AI_HUB_API_BASE`, `B1_AI_HUB_API_KEY_FILE`, and optionally `B1_AI_HUB_CA_FILE` and `B1_AI_HUB_DOWNLOAD_DIR` in the external ComfyUI process environment before loading the examples. Alternatively, place a private JSON config at `~/.config/b1-ai-hub/comfyui-remote-nodes.json` with `api_key_file` pointing at a private key file and `ca_file` pointing at the exported Caddy root certificate, or point `B1_AI_HUB_CONFIG_FILE` at another path; do not store API keys in workflow JSON. Inline `B1_AI_HUB_API_KEY` remains supported for short-lived development runs.
