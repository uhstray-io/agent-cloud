"""Synthetic contract tests for the read-only launch-contract diagnostic."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/diagnose-o11y-launch-contract.py"
PLAYBOOK = ROOT / "platform/playbooks/diagnose-o11y-launch-contract.yml"
SPEC = importlib.util.spec_from_file_location("o11y_launch_contract", HELPER)
DIAGNOSTIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTIC)


def test_diagnostic_reports_only_local_categories_and_leaves_prerequisites_unverified(monkeypatch):
    monkeypatch.setattr(DIAGNOSTIC.os, "geteuid", lambda: 501)
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: __import__("io").BytesIO(b"0::/private/container-id\n"),
    )

    report = DIAGNOSTIC.diagnose()

    assert report["status"] == "unverified"
    assert report["local_observations"]["probe_process_uid"]["value"] == "nonroot"
    assert report["local_observations"]["probe_process_cgroup_mode"]["value"] == "v2"
    assert set(report["launch_prerequisites"].values()) == {"unverified"}
    assert report["actions"] == {
        "api_requests": "not_called",
        "network_requests": "not_called",
        "target_probes": "not_called",
        "writes": "not_called",
    }
    assert "private/container-id" not in json.dumps(report)
    assert "authenticated" not in json.dumps(report)
    assert "permit" not in json.dumps(report)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"x" * (DIAGNOSTIC.MAX_CGROUP_BYTES + 1), "probe_oversized"),
        (b"", "probe_missing"),
        (b"not-a-cgroup-row /private/path\n", "probe_malformed"),
        (None, "probe_unavailable"),
    ],
)
def test_unusable_cgroup_input_is_refused_without_raw_values(monkeypatch, raw, reason):
    monkeypatch.setattr(DIAGNOSTIC.os, "geteuid", lambda: 0)

    def open_cgroup(*_args, **_kwargs):
        if raw is None:
            raise PermissionError("private /path and 77123")
        return __import__("io").BytesIO(raw)

    monkeypatch.setattr("builtins.open", open_cgroup)

    report = DIAGNOSTIC.diagnose()

    observation = report["local_observations"]["probe_process_cgroup_mode"]
    assert observation == {
        "status": "refused",
        "value": "unknown",
        "reason": reason,
    }
    encoded = json.dumps(report)
    assert "private /path" not in encoded
    assert "77123" not in encoded
    assert "/private/path" not in encoded


def test_invalid_arguments_return_fixed_unverified_report(monkeypatch, capsys):
    monkeypatch.setattr(DIAGNOSTIC, "_local_observations", lambda: (_ for _ in ()).throw(AssertionError()))

    assert DIAGNOSTIC.main(["caller-selected-id"]) == 0

    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["status"] == "unverified"
    assert "caller-selected-id" not in output
    assert set(report["launch_prerequisites"].values()) == {"unverified"}


def test_playbook_is_dev_bound_local_only_and_skips_probe_in_check_mode():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    matching = [item for item in templates if item["name"] == "Diagnose o11y Launch Contract (Dev)"]

    assert len(matching) == 1
    assert matching[0]["repository"] == "agent-cloud dev"
    assert matching[0]["playbook"] == "platform/playbooks/diagnose-o11y-launch-contract.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert plays[-1]["hosts"] == "localhost"
    assert plays[-1]["connection"] == "local"
    tasks = plays[-1]["tasks"]
    probe = next(task for task in tasks if task["name"] == "Read bounded local process categories")
    check_mode = next(task for task in tasks if task["name"] == "Report check mode without running the local probe")
    assert probe["ansible.builtin.command"]["argv"] == ["python3", "-"]
    assert probe["when"] == "not ansible_check_mode"
    assert probe["no_log"] is True
    assert check_mode["when"] == "ansible_check_mode"
    assert check_mode["ansible.builtin.debug"]["msg"]["reason"] == (
        "uid_cgroup_probe_skipped_checkout_verification_reads_code"
    )
    checkout_guard = yaml.safe_load(
        (ROOT / "platform/playbooks/require-reviewed-checkout.yml").read_text()
    )[0]
    checkout_check = next(
        task
        for task in checkout_guard["tasks"]
        if task["name"] == "Compare checkout bytes with the reviewed commit"
    )
    assert checkout_check["check_mode"] is False
    assert "lookup('ansible.builtin.file'" in checkout_check[
        "ansible.builtin.command"
    ]["stdin"]
    assert "check_mode: false" not in json.dumps(tasks)
    assert "ansible.builtin.uri" not in json.dumps(tasks)
    assert "ansible.builtin.shell" not in json.dumps(tasks)
    assert "SEMAPHORE_JWT" not in PLAYBOOK.read_text()
