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

Future security tests will cover authentication, under-scoped requests, runtime-agent allowlists, traversal, symlink escape, archive bombs, SSRF, CORS, CSRF, custom-node installation policy, and secret redaction.
