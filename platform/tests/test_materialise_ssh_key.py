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
import re
import shutil
import tempfile
from pathlib import Path

import harness_sandbox
import pytest
import test_check_mode_contract as check_mode_contract
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
MATERIALISE = "tasks/materialise-ssh-key.yml"
REMOVE = "tasks/remove-ssh-key.yml"
PIN = "tasks/pin-ssh-host-key.yml"
# Multi-line like a real key, with no key framing (the secret gates rightly refuse one).
KEY = "STUB-KEY-MATERIAL-line-1\nSTUB-KEY-MATERIAL-line-2\nSTUB-KEY-MATERIAL-line-3"
HOSTKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStubHostKey root@target"

# The harness runs these tasks FOR REAL on the developer's machine. Two independent lines keep
# it inside its scratch (docs/MISTAKES.md 3.9: a mutation run once overwrote the real
# ~/.ssh/known_hosts; PR #319 review: a source guard alone was routed around by indirection):
# 1. before any run, the check-mode contract's runner-scratch rule applied to EVERY file write
#    in the lifted section and the shared tasks, plus the repository-wide pinned definitions;
# 2. the run itself goes through harness_sandbox.run (a kernel sandbox where one exists).
# Turning line 1 off is allowed only to prove line 2, and only under a kernel sandbox.
SHARED = [MATERIALISE, REMOVE, PIN]
STATIC_GUARD = os.environ.get("SSH_HARNESS_STATIC_GUARD", "1") != "0"


def _stray_writes(tasks, rel=None) -> list[str]:
    return check_mode_contract.violations_in(tasks, rel, all_writes=True)


def _refuse_real_writes(tasks):
    if not STATIC_GUARD:
        assert harness_sandbox.SANDBOX, "the static guard may be disabled only under a kernel sandbox"
        return
    stray = _stray_writes(tasks)
    for shared in SHARED:
        rel = f"platform/playbooks/{shared}"
        stray += _stray_writes(yaml.safe_load((PLAYBOOKS / shared).read_text()), rel)
    stray += check_mode_contract.pinned_definition_problems()
    assert not stray, f"refusing to run: these would write outside the runner scratch: {stray}"


def _ansible(tmp_path: Path, args: list[str], tasks, env_extra: dict | None = None, denied=None):
    """Every ansible run in this file: guard first, then the sandboxed run."""
    _refuse_real_writes(tasks)
    env = harness_sandbox.env_for(tmp_path)
    env.update(env_extra or {})
    return harness_sandbox.run(["ansible-playbook", *args], tmp_path, cwd=REPO, env=env, denied=denied)


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
        seen["known_hosts_path"] = kh
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
        return [block], {"_mgmt_priv_key": KEY, "_ssh_user": "tester", "service_name": "svc"}, "_mgmt_key"
    if playbook == "harden-ssh.yml":
        block = _named(path, "Verify the lockdown with a runner-local copy of the management key")
        return [block], {"_mgmt_priv_key": KEY, "ansible_user": "tester", "service_name": "svc"}, "_verify_key"
    block = _named(path, "Key-only reachability probe")
    block = _replace(block, "Fetch the per-service private key from the secret store",
                     {"ansible.builtin.set_fact": {"_svc_key": KEY}})
    return [block], {"_reach": {"rc": 0}, "_bao_url": "https://bao.example.test", "service_name": "svc",
                     "_target_addr": "192.0.2.10", "_target_user": "tester", "_target_port": 22}, "_probe_key"


