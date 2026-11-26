#!/usr/bin/env bats
# Local-dev wiring for the launch gate (skynet-launch-containment, task 3.4, the local half). Covers:
#   platform/playbooks/bootstrap-local-dev.yml  the local Semaphore's openssl, and the local inventory's launch_gate_svc
#   platform/inventory/local-dev.yml.example    the host-side copy of that declaration
#   platform/services/launch-gate/deployment/compose.local.yml  the local uid and the TLS-name network alias
#   platform/playbooks/deploy-launch-gate.yml   the Phase 3 probe target under local_mode (and production unchanged)
#
# The behaviour checks RENDER the repository's own expressions: the bootstrap's INI is rendered by ansible and read
# back by ansible-inventory (the plugin's ast.literal_eval reads the leaf list), and the Phase 3 argv expressions are
# evaluated by ansible-playbook for a local host and a production host. Nothing is copied into this file.
#
# Run: bats platform/tests/test_launch_gate_local_dev.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  BOOTSTRAP="$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
  DEPLOY="$REPO_ROOT/platform/playbooks/deploy-launch-gate.yml"
  EXAMPLE="$REPO_ROOT/platform/inventory/local-dev.yml.example"
  DEPLOY_DIR="$REPO_ROOT/platform/services/launch-gate/deployment"
  IMAGE="docker.io/library/python@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"
}

# ── bootstrap: the local Semaphore has openssl (decision 1) ───────────────────────────────────────────────────────

@test "launch-gate local: the local Semaphore install step adds openssl beside podman, each guarded so a warm re-run adds nothing" {
  python3 - "$BOOTSTRAP" <<'PY'
import sys, yaml
tasks = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[0]["tasks"]
[task] = [t for t in tasks if isinstance(t, dict) and str(t.get("name", "")).startswith("Install podman and openssl inside Semaphore")]
cmd = task["ansible.builtin.command"]
assert "--user root" in cmd, cmd
assert "command -v podman >/dev/null || apk add --no-cache podman" in cmd, cmd
assert "command -v openssl >/dev/null || apk add --no-cache openssl" in cmd, cmd
PY
}

@test "launch-gate local: a failed podman install fails the step even when openssl is already present" {
  # Run the step's own shell script against a PATH holding only a stub apk that fails for podman and an openssl
  # that is "present": the guard order must not let the openssl half's success hide the podman failure.
  local inner stub="$BATS_TEST_TMPDIR/bin"
  inner="$(python3 - "$BOOTSTRAP" <<'PY'
import re, sys, yaml
tasks = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[0]["tasks"]
[task] = [t for t in tasks if isinstance(t, dict) and str(t.get("name", "")).startswith("Install podman and openssl inside Semaphore")]
print(re.search(r'sh -c\s+"(.*)"\s*$', task["ansible.builtin.command"], re.S).group(1))
PY
)"
  mkdir -p "$stub"
  printf '#!/bin/sh\ncase "$*" in *podman*) exit 1;; esac\nexit 0\n' > "$stub/apk"
  printf '#!/bin/sh\nexit 0\n' > "$stub/openssl"
  chmod +x "$stub/apk" "$stub/openssl"
  run env PATH="$stub" /bin/sh -c "$inner"
  [ "$status" -ne 0 ]
}

# ── bootstrap: the local inventory declares launch_gate_svc (decision 2) ──────────────────────────────────────────

@test "launch-gate local: the local inventory's launch_gate_svc carries the decided values, with no underscore variable" {
  python3 - "$BOOTSTRAP" <<'PY'
import re, sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[0]
ini = next(t for t in play["tasks"] if t.get("name") == "Write the local inventory INI source")["vars"]["_inv_ini"]
# YAML's block scalar removes the file's indentation, so the INI text has the group header at column 0.
assert "\n[launch_gate_svc]\nlaunch-gate-local ansible_host=127.0.0.1 ansible_connection=local\n" in ini
# The group's own section: from its header to the next group header (indented, as the INI is written).
block = re.split(r"\n\s*\[", ini.split("[launch_gate_svc:vars]", 1)[1], maxsplit=1)[0]
lines = {line.strip() for line in block.splitlines() if line.strip() and not line.strip().startswith("#")}
for want in (
    "ansible_user=semaphore", "service_name=launch-gate", "monorepo_deploy_path=platform/services/launch-gate/deployment",
    "container_engine=podman", "local_mode=true", "launch_gate_image={{ _launch_gate_image }}",
    "launch_gate_bind=127.0.0.1", "launch_gate_port=18443", "launch_gate_semaphore_url=http://{{ _sem_name }}:3000",
    "launch_gate_semaphore_project={{ _pid }}", "launch_gate_opa_url=http://opa:8181", "dns_site=local",
    "dns_zone={{ _dev_zone }}", "internal_leaves={{ _launch_gate_leaves | string }}",
    "local_monorepo_dir=/var/lib/agent-cloud-deploy/agent-cloud", "local_home_dir=/var/lib/agent-cloud-deploy",
):
    assert want in lines, want
# An inventory may not define a variable whose name starts with an underscore (refuse-internal-extra-vars.yml).
assert not [line for line in lines if re.match(r"_", line)], lines
# The port is 18443 because Caddy holds 8443 on the Mac; a bare 8443 would be the collision the comment explains.
assert "launch_gate_port=8443" not in lines
# The image default is the digest-pinned form deploy-launch-gate.yml accepts (96 characters, a digest, no tag).
default = re.search(r"_launch_gate_image: \"\{\{ local_launch_gate_image \| default\('([^']+)'\) \}\}\"", open(sys.argv[1], encoding="utf-8").read())
assert default, "no _launch_gate_image default"
assert re.fullmatch(r"docker[.]io/library/python@sha256:[0-9a-f]{64}", default.group(1)), default.group(1)
PY
}

