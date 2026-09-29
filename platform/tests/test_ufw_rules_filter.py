"""The ufw rule readers (filter_plugins/ufw_rules.py), tested directly.

test_apply_firewall_convergence.py proves apply-firewall.yml wires them into the prune
guard; the spellings live here, as function calls instead of playbook runs. Specs are in
the form `ufw show added` prints (ufw 0.36.2 src/parser.py get_command), without the
leading `ufw ` and the comment.
"""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "ufw_rules", Path(__file__).resolve().parents[1] / "playbooks/filter_plugins/ufw_rules.py")
rules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rules)


@pytest.mark.parametrize("given,stored", [
    ("192.0.2.5", "192.0.2.5"), ("192.0.2.5/32", "192.0.2.5"), ("192.0.2.5/255.255.255.255", "192.0.2.5"),
    ("192.0.2.77/24", "192.0.2.0/24"), ("192.0.2.0/24", "192.0.2.0/24"),
    ("192.0.2.9/255.255.255.0", "192.0.2.0/24"),
    ("2001:db8::1/128", "2001:db8::1"), ("2001:0db8::0001", "2001:db8::1"), ("2001:db8::/32", "2001:db8::/32"),
    ("any", "any"), (" 192.0.2.5 ", "192.0.2.5"), ("not-an-address", "not-an-address"),
])
def test_an_address_is_spelled_as_ufw_stores_it(given, stored):
    assert rules.ufw_address(given) == stored


@pytest.mark.parametrize("spec", [
    "allow from 192.0.2.0/24 to any port 22 proto tcp",
    "allow from 192.0.2.0/24 to any port 22",             # proto any: ufw prints no proto
    "allow from 192.0.2.0/24 to any port 22,2222 proto tcp",
    "allow from 192.0.2.0/24 to any port 2222,22 proto tcp",
    "allow from 192.0.2.0/24 to any port 20:30 proto tcp",
    "allow from 192.0.2.0/24",                            # no port clause: every port
    "allow from 192.0.2.0/24 proto tcp",
    "allow from 192.0.2.0/24 port 5000 to any port 22 proto tcp",  # a source port narrows nothing
    "allow 22/tcp", "allow 22", "allow 22,80/tcp", "allow 1:1024/tcp",
    "limit 22/tcp", "limit from 192.0.2.0/24 to any port 22 proto tcp",
    "allow in on eth0 to any port 22 proto tcp", "allow in to any port 22 proto tcp",
    "allow log from 192.0.2.0/24 to any port 22 proto tcp",
    "route allow from 192.0.2.0/24 to any port 22 proto tcp", "route allow from 192.0.2.0/24",
    "route allow out on eth1 to 192.0.2.164 port 22 proto tcp",
    "route allow in on eth0 out on eth1 to 192.0.2.164 port 22 proto tcp",
    "allow OpenSSH", "allow from 192.0.2.0/24 to any app OpenSSH",  # unreadable: counts
])
def test_a_rule_that_admits_ssh_is_recognised(spec):
    assert rules.ufw_rule_admits_port(spec, 22) is True


@pytest.mark.parametrize("spec", [
    "allow from 192.0.2.0/24 to any port 22 proto udp",
    "allow 22/udp",
    "allow from 192.0.2.0/24 to any port 2222 proto tcp",
    "allow from 192.0.2.0/24 to any port 23:30 proto tcp",
    "allow from 192.0.2.0/24 to any port 80,443 proto tcp",
    "allow from 192.0.2.0/24 proto udp",
    "allow in on podman1 to any port 53 proto udp",
    "deny from 192.0.2.0/24 to any port 22 proto tcp",
    "deny out to 192.0.2.164 port 22 proto tcp",
    "allow out to 192.0.2.164 port 22 proto tcp",
    "route deny from 192.0.2.0/24 to any port 22 proto tcp",
])
def test_a_rule_that_does_not_admit_ssh_is_not(spec):
    assert rules.ufw_rule_admits_port(spec, 22) is False
