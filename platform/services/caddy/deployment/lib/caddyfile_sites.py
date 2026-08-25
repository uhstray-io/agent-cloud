"""Read and edit the site blocks of a flat Caddyfile.

WHY THIS EXISTS. Routes on the central Caddy arrived by hand, one block at a
time, and the file became the only record of them. That is how
one platform route came to point at an address declared for a *different* host,
and kept pointing there after the two machines started answering one IP. Nothing
compared the route to the declaration, because nothing could read the routes.

So this does two things, and only two:

  list   — report every site block: its addresses, its upstreams, and whether it
           sits inside the ANSIBLE MANAGED region. That is the "read" half, and
           it is what makes drift between Caddy and the inventory visible.
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
"""

from __future__ import annotations

import argparse
import json
import re
import sys

MANAGED_BEGIN = "# BEGIN ANSIBLE MANAGED"
MANAGED_END = "# END ANSIBLE MANAGED"

# `respond <<HTML` ... a line that is exactly `HTML` ends it.
_HEREDOC = re.compile(r"(?<!\\)<<([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _split_addresses(header: str) -> list[str]:
    """Addresses may be separated by commas OR whitespace, per the docs:
    `localhost:8080, example.com` and `localhost:8080 example.com` are both one
    site with two addresses. Splitting on commas alone yielded the single
    address "a.example.io b.example.io", which no lookup could ever match."""
    return [a for a in re.split(r"[,\s]+", header.strip()) if a]


def _strip_comment(line: str) -> str:
    """Drop a trailing comment, per the documented rule.

    The Caddyfile spec is positional, not quote-based: "The hash character `#`
    for a comment cannot appear in the middle of a token (i.e. it must be
    preceded by a space or appear at the beginning of a line)." That is
    precisely so a `#` inside a URI needs no quoting.

    An earlier version here tracked quotes instead, which truncated
    `reverse_proxy http://host/#frag` to `http://host/` — reporting an upstream
    the server does not use.
    """
    if line.lstrip().startswith("#"):
        return ""
    for i, ch in enumerate(line):
        if ch == "#" and i > 0 and line[i - 1] in " \t":
            return line[:i]
    return line


# A site header is an address list; these column-0 blocks are not sites.
#   (name) {   snippet, invoked with `import`
#   &(name) {  named route, invoked with `invoke`
def _is_site_header(header: str) -> bool:
    return not header.startswith(("(", "&("))


def parse_sites(text: str) -> list[dict]:
    """Return one entry per site block, in file order."""
    sites: list[dict] = []
    depth = 0
    managed = False
    cur: dict | None = None
    heredoc: str | None = None

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
                    "managed": managed,
                }

        if cur is not None and "reverse_proxy" in stripped:
            parts = stripped.split()
            # `reverse_proxy [matcher] <upstream...> [{`  — take the tokens that
            # look like upstreams, skipping a matcher and a trailing brace.
            for tok in parts[1:]:
                if tok in ("{", "}") or tok.startswith(("@", "/")):
                    continue
                cur["upstreams"].append(tok)

        depth += code.count("{") - code.count("}")

        if cur is not None and depth == 0 and idx >= cur["start"]:
            cur["end"] = idx
            sites.append(cur)
            cur = None

        if MANAGED_END in raw:
            managed = False

    return sites


def find_site(sites: list[dict], address: str) -> dict | None:
    for s in sites:
        if address in s["addresses"]:
            return s
    return None


def retire(text: str, addresses: list[str]) -> tuple[str, list[str]]:
    """Remove hand-maintained blocks for `addresses`. Returns (new_text, removed).

    Refuses to touch a block inside the managed region: that region is rewritten
    wholesale by blockinfile, so deleting from it would be undone on the next run
    and would mask the fact that the caller asked for the wrong thing.
    """
    sites = parse_sites(text)
    lines = text.splitlines(keepends=True)
    drop: set[int] = set()
    removed: list[str] = []

    for addr in addresses:
        site = find_site(sites, addr)
        if site is None:
            continue
        if site["managed"]:
            raise SystemExit(
                f"refusing to retire '{addr}': it is inside the ANSIBLE MANAGED "
                "region, which is rewritten from inventory on every run. Remove "
                "it from caddy_managed_sites instead."
            )
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
    return "".join(out), removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("retire")
    r.add_argument("--address", action="append", required=True)
    r.add_argument("--write", action="store_true", help="edit in place (default: dry run)")
    args = ap.parse_args()

    with open(args.path, encoding="utf-8") as fh:
        text = fh.read()

    if args.cmd == "list":
        print(json.dumps(parse_sites(text), indent=2))
        return 0

    new_text, removed = retire(text, args.address)
    if args.write and removed:
        with open(args.path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    print(json.dumps({"removed": removed, "changed": bool(removed), "written": bool(args.write and removed)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
