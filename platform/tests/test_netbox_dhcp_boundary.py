"""Live DHCP response must be checked before a NetBox reservation write."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PLAYBOOKS = Path(__file__).resolve().parents[1] / "playbooks"
SERVER = {
    "id": "lan",
    "interface": "LAN",
    "enable": True,
    "range_from": "192.0.2.100",
    "range_to": "192.0.2.150",
    "pool": [{"range_from": "192.0.2.160", "range_to": "192.0.2.170"}],
    "staticmap": [{"ipaddr": "192.0.2.220"}],
}


def check(address, server=SERVER):
    request = {
        "prefix": "192.0.2.0/24",
        "interface": "lan",
        "assignments": [{"address": address}],
        "response": {"code": 200, "data": server},
    }
    return subprocess.run(
        [sys.executable, str(PLAYBOOKS / "netbox_dhcp_boundary.py")],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("192.0.2.120/24", False),
        ("192.0.2.165/24", False),
        ("192.0.2.220/24", False),
        ("192.0.2.219/24", True),
        ("192.0.2.0/24", False),
        ("192.0.2.219/25", False),
    ],
)
def test_live_dhcp_boundary(address, allowed):
    result = check(address)
    assert (result.returncode == 0) is allowed
    assert ("REFUSED" in result.stdout) is not allowed


def test_unknown_router_response_fails_closed():
    for server in (
        None,
        {**SERVER, "pool": None},
        {**SERVER, "id": "wan"},
        {key: value for key, value in SERVER.items() if key != "id"},
        {**SERVER, "range_to": ""},
    ):
        assert check("192.0.2.219/24", server).returncode != 0


def test_router_check_precedes_netbox_write():
    playbook = (PLAYBOOKS / "netbox-allocate-ip.yml").read_text()
    tasks = yaml.safe_load(playbook)[0]["tasks"]
    names = [task["name"] for task in tasks]
    read = names.index("Read the live pfSense DHCP server configuration")
    check_task = names.index("Check the live DHCP boundary before reserving")
    refusal = names.index("Refuse an address pfSense could assign or an unknown boundary")
    write = names.index("Record each new address as allocated")
    assert read < check_task < refusal < write
    assert all(tasks[index]["when"] == "_reserve" for index in (read, check_task, refusal))
    assert tasks[read]["no_log"] and tasks[check_task]["no_log"]
    assert "?id={{ _pfsense_interface | urlencode }}" in tasks[read]["ansible.builtin.uri"]["url"]
    source = tasks[names.index("Require the pfSense DHCP source before reserving")]
    assert "_pfsense_url is match('^https://')" in source["ansible.builtin.assert"]["that"]
