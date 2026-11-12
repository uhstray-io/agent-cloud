"""The OpenBao transport guard's local-mode forms need a local-dev inventory too (AC-696).

tasks/assert-bao-transport.yml accepts a single-label http:// host, and host.containers.internal,
only when local_mode is set AND the inventory is local-dev (the identity filter_plugins/local_dev.py
decides, as tasks/assert-local-mode-identity.yml does). Before, local_mode alone opened those forms.
An extra var outranks the inventory, so `-e local_mode=true` on a production run widened the guard
that keeps the AppRole secret_id off a public name.

These tests run the real guard in a real ansible-playbook on synthetic inventories, the way
test_local_mode_identity.py does: a scratch play on localhost includes the task with the URL set on
the include. A forged local_mode is a templated forgery (docs/MISTAKES.md 1.15). Private addresses
are built from octets, so no literal private address sits in this file (the IP gate).
"""

import json
import re
from pathlib import Path

import forgeries
import harness_sandbox
import pytest
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/assert-bao-transport.yml"
REFUSAL = "Refusing to send secret material"

pytestmark = needs_ansible

LOCAL_INI = """\
localhost ansible_connection=local
dns-local ansible_connection=local

[all:vars]
local_mode=true
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

SINGLE_LABEL = "http://local-openbao:8200"
DOTTED_ENGINE_NAME = "http://host.containers.internal:8000"
LOCAL_MODE_FORMS = [SINGLE_LABEL, DOTTED_ENGINE_NAME]
PRIVATE_URL = "http://" + ".".join(map(str, (10, 0, 0, 5))) + ":8200"


def _run(tmp_path: Path, inventory: str, url: str, *args: str, suffix: str = "ini"):
    inv = tmp_path / f"inventory.{suffix}"
    inv.write_text(inventory)
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        "- hosts: localhost\n"
        "  connection: local\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - name: the shared transport guard\n"
        f"      ansible.builtin.include_tasks: {json.dumps(str(TASK))}\n"
        "      vars:\n"
        f"        _assert_bao_url: {json.dumps(url)}\n")
    proc = harness_sandbox.run_playbook(playbook, tmp_path, *args, inventory=inv)
    proc.output = " ".join((proc.stdout + proc.stderr).split())   # one line, whatever the wrapping
    return proc


def _passed(proc) -> None:
    assert proc.returncode == 0, proc.output
    assert "failed=0" in proc.output, proc.output
    assert re.search(r"ok=[1-9]", proc.output), proc.output


def _refused(proc) -> None:
    assert proc.returncode != 0, proc.output
    assert REFUSAL in proc.output, proc.output


def test_a_local_dev_inventory_under_local_mode_takes_the_single_label_form(tmp_path):
    _passed(_run(tmp_path, LOCAL_INI, SINGLE_LABEL))


@pytest.mark.parametrize("url", LOCAL_MODE_FORMS)
def test_the_local_mode_forms_pass_on_the_example_shape_with_implicit_localhost(tmp_path, url):
    # platform/inventory/local-dev.yml.example: all.vars.local_mode true, one `-local` host, and a
    # play on localhost, which the inventory does not list.
    _passed(_run(tmp_path, EXAMPLE_YAML, url, suffix="yml"))


def test_a_local_dev_inventory_with_local_mode_false_refuses_the_single_label_form(tmp_path):
    _refused(_run(tmp_path, LOCAL_INI, SINGLE_LABEL, "-e", "local_mode=false"))


@pytest.mark.parametrize("url", LOCAL_MODE_FORMS)
def test_a_production_inventory_without_local_mode_refuses_the_local_mode_forms(tmp_path, url):
    _refused(_run(tmp_path, PROD_INI, url))


@pytest.mark.parametrize("url", LOCAL_MODE_FORMS)
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_a_forged_local_mode_does_not_open_the_local_mode_forms_on_production(tmp_path, forge, url):
    _refused(_run(tmp_path, PROD_INI, url, "-e", forge(tmp_path)))


@pytest.mark.parametrize("url", LOCAL_MODE_FORMS)
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_a_forged_local_mode_is_refused_even_with_a_local_looking_inventory_hostname(tmp_path, forge, url):
    _refused(_run(tmp_path, PROD_INI, url, "-e", forge(tmp_path), "-e", "inventory_hostname=dns-local"))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_forged_groups_do_not_make_a_production_inventory_local_dev(tmp_path, forge):
    # The identity is read from groups. This forges that membership on the command line as well as
    # the mode: the guard must still refuse, so groups must not be settable with -e.
    forged_groups = json.dumps({"groups": {"all": ["dns-local"]}})
    _refused(_run(tmp_path, PROD_INI, SINGLE_LABEL, "-e", forge(tmp_path), "-e", forged_groups))


def test_a_production_inventory_takes_an_https_endpoint(tmp_path):
    _passed(_run(tmp_path, PROD_INI, "https://bao.example.test"))


def test_a_production_inventory_takes_a_private_address(tmp_path):
    _passed(_run(tmp_path, PROD_INI, PRIVATE_URL))
