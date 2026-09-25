#!/usr/bin/env bats
# Workflow steps lookup-inventory and validate-address (change service-deployment-workflow
# task 7.3). The address verdict is evaluated, not grepped: the REAL "Judge the address" task
# is extracted and run against both answer shapes pfrest has returned.

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  VALIDATE="$REPO_ROOT/platform/playbooks/validate-address-free.yml"
  LOOKUP="$REPO_ROOT/platform/playbooks/lookup-service-inventory.yml"
}

# judge <arp-json> <pve-json> [vm-config-json] -> prints "hits=<n> own=<bool>"
# The VM config stands in for the Proxmox read (its NIC MACs decide ownership). It defaults to
# a NIC carrying the ARP entry's MAC, so a case that expects own=False fails only on the filter
# it is about: with no MACs every case read False whatever the id, name or status (grounding
# review of PR 195).
judge() {
  local cfg='{"data":{"net0":"virtio=BC:24:11:00:00:60,bridge=vmbr0"}}'
  [ -z "${3:-}" ] || cfg=$3
  python3 - "$VALIDATE" "$BATS_TEST_TMPDIR/judge.yml" "$1" "$2" "$cfg" <<'PY'
import json, sys, yaml
plays = yaml.safe_load(open(sys.argv[1]))
tasks = [t for p in plays for t in p["tasks"]
         if t.get("name") in ("Judge the address", "Judge whether the ARP entry is the declared VM's own")]
yaml.safe_dump([{
    "hosts": "localhost", "connection": "local", "gather_facts": False,
    # RFC 5737 documentation address only.
    "vars": {"_ip": "192.0.2.60", "_vmid": "260", "_name": "svc-vm",
             "_arp": {"json": json.loads(sys.argv[3])}, "_pve_vms": {"json": json.loads(sys.argv[4])},
             "_vm_config": {"json": json.loads(sys.argv[5])}},
    "tasks": [*tasks, {"ansible.builtin.debug": {"msg": "hits={{ _arp_hits | length }} own={{ _own_vm }}"}}],
}], open(sys.argv[2], "w"))
PY
  ansible-playbook -i localhost, "$BATS_TEST_TMPDIR/judge.yml" 2>&1 | grep -oE 'hits=[0-9]+ own=(True|False)'
}

@test "validate-address: an ARP hit is seen in the list shape and in the dict shape" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local none='{"data":[]}'
  [ "$(judge '{"data":[{"ip":"192.0.2.60","mac":"aa"}]}' "$none")" = "hits=1 own=False" ]
  [ "$(judge '{"data":{"0":{"ip":"192.0.2.60","mac":"aa"}}}' "$none")" = "hits=1 own=False" ]
  [ "$(judge '{"data":[{"ip":"192.0.2.61"}]}' "$none")" = "hits=0 own=False" ]
}

@test "validate-address: the service's own running VM owns its address (backfill is skip, not a refusal)" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local arp='{"data":[{"ip":"192.0.2.60","mac":"bc:24:11:00:00:60"}]}'
  [ "$(judge "$arp" '{"data":[{"vmid":260,"name":"svc-vm","status":"running"}]}')" = "hits=1 own=True" ]
  # running and ours by id and name, but the ARP MAC is another device's (PR 195 Codex review)
  [ "$(judge "$arp" '{"data":[{"vmid":260,"name":"svc-vm","status":"running"}]}' '{"data":{"net0":"virtio=BC:24:11:99:99:99,bridge=vmbr0"}}')" = "hits=1 own=False" ]
  # a DIFFERENT VM at the vmid, or ours by name at another vmid, owns nothing
  [ "$(judge "$arp" '{"data":[{"vmid":260,"name":"other","status":"running"}]}')" = "hits=1 own=False" ]
  [ "$(judge "$arp" '{"data":[{"vmid":261,"name":"svc-vm","status":"running"}]}')" = "hits=1 own=False" ]
  # ours but STOPPED cannot answer ARP: another device holds the address (PR 195 Codex review)
  [ "$(judge "$arp" '{"data":[{"vmid":260,"name":"svc-vm","status":"stopped"}]}')" = "hits=1 own=False" ]
  assert_grep -qF "'skip' if _own_vm else 'pass'" "$VALIDATE"
}

@test "validate-address: the one write is skipped under --check, and the reads run" {
  blk=$(sed -n '/name: "NetBox: create the VM record/,/_vm_record | length == 0/p' "$VALIDATE")
  assert_grep -qF 'not ansible_check_mode' <<<"$blk"
  [ "$(grep -c 'check_mode: false' "$VALIDATE")" -eq 4 ]
}

@test "lookup-inventory: read-only, and the address must be reserved or active in NetBox" {
  refute_grep -qE 'method: (POST|PATCH|PUT|DELETE)' "$LOOKUP"
  assert_grep -qF "_address_status in ['reserved', 'active']" "$LOOKUP"
  assert_grep -qF 'vars/declared-vm.yml' "$LOOKUP"
}

@test "lookup-inventory: the token-bearing NetBox request is no_log, and fails visibly on status" {
  # Review of PR #195: _nb_headers carries the live automation token.
  local blk
  blk=$(task_block "$LOOKUP" 'Ask the IPAM authority about the declared address')
  assert_grep -qF 'headers: "{{ _nb_headers }}"' <<<"$blk"
  assert_grep -qF 'no_log: true' <<<"$blk"
  assert_grep -qF 'failed_when: false' <<<"$blk"
  blk=$(task_block "$LOOKUP" 'Require the IPAM lookup to have answered')
  assert_grep -qF '_nb_ip.status | default(-1) == 200' <<<"$blk"
  refute_grep -qF '_nb_headers' <<<"$blk"
  assert_precedes "$LOOKUP" 'Require the IPAM lookup to have answered' 'name: "Decide"'
}
