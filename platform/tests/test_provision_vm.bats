#!/usr/bin/env bats
# Structural tests for provision-vm.yml — specifically that it is reachable from
# the orchestrator at all.
#
# The bug these pin: the playbook read its VM spec from
# site-config/proxmox/vm-specs.yml via a path that resolved INSIDE the
# agent-cloud checkout. site-config is private and the Semaphore runner never
# checks it out, so the file was never there and this playbook had never been
# Semaphore-runnable — while its template sat in the UI looking operational.
#
# Structural only (grep asserts) — no live Proxmox calls.
# Run: bats platform/tests/test_provision_vm.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  RESIZE="$REPO_ROOT/platform/playbooks/resize-vm.yml"
}

@test "provision-vm: the vm-specs lookup cannot raise when the file is absent" {
  # Absent is the NORMAL case on the runner, not a failure.
  run bash -c "grep -c \"lookup('file', _project_root + '/proxmox/vm-specs.yml', errors='ignore')\" '$PB'"
  [ "$output" = "1" ]
  # The un-guarded form must be gone.
  ! grep -qE "lookup\('file', _project_root \+ '/proxmox/vm-specs\.yml'\) \| from_yaml" "$PB"
}

@test "provision-vm: each field's OWN assignment prefers inventory over the ledger" {
  # Proving `_decl.vm_x` appears somewhere in the file proves nothing about
  # precedence. Check each resolved assignment on its own line: the inventory
  # source must appear, and must come BEFORE any ledger source on that line.
  grep -qE "_decl: .*hostvars\\[" "$PB"
  local pairs="_vmid:vm_vmid _node:vm_node _name:vm_name _cores:vm_cores _mem:vm_memory \
               _disk:vm_disk _ip:vm_ip _gw:vm_gateway _dns:vm_nameserver \
               _storage:vm_disk_storage _bridge:vm_net_bridge _netmask:vm_netmask _tags:vm_tags"
  local pair var inv line before
  for pair in $pairs; do
    var="${pair%%:*}"; inv="${pair##*:}"
    line=$(grep -E "^    ${var}: " "$PB" | head -1)
    [ -n "$line" ] || { echo "no assignment found for $var"; return 1; }
    case "$line" in
      *"_decl.$inv"*) ;;
      *) echo "$var does not read _decl.$inv"; return 1 ;;
    esac
    # Everything left of the inventory source must contain no ledger source.
    before="${line%%_decl.$inv*}"
    case "$before" in
      *"_svc."*|*"_defaults."*) echo "$var reads the ledger before inventory"; return 1 ;;
    esac
  done
}

@test "provision-vm: the completeness gate asserts every required field" {
  # Comparing the gate's position to one debug task proves neither that it checks
  # every field nor that it precedes the writes. Extract the gate and inspect it.
  awk '/Require a complete VM declaration before touching Proxmox/{f=1;next} f&&/^    - name: /{exit} f' "$PB" > "$BATS_TEST_TMPDIR/gate.txt"
  [ -s "$BATS_TEST_TMPDIR/gate.txt" ]
  local v
  for v in _vmid _node _cores _mem _disk _ip _gw _dns _storage; do
    grep -qF "($v | string | length) > 0" "$BATS_TEST_TMPDIR/gate.txt" \
      || { echo "gate does not assert $v"; return 1; }
  done
}

@test "provision-vm: the gate precedes EVERY Proxmox write" {
  # Not merely the plan display: no POST/PUT/DELETE to the Proxmox API may appear
  # before the gate.
  local gate first_write
  gate=$(grep -n 'Require a complete VM declaration' "$PB" | head -1 | cut -d: -f1)
  first_write=$(grep -nE 'method: (POST|PUT|DELETE)' "$PB" | head -1 | cut -d: -f1)
  [ -n "$gate" ]
  [ -n "$first_write" ]
  [ "$gate" -lt "$first_write" ]
}

