#!/usr/bin/env bash
# require-gnu-rsync.sh — exit 0 only when the rsync first on PATH is GNU rsync 3.x.
#
# Local placement (platform/playbooks/tasks/place-monorepo.yml) copies the working tree with
# `rsync --delete --filter=':- .gitignore'`. macOS /usr/bin/rsync is openrsync, which ignores
# that filter on --delete, so it deletes gitignored files a deploy rendered (Authentik's
# blueprints-active), leaving running containers on stale bind mounts. GNU rsync 3.x honours
# it. Used by the local preflight (scripts/local-dev.sh) and by place-monorepo.yml, so the two
# can never disagree.

set -euo pipefail

line=""
if command -v rsync >/dev/null 2>&1; then
  line="$(rsync --version 2>/dev/null | sed -n '1p')" || true
fi
if printf '%s\n' "$line" | grep -qE '^rsync +version v?3\.'; then
  echo "$line"
  exit 0
fi
echo "rsync on PATH is not GNU rsync 3.x (${line:-not found}). macOS ships openrsync, which deletes gitignored files on --delete. Install GNU rsync (brew install rsync, or brew bundle) and keep Homebrew's bin ahead of /usr/bin on PATH." >&2
exit 1
