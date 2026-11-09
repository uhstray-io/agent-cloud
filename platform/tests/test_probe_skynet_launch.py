"""probe-skynet-launch.yml and files/skynet-launch-probe.py: the launch-path probe of skynet's client (openspec
skynet-launch-containment, task 4.4 local).

The playbook runs for real through ansible-playbook on localhost, under harness_sandbox. Its `podman exec skynet` is
replaced by a fake engine, as in test_probe_launch_gate.py: a python file that accepts exactly the playbook's
`exec skynet python ...`, appends the test skynet API's address (the one argument production does not pass), and execs
the probe script as the playbook passed it. The skynet API and Semaphore are synthetic HTTP listeners: skynet's answers
come from a script, and Semaphore's task list from a list the test sets. Everything else is production code: the
script, the playbook's argv, its guards and its assertions. The bearers reach the script only through the environment,
as they do in production.
"""

import json
import ssl
import sys
import threading
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml
from fake_http import DrainingHandler, refused_port
from test_agw_client_tls_probe import _ca, _leaf

REPO = playbook_yaml.REPO
PLAYBOOK = REPO / "platform/playbooks/probe-skynet-launch.yml"
SCRIPT = REPO / "platform/playbooks/files/skynet-launch-probe.py"
REGISTRY = REPO / "platform/workflows/service-onboarding/registry.yml"
DATA = REPO / "platform/services/opa/deployment/policies/agentcloud/data.json"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
TEMPLATES_LOCAL = REPO / "platform/semaphore/templates-local.yml"
AGENTS = REPO / "AGENTS.md"
PROJECT = "3"
RUN_ID = "service_onboarding:0f0e0d0c-0b0a-4090-8080-706050403020"
TAG = f"skynet {RUN_ID} service-validate"
# Synthetic. The playbook's script reads them from the container environment; they are not credentials.
SKYNET_TOKEN = "synthetic-skynet-api-token-not-a-credential"
SEMAPHORE_TOKEN = "synthetic-svc-skynet-read-token-not-a-credential"
# What a run's `failure` field could carry. The probe must never print it.
FAILURE_TEXT = "SYNTHETIC-FAILURE-TEXT-THAT-MUST-NEVER-PRINT"


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    directory = tmp_path_factory.mktemp("skynet-probe-pki")
    _ca(directory, "ca")
    _leaf(directory, "ca", "server", "DNS:localhost", "serverAuth", directory / "leaves/server")
    return directory


class _Listener(ThreadingHTTPServer):
    """A synthetic listener on 127.0.0.1. With a pki it speaks TLS, so the probe's trust is exercised, not assumed.
    Every request is recorded in `seen`; the handler reads the body before dispatch (fake_http.DrainingHandler)."""

    daemon_threads = True

    def __init__(self, handler, pki: Path | None = None):
        super().__init__(("127.0.0.1", 0), handler)
        self.seen: list[dict] = []
        self.context = None
        if pki is not None:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.context.load_cert_chain(pki / "leaves/server/current/cert.pem", pki / "leaves/server/current/key.pem")
        # Serving starts here: a listener that is bound but never served leaves the probe's request to time out.
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def server_close(self):
        self.shutdown()  # ends serve_forever before the socket is closed
        super().server_close()

    def get_request(self):
        sock, addr = self.socket.accept()
        if self.context is None:
            return sock, addr
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        pass

    @property
    def address(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


def _reply(handler, status: int, payload) -> None:
    reply = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(reply)))
    handler.end_headers()
    handler.wfile.write(reply)


