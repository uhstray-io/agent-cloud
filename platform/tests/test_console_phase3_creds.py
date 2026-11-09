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
import tempfile
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


def test_proxmox_play_is_local_and_imports_the_extra_var_guard_first():
    # Static: reads the play's first import. The real refusal runs in
    # test_executor_internal_overrides.py::test_every_launchable_playbook_refuses_a_forged_internal_name.
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


def test_the_proxmox_template_declares_both_replace_and_recreate_flags_default_false():
    tpl = next(t for t in yaml.safe_load(TEMPLATES.read_text())["templates"]
               if t["name"] == "Provision Console Proxmox Token")
    fields = {s["name"]: s for s in tpl["survey_vars"]}
    assert set(fields) == {"console_proxmox_token_recreate", "console_proxmox_token_replace"}, sorted(fields)
    for name, field in fields.items():
        assert field["type"] == "string" and field["required"] is False and field["default_value"] == "false", name
    # The playbook reads both names; a survey var it never reads would be a flag that does nothing.
    text = PROXMOX.read_text()
    assert "console_proxmox_token_replace" in text and "console_proxmox_token_recreate" in text


def test_the_proof_is_skipped_on_a_check_mode_mint_and_never_proves_a_replaced_secret():
    proof = _proxmox_task("Prove the stored console token authenticates (read only)")
    assert "not (_mint and ansible_check_mode)" in proof["when"], proof["when"]
    choose = _proxmox_task("Choose the stored console token to prove")["ansible.builtin.set_fact"][
        "_console_proof_secret"]
    assert "(_console_stored and not _mint)" in choose, choose


