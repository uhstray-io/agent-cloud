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

def _jwk(name, dur=None):
    claims = {"maxTLSCertDuration": dur, "defaultTLSCertDuration": dur} if dur else {}
    return {"type": "JWK", "name": name, "claims": claims}


CONVERGED = [_jwk("admin", "8760h0m0s"), _jwk("issuer-server", "720h0m0s"), _jwk("issuer-client", "720h0m0s")]
# {case: (ca.json provisioners, the running CA's list or None for the same)}
PLAN_CASES = {
    "fresh": ([_jwk("admin")], None),
    "converged": (CONVERGED, None),
    "one-differs": ([_jwk("admin", "8760h0m0s"), _jwk("issuer-server", "24h0m0s"),
                     _jwk("issuer-client", "720h0m0s")], None),
    "max-differs": ([_jwk("admin", "8760h0m0s"),
                     {"type": "JWK", "name": "issuer-server",
                      "claims": {"maxTLSCertDuration": "24h0m0s", "defaultTLSCertDuration": "720h0m0s"}},
                     _jwk("issuer-client", "720h0m0s")], None),
    # A run stopped between an add and the reload: ca.json has the issuers, the running CA
    # does not. Nothing is left to add, but the reload must still run.
    "never-reloaded": (CONVERGED, [_jwk("admin", "8760h0m0s")]),
}


@pytest.fixture(scope="module")
def plans(tmp_path_factory) -> dict:
    """Each case's plan, the reads faked per host. The report task runs too, so a plan key
    it cannot read fails."""
    tmp = tmp_path_factory.mktemp("plans")
    play = _play(DEPLOY, "Phase 2.5")
    hosts = {case: {"_ca_present": {"rc": 0},
                    "_ca_json": {"stdout": json.dumps({"authority": {"provisioners": prov}})},
                    "_prov_list": {"stdout": json.dumps(prov if running is None else running)}}
             for case, (prov, running) in PLAN_CASES.items()}
    dump = {"name": "dump", "ansible.builtin.copy": {
        "content": "{{ _prov_plan | to_json }}", "dest": str(tmp / "{{ inventory_hostname }}.json"), "mode": "0600"}}
    loops = {"name": "evaluate the loops", "ansible.builtin.debug": {"msg": [
        "{{ _task_add_loop }}", "{{ _task_set_loop }}"]}, "vars": {
        "_task_add_loop": _task(play, "Add each missing issuing provisioner")["loop"],
        "_task_set_loop": _task(play, "Set the issuing provisioners' leaf lifetime")["loop"]}}
    tasks = [_task(play, "Plan the provisioner changes"), _task(play, "Report the provisioner plan"), loops, dump]
    r = _run(tmp, {}, tasks, play_vars=play["vars"], hosts=hosts)
    assert r.returncode == 0, r.stdout + r.stderr
    return {case: json.loads((tmp / f"{case}.json").read_text()) for case in PLAN_CASES}


def test_a_fresh_ca_gets_both_issuers_and_every_lifetime(plans):
    assert plans["fresh"] == {"raise_admin": True, "add": ["issuer-server", "issuer-client"],
                              "set_lifetime": ["issuer-server", "issuer-client"], "reload_pending": False}


def test_a_converged_ca_plans_nothing(plans):
    assert plans["converged"] == {"raise_admin": False, "add": [], "set_lifetime": [], "reload_pending": False}


def test_only_the_provisioner_whose_lifetime_differs_is_updated(plans):
    assert plans["one-differs"] == {"raise_admin": False, "add": [], "set_lifetime": ["issuer-server"],
                                    "reload_pending": False}


def test_a_differing_maximum_alone_triggers_the_update(plans):
    assert plans["max-differs"]["set_lifetime"] == ["issuer-server"]


def test_a_change_written_but_never_reloaded_is_reloaded(plans):
    assert plans["never-reloaded"] == {"raise_admin": False, "add": [], "set_lifetime": [], "reload_pending": True}


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
    assert {i["secret"] for i in _play(DEPLOY, "Phase 2.5")["vars"]["_issuers"]} <= declared
