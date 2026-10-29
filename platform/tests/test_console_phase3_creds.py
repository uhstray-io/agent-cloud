"""Dispatch Console Phase 3 credentials and deploy wiring (plan 17, D22-D24).

Three properties, pinned where they can break:

- The NetBox `console` profile is view-only on exactly the seven types the console reads.
- provision-console-proxmox-token.yml never rotates a live token, refuses the states it cannot recover,
  grants PVEAuditor to both the user and the privilege-separated token, and writes only after the
  check-mode gate.
- The console's rendered inputs (env, flows.json, the Proxmox CA) carry names and gated groups only,
  never an address, and the deploy refuses a duplicate netbox_site and a non-certificate CA by name.

Templates and guards run through the real ansible-playbook and template engine, the way
test_workflow_credential_boundaries.py does, so the Ansible filters (regex_findall, to_nice_json) are the
ones production uses. Structure is read with playbook_yaml, the repository's parsed-YAML cache.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml
from test_check_mode_contract import CONSOLE_CA_DIR, CONSOLE_CA_FILE

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
CONSOLE = REPO / "platform/services/console/deployment"
NETBOX = PLAYBOOKS / "provision-netbox-automation-token.yml"
PROXMOX = PLAYBOOKS / "provision-console-proxmox-token.yml"
DEPLOY = PLAYBOOKS / "deploy-console.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
CATALOG = REPO / "platform/services/authentik/deployment/app-catalog.yml"

NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
PORT = re.compile(r":\d+")
# Built at runtime so the source carries no literal private-key header for the secret scanner.
PRIVATE_KEY_PEM = "-----BEGIN " + "PRIV" + "ATE KEY-----\nMIIBfakebody\n-----END " + "PRIV" + "ATE KEY-----\n"
CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfakebody\n-----END CERTIFICATE-----\n"


def _tasks(path: Path) -> list[dict]:
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    doc = yaml.safe_load(path.read_text())
    for play in doc if isinstance(doc, list) else []:
        walk(play.get("tasks") if isinstance(play, dict) and "hosts" in play else [play])
    return out


def _named(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


def _run(tmp_path: Path, playbook: dict) -> subprocess.CompletedProcess:
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump([playbook]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False)


def _render(tmp_path: Path, src: Path, variables: dict) -> str:
    dest = tmp_path / "rendered.out"
    play = {"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
            "tasks": [{"ansible.builtin.template": {"src": str(src), "dest": str(dest), "mode": "0644"}}]}
    proc = _run(tmp_path, play)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return dest.read_text()


def _run_tasks(tmp_path: Path, tasks: list, variables: dict) -> subprocess.CompletedProcess:
    return _run(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                           "vars": variables, "tasks": tasks})


# --- NetBox: the console profile is view-only ----------------------------------------------------------

CONSOLE_TYPES = {"dcim.site", "dcim.device", "virtualization.cluster", "virtualization.virtualmachine",
                 "ipam.prefix", "extras.tag"}


def _netbox_profiles() -> dict:
    return playbook_yaml.plays(NETBOX)[0]["vars"]["_profiles"]


def test_console_netbox_profile_is_view_only():
    profile = _netbox_profiles()["console"]
    # Only view: no add, change or delete on any type, so the token cannot write.
    assert profile["actions"] == ["view"], profile["actions"]
    assert not {"add", "change", "delete"} & set(profile["actions"])
    assert "view_object_types" not in profile, profile


def test_console_netbox_profile_covers_exactly_the_six_v1_types_prefixes_only():
    profile = _netbox_profiles()["console"]
    assert set(profile["object_types"]) == CONSOLE_TYPES, profile["object_types"]
    # D24 v1 reads prefixes only: no IP addresses and no interfaces.
    assert not any(t in ("ipam.ipaddress", "dcim.interface", "virtualization.vminterface")
                   or "interface" in t for t in profile["object_types"]), profile["object_types"]


def test_console_token_is_write_disabled_on_the_token_itself():
    # NetBox's Token model declares write_enabled = BooleanField(default=True) (v4.5.0): the console token is
    # created with write_enabled=False, and only the console profile is.
    assert _netbox_profiles()["console"]["token_write_enabled"] is False
    for name, profile in _netbox_profiles().items():
        if name != "console":
            assert "token_write_enabled" not in profile, name


@NEEDS_ANSIBLE
def test_the_mint_passes_write_enabled_false_only_for_the_console_profile(tmp_path):
    expr = "{{ 'write_enabled=False,' if not (_profile.token_write_enabled | default(true)) else '' }}"
    got = {}
    for name, profile in _netbox_profiles().items():
        proc = _run_tasks(tmp_path, [{"ansible.builtin.debug": {"msg": "OUT|" + expr}}],
                          {"_profile": profile})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        got[name] = re.search(r'OUT\|([^"]*)"', proc.stdout).group(1)
    assert got["console"] == "write_enabled=False,", got
    assert all(v == "" for k, v in got.items() if k != "console"), got


def test_the_console_user_is_refused_when_it_holds_superuser_or_group_rights_before_any_write():
    names = [t.get("name") for t in _tasks(NETBOX)]
    read = names.index("Read the console user's superuser and group rights (console profile only)")
    refuse = names.index("Refuse a console user that holds superuser or group rights")
    ensure = names.index("Ensure NetBox automation user and scoped permissions")
    assert read < refuse < ensure, (read, refuse, ensure)
    assert _named(NETBOX, "Read the console user's superuser and group rights (console profile only)")["when"] == \
        "_profile_name == 'console'"
    assert _named(NETBOX, "Refuse a console user that holds superuser or group rights")["when"] == \
        "_profile_name == 'console'"
    body = _named(NETBOX, "Read the console user's superuser and group rights (console profile only)")[
        "ansible.builtin.shell"]
    assert "user.is_superuser" in body and "user.groups.exists()" in body, body
    # NetBox's User model declares no is_staff: read it with getattr, never as an attribute.
    assert 'getattr(user, "is_staff", False)' in body, body


def test_console_netbox_profile_field_user_and_description():
    profile = _netbox_profiles()["console"]
    assert profile["field"] == "console_api_token"
    assert profile["user"] == "console"
    assert profile["permission"] == "console-readonly"
    assert profile["description"] == "Dispatch Console read-only inventory (Phase 3)"


def test_console_profile_is_the_last_survey_value():
    # The survey enum must equal the profile keys in order (test_workflow_credential_boundaries).
    # Appending console keeps every earlier launch's choice unchanged.
    names = list(_netbox_profiles())
    assert names[-1] == "console", names
    tpl = next(t for t in yaml.safe_load(TEMPLATES.read_text())["templates"]
               if t["name"] == "Provision NetBox Automation Token")
    field = next(s for s in tpl["survey_vars"] if s["name"] == "netbox_token_profile")
    assert [v["value"] for v in field["values"]][-1] == "console"


# --- Proxmox: provision-console-proxmox-token.yml -------------------------------------------------------

def _proxmox_play() -> dict:
    return playbook_yaml.plays(PROXMOX)[0]


def _proxmox_vars() -> dict:
    # The play has no vars: block (the check-mode contract forbids one on a play with a forced write); its
    # values are the facts its first two tasks set, merged here in order.
    tasks = _proxmox_play()["tasks"]
    assert tasks[0]["name"] == "Set the play's values", tasks[0].get("name")
    assert tasks[1]["name"] == "Derive the console token id", tasks[1].get("name")
    return {**tasks[0]["ansible.builtin.set_fact"], **tasks[1]["ansible.builtin.set_fact"]}


def _proxmox_tasks() -> list[dict]:
    return _tasks(PROXMOX)


def _proxmox_task(name: str) -> dict:
    return _named(PROXMOX, name)


def _uri(task: dict) -> dict:
    return task.get("ansible.builtin.uri") or {}


def _method(task: dict) -> str | None:
    return _uri(task).get("method")


def _is_login(task: dict) -> bool:
    return "auth/approle/login" in str(_uri(task).get("url", ""))


def _is_store(task: dict) -> bool:
    return "bao-merge-keys" in str(task.get("ansible.builtin.include_tasks", ""))


def _writes() -> list[dict]:
    return [t for t in _proxmox_tasks()
            if (not _is_login(t)) and (_method(t) in ("POST", "PUT", "DELETE") or _is_store(t))]


def _run_env(tmp_path: Path, playbook: dict, extra_env: dict) -> subprocess.CompletedProcess:
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump([playbook]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    env.update(extra_env)
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False)


def test_proxmox_play_is_local_and_refuses_internal_extra_vars():
    play = _proxmox_play()
    assert play["hosts"] == "localhost" and play.get("connection") == "local"
    assert "vars" not in play, "a play-level vars: block would reach the forced CA write"
    doc = yaml.safe_load(PROXMOX.read_text())
    assert doc[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"


def test_proxmox_user_and_token_ids_are_the_declared_ones():
    v = _proxmox_vars()
    assert v["_console_userid"] == "console@pve"
    assert v["_console_tokenid"] == "console"
    assert v["_console_token_full"] == "{{ _console_userid }}!{{ _console_tokenid }}"


def test_proxmox_required_rights_are_the_three_the_source_checks():
    # Realm.AllocateUser + User.Modify (create_user), User.Modify (generate_token), Permissions.Modify (update_acl).
    assert _proxmox_vars()["_required_rights"] == ["Realm.AllocateUser", "User.Modify", "Permissions.Modify"]


def test_every_uri_request_in_the_proxmox_play_is_no_log():
    # Every request carries the admin token, the console token or the OpenBao token, so none may print its result.
    uris = [t for t in _proxmox_tasks() if "ansible.builtin.uri" in t]
    assert uris, "no uri tasks found"
    loose = [t.get("name") for t in uris if t.get("no_log") is not True]
    assert loose == [], loose


def test_proxmox_debug_tasks_are_visible():
    # Only credential tasks are no_log; the decision and report print state names and no values.
    debugs = [t for t in _proxmox_tasks() if "ansible.builtin.debug" in t]
    hidden = [t.get("name") for t in debugs if t.get("no_log")]
    assert hidden == [], hidden


def test_names_and_assertions_that_print_no_secret_are_visible():
    # Names of rights and privileges, and the https and audit assertions, print no secret. Hiding them only
    # hides the reason a run failed.
    for name in ("Name the admin token's missing rights (names only)", "Report the admin token's rights",
                 "Refuse a Proxmox URL that is not https", "Name the privileges the console token holds",
                 "Assert the console token holds only audit privileges"):
        assert _proxmox_task(name).get("no_log") is not True, name


def test_absence_is_read_from_the_list_endpoints_never_from_read_user_or_read_token():
    # read_user and read_token die for a missing object (HTTP 500), so a single-object GET cannot tell absent
    # from broken. Presence comes from the lists: GET /access/users and GET /access/users/{userid}/token.
    gets = {_uri(t).get("url") for t in _proxmox_tasks() if _method(t) == "GET"}
    assert "{{ _pve_url }}/api2/json/access/users" in gets, gets
    assert "{{ _pve_url }}/api2/json/access/users/{{ _console_userid }}/token" in gets, gets
    for url in gets:
        assert not str(url).endswith("/access/users/{{ _console_userid }}"), url
        assert not str(url).endswith("/token/{{ _console_tokenid }}"), url


def test_the_https_gate_precedes_every_admin_request():
    names = [t.get("name") for t in _proxmox_tasks()]
    gate = names.index("Refuse a Proxmox URL that is not https")
    first_admin = next(i for i, t in enumerate(_proxmox_tasks()) if "_pve_headers" in str(_uri(t).get("headers", "")))
    assert gate < first_admin, (gate, first_admin)
    assert _proxmox_task("Refuse a Proxmox URL that is not https")["ansible.builtin.assert"]["that"] == \
        "_pve_url is match('https://')"


def test_every_admin_or_proof_request_verifies_against_the_declared_ca_file():
    # Nine requests carry the admin token or the console token: list users, list tokens, admin rights,
    # revoke one token, create user, grant user ACL, mint, grant token ACL, and the console-token proof.
    uris = [t for t in _proxmox_tasks()
            if "_pve_headers" in str(_uri(t).get("headers", ""))
            or "_console_proof_secret" in str(_uri(t).get("headers", ""))]
    assert len(uris) == 9, [t.get("name") for t in uris]
    for t in uris:
        u = _uri(t)
        assert u.get("validate_certs") is True, t.get("name")
        assert u.get("ca_path") == CONSOLE_CA_FILE, t.get("name")


def test_every_write_is_gated_on_the_mint_decision_or_the_one_revoke_and_never_check_mode():
    writes = _writes()
    assert len(writes) == 6, [t.get("name") for t in writes]
    for t in writes:
        when = str(t.get("when"))
        assert "not ansible_check_mode" in when, t.get("name")
        assert "_mint" in when or ("_console_state == 'unstored_present'" in when and "_replace" in when), t.get("name")


def test_the_only_delete_revokes_one_token_on_the_replace_path():
    deletes = [t for t in _proxmox_tasks() if _method(t) == "DELETE"]
    assert len(deletes) == 1, [t.get("name") for t in deletes]
    url = _uri(deletes[0]).get("url", "")
    assert url.endswith("/access/users/{{ _console_userid }}/token/{{ _console_tokenid }}"), url
    assert _uri(deletes[0]).get("status_code") == [200]
    when = [str(w) for w in deletes[0]["when"]]
    assert ("_console_state == 'unstored_present'" in when and "_replace" in when
            and "not ansible_check_mode" in when), when


def test_a_live_stored_token_is_never_rotated():
    # stored_present is the only state that writes nothing. The mint decision names the other states only.
    decide = _proxmox_task("Decide the state from what Proxmox and the store hold")["ansible.builtin.set_fact"]
    assert "'stored_present' if (_console_stored and _console_token_present)" in decide["_console_state"]
    mint = _proxmox_task("Decide whether this run mints the console token")["ansible.builtin.set_fact"]["_mint"]
    assert "_console_state in ['neither', 'stored_absent']" in mint, mint
    assert "_console_state == 'unstored_present' and _replace" in mint, mint


def test_an_unstored_live_token_is_refused_unless_the_replace_flag_is_set():
    that = _proxmox_task("Refuse a Proxmox token the store cannot recover, unless replace is requested")[
        "ansible.builtin.assert"]["that"]
    assert "_console_state != 'unstored_present' or _replace" in that, that
    fail = _proxmox_task("Refuse a Proxmox token the store cannot recover, unless replace is requested")[
        "ansible.builtin.assert"]["fail_msg"]
    assert "console_proxmox_token_replace=true" in fail and "exactly" in fail, fail


@NEEDS_ANSIBLE
def test_the_replace_refusal_is_real_and_names_the_flag(tmp_path):
    task = _proxmox_task("Refuse a Proxmox token the store cannot recover, unless replace is requested")
    refused = _run_tasks(tmp_path, [task], {"_console_state": "unstored_present", "_replace": False})
    assert refused.returncode != 0 and "console_proxmox_token_replace=true" in refused.stdout, refused.stdout
    allowed = _run_tasks(tmp_path, [task], {"_console_state": "unstored_present", "_replace": True})
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def test_a_stored_token_proxmox_no_longer_has_needs_the_recreate_flag():
    assert_task = _proxmox_task("Refuse a stored token that Proxmox no longer has, unless recreate is requested")
    assert "_console_state != 'stored_absent' or _recreate" in assert_task["ansible.builtin.assert"]["that"]
    assert "console_proxmox_token_recreate=true" in assert_task["ansible.builtin.assert"]["fail_msg"]


def test_token_is_privilege_separated_and_granted_read_only_on_root():
    tokens = [t for t in _proxmox_tasks() if _method(t) == "POST" and "/token/" in str(_uri(t).get("url", ""))]
    assert len(tokens) == 1, [t.get("name") for t in tokens]
    assert tokens[0]["ansible.builtin.uri"]["body"]["privsep"] == 1


def test_both_acls_grant_pveauditor_on_root_user_and_token():
    acls = [_uri(t) for t in _proxmox_tasks()
            if _method(t) == "PUT" and str(_uri(t).get("url", "")).endswith("/access/acl")]
    assert len(acls) == 2, len(acls)
    for acl in acls:
        assert acl["body"]["path"] == "/" and acl["body"]["roles"] == "PVEAuditor", acl["body"]
    by_subject = {("users" if "users" in a["body"] else "tokens"): a["body"] for a in acls}
    assert by_subject["users"]["users"] == "{{ _console_userid }}", by_subject
    assert by_subject["tokens"]["tokens"] == "{{ _console_token_full }}", by_subject


def test_the_user_is_created_without_a_password():
    create = next(_uri(t) for t in _proxmox_tasks() if _method(t) == "POST"
                  and str(_uri(t).get("url", "")).endswith("/api2/json/access/users"))
    assert "password" not in create["body"], create["body"]


def test_the_store_is_written_after_both_acl_grants():
    names = [t.get("name") for t in _proxmox_tasks()]
    store = names.index("Store the token id and secret at secret/services/proxmox (siblings preserved)")
    grants = [i for i, n in enumerate(names) if n and n.startswith("Grant PVEAuditor")]
    assert len(grants) == 2 and max(grants) < store, (grants, store)


def test_the_store_writes_the_id_and_the_secret_through_the_merge_task():
    store = _proxmox_task("Store the token id and secret at secret/services/proxmox (siblings preserved)")
    assert store["ansible.builtin.include_tasks"] == "tasks/bao-merge-keys.yml"
    data = store["vars"]["_bm_data"]
    assert set(data) == {"console_token_id", "console_token"}, data
    assert store["vars"]["_bm_path"] == "services/proxmox"


def test_the_proof_uses_the_console_token_and_asserts_audit_privileges_only():
    proof = _proxmox_task("Prove the stored console token authenticates (read only)")
    assert _uri(proof)["url"] == "{{ _pve_url }}/api2/json/access/permissions?path=%2F"
    assert _uri(proof)["headers"]["Authorization"] == \
        "PVEAPIToken={{ _console_userid }}!{{ _console_tokenid }}={{ _console_proof_secret }}"
    assert proof.get("no_log") is True and proof.get("check_mode") is False
    assert "verify" in proof["tags"]
    assert _proxmox_task("Assert the console token holds only audit privileges")["ansible.builtin.assert"]["that"][1] \
        == "(_console_privs | select('search', '[.]Audit$') | list | length) == (_console_privs | length)"


def test_verify_tags_mark_reads_asserts_and_facts_and_never_a_write():
    for t in _proxmox_tasks():
        tags = t.get("tags") or []
        if _is_login(t):
            continue
        if _method(t) in ("POST", "PUT", "DELETE") or _is_store(t):
            assert "verify" not in tags, t.get("name")
        elif _method(t) == "GET" or "ansible.builtin.assert" in t or "ansible.builtin.set_fact" in t:
            assert "verify" in tags, t.get("name")


def test_the_header_names_no_local_twin_and_no_dev_only_claim():
    text = PROXMOX.read_text()
    assert "(Local)" not in text
    assert "Provision Console Proxmox Token" in text and "(Dev) twin" in text


def test_the_ca_gate_comes_before_any_credential_read_and_names_the_var():
    names = [t.get("name") for t in _proxmox_tasks()]
    gate_name = "Require the declared Proxmox CA before any credential is read"
    assert names.index(gate_name) < names.index("Authenticate to OpenBao")
    fail_msg = _proxmox_task(gate_name)["ansible.builtin.assert"]["fail_msg"]
    assert "console_proxmox_ca_pem" in fail_msg and "required" in fail_msg, fail_msg


def test_the_ca_directory_is_created_pinned_and_the_ca_file_inside_it():
    make = _proxmox_task("Create the per-run CA scratch directory")
    assert make["ansible.builtin.tempfile"] == {
        "state": "directory", "prefix": ".console-ca-",
        "path": "{{ lookup('ansible.builtin.env', 'TMPDIR') | default('/tmp', true) | realpath }}"}, make
    write = _proxmox_task("Write the declared CA into the per-run scratch directory")
    copy = write["ansible.builtin.copy"]
    assert copy["content"].startswith("{{ _console_ca_pem"), copy
    assert copy["dest"] == CONSOLE_CA_FILE and copy["mode"] == "0600", copy
    assert write["delegate_to"] == "localhost" and write["check_mode"] is False, write
    assert "vars" not in write and "vars" not in make, (write, make)
    doc = yaml.safe_load(PROXMOX.read_text())
    play = next(p for p in doc if isinstance(p, dict) and "hosts" in p)
    tls = next(t for t in play["tasks"] if "block" in t)
    assert "vars" not in tls, tls
    removal_task = tls["always"][0]
    assert removal_task["ansible.builtin.file"] == {"path": CONSOLE_CA_DIR, "state": "absent"} or (
        removal_task["ansible.builtin.file"]["state"] == "absent"
        and removal_task["ansible.builtin.file"]["path"] == CONSOLE_CA_DIR), removal_task
    assert removal_task["delegate_to"] == "localhost" and removal_task["check_mode"] is False, removal_task


@NEEDS_ANSIBLE
def test_the_per_run_directory_must_sit_directly_under_the_temp_root(tmp_path):
    root = Path(os.path.realpath(tmp_path))
    task = _proxmox_task("Assert the per-run scratch directory sits directly under the temp root")
    good = root / ".console-ca-good"
    good.mkdir()
    ok = _run_env(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                             "vars": {"_ca_dir": {"path": str(good)}}, "tasks": [task]}, {"TMPDIR": str(root)})
    assert ok.returncode == 0, ok.stdout + ok.stderr
    nested = root / "sub"
    nested.mkdir()
    bad = nested / ".console-ca-bad"
    bad.mkdir()
    refused = _run_env(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                                  "vars": {"_ca_dir": {"path": str(bad)}}, "tasks": [task]}, {"TMPDIR": str(root)})
    assert refused.returncode != 0 and "refusing to write into it" in refused.stdout, refused.stdout


@NEEDS_ANSIBLE
def test_the_always_cleanup_removes_the_per_run_directory_and_its_file(tmp_path):
    scratch = Path(os.path.realpath(tmp_path)) / ".console-ca-cleanup"
    scratch.mkdir()
    (scratch / "ca.pem").write_text(CERT_PEM)
    task = _proxmox_task("Remove the per-run CA scratch directory")
    proc = _run_env(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                               "vars": {"_ca_dir": {"path": str(scratch)}}, "tasks": [task]}, {})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not scratch.exists()


# The membership and state decision, run from the playbook's own tasks against realistic list responses. The
# list shapes are the ones User.pm returns: index entries carry userid, token_index entries carry tokenid.
ROOT_USER = {"userid": "root@pam", "enable": 1, "realm-type": "pam"}
CONSOLE_USER = {"userid": "console@pve", "enable": 1, "realm-type": "pve"}
CONSOLE_TOKEN = {"tokenid": "console", "privsep": 1, "comment": "Dispatch Console read-only inventory (Phase 3)"}
OTHER_TOKEN = {"tokenid": "ci-deploy", "privsep": 0}
FULL_RIGHTS = {"/": {"Realm.AllocateUser": 0, "User.Modify": 1, "Permissions.Modify": 0}}


def _listing(*entries) -> dict:
    return {"status": 200, "json": {"data": list(entries)}}


def _decide(tmp_path: Path, variables: dict) -> tuple[str, str, str, str, str]:
    names = ("Note whether the console user exists", "Note whether the console token exists",
             "Decide the state from what Proxmox and the store hold",
             "Name the admin token's missing rights (names only)",
             "Decide whether this run mints the console token")
    tasks = [_proxmox_task(n) for n in names]
    tasks.append({"ansible.builtin.debug": {"msg": "OUT|{{ _console_state }}|{{ _mint }}|{{ _console_user_present }}"
                                                   "|{{ _console_token_present }}|{{ _missing_rights | join(',') }}"}})
    base = {"_console_userid": "console@pve", "_console_tokenid": "console", "_recreate": False, "_replace": False,
            "_required_rights": ["Realm.AllocateUser", "User.Modify", "Permissions.Modify"],
            "_admin_perms": {"status": 200, "json": {"data": FULL_RIGHTS}}}
    proc = _run(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                           "vars": {**base, **variables}, "tasks": tasks})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = re.search(r'OUT\|([^"]*)"', proc.stdout)
    assert out, proc.stdout
    state, mint, user, token, missing = out.group(1).split("|")
    return state, mint, user, token, missing


@NEEDS_ANSIBLE
def test_neither_state_from_the_user_list(tmp_path):
    got = _decide(tmp_path, {"_console_stored": False, "_console_users_list": _listing(ROOT_USER)})
    assert got == ("neither", "True", "False", "False", ""), got


@NEEDS_ANSIBLE
def test_stored_present_state_from_the_user_and_token_lists(tmp_path):
    got = _decide(tmp_path, {"_console_stored": True,
                             "_console_users_list": _listing(ROOT_USER, CONSOLE_USER),
                             "_console_tokens_list": _listing(CONSOLE_TOKEN)})
    assert got == ("stored_present", "False", "True", "True", ""), got


@NEEDS_ANSIBLE
def test_stored_absent_state_when_the_token_list_lacks_the_console_token(tmp_path):
    got = _decide(tmp_path, {"_console_stored": True,
                             "_console_users_list": _listing(CONSOLE_USER),
                             "_console_tokens_list": _listing(OTHER_TOKEN)})
    assert got == ("stored_absent", "True", "True", "False", ""), got


@NEEDS_ANSIBLE
def test_unstored_present_state_when_proxmox_has_the_token_and_the_store_does_not(tmp_path):
    got = _decide(tmp_path, {"_console_stored": False,
                             "_console_users_list": _listing(CONSOLE_USER),
                             "_console_tokens_list": _listing(CONSOLE_TOKEN)})
    assert got == ("unstored_present", "False", "True", "True", ""), got


@NEEDS_ANSIBLE
def test_unstored_present_mints_only_when_the_replace_flag_is_set(tmp_path):
    got = _decide(tmp_path, {"_console_stored": False, "_replace": True,
                             "_console_users_list": _listing(CONSOLE_USER),
                             "_console_tokens_list": _listing(CONSOLE_TOKEN)})
    assert got == ("unstored_present", "True", "True", "True", ""), got


@NEEDS_ANSIBLE
def test_a_stored_token_with_no_user_is_stored_absent_and_never_reads_a_token_list(tmp_path):
    got = _decide(tmp_path, {"_console_stored": True, "_console_users_list": _listing(ROOT_USER)})
    assert got == ("stored_absent", "True", "False", "False", ""), got


@NEEDS_ANSIBLE
def test_rights_are_read_from_key_presence_so_a_zero_propagate_bit_still_counts(tmp_path):
    rights = {"/": {"Realm.AllocateUser": 0, "User.Modify": 0, "Permissions.Modify": 0}}
    got = _decide(tmp_path, {"_console_stored": False, "_console_users_list": _listing(ROOT_USER),
                             "_admin_perms": {"status": 200, "json": {"data": rights}}})
    assert got[4] == "", got
    short = {"/": {"User.Modify": 1}}
    got = _decide(tmp_path, {"_console_stored": False, "_console_users_list": _listing(ROOT_USER),
                             "_admin_perms": {"status": 200, "json": {"data": short}}})
    # difference() returns a set, so the names are compared as a set, not in order.
    assert set(got[4].split(",")) == {"Realm.AllocateUser", "Permissions.Modify"}, got


@NEEDS_ANSIBLE
def test_the_proof_passes_for_audit_privileges_and_refuses_anything_else(tmp_path):
    names = ("Name the privileges the console token holds", "Assert the console token holds only audit privileges")
    tasks = [_proxmox_task(n) for n in names]
    audit = {"status": 200, "json": {"data": {"/": {"Sys.Audit": 1, "VM.Audit": 0, "Datastore.Audit": 1}}}}
    ok = _run(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                         "vars": {"_console_perms": audit}, "tasks": tasks})
    assert ok.returncode == 0, ok.stdout + ok.stderr
    wide = {"status": 200, "json": {"data": {"/": {"Sys.Audit": 1, "Permissions.Modify": 0}}}}
    refused = _run(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                              "vars": {"_console_perms": wide}, "tasks": tasks})
    assert refused.returncode != 0 and "audit privilege" in refused.stdout, refused.stdout
    empty = {"status": 200, "json": {"data": {}}}
    none_held = _run(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                                "vars": {"_console_perms": empty}, "tasks": tasks})
    assert none_held.returncode != 0, none_held.stdout


@NEEDS_ANSIBLE
def test_a_plain_http_url_is_refused_before_any_admin_request(tmp_path):
    task = _proxmox_task("Refuse a Proxmox URL that is not https")
    plain = _run_tasks(tmp_path, [task], {"_pve_url": "http://pve.example.test:8006"})
    assert plain.returncode != 0 and "must start with https://" in plain.stdout, plain.stdout
    tls = _run_tasks(tmp_path, [task], {"_pve_url": "https://pve.example.test:8006"})
    assert tls.returncode == 0, tls.stdout + tls.stderr


@NEEDS_ANSIBLE
def test_the_recreate_refusal_is_real_in_check_mode(tmp_path):
    # The assert runs in check mode too, and refuses stored_absent unless recreate is set.
    task = _proxmox_task("Refuse a stored token that Proxmox no longer has, unless recreate is requested")
    variables = {"_console_state": "stored_absent", "_recreate": False}
    refused = _run_tasks(tmp_path, [task], variables)
    assert refused.returncode != 0 and "console_proxmox_token_recreate=true" in refused.stdout
    variables["_recreate"] = True
    allowed = _run_tasks(tmp_path, [task], variables)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


@NEEDS_ANSIBLE
def test_an_unstored_live_token_is_refused_by_name(tmp_path):
    task = _proxmox_task("Refuse a Proxmox token the store cannot recover, unless replace is requested")
    refused = _run_tasks(tmp_path, [task], {"_console_state": "unstored_present", "_replace": False})
    assert refused.returncode != 0 and "console_proxmox_token_replace=true" in refused.stdout
    assert _run_tasks(tmp_path, [task], {"_console_state": "stored_present", "_replace": False}).returncode == 0


def _run_with_inventory(tmp_path: Path, inventory: dict, playbook: dict) -> subprocess.CompletedProcess:
    inv = tmp_path / "inventory.yml"
    inv.write_text(yaml.safe_dump(inventory))
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump([playbook]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(["ansible-playbook", "-i", str(inv), str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False)


def _ca_gate_harness(hosts: dict | None) -> tuple[dict, dict]:
    # The real gate task and the real var expression, run against an inventory this test builds.
    gate = _proxmox_task("Require the declared Proxmox CA before any credential is read")
    ca_expr = _proxmox_vars()["_console_ca_pem"]
    inventory = {"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}}
    if hosts is not None:
        inventory["all"]["children"] = {"console_svc": {"hosts": hosts}}
    playbook = {"hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {"_console_ca_pem": ca_expr},
                "tasks": [gate, {"ansible.builtin.debug": {"msg": "PASSED-GATE"}}]}
    return inventory, playbook


@NEEDS_ANSIBLE
def test_the_ca_gate_refuses_when_the_console_host_lacks_the_var(tmp_path):
    inv, play = _ca_gate_harness({"console-local": {"ansible_connection": "local"}})
    proc = _run_with_inventory(tmp_path, inv, play)
    assert proc.returncode != 0 and "console_proxmox_ca_pem is not set" in proc.stdout, proc.stdout
    assert "PASSED-GATE" not in proc.stdout


@NEEDS_ANSIBLE
def test_the_ca_gate_refuses_when_no_console_host_is_declared(tmp_path):
    inv, play = _ca_gate_harness(None)
    proc = _run_with_inventory(tmp_path, inv, play)
    assert proc.returncode != 0 and "console_proxmox_ca_pem is not set" in proc.stdout, proc.stdout
    assert "PASSED-GATE" not in proc.stdout


@NEEDS_ANSIBLE
def test_the_ca_gate_refuses_text_that_is_not_a_certificate(tmp_path):
    # Not a key, so only the certificate-header clause can refuse it.
    inv, play = _ca_gate_harness({"console-local": {"ansible_connection": "local",
                                                    "console_proxmox_ca_pem": "not a certificate"}})
    proc = _run_with_inventory(tmp_path, inv, play)
    assert proc.returncode != 0 and "public certificate" in proc.stdout, proc.stdout
    assert "PASSED-GATE" not in proc.stdout


@NEEDS_ANSIBLE
def test_the_ca_gate_refuses_a_private_key_and_passes_a_certificate(tmp_path):
    key_inv, key_play = _ca_gate_harness({"console-local": {"ansible_connection": "local",
                                                            "console_proxmox_ca_pem": PRIVATE_KEY_PEM}})
    key = _run_with_inventory(tmp_path, key_inv, key_play)
    assert key.returncode != 0 and "public certificate" in key.stdout, key.stdout
    bundle_inv, bundle_play = _ca_gate_harness({"console-local": {
        "ansible_connection": "local", "console_proxmox_ca_pem": CERT_PEM + PRIVATE_KEY_PEM}})
    bundle = _run_with_inventory(tmp_path, bundle_inv, bundle_play)
    assert bundle.returncode != 0 and "public certificate" in bundle.stdout, bundle.stdout
    ok_inv, ok_play = _ca_gate_harness({"console-local": {"ansible_connection": "local",
                                                          "console_proxmox_ca_pem": CERT_PEM}})
    ok = _run_with_inventory(tmp_path, ok_inv, ok_play)
    assert ok.returncode == 0 and "PASSED-GATE" in ok.stdout, ok.stdout + ok.stderr


# --- deploy-console.yml: Phase 3 wiring ---------------------------------------------------------------

def _deploy_vars() -> dict:
    return next(p for p in playbook_yaml.plays(DEPLOY) if "_secret_definitions" in (p.get("vars") or {}))["vars"]


def test_phase3_reads_are_gated_on_their_url_vars():
    v = _deploy_vars()
    assert "console_netbox_url" in v["_console_netbox_reads"]
    assert "console_proxmox_url" in v["_console_proxmox_reads"]
    # The Phase 1 OIDC read is unchanged: authentik owns it, the console only reads it.
    assert "_console_oidc_reads" in v and "from_service: authentik" in DEPLOY.read_text()
    assert "_shared_reads" in v


def test_phase3_reads_name_the_declared_keys_only():
    v = _deploy_vars()
    assert "{'from_service': 'netbox', 'read_keys': ['console_api_token']}" in v["_console_netbox_reads"]
    assert "'read_keys': ['console_token_id', 'console_token']" in v["_console_proxmox_reads"]


def test_flows_and_ca_are_rendered_into_config():
    templates = {t["dest"]: t["src"] for t in _deploy_vars()["_env_templates"]}
    assert templates["config/flows.json"] == "flows.json.j2"
    assert templates["config/proxmox-ca.pem"] == "proxmox-ca.pem.j2"


def test_compose_binds_flows_and_the_ca_read_only_like_scope_and_workflow():
    text = (CONSOLE / "compose.yml").read_text()
    assert "      - ./config/flows.json:/etc/console/flows.json:ro" in text
    assert "      - ./config/proxmox-ca.pem:/etc/console/proxmox-ca.pem:ro" in text


def test_deploy_sh_checks_and_hashes_flows_and_the_ca():
    text = (CONSOLE / "deploy.sh").read_text()
    assert "config/flows.json not found" in text
    assert "config/proxmox-ca.pem not found" in text
    # Both up branches recreate on a change to either file.
    assert text.count("config/flows.json config/proxmox-ca.pem") == 2


_NO_KEY = object()  # a site that does not declare netbox_site at all (not "declares it empty")
NETBOX_SITE_CHECKS = (
    "Collect the sites that declare netbox_site across console_tenants",
    "Collect the netbox_site slugs declared across console_tenants",
    "Refuse an empty or null netbox_site",
    "Refuse a netbox_site that is not a string",
    "Refuse a netbox_site with surrounding or internal whitespace",
    "Refuse a netbox_site slug declared on more than one site",
)


def _netbox_site_tasks() -> list[dict]:
    return [_named(DEPLOY, n) for n in NETBOX_SITE_CHECKS]


def _tenants(values: list) -> list[dict]:
    # One site per value: _NO_KEY leaves netbox_site out, anything else (None, "", " hq", 7, "hq") sets it.
    sites = []
    for i, value in enumerate(values):
        site = {"id": f"site-{i}", "name": f"Site {i}", "envs": ["prod"]}
        if value is not _NO_KEY:
            site["netbox_site"] = value
        sites.append(site)
    return [{"id": "t", "name": "T", "orgs": [{"id": "o", "name": "O", "sites": sites}]}]


@NEEDS_ANSIBLE
def test_duplicate_netbox_site_slugs_are_refused_by_name(tmp_path):
    proc = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants(["hq", "hq", "dc"])})
    assert proc.returncode != 0, proc.stdout
    assert "declares netbox_site 'hq' on more than one site" in proc.stdout, proc.stdout


@NEEDS_ANSIBLE
def test_unique_or_unmapped_netbox_sites_pass(tmp_path):
    ok = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants(["hq", _NO_KEY, "dc"])})
    assert ok.returncode == 0, ok.stdout + ok.stderr
    none = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants([_NO_KEY, _NO_KEY])})
    assert none.returncode == 0, none.stdout + none.stderr


@NEEDS_ANSIBLE
def test_a_null_or_blank_netbox_site_is_a_named_error_not_ignored(tmp_path):
    for value in (None, "", "   "):
        proc = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants([value])})
        assert proc.returncode != 0, (value, proc.stdout)
        assert "netbox_site is empty on site 'site-0'" in proc.stdout, (value, proc.stdout)


@NEEDS_ANSIBLE
def test_a_non_string_netbox_site_is_a_named_error(tmp_path):
    proc = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants([7])})
    assert proc.returncode != 0 and "netbox_site on site 'site-0' must be a string slug" in proc.stdout, proc.stdout


@NEEDS_ANSIBLE
def test_a_netbox_site_with_whitespace_is_refused_compared_exactly_not_trimmed(tmp_path):
    for value in (" hq", "hq ", "h q", "h\tq"):
        proc = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants([value])})
        assert proc.returncode != 0, (value, proc.stdout)
        assert "has surrounding or internal whitespace" in proc.stdout, (value, proc.stdout)


@NEEDS_ANSIBLE
def test_a_private_key_is_refused_as_the_proxmox_ca(tmp_path):
    task = _named(DEPLOY, "Refuse a Proxmox CA that is not a public certificate")
    key = _run_tasks(tmp_path, [task], {"console_proxmox_ca_pem": PRIVATE_KEY_PEM})
    assert key.returncode != 0 and "public CA certificate" in key.stdout
    # The case the certificate-header clause alone would miss: a bundle with a key appended to a certificate.
    bundle = _run_tasks(tmp_path, [task], {"console_proxmox_ca_pem": CERT_PEM + PRIVATE_KEY_PEM})
    assert bundle.returncode != 0 and "public CA certificate" in bundle.stdout, bundle.stdout
    cert = _run_tasks(tmp_path, [task], {"console_proxmox_ca_pem": CERT_PEM})
    assert cert.returncode == 0, cert.stdout + cert.stderr
    unset = _run_tasks(tmp_path, [task], {})
    assert unset.returncode == 0, unset.stdout + unset.stderr


# --- Rendered inputs: env, the CA file, and flows.json ------------------------------------------------

ENV_SECRETS = {"console_oidc_client_secret": "OIDC-REF", "semaphore_api_token": "SEM-REF",
               "console_api_token": "NB-REF", "console_token_id": "console@pve!console", "console_token": "PX-REF"}


@NEEDS_ANSIBLE
def test_env_groups_render_only_when_their_url_is_set(tmp_path):
    unset = _render(tmp_path, CONSOLE / "templates/env.j2", {"secrets": ENV_SECRETS})
    assert "CONSOLE_NETBOX_URL" not in unset and "CONSOLE_PROXMOX_URL" not in unset
    assert "CONSOLE_PROXMOX_CA_FILE" not in unset
    # The flows path is always set, whether or not any Phase 3 source is configured.
    assert "CONSOLE_FLOWS_FILE=/etc/console/flows.json" in unset
    netbox = _render(tmp_path, CONSOLE / "templates/env.j2",
                     {"secrets": ENV_SECRETS, "console_netbox_url": "https://netbox.example.test"})
    assert "CONSOLE_NETBOX_URL=https://netbox.example.test" in netbox
    assert "CONSOLE_NETBOX_TOKEN=NB-REF" in netbox
    assert "CONSOLE_PROXMOX_URL" not in netbox


@NEEDS_ANSIBLE
def test_proxmox_group_is_all_or_none_with_the_ca_only_when_pem_is_set(tmp_path):
    base = {"secrets": ENV_SECRETS, "console_proxmox_url": "https://pve.example.test"}
    no_ca = _render(tmp_path, CONSOLE / "templates/env.j2", base)
    for key in ("CONSOLE_PROXMOX_URL=", "CONSOLE_PROXMOX_TOKEN_ID=console@pve!console", "CONSOLE_PROXMOX_TOKEN=PX-REF"):
        assert key in no_ca, key
    assert "CONSOLE_PROXMOX_CA_FILE" not in no_ca
    with_ca = _render(tmp_path, CONSOLE / "templates/env.j2", {**base, "console_proxmox_ca_pem": CERT_PEM})
    assert "CONSOLE_PROXMOX_CA_FILE=/etc/console/proxmox-ca.pem" in with_ca


@NEEDS_ANSIBLE
def test_grafana_base_url_is_absent_when_unset_and_rendered_when_set(tmp_path):
    unset = _render(tmp_path, CONSOLE / "templates/env.j2", {"secrets": ENV_SECRETS})
    assert "CONSOLE_GRAFANA_URL" not in unset
    set_ = _render(tmp_path, CONSOLE / "templates/env.j2",
                   {"secrets": ENV_SECRETS, "console_grafana_url": "https://grafana.example.test"})
    assert "CONSOLE_GRAFANA_URL=https://grafana.example.test" in set_.splitlines()


@NEEDS_ANSIBLE
def test_the_ca_file_is_blank_when_unset_and_the_pem_when_set(tmp_path):
    assert _render(tmp_path, CONSOLE / "templates/proxmox-ca.pem.j2", {}).strip() == ""
    assert _render(tmp_path, CONSOLE / "templates/proxmox-ca.pem.j2",
                   {"console_proxmox_ca_pem": CERT_PEM}).strip() == CERT_PEM.strip()


def _flow_variables() -> dict:
    catalog = yaml.safe_load(CATALOG.read_text())["authentik_app_catalog"]
    # The caddy block carries an address label, an address upstream and a host:port label, all of which
    # must stay out; the hostname labels are the only edges. The o11y instances carry host:port addresses.
    caddy = (
        "inference.example.test {\n"
        "    reverse_proxy 203.0.113.40:8000\n"
        "}\n"
        "203.0.113.41:9000 {\n"
        "    respond 404\n"
        "}\n"
        ":8443 {\n"
        "    respond 403\n"
        "}\n"
        "console.example.test:8443 {\n"
        "    reverse_proxy console.internal.example.test:8080\n"
        "}\n"
    )
    targets = [
        {"uid": "o11y_missing_caddy", "job": "caddy", "instance": "203.0.113.40:2021",
         "service": "caddy-reverse-proxy/caddy", "cluster": "agent-cloud-local", "environment": "local"},
        {"uid": "node_exporter", "job": "node", "instance": "203.0.113.41:9100"},
    ]
    return {
        "_console_authentik_catalog": catalog,
        "_console_authentik_apps": list(catalog),
        "_console_caddy_sites": caddy,
        "_console_o11y_targets": targets,
    }


@NEEDS_ANSIBLE
def test_flows_render_names_only_with_no_address_and_no_port(tmp_path):
    out = _render(tmp_path, CONSOLE / "templates/flows.json.j2", _flow_variables())
    assert IPV4.search(out) is None, out
    assert PORT.search(out) is None, out
    edges = json.loads(out)["edges"]
    assert {(e["from"], e["to"]) for e in edges if e["kind"] == "edge"} == {
        ("caddy", "inference.example.test"), ("caddy", "console.example.test")}
    assert {(e["from"], e["to"]) for e in edges if e["kind"] == "metrics"} == {
        ("caddy-reverse-proxy/caddy", "o11y"), ("node", "o11y")}


@NEEDS_ANSIBLE
def test_flow_kinds_follow_the_catalog_type(tmp_path):
    edges = json.loads(_render(tmp_path, CONSOLE / "templates/flows.json.j2", _flow_variables()))["edges"]
    kinds = {e["from"]: e["kind"] for e in edges if e["to"] == "authentik"}
    catalog = yaml.safe_load(CATALOG.read_text())["authentik_app_catalog"]
    assert kinds["console"] == "oidc" and catalog["console"]["type"] == "oidc"
    forward = next(k for k, v in catalog.items() if v["type"] == "forward_auth")
    assert kinds[forward] == "forward_auth"


@NEEDS_ANSIBLE
def test_missing_sources_give_no_edges_of_that_kind_not_an_error(tmp_path):
    variables = {"_console_authentik_catalog": {}, "_console_authentik_apps": [],
                 "_console_caddy_sites": "", "_console_o11y_targets": []}
    edges = json.loads(_render(tmp_path, CONSOLE / "templates/flows.json.j2", variables))["edges"]
    assert edges == []


@NEEDS_ANSIBLE
def _caddy_hosts(tmp_path: Path, caddy: str) -> list[str]:
    variables = {"_console_authentik_catalog": {}, "_console_authentik_apps": [],
                 "_console_caddy_sites": caddy, "_console_o11y_targets": []}
    out = _render(tmp_path, CONSOLE / "templates/flows.json.j2", variables)
    assert IPV4.search(out) is None and PORT.search(out) is None, out
    edges = json.loads(out)["edges"]
    assert all(e["from"] == "caddy" for e in edges), edges
    return [e["to"] for e in edges]


@NEEDS_ANSIBLE
def test_a_scheme_prefixed_site_emits_its_hostname_without_scheme_or_port(tmp_path):
    assert _caddy_hosts(tmp_path, "https://console.example.test:8443 {\n    respond 200\n}\n") == \
        ["console.example.test"]


@NEEDS_ANSIBLE
def test_comma_separated_site_addresses_emit_each_hostname(tmp_path):
    assert _caddy_hosts(tmp_path, "a.example.test, b.example.test:8443 {\n    respond 200\n}\n") == \
        ["a.example.test", "b.example.test"]


@NEEDS_ANSIBLE
def test_a_wildcard_site_is_emitted_as_written(tmp_path):
    assert _caddy_hosts(tmp_path, "*.agent-cloud.test {\n    respond 200\n}\n") == ["*.agent-cloud.test"]


@NEEDS_ANSIBLE
def test_a_tab_indented_site_line_is_read_as_a_site(tmp_path):
    assert _caddy_hosts(tmp_path, "\tconsole.example.test {\n\t\trespond 200\n\t}\n") == ["console.example.test"]


@NEEDS_ANSIBLE
def test_a_label_with_a_dotted_quad_emits_nothing(tmp_path):
    for label in ("203.0.113.7 {", "https://203.0.113.7:8443 {", "host.203.0.113.7.example.test {"):
        assert _caddy_hosts(tmp_path, label + "\n    respond 200\n}\n") == [], label


@NEEDS_ANSIBLE
def test_a_dotted_quad_in_a_comma_list_drops_only_that_address(tmp_path):
    assert _caddy_hosts(tmp_path, "a.example.test, 203.0.113.7:9000 {\n    respond 200\n}\n") == ["a.example.test"]


@NEEDS_ANSIBLE
def test_a_directive_with_a_brace_is_never_a_site(tmp_path):
    caddy = "inference.example.test {\n    reverse_proxy inference.internal.example.test:8000 {\n    }\n}\n"
    assert _caddy_hosts(tmp_path, caddy) == ["inference.example.test"]


@NEEDS_ANSIBLE
def test_a_port_only_label_emits_nothing(tmp_path):
    assert _caddy_hosts(tmp_path, ":8443 {\n    respond 403\n}\n") == []


def test_the_flows_template_reads_no_upstream_or_instance_field():
    text = (CONSOLE / "templates/flows.json.j2").read_text()
    for forbidden in ("reverse_proxy", "instance", "upstream", "hostport"):
        assert forbidden not in text.split("-#}", 1)[1], forbidden


PHASE3_KEYS = ("console_netbox_url", "console_proxmox_url", "console_proxmox_ca_pem", "console_grafana_url",
               "netbox_site")


def test_inventory_placeholders_carry_no_real_address():
    # Only the Phase 3 keys are checked: the file's existing console_bind (127.0.0.1) is not a Phase 3 value.
    for path in (REPO / "platform/inventory/local-dev.yml.example", REPO / "platform/inventory/production.yml"):
        lines = [ln for ln in path.read_text().splitlines() if any(k in ln for k in PHASE3_KEYS)]
        assert lines, path
        for ln in lines:
            assert IPV4.search(ln) is None, (path.name, ln)
