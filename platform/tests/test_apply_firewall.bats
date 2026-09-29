#!/usr/bin/env bats
# Structural tests for apply-firewall.yml — the UFW lockdown playbook.
#
# Focus: the rootful-podman / Docker FORWARD-chain (route) path. Published ports on
# those engines are DNAT'd to the container netns, so inbound traffic crosses UFW's
# `route` (FORWARD) chain — `default deny (routed)` DROPS it — not the INPUT chain
# that `ufw allow` governs. The playbook must therefore (a) emit `ufw route allow`
# rules and (b) query the engine as root so detection actually sees the containers.
# These tests also pin the anti-lockout invariant (SSH + route allows BEFORE enable).
# The exact ufw command forms are built by filter_plugins/ufw_rules.py and asserted in
# test_ufw_rules_filter.py; the runs against a stub ufw live in
# test_apply_firewall_convergence.py.
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

@test "firewall: firewall_route_rules is wired into the declared rule set" {
  grep -qF '_route_rules: "{{ firewall_route_rules | default([]) }}"' "$PLAYBOOK"
  blk=$(task_block "$PLAYBOOK" "Compute the declared rules")
  assert_grep -qF 'route_rules=_route_rules' <<<"$blk"
}

@test "firewall: no ufw rule command or tag is hand-built in the playbook (one definition)" {
  # The command forms and the `agent-cloud:` tag are built in ONE place,
  # filter_plugins/ufw_rules.py ufw_desired_rules, and their exact text is asserted by
  # platform/tests/test_ufw_rules_filter.py (route rules proto-first, bridge DNS scoped to
  # 53, egress with an optional port). A copy here could disagree with the declared set
  # the prune step compares against, which is what the drift guard exists to catch.
  local code
  code=$(grep -vE '^\s*#' "$PLAYBOOK")
  refute_grep -qE "agent-cloud:" <<<"$code"
  refute_grep -qE 'ufw (route )?(allow|deny|limit) ' <<<"$code"
  # Both add tasks run the prepared command with its tag, and nothing else.
  for task in "Allow SSH (22/tcp) from each admin CIDR" "Add each other declared rule"; do
    blk=$(task_block "$PLAYBOOK" "$task")
    assert_grep -qF "ansible.builtin.command: \"ufw {{ item.cmd }} comment '{{ item.tag }}'\"" <<<"$blk"
  done
}

@test "firewall: DETECTED ports get a route-allow mirror, gated on _rootful" {
  # Each auto-detected published port also gets a FORWARD rule on rootful hosts, once per
  # declared upstream; rootless hosts (host-terminating ports) get none. The mirror itself
  # is ufw_desired_rules' (test_ufw_rules_filter.py
  # test_detected_ports_get_no_forward_mirror_on_a_rootless_host); here: it is fed _rootful.
  blk=$(task_block "$PLAYBOOK" "Compute the declared rules")
  assert_grep -qF 'rootful=_rootful | bool' <<<"$blk"
  assert_grep -qF 'detected=_detected.stdout_lines | default([])' <<<"$blk"
}

@test "firewall: detection runs as root on rootful/Docker (become follows _rootful)" {
  # The old hardcoded become:false silently found NO containers on rootful/Docker
  # hosts (their containers belong to root). It must now track _rootful.
  grep -qF 'become: "{{ _rootful }}"' "$PLAYBOOK"
  ! grep -qE '^\s*become:\s*false\s*$' "$PLAYBOOK"
}

@test "firewall: anti-lockout intact — SSH allows first, every add before enable, reset_connection present" {
  # The SSH allows are their own task, added before any other rule; every other add
  # (static, route, detected, bridge DNS, egress) precedes enabling UFW.
  assert_precedes "$PLAYBOOK" 'name: "Allow SSH \(22/tcp\) from each admin CIDR' 'name: "Add each other declared rule'
  assert_precedes "$PLAYBOOK" 'name: "Add each other declared rule' 'ufw --force enable'
  blk=$(task_block "$PLAYBOOK" "Allow SSH (22/tcp) from each admin CIDR")
  assert_grep -qF "loop: \"{{ _fw_to_add | selectattr('is_ssh') | list }}\"" <<<"$blk"
  blk=$(task_block "$PLAYBOOK" "Add each other declared rule")
  assert_grep -qF "loop: \"{{ _fw_to_add | rejectattr('is_ssh') | list }}\"" <<<"$blk"
  # The fresh-handshake check that proves SSH survives the firewall must remain.
  grep -q 'ansible.builtin.meta: reset_connection' "$PLAYBOOK"
}

