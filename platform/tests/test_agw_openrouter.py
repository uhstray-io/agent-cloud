"""OpenRouter models on the agentgateway (change: agentgateway-openrouter).

An `agw_models` entry with `provider: openrouter` is served by the gateway's native OpenRouter
provider (a custom-provider preset in v1.5.0: crates/llm/src/custom.rs:52-67,81,144-153). It is
paid per token on the operator's account, so it is opt-in twice: the model is listed by name with
its OpenRouter id, and `agw_openrouter_identities` names who may call it; every other identity is
denied it through `allowedModels`. The real vars file, the real guard and verify tasks of
deploy-agentgateway.yml and the real config and env templates run on localhost through
harness_sandbox, so nothing is written outside the test's directory.

What each test pins: an inventory without the provider renders as before; the OpenRouter entry's
shape and that the key is an env reference only (never in config.yaml); which identities may use
the model (including the `legacy-shared` identity and the intersection with an existing
allow-list); every refusal; that the secret is declared and required exactly when a model needs it;
that the verify never spends OpenRouter tokens unless asked; and, when the pinned image is present,
that the render passes `--validate-only` and puts the right model, key and stream options on the
wire.
"""

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from ansible_python import ansible_python
from fake_http import DrainingHandler
from test_agw_admin_identities import CONFIG, DEPLOY, PHASE1, PHASE3, SECRETS, _ansible, _task, admin
from test_agw_mcp_render import SCHEMA_FILE, SCHEMA_TEXT, TARGETS
from test_agw_stream_usage import IMAGE, _free_port, _image_present

ENV_TEMPLATE = CONFIG.with_name("env.j2")
CONFORMANCE = DEPLOY.with_name("run-agw-conformance.yml")
ISOLATION = DEPLOY.with_name("probe-agentgateway-isolation.yml")
OR_KEY = "or-synthetic-key-91c4"
OR_EXPR = (
    "has(llmRequest.messages) && has(llmRequest.stream) && llmRequest.stream == true "
    "? (has(llmRequest.stream_options) && type(llmRequest.stream_options) == map "
    '? llmRequest.stream_options : {}).merge({"include_usage": true}) '
    ": llmRequest.stream_options"
)
VLLM = {"name": "gpt-oss-120b"}
OR_MINI = {"name": "or-mini", "provider": "openrouter", "upstream_model": "openai/gpt-4o-mini"}
OR_SONNET = {"name": "or-sonnet", "provider": "openrouter", "upstream_model": "anthropic/claude-sonnet-4"}
STORE = {**SECRETS, "openrouter_api_key": OR_KEY, "client_ws": "stored-key-for-ws", "client_pi": "stored-key-for-pi"}

REFUSE_TASK = "Refuse a malformed OpenRouter declaration"
PICK_TASK = "Keyed probes: pick the verifying identity, its models and the gateway URL"
NON_OR_TASK = "Keyed probes: the identity may use a model that is not an OpenRouter one"
PICK_OR_TASK = "Keyed probes: pick the OpenRouter model for the opt-in completion"
REQUIRE_OR_TASK = "Require an OpenRouter model for the verifying identity when the opt-in is set"
OPT_IN_TASKS = [
    PICK_OR_TASK,
    REQUIRE_OR_TASK,
    "An enrolled identity completes one OpenRouter request (opt-in)",
    "Keyed probes: keep the OpenRouter completion's response",
    "Require a completion OpenRouter produced",
]


def _template(tmp: Path, src: Path, dest: str, **hv) -> str:
    hv = {
        "agw_models": [VLLM],
        "agw_upstream_base_url": "http://u.invalid/v1",
        "agw_ui_enabled": False,
        "secrets": STORE,
        **hv,
    }
    task = {"ansible.builtin.template": {"src": str(src), "dest": str(tmp / dest), "mode": "0644"}}
    r = _ansible(tmp, hv, [task])
    assert r.returncode == 0, r.stdout + r.stderr
    return (tmp / dest).read_text()


def render(tmp: Path, **hv) -> dict:
    return yaml.safe_load(_template(tmp, CONFIG, "config.yaml", **hv))


def render_text(tmp: Path, **hv) -> str:
    return _template(tmp, CONFIG, "config.yaml", **hv)


def keys(cfg: dict) -> dict:
    """identity -> its apiKey entry."""
    return {k["metadata"]["name"]: k for k in cfg["llm"]["policies"]["apiKey"]["keys"]}


def evaluate(tmp: Path, host_vars: dict) -> dict:
    out = tmp / "eval.json"
    expr = (
        "{{ {'problems': _agw_openrouter_problems, 'models': _agw_openrouter_models, "
        "'defs': _secret_definitions, 'required': _required_existing_secrets, "
        "'ids': _agw_openrouter_identities} | to_json }}"
    )
    r = _ansible(tmp, host_vars, [{"ansible.builtin.copy": {"content": expr, "dest": str(out)}}])
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(out.read_text())


