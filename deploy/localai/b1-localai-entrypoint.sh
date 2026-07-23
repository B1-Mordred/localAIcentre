#!/usr/bin/env bash
set -euo pipefail

public_host="${B1_LOCALAI_PUBLIC_HOST:-0.0.0.0}"
public_port="${B1_LOCALAI_PUBLIC_PORT:-8080}"
upstream_address="${B1_LOCALAI_UPSTREAM_ADDRESS:-127.0.0.1:18080}"

export B1_LOCALAI_PUBLIC_HOST="$public_host"
export B1_LOCALAI_PUBLIC_PORT="$public_port"
export B1_LOCALAI_UPSTREAM_ADDRESS="$upstream_address"
export B1_LOCALAI_UPSTREAM_URL="${B1_LOCALAI_UPSTREAM_URL:-http://${upstream_address}}"
export LOCALAI_ADDRESS="${LOCALAI_ADDRESS:-${upstream_address}}"

mkdir -p \
  "${LOCALAI_MODELS_PATH:-/srv/b1-ai-hub/models}" \
  "${LOCALAI_BACKENDS_PATH:-/srv/b1-ai-hub/localai/backends}" \
  "${LOCALAI_CONFIG_DIR:-/srv/b1-ai-hub/localai/configuration}" \
  "${LOCALAI_DATA_PATH:-/srv/b1-ai-hub/localai/data}" \
  "${XDG_CACHE_HOME:-/srv/b1-ai-hub/cache/xdg}" \
  "${HF_HOME:-/srv/b1-ai-hub/cache/huggingface}"

/entrypoint.sh "$@" &
localai_pid="$!"

terminate() {
  if kill -0 "$localai_pid" 2>/dev/null; then
    kill "$localai_pid" 2>/dev/null || true
    wait "$localai_pid" 2>/dev/null || true
  fi
}

trap terminate INT TERM

python3 /usr/local/bin/b1_localai_proxy.py &
proxy_pid="$!"

set +e
wait -n "$localai_pid" "$proxy_pid"
status="$?"
set -e

terminate
if kill -0 "$proxy_pid" 2>/dev/null; then
  kill "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
fi

exit "$status"
