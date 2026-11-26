#!/usr/bin/env python3
"""Install or remove the agentgateway MCP server in Claude Code and pi, secret-free.

Renders the committed client templates for one MCP hostname and merges exactly one entry
(`agentgateway`) into each harness's user-level configuration, through scripts/mcp_harness.py: Claude
Code through its own CLI (`claude mcp add-json -s user`), pi by a merge-only edit of ~/.pi/agent/mcp.json.
The entry carries a command reference to scripts/agw-mcp-key, never a key. Idempotent; output names
only; an unparseable harness file is refused and left untouched.
"""

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_harness import load_template, run_harnesses  # noqa: E402

SERVER = "agentgateway"
PLACEHOLDER_HOST = "mcp.example.invalid"
ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "platform/services/agentgateway/context/clients"
HELPER = Path(__file__).resolve().parent / "agw-mcp-key"
LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOSTNAME = re.compile(rf"{LABEL}(\.{LABEL})+")
IDENTITY = re.compile(r"[A-Za-z0-9._-]+")


def helper_command(fmt, identity, key_file):
    parts = [str(HELPER), "--format", fmt, "--identity", identity]
    if key_file:
        parts += ["--key-file", str(Path(key_file).expanduser().resolve())]
    return " ".join(shlex.quote(p) for p in parts)


def render_claude(host, identity, key_file):
    entry = load_template(TEMPLATES / "claude-code.json")
    entry["url"] = entry["url"].replace(PLACEHOLDER_HOST, host)
    entry["headersHelper"] = helper_command("claude", identity, key_file)
    return entry


def render_pi(host, identity, key_file):
    entry = load_template(TEMPLATES / "pi-mcp.json")
    entry["url"] = entry["url"].replace(PLACEHOLDER_HOST, host)
    entry["headers"]["Authorization"] = "!" + helper_command("pi", identity, key_file)
    return entry


RENDERERS = {"claude": render_claude, "pi": render_pi}


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
    return run_harnesses(
        SERVER,
        names,
        lambda name: RENDERERS[name](args.host, args.identity, args.key_file),
        args.remove,
        args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
