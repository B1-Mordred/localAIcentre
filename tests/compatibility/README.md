# Compatibility Tests

Compatibility tests will cover native ComfyUI REST/WebSocket clients, the optional legacy `:8188` listener, external ComfyUI B1 remote nodes, Model Hub clients, and Voicebox remote/server mode.

## Native ComfyUI Compatibility

The native ComfyUI compatibility path uses the public `comfy.ai.b1.germering` gateway endpoint, not the internal `comfyui` container port. It verifies metadata routes, native image and mask uploads, native prompt submission, native WebSocket events, native history lookup, native queue deletion, targeted interrupt, and `/view` artifact retrieval with a real API-format prompt supplied by the operator.

```bash
export B1_NATIVE_COMFYUI_LIVE_TEST=1
export B1_NATIVE_COMFYUI_BASE=https://comfy.ai.b1.germering
export B1_NATIVE_COMFYUI_API_KEY=...
export B1_NATIVE_COMFYUI_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json
export B1_NATIVE_COMFYUI_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/native-comfyui.json
python3 -m unittest tests.compatibility.test_native_comfyui_compatibility
```

For temporary IP/host validation, set `B1_NATIVE_COMFYUI_HOST_HEADER=comfy.ai.b1.germering`. For a Caddy internal CA that is not trusted by the test host yet, set `B1_NATIVE_COMFYUI_CA_FILE=/path/to/root.crt`; use `B1_NATIVE_COMFYUI_TLS_VERIFY=0` only during an explicit LAN validation window. `B1_NATIVE_COMFYUI_PROMPT_JSON` may be used instead of `B1_NATIVE_COMFYUI_PROMPT_FILE` for a small inline native prompt.

When `B1_NATIVE_COMFYUI_EVIDENCE` is set, the test writes a machine-readable evidence file with `status`, `required_checks`, per-check records, and redacted route/prompt samples. Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` native ComfyUI evidence file and block handoff if `/object_info`, `/system_stats`, `/models`, `/queue`, `/upload/image`, `/upload/mask`, `POST /prompt`, `/ws`, `/history/{prompt_id}`, `POST /queue` deletion, targeted `POST /interrupt`, or `/view` artifact checks are absent or incomplete.

## Optional Legacy ComfyUI Listener

The disabled-by-default legacy listener is only for clients that must use plain `http://host:8188` and cannot set API prefixes or bearer headers. Start it with the explicit legacy Compose profile, keep `B1_LEGACY_COMFY_ALLOW_CIDRS` narrow, then run:

```bash
export B1_LEGACY_COMFY_LIVE_TEST=1
export B1_LEGACY_COMFY_BASE=http://ai.b1.germering:8188
export B1_LEGACY_COMFY_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/legacy-comfy-listener.json
python3 -m unittest tests.compatibility.test_legacy_comfyui_listener
```

The legacy harness intentionally sends no bearer token. It verifies that `/object_info`, `/system_stats`, and `/ws` are reachable through the scheduler-aware Caddy/control-plane path. The evidence file is an operator artifact for optional legacy-client validation; it is not a substitute for the authenticated native ComfyUI compatibility evidence required for handoff.

## Remote Nodes Without Server-Side ComfyUI

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

## Voicebox Remote/Server Compatibility

The Voicebox compatibility path verifies the public `voice.ai.b1.germering` gateway endpoint and the unified `api.ai.b1.germering` speech/profile APIs. It checks native HTTP proxying, Voicebox profile create/export/delete lifecycle, OpenAI-compatible speech through the scheduler, and native WebSocket connection behaviour where the pinned upstream supports it.

```bash
export B1_VOICEBOX_LIVE_TEST=1
export B1_VOICEBOX_BASE=https://voice.ai.b1.germering
export B1_VOICEBOX_API_BASE=https://api.ai.b1.germering
export B1_VOICEBOX_API_KEY=...
export B1_VOICEBOX_SPEECH_MODEL=tts-quality
export B1_VOICEBOX_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/voicebox-remote.json
python3 -m unittest tests.compatibility.test_voicebox_remote
```

For temporary IP/host validation, set `B1_VOICEBOX_HOST_HEADER=voice.ai.b1.germering` and `B1_VOICEBOX_API_HOST_HEADER=api.ai.b1.germering`. For a Caddy internal CA that is not trusted by the test host yet, set `B1_VOICEBOX_CA_FILE=/path/to/root.crt`; use `B1_VOICEBOX_TLS_VERIFY=0` only during an explicit LAN validation window.

If the pinned Voicebox upstream version does not support a specific remote surface, record that limitation explicitly instead of treating the check as skipped:

```bash
export B1_VOICEBOX_SKIP_WEBSOCKET=1
export B1_VOICEBOX_WEBSOCKET_LIMITATION="Pinned Voicebox v0.5.0 does not expose a stable remote WebSocket route for this mode."
```

`B1_VOICEBOX_SKIP_SPEECH=1` similarly requires `B1_VOICEBOX_SPEECH_LIMITATION`, but use it only when speech is blocked by a pinned upstream/version limitation rather than missing model installation or bad credentials. When `B1_VOICEBOX_EVIDENCE` is set, Control Center acceptance reports ingest the resulting evidence file and block handoff if native HTTP proxying, profile lifecycle validation, speech-or-limitation proof, or WebSocket-or-limitation proof is absent or incomplete.
