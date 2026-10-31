"""sync_projects.py against a stub Plane: what it creates, what it links, what it never touches.

The script runs inside plane-api with the workspace service token on stdin and its last stdout line is
shown in the Semaphore log. These tests run the real script against a stub that serves Plane's shapes:
a cursor-paginated /api/v1 list envelope (results, next_cursor, next_page_results), a module-issues list
whose rows are work items, a 409 that names the existing id on a repeat external pair, and a 409 on a
taken project identifier.
"""

import fcntl
import http.server
import json
import pathlib
import re
import subprocess
import sys
import threading
import urllib.parse

import pytest
from fake_http import DrainingHandler

DEPLOY = pathlib.Path(__file__).resolve().parents[2] / "platform/services/plane/deployment"
# The playbook runs plane_session.py and the script joined, exactly as here.
SESSION = (DEPLOY / "plane_session.py").read_text()
SOURCE = SESSION + "\n" + (DEPLOY / "sync_projects.py").read_text()
TOKEN = "service-token-that-must-never-print"
WS = "/api/v1/workspaces/ws"
NO_TOKEN = "no service token given; run Deploy Plane, which mints it into OpenBao"
# The states a new Plane project starts with (live DB, 2026-10-30), minus Triage: the list hides it.
PLANE_DEFAULT_STATES = [
    {"name": "Backlog", "group": "backlog", "color": "#60646C", "default": True},
    {"name": "Todo", "group": "unstarted", "color": "#60646C", "default": False},
    {"name": "In Progress", "group": "started", "color": "#F59E0B", "default": False},
    {"name": "Done", "group": "completed", "color": "#46A758", "default": False},
    {"name": "Cancelled", "group": "cancelled", "color": "#9AA4BC", "default": False},
]
SCHEMA = {
    "states": [{"name": "Todo", "group": "unstarted", "color": "#60646C"},
               {"name": "In Progress", "group": "started", "color": "#F59E0B"},
               {"name": "Review", "group": "started", "color": "#3B82F6"}],
    "labels": [{"name": "openspec", "color": "#0EA5E9", "description": "Declared from openspec"},
               {"name": "blocked", "color": "#EF4444", "description": ""}],
    "item_labels": ["openspec"],
}


def plane_default_states(pid):
    return [{**d, "id": f"{pid}-{d['name'].lower().replace(' ', '-')}"} for d in PLANE_DEFAULT_STATES]

PROJECT = {
    "repo": "agent-cloud", "identifier": "ACL", "name": "Agent Cloud",
    "modules": [
        {"external_id": "mod-a", "name": "Module A", "description": "first",
         "items": [{"external_id": "item-1", "name": "Item 1", "description_html": "<p>one</p>"},
                   {"external_id": "item-2", "name": "Item 2", "description_html": "<p>two</p>"}]},
        {"external_id": "mod-b", "name": "Module B", "description": "second",
         "items": [{"external_id": "item-3", "name": "Item 3", "description_html": "<p>three</p>"}]},
    ],
}


def row(rid, **fields):
    """A row as the v1 list serves it: id plus the external pair the serializers return."""
    return {"id": rid, "external_source": "openspec", **fields}


