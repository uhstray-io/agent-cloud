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
