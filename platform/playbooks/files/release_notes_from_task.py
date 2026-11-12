#!/usr/bin/env python3
"""Return the release-notes body one check-mode refresh task recorded, and nothing else.

A Refresh Release Notes run prints the notes it would publish and records them with set_stats,
which the repository ansible.cfg prints as one line `RUN: {...json...}` after `CUSTOM STATS:`.
The body is model prose where the gateway answers, so two runs of the same refresh can differ:
on 2026-11-10 the check-mode refresh of v0.14.0 (Semaphore task 14801) and the real refresh that
followed (task 14806) wrote different summaries. The publish step must therefore take the body that one reviewed
check-mode task recorded, not a fresh generation. This helper reads that task's own history
and refuses anything that is not exactly that task.

Input: one JSON object on stdin, from the playbook that owns the publish step:
  {"task": {...}, "output_rows": [...], "template": {...},
   "expected_tag": "vX.Y.Z", "expected_task_id": <int>}

Accepted only when every rule holds:
  - the task is the expected task: its id equals expected_task_id and its status is "success";
  - it ran in check mode: params is an object whose dry_run is exactly true;
  - the template is the task's own template (same id) and is a refresh template by name;
  - the output, with ANSI colour stripped and split on "\n" only, carries exactly one RUN line;
  - that line's JSON is an object with a release_notes object, and no key repeats in it;
  - release_notes.tag equals expected_tag;
  - release_notes.body is a non-empty string;
  - release_notes.sha256 is the SHA-256 of the body's UTF-8 bytes.

On accept: one JSON object on stdout {"task_id", "tag", "sha256", "bytes", "body"}, exit 0.
On refuse: nothing on stdout; one line on stderr `release-notes-from-task: <reason>` where the
reason is a fixed token naming the rule (never the body or any row text); exit 2.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any

TEMPLATE_NAMES = frozenset({"Refresh Release Notes (Dev)", "Refresh Release Notes"})
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RUN = re.compile(r"^\s*RUN:\s*(\{.*\})\s*$")


class Refusal(ValueError):
    """A fixed reason token. It never carries request or response text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def _is_int(value: object) -> bool:
    return type(value) is int


def _same_int(left: object, right: object) -> bool:
    return _is_int(left) and _is_int(right) and left == right


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "run_json_duplicate_key")
        result[key] = value
    return result


def _check_task(task: object, template: object, expected_task_id: object) -> None:
    _require(_is_int(expected_task_id), "invalid_input")
    _require(isinstance(task, dict), "task_invalid")
    _require(_same_int(task.get("id"), expected_task_id), "task_id_mismatch")
    _require(task.get("status") == "success", "task_not_success")
    params = task.get("params")
    _require(isinstance(params, dict) and params.get("dry_run") is True, "not_check_mode")
    _require(isinstance(template, dict), "template_invalid")
    _require(_same_int(template.get("id"), task.get("template_id")), "template_id_mismatch")
    _require(template.get("name") in TEMPLATE_NAMES, "template_name_mismatch")


def _output_lines(rows: object) -> list[str]:
    _require(isinstance(rows, list), "invalid_input")
    lines: list[str] = []
    for row in rows:
        _require(isinstance(row, dict), "output_row_invalid")
        text = row.get("output")
        _require(isinstance(text, str), "output_row_invalid")
        # ponytail: split on "\n" only. str.splitlines() also splits on U+2028, U+0085 and a bare "\r",
        # which the stats dump prints raw inside the body's JSON string, so a body holding one would
        # break its own RUN line and be refused as no_run_line.
        lines.extend(line.removesuffix("\r") for line in ANSI.sub("", text).split("\n"))
    return lines


def _run_payload(rows: object) -> str:
    payloads = []
    for line in _output_lines(rows):
        match = RUN.match(line)
        if match:
            payloads.append(match.group(1))
    _require(len(payloads) != 0, "no_run_line")
    _require(len(payloads) == 1, "multiple_run_lines")
    return payloads[0]


def _release_notes(payload: str) -> dict[str, Any]:
    try:
        run = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError:
        raise Refusal("run_json_invalid") from None
    _require(isinstance(run, dict), "run_json_invalid")
    notes = run.get("release_notes")
    _require(isinstance(notes, dict), "no_release_notes")
    return notes


def _check_notes(notes: dict[str, Any], expected_tag: object) -> tuple[str, bytes]:
    _require(isinstance(expected_tag, str), "invalid_input")
    _require(notes.get("tag") == expected_tag, "tag_mismatch")
    body = notes.get("body")
    _require(isinstance(body, str), "body_not_text")
    _require(body.strip() != "", "body_empty")
    try:
        encoded = body.encode("utf-8")
    except UnicodeEncodeError:
        raise Refusal("body_not_text") from None
    digest = notes.get("sha256")
    _require(isinstance(digest, str) and digest == hashlib.sha256(encoded).hexdigest(), "sha256_mismatch")
    return body, encoded


def select_body(request: dict[str, Any]) -> dict[str, object]:
    """Return the recorded body for the expected task, or raise Refusal naming the rule that failed."""
    task = request.get("task")
    _check_task(task, request.get("template"), request.get("expected_task_id"))
    notes = _release_notes(_run_payload(request.get("output_rows")))
    body, encoded = _check_notes(notes, request.get("expected_tag"))
    return {
        "task_id": task["id"],
        "tag": notes["tag"],
        "sha256": notes["sha256"],
        "bytes": len(encoded),
        "body": body,
    }


def main() -> int:
    """Stdin in, one JSON object out on accept. A refusal prints only its reason token on stderr."""
    try:
        request = json.load(sys.stdin)
        _require(isinstance(request, dict), "invalid_input")
        result = select_body(request)
    except Refusal as exc:
        print(f"release-notes-from-task: {exc.reason}", file=sys.stderr)
        return 2
    except (AttributeError, KeyError, TypeError, ValueError):
        print("release-notes-from-task: invalid_input", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
