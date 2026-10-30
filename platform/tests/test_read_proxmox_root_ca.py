"""The read-only Proxmox root CA read (platform/playbooks/read-proxmox-root-ca.yml, "Read Proxmox Root CA").

Structure, read from the parsed playbook:
- order: refuse-internal-extra-vars.yml is the first play; the stored token id is asserted before the first
  request; the leaf certificate is observed, and the operator's pin is checked against it, before the first
  token-bearing request; the node-list check precedes the per-node reads; the root CA agreement, the pve-ssl check of
  the serving nodes and the issuer check of every served match precede the one debug;
- read-only: every command is the pinned helper (platform/lib/pve_pinned_get.py) with check_mode false and
  changed_when false; the helper sends GET only; no write module; the only lookups read
  secret/data/services/proxmox and the environment; the play writes to neither OpenBao nor Proxmox;
- TLS and token: no uri module, no module_defaults, no validate_certs and no inline Authorization header
  anywhere in the playbook text; the admin token reaches the helper on stdin only, under no_log;
- no protected output: the one debug references only _pve_online_nodes, _pve_ca, _pve_served_names and the observed
  leaf digest; the node list, the per-node reads and the flattened entries are referenced only by no_log tasks; no
  SAN is named in code;
- selection: the root CA and the node certificate are chosen by basename, never by a full path; every online node is
  read, in name order, by index; the certificates the pinned connection presented are chosen by fingerprint across all
  nodes, one or more matches are named, and every match must be issued by the root CA, as must every serving node's
  pve-ssl.pem;
- failures: the observe task never fails itself (a failed command prints its argv, which carries the URL); each
  refusal is named by the helper's exit code, from a no_log fact, and no visible task reads stderr.

Behaviour: the real tasks run under ansible-playbook against a loopback HTTPS fake Proxmox (a leaf certificate
generated at test time, the same generator as test_pve_pinned_get.py). Refusals are proved on the wire: the fake
records every request, and a refused run records none.
"""

import copy
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible
from fake_http import DrainingHandler
from test_pve_pinned_get import _generate_leaf

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/read-proxmox-root-ca.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
HELPER = REPO / "platform/lib/pve_pinned_get.py"

TEXT = PLAYBOOK.read_text()
DOC = yaml.safe_load(TEXT)
HELPER_TEXT = HELPER.read_text()
TASK_KEYWORDS = {
    "name", "register", "when", "no_log", "vars", "delegate_to", "loop", "loop_control", "tags", "check_mode",
    "changed_when", "failed_when", "become", "ignore_errors", "notify", "module_defaults",
}
PREFIX = "ansible.builtin."
# command is the one module that reaches Proxmox, and only as the pinned helper (checked per task below).
READ_MODULES = {"set_fact", "assert", "debug", "include_tasks"}
WRITE_MODULES = {
    "copy", "file", "template", "lineinfile", "blockinfile", "replace", "command", "shell", "raw",
    "script", "unarchive", "get_url", "archive", "fetch", "uri",
}
HELPER_SUBCOMMANDS = {"fingerprint", "get"}
# Responses that carry node addresses (the SAN of each entry) or the admin token's answers: visible tasks never
# touch them. The per-node registers and the flattened entries hold the same responses, one level derived.
PROTECTED = {"_pve_nodes", "_pve_certs_all", "_pve_read_list", "_pve_all_entries", "_pve_served_list"}
SECRETS = {"_pve_secret", "_pve_data"}
DEBUG_NAMES = {"_pve_online_nodes", "_pve_ca", "_observed_leaf", "_pve_served_names"}
DEBUG_FIELDS = {"subject", "fingerprint", "notafter", "pem"}
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
URL_LITERAL = re.compile(r"https?://")
SAN = re.compile(r"\bsan\b", re.IGNORECASE)


def _play():
    return next(p for p in DOC if isinstance(p, dict) and p.get("hosts") == "localhost" and "tasks" in p)


def _tasks():
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    walk(_play()["tasks"])
    return out


def _module(task):
    keys = [k for k in task if k not in TASK_KEYWORDS]
    assert len(keys) == 1, task.get("name")
    key = keys[0]
    return key[len(PREFIX):] if key.startswith(PREFIX) else key


def _value(task):
    return task[next(k for k in task if k not in TASK_KEYWORDS)]


def _args(task):
    value = _value(task)
    return value if isinstance(value, dict) else {}


def _that(task):
    that = _args(task).get("that")
    return [that] if isinstance(that, str) else list(that or [])


def _refs(task):
    """Every reference the task makes, excluding its name (a title may say "token")."""
    return yaml.safe_dump({k: v for k, v in task.items() if k != "name"}, sort_keys=True)


def _index(predicate):
    return [i for i, t in enumerate(_tasks()) if predicate(t)]


def _helper_argv(task):
    """The argv of a command task that calls the pinned helper, or None for any other task."""
    if _module(task) != "command":
        return None
    argv = _args(task).get("argv")
    if not isinstance(argv, list) or len(argv) < 3:
        return None
    if argv[0] != "{{ ansible_playbook_python }}" or argv[1] != "{{ _pve_helper }}":
        return None
    return argv


def _helper_tasks():
    return [t for t in _tasks() if _helper_argv(t) is not None]


def _is_get(task):
    return _helper_argv(task)[2] == "get"


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


# --- the play, its imports and its placement ----------------------------------------------------------------


def test_the_refusal_import_is_the_first_play():
    first = DOC[0]
    assert (first.get("ansible.builtin.import_playbook") or first.get("import_playbook")) == \
        "refuse-internal-extra-vars.yml"


