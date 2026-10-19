"""The skynet orchestration route on the agentgateway (agentgateway task 3.3, render half).

With `agw_skynet_route_enabled`, config.yaml gains a top-level `routes:` block: `/skynet/...` on the default
gateway, admitted only to the declared route identities by a strict key policy, with backendAuth swapping the
caller's key for skynet's own token before the request reaches skynet. Disabled (the default), there is no
routes block and no skynet secret is read. The real vars file, the real refusal, shared-read and render-check
tasks of deploy-agentgateway.yml and the real config and env templates run on localhost through harness_sandbox,
so nothing is written outside the test's directory. The live tests run the pinned image when it is present.

What each test pins: a disabled render is what it was before the route existed; the route's shape and its own
strict key policy, with no budget or model list (it is not an LLM surface); the token is an env reference in
config.yaml and a value only in .env, and only while the route is on; every refusal of a bad declaration, before
any render or restart; the shared read of skynet's token is added only while the route is on; the render check
names a route that is missing, unprotected, extra or not wanted; and, on the pinned image, that the render
validates and that a keyless or wrong-identity call never reaches the backend, which receives skynet's token and
nothing else.
"""

import hashlib
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
from fake_http import DrainingHandler
from test_agw_admin_identities import CONFIG, PHASE1, REPO, SECRETS, _ansible, _task
from test_agw_stream_usage import IMAGE, _free_port, _image_present

ENV_TEMPLATE = CONFIG.with_name("env.j2")
TOKEN = "skynet-synthetic-token-7d2e"
BACKEND = "host.containers.internal:18556"
STORE = {**SECRETS, "api_token": TOKEN}
ROUTE_HV = {
    "agw_clients": ["skynet", "alice"],
    "agw_skynet_route_enabled": True,
    "agw_skynet_backend": BACKEND,
    "agw_skynet_route_identities": ["skynet"],
}
PROBLEMS_TASK = "Refuse a malformed skynet route declaration"
TOKEN_TASK = "Refuse a skynet route whose backend token is empty"
READ_TASK = "Read the identity names and key-entry count the render wrote"
CHECK_MATCH = "Require the rendered skynet route to match agw_skynet_route_enabled"
CHECK_STRICT = "Require the rendered skynet route to carry its own strict key policy"
CHECK_NAMES = "Require the skynet route's key policy to enrol exactly the declared route identities"
CHECKS = [READ_TASK, CHECK_MATCH, CHECK_STRICT, CHECK_NAMES]


def _template(tmp: Path, src: Path, dest: str, **hv) -> str:
    hv = {
        "agw_models": [{"name": "m"}],
        "agw_upstream_base_url": "http://u.invalid/v1",
        "agw_ui_enabled": False,
        "secrets": STORE,
        **hv,
    }
    task = {"ansible.builtin.template": {"src": str(src), "dest": str(tmp / dest), "mode": "0644"}}
    r = _ansible(tmp, hv, [task])
    assert r.returncode == 0, r.stdout + r.stderr
    return (tmp / dest).read_text()


def render_text(tmp: Path, **hv) -> str:
    return _template(tmp, CONFIG, "config.yaml", **hv)


def render(tmp: Path, **hv) -> dict:
    return yaml.safe_load(render_text(tmp, **hv))


def problems(tmp: Path, **hv) -> list:
    out = tmp / "eval.json"
    expr = "{{ _agw_skynet_route_problems | to_json }}"
    task = {"ansible.builtin.copy": {"content": expr, "dest": str(out)}}
    r = _ansible(tmp, {"agw_clients": ["skynet", "alice"], **hv}, [task])
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(out.read_text())


def _check(tmp: Path, text: str, host_vars: dict):
    """The real reader and the skynet render asserts, against one rendered config."""
    (tmp / "config.yaml").write_text(text)
    play_vars = {"_deploy_dir": str(tmp), "_legacy_shared_active": False}
    return _ansible(tmp, host_vars, [_task(PHASE1, n) for n in CHECKS], play_vars)


def _strict_key_hash(name: str) -> str:
    return "sha256:" + hashlib.sha256(STORE[f"client_{name}"].encode()).hexdigest()


# -- Off by default: the render is what it was before the route existed ----------------------------


