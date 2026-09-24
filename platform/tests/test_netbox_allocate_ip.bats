#!/usr/bin/env bats
# Structural tests for netbox-allocate-ip.yml — asking the IPAM authority for addresses.
#
# NetBox is the platform's address authority. Before this playbook the only ways to pick
# an address for a new VM were the inventory (which records what is DECLARED, not what is
# allocated) or eyeballing the network — neither is the authority. These tests hold the
# properties that make it safe to run against a live ledger.
#
# Run: bats platform/tests/test_netbox_allocate_ip.bats

load assert_helpers

setup() {
  PLAYBOOK="$BATS_TEST_DIRNAME/../playbooks/netbox-allocate-ip.yml"
  [ -f "$PLAYBOOK" ]
}

@test "netbox-allocate: the default mode is read-only" {
  # A run that mutates the ledger by default is one nobody can safely use to look.
  grep -qF '_reserve: "{{ reserve | default(false) | bool }}"' "$PLAYBOOK"
  # Every write is gated on it.
  local writes
  writes=$(grep -c 'method: POST' "$PLAYBOOK")
  [ "$writes" -gt 0 ]
  grep -q 'when:' "$PLAYBOOK"
  grep -qE '^\s+- _reserve$' "$PLAYBOOK"
}

@test "netbox-allocate: a reserve run must name what it records" {
  # reserve=true with an empty assignment list would otherwise do nothing and report
  # success — the false-green shape docs/MISTAKES.md §2 is about.
  grep -qF '(not _reserve) or (_assignments | length > 0)' "$PLAYBOOK"
  # And every entry must carry an address.
  grep -qF "rejectattr('address', 'defined')" "$PLAYBOOK"
}

@test "netbox-allocate: allocation takes explicit addresses, never 'the next free one'" {
  # Reserving whatever is free AT RUN TIME is not reproducible: two runs a minute apart
  # reserve different addresses and the declaration that follows disagrees with the
  # ledger. The POST body must come from the operator's list, not from the free-IP query.
  grep -qF 'address: "{{ item.item.address }}"' "$PLAYBOOK"
  ! grep -qE 'address: "\{\{ _free\.' "$PLAYBOOK"
}

@test "netbox-allocate: existence is checked before creating, per address" {
  # NetBox permits duplicate addresses in some configurations, so a blind POST can
  # produce a second record and leave the ledger ambiguous about which is authoritative.
  grep -qF '/api/ipam/ip-addresses/?address=' "$PLAYBOOK"
  grep -qF "(item.json.results | default([])) | length == 0" "$PLAYBOOK"
  # An address that already exists is left alone, not re-described.
  grep -qF "(item.json.results | default([])) | length > 0" "$PLAYBOOK"
}

@test "netbox-allocate: the prefix must already exist in the authority" {
  # Inventing an address outside a declared prefix is how a ledger stops being one.
  grep -qF '(_pfx.json.results | default([])) | length == 1' "$PLAYBOOK"
}

@test "netbox-allocate: bootstrap token can view prefixes without adding them" {
  local bootstrap="$BATS_TEST_DIRNAME/../playbooks/provision-netbox-automation-token.yml"
  assert_precedes "$bootstrap" 'Ensure NetBox automation user and scoped permissions' 'Already provisioned'
  assert_grep -qF '"skynet-ipam-prefix-view", ["view"]' "$bootstrap"
  assert_grep -qF 'app_label="ipam", model="prefix"' "$bootstrap"
  assert_grep -qF 'perm.enabled, perm.actions = True, actions' "$bootstrap"
  assert_grep -qF 'perm.users.add(user)' "$bootstrap"
}

@test "netbox-allocate: token bootstrap has a dev-bound Semaphore template" {
  local templates="$BATS_TEST_DIRNAME/../semaphore/templates.yml"
  local block
  block=$(grep -A2 -F 'name: Provision NetBox Automation Token' "$templates")
  assert_contains "$block" 'dev_variant: true'
}

@test "NetBox API consumers share version-aware credential headers" {
  python3 - "$BATS_TEST_DIRNAME/../playbooks" <<'PY'
import pathlib
import sys
import yaml
from jinja2 import Environment

root = pathlib.Path(sys.argv[1])
helper = yaml.safe_load((root / "tasks/netbox-api-headers.yml").read_text())[0]
header = helper["ansible.builtin.set_fact"]["_nb_headers"]["Authorization"]
env = Environment()
assert env.from_string(header).render(_netbox_api_token="nbt_key.value") == "Bearer nbt_key.value"
assert env.from_string(header).render(_netbox_api_token="legacyvalue") == "Token legacyvalue"
assert helper["no_log"] is True
for name in ("netbox-allocate-ip.yml", "create-netbox-device.yml"):
    tasks = yaml.safe_load((root / name).read_text())[0]["tasks"]
    auth, = (task for task in tasks if task["name"] in ("Set the NetBox auth header", "Set NetBox auth header"))
    assert auth["ansible.builtin.include_tasks"] == "tasks/netbox-api-headers.yml"
    assert auth["no_log"] is True
PY
}