class StubPlane(DrainingHandler):
    state = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body=None, location=None, cookie=None):
        self.state.setdefault("requests", []).append((self.command, self.path))
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        data = json.dumps(body).encode() if body is not None else b""
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _raw(self, status, data):
        """A reply whose body is not JSON: the script's parse of it fails."""
        self.state.setdefault("requests", []).append((self.command, self.path))
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        """The JSON body of a request, or None for no body."""
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw[:1] in (b"{", b"[") else None

    def _record(self, body=None):
        path = urllib.parse.urlsplit(self.path).path
        self.state["calls"].append({"method": self.command, "path": path,
                                    "key": self.headers.get("X-Api-Key"), "body": body,
                                    "host": self.headers.get("Host"), "origin": self.headers.get("Origin")})

    def _rows(self, path):
        """The row list a v1 list path serves, or None for a path this stub does not serve."""
        s = self.state
        if path == f"{WS}/projects/":
            return s["projects"]
        m = re.fullmatch(rf"{WS}/projects/([^/]+)/modules/([^/]+)/module-issues/", path)
        if m:
            return s["links"].setdefault((m.group(1), m.group(2)), [])
        m = re.fullmatch(rf"{WS}/projects/([^/]+)/(modules|issues|states|labels)/", path)
        if m:
            return s[m.group(2)].setdefault(m.group(1), [])
        return None

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if self.state.get("garbage") and self.state["garbage"] in url.path:
            return self._raw(200, b'{"results": [')  # starts like JSON, is not: the script's parse fails
        rows = self._rows(url.path)
        if rows is None:
            self._send(404, {})
            return
        self._record()
        # One row per page, so every list is only complete when the cursor is followed.
        query = urllib.parse.parse_qs(url.query)
        n = int(query.get("cursor", ["100:0:0"])[0].split(":")[1])
        self._send(200, {"results": rows[n:n + 1], "next_cursor": f"100:{n + 1}:0",
                         "next_page_results": n + 1 < len(rows)})

    def _create(self, kind, scope, body):
        """Plane's create: 409 with the existing id for a repeat external pair, else 201 with a new id."""
        s = self.state
        path = f"{WS}/projects/" if kind == "projects" else f"{WS}/projects/{scope}/{kind}/"
        store = self._rows(path)
        ext = body.get("external_id")
        ghost = s["ghosts"].get((kind, scope, ext))
        if ghost:  # exists in Plane but not in the list: the race the 409 exists to catch
            return self._send(409, {"error": "already exists", "id": ghost})
        for existing in store:
            if existing.get("external_id") == ext and existing.get("external_source") == body.get("external_source"):
                return self._send(409, {"error": "already exists", "id": existing["id"]})
        if kind == "projects" and body.get("identifier") in s["taken"]:
            return self._send(409, {"identifier": "The project identifier is already taken"})
        s["seq"] += 1
        made = row(f"{kind[:3]}-{s['seq']}", **body)
        store.append(made)
        if kind == "projects":
            s["states"][made["id"]] = plane_default_states(made["id"])
        return self._send(201, made)

    def _create_named(self, kind, pid, body):
        """Plane's state and label create, by name: a clash answers 409 with the existing id (exact for a
        state, case-insensitive for a label). The views answer a state 200 and a label 201."""
        store = self.state[kind].setdefault(pid, [])
        if kind == "states":
            clash = [r for r in store if r["name"] == body["name"]]
        else:
            clash = [r for r in store if r["name"].lower() == body["name"].lower()]
        if clash:
            return self._send(409, {"error": "already exists", "id": clash[0]["id"]})
        self.state["seq"] += 1
        made = {"id": f"{kind[:3]}-{self.state['seq']}", **body}
        store.append(made)
        return self._send(200 if kind == "states" else 201, made)

    def _link(self, url, body):
        """Plane's add: a 2xx links each id once (re-adding a pair is a no-op); a refusal links nothing."""
        s = self.state
        if s["links_ok"] is not None:
            status = 201 if s["links_ok"] > 0 else 400
            s["links_ok"] = max(0, s["links_ok"] - 1)
        else:
            status = s["link_status"]
        if status in (200, 201):
            store = self._rows(url)
            for iid in body["issues"]:
                if not any(r["id"] == iid for r in store):
                    store.append({"id": iid})
        return self._send(status, {})

    def do_POST(self):
        body = self._json()
        self._record(body)
        url = urllib.parse.urlsplit(self.path).path
        if url == f"{WS}/projects/":
            return self._create("projects", "", body)
        m = re.fullmatch(rf"{WS}/projects/([^/]+)/(states|labels)/", url)
        if m:
            return self._create_named(m.group(2), m.group(1), body)
        m = re.fullmatch(rf"{WS}/projects/([^/]+)/(modules|issues)/", url)
        if m:
            return self._create(m.group(2), m.group(1), body)
        if re.fullmatch(rf"{WS}/projects/([^/]+)/modules/([^/]+)/module-issues/", url):
            return self._link(url, body)
        return self._send(404, {})

    def do_PATCH(self):
        body = self._json()
        self._record(body)
        url = urllib.parse.urlsplit(self.path).path
        m = re.fullmatch(rf"{WS}/projects/([^/]+)/(states|labels)/([^/]+)/", url)
        if m:  # a state or label PATCH changes the stored row, so a later read shows it
            for stored in self.state[m.group(2)].get(m.group(1), []):
                if stored["id"] == m.group(3):
                    stored.update(body)
            return self._send(200, {})
        self._send(200, {})

    def do_DELETE(self):
        self._record()
        self._send(204)


