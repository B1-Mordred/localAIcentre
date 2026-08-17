#!/usr/bin/env bash
set -euo pipefail

token_file="${B1_RUNTIME_CONTROL_TOKEN_FILE:-/run/secrets/runtime_control_token}"
if [[ ! -r "$token_file" || ! -s "$token_file" ]]; then
  printf 'ERROR: runtime control token is missing or unreadable\n' >&2
  exit 1
fi

B1_RUNTIME_CONTROL_TOKEN="$(tr -d '\r\n' <"$token_file")"
if [[ -z "$B1_RUNTIME_CONTROL_TOKEN" ]]; then
  printf 'ERROR: runtime control token is empty\n' >&2
  exit 1
fi
export B1_RUNTIME_CONTROL_TOKEN

exec /usr/bin/setpriv \
  --reuid=999 \
  --regid=999 \
  --clear-groups \
  /usr/bin/tini -- b1-localai-entrypoint.sh "$@"
