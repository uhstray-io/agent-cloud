"""Tests for platform/playbooks/files/release_notes_from_task.py.

The helper runs as a subprocess (python3 -I, one JSON object on stdin), as the playbook calls it.
Each case checks the exit status, stdout, and the one-line stderr contract, and that no body
or row text reaches stderr.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import harness_sandbox
import pytest

HELPER = Path(__file__).resolve().parents[1] / "playbooks/files/release_notes_from_task.py"
PREFIX = "release-notes-from-task: "
TASK_ID = 14801
TEMPLATE_ID = 7
TAG = "v1.4.0"
# Non-ASCII on purpose: the byte count must be the UTF-8 length, not the character count.
BODY = "## Summary\n\nÜber release with model prose.\n\n- item one\n- item two\n"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def notes(body: Any = BODY, tag: str = TAG, sha: str | None = None) -> dict[str, Any]:
    digest = sha256_hex(body) if sha is None else sha
    return {"tag": tag, "sha256": digest, "body": body}


def run_line(payload: object) -> str:
    """One RUN line wrapped in ANSI colour, as Semaphore keeps the repository's stats line."""
    return "\x1b[1;32mRUN: " + json.dumps(payload) + "\x1b[0m"


def rows(*lines: str) -> list[dict[str, Any]]:
    return [
        {"id": index, "task_id": TASK_ID, "time": "2026-11-10T10:00:00Z", "output": line}
        for index, line in enumerate(lines)
    ]


def valid_rows() -> list[dict[str, Any]]:
    return rows(
        "\x1b[0;33mPLAY RECAP\x1b[0m",
        "\x1b[1mCUSTOM STATS:\x1b[0m",
        run_line({"release_notes": notes()}),
    )


def valid_request() -> dict[str, Any]:
    return {
        "task": {
            "id": TASK_ID,
            "template_id": TEMPLATE_ID,
            "status": "success",
            "params": {"dry_run": True, "diff": True},
        },
        "template": {"id": TEMPLATE_ID, "name": "Refresh Release Notes (Dev)"},
        "output_rows": valid_rows(),
        "expected_tag": TAG,
        "expected_task_id": TASK_ID,
    }


def send(text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(HELPER)],
        input=text,
        capture_output=True,
        text=True,
        check=False,
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
    )


def run(request: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return send(json.dumps(request))


def assert_refused(result: subprocess.CompletedProcess[str], reason: str) -> None:
    assert result.returncode == 2, result.stdout
    assert result.stdout == ""
    assert result.stderr == PREFIX + reason + "\n"
    for fragment in (BODY, "item one", "RUN:"):
        assert fragment not in result.stderr


def test_accepts_the_recorded_body_exactly() -> None:
    result = run(valid_request())

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "task_id": TASK_ID,
        "tag": TAG,
        "sha256": sha256_hex(BODY),
        "bytes": len(BODY.encode("utf-8")),
        "body": BODY,
    }


def callback_run_line(body: str) -> str:
    """The RUN line exactly as the repository's stdout callback prints set_stats (captured from
    ansible-playbook with the repo ansible.cfg): a tab, spaced JSON, sorted keys, and U+2028 or
    U+0085 inside the body printed raw rather than escaped."""
    return (
        '\tRUN: { "release_notes": {  "body": '
        + json.dumps(body, ensure_ascii=False)
        + ',  "sha256": "'
        + sha256_hex(body)
        + '",  "tag": "'
        + TAG
        + '" }}'
    )


def test_accepts_the_callback_format_with_raw_line_separators_in_the_body() -> None:
    body = "## Summary\n\nline\u2028two\u0085three\n"
    request = valid_request()
    request["output_rows"] = rows("CUSTOM STATS: ****\r\n\r\n" + callback_run_line(body) + "\r\n")

    result = run(request)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["body"] == body


def test_accepts_a_run_line_inside_a_multi_line_row() -> None:
    request = valid_request()
    request["output_rows"] = rows("warning: first line\n" + run_line({"release_notes": notes()}) + "\nlast line")

    result = run(request)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["body"] == BODY


def _two_run_lines_in_one_row(request: dict[str, Any]) -> None:
    line = run_line({"release_notes": notes()})
    request["output_rows"] = rows(line + "\n" + line)


def _run_json(payload_text: str) -> list[dict[str, Any]]:
    return rows("RUN: " + payload_text)


def _duplicate_release_notes_key() -> str:
    first = json.dumps(notes())
    second = json.dumps(notes(body="a different body"))
    return '{"release_notes": ' + first + ', "release_notes": ' + second + "}"


def _set_notes(**fields: Any) -> dict[str, Any]:
    return {"release_notes": {"tag": TAG, "sha256": sha256_hex(BODY), "body": BODY, **fields}}


def _rows_with_notes(**fields: Any) -> list[dict[str, Any]]:
    return rows(run_line(_set_notes(**fields)))


