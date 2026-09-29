"""apply-firewall.yml's firewall_allow_groups: one rule per group member, from its ansible_host.

The play's own vars block and its refusal task are lifted and run against a stub
inventory, so the test exercises the real Jinja rather than a copy of it. Runs through
harness_sandbox so the ansible run cannot write outside the test's temp dir.
"""

import json
import subprocess
from pathlib import Path

import harness_sandbox
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/apply-firewall.yml"
REFUSAL = "Refuse a group rule that names an unknown group or a member without an IPv4 address"


def _render(tmp_path: Path, host_vars: dict, groups: dict) -> subprocess.CompletedProcess:
    play, = yaml.safe_load(PLAYBOOK.read_text())
    refusal, = (t for t in play["tasks"] if t.get("name") == REFUSAL)
    inventory = {"all": {"hosts": {"target": {"ansible_connection": "local", **host_vars}},
                         "children": {g: {"hosts": {h: {"ansible_host": a} for h, a in m.items()}}
                                      for g, m in groups.items()}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inventory))
    out = tmp_path / "rules.json"
    lifted = [{"hosts": "target", "gather_facts": False, "vars": play["vars"],
               "tasks": [refusal, {"name": "dump", "ansible.builtin.copy": {
                   "content": "{{ _rules | to_json }}", "dest": str(out), "mode": "0600"}}]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(lifted))
    return harness_sandbox.run(
        ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml")],
        # the lifted play sits outside platform/playbooks; the repository ansible.cfg (cwd)
        # supplies its filters
        tmp_path, cwd=REPO, env=harness_sandbox.env_for(tmp_path))


def test_a_group_rule_expands_to_one_rule_per_member(tmp_path):
    r = _render(tmp_path, {
        "firewall_allow_rules": [{"port": 53, "proto": "udp", "from": "192.0.2.200", "comment": "dgx"}],
        "firewall_allow_groups": [{"port": 53, "proto": "udp", "group": "members", "comment": "LAN DNS"}],
    }, {"members": {"a": "192.0.2.10", "b": "192.0.2.11"}})
    assert r.returncode == 0, r.stdout + r.stderr
    rules = json.loads((tmp_path / "rules.json").read_text())
    assert rules == [
        {"port": 53, "proto": "udp", "from": "192.0.2.200", "comment": "dgx"},
        {"port": 53, "proto": "udp", "from": "192.0.2.10", "comment": "LAN DNS: a"},
        {"port": 53, "proto": "udp", "from": "192.0.2.11", "comment": "LAN DNS: b"},
    ]


def test_no_group_rules_leaves_the_static_rules_alone(tmp_path):
    r = _render(tmp_path, {"firewall_allow_rules": [{"port": 22, "from": "192.0.2.1"}]}, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads((tmp_path / "rules.json").read_text()) == [{"port": 22, "from": "192.0.2.1"}]


@pytest.mark.parametrize("groups,rule_group", [
    ({"members": {"a": "192.0.2.10"}}, "missing"),        # unknown group
    ({"members": {"a": "dns.example.test"}}, "members"),  # member without an IPv4 address
    ({"members": {"a": "999.1.1.1"}}, "members"),         # four octets, not an IPv4 address
])
def test_an_unresolvable_group_rule_is_refused(tmp_path, groups, rule_group):
    r = _render(tmp_path, {"firewall_allow_groups": [{"port": 53, "group": rule_group}]}, groups)
    assert r.returncode != 0
    assert "refusing rather than dropping a source" in r.stdout
    assert not (tmp_path / "rules.json").exists()
