#!/usr/bin/env bats
# Structural tests for the hickory-dns service (platform/services/dns/deployment).
# Verifies compose, deploy.sh, templates, and the local overlay follow
# agent-cloud conventions.
#
# Run: bats platform/tests/test_service_dns.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/dns/deployment"
}

@test "dns: compose.yml pulls a pinned hickory-dns image (no :latest, no build)" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE 'hickorydns/hickory-dns:[0-9]' "$f"
  ! grep -qE 'hickory-dns:latest' "$f"
  ! grep -qE '^[[:space:]]*build:' "$f"
}

@test "dns: compose.yml env-parameterizes the published port (overlays cannot replace ports)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{DNS_LISTEN' "$f"
  grep -qE '\$\{DNS_PORT' "$f"
  grep -qE '53/udp' "$f"
  grep -qE '53/tcp' "$f"
}

@test "dns: compose.yml defines a query-based healthcheck" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'healthcheck:' "$f"
  grep -q 'dig' "$f"
}

@test "dns: deploy.sh is executable with a bash shebang" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ]
  [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
}

@test "dns: deploy.sh sources common.sh and uses the compose helper" {
  local f="$DEPLOY_DIR/deploy.sh"
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
}

@test "dns: deploy.sh is container-only (no secret generation or OpenBao)" {
  local f="$DEPLOY_DIR/deploy.sh"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_|vault|openbao|generate-secrets\.sh|generate_[[:alnum:]_]*secret[[:alnum:]_]*)\b' "$f"
  ! grep -qE 'bao-client\.sh' "$f"
  ! grep -qE 'curl[^#]*:8200\b' "$f"
}

@test "dns: deploy.sh exits non-zero when config/named.toml is missing" {
  # The config is rendered by Ansible and never committed.
  [ ! -f "$DEPLOY_DIR/config/named.toml" ]
  run env CONTAINER_ENGINE=docker bash "$DEPLOY_DIR/deploy.sh"
  [ "$status" -ne 0 ]
  [[ "$output" == *"config/named.toml"* ]]
}

@test "dns: named.toml.j2 declares a Primary zone and a forward store (no literal domain)" {
  local f="$DEPLOY_DIR/templates/named.toml.j2"
  [ -f "$f" ]
  grep -q 'zone_type = "Primary"' "$f"
  grep -q 'type = "forward"' "$f"
  grep -q '{{ dns_zone }}' "$f"
  # No real domain literal — the zone is always a variable.
  ! grep -qE 'uhstray\.io' "$f"
}

@test "dns: zone template has an SOA and a wildcard record" {
  local f="$DEPLOY_DIR/templates/zone.local-dev.j2"
  [ -f "$f" ]
  grep -q 'SOA' "$f"
  grep -qE '^\*[[:space:]]+IN[[:space:]]+A' "$f"
}

@test "dns: local overlay adds caps + SELinux opt but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  # A ports list in the overlay would APPEND to (not replace) the base — forbidden.
  ! grep -qE '^[[:space:]]*ports:' "$f"
}
