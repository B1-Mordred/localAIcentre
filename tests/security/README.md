# Security Tests

Security tests are offline policy checks and opt-in live checks for a deployed B1 AI Hub.

Run the offline security suite:

```bash
make security
```

The offline suite validates Compose exposure policy and runtime-agent source policy:

- only `gateway` may publish host ports
- backend services must not publish LocalAI, ComfyUI, Voicebox, PostgreSQL, Redis, artifact-server, runtime-agent, Control Center, Media Studio, or Open WebUI ports
- backend services must not run privileged or add Linux capabilities, except the documented minimal PostgreSQL/Redis capability sets
- runtime-agent exposes only the fixed allowlisted status, metrics, service lifecycle, pinned-image, runtime-action, bounded-log, and rollback routes
- runtime-agent mutating routes keep service allowlist guards, pinned-image guards, runtime-action guards, and bounded-log limits
- runtime-agent source must not include Docker exec/container-create/build/volume/secret/plugin passthrough paths or arbitrary host command/environment/mount controls

Run deployed security acceptance on the target stack after production auth is configured:

```bash
export B1_SECURITY_API_BASE=https://api.ai.b1.germering
export B1_SECURITY_COMFY_BASE=https://comfy.ai.b1.germering
export B1_SECURITY_API_KEY=...
export B1_SECURITY_CREATE_TEMP_UNDERSCOPED_CLIENT=1
export B1_SECURITY_BROWSER_USERNAME=admin
export B1_SECURITY_BROWSER_PASSWORD=...
export B1_SECURITY_ARTIFACT_JOB_ID=<completed-job-with-artifact>
export B1_SECURITY_CA_FILE=/srv/b1-ai-hub/data/caddy/pki/authorities/local/root.crt
export B1_SECURITY_EVIDENCE=/srv/b1-ai-hub/backups/acceptance/security-acceptance.json
make security-acceptance
```

`B1_SECURITY_CREATE_TEMP_UNDERSCOPED_CLIENT=1` creates a temporary `user` API client with `models:read`, proves it cannot read `/admin/self-test`, and revokes it in teardown. Instead, set `B1_SECURITY_UNDERSCOPED_API_KEY` to a pre-created low-scope key when you want no client lifecycle mutation. The artifact authorization probe also creates and revokes a temporary `user` API client with `jobs:read`, unless `B1_SECURITY_ARTIFACT_READER_API_KEY` is supplied. Point `B1_SECURITY_ARTIFACT_URL` or `B1_SECURITY_ARTIFACT_JOB_ID` at an artifact from the smoke or installed-workflow acceptance run; otherwise the harness searches recent completed jobs and fails if no generated artifact exists. For the CSRF check, either provide `B1_SECURITY_BROWSER_USERNAME`/`B1_SECURITY_BROWSER_PASSWORD` so the harness can log in and omit `X-B1-CSRF`, or provide a valid `B1_SECURITY_BROWSER_SESSION_COOKIE`. The live security harness refuses to send API keys, browser cookies, or credential-bearing JSON bodies over plain HTTP unless `B1_ACCEPTANCE_ALLOW_INSECURE_HTTP=true` is set for an isolated development run.

The live suite writes `b1-ai-hub-security-acceptance/v1` evidence covering unauthenticated rejection, under-scoped rejection, credentialed CORS wildcard denial, browser CSRF rejection, ComfyUI Manager/custom-node management denial, model-import SSRF rejection, artifact traversal rejection, artifact authorization rejection for unauthenticated, under-scoped, and different-owner clients, runtime-agent mutation-guard posture through `/admin/self-test`, and runtime-agent/control-plane log redaction. Control Center acceptance reports ingest the latest supported direct-child `$B1_BACKUP_ROOT/acceptance/*.json` security evidence file and block handoff if any required check is absent or incomplete.
