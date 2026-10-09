"""The agentgateway apiKey extraction filter (rollback-inference-route.yml), tested directly.

The rollback reads enrolments out of config files that hold key hashes, so the filter returns
native data and, on a file it cannot use, only the file's problem CLASS. All values are
synthetic.
"""

import base64
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "agw_apikey_entries", Path(__file__).resolve().parents[1] / "playbooks/filter_plugins/agw_apikey_entries.py")
filt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(filt)


def run(text):
    return filt.agw_apikey_entries(base64.b64encode(text.encode()).decode())


def test_it_is_registered_under_its_name():
    assert filt.FilterModule().filters() == {"agw_apikey_entries": filt.agw_apikey_entries}


def test_it_returns_the_entries_of_the_one_apikey_policy():
    out = run("llm:\n  policies:\n    apiKey:\n      mode: strict\n      keys:\n"
              "        - {keyHash: 'sha256:aaaa', metadata: {name: ws}}\n"
              "        - {key: plain, metadata: {name: pi}}\n")
    assert out == {"problem": "", "entries": [{"keyHash": "sha256:aaaa", "metadata": {"name": "ws"}},
                                                {"key": "plain", "metadata": {"name": "pi"}}]}


def test_it_finds_apikey_blocks_at_any_depth_in_document_order():
    out = run("a:\n  apiKey: {keys: [{keyHash: h1}]}\n"
              "binds:\n  - listeners:\n      - routes:\n          - policies:\n"
              "              apiKey: {keys: [{keyHash: h2}, {keyHash: h3}]}\n"
              "z: {deep: {er: {apiKey: {keys: [{keyHash: h4}]}}}}\n")
    assert [e["keyHash"] for e in out["entries"]] == ["h1", "h2", "h3", "h4"]


def test_it_collects_several_apikey_blocks_side_by_side():
    out = run("x: [{apiKey: {keys: [{keyHash: h1}]}}, {apiKey: {keys: [{keyHash: h2}]}}]\n")
    assert [e["keyHash"] for e in out["entries"]] == ["h1", "h2"]


def test_entries_that_are_not_mappings_and_empty_ones_are_dropped():
    out = run("apiKey: {keys: [bare-string, 7, null, [nested], {}, {keyHash: h1}]}\n")
    assert out == {"problem": "", "entries": [{"keyHash": "h1"}]}


@pytest.mark.parametrize("keys", ["not-a-list", "{keyHash: h1}", "null", "42"])
def test_a_keys_value_that_is_not_a_list_is_ignored(keys):
    assert run(f"apiKey: {{keys: {keys}}}\n") == {"problem": "", "entries": []}


@pytest.mark.parametrize("value", ["a-string", "[1, 2]", "null", "{mode: strict}"])
def test_an_apikey_value_that_is_not_a_mapping_with_keys_enrols_nobody(value):
    assert run(f"llm: {{apiKey: {value}}}\n") == {"problem": "", "entries": []}


def test_anchors_aliases_and_merge_keys_are_resolved():
    out = run("base: &b {keyHash: h1, metadata: {name: ws}}\n"
              "apiKey:\n  keys:\n    - *b\n    - <<: *b\n      keyHash: h2\n")
    assert [e["keyHash"] for e in out["entries"]] == ["h1", "h2"]
    assert out["entries"][1]["metadata"] == {"name": "ws"}


def test_a_yaml_date_becomes_text_so_the_result_is_plain_json_data():
    out = run("apiKey: {keys: [{keyHash: h1, metadata: {name: ws, since: 2026-10-01, n: 3}}]}\n")
    assert out["entries"][0]["metadata"] == {"name": "ws", "since": "2026-10-01", "n": 3}


@pytest.mark.parametrize("text", ["", "# only a comment\n", "---\n"])
def test_an_empty_document_enrols_nobody(text):
    assert run(text) == {"problem": "", "entries": []}


@pytest.mark.parametrize(("text", "problem"), [
    ("llm: [unclosed\n  keyHash: sha256:aaaa1111\n", "not YAML"),
    ("a: 1\n---\nb: 2\n", "not YAML"),
    ("- keyHash: sha256:aaaa1111\n", "not a mapping"),
    ("sha256:aaaa1111\n", "not a mapping"),
    ("42\n", "not a mapping"),
])
def test_an_unusable_file_yields_only_its_problem_class(text, problem):
    out = run(text)
    assert out == {"problem": problem, "entries": []}
    assert "aaaa1111" not in repr(out)


def test_content_that_is_not_base64_text_is_not_yaml():
    assert filt.agw_apikey_entries("!!! not base64 !!!") == {"problem": "not YAML", "entries": []}
    assert filt.agw_apikey_entries(base64.b64encode(b"\xff\xfe\x00").decode()) == {"problem": "not YAML", "entries": []}


def test_a_structure_nested_too_deeply_is_named_not_crashed():
    text = "a: " + "[" * 5000 + "]" * 5000 + "\n"
    assert run(text)["problem"] in {"nested too deeply", "not YAML"}


def test_a_cyclic_alias_is_named_nested_too_deeply():
    """`a: &a [*a]` is a list holding itself: valid YAML, an endless walk."""
    assert run("a: &a [*a]\n") == {"problem": "nested too deeply", "entries": []}


def test_a_key_that_json_cannot_carry_is_named_not_crashed():
    out = run("apiKey:\n  keys:\n    - {keyHash: h1, 2026-10-01: dated-key}\n")
    assert out == {"problem": "unreadable structure", "entries": []}
