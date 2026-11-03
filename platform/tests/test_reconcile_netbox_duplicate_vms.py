"""Duplicate VM reconciliation (reconcile-netbox-duplicate-vms.yml, Semaphore "Reconcile NetBox Duplicate VMs").

Structure, read from the parsed playbook and the template catalog:
- the extra-var guard is the run's first play;
- every write (PATCH, DELETE) is skipped under check mode and comes AFTER the refusal that stops a run
  whose bare record holds an interface or a primary IP; every PATCH precedes every DELETE;
- every NetBox and OpenBao request is no_log; every visible task avoids the token, the request headers,
  a response body and the custom_fields dicts; the only visible debug prints the plan and the summary;
- the template is a dev_variant with one string survey field, reconcile_names;
- the vm-reconciler token profile may view, change and delete virtual machines and nothing else.

Behaviour, through the real ansible-playbook against one loopback fake (OpenBao and NetBox on the same
port). The fake NetBox replaces custom_fields on PATCH (the stricter reading of a PATCH), so the playbook
must send the whole dict. Secrets, the request headers, addresses and comments must not reach the output
at default verbosity or at -vvv. The interface and primary-IP reads use the paths the playbook names;
the NetBox documentation does not state them, so these tests prove the playbook's logic, not NetBox's
routes (see the PR body).
"""

import ast
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import playbook_yaml
import pytest
import yaml
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/reconcile-netbox-duplicate-vms.yml"
PROVISION = REPO / "platform/playbooks/provision-netbox-automation-token.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

TEXT = PLAYBOOK.read_text()
DOC = yaml.safe_load(TEXT)
PLAY = DOC[1]
TASK_KEYWORDS = {
    "name", "register", "when", "no_log", "vars", "delegate_to", "loop", "loop_control", "tags",
    "check_mode", "changed_when", "failed_when", "become", "ignore_errors", "timeout", "args",
    "block", "rescue", "always", "connection", "diff", "notify", "environment",
}
PREFIX = "ansible.builtin."
MARKER = "Declared by the service deployment workflow (validate-address)"
SENT = "SENTINEL-VM-RECONCILER-TOKEN-4d1f"
CLIENT = "fake-approle-client-token"
ADDR = "192.0.2.77"
# The writes, and the visible tasks that must never name a secret, a header, a response body or a
# custom_fields dict. `_plan` and `_pairs` are no_log facts a visible task may read for names and ids only.
FORBIDDEN_VISIBLE = re.compile(
    r"\.json\b|_nb_token\b|_nb_headers|X-Vault-Token|Authorization|patch_fields|move_values|custom_fields|client_token"
)


def _tasks():
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    walk(PLAY["tasks"])
    return out


def _module(task):
    key = next(k for k in task if k not in TASK_KEYWORDS)
    return key[len(PREFIX):] if key.startswith(PREFIX) else key


def _args(task):
    key = next(k for k in task if k not in TASK_KEYWORDS)
    return task[key] if isinstance(task[key], dict) else {}


def _is_write(task):
    return _module(task) == "uri" and str(_args(task).get("method", "GET")).upper() in {"PATCH", "DELETE"}


def _is_get(task):
    return _module(task) == "uri" and str(_args(task).get("method", "GET")).upper() == "GET"


def _skips_in_check_mode(task):
    when = task.get("when")
    items = when if isinstance(when, list) else [when]
    return any(str(i).strip() == "not ansible_check_mode" for i in items)


def _named(name):
    return next(t for t in _tasks() if t.get("name") == name)


# --- the play, its guards and its order -----------------------------------------------------------------


def test_playbook_imports_the_extra_var_guard_as_its_first_play():
    assert DOC[0].get("ansible.builtin.import_playbook", DOC[0].get("import_playbook")) == \
        "refuse-internal-extra-vars.yml"
    assert PLAY["hosts"] == "localhost" and PLAY["gather_facts"] is False and PLAY["become"] is False


def test_both_transport_guards_come_before_the_first_credential_read():
    tasks = _tasks()
    guards = [i for i, t in enumerate(tasks) if str(t.get("include_tasks", t.get("ansible.builtin.include_tasks", "")))
              .endswith("assert-bao-transport.yml")]
    assert len(guards) == 2, guards
    first_credential = min(i for i, t in enumerate(tasks)
                           if _module(t) == "uri" and ("/v1/" in str(_args(t).get("url", ""))))
    assert max(guards) < first_credential


def test_every_write_is_skipped_under_check_mode():
    writes = [t for t in _tasks() if _is_write(t)]
    assert {str(_args(t)["method"]) for t in writes} == {"PATCH", "DELETE"}
    for t in writes:
        assert _skips_in_check_mode(t), t.get("name")


def test_every_read_runs_under_check_mode():
    for t in _tasks():
        if _is_get(t) or (_module(t) == "uri" and str(_args(t).get("method", "")).upper() == "POST"):
            assert t.get("check_mode") is False, t.get("name")


def test_every_request_that_sends_a_header_is_no_log():
    for t in _tasks():
        if _module(t) == "uri" and "headers" in _args(t):
            assert t.get("no_log") is True, t.get("name")


