# Voicebox

Voicebox is an optional managed runtime. It is routed at `https://voice.ai.b1.germering/` through the gateway while the backend remains internal.

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
- empty speech responses are treated as `recovery_required`, not successful placeholder output
- administrators and operators can list, create, update, export, and soft-delete Voicebox profiles through Control Center and `/admin/voicebox/profiles`
- profile records are durable PostgreSQL rows included in the logical backup export; reference samples and cloned-voice material are stored only as artifact references, not inline profile payloads
- the production image includes a B1 proxy that forwards native REST/web/MCP HTTP and WebSocket traffic to loopback upstream Voicebox while exposing scheduler lifecycle hooks on `/b1/runtime/*`
- model-specific voice-cloning execution and target-host WebSocket compatibility validation remain open implementation work

The production override builds Jamie Pine Voicebox `v0.5.0` at commit `2bcb98d1a8b6fe05e15fbc1559e3085669e4035d`, exposes the B1 proxy on the internal native port `17493`, starts upstream Voicebox on loopback `127.0.0.1:17494`, and maps voice data/cache/model views into B1-managed paths. The default Compose file still keeps the lightweight placeholder so `docker compose up -d` remains small. Runtime model downloads are disabled by default with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`; Model Hub must prepare compatible runtime views before enabling a Voicebox engine/profile.

The lifecycle hooks are conservative. `load` checks model files/directories visible to Voicebox and returns `unconfirmed` because upstream engine/profile selection performs final validation. `warm` and `smoke` do not synthesize audio unless explicitly enabled with `B1_VOICEBOX_HOOK_WARM_ENABLED=true` or `B1_VOICEBOX_HOOK_SMOKE_ENABLED=true`. `unload` restarts the loopback upstream process only when the proxy has no active native requests, giving the GPU scheduler a bounded cleanup mechanism without terminating the container.

Do not add `voicebox` to `B1_RUNTIME_PRODUCTION_REQUIRED` until the selected engine/profile has passed target-host smoke tests, voice profile backup/export/delete validation, and GPU lease overlap checks against LocalAI and ComfyUI.
