# Smoke Tests

Smoke tests are opt-in live checks against a running B1 AI Hub deployment. By default they skip so normal unit validation never mutates a local stack.

Run the first live path with a scoped key that has `models:read`, `jobs:read`, and `jobs:write`:

```bash
export B1_SMOKE_LIVE_TEST=1
export B1_AI_HUB_API_BASE=https://api.ai.b1.germering
export B1_AI_HUB_API_KEY=...
export B1_SMOKE_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/live-smoke.json
python3 -m unittest discover -s tests/smoke -v
```

Or through Make:

```bash
B1_SMOKE_LIVE_TEST=1 B1_AI_HUB_API_KEY=... make smoke
```

When testing through `https://127.0.0.1` or another temporary address, set the routed host explicitly:

```bash
export B1_AI_HUB_API_BASE=https://127.0.0.1
export B1_SMOKE_HOST_HEADER=api.ai.b1.germering
```

For the default Caddy internal CA, either trust the generated root certificate on the test machine or pass it directly:

```bash
export B1_SMOKE_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
```

For temporary lab runs only, TLS verification can be disabled:

```bash
export B1_SMOKE_TLS_VERIFY=0
```

The live suite currently checks:

- gateway/control-plane `/healthz`
- authenticated `/v1/models`
- async TTS media-job creation through `tts-fast`
- terminal job polling
- SSE job event delivery
- artifact listing and authenticated artifact download
- optional `/admin/self-test` when `B1_SMOKE_ADMIN_API_KEY` has sufficient scope

When `B1_SMOKE_EVIDENCE` is set, the suite writes a machine-readable `b1-ai-hub-live-smoke/v1` evidence file with `status`, `required_checks`, per-check records, and redacted samples. Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` smoke evidence file and block handoff if health, model listing, async TTS completion, SSE events, or artifact download checks are absent or incomplete.

Useful knobs:

- `B1_SMOKE_TTS_MODEL`, default `tts-fast`
- `B1_SMOKE_TTS_RUNTIME_POLICY`, default `non_comfy_only`
- `B1_SMOKE_TTS_VOICE`, default `default`
- `B1_SMOKE_EVIDENCE`, optional machine-readable handoff evidence path
- `B1_SMOKE_JOB_TIMEOUT_SECONDS`, default `120`
- `B1_SMOKE_HTTP_TIMEOUT_SECONDS`, default `10`
