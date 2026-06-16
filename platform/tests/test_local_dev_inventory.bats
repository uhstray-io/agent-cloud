#!/usr/bin/env bats
# Structural tests for platform/inventory/local-dev.yml.example.
# Verifies the committed template is valid YAML, localhost-only by construction,
# carries all required service groups, and contains no real credentials or IPs.
#
# Run: bats platform/tests/test_local_dev_inventory.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  INVENTORY="$REPO_ROOT/platform/inventory/local-dev.yml.example"
}

# ── YAML validity ─────────────────────────────────────────────────────────────

@test "inventory example: file exists" {
  [ -f "$INVENTORY" ]
}

@test "inventory example: valid YAML (python loader)" {
  # YAML validity is already enforced repo-wide by yamllint (CI Static Analysis);
  # this is a belt-and-suspenders check that only runs where PyYAML is present.
  python3 -c "import yaml" 2>/dev/null || skip "PyYAML not available in this environment"
  run python3 -c "import yaml, sys; yaml.safe_load(open(sys.argv[1]))" "$INVENTORY"
  [ "$status" -eq 0 ]
}

# ── localhost-only safety ─────────────────────────────────────────────────────

@test "inventory example: all ansible_host values are 127.0.0.1 (localhost-only)" {
  # No host entry should point at a non-loopback IP.
  run grep -E 'ansible_host:' "$INVENTORY"
  [ "$status" -eq 0 ]
  # Every ansible_host line must equal 127.0.0.1
  while IFS= read -r line; do
    [[ "$line" =~ ansible_host:[[:space:]]*127\.0\.0\.1 ]] || {
      echo "Non-localhost ansible_host found: $line"
      return 1
    }
  done < <(grep 'ansible_host:' "$INVENTORY")
}

@test "inventory example: openbao_addr is localhost-only" {
  run grep -E 'openbao_addr:' "$INVENTORY"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "127.0.0.1" ]]
}

@test "inventory example: local_mode is true" {
  run grep -E 'local_mode:' "$INVENTORY"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "true" ]]
}

@test "inventory example: no real credentials (no values that look like passwords or tokens)" {
  # Inventory must not contain any value that looks like a real secret.
  # Placeholders like __REPO_DIR__ and LOCAL_FAKE_ are acceptable;
  # actual secrets are not.
  ! grep -qiE 'password[[:space:]]*:[[:space:]]*[^{"\047][a-zA-Z0-9._-]{8,}' "$INVENTORY"
  ! grep -qiE 'secret[[:space:]]*:[[:space:]]*[^{"\047][a-zA-Z0-9._-]{16,}' "$INVENTORY"
  ! grep -qiE 'token[[:space:]]*:[[:space:]]*[^{"\047][a-zA-Z0-9._-]{16,}' "$INVENTORY"
}

