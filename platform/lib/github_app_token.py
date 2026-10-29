#!/usr/bin/env python3
"""Mint short-lived GitHub credentials from an organisation App.

Why Python and not the shell: the first version of this used ``openssl`` and ``jq``,
on the reasoning that both are everywhere. The orchestrator's container image has
neither, so every run failed with ``openssl: command not found`` — and because the
signing step is inside a ``no_log`` boundary, it surfaced as an unexplained credential
failure rather than a missing binary. Standard library plus ``cryptography`` (already
present wherever Ansible's ``community.hashi_vault`` runs) removes both dependencies.

The credential chain, each step shorter-lived than the last::

    App private key (secret store, never on a runner host)
      -> RS256 assertion   (<=10 min, signed here)
        -> installation token (~1 hour)
          -> registration / remove token (1 hour, single use)
          -> just-in-time runner configuration (one runner, one job)

The private key is read from stdin or a file, never from argv, because argv is
world-readable through /proc for the life of the process. Tokens are written to stdout
alone so a caller can capture one without filtering log noise; everything diagnostic
goes to stderr, and no diagnostic ever contains key or token material.

Usage:
    github_app_token.py installation-id     --issuer <id> --org <org> [--key <path>]
    github_app_token.py installation-token  --issuer <id> --org <org> [--key <path>]
        [--permissions <json>] [--repository <name>]...
    github_app_token.py registration-token   --issuer <id> --org <org> [--key <path>]
    github_app_token.py remove-token         --issuer <id> --org <org> [--key <path>]
    github_app_token.py jit-config      ... --group <name> --label <l>... --prefix <p> --count <n>
    github_app_token.py list-runners    ... --prefix <p>
    github_app_token.py prune-runners   ... --prefix <p> [--keep <name>]... [--retire <name>]

jit-config prints one JSON object per line, {"name", "id", "encoded_jit_config"}: the
encoded configuration is a credential for one runner and goes to stdout only, like a token.
list-runners and prune-runners print one JSON object with runner names and states, never a
credential. The ephemeral lifecycle that uses them is
plan/development/openspec/changes/add-ephemeral-github-runners/design.md.

--key defaults to '-' (stdin). --issuer is the App's client id (upstream's
recommendation) or its app id; the App's OAuth client secret plays no part here.

installation-token with --permissions and/or --repository asks GitHub for a DOWNSCOPED token:
the request body carries the permissions object and the repository names. The token is refused
(nonzero exit, nothing on stdout) when the grant the response reports is wider than requested:
a permission that was not requested, a level above or below the one requested, or a repository
outside the requested set. A requested permission the response leaves out is a narrower grant
and is accepted. Without either flag the request carries no body, exactly as before.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API", "https://api.github.com")


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _b64url(raw: bytes) -> str:
    """base64url without padding, as a JWT requires."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_key(path: str) -> bytes:
    if path == "-":
        data = sys.stdin.buffer.read()
        if not data:
            die("no private key on stdin")
        return data
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        die(f"private key not readable: {path} ({exc.strerror})")
    return b""  # unreachable; keeps type checkers quiet


def sign_assertion(issuer: str, key_pem: bytes) -> str:
    if not issuer:
        die("issuer is required (the App's client id, or its app id)", 2)
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        die(
            "the 'cryptography' package is unavailable to this interpreter, so the App "
            "assertion cannot be signed. Install it for the interpreter Ansible uses on "
            "this host."
        )

    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same story here
        die(f"the private key could not be parsed as a PEM private key: {exc}")

    if not isinstance(key, rsa.RSAPrivateKey):
        die("the App assertion must be signed with an RSA key; this key is not RSA")

    now = int(time.time())
    # iat is backdated 60s to absorb clock skew against the forge, which rejects an
    # assertion issued in its own future. exp is 9 minutes: the documented ceiling is
    # 10, and sitting on the boundary turns skew into an intermittent auth failure.
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": issuer}, separators=(",", ":")).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(sig)}"


def api(path: str, bearer: str, method: str = "GET", body: object | None = None) -> object:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-cloud-runner-automation",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        # The response body can carry the forge's own explanation, which is far more
        # useful than the status alone. It never contains our credentials.
        detail = ""
        try:
            detail = json.loads(exc.read() or b"{}").get("message", "")
        except Exception:  # noqa: BLE001
            pass
        die(f"{method} {path} failed: HTTP {exc.code}{' — ' + detail if detail else ''}")
    except urllib.error.URLError as exc:
        die(f"{method} {path} failed: {exc.reason}")
    return None


