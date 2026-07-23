# B1 Remote Node Examples

These examples are lightweight workflow sketches for an external ComfyUI installation with `comfyui-b1-remote-nodes` installed.

- `tts-fast.non-comfy.workflow.json` submits `B1 Text To Speech` against the `tts-fast` alias. This is the default smoke path for proving the external ComfyUI nodes call the unified API and do not require the server-side B1 ComfyUI runtime.
- `text-to-image.async.workflow.json` submits an asynchronous `image-default` job, waits for completion, lists artifacts, and downloads the generated artifact through the authenticated `/artifacts/...` route.

Set `B1_AI_HUB_API_BASE`, `B1_AI_HUB_API_KEY`, and optionally `B1_AI_HUB_DOWNLOAD_DIR` in the external ComfyUI process environment before loading the examples.
