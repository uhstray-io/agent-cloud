#!/usr/bin/env bats
# Guards PRINCIPLES.md Section 3 — "Every credential is bounded in time and uses;
# TTL=0 is a defect." Any `secret_id_ttl=0` / `token_num_uses=0` in playbooks or service
# deploy code must carry the `allow: orchestrator-unlimited-ttl` marker within the few
# lines above it. The Semaphore orchestrator is the SOLE documented unlimited-TTL
# exception; every other AppRole must be bounded (default 90d / 25 uses).
#
# Run: bats platform/tests/test_approle_ttl.bats

setup() {
  REPO="$BATS_TEST_DIRNAME/../.."
  SCAN_DIRS=("$REPO/platform/playbooks" "$REPO/platform/services")
}

@test "approle: no unbounded TTL=0 except the allow-listed orchestrator" {
  local hits offenders=""
  # Match secret_id_ttl / token_num_uses set to 0 (handles `=0` and `: 0`); the
  # trailing class avoids matching a 0 inside a larger number.
  hits=$(grep -rnE 'secret_id_ttl[ =:]+0([^0-9]|$)|token_num_uses[ =:]+0([^0-9]|$)' "${SCAN_DIRS[@]}" 2>/dev/null || true)
  [ -n "$hits" ] || skip "no TTL=0 occurrences found"
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    local file line start
    file=$(printf '%s' "$hit" | cut -d: -f1)
    line=$(printf '%s' "$hit" | cut -d: -f2)
    start=$(( line > 6 ? line - 6 : 1 ))
    # The exemption is role-specific: the window must carry the marker AND name the
    # Semaphore role, so a non-Semaphore role can't bypass by pasting the marker alone.
    window=$(sed -n "${start},${line}p" "$file")
    if ! { printf '%s\n' "$window" | grep -q 'allow: orchestrator-unlimited-ttl' \
        && printf '%s\n' "$window" | grep -qi 'semaphore'; }; then
      offenders="${offenders}${file}:${line}"$'\n'
    fi
  done <<< "$hits"
  if [ -n "$offenders" ]; then
    echo "Unbounded credential TTL without the 'allow: orchestrator-unlimited-ttl' marker" >&2
    echo "(PRINCIPLES.md Section 3 — bound to 90d/25 uses, or mark the orchestrator):" >&2
    printf '%s' "$offenders" >&2
    return 1
  fi
}

@test "approle: the orchestrator unlimited-TTL exception stays present + marked" {
  # Regression: the one sanctioned 0/0 (Semaphore orchestrator) must keep its marker.
  grep -qE 'allow: orchestrator-unlimited-ttl' "$REPO/platform/services/openbao/deployment/deploy.sh"
}
