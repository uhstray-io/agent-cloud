"""The local-dev inventory test (filter_plugins/local_dev.py), tested directly.

tasks/assert-local-mode-identity.yml asks this filter whether `groups['all']` is a local-dev
inventory, and the shared guard refuses a local_mode that disagrees with the answer.
test_local_mode_identity.py runs the guard in a real ansible-playbook; these cases pin the
filter's rules on their own: the controller is excluded, every other host must be named
`<name>-local`, an empty inventory is not local-dev, and an input that is not a list of
strings raises instead of answering.
"""

import importlib.util
from pathlib import Path

import pytest
from ansible.errors import AnsibleFilterError

SPEC = importlib.util.spec_from_file_location(
    "local_dev", Path(__file__).resolve().parents[1] / "playbooks/filter_plugins/local_dev.py")
local_dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_dev)


def test_it_is_registered_under_its_name():
    assert local_dev.FilterModule().filters() == {"local_dev_inventory": local_dev.local_dev_inventory}


@pytest.mark.parametrize("hosts", [
    ["dns-local"],
    ["dns-local", "o11y-local"],
    ["localhost", "dns-local"],
    ["dns-local", "localhost", "step-ca-local"],
    ["a.b_c-local"],
])
def test_a_local_dev_inventory_is_recognised(hosts):
    assert local_dev.local_dev_inventory(hosts) is True


@pytest.mark.parametrize("hosts", [
    ["localhost"],                       # no service host at all
    [],                                  # an empty inventory is not local-dev
    ["dns-local", "dns-prod"],           # one host outside the local-dev naming
    ["dns-prod"],
    ["localhost", "dns-prod"],           # a production inventory with its controller
    ["x-localx"],                        # `-local` must end the name
    ["X-local\n"],                       # a trailing newline is not a local name
    ["-local"],                          # the name needs at least one character before it
    ["dns-local", "dns local"],
])
def test_anything_else_is_not_local_dev(hosts):
    assert local_dev.local_dev_inventory(hosts) is False


@pytest.mark.parametrize("hosts", [
    "dns-local",                         # a string is one name, not a list
    None,
    5,
    {"dns-local": {}},                   # a mapping would iterate its keys: refuse, do not guess
    [None],
    ["dns-local", 7],
])
def test_an_input_that_is_not_a_list_of_names_raises(hosts):
    with pytest.raises(AnsibleFilterError):
        local_dev.local_dev_inventory(hosts)
