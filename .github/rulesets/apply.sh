#!/usr/bin/env bash
# Apply agent-cloud repository rulesets via the GitHub API — idempotent.
#
# Rulesets are config-as-code: edit the *.json beside this script, then run this
# to create-or-update the matching ruleset on GitHub. Re-running converges
# (matched by ruleset name) instead of creating duplicates, so it is safe to run
# repeatedly — the same foundational, re-runnable pattern the platform uses for
# every other piece of automation.
#
# Requires: gh (authenticated as a repository admin), jq.
#
# Usage:
#   .github/rulesets/apply.sh                      # all *.json here, default repo
#   .github/rulesets/apply.sh OWNER/REPO           # all *.json here, other repo
#   .github/rulesets/apply.sh OWNER/REPO a.json b.json
set -euo pipefail

REPO="${1:-uhstray-io/agent-cloud}"
shift || true

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -gt 0 ]; then
  files=("$@")
else
  files=("$SCRIPT_DIR"/*.json)
fi

for cmd in gh jq; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: '$cmd' is required" >&2; exit 1; }
done

for file in "${files[@]}"; do
  # Explicit args are documented to sit beside this script; if a bare name isn't
  # found relative to the current directory, resolve it against SCRIPT_DIR.
  if [ ! -f "$file" ] && [ -f "$SCRIPT_DIR/$file" ]; then
    file="$SCRIPT_DIR/$file"
  fi
  [ -f "$file" ] || { echo "ERROR: not a file: $file" >&2; exit 1; }

  name="$(jq -r '.name // empty' "$file")"
  [ -n "$name" ] || { echo "ERROR: $file has no .name" >&2; exit 1; }

  # Idempotency key: an existing ruleset with the same name is updated in place.
  # --paginate + --slurp so a repo with more than one page of rulesets can't hide
  # an existing one (which would cause a duplicate create). Capturing separately
  # means a real gh/auth failure aborts loudly (set -e) with gh's own error,
  # instead of being swallowed and silently falling through to a create.
  existing="$(gh api --paginate --slurp "repos/$REPO/rulesets")"
  id="$(jq -r --arg name "$name" 'add | .[] | select(.name == $name) | .id' <<<"$existing" | head -n1)"

  if [ -n "$id" ]; then
    echo "Updating ruleset '$name' (id $id) on $REPO ..."
    gh api -X PUT "repos/$REPO/rulesets/$id" --input "$file" \
      --jq '"  -> \(.name): enforcement=\(.enforcement), rules=\(.rules | length)"'
  else
    echo "Creating ruleset '$name' on $REPO ..."
    gh api -X POST "repos/$REPO/rulesets" --input "$file" \
      --jq '"  -> \(.name): enforcement=\(.enforcement), rules=\(.rules | length)"'
  fi
done
