"""Build and read ufw rules the way ufw itself stores them (apply-firewall.yml, "Convergence").

Building — the ONE place the playbook's rule format lives:

- ufw_desired_rules: every rule the inventory declares, in the order it must be added (SSH
  first, so the allows that keep the runner connected land before anything else), each as
  {cmd, tag, spec, family, is_ssh, dual_family}. `cmd` is the ufw argument list without the comment,
  `tag` the comment naming the rule, `spec` the form `ufw show added` prints it in. A rule
  declared twice (an SSH CIDR also listed as a static 22/tcp rule) appears once.
- ufw_tag (a module function, not an exported filter: no playbook spells a tag):
  `agent-cloud:<family>:<port>/<proto>:<peer>` (`any` for a port-less denial). The
  peer is last because it is the only field that may itself contain ':' (IPv6). The
  builders pass the peer as ufw stores it (ufw_address), so one address spelled two ways
  (`192.0.2.10`, `192.0.2.10/32`) is one rule under one tag.
- ufw_is_ipv4: a single IPv4 address, by stdlib ipaddress (so 999.1.1.1 is not one).
- ufw_egress_problems: why each firewall_deny_egress entry must be refused ([] when none):
  containment against the SSH CIDRs by stdlib ipaddress, so a supernet of the management
  network is refused even when flagged broad.
- ufw_delete_args: the arguments that delete a stored rule by its spec.

Reading — what the prune step must answer from a stored rule, not from its tag:

- ufw_parse_added: `ufw show added` output as [{spec, tag, family, peer}]; `tag` is ''
  unless the rule's comment carries the reserved `agent-cloud:` prefix.
- ufw_absent: the declared rules the host does not hold, by (tag, spec) pair; optionally
  every dual-family rule too, so a lost IP-family half is re-added.
- ufw_masking: stored rules carrying a declared tag over another spec (reported, kept).
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
import re

# The actions that let traffic in; `limit` is an allow with a rate limit (ufw(8)).
_ADMITS = ("allow", "limit")
_LOG = ("log", "log-all")


def ufw_address(addr):
    """The address as ufw stores it; anything unparseable (`any`, a typo) is returned as is.

    The whole address space is printed as `any` (ufw 0.36.2 src/parser.py get_command), so
    `0.0.0.0/0` and `::/0` are spelled `any`; kept literally, a declared rule from either
    would never match its own stored form.
    """
    text = str(addr).strip()
    stored = _stored_address(text)
    return "any" if stored in ("0.0.0.0/0", "::/0") else stored


def _stored_address(text):
    """ufw's normalize_address: host masks dropped, IPv4 masked to its network."""
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
    route = words[:1] == ["route"]
    if route:
        words = words[1:]
    if not words or words[0] not in _ADMITS:
        return False
    words = words[1:]
    if words[:1] == ["out"] and not route:
        return False
    # A route rule may name both interfaces (`in on A out on B`); either direction of a
    # FORWARD allow can carry SSH to a forwarded host, so neither excludes it.
    while words[:1] in (["in"], ["out"]):
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


TAG_PREFIX = "agent-cloud:"
_ADDED = re.compile(r"^ufw (?P<spec>.*?)(?: comment '(?P<comment>[^']*)')?$")


def ufw_is_ipv4(value):
    """True for one IPv4 address in dotted form; a CIDR, a name, or 999.1.1.1 is not."""
    try:
        ipaddress.IPv4Address(str(value).strip())
    except ValueError:
        return False
    return True


def ufw_tag(family, port_proto, peer):
    """The comment naming a rule this playbook adds (apply-firewall.yml, "Convergence")."""
    return f"{TAG_PREFIX}{family}:{port_proto}:{peer}"


