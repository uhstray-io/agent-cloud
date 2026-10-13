"""Tests for the Caddyfile site reader/editor.

Addresses are RFC 5737 documentation addresses (192.0.2.0/24), never RFC1918:
a fixture using a private range reads as a real internal address to both a human
and the secret scanner, which is how a real one got committed as a test vector
once already.

The fixture reproduces the quirks of the live file rather than an idealised one:
mixed tabs and spaces, `{$VAR}` placeholders that open and close on one line, a
nested `route {}`, a multi-address block, and a global options block.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import caddyfile_sites  # noqa: E402
from caddyfile_sites import (  # noqa: E402
    _strip_comment,
    find_site,
    parse_sites,
    retire,
    running_handlers,
    summarize,
)

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
    out, removed, already_managed = retire(CADDYFILE, ["devlog.example.io"])
    assert removed == ["devlog.example.io"]
    assert already_managed == []
    assert "devlog.example.io" not in out
    # Everything else survives, including the block that followed it.
    for keep in ("nocodb.example.io", "alpha.example.io", "canvas.example.io"):
        assert keep in out
    assert "email admin@example.com" in out


def test_retire_is_idempotent():
    once, _, _ = retire(CADDYFILE, ["devlog.example.io"])
    twice, removed, _ = retire(once, ["devlog.example.io"])
    assert removed == []
    assert once == twice


def test_retire_leaves_an_already_adopted_block_alone_and_says_so():
    # The steady state after an adoption: the hostname sits INSIDE the managed
    # region and caddy_retire_sites still names it. That declaration must
    # converge on every later run — an earlier version raised here, and the
    # first re-run after adopting a route failed on the production Caddy
    # (rolled back, but unable to complete until the inventory was edited).
    out, removed, already_managed = retire(CADDYFILE, ["canvas.example.io"])
    assert removed == []
    assert already_managed == ["canvas.example.io"]
    assert out == CADDYFILE


def test_retire_mixes_an_adopted_name_with_a_real_retirement():
    out, removed, already_managed = retire(CADDYFILE, ["canvas.example.io", "devlog.example.io"])
    assert removed == ["devlog.example.io"]
    assert already_managed == ["canvas.example.io"]
    assert "devlog.example.io" not in out
    # The adopted block survives as a parsed, still-managed site, not merely as text.
    assert find_site(parse_sites(out), "canvas.example.io")["managed"] is True


def test_retire_refuses_a_shared_block():
    # Deleting this block would take beta down with alpha, with no mention of it.
    with pytest.raises(SystemExit, match="beta.example.io"):
        retire(CADDYFILE, ["alpha.example.io"])


def test_retire_leaves_the_file_parseable():
    out, _, _ = retire(CADDYFILE, ["devlog.example.io"])
    remaining = [a for s in parse_sites(out) for a in s["addresses"]]
    assert remaining == [
        "nocodb.example.io",
        "alpha.example.io",
        "beta.example.io",
        "canvas.example.io",
    ]


# ── Conformance to the documented Caddyfile grammar ──────────────────────────
# Each case below is a rule quoted from https://caddyserver.com/docs/caddyfile/
# concepts, and each one FAILED before these tests existed. The parser was
# written from the shape of one live file, which is why it agreed with that file
# and disagreed with the grammar.


def test_addresses_may_be_space_separated():
    # "localhost:8080 example.com www.example.com" — comma OR whitespace.
    # Splitting on commas alone produced the single address
    # "a.example.io b.example.io", which no lookup could ever match, so the
    # multi-address refusal in retire() would not have fired either.
    sites = parse_sites("a.example.io b.example.io {\n\treverse_proxy 192.0.2.1:80\n}\n")
    assert [s["addresses"] for s in sites] == [["a.example.io", "b.example.io"]]


def test_hash_mid_token_is_not_a_comment():
    # "The hash character # for a comment cannot appear in the middle of a token
    # (i.e. it must be preceded by a space or appear at the beginning of a
    # line)." Quote-tracking instead of position truncated this upstream to
    # http://192.0.2.1:80/ — reporting an upstream the server does not use.
    sites = parse_sites("a.example.io {\n\treverse_proxy http://192.0.2.1:80/#frag\n}\n")
    assert sites[0]["upstreams"] == ["http://192.0.2.1:80/#frag"]


def test_trailing_comment_is_still_stripped():
    sites = parse_sites("a.example.io {\n\treverse_proxy 192.0.2.1:80 # note\n}\n")
    assert sites[0]["upstreams"] == ["192.0.2.1:80"]


def test_snippets_and_named_routes_are_not_sites():
    # A snippet `(name) {` and a named route `&(name) {` are column-0 brace
    # blocks that are not sites. Treating them as sites let `list` misreport
    # them and would let `retire` delete a snippet every site imports.
    text = (
        "(common) {\n\theader X 1\n}\n"
        "&(myroute) {\n\trespond \"x\"\n}\n"
        "a.example.io {\n\timport common\n}\n"
    )
    assert [s["addresses"] for s in parse_sites(text)] == [["a.example.io"]]


def test_heredoc_contents_do_not_affect_brace_depth():
    # "Inside quoted tokens, all other characters are treated literally."
    # An unbalanced brace inside a heredoc desynced depth for the REST of the
    # file: parse_sites returned [] — the file read as having no routes at all,
    # rather than as unparseable, which is the dangerous direction.
    text = (
        "a.example.io {\n\trespond <<HTML\n\t{ unbalanced\n\tHTML\n}\n"
        "b.example.io {\n\treverse_proxy 192.0.2.2:80\n}\n"
    )
    assert [s["addresses"] for s in parse_sites(text)] == [
        ["a.example.io"],
        ["b.example.io"],
    ]


def test_single_line_block_is_not_a_site():
    # "The open curly brace { must be at the end of its line." A one-line block
    # is rejected by Caddy itself ("Unexpected '}' because no matching opening
    # brace", verified against the live binary), so ignoring it matches the
    # grammar. Pinned so nobody "fixes" the parser to accept invalid syntax.
    assert parse_sites('a.example.io { respond "hi" }\n') == []


def test_hash_inside_a_quoted_token_is_literal():
    # "Inside quoted tokens, all other characters are treated literally." So a
    # `#` in a quoted token is literal even with a space before it — the
    # position rule alone truncated this. Both rules apply, not either.
    sites = parse_sites('a.example.io {\n\trespond "a # b"\n}\n')
    assert sites[0]["addresses"] == ["a.example.io"]
    assert _strip_comment('\trespond "a # b"') == '\trespond "a # b"'
    assert _strip_comment("\trespond `x # y`") == "\trespond `x # y`"


def test_a_real_trailing_comment_is_still_removed():
    assert _strip_comment("\treverse_proxy 192.0.2.1:80 # note") == "\treverse_proxy 192.0.2.1:80 "
    assert _strip_comment("# whole line") == ""


def test_allowlist_site_with_nested_handle_blocks_reports_both_upstreams():
    # The inference edge (vLLM security-guide shape): a named matcher, nested
    # `handle` blocks each carrying a reverse_proxy — one with a sub-block — and
    # a bare `handle { respond 404 }`. The `list` report the managed-sites
    # playbook prints must attribute both upstreams to the ONE site, with the
    # matcher tokens and braces skipped, or the drift report lies about it.
    text = (
        "inference.example.io {\n"
        "\ttls {\n\t\tdns cloudflare {$CLOUDFLARE_API_KEY}\n\t}\n"
        "\t@api path /v1/*\n"
        "\thandle @api {\n"
        '\t\t@noauth not header_regexp Authorization "^Bearer [^[:space:]]+$"\n'
        "\t\trespond @noauth 401\n"
        "\t\treverse_proxy 192.0.2.7:8000 {\n\t\t\tflush_interval -1\n\t\t}\n"
        "\t}\n"
        "\thandle /health {\n\t\treverse_proxy 192.0.2.7:8000\n\t}\n"
        "\thandle {\n\t\trespond 404\n\t}\n"
        "}\n"
        "b.example.io {\n\treverse_proxy 192.0.2.2:80\n}\n"
    )
    sites = parse_sites(text)
    assert [s["addresses"] for s in sites] == [["inference.example.io"], ["b.example.io"]]
    assert sites[0]["upstreams"] == ["192.0.2.7:8000", "192.0.2.7:8000"]
    assert sites[1]["upstreams"] == ["192.0.2.2:80"]


SPLIT = (
    "inference.example.io {\n"
    "\t@api path /v1/*\n"
    "\thandle @api {\n"
    '\t\t@noauth not header_regexp Authorization "^Bearer [^[:space:]]+$"\n'
    "\t\trespond @noauth 401\n"
    "\t\treverse_proxy https://192.0.2.9:4000 {\n\t\t\tflush_interval -1\n\t\t}\n"
    "\t}\n"
    "\thandle /health {\n\t\treverse_proxy 192.0.2.7:8000\n\t}\n"
    "\thandle {\n\t\trespond 404\n\t}\n"
    "}\n"
)


def test_each_handle_block_reports_its_own_paths_and_upstreams():
    (site,) = parse_sites(SPLIT)
    assert site["handlers"] == [
        {"matcher": "@api", "paths": ["/v1/*"], "upstreams": ["https://192.0.2.9:4000"]},
        {"matcher": "/health", "paths": ["/health"], "upstreams": ["192.0.2.7:8000"]},
        {"matcher": None, "paths": [], "upstreams": []},
    ]
    assert site["upstreams"] == ["https://192.0.2.9:4000", "192.0.2.7:8000"]


def test_a_named_matcher_declared_after_its_handle_still_resolves():
    (site,) = parse_sites("a.example.io {\n\thandle @late {\n\t\treverse_proxy 192.0.2.1:80\n\t}\n\t@late path /x\n}\n")
    assert site["handlers"][0]["paths"] == ["/x"]


def test_a_matcher_that_is_not_a_path_never_reads_as_a_catch_all():
    text = "a.example.io {\n\t@h header X-A 1\n\thandle @h {\n\t\treverse_proxy 192.0.2.1:80\n\t}\n}\n"
    (site,) = parse_sites(text)
    assert site["handlers"][0]["paths"] == ["@h"]
    assert summarize(site["handlers"]) == {"v1": [], "health": [], "other": ["192.0.2.1:80"]}


def test_a_reverse_proxy_outside_any_handle_is_a_catch_all_handler():
    (site,) = parse_sites("a.example.io {\n\treverse_proxy 192.0.2.1:80\n}\n")
    assert site["handlers"] == [{"matcher": None, "paths": [], "upstreams": ["192.0.2.1:80"]}]


def test_the_handler_state_does_not_leak_into_the_next_site():
    text = SPLIT + "b.example.io {\n\treverse_proxy 192.0.2.2:80\n}\n"
    sites = parse_sites(text)
    assert sites[1]["handlers"] == [{"matcher": None, "paths": [], "upstreams": ["192.0.2.2:80"]}]


def test_summarize_names_where_each_path_goes_and_strips_the_scheme():
    (site,) = parse_sites(SPLIT)
    assert summarize(site["handlers"]) == {"v1": ["192.0.2.9:4000"], "health": ["192.0.2.7:8000"], "other": []}


def test_a_catch_all_that_proxies_serves_a_path_only_when_nothing_names_it():
    both = [{"paths": [], "upstreams": ["192.0.2.1:80"]}]
    assert summarize(both) == {"v1": ["192.0.2.1:80"], "health": ["192.0.2.1:80"], "other": []}
    named = [
        {"paths": ["/v1/*"], "upstreams": ["192.0.2.1:80"]},
        {"paths": ["/health"], "upstreams": ["192.0.2.1:80"]},
        {"paths": [], "upstreams": ["192.0.2.5:80"]},
    ]
    assert summarize(named)["other"] == ["192.0.2.5:80"]


def test_a_path_nothing_expects_to_proxy_is_other():
    handlers = [
        {"paths": ["/v1/*"], "upstreams": ["192.0.2.9:4000"]},
        {"paths": ["/health"], "upstreams": ["192.0.2.7:8000"]},
        {"paths": ["/metrics"], "upstreams": ["192.0.2.7:8000"]},
    ]
    assert summarize(handlers)["other"] == ["192.0.2.7:8000"]


def _caddy_json(*routes, host="inference.example.io"):
    return {"apps": {"http": {"servers": {"srv0": {"routes": [{"match": [{"host": [host]}], "handle": [
        {"handler": "subroute", "routes": list(routes)}]}]}}}}}


def _proxied(path, *dials):
    # `caddy adapt` shape: route (path match) -> subroute -> route -> reverse_proxy
    route = {"handle": [{"handler": "subroute", "routes": [
        {"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": d} for d in dials]}]}]}]}
    if path:
        route["match"] = [{"path": [path]}]
    return route


def test_the_running_config_is_read_per_handler_through_nested_subroutes():
    config = _caddy_json(_proxied("/v1/*", "192.0.2.9:4000"), _proxied("/health", "192.0.2.7:8000"))
    assert summarize(running_handlers(config, "inference.example.io")) == {
        "v1": ["192.0.2.9:4000"], "health": ["192.0.2.7:8000"], "other": []}


def test_the_running_config_of_another_host_is_not_read():
    config = _caddy_json(_proxied("/v1/*", "192.0.2.9:4000"), host="b.example.io")
    assert running_handlers(config, "inference.example.io") == []


def test_a_dial_the_walk_cannot_attribute_still_counts_as_other():
    hidden = _proxied("/v1/*", "192.0.2.9:4000")
    hidden["handle"][0]["routes"][0]["handle"][0]["handle_response"] = [
        {"routes": [{"handle": [{"handler": "x", "upstreams": [{"dial": "192.0.2.66:1"}]}]}]}]
    config = _caddy_json(hidden, _proxied("/health", "192.0.2.7:8000"))
    assert summarize(running_handlers(config, "inference.example.io"))["other"] == ["192.0.2.66:1"]


def test_the_routes_command_reports_the_managed_flag_and_the_summary(tmp_path, capsys, monkeypatch):
    path = tmp_path / "Caddyfile"
    path.write_text("# BEGIN ANSIBLE MANAGED\n" + SPLIT + "# END ANSIBLE MANAGED\n")
    monkeypatch.setattr(sys, "argv", ["x", str(path), "routes", "--address", "inference.example.io"])
    assert caddyfile_sites.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "found": True, "managed": True, "v1": ["192.0.2.9:4000"], "health": ["192.0.2.7:8000"], "other": []}
    monkeypatch.setattr(sys, "argv", ["x", str(path), "routes", "--address", "nope.example.io"])
    assert caddyfile_sites.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "found": False, "managed": False, "v1": [], "health": [], "other": []}


def test_the_running_command_prints_the_upstream_lists_and_nothing_from_the_config(tmp_path, capsys, monkeypatch):
    config = _caddy_json(_proxied("/v1/*", "192.0.2.9:4000"), _proxied("/health", "192.0.2.7:8000"))
    config["apps"]["tls"] = {"dns_token": "synthetic-secret-value"}
    engine = tmp_path / "engine"
    engine.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(config)}\nEOF\n")
    engine.chmod(0o755)
    monkeypatch.setattr(sys, "argv", ["x", "running", "--address", "inference.example.io",
                                      "--engine", str(engine), "--container", "caddy"])
    assert caddyfile_sites.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"v1": ["192.0.2.9:4000"], "health": ["192.0.2.7:8000"], "other": []}
    assert "synthetic-secret-value" not in out


def test_the_running_command_fails_when_the_admin_api_cannot_be_read(tmp_path, capsys, monkeypatch):
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\nexit 1\n")
    engine.chmod(0o755)
    monkeypatch.setattr(sys, "argv", ["x", "running", "--address", "a", "--engine", str(engine), "--container", "c"])
    assert caddyfile_sites.main() == 1


def test_the_declared_command_reads_a_rendered_declaration_from_the_environment(capsys, monkeypatch):
    monkeypatch.setenv("SITES_TEXT", SPLIT)
    monkeypatch.setattr(sys, "argv", ["x", "declared", "--address", "inference.example.io", "--env", "SITES_TEXT"])
    assert caddyfile_sites.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "found": True, "v1": ["192.0.2.9:4000"], "health": ["192.0.2.7:8000"], "other": []}
    monkeypatch.setattr(sys, "argv", ["x", "declared", "--address", "nope.example.io", "--env", "SITES_TEXT"])
    assert caddyfile_sites.main() == 0
    assert json.loads(capsys.readouterr().out)["found"] is False
