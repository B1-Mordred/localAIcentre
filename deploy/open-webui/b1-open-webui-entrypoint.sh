#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${B1_OPEN_WEBUI_API_KEY_FILE:-}" ]]; then
  if [[ ! -r "${B1_OPEN_WEBUI_API_KEY_FILE}" ]]; then
    echo "B1 Open WebUI API key file is not readable" >&2
    exit 1
  fi
  OPENAI_API_KEY="$(tr -d '\r\n' < "${B1_OPEN_WEBUI_API_KEY_FILE}")"
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "B1 Open WebUI API key is not configured" >&2
  exit 1
fi

b1_api_base_url="${B1_OPEN_WEBUI_API_BASE_URL:-${OPENAI_API_BASE_URL:-http://control-plane:8000/v1}}"
b1_api_base_url="${b1_api_base_url%/}"

export B1_OPEN_WEBUI_API_BASE_URL="${b1_api_base_url}"
export OPENAI_API_BASE_URL="${b1_api_base_url}"
export OPENAI_API_BASE_URLS="${b1_api_base_url}"
export RAG_OPENAI_API_BASE_URL="${b1_api_base_url}"
export IMAGES_OPENAI_API_BASE_URL="${b1_api_base_url}"
export IMAGES_EDIT_OPENAI_API_BASE_URL="${b1_api_base_url}"
export AUDIO_TTS_OPENAI_API_BASE_URL="${b1_api_base_url}"
export AUDIO_STT_OPENAI_API_BASE_URL="${b1_api_base_url}"

export OPENAI_API_KEY
export OPENAI_API_KEYS="${OPENAI_API_KEY}"
export RAG_OPENAI_API_KEY="${OPENAI_API_KEY}"
export IMAGES_OPENAI_API_KEY="${OPENAI_API_KEY}"
export IMAGES_EDIT_OPENAI_API_KEY="${OPENAI_API_KEY}"
export AUDIO_TTS_OPENAI_API_KEY="${OPENAI_API_KEY}"
export AUDIO_STT_OPENAI_API_KEY="${OPENAI_API_KEY}"

exec "$@"