def ufw_parse_added(lines):
    """`ufw show added` lines -> [{spec, tag, family, peer}] (src/frontend.py get_show_added, 0.36.2).

    Only lines naming a rule (`ufw ...`) count; the header and "(None)" do not. The spec is
    ufw's own form with the comment stripped, which is what `ufw delete` takes back. `family`
    and `peer` are read from the tag ('' when untagged or the tag has fewer fields); the peer
    is given in the spelling ufw stores (ufw_address), so a tag written by an earlier version
    in the declared spelling (`192.0.2.7/32`) still names the same source.
    """
    out = []
    for line in lines or []:
        match = _ADDED.match(str(line))
        if not match:
            continue
        comment = match.group("comment") or ""
        tag = comment if comment.startswith(TAG_PREFIX) else ""
        fields = tag.split(":", 3)
        family, peer = (fields[1], ufw_address(fields[3])) if len(fields) == 4 else ("", "")
        out.append({"spec": match.group("spec"), "tag": tag, "family": family, "peer": peer})
    return out


def _stored_command(action, src="any", dst="any", port="any", proto="any", iface="", out=False,
                    route=False):
    """src/parser.py get_command, for the rule shapes ufw_desired_rules emits."""
    src, dst = ufw_address(src), ufw_address(dst)
    res = action
    if src == "any" and dst == "any" and not iface and port != "any":
        res += (" out" if out else "") + f" {port}" + (f"/{proto}" if proto != "any" else "")
    else:
        if iface:
            res += f" {'out' if out else 'in'} on {iface}"
        elif out:
            res += " out"
        if src != "any":
            res += f" from {src}"
        if dst != "any" or port != "any":
            res += f" to {dst}" + (f" port {port}" if port != "any" else "")
        if " to " not in res and " from " not in res and not iface:
            res += " to any"
        if proto != "any":
            res += f" proto {proto}"
    return ("route " if route else "") + res


def _names_no_address(spec):
    """No address on either side (`allow in on IFACE ...`, `from any`): ufw stores the rule once
    per IP family, and `show added` prints the two as one line (src/frontend.py
    get_show_added drops a repeated line, 0.36.2), so a lost half is invisible there."""
    words = spec.split()
    return all(words[i + 1] == "any" for i, w in enumerate(words[:-1]) if w in ("from", "to"))


def _rule(family, cmd, port_proto, peer, spec, is_ssh=False):
    # The tag names the peer as ufw stores it, never as declared: `192.0.2.10` and
    # `192.0.2.10/32` are one stored rule, so they must be one tag, or the second add rewrites
    # the first's comment and the drift guard finds a declared tag missing. An interface name
    # (in-on) is not an address and passes through unchanged.
    return {"cmd": cmd, "tag": ufw_tag(family, port_proto, ufw_address(peer)), "spec": spec, "family": family,
            "is_ssh": is_ssh, "dual_family": _names_no_address(spec)}


def _allow_in(peer, port, proto, is_ssh=False):
    return _rule("in", f"allow from {peer} to any port {port} proto {proto}", f"{port}/{proto}", peer,
                 _stored_command("allow", src=peer, port=port, proto=proto), is_ssh)


def _allow_route(peer, port, proto):
    # proto-first: the form verified on the first rootful host (bootstrap hotfix)
    return _rule("route", f"route allow proto {proto} from {peer} to any port {port}", f"{port}/{proto}", peer,
                 _stored_command("allow", src=peer, port=port, proto=proto, route=True))


_BOOL_TRUE = ("y", "yes", "on", "1", "true", "t")
_BOOL_FALSE = ("n", "no", "off", "0", "false", "f")
_EGRESS_PROTOS = ("tcp", "udp", "any")


