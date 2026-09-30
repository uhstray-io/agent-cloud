"""deploy-step-ca.yml and clean-deploy-step-ca.yml: the production guards and the provisioner plan.

The playbooks' own tasks are lifted and run against a local stub host, so the tests exercise
the real Jinja and asserts rather than copies (openspec production-internal-ca, task 2.5).
Runs through harness_sandbox so an ansible run cannot write outside the test's temp dir. Cases
that differ only in inventory share one ansible-playbook run, one host per case: the guard
has no any_errors_fatal, so each host fails or passes on its own.
"""

import json
import re
import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = playbook_yaml.REPO
FIRST_BOOT_TASKS = REPO / "platform/playbooks/tasks/assert-step-ca-first-boot.yml"
DEPLOY = REPO / "platform/playbooks/deploy-step-ca.yml"
CLEAN = REPO / "platform/playbooks/clean-deploy-step-ca.yml"
ENV_J2 = REPO / "platform/services/step-ca/deployment/templates/env.j2"
ISSUERS = REPO / "platform/playbooks/vars/step-ca-issuance.yml"


def _play(path: Path, name_prefix: str) -> dict:
    return next(p for p in playbook_yaml.load(path) if p.get("name", "").startswith(name_prefix))


def _task(play: dict, name: str) -> dict:
    return next(t for t in play["tasks"] if t.get("name") == name)


