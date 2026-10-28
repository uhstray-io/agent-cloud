"""The shared Loki push task (production-internal-ca task 6.0) and the collector's use of it.

Structural half: the conformance collector pushes through tasks/push-loki-lines.yml and
carries no inline push of its own. Behavioural half: real ansible-playbook runs against a
loopback HTTP server prove the request (path, JSON body), the 204 contract, that the CALLER's
push_loki_must_succeed decides whether a failed push fails the run, and that --check sends
nothing.
"""

import copy
import json
import os
import subprocess
import threading
from http.server import HTTPServer
from pathlib import Path

import playbook_yaml
import pytest
import yaml
from ansible_python import needs_ansible
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/push-loki-lines.yml"
COLLECTOR = REPO / "platform/playbooks/collect-service-conformance.yml"
PUSH_PATH = "/loki/api/v1/push"
STREAMS = [{"stream": {"job": "fixture", "step": "s1"}, "values": [["1700000000000000000", "line one"]]}]


def _collector_tasks() -> list[dict]:
    return playbook_yaml.plays(COLLECTOR)[0]["tasks"]


def _local_loki_task() -> dict:
    return next(t for t in _collector_tasks() if t.get("name") == "Local Loki: one line per step result")


def test_collector_includes_the_shared_push_task():
    task = _local_loki_task()
    assert task["ansible.builtin.include_tasks"] == "tasks/push-loki-lines.yml"
    assert task["vars"] == {
        "push_loki_url": "{{ collector_loki_url }}",
        "push_loki_streams": "{{ _agg.loki_streams }}",
        "push_loki_must_succeed": False,  # an unreachable Loki never fails the collector
    }
    assert task["when"] == [
        "not ansible_check_mode",
        "local_mode | default(false) | bool",
        "collector_loki_url is defined",
        "_agg.loki_streams | length > 0",
    ]


def test_collector_carries_no_inline_loki_push():
    text = COLLECTOR.read_text()
    assert PUSH_PATH not in text
    for task in _collector_tasks():
        uri = task.get("ansible.builtin.uri") or task.get("uri") or {}
        assert "loki" not in str(uri.get("url", "")).lower(), task.get("name")
    # The report uses the bounded status snapshot saved before the final receipt push.
    assert "_conformance_loki_status" in next(t for t in _collector_tasks() if t.get("name") == "Report")[
        "ansible.builtin.debug"]["msg"]["loki"]


def test_task_is_the_one_place_a_playbook_pushes_to_loki():
    tasks = yaml.safe_load(TASK.read_text())
    post = next(t for t in tasks if "ansible.builtin.uri" in t)["ansible.builtin.uri"]
    assert post["url"] == "{{ push_loki_url }}" + PUSH_PATH
    assert post["method"] == "POST"
    assert post["body_format"] == "json"
    assert post["body"] == {"streams": "{{ push_loki_streams }}"}
    assert post["status_code"] == [204]


class _Loki(DrainingHandler):
    status = 204
    statuses = []
    requests: list = []

    def do_POST(self):  # noqa: N802 — http.server's handler name
        length = int(self.headers.get("Content-Length", 0))
        type(self).requests.append({
            "path": self.path,
            "content_type": self.headers.get("Content-Type"),
            "body": json.loads(self.rfile.read(length) or b"null"),
        })
        status = type(self).statuses.pop(0) if type(self).statuses else type(self).status
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class _OtlpUnknownAck(DrainingHandler):
    requests: list = []

    def do_POST(self):  # noqa: N802 — http.server's handler name
        length = int(self.headers.get("Content-Length", 0))
        type(self).requests.append({"path": self.path, "body": json.loads(self.rfile.read(length) or b"null")})
        body = b"private invalid OTLP response"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def loki():
    handler = type("Loki", (_Loki,), {"status": 204, "statuses": [], "requests": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handler.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield handler
    server.shutdown()
    server.server_close()


def _run(tmp_path: Path, tasks: list, vars_: dict, *args: str) -> subprocess.CompletedProcess:
    play = [{"name": "push fixture", "hosts": "localhost", "connection": "local",
             "gather_facts": False, "vars": vars_, "tasks": tasks}]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))  # JSON is valid YAML
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_STDOUT_CALLBACK="default",
               ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(path), *args],
                          cwd=REPO, env=env, text=True, capture_output=True, timeout=60, check=False)


def _include(**vars_) -> list:
    return [
        {"name": "push", "ansible.builtin.include_tasks": str(TASK), "vars": vars_},
        {"name": "show", "ansible.builtin.debug": {
            "msg": "STATUS={{ _push_loki_result.status | default('none') }} "
                   "SKIPPED={{ _push_loki_result is skipped }}"}},
    ]


