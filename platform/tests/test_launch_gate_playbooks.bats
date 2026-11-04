#!/usr/bin/env bats
# Structural and behavioural tests for the launch gate's three playbooks (skynet-launch-containment, tasks 3.2 and 3.3):
#   platform/playbooks/deploy-launch-gate.yml                    the deploy: refusals, secrets, leaf, container, verify
#   platform/playbooks/clean-deploy-launch-gate.yml              the destructive wrapper, which runs the deploy after
#   platform/playbooks/provision-launch-gate-semaphore-access.yml the gate's own Semaphore identity (svc-launch-gate)
#
# The gate is the only identity that launches agent work on Semaphore, so each refusal in these plays is pinned here by
# its position and its shape: a refusal must come before any change, and a verify must prove the policy from inside the
# container. The gate's own behaviour is tested in test_launch_gate*.py; the container shape in test_service_launch_gate.bats.
#
# Structural (YAML and grep) plus one behavioural test: the deploy is run against a scratch inventory with a tag where a
# digest is required, and must refuse before the repo is placed. It touches no service.
#
# Run: bats platform/tests/test_launch_gate_playbooks.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY="$REPO_ROOT/platform/playbooks/deploy-launch-gate.yml"
  CLEAN="$REPO_ROOT/platform/playbooks/clean-deploy-launch-gate.yml"
  PROVISION="$REPO_ROOT/platform/playbooks/provision-launch-gate-semaphore-access.yml"
  SHARED="$REPO_ROOT/platform/playbooks/tasks/provision-semaphore-service-identity.yml"
}

# ── deploy-launch-gate.yml: the order of the plays and the guards ─────────────────────────────────────────────

@test "launch-gate deploy: the internal-names guard is the first entry, then the pre-flight names launch_gate_svc twice" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
assert doc[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml", doc[0]
pre = doc[1]
assert pre["ansible.builtin.import_playbook"] == "preflight-target-group.yml", pre
assert pre["vars"] == {"preflight_group": "launch_gate_svc", "preflight_group_expected": "launch_gate_svc"}, pre["vars"]
PY
}

@test "launch-gate deploy: three plays after the guards, Phase 1 on launch_gate_svc, Phase 2 and Phase 3 likewise" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
plays = [p for p in doc if "hosts" in p]
assert [p["name"].split(":")[0] for p in plays] == ["Phase 1", "Phase 2", "Phase 3"], [p["name"] for p in plays]
assert all(p["hosts"] == "launch_gate_svc" and p["become"] is False for p in plays)
PY
}

@test "launch-gate deploy: Phase 1 refuses the image before the repo is placed; the digest is pinned in full (no trailing newline)" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
names = [t.get("name", "") for t in play["tasks"]]
image = names.index("Refuse launch_gate_image unless it is a digest-pinned official Python image")
place = next(i for i, t in enumerate(play["tasks"]) if str(t.get("ansible.builtin.include_tasks", "")).endswith("tasks/place-monorepo.yml"))
assert image < place, (image, place)
tests = play["tasks"][image]["ansible.builtin.assert"]["that"]
assert any(t == "_launch_gate_image is match('^docker[.]io/library/python@sha256:[0-9a-f]{64}$')" for t in tests), tests
assert any("== 96" in t for t in tests), tests
PY
}

@test "launch-gate deploy: the bind is one non-wildcard IPv4 address, and production refuses a loopback bind" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = {t["name"]: t for t in play["tasks"] if "name" in t}
bind = tasks["Refuse launch_gate_bind unless it is one non-wildcard IPv4 address"]["ansible.builtin.assert"]["that"]
assert any("(?!0[.]0[.]0[.]0$)" in t for t in bind), bind
prod = tasks["Refuse a production run whose launch_gate_bind is loopback"]
assert prod["when"] == "not _launch_gate_local_mode", prod["when"]
assert "^127[.]" in prod["ansible.builtin.assert"]["that"][0], prod
PY
}

