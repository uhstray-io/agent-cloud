"""Harden SSH's access-harden step result under --check: skip, never a false pass.

A dry run withdraws nothing. Recording `pass` would claim password auth was rejected when
it was only simulated (Semaphore task 2847 did exactly that). The record play follows
apply-firewall's fw-harden convention: what a real run would still change is reported and
the step is `skip`; `pass` only when the host already meets every criterion; `fail` on a
real error such as a failed key-only pre-proof. A real run's semantics are unchanged.

The record play is run as-is, behind a play that plants the facts the host play gathers.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None,
                                reason="ansible-playbook not installed")

PROVEN = {"stdout": "KEY_ONLY_OK"}
HARDENED = {
    "_harden_verdict": True, "_key_preproof": PROVEN, "_pw_problems": [],
    "_sudoers_change": {"changed": False},
    "_sshd_main_change": {"results": [{"changed": False, "item": {"line": "PasswordAuthentication no"}}]},
    "_sshd_dropin_change": {"results": []},
}
SOFT = {
    "_harden_verdict": True, "_key_preproof": PROVEN,
    "_pw_problems": ["server offers password"],
    "_sudoers_change": {"changed": True},
    "_sshd_main_change": {"results": [{"changed": True, "item": {"line": "PasswordAuthentication no"}}]},
    "_sshd_dropin_change": {"results": [
        {"changed": True, "item": [{"path": "/x/50-cloud-init.conf"}, {"line": "KbdInteractiveAuthentication no"}]}]},
}
PREPROOF_FAILED = {"_harden_verdict": True,
                   "_harden_error": "Probe a fresh key-only login before hardening: failed",
                   "_pw_problems": ["unproven"]}


def _record(tmp_path: Path, facts: dict, check: bool):
    plays = yaml.safe_load((PLAYBOOKS / "harden-ssh.yml").read_text())
    plant = {"name": "plant", "hosts": "demo_svc", "gather_facts": False, "tasks": [
        {"ansible.builtin.set_fact": {"{{ item.key }}": "{{ item.value }}"},
         "loop": "{{ _fx | dict2items }}", "check_mode": False}]}
    (tmp_path / "tasks").symlink_to(PLAYBOOKS / "tasks")
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([plant, plays[-1]], sort_keys=False))
    inv = tmp_path / "inv.ini"
    inv.write_text("[demo_svc]\nh1 ansible_connection=local service_name=demo\n")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inv), str(pb), *(["--check"] if check else []),
         "-e", "target_service=demo_svc", "-e", json.dumps({"_fx": facts})],
        cwd=REPO, env=env, text=True, capture_output=True, check=False)
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, proc.stdout + proc.stderr
    return proc, found[0]


def test_check_mode_on_a_soft_host_is_skip_with_what_would_change(tmp_path):
    proc, r = _record(tmp_path, SOFT, check=True)
    assert proc.returncode == 0, proc.stdout
    assert r["status"] == "skip" and r["check_mode"] is True
    assert r["evidence"]["key_only_proven"] is True
    assert r["evidence"]["password_rejected"] is False
    assert "would withdraw password auth (server offers password)" in proc.stdout
    assert "would write the NOPASSWD sudoers entry" in proc.stdout
    assert "PasswordAuthentication no" in proc.stdout
    assert "KbdInteractiveAuthentication no" in proc.stdout


def test_check_mode_is_skip_when_only_sudoers_would_change(tmp_path):
    proc, r = _record(tmp_path, {**HARDENED, "_sudoers_change": {"changed": True}}, check=True)
    assert r["status"] == "skip", proc.stdout


def test_check_mode_passes_only_when_already_hardened(tmp_path):
    proc, r = _record(tmp_path, HARDENED, check=True)
    assert proc.returncode == 0, proc.stdout
    assert r["status"] == "pass"
    assert ": would " not in proc.stdout


def test_check_mode_fails_on_a_failed_preproof(tmp_path):
    proc, r = _record(tmp_path, PREPROOF_FAILED, check=True)
    assert proc.returncode != 0
    assert r["status"] == "fail" and "key-only" in r["error"]


def test_a_real_run_passes_on_proof_even_though_it_changed_state(tmp_path):
    proc, r = _record(tmp_path, {**HARDENED, "_sudoers_change": {"changed": True}}, check=False)
    assert proc.returncode == 0, proc.stdout
    assert r["status"] == "pass" and r["check_mode"] is False


def test_a_real_run_still_fails_when_password_is_not_rejected(tmp_path):
    proc, r = _record(tmp_path, SOFT, check=False)
    assert proc.returncode != 0
    assert r["status"] == "fail" and "server offers password" in r["error"]


def test_check_mode_is_skip_when_only_the_main_sshd_config_would_change(tmp_path):
    facts = {**HARDENED, "_sshd_main_change": {"results": [
        {"changed": True, "item": {"line": "PermitRootLogin no"}}]}}
    proc, r = _record(tmp_path, facts, check=True)
    assert r["status"] == "skip", proc.stdout
    assert "would set sshd PermitRootLogin no" in proc.stdout


def test_check_mode_is_skip_when_only_a_dropin_would_change(tmp_path):
    facts = {**HARDENED, "_sshd_dropin_change": {"results": [
        {"changed": True, "item": [{"path": "/x/50-cloud-init.conf"}, {"line": "PasswordAuthentication no"}]}]}}
    proc, r = _record(tmp_path, facts, check=True)
    assert r["status"] == "skip", proc.stdout
    assert "would set sshd PasswordAuthentication no" in proc.stdout


SENTINEL = "SENTINEL-must-not-print-7f3a"


def test_registered_contents_and_diffs_never_reach_the_output(tmp_path):
    # Only setting lines and fixed phrases are reported; a registered result's content or
    # diff (which could carry file bytes) must never be echoed.
    leak = {"content": SENTINEL, "diff": {"before": SENTINEL, "after": SENTINEL}}
    facts = {
        **SOFT,
        "_sudoers_change": {"changed": True, **leak},
        "_sshd_main_change": {"results": [
            {"changed": True, "item": {"line": "PasswordAuthentication no"}, **leak}]},
        "_sshd_dropin_change": {"results": [
            {"changed": True, "item": [{"path": "/x/a.conf"}, {"line": "KbdInteractiveAuthentication no"}], **leak}]},
    }
    proc, r = _record(tmp_path, facts, check=True)
    assert r["status"] == "skip", proc.stdout
    # The plant play echoes its loop items; judge only the record play's output.
    record = proc.stdout.split("PLAY [Record the access-harden step result]", 1)[1]
    assert SENTINEL not in record
    assert SENTINEL not in json.dumps(r)
