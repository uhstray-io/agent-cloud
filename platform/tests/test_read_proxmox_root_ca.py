"""The read-only Proxmox root CA read (platform/playbooks/read-proxmox-root-ca.yml, "Read Proxmox Root CA").

Structure, read from the parsed playbook:
- order: refuse-internal-extra-vars.yml is the first play; the stored token id is asserted before the first
  request; the node-list check precedes the certificate read; the self-signed and issued-by checks precede the
  one debug;
- read-only: every uri is a GET with check_mode false and no_log true; no write module; the only lookups read
  secret/data/services/proxmox and the environment; the play writes to neither OpenBao nor Proxmox;
- TLS: validate_certs is false in the play's module_defaults only, the admin channel of provision-vm.yml:27;
- no protected output: the one debug references only _pve_node and _pve_ca; the node list and the certificate
  response are referenced only by no_log tasks; no SAN is named in code;
- selection: the root CA and the node certificate are chosen by basename, never by a full path.
"""

import copy
import json
import os
import re
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/read-proxmox-root-ca.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"

TEXT = PLAYBOOK.read_text()
DOC = yaml.safe_load(TEXT)
TASK_KEYWORDS = {
    "name", "register", "when", "no_log", "vars", "delegate_to", "loop", "tags", "check_mode",
    "changed_when", "failed_when", "become", "ignore_errors", "notify", "module_defaults",
}
PREFIX = "ansible.builtin."
READ_MODULES = {"uri", "set_fact", "assert", "debug", "include_tasks"}
WRITE_MODULES = {
    "copy", "file", "template", "lineinfile", "blockinfile", "replace", "command", "shell", "raw",
    "script", "unarchive", "get_url", "archive", "fetch",
}
# Responses that carry node addresses (the SAN of each entry) or the admin token's answers: visible tasks never
# touch them.
PROTECTED = {"_pve_nodes", "_pve_certs"}
SECRETS = {"_pve_secret", "_pve_data"}
DEBUG_NAMES = {"_pve_node", "_pve_ca"}
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
    # Read-only and launched without survey values: the play takes no input beyond the store.
    assert "vars_prompt" not in _play()


# --- read-only ----------------------------------------------------------------------------------------------


def test_every_task_is_a_read_or_a_local_check():
    for t in _tasks():
        assert _module(t) in READ_MODULES, (t.get("name"), _module(t))


def test_the_only_includes_are_the_two_transport_guards():
    includes = [_value(t) for t in _tasks() if _module(t) == "include_tasks"]
    assert includes == ["tasks/assert-bao-transport.yml", "tasks/assert-bao-transport.yml"], includes


def test_every_uri_is_a_get_with_check_mode_false_and_no_log():
    uris = [t for t in _tasks() if _module(t) == "uri"]
    assert len(uris) == 2, [t.get("name") for t in uris]
    for t in uris:
        assert _args(t).get("method") == "GET", t.get("name")
        assert t.get("check_mode") is False, t.get("name")
        assert t.get("no_log") is True, t.get("name")


def test_the_play_writes_nothing_to_a_host_a_store_or_the_source_tree():
    for t in _tasks():
        assert _module(t) not in WRITE_MODULES, (t.get("name"), _module(t))
    methods = {str(_args(t).get("method", "GET")).upper() for t in _tasks() if _module(t) == "uri"}
    assert methods == {"GET"}, methods
    assert "vault_write" not in TEXT


def test_the_only_lookups_read_the_proxmox_secret_and_the_environment():
    lookups = set(re.findall(r"lookup\(\s*'([^']+)'", TEXT))
    assert lookups == {"env", "community.hashi_vault.hashi_vault"}, lookups
    paths = re.findall(r"'(secret/[^']+)'", TEXT)
    assert paths == ["secret/data/services/proxmox"], paths


# --- TLS: the admin channel's posture, in this play only ----------------------------------------------------


def test_validate_certs_is_false_only_in_the_play_module_defaults():
    defaults = _play()["module_defaults"]["ansible.builtin.uri"]
    assert defaults["validate_certs"] is False
    code_lines = [ln for ln in TEXT.splitlines() if "validate_certs" in ln and not ln.lstrip().startswith("#")]
    assert len(code_lines) == 1, code_lines
    for t in _tasks():
        if _module(t) == "uri":
            assert "validate_certs" not in _args(t), t.get("name")


def test_the_admin_header_is_the_proxmox_token_from_the_store():
    headers = _play()["module_defaults"]["ansible.builtin.uri"]["headers"]
    assert headers == {"Authorization": "PVEAPIToken={{ _pve_token_id }}={{ _pve_secret }}"}, headers
    assert _play()["vars"]["_pve_token_id"] == "{{ _pve_data.token_id | default('') }}"
    assert _play()["vars"]["_pve_secret"] == "{{ _pve_data.api_token }}"


# --- order of the guards ------------------------------------------------------------------------------------


def test_the_token_id_is_asserted_before_the_first_request():
    guards = _index(lambda t: _module(t) == "assert" and "_pve_token_id | length > 0" in _that(t))
    first_request = _index(lambda t: _module(t) == "uri")[0]
    assert guards and guards[0] < first_request, (guards, first_request)


