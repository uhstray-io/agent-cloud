"""The read-only reachability probe of the Dispatch Console's Phase 3 sources (plan 17 task 6.3).

Structure, read from the parsed playbook:
- read-only: only probe modules; every uri is a GET, except the OpenBao AppRole login; check_mode is false on
  every read; every uri has a timeout and verifies TLS;
- placement: the NetBox, Proxmox and OpenBao health probes run on console_svc; the OpenBao credential reads run
  on the controller (delegate_to localhost), as deploy-console.yml reads its shared secrets;
- no secret in output: a task that touches a token, a header or a protected result is no_log; the summary and
  verdict builders reference only the derived line facts; debug prints only the summary;
- no address in output: no literal address, no URL in a fail_msg, the CA read from its rendered path;
- transport: each configured token URL (NetBox, Proxmox) passes the transport guard, gated on the URL being set, and
  the Proxmox URL must be https, all before the first credential read; every token-bearing uri refuses redirects and
  keeps Authorization off them.

Behaviour, through the real ansible-playbook against loopback fakes (fake_http.DrainingHandler): a sentinel token
must not appear in the output at default verbosity or at -vvv; the summary lines must appear; a configured source
that does not answer must fail the play, named, with no address printed.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/probe-console-sources.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

TEXT = PLAYBOOK.read_text()
DOC = yaml.safe_load(TEXT)
TASK_KEYWORDS = {
    "name", "register", "when", "no_log", "vars", "delegate_to", "delegate_facts", "run_once", "loop",
    "loop_control", "tags", "check_mode", "changed_when", "failed_when", "become", "ignore_errors", "notify",
    "timeout", "environment", "args", "any_errors_fatal", "ignore_unreachable", "block", "rescue", "always",
    "until", "retries", "delay", "connection", "diff",
}
PREFIX = "ansible.builtin."
PROBE_MODULES = {"include_tasks", "import_playbook", "uri", "set_fact", "wait_for", "stat", "assert", "debug"}
# A reference to a secret-holding fact or to a request header. A token-named fact that only holds a derived line
# (_probe_line_nb_token) is not a secret, so the pattern names the holders, not the word.
SECRET_REF = re.compile(
    r"\b_probe_(?:nb|pve)_(?:token|token_id|headers)\b|\b_nb_(?:headers|token)\b"
    r"|X-Vault-Token|PVEAPIToken|Authorization|console_api_token|console_token"
)
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
URL_LITERAL = re.compile(r"https?://")


def _play():
    return next(p for p in DOC if isinstance(p, dict) and p.get("hosts") == "console_svc" and "tasks" in p)


def _tasks(play=None):
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    walk((play or _play())["tasks"])
    return out


def _module(task):
    keys = [k for k in task if k not in TASK_KEYWORDS]
    assert len(keys) == 1, task.get("name")
    key = keys[0]
    return key[len(PREFIX):] if key.startswith(PREFIX) else key


def _args(task):
    args = task[next(k for k in task if k not in TASK_KEYWORDS)]
    return args if isinstance(args, dict) else {}


def _has_headers(task):
    return _module(task) == "uri" and "headers" in _args(task)


def _bearing(tasks):
    """Registers whose result carries a secret or a request header: the task hid itself, or sent headers."""
    return {t["register"] for t in tasks if t.get("register") and (t.get("no_log") is True or _has_headers(t))}


def _refs(task):
    """Every reference the task makes, excluding its name (a title may say "token")."""
    return json.dumps({k: v for k, v in task.items() if k != "name"}, sort_keys=True)


def _uri_url(task):
    return str(_args(task).get("url", ""))


def _is_openbao_credential(task):
    url = _uri_url(task)
    return _module(task) == "uri" and ("/v1/auth/" in url or "/v1/secret/" in url)


def _is_probe_read(task):
    return _module(task) in {"uri", "wait_for", "stat"} and not _is_openbao_credential(task)


def _is_transport_include(task):
    return str(task.get("ansible.builtin.include_tasks", task.get("include_tasks", ""))).endswith(
        "assert-bao-transport.yml")


def _is_token_id_presence_check(task):
    """The one assert that names a credential and renders no value: `that` is only
    `_probe_pve_token_id | length > 0` and its fail_msg renders nothing. It is exempt from the SECRET_REF rule
    and is not no_log, because it can fail only when the fact is empty (see its comment in the playbook)."""
    if _module(task) != "assert":
        return False
    that = _args(task).get("that")
    that = [that] if isinstance(that, str) else list(that or [])
    return that == ["_probe_pve_token_id | length > 0"] and "{{" not in str(_args(task).get("fail_msg", ""))


# --- the play, its imports and its placement ----------------------------------------------------------------


def test_playbook_imports_the_extra_var_guard_then_preflights_the_console_group():
    first = DOC[0]
    assert (first.get("ansible.builtin.import_playbook") or first.get("import_playbook")) == \
        "refuse-internal-extra-vars.yml"
    second = DOC[1]
    assert (second.get("ansible.builtin.import_playbook") or second.get("import_playbook")) == \
        "preflight-target-group.yml"
    assert second["vars"] == {"preflight_group": "console_svc", "preflight_group_expected": "console_svc"}


def test_probe_play_runs_on_console_svc_without_facts_or_privilege():
    play = _play()
    assert play["hosts"] == "console_svc"
    assert play["gather_facts"] is False
    assert play["become"] is False


def test_the_playbook_has_no_address_literal_and_no_url_literal():
    assert IPV4.search(TEXT) is None, IPV4.search(TEXT).group(0)
    assert URL_LITERAL.search(TEXT) is None


# --- read-only ----------------------------------------------------------------------------------------------


def test_every_task_is_a_probe_module():
    for t in _tasks():
        assert _module(t) in PROBE_MODULES, (t.get("name"), _module(t))


def test_no_task_loops_so_no_protected_result_is_iterated_or_printed_per_item():
    for t in _tasks():
        assert not any(k == "loop" or k.startswith("with_") for k in t), t.get("name")


def test_every_uri_is_a_get_except_the_approle_login_and_that_login_is_read_only():
    for t in _tasks():
        if _module(t) != "uri":
            continue
        method = str(_args(t).get("method", "GET")).upper()
        if method == "POST":
            assert _uri_url(t).endswith("/v1/auth/approle/login"), t.get("name")
            assert t.get("changed_when") is False, t.get("name")
        else:
            assert method == "GET", (t.get("name"), method)


def test_every_read_runs_under_check_mode():
    for t in _tasks():
        if _module(t) in {"uri", "wait_for", "stat"}:
            assert t.get("check_mode") is False, t.get("name")


def test_every_uri_has_a_timeout_and_verifies_tls():
    for t in _tasks():
        if _module(t) == "uri":
            assert "timeout" in _args(t), t.get("name")
            assert _args(t).get("validate_certs", True) is not False, t.get("name")


# --- placement and no_log -----------------------------------------------------------------------------------


def test_openbao_credential_reads_and_the_login_run_on_the_controller_and_are_no_log():
    creds = [t for t in _tasks() if _is_openbao_credential(t)]
    assert len(creds) == 3, [t.get("name") for t in creds]
    for t in creds:
        assert t.get("delegate_to") == "localhost", t.get("name")
        assert t.get("no_log") is True, t.get("name")


def test_source_probes_run_on_the_console_host_not_the_controller():
    for t in _tasks():
        if _is_probe_read(t):
            assert "delegate_to" not in t, t.get("name")


def test_the_unauthenticated_probes_stay_visible():
    for t in _tasks():
        url = _uri_url(t)
        if _module(t) == "uri" and ("/api/status/" in url or "/v1/sys/" in url):
            assert t.get("no_log") is not True, t.get("name")
        if _module(t) in {"wait_for", "stat"}:
            assert t.get("no_log") is not True, t.get("name")


def test_every_task_touching_a_secret_or_a_protected_result_is_no_log():
    tasks = _tasks()
    bearing = _bearing(tasks)
    for t in tasks:
        if t.get("no_log") is True:
            continue
        refs = _refs(t)
        if not _is_token_id_presence_check(t):
            assert SECRET_REF.search(refs) is None, (t.get("name"), SECRET_REF.search(refs).group(0))
        for reg in bearing:
            assert not re.search(rf"\b{re.escape(reg)}\b", refs), (t.get("name"), reg)


def test_every_request_that_sends_a_header_is_no_log():
    for t in _tasks():
        if _has_headers(t):
            assert t.get("no_log") is True, t.get("name")


def test_the_proxmox_ca_is_the_rendered_deploy_file_and_is_never_bypassed():
    play_vars = _play()["vars"]
    assert play_vars["_proxmox_ca_file"] == "{{ _deploy_dir }}/config/proxmox-ca.pem"
    for t in _tasks():
        if _module(t) == "uri" and "ca_path" in _args(t):
            assert _args(t)["ca_path"] == "{{ _proxmox_ca_file }}", t.get("name")
    assert "validate_certs: false" not in TEXT.lower()


def test_openbao_transport_guard_comes_before_the_first_openbao_request():
    tasks = _tasks()
    guard = next(i for i, t in enumerate(tasks)
                 if str(t.get("ansible.builtin.include_tasks", t.get("include_tasks", ""))).endswith(
                     "assert-bao-transport.yml"))
    first = next(i for i, t in enumerate(tasks) if "/v1/" in _uri_url(t))
    assert guard < first


def test_each_configured_token_url_passes_the_transport_guard_before_any_credential_read():
    tasks = _tasks()
    first_credential = min(i for i, t in enumerate(tasks) if _is_openbao_credential(t))
    for label, var in (("NetBox", "_netbox_url"), ("Proxmox", "_proxmox_url")):
        guards = [i for i, t in enumerate(tasks)
                  if _is_transport_include(t) and (t.get("vars") or {}).get("_assert_url_label") == label]
        assert len(guards) == 1, (label, guards)
        guard = tasks[guards[0]]
        assert guard["vars"]["_assert_bao_url"] == "{{ " + var + " }}", label
        assert guard.get("when") == var + " | length > 0", label
        assert guards[0] < first_credential, label


def test_the_proxmox_url_must_use_the_https_scheme_before_any_credential_read():
    tasks = _tasks()
    first_credential = min(i for i, t in enumerate(tasks) if _is_openbao_credential(t))
    checks = [i for i, t in enumerate(tasks)
              if _module(t) == "assert" and "urlsplit('scheme')" in str(_args(t).get("that"))]
    assert len(checks) == 1, [tasks[i].get("name") for i in checks]
    check = tasks[checks[0]]
    assert _args(check)["that"] == ["(_proxmox_url | urlsplit('scheme')) == 'https'"], _args(check)
    assert check.get("when") == "_proxmox_url | length > 0"
    assert checks[0] < first_credential


def test_the_token_id_presence_check_is_not_no_log_and_its_fact_is_held_by_a_no_log_task():
    checks = [t for t in _tasks() if _is_token_id_presence_check(t)]
    assert len(checks) == 1, [t.get("name") for t in checks]
    (check,) = checks
    assert check.get("no_log") is not True, "the presence check must not be no_log"
    assert check.get("when") == "_probe_pve_called | bool"
    holders = [t for t in _tasks() if "_probe_pve_token_id" in (t.get("ansible.builtin.set_fact") or {})]
    assert holders and all(h.get("no_log") is True for h in holders), [h.get("name") for h in holders]


def test_every_token_bearing_uri_refuses_redirects_and_keeps_authorization_off_them():
    # The OpenBao reads carry X-Vault-Token and are covered by their own rule; this one is the NetBox and Proxmox
    # requests, which carry the console tokens (three NetBox reads, two Proxmox reads).
    bearing = [t for t in _tasks() if _has_headers(t) and not _is_openbao_credential(t)]
    assert len(bearing) == 5, [t.get("name") for t in bearing]
    for t in bearing:
        assert _args(t).get("follow_redirects") == "none", t.get("name")
        assert "Authorization" in (_args(t).get("unredirected_headers") or []), t.get("name")


# --- output: debug prints only the summary; the summary and verdict build from lines alone ------------------


def test_debug_prints_only_the_summary():
    for t in _tasks():
        if _module(t) == "debug":
            assert _args(t) == {"var": "_probe_summary"}, t.get("name")


def test_the_summary_builder_references_only_derived_line_facts():
    summary = [t for t in _tasks() if isinstance(t.get("ansible.builtin.set_fact", t.get("set_fact")), dict)
               and "_probe_summary" in t.get("ansible.builtin.set_fact", t.get("set_fact"))]
    assert len(summary) == 1, [t.get("name") for t in summary]
    value = str(summary[0]["ansible.builtin.set_fact" if "ansible.builtin.set_fact" in summary[0]
                            else "set_fact"]["_probe_summary"])
    names = set(re.findall(r"\b_probe_\w+", value))
    assert names, value
    assert all(n.startswith("_probe_line_") for n in names), names
    assert SECRET_REF.search(value) is None


def test_the_failure_message_names_the_sources_and_no_url():
    asserts = [t for t in _tasks() if _module(t) == "assert"]
    assert asserts, "no assert"
    for t in asserts:
        msg = str(_args(t).get("fail_msg", ""))
        # A fail_msg may NAME a variable (console_proxmox_url, in words) but may not RENDER one: every {{ }}
        # expression is checked, so no URL value and no CA file path can reach the output.
        for expr in re.findall(r"\{\{[^}]*\}\}", msg):
            assert "url" not in expr and "_proxmox_ca_file" not in expr, (t.get("name"), expr)


# --- the template -------------------------------------------------------------------------------------------


def test_the_probe_template_is_a_dev_variant_with_no_survey_vars():
    doc = yaml.safe_load(TEMPLATES.read_text())
    items = doc.get("templates", doc) if isinstance(doc, dict) else doc
    entries = [e for e in items if isinstance(e, dict) and e.get("name") == "Probe Console Sources"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["playbook"] == "platform/playbooks/probe-console-sources.yml"
    assert entry["dev_variant"] is True
    assert "survey_vars" not in entry


# --- behaviour: the real ansible-playbook against loopback fakes --------------------------------------------

SENT_NB = "SENTINEL-NETBOX-TOKEN-7f3a9c"
SENT_PVE = "SENTINEL-PROXMOX-TOKEN-5e21b8"
CLIENT = "fake-approle-client-token"


def _serve(answer):
    class Handler(DrainingHandler):
        def do_GET(self):
            self._reply("GET")

        def do_POST(self):
            self._reply("POST")

        def _reply(self, method):
            status, body = answer(method, self.path, self.headers)
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
    return server


def _recording(answer, seen):
    """Wrap a fake's answer so each request it receives is appended to `seen`, when a list is given."""
    if seen is None:
        return answer

    def recorded(method, path, headers):
        seen.append((method, path))
        return answer(method, path, headers)

    return recorded


