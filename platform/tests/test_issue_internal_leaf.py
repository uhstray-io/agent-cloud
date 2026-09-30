"""tasks/issue-internal-leaf.yml: a declared leaf, its key made on the consumer and never sent.

The task's own tasks are lifted and run on localhost (consumer and "CA host" alike). Two are
replaced: the OpenBao read of the issuer password (a fixed test value instead) and the
transport guard include (it has its own tests). The container engine is a stub that keeps
what the CA would receive and answers with a certificate made here by openssl, so the
placement, the `current` swap and the pruning run for real (openspec production-internal-ca
tasks 4.1, 4.2 and 4.5). Runs through harness_sandbox.
"""

import json
import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

TASKS = playbook_yaml.REPO / "platform/playbooks/tasks/issue-internal-leaf.yml"
SITE = {"dns_site": "dc1", "dns_zone": "example.internal"}
REPLACED = {"Read the issuing provisioner's password", "Refuse a cleartext secret-store endpoint"}


def _lifted() -> list:
    """The task file with the two replaced steps swapped out, blocks kept."""
    out = []
    for task in playbook_yaml.load(TASKS):
        if "block" in task:
            block = []
            for inner in task["block"]:
                if inner["name"] == "Read the issuing provisioner's password":
                    block.append({"name": "fake issuer password",
                                  "ansible.builtin.set_fact": {"_leaf_issuer_pw": "test-issuer-pw"}})
                elif inner["name"] not in REPLACED:
                    block.append(inner)
            task = {**task, "block": block}
        out.append(task)
    return out


def _stub(tmp: Path, refuse: bool = False) -> Path:
    """An engine whose `exec -i` keeps its stdin and prints a freshly made certificate."""
    stub = tmp / "engine"
    answer = 'echo "SIGNERR: certificate request does not match the policy"; exit 1' if refuse else (
        f'n=$(ls "{tmp}"/stdin.* 2>/dev/null | wc -l | tr -d " "); '
        f'openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 '
        f'-subj /CN=stub -keyout /dev/null -out /dev/stdout -set_serial "0x$(( n + 10 ))" 2>/dev/null')
    stub.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = exec ]; then cat > "{tmp}/stdin.$$"; {answer}; fi\n'
    )
    stub.chmod(0o755)
    return stub


def _run(tmp: Path, hosts: dict, extra: dict | None = None) -> subprocess.CompletedProcess:
    inv = {"all": {"hosts": {h: {"ansible_connection": "local", **v} for h, v in hosts.items()}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    play = [{"hosts": "all", "gather_facts": False, "vars": extra or {}, "tasks": _lifted()}]
    (tmp / "play.yml").write_text(yaml.safe_dump(play))
    return harness_sandbox.run(["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml")],
                               tmp, cwd=playbook_yaml.REPO, env=harness_sandbox.env_for(tmp))


def _leaf(tmp: Path, **over) -> dict:
    return {"name": "caddy", "host": "consumer", "dir": str(tmp / "certs"), "profile": "client",
            "sans": ["vm01.caddy.dc1.example.internal", "caddy.dc1.example.internal"], **over}


def _consumer(tmp: Path, leaves: list, **over) -> dict:
    return {**SITE, "internal_leaves": leaves, "_mint_name": "caddy", "container_engine": str(_stub(tmp)), **over}


# ── Task 4.2: the declared-name guard ──────────────────────────────────────────

@pytest.mark.parametrize("over,ok", [
    ({}, True),
    ({"name": "other"}, False),                                   # not declared by that name
    ({"host": "somewhere-else"}, False),                          # declared for another host
    ({"profile": "both"}, False),
    ({"dir": "relative/certs"}, False),
    ({"dir": "/tmp/$(id)"}, False),                               # review of #363: shell injection
    ({"dir": '/tmp/a"b'}, False),
    ({"dir": "/tmp/certs/"}, False),                              # review of #369: trailing slash
    ({"sans": ["*.dc1.example.internal"]}, False),                 # a wildcard
    ({"sans": ["caddy.dc2.example.internal"]}, False),            # another site
    ({"sans": ["evildc1.example.internal"]}, False),              # a suffix, not a subdomain
    ({"sans": []}, False),
], ids=["declared", "undeclared", "other-host", "bad-profile", "relative-dir", "shell-dir", "quote-dir", "slash-dir",
         "wildcard",
         "other-site",
        "suffix-only", "no-sans"])
def test_only_a_leaf_declared_for_this_host_reaches_the_ca(tmp_path, over, ok):
    r = _run(tmp_path, {"consumer": _consumer(tmp_path, [_leaf(tmp_path, **over)])})
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    sent = list(tmp_path.glob("stdin.*"))
    assert bool(sent) is ok, "the CA received a request for a refused leaf" if sent else "nothing was signed"


# ── Task 4.1: issuance, the swap, and where the key goes ───────────────────────

def test_the_key_stays_on_the_consumer_and_current_points_at_the_new_serial(tmp_path):
    r = _run(tmp_path, {"consumer": _consumer(tmp_path, [_leaf(tmp_path)])})
    assert r.returncode == 0, r.stdout + r.stderr
    certs = tmp_path / "certs"
    current = certs / "current"
    assert current.is_symlink() and not Path(current.readlink()).is_absolute()
    assert (current / "cert.pem").read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert (current / "key.pem").stat().st_mode & 0o777 == 0o600
    # What crossed to the CA: the issuer password line, then the request, and no key.
    sent = next(tmp_path.glob("stdin.*")).read_text()
    assert sent.startswith("test-issuer-pw\n-----BEGIN CERTIFICATE REQUEST-----")
    assert "PRIVATE KEY" not in sent
    assert not list(certs.glob(".pending*"))


def test_a_second_run_keeps_the_certificate_and_a_reissue_keeps_one_previous(tmp_path):
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "not-a-serial").mkdir()  # something else in the directory is never pruned
    host = _consumer(tmp_path, [_leaf(tmp_path)])
    assert _run(tmp_path, {"consumer": host}).returncode == 0
    first = (certs / "current").readlink()
    r = _run(tmp_path, {"consumer": host})
    assert r.returncode == 0 and (certs / "current").readlink() == first
    assert "current certificate kept" in r.stdout
    serials = [first]
    for _ in range(2):
        r = _run(tmp_path, {"consumer": {**host, "_mint_reissue": True}})
        assert r.returncode == 0, r.stdout + r.stderr
        serials.append((certs / "current").readlink())
    assert len(set(serials)) == 3
    kept = sorted(p.name for p in certs.iterdir() if p.is_dir() and not p.is_symlink())
    # The newest and the one before it; the first is pruned; unrelated entries stay.
    assert kept == sorted([str(serials[1]), str(serials[2]), "not-a-serial"])