@test "provision-vm and resize-vm read the SAME declaration" {
  # One source of truth for both, rather than two that drift. resize-vm reads the
  # sizing fields; provision-vm reads those plus the provisioning-only ones.
  for v in vm_cores vm_memory vm_disk; do
    grep -qE "$v" "$PB" || { echo "provision-vm missing $v"; return 1; }
    grep -qE "$v" "$RESIZE" || { echo "resize-vm missing $v"; return 1; }
  done
}

@test "provision-vm: a multi-host service must name which declaration to provision" {
  # `first` is an accident of file order. With two declared hosts — a pool of CI runners,
  # say — it would provision one of them and silently apply the other's overrides to it,
  # which against a live VM is a rebuild. Refusing beats guessing.
  local PB="$BATS_TEST_DIRNAME/../playbooks/provision-vm.yml"
  grep -qF '_decl_host: "{{ target_host | default(_group_hosts | first' "$PB"
  grep -qF "(_group_hosts | length) <= 1 or (target_host | default('') | length > 0)" "$PB"
  # And a named host that is not in the group is refused, not silently defaulted.
  grep -qF '_decl_host | length == 0 or _decl_host in _group_hosts' "$PB"
}

@test "proxmox-validate: a guest on an OFFLINE node cannot abort the pre-flight check" {
  # The cluster API omits `name` for a guest whose node is down. A bare item.name aborted
  # the whole validation play — and since provision-vm.yml imports it as a precondition,
  # one powered-off hypervisor blocked provisioning anywhere on the cluster. A pre-flight
  # check that fails on a degraded fleet member is inverted: that is what it is for.
  local PV="$BATS_TEST_DIRNAME/../playbooks/proxmox-validate.yml"
  grep -qF "item.name | default(" "$PV"
  grep -qF "item.node | default(" "$PV"
  grep -qF "item.status | default(" "$PV"
  # No undefaulted field may remain in that message.
  ! grep -qE 'msg: "\{\{ item\.vmid \}\}: \{\{ item\.name \}\}' "$PV"
}

@test "provision-vm: waits for the VM to be UNLOCKED, not merely for the clone task" {
  # Proxmox reports the clone task complete while still holding the config lock, so the
  # next config write fails with "can't lock file lock-<vmid>.conf". Waiting on the task
  # is necessary but not sufficient — the condition that must hold is that the VM is
  # unlocked, so that is what must be polled.
  local PB="$BATS_TEST_DIRNAME/../playbooks/provision-vm.yml"
  grep -qF 'Wait for the clone lock to clear before configuring' "$PB"
  # And the wait must require the request to have SUCCEEDED. Without that, a failed
  # request has no json.data.lock, the default makes '' == '' true, and the wait exits
  # declaring the VM unlocked on the strength of a request that never answered.
  grep -qF "(_vm_lock is succeeded)" "$PB"
  grep -qF "(_vm_lock.json.data.lock | default('')) == ''" "$PB"
  # And the wait must precede the first config write, or it protects nothing.
  local wait_line cfg_line
  wait_line=$(grep -n 'Wait for the clone lock to clear' "$PB" | head -1 | cut -d: -f1)
  cfg_line=$(grep -n 'Configure VM resources and cloud-init' "$PB" | head -1 | cut -d: -f1)
  [ "$wait_line" -lt "$cfg_line" ]
}

@test "provision-vm: a different VM at the declared vmid is a refusal, never an adoption" {
  # MISTAKES 3.5: "exists" skipped the clone and the run went on to configure the
  # foreign VM. The guard must compare name AND node and sit before the skip.
  local pb="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  assert_grep -q 'Refuse to adopt a DIFFERENT VM' "$pb"
  assert_grep -qF "(_existing.name | default('')) == _name" "$pb"
  assert_grep -qF "(_existing.node | default('')) == _node" "$pb"
  # An interrupted run (our name, still on the template's node) is RESUMED, not refused.
  assert_grep -q '_ours_pending_migrate' "$pb"
  local guard skip
  guard=$(grep -n 'Refuse to adopt a DIFFERENT VM' "$pb" | cut -d: -f1)
  skip=$(grep -n 'Skip clone if VM already exists' "$pb" | cut -d: -f1)
  [ "$guard" -lt "$skip" ]
}

