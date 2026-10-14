"""The MCP surface in the rendered gateway config (agentgateway-mcp tasks 3.1, 3.2, 3.6-3.9, 4.2-4.5).

The real config.yaml.j2 and env.j2 are rendered on localhost through harness_sandbox, the way
manage-secrets renders them, and the result is parsed as YAML: each test reads the property it is
about off the rendered document, never a window of text. Every key here is synthetic.

What is pinned, and the spike finding (design.md, "Findings") each one comes from:
- flag false renders no `mcp` block, no loopback gateway and the unchanged `llm.gateways` (G4);
- flag true serves MCP on `default` only, stateless, behind a strict key policy built by the same
  macro as the llm one, without the legacy shared key, the internal identity, or any budget (1.1,
  1.4, 1.6, G2);
- the self-call goes to a loopback gateway attached to `llm` only and restricted to the container's
  own address (1.5, task 3.7), through an `openapi` target whose host is that listener (3.8);
- the internal key's value is in the 0600 env file and nowhere else (4.3, 4.4), including under the
  local-dev plaintext-keys flag.
"""

import hashlib
import json
import sys

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = playbook_yaml.REPO
DEPLOY_DIR = REPO / "platform/services/agentgateway/deployment"
CONFIG = DEPLOY_DIR / "templates/config.yaml.j2"
ENV = DEPLOY_DIR / "templates/env.j2"
SCHEMA_FILE = "agentgateway-v1.json"
SCHEMA_PATH = DEPLOY_DIR / "mcp-schemas" / SCHEMA_FILE
SCHEMA_TEXT = json.dumps(json.loads(SCHEMA_PATH.read_text()), separators=(",", ":"))

KEYS = {
    "workstation": "ws-key-AAAA-1111",
    "pi": "pi-key-BBBB-2222",
    "mcp-self": "SELF-key-CCCC-3333",
    "dev-local": "dev-key-DDDD-4444",
    "alice": "alice-key-EEEE-5555",
    "bob": "bob-key-FFFF-6666",
}
LEGACY_FP = "f" * 64
TARGETS = [{"name": "gateway", "schema": SCHEMA_FILE}]

# Service identities (agw_clients) and personal admin identities (derived from platform_admins). /mcp admits
# the admins only; /v1 admits both.
ADMINS = [{"key": "alice", "name": "alice"}, {"key": "bob", "name": "bob"}]
BASE = {
    "agw_clients": ["workstation", "pi", "mcp-self"],
    "_agw_admin_identities": ADMINS,
    "agw_identities": ["workstation", "pi", "mcp-self", "alice", "bob"],
    "agw_client_policies": {
        "pi": {"allowed_models": ["m"], "tokens_per_hour": 5000},
        "mcp-self": {"tokens_per_hour": 100},
    },
    "agw_models": [{"name": "m"}],
    "agw_upstream_base_url": "http://upstream.invalid:8000/v1",
    "_legacy_shared_active": True,
    "_legacy_fp_recorded": LEGACY_FP,
}
ON = {**BASE, "agw_mcp_enabled": True, "agw_mcp_openapi_targets": TARGETS}


def _secrets(hv: dict) -> dict:
    out = {
        "vllm_api_key": "v",
        "agw_db_password": "p",
        "agw_oidc_cookie_seed": "s",
        "agentgateway_oidc_client_secret": "c",
    }
    for name in hv["agw_identities"]:
        out["client_" + name] = KEYS[name]
    return out


@pytest.fixture(scope="module")
def render(tmp_path_factory):
    """render(**host_vars) -> (config dict, config text, env text); one ansible run per distinct input."""
    cache: dict[str, tuple] = {}

    def go(**overrides):
        hv = {**BASE, **overrides}
        hv["secrets"] = _secrets(hv)
        if hv.get("agw_mcp_enabled"):
            hv["_agw_mcp_schemas"] = {SCHEMA_FILE: SCHEMA_TEXT}
        key = json.dumps(hv, sort_keys=True)
        if key not in cache:
            tmp = tmp_path_factory.mktemp("render")
            (tmp / "play.yml").write_text(
                yaml.safe_dump(
                    [
                        {
                            "hosts": "localhost",
                            "gather_facts": False,
                            "vars": hv,
                            "tasks": [
                                {
                                    "ansible.builtin.template": {
                                        "src": str(CONFIG),
                                        "dest": str(tmp / "config.yaml"),
                                        "mode": "0644",
                                    }
                                },
                                {
                                    "ansible.builtin.template": {
                                        "src": str(ENV),
                                        "dest": str(tmp / "env"),
                                        "mode": "0600",
                                    }
                                },
                            ],
                        }
                    ]
                )
            )
            r = harness_sandbox.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local", str(tmp / "play.yml")],
                tmp,
                cwd=REPO,
                env={**harness_sandbox.env_for(tmp), "ANSIBLE_PYTHON_INTERPRETER": sys.executable},
            )
            assert r.returncode == 0, r.stdout + r.stderr
            text = (tmp / "config.yaml").read_text()
            cache[key] = (yaml.safe_load(text), text, (tmp / "env").read_text())
        return cache[key]

    return go


