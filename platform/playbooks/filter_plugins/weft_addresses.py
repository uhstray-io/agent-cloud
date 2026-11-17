"""Why a weft host's published address or trusted peers must be refused (tasks/weft-bind.yml).

The board and the front door run on pasta, which keeps a connection's source address (weft #580),
so who they believe is decided by addresses, and those are compared here as addresses, by stdlib
ipaddress, never as text: `::0`, `0::1` and `::ffff:7f00:1` are the wildcard and loopback too.

- weft_bind_problems: a published address that is a wildcard or loopback, under any spelling, or a
  numeric spelling ipaddress cannot read (`127.1`, which inet_aton reads as loopback). A host name
  other than localhost passes: it is not an address to judge.
- weft_peer_problems: trusted_proxies entries wider than one address, and trusted_proxies or
  ingest_from entries holding an address a process on the VM itself arrives as. Under pasta that
  is the VM's default gateway (podman 4.9.3) or 169.254.1.2 (podman 5.3 on), so either in a list
  of believed peers believes everything local to the VM.

Each returns one sentence per problem, [] when there is none. Tested directly by
platform/tests/test_weft_addresses_filter.py.
"""

import ipaddress
import re

# What pasta reports for a connection from the VM itself from podman 5.3 on (its --map-guest-addr
# default). On 4.9.3 it is the default gateway, which the caller passes.
PASTA_GUEST_ADDRESS = "169.254.1.2"

# Text ipaddress refuses but a C resolver may still read as an address (inet_aton: `127.1`,
# `0x7f.1`, `2130706433`), or a colon form ipaddress refuses. Never passed as a host name.
_NUMERIC = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*\.?$|:")


def _address(text):
    """The address `text` names (brackets and a zone dropped), or None when it names none."""
    bare = str(text).strip().lower()
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1]
    try:
        return ipaddress.ip_address(bare)
    except ValueError:
        return None


def _unmapped(ip):
    """`::ffff:a.b.c.d` is the IPv4 address a.b.c.d to a dual-stack socket, so it is judged as one."""
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip


def weft_bind_problems(bind, name="bind"):
    """Why `bind` may not be the address the board or the front door is published on ([] if none)."""
    text = str(bind if bind is not None else "").strip()
    if not text:
        return [f"{name} is empty"]
    ip = _address(text)
    if ip is None:
        lowered = text.lower().rstrip(".")
        if lowered == "localhost" or lowered.endswith(".localhost"):
            return [f"{name} {text!r} is loopback"]
        if _NUMERIC.search(lowered.strip("[]")):
            return [f"{name} {text!r} is not an address in a form that can be checked"]
        return []
    ip = _unmapped(ip)
    if ip.is_unspecified:
        return [f"{name} {text!r} is a wildcard ({ip})"]
    if ip.is_loopback:
        return [f"{name} {text!r} is loopback ({ip})"]
    return []


def _block(text):
    """The network a CIDR block names, an IPv4-mapped one as IPv4, or None when weft cannot read it.

    Read as client-address's Allowlist::parse reads it, so nothing passes here that weft would
    refuse only after the stop: `<address>/<bits>`, nothing trimmed, the address with no zone id
    (`fe80::1%eth0` is not an IpAddr), the bits plain digits (no netmask) no wider than the family.
    A mapped block of 96 bits or more weft reads as IPv4, so it is compared as one; a shorter one
    weft refuses."""
    addr, slash, bits = str(text).partition("/")
    if not slash or "%" in addr or not (bits.isascii() and bits.isdigit()):
        return None
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    if int(bits) > ip.max_prefixlen:
        return None
    mapped = ip.ipv4_mapped if ip.version == 6 else None
    if mapped is not None:
        if int(bits) < 96:
            return None
        return ipaddress.ip_network((mapped, int(bits) - 96), strict=False)
    return ipaddress.ip_network((ip, int(bits)), strict=False)


def _local_peers(gateways):
    peers = []
    for text in [*(gateways or []), PASTA_GUEST_ADDRESS]:
        ip = _address(text) if text else None
        if ip is not None and _unmapped(ip) not in peers:
            peers.append(_unmapped(ip))
    return peers


def _holds(net, peers):
    return [str(p) for p in peers if p.version == net.version and p in net]


def weft_peer_problems(trusted_proxies, ingest_from=(), gateways=(), names=("trusted_proxies", "ingest_from"),
                       trimmed=False):
    """Why these believed peers must be refused ([] if none).

    trusted_proxies  every entry one address, written as a /32 or /128 block (weft reads only
                     blocks), and none of them a local peer: each may name any client it likes.
    ingest_from      no entry holding a local peer: one would admit every process on the VM.
    gateways         the VM's default gateway(s); 169.254.1.2 is always a local peer.
    trimmed          the lists reach weft trimmed and with blank entries dropped (door.env.j2).
                     Otherwise they reach it as written (weft.toml.j2 renders them with to_json)
                     and are judged untrimmed. weft does not trim ingest_from, so a blank or padded
                     entry there is one weft refuses. It does trim the board's trusted_proxies, so a
                     blank entry there is refused by weft too, and a padded one is refused here only
                     because this is stricter than weft.
    Every entry weft could not read is refused, so it is refused here and not after the stop.
    """
    peers = _local_peers(gateways)
    problems = []
    for name, entries, single in ((names[0], trusted_proxies, True), (names[1], ingest_from, False)):
        if isinstance(entries, str):
            # Judged as the one entry it names, not character by character. Whether a string may
            # stand for the list is weft-door-header.yml's refusal, not this one.
            entries = [entries]
        for entry in entries or []:
            text = str(entry).strip() if trimmed else str(entry)
            if trimmed and not text:
                continue  # dropped when rendered, so nothing is believed from it
            net = _block(text)
            if net is None:
                problems.append(f"{name} entry {entry!r} is not a CIDR block weft can read")
                continue
            if single and net.num_addresses != 1:
                problems.append(f"{name} entry {entry!r} is wider than one address; name the Caddy host "
                                f"alone, as a /32 (or /128)")
            for peer in _holds(net, peers):
                problems.append(f"{name} entry {entry!r} holds {peer}, the address every process on this VM "
                                f"arrives as under pasta")
    return problems


class FilterModule:
    def filters(self):
        return {"weft_bind_problems": weft_bind_problems, "weft_peer_problems": weft_peer_problems}
