"""The retired-field declaration, and the playbook that removes what it declares.

retire-openbao-fields.yml deletes OpenBao KV-v2 fields. These tests read the declaration and the
playbook, and run the playbook:

- the declaration is well formed, and it keeps the seeded fields;
- the playbook's guards, read from its parsed structure: the refusal comes before any OpenBao request,
  the reads run in check mode, the merge is live-only and planned per path, no visible task prints a
  registered result, no_log covers only the credential tasks, and the OpenBao URL is the single-host
  pattern;
- the real tree: no retired field is declared in a file the playbook scans. Nothing on the tree
  declares a retired field;
- behaviour: the real playbook runs under ansible-playbook against a local KV-v2 double, in a fixture
  tree with its own declaration and deploy files. A run that fails the refusal makes no OpenBao request.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import playbook_yaml
import pytest
import yaml
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
PLAYBOOK = PLAYBOOKS / "retire-openbao-fields.yml"
DECLARATION = PLAYBOOKS / "vars/retired-secret-fields.yml"
REFUSE = PLAYBOOKS / "refuse-internal-extra-vars.yml"
TASKS = PLAYBOOKS / "tasks"
# The single-host OpenBao address expression; tasks/guard-o11y-restore-target.yml:14 uses the same one.
SINGLE_HOST_BAO_URL = (
    "{{ hostvars[(groups['openbao_svc'] | default([]) | first)].service_url | default('') "
    "if (groups['openbao_svc'] | default([]) | length == 1) else '' }}"
)
TEMPLATES = REPO / "platform/semaphore/templates.yml"

SEEDED = {
    ("services/tududi", "tududi_session_secret"),
    ("services/tududi", "tududi_admin_password"),
    ("services/tududi", "api_token"),
    ("services/authentik", "tududi_oidc_client_secret"),
    ("services/github", "tududi_sync_app_key"),
    ("services/github", "tududi_sync_app_client_id"),
}
# The playbook's refusal matches a field as a whole word. The same pattern is used here, so the real-tree
# scan and the playbook agree.
WHOLE_WORD = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])"
PROTECTED = ("_bao_auth", "_reads", "_readback")
HELD = "held-value-7f3a91"
KEPT = "KEPTVALUE-2b8c40"
DEMO = [{"path": "services/demo", "fields": ["old_token", "absent_one"], "retired": "2026-11-09", "reason": "fixture"}]


def _declaration() -> list[dict]:
    return yaml.safe_load(DECLARATION.read_text())["retired_secret_fields"]


def _play() -> dict:
    plays = playbook_yaml.plays(PLAYBOOK)
    assert len(plays) == 1, "the playbook is one play after the extra-var guard import"
    return plays[0]


def _tasks() -> list[dict]:
    return list(playbook_yaml.tasks(_play()["tasks"]))


def _only(tasks: list[dict], predicate, what: str) -> int:
    found = [index for index, task in enumerate(tasks) if predicate(task)]
    assert len(found) == 1, f"{what}: {len(found)} found"
    return found[0]


def _is_uri(task: dict, method: str | None = None, fragment: str = "") -> bool:
    for key in ("ansible.builtin.uri", "uri"):
        if key in task:
            args = task[key]
            wanted = method is None or str(args.get("method", "GET")).upper() == method
            return wanted and fragment in str(args.get("url", ""))
    return False


def _is_include(task: dict, target: str) -> bool:
    return str(task.get("ansible.builtin.include_tasks", task.get("include_tasks", ""))).endswith(target)


def _sources(playbooks: Path, path: str) -> list[Path]:
    """The files a path's refusal scans: its own secret-declarations file and its own deploy file."""
    svc = path.removeprefix("services/")
    candidates = (playbooks / "vars/secret-declarations" / f"{svc}.yml", playbooks / f"deploy-{svc}.yml")
    return [candidate for candidate in candidates if candidate.is_file()]


def _literal_hits(playbooks: Path, entries: list[dict]) -> list[tuple[str, str, str]]:
    hits = []
    for entry in entries:
        for source in _sources(playbooks, entry["path"]):
            text = source.read_text()
            for field in entry["fields"]:
                if re.search(WHOLE_WORD.format(field), text):
                    hits.append((entry["path"], field, source.relative_to(playbooks).as_posix()))
    return hits


