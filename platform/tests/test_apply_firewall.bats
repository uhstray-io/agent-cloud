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

load assert_helpers

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

@test "firewall: rootful podman gets a DNS-scoped bridge INPUT allow, podman+rootful-gated, before enable" {
  # Podman runs aardvark-dns on the bridge GATEWAY (a host IP), so a container
  # resolving a sibling by name sends a DNS query INPUT to the host that
  # default-deny DROPS. Rootful podman hosts must allow that DNS query in.
  # Docker (in-netns 127.0.0.11) and rootless podman (own netns) never cross host
  # UFW, so the task is gated on _rootful AND _engine == 'podman'.
  grep -qF '_allow_bridge_dns: "{{ firewall_allow_bridge_dns | default(true) | bool }}"' "$PLAYBOOK"
  # Scoped to DNS (53/udp+tcp) — NOT a blanket allow on the whole bridge.
  grep -qF 'ufw allow in on {{ item.0 }} to any port 53 proto {{ item.1 }}' "$PLAYBOOK"
  ! grep -qF 'ufw allow in on {{ item }}' "$PLAYBOOK"
  # Detection iterates the podman networks' bridge interfaces.
  grep -qF 'podman network inspect' "$PLAYBOOK"
  # Gated on the podman engine (so Docker hosts skip it)...
  grep -qE "^\s*- _engine == 'podman'\s*$" "$PLAYBOOK"
  # ...AND on _rootful, right alongside the podman check — removing _rootful must
  # fail this test (the gate must also exclude rootless podman, not just Docker).
  grep -B1 -E "^\s*- _engine == 'podman'\s*$" "$PLAYBOOK" | grep -qE "^\s*- _rootful\s*$"
  # The bridge allow must be added BEFORE enabling UFW (anti-lockout ordering).
  local enable_line bridge_line
  enable_line=$(grep -n 'ufw --force enable' "$PLAYBOOK" | head -1 | cut -d: -f1)
  bridge_line=$(grep -n 'ufw allow in on {{ item.0 }}' "$PLAYBOOK" | head -1 | cut -d: -f1)
  [ -n "$enable_line" ] && [ -n "$bridge_line" ]
  [ "$bridge_line" -lt "$enable_line" ]
}

# ── Egress containment (firewall_deny_egress) ──────────────────────────────────
# Added for the self-hosted CI runner hosts, which run repository-authored code and
# must be denied the platform's interior (secret store, hypervisor, orchestrator) at
# the NETWORK boundary rather than merely by withholding a credential.
#
# The behavioural test at the bottom is deliberate: §2.5 in docs/MISTAKES.md records
# a guard on this very feature that was specified without ever being evaluated against
# the declarations it would judge — and which, as written, would have rejected every
# legitimate entry. Grepping for the predicate is not enough; it is executed here.

@test "firewall: firewall_deny_egress is wired to a _deny_egress loop var, default EMPTY" {
  # Default-empty is the regression guard for every EXISTING service host: a host that
  # declares no egress list must emit a byte-identical rule set to before this feature.
  grep -qF '_deny_egress: "{{ firewall_deny_egress | default([]) }}"' "$PLAYBOOK"
}

@test "firewall: egress denials emit ufw deny out, with the port clause optional" {
  # Omitting `port` denies the destination entirely; supplying it scopes the denial.
  grep -qF 'ufw deny out to {{ item.to }}' "$PLAYBOOK"
  grep -qF "(' port ' ~ item.port ~ ' proto ' ~ (item.proto | default('tcp'))) if item.port is defined else ''" "$PLAYBOOK"
  grep -qF 'loop: "{{ _deny_egress }}"' "$PLAYBOOK"
}

@test "firewall: the single-host guard predicate is present verbatim (drift guard)" {
  # This pins the predicate that the behavioural test below executes. If the playbook's
  # predicate is edited, this fails — forcing the behavioural cases to be revisited
  # rather than silently drifting away from what is actually enforced.
  grep -qF "('/' not in item.to)" "$PLAYBOOK"
  grep -qF "or ((item.to.split('/') | last) == '32')" "$PLAYBOOK"
  grep -qF "or (item.broad | default(false) | bool)" "$PLAYBOOK"
  grep -qF 'item.to not in _ssh_cidrs' "$PLAYBOOK"
  grep -qF "(item.reason | default('')) | length > 0" "$PLAYBOOK"
  # And it must reference the recorded rationale, so the next reader finds the why.
  grep -qF '§2.5' "$PLAYBOOK"
}

@test "firewall: egress denials are applied BEFORE enable (no uncontained window)" {
  local enable_line deny_line assert_line
  enable_line=$(grep -n 'ufw --force enable' "$PLAYBOOK" | head -1 | cut -d: -f1)
  deny_line=$(grep -n 'ufw deny out to' "$PLAYBOOK" | head -1 | cut -d: -f1)
  assert_line=$(grep -n 'Validate each egress denial' "$PLAYBOOK" | head -1 | cut -d: -f1)
  [ -n "$enable_line" ] && [ -n "$deny_line" ] && [ -n "$assert_line" ]
  # Validation precedes the rules, and the rules precede enabling.
  [ "$assert_line" -lt "$deny_line" ]
  [ "$deny_line" -lt "$enable_line" ]
}