@test "provision-vm: clones on the template's node, then migrates offline when the declared node differs" {
  # qm(1): --target is only allowed when the source is on SHARED storage; template
  # 9000 is on local storage, so a cross-node --target clone is refused (task 1064).
  local pb="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  # The clone body carries no `target:` any more.
  refute_grep -qE '^\s+target: "\{\{ _node \}\}"$' <(sed -n '/name: "Clone template to new VM/,/register: clone_result/p' "$pb")
  assert_grep -qE 'qemu/\{\{ _vmid \}\}/migrate' "$pb"
  assert_grep -qE 'targetstorage: "\{\{ _storage \}\}"' "$pb"
  assert_grep -q '_do_migrate' "$pb"
  # The pre-migrate wait is cluster-wide (the VM may already have left the template's node).
  assert_grep -qE 'cluster/resources\?type=vm' <(sed -n '/Wait until the VM is unlocked/,/Skip the migrate POST/p' "$pb")
  assert_grep -q 'Skip the migrate POST when the VM already sits on the declared node' "$pb"
  # After the wait the record is re-validated by name and permitted node (vmid reuse).
  assert_grep -q "Re-validate the VM's identity after the wait" "$pb"
  assert_grep -qF "in [_tmpl_node, _node]" "$pb"
  # uri reports changed:false on a POST — nothing may gate on `is changed` (CodeRabbit, PR 188).
  refute_grep -q 'is changed' "$pb"
  [ "$(grep -c 'changed_when: .*json.data is defined' "$pb")" -eq 2 ]
  # Migration is verified like the clone: task status polled, exitstatus asserted.
  assert_grep -q 'Verify migrate succeeded' "$pb"
}

@test "provision-vm: the VM starts with its node (onboot), opt-out per host" {
  # Change service-deployment-workflow task 7.2; registry step provision-vm requires onboot=1.
  local pb="$BATS_TEST_DIRNAME/../playbooks/provision-vm.yml"
  blk=$(sed -n '/name: "Configure VM resources and cloud-init"/,/status_code/p' "$pb")
  printf '%s' "$blk" | grep -qF 'onboot: "{{ _onboot }}"'
  # Read from the declared HOST: the play runs on localhost, so a bare vm_onboot misses it.
  grep -qF "_onboot: \"{{ '1' if (_decl.vm_onboot | default(true) | bool) else '0' }}\"" "$pb"
}

@test "provision-vm: the guest-agent option honours the same per-host opt-out as resize-vm" {
  local pb="$BATS_TEST_DIRNAME/../playbooks/provision-vm.yml"
  blk=$(sed -n '/name: "Configure VM resources and cloud-init"/,/status_code/p' "$pb")
  printf '%s' "$blk" | grep -qF 'agent: "{{ _agent }}"'
  grep -qF "_agent: \"{{ '1' if (_decl.vm_agent | default(true) | bool) else '0' }}\"" "$pb"
}

@test "provision-vm: refuses a declared address another inventory host claims (evaluated)" {
  # docs/MISTAKES.md 4.7: an edit left a runner declared at the gateway's address.
  # Extract the REAL guard and run it against a small inventory, both ways.
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local pb="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  python3 - "$pb" "$BATS_TEST_TMPDIR/claim.yml" "$BATS_TEST_DIRNAME" <<'PY2'
import sys, yaml
sys.path.insert(0, sys.argv[3])
import playbook_yaml
plays = yaml.safe_load(open(sys.argv[1]))
task = [t for t in playbook_yaml.tasks(plays)
        if t.get('name') == 'Refuse a declared address claimed by another inventory host'][0]
yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                 'tasks': [task]}], open(sys.argv[2], 'w'))
PY2
  # RFC 5737 documentation addresses only.
  cat > "$BATS_TEST_TMPDIR/inv.yml" <<'YAML'
