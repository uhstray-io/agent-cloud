"""The retirement declaration, and the playbook that deletes what it declares.

retire-semaphore-templates.yml deletes live Semaphore templates and their schedules. These guards
read the files; they do not run the playbook:

- the declaration is well formed: exact names, a quoted date, one reason line, no name twice;
- every uri is classed for the check-mode contract (plan/architecture/08-ansible-automation-standards.md):
  a GET runs under --check, a DELETE is skipped under it, so a dry run never deletes;
- the declared-name refusal comes before any uri, and before the runtime token is resolved, and the
  destination is the fixed loopback API, not a survey-controlled URL.

test_retired_names_are_not_declared reads the real catalog: a retired name must not be declared,
so the change that retires a template removes its declaration in the same PR.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DECLARATION = REPO / "platform/semaphore/retired-templates.yml"
PLAYBOOK = REPO / "platform/playbooks/retire-semaphore-templates.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
LOCAL_TEMPLATES = REPO / "platform/semaphore/templates-local.yml"
DESTINATION = "http://127.0.0.1:3000"
URI = ("uri", "ansible.builtin.uri")


def _declaration() -> list[dict]:
    return yaml.safe_load(DECLARATION.read_text())["retired_templates"]


def _declared_names() -> set[str]:
    """Every name the catalog publishes: a declared template and its generated (Dev) twin."""
    names: set[str] = set()
    for path in (TEMPLATES, LOCAL_TEMPLATES):
        for template in yaml.safe_load(path.read_text())["templates"]:
            names.add(template["name"])
            if template.get("dev_variant"):
                names.add(template["name"] + " (Dev)")
    return names


def _uri_tasks():
    """(task, args) for every uri task in the playbook, in file order."""
    for task in playbook_yaml.tasks(playbook_yaml.load(PLAYBOOK)):
        for key in URI:
            if key in task:
                yield task, task[key]
                break


def _play() -> dict:
    plays = playbook_yaml.plays(PLAYBOOK)
    assert len(plays) == 1, "the playbook is one play after the extra-var guard import"
    return plays[0]


# ── (a) the declaration ──────────────────────────────────────────────────────


def test_declaration_has_the_three_fields_and_nothing_else():
    entries = _declaration()
    assert entries, "retired_templates must not be empty"
    for entry in entries:
        assert set(entry) == {"name", "retired", "reason"}, entry


def test_declaration_names_are_exact_nonblank_and_unique():
    names = [entry["name"] for entry in _declaration()]
    for name in names:
        assert isinstance(name, str) and name.strip() == name and name, repr(name)
    assert len(names) == len(set(names)), "a name is listed twice"


def test_declaration_dates_are_quoted_iso_dates():
    for entry in _declaration():
        # An unquoted 2026-11-07 parses as a date object, not the string the playbook compares.
        assert isinstance(entry["retired"], str), entry
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["retired"]), entry
        datetime.date.fromisoformat(entry["retired"])


def test_declaration_reasons_are_one_nonblank_line():
    for entry in _declaration():
        reason = entry["reason"]
        assert isinstance(reason, str) and reason.strip() and "\n" not in reason, entry


# ── (b) real files: a retired name is no longer declared ─────────────────────


def test_retired_names_are_not_declared_including_dev_variants():
    retired = {entry["name"] for entry in _declaration()}
    still_declared = sorted(retired & _declared_names())
    assert not still_declared, f"still declared in templates.yml or templates-local.yml: {still_declared}"


# ── (c) check-mode classes of every uri ──────────────────────────────────────


def test_every_uri_is_classed_for_check_mode():
    methods: list[str] = []
    for task, args in _uri_tasks():
        name = task.get("name", "<unnamed>")
        method = str(args.get("method", "GET")).upper()
        methods.append(method)
        if method == "GET":
            # A read-only probe: runs under --check, so it must say so, and must not claim a change.
            assert task.get("check_mode") is False, f"{name}: GET must set check_mode: false"
            assert task.get("changed_when") is False, f"{name}: GET must set changed_when: false"
        elif method == "DELETE":
            # A write uri cannot simulate: it is skipped under --check, and the guard is explicit.
            # changed_when: false would make the contract classify it as a read, so it is absent.
            assert task.get("check_mode") is not False, f"{name}: DELETE must not run for real under --check"
            assert task.get("when") == "not ansible_check_mode", (
                f"{name}: DELETE needs when: not ansible_check_mode"
            )
            assert "changed_when" not in task, f"{name}: DELETE must not set changed_when"
        else:
            pytest.fail(f"{name}: unexpected uri method {method}")
    assert methods.count("GET") >= 1 and methods.count("DELETE") >= 1, methods


# ── (d) refusal before authentication ────────────────────────────────────────


def test_declared_name_refusal_precedes_runtime_access_and_every_uri():
    tasks = _play()["tasks"]
    auth = [
        index for index, task in enumerate(tasks)
        if str(task.get("ansible.builtin.include_tasks", task.get("include_tasks", ""))).endswith("runtime-access.yml")
    ]
    assert len(auth) == 1, "exactly one runtime-access include"
    before = tasks[: auth[0]]
    refusal = [
        index for index, task in enumerate(before)
        if "ansible.builtin.assert" in task and "_still_declared" in str(task["ansible.builtin.assert"])
    ]
    assert refusal, "no declared-name refusal before runtime access"
    assert not [task for task in before if any(key in task for key in URI)], (
        "a uri runs before runtime access"
    )


# ── (e) the destination is fixed ─────────────────────────────────────────────


def test_destination_is_the_fixed_loopback_api_and_no_survey_sets_it():
    assert _play().get("vars", {}).get("_semaphore_url") == DESTINATION
    text = PLAYBOOK.read_text()
    assert not re.search(r"(?<![A-Za-z0-9_])semaphore_url", text), "a semaphore_url variable is read"
    for task, args in _uri_tasks():
        url = str(args.get("url", ""))
        assert url.startswith("{{ _semaphore_url }}/"), f"{task.get('name')}: {url}"


def test_destination_and_project_id_are_refused_before_runtime_access():
    """semaphore_project_id is an ordinary extra var interpolated into DELETE paths, so it must be
    a bare positive integer, checked with the fixed destination before the token is resolved."""
    tasks = _play()["tasks"]
    auth = next(
        index for index, task in enumerate(tasks)
        if str(task.get("ansible.builtin.include_tasks", task.get("include_tasks", ""))).endswith("runtime-access.yml")
    )
    guards = [
        task["ansible.builtin.assert"]["that"] for task in tasks[:auth]
        if "ansible.builtin.assert" in task and "_project_id" in str(task["ansible.builtin.assert"])
    ]
    assert len(guards) == 1, "one destination/project-id guard before runtime access"
    that = " ".join(guards[0])
    assert f"_semaphore_url == '{DESTINATION}'" in that
    assert "_project_id is match('^[1-9][0-9]*$')" in that


def test_deletes_accept_already_gone_and_the_read_back_still_proves_it():
    """A DELETE that finds the object gone (404) must not fail the run; the read-back is the proof."""
    deletes = [args for _, args in _uri_tasks() if str(args.get("method", "GET")).upper() == "DELETE"]
    assert len(deletes) == 2, "one schedule delete and one template delete"
    for args in deletes:
        assert sorted(args.get("status_code", [])) == [204, 404], args.get("url")
    tasks = _play()["tasks"]
    delete_at = [
        i for i, t in enumerate(tasks) for key in URI
        if key in t and str(t[key].get("method", "")).upper() == "DELETE"
    ]
    readback = [
        i for i, t in enumerate(tasks)
        if "ansible.builtin.assert" in t
        and "_templates_after" in str(t["ansible.builtin.assert"]["that"])
        and "_schedules_after" in str(t["ansible.builtin.assert"]["that"])
    ]
    assert len(readback) == 1, "one read-back assert over the post-delete template and schedule lists"
    assert readback[0] > max(delete_at), "the read-back assert must run after every DELETE"
    reads = [
        i for i, t in enumerate(tasks)
        if any(key in t for key in URI) and t.get("register") in ("_templates_after", "_schedules_after")
    ]
    assert len(reads) == 2, "two post-delete GETs"
    assert max(delete_at) < min(reads) < readback[0], "post-delete GETs sit between the deletes and the assert"
    for i in reads:
        assert "when" not in tasks[i] or tasks[i]["when"] == "not ansible_check_mode", tasks[i].get("name")


def test_template_delete_allows_for_a_long_task_history():
    """Deleting a template removes its task history; the uri default (30 s) timed out on the
    tududi refresher's (Semaphore tasks 14206, 14211)."""
    deletes = [
        args for _, args in _uri_tasks()
        if str(args.get("method", "")).upper() == "DELETE" and "/templates/" in str(args.get("url", ""))
    ]
    assert len(deletes) == 1
    assert int(deletes[0].get("timeout", 30)) >= 300, "template DELETE needs a long timeout"