@test "firewall: rootful podman gets a DNS-scoped bridge INPUT allow, podman+rootful-gated, before enable" {
  # Podman runs aardvark-dns on the bridge GATEWAY (a host IP), so a container
  # resolving a sibling by name sends a DNS query INPUT to the host that
  # default-deny DROPS. Rootful podman hosts must allow that DNS query in.
  # Docker (in-netns 127.0.0.11) and rootless podman (own netns) never cross host
  # UFW, so bridge detection is gated on _rootful AND _engine == 'podman'. The rule
  # itself (53/udp+tcp only, never the whole bridge) is asserted in test_ufw_rules_filter.py.
  grep -qF '_allow_bridge_dns: "{{ firewall_allow_bridge_dns | default(true) | bool }}"' "$PLAYBOOK"
  blk=$(task_block "$PLAYBOOK" "Detect podman bridge interfaces")
  assert_grep -qF 'podman network inspect' <<<"$blk"
  assert_grep -qE '^\s*- _allow_bridge_dns\s*$' <<<"$blk"
  # Removing _rootful must fail this test: the gate must also exclude rootless podman.
  assert_grep -qE '^\s*- _rootful\s*$' <<<"$blk"
  assert_grep -qE "^\s*- _engine == 'podman'\s*$" <<<"$blk"
  # The bridges found feed the declared set, which is added before enable (anti-lockout test).
  blk=$(task_block "$PLAYBOOK" "Compute the declared rules")
  assert_grep -qF 'bridges=_bridges.stdout_lines | default([])' <<<"$blk"
  assert_precedes "$PLAYBOOK" 'name: "Detect podman bridge interfaces' 'name: "Compute the declared rules'
}

# ── Egress containment (firewall_deny_egress) ──────────────────────────────────
# Added for the self-hosted CI runner hosts, which run repository-authored code and
# must be denied the platform's interior (secret store, hypervisor, orchestrator) at
# the NETWORK boundary rather than merely by withholding a credential.
#
# §2.5 in docs/MISTAKES.md records a guard on this very feature that was specified without
# ever being evaluated against the declarations it would judge — and which, as written,
# would have rejected every legitimate entry. Grepping for the predicate is not enough, so
# the predicate is a filter (ufw_egress_problems) executed in test_ufw_rules_filter.py.

@test "firewall: firewall_deny_egress is wired to _deny_egress, default EMPTY" {
  # Default-empty is the regression guard for every EXISTING service host: a host that
  # declares no egress list must emit a byte-identical rule set to before this feature.
  grep -qF '_deny_egress: "{{ firewall_deny_egress | default([]) }}"' "$PLAYBOOK"
}

@test "firewall: egress denials feed the declared rule set" {
  # Omitting `port` denies the destination entirely; supplying it scopes the denial. The
  # `ufw deny out` forms are asserted in test_ufw_rules_filter.py.
  blk=$(task_block "$PLAYBOOK" "Compute the declared rules")
  assert_grep -qF 'deny_egress=_deny_egress' <<<"$blk"
}

@test "firewall: every egress denial is judged by ufw_egress_problems against the SSH CIDRs" {
  # The guard itself (single host unless broad with a reason, never a supernet of or equal to
  # an SSH CIDR, port and proto shapes) is ufw_egress_problems, executed against the real
  # declaration shapes in test_ufw_rules_filter.py (docs/MISTAKES.md 2.5: a guard must be run
  # against what it judges, not only grepped for). Here: the playbook asserts on it, fed the
  # declared denials and the declared SSH CIDRs, and the refusal names the recorded why.
  # Behaviour end to end (refused before any rule is added): test_apply_firewall_convergence.py.
  blk=$(task_block "$PLAYBOOK" "Validate each egress denial")
  assert_grep -qF '_fw_egress_problems: "{{ _deny_egress | ufw_egress_problems(_ssh_cidrs) }}"' <<<"$blk"
  assert_grep -qF '_fw_egress_problems | length == 0' <<<"$blk"
  assert_grep -qF '§2.5' <<<"$blk"
}

@test "firewall: egress denials are applied BEFORE enable (no uncontained window)" {
  # Validation precedes building the rules, and the rules precede enabling.
  assert_precedes "$PLAYBOOK" 'name: "Validate each egress denial' 'name: "Compute the declared rules'
  assert_precedes "$PLAYBOOK" 'name: "Compute the declared rules' 'name: "Add each other declared rule'
  assert_precedes "$PLAYBOOK" 'name: "Add each other declared rule' 'ufw --force enable'
}

@test "firewall: the default outbound policy is still allow (denials are specific)" {
  # Egress containment must be a set of specific denials evaluated ahead of the default
  # policy — NOT a flip to default-deny outbound, which would break package fetches,
  # DNS, and NTP on every host this shared playbook touches.
  grep -qF 'ufw default allow outgoing' "$PLAYBOOK"
  ! grep -qF 'ufw default deny outgoing' "$PLAYBOOK"
}

