"""o11y-fault-drill.yml (inference-telemetry-production tasks 2.5, 3.5, 3.6).

Runnable cases execute the real playbook from a throwaway clean git copy of
platform/playbooks (the drill refuses a dirty or different controller revision), with a
fake container engine on PATH that records every call. They prove the refusals and that
check mode induces nothing. Structural cases pin the restore-in-always contract and the
Semaphore template. All values are synthetic.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/o11y-fault-drill.yml"

FAKE_ENGINE = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_ENGINE_LOG"
case "$*" in
  *provisioning/alert-rules/*) echo '{"isPaused": false}' ;;
  *) echo '{}' ;;
esac
"""


@pytest.fixture(scope="module")
def clean_copy(tmp_path_factory):
    """A committed, clean copy: the drill's revision gate then passes for real."""
    root = tmp_path_factory.mktemp("drill-repo")
    shutil.copytree(ROOT / "platform/playbooks", root / "platform/playbooks")
    shutil.copy(ROOT / "ansible.cfg", root / "ansible.cfg")
    if (ROOT / "callback_plugins").is_dir():
        shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "drill"]):
        subprocess.run(argv, cwd=root, env=env, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env, check=True,
                         capture_output=True, text=True).stdout.strip()
    return root, sha


def run(tmp_path, clean_copy, extra, *, check=False, exporter_host="o11y-test", other_hosts=None):
    root, sha = clean_copy
    bindir = tmp_path / "bin"
    bindir.mkdir()
    engine = bindir / "podman"
    engine.write_text(FAKE_ENGINE)
    engine.chmod(0o755)
    log = tmp_path / "engine.log"
    log.write_text("")
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(json.dumps({"all": {"children": {
        "o11y_svc": {"hosts": {"o11y-test": {
            "ansible_connection": "local",
            "ansible_python_interpreter": shutil.which("python3"),
            "monorepo_deploy_path": "platform/services/o11y/deployment",
            "dgx_spark_scrape_enabled": True,
            "dgx_spark_nodes": [{"name": "spark-test", "address": "192.0.2.10"}],
            "o11y_fault_drill_exporters": {"spark-test": {"host": exporter_host, "unit": "node-exporter.service"}},
        }}},
        "dgx": {"hosts": other_hosts or {}},
    }}}))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("OPENBAO_ADDR", "BAO_ROLE_ID", "BAO_SECRET_ID")}
    env.update(PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}", FAKE_ENGINE_LOG=str(log),
               ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
    result = subprocess.run(
        ["ansible-playbook", *(["--check"] if check else []), "-i", str(inventory),
         "platform/playbooks/o11y-fault-drill.yml", "-e", json.dumps({"expected_repository_sha": sha, **extra})],
        cwd=root, env=env, text=True, capture_output=True, timeout=180, stdin=subprocess.DEVNULL,
    )
    return result.returncode, result.stdout + result.stderr, log.read_text().splitlines()


@pytest.mark.parametrize("extra", [
    {"drill": "grafana"},
    {"drill": "grafana", "confirm_fault_drill": "probe"},
    {"drill": "wipe", "confirm_fault_drill": "wipe"},
])
def test_refuses_without_matching_confirmation(tmp_path, clean_copy, extra):
    rc, out, calls = run(tmp_path, clean_copy, extra)
    assert rc != 0
    assert "no fault was induced" in out
    assert calls == []


def test_exporter_refused_outside_a_window(tmp_path, clean_copy):
    rc, out, calls = run(tmp_path, clean_copy, {
        "drill": "exporter", "confirm_fault_drill": "exporter", "drill_node": "spark-test"})
    assert rc != 0
    assert "drill_window_confirmed=true" in out
    assert calls == []


def test_exporter_refuses_an_undeclared_node(tmp_path, clean_copy):
    rc, out, calls = run(tmp_path, clean_copy, {
        "drill": "exporter", "confirm_fault_drill": "exporter", "drill_node": "spark-other",
        "drill_window_confirmed": True})
    assert rc != 0
    assert "No fault was induced" in out
    assert calls == []


def test_check_mode_reads_but_induces_nothing(tmp_path, clean_copy):
    rc, out, calls = run(tmp_path, clean_copy, {
        "drill": "exporter", "confirm_fault_drill": "exporter", "drill_node": "spark-test",
        "drill_window_confirmed": True}, check=True)
    assert rc == 0, out
    assert "check mode: drill=exporter would stop node-exporter.service" in out
    # Only the read of the rule ran; no stop/start/run of anything.
    assert calls and all(c.startswith("exec o11y-grafana") for c in calls), calls
    assert any("alert-rules/inference_target_down" in c for c in calls)
    assert "systemd" not in " ".join(c for c in calls)


# ---- structural ----------------------------------------------------------------------

def _fault_block():
    plays = playbook_yaml.load(PLAYBOOK)
    tasks = plays[2]["tasks"]
    blocks = [t for t in tasks if "block" in t]
    assert len(blocks) == 1
    return blocks[0]


def test_every_fault_is_inside_the_one_guarded_block():
    block = _fault_block()
    assert block["when"] == "not ansible_check_mode"
    plays = playbook_yaml.load(PLAYBOOK)
    outside = [t for t in plays[2]["tasks"] if "block" not in t]
    for task in outside:
        text = json.dumps(task)
        assert "systemd_service" not in text and "lineinfile" not in text
        assert '"stop"' not in text and '"start"' not in text


