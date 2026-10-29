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
from test_check_mode_contract import CONSOLE_CA

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
                 "ipam.prefix", "ipam.ipaddress", "extras.tag"}


def _netbox_profiles() -> dict:
    return playbook_yaml.plays(NETBOX)[0]["vars"]["_profiles"]


def test_console_netbox_profile_is_view_only():
    profile = _netbox_profiles()["console"]
    # Only view: no add, change or delete on any type, so the token cannot write.
    assert profile["actions"] == ["view"], profile["actions"]
    assert not {"add", "change", "delete"} & set(profile["actions"])
    assert "view_object_types" not in profile, profile


def test_console_netbox_profile_covers_exactly_the_seven_declared_types():
    profile = _netbox_profiles()["console"]
    assert set(profile["object_types"]) == CONSOLE_TYPES, profile["object_types"]
    # D24 scope: prefixes and addresses, no interfaces.
    assert not any("interface" in t for t in profile["object_types"]), profile["object_types"]


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


def _state_gate(when) -> bool:
    items = when if isinstance(when, list) else [when]
    return any("_console_state in ['neither', 'stored_absent']" in str(w) for w in items)


def test_proxmox_play_is_local_and_refuses_internal_extra_vars():
    play = _proxmox_play()
    assert play["hosts"] == "localhost" and play.get("connection") == "local"
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
    # Every request carries the admin token or the OpenBao token, so none may print its result.
    uris = [t for t in _proxmox_tasks() if "ansible.builtin.uri" in t]
    assert uris, "no uri tasks found"
    loose = [t.get("name") for t in uris if t.get("no_log") is not True]
    assert loose == [], loose


def test_proxmox_debug_tasks_are_visible():
    # Only credential tasks are no_log; the decision and report print state names and no values.
    debugs = [t for t in _proxmox_tasks() if "ansible.builtin.debug" in t]
    hidden = [t.get("name") for t in debugs if t.get("no_log")]
    assert hidden == [], hidden


def test_every_write_is_gated_on_the_mint_states_and_never_check_mode():
    # A write runs only for neither or stored_absent, and never under --check. The OpenBao AppRole login is
    # a POST that writes nothing: it mints a read token and is exempt by design, so it is not a write here.
    writes = [t for t in _proxmox_tasks()
              if "auth/approle/login" not in str((t.get("ansible.builtin.uri") or {}).get("url", ""))
              and ((t.get("ansible.builtin.uri") or {}).get("method") in ("POST", "PUT")
                   or ("ansible.builtin.include_tasks" in t
                       and "bao-merge-keys" in str(t.get("ansible.builtin.include_tasks"))))]
    assert len(writes) == 5, [t.get("name") for t in writes]
    for t in writes:
        assert _state_gate(t.get("when")), t.get("name")
        assert "not ansible_check_mode" in str(t.get("when")), t.get("name")


def test_no_delete_request_exists():
    # Nothing in this play removes a token or an ACL: an operator deletes a token by hand, by name.
    methods = [(t.get("ansible.builtin.uri") or {}).get("method") for t in _proxmox_tasks()]
    assert "DELETE" not in methods


def test_a_live_stored_token_is_never_rotated():
    # stored_present is the only state that writes nothing. The mint gate names the other two states only.
    states = _proxmox_vars()
    assert "_recreate" in states
    decide = _named(PROXMOX, "Decide the state from what Proxmox and the store hold")["ansible.builtin.set_fact"]
    assert "'stored_present' if (_console_stored and _console_token_present)" in decide["_console_state"]


def test_an_unrecoverable_token_is_refused_by_name():
    that = _named(PROXMOX, "Refuse a Proxmox token the store cannot recover")["ansible.builtin.assert"]["that"]
    assert "_console_state != 'unstored_present'" in that, that


def test_a_stored_token_proxmox_no_longer_has_needs_the_recreate_flag():
    that = _named(PROXMOX, "Refuse a stored token that Proxmox no longer has, unless recreate is requested")[
        "ansible.builtin.assert"]["that"]
    assert "_console_state != 'stored_absent'" in that and "_recreate" in that, that


def test_token_is_privilege_separated_and_granted_read_only_on_root():
    tokens = [t for t in _proxmox_tasks() if (t.get("ansible.builtin.uri") or {}).get("method") == "POST"
              and "/token/" in str((t.get("ansible.builtin.uri") or {}).get("url", ""))]
    assert len(tokens) == 1, [t.get("name") for t in tokens]
    body = tokens[0]["ansible.builtin.uri"]["body"]
    assert body["privsep"] == 1, body


