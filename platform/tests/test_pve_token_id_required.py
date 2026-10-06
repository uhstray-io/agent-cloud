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
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

# Every play that builds a PVEAPIToken header, and the variable that carries its token id there.
TOKEN_ID_VAR = {
    "destroy-vm.yml": "_pve_token_id",
    "provision-template.yml": "_pve_token_id",
    "provision-vm.yml": "_pve_token_id",
    "proxmox-validate.yml": "_pve_token_id",
    "reconcile-o11y-backup-job.yml": "_pve_token_id",
    "resize-vm.yml": "_pve_token_id",
    "snapshot-vm.yml": "_pve_tid",
    "survey-o11y-backup-artifact.yml": "_pve_token_id",
    "survey-o11y-backup-readiness.yml": "_pve_token_id",
    "survey-o11y-pbs-physical-storage.yml": "_pve_token_id",
    "validate-address-free.yml": "_pve.token_id",
}

# A literal Proxmox token id: <user>@<realm>!<name> with no template or placeholder in it.
LITERAL_TOKEN_ID = re.compile(r"['\"][A-Za-z0-9._-]+@(pve|pam)![A-Za-z0-9._-]+['\"]")


def _plays_with_pve_header(path):
    for play in playbook_yaml.load(path) or []:
        if isinstance(play, dict) and any("PVEAPIToken" in s for s in playbook_yaml.strings(play)):
            yield play


def _token_asserts(play, var):
    return [t for t in playbook_yaml.tasks(play.get("tasks"))
            if "ansible.builtin.assert" in t
            and any(f"{var}" in s and "length > 0" in s
                    for s in playbook_yaml.strings(t["ansible.builtin.assert"].get("that")))]


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
        first_request = next(i for i, t in enumerate(ordered) if "ansible.builtin.uri" in t)
        assert ordered.index(guards[0]) < first_request, f"{name}: the token id guard runs after a request"


def _token_id_sources(play):
    """Where this play itself assigns its token id: play vars and set_fact, nothing inherited.
    Play vars and facts set in an earlier play's vars do not cross the play boundary."""
    names = ("_pve_token_id", "_pve_tid", "_pve_headers", "_pve")
    found = [str(v) for k, v in (play.get("vars") or {}).items() if k in names]
    for task in playbook_yaml.tasks(play.get("tasks")):
        facts = task.get("ansible.builtin.set_fact") or {}
        found += [str(v) for k, v in facts.items() if k in names]
    return found


@pytest.mark.parametrize("name", sorted(TOKEN_ID_VAR))
def test_each_pve_play_reads_its_token_id_from_the_store_itself(name):
    # PR 462 review: the template play validated the stored token id in its imported
    # validation play, then defaulted its own to empty and refused on the Semaphore path.
    for play in _plays_with_pve_header(PLAYBOOKS / name):
        sources = _token_id_sources(play)
        assert any(re.search(r"_pve(_data)?\.token_id", s) for s in sources), (
            f"{name}: play {play.get('name')!r} does not read the stored token id itself")


def _run_guards(tmp_path, name, token_id):
    var = TOKEN_ID_VAR[name]
    guards = [copy.deepcopy(g) for play in _plays_with_pve_header(PLAYBOOKS / name)
              for g in _token_asserts(play, var)]
    pve = {"url": "https://pve.test:8006", "api_token": "synthetic"}
    if token_id is not None:
        pve["token_id"] = token_id
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_pve": pve, "_pve_token_id": token_id or "", "_pve_tid": token_id or "",
                 "_pve_host": pve["url"], "_pve_url": pve["url"], "_pve_secret": "synthetic",
                 "_pve_sec": "synthetic", "pve_vmid": 100, "pve_node": "node1"},
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
