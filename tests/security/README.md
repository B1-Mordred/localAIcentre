# Security Tests

Security tests are offline policy checks and opt-in live checks for a deployed B1 AI Hub.

Run the current offline security suite:

```bash
make security
```

The first committed security suite validates Compose exposure policy:

- only `gateway` may publish host ports
- backend services must not publish LocalAI, ComfyUI, Voicebox, PostgreSQL, Redis, artifact-server, runtime-agent, Control Center, Media Studio, or Open WebUI ports
- backend services must not run privileged or add Linux capabilities, except the documented minimal PostgreSQL/Redis capability sets
- runtime-agent exposes only the fixed allowlisted status, metrics, service lifecycle, pinned-image, runtime-action, bounded-log, and rollback routes
- runtime-agent mutating routes keep service allowlist guards, pinned-image guards, runtime-action guards, and bounded-log limits
- runtime-agent source must not include Docker exec/container-create/build/volume/secret/plugin passthrough paths or arbitrary host command/environment/mount controls

Future security tests will cover deployed authentication, under-scoped requests, traversal, symlink escape, archive bombs, SSRF, CORS, CSRF, custom-node installation policy, and secret redaction.