def installation_id(issuer: str, key_pem: bytes, org: str) -> str:
    jwt = sign_assertion(issuer, key_pem)
    installs = api("/app/installations", jwt) or []
    if not installs:
        die(
            "this App has no installations. It exists and its key authenticates, but it "
            f"has not been INSTALLED on any account — install it on '{org}' before a "
            "runner can be registered."
        )
    for inst in installs:
        if (inst.get("account") or {}).get("login") == org:
            return str(inst["id"])
    where = ", ".join(str((i.get("account") or {}).get("login")) for i in installs)
    die(f"this App is installed, but not on '{org}'. Installed on: {where}")
    return ""


def parse_permissions(raw: str) -> dict:
    """--permissions: a non-empty JSON object of permission name -> level, both strings."""
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None
    if not isinstance(obj, dict) or not obj or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in obj.items()
    ):
        die("--permissions must be a non-empty JSON object mapping permission names to levels", 2)
    return obj


def scope_body(permissions: dict | None, repositories: list[str] | None) -> dict | None:
    """The request body of a downscoped mint, or None: an unscoped mint sends no body."""
    if permissions is None and repositories is None:
        return None
    body: dict = {}
    if permissions is not None:
        body["permissions"] = permissions
    if repositories is not None:
        body["repositories"] = repositories
    return body


def check_grant(data: dict, permissions: dict | None, repositories: list[str] | None) -> None:
    """Refuse a minted token whose reported grant is wider than the one requested.

    Runs before the token is returned, so a refused token never reaches stdout. Messages name
    permission keys and repository names only, never a token or a level.
    """
    if permissions is not None:
        granted = data.get("permissions")
        if not isinstance(granted, dict):
            die(
                "the installation response carries no permissions object, so the grant cannot be "
                "checked; the token is refused"
            )
        extra = sorted(k for k in granted if k not in permissions)
        if extra:
            die(
                f"the token was granted permission(s) that were not requested: {', '.join(extra)}; "
                "the token is refused"
            )
        differing = sorted(k for k in granted if granted[k] != permissions[k])
        if differing:
            die(
                "the token was granted a different level than requested for: "
                f"{', '.join(differing)}; the token is refused"
            )
    if repositories is not None:
        repos = data.get("repositories")
        if not isinstance(repos, list):
            die(
                "the installation response carries no repositories list, so the coverage cannot be "
                "checked; the token is refused"
            )
        covered = {r.get("name") for r in repos if isinstance(r, dict)}
        if covered != set(repositories):
            die(
                f"the token covers repositories {sorted(str(c) for c in covered)}, not the requested "
                f"{sorted(repositories)}; the token is refused"
            )


# The downscoped mint. Documented at
# https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
# (read 2026-10-29): POST /app/installations/{installation_id}/access_tokens takes an optional
# JSON body with "repositories" (names) and "permissions" (name -> "read" or "write", among
# others); the response carries "token", "expires_at", "permissions" and "repositories". The
# documented success status is 201 Created. urllib raises HTTPError for every non-2xx answer,
# so no status check is needed here. The page does not say how a request for more than the App
# was granted is answered, so the check below reads the grant the response REPORTS.
def installation_token(
    issuer: str,
    key_pem: bytes,
    org: str,
    *,
    permissions: dict | None = None,
    repositories: list[str] | None = None,
) -> str:
    jwt = sign_assertion(issuer, key_pem)
    iid = installation_id(issuer, key_pem, org)
    body = scope_body(permissions, repositories)
    data = api(f"/app/installations/{iid}/access_tokens", jwt, method="POST", body=body) or {}
    token = data.get("token")
    if not token:
        die("no token in the installation response")
    check_grant(data, permissions, repositories)
    return token


def runner_token(issuer: str, key_pem: bytes, org: str, kind: str) -> str:
    itok = installation_token(issuer, key_pem, org)
    data = api(f"/orgs/{org}/actions/runners/{kind}", itok, method="POST") or {}
    token = data.get("token")
    if not token:
        die(
            f"no token in the {kind} response — does the App grant Organization "
            "'Self-hosted runners: read & write'?"
        )
    return token