def test_the_refusal_comes_before_the_first_write():
    tasks = _tasks()
    refusal = next(i for i, t in enumerate(tasks)
                   if t.get("name") == "Refuse the run when a selected bare record holds an interface or a primary IP")
    first_write = min(i for i, t in enumerate(tasks) if _is_write(t))
    assert refusal < first_write
    assert _module(tasks[refusal]) == "assert"
    assert "_blocked_names | length == 0" in str(_args(tasks[refusal])["that"])


def test_every_patch_precedes_every_delete():
    tasks = _tasks()
    patches = [i for i, t in enumerate(tasks) if _is_write(t) and _args(t)["method"] == "PATCH"]
    deletes = [i for i, t in enumerate(tasks) if _is_write(t) and _args(t)["method"] == "DELETE"]
    assert patches and deletes and max(patches) < min(deletes)


def test_the_discovery_readback_is_checked_before_the_first_delete():
    tasks = _tasks()
    compare = next(i for i, t in enumerate(tasks) if t.get("name") == "Compare the read-back with what was written")
    assert compare < min(i for i, t in enumerate(tasks) if _is_write(t) and _args(t)["method"] == "DELETE")


def test_the_discovery_patch_sends_the_whole_custom_fields_dict():
    # The fake and NetBox's own merge semantics are not both known; the playbook must not depend on either.
    patch = _named("NetBox: write the workflow fields onto each discovery record")
    assert patch["ansible.builtin.uri"]["body"]["custom_fields"] == "{{ item.patch_fields }}"
    assert "'patch_fields': dcf | combine(mv.vals)" in TEXT


# --- visible output ------------------------------------------------------------------------------------


def test_a_visible_task_never_names_a_secret_a_header_a_body_or_a_custom_field():
    for t in _tasks():
        if t.get("no_log") is True or _module(t) == "include_tasks" or "include_tasks" in t \
                or "ansible.builtin.include_tasks" in t:
            continue
        refs = json.dumps({k: v for k, v in t.items() if k != "name"}, sort_keys=True)
        assert FORBIDDEN_VISIBLE.search(refs) is None, (t.get("name"), FORBIDDEN_VISIBLE.search(refs).group(0))


def test_the_only_visible_debug_prints_the_plan_and_the_summary():
    debugs = [t for t in _tasks() if _module(t) == "debug"]
    assert len(debugs) == 2, [t.get("name") for t in debugs]
    assert _args(_named("Print the plan")) == {"var": "_plan_lines"}
    assert set(_args(_named("Summarise the run (names only)"))) == {"msg"}


def test_the_plan_line_is_built_from_names_ids_tenants_stamps_and_reasons_only():
    text = json.dumps(_named("Show the plan (names only)"), sort_keys=True)
    for forbidden in ("primary_ip", "comments", "description", "patch_fields", "move_values", "custom_fields"):
        assert forbidden not in text, forbidden


def test_no_loop_iterates_a_protected_result():
    for t in _tasks():
        loop = str(t.get("loop", ""))
        if re.search(r"\b_(vm_page0|vm_pages|iface_reads|patch|readback|delete|gone|bao_auth|nb_secret)\b", loop):
            assert t.get("no_log") is True, t.get("name")


# --- the template and the token profile ------------------------------------------------------------------


