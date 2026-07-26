# Compatibility Tests

Compatibility tests will cover native ComfyUI REST/WebSocket clients, the optional legacy `:8188` listener, external ComfyUI B1 remote nodes, Model Hub clients, and Voicebox remote/server mode.

## Native ComfyUI Compatibility

The native ComfyUI compatibility path uses the public `comfy.ai.b1.germering` gateway endpoint, not the internal `comfyui` container port. It verifies metadata routes including node-specific `/object_info/{node}`, native image and mask uploads, native prompt submission, `Idempotency-Key` replay of the same native `prompt_id`, native WebSocket events, native history listing and prompt lookup, native queue deletion, targeted interrupt, and `/view` artifact retrieval for every returned history artifact with a real API-format prompt supplied by the operator.

Bootstrap copies editable prompt templates once to `/srv/b1-ai-hub/workflows/acceptance/` and preserves later operator edits. Use `native-comfyui-smoke-prompt.json` only for route-level rehearsals; it emits a deterministic tiny image through the B1 runtime hook and is not model acceptance evidence. Final handoff should use a real prompt such as the edited `text-to-image-api-prompt.json`.

```bash
export B1_NATIVE_COMFYUI_LIVE_TEST=1
export B1_NATIVE_COMFYUI_BASE=https://comfy.ai.b1.germering
export B1_NATIVE_COMFYUI_API_BASE=https://api.ai.b1.germering
export B1_NATIVE_COMFYUI_API_KEY=...
export B1_NATIVE_COMFYUI_PROMPT_FILE=/srv/b1-ai-hub/workflows/acceptance/text-to-image-api-prompt.json
export B1_NATIVE_COMFYUI_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/native-comfyui.json
make native-comfyui-compatibility
```

For temporary IP/host validation, set `B1_NATIVE_COMFYUI_HOST_HEADER=comfy.ai.b1.germering` and `B1_NATIVE_COMFYUI_API_HOST_HEADER=api.ai.b1.germering`. For a Caddy internal CA that is not trusted by the test host yet, set `B1_NATIVE_COMFYUI_CA_FILE=/path/to/root.crt`; use `B1_NATIVE_COMFYUI_TLS_VERIFY=0` only during an explicit LAN validation window. The harness refuses to send `B1_NATIVE_COMFYUI_API_KEY` over plain HTTP or `ws://` unless `B1_ACCEPTANCE_ALLOW_INSECURE_HTTP=true` is set for an isolated development run. `B1_NATIVE_COMFYUI_PROMPT_JSON` may be used instead of `B1_NATIVE_COMFYUI_PROMPT_FILE` for a small inline native prompt.

When `B1_NATIVE_COMFYUI_EVIDENCE` is set, the test writes a machine-readable evidence file with `status`, `required_checks`, per-check records, and redacted route/prompt samples. Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` native ComfyUI evidence file and block handoff if `/object_info`, `/object_info/{node}`, `/system_stats`, `/models`, `/queue`, `/upload/image`, `/upload/mask`, `POST /prompt`, `Idempotency-Key` replay, `/ws`, `/history`, `/history/{prompt_id}`, B1 durable job lookup by native prompt ID, B1 artifact listing/download, `POST /queue` deletion, targeted `POST /interrupt`, or `/view` artifact checks are absent or incomplete. The report also requires native prompt ID correlation, replay header proof, completed native WebSocket events or previews, B1 durable job/artifact IDs, successful queue/interrupt HTTP status, B1 `artifact_proofs[]` entries for every stored artifact, and retrieved `/view` proof entries with byte count, SHA-256, content type, filename, and output key for every returned native history artifact; check names alone are not accepted as proof.

## Optional Legacy ComfyUI Listener

The disabled-by-default legacy listener is only for clients that must use plain `http://host:8188` and cannot set API prefixes or bearer headers. Start it with the explicit legacy Compose profile, keep `B1_LEGACY_COMFY_ALLOW_CIDRS` narrow, then run:

```bash
export B1_LEGACY_COMFY_LIVE_TEST=1
export B1_LEGACY_COMFY_BASE=http://ai.b1.germering:8188
export B1_LEGACY_COMFY_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/legacy-comfy-listener.json
make legacy-comfyui-compatibility
```

The legacy harness intentionally sends no bearer token. The listener strips any client `Authorization`, `Cookie`, or `X-B1-CSRF` headers before forwarding to the scheduler-aware Caddy/control-plane path, then verifies that `/object_info`, `/system_stats`, and `/ws` are reachable. Control Center acceptance reports ingest this optional evidence when present; missing legacy evidence is non-blocking because the listener is disabled by default, but incomplete or stale evidence is reported. It is not a substitute for the authenticated native ComfyUI compatibility evidence required for handoff.

