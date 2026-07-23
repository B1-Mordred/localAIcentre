# Voicebox

Voicebox is an optional managed runtime. It is routed at `https://voice.ai.b1.germering/` through the gateway while the backend remains internal.

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
- native Voicebox WebSocket compatibility validation, model-specific voice-cloning execution, and pinned upstream packaging remain open implementation work

The first Compose slice includes a `voicebox` profile with a placeholder runtime. It must be replaced with a pinned tested upstream release before production use.
