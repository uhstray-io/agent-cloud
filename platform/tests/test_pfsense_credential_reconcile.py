"""Protect the pfSense backup-to-OpenBao mismatch gate."""

from pathlib import Path

import yaml
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment


def test_reconcile_refuses_a_different_live_key_before_merging():
    playbook = Path(__file__).resolve().parents[1] / "playbooks/reconcile-pfsense-api-key.yml"
    tasks = yaml.safe_load(playbook.read_text())[0]["tasks"]
    block = next(task["block"] for task in tasks if "block" in task)
    names = [task["name"] for task in block]
    assert names.index("Preserve a different live key pending verification") < names.index(
        "Seed the missing key through the shared KV-v2 merge task"
    )

    facts = next(
        task["ansible.builtin.set_fact"]
        for task in block
        if task["name"] == "Classify the current key without displaying it"
    )
    guard = next(
        task["ansible.builtin.assert"]["that"]
        for task in block
        if task["name"] == "Preserve a different live key pending verification"
    )
    env = NativeEnvironment(undefined=StrictUndefined)
    for current, allowed in ((None, True), ("candidate", True), ("different", False)):
        response = {"json": {"data": {"data": {"api_key": current}}}} if current else {"json": {}}
        context = {"_pfsense_current": response, "_pfsense_key": "candidate"}
        context.update({name: env.from_string(expr).render(**context) for name, expr in facts.items()})
        assert env.compile_expression(guard)(**context) is allowed
