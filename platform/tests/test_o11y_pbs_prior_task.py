from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import playbook_yaml
import pytest
from ansible_python import ansible_python

PLAYBOOK = Path(__file__).resolve().parents[1] / "playbooks" / "tasks" / "read-o11y-pbs-prior-task.yml"
TEXT = PLAYBOOK.read_text()
TASKS = playbook_yaml.load(PLAYBOOK)
INPUT_CHECK = "Require the fixed PBS task ID and a known receipt contract"
TRANSPORT = "Refuse public cleartext OpenBao access for PBS task authentication"
LOOKUP = "Read Semaphore task API credentials from OpenBao"
TRIM = "Take the Semaphore base URL without a trailing slash (hidden: it comes from the secret read)"
URL_CHECK = "Require secret/services/semaphore to hold an https url (hidden: names the requirement only)"
TOKEN_CHECK = "Require secret/services/semaphore to hold an api_token (hidden: names the requirement only)"

# (stored value, endpoint the shipped trim must yield, whether the shipped https check accepts it)
CASES = (
    ("https://s.example.test", "https://s.example.test", True),
    ("https://s.example.test/", "https://s.example.test", True),
    ("https://s.example.test//", "https://s.example.test", True),
    (" https://s.example.test:3000/ ", "https://s.example.test:3000", True),
    ("http://s.example.test", "http://s.example.test", False),
    ("https://s.example.test/api", "https://s.example.test/api", False),
)

# Renders the SHIPPED trim fact and the SHIPPED url assert through Ansible's Templar, as the precedent in
# test_agentgateway_runtime_proof.py does. Runs in the interpreter ansible-playbook runs under (ansible_python).
HARNESS = r"""
import json
import sys

from ansible._internal._datatag._tags import TrustedAsTemplate
from ansible.module_utils._internal._datatag import AnsibleTagHelper
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

request = json.loads(sys.stdin.read())
loader = DataLoader()
tasks = loader.load_from_file(request["playbook"])
trim = next(t for t in tasks if t.get("name") == request["trim_name"])
check = next(t for t in tasks if t.get("name") == request["url_name"])
trim_expression = trim["ansible.builtin.set_fact"]["_pbs_task_semaphore_url"]
check_expression = check["ansible.builtin.assert"]["that"][0]
rows = []
for stored in request["stored"]:
    templar = Templar(
        loader=loader, variables={"_pbs_task_semaphore_secret": {"url": stored, "api_token": "synthetic"}}
    )
    trimmed = templar.template(AnsibleTagHelper.tag(trim_expression, TrustedAsTemplate()))
    templar = Templar(loader=loader, variables={"_pbs_task_semaphore_url": str(trimmed)})
    verdict = templar.template(
        AnsibleTagHelper.tag("{{ 'accepted' if (" + check_expression + ") else 'refused' }}", TrustedAsTemplate())
    )
    rows.append([stored, str(trimmed), str(verdict)])
print("RESULT " + json.dumps(rows))
"""


def _named(name):
    found = [t for t in TASKS if t.get("name") == name]
    assert len(found) == 1, (name, len(found))
    return found[0]


def _index(name):
    return TASKS.index(_named(name))


def _temp_root():
    return "/private/tmp" if Path("/private/tmp").is_dir() else tempfile.gettempdir()


@pytest.fixture(scope="module")
def rendered():
    python = ansible_python()
    request = {
        "playbook": str(PLAYBOOK),
        "trim_name": TRIM,
        "url_name": URL_CHECK,
        "stored": [stored for stored, _, _ in CASES],
    }
    with tempfile.TemporaryDirectory(prefix="pbs-prior-templar-", dir=_temp_root()) as scratch:
        env = os.environ.copy() | {"ANSIBLE_LOCAL_TEMP": scratch, "ANSIBLE_REMOTE_TEMP": scratch}
        result = subprocess.run(
            [python, "-c", HARNESS],
            input=json.dumps(request),
            cwd=scratch,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    assert result.returncode == 0, result.stderr[-2000:]
    line = next(x for x in result.stdout.splitlines() if x.startswith("RESULT "))
    return {stored: (trimmed, verdict) for stored, trimmed, verdict in json.loads(line[len("RESULT ") :])}


def test_the_trim_and_both_checks_are_named_hidden_and_single_expression():
    trim = _named(TRIM)
    assert trim["no_log"] is True
    assert set(trim["ansible.builtin.set_fact"]) == {"_pbs_task_semaphore_url"}
    for name in (URL_CHECK, TOKEN_CHECK):
        check = _named(name)
        assert check["no_log"] is True
        assert check["ansible.builtin.assert"]["quiet"] is True
        assert len(check["ansible.builtin.assert"]["that"]) == 1


def test_the_visible_input_check_precedes_the_secret_read_and_the_secret_checks_precede_requests():
    order = [INPUT_CHECK, TRANSPORT, LOOKUP, TRIM, URL_CHECK, TOKEN_CHECK]
    positions = [_index(name) for name in order]
    assert positions == sorted(positions)
    assert _named(INPUT_CHECK).get("no_log") is not True
    first_request = min(i for i, t in enumerate(TASKS) if "ansible.builtin.uri" in t)
    assert positions[-1] < first_request


def test_the_raw_stored_url_is_read_only_inside_the_trim():
    assert TEXT.count("_pbs_task_semaphore_secret.url") == 1


def test_both_requests_go_to_the_trimmed_url():
    uris = [t["ansible.builtin.uri"]["url"] for t in TASKS if "ansible.builtin.uri" in t]
    assert len(uris) == 2
    assert all(url.startswith("{{ _pbs_task_semaphore_url }}/api/project/") for url in uris), uris


@pytest.mark.parametrize(("stored", "endpoint", "accepted"), CASES)
def test_the_shipped_trim_and_https_check_accept_and_refuse_each_stored_form(rendered, stored, endpoint, accepted):
    trimmed, verdict = rendered[stored]
    assert trimmed == endpoint
    assert verdict == ("accepted" if accepted else "refused")