GOOD = {"agw_models": [VLLM, OR_MINI], "agw_clients": ["skynet", "ws"], "agw_openrouter_identities": ["skynet"]}


# -- Without the provider the deployment is what it was --------------------------------------


def test_no_openrouter_model_renders_no_trace_of_it(tmp_path):
    text = render_text(tmp_path, agw_clients=["ws"])
    (tmp_path / "e").mkdir()
    env = _template(tmp_path / "e", ENV_TEMPLATE, ".env", agw_clients=["ws"])
    assert "openrouter" not in text.lower()
    assert "OPENROUTER" not in env
    assert "allowedModels" not in text  # no policy declared, nothing is restricted
    model = yaml.safe_load(text)["llm"]["models"][0]
    assert model["provider"] == {"custom": {"formats": [{"type": "completions"}, {"type": "responses"}]}}


def test_a_declared_identities_list_changes_nothing_while_no_model_is_openrouter(tmp_path):
    base = render_text(tmp_path, agw_clients=["ws"], agw_client_policies={"ws": {"allowed_models": ["gpt-oss-120b"]}})
    d = tmp_path / "b"
    d.mkdir()
    again = render_text(
        d,
        agw_clients=["ws"],
        agw_client_policies={"ws": {"allowed_models": ["gpt-oss-120b"]}},
        agw_openrouter_identities=["ws"],
    )
    assert again == base


def test_without_an_openrouter_model_the_secret_declaration_is_unchanged(tmp_path):
    got = evaluate(tmp_path, {"agw_models": [VLLM], "agw_clients": ["ws"]})
    assert got["problems"] == [] and got["models"] == []
    assert [d["name"] for d in got["defs"]] == ["vllm_api_key", "agw_db_password", "agw_oidc_cookie_seed", "client_ws"]
    assert got["required"] == ["vllm_api_key"]


# -- The OpenRouter entry ----------------------------------------------------------------------


def test_an_openrouter_model_renders_the_native_provider_with_the_key_as_an_env_reference(tmp_path):
    text = render_text(tmp_path, **GOOD)
    models = {m["name"]: m for m in yaml.safe_load(text)["llm"]["models"]}
    assert models["or-mini"] == {
        "name": "or-mini",
        "provider": "openrouter",
        "params": {"model": "openai/gpt-4o-mini", "apiKey": "$OPENROUTER_API_KEY"},
        "transformation": {"stream_options": OR_EXPR},
    }
    # The vLLM entry beside it is untouched: still the custom provider with the vLLM key.
    assert models["gpt-oss-120b"]["params"]["apiKey"] == "$VLLM_API_KEY"
    assert "custom" in models["gpt-oss-120b"]["provider"]
    # No wildcard model is ever rendered, and the key value is nowhere in the config.
    assert not any("*" in m["name"] for m in models.values())
    assert OR_KEY not in text


def test_the_openrouter_name_maps_to_the_openrouter_id_not_to_the_vllm_base_url(tmp_path):
    models = {m["name"]: m for m in render(tmp_path, **GOOD)["llm"]["models"]}
    assert "baseUrl" not in models["or-mini"]["params"]  # the preset supplies https://openrouter.ai/api/v1


def test_the_env_carries_the_openrouter_key_only_when_a_model_needs_it(tmp_path):
    on = tmp_path / "on"
    off = tmp_path / "off"
    on.mkdir()
    off.mkdir()
    assert f"\nOPENROUTER_API_KEY={OR_KEY}\n" in _template(on, ENV_TEMPLATE, ".env", **GOOD)
    assert "OPENROUTER_API_KEY=" not in _template(off, ENV_TEMPLATE, ".env", agw_models=[VLLM], agw_clients=["ws"])


# -- Who may call it -----------------------------------------------------------------------------


def test_only_the_named_identities_may_call_openrouter_models(tmp_path):
    got = keys(render(tmp_path, **GOOD))
    assert "allowedModels" not in got["skynet"]  # named: every declared model, as before
    assert got["ws"]["allowedModels"] == ["gpt-oss-120b"]  # not named: denied the OpenRouter one


def test_an_existing_allow_list_is_narrowed_by_the_exclusion_not_replaced(tmp_path):
    hv = {
        **GOOD,
        "agw_models": [VLLM, {"name": "m2"}, OR_MINI, OR_SONNET],
        "agw_clients": ["skynet", "ws", "pi"],
        "agw_client_policies": {
            "ws": {"allowed_models": ["m2", "or-mini", "gpt-oss-120b"]},  # names an OpenRouter model
            "skynet": {"allowed_models": ["or-sonnet", "m2"]},  # named identity keeps its own list
        },
    }
    got = keys(render(tmp_path, **hv))
    assert got["ws"]["allowedModels"] == ["m2", "gpt-oss-120b"]  # order kept, OpenRouter name removed
    assert got["skynet"]["allowedModels"] == ["or-sonnet", "m2"]
    assert got["pi"]["allowedModels"] == ["gpt-oss-120b", "m2"]  # no policy: every non-OpenRouter model


