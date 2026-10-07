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

# Deterministic unreachable host: Ansible's ssh connection treats exit 255 as
# UNREACHABLE. Real network routing to a TEST-NET address differs between machines.
FAKE_SSH = """#!/bin/sh
echo "ssh: connect to host spark-host port 22: Connection timed out" >&2
exit 255
"""

FAKE_ENGINE = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_ENGINE_LOG"
case "$*" in
  *provisioning/alert-rules/*) echo '{"isPaused": false, "title": "DGX Spark scrape target down"}' ;;
  *) echo '{}' ;;
esac
"""


@pytest.fixture(scope="module")
def clean_copy(tmp_path_factory):
    """A committed, clean copy: the drill's revision gate then passes for real."""
    root = tmp_path_factory.mktemp("drill-repo")
    # Bytecode is excluded and ignored: ansible-playbook compiles the filter and callback
    # plugins on first use, and an untracked __pycache__ makes the drill's clean-checkout
    # gate refuse every later run. A developer tree that already holds compiled files
    # hid this; a fresh CI checkout does not (CI run 37259691735).
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", "*.retry")
    shutil.copytree(ROOT / "platform/playbooks", root / "platform/playbooks", ignore=skip)
    shutil.copy(ROOT / "ansible.cfg", root / "ansible.cfg")
    if (ROOT / "callback_plugins").is_dir():
        shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins", ignore=skip)
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n*.retry\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "drill"]):
        subprocess.run(argv, cwd=root, env=env, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env, check=True,
                         capture_output=True, text=True).stdout.strip()
    return root, sha


def run(tmp_path, clean_copy, extra, *, check=False, exporter_host="o11y-test", other_hosts=None,
        expect_dirty=False, engine_script=FAKE_ENGINE):
    root, sha = clean_copy
    bindir = tmp_path / "bin"
    bindir.mkdir()
    engine = bindir / "podman"
    engine.write_text(engine_script)
    engine.chmod(0o755)
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
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
               PYTHONDONTWRITEBYTECODE="1", ANSIBLE_SSH_RETRIES="0",
               ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
    result = subprocess.run(
        ["ansible-playbook", *(["--check"] if check else []), "-i", str(inventory),
         "platform/playbooks/o11y-fault-drill.yml", "-e", json.dumps({"expected_repository_sha": sha, **extra})],
        cwd=root, env=env, text=True, capture_output=True, timeout=180, stdin=subprocess.DEVNULL,
    )
    out = result.stdout + result.stderr
    # Every case must get past the revision gate; a dirty copy would make each refusal
    # pass for the wrong reason.
    if not expect_dirty:
        assert "Controller checkout has uncommitted files" not in out, out
    return result.returncode, out, log.read_text().splitlines()


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
    plays = playbook_yaml.plays(PLAYBOOK)
    tasks = plays[2]["tasks"]
    blocks = [t for t in tasks if "block" in t]
    assert len(blocks) == 1
    return blocks[0]


def test_every_fault_is_inside_the_one_guarded_block():
    block = _fault_block()
    assert block["when"] == "not ansible_check_mode"
    plays = playbook_yaml.plays(PLAYBOOK)
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
    return playbook_yaml.plays(PLAYBOOK)[2]["tasks"]


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
    # The stop AND the restart are unreachable. Without
    # ignore_unreachable the always section would be skipped (Ansible docs).
    # The fake ssh on PATH exits 255 for every connection: unreachable on any machine.
    unreachable = {"spark-host": {"ansible_host": "spark-host.invalid", "ansible_connection": "ssh",
                                  "ansible_ssh_executable": "ssh"}}
    rc, out, _ = run(tmp_path, clean_copy, {
        "drill": "exporter", "confirm_fault_drill": "exporter", "drill_node": "spark-test",
        "drill_window_confirmed": True}, exporter_host="spark-host", other_hosts=unreachable)
    assert rc != 0
    assert "TASK [Restart the DGX node exporter]" in out
    assert "A restore step could not reach its host" in out
    assert "UNREACHABLE" in out


def test_dirty_checkout_is_refused_before_any_fault(tmp_path, clean_copy):
    root, _ = clean_copy
    stray = root / "platform/playbooks/uncommitted-drill-edit.yml"
    stray.write_text("# not reviewed\n")
    try:
        rc, out, calls = run(tmp_path, clean_copy, {
            "drill": "exporter", "confirm_fault_drill": "exporter", "drill_node": "spark-test",
            "drill_window_confirmed": True}, expect_dirty=True)
    finally:
        stray.unlink()
    assert rc != 0
    assert "Controller checkout has uncommitted files" in out
    assert "PLAY [Induce one bounded o11y fault" not in out
    assert calls == []


# ── probe drill: where "health up" is read ──────────────────────────────────

PROBE_REFUSAL = "Require the synthetic probe deployed and its health route declared"
HEALTH_HOLD = "Hold the invalid target while /health stays up"
PUBLIC_URL = "https://inference.example.test/v1"
UPSTREAM = "http://192.0.2.30:8000/v1"


def _probe_wiring(tmp_path, o11y=None, gateways=1, upstream=UPSTREAM):
    """Run the drill's own probe refusal on the o11y host, then report where /health is read."""
    play = playbook_yaml.plays(PLAYBOOK)[2]
    names = ["_restore_only", "_probe_gateway_identity", "_probe_gw", "_probe_upstream", "_probe_health_url",
             "_probe_health_from"]
    refusal = next(t for t in play["tasks"] if t.get("name") == PROBE_REFUSAL)
    harness = [{"hosts": "o11y_svc", "gather_facts": False, "vars": {k: play["vars"][k] for k in names},
                "tasks": [refusal, {"ansible.builtin.debug": {
                    "msg": "HEALTH {{ {'url': _probe_health_url, 'from': _probe_health_from} | to_json }}"}}]}]
    gw = {"agw_upstream_base_url": upstream} if upstream is not None else {}
    o11y_vars = {"ansible_connection": "local", "ansible_python_interpreter": shutil.which("python3"),
                 "drill": "probe", "o11y_inference_probe_enabled": True, "o11y_inference_probe_url": PUBLIC_URL,
                 "o11y_inference_probe_model": "served-model-a", **(o11y or {})}
    o11y_vars = {k: v for k, v in o11y_vars.items() if v is not None}
    inventory = {"all": {"children": {
        "o11y_svc": {"hosts": {"o11y-test": o11y_vars}},
        "agentgateway_svc": {"hosts": {f"gw{i}": gw for i in range(gateways)}},
    }}}
    (tmp_path / "pb.yml").write_text(yaml.safe_dump(harness))
    (tmp_path / "inv.yml").write_text(json.dumps(inventory))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_STDOUT_CALLBACK="default")
    done = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "pb.yml")],
                          cwd=ROOT, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    line = next((ln for ln in done.stdout.splitlines() if "HEALTH " in ln), None)
    got = json.loads(json.loads(line.split('"msg": ', 1)[1]).split("HEALTH ", 1)[1]) if line else None
    return done, got


def test_public_path_reads_health_through_the_probe_url_from_the_runner(tmp_path):
    done, got = _probe_wiring(tmp_path, gateways=0, upstream=None)
    assert done.returncode == 0, done.stdout + done.stderr
    assert got == {"url": "https://inference.example.test/health", "from": "localhost"}


def test_gateway_identity_reads_the_upstreams_health_from_the_o11y_host(tmp_path):
    # The gateway-identity probe URL names the gateway's mutual-TLS listener, which resolves
    # only on the o11y host and serves no /health: task 2981 failed 24/24 on exactly that.
    done, got = _probe_wiring(tmp_path, o11y={
        "o11y_inference_probe_client_leaf": "o11y-probe",
        "o11y_inference_probe_url": "https://gateway.lab.example.test:4000/v1"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert got == {"url": "http://192.0.2.30:8000/health", "from": "o11y-test"}


@pytest.mark.parametrize("gateways,upstream,why", [
    (0, None, "no gateway to read the upstream from"),
    (2, UPSTREAM, "two gateways"),
    (1, None, "gateway without a declared upstream"),
    (1, "http://192.0.2.30:8000", "upstream that is not a /v1 base URL"),
])
def test_gateway_identity_without_one_declared_upstream_is_refused(tmp_path, gateways, upstream, why):
    done, got = _probe_wiring(tmp_path, o11y={"o11y_inference_probe_client_leaf": "o11y-probe"},
                              gateways=gateways, upstream=upstream)
    assert done.returncode != 0, why
    assert "No fault was induced" in done.stdout, why
    assert got is None, why


@pytest.mark.parametrize("gateways,upstream", [(0, None), (2, UPSTREAM), (1, None), (1, "http://192.0.2.30:8000")])
def test_a_restore_only_run_is_never_gated_on_the_health_hold_inputs(tmp_path, gateways, upstream):
    # Restore must not wait on anything it does not need: the gateway upstream feeds only the
    # health hold, which a restore-only run never performs.
    done, _ = _probe_wiring(tmp_path, o11y={"o11y_inference_probe_client_leaf": "o11y-probe",
                                            "drill_restore_only": True},
                            gateways=gateways, upstream=upstream)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("o11y,why", [
    ({"o11y_inference_probe_enabled": False}, "probe disabled since the fault"),
    ({"o11y_inference_probe_url": None}, "no probe URL"),
    ({"o11y_inference_probe_url": "http://inference.example.test/v1"}, "a URL the probe would refuse"),
])
def test_a_restore_only_run_is_gated_only_on_what_the_restore_uses(tmp_path, o11y, why):
    restore = {**o11y, "drill_restore_only": True}
    done, _ = _probe_wiring(tmp_path, o11y=restore, gateways=0, upstream=None)
    assert done.returncode == 0, (why, done.stdout + done.stderr)
    # The same inventory still refuses a run that would induce the fault.
    done, _ = _probe_wiring(tmp_path, o11y=o11y, gateways=0, upstream=None)
    assert done.returncode != 0, why
    assert "No fault was induced" in done.stdout, why


def test_a_restore_only_run_still_needs_the_model_its_proof_reads(tmp_path):
    done, _ = _probe_wiring(tmp_path, o11y={"drill_restore_only": True, "o11y_inference_probe_model": None},
                            gateways=0, upstream=None)
    assert done.returncode != 0
    assert "No fault was induced" in done.stdout


def test_the_health_hold_is_read_from_the_path_aware_host():
    def walk(tasks):
        for t in tasks:
            yield t
            for key in ("block", "rescue", "always"):
                yield from walk(t.get(key, []))
    task = next(t for t in walk(_play_tasks()) if t.get("name") == HEALTH_HOLD)
    assert task["delegate_to"] == "{{ _probe_health_from }}"
    assert task["ansible.builtin.uri"]["url"] == "{{ _probe_health_url }}"
    assert task["ansible.builtin.uri"]["status_code"] == [200]


# ── alert identity: the firing proof and the Discord receipt name THIS mode's alert ──
# PR #465 review: the probe check matched any firing alert carrying the drill model, so a
# different rule firing for that model passed it. Each case evaluates the playbook's own
# expression with synthetic Grafana and Discord payloads.

PROBE_TITLE = "Synthetic inference probe failing"
EXPORTER_TITLE = "DGX Spark scrape target down"
DRILL_MODEL = "o11y-drill-absent-model-0123456789ab"
WEBHOOK = "900000000000000001"


def _walk(tasks):
    for t in tasks:
        yield t
        for key in ("block", "rescue", "always"):
            yield from _walk(t.get(key, []))


def _task(name):
    return next(t for t in _walk(_play_tasks()) if t.get("name") == name)


def _evaluate(tmp_path, expr, facts):
    """True when the playbook expression `expr` holds for `facts`, with the play's own vars."""
    play_vars = playbook_yaml.load(PLAYBOOK)[2]["vars"]
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {**play_vars, **facts},
                "tasks": [{"ansible.builtin.debug": {"msg": "VERDICT {{ (" + expr + ") | bool }}"}}]}]
    (tmp_path / "pb.yml").write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_LOCALHOST_WARNING="0",
               ANSIBLE_INVENTORY_UNPARSED_WARNING="0")
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(tmp_path / "pb.yml"),
                           "-e", f"ansible_python_interpreter={shutil.which('python3')}"],
                          cwd=ROOT, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout + done.stderr
    if "VERDICT True" in done.stdout:
        return True
    assert "VERDICT False" in done.stdout, done.stdout
    return False


def _alerts(*alerts):
    return {"rc": 0, "stdout": json.dumps({"data": {"alerts": [
        {"labels": labels, "state": "Alerting"} for labels in alerts]}})}


@pytest.mark.parametrize(("labels", "want"), [
    ({"alertname": PROBE_TITLE, "model_name": DRILL_MODEL}, True),
    # Same model, another rule: the staleness alert carries model_name too.
    ({"alertname": "Synthetic inference probe has not reported", "model_name": DRILL_MODEL}, False),
    ({"model_name": DRILL_MODEL}, False),
    ({"alertname": PROBE_TITLE, "model_name": "served-model-a"}, False),
])
def test_probe_firing_proof_requires_the_rule_and_the_model(tmp_path, labels, want):
    task = _task("Require inference_probe_failing firing for the drill model")
    facts = {"drill": "probe", "_probe_alerts": _alerts(labels), "_drill_alertname": PROBE_TITLE,
             "_drill_model": {"stdout": DRILL_MODEL}}
    assert _evaluate(tmp_path, task["until"], facts) is want


@pytest.mark.parametrize(("labels", "want"), [
    ({"alertname": EXPORTER_TITLE, "node": "spark-test", "job": "dgx-spark-node"}, True),
    # Same node and job, another rule (the memory guard alerts carry both).
    ({"alertname": "DGX Spark node free memory below the memory guard floor", "node": "spark-test",
      "job": "dgx-spark-node"}, False),
    ({"node": "spark-test", "job": "dgx-spark-node"}, False),
    ({"alertname": EXPORTER_TITLE, "node": "spark-other", "job": "dgx-spark-node"}, False),
])
def test_exporter_firing_proof_requires_the_rule_and_the_node(tmp_path, labels, want):
    task = _task("Wait for inference_target_down to fire for the stopped node")
    facts = {"drill": "exporter", "drill_node": "spark-test", "_exporter_alerts": _alerts(labels),
             "_drill_alertname": EXPORTER_TITLE}
    assert _evaluate(tmp_path, task["until"], facts) is want


def _contact_message(firing, resolved=()):
    """The agent-cloud-ops contact point's content: marker lines + Grafana's default.message."""
    def alerts(entries):
        return "".join(
            "\nValue: B=0\nLabels:\n" + "".join(f" - {k} = {v}\n" for k, v in sorted(e.items()))
            + "Annotations:\n - summary = synthetic\n" for e in entries)
    head = "".join(f"o11y-delivery-status=firing service={e['service']}\n" for e in firing) + "\n"
    body = ("**Firing**\n" + alerts(firing) if firing else "") + (
        "\n\n**Resolved**\n" + alerts(resolved) if resolved else "")
    return head + body


def _probe_alert(alertname=PROBE_TITLE, model=DRILL_MODEL):
    return {"alertname": alertname, "model_name": model, "service": "vllm", "cluster": "dgx-spark",
            "environment": "prod", "grafana_folder": "agent-cloud", "severity": "critical"}


def _receipt(tmp_path, drill, contents, webhook=WEBHOOK):
    task = _task("Wait for the matching Discord message from the alert webhook")
    facts = {"drill": drill, "_webhook_id": WEBHOOK, "_drill_alertname": PROBE_TITLE,
             "_drill_model": {"stdout": DRILL_MODEL},
             "_discord_messages": {"json": [{"webhook_id": webhook, "content": c} for c in contents]}}
    return _evaluate(tmp_path, task["until"], facts)


def test_probe_receipt_names_the_rule_and_this_runs_model(tmp_path):
    assert _receipt(tmp_path, "probe", [_contact_message([_probe_alert()])]) is True


@pytest.mark.parametrize("contents", [
    # Another vllm rule's notification, same contact point line.
    [_contact_message([{**_probe_alert(alertname="vLLM metric families absent"), "model_name": "x"}])],
    # The probe rule, but for another model.
    [_contact_message([_probe_alert(model="served-model-a")])],
    # This run's model only in the Resolved section.
    [_contact_message([_probe_alert(model="served-model-a")], resolved=[_probe_alert()])],
    # The two halves split across two messages.
    [_contact_message([_probe_alert(model="served-model-a")]),
     _contact_message([{**_probe_alert(alertname="vLLM metric families absent")}])],
    # Labels right, but not through the contact point's firing line.
    [_contact_message([_probe_alert()]).replace("o11y-delivery-status=firing service=vllm", "")],
])
def test_probe_receipt_refuses_another_alerts_message(tmp_path, contents):
    assert _receipt(tmp_path, "probe", contents) is False


def test_probe_receipt_ignores_other_webhooks(tmp_path):
    assert _receipt(tmp_path, "probe", [_contact_message([_probe_alert()])], webhook="900000000000000002") is False


@pytest.mark.parametrize(("content", "want"), [
    ("o11y liveness watcher: grafana /api/health failed (status -1); prometheus datasource unhealthy "
     "(status -1) (https://grafana.example.test)", True),
    # The watcher posting for a reason that is not Grafana being down.
    ("o11y liveness watcher: watcher_token is not in secret/services/o11y; run Provision o11y Watcher Token "
     "(https://grafana.example.test)", False),
    ("o11y liveness watcher: prometheus datasource unhealthy (status 502) (https://grafana.example.test)", False),
])
def test_grafana_receipt_requires_the_grafana_health_failure(tmp_path, content, want):
    assert _receipt(tmp_path, "grafana", [content]) is want


def test_final_report_names_the_alert_read_from_grafana(tmp_path):
    play_vars = playbook_yaml.load(PLAYBOOK)[2]["vars"]
    expr = "_drill_proved[drill] is search('inference_probe_failing [(]Read Back Title[)] fired for model " \
           + DRILL_MODEL + "')"
    facts = {"drill": "probe", "_drill_rule_uid": "inference_probe_failing", "_drill_alertname": "Read Back Title",
             "_drill_model": {"stdout": DRILL_MODEL}}
    assert "_drill_alertname" in play_vars["_drill_proved"]["probe"]
    assert _evaluate(tmp_path, expr, facts) is True


def test_a_rule_without_a_title_is_refused_before_any_fault(tmp_path, clean_copy):
    untitled = FAKE_ENGINE.replace(', "title": "DGX Spark scrape target down"', "")
    rc, out, calls = run(tmp_path, clean_copy, {
        "drill": "exporter", "confirm_fault_drill": "exporter", "drill_node": "spark-test",
        "drill_window_confirmed": True}, check=True, engine_script=untitled)
    assert rc != 0
    assert "has no title; no fault was induced" in out
    assert all(c.startswith("exec o11y-grafana") for c in calls), calls
