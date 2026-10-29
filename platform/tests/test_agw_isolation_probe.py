"""probe-agentgateway-isolation.yml: encoded proof of the spec scenario "One client cannot exceed its
share" (inference-gateway-agentgateway task 4.5).

The playbook runs for real on localhost through harness_sandbox, against a synthetic OpenBao that
holds one client key per identity and a stub gateway that charges a rolling token budget after each
completion, the way the real one does: the request that crosses the limit completes, the next is 429
`budget_exceeded`. The stub also answers the ways a gateway can go wrong (a 429 from the global
request bucket with no budget code, a gateway that never refuses, a control identity that is refused)
and records every request, so each verdict is tied to a request the gateway actually received. All
keys are synthetic.
"""

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import seed_harness
import yaml
from fake_http import DrainingHandler

REPO = playbook_yaml.REPO
PLAYBOOK = REPO / "platform/playbooks/probe-agentgateway-isolation.yml"
BURST, CONTROL = "isolation-probe", "other"
KEYS = {"client_isolation-probe": "synthetic-burst-key-5c1e", "client_other": "synthetic-control-key-93ab",
        "vllm_api_key": "synthetic-upstream-key-0d77"}
BUDGET_BODY = {"error": {"message": "Budget exceeded", "type": "rate_limit_error", "code": "budget_exceeded"}}
USED = 94  # tokens one completion is charged (the measured local figure)


class Bao(seed_harness.FakeBao):
    store: dict = {}

    def do_POST(self):
        self.record("POST")
        if self.path == "/v1/auth/approle/login":
            return self.reply({"auth": {"client_token": seed_harness.LOGIN}})
        self.reply({}, 404)

    def do_GET(self):
        self.record("GET")
        if self.path == "/v1/secret/data/services/agentgateway":
            return self.reply({"data": {"data": type(self).store, "metadata": {"version": 1}}})
        self.reply({}, 404)


class Gateway(ThreadingHTTPServer):
    """An OpenAI-compatible stub with a per-key rolling token budget.

    budgets  {key: tokens it may still be charged}; a key not listed is unlimited
    refuse   what a 429 looks like: "budget" (the gateway's JSON body), "bare" (JSON, no code: a global
             request bucket or an upstream), "text" (plain-text `rate limit exceeded`),
             "budget-text" (the budget body, but Content-Type text/plain)
    status   {key: status} forces every answer for that key (a refused key, a broken route)
    never    True: no key is ever refused (a gateway whose budget does nothing)
    """

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, budgets=None, refuse="budget", status=None, never=False):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.budgets, self.refuse, self.status, self.never = dict(budgets or {}), refuse, status or {}, never
        self.seen: list[dict] = []
        self.url = f"http://127.0.0.1:{self.server_port}"
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self):
        self.shutdown()
        self.server_close()