def _rows_with_notes_without(*keys: str) -> list[dict[str, Any]]:
    payload = _set_notes()
    for key in keys:
        payload["release_notes"].pop(key)
    return rows(run_line(payload))


REFUSALS = [
    pytest.param(lambda r: r["task"].update(id=TASK_ID + 1), "task_id_mismatch", id="wrong_task_id"),
    pytest.param(lambda r: r["task"].update(id=str(TASK_ID)), "task_id_mismatch", id="task_id_as_string"),
    pytest.param(lambda r: r["task"].update(status="error"), "task_not_success", id="task_status_error"),
    pytest.param(lambda r: r["task"]["params"].pop("dry_run"), "not_check_mode", id="dry_run_missing"),
    pytest.param(lambda r: r["task"]["params"].update(dry_run=False), "not_check_mode", id="dry_run_false"),
    pytest.param(lambda r: r["task"]["params"].update(dry_run="true"), "not_check_mode", id="dry_run_truthy_string"),
    pytest.param(lambda r: r["task"].update(params=None), "not_check_mode", id="params_not_object"),
    pytest.param(
        lambda r: r["template"].update(name="Deploy Something"), "template_name_mismatch", id="template_name_wrong"
    ),
    pytest.param(lambda r: r["template"].update(id=TEMPLATE_ID + 1), "template_id_mismatch", id="template_id_mismatch"),
    pytest.param(
        lambda r: r.update(output_rows=rows("CUSTOM STATS:", "PLAY RECAP")), "no_run_line", id="zero_run_lines"
    ),
    pytest.param(
        lambda r: r.update(
            output_rows=rows(run_line({"release_notes": notes()}), run_line({"release_notes": notes()}))
        ),
        "multiple_run_lines",
        id="two_run_lines",
    ),
    pytest.param(_two_run_lines_in_one_row, "multiple_run_lines", id="two_run_lines_in_one_row"),
    pytest.param(
        lambda r: r.update(output_rows=rows(run_line({"other": 1}))), "no_release_notes", id="run_without_release_notes"
    ),
    pytest.param(lambda r: r.update(output_rows=_run_json("{not json}")), "run_json_invalid", id="run_json_invalid"),
    pytest.param(
        lambda r: r.update(output_rows=_run_json(_duplicate_release_notes_key())),
        "run_json_duplicate_key",
        id="run_json_duplicate_key",
    ),
    pytest.param(lambda r: r.update(expected_tag="v1.4.1"), "tag_mismatch", id="wrong_tag"),
    pytest.param(
        lambda r: r.update(output_rows=_rows_with_notes(body="", sha256=sha256_hex(""))), "body_empty", id="empty_body"
    ),
    pytest.param(
        lambda r: r.update(output_rows=_rows_with_notes(body="  \n\t", sha256=sha256_hex("  \n\t"))),
        "body_empty",
        id="whitespace_body",
    ),
    pytest.param(
        lambda r: r.update(output_rows=_rows_with_notes(sha256="0" * 64)), "sha256_mismatch", id="sha_mismatch"
    ),
    pytest.param(
        lambda r: r.update(output_rows=_rows_with_notes_without("sha256")), "sha256_mismatch", id="sha_missing"
    ),
    pytest.param(
        lambda r: r.update(output_rows=_rows_with_notes(body=123, sha256="0" * 64)), "body_not_text", id="body_not_text"
    ),
    pytest.param(
        lambda r: r.update(output_rows=_rows_with_notes(body="\ud800", sha256="0" * 64)),
        "body_not_text",
        id="body_lone_surrogate",
    ),
    pytest.param(lambda r: r.update(output_rows=["RUN: {}"]), "output_row_invalid", id="non_dict_row"),
    pytest.param(lambda r: r.update(output_rows=[{"output": 5}]), "output_row_invalid", id="row_output_not_text"),
    pytest.param(lambda r: r.update(output_rows="RUN"), "invalid_input", id="output_rows_not_list"),
    pytest.param(lambda r: r.update(expected_task_id=str(TASK_ID)), "invalid_input", id="expected_task_id_not_int"),
    pytest.param(lambda r: r.pop("expected_tag"), "invalid_input", id="expected_tag_missing"),
    pytest.param(lambda r: r.update(task=None), "task_invalid", id="task_not_object"),
    pytest.param(lambda r: r.update(template=None), "template_invalid", id="template_not_object"),
]


@pytest.mark.parametrize(("mutate", "reason"), REFUSALS)
def test_refuses_with_one_named_reason(mutate: Any, reason: str) -> None:
    request = valid_request()
    mutate(request)

    assert_refused(run(request), reason)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        pytest.param("{not json", "invalid_input", id="malformed_json"),
        pytest.param("[]", "invalid_input", id="not_an_object"),
        pytest.param("", "invalid_input", id="empty_stdin"),
    ],
)
def test_refuses_unreadable_stdin(raw: str, reason: str) -> None:
    assert_refused(send(raw), reason)