def _flag(value):
    """True/False for the spellings Ansible's `bool` filter accepts; None for anything else."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return True if text in _BOOL_TRUE else False if text in _BOOL_FALSE else None


def _network(text):
    """The IPv4/IPv6 network an address or CIDR names (host bits masked, as ufw stores it), or
    None when it is not one (`any`, a name, a typo)."""
    try:
        return ipaddress.ip_network(str(text).strip(), strict=False)
    except ValueError:
        return None


def ufw_egress_problems(deny_egress, ssh_cidrs):
    """Every reason a firewall_deny_egress declaration must be refused, one string each; [] when
    it may be applied (apply-firewall.yml, "Validate each egress denial").

    The hazard is a denial BROADER than the management network (docs/MISTAKES.md 2.5): the
    destinations worth denying sit INSIDE firewall_ssh_cidrs, so containment in an SSH CIDR is
    accepted and a denial that contains, or equals, an SSH CIDR is refused. Per entry:

    - `to` is one IPv4/IPv6 address or network. `any` and anything unparseable are refused: a
      denial of the whole address space is not a scoped denial.
    - It names one address (no prefix, /32, /255.255.255.255, /128) unless `broad: true`, and a
      broad entry carries a non-blank `reason`.
    - Broad or not, it is never a supernet of, nor equal to, a declared SSH CIDR. Compared as
      networks with host bits masked, as ufw stores them (src/util.py normalize_address,
      0.36.2), so 192.0.2.1/24 equals 192.0.2.0/24 and 192.0.2.5 equals 192.0.2.5/32.
      Addresses of different families never contain each other.
    - `port`, when the key is present, is one port 1-65535: a null or empty port must not read
      as "no port" and widen a scoped denial to the whole destination. `proto`, when present,
      is tcp, udp or any.

    An SSH CIDR that is not an address cannot be compared, so with any denial declared it is a
    problem too; `any` is the whole address space, which no accepted denial can contain.
    """
    entries = list(deny_egress or [])
    if not entries:
        return []
    problems, ssh = [], []
    for cidr in ssh_cidrs or []:
        if ufw_address(cidr) == "any":
            ssh += [ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")]
        elif (net := _network(cidr)) is None:
            problems.append(f"SSH CIDR {cidr!r} is not an address, so no denial can be proved to leave it reachable")
        else:
            ssh.append(net)
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append(f"{entry!r}: an entry is a mapping with at least `to`")
            continue
        to = entry.get("to")
        net = _network(to) if to is not None and str(to).strip() else None
        if net is None:
            problems.append(f"{to!r}: `to` is required and must be an IPv4/IPv6 address or network")
        broad = _flag(entry.get("broad", False))
        if broad is None:
            problems.append(f"{to!r}: `broad` must be true or false, not {entry['broad']!r}")
        if net is not None and net.num_addresses > 1 and not broad:
            problems.append(f"{to!r}: names more than one address; a wider denial sets `broad: true` and a `reason`")
        if broad and not str(entry.get("reason") or "").strip():
            problems.append(f"{to!r}: `broad: true` requires a `reason`")
        for cidr in ssh:
            if net is not None and net.version == cidr.version and net.supernet_of(cidr):
                problems.append(f"{to!r}: contains or equals the SSH CIDR {cidr}, which would cut this host off "
                                f"the management network; `broad` never permits that")
        if "port" in entry:
            port = str(entry["port"])
            if not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
                problems.append(f"{to!r}: `port` {entry['port']!r} is not one port 1-65535")
        if "proto" in entry and entry["proto"] not in _EGRESS_PROTOS:
            problems.append(f"{to!r}: `proto` {entry['proto']!r} is not tcp, udp or any")
    return problems


def ufw_desired_rules(ssh_cidrs, allow_rules=(), route_rules=(), detected=(), upstreams=(), rootful=False,
                      bridges=(), deny_egress=()):
    """Every declared rule, in add order, once each (see the module docstring).

    ssh_cidrs    firewall_ssh_cidrs: an INPUT 22/tcp allow from each, added FIRST
    allow_rules  static INPUT rules [{port, proto(=tcp), from}] (firewall_allow_rules plus the
                 expanded firewall_allow_groups)
    route_rules  static FORWARD rules, same shape (firewall_route_rules)
    detected     published ports found on the host, one "<port> <proto>" per line; each is
                 allowed from every upstream, and mirrored on FORWARD when `rootful`
    bridges      podman bridge interfaces that get a 53/udp+tcp INPUT allow
    deny_egress  [{to, port(optional), proto(=tcp)}]: `deny out` rules
    """
    rules = [_allow_in(c, 22, "tcp", is_ssh=True) for c in ssh_cidrs or []]
    # `.get(key, default)`, not `or`: like the Jinja `default()` these replace, an explicit null
    # renders as-is and ufw refuses it, instead of being silently read as tcp.
    rules += [_allow_in(r["from"], r["port"], r.get("proto", "tcp")) for r in allow_rules or []]
    rules += [_allow_route(r["from"], r["port"], r.get("proto", "tcp")) for r in route_rules or []]
    for fields in (str(line).split() for line in detected or [] if str(line).strip()):
        port, proto = fields[0], fields[1]
        for src in upstreams or []:
            rules.append(_allow_in(src, port, proto))
            if rootful:
                rules.append(_allow_route(src, port, proto))
    for iface in bridges or []:
        for proto in ("udp", "tcp"):
            rules.append(_rule("in-on", f"allow in on {iface} to any port 53 proto {proto}", f"53/{proto}", iface,
                               _stored_command("allow", port=53, proto=proto, iface=iface)))
    for e in deny_egress or []:
        # A `port` key that is present but null is NOT "no port": it must never widen a scoped
        # denial to the whole destination. The playbook refuses it before this runs; here it
        # renders `port None`, which ufw rejects.
        if "port" in e:
            proto = e.get("proto", "tcp")
            rules.append(_rule("out-deny", f"deny out to {e['to']} port {e['port']} proto {proto}",
                               f"{e['port']}/{proto}", e["to"],
                               _stored_command("deny", dst=e["to"], port=e["port"], proto=proto, out=True)))
        else:
            rules.append(_rule("out-deny", f"deny out to {e['to']}", "any", e["to"],
                               _stored_command("deny", dst=e["to"], out=True)))
    seen, unique = set(), []
    for rule in rules:
        if rule["tag"] not in seen:
            seen.add(rule["tag"])
            unique.append(rule)
    return unique


def ufw_absent(desired, current, reassert_dual_family=False):
    """The desired rules the host does not hold, in order: present means a stored rule with the
    same tag AND the same spec. A tag alone is not enough — a rule carrying a declared tag
    over a different spec (a hand rule under the reserved prefix) must not stand in for the
    declared rule, SSH included. With `reassert_dual_family`, every rule naming no address is
    also returned: the host may hold one IP family of it while `show added` still prints it
    (see _names_no_address), and re-adding it restores the lost half; with both stored, ufw
    only prints "Skipping adding existing rule"."""
    held = {(r["tag"], r["spec"]) for r in current or []}
    return [d for d in desired or []
            if (d["tag"], d["spec"]) not in held or (reassert_dual_family and d["dual_family"])]


def ufw_masking(current, desired):
    """The stored rules that carry a declared tag over a spec that is not that tag's rule. They
    are not counted as the declared rule (ufw_absent), and not pruned either: deleting a
    rule because its spec differs would lean on the computed spec matching ufw's printed form
    exactly, and where it did not, the declared rule itself — an SSH allow included — would be
    deleted. They are reported instead."""
    spec_of = {d["tag"]: d["spec"] for d in desired or []}
    return [r for r in current or [] if r["tag"] in spec_of and r["spec"] != spec_of[r["tag"]]]


def ufw_delete_args(spec):
    """The `ufw` arguments deleting a stored rule by its spec (ufw(8): prefix the rule with
    `delete`; a route rule is `route delete ...`)."""
    return f"route delete {spec[6:]}" if spec.startswith("route ") else f"delete {spec}"


class FilterModule:
    def filters(self):
        return {"ufw_address": ufw_address, "ufw_rule_admits_port": ufw_rule_admits_port,
                "ufw_is_ipv4": ufw_is_ipv4, "ufw_egress_problems": ufw_egress_problems,
                "ufw_parse_added": ufw_parse_added,
                "ufw_desired_rules": ufw_desired_rules, "ufw_absent": ufw_absent,
                "ufw_masking": ufw_masking, "ufw_delete_args": ufw_delete_args}