@test "launch-gate deploy: the port is refused outside 1024 to 65535, and the URLs are https (http only under local_mode)" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = {t["name"]: t for t in play["tasks"] if "name" in t}
port = tasks["Refuse launch_gate_port unless it is a whole port number from 1024 to 65535"]["ansible.builtin.assert"]["that"]
assert "(_launch_gate_port | int) >= 1024" in port and "(_launch_gate_port | int) <= 65535" in port, port
for name in ("Refuse launch_gate_semaphore_url unless it is https (http only under local_mode)",
             "Refuse launch_gate_opa_url unless it is https (http only under local_mode)"):
    text = " ".join(tasks[name]["ansible.builtin.assert"]["that"])
    assert "match('^https://')" in text and "_launch_gate_local_mode and" in text and "match('^http://')" in text, name
PY
}

@test "launch-gate deploy: callers are refused unless each has a literal token and a non-empty agent list" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = {t["name"]: t for t in play["tasks"] if "name" in t}
checks = tasks["Refuse launch_gate_callers unless every caller has a literal token and a non-empty agent list"]["ansible.builtin.assert"]["that"]
assert any("difference(_launch_gate_token_callers)" in c for c in checks), checks
assert any("map('length') | min) > 0" in c for c in checks), checks
assert play["vars"]["_launch_gate_token_callers"] == ["skynet"], play["vars"]["_launch_gate_token_callers"]
PY
}

@test "launch-gate deploy: every caller token is a literal secret definition, and semaphore_api_token is existing and required" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
v = play["vars"]
# A string here is a computed declaration, which check-secrets cannot evaluate (test_check_secrets_verdict.py).
assert isinstance(v["_secret_definitions"], list), v["_secret_definitions"]
assert v["_secret_definitions"] == [
    {"name": "caller_skynet_token", "type": "random", "length": 48},
    {"name": "semaphore_api_token", "type": "existing"},
], v["_secret_definitions"]
assert v["_required_existing_secrets"] == ["semaphore_api_token"], v["_required_existing_secrets"]
assert v["_env_templates"] == [
    {"src": "env.j2", "dest": ".env", "mode": "0600"},
    {"src": "config.json.j2", "dest": "config.json", "mode": "0644"},
], v["_env_templates"]
PY
}

@test "launch-gate deploy: internal_leaves must declare the server leaf for this host in every mode, checked before anything is issued" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = play["tasks"]
names = [t.get("name", "") for t in tasks]
leaf = tasks[names.index("Refuse a deploy whose internal_leaves does not declare the server leaf for this host")]
assert "when" not in leaf, leaf.get("when")
checks = " | ".join(leaf["ansible.builtin.assert"]["that"])
for needle in ("profile == 'server'", "host == inventory_hostname", "reload == 'none'",
               "_deploy_dir ~ '/certs/' ~ _launch_gate_server_leaf", "_launch_gate_tls_name in (_lg_leaf_decl[0].sans"):
    assert needle in checks, needle
assert leaf["vars"]["_lg_leaf_decl"].startswith("{{ internal_leaves | default([]) | selectattr('name', 'equalto', _launch_gate_server_leaf)"), leaf["vars"]
assert names.index("Refuse a deploy whose internal_leaves does not declare the server leaf for this host") < names.index("Issue the server leaf (key here, signed on the CA host)")
PY
}

@test "launch-gate deploy: the allowlist is computed in Phase 1 from OPA's catalog and templates.yml, before the repo is placed" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = play["tasks"]
names = [t.get("name", "") for t in tasks]
compute = tasks[names.index("Compute the launch allowlist from OPA's catalog and templates.yml (drift fails here)")]
expr = compute["ansible.builtin.set_fact"]["_launch_gate_allowlist"]
assert "services/opa/deployment/policies/agentcloud/data.json" in expr, expr
assert "semaphore/templates.yml" in expr, expr
assert "| launch_gate_allowlist(" in expr and "_launch_gate_agents" in expr, expr
place = next(i for i, t in enumerate(tasks) if str(t.get("ansible.builtin.include_tasks", "")).endswith("tasks/place-monorepo.yml"))
assert names.index("Compute the launch allowlist from OPA's catalog and templates.yml (drift fails here)") < place
assert names.index("Refuse an empty launch allowlist") < place
PY
}