class _Handler(DrainingHandler):
    def log_message(self, *_a):
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        gw = self.server
        key = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        gw.seen.append({"path": self.path, "key": key, "body": body})
        if key in gw.status:
            return self._send(gw.status[key], {"error": "forced"})
        left = gw.budgets.get(key)
        if left is not None and left <= 0 and not gw.never:
            return {
                "budget": lambda: self._send(429, BUDGET_BODY),
                "bare": lambda: self._send(429, {"error": {"message": "slow down", "code": "rate_limited"}}),
                "text": lambda: self._send(429, b"rate limit exceeded", "text/plain"),
                "budget-text": lambda: self._send(429, json.dumps(BUDGET_BODY).encode(), "text/plain"),
            }[gw.refuse]()
        if left is not None:
            gw.budgets[key] = left - USED  # charged AFTER the response is produced
        self._send(200, {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": USED}})


@pytest.fixture
def gateway():
    started = []

    def start(**kw):
        gw = Gateway(**kw)
        started.append(gw)
        return gw

    yield start
    for g in started:
        g.stop()


def _policies(budget=20):
    return {BURST: {"tokens_per_hour": budget}} if budget is not None else {}


def _run(tmp: Path, gw: Gateway, *, extra=None, host=None, check=False, store=None):
    """Run the playbook once; returns (returncode, output, bao requests)."""
    Bao.requests = []
    Bao.store = dict(KEYS if store is None else store)
    with seed_harness.serve(Bao) as address:
        host_vars = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                     "service_name": "agentgateway", "agw_clients": [BURST, CONTROL],
                     "agw_models": [{"name": "m"}], "agw_verify_base_url": gw.url,
                     "agw_client_policies": _policies(), **(host or {})}
        inv = {"all": {"vars": {"openbao_addr": address},
                       "children": {"agentgateway_svc": {"hosts": {"gw": host_vars}}}}}
        (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
        ev = {**seed_harness.ROLE, "agw_isolation_burst": BURST, **(extra or {})}
        ev = {k: v for k, v in ev.items() if v is not None}
        cmd = ["ansible-playbook", "-v", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), "-e", json.dumps(ev),
               *(["--check"] if check else [])]
        env = {**harness_sandbox.env_for(tmp), "ANSIBLE_STDOUT_CALLBACK": "default"}
        r = harness_sandbox.run(cmd, tmp, cwd=REPO, env=env, timeout=300)
    out = r.stdout + r.stderr
    for value in (*KEYS.values(), *seed_harness.NEVER_PRINTED, "Bearer"):
        assert value not in out, f"{value} printed"
    return r.returncode, out, list(Bao.requests)


def _flat(out: str) -> str:
    return " ".join(out.split())


def _keys(gw: Gateway) -> list[str]:
    names = {v: k for k, v in KEYS.items()}
    return [names[s["key"]].removeprefix("client_") for s in gw.seen]


# ── the scenario ──────────────────────────────────────────────────────────────

def test_the_burst_identity_is_refused_for_its_budget_while_the_control_is_served(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 20})
    rc, out, _ = _run(tmp_path, gw)
    assert rc == 0, out
    # the request that crosses the limit completes; the next one is the refusal; then the control
    assert _keys(gw) == [BURST, BURST, CONTROL]
    assert "burst attempts: 1: HTTP 200; 2: HTTP 429 budget_exceeded" in _flat(out), out
    assert "control: HTTP 200 (want HTTP 200)" in out, out
    assert "burst budget refusal (HTTP 429 budget_exceeded): seen" in out, out
    assert all(s["path"] == "/v1/chat/completions" for s in gw.seen)
    assert gw.seen[0]["body"] == {"model": "m", "messages": [{"role": "user", "content": "ok"}], "max_tokens": 16}


def test_an_identity_already_exhausted_when_the_run_starts_is_valid_evidence(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0})
    rc, out, _ = _run(tmp_path, gw)
    assert rc == 0, out
    assert _keys(gw) == [BURST, CONTROL]
    assert "burst attempts: 1: HTTP 429 budget_exceeded" in _flat(out), out


def test_the_budget_code_is_recognised_when_the_content_type_is_not_json(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0}, refuse="budget-text")
    rc, out, _ = _run(tmp_path, gw)
    assert rc == 0, out
    assert "1: HTTP 429 budget_exceeded" in _flat(out), out


def test_each_identity_is_sent_with_the_models_it_may_use(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0})
    host = {"agw_models": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
            "agw_client_policies": {BURST: {"tokens_per_hour": 20, "allowed_models": ["b", "c"]},
                                    CONTROL: {"allowed_models": ["c"]}}}
    rc, out, _ = _run(tmp_path, gw, host=host)
    assert rc == 0, out
    assert [s["body"]["model"] for s in gw.seen] == ["b", "c"]


def test_a_429_without_the_budget_code_is_not_isolation_evidence(tmp_path, gateway):
    # the global request bucket (or an upstream) answers 429 for everyone: say so and fail
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0}, refuse="bare")
    rc, out, _ = _run(tmp_path, gw)
    assert rc != 0
    assert "1: HTTP 429 rate_limited" in _flat(out), out
    assert "WITHOUT error.code budget_exceeded" in _flat(out), out
    assert "budget refusal (HTTP 429 budget_exceeded): NOT seen" in out, out
    assert _keys(gw) == [BURST, CONTROL]  # the report still carries the control's answer


