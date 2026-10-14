#!/usr/bin/env python3
"""Install or remove the agentgateway MCP server in Claude Code and pi, secret-free.

Renders the committed client templates for one MCP hostname and merges exactly one entry
(`agentgateway`) into each harness's user-level configuration: Claude Code through its own
CLI (`claude mcp add-json -s user`), pi by a merge-only edit of ~/.pi/agent/mcp.json. The
entry carries a command reference to scripts/agw-mcp-key, never a key. Idempotent; output
names only; an unparseable harness file is refused and left untouched.
"""

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER = "agentgateway"
PLACEHOLDER_HOST = "mcp.example.invalid"
ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "platform/services/agentgateway/context/clients"
HELPER = Path(__file__).resolve().parent / "agw-mcp-key"
LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOSTNAME = re.compile(rf"{LABEL}(\.{LABEL})+")
IDENTITY = re.compile(r"[A-Za-z0-9._-]+")


class Refusal(Exception):
    """A named refusal; the message carries names only."""


def helper_command(fmt, identity, key_file):
    parts = [str(HELPER), "--format", fmt, "--identity", identity]
    if key_file:
        parts += ["--key-file", str(Path(key_file).expanduser().resolve())]
    return " ".join(shlex.quote(p) for p in parts)


def load_template(name):
    return json.loads((TEMPLATES / name).read_text(encoding="utf-8"))


def render_claude(host, identity, key_file):
    entry = load_template("claude-code.json")
    entry["url"] = entry["url"].replace(PLACEHOLDER_HOST, host)
    entry["headersHelper"] = helper_command("claude", identity, key_file)
    return entry


def render_pi(host, identity, key_file):
    entry = load_template("pi-mcp.json")
    entry["url"] = entry["url"].replace(PLACEHOLDER_HOST, host)
    entry["headers"]["Authorization"] = "!" + helper_command("pi", identity, key_file)
    return entry


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


def apply_claude(host, identity, key_file, remove, dry_run):
    if not shutil.which("claude"):
        raise Refusal("claude: CLI not on PATH")
    current = read_json_object(claude_config_path(), "claude user config").get("mcpServers", {}).get(SERVER)
    desired = None if remove else render_claude(host, identity, key_file)
    action = plan_for(current, desired)
    if not dry_run:
        if action in ("remove", "replace"):
            run_claude("remove", "-s", "user", SERVER)
        if action in ("add", "replace"):
            try:
                run_claude("add-json", "-s", "user", SERVER, json.dumps(desired))
            except Refusal:
                if action == "replace":
                    raise Refusal(f"claude: removed {SERVER}; add failed, rerun the installer") from None
                raise
    return action


def apply_pi(host, identity, key_file, remove, dry_run):
    path = pi_config_path()
    data = read_json_object(path, "pi mcp.json")
    servers = data.get("mcpServers", {})
    desired = None if remove else render_pi(host, identity, key_file)
    action = plan_for(servers.get(SERVER), desired)
    if dry_run or action in ("unchanged", "absent"):
        return action
    merged = copy.deepcopy(data)
    merged.setdefault("mcpServers", {})
    if desired is None:
        del merged["mcpServers"][SERVER]
    else:
        merged["mcpServers"][SERVER] = desired
    atomic_write_json(path, merged)
    return action


HARNESSES = {"claude": apply_claude, "pi": apply_pi}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    env = os.environ.get
    parser.add_argument(
        "--host",
        default=env("AGW_MCP_HOST"),
        help="MCP hostname, no scheme or path (env AGW_MCP_HOST; required unless --remove)",
    )
    parser.add_argument(
        "--identity",
        default=env("AGW_MCP_IDENTITY"),
        help="gateway identity the key was minted for (env AGW_MCP_IDENTITY; required unless --remove)",
    )
    parser.add_argument(
        "--key-file",
        default=env("AGW_MCP_KEY_FILE"),
        help="handout file holding the key (env AGW_MCP_KEY_FILE; a path, the helper reads it at connect time)",
    )
    parser.add_argument(
        "--harness", default=env("AGW_MCP_HARNESS") or "both", help="claude, pi or both (env AGW_MCP_HARNESS)"
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    parser.add_argument("--remove", action="store_true", help=f"delete exactly the {SERVER} entry")
    args = parser.parse_args(argv)
    if args.harness not in ("claude", "pi", "both"):
        parser.error("--harness must be claude, pi or both")
    if not args.remove:
        if not args.host or not HOSTNAME.fullmatch(args.host):
            parser.error("--host must be a bare hostname such as mcp.example.com")
        if not args.identity or not IDENTITY.fullmatch(args.identity):
            parser.error("--identity must match [A-Za-z0-9._-]+")
    names = ("claude", "pi") if args.harness == "both" else (args.harness,)
    failed = False
    for name in names:
        try:
            action = HARNESSES[name](args.host, args.identity, args.key_file, args.remove, args.dry_run)
        except Refusal as refusal:
            print(refusal, file=sys.stderr)
            failed = True
            continue
        print(f"{name}: {'would ' if args.dry_run and action in ('add', 'replace', 'remove') else ''}{action} {SERVER}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
