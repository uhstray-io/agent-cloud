"""The copy declaration, and the playbook that copies OpenBao KV-v2 fields between paths.

copy-openbao-fields.yml reads a value from one KV-v2 field and writes it to another, inside the store.
These tests read the declaration and the playbook, and run the playbook against a local KV-v2 double:

- the declaration is well formed, and it keeps the nemoclaw -> honcho copy;
- the playbook's structure, read from its parsed form: the extra-var guard is first, the shape check comes
  before the transport guard and every OpenBao request, the reads run in check mode, no_log covers only the
  credential tasks, nothing visible prints or loops over a registered result, the merge is live-only and
  planned per pending entry, and every write goes through the shared merge;
- the template is a Dev variant declared after Retire OpenBao Fields, and AGENTS.md lists the playbook after
  its neighbour;
- behaviour: a pending target is written beside its siblings and then equals its source; a value never
  reaches the output; a second run writes nothing; a conflict, a missing source or a malformed declaration
  refuses before any write.
"""

from __future__ import annotations

import json
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

import fake_http
import harness_sandbox
import playbook_yaml
import pytest
import yaml
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
PLAYBOOK = PLAYBOOKS / "copy-openbao-fields.yml"
DECLARATION = PLAYBOOKS / "vars/copied-secret-fields.yml"
REFUSE = PLAYBOOKS / "refuse-internal-extra-vars.yml"
TASKS = PLAYBOOKS / "tasks"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
AGENTS = REPO / "AGENTS.md"
# openbao_addr first (the shared environment sets it), else the inventory's single openbao_svc host.
OPENBAO_URL_EXPRESSION = (
    "{{ openbao_addr | default(hostvars[(groups['openbao_svc'] | default([]) | first)].service_url | default('') "
    "if (groups['openbao_svc'] | default([]) | length == 1) else '', true) }}"
)
NEMOCLAW_TO_HONCHO = {
    "from_path": "services/nemoclaw",
    "from_field": "gemini_api_key",
    "to_path": "services/honcho",
    "to_field": "gemini_api_key",
    "copied": "2026-11-11",
    "reason": "NemoClaw retired (agent-cloud #1043); honcho owns its Gemini key",
}
SOURCE = "services/src"
TARGET = "services/dst"
HELD = "held-value-5c1e77"
KEPT = "KEPTVALUE-9a2d04"
OTHER = "OTHERVALUE-3f8b21"
SECOND = "SECONDVALUE-7d4e19"
# The registered results of the playbook's reads, and the login's. A visible task may not print or loop over
# them (test_no_request_in_loop_items.py has the same rule across the repository).
PROTECTED = ("_bao_auth", "_src_reads", "_dst_reads", "_dst_readback")


def _copy(**overrides) -> dict:
    """A declared copy of SOURCE/token to TARGET/token, with any field overridden for one case."""
    entry = {
        "from_path": SOURCE,
        "from_field": "token",
        "to_path": TARGET,
        "to_field": "token",
        "copied": "2026-11-11",
        "reason": "fixture",
    }
    entry.update(overrides)
    return entry


def _declaration() -> list[dict]:
    return yaml.safe_load(DECLARATION.read_text())["copied_secret_fields"]


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


def _named(tasks: list[dict], prefix: str) -> int:
    return _only(tasks, lambda t: str(t.get("name", "")).startswith(prefix), prefix)


def _is_uri(task: dict, method: str | None = None, fragment: str = "") -> bool:
    for key in ("ansible.builtin.uri", "uri"):
        if key in task:
            args = task[key]
            wanted = method is None or str(args.get("method", "GET")).upper() == method
            return wanted and fragment in str(args.get("url", ""))
    return False


def _is_include(task: dict, target: str) -> bool:
    return str(task.get("ansible.builtin.include_tasks", task.get("include_tasks", ""))).endswith(target)


def _when(task: dict) -> list:
    when = task.get("when")
    return when if isinstance(when, list) else [when]


# ── (a) the declaration ──────────────────────────────────────────────────────


def test_declaration_entries_have_exactly_the_declared_keys():
    entries = _declaration()
    assert entries, "copied_secret_fields must not be empty"
    for entry in entries:
        assert set(entry) == {"from_path", "from_field", "to_path", "to_field", "copied", "reason"}, entry