def test_an_undeclared_identities_list_denies_everyone_rather_than_allowing_everyone(tmp_path):
    hv = {k: v for k, v in GOOD.items() if k != "agw_openrouter_identities"}
    got = keys(render(tmp_path, **hv))
    assert got["skynet"]["allowedModels"] == ["gpt-oss-120b"]
    assert got["ws"]["allowedModels"] == ["gpt-oss-120b"]


def test_the_shared_key_identity_is_denied_openrouter_models_too(tmp_path):
    hv = {**GOOD, "_legacy_shared_active": True, "legacy_shared_expires": "2999-01-01"}
    got = keys(render(tmp_path, **hv))
    assert got["legacy-shared"]["allowedModels"] == ["gpt-oss-120b"]
    d = tmp_path / "named"
    d.mkdir()
    # Even when an inventory names it, the template denies it (the deploy refuses naming it, below).
    named = keys(render(d, **{**hv, "agw_openrouter_identities": ["skynet", "legacy-shared"]}))
    assert named["legacy-shared"]["allowedModels"] == ["gpt-oss-120b"]


# -- Admin-derived identities (agw_openrouter_admins) ---------------------------------------------

ADMIN_HV = {
    **GOOD,
    "agw_clients": ["ws"],
    "agw_openrouter_identities": [],
    "agw_openrouter_admins": True,
    "platform_admins": [admin("alice", "alice"), admin("bob", "bob")],
}


def test_the_admin_flag_allows_every_admin_derived_identity_and_no_service_client(tmp_path):
    got = keys(render(tmp_path, **ADMIN_HV))
    assert "allowedModels" not in got["alice"] and "allowedModels" not in got["bob"]
    assert got["ws"]["allowedModels"] == ["gpt-oss-120b"]  # a service client stays denied


def test_the_admin_flag_defaults_to_off_so_admins_are_denied_like_any_identity(tmp_path):
    hv = {k: v for k, v in ADMIN_HV.items() if k != "agw_openrouter_admins"}
    hv["agw_openrouter_identities"] = ["ws"]
    got = keys(render(tmp_path, **hv))
    assert got["alice"]["allowedModels"] == ["gpt-oss-120b"] and got["bob"]["allowedModels"] == ["gpt-oss-120b"]
    assert "allowedModels" not in got["ws"]


def test_the_admin_flag_adds_to_the_explicit_list_and_each_name_counts_once(tmp_path):
    hv = {**ADMIN_HV, "agw_openrouter_identities": ["ws", "alice", "ws"]}
    assert evaluate(tmp_path, hv)["ids"] == ["ws", "alice", "bob"]
    d = tmp_path / "render"
    d.mkdir()
    got = keys(render(d, **hv))
    assert all("allowedModels" not in got[n] for n in ("ws", "alice", "bob"))
    assert problems(tmp_path, **{k: v for k, v in hv.items() if k not in ("agw_models", "agw_clients")}) == []


def test_an_admin_with_its_own_allow_list_keeps_it_when_the_admin_flag_allows_openrouter(tmp_path):
    hv = {**ADMIN_HV, "agw_client_policies": {"alice": {"allowed_models": ["or-mini"]}}}
    assert keys(render(tmp_path, **hv))["alice"]["allowedModels"] == ["or-mini"]


def test_the_verify_runs_as_an_admin_identity_with_openrouter_models_last(tmp_path):
    hv = {**ADMIN_HV, "agw_clients": [], "agw_models": [OR_MINI, VLLM]}
    assert verify_models(tmp_path, **hv) == ["gpt-oss-120b", "or-mini"]  # first identity is alice


# -- Refusals ------------------------------------------------------------------------------------


def problems(tmp_path, **hv) -> list:
    return evaluate(tmp_path, {**GOOD, **hv})["problems"]


def test_a_complete_declaration_has_no_problem(tmp_path):
    assert problems(tmp_path) == []


@pytest.mark.parametrize("name", ["*", "or-*", "*/x"])
def test_a_wildcard_openrouter_name_is_refused(tmp_path, name):
    got = problems(tmp_path, agw_models=[VLLM, {**OR_MINI, "name": name}])
    assert len(got) == 1 and "never a wildcard" in got[0]


def test_a_wildcard_upstream_id_is_refused(tmp_path):
    got = problems(tmp_path, agw_models=[VLLM, {**OR_MINI, "upstream_model": "openai/*"}])
    assert len(got) == 1 and "never a wildcard" in got[0]


@pytest.mark.parametrize("upstream", [None, "", "gpt-4o-mini", "openai/", "/gpt", "openai/gpt 4", "openai/x'y"])
def test_an_empty_or_malformed_openrouter_id_is_refused(tmp_path, upstream):
    entry = {k: v for k, v in OR_MINI.items() if k != "upstream_model"}
    if upstream is not None:
        entry["upstream_model"] = upstream
    got = problems(tmp_path, agw_models=[VLLM, entry])
    assert len(got) == 1 and "vendor/model" in got[0]


