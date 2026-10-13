"""A host can opt out of the conformance collector (platform/service-onboarding-workflow).

A host that borrows a service_name without being a workflow service (the DGX drill hosts)
sets `service_conformance_tracked: false`. The collector maps hosts to services in exactly one
task, and every later step (group mapping, VM lookup, aggregate, "never run" row, Loki marker)
reads that map, so the guard runs the REAL task against a real inventory.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COLLECTOR = REPO / "platform/playbooks/collect-service-conformance.yml"
TASK_NAME = "Map each inventory host to its service name"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

HOSTS = """\
[a_svc]
tracked ansible_connection=local service_name=alpha
[b_svc]
flag_true ansible_connection=local service_name=beta service_conformance_tracked=true
[drill_svc]
flag_false ansible_connection=local service_name=drill service_conformance_tracked=false
[c_svc]
flag_string ansible_connection=local service_name=gamma service_conformance_tracked="false"
[d_svc]
flag_unset ansible_connection=local service_name=delta
[e_svc]
no_service ansible_connection=local
"""


def _mapping_task() -> dict:
    found = [t for t in playbook_yaml.tasks(playbook_yaml.load(COLLECTOR)) if t.get("name") == TASK_NAME]
    assert len(found) == 1, f"expected one '{TASK_NAME}' task in the collector"
    return found[0]


def _host_services(tmp_path: Path) -> dict:
    out = tmp_path / "map.json"
    inv = tmp_path / "inv.ini"
    inv.write_text(HOSTS)
    play = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "tasks": [
                _mapping_task(),
                {"ansible.builtin.copy": {"content": "{{ _host_services | default({}) | to_json }}", "dest": str(out)}},
            ],
        }
    ]
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump(play, sort_keys=False))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inv), str(pb)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(out.read_text())


def test_a_host_with_the_flag_false_contributes_no_service(tmp_path):
    mapped = _host_services(tmp_path)
    assert "flag_false" not in mapped
    assert "drill" not in mapped.values()


def test_the_string_false_is_honoured(tmp_path):
    # an inventory or extra var written as a string must not read as truthy
    assert "flag_string" not in _host_services(tmp_path)


def test_a_host_without_the_flag_or_with_it_true_is_still_tracked(tmp_path):
    mapped = _host_services(tmp_path)
    assert mapped["tracked"] == "alpha"
    assert mapped["flag_true"] == "beta"
    assert mapped["flag_unset"] == "delta"
    assert "no_service" not in mapped


def test_the_opt_out_reaches_the_parser_input(tmp_path):
    # the map the parser receives is this one: a group whose first host opted out names no service
    mapped = _host_services(tmp_path)
    step_results = playbook_yaml.REPO / "platform/workflows/service-onboarding/lib/step_results.py"
    proc = subprocess.run(
        ["python3", str(step_results)],
        text=True,
        capture_output=True,
        check=False,
        input=json.dumps(
            {
                "mode": "select",
                "registry": [],
                "templates": [],
                "deploy_templates": [],
                "groups": {"a_svc": ["tracked"], "drill_svc": ["flag_false"]},
                "host_services": mapped,
            }
        ),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["inventory_services"] == ["alpha"]