def test_paths_and_fields_are_well_formed():
    for entry in _declaration():
        for path in (entry["from_path"], entry["to_path"]):
            assert re.fullmatch(r"services/[a-z0-9-]+", path), path
        for field in (entry["from_field"], entry["to_field"]):
            assert re.fullmatch(r"[a-z][a-z0-9_]*", field), field


def test_no_entry_copies_a_field_onto_itself_and_no_target_is_named_twice():
    entries = _declaration()
    targets = [(entry["to_path"], entry["to_field"]) for entry in entries]
    assert len(targets) == len(set(targets)), "two entries name one target"
    for entry in entries:
        assert (entry["from_path"], entry["from_field"]) != (entry["to_path"], entry["to_field"]), entry


def test_copied_is_a_quoted_iso_date():
    for entry in _declaration():
        # An unquoted 2026-11-11 parses as a date object, not the string the playbook would compare.
        assert isinstance(entry["copied"], str), entry
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["copied"]), entry


def test_reasons_are_one_nonblank_line():
    for entry in _declaration():
        reason = entry["reason"]
        assert isinstance(reason, str) and reason.strip() and "\n" not in reason, entry


def test_declaration_keeps_the_nemoclaw_to_honcho_copy():
    assert NEMOCLAW_TO_HONCHO in _declaration()


# ── (b) the playbook's structure ─────────────────────────────────────────────