def test_the_play_is_local_and_gathers_no_facts():
    play = _play()
    assert play["hosts"] == "localhost"
    assert play["connection"] == "local"
    assert play["gather_facts"] is False


def test_the_play_has_no_survey_or_extra_var_surface():
    # The one survey value (proxmox_leaf_sha256) is declared on the template, not as a prompt in the play.
    assert "vars_prompt" not in _play()


def test_the_helper_and_the_pin_are_the_play_vars_this_play_relies_on():
    variables = _play()["vars"]
    assert variables["_pve_helper"] == "{{ playbook_dir }}/../lib/pve_pinned_get.py"
    assert variables["_pve_pin"] == "{{ proxmox_leaf_sha256 | default('', true) | trim }}"
    assert HELPER.is_file()


# --- read-only ----------------------------------------------------------------------------------------------


def test_every_task_is_a_read_or_a_pinned_helper_call():
    for t in _tasks():
        if _helper_argv(t) is not None:
            continue
        assert _module(t) in READ_MODULES, (t.get("name"), _module(t))


def test_the_only_includes_are_the_two_transport_guards():
    includes = [_value(t) for t in _tasks() if _module(t) == "include_tasks"]
    assert includes == ["tasks/assert-bao-transport.yml", "tasks/assert-bao-transport.yml"], includes


def test_every_helper_call_is_a_read_through_the_pinned_helper_with_check_mode_false():
    calls = _helper_tasks()
    assert calls, "the play reads Proxmox only through the helper"
    for t in calls:
        argv = _helper_argv(t)
        assert argv[2] in HELPER_SUBCOMMANDS, (t.get("name"), argv[2])
        assert t.get("changed_when") is False, t.get("name")
        assert t.get("check_mode") is False, t.get("name")
        assert t.get("delegate_to") == "localhost", t.get("name")


def test_the_observe_call_sends_no_secret_and_is_the_handshake_only_fingerprint():
    (observe,) = [t for t in _helper_tasks() if _helper_argv(t)[2] == "fingerprint"]
    assert _helper_argv(observe) == ["{{ ansible_playbook_python }}", "{{ _pve_helper }}", "fingerprint",
                                     "--url", "{{ _pve_host }}"]
    assert "stdin" not in _args(observe)
    assert "_pve_secret" not in _refs(observe) and "_pve_token_id" not in _refs(observe)


def test_every_get_is_no_log_and_hands_the_token_only_on_stdin():
    gets = [t for t in _helper_tasks() if _is_get(t)]
    assert len(gets) == 2, [t.get("name") for t in gets]
    for t in gets:
        argv = _helper_argv(t)
        assert t.get("no_log") is True, t.get("name")
        assert _args(t).get("stdin") == "{{ _pve_token_id }}\n{{ _pve_secret }}", t.get("name")
        assert "_pve_secret" not in " ".join(argv) and "_pve_token_id" not in " ".join(argv), t.get("name")


def test_the_secret_is_referenced_only_by_the_secret_var_and_the_get_stdin():
    for t in _tasks():
        if _helper_argv(t) is not None and _is_get(t):
            continue
        refs = _refs(t)
        for name in SECRETS:
            assert not re.search(rf"\b{name}\b", refs), (t.get("name"), name)


def test_the_play_writes_nothing_to_a_host_a_store_or_the_source_tree():
    for t in _tasks():
        if _helper_argv(t) is not None:
            continue
        assert _module(t) not in WRITE_MODULES, (t.get("name"), _module(t))
    assert "vault_write" not in TEXT


def test_the_helper_sends_only_get_requests():
    assert HELPER_TEXT.count('"GET",') == 1, "exactly one request, a GET"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert f'"{method}"' not in HELPER_TEXT, method


def test_the_only_lookups_read_the_proxmox_secret_and_the_environment():
    lookups = set(re.findall(r"lookup\(\s*'([^']+)'", TEXT))
    assert lookups == {"env", "community.hashi_vault.hashi_vault"}, lookups
    paths = re.findall(r"'(secret/[^']+)'", TEXT)
    assert paths == ["secret/data/services/proxmox"], paths


# --- TLS and the token: no bypass, no inline header --------------------------------------------------------


def test_the_play_has_no_uri_task_no_module_defaults_and_no_tls_bypass():
    assert not [t for t in _tasks() if _module(t) == "uri"], "every request goes through the pinned helper"
    assert "module_defaults" not in _play()
    assert "validate_certs" not in TEXT, "TLS is pinned by the helper, not switched off here"


def test_the_playbook_carries_no_inline_proxmox_auth_header():
    assert "PVEAPIToken" not in TEXT, "the header is built in the helper, never in the playbook"
    assert "Authorization" not in TEXT


# --- order of the guards ------------------------------------------------------------------------------------


def test_the_token_id_is_asserted_before_the_first_request():
    guards = _index(lambda t: _module(t) == "assert" and "_pve_token_id | length > 0" in _that(t))
    first_request = _index(lambda t: _helper_argv(t) is not None)[0]
    assert guards and guards[0] < first_request, (guards, first_request)


def test_the_leaf_is_observed_and_the_pin_checked_before_the_first_token_use():
    observe = _index(lambda t: _helper_argv(t) is not None and _helper_argv(t)[2] == "fingerprint")
    pin_given = _index(lambda t: _module(t) == "assert" and "_pve_pin | length > 0" in _that(t))
    pin_matches = _index(lambda t: _module(t) == "assert" and "_observed_leaf" in " ".join(_that(t))
                         and "_pve_pin" in " ".join(_that(t)))
    first_get = _index(lambda t: _helper_argv(t) is not None and _is_get(t))[0]
    assert observe and pin_given and pin_matches, (observe, pin_given, pin_matches)
    assert observe[0] < pin_given[0] < pin_matches[0] < first_get, (observe, pin_given, pin_matches, first_get)


