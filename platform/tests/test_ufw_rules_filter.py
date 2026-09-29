"""The ufw rule builders and readers (filter_plugins/ufw_rules.py), tested directly.

test_apply_firewall_convergence.py proves apply-firewall.yml wires them into the adds and
the prune guard; the command forms, tags and spellings live here, as function calls
instead of playbook runs. Specs are in
the form `ufw show added` prints (ufw 0.36.2 src/parser.py get_command), without the
leading `ufw ` and the comment.
"""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "ufw_rules", Path(__file__).resolve().parents[1] / "playbooks/filter_plugins/ufw_rules.py")
rules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rules)


@pytest.mark.parametrize("given,stored", [
    ("192.0.2.5", "192.0.2.5"), ("192.0.2.5/32", "192.0.2.5"), ("192.0.2.5/255.255.255.255", "192.0.2.5"),
    ("192.0.2.77/24", "192.0.2.0/24"), ("192.0.2.0/24", "192.0.2.0/24"),
    ("192.0.2.9/255.255.255.0", "192.0.2.0/24"),
    ("2001:db8::1/128", "2001:db8::1"), ("2001:0db8::0001", "2001:db8::1"), ("2001:db8::/32", "2001:db8::/32"),
    ("any", "any"), (" 192.0.2.5 ", "192.0.2.5"), ("not-an-address", "not-an-address"),
    ("0.0.0.0/0", "any"), ("::/0", "any"), ("0.0.0.0/0.0.0.0", "any"), ("0.0.0.5/0", "any"),
    ("::1/0", "::1/0"),
])
def test_an_address_is_spelled_as_ufw_stores_it(given, stored):
    assert rules.ufw_address(given) == stored


@pytest.mark.parametrize("spec", [
    "allow from 192.0.2.0/24 to any port 22 proto tcp",
    "allow from 192.0.2.0/24 to any port 22",             # proto any: ufw prints no proto
    "allow from 192.0.2.0/24 to any port 22,2222 proto tcp",
    "allow from 192.0.2.0/24 to any port 2222,22 proto tcp",
    "allow from 192.0.2.0/24 to any port 20:30 proto tcp",
    "allow from 192.0.2.0/24",                            # no port clause: every port
    "allow from 192.0.2.0/24 proto tcp",
    "allow from 192.0.2.0/24 port 5000 to any port 22 proto tcp",  # a source port narrows nothing
    "allow 22/tcp", "allow 22", "allow 22,80/tcp", "allow 1:1024/tcp",
    "limit 22/tcp", "limit from 192.0.2.0/24 to any port 22 proto tcp",
    "allow in on eth0 to any port 22 proto tcp", "allow in to any port 22 proto tcp",
    "allow log from 192.0.2.0/24 to any port 22 proto tcp",
    "route allow from 192.0.2.0/24 to any port 22 proto tcp", "route allow from 192.0.2.0/24",
    "route allow out on eth1 to 192.0.2.164 port 22 proto tcp",
    "route allow in on eth0 out on eth1 to 192.0.2.164 port 22 proto tcp",
    "allow OpenSSH", "allow from 192.0.2.0/24 to any app OpenSSH",  # unreadable: counts
])
def test_a_rule_that_admits_ssh_is_recognised(spec):
    assert rules.ufw_rule_admits_port(spec, 22) is True


@pytest.mark.parametrize("spec", [
    "allow from 192.0.2.0/24 to any port 22 proto udp",
    "allow 22/udp",
    "allow from 192.0.2.0/24 to any port 2222 proto tcp",
    "allow from 192.0.2.0/24 to any port 23:30 proto tcp",
    "allow from 192.0.2.0/24 to any port 80,443 proto tcp",
    "allow from 192.0.2.0/24 proto udp",
    "allow in on podman1 to any port 53 proto udp",
    "deny from 192.0.2.0/24 to any port 22 proto tcp",
    "deny out to 192.0.2.164 port 22 proto tcp",
    "allow out to 192.0.2.164 port 22 proto tcp",
    "route deny from 192.0.2.0/24 to any port 22 proto tcp",
])
def test_a_rule_that_does_not_admit_ssh_is_not(spec):
    assert rules.ufw_rule_admits_port(spec, 22) is False


