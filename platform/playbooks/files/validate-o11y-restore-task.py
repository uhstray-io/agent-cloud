#!/usr/bin/env python3
"""Validate one authenticated Semaphore task and its bounded receipt output.

The caller must fetch task, template, repository, and output records over the
protected Semaphore API route. This validator never makes network requests and
does not accept template bindings from untrusted survey or extra-var fields.
"""

from __future__ import annotations

import json
import sys

from o11y_isolated_restore import Refusal, verify_restore_task_proof, verify_task_proof

MAX_INPUT_BYTES = 2 * 1024 * 1024


def validate(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise Refusal("invalid_input")
    if set(payload) == {"proof"}:
        return verify_task_proof(payload["proof"])
    if set(payload) == {"restore_proof"}:
        return verify_restore_task_proof(payload["restore_proof"])
    raise Refusal("invalid_input")


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise Refusal("invalid_input")
        result = validate(json.loads(raw))
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": exc.reason}, separators=(",", ":")))
        return 2
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        print('{"status":"refused","reason":"invalid_input"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
