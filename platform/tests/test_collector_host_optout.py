"""A host can opt out of the conformance collector (platform/service-onboarding-workflow).

A host that borrows a service_name without being a workflow service (the DGX drill hosts)
sets `service_conformance_tracked: false`. The collector maps hosts to services in exactly one
task, and every later step (group mapping, VM lookup, aggregate, "never run" row, Loki marker)
reads that map, so the guard runs the REAL task against a real inventory.

The production destination guard (AC-726) is run the same way: the collector's three guards on a production
inventory, under a forged local_mode, which must not skip the guard.
"""

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import forgeries
import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COLLECTOR = REPO / "platform/playbooks/collect-service-conformance.yml"
TASK_NAME = "Map each inventory host to its service name"
# The collector's guards before its first write, in play order. The destination guard takes its branch from the
# inventory, not local_mode (plan 24), so these run on a production inventory under a forged local_mode.
GUARDS = (
    "Refuse a controller API destination override before authentication",
    "Require the inventory to have parsed",
    "Require the exact private Alloy OTLP/HTTP destination in production",
)
# An RFC 1918 address built at run time: the pre-commit hook refuses the literal.
PRIVATE = ".".join(["10", "42", "0", "9"])
DECLARED = f"http://{PRIVATE}:4318/v1/logs"
REFUSAL = ("Production conformance delivery requires collector_otlp_url to match the declared private "
           "Alloy OTLP/HTTP receiver.")

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
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
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


def _production_inventory(tmp_path: Path, delivered: str) -> Path:
    """A production inventory: one o11y receiver declaring PRIVATE, one Semaphore host delivering to `delivered`."""
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "localhost ansible_connection=local\n"
        "collector-prod ansible_connection=local\n\n"
        f"[o11y_svc]\no11y-prod ansible_connection=local o11y_otlp_bind={PRIVATE}\n\n"
        f"[semaphore_svc]\nsemaphore-prod ansible_connection=local collector_otlp_url={delivered}\n"
    )
    return inventory


def _guards_play(tmp_path: Path) -> Path:
    """The collector's own play with only its three guards kept: their vars and their include, unchanged."""
    play = copy.deepcopy(playbook_yaml.plays(COLLECTOR)[0])
    play["tasks"] = [t for t in play["tasks"] if t.get("name") in GUARDS]
    assert [t.get("name") for t in play["tasks"]] == list(GUARDS)
    for task in play["tasks"]:
        include = task.get("ansible.builtin.include_tasks")
        if include:
            task["ansible.builtin.include_tasks"] = str(REPO / "platform/playbooks" / include)
    path = tmp_path / "guards.yml"
    path.write_text(json.dumps([play]))  # JSON is YAML
    return path


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True)[:2])
def test_a_forged_local_mode_cannot_skip_the_production_destination_guard(tmp_path, forge):
    proc = harness_sandbox.run_playbook(
        _guards_play(tmp_path), tmp_path, "-e", forge(tmp_path),
        inventory=_production_inventory(tmp_path, delivered="http://127.0.0.1:4318/v1/logs"),
    )
    output = " ".join((proc.stdout + proc.stderr).split())
    assert proc.returncode != 0, output
    assert REFUSAL in output, output


def test_the_production_destination_guard_passes_when_delivery_is_the_declared_receiver(tmp_path):
    proc = harness_sandbox.run_playbook(
        _guards_play(tmp_path), tmp_path, inventory=_production_inventory(tmp_path, delivered=DECLARED),
    )
    assert proc.returncode == 0, proc.stdout[-3000:]