def test_the_template_is_a_dev_variant_with_one_optional_string_survey_field():
    entries = [e for e in yaml.safe_load(TEMPLATES.read_text())["templates"]
               if e.get("name") == "Reconcile NetBox Duplicate VMs"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["playbook"] == "platform/playbooks/reconcile-netbox-duplicate-vms.yml"
    assert entry["dev_variant"] is True
    assert entry["survey_vars"] == [{"name": "reconcile_names", "title": "VM names to reconcile",
                                     "description": entry["survey_vars"][0]["description"], "type": "string",
                                     "required": False, "default_value": ""}]


def _reconciler_profile():
    return playbook_yaml.plays(PROVISION)[0]["vars"]["_profiles"]["vm-reconciler"]


def test_the_vm_reconciler_profile_grants_view_and_change_and_delete_only_where_declared():
    profile = _reconciler_profile()
    assert profile["field"] == "vm_reconciler_api_token"
    assert profile["token_write_enabled"] is True
    assert profile["actions"] == ["view"]
    assert profile["object_types"] == ["virtualization.virtualmachine"]
    assert sorted(profile["view_object_types"]) == [
        "tenancy.tenant", "virtualization.cluster", "virtualization.vminterface"]
    extra = {scope["permission"]: scope for scope in profile["extra_scopes"]}
    assert set(extra) == {"workflow-vm-reconcile-changelog-view", "workflow-vm-reconcile-change",
                          "workflow-vm-reconcile-delete"}
    # NetBox does not apply object constraints to the changelog endpoint (ObjectChangeViewSet is not restricted),
    # so the changelog scope carries none: a constraint there would look like a restriction and restrict nothing.
    assert extra["workflow-vm-reconcile-changelog-view"] == {
        "permission": "workflow-vm-reconcile-changelog-view", "actions": ["view"],
        "object_types": ["core.objectchange"]}
    change = extra["workflow-vm-reconcile-change"]
    delete = extra["workflow-vm-reconcile-delete"]
    assert change["actions"] == ["change"] and delete["actions"] == ["delete"]
    for scope in (change, delete):
        assert scope["object_types"] == ["virtualization.virtualmachine"], scope
        # NetBox: a null or empty constraint is unconstrained access, so a constrained scope is a non-empty dict.
        assert isinstance(scope["constraints"], dict) and scope["constraints"], scope
    assert change["constraints"] == {"comments__startswith": "VMID:", "vcpus__isnull": False}
    assert delete["constraints"] == {
        "description": "Declared by the service deployment workflow (validate-address)",
        "vcpus__isnull": True, "comments": ""}
    names = [profile["permission"], profile["view_permission"], *(s["permission"] for s in profile["extra_scopes"])]
    assert len(set(names)) == 5, names
    assert "add" not in {a for s in profile["extra_scopes"] for a in s["actions"]} | set(profile["actions"])


def _matches(constraints, record):
    """The subset of NetBox's Q(**constraint) this profile uses: exact, __startswith and __isnull, ANDed."""
    for key, want in constraints.items():
        if key.endswith("__startswith"):
            ok = str(record.get(key[:-len("__startswith")]) or "").startswith(want)
        elif key.endswith("__isnull"):
            ok = (record.get(key[:-len("__isnull")]) is None) == want
        else:
            ok = record.get(key) == want
        if not ok:
            return False
    return True


def test_the_change_and_delete_constraints_cannot_both_match_one_record():
    extra = {s["permission"]: s for s in _reconciler_profile()["extra_scopes"]}
    change = extra["workflow-vm-reconcile-change"]["constraints"]
    delete = extra["workflow-vm-reconcile-delete"]["constraints"]
    # Structural: one needs comments that start with VMID: (so never empty), the other needs empty comments.
    assert change["comments__startswith"].startswith("VMID:") and delete["comments"] == ""
    assert change["vcpus__isnull"] is False and delete["vcpus__isnull"] is True
    # And over the shapes that exist, including the ones that differ in one field only.
    marker = "Declared by the service deployment workflow (validate-address)"
    shapes = [
        {"description": marker, "vcpus": None, "comments": ""},            # the bare record
        {"description": "Proxmox notes", "vcpus": 2.0, "comments": "VMID: 101. Host: pve1."},  # discovery
        {"description": marker, "vcpus": None, "comments": "VMID: 9."},    # marker, no vcpus, VMID comment
        {"description": marker, "vcpus": 2.0, "comments": ""},             # marker, vcpus set, no comments
        {"description": "", "vcpus": None, "comments": ""},
        {"description": "", "vcpus": 2.0, "comments": "VMID: 1."},
    ]
    for record in shapes:
        assert not (_matches(change, record) and _matches(delete, record)), record
    assert _matches(change, shapes[1]) and _matches(delete, shapes[0])
    assert not _matches(delete, shapes[1]) and not _matches(change, shapes[0])


def test_the_classifier_uses_the_same_predicates_as_the_constraints():
    # A record the playbook calls bare must be one the delete scope can remove, and a discovery one one the change
    # scope can patch. The two predicates are pinned to the constraints by their text.
    assert "(vm.get('vcpus') is none) and (comments == '') -%}" in TEXT
    assert "'is_disc': comments.startswith('VMID:') and (vm.get('vcpus') is not none)," in TEXT


def test_the_reconciler_account_name_matches_the_vm_recorder_profile_user_the_creator_check_pins():
    profiles = playbook_yaml.plays(PROVISION)[0]["vars"]["_profiles"]
    assert profiles["vm-recorder"]["user"] == "workflow-vm-recorder"
    assert PLAY["vars"]["_recorder_user"] == profiles["vm-recorder"]["user"]


# --- behaviour: the real ansible-playbook against the loopback fake --------------------------------------


def _cf(**values):
    base = {"ac_workflow_status": {}, "ac_failed_steps": None, "ac_last_run": None}
    base.update(values)
    return base


BARE_CF = {"ac_workflow_status": {"step-a": "pass"}, "ac_failed_steps": "step-b",
           "ac_last_run": "2026-11-01T10:00:00Z"}


def _bare(vm_id, name, cluster=1, cf=None, ip=None, cluster_name="uhstray-cluster"):
    return {"id": vm_id, "name": name, "cluster": {"id": cluster, "name": cluster_name}, "tenant": None,
            "created": "2026-11-01T09:00:00Z", "description": MARKER, "vcpus": None, "comments": "",
            "primary_ip4": ip, "primary_ip6": None, "custom_fields": dict(BARE_CF if cf is None else cf)}


def _disc(vm_id, name, cluster=1, cf=None, cluster_name="uhstray-cluster"):
    return {"id": vm_id, "name": name, "cluster": {"id": cluster, "name": cluster_name},
            "tenant": {"id": 2, "name": "uhstray"}, "created": "2026-10-01T09:00:00Z",
            "description": "Proxmox notes", "vcpus": 2.0,
            "comments": "VMID: 101. Host: pve1. Discovered via Proxmox API.",
            "primary_ip4": None, "primary_ip6": None, "custom_fields": dict(_cf() if cf is None else cf)}


RECORDER = "workflow-vm-recorder"
DISCOVERY = "proxmox-discovery"


class FakeNetBox:
    """One OpenBao and one NetBox on a single loopback port. PATCH replaces custom_fields.

    Each VM has creation records in the fake changelog: a bare record is created by the recorder account, a
    discovery record by another account, unless `origins` overrides the creators of an id (an empty list means no
    creation record at all)."""

    def __init__(self, vms, interfaces, origins=None):
        self.vms = {vm["id"]: vm for vm in vms}
        self.interfaces = dict(interfaces)
        self.origins = {}
        for vm in vms:
            bare = vm["description"] == MARKER and vm["vcpus"] is None
            self.origins[vm["id"]] = [RECORDER] if bare else [DISCOVERY]
        self.origins.update(origins or {})
        self.log = []

    def writes(self):
        return [entry for entry in self.log if entry[0] in ("PATCH", "DELETE")]

    def answer(self, method, path, headers, body):
        url = urlsplit(path)
        query = parse_qs(url.query)
        if url.path == "/v1/auth/approle/login" and method == "POST":
            return 200, {"auth": {"client_token": CLIENT}}
        if url.path.startswith("/v1/"):
            if headers.get("X-Vault-Token") != CLIENT:
                return 403, {"errors": ["permission denied"]}
            if url.path == "/v1/secret/data/services/netbox":
                return 200, {"data": {"data": {"vm_reconciler_api_token": SENT}}}
            return 404, {"errors": []}
        if headers.get("Authorization") != "Token " + SENT:
            return 403, {"detail": "Authentication credentials were not provided."}
        self.log.append((method, url.path))
        if url.path == "/api/virtualization/virtual-machines/" and method == "GET":
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])
            ids = sorted(self.vms)
            page = [self.vms[i] for i in ids[offset:offset + limit]]
            return 200, {"count": len(ids), "next": None, "previous": None, "results": page}
        if url.path == "/api/core/object-changes/" and method == "GET":
            # The filter names the playbook must send: NetBox's ObjectChangeFilterSet (v4.5.0) takes them as given.
            wanted = {"changed_object_type": ["virtualization.virtualmachine"], "action": ["create"]}
            if any(query.get(key) != value for key, value in wanted.items()):
                return 400, {"detail": "unexpected filter"}
            vm_id = int(query["changed_object_id"][0])
            rows = [{"id": n, "action": "create", "changed_object_id": vm_id, "user_name": who}
                    for n, who in enumerate(self.origins.get(vm_id, []))]
            return 200, {"count": len(rows), "next": None, "previous": None, "results": rows}
        if url.path == "/api/virtualization/interfaces/" and method == "GET":
            count = self.interfaces.get(int(query["virtual_machine_id"][0]), 0)
            return 200, {"count": count, "next": None, "previous": None, "results": []}
        match = re.fullmatch(r"/api/virtualization/virtual-machines/(\d+)/", url.path)
        if match:
            vm_id = int(match.group(1))
            if vm_id not in self.vms:
                return 404, {"detail": "Not found."}
            if method == "GET":
                return 200, self.vms[vm_id]
            if method == "PATCH":
                self.vms[vm_id]["custom_fields"] = json.loads(body)["custom_fields"]
                return 200, self.vms[vm_id]
            if method == "DELETE":
                del self.vms[vm_id]
                return 204, None
        return 404, {"detail": "Not found."}