all:
  hosts:
    localhost: { ansible_connection: local }
    gw: { ansible_host: 192.0.2.56, vm_ip: 192.0.2.56 }
    runner: { ansible_host: 192.0.2.54, vm_ip: 192.0.2.54 }
YAML
  # No conflict: gw's own address is not a claim against itself.
  ansible-playbook -i "$BATS_TEST_TMPDIR/inv.yml" "$BATS_TEST_TMPDIR/claim.yml" \
    -e _decl_host=gw -e _ip=192.0.2.56 >/dev/null 2>&1
  # Conflict via another host's vm_ip (the real incident) and via its ansible_host.
  sed -i.bak 's/runner: { ansible_host: 192.0.2.54, vm_ip: 192.0.2.54 }/runner: { ansible_host: 192.0.2.54, vm_ip: 192.0.2.56 }/' "$BATS_TEST_TMPDIR/inv.yml"
  run ansible-playbook -i "$BATS_TEST_TMPDIR/inv.yml" "$BATS_TEST_TMPDIR/claim.yml" -e _decl_host=gw -e _ip=192.0.2.56
  [ "$status" -ne 0 ]
  run ansible-playbook -i "$BATS_TEST_TMPDIR/inv.yml" "$BATS_TEST_TMPDIR/claim.yml" -e _decl_host=gw -e _ip=192.0.2.54
  [ "$status" -ne 0 ]
}

@test "provision-vm: the summary reports the login and runner checks, not the port wait" {
  # Semaphore task 1753: port 22 open, the login failed (no key file on the runner) and the
  # runner step was skipped, yet the summary printed "SSH: ok" and "Runner: configured".
  # Runs the REAL classify + summary tasks against injected registers.
  command -v ansible-playbook >/dev/null || skip "ansible-playbook not installed"
  local play="$BATS_TEST_TMPDIR/summary.yml"
  python3 - "$BATS_TEST_DIRNAME/../playbooks/provision-vm.yml" "$play" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
post, = (p for p in plays if p.get("hosts") == "_provisioned_vm")
tasks = {t["name"]: t for t in post["tasks"]}
classify = tasks["Classify the post-boot results"]
summary = dict(tasks["Provisioning complete"], register="summary")
common = {"_pv_vmid": 220, "_pv_name": "dns", "_pv_node": "n", "ansible_host": "x", "ansible_user": "u",
          "_pv_service": "dns", "_pv_status": "running", "_pv_agent_ok": True, "_pv_port_open": True,
          "_runner_token": "t"}
skip = {"skipped": True, "changed": False}
cases = {
    "failed": {"_login": {"failed": True, "msg": "timed out"}, "_cloudinit": skip, "_runner_env": skip, "_runner_svc": skip},
    "ok": {"_login": {"changed": False}, "_cloudinit": {"rc": 0, "changed": False},
           "_runner_env": {"changed": True}, "_runner_svc": {"changed": True}},
    "runnerfail": {"_login": {"changed": False}, "_cloudinit": {"rc": 0, "changed": False},
                   "_runner_env": {"changed": True}, "_runner_svc": {"failed": True}},
    # ignore_unreachable leaves a result that `succeeded` accepts
    "unreach": {"_login": {"changed": False}, "_cloudinit": {"unreachable": True, "changed": False},
                "_runner_env": skip, "_runner_svc": skip},
    "notoken": {"_runner_token": "", "_login": {"changed": False}, "_cloudinit": {"rc": 2, "changed": False},
                "_runner_env": skip, "_runner_svc": skip},
    # the shape --check leaves: the port wait, agent ping and every connection step skipped
    "check": {"_pv_agent_ok": False, "_pv_port_open": False,
              "_login": skip, "_cloudinit": skip, "_runner_env": skip, "_runner_svc": skip},
}
out = []
for name, regs in cases.items():
    out.append({"hosts": "localhost", "gather_facts": False, "vars": {**common, **regs},
                "tasks": [classify, summary, {"name": "save", "check_mode": False, "ansible.builtin.copy": {
                    "content": "{{ summary.msg }}", "dest": f"{sys.argv[2]}.{name}", "mode": "0600"}}]})
yaml.safe_dump(out, open(sys.argv[2], "w"))
PY
  ansible-playbook -i localhost, -c local "$play" >/dev/null
  assert_grep -qF "SSH port: open" "$play.failed"
  assert_grep -qF "SSH login: FAILED" "$play.failed"
  assert_grep -qF "cloud-init: not checked" "$play.failed"
  assert_grep -qF "Runner: not attempted" "$play.failed"
  assert_grep -qF "SSH login: ok" "$play.ok"
  assert_grep -qF "cloud-init: done" "$play.ok"
  assert_grep -qF "Runner: configured" "$play.ok"
  assert_grep -qF "Runner: FAILED starting semaphore-runner" "$play.runnerfail"
  assert_grep -qF "cloud-init: FAILED" "$play.unreach"
  assert_grep -qF "cloud-init: done with recoverable errors" "$play.notoken"
  assert_grep -qF "Runner: skipped (no runner token supplied)" "$play.notoken"
  assert_grep -qF "SSH port: pending" "$play.check"
  assert_grep -qF "Agent: pending" "$play.check"
  assert_grep -qF "SSH login: not checked" "$play.check"
}

