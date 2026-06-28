#!/usr/bin/env bats
# Structural tests for apply-firewall.yml — the UFW lockdown playbook.
#
# Focus: the rootful-podman / Docker FORWARD-chain (route) path. Published ports on
# those engines are DNAT'd to the container netns, so inbound traffic crosses UFW's
# `route` (FORWARD) chain — `default deny (routed)` DROPS it — not the INPUT chain
# that `ufw allow` governs. The playbook must therefore (a) emit `ufw route allow`
# rules and (b) query the engine as root so detection actually sees the containers.
# These tests also pin the anti-lockout invariant (SSH + route allows BEFORE enable).
#
# Run: bats platform/tests/test_apply_firewall.bats

setup() {
  PLAYBOOK="$BATS_TEST_DIRNAME/../playbooks/apply-firewall.yml"
  [ -f "$PLAYBOOK" ]
}

@test "firewall: rootful flag defaults to Docker, overridable via firewall_rootful" {
  # Docker is always rootful; rootful podman hosts can't be told apart by engine
  # name, so they opt in with firewall_rootful: true.
  grep -qF "_rootful: \"{{ firewall_rootful | default(_engine == 'docker') | bool }}\"" "$PLAYBOOK"
  grep -qF "_engine: \"{{ container_engine | default('podman') }}\"" "$PLAYBOOK"
}

@test "firewall: firewall_route_rules is wired to a _route_rules loop var" {
  grep -qF '_route_rules: "{{ firewall_route_rules | default([]) }}"' "$PLAYBOOK"
}

@test "firewall: STATIC route rules emit ufw route allow (FORWARD) over _route_rules" {
  # proto-first form, matching the bootstrap hotfix syntax verified on .117.
  grep -qE 'ufw route allow proto \{\{ item\.proto \| default\(.tcp.\) \}\} from \{\{ item\.from \}\} to any port \{\{ item\.port \}\}' "$PLAYBOOK"
  # The static route task must iterate the route-rules list, not the INPUT list.
  grep -qF 'loop: "{{ _route_rules }}"' "$PLAYBOOK"
}

@test "firewall: DETECTED ports get a route-allow mirror, gated on _rootful" {
  # Each auto-detected published port must also get a FORWARD rule on rootful hosts.
  grep -qF 'ufw route allow proto {{ item.split()[1] }} from {{ firewall_upstream_source }} to any port {{ item.split()[0] }}' "$PLAYBOOK"
  # The mirror task is gated so rootless hosts (host-terminating ports) skip it.
  grep -q 'Allow each DETECTED published port on the FORWARD chain' "$PLAYBOOK"
  # A `- _rootful` when-condition must exist guarding the detected-route mirror.
  grep -qE '^\s*- _rootful\s*$' "$PLAYBOOK"
}

@test "firewall: detection runs as root on rootful/Docker (become follows _rootful)" {
  # The old hardcoded become:false silently found NO containers on rootful/Docker
  # hosts (their containers belong to root). It must now track _rootful.
  grep -qF 'become: "{{ _rootful }}"' "$PLAYBOOK"
  ! grep -qE '^\s*become:\s*false\s*$' "$PLAYBOOK"
}

@test "firewall: anti-lockout intact — SSH + route allows precede enable, reset_connection present" {
  local enable_line ssh_line route_line
  enable_line=$(grep -n 'ufw --force enable' "$PLAYBOOK" | head -1 | cut -d: -f1)
  ssh_line=$(grep -n 'to any port 22 proto tcp' "$PLAYBOOK" | head -1 | cut -d: -f1)
  route_line=$(grep -n 'ufw route allow' "$PLAYBOOK" | head -1 | cut -d: -f1)
  [ -n "$enable_line" ] && [ -n "$ssh_line" ] && [ -n "$route_line" ]
  # Both the SSH allow and the first route rule must be added BEFORE enabling UFW.
  [ "$ssh_line" -lt "$enable_line" ]
  [ "$route_line" -lt "$enable_line" ]
  # The fresh-handshake check that proves SSH survives the firewall must remain.
  grep -q 'ansible.builtin.meta: reset_connection' "$PLAYBOOK"
}
