#!/usr/bin/env bats
# Structural tests for tasks/manage-secrets.yml — the shared secret lifecycle
# every composable deploy includes.
#
# Run: bats platform/tests/test_manage_secrets.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  TASK="$REPO_ROOT/platform/playbooks/tasks/manage-secrets.yml"
  load assert_helpers
}

@test "manage-secrets: the store-back is a sibling-preserving merge, never a whole-document write" {
  # A KV-v2 POST of `_resolved` writes the FULL document, silently deleting
  # every key at the path that _secret_definitions does not declare. Measured:
  # a deploy re-run erased the service's post-deploy n8n_api_key from the store
  # (docs/MISTAKES.md 10.11). Post-deploy keys (captured API keys, seeded
  # operator credentials) legitimately live beside the deploy-managed ones, so
  # the store-back must go through the shared merge-patch implementation.
  assert_grep -q 'include_tasks: bao-merge-keys.yml' "$TASK"
  # And no direct write to the service path remains: every uri task hitting
  # secret/data in this file is a GET (fetch existing / shared reads).
  local writes
  writes=$(awk '/secret\/data/{f=1} f&&/method:/{print; f=0}' "$TASK")
  refute_grep -qE 'method: (POST|PUT|PATCH)' <<<"$writes"
}