# ── just-in-time runners (the ephemeral lifecycle) ─────────────────────────────
# A just-in-time configuration registers a runner that accepts exactly one job and is then
# removed by the service (POST /orgs/{org}/actions/runners/generate-jitconfig). The App needs
# the same permission as for a registration token. The name, group and labels are fixed by
# THIS call, on the orchestrator; the host that later starts the runner cannot choose them,
# which a registration token (good for any name, labels and group the App allows) would let
# it do.

# A prefix must name one host's ephemeral runners and nothing else: prune deletes by it.
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*-jit-$")


def check_prefix(prefix: str) -> str:
    if not PREFIX_RE.match(prefix or ""):
        die(f"--prefix must look like '<runner-name>-jit-', got {prefix!r}", 2)
    return prefix


def paged(path: str, token: str, key: str) -> list:
    out: list = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        data = api(f"{path}{sep}per_page=100&page={page}", token) or {}
        items = data.get(key) or []
        out.extend(items)
        if len(items) < 100:
            return out
        page += 1


def runner_group_id(token: str, org: str, group: str) -> int:
    for g in paged(f"/orgs/{org}/actions/runner-groups", token, "runner_groups"):
        if g.get("name") == group:
            return int(g["id"])
    die(
        f"no runner group named {group!r} in {org!r}. Converge it first with "
        "manage-github-runner-group.yml; a runner must never land in another group."
    )
    return 0


def jit_name(prefix: str) -> str:
    return f"{prefix}{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{secrets.token_hex(3)}"


def minted_at(name: str, prefix: str) -> float | None:
    """The mint time encoded in a just-in-time runner's name (jit_name), or None."""
    stamp = name[len(prefix) :].split("-", 1)[0]
    try:
        return calendar.timegm(time.strptime(stamp, "%Y%m%d%H%M%S"))
    except ValueError:
        return None


def jit_configs(token: str, org: str, group: str, labels: list[str], prefix: str, count: int) -> list[dict]:
    if count < 0 or count > 50:
        die("--count must be between 0 and 50", 2)
    if not labels:
        die("at least one --label is required: a runner without labels cannot be targeted", 2)
    gid = runner_group_id(token, org, group)
    out = []
    for _ in range(count):
        name = jit_name(prefix)
        data = (
            api(
                f"/orgs/{org}/actions/runners/generate-jitconfig",
                token,
                method="POST",
                body={"name": name, "runner_group_id": gid, "labels": labels, "work_folder": "_work"},
            )
            or {}
        )
        runner = data.get("runner") or {}
        encoded = data.get("encoded_jit_config")
        if not encoded or runner.get("name") != name:
            die("the just-in-time response carried no configuration for the requested runner")
        out.append({"name": name, "id": runner.get("id"), "encoded_jit_config": encoded})
    return out


def runners_with_prefix(token: str, org: str, prefix: str) -> list[dict]:
    runners = paged(f"/orgs/{org}/actions/runners", token, "runners")
    return [r for r in runners if str(r.get("name", "")).startswith(prefix)]


def summarise(runners: list[dict]) -> dict:
    return {
        "online": sorted(r["name"] for r in runners if r.get("status") == "online" and not r.get("busy")),
        "busy": sorted(r["name"] for r in runners if r.get("busy")),
        "offline": sorted(r["name"] for r in runners if r.get("status") != "online" and not r.get("busy")),
    }


