"""The runner-local SSH probe key has ONE implementation, and it works under --check.

distribute-ssh-keys, harden-ssh and verify-host-access each hand-rolled "tempfile, copy the
key 0600, ssh -i, delete". ansible.builtin.tempfile has no check-mode support, so under
--check it registered no `path` and the copy writing the key aborted: none of the three
could pass a dry run (the class Semaphore task 1756 hit in the keygen). They now include
tasks/materialise-ssh-key.yml and wipe with tasks/remove-ssh-key.yml from `always`.

The behavioural tests lift each playbook's real key section out of the file, run it on
localhost under --check (and for real) with `ssh` replaced by a stub on PATH, and require:
success; that the stub saw the key at 0600 in a 0700 directory with the exact material;
that the directory is gone afterwards, including when the probe fails; and that the
material never reaches the output, even at -v.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
MATERIALISE = "tasks/materialise-ssh-key.yml"
REMOVE = "tasks/remove-ssh-key.yml"
# Multi-line like a real key, with no key framing (the secret gates rightly refuse one).
KEY = "STUB-KEY-MATERIAL-line-1\nSTUB-KEY-MATERIAL-line-2\nSTUB-KEY-MATERIAL-line-3"
HOSTKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStubHostKey root@target"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

# Records what the key looked like AT THE MOMENT ssh used it, then answers like a host that
# refuses passwords and accepts the key (SSH_STUB=ok) or refuses everything (SSH_STUB=fail).
SSH_STUB = r'''#!/usr/bin/env python3
import json, os, stat, sys
args = sys.argv[1:]
seen = {"args": args}
if "-i" in args:
    key = args[args.index("-i") + 1]
    seen["key"] = key
    if os.path.exists(key):
        seen["key_mode"] = oct(stat.S_IMODE(os.stat(key).st_mode))
        seen["dir_mode"] = oct(stat.S_IMODE(os.stat(os.path.dirname(key)).st_mode))
        seen["content"] = open(key).read()
for a in args:
    if a.startswith("UserKnownHostsFile="):
        kh = a.split("=", 1)[1]
        seen["known_hosts"] = open(kh).read() if os.path.exists(kh) else None
with open(os.environ["SSH_STUB_LOG"], "a") as log:
    log.write(json.dumps(seen) + "\n")
if os.environ.get("SSH_STUB") != "ok" or "PubkeyAuthentication=no" in args or "key" not in seen:
    sys.exit(255)
print(args[-1].split()[-1])
'''


def _tasks(path: Path) -> list[dict]:
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    for play in yaml.safe_load(path.read_text()):
        walk(play.get("tasks"))
    return out


def _named(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


def _replace(block: dict, name: str, stub: dict) -> dict:
    """Swap one task of a lifted block for a stub (a secret-store read, a host read). The
    original's no_log carries over, so the stub hides exactly what the real task hides."""
    block = json.loads(json.dumps(block))
    hits = [i for i, t in enumerate(block["block"]) if t.get("name") == name]
    assert len(hits) == 1, f"{name!r} not in the block"
    original = block["block"][hits[0]]
    block["block"][hits[0]] = {"name": name, **stub, **({"no_log": original["no_log"]} if "no_log" in original else {})}
    return block


def _section(playbook: str):
    """(tasks, vars) for each playbook's real key section, secret-store reads stubbed."""
    path = PLAYBOOKS / playbook
    if playbook == "distribute-ssh-keys.yml":
        block = _named(path, "Test SSH key auth with a runner-local copy of the management key")
        return [block], {"_mgmt_priv_key": KEY, "_ssh_user": "tester", "service_name": "svc",
                         "ansible_host": "192.0.2.10"}, "_mgmt_key"
    if playbook == "harden-ssh.yml":
        block = _named(path, "Verify the lockdown with a runner-local copy of the management key")
        return [block], {"_mgmt_priv_key": KEY, "ansible_user": "tester", "service_name": "svc",
                         "ansible_host": "192.0.2.10"}, "_verify_key"
    block = _named(path, "Key-only reachability probe")
    block = _replace(block, "Fetch the per-service private key from the secret store",
                     {"ansible.builtin.set_fact": {"_svc_key": KEY}})
    block = _replace(block, "Read the target's own SSH host key over the existing connection",
                     {"ansible.builtin.set_fact": {"_hostkey": {"stdout": HOSTKEY}}})
    return [block], {"_reach": {"rc": 0}, "_bao_url": "https://bao.example.test", "service_name": "svc",
                     "_target_addr": "192.0.2.10", "_target_user": "tester", "_target_port": 22}, "_probe_key"