# ── bootstrap: the rendered inventory parses, and the leaf reads back as declared (decision 2, decision 6) ────────

@test "launch-gate local: the rendered inventory parses through ansible-inventory, and launch-gate-local carries its leaf as a list" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  command -v ansible-inventory >/dev/null 2>&1 || skip "ansible-inventory not available"
  local work="$BATS_TEST_TMPDIR/render"
  mkdir -p "$work"
  python3 - "$BOOTSTRAP" "$work" <<'PY'
import sys, yaml
bootstrap, work = sys.argv[1], sys.argv[2]
play = yaml.safe_load(open(bootstrap, encoding="utf-8"))[0]
task = next(t for t in play["tasks"] if t.get("name") == "Write the local inventory INI source")
variables = dict(play["vars"])
variables.update(task["vars"])                      # _dev_zone and _inv_ini
variables.update({"_pid": 1, "_o11y_projection_stat": {"stat": {"exists": False}}})
render = [{"hosts": "localhost", "gather_facts": False, "connection": "local", "vars": variables, "tasks": [
    {"name": "Render the local inventory", "ansible.builtin.copy": {"content": "{{ _inv_ini }}", "dest": work + "/local-inventory.ini"}}]}]
open(work + "/render.yml", "w", encoding="utf-8").write(yaml.safe_dump(render, sort_keys=False))
PY
  ansible-playbook -i localhost, "$work/render.yml" > "$work/render.log" 2>&1
  ansible-inventory -i "$work/local-inventory.ini" --host launch-gate-local > "$work/host.json"
  python3 - "$work/host.json" <<'PY'
import json, sys
host = json.load(open(sys.argv[1], encoding="utf-8"))
assert host["ansible_connection"] == "local" and host["local_mode"] in (True, "true"), host
assert str(host["launch_gate_port"]) == "18443", host["launch_gate_port"]
assert host["launch_gate_bind"] == "127.0.0.1", host["launch_gate_bind"]
assert host["dns_site"] == "local" and host["dns_zone"] == "agent-cloud.test", (host["dns_site"], host["dns_zone"])
assert host["launch_gate_semaphore_url"] == "http://local-semaphore:3000", host["launch_gate_semaphore_url"]
assert host["launch_gate_opa_url"] == "http://opa:8181", host["launch_gate_opa_url"]
assert host["launch_gate_image"].startswith("docker.io/library/python@sha256:"), host["launch_gate_image"]
# The leaf must read back as a list (the plugin's literal_eval), declared the way deploy-launch-gate.yml's assert requires.
leaves = host["internal_leaves"]
assert isinstance(leaves, list) and len(leaves) == 1, leaves
leaf = leaves[0]
assert leaf["name"] == "launch-gate-server" and leaf["host"] == "launch-gate-local", leaf
assert leaf["profile"] == "server" and leaf["reload"] == "none", leaf
assert leaf["sans"] == ["launch-gate.local.agent-cloud.test"], leaf["sans"]
expected_dir = host["local_monorepo_dir"] + "/" + host["monorepo_deploy_path"] + "/certs/" + leaf["name"]
assert leaf["dir"] == expected_dir == "/var/lib/agent-cloud-deploy/agent-cloud/platform/services/launch-gate/deployment/certs/launch-gate-server", leaf["dir"]
PY
}

# ── the example inventory mirrors the bootstrap (decision 3) ──────────────────────────────────────────────────────

