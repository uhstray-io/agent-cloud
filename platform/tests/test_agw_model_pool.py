"""Shared model pools: one hourly token cap on a model for every caller together.

v1.5.0 budgets attach to API keys only, so config.yaml.j2 renders a pooled model twice: its public name relays
over the gateway's own loopback listener as the pool identity, and the hidden `pool/<name>` entry reaches the
upstream for that identity alone. The real vars file and template run on localhost through harness_sandbox.
What is pinned:

- the public entry relays to the loopback listener under the pool key (an env reference), the hidden entry
  carries the declared upstream, and the loopback listener exists with MCP off;
- the pool identity may call only `pool/` names (no relay loop), and every other key entry, admins and the
  legacy shared key included, has every `pool/` name removed (no way around the cap);
- the deploy refuses a pool it cannot render safely;
- on the pinned image, the real render caps the pool and refuses the hidden name.
"""

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml
from test_agw_stream_usage import IMAGE, Upstream, _free_port, _image_present, _post

REPO = playbook_yaml.REPO
DECL = REPO / "platform/playbooks/vars/secret-declarations/agentgateway.yml"
DEPLOY_DIR = REPO / "platform/services/agentgateway/deployment"
CONFIG = DEPLOY_DIR / "templates/config.yaml.j2"
ENV = DEPLOY_DIR / "templates/env.j2"

