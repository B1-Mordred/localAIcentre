# Integration Tests

Integration tests are opt-in live checks against a running B1 AI Hub deployment. They are skipped by default.

Run live integration checks with an administrator/operator key that has `runtimes:read`:

```bash
export B1_INTEGRATION_LIVE_TEST=1
export B1_INTEGRATION_API_BASE=https://api.ai.b1.germering
export B1_INTEGRATION_API_KEY=...
python3 -m unittest discover -s tests/integration -v
```

Or through Make:

```bash
B1_INTEGRATION_LIVE_TEST=1 B1_INTEGRATION_API_KEY=... make integration
```

The first live integration path checks `/admin/runtimes` adapter-contract, health, and production-readiness shape. It verifies that required runtime adapters remain visible without assuming a particular installed model inventory.

Use the same TLS helper variables as smoke tests when testing through the Caddy internal CA or temporary hostnames:

- `B1_INTEGRATION_CA_FILE`
- `B1_INTEGRATION_TLS_VERIFY=0`
- `B1_INTEGRATION_HOST_HEADER`

Remaining integration coverage will expand to LocalAI streaming/unload, ComfyUI native API proxy behaviour, Voicebox health, CPU audio, Model Hub blob semantics, artifact authorization, and restart reconciliation.