@test "launch-gate local: the example inventory's launch-gate-local mirrors the bootstrap's launch_gate_svc values" {
  python3 - "$EXAMPLE" "$BOOTSTRAP" <<'PY'
import re, sys, yaml
host = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["all"]["children"]["launch_gate_svc"]["hosts"]["launch-gate-local"]
assert host["ansible_host"] == "127.0.0.1" and host["ansible_connection"] == "local" and host["local_mode"] is True, host
assert host["service_name"] == "launch-gate" and host["monorepo_deploy_path"] == "platform/services/launch-gate/deployment", host
assert str(host["launch_gate_port"]) == "18443" and host["launch_gate_bind"] == "127.0.0.1", host
assert host["dns_site"] == "local" and host["dns_zone"] == "agent-cloud.test", host
assert host["launch_gate_semaphore_url"] == "http://local-semaphore:3000" and host["launch_gate_opa_url"] == "http://opa:8181", host
assert host["launch_gate_image"] == "docker.io/library/python@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30", host["launch_gate_image"]
# The bootstrap's leaf, with its {{ _dev_zone }} resolved to the example's literal zone (the example holds no Jinja).
play = yaml.safe_load(open(sys.argv[2], encoding="utf-8"))[0]
bootstrap_leaves = play["vars"]["_launch_gate_leaves"]
assert host["internal_leaves"] == [{**leaf, "sans": [s.replace("{{ _dev_zone }}", "agent-cloud.test") for s in leaf["sans"]]} for leaf in bootstrap_leaves], host["internal_leaves"]
PY
}

# ── the local overlay: the uid and the TLS-name alias (decision 4) ────────────────────────────────────────────────

@test "launch-gate local: podman-compose renders the overlay's uid and the TLS-name alias on the local-dev network only" {
  command -v podman-compose >/dev/null 2>&1 || skip "podman-compose not available"
  LAUNCH_GATE_IMAGE="$IMAGE" LAUNCH_GATE_TLS_NAME=launch-gate.local.agent-cloud.test \
    podman-compose -f "$DEPLOY_DIR/compose.yml" -f "$DEPLOY_DIR/compose.local.yml" config > "$BATS_TEST_TMPDIR/config.yml"
  python3 - "$BATS_TEST_TMPDIR/config.yml" <<'PY'
import sys, yaml
svc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["services"]["launch-gate"]
assert str(svc["user"]) == "1001:0", svc.get("user")
assert svc["networks"]["local-dev"]["aliases"] == ["launch-gate.local.agent-cloud.test"], svc["networks"]
assert "label=disable" in svc["security_opt"] and "no-new-privileges:true" in svc["security_opt"], svc["security_opt"]
PY
}

# ── deploy-launch-gate.yml Phase 3: the local probe targets the alias on 8443, production is unchanged ──────────

# Renders the Phase 3 probe argv for one inventory with ansible-playbook. $1 = inventory, $2 = host, $3 = output file.
render_phase3_probes() {
  local inventory="$1" host="$2" out="$3"
  python3 - "$DEPLOY" "$host" "$out" <<'PY'
import sys, yaml
deploy, host, out = sys.argv[1], sys.argv[2], sys.argv[3]
play = [p for p in yaml.safe_load(open(deploy, encoding="utf-8")) if "hosts" in p][2]
tasks = {t["name"]: t for t in play["tasks"] if "name" in t}
health = tasks["Health over TLS from this host (GET /healthz -> 200, against the step-ca bundle)"]["ansible.builtin.command"]["argv"]
plain = tasks["Plain HTTP to the TLS listener must not answer 200"]["ansible.builtin.command"]["argv"][-1]
render = [{"hosts": host, "gather_facts": False, "vars": play["vars"], "tasks": [
    {"name": "Hold the probe argv", "ansible.builtin.set_fact": {"health_argv": health, "plain_argv": plain}},
    {"name": "Write the probe argv", "ansible.builtin.copy": {"dest": out, "content":
        "{{ {'health': health_argv, 'plain': plain_argv, 'resolve': _tls_resolve, 'probe_port': _launch_gate_probe_port,"
        " 'plain_host': _launch_gate_plain_host} | to_json }}"}}]}]
open(out + ".yml", "w", encoding="utf-8").write(yaml.safe_dump(render, sort_keys=False))
PY
  ansible-playbook -i "$inventory" "$out.yml" > "$out.log" 2>&1
}

@test "launch-gate local: under local_mode the Phase 3 probes dial the TLS name on 8443 through the alias, with no --resolve" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local work="$BATS_TEST_TMPDIR/phase3" inv="$BATS_TEST_TMPDIR/phase3/local.ini"
  mkdir -p "$work"
  cat > "$inv" <<'INV'
