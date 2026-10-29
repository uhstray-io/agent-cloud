"""The Proxmox API token id comes from the store or the run, never from a literal in this repo.

Every Proxmox play used to fall back to a literal `<user>@pve!<token>` when secret/services/proxmox
had no token_id, which put a real site account into this public repository. The fallback is gone:
each play refuses, before its first Proxmox request, when the token id is empty. The token writer
(tasks/update-vault-field.yml) writes token_id only when the run names it.

The refusals run for real here (ansible-playbook on localhost), so a removed or loosened guard
fails a test rather than a production provision. Requires ansible-playbook for the run tests.
"""

import copy
import json
import os
import re
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
# Every play that builds a PVEAPIToken header, and the variable that carries its token id there.
TOKEN_ID_VAR = {
    "attach-pbs-data-disk.yml": "_pve_token_id",
    "backup-o11y-to-pbs.yml": "_pve_token_id",
    "cleanup-o11y-isolated-restore.yml": "_cleanup_pve_token_id",
    "configure-pbs-backup-access.yml": "_pve_token_id",
    "destroy-vm.yml": "_pve_token_id",
    "enable-o11y-pbs-import-content.yml": "_pve_token_id",
    "provision-console-proxmox-token.yml": "_pve_token_id",
    "provision-template.yml": "_pve_token_id",
    "provision-vm.yml": "_pve_token_id",
    "provision-pbs-debian-template.yml": "_pve_token_id",
    "register-o11y-pbs-restore-storage.yml": "_proxmox_secret.token_id",
    "prepare-pbs-guest-data-disk.yml": "_pve_token_id",
    "preflight-o11y-restore-target.yml": "_pve_token_id",
    "probe-console-sources.yml": "_probe_pve_token_id",
    "proxmox-validate.yml": "_pve_token_id",
    "read-proxmox-root-ca.yml": "_pve_token_id",
    "reconcile-o11y-backup-job.yml": "_pve_token_id",
    "restore-o11y-isolated.yml": "_restore_pve_token_id",
    "resize-vm.yml": "_pve_token_id",
    "snapshot-vm.yml": "_pve_tid",
    "survey-o11y-backup-artifact.yml": "_pve_token_id",
    "survey-o11y-backup-readiness.yml": "_pve_token_id",
    "survey-pbs-vm-census.yml": "_pve_token_id",
    "survey-pbs-provisioning-candidates.yml": "_pve_token_id",
    "survey-o11y-pbs-physical-storage.yml": "_pve_token_id",
    "validate-address-free.yml": "_pve.token_id",
    "verify-o11y-protected-pbs-copy.yml": "_pve_secret_record.token_id",
}

# A literal Proxmox token id: <user>@<realm>!<name> with no template or placeholder in it.
LITERAL_TOKEN_ID = re.compile(r"['\"][A-Za-z0-9._-]+@(pve|pam)![A-Za-z0-9._-]+['\"]")


def _expand_import_tasks(play, base):
    tasks = []
    for task in play.get("tasks", []):
        tasks.append(task)
        imported = task.get("ansible.builtin.import_tasks")
        if imported and (base / imported).is_file():
            tasks.extend(_expand_import_tasks({"tasks": playbook_yaml.load(base / imported)}, base))
    return tasks


def _plays_with_pve_header(path):
    for play in playbook_yaml.load(path) or []:
        if isinstance(play, dict):
            play = copy.deepcopy(play)
            play["tasks"] = _expand_import_tasks(play, PLAYBOOKS)
            if any("PVEAPIToken" in s for s in playbook_yaml.strings(play)):
                yield play


def _token_asserts(play, var):
    return [t for t in playbook_yaml.tasks(play.get("tasks"))
            if "ansible.builtin.assert" in t
            and any(f"{var}" in s and "length > 0" in s
                    for s in playbook_yaml.strings(t["ansible.builtin.assert"].get("that")))]


def _uses_pve_token_header(play, uri_args):
    """Resolve one level of play vars so indirect headers remain covered."""
    text = " ".join(playbook_yaml.strings(uri_args))
    if "PVEAPIToken" in text:
        return True
    return any(
        name in text and "PVEAPIToken" in " ".join(playbook_yaml.strings(value))
        for name, value in (play.get("vars") or {}).items()
    )