class _SkynetHandler(DrainingHandler):
    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        request = {"method": "POST", "path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        self.server.seen.append(request)
        if self.path != "/skynet/runs":
            status, payload = 404, {"error": "not found"}
        elif request["auth"] != f"Bearer {SKYNET_TOKEN}":
            status, payload = 401, {"error": "unauthorized"}
        else:
            status, payload = self.server.answer_post()
        _reply(self, status, payload)

    def do_GET(self):
        request = {"method": "GET", "path": self.path, "auth": self.headers.get("Authorization"), "body": None}
        self.server.seen.append(request)
        if not self.path.startswith("/skynet/runs/"):
            status, payload = 404, {"error": "not found"}
        elif request["auth"] != f"Bearer {SKYNET_TOKEN}":
            status, payload = 401, {"error": "unauthorized"}
        else:
            state = self.server.next_state()
            if state == "404":
                status, payload = 404, {"error": "not found"}
            else:
                status = 200
                payload = {
                    "run_id": self.path.removeprefix("/skynet/runs/"),
                    "state": state,
                    "next": [],
                    "status": "pass" if state == "finished" else None,
                    "failure": FAILURE_TEXT,
                }
        _reply(self, status, payload)

    def log_message(self, *args):
        pass


class Skynet(_Listener):
    """skynet's API. Each POST takes the next entry of `posts` (a status and a body), and defaults to 202 with RUN_ID.
    Each GET takes the next of `states`: a run state, or "404" for a run with no checkpoint yet. The last state repeats,
    so a run that never leaves running is one state in the list."""

    def __init__(self, states: list[str], posts: list[tuple[int, dict]] | None = None, pki: Path | None = None):
        super().__init__(_SkynetHandler, pki)
        self.states = list(states)
        self.posts = list(posts or [])

    def answer_post(self):
        return self.posts.pop(0) if self.posts else (202, {"run_id": RUN_ID})

    def next_state(self) -> str:
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class _SemaphoreHandler(DrainingHandler):
    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        self.server.seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
        if self.server.status != 200:
            status, payload = self.server.status, {"error": "refused"}
        elif path != f"/api/project/{PROJECT}/tasks/last":
            status, payload = 404, {"error": "not found"}
        elif self.headers.get("Authorization") != f"Bearer {SEMAPHORE_TOKEN}":
            status, payload = 401, {"error": "unauthorized"}
        else:
            status, payload = 200, self.server.rows
        _reply(self, status, payload)

    def log_message(self, *args):
        pass


class Semaphore(_Listener):
    """Semaphore's task list. `rows` is what /tasks/last returns; `status` other than 200 is returned instead."""

    def __init__(self, rows: list[dict], status: int = 200, pki: Path | None = None):
        super().__init__(_SemaphoreHandler, pki)
        self.rows = rows
        self.status = status


def _task(task_id: int, message: str, status: str = "success") -> dict:
    return {
        "id": task_id,
        "status": status,
        "message": message,
        "template_id": 7,
        "user_id": 2,
        "created": "2026-11-09T00:00:00Z",
    }


def _engine(tmp: Path, address: str) -> Path:
    """The fake `podman`: it accepts exactly the playbook's `exec skynet python ...` and runs the script with the
    skynet API's address appended."""
    python = sys.executable
    engine = tmp / "fake-engine"
    engine.write_text(
        f"#!{python}\n"
        "import os\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        'if args[:3] != ["exec", "skynet", "python"]:\n'
        '    sys.exit("fake engine: the playbook must exec python in skynet")\n'
        f"os.execv({python!r}, [{python!r}, *args[3:], {address!r}])\n"
    )
    engine.chmod(0o755)
    return engine


def _run(
    tmp: Path,
    *,
    skynet: Skynet,
    semaphore_url: str,
    extra_vars: dict | None = None,
    host: dict | None = None,
    skynet_hosts: dict | None = None,
    check: bool = False,
    env_extra: dict | None = None,
    drop_env: tuple = (),
):
    engine = _engine(tmp, skynet.address)
    host_vars = {
        "ansible_connection": "local",
        "ansible_python_interpreter": sys.executable,
        "container_engine": str(engine),
        **(host or {}),
    }
    inventory = {
        "all": {
            "children": {
                "skynet_svc": {"hosts": {"skynet": host_vars, **(skynet_hosts or {})}},
                "opa_svc": {"hosts": {"opa": {}}},
            }
        }
    }
    (tmp / "inv.yml").write_text(yaml.safe_dump(inventory))
    cmd = ["ansible-playbook", "-v", "-i", str(tmp / "inv.yml"), str(PLAYBOOK)]
    if extra_vars is not None:
        cmd += ["-e", json.dumps(extra_vars)]
    if check:
        cmd.append("--check")
    env = {k: v for k, v in harness_sandbox.env_for(tmp).items() if k not in drop_env}
    env.update(
        {
            "ANSIBLE_STDOUT_CALLBACK": "default",
            "SKYNET_API_TOKEN": SKYNET_TOKEN,
            "SKYNET_SEMAPHORE_URL": semaphore_url,
            "SKYNET_SEMAPHORE_PROJECT": PROJECT,
            "SKYNET_SEMAPHORE_TOKEN": SEMAPHORE_TOKEN,
            **(env_extra or {}),
        }
    )
    result = harness_sandbox.run(cmd, tmp, cwd=REPO, env=env, timeout=300)
    return result.returncode, result.stdout + result.stderr


def _no_leak(out: str):
    for secret in (SKYNET_TOKEN, SEMAPHORE_TOKEN, FAILURE_TEXT):
        assert secret not in out, out


# ── the launch path: one bounded run, one tagged task ────────────────────────


def test_one_tagged_task_passes_and_the_run_is_bounded_to_service_validate(tmp_path):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[_task(4234, TAG)])
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address)
    assert rc == 0, out
    assert f"run 202 - {RUN_ID} finished pass" in out, out
    assert "task 1 - 4234 success" in out, out
    [post] = [r for r in skynet.seen if r["method"] == "POST"]
    assert (post["path"], post["auth"]) == ("/skynet/runs", f"Bearer {SKYNET_TOKEN}")
    assert post["body"] == {
        "graph": "service_onboarding",
        "input": {"service": "opa", "mode": "check", "from_order": 14, "to_order": 14},
    }
    assert [r["path"] for r in semaphore.seen] == [f"/api/project/{PROJECT}/tasks/last?count=200"]
    assert semaphore.seen[0]["auth"] == f"Bearer {SEMAPHORE_TOKEN}"
    _no_leak(out)
    semaphore.server_close()
    skynet.server_close()