@pytest.mark.parametrize(("fault", "restore"), [
    ("Stop the declared DGX node exporter", "Restart the DGX node exporter"),
    ("Point the probe at a model the server does not serve", "Restore the deployed probe environment"),
    ("Stop Grafana", "Start Grafana"),
])
def test_each_fault_has_its_restore_in_always(fault, restore):
    block = _fault_block()
    assert fault in [t["name"] for t in block["block"]]
    assert restore in [t["name"] for t in block["always"]]
    # The restore must not be skipped by restore_only or by the fault's own success.
    task = next(t for t in block["always"] if t["name"] == restore)
    assert "_restore_only" not in str(task.get("when", ""))


def test_exporter_restore_is_idempotent_start():
    block = _fault_block()
    task = next(t for t in block["always"] if t["name"] == "Restart the DGX node exporter")
    assert task["ansible.builtin.systemd_service"]["state"] == "started"


def test_template_is_dev_bound_and_asks_for_confirmation():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    found = [t for t in templates if t["playbook"] == "platform/playbooks/o11y-fault-drill.yml"]
    assert len(found) == 1
    tpl = found[0]
    assert tpl["repository"] == "agent-cloud dev"
    names = {v["name"]: v for v in tpl["survey_vars"]}
    for required in ("expected_repository_sha", "drill", "confirm_fault_drill"):
        assert names[required]["required"] is True
    assert names["drill_window_confirmed"]["default_value"] == "false"


def test_render_proof_uses_throwaway_state_and_always_cleans_up():
    text = (ROOT / "scripts/o11y-render-proof.sh").read_text()
    assert "trap cleanup EXIT" in text
    assert 'rm -f -v "$NAME"' in text
    # Anonymous volume, unique name, no published port, never the stack's container names.
    assert "-v /var/lib/grafana" in text
    assert 'NAME="o11y-render-proof-$RUN_ID"' in text
    assert "--publish" not in text and " -p 3" not in text and "--network" not in text and "o11y-grafana" not in text
    make = (ROOT / "Makefile").read_text()
    assert "local-o11y-render-proof:" in make and "scripts/o11y-render-proof.sh" in make


# ---- PR #433 review: unreachable, --diff, vacuous resolve, unverified restore ----------

def _play_tasks():
    return playbook_yaml.load(PLAYBOOK)[2]["tasks"]


def test_unreachable_cannot_skip_the_restore():
    # Ansible docs (playbooks_blocks): unreachable hosts do not trigger rescue/always.
    block = _fault_block()
    assert block["ignore_unreachable"] is True
    registers = [t["register"] for t in block["always"]
                 if any(k.endswith(("systemd_service", "copy", "command")) for k in t)]
    assert len(registers) == 4, registers
    gate = block["always"][-1]
    assert gate["name"] == "Require every restore to have reached its host"
    that = gate["ansible.builtin.assert"]["that"]
    for name in registers:
        assert name in that
    assert "drill_restore_only=true" in gate["ansible.builtin.assert"]["fail_msg"]
    # Last in always: it runs whether the block failed or not, after every restore.


def test_every_task_touching_the_probe_env_hides_it():
    def walk(tasks):
        for t in tasks:
            yield t
            for key in ("block", "always", "rescue"):
                yield from walk(t.get(key, []))
    touching = [t for t in walk(_play_tasks()) if "block" not in t
                and ("_probe_env }}" in json.dumps(t) or "_probe_env_backup" in json.dumps(t))]
    assert len(touching) >= 5
    for t in touching:
        assert t.get("diff") is False, t["name"]
        module = next(k for k in t if k.startswith("ansible.builtin."))
        if module.split(".")[-1] in ("copy", "lineinfile", "file"):
            assert t.get("no_log") is True, t["name"]


def test_probe_resolve_gate_matches_every_drill_model_not_this_runs():
    gate = next(t for t in _play_tasks() if t.get("name") == "Require the drill alert resolved")
    assert "_drill_model_prefix" in gate["until"]
    assert "_drill_model.stdout" not in gate["until"]
    assert "'match'" in gate["until"]


def test_probe_restore_requires_a_fresh_success_sample():
    task = next(t for t in _play_tasks()
                if t.get("name") == "Require a successful probe sample for the deployed model after the restore")
    query = task["vars"]["_probe_restored_query"]
    assert "inference_probe_success" in query and "== 1" in query
    assert "inference_probe_last_run_timestamp_seconds" in query and "_restored_at" in query
    assert "drill == 'probe'" in task["when"]
    assert "_restore_only" not in task["when"]


def test_unreachable_exporter_host_still_runs_the_restore_and_fails_loudly(tmp_path, clean_copy):
    # TEST-NET-1, never routed: the stop AND the restart are unreachable. Without
    # ignore_unreachable the always section would be skipped (Ansible docs).
    unreachable = {"spark-host": {"ansible_host": "192.0.2.1", "ansible_connection": "ssh",
                                  "ansible_ssh_common_args": "-o ConnectTimeout=1 -o BatchMode=yes"}}
    rc, out, _ = run(tmp_path, clean_copy, {
        "drill": "exporter", "confirm_fault_drill": "exporter", "drill_node": "spark-test",
        "drill_window_confirmed": True}, exporter_host="spark-host", other_hosts=unreachable)
    assert rc != 0
    assert "TASK [Restart the DGX node exporter]" in out
    assert "A restore step could not reach its host" in out