def test_every_get_sends_the_operators_pin_and_never_the_observed_digest():
    for t in _helper_tasks():
        if not _is_get(t):
            continue
        argv = _helper_argv(t)
        assert _flag(argv, "--sha256") == "{{ _pve_pin }}", t.get("name")
        assert "_observed_leaf" not in " ".join(argv), t.get("name")


def test_the_node_list_is_checked_before_the_certificate_read():
    node_check = _index(lambda t: _module(t) == "assert" and "_pve_nodes_ok | bool" in _that(t))
    cert_read = _index(lambda t: _helper_argv(t) is not None and _is_get(t)
                       and "certificates/info" in str(_flag(_helper_argv(t), "--path")))
    assert node_check and cert_read and node_check[0] < cert_read[0], (node_check, cert_read)


def test_the_checks_come_before_the_one_print():
    (debug,) = _index(lambda t: _module(t) == "debug")
    self_signed = _index(lambda t: _module(t) == "assert"
                         and "_pve_ca.subject == _pve_ca.issuer" in _that(t))
    unissued = _index(lambda t: _module(t) == "assert"
                      and "_pve_served_nodes_unissued | length == 0" in _that(t))
    issuers = _index(lambda t: _module(t) == "assert"
                     and "_pve_served_issuers | length == 1 and _pve_served_issuers[0] == _pve_ca.subject" in _that(t))
    public = _index(lambda t: _module(t) == "assert"
                    and "'-----BEGIN CERTIFICATE-----' in _pve_ca.pem" in _that(t))
    assert self_signed and unissued and issuers and public, (self_signed, unissued, issuers, public)
    assert max(self_signed + unissued + issuers + public) < debug, (self_signed, unissued, issuers, public, debug)
    assert debug == len(_tasks()) - 1, "the print is the last task"


def test_the_self_signed_check_is_guarded_and_the_issued_by_checks_read_only_derived_facts():
    for t in _tasks():
        if _module(t) == "assert" and "_pve_ca.subject == _pve_ca.issuer" in _that(t):
            assert "_pve_ca.subject | length > 0" in _that(t), t.get("name")
    # The serving nodes' pve-ssl check is computed under no_log; the assert reads only that derived list.
    derived = [t for t in _tasks() if "_pve_served_nodes_unissued" in (_args(t) or {})]
    assert len(derived) == 1 and derived[0].get("no_log") is True, derived


def test_the_observe_never_fails_itself_and_an_assert_names_its_exit_code():
    (observe,) = [t for t in _helper_tasks() if _helper_argv(t)[2] == "fingerprint"]
    assert observe.get("failed_when") is False, "a failed command prints its argv, which carries the API URL"
    named = _index(lambda t: _module(t) == "assert" and "_observed_leaf.rc | default(-1) == 0" in _that(t))
    pin = _index(lambda t: _module(t) == "assert" and "_pve_pin | length > 0" in _that(t))
    observed = _index(lambda t: t.get("name") == observe.get("name"))
    assert observed and named and pin, (observed, named, pin)
    assert observed[0] < named[0] < pin[0], (observed, named, pin)


def test_each_get_reason_is_derived_from_its_exit_code_under_no_log_and_no_visible_task_reads_stderr():
    for fact, register in (("_pve_nodes_reason", "_pve_nodes.rc"), ("_pve_read_reason", "_pve_read_list")):
        holders = [t for t in _tasks() if fact in (t.get("ansible.builtin.set_fact") or {})]
        assert len(holders) == 1 and holders[0].get("no_log") is True, fact
        value = str(holders[0]["ansible.builtin.set_fact"][fact])
        assert register in value and "stdout" not in value and "stderr" not in value, fact
        assert "_pve_helper_reasons" in value, fact
    for t in _tasks():
        if t.get("no_log") is True:
            continue
        assert re.search(r"\bstderr\b", _refs(t)) is None, t.get("name")


def test_every_online_node_is_read_through_the_pinned_helper_by_index_and_no_visible_task_loops_over_a_result():
    (reads,) = [t for t in _helper_tasks() if "loop" in t]
    assert reads.get("no_log") is True and reads.get("failed_when") is False, reads.get("name")
    assert reads["loop"] == "{{ _pve_online_nodes }}", reads["loop"]
    assert reads["loop_control"]["index_var"] == "_pve_node_idx", reads["loop_control"]
    assert _helper_argv(reads)[2] == "get", reads.get("name")
    assert _flag(_helper_argv(reads), "--path") == "/api2/json/nodes/{{ item }}/certificates/info"
    assert _flag(_helper_argv(reads), "--sha256") == "{{ _pve_pin }}"
    # The results are read back by index, in the one no_log collection that builds the per-node records.
    collect = [t for t in _tasks() if "_pve_read_list" in (t.get("ansible.builtin.set_fact") or {})]
    assert len(collect) == 1 and collect[0].get("no_log") is True
    assert collect[0]["loop"] == "{{ _pve_online_nodes }}"
    assert collect[0]["loop_control"]["index_var"] == "_pve_node_idx"
    assert "_pve_certs_all.results[_pve_node_idx]" in str(collect[0]["ansible.builtin.set_fact"]["_pve_read_list"])
    for t in _tasks():
        if t.get("no_log") is True:
            continue
        assert "loop" not in t, t.get("name")