KEYS = {"opencode": "oc-key-1111", "kimi-pool": "pool-key-2222", "alice": "alice-key-3333"}
BASE = {
    "agw_clients": ["opencode", "kimi-pool"],
    "agw_admin_identities_enabled": True,
    "platform_admins": [{"key": "alice", "username": "alice", "name": "Alice", "email": "alice@example.invalid"}],
    "agw_openrouter_admins": True,
    "agw_models": [
        {"name": "m"},
        {"name": "vendor/kimi", "provider": "openrouter", "upstream_model": "vendor/kimi",
         "pool_identity": "kimi-pool"},
    ],
    "agw_client_policies": {"kimi-pool": {"tokens_per_hour": 3000000}},
    "agw_upstream_base_url": "http://upstream.invalid:8000/v1",
    "agw_ui_enabled": False,
    "_legacy_shared_active": True,
    "_legacy_fp_recorded": "f" * 64,
    "secrets": {"agw_db_password": "p", "openrouter_api_key": "or-key",
                **{"client_" + n: k for n, k in KEYS.items()}},
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


def _render(tmp: Path, **hv) -> tuple[dict, str]:
    tasks = [{"ansible.builtin.template": {"src": str(src), "dest": str(tmp / dest), "mode": "0600"}}
             for src, dest in ((CONFIG, "config.yaml"), (ENV, "env"))]
    r = _play(tmp, {**BASE, **hv}, tasks)
    assert r.returncode == 0, r.stdout + r.stderr
    return yaml.safe_load((tmp / "config.yaml").read_text()), (tmp / "env").read_text()


def _problems(tmp: Path, **hv) -> list[str]:
    out = tmp / "problems.json"
    r = _play(tmp, {**BASE, **hv}, [{"ansible.builtin.copy": {"content": "{{ _agw_pool_problems | to_json }}",
                                                              "dest": str(out)}}])
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(out.read_text())


def _keys(cfg: dict) -> dict:
    return {k["metadata"]["name"]: k for k in cfg["llm"]["policies"]["apiKey"]["keys"]}


# ── Render ────────────────────────────────────────────────────────────────────


def test_the_public_name_relays_over_loopback_and_the_hidden_name_reaches_the_upstream(tmp_path):
    cfg, env = _render(tmp_path)
    models = {m["name"]: m for m in cfg["llm"]["models"]}
    assert list(models) == ["m", "vendor/kimi", "pool/vendor/kimi"]
    outer, inner = models["vendor/kimi"], models["pool/vendor/kimi"]
    assert outer["params"] == {"baseUrl": "http://127.0.0.1:4002/v1", "model": "pool/vendor/kimi",
                               "apiKey": "$AGW_POOL_KEY_KIMI_POOL"}
    assert inner["provider"] == "openrouter" and inner["params"]["model"] == "vendor/kimi"
    # MCP is off: the loopback listener exists for the pool alone, restricted to the gateway's own address.
    assert "mcp" not in cfg
    assert cfg["gateways"]["loopback"]["authorization"]["rules"] == [{"require": 'source.address == "127.0.0.1"'}]
    assert "loopback" in cfg["llm"]["gateways"]
    assert "AGW_POOL_KEY_KIMI_POOL=pool-key-2222\n" in env
    assert "pool-key-2222" not in (tmp_path / "config.yaml").read_text()


def test_only_the_pool_identity_may_call_the_hidden_name_and_it_may_call_nothing_else(tmp_path):
    cfg, _ = _render(tmp_path)
    keys = _keys(cfg)
    assert set(keys) == {"opencode", "kimi-pool", "alice", "legacy-shared"}
    assert keys["kimi-pool"]["allowedModels"] == ["pool/vendor/kimi"]  # never the public name: no relay loop
    assert keys["kimi-pool"]["budgets"][0]["limit"]["amount"] == 3000000
    assert keys["kimi-pool"]["keyHash"] == "sha256:" + hashlib.sha256(b"pool-key-2222").hexdigest()
    for name, entry in keys.items():
        if name != "kimi-pool":
            assert not any(m.startswith("pool/") for m in entry["allowedModels"]), name
    # alice is an OpenRouter admin: she keeps the public name.
    assert keys["alice"]["allowedModels"] == ["m", "vendor/kimi"]


def test_without_a_pool_nothing_changes(tmp_path):
    models = [{"name": "m"}, {"name": "vendor/kimi", "provider": "openrouter", "upstream_model": "vendor/kimi"}]
    cfg, env = _render(tmp_path, agw_models=models, agw_clients=["opencode"], agw_client_policies={})
    assert "loopback" not in cfg["gateways"] and "loopback" not in cfg["llm"]["gateways"]
    assert [m["name"] for m in cfg["llm"]["models"]] == ["m", "vendor/kimi"]
    assert "allowedModels" not in _keys(cfg)["alice"]
    assert "AGW_POOL_KEY" not in env


# ── Deploy guard ──────────────────────────────────────────────────────────────


def test_a_correct_pool_has_no_problems(tmp_path):
    assert _problems(tmp_path) == []


@pytest.mark.parametrize("hv, needle", [
    pytest.param({"agw_client_policies": {}}, "needs agw_client_policies.kimi-pool.tokens_per_hour", id="no-figure"),
    pytest.param({"agw_client_policies": {"kimi-pool": {"tokens_per_hour": 5, "tier": "admin"}}},
                 "must not set allowed_models or tier", id="tier"),
    pytest.param({"agw_clients": ["opencode"]}, "must be declared in agw_clients", id="not-a-client"),
    pytest.param({"agw_clients": ["kimi-pool", "opencode"]}, "the identity the deploy verifies with", id="verify-id"),
    pytest.param({"agw_openrouter_identities": ["kimi-pool"]}, "must not be in agw_openrouter_identities",
                 id="openrouter-list"),
    pytest.param({"agw_models": [{"name": "m", "pool_identity": "alice"}], "agw_clients": ["opencode", "alice"],
                  "agw_client_policies": {"alice": {"tokens_per_hour": 5}}}, "is a personal admin identity",
                 id="admin"),
    pytest.param({"agw_models": [{"name": "pool/m"}]}, "the pool/ prefix is reserved", id="reserved-prefix"),
    pytest.param({"agw_loopback_port": 4000}, "agw_loopback_port must be", id="loopback-port"),
])
def test_an_unsafe_pool_is_refused_by_name(tmp_path, hv, needle):
    problems = _problems(tmp_path, **hv)
    assert any(needle in p for p in problems), problems


# ── Live, on the pinned image ─────────────────────────────────────────────────


@pytest.mark.skipif(not _image_present(), reason=f"podman or {IMAGE} not available")
def test_the_real_render_caps_the_pool_on_the_pinned_image(tmp_path):
    # The hidden entry is a custom upstream here (the fake one), so the run spends nothing; the pool rendering is
    # provider-agnostic. Pool budget 10 tokens, each call costs 50: the first call passes, the second is the
    # pool's 429 relayed to the caller, and the hidden name is refused to the caller's own key.
    up = Upstream()
    try:
        url = f"http://host.containers.internal:{up.server.server_port}/v1"
        cfg, _ = _render(tmp_path, agw_upstream_base_url=url, agw_models=[{"name": "m", "pool_identity": "kimi-pool"}],
                         agw_client_policies={"kimi-pool": {"tokens_per_hour": 10}}, _legacy_shared_active=False)
        cfg["config"]["database"]["url"] = "sqlite:///tmp/budget.db"
        path = tmp_path / "live.yaml"
        path.write_text(yaml.safe_dump(cfg))
        port, name = _free_port(), f"agw-model-pool-{_free_port()}"
        subprocess.run(["podman", "run", "-d", "--rm", "--name", name,
                        "-e", f"AGW_POOL_KEY_KIMI_POOL={KEYS['kimi-pool']}", "-p", f"127.0.0.1:{port}:4000",
                        "-v", f"{path}:/config.yaml:ro", IMAGE, "-f", "/config.yaml"],
                       check=True, capture_output=True)
        try:
            chat = lambda model: {"model": model, "messages": [{"role": "user", "content": "x"}]}  # noqa: E731
            deadline = time.time() + 30
            while True:  # the listener may reset a connection before it is up
                try:
                    first = _post(port, "/v1/chat/completions", chat("m"), key=KEYS["opencode"])
                except OSError:
                    first = None
                if first not in (None, 502, 503) or time.time() > deadline:
                    break
                time.sleep(0.5)
            assert first == 200
            assert _post(port, "/v1/chat/completions", chat("m"), key=KEYS["opencode"]) == 429
            assert _post(port, "/v1/chat/completions", chat("pool/m"), key=KEYS["alice"]) == 403
            assert [s["body"]["model"] for s in up.seen] == ["m"]
        finally:
            subprocess.run(["podman", "rm", "-f", name], capture_output=True)
    finally:
        up.close()


# ── The deploy's own tasks (reviewer finding: the guards must be pinned where they run) ──

DEPLOY = REPO / "platform/playbooks/deploy-agentgateway.yml"
PHASE1 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 1"))
NAMES = [t.get("name") for t in PHASE1["tasks"]]
POOL_GUARD = "Refuse a shared model pool that cannot be rendered safely"
TIER_GUARD = "Refuse a budget tier with no figure"
RENDER = "Manage secrets and render env + config"
SURFACE = "Require the rendered MCP surface to match agw_mcp_enabled"


def _task(name: str) -> dict:
    return next(t for t in PHASE1["tasks"] if t.get("name") == name)


def test_the_pool_and_tier_guards_run_before_the_render_and_refuse(tmp_path):
    assert NAMES.index(POOL_GUARD) < NAMES.index(RENDER)
    assert NAMES.index(TIER_GUARD) < NAMES.index(RENDER)
    r = _play(tmp_path, BASE, [_task(POOL_GUARD), _task(TIER_GUARD)])
    assert r.returncode == 0, r.stdout + r.stderr
    r = _play(tmp_path, {**BASE, "agw_client_policies": {}}, [_task(POOL_GUARD)])
    assert r.returncode != 0 and "needs agw_client_policies.kimi-pool.tokens_per_hour" in r.stdout, r.stdout
    r = _play(tmp_path, {**BASE, "agw_tier_tokens_per_hour": {"developer": 5}}, [_task(TIER_GUARD)])
    assert r.returncode != 0 and "has no admin figure" in r.stdout, r.stdout


def _surface(tmp_path, loopback: bool, **hv):
    read = {"stdout": json.dumps({"mcp": False, "loopback": loopback})}
    return _play(tmp_path, {**BASE, "_agw_render_read": read, **hv}, [_task(SURFACE)])


def test_the_render_check_requires_the_loopback_listener_exactly_when_a_pool_or_mcp_needs_it(tmp_path):
    assert _surface(tmp_path, True).returncode == 0
    assert _surface(tmp_path, False).returncode != 0
    no_pool = {"agw_models": [{"name": "m"}]}
    assert _surface(tmp_path, False, **no_pool).returncode == 0
    assert _surface(tmp_path, True, **no_pool).returncode != 0


def test_pool_identities_are_internal_outside_the_template(tmp_path):
    # Rollback publishes no direct_<pool> copy, the isolation probe never picks a pool as its control, and the
    # skynet route refuses one: all three read the vars file's lists.
    out = tmp_path / "ids.json"
    r = _play(tmp_path, BASE, [{"ansible.builtin.copy": {"content": "{{ _agw_internal_identities | to_json }}",
                                                         "dest": str(out)}}])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "kimi-pool" in json.loads(out.read_text())
    rollback = yaml.safe_load((REPO / "platform/playbooks/rollback-inference-route.yml").read_text())
    assert any("_agw_pool_identities" in str(p.get("vars", {}).get("_declared", "")) for p in rollback)
    probe = (REPO / "platform/playbooks/probe-agentgateway-isolation.yml").read_text()
    assert "reject('in', _agw_pool_identities)" in probe
