#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${B1_OPEN_WEBUI_API_KEY_FILE:-}" ]]; then
  if [[ ! -r "${B1_OPEN_WEBUI_API_KEY_FILE}" ]]; then
    echo "B1 Open WebUI API key file is not readable" >&2
    exit 1
  fi
  OPENAI_API_KEY="$(tr -d '\r\n' < "${B1_OPEN_WEBUI_API_KEY_FILE}")"
  export OPENAI_API_KEY
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "B1 Open WebUI API key is not configured" >&2
  exit 1
fi

exec "$@"
