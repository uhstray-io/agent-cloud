"""probe-launch-gate.yml and files/launch-gate-allow-probe.py: the launch gate's allow-path probe (openspec change
skynet-launch-containment, task 5.5).

The playbook runs for real through ansible-playbook on localhost, under harness_sandbox. Its `podman exec launch-gate`
is replaced by a fake engine: a python file in the test directory that accepts exactly the playbook's
`exec launch-gate python ...`, appends the test listener's address and CA (the two arguments production does not
pass), and execs the probe script as the playbook passed it. The gate is a synthetic TLS listener that answers each
POST /v1/launch from a script and records what it received. Everything else is production code: the script, the
playbook's argv, its guards and its assertions. The caller token is synthetic and reaches the script only through the
environment, as it does in production.
"""

import json
import re
import ssl
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml
from fake_http import DrainingHandler, refused_port
from test_agw_client_tls_probe import _ca, _leaf

REPO = playbook_yaml.REPO
PLAYBOOK = REPO / "platform/playbooks/probe-launch-gate.yml"
SCRIPT = REPO / "platform/playbooks/files/launch-gate-allow-probe.py"
DEPLOY = REPO / "platform/playbooks/deploy-launch-gate.yml"
DATA = REPO / "platform/services/opa/deployment/policies/agentcloud/data.json"
TEMPLATE = "Snapshot Service Assessment (Dev)"
TLS_NAME = "localhost"
# Synthetic. The playbook reads it from the container environment; it is not a credential.
TOKEN = "synthetic-probe-caller-token-not-a-credential"
ENV_TOKEN = "LAUNCH_GATE_CALLER_TOKEN_SKYNET"


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    directory = tmp_path_factory.mktemp("launch-gate-probe-pki")
    _ca(directory, "ca")
    _leaf(directory, "ca", "server", "DNS:localhost", "serverAuth", directory / "leaves/server")
    return directory


class Gate(ThreadingHTTPServer):
    """A launch gate's listener for the probe. Each POST /v1/launch takes the next entry of `script`: a status,
    optionally with an error code (`403:template_not_allowed`). `201` makes a task (ids count up from 4200) with the
    message the gate builds. Once the script is used up every request is answered 500, so an extra request cannot
    pass as an answer. Every request is recorded in `seen`."""

    daemon_threads = True

    def __init__(self, pki: Path, script: list[str]):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ctx.load_cert_chain(pki / "leaves/server/current/cert.pem", pki / "leaves/server/current/key.pem")
        self.script = list(script)
        self.seen: list[dict] = []
        self.next_task = 4200

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        pass

    def answer(self, body: dict) -> tuple[int, dict]:
        entry = self.script.pop(0) if self.script else "500:script_exhausted"
        status, _, error = entry.partition(":")
        if status == "201":
            task_id, self.next_task = self.next_task, self.next_task + 1
            return 201, {
                "task_id": task_id,
                "message": f"skynet {body['workflow_id']} {body['step']}",
                "decision": {"allowed": True, "reason": "allowed by agent policy"},
            }
        return int(status), {"error": error or "refused", "detail": "synthetic refusal"}