## Remote Nodes Without Server-Side ComfyUI

The first concrete remote-node scenario is the non-Comfy smoke path: run `integrations/comfyui-b1-remote-nodes/examples/tts-fast.non-comfy.workflow.json` from an external ComfyUI while the server-side B1 `comfyui` container is stopped. The workflow must list/select B1 models through the unified API, keep credentials external to workflow JSON, and complete through the `audio-cpu`/`tts-fast` path.

The same path has an opt-in Python compatibility test:

```bash
export B1_REMOTE_NODES_LIVE_TEST=1
install -d -m 0700 ~/.config/b1-ai-hub
install -m 0600 /dev/null ~/.config/b1-ai-hub/comfyui-remote-nodes.key
# Paste the scoped remote-node API key into ~/.config/b1-ai-hub/comfyui-remote-nodes.key.
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY_FILE=~/.config/b1-ai-hub/comfyui-remote-nodes.key
export B1_AI_HUB_DOWNLOAD_DIR=/tmp/b1-remote-node-output
export B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose
export B1_REMOTE_NODES_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/remote-nodes-non-comfy.json
make remote-nodes-non-comfy-compatibility
```

`B1_REMOTE_NODES_COMFYUI_STOP_MODE=docker-compose` makes the test stop the local Compose `comfyui` service before running the remote-node operation and restore it afterward if it was previously running. Use this only during an explicit compatibility window. If the service has already been stopped by another runbook, set `B1_REMOTE_NODES_COMFYUI_STOP_MODE=manual`; the test then avoids local Compose mutations but still verifies the stopped state through `GET /admin/runtimes` runtime-agent service inventory. The API key must include the normal remote-node inference scopes plus `runtimes:read` for that verification.

When `B1_REMOTE_NODES_EVIDENCE` is set, the test writes a machine-readable evidence file with `status`, `required_checks`, per-check records, and redacted model/artifact samples. Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` remote-node evidence file and block handoff if the server-side ComfyUI stop action, runtime-agent stop verification before and after the operation, remote model listing, model-alias selection, credential-externalization, non-Comfy TTS completion, or artifact download checks are absent or incomplete. The report also requires runtime-agent stopped-container proof with zero running ComfyUI containers after the TTS call, the selected visible alias, an external credential source with no workflow-secret findings, explicit `runtime_policy=non_comfy_only`, generated audio byte count and SHA-256, non-placeholder TTS proof from the `X-B1-Placeholder` and `X-B1-Cpu-Audio-Engine` response headers, and a file-backed downloaded-artifact proof with matching stat size, file SHA-256, relative path under the configured download directory, non-symlink status, and private file mode. The scaffold CPU audio engine is development-only and cannot satisfy handoff evidence. `B1_REMOTE_NODES_ALLOW_PLACEHOLDER=1` may be used for a local dry run, but the evidence remains incomplete for acceptance.

## Model Hub Client Sync

The Model Hub compatibility path uses the real `b1-model-client` library against the deployed gateway. It lists the catalog, creates a sync plan for a permitted downloadable model, validates blob `HEAD` metadata including `ETag`, `Content-Length`, checksum, and byte-range support, proves matching `If-None-Match` revalidation returns `304 Not Modified`, seeds a partial blob through HTTP Range, lets the client resume and verify the blob into a temporary managed cache, records the final cache file SHA-256, size, relative path, non-symlink status, private POSIX modes where applicable, and removed `.partial` staging file, checks dry-run prune behavior against an unmanaged local file, and verifies an inference-only model cannot be downloaded.

```bash
export B1_MODELHUB_LIVE_TEST=1
export B1_MODELHUB_URL=https://models.ai.b1.germering
export B1_MODELHUB_TOKEN=...
export B1_MODELHUB_CA_FILE=/path/to/b1-caddy-root.crt
export B1_MODELHUB_SYNC_MODEL=chat-default
export B1_MODELHUB_INFERENCE_ONLY_MODEL=tts-quality
export B1_MODELHUB_ACCEPT_LICENSES=1
export B1_MODELHUB_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/modelhub-client-sync.json
make modelhub-compatibility
```

Set `B1_MODELHUB_CA_FILE` when the test host does not already trust the Caddy internal CA. The harness uses the same hardened transport helpers as `b1-model-client`, including the direct `HEAD` metadata, `If-None-Match`, and Range probes, and refuses to send `B1_MODELHUB_TOKEN` over plain HTTP unless `B1_MODEL_CLIENT_ALLOW_INSECURE_HTTP=true` is set for an isolated development harness. Set `B1_MODELHUB_ACCEPT_LICENSES=1` only after reviewing the sync plan and licence terms for the selected downloadable model. When `B1_MODELHUB_EVIDENCE` is set, Control Center acceptance reports ingest the resulting evidence file and block handoff if catalog access, plan creation, `HEAD` metadata validation, `If-None-Match`/304 revalidation, Range/resume download, managed cache state, safe prune behavior, or inference-only download policy checks are absent or incomplete. The report also requires the check records to include the synced blob SHA-256, expected size, content-addressed `ETag`, checksum, partial and final download sizes, final file SHA-256, managed-state SHA-256 and size, cache-relative `blobs/{sha256}` path under the configured cache root, regular-file/non-symlink proof, removed partial file proof, POSIX private-mode proof when checked, prune safety flag, and inference-only policy action count; check names alone are not accepted as proof.

## Voicebox Remote/Server Compatibility

The Voicebox compatibility path verifies the public `voice.ai.b1.germering` gateway endpoint and the unified `api.ai.b1.germering` speech/profile APIs. It first reads `/b1/runtime/build-info` through the Voicebox compatibility endpoint and checks the deployed B1 proxy version, Jamie Pine Voicebox version, pinned commit, and source archive SHA-256. It then checks native HTTP proxying, Voicebox reference-sample upload protection, profile create/export/delete lifecycle, audit records for sample upload/export/delete, OpenAI-compatible speech through the scheduler, and native WebSocket connection behaviour where the pinned upstream supports it.

```bash
export B1_VOICEBOX_LIVE_TEST=1
export B1_VOICEBOX_BASE=https://voice.ai.b1.germering
export B1_VOICEBOX_API_BASE=https://api.ai.b1.germering
export B1_VOICEBOX_API_KEY=...
export B1_VOICEBOX_SPEECH_MODEL=tts-quality
export B1_VOICEBOX_EXPECTED_VERSION=v0.5.0
export B1_VOICEBOX_EXPECTED_COMMIT=2bcb98d1a8b6fe05e15fbc1559e3085669e4035d
export B1_VOICEBOX_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/voicebox-remote.json
make voicebox-compatibility
```

For temporary IP/host validation, set `B1_VOICEBOX_HOST_HEADER=voice.ai.b1.germering` and `B1_VOICEBOX_API_HOST_HEADER=api.ai.b1.germering`. For a Caddy internal CA that is not trusted by the test host yet, set `B1_VOICEBOX_CA_FILE=/path/to/root.crt`; use `B1_VOICEBOX_TLS_VERIFY=0` only during an explicit LAN validation window. The harness refuses to send `B1_VOICEBOX_API_KEY` or `B1_VOICEBOX_NATIVE_API_KEY` over plain HTTP or `ws://` unless `B1_ACCEPTANCE_ALLOW_INSECURE_HTTP=true` is set for an isolated development run.