def _run(tmp_path: Path, playbook: str, *, check: bool, ssh: str, host_key: bool = True, extra: dict | None = None,
         cli: list[str] | None = None):
    tasks, variables, result_var = _section(playbook)
    variables = {**variables, **(extra or {})}
    # The pin's raw read really runs (over the local connection); only the file it reads
    # is a stand-in for the target's /etc/ssh host key.
    hostkey = tmp_path / "ssh_host_ed25519_key.pub"
    if host_key:
        hostkey.write_text(HOSTKEY + "\n")
    variables = {**variables, "ssh_host_key_files": [str(tmp_path / "absent.pub"), str(hostkey)]}
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
    # ansible_host as an INVENTORY var, where real inventories put it: the playbooks read it
    # through hostvars[inventory_hostname], which a play var does not reach.
    (tmp_path / "inventory.ini").write_text("localhost ansible_connection=local ansible_host=192.0.2.10\n")
    args = ["-v", "-i", str(tmp_path / "inventory.ini"), str(tmp_path / "h.yml")] + (["--check"] if check else [])
    args += cli or []
    out = _ansible(tmp_path, args, tasks, {"SSH_STUB": ssh, "SSH_STUB_LOG": str(log),
                                           "PATH": f"{tmp_path / 'bin'}:{os.environ.get('PATH', '')}"})
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
    # Every ssh call trusts exactly the host key read over the Ansible connection, from the
    # scratch file, and nothing in the runner's own known_hosts.
    pinned = "192.0.2.10 " + " ".join(HOSTKEY.split()[:2]) + "\n"
    for call in calls:
        assert call.get("known_hosts_path") == result["known_hosts"], call
        assert call.get("known_hosts") == pinned, call
        assert "StrictHostKeyChecking=yes" in call["args"], call
    assert "STUB-KEY-MATERIAL" not in out.stdout + out.stderr
    # The scratch leaves nothing behind, so a dry run must not report it as a change.
    assert "changed=0" in out.stdout.rsplit("PLAY RECAP", 1)[1], out.stdout[-800:]


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
    for allow, ok in ((False, False), (True, True)):
        (tmp_path / "h.yml").write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local",
                                                         "gather_facts": False, "vars": {"allow": allow},
                                                         "tasks": harness_tasks}]))
        out = _ansible(tmp_path, ["-i", "localhost,", str(tmp_path / "h.yml"), "--check"], harness_tasks)
        assert (out.returncode == 0) is ok, out.stdout[-1500:]
        if ok:
            assert '\\"materialised\\": false' in out.stdout, out.stdout[-1500:]
        else:
            assert "No private key material" in out.stdout


def test_remove_refuses_a_directory_it_did_not_create():
    (task,) = [t for t in yaml.safe_load((PLAYBOOKS / REMOVE).read_text()) if "ansible.builtin.file" in t]
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
    "run-agw-conformance.yml": "conformance key files and results, removed in an always: block",
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


def _ssh_calls(path: Path) -> list[list[str]]:
    calls = []
    for task in _tasks(path):
        args = task.get("ansible.builtin.command") or {}
        # a Jinja expression is one word even though it contains spaces
        argv = args.get("argv") or re.findall(r"\S*\{\{.*?\}\}\S*|\S+", str(args.get("cmd") or ""))
        if argv and argv[0] == "ssh":
            calls.append([str(a) for a in argv])
    return calls


@pytest.mark.parametrize("playbook", CONVERTED)
def test_every_ssh_call_uses_the_scratch_known_hosts(playbook):
    # accept-new against the runner's ~/.ssh/known_hosts wrote to the runner on every run,
    # dry runs included; pointing ssh at the scratch file keeps the run inside the scratch.
    calls = _ssh_calls(PLAYBOOKS / playbook)
    assert calls, playbook
    for argv in calls:
        files = [a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile=")]
        assert len(files) == 1 and files[0].endswith(".known_hosts }}"), argv
        checking = [a for a in argv if a.startswith("StrictHostKeyChecking=")]
        assert checking == ["StrictHostKeyChecking=yes"], argv


@pytest.mark.parametrize("playbook", CONVERTED)
def test_the_host_key_is_pinned_by_the_shared_task_before_any_probe(playbook):
    block = next(t for t in _tasks(PLAYBOOKS / playbook)
                 if any(s.get("ansible.builtin.include_tasks") == MATERIALISE for s in t.get("block") or []))
    steps = block["block"]
    includes = [s.get("ansible.builtin.include_tasks") for s in steps]
    assert PIN in includes, playbook
    first_ssh = next(i for i, s in enumerate(steps) if "ansible.builtin.command" in s)
    assert includes.index(MATERIALISE) < includes.index(PIN) < first_ssh, playbook
    (pin,) = [s for s in steps if s.get("ansible.builtin.include_tasks") == PIN]
    materialise = next(s for s in steps if s.get("ansible.builtin.include_tasks") == MATERIALISE)
    assert pin["vars"]["ssh_key_result_var"] == materialise["vars"]["ssh_key_result_var"]
    # only the shared task reads the host key; no third copy in a caller
    assert "ssh_host_ed25519_key" not in (PLAYBOOKS / playbook).read_text(), playbook


@needs_ansible
def test_remove_deletes_only_a_scratch_dir_under_the_temp_root(tmp_path):
    # A `.sshkey_` name is not enough: the directory must sit directly under the temp root
    # the materialise task uses. pytest's tmp_path is nested below that root.
    decoy = tmp_path / ".sshkey_decoy"
    decoy.mkdir()
    (decoy / "keep").write_text("x")
    (tmp_path / "tasks").symlink_to(PLAYBOOKS / "tasks")
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {"_k": {"materialised": True, "dir": str(decoy)}},
                "tasks": [{"ansible.builtin.include_tasks": REMOVE, "vars": {"ssh_key_result_var": "_k"}}]}]
    (tmp_path / "h.yml").write_text(yaml.safe_dump(harness))
    out = _ansible(tmp_path, ["-i", "localhost,", str(tmp_path / "h.yml")], harness[0]["tasks"])
    assert out.returncode == 0, out.stdout[-1500:]
    assert (decoy / "keep").exists(), "the wipe deleted a .sshkey_ directory it did not create"


