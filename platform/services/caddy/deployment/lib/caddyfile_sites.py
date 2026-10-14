"""Read and edit the site blocks of a flat Caddyfile.

WHY THIS EXISTS. Routes on the central Caddy arrived by hand, one block at a
time, and the file became the only record of them. That is how
one platform route came to point at an address declared for a *different* host,
and kept pointing there after the two machines started answering one IP. Nothing
compared the route to the declaration, because nothing could read the routes.

So this does two things, and only two:

  list   — report every site block: its addresses, its upstreams (all together,
           and per `handle` block), and whether it sits inside the ANSIBLE MANAGED
           region. That is the "read" half, and it is what makes drift between
           Caddy and the inventory visible.
  routes / declared / running — where one address sends /v1/* and /health, read
           from the file, from a declaration not yet written, or from Caddy's
           running config (see ROUTES below).
  retire — delete a named site block that sits OUTSIDE the managed region, so the
           managed region can take ownership of that hostname. Caddy refuses a
           file that defines one hostname twice, so migrating a hand-maintained
           route means removing the old block in the same pass that adds the new.

It deliberately does NOT rewrite upstreams in place. Editing a hand-maintained
block would keep the file as the source of truth and leave the next drift just as
invisible; retiring it moves the declaration into inventory, where it is
reviewable and diffable. One-way, on purpose.

Parsing notes, each of which is a real case in this file:
  * `{$CLOUDFLARE_API_KEY}` and `{http.request.uri}` open and close on one line,
    so depth counting handles them without special-casing.
  * The global options block is a brace block at column 0 with no address before
    it; it is not a site and must never be reported or retired as one.
  * A site header may list several comma-separated addresses.
  * Only column-0 headers start a site block; `route {` and `tls {` are nested.
  * A `handle <matcher> {` block directly inside a site is one handler: the
    matcher is a literal path or a `@name path ...` declared in the same site,
    and the upstreams are those of the reverse_proxy lines inside it. A
    reverse_proxy outside any `handle` is a catch-all handler (no paths).

ROUTES. A route that fronts an API in the allowlist shape sends different paths
to different upstreams (the inference edge: `/v1/*` follows the gateway/direct
mode while `/health` always goes straight to vLLM). `summarize` reduces a site's
handlers, read from the file or from Caddy's running config, to the three sets a
verifier needs: where `/v1/*` goes, where `/health` goes, and every other
upstream the block can reach.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

MANAGED_BEGIN = "# BEGIN ANSIBLE MANAGED"
MANAGED_END = "# END ANSIBLE MANAGED"

# The two paths of the inference allowlist shape that `summarize` reports on.
API_PATH = "/v1/*"
HEALTH_PATH = "/health"

# `respond <<HTML` ... a line that is exactly `HTML` ends it.
_HEREDOC = re.compile(r"(?<!\\)<<([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _split_addresses(header: str) -> list[str]:
    """Addresses may be separated by commas OR whitespace, per the docs:
    `localhost:8080, example.com` and `localhost:8080 example.com` are both one
    site with two addresses. Splitting on commas alone yielded the single
    address "a.example.io b.example.io", which no lookup could ever match."""
    return [a for a in re.split(r"[,\s]+", header.strip()) if a]


def _strip_comment(line: str) -> str:
    """Drop a trailing comment, per the two documented rules TOGETHER.

    Position: "The hash character `#` for a comment cannot appear in the middle
    of a token (i.e. it must be preceded by a space or appear at the beginning of
    a line)." That is precisely so a `#` inside a URI needs no quoting.

    Quoting: "Inside quoted tokens, all other characters are treated literally."
    So a `#` inside a quoted token is literal even when a space precedes it.

    Both rules are needed. An earlier version applied only quoting and truncated
    `reverse_proxy http://host/#frag`; the version after it applied only position
    and truncated `respond "a # b"`. Each was right about one rule and wrong
    about the other.
    """
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            # Within a double-quoted token a quote may be escaped: "\"abc\"".
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "`"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
        i += 1
    return line


# A site header is an address list; these column-0 blocks are not sites.
#   (name) {   snippet, invoked with `import`
#   &(name) {  named route, invoked with `invoke`
def _is_site_header(header: str) -> bool:
    return not header.startswith(("(", "&("))


def _handler_paths(matcher: str | None, matchers: dict[str, list[str] | None]) -> list[str]:
    """The paths a `handle` matcher selects; [] is a catch-all. A matcher this module
    cannot reduce to paths (a header or method matcher) is kept as its own token, so
    it never reads as a catch-all and never equals a path anyone checks for."""
    if matcher is None or matcher == "*":
        return []
    if matcher.startswith("@"):
        return matchers.get(matcher) or [matcher]
    return [matcher]


def parse_sites(text: str) -> list[dict]:
    """Return one entry per site block, in file order."""
    sites: list[dict] = []
    depth = 0
    managed = False
    cur: dict | None = None
    heredoc: str | None = None
    matchers: dict[str, list[str] | None] = {}
    handler: dict | None = None
    implicit: dict | None = None

    for idx, raw in enumerate(text.splitlines()):
        if MANAGED_BEGIN in raw:
            managed = True

        # Inside a heredoc every character is literal, braces included. Counting
        # them desyncs depth for the rest of the file: one unbalanced brace in a
        # heredoc made parse_sites return NOTHING, so the file read as having no
        # routes at all rather than as unparseable.
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue

        code = _strip_comment(raw)
        stripped = code.strip()

        m = _HEREDOC.search(stripped)
        if m:
            heredoc = m.group(1)

        if depth == 0 and stripped.endswith("{") and not raw[:1].isspace():
            header = stripped[:-1].strip()
            # A bare '{' at column 0 is the global options block; '(x)' is a
            # snippet and '&(x)' a named route. None of the three is a site.
            if header and _is_site_header(header):
                cur = {
                    "addresses": _split_addresses(header),
                    "start": idx,
                    "end": None,
                    "upstreams": [],
                    "handlers": [],
                    "managed": managed,
                }
                matchers, handler, implicit = {}, None, None

        if cur is not None and depth == 1:
            parts = stripped.split()
            if parts and parts[0].startswith("@"):
                # `@name path /a /b` is resolvable; any other matcher is not.
                matchers[parts[0]] = parts[2:] if len(parts) > 2 and parts[1] == "path" else None
            elif parts and parts[0] == "handle":
                arg = [t for t in parts[1:] if t != "{"]
                handler = {"matcher": arg[0] if arg else None, "paths": [], "upstreams": []}
                cur["handlers"].append(handler)

        if cur is not None and "reverse_proxy" in stripped:
            parts = stripped.split()
            target = handler
            if target is None:
                if implicit is None:
                    implicit = {"matcher": None, "paths": [], "upstreams": []}
                    cur["handlers"].append(implicit)
                target = implicit
            # `reverse_proxy [matcher] <upstream...> [{`  — take the tokens that
            # look like upstreams, skipping a matcher and a trailing brace.
            for tok in parts[1:]:
                if tok in ("{", "}") or tok.startswith(("@", "/")):
                    continue
                cur["upstreams"].append(tok)
                target["upstreams"].append(tok)

        depth += code.count("{") - code.count("}")

        if handler is not None and depth <= 1:
            handler = None

        if cur is not None and depth == 0 and idx >= cur["start"]:
            for h in cur["handlers"]:
                h["paths"] = _handler_paths(h["matcher"], matchers)
            cur["end"] = idx
            sites.append(cur)
            cur = None

        if MANAGED_END in raw:
            managed = False

    return sites


def _netloc(upstream: str) -> str:
    """`https://h:p/x` -> `h:p`: Caddy dials the host and port whatever the scheme."""
    return re.sub(r"/.*$", "", re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", upstream))


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def summarize(handlers: list[dict]) -> dict:
    """Reduce a route's handlers ({"paths": [...], "upstreams": [...]}) to where each of
    the two paths of the inference allowlist shape goes.

    `v1` / `health`: the upstreams (host:port) that serve API_PATH / HEALTH_PATH. A
    handler naming the path serves it; only when none does, the catch-all handlers
    (no paths) do. `other`: every upstream of a handler that serves neither, i.e. a
    place the block can send a request that nothing was meant to proxy (a catch-all
    that also proxies, a /metrics route). Each list is unique, in first-seen order."""
    used: set[int] = set()
    out: dict[str, list[str]] = {}
    for key, path in (("v1", API_PATH), ("health", HEALTH_PATH)):
        serving = [i for i, h in enumerate(handlers) if path in h["paths"]]
        if not serving:
            serving = [i for i, h in enumerate(handlers) if not h["paths"]]
        used.update(serving)
        out[key] = _unique(_netloc(u) for i in serving for u in handlers[i]["upstreams"])
    out["other"] = _unique(
        _netloc(u) for i, h in enumerate(handlers) if i not in used for u in h["upstreams"]
    )
    return out


def _dials(node) -> list[str]:
    """Every `dial` anywhere under a JSON value."""
    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("dial"), str):
            found.append(node["dial"])
        for v in node.values():
            found.extend(_dials(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(_dials(v))
    return found


def _walk_route(route: dict, paths: list[str], out: list[dict]) -> None:
    # Caddy nests a `handle` block as route -> subroute -> route -> reverse_proxy; the
    # path matcher sits on the outer route, so the nearest one above an upstream wins.
    own = [p for m in route.get("match") or [] for p in m.get("path") or []]
    paths = own or paths
    for h in route.get("handle") or []:
        if h.get("handler") == "reverse_proxy":
            dials = [u["dial"] for u in h.get("upstreams") or [] if "dial" in u]
            out.append({"paths": paths, "upstreams": dials})
        for child in h.get("routes") or []:
            _walk_route(child, paths, out)


def running_handlers(config: dict, address: str) -> list[dict]:
    """The handlers of the routes Caddy is RUNNING for `address` (its admin API config).

    A dial the walk could not attribute to a handler (reached through a structure this
    does not follow) is added under a path nothing matches, so it counts as `other`
    instead of being missed."""
    out: list[dict] = []
    for srv in ((config.get("apps") or {}).get("http") or {}).get("servers", {}).values():
        for route in srv.get("routes") or []:
            if not any(address in (m.get("host") or []) for m in route.get("match") or []):
                continue
            seen: list[dict] = []
            _walk_route(route, [], seen)
            missed = [d for d in _dials(route) if d not in {u for h in seen for u in h["upstreams"]}]
            out.extend(seen)
            if missed:
                out.append({"paths": ["<unattributed>"], "upstreams": _unique(missed)})
    return out


def find_site(sites: list[dict], address: str) -> dict | None:
    for s in sites:
        if address in s["addresses"]:
            return s
    return None


def retire(text: str, addresses: list[str]) -> tuple[str, list[str], list[str]]:
    """Remove hand-maintained blocks for `addresses`.

    Returns (new_text, removed, already_managed).

    An address found only INSIDE the managed region is not an error: it is the
    steady state after an adoption. The declaration that adopted it — the block
    in caddy_managed_sites plus the name in caddy_retire_sites — is what every
    later run carries too, so the same declaration has to converge, not refuse.
    Such an address is left alone and reported under `already_managed`; nothing
    is deleted from the managed region, which blockinfile rewrites wholesale
    anyway. (An earlier version raised here. The first re-run after adopting a
    route then failed on the production Caddy — rolled back, nothing served
    wrong, but the run could not complete until the declaration was edited,
    which is the opposite of idempotent.)
    """
    sites = parse_sites(text)
    lines = text.splitlines(keepends=True)
    drop: set[int] = set()
    removed: list[str] = []
    already_managed: list[str] = []

    for addr in addresses:
        site = find_site(sites, addr)
        if site is None:
            continue
        if site["managed"]:
            already_managed.append(addr)
            continue
        if len(site["addresses"]) > 1:
            raise SystemExit(
                f"refusing to retire '{addr}': its block also serves "
                f"{', '.join(a for a in site['addresses'] if a != addr)}. "
                "Split the block by hand first — deleting it would silently take "
                "those hostnames down too."
            )
        drop.update(range(site["start"], site["end"] + 1))
        removed.append(addr)

    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    # Collapse the run of blank lines the deletion leaves behind, so repeated
    # runs do not grow the file with whitespace.
    out: list[str] = []
    for ln in kept:
        if ln.strip() == "" and out and out[-1].strip() == "":
            continue
        out.append(ln)
    return "".join(out), removed, already_managed


def running_main(argv: list[str]) -> int:
    """`running --address A --engine E --container C`: no Caddyfile is involved, so it has
    its own parser rather than a path argument it would ignore."""
    ap = argparse.ArgumentParser(prog="caddyfile_sites.py running")
    ap.add_argument("--address", required=True)
    ap.add_argument("--engine", required=True)
    ap.add_argument("--container", required=True)
    args = ap.parse_args(argv)
    # Read here, not by the caller: the running config can carry resolved secrets (a
    # DNS-challenge token), and only the three upstream lists should leave.
    proc = subprocess.run(
        [args.engine, "exec", args.container, "wget", "-qO-", "http://127.0.0.1:2019/config/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print("could not read Caddy's running config from its admin API", file=sys.stderr)
        return 1
    print(json.dumps(summarize(running_handlers(json.loads(proc.stdout or "null") or {}, args.address))))
    return 0


def declared_main(argv: list[str]) -> int:
    """`declared --address A`: where the site blocks on stdin (a rendered caddy_managed_sites)
    send A, before anything is written. Stdin, not an argument or the environment, so the
    declaration never lands in a process listing."""
    ap = argparse.ArgumentParser(prog="caddyfile_sites.py declared")
    ap.add_argument("--address", required=True)
    args = ap.parse_args(argv)
    site = find_site(parse_sites(sys.stdin.read()), args.address)
    print(json.dumps({"found": site is not None, **summarize(site["handlers"] if site else [])}))
    return 0


def main() -> int:
    if sys.argv[1:2] == ["running"]:
        return running_main(sys.argv[2:])
    if sys.argv[1:2] == ["declared"]:
        return declared_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    q = sub.add_parser("routes", help="where one site's /v1/* and /health go, from the file")
    q.add_argument("--address", required=True)
    r = sub.add_parser("retire")
    r.add_argument("--address", action="append", required=True)
    r.add_argument("--write", action="store_true", help="edit in place (default: dry run)")
    args = ap.parse_args()

    with open(args.path, encoding="utf-8") as fh:
        text = fh.read()

    if args.cmd == "routes":
        site = find_site(parse_sites(text), args.address)
        found = site is not None
        print(
            json.dumps(
                {
                    "found": found,
                    "managed": bool(found and site["managed"]),
                    **summarize(site["handlers"] if found else []),
                }
            )
        )
        return 0

    if args.cmd == "list":
        print(json.dumps(parse_sites(text), indent=2))
        return 0

    new_text, removed, already_managed = retire(text, args.address)
    if args.write and removed:
        with open(args.path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    print(
        json.dumps(
            {
                "removed": removed,
                "already_managed": already_managed,
                "changed": bool(removed),
                "written": bool(args.write and removed),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