# ── (f) the task-history drain before the template delete ────────────────────

HISTORY_PAGE = "/tasks/last?limit=200"


def _has_module(task: dict, *names: str) -> bool:
    return any(name in task for name in names)


def _is_request(task: dict, method: str, fragment: str) -> bool:
    for key in URI:
        if key in task:
            args = task[key]
            return str(args.get("method", "GET")).upper() == method and fragment in str(args.get("url", ""))
    return False


def _preview_index(tasks: list) -> int:
    found = [i for i, task in enumerate(tasks) if _is_request(task, "GET", HISTORY_PAGE)]
    assert len(found) == 1, "one newest-page read of each retired template's history"
    return found[0]


def _drain_block(tasks: list) -> dict:
    blocks = [
        task
        for task in tasks
        if "block" in task and any("semaphore_drain_template.py" in str(child) for child in task["block"])
    ]
    assert len(blocks) == 1, "one drain block"
    return blocks[0]


def test_history_preview_is_a_read_that_runs_in_check_mode():
    tasks = _play()["tasks"]
    task = tasks[_preview_index(tasks)]
    assert task.get("check_mode") is False, "the preview must run under --check"
    assert task.get("changed_when") is False, "the preview changes nothing"
    assert task.get("loop") == "{{ _present_template_ids }}", "loop over the clean id list, not a register"
    assert task.get("register") == "_drain_pages"