def test_the_probe_polls_past_no_checkpoint_and_interrupted_until_the_run_is_final(tmp_path):
    # 404 (no checkpoint yet) and interrupted are both not final; finished is. Two 5 s waits.
    skynet = Skynet(states=["404", "interrupted", "finished"])
    semaphore = Semaphore(rows=[_task(4235, TAG)])
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address)
    assert rc == 0, out
    gets = [r["path"] for r in skynet.seen if r["method"] == "GET"]
    # The run id goes on the path raw: skynet does not decode it, so a percent-encoded colon would not be found.
    assert gets == [f"/skynet/runs/{RUN_ID}"] * 3, gets
    assert f"run 202 - {RUN_ID} finished pass" in out, out
    _no_leak(out)
    semaphore.server_close()
    skynet.server_close()


def test_a_run_still_running_at_the_deadline_is_reported_and_the_task_check_decides(tmp_path):
    # running forever with a 2 s bound: the probe reads the task list at the deadline, and the tag decides.
    skynet = Skynet(states=["running"])
    semaphore = Semaphore(rows=[_task(4236, TAG, "running")])
    rc, out = _run(
        tmp_path,
        skynet=skynet,
        semaphore_url=semaphore.address,
        host={"skynet_probe_timeout_s": 2},
    )
    assert rc == 0, out
    assert f"run 202 - {RUN_ID} running -" in out, out
    assert "task 1 - 4236 running" in out, out
    _no_leak(out)
    semaphore.server_close()
    skynet.server_close()


def test_zero_tagged_tasks_fails_and_names_the_run_state(tmp_path):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[])
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address)
    assert rc != 0, out
    assert "no Semaphore task carries the tag" in out, out
    assert "The run's state is finished and its status pass" in out, out
    _no_leak(out)
    semaphore.server_close()
    skynet.server_close()


def test_zero_tasks_while_still_running_names_running_as_the_state(tmp_path):
    skynet = Skynet(states=["running"])
    semaphore = Semaphore(rows=[])
    rc, out = _run(
        tmp_path,
        skynet=skynet,
        semaphore_url=semaphore.address,
        host={"skynet_probe_timeout_s": 2},
    )
    assert rc != 0, out
    assert "The run's state is running" in out, out
    assert "the deadline passed" in out, out
    semaphore.server_close()
    skynet.server_close()


def test_two_tagged_tasks_fail_as_ambiguous(tmp_path):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[_task(4237, TAG), _task(4238, TAG)])
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address)
    assert rc != 0, out
    assert "ambiguous: 2 Semaphore tasks carry the tag" in out, out
    _no_leak(out)
    semaphore.server_close()
    skynet.server_close()


def test_a_task_tagged_for_another_run_or_step_is_not_counted(tmp_path):
    other_run = "service_onboarding:11111111-2222-4333-8444-555555555555"
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(
        rows=[
            _task(4239, f"skynet {other_run} service-validate"),
            _task(4240, f"skynet {RUN_ID} service-assess"),
            _task(4241, f"console {RUN_ID} service-validate"),
        ]
    )
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address)
    assert rc != 0, out
    assert "no Semaphore task carries the tag" in out, out
    semaphore.server_close()
    skynet.server_close()


def test_skynet_refusing_the_run_fails_before_any_task_is_read(tmp_path):
    skynet = Skynet(states=["finished"], posts=[(400, {"error": 'expected {"graph", "input"}'})])
    semaphore = Semaphore(rows=[_task(4242, TAG)])
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address)
    assert rc != 0, out
    assert "skynet did not accept the run" in out, out
    assert "run 400 - - - -" in out, out
    assert [r["method"] for r in skynet.seen] == ["POST"]
    assert semaphore.seen == []
    _no_leak(out)
    semaphore.server_close()
    skynet.server_close()


