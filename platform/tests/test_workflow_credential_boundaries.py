"""The service deployment workflow's NetBox calls keep their tokens out of task output.

PR 195 Codex review: the collector copied whole registered uri results (which can carry the
request headers) into a visible fact, and validate-address's token-bearing lookup could print
its headers on failure. Also covers two local-dev defects from the same review.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"


def _tasks(path: Path) -> list[dict]:
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    doc = yaml.safe_load(path.read_text())
    for play in doc if isinstance(doc, list) else []:
        walk(play.get("tasks") if isinstance(play, dict) and "hosts" in play else [play])
    return out


def _named(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_the_collector_keeps_only_service_and_vm_id(tmp_path):
    sort = _named(PLAYBOOKS / "collect-service-conformance.yml", "NetBox: sort the lookups")
    results = [
        {"item": "svc-a", "status": 200, "json": {"count": 1, "results": [{"id": 7}]},
         "invocation": {"module_args": {"headers": {"Authorization": "Bearer nbt_secret.value"}}}},
        {"item": "svc-b", "status": 200, "json": {"count": 0, "results": []}},
    ]
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_vms": {"results": results}},
        "tasks": [sort, {"ansible.builtin.debug": {"msg": "FOUND {{ _vm_found | to_json }}"}}],
    }]
    path = tmp_path / "sort.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                         text=True, capture_output=True, check=True).stdout
    line = next(ln for ln in out.splitlines() if "FOUND " in ln)
    found = json.loads(json.loads(line.split('"msg": ', 1)[1]).split("FOUND ", 1)[1])
    assert found == [{"item": "svc-a", "id": 7}]
    assert "nbt_secret" not in out


def test_validate_address_hides_its_token_bearing_requests():
    path = PLAYBOOKS / "validate-address-free.yml"
    for name in ("NetBox: find the service's VM record and the Proxmox clusters",
                 "NetBox: create the VM record (planned, in the Proxmox cluster)"):
        task = _named(path, name)
        assert task.get("no_log") is True and task.get("failed_when") is False, name
    assert _named(path, "Require both NetBox reads to answer").get("no_log") is not True


def test_token_minting_uses_the_configured_engine():
    play = yaml.safe_load((PLAYBOOKS / "provision-netbox-automation-token.yml").read_text())[0]
    manage = play["vars"]["_manage"]
    assert "container_engine" in manage and "netbox_app_container" in manage, manage


def test_local_discovery_requires_the_subnet_its_scans_render():
    names = [t.get("name") for t in yaml.safe_load((PLAYBOOKS / "tasks/assert-local-discovery-scope.yml").read_text())]
    assert "Local discovery: extra targets need the subnet the scans cover" in names


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
@pytest.mark.parametrize("status,own", [("running", True), ("stopped", False)])
def test_an_arp_hit_is_the_services_own_only_while_its_vm_runs(tmp_path, status, own):
    # PR 195 Codex review: a STOPPED VM with the declared id and name still made the ARP hit
    # "its own", so an address another device held passed as free.
    judge = _named(PLAYBOOKS / "validate-address-free.yml", "Judge the address")
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_ip": "192.0.2.10", "_vmid": 101, "_name": "svc",
                 "_arp": {"json": {"data": [{"ip": "192.0.2.10", "mac": "aa"}]}},
                 "_pve_vms": {"json": {"data": [{"vmid": 101, "name": "svc", "status": status}]}}},
        "tasks": [judge, {"ansible.builtin.debug": {"msg": "OWN {{ _own_vm | bool }}"}}],
    }]
    path = tmp_path / "judge.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                         text=True, capture_output=True, check=True).stdout
    assert f"OWN {own}" in out, out[-800:]


def _run_tasks(tmp_path, tasks, variables, probe):
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
                "tasks": [*tasks, {"ansible.builtin.debug": {"msg": "PROBE {{ " + probe + " | to_json }}"}}]}]
    path = tmp_path / "h.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                         text=True, capture_output=True, check=True).stdout
    line = next(ln for ln in out.splitlines() if "PROBE " in ln)
    return json.loads(json.loads(line.split('"msg": ', 1)[1]).split("PROBE ", 1)[1]), out


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_the_aggregate_input_carries_no_request_headers(tmp_path):
    # PR 195 Codex review: the aggregate command's stdin held whole registered uri results,
    # the Semaphore token among their request headers, and a failed command prints its stdin.
    task = _named(PLAYBOOKS / "collect-service-conformance.yml", "Aggregate: latest result per service and step")
    outputs = {"results": [{"item": {"id": 5}, "content": "RUN: {}", "status": 200,
                            "invocation": {"module_args": {"headers": {"Authorization": "Bearer sem-token"}}}}]}
    probe = {"ansible.builtin.set_fact": {"_got": "{{ _fetched }}"}, "vars": task["vars"]}
    got, out = _run_tasks(tmp_path, [probe], {"_outputs": outputs}, "_got")
    assert got == [{"item": {"id": 5}, "content": "RUN: {}", "status": 200}]
    assert "sem-token" not in out


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_validate_address_reaches_a_verdict(tmp_path):
    # PR 195 Codex review: _address_errors read _clusters inside the set_fact assigning it.
    path = PLAYBOOKS / "validate-address-free.yml"
    tasks = [_named(path, "Decide"), _named(path, "Decide: judge the address and the record placement")]
    variables = {"_arp_hits": [], "_own_vm": False, "_ip": "192.0.2.10", "_vmid": 1, "_name": "svc",
                 "_nb": {"results": [{"json": {"count": 0, "results": []}},
                                     {"json": {"results": [{"id": 3, "type": {"name": "Proxmox VE"}}]}}]}}
    got, _ = _run_tasks(tmp_path, tasks, variables, "_address_errors")
    assert got == []


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
@pytest.mark.parametrize("enabled,expect_error", [(True, False), (False, True)])
def test_persistence_requires_the_podman_user_boot_unit(tmp_path, enabled, expect_error):
    # PR 195 Codex review: linger without podman-restart.service passed, and the containers stay
    # down after a reboot.
    path = PLAYBOOKS / "verify-service-persistence.yml"
    tasks = [_named(path, "Decide the result"), _named(path, "Decide the failures")]
    variables = {"_policies": {"results": [{"item": "c1", "stdout": "always"}]},
                 "_linger": {"stdout": "Linger=yes"}, "_boot_unit": {"stat": {"exists": enabled}},
                 "_lsc": {"stdout_lines": ["c1"], "rc": 0, "stderr": ""},
                 "_restart_ok": ["always", "unless-stopped"], "_deploy_dir": "/d"}
    got, _ = _run_tasks(tmp_path, tasks, variables, "_persistence_errors")
    assert any("podman-restart.service" in e for e in got) is expect_error, got