@test "launch-gate deploy: the probe identities are fixed in Phase 1 from the same declaration (caller, its first agent, first allowlisted template)" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = {t["name"]: t for t in play["tasks"] if "name" in t}
caller = tasks["Hold the caller and template the verify play probes with"]["ansible.builtin.set_fact"]
assert caller["_launch_gate_probe_caller"] == "{{ _launch_gate_callers.keys() | list | sort | first }}", caller
assert caller["_launch_gate_probe_template"] == "{{ _launch_gate_allowlist[0] }}", caller
agent = tasks["Hold the agent the verify play probes with (the probe caller's first agent)"]["ansible.builtin.set_fact"]
assert agent["_launch_gate_probe_agent"] == "{{ _launch_gate_callers[_launch_gate_probe_caller][0] }}", agent
PY
}

@test "launch-gate deploy: production needs exactly one step_ca_svc host, and each CA host is chosen by a conditional that never indexes an absent group" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = {t["name"]: t for t in play["tasks"] if "name" in t}
one = tasks["Refuse a production deploy without exactly one step_ca_svc host to sign the server leaf"]
assert one["when"] == "not _launch_gate_local_mode", one["when"]
assert one["ansible.builtin.assert"]["that"] == ["groups.get('step_ca_svc', []) | length == 1"], one
cond = "groups['step_ca_svc'] | first if not _launch_gate_local_mode else inventory_hostname"
mint = next(t for t in play["tasks"] if t.get("name") == "Issue the server leaf (key here, signed on the CA host)")
assert mint["vars"]["_mint_ca_host"] == "{{ " + cond + " }}", mint["vars"]
assert mint["vars"]["_mint_reissue"] is False and mint["vars"]["_mint_name"] == "{{ _launch_gate_server_leaf }}", mint["vars"]
dist = next(t for t in play["tasks"] if t.get("name") == "Distribute the step-ca trust bundle into ./certs")
assert dist["vars"]["_ca_host"] == "{{ " + cond + " }}", dist["vars"]
assert dist["vars"]["_ca_root_dest_dir"] == "{{ _deploy_dir }}", dist["vars"]
PY
}

@test "launch-gate deploy: the leaf and bundle are stat-checked after issuance, and the refusal is skipped only under --check" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = play["tasks"]
names = [t.get("name", "") for t in tasks]
stat = tasks[names.index("Read the issued server leaf and the trust bundle")]
paths = stat["loop"]
assert paths == [
    "{{ _deploy_dir }}/certs/{{ _launch_gate_server_leaf }}/current/cert.pem",
    "{{ _deploy_dir }}/certs/{{ _launch_gate_server_leaf }}/current/key.pem",
    "{{ _deploy_dir }}/certs/step-ca-bundle.crt",
], paths
refuse = tasks[names.index("Refuse a deploy before its leaf and trust bundle are on the host")]
assert refuse["when"] == "not ansible_check_mode", refuse.get("when")
assert names.index("Issue the server leaf (key here, signed on the CA host)") < names.index("Distribute the step-ca trust bundle into ./certs") < names.index("Read the issued server leaf and the trust bundle")
PY
}

@test "launch-gate deploy: the env and config are rendered by manage-secrets, after the repo is placed; the secrets task is the shared one" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
tasks = play["tasks"]
inc = [str(t.get("ansible.builtin.include_tasks", "")) for t in tasks]
assert "tasks/manage-secrets.yml" in inc and "tasks/place-monorepo.yml" in inc, inc
assert inc.index("tasks/place-monorepo.yml") < inc.index("tasks/manage-secrets.yml"), inc
PY
}

@test "launch-gate deploy: deploy.sh gets the engine, LOCAL_MODE and FORCE_RECREATE, and a change is read from DEPLOY_CHANGED" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][1]
run = next(t for t in play["tasks"] if t.get("name") == "Run deploy.sh (container lifecycle)")
env = run["environment"]
assert env["CONTAINER_ENGINE"] == "{{ _engine }}", env
assert "LOCAL_MODE" in env and "FORCE_RECREATE" in env, env
assert run["changed_when"] == "'DEPLOY_CHANGED=true' in _deploy.stdout_lines", run["changed_when"]
assert run["when"] == "not ansible_check_mode", run["when"]
PY
}