def test_preview_and_running_refusal_precede_every_delete_in_both_modes():
    tasks = _play()["tasks"]
    preview = _preview_index(tasks)
    refusal = [
        i
        for i, task in enumerate(tasks)
        if "ansible.builtin.assert" in task and "running_ids" in str(task["ansible.builtin.assert"])
    ]
    assert len(refusal) == 1, "one running-task refusal"
    assert "when" not in tasks[refusal[0]], "the refusal must apply in check mode too"
    deletes = [
        i
        for i, task in enumerate(tasks)
        if any(key in task and str(task[key].get("method", "")).upper() == "DELETE" for key in URI)
    ]
    drain = tasks.index(_drain_block(tasks))
    assert preview < refusal[0] < min(deletes), "the refusal must precede every DELETE"
    assert refusal[0] < drain, "the refusal must precede the history drain"


def test_drain_block_is_live_only_and_runs_after_schedules_and_before_the_template_delete():
    tasks = _play()["tasks"]
    block = _drain_block(tasks)
    whens = block["when"] if isinstance(block["when"], list) else [block["when"]]
    assert "not ansible_check_mode" in whens, "the drain block must be skipped under --check"
    schedule_deletes = [i for i, task in enumerate(tasks) if _is_request(task, "DELETE", "/schedules/")]
    template_deletes = [i for i, task in enumerate(tasks) if _is_request(task, "DELETE", "/templates/")]
    drain = tasks.index(block)
    assert len(template_deletes) == 1
    assert max(schedule_deletes) < drain < template_deletes[0], (
        "schedules first (no new run starts during the drain), then the drain, then the template delete"
    )