@pytest.mark.parametrize("value,ok", [
    ("192.0.2.10", True), (" 192.0.2.10 ", True), ("0.0.0.0", True),
    ("999.1.1.1", False), ("192.0.2.256", False), ("192.0.2", False), ("192.0.2.10/32", False),
    ("2001:db8::1", False), ("dns.example.test", False), ("", False), (None, False),
])
def test_only_a_single_ipv4_address_is_one(value, ok):
    assert rules.ufw_is_ipv4(value) is ok


def test_show_added_is_read_into_spec_and_tag():
    lines = [
        "Added user rules (see 'ufw status' for running firewall):",
        "ufw allow from 192.0.2.0/24 to any port 22 proto tcp comment 'agent-cloud:in:22/tcp:192.0.2.0/24'",
        "ufw route allow from 192.0.2.7 to any port 443 proto tcp comment 'agent-cloud:route:443/tcp:192.0.2.7'",
        "ufw allow from 2001:db8::1 to any port 80 proto tcp comment 'agent-cloud:in:80/tcp:2001:db8::1'",
        "ufw allow 8443/tcp",
        "ufw allow from 203.0.113.9 to any port 5432 proto tcp comment 'set by hand'",
        "ufw deny out to 198.51.100.2 comment 'agent-cloud:legacy'",
        "(None)",
    ]
    assert rules.ufw_parse_added(lines) == [
        {"spec": "allow from 192.0.2.0/24 to any port 22 proto tcp", "tag": "agent-cloud:in:22/tcp:192.0.2.0/24",
         "family": "in", "peer": "192.0.2.0/24"},
        {"spec": "route allow from 192.0.2.7 to any port 443 proto tcp",
         "tag": "agent-cloud:route:443/tcp:192.0.2.7", "family": "route", "peer": "192.0.2.7"},
        # the peer is the last field, so an IPv6 peer keeps its colons
        {"spec": "allow from 2001:db8::1 to any port 80 proto tcp", "tag": "agent-cloud:in:80/tcp:2001:db8::1",
         "family": "in", "peer": "2001:db8::1"},
        {"spec": "allow 8443/tcp", "tag": "", "family": "", "peer": ""},
        # a hand comment is not a tag; its spec still loses the comment
        {"spec": "allow from 203.0.113.9 to any port 5432 proto tcp", "tag": "", "family": "", "peer": ""},
        # a reserved-prefix comment IS a tag (it is pruned when undeclared), whatever its shape
        {"spec": "deny out to 198.51.100.2", "tag": "agent-cloud:legacy", "family": "", "peer": ""},
    ]


def test_the_tag_names_the_rule_with_the_peer_last():
    assert rules.ufw_tag("in", "22/tcp", "192.0.2.0/24") == "agent-cloud:in:22/tcp:192.0.2.0/24"
    assert rules.ufw_tag("out-deny", "any", "198.51.100.2") == "agent-cloud:out-deny:any:198.51.100.2"


def _desired(**kwargs):
    return [(r["cmd"], r["tag"], r["spec"]) for r in rules.ufw_desired_rules(["192.0.2.0/24"], **kwargs)]


