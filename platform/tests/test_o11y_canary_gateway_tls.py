"""The o11y trace canary reaches the gateway the way the listener is configured.

With `agw_listener_tls` the gateway listener only speaks TLS, so the canary's plain-HTTP request
was answered with a TLS alert record and all 100 requests failed (Verify o11y Service, task 5250).
The canary now builds its URL the way probe-agentgateway-gate.yml does: the server leaf's SAN with
TLS, the published bind without it, and presents the verifier leaf and the internal trust bundle.
"""

import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-o11y-service.yml"
PLAY_NAME = "Emit an optional gateway trace canary"

TLS_VARS = {
    "agw_listener_tls": True,
    "agw_tls_server_name": "gateway.site.example.test",
    "agw_port": 4443,
    "agw_bind": "0.0.0.0",
}
PLAIN_VARS = {"agw_bind": "127.0.0.1"}

TRACE_ROLLOUT_RECEIVER_VARS = {
    "o11y_trace_rollout_enabled": True,
    "o11y_alerts_enabled": True,
    "o11y_metrics_receipt_id": "101",
    "o11y_alert_delivery_receipt_id": "102",
    "o11y_retention_receipt_id": "103",
    "o11y_cardinality_receipt_id": "104",
    "o11y_prom_retention": "15d",
    "o11y_prom_retention_size": "0B",
    "o11y_loki_retention": "7d",
    "o11y_tempo_retention": "7d",
    "o11y_scrape_sample_limit": 2000,
    "agentgateway_metrics_address": "127.0.0.1:9090",
}

TRACE_ROLLOUT_PREREQUISITES = tuple(TRACE_ROLLOUT_RECEIVER_VARS)
_ABSENT = object()


def _play():
    return next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if p.get("name") == PLAY_NAME)


def _task(prefix):
    return next(t for t in _play()["tasks"] if t.get("name", "").startswith(prefix))


def _module(task):
    return next((key for key in task if key.startswith("ansible.builtin.")), None)


