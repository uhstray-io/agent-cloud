"""The weft address checks (filter_plugins/weft_addresses.py), tested directly.

test_weft_deploy_runs.py proves tasks/weft-bind.yml refuses through them before anything stops;
the spellings and the containment rules live here, as function calls instead of playbook runs.
Addresses are documentation ranges (RFC 5737, RFC 3849).
"""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "weft_addresses", Path(__file__).resolve().parents[1] / "playbooks/filter_plugins/weft_addresses.py")
weft = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(weft)

GATEWAY = "192.0.2.1"
CADDY = "192.0.2.5/32"


@pytest.mark.parametrize("bind", [
    "0.0.0.0", "::", "[::]", "::0", "0::0", "0:0:0:0:0:0:0:0", "::0.0.0.0", "::ffff:0.0.0.0",
    "127.0.0.1", "127.1.2.3", "::1", "[::1]", "0::1", "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1",
    "::ffff:7f00:1", "[::ffff:7f00:1]", " 127.0.0.1 ",
    "localhost", "LOCALHOST", "localhost.", "box.localhost",
    # inet_aton reads these as 127.0.0.1; ipaddress cannot read them, so they are not judged as names.
    "127.1", "0x7f.1", "2130706433", "0", "::ffff:127.1", "", " ", None,
])
def test_a_wildcard_or_loopback_bind_is_refused_under_any_spelling(bind):
    # weft #582: the text match this replaced let `::0`, `0::1` and `::ffff:7f00:1` through, and
    # under pasta each lets every process on the VM arrive as loopback, which the board admits.
    assert weft.weft_bind_problems(bind, "weft_board_bind"), f"{bind!r} was accepted as a published address"


@pytest.mark.parametrize("bind", ["192.0.2.10", "[2001:db8::10]", "2001:db8::10", "::ffff:192.0.2.10"])
def test_the_vm_s_own_address_is_accepted_as_a_bind(bind):
    assert weft.weft_bind_problems(bind) == []


@pytest.mark.parametrize("bind", ["weft-ws1.example.test", "weft-ws1", "weft-ws1.example.test.", "[weft-ws1]",
                                  "192.0.2.10:8080", "192.0.2.0/24"])
def test_a_bind_that_is_not_an_ip_address_is_refused(bind):
    # weft #585: the bind is the host IP of a published port, and podman takes only a literal there.
    # A name was refused at `compose up`, after the stop, and one resolving to loopback was never judged.
    problems = weft.weft_bind_problems(bind, "weft_door_bind")
    assert problems and all(p.startswith("weft_door_bind ") and "not an IP address" in p for p in problems), problems


@pytest.mark.parametrize("bind, host", [
    ("2001:db8::10", "[2001:db8::10]"), ("[2001:db8::10]", "[2001:db8::10]"), (" 2001:db8::10 ", "[2001:db8::10]"),
    ("::ffff:192.0.2.10", "[::ffff:192.0.2.10]"), ("192.0.2.10", "192.0.2.10"), ("[192.0.2.10]", "192.0.2.10"),
    (" 192.0.2.10 ", "192.0.2.10"),
    # Local mode judges nothing, so a name can reach a render there; it is left as written.
    ("weft.localhost", "weft.localhost"),
])
def test_an_ipv6_bind_is_bracketed_before_a_port_and_brackets_are_never_doubled(bind, host):
    # weft #585: `2001:db8::10:8080` is not a host and a port; podman refuses it as a published port
    # (`invalid port format`, measured on podman 6.2), and neither curl nor Caddy can split it.
    assert weft.weft_host_for_port(bind) == host


@pytest.mark.parametrize("proxies", [["192.0.2.0/24"], ["192.0.2.4/31"], ["2001:db8::/64"],
                                     ["::ffff:192.0.2.0/120"], [CADDY, "198.51.100.0/24"]])
def test_a_trusted_proxy_wider_than_one_address_is_refused(proxies):
    # Every trusted address may name any client; a range trusts whatever else lives in it.
    problems = weft.weft_peer_problems(proxies, [], [GATEWAY])
    assert any("wider than one address" in p for p in problems), problems


@pytest.mark.parametrize("trimmed", [False, True], ids=["board", "door"])
@pytest.mark.parametrize("proxies", [[CADDY], ["2001:db8::5/128"], ["::ffff:192.0.2.5/128"], []])
def test_the_caddy_host_alone_is_accepted_as_a_trusted_proxy(proxies, trimmed):
    assert weft.weft_peer_problems(proxies, ["198.51.100.0/24"], [GATEWAY], trimmed=trimmed) == []


