"""The retirement declaration, and the playbook that deletes what it declares.

retire-semaphore-templates.yml deletes live Semaphore templates and their schedules. These guards
read the files; they do not run the playbook:

- the declaration is well formed: exact names, a quoted date, one reason line, no name twice;
- every uri is classed for the check-mode contract (plan/architecture/08-ansible-automation-standards.md):
  a GET runs under --check, a DELETE is skipped under it, so a dry run never deletes;
- the declared-name refusal comes before any uri, and before the runtime token is resolved, and the
  destination is the fixed loopback API, not a survey-controlled URL.

test_retired_names_are_not_declared is xfail(strict=True): until the declarations of the retired
templates are removed from templates.yml and templates-local.yml (chore/retire-tududi-sync), it
fails as expected. Once that merges it passes, strict turns that into a failure, and the xfail
marker must be removed.
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


@pytest.mark.xfail(strict=True, reason="sync declarations removed on chore/retire-tududi-sync")
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