def test_a_refused_task_list_read_fails_and_names_the_status(tmp_path):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[], status=401)
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address)
    assert rc != 0, out
    assert "Semaphore's task list could not be read" in out, out
    assert "task - http401 - -" in out, out
    _no_leak(out)
    semaphore.server_close()
    skynet.server_close()


def test_an_unreachable_semaphore_fails_and_names_the_transport_error(tmp_path):
    skynet = Skynet(states=["finished"])
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=f"http://127.0.0.1:{refused_port()}")
    assert rc != 0, out
    assert "task - ConnectionRefusedError - -" in out, out
    _no_leak(out)
    skynet.server_close()


# ── trust: the Semaphore origin is verified against SSL_CERT_FILE, and nothing else ──


def test_a_tls_semaphore_is_trusted_through_ssl_cert_file(tmp_path, pki):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[_task(4243, TAG)], pki=pki)
    rc, out = _run(
        tmp_path,
        skynet=skynet,
        semaphore_url=f"https://localhost:{semaphore.server_port}",
        env_extra={"SSL_CERT_FILE": str(pki / "ca.crt")},
    )
    assert rc == 0, out
    assert "task 1 - 4243 success" in out, out
    semaphore.server_close()
    skynet.server_close()


def test_a_tls_semaphore_that_no_trust_store_holds_is_refused_by_name(tmp_path, pki):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[_task(4244, TAG)], pki=pki)
    rc, out = _run(
        tmp_path,
        skynet=skynet,
        semaphore_url=f"https://localhost:{semaphore.server_port}",
        drop_env=("SSL_CERT_FILE",),
    )
    assert rc != 0, out
    assert "task - SSLCertVerificationError - -" in out, out
    semaphore.server_close()
    skynet.server_close()


# ── dry run and refusals before any request ──────────────────────────────────


def test_a_dry_run_sends_nothing_and_says_so(tmp_path):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[_task(4245, TAG)])
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address, check=True)
    assert rc == 0, out
    assert "Nothing was sent" in out, out
    assert skynet.seen == []
    assert semaphore.seen == []
    skynet.server_close()
    semaphore.server_close()


@pytest.mark.parametrize("check", [False, True], ids=["real", "check"])
@pytest.mark.parametrize("target", ["Opa", "all", "bad_svc", "*", "missing", "x--"])
def test_a_probe_service_that_is_not_one_populated_service_group_is_refused_before_any_request(tmp_path, target, check):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[_task(4246, TAG)])
    rc, out = _run(
        tmp_path,
        skynet=skynet,
        semaphore_url=semaphore.address,
        extra_vars={"probe_service": target},
        check=check,
    )
    assert rc != 0 and "probe_service must name a skynet service_name" in out, out
    assert skynet.seen == [] and semaphore.seen == []
    skynet.server_close()
    semaphore.server_close()


def test_a_blank_probe_service_takes_the_default_opa(tmp_path):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[_task(4247, TAG)])
    rc, out = _run(tmp_path, skynet=skynet, semaphore_url=semaphore.address, extra_vars={"probe_service": ""})
    assert rc == 0, out
    [post] = [r for r in skynet.seen if r["method"] == "POST"]
    assert post["body"]["input"]["service"] == "opa"
    skynet.server_close()
    semaphore.server_close()


def test_two_skynet_hosts_are_refused_before_any_request(tmp_path):
    skynet = Skynet(states=["finished"])
    semaphore = Semaphore(rows=[_task(4248, TAG)])
    rc, out = _run(
        tmp_path,
        skynet=skynet,
        semaphore_url=semaphore.address,
        skynet_hosts={"skynet-2": {"ansible_connection": "local"}},
    )
    assert rc != 0 and "exactly one skynet_svc host" in out, out
    assert skynet.seen == [] and semaphore.seen == []
    skynet.server_close()
    semaphore.server_close()


# ── static properties ────────────────────────────────────────────────────────


def _doc():
    return playbook_yaml.load(PLAYBOOK)


def _probe_play():
    return _doc()[3]


def test_the_playbook_is_guarded_first_and_its_probe_play_targets_the_skynet_host():
    doc = _doc()
    assert doc[0].get("ansible.builtin.import_playbook") == "refuse-internal-extra-vars.yml"
    assert doc[1].get("ansible.builtin.import_playbook") == "preflight-target-group.yml"
    assert doc[1]["vars"] == {"preflight_group": "skynet_svc", "preflight_group_expected": "skynet_svc"}
    assert doc[2]["hosts"] == "localhost"
    assert doc[3]["hosts"] == "skynet_svc"


