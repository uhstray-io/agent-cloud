#!/usr/bin/env python3
"""Block interpreter programs read from stdin while stdin is redirected from secrets/."""

import json
import re
import sys

SECRET_INPUT = re.compile(r"(?<!<)<\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s;&|]+))")
PYTHON = r"(?:python(?:\d+(?:\.\d+)?)?|pypy\d*)"
PYTHON_OPTIONS = r"(?:\s+-(?:[bdiEIPqSsvx]|O{1,2}|u|[WX](?:\s+[^\s<>|;&]+|[^\s<>|;&]+)))*"
STDIN_PROGRAM = re.compile(
    rf"(?<![\w.-]){PYTHON}{PYTHON_OPTIONS}\s+-\s*(?=$|[<\s])"
    rf"|\b{PYTHON}{PYTHON_OPTIONS}\s*<<-?\s*[\w'-]+"
    r"|\bbash(?:\s+-[A-Za-z]+)*\s+-s(?:\s|$|<)"
    r"|\bbash(?:\s+-[A-Za-z]+)*\s*<<-?\s*[\w'-]+"
)


def secret_path(value):
    normalized = value.strip("\"'").replace("\\", "/")
    return normalized.startswith("secrets/") or "/secrets/" in normalized


def unsafe_command(command):
    if not isinstance(command, str):
        return False
    return any(STDIN_PROGRAM.search(segment) and any(
        secret_path(next(part for part in match.groups() if part is not None))
        for match in SECRET_INPUT.finditer(segment)) for segment in command_segments(command))


def command_segments(command):
    """Split shell commands without scanning heredoc bodies as executable commands."""
    segments = []
    current = []
    quote = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            current.append(char)
            escaped = True
        elif quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in "'\"":
            current.append(char)
            quote = char
        elif char in ";|&\n":
            if current:
                segments.append("".join(current))
            header = "".join(current)
            current = []
            if char == "\n":
                delimiters = re.findall(r"<<-?\s*(['\"]?)([\w.-]+)\1", header)
                for _quote, delimiter in delimiters:
                    while index + 1 < len(command):
                        index += 1
                        line_end = command.find("\n", index)
                        if line_end < 0:
                            index = len(command)
                            break
                        line = command[index:line_end].lstrip("\t")
                        if line == delimiter:
                            index = line_end
                            break
                        index = line_end
        else:
            current.append(char)
        index += 1
    if current:
        segments.append("".join(current))
    return segments


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
