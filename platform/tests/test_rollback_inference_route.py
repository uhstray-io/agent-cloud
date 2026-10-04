"""rollback-inference-route.yml (change inference-gateway-agentgateway, task 4.6).

The playbook runs for real on localhost: both inventory hosts use the local connection, a
synthetic OpenBao answers its requests, a stub container engine stands in for Caddy and the
gateway's database container, and the Caddy step is the real manage-caddy-sites.yml editing a
Caddyfile in the test directory. The task text asks for the three modes to exist and for
`direct` never to print a key; both are asserted on runs, not on source text, together with
the idempotency each mode promises. Runs through harness_sandbox, so no write leaves the test
directory. All values are synthetic.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import fake_http
import harness_sandbox
import playbook_yaml
import pytest
import seed_harness
import yaml

PLAYBOOK = playbook_yaml.REPO / "platform/playbooks/rollback-inference-route.yml"
SITES_LIB = str(playbook_yaml.REPO / "platform/services/caddy/deployment/lib")
LIVE = "synthetic-vllm-live-key-1"
ROTATED = "synthetic-vllm-rotated-key-2"
GATEWAY = "gw.example.test:4000"
HEAD = "vllm-head.example.test:8000"
ADDRESS = "inference.example.test"
MARK = "# {} ANSIBLE MANAGED — agent-cloud extra sites"

# The site-config shape the playbook's header asks for: the inference block selects its
# upstream on inference_route_mode, and the gateway hop carries its own transport block.
SITES = """\
{{ inference_route_address }} {
	@api path /v1/*
	handle @api {
{% if inference_route_mode | default('gateway') == 'direct' %}
		reverse_proxy vllm-head.example.test:8000 {
			flush_interval -1
		}
{% else %}
		reverse_proxy https://gw.example.test:4000 {
			flush_interval -1
			transport http {
				tls_server_name gw.example.test
			}
		}
{% endif %}
	}
	handle /health {
{% if inference_route_mode | default('gateway') == 'direct' %}
		reverse_proxy vllm-head.example.test:8000
{% else %}
		reverse_proxy https://gw.example.test:4000
{% endif %}
	}
	handle {
		respond 404
	}
}
"""

# The stub keeps Caddy's RUNNING config apart from the file: `restart` loads the file into
# live.json (shaped as `caddy adapt` v2.11.4 renders a reverse_proxy route: the host match,
# then each upstream as a `dial` of host:port), and the admin API serves live.json. With
# `restart_noop` the process keeps its old routes, so the file and the process can diverge.
ENGINE = r"""#!/usr/bin/env python3
import json, os, re, shutil, sys
from pathlib import Path
tmp = Path(os.environ["STUB_DIR"])
sys.path.insert(0, os.environ["STUB_SITES_LIB"])
import caddyfile_sites
state = json.loads((tmp / "stub.json").read_text())
a = sys.argv[1:]
with (tmp / "calls").open("a") as f:
    f.write(" ".join(a) + "\n")

def load():
    routes = []
    for site in caddyfile_sites.parse_sites((tmp / "caddy" / "Caddyfile").read_text()):
        dials = [re.sub(r"/.*$", "", re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", u)) for u in site["upstreams"]]
        routes.append({"match": [{"host": site["addresses"]}], "handle": [{"handler": "subroute", "routes": [
            {"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": d}]}]} for d in dials]}]})
    (tmp / "live.json").write_text(json.dumps({"apps": {"http": {"servers": {"srv0": {"routes": routes}}}}}))

if a[0] == "load":
    load()
elif a[0] == "cp":
    shutil.copy(a[1], tmp / "validate.caddyfile")
elif a[0] == "restart":
    if not state.get("restart_noop"):
        load()
elif a[:2] == ["exec", "caddy"] and "validate" in a:
    sys.exit(1 if state.get("caddy_invalid") else 0)
elif a[:2] == ["exec", "caddy"]:
    print((tmp / "live.json").read_text())
elif a[:2] == ["exec", "agentgateway-db"]:
    sys.exit(0 if state.get("gateway_ready") else 1)
elif a[0] == "inspect" and a[-1] == "agentgateway":
    if not state.get("gateway_key"):
        sys.exit("no such container")
    print(json.dumps(["PATH=/usr/bin", "VLLM_API_KEY=" + state["gateway_key"]]))
else:
    sys.exit("unexpected engine call: " + " ".join(a))
"""

# deploy.sh recreates the gateway: its running environment becomes the rendered one.
DEPLOY = """#!/usr/bin/env bash
echo deploy >> "$STUB_DIR/calls"
python3 - <<'PY'
import json, os, re
from pathlib import Path
d = Path(os.environ["STUB_DIR"])
s = json.loads((d / "stub.json").read_text())
s["gateway_ready"] = True
s["gateway_key"] = re.search(r"(?m)^VLLM_API_KEY=(.*)$", (d / "gw" / ".env").read_text()).group(1)
(d / "stub.json").write_text(json.dumps(s))
PY
"""


class Bao(seed_harness.FakeBao):
    """KV-v2 for one path: versioned, check-and-set honoured, every write logged to the
    shared event file so its order against the Caddy calls can be asserted. `rotate_on_get`
    rotates the key just before the Nth read (a rotation landing between two reads);
    `bump_before_cas` makes another writer win the race before a check-and-set write."""

    store: dict = {}
    version = 1
    events: Path | None = None
    rotate_on_get = 0
    bump_before_cas = False
    gets = 0

    def event(self, text):
        with type(self).events.open("a") as f:
            f.write(text + "\n")

    def do_POST(self):
        self.record("POST")
        cls = type(self)
        if self.path == "/v1/auth/approle/login":
            return self.reply({"auth": {"client_token": seed_harness.LOGIN}})
        if self.path == "/v1/secret/data/services/agentgateway":
            body = self.body()
            if cls.bump_before_cas:
                cls.version += 1
            if body.get("options", {}).get("cas") != cls.version:
                return self.reply({"errors": ["check-and-set parameter did not match"]}, 400)
            cls.store = dict(body["data"])
            cls.version += 1
            self.event("bao write")
            return self.reply({"data": {"version": cls.version}})
        self.reply({}, 404)

    def do_GET(self):
        self.record("GET")
        cls = type(self)
        if self.path == "/v1/secret/data/services/agentgateway":
            cls.gets += 1
            if cls.gets == cls.rotate_on_get:
                cls.store["vllm_api_key"] = ROTATED
                cls.version += 1
            return self.reply({"data": {"data": cls.store, "metadata": {"version": cls.version}}})
        self.reply({}, 404)

    def do_PATCH(self):
        self.record("PATCH")
        cls = type(self)
        for key, value in self.body()["data"].items():
            if value is None:
                cls.store.pop(key, None)
            else:
                cls.store[key] = value
        cls.version += 1
        self.event("bao write")
        self.reply({"data": {}})


def _patches():
    """Every request that wrote the store (a merge-patch, or a check-and-set POST)."""
    return [r for r in Bao.requests if r[0] == "PATCH" or (r[0] == "POST" and r[1].startswith("/v1/secret/"))]


def _load_live(tmp):
    """Make Caddy's running config the current file, as a start would (logged nowhere)."""
    subprocess.run([str(tmp / "engine"), "load"], env={**os.environ, "STUB_DIR": str(tmp), "STUB_SITES_LIB": SITES_LIB},
                   check=True)
    (tmp / "calls").unlink()


def _state(tmp, **changes):
    path = tmp / "stub.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))


def _caddyfile(mode: str) -> str:
    """A Caddyfile whose managed region already serves the route in `mode`, as a real
    manage-caddy-sites run leaves it (the block text is what the inventory renders)."""
    block = SITES.replace("{{ inference_route_address }}", ADDRESS)
    lines, keep = [], True
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("{% if"):
            keep = mode == "direct"
            continue
        if s.startswith("{% else"):
            keep = mode != "direct"
            continue
        if s.startswith("{% endif"):
            keep = True
            continue
        if keep:
            lines.append(line)
    body = "\n".join(lines)
    return ("{\n\temail ops@example.test\n}\n\nother.example.test {\n\treverse_proxy other.example.test:80\n}\n\n"
            f"{MARK.format('BEGIN')}\n{body}\n{MARK.format('END')}\n")


class Route(fake_http.DrainingHandler):
    """The inference route as a client reaches it: manage-caddy-sites probes it after the edit
    (caddy_probe_url), and the rollback fails unless it answers below 500."""

    status = 200

    def do_GET(self):
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def route():
    Route.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), Route)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def env(tmp_path, route):
    (tmp_path / "caddy").mkdir()
    (tmp_path / "caddy" / "Caddyfile").write_text(_caddyfile("gateway"))
    gw = tmp_path / "gw"
    gw.mkdir()
    (gw / "config.yaml").write_text("config: current\n")
    (gw / ".env").write_text(f"AGW_X=1\nVLLM_API_KEY={LIVE}\n")
    (gw / "deploy.sh").write_text(DEPLOY)
    (gw / "gateway-addr.sh").write_text("#!/usr/bin/env bash\necho gw-addr.example.test\n")
    (gw / "gateway-addr.sh").chmod(0o755)
    stub = tmp_path / "engine"
    stub.write_text(f"#!{sys.executable}\n" + ENGINE.split("\n", 1)[1])
    stub.chmod(0o755)
    (tmp_path / "stub.json").write_text(json.dumps({"gateway_ready": True, "gateway_key": LIVE}))
    _load_live(tmp_path)
    Bao.requests, Bao.version, Bao.events, Bao.gets = [], 1, tmp_path / "calls", 0
    Bao.rotate_on_get, Bao.bump_before_cas = 0, False
    Bao.store = {"vllm_api_key": LIVE, "client_stray": "synthetic-client-stray", "client_pi": "synthetic-client-pi",
                 "agw_db_password": "synthetic-db"}
    with seed_harness.serve(Bao) as address:
        yield tmp_path, address, route


def _children(tmp, caddy=None, gateway=None, probe_url=None) -> dict:
    """The inventory groups; an override of None removes that variable."""
    common = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
              "container_engine": str(tmp / "engine")}
    caddy_host = {**common, "caddy_caddyfile_path": str(tmp / "caddy" / "Caddyfile"), "caddy_container": "caddy",
                  "caddy_managed_sites": SITES, "caddy_probe_host": ADDRESS, "caddy_probe_url": probe_url,
                  "inference_route_address": ADDRESS,
                  "inference_route_gateway_upstream": GATEWAY, **(caddy or {})}
    gw_host = {**common, "service_name": "agentgateway", "local_monorepo_dir": str(tmp), "monorepo_deploy_path": "gw",
               "agw_clients": ["stray", "pi"], "agw_upstream_base_url": f"http://{HEAD}/v1", **(gateway or {})}
    return {"caddy_svc": {"hosts": {"c": {k: v for k, v in caddy_host.items() if v is not None}}},
            "agentgateway_svc": {"hosts": {"g": {k: v for k, v in gw_host.items() if v is not None}}}}


def _run(env, mode=None, check=False, tags=None, caddy=None, gateway=None, groups=None):
    tmp, address, probe_url = env
    children = groups if groups is not None else _children(tmp, caddy, gateway, probe_url)
    inventory = {"all": {"vars": {"openbao_addr": address}, "children": children}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inventory, allow_unicode=True))
    extra = {**seed_harness.ROLE, **({"mode": mode} if mode else {})}
    # -v prints every task result that no_log does not censor, so a value that escapes it shows.
    cmd = ["ansible-playbook", "-v", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), "-e", json.dumps(extra),
           *(["--check"] if check else []), *(["--tags", tags] if tags else [])]
    run_env = {**harness_sandbox.env_for(tmp), "STUB_DIR": str(tmp), "STUB_SITES_LIB": SITES_LIB,
               "ANSIBLE_STDOUT_CALLBACK": "default"}
    r = harness_sandbox.run(cmd, tmp, cwd=playbook_yaml.REPO, env=run_env)
    out = r.stdout + r.stderr
    for value in (LIVE, ROTATED, *seed_harness.NEVER_PRINTED, "synthetic-client-stray", "synthetic-db"):
        assert value not in out, f"{value} printed"
    return r.returncode, out


def _route(tmp) -> str:
    text = (tmp / "caddy" / "Caddyfile").read_text()
    return text[text.index(MARK.format("BEGIN")):]


def _calls(tmp, prefix):
    calls = tmp / "calls"
    return [c for c in (calls.read_text().splitlines() if calls.exists() else []) if c.startswith(prefix)]


def _unchanged(out):
    """Every host in the PLAY RECAP reports no change."""
    recap = [line for line in out.splitlines() if " : ok=" in line]
    return bool(recap) and all("changed=0 " in line for line in recap)


def test_the_three_modes_exist_and_anything_else_is_refused(env):
    tmp = env[0]
    plays = playbook_yaml.load(PLAYBOOK)
    assert "['gateway-config', 'direct', 'restore']" in plays[0]["tasks"][0]["ansible.builtin.assert"]["that"]
    rc, out = _run(env, mode="sideways")
    assert rc != 0 and "takes gateway-config (default), direct or restore" in out, out
    assert _patches() == [] and not _calls(tmp, "restart")


def test_direct_publishes_the_live_key_then_routes_to_vllm_and_never_prints_it(env):
    tmp = env[0]
    Bao.store["direct_retired"] = LIVE  # a copy for a name no longer in agw_clients
    rc, out = _run(env, mode="direct")
    assert rc == 0, out
    assert Bao.store["direct_stray"] == LIVE and Bao.store["direct_pi"] == LIVE
    assert "direct_retired" not in Bao.store and Bao.store["client_stray"] == "synthetic-client-stray"
    route = _route(tmp)
    assert HEAD in route and GATEWAY not in route
    assert len(_calls(tmp, "restart caddy")) == 1
    assert f"{ADDRESS} dials {HEAD} only (running config)." in out

    # Re-run: nothing published twice, nothing restarted.
    before = len(_patches())
    rc, out = _run(env, mode="direct")
    assert rc == 0 and len(_patches()) == before, out
    assert "already published" in out and len(_calls(tmp, "restart caddy")) == 1 and _unchanged(out), out


def test_restore_routes_back_but_keeps_copies_that_still_hold_the_live_key(env):
    tmp = env[0]
    assert _run(env, mode="direct")[0] == 0
    rc, out = _run(env, mode="restore")
    assert rc != 0 and "still hold the" in out and "LIVE vLLM key" in out, out
    route = _route(tmp)
    assert GATEWAY in route and HEAD not in route  # the route is back on the gateway first
    assert Bao.store["direct_stray"] == LIVE and Bao.store["direct_pi"] == LIVE


def test_restore_after_rotation_waits_for_the_gateway_to_hold_the_new_key(env):
    assert _run(env, mode="direct")[0] == 0
    Bao.store["vllm_api_key"] = ROTATED  # rotated at vLLM and in OpenBao; gateway not redeployed
    rc, out = _run(env, mode="restore")
    assert rc != 0 and "still holds the previous one" in out, out
    assert "direct_stray" in Bao.store and "direct_pi" in Bao.store


def test_restore_waits_for_the_running_gateway_not_just_the_rendered_file(env):
    tmp = env[0]
    assert _run(env, mode="direct")[0] == 0
    Bao.store["vllm_api_key"] = ROTATED
    (tmp / "gw" / ".env").write_text(f"VLLM_API_KEY={ROTATED}\n")  # rendered, container not recreated
    rc, out = _run(env, mode="restore")
    assert rc != 0 and "the running agentgateway" in out and "container does not" in out, out
    assert "direct_stray" in Bao.store and "direct_pi" in Bao.store


def test_restore_after_rotation_and_redeploy_removes_every_copy_then_is_a_no_op(env):
    tmp = env[0]
    assert _run(env, mode="direct")[0] == 0
    Bao.store["vllm_api_key"] = ROTATED
    (tmp / "gw" / ".env").write_text(f"AGW_X=1\nVLLM_API_KEY={ROTATED}\n")
    _state(tmp, gateway_key=ROTATED)  # the gateway was redeployed on the rotated key
    rc, out = _run(env, mode="restore")
    assert rc == 0, out
    assert not [k for k in Bao.store if k.startswith("direct_")]
    assert GATEWAY in _route(tmp) and HEAD not in _route(tmp)

    before, restarts = len(_patches()), len(_calls(tmp, "restart caddy"))
    rc, out = _run(env, mode="restore")
    assert rc == 0 and "restore is complete" in out, out
    assert len(_patches()) == before and len(_calls(tmp, "restart caddy")) == restarts and _unchanged(out), out


def test_direct_writes_the_store_before_caddy_is_touched(env):
    tmp = env[0]
    assert _run(env, mode="direct")[0] == 0
    events = (tmp / "calls").read_text().splitlines()
    first_caddy = next(i for i, e in enumerate(events) if e.startswith(("cp ", "restart caddy")))
    writes = [i for i, e in enumerate(events) if e == "bao write"]
    assert writes and max(writes) < first_caddy, events


@pytest.mark.parametrize("probe", ["status", "dns"])
def test_a_route_that_does_not_answer_fails_the_rollback(env, probe):
    # PR 417 review: manage-caddy-sites only RECORDS its edge verdict, so the rollback must
    # consume it, or a route that answers 502 (or does not resolve) leaves the rollback green.
    tmp = env[0]
    if probe == "status":
        Route.status = 502
        rc, out = _run(env, mode="direct")
        assert rc != 0 and "answered 502" in out, out[-3000:]
    else:
        rc, out = _run(env, mode="direct", caddy={"caddy_probe_url": "https://no-such-host.invalid/"})
        assert rc != 0 and "no-such-host.invalid does not resolve" in out, out[-3000:]
    assert "The inference route failed its edge check" in out
    # the store was written before Caddy, as in any direct run; no copy is retired by a failed route
    assert HEAD in _route(tmp)


def test_a_running_caddy_that_keeps_the_old_route_fails_the_run(env):
    tmp = env[0]
    _state(tmp, restart_noop=True)  # the file changes; the process does not load it
    rc, out = _run(env, mode="direct")
    assert rc != 0 and "The file and the process disagree" in out, out
    assert HEAD in _route(tmp)  # the file read-back alone would have passed


def test_a_rotation_between_the_read_and_the_publication_publishes_nothing(env):
    tmp = env[0]
    Bao.rotate_on_get = 2  # the merge's own fetch sees a key other than the one read
    rc, out = _run(env, mode="direct")
    assert rc != 0 and "guarded OpenBao key changed" in out, out
    assert _patches() == [] and not [k for k in Bao.store if k.startswith("direct_")]
    assert not _calls(tmp, "restart") and GATEWAY in _route(tmp)


def test_a_write_racing_the_publication_publishes_nothing(env):
    tmp = env[0]
    Bao.bump_before_cas = True  # another writer lands between the fetch and the write
    rc, out = _run(env, mode="direct")
    assert rc != 0 and "version-guarded update" in out, out
    assert not [k for k in Bao.store if k.startswith("direct_")] and not _calls(tmp, "restart")


def test_a_caddy_failure_in_restore_retires_no_copy(env):
    tmp = env[0]
    assert _run(env, mode="direct")[0] == 0
    Bao.store["vllm_api_key"] = ROTATED
    (tmp / "gw" / ".env").write_text(f"VLLM_API_KEY={ROTATED}\n")
    _state(tmp, gateway_key=ROTATED, caddy_invalid=True)
    rc, out = _run(env, mode="restore")
    assert rc != 0 and "rolled back" in out, out
    assert HEAD in _route(tmp) and "direct_stray" in Bao.store and "direct_pi" in Bao.store


def test_a_route_declaration_that_ignores_the_mode_is_refused_before_any_write(env):
    tmp = env[0]
    hardcoded = SITES.replace("inference_route_mode | default('gateway') == 'direct'", "false")
    before = _route(tmp)
    rc, out = _run(env, mode="direct", caddy={"caddy_managed_sites": hardcoded})
    assert rc != 0 and "must select its upstream on inference_route_mode" in out, out
    assert _patches() == [] and _route(tmp) == before and not _calls(tmp, "restart")


def test_a_dry_run_of_direct_writes_nothing(env):
    tmp = env[0]
    before = (tmp / "caddy" / "Caddyfile").read_text()
    rc, out = _run(env, mode="direct", check=True)
    assert rc == 0, out
    assert _patches() == [] and (tmp / "caddy" / "Caddyfile").read_text() == before
    assert not _calls(tmp, "restart") and not _calls(tmp, "cp ")
    assert "keys that would be set: direct_stray, direct_pi" in out


def test_verify_reads_the_state_and_changes_nothing(env):
    tmp = env[0]
    rc, out = _run(env, mode="direct", tags="verify")
    assert rc != 0 and f"expects every upstream to be {HEAD}" in out, out  # not rolled back yet
    assert _patches() == [] and not _calls(tmp, "restart")
    assert _run(env, mode="direct")[0] == 0
    before, restarts = len(_patches()), len(_calls(tmp, "restart"))
    rc, out = _run(env, mode="direct", tags="verify")
    assert rc == 0 and "published direct-path copies: direct_pi, direct_stray" in out, out
    assert len(_patches()) == before and len(_calls(tmp, "restart")) == restarts


def test_gateway_config_puts_the_previous_config_back_and_leaves_caddy_alone(env):
    tmp = env[0]
    (tmp / "gw" / "config.yaml.previous").write_text("config: previous\n")
    # A route on the direct path stays there: this mode never touches Caddy.
    (tmp / "caddy" / "Caddyfile").write_text(_caddyfile("direct"))
    caddy_before = (tmp / "caddy" / "Caddyfile").read_text()
    rc, out = _run(env)  # gateway-config is the default
    assert rc == 0, out
    assert (tmp / "gw" / "config.yaml").read_text() == "config: previous\n"
    assert len(_calls(tmp, "deploy")) == 1 and "gateway recreated and ready" in out
    assert (tmp / "caddy" / "Caddyfile").read_text() == caddy_before and not _calls(tmp, "restart")
    assert _patches() == []

    rc, out = _run(env, mode="gateway-config")
    assert rc == 0 and len(_calls(tmp, "deploy")) == 1 and "already in place" in out and _unchanged(out), out


def test_gateway_config_recreates_a_gateway_left_down_by_an_interrupted_run(env):
    tmp = env[0]
    (tmp / "gw" / "config.yaml.previous").write_text("config: current\n")  # already copied
    _state(tmp, gateway_ready=False)
    rc, out = _run(env, mode="gateway-config")
    assert rc == 0 and len(_calls(tmp, "deploy")) == 1, out


def test_gateway_config_without_a_previous_config_is_refused(env):
    tmp = env[0]
    rc, out = _run(env, mode="gateway-config")
    assert rc != 0 and "config.yaml.previous does not exist" in out, out
    assert (tmp / "gw" / "config.yaml").read_text() == "config: current\n" and not _calls(tmp, "deploy")


def _cfg(*keys) -> str:
    """A rendered config enrolling (name, hash) pairs where the template puts them."""
    return yaml.safe_dump({"llm": {"policies": {"apiKey": {"mode": "strict", "keys": [
        {"keyHash": f"sha256:{h}", "metadata": {"name": n}} for n, h in keys]}}}})


def _hash_never_printed(out):
    for h in ("aaaa1111", "bbbb2222", "cccc3333", "dddd4444"):
        assert h not in out, f"{h} printed"


def test_gateway_config_refuses_a_previous_that_enrols_a_rotated_or_revoked_key(env):
    tmp = env[0]
    live = _cfg(("stray", "aaaa1111"), ("pi", "bbbb2222"))
    (tmp / "gw" / "config.yaml").write_text(live)
    # stray's old hash (rotated) and old-laptop (revoked) are enrolled only in the kept copy.
    (tmp / "gw" / "config.yaml.previous").write_text(
        _cfg(("stray", "cccc3333"), ("pi", "bbbb2222"), ("old-laptop", "dddd4444")))
    for check in (False, True):
        rc, out = _run(env, mode="gateway-config", check=check)
        assert rc != 0 and "rotated, revoked or never enrolled now): old-laptop, stray" in out, out
        _hash_never_printed(out)
        assert (tmp / "gw" / "config.yaml").read_text() == live and not _calls(tmp, "deploy")


def test_gateway_config_allows_a_previous_whose_identities_the_live_config_enrols(env):
    tmp = env[0]
    live = _cfg(("stray", "aaaa1111"), ("pi", "bbbb2222"))
    (tmp / "gw" / "config.yaml").write_text(live)
    previous = _cfg(("stray", "aaaa1111"))  # a removed client is fine: fewer keys, not more
    (tmp / "gw" / "config.yaml.previous").write_text(previous)
    rc, out = _run(env, mode="gateway-config", check=True)  # check mode passes the guard, writes nothing
    assert rc == 0 and not _calls(tmp, "deploy"), out
    assert (tmp / "gw" / "config.yaml").read_text() == live
    rc, out = _run(env, mode="gateway-config")
    assert rc == 0, out
    _hash_never_printed(out)
    assert (tmp / "gw" / "config.yaml").read_text() == previous and len(_calls(tmp, "deploy")) == 1


def test_gateway_config_refuses_a_previous_without_a_live_config_to_compare(env):
    tmp = env[0]
    (tmp / "gw" / "config.yaml").unlink()
    (tmp / "gw" / "config.yaml.previous").write_text(_cfg(("stray", "aaaa1111")))
    rc, out = _run(env, mode="gateway-config")
    assert rc != 0 and "never enrolled now): stray" in out, out
    assert not (tmp / "gw" / "config.yaml").exists()


@pytest.mark.parametrize(("expires", "refused"), [("2026-10-01", True), ("2099-01-01", False)])
def test_gateway_config_refuses_legacy_shared_after_its_grace_period(env, expires, refused):
    tmp = env[0]
    both = _cfg(("stray", "aaaa1111"), ("legacy-shared", "bbbb2222"))
    (tmp / "gw" / "config.yaml").write_text(both)
    (tmp / "gw" / "config.yaml.previous").write_text(both.replace("mode: strict", "mode: strict  # kept"))
    rc, out = _run(env, mode="gateway-config",
                   gateway={"legacy_shared_expires": expires, "agw_today": "2026-10-03"})
    assert (rc != 0) == refused, out
    assert ("legacy-shared, whose grace period (legacy_shared_expires) has ended" in out) == refused, out


def _refused(env, *names):
    tmp = env[0]
    live = (tmp / "gw" / "config.yaml").read_text() if (tmp / "gw" / "config.yaml").exists() else None
    rc, out = _run(env, mode="gateway-config")
    assert rc != 0 and f"never enrolled now): {', '.join(names)}." in out, out
    _hash_never_printed(out)
    assert not _calls(tmp, "deploy")
    assert ((tmp / "gw" / "config.yaml").read_text() if live is not None else None) == live


def test_gateway_config_refuses_a_plaintext_key_the_live_config_does_not_enrol(env):
    """local-dev agw_plaintext_keys renders `key:` (the value), not `keyHash:`."""
    tmp = env[0]
    (tmp / "gw" / "config.yaml").write_text(yaml.safe_dump({"llm": {"policies": {"apiKey": {"keys": [
        {"key": "aaaa1111", "metadata": {"name": "stray"}}]}}}}))
    (tmp / "gw" / "config.yaml.previous").write_text(yaml.safe_dump({"llm": {"policies": {"apiKey": {"keys": [
        {"key": "aaaa1111", "metadata": {"name": "stray"}}, {"key": "bbbb2222", "metadata": {"name": "pi"}}]}}}}))
    _refused(env, "pi")


def test_gateway_config_sees_through_anchors_aliases_and_merge_keys(env):
    tmp = env[0]
    (tmp / "gw" / "config.yaml").write_text(_cfg(("stray", "aaaa1111")))
    (tmp / "gw" / "config.yaml.previous").write_text(
        "x-base: &base\n  keyHash: sha256:aaaa1111\n  metadata: {name: stray}\n"
        "x-extra: &extra {keyHash: 'sha256:cccc3333', metadata: {name: ghost}}\n"
        "llm:\n  policies:\n    apiKey:\n      keys:\n"
        "        - *base\n        - *extra\n"
        "        - <<: *base\n          keyHash: sha256:dddd4444\n          metadata: {name: merged}\n")
    _refused(env, "ghost", "merged")


def test_gateway_config_counts_every_identity_new_when_live_enrols_none(env):
    tmp = env[0]
    (tmp / "gw" / "config.yaml").write_text(yaml.safe_dump({"llm": {"policies": {"localRateLimit": []}}}))
    (tmp / "gw" / "config.yaml.previous").write_text(_cfg(("stray", "aaaa1111"), ("pi", "bbbb2222")))
    _refused(env, "pi", "stray")


def test_gateway_config_reads_apikey_enrolments_outside_the_llm_policy(env):
    """A kept file is whatever was on disk: keys under a route policy count too."""
    tmp = env[0]
    (tmp / "gw" / "config.yaml").write_text(_cfg(("stray", "aaaa1111")))
    previous = yaml.safe_load(_cfg(("stray", "aaaa1111")))
    previous["binds"] = [{"listeners": [{"routes": [{"policies": {"apiKey": {"keys": [
        {"keyHash": "sha256:cccc3333", "metadata": {"name": "sneaky"}}]}}}]}]}]
    (tmp / "gw" / "config.yaml.previous").write_text(yaml.safe_dump(previous))
    _refused(env, "sneaky")


TEMPLATE = playbook_yaml.REPO / "platform/services/agentgateway/deployment/templates/config.yaml.j2"


def _render(tmp, dest, clients, **hv):
    """Render the repo's real config.yaml.j2, as the deploy does."""
    secrets = {"vllm_api_key": "synthetic-vllm-x",
               **{f"client_{c}": f"synthetic-k-{c}-{v}" for c, v in clients.items()}}
    vars_ = {"agw_clients": list(clients), "agw_models": [{"name": "m"}], "agw_upstream_base_url": "http://u.invalid/v1",
             "secrets": secrets, **hv}
    (tmp / "rv.json").write_text(json.dumps(vars_))
    (tmp / "render.yml").write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "tasks": [
        {"ansible.builtin.template": {"src": str(TEMPLATE), "dest": str(dest), "mode": "0644"}}]}]))
    r = harness_sandbox.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(tmp / "render.yml"),
                             "-e", f"@{tmp / 'rv.json'}"],
                            tmp, cwd=playbook_yaml.REPO, env=harness_sandbox.env_for(tmp))
    assert r.returncode == 0, r.stdout + r.stderr


def _template_enrolments(path):
    """Every apiKey keys entry in a rendered file, found independently of the playbook."""
    found = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "apiKey" and isinstance(v, dict) and isinstance(v.get("keys"), list):
                    found.extend(e["metadata"]["name"] for e in v["keys"])
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(yaml.safe_load(path.read_text()))
    return sorted(found)


@pytest.mark.parametrize("hv", [{}, {"agw_ui_enabled": False},
                                {"legacy_shared_expires": "2099-01-01", "agw_today": "2026-10-03",
                                 "_legacy_shared_active": True}])
def test_gateway_config_reads_every_enrolment_the_real_template_renders(env, hv):
    tmp = env[0]
    live, prev = tmp / "gw" / "config.yaml", tmp / "gw" / "config.yaml.previous"
    _render(tmp, prev, {"stray": 1, "pi": 1, "old-laptop": 1}, **hv)
    expected = ["old-laptop", "pi", "stray"] + (["legacy-shared"] if hv.get("legacy_shared_expires") else [])
    assert _template_enrolments(prev) == sorted(expected)
    # A rotation of stray and a revocation of old-laptop, rendered for real.
    _render(tmp, live, {"stray": 2, "pi": 1}, **hv)
    rc, out = _run(env, mode="gateway-config", gateway=hv)
    assert rc != 0 and "never enrolled now): old-laptop, stray." in out, out
    assert "synthetic-k-" not in out
    # The same render on both sides is admitted.
    _render(tmp, prev, {"stray": 2, "pi": 1}, **hv)
    rc, out = _run(env, mode="gateway-config", gateway=hv)
    assert rc == 0, out


@pytest.mark.parametrize("group", ["caddy_svc", "agentgateway_svc"])
def test_a_group_that_matches_no_hosts_fails(env, group):
    tmp = env[0]
    children = _children(tmp)
    children[group] = {"hosts": {}}
    rc, out = _run(env, mode="direct", groups=children)
    assert rc != 0 and f"Inventory group '{group}' is absent or empty" in out, out
    assert _patches() == [] and not _calls(tmp, "restart")


def test_the_gateway_upstream_defaults_to_the_gateway_bind_and_port(env):
    tmp = env[0]
    sites = SITES.replace("gw.example.test:4000", "gw-bind.example.test:4100")
    (tmp / "caddy" / "Caddyfile").write_text(_caddyfile("gateway").replace("gw.example.test:4000",
                                                                            "gw-bind.example.test:4100"))
    _load_live(tmp)
    rc, out = _run(env, mode="restore", caddy={"caddy_managed_sites": sites, "inference_route_gateway_upstream": None},
                   gateway={"agw_bind": "gw-bind.example.test", "agw_port": "4100"})
    assert rc == 0 and f"{ADDRESS} dials gw-bind.example.test:4100 only (running config)." in out, out
