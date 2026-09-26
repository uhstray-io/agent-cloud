"""Live DHCP response must be checked before a NetBox reservation write."""

import json
import os
import shutil
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
    parsed = yaml.safe_load(playbook)[0]
    tasks = parsed["tasks"]
    names = [task["name"] for task in tasks]
    read = names.index("Read the live pfSense DHCP server configuration")
    check_task = names.index("Check the live DHCP boundary before reserving")
    refusal = names.index("Refuse an address pfSense could assign or an unknown boundary")
    write = names.index("Record each new address as allocated")
    assert read < check_task < refusal < write
    assert all(tasks[index]["when"] == "_reserve" for index in (read, check_task, refusal))
    assert tasks[read]["no_log"] and tasks[check_task]["no_log"]
    site_url = "hostvars[groups['netbox_svc'][0]].pfsense_dhcp_api_url"
    site_interface = "hostvars[groups['netbox_svc'][0]].pfsense_dhcp_interface"
    assert tasks[read]["ansible.builtin.uri"]["url"] == (
        f"{{{{ {site_url} }}}}/api/v2/services/dhcp_server?id={{{{ {site_interface} | urlencode }}}}"
    )
    site_tls = "hostvars[groups['netbox_svc'][0]].pfsense_dhcp_validate_certs | default(true)"
    assert not {"_netbox_site", "_pfsense_url", "_pfsense_interface", "_pfsense_validate_certs"} & parsed["vars"].keys()
    assert tasks[read]["ansible.builtin.uri"]["validate_certs"] == f"{{{{ {site_tls} | bool }}}}"
    tls_guard = names.index("Require inventory-owned pfSense DHCP settings")
    tls_notice = names.index("Report a private pfSense TLS exception")
    assert tls_guard < names.index("Authenticate to OpenBao (AppRole)")
    assert tls_guard < tls_notice < read
    assert tasks[tls_guard]["when"] == "_reserve"
    assert tasks[tls_guard]["ansible.builtin.assert"]["that"] == [
        "pfsense_dhcp_api_url is not defined",
        "pfsense_dhcp_interface is not defined",
        "pfsense_dhcp_validate_certs is not defined",
        f"({site_tls}) is boolean",
    ]
    assert tasks[tls_notice]["when"] == ["_reserve", f"not ({site_tls} | bool)"]
    source = tasks[names.index("Require the pfSense DHCP source before reserving")]
    assert "groups.get('netbox_svc', []) | length == 1" in source["ansible.builtin.assert"]["that"]
    assert f"{site_url} is match('^https://')" in source["ansible.builtin.assert"]["that"]
    transport = tasks[names.index("Refuse a cleartext pfSense endpoint")]
    assert transport["vars"]["_assert_bao_url"] == f"{{{{ {site_url} }}}}"
    assert site_interface in tasks[check_task]["ansible.builtin.command"]["stdin"]


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook is unavailable")
def test_tls_choice_uses_private_host_even_with_play_var_overrides(tmp_path):
    """Execute actual guards and URI templates without contacting a host."""
    parsed = yaml.safe_load((PLAYBOOKS / "netbox-allocate-ip.yml").read_text())[0]
    tasks = {task["name"]: task for task in parsed["tasks"]}
    source_guard = tasks["Require the pfSense DHCP source before reserving"]
    guard = tasks["Require inventory-owned pfSense DHCP settings"]
    notice = tasks["Report a private pfSense TLS exception"]
    uri = tasks["Read the live pfSense DHCP server configuration"]["ansible.builtin.uri"]
    for private_value, overrides, expected, refused in (
        (None, {}, True, False),
        (False, {}, False, False),
        (False, {
            "_pfsense_validate_certs": True,
            "_netbox_site": {},
            "_pfsense_url": "https://attacker.example",
            "_pfsense_interface": "wan",
        }, False, False),
        (False, {"pfsense_dhcp_validate_certs": True}, None, True),
        (False, {"pfsense_dhcp_api_url": "https://attacker.example"}, None, True),
        (False, {"pfsense_dhcp_interface": "wan"}, None, True),
        ("false", {}, None, True),
    ):
        host_vars = {"pfsense_dhcp_api_url": "https://router.example", "pfsense_dhcp_interface": "lan"}
        if private_value is not None:
            host_vars["pfsense_dhcp_validate_certs"] = private_value
        inventory = tmp_path / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"children": {"netbox_svc": {"hosts": {"netbox": host_vars}}}}}))
        playbook = tmp_path / "tls-check.yml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost",
            "gather_facts": False,
            "vars": {"_reserve": True},
            "tasks": [
                source_guard,
                guard,
                notice,
                {
                    "name": "Evaluate the actual URI TLS template",
                    "ansible.builtin.debug": {"msg": uri["validate_certs"]},
                },
                {"name": "Evaluate the actual URI URL template", "ansible.builtin.debug": {"msg": uri["url"]}},
            ],
        }]))
        command = ["ansible-playbook", "-i", str(inventory), str(playbook)]
        if overrides:
            command.extend(["-e", json.dumps(overrides)])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path)},
        )
        output = result.stdout + result.stderr
        assert (result.returncode != 0) is refused, output
        if not refused:
            assert f'"msg": {str(expected).lower()}' in output, output
            assert '"msg": "https://router.example/api/v2/services/dhcp_server?id=lan"' in output, output
            assert ("pfSense DHCP certificate validation is disabled" in output) is (not expected)