@test "firewall: the default outbound policy is still allow (denials are specific)" {
  # Egress containment must be a set of specific denials evaluated ahead of the default
  # policy — NOT a flip to default-deny outbound, which would break package fetches,
  # DNS, and NTP on every host this shared playbook touches.
  grep -qF 'ufw default allow outgoing' "$PLAYBOOK"
  ! grep -qF 'ufw default deny outgoing' "$PLAYBOOK"
}

@test "firewall: the egress guard accepts real shapes and cannot express the self-lock" {
  # Behavioural, not structural: it executes the predicate the playbook enforces against
  # the declaration shapes it will really judge (docs/MISTAKES.md §2.5 — that predicate's
  # first draft would have rejected every legitimate entry).
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"

  cat > "$BATS_TEST_TMPDIR/guard.yml" <<'YAML'
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    # RFC 5737 / RFC 3849 documentation ranges standing in for the declared management
    # prefixes. Real addresses live only in the private site-config repo (§4.3).
    _ssh_cidrs: ["192.0.2.0/24", "198.51.100.0/24"]
  tasks:
    - ansible.builtin.assert:
        that:
          - (item.to | default('')) | length > 0
          - >-
            ('/' not in item.to)
            or ((item.to.split('/') | last) == '32')
            or (item.broad | default(false) | bool)
          - item.to not in _ssh_cidrs
          - >-
            (not (item.broad | default(false) | bool))
            or ((item.reason | default('')) | length > 0)
      loop: "{{ cases }}"
YAML

  guard() {
    ansible-playbook "$BATS_TEST_TMPDIR/guard.yml" -e "{\"cases\":[$1]}" >/dev/null 2>&1
  }

  # ACCEPTED — the shape the runner hosts actually declare. Every target sits INSIDE the
  # management prefix, which is exactly why the guard must not be phrased as "reject
  # anything within an SSH CIDR".
  guard '{"to":"192.0.2.164","port":8200,"comment":"secret store"}'
  guard '{"to":"192.0.2.117","port":3000,"comment":"orchestrator"}'
  guard '{"to":"192.0.2.110/32","port":8006,"comment":"hypervisor"}'
  # ACCEPTED — a wider mask OUTSIDE the management prefixes, flagged and justified.
  guard '{"to":"203.0.113.0/24","broad":true,"reason":"third-party range"}'

  # REJECTED — a supernet of the management network, which would cut the host off the LAN.
  # `run` + `[ ]` rather than `! guard`: a bang-inverted command is exempt from set -e
  # anywhere but the final line, so `! guard ...` mid-body can never fail (§2.9).
  run guard '{"to":"192.0.2.0/24"}'
  [ "$status" -ne 0 ]
  run guard '{"to":"192.0.0.0/16"}'
  [ "$status" -ne 0 ]
  # REJECTED even WITH the flag and a reason: `broad` is an escape hatch for a wider mask,
  # never a bypass of the one thing this guard exists to prevent. A target equal to a
  # declared SSH CIDR IS the self-lock, so no opt-in may express it.
  run guard '{"to":"192.0.2.0/24","broad":true,"reason":"stated"}'
  [ "$status" -ne 0 ]
  # REJECTED — broad with no stated reason, so it cannot be set in passing.
  run guard '{"to":"203.0.113.0/24","broad":true}'
  [ "$status" -ne 0 ]
  # REJECTED — a malformed entry with no destination at all.
  run guard '{"port":8200}'
  [ "$status" -ne 0 ]
}

@test "apply-firewall: the upstream source may be a LIST, and a bare string still works" {
  local pb="$PLAYBOOK"
  # A service can have more than one legitimate upstream — a reverse proxy AND
  # the automation host that drives its API. Widening the single value to a
  # CIDR would have granted every host in that subnet, so the declaration takes
  # a list instead and the rules fan out over it.
  assert_grep -qF '_upstream_sources' "$pb"
  # Backward compatibility is structural: a string is normalised to a
  # one-element list, so every host declaring a single address is untouched.
  blk=$(task_block "$pb" "Normalise the upstream source(s) to a list")
  [ -n "$blk" ]
  assert_grep -qF 'is not string' <<<"$blk"
  # Both rule families (INPUT and the rootful FORWARD mirror) fan out over
  # ports x sources, and neither reads the raw variable any more — a leftover
  # direct reference would silently apply only the first source.
  assert_grep -qF "product(_upstream_sources)" "$pb"
  [ "$(grep -c 'product(_upstream_sources)' "$pb")" -eq 2 ]
  refute_grep -qE 'ufw (route )?allow.*\{\{ firewall_upstream_source \}\}' "$pb"
  # The guard still refuses an empty declaration rather than opening nothing
  # quietly, and it now asserts on the normalised list.
  blk=$(task_block "$pb" "Require an upstream source when detected ports exist")
  assert_grep -qF '_upstream_sources | length > 0' <<<"$blk"
}