def test_the_table_covers_every_play_that_sends_a_pve_token():
    senders = {p.name for p in PLAYBOOKS.glob("*.yml") if any(_plays_with_pve_header(p))}
    assert senders == set(TOKEN_ID_VAR)


def test_no_playbook_carries_a_literal_token_id():
    offenders = [f"{p.relative_to(REPO)}:{n}" for p in playbook_yaml.files()
                 for n, line in enumerate(p.read_text().splitlines(), 1) if LITERAL_TOKEN_ID.search(line)]
    assert not offenders, offenders


@pytest.mark.parametrize("name", sorted(TOKEN_ID_VAR))
def test_each_pve_play_refuses_an_empty_token_id_before_its_first_request(name):
    var = TOKEN_ID_VAR[name]
    for play in _plays_with_pve_header(PLAYBOOKS / name):
        ordered = list(playbook_yaml.tasks(play.get("tasks")))
        guards = _token_asserts(play, var)
        assert guards, f"{name}: play {play.get('name')!r} sends a PVE token without refusing an empty {var}"
        uri_defaults = (play.get("module_defaults") or {}).get("ansible.builtin.uri", {})
        first_request = next(
            i for i, task in enumerate(ordered)
            if "ansible.builtin.uri" in task
            and _uses_pve_token_header(play, {**uri_defaults, **task["ansible.builtin.uri"]})
        )
        assert ordered.index(guards[0]) < first_request, f"{name}: the token id guard runs after a request"


def _token_id_sources(play):
    """Where this play itself assigns its token id: play vars and set_fact, nothing inherited.
    Play vars and facts set in an earlier play's vars do not cross the play boundary."""
    names = (
        "_pve_token_id",
        "_probe_pve_token_id",
        "_pve_tid",
        "_pve_headers",
        "_pve",
        "_pve_credentials",
        "_restore_pve_secret",
        "_cleanup_pve",
        "_pve_secret_record",
        "_proxmox_secret",
    )
    found = [str(v) for k, v in (play.get("vars") or {}).items() if k in names]
    for task in playbook_yaml.tasks(play.get("tasks")):
        facts = task.get("ansible.builtin.set_fact") or {}
        found += [str(v) for k, v in facts.items() if k in names]
    return found


def _localhost_token_id_fact_sources(play):
    """Return only a localhost set_fact handoff, never play vars or another host."""
    if play.get("hosts") != "localhost":
        return []
    found = []
    for task in playbook_yaml.tasks(play.get("tasks")):
        facts = task.get("ansible.builtin.set_fact") or {}
        if "_pve_token_id" in facts:
            found.append(str(facts["_pve_token_id"]))
    return found


def _token_id_comes_from_store(play, prior_plays):
    sources = _token_id_sources(play)
    stored = (
        r"_pve\.token_id|_pve_data\.token_id|_pve_bao_read\.json\.data\.data\.token_id"
        r"|_pve_credentials\.token_id|_restore_pve_secret\.token_id|_cleanup_pve\.token_id"
        r"|_proxmox_secret\.token_id"
        r"|_pve_secret_record\.token_id"
        r"|_probe_bao_proxmox\.json\.data\.data\.console_token_id"
    )
    if any(re.search(stored, source) for source in sources):
        return True
    if any("lookup(" in source and "secret/data/services/proxmox" in source for source in sources):
        return True
    handed_off = any(
        re.search(r"hostvars\[['\"]localhost['\"]\]\._pve_token_id", source)
        for source in sources
    )
    return handed_off and any(
        any(re.search(stored, source) for source in _localhost_token_id_fact_sources(candidate))
        for candidate in prior_plays
    )


@pytest.mark.parametrize("name", sorted(TOKEN_ID_VAR))
def test_each_pve_play_reads_its_token_id_from_the_store_itself(name):
    # PR 462 review: the template play validated the stored token id in its imported
    # validation play, then defaulted its own to empty and refused on the Semaphore path.
    plays = list(_plays_with_pve_header(PLAYBOOKS / name))
    for index, play in enumerate(plays):
        assert _token_id_comes_from_store(play, plays[:index]), (
            f"{name}: play {play.get('name')!r} does not read the stored token id itself")