@pytest.fixture
def plane():
    StubPlane.state = {"projects": [], "modules": {}, "issues": {}, "links": {}, "ghosts": {}, "taken": set(),
                       "states": {}, "labels": {},
                       "calls": [], "requests": [], "seq": 0, "link_status": 201, "links_ok": None,
                       "garbage": None}
    server = http.server.HTTPServer(("127.0.0.1", 0), StubPlane)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield StubPlane.state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def lock(tmp_path):
    return str(tmp_path / "sync.lock")


def run_with(source, cfg):
    return subprocess.run([sys.executable, "-I", "-c", source], input=json.dumps(cfg) + "\n",
                          capture_output=True, text=True, timeout=60)


def run(base, lock_path, projects, apply, token=TOKEN, schema=None):
    cfg = {"base": base, "host": "plane.test", "origin": "https://plane.test", "service_token": token,
           "workspace_slug": "ws", "apply": apply, "projects": projects,
           "pace_seconds": 0, "lock_path": lock_path}
    if schema is not None:  # absent from cfg, not empty: the no-schema run must make no state or label request
        cfg["schema"] = schema
    return run_with(SOURCE, cfg)


def summary(out):
    assert out.returncode == 0, out.stdout + out.stderr
    assert TOKEN not in out.stdout + out.stderr
    return json.loads(out.stdout.splitlines()[-1])


def refusal(out):
    assert out.returncode == 1, out.stdout + out.stderr
    assert TOKEN not in out.stdout + out.stderr
    return json.loads(out.stdout.splitlines()[-1])


def writes(state):
    return [c for c in state["calls"] if c["method"] != "GET" and c["path"].startswith(WS)]


ZERO = {"projects": 0, "modules": 0, "items": 0, "links": 0, "states": 0, "labels": 0}
SCHEMA_ZERO = {"states": 0, "labels": 0}


def counts(created=None, would=None, existing=None, repairs=(), updated=None, would_update=None):
    return {"created": {**ZERO, **(created or {})}, "would_create": {**ZERO, **(would or {})},
            "existing": {**ZERO, **(existing or {})},
            "updated": {**SCHEMA_ZERO, **(updated or {})}, "would_update": {**SCHEMA_ZERO, **(would_update or {})},
            "repairs": list(repairs)}


def seed_everything(state, pid="p1", linked=True):
    """The whole declared PROJECT already in Plane, one row per page, under pid; linked puts each item in its module."""
    state["projects"].append(row(pid, name="Agent Cloud", identifier="ACL", external_id="agent-cloud"))
    state["modules"][pid] = [row("m1", external_id="mod-a"), row("m2", external_id="mod-b")]
    state["issues"][pid] = [row("i1", external_id="item-1"), row("i2", external_id="item-2"),
                            row("i3", external_id="item-3")]
    if linked:
        state["links"][(pid, "m1")] = [{"id": "i1"}, {"id": "i2"}]
        state["links"][(pid, "m2")] = [{"id": "i3"}]


def test_dry_run_writes_nothing_and_counts_what_it_would_create(plane, lock):
    state, base = plane
    out = summary(run(base, lock, [PROJECT], apply=False))
    assert writes(state) == []
    assert out == {"changed": False, "applied": False,
                   **counts(would={"projects": 1, "modules": 2, "items": 3, "links": 3})}


def test_dry_run_counts_what_is_already_there_as_existing(plane, lock):
    state, base = plane
    state["projects"].append(row("p1", name="Agent Cloud", identifier="ACL", external_id="agent-cloud"))
    state["modules"]["p1"] = [row("m1", external_id="mod-a")]
    state["issues"]["p1"] = [row("i1", external_id="item-1")]
    out = summary(run(base, lock, [PROJECT], apply=False))
    assert writes(state) == []
    assert out == {"changed": False, "applied": False,
                   **counts(would={"modules": 1, "items": 2, "links": 3},
                            existing={"projects": 1, "modules": 1, "items": 1})}