@test "launch-gate deploy: Phase 3 proves the TLS listener from this host (200, pinned to the bundle) and refuses plain-HTTP 200" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][2]
v = play["vars"]
args = " ".join(str(a) for a in v["_tls_args"])
assert "--cacert {{ _deploy_dir }}/certs/step-ca-bundle.crt" in args, args
assert "--resolve {{ _launch_gate_tls_name }}:{{ _launch_gate_port }}:{{ _launch_gate_bind }}" in args, args
assert "%{http_code}" in args, args
tasks = {t["name"]: t for t in play["tasks"] if "name" in t}
health = tasks["Health over TLS from this host (GET /healthz -> 200, against the step-ca bundle)"]
assert health["ansible.builtin.command"]["argv"] == "{{ _tls_args + ['https://' ~ _launch_gate_tls_name ~ ':' ~ _launch_gate_port ~ '/healthz'] }}", health
assert health["check_mode"] is False, health
plain = tasks["Plain HTTP to the TLS listener must not answer 200"]
assert plain["ansible.builtin.command"]["argv"][-1] == "http://{{ _launch_gate_bind }}:{{ _launch_gate_port }}/healthz", plain
assert tasks["Refuse a plain-HTTP 200 from the TLS listener"]["ansible.builtin.assert"]["that"] == ["_launch_gate_plain_http.stdout | trim != '200'"]
PY
}

@test "launch-gate deploy: Phase 3 probes from inside the container: 401 unauthorized, 403 template_not_allowed, 400 undeclared_field" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][2]
tasks = {t["name"]: t for t in play["tasks"] if "name" in t}
probe = tasks["Probe the gate from inside its container (401, 403 and 400 from its own listener)"]
argv = probe["ansible.builtin.command"]["argv"]
assert argv[:4] == ["{{ _engine }}", "exec", "launch-gate", "python"], argv[:4]
assert argv[4:6] == ["-I", "-c"], argv[4:6]
script = argv[6]
for needle in ('os.environ["LAUNCH_GATE_CALLER_TOKEN_"', 'launch("no-token", None,', 'launch("other-template", token, {**verify, "template": "__agent_cloud_not_a_template__"})',
               'launch("allowed-template", token,', '"__agent_cloud_verify__": "x"', 'cafile="/certs/step-ca-bundle.crt"',
               'minimum_version = ssl.TLSVersion.TLSv1_2', 'server_hostname=name'):
    assert needle in script, needle
assert "127.0.0.1" in script and "8443" in script, script
# The tokens reach the probe through the container's environment only: argv carries names, never a Bearer value.
assert not any("Bearer" in str(a) or "Authorization" in str(a) for a in argv[7:]), argv[7:]
assert argv[7:] == ["{{ _launch_gate_tls_name }}", "{{ _launch_gate_probe_caller }}", "{{ _launch_gate_probe_agent }}", "{{ _launch_gate_probe_template }}"], argv[7:]
assert probe["when"] == "not ansible_check_mode", probe.get("when")
assert probe["check_mode"] is False and probe["changed_when"] is False, probe
asserted = tasks["Assert the gate refused each probe as the policy requires"]["ansible.builtin.assert"]["that"]
assert asserted == [
    "'no-token 401 unauthorized' in _launch_gate_probes.stdout_lines",
    "'other-template 403 template_not_allowed' in _launch_gate_probes.stdout_lines",
    "'allowed-template 400 undeclared_field' in _launch_gate_probes.stdout_lines",
], asserted
PY
}

@test "launch-gate deploy: no verify task is no_log, so a failure is visible; the caller token is never in a visible task's text" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][2]
for task in play["tasks"]:
    assert "no_log" not in task, task.get("name")
    assert "LAUNCH_GATE_CALLER_TOKEN" not in yaml.safe_dump(task) or "os.environ" in yaml.safe_dump(task), task.get("name")
PY
}

@test "launch-gate deploy: Phase 1 and Phase 3 render the same bind, port and TLS name by default (the probes check what Phase 1 serves)" {
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
plays = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p]
one, three = plays[0]["vars"], plays[2]["vars"]
for key in ("_launch_gate_bind", "_launch_gate_port", "_launch_gate_tls_name"):
    assert one[key] == three[key], (key, one[key], three[key])