def _run(tmp_path: Path, playbook: str, *, check: bool, ssh: str):
    tasks, variables, result_var = _section(playbook)
    probe = {"ansible.builtin.debug": {"msg": "PROBE {{ " + result_var + " | to_json }}"}}
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
                "tasks": [*tasks, probe]}]
    # The lifted tasks include tasks/... relative to the playbook; serve the real library.
    (tmp_path / "tasks").symlink_to(PLAYBOOKS / "tasks")
    (tmp_path / "bin").mkdir()
    stub = tmp_path / "bin/ssh"
    stub.write_text(SSH_STUB)
    stub.chmod(0o755)
    (tmp_path / "h.yml").write_text(yaml.safe_dump(harness))
    log = tmp_path / "ssh.log"
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", SSH_STUB=ssh, SSH_STUB_LOG=str(log),
               PATH=f"{tmp_path / 'bin'}:{env.get('PATH', '')}")
    cmd = ["ansible-playbook", "-v", "-i", "localhost,", str(tmp_path / "h.yml")] + (["--check"] if check else [])
    out = subprocess.run(cmd, cwd=REPO, env=env, text=True, capture_output=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    result = None
    for line in out.stdout.splitlines():
        if "PROBE " in line:
            result = json.loads(json.loads(line.split('"msg": ', 1)[1]).split("PROBE ", 1)[1])
    return out, calls, result


CONVERTED = ["distribute-ssh-keys.yml", "harden-ssh.yml", "verify-host-access.yml"]


@needs_ansible
@pytest.mark.parametrize("check", [True, False], ids=["check", "real"])
@pytest.mark.parametrize("playbook", CONVERTED)
def test_key_section_probes_with_a_private_key_and_wipes_it(tmp_path, playbook, check):
    out, calls, result = _run(tmp_path, playbook, check=check, ssh="ok")
    assert out.returncode == 0, (out.stdout + out.stderr)[-3000:]
    keyed = [c for c in calls if "key" in c]
    assert len(keyed) == 1, calls
    seen = keyed[0]
    assert seen.get("content") == KEY + "\n", "the probe ran without the key on disk"
    assert (seen["key_mode"], seen["dir_mode"]) == ("0o600", "0o700"), seen
    assert result["materialised"] is True and seen["key"] == result["key"]
    assert not Path(result["dir"]).exists(), "the scratch directory survived the run"
    if playbook == "verify-host-access.yml":
        assert seen["known_hosts"] == "192.0.2.10 " + " ".join(HOSTKEY.split()[:2]) + "\n", seen
    assert "STUB-KEY-MATERIAL" not in out.stdout + out.stderr


@needs_ansible
@pytest.mark.parametrize("playbook", CONVERTED)
def test_a_failed_probe_still_wipes_the_key(tmp_path, playbook):
    # distribute and harden fail the host on a refused key; the access gate reports it.
    out, calls, _ = _run(tmp_path, playbook, check=False, ssh="fail")
    keyed = [c for c in calls if "key" in c]
    assert len(keyed) == 1 and keyed[0].get("content") == KEY + "\n", calls
    assert (out.returncode == 0) is (playbook == "verify-host-access.yml"), out.stdout[-2000:]
    assert not Path(keyed[0]["key"]).parent.exists(), "a failed probe left the key on the runner"


@needs_ansible
def test_distribute_dry_run_tolerates_a_key_not_yet_authorised(tmp_path):
    # Under --check the authorized_key writes were simulated, so a host that does not yet
    # trust the key is the expected state, not a dry-run failure.
    out, _, result = _run(tmp_path, "distribute-ssh-keys.yml", check=True, ssh="fail")
    assert out.returncode == 0, out.stdout[-2000:]
    assert "dry run" in out.stdout and not Path(result["dir"]).exists()


@needs_ansible
def test_an_empty_key_is_refused_unless_the_caller_reports_it(tmp_path):
    harness_tasks = [{"block": [{"ansible.builtin.include_tasks": MATERIALISE,
                                 "vars": {"ssh_key_content": "", "ssh_key_result_var": "_k",
                                          "ssh_key_allow_empty": "{{ allow }}"}}],
                      "always": [{"ansible.builtin.include_tasks": REMOVE, "vars": {"ssh_key_result_var": "_k"}}]},
                     {"ansible.builtin.debug": {"msg": "PROBE {{ _k | to_json }}"}}]
    (tmp_path / "tasks").symlink_to(PLAYBOOKS / "tasks")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    for allow, ok in ((False, False), (True, True)):
        (tmp_path / "h.yml").write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local",
                                                         "gather_facts": False, "vars": {"allow": allow},
                                                         "tasks": harness_tasks}]))
        out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(tmp_path / "h.yml"), "--check"],
                             cwd=REPO, env=env, text=True, capture_output=True)
        assert (out.returncode == 0) is ok, out.stdout[-1500:]
        if ok:
            assert '\\"materialised\\": false' in out.stdout, out.stdout[-1500:]
        else:
            assert "No private key material" in out.stdout


