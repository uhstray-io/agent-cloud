#!/usr/bin/env bats
# install-qemu-guest-agent.yml: refusals that must fire before any host change.
# Run: bats platform/tests/test_install_qemu_guest_agent.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/install-qemu-guest-agent.yml"
  INV="$BATS_TEST_TMPDIR/inventory.yml"
  printf 'all:\n  children:\n    demo_svc:\n      hosts:\n        demo:\n          ansible_connection: local\n' > "$INV"
}

@test "qga: a misspelled group refuses instead of reporting success" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  run ansible-playbook -i "$INV" "$PB" -e target_service=demo_svx
  [ "$status" -ne 0 ]
  assert_contains "$output" "Inventory group 'demo_svx' is absent or empty"
  refute_contains "$output" "PLAY [Install QEMU guest agent"
}

@test "qga: a host pattern is refused before sudo resolution" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  run ansible-playbook -i "$INV" "$PB" -e target_service=all
  [ "$status" -ne 0 ]
  assert_contains "$output" "target_service must be one <name>_svc group"
  refute_contains "$output" "Resolve the sudo password"
}

@test "qga: the virtio-port check runs before the package install" {
  assert_precedes "$PB" 'Look for the guest-agent virtio port' 'ansible\.builtin\.apt:'
}

@test "qga: an unset target can never fall back to the controller" {
  refute_grep -qF "default('localhost'" "$PB"
}