def test_every_declared_rule_kind_has_its_command_tag_and_stored_spec_in_add_order():
    got = rules.ufw_desired_rules(
        ["192.0.2.0/24", "198.51.100.77/24"],
        allow_rules=[{"port": 8080, "from": "192.0.2.5/32"}, {"port": 53, "proto": "udp", "from": "192.0.2.10"}],
        route_rules=[{"port": 9000, "from": "192.0.2.6"}],
        detected=["443 tcp", "", "53 udp"], upstreams=["192.0.2.7", "192.0.2.8"], rootful=True,
        bridges=["podman1"],
        deny_egress=[{"to": "198.51.100.1", "port": 8200}, {"to": "198.51.100.2", "port": 53, "proto": "udp"},
                     {"to": "198.51.100.3"}])
    assert [(r["cmd"], r["tag"], r["spec"], r["family"], r["is_ssh"]) for r in got] == [
        # SSH first: these must land before any other rule and before enable (anti-lockout)
        ("allow from 192.0.2.0/24 to any port 22 proto tcp", "agent-cloud:in:22/tcp:192.0.2.0/24",
         "allow from 192.0.2.0/24 to any port 22 proto tcp", "in", True),
        # the command and tag keep the declared spelling; the spec is ufw's stored one
        ("allow from 198.51.100.77/24 to any port 22 proto tcp", "agent-cloud:in:22/tcp:198.51.100.77/24",
         "allow from 198.51.100.0/24 to any port 22 proto tcp", "in", True),
        ("allow from 192.0.2.5/32 to any port 8080 proto tcp", "agent-cloud:in:8080/tcp:192.0.2.5/32",
         "allow from 192.0.2.5 to any port 8080 proto tcp", "in", False),
        ("allow from 192.0.2.10 to any port 53 proto udp", "agent-cloud:in:53/udp:192.0.2.10",
         "allow from 192.0.2.10 to any port 53 proto udp", "in", False),
        # FORWARD rules proto-first, the form verified on the first rootful host
        ("route allow proto tcp from 192.0.2.6 to any port 9000", "agent-cloud:route:9000/tcp:192.0.2.6",
         "route allow from 192.0.2.6 to any port 9000 proto tcp", "route", False),
        # each detected port from every upstream, mirrored on FORWARD because rootful
        ("allow from 192.0.2.7 to any port 443 proto tcp", "agent-cloud:in:443/tcp:192.0.2.7",
         "allow from 192.0.2.7 to any port 443 proto tcp", "in", False),
        ("route allow proto tcp from 192.0.2.7 to any port 443", "agent-cloud:route:443/tcp:192.0.2.7",
         "route allow from 192.0.2.7 to any port 443 proto tcp", "route", False),
        ("allow from 192.0.2.8 to any port 443 proto tcp", "agent-cloud:in:443/tcp:192.0.2.8",
         "allow from 192.0.2.8 to any port 443 proto tcp", "in", False),
        ("route allow proto tcp from 192.0.2.8 to any port 443", "agent-cloud:route:443/tcp:192.0.2.8",
         "route allow from 192.0.2.8 to any port 443 proto tcp", "route", False),
        ("allow from 192.0.2.7 to any port 53 proto udp", "agent-cloud:in:53/udp:192.0.2.7",
         "allow from 192.0.2.7 to any port 53 proto udp", "in", False),
        ("route allow proto udp from 192.0.2.7 to any port 53", "agent-cloud:route:53/udp:192.0.2.7",
         "route allow from 192.0.2.7 to any port 53 proto udp", "route", False),
        ("allow from 192.0.2.8 to any port 53 proto udp", "agent-cloud:in:53/udp:192.0.2.8",
         "allow from 192.0.2.8 to any port 53 proto udp", "in", False),
        ("route allow proto udp from 192.0.2.8 to any port 53", "agent-cloud:route:53/udp:192.0.2.8",
         "route allow from 192.0.2.8 to any port 53 proto udp", "route", False),
        # bridge DNS: port 53 only, never the whole bridge
        ("allow in on podman1 to any port 53 proto udp", "agent-cloud:in-on:53/udp:podman1",
         "allow in on podman1 to any port 53 proto udp", "in-on", False),
        ("allow in on podman1 to any port 53 proto tcp", "agent-cloud:in-on:53/tcp:podman1",
         "allow in on podman1 to any port 53 proto tcp", "in-on", False),
        ("deny out to 198.51.100.1 port 8200 proto tcp", "agent-cloud:out-deny:8200/tcp:198.51.100.1",
         "deny out to 198.51.100.1 port 8200 proto tcp", "out-deny", False),
        ("deny out to 198.51.100.2 port 53 proto udp", "agent-cloud:out-deny:53/udp:198.51.100.2",
         "deny out to 198.51.100.2 port 53 proto udp", "out-deny", False),
        # no port: the destination is denied entirely
        ("deny out to 198.51.100.3", "agent-cloud:out-deny:any:198.51.100.3", "deny out to 198.51.100.3",
         "out-deny", False),
    ]