def _serve(state):
    class Handler(DrainingHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            status, payload = state.answer(method, self.path, self.headers, body)
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def reconcile(tmp_path):
    servers = []

    def run(vms, interfaces=None, check=False, extra=None, verbosity=None, origins=None):
        state = FakeNetBox(vms, interfaces or {}, origins)
        server = _serve(state)
        servers.append(server)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        inventory = {"all": {"hosts": {"localhost": {
            "ansible_connection": "local",
            "openbao_addr": url,
            "netbox_url": url,
            "bao_role_id": "fake-role-id",
            "bao_secret_id": "fake-secret-id",
        }}}}
        inv = tmp_path / "inventory.yml"
        inv.write_text(yaml.safe_dump(inventory))
        cmd = ["ansible-playbook", "-i", str(inv), str(PLAYBOOK)]
        if check:
            cmd.append("--check")
        if verbosity:
            cmd.append(verbosity)
        if extra is not None:
            cmd += ["--extra-vars", json.dumps(extra)]
        env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
        env["ANSIBLE_NOCOLOR"] = "1"
        proc = subprocess.run(cmd, cwd=REPO, env=env, text=True, capture_output=True, check=False, timeout=300)
        out = proc.stdout + proc.stderr
        return proc, out, state

    yield run
    for server in servers:
        server.shutdown()
        server.server_close()


def _three_pairs():
    """dns: the discovery record is empty, so every bare field moves.
    step-ca: the bare run is newer than the discovery run, so every bare field moves, overwriting.
    vault: the discovery run is newer, so only the fields the discovery record lacks move."""
    newer_disc = {"ac_workflow_status": {"step-a": "fail"}, "ac_failed_steps": "step-a",
                  "ac_last_run": "2026-10-01T00:00:00Z"}
    vault_bare = {"ac_workflow_status": {"x": "pass"}, "ac_failed_steps": "f",
                  "ac_last_run": "2026-10-01T00:00:00Z"}
    vault_disc = {"ac_workflow_status": {"y": "fail"}, "ac_failed_steps": None,
                  "ac_last_run": "2026-11-01T00:00:00Z"}
    return [_bare(10, "dns"), _disc(11, "dns"),
            _bare(20, "step-ca", cf=BARE_CF), _disc(21, "step-ca", cf=newer_disc),
            _bare(30, "vault", cf=vault_bare), _disc(31, "vault", cf=vault_disc)]


@NEEDS_ANSIBLE
def test_a_qualifying_pair_gets_its_fields_then_its_bare_record_is_deleted(reconcile):
    proc, out, state = reconcile(_three_pairs())
    assert proc.returncode == 0, out
    assert "pair dns in cluster uhstray-cluster: bare 10" in out, out
    assert "move ac_workflow_status, ac_failed_steps, ac_last_run" in out, out
    assert 10 not in state.vms and 20 not in state.vms and 30 not in state.vms
    assert state.vms[11]["custom_fields"] == BARE_CF
    assert state.vms[21]["custom_fields"] == BARE_CF, "a newer bare run must overwrite the discovery run"
    assert state.vms[31]["custom_fields"] == {"ac_workflow_status": {"y": "fail"}, "ac_failed_steps": "f",
                                              "ac_last_run": "2026-11-01T00:00:00Z"}, \
        "a newer discovery run keeps its own fields and takes only the ones it lacks"
    writes = state.writes()
    assert writes.index(("PATCH", "/api/virtualization/virtual-machines/11/")) < writes.index(
        ("DELETE", "/api/virtualization/virtual-machines/10/")), writes
    for secret in (SENT, CLIENT, ADDR, "VMID", "127.0.0.1"):
        assert secret not in out, secret


@NEEDS_ANSIBLE
def test_check_mode_reports_the_plan_and_writes_nothing(reconcile):
    proc, out, state = reconcile(_three_pairs(), check=True)
    assert proc.returncode == 0, out
    assert "pair dns in cluster uhstray-cluster: bare 10" in out, out
    assert "check mode: nothing changed" in out, out
    assert state.writes() == [], state.writes()
    assert 10 in state.vms and 11 in state.vms and state.vms[11]["custom_fields"] == _cf()
    assert SENT not in out and CLIENT not in out


@NEEDS_ANSIBLE
def test_a_bare_record_with_an_interface_is_refused_before_any_write(reconcile):
    vms = [_bare(40, "openbao"), _disc(41, "openbao")]
    proc, out, state = reconcile(vms, interfaces={40: 1})
    assert proc.returncode != 0, out
    assert "cannot be reconciled" in out, out
    assert "blocked has interfaces" in out, out
    assert state.writes() == [], state.writes()
    assert 40 in state.vms and 41 in state.vms


@NEEDS_ANSIBLE
def test_a_check_mode_run_refuses_the_same_pair_so_it_fails_where_the_real_run_would(reconcile):
    vms = [_bare(40, "openbao"), _disc(41, "openbao")]
    proc, out, state = reconcile(vms, interfaces={40: 1}, check=True)
    assert proc.returncode != 0, out
    assert state.writes() == []


@NEEDS_ANSIBLE
def test_a_bare_record_with_a_primary_ip_is_refused_and_the_address_is_not_printed(reconcile):
    ip = {"id": 9, "address": ADDR + "/24"}
    vms = [_bare(50, "gh-runner-01", ip=ip), _disc(51, "gh-runner-01")]
    proc, out, state = reconcile(vms)
    assert proc.returncode != 0, out
    assert "blocked has a primary IP" in out, out
    assert ADDR not in out
    assert state.writes() == [] and 50 in state.vms


@NEEDS_ANSIBLE
def test_a_group_that_is_not_one_bare_and_one_discovery_record_is_ambiguous_and_untouched(reconcile):
    vms = [_bare(60, "agentgateway"), _bare(61, "agentgateway"),
           _bare(70, "o11y"), _disc(71, "o11y"), _disc(72, "o11y")]
    proc, out, state = reconcile(vms)
    assert proc.returncode == 0, out
    assert "ambiguous, not touched: agentgateway in cluster uhstray-cluster (2 records)" in out, out
    assert "ambiguous, not touched: o11y in cluster uhstray-cluster (3 records)" in out, out
    assert state.writes() == []
    assert set(state.vms) == {60, 61, 70, 71, 72}


@NEEDS_ANSIBLE
@pytest.mark.parametrize("extra", [{"reconcile_names": ["step-ca"]}, {"reconcile_names": '["step-ca"]'}],
                         ids=["list", "survey-string"])
def test_reconcile_names_limits_the_run_to_the_named_pair(reconcile, extra):
    proc, out, state = reconcile(_three_pairs(), extra=extra)
    assert proc.returncode == 0, out
    assert 20 not in state.vms and 21 in state.vms, "the named pair was not reconciled"
    assert 10 in state.vms and 11 in state.vms and 30 in state.vms and 31 in state.vms, "an unnamed pair was touched"


@NEEDS_ANSIBLE
def test_a_name_that_matches_no_duplicate_pair_refuses_before_any_write(reconcile):
    proc, out, state = reconcile(_three_pairs(), extra={"reconcile_names": '["nope"]'})
    assert proc.returncode != 0, out
    assert "names no verified duplicate pair" in out and "nope" in out, out
    assert state.writes() == []


@NEEDS_ANSIBLE
def test_no_token_or_address_reaches_the_output_at_maximum_verbosity(reconcile):
    proc, out, state = reconcile(_three_pairs(), verbosity="-vvv")
    assert proc.returncode == 0, out
    assert SENT not in out and CLIENT not in out and ADDR not in out
    assert "pair dns in cluster uhstray-cluster" in out, "the -vvv run did not reach the plan"


# --- provenance: the changelog says who created each record -------------------------------------------------


def test_the_creation_records_are_read_before_any_pair_is_selected_or_written():
    tasks = _tasks()
    origin = next(i for i, t in enumerate(tasks)
                  if t.get("name") == "NetBox: read the creation record of each candidate VM")
    judged = next(i for i, t in enumerate(tasks) if t.get("name") == "Judge the origin of each candidate pair")
    selected = next(i for i, t in enumerate(tasks)
                    if t.get("name") == "Hold the selected pairs (every verified pair when reconcile_names is blank)")
    first_write = min(i for i, t in enumerate(tasks) if _is_write(t))
    assert origin < judged < selected < first_write


def test_the_creation_record_read_queries_the_changelog_for_creates_of_virtual_machines():
    url = str(_args(_named("NetBox: read the creation record of each candidate VM"))["url"])
    assert "/api/core/object-changes/?changed_object_type=virtualization.virtualmachine" in url
    assert "changed_object_id={{ item }}" in url and "action=create" in url


def test_selection_and_the_unmatched_check_read_only_the_verified_pairs():
    selection = str(_named(
        "Hold the selected pairs (every verified pair when reconcile_names is blank)")["ansible.builtin.set_fact"])
    assert "for p in _verified_pairs" in selection and "for p in _pairs" not in selection
    unmatched = str(_named(
        "Hold the requested names that match no verified duplicate pair")["ansible.builtin.set_fact"])
    assert "_verified_pairs" in unmatched and re.search(r"(?<![A-Za-z_])_pairs\b", unmatched) is None


def test_the_bare_record_is_judged_by_its_creator_and_the_discovery_record_is_not():
    judge = str(_named("Judge the origin of each candidate pair")["ansible.builtin.set_fact"])
    assert "bare_o[0]['creators'] == [_recorder_user]" in judge
    assert "(_recorder_user not in disc_o[0]['creators'])" in judge


@NEEDS_ANSIBLE
@pytest.mark.parametrize("origins", [
    {10: ["console"]},          # the bare record was created by another account
    {11: [RECORDER]},           # the discovery record was created by the recorder account
    {10: []},                   # the bare record has no creation record
    {11: []},                   # the discovery record has no creation record
], ids=["bare-by-other", "discovery-by-recorder", "bare-missing", "discovery-missing"])
def test_an_unconfirmed_origin_is_reported_and_never_touched(reconcile, origins):
    vms = [_bare(10, "dns"), _disc(11, "dns")]
    proc, out, state = reconcile(vms, origins=origins)
    assert proc.returncode == 0, out
    assert "unverified origin, not touched: dns in cluster uhstray-cluster" in out, out
    assert state.writes() == [], state.writes()
    assert 10 in state.vms and 11 in state.vms
    assert state.vms[11]["custom_fields"] == _cf(), "the workflow fields moved before the origin was confirmed"


@NEEDS_ANSIBLE
def test_an_unverified_pair_does_not_stop_the_verified_pairs(reconcile):
    pairs = _three_pairs()
    proc, out, state = reconcile(pairs, origins={30: ["console"]})
    assert proc.returncode == 0, out
    assert "unverified origin, not touched: vault in cluster uhstray-cluster" in out, out
    assert 10 not in state.vms and 20 not in state.vms
    assert 30 in state.vms and 31 in state.vms


@NEEDS_ANSIBLE
def test_a_requested_name_whose_origin_is_unverified_refuses_before_any_write(reconcile):
    proc, out, state = reconcile([_bare(10, "dns"), _disc(11, "dns")], origins={10: ["console"]},
                                 extra={"reconcile_names": '["dns"]'})
    assert proc.returncode != 0, out
    assert "names no verified duplicate pair" in out and "dns" in out, out
    assert state.writes() == []


@NEEDS_ANSIBLE
def test_check_mode_reports_an_unverified_origin_too(reconcile):
    proc, out, state = reconcile([_bare(10, "dns"), _disc(11, "dns")], origins={11: [RECORDER]}, check=True)
    assert proc.returncode == 0, out
    assert "unverified origin, not touched: dns" in out, out
    assert state.writes() == []


# --- constraints: rendered into the permission definitions the playbook creates ---------------------------


@NEEDS_ANSIBLE
def test_the_permission_script_renders_each_scope_with_its_constraint(tmp_path):
    profiles = playbook_yaml.plays(PROVISION)[0]["vars"]["_profiles"]
    profile = profiles["vm-reconciler"]
    task = next(t for t in playbook_yaml.plays(PROVISION)[0]["tasks"]
                if t.get("name") == "Ensure NetBox automation user and scoped permissions")
    rendered = tmp_path / "permissions.py"
    harness = tmp_path / "harness.yml"
    harness.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_profiles": profiles, "_profile_name": "vm-reconciler", "_profile": profile,
                 "_user": profile["user"], "_manage": f"cat > {rendered}"},
        "tasks": [task],
    }]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(harness)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = rendered.read_text()
    compile(text, "rendered-permission-script", "exec")
    literal = re.search(r"json\.loads\((\".*\")\)", text)
    assert literal, text
    assert json.loads(ast.literal_eval(literal.group(1))) == profile["extra_scopes"]
    # The constraint is read with .get: the changelog scope has none, so it is rendered as None (unconstrained).
    assert 's.get("constraints")' in text