def test_remove_refuses_a_directory_it_did_not_create():
    task = yaml.safe_load((PLAYBOOKS / REMOVE).read_text())[0]
    assert task["ansible.builtin.file"]["state"] == "absent"
    assert task.get("check_mode") is False, "a simulated delete leaves the key on the runner"
    assert any(".sshkey_" in str(w) for w in task["when"]), task["when"]


def test_every_converted_playbook_wipes_in_always():
    for playbook in CONVERTED:
        blocks = [t for t in _tasks(PLAYBOOKS / playbook)
                  if any(s.get("ansible.builtin.include_tasks") == MATERIALISE for s in t.get("block") or [])]
        assert len(blocks) == 1, playbook
        (block,) = blocks
        (inc,) = [s for s in block["block"] if s.get("ansible.builtin.include_tasks") == MATERIALISE]
        wipes = [s for s in block.get("always") or [] if s.get("ansible.builtin.include_tasks") == REMOVE]
        assert [w["vars"]["ssh_key_result_var"] for w in wipes] == [inc["vars"]["ssh_key_result_var"]], playbook


def test_only_the_write_of_the_key_is_no_log():
    tasks = yaml.safe_load((PLAYBOOKS / MATERIALISE).read_text())
    hidden = [t["name"] for t in tasks if t.get("no_log")]
    assert hidden == ["Materialise SSH key: write the key material (0600)"], hidden
    (write,) = [t for t in tasks if t["name"] == hidden[0]]
    assert write["ansible.builtin.copy"]["mode"] == "0600" and write.get("diff") is False
    for t in tasks:
        if t.get("check_mode") is False:
            assert t.get("delegate_to") == "localhost", f"{t['name']} runs for real under --check off the runner"


def _all_tasks(path: Path) -> list[dict]:
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks", "handlers"):
                    if isinstance(t.get(k), list):
                        walk(t[k])

    doc = yaml.safe_load(path.read_text())
    walk(doc if isinstance(doc, list) else [])
    return out


def _yml():
    return sorted([*PLAYBOOKS.rglob("*.yml"), *(REPO / "platform/semaphore").rglob("*.yml")])


# CLOSED sets, not "the shared task contains the write": the failure mode is a caller
# growing its own copy again, which a presence check cannot see (test_backup_ssh_key.bats
# makes the same argument for the backup write). Each other member is a different job.
PRIVATE_KEY_WRITERS = {
    MATERIALISE: "the login key for an ssh -i probe of a target host",
    "tasks/backup-ssh-key-to-site-config.yml": "the backup copy and its pair derivation",
    "tasks/site-config-clone.yml": "the site-config deploy key for git over ssh",
}
# A writer is recognised by the variable its content templates, so a copy that renamed the
# key to something bland would slip past this set; the tempfile set is the second net.
KEY_VAR_HINTS = ("priv", "private_key", "_svc_key", "ssh_key", "deploy_key")
TEMPFILE_USERS = {
    MATERIALISE: "the probe key's scratch directory",
    "tasks/backup-ssh-key-to-site-config.yml": "pair derivation scratch",
    "tasks/site-config-clone.yml": "clone and deploy-key scratch",
    "generate-service-ssh-key.yml": "ssh-keygen output scratch",
}


def test_exactly_one_file_materialises_an_ssh_probe_key():
    writers, tempfiles = set(), set()
    for path in _yml():
        try:
            tasks = _all_tasks(path)
        except yaml.YAMLError:
            continue
        rel = str(path.relative_to(PLAYBOOKS)) if path.is_relative_to(PLAYBOOKS) else str(path)
        for t in tasks:
            copy = t.get("ansible.builtin.copy") or t.get("copy")
            if isinstance(copy, dict) and any(w in str(copy.get("content", "")) for w in KEY_VAR_HINTS):
                writers.add(rel)
            if "ansible.builtin.tempfile" in t or "tempfile" in t:
                tempfiles.add(rel)
    assert writers == set(PRIVATE_KEY_WRITERS), f"private-key writers changed: {sorted(writers)}"
    assert tempfiles == set(TEMPFILE_USERS), f"tempfile users changed: {sorted(tempfiles)}"