@pytest.mark.parametrize("proxies", [[CADDY, "  "], [CADDY, ""], [f" {CADDY} "]])
def test_a_blank_or_padded_proxy_passes_only_where_the_template_trims_it(proxies):
    # door.env.j2 trims each entry and drops blanks; weft.toml.j2 renders the board's list as
    # written. weft trims the board's trusted_proxies, so a blank entry is refused by weft too
    # (after the stop), and a padded one is refused here only because the filter is stricter.
    assert weft.weft_peer_problems(proxies, [], [GATEWAY], trimmed=True) == []
    problems = weft.weft_peer_problems(proxies, [], [GATEWAY])
    assert any("not a CIDR block" in p for p in problems), problems


UNREADABLE = ["192.0.2.5", "any", "192.0.2.5/33", "::ffff:192.0.2.0/64", "192.0.2.5/255.255.255.255",
              "fe80::1%eth0/128", "192.0.2.5/+32", "192.0.2.5/32/1", "2001:db8::5/129", "192.0.2.5/"]


@pytest.mark.parametrize("trimmed", [False, True], ids=["board", "door"])
@pytest.mark.parametrize("entry", UNREADABLE)
def test_a_trusted_proxy_weft_cannot_read_is_refused_before_weft_refuses_it_after_the_stop(entry, trimmed):
    # Read as client-address's Allowlist::parse reads it: `<IpAddr>/<u8>`, so no netmask and no zone.
    problems = weft.weft_peer_problems([entry], [], [GATEWAY], trimmed=trimmed)
    assert any("not a CIDR block" in p for p in problems), problems


@pytest.mark.parametrize("entry", [*UNREADABLE, "", "  ", " 198.51.100.0/24", "198.51.100.0/24 "])
def test_an_ingest_source_weft_cannot_read_is_refused_before_weft_refuses_it_after_the_stop(entry):
    # weft does not trim ingest_from (config.rs, Allowlist::parse over the entries as written).
    problems = weft.weft_peer_problems([], [entry], [GATEWAY])
    assert any("not a CIDR block" in p for p in problems), problems


def test_a_bare_string_of_proxies_is_judged_as_one_entry_not_by_character():
    # Character by character, "192.0.2.5/32" would be twelve unreadable entries; as one it is the
    # Caddy host, and a range is still refused.
    assert weft.weft_peer_problems(CADDY, [], [GATEWAY]) == []
    assert weft.weft_peer_problems("192.0.2.0/24", [], [GATEWAY])


@pytest.mark.parametrize("which", ["trusted", "ingest"])
@pytest.mark.parametrize("entry", [f"{GATEWAY}/32", "192.0.2.0/24", "::ffff:192.0.2.1/128",
                                   "169.254.1.2/32", "169.254.0.0/16", "::ffff:169.254.1.2/128"])
def test_an_entry_holding_an_address_the_vm_s_own_processes_arrive_as_is_refused(which, entry):
    # Under pasta a process or container on the VM arrives as the default gateway (podman 4.9.3)
    # or 169.254.1.2 (5.3 on). Believed, it is believed for every local process at once.
    trusted, ingest = ([entry], []) if which == "trusted" else ([], [entry])
    problems = weft.weft_peer_problems(trusted, ingest, [GATEWAY])
    assert any("arrives as under pasta" in p for p in problems), problems


def test_169_254_1_2_is_refused_even_when_no_gateway_could_be_read():
    problems = weft.weft_peer_problems([], ["169.254.0.0/16"], [])
    assert any("169.254.1.2" in p for p in problems), problems


def test_an_ipv6_gateway_is_held_to_the_same_rule():
    problems = weft.weft_peer_problems([], ["2001:db8::/64"], [GATEWAY, "2001:db8::1"])
    assert any("2001:db8::1" in p for p in problems), problems


def test_an_ingest_range_clear_of_the_gateway_is_accepted():
    # ingest_from may be a range: collectors are many; only the local peers are refused in it.
    assert weft.weft_peer_problems([CADDY], ["198.51.100.0/24", "2001:db8:1::/48"], [GATEWAY]) == []


def test_each_problem_names_the_variable_it_came_from():
    problems = weft.weft_peer_problems(["192.0.2.0/24"], ["192.0.2.0/24"], [GATEWAY],
                                       ["weft_door_trusted_proxies", "weft_board_ingest_from"])
    assert any(p.startswith("weft_door_trusted_proxies ") for p in problems), problems
    assert any(p.startswith("weft_board_ingest_from ") for p in problems), problems