[launch_gate_svc]
launch-gate-local ansible_connection=local

[launch_gate_svc:vars]
local_mode=true
service_name=launch-gate
monorepo_deploy_path=platform/services/launch-gate/deployment
launch_gate_bind=127.0.0.1
launch_gate_port=18443
dns_site=local
dns_zone=agent-cloud.test
local_monorepo_dir=/var/lib/agent-cloud-deploy/agent-cloud
INV
  render_phase3_probes "$inv" launch_gate_svc "$work/local.json"
  python3 - "$work/local.json" <<'PY'
import json, sys
got = json.load(open(sys.argv[1], encoding="utf-8"))
bundle = "/var/lib/agent-cloud-deploy/agent-cloud/platform/services/launch-gate/deployment/certs/step-ca-bundle.crt"
assert got["resolve"] == [], got["resolve"]
assert got["probe_port"] == "8443" and got["plain_host"] == "launch-gate.local.agent-cloud.test", got
assert got["health"] == ["curl", "--silent", "--show-error", "--max-time", "10", "--output", "/dev/null", "--write-out", "%{http_code}",
                         "--cacert", bundle, "https://launch-gate.local.agent-cloud.test:8443/healthz"], got["health"]
assert got["plain"] == "http://launch-gate.local.agent-cloud.test:8443/healthz", got["plain"]
PY
}

@test "launch-gate local: production keeps --resolve to the declared bind and port, and the plain probe on the bind" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local work="$BATS_TEST_TMPDIR/phase3" inv="$BATS_TEST_TMPDIR/phase3/prod.ini"
  mkdir -p "$work"
  cat > "$inv" <<'INV'
[launch_gate_svc]
gate-prod ansible_connection=local

[launch_gate_svc:vars]
service_name=launch-gate
monorepo_deploy_path=platform/services/launch-gate/deployment
launch_gate_bind=192.0.2.10
launch_gate_port=8443
dns_site=dc1
dns_zone=example.internal
local_monorepo_dir=/srv/agent-cloud
INV
  render_phase3_probes "$inv" launch_gate_svc "$work/prod.json"
  python3 - "$work/prod.json" <<'PY'
import json, sys
got = json.load(open(sys.argv[1], encoding="utf-8"))
bundle = "/srv/agent-cloud/platform/services/launch-gate/deployment/certs/step-ca-bundle.crt"
assert got["resolve"] == ["--resolve", "launch-gate.dc1.example.internal:8443:192.0.2.10"], got["resolve"]
assert got["probe_port"] == "8443" and got["plain_host"] == "192.0.2.10", got
assert got["health"] == ["curl", "--silent", "--show-error", "--max-time", "10", "--output", "/dev/null", "--write-out", "%{http_code}",
                         "--cacert", bundle, "--resolve", "launch-gate.dc1.example.internal:8443:192.0.2.10",
                         "https://launch-gate.dc1.example.internal:8443/healthz"], got["health"]
assert got["plain"] == "http://192.0.2.10:8443/healthz", got["plain"]
PY
}

@test "launch-gate local-dev: the deploy re-issues the gate's leaf when it has under 7 days left, in local-dev only" {
  # No Renew Internal Certs run exists locally and a local Clean Deploy keeps certs/, so without this the
  # local leaf (720h by default) would expire and stay expired.
  assert_grep -qF 'argv: [openssl, x509, -checkend, "604800", -noout, -in, "{{ _deploy_dir }}/certs/{{ _launch_gate_server_leaf }}/current/cert.pem"]' "$DEPLOY"
  assert_grep -qF '_mint_reissue: "{{ _launch_gate_local_mode and (_launch_gate_leaf_fresh.rc | default(0)) != 0 }}"' "$DEPLOY"
  refute_grep -qF '_mint_reissue: false' "$DEPLOY"
}

@test "launch-gate local: the leaf-validity check runs only in local-dev, and also under --check" {
  # Without check_mode false a dry run skips it, rc defaults to 0, and an expired local leaf reads as kept.
  python3 - "$DEPLOY" <<'PY'
import sys, yaml
play = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8")) if "hosts" in p][0]
[task] = [t for t in play["tasks"] if t.get("name") == "Local-dev: is the gate's leaf valid for at least 7 more days"]
assert task["when"] == "_launch_gate_local_mode", task.get("when")
assert task["check_mode"] is False, task.get("check_mode")
assert task["changed_when"] is False and task["failed_when"] is False, task
assert task["register"] == "_launch_gate_leaf_fresh", task["register"]
PY
}
