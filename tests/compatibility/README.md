# Compatibility Tests

Compatibility tests will cover native ComfyUI REST/WebSocket clients, the optional legacy `:8188` listener, external ComfyUI B1 remote nodes, Model Hub clients, and Voicebox remote/server mode.

The first concrete remote-node scenario is the non-Comfy smoke path: run `integrations/comfyui-b1-remote-nodes/examples/tts-fast.non-comfy.workflow.json` from an external ComfyUI while the server-side B1 `comfyui` container is stopped. The workflow must complete through the unified API and the `audio-cpu`/`tts-fast` path.

The same path has an opt-in Python compatibility test:

```bash
export B1_REMOTE_NODES_LIVE_TEST=1
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY=...
export B1_AI_HUB_DOWNLOAD_DIR=/tmp/b1-remote-node-output
export B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose
python3 -m unittest tests.compatibility.test_remote_nodes_non_comfy
```

`B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose` makes the test stop the local Compose `comfyui` service before running the remote-node operation and restore it afterward if it was previously running. Use this only during an explicit compatibility window. If the service has already been stopped by another runbook, set `B1_REMOTE_NODES_COMFYUI_STOP_MODE=manual`; the test then relies on that operator-controlled state and does not mutate Compose.
