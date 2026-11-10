#!/usr/bin/env bats
# Structural and behavioural tests for skynet's own Semaphore identity: svc-skynet at project role
# guest (read-only; interim since 2026-10-31, was task_runner), provisioned by
# platform/playbooks/provision-skynet-semaphore-access.yml, which includes the
# shared task file platform/playbooks/tasks/provision-semaphore-service-identity.yml.
#
# The shared file's request-level properties (no_log coverage, POST-only token mint, the proof's
# check-mode guard) are pinned by test_service_console.bats, which reads that same file. This file pins what
# is skynet's own: the identity, the role, the launch status, the store path, the project declaration, the
# no-fork wiring, and the store write that must leave skynet's api_token and db_password in place.
#
# Structural (YAML and grep) plus one behavioural test that runs the real project-resolution tasks under
# ansible-playbook against a scratch inventory. It touches no service.
#
# Run: bats platform/tests/test_skynet_semaphore_access.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PLAYBOOK="$REPO_ROOT/platform/playbooks/provision-skynet-semaphore-access.yml"
  CONSOLE="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  TASKS="$REPO_ROOT/platform/playbooks/tasks/provision-semaphore-service-identity.yml"
  MERGE="$REPO_ROOT/platform/playbooks/tasks/bao-merge-keys.yml"
}

@test "skynet access: play is a separate localhost play that refuses extra vars setting internal names" {
  [ -f "$PLAYBOOK" ]
  assert_grep -qF 'import_playbook: refuse-internal-extra-vars.yml' "$PLAYBOOK"
  assert_grep -qE '^  hosts: localhost$' "$PLAYBOOK"
}

@test "skynet access: identity is svc-skynet at project role guest, launch probe expects 403, store is services/skynet" {
  python3 - "$PLAYBOOK" <<'PY'
import sys, yaml
v = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]["vars"]
assert v["_svc_user"] == "svc-skynet", v["_svc_user"]
# guest holds no run permission (upstream db/ProjectUser.go:26), so the permission gate refuses the probe: 403, nothing launched.
# Interim (operator decision 2026-10-31): task_runner carries CanRunProjectTasks, and any launch right can inject extra vars into a run on the shared runner.
assert v["_svc_role"] == "guest", v["_svc_role"]
assert v["_svc_launch_status"] == 403, v["_svc_launch_status"]
assert v["_store_path"] == "services/skynet", v["_store_path"]
assert v["_svc_display_name"] == "Skynet", v["_svc_display_name"]
assert v["_svc_email"].startswith("svc-skynet@"), v["_svc_email"]
assert v["_semaphore_url"] == "http://127.0.0.1:3000", v["_semaphore_url"]
PY
}

@test "skynet access: the project is the skynet_svc host's declaration; production refuses a run with none, local_mode falls back to 1" {
  python3 - "$PLAYBOOK" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
v = play["vars"]
assert v["_skynet_svc_hosts"] == "{{ groups['skynet_svc'] | default([]) }}", v["_skynet_svc_hosts"]
assert "hostvars[_skynet_svc_hosts[0]].skynet_semaphore_project" in v["_skynet_svc_project"], v["_skynet_svc_project"]
tasks = [t for t in play["tasks"] if isinstance(t, dict)]
names = [t.get("name") for t in tasks]
refuse = tasks[names.index("Refuse a production run with no declared Semaphore project for skynet")]
assert refuse.get("when") == "not (local_mode | default(false) | bool)", refuse.get("when")
resolve = tasks[names.index("Resolve the Semaphore project skynet is bound to")]
expr = resolve["ansible.builtin.set_fact"]["_pid"]
assert "skynet_semaphore_project | default(1)" in expr, expr
PY
}

@test "skynet access: the play sends no request itself; the shared identity tasks run after the project is resolved" {
  python3 - "$PLAYBOOK" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
tasks = [t for t in play["tasks"] if isinstance(t, dict)]
assert not any("ansible.builtin.uri" in t for t in tasks), "the play itself sends a request"
names = [t.get("name") for t in tasks]
refuse = names.index("Refuse a production run with no declared Semaphore project for skynet")
resolve = names.index("Resolve the Semaphore project skynet is bound to")
inc = [i for i, t in enumerate(tasks)
       if t.get("ansible.builtin.include_tasks") == "tasks/provision-semaphore-service-identity.yml"]
assert len(inc) == 1, inc
assert refuse < resolve < inc[0], (refuse, resolve, inc)
PY
}

@test "skynet access: skynet and the console include the same shared task file; no per-service copy of the identity steps" {
  assert_grep -qF 'ansible.builtin.include_tasks: tasks/provision-semaphore-service-identity.yml' "$PLAYBOOK"
  assert_grep -qF 'ansible.builtin.include_tasks: tasks/provision-semaphore-service-identity.yml' "$CONSOLE"
  refute_grep -qE '/api/users|/api/user|/api/auth/login|/api/user/tokens' "$PLAYBOOK"
}

@test "skynet access: the transport guard is the shared file's first task, before its first OpenBao or Semaphore request" {
  assert_precedes "$TASKS" 'include_tasks: assert-bao-transport\.yml' '^[[:space:]]+ansible\.builtin\.uri:'
}