def prune(
    token: str, org: str, prefix: str, keep: list[str], retire: str | None, grace_minutes: int = 30
) -> dict:
    """Delete this host's ephemeral runners that can never take a job.

    An ephemeral runner that is offline, not busy, and not in the host's stock (`keep`) is a
    configuration that was discarded, expired, or whose runner never started. Only runners
    the service itself reports as ephemeral are touched, so a persistent registration that
    happens to share the prefix is never deleted by this path.

    A record younger than `grace_minutes` (by the mint time in its name) is kept even when
    it is not in `keep`: `keep` is read from the host before this call, so a concurrent run
    (the deploy and the schedule) may have minted and stocked it since. Deleting it would
    leave a stocked configuration whose runner no longer exists. A record whose name holds
    no readable time is treated as old.

    `retire` names the persistent registration a host is leaving (the switch to the
    ephemeral lifecycle). It is deleted unless it is running a job.
    """
    keep_set = set(keep)
    cutoff = time.time() - grace_minutes * 60
    found = runners_with_prefix(token, org, prefix)
    deleted = []
    for r in found:
        idle = r.get("status") != "online" and not r.get("busy")
        young = (minted_at(r["name"], prefix) or 0) > cutoff
        if r.get("ephemeral") is True and idle and not young and r["name"] not in keep_set:
            api(f"/orgs/{org}/actions/runners/{int(r['id'])}", token, method="DELETE")
            deleted.append(r["name"])
    retired = None
    if retire:
        if retire.startswith(prefix):
            die("--retire names a persistent runner, not one of the prefix's ephemeral runners", 2)
        for r in paged(f"/orgs/{org}/actions/runners", token, "runners"):
            if r.get("name") == retire:
                if r.get("busy"):
                    die(f"{retire} is running a job; drain it before retiring its registration")
                api(f"/orgs/{org}/actions/runners/{int(r['id'])}", token, method="DELETE")
                retired = retire
    remaining = [r for r in found if r["name"] not in set(deleted)]
    return {"deleted": sorted(deleted), "retired": retired, **summarise(remaining)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "action",
        choices=[
            "installation-id",
            "installation-token",
            "registration-token",
            "remove-token",
            "jit-config",
            "list-runners",
            "prune-runners",
        ],
    )
    ap.add_argument("--issuer", required=True, help="App client id (recommended) or app id")
    ap.add_argument("--org", required=True)
    ap.add_argument("--key", default="-", help="private key path, or '-' for stdin (default)")
    ap.add_argument("--group", help="jit-config: runner group name")
    ap.add_argument("--label", action="append", default=[], help="jit-config: a label (repeat)")
    ap.add_argument("--prefix", help="jit-config / list-runners / prune-runners: '<runner-name>-jit-'")
    ap.add_argument("--count", type=int, default=1, help="jit-config: how many configurations")
    ap.add_argument("--keep", action="append", default=[], help="prune-runners: a stocked name (repeat)")
    ap.add_argument("--retire", help="prune-runners: a persistent registration to delete")
    ap.add_argument(
        "--grace-minutes", type=int, default=30, help="prune-runners: keep unheld records minted more recently"
    )
    ap.add_argument(
        "--permissions",
        help="installation-token: JSON object of permission name -> level; the token is refused if granted more",
    )
    ap.add_argument(
        "--repository",
        action="append",
        default=[],
        help="installation-token: a repository name the token must cover exactly (repeat)",
    )
    args = ap.parse_args()
    if (args.permissions is not None or args.repository) and args.action != "installation-token":
        die("--permissions and --repository apply to installation-token only", 2)

    if args.action in ("jit-config", "list-runners", "prune-runners"):
        prefix = check_prefix(args.prefix or "")
        if args.action == "jit-config" and not args.group:
            die("jit-config needs --group", 2)
        token = installation_token(args.issuer, read_key(args.key), args.org)
        if args.action == "jit-config":
            for item in jit_configs(token, args.org, args.group, args.label, prefix, args.count):
                sys.stdout.write(json.dumps(item) + "\n")
        elif args.action == "list-runners":
            sys.stdout.write(json.dumps(summarise(runners_with_prefix(token, args.org, prefix))))
        else:
            result = prune(token, args.org, prefix, args.keep, args.retire, args.grace_minutes)
            sys.stdout.write(json.dumps(result))
        return

    key_pem = read_key(args.key)
    if args.action == "installation-id":
        out = installation_id(args.issuer, key_pem, args.org)
    elif args.action == "installation-token":
        permissions = parse_permissions(args.permissions) if args.permissions is not None else None
        out = installation_token(
            args.issuer,
            key_pem,
            args.org,
            permissions=permissions,
            repositories=args.repository or None,
        )
    elif args.action == "registration-token":
        out = runner_token(args.issuer, key_pem, args.org, "registration-token")
    else:
        out = runner_token(args.issuer, key_pem, args.org, "remove-token")
    sys.stdout.write(out)


if __name__ == "__main__":
    main()
