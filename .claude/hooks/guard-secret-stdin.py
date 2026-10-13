#!/usr/bin/env python3
"""Block interpreter programs read from stdin while stdin is redirected from secrets/."""

import json
import re
import sys

SECRET_INPUT = re.compile(r"(?<!<)<\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s;&|]+))")
STDIN_PROGRAM = re.compile(
    r"(?<![\w.-])(?:python(?:\d+(?:\.\d+)?)?|pypy\d*)\s+-\s*(?=$|[<\s])"
    r"|\b(?:python(?:\d+(?:\.\d+)?)?|pypy\d*)\s*<<-?\s*[\w'-]+"
    r"|\bbash(?:\s+-[A-Za-z]+)*\s+-s(?:\s|$|<)"
    r"|\bbash(?:\s+-[A-Za-z]+)*\s*<<-?\s*[\w'-]+"
)


def secret_path(value):
    normalized = value.strip("\"'").replace("\\", "/")
    return normalized.startswith("secrets/") or "/secrets/" in normalized


def unsafe_command(command):
    if not isinstance(command, str) or not STDIN_PROGRAM.search(command):
        return False
    return any(secret_path(next(part for part in match.groups() if part is not None))
               for match in SECRET_INPUT.finditer(command))


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        print("Blocked: command input could not be inspected.", file=sys.stderr)
        return 2
    tool_input = event.get("tool_input", {}) if isinstance(event, dict) else {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if unsafe_command(command):
        print("Blocked: keep the interpreter program in a file or -c, and pass the secret on stdin.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
