"""The OpenBao transport guard's local-mode forms need a local-dev inventory too (AC-696, AC-726).

tasks/assert-bao-transport.yml accepts a single-label http:// host, and host.containers.internal,
only on a local-dev inventory (the identity filter_plugins/local_dev.py decides, read from `groups`).
The guard does not read local_mode at all (plan 24): before AC-726 it also required local_mode, and an
extra var outranks the inventory. Before AC-696, local_mode alone opened those forms, so `-e
local_mode=true` on a production run widened the guard that keeps the AppRole secret_id off a public name.

These tests run the real guard in a real ansible-playbook on the inventories in
local_mode_inventories.py: a scratch play on localhost includes the task with the URL set on the
include. A forged local_mode is a templated forgery (docs/MISTAKES.md 1.15). Private addresses are
built from octets, so no literal private address sits in this file (the IP gate).
"""

import json
from pathlib import Path

import forgeries
import pytest
from ansible_python import needs_ansible
from local_mode_inventories import (
    EXAMPLE_YAML,
    LOCAL_INI,
    PROD_INI,
    include_play,
    passed,
    refused,
    run,
)

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/assert-bao-transport.yml"
REFUSAL = "Refusing to send secret material"

pytestmark = needs_ansible

SINGLE_LABEL = "http://local-openbao:8200"
DOTTED_ENGINE_NAME = "http://host.containers.internal:8000"
LOCAL_MODE_FORMS = [SINGLE_LABEL, DOTTED_ENGINE_NAME]
PRIVATE_URL = "http://" + ".".join(map(str, (10, 0, 0, 5))) + ":8200"


def _guard(tmp_path, inventory, url, *args, suffix="ini"):
    play = include_play(TASK, hosts="localhost", task_vars={"_assert_bao_url": url})
    return run(tmp_path, inventory, play, *args, suffix=suffix)


def test_a_local_dev_inventory_under_local_mode_takes_the_single_label_form(tmp_path):
    passed(_guard(tmp_path, LOCAL_INI, SINGLE_LABEL))


@pytest.mark.parametrize("url", LOCAL_MODE_FORMS)
def test_the_local_mode_forms_pass_on_the_example_shape_with_implicit_localhost(tmp_path, url):
    # platform/inventory/local-dev.yml.example: all.vars.local_mode true, one `-local` host, and a
    # play on localhost, which the inventory does not list.
    passed(_guard(tmp_path, EXAMPLE_YAML, url, suffix="yml"))


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=True, forged=False))
def test_a_local_dev_inventory_takes_the_single_label_form_whatever_local_mode_says(tmp_path, forge):
    # The mode follows the inventory: a forged false cannot close the form on a local-dev inventory. Before
    # AC-726 the guard also read local_mode, so this refused. The plain and context forms show it; a
    # stateful form is honest on its only read here and so cannot.
    passed(_guard(tmp_path, LOCAL_INI, SINGLE_LABEL, "-e", forge(tmp_path)))


@pytest.mark.parametrize("url", LOCAL_MODE_FORMS)
def test_a_production_inventory_without_local_mode_refuses_the_local_mode_forms(tmp_path, url):
    refused(_guard(tmp_path, PROD_INI, url), REFUSAL)


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_a_forged_local_mode_does_not_open_the_local_mode_forms_on_production(tmp_path, forge):
    # The single-label form only: the identity term refuses the forgery for either URL form, so the
    # dotted name would repeat these cases.
    refused(_guard(tmp_path, PROD_INI, SINGLE_LABEL, "-e", forge(tmp_path)), REFUSAL)


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_forged_groups_do_not_make_a_production_inventory_local_dev(tmp_path, forge):
    # The identity is read from groups. This forges that membership on the command line as well as
    # the mode: the guard must still refuse, so groups must not be settable with -e.
    forged_groups = json.dumps({"groups": {"all": ["dns-local"]}})
    refused(_guard(tmp_path, PROD_INI, SINGLE_LABEL, "-e", forge(tmp_path), "-e", forged_groups), REFUSAL)


def test_a_production_inventory_takes_an_https_endpoint(tmp_path):
    passed(_guard(tmp_path, PROD_INI, "https://bao.example.test"))


def test_a_production_inventory_takes_a_private_address(tmp_path):
    passed(_guard(tmp_path, PROD_INI, PRIVATE_URL))
