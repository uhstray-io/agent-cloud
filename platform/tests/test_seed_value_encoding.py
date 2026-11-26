"""seed-openbao-key.yml's bao_value_encoding, run for real against a synthetic OpenBao.

The seed input is one line, so a multi-line value (a PEM key) is staged as base64 and the
playbook decodes it before the merge. Every value here is synthetic. The block is a plain
text fixture, not key material.

A refusal is checked by the NAME of the task that failed, not by its message: a no_log
result hides its fail_msg, so the task name is the only readable part of a refused
base64 check (the playbook puts the rule in that name).
"""

import base64
import importlib.util
import shutil
from pathlib import Path

import pytest
from fake_http import serve
from seed_harness import FakeBao, run_seed, writes

PLAYBOOK = "platform/playbooks/seed-openbao-key.yml"
PATH = "services/agentgateway"
KEY = "synthetic_pem_key"
EDGE = "-" * 5
# Built from parts, so no literal "BEGIN ... KEY" line sits in this file.
BLOCK = (f"{EDGE}BEGIN SYNTHETIC TEST BLOCK{EDGE}\nsynthetic-line-one\nsynthetic-line-two\n"
         f"{EDGE}END SYNTHETIC TEST BLOCK{EDGE}\n")
ENCODED = base64.b64encode(BLOCK.encode()).decode()

RULE_CHARS = "Refuse a BAO_VALUE with characters outside standard base64 (rule only; value not shown)"
RULE_LENGTH = "Refuse a BAO_VALUE whose length is not a multiple of four (rule only; value not shown)"
RULE_UTF8 = "Refuse a BAO_VALUE that does not decode to UTF-8 text (rule only; value not shown)"
ENCODING = "Validate the value encoding"
LOGIN = "Authenticate to OpenBao (AppRole)"