@test "provision-vm: no post-boot step shells out to ssh or disables host key checking" {
  # The old steps ran `ssh -o StrictHostKeyChecking=no -i "{{ ssh_private_key_path }}"`;
  # that variable is set nowhere, so every Semaphore run passed `-i ""` (tasks 1753/1755).
  local pb="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  refute_grep -q 'StrictHostKeyChecking=no' "$pb"
  refute_grep -q 'ssh_private_key_path' "$pb"
  refute_grep -qE '^\s+(cmd|argv): .*\bssh\b' "$pb"
  # The login is Ansible's own connection to the declared host, added in memory.
  assert_grep -qF 'ansible.builtin.wait_for_connection:' "$pb"
  assert_grep -qF 'name: "{{ _decl_host | default(_name, true) }}"' "$pb"
  assert_grep -qF 'groups: _provisioned_vm' "$pb"
}

@test "playbooks: no command, shell, raw or script task carries a runner token" {
  # A token in a command string sits in the argv of every process that runs it and, when
  # the task fails without no_log, in Semaphore's durable task log.
  python3 - "$REPO_ROOT/platform/playbooks" <<'PY'
import pathlib, re, sys, yaml

SHELLY = {"command", "shell", "raw", "script", "ansible.builtin.command", "ansible.builtin.shell",
          "ansible.builtin.raw", "ansible.builtin.script"}
TOKEN = re.compile(r"runner_token|SEMAPHORE_RUNNER_TOKEN", re.I)

def tasks(node):
    if isinstance(node, list):
        for n in node:
            yield from tasks(n)
    elif isinstance(node, dict):
        if any(k in SHELLY for k in node):
            yield node
        for k in ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always"):
            if k in node:
                yield from tasks(node[k])

bad, scanned = [], 0
for f in sorted(pathlib.Path(sys.argv[1]).rglob("*.yml")):
    try:
        doc = yaml.safe_load(f.read_text())
    except yaml.YAMLError:
        continue
    for t in tasks(doc):
        scanned += 1
        args = {k: v for k, v in t.items() if k in SHELLY or k in ("args", "environment")}
        if TOKEN.search(yaml.safe_dump(args)):
            bad.append(f"{f.name}: {t.get('name')}")
assert scanned > 50, f"scanned only {scanned} command tasks; the walker is broken"
if bad:
    sys.exit("runner token in a command task: " + "; ".join(bad))
PY
}