def _run(tmp_path: Path, host_vars: dict, tasks: list, play_vars: dict | None = None,
         extra: list | None = None, hosts: dict | None = None) -> subprocess.CompletedProcess:
    """One host `step-ca` with `host_vars`, or every host in `hosts` ({name: vars})."""
    hosts = hosts or {"step-ca": host_vars}
    inv = {"all": {"hosts": {h: {"ansible_connection": "local", **v} for h, v in hosts.items()}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    lifted = [{"hosts": "all", "gather_facts": False, "vars": play_vars or {}, "tasks": tasks}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(lifted))
    return harness_sandbox.run(
        ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml"), *(extra or [])],
        tmp_path, cwd=REPO, env=harness_sandbox.env_for(tmp_path))


def _registered_reads(tmp: Path, reads: dict) -> list:
    """Fake each read the way production gets it: a command's registered stdout. The x509
    template holds Go template braces, which Ansible would render as Jinja if the fake
    arrived as a plain variable; a module's result is not rendered again."""
    return [{"name": f"fake {var}", "ansible.builtin.command": f"cat {tmp}/{{{{ inventory_hostname }}}}.{suffix}",
             "register": var, "changed_when": False} for var, suffix in reads.items()]


def _failed(stdout: str) -> dict:
    """{host: failed count} from the PLAY RECAP."""
    return {m[1]: int(m[2]) for m in re.finditer(r"^(\S+)\s+:\s+ok=\d+.*?failed=(\d+)", stdout, re.M)}


# ── Reset guard (task 2.3) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("confirm,ok", [(None, False), ("some-other-host", False), ("step-ca", True)])
def test_a_ca_reset_runs_only_when_the_launch_names_the_host(tmp_path, confirm, ok):
    guard = _play(CLEAN, "Refuse a CA reset")
    extra = ["-e", f"confirm_ca_reset={confirm}"] if confirm else []
    r = _run(tmp_path, {"local_mode": True}, [_task(guard, "Require the run to NAME the host whose CA it destroys")],
             extra=extra)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    if not ok:
        assert "Refusing: pass -e confirm_ca_reset=step-ca" in r.stdout


def test_the_reset_guard_runs_before_anything_is_destroyed():
    plays = playbook_yaml.load(CLEAN)
    assert plays[0]["name"].startswith("Refuse a CA reset")
    assert plays[0].get("any_errors_fatal") is True
    # A confirmed reset with incomplete inventory refuses before the root is destroyed.
    includes = [t.get("ansible.builtin.include_tasks") for t in plays[0]["tasks"]]
    assert "tasks/assert-step-ca-first-boot.yml" in includes


def test_deploy_runs_the_first_boot_guard_before_anything_is_written():
    names = [t["name"] for t in _play(DEPLOY, "Phase 1")["tasks"]]
    first = _task(_play(DEPLOY, "Phase 1"), "Refuse a production CA without its first-boot settings")
    assert first["ansible.builtin.include_tasks"] == "tasks/assert-step-ca-first-boot.yml"
    assert names.index("Refuse a production CA without its first-boot settings") < names.index(
        "Manage secrets and template env file")


# ── First-boot guard (task 2.2) ────────────────────────────────────────────────

FIRST_BOOT = {"stepca_name": "Example CA", "stepca_dns_names": "ca.example.test,step-ca,localhost",
              "stepca_init_acme": "false", "stepca_bind": "127.0.0.1"}


FIRST_BOOT_CASES = {
    "declared": (FIRST_BOOT, True),
    "acme-on": ({**FIRST_BOOT, "stepca_init_acme": "true"}, False),
    "no-name": ({k: v for k, v in FIRST_BOOT.items() if k != "stepca_name"}, False),
    "no-dns-names": ({k: v for k, v in FIRST_BOOT.items() if k != "stepca_dns_names"}, False),
    "acme-unset": ({k: v for k, v in FIRST_BOOT.items() if k != "stepca_init_acme"}, True),  # off by default
    "bind-lan": ({**FIRST_BOOT, "stepca_bind": "0.0.0.0"}, False),
    "other-provisioner": ({**FIRST_BOOT, "stepca_provisioner": "other"}, False),  # Phase 2.5 raises admin's
    "no-localhost": ({**FIRST_BOOT, "stepca_dns_names": "ca.example.test,step-ca"}, False),  # health uses it
    "dns-names-list": ({**FIRST_BOOT, "stepca_dns_names": ["ca.example.test", "localhost"]}, False),
    "local-mode": ({"local_mode": True}, True),  # local-dev keeps its defaults
}


@pytest.fixture(scope="module")
def first_boot_failed(tmp_path_factory) -> dict:
    tmp = tmp_path_factory.mktemp("first-boot")
    r = _run(tmp, {}, playbook_yaml.load(FIRST_BOOT_TASKS), hosts={c: v for c, (v, _) in FIRST_BOOT_CASES.items()})
    failed = _failed(r.stdout)
    assert failed.keys() == FIRST_BOOT_CASES.keys(), r.stdout + r.stderr
    return failed


@pytest.mark.parametrize("case", FIRST_BOOT_CASES)
def test_a_production_ca_is_refused_without_its_first_boot_settings(first_boot_failed, case):
    assert (first_boot_failed[case] == 0) is FIRST_BOOT_CASES[case][1]


# ── Provisioner plan (task 2.1) ────────────────────────────────────────────────

def _template(eku: str) -> str:
    """The profile template as Phase 2.5 renders it: its '{{' pieces become literal braces."""
    raw = _play(DEPLOY, "Phase 2.5")["vars"]["_x509_template"]
    return raw.replace("{{ '{{' }}", "{{").replace("{{ '}}' }}", "}}").replace("__EKU__", eku)


def _jwk(name, dur=None, eku=None):
    claims = {"maxTLSCertDuration": dur, "defaultTLSCertDuration": dur} if dur else {}
    prov = {"type": "JWK", "name": name, "claims": claims}
    if eku:
        prov["options"] = {"x509": {"template": _template(eku)}}
    return prov


SERVER = _jwk("issuer-server", "720h0m0s", "serverAuth")
CLIENT = _jwk("issuer-client", "720h0m0s", "clientAuth")
CONVERGED = [_jwk("admin", "8760h0m0s"), SERVER, CLIENT]
# {case: (ca.json provisioners, the running CA's list or None for the same)}
PLAN_CASES = {
    "fresh": ([_jwk("admin")], None),
    "converged": (CONVERGED, None),
    "one-differs": ([_jwk("admin", "8760h0m0s"), _jwk("issuer-server", "24h0m0s", "serverAuth"), CLIENT], None),
    "max-differs": ([_jwk("admin", "8760h0m0s"),
                     {**SERVER, "claims": {"maxTLSCertDuration": "24h0m0s", "defaultTLSCertDuration": "720h0m0s"}},
                     CLIENT], None),
    # A run stopped between an add and the reload: ca.json has the issuers, the running CA
    # does not. Nothing is left to add, but the reload must still run.
    "never-reloaded": (CONVERGED, [_jwk("admin", "8760h0m0s")]),
    # The issuers created on 2026-09-29, before profiles: no template, so both key usages.
    "no-templates": ([_jwk("admin", "8760h0m0s"), _jwk("issuer-server", "720h0m0s"),
                      _jwk("issuer-client", "720h0m0s")], None),
    # A template of the wrong profile is replaced, not kept.
    "wrong-profile": ([_jwk("admin", "8760h0m0s"), _jwk("issuer-server", "720h0m0s", "clientAuth"), CLIENT], None),
    # A template written to ca.json but never reloaded.
    "template-unreloaded": (CONVERGED, [_jwk("admin", "8760h0m0s"), _jwk("issuer-server", "720h0m0s"),
                                        _jwk("issuer-client", "720h0m0s")]),
}


@pytest.fixture(scope="module")
def plans(tmp_path_factory) -> dict:
    """Each case's plan, the reads faked per host. The report task runs too, so a plan key
    it cannot read fails."""
    tmp = tmp_path_factory.mktemp("plans")
    play = _play(DEPLOY, "Phase 2.5")
    for case, (prov, running) in PLAN_CASES.items():
        (tmp / f"{case}.ca.json").write_text(json.dumps({"authority": {"provisioners": prov}}))
        (tmp / f"{case}.list.json").write_text(json.dumps(prov if running is None else running))
    hosts = {case: {"_ca_present": {"rc": 0}} for case in PLAN_CASES}
    dump = {"name": "dump", "ansible.builtin.copy": {
        "content": "{{ _prov_plan | to_json }}", "dest": str(tmp / "{{ inventory_hostname }}.json"), "mode": "0600"}}
    loops = {"name": "evaluate the loops", "ansible.builtin.debug": {"msg": [
        "{{ _task_add_loop }}", "{{ _task_set_loop }}"]}, "vars": {
        "_task_add_loop": _task(play, "Add each missing issuing provisioner")["loop"],
        "_task_set_loop": _task(play, "Set the issuing provisioners' leaf lifetime")["loop"]}}
    tasks = [*_registered_reads(tmp, {"_ca_json": "ca.json", "_prov_list": "list.json"}),
             _task(play, "Plan the provisioner changes"), _task(play, "Report the provisioner plan"), loops, dump]
    r = _run(tmp, {}, tasks, play_vars={**playbook_yaml.load(ISSUERS), **play["vars"]}, hosts=hosts)
    assert r.returncode == 0, r.stdout + r.stderr
    return {case: json.loads((tmp / f"{case}.json").read_text()) for case in PLAN_CASES}


BOTH = ["issuer-server", "issuer-client"]
NOTHING = {"raise_admin": False, "add": [], "set_lifetime": [], "set_template": [], "set_policy": False,
           "reload_pending": False}


def test_a_fresh_ca_gets_both_issuers_every_lifetime_and_both_profiles(plans):
    assert plans["fresh"] == {**NOTHING, "raise_admin": True, "add": BOTH, "set_lifetime": BOTH, "set_template": BOTH}


def test_a_converged_ca_plans_nothing(plans):
    assert plans["converged"] == NOTHING


def test_only_the_provisioner_whose_lifetime_differs_is_updated(plans):
    assert plans["one-differs"] == {**NOTHING, "set_lifetime": ["issuer-server"]}


def test_issuers_without_a_profile_template_get_one(plans):
    assert plans["no-templates"] == {**NOTHING, "set_template": BOTH}


def test_a_template_of_the_wrong_profile_is_replaced(plans):
    assert plans["wrong-profile"] == {**NOTHING, "set_template": ["issuer-server"]}


def test_a_template_written_but_never_reloaded_is_reloaded(plans):
    assert plans["template-unreloaded"] == {**NOTHING, "reload_pending": True}


def test_a_differing_maximum_alone_triggers_the_update(plans):
    assert plans["max-differs"]["set_lifetime"] == ["issuer-server"]


def test_a_change_written_but_never_reloaded_is_reloaded(plans):
    assert plans["never-reloaded"] == {**NOTHING, "reload_pending": True}


# ── Running profiles (findings 2026-09-30) ─────────────────────────────────────

RUNNING_CASES = {
    "profiles": ([SERVER, CLIENT], True),
    "no-template": ([_jwk("issuer-server", "720h0m0s"), CLIENT], False),
    "swapped": ([_jwk("issuer-server", "720h0m0s", "clientAuth"), _jwk("issuer-client", "720h0m0s", "serverAuth")],
                False),
}


def test_phase_3_refuses_a_running_issuer_without_its_profile(tmp_path):
    play = _play(DEPLOY, "Phase 3")
    for case, (provs, _) in RUNNING_CASES.items():
        (tmp_path / f"{case}.list.json").write_text(json.dumps(provs))
    hosts = {case: {"_ca_up": {"rc": 0}} for case in RUNNING_CASES}
    # `_ca_state` is a loop register in production: results[0] is the provisioner list.
    fake = {"name": "fake the state reads", "ansible.builtin.command": "cat {{ item }}",
            "loop": [f"{tmp_path}/{{{{ inventory_hostname }}}}.list.json"], "register": "_ca_state",
            "changed_when": False}
    task = _task(play, "Refuse an issuing provisioner without its profile's key usage")
    r = _run(tmp_path, {}, [fake, task], play_vars={**playbook_yaml.load(ISSUERS), **play["vars"]}, hosts=hosts)
    failed = _failed(r.stdout)
    assert {c: failed.get(c) == 0 for c in RUNNING_CASES} == {c: ok for c, (_, ok) in RUNNING_CASES.items()}, r.stdout


def test_the_list_is_read_before_any_provisioner_is_added_and_the_add_is_hidden():
    names = [t["name"] for t in _play(DEPLOY, "Phase 2.5")["tasks"]]
    assert names.index("Read the provisioner list") < names.index("Add each missing issuing provisioner")
    add = _task(_play(DEPLOY, "Phase 2.5"), "Add each missing issuing provisioner")
    assert add.get("no_log") is True
    assert "stdin" in add["ansible.builtin.command"]  # the password never reaches argv or .env


# ── Production render (task 2.5) ───────────────────────────────────────────────

@pytest.mark.parametrize("host_vars,acme", [
    ({"local_mode": True}, "true"),                                   # an existing local inventory
    ({"local_mode": True, "stepca_init_acme": "false"}, "false"),    # declared wins
    ({"stepca_name": "x", "stepca_dns_names": "localhost"}, "false"),  # production, undeclared
], ids=["local-default", "local-declared", "production-default"])
def test_acme_is_on_only_in_local_mode_unless_declared(tmp_path, host_vars, acme):
    out = tmp_path / "env"
    task = {"name": "render", "ansible.builtin.template": {"src": str(ENV_J2), "dest": str(out), "mode": "0600"}}
    r = _run(tmp_path, {**host_vars, "secrets": {"init_password": "x"}}, [task])
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"STEPCA_INIT_ACME={acme}" in out.read_text().splitlines()


def test_production_values_render_the_loopback_bind_and_acme_off(tmp_path):
    out = tmp_path / "env"
    task = {"name": "render", "ansible.builtin.template": {"src": str(ENV_J2), "dest": str(out), "mode": "0600"}}
    r = _run(tmp_path, {**FIRST_BOOT, "secrets": {"init_password": "x"}}, [task])
    assert r.returncode == 0, r.stdout + r.stderr
    lines = out.read_text().splitlines()
    assert "STEPCA_BIND=127.0.0.1" in lines
    assert "STEPCA_INIT_ACME=false" in lines
    assert "STEPCA_NAME=Example CA" in lines
    # The issuer passwords are not in the env file: they reach the container on stdin.
    assert not any("ISSUER" in line for line in lines)


def test_the_issuer_password_comes_from_the_fact_manage_secrets_sets():
    # Production task 1971 failed here (docs/MISTAKES.md 10.17, occurrence 2): the add read
    # `secrets[...]`, which manage-secrets only defines inside its template task, while the
    # lifted tests stubbed a `secrets` fact and passed. `_resolved` holds every declared
    # secret (manage-secrets' documented output), and each issuer's secret is declared.
    add = _task(_play(DEPLOY, "Phase 2.5"), "Add each missing issuing provisioner")
    assert re.match(r"\{\{\s*_resolved\[", add["ansible.builtin.command"]["stdin"])
    declared = {d["name"] for d in _play(DEPLOY, "Phase 1")["vars"]["_secret_definitions"]}
    assert {i["secret"] for i in playbook_yaml.load(ISSUERS)["_issuers"]} <= declared


# ── Name policy (findings 2026-09-30; exact declared names, decided 2026-09-30) ──

SITE = {"dns_site": "dc1", "dns_zone": "example.internal"}
LEAVES = [
    {"name": "caddy", "sans": ["vm01.caddy.dc1.example.internal", "caddy.dc1.example.internal"]},
    {"name": "gateway", "sans": ["gateway.dc1.example.internal", "caddy.dc1.example.internal"]},
]
POLICY = {"x509": {"allow": {"dns": sorted({s for leaf in LEAVES for s in leaf["sans"]})}}}

NAME_CASES = {
    "declared": ({**SITE, "internal_leaves": LEAVES}, True),
    "none-declared": ({}, True),
    "wildcard": ({**SITE, "internal_leaves": [{"name": "w", "sans": ["*.dc1.example.internal"]}]}, False),
    "other-site": ({**SITE, "internal_leaves": [{"name": "o", "sans": ["gateway.dc2.example.internal"]}]}, False),
    "suffix-only": ({**SITE, "internal_leaves": [{"name": "s", "sans": ["evildc1.example.internal"]}]}, False),
    "upper-case": ({**SITE, "internal_leaves": [{"name": "u", "sans": ["Gateway.dc1.example.internal"]}]}, False),
    "no-site": ({"internal_leaves": LEAVES}, False),
    "local-mode": ({"local_mode": True, "internal_leaves": [{"name": "w", "sans": ["*.agent-cloud.test"]}]}, True),
}


def _issuance_vars(play: dict) -> dict:
    return {**playbook_yaml.load(ISSUERS), **play["vars"]}


def test_a_name_the_policy_cannot_hold_is_refused_before_anything_is_read(tmp_path):
    play = _play(DEPLOY, "Phase 2.5")
    assert play["tasks"][0]["name"] == "Refuse a declared name the CA's name policy cannot hold"
    hosts = {c: v for c, (v, _) in NAME_CASES.items()}
    r = _run(tmp_path, {}, [play["tasks"][0]], play_vars=_issuance_vars(play), hosts=hosts)
    failed = _failed(r.stdout)
    assert {c: failed.get(c) == 0 for c in NAME_CASES} == {c: ok for c, (_, ok) in NAME_CASES.items()}, r.stdout


# {case: (host vars, the policy ca.json holds or None, set_policy expected)}
POLICY_CASES = {
    "add": ({**SITE, "internal_leaves": LEAVES}, None, True),
    "same": ({**SITE, "internal_leaves": LEAVES}, POLICY, False),
    "narrowed": ({**SITE, "internal_leaves": LEAVES[:1]}, POLICY, True),
    "removed": ({}, POLICY, True),
    "never": ({}, None, False),
}


def test_the_policy_is_planned_from_the_declared_leaves(tmp_path):
    play = _play(DEPLOY, "Phase 2.5")
    for case, (_, current, _) in POLICY_CASES.items():
        auth = {"provisioners": CONVERGED, **({"policy": current} if current else {})}
        (tmp_path / f"{case}.ca.json").write_text(json.dumps({"authority": auth}))
        (tmp_path / f"{case}.list.json").write_text(json.dumps(CONVERGED))
    dump = {
        "name": "dump",
        "ansible.builtin.copy": {
            "content": "{{ _prov_plan.set_policy | to_json }}",
            "dest": str(tmp_path / "{{ inventory_hostname }}.out"),
            "mode": "0600",
        },
    }
    tasks = [*_registered_reads(tmp_path, {"_ca_json": "ca.json", "_prov_list": "list.json"}),
             _task(play, "Plan the provisioner changes"), dump]
    hosts = {case: {"_ca_present": {"rc": 0}, **hv} for case, (hv, _, _) in POLICY_CASES.items()}
    r = _run(tmp_path, {}, tasks, play_vars=_issuance_vars(play), hosts=hosts)
    assert r.returncode == 0, r.stdout + r.stderr
    got = {case: json.loads((tmp_path / f"{case}.out").read_text()) for case in POLICY_CASES}
    assert got == {case: want for case, (_, _, want) in POLICY_CASES.items()}


def _stub_engine(tmp_path: Path, log: str = "") -> Path:
    """A container engine that keeps what `exec -i` is given, records signals, and prints `log`."""
    stub = tmp_path / "engine"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  exec) cat > "{tmp_path}/written.json" ;;\n'
        # Like podman, `kill` prints the container's name on stdout.
        f'  kill) echo "$@" >> "{tmp_path}/signals"; echo step-ca ;;\n'
        # Like podman, `logs --since` refuses anything but a timestamp (a stray line in the
        # write's output once reached it, found against a real CA).
        '  logs) echo "$3" | grep -qE "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$" || exit 125;\n'
        f"        printf '%s\\n' {json.dumps(log)} ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return stub


@pytest.mark.parametrize("hv,want", [({**SITE, "internal_leaves": LEAVES}, POLICY), ({}, None)],
                         ids=["written", "removed"])
def test_the_policy_write_keeps_the_rest_of_ca_json_and_reloads(tmp_path, hv, want):
    play = _play(DEPLOY, "Phase 2.5")
    current = {"authority": {"provisioners": CONVERGED, "policy": {"x509": {"allow": {"dns": ["old"]}}},
                             "claims": {"x": 1}}, "root": "/r"}
    # The re-read's first line is the file's digest, the write's precondition.
    (tmp_path / "step-ca.ca.json").write_text("0" * 64 + "\n" + json.dumps(current))
    stub = _stub_engine(tmp_path, "Serving HTTPS on :9000 ...")
    # The reload check runs on the write's real output, as in the play.
    check = {**_task(play, "Refuse a reload that did not take the new configuration"), "retries": 1, "delay": 0}
    tasks = [*_registered_reads(tmp_path, {"_ca_json_now": "ca.json"}),
             {"name": "plan", "ansible.builtin.set_fact": {"_prov_plan": {"set_policy": True}}},
             _task(play, "Write the name policy into ca.json and reload the CA"), check]
    host = {"_ca_present": {"rc": 0}, "container_engine": str(stub), **hv}
    r = _run(tmp_path, host, tasks, play_vars=_issuance_vars(play))
    assert r.returncode == 0, r.stdout + r.stderr
    written = json.loads((tmp_path / "written.json").read_text())
    assert written["authority"].get("policy") == want
    assert written["authority"]["provisioners"] == CONVERGED and written["authority"]["claims"] == {"x": 1}
    assert written["root"] == "/r"
    assert "--signal HUP step-ca" in (tmp_path / "signals").read_text()


@pytest.mark.parametrize("log,ok", [
    ("reloading ...\nServing HTTPS on :9000 ...", True),
    ("error reloading server: error reloading ca: cannot parse permitted domain constraint", False),
], ids=["applied", "reload-error"])
def test_a_reload_that_did_not_apply_fails_the_run(tmp_path, log, ok):
    play = _play(DEPLOY, "Phase 2.5")
    stub = _stub_engine(tmp_path, log)
    task = {**_task(play, "Refuse a reload that did not take the new configuration"), "retries": 1, "delay": 0}
    wrote = {"name": "the write ran", "ansible.builtin.command": "echo 2026-09-30T00:00:00Z",
             "register": "_prov_policy", "changed_when": True}
    r = _run(tmp_path, {"container_engine": str(stub)}, [wrote, task], play_vars=play["vars"])
    assert (r.returncode == 0) is ok, r.stdout + r.stderr


def test_the_policy_write_refuses_a_ca_json_changed_since_it_was_read():
    # Review of #363: a provisioner update between the read and the write would be lost.
    task = _task(_play(DEPLOY, "Phase 2.5"), "Write the name policy into ca.json and reload the CA")
    script = task["ansible.builtin.shell"]
    assert '[ "${s%% *}" = "$0" ] || {' in script and "_ca_json_now.stdout_lines[0] | quote" in script
    assert script.index("sha256sum") < script.index('mv "$new" "$f"')


# ── A dry run with changes pending (MISTAKES 10.18, occurrence 2) ──────────────

# One assertion per run, and only ONE thing wrong with the CA in each, so each case proves
# its own gate (review of #368): the gate may skip only under --check, and only for the
# change Phase 2.5 plans.
# {case: (Phase 3 task, check mode, plan, passes)}
GATE_CASES = {
    "count-check-planned": ("Refuse a production CA with ACME on or its API off loopback", True,
                            {"add": ["issuer-client"]}, True),
    "count-check-unplanned": ("Refuse a production CA with ACME on or its API off loopback", True, {}, False),
    "count-real-planned": ("Refuse a production CA with ACME on or its API off loopback", False,
                           {"add": ["issuer-client"]}, False),
    "template-check-planned": ("Refuse an issuing provisioner without its profile's key usage", True,
                               {"set_template": ["issuer-client"]}, True),
    "template-check-unplanned": ("Refuse an issuing provisioner without its profile's key usage", True,
                                 {"set_template": []}, False),
    "template-check-other-planned": ("Refuse an issuing provisioner without its profile's key usage", True,
                                     {"set_template": ["issuer-server"]}, False),
    "template-real-planned": ("Refuse an issuing provisioner without its profile's key usage", False,
                              {"set_template": ["issuer-client"]}, False),
    "policy-check-planned": ("Refuse a CA whose name policy is not the declared names", True,
                             {"set_policy": True}, True),
    "policy-check-unplanned": ("Refuse a CA whose name policy is not the declared names", True,
                               {"set_policy": False}, False),
    "policy-real-planned": ("Refuse a CA whose name policy is not the declared names", False,
                            {"set_policy": True}, False),
}


@pytest.mark.parametrize("case", GATE_CASES)
def test_a_dry_run_skips_an_assertion_only_where_phase_2_5_plans_that_change(tmp_path, case):
    # Task 2132: the first dry run after #363 failed Phase 3 because the templates it plans
    # are not on the running CA until the real run sets them.
    name, check, plan, ok = GATE_CASES[case]
    play = _play(DEPLOY, "Phase 3")
    # Each case breaks only what its own assertion reads: the count case lacks issuer-client,
    # the template case lacks issuer-client's template, the policy case lacks the policy.
    if name.startswith("Refuse a production CA"):
        running = [SERVER]
    elif "profile" in name:
        running = [SERVER, _jwk("issuer-client", "720h0m0s")]
    else:
        running = [SERVER, CLIENT]
    (tmp_path / "list.json").write_text(json.dumps(running))
    (tmp_path / "port.txt").write_text("9000/tcp -> 127.0.0.1:9000\n")
    (tmp_path / "ca.json").write_text(json.dumps({"authority": {"provisioners": running}}))
    fakes = [
        {"name": "fake the plan", "ansible.builtin.set_fact": {"_prov_plan": plan}},
        {"name": "fake the state reads", "ansible.builtin.command": "cat {{ item }}", "check_mode": False,
         "loop": [str(tmp_path / "list.json"), str(tmp_path / "port.txt")], "register": "_ca_state",
         "changed_when": False},
        {"name": "fake the ca.json read", "ansible.builtin.command": f"cat {tmp_path / 'ca.json'}",
         "check_mode": False, "register": "_ca_json_final", "changed_when": False},
    ]
    host = {"_ca_up": {"rc": 0}, "dns_site": "dc1", "dns_zone": "example.internal",
            "internal_leaves": [{"name": "g", "sans": ["g.dc1.example.internal"]}] if "policy" in name else []}
    r = _run(tmp_path, host, [*fakes, _task(play, name)], play_vars={**playbook_yaml.load(ISSUERS), **play["vars"]},
             extra=["--check"] if check else [])
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
