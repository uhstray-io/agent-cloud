"""Client and flow tests for the launch gate, against fake Semaphore and OPA servers on loopback.

Each test gets its own fakes and config file, so nothing is shared and the file runs under pytest-xdist.
The unreachable-OPA case aims at fake_http.refused_port(), a port that refuses at once. The fakes derive
from DrainingHandler (platform/tests/fake_http.py), the base every fake HTTP server in the suites uses, so
no reply leaves an unread request body. Gate.handle is called directly; the TLS listener is tested where
the server lives.
"""

import contextlib
import dataclasses
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest
from fake_http import DrainingHandler, refused_port, serve

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "platform/services/launch-gate/deployment/app/launch_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("launch_gate_flow", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses can resolve the module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lg = _load_gate()

PROJECT = 2
TEMPLATE = "Deploy agentgateway (Dev)"
TEMPLATE_ID = 12
OTHER_TEMPLATE = "Check o11y Liveness (Dev)"
OTHER_TEMPLATE_ID = 13
REPO_ID = 5
TASK_ID = 77
OTHER_TASK_ID = 88
VERIFIED = "v2.19.11"
SEMA_BASE = f"/api/project/{PROJECT}"
TASKS = f"{SEMA_BASE}/tasks"
OPA_PATH = "/v1/data/agentcloud/decision"
SEMA_TOKEN = "sema-" + "k" * 40
SKYNET_TOKEN = "skyn-" + "j" * 40
CONSOLE_TOKEN = "cons-" + "m" * 40
# Unique sentinels: a leak of either shows up in the events, never by coincidence with a name or a branch.
SURVEY_VALUE = "sv-q7x3"
DEFAULT_VALUE = "sv-default-r4k9"
OPA_REASON = "ok by policy"
ENV = {
    "LAUNCH_GATE_SEMAPHORE_TOKEN": SEMA_TOKEN,
    "LAUNCH_GATE_CALLER_TOKEN_SKYNET": SKYNET_TOKEN,
    "LAUNCH_GATE_CALLER_TOKEN_CONSOLE": CONSOLE_TOKEN,
}
TEMPLATE_DETAIL = {
    "id": TEMPLATE_ID,
    "name": TEMPLATE,
    "repository_id": REPO_ID,
    "survey_vars": [
        {"name": "service_name", "required": True},
        {"name": "image_tag", "default_value": DEFAULT_VALUE},
    ],
}


def _happy_semaphore():
    return {
        ("GET", f"{SEMA_BASE}/templates"): (
            200,
            [{"id": TEMPLATE_ID, "name": TEMPLATE}, {"id": 13, "name": "Check o11y Liveness (Dev)"}],
            {},
        ),
        ("GET", f"{SEMA_BASE}/templates/{TEMPLATE_ID}"): (200, TEMPLATE_DETAIL, {}),
        ("GET", f"{SEMA_BASE}/repositories"): (
            200,
            [{"id": 4, "git_branch": "main"}, {"id": REPO_ID, "git_branch": "dev"}],
            {},
        ),
        ("GET", f"{SEMA_BASE}/tasks/last"): (200, [], {}),
        ("POST", TASKS): (201, {"id": TASK_ID}, {}),
        ("POST", f"{TASKS}/{TASK_ID}/stop"): (204, None, {}),
        ("GET", "/api/info"): (200, {"version": VERIFIED}, {}),
    }


class Fake:
    """A scripted HTTP server on loopback. It records every request and answers from ``routes``.

    routes maps (method, path) to (status, payload, headers). A payload of None sends an empty body;
    anything else is sent as JSON. An unscripted request is answered 404 and still recorded.
    """

    def __init__(self):
        self.requests = []
        self.routes = {}
        # (method, path) -> callable(record) returning (status, payload, headers). Lets a test block one call
        # or make a read depend on an earlier write; it takes precedence over ``routes``.
        self.hooks = {}
        fake = self

        class Handler(DrainingHandler):
            def log_message(self, format, *args):
                return

            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                record = {
                    "method": method,
                    "path": self.path,
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                    "body": json.loads(raw) if raw else None,
                }
                fake.requests.append(record)
                if (method, self.path) in fake.hooks:
                    status, payload, extra = fake.hooks[(method, self.path)](record)
                else:
                    status, payload, extra = fake.routes.get((method, self.path), (404, {"error": "unscripted"}, {}))
                data = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                for name, value in extra.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self._fixture = contextlib.ExitStack()
        self.url = self._fixture.enter_context(serve(Handler))

    def calls(self):
        return [(record["method"], record["path"]) for record in self.requests]

    def close(self):
        self._fixture.close()


