"""Add ONE volume line to ONE service of a hand-maintained compose file, keeping everything else.

    compose_add_volume.py <service> <volume>   (the file's text on stdin)

Prints JSON: {"changed": bool, "text": <new text>} or exits non-zero with the reason.

A line edit, not a YAML round-trip, so the operator's comments and layout survive. It is
refused unless the result parses to exactly the original plus that one volume, so a layout
it does not understand can never be rewritten into something else. Already present: no
change. Another volume at the same container path: refused, never replaced.
"""

import json
import re
import sys

import yaml


def target(volume: str) -> str:
    # short syntax: source:target[:mode]
    return volume.split(":")[1] if ":" in volume else volume


def add(text: str, service: str, volume: str) -> tuple[bool, str]:
    old = yaml.safe_load(text) or {}
    svc = (old.get("services") or {}).get(service)
    if not isinstance(svc, dict):
        raise SystemExit(f"no service {service!r} in the compose file")
    vols = svc.get("volumes")
    if not isinstance(vols, list) or not all(isinstance(v, str) for v in vols):
        raise SystemExit(f"service {service!r} has no short-syntax volumes list to extend")
    if volume in vols:
        return False, text
    clash = [v for v in vols if target(v) == target(volume)]
    if clash:
        raise SystemExit(f"service {service!r} already mounts {clash[0]!r} at {target(volume)}")

    lines = text.splitlines(keepends=True)
    svc_re = re.compile(r"^(\s+)" + re.escape(service) + r":\s*(#.*)?$")
    start = next((i for i, ln in enumerate(lines) if svc_re.match(ln)), None)
    if start is None:
        raise SystemExit(f"service {service!r} is not a plain block key")
    svc_indent = len(svc_re.match(lines[start]).group(1))
    vol_at, last_item, item_indent = None, None, None
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent <= svc_indent:
            break  # the next service, or the end of services
        if vol_at is None:
            if re.match(r"^\s+volumes:\s*(#.*)?$", ln):
                vol_at = i
            continue
        if ln.lstrip().startswith("- ") and indent > svc_indent:
            if item_indent is None:
                item_indent = indent
            if indent == item_indent:
                last_item = i
                continue
        if indent <= len(lines[vol_at]) - len(lines[vol_at].lstrip()):
            break  # the key after volumes
    if last_item is None:
        raise SystemExit(f"service {service!r} has no block volumes list to extend")
    nl = "\n" if not lines[last_item].endswith("\n") else ""
    lines.insert(last_item + 1, nl + " " * item_indent + "- " + volume + "\n")
    new_text = "".join(lines)

    new = yaml.safe_load(new_text)
    want = json.loads(json.dumps(old))
    want["services"][service]["volumes"] = vols + [volume]
    if new != want:
        raise SystemExit("the edit would change more than the one volume; refusing")
    return True, new_text


if __name__ == "__main__":
    changed, out = add(sys.stdin.read(), sys.argv[1], sys.argv[2])
    print(json.dumps({"changed": changed, "text": out}))
