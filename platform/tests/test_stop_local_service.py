"""Stop Local Service stops one idle local-dev service's containers and nothing else.

platform/playbooks/stop-local-service.yml is the reversible way to free a local service's
memory: containers stopped, containers/images/volumes kept, restored by the service's own
Deploy. These tests run the REAL playbook with ansible-playbook against a stateful fake
container engine on PATH (it holds a set of containers, answers `ps` with the compose-label
filter the real selector uses, and logs every call), so they prove what the engine was asked,
not what the YAML says.
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from test_executor_internal_overrides import PLAYBOOKS, REPO, _run, needs_ansible

PLAYBOOK = PLAYBOOKS / "stop-local-service.yml"
DEPLOY_PATH = "platform/services/erpnext/deployment"
EXTRA = "netbox-orb-agent"

# A stand-in for podman/docker. State: {"containers": [{"name", "dir", "running"}]}; `dir` is the
# compose working_dir label ("" for a container the compose project did not create). Every
# call is appended to the log as one JSON argv line.
FAKE_ENGINE = f"""#!{sys.executable}
import json, os, sys
state_path, log_path = os.environ["FAKE_ENGINE_STATE"], os.environ["FAKE_ENGINE_LOG"]
argv = sys.argv[1:]
with open(log_path, "a") as log:
    log.write(json.dumps(argv) + "\\n")
state = json.load(open(state_path))
cs = state["containers"]
if argv[:1] == ["ps"]:
    rest = argv[1:]
    want_all = "-a" in rest
    label = next((rest[i + 1].split("=", 2)[2] for i, a in enumerate(rest) if a == "--filter"), None)
    for c in cs:
        if (want_all or c["running"]) and (label is None or c["dir"] == label):
            print(c["name"])
elif argv[:1] == ["stop"]:
    for name in argv[1:]:
        hit = [c for c in cs if c["name"] == name]
        if not hit:
            print("no such container", name, file=sys.stderr)
            sys.exit(125)
        hit[0]["running"] = False
    json.dump(state, open(state_path, "w"))