def test_both_acls_grant_pveauditor_on_root_user_and_token():
    acls = [t["ansible.builtin.uri"] for t in _proxmox_tasks()
            if (t.get("ansible.builtin.uri") or {}).get("method") == "PUT"
            and str((t.get("ansible.builtin.uri") or {}).get("url", "")).endswith("/access/acl")]
    assert len(acls) == 2, len(acls)
    for acl in acls:
        assert acl["body"]["path"] == "/" and acl["body"]["roles"] == "PVEAuditor", acl["body"]
    by_subject = {("users" if "users" in a["body"] else "tokens"): a["body"] for a in acls}
    assert by_subject["users"]["users"] == "{{ _console_userid }}", by_subject
    assert by_subject["tokens"]["tokens"] == "{{ _console_token_full }}", by_subject


def test_the_user_is_created_without_a_password():
    create = next(t["ansible.builtin.uri"] for t in _proxmox_tasks()
                  if (t.get("ansible.builtin.uri") or {}).get("method") == "POST"
                  and str((t.get("ansible.builtin.uri") or {}).get("url", "")).endswith("/api2/json/access/users"))
    assert "password" not in create["body"], create["body"]


def test_the_store_is_written_after_both_acl_grants():
    names = [t.get("name") for t in _proxmox_tasks()]
    store = names.index("Store the token id and secret at secret/services/proxmox (siblings preserved)")
    grants = [i for i, n in enumerate(names) if n and n.startswith("Grant PVEAuditor")]
    assert len(grants) == 2 and max(grants) < store, (grants, store)


def test_the_store_writes_the_id_and_the_secret_through_the_merge_task():
    store = _named(PROXMOX, "Store the token id and secret at secret/services/proxmox (siblings preserved)")
    assert store["ansible.builtin.include_tasks"] == "tasks/bao-merge-keys.yml"
    data = store["vars"]["_bm_data"]
    assert set(data) == {"console_token_id", "console_token"}, data
    assert store["vars"]["_bm_path"] == "services/proxmox"


def test_the_proxmox_play_never_disables_certificate_validation():
    # The admin token is never sent over unverified TLS. Grep the raw text, comments included, so no
    # spelling of the switch can hide in a comment or a task.
    text = PROXMOX.read_text()
    assert re.search(r"validate_certs\s*:\s*(false|no|0)\b", text, re.IGNORECASE) is None


def test_every_admin_header_request_verifies_against_the_declared_ca_file():
    uris = [t for t in _proxmox_tasks()
            if "_pve_headers" in str((t.get("ansible.builtin.uri") or {}).get("headers", ""))]
    assert len(uris) == 7, [t.get("name") for t in uris]
    for t in uris:
        u = t["ansible.builtin.uri"]
        assert u.get("validate_certs") is True, t.get("name")
        assert u.get("ca_path") == CONSOLE_CA, t.get("name")


def test_the_ca_gate_comes_before_any_credential_read_and_names_the_var():
    names = [t.get("name") for t in _proxmox_tasks()]
    gate_name = "Require the declared Proxmox CA before any credential is read"
    assert names.index(gate_name) < names.index("Authenticate to OpenBao")
    assert "console_proxmox_ca_pem" in _named(PROXMOX, gate_name)["ansible.builtin.assert"]["fail_msg"]