def test_token_id_source_rejects_environment_fallback_self_reference_and_unproven_handoff():
    direct_fallbacks = [
        {"vars": {"_pve_token_id": "{{ lookup('env', 'PVE_TOKEN_ID') }}"}},
        {"vars": {"_pve_token_id": "{{ _pve_token_id | default('') }}"}},
        {"vars": {"_pve_token_id": "{{ hostvars['localhost']._pve_token_id | default('') }}"}},
    ]
    for play in direct_fallbacks:
        assert not _token_id_comes_from_store(play, [play])

    handoff = {"vars": {"_pve_token_id": "{{ hostvars['localhost']._pve_token_id | default('') }}"}}
    localhost_fact = {
        "hosts": "localhost",
        "tasks": [{"ansible.builtin.set_fact": {"_pve_token_id": "{{ _pve_bao_read.json.data.data.token_id }}"}}],
    }
    assert _token_id_comes_from_store(handoff, [localhost_fact])
    assert not _token_id_comes_from_store(handoff, [{
        "hosts": "pve_node",
        "tasks": [{"ansible.builtin.set_fact": {"_pve_token_id": "{{ _pve_bao_read.json.data.data.token_id }}"}}],
    }])
    assert not _token_id_comes_from_store(handoff, [{
        "hosts": "localhost",
        "vars": {"_pve_token_id": "{{ _pve_bao_read.json.data.data.token_id }}"},
    }])


def test_every_private_name_in_module_defaults_is_a_play_var():
    # Semaphore task 3177: module_defaults is templated for every task, fact gathering and the
    # task that resolves the connection included. The template play named `_pve_token_id`
    # there and set it only by set_fact, which ansible-core before 2.19 refuses as undefined;
    # 2.21 tolerates it, so a run on a newer controller cannot catch it. A `_` name is play
    # scoped (no inventory supplies it), so the play itself must declare it.
    missing = []
    for path in playbook_yaml.files():
        for play in playbook_yaml.load(path) or []:
            if not isinstance(play, dict) or "module_defaults" not in play:
                continue
            for text in playbook_yaml.strings(play["module_defaults"]):
                for expr in re.findall(r"\{\{(.*?)\}\}", text):
                    missing += [f"{path.name}: {name}" for name in re.findall(r"(?<![\w.])(_[a-z]\w*)", expr)
                                if name not in (play.get("vars") or {})]
    assert not missing, missing