@test "apply-firewall: the upstream source may be a LIST, and a bare string still works" {
  local pb="$PLAYBOOK" blk
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
  # The declared set fans out over the normalised list and never reads the raw
  # variable — a leftover direct reference would silently apply only the first source.
  blk=$(task_block "$pb" "Compute the declared rules")
  assert_grep -qF 'upstreams=_upstream_sources' <<<"$blk"
  refute_grep -qF 'firewall_upstream_source' <<<"$blk"
  # The guard still refuses an empty declaration rather than opening nothing
  # quietly, and it now asserts on the normalised list.
  blk=$(task_block "$pb" "Require an upstream source when detected ports exist")
  assert_grep -qF '_upstream_sources | length > 0' <<<"$blk"
}

@test "apply-firewall: blank upstream sources stop before either detected-port rule" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  # Execute the real normalization, guard and rule-building tasks in their original
  # order, then print the commands that would be added. Nothing reaches a firewall.
  python3 - "$PLAYBOOK" "$BATS_TEST_TMPDIR/upstream.yml" <<'PYTHON'
import json
import sys
import yaml

names = [
    "Normalise the upstream source(s) to a list",
    "Require an upstream source when detected ports exist",
    "Compute the declared rules (command, tag, stored spec), SSH first",
]
with open(sys.argv[1]) as source:
    tasks = [task for task in yaml.safe_load(source)[0]["tasks"] if task["name"] in names]
assert [task["name"] for task in tasks] == names
tasks.append({"name": "Show the rule commands",
              "ansible.builtin.debug": {"msg": "{{ _fw_desired | map(attribute='cmd') | list }}"}})
play = {"hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_ssh_cidrs": ["192.0.2.0/24"], "_rules": [], "_route_rules": [], "_deny_egress": [],
                 "_rootful": True, "_detected": {"stdout_lines": ["443 tcp", "53 udp"]}},
        "tasks": tasks}
with open(sys.argv[2], "w") as target:
    json.dump([play], target)
PYTHON

  local values
  # The lifted play sits in a temp dir; its filters come from the repository ansible.cfg,
  # which applies from the repository root whatever directory bats was started in.
  cd "$BATS_TEST_DIRNAME/../.."
  for values in '"192.0.2.10"' '["192.0.2.10","198.51.100.10"]'; do
    run ansible-playbook "$BATS_TEST_TMPDIR/upstream.yml" -e "{\"firewall_upstream_source\":$values}"
    [ "$status" -eq 0 ]
    assert_contains "$output" 'allow from 192.0.2.10 to any port 443 proto tcp'
    assert_contains "$output" 'route allow proto udp from 192.0.2.10 to any port 53'
    if [[ "$values" == *198.51.100.10* ]]; then
      assert_contains "$output" 'allow from 198.51.100.10 to any port 443 proto tcp'
      assert_contains "$output" 'route allow proto udp from 198.51.100.10 to any port 53'
    fi
  done
  for values in '["192.0.2.10",""]' '["192.0.2.10","  "]' '["192.0.2.10",null]' '""' '"  "' '[]'; do
    run ansible-playbook "$BATS_TEST_TMPDIR/upstream.yml" -e "{\"firewall_upstream_source\":$values}"
    [ "$status" -ne 0 ]
    assert_contains "$output" 'Set firewall_upstream_source'
    refute_contains "$output" 'TASK [Compute the declared rules'
  done
}

@test "firewall: the orchestrator's SSH source must be one of the SSH CIDRs, checked BEFORE enable" {
  # Change service-deployment-workflow task 4.6. Semaphore reaches every host over SSH; a
  # firewall without its source orphans the host. Compared in the spelling ufw stores, so
  # 192.0.2.5 and 192.0.2.5/32 are one source (behaviour: test_apply_firewall_convergence.py).
  pb="$PLAYBOOK"
  assert_grep -qF '(firewall_controller_cidr | ufw_address) in _fw_ssh_cidrs_stored' "$pb"
  assert_line=$(grep -nF '(firewall_controller_cidr | ufw_address) in _fw_ssh_cidrs_stored' "$pb" | head -1 | cut -d: -f1)
  enable_line=$(grep -nF 'ufw --force enable' "$pb" | head -1 | cut -d: -f1)
  [ "$assert_line" -lt "$enable_line" ]
  ssh_allow_line=$(grep -nF 'Allow SSH (22/tcp) from each admin CIDR' "$pb" | head -1 | cut -d: -f1)
  [ "$assert_line" -lt "$ssh_allow_line" ]
}
