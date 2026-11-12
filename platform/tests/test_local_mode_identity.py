"""local_mode follows the inventory, not the variable (tasks/assert-local-mode-identity.yml).

The shared placement and linger tasks branch on local_mode, and an extra var outranks the
inventory. These tests run a real ansible-playbook on the inventories in local_mode_inventories.py
and check each combination of inventory and `-e local_mode=` against the rule the guard enforces:

  - a local-dev inventory (every host but localhost named <name>-local) must have local_mode
    true, from the inventory or the command line;
  - any other inventory must leave local_mode unset or false, so a forged `true` is refused.

A refusal test parametrizes over forgeries.templated_forgeries (docs/MISTAKES.md 1.15): an
extra var renders afresh at each read, so the forged value is a template as well as a plain
value. Every inventory here connects with `local` and every task is an assert or a check-mode
run, so nothing is copied, cloned or written. The place-monorepo case runs with --check: its
first task refuses before any copy. A static test pins the first task of place-monorepo.yml and
enable-linger.yml to the include, so the guard cannot be placed after a read of local_mode. A
caller's own raw local_mode gate is not covered here: AC-726.
"""

import re
from pathlib import Path

import forgeries
import playbook_yaml
import pytest
from ansible_python import needs_ansible
from local_mode_inventories import (
    EXAMPLE_YAML,
    IDENTITY_REFUSAL,
    IDENTITY_TASK,
    LOCAL_INI,
    PROD_INI,
    include_play,
    passed,
    refused,
    run,
)

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/assert-local-mode-identity.yml"
PLACE = REPO / "platform/playbooks/tasks/place-monorepo.yml"
LINGER = REPO / "platform/playbooks/tasks/enable-linger.yml"

pytestmark = needs_ansible

LOCAL_UNSET_INI = """\
localhost ansible_connection=local
dns-local ansible_connection=local
"""


def _guard(tmp_path, inventory, *args, hosts="all", suffix="ini"):
    return run(tmp_path, inventory, include_play(TASK, hosts=hosts), *args, suffix=suffix)


def test_a_local_dev_inventory_with_local_mode_true_passes(tmp_path):
    passed(_guard(tmp_path, LOCAL_INI))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=True, forged=False))
def test_a_local_dev_inventory_refuses_a_command_line_false(tmp_path, forge):
    refused(_guard(tmp_path, LOCAL_INI, "-e", forge(tmp_path)), IDENTITY_REFUSAL)


def test_a_local_dev_inventory_with_no_local_mode_is_refused(tmp_path):
    refused(_guard(tmp_path, LOCAL_UNSET_INI), IDENTITY_REFUSAL)


def test_a_production_inventory_with_no_local_mode_passes(tmp_path):
    passed(_guard(tmp_path, PROD_INI))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_a_forged_local_mode_is_refused_even_with_a_local_looking_inventory_hostname(tmp_path, forge):
    refused(_guard(tmp_path, PROD_INI, "-e", forge(tmp_path), "-e", "inventory_hostname=dns-local"),
            IDENTITY_REFUSAL)


def test_a_production_inventory_with_local_mode_false_passes(tmp_path):
    passed(_guard(tmp_path, PROD_INI, "-e", "local_mode=false"))


def test_the_example_shape_passes_on_the_implicit_localhost_play(tmp_path):
    # platform/inventory/local-dev.yml.example: all.vars.local_mode true, one `-local` host, and a
    # play on localhost, which the inventory does not list.
    passed(_guard(tmp_path, EXAMPLE_YAML, hosts="localhost", suffix="yml"))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_place_monorepo_refuses_a_forged_local_mode_before_its_own_tasks(tmp_path, forge):
    play = include_play(PLACE, play_vars={
        "_monorepo_dir": str(tmp_path / "dst"),
        "_branch": "main",
        "monorepo_repo": "https://example.invalid/agent-cloud.git",
    })
    proc = run(tmp_path, PROD_INI, play, "-e", forge(tmp_path), "--check")
    refused(proc, IDENTITY_REFUSAL)
    # Refused by place-monorepo's own first task, not later by enable-linger.yml's copy of the check
    # (its last include): no task of place-monorepo ran. Under --check the copy would not write, so
    # the absence of the destination would prove nothing.
    assert "Assert required variables for monorepo placement" not in proc.output, proc.output


@pytest.mark.parametrize("path", [PLACE, LINGER], ids=["place-monorepo", "enable-linger"])
def test_the_shared_callers_name_the_identity_guard_as_their_first_task(path):
    first = playbook_yaml.load(path)[0]
    assert first["name"] == IDENTITY_TASK, first
    assert first.get("ansible.builtin.include_tasks") == "assert-local-mode-identity.yml", first
    assert "when" not in first, first


def test_the_guard_file_is_one_named_assert_with_no_condition_of_its_own():
    tasks = playbook_yaml.load(TASK)
    assert len(tasks) == 1, tasks
    assert tasks[0]["name"] == IDENTITY_TASK, tasks[0]
    assert "ansible.builtin.assert" in tasks[0], tasks[0]
    assert "when" not in tasks[0], tasks[0]


def _linger(tmp_path, inventory, *args):
    # linger_user is set so the linger task names render on any host, and a run that reaches a
    # linger task fails for no reason but the guard's. --check: nothing is enabled or written.
    play = include_play(LINGER, hosts="localhost", play_vars={"linger_user": "testuser"})
    return run(tmp_path, inventory, play, *args, "--check")


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_a_forged_local_mode_is_refused_before_any_linger_task(tmp_path, forge):
    proc = _linger(tmp_path, PROD_INI, "-e", forge(tmp_path))
    refused(proc, IDENTITY_REFUSAL)
    assert f"TASK [{IDENTITY_TASK}]" in proc.output, proc.output
    assert "TASK [Check whether lingering" not in proc.output, proc.output


def test_a_local_dev_inventory_passes_the_linger_guard_and_skips_the_linger_tasks(tmp_path):
    proc = _linger(tmp_path, LOCAL_INI)
    passed(proc)
    # The linger tasks self-gate under local_mode: they are skipped, not run.
    assert re.search(r"skipped=[1-9]", proc.output), proc.output