def test_a_plain_text_429_is_the_request_bucket_and_fails(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0}, refuse="text")
    rc, out, _ = _run(tmp_path, gw)
    assert rc != 0 and "WITHOUT error.code budget_exceeded" in _flat(out), out
    assert 'burst attempts: 1: HTTP 429",' in out, out  # no error code at all


def test_a_gateway_that_never_refuses_fails_after_five_attempts(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0}, never=True)
    rc, out, _ = _run(tmp_path, gw)
    assert rc != 0
    assert _keys(gw) == [BURST] * 5 + [CONTROL]
    assert "no HTTP 429 budget_exceeded for 'isolation-probe' in 5 attempt(s)" in _flat(out), out


def test_an_answer_other_than_200_stops_the_burst_and_fails(tmp_path, gateway):
    gw = gateway(status={KEYS["client_isolation-probe"]: 401})
    rc, out, _ = _run(tmp_path, gw)
    assert rc != 0
    assert _keys(gw) == [BURST, CONTROL]  # one attempt, then the control
    assert "burst attempts: 1: HTTP 401" in _flat(out), out
    assert "no HTTP 429 budget_exceeded" in _flat(out), out


def test_a_control_identity_that_is_refused_too_fails_the_run(tmp_path, gateway):
    # the burst identity's budget took the control's share (or the control is itself refused)
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0, KEYS["client_other"]: 0})
    rc, out, _ = _run(tmp_path, gw)
    assert rc != 0
    assert "Isolation FAILED: the control identity 'other' answered 429" in _flat(out), out
    assert "control: HTTP 429 (want HTTP 200)" in out, out


def test_a_gateway_that_is_not_listening_fails_with_no_http_answer(tmp_path, gateway):
    gw = gateway()
    gw.stop()
    rc, out, _ = _run(tmp_path, gw)
    assert rc != 0 and "no HTTP answer" in out, out


def test_a_blank_control_survey_value_takes_the_first_other_identity(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0})
    rc, out, _ = _run(tmp_path, gw, extra={"agw_isolation_control": ""},
                      host={"agw_clients": [BURST, "second", CONTROL]},
                      store={**KEYS, "client_second": "synthetic-second-key-11aa"})
    assert rc == 0, out
    assert "control identity: second" in out, out


def test_a_named_control_identity_is_the_one_probed(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0})
    rc, out, _ = _run(tmp_path, gw, extra={"agw_isolation_control": CONTROL},
                      host={"agw_clients": [BURST, "second", CONTROL]},
                      store={**KEYS, "client_second": "synthetic-second-key-11aa"})
    assert rc == 0, out
    assert _keys(gw) == [BURST, CONTROL]


def test_the_cap_itself_is_allowed(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0})
    rc, out, _ = _run(tmp_path, gw, host={"agw_client_policies": _policies(1000)})
    assert rc == 0, out


# ── refusals: before any request, before any OpenBao call ─────────────────────