# --- convergence: the dedicated user ends with exactly the declared permissions -----------------------------
#
# The rendered permission snippet runs against a small in-memory stand-in for the NetBox ORM calls it makes
# (get_user_model, ObjectPermission, ObjectType). The stand-in is test-only and keeps its state in a JSON file, so
# the real snippet's detach logic, its idempotence and its check-mode silence are exercised. The stand-in is not
# NetBox: that the M2M detach and the JSON constraints behave so in NetBox v4.5.0 is read from its source.

FAKE_ORM = '''
import atexit, json, os
_STATE = os.environ["FAKE_NETBOX_STATE"]
if os.path.exists(_STATE):
    with open(_STATE) as fh:
        _S = json.load(fh)
else:
    _S = {"users": {}, "perms": {}, "types": {}}


def _save():
    with open(_STATE, "w") as fh:
        json.dump(_S, fh, sort_keys=True)


atexit.register(_save)


class _User:
    def __init__(self, name):
        self.username = name
        self.pk = _S["users"][name]["id"]


class _UserQS:
    def __init__(self, names):
        self._names = names

    def first(self):
        return _User(self._names[0]) if self._names else None


class _UserManager:
    def get_or_create(self, username, defaults=None):
        if username in _S["users"]:
            return _User(username), False
        _S["users"][username] = {"id": len(_S["users"]) + 1}
        return _User(username), True

    def filter(self, username=None):
        return _UserQS([username] if username in _S["users"] else [])


def get_user_model():
    class User:
        objects = _UserManager()
    return User


class _Type:
    def __init__(self, key):
        self.pk = _S["types"].setdefault(key, len(_S["types"]) + 1)


class _TypeManager:
    def get_by_natural_key(self, app_label, model):
        return _Type(app_label + "." + model)


class ObjectType:
    objects = _TypeManager()


class _Exists:
    def __init__(self, found):
        self._found = found

    def exists(self):
        return self._found


class _Related:
    def __init__(self, perm, field):
        self._perm, self._field = perm, field

    def _ids(self):
        return _S["perms"][self._perm][self._field]

    def values_list(self, field, flat=False):
        return list(self._ids())

    def set(self, ids):
        _S["perms"][self._perm][self._field] = list(ids)

    def filter(self, pk=None):
        return _Exists(pk in self._ids())

    def add(self, user):
        if user.pk not in self._ids():
            self._ids().append(user.pk)

    def remove(self, user):
        if user.pk in self._ids():
            self._ids().remove(user.pk)


class _Perm:
    _FIELDS = ("enabled", "actions", "constraints")

    def __init__(self, name):
        object.__setattr__(self, "name", name)

    def __getattr__(self, key):
        if key in _Perm._FIELDS:
            return _S["perms"][self.name][key]
        if key in ("object_types", "users"):
            return _Related(self.name, key)
        raise AttributeError(key)

    def __setattr__(self, key, value):
        if key not in _Perm._FIELDS:
            raise AttributeError(key)
        _S["perms"][self.name][key] = value

    def save(self, update_fields=None):
        pass


class _PermQS:
    def __init__(self, names):
        self._names = list(names)

    def filter(self, users=None):
        return _PermQS([n for n in self._names if users.pk in _S["perms"][n]["users"]])

    def exclude(self, name__in=()):
        return _PermQS([n for n in self._names if n not in name__in])

    def order_by(self, *fields):
        return _PermQS(sorted(self._names))

    def values_list(self, field, flat=False):
        return list(self._names)

    def __iter__(self):
        return iter([_Perm(n) for n in self._names])


class _PermManager:
    def get_or_create(self, name, defaults=None):
        if name in _S["perms"]:
            return _Perm(name), False
        record = {"enabled": True, "actions": [], "constraints": None, "object_types": [], "users": []}
        record.update(defaults or {})
        _S["perms"][name] = record
        return _Perm(name), True

    def filter(self, users=None):
        return _PermQS(list(_S["perms"])).filter(users=users)


class ObjectPermission:
    objects = _PermManager()
'''