@pytest.mark.parametrize("name", ["a b", "a'b", "a\nb", ""])
def test_an_openrouter_name_outside_the_safe_charset_is_refused(tmp_path, name):
    got = problems(tmp_path, agw_models=[VLLM, {**OR_MINI, "name": name}])
    assert any("name must match" in p for p in got)


def test_a_provider_other_than_openrouter_is_refused_not_rendered_as_vllm(tmp_path):
    got = problems(tmp_path, agw_models=[VLLM, {**OR_MINI, "provider": "OpenRouter"}])
    assert len(got) == 1 and 'provider "OpenRouter" is not supported' in got[0]


def test_a_duplicated_name_is_refused(tmp_path):
    got = problems(tmp_path, agw_models=[VLLM, OR_MINI, {"name": "or-mini"}])
    assert len(got) == 1 and "declared more than once" in got[0]


def test_enabling_openrouter_without_declaring_who_may_call_it_is_refused(tmp_path):
    hv = {k: v for k, v in GOOD.items() if k != "agw_openrouter_identities"}
    got = evaluate(tmp_path, hv)["problems"]
    assert len(got) == 1 and "no identity may call the OpenRouter models" in got[0]


@pytest.mark.parametrize(
    "hv",
    [
        {"agw_openrouter_identities": []},  # declared, but nobody
        {"agw_openrouter_identities": [], "agw_openrouter_admins": False},
        {"agw_openrouter_identities": [], "agw_openrouter_admins": True},  # flag on, no admin identities derived
        # flag on, admins declared, but the derivation gate is off: still nobody
        {
            "agw_openrouter_identities": [],
            "agw_openrouter_admins": True,
            "platform_admins": [admin("alice", "alice")],
            "agw_admin_identities_enabled": None,
        },
    ],
)
def test_an_empty_union_of_who_may_call_openrouter_is_refused(tmp_path, hv):
    got = problems(tmp_path, **hv)
    assert len(got) == 1 and "no identity may call the OpenRouter models" in got[0]


@pytest.mark.parametrize("value", ["skynet", {"skynet": True}, 3])
def test_an_identities_value_that_is_not_a_list_is_refused(tmp_path, value):
    got = problems(tmp_path, agw_openrouter_identities=value)
    assert "agw_openrouter_identities must be a list of identity names" in got


def test_a_named_identity_that_is_not_declared_is_refused(tmp_path):
    got = problems(tmp_path, agw_openrouter_identities=["skynet", "typo"])
    assert got == ['agw_openrouter_identities names "typo", which is not a declared gateway identity']


def test_the_identities_list_is_not_read_while_no_model_is_openrouter(tmp_path):
    assert (
        evaluate(tmp_path, {"agw_models": [VLLM], "agw_clients": ["ws"], "agw_openrouter_identities": ["x"]})[
            "problems"
        ]
        == []
    )


def test_the_deploy_refuses_a_malformed_declaration_before_any_render(tmp_path):
    names = [t.get("name") for t in PHASE1["tasks"]]
    assert names.index(REFUSE_TASK) < names.index("Place the monorepo + ensure podman/compose")
    bad = {**GOOD, "agw_models": [VLLM, {**OR_MINI, "name": "*"}]}
    r = _ansible(tmp_path, bad, [_task(PHASE1, REFUSE_TASK)])
    assert r.returncode != 0 and "never a wildcard" in r.stdout
    d = tmp_path / "ok"
    d.mkdir()
    ok = _ansible(d, GOOD, [_task(PHASE1, REFUSE_TASK)])
    assert ok.returncode == 0, ok.stdout + ok.stderr


# -- Review findings: internal identities, glob names, never-billed probes -------------------------


@pytest.mark.parametrize("internal", ["mcp-self", "legacy-shared"])
def test_an_internal_identity_listed_for_openrouter_is_refused_by_name(tmp_path, internal):
    for i, clients in enumerate((["skynet", "ws"], ["skynet", "ws", internal])):
        d = tmp_path / str(i)
        d.mkdir()
        got = problems(d, agw_clients=clients, agw_openrouter_identities=["skynet", internal])
        assert got == [
            f'agw_openrouter_identities names "{internal}", '
            "an internal identity that is never granted OpenRouter models"
        ]


