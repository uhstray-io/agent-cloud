"""The Semaphore history drain helper, platform/playbooks/files/semaphore_drain_template.py.

Each test runs the helper as a child process, so the exit codes and the output are what the
playbook sees. A fake Semaphore on 127.0.0.1 (an ephemeral port) serves the two routes the helper
calls, with the DELETE replies a test asks for. It answers 401 to any token but TOKEN, so a passing
run also proves the token was sent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_http import DrainingHandler

HELPER = Path(__file__).resolve().parents[2] / "platform/playbooks/files/semaphore_drain_template.py"
TOKEN = "SYNTHETIC_DRAIN_TOKEN_CANARY_4b7e"
PAGE_PATH = re.compile(r"/api/project/1/templates/(\d+)/tasks/last\?limit=200")
TASK_PATH = re.compile(r"/api/project/1/tasks/(\d+)")
STAMP = "2026-11-01T00:00:00Z"


def tasks(first_id, count, status="success"):
    return [{"id": first_id + n, "status": status, "created": STAMP} for n in range(count)]


def task(task_id, status):
    return {"id": task_id, "status": status, "created": STAMP}


class FakeSemaphore(DrainingHandler):
    """state: templates {id: [task rows, newest first]}, delete_reply {task id: (status, removed)},
    redirect {template id} (its page answers 302 to itself), requests [(method, path)]."""

    def log_message(self, _format, *_args):
        return

    def _reply(self, status, body=None, headers=()):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _admit(self):
        self.state["requests"].append((self.command, self.path))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self._admit():
            return
        match = PAGE_PATH.fullmatch(self.path)
        if not match:
            self._reply(404, {"error": "no route"})
            return
        template_id = int(match.group(1))
        if template_id in self.state["redirect"] and "moved=1" not in self.path:
            # Once only: a client that follows lands on a 200, so only the refusal can fail the run.
            location = f"http://127.0.0.1:{self.server.server_address[1]}{self.path}&moved=1"
            self._reply(302, None, headers=[("Location", location)])
            return
        rows = self.state["templates"].get(template_id)
        if rows is None:
            self._reply(404, {"error": "no template"})
            return
        self._reply(200, rows[:200])

    def do_DELETE(self):
        if not self._admit():
            return
        match = TASK_PATH.fullmatch(self.path)
        task_id = int(match.group(1)) if match else None
        status, removed = self.state["delete_reply"].get(task_id, (204, True))
        if removed:
            for rows in self.state["templates"].values():
                rows[:] = [row for row in rows if row["id"] != task_id]
        self._reply(status, None if status == 204 else {"error": "reply"})


@pytest.fixture
def fake():
    servers = []

    def start(templates, delete_reply=None, redirect=()):
        state = {
            "requests": [],
            "templates": templates,
            "delete_reply": dict(delete_reply or {}),
            "redirect": set(redirect),
        }
        handler = type("Handler", (FakeSemaphore,), {"state": state})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(httpd)
        port = httpd.server_address[1]
        return SimpleNamespace(state=state, port=port, url=f"http://127.0.0.1:{port}")

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def drain(tmp_path, url, *extra, token_text=TOKEN, env=None, timeout=60):
    token_file = tmp_path / "token"
    token_file.write_text(token_text)
    # NO_PROXY is blanked so a proxy in the environment is never bypassed for 127.0.0.1.
    child_env = {**os.environ, "NO_PROXY": "", "no_proxy": "", **(env or {})}
    return subprocess.run(
        [sys.executable, str(HELPER), "--url", url, "--project", "1", "--token-file", str(token_file), *extra],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=child_env,
        check=False,
    )


def requests_of(srv, method):
    return [path for verb, path in srv.state["requests"] if verb == method]


def test_drains_every_task_over_three_pages_then_stops(fake, tmp_path):
    srv = fake({7: tasks(1000, 450)})
    result = drain(tmp_path, srv.url, "--template-id", "7")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"dry_run": False, "templates": {"7": {"deleted": 450, "pages": 3}}}
    assert srv.state["templates"][7] == []
    assert len(requests_of(srv, "DELETE")) == 450
    # The pre-scan read, three pages of tasks, and the empty page that ends the drain.
    assert len(requests_of(srv, "GET")) == 5
    assert "450 deleted so far" in result.stderr


def test_a_task_already_gone_is_not_an_error(fake, tmp_path):
    srv = fake({7: tasks(1000, 3)}, delete_reply={1001: (404, True)})
    result = drain(tmp_path, srv.url, "--template-id", "7")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["templates"] == {"7": {"deleted": 2, "pages": 1}}
    assert srv.state["templates"][7] == []


def test_a_400_for_a_task_the_list_no_longer_carries_is_gone(fake, tmp_path):
    # Semaphore answers 400 for a missing task too (plan/development/18, "Why a presence check").
    srv = fake({7: tasks(1000, 3)}, delete_reply={1001: (400, True)})
    result = drain(tmp_path, srv.url, "--template-id", "7")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["templates"] == {"7": {"deleted": 2, "pages": 1}}


def test_a_400_for_a_task_still_listed_is_a_failure(fake, tmp_path):
    srv = fake({7: tasks(1000, 3)}, delete_reply={1001: (400, False)})
    result = drain(tmp_path, srv.url, "--template-id", "7")
    assert result.returncode == 4, result.stderr
    assert "1001" in result.stderr


@pytest.mark.parametrize(
    "status",
    ["running", "waiting", "starting", "stopping", "waiting_confirmation", "confirmed", "rejected", "never-seen", None],
)
def test_a_running_or_queued_task_on_any_template_refuses_before_any_delete(fake, tmp_path, status):
    srv = fake({7: tasks(1000, 5), 8: tasks(2000, 2) + [task(2999, status)]})
    result = drain(tmp_path, srv.url, "--template-id", "7", "--template-id", "8")
    assert result.returncode == 3, result.stderr
    assert "2999" in result.stderr
    assert result.stdout == ""
    assert requests_of(srv, "DELETE") == []
    assert len(srv.state["templates"][7]) == 5


def test_a_task_that_starts_running_mid_drain_stops_the_drain(fake, tmp_path):
    rows = tasks(1000, 200) + [task(1500, "running")] + tasks(1200, 10)
    srv = fake({7: rows})
    result = drain(tmp_path, srv.url, "--template-id", "7")
    assert result.returncode == 3, result.stderr
    assert "1500" in result.stderr
    assert len(requests_of(srv, "DELETE")) == 200


def test_dry_run_reads_the_first_page_and_deletes_nothing(fake, tmp_path):
    srv = fake({7: tasks(1000, 450)})
    result = drain(tmp_path, srv.url, "--template-id", "7", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "dry_run": True,
        "templates": {"7": {"first_page": 200, "capped": True, "deleted": 0, "pages": 0}},
    }
    assert requests_of(srv, "DELETE") == []
    assert len(srv.state["templates"][7]) == 450


@pytest.mark.parametrize(
    "pattern",
    [
        "http://localhost:{port}",
        "http://example.com:{port}",
        "https://127.0.0.1:{port}",
        "http://203.0.113.5:{port}",
        "http://127.0.0.1:{port}/api",
        "http://127.0.0.1:{port}/",
        "http://127.0.0.1:99999",
    ],
)
def test_a_non_loopback_destination_is_refused_before_any_request(fake, tmp_path, pattern):
    srv = fake({7: tasks(1000, 3)})
    result = drain(tmp_path, pattern.format(port=srv.port), "--template-id", "7")
    assert result.returncode == 2, result.stderr
    assert srv.state["requests"] == []


def test_the_token_is_never_printed(fake, tmp_path):
    success = fake({7: tasks(1000, 2)})
    ok = drain(tmp_path, success.url, "--template-id", "7")
    failure = fake({7: tasks(1000, 3)}, delete_reply={1001: (500, False)})
    bad = drain(tmp_path, failure.url, "--template-id", "7")
    running = fake({7: tasks(1000, 2) + [task(1500, "running")]})
    refused = drain(tmp_path, running.url, "--template-id", "7")
    for result in (ok, bad, refused):
        assert TOKEN not in result.stdout
        assert TOKEN not in result.stderr
    assert (ok.returncode, bad.returncode, refused.returncode) == (0, 4, 3)


def test_max_pages_stops_a_drain_that_never_empties(fake, tmp_path):
    srv = fake({7: tasks(1000, 3)}, delete_reply={1000: (404, False), 1001: (404, False), 1002: (404, False)})
    result = drain(tmp_path, srv.url, "--template-id", "7", "--max-pages", "3", timeout=30)
    assert result.returncode == 5, result.stderr
    assert "--max-pages 3" in result.stderr


def test_an_empty_token_file_is_refused_before_any_request(fake, tmp_path):
    srv = fake({7: tasks(1000, 3)})
    result = drain(tmp_path, srv.url, "--template-id", "7", token_text=" \n")
    assert result.returncode == 2, result.stderr
    assert srv.state["requests"] == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--template-id", "0"],
        ["--template-id", "07"],
        ["--template-id", "7", "--template-id", "7"],
        ["--template-id", "7", "--max-pages", "0"],
    ],
)
def test_bad_arguments_are_refused_before_any_request(fake, tmp_path, extra):
    srv = fake({7: tasks(1000, 3)})
    result = drain(tmp_path, srv.url, *extra)
    assert result.returncode == 2, result.stderr
    assert srv.state["requests"] == []


def test_a_proxy_in_the_environment_is_not_used(fake, tmp_path):
    srv = fake({7: tasks(1000, 2)})
    env = {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1"}
    result = drain(tmp_path, srv.url, "--template-id", "7", env=env)
    assert result.returncode == 0, result.stderr
    assert srv.state["templates"][7] == []


def test_a_redirect_is_refused_so_the_token_does_not_follow_it(fake, tmp_path):
    srv = fake({7: tasks(1000, 2)}, redirect=[7])
    result = drain(tmp_path, srv.url, "--template-id", "7")
    assert result.returncode == 6, result.stderr
    assert requests_of(srv, "DELETE") == []
    # The token never reaches the redirect target: no request to it was made.
    assert not any("moved=1" in path for path in requests_of(srv, "GET"))