SHIM = {
    "django/__init__.py": "",
    "django/contrib/__init__.py": "",
    "django/contrib/auth/__init__.py": "from _fake_orm import get_user_model\n",
    "users/__init__.py": "",
    "users/models.py": "from _fake_orm import ObjectPermission\n",
    "core/__init__.py": "",
    "core/models.py": "from _fake_orm import ObjectType\n",
    "manage.py": "import sys\nexec(compile(sys.stdin.read(), '<shell>', 'exec'), {'__name__': '__main__'})\n",
}

DECLARED = {"workflow-vm-reconcile-vm-view", "workflow-vm-reconcile-view", "workflow-vm-reconcile-changelog-view",
            "workflow-vm-reconcile-change", "workflow-vm-reconcile-delete"}


def _seed_state():
    def perm(users, actions=("view",)):
        return {"enabled": True, "actions": list(actions), "constraints": None, "object_types": [], "users": users}
    return {"users": {"workflow-vm-reconciler": {"id": 1}, "someone": {"id": 2}}, "types": {}, "perms": {
        "workflow-vm-reconcile": perm([1], ("view", "change", "delete")),   # the earlier broad grant: undeclared
        "manual-grant": perm([1]),                                          # granted by hand: undeclared
        "workflow-vm-reconcile-vm-view": perm([1]),                         # declared, already attached
        "other-user-perm": perm([2]),                                       # another account: not ours to touch
    }}