def _config(tmp_path, opa_url, semaphore_url):
    doc = {
        "semaphore_url": semaphore_url,
        "semaphore_project": PROJECT,
        "opa_url": opa_url,
        "allowlist": [TEMPLATE, OTHER_TEMPLATE],
        "callers": {"skynet": ["infra-agent"], "console": []},
        "listen_host": "127.0.0.1",
        "listen_port": 8443,
        "tls_cert": "/certs/launch-gate-server/current/cert.pem",
        "tls_key": "/certs/launch-gate-server/current/key.pem",
        "local_mode": True,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return lg.load_config(str(path), dict(ENV))


def _request(**changes):
    doc = {
        "template": TEMPLATE,
        "agent": "infra-agent",
        "workflow_id": "onboard-x",
        "step": "ssh-keys",
        "survey": {"service_name": SURVEY_VALUE},
    }
    doc.update(changes)
    return doc


def _raw(doc):
    return json.dumps(doc).encode("utf-8")


def _posts(world):
    return [record for record in world.semaphore.requests if (record["method"], record["path"]) == ("POST", TASKS)]


class World:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.semaphore = Fake()
        self.opa = Fake()
        self.events = []
        self.semaphore.routes.update(_happy_semaphore())
        self.opa.routes[("POST", OPA_PATH)] = (200, {"result": {"allowed": True, "reason": OPA_REASON}}, {})
        self.gate = self.gate_for(self.opa.url)

    def gate_for(self, opa_url, opa=None):
        cfg = _config(self.tmp_path, opa_url, self.semaphore.url)
        semaphore = lg.SemaphoreClient(cfg.semaphore_url, cfg.semaphore_project, cfg.semaphore_token)
        opa_client = opa if opa is not None else lg.OPAClient(cfg.opa_url)
        return lg.Gate(cfg, semaphore, opa_client, log=self.events.append)

    def launch(self, body=None, *, token=SKYNET_TOKEN, raw=None, gate=None):
        return self._post("/v1/launch", body, token=token, raw=raw, gate=gate)

    def decide(self, body=None, *, token=SKYNET_TOKEN, raw=None, gate=None):
        return self._post("/v1/decide", body, token=token, raw=raw, gate=gate)

    def _post(self, path, body, *, token, raw, gate):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        payload = raw if raw is not None else _raw(_request() if body is None else body)
        return (gate or self.gate).handle("POST", path, headers, payload)

    def set_opa(self, answer):
        self.opa.routes[("POST", OPA_PATH)] = (200, answer, {})

    def close(self):
        self.semaphore.close()
        self.opa.close()


@pytest.fixture
def world(tmp_path):
    built = World(tmp_path)
    yield built
    built.close()


# ---------- routing ----------


def test_healthz_answers_without_a_token(world):
    assert world.gate.handle("GET", "/healthz", {}, b"") == (200, {"status": "ok"})
    assert world.semaphore.requests == [] and world.opa.requests == [] and world.events == []


@pytest.mark.parametrize(("method", "path"), [("GET", "/nope"), ("POST", "/healthz")])
def test_unknown_path_is_404(world, method, path):
    assert world.gate.handle(method, path, {}, b"") == (404, {"error": "not_found"})
    assert world.semaphore.requests == [] and world.opa.requests == []


def test_launch_path_refuses_other_methods_with_405(world):
    assert world.gate.handle("GET", "/v1/launch", {}, b"") == (405, {"error": "method_not_allowed"})
    assert world.semaphore.requests == [] and world.opa.requests == [] and world.events == []


def test_decide_path_refuses_other_methods_with_405(world):
    assert world.gate.handle("GET", "/v1/decide", {}, b"") == (405, {"error": "method_not_allowed"})
    assert world.semaphore.requests == [] and world.opa.requests == [] and world.events == []


# ---------- authentication and agent binding ----------


def test_missing_token_is_401_and_reaches_neither_server(world):
    status, payload = world.launch(token=None)
    assert (status, payload["error"]) == (401, "unauthorized")
    assert world.semaphore.requests == [] and world.opa.requests == []
    assert world.events == [{"event": "launch", "outcome": 401, "error": "unauthorized"}]


def test_console_token_cannot_act_as_skynets_agent(world):
    status, payload = world.launch(_request(agent="infra-agent"), token=CONSOLE_TOKEN)
    assert (status, payload["error"]) == (403, "agent_not_permitted")
    assert world.semaphore.requests == [] and world.opa.requests == []


def test_authorization_header_name_is_case_insensitive(world):
    status, _ = world.gate.handle("POST", "/v1/launch", {"authorization": f"Bearer {SKYNET_TOKEN}"}, _raw(_request()))
    assert status == 201


def test_template_off_the_allowlist_is_403(world):
    status, payload = world.launch(_request(template="Destroy VM"))
    assert (status, payload["error"]) == (403, "template_not_allowed")
    assert world.semaphore.requests == [] and world.opa.requests == []


# ---------- request shape and literal values ----------


def test_templated_survey_value_is_400_and_reaches_neither_server(world):
    status, payload = world.launch(_request(survey={"service_name": "{{ lookup('env', 'X') }}"}))
    assert (status, payload["error"]) == (400, "bad_request")
    assert world.semaphore.requests == [] and world.opa.requests == []


def test_human_approved_from_the_caller_is_refused_as_an_unknown_key(world):
    status, payload = world.launch(_request(human_approved=True))
    assert (status, payload["error"]) == (400, "bad_request")
    assert "human_approved" in payload["detail"]
    assert world.semaphore.requests == [] and world.opa.requests == []


def test_bad_json_body_is_400_and_logged_without_request_fields(world):
    status, payload = world.launch(raw=b"{not json")
    assert (status, payload["error"]) == (400, "bad_request")
    assert world.events == [
        {
            "event": "launch",
            "caller": "skynet",
            "outcome": 400,
            "error": "bad_request",
            "reason": None,
            "task_id": None,
        }
    ]


# ---------- survey merge: Semaphore reads only, then refusal before OPA ----------


def test_undeclared_key_is_refused_before_opa_and_creates_nothing(world):
    status, payload = world.launch(_request(survey={"service_name": SURVEY_VALUE, "inventory_id": "7"}))
    assert (status, payload["error"]) == (400, "undeclared_field")
    assert "inventory_id" in payload["detail"]
    assert world.opa.requests == []
    assert world.semaphore.calls() == [
        ("GET", f"{SEMA_BASE}/templates"),
        ("GET", f"{SEMA_BASE}/templates/{TEMPLATE_ID}"),
    ]


def test_missing_required_field_is_400_and_asks_opa_nothing(world):
    status, payload = world.launch(_request(survey={}))
    assert (status, payload["error"]) == (400, "missing_required")
    assert world.opa.requests == []
    assert world.semaphore.calls() == [
        ("GET", f"{SEMA_BASE}/templates"),
        ("GET", f"{SEMA_BASE}/templates/{TEMPLATE_ID}"),
    ]


def test_template_name_with_no_match_is_502(world):
    world.semaphore.routes[("GET", f"{SEMA_BASE}/templates")] = (
        200,
        [{"id": 13, "name": "Check o11y Liveness (Dev)"}],
        {},
    )
    status, payload = world.launch()
    assert (status, payload["error"]) == (502, "template_not_found")
    assert world.opa.requests == [] and _posts(world) == []


def test_template_name_with_two_matches_is_502_and_never_guessed(world):
    world.semaphore.routes[("GET", f"{SEMA_BASE}/templates")] = (
        200,
        [{"id": TEMPLATE_ID, "name": TEMPLATE}, {"id": 14, "name": TEMPLATE}],
        {},
    )
    status, payload = world.launch()
    assert (status, payload["error"]) == (502, "template_ambiguous")
    assert world.semaphore.calls() == [("GET", f"{SEMA_BASE}/templates")]


@pytest.mark.parametrize(
    "repositories",
    [
        [{"id": 4, "git_branch": "main"}],  # the template's record is absent
        [{"id": REPO_ID, "git_branch": ""}],  # the record has an empty branch
    ],
)
def test_unreadable_repository_record_is_502_and_asks_opa_nothing(world, repositories):
    world.semaphore.routes[("GET", f"{SEMA_BASE}/repositories")] = (200, repositories, {})
    status, payload = world.launch()
    assert (status, payload["error"]) == (502, "semaphore_unreadable")
    assert world.opa.requests == [] and _posts(world) == []


# ---------- OPA decision ----------


def test_opa_deny_is_403_with_its_reason_and_no_task(world):
    world.set_opa({"result": {"allowed": False, "reason": "outside the change window"}})
    status, payload = world.launch()
    assert (status, payload["error"]) == (403, "opa_denied")
    assert payload["detail"] == "OPA denied the launch"
    assert payload["reason"] == "outside the change window"
    assert _posts(world) == []
    assert world.events[-1]["error"] == "opa_denied"
    assert world.events[-1]["reason"] == "outside the change window"


def test_opa_unreachable_is_503_and_no_task(world):
    gate = world.gate_for(f"http://127.0.0.1:{refused_port()}")
    status, payload = world.launch(gate=gate)
    assert (status, payload["error"]) == (503, "opa_unavailable")
    assert _posts(world) == []


@pytest.mark.parametrize(
    "answer",
    [
        {"result": {"reason": "no decision"}},  # allowed missing
        {"result": {"allowed": "true", "reason": "x"}},  # the string "true" is not a boolean
        {"result": {"allowed": 1, "reason": "x"}},  # truthy, not True
        {"result": {"allowed": 0, "reason": "x"}},  # falsy, not False
        {"result": {"allowed": None, "reason": "x"}},
        {"result": True},  # result is not an object
        {"allowed": True},  # no result wrapper
        {},
    ],
)
def test_opa_answer_that_is_not_a_boolean_is_503_and_no_task(world, answer):
    world.set_opa(answer)
    status, payload = world.launch()
    assert (status, payload["error"]) == (503, "opa_unavailable")
    assert _posts(world) == []


# ---------- the allowed launch ----------


def test_allowed_launch_creates_exactly_one_task_with_the_validated_values(world):
    status, payload = world.launch()
    assert status == 201
    assert payload == {
        "task_id": TASK_ID,
        "message": "skynet onboard-x ssh-keys",
        "decision": {"allowed": True, "reason": OPA_REASON},
    }
    assert world.semaphore.calls() == [
        ("GET", f"{SEMA_BASE}/templates"),
        ("GET", f"{SEMA_BASE}/templates/{TEMPLATE_ID}"),
        ("GET", f"{SEMA_BASE}/repositories"),
        ("GET", f"{SEMA_BASE}/tasks/last"),
        ("POST", TASKS),
    ]
    assert [record["body"] for record in _posts(world)] == [
        {
            "project_id": PROJECT,
            "template_id": TEMPLATE_ID,
            "message": "skynet onboard-x ssh-keys",
            "environment": '{"image_tag": "sv-default-r4k9", "service_name": "sv-q7x3"}',
        }
    ]
    assert world.opa.calls() == [("POST", OPA_PATH)]
    assert world.opa.requests[0]["body"] == {
        "input": {
            "agent": "infra-agent",
            "service": "semaphore",
            "action": "run_task",
            "template_name": TEMPLATE,
            "human_approved": False,
            "step": "ssh-keys",
            "git_branch": "dev",
            "settings": {"image_tag": DEFAULT_VALUE, "service_name": SURVEY_VALUE},
        }
    }


def test_semaphore_gets_the_gates_token_and_opa_gets_no_authorization(world):
    world.launch()
    for record in world.semaphore.requests:
        assert record["headers"].get("authorization") == f"Bearer {SEMA_TOKEN}", record["path"]
    for record in world.opa.requests:
        assert "authorization" not in record["headers"]


def test_running_task_of_the_template_is_409_naming_it(world):
    world.semaphore.routes[("GET", f"{SEMA_BASE}/tasks/last")] = (
        200,
        [
            {"id": 6, "template_id": TEMPLATE_ID, "status": "success"},
            {"id": 7, "template_id": TEMPLATE_ID, "status": "running"},
            {"id": 8, "template_id": 99, "status": "running"},
        ],
        {},
    )
    status, payload = world.launch()
    assert (status, payload["error"]) == (409, "already_running")
    assert "task: 7" in payload["detail"]
    assert _posts(world) == []


# ---------- check mode ----------


def test_dry_run_on_an_unverified_semaphore_version_is_refused_before_opa(world):
    world.semaphore.routes[("GET", "/api/info")] = (200, {"version": "v2.20.0"}, {})
    status, payload = world.launch(_request(dry_run=True))
    assert (status, payload["error"]) == (400, "dry_run_unverified")
    assert world.opa.requests == [] and _posts(world) == []


def test_dry_run_recorded_as_check_mode_launches_and_keeps_the_task(world):
    world.semaphore.routes[("GET", f"{TASKS}/{TASK_ID}")] = (
        200,
        {"id": TASK_ID, "params": {"dry_run": True, "diff": True}},
        {},
    )
    status, payload = world.launch(_request(dry_run=True))
    assert (status, payload["task_id"]) == (201, TASK_ID)
    assert [record["body"]["params"] for record in _posts(world)] == [{"dry_run": True, "diff": True}]
    assert world.semaphore.calls()[-1] == ("GET", f"{TASKS}/{TASK_ID}")
    assert not any(path.endswith("/stop") for _, path in world.semaphore.calls())


@pytest.mark.parametrize(
    "readback",
    [
        (200, {"id": TASK_ID}, {}),  # no params at all
        (200, {"id": TASK_ID, "params": {"dry_run": False}}, {}),  # params present, flag false
        (200, {"id": TASK_ID, "params": None}, {}),  # params not an object
        (500, {"error": "boom"}, {}),  # the read-back itself fails
    ],
)
def test_dry_run_not_recorded_stops_the_task_and_refuses(world, readback):
    world.semaphore.routes[("GET", f"{TASKS}/{TASK_ID}")] = readback
    status, payload = world.launch(_request(dry_run=True))
    assert (status, payload["error"]) == (502, "dry_run_not_recorded")
    assert f"task {TASK_ID}" in payload["detail"] and "stop requested" in payload["detail"]
    stops = [record for record in world.semaphore.requests if record["path"].endswith("/stop")]
    assert [(r["method"], r["path"], r["body"]) for r in stops] == [("POST", f"{TASKS}/{TASK_ID}/stop", {})]
    assert world.events[-1]["task_id"] == TASK_ID


def test_dry_run_not_recorded_and_failed_stop_says_so(world):
    world.semaphore.routes[("GET", f"{TASKS}/{TASK_ID}")] = (200, {"id": TASK_ID, "params": {}}, {})
    world.semaphore.routes[("POST", f"{TASKS}/{TASK_ID}/stop")] = (500, {"error": "boom"}, {})
    status, payload = world.launch(_request(dry_run=True))
    assert (status, payload["error"]) == (502, "dry_run_not_recorded")
    assert "stop request also failed" in payload["detail"]
    assert "stop requested" not in payload["detail"]


# ---------- submission outcome ----------


@pytest.mark.parametrize(
    "answer",
    [
        (500, {"error": "boom"}, {}),
        (201, {"id": "77"}, {}),  # an id that is not an int
        (201, {"id": True}, {}),  # a JSON true is not an id
        (201, None, {}),  # an empty body
    ],
)
def test_failed_task_post_is_an_uncertain_outcome_and_is_never_retried(world, answer):
    world.semaphore.routes[("POST", TASKS)] = answer
    status, payload = world.launch()
    assert (status, payload["error"]) == (502, "outcome_uncertain")
    assert payload["detail"].startswith("submission outcome uncertain")
    assert len(_posts(world)) == 1
    assert world.events[-1]["task_id"] is None


def test_redirect_on_a_read_is_refused_and_never_followed(world):
    world.semaphore.routes[("GET", f"{SEMA_BASE}/templates")] = (302, None, {"Location": f"{SEMA_BASE}/elsewhere"})
    world.semaphore.routes[("GET", f"{SEMA_BASE}/elsewhere")] = (200, [{"id": TEMPLATE_ID, "name": TEMPLATE}], {})
    status, payload = world.launch()
    assert (status, payload["error"]) == (502, "semaphore_unreadable")
    assert "HTTP 302" in payload["detail"]
    assert ("GET", f"{SEMA_BASE}/elsewhere") not in world.semaphore.calls()
    assert world.opa.requests == [] and _posts(world) == []


def test_redirect_on_the_task_post_is_uncertain_and_never_followed(world):
    world.semaphore.routes[("POST", TASKS)] = (302, None, {"Location": f"{SEMA_BASE}/elsewhere-tasks"})
    world.semaphore.routes[("POST", f"{SEMA_BASE}/elsewhere-tasks")] = (201, {"id": 99}, {})
    status, payload = world.launch()
    assert (status, payload["error"]) == (502, "outcome_uncertain")
    assert ("POST", f"{SEMA_BASE}/elsewhere-tasks") not in world.semaphore.calls()
    assert len(_posts(world)) == 1


# ---------- the branch the launch runs from ----------


def _serve_template(world, **changes):
    """A template that declares service_branch, with a default that the record's branch must override."""
    detail = {
        "id": TEMPLATE_ID,
        "name": TEMPLATE,
        "repository_id": REPO_ID,
        "survey_vars": [
            {"name": "service_name", "required": True},
            {"name": "service_branch", "type": "string", "default_value": "main"},
        ],
    }
    detail.update(changes)
    world.semaphore.routes[("GET", f"{SEMA_BASE}/templates/{TEMPLATE_ID}")] = (200, detail, {})


def test_caller_branch_other_than_the_record_is_400_and_asks_opa_nothing(world):
    _serve_template(world)
    status, payload = world.launch(_request(survey={"service_name": SURVEY_VALUE, "service_branch": "feature-x"}))
    assert (status, payload["error"]) == (400, "branch_mismatch")
    assert payload["detail"] == "service_branch must be the template's branch dev"
    assert world.opa.requests == [] and _posts(world) == []
    assert "feature-x" not in json.dumps(world.events)


def test_omitted_service_branch_is_the_record_branch_in_the_task_and_in_opa(world):
    _serve_template(world)
    status, _ = world.launch()
    assert status == 201
    assert json.loads(_posts(world)[0]["body"]["environment"]) == {
        "service_branch": "dev",
        "service_name": SURVEY_VALUE,
    }
    opa_input = world.opa.requests[0]["body"]["input"]
    assert opa_input["git_branch"] == "dev"
    assert opa_input["settings"] == {"service_branch": "dev", "service_name": SURVEY_VALUE}


def test_caller_branch_equal_to_the_record_launches(world):
    _serve_template(world)
    status, _ = world.launch(_request(survey={"service_name": SURVEY_VALUE, "service_branch": "dev"}))
    assert status == 201
    assert json.loads(_posts(world)[0]["body"]["environment"])["service_branch"] == "dev"


def _record_declares(world, branch):
    world.semaphore.routes[("GET", f"{SEMA_BASE}/repositories")] = (
        200,
        [{"id": 4, "git_branch": "main"}, {"id": REPO_ID, "git_branch": branch}],
        {},
    )


def test_local_mode_reads_the_worktree_record_head_as_dev_for_opa_and_the_pin(world):
    # Local-dev's working-tree record says HEAD; OPA allows agent launches only from main or dev.
    _serve_template(world)
    _record_declares(world, "HEAD")
    status, _ = world.launch()
    assert status == 201
    opa_input = world.opa.requests[0]["body"]["input"]
    assert opa_input["git_branch"] == "dev"
    assert json.loads(_posts(world)[0]["body"]["environment"])["service_branch"] == "dev"


def test_outside_local_mode_head_reaches_opa_unchanged(world):
    _serve_template(world)
    _record_declares(world, "HEAD")
    # load_config refuses http:// outside local mode, and the fakes serve http, so only the flag is switched.
    cfg = dataclasses.replace(_config(world.tmp_path, world.opa.url, world.semaphore.url), local_mode=False)
    semaphore = lg.SemaphoreClient(cfg.semaphore_url, cfg.semaphore_project, cfg.semaphore_token)
    gate = lg.Gate(cfg, semaphore, lg.OPAClient(cfg.opa_url), log=world.events.append)
    world.launch(gate=gate)
    assert world.opa.requests[0]["body"]["input"]["git_branch"] == "HEAD"


@pytest.mark.parametrize(
    ("branch", "local_mode", "expected"),
    [
        ("HEAD", True, "dev"),
        ("HEAD", False, "HEAD"),
        ("main", True, "main"),
        ("dev", True, "dev"),
        ("feature-x", True, "feature-x"),
        ("head", True, "head"),
    ],
)
def test_policy_branch_maps_only_head_and_only_in_local_mode(branch, local_mode, expected):
    assert lg.policy_branch(branch, local_mode) == expected


def test_template_with_its_own_different_branch_is_502_and_asks_opa_nothing(world):
    _serve_template(world, git_branch="other")
    status, payload = world.launch()
    assert (status, payload["error"]) == (502, "branch_ambiguous")
    assert world.opa.requests == [] and _posts(world) == []


# ---------- one launch at a time ----------


def test_two_concurrent_launches_of_one_template_create_exactly_one_task(world):
    # Both launches must read the template's tasks before either creates one. With the per-template lock the
    # second read waits for the first create, so the wait below times out and the create proceeds; without the
    # lock the two reads both see the template idle and both creates go through.
    reads = []
    created = []
    both_read = threading.Event()
    state = threading.Lock()

    def list_tasks(_record):
        with state:
            reads.append(1)
            if len(reads) == 2:
                both_read.set()
            busy = [{"id": TASK_ID, "template_id": TEMPLATE_ID, "status": "waiting"}] if created else []
        return 200, busy, {}

    def create_task(_record):
        both_read.wait(timeout=1.0)
        with state:
            created.append(TASK_ID)
        return 201, {"id": TASK_ID}, {}

    world.semaphore.hooks[("GET", f"{SEMA_BASE}/tasks/last")] = list_tasks
    world.semaphore.hooks[("POST", TASKS)] = create_task

    results = []
    threads = [threading.Thread(target=lambda: results.append(world.launch())) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(status for status, _ in results) == [201, 409]
    assert [payload["error"] for status, payload in results if status == 409] == ["already_running"]
    assert len(_posts(world)) == 1


def test_launches_of_different_templates_do_not_wait_on_each_other(world):
    # Template A's create blocks until template B has posted. B needs no lock A holds, so it posts while A is
    # in flight. Under one lock for every template, B waits behind A, never posts during A's wait, and
    # overlapped records False.
    world.semaphore.routes[("GET", f"{SEMA_BASE}/templates/{OTHER_TEMPLATE_ID}")] = (
        200,
        {"id": OTHER_TEMPLATE_ID, "name": OTHER_TEMPLATE, "repository_id": REPO_ID, "survey_vars": []},
        {},
    )
    a_posting = threading.Event()
    b_posted = threading.Event()
    overlapped = []

    def create_task(record):
        if record["body"]["template_id"] == TEMPLATE_ID:
            a_posting.set()
            overlapped.append(b_posted.wait(timeout=5))
            return 201, {"id": TASK_ID}, {}
        b_posted.set()
        return 201, {"id": OTHER_TASK_ID}, {}

    world.semaphore.hooks[("POST", TASKS)] = create_task
    results = []
    first = threading.Thread(target=lambda: results.append(world.launch(_request())))
    first.start()
    assert a_posting.wait(timeout=10)
    second = threading.Thread(target=lambda: results.append(world.launch(_request(template=OTHER_TEMPLATE, survey={}))))
    second.start()
    for thread in (first, second):
        thread.join(timeout=30)

    assert overlapped == [True]
    assert sorted(status for status, _ in results) == [201, 201]
    assert sorted(record["body"]["template_id"] for record in _posts(world)) == [TEMPLATE_ID, OTHER_TEMPLATE_ID]


# ---------- the log ----------


def test_launch_event_records_the_decision_and_no_survey_value(world):
    world.launch()
    assert world.events == [
        {
            "event": "launch",
            "caller": "skynet",
            "agent": "infra-agent",
            "template": TEMPLATE,
            "step": "ssh-keys",
            "workflow_id": "onboard-x",
            "dry_run": False,
            "survey_keys": ["service_name"],
            "outcome": 201,
            "error": None,
            "reason": OPA_REASON,
            "task_id": TASK_ID,
        }
    ]


def test_no_secret_or_survey_value_reaches_the_log_on_any_outcome(world):
    world.launch()  # allowed
    world.set_opa({"result": {"allowed": False, "reason": "no"}})
    world.launch()  # denied by OPA
    world.launch(token=None)  # unauthenticated
    world.launch(_request(survey={"service_name": SURVEY_VALUE, "inventory_id": SURVEY_VALUE}))  # undeclared
    text = json.dumps(world.events, sort_keys=True)
    for secret in (SEMA_TOKEN, SKYNET_TOKEN, CONSOLE_TOKEN, SURVEY_VALUE, DEFAULT_VALUE):
        assert secret not in text
    assert [event["outcome"] for event in world.events] == [201, 403, 401, 400]


class _ExplodingOPA:
    def decide(self, decision_input):
        raise RuntimeError(f"unexpected {SURVEY_VALUE}")


def test_unexpected_exception_is_500_and_logs_only_its_class_name(world):
    gate = world.gate_for(world.opa.url, opa=_ExplodingOPA())
    status, payload = world.launch(gate=gate)
    assert (status, payload) == (500, {"error": "internal"})
    assert world.events[-1]["exception"] == "RuntimeError"
    assert (world.events[-1]["outcome"], world.events[-1]["error"]) == (500, "internal")
    assert SURVEY_VALUE not in json.dumps(world.events)
    assert _posts(world) == []


def test_default_log_writes_one_json_line_to_stdout(world, capsys):
    cfg = _config(world.tmp_path, world.opa.url, world.semaphore.url)
    gate = lg.Gate(
        cfg,
        lg.SemaphoreClient(cfg.semaphore_url, cfg.semaphore_project, cfg.semaphore_token),
        lg.OPAClient(cfg.opa_url),
    )
    gate.handle("POST", "/v1/launch", {}, _raw(_request()))
    assert capsys.readouterr().out == (
        json.dumps({"event": "launch", "outcome": 401, "error": "unauthorized"}, sort_keys=True) + "\n"
    )


# ---------- the decision route: POST /v1/decide (D51) ----------
# The same checks as /v1/launch up to and including OPA, and the answer. It creates no task, so none of the
# launch's Semaphore writes, template lock or running-task read may happen on it.


def test_allowed_decision_answers_200_and_creates_nothing(world):
    status, payload = world.decide()
    assert status == 200
    assert payload == {"decision": {"allowed": True, "reason": OPA_REASON}}
    assert world.semaphore.calls() == [
        ("GET", f"{SEMA_BASE}/templates"),
        ("GET", f"{SEMA_BASE}/templates/{TEMPLATE_ID}"),
        ("GET", f"{SEMA_BASE}/repositories"),
    ]
    assert _posts(world) == []
    assert world.opa.calls() == [("POST", OPA_PATH)]


def test_decision_asks_opa_the_same_question_as_a_launch(world):
    world.launch()
    launch_input = world.opa.requests[-1]["body"]
    world.decide()
    assert world.opa.requests[-1]["body"] == launch_input


def test_decision_event_has_the_launch_fields_and_no_task_id(world):
    world.decide()
    assert world.events == [
        {
            "event": "decide",
            "caller": "skynet",
            "agent": "infra-agent",
            "template": TEMPLATE,
            "step": "ssh-keys",
            "workflow_id": "onboard-x",
            "dry_run": False,
            "survey_keys": ["service_name"],
            "outcome": 200,
            "error": None,
            "reason": OPA_REASON,
        }
    ]


def test_bad_json_decision_event_has_no_task_id(world):
    status, _ = world.decide(raw=b"{not json")
    assert status == 400
    assert world.events == [
        {"event": "decide", "caller": "skynet", "outcome": 400, "error": "bad_request", "reason": None}
    ]


def test_opa_deny_on_decision_is_200_allowed_false_with_its_reason_and_no_task(world):
    world.set_opa({"result": {"allowed": False, "reason": "outside the change window"}})
    status, payload = world.decide()
    assert status == 200
    assert payload == {"decision": {"allowed": False, "reason": "outside the change window"}}
    assert _posts(world) == []
    event = world.events[-1]
    assert (event["event"], event["outcome"], event["error"], event["reason"]) == (
        "decide",
        200,
        None,
        "outside the change window",
    )
    assert "task_id" not in event


def test_missing_token_on_decision_is_401_and_reaches_neither_server(world):
    status, payload = world.decide(token=None)
    assert (status, payload["error"]) == (401, "unauthorized")
    assert world.semaphore.requests == [] and world.opa.requests == []
    assert world.events == [{"event": "decide", "outcome": 401, "error": "unauthorized"}]


def test_undeclared_field_on_decision_is_400_before_opa(world):
    status, payload = world.decide(_request(survey={"service_name": SURVEY_VALUE, "inventory_id": "7"}))
    assert (status, payload["error"]) == (400, "undeclared_field")
    assert "inventory_id" in payload["detail"]
    assert world.opa.requests == []


def test_template_off_the_allowlist_on_decision_is_403_before_semaphore(world):
    status, payload = world.decide(_request(template="Destroy VM"))
    assert (status, payload["error"]) == (403, "template_not_allowed")
    assert world.semaphore.requests == [] and world.opa.requests == []


def test_opa_unreachable_on_decision_is_503_and_no_task(world):
    gate = world.gate_for(f"http://127.0.0.1:{refused_port()}")
    status, payload = world.decide(gate=gate)
    assert (status, payload["error"]) == (503, "opa_unavailable")
    assert _posts(world) == []


def test_dry_run_on_unverified_version_on_decision_is_400_before_opa(world):
    world.semaphore.routes[("GET", "/api/info")] = (200, {"version": "v2.20.0"}, {})
    status, payload = world.decide(_request(dry_run=True))
    assert (status, payload["error"]) == (400, "dry_run_unverified")
    assert world.opa.requests == [] and _posts(world) == []


def test_dry_run_decision_on_a_verified_version_asks_opa_and_launches_nothing(world):
    status, payload = world.decide(_request(dry_run=True))
    assert (status, payload) == (200, {"decision": {"allowed": True, "reason": OPA_REASON}})
    assert ("GET", "/api/info") in world.semaphore.calls()
    assert world.opa.calls() == [("POST", OPA_PATH)]
    assert _posts(world) == []
    assert not any(path.endswith("/stop") for _, path in world.semaphore.calls())


def test_decision_ignores_a_running_task_of_the_template(world):
    # A launch of this template would be a 409 here. A decision never reads the template's tasks at all.
    world.semaphore.routes[("GET", f"{SEMA_BASE}/tasks/last")] = (
        200,
        [{"id": 7, "template_id": TEMPLATE_ID, "status": "running"}],
        {},
    )
    status, payload = world.decide()
    assert (status, payload) == (200, {"decision": {"allowed": True, "reason": OPA_REASON}})
    assert not any(path.endswith("/tasks/last") for _, path in world.semaphore.calls())
    assert _posts(world) == []


def test_decision_does_not_wait_for_a_launch_holding_the_template_lock(world):
    # A launch holds its template's lock while it reads the running tasks and creates. A decision takes no lock,
    # so it must answer while the lock is held. Were it to take the lock, the join below would time out.
    lock = world.gate._template_lock(TEMPLATE_ID)
    results = []
    with lock:
        thread = threading.Thread(target=lambda: results.append(world.decide()))
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive(), "the decision waited for the template lock"
    assert results == [(200, {"decision": {"allowed": True, "reason": OPA_REASON}})]
    assert _posts(world) == []


def test_decision_pins_the_branch_in_the_opa_input_like_a_launch(world):
    _serve_template(world)
    status, _ = world.decide()
    assert status == 200
    opa_input = world.opa.requests[0]["body"]["input"]
    assert opa_input["git_branch"] == "dev"
    assert opa_input["settings"] == {"service_branch": "dev", "service_name": SURVEY_VALUE}


def test_decision_refuses_a_caller_branch_other_than_the_record_before_opa(world):
    _serve_template(world)
    status, payload = world.decide(_request(survey={"service_name": SURVEY_VALUE, "service_branch": "feature-x"}))
    assert (status, payload["error"]) == (400, "branch_mismatch")
    assert world.opa.requests == []


def _template_absent(world):
    world.semaphore.routes[("GET", f"{SEMA_BASE}/templates")] = (
        200,
        [{"id": 13, "name": OTHER_TEMPLATE}],
        {},
    )


def _version_unverified(world):
    world.semaphore.routes[("GET", "/api/info")] = (200, {"version": "v2.20.0"}, {})


def _record_branch(world):
    _serve_template(world)


@pytest.mark.parametrize(
    ("prepare", "kwargs"),
    [
        pytest.param(None, {"raw": b"{not json"}, id="bad-json"),
        pytest.param(
            None, {"body": _request(survey={"service_name": SURVEY_VALUE, "inventory_id": "7"})}, id="undeclared-field"
        ),
        pytest.param(None, {"body": _request(survey={})}, id="missing-required"),
        pytest.param(None, {"body": _request(template="Destroy VM")}, id="template-off-allowlist"),
        pytest.param(None, {"token": CONSOLE_TOKEN}, id="agent-not-permitted"),
        pytest.param(None, {"token": None}, id="unauthenticated"),
        pytest.param(_template_absent, {}, id="template-not-found"),
        pytest.param(_version_unverified, {"body": _request(dry_run=True)}, id="dry-run-unverified"),
        pytest.param(
            _record_branch,
            {"body": _request(survey={"service_name": SURVEY_VALUE, "service_branch": "feature-x"})},
            id="branch-mismatch",
        ),
    ],
)
def test_decision_refuses_exactly_what_a_launch_refuses(world, prepare, kwargs):
    # The two routes share one check sequence. Every refusal before OPA must come back identically from both:
    # the status, the error code and the detail. OPA is never asked in any of these cases.
    if prepare is not None:
        prepare(world)
    launch = world.launch(**kwargs)
    decision = world.decide(**kwargs)
    assert launch[0] >= 400, launch
    assert decision == launch
    assert world.opa.requests == []