def _safe_preflight_harness(tmp_path, *, receiver_vars=None, receiver_count=1,
                            verification_mode="legacy", emit=_ABSENT):
    """Run the real canary pre_tasks and shared gate with only local debug sentinels after them."""
    play = deepcopy(_play())
    receiver_vars = TRACE_ROLLOUT_RECEIVER_VARS.copy() if receiver_vars is None else receiver_vars
    hosts = {
        "gateway": {"ansible_connection": "local", "o11y_verification_mode": verification_mode},
    }
    if emit is not _ABSENT:
        hosts["gateway"]["emit_agentgateway_canary"] = emit
    receivers = {
        f"receiver{index + 1}": {"ansible_connection": "local", **receiver_vars}
        for index in range(receiver_count)
    }
    inventory = {
        "all": {"children": {
            "agentgateway_svc": {"hosts": {"gateway": hosts["gateway"]}},
            "o11y_svc": {"hosts": receivers},
        }},
    }
    harness_dir = tmp_path / "ansible-harness"
    task_dir = harness_dir / "tasks"
    task_dir.mkdir(parents=True)
    shutil.copy2(REPO / "platform/playbooks/tasks/assert-o11y-trace-rollout.yml",
                 task_dir / "assert-o11y-trace-rollout.yml")
    inventory_path = harness_dir / "inventory.yml"
    inventory_path.write_text(yaml.safe_dump(inventory))

    # The source play's pre_tasks (including its rollout gate once added) stay intact. These
    # sentinels replace the clock, stat, resolution, set_fact, and URI operations entirely.
    play["tasks"] = [
        {"name": "SAFE_PREFLIGHT_COMPLETE", "ansible.builtin.debug": {"msg": "SAFE_PREFLIGHT_COMPLETE"}},
        {"name": "Safe conditional canary operation", "ansible.builtin.debug": {
            "msg": "CANARY_OPERATION_REACHED",
        }, "when": "_canary_on"},
    ]
    safe_modules = {"ansible.builtin.assert", "ansible.builtin.debug", "ansible.builtin.include_tasks"}
    for task in play.get("pre_tasks", []) + play["tasks"]:
        assert _module(task) in safe_modules, task.get("name")
    gate = yaml.safe_load((task_dir / "assert-o11y-trace-rollout.yml").read_text())
    assert all(_module(task) == "ansible.builtin.assert" for task in gate)

    playbook_path = harness_dir / "preflight.yml"
    playbook_path.write_text(yaml.safe_dump([play]))
    ansible_home = harness_dir / "ansible-home"
    local_temp = harness_dir / "ansible-local"
    remote_temp = harness_dir / "ansible-remote"
    for directory in (ansible_home, local_temp, remote_temp):
        directory.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(ansible_home),
        "TMPDIR": str(tmp_path),
        "ANSIBLE_HOME": str(ansible_home),
        "ANSIBLE_LOCAL_TEMP": str(local_temp),
        "ANSIBLE_REMOTE_TEMP": str(remote_temp),
        "ANSIBLE_NOCOLOR": "1",
    }
    return subprocess.run(
        ["ansible-playbook", "-i", str(inventory_path), str(playbook_path)],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _assert_refused_before_canary(done, expected_message="Trace rollout is blocked"):
    assert done.returncode != 0, done.stdout + done.stderr
    assert expected_message in done.stdout
    assert "SAFE_PREFLIGHT_COMPLETE" not in done.stdout
    assert "CANARY_OPERATION_REACHED" not in done.stdout


def test_shared_trace_gate_is_guarded_before_the_existing_canary_tasks():
    play = _play()
    includes = [task for task in play.get("pre_tasks", [])
                if task.get("ansible.builtin.include_tasks") == "tasks/assert-o11y-trace-rollout.yml"]
    assert len(includes) == 1
    assert includes[0]["when"] == "_canary_on"
    assert play["gather_facts"] is False
    assert play["tasks"][0]["name"] == "Mark the canary start on the Tempo receiver clock"


@needs_ansible
def test_removed_shared_trace_gate_reaches_canary_and_breaks_refusal_contract(tmp_path, monkeypatch):
    play = deepcopy(_play())
    includes = [task for task in play.get("pre_tasks", [])
                if task.get("ansible.builtin.include_tasks") == "tasks/assert-o11y-trace-rollout.yml"]
    assert len(includes) == 1
    play["pre_tasks"].remove(includes[0])

    mutant_playbook = tmp_path / "verify-o11y-service-without-trace-gate.yml"
    mutant_playbook.write_text(yaml.safe_dump([play]))
    monkeypatch.setattr(sys.modules[__name__], "PLAYBOOK", mutant_playbook)

    done = _safe_preflight_harness(tmp_path, receiver_vars={}, emit="true")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SAFE_PREFLIGHT_COMPLETE" in done.stdout
    assert "CANARY_OPERATION_REACHED" in done.stdout
    with pytest.raises(AssertionError):
        _assert_refused_before_canary(done)


@needs_ansible
@pytest.mark.parametrize("prerequisite", TRACE_ROLLOUT_PREREQUISITES)
def test_legacy_emitting_canary_refuses_each_missing_receiver_prerequisite(tmp_path, prerequisite):
    receiver_vars = TRACE_ROLLOUT_RECEIVER_VARS.copy()
    receiver_vars.pop(prerequisite)
    done = _safe_preflight_harness(tmp_path, receiver_vars=receiver_vars, emit="true")
    _assert_refused_before_canary(done)


@needs_ansible
@pytest.mark.parametrize(("prerequisite", "invalid_value"), [
    ("o11y_trace_rollout_enabled", False),
    ("o11y_alerts_enabled", False),
    ("o11y_metrics_receipt_id", "0"),
    ("o11y_alert_delivery_receipt_id", "0"),
    ("o11y_retention_receipt_id", "0"),
    ("o11y_cardinality_receipt_id", "0"),
    ("o11y_prom_retention", "0d"),
    ("o11y_prom_retention_size", "0"),
    ("o11y_loki_retention", "0d"),
    ("o11y_tempo_retention", "0d"),
    ("o11y_scrape_sample_limit", 0),
    ("agentgateway_metrics_address", ""),
])
def test_legacy_emitting_canary_refuses_disabled_or_invalid_receiver_prerequisites(
        tmp_path, prerequisite, invalid_value):
    receiver_vars = TRACE_ROLLOUT_RECEIVER_VARS.copy()
    receiver_vars[prerequisite] = invalid_value
    done = _safe_preflight_harness(tmp_path, receiver_vars=receiver_vars, emit="true")
    _assert_refused_before_canary(done)


@needs_ansible
@pytest.mark.parametrize("receiver_count", [0, 2])
def test_legacy_emitting_canary_refuses_non_exact_receiver_count(tmp_path, receiver_count):
    done = _safe_preflight_harness(tmp_path, receiver_count=receiver_count, emit="true")
    _assert_refused_before_canary(
        done, expected_message="Trace rollout requires exactly one declared o11y receiver",
    )


@needs_ansible
def test_complete_receiver_only_prerequisites_allow_canary_progression(tmp_path):
    done = _safe_preflight_harness(tmp_path, emit="true")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SAFE_PREFLIGHT_COMPLETE" in done.stdout
    assert "CANARY_OPERATION_REACHED" in done.stdout


@needs_ansible
@pytest.mark.parametrize("emit", [_ABSENT, False, "false"])
def test_legacy_nonemitting_canary_skips_shared_trace_gate(tmp_path, emit):
    done = _safe_preflight_harness(tmp_path, receiver_vars={}, emit=emit)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SAFE_PREFLIGHT_COMPLETE" in done.stdout
    assert "CANARY_OPERATION_REACHED" not in done.stdout


@needs_ansible
@pytest.mark.parametrize("emit", [False, "false", "true"])
def test_strict_verification_skips_shared_trace_gate(tmp_path, emit):
    done = _safe_preflight_harness(tmp_path, receiver_vars={}, verification_mode="strict", emit=emit)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SAFE_PREFLIGHT_COMPLETE" in done.stdout
    assert "CANARY_OPERATION_REACHED" not in done.stdout


def _canary_url(tmp_path, host_vars):
    """Render the canary's real play vars, base-URL task and request URL for one inventory."""
    play = _play()
    request = _task("Send anonymous requests")["ansible.builtin.uri"]["url"]
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {**play["vars"], "emit_agentgateway_canary": "true", **host_vars},
        "tasks": [
            _task("The gateway base URL"),
            {"ansible.builtin.debug": {"msg": "URL " + request}},
        ],
    }]
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=True)
    line = next(ln for ln in done.stdout.splitlines() if '"msg": "URL ' in ln)
    return json.loads(line.split('"msg": ', 1)[1]).split("URL ", 1)[1]


