#!/usr/bin/env bash
# Read-only: compare every live ruleset named in .github/rulesets/*.json with its file.
#
# The read-back half of apply.sh (docs/MISTAKES.md 10.21). Matches rulesets by
# name exactly as apply.sh does, fetches each by id, and hands both to compare.py.
# Exits non-zero naming every difference; a ruleset missing live is drift too.
#
# Requires: gh (authenticated; GH_TOKEN in CI), jq, python3. Never prints the token.
#
# Usage:
#   .github/rulesets/check-drift.sh                     # all *.json, default repo
#   .github/rulesets/check-drift.sh OWNER/REPO [a.json ...]
set -euo pipefail

REPO="${1:-uhstray-io/agent-cloud}"
shift || true

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -gt 0 ]; then
  files=("$@")
else
  files=("$SCRIPT_DIR"/*.json)
fi

for cmd in gh jq python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: '$cmd' is required" >&2; exit 1; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# One listing for every file; a gh/auth failure aborts loudly (set -e).
existing="$(gh api --paginate --slurp "repos/$REPO/rulesets")"

status=0
for file in "${files[@]}"; do
  if [ ! -f "$file" ] && [ -f "$SCRIPT_DIR/$file" ]; then
    file="$SCRIPT_DIR/$file"
  fi
  [ -f "$file" ] || { echo "ERROR: not a file: $file" >&2; exit 1; }

  name="$(jq -r '.name // empty' "$file")"
  [ -n "$name" ] || { echo "ERROR: $file has no .name" >&2; exit 1; }

  id="$(jq -r --arg name "$name" 'add | .[] | select(.name == $name) | .id' <<<"$existing" | head -n1)"
  if [ -z "$id" ]; then
    echo "DRIFT [$name] no live ruleset with this name on $REPO"
    status=1
    continue
  fi

  gh api "repos/$REPO/rulesets/$id" >"$work/live.json"
  python3 "$SCRIPT_DIR/compare.py" "$file" "$work/live.json" || status=1
done
exit "$status"