@test "provision-vm: the runner-env write is the only token reader, and it is no_log" {
  python3 - "$REPO_ROOT/platform/playbooks/provision-vm.yml" <<'PY'
import sys, yaml

plays = yaml.safe_load(open(sys.argv[1]))
readers = []
for p in plays:
    for t in p.get("tasks") or []:
        body = {k: v for k, v in t.items() if k not in ("when", "name")}
        if "_runner_token" in yaml.safe_dump(body):
            readers.append(t)
names = [t["name"] for t in readers]
assert names == ["Write the Semaphore runner environment", "Classify the post-boot results"], names
write, classify = readers
assert write.get("no_log") is True, "the runner-env write must be no_log"
assert write["ansible.builtin.copy"]["mode"] == "0600"
assert write.get("become") is True
# The classifier only measures the token's length; it never renders the value.
dumped = yaml.safe_dump(classify)
assert dumped.count("_runner_token") == dumped.count("_runner_token | length"), "classifier may only test length"
PY
}

@test "provision-vm: under --check the post-boot play skips every connection step and says so" {
  # A dry run must not read as a proved login. Run the REAL post-boot play in check mode on
  # a local-connection host: if any connection step ran, the login would succeed and read "ok".
  command -v ansible-playbook >/dev/null || skip "ansible-playbook not installed"
  local play="$BATS_TEST_TMPDIR/postboot.yml" out="$BATS_TEST_TMPDIR/postboot.out"
  python3 - "$REPO_ROOT/platform/playbooks/provision-vm.yml" "$play" "$out" <<'PY'
import sys, yaml

plays = yaml.safe_load(open(sys.argv[1]))
post = dict(next(p for p in plays if p.get("hosts") == "_provisioned_vm"))
tasks = [dict(t) for t in post["tasks"]]
tasks[[t["name"] for t in tasks].index("Provisioning complete")]["register"] = "summary"
tasks.append({"name": "save", "check_mode": False, "delegate_to": "localhost",
              "ansible.builtin.copy": {"content": "{{ summary.msg }}", "dest": sys.argv[3], "mode": "0600"}})
post["tasks"] = tasks
yaml.safe_dump([post], open(sys.argv[2], "w"))
PY
  cat > "$BATS_TEST_TMPDIR/inv.yml" <<'YAML'
_provisioned_vm:
  hosts:
    vm1:
      ansible_connection: local
      ansible_host: 192.0.2.10
      ansible_user: u
      _pv_service: dns
      _pv_vmid: 220
      _pv_name: dns
      _pv_node: n
      _pv_status: running
      _pv_agent_ok: false
      _pv_port_open: false
YAML
  ansible-playbook --check -i "$BATS_TEST_TMPDIR/inv.yml" "$play" -e runner_token=fake-token \
    >"$BATS_TEST_TMPDIR/log" 2>&1 || { cat "$BATS_TEST_TMPDIR/log"; return 1; }
  assert_grep -qF "SSH login: not checked (check mode)" "$out"
  assert_grep -qF "cloud-init: not checked" "$out"
  assert_grep -qF "Runner: not checked" "$out"
  # No connection step ran — each is reported skipped by name.
  local t
  for t in "Log in over Ansible's connection" "Wait for cloud-init to finish" \
           "Write the Semaphore runner environment" "Enable and start the Semaphore runner"; do
    # The task's own section of the log: its TASK header up to the next header.
    awk -v h="TASK [$t]" 'f && /^(TASK|PLAY|RUNNING HANDLER) \[/ {exit} f {print} index($0, h) == 1 {f = 1}' \
      "$BATS_TEST_TMPDIR/log" | grep -q '^skipping' \
      || { echo "not skipped under --check: $t"; cat "$BATS_TEST_TMPDIR/log"; return 1; }
  done
  refute_grep -qF "fake-token" "$BATS_TEST_TMPDIR/log"
}

