#!/bin/bash
# Push local code to the Pi and restart the service.
# Run from the repo root: bash scripts/deploy.sh

set -e

HOST="${MULCHY_HOST:-pi@192.168.0.142}"
DEST="${MULCHY_DEST:-~/mulchy/}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Syncing to $HOST:$DEST ==="
rsync -av --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='tests' \
  --exclude='reference' \
  --exclude='node_modules' \
  --exclude='.github' \
  --exclude='.vscode' \
  --exclude='.ruff_cache' \
  --exclude='.pytest_cache' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  . "$HOST:$DEST"

echo "=== Restarting mulchy service ==="
ssh "$HOST" sudo systemctl restart mulchy

echo "=== Done ==="
echo "Tail logs: ssh $HOST journalctl -u mulchy -f"