def test_token_copy_is_hidden_and_the_drain_command_is_visible_and_carries_no_token():
    children = _drain_block(_play()["tasks"])["block"]
    copies = [child for child in children if _has_module(child, "copy", "ansible.builtin.copy")]
    assert len(copies) == 1, "one token copy"
    assert copies[0].get("no_log") is True, "the token copy is a credential task"
    assert "_token" in str(copies[0].get("ansible.builtin.copy", copies[0].get("copy"))), "the copy carries the token"
    commands = [child for child in children if _has_module(child, "command", "ansible.builtin.command")]
    assert len(commands) == 1, "one drain command"
    command = commands[0]
    assert command.get("no_log") is not True, "the drain command is visible: its counts are the report"
    assert command.get("check_mode") is not False, "the drain runs only live, so it is never forced"
    argv = str(command["ansible.builtin.command"]["argv"])
    assert "--token-file=" in argv, "the token travels in a file"
    assert "_token" not in argv, "the token never reaches the command line"


def test_scratch_directory_is_created_and_removed_in_always():
    block = _drain_block(_play()["tasks"])
    created = [child for child in block["block"] if _has_module(child, "tempfile", "ansible.builtin.tempfile")]
    assert len(created) == 1 and created[0].get("register") == "_drain_tmp"
    assert created[0].get("ansible.builtin.tempfile", created[0].get("tempfile", {})).get("state") == "directory"
    removed = [
        child
        for child in block.get("always", [])
        if _has_module(child, "file", "ansible.builtin.file")
        and child.get("ansible.builtin.file", child.get("file", {})).get("state") == "absent"
    ]
    assert len(removed) == 1, "the scratch directory is removed in always, so a failed drain leaves no token behind"
    assert removed[0]["ansible.builtin.file"]["path"] == "{{ _drain_tmp.path }}"


def test_preview_counts_every_unfinished_status_as_running():
    """An allowlist of finished statuses (upstream TaskStatus.IsFinished), not a denylist of busy ones:
    a status the playbook has never seen must refuse, not pass."""
    text = PLAYBOOK.read_text()
    assert "rejectattr('status', 'in', ['success', 'error', 'stopped'])" in text
    assert "selectattr('status', 'in'" not in text


def _user_index(tasks: list) -> int:
    found = [i for i, task in enumerate(tasks) if _is_request(task, "GET", "/api/user/")]
    assert len(found) == 1, "one logged-in-user read"
    return found[0]


def test_admin_gate_precedes_every_delete_in_both_modes():
    tasks = _play()["tasks"]
    user = _user_index(tasks)
    assert tasks[user].get("check_mode") is False, "the admin read must run under --check"
    assert tasks[user].get("changed_when") is False, "the admin read changes nothing"
    assert tasks[user].get("register") == "_user"
    deletes = [
        i
        for i, task in enumerate(tasks)
        if any(key in task and str(task[key].get("method", "")).upper() == "DELETE" for key in URI)
    ]
    drain = tasks.index(_drain_block(tasks))
    assert user < min(deletes), "the admin gate must precede every schedule and template delete"
    assert user < drain, "the admin gate must precede the task-history drain"


def test_admin_gate_applies_only_when_task_history_must_go_and_names_admin():
    tasks = _play()["tasks"]
    gates = [
        task
        for task in tasks
        if "ansible.builtin.assert" in task and "_user.json.admin" in str(task["ansible.builtin.assert"])
    ]
    assert len(gates) == 1, "one admin assert"
    checks = " ".join(str(item) for item in gates[0]["ansible.builtin.assert"]["that"])
    assert "first_page" in checks and "'gt', 0" in checks, "the gate applies only to templates with history"
    assert "admin" in str(gates[0]["ansible.builtin.assert"]["fail_msg"]), "the refusal names the admin need"
    assert tasks.index(gates[0]) > _user_index(tasks), "the assert reads the admin GET it depends on"


def test_drain_changed_when_requires_a_zero_return_code_before_parsing_stdout():
    block = _drain_block(_play()["tasks"])
    commands = [child for child in block["block"] if _has_module(child, "command", "ansible.builtin.command")]
    assert len(commands) == 1, "one drain command"
    assert str(commands[0]["changed_when"]).startswith("_drain_run.rc == 0 and "), (
        "a failed run must short-circuit before from_json reads its stdout"
    )
