"""Per-tier hourly token budgets in the rendered gateway config.

The real vars file and config.yaml.j2 run on localhost through harness_sandbox, and the rendered
document is parsed: each test reads the `hourly-tokens` amount per identity. What is pinned:

- an identity derived from platform_admins gets the `admin` tier figure;
- another identity joins a tier with agw_client_policies.<name>.tier;
- an identity's own tokens_per_hour beats its tier, and an untiered identity keeps agw_rate_tokens_per_hour;
- with no tier map declared, every budget is what it was before;
- a tier with no figure, or a figure that is not a positive whole number, is refused by name.
"""

import json
import sys
from pathlib import Path

import harness_sandbox
import playbook_yaml
import yaml

REPO = playbook_yaml.REPO
DECL = REPO / "platform/playbooks/vars/secret-declarations/agentgateway.yml"
CONFIG = REPO / "platform/services/agentgateway/deployment/templates/config.yaml.j2"

TIERS = {"admin": 1000000000, "developer": 500000000, "business": 250000000}
IDENTITIES = ["svc", "dev1", "biz1", "capped", "alice"]
BASE = {
    "agw_clients": ["svc", "dev1", "biz1", "capped"],
    "agw_admin_identities_enabled": True,
    "platform_admins": [{"key": "alice", "username": "alice", "name": "Alice", "email": "alice@example.invalid"}],
    "agw_client_policies": {
        "dev1": {"tier": "developer"},
        "biz1": {"tier": "business"},
        "capped": {"tier": "developer", "tokens_per_hour": 7},
    },
    "agw_models": [{"name": "m"}],
    "agw_upstream_base_url": "http://upstream.invalid:8000/v1",
    "agw_rate_tokens_per_hour": 10000000,
    "secrets": {
        "vllm_api_key": "v",
        "agw_db_password": "p",
        "agw_oidc_cookie_seed": "s",
        "agentgateway_oidc_client_secret": "c",
        **{"client_" + n: "key-" + n for n in IDENTITIES},
    },
}


def _play(tmp: Path, host_vars: dict, tasks: list):
    inv = {"all": {"hosts": {"gw": {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                                    **host_vars}}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp / "play.yml").write_text(yaml.safe_dump(
        [{"hosts": "all", "gather_facts": False, "vars_files": [str(DECL)], "tasks": tasks}]))
    return harness_sandbox.run(
        ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml")],
        tmp,
        cwd=REPO,
        env={**harness_sandbox.env_for(tmp), "ANSIBLE_STDOUT_CALLBACK": "default"},
    )


def _budgets(tmp: Path, host_vars: dict) -> dict:
    out = tmp / "config.yaml"
    r = _play(tmp, host_vars, [{"ansible.builtin.template": {"src": str(CONFIG), "dest": str(out), "mode": "0644"}}])
    assert r.returncode == 0, r.stdout + r.stderr
    doc = yaml.safe_load(out.read_text())
    keys = doc["llm"]["policies"]["apiKey"]["keys"]
    return {k["metadata"]["name"]: k["budgets"][0]["limit"]["amount"] for k in keys}


def _problems(tmp: Path, host_vars: dict) -> list[str]:
    out = tmp / "problems.json"
    r = _play(tmp, host_vars, [{"ansible.builtin.copy": {"content": "{{ _agw_tier_problems | to_json }}",
                                                         "dest": str(out)}}])
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(out.read_text())


def test_each_identity_gets_its_tier_figure(tmp_path):
    assert _budgets(tmp_path, {**BASE, "agw_tier_tokens_per_hour": TIERS}) == {
        "svc": 10000000,  # no tier: the global default
        "dev1": 500000000,
        "biz1": 250000000,
        "capped": 7,  # its own figure beats its tier
        "alice": 1000000000,  # derived from platform_admins: tier admin
    }
    assert _problems(tmp_path, {**BASE, "agw_tier_tokens_per_hour": TIERS}) == []


def test_no_tier_map_leaves_every_budget_unchanged(tmp_path):
    hv = {**BASE, "agw_client_policies": {"capped": {"tokens_per_hour": 7}}}
    assert _budgets(tmp_path, hv) == {"svc": 10000000, "dev1": 10000000, "biz1": 10000000, "capped": 7,
                                      "alice": 10000000}


def test_an_unknown_or_malformed_tier_is_refused_by_name(tmp_path):
    hv = {**BASE, "agw_tier_tokens_per_hour": {"admin": 1000000000, "developer": "lots"}}
    problems = _problems(tmp_path, hv)
    assert any("agw_tier_tokens_per_hour.developer must be a positive whole number" in p for p in problems)
    assert any("agw_client_policies.biz1.tier names business" in p for p in problems)
    assert not any("dev1" in p for p in problems)