def test_apply_creates_everything_and_links_each_new_item_to_its_module(plane, lock):
    state, base = plane
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out == {"changed": True, "applied": True,
                   **counts(created={"projects": 1, "modules": 2, "items": 3, "links": 3})}
    pid = state["projects"][0]["id"]
    mod = {m["external_id"]: m["id"] for m in state["modules"][pid]}
    item = {i["external_id"]: i["id"] for i in state["issues"][pid]}
    assert [c["body"] for c in writes(state) if c["path"] == f"{WS}/projects/"] == [
        {"name": "Agent Cloud", "identifier": "ACL", "external_id": "agent-cloud", "external_source": "openspec",
         "module_view": True}]
    links = [(c["path"], c["body"]) for c in writes(state) if c["path"].endswith("/module-issues/")]
    assert links == [
        (f"{WS}/projects/{pid}/modules/{mod['mod-a']}/module-issues/", {"issues": [item["item-1"]]}),
        (f"{WS}/projects/{pid}/modules/{mod['mod-a']}/module-issues/", {"issues": [item["item-2"]]}),
        (f"{WS}/projects/{pid}/modules/{mod['mod-b']}/module-issues/", {"issues": [item["item-3"]]}),
    ]


def test_a_second_apply_creates_nothing(plane, lock):
    state, base = plane
    summary(run(base, lock, [PROJECT], apply=True))
    state["calls"].clear()
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert writes(state) == []
    assert out == {"changed": False, "applied": True,
                   **counts(existing={"projects": 1, "modules": 2, "items": 3})}


def test_an_existing_item_missing_from_its_module_is_linked_on_apply_and_only_counted_on_dry_run(plane, lock):
    state, base = plane
    seed_everything(state, linked=False)
    out = summary(run(base, lock, [PROJECT], apply=False))
    assert writes(state) == []
    assert out == {"changed": False, "applied": False,
                   **counts(existing={"projects": 1, "modules": 2, "items": 3}, would={"links": 3})}
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out == {"changed": True, "applied": True,
                   **counts(existing={"projects": 1, "modules": 2, "items": 3}, created={"links": 3})}
    assert [(c["path"], c["body"]) for c in writes(state)] == [
        (f"{WS}/projects/p1/modules/m1/module-issues/", {"issues": ["i1"]}),
        (f"{WS}/projects/p1/modules/m1/module-issues/", {"issues": ["i2"]}),
        (f"{WS}/projects/p1/modules/m2/module-issues/", {"issues": ["i3"]}),
    ]
    state["calls"].clear()
    assert summary(run(base, lock, [PROJECT], apply=True))["changed"] is False
    assert writes(state) == []


def test_links_are_only_added_a_link_to_another_module_is_kept_and_nothing_is_removed(plane, lock):
    state, base = plane
    seed_everything(state)
    # item-1 also sits in module B: a link the declaration does not name
    state["links"][("p1", "m2")].append({"id": "i1"})
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out["changed"] is False
    assert writes(state) == []
    assert [r["id"] for r in state["links"][("p1", "m2")]] == ["i3", "i1"]


def test_rows_the_declaration_no_longer_names_are_left_alone_and_nothing_is_patched_or_deleted(plane, lock):
    state, base = plane
    seed_everything(state)
    state["modules"]["p1"].append(row("mold", external_id="mod-old"))
    state["issues"]["p1"].append(row("iold", external_id="item-old"))
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out["changed"] is False
    assert [c for c in state["calls"] if c["method"] in ("PATCH", "DELETE")] == []
    assert [m["external_id"] for m in state["modules"]["p1"]] == ["mod-a", "mod-b", "mod-old"]
    assert [i["external_id"] for i in state["issues"]["p1"]] == ["item-1", "item-2", "item-3", "item-old"]


