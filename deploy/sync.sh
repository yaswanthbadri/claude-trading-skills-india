#!/usr/bin/env bash
# Push this folder to the server without the bulky or private bits.
#
#   deploy/sync.sh user@host [remote-dir]
#   deploy/sync.sh root@203.0.113.5
#
# Deliberately does NOT use --delete: the server's candle cache, reports and
# scorecard are the state of the system. Syncing code must never erase them.
set -euo pipefail

TARGET="${1:?usage: deploy/sync.sh user@host [remote-dir]}"
REMOTE="${2:-/opt/market-desk}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "syncing $HERE -> $TARGET:$REMOTE"

ssh "$TARGET" "mkdir -p '$REMOTE'"

rsync -az \
  --exclude '.git' \
  --exclude '.env' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude 'logs/' \
  --exclude 'reports/' \
  --exclude 'data/candles/' \
  --exclude 'data/nifty.csv' \
  --exclude 'data/cache_state.json' \
  --exclude 'data/recommendations.json' \
  --exclude 'data/subscribers.json' \
  "$HERE/" "$TARGET:$REMOTE/"

echo "code synced. First time? run:"
echo "  ssh $TARGET 'bash $REMOTE/deploy/vps_setup.sh'"
echo "After that, to apply changes:"
echo "  ssh $TARGET 'cd $REMOTE && docker compose up -d --build'"
