"""sync_projects.py against a stub Plane: what it creates, what it links, what it never touches.

The script runs inside plane-api with the automation password on stdin and its last stdout line is
shown in the Semaphore log. These tests run the real script against a stub that serves Plane
v3.3.3's shapes: a cursor-paginated /api/v1 list envelope (results, next_cursor, next_page_results),
a 409 that names the existing id on a repeat external pair, and a 409 on a taken project identifier.
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
SOURCE = (DEPLOY / "plane_session.py").read_text() + "\n" + (DEPLOY / "sync_projects.py").read_text()
PASSWORD = "pw-that-must-never-print"
TOKEN = "service-token-that-must-never-print"
ME = "automation@example.test"
WS = "/api/v1/workspaces/ws"

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

    def _json(self):
        """The JSON body of a request, or None for a form post (sign-in) or no body."""
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw[:1] in (b"{", b"[") else None

    def _record(self, body=None):
        path = urllib.parse.urlsplit(self.path).path
        self.state["calls"].append({"method": self.command, "path": path,
                                    "key": self.headers.get("X-Api-Key"), "body": body})

    def _rows(self, path):
        """The row list a v1 list path serves, or None for a path this stub does not serve."""
        s = self.state
        if path == f"{WS}/projects/":
            return s["projects"]
        m = re.fullmatch(rf"{WS}/projects/([^/]+)/(modules|issues)/", path)
        if m:
            return s[m.group(2)].setdefault(m.group(1), [])
        return None

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/auth/get-csrf-token/":
            self._send(200, {"csrf_token": "tok"}, cookie="csrftoken=tok; Secure; HttpOnly")
            return
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
        return self._send(201, made)

    def do_POST(self):
        body = self._json()
        self._record(body)
        url = urllib.parse.urlsplit(self.path).path
        if url == "/auth/sign-in/":
            return self._send(302, location="/", cookie="session-id=s; Secure; HttpOnly")
        if url == "/api/workspaces/ws/service-api-tokens/":
            return self._send(201, {"token": TOKEN})
        if url == f"{WS}/projects/":
            return self._create("projects", "", body)
        m = re.fullmatch(rf"{WS}/projects/([^/]+)/(modules|issues)/", url)
        if m:
            return self._create(m.group(2), m.group(1), body)
        m = re.fullmatch(rf"{WS}/projects/([^/]+)/modules/([^/]+)/module-issues/", url)
        if m:
            return self._send(self.state["link_status"], {})
        return self._send(404, {})

    def do_PATCH(self):
        self._record(self._json())
        self._send(200, {})

    def do_DELETE(self):
        self._record()
        self._send(204)


@pytest.fixture
def plane():
    StubPlane.state = {"projects": [], "modules": {}, "issues": {}, "ghosts": {}, "taken": set(),
                       "calls": [], "requests": [], "seq": 0, "link_status": 201}
    server = http.server.HTTPServer(("127.0.0.1", 0), StubPlane)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield StubPlane.state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def lock(tmp_path):
    return str(tmp_path / "sync.lock")


def run(base, lock_path, projects, apply):
    cfg = {"base": base, "host": "plane.test", "origin": "https://plane.test", "email": ME,
           "password": PASSWORD, "workspace_slug": "ws", "apply": apply, "projects": projects,
           "pace_seconds": 0, "lock_path": lock_path}
    return subprocess.run([sys.executable, "-I", "-c", SOURCE], input=json.dumps(cfg) + "\n",
                          capture_output=True, text=True, timeout=60)


def summary(out):
    assert out.returncode == 0, out.stdout + out.stderr
    assert PASSWORD not in out.stdout + out.stderr and TOKEN not in out.stdout + out.stderr
    return json.loads(out.stdout.splitlines()[-1])


def refusal(out):
    assert out.returncode == 1, out.stdout + out.stderr
    assert PASSWORD not in out.stdout + out.stderr and TOKEN not in out.stdout + out.stderr
    return json.loads(out.stdout.splitlines()[-1])


def writes(state):
    return [c for c in state["calls"] if c["method"] != "GET" and c["path"].startswith(WS)]


ZERO = {"projects": 0, "modules": 0, "items": 0}


def counts(created=None, would=None, existing=None, repairs=()):
    return {"created": {**ZERO, **(created or {})}, "would_create": {**ZERO, **(would or {})},
            "existing": {**ZERO, **(existing or {})}, "repairs": list(repairs)}


def seed_everything(state, pid="p1"):
    """The whole declared PROJECT already in Plane, one row per page, under pid."""
    state["projects"].append(row(pid, name="Agent Cloud", identifier="ACL", external_id="agent-cloud"))
    state["modules"][pid] = [row("m1", external_id="mod-a"), row("m2", external_id="mod-b")]
    state["issues"][pid] = [row("i1", external_id="item-1"), row("i2", external_id="item-2"),
                            row("i3", external_id="item-3")]


def test_dry_run_writes_nothing_and_counts_what_it_would_create(plane, lock):
    state, base = plane
    out = summary(run(base, lock, [PROJECT], apply=False))
    assert writes(state) == []
    assert out == {"changed": False, "applied": False,
                   **counts(would={"projects": 1, "modules": 2, "items": 3})}


def test_dry_run_counts_what_is_already_there_as_existing(plane, lock):
    state, base = plane
    state["projects"].append(row("p1", name="Agent Cloud", identifier="ACL", external_id="agent-cloud"))
    state["modules"]["p1"] = [row("m1", external_id="mod-a")]
    state["issues"]["p1"] = [row("i1", external_id="item-1")]
    out = summary(run(base, lock, [PROJECT], apply=False))
    assert writes(state) == []
    assert out == {"changed": False, "applied": False,
                   **counts(would={"modules": 1, "items": 2}, existing={"projects": 1, "modules": 1, "items": 1})}


def test_apply_creates_everything_and_links_each_new_item_to_its_module(plane, lock):
    state, base = plane
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out == {"changed": True, "applied": True, **counts(created={"projects": 1, "modules": 2, "items": 3})}
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


def test_a_project_of_ours_with_modules_off_gets_exactly_that_one_setting_back(plane, lock):
    # Plane creates projects with Modules off and then refuses every module create.
    state, base = plane
    seed_everything(state)
    state["projects"][0]["module_view"] = False
    out = summary(run(base, lock, [PROJECT], apply=False))
    assert out["repairs"] == ["would enable modules on project agent-cloud"]
    assert writes(state) == []
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out["changed"] is True and out["repairs"] == ["enabled modules on project agent-cloud"]
    assert [(c["method"], c["path"], c["body"]) for c in writes(state)] == [
        ("PATCH", f"{WS}/projects/p1/", {"module_view": True})]


def test_a_project_that_is_not_ours_is_never_patched(plane, lock):
    state, base = plane
    seed_everything(state)
    state["projects"].append({"id": "px", "external_source": "someone-else", "external_id": "agent-cloud",
                              "module_view": False})
    summary(run(base, lock, [PROJECT], apply=True))
    assert [c for c in state["calls"] if c["method"] == "PATCH"] == []


def test_a_409_that_names_an_id_is_existing_and_is_not_linked(plane, lock):
    state, base = plane
    state["projects"].append(row("p1", name="Agent Cloud", identifier="ACL", external_id="agent-cloud"))
    state["modules"]["p1"] = [row("m1", external_id="mod-a"), row("m2", external_id="mod-b")]
    state["ghosts"][("issues", "p1", "item-1")] = "ghost-1"  # in Plane, missing from the list
    out = summary(run(base, lock, [PROJECT], apply=True))
    assert out == {"changed": True, "applied": True,
                   **counts(created={"items": 2}, existing={"projects": 1, "modules": 2, "items": 1})}
    item = {i["external_id"]: i["id"] for i in state["issues"]["p1"]}
    # Only the two items this run created are linked; the 409'd item-1 is not, and nothing links ghost-1.
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


def test_the_v1_key_goes_on_every_v1_call_and_only_those(plane, lock):
    state, base = plane
    summary(run(base, lock, [PROJECT], apply=True))
    v1 = [c for c in state["calls"] if c["path"].startswith("/api/v1/")]
    assert v1 and all(c["key"] == TOKEN for c in v1)
    assert all(c["key"] is None for c in state["calls"] if not c["path"].startswith("/api/v1/"))


def test_a_refused_link_fails_and_names_what_was_already_created(plane, lock):
    state, base = plane
    state["link_status"] = 400
    err = refusal(run(base, lock, [PROJECT], apply=True))
    assert "linking work item Item 1 to module Module A failed (400)" in err["error"]
    assert err["actions"] == ["created project agent-cloud", "created module Module A", "created work item Item 1"]
    assert len([c for c in writes(state) if c["path"].endswith("/issues/")]) == 1  # stopped at the first link


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