def test_a_project_with_modules_off_gets_exactly_that_one_setting_back_when_a_module_is_missing(plane, lock):
    # Plane creates projects with Modules off and then refuses every module create. A module this run
    # must create is the only reason to switch the setting on.
    state, base = plane
    seed_everything(state)
    state["modules"]["p1"] = [row("m1", external_id="mod-a")]  # mod-b must be created
    state["projects"][0]["module_view"] = False
    out = summary(run(base, lock, [PROJECT], apply=False))
    assert out["repairs"] == ["would enable modules on project agent-cloud"]
    assert writes(state) == []
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out == {"changed": True, "applied": True,
                   **counts(created={"modules": 1, "links": 1}, existing={"projects": 1, "modules": 1, "items": 3},
                            repairs=["enabled modules on project agent-cloud"])}
    new_mid = [m["id"] for m in state["modules"]["p1"] if m["external_id"] == "mod-b"][0]
    assert [(c["method"], c["path"], c["body"]) for c in writes(state)] == [
        ("PATCH", f"{WS}/projects/p1/", {"module_view": True}),
        ("POST", f"{WS}/projects/p1/modules/", {"name": "Module B", "description": "second",
                                                "external_id": "mod-b", "external_source": "openspec"}),
        ("POST", f"{WS}/projects/p1/modules/{new_mid}/module-issues/", {"issues": ["i3"]}),
    ]


def test_a_project_with_modules_off_and_every_module_present_is_not_patched(plane, lock):
    state, base = plane
    seed_everything(state)
    state["projects"][0]["module_view"] = False
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out == {"changed": False, "applied": True,
                   **counts(existing={"projects": 1, "modules": 2, "items": 3})}
    assert writes(state) == []


def test_a_project_that_is_not_ours_is_never_patched(plane, lock):
    state, base = plane
    seed_everything(state)
    state["modules"]["p1"] = [row("m1", external_id="mod-a")]  # mod-b must be created in OUR project
    state["projects"].append({"id": "px", "external_source": "someone-else", "external_id": "agent-cloud",
                              "module_view": False})
    summary(run(base, lock, [PROJECT], apply=True))
    assert [c for c in state["calls"] if c["method"] == "PATCH"] == []
    assert [c["path"] for c in writes(state) if c["path"].endswith("/modules/")] == [f"{WS}/projects/p1/modules/"]


def test_a_409_that_names_an_id_is_existing_and_not_linked_because_the_listing_does_not_show_it(plane, lock):
    state, base = plane
    state["projects"].append(row("p1", name="Agent Cloud", identifier="ACL", external_id="agent-cloud"))
    state["modules"]["p1"] = [row("m1", external_id="mod-a"), row("m2", external_id="mod-b")]
    state["ghosts"][("issues", "p1", "item-1")] = "ghost-1"  # in Plane, missing from the list
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out == {"changed": True, "applied": True,
                   **counts(created={"items": 2, "links": 2}, existing={"projects": 1, "modules": 2, "items": 1})}
    item = {i["external_id"]: i["id"] for i in state["issues"]["p1"]}
    # Only the two items this run created are linked. The 409'd item-1 (ghost-1) is never linked: Plane
    # refuses to link an archived or triage item, which is what a row missing from the list usually is.
    links = [(c["path"], c["body"]) for c in writes(state) if c["path"].endswith("/module-issues/")]
    assert links == [
        (f"{WS}/projects/p1/modules/m1/module-issues/", {"issues": [item["item-2"]]}),
        (f"{WS}/projects/p1/modules/m2/module-issues/", {"issues": [item["item-3"]]}),
    ]


def test_identifier_taken_fails_by_name_before_anything_is_created(plane, lock):
    state, base = plane
    state["taken"].add("ACL")
    err = refusal(run(base, lock, [PROJECT], apply=True))
    assert err["error"] == "project identifier ACL for agent-cloud is taken by another project"
    assert err["actions"] == []
    assert err["actions_total"] == 0
    assert [c["path"] for c in writes(state)] == [f"{WS}/projects/"]


def test_every_page_of_every_list_is_followed(plane, lock):
    state, base = plane
    state["projects"].append(row("p0", name="Other", identifier="OTH", external_id="other-repo"))
    seed_everything(state, pid="p1")
    out = summary(run(base, lock, [PROJECT], apply=True))
    # agent-cloud is on the second page of the project list: missing it would create a duplicate.
    assert writes(state) == []
    assert out == {"changed": False, "applied": True,
                   **counts(existing={"projects": 1, "modules": 2, "items": 3})}
    assert any("cursor=" in p for _, p in state["requests"])