@pytest.mark.parametrize("extra, host, message", [
    pytest.param({"agw_isolation_burst": None}, {}, "Set agw_isolation_burst", id="burst-missing"),
    pytest.param({"agw_isolation_burst": "  "}, {}, "Set agw_isolation_burst", id="burst-blank"),
    pytest.param({"agw_isolation_burst": "ghost"}, {}, "'ghost' is not a declared identity", id="burst-undeclared"),
    pytest.param({}, {"agw_client_policies": {}}, "has no agw_client_policies.isolation-probe.tokens_per_hour",
                 id="no-budget"),
    pytest.param({}, {"agw_client_policies": {BURST: {"allowed_models": ["m"]}}},
                 "has no agw_client_policies.isolation-probe.tokens_per_hour", id="policy-without-budget"),
    pytest.param({}, {"agw_client_policies": _policies("lots")}, "has no agw_client_policies",
                 id="budget-not-a-number"),
    pytest.param({}, {"agw_client_policies": _policies(0)}, "has no agw_client_policies", id="budget-zero"),
    pytest.param({}, {"agw_client_policies": _policies(1001)}, "is 1001, above the 1000", id="budget-1001"),
    pytest.param({}, {"agw_client_policies": _policies(2000000)}, "above the 1000", id="budget-the-default-figure"),
    pytest.param({"agw_isolation_control": BURST}, {}, "must be declared in agw_identities and differ from the burst",
                 id="control-is-burst"),
    pytest.param({"agw_isolation_control": "ghost"}, {}, "must be declared in agw_identities and differ",
                 id="control-undeclared"),
    pytest.param({}, {"agw_clients": [BURST]}, "must be declared in agw_identities and differ", id="no-other-identity"),
    pytest.param({"agw_isolation_control": CONTROL},
                 {"agw_models": [{"name": "m"}, {"name": "p", "pool_identity": CONTROL}]},
                 "'other' is a shared-pool identity", id="control-is-a-pool"),
    pytest.param({}, {"agw_models": [{"name": "m"}, {"name": "p", "pool_identity": BURST}]},
                 "'isolation-probe' is a shared-pool identity", id="burst-is-a-pool"),
    pytest.param({}, {"agw_client_policies": {BURST: {"tokens_per_hour": 20, "allowed_models": ["nope"]}}},
                 "at least one declared agw_models name", id="burst-allowed-no-declared-model"),
])
def test_an_unsafe_or_incomplete_input_is_refused_before_any_request(tmp_path, gateway, extra, host, message):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0})
    rc, out, bao = _run(tmp_path, gw, extra=extra, host=host)
    assert rc != 0 and message in _flat(out), out
    assert gw.seen == [] and bao == []


def test_a_cleartext_public_gateway_is_refused_before_a_key_is_read(tmp_path, gateway):
    gw = gateway()
    rc, out, bao = _run(tmp_path, gw, host={"agw_verify_base_url": "http://gateway.example.com:4000"})
    assert rc != 0 and "Refusing to send secret material to the gateway at an invalid endpoint" in _flat(out), out
    assert gw.seen == [] and bao == []


def test_a_missing_client_key_stops_the_run_before_any_request(tmp_path, gateway):
    gw = gateway()
    store = {k: v for k, v in KEYS.items() if k != "client_other"}
    rc, out, bao = _run(tmp_path, gw, store=store)
    assert rc != 0 and "lacks client_other" in _flat(out), out
    assert gw.seen == [] and ("GET", "/v1/secret/data/services/agentgateway") in bao


def test_a_dry_run_validates_and_sends_nothing(tmp_path, gateway):
    gw = gateway(budgets={KEYS["client_isolation-probe"]: 0})
    rc, out, bao = _run(tmp_path, gw, check=True)
    assert rc == 0 and "No key was read and nothing was sent" in _flat(out), out
    assert gw.seen == [] and bao == []


def test_a_dry_run_still_refuses_an_unsafe_input(tmp_path, gateway):
    gw = gateway()
    rc, out, bao = _run(tmp_path, gw, check=True, host={"agw_client_policies": _policies(1001)})
    assert rc != 0 and "above the 1000" in _flat(out), out
    assert gw.seen == [] and bao == []


# ── static properties ─────────────────────────────────────────────────────────

def _tasks():
    """The play's tasks in file order, a block's children right after the block."""
    def walk(tasks):
        for t in tasks:
            yield t
            yield from walk(t.get("block", []))
    return walk(playbook_yaml.load(PLAYBOOK)[2]["tasks"])


def test_only_the_credential_tasks_are_no_log():
    quiet = [t["name"] for t in _tasks() if t.get("no_log") is True]
    assert quiet == ["Authenticate to OpenBao", "Read the gateway's secret path", "Keep the two identities' keys"]


