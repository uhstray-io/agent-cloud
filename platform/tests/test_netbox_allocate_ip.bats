#!/usr/bin/env bats
# Structural tests for netbox-allocate-ip.yml — asking the IPAM authority for addresses.
#
# NetBox is the platform's address authority. Before this playbook the only ways to pick
# an address for a new VM were the inventory (which records what is DECLARED, not what is
# allocated) or eyeballing the network — neither is the authority. These tests hold the
# properties that make it safe to run against a live ledger.
#
# Run: bats platform/tests/test_netbox_allocate_ip.bats

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

@test "netbox-allocate: the OpenBao transport guard is included" {
  # Every play that reaches OpenBao carries the shared cleartext guard — the rule lives
  # in one file precisely because six hand-written copies drifted (§5.1).
  grep -qF 'tasks/assert-bao-transport.yml' "$PLAYBOOK"
  grep -qF '_assert_bao_url: "{{ _bao_url }}"' "$PLAYBOOK"
}

@test "netbox-allocate: no_log is scoped to the credential boundary only" {
  # no_log on a deploy or a verification hides the failure and makes a Semaphore run
  # undiagnosable. It belongs on auth, secret reads, and header construction — nowhere else.
  local nolog
  nolog=$(grep -c 'no_log: true' "$PLAYBOOK")
  [ "$nolog" -eq 3 ]
  # The address operations must remain visible.
  ! grep -A12 'available-ips' "$PLAYBOOK" | grep -q 'no_log: true'
}

@test "netbox-allocate: a sane ceiling on how many addresses one run can take" {
  grep -qF '_count | int <= 32' "$PLAYBOOK"
}

@test "netbox-allocate: carries no real addresses" {
  # Documentation and fixtures in this public repo never carry site data (§4.3).
  ! grep -qE '(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)' "$PLAYBOOK"
}