def _proxmox_json(method, path, headers):
    return 200, {"data": []}


def _openbao(netbox_stored):
    def answer(method, path, headers):
        if path == "/v1/sys/health":
            return 200, {"initialized": True, "sealed": False}
        if path == "/v1/sys/seal-status":
            return 200, {"sealed": False}
        if path == "/v1/auth/approle/login" and method == "POST":
            return 200, {"auth": {"client_token": CLIENT}}
        if headers.get("X-Vault-Token") != CLIENT:
            return 403, {"errors": ["permission denied"]}
        if path == "/v1/secret/data/services/netbox":
            if not netbox_stored:
                return 404, {"errors": []}
            return 200, {"data": {"data": {"console_api_token": SENT_NB}}}
        if path == "/v1/secret/data/services/proxmox":
            return 200, {"data": {"data": {"console_token_id": "console@pve!console",
                                           "console_token": SENT_PVE}}}
        return 404, {"errors": []}

    return answer


def _netbox():
    sites = {"count": 2, "results": [{"id": 1, "slug": "alpha-dc", "name": "Alpha DC"},
                                     {"id": 2, "slug": "beta-dc", "name": "Beta DC"}]}
    clusters = {"count": 1, "results": [{"id": 1, "name": "lab-cluster", "type": {"slug": "proxmox"},
                                         "scope_type": "dcim.site", "scope_id": 1,
                                         "scope": {"slug": "alpha-dc"}}]}
    vms = {"count": 1, "results": [{"id": 9, "name": "vm-one", "cluster": {"id": 1},
                                    "custom_fields": {"ac_workflow_status": None, "rack_note": "not an ac field"},
                                    "primary_ip": None}]}

    def answer(method, path, headers):
        if path.startswith("/api/status/"):
            return 200, {"netbox-version": "4.5.0"}
        if headers.get("Authorization") != "Token " + SENT_NB:
            return 403, {"detail": "Authentication credentials were not provided."}
        if path.startswith("/api/dcim/sites/"):
            return 200, sites
        if path.startswith("/api/virtualization/clusters/"):
            return 200, clusters
        if path.startswith("/api/virtualization/virtual-machines/"):
            return 200, vms
        return 404, {"detail": "Not found."}

    return answer