def _run_guards(tmp_path, name, token_id):
    var = TOKEN_ID_VAR[name]
    guards = [copy.deepcopy(g) for play in _plays_with_pve_header(PLAYBOOKS / name)
              for g in _token_asserts(play, var)]
    for guard in guards:
        guard.pop("no_log", None)
    pve = {"url": "https://pve.test:8006", "api_token": "synthetic"}
    if token_id is not None:
        pve["token_id"] = token_id
    pve_token_id = token_id or ""
    pbs_copy_secret = {
        "o11y_copy_reader_token_id": "o11y-copy-reader@pbs!pbs-sync",
        "o11y_copy_reader_token_secret": "synthetic-reader",
        "o11y_copy_reader_fingerprint": "a" * 64,
        "o11y_copy_manager_token_id": "o11y-copy-manager@pbs!copy-api",
        "o11y_copy_manager_token_secret": "synthetic-manager",
        "o11y_copy_manager_fingerprint": "b" * 64,
        "o11y_copy_restore_user": "o11y-copy-restore@pbs",
        "o11y_copy_restore_token_id": "o11y-copy-restore@pbs!pve-restore",
        "o11y_copy_restore_token_secret": "synthetic-restore-reader",
        "o11y_copy_restore_datastore": "o11y",
        "o11y_copy_restore_fingerprint": "c" * 64,
    }
    extra_vars = {
        "backup-o11y-to-pbs.yml": {
            "_pbs_manifest_auth_id": "o11y-copy-reader@pbs!pbs-sync",
            "_pbs_manifest_secret": "synthetic-reader",
            "_pbs_fingerprint": "c" * 64,
            "_pbs_inventory": {
                "status": "observed",
                "reason": "exact_bytes_match",
                "inventory_fingerprint": "d" * 64,
            },
        },
        "cleanup-o11y-isolated-restore.yml": {
            "_cleanup_bao_auth": {"status": 200, "json": {"auth": {"client_token": "synthetic"}}},
            "_cleanup_pve_host": pve["url"],
            "_cleanup_pve_token_id": pve_token_id,
            "_cleanup_pve_token": "synthetic",
            "_cleanup_semaphore_url": "https://semaphore.test",
            "_cleanup_semaphore_token": "synthetic",
            "_cleanup_project_id": 1,
        },
        "restore-o11y-isolated.yml": {
            "_restore_pve_host": pve["url"],
            "_restore_pve_token_id": pve_token_id,
            "_restore_pve_token": "synthetic",
            "_restore_pbs_secret": pbs_copy_secret,
            "_restore_copy_token_id": "o11y-copy-restore@pbs!pve-restore",
            "_restore_copy_token": "synthetic-restore-reader",
            "_restore_copy_fingerprint": "c" * 64,
            "_restore_semaphore_secret": {"url": "https://semaphore.test", "api_token": "synthetic"},
            "_restore_semaphore_project_id": 1,
            "_restore_copy_vm_storage_id": "pbs-vm-store",
            "_restore_copy_storage_id": "pbs-vm-store",
        },
        "verify-o11y-protected-pbs-copy.yml": {
            "_pbs_copy_secret_record": pbs_copy_secret,
            "_pve_secret_record": {**pve, "token_id": pve_token_id},
            "_pbs_copy_store": "pbs-copy-store",
        },
        "register-o11y-pbs-restore-storage.yml": {
            "_proxmox_secret": {**pve, "token_id": pve_token_id},
            "_pbs_secret": pbs_copy_secret,
        },
        # The probe's guard sits behind its token-minted gate and reads its own token id fact.
        "probe-console-sources.yml": {
            "_probe_pve_called": True,
            "_probe_pve_token_id": pve_token_id,
        },
    }
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_pve": pve, "_pve_token_id": token_id or "", "_pve_tid": token_id or "",
                 "_pve_host": pve["url"], "_pve_url": pve["url"], "_pve_secret": "synthetic",
                 "_pve_token_secret": "synthetic",
                 "_pve_sec": "synthetic", "pve_vmid": 100, "pve_node": "node1",
                 **extra_vars.get(name, {})},
        "tasks": guards,
    }]
    path = tmp_path / "guards.yml"
    path.write_text(yaml.safe_dump(harness, sort_keys=False))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, stdin=subprocess.DEVNULL)


@needs_ansible
@pytest.mark.parametrize("name", sorted(TOKEN_ID_VAR))
def test_the_guard_refuses_a_missing_token_id_and_passes_a_stored_one(tmp_path, name):
    refused = _run_guards(tmp_path, name, None)
    assert refused.returncode != 0, refused.stdout[-1500:]
    assert "token_id" in refused.stdout or "pve_token_id" in refused.stdout, refused.stdout[-1500:]
    passed = _run_guards(tmp_path, name, "automation@pve!fixture")
    assert passed.returncode == 0, passed.stdout[-1500:]


@needs_ansible
@pytest.mark.parametrize("token_id,expected", [(None, {"api_token": "new"}),
                                               ("automation@pve!fixture",
                                                {"api_token": "new", "token_id": "automation@pve!fixture"})])
def test_the_token_writer_names_a_token_id_only_when_the_run_does(tmp_path, token_id, expected):
    tasks = yaml.safe_load((PLAYBOOKS / "tasks/update-vault-field.yml").read_text())
    merge = copy.deepcopy(next(t for t in tasks if "ansible.builtin.include_tasks" in t))
    out = tmp_path / "data.json"
    del merge["ansible.builtin.include_tasks"]
    merge["ansible.builtin.copy"] = {"content": "{{ _bm_data | to_json }}", "dest": str(out)}
    extra = {"proxmox_api_token": "new", "_vault_path": "secret/data/services/proxmox",
             "_bao_url": "https://bao.test", "_bao_auth": {"json": {"auth": {"client_token": "t"}}}}
    if token_id is not None:
        extra["proxmox_token_id"] = token_id
    path = tmp_path / "writer.yml"
    path.write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local", "gather_facts": False,
                                     "vars": extra, "tasks": [merge]}], sort_keys=False))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout[-1500:]
    assert json.loads(out.read_text()) == expected