def test_the_pinned_leaf_is_matched_across_every_online_node_and_every_match_must_be_issued_by_the_root_ca():
    (debug,) = _index(lambda t: _module(t) == "debug")
    derive = _index(lambda t: "_pve_served_list" in (_args(t) or {}))
    keep = _index(lambda t: "_pve_served_names" in (_args(t) or {}))
    count = _index(lambda t: _module(t) == "assert" and "_pve_served_count | int >= 1" in _that(t))
    unissued = _index(lambda t: _module(t) == "assert" and "_pve_served_nodes_unissued | length == 0" in _that(t))
    issuer_check = _index(lambda t: _module(t) == "assert"
                          and "_pve_served_issuers | length == 1 and _pve_served_issuers[0] == _pve_ca.subject"
                          in _that(t))
    assert derive and keep and count and unissued and issuer_check, (derive, keep, count, unissued, issuer_check)
    assert derive[0] < keep[0] < count[0] < unissued[0] < issuer_check[0] < debug, \
        (derive, keep, count, unissued, issuer_check, debug)
    # The names, nodes and issuers are scalars derived under no_log from the matches; no visible task reads the list.
    assert _tasks()[keep[0]].get("no_log") is True, _tasks()[keep[0]].get("name")
    # Matched over the entries of ALL nodes (not the first), by fingerprint through the normalising pattern.
    value = str(_args(_tasks()[derive[0]])["_pve_served_list"])
    assert "_pve_all_entries" in value and "fingerprint" in value and "_pve_pin_pattern" in value, value
    assert "first" not in value and "node" not in value, value
    assert "pveproxy-ssl.pem" not in value and "pve-ssl.pem" not in value
    pattern = [t for t in _tasks() if "_pve_pin_pattern" in (_args(t) or {})]
    assert len(pattern) == 1 and "(?i)^" in str(_args(pattern[0])["_pve_pin_pattern"])
    # The pin is the observed digest, so the pattern holds only its hex pairs.
    assert "regex_findall('..')" in str(_args(pattern[0])["_pve_pin_pattern"])


# --- output: one debug, derived facts only ------------------------------------------------------------------


def test_the_one_debug_references_only_the_derived_facts():
    (debug,) = [t for t in _tasks() if _module(t) == "debug"]
    msg = _args(debug).get("msg")
    assert isinstance(msg, str), msg
    names = set(re.findall(r"\b_[a-z]\w*", msg))
    assert names <= DEBUG_NAMES, names
    fields = set(re.findall(r"_pve_ca\.(\w+)", msg))
    assert fields <= DEBUG_FIELDS, fields
    assert SAN.search(msg) is None, msg


def test_protected_responses_are_touched_only_by_no_log_tasks():
    for t in _tasks():
        if t.get("no_log") is True:
            continue
        refs = _refs(t)
        for name in PROTECTED | SECRETS:
            assert not re.search(rf"\b{name}\b", refs), (t.get("name"), name)


def test_no_san_or_address_or_url_is_named_in_code():
    code = "\n".join(ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("#"))
    assert SAN.search(code) is None
    assert IPV4.search(TEXT) is None, IPV4.search(TEXT).group(0)
    assert URL_LITERAL.search(TEXT) is None, URL_LITERAL.search(TEXT).group(0)


# --- selection: basenames, every online node ----------------------------------------------------------------


def test_the_ca_and_the_node_certificate_are_selected_by_basename():
    assert "/etc/pve" not in TEXT
    names = set(re.findall(r"'filename', 'equalto', '([^']+)'", TEXT))
    assert names == {"pve-root-ca.pem", "pve-ssl.pem"}, names


def test_every_online_node_is_listed_in_name_order_and_no_single_node_is_chosen():
    # The stored URL may reach any node, and the leaf belongs to whichever node serves it, so the read covers every
    # online node. A "first node" selection here would make the served check depend on which node the URL hits.
    (pick,) = [t for t in _tasks() if "_pve_online_nodes" in (_args(t) or {})]
    expr = str(_args(pick)["_pve_online_nodes"])
    assert "selectattr('status', 'equalto', 'online')" in expr, expr
    assert "| sort" in expr and "| list" in expr, expr
    assert "first" not in expr, expr


# --- the template -------------------------------------------------------------------------------------------


def test_the_template_entry_is_a_dev_variant_with_one_optional_pin_survey_value():
    doc = yaml.safe_load(TEMPLATES.read_text())
    items = doc.get("templates", doc) if isinstance(doc, dict) else doc
    entries = [e for e in items if isinstance(e, dict) and e.get("name") == "Read Proxmox Root CA"]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["playbook"] == "platform/playbooks/read-proxmox-root-ca.yml"
    assert entry["dev_variant"] is True
    survey = entry["survey_vars"]
    assert [v["name"] for v in survey] == ["proxmox_leaf_sha256"], survey
    var = survey[0]
    assert var["type"] == "string"
    assert var["required"] is False
    assert var["default_value"] == ""
    assert "observe" in var["description"].lower()
    assert not var["name"].startswith("_")


# --- behaviour: the real tasks, run against a loopback HTTPS Proxmox API ------------------------------------
#
# The playbook's selection and checks are Jinja over JSON, so they run here, not only as text. The harness keeps
# the real play's tasks, including the pinned helper calls, and replaces the OpenBao lookup with literal vars and
# the two transport includes, which need the tasks/ directory beside the playbook. The helper path is overridden
# to the absolute repo path, because the harness file lives in tmp_path. The lookup and transport are covered by
# the structure tests above and by the token-id guard.