def test_a_refused_request_fails_the_run_with_the_cas_reason(tmp_path):
    host = {**_consumer(tmp_path, [_leaf(tmp_path)]), "container_engine": str(_stub(tmp_path, refuse=True))}
    r = _run(tmp_path, {"consumer": host})
    assert r.returncode != 0
    assert "does not match the policy" in r.stdout
    assert "test-issuer-pw" not in r.stdout + r.stderr
    assert not (tmp_path / "certs" / "current").exists()
    assert not list((tmp_path / "certs").glob(".pending*")), "the unused key was left behind"


def test_a_lock_left_by_a_killed_run_does_not_block(tmp_path):
    # The lock is a kernel flock, released when its holder dies: a leftover lock file from
    # a killed run holds nothing.
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / ".placement.lock").write_text("")
    r = _run(tmp_path, {"consumer": {**_consumer(tmp_path, [_leaf(tmp_path)]), "_mint_lock_wait": 1}})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "certs" / "current" / "cert.pem").exists()


def test_a_pending_key_from_a_killed_run_is_swept_after_an_hour(tmp_path):
    import os
    old = tmp_path / "certs" / ".pending.killed"
    old.mkdir(parents=True)
    (old / "key.pem").write_text("left by a killed run")
    past = old.stat().st_mtime - 7200
    os.utime(old, (past, past))
    fresh = tmp_path / "certs" / ".pending.running"
    fresh.mkdir()
    assert _run(tmp_path, {"consumer": _consumer(tmp_path, [_leaf(tmp_path)])}).returncode == 0
    assert not old.exists() and fresh.exists()


def test_a_live_holder_blocks_the_placement_until_the_wait_runs_out(tmp_path):
    (tmp_path / "certs").mkdir()
    holder = subprocess.Popen(
        ["python3", "-c", "import fcntl, sys, time; f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); "
         "print('held', flush=True); time.sleep(60)", str(tmp_path / "certs" / ".placement.lock")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        r = _run(tmp_path, {"consumer": {**_consumer(tmp_path, [_leaf(tmp_path)]), "_mint_lock_wait": 2}})
        assert r.returncode != 0 and "another issuance holds" in r.stdout + r.stderr
        assert not (tmp_path / "certs" / "current").exists()
        assert not list((tmp_path / "certs").glob(".pending*")), "the unused key was left behind"
    finally:
        holder.kill()


def test_check_mode_reads_and_reports_but_writes_nothing(tmp_path):
    host = _consumer(tmp_path, [_leaf(tmp_path)])
    inv = {"all": {"hosts": {"consumer": {"ansible_connection": "local", **host}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp_path / "play.yml").write_text(yaml.safe_dump([{"hosts": "all", "gather_facts": False,
                                                          "tasks": _lifted()}]))
    r = harness_sandbox.run(["ansible-playbook", "--check", "-i", str(tmp_path / "inv.yml"),
                             str(tmp_path / "play.yml")], tmp_path, cwd=playbook_yaml.REPO,
                            env=harness_sandbox.env_for(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "check mode: nothing is written" in r.stdout
    assert not list(tmp_path.glob("stdin.*")) and not (tmp_path / "certs").exists()


def test_the_signing_and_password_tasks_are_hidden_and_nothing_else_is():
    hidden = {t["name"] for t in playbook_yaml.tasks(playbook_yaml.load(TASKS)) if t.get("no_log") is True}
    assert hidden == {"Read the issuing provisioner's password",
                      "Sign the request on the CA host with issuer-{{ _leaf.profile }}"}
    assert json.dumps(playbook_yaml.load(TASKS)).count("delegate_to") == 1


def test_a_symlinked_leaf_directory_is_refused_before_anything_reaches_the_ca(tmp_path):
    # Review of #369: the placement changes into this directory.
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "certs").symlink_to(target)
    r = _run(tmp_path, {"consumer": _consumer(tmp_path, [_leaf(tmp_path)])})
    assert r.returncode != 0 and "is a symbolic link" in r.stdout, r.stdout
    assert not list(tmp_path.glob("stdin.*")) and not list(target.iterdir())