def test_detected_ports_get_no_forward_mirror_on_a_rootless_host():
    assert _desired(detected=["443 tcp"], upstreams=["192.0.2.7"]) == [
        ("allow from 192.0.2.0/24 to any port 22 proto tcp", "agent-cloud:in:22/tcp:192.0.2.0/24",
         "allow from 192.0.2.0/24 to any port 22 proto tcp"),
        ("allow from 192.0.2.7 to any port 443 proto tcp", "agent-cloud:in:443/tcp:192.0.2.7",
         "allow from 192.0.2.7 to any port 443 proto tcp"),
    ]


def test_nothing_declared_beyond_ssh_yields_only_the_ssh_allows():
    # The regression guard for every host that declares no extras: no stray rule appears.
    assert [c for c, _, _ in _desired()] == ["allow from 192.0.2.0/24 to any port 22 proto tcp"]
    # detected ports without an upstream are refused before this runs; none here, no rule
    assert [c for c, _, _ in _desired(detected=["443 tcp"])] == ["allow from 192.0.2.0/24 to any port 22 proto tcp"]


def test_a_rule_declared_twice_appears_once_and_keeps_its_first_place():
    got = rules.ufw_desired_rules(["192.0.2.0/24"], allow_rules=[{"port": 22, "from": "192.0.2.0/24"}],
                                  detected=["8080 tcp"], upstreams=["192.0.2.5"],
                                  route_rules=[])
    again = rules.ufw_desired_rules(["192.0.2.0/24"], allow_rules=[{"port": 8080, "from": "192.0.2.5"}],
                                    detected=["8080 tcp"], upstreams=["192.0.2.5"])
    assert [(r["tag"], r["is_ssh"]) for r in got] == [
        ("agent-cloud:in:22/tcp:192.0.2.0/24", True), ("agent-cloud:in:8080/tcp:192.0.2.5", False)]
    assert [r["tag"] for r in again] == ["agent-cloud:in:22/tcp:192.0.2.0/24", "agent-cloud:in:8080/tcp:192.0.2.5"]


def test_a_rule_naming_no_address_is_marked_dual_family():
    got = {r["spec"]: r["dual_family"] for r in rules.ufw_desired_rules(
        ["192.0.2.0/24", "any"], allow_rules=[{"port": 80, "from": "any"}], bridges=["podman1"],
        deny_egress=[{"to": "198.51.100.3"}])}
    assert got == {
        "allow from 192.0.2.0/24 to any port 22 proto tcp": False,
        "allow 22/tcp": True,                       # from any: ufw's short form, both families
        "allow 80/tcp": True,
        "allow in on podman1 to any port 53 proto udp": True,
        "allow in on podman1 to any port 53 proto tcp": True,
        "deny out to 198.51.100.3": False,
    }


def _held(spec, tag):
    return {"spec": spec, "tag": tag, "family": "", "peer": ""}


def test_a_rule_is_held_only_by_its_tag_and_its_spec_together():
    desired = rules.ufw_desired_rules(["192.0.2.0/24"], allow_rules=[{"port": 8080, "from": "192.0.2.5"}])
    ssh, web = desired
    # the same tag over another rule does not stand in for the declared one (SSH included)
    masked = _held("allow from 203.0.113.9 to any port 5432 proto tcp", ssh["tag"])
    assert rules.ufw_absent(desired, [masked, _held(web["spec"], web["tag"])]) == [ssh]
    # the declared spec without its tag (untagged, or an old tag) is absent too: it is retagged
    assert rules.ufw_absent(desired, [_held(ssh["spec"], ""), _held(web["spec"], "agent-cloud:old")]) == desired
    assert rules.ufw_absent(desired, [_held(ssh["spec"], ssh["tag"]), _held(web["spec"], web["tag"])]) == []
    assert rules.ufw_masking([masked, _held(web["spec"], web["tag"]), _held("allow 8443/tcp", "")], desired) == [masked]