@test "provision-vm: a post-boot failure does not stop the summary or post-validate, then fails the run" {
  # Evaluated: the real post-boot and verdict plays on a local-connection host with no
  # `cloud-init` binary. The login succeeds, cloud-init fails, the play must continue to its
  # summary, and the verdict play must then fail with a diagnosis.
  command -v ansible-playbook >/dev/null || skip "ansible-playbook not installed"
  local pb="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  # Order is the contract: post-boot, then the post-validate import, then the verdict.
  local post val verdict
  post=$(grep -n 'hosts: _provisioned_vm' "$pb" | cut -d: -f1)
  val=$(grep -n 'name: "Post-validate: VM running"' "$pb" | cut -d: -f1)
  verdict=$(grep -n 'name: "Verdict: the new VM is reachable and configured"' "$pb" | cut -d: -f1)
  [ "$post" -lt "$val" ]
  [ "$val" -lt "$verdict" ]
  local play="$BATS_TEST_TMPDIR/chain.yml"
  python3 - "$pb" "$play" <<'PY'
import sys, yaml

plays = yaml.safe_load(open(sys.argv[1]))
post = next(p for p in plays if p.get("hosts") == "_provisioned_vm")
verdict = next(p for p in plays if str(p.get("name", "")).startswith("Verdict:"))
# The chain runs from a scratch dir, so the verdict's cloud-init step-result include is
# named by its absolute path.
for task in verdict["tasks"]:
    inc = task.get("ansible.builtin.include_tasks")
    if inc:
        task["ansible.builtin.include_tasks"] = sys.argv[1].rsplit("/", 1)[0] + "/" + inc
marker = {"hosts": "localhost", "gather_facts": False,
          "tasks": [{"name": "stand-in for post-validate", "ansible.builtin.debug": {"msg": "POST-VALIDATE RAN"}}]}
yaml.safe_dump([post, marker, verdict], open(sys.argv[2], "w"))
PY
  cat > "$BATS_TEST_TMPDIR/inv.yml" <<'YAML'
all:
  hosts:
    localhost: { ansible_connection: local }
_provisioned_vm:
  hosts:
    vm1:
      ansible_connection: local
      ansible_host: 192.0.2.10
      ansible_user: u
      _pv_service: dns
      _pv_vmid: 220
      _pv_name: dns
      _pv_node: n
      _pv_status: running
      _pv_agent_ok: true
      _pv_port_open: true
YAML
  local bin="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$bin"
  # A PATH with no cloud-init on it (ansible and python are resolved first).
  local path="$bin:$(dirname "$(command -v ansible-playbook)"):$(dirname "$(command -v python3)"):/usr/bin:/bin"
  if PATH="$path" command -v cloud-init >/dev/null; then skip "cloud-init installed on this machine"; fi
  run env PATH="$path" SEMAPHORE_RUNNER_TOKEN= ansible-playbook -i "$BATS_TEST_TMPDIR/inv.yml" "$play"
  [ "$status" -ne 0 ] || { echo "$output"; return 1; }
  assert_grep -qF "cloud-init: FAILED" <<<"$output"
  assert_grep -qF "POST-VALIDATE RAN" <<<"$output"
  assert_grep -qF "reported a crash or never finished" <<<"$output"
  # And with a cloud-init that finishes cleanly and no runner token, the run passes.
  printf '#!/bin/sh\necho "status: done"\nexit 0\n' > "$bin/cloud-init"
  chmod +x "$bin/cloud-init"
  run env PATH="$path" SEMAPHORE_RUNNER_TOKEN= ansible-playbook -i "$BATS_TEST_TMPDIR/inv.yml" "$play"
  [ "$status" -eq 0 ] || { echo "$output"; return 1; }
  assert_grep -qF "cloud-init: done" <<<"$output"
  assert_grep -qF "Runner: skipped (no runner token supplied)" <<<"$output"
}
