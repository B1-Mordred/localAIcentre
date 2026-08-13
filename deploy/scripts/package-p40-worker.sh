#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$REPO_ROOT/artifacts/b1-p40-worker.tar.gz}"
TMP="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

mkdir -p "$(dirname "$OUT")"
mkdir -p "$TMP/b1-p40-worker/deploy"

cp -a "$REPO_ROOT/deploy/localai" "$TMP/b1-p40-worker/deploy/localai"
find "$TMP/b1-p40-worker/deploy/localai" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$TMP/b1-p40-worker/deploy/localai" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
cp -a "$REPO_ROOT/deploy/remote-p40-worker/compose.yaml" "$TMP/b1-p40-worker/compose.yaml"
cp -a "$REPO_ROOT/deploy/remote-p40-worker/Caddyfile" "$TMP/b1-p40-worker/Caddyfile"
cp -a "$REPO_ROOT/deploy/remote-p40-worker/.env.example" "$TMP/b1-p40-worker/.env.example"
cp -a "$REPO_ROOT/deploy/remote-p40-worker/install.sh" "$TMP/b1-p40-worker/install.sh"
cp -a "$REPO_ROOT/deploy/remote-p40-worker/README.md" "$TMP/b1-p40-worker/README.md"

chmod 0755 "$TMP/b1-p40-worker/install.sh"

if [[ "${B1_P40_INCLUDE_RUNTIME_TOKEN:-false}" == "true" ]]; then
  TOKEN_SOURCE="${B1_RUNTIME_CONTROL_TOKEN_FILE:-/srv/b1-ai-hub/secrets/runtime_control_token}"
  if [[ ! -s "$TOKEN_SOURCE" ]]; then
    printf 'ERROR: runtime token not found at %s\n' "$TOKEN_SOURCE" >&2
    exit 1
  fi
  install -d -m 0700 "$TMP/b1-p40-worker/secrets"
  install -m 0600 "$TOKEN_SOURCE" "$TMP/b1-p40-worker/secrets/runtime_control_token"
  printf 'Included runtime control token from %s\n' "$TOKEN_SOURCE" >&2
fi

tar -C "$TMP" -czf "$OUT" b1-p40-worker
(
  cd "$(dirname "$OUT")"
  sha256sum "$(basename "$OUT")" >"$(basename "$OUT").sha256"
)

printf 'Wrote %s\n' "$OUT"
printf 'Wrote %s.sha256\n' "$OUT"
