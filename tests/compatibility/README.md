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
export B1_REMOTE_NODES_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/remote-nodes-non-comfy.json
python3 -m unittest tests.compatibility.test_remote_nodes_non_comfy
```

`B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose` makes the test stop the local Compose `comfyui` service before running the remote-node operation and restore it afterward if it was previously running. Use this only during an explicit compatibility window. If the service has already been stopped by another runbook, set `B1_REMOTE_NODES_COMFYUI_STOP_MODE=manual`; the test then relies on that operator-controlled state and does not mutate Compose.

When `B1_REMOTE_NODES_EVIDENCE` is set, the test writes a machine-readable evidence file with `status`, `required_checks`, per-check records, and a redacted artifact sample. Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` remote-node evidence file and block handoff if the server-side ComfyUI stop, non-Comfy TTS completion, or artifact download checks are absent or incomplete.

## Model Hub Client Sync

The Model Hub compatibility path uses the real `b1-model-client` library against the deployed gateway. It lists the catalog, creates a sync plan for a permitted downloadable model, seeds a partial blob through HTTP Range, lets the client resume and verify the blob into a temporary managed cache, checks dry-run prune behavior against an unmanaged local file, and verifies an inference-only model cannot be downloaded.

```bash
export B1_MODELHUB_LIVE_TEST=1
export B1_MODELHUB_URL=https://models.ai.b1.germering
export B1_MODELHUB_TOKEN=...
export B1_MODELHUB_SYNC_MODEL=chat-default
export B1_MODELHUB_INFERENCE_ONLY_MODEL=tts-quality
export B1_MODELHUB_ACCEPT_LICENSES=1
export B1_MODELHUB_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/modelhub-client-sync.json
python3 -m unittest tests.compatibility.test_modelhub_client_sync
```

Set `B1_MODELHUB_ACCEPT_LICENSES=1` only after reviewing the sync plan and licence terms for the selected downloadable model. When `B1_MODELHUB_EVIDENCE` is set, Control Center acceptance reports ingest the resulting evidence file and block handoff if catalog access, plan creation, Range/resume download, managed cache state, safe prune behavior, or inference-only download policy checks are absent or incomplete.
