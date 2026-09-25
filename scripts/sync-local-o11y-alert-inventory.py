#!/usr/bin/env python3
"""Copy the reviewed private Discord destination into local Semaphore inventory.

Reads site-config's production declaration and changes only two entries in the
existing local o11y_svc INI group. The operator token arrives on stdin. Without
--apply this is a read-only preview; rerunning an applied revision is a no-op.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("o11y_alert_discord_guild_id", "o11y_alert_discord_channel_id")
SECTION = "[o11y_svc:vars]"


class Refusal(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise Refusal("A required Git revision or clean source file could not be verified")
    return result.stdout.strip()


def destination(source):
    try:
        record = yaml.safe_load(source.read_text())["agent_cloud"]["children"]["o11y_svc"]["vars"]
        values = {name: str(record[name]) for name in FIELDS}
    except (OSError, TypeError, KeyError, AttributeError, yaml.YAMLError):
        raise Refusal("Private inventory has no single declared o11y alert destination") from None
    if any(not re.fullmatch(r"[0-9]{17,21}", value) for value in values.values()):
        raise Refusal("Private Discord guild and channel IDs must be 17-21 decimal digits")
    return values


def overlay(inventory, values):
    lines = inventory.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if line.strip() == SECTION]
    hosts = [i for i, line in enumerate(lines) if line.strip() == "[o11y_svc]"]
    if len(headers) != 1 or len(hosts) != 1:
        raise Refusal("Local inventory must declare exactly one o11y service and vars section")
    host_end = next((i for i in range(hosts[0] + 1, len(lines))
                     if re.fullmatch(r"\s*\[[^\]]+\]\s*", lines[i])), len(lines))
    declared_hosts = [line.split()[0] for line in lines[hosts[0] + 1:host_end]
                      if line.strip() and not line.lstrip().startswith(("#", ";"))]
    if declared_hosts != ["o11y-local"]:
        raise Refusal("Local o11y group must contain only its declared receiver host")
    start = headers[0] + 1
    end = next((i for i in range(start, len(lines)) if re.fullmatch(r"\s*\[[^\]]+\]\s*", lines[i])), len(lines))
    block = lines[start:end]
    for name, value in values.items():
        matches = [i for i, line in enumerate(block) if re.match(rf"^\s*{re.escape(name)}\s*=", line)]
        if len(matches) > 1:
            raise Refusal(f"Local inventory has duplicate {name} entries")
        replacement = f"{name}={value}\n"
        if matches:
            block[matches[0]] = replacement
        else:
            block.append(replacement)
    return "".join(lines[:start] + block + lines[end:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config-root", type=Path, required=True)
    parser.add_argument("--site-config-sha", required=True)
    parser.add_argument("--expected-dev-sha", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", type=int, default=1)
    parser.add_argument("--inventory", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (args.site_config_sha, args.expected_dev_sha)):
            raise Refusal("Supply exact reviewed Git commits")
        source_root = args.site_config_root.resolve()
        if Path(git(source_root, "rev-parse", "--show-toplevel")).resolve() != source_root:
            raise Refusal("Site-config source must be a Git worktree root")
        if git(source_root, "rev-parse", "HEAD") != args.site_config_sha:
            raise Refusal("Private inventory checkout differs from its reviewed commit")
        if git(source_root, "status", "--porcelain", "--", "inventory/production.yml"):
            raise Refusal("Private inventory has uncommitted changes")
        if (git(ROOT, "rev-parse", "HEAD") != args.expected_dev_sha
                or git(ROOT, "status", "--porcelain", "--untracked-files=all")):
            raise Refusal("Automation checkout differs from its clean reviewed commit")
        if git(ROOT, "ls-remote", "origin", "refs/heads/dev").split()[0] != args.expected_dev_sha:
            raise Refusal("Remote dev moved; review the new revision before syncing")
        values = destination(source_root / "inventory/production.yml")
        parsed = urllib.parse.urlsplit(args.url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise Refusal("Semaphore URL must be a plain HTTPS origin")
        token = sys.stdin.read().strip()
        if not token:
            raise Refusal("An approved operator token is required on stdin")
        endpoint = args.url.rstrip("/") + f"/api/project/{args.project}/inventory/{args.inventory}"
        opener = urllib.request.build_opener(NoRedirect)

        def request(method, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(endpoint, data=data, method=method,
                                         headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json"})
            try:
                with opener.open(req, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                raise Refusal(f"Semaphore inventory API returned HTTP {error.code}; body suppressed") from None
            except (OSError, ValueError):
                raise Refusal("Semaphore inventory API outcome unavailable; inspect before retrying") from None

        current = request("GET")
        if (not isinstance(current, dict) or current.get("id") != args.inventory
                or current.get("project_id") != args.project
                or current.get("name") != "local" or current.get("type") != "static"
                or not isinstance(current.get("inventory"), str)):
            raise Refusal("Target is not the reviewed local static inventory")
        desired = overlay(current["inventory"], values)
        if desired == current["inventory"]:
            print(f"Local inventory {args.inventory} already matches the private Discord destination")
            return 0
        if not args.apply:
            print(f"Would update only {', '.join(FIELDS)} in local inventory {args.inventory}; no write")
            return 0
        if request("GET") != current:
            raise Refusal("Local inventory changed during preparation; no automatic retry")
        body = {key: current.get(key) for key in ("id", "name", "project_id", "type", "ssh_key_id", "become_key_id")}
        body["inventory"] = desired
        request("PUT", body)
        after = request("GET")
        if not isinstance(after, dict) or after.get("inventory") != desired:
            raise Refusal("Local inventory readback differs; reconcile before alerts")
        print(f"Local inventory {args.inventory} now matches the private Discord destination")
        return 0
    except (Refusal, IndexError) as error:
        print(str(error) if isinstance(error, Refusal) else "Remote dev identity unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
