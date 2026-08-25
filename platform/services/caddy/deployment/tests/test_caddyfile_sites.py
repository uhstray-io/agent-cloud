"""Tests for the Caddyfile site reader/editor.

Addresses are RFC 5737 documentation addresses (192.0.2.0/24), never RFC1918:
a fixture using a private range reads as a real internal address to both a human
and the secret scanner, which is how a real one got committed as a test vector
once already.

The fixture reproduces the quirks of the live file rather than an idealised one:
mixed tabs and spaces, `{$VAR}` placeholders that open and close on one line, a
nested `route {}`, a multi-address block, and a global options block.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from caddyfile_sites import find_site, parse_sites, retire  # noqa: E402

CADDYFILE = """\
{
\temail admin@example.com
}

# a hand-maintained route
nocodb.example.io {
\ttls {
\t\tdns cloudflare {$CLOUDFLARE_API_KEY}
\t\tresolvers 1.1.1.1
\t}

        reverse_proxy http://192.0.2.1:8080
}

devlog.example.io {
\ttls {
                dns cloudflare {$CLOUDFLARE_API_KEY}
        }

        reverse_proxy http://192.0.2.2:8000
}

alpha.example.io, beta.example.io {
\treverse_proxy http://192.0.2.9:80
}

# BEGIN ANSIBLE MANAGED — agent-cloud extra sites
canvas.example.io {
\troute {
\t\treverse_proxy /outpost.goauthentik.io/* http://192.0.2.3:9000
\t\tforward_auth http://192.0.2.3:9000 {
\t\t\turi /outpost.goauthentik.io/auth/caddy
\t\t}
\t\treverse_proxy 192.0.2.4:3000 {
\t\t\tflush_interval -1
\t\t}
\t}
}
# END ANSIBLE MANAGED — agent-cloud extra sites
"""


@pytest.fixture
def sites():
    return parse_sites(CADDYFILE)


def test_global_options_block_is_not_a_site(sites):
    # A bare `{` at column 0 is the global options block. Reporting it as a site
    # would let `retire` delete the email/ACME configuration for every route.
    assert all(s["addresses"] for s in sites)
    assert not any("email" in a for s in sites for a in s["addresses"])


def test_finds_every_site_and_no_nested_block(sites):
    found = [a for s in sites for a in s["addresses"]]
    assert found == [
        "nocodb.example.io",
        "devlog.example.io",
        "alpha.example.io",
        "beta.example.io",
        "canvas.example.io",
    ]
    # `tls {`, `route {`, and `forward_auth ... {` are nested, never sites.


def test_managed_region_is_distinguished(sites):
    assert find_site(sites, "canvas.example.io")["managed"] is True
    assert find_site(sites, "devlog.example.io")["managed"] is False


def test_upstreams_are_reported(sites):
    assert find_site(sites, "devlog.example.io")["upstreams"] == ["http://192.0.2.2:8000"]
    # A matcher token is not an upstream; both real upstreams are kept.
    assert find_site(sites, "canvas.example.io")["upstreams"] == [
        "http://192.0.2.3:9000",
        "192.0.2.4:3000",
    ]


def test_placeholder_braces_do_not_desync_depth(sites):
    # `{$CLOUDFLARE_API_KEY}` is balanced on its line. If depth tracking treated
    # it as an opening brace, every block after it would be swallowed into one.
    assert find_site(sites, "devlog.example.io") is not None
    assert find_site(sites, "alpha.example.io") is not None


def test_retire_removes_only_the_named_block(sites):
    out, removed = retire(CADDYFILE, ["devlog.example.io"])
    assert removed == ["devlog.example.io"]
    assert "devlog.example.io" not in out
    # Everything else survives, including the block that followed it.
    for keep in ("nocodb.example.io", "alpha.example.io", "canvas.example.io"):
        assert keep in out
    assert "email admin@example.com" in out


def test_retire_is_idempotent():
    once, _ = retire(CADDYFILE, ["devlog.example.io"])
    twice, removed = retire(once, ["devlog.example.io"])
    assert removed == []
    assert once == twice


def test_retire_refuses_a_managed_block():
    # The managed region is rewritten from inventory every run, so deleting from
    # it here would be silently undone.
    with pytest.raises(SystemExit, match="ANSIBLE MANAGED"):
        retire(CADDYFILE, ["canvas.example.io"])


def test_retire_refuses_a_shared_block():
    # Deleting this block would take beta down with alpha, with no mention of it.
    with pytest.raises(SystemExit, match="beta.example.io"):
        retire(CADDYFILE, ["alpha.example.io"])


def test_retire_leaves_the_file_parseable():
    out, _ = retire(CADDYFILE, ["devlog.example.io"])
    remaining = [a for s in parse_sites(out) for a in s["addresses"]]
    assert remaining == [
        "nocodb.example.io",
        "alpha.example.io",
        "beta.example.io",
        "canvas.example.io",
    ]