@pytest.mark.parametrize("hv", [{}, {"agw_skynet_route_enabled": False}])
def test_disabled_renders_no_routes_key_and_no_token(tmp_path, hv):
    # The declaration is present but off: nothing renders, and the token is not written to .env.
    declared = {k: v for k, v in ROUTE_HV.items() if k != "agw_skynet_route_enabled"}
    text = render_text(tmp_path, **{**declared, **hv})
    assert "\nroutes:" not in text and "routes" not in yaml.safe_load(text)
    env = _template(tmp_path, ENV_TEMPLATE, ".env", **{**declared, **hv})
    assert "SKYNET_API_TOKEN" not in env


def test_disabled_with_nothing_declared_is_the_same_render_as_before(tmp_path):
    base = render_text(tmp_path, agw_clients=["skynet", "alice"])
    assert "routes" not in yaml.safe_load(base)
    assert "$SKYNET_API_TOKEN" not in base


# -- The route itself -------------------------------------------------------------------------------


def test_enabled_renders_one_route_with_its_own_strict_key_policy(tmp_path):
    (route,) = render(tmp_path, **ROUTE_HV)["routes"]
    assert route["name"] == "skynet"
    assert route["matches"] == [{"path": {"pathPrefix": "/skynet/"}}]
    assert route["backends"] == [{"host": BACKEND}]
    # Pinned to the external listener, never `ui` or the MCP self-call's `loopback` (review of #719).
    assert route["gateways"] == ["default"]
    policy = route["policies"]["apiKey"]
    assert policy["mode"] == "strict"
    assert policy["location"] == {"header": {"name": "authorization", "prefix": "Bearer "}}
    assert policy["keys"] == [{"keyHash": _strict_key_hash("skynet"), "metadata": {"name": "skynet"}}]


def test_the_route_is_not_an_llm_surface_so_its_keys_carry_no_budget_or_model_list(tmp_path):
    key = render(tmp_path, **ROUTE_HV)["routes"][0]["policies"]["apiKey"]["keys"][0]
    assert set(key) == {"keyHash", "metadata"}


def test_backend_auth_swaps_the_caller_key_for_the_env_reference_only(tmp_path):
    route = render(tmp_path, **ROUTE_HV)["routes"][0]
    assert route["policies"]["backendAuth"] == {"key": "$SKYNET_API_TOKEN"}
    text = render_text(tmp_path, **ROUTE_HV)
    assert TOKEN not in text  # the value is never in config.yaml


def test_the_token_is_written_to_env_while_the_route_is_on(tmp_path):
    env = _template(tmp_path, ENV_TEMPLATE, ".env", **ROUTE_HV)
    assert f"\nSKYNET_API_TOKEN={TOKEN}\n" in env


def test_only_the_declared_route_identities_are_enrolled_on_the_route(tmp_path):
    hv = {**ROUTE_HV, "agw_skynet_route_identities": ["alice"]}
    (route,) = render(tmp_path, **hv)["routes"]
    assert [k["metadata"]["name"] for k in route["policies"]["apiKey"]["keys"]] == ["alice"]


# -- Refusals, before any render or restart ----------------------------------------------------------


def test_the_refusal_runs_before_any_render_or_restart():
    names = [t.get("name") for t in PHASE1["tasks"]]
    refuse = names.index(PROBLEMS_TASK)
    assert refuse < names.index("Manage secrets and render env + config")
    assert refuse < names.index("Place the monorepo + ensure podman/compose")
    assert refuse < names.index("Refuse a cleartext OpenBao endpoint")


def test_the_empty_token_refusal_follows_the_shared_read_it_guards():
    names = [t.get("name") for t in PHASE1["tasks"]]
    assert names.index("Manage secrets and render env + config") < names.index(TOKEN_TASK)


@pytest.mark.parametrize(
    "hv, shared, passes",
    [
        (ROUTE_HV, {"api_token": TOKEN}, True),
        (ROUTE_HV, {"api_token": ""}, False),
        (ROUTE_HV, {}, False),
        ({**ROUTE_HV, "agw_skynet_route_enabled": False}, {"api_token": ""}, True),
    ],
)
def test_an_empty_skynet_token_is_refused_only_while_the_route_is_on(tmp_path, hv, shared, passes):
    r = _ansible(tmp_path, hv, [_task(PHASE1, TOKEN_TASK)], {"_shared": shared})
    assert (r.returncode == 0) is passes, r.stdout + r.stderr
    if not passes:
        assert "backend token is empty" in r.stdout + r.stderr
        assert TOKEN not in r.stdout + r.stderr