def test_the_v1_key_goes_on_every_call_and_the_sync_never_signs_in_or_mints_a_token(plane, lock):
    state, base = plane
    summary(run(base, lock, [PROJECT], apply=True))
    assert state["calls"] and all(c["key"] == TOKEN for c in state["calls"])
    assert all(c["path"].startswith("/api/v1/") for c in state["calls"])
    assert [p for _, p in state["requests"] if "/auth/" in p or "service-api-tokens" in p] == []


def test_a_refused_link_fails_and_names_what_was_already_created(plane, lock):
    state, base = plane
    state["link_status"] = 400
    err = refusal(run(base, lock, [PROJECT], apply=True))
    assert "linking work item Item 1 to module Module A failed (400)" in err["error"]
    assert err["actions"] == ["created project agent-cloud", "created module Module A", "created work item Item 1"]
    assert err["actions_total"] == 3
    assert len([c for c in writes(state) if c["path"].endswith("/issues/")]) == 1  # stopped at the first link


@pytest.mark.parametrize("token", ["", None])
def test_no_service_token_is_refused_before_any_request(plane, lock, token):
    state, base = plane
    err = refusal(run(base, lock, [PROJECT], apply=True, token=token))
    assert err == {"error": NO_TOKEN}
    assert state["requests"] == []


def test_a_crash_after_the_lock_is_a_named_stop_that_lists_what_was_done(plane, lock):
    state, base = plane
    state["garbage"] = "module-issues"  # the new module's link list answers with a body that is not JSON
    out = run(base, lock, [PROJECT], apply=True)
    assert refusal(out) == {"error": "stopped by JSONDecodeError",
                            "actions": ["created project agent-cloud", "created module Module A"],
                            "actions_total": 2}
    assert "Traceback" not in out.stderr


def test_the_actions_named_in_a_refusal_are_capped_at_twenty_and_the_true_total_is_given(plane, lock):
    state, base = plane
    big = [{**PROJECT, "modules": [{"external_id": "mod-a", "name": "Module A", "description": "first",
                                    "items": [{"external_id": f"item-{i}", "name": f"Item {i}",
                                               "description_html": "<p>x</p>"} for i in range(30)]}]}]
    state["links_ok"] = 25  # the 26th link is refused
    err = refusal(run(base, lock, big, apply=True))
    # project, module, then created+linked for items 0-24, then item 25 created and its link refused
    assert "linking work item Item 25 to module Module A failed (400)" in err["error"]
    assert err["actions_total"] == 2 + 2 * 25 + 1
    assert len(err["actions"]) == 20
    assert err["actions"][0] == "linked work item Item 15"
    assert err["actions"][-1] == "created work item Item 25"


def test_a_caller_cannot_replace_host_origin_or_referer(plane, lock):
    # plane_session.call takes caller headers, but the session's own Host, Origin and Referer win.
    state, base = plane
    probe = SESSION + ("\ncall('GET', '/api/v1/workspaces/ws/projects/', headers={'Host': 'evil.test', "
                       "'Origin': 'https://evil.test', 'Referer': 'https://evil.test/'})\n")
    out = run_with(probe, {"base": base, "host": "plane.test", "origin": "https://plane.test"})
    assert out.returncode == 0, out.stdout + out.stderr
    sent = state["calls"][-1]
    assert (sent["host"], sent["origin"]) == ("plane.test", "https://plane.test")


def test_the_overlap_lock_refuses_a_second_run_and_makes_no_request(plane, tmp_path):
    state, base = plane
    lock_path = tmp_path / "sync.lock"
    with open(lock_path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another run holds the lock while this one starts
        err = refusal(run(base, str(lock_path), [PROJECT], apply=True))
    assert "overlap" in err["error"]
    assert state["requests"] == []


def test_the_lock_is_released_when_a_run_ends(plane, lock):
    state, base = plane
    summary(run(base, lock, [PROJECT], apply=False))
    summary(run(base, lock, [PROJECT], apply=False))


def test_the_joined_source_defines_every_name_it_uses():
    # The script is a fragment that only runs after plane_session.py, so ruff's undefined-name
    # check is off for the file (noqa F821); this checks the joined source the playbook runs.
    import builtins
    import symtable

    script = "sync_projects.py"
    source = (DEPLOY / "plane_session.py").read_text() + "\n" + (DEPLOY / script).read_text()
    top = symtable.symtable(source, script, "exec")
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}
    defined |= {c.get_name() for c in top.get_children()} | set(dir(builtins))

    def used(table):
        for s in table.get_symbols():
            if s.is_referenced() and (s.is_global() or table is top):
                yield s.get_name()
        for child in table.get_children():
            yield from used(child)

    assert sorted(set(used(top)) - defined) == []


