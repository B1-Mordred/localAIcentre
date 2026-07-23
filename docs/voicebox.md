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
- native Voicebox WebSocket compatibility validation and model-specific voice-cloning execution remain open implementation work

The production override builds Jamie Pine Voicebox `v0.5.0` at commit `2bcb98d1a8b6fe05e15fbc1559e3085669e4035d`, exposes upstream REST, web UI, and MCP HTTP surfaces on the internal native port `17493`, and maps voice data/cache/model views into B1-managed paths. The default Compose file still keeps the lightweight placeholder so `docker compose up -d` remains small. Runtime model downloads are disabled by default with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`; Model Hub must prepare compatible runtime views before enabling a Voicebox engine/profile.

Do not add `voicebox` to `B1_RUNTIME_PRODUCTION_REQUIRED` until the selected engine/profile has passed target-host smoke tests, voice profile backup/export/delete validation, and GPU lease overlap checks against LocalAI and ComfyUI.
