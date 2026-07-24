# ComfyUI Compatibility

Native ComfyUI traffic is exposed at `https://comfy.ai.b1.germering/` through the control-plane compatibility proxy, not directly to the ComfyUI backend.

Normal `https://comfy.ai.b1.germering/` requests must authenticate with a B1 bearer token. Read-only native routes such as `/object_info`, `/system_stats`, `/models`, `/history`, `/view`, and `/ws` require `jobs:read`; mutating routes such as `POST /prompt`, `/queue`, `/interrupt`, uploads, and approved custom-node API routes require `jobs:write`. The gateway stamps the managed compatibility header for this virtual host and strips client-supplied `X-B1-Compatibility` on normal API/model hosts so clients cannot spoof the legacy path. The control plane removes `Authorization` and the compatibility marker before forwarding to the ComfyUI runtime.

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

The compatibility catch-all is policy-gated before proxying. `GET`, `HEAD`, and `OPTIONS` routes pass for native metadata, frontend assets, previews, and future read-only ComfyUI routes unless they match a blocked management prefix. Mutating routes are limited to the core native API required for normal clients: `/queue`, `/interrupt`, `/upload/image`, `/upload/mask`, and `/api/userdata`. Internal B1 lifecycle hooks under `/b1/runtime`, ComfyUI Manager/custom-node management prefixes, and mutating routes containing install/update/pip/git/snapshot management tokens are rejected before the request reaches ComfyUI. If an approved custom node exposes a required mutating API, add its route prefix to both `B1_COMFYUI_TRUSTED_ROUTE_PREFIXES` and `allowed_route_prefixes` on the exact approved `(node id, commit)` entry in `workflows/approved-node-pins.json`. The proxy forwards a custom mutating route only when the request path matches both allowlists.

`POST /prompt` is scheduler-aware. The control plane creates a durable job record, waits up to `B1_COMFY_PROMPT_WAIT_TIMEOUT_SECONDS` for the global GPU lease, records the scheduler admission states, asks runtime-agent to unload other GPU runtimes, verifies the VRAM reserve, calls ComfyUI's `/b1/runtime/load` and `/b1/runtime/warm` hooks when present, forwards the original JSON body unchanged to native ComfyUI only after preparation succeeds, and stores the returned native `prompt_id` on the job as `native_prompt_id`. The production ComfyUI image installs those hooks as a B1 custom-node route package. Optional missing/unreachable hooks are tolerated for non-production images, but explicit failure-like hook statuses or HTTP errors fail preparation. Explicit hook failures return HTTP 503 with `detail.type=runtime_prepare_failed`, `detail.runtime=comfyui`, and `detail.public_model=comfyui-native`. A background tracker renews the same lease while polling `/history/{prompt_id}`, marks the ComfyUI runtime idle when native history confirms completion or cancellation, also records idle state if a WebSocket event has already made the job terminal, and releases the lease after the native history entry appears plus `B1_COMFY_PROMPT_IDLE_GRACE_SECONDS`. If the prompt cannot acquire the lease or runtime preparation fails, the request is not forwarded and the durable job is marked failed.

`/ws` is a bidirectional bridge to the internal ComfyUI WebSocket endpoint. It preserves the query string used by native clients for client IDs and forwards text and binary messages in both directions, including progress events and binary previews. Browser/API credentials and WebSocket handshake headers are not forwarded to the internal runtime. Native text events are also parsed by `prompt_id` where possible so matching durable jobs show `execution_start`, `progress`, `executing`, `executed`, and `execution_error` state in `/v1/media/jobs/{job_id}` and the SSE job event stream.

Native `/interrupt` and mutating `/queue` requests are forwarded to ComfyUI immediately, then mirrored into B1 job state when ComfyUI accepts them. `/interrupt` marks every active ComfyUI-native durable job as cancelling or cancelled. `/queue` requests with native prompt IDs in `delete`, `cancel`, `prompt_id`, or `prompt_ids` mark only matching jobs; explicit clear-all queue payloads mark all active ComfyUI-native jobs. This keeps native clients compatible while making cancellation visible in Control Center, `/v1/media/jobs/{job_id}`, and job event streams.

When ComfyUI reports image, video, GIF, or audio outputs through live `executed` events or `/history/{prompt_id}`, the control plane records authenticated `/artifacts/comfyui/...` artifact references on the durable job. Live WebSocket `executed` events now stream each referenced output from internal ComfyUI `/view` into `$B1_ARTIFACT_ROOT/comfyui/...`, hash it, record byte count and SHA-256 metadata, and mark the artifact as `source=artifact_store` before the job is updated. The history tracker repeats the same ingest step at completion, so a failed live ingest can still be retried from `/history/{prompt_id}`. If final history-time ingestion fails, the job enters `recovery_required` with `failure_category=artifact_ingest_failed` and retains the ComfyUI `/view` metadata for diagnosis or retry. The normal artifact endpoint serves stored files through the internal artifact-server after `jobs:read` owner/scope checks.

The live native compatibility harness also fetches one generated artifact through the public `/view` route and records it as `view_artifact_accessible` evidence. Acceptance reports block handoff if this proof is missing.

Queued `/v1/media/jobs` resolved to the `comfyui` runtime use the same global GPU lease but are not transparent passthrough. They submit a native payload supplied as `input.comfyui_payload`, `input.comfyui_prompt`, `input.native_prompt`, `input.workflow_json`, or from a published workflow's non-empty `workflow_json`; optional `comfyui_parameter_mappings` on the published workflow can write validated `input.parameters` values into graph paths, and simple placeholders can also be filled before submission. These jobs record the native prompt ID, poll native history, ingest outputs from `/view`, and then serve artifacts through the normal authenticated artifact endpoint.

Published workflows that require custom ComfyUI nodes must declare `node` dependencies pinned to exact git commits. The control plane validates those pins against `/opt/b1/workflows/approved-node-pins.json` through `B1_COMFYUI_NODE_PIN_REGISTRY`; unapproved, disabled, superseded, or missing pins keep the workflow in `needs_dependencies` instead of treating arbitrary custom-node code as ready. Approved pin records may include `allowed_route_prefixes` for audited mutating custom APIs; leave the list empty for nodes that only contribute graph nodes or read-only metadata.

The optional legacy `:8188` listener is disabled by default. The base Caddyfile does not define the listener and the base Compose project does not bind `8188`.

Enable the legacy listener only for clients that cannot use `https://comfy.ai.b1.germering/`:

```bash
docker compose -f compose.yaml -f compose.legacy-comfy.yaml --profile legacy-comfy up -d gateway
```

The override uses `deploy/caddy/Caddyfile.legacy-comfy`, terminates at the control-plane compatibility proxy, and applies `B1_LEGACY_COMFY_ALLOW_CIDRS`. It never proxies directly to the ComfyUI backend. This listener is the only ComfyUI compatibility path that may omit bearer authentication; use it only for clients that cannot set headers, keep the allowlist narrow, and prefer the authenticated HTTPS virtual host whenever possible.
