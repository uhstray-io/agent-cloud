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
import sys

MANAGED_BEGIN = "# BEGIN ANSIBLE MANAGED"
MANAGED_END = "# END ANSIBLE MANAGED"


def _strip_comment(line: str) -> str:
    """Drop a trailing comment.

    A '#' inside a quoted string would be mis-read as a comment. No block in this
    Caddyfile has one, and treating the rare case as a comment fails toward
    *not* matching a site header, which is the safe direction: the block is left
    alone rather than silently retired.
    """
    out, in_quote = [], False
    for ch in line:
        if ch == '"':
            in_quote = not in_quote
        if ch == "#" and not in_quote:
            break
        out.append(ch)
    return "".join(out)


def parse_sites(text: str) -> list[dict]:
    """Return one entry per site block, in file order."""
    sites: list[dict] = []
    depth = 0
    managed = False
    cur: dict | None = None

    for idx, raw in enumerate(text.splitlines()):
        if MANAGED_BEGIN in raw:
            managed = True
        code = _strip_comment(raw)
        stripped = code.strip()

        if depth == 0 and stripped.endswith("{") and not raw[:1].isspace():
            header = stripped[:-1].strip()
            if header:  # a bare '{' at column 0 is the global options block
                cur = {
                    "addresses": [a.strip() for a in header.split(",") if a.strip()],
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