def test_the_extra_var_guard_is_the_first_document_entry():
    document = playbook_yaml.load(PLAYBOOK)
    assert document[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"


def test_the_declaration_is_checked_before_the_transport_guard_and_every_openbao_request():
    tasks = _tasks()
    shape = _named(tasks, "Require a well-formed declaration")
    guard = _only(tasks, lambda t: _is_include(t, "tasks/assert-bao-transport.yml"), "the transport guard")
    requests = [index for index, task in enumerate(tasks) if _is_uri(task)]
    assert requests, "the playbook makes no OpenBao request"
    assert shape < guard < min(requests), "the shape check must run before the guard and every request"


def test_the_shape_check_refuses_a_self_copy_a_shared_target_and_a_chained_copy():
    tasks = _tasks()
    shape = tasks[_named(tasks, "Require a well-formed declaration")]
    assert "_problems" in shape["vars"]
    text = " ".join(playbook_yaml.strings(shape["vars"]))
    assert "onto itself" in text and "named by two entries" in text
    # Pin the statement that appends the chained refusal, not the comment that describes it (which also says
    # "chained copy"): the statement is what the refusal is.
    assert "problems.append('copies ' ~ source ~ ', which another entry writes (chained copy)')" in text


def test_the_plan_precedes_the_refusal_then_the_write_then_the_read_back_and_comparison():
    tasks = _tasks()
    plan = _named(tasks, "Plan each copy")
    refusal = _named(tasks, "Refuse a copy whose source")
    merge = _only(tasks, lambda t: _is_include(t, "tasks/bao-merge-keys.yml"), "the shared merge")
    readback = _only(tasks, lambda t: _is_uri(t, "GET") and t.get("register") == "_dst_readback", "the read back")
    compare = _named(tasks, "Compare each pending target")
    assert plan < refusal < merge < readback < compare


def test_openbao_url_prefers_openbao_addr_then_the_single_host():
    assert _play()["vars"]["_bao_url"] == OPENBAO_URL_EXPRESSION


def test_every_read_is_a_check_mode_read_with_both_statuses_and_no_log():
    gets = [task for task in _tasks() if _is_uri(task, "GET", "/v1/secret/data/")]
    assert len(gets) == 3, "the source read, the target read and the read back"
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


def test_no_log_is_only_on_the_login_the_reads_the_plan_and_the_comparison():
    names = {task["name"] for task in _tasks() if task.get("no_log") is True}
    assert names == {
        "Authenticate to OpenBao",
        "Read each source path (a read, also in check mode; the values are never printed)",
        "Read each target path (a read, also in check mode; the values are never printed)",
        "Plan each copy from the live state (states and field NAMES only; values are compared here, never printed)",
        "Read each pending target back (live only; the values are never printed)",
        "Compare each pending target with its source after the write (names only)",
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


def test_the_merge_is_live_only_planned_per_pending_entry_and_creates_or_guards_the_target():
    tasks = _tasks()
    merge = tasks[_only(tasks, lambda t: _is_include(t, "tasks/bao-merge-keys.yml"), "the shared merge")]
    assert "not ansible_check_mode" in _when(merge), "the merge must be skipped under --check"
    assert "selectattr('state', 'equalto', 'pending')" in merge["loop"], "only a pending entry is written"
    args = merge["vars"]
    assert args["_bm_path"] == "{{ item.to_path }}"
    assert args["_bm_on_missing"] == "create", "an absent target path is created, not refused"
    assert args["_bm_guard_absent_keys"] == ["{{ item.to_field }}"], "a target field that appeared is refused"
    guards = {"_bm_remove", "_bm_guard_values", "_bm_guard_key"}
    assert not guards & set(args), "the merge sets, it does not guard others"
    assert "_src_reads.results[item.index]" in args["_bm_data"] and "item.from_field" in args["_bm_data"]
    assert "item.to_field" in args["_bm_data"]


def test_the_playbook_writes_only_through_the_shared_merge_and_the_login():
    tasks = _tasks()
    methods = {str(task["ansible.builtin.uri"].get("method", "GET")).upper() for task in tasks if _is_uri(task)}
    assert methods == {"GET", "POST"}, methods
    posts = [task for task in tasks if _is_uri(task, "POST")]
    assert len(posts) == 1 and posts[0]["ansible.builtin.uri"]["url"].endswith("/v1/auth/approle/login")
    includes = {str(task["ansible.builtin.include_tasks"]) for task in tasks if "ansible.builtin.include_tasks" in task}
    assert includes == {"tasks/assert-bao-transport.yml", "tasks/bao-merge-keys.yml"}, includes


def test_the_read_back_and_its_comparison_follow_the_merge_and_are_live_only_over_the_same_list():
    tasks = _tasks()
    merge = _only(tasks, lambda t: _is_include(t, "tasks/bao-merge-keys.yml"), "the shared merge")
    readback = _only(tasks, lambda t: _is_uri(t, "GET") and t.get("register") == "_dst_readback", "the read back")
    compare = _named(tasks, "Compare each pending target")
    check = _named(tasks, "Require every pending target")
    assert merge < readback < compare < check
    # The read back and the merge loop over one list, so result i is the read of merge item i.
    assert tasks[readback]["loop"] == tasks[merge]["loop"]
    assert "selectattr('state', 'equalto', 'pending')" in tasks[compare]["vars"]["_copy_unequal_json"]
    for index in (readback, compare, check):
        assert "not ansible_check_mode" in _when(tasks[index]), tasks[index]["name"]


def test_the_template_is_a_dev_variant_declared_directly_after_retire_openbao_fields():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    names = [template["name"] for template in templates]
    assert names.count("Copy OpenBao Fields") == 1
    template = templates[names.index("Copy OpenBao Fields")]
    assert template["playbook"] == "platform/playbooks/copy-openbao-fields.yml"
    assert template.get("dev_variant") is True
    assert "survey_vars" not in template and "survey" not in template
    assert names.index("Copy OpenBao Fields") == names.index("Retire OpenBao Fields") + 1


def test_agents_md_lists_the_playbook_directly_after_the_retire_row():
    rows = [line for line in AGENTS.read_text().splitlines() if line.startswith("| ")]
    names = [line.split("|")[1].strip() for line in rows]
    assert names.count("Copy OpenBao Fields") == 1
    assert names.index("Copy OpenBao Fields") == names.index("Retire OpenBao Fields") + 1
    assert "`copy-openbao-fields.yml`" in rows[names.index("Copy OpenBao Fields")]


# ── (c) behaviour: the real playbook against a local KV-v2 double ───────────────

requires_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


class _Vault:
    """A KV-v2 double: path -> data (None when the path is absent), with a version per present path.

    `interleave` maps a path to (n, field, value): before the n-th GET of that path, a concurrent writer sets
    `field` to `value` and bumps the version. It stands in for a write that lands between the playbook's plan
    read and the merge's own fresh read (the merge's guard must then refuse)."""

    def __init__(self, paths: dict, interleave: dict | None = None):
        self.paths = {path: (dict(data) if data is not None else None) for path, data in paths.items()}
        self.versions = {path: 1 for path, data in paths.items() if data is not None}
        self.interleave = dict(interleave or {})
        self.gets: dict[str, int] = {}
        self.requests: list[tuple[str, str, object]] = []

    def writes(self) -> list[tuple[str, str, object]]:
        return [request for request in self.requests if request[0] != "GET" and request[1].startswith("/v1/secret/")]


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

        def _path(self) -> str:
            return self.path.removeprefix("/v1/secret/data/")

        def do_POST(self):
            body = self._body()
            vault.requests.append(("POST", self.path, body))
            if self.path == "/v1/auth/approle/login":
                self._reply(200, {"auth": {"client_token": "fixture-token"}})
                return
            # KV-v2 check-and-set: cas 0 creates an absent path only; cas N replaces a present path only
            # while its version is N. Anything else is refused, as OpenBao refuses it.
            path = self._path()
            cas = body.get("options", {}).get("cas")
            present = vault.paths.get(path) is not None
            if (cas == 0 and not present) or (present and cas == vault.versions[path]):
                vault.paths[path] = dict(body["data"])
                vault.versions[path] = vault.versions.get(path, 0) + 1
                self._reply(200, {"data": {"version": vault.versions[path]}})
            else:
                self._reply(400, {"errors": ["check-and-set parameter did not match the current version"]})

        def do_GET(self):
            vault.requests.append(("GET", self.path, None))
            path = self._path()
            vault.gets[path] = vault.gets.get(path, 0) + 1
            if path in vault.interleave and vault.interleave[path][0] == vault.gets[path]:
                _, field, value = vault.interleave[path]
                vault.paths[path][field] = value
                vault.versions[path] += 1
            data = vault.paths.get(path)
            if data is None:
                self._reply(404, {"errors": []})
            else:
                self._reply(200, {"data": {"data": dict(data), "metadata": {"version": vault.versions[path]}}})

        def do_PATCH(self):
            body = self._body()
            vault.requests.append(("PATCH", self.path, body))
            path = self._path()
            data = vault.paths.get(path)
            if data is None:
                self._reply(404, {"errors": []})
                return
            for key, value in body["data"].items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            vault.versions[path] += 1
            self._reply(200, {"data": {"version": vault.versions[path]}})

    return Handler


@contextmanager
def _bao(paths: dict, interleave: dict | None = None):
    vault = _Vault(paths, interleave)
    with fake_http.serve(_handler(vault)) as url:
        yield vault, url


def _tree(tmp_path: Path, declaration: list[dict]) -> Path:
    root = tmp_path / "playbooks"
    (root / "vars").mkdir(parents=True)
    shutil.copy(PLAYBOOK, root / PLAYBOOK.name)
    shutil.copy(REFUSE, root / REFUSE.name)
    shutil.copytree(TASKS, root / "tasks")
    (root / "vars/copied-secret-fields.yml").write_text(yaml.safe_dump({"copied_secret_fields": declaration}))
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


def _run(root: Path, inventory: Path, *, check: bool):
    # The shared runner: bounded, sandboxed to the test's directory, no stdin, run from the repository root so
    # its ansible.cfg applies. root is tmp_path/playbooks, so its parent is the test's own directory.
    return harness_sandbox.run_playbook(
        root / PLAYBOOK.name,
        root.parent,
        *(["--check"] if check else []),
        inventory=inventory,
        env={"BAO_ROLE_ID": "fixture-role", "BAO_SECRET_ID": "fixture-secret"},
    )


def _out(done) -> str:
    return done.stdout + done.stderr


@requires_ansible
@pytest.mark.parametrize("check", [True, False], ids=["check", "live"])
def test_a_pending_target_is_written_beside_its_siblings_and_then_equals_its_source(tmp_path, check):
    paths = {SOURCE: {"token": HELD}, TARGET: {"keep": KEPT}}
    with _bao(paths) as (vault, url):
        done = _run(_tree(tmp_path, [_copy()]), _inventory(tmp_path, url), check=check)
    out = _out(done)
    assert done.returncode == 0, out[-2000:]
    assert f"{TARGET}/token from {SOURCE}/token: pending" in out, out[-2000:]
    assert HELD not in out and KEPT not in out, "a field value reached the output"
    if check:
        assert vault.writes() == [], "check mode wrote"
        assert vault.paths == paths
    else:
        # The update is a version-checked write (cas 1) of the target's whole data, sibling kept.
        assert vault.writes() == [
            ("POST", f"/v1/secret/data/{TARGET}", {"data": {"keep": KEPT, "token": HELD}, "options": {"cas": 1}})
        ]
        assert vault.paths == {SOURCE: {"token": HELD}, TARGET: {"keep": KEPT, "token": HELD}}
        assert f"Written: {TARGET}/token" in out, out[-2000:]


@requires_ansible
def test_an_absent_target_path_is_created_with_a_compare_and_swap_of_zero(tmp_path):
    paths = {SOURCE: {"token": HELD}, TARGET: None}
    with _bao(paths) as (vault, url):
        done = _run(_tree(tmp_path, [_copy()]), _inventory(tmp_path, url), check=False)
    out = _out(done)
    assert done.returncode == 0, out[-2000:]
    assert vault.writes() == [
        ("POST", f"/v1/secret/data/{TARGET}", {"data": {"token": HELD}, "options": {"cas": 0}})
    ]
    assert vault.paths[TARGET] == {"token": HELD}
    assert HELD not in out


@requires_ansible
def test_two_pending_fields_of_one_target_both_land_at_bumped_versions(tmp_path):
    # Two declared copies write different fields of one existing target. The second merge reads the version the
    # first one left, so its compare-and-swap is at the bumped version, and neither write drops the other field.
    declaration = [_copy(), _copy(from_field="secret", to_field="second_key")]
    paths = {SOURCE: {"token": HELD, "secret": SECOND}, TARGET: {"keep": KEPT}}
    with _bao(paths) as (vault, url):
        done = _run(_tree(tmp_path, declaration), _inventory(tmp_path, url), check=False)
    out = _out(done)
    assert done.returncode == 0, out[-2000:]
    assert vault.writes() == [
        ("POST", f"/v1/secret/data/{TARGET}", {"data": {"keep": KEPT, "token": HELD}, "options": {"cas": 1}}),
        (
            "POST",
            f"/v1/secret/data/{TARGET}",
            {"data": {"keep": KEPT, "token": HELD, "second_key": SECOND}, "options": {"cas": 2}},
        ),
    ]
    assert vault.paths[TARGET] == {"keep": KEPT, "token": HELD, "second_key": SECOND}
    assert vault.versions[TARGET] == 3, "one version per write, from the initial version 1"
    assert f"Written: {TARGET}/token, {TARGET}/second_key" in out, out[-2000:]
    assert HELD not in out and SECOND not in out and KEPT not in out


@requires_ansible
def test_a_field_written_between_the_plan_read_and_the_write_is_refused_and_not_written(tmp_path):
    # The plan reads the target once (its GET 1). Before the merge's own fresh read (GET 2), a concurrent writer sets
    # the target field. The shared merge's guard (_bm_guard_absent_keys) then refuses, so nothing is written over the
    # concurrent value. The refusal is the shared task's guard, reached after this playbook's plan said "pending".
    paths = {SOURCE: {"token": HELD}, TARGET: {"keep": KEPT}}
    interleave = {TARGET: (2, "token", OTHER)}
    with _bao(paths, interleave) as (vault, url):
        done = _run(_tree(tmp_path, [_copy()]), _inventory(tmp_path, url), check=False)
    out = _out(done)
    assert done.returncode != 0, out[-2000:]
    assert vault.writes() == [], "a write landed over a field that appeared after the plan"
    assert vault.paths[TARGET] == {"keep": KEPT, "token": OTHER}
    assert HELD not in out and OTHER not in out and KEPT not in out


@requires_ansible
def test_a_second_live_run_after_the_copy_writes_nothing(tmp_path):
    paths = {SOURCE: {"token": HELD}, TARGET: {"keep": KEPT}}
    with _bao(paths) as (vault, url):
        root = _tree(tmp_path, [_copy()])
        inventory = _inventory(tmp_path, url)
        first = _run(root, inventory, check=False)
        assert first.returncode == 0, _out(first)[-2000:]
        assert len(vault.writes()) == 1
        second = _run(root, inventory, check=False)
        assert second.returncode == 0, _out(second)[-2000:]
        assert len(vault.writes()) == 1, "the second run wrote"
        assert f"Already copied (no-op): {TARGET}/token" in _out(second)


@requires_ansible
@pytest.mark.parametrize("check", [True, False], ids=["check", "live"])
def test_a_target_that_already_holds_the_source_value_is_a_no_op(tmp_path, check):
    paths = {SOURCE: {"token": HELD}, TARGET: {"token": HELD, "keep": KEPT}}
    with _bao(paths) as (vault, url):
        done = _run(_tree(tmp_path, [_copy()]), _inventory(tmp_path, url), check=check)
    out = _out(done)
    assert done.returncode == 0, out[-2000:]
    assert f"Already copied (no-op): {TARGET}/token" in out, out[-2000:]
    assert vault.writes() == []
    assert vault.paths == paths
    assert HELD not in out


@requires_ansible
@pytest.mark.parametrize("check", [True, False], ids=["check", "live"])
@pytest.mark.parametrize("held", [OTHER, ""], ids=["different", "empty"])
def test_a_target_holding_another_value_refuses_before_any_write(tmp_path, check, held):
    paths = {SOURCE: {"token": HELD}, TARGET: {"token": held, "keep": KEPT}}
    with _bao(paths) as (vault, url):
        done = _run(_tree(tmp_path, [_copy()]), _inventory(tmp_path, url), check=check)
    out = _out(done)
    assert done.returncode != 0, out[-2000:]
    assert f"{TARGET}/token from {SOURCE}/token: conflict" in out, out[-2000:]
    assert "Nothing was written" in out, out[-2000:]
    assert HELD not in out and OTHER not in out and KEPT not in out
    assert vault.writes() == [], "a conflicting target was written"
    assert vault.paths == paths


@requires_ansible
@pytest.mark.parametrize("check", [True, False], ids=["check", "live"])
@pytest.mark.parametrize(
    "source",
    [None, {"other": HELD}, {"token": ""}],
    ids=["path-absent", "field-absent", "field-empty"],
)
def test_a_missing_source_refuses_before_any_write(tmp_path, check, source):
    paths = {SOURCE: source, TARGET: {"keep": KEPT}}
    with _bao(paths) as (vault, url):
        done = _run(_tree(tmp_path, [_copy()]), _inventory(tmp_path, url), check=check)
    out = _out(done)
    assert done.returncode != 0, out[-2000:]
    assert f"{TARGET}/token from {SOURCE}/token: source-missing" in out, out[-2000:]
    assert "Nothing was written" in out, out[-2000:]
    assert HELD not in out and KEPT not in out
    assert vault.writes() == [], "a copy with no source was written"
    assert vault.paths == paths


@requires_ansible
def test_a_refused_entry_stops_a_pending_entry_beside_it_from_being_written(tmp_path):
    declaration = [
        _copy(),
        _copy(from_path="services/src2", to_path="services/dst2"),
    ]
    paths = {
        SOURCE: {"token": HELD},
        TARGET: {"keep": KEPT},
        "services/src2": {"token": HELD},
        "services/dst2": {"token": OTHER},
    }
    with _bao(paths) as (vault, url):
        done = _run(_tree(tmp_path, declaration), _inventory(tmp_path, url), check=False)
    out = _out(done)
    assert done.returncode != 0, out[-2000:]
    assert f"{TARGET}/token from {SOURCE}/token: pending" in out, out[-2000:]
    assert "services/dst2/token from services/src2/token: conflict" in out, out[-2000:]
    assert vault.writes() == [], "a pending entry was written beside a refused one"
    assert vault.paths == paths


@requires_ansible
@pytest.mark.parametrize(
    ("declaration", "reason"),
    [
        ([_copy(to_path=SOURCE, to_field="token")], "onto itself"),
        ([_copy(), _copy(from_path="services/other", from_field="x")], "named by two entries"),
        ([_copy(from_path="services/Bad_Name")], "must list at least one copy"),
        ([_copy(to_field="Token")], "must list at least one copy"),
        ([], "must list at least one copy"),
        # The second entry's source is the first entry's target: a chained copy, refused before any read.
        (
            [_copy(), _copy(from_path=TARGET, from_field="token", to_path="services/dst3", to_field="token")],
            "chained copy",
        ),
    ],
    ids=["self-copy", "shared-target", "bad-path", "bad-field", "empty", "chained"],
)
def test_a_malformed_declaration_refuses_before_any_openbao_request(tmp_path, declaration, reason):
    with _bao({SOURCE: {"token": HELD}}) as (vault, url):
        done = _run(_tree(tmp_path, declaration), _inventory(tmp_path, url), check=False)
    out = _out(done)
    assert done.returncode != 0, out[-2000:]
    assert reason in out, out[-2000:]
    assert vault.requests == [], "the shape check ran after OpenBao was reached"
    assert HELD not in out