@pytest.fixture
def probe_run(tmp_path):
    servers = []

    def run(netbox_stored, proxmox_open, verbosity=None, netbox_url=None, proxmox_url=None, bao_seen=None):
        bao = _serve(_recording(_openbao(netbox_stored), bao_seen))
        nb = _serve(_netbox())
        servers.extend([bao, nb])
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        servers.append(listener)
        if proxmox_open:
            pve_port = listener.getsockname()[1]
        else:
            closed = socket.socket()
            closed.bind(("127.0.0.1", 0))
            pve_port = closed.getsockname()[1]
            closed.close()
        inventory = {"all": {"children": {"console_svc": {"hosts": {"console-probe": {
            "ansible_connection": "local",
            "openbao_addr": f"http://127.0.0.1:{bao.server_address[1]}",
            "bao_role_id": "fake-role-id",
            "bao_secret_id": "fake-secret-id",
            "console_netbox_url": netbox_url or f"http://127.0.0.1:{nb.server_address[1]}",
            "console_proxmox_url": proxmox_url or f"https://127.0.0.1:{pve_port}",
            "monorepo_deploy_path": "platform/services/console/deployment",
            "local_monorepo_dir": str(tmp_path),
        }}}}}}
        inv = tmp_path / "inventory.yml"
        inv.write_text(yaml.safe_dump(inventory))
        env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
        env["ANSIBLE_NOCOLOR"] = "1"
        cmd = ["ansible-playbook", "-i", str(inv), str(PLAYBOOK)] + ([verbosity] if verbosity else [])
        return subprocess.run(cmd, cwd=REPO, env=env, text=True, capture_output=True, check=False, timeout=300)

    yield run
    for s in servers:
        if isinstance(s, socket.socket):
            s.close()
        else:
            s.shutdown()
            s.server_close()


