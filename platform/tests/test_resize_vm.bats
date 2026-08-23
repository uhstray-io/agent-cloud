#!/usr/bin/env bats
# Structural tests for resize-vm.yml — the composable VM-resize playbook.
#
# The safety properties are the point of this playbook, so they are what these
# tests pin: disk growth only, opt-in reboots, idempotent writes, and specs read
# from the declared vm-specs file rather than invented at launch time.
#
# Structural only (grep asserts) — no live Proxmox calls.
# Run: bats platform/tests/test_resize_vm.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/resize-vm.yml"
  TPL="$REPO_ROOT/platform/semaphore/templates.yml"
}

@test "resize-vm: exists and runs from localhost against the Proxmox API" {
  [ -f "$PB" ]
  grep -qE '^\s+hosts: localhost' "$PB"
  grep -qE 'PVEAPIToken=' "$PB"
}

@test "resize-vm: sources the Proxmox token from OpenBao, never a literal" {
  grep -qE "secret/data/services/proxmox" "$PB"
  grep -qE 'community\.hashi_vault\.hashi_vault' "$PB"
  # No token value committed.
  ! grep -qiE 'api_token:\s*[A-Za-z0-9-]{12}' "$PB"
}

@test "resize-vm: desired state comes from the declared vm-specs file" {
  # The whole design point: converge to what is written down, so the next person
  # can read the intended size instead of guessing from a launch log.
  grep -qE "proxmox/vm-specs\.yml" "$PB"
  grep -qE '_want_cores:.*_svc\.cores' "$PB"
  grep -qE '_want_memory:.*_svc\.memory' "$PB"
  grep -qE '_want_disk_gb:.*_svc\.disk' "$PB"
}

@test "resize-vm: launch overrides exist but do not require a spec entry" {
  grep -qE 'resize_cores \| default' "$PB"
  grep -qE 'resize_memory \| default' "$PB"
  grep -qE 'resize_disk \| default' "$PB"
  # Drivable by vmid alone for a VM not yet in vm-specs.
  grep -qE 'target_vmid \| default' "$PB"
  grep -qE 'target_node \| default' "$PB"
}

@test "resize-vm: REFUSES to shrink a disk" {
  # Proxmox cannot shrink without data loss, so this must be an assert that
  # fails the run — not a warning, and not an attempt.
  grep -qE 'Refuse to shrink the disk' "$PB"
  grep -qE 'ansible\.builtin\.assert' "$PB"
  grep -qE '\(_want_disk_gb \| int\) >= \(_have_disk_gb' "$PB"
}

@test "resize-vm: disk grow passes an INCREMENT so re-runs are no-ops" {
  # Proxmox's resize endpoint takes +NG. Passing the absolute target would add
  # the full size again on every run.
  grep -qE 'size: "\+\{\{ \(_want_disk_gb \| int\) - \(_have_disk_gb \| int\) \}\}G"' "$PB"
  grep -qE '\(_want_disk_gb \| int\) > \(_have_disk_gb \| int\)' "$PB"
}

@test "resize-vm: reboots are opt-in and default to off" {
  grep -qE '_allow_reboot: "\{\{ allow_reboot \| default\(false\)' "$PB"
  # The whole stop/start sequence lives in ONE block gated on the flag, so
  # asserting the block's `when:` carries it is stronger than counting
  # occurrences (the earlier version counted four per-task gates and broke when
  # the tasks were consolidated into the block — a stale test, not a regression).
  run bash -c "awk '/Restart the guest so cores\/memory take effect/{f=1} f&&/block:/{exit} f' '$PB' | grep -c '_allow_reboot | bool'"
  [ "$output" -ge 1 ]
  # And no stop/start task may sit OUTSIDE that block.
  run bash -c "awk '/^    - name: /{inblock=0} /Restart the guest so cores\/memory take effect/{inblock=1} !inblock&&/status\/(shutdown|start)/' '$PB' | grep -c 'status/'"
  [ "$output" = "0" ]
}

@test "resize-vm: a pending change is REPORTED when not rebooting" {
  # Silently leaving cores/memory staged would look like success while the guest
  # still runs on the old size.
  grep -qE 'Report a pending change that needs a reboot' "$PB"
  grep -qE 'not \(_allow_reboot \| bool\)' "$PB"
}