ROOT_SUBJECT = "CN=PVE Root CA"
ROOT_PEM = "-----BEGIN CERTIFICATE-----\nMIIFakeRootCA\n-----END CERTIFICATE-----\n"
TOKEN_ID = "automation@pve!fixture"
SENT_PVE = "SENTINEL-PROXMOX-TOKEN-4c1d"
SAN_ADDRESS = "192.0.2.7"
LEAF_PEM = "-----BEGIN CERTIFICATE-----\nMIILeafNotPrinted\n-----END CERTIFICATE-----\n"
# Built from parts so the literal private-key block header never appears in source (detect-private-key gate).
PRIVATE_KEY_PEM = "-----BEGIN " + "PRIVATE KEY-----\nMIIFake\n-----END " + "PRIVATE KEY-----\n"
# A fingerprint placeholder in a parametrised case: the test swaps it for the generated leaf's digest, which a
# parametrise argument cannot see (the leaf is a fixture).
LEAF_TOKEN = "<leaf>"
OTHER_ROOT_FP = ":".join(["11"] * 32)


@pytest.fixture(scope="module")
def leaf(tmp_path_factory):
    return _generate_leaf(tmp_path_factory.mktemp("root-ca-leaf"))


def _entry(filename, subject, issuer, pem=ROOT_PEM, fingerprint="AA:BB:CC:DD", omit_issuer=False):
    # The same fields pve-common get_certificate_info returns; san carries an address that must never print.
    # omit_issuer drops the issuer key, for an entry the endpoint returned without one.
    entry = {"filename": filename, "fingerprint": fingerprint, "subject": subject, "issuer": issuer,
             "notafter": 1900000000, "notbefore": 1800000000, "san": [SAN_ADDRESS], "pem": pem}
    if omit_issuer:
        del entry["issuer"]
    return entry


def _good_certs(node, leaf=None):
    # A node's own pve-ssl.pem, issued by the root. Given the leaf, this node serves it: its pve-ssl.pem is the leaf
    # the pin names. Without it, the node holds some other certificate. pveproxy-ssl.pem is a different certificate.
    served = leaf.colon if leaf is not None else "AA:BB:CC:DD"
    return {node: [
        _entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
        _entry("pve-ssl.pem", f"CN={node}", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=served),
        _entry("pveproxy-ssl.pem", f"CN={node}", ROOT_SUBJECT, pem=LEAF_PEM),
    ]}


def _with_leaf(certs, leaf):
    """A copy of `certs` with every LEAF_TOKEN fingerprint replaced by the generated leaf's digest."""
    out = copy.deepcopy(certs)
    for entries in out.values():
        for entry in entries:
            if entry["fingerprint"] == LEAF_TOKEN:
                entry["fingerprint"] = leaf.colon
    return out