If the pinned Voicebox upstream version does not support a specific remote surface, record that limitation explicitly instead of treating the check as skipped:

```bash
export B1_VOICEBOX_SKIP_WEBSOCKET=1
export B1_VOICEBOX_WEBSOCKET_LIMITATION="Pinned Voicebox v0.5.0 does not expose a stable remote WebSocket route for this mode."
```

`B1_VOICEBOX_SKIP_SPEECH=1` similarly requires `B1_VOICEBOX_SPEECH_LIMITATION`, but use it only when speech is blocked by a pinned upstream/version limitation rather than missing model installation or bad credentials. Limitation records must carry the same proxy version, upstream repository/version/commit, and source archive SHA-256 returned by `/b1/runtime/build-info`; the acceptance report rejects environment-only or mismatched limitation evidence. When `B1_VOICEBOX_EVIDENCE` is set, Control Center acceptance reports ingest the resulting evidence file and block handoff if proxy build-info proof, native HTTP proxying, profile lifecycle validation, reference-sample artifact protection, profile export validation, delete audit proof, speech-or-limitation proof, or WebSocket-or-limitation proof is absent or incomplete. The report also requires pinned proxy/upstream/source metadata, profile ID/model/sample counts, protected sample ID/URL/bytes/SHA-256/MIME type, fetched profile sample URL/hash matching the upload, exported profile sample URL/hash matching the protected sample, export format and sensitivity flag, delete status, and audit proof that upload/export/delete events targeted the expected sample/profile while redacting sample arrays. Speech evidence must include generated bytes/hash/content type or a pinned-upstream limitation tied to the deployed proxy. WebSocket evidence likewise must include either a validated route/result type or a pinned-upstream limitation tied to the deployed proxy.

After the native ComfyUI prompt, remote-node credentials, Model Hub sync client, and Voicebox settings are configured, run the required external compatibility evidence group with:

```bash
make external-compatibility-acceptance
```

The optional legacy listener remains separate because it is disabled by default and should only be enabled during a narrow legacy-client compatibility window.