@needs_ansible
def test_tls_listener_is_addressed_by_its_server_name(tmp_path):
    assert _canary_url(tmp_path, TLS_VARS) == "https://gateway.site.example.test:4443/v1/models"


@needs_ansible
def test_plain_listener_url_is_unchanged(tmp_path):
    assert _canary_url(tmp_path, PLAIN_VARS) == "http://127.0.0.1:4000/v1/models"


def test_request_presents_the_verifier_leaf_and_trusts_the_internal_ca():
    uri = _task("Send anonymous requests")["ansible.builtin.uri"]
    assert "_gateway_base" in uri["url"] and "http://" not in uri["url"]
    for key in ("client_cert", "client_key", "ca_path"):
        assert "if _tls else omit" in uri[key], key
    assert uri["client_cert"].endswith("/current/cert.pem') if _tls else omit }}")
    assert "validate_certs" not in uri
    assert _task("Send anonymous requests")["check_mode"] is False


def test_resolution_is_read_only_and_every_new_task_keeps_the_canary_gate():
    tasks = _play()["tasks"]
    resolution = _task("Resolve the server leaf's name")
    assert resolution["vars"] == {
        "_agwr_check_only": True,
        "_agwr_name": "{{ _server_name }}",
        "_agwr_ip": "{{ _canary_bind }}",
        "_agwr_expect": "",
        "_agwr_hosts_file": "/etc/hosts",
    }
    assert resolution["ansible.builtin.include_tasks"] == "tasks/agw-probe-resolution.yml"
    assert "_tls" in resolution["when"]
    assert "_canary_on" in _play()["vars"]
    for task in tasks:
        assert "_canary_on" in task["when"], task["name"]
    assert "legacy" in _play()["vars"]["_canary_on"] and "emit_agentgateway_canary" in _play()["vars"]["_canary_on"]


def test_tls_files_are_read_and_refused_when_missing():
    stat = _task("Read the verifier leaf and the trust bundle")
    assert stat["loop"] == [
        "{{ _leaf_dir }}/current/cert.pem",
        "{{ _leaf_dir }}/current/key.pem",
        "{{ _ca }}",
    ]
    assert stat["when"] == "_canary_on and _tls"
    refuse = _task("Refuse a verifier leaf or trust bundle")
    assert refuse["ansible.builtin.assert"]["that"] == [
        "_leaf_dir | length > 0",
        "_tls_files.results | rejectattr('stat.exists') | list | length == 0",
    ]
    assert refuse["when"] == "_canary_on and _tls"
