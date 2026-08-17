#!/usr/bin/env bash
set -euo pipefail

data_dir="${B1_VOICEBOX_DATA_DIR:-/srv/b1-ai-hub/voicebox}"
models_dir="${B1_VOICEBOX_MODELS_DIR:-/srv/b1-ai-hub/models}"
host="${B1_VOICEBOX_HOST:-0.0.0.0}"
port="${B1_VOICEBOX_PORT:-17493}"
upstream_host="${B1_VOICEBOX_UPSTREAM_HOST:-127.0.0.1}"
upstream_port="${B1_VOICEBOX_UPSTREAM_PORT:-17494}"

mkdir -p \
  "${data_dir}/generations" \
  "${data_dir}/profiles" \
  "${data_dir}/captures" \
  "${data_dir}/cache" \
  "/srv/b1-ai-hub/cache/huggingface" \
  "/srv/b1-ai-hub/cache/xdg" \
  "/tmp/numba_cache"

export VOICEBOX_MODELS_DIR="${models_dir}"
export HF_HUB_DISABLE_TELEMETRY="${HF_HUB_DISABLE_TELEMETRY:-1}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export DO_NOT_TRACK="${DO_NOT_TRACK:-1}"
export NUMBA_CACHE_DIR="${NUMBA_CACHE_DIR:-/tmp/numba_cache}"
export HF_HOME="${HF_HOME:-/srv/b1-ai-hub/cache/huggingface}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/srv/b1-ai-hub/cache/xdg}"
export B1_VOICEBOX_UPSTREAM_HOST="${upstream_host}"
export B1_VOICEBOX_UPSTREAM_PORT="${upstream_port}"
export B1_VOICEBOX_UPSTREAM_URL="${B1_VOICEBOX_UPSTREAM_URL:-http://${upstream_host}:${upstream_port}}"

exec python /usr/local/bin/b1_voicebox_proxy.py
