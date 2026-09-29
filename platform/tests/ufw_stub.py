"""A stand-in `ufw` (and `podman`) for the apply-firewall.yml convergence harness.

It keeps the firewall in a JSON file and reproduces the ufw behaviour the playbook relies
on, read from the ufw 0.36.2 source (git.launchpad.net/ufw, tag 0.36.2):

- `show added` prints each stored rule in ufw's normalised command form with its comment
  (src/frontend.py get_show_added, src/parser.py UFWCommandRule.get_command), active or not.
- Adding a rule that exists with the same comment prints "Skipping adding existing rule";
  with a different comment it REPLACES the comment ("Rule updated", or "Rules updated" when
  inactive); a new rule prints "Rule added" ("Rules updated" when inactive)
  (src/backend_iptables.py set_rule).
- `delete RULE` matches with or without the comment; a different comment does not match
  ("Could not delete non-existent rule"). Deleted prints "Rule deleted" ("Rules updated"
  when inactive).
- A comment containing "'" is refused (src/parser.py).
- `status verbose` prints "Status: inactive" alone while inactive.
- Addresses are stored normalised (src/util.py normalize_address): a host mask (/32,
  /255.255.255.255, /128) is dropped and an IPv4 CIDR is masked to its network.
- A rule with no address (e.g. `allow in on IFACE ...`) covers both IP families and is
  reported per family; a state entry may carry a third element "v4" or "v6" to say only
  that family is stored, which a delete then reports as "Could not delete non-existent
  rule" for the other (src/frontend.py set_rule, "both").

`ufw delete NUM` and any syntax the playbook is not expected to emit exit non-zero, so a
test fails loudly rather than the stub guessing. Every invocation is appended to the log
file, which is how a test proves a dry run issued no mutating command.

Environment: UFW_STUB_STATE (JSON state file), UFW_STUB_LOG (invocation log),
UFW_STUB_DROP (optional substring: an add whose spec contains it reports "Rule added" but
is not stored, simulating a rule that silently failed to land), UFW_STUB_COLLATERAL
(optional substring: every delete also removes the rules whose spec contains it,
simulating a delete that took more than it named), PODMAN_STUB (JSON:
{"ports": {"<id>": ["8080/tcp -> 0.0.0.0:8080", ...]}, "bridges": {"<net>": "<iface>"}}).
"""

import ipaddress
import json
import os
import sys


def _load(path):
    with open(path) as f:
        return json.load(f)


def _save(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=1)


def _normalise(addr):
    """src/util.py normalize_address, for the address forms the playbook emits."""
    if addr == "any":
        return addr
    host, _, mask = addr.partition("/")
    v6 = ":" in host
    if not mask or mask in (("128",) if v6 else ("32", "255.255.255.255")):
        return str(ipaddress.ip_address(host))
    if v6:
        return f"{ipaddress.ip_address(host)}/{mask}"
    return str(ipaddress.ip_network(addr, strict=False))


def _parse(tokens):
    """[route] [delete] allow|deny [in|out] [on IFACE] ... -> (remove, rule dict)."""
    t = list(tokens)
    rule = {"route": False, "direction": "in", "iface": "", "proto": "any",
            "src": "any", "sport": "any", "dst": "any", "dport": "any", "comment": ""}
    remove = False
    if t and t[0] == "route":
        rule["route"] = True
        t.pop(0)
    if t and t[0] == "delete":
        remove = True
        t.pop(0)
    if not t or t[0] not in ("allow", "deny"):
        raise SystemExit(f"stub ufw: unsupported rule {tokens}")
    rule["action"] = t.pop(0)
    if t and t[0] in ("in", "out"):
        rule["direction"] = t.pop(0)
        if t and t[0] == "on":
            t.pop(0)
            rule["iface"] = t.pop(0)
    last = None
    while t:
        word = t.pop(0)
        if word == "proto":
            rule["proto"] = t.pop(0)
        elif word in ("from", "to"):
            last = "src" if word == "from" else "dst"
            rule[last] = _normalise(t.pop(0))
        elif word == "port" and last:
            rule["sport" if last == "src" else "dport"] = t.pop(0)
        elif word == "comment":
            rule["comment"] = t.pop(0)
            if "'" in rule["comment"]:
                print("ERROR: Comment may not contain \"'\"")
                raise SystemExit(1)
        else:
            raise SystemExit(f"stub ufw: unsupported token {word!r} in {tokens}")
    return remove, rule


def _command(r):
    """src/parser.py get_command, for the shapes the playbook emits."""
    res = r["action"]
    if (r["dst"] == "any" and r["src"] == "any" and r["sport"] == "any" and not r["iface"]
            and r["dport"] != "any"):
        if r["direction"] == "out":
            res += " out"
        res += f" {r['dport']}" + (f"/{r['proto']}" if r["proto"] != "any" else "")
    else:
        if r["iface"]:
            res += f" {r['direction']} on {r['iface']}"
        elif r["direction"] == "out":
            res += " out"
        for loc, port, word in ((r["src"], r["sport"], "from"), (r["dst"], r["dport"], "to")):
            if loc != "any" or port != "any":
                res += f" {word} {loc}" + (f" port {port}" if port != "any" else "")
        if " to " not in res and " from " not in res and not r["iface"]:
            res += " to any"
        if r["proto"] != "any":
            res += f" proto {r['proto']}"
    return ("route " if r["route"] else "") + res


