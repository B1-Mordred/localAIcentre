# ComfyUI Compatibility

Native ComfyUI traffic is exposed at `https://comfy.ai.b1.germering/` through the control-plane compatibility proxy, not directly to the ComfyUI backend.

The development Compose topology uses a mock ComfyUI service. Run the production native ComfyUI runtime with:

```bash
docker compose -f compose.yaml -f compose.production-comfyui.yaml up -d
```

That override points the control plane at `http://comfyui:8188`, while the backend port remains internal-only.

The proxy preserves the native path layout for:

- `POST /prompt`
- `/ws`
- `/history`
- `/queue`
- `/upload/image`
- `/view`
- `/object_info`
- `/system_stats`
- `/models`

Execution requests must acquire the global GPU lease before being forwarded. Metadata, upload, and view routes can pass without a GPU lease.

`POST /prompt` is scheduler-aware. The control plane creates a durable job record, waits up to `B1_COMFY_PROMPT_WAIT_TIMEOUT_SECONDS` for the global GPU lease, records the scheduler admission states, asks runtime-agent to unload other GPU runtimes, verifies the VRAM reserve, calls ComfyUI's optional `/b1/runtime/load` and `/b1/runtime/warm` hooks, forwards the original JSON body unchanged to native ComfyUI only after preparation succeeds, and stores the returned native `prompt_id` on the job as `native_prompt_id`. Optional missing/unreachable hooks are tolerated, but explicit failure-like hook statuses or HTTP errors fail preparation. Explicit hook failures return HTTP 503 with `detail.type=runtime_prepare_failed`, `detail.runtime=comfyui`, and `detail.public_model=comfyui-native`. A background tracker renews the same lease while polling `/history/{prompt_id}`, marks the ComfyUI runtime idle when native history confirms completion or cancellation, and releases the lease after the native history entry appears plus `B1_COMFY_PROMPT_IDLE_GRACE_SECONDS`. If the prompt cannot acquire the lease or runtime preparation fails, the request is not forwarded and the durable job is marked failed.

`/ws` is a bidirectional bridge to the internal ComfyUI WebSocket endpoint. It preserves the query string used by native clients for client IDs and forwards text and binary messages in both directions, including progress events and binary previews. Browser/API credentials and WebSocket handshake headers are not forwarded to the internal runtime. Native text events are also parsed by `prompt_id` where possible so matching durable jobs show `execution_start`, `progress`, `executing`, `executed`, and `execution_error` state in `/v1/media/jobs/{job_id}` and the SSE job event stream.

When ComfyUI reports image, video, GIF, or audio outputs through live `executed` events or `/history/{prompt_id}`, the control plane records authenticated `/artifacts/comfyui/...` artifact references on the durable job. Live WebSocket `executed` events now stream each referenced output from internal ComfyUI `/view` into `$B1_ARTIFACT_ROOT/comfyui/...`, hash it, record byte count and SHA-256 metadata, and mark the artifact as `source=artifact_store` before the job is updated. The history tracker repeats the same ingest step at completion, so a failed live ingest can still be retried from `/history/{prompt_id}`. If final history-time ingestion fails, the job enters `recovery_required` with `failure_category=artifact_ingest_failed` and retains the ComfyUI `/view` metadata for diagnosis or retry. The normal artifact endpoint serves stored files through the internal artifact-server after `jobs:read` owner/scope checks.

Queued `/v1/media/jobs` resolved to the `comfyui` runtime use the same global GPU lease but are not transparent passthrough. They submit a native payload supplied as `input.comfyui_payload`, `input.comfyui_prompt`, `input.native_prompt`, `input.workflow_json`, or from a published workflow's non-empty `workflow_json`; optional `comfyui_parameter_mappings` on the published workflow can write validated `input.parameters` values into graph paths, and simple placeholders can also be filled before submission. These jobs record the native prompt ID, poll native history, ingest outputs from `/view`, and then serve artifacts through the normal authenticated artifact endpoint.

Published workflows that require custom ComfyUI nodes must declare `node` dependencies pinned to exact git commits. The control plane validates those pins against `/opt/b1/workflows/approved-node-pins.json` through `B1_COMFYUI_NODE_PIN_REGISTRY`; unapproved, disabled, superseded, or missing pins keep the workflow in `needs_dependencies` instead of treating arbitrary custom-node code as ready.

The optional legacy `:8188` listener is disabled by default. The base Caddyfile does not define the listener and the base Compose project does not bind `8188`.

Enable the legacy listener only for clients that cannot use `https://comfy.ai.b1.germering/`:

```bash
docker compose -f compose.yaml -f compose.legacy-comfy.yaml --profile legacy-comfy up -d gateway
```

The override uses `deploy/caddy/Caddyfile.legacy-comfy`, terminates at the control-plane compatibility proxy, and applies `B1_LEGACY_COMFY_ALLOW_CIDRS`. It never proxies directly to the ComfyUI backend.
