"""Harness-generic install machinery for the secret-free MCP client scripts.

scripts/agw-mcp-client.py and scripts/plane-mcp-client.py render their own entries and hand them
here. Claude Code is written through its own CLI (`claude mcp add-json -s user`); pi is a
merge-only edit of ~/.pi/agent/mcp.json. Each call touches exactly one server name. A harness file
that is not a JSON object with an mcpServers object is refused and left untouched. Output names
only. Standard library only.

Author: Joseph A. Wisneski IV
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class Refusal(Exception):
    """A named refusal; the message carries names only."""


def load_template(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_json_object(path, label):
    """The parsed top-level object, {} for an absent file; a named refusal when it is not one."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise Refusal(f"{label}: unreadable ({type(exc).__name__})") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise Refusal(f"{label}: unparseable, left untouched") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise Refusal(f"{label}: not a JSON object with an mcpServers object, left untouched")
    return data


def atomic_write_json(path, data):
    """Replace the file atomically, keeping the mode of the file it replaces (0600 for a new one)."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def claude_config_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home()) / ".claude.json"


def pi_config_path():
    return Path.home() / ".pi/agent/mcp.json"


def plan_for(current, desired):
    if desired is None:
        return "remove" if current is not None else "absent"
    if current is None:
        return "add"
    return "unchanged" if current == desired else "replace"


def run_claude(*args):
    done = subprocess.run(["claude", "mcp", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise Refusal(f"claude: `mcp {args[0]}` failed (exit {done.returncode})")


def apply_claude(server, desired, dry_run):
    """Make Claude Code's user scope hold `desired` under `server`. `desired` None removes it."""
    if not shutil.which("claude"):
        raise Refusal("claude: CLI not on PATH")
    current = read_json_object(claude_config_path(), "claude user config").get("mcpServers", {}).get(server)
    action = plan_for(current, desired)
    if not dry_run:
        if action in ("remove", "replace"):
            run_claude("remove", "-s", "user", server)
        if action in ("add", "replace"):
            try:
                run_claude("add-json", "-s", "user", server, json.dumps(desired))
            except Refusal:
                if action == "replace":
                    raise Refusal(f"claude: removed {server}; add failed, rerun the installer") from None
                raise
    return action


def apply_pi(server, desired, dry_run):
    """Make pi's mcp.json hold `desired` under `server`, touching nothing else. `desired` None removes it."""
    path = pi_config_path()
    data = read_json_object(path, "pi mcp.json")
    servers = data.get("mcpServers", {})
    action = plan_for(servers.get(server), desired)
    if dry_run or action in ("unchanged", "absent"):
        return action
    merged = copy.deepcopy(data)
    merged.setdefault("mcpServers", {})
    if desired is None:
        del merged["mcpServers"][server]
    else:
        merged["mcpServers"][server] = desired
    atomic_write_json(path, merged)
    return action


HARNESSES = {"claude": apply_claude, "pi": apply_pi}


def run_harnesses(server, names, render, remove, dry_run):
    """Apply to each named harness and print one names-only line per harness. Returns 1 if any was refused.

    `render(name)` returns that harness's entry. It is called only when adding, so a removal needs no
    host or identity.
    """
    failed = False
    for name in names:
        desired = None if remove else render(name)
        try:
            action = HARNESSES[name](server, desired, dry_run)
        except Refusal as refusal:
            print(refusal, file=sys.stderr)
            failed = True
            continue
        would = "would " if dry_run and action in ("add", "replace", "remove") else ""
        print(f"{name}: {would}{action} {server}")
    return 1 if failed else 0
