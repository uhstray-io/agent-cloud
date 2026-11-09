#!/usr/bin/env bash
# Apply the release-major GitHub environment as code, idempotently.
#
# release-major.json beside this script is the request body for
# PUT /repos/{owner}/{repo}/environments/{environment_name} (GitHub REST, "Create or update an
# environment"). The PUT sets the environment's settings from that body, so re-running converges. The
# deployment branch policy `main` is created only when no branch policy of that name is listed,
# so a second run creates nothing.
#
# Dry run is the default. It makes no API call at all: it prints the requests --apply would send.
#
# Requires: jq. gh, authenticated as a repository admin, is needed only with --apply.
#
# Usage:
#   .github/environments/apply.sh                       # dry run, default repo
#   .github/environments/apply.sh --apply               # apply, default repo
#   .github/environments/apply.sh --apply OWNER/REPO    # apply, other repo
set -euo pipefail

ENV_NAME="release-major"
POLICY_NAME="main"
REPO="uhstray-io/agent-cloud"
MODE="dry-run"

for arg in "$@"; do
  case "$arg" in
    --apply) MODE="apply" ;;
    --dry-run) MODE="dry-run" ;;
    -*) echo "ERROR: unknown option: $arg" >&2; exit 2 ;;
    */*) REPO="$arg" ;;
    *) echo "ERROR: expected OWNER/REPO, got: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SPEC="$SCRIPT_DIR/$ENV_NAME.json"

command -v jq >/dev/null 2>&1 || { echo "ERROR: 'jq' is required" >&2; exit 1; }
[ -f "$SPEC" ] || { echo "ERROR: not a file: $SPEC" >&2; exit 1; }
jq -e 'type == "object"' "$SPEC" >/dev/null || { echo "ERROR: $SPEC is not a JSON object" >&2; exit 1; }

# Request body for POST .../deployment-branch-policies ("Create a deployment branch policy").
POLICY_BODY="$(jq -n --arg name "$POLICY_NAME" '{name: $name, type: "branch"}')"

if [ "$MODE" = "dry-run" ]; then
  echo "DRY RUN for $REPO: no API call is made. Pass --apply to write."
  echo
  echo "1. PUT repos/$REPO/environments/$ENV_NAME with this body:"
  jq . "$SPEC"
  echo
  echo "2. Ensure a deployment branch policy named '$POLICY_NAME' exists:"
  echo "   GET repos/$REPO/environments/$ENV_NAME/deployment-branch-policies (all pages), then POST"
  echo "   this body only when no branch policy named '$POLICY_NAME' is listed:"
  echo "$POLICY_BODY" | jq .
  echo
  echo "3. Read back the environment's reviewers and its branch policies."
  exit 0
fi

command -v gh >/dev/null 2>&1 || { echo "ERROR: 'gh' is required" >&2; exit 1; }

echo "Applying environment '$ENV_NAME' on $REPO ..."
gh api -X PUT "repos/$REPO/environments/$ENV_NAME" --input "$SPEC" \
  --jq '"  -> environment \(.name): custom_branch_policies=\(.deployment_branch_policy.custom_branch_policies)"'

# The PUT above turns custom_branch_policies on. Branch policies can be added only with it on.
# The endpoint is paginated (per-page/page), so every page is read before deciding to create.
policies="$(gh api --paginate --slurp "repos/$REPO/environments/$ENV_NAME/deployment-branch-policies")"
present="$(jq -r --arg name "$POLICY_NAME" \
  '[.[].branch_policies[]? | select(.name == $name and .type == "branch")] | length' <<<"$policies")"
if [ "$present" = "0" ]; then
  echo "Creating deployment branch policy '$POLICY_NAME' ..."
  printf '%s' "$POLICY_BODY" | gh api -X POST \
    "repos/$REPO/environments/$ENV_NAME/deployment-branch-policies" --input - \
    --jq '"  -> branch policy \(.name) (\(.type))"'
else
  echo "Deployment branch policy '$POLICY_NAME' already present; nothing to create."
fi

echo "Read back:"
gh api "repos/$REPO/environments/$ENV_NAME" --jq '
  "  environment: \(.name)",
  "  deployment_branch_policy: protected_branches=\(.deployment_branch_policy.protected_branches) custom_branch_policies=\(.deployment_branch_policy.custom_branch_policies)",
  (.protection_rules[]? | select(.type == "required_reviewers")
    | "  required_reviewers (prevent_self_review=\(.prevent_self_review)):",
      (.reviewers[]? | "    \(.type) \(.reviewer.login // .reviewer.slug // .reviewer.name // "?")"))'
gh api --paginate "repos/$REPO/environments/$ENV_NAME/deployment-branch-policies" \
  --jq '.branch_policies[]? | "  branch policy: \(.type) \(.name)"'