def _fake_proxmox(nodes, certs, leaf):
    """A loopback HTTPS Proxmox API: the node list and the certificate info of each node. Records every request."""
    seen = []
    prefix = "/api2/json/nodes/"
    suffix = "/certificates/info"

    class Handler(DrainingHandler):
        def do_GET(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            if self.path == "/api2/json/nodes":
                return self._send(200, {"data": nodes})
            if self.path.startswith(prefix) and self.path.endswith(suffix):
                node = self.path[len(prefix):-len(suffix)]
                if node in certs:
                    return self._send(200, {"data": certs[node]})
            return self._send(404, {"errors": {"path": "not found"}})

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(str(leaf.cert_path), str(leaf.key_path))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _stop(server):
    server.shutdown()
    server.server_close()


def _harness(tmp_path, pve_url, token_id, pin=None):
    real = copy.deepcopy(_play())
    tasks = [t for t in real["tasks"] if _module(t) != "include_tasks"]
    variables = {
        "_pve_host": pve_url,
        "_pve_token_id": token_id,
        # The secret is read from the environment, as the real play reads it from the store: the harness file
        # never holds the literal, so no error excerpt can echo it.
        "_pve_secret": "{{ lookup('env', 'HARNESS_PVE_SECRET') }}",
        "_pve_helper": str(HELPER),
        # The real pin expression, so the harness exercises the same value the operator's survey value feeds.
        "_pve_pin": real["vars"]["_pve_pin"],
        # The real exit-code table: the named refusals are read from the playbook, not restated here.
        "_pve_helper_reasons": real["vars"]["_pve_helper_reasons"],
    }
    if pin is not None:
        variables["proxmox_leaf_sha256"] = pin
    play = {
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": variables,
        "tasks": tasks,
    }
    path = tmp_path / "read-root-ca-harness.yml"
    path.write_text(yaml.safe_dump([play], sort_keys=False))
    return path


def _run(path, verbosity="-vvv"):
    """Run the harness. verbosity=None is the default output, the one an operator reads without -v."""
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    env["HARNESS_PVE_SECRET"] = SENT_PVE
    cmd = ["ansible-playbook", "-i", "localhost,"] + ([verbosity] if verbosity else []) + [str(path)]
    return subprocess.run(cmd, cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=240)


def _printed_pem(out):
    """The PEM as a YAML double-quoted body, read back from the printed (JSON-escaped) msg line."""
    line = next(ln for ln in out.splitlines() if ln.lstrip().startswith('"msg": "Proxmox cluster root CA'))
    raw = line.split('"msg": "', 1)[1]
    raw = raw[: raw.rindex('"')]
    marker = "paste between double quotes):" + "\\n"
    body = raw.split(marker, 1)[1]
    return yaml.safe_load('pem: "' + body + '"')["pem"]


@needs_ansible
@pytest.mark.parametrize("serving", ["pve-b", "pve-c"])
def test_the_leaf_is_found_on_whichever_online_node_holds_it_and_only_the_root_ca_is_printed(tmp_path, leaf, serving):
    # Two online nodes and one offline. The pinned leaf is on `serving`; the other node holds a different certificate.
    # serving="pve-c" is the second node: the case a first-node-only search misses.
    nodes = [{"node": "pve-b", "status": "online"}, {"node": "pve-a", "status": "offline"},
             {"node": "pve-c", "status": "online"}]
    certs = _good_certs("pve-b", leaf if serving == "pve-b" else None) \
        | _good_certs("pve-c", leaf if serving == "pve-c" else None)
    server, seen = _fake_proxmox(nodes, certs, leaf)
    try:
        # The lowercase, colon-free form of the pin: the playbook and the helper both normalise it.
        done = _run(_harness(tmp_path, f"https://127.0.0.1:{server.server_address[1]}", TOKEN_ID,
                             pin=leaf.flat.lower()))
    finally:
        _stop(server)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out[-2000:]
    # The node list, then one certificate-info GET per online node in name order, each with the admin header.
    # The offline node is not read.
    assert [(m, p) for m, p, _ in seen] == [
        ("GET", "/api2/json/nodes"),
        ("GET", "/api2/json/nodes/pve-b/certificates/info"),
        ("GET", "/api2/json/nodes/pve-c/certificates/info")], seen
    assert {auth for _, _, auth in seen} == {f"PVEAPIToken={TOKEN_ID}={SENT_PVE}"}
    assert "read from 2 online nodes, which agree on it." in out
    assert f"subject: {ROOT_SUBJECT}" in out
    assert _printed_pem(out) == ROOT_PEM.strip()
    # The matching entry is found on the node that holds the leaf, by fingerprint (pve-ssl.pem, not the pveproxy entry).
    assert f"listed as: {serving}/pve-ssl.pem, issued by this root CA." in out
    # The observed digest is printed by the observe step (public); the token and the response carry nothing else.
    assert leaf.colon in out
    for leaked in (SENT_PVE, "PVEAPIToken", SAN_ADDRESS, "MIILeafNotPrinted"):
        assert leaked not in out, leaked


@needs_ansible
def test_a_successful_multi_node_run_prints_no_address_at_default_verbosity(tmp_path, leaf):
    # The per-node reads are no_log, so their responses (node addresses) never print; the default output carries only
    # the derived debug. -v and above print the observe argv, which carries the API URL, so this runs without -v.
    nodes = [{"node": "pve-b", "status": "online"}, {"node": "pve-c", "status": "online"}]
    certs = _good_certs("pve-b") | _good_certs("pve-c", leaf)
    server, _ = _fake_proxmox(nodes, certs, leaf)
    url = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        done = _run(_harness(tmp_path, url, TOKEN_ID, pin=leaf.colon), verbosity=None)
    finally:
        _stop(server)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out[-2000:]
    assert "listed as: pve-c/pve-ssl.pem, issued by this root CA." in out, out[-1500:]
    assert url not in out and "127.0.0.1" not in out, out[-1500:]
    assert SAN_ADDRESS not in out and SENT_PVE not in out and "PVEAPIToken" not in out


def _run_pinned(tmp_path, leaf, nodes, certs):
    """Run the harness on a fake Proxmox serving `certs`; returns (done, seen)."""
    server, seen = _fake_proxmox(nodes, _with_leaf(certs, leaf), leaf)
    try:
        done = _run(_harness(tmp_path, f"https://127.0.0.1:{server.server_address[1]}", TOKEN_ID, pin=leaf.colon))
    finally:
        _stop(server)
    return done, seen


@needs_ansible
def test_the_pin_listed_by_two_nodes_passes_and_both_entries_are_named(tmp_path, leaf):
    # The same certificate is pve-ssl.pem on pve-b and on pve-c: identical fingerprint, one root. Both are named.
    nodes = [{"node": "pve-b", "status": "online"}, {"node": "pve-c", "status": "online"}]
    certs = {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                       _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)],
             "pve-c": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                       _entry("pve-ssl.pem", "CN=pve-c", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)]}
    done, _ = _run_pinned(tmp_path, leaf, nodes, certs)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out[-2000:]
    assert "listed as: pve-b/pve-ssl.pem, pve-c/pve-ssl.pem, issued by this root CA." in out, out[-1500:]
    assert _printed_pem(out) == ROOT_PEM.strip()
    for leaked in (SENT_PVE, "PVEAPIToken", SAN_ADDRESS, "MIILeafNotPrinted"):
        assert leaked not in out, leaked


@needs_ansible
def test_the_pin_listed_by_two_entries_on_one_node_passes_and_both_entries_are_named(tmp_path, leaf):
    # pve-ssl.pem and pveproxy-ssl.pem on pve-b are the same certificate, both root-issued: both are named, and the
    # serving node's pve-ssl.pem is the root-issued one it must be.
    nodes = [{"node": "pve-b", "status": "online"}]
    certs = {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                       _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN),
                       _entry("pveproxy-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)]}
    done, _ = _run_pinned(tmp_path, leaf, nodes, certs)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out[-2000:]
    assert "listed as: pve-b/pve-ssl.pem, pve-b/pveproxy-ssl.pem, issued by this root CA." in out, out[-1500:]
    assert _printed_pem(out) == ROOT_PEM.strip()
    for leaked in (SENT_PVE, "PVEAPIToken", SAN_ADDRESS, "MIILeafNotPrinted"):
        assert leaked not in out, leaked


@needs_ansible
def test_a_non_serving_node_whose_pve_ssl_has_no_issuer_does_not_block_the_pinned_leaf(tmp_path, leaf):
    # pve-c does not serve the pinned leaf, and its pve-ssl.pem came back without an issuer. The served-node check must
    # not read it, so the run passes on pve-b, the node that serves the leaf.
    nodes = [{"node": "pve-b", "status": "online"}, {"node": "pve-c", "status": "online"}]
    certs = {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                       _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)],
             "pve-c": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                       _entry("pve-ssl.pem", "CN=pve-c", None, pem=LEAF_PEM, omit_issuer=True)]}
    done, _ = _run_pinned(tmp_path, leaf, nodes, certs)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out[-2000:]
    assert "listed as: pve-b/pve-ssl.pem, issued by this root CA." in out, out[-1500:]
    assert _printed_pem(out) == ROOT_PEM.strip()
    for leaked in (SENT_PVE, "PVEAPIToken", SAN_ADDRESS, "MIILeafNotPrinted"):
        assert leaked not in out, leaked