def test_the_node_list_is_checked_before_the_certificate_read():
    node_check = _index(lambda t: _module(t) == "assert" and "_pve_nodes_ok | bool" in _that(t))
    cert_read = _index(lambda t: _module(t) == "uri" and "certificates/info" in str(_args(t).get("url")))
    assert node_check and cert_read and node_check[0] < cert_read[0], (node_check, cert_read)


def test_the_checks_come_before_the_one_print():
    (debug,) = _index(lambda t: _module(t) == "debug")
    self_signed = _index(lambda t: _module(t) == "assert"
                         and "_pve_ca.subject == _pve_ca.issuer" in _that(t))
    issued_by = _index(lambda t: _module(t) == "assert"
                       and "_pve_node_issuer == _pve_ca.subject" in _that(t))
    public = _index(lambda t: _module(t) == "assert"
                    and "'-----BEGIN CERTIFICATE-----' in _pve_ca.pem" in _that(t))
    assert self_signed and issued_by and public, (self_signed, issued_by, public)
    assert max(self_signed + issued_by + public) < debug, (self_signed, issued_by, public, debug)
    assert debug == len(_tasks()) - 1, "the print is the last task"


def test_the_self_signed_check_and_the_issued_by_check_are_both_assertions_on_derived_facts():
    for t in _tasks():
        if _module(t) == "assert" and "_pve_ca.subject == _pve_ca.issuer" in _that(t):
            assert "_pve_ca.subject | length > 0" in _that(t), t.get("name")
        if _module(t) == "assert" and "_pve_node_issuer == _pve_ca.subject" in _that(t):
            assert "_pve_node_issuer | length > 0" in _that(t), t.get("name")


# --- output: one debug, derived facts only ------------------------------------------------------------------


def test_the_one_debug_references_only_the_derived_facts():
    (debug,) = [t for t in _tasks() if _module(t) == "debug"]
    msg = _args(debug).get("msg")
    assert isinstance(msg, str), msg
    names = set(re.findall(r"\b_pve_\w+", msg))
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


# --- selection: basenames, the first online node -----------------------------------------------------------


def test_the_ca_and_the_node_certificate_are_selected_by_basename():
    assert "/etc/pve" not in TEXT
    names = set(re.findall(r"'filename', 'equalto', '([^']+)'", TEXT))
    assert names == {"pve-root-ca.pem", "pve-ssl.pem"}, names


def test_the_node_is_the_first_online_node_by_name():
    (pick,) = [t for t in _tasks() if "_pve_node" in (_args(t) or {})]
    expr = _args(pick)["_pve_node"]
    assert "selectattr('status', 'equalto', 'online')" in expr, expr
    assert "| sort" in expr and "| first" in expr, expr


# --- the template -------------------------------------------------------------------------------------------


def test_the_template_entry_is_a_dev_variant_with_no_survey_vars():
    doc = yaml.safe_load(TEMPLATES.read_text())
    items = doc.get("templates", doc) if isinstance(doc, dict) else doc
    entries = [e for e in items if isinstance(e, dict) and e.get("name") == "Read Proxmox Root CA"]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["playbook"] == "platform/playbooks/read-proxmox-root-ca.yml"
    assert entry["dev_variant"] is True
    assert "survey_vars" not in entry


# --- behaviour: the real tasks, run against a loopback Proxmox API -----------------------------------------
#
# The playbook's selection and checks are Jinja over JSON, so they run here, not only as text. The harness keeps
# the real play's tasks and module_defaults (the admin header, validate_certs false) and replaces the OpenBao
# lookup with literal vars and the two transport includes, which need the tasks/ directory beside the playbook.
# The lookup and transport are covered by the structure tests above and by the token-id guard.

ROOT_SUBJECT = "CN=PVE Root CA"
ROOT_PEM = "-----BEGIN CERTIFICATE-----\nMIIFakeRootCA\n-----END CERTIFICATE-----\n"
TOKEN_ID = "automation@pve!fixture"
SENT_PVE = "SENTINEL-PROXMOX-TOKEN-4c1d"
SAN_ADDRESS = "192.0.2.7"
LEAF_PEM = "-----BEGIN CERTIFICATE-----\nMIILeafNotPrinted\n-----END CERTIFICATE-----\n"
# Built from parts so the literal private-key block header never appears in source (detect-private-key gate).
PRIVATE_KEY_PEM = "-----BEGIN " + "PRIVATE KEY-----\nMIIFake\n-----END " + "PRIVATE KEY-----\n"


def _entry(filename, subject, issuer, pem=ROOT_PEM):
    # The same fields pve-common get_certificate_info returns; san carries an address that must never print.
    return {"filename": filename, "fingerprint": "AA:BB:CC:DD", "subject": subject, "issuer": issuer,
            "notafter": 1900000000, "notbefore": 1800000000, "san": [SAN_ADDRESS], "pem": pem}