@test "skynet access: the store is written only by merge, so skynet's api_token and db_password survive" {
  python3 - "$TASKS" "$MERGE" <<'PY'
import sys, yaml
def tasks(seq):
    for t in seq or []:
        if isinstance(t, dict):
            yield t
            for sec in ("block", "rescue", "always"):
                yield from tasks(t.get(sec))
shared = list(tasks(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))))
# No direct OpenBao write in the shared file: the only request to the store path is the GET read.
for t in shared:
    u = t.get("ansible.builtin.uri")
    if u and "/v1/secret/" in str(u.get("url", "")):
        assert str(u.get("method", "GET")).upper() == "GET", (t.get("name"), u.get("method"))
# Every store write is an include of the merge task. Together they write the password and the token, and nothing else.
merges = [t["vars"]["_bm_data"] for t in shared
          if "ansible.builtin.include_tasks" in t
          and str(t["ansible.builtin.include_tasks"]).endswith("bao-merge-keys.yml")]
assert sorted(set(k for d in merges for k in d)) == ["semaphore_api_token", "semaphore_password"], merges
# The merge task updates an existing path by merge-patch (siblings kept) and creates a new one only with cas: 0.
text = open(sys.argv[2], encoding="utf-8").read()
assert "method: PATCH" in text and "application/merge-patch+json" in text, "no merge-patch update"
assert "cas: 0" in text, "no cas:0 create"
PY
}

@test "skynet access: the launch probe is POST-only with template -1, skipped under --check, and must answer the role's status" {
  python3 - "$TASKS" <<'PY'
import sys, yaml
def tasks(seq):
    for t in seq or []:
        if isinstance(t, dict):
            yield t
            for sec in ("block", "rescue", "always"):
                yield from tasks(t.get(sec))
shared = list(tasks(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))))
posts = [t for t in shared if "ansible.builtin.uri" in t
         and str(t["ansible.builtin.uri"].get("url", "")).endswith("/tasks")]
assert len(posts) == 1, len(posts)
probe = posts[0]["ansible.builtin.uri"]
assert probe["method"] == "POST" and probe["body"]["template_id"] == -1, probe
assert "not ansible_check_mode" in str(posts[0].get("when")), posts[0].get("when")
assert posts[0].get("no_log") is True, posts[0].get("name")
asserts = [t for t in shared if "ansible.builtin.assert" in t
           and "_probe_launch.status" in str(t["ansible.builtin.assert"].get("that"))]
assert len(asserts) == 1, len(asserts)
assert "_probe_launch.status == (_svc_launch_status | int)" in str(asserts[0]["ansible.builtin.assert"]["that"]), asserts[0]
PY
}

@test "skynet access: resolves the project the skynet_svc host declares; production refuses none; local_mode falls back to 1" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local wrap="$BATS_TEST_TMPDIR/provision.yml" inv="$BATS_TEST_TMPDIR/hosts.ini"
  python3 - "$PLAYBOOK" "$wrap" <<'PY'
import sys, yaml
pb, out = sys.argv[1], sys.argv[2]
play = yaml.safe_load(open(pb, encoding="utf-8"))[1]
keep = ["Refuse a production run with no declared Semaphore project for skynet",
        "Resolve the Semaphore project skynet is bound to"]
kept = [t for t in play["tasks"] if isinstance(t, dict) and t.get("name") in keep]
assert sorted(t["name"] for t in kept) == sorted(keep), [t.get("name") for t in kept]
kept.append({"name": "Show the resolved project",
             "ansible.builtin.debug": {"msg": "PID={{ _pid | default('unset') }}"}})
wrap = {"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": play["vars"], "tasks": kept}
with open(out, "w", encoding="utf-8") as f:
    yaml.safe_dump([wrap], f, sort_keys=False)
PY
  printf 'localhost ansible_connection=local\n[skynet_svc]\nskynet-test ansible_connection=local skynet_semaphore_project=7\n' > "$inv"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=false
  [ "$status" -eq 0 ]
  assert_contains "$output" "PID=7"
  # Production with no declared project: refused before any login.
  printf 'localhost ansible_connection=local\n[skynet_svc]\nskynet-test ansible_connection=local\n' > "$inv"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=false
  [ "$status" -ne 0 ]
  assert_contains "$output" "No Semaphore project is declared for skynet"
  # Local-dev with nothing declared keeps the local default, project 1.
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=true
  [ "$status" -eq 0 ]
  assert_contains "$output" "PID=1"
}

@test "skynet access: the shared tasks refuse a project id that is not a positive whole number, before any request" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local wrap="$BATS_TEST_TMPDIR/pid.yml"
  python3 - "$TASKS" "$wrap" <<'PY'
import sys, yaml
shared = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
names = [t.get("name") for t in shared if isinstance(t, dict)]
check = "Refuse a Semaphore project id that is not a positive whole number"
# It is the first task: nothing that sends a request runs before it.
assert names[0] == check, names[:3]
wrap = {"hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_svc_user": "svc-skynet"}, "tasks": [shared[0]]}
with open(sys.argv[2], "w", encoding="utf-8") as f:
    yaml.safe_dump([wrap], f, sort_keys=False)
PY
  run ansible-playbook -i localhost, "$wrap" -e _pid=7
  [ "$status" -eq 0 ]
  local bad
  for bad in '' abc 0 07 true -1; do
    run ansible-playbook -i localhost, "$wrap" -e "_pid=$bad"
    [ "$status" -ne 0 ]
    assert_contains "$output" "is not a positive whole number"
  done
}