@needs_ansible
def test_a_missing_pin_stops_before_the_token_is_used(tmp_path, leaf):
    nodes = [{"node": "pve-b", "status": "online"}]
    server, seen = _fake_proxmox(nodes, _good_certs("pve-b", leaf), leaf)
    try:
        done = _run(_harness(tmp_path, f"https://127.0.0.1:{server.server_address[1]}", TOKEN_ID, pin=None))
    finally:
        _stop(server)
    out = done.stdout + done.stderr
    assert done.returncode != 0, out[-1500:]
    assert "No proxmox_leaf_sha256 was given" in out, out[-1500:]
    # The first run's job: the operator gets the digest to confirm, and nothing else is sent.
    assert leaf.colon in out
    assert seen == [], "a request reached Proxmox without a pin"
    assert SENT_PVE not in out and "PVEAPIToken" not in out


@needs_ansible
def test_a_wrong_pin_stops_before_the_token_is_used(tmp_path, leaf):
    wrong = hashlib.sha256(b"not the leaf certificate").hexdigest()
    nodes = [{"node": "pve-b", "status": "online"}]
    server, seen = _fake_proxmox(nodes, _good_certs("pve-b", leaf), leaf)
    try:
        done = _run(_harness(tmp_path, f"https://127.0.0.1:{server.server_address[1]}", TOKEN_ID, pin=wrong))
    finally:
        _stop(server)
    out = done.stdout + done.stderr
    assert done.returncode != 0, out[-1500:]
    assert "does not match the API leaf certificate observed from this run" in out, out[-1500:]
    assert seen == [], "a request reached Proxmox under a pin that does not match"
    assert SENT_PVE not in out and "PVEAPIToken" not in out


@needs_ansible
@pytest.mark.parametrize("case,nodes,certs,token_id,expected", [
    ("token id empty", [{"node": "pve-b", "status": "online"}], {}, "", "token_id"),
    ("no online node", [{"node": "pve-b", "status": "offline"}], {}, TOKEN_ID, "names no online node"),
    ("root not self-signed", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", "CN=Intermediate", ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM)]}, TOKEN_ID,
     "is not self-signed"),
    # The node serves the leaf (LEAF_TOKEN, swapped for the digest in the test) but its pve-ssl.pem is not root-issued.
    ("node cert not issued by the root", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", "CN=Other CA", pem=LEAF_PEM, fingerprint=LEAF_TOKEN)]}, TOKEN_ID,
     "The pve-ssl.pem entry of pve-b is absent, or it was not issued by the root CA"),
    ("root entry is not a public certificate", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT,
                       pem=PRIVATE_KEY_PEM),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM)]}, TOKEN_ID,
     "The certificate endpoint did not return pve-root-ca.pem as a public certificate"),
    ("no certificate entry matches the pin", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM)]}, TOKEN_ID,
     "No online node lists the pinned leaf certificate"),
    ("no node lists the pin, on two nodes", [{"node": "pve-b", "status": "online"},
                                             {"node": "pve-c", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM)],
      "pve-c": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-c", ROOT_SUBJECT, pem=LEAF_PEM)]}, TOKEN_ID,
     "No online node lists the pinned leaf certificate"),
    # The serving node's pve-ssl.pem came back without an issuer: it is not root-issued, so the served-node check names
    # the node as the refusal (not a crash in the no_log read).
    ("serving node's pve-ssl.pem has no issuer", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", None, pem=LEAF_PEM, fingerprint=LEAF_TOKEN, omit_issuer=True)]},
     TOKEN_ID, "The pve-ssl.pem entry of pve-b is absent, or it was not issued by the root CA"),
    # A listing of the pin came back without an issuer, beside a root-issued one: the match's issuer check names it.
    ("a matching listing has no issuer, beside a root-issued one", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN),
                _entry("pveproxy-ssl.pem", "CN=pve-b", None, pem=LEAF_PEM, fingerprint=LEAF_TOKEN,
                       omit_issuer=True)]}, TOKEN_ID,
     "The pinned leaf is listed by pve-b/pve-ssl.pem, pve-b/pveproxy-ssl.pem, and at least one of those entries is "
     "not issued by the cluster root CA"),
    # The serving node's pve-ssl.pem is absent. Its pveproxy-ssl.pem matches the pin and is root-issued, so the issuer
    # check of the matches passes and only the serving-node check refuses (its absent-path arm).
    ("serving node has no pve-ssl.pem", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pveproxy-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)]}, TOKEN_ID,
     "The pve-ssl.pem entry of pve-b is absent, or it was not issued by the root CA"),
    # The serving node's pve-ssl.pem is foreign (not matching the pin). Its root-issued pveproxy-ssl.pem matches, so
    # only the serving-node check refuses (its foreign-issuer arm).
    ("serving node's pve-ssl.pem is foreign, its pveproxy-ssl.pem is root-issued",
     [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", "CN=Other CA", pem=LEAF_PEM),
                _entry("pveproxy-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)]}, TOKEN_ID,
     "The pve-ssl.pem entry of pve-b is absent, or it was not issued by the root CA"),
    # Two listings across two nodes, one not issued by the root: every listing is named, the foreign one refuses. The
    # serving nodes' own pve-ssl.pem entries are root-issued, so only the matches' issuer check refuses.
    ("a listing the root did not issue, beside root-issued listings",
     [{"node": "pve-b", "status": "online"}, {"node": "pve-c", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN),
                _entry("pveproxy-ssl.pem", "CN=pve-b", "CN=Example ACME CA", pem=LEAF_PEM, fingerprint=LEAF_TOKEN)],
      "pve-c": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-c", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)]}, TOKEN_ID,
     "The pinned leaf is listed by pve-b/pve-ssl.pem, pve-b/pveproxy-ssl.pem, pve-c/pve-ssl.pem, and at least one of "
     "those entries is not issued by the cluster root CA"),
    # Two online nodes return different copies of the cluster root CA: the cluster has no single CA to print.
    ("nodes disagree on the cluster root CA", [{"node": "pve-b", "status": "online"},
                                               {"node": "pve-c", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT, fingerprint=OTHER_ROOT_FP),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM)],
      "pve-c": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-c", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)]}, TOKEN_ID,
     "The nodes disagree on the cluster root CA"),
    ("no online node returns the root CA", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM, fingerprint=LEAF_TOKEN)]}, TOKEN_ID,
     "No online node returned pve-root-ca.pem"),
])
def test_each_refusal_stops_before_anything_is_printed(tmp_path, leaf, case, nodes, certs, token_id, expected):
    certs = _with_leaf(certs, leaf)
    server, seen = _fake_proxmox(nodes, certs, leaf)
    try:
        done = _run(_harness(tmp_path, f"https://127.0.0.1:{server.server_address[1]}", token_id,
                             pin=leaf.colon))
    finally:
        _stop(server)
    out = done.stdout + done.stderr
    assert done.returncode != 0, (case, out[-1500:])
    assert expected in out, (case, out[-1500:])
    assert "MIIFakeRootCA" not in out and "MIILeafNotPrinted" not in out, case
    assert SENT_PVE not in out and "PVEAPIToken" not in out, case
    if token_id == "":
        assert seen == [], "a request was sent before the token id was checked"