@test "resize-vm: config writes only happen when something actually differs" {
  grep -qE '_cfg_changes' "$PB"
  grep -qE 'when: _cfg_changes \| length > 0' "$PB"
}

@test "resize-vm: waits for the guest to stop before starting it again" {
  # Issuing start against a still-stopping guest is how a resize turns into a
  # wedged VM.
  grep -qE 'Wait for the guest to stop' "$PB"
  grep -qE "until: \(_stop_wait\.json\.data\.status \| default\(''\)\) == 'stopped'" "$PB"
  grep -qE 'Wait for the guest to be running again' "$PB"
}

@test "resize-vm: verifies by re-reading the config, not by assuming" {
  grep -qE 'Re-read the config to confirm' "$PB"
  grep -qE '_cfg_after' "$PB"
}

@test "resize-vm: reports the diff before any write" {
  # A run with allow_reboot unset is meant to be a safe preview, which only
  # works if the diff is printed before the first PUT.
  local diff_line put_line
  diff_line=$(grep -n 'Current vs desired' "$PB" | cut -d: -f1)
  put_line=$(grep -n 'Apply cores/memory to the VM config' "$PB" | cut -d: -f1)
  [ "$diff_line" -lt "$put_line" ]
}

@test "resize-vm: warns that a grown disk still needs the guest filesystem extended" {
  # Proxmox grows the block device only; forgetting this is why a resize
  # "worked" and the guest still reports the old free space.
  grep -qiE 'resize2fs|growpart|FILESYSTEM extended' "$PB"
}

@test "resize-vm: Semaphore template exists with service + reboot survey vars" {
  grep -qE '^  - name: Resize VM$' "$TPL"
  run bash -c "awk '/^  - name: Resize VM\$/,/^  - name: [^R]/' '$TPL' | grep -c 'target_service'"
  [ "$output" -ge 1 ]
  run bash -c "awk '/^  - name: Resize VM\$/,/^  - name: [^R]/' '$TPL' | grep -c 'allow_reboot'"
  [ "$output" -ge 1 ]
}

@test "resize-vm: reboot survey var defaults to false" {
  run bash -c "awk '/^  - name: Resize VM\$/,/^  - name: [^R]/' '$TPL' | grep -A 5 'name: allow_reboot' | grep -c 'default_value: \"false\"'"
  [ "$output" = "1" ]
}

@test "resize-vm: requires HTTPS for the Proxmox API" {
  # The PVE token is sent on every request; cleartext would leak it.
  grep -qE 'Require HTTPS for the Proxmox API' "$PB"
  grep -qE "_pve_host is match\('\^https://'\)" "$PB"
}

@test "resize-vm: a failed restart cannot leave the guest powered off" {
  # Without the rescue, a shutdown-wait timeout fails the play between
  # "shutdown issued" and "start issued" — turning a resize into an outage.
  grep -qE '^\s+block:' "$PB"
  grep -qE '^\s+rescue:' "$PB"
  grep -qE 'Recovery: bring a stopped guest back up' "$PB"
  # And it must only start a guest that is genuinely stopped.
  grep -qE "when: \(_rescue_state\.json\.data\.status \| default\(''\)\) == 'stopped'" "$PB"
}

@test "resize-vm: certificate verification is an inventory knob, not hardcoded" {
  # Turning verification on should be a one-line inventory change once the PVE
  # CA reaches the runner, not a code edit.
  grep -qE 'validate_certs: "\{\{ proxmox_validate_certs \| default\(false\) \| bool \}\}"' "$PB"
}

@test "resize-vm: disk size parsing cannot raise on a non-match" {
  # regex_search WITH a capture group calls .group() on None and raises, so a
  # trailing `| default(0)` never runs. This failed on the first real run.
  ! grep -qE "regex_search\('size=\(\[0-9\]\+\)G'" "$PB"
  grep -qE "regex_replace\('\^\.\*size=" "$PB"
}

@test "resize-vm: an unparseable disk size refuses instead of assuming zero" {
  # If _have_disk_gb silently became 0, the grow delta (want - have) would be the
  # FULL declared size — a 32G disk grown by another 32G — and the shrink guard
  # (want >= have) would pass. Only whole-GB sizes are understood; anything else
  # must refuse.
  grep -qE '_disk_parsed' "$PB"
  grep -qE 'Refuse a disk change when the current size could not be read' "$PB"
}