def set_row(rows, name, **fields):
    """Edit one stored row the way Plane already holds it, so a test sets up exactly one difference."""
    (found,) = [r for r in rows if r["name"] == name]
    found.update(fields)


def schema_writes(state):
    """Every write that touches a state or a label, in order."""
    return [(c["method"], c["path"], c["body"]) for c in writes(state)
            if "/states/" in c["path"] or "/labels/" in c["path"]]


def test_no_schema_in_cfg_makes_no_state_or_label_request_and_item_bodies_carry_no_labels(plane, lock):
    state, base = plane
    summary(run(base, lock, [PROJECT], apply=True))
    assert [p for _, p in state["requests"] if "/states/" in p or "/labels/" in p] == []
    bodies = [c["body"] for c in writes(state) if c["path"].endswith("/issues/")]
    assert len(bodies) == 3
    assert all("labels" not in b for b in bodies)


def test_a_dry_run_of_a_project_not_yet_made_counts_its_schema_without_listing_it(plane, lock):
    state, base = plane
    out = summary(run(base, lock, [PROJECT], apply=False, schema=SCHEMA))
    assert writes(state) == []
    assert [p for _, p in state["requests"] if "/states/" in p or "/labels/" in p] == []
    assert out == {"changed": False, "applied": False,
                   # Todo and In Progress come with the project (Plane seeds them); only Review is a create.
                   **counts(would={"projects": 1, "modules": 2, "items": 3, "links": 3, "states": 1, "labels": 2})}


def test_a_dry_run_counts_the_schema_it_would_converge_and_writes_nothing(plane, lock):
    state, base = plane
    seed_everything(state)
    state["states"]["p1"] = plane_default_states("p1")
    set_row(state["states"]["p1"], "In Progress", color="#000000")
    out = summary(run(base, lock, [PROJECT], apply=False, schema=SCHEMA))
    assert writes(state) == []
    assert out == {"changed": False, "applied": False,
                   **counts(would={"states": 1, "labels": 2}, would_update={"states": 1},
                            existing={"projects": 1, "modules": 2, "items": 3, "states": 2})}


def test_apply_creates_the_missing_rows_and_patches_only_the_fields_that_differ_and_never_touches_the_rest(plane, lock):
    state, base = plane
    seed_everything(state)
    states = state["states"]["p1"] = plane_default_states("p1")
    set_row(states, "Todo", color="#60646c")  # the declared color in another case: not a difference
    set_row(states, "In Progress", color="#000000")  # the one difference: group is already right
    state["labels"]["p1"] = [{"id": "lab-os", "name": "OpenSpec", "color": "#0ea5e9", "description": "old text"},
                             {"id": "lab-legacy", "name": "legacy", "color": "#999999", "description": ""}]
    undeclared_before = [dict(r) for r in states if r["name"] in ("Backlog", "Done", "Cancelled")]
    out = summary(run(base, lock, [PROJECT], apply=True, schema=SCHEMA))
    assert out == {"changed": True, "applied": True,
                   **counts(created={"states": 1, "labels": 1}, updated={"states": 1, "labels": 1},
                            existing={"projects": 1, "modules": 2, "items": 3, "states": 2, "labels": 1})}
    assert schema_writes(state) == [
        ("PATCH", f"{WS}/projects/p1/states/p1-in-progress/", {"color": "#F59E0B"}),
        ("POST", f"{WS}/projects/p1/states/", {"name": "Review", "group": "started", "color": "#3B82F6"}),
        ("PATCH", f"{WS}/projects/p1/labels/lab-os/", {"description": "Declared from openspec"}),
        ("POST", f"{WS}/projects/p1/labels/", {"name": "blocked", "color": "#EF4444", "description": ""}),
    ]
    assert [c for c in writes(state) if c["method"] == "DELETE"] == []
    assert [dict(r) for r in states if r["name"] in ("Backlog", "Done", "Cancelled")] == undeclared_before
    assert [r["name"] for r in state["labels"]["p1"]] == ["OpenSpec", "legacy", "blocked"]