# A plain default(), as renew-internal-certs.yml's listener table: '' means '' to both, and is refused.
assert one["_launch_gate_bind"] == "{{ launch_gate_bind | default('127.0.0.1') | string }}", one["_launch_gate_bind"]
assert one["_launch_gate_port"] == "{{ launch_gate_port | default('8443') | string }}", one["_launch_gate_port"]
assert one["_launch_gate_server_leaf"] == "{{ launch_gate_server_leaf | default('launch-gate-server') | string }}", one["_launch_gate_server_leaf"]
PY
}

@test "launch-gate deploy: every register and set_fact name is underscore-prefixed (the extra-var guard covers them)" {
  python3 - "$DEPLOY" "$CLEAN" "$PROVISION" <<'PY'
import sys, yaml
bad = []
def walk(node, path):
    if isinstance(node, list):
        for item in node:
            walk(item, path)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in ("ansible.builtin.set_fact", "set_fact") and isinstance(value, dict):
                bad.extend(f"{path}:{name}" for name in value if not name.startswith("_") and name != "cacheable")
            if key == "register" and isinstance(value, str) and not value.startswith("_"):
                bad.append(f"{path}:register {value}")
            walk(value, path)
for path in sys.argv[1:]:
    walk(yaml.safe_load(open(path, encoding="utf-8")), path.rsplit("/", 1)[-1])
assert not bad, bad
PY
}

# ── clean-deploy-launch-gate.yml ──────────────────────────────────────────────────────────────────────────────