def _attached(state, user_id):
    return {name for name, rec in state["perms"].items() if user_id in rec["users"]}


@pytest.fixture
def converge(tmp_path):
    for rel, text in SHIM.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / "_fake_orm.py").write_text(FAKE_ORM)
    state = tmp_path / "state.json"

    def run(check=False, seed=None):
        if seed is not None:
            state.write_text(json.dumps(seed, sort_keys=True))
        manage = (f"FAKE_NETBOX_STATE={state} PYTHONPATH={tmp_path} python3 {tmp_path}/manage.py shell")
        play = playbook_yaml.plays(PROVISION)[0]
        names = {"Ensure NetBox automation user and scoped permissions",
                 "Show the permissions removed from the dedicated user",
                 "Dry run: report the permissions the dedicated user would lose",
                 "Dry run: show the permissions that would be removed"}
        tasks = [t for t in play["tasks"] if t.get("name") in names]
        assert len(tasks) == 4, [t.get("name") for t in tasks]
        harness = tmp_path / "harness.yml"
        harness.write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {**play["vars"], "_manage": manage, "netbox_token_profile": "vm-reconciler"},
            "tasks": tasks,
        }]))
        env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
        env["ANSIBLE_NOCOLOR"] = "1"
        cmd = ["ansible-playbook", "-i", "localhost,", str(harness)] + (["--check"] if check else [])
        proc = subprocess.run(cmd, cwd=REPO, env=env, text=True, capture_output=True, check=False, timeout=300)
        return proc, proc.stdout + proc.stderr, json.loads(state.read_text())

    return run


