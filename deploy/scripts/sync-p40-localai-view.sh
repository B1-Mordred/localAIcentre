#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  cat >&2 <<'EOF'
Usage:
  deploy/scripts/sync-p40-localai-view.sh user@p40-worker.b1.germering [remote-model-dir]

Copies the B1 LocalAI runtime view to a portable P40 worker. This is
non-destructive: it does not delete remote files. Symlinks are followed so the
worker receives real model files and manifest.b1.json files.
EOF
  exit 2
fi

REMOTE="$1"
REMOTE_DIR="${2:-/srv/b1-p40-worker/models/}"
SRC="${B1_LOCALAI_RUNTIME_VIEW:-/srv/b1-ai-hub/models/runtime-views/localai/}"

if [[ ! -d "$SRC" ]]; then
  printf 'ERROR: LocalAI runtime view not found: %s\n' "$SRC" >&2
  exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
  printf 'ERROR: rsync is required on the B1 host and worker\n' >&2
  exit 1
fi

RSYNC=(rsync -a --copy-links --partial --human-readable --info=progress2)
if [[ "${B1_P40_RSYNC_SUDO:-false}" == "true" ]]; then
  RSYNC+=(--rsync-path="sudo rsync")
fi

printf 'Syncing %s to %s:%s\n' "$SRC" "$REMOTE" "$REMOTE_DIR"
"${RSYNC[@]}" "$SRC" "$REMOTE:$REMOTE_DIR"