def _structural_names(node):
    """Every `{name:, type:}` declaration anywhere in a parsed document."""
    if isinstance(node, dict):
        if isinstance(node.get("name"), str) and "type" in node:
            yield node["name"]
        for value in node.values():
            yield from _structural_names(value)
    elif isinstance(node, list):
        for item in node:
            yield from _structural_names(item)


# ── (a) the declaration ──────────────────────────────────────────────────────


def test_declaration_entries_have_exactly_the_declared_keys():
    entries = _declaration()
    assert entries, "retired_secret_fields must not be empty"
    for entry in entries:
        assert set(entry) == {"path", "fields", "retired", "reason"}, entry


def test_paths_are_services_names_and_unique():
    paths = [entry["path"] for entry in _declaration()]
    assert len(paths) == len(set(paths)), "a path is listed twice"
    for path in paths:
        assert re.fullmatch(r"services/[a-z0-9-]+", path), path


def test_fields_are_nonempty_well_formed_and_unique_within_a_path():
    for entry in _declaration():
        fields = entry["fields"]
        assert isinstance(fields, list) and fields, entry
        assert len(fields) == len(set(fields)), f"{entry['path']} lists a field twice"
        for field in fields:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", field), field


def test_retired_is_a_quoted_iso_date():
    for entry in _declaration():
        # An unquoted 2026-11-09 parses as a date object, not the string the playbook would compare.
        assert isinstance(entry["retired"], str), entry
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["retired"]), entry


def test_reasons_are_one_nonblank_line():
    for entry in _declaration():
        reason = entry["reason"]
        assert isinstance(reason, str) and reason.strip() and "\n" not in reason, entry


def test_declaration_keeps_the_seeded_fields():
    assert {(entry["path"], field) for entry in _declaration() for field in entry["fields"]} == SEEDED


# ── (b) the real tree ─────────────────────────────────────────────────────────


def test_retired_fields_are_not_declared_in_any_source_the_playbook_scans():
    assert _literal_hits(PLAYBOOKS, _declaration()) == []


def test_the_whole_word_scan_catches_every_structural_declaration_in_the_real_tree():
    # The playbook matches literally, not structurally, because some declarations are Jinja templates.
    # This pins the direction that matters: every declaration a parse can see is one the scan also finds.
    for entry in _declaration():
        for source in _sources(PLAYBOOKS, entry["path"]):
            text = source.read_text()
            declared = set(_structural_names(playbook_yaml.load(source)))
            for field in entry["fields"]:
                if field in declared:
                    assert re.search(WHOLE_WORD.format(field), text), (source.name, field)


# ── (c) the playbook's guards, read from its structure ───────────────────────


def test_refusal_precedes_the_transport_guard_and_every_openbao_request():
    tasks = _tasks()
    refusal = _only(
        tasks,
        lambda t: "ansible.builtin.assert" in t and "_declared_hits" in str(t["ansible.builtin.assert"]["that"]),
        "the declared-field refusal",
    )
    guard = _only(tasks, lambda t: _is_include(t, "tasks/assert-bao-transport.yml"), "the transport guard")
    requests = [index for index, task in enumerate(tasks) if _is_uri(task)]
    assert requests, "the playbook makes no OpenBao request"
    assert refusal < guard < min(requests), "the refusal must run before the guard and every request"


def test_the_refusal_scans_only_the_paths_own_two_files_as_whole_words():
    text = PLAYBOOK.read_text()
    assert "'/vars/secret-declarations/' ~ svc ~ '.yml'" in text
    assert "'/deploy-' ~ svc ~ '.yml'" in text
    # A glob over the repository would match another service's file: the bare-name refusal the declaration
    # rules out (skynet declares api_token for its own path).
    assert "/deploy-*" not in text and "**" not in text
    assert "(?<![A-Za-z0-9_])' ~ field ~ '(?![A-Za-z0-9_])" in text


def test_openbao_url_is_the_single_host_pattern():
    def bao_url(path: Path) -> str:
        play = next(play for play in playbook_yaml.plays(path) if "_bao_url" in (play.get("vars") or {}))
        return play["vars"]["_bao_url"]

    assert bao_url(PLAYBOOK) == SINGLE_HOST_BAO_URL


def test_reads_are_check_mode_reads_with_both_statuses_and_no_log():
    tasks = _tasks()
    gets = [task for task in tasks if _is_uri(task, "GET", "/v1/secret/data/")]
    assert len(gets) == 2, "one read of each declared path, and one read back after the merge"
    for task in gets:
        assert task.get("check_mode") is False, task["name"]
        assert task.get("changed_when") is False, task["name"]
        assert task.get("no_log") is True, task["name"]
        assert sorted(task["ansible.builtin.uri"]["status_code"]) == [200, 404], task["name"]


