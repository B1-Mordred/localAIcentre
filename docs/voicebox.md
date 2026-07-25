# Voicebox

Voicebox is an optional managed runtime. It is routed at `https://voice.ai.b1.germering/` through the gateway while the backend remains internal.

The normal Voicebox compatibility host requires a B1 bearer token before the control plane proxies native HTTP or WebSocket traffic. Read-only native requests require `jobs:read`; mutating native requests require `jobs:write`. The control plane strips `Authorization`, cookies, CSRF/session headers, forwarded-client-IP headers, hop-by-hop headers, and WebSocket handshake headers before forwarding to the internal Voicebox proxy, so upstream Voicebox never receives B1 API keys or browser credentials.

Start the pinned production server/web/API build with:

```bash
docker compose -f compose.yaml -f compose.production-voicebox.yaml --profile voicebox up -d
```

Requirements for the pinned Voicebox integration:

- preserve native remote/server API and WebSocket behaviour
- register engines, models, and voice profiles in Control Center
- route GPU work through the global lease
- protect reference samples and cloned voices as sensitive user data
- expose compatible `/v1/audio/speech` semantics where validated

Current implementation status:

- queued `tts/speech` jobs resolved to `voicebox` acquire the global GPU lease, call internal `/v1/audio/speech`, and store returned audio as durable artifacts
- synchronous OpenAI-compatible `POST /v1/audio/speech` resolves the public alias, rewrites it to the immutable model ID, and proxies Voicebox requests under a scheduler lease
- synchronous speech can use a B1 voice profile ID in `voice`, `voice_profile`, or `voice_profile_id`; if `model` is omitted, the active profile's `model_alias` selects the runtime/model
- queued Voicebox speech jobs revalidate the referenced profile before submission, then forward the same bounded profile envelope under the GPU lease
- empty speech responses are treated as `recovery_required`, not successful placeholder output
- administrators and operators can list, create, update, export, and soft-delete Voicebox profiles through Control Center and `/admin/voicebox/profiles`
- Control Center exposes common safe upstream selector metadata fields, including `upstream_voice_id`, `upstream_speaker_id`, `language`, `style`, and `speed`, while retaining the metadata JSON editor for reviewed engine-specific fields
- administrators and operators can upload bounded audio reference samples through Control Center or `POST /admin/voicebox/sample-artifacts`; Storage can dry-run and clean unreferenced Voicebox samples without touching profile-referenced files
- profile records are durable PostgreSQL rows included in the logical backup export; reference samples and cloned-voice material are stored only as artifact references, not inline profile payloads
- the production image includes a B1 proxy that forwards native REST/web/MCP HTTP and WebSocket traffic to loopback upstream Voicebox while exposing scheduler lifecycle hooks on `/b1/runtime/*`
- for `/v1/audio/speech`, that proxy consumes the B1 profile envelope, strips B1-only fields, maps safe upstream selector metadata such as `upstream_voice`, and translates validated `/artifacts/voicebox/...` sample references to read-only in-container paths for engines that support reference or cloned voices
- target-host WebSocket and engine-specific speech compatibility are covered by the opt-in compatibility harness and must either pass against the pinned upstream route or record an explicit pinned-upstream limitation

The production override builds Jamie Pine Voicebox `v0.5.0` at commit `2bcb98d1a8b6fe05e15fbc1559e3085669e4035d`, exposes the B1 proxy on the internal native port `17493`, starts upstream Voicebox on loopback `127.0.0.1:17494`, and maps voice data/cache/model views plus the read-only Voicebox artifact namespace into B1-managed paths. The default Compose file still keeps the lightweight placeholder so `docker compose up -d` remains small. Runtime model downloads are disabled by default with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`; Model Hub must prepare compatible runtime views before enabling a Voicebox engine/profile.

The lifecycle hooks are conservative. `load` checks model files/directories visible to Voicebox and returns `unconfirmed` because upstream engine/profile selection performs final validation. `warm` and `smoke` do not synthesize audio unless explicitly enabled with `B1_VOICEBOX_HOOK_WARM_ENABLED=true` or `B1_VOICEBOX_HOOK_SMOKE_ENABLED=true`. `unload` restarts the loopback upstream process only when the proxy has no active native requests, giving the GPU scheduler a bounded cleanup mechanism without terminating the container.

Do not add `voicebox` to `B1_RUNTIME_PRODUCTION_REQUIRED` until the selected engine/profile has passed target-host smoke tests, voice profile backup/export/delete validation, and GPU lease overlap checks against LocalAI and ComfyUI.

Run the remote/server compatibility harness during target-host acceptance:

```bash
B1_VOICEBOX_LIVE_TEST=1 \
B1_VOICEBOX_BASE=https://voice.ai.b1.germering \
B1_VOICEBOX_API_BASE=https://api.ai.b1.germering \
B1_VOICEBOX_API_KEY=... \
B1_VOICEBOX_SPEECH_MODEL=tts-quality \
B1_VOICEBOX_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/voicebox-remote.json \
python3 -m unittest tests.compatibility.test_voicebox_remote
```

If pinned upstream Voicebox lacks a stable remote WebSocket route or compatible speech mode for the selected profile, set `B1_VOICEBOX_SKIP_WEBSOCKET=1` with `B1_VOICEBOX_WEBSOCKET_LIMITATION=...` or `B1_VOICEBOX_SKIP_SPEECH=1` with `B1_VOICEBOX_SPEECH_LIMITATION=...`. Those limitation strings become machine-readable acceptance evidence; missing models, unhealthy containers, or credentials are not valid upstream limitations.
