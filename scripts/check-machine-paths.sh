#!/bin/sh
# Refuse a staged line that names a real machine account's home directory.
#
# agent-cloud is a public template; a path like /Users/<name>/... or /home/<name>/... names
# the operator's login and machine layout (docs/MISTAKES.md 4.11: a path reached ledger
# entry 3.9 and was redacted afterwards by #328). This was the AGENTS.md pre-push audit's third grep, run by hand only; this hook
# makes it a gate. Fails closed, like the secret gates: a leak is irreversible once pushed.
#
# Passes: placeholders (/home/<user>, /home/{{ ansible_user }}, /home/${USER}) and the
# container or service accounts below, which name an image's user, not a person.
#
# Usage: check-machine-paths.sh        scan `git diff --cached` (the pre-commit hook)
#        check-machine-paths.sh -      scan a unified diff on stdin (tests)
set -eu

# ponytail: a fixed allowlist; add a service account here with the image it comes from.
ALLOWED='step|frappe|node|nobody|root|runner|Shared'

if [ "${1:-}" = "-" ]; then
  diff=$(cat)
else
  # R included: a renamed file's added lines are new content too.
  diff=$(git diff --cached --diff-filter=ACMR -U0)
fi

# Every home-directory path on an added line must be an allowed account; one line can hold
# an allowed path and a personal one, so each path is checked, not each line.
leaks=$(printf '%s\n' "$diff" | grep -E '^\+' | grep -vE '^\+\+\+ (b/|/dev/null)' \
  | grep -E "/(Users|home)/[A-Za-z0-9._-]+" | while IFS= read -r line; do
    printf '%s\n' "$line" | grep -oE "/(Users|home)/[A-Za-z0-9._-]+" \
      | grep -vqE "^/(Users|home)/($ALLOWED)\$" && printf '%s\n' "$line"
  done || true)

if [ -n "$leaks" ]; then
  echo "ERROR: a machine account's home directory is in the staged changes:"
  printf '%s\n' "$leaks"
  echo ""
  echo "agent-cloud is a public template. Use a placeholder (/home/<user>, \$HOME, {{ ansible_user }})."
  exit 1
fi