@NEEDS_ANSIBLE
def test_a_reachable_console_reports_its_sources_and_prints_no_token(probe_run):
    proc = probe_run(netbox_stored=True, proxmox_open=True)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    for line in ("NetBox: reachable (HTTP 200), netbox-version: 4.5.0",
                 "NetBox token: minted",
                 "NetBox sites: 2 (alpha-dc (Alpha DC), beta-dc (Beta DC))",
                 "NetBox clusters: 1 (lab-cluster (type proxmox; scope dcim.site alpha-dc))",
                 "NetBox VM workflow custom fields: ac_workflow_status",
                 "OpenBao sealed: False",
                 "Proxmox: TCP to the API port reachable",
                 "Proxmox CA: not declared on console_svc",
                 "Proxmox token: minted",
                 "Proxmox API: not called"):
        assert line in out, (line, out)
    assert SENT_NB not in out and SENT_PVE not in out
    assert "127.0.0.1" not in out, "an address reached the default output"
    assert "rack_note" not in out, "a non-ac custom field name reached the output"


@NEEDS_ANSIBLE
def test_the_tokens_stay_out_of_output_at_maximum_verbosity(probe_run):
    proc = probe_run(netbox_stored=True, proxmox_open=True, verbosity="-vvv")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "NetBox sites: 2" in out, "the -vvv run did not reach the NetBox reads"
    assert SENT_NB not in out and SENT_PVE not in out