def test_login_is_a_no_log_credential_read():
    tasks = _tasks()
    login = tasks[_only(tasks, lambda t: _is_uri(t, "POST", "/v1/auth/approle/login"), "the AppRole login")]
    assert login.get("check_mode") is False and login.get("changed_when") is False, login["name"]
    assert login.get("no_log") is True, login["name"]


def test_no_log_is_only_on_the_login_and_the_two_reads():
    names = {task["name"] for task in _tasks() if task.get("no_log") is True}
    assert names == {
        "Authenticate to OpenBao",
        "Read each declared path (a read, also in check mode; the values are never printed)",
        "Read each merged path back (live only; the values are never printed)",
    }


def test_no_visible_debug_or_assert_prints_a_registered_result():
    for task in _tasks():
        if ("ansible.builtin.debug" in task or "ansible.builtin.assert" in task) and task.get("no_log") is not True:
            text = " ".join(playbook_yaml.strings(task))
            for name in PROTECTED:
                assert not re.search(rf"\b{name}\b", text), f"{task['name']} prints {name}"


def test_no_visible_loop_iterates_a_registered_result():
    for task in _tasks():
        if "loop" in task:
            text = " ".join(playbook_yaml.strings(task["loop"]))
            for name in PROTECTED:
                assert not re.search(rf"\b{name}\b", text), f"{task['name']} loops over {name}"


def test_the_merge_is_live_only_and_planned_per_path_with_a_held_field():
    tasks = _tasks()
    merge = tasks[_only(tasks, lambda t: _is_include(t, "tasks/bao-merge-keys.yml"), "the shared merge")]
    when = merge["when"] if isinstance(merge["when"], list) else [merge["when"]]
    assert "not ansible_check_mode" in when, "the merge must be skipped under --check"
    assert merge["vars"]["_bm_remove"] == "{{ item.fields }}", "the merge removes the planned fields, as nulls"
    assert merge["vars"]["_bm_path"] == "{{ item.path }}"
    assert "_bm_data" not in merge["vars"], "a removal sets no value"
    assert "selectattr('status', 'equalto', 'present')" in merge["loop"], "only a path that holds a planned field"


def test_no_task_gives_the_merge_a_fail_on_missing_path():
    for task in _tasks():
        assert "_bm_on_missing" not in (task.get("vars") or {}), task["name"]


def test_the_playbook_writes_only_through_the_shared_merge_and_the_login():
    tasks = _tasks()
    methods = [str(task["ansible.builtin.uri"].get("method", "GET")).upper() for task in tasks if _is_uri(task)]
    assert "PATCH" not in methods and "DELETE" not in methods and "PUT" not in methods, methods
    posts = [task for task in tasks if _is_uri(task, "POST")]
    assert len(posts) == 1 and posts[0]["ansible.builtin.uri"]["url"].endswith("/v1/auth/approle/login")
    includes = {str(t.get("ansible.builtin.include_tasks", "")) for t in tasks if "ansible.builtin.include_tasks" in t}
    assert includes == {"tasks/assert-bao-transport.yml", "tasks/bao-merge-keys.yml"}, includes


def test_the_read_back_follows_the_merge_and_both_are_live_only():
    tasks = _tasks()
    merge = _only(tasks, lambda t: _is_include(t, "tasks/bao-merge-keys.yml"), "the shared merge")
    readback = _only(tasks, lambda t: _is_uri(t, "GET") and t.get("register") == "_readback", "the read back")
    remaining = _only(tasks, lambda t: "_remaining" in str(t.get("ansible.builtin.set_fact", "")), "remaining")
    assert merge < readback < remaining
    for index in (readback, remaining):
        when = tasks[index].get("when")
        assert "not ansible_check_mode" in (when if isinstance(when, list) else [when]), tasks[index]["name"]


def test_the_template_is_a_dev_variant_declared_after_retire_semaphore_templates():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    names = [template["name"] for template in templates]
    template = templates[names.index("Retire OpenBao Fields")]
    assert template["playbook"] == "platform/playbooks/retire-openbao-fields.yml"
    assert template.get("dev_variant") is True
    assert "survey_vars" not in template and "survey" not in template
    assert names.index("Retire OpenBao Fields") == names.index("Retire Semaphore Templates") + 1