def test_a_complete_declaration_has_no_problem(tmp_path):
    assert problems(tmp_path, **ROUTE_HV) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"agw_skynet_backend": ""},
        {"agw_skynet_backend": "http://host:18556"},
        {"agw_skynet_backend": "host"},
        {"agw_skynet_backend": "host:"},
        {"agw_skynet_backend": "h o:1"},
        {"agw_skynet_route_identities": []},
        {"agw_skynet_route_identities": "skynet"},
        {"agw_skynet_route_identities": ["ghost"]},
        {"agw_skynet_route_identities": ["mcp-self"]},
        {"agw_skynet_route_identities": ["legacy-shared"]},
        {"agw_skynet_route_identities": ["skynet", "skynet"]},
    ],
)
def test_a_bad_route_declaration_is_refused(tmp_path, bad):
    assert problems(tmp_path, **{**ROUTE_HV, **bad}) != []


def test_an_unset_backend_and_identities_are_refused(tmp_path):
    got = problems(tmp_path, agw_skynet_route_enabled=True)
    assert len(got) == 2


def test_a_bad_declaration_is_not_read_while_the_route_is_off(tmp_path):
    bad = {"agw_skynet_route_enabled": False, "agw_skynet_backend": "", "agw_skynet_route_identities": ["ghost"]}
    assert problems(tmp_path, **bad) == []


def test_each_problem_names_the_offending_entry_and_no_token(tmp_path):
    got = problems(tmp_path, **{**ROUTE_HV, "agw_skynet_route_identities": ["ghost", "mcp-self"]})
    assert any("ghost" in p and "not a declared gateway identity" in p for p in got)
    assert any("mcp-self" in p and "internal identity" in p for p in got)
    assert all(TOKEN not in p for p in got)


# -- The shared read ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hv, expected",
    [
        ({}, [("authentik", ["agentgateway_oidc_client_secret"])]),
        (
            {"agw_skynet_route_enabled": True},
            [("authentik", ["agentgateway_oidc_client_secret"]), ("skynet", ["api_token"])],
        ),
        ({"agw_skynet_route_enabled": False}, [("authentik", ["agentgateway_oidc_client_secret"])]),
    ],
)
def test_skynets_token_is_shared_read_only_while_the_route_is_on(tmp_path, hv, expected):
    out = tmp_path / "reads.json"
    expr = "{{ _shared_reads | to_json }}"
    play_vars = {"_shared_reads": PHASE1["vars"]["_shared_reads"]}
    r = _ansible(tmp_path, hv, [{"ansible.builtin.copy": {"content": expr, "dest": str(out)}}], play_vars)
    assert r.returncode == 0, r.stdout + r.stderr
    got = [(item["from_service"], item["read_keys"]) for item in json.loads(out.read_text())]
    assert got == expected


# -- The render check --------------------------------------------------------------------------------


def _mutate_routes(text: str, old: str, new: str) -> str:
    head, tail = text.split("\nroutes:\n", 1)
    assert old in tail
    return head + "\nroutes:\n" + tail.replace(old, new, 1)


def test_the_render_check_passes_for_the_declared_route(tmp_path):
    d = tmp_path / "render"
    d.mkdir()
    r = _check(d, render_text(d, **ROUTE_HV), ROUTE_HV)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_render_check_refuses_a_route_with_no_strict_key_policy(tmp_path):
    d = tmp_path / "render"
    d.mkdir()
    text = _mutate_routes(render_text(d, **ROUTE_HV), "      mode: strict", "      mode: loose")
    r = _check(d, text, ROUTE_HV)
    assert r.returncode != 0 and "no strict apiKey policy" in r.stdout + r.stderr


def test_the_render_check_refuses_a_route_the_flag_does_not_want(tmp_path):
    d = tmp_path / "render"
    d.mkdir()
    r = _check(d, render_text(d, **ROUTE_HV), {**ROUTE_HV, "agw_skynet_route_enabled": False})
    assert r.returncode != 0 and "agw_skynet_route_enabled is False" in r.stdout + r.stderr


def test_the_render_check_names_a_missing_route_identity(tmp_path):
    d = tmp_path / "render"
    d.mkdir()
    rendered = render_text(d, **ROUTE_HV)
    r = _check(d, rendered, {**ROUTE_HV, "agw_skynet_route_identities": ["skynet", "alice"]})
    assert r.returncode != 0 and "missing alice" in r.stdout + r.stderr


def test_the_render_check_names_an_extra_route_identity(tmp_path):
    d = tmp_path / "render"
    d.mkdir()
    rendered = render_text(d, **ROUTE_HV)
    r = _check(d, rendered, {**ROUTE_HV, "agw_skynet_route_identities": []})
    assert r.returncode != 0 and "not allowed skynet" in r.stdout + r.stderr