def _names(policy: dict) -> list[str]:
    return [k["metadata"]["name"] for k in policy["keys"]]


def _hash(name: str) -> str:
    return "sha256:" + hashlib.sha256(KEYS[name].encode()).hexdigest()


# ── flag false: nothing changes (G4, task 3.2, 3.9) ───────────────────────────


def test_flag_false_renders_no_mcp_block(render):
    cfg, _, _ = render()
    assert "mcp" not in cfg


def test_flag_false_renders_no_loopback_gateway_and_the_unchanged_llm_gateways(render):
    cfg, _, _ = render()
    assert sorted(cfg["gateways"]) == ["default", "ui"]
    assert cfg["llm"]["gateways"] == ["default", "ui"]
    cfg, _, _ = render(agw_ui_enabled=False)
    assert list(cfg["gateways"]) == ["default"]
    assert cfg["llm"]["gateways"] == ["default"]


def test_flag_false_renders_no_self_key_in_the_env_file(render):
    _, _, env = render()
    assert "AGW_MCP_SELF_KEY" not in env


def test_an_explicit_false_renders_what_an_unset_flag_does(render):
    assert render(agw_mcp_enabled=False)[1:] == render()[1:]


# ── flag true: the endpoint and its key policy (tasks 3.1, 3.2, 3.6) ──────────


def test_flag_true_serves_mcp_on_the_default_gateway_only_and_stateless(render):
    cfg, _, _ = render(**ON)
    assert cfg["mcp"]["gateways"] == ["default"]
    assert cfg["mcp"]["statefulMode"] == "stateless"
    assert cfg["mcp"]["prefixMode"] == "conditional"
    assert "dnsRebindingProtection" not in cfg["mcp"]


def test_the_mcp_policy_is_a_strict_key_policy_and_nothing_else(render):
    cfg, _, _ = render(**ON)
    policies = cfg["mcp"]["policies"]
    assert list(policies) == ["apiKey"]
    assert policies["apiKey"]["mode"] == "strict"
    assert policies["apiKey"]["location"] == cfg["llm"]["policies"]["apiKey"]["location"]


def test_the_mcp_key_list_is_the_admin_identities_only(render):
    """/mcp admits the admin-derived personal identities and nothing else (the confused-deputy fix)."""
    cfg, _, _ = render(**ON)
    llm, mcp = cfg["llm"]["policies"]["apiKey"], cfg["mcp"]["policies"]["apiKey"]
    assert _names(llm) == ["workstation", "pi", "mcp-self", "alice", "bob", "legacy-shared"]
    assert _names(mcp) == ["alice", "bob"]
    by_name = {k["metadata"]["name"]: k for k in llm["keys"]}
    for entry in mcp["keys"]:
        assert entry["keyHash"] == by_name[entry["metadata"]["name"]]["keyHash"] == _hash(entry["metadata"]["name"])


def test_a_service_client_is_enrolled_on_v1_and_absent_from_mcp(render):
    """A service client on /mcp would reach the chat tool, whose self-call runs as mcp-self, and so escape its own
    allowedModels and hourly budget."""
    cfg, _, _ = render(**ON)
    llm, mcp = _names(cfg["llm"]["policies"]["apiKey"]), _names(cfg["mcp"]["policies"]["apiKey"])
    for service in ("workstation", "pi"):
        assert service in llm and service not in mcp


def test_an_admin_that_is_also_a_service_identity_is_admitted_on_mcp(render):
    admins = [*ADMINS, {"key": "w", "name": "workstation"}]
    cfg, _, _ = render(**{**ON, "_agw_admin_identities": admins})
    assert _names(cfg["mcp"]["policies"]["apiKey"]) == ["alice", "bob", "workstation"]