@needs_ansible
def test_a_custom_served_certificate_is_named_by_its_basename_and_nothing_is_printed(tmp_path, leaf):
    # The pinned connection presents pveproxy-ssl.pem (custom, matching the pin, issued by some other CA). The
    # node's own pve-ssl.pem is root-issued and does not match the pin, so only the served check can refuse it.
    certs = {"pve-b": [
        _entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
        _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM),
        _entry("pveproxy-ssl.pem", "CN=pve-b", "CN=Example ACME CA", pem=LEAF_PEM, fingerprint=leaf.colon.lower()),
    ]}
    server, _ = _fake_proxmox([{"node": "pve-b", "status": "online"}], certs, leaf)
    try:
        done = _run(_harness(tmp_path, f"https://127.0.0.1:{server.server_address[1]}", TOKEN_ID,
                             pin=leaf.flat.lower()), verbosity=None)
    finally:
        _stop(server)
    out = done.stdout + done.stderr
    assert done.returncode != 0, out[-1500:]
    assert ("The pinned leaf is listed by pve-b/pveproxy-ssl.pem, and at least one of those entries is not issued by "
            "the cluster root CA: a custom or ACME certificate?") in out, out[-1500:]
    assert "MIIFakeRootCA" not in out and "MIILeafNotPrinted" not in out, out[-1500:]
    assert SENT_PVE not in out and "PVEAPIToken" not in out


@needs_ansible
def test_an_unreachable_api_is_named_by_exit_code_and_its_url_is_not_printed_at_default_verbosity(tmp_path, leaf):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    url = f"https://127.0.0.1:{port}"
    done = _run(_harness(tmp_path, url, TOKEN_ID, pin=leaf.colon), verbosity=None)
    out = done.stdout + done.stderr
    assert done.returncode != 0, out[-1500:]
    assert ("The API leaf certificate could not be observed from this run: a connection or TLS failure"
            in out), out[-1500:]
    # The failed command task would print its argv, which carries this URL; the default output must not.
    assert url not in out and "127.0.0.1" not in out, out[-1500:]
    assert SENT_PVE not in out and "PVEAPIToken" not in out


@needs_ansible
def test_a_refused_certificate_read_is_named_by_exit_code_and_its_url_is_not_printed_at_default_verbosity(
        tmp_path, leaf):
    # The node is online but has no certificate entry in the fake, so the certificate read answers 404: the helper
    # exits 4 after the pin held and the token was sent. The named reason is the only thing that may print.
    server, seen = _fake_proxmox([{"node": "pve-b", "status": "online"}], {}, leaf)
    url = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        done = _run(_harness(tmp_path, url, TOKEN_ID, pin=leaf.colon), verbosity=None)
    finally:
        _stop(server)
    out = done.stdout + done.stderr
    assert done.returncode != 0, out[-1500:]
    assert seen and seen[-1][1] == "/api2/json/nodes/pve-b/certificates/info", seen
    assert ("The certificate information was not read from every online node. First failure, by node name: "
            "pve-b: the API answered with a status other than 200") in out, out[-1500:]
    assert url not in out and "127.0.0.1" not in out, out[-1500:]
    assert SENT_PVE not in out and "PVEAPIToken" not in out


def test_the_approle_comes_from_the_orchestrator_variables_before_the_environment():
    # Production Semaphore supplies bao_role_id/bao_secret_id as variables; an environment-only read
    # failed there with "missing role_id" (Semaphore task 10407, 2026-10-30).
    play_vars = next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if isinstance(p, dict) and "vars" in p)["vars"]
    assert play_vars["_bao_role_id"].startswith("{{ bao_role_id | default(")
    assert play_vars["_bao_secret_id"].startswith("{{ bao_secret_id | default(")