# -- On the pinned image -----------------------------------------------------------------------------

live = pytest.mark.skipif(not _image_present(), reason=f"podman or {IMAGE} not available")


@live
def test_the_render_passes_the_pinned_images_validator(tmp_path):
    render_text(tmp_path, **ROUTE_HV)
    env = tmp_path / "gateway.env"
    env.write_text(f"VLLM_API_KEY=v\nSKYNET_API_TOKEN={TOKEN}\nAGW_DATABASE_URL=postgresql://db/x\n")
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


class Backend(DrainingHandler):
    """A stand-in skynet: records the method, path and Authorization header of every request."""

    seen: list = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _reply(self):
        type(self).seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization")})
        data = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _reply
    do_POST = _reply


def _call(port: int, method: str, path: str, key: str | None = None, body: dict | None = None) -> int:
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@live
def test_the_route_admits_only_its_identity_and_skynet_sees_only_its_own_token(tmp_path):
    """The real render, with only the backend pointed at a local stand-in and the database dropped (the
    route's policy has no budget, and the llm keys' budgets need a database this check does not start)."""
    Backend.seen = []
    server = ThreadingHTTPServer(("0.0.0.0", 0), Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    name = f"agw-skynet-{_free_port()}"
    try:
        cfg = render(tmp_path, **{**ROUTE_HV, "agw_skynet_backend": f"host.containers.internal:{server.server_port}"})
        cfg["config"] = {"adminAddr": "127.0.0.1:15000"}
        for entry in cfg["llm"]["policies"]["apiKey"]["keys"]:
            entry.pop("budgets", None)
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
                f"SKYNET_API_TOKEN={TOKEN}",
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
        assert _call(port, "GET", "/skynet/healthz") == 401  # keyless: refused at the gateway
        assert _call(port, "GET", "/skynet/healthz", STORE["client_alice"]) == 401  # enrolled, not a route identity
        assert Backend.seen == []  # neither refused call reached skynet
        assert _call(port, "GET", "/skynet/healthz", STORE["client_skynet"]) == 200
        assert _call(port, "POST", "/skynet/runs", STORE["client_skynet"], {"x": 1}) == 200
        assert [s["path"] for s in Backend.seen] == ["/skynet/healthz", "/skynet/runs"]  # no rewrite
        assert {s["auth"] for s in Backend.seen} == {f"Bearer {TOKEN}"}  # the caller's key never arrives
    finally:
        subprocess.run(["podman", "rm", "-f", name], capture_output=True)
        server.shutdown()
        server.server_close()


# -- The backend receives skynet's bearer over plain HTTP: it must be loopback or private --------------
TRANSPORT_TASK = "Refuse a cleartext skynet route backend outside the private network"


def _transport_task() -> dict:
    task = dict(_task(PHASE1, TRANSPORT_TASK))
    task["ansible.builtin.include_tasks"] = str(REPO / "platform/playbooks" / task["ansible.builtin.include_tasks"])
    return task


def test_the_transport_check_runs_before_secrets_and_only_while_the_route_is_on():
    names = [t.get("name") for t in PHASE1["tasks"]]
    assert names.index(TRANSPORT_TASK) < names.index("Manage secrets and render env + config")
    task = _task(PHASE1, TRANSPORT_TASK)
    assert task["ansible.builtin.include_tasks"] == "tasks/assert-bao-transport.yml"
    assert task["vars"]["_assert_bao_url"] == "http://{{ agw_skynet_backend }}"
    assert task["when"] == "agw_skynet_route_enabled | default(false) | bool"


@pytest.mark.parametrize(
    "backend, local, passes",
    [
        ("skynet.example.com:8080", False, False),  # a public name: the bearer would cross in cleartext
        (".".join(["10", "20", "30", "40"]) + ":8080", False, True),  # synthetic RFC1918 address, built so no literal sits in the repo
        ("127.0.0.1:8080", False, True),  # loopback
        ("skynet:8080", True, True),  # a container name on the local podman network
        ("skynet:8080", False, False),  # a bare name outside local-dev can resolve to a public FQDN
    ],
)
def test_a_cleartext_backend_is_refused_unless_private(tmp_path, backend, local, passes):
    hv = {"agw_skynet_route_enabled": True, "agw_skynet_backend": backend, "local_mode": local}
    r = _ansible(tmp_path, hv, [_transport_task()])
    assert (r.returncode == 0) is passes, r.stdout[-2000:]