@test "launch-gate clean deploy: refuses internal names first, destroys through clean-service on launch_gate_svc, then runs the deploy nested" {
  python3 - "$CLEAN" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
assert doc[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml", doc[0]
clean = doc[1]
assert clean["hosts"] == "launch_gate_svc" and clean["become"] is False, clean
assert clean["tasks"][0]["ansible.builtin.include_tasks"] == "tasks/clean-service.yml", clean["tasks"]
fresh = doc[2]
assert fresh["import_playbook"] == "deploy-launch-gate.yml", fresh
assert fresh["vars"] == {"_extra_var_guard_nested": True}, fresh["vars"]
PY
}

# ── provision-launch-gate-semaphore-access.yml ────────────────────────────────────────────────────────────────

@test "launch-gate access: the internal-names guard comes first and the play is a localhost play with a fixed loopback API" {
  python3 - "$PROVISION" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
assert doc[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml", doc[0]
play = doc[1]
assert play["hosts"] == "localhost" and play["connection"] == "local", play
assert play["vars"]["_semaphore_url"] == "http://127.0.0.1:3000", play["vars"]["_semaphore_url"]
PY
}

@test "launch-gate access: identity is svc-launch-gate at task_runner, launch probe expects 404, store is services/launch-gate" {
  python3 - "$PROVISION" <<'PY'
import sys, yaml
v = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]["vars"]
assert v["_svc_user"] == "svc-launch-gate", v["_svc_user"]
# task_runner passes the permission gate and finds no template -1, so the probe answers 404 and launches nothing.
assert v["_svc_role"] == "task_runner", v["_svc_role"]
assert v["_svc_launch_status"] == 404, v["_svc_launch_status"]
assert v["_store_path"] == "services/launch-gate", v["_store_path"]
assert v["_svc_display_name"] == "launch gate", v["_svc_display_name"]
assert v["_svc_email"].startswith("svc-launch-gate@"), v["_svc_email"]
PY
}

@test "launch-gate access: the project is the launch_gate_svc host's declaration; production refuses a run with none; local_mode falls back to 1" {
  python3 - "$PROVISION" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
v = play["vars"]
assert v["_launch_gate_svc_hosts"] == "{{ groups['launch_gate_svc'] | default([]) }}", v["_launch_gate_svc_hosts"]
assert "hostvars[_launch_gate_svc_hosts[0]].launch_gate_semaphore_project" in v["_launch_gate_svc_project"], v["_launch_gate_svc_project"]
tasks = [t for t in play["tasks"] if isinstance(t, dict)]
names = [t.get("name") for t in tasks]
refuse = tasks[names.index("Refuse a production run with no declared Semaphore project for the launch gate")]
assert refuse["when"] == "not (local_mode | default(false) | bool)", refuse.get("when")
resolve = tasks[names.index("Resolve the Semaphore project the launch gate is bound to")]
assert "launch_gate_semaphore_project | default(1)" in resolve["ansible.builtin.set_fact"]["_pid"], resolve
assert names.index("Refuse a production run with no declared Semaphore project for the launch gate") < names.index("Resolve the Semaphore project the launch gate is bound to")
PY
}

@test "launch-gate access: the account, membership, token and proof come from the shared task file, with no second copy of its API calls" {
  python3 - "$PROVISION" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
tasks = [t for t in play["tasks"] if isinstance(t, dict)]
assert tasks[-1]["ansible.builtin.include_tasks"] == "tasks/provision-semaphore-service-identity.yml", tasks[-1]
PY
  refute_grep -qE '/api/(users|user|project|auth)' "$PROVISION"
  python3 - "$PROVISION" <<'PY'
import sys
# The header documents the store keys in comments; only a live line may name them.
live = [line for line in open(sys.argv[1], encoding="utf-8") if line.strip() and not line.lstrip().startswith("#")]
assert not [line for line in live if "semaphore_password" in line or "semaphore_api_token" in line], live
PY
}

@test "launch-gate access: the header says why this account holds task_runner, and that it is the gate's only launch right" {
  assert_grep -qF 'Why task_runner here, and why one account only.' "$PROVISION"
  assert_grep -qF 'containment is the gate, not this role' "$PROVISION"
}

# ── Behaviour: the deploy refuses a non-digest image before anything changes ──────────────────────────────────

@test "launch-gate deploy: a tag where a digest is required is refused before the repo is placed, and nothing is written" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local inv="$BATS_TEST_TMPDIR/hosts.ini"
  cat > "$inv" <<'INV'
[launch_gate_svc]
lg-test-local ansible_connection=local

[launch_gate_svc:vars]
local_mode=true
service_name=launch-gate
monorepo_deploy_path=platform/services/launch-gate/deployment
openbao_addr=http://127.0.0.1:8200
launch_gate_image=docker.io/library/python:3.12-slim
launch_gate_semaphore_url=http://127.0.0.1:3000
launch_gate_semaphore_project=1
launch_gate_opa_url=http://127.0.0.1:8181
INV
  run ansible-playbook -i "$inv" "$DEPLOY" -e local_monorepo_dir="$BATS_TEST_TMPDIR/monorepo"
  [ "$status" -ne 0 ]
  assert_contains "$output" 'Refuse launch_gate_image unless it is a digest-pinned official Python image'
  assert_contains "$output" 'launch_gate_image must be a digest-pinned reference'
  refute_contains "$output" 'Place the monorepo'
  [ ! -e "$BATS_TEST_TMPDIR/monorepo" ]
}

@test "launch-gate deploy: -e local_mode=true on a host that is not <name>-local does not lift the production refusals" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local inv="$BATS_TEST_TMPDIR/hosts.ini"
  # A production-shaped host (no -local suffix) on the loopback default bind. With the public flag alone
  # selecting local-dev, the loopback refusal would be skipped; the mode is the host's identity instead.
  cat > "$inv" <<'INV'
[launch_gate_svc]
lg-prod ansible_connection=local

[launch_gate_svc:vars]
service_name=launch-gate
monorepo_deploy_path=platform/services/launch-gate/deployment
openbao_addr=http://127.0.0.1:8200
launch_gate_image=docker.io/library/python@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
launch_gate_semaphore_url=https://semaphore.example.test
launch_gate_semaphore_project=1
launch_gate_opa_url=https://opa.example.test:8281
INV
  run ansible-playbook -i "$inv" "$DEPLOY" -e local_mode=true -e local_monorepo_dir="$BATS_TEST_TMPDIR/monorepo"
  [ "$status" -ne 0 ]
  assert_contains "$output" 'Refuse a production run whose launch_gate_bind is loopback'
  assert_contains "$output" 'Production launch-gate must publish on the VM'
  refute_contains "$output" 'Place the monorepo'
  [ ! -e "$BATS_TEST_TMPDIR/monorepo" ]
}
