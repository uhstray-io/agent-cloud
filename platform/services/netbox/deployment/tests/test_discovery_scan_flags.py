"""A network_discovery scope never pairs fast_mode with an explicit port list.

fast_mode makes the orb-agent run nmap with -F; nmap refuses -F together with -p
("You cannot use -F (fast scan) with -p ... QUITTING!") and exits 1, so the
subnet_scan policy errored on every scheduled run and NetBox stayed empty.
-F mapping: netboxlabs/orb-agent docs/backends/network_discovery.md.
"""

from pathlib import Path

import jinja2
import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1]
TEMPLATE = DEPLOY / "templates" / "agent.yaml.j2"
EXAMPLES = [
    DEPLOY / "discovery" / "agent.yaml.example",
    DEPLOY / "discovery" / "agent.yaml.rootless.example",
]


def _render(workers_enabled):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = bool  # Ansible's filter; plain bool is enough for a literal
    text = TEMPLATE.read_text()
    return yaml.safe_load(env.from_string(text).render(
        _orb_approle={"role_id": "r", "secret_id": "s"},
        openbao_addr="http://bao.invalid:8200",
        discovery_target_subnet="192.0.2.0/24",
        discovery_workers_enabled=workers_enabled,
    ))


def _scopes(doc):
    """Yield (backend/policy, scope) for every policy of every backend."""
    for backend, policies in doc["orb"]["policies"].items():
        for name, policy in (policies or {}).items():
            yield f"{backend}/{name}", (policy or {}).get("scope") or {}


def _offenders(doc):
    return [n for n, s in _scopes(doc) if s.get("fast_mode") and s.get("ports")]


@pytest.mark.parametrize("workers", [True, False])
def test_rendered_template_has_no_fast_mode_with_ports(workers):
    doc = _render(workers)
    names = [n for n, _ in _scopes(doc)]
    assert "network_discovery/subnet_scan" in names  # the render really reached the scan
    assert _offenders(doc) == []


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_have_no_fast_mode_with_ports(path):
    doc = yaml.safe_load(path.read_text())
    assert [n for n, _ in _scopes(doc)]
    assert _offenders(doc) == []
