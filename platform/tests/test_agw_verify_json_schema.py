"""The deploy verify proves `response_format` json_schema passes through the gateway.

service-deployment-workflow tasks 0.5 and 3.3: skynet's structured output depends on agentgateway
handing `response_format` to the upstream unchanged. The verify sends one schema-constrained
completion and requires a reply that parses against the schema. The assert task is read from
deploy-agentgateway.yml and run by ansible-playbook against synthetic responses, so the test runs
the deploy's own expression, not a copy.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/deploy-agentgateway.yml"
TASKS = [task for play in yaml.safe_load(PLAYBOOK.read_text()) for task in play.get("tasks", [])]
GATE = "agw_verify_json_schema | default(true) | bool"


def _task(name):
    matches = [task for task in TASKS if task.get("name") == name]
    assert len(matches) == 1, f"deploy-agentgateway.yml must have exactly one task named {name!r}"
    return matches[0]


PROBE = _task("An enrolled identity completes one schema-constrained request through the upstream")
KEEP = _task("Keyed probes: keep the schema-constrained response")
REQUIRE = _task("Require a reply that parses against the requested schema")


def test_request_asks_for_a_strict_json_schema():
    body = PROBE["vars"]["_agwp_body"]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["required"] == ["reply"] and schema["additionalProperties"] is False
    assert PROBE["vars"]["_agwp_path"] == "/v1/chat/completions"
    assert PROBE["vars"]["_agwp_method"] == "POST"


def test_runs_only_on_a_real_proven_keyed_verify():
    # Skipped under --check, without a verifying identity, after the gateway's own bucket refused
    # the plain completion, and when the operator turns it off.
    for task in (PROBE, KEEP, REQUIRE):
        assert "not ansible_check_mode" in task["when"], task["name"]
        assert "not (_verify_refused | bool)" in task["when"], task["name"]
        assert GATE in task["when"], task["name"]


def test_follows_the_plain_completion_gate():
    names = [task.get("name") for task in TASKS]
    assert names.index("Require a completion the upstream produced") < names.index(PROBE["name"])
    assert names.index(PROBE["name"]) < names.index(KEEP["name"]) < names.index(REQUIRE["name"])


def _reply(content, status=200):
    return {"status": status, "json": {"object": "chat.completion", "choices": [{"message": {"content": content}}]}}


def _run(tmp_path, response):
    playbook = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {"_keyed_schema": response, "_verify_client": "ws", "_verify_models": ["m"]},
            "tasks": [{k: v for k, v in REQUIRE.items() if k != "when"}],
        }
    ]
    path = tmp_path / "verify-under-test.yml"
    path.write_text(yaml.safe_dump(playbook, sort_keys=False))
    env = os.environ | {"ANSIBLE_LOCAL_TEMP": str(tmp_path), "ANSIBLE_NOCOLOR": "1"}
    result = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode


def test_a_reply_matching_the_schema_passes(tmp_path):
    assert _run(tmp_path, _reply(json.dumps({"reply": "pong"}))) == 0


def test_leading_whitespace_is_accepted(tmp_path):
    assert _run(tmp_path, _reply('\n {"reply": "pong"}')) == 0


@pytest.mark.parametrize(
    "response",
    [
        _reply("pong"),  # response_format dropped: free text
        _reply(json.dumps({"reply": "pong", "extra": 1})),  # schema not enforced
        _reply(json.dumps({"answer": "pong"})),  # wrong key
        _reply(json.dumps({"reply": 7})),  # wrong type
        _reply(json.dumps(["pong"])),  # not an object
        _reply(None),  # empty content (e.g. all tokens spent reasoning)
        _reply(json.dumps({"reply": "pong"}), status=429),
        {"status": 502, "json": {}},
    ],
    ids=["free-text", "extra-key", "wrong-key", "wrong-type", "array", "empty", "status-429", "no-choices"],
)
def test_anything_else_fails_the_deploy(tmp_path, response):
    assert _run(tmp_path, response) != 0