def test_a_rule_naming_no_address_is_re_asserted_even_when_held():
    desired = rules.ufw_desired_rules(["192.0.2.0/24"], bridges=["podman1"])
    held = [_held(d["spec"], d["tag"]) for d in desired]
    assert rules.ufw_absent(desired, held) == []
    assert [d["spec"] for d in rules.ufw_absent(desired, held, reassert_dual_family=True)] == [
        "allow in on podman1 to any port 53 proto udp", "allow in on podman1 to any port 53 proto tcp"]


def test_a_rule_is_deleted_by_its_spec():
    assert rules.ufw_delete_args("allow from 192.0.2.7 to any port 443 proto tcp") == \
        "delete allow from 192.0.2.7 to any port 443 proto tcp"
    assert rules.ufw_delete_args("route allow from 192.0.2.7 to any port 443 proto tcp") == \
        "route delete allow from 192.0.2.7 to any port 443 proto tcp"


def test_an_explicit_null_is_never_read_as_a_default():
    # A null egress port must not widen a scoped denial to the whole destination, and a null
    # proto must not become tcp: both render as written, which ufw refuses (the playbook's
    # egress validation refuses them first).
    [_, deny] = rules.ufw_desired_rules(["192.0.2.0/24"], deny_egress=[{"to": "198.51.100.1", "port": None}])
    assert deny["cmd"] == "deny out to 198.51.100.1 port None proto tcp"
    [_, allow] = rules.ufw_desired_rules(["192.0.2.0/24"],
                                         allow_rules=[{"port": 80, "proto": None, "from": "192.0.2.5"}])
    assert allow["cmd"] == "allow from 192.0.2.5 to any port 80 proto None"


# ── Egress denials (ufw_egress_problems). RFC 5737 / RFC 3849 documentation ranges stand in
# for the declared management prefixes; real addresses live only in site-config.
MGMT = ["192.0.2.0/24", "198.51.100.0/24"]


@pytest.mark.parametrize("entry", [
    # the shape the runner hosts declare: every target INSIDE the management prefix, which is
    # why the guard must never be phrased as "reject anything within an SSH CIDR" (MISTAKES 2.5)
    {"to": "192.0.2.164", "port": 8200, "comment": "secret store"},
    {"to": "192.0.2.117", "port": 3000},
    {"to": "192.0.2.110/32", "port": 8006},
    {"to": "192.0.2.5/255.255.255.255"},
    {"to": " 192.0.2.9 ", "port": "8200", "proto": "any"},
    {"to": "192.0.2.10", "port": 53, "proto": "udp"},
    {"to": "2001:db8::1/128"},
    {"to": "2001:db8::1", "broad": False},
    # a wider mask, flagged and justified: outside the management prefixes, or inside one
    {"to": "203.0.113.0/24", "broad": True, "reason": "third-party range"},
    {"to": "192.0.2.128/25", "broad": "yes", "reason": "the upper half, not the whole prefix"},
])
def test_an_egress_denial_the_platform_really_declares_is_accepted(entry):
    assert rules.ufw_egress_problems([entry], MGMT) == []