@NEEDS_ANSIBLE
def test_a_closed_source_fails_the_play_by_name_after_the_summary_and_prints_no_address(probe_run):
    proc = probe_run(netbox_stored=False, proxmox_open=False)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "NetBox token: not minted" in out
    assert "Proxmox: TCP to the API port not reachable from the console VM" in out
    assert "did not answer the probe: Proxmox" in out
    assert "127.0.0.1" not in out, "an address reached the default output"
    assert SENT_NB not in out and SENT_PVE not in out


@NEEDS_ANSIBLE
def test_a_cleartext_public_netbox_url_is_refused_before_any_openbao_request(probe_run):
    url = "http://netbox.invalid:8000"
    bao_seen = []
    proc = probe_run(netbox_stored=True, proxmox_open=True, netbox_url=url, bao_seen=bao_seen)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "Refusing to send secret material to NetBox" in out, out
    assert bao_seen == [], "an OpenBao request was sent before the NetBox transport guard ran"
    assert url not in out and "netbox.invalid" not in out, "the refused URL was printed"
    assert SENT_NB not in out and SENT_PVE not in out


@NEEDS_ANSIBLE
def test_a_cleartext_public_proxmox_url_is_refused_before_any_openbao_request(probe_run):
    url = "http://pve.invalid:8006"
    bao_seen = []
    proc = probe_run(netbox_stored=True, proxmox_open=True, proxmox_url=url, bao_seen=bao_seen)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "Refusing to send secret material to Proxmox" in out, out
    assert bao_seen == [], "an OpenBao request was sent before the Proxmox transport guard ran"
    assert url not in out and "pve.invalid" not in out, "the refused URL was printed"
    assert SENT_PVE not in out


@NEEDS_ANSIBLE
def test_a_loopback_http_proxmox_url_is_refused_by_name_before_any_request(probe_run):
    # Loopback http passes the transport guard (it is a local address), so the https check is what refuses it.
    bao_seen, pve_seen = [], []
    pve = _serve(_recording(_proxmox_json, pve_seen))
    try:
        proc = probe_run(netbox_stored=True, proxmox_open=True,
                         proxmox_url=f"http://127.0.0.1:{pve.server_address[1]}", bao_seen=bao_seen)
    finally:
        pve.shutdown()
        pve.server_close()
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "console_proxmox_url must use the https scheme" in out, out
    assert bao_seen == [], "an OpenBao request was sent before the https check ran"
    assert pve_seen == [], "a request reached the Proxmox endpoint over cleartext"
    assert "127.0.0.1" not in out, "an address reached the default output"
    assert SENT_PVE not in out and "PVEAPIToken" not in out