def test_the_internal_list_covers_the_identities_the_template_renders_for_itself(tmp_path):
    text = CONFIG.read_text()
    rendered_for_itself = {"legacy-shared"} | set(
        __import__("re").search(r"_internal_ids = \[(.*?)\]", text).group(1).replace("'", "").split(", ")
    )
    internal = evaluate(tmp_path, GOOD)
    assert internal  # the vars file evaluated
    out = tmp_path / "internal.json"
    r = _ansible(
        tmp_path,
        GOOD,
        [{"ansible.builtin.copy": {"content": "{{ _agw_internal_identities | to_json }}", "dest": str(out)}}],
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert rendered_for_itself <= set(json.loads(out.read_text()))


def test_mcp_self_keeps_an_explicit_allow_list_without_the_openrouter_names(tmp_path):
    hv = {
        **GOOD,
        "agw_clients": ["skynet", "ws", "mcp-self"],
        "agw_mcp_enabled": True,
        "agw_mcp_openapi_targets": TARGETS,
        "_agw_mcp_schemas": {SCHEMA_FILE: SCHEMA_TEXT},
        "secrets": {**STORE, "client_mcp-self": "stored-key-for-mcp-self"},
    }
    cfg = render(tmp_path, **hv)
    assert keys(cfg)["mcp-self"]["allowedModels"] == ["gpt-oss-120b"]
    # Even named in the list, as an inventory that skipped the deploy's refusal would.
    d = tmp_path / "named"
    d.mkdir()
    named = render(d, **{**hv, "agw_openrouter_identities": ["skynet", "mcp-self"]})
    assert keys(named)["mcp-self"]["allowedModels"] == ["gpt-oss-120b"]
    # The /mcp key lists (every other apiKey block) never carried allowedModels.
    other = [b for b in _api_key_blocks(cfg) if b is not cfg["llm"]["policies"]["apiKey"]]
    assert other and not any("allowedModels" in k for b in other for k in (b["keys"] or []))


def _api_key_blocks(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "apiKey" and isinstance(v, dict) and "keys" in v:
                yield v
            yield from _api_key_blocks(v)
    elif isinstance(node, list):
        for v in node:
            yield from _api_key_blocks(v)


@pytest.mark.parametrize("name", ["a*", "*", "a?b", "a[b]", "x]"])
def test_a_glob_character_in_any_model_name_is_refused(tmp_path, name):
    got = problems(tmp_path, agw_models=[VLLM, {"name": name}, OR_MINI])
    assert got == [f'agw_models entry "{name}": a model name must not contain a glob character (* ? [ ])']


def test_a_glob_openrouter_name_still_gets_the_one_wildcard_message(tmp_path):
    got = problems(tmp_path, agw_models=[VLLM, {**OR_MINI, "name": "or-*"}])
    assert len(got) == 1 and "never a wildcard" in got[0]


def test_a_glob_in_a_plain_vllm_inventory_is_refused_too(tmp_path):
    got = evaluate(tmp_path, {"agw_models": [{"name": "m*"}], "agw_clients": ["ws"]})["problems"]
    assert len(got) == 1 and "glob character" in got[0]


def _templar(play: dict, names: list, inputs: dict) -> dict:
    """The play's own var expressions, rendered by ansible's templar in a fresh interpreter."""
    script = """
import json, sys
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
exprs = json.loads(sys.argv[1]); v = json.loads(sys.argv[2])
for k, e in exprs.items():
    v[k] = Templar(loader=DataLoader(), variables=v).template(trust_as_template(e))
print(json.dumps({k: v[k] for k in exprs}))
"""
    exprs = {k: play["vars"][k] for k in names}
    r = subprocess.run(
        [ansible_python(), "-I", "-c", script, json.dumps(exprs), json.dumps(inputs)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


MODELS_OR_FIRST = [OR_MINI, VLLM, {"name": "m2"}, OR_SONNET]


def test_the_conformance_run_never_picks_an_openrouter_model():
    play = next(p for p in yaml.safe_load(CONFORMANCE.read_text()) if "_client_models" in p.get("vars", {}))
    base = {"agw_models": MODELS_OR_FIRST, "_client": "ws"}
    got = _templar(play, ["_client_models", "_model"], {**base, "agw_clients": ["ws"]})
    assert got["_client_models"] == ["gpt-oss-120b", "m2"] and got["_model"] == "gpt-oss-120b"
    policy = {"agw_client_policies": {"ws": {"allowed_models": ["or-sonnet", "m2", "or-mini"]}}}
    got = _templar(play, ["_client_models", "_model"], {**base, **policy})
    assert got["_client_models"] == ["m2"]


def test_the_isolation_probe_never_bursts_or_controls_with_an_openrouter_model():
    play = next(p for p in yaml.safe_load(ISOLATION.read_text()) if "_burst_models" in p.get("vars", {}))
    inputs = {"agw_models": MODELS_OR_FIRST, "_burst": "b", "_control": "c"}
    got = _templar(play, ["_openrouter_names", "_burst_models", "_control_models"], inputs)
    assert got["_burst_models"] == ["gpt-oss-120b", "m2"] == got["_control_models"]
    policy = {"agw_client_policies": {"b": {"allowed_models": ["or-mini", "m2"]}}}
    got = _templar(play, ["_openrouter_names", "_burst_models", "_control_models"], {**inputs, **policy})
    assert got["_burst_models"] == ["m2"]


def test_the_mcp_verify_asks_the_first_vllm_model_not_an_openrouter_one_declared_first():
    task = _task(PHASE3, "MCP: pick the chat tool and the model")
    expr = task["ansible.builtin.set_fact"]["_mcp_model"]
    play = {"vars": {"_mcp_model": expr}}
    assert _templar(play, ["_mcp_model"], {"agw_models": MODELS_OR_FIRST})["_mcp_model"] == "gpt-oss-120b"
    assert _templar(play, ["_mcp_model"], {"agw_models": [VLLM, OR_MINI]})["_mcp_model"] == "gpt-oss-120b"


def _mcp_model(models, policies=None):
    expr = _task(PHASE3, "MCP: pick the chat tool and the model")["ansible.builtin.set_fact"]["_mcp_model"]
    inputs = {"agw_models": models, **({"agw_client_policies": policies} if policies is not None else {})}
    return _templar({"vars": {"_mcp_model": expr}}, ["_mcp_model"], inputs)["_mcp_model"]


def test_the_mcp_verify_asks_a_model_mcp_self_may_use():
    models = [OR_MINI, VLLM, {"name": "m2"}]
    only_second = {"mcp-self": {"allowed_models": ["m2"]}}
    assert _mcp_model(models, only_second) == "m2"  # not the first declared name, which mcp-self may not use
    both = {"mcp-self": {"allowed_models": ["m2", "gpt-oss-120b", "or-mini"]}}
    assert _mcp_model(models, both) == "gpt-oss-120b"  # agw_models order, never an OpenRouter name
    assert _mcp_model(models, {"mcp-self": {"tokens_per_hour": 5}}) == "gpt-oss-120b"  # no list: every name
    assert _mcp_model(models, {}) == "gpt-oss-120b"


def test_the_mcp_verify_has_no_model_when_none_qualifies_and_says_so():
    assert _mcp_model([OR_MINI, VLLM], {"mcp-self": {"allowed_models": ["or-mini", "elsewhere"]}}) == ""
    assert _mcp_model([OR_MINI]) == ""
    assert _mcp_model([]) == ""
    task = _task(PHASE3, "Require a model mcp-self may use for the chat call")
    assert task["ansible.builtin.assert"]["that"] == "_mcp_model | length > 0"
    assert "unproven" in task["ansible.builtin.assert"]["fail_msg"]


# -- The credential: declared and required only when a model needs it -----------------------------


def test_the_openrouter_key_is_an_existing_secret_declared_and_required_with_a_model(tmp_path):
    got = evaluate(tmp_path, GOOD)
    defs = {d["name"]: d for d in got["defs"]}
    assert defs["openrouter_api_key"] == {"name": "openrouter_api_key", "type": "existing"}  # never generated
    assert got["required"] == ["vllm_api_key", "openrouter_api_key"]
    assert [d["name"] for d in got["defs"]][-2:] == ["client_skynet", "client_ws"]  # client fields still last


def test_the_openrouter_key_stays_required_when_the_vllm_upstream_takes_no_key(tmp_path):
    got = evaluate(tmp_path, {**GOOD, "agw_upstream_requires_key": False})
    assert got["required"] == ["openrouter_api_key"]


# -- The verify never spends OpenRouter tokens unless asked --------------------------------------


def picked(tmp: Path, host_vars: dict, tasks=(PICK_TASK,), extra=None) -> subprocess.CompletedProcess:
    out = tmp / "picked.json"
    run = [
        *[_task(PHASE3, n) for n in tasks],
        {
            "ansible.builtin.copy": {
                "content": "{{ {'models': _verify_models, 'or': _verify_or_model | default('')} | to_json }}",
                "dest": str(out),
            }
        },
    ]
    return _ansible(
        tmp,
        host_vars,
        run,
        {"_verify_default": PHASE3["vars"]["_verify_default"], "_agwp_base": "http://gw.invalid", **(extra or {})},
    )


def verify_models(tmp: Path, **hv) -> list:
    r = picked(tmp, {**GOOD, **hv})
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads((tmp / "picked.json").read_text())["models"]


def test_the_verifying_identity_sees_openrouter_models_last_and_only_when_named(tmp_path):
    hv = {"agw_models": [OR_MINI, VLLM, OR_SONNET], "agw_openrouter_identities": ["skynet"]}
    assert verify_models(tmp_path, **hv) == ["gpt-oss-120b", "or-mini", "or-sonnet"]  # default completion is vLLM
    d = tmp_path / "other"
    d.mkdir()
    assert verify_models(d, **hv, agw_verify_client="ws") == ["gpt-oss-120b"]


def test_an_allow_list_keeps_its_order_within_each_group(tmp_path):
    hv = {
        "agw_models": [VLLM, {"name": "m2"}, OR_MINI, OR_SONNET],
        "agw_client_policies": {"skynet": {"allowed_models": ["or-sonnet", "m2", "or-mini", "gpt-oss-120b"]}},
    }
    assert verify_models(tmp_path, **hv) == ["m2", "gpt-oss-120b", "or-sonnet", "or-mini"]


def test_without_openrouter_models_the_verify_models_are_unchanged(tmp_path):
    hv = {"agw_models": [VLLM, {"name": "m2"}], "agw_clients": ["ws"]}
    r = picked(tmp_path, hv)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads((tmp_path / "picked.json").read_text())["models"] == ["gpt-oss-120b", "m2"]


def test_an_identity_allowed_only_openrouter_models_is_refused_by_the_verify(tmp_path):
    hv = {
        **GOOD,
        "agw_client_policies": {"skynet": {"allowed_models": ["or-mini"]}},
    }
    r = picked(tmp_path, hv, (PICK_TASK, NON_OR_TASK))
    assert r.returncode != 0 and "allowed only OpenRouter models" in r.stdout
    d = tmp_path / "ok"
    d.mkdir()
    ok = picked(d, GOOD, (PICK_TASK, NON_OR_TASK))
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_the_opt_in_picks_the_first_openrouter_model_the_identity_may_use(tmp_path):
    hv = {**GOOD, "agw_models": [VLLM, OR_SONNET, OR_MINI], "agw_verify_openrouter": True}
    r = picked(tmp_path, hv, (PICK_TASK, PICK_OR_TASK, REQUIRE_OR_TASK))
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads((tmp_path / "picked.json").read_text())["or"] == "or-sonnet"


def test_the_opt_in_without_an_openrouter_model_for_the_identity_fails_loudly(tmp_path):
    hv = {**GOOD, "agw_verify_openrouter": True, "agw_verify_client": "ws"}
    r = picked(tmp_path, hv, (PICK_TASK, PICK_OR_TASK, REQUIRE_OR_TASK))
    assert r.returncode != 0 and "may use no OpenRouter model" in r.stdout


def test_without_the_flag_no_openrouter_model_is_picked(tmp_path):
    r = picked(tmp_path, GOOD, (PICK_TASK, PICK_OR_TASK, REQUIRE_OR_TASK))
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads((tmp_path / "picked.json").read_text())["or"] == ""


def test_every_opt_in_task_is_gated_on_the_flag_and_the_completion_is_tiny():
    for name in OPT_IN_TASKS:
        task = _task(PHASE3, name)
        assert "agw_verify_openrouter | default(false) | bool" in task["when"], name
    probe = _task(PHASE3, "An enrolled identity completes one OpenRouter request (opt-in)")
    assert probe["vars"]["_agwp_body"]["max_tokens"] == 8
    assert probe["vars"]["_agwp_body"]["model"] == "{{ _verify_or_model }}"
    # The default completion keeps asking for the first (non-OpenRouter) model.
    default = _task(PHASE3, "An enrolled identity completes one chat request through the upstream")
    assert default["vars"]["_agwp_body"]["model"] == "{{ _verify_models[0] }}"


def test_the_playbook_documents_the_new_inventory_names():
    text = DEPLOY.read_text()
    for name in ("agw_openrouter_identities", "agw_verify_openrouter", "provider: openrouter"):
        assert name in text.split("- name:", 1)[0], name


# -- Every deploy: the render is counted, and a service identity is refused OpenRouter -----------

COUNT_TASK = "Report the rendered identities and key entries"
REFUSAL_PROBE = "A service identity is refused an OpenRouter model"
REFUSAL_ASSERT = "Require the gateway to refuse an OpenRouter model the identity may not use"


def test_the_rendered_count_is_printed_on_every_run_and_holds_no_secret():
    task = _task(PHASE1, COUNT_TASK)
    assert "ansible.builtin.debug" in task
    assert "no_log" not in task
    assert task.get("when") == "not ansible_check_mode"


def test_the_refusal_probe_expects_a_refusal_for_a_non_openrouter_identity_only():
    probe = _task(PHASE3, REFUSAL_PROBE)
    assert probe["vars"]["_agwp_status"] == [400, 403, 404, 429]  # 200 would mean OpenRouter answered it
    assert 200 not in probe["vars"]["_agwp_status"]
    assert probe["vars"]["_agwp_method"] == "POST"
    assert "_verify_client not in (_agw_openrouter_identities | default([]))" in probe["when"]
    assert "_or_all | length > 0" in probe["when"]
    assert "not (_verify_refused | bool)" in probe["when"]


def _refusal_check(tmp: Path, status: int, unproven: bool) -> subprocess.CompletedProcess:
    # The real assert task's own expression, run against a given probe status and operator flag.
    that = _task(PHASE3, REFUSAL_ASSERT)["ansible.builtin.assert"]["that"]
    check = {"ansible.builtin.assert": {"that": that}}
    return _ansible(tmp, {"agw_verify_allow_unproven": unproven}, [check], {"_keyed_or_refused": status})


@pytest.mark.parametrize(
    "status, unproven, passes",
    [
        (400, False, True),
        (403, False, True),
        (404, False, True),
        (429, True, True),  # the gateway's own bucket, accepted as unproven
        (429, False, False),  # the same 429 without the operator's acceptance fails the deploy
        (200, False, False),  # the identity reached OpenRouter
        (200, True, False),
        (401, True, False),
    ],
)
def test_the_refusal_assert_passes_a_refusal_and_a_429_only_when_unproven_is_accepted(
    tmp_path, status, unproven, passes
):
    r = _refusal_check(tmp_path, status, unproven)
    assert (r.returncode == 0) == passes, r.stdout + r.stderr


def test_the_report_names_the_refusal_only_when_the_probe_ran_and_says_when_it_was_not_proven():
    msg = _task(PHASE3, "Report")["ansible.builtin.debug"]["msg"]
    assert "{% if _keyed_or_refused is defined and (_keyed_or_refused | int) == 429 %}" in msg
    assert "was not proven (HTTP 429, request bucket)" in msg
    assert "{% elif _keyed_or_refused is defined %}" in msg


# -- On the pinned image ---------------------------------------------------------------------------

live = pytest.mark.skipif(not _image_present(), reason=f"podman or {IMAGE} not available")


@live
def test_the_render_passes_the_pinned_images_validator(tmp_path):
    render(tmp_path, **GOOD)
    env = tmp_path / "gateway.env"
    env.write_text(f"VLLM_API_KEY=v\nOPENROUTER_API_KEY={OR_KEY}\nAGW_DATABASE_URL=postgresql://db/x\n")
    r = subprocess.run(
        [
            "podman",
            "run",
            "--pull=never",
            "--rm",
            "--env-file",
            str(env),
            "-v",
            f"{tmp_path / 'config.yaml'}:/config.yaml:ro",
            IMAGE,
            "--validate-only",
            "-f",
            "/config.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0 and "Configuration is valid" in (r.stdout + r.stderr), r.stdout + r.stderr


class Recorder(DrainingHandler):
    """A stand-in OpenRouter: records the Authorization header and the body, answers a chat."""

    seen: list = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if body.get("stream"):
            chunk = {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "delta": {"content": "pong"}, "finish_reason": "stop"}],
            }
            final = {**chunk, "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for event in (chunk, final):
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        data = json.dumps(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "pong"}}],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _post(port, body, key):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@live
def test_the_gateway_sends_the_openrouter_id_and_key_and_gates_the_model_by_identity(tmp_path):
    """The real render, with only the OpenRouter base URL pointed at a local recorder (a request
    to openrouter.ai itself costs money): the client name is remapped to the OpenRouter id, the
    upstream receives the OpenRouter key and never the client's, a streamed request is asked for
    usage, and an identity not named in agw_openrouter_identities is refused the model."""
    Recorder.seen = []
    server = ThreadingHTTPServer(("0.0.0.0", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    name = f"agw-openrouter-{_free_port()}"
    try:
        cfg = render(tmp_path, **{**GOOD, "agw_plaintext_keys": True, "local_mode": True})
        for m in cfg["llm"]["models"]:
            if m["provider"] == "openrouter":
                m["params"]["baseUrl"] = f"http://host.containers.internal:{server.server_port}/api/v1"
            else:
                m["params"]["baseUrl"] = f"http://host.containers.internal:{server.server_port}/v1"
        cfg.pop("frontendPolicies", None)
        cfg.pop("ui", None)
        cfg["config"] = {"adminAddr": "127.0.0.1:15000"}
        for entry in cfg["llm"]["policies"]["apiKey"]["keys"]:
            entry.pop("budgets", None)  # budgets need the database, which this check does not start
        (tmp_path / "live.yaml").write_text(yaml.safe_dump(cfg))
        port = _free_port()
        subprocess.run(
            [
                "podman",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:4000",
                "-e",
                f"OPENROUTER_API_KEY={OR_KEY}",
                "-e",
                "VLLM_API_KEY=vllm-synthetic",
                "-v",
                f"{tmp_path / 'live.yaml'}:/config.yaml:ro",
                IMAGE,
                "-f",
                "/config.yaml",
            ],
            check=True,
            capture_output=True,
        )
        deadline = time.time() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        chat = {"model": "or-mini", "messages": [{"role": "user", "content": "x"}]}
        assert _post(port, chat, STORE["client_skynet"]) == 200
        assert _post(port, {**chat, "stream": True}, STORE["client_skynet"]) == 200
        refused = _post(port, chat, STORE["client_ws"])
        assert refused in (400, 403, 404), refused
        assert len(Recorder.seen) == 2, Recorder.seen
        for call in Recorder.seen:
            assert call["path"] == "/api/v1/chat/completions"
            assert call["auth"] == f"Bearer {OR_KEY}"  # the gateway's key, never the client's
            assert call["body"]["model"] == "openai/gpt-4o-mini"  # remapped from the client's name
        assert Recorder.seen[1]["body"]["stream_options"] == {"include_usage": True}
        assert "stream_options" not in Recorder.seen[0]["body"]
    finally:
        subprocess.run(["podman", "rm", "-f", name], capture_output=True)
        server.shutdown()
        server.server_close()