class _Handler(DrainingHandler):
    def setup(self):
        self.request.do_handshake()
        super().setup()

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
        request = {"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(data)}
        self.server.seen.append(request)
        if self.path != "/v1/launch":
            status, payload = 404, {"error": "not_found"}
        elif request["auth"] != f"Bearer {TOKEN}":
            status, payload = 401, {"error": "unauthorized", "detail": "missing or invalid bearer token"}
        else:
            status, payload = self.server.answer(request["body"])
        reply = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def gate(pki):
    started: list[Gate] = []

    def start(script: list[str]) -> Gate:
        server = Gate(pki, script)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def _engine(tmp: Path, address: str, cafile: Path) -> Path:
    """The fake `podman`: it accepts exactly the playbook's `exec launch-gate python ...` and runs the script with the
    listener's address and CA appended."""
    python = sys.executable
    engine = tmp / "fake-engine"
    engine.write_text(
        f"#!{python}\n"
        "import os\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        'if args[:3] != ["exec", "launch-gate", "python"]:\n'
        '    sys.exit("fake engine: the playbook must exec python in launch-gate")\n'
        f"os.execv({python!r}, [{python!r}, *args[3:], {address!r}, {str(cafile)!r}])\n"
    )
    engine.chmod(0o755)
    return engine


def _run(
    tmp: Path,
    *,
    address: str,
    cafile: Path,
    extra_vars: dict | None = None,
    host: dict | None = None,
    gate_hosts: dict | None = None,
    check: bool = False,
):
    engine = _engine(tmp, address, cafile)
    host_vars = {
        "ansible_connection": "local",
        "ansible_python_interpreter": sys.executable,
        "container_engine": str(engine),
        "launch_gate_tls_server_name": TLS_NAME,
        **(host or {}),
    }
    inventory = {
        "all": {
            "children": {
                "launch_gate_svc": {"hosts": {"gate": host_vars, **(gate_hosts or {})}},
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
    env = {**harness_sandbox.env_for(tmp), "ANSIBLE_STDOUT_CALLBACK": "default", ENV_TOKEN: TOKEN}
    result = harness_sandbox.run(cmd, tmp, cwd=REPO, env=env, timeout=300)
    return result.returncode, result.stdout + result.stderr


def _no_leak(out: str):
    assert TOKEN not in out, out


# ── the allow path, the duplicate refusal, the wire ──────────────────────────


def test_the_first_launch_is_allowed_and_the_repeat_is_refused_already_running(tmp_path, pki, gate):
    server = gate(["201", "409:already_running"])
    rc, out = _run(tmp_path, address=f"127.0.0.1:{server.server_port}", cafile=pki / "ca.crt")
    assert rc == 0, out
    first, repeat = server.seen
    workflow_id = first["body"]["workflow_id"]
    assert re.fullmatch(r"probe-\d+", workflow_id), workflow_id
    assert f"first 201 - 4200 skynet {workflow_id} service-assess" in out, out
    assert "repeat 409 already_running -" in out, out
    assert [request["path"] for request in server.seen] == ["/v1/launch", "/v1/launch"]
    assert first["auth"] == repeat["auth"] == f"Bearer {TOKEN}"
    assert (
        first["body"]
        == repeat["body"]
        == {
            "agent": "service-agent",
            "template": TEMPLATE,
            "workflow_id": workflow_id,
            "step": "service-assess",
            "dry_run": True,
            "survey": {"target_service": "opa_svc"},
        }
    )
    _no_leak(out)


def test_a_repeat_that_is_also_accepted_is_inconclusive_and_fails(tmp_path, pki, gate):
    server = gate(["201", "201"])
    rc, out = _run(tmp_path, address=f"127.0.0.1:{server.server_port}", cafile=pki / "ca.crt")
    assert rc != 0, out
    assert "inconclusive: the first task finished before the repeat" in out, out
    assert "first task 4200, repeat task 4201" in out, out
    _no_leak(out)


@pytest.mark.parametrize(
    "entry, expected",
    [
        pytest.param("403:template_not_allowed", "first 403 template_not_allowed -", id="off-allowlist"),
        pytest.param("409:already_running", "first 409 already_running -", id="running-before-the-first"),
        pytest.param("400:dry_run_unverified", "first 400 dry_run_unverified -", id="unverified-semaphore"),
        pytest.param("401:unauthorized", "first 401 unauthorized -", id="unauthenticated"),
    ],
)
def test_a_refused_first_launch_fails_the_run_and_names_the_refusal(tmp_path, pki, gate, entry, expected):
    server = gate([entry, "409:already_running"])
    rc, out = _run(tmp_path, address=f"127.0.0.1:{server.server_port}", cafile=pki / "ca.crt")
    assert rc != 0, out
    assert "did not answer the allow path" in out, out
    assert expected in out, out
    _no_leak(out)


def test_a_gate_that_is_not_listening_fails_the_run_with_the_transport_error(tmp_path, pki):
    rc, out = _run(tmp_path, address=f"127.0.0.1:{refused_port()}", cafile=pki / "ca.crt")
    assert rc != 0, out
    assert "first - ConnectionRefusedError - -" in out, out
    _no_leak(out)


def test_a_dry_run_sends_nothing_and_says_so(tmp_path, pki, gate):
    server = gate(["201", "409:already_running"])
    rc, out = _run(tmp_path, address=f"127.0.0.1:{server.server_port}", cafile=pki / "ca.crt", check=True)
    assert rc == 0, out
    assert "Nothing was sent" in out, out
    assert server.seen == []


def test_a_blank_target_takes_the_default_opa_svc(tmp_path, pki, gate):
    server = gate(["201", "409:already_running"])
    rc, out = _run(
        tmp_path,
        address=f"127.0.0.1:{server.server_port}",
        cafile=pki / "ca.crt",
        extra_vars={"probe_target_service": ""},
    )
    assert rc == 0, out
    assert server.seen[0]["body"]["survey"] == {"target_service": "opa_svc"}


# ── refusals before any request ──────────────────────────────────────────────


@pytest.mark.parametrize("target", ["all", "opa", "*_svc", "missing_svc"])
def test_a_target_that_is_not_one_populated_service_group_is_refused_before_any_request(tmp_path, pki, gate, target):
    server = gate(["201", "409:already_running"])
    rc, out = _run(
        tmp_path,
        address=f"127.0.0.1:{server.server_port}",
        cafile=pki / "ca.crt",
        extra_vars={"probe_target_service": target},
    )
    assert rc != 0 and "probe_target_service must name one populated" in out, out
    assert server.seen == []


def test_the_target_guard_also_refuses_in_a_dry_run(tmp_path, pki, gate):
    server = gate(["201", "409:already_running"])
    rc, out = _run(
        tmp_path,
        address=f"127.0.0.1:{server.server_port}",
        cafile=pki / "ca.crt",
        extra_vars={"probe_target_service": "all"},
        check=True,
    )
    assert rc != 0 and "probe_target_service must name one populated" in out, out
    assert server.seen == []


@pytest.mark.parametrize(
    "callers",
    [
        pytest.param({"skynet": ["infra-agent"]}, id="agent-not-listed"),
        pytest.param({"o11y-agent": ["service-agent"]}, id="no-skynet"),
        pytest.param({"skynet": "service-agent"}, id="string-not-a-list"),
    ],
)
def test_a_caller_map_that_does_not_let_skynet_act_as_service_agent_is_refused(tmp_path, pki, gate, callers):
    server = gate(["201", "409:already_running"])
    rc, out = _run(
        tmp_path,
        address=f"127.0.0.1:{server.server_port}",
        cafile=pki / "ca.crt",
        host={"launch_gate_callers": callers},
    )
    assert rc != 0 and "does not list that agent for that caller" in out, out
    assert server.seen == []


def test_two_gate_hosts_are_refused_before_any_request(tmp_path, pki, gate):
    server = gate(["201", "409:already_running"])
    rc, out = _run(
        tmp_path,
        address=f"127.0.0.1:{server.server_port}",
        cafile=pki / "ca.crt",
        gate_hosts={"gate-2": {"ansible_connection": "local"}},
    )
    assert rc != 0 and "exactly one launch_gate_svc host" in out, out
    assert server.seen == []


# ── static properties ────────────────────────────────────────────────────────


def _probe_play():
    return playbook_yaml.load(PLAYBOOK)[3]


def test_the_playbook_is_guarded_first_and_its_probe_play_targets_the_gate():
    doc = playbook_yaml.load(PLAYBOOK)
    assert doc[0].get("ansible.builtin.import_playbook") == "refuse-internal-extra-vars.yml"
    assert doc[1].get("ansible.builtin.import_playbook") == "preflight-target-group.yml"
    assert doc[2]["hosts"] == "localhost"
    assert doc[3]["hosts"] == "launch_gate_svc"


def test_the_probe_identity_and_the_step_are_fixed():
    variables = _probe_play()["vars"]
    assert variables["_launch_gate_probe_caller"] == "skynet"
    assert variables["_launch_gate_probe_agent"] == "service-agent"
    assert variables["_launch_gate_probe_template"] == TEMPLATE
    assert variables["_launch_gate_probe_step"] == "service-assess"


def test_the_step_is_a_registry_step_service_agent_runs_this_template_as():
    catalog = json.loads(DATA.read_text())["catalog"]
    step = catalog["workflow_steps"]["service-assess"]
    assert step["owner"] == "service-agent"
    assert "Snapshot Service Assessment" in step["templates"]
    assert "Snapshot Service Assessment" in catalog["service-agent"]["allowed_templates"]


def test_the_only_send_is_one_exec_skipped_in_check_mode_with_the_script_as_argv():
    sends = [task for task in _probe_play()["tasks"] if "ansible.builtin.command" in task]
    assert len(sends) == 1
    assert sends[0]["when"] == "not ansible_check_mode"
    argv = sends[0]["ansible.builtin.command"]["argv"]
    assert argv[:6] == ["{{ _engine }}", "exec", "launch-gate", "python", "-I", "-c"]
    assert argv[6] == "{{ _launch_gate_script }}"
    assert argv[7:] == [
        "{{ _launch_gate_tls_name }}",
        "{{ _launch_gate_probe_caller }}",
        "{{ _launch_gate_probe_agent }}",
        "{{ _launch_gate_probe_template }}",
        "{{ _probe_target }}",
        "{{ _launch_gate_workflow_id }}",
        "{{ _launch_gate_probe_step }}",
    ]


def test_no_no_log_and_no_openbao_in_the_code():
    code = yaml.safe_dump(playbook_yaml.load(PLAYBOOK))  # the code, not the header comment
    assert "no_log" not in code
    for needle in ("openbao", "vault", "bao_"):
        assert needle not in code.lower(), needle


def test_the_script_has_the_production_defaults_and_survives_the_playbook_lookup():
    script = SCRIPT.read_text()
    assert 'ADDRESS = sys.argv[8] if len(sys.argv) > 8 else "127.0.0.1:8443"' in script
    assert 'CAFILE = sys.argv[9] if len(sys.argv) > 9 else "/certs/step-ca-bundle.crt"' in script
    deploy = DEPLOY.read_text()
    assert "/certs/step-ca-bundle.crt" in deploy and '("127.0.0.1", 8443)' in deploy
    # The playbook passes the file's text as one argv item; a template delimiter in it would be read as a template.
    for delimiter in ("{{", "{%", "{#"):
        assert delimiter not in script, delimiter


def test_the_caller_default_is_the_deploy_default():
    pattern = re.compile(r"\{'skynet': \[[^\]]*\]\}")
    here, there = pattern.search(PLAYBOOK.read_text()), pattern.search(DEPLOY.read_text())
    assert here and there and here.group(0) == there.group(0)


def test_the_semaphore_template_is_dev_bound_with_one_optional_target_survey():
    templates = yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(t for t in templates if t["name"] == "Probe launch-gate (Dev)")
    assert template["playbook"] == "platform/playbooks/probe-launch-gate.yml"
    assert template["repository"] == "agent-cloud dev"
    assert "dev_variant" not in template
    [survey] = template["survey_vars"]
    assert survey["name"] == "probe_target_service"
    assert survey["type"] == "string"
    assert survey["required"] is False


def test_the_docs_list_the_probe():
    assert "| Probe launch-gate (Dev) | `probe-launch-gate.yml` |" in (REPO / "AGENTS.md").read_text()
    assert "probe-launch-gate.yml" in (REPO / "platform/services/launch-gate/deployment/CLAUDE.md").read_text()
    tasks = (REPO / "plan/development/openspec/changes/skynet-launch-containment/tasks.md").read_text()
    assert "(allow path: `Probe launch-gate (Dev)`)" in tasks