# ── (d) behaviour: the real playbook against a local KV-v2 double ───────────────

requires_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


class _Vault:
    def __init__(self, paths: dict):
        self.paths = {path: (dict(data) if data is not None else None) for path, data in paths.items()}
        self.requests: list[tuple[str, str, object]] = []

    def writes(self) -> list[tuple[str, str, object]]:
        return [request for request in self.requests if request[1].startswith("/v1/secret/") and request[0] != "GET"]


def _handler(vault: _Vault):
    class Handler(DrainingHandler):
        def log_message(self, *_args):
            pass

        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

        def _reply(self, status: int, body: dict):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = self._body()
            vault.requests.append(("POST", self.path, body))
            if self.path == "/v1/auth/approle/login":
                self._reply(200, {"auth": {"client_token": "fixture-token"}})
            else:
                self._reply(404, {"errors": []})

        def do_GET(self):
            vault.requests.append(("GET", self.path, None))
            path = self.path.removeprefix("/v1/secret/data/")
            data = vault.paths.get(path)
            if data is None:
                self._reply(404, {"errors": []})
            else:
                self._reply(200, {"data": {"data": dict(data), "metadata": {"version": 1}}})

        def do_PATCH(self):
            body = self._body()
            vault.requests.append(("PATCH", self.path, body))
            path = self.path.removeprefix("/v1/secret/data/")
            data = vault.paths.get(path)
            if data is None:
                self._reply(404, {"errors": []})
                return
            for key, value in body["data"].items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            self._reply(200, {"data": {"version": 2}})

    return Handler


@contextmanager
def _bao(paths: dict):
    vault = _Vault(paths)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(vault))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield vault, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _tree(
    tmp_path: Path,
    declaration: list[dict],
    deploys: dict[str, str] | None = None,
    declarations: dict[str, str] | None = None,
) -> Path:
    root = tmp_path / "playbooks"
    (root / "vars/secret-declarations").mkdir(parents=True)
    shutil.copy(PLAYBOOK, root / PLAYBOOK.name)
    shutil.copy(REFUSE, root / REFUSE.name)
    shutil.copytree(TASKS, root / "tasks")
    (root / "vars/retired-secret-fields.yml").write_text(yaml.safe_dump({"retired_secret_fields": declaration}))
    for name, text in (deploys or {}).items():
        (root / name).write_text(text)
    for name, text in (declarations or {}).items():
        (root / "vars/secret-declarations" / name).write_text(text)
    return root


def _inventory(tmp_path: Path, url: str) -> Path:
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(
        yaml.safe_dump(
            {
                "all": {
                    "hosts": {"localhost": {"ansible_connection": "local"}},
                    "children": {
                        "openbao_svc": {
                            "hosts": {"openbao-fixture": {"ansible_connection": "local", "service_url": url}}
                        }
                    },
                }
            }
        )
    )
    return inventory


def _run(root: Path, inventory: Path, *, check: bool) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "ANSIBLE_CONFIG"}
    env.update({"ANSIBLE_NOCOLOR": "1", "BAO_ROLE_ID": "fixture-role", "BAO_SECRET_ID": "fixture-secret"})
    argv = ["ansible-playbook", "-i", str(inventory), str(root / PLAYBOOK.name)]
    if check:
        argv.append("--check")
    # cwd is the repository: its ansible.cfg supplies the filter plugins and the redacting callback.
    return subprocess.run(
        argv, cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=300
    )


def _out(done: subprocess.CompletedProcess) -> str:
    return done.stdout + done.stderr


@requires_ansible
@pytest.mark.parametrize("check", [True, False], ids=["check", "live"])
def test_only_the_held_declared_fields_are_planned_and_no_value_is_printed(tmp_path, check):
    paths = {"services/demo": {"old_token": HELD, "keep": KEPT}}
    with _bao(paths) as (vault, url):
        done = _run(_tree(tmp_path, DEMO), _inventory(tmp_path, url), check=check)
    out = _out(done)
    assert done.returncode == 0, out[-2000:]
    assert "services/demo: holds old_token (to remove)" in out, out[-2000:]
    assert HELD not in out and KEPT not in out, "a field value reached the output"
    if check:
        assert vault.writes() == [], "check mode wrote"
        assert vault.paths == paths
    else:
        assert vault.writes() == [("PATCH", "/v1/secret/data/services/demo", {"data": {"old_token": None}})]
        assert vault.paths == {"services/demo": {"keep": KEPT}}, "a sibling was lost or the field survived"