def test_materialise_and_remove_agree_on_the_temp_root():
    root = yaml.safe_load((PLAYBOOKS / MATERIALISE).read_text())
    (tmp,) = [t for t in root if "ansible.builtin.tempfile" in t]
    _resolve, wipe = yaml.safe_load((PLAYBOOKS / REMOVE).read_text())
    assert tmp["ansible.builtin.tempfile"]["path"] == check_mode_contract.SCRATCH_ROOT
    assert "(_rsk_dir | realpath | dirname) == " + check_mode_contract.SCRATCH_ROOT_INLINE in wipe["when"]


@needs_ansible
@pytest.mark.parametrize("playbook", CONVERTED)
def test_no_readable_host_key_means_no_probe(tmp_path, playbook):
    # An unpinned probe would have to trust whatever answers. Distribute and Harden refuse;
    # the access gate reports it as NO-GO. Either way ssh is never run.
    out, calls, _ = _run(tmp_path, playbook, check=False, ssh="ok", host_key=False)
    assert [c for c in calls if "key" in c] == [], calls
    if playbook == "verify-host-access.yml":
        assert out.returncode == 0, out.stdout[-1500:]
    else:
        assert out.returncode != 0 and "Refusing" in out.stdout, out.stdout[-1500:]


@needs_ansible
def test_a_non_default_port_is_pinned_as_host_and_port(tmp_path):
    out, calls, _ = _run(tmp_path, "verify-host-access.yml", check=True, ssh="ok", extra={"_target_port": 2222})
    assert out.returncode == 0, out.stdout[-1500:]
    (seen,) = [c for c in calls if "key" in c]
    assert seen["known_hosts"] == "[192.0.2.10]:2222 " + " ".join(HOSTKEY.split()[:2]) + "\n", seen



def test_the_harness_refuses_a_write_outside_the_scratch():
    # Static: nothing is executed, so a failure of this guard cannot touch the machine.
    for dest in ("~/.ssh/known_hosts", "/etc/motd", "{{ _other.known_hosts }}", "{{ _probe_key.known_hosts }}"):
        bad = [{"name": "w", "block": [{"name": "w", "ansible.builtin.copy": {"content": "x", "dest": dest},
                                        "delegate_to": "localhost"}]}]
        with pytest.raises(AssertionError, match="refusing to run"):
            _refuse_real_writes(bad)


@needs_ansible
@pytest.mark.skipif(harness_sandbox.SANDBOX is None, reason="no kernel sandbox on this host (see harness_sandbox)")
def test_the_sandbox_blocks_a_write_the_static_guard_never_saw(tmp_path, tmp_path_factory):
    # The target is a CANARY directory this test creates and denies explicitly; the real home
    # is never aimed at. A crafted play bypasses the static guard on purpose.
    canary = tmp_path_factory.mktemp("sandbox-canary")
    target = canary / "written"
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "tasks": [{"ansible.builtin.copy": {"content": "x", "dest": str(target)}}]}]
    (tmp_path / "h.yml").write_text(yaml.safe_dump(play))
    out = harness_sandbox.run(["ansible-playbook", "-i", "localhost,", str(tmp_path / "h.yml")], tmp_path,
                              cwd=REPO, env=harness_sandbox.env_for(tmp_path), denied=[str(canary)])
    assert out.returncode != 0 and not target.exists(), out.stdout[-1500:]
    # and the same run, not denied, writes: the failure above is the sandbox, not the play
    out = harness_sandbox.run(["ansible-playbook", "-i", "localhost,", str(tmp_path / "h.yml")], tmp_path,
                              cwd=REPO, env=harness_sandbox.env_for(tmp_path))
    assert out.returncode == 0 and target.exists(), out.stdout[-1500:]


