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
STATES = [
    {"id": "s-b", "name": "Backlog", "group": "backlog"},
    {"id": "s-t", "name": "Todo", "group": "unstarted"},
    {"id": "s-p", "name": "In Progress", "group": "started"},
    {"id": "s-d", "name": "Done", "group": "completed"},
    {"id": "s-c", "name": "Cancelled", "group": "cancelled"},
]


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
            pid = path.split("/")[5]
            return self._send(200, s.get("states", {}).get(pid, STATES))
        self._send(404, {})

    def do_POST(self):
        s, path = self.state, urllib.parse.urlsplit(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        if path == "/auth/sign-in/":
            return self._send(302, location="/", cookie="session-id=sess; Path=/")
        if path == f"/silo/api/entity-connections/{W}/{C}":
            body = json.loads(raw)
            if (body["type"], body["project_id"]) in s.get("refuse", ()):
                return self._send(500, {})
            s["created"].append(body)
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
    assert rc == 0 and out["would_link"] == ["AC", "HUHHB"] and out["pr_would_link"] == ["AC", "HUHHB"]
    assert state["created"] == []


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
                    "ISSUE_OPEN": {"id": "s-t", "name": "Todo"},
                    "ISSUE_CLOSED": {"id": "s-d", "name": "Done"},
                }
            },
            "allowBidirectionalSync": True,
        },
        "type": "PROJECT_ISSUE_SYNC",
    }
    # The pull request mapping Joseph chose by hand for the first eight projects (2026-11-10).
    assert state["created"][1] == {
        "workspace_id": W,
        "workspace_slug": "uhstray",
        "project_id": "p-ac",
        "workspace_connection_id": C,
        "entity_type": "GITHUB",
        "config": {
            "states": {
                "mergeRequestEventMapping": {
                    "DRAFT_MR_OPENED": {"id": "s-b", "name": "Backlog"},
                    "MR_OPENED": {"id": "s-t", "name": "Todo"},
                    "MR_REVIEW_REQUESTED": {"id": "s-p", "name": "In Progress"},
                    "MR_READY_FOR_MERGE": {"id": "s-p", "name": "In Progress"},
                    "MR_MERGED": {"id": "s-d", "name": "Done"},
                    "MR_CLOSED": {"id": "s-c", "name": "Cancelled"},
                }
            },
            "skipBackwardStateMovement": False,
        },
        "type": "PROJECT_PR_AUTOMATION",
    }
    assert out["pr_linked"] == ["AC", "HUHHB"] and len(state["created"]) == 4
    # Silo authenticates by forwarding the session cookie, so every silo call must carry it.
    assert state["silo_cookies"] and all("session-id=sess" in c for c in state["silo_cookies"])


def test_an_existing_link_to_the_declared_repo_is_left_alone(stub):
    state, base = stub
    state["existing"] = [
        {"type": "PROJECT_ISSUE_SYNC", "project_id": "p-ac", "entity_id": "11"},
        {"type": "PROJECT_PR_AUTOMATION", "project_id": "p-ac"},
    ]
    rc, out = run(base, apply=True)
    assert rc == 0 and out["present"] == ["AC"] and out["linked"] == ["HUHHB"]
    assert out["pr_present"] == ["AC"] and out["pr_linked"] == ["HUHHB"]
    assert [(c["type"], c["project_id"]) for c in state["created"]] == [
        ("PROJECT_ISSUE_SYNC", "p-hu"),
        ("PROJECT_PR_AUTOMATION", "p-hu"),
    ]


def test_a_missing_pr_mapping_is_added_beside_an_existing_link(stub):
    state, base = stub
    state["existing"] = [{"type": "PROJECT_ISSUE_SYNC", "project_id": "p-ac", "entity_id": "11"}]
    rc, out = run(base, apply=True, projects=[{"identifier": "AC", "repo": "uhstray-io/agent-cloud"}])
    assert rc == 0 and out["present"] == ["AC"] and out["pr_linked"] == ["AC"]
    assert [c["type"] for c in state["created"]] == ["PROJECT_PR_AUTOMATION"]


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
            "Linked: AC; already linked HUHHB; pull request automation added for none, already set none",
        ),
        (
            {
                "_link_plan": _result(
                    '{"would_link": ["AC"], "present": [], "missing_projects": ["X"], "conflicts": []}'
                ),
                "_link_apply": {"skipped": True},
            },
            True,
            "already set none; not yet in Plane: X",
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


@needs_ansible
@pytest.mark.parametrize(
    "plan,skipped",
    [
        ('{"error": "some declared links conflict with what Plane holds", "conflicts": ["AC: x"]}', True),
        ("Traceback", True),
        ('{"would_link": ["AC"], "present": [], "missing_projects": [], "conflicts": []}', False),
    ],
    ids=["plan-refused", "plan-unreadable", "plan-clean"],
)
def test_the_apply_runs_only_after_a_clean_plan(tmp_path, plan, skipped):
    # A refused plan must not go on to apply: the script creates the non-conflicting links first.
    import yaml

    play = next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if p.get("name", "").startswith("Link the declared"))
    block = next(t for t in play["tasks"] if "block" in t)["block"]
    apply = dict(next(t for t in block if t.get("name") == "Link what is missing"))
    apply["ansible.builtin.command"] = {"argv": ["true"]}  # never the real exec; only the gate is under test
    check = {
        "name": "gate",
        "ansible.builtin.assert": {"that": [f"_link_apply is {'skipped' if skipped else 'not skipped'}"]},
    }
    facts = {"_link_plan": {"rc": 0, "stdout_lines": plan.splitlines()}}
    (tmp_path / "play.yml").write_text(
        yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": facts, "tasks": [apply, check]}])
    )
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(tmp_path / "play.yml")],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-1500:]


def test_a_dry_run_refuses_a_missing_state_and_claims_nothing_created(stub):
    state, base = stub
    state["states"] = {"p-hu": [st for st in STATES if st["name"] != "Cancelled"]}
    rc, out = run(base, apply=False)
    assert rc == 1 and "lacks the states Cancelled" in out["error"] and state["created"] == []
    # Planned, not created: a dry run's refusal must not read as if Plane changed.
    assert out["would_link"] == ["AC"] and "linked" not in out and "pr_linked" not in out


def test_a_failed_apply_names_every_record_already_created(stub):
    state, base = stub
    state["refuse"] = {("PROJECT_PR_AUTOMATION", "p-hu")}
    rc, out = run(base, apply=True)
    assert rc == 1 and "PROJECT_PR_AUTOMATION" in out["error"]
    assert out["linked"] == ["AC", "HUHHB"] and out["pr_linked"] == ["AC"]