@pytest.mark.parametrize("entry,why", [
    # a supernet of the management network: the self-lock, refused even flagged and justified
    ({"to": "192.0.0.0/16", "broad": True, "reason": "stated"}, "contains or equals the SSH CIDR 192.0.2.0/24"),
    ({"to": "192.0.2.0/24", "broad": True, "reason": "stated"}, "contains or equals the SSH CIDR 192.0.2.0/24"),
    # the same network spelled with host bits set, as ufw would store it
    ({"to": "192.0.2.1/24", "broad": True, "reason": "stated"}, "contains or equals the SSH CIDR 192.0.2.0/24"),
    ({"to": "192.0.2.77/255.255.255.0", "broad": True, "reason": "x"}, "contains or equals"),
    ({"to": "0.0.0.0/0", "broad": True, "reason": "everything"}, "contains or equals the SSH CIDR 198.51.100.0/24"),
    # wider than one address without the flag, or flagged without a reason
    ({"to": "192.0.2.0/24"}, "names more than one address"),
    ({"to": "203.0.113.0/24"}, "names more than one address"),
    ({"to": "203.0.113.0/24", "broad": True}, "requires a `reason`"),
    ({"to": "203.0.113.0/24", "broad": True, "reason": "  "}, "requires a `reason`"),
    ({"to": "203.0.113.0/24", "broad": "maybe", "reason": "x"}, "`broad` must be true or false"),
    # no destination, or one that is not an address (`any` would deny all egress)
    ({"port": 8200}, "`to` is required"),
    ({"to": ""}, "`to` is required"),
    ({"to": None}, "`to` is required"),
    ({"to": "any"}, "`to` is required"),
    ({"to": "vault.example.test"}, "`to` is required"),
    ({"to": "999.1.1.1"}, "`to` is required"),
    # a port that is not one port: null or empty must not read as "no port" and widen the denial
    ({"to": "198.51.100.1", "port": None}, "is not one port"),
    ({"to": "198.51.100.1", "port": ""}, "is not one port"),
    ({"to": "198.51.100.1", "port": 0}, "is not one port"),
    ({"to": "198.51.100.1", "port": 70000}, "is not one port"),
    ({"to": "198.51.100.1", "port": "22,80"}, "is not one port"),
    ({"to": "198.51.100.1", "port": True}, "is not one port"),
    ({"to": "198.51.100.1", "port": 8200, "proto": None}, "is not tcp, udp or any"),
    ({"to": "198.51.100.1", "port": 8200, "proto": "icmp"}, "is not tcp, udp or any"),
    ("192.0.2.164", "an entry is a mapping"),
])
def test_an_egress_denial_that_is_not_scoped_is_refused(entry, why):
    problems = rules.ufw_egress_problems([entry], MGMT)
    assert any(why in p for p in problems), problems


def test_egress_containment_is_compared_in_the_spelling_ufw_stores():
    # A /32 SSH CIDR and the bare address are one host: denying it is denying the SSH source.
    assert rules.ufw_egress_problems([{"to": "192.0.2.5"}], ["192.0.2.5/32"])
    assert rules.ufw_egress_problems([{"to": "192.0.2.5/32"}], ["192.0.2.5"])
    # A broad range over a single-host SSH source contains it.
    assert rules.ufw_egress_problems([{"to": "192.0.2.0/28", "broad": True, "reason": "x"}], ["192.0.2.5"])
    # IPv6: a supernet is refused, and families never contain each other.
    assert rules.ufw_egress_problems([{"to": "2001:db8::/16", "broad": True, "reason": "x"}], ["2001:db8::/32"])
    assert rules.ufw_egress_problems([{"to": "192.0.0.0/8", "broad": True, "reason": "x"}], ["2001:db8::/32"]) == []


def test_an_ssh_cidr_that_is_not_an_address_cannot_prove_a_denial_safe():
    assert rules.ufw_egress_problems([], ["mgmt-net"]) == []  # nothing declared: nothing to prove
    [problem] = rules.ufw_egress_problems([{"to": "198.51.100.1"}], ["mgmt-net"])
    assert "SSH CIDR 'mgmt-net' is not an address" in problem
    # `any` is the whole address space: a host inside it is fine, the whole space is not
    assert rules.ufw_egress_problems([{"to": "198.51.100.1"}], ["any"]) == []
    assert rules.ufw_egress_problems([{"to": "::/0", "broad": True, "reason": "x"}], ["any"])


def test_every_problem_of_an_entry_is_reported_not_only_the_first():
    problems = rules.ufw_egress_problems([{"to": "192.0.0.0/16", "port": 0, "proto": "icmp"}], MGMT)
    assert len(problems) == 4, problems  # not broad, contains the SSH CIDR, port, proto
