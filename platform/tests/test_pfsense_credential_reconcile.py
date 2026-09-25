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

    raw_fact = next(
        task["ansible.builtin.set_fact"]
        for task in block
        if task["name"] == "Classify the current key without displaying it"
    )
    facts = next(
        task["ansible.builtin.set_fact"]
        for task in block
        if task["name"] == "Classify the current key value without displaying it"
    )
    guard = next(
        task["ansible.builtin.assert"]["that"]
        for task in block
        if task["name"] == "Preserve a different live key pending verification"
    )
    env = NativeEnvironment(undefined=StrictUndefined)
    for current, allowed in ((None, True), ("", True), ("candidate", True), ("different", False)):
        response = (
            {"json": {"data": None}}
            if current is None
            else {"json": {"data": {"data": {"api_key": current}}}}
        )
        context = {"_pfsense_current": response, "_pfsense_key": "candidate"}
        context.update({name: env.from_string(expr).render(**context) for name, expr in raw_fact.items()})
        context.update({name: env.from_string(expr).render(**context) for name, expr in facts.items()})
        assert env.compile_expression(guard)(**context) is allowed


def test_reconcile_uses_visible_backup_check_and_version_guard():
    playbook = Path(__file__).resolve().parents[1] / "playbooks/reconcile-pfsense-api-key.yml"
    tasks = yaml.safe_load(playbook.read_text())[0]["tasks"]
    block = next(task["block"] for task in tasks if "block" in task)
    names = [task["name"] for task in block]
    assert names.index("Check the pfSense API key backup path") < names.index("Load the pfSense API key backup")
    assert names.index("Require the pfSense API key backup") < names.index("Load the pfSense API key backup")
    merge = next(task for task in block if task["name"] == "Seed the missing key through the shared KV-v2 merge task")
    assert merge["vars"]["_bm_guard_key"] == "api_key"

    helper = yaml.safe_load((playbook.parent / "tasks/bao-merge-keys.yml").read_text())
    guarded = next(task for task in helper if task["name"] == "Write a guarded update at the version just read")
    assert guarded["ansible.builtin.uri"]["body"]["options"]["cas"] == "{{ _bm_existing.json.data.metadata.version }}"
    assert any(task["name"] == "Refuse a concurrent guarded update" for task in helper)


def test_reservation_treats_deleted_or_null_key_as_absent():
    playbook = Path(__file__).resolve().parents[1] / "playbooks/netbox-allocate-ip.yml"
    tasks = yaml.safe_load(playbook.read_text())[0]["tasks"]
    facts = next(
        task["ansible.builtin.set_fact"]
        for task in tasks
        if task["name"] == "Classify the credential outcome (names and verdicts only)"
    )
    expr = facts["_pfsense_key_present"]
    env = NativeEnvironment(undefined=StrictUndefined)
    for response in ({"json": {"data": None}}, {"json": {"data": {"data": {"api_key": None}}}}):
        assert env.from_string(expr).render(_reserve=True, _pfsense_secret=response) is False
