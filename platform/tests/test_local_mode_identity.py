"""local_mode follows the inventory, not the variable (tasks/assert-local-mode-identity.yml).

The shared placement and linger tasks branch on local_mode, and an extra var outranks the
inventory. These tests run a real ansible-playbook on synthetic inventories and check each
combination of inventory and `-e local_mode=` against the rule the guard enforces:

  - a local-dev inventory (every host but localhost named <name>-local) must have local_mode
    true, from the inventory or the command line;
  - any other inventory must leave local_mode unset or false, so a forged `true` is refused.

A refusal test parametrizes over forgeries.templated_forgeries (docs/MISTAKES.md 1.15): an
extra var renders afresh at each read, so the forged value is a template as well as a plain
value. Every inventory here connects with `local` and every task is an assert or a check-mode
run, so nothing is copied, cloned or written. The place-monorepo case runs with --check: its
first task refuses before any copy. A static test pins place-monorepo.yml's first task to the
include, so the guard cannot be placed after a read of local_mode.
"""

import json
import re
from pathlib import Path

import forgeries
import harness_sandbox
import pytest
import yaml
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/assert-local-mode-identity.yml"
PLACE = REPO / "platform/playbooks/tasks/place-monorepo.yml"
REFUSAL = "The mode follows the inventory, not the variable"
NAME = "Refuse a local_mode that disagrees with the inventory's identity"

pytestmark = needs_ansible

LOCAL_INI = """\
localhost ansible_connection=local
dns-local ansible_connection=local

[all:vars]
local_mode=true
"""
LOCAL_UNSET_INI = """\
localhost ansible_connection=local
dns-local ansible_connection=local
"""
PROD_INI = """\
localhost ansible_connection=local
dns-prod ansible_connection=local
"""
EXAMPLE_YAML = """\
all:
  vars:
    local_mode: true
  hosts:
    dns-local:
      ansible_connection: local
"""

def _run(tmp_path: Path, inventory: str, *args: str, hosts: str = "all", play: str | None = None,
         suffix: str = "ini", check: bool = False):
    inv = tmp_path / f"inventory.{suffix}"
    inv.write_text(inventory)
    playbook = tmp_path / "play.yml"
    playbook.write_text(play if play is not None else (
        f"- hosts: {hosts}\n"
        "  connection: local\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - name: the shared identity check\n"
        f"      ansible.builtin.include_tasks: {json.dumps(str(TASK))}\n"))
    flags = ["--check"] if check else []
    proc = harness_sandbox.run_playbook(playbook, tmp_path, *args, *flags, inventory=inv)
    proc.output = " ".join((proc.stdout + proc.stderr).split())   # one line, whatever the wrapping
    return proc


def _passed(proc) -> None:
    assert proc.returncode == 0, proc.output
    assert "failed=0" in proc.output, proc.output
    assert re.search(r"ok=[1-9]", proc.output), proc.output


def _refused(proc) -> None:
    assert proc.returncode != 0, proc.output
    assert REFUSAL in proc.output, proc.output


def test_a_local_dev_inventory_with_local_mode_true_passes(tmp_path):
    _passed(_run(tmp_path, LOCAL_INI))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=True, forged=False))
def test_a_local_dev_inventory_refuses_a_command_line_false(tmp_path, forge):
    _refused(_run(tmp_path, LOCAL_INI, "-e", forge(tmp_path)))


def test_a_local_dev_inventory_with_no_local_mode_is_refused(tmp_path):
    _refused(_run(tmp_path, LOCAL_UNSET_INI))


def test_a_production_inventory_with_no_local_mode_passes(tmp_path):
    _passed(_run(tmp_path, PROD_INI))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_a_production_inventory_refuses_a_forged_local_mode(tmp_path, forge):
    _refused(_run(tmp_path, PROD_INI, "-e", forge(tmp_path)))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_a_forged_local_mode_is_refused_even_with_a_local_looking_inventory_hostname(tmp_path, forge):
    _refused(_run(tmp_path, PROD_INI, "-e", forge(tmp_path), "-e", "inventory_hostname=dns-local"))


def test_a_production_inventory_with_local_mode_false_passes(tmp_path):
    _passed(_run(tmp_path, PROD_INI, "-e", "local_mode=false"))


def test_the_example_shape_passes_on_the_implicit_localhost_play(tmp_path):
    # platform/inventory/local-dev.yml.example: all.vars.local_mode true, one `-local` host, and a
    # play on localhost, which the inventory does not list.
    _passed(_run(tmp_path, EXAMPLE_YAML, hosts="localhost", suffix="yml"))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_place_monorepo_refuses_a_forged_local_mode_before_it_copies_anything(tmp_path, forge):
    dst = tmp_path / "dst"
    play = (
        "- hosts: all\n"
        "  connection: local\n"
        "  gather_facts: false\n"
        "  vars:\n"
        f"    _monorepo_dir: {json.dumps(str(dst))}\n"
        "    _branch: main\n"
        "    monorepo_repo: https://example.invalid/agent-cloud.git\n"
        "  tasks:\n"
        "    - name: place the monorepo\n"
        f"      ansible.builtin.include_tasks: {json.dumps(str(PLACE))}\n")
    proc = _run(tmp_path, PROD_INI, "-e", forge(tmp_path), play=play, check=True)
    _refused(proc)
    assert not dst.exists(), proc.output


def test_place_monorepo_names_its_first_task_as_the_identity_guard():
    tasks = yaml.safe_load(PLACE.read_text())
    first = tasks[0]
    assert first["name"] == NAME, first
    assert first.get("ansible.builtin.include_tasks") == "assert-local-mode-identity.yml", first
    assert "when" not in first, first
    assert TASK.is_file()


def test_the_guard_file_is_one_named_assert_with_no_condition_of_its_own():
    tasks = yaml.safe_load(TASK.read_text())
    assert len(tasks) == 1, tasks
    assert tasks[0]["name"] == NAME, tasks[0]
    assert "ansible.builtin.assert" in tasks[0], tasks[0]
    assert "when" not in tasks[0], tasks[0]