def test_an_admin_derived_internal_name_is_never_admitted_on_mcp(render):
    cfg, _, _ = render(**{**ON, "_agw_admin_identities": [*ADMINS, {"key": "s", "name": "mcp-self"}]})
    assert _names(cfg["mcp"]["policies"]["apiKey"]) == ["alice", "bob"]


def test_the_legacy_shared_key_is_not_admitted_on_mcp(render):
    cfg, text, _ = render(**ON)
    assert "legacy-shared" in _names(cfg["llm"]["policies"]["apiKey"])
    assert "legacy-shared" not in _names(cfg["mcp"]["policies"]["apiKey"])
    assert text.count(f"keyHash: sha256:{LEGACY_FP}") == 1


def test_the_internal_identity_is_not_admitted_on_mcp_but_is_on_v1(render):
    cfg, _, _ = render(**ON)
    assert "mcp-self" in _names(cfg["llm"]["policies"]["apiKey"])
    assert "mcp-self" not in _names(cfg["mcp"]["policies"]["apiKey"])


def test_no_budget_or_model_limit_is_rendered_under_the_mcp_keys(render):
    """A budget under mcp.policies.apiKey is accepted and never charged (spike 1.4): a no-op that reads as a control."""
    cfg, _, _ = render(**ON)
    for entry in cfg["mcp"]["policies"]["apiKey"]["keys"]:
        assert set(entry) == {"keyHash", "metadata"}, entry
    llm = {k["metadata"]["name"]: k for k in cfg["llm"]["policies"]["apiKey"]["keys"]}
    assert llm["pi"]["allowedModels"] == ["m"] and llm["pi"]["budgets"][0]["limit"]["amount"] == 5000
    assert llm["mcp-self"]["budgets"][0]["limit"]["amount"] == 100


def test_the_llm_policy_is_the_same_with_the_flag_on_or_off(render):
    """The shared macro gives llm the same entries whatever the flag does to the rest of the file."""
    off, on = render()[0], render(**ON)[0]
    assert on["llm"]["policies"] == off["llm"]["policies"]
    assert on["llm"]["models"] == off["llm"]["models"]


# ── the loopback gateway (tasks 3.7, 3.9) ─────────────────────────────────────


def test_the_loopback_gateway_admits_only_the_containers_own_address(render):
    cfg, _, _ = render(**ON)
    assert cfg["gateways"]["loopback"] == {
        "port": 4002,
        "authorization": {"rules": [{"require": 'source.address == "127.0.0.1"'}]},
    }


def test_the_loopback_gateway_is_attached_to_llm_alone(render):
    cfg, _, _ = render(**ON)
    assert cfg["llm"]["gateways"] == ["default", "ui", "loopback"]
    assert cfg["mcp"]["gateways"] == ["default"]
    assert cfg["ui"]["gateways"] == "ui"
    cfg, _, _ = render(**{**ON, "agw_ui_enabled": False})
    assert cfg["llm"]["gateways"] == ["default", "loopback"]
    assert list(cfg["gateways"]) == ["default", "loopback"]


def test_the_loopback_port_is_the_inventory_value(render):
    cfg, _, _ = render(**{**ON, "agw_loopback_port": 4010})
    assert cfg["gateways"]["loopback"]["port"] == 4010
    assert cfg["mcp"]["targets"][0]["openapi"]["host"] == "127.0.0.1:4010"


# ── the target: OpenAPI over the loopback listener only (tasks 3.8, 4.1, 4.3) ──


def test_every_target_is_an_openapi_target_on_the_loopback_listener(render):
    cfg, _, _ = render(**ON)
    (target,) = cfg["mcp"]["targets"]
    assert set(target) == {"name", "openapi", "policies"}  # no mcp, sse or stdio kind
    assert set(target["openapi"]) == {"schema", "host"}
    assert target["name"] == "gateway"
    assert target["openapi"]["host"] == "127.0.0.1:4002"
    assert set(target["policies"]) == {"backendAuth"}  # no client certificate, no backend TLS


def test_the_target_schema_is_the_shipped_file_embedded(render):
    cfg, _, _ = render(**ON)
    embedded = cfg["mcp"]["targets"][0]["openapi"]["schema"]
    assert isinstance(embedded, str) and embedded == SCHEMA_TEXT  # a string, not a {file: path} mapping