@needs_ansible
def test_pushes_the_streams_and_reports_204(tmp_path, loki):
    result = _run(tmp_path, _include(push_loki_url=loki.url, push_loki_streams=STREAMS,
                                     push_loki_must_succeed=True), {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert loki.requests == [{"path": PUSH_PATH, "content_type": "application/json",
                              "body": {"streams": STREAMS}}]
    assert "STATUS=204" in result.stdout


@needs_ansible
@pytest.mark.parametrize("must_succeed, expected_rc", [(False, 0), (True, 2)])
def test_the_caller_decides_whether_a_failed_push_fails(tmp_path, loki, must_succeed, expected_rc):
    loki.status = 500
    result = _run(tmp_path, _include(push_loki_url=loki.url, push_loki_streams=STREAMS,
                                     push_loki_must_succeed=must_succeed), {})
    assert result.returncode == expected_rc, result.stdout + result.stderr
    assert len(loki.requests) == 1
    if not must_succeed:
        assert "STATUS=500" in result.stdout


@needs_ansible
def test_refuses_without_the_callers_failure_decision(tmp_path, loki):
    result = _run(tmp_path, _include(push_loki_url=loki.url, push_loki_streams=STREAMS), {})
    assert result.returncode != 0
    assert "push_loki_must_succeed" in result.stdout
    assert loki.requests == []


@needs_ansible
def test_check_mode_sends_nothing(tmp_path, loki):
    result = _run(tmp_path, _include(push_loki_url=loki.url, push_loki_streams=STREAMS,
                                     push_loki_must_succeed=True), {}, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert loki.requests == []
    assert "SKIPPED=True" in result.stdout


@needs_ansible
@pytest.mark.parametrize("statuses, expected_loki, expected_receipt, request_count", [
    ([204, 204], "pushed", "acknowledged", 2),
    ([500], "failed", "not_attempted", 1),
    ([204, 500], "pushed", "failed/UNKNOWN", 2),
])
def test_collector_publishes_receipt_separately_after_data_ack(
    tmp_path, loki, statuses, expected_loki, expected_receipt, request_count
):
    loki.statuses = list(statuses)
    tasks = _collector_tasks()
    by_name = {task.get("name"): task for task in tasks}
    base = copy.deepcopy(_local_loki_task())
    base["ansible.builtin.include_tasks"] = str(TASK)
    final = copy.deepcopy(by_name["Local Loki: publish collector completion receipt"])
    final["ansible.builtin.include_tasks"] = str(TASK)
    selected = [
        base,
        by_name["Classify the OTLP response without exposing its body"],
        by_name["Completion: hold the classified OTLP acknowledgement"],
        by_name["Completion: capture conformance transport result"],
        by_name["Completion: classify prior data delivery"],
        by_name["Completion: hold publication boundary"],
        by_name["Completion: build separate receipt payload"],
        by_name["Completion: hold receipt payload"],
        final,
        by_name["Completion: classify final receipt response"],
        by_name["Completion: hold final receipt result"],
        by_name["Completion: classify final receipt publication"],
        by_name["Completion: hold final receipt publication result"],
        {"name": "Report", **by_name["Report"]},
    ]
    vars_ = {
        "local_mode": True,
        "collector_loki_url": loki.url,
        "ansible_check_mode": False,
        "_completion_ready": True,
        "_agg": {"loki_streams": STREAMS, "otlp_payload": {"resourceLogs": []}, "report": {}},
        "_nb_writes": {"results": []},
        "_vm_missing": [],
        "_vm_ambiguous": [],
        "_vm_unknown": [],
        "_pick": {"stdout": '{"window_full": []}'},
        "_parser": str(REPO / "platform/workflows/service-onboarding/lib/step_results.py"),
    }
    result = _run(tmp_path, selected, vars_)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'"loki": "{expected_loki}"' in result.stdout
    assert f'"completion_receipt": "{expected_receipt}"' in result.stdout
    assert len(loki.requests) == request_count
    assert loki.requests[0]["body"] == {"streams": STREAMS}
    if request_count == 2:
        assert loki.requests[1]["body"]["streams"] == [{
            "stream": {"job": "agent-cloud-conformance", "kind": "run", "status": "success"},
            "values": [[loki.requests[1]["body"]["streams"][0]["values"][0][0],
                        '{"collection_completed":true,"version":1}']],
        }]


@needs_ansible
def test_unknown_otlp_data_ack_suppresses_the_separate_receipt_post(tmp_path):
    handler = type("OtlpUnknownAck", (_OtlpUnknownAck,), {"requests": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/logs"
        tasks = _collector_tasks()
        selected = [next(t for t in tasks if t.get("name") == name) for name in (
            "Production Alloy: deliver conformance records over OTLP/HTTP",
            "Classify the OTLP response without exposing its body",
            "Completion: hold the classified OTLP acknowledgement",
            "Completion: classify prior data delivery",
            "Completion: hold publication boundary",
            "Completion: build separate receipt payload",
            "Completion: hold receipt payload",
            "Production Alloy: publish collector completion receipt",
        )]
        payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"body": {"stringValue": "step"}}]}]}]}
        play = [{
            "name": "unknown OTLP acknowledgement suppresses the receipt",
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {
                "local_mode": False, "ansible_check_mode": False,
                "_collector_otlp_url": url, "_completion_ready": True,
                "_agg": {"loki_streams": STREAMS, "otlp_payload": payload, "report": {}},
                "_parser": str(REPO / "platform/workflows/service-onboarding/lib/step_results.py"),
            },
            "tasks": selected,
        }]
        path = tmp_path / "unknown-otlp.yml"
        path.write_text(json.dumps(play))
        env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
        env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_STDOUT_CALLBACK="default",
                   ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
        result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO,
                                env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(handler.requests) == 1
        assert handler.requests[0]["path"] == "/v1/logs"
        assert handler.requests[0]["body"] == payload
        assert "private invalid OTLP response" not in result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()


@needs_ansible
def test_collector_reports_skipped_when_it_has_no_loki(tmp_path, loki):
    include = copy.deepcopy(_local_loki_task())
    include["ansible.builtin.include_tasks"] = str(TASK)
    report = next(t for t in _collector_tasks() if t.get("name") == "Report")
    show = {"name": "loki report", "ansible.builtin.debug": {
        "msg": "LOKI=" + report["ansible.builtin.debug"]["msg"]["loki"]}}
    result = _run(tmp_path, [include, show], {
        "local_mode": False,
        "_conformance_loki_status": 0,
        "_conformance_loki_skipped": True,
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LOKI=skipped" in result.stdout
    assert loki.requests == []