def test_the_step_is_pinned_to_the_registry_step_the_probe_names():
    registry = yaml.safe_load(REGISTRY.read_text())
    [step] = [s for s in registry["steps"] if s["id"] == "service-validate"]
    variables = _probe_play()["vars"]
    assert variables["_skynet_probe_step"] == step["id"] == "service-validate"
    assert variables["_skynet_probe_order"] == step["order"] == 14
    assert step["owner"] == "service-agent"
    assert step["executor"] == "Verify Service Health"
    assert step["reviewed"]


def test_opa_lets_service_agent_run_this_step_through_this_template():
    catalog = json.loads(DATA.read_text())["catalog"]
    assert "Verify Service Health" in catalog["workflow_steps"]["service-validate"]["templates"]
    assert "Verify Service Health" in catalog["service-agent"]["allowed_templates"]


def test_the_only_send_is_one_command_skipped_in_check_mode_with_the_script_as_argv():
    sends = [task for task in _probe_play()["tasks"] if "ansible.builtin.command" in task]
    assert len(sends) == 1
    assert sends[0]["when"] == "not ansible_check_mode"
    argv = sends[0]["ansible.builtin.command"]["argv"]
    assert argv[:6] == ["{{ _engine }}", "exec", "skynet", "python", "-I", "-c"]
    assert argv[6] == "{{ _skynet_probe_script }}"
    assert argv[7:] == [
        "{{ _probe_service }}",
        "{{ _skynet_probe_step }}",
        "{{ _skynet_probe_order }}",
        "{{ _skynet_probe_timeout_s }}",
    ]


def test_every_assertion_on_the_answer_is_skipped_in_check_mode():
    for task in _probe_play()["tasks"]:
        if "ansible.builtin.assert" in task or "ansible.builtin.set_fact" in task:
            assert task.get("when") == "not ansible_check_mode", task.get("name")


def test_no_no_log_and_no_openbao_in_the_code():
    code = yaml.safe_dump(_doc())  # the code, not the header comment
    assert "no_log" not in code
    for needle in ("openbao", "vault", "bao_"):
        assert needle not in code.lower(), needle


def test_the_script_defaults_to_skynet_and_carries_no_template_delimiter():
    script = SCRIPT.read_text()
    assert 'API_BASE = sys.argv[5] if len(sys.argv) > 5 else "http://127.0.0.1:8080"' in script
    assert 'MOVING = frozenset({"running", "interrupted"})' in script
    # The playbook passes the file's text as one argv item; a template delimiter in it would be read as a template.
    for delimiter in ("{{", "{%", "{#"):
        assert delimiter not in script, delimiter


def test_the_script_prints_exactly_two_lines_and_never_reads_the_failure_text():
    script = SCRIPT.read_text()
    assert script.count("print(") == 2
    assert '"failure"' not in script
    assert "Authorization" in script  # the bearer is sent, and only ever as a header


def test_the_target_mapping_is_the_one_skynet_applies():
    # skynet names a service's group with "-" as "_" plus "_svc" (orchestrator/service_onboarding.py, inventory_group).
    playbook = PLAYBOOK.read_text()
    assert "_probe_service | replace('-', '_') ~ '_svc'" in playbook
    assert "_probe_service is match('^[a-z][a-z0-9-]*$')" in playbook


def test_the_semaphore_template_is_dev_bound_with_one_optional_service_survey():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    template = next(t for t in templates if t["name"] == "Probe skynet Launch (Dev)")
    assert template["playbook"] == "platform/playbooks/probe-skynet-launch.yml"
    assert template["repository"] == "agent-cloud dev"
    assert "dev_variant" not in template
    [survey] = template["survey_vars"]
    assert survey["name"] == "probe_service"
    assert survey["type"] == "string"
    assert survey["required"] is False


def test_the_local_template_runs_the_same_playbook_and_names_no_repository():
    templates = yaml.safe_load(TEMPLATES_LOCAL.read_text())["templates"]
    [template] = [t for t in templates if t["name"] == "Probe skynet Launch (Local)"]
    assert template["playbook"] == "platform/playbooks/probe-skynet-launch.yml"
    assert "repository" not in template


def test_the_docs_list_the_probe():
    assert "| Probe skynet Launch (Dev) | `probe-skynet-launch.yml` |" in AGENTS.read_text()