def test_the_probe_tasks_and_the_verdict_are_not_no_log():
    tasks = list(_tasks())
    sends = [t for t in tasks if t.get("ansible.builtin.include_tasks") == "tasks/agw-probe.yml"]
    assert len(sends) == 6  # five burst attempts and the control
    assert all(t["vars"]["_agwp_method"] == "POST" and t["vars"]["_agwp_path"] == "/v1/chat/completions"
               for t in sends)
    assert [t["vars"]["_agwp_key"] for t in sends] == ["{{ _iso_key_burst }}"] * 5 + ["{{ _iso_key_control }}"]
    for t in tasks:
        if "ansible.builtin.assert" in t or "ansible.builtin.debug" in t or t in sends:
            assert t.get("no_log") is not True, t["name"]


def test_every_request_goes_through_the_shared_probe_path_and_no_other_module_sends_one():
    tasks = list(_tasks())
    direct = [t["name"] for t in tasks if "ansible.builtin.uri" in t]
    assert direct == ["Authenticate to OpenBao", "Read the gateway's secret path"]
    assert not [t for t in tasks if "ansible.builtin.command" in t or "ansible.builtin.shell" in t
                or "ansible.builtin.raw" in t]


def test_no_visible_task_loops_over_a_protected_result():
    # MISTAKES 4.6: the only loop is the leaf stat, over its own clean input
    looped = [t["name"] for t in _tasks() if "loop" in t or "with_items" in t]
    assert looped == ["Read the verifier leaf and the trust bundle (listener TLS only)"]


def test_the_burst_cap_and_the_refusals_come_before_any_openbao_call():
    names = [t["name"] for t in _tasks()]
    first_bao = names.index("Authenticate to OpenBao")
    for refusal in ("Refuse a missing burst identity", "Refuse a burst identity the gateway does not declare",
                    "Refuse a burst identity with no declared token budget",
                    "Refuse a burst identity whose budget is a real one",
                    "Refuse a control identity that is missing, undeclared or the burst identity"):
        assert names.index(refusal) < first_bao, refusal


# ── wiring ────────────────────────────────────────────────────────────────────

def _templates():
    return yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]


def test_the_semaphore_template_is_dev_bound_with_the_two_survey_vars():
    names = [t["name"] for t in _templates()]
    t = next(t for t in _templates() if t["name"] == "Probe agentgateway Isolation (Dev)")
    assert names.index(t["name"]) == names.index("Probe agentgateway Gate (Dev)") + 1
    assert t["playbook"] == "platform/playbooks/probe-agentgateway-isolation.yml"
    assert t["repository"] == "agent-cloud dev"
    assert "dev_variant" not in t
    sv = {v["name"]: v for v in t["survey_vars"]}
    assert list(sv) == ["agw_isolation_burst", "agw_isolation_control"]
    assert sv["agw_isolation_burst"]["required"] is True and sv["agw_isolation_control"]["required"] is False


def test_the_playbook_is_launchable_and_guarded_first():
    doc = playbook_yaml.load(PLAYBOOK)
    assert doc[0].get("ansible.builtin.import_playbook") == "refuse-internal-extra-vars.yml"
    assert doc[1].get("ansible.builtin.import_playbook") == "preflight-target-group.yml"
    assert doc[2]["hosts"] == "agentgateway_svc"
    assert json.dumps(doc[1]["vars"]) == json.dumps(
        {"preflight_group": "agentgateway_svc", "preflight_group_expected": "agentgateway_svc"})


def test_the_local_inventory_declares_a_probe_identity_within_the_cap():
    text = (REPO / "platform/inventory/local-dev.yml.example").read_text()
    host = yaml.safe_load(text)["all"]["children"]["agentgateway_svc"]["hosts"]["agentgateway-local"]
    assert "isolation-probe" in host["agw_clients"]
    assert 0 < host["agw_client_policies"]["isolation-probe"]["tokens_per_hour"] <= 1000


def test_agents_md_lists_the_workflow():
    agents = (REPO / "AGENTS.md").read_text()
    assert "| Probe agentgateway Isolation (Dev) | `probe-agentgateway-isolation.yml` |" in agents