"""


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.deploy_dir = f"{tmp_path}/monorepo/{DEPLOY_PATH}"
        self.state = tmp_path / "engine-state.json"
        self.log = tmp_path / "engine-log"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name in ("podman", "docker"):
            (self.bin / name).write_text(FAKE_ENGINE)
            (self.bin / name).chmod(0o755)
        monkeypatch.setenv("FAKE_ENGINE_STATE", str(self.state))
        monkeypatch.setenv("FAKE_ENGINE_LOG", str(self.log))
        self.set_containers([])

    def set_containers(self, containers):
        self.state.write_text(json.dumps({"containers": containers}))

    def c(self, name, running=True, other_project=False, unlabelled=False):
        return {"name": name, "running": running,
                "dir": "" if unlabelled else ("/elsewhere" if other_project else self.deploy_dir)}

    def running(self):
        return sorted(c["name"] for c in json.loads(self.state.read_text())["containers"] if c["running"])

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def inventory(self, hosts=None, extras=None, group="erpnext_svc", **hostvars):
        hosts = hosts or ["erpnext-local"]
        vars_ = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                 "service_name": "erpnext", "monorepo_deploy_path": DEPLOY_PATH,
                 "local_monorepo_dir": f"{self.tmp}/monorepo", "container_engine": "podman", **hostvars}
        if extras is not None:
            vars_["stop_extra_containers"] = extras
        path = self.tmp / "inv.yml"
        path.write_text(yaml.safe_dump({"all": {"children": {group: {"hosts": {h: {**vars_} for h in hosts}}}}}))
        return str(path)

    def run(self, inventory, *args, target="erpnext_svc"):
        return _run(PLAYBOOK, self.tmp, "-e", f"target_service={target}", *args,
                    inventory=inventory, path_prepend=self.bin)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def _recap(proc):
    m = re.search(r"ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", proc.stdout)
    assert m, proc.stdout + proc.stderr
    return {"ok": int(m[1]), "changed": int(m[2]), "failed": int(m[4])}


# Subcommands that would delete state or alter a container's definition. None may ever be sent.
FORBIDDEN = {"rm", "down", "kill", "update", "volume", "prune", "system", "compose", "restart", "start"}


def _subcommands(calls):
    return [c[0] for c in calls if c]


@needs_ansible
def test_it_stops_only_the_running_labelled_containers_and_the_declared_extras(rig):
    rig.set_containers([
        rig.c("erpnext_backend_1"), rig.c("erpnext_db_1"),
        rig.c("erpnext_worker_1", running=False),           # already stopped: left alone
        rig.c("other_app_1", other_project=True),            # another project: untouched
        rig.c(EXTRA, unlabelled=True),                       # the declared unlabelled extra
        rig.c("bystander", unlabelled=True),                 # unlabelled and NOT declared: untouched
    ])
    proc = rig.run(rig.inventory(extras=[EXTRA, "absent-extra"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    stops = [c for c in rig.calls() if c[0] == "stop"]
    assert len(stops) == 1, rig.calls()
    assert sorted(stops[0][1:]) == sorted(["erpnext_backend_1", "erpnext_db_1", EXTRA]), stops
    assert rig.running() == ["bystander", "other_app_1"]
    assert not set(_subcommands(rig.calls())) & FORBIDDEN, rig.calls()
    assert set(_subcommands(rig.calls())) <= {"ps", "stop"}, rig.calls()
    assert _recap(proc)["changed"] == 1
    assert "Bring it back with its Deploy <Service> (Local) template" in proc.stdout
    assert "erpnext_worker_1" in proc.stdout  # reported as already stopped


@needs_ansible
def test_a_second_run_changes_nothing(rig):
    rig.set_containers([rig.c("erpnext_backend_1"), rig.c(EXTRA, unlabelled=True)])
    inv = rig.inventory(extras=[EXTRA])
    assert _recap(rig.run(inv))["changed"] == 1
    rig.log.unlink()
    proc = rig.run(inv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _recap(proc)["changed"] == 0
    assert "stop" not in _subcommands(rig.calls()), rig.calls()
    assert rig.running() == []


@needs_ansible
def test_check_mode_reports_and_stops_nothing(rig):
    rig.set_containers([rig.c("erpnext_backend_1"), rig.c(EXTRA, unlabelled=True)])
    proc = rig.run(rig.inventory(extras=[EXTRA]), "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "stop" not in _subcommands(rig.calls()), rig.calls()
    assert rig.running() == sorted(["erpnext_backend_1", EXTRA])
    assert "Would stop" in proc.stdout and "erpnext_backend_1" in proc.stdout
    assert _recap(proc)["changed"] == 0


@needs_ansible
def test_a_host_not_named_local_is_refused_before_any_engine_call(rig):
    rig.set_containers([rig.c("erpnext_backend_1")])
    proc = rig.run(rig.inventory(hosts=["erpnext-prod"]))
    assert proc.returncode != 0, proc.stdout
    assert "local-dev hosts only" in proc.stdout, proc.stdout
    assert rig.calls() == []
    assert rig.running() == ["erpnext_backend_1"]


@needs_ansible
def test_one_non_local_host_in_the_group_refuses_the_whole_play(rig):
    rig.set_containers([rig.c("erpnext_backend_1")])
    proc = rig.run(rig.inventory(hosts=["erpnext-local", "erpnext-prod"]))
    assert proc.returncode != 0, proc.stdout
    assert "erpnext-prod" in proc.stdout and "local-dev hosts only" in proc.stdout
    assert rig.calls() == []
    assert rig.running() == ["erpnext_backend_1"]


@needs_ansible
@pytest.mark.parametrize("target", ["nope_svc", "", "all", "erpnext"])
def test_an_empty_or_misshapen_group_is_refused_before_any_engine_call(rig, target):
    rig.set_containers([rig.c("erpnext_backend_1")])
    proc = rig.run(rig.inventory(), target=target)
    assert proc.returncode != 0, proc.stdout
    assert "Nothing was stopped" in proc.stdout, proc.stdout
    assert rig.calls() == []
    assert rig.running() == ["erpnext_backend_1"]


@needs_ansible
@pytest.mark.parametrize("extras", [["--all"], ["a b"], ["ok", "-x"], "netbox-orb-agent", [""], [["nested"]]])
def test_a_malformed_extra_container_list_is_refused_before_any_engine_call(rig, extras):
    rig.set_containers([rig.c("erpnext_backend_1"), rig.c(EXTRA, unlabelled=True)])
    proc = rig.run(rig.inventory(extras=extras))
    assert proc.returncode != 0, proc.stdout
    assert "stop_extra_containers" in proc.stdout, proc.stdout
    assert rig.calls() == []
    assert rig.running() == sorted(["erpnext_backend_1", EXTRA])


@needs_ansible
def test_a_service_with_nothing_running_is_a_clean_no_op(rig):
    rig.set_containers([rig.c("erpnext_backend_1", running=False)])
    proc = rig.run(rig.inventory())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _recap(proc)["changed"] == 0
    assert "nothing was running" in proc.stdout
    assert "stop" not in _subcommands(rig.calls())


# ── structure: registered, reachable, and local-only ───────────────────────────────────────
def test_the_local_catalog_registers_it_with_a_group_survey_field():
    local = yaml.safe_load((REPO / "platform/semaphore/templates-local.yml").read_text())["templates"]
    shared = yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]
    mine = [t for t in local if t["playbook"] == "platform/playbooks/stop-local-service.yml"]
    assert [t["name"] for t in mine] == ["Stop Local Service"]
    assert [v["name"] for v in mine[0]["survey_vars"]] == ["target_service"]
    assert mine[0]["survey_vars"][0]["required"] is True
    # Local-only by design: stopping a production service is an outage, so no shared template.
    assert not [t for t in shared if "stop-local-service" in t["playbook"]]


def test_the_netbox_local_host_declares_its_unlabelled_orb_agent():
    inv = yaml.safe_load((REPO / "platform/inventory/local-dev.yml.example").read_text())
    host = inv["all"]["children"]["netbox_svc"]["hosts"]["netbox-local"]
    assert host["stop_extra_containers"] == ["netbox-orb-agent"]
    # The name must be the one tasks/deploy-orb-agent.yml starts.
    assert "--name netbox-orb-agent" in (PLAYBOOKS / "tasks/deploy-orb-agent.yml").read_text()


def test_the_make_target_refuses_an_empty_service():
    if shutil.which("make") is None:
        pytest.skip("make not installed")
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "SERVICE"}
    proc = subprocess.run(["make", "local-service-down"], cwd=REPO, env=env, text=True,
                          capture_output=True, check=False, timeout=60)
    assert proc.returncode != 0
    assert "usage: make local-service-down SERVICE=" in proc.stderr, proc.stderr
