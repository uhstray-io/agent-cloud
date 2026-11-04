#!/usr/bin/env python3
"""Install or remove the Plane MCP server in Claude Code and pi, secret-free.

Renders the committed client templates for one Plane MCP host and merges exactly one entry (`plane`)
into each harness's user-level configuration, through scripts/mcp_harness.py: Claude Code through its
own CLI (`claude mcp add-json -s user`), pi by a merge-only edit of ~/.pi/agent/mcp.json. The entry
carries the URL and nothing else. Each harness runs Plane's OAuth sign-in itself, so there is no
header, token or helper to install. Idempotent; output names only; an unparseable harness file is
refused and left untouched.

pi support, checked against the installed pi-mcp-adapter 5.1.0: a URL-only HTTP server gets OAuth
automatically (docs/auth.md:43-49), manual sign-in is the default flow (docs/auth.md:118-126), and the
file it reads is ~/.pi/agent/mcp.json (README.md:81). Pi's built-in MCP also signs in to OAuth servers
(docs/auth.md:139), so the URL-only entry does not depend on the adapter alone.

Author: Joseph A. Wisneski IV
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_harness import load_template, run_harnesses  # noqa: E402

SERVER = "plane"
PLACEHOLDER_HOST = "plane-mcp.example.invalid"
ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "platform/services/plane-mcp/context/clients"
LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
# A bare hostname with an optional port. The local Caddy serves Plane MCP on :8443 (docs/LOCAL-DEV.md).
HOST_PORT = re.compile(rf"(?P<host>{LABEL}(\.{LABEL})+)(:(?P<port>[1-9][0-9]{{0,4}}))?")


def valid_host(value):
    match = HOST_PORT.fullmatch(value or "")
    return bool(match) and (match.group("port") is None or int(match.group("port")) <= 65535)


def render_entry(name, host):
    entry = load_template(TEMPLATES / name)
    entry["url"] = entry["url"].replace(PLACEHOLDER_HOST, host)
    return entry


def render_claude(host):
    return render_entry("claude-code.json", host)


def render_pi(host):
    return render_entry("pi-mcp.json", host)


RENDERERS = {"claude": render_claude, "pi": render_pi}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host",
        default=os.environ.get("PLANE_MCP_HOST"),
        help="Plane MCP hostname, optional :port, no scheme or path (env PLANE_MCP_HOST; required unless --remove)",
    )
    parser.add_argument("--harness", default="both", help="claude, pi or both")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    parser.add_argument("--remove", action="store_true", help=f"delete exactly the {SERVER} entry")
    args = parser.parse_args(argv)
    if args.harness not in ("claude", "pi", "both"):
        parser.error("--harness must be claude, pi or both")
    if not args.remove and not valid_host(args.host):
        parser.error("--host must be a bare hostname, optionally with :port, such as plane-mcp.example.com")
    names = ("claude", "pi") if args.harness == "both" else (args.harness,)
    return run_harnesses(SERVER, names, lambda name: RENDERERS[name](args.host), args.remove, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