def _good_certs(node):
    return {node: [
        _entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
        _entry("pve-ssl.pem", f"CN={node}", ROOT_SUBJECT, pem=LEAF_PEM),
        _entry("pveproxy-ssl.pem", f"CN={node}", ROOT_SUBJECT, pem=LEAF_PEM),
    ]}


def _fake_proxmox(nodes, certs):
    """A loopback Proxmox API: the node list and the certificate info of each node. Records every request."""
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _harness(tmp_path, pve_url, token_id):
    real = copy.deepcopy(_play())
    tasks = [t for t in real["tasks"] if _module(t) != "include_tasks"]
    play = {
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "module_defaults": real["module_defaults"],
        # The secret is read from the environment, as the real play reads it from the store: the harness file
        # never holds the literal, so no error excerpt can echo it.
        "vars": {"_pve_host": pve_url, "_pve_token_id": token_id,
                 "_pve_secret": "{{ lookup('env', 'HARNESS_PVE_SECRET') }}"},
        "tasks": tasks,
    }
    path = tmp_path / "read-root-ca-harness.yml"
    path.write_text(yaml.safe_dump([play], sort_keys=False))
    return path


def _run(path):
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    env["HARNESS_PVE_SECRET"] = SENT_PVE
    return subprocess.run(["ansible-playbook", "-i", "localhost,", "-vvv", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=240)


def _printed_pem(out):
    """The PEM as a YAML double-quoted body, read back from the printed (JSON-escaped) msg line."""
    line = next(ln for ln in out.splitlines() if ln.lstrip().startswith('"msg": "Proxmox cluster root CA'))
    raw = line.split('"msg": "', 1)[1]
    raw = raw[: raw.rindex('"')]
    marker = "paste between double quotes):" + "\\n"
    body = raw.split(marker, 1)[1]
    return yaml.safe_load('pem: "' + body + '"')["pem"]


@needs_ansible
def test_the_run_reads_the_first_online_node_and_prints_the_root_ca_only(tmp_path):
    nodes = [{"node": "pve-b", "status": "online"}, {"node": "pve-a", "status": "offline"},
             {"node": "pve-c", "status": "online"}]
    server, seen = _fake_proxmox(nodes, _good_certs("pve-b") | _good_certs("pve-c"))
    try:
        done = _run(_harness(tmp_path, f"http://127.0.0.1:{server.server_address[1]}", TOKEN_ID))
    finally:
        server.shutdown()
    out = done.stdout + done.stderr
    assert done.returncode == 0, out[-2000:]
    # Only the node list and the first online node's certificate info are read, both as GET with the admin header.
    assert [(m, p) for m, p, _ in seen] == [
        ("GET", "/api2/json/nodes"), ("GET", "/api2/json/nodes/pve-b/certificates/info")], seen
    assert {auth for _, _, auth in seen} == {f"PVEAPIToken={TOKEN_ID}={SENT_PVE}"}
    assert "read from node pve-b" in out
    assert f"subject: {ROOT_SUBJECT}" in out
    assert _printed_pem(out) == ROOT_PEM.strip()
    # Nothing the responses or the request carried is printed, at -vvv.
    for leaked in (SENT_PVE, "PVEAPIToken", SAN_ADDRESS, "MIILeafNotPrinted"):
        assert leaked not in out, leaked


@needs_ansible
@pytest.mark.parametrize("case,nodes,certs,token_id,expected", [
    ("token id empty", [{"node": "pve-b", "status": "online"}], {}, "", "token_id"),
    ("no online node", [{"node": "pve-b", "status": "offline"}], {}, TOKEN_ID, "names no online node"),
    ("root not self-signed", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", "CN=Intermediate", ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM)]}, TOKEN_ID,
     "is not self-signed"),
    ("node cert not issued by the root", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT),
                _entry("pve-ssl.pem", "CN=pve-b", "CN=Other CA", pem=LEAF_PEM)]}, TOKEN_ID,
     "not issued by the root CA"),
    ("root entry is not a public certificate", [{"node": "pve-b", "status": "online"}],
     {"pve-b": [_entry("pve-root-ca.pem", ROOT_SUBJECT, ROOT_SUBJECT,
                       pem=PRIVATE_KEY_PEM),
                _entry("pve-ssl.pem", "CN=pve-b", ROOT_SUBJECT, pem=LEAF_PEM)]}, TOKEN_ID,
     "as a public certificate"),
])
def test_each_refusal_stops_before_anything_is_printed(tmp_path, case, nodes, certs, token_id, expected):
    server, seen = _fake_proxmox(nodes, certs)
    try:
        done = _run(_harness(tmp_path, f"http://127.0.0.1:{server.server_address[1]}", token_id))
    finally:
        server.shutdown()
    out = done.stdout + done.stderr
    assert done.returncode != 0, (case, out[-1500:])
    assert expected in out, (case, out[-1500:])
    assert "MIIFakeRootCA" not in out and "MIILeafNotPrinted" not in out, case
    assert SENT_PVE not in out and "PVEAPIToken" not in out, case
    if token_id == "":
        assert seen == [], "a request was sent before the token id was checked"
