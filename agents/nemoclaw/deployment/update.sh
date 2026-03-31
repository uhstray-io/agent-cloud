#!/usr/bin/env bash
# Non-destructive update: check upstream sync status, then deploy.
# This is a convenience wrapper around deploy.sh (without --onboard).
#
# Usage:
#   ./update.sh              Check upstream sync + deploy (update/migrate)
#   ./update.sh --local      Same, but already on the server
#   ./update.sh --sync-only  Just check if fork is behind upstream

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SYNC_ONLY=false
FORWARD_ARGS=""
for arg in "$@"; do
  case "$arg" in
    --sync-only) SYNC_ONLY=true ;;
    --local) FORWARD_ARGS="$FORWARD_ARGS --local" ;;
  esac
done

# ── Check fork sync status ──────────────────────────────────────────
echo "==> Checking NemoClaw fork sync status..."
if [ -d "$SCRIPT_DIR/NemoClaw/.git" ]; then
  cd "$SCRIPT_DIR/NemoClaw"
  git remote add upstream https://github.com/NVIDIA/NemoClaw.git 2>/dev/null || true
  git fetch upstream 2>/dev/null
  BEHIND=$(git rev-list --count HEAD..upstream/main 2>/dev/null || echo "0")
  if [ "$BEHIND" -gt 0 ] 2>/dev/null; then
    echo "    Fork is $BEHIND commits behind upstream."
    echo "    To sync: cd NemoClaw && git merge upstream/main"
  else
    echo "    Fork is up to date with upstream."
  fi
  cd "$SCRIPT_DIR"
else
  echo "    NemoClaw/ directory not found. Skipping fork sync check."
fi

if $SYNC_ONLY; then
  echo "==> Sync check complete."
  exit 0
fi

# ── Run deploy (update/migrate, not onboard) ────────────────────────
exec "$SCRIPT_DIR/deploy.sh" $FORWARD_ARGS
