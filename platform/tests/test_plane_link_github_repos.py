"""link_github_repos.py: every declared Plane project gets its GitHub issue-sync link, create-only.

The script runs joined after plane_session.py inside plane-api, as sync-plane-projects.yml Play 3
does. Here it runs the same way against one stub that serves both Plane's API and silo's routes,
so the payload is checked against the record Plane's own form creates, and the refusals (an org
not connected, a link pointing elsewhere, a repository the App cannot see) fail without writing.
"""

import http.server
import json
import pathlib
import subprocess
import sys
import threading
import urllib.parse

import pytest
from ansible_python import needs_ansible
from fake_http import DrainingHandler

DEPLOY = pathlib.Path(__file__).resolve().parents[1] / "services/plane/deployment"
SOURCE = (DEPLOY / "plane_session.py").read_text() + "\n" + (DEPLOY / "link_github_repos.py").read_text()
W, C = "ws-1", "conn-1"
REPOS = [
    {"id": 11, "name": "agent-cloud", "full_name": "uhstray-io/agent-cloud"},
    {"id": 22, "name": "huhhb", "full_name": "uhstray-io/huhhb"},
]
STATES = [{"id": "s-b", "name": "Backlog", "group": "backlog"}, {"id": "s-d", "name": "Done", "group": "completed"}]


