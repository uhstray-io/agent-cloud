"""Verify o11y Service can require a Loki line of one signal.

`{service="agentgateway"}` matches both the gateway's OTLP access logs and its span logs
(signal="span"), so a recent line of either proves nothing about the other. `expected_log_signal`
adds `,signal="<x>"` to the selector; empty leaves it exactly `{service="<svc>"}`.
"""

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-o11y-service.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
QUERY_PLAY = "Verify the named service is collected"
VALIDATE_PLAY = "Require a declared o11y receiver and a service identity"

pytestmark = needs_ansible


def _play(name):
    return next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if p.get("name") == name)


def _task(play, prefix):
    return next(t for t in play["tasks"] if t.get("name", "").startswith(prefix))


def _run(tmp_path, harness, check=True):
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump(harness))
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("[o11y_svc]\nlocalhost ansible_connection=local\n")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(["ansible-playbook", "-i", str(inventory), str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=check)


def _selector(tmp_path, extra):
    """Render the real Loki query task's URL and return the decoded `query` parameter."""
    play = _play(QUERY_PLAY)
    argv = _task(play, "Query recent Loki logs")["ansible.builtin.command"]["argv"]
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {**play["vars"], "expected_service": "agentgateway", **extra},
        "tasks": [{"ansible.builtin.debug": {"msg": "QUERY " + argv[-1]}}],
    }]
    done = _run(tmp_path, harness)
    line = next(ln for ln in done.stdout.splitlines() if '"msg": "QUERY ' in ln)
    url = json.loads(line.split('"msg": ', 1)[1]).split("QUERY ", 1)[1]
    return parse_qs(urlsplit(url).query)["query"][0]


def test_no_signal_leaves_the_selector_unchanged(tmp_path):
    assert _selector(tmp_path, {}) == '{service="agentgateway"}'
    assert _selector(tmp_path, {"expected_log_signal": ""}) == '{service="agentgateway"}'


def test_a_signal_is_added_to_the_selector(tmp_path):
    assert _selector(tmp_path, {"expected_log_signal": "span"}) == '{service="agentgateway",signal="span"}'


def _validate(tmp_path, signal):
    task = _task(_play(VALIDATE_PLAY), "Validate verification inputs")
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"expected_service": "agentgateway", "expected_log_signal": signal},
        "tasks": [task],
    }]
    return _run(tmp_path, harness, check=False)


def test_a_malformed_signal_is_refused(tmp_path):
    assert _validate(tmp_path, "span").returncode == 0
    assert _validate(tmp_path, "").returncode == 0
    for bad in ['Span"x', "span,x", "1span", 'a"}|{b']:
        done = _validate(tmp_path, bad)
        assert done.returncode != 0, bad
        assert "expected_log_signal" in done.stdout, bad


def test_the_survey_offers_the_optional_signal():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    survey = next(t for t in templates if t["name"] == "Verify o11y Service")["survey_vars"]
    entry = next(v for v in survey if v["name"] == "expected_log_signal")
    assert entry["type"] == "string" and entry["required"] is False and entry["default_value"] == ""


def test_the_failure_and_report_messages_name_the_signal(tmp_path):
    play = _play(QUERY_PLAY)
    assert "_signal_label" in _task(play, "Require a recent service log line")["ansible.builtin.assert"]["fail_msg"]
    assert "_signal_label" in _task(play, "Report verified signals")["ansible.builtin.debug"]["msg"]
    labels = []
    for extra in ({}, {"expected_log_signal": "span"}):
        harness = [{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {**play["vars"], **extra},
            "tasks": [{"ansible.builtin.debug": {"msg": "LABEL[{{ _signal_label }}]"}}],
        }]
        done = _run(tmp_path, harness)
        line = next(ln for ln in done.stdout.splitlines() if '"msg": "LABEL[' in ln)
        labels.append(line.split("LABEL[")[1].split("]")[0])
    assert labels == ["", " (signal span)"]