@test "inventory example: no public/routable IP addresses (RFC 1918 + loopback only)" {
  # grep for IPv4 patterns; strip known-good loopback; fail on anything else.
  # This guards against accidentally committing prod IPs into the template.
  while IFS= read -r line; do
    # Strip comment lines
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    # dns_upstreams legitimately lists PUBLIC resolvers (1.1.1.1, 1.0.0.1) — that
    # is required forwarding config, not a leak (asserted by the dns_upstreams
    # test below). Skip it so the leak-check targets host/service IPs only.
    [[ "$line" =~ dns_upstreams ]] && continue
    # Extract IP-looking strings
    while IFS= read -r ip; do
      # Allow loopback (127.x.x.x) and RFC 1918 (10., 172.16-31., 192.168.)
      # Allow loopback, RFC 1918, and the unspecified/bind-all address 0.0.0.0
      # (used for VM-side service binds, e.g. dns_listen — not a routable IP).
      if [[ "$ip" =~ ^127\. ]] || [[ "$ip" =~ ^10\. ]] || \
         [[ "$ip" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]] || \
         [[ "$ip" =~ ^192\.168\. ]] || [[ "$ip" = "0.0.0.0" ]]; then
        continue
      fi
      echo "Unexpected public IP in inventory: $ip (line: $line)"
      return 1
    done < <(grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' <<< "$line")
  done < "$INVENTORY"
}

# ── Required service groups ────────────────────────────────────────────────────

@test "inventory example: defines dns_svc group" {
  grep -q 'dns_svc:' "$INVENTORY"
}

@test "inventory example: defines step_ca_svc group" {
  grep -q 'step_ca_svc:' "$INVENTORY"
}

@test "inventory example: defines caddy_svc group" {
  grep -q 'caddy_svc:' "$INVENTORY"
}

@test "inventory example: defines authentik_svc group" {
  grep -q 'authentik_svc:' "$INVENTORY"
}

@test "inventory example: defines o11y_svc group" {
  grep -q 'o11y_svc:' "$INVENTORY"
}

@test "inventory example: defines opa_svc group" {
  grep -q 'opa_svc:' "$INVENTORY"
}

@test "inventory example: defines n8n_svc group" {
  grep -q 'n8n_svc:' "$INVENTORY"
}

@test "inventory example: defines erpnext_svc group" {
  grep -q 'erpnext_svc:' "$INVENTORY"
}

@test "inventory example: defines uhhcraft_svc group" {
  grep -q 'uhhcraft_svc:' "$INVENTORY"
}

# ── Required per-host fields ────────────────────────────────────────────────────

@test "inventory example: every host has ansible_connection: local" {
  # All hosts are localhost; every ansible_connection must be 'local'.
  run grep -E 'ansible_connection:' "$INVENTORY"
  [ "$status" -eq 0 ]
  while IFS= read -r line; do
    [[ "$line" =~ ansible_connection:[[:space:]]*local ]] || {
      echo "Non-local ansible_connection: $line"
      return 1
    }
  done < <(grep 'ansible_connection:' "$INVENTORY")
}

@test "inventory example: all service hosts declare service_name" {
  # service_name is required by the composable playbook pattern.
  run grep -c 'service_name:' "$INVENTORY"
  [ "$status" -eq 0 ]
  [ "$output" -ge 8 ]   # at least 8 concrete service hosts
}

@test "inventory example: all service hosts declare monorepo_deploy_path" {
  run grep -c 'monorepo_deploy_path:' "$INVENTORY"
  [ "$status" -eq 0 ]
  [ "$output" -ge 8 ]
}

@test "inventory example: all service hosts declare container_engine" {
  run grep -c 'container_engine:' "$INVENTORY"
  [ "$status" -eq 0 ]
  [ "$output" -ge 8 ]
}

@test "inventory example: container_engine values are podman or docker only" {
  while IFS= read -r line; do
    [[ "$line" =~ container_engine:[[:space:]]*(podman|docker) ]] || {
      echo "Unexpected container_engine: $line"
      return 1
    }
  done < <(grep 'container_engine:' "$INVENTORY")
}

# ── Placeholder substitution markers ──────────────────────────────────────────

@test "inventory example: contains __REPO_DIR__ placeholder for local_workspace_dir" {
  grep -q '__REPO_DIR__' "$INVENTORY"
}

@test "inventory example: contains __GENESIS_DIR__ placeholder for local_monorepo_dir" {
  grep -q '__GENESIS_DIR__' "$INVENTORY"
}

# ── Caddy routes structural checks ────────────────────────────────────────────

@test "inventory example: caddy_routes includes semaphore and openbao entries" {
  grep -q 'semaphore.agent-cloud.test' "$INVENTORY"
  grep -q 'openbao.agent-cloud.test' "$INVENTORY"
}

@test "inventory example: caddy_routes forward_auth gates use authentik-server:9000" {
  run grep -c 'forward_auth:.*authentik-server:9000' "$INVENTORY"
  [ "$status" -eq 0 ]
  [ "$output" -ge 2 ]   # openbao and netbox at minimum
}

@test "inventory example: caddy_routes auth entry exposes internal_https_port 8443" {
  grep -qE 'auth\.agent-cloud\.test.*internal_https_port' "$INVENTORY" ||
    grep -A2 'auth.agent-cloud.test' "$INVENTORY" | grep -q 'internal_https_port'
}

# ── DNS-specific checks ────────────────────────────────────────────────────────

@test "inventory example: dns zone is agent-cloud.test" {
  grep -q 'dns_zone: agent-cloud.test' "$INVENTORY"
}

@test "inventory example: dns_port is 5300 (mDNS/VPN-safe)" {
  grep -q 'dns_port: 5300' "$INVENTORY"
}

@test "inventory example: dns_upstreams contains public resolvers" {
  grep -q '1.1.1.1' "$INVENTORY"
}

# ── step-ca checks ────────────────────────────────────────────────────────────

@test "inventory example: step-ca name identifies it as internal CA" {
  grep -qiE 'stepca_name:.*[Ii]nternal' "$INVENTORY"
}

@test "inventory example: stepca_dns_names includes step-ca and localhost" {
  run grep 'stepca_dns_names:' "$INVENTORY"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "step-ca" ]]
  [[ "$output" =~ "localhost" ]]
}