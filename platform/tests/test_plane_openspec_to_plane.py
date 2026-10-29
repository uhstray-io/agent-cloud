"""openspec_to_plane.py: OpenSpec tasks.md files to desired Plane state, from a checkout and from GitHub.

Every exact-output expectation below is written out by hand from the fixture text. Both input
modes are driven from one fixture dict: the checkout copy and the stub GitHub tree serve the same
bytes, and the CLI output must equal parse_repo's output for them.
"""

import http.server
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import threading
import urllib.parse

import pytest
from fake_http import DrainingHandler

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
LIB_PATH = REPO_ROOT / "platform/services/plane/lib/openspec_to_plane.py"
_spec = importlib.util.spec_from_file_location("openspec_to_plane", LIB_PATH)
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)

REPO = "uhstray-io/fixture"
BRANCH = "main"
TOKEN = "credential-that-must-never-print"
ALPHA = "plan/development/openspec/changes/alpha/tasks.md"
ARCHIVE_NOTES = "plan/development/openspec/changes/archive-notes/tasks.md"
DONE_ONLY = "plan/development/openspec/changes/done-only/tasks.md"
GAMMA = "plan/development/openspec/changes/gamma/tasks.md"
BETA = "plans/development/openspec/changes/beta/tasks.md"

ALPHA_TEXT = "\n".join([
    "## 1. Start",
    "- [ ] 1.1 Open task",
    "- [x] 1.2 Done task",
    "- [ ] ~~1.3~~ **Superseded** by 1.1",
    "- [x] ~~1.4~~ Struck and checked",
    "- [ ] Implement a thing without an id",
    "- [x] Checked without an id",
    "- [ ] 1.5 first open",
    "- [ ] 1.5 second open is a duplicate",
    "- [x] 1.6 done first",
    "- [ ] 1.6 open later is a duplicate",
    "- plain bullet, not a task",
    "Prose line, not a task.",
    "- [ ] 1.7: colon after the id is not an id",
]) + "\n"

BETA_TEXT = "\n".join([
    "# Beta",
    "- [ ] 2.1 Parent task with continuation",
    "  second line of the parent",
    "      deeper third line",
    "- [ ] 2.2 Next task",
    "    - sub bullet belongs to 2.2",
    "    - [x] 2.3 nested done task",
    "    - [ ] 2.4 nested open task",
    "        deeper continuation of 2.4",
    "",
    "  not part of 2.4 after blank",
    "- [ ] 2.5 Task before heading",
    "  stays with 2.5",
    "## Heading",
    "    indented after heading, not part of 2.5",
    "- [ ] 2.6 Last task",
    "Flush prose ends 2.6.",
    "  indented after flush, not part of 2.6",
]) + "\n"

GAMMA_TEXT = "\n".join([
    "- [ ] 3.1 Use **bold**, *italic*, _under_ and ~~gone~~ words",
    "- [ ] 3.2 See [the docs](https://example.test/docs), `weft_mcp_pat`, `changes/*/tasks.md`, `**kept**`",
    '- [ ] 3.3 Compare a < b & c > "d" it\'s',
    "- [ ] 3.4 " + "x" * 300,
    "- [ ] 3.5 " + "y" * 251,
    "- [ ] 3.6 " + "z" * 252,
]) + "\n"

DELTA = "plan/development/openspec/changes/delta/tasks.md"
DELTA_TEXT = "\n".join([
    "- [ ] 1.2f.1 first boundary id",
    "- [ ] 1.2f.2 second boundary id",
    "- [ ] 5H.1 letter before the dot",
    "- [ ] 6S.1 another letter before the dot",
    "- [ ] 5P.1 P before the dot",
    "- [ ] 4.6c2 digit after a letter suffix",
    "- [ ] 4.6d1a digit and letter suffix",
    "- [ ] 8.0a letter suffix",
    "- [ ] ~~8.0b~~ struck id is superseded",
    "- [ ] 8.0b open after a struck id is imported",
]) + "\n"

FIXTURE = {
    ALPHA: ALPHA_TEXT,
    BETA: BETA_TEXT,
    GAMMA: GAMMA_TEXT,
    DONE_ONLY: "- [x] 4.1 finished\n",
    ARCHIVE_NOTES: "- [ ] 5.1 notes open\n",
    # Not selected: archive dirs, an archive-named part, a specs/ tasks.md, a non-OpenSpec path.
    "plan/development/openspec/changes/archive/old/tasks.md": "- [ ] 9.9 archived\n",
    "plan/archive/development/openspec/changes/ancient/tasks.md": "- [ ] 9.8 archived\n",
    "plan/development/openspec/changes/alpha/specs/tasks.md": "- [ ] 9.7 not a change task list\n",
    "plan/development/notes/tasks.md": "- [ ] 9.6 not in openspec/changes\n",
    "README.md": "# not a task list\n",
}


def link(path):
    return f'<p>Plan: <a href="https://github.com/{REPO}/blob/{BRANCH}/{path}">tasks.md</a></p>'


def describe(change_dir, change):
    return f"OpenSpec change {change} in {REPO}: https://github.com/{REPO}/tree/{BRANCH}/{change_dir}"


def module_dir(path):
    return path.rsplit("/", 1)[0]


def checkbox_lines(files):
    pattern = re.compile(r"^\s*[-*+]\s+\[[ xX]\]")
    return sum(1 for path, text in files.items()
               if lib.change_of(path) is not None
               for line in text.splitlines() if pattern.match(line))


# -- exact output ---------------------------------------------------------------------------------


def test_alpha_skip_reasons_and_precedence_are_exact():
    out = lib.parse_files({ALPHA: ALPHA_TEXT}, REPO, BRANCH)
    assert out["modules"] == [{
        "external_source": "openspec",
        "external_id": f"{REPO}/alpha",
        "name": "alpha",
        "description": describe(module_dir(ALPHA), "alpha"),
        "items": [
            {"external_source": "openspec", "external_id": f"{REPO}/alpha/1.1", "name": "1.1 Open task",
             "description_html": "<p>1.1 Open task</p>" + link(ALPHA)},
            {"external_source": "openspec", "external_id": f"{REPO}/alpha/1.5", "name": "1.5 first open",
             "description_html": "<p>1.5 first open</p>" + link(ALPHA)},
        ],
    }]
    assert out["skipped"] == [
        {"change": "alpha", "line": 3, "reason": "done before import"},
        {"change": "alpha", "line": 4, "reason": "superseded"},
        {"change": "alpha", "line": 5, "reason": "superseded"},
        {"change": "alpha", "line": 6, "reason": "no task id"},
        {"change": "alpha", "line": 7, "reason": "no task id"},
        {"change": "alpha", "line": 9, "reason": "duplicate id"},
        {"change": "alpha", "line": 10, "reason": "done before import"},
        {"change": "alpha", "line": 11, "reason": "duplicate id"},
        {"change": "alpha", "line": 14, "reason": "no task id"},
    ]


def test_task_id_shapes_are_exact():
    out = lib.parse_files({DELTA: DELTA_TEXT}, REPO, BRANCH)
    assert [(item["external_id"], item["name"]) for item in out["modules"][0]["items"]] == [
        (f"{REPO}/delta/1.2f.1", "1.2f.1 first boundary id"),
        (f"{REPO}/delta/1.2f.2", "1.2f.2 second boundary id"),
        (f"{REPO}/delta/5H.1", "5H.1 letter before the dot"),
        (f"{REPO}/delta/6S.1", "6S.1 another letter before the dot"),
        (f"{REPO}/delta/5P.1", "5P.1 P before the dot"),
        (f"{REPO}/delta/4.6c2", "4.6c2 digit after a letter suffix"),
        (f"{REPO}/delta/4.6d1a", "4.6d1a digit and letter suffix"),
        (f"{REPO}/delta/8.0a", "8.0a letter suffix"),
        (f"{REPO}/delta/8.0b", "8.0b open after a struck id is imported"),
    ]
    assert out["skipped"] == [{"change": "delta", "line": 9, "reason": "superseded"}]


def test_continuation_nesting_and_stops_are_exact():
    out = lib.parse_files({BETA: BETA_TEXT}, REPO, BRANCH)
    names = [(item["external_id"], item["name"]) for item in out["modules"][0]["items"]]
    assert names == [
        (f"{REPO}/beta/2.1", "2.1 Parent task with continuation"),
        (f"{REPO}/beta/2.2", "2.2 Next task"),
        (f"{REPO}/beta/2.4", "2.4 nested open task"),
        (f"{REPO}/beta/2.5", "2.5 Task before heading"),
        (f"{REPO}/beta/2.6", "2.6 Last task"),
    ]
    descriptions = [item["description_html"] for item in out["modules"][0]["items"]]
    assert descriptions == [
        "<p>2.1 Parent task with continuation second line of the parent deeper third line</p>" + link(BETA),
        "<p>2.2 Next task - sub bullet belongs to 2.2</p>" + link(BETA),
        "<p>2.4 nested open task deeper continuation of 2.4</p>" + link(BETA),
        "<p>2.5 Task before heading stays with 2.5</p>" + link(BETA),
        "<p>2.6 Last task</p>" + link(BETA),
    ]
    assert out["skipped"] == [{"change": "beta", "line": 7, "reason": "done before import"}]


def test_markdown_escaping_and_truncation_are_exact():
    out = lib.parse_files({GAMMA: GAMMA_TEXT}, REPO, BRANCH)
    items = {item["external_id"].rsplit("/", 1)[1]: item for item in out["modules"][0]["items"]}
    assert items["3.1"]["name"] == "3.1 Use bold, *italic*, _under_ and gone words"
    assert items["3.2"]["name"] == "3.2 See the docs, weft_mcp_pat, changes/*/tasks.md, **kept**"
    assert items["3.2"]["description_html"] == (
        "<p>3.2 See [the docs](https://example.test/docs), `weft_mcp_pat`, `changes/*/tasks.md`, `**kept**`</p>"
        + link(GAMMA))
    assert items["3.3"]["name"] == '3.3 Compare a < b & c > "d" it\'s'
    assert items["3.3"]["description_html"] == (
        "<p>3.3 Compare a &lt; b &amp; c &gt; &quot;d&quot; it&#x27;s</p>" + link(GAMMA))
    # 255 characters is kept whole; 256 is cut to 254 plus the ellipsis, so the result is 255.
    assert items["3.5"]["name"] == "3.5 " + "y" * 251
    assert len(items["3.5"]["name"]) == 255
    assert items["3.6"]["name"] == "3.6 " + "z" * 250 + "…"
    assert len(items["3.6"]["name"]) == 255
    assert items["3.4"]["name"] == "3.4 " + "x" * 250 + "…"
    # The description keeps the whole text; only the name is cut.
    assert items["3.6"]["description_html"] == "<p>3.6 " + "z" * 252 + "</p>" + link(GAMMA)


def test_layouts_archive_and_module_rules_are_exact():
    out = lib.parse_files(FIXTURE, REPO, BRANCH)
    assert [module["name"] for module in out["modules"]] == ["alpha", "archive-notes", "beta", "gamma"]
    for module in out["modules"]:
        assert module["external_source"] == "openspec"
        assert all(item["external_source"] == "openspec" for item in module["items"])
    assert all("external_source" not in entry for entry in out["skipped"])
    by_name = {module["name"]: module for module in out["modules"]}
    assert by_name["archive-notes"]["external_id"] == f"{REPO}/archive-notes"
    assert by_name["archive-notes"]["description"] == describe(module_dir(ARCHIVE_NOTES), "archive-notes")
    assert [item["external_id"] for item in by_name["archive-notes"]["items"]] == [f"{REPO}/archive-notes/5.1"]
    assert out["skipped"] == [
        {"change": "alpha", "line": 3, "reason": "done before import"},
        {"change": "alpha", "line": 4, "reason": "superseded"},
        {"change": "alpha", "line": 5, "reason": "superseded"},
        {"change": "alpha", "line": 6, "reason": "no task id"},
        {"change": "alpha", "line": 7, "reason": "no task id"},
        {"change": "alpha", "line": 9, "reason": "duplicate id"},
        {"change": "alpha", "line": 10, "reason": "done before import"},
        {"change": "alpha", "line": 11, "reason": "duplicate id"},
        {"change": "alpha", "line": 14, "reason": "no task id"},
        {"change": "done-only", "line": 1, "reason": "done before import"},
        {"change": "done-only", "line": None, "reason": "no open tasks"},
        {"change": "beta", "line": 7, "reason": "done before import"},
    ]


def test_conservation_every_checkbox_line_is_imported_or_reported():
    out = lib.parse_files(FIXTURE, REPO, BRANCH)
    imported = sum(len(module["items"]) for module in out["modules"])
    reported = sum(1 for entry in out["skipped"] if entry["line"] is not None)
    assert imported == 14
    assert imported + reported == checkbox_lines(FIXTURE) == 25


def test_output_does_not_depend_on_insertion_order():
    forward = lib.parse_files(FIXTURE, REPO, BRANCH)
    backward = lib.parse_files(dict(reversed(list(FIXTURE.items()))), REPO, BRANCH)
    assert forward == backward


def test_one_change_name_at_two_roots_is_refused():
    with pytest.raises(ValueError, match="appears at both"):
        lib.parse_files({
            "plan/development/openspec/changes/x/tasks.md": "- [ ] 1.1 a\n",
            "plans/development/openspec/changes/x/tasks.md": "- [ ] 1.1 b\n",
        }, REPO, BRANCH)


def test_change_of_selection_rule():
    assert lib.change_of("plan/development/openspec/changes/archive-notes/tasks.md") == "archive-notes"
    assert lib.change_of("plans/development/openspec/changes/beta/tasks.md") == "beta"
    assert lib.change_of("plan/development/openspec/changes/archive/old/tasks.md") is None
    assert lib.change_of("plan/archive/development/openspec/changes/ancient/tasks.md") is None
    assert lib.change_of("plan/development/openspec/changes/alpha/specs/tasks.md") is None
    assert lib.change_of("openspec/changes/tasks.md") is None
    assert lib.change_of("plan/development/openspec/changes/alpha/tasks.md.bak") is None


# -- checkout mode --------------------------------------------------------------------------------


def write_tree(root, files):
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def test_checkout_walk_matches_the_dict_parse(tmp_path):
    write_tree(tmp_path, FIXTURE)
    assert lib.parse_repo(tmp_path, REPO, BRANCH) == lib.parse_files(FIXTURE, REPO, BRANCH)


# -- GitHub stub ----------------------------------------------------------------------------------


class StubGitHub(DrainingHandler):
    state = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body, location=None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        s = self.state
        path = urllib.parse.urlsplit(self.path).path
        s["seen"].append((path, self.headers.get("Authorization")))
        if path.startswith("/repos/"):
            if s["tree_status"] != 200:
                self._send(s["tree_status"], b"{}")
                return
            body = json.dumps({"truncated": s["truncated"], "tree": s["tree"]}).encode()
            self._send(200, body)
        elif path in s["redirects"]:
            self._send(302, b"", location=s["redirects"][path])
        elif path in s["raw_fail"]:
            self._send(s["raw_fail"][path], b"refused")
        elif path in s["raw"]:
            self._send(200, s["raw"][path].encode())
        else:
            self._send(404, b"not found")


@pytest.fixture
def github():
    StubGitHub.state = {"tree": [], "truncated": False, "tree_status": 200, "raw": {}, "raw_fail": {},
                        "redirects": {}, "seen": []}
    server = http.server.HTTPServer(("127.0.0.1", 0), StubGitHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield StubGitHub.state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def publish(state, files):
    state["tree"] = [{"path": "README.md", "type": "blob"}, {"path": "plan", "type": "tree"}]
    state["tree"] += [{"path": path, "type": "blob"} for path in sorted(files)]
    state["raw"] = {f"/{REPO}/{BRANCH}/{path}": text for path, text in files.items()}


def run_cli(args, stdin=""):
    return subprocess.run([sys.executable, "-I", str(LIB_PATH), *args], input=stdin,
                          capture_output=True, text=True, timeout=60)


def test_fetch_sends_the_token_only_when_given(github):
    state, base = github
    publish(state, {ALPHA: ALPHA_TEXT})
    assert lib.fetch_files(REPO, BRANCH, None, api=base, raw=base) == {ALPHA: ALPHA_TEXT}
    assert state["seen"] and all(auth is None for _, auth in state["seen"])
    state["seen"].clear()
    assert lib.fetch_files(REPO, BRANCH, TOKEN, api=base, raw=base) == {ALPHA: ALPHA_TEXT}
    assert len(state["seen"]) == 2
    assert all(auth == f"Bearer {TOKEN}" for _, auth in state["seen"])


def test_truncated_tree_is_refused_before_any_fetch(github):
    state, base = github
    publish(state, {ALPHA: ALPHA_TEXT})
    state["truncated"] = True
    with pytest.raises(RuntimeError, match="truncated"):
        lib.fetch_files(REPO, BRANCH, None, api=base, raw=base)
    assert [path for path, _ in state["seen"]] == [f"/repos/{REPO}/git/trees/{BRANCH}"]


def test_non_200_error_names_status_and_path_and_never_the_token(github):
    state, base = github
    publish(state, {ALPHA: ALPHA_TEXT})
    state["raw_fail"] = {f"/{REPO}/{BRANCH}/{ALPHA}": 403}
    with pytest.raises(RuntimeError) as raw_error:
        lib.fetch_files(REPO, BRANCH, TOKEN, api=base, raw=base)
    assert "HTTP 403" in str(raw_error.value) and ALPHA in str(raw_error.value)
    assert TOKEN not in str(raw_error.value)

    state["raw_fail"] = {}
    state["tree_status"] = 401
    with pytest.raises(RuntimeError) as tree_error:
        lib.fetch_files(REPO, BRANCH, TOKEN, api=base, raw=base)
    assert "HTTP 401" in str(tree_error.value)
    assert TOKEN not in str(tree_error.value)


def test_archive_and_unselected_paths_are_never_fetched(github):
    state, base = github
    publish(state, FIXTURE)
    fetched = lib.fetch_files(REPO, BRANCH, None, api=base, raw=base)
    selected = sorted([ALPHA, ARCHIVE_NOTES, BETA, DONE_ONLY, GAMMA])
    assert sorted(fetched) == selected
    raw_requests = sorted(path.removeprefix(f"/{REPO}/{BRANCH}/") for path, _ in state["seen"]
                          if not path.startswith("/repos/"))
    assert raw_requests == selected


def test_token_is_not_forwarded_across_a_redirect(github):
    state, base = github
    publish(state, {ALPHA: ALPHA_TEXT})
    elsewhere = base.replace("127.0.0.1", "localhost")
    state["redirects"] = {f"/{REPO}/{BRANCH}/{ALPHA}": f"{elsewhere}/elsewhere/alpha"}
    state["raw"]["/elsewhere/alpha"] = ALPHA_TEXT
    assert lib.fetch_files(REPO, BRANCH, TOKEN, api=base, raw=base) == {ALPHA: ALPHA_TEXT}
    auth_by_path = dict(state["seen"])
    assert auth_by_path[f"/repos/{REPO}/git/trees/{BRANCH}"] == f"Bearer {TOKEN}"
    assert auth_by_path["/elsewhere/alpha"] is None


def test_cli_github_output_equals_checkout_output_for_the_same_bytes(github, tmp_path):
    state, base = github
    publish(state, FIXTURE)
    write_tree(tmp_path, FIXTURE)
    via_github = run_cli(["--repo", REPO, "--branch", BRANCH, "--github", "--token-stdin",
                          "--api", base, "--raw", base], stdin=TOKEN + "\n")
    assert via_github.returncode == 0, via_github.stderr
    via_checkout = run_cli(["--repo", REPO, "--branch", BRANCH, "--checkout", str(tmp_path)])
    assert via_checkout.returncode == 0, via_checkout.stderr
    expected = lib.parse_repo(tmp_path, REPO, BRANCH)
    assert json.loads(via_github.stdout) == expected
    assert json.loads(via_checkout.stdout) == expected
    assert via_github.stdout.count("\n") == 1
    assert all(auth == f"Bearer {TOKEN}" for _, auth in state["seen"])
    assert TOKEN not in via_github.stdout + via_github.stderr


def test_cli_error_path_prints_no_token(github):
    state, base = github
    state["tree_status"] = 401
    out = run_cli(["--repo", REPO, "--github", "--token-stdin", "--api", base, "--raw", base],
                  stdin=TOKEN + "\n")
    assert out.returncode != 0
    assert "HTTP 401" in out.stderr
    assert TOKEN not in out.stdout + out.stderr


def test_token_stdin_without_github_is_a_usage_error(tmp_path):
    out = run_cli(["--repo", REPO, "--checkout", str(tmp_path), "--token-stdin"])
    assert out.returncode == 2
    assert "requires --github" in out.stderr


# -- this repository ------------------------------------------------------------------------------


def test_cli_reads_this_repository_checkout():
    out = run_cli(["--repo", "uhstray-io/agent-cloud", "--checkout", str(REPO_ROOT)])
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    # No change is named: any one of them is archived sooner or later, and this must not break then.
    assert data["modules"]
    assert all(module["external_id"] == "uhstray-io/agent-cloud/" + module["name"] for module in data["modules"])
    ids = [item["external_id"] for module in data["modules"] for item in module["items"]]
    assert len(ids) == len(set(ids))
    assert all(len(external_id) <= 255 for external_id in ids)
    assert all(len(item["name"]) <= 255 for module in data["modules"] for item in module["items"])