def test_several_declared_targets_render_one_target_each_in_order(render):
    two = [*TARGETS, {"name": "second", "schema": SCHEMA_FILE}]
    cfg, _, _ = render(**{**ON, "agw_mcp_openapi_targets": two})
    assert [t["name"] for t in cfg["mcp"]["targets"]] == ["gateway", "second"]
    assert {t["openapi"]["host"] for t in cfg["mcp"]["targets"]} == {"127.0.0.1:4002"}


def test_the_target_authenticates_with_the_self_key_by_environment_reference(render):
    cfg, _, _ = render(**ON)
    auth = cfg["mcp"]["targets"][0]["policies"]["backendAuth"]
    assert auth == {
        "key": {"value": "$AGW_MCP_SELF_KEY", "location": {"header": {"name": "authorization", "prefix": "Bearer "}}}
    }


# ── the internal key never leaves the env file (tasks 4.3-4.5) ────────────────


@pytest.mark.parametrize(
    "extra", [{}, {"local_mode": True, "agw_plaintext_keys": True}], ids=["hashed", "local-plaintext"]
)
def test_the_internal_key_value_is_in_the_env_file_and_nowhere_else(render, extra):
    _, text, env = render(**{**ON, **extra})
    assert KEYS["mcp-self"] not in text
    assert f"AGW_MCP_SELF_KEY={KEYS['mcp-self']}\n" in env
    assert env.count(KEYS["mcp-self"]) == 1


def test_under_local_plaintext_keys_clients_are_raw_but_the_internal_identity_stays_a_hash(render):
    cfg, text, _ = render(**{**ON, "local_mode": True, "agw_plaintext_keys": True})
    entries = {k["metadata"]["name"]: k for k in cfg["llm"]["policies"]["apiKey"]["keys"]}
    assert entries["workstation"]["key"] == KEYS["workstation"]  # the flag took effect
    assert (
        entries["mcp-self"] == {**entries["mcp-self"], "keyHash": _hash("mcp-self")}
        and "key" not in entries["mcp-self"]
    )
    assert KEYS["mcp-self"] not in text
    # and the MCP list never carries a raw key either way it was rendered
    assert all("key" not in e or e["metadata"]["name"] != "mcp-self" for e in cfg["mcp"]["policies"]["apiKey"]["keys"])


def test_the_templates_name_the_internal_key_only_by_its_environment_variable():
    config = CONFIG.read_text()
    live = [ln.strip() for ln in config.splitlines() if "AGW_MCP_SELF_KEY" in ln and not ln.lstrip().startswith("#")]
    assert live == ["value: $AGW_MCP_SELF_KEY"]
    assert "client_mcp-self" not in config
    env = [ln for ln in ENV.read_text().splitlines() if "AGW_MCP_SELF_KEY" in ln and not ln.startswith("#")]
    assert env == ["AGW_MCP_SELF_KEY={{ secrets['client_mcp-self'] }}"]
    for path in (*DEPLOY_DIR.glob("compose*.yml"), SCHEMA_PATH):
        assert "SELF_KEY" not in path.read_text() and "client_mcp-self" not in path.read_text(), path


# ── the shipped schema (task 4.1, 4.5) ────────────────────────────────────────


def test_the_shipped_schema_lists_exactly_chat_completions_and_model_listing():
    doc = json.loads(SCHEMA_PATH.read_text())
    ops = {(path, verb): spec["operationId"] for path, item in doc["paths"].items() for verb, spec in item.items()}
    assert ops == {("/v1/models", "get"): "list_models", ("/v1/chat/completions", "post"): "chat_completions"}
    assert "servers" not in doc  # the host is the deploy's loopback listener, never the file's


def test_no_target_or_schema_addresses_the_admin_port_or_the_config_dump(render):
    cfg, text, _ = render(**ON)
    schema = SCHEMA_PATH.read_text()
    for needle in ("config_dump", "15000", "/admin", "/ui"):
        assert needle not in schema, needle
    hosts = [t["openapi"]["host"] for t in cfg["mcp"]["targets"]]
    assert all(h.endswith(":4002") for h in hosts) and not any("15000" in h for h in hosts)
    assert cfg["config"]["adminAddr"] == "127.0.0.1:15000"