def ufw(argv):
    state_path = os.environ["UFW_STUB_STATE"]
    state = _load(state_path) if os.path.exists(state_path) else {}
    state.setdefault("active", False)
    state.setdefault("incoming", "deny")
    state.setdefault("outgoing", "allow")
    state.setdefault("rules", [])  # [[spec, comment], ...] in insertion order
    active = state["active"]
    if argv == ["show", "added"]:
        print("Added user rules (see 'ufw status' for running firewall):")
        if not state["rules"]:
            print("(None)")
        for spec, comment, *_ in state["rules"]:
            print(f"ufw {spec}" + (f" comment '{comment}'" if comment else ""))
        return 0
    if argv[:1] == ["status"]:
        if argv[1:] not in ([], ["verbose"]):
            raise SystemExit(f"stub ufw: unsupported {argv}")
        if not active:
            print("Status: inactive")
            return 0
        print("Status: active")
        if argv[1:] == ["verbose"]:
            print("Logging: on (low)")
            print(f"Default: {state['incoming']} (incoming), {state['outgoing']} (outgoing), deny (routed)")
            print("New profiles: skip")
        print("")
        for spec, comment, *_ in state["rules"]:
            print(f"{spec}" + (f"  # {comment}" if comment else ""))
        return 0
    if argv[:1] == ["default"] and len(argv) == 3:
        policy, direction = argv[1], argv[2]
        state["incoming" if direction == "incoming" else "outgoing"] = policy
        _save(state_path, state)
        print(f"Default {direction} policy changed to '{policy}'")
        print("(be sure to update your rules accordingly)")
        return 0
    if argv == ["--force", "enable"]:
        state["active"] = True
        _save(state_path, state)
        print("Firewall is active and enabled on system startup")
        return 0
    if len(argv) == 2 and argv[0] == "delete" and argv[1].isdigit():
        raise SystemExit("stub ufw: delete by NUMBER is not supported by design")
    remove, rule = _parse(argv)
    spec = _command(rule)
    both = rule["src"] == "any" and rule["dst"] == "any"

    def say(message, families=("v4", "v6")):
        # One line per IP family the rule covers: "(v6)" marks the second (frontend.set_rule).
        lines = [message(fam) + (" (v6)" if fam == "v6" else "") for fam in (families if both else ("v4",))]
        print("\n".join(lines))

    index = next((i for i, r in enumerate(state["rules"]) if r[0] == spec), None)
    if remove:
        if index is None or (rule["comment"] and state["rules"][index][1] != rule["comment"]):
            say(lambda fam: "Could not delete non-existent rule")
            return 0
        stored = state["rules"][index][2:] or ["v4", "v6"]
        del state["rules"][index]
        collateral = os.environ.get("UFW_STUB_COLLATERAL")
        if collateral:
            state["rules"] = [r for r in state["rules"] if collateral not in r[0]]
        _save(state_path, state)
        say(lambda fam: ("Rule deleted" if active else "Rules updated") if fam in stored
            else "Could not delete non-existent rule")
        return 0
    if index is not None:
        if state["rules"][index][1] == rule["comment"]:
            say(lambda fam: "Skipping adding existing rule")
            return 0
        state["rules"][index][1] = rule["comment"]
        _save(state_path, state)
        say(lambda fam: "Rule updated" if active else "Rules updated")
        return 0
    drop = os.environ.get("UFW_STUB_DROP")
    if not (drop and drop in spec):
        state["rules"].append([spec, rule["comment"]])
        _save(state_path, state)
    say(lambda fam: "Rule added" if active else "Rules updated")
    return 0


def podman(argv):
    fixture = _load(os.environ["PODMAN_STUB"]) if os.environ.get("PODMAN_STUB") else {}
    ports, bridges = fixture.get("ports", {}), fixture.get("bridges", {})
    if argv == ["ps", "-q"]:
        print("\n".join(ports))
    elif argv[:1] == ["port"] and len(argv) == 2:
        print("\n".join(ports.get(argv[1], [])))
    elif argv == ["network", "ls", "-q"]:
        print("\n".join(bridges))
    elif argv[:2] == ["network", "inspect"] and len(argv) == 3:
        print(json.dumps([{"name": argv[2], "network_interface": bridges[argv[2]]}], indent=1))
    else:
        raise SystemExit(f"stub podman: unsupported {argv}")
    return 0


def main():
    name = os.environ.get("STUB_AS", "ufw")
    with open(os.environ["UFW_STUB_LOG"], "a") as log:
        log.write(json.dumps([name, *sys.argv[1:]]) + "\n")
    return podman(sys.argv[1:]) if name == "podman" else ufw(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