class Stub(DrainingHandler):
    state = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body=None, location=None, cookie=None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        data = json.dumps(body).encode() if body is not None else b""
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        s, path = self.state, urllib.parse.urlsplit(self.path).path
        if path.startswith("/silo/"):
            s["silo_cookies"].append(self.headers.get("Cookie", ""))
        routes = {
            "/auth/get-csrf-token/": {"csrf_token": "tok"},
            "/api/users/me/workspaces/": [{"id": W, "slug": "uhstray"}],
            "/api/workspaces/uhstray/projects/": s["projects"],
            f"/silo/api/github/auth/organization-status/{W}": s["orgs"],
            f"/silo/api/github/{W}/repos": REPOS,
            f"/silo/api/entity-connections/{W}/{C}": s["existing"],
        }
        if path in routes:
            cookie = "csrftoken=tok; Path=/" if path == "/auth/get-csrf-token/" else None
            return self._send(200, routes[path], cookie=cookie)
        if path.startswith("/api/workspaces/uhstray/projects/") and path.endswith("/states/"):
            return self._send(200, STATES)
        self._send(404, {})

    def do_POST(self):
        s, path = self.state, urllib.parse.urlsplit(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        if path == "/auth/sign-in/":
            return self._send(302, location="/", cookie="session-id=sess; Path=/")
        if path == f"/silo/api/entity-connections/{W}/{C}":
            s["created"].append(json.loads(raw))
            return self._send(200, {"id": "new"})
        self._send(404, {})


@pytest.fixture
def stub():
    Stub.state = {
        "projects": [{"identifier": "AC", "id": "p-ac"}, {"identifier": "HUHHB", "id": "p-hu"}],
        "orgs": [{"id": C}],
        "existing": [],
        "created": [],
        "silo_cookies": [],
    }
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Stub.state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def run(base, apply, projects=None):
    cfg = {
        "base": base,
        "host": "plane.example.test",
        "origin": "https://plane.example.test",
        "email": "a@example.test",
        "password": "pw-not-printed",
        "workspace_slug": "uhstray",
        "silo_base": base + "/silo",
        "apply": apply,
        "projects": projects
        or [
            {"identifier": "AC", "repo": "uhstray-io/agent-cloud"},
            {"identifier": "HUHHB", "repo": "uhstray-io/huhhb"},
        ],
    }
    proc = subprocess.run(
        [sys.executable, "-I", "-c", SOURCE],
        input=json.dumps(cfg) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "pw-not-printed" not in proc.stdout + proc.stderr
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


def test_dry_run_reports_and_writes_nothing(stub):
    state, base = stub
    rc, out = run(base, apply=False)
    assert rc == 0 and out["would_link"] == ["AC", "HUHHB"] and state["created"] == []


def test_apply_creates_the_record_plane_s_form_creates(stub):
    state, base = stub
    rc, out = run(base, apply=True)
    assert rc == 0 and out["linked"] == ["AC", "HUHHB"]
    ac = state["created"][0]
    assert ac == {
        "workspace_id": W,
        "workspace_slug": "uhstray",
        "project_id": "p-ac",
        "workspace_connection_id": C,
        "entity_id": "11",
        "entity_type": "GITHUB",
        "entity_slug": "uhstray-io/agent-cloud",
        "entity_data": REPOS[0],
        "config": {
            "states": {
                "issueEventMapping": {
                    "ISSUE_OPEN": {"id": "s-b", "name": "Backlog"},
                    "ISSUE_CLOSED": {"id": "s-d", "name": "Done"},
                }
            },
            "allowBidirectionalSync": True,
        },
        "type": "PROJECT_ISSUE_SYNC",
    }
    # Silo authenticates by forwarding the session cookie, so every silo call must carry it.
    assert state["silo_cookies"] and all("session-id=sess" in c for c in state["silo_cookies"])


def test_an_existing_link_to_the_declared_repo_is_left_alone(stub):
    state, base = stub
    state["existing"] = [{"type": "PROJECT_ISSUE_SYNC", "project_id": "p-ac", "entity_id": "11"}]
    rc, out = run(base, apply=True)
    assert rc == 0 and out["present"] == ["AC"] and out["linked"] == ["HUHHB"]
    assert [c["project_id"] for c in state["created"]] == ["p-hu"]


@pytest.mark.parametrize(
    "existing,projects",
    [
        (
            [
                {
                    "type": "PROJECT_ISSUE_SYNC",
                    "project_id": "p-ac",
                    "entity_id": "22",
                    "entity_slug": "uhstray-io/huhhb",
                }
            ],
            None,
        ),
        ([{"type": "PROJECT_ISSUE_SYNC", "project_id": "p-other", "entity_id": "11"}], None),
        ([], [{"identifier": "AC", "repo": "uhstray-io/not-installed"}]),
    ],
    ids=["project-linked-elsewhere", "repo-linked-to-another-project", "repo-not-visible"],
)
def test_conflicts_are_refused_never_re_pointed(stub, existing, projects):
    state, base = stub
    state["existing"] = existing
    rc, out = run(base, apply=True, projects=projects)
    assert rc == 1 and out["conflicts"] and "conflict" in out["error"]
    assert all(c["project_id"] != "p-ac" for c in state["created"])


def test_an_unconnected_organization_is_refused_before_any_write(stub):
    state, base = stub
    state["orgs"] = []
    rc, out = run(base, apply=True)
    assert rc == 1 and "not connected" in out["error"] and state["created"] == []


def test_a_project_not_yet_in_plane_is_reported_not_failed(stub):
    state, base = stub
    state["projects"] = [{"identifier": "AC", "id": "p-ac"}]
    rc, out = run(base, apply=True)
    assert rc == 0 and out["missing_projects"] == ["HUHHB"] and out["linked"] == ["AC"]


PLAYBOOK = pathlib.Path(__file__).resolve().parents[1] / "playbooks/sync-plane-projects.yml"


def _judging_tasks():
    import yaml

    play = next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if p.get("name", "").startswith("Link the declared"))
    block = next(t for t in play["tasks"] if "block" in t)["block"]
    keep = ("Keep only the link summary", "Links converged", "Report")
    return [t for t in block if t.get("name") in keep]


def _result(stdout, rc=0):
    return {"rc": rc, "stdout_lines": stdout.splitlines()}


@needs_ansible
@pytest.mark.parametrize(
    "facts,ok,expect",
    [
        (
            {
                "_link_plan": _result('{"would_link": ["AC"], "present": [], "missing_projects": [], "conflicts": []}'),
                "_link_apply": _result(
                    '{"linked": ["AC"], "present": ["HUHHB"], "missing_projects": [], "conflicts": []}'
                ),
            },
            True,
            "Linked: AC; already linked HUHHB",
        ),
        (
            {
                "_link_plan": _result(
                    '{"would_link": ["AC"], "present": [], "missing_projects": ["X"], "conflicts": []}'
                ),
                "_link_apply": {"skipped": True},
            },
            True,
            "already linked none; not yet in Plane: X",
        ),
        (
            {
                "_link_plan": _result('{"would_link": [], "present": [], "missing_projects": [], "conflicts": []}'),
                "_link_apply": _result(
                    '{"error": "some declared links conflict with what Plane holds", "linked": ["AC"], '
                    '"conflicts": ["HUHHB: x is already linked to another project"]}',
                    rc=1,
                ),
            },
            False,
            "conflicts: HUHHB: x is already linked to another project; linked before the failure: AC",
        ),
        (
            {"_link_plan": _result("Traceback", rc=1), "_link_apply": {"skipped": True}},
            False,
            "printed no summary (exit 1)",
        ),
    ],
    ids=["applied", "planned-only", "conflict", "no-summary"],
)
def test_the_play_judges_and_reports_the_link_summary(tmp_path, facts, ok, expect):
    import yaml

    (tmp_path / "play.yml").write_text(
        yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": facts, "tasks": _judging_tasks()}])
    )
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(tmp_path / "play.yml")],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert (proc.returncode == 0) is ok, proc.stdout[-1500:]
    assert expect in proc.stdout