@requires_ansible
@pytest.mark.parametrize("check", [True, False], ids=["check", "live"])
def test_an_absent_path_is_a_no_op(tmp_path, check):
    with _bao({"services/demo": None}) as (vault, url):
        done = _run(_tree(tmp_path, DEMO), _inventory(tmp_path, url), check=check)
    assert done.returncode == 0, _out(done)[-2000:]
    assert "services/demo: absent (no-op)" in _out(done)
    assert vault.writes() == [] and vault.paths == {"services/demo": None}


@requires_ansible
@pytest.mark.parametrize("check", [True, False], ids=["check", "live"])
def test_a_path_holding_no_declared_field_is_a_no_op(tmp_path, check):
    with _bao({"services/demo": {"keep": KEPT}}) as (vault, url):
        done = _run(_tree(tmp_path, DEMO), _inventory(tmp_path, url), check=check)
    assert done.returncode == 0, _out(done)[-2000:]
    assert "services/demo: holds no declared field (no-op)" in _out(done)
    assert vault.writes() == [] and vault.paths == {"services/demo": {"keep": KEPT}}


@requires_ansible
def test_a_declared_field_still_in_the_paths_deploy_refuses_before_any_request(tmp_path):
    declaration = [{"path": "services/demo", "fields": ["old_token"], "retired": "2026-11-09", "reason": "fixture"}]
    deploys = {
        "deploy-demo.yml": "- name: Deploy demo\n  hosts: demo_svc\n  vars:\n"
        "    _secret_definitions:\n"
        "      - { name: old_token, type: random, length: 40 }\n"
    }
    with _bao({"services/demo": {"old_token": HELD}}) as (vault, url):
        done = _run(_tree(tmp_path, declaration, deploys=deploys), _inventory(tmp_path, url), check=False)
    out = _out(done)
    assert done.returncode != 0, out[-2000:]
    assert "services/demo old_token declared in deploy-demo.yml" in out, out[-2000:]
    assert vault.requests == [], "the refusal ran after OpenBao was reached"
    assert HELD not in out


@requires_ansible
def test_a_declared_field_in_the_paths_secret_declarations_file_refuses(tmp_path):
    declaration = [{"path": "services/demo", "fields": ["old_token"], "retired": "2026-11-09", "reason": "fixture"}]
    declarations = {"demo.yml": "_static_secret_definitions:\n  - { name: old_token, type: random, length: 40 }\n"}
    with _bao({"services/demo": {"old_token": HELD}}) as (vault, url):
        done = _run(_tree(tmp_path, declaration, declarations=declarations), _inventory(tmp_path, url), check=True)
    out = _out(done)
    assert done.returncode != 0, out[-2000:]
    assert "old_token declared in vars/secret-declarations/demo.yml" in out, out[-2000:]
    assert vault.requests == []


@requires_ansible
def test_a_name_declared_for_another_service_does_not_refuse(tmp_path):
    declaration = [{"path": "services/demo", "fields": ["api_token"], "retired": "2026-11-09", "reason": "fixture"}]
    deploys = {"deploy-skynet.yml": "      - { name: api_token, type: random, length: 48 }\n"}
    with _bao({"services/demo": {"api_token": HELD}}) as (vault, url):
        done = _run(_tree(tmp_path, declaration, deploys=deploys), _inventory(tmp_path, url), check=True)
    out = _out(done)
    assert done.returncode == 0, out[-2000:]
    assert "services/demo: holds api_token (to remove)" in out, out[-2000:]


@requires_ansible
def test_a_field_name_inside_a_longer_name_does_not_refuse(tmp_path):
    declaration = [{"path": "services/demo", "fields": ["token"], "retired": "2026-11-09", "reason": "fixture"}]
    deploys = {"deploy-demo.yml": "      - { name: github_token, type: random, length: 40 }\n"}
    with _bao({"services/demo": {"token": HELD}}) as (vault, url):
        done = _run(_tree(tmp_path, declaration, deploys=deploys), _inventory(tmp_path, url), check=True)
    out = _out(done)
    assert done.returncode == 0, out[-2000:]
    assert "services/demo: holds token (to remove)" in out, out[-2000:]
