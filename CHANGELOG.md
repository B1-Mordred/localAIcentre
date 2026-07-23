# Changelog

## Unreleased

- Added implementation plan for B1 AI Hub.
- Added initial Docker Compose topology with pinned base images and internal networks.
- Added service scaffolds for the control plane, runtime agent, artifact server, CPU audio, and runtime placeholders.
- Added Caddy gateway routing for the intended LAN virtual hosts.
- Added bootstrap, inventory, backup, and validation entry points.
- Added a production LocalAI Compose override and B1 wrapper image built from the pinned official CUDA 12 image digest, including internal native API proxying and scheduler lifecycle hooks for model-list, smoke, warm, and unload probes.
- Added a production ComfyUI Compose override and pinned B1 ComfyUI image build from upstream `v0.3.77`, including native `:8188` routing, B1 model-view mapping, conservative RTX 3060 startup flags, B1 lifecycle hooks, and CI/SBOM inventory coverage.
- Added a production Voicebox Compose override and pinned B1 Voicebox image build from upstream `v0.5.0`, including native `:17493` routing through a B1 REST/WebSocket proxy, B1-managed voice data/cache storage, read-only model-view mapping, conservative scheduler lifecycle hooks, and CI/SBOM inventory coverage.
- Added a control-plane observability report at `GET /admin/metrics` and wired the Dashboard to display queue waits, recent job timings, model switches, runtime-agent availability, GPU telemetry, and host memory/storage without requiring a heavy monitoring stack.
- Added generated-artifact retention planning and confirmed cleanup through the Storage tab and `POST /admin/artifacts/*`, preserving protected Voicebox samples and marking reclaimed job artifacts as deleted.
- Added configurable admission controls for media job queue/rate limits and artifact storage headroom, exposed through `GET /admin/admission`, Dashboard, and the Storage tab.
