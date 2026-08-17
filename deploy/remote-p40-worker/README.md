# Portable P40 Worker

This directory installs a second-PC NVIDIA P40 as a portable B1 AI Hub LocalAI worker. It is intended for large GGUF LLM work where the current 6 GB laptop GPU is too constrained.

The worker runs the same pinned B1 LocalAI wrapper used by the appliance, keeps one active backend, exposes authenticated lifecycle and GPU-metrics hooks, and publishes only a LAN-restricted HTTPS gateway. It does not replace the main B1 appliance. The hub schedules explicitly compatible LLM aliases through the dedicated `lan-localai-worker` adapter.

## Hardware Fit

Use the P40 primarily for GGUF LLM inference:

- GPT-OSS 20B MXFP4 / Q-style GGUF
- Laguna XS 2.1 GGUF
- Gemma 4 12B GGUF
- 7B to 14B quality models with longer context

The P40 has 24 GB VRAM but no tensor cores. It is a good capacity upgrade for LLMs, but it is not the best first choice for modern diffusion/video stacks that depend on newer CUDA kernels or tensor-core acceleration. Keep ComfyUI video/image work on the main appliance until the exact workflow is validated on the P40 host.

## Host Prerequisites

Install these on the worker PC before running the portable installer:

- Linux host with the P40 installed, powered, and cooled
- NVIDIA driver visible through `nvidia-smi`
- Docker Engine
- Docker Compose v2
- NVIDIA Container Toolkit configured for Docker
- LAN DNS entry such as `p40-worker.b1.germering`

The installer validates prerequisites but does not install or reconfigure the NVIDIA driver or Docker.

## Build A Portable Bundle On The B1 Host

From the repository root:

```bash
deploy/scripts/package-p40-worker.sh /srv/b1-ai-hub/backups/b1-p40-worker.tar.gz
```

To include the current B1 runtime hook token in the bundle, run:

```bash
B1_P40_INCLUDE_RUNTIME_TOKEN=true \
  deploy/scripts/package-p40-worker.sh /srv/b1-ai-hub/backups/b1-p40-worker.tar.gz
```

The token-bearing archive is sensitive. Transfer it only over SSH or another trusted channel and remove stale copies after installation.

## Install On The Worker PC

```bash
sudo mkdir -p /opt/b1-p40-worker
sudo tar -xzf /tmp/b1-p40-worker.tar.gz -C /opt/b1-p40-worker --strip-components=1
cd /opt/b1-p40-worker
sudo ./install.sh
```

The installer creates:

```text
/srv/b1-p40-worker/
├── models/
├── cache/
├── data/
├── logs/
└── secrets/
```

The authoritative model library still belongs to the B1 hub. Put only validated runtime views or synchronized model cache content under `/srv/b1-p40-worker/models`.

## Copy Installed LocalAI Models To The Worker

From the B1 hub host, after at least one LocalAI model is installed:

```bash
deploy/scripts/sync-p40-localai-view.sh mordred@p40-worker.b1.germering
```

If the remote user needs sudo to write `/srv/b1-p40-worker/models`, use:

```bash
B1_P40_RSYNC_SUDO=true \
  deploy/scripts/sync-p40-localai-view.sh mordred@p40-worker.b1.germering
```

The sync is deliberately non-destructive. It follows the hub runtime-view symlinks and copies actual model files plus `manifest.b1.json` metadata so the worker can generate LocalAI configs locally.

## Direct Smoke Test

On the worker:

```bash
curl --fail http://127.0.0.1:${B1_P40_LOCALAI_DEBUG_PORT:-18080}/readyz
```

From the B1 host, after DNS points at the worker:

```bash
curl --fail --cacert /path/to/p40-worker-caddy-root.crt https://p40-worker.b1.germering:9443/healthz
curl --fail --cacert /path/to/p40-worker-caddy-root.crt https://p40-worker.b1.germering:9443/v1/models
```

The `--insecure` flag is needed until the worker Caddy internal-CA root is trusted by the client. The root certificate is created after first start:

```text
  /srv/b1-p40-worker/data/caddy/caddy/pki/authorities/local/root.crt
```

## Runtime Token

The B1 LocalAI wrapper protects lifecycle routes such as `/b1/runtime/unload` with the `runtime_control_token`. For managed use, the worker and the hub must share the same token.

If the portable archive was made without `B1_P40_INCLUDE_RUNTIME_TOKEN=true`, pass the hub token during install:

```bash
sudo B1_RUNTIME_CONTROL_TOKEN="$(sudo cat /srv/b1-ai-hub/secrets/runtime_control_token)" ./install.sh
```

If no token is provided, `install.sh` generates a new one and prints a warning. Direct LocalAI smoke tests still work, but the hub cannot use B1 lifecycle hooks until it is configured with the same token for this worker.

## Managed B1 Integration

The existing generic external-runtime adapter deliberately rejects private LAN addresses to prevent SSRF and accidental data exfiltration. Do not weaken that guard rail for this P40 worker.

Configure the dedicated adapter on the B1 host:

```dotenv
B1_LAN_LOCALAI_WORKER_URL=https://p40-worker.b1.germering:9443
B1_LAN_LOCALAI_WORKER_HOSTNAME=p40-worker.b1.germering
B1_LAN_LOCALAI_WORKER_ALLOWED_CIDRS=192.168.2.109/32
B1_LAN_LOCALAI_WORKER_TLS_CA_FILE=/srv/b1-ai-hub/data/control-plane/p40-worker-caddy-root.crt
```

The adapter revalidates DNS against the approved hostname and CIDRs on every URL construction, verifies the worker's private CA, uses the shared runtime token for lifecycle and metrics, participates in the global GPU lease, and is eligible only for manifests that explicitly list `lan-localai-worker`.
