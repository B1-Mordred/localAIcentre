# Changelog

## Unreleased

- Added implementation plan for B1 AI Hub.
- Added initial Docker Compose topology with pinned base images and internal networks.
- Added service scaffolds for the control plane, runtime agent, artifact server, CPU audio, and runtime placeholders.
- Added Caddy gateway routing for the intended LAN virtual hosts.
- Added bootstrap, inventory, backup, and validation entry points.
- Added a production LocalAI Compose override pinned to the official CUDA 12 image digest, plus bootstrap directories and adapter readiness checks for LocalAI's native `:8080` runtime.
- Added a production ComfyUI Compose override and pinned B1 ComfyUI image build from upstream `v0.3.77`, including native `:8188` routing, B1 model-view mapping, conservative RTX 3060 startup flags, and CI/SBOM inventory coverage.
