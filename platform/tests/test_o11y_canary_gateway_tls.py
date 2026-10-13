"""The o11y trace canary reaches the gateway the way the listener is configured.

With `agw_listener_tls` the gateway listener only speaks TLS, so the canary's plain-HTTP request
was answered with a TLS alert record and all 100 requests failed (Verify o11y Service, task 5250).
The canary now builds its URL the way probe-agentgateway-gate.yml does: the server leaf's SAN with
TLS, the published bind without it, and presents the verifier leaf and the internal trust bundle.
"""

import json
import os
import subprocess
from pathlib import Path

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


def _play():
    return next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if p.get("name") == PLAY_NAME)


def _task(prefix):
    return next(t for t in _play()["tasks"] if t.get("name", "").startswith(prefix))


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
