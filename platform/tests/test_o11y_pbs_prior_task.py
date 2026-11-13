from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

TASK = Path(__file__).resolve().parents[1] / "playbooks" / "tasks" / "read-o11y-pbs-prior-task.yml"
TEXT = TASK.read_text()
TASKS = yaml.safe_load(TEXT)
CONTRACTS = ["o11y-pbs-backup/v1", "o11y-pbs-verify/v1", "o11y-pbs-copy-sync/v1", "o11y-pbs-copy-ready/v1"]
TRIM = "{{ (_pbs_task_semaphore_secret.url | default('', true) | string | trim) | regex_replace('/+$', '') }}"


def _find(pred, what):
    found = [t for t in TASKS if pred(t)]
    assert len(found) == 1, (what, [t.get("name") for t in found])
    return found[0]


def _set_fact(name):
    return _find(lambda t: name in t.get("ansible.builtin.set_fact", {}), f"set_fact {name}")


def _assert_that(items):
    return _find(lambda t: t.get("ansible.builtin.assert", {}).get("that") == items, f"assert {items}")


def test_the_stored_url_is_trimmed_once_and_hidden_before_any_use():
    trim = _set_fact("_pbs_task_semaphore_url")
    assert trim["no_log"] is True
    assert trim["ansible.builtin.set_fact"]["_pbs_task_semaphore_url"] == TRIM
    # The raw stored url is read exactly once, inside the trim; every later use is the trimmed fact.
    assert TEXT.count("_pbs_task_semaphore_secret.url") == 1


def test_the_url_check_is_hidden_https_only_and_runs_after_the_trim():
    url_check = _assert_that(["_pbs_task_semaphore_url is match('^https://[^/]+$')"])
    assert url_check["no_log"] is True
    assert url_check["ansible.builtin.assert"]["fail_msg"] == (
        "secret/services/semaphore must hold an https url with no path before the PBS task can be read."
    )
    assert url_check["ansible.builtin.assert"]["quiet"] is True
    trim = _set_fact("_pbs_task_semaphore_url")
    assert TASKS.index(trim) < TASKS.index(url_check)


def test_the_api_token_check_is_hidden_and_names_only_the_requirement():
    token_check = _assert_that(["(_pbs_task_semaphore_secret.api_token | default('', true)) | length > 0"])
    assert token_check["no_log"] is True
    assert token_check["ansible.builtin.assert"]["fail_msg"] == (
        "secret/services/semaphore must hold an api_token before the PBS task can be read."
    )
    assert token_check["ansible.builtin.assert"]["quiet"] is True


def test_the_task_input_check_is_visible_and_covers_id_and_contract():
    inputs = _assert_that(
        [
            "_pbs_task_id is integer",
            "_pbs_task_id > 0",
            f"_pbs_task_contract in {CONTRACTS}",
        ]
    )
    assert inputs.get("no_log") is not True
    assert inputs["ansible.builtin.assert"]["fail_msg"] == (
        "PBS task authentication requires its fixed task ID and a known receipt contract."
    )
    assert inputs["ansible.builtin.assert"]["quiet"] is True


def test_the_three_checks_run_before_the_first_request_to_semaphore():
    uris = [t for t in TASKS if "ansible.builtin.uri" in t]
    assert len(uris) == 2
    first_request = min(TASKS.index(t) for t in uris)
    checks = [
        _set_fact("_pbs_task_semaphore_url"),
        _assert_that(["_pbs_task_semaphore_url is match('^https://[^/]+$')"]),
        _assert_that(["(_pbs_task_semaphore_secret.api_token | default('', true)) | length > 0"]),
        _assert_that(
            [
                "_pbs_task_id is integer",
                "_pbs_task_id > 0",
                f"_pbs_task_contract in {CONTRACTS}",
            ]
        ),
    ]
    positions = [TASKS.index(t) for t in checks]
    assert positions == sorted(positions)
    assert positions[-1] < first_request


def test_both_requests_go_to_the_trimmed_url():
    uris = [t["ansible.builtin.uri"]["url"] for t in TASKS if "ansible.builtin.uri" in t]
    assert len(uris) == 2
    for url in uris:
        assert url.startswith("{{ _pbs_task_semaphore_url }}/api/project/"), url


@pytest.mark.parametrize(
    ("stored", "verdict"),
    [
        ("https://s.example.test", "ok"),
        ("https://s.example.test/", "ok"),
        ("https://s.example.test//", "ok"),
        (" https://s.example.test:3000/ ", "ok"),
        ("http://s.example.test", "refused"),
        ("https://s.example.test/api", "refused"),
    ],
)
def test_the_trim_and_the_https_pattern_accept_and_refuse_the_stored_forms(stored, verdict):
    from ansible.plugins.filter.core import regex_replace

    assert _set_fact("_pbs_task_semaphore_url")["ansible.builtin.set_fact"]["_pbs_task_semaphore_url"] == TRIM
    trimmed = regex_replace(stored.strip(), "/+$", "")
    assert bool(re.match(r"^https://[^/]+$", trimmed)) == (verdict == "ok")