@NEEDS_ANSIBLE
def test_the_dedicated_user_ends_with_exactly_the_declared_permissions(converge):
    proc, out, state = converge(seed=_seed_state())
    assert proc.returncode == 0, out
    assert "removed: manual-grant" in out and "removed: workflow-vm-reconcile" in out, out
    assert _attached(state, 1) == DECLARED, _attached(state, 1)
    # Detach only: the stale grants still exist, and another account's grant is untouched.
    assert "workflow-vm-reconcile" in state["perms"] and "manual-grant" in state["perms"]
    assert _attached(state, 2) == {"other-user-perm"}


@NEEDS_ANSIBLE
def test_a_second_run_changes_nothing(converge):
    converge(seed=_seed_state())
    proc, out, state = converge()
    assert proc.returncode == 0, out
    assert "removed: " not in out, out
    assert _attached(state, 1) == DECLARED
    recap = re.search(r"changed=(\d+)", out.split("PLAY RECAP")[1])
    assert recap and recap.group(1) == "0", out


@NEEDS_ANSIBLE
def test_check_mode_reports_the_removals_and_changes_nothing(converge):
    seed = _seed_state()
    proc, out, state = converge(check=True, seed=seed)
    assert proc.returncode == 0, out
    assert "would remove: manual-grant" in out and "would remove: workflow-vm-reconcile" in out, out
    assert state == seed, "a dry run wrote to the store"
    assert _attached(state, 1) == {"workflow-vm-reconcile", "manual-grant", "workflow-vm-reconcile-vm-view"}


@NEEDS_ANSIBLE
def test_the_removal_names_are_printed_and_no_token_is_in_the_output(converge):
    proc, out, _ = converge(seed=_seed_state())
    assert proc.returncode == 0, out
    assert SENT not in out and CLIENT not in out