def _run_check(tmp_path: Path, playbook: dict) -> subprocess.CompletedProcess:
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump([playbook]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(["ansible-playbook", "--check", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False)


@NEEDS_ANSIBLE
def test_a_check_mode_mint_proves_nothing_even_when_a_dead_stored_secret_is_present(tmp_path):
    # stored_absent with recreate under --check: the store still holds the dead secret, but the mint has not run.
    names = ("Choose the stored console token to prove", "Prove the stored console token authenticates (read only)")
    tasks = [_proxmox_task(n) for n in names]
    tasks.append({"ansible.builtin.debug": {
        "msg": "OUT|{{ _console_proof_secret | length }}|{{ _console_perms is skipped }}"}})
    variables = {"_mint": True, "_console_stored": True,
                 "_pve_bao_read": {"status": 200, "json": {"data": {"data": {"console_token": "DEAD-SECRET"}}}},
                 "_pve_url": "https://pve.invalid:8006", "_console_userid": "console@pve",
                 "_console_tokenid": "console", "_ca_dir": {"path": str(tmp_path)}}
    proc = _run_check(tmp_path, {"hosts": "localhost", "connection": "local", "gather_facts": False,
                                 "vars": variables, "tasks": tasks})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OUT|0|True" in proc.stdout, proc.stdout
    assert "DEAD-SECRET" not in proc.stdout


def test_the_claude_md_describes_the_replace_flag():
    text = (REPO / "platform/services/console/deployment/CLAUDE.md").read_text()
    assert "console_proxmox_token_replace=true" in text
    assert "console_proxmox_token_recreate=true" in text


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
    assert templates["config/gateway.json"] == "gateway.json.j2"


def test_compose_binds_flows_and_the_ca_read_only_like_scope_and_workflow():
    text = (CONSOLE / "compose.yml").read_text()
    assert "      - ./config/flows.json:/etc/console/flows.json:ro" in text
    assert "      - ./config/proxmox-ca.pem:/etc/console/proxmox-ca.pem:ro" in text
    assert "      - ./config/gateway.json:/etc/console/gateway.json:ro" in text


def test_deploy_sh_checks_and_hashes_flows_and_the_ca():
    text = (CONSOLE / "deploy.sh").read_text()
    assert "config/flows.json not found" in text
    assert "config/proxmox-ca.pem not found" in text
    assert "config/gateway.json not found" in text
    # Both up branches recreate on a change to any of the four files (task 8.3a adds gateway.json last).
    assert text.count("config/flows.json config/proxmox-ca.pem config/gateway.json") == 2
    # Each compose_up_if_changed line passes the file exactly once: the digest covers it on both branches.
    up_lines = [ln for ln in text.splitlines() if "compose_up_if_changed config/" in ln]
    assert [ln.count("config/gateway.json") for ln in up_lines] == [1, 1]


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
               "console_api_token": "NB-REF", "console_token_id": "console@pve!console", "console_token": "PX-REF",
               # Shared-read with console_grafana_api_enabled and console_grafana_url (D26): env.j2 renders it
               # only behind both.
               "console_grafana_token": "GRAFANA-REF"}


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
    assert "CONSOLE_GRAFANA_URL" not in unset and "CONSOLE_GRAFANA_TOKEN" not in unset
    # The URL alone is the deep-link base, as on dev: it renders no token.
    set_ = _render(tmp_path, CONSOLE / "templates/env.j2",
                   {"secrets": ENV_SECRETS, "console_grafana_url": "https://grafana.example.test"})
    assert "CONSOLE_GRAFANA_URL=https://grafana.example.test" in set_.splitlines()
    assert "CONSOLE_GRAFANA_TOKEN" not in set_


@NEEDS_ANSIBLE
def test_grafana_token_renders_only_with_the_api_switch_and_its_url(tmp_path):
    switch_only = _render(tmp_path, CONSOLE / "templates/env.j2",
                          {"secrets": ENV_SECRETS, "console_grafana_api_enabled": True})
    assert "CONSOLE_GRAFANA_TOKEN" not in switch_only and "CONSOLE_GRAFANA_URL" not in switch_only
    both = _render(tmp_path, CONSOLE / "templates/env.j2",
                   {"secrets": ENV_SECRETS, "console_grafana_url": "https://grafana.example.test",
                    "console_grafana_api_enabled": True})
    assert "CONSOLE_GRAFANA_URL=https://grafana.example.test" in both.splitlines()
    assert "CONSOLE_GRAFANA_TOKEN=GRAFANA-REF" in both.splitlines()


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
    edges = json.loads(out)
    assert {(e["from"], e["to"]) for e in edges if e["kind"] == "edge"} == {
        ("caddy", "inference.example.test"), ("caddy", "console.example.test")}
    assert {(e["from"], e["to"]) for e in edges if e["kind"] == "metrics"} == {
        ("caddy-reverse-proxy/caddy", "o11y"), ("node", "o11y")}


@NEEDS_ANSIBLE
def test_flow_kinds_follow_the_catalog_type(tmp_path):
    edges = json.loads(_render(tmp_path, CONSOLE / "templates/flows.json.j2", _flow_variables()))
    kinds = {e["from"]: e["kind"] for e in edges if e["to"] == "authentik"}
    catalog = yaml.safe_load(CATALOG.read_text())["authentik_app_catalog"]
    assert kinds["console"] == "oidc" and catalog["console"]["type"] == "oidc"
    forward = next(k for k, v in catalog.items() if v["type"] == "forward_auth")
    assert kinds[forward] == "forward_auth"


@NEEDS_ANSIBLE
def test_missing_sources_give_no_edges_of_that_kind_not_an_error(tmp_path):
    variables = {"_console_authentik_catalog": {}, "_console_authentik_apps": [],
                 "_console_caddy_sites": "", "_console_o11y_targets": []}
    edges = json.loads(_render(tmp_path, CONSOLE / "templates/flows.json.j2", variables))
    assert edges == []


@NEEDS_ANSIBLE
def _caddy_hosts(tmp_path: Path, caddy: str) -> list[str]:
    variables = {"_console_authentik_catalog": {}, "_console_authentik_apps": [],
                 "_console_caddy_sites": caddy, "_console_o11y_targets": []}
    out = _render(tmp_path, CONSOLE / "templates/flows.json.j2", variables)
    assert IPV4.search(out) is None and PORT.search(out) is None, out
    edges = json.loads(out)
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


def _flow_struct_fields() -> list[str]:
    """The field names of the server's Flow type, read from its source (app/src/server/inventory.rs)."""
    src = (CONSOLE.parent / "app/src/server/inventory.rs").read_text()
    body = re.search(r"pub struct Flow \{(.*?)\n\}", src, re.S)
    assert body, "Flow struct not found in inventory.rs"
    return re.findall(r"pub (\w+):", body.group(1))


@NEEDS_ANSIBLE
def test_rendered_flows_match_the_servers_flow_type(tmp_path):
    # Production deploy 2026-10-30 (Semaphore task 10457) crash-looped the console: this template rendered
    # {"edges": [...]} without labels, the server parses a bare Vec<Flow> with deny_unknown_fields. Each side's
    # own tests passed; this test checks one against the other.
    src = (CONSOLE.parent / "app/src/server/inventory.rs").read_text()
    assert "#[serde(deny_unknown_fields)]\npub struct Flow {" in src
    assert "serde_json::from_str(&text)" in src and "Result<Vec<Flow>, String>" in src
    fields = _flow_struct_fields()
    assert fields == ["kind", "from", "to", "label"], fields
    flows = json.loads(_render(tmp_path, CONSOLE / "templates/flows.json.j2", _flow_variables()))
    assert isinstance(flows, list) and flows, flows
    for flow in flows:
        assert sorted(flow) == sorted(fields), flow
        assert all(isinstance(flow[k], str) and flow[k] for k in fields), flow


# --- gateway.json (task 8.3a): the agentgateway clients, budgets and models ------------------------------------
# The file is rendered through the deploy's own _console_gw_* expressions, against an inventory that gives the
# agentgateway host vars. The gateway's own admin derivation (the secret-declarations file, evaluated on that same
# host) runs beside it, so a test can compare the console's view with the gateway's on identical inputs.

AGW_SECRET_DECL = REPO / "platform/playbooks/vars/secret-declarations/agentgateway.yml"
GW_SENTINEL = "SENTINEL-DO-NOT-LEAK"
GW_MODELS = [{"name": "m1", "upstream_model": "vendor/one"}, {"name": "m2", "upstream_model": "vendor/two"}]
GW_ADMIN = {
    "agw_admin_identities_enabled": True,
    "platform_admins": [
        {"key": "ops", "username": "ops-user", "name": "Ops", "email": "ops@agent-cloud.test"},
        {"key": "svc", "username": "svc-automation", "agw_identity": "svc-ident", "type": "service_account"},
    ],
}


def _gw_play_vars() -> dict:
    play = next(p for p in playbook_yaml.plays(DEPLOY) if "_secret_definitions" in (p.get("vars") or {}))
    return {k: v for k, v in play["vars"].items() if k.startswith("_console_gw_")}


def _gw_render(tmp_path: Path, gateway: dict | None) -> tuple[str, list[str]]:
    """The rendered gateway.json text, and the admin identity names the gateway derives on the same host.

    `gateway` is the agentgateway host's vars, or None for an inventory with no agentgateway_svc host.
    """
    case = Path(tempfile.mkdtemp(dir=tmp_path))
    inventory = {"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}}
    plays = []
    admins_out = case / "admins.json"
    if gateway is not None:
        inventory["all"]["children"] = {"agentgateway_svc": {"hosts": {
            "gw-test": {"ansible_connection": "local", **gateway}}}}
        plays.append({"name": "the gateway's own admin derivation", "hosts": "gw-test", "gather_facts": False,
                      "tasks": [{"ansible.builtin.include_vars": {"file": str(AGW_SECRET_DECL)}},
                                {"ansible.builtin.copy": {"dest": str(admins_out),
                                                          "content": "{{ _agw_admin_identities | to_json }}"}}]})
    out = case / "gateway.json"
    plays.append({"name": "the console's gateway file", "hosts": "localhost", "connection": "local",
                  "gather_facts": False, "vars": _gw_play_vars(),
                  "tasks": [{"ansible.builtin.template": {"src": str(CONSOLE / "templates/gateway.json.j2"),
                                                          "dest": str(out), "mode": "0644"}}]})
    inv = case / "inventory.yml"
    inv.write_text(yaml.safe_dump(inventory))
    path = case / "plays.yml"
    path.write_text(yaml.safe_dump(plays))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(["ansible-playbook", "-i", str(inv), str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    admins = [a["name"] for a in json.loads(admins_out.read_text())] if gateway is not None else []
    return out.read_text(), admins


def _gw_body(tmp_path: Path, gateway: dict | None) -> dict:
    text, _ = _gw_render(tmp_path, gateway)
    return json.loads(text)


def _gw_client(body: dict, name: str) -> dict:
    return next(c for c in body["clients"] if c["name"] == name)


@NEEDS_ANSIBLE
def test_gateway_file_is_empty_lists_with_no_agentgateway_host(tmp_path):
    text, admins = _gw_render(tmp_path, None)
    assert json.loads(text) == {"clients": [], "models": []}
    assert admins == []


@NEEDS_ANSIBLE
def test_client_figure_is_its_own_policy_before_its_tier(tmp_path):
    gw = {"agw_clients": ["alpha"], "agw_models": GW_MODELS, "agw_rate_tokens_per_hour": 123,
          "agw_tier_tokens_per_hour": {"developer": 300000},
          "agw_client_policies": {"alpha": {"tokens_per_hour": 5000, "tier": "developer", "allowed_models": ["m1"]}}}
    body = _gw_body(tmp_path, gw)
    assert body["clients"] == [{"name": "alpha", "tier": "developer", "tokens_per_hour": 5000,
                                "budget_source": "client", "allowed_models": ["m1"]}]
    assert body["models"] == ["m1", "m2"]


@NEEDS_ANSIBLE
def test_client_figure_falls_back_to_its_tier(tmp_path):
    gw = {"agw_clients": ["alpha"], "agw_models": GW_MODELS, "agw_rate_tokens_per_hour": 123,
          "agw_tier_tokens_per_hour": {"developer": 300000},
          "agw_client_policies": {"alpha": {"tier": "developer"}}}
    alpha = _gw_client(_gw_body(tmp_path, gw), "alpha")
    assert (alpha["tokens_per_hour"], alpha["budget_source"], alpha["tier"]) == (300000, "tier", "developer")


@NEEDS_ANSIBLE
def test_client_figure_falls_back_to_the_default_rate_and_an_unknown_tier_falls_through(tmp_path):
    gw = {"agw_clients": ["alpha", "beta"], "agw_models": GW_MODELS, "agw_rate_tokens_per_hour": 123456,
          "agw_tier_tokens_per_hour": {"developer": 300000},
          "agw_client_policies": {"beta": {"tier": "ghost"}}}
    body = _gw_body(tmp_path, gw)
    alpha = _gw_client(body, "alpha")
    assert (alpha["tokens_per_hour"], alpha["budget_source"], alpha["tier"]) == (123456, "default", None)
    beta = _gw_client(body, "beta")
    assert (beta["tokens_per_hour"], beta["budget_source"], beta["tier"]) == (123456, "default", "ghost")


@NEEDS_ANSIBLE
def test_default_figure_is_2000000_when_the_rate_is_unset(tmp_path):
    alpha = _gw_client(_gw_body(tmp_path, {"agw_clients": ["alpha"], "agw_models": GW_MODELS}), "alpha")
    assert (alpha["tokens_per_hour"], alpha["budget_source"]) == (2000000, "default")


@NEEDS_ANSIBLE
def test_an_admin_overlap_takes_the_admin_tier_and_its_figure(tmp_path):
    gw = {**GW_ADMIN, "agw_clients": ["alpha", "ops-user", "svc-ident", "svc-automation"], "agw_models": GW_MODELS,
          "agw_tier_tokens_per_hour": {"admin": 900000, "developer": 300000},
          "agw_client_policies": {"svc-ident": {"tokens_per_hour": 42}}}
    text, admins = _gw_render(tmp_path, gw)
    body = json.loads(text)
    # The gateway derives the admin names from agw_identity, else username.
    assert admins == ["ops-user", "svc-ident"]
    ops = _gw_client(body, "ops-user")
    assert (ops["tier"], ops["tokens_per_hour"], ops["budget_source"]) == ("admin", 900000, "tier")
    svc = _gw_client(body, "svc-ident")
    assert (svc["tier"], svc["tokens_per_hour"], svc["budget_source"]) == ("admin", 42, "client")
    # A username that is not the derived name is a plain client, never tier admin.
    plain = _gw_client(body, "svc-automation")
    assert (plain["tier"], plain["budget_source"]) == (None, "default")
    assert _gw_client(body, "alpha")["tier"] is None


@NEEDS_ANSIBLE
def test_an_admin_overlap_with_no_admin_figure_falls_through_like_the_gateway(tmp_path):
    gw = {**GW_ADMIN, "agw_clients": ["ops-user"], "agw_models": GW_MODELS,
          "agw_tier_tokens_per_hour": {"developer": 300000}, "agw_rate_tokens_per_hour": 77}
    ops = _gw_client(_gw_body(tmp_path, gw), "ops-user")
    assert (ops["tier"], ops["tokens_per_hour"], ops["budget_source"]) == ("admin", 77, "default")


@NEEDS_ANSIBLE
def test_the_admin_tier_needs_the_gate_and_the_gateway_derives_no_admin_without_it(tmp_path):
    gw = {**GW_ADMIN, "agw_admin_identities_enabled": False, "agw_clients": ["ops-user"], "agw_models": GW_MODELS,
          "agw_tier_tokens_per_hour": {"admin": 900000}}
    text, admins = _gw_render(tmp_path, gw)
    assert admins == []
    ops = _gw_client(json.loads(text), "ops-user")
    assert (ops["tier"], ops["tokens_per_hour"], ops["budget_source"]) == (None, 2000000, "default")


@NEEDS_ANSIBLE
def test_internal_identities_are_not_listed(tmp_path):
    # mcp-self, legacy-shared and the pool identity are internal (config.yaml.j2 _internal_ids); only alpha is a client.
    gw = {"agw_clients": ["mcp-self", "legacy-shared", "pool-id", "alpha"],
          "agw_models": [{"name": "m1", "upstream_model": "vendor/one", "pool_identity": "pool-id"},
                         {"name": "m2", "upstream_model": "vendor/two"}],
          "agw_client_policies": {"pool-id": {"tokens_per_hour": 9000}}}
    text = _gw_render(tmp_path, gw)[0]
    body = json.loads(text)
    assert [c["name"] for c in body["clients"]] == ["alpha"]
    assert body["clients"][0]["allowed_models"] == ["m1", "m2"]
    assert "pool/" not in text and "mcp-self" not in text and "legacy-shared" not in text and "pool-id" not in text


@NEEDS_ANSIBLE
def test_allowed_models_follow_openrouter_and_pool_rules_and_an_absent_key_means_every_model(tmp_path):
    gw = {"agw_clients": ["alpha", "beta", "gamma", "or-user", "pool-id"],
          "agw_models": [{"name": "m1", "upstream_model": "vendor/one"},
                         {"name": "or1", "provider": "openrouter", "upstream_model": "vendor/x"},
                         {"name": "pooled", "upstream_model": "vendor/p", "pool_identity": "pool-id"}],
          "agw_openrouter_identities": ["or-user"],
          "agw_client_policies": {"alpha": {"allowed_models": ["or1", "m1", "pool/pooled"]},
                                  "beta": {"allowed_models": []}, "or-user": {}}}
    body = _gw_body(tmp_path, gw)
    assert _gw_client(body, "alpha")["allowed_models"] == ["m1"]      # OpenRouter removed, pool/ removed
    assert _gw_client(body, "beta")["allowed_models"] == []           # explicit empty is empty
    assert _gw_client(body, "gamma")["allowed_models"] == ["m1", "pooled"]  # absent: every model minus OpenRouter
    assert _gw_client(body, "or-user")["allowed_models"] == ["m1", "or1", "pooled"]  # an OpenRouter identity keeps them
    assert body["models"] == ["m1", "or1", "pooled"]


@NEEDS_ANSIBLE
def test_openrouter_admins_extends_the_openrouter_identities_to_the_admin_overlap(tmp_path):
    base = {**GW_ADMIN, "agw_clients": ["ops-user"], "agw_models": [
        {"name": "m1", "upstream_model": "vendor/one"},
        {"name": "or1", "provider": "openrouter", "upstream_model": "vendor/x"}]}
    assert _gw_client(_gw_body(tmp_path, {**base, "agw_openrouter_admins": True}), "ops-user")[
        "allowed_models"] == ["m1", "or1"]
    assert _gw_client(_gw_body(tmp_path, {**base, "agw_openrouter_admins": False}), "ops-user")[
        "allowed_models"] == ["m1"]


@NEEDS_ANSIBLE
def test_no_key_client_field_address_or_port_reaches_the_file(tmp_path):
    gw = {"agw_clients": ["alpha"], "agw_models": GW_MODELS,
          "agw_plaintext_keys": True, "client_alpha": GW_SENTINEL, "vllm_api_key": GW_SENTINEL,
          "agw_upstream_base_url": "http://198.51.100.9:8000/v1", "agw_loopback_port": 4002,
          "agw_client_policies": {"alpha": {"tokens_per_hour": 5000}}}
    text = _gw_render(tmp_path, gw)[0]
    assert GW_SENTINEL not in text and "198.51.100" not in text and "4002" not in text
    assert IPV4.search(text) is None, text
    body = json.loads(text)
    assert set(body) == {"clients", "models"}
    assert set(body["clients"][0]) == {"name", "tier", "tokens_per_hour", "budget_source", "allowed_models"}


def test_the_gateway_play_vars_read_no_key_field_or_secret():
    for name, expr in _gw_play_vars().items():
        text = str(expr)
        assert re.search(r"(?<![A-Za-z_])client_", text) is None, name
        assert "vllm_api_key" not in text and "secrets" not in text and "agw_plaintext_keys" not in text, name


def _gw_struct(name: str) -> str:
    src = (CONSOLE.parent / "app/src/server/gateway.rs").read_text()
    body = re.search(rf"pub struct {name} \{{(.*?)\n\}}", src, re.S)
    assert body, f"{name} not found in gateway.rs"
    return body.group(1)


@NEEDS_ANSIBLE
def test_rendered_gateway_json_matches_the_servers_structs(tmp_path):
    # Same contract check as the flows file: a field the server does not declare, or a budget_source word the
    # server's enum does not name, would fail the console's startup. The server refuses unknown fields.
    src = (CONSOLE.parent / "app/src/server/gateway.rs").read_text()
    assert src.count("#[serde(deny_unknown_fields)]") >= 2
    file_fields = re.findall(r"pub (\w+):", _gw_struct("GatewayFile"))
    client_fields = re.findall(r"pub (\w+):", _gw_struct("GatewayClient"))
    assert file_fields == ["clients", "models"], file_fields
    assert client_fields == ["name", "tier", "tokens_per_hour", "budget_source", "allowed_models"], client_fields
    variants = [v.lower() for v in re.findall(r"^\s+([A-Z]\w*),", re.search(r"pub enum BudgetSource \{(.*?)\n\}", src,
                                                                          re.S).group(1), re.M)]
    assert '#[serde(rename_all = "lowercase")]' in src
    gw = {**GW_ADMIN, "agw_clients": ["alpha", "ops-user"], "agw_models": GW_MODELS,
          "agw_tier_tokens_per_hour": {"admin": 900000},
          "agw_client_policies": {"alpha": {"tokens_per_hour": 5000}}}
    body = _gw_body(tmp_path, gw)
    assert sorted(body) == sorted(file_fields)
    for client in body["clients"]:
        assert sorted(client) == sorted(client_fields), client
        assert client["budget_source"] in variants, client
        assert isinstance(client["tokens_per_hour"], int), client



AGW_CONFIG = REPO / "platform/services/agentgateway/deployment/templates/config.yaml.j2"


@NEEDS_ANSIBLE
def test_the_console_file_agrees_with_the_gateways_own_render(tmp_path):
    # Differential: the gateway's real config.yaml.j2, rendered on the same host with the same secret declarations,
    # against the console's gateway.json from the same vars. Every budget and every allow-list must match, and the
    # key loop must enrol exactly the listed clients plus the internal and admin-only identities the console leaves out.
    gw = {**GW_ADMIN,
          "agw_upstream_base_url": "http://127.0.0.1:8000/v1",
          "agw_clients": ["alpha", "beta", "ops-user", "mcp-self", "pool-id", "or-user"],
          "agw_models": [{"name": "m1", "upstream_model": "vendor/one"},
                         {"name": "or1", "provider": "openrouter", "upstream_model": "vendor/x"},
                         {"name": "pooled", "upstream_model": "vendor/p", "pool_identity": "pool-id"}],
          "agw_openrouter_identities": ["or-user"], "agw_openrouter_admins": False,
          "agw_tier_tokens_per_hour": {"admin": 900000, "developer": 300000},
          "agw_rate_tokens_per_hour": 77777,
          "agw_client_policies": {
              "alpha": {"tokens_per_hour": 5000, "allowed_models": ["m1", "or1", "pool/pooled"]},
              "beta": {"tier": "developer", "allowed_models": []},
              "or-user": {"tier": "developer"},
              "mcp-self": {"tokens_per_hour": 1000},
              "pool-id": {"tokens_per_hour": 9000}}}
    console_body = _gw_body(tmp_path, gw)

    case = Path(tempfile.mkdtemp(dir=tmp_path))
    # The key loop reads every identity's secret, the admin-derived one included.
    secrets = {f"client_{name}": f"fixture-{name}" for name in [*gw["agw_clients"], "svc-ident"]}
    secrets["vllm_api_key"] = GW_SENTINEL
    inventory = {"all": {"hosts": {"localhost": {"ansible_connection": "local"}}, "children": {
        "agentgateway_svc": {"hosts": {"gw-test": {"ansible_connection": "local", **gw, "secrets": secrets}}}}}}
    out = case / "config.yaml"
    play = {"name": "the gateway's own render", "hosts": "gw-test", "gather_facts": False,
            "tasks": [{"ansible.builtin.include_vars": {"file": str(AGW_SECRET_DECL)}},
                      {"ansible.builtin.template": {"src": str(AGW_CONFIG), "dest": str(out), "mode": "0644"}}]}
    inv = case / "inventory.yml"
    inv.write_text(yaml.safe_dump(inventory))
    path = case / "plays.yml"
    path.write_text(yaml.safe_dump([play]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(["ansible-playbook", "-i", str(inv), str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    keys = {k["metadata"]["name"]: k for k in yaml.safe_load(out.read_text())["llm"]["policies"]["apiKey"]["keys"]}

    # The key loop enrols every identity; the console lists the clients, not the internal or admin-only ones.
    assert set(keys) == {"alpha", "beta", "ops-user", "mcp-self", "pool-id", "or-user", "svc-ident"}
    assert sorted(c["name"] for c in console_body["clients"]) == ["alpha", "beta", "ops-user", "or-user"]
    all_models = sorted(m["name"] for m in gw["agw_models"])
    for client in console_body["clients"]:
        key = keys[client["name"]]
        assert key["budgets"][0]["limit"]["amount"] == client["tokens_per_hour"], client
        assert sorted(key.get("allowedModels", all_models)) == client["allowed_models"], client
    assert console_body["models"] == all_models


# --- deploy-console.yml: the Policy page's GitHub source is refused before anything is placed (D36) -----

GITHUB_ASSERT = "Refuse a console_github_repo or console_github_ref the console would refuse"
OPENBAO_SET = {"console_openbao_url": "https://openbao.example.test"}


def _github_assert(tmp_path: Path, variables: dict) -> subprocess.CompletedProcess:
    return _run_tasks(tmp_path, [_named(DEPLOY, GITHUB_ASSERT)], {**OPENBAO_SET, **variables})


@NEEDS_ANSIBLE
def test_a_valid_github_pair_the_defaults_and_a_padded_ref_pass_the_deploy_assert(tmp_path):
    # The padded ref passes because server/config.rs trims the value before it validates it.
    for variables in [
        {},
        {"console_github_repo": "uhstray-io/agent-cloud", "console_github_ref": "feat/policy-page"},
        {"console_github_ref": " dev "},
    ]:
        proc = _github_assert(tmp_path, variables)
        assert proc.returncode == 0, (variables, proc.stdout + proc.stderr)


@NEEDS_ANSIBLE
def test_a_github_value_the_console_would_refuse_fails_the_deploy_by_name(tmp_path):
    # Native values, so the newline and the space reach the assert as written.
    for variables in [
        {"console_github_ref": "dev\nmain"},
        {"console_github_ref": "dev main"},
        {"console_github_ref": "feat/../main"},
        {"console_github_ref": "a/./b"},
        {"console_github_repo": "uhstray-io/.."},
        {"console_github_repo": "uhstray io/agent-cloud"},
        {"console_github_repo": "uhstray-io"},
    ]:
        proc = _github_assert(tmp_path, variables)
        assert proc.returncode != 0 and "Refusing to deploy the console" in proc.stdout, (
            variables,
            proc.stdout,
        )


@NEEDS_ANSIBLE
def test_the_github_assert_is_skipped_without_openbao(tmp_path):
    # Without OpenBao the pair is not rendered, so a bad value there blocks nothing.
    proc = _run_tasks(tmp_path, [_named(DEPLOY, GITHUB_ASSERT)], {"console_github_ref": "feat/../main"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- The launch gate (plan 17 D49, D53): deploy wiring --------------------------------------------------
# Documentation addresses only (RFC 5737) and example.test names. The gate is optional: each test that declares it
# also checks what an undeclared deploy renders and runs.

GATE_URL = "https://launch-gate.example.test:8443"
GATE_IP = "192.0.2.10"
GATE_SECRETS = {**ENV_SECRETS, "caller_console_token": "GATE-REF"}
GATE_DECLARED = "_console_launch_gate_declared"
GATE_PROBE = "Launch gate refuses a request with no token (POST /v1/launch -> 401, from this host)"
GATE_PROBE_ASSERT = "Assert the launch gate refused a request with no token"
GATE_OVERLAY = CONSOLE / "compose.launch-gate.yml"
DEPLOY_SH = CONSOLE / "deploy.sh"


def _gate_env(tmp_path: Path, variables: dict) -> list[str]:
    return _render(tmp_path, CONSOLE / "templates/env.j2", variables).splitlines()


def _verify_vars() -> dict:
    return next(p for p in playbook_yaml.plays(DEPLOY) if "_console_gate_probe_argv" in (p.get("vars") or {}))["vars"]


def _gate_vars(url: str, *, local: bool = False, ip: str = GATE_IP) -> dict:
    # The playbook's own declared-expression, so each refusal is judged by the same condition the deploy uses.
    return {"console_launch_gate_url": url, "console_launch_gate_ip": ip, "local_mode": local,
            GATE_DECLARED: _deploy_vars()[GATE_DECLARED]}


def _shell_function(name: str) -> str:
    lines = DEPLOY_SH.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _overlay_block() -> str:
    lines = DEPLOY_SH.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln == "LAUNCH_GATE_OVERLAY=false")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start:end + 1]) + "\n"


def _bash(script: str, cwd: Path, extra_env: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("LOCAL_MODE", "COMPOSE_OVERLAYS")}
    env.update(extra_env)
    return subprocess.run(["bash", "-c", script], cwd=cwd, env=env, text=True, capture_output=True, check=False)


@NEEDS_ANSIBLE
def test_an_undeclared_gate_renders_no_launch_gate_variable(tmp_path):
    # GATE_SECRETS is not passed here, so a render that reached for the caller token would fail.
    lines = _gate_env(tmp_path, {"secrets": ENV_SECRETS})
    assert not [ln for ln in lines if ln.startswith("CONSOLE_LAUNCH_GATE_")], lines
    # The dev-variant flag renders whether or not the gate is declared (D53).
    assert "CONSOLE_LAUNCH_DEV_VARIANTS=true" in lines


@NEEDS_ANSIBLE
def test_a_declared_gate_in_local_mode_renders_url_and_token_and_no_name_mapping(tmp_path):
    lines = _gate_env(tmp_path, {"secrets": GATE_SECRETS, "console_launch_gate_url": GATE_URL, "local_mode": True})
    assert f"CONSOLE_LAUNCH_GATE_URL={GATE_URL}" in lines
    assert "CONSOLE_LAUNCH_GATE_TOKEN=GATE-REF" in lines
    # Local mode resolves the gate's name on its own network, so no extra_hosts pair is rendered.
    assert not [ln for ln in lines if ln.startswith(("CONSOLE_LAUNCH_GATE_HOSTNAME", "CONSOLE_LAUNCH_GATE_IP"))], lines


@NEEDS_ANSIBLE
def test_a_declared_gate_in_production_renders_the_name_mapping_the_overlay_needs(tmp_path):
    lines = _gate_env(tmp_path, {"secrets": GATE_SECRETS, "console_launch_gate_url": GATE_URL,
                                 "console_launch_gate_ip": GATE_IP, "local_mode": False})
    assert f"CONSOLE_LAUNCH_GATE_URL={GATE_URL}" in lines  # the port is kept
    assert "CONSOLE_LAUNCH_GATE_TOKEN=GATE-REF" in lines
    assert "CONSOLE_LAUNCH_GATE_HOSTNAME=launch-gate.example.test" in lines
    assert f"CONSOLE_LAUNCH_GATE_IP={GATE_IP}" in lines


@NEEDS_ANSIBLE
@pytest.mark.parametrize("variables, rendered", [
    ({}, "true"),
    ({"console_launch_dev_variants": True}, "true"),
    ({"console_launch_dev_variants": False}, "false"),
    ({"console_launch_dev_variants": "false"}, "false"),
])
def test_the_dev_variant_flag_renders_as_a_lowercase_boolean(tmp_path, variables, rendered):
    lines = _gate_env(tmp_path, {"secrets": ENV_SECRETS, **variables})
    assert f"CONSOLE_LAUNCH_DEV_VARIANTS={rendered}" in lines


def test_env_template_never_sets_the_extra_ca_file():
    # The step-ca bundle's variable is the overlay's alone, so it cannot reach a deploy that did not declare the gate.
    assert "CONSOLE_EXTRA_CA_FILE" not in (CONSOLE / "templates/env.j2").read_text()


@NEEDS_ANSIBLE
def test_the_caller_token_read_names_one_key_and_is_gated_on_the_url(tmp_path):
    expr = _deploy_vars()["_console_launch_gate_reads"]
    tpl = tmp_path / "reads.j2"
    tpl.write_text("{{ gate_reads | to_json }}")
    declared = json.loads(_render(tmp_path, tpl, {"console_launch_gate_url": GATE_URL, "gate_reads": expr}))
    assert declared == [{"from_service": "launch-gate", "read_keys": ["caller_console_token"]}]
    undeclared = json.loads(_render(tmp_path, tpl, {"console_launch_gate_url": "", "gate_reads": expr}))
    assert undeclared == []
    assert "_console_launch_gate_reads" in _deploy_vars()["_shared_reads"]


@NEEDS_ANSIBLE
def test_a_launch_gate_url_that_is_not_https_with_a_host_is_refused_by_name(tmp_path):
    task = _named(DEPLOY, "Refuse console_launch_gate_url unless it is https")
    for url in ["http://launch-gate.example.test:8443", "https://", "launch-gate.example.test:8443"]:
        proc = _run_tasks(tmp_path, [task], _gate_vars(url))
        assert proc.returncode != 0 and "must be an https:// URL with a host name" in proc.stdout, (url, proc.stdout)
    ok = _run_tasks(tmp_path, [task], _gate_vars(GATE_URL))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    undeclared = _run_tasks(tmp_path, [task], _gate_vars(""))
    assert undeclared.returncode == 0, undeclared.stdout + undeclared.stderr


@NEEDS_ANSIBLE
def test_a_launch_gate_ip_that_is_not_one_address_is_refused_in_production(tmp_path):
    task = _named(DEPLOY, "Refuse console_launch_gate_ip unless it is one non-wildcard IPv4 address in production")
    # 192.0.2.256 has the right shape, so it is the octet range check that refuses it.
    for ip in ["0.0.0.0", "", "not-an-ip", "192.0.2.256"]:
        proc = _run_tasks(tmp_path, [task], _gate_vars(GATE_URL, ip=ip))
        assert proc.returncode != 0, (ip, proc.stdout)
        assert "must be the IPv4 address of the launch gate host" in proc.stdout, (ip, proc.stdout)
    ok = _run_tasks(tmp_path, [task], _gate_vars(GATE_URL, ip=GATE_IP))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # Local mode and an undeclared gate skip the address check.
    local = _run_tasks(tmp_path, [task], _gate_vars(GATE_URL, local=True, ip="0.0.0.0"))
    assert local.returncode == 0, local.stdout + local.stderr
    undeclared = _run_tasks(tmp_path, [task], _gate_vars("", ip=""))
    assert undeclared.returncode == 0, undeclared.stdout + undeclared.stderr


@NEEDS_ANSIBLE
def test_a_production_gate_needs_a_step_ca_host_to_read_the_root_from(tmp_path):
    task = _named(DEPLOY, "Refuse a production launch gate without a step_ca_svc host to read the CA root from")
    refused = _run_tasks(tmp_path, [task], _gate_vars(GATE_URL))
    assert refused.returncode != 0 and "step_ca_svc" in refused.stdout, refused.stdout
    ca_host = {"ca-host.example.test": {"ansible_connection": "local"}}
    inventory = {"all": {"hosts": {"localhost": {"ansible_connection": "local"}},
                         "children": {"step_ca_svc": {"hosts": ca_host}}}}
    playbook = {"hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": _gate_vars(GATE_URL), "tasks": [task]}
    ok = _run_with_inventory(tmp_path, inventory, playbook)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    local = _run_tasks(tmp_path, [task], _gate_vars(GATE_URL, local=True))
    assert local.returncode == 0, local.stdout + local.stderr


def test_the_launch_gate_refusals_run_before_the_bao_guard_and_the_placement():
    names = [t.get("name") for t in _tasks(DEPLOY)]
    prefixes = ("Refuse console_launch_gate", "Refuse a production launch gate")
    refusals = [n for n in names if n and n.startswith(prefixes)]
    assert len(refusals) == 3, refusals
    assert max(names.index(n) for n in refusals) < names.index(
        "Transport guard: the OpenBao endpoint must not be cleartext public") < names.index(
        "Place the monorepo + ensure podman/compose")


def test_the_production_trust_distribution_reuses_the_task_and_is_gated_on_the_gate():
    names = [t.get("name") for t in _tasks(DEPLOY)]
    name = "Distribute the step-ca root for the launch gate's TLS trust (production)"
    task = _named(DEPLOY, name)
    assert task["ansible.builtin.include_tasks"] == "tasks/distribute-ca-root.yml"
    # The bundle lands in the deploy's certs/ (what the overlay mounts). No combined bundle: the console adds roots.
    assert task["vars"] == {"_ca_root_dest_dir": "{{ _deploy_dir }}", "_ca_host": "{{ groups['step_ca_svc'] | first }}"}
    assert task["when"] == ["_console_launch_gate_declared | bool", "not (local_mode | default(false) | bool)"]
    assert names.index("Manage secrets and template env + scope files") < names.index(name) < names.index(
        "Create convenience symlink ~/console -> deploy dir")


def test_the_gate_probe_is_a_read_only_post_that_asserts_401_and_prints_no_address():
    task = _named(DEPLOY, GATE_PROBE)
    assert task["ansible.builtin.command"] == {"argv": "{{ _console_gate_probe_argv }}"}
    assert task["changed_when"] is False and task["failed_when"] is False
    assert task["until"] == "_console_gate_probe.stdout | trim == '401'"
    assert task["when"] == ["_console_launch_gate_declared | bool", "not ansible_check_mode"]
    assert_task = _named(DEPLOY, GATE_PROBE_ASSERT)
    assert assert_task["ansible.builtin.assert"]["that"] == ["_console_gate_probe.stdout | trim == '401'"]
    assert "console_launch_gate_ip" not in assert_task["ansible.builtin.assert"]["fail_msg"]
    names = [t.get("name") for t in _tasks(DEPLOY)]
    assert names.index(GATE_PROBE) < names.index(GATE_PROBE_ASSERT) < names.index("Report")


@NEEDS_ANSIBLE
def test_the_gate_probe_argv_resolves_only_outside_local_mode_and_keeps_the_port(tmp_path):
    tpl = tmp_path / "argv.j2"
    tpl.write_text("{{ _console_gate_probe_argv | to_json }}")
    verify = _verify_vars()

    def argv(url: str, local: bool) -> list[str]:
        variables = {k: verify[k] for k in ("_console_gate_probe_url", "_console_gate_probe_resolve",
                                            "_console_gate_probe_argv")}
        variables.update({"_deploy_dir": "/srv/console/deployment", "console_launch_gate_url": url,
                          "console_launch_gate_ip": GATE_IP, "local_mode": local})
        return json.loads(_render(tmp_path, tpl, variables))

    prod = argv(GATE_URL, False)
    assert prod[prod.index("--resolve") + 1] == "launch-gate.example.test:8443:192.0.2.10"
    assert prod[prod.index("--cacert") + 1] == "/srv/console/deployment/certs/step-ca-bundle.crt"
    assert prod[prod.index("--request") + 1] == "POST"
    assert prod[-1] == "https://launch-gate.example.test:8443/v1/launch"
    assert not [a for a in prod if "uthorization" in a or "GATE" in a], prod  # no credential in argv
    default_port = argv("https://launch-gate.example.test", False)
    assert default_port[default_port.index("--resolve") + 1] == "launch-gate.example.test:443:192.0.2.10"
    slash = argv(GATE_URL + "/", False)
    assert slash[-1] == "https://launch-gate.example.test:8443/v1/launch"
    assert "--resolve" not in argv(GATE_URL, True)


@NEEDS_ANSIBLE
def test_the_gate_probe_passes_on_401_and_fails_naming_the_status_otherwise(tmp_path, monkeypatch):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "curl"
    # Stands in for curl: records its argv (one per line) and prints the status the test sets.
    stub.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$GATE_STUB_ARGV"\n'
                    'printf \'%s\' "$GATE_STUB_STATUS"\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("GATE_STUB_ARGV", str(tmp_path / "argv.txt"))
    verify = _verify_vars()
    variables = {k: verify[k] for k in ("_console_gate_probe_url", "_console_gate_probe_resolve",
                                        "_console_gate_probe_argv")}
    variables.update({"_deploy_dir": str(tmp_path), "console_launch_gate_url": GATE_URL,
                      "console_launch_gate_ip": GATE_IP, "local_mode": False,
                      GATE_DECLARED: _deploy_vars()[GATE_DECLARED]})
    tasks = [_named(DEPLOY, GATE_PROBE), _named(DEPLOY, GATE_PROBE_ASSERT)]

    monkeypatch.setenv("GATE_STUB_STATUS", "401")
    passed = _run_tasks(tmp_path, tasks, variables)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    recorded = (tmp_path / "argv.txt").read_text().splitlines()
    assert recorded[recorded.index("--resolve") + 1] == "launch-gate.example.test:8443:192.0.2.10"
    assert recorded[-1] == "https://launch-gate.example.test:8443/v1/launch"

    monkeypatch.setenv("GATE_STUB_STATUS", "000")
    failed = _run_tasks(tmp_path, tasks, variables)
    assert failed.returncode != 0 and "answered 000" in failed.stdout, failed.stdout
    assert "192.0.2.10" not in failed.stdout  # the address is never printed


@pytest.mark.parametrize("local, env_file, loaded", [
    (None, "CONSOLE_LAUNCH_GATE_HOSTNAME=launch-gate.example.test\n", True),
    ("true", "CONSOLE_LAUNCH_GATE_HOSTNAME=launch-gate.example.test\n", False),
    (None, "CONSOLE_LAUNCH_GATE_URL=https://launch-gate.example.test:8443\n", False),
    (None, "CONSOLE_LAUNCH_GATE_HOSTNAME=\n", False),
])
def test_the_overlay_loads_only_outside_local_mode_and_with_the_gate_host(tmp_path, local, env_file, loaded):
    (tmp_path / ".env").write_text(env_file)
    script = ("set -euo pipefail\n" + _overlay_block()
              + 'printf "%s|%s" "$LAUNCH_GATE_OVERLAY" "${COMPOSE_OVERLAYS:-}"\n')
    proc = _bash(script, tmp_path, {} if local is None else {"LOCAL_MODE": local})
    assert proc.returncode == 0, proc.stderr
    flag, overlays = proc.stdout.split("|", 1)
    assert flag == ("true" if loaded else "false"), proc.stdout
    assert ("compose.launch-gate.yml" in overlays) == loaded, proc.stdout


@pytest.mark.parametrize("local, flag, bundle_hashed", [
    (None, "true", True),
    (None, "false", False),
    ("true", "false", True),
])
def test_step_start_hashes_the_gate_bundle_only_with_its_overlay(tmp_path, local, flag, bundle_hashed):
    if local is not None:
        (tmp_path / "compose.local.yml").write_text("services: {}\n")
    script = ("set -euo pipefail\ninfo() { :; }\n"
              'compose_up_if_changed() { echo "UP $*"; }\n'
              f"LAUNCH_GATE_OVERLAY={flag}\n" + _shell_function("step_start") + "step_start\n")
    proc = _bash(script, tmp_path, {} if local is None else {"LOCAL_MODE": local})
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.strip()
    assert line.count("config/gateway.json") == 1, line
    assert ("certs/step-ca-bundle.crt" in line) == bundle_hashed, line


REQUIRED_CONSOLE_INPUTS = (".env", "config/scope.json", "config/workflow.json", "config/flows.json",
                           "config/proxmox-ca.pem", "config/gateway.json", "config/role-map.json",
                           "config/catalog.json")


def test_the_gate_bundle_is_refused_before_a_bind_mount_can_create_a_directory(tmp_path):
    for rel in REQUIRED_CONSOLE_INPUTS:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    script = ("set -euo pipefail\ninfo() { :; }\n"
              'error() { echo "ERR: $*"; exit 1; }\n'
              f'SCRIPT_DIR="{tmp_path}"\nLAUNCH_GATE_OVERLAY=true\n'
              + _shell_function("step_verify_inputs") + "step_verify_inputs\n")
    missing = _bash(script, tmp_path, {})
    assert missing.returncode != 0 and "the launch gate's trust bundle" in missing.stdout, missing.stdout
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs/step-ca-bundle.crt").write_text(CERT_PEM)
    present = _bash(script, tmp_path, {})
    assert present.returncode == 0, present.stdout + present.stderr
    # Without the gate overlay the bundle is not required, so a deploy that never declared the gate is unchanged.
    (tmp_path / "certs/step-ca-bundle.crt").unlink()
    off = _bash(script.replace("LAUNCH_GATE_OVERLAY=true", "LAUNCH_GATE_OVERLAY=false"), tmp_path, {})
    assert off.returncode == 0, off.stdout + off.stderr


def test_the_overlay_mounts_the_bundle_it_names_and_maps_the_gate_with_required_variables():
    doc = yaml.safe_load(GATE_OVERLAY.read_text())
    assert set(doc["services"]) == {"console"}
    svc = doc["services"]["console"]
    assert set(svc) == {"volumes", "environment", "extra_hosts"}
    assert svc["volumes"] == ["./certs/step-ca-bundle.crt:/etc/ssl/certs/step-ca-bundle.crt:ro"]
    assert svc["environment"] == {"CONSOLE_EXTRA_CA_FILE": "/etc/ssl/certs/step-ca-bundle.crt"}
    assert svc["extra_hosts"] == [
        "${CONSOLE_LAUNCH_GATE_HOSTNAME:?set console_launch_gate_url in the inventory}"
        ":${CONSOLE_LAUNCH_GATE_IP:?set console_launch_gate_ip in the inventory}"]