SPEC = importlib.util.spec_from_file_location(
    "semaphore_seed_input", Path(__file__).resolve().parents[2] / "scripts/semaphore-seed-input.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def seed(tmp_path, *, value=None, settings=None, stored=None, verify=False):
    """Run the seed once and return (returncode, output, state, requests).

    `stored` is the path's data before the run (None = the path is absent). The fake store
    answers the GET, the capability probe, the POST create and the PATCH merge, so the real
    write path runs.
    """
    state = {"data": stored, "version": 0 if stored is None else 1}

    class Bao(FakeBao):
        requests = []

        def do_POST(self):  # noqa: N802
            self.record("POST")
            payload = self.body()
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/v1/sys/capabilities-self":
                return self.reply({"capabilities": ["read", "create"], f"secret/data/{PATH}": ["read", "create"]})
            if self.path == f"/v1/secret/data/{PATH}":
                if payload.get("options", {}).get("cas") == 0 and state["data"] is not None:
                    return self.reply({"errors": ["check-and-set parameter did not match"]}, 400)
                state["data"], state["version"] = payload["data"], state["version"] + 1
                return self.reply({"data": {"version": state["version"]}})
            return self.reply({}, 403)

        def do_GET(self):  # noqa: N802
            self.record("GET")
            if state["data"] is None:
                return self.reply({}, 404)
            return self.reply({"data": {"data": state["data"], "metadata": {"version": state["version"]}}})

        def do_PATCH(self):  # noqa: N802
            self.record("PATCH")
            if state["data"] is None:
                return self.reply({}, 404)
            state["data"] = {**state["data"], **self.body()["data"]}
            state["version"] += 1
            return self.reply({"data": {"version": state["version"]}})

    extra = {"bao_path": PATH, "bao_key": KEY, "bao_verify_access_only": verify, **(settings or {})}
    inputs = {} if value is None else {"BAO_VALUE": value}
    with serve(Bao) as address:
        code, output = run_seed(tmp_path, PLAYBOOK, address, verbose=True, extra=extra, inputs=inputs)
    return code, output, state, Bao.requests


def failing_task(output):
    """The name of the task that failed: the last TASK header above its fatal line."""
    fatal = output.find("fatal: [localhost]")
    assert fatal != -1, output
    start = output.rfind("TASK [", 0, fatal)
    return output[start + len("TASK ["):output.index("]", start)]


@needs_ansible
@pytest.mark.parametrize("encoding", [None, "plain", ""])
def test_plain_is_the_default_and_a_base64_looking_value_is_stored_as_given(tmp_path, encoding):
    # Base64 of "ABCA", but plain: it must be merged exactly as written, never decoded.
    value = "QUJDQQ=="
    settings = None if encoding is None else {"bao_value_encoding": encoding}
    code, output, state, requests = seed(tmp_path, value=value, settings=settings)
    assert code == 0, output
    assert state["data"] == {KEY: value}
    assert writes(requests) == [("POST", f"/v1/secret/data/{PATH}")]


@needs_ansible
def test_base64_is_decoded_and_the_decoded_text_is_what_gets_merged(tmp_path):
    code, output, state, requests = seed(tmp_path, value=ENCODED, settings={"bao_value_encoding": "base64"})
    assert code == 0, output
    # Exact: the line breaks and the final newline of the decoded text survive the merge.
    assert state["data"] == {KEY: BLOCK}
    assert writes(requests) == [("POST", f"/v1/secret/data/{PATH}")]
    assert "created" in output
    for line in BLOCK.splitlines():
        assert line not in output  # the decoded text is never printed
    assert ENCODED not in output  # nor the encoded form


@needs_ansible
def test_the_decoded_text_is_what_the_idempotency_check_compares(tmp_path):
    code, output, state, requests = seed(tmp_path, value=ENCODED, settings={"bao_value_encoding": "base64"},
                                         stored={KEY: BLOCK})
    assert code == 0, output
    assert writes(requests) == []
    assert "already up to date" in output


@needs_ansible
def test_base64_merges_the_decoded_text_beside_existing_siblings(tmp_path):
    code, output, state, requests = seed(tmp_path, value=ENCODED, settings={"bao_value_encoding": "base64"},
                                         stored={"sibling": "kept"})
    assert code == 0, output
    assert state["data"] == {"sibling": "kept", KEY: BLOCK}
    assert writes(requests) == [("PATCH", f"/v1/secret/data/{PATH}")]


@needs_ansible
@pytest.mark.parametrize("value,rule", [
    # Alphabet: "QUJD!!!!" decodes (to "ABC") under Python's lenient decoder, so only the
    # alphabet rule stops it. Without that rule it would reach the login.
    ("QUJD!!!!", RULE_CHARS),
    # Padding past two characters is outside the alphabet rule's pattern as well.
    ("QUJDQQ===", RULE_CHARS),
    # Standard alphabet, but five characters: only the length rule refuses it.
    ("QUJDQ", RULE_LENGTH),
    # A trailing newline: "$" would accept it (Python matches before a final newline), "\Z" refuses it.
    ("QUJDQQ=\n", RULE_CHARS),
])
def test_malformed_base64_is_refused_by_name_before_the_login(tmp_path, value, rule):
    code, output, state, requests = seed(tmp_path, value=value, settings={"bao_value_encoding": "base64"})
    assert code != 0, output
    assert failing_task(output) == rule
    assert LOGIN not in output
    assert requests == []  # not even the AppRole login


@needs_ansible
@pytest.mark.parametrize("value", [
    # /w== is the byte 0xFF, and 4pg= is the truncated sequence E2 98: neither is UTF-8.
    # Ansible's b64decode turns each into a lone surrogate instead of failing, so without
    # the UTF-8 rule both would log in and be written as junk.
    "/w==",
    "4pg=",
])
def test_base64_that_is_not_utf8_text_is_refused_by_name_before_the_login(tmp_path, value):
    code, output, state, requests = seed(tmp_path, value=value, settings={"bao_value_encoding": "base64"})
    assert code != 0, output
    assert failing_task(output) == RULE_UTF8
    assert LOGIN not in output
    assert requests == []  # not even the AppRole login


@needs_ansible
@pytest.mark.parametrize("value,text", [
    # Multi-byte UTF-8 must still pass: w6k= is e-acute (C3 A9), 8J+YgA== is an emoji (F0 9F 98 80).
    ("w6k=", "é"),
    ("8J+YgA==", "\U0001f600"),
])
def test_base64_utf8_text_is_accepted_and_stored_decoded(tmp_path, value, text):
    code, output, state, requests = seed(tmp_path, value=value, settings={"bao_value_encoding": "base64"})
    assert code == 0, output
    assert state["data"] == {KEY: text}
    assert writes(requests) == [("POST", f"/v1/secret/data/{PATH}")]


@needs_ansible
@pytest.mark.parametrize("encoding", ["hex", "Base64", "base64 "])
def test_an_unknown_encoding_is_refused_before_the_login(tmp_path, encoding):
    code, output, state, requests = seed(tmp_path, value=ENCODED, settings={"bao_value_encoding": encoding})
    assert code != 0, output
    assert failing_task(output) == ENCODING
    assert "bao_value_encoding must be plain (the default) or base64." in output
    assert requests == []


@needs_ansible
def test_the_read_only_access_check_takes_the_encoding_and_needs_no_value(tmp_path):
    # Without a value the base64 checks must be skipped, or this run would refuse an empty value.
    code, output, state, requests = seed(tmp_path, settings={"bao_value_encoding": "base64"}, verify=True)
    assert code == 0, output
    assert "Read-only access to secret/services/agentgateway verified" in output
    assert writes(requests) == []


def decl_var(name):
    decl = cli.declaration("Seed OpenBao Key")
    return {v["name"]: v for v in decl["survey_vars"]}[name]


def test_the_survey_declares_the_encoding_with_plain_as_the_default():
    var = decl_var("bao_value_encoding")
    assert var["default_value"] == "plain"
    assert var.get("required") is False


def test_the_seed_cli_accepts_the_encoding_setting_without_a_cli_change():
    decl = cli.declaration("Seed OpenBao Key")
    settings = cli.read_settings(["bao_path=services/x", "bao_key=k", "bao_value_encoding=base64"], decl)
    assert settings["bao_value_encoding"] == "base64"