def test_a_second_apply_writes_no_state_or_label(plane, lock):
    state, base = plane
    summary(run(base, lock, [PROJECT], apply=True, schema=SCHEMA))
    state["calls"].clear()
    out = summary(run(base, lock, [PROJECT], apply=True, schema=SCHEMA))
    assert writes(state) == []
    assert out == {"changed": False, "applied": True,
                   **counts(existing={"projects": 1, "modules": 2, "items": 3, "states": 3, "labels": 2})}


def test_a_new_work_item_carries_the_item_labels_and_an_existing_item_is_never_patched(plane, lock):
    state, base = plane
    seed_everything(state)
    state["issues"]["p1"] = [i for i in state["issues"]["p1"] if i["external_id"] != "item-3"]
    state["links"][("p1", "m2")] = []  # the link the removed item had: the new item needs its own
    state["states"]["p1"] = plane_default_states("p1")
    out = summary(run(base, lock, [PROJECT], apply=True, schema=SCHEMA))
    openspec_id = [r["id"] for r in state["labels"]["p1"] if r["name"] == "openspec"][0]
    issue_posts = [c["body"] for c in writes(state) if c["method"] == "POST" and c["path"].endswith("/issues/")]
    assert issue_posts == [{"name": "Item 3", "description_html": "<p>three</p>", "external_id": "item-3",
                            "external_source": "openspec", "labels": [openspec_id]}]
    assert [c for c in writes(state) if c["method"] == "PATCH" and "/issues/" in c["path"]] == []
    assert out == {"changed": True, "applied": True,
                   **counts(created={"items": 1, "links": 1, "states": 1, "labels": 2},
                            existing={"projects": 1, "modules": 2, "items": 2, "states": 2})}


@pytest.mark.parametrize("schema", [
    {"item_labels": ["openspec"]},
    {"labels": [{"name": "blocked", "color": "#EF4444", "description": ""}], "item_labels": ["openspec"]},
])
def test_an_item_label_that_is_not_declared_is_refused_before_any_request(plane, lock, schema):
    state, base = plane
    err = refusal(run(base, lock, [PROJECT], apply=True, schema=schema))
    assert err == {"error": "item label openspec is not declared in schema labels", "actions": [], "actions_total": 0}
    assert state["requests"] == []


@pytest.mark.parametrize("declared", [
    {"name": "Triage", "group": "backlog", "color": "#000000"},
    {"name": "Intake", "group": "triage", "color": "#000000"},
])
def test_a_declared_triage_state_is_refused_before_any_request(plane, lock, declared):
    state, base = plane
    err = refusal(run(base, lock, [PROJECT], apply=True, schema={"states": [declared]}))
    assert err == {"error": f"state {declared['name']} cannot be declared: the Triage name and the triage group are "
                            "reserved", "actions": [], "actions_total": 0}
    assert state["requests"] == []


@pytest.mark.parametrize("bad, words", [
    ({"states": [{"name": "Todo", "group": "unstarted"}]}, "missing color"),
    ({"labels": [{"name": "x", "color": "#000000"}]}, "missing description"),
    ({"states": [{"name": " Todo", "group": "unstarted", "color": "#000000"}]}, "leading or trailing spaces"),
    ({"labels": [{"name": "a", "color": "#000000", "description": ""},
                 {"name": "A", "color": "#000000", "description": ""}]}, "declared twice"),
    ({"states": ["Todo"]}, "needs a name"),
])
def test_a_malformed_schema_is_refused_by_row_before_any_request(plane, lock, bad, words):
    state, base = plane
    out = refusal(run(base, lock, [PROJECT], apply=True, schema=bad))
    assert words in out["error"]
    assert state["requests"] == []


def test_a_null_description_matches_an_empty_declared_one(plane, lock):
    state, base = plane
    seed_everything(state)
    state["labels"]["p1"] = [{"id": "l1", "name": "blocked", "color": "#ef4444", "description": None}]
    schema = {"labels": [{"name": "blocked", "color": "#EF4444", "description": ""}]}
    summary(run(base, lock, [PROJECT], apply=True, schema=schema))
    assert [c for c in state["calls"] if c["method"] == "PATCH"] == []
