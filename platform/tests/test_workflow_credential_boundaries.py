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
        {"item": "svc-a", "status": 200,
         "json": {"count": 1, "results": [{"id": 7, "custom_fields": {"ac_workflow_status": {"fw-harden": "pass"}}}]},
         "invocation": {"module_args": {"headers": {"Authorization": "Bearer nbt_secret.value"}}}},
        {"item": "svc-b", "status": 200, "json": {"count": 0, "results": []}},
        # a record whose status field was never written reads as {}, not null
        {"item": "svc-d", "status": 200,
         "json": {"count": 1, "results": [{"id": 4, "custom_fields": {"ac_workflow_status": None}}]}},
        # same-named VMs in two clusters: never write the first (PR 195 Codex review)
        {"item": "svc-c", "status": 200, "json": {"count": 2, "results": [{"id": 8}, {"id": 9}]}},
    ]
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_vms": {"results": results}},
        "tasks": [sort, {"ansible.builtin.debug": {"msg": "FOUND {{ _vm_found | to_json }}"}},
                  {"ansible.builtin.debug": {"msg": "AMBIGUOUS {{ _vm_ambiguous | to_json }}"}}],
    }]
    path = tmp_path / "sort.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                         text=True, capture_output=True, check=True).stdout
    line = next(ln for ln in out.splitlines() if "FOUND " in ln)
    found = json.loads(json.loads(line.split('"msg": ', 1)[1]).split("FOUND ", 1)[1])
    # the name, the id and the status NetBox already holds, which the write merges into
    assert found == [{"item": "svc-a", "id": 7, "status": {"fw-harden": "pass"}},
                     {"item": "svc-d", "id": 4, "status": {}}]
    assert '"AMBIGUOUS [\\"svc-c\\"]"' in out, out[-600:]
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


PVE_CLUSTER = {"id": 3, "name": "pve", "type": {"name": "Proxmox VE"}}


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
@pytest.mark.parametrize("records,errors", [
    ([], []),                                                    # none yet: created in the cluster
    ([{"id": 7, "cluster": {"id": 3, "name": "pve"}}], []),      # ours, in discovery's cluster
    ([{"id": 7, "cluster": {"id": 9, "name": "other"}}], ["the NetBox VM record svc is in cluster other"]),
    ([{"id": 7, "cluster": {"id": 3}}, {"id": 8, "cluster": {"id": 9}}], ["2 NetBox VM records are named svc"]),
])
def test_validate_address_reaches_a_verdict(tmp_path, records, errors):
    # PR 195 Codex reviews: _address_errors read _clusters inside the set_fact assigning it, and
    # any same-named record, in any cluster or one of several, passed as the service's own.
    path = PLAYBOOKS / "validate-address-free.yml"
    tasks = [_named(path, "Decide"), _named(path, "Decide: judge the address and the record placement")]
    variables = {"_arp_hits": [], "_own_vm": False, "_ip": "192.0.2.10", "_vmid": 1, "_name": "svc",
                 "_nb": {"results": [{"json": {"count": len(records), "results": records}},
                                     {"json": {"results": [PVE_CLUSTER]}}]}}
    got, _ = _run_tasks(tmp_path, tasks, variables, "_address_errors")
    assert [e for e in got if not any(e.startswith(w) for w in errors)] == [] and len(got) == len(errors), got


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


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_persistence_accepts_only_what_boots(tmp_path):
    # podman-restart.service, rootful and rootless, starts only `restart: always` containers
    # (test_restart_policy.bats). "no" is accepted only from a one-shot init container that has
    # exited 0, never from a running one (PR review, Codex). Rootful podman once kept accepting
    # unless-stopped (PR 195 grounding review).
    path = PLAYBOOKS / "verify-service-persistence.yml"
    play = yaml.safe_load(path.read_text())[0]
    tasks = [_named(path, "Decide the result"), _named(path, "Decide the failures")]
    inspected = {"a": "always running 0 ", "b": "no exited 0 true", "c": "unless-stopped running 0 ",
                 "d": "on-failure running 0 ", "e": "no running 0 true", "f": "no exited 1 true",
                 "g": "no exited 0 "}  # g: exited 0 but never declared one-shot (PR 253 Codex review)
    variables = {**play["vars"], "_engine": "podman", "podman_rootful": True, "_deploy_dir": "/d",
                 "_policies": {"results": [{"item": k, "stdout": v} for k, v in inspected.items()]},
                 "_linger": {"skipped": True}, "_boot_unit": {"skipped": True},
                 "_lsc": {"stdout_lines": list(inspected), "rc": 0, "stderr": ""}}
    got, _ = _run_tasks(tmp_path, tasks, variables, "[_persistence_errors, _restart_policies]")
    errors, policies = got
    assert errors == ["restart policy not always: c", "restart policy not always: d",
                      *(f'restart policy "no" but not a declared one-shot container '
                        f'(label agent-cloud.one-shot=true) that exited 0: {c}' for c in "efg")]
    # the evidence keeps the policy alone
    assert policies == {k: v.split()[0] for k, v in inspected.items()}


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_the_aggregate_retains_what_each_vm_already_holds(tmp_path):
    # PR 258 Codex review: the parser merges NetBox's statuses so the report and Loki agree
    # with the write; the collector hands it exactly {service: status} from the lookup.
    task = _named(PLAYBOOKS / "collect-service-conformance.yml", "Aggregate: latest result per service and step")
    probe = {"ansible.builtin.set_fact": {"_got": "{{ _retained }}"}, "vars": task["vars"]}
    found = [{"item": "svc-a", "id": 7, "status": {"fw-harden": "fail"}}, {"item": "svc-d", "id": 4, "status": {}}]
    got, _ = _run_tasks(tmp_path, [probe], {"_vm_found": found, "_outputs": {"results": []}}, "_got")
    assert got == {"svc-a": {"fw-harden": "fail"}, "svc-d": {}}
    write = _named(PLAYBOOKS / "collect-service-conformance.yml", "NetBox: write each service's workflow status")
    fields = write["ansible.builtin.uri"]["body"]["custom_fields"]
    assert fields["ac_workflow_status"] == "{{ _agg.status_by_service[item.item] }}"
