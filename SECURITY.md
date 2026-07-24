# Security

B1 AI Hub is LAN-internal by default. Do not configure router port forwarding for any B1 AI Hub endpoint.

## Principles

- Caddy is the only normal LAN entry point.
- Backend services bind only to internal Docker networks.
- The control plane must not mount `/var/run/docker.sock`.
- Runtime mutation is delegated to `runtime-agent`, whose `/v1/*` API is mTLS/token-protected, fail-closed when the token secret is missing, allowlisted, and audited.
- Model imports, custom nodes, uploaded media, archives, and external runtime URLs must be validated before use.
- Prompts, uploaded documents, voice samples, bearer tokens, API keys, and model-download credentials must never be logged.
- Remote/cloud providers remain disabled unless an administrator explicitly enables and labels them as external.

## Initial Operator Checklist

1. Run `make inventory` and review the old stack before cutover.
2. Verify backups under `$B1_DATA_ROOT/backups`.
3. Confirm Caddy internal CA trust only on intended LAN clients.
4. Restrict optional legacy ComfyUI `:8188` access by CIDR.
5. Create the first administrator through Control Center with the generated bootstrap key, then use scoped API clients for automation instead of the bootstrap key.

## Source Hygiene

Run the local source checks before committing security-sensitive changes:

```bash
make secret-scan
make sbom
```

CI additionally validates the generated CycloneDX SBOM, audits pinned Python requirements with `pip-audit`, builds the frontend and service images, runs production NPM audits, and scans the source tree plus built images with the pinned Trivy container. B1-owned images fail the build on HIGH or CRITICAL findings. The pinned official Open WebUI wrapper image is scanned and uploaded as an inventory because its internal upstream dependency graph is not hand-modified in this repository.

## Reporting Issues

This is a private LAN appliance project. Report issues through the repository owner or internal B1 operational channel.
