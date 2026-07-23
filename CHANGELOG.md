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
- Added configurable admission controls for media job queue/rate limits and artifact storage headroom, exposed through `GET /admin/admission`, persisted admin policy APIs, Dashboard, Storage, and System tab editing.
- Added an administrator/operator runtime reservation fleet view at `GET /admin/runtime-reservations` and Jobs tab controls for reservation creation, cancellation, filtering, and current GPU lease inspection.
- Added Hugging Face repository source support for model download planning and resumable blob downloads, with bounded safe redirect handling and credential forwarding limited to the original source host.
- Added a maintenance-gated update promotion preflight at `POST /admin/updates/{id}/promote`, verifying staged Compose override checksums and pinned image availability before recording the Control Center promotion handoff.
- Added model-blob quarantine retention planning and confirmed cleanup through Storage and `POST /admin/models/quarantine/*`, preserving malformed entries and never deleting active authoritative blobs.
- Exposed a versioned `b1-runtime-adapter/v1alpha1` contract from `/admin/runtimes` and the Control Center Runtimes tab, including adapter capabilities, scheduler/submission/event surfaces, lifecycle hooks, metrics source, and runtime-agent unload/recovery boundaries.
- Added model-download retry/requeue through `POST /admin/models/downloads/{id}/retry` and the Control Center Models tab, preserving staged partial files so failed or cancelled downloads can resume through the verified worker path.
- Added model-download pause/resume through `POST /admin/models/downloads/{id}/pause` and `/resume` plus Control Center actions, stopping running downloads at chunk boundaries while preserving staged partial blobs.
- Added cooperative cancellation for blocking LocalAI and Voicebox GPU media runtime calls, aborting the control-plane request, marking the durable job cancelled, and requesting bounded runtime-agent recovery before releasing the GPU lease.
- Added an opt-in live smoke test target for deployed health, authenticated model listing, async TTS media jobs, SSE event delivery, artifact download, and optional admin self-test.
- Added Makefile targets for smoke, integration, compatibility, and security tests, with an opt-in live `/admin/runtimes` integration check and offline Compose exposure/privilege security checks.
- Added Redis-coordinated Model Hub blob download rate limiting with per-subject `X-RateLimit-*` headers and a bounded in-process fallback before proxying blob requests to internal storage.