def test_the_ca_file_is_written_from_the_declared_var_privately_at_the_pinned_path_and_removed_in_always():
    write_task = _named(PROXMOX, "Write the declared CA to the pinned scratch file")
    copy = write_task["ansible.builtin.copy"]
    assert copy["content"].startswith("{{ _console_ca_pem"), copy
    assert copy["dest"] == CONSOLE_CA and copy["mode"] == "0600", copy
    # Forced under --check, so the write must be delegated to the runner and must not be given vars:.
    assert write_task["delegate_to"] == "localhost" and write_task["check_mode"] is False, write_task
    assert "vars" not in write_task, write_task
    doc = yaml.safe_load(PROXMOX.read_text())
    play = next(p for p in doc if isinstance(p, dict) and "hosts" in p)
    assert "vars" not in play, "a play-level vars: block would reach the forced CA write"
    tls = next(t for t in play["tasks"] if "block" in t)
    assert "vars" not in tls, tls
    removal_task = tls["always"][0]
    removal = removal_task["ansible.builtin.file"]
    assert removal == {"path": CONSOLE_CA, "state": "absent"}, removal
    assert removal_task["delegate_to"] == "localhost" and removal_task["check_mode"] is False, removal_task
    assert "when" not in removal_task, removal_task


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
    gate = _named(PROXMOX, "Require the declared Proxmox CA before any credential is read")
    ca_expr = _proxmox_vars()["_console_ca_pem"]
    inventory = {"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}}
    if hosts is not None:
        inventory["all"]["children"] = {"console_svc": {"hosts": hosts}}
    playbook = {"hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {"_console_ca_pem": ca_expr},
                "tasks": [gate, {"ansible.builtin.debug": {"msg": "PASSED-GATE"}}]}
    return inventory, playbook


@NEEDS_ANSIBLE
def test_the_always_cleanup_removes_the_pinned_ca_file(tmp_path):
    # The pinned path is computed from TMPDIR, so for this one run TMPDIR is the test's own directory: the
    # real cleanup task then removes a file this test wrote, never one in the runner's temp root.
    root = Path(os.path.realpath(tmp_path))
    ca = root / "console-proxmox-ca.pem"
    ca.write_text(CERT_PEM)
    task = _named(PROXMOX, "Remove the pinned CA scratch file")
    harness = tmp_path / "harness.yml"
    harness.write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local", "gather_facts": False,
                                        "tasks": [task]}]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    env["TMPDIR"] = str(root)
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(harness)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not ca.exists()


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
    ok_inv, ok_play = _ca_gate_harness({"console-local": {"ansible_connection": "local",
                                                          "console_proxmox_ca_pem": CERT_PEM}})
    ok = _run_with_inventory(tmp_path, ok_inv, ok_play)
    assert ok.returncode == 0 and "PASSED-GATE" in ok.stdout, ok.stdout + ok.stderr


@NEEDS_ANSIBLE
def test_the_recreate_refusal_is_real_in_check_mode(tmp_path):
    # The assert runs in check mode too, and refuses stored_absent unless recreate is set.
    task = _named(PROXMOX, "Refuse a stored token that Proxmox no longer has, unless recreate is requested")
    variables = {"_console_state": "stored_absent", "_recreate": False}
    refused = _run_tasks(tmp_path, [task], variables)
    assert refused.returncode != 0 and "console_proxmox_token_recreate=true" in refused.stdout
    variables["_recreate"] = True
    allowed = _run_tasks(tmp_path, [task], variables)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


@NEEDS_ANSIBLE
def test_an_unstored_live_token_is_refused_by_name(tmp_path):
    task = _named(PROXMOX, "Refuse a Proxmox token the store cannot recover")
    refused = _run_tasks(tmp_path, [task], {"_console_state": "unstored_present"})
    assert refused.returncode != 0 and "Delete that token in Proxmox" in refused.stdout
    assert _run_tasks(tmp_path, [task], {"_console_state": "stored_present"}).returncode == 0


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


def _netbox_site_tasks() -> list[dict]:
    return [_named(DEPLOY, "Collect the netbox_site slugs declared across console_tenants"),
            _named(DEPLOY, "Refuse a netbox_site slug declared on more than one site")]


def _tenants(slugs_by_site: list[list]) -> list[dict]:
    sites = [{"id": f"site-{i}", "name": f"Site {i}", "envs": ["prod"]} | ({"netbox_site": s} if s else {})
             for i, s in enumerate(slugs_by_site)]
    return [{"id": "t", "name": "T", "orgs": [{"id": "o", "name": "O", "sites": sites}]}]


@NEEDS_ANSIBLE
def test_duplicate_netbox_site_slugs_are_refused_by_name(tmp_path):
    proc = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants(["hq", "hq", "dc"])})
    assert proc.returncode != 0, proc.stdout
    assert "declares netbox_site 'hq' on more than one site" in proc.stdout, proc.stdout


@NEEDS_ANSIBLE
def test_unique_or_unmapped_netbox_sites_pass(tmp_path):
    ok = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants(["hq", None, "dc"])})
    assert ok.returncode == 0, ok.stdout + ok.stderr
    none = _run_tasks(tmp_path, _netbox_site_tasks(), {"console_tenants": _tenants([None, None])})
    assert none.returncode == 0, none.stdout + none.stderr


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
