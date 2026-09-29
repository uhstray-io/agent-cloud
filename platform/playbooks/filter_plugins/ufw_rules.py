"""Read ufw rules the way ufw itself stores them (apply-firewall.yml, "Convergence").

Two questions the prune step must answer from a stored rule, not from its tag:

- ufw_address: the spelling ufw stores for an address, so a declared CIDR can be compared
  with what `ufw show added` prints. ufw drops a host mask (/32, /255.255.255.255, /128),
  turns a dotted netmask into a prefix, and masks an IPv4 CIDR to its network
  (src/util.py normalize_address, ufw 0.36.2). IPv6 is only given its compressed form.
- ufw_rule_admits_port: whether a stored rule spec (a `ufw show added` line without the
  leading `ufw ` and without its comment) admits inbound traffic to a port, e.g. SSH. The
  printed form omits what is "any" (src/parser.py get_command: no `proto` clause for proto
  any, no `port` clause for every port), so absence means ALL, never none.

Tested directly by platform/tests/test_ufw_rules_filter.py.
"""

import ipaddress

# The actions that let traffic in; `limit` is an allow with a rate limit (ufw(8)).
_ADMITS = ("allow", "limit")
_LOG = ("log", "log-all")


def ufw_address(addr):
    """The address as ufw stores it; anything unparseable (`any`, a typo) is returned as is."""
    text = str(addr).strip()
    host, _, mask = text.partition("/")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return text
    if not mask or mask in (("128",) if ip.version == 6 else ("32", "255.255.255.255")):
        return str(ip)
    if ip.version == 6:
        return f"{ip}/{mask}"
    try:
        return str(ipaddress.ip_network(text, strict=False))
    except ValueError:
        return text


def _covers(ports, port):
    """A ufw port list ("22", "22,2222", "20:30", "any") includes `port`. Unparseable: True."""
    if ports == "any":
        return True
    for item in ports.split(","):
        low, _, high = item.partition(":")
        try:
            if int(low) <= port <= int(high or low):
                return True
        except ValueError:
            return True  # a service name or app: cannot rule it out
    return False


def ufw_rule_admits_port(spec, port=22, protocols=("tcp",)):
    """True when the rule lets NEW inbound traffic reach `port` over one of `protocols`.

    Counted: allow/limit rules on INPUT (with or without `in on IFACE`) and route (FORWARD)
    rules — a route allow can carry SSH to a forwarded or containerised host, so deleting one
    is treated like deleting a host SSH allow. Not counted: deny/reject (deleting a denial
    never removes access) and `out` rules (egress, not an inbound path). A rule this cannot
    read (an application profile, an unknown token) counts as admitting: the caller refuses
    to delete it unguarded rather than guess.
    """
    words = str(spec).split()
    if words[:1] == ["route"]:
        words = words[1:]
    if not words or words[0] not in _ADMITS:
        return False
    words = words[1:]
    if words[:1] == ["out"]:
        return False
    if words[:1] == ["in"]:
        words = words[3:] if words[1:2] == ["on"] else words[1:]
    words = [w for w in words if w not in _LOG]
    proto, dports = "any", "any"
    if words and words[0] not in ("from", "to", "proto"):
        # Short form: PORT[/PROTO], or an application profile name.
        head, _, proto = words[0].partition("/")
        proto = proto or "any"
        if not head[:1].isdigit():
            return True
        dports = head
    else:
        side = None
        it = iter(words)
        for word in it:
            if word in ("from", "to"):
                side = word
                next(it, None)
            elif word == "proto":
                proto = next(it, "any")
            elif word == "port" and side == "to":
                dports = next(it, "any")
            elif word == "app" and side == "to":
                return True
            elif word in ("port", "app"):
                next(it, None)  # a source port or app does not narrow the destination
            else:
                return True
    return proto in (*protocols, "any") and _covers(dports, int(port))


class FilterModule:
    def filters(self):
        return {"ufw_address": ufw_address, "ufw_rule_admits_port": ufw_rule_admits_port}
