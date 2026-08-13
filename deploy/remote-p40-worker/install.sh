#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'ERROR: required command not found: %s\n' "$1" >&2
    exit 1
  fi
}

require_command docker
require_command nvidia-smi

if ! docker compose version >/dev/null 2>&1; then
  printf 'ERROR: Docker Compose v2 is required: docker compose version failed\n' >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  log "Created .env from .env.example"
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [[ -z "${B1_P40_LOCALAI_BUILD_CONTEXT:-}" ]]; then
  if [[ -d "$SCRIPT_DIR/deploy/localai" ]]; then
    export B1_P40_LOCALAI_BUILD_CONTEXT="./deploy/localai"
  else
    export B1_P40_LOCALAI_BUILD_CONTEXT="../localai"
  fi
fi

ROOT="${B1_P40_WORKER_ROOT:-/srv/b1-p40-worker}"

log "Checking NVIDIA GPU visibility"
nvidia-smi -L

if ! docker info 2>/dev/null | grep -qi 'nvidia'; then
  warn "Docker does not report an NVIDIA runtime. Install/configure nvidia-container-toolkit before relying on GPU containers."
fi

if [[ "${B1_P40_RUN_DOCKER_GPU_PROBE:-false}" == "true" ]]; then
  log "Running optional Docker GPU probe"
  docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
fi

log "Creating worker directories under $ROOT"
install -d -m 0755 \
  "$ROOT/models" \
  "$ROOT/cache/localai" \
  "$ROOT/cache/caddy" \
  "$ROOT/data/localai/configuration" \
  "$ROOT/data/localai/backends" \
  "$ROOT/data/localai/data" \
  "$ROOT/data/caddy" \
  "$ROOT/logs/localai" \
  "$ROOT/secrets"

# The non-root LocalAI workload needs to traverse the read-only model view.
# Synced model files and subdirectories retain their managed permissions.
chmod 0755 "$ROOT/models"

chown -R 999:999 \
  "$ROOT/cache/localai" \
  "$ROOT/data/localai" \
  "$ROOT/logs/localai"

TOKEN_FILE="$ROOT/secrets/runtime_control_token"
if [[ ! -s "$TOKEN_FILE" ]]; then
  if [[ -s "$SCRIPT_DIR/secrets/runtime_control_token" ]]; then
    install -m 0600 "$SCRIPT_DIR/secrets/runtime_control_token" "$TOKEN_FILE"
    log "Imported bundled runtime control token"
  elif [[ -n "${B1_RUNTIME_CONTROL_TOKEN:-}" ]]; then
    umask 077
    printf '%s' "$B1_RUNTIME_CONTROL_TOKEN" >"$TOKEN_FILE"
    log "Imported runtime control token from B1_RUNTIME_CONTROL_TOKEN"
  else
    umask 077
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -base64 48 >"$TOKEN_FILE"
    else
      head -c 48 /dev/urandom | base64 >"$TOKEN_FILE"
    fi
    warn "Generated a new runtime control token. Configure the B1 hub with the same token before managed lifecycle hooks can control this worker."
  fi
fi
chmod 600 "$TOKEN_FILE"

COMPOSE=(docker compose --env-file .env -f compose.yaml)

log "Validating worker Compose project"
"${COMPOSE[@]}" config --quiet

log "Starting B1 P40 worker"
"${COMPOSE[@]}" up -d --build

log "Worker containers:"
"${COMPOSE[@]}" ps

cat <<EOF

B1 P40 worker is starting.

Local debug URL on the worker:
  http://127.0.0.1:${B1_P40_LOCALAI_DEBUG_PORT:-18080}/readyz

LAN HTTPS URL, subject to the Caddy allowlist:
  https://<worker-hostname>:${B1_P40_WORKER_HTTPS_BIND##*:}/healthz
  https://<worker-hostname>:${B1_P40_WORKER_HTTPS_BIND##*:}/v1/models

Model views must be placed under:
  $ROOT/models

The worker Caddy internal-CA root certificate appears after first start at:
  $ROOT/data/caddy/pki/authorities/local/root.crt
EOF