@test "netbox-allocate: the OpenBao transport guard is included" {
  # Every play that reaches OpenBao carries the shared cleartext guard — the rule lives
  # in one file precisely because six hand-written copies drifted (§5.1).
  grep -qF 'tasks/assert-bao-transport.yml' "$PLAYBOOK"
  grep -qF '_assert_bao_url: "{{ _bao_url }}"' "$PLAYBOOK"
}

@test "netbox-allocate: no_log is scoped to the credential boundary only" {
  # no_log on a deploy or a verification hides the failure and makes a Semaphore run
  # undiagnosable. Only tasks handling credentials or the raw router response are hidden.
  python3 - "$PLAYBOOK" <<'PY'
import sys
import yaml

tasks = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[0]["tasks"]
hidden = {task["name"] for task in tasks if task.get("no_log") is True}
assert hidden == {
    "Authenticate to OpenBao (AppRole)",
    "Read the NetBox automation token from OpenBao",
    "Classify the credential outcome (names and verdicts only)",
    "Read the live pfSense DHCP server configuration",
    "Check the live DHCP boundary before reserving",
    "Set the NetBox auth header",
}
PY
}

@test "netbox-allocate: a sane ceiling on how many addresses one run can take" {
  grep -qF '_count | int <= 32' "$PLAYBOOK"
}

@test "netbox-allocate: carries no real addresses" {
  # Documentation and fixtures in this public repo never carry site data (§4.3).
  ! grep -qE '(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)' "$PLAYBOOK"
}

@test "netbox-allocate: the report re-reads after writing, so a created address is not reported absent" {
  # The report used to iterate the PRE-create GET, so an address this run had just created
  # (HTTP 201) still printed "NOT recorded in the authority" — the run contradicting
  # itself, and an operator reading that would retry a reservation that had succeeded.
  assert_grep -qF 'Re-read each named address after any writes' "$PLAYBOOK"
  assert_grep -qF 'register: _final_state' "$PLAYBOOK"
  assert_grep -qF 'loop: "{{ _final_state.results | default([]) }}"' "$PLAYBOOK"
  # The report must NOT read the pre-create results any more.
  # Extract the report task to a file and assert on THAT. The previous form was a no-op
  # twice over: `grep -vq` succeeds when ANY line lacks the string, so it passed with
  # `_existing.results` present, and `|| true` discarded even that. Written while fixing
  # exactly this class (docs/MISTAKES.md §2.9).
  sed -n '/Report the recorded state of each named address/,/^$/p' "$PLAYBOOK" \
    > "$BATS_TEST_TMPDIR/report.yml"
  assert_grep -qF '_final_state.results' "$BATS_TEST_TMPDIR/report.yml"
  refute_grep -qF '_existing.results' "$BATS_TEST_TMPDIR/report.yml"
}

@test "netbox-allocate: a reserve run that failed to create is not reported as success" {
  assert_grep -qF 'Refuse to report success for an address a reserve run failed to create' "$PLAYBOOK"
  assert_grep -qF 'is still not recorded after a reserve run' "$PLAYBOOK"
}

@test "netbox-allocate: a report run reads each address once, not twice" {
  # The pre-create read exists only to decide what needs creating, and the report re-reads
  # after the writes. Leaving the first read ungated made a plain report run query every
  # named address twice for one answer.
  local check
  check=$(sed -n '/Check whether each named address is already recorded/,/^$/p' "$PLAYBOOK")
  printf '%s' "$check" | grep -qF 'when: _reserve'
  # The create loop must still tolerate the skipped register: `_reserve` is evaluated
  # FIRST so the json lookup is never reached on a report run (verified behaviourally —
  # a skipped looped register yields results entries with no .json).
  local create
  create=$(sed -n '/Record each new address as allocated/,/^$/p' "$PLAYBOOK")
  printf '%s' "$create" | grep -qF '_existing.results | default([])'
  printf '%s' "$create" | grep -A2 'when:' | head -2 | grep -qF '_reserve'
}