def test_the_sandbox_profile_denies_writes_by_default():
    profile = harness_sandbox._profile(["/private/var/folders/x"], ["/private/var/folders/x/canary"])
    assert "(deny file-write*)" in profile and '(subpath "/Users")' not in profile
    # explicit denials come LAST, since later rules win
    assert profile.rindex("(deny file-write*") > profile.index("(allow file-write*")


@needs_ansible
@pytest.mark.skipif(harness_sandbox.SANDBOX != "sandbox-exec", reason="macOS sandbox profile only")
def test_the_sandbox_refuses_writes_outside_its_allowlist(tmp_path):
    # Not denied explicitly: refused only because they are outside the allowlist. Both probe
    # roots are created here and removed afterwards; neither is the home directory or a
    # system path.
    roots = [Path(tempfile.mkdtemp(prefix=".sandbox-probe-", dir=REPO)),
             Path(tempfile.mkdtemp(prefix="sandbox-probe-", dir="/private/tmp"))]
    try:
        for root in roots:
            assert not any(str(root).startswith(a) for a in (str(tmp_path), harness_sandbox.temp_root())), root
            target = root / "written"
            play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                     "tasks": [{"ansible.builtin.copy": {"content": "x", "dest": str(target)}}]}]
            (tmp_path / "h.yml").write_text(yaml.safe_dump(play))
            out = harness_sandbox.run(["ansible-playbook", "-i", "localhost,", str(tmp_path / "h.yml")], tmp_path,
                                      cwd=REPO, env=harness_sandbox.env_for(tmp_path))
            assert out.returncode != 0 and not target.exists(), (root, out.stdout[-1200:])
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)


@needs_ansible
@pytest.mark.parametrize("playbook", CONVERTED)
def test_extra_vars_cannot_move_the_pinned_known_hosts(tmp_path, playbook):
    # PR #319 review (B4): `-e _pshk_root=X -e _pshk_kh=X/.sshkey_q/known_hosts` outranked the
    # set_fact and the runtime check compared against the moved root, so it WROTE. The root
    # is now computed inline. The target is inside tmp_path, which the sandbox allows, so only
    # that check stands in the way; the directory exists so the copy would otherwise succeed.
    evil = tmp_path / "evil" / ".sshkey_q"
    evil.mkdir(parents=True)
    kh = evil / "known_hosts"
    out, _, _ = _run(tmp_path, playbook, check=True, ssh="ok",
                     cli=["-e", f"_pshk_root={tmp_path / 'evil'}", "-e", f"_pshk_kh={kh}"])
    assert not kh.exists(), "an extra var redirected the pinned known_hosts"
    assert out.returncode != 0 and "refusing to write it" in out.stdout, out.stdout[-1500:]


@needs_ansible
def test_extra_vars_cannot_widen_the_wipe(tmp_path):
    # B4 for the wipe: neither a moved root nor a directory handed in by extra var is deleted.
    decoy = tmp_path / ".sshkey_decoy"
    decoy.mkdir()
    (decoy / "keep").write_text("x")
    (tmp_path / "tasks").symlink_to(PLAYBOOKS / "tasks")
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                "tasks": [{"ansible.builtin.include_tasks": REMOVE, "vars": {"ssh_key_result_var": "_k"}}]}]
    (tmp_path / "h.yml").write_text(yaml.safe_dump(harness))
    out = _ansible(tmp_path, ["-i", "localhost,", str(tmp_path / "h.yml"),
                              "-e", f"_rsk_root={tmp_path}", "-e", f"_rsk_dir={decoy}"], harness[0]["tasks"])
    assert out.returncode == 0, out.stdout[-1500:]
    assert (decoy / "keep").exists(), "an extra var widened the wipe"
