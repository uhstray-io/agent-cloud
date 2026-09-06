"""Incident evidence stays bounded, read-only and distinct from recovery."""

import ast
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

PLATFORM = Path(__file__).resolve().parents[4]
MODULE = runpy.run_path(str(PLATFORM / "playbooks/files/discovery-diagnostics.py"))
COLLECT = MODULE["collect"]
WINDOW = {"runtime": "docker", "since": "2026-04-23T00:00:00Z", "until": "2026-04-24T00:00:00Z"}


def test_collection_reads_mounted_config_and_never_reports_recovery(tmp_path):
    config = tmp_path / "actual-mounted.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "orb": {
                    "secrets_manager": {"active": "vault", "secret_id": "synthetic-secret"},
                    "policies": {
                        "worker": {
                            "pfsense_sync": {
                                "config": {"schedule": "*/15 * * * *", "timeout": 600, "password": "synthetic-secret"}
                            }
                        }
                    },
                }
            }
        )
    )
    commands = []

    def read(argv):
        commands.append(argv)
        if argv[1] == "inspect":
            return json.dumps(
                [
                    {
                        "State": {"Status": "running"},
                        "Image": "sha256:" + "a" * 64,
                        "Created": "2026-04-22T00:00:00Z",
                        "Config": {"Env": ["synthetic-secret"]},
                        "Mounts": [{"Destination": "/opt/orb/agent.yaml", "Source": str(config)}],
                    }
                ]
            ).encode(), b""
        if argv[1] == "logs":
            return (
                b"2026-04-23T07:45:01.123680757Z Policy pfsense_sync: Successfully ingested 16 entities "
                b"synthetic-secret\n"
            ), b""
        assert argv == [
            "docker",
            "exec",
            "--env",
            "PGOPTIONS=-c default_transaction_read_only=on",
            "netbox-netbox-1",
            "/opt/netbox/netbox/manage.py",
            "shell",
            "--interface",
            "python",
            "-c",
            MODULE["DB_QUERY"],
        ]
        return json.dumps(dict.fromkeys(MODULE["COUNTS"], 3)).encode(), b""

    with patch.dict(COLLECT.__globals__, read_command=read):
        result = COLLECT(WINDOW)
    names = [
        "netbox-orb-agent",
        "netbox-netbox-1",
        "netbox-diode-ingester-1",
        "netbox-diode-reconciler-1",
        "netbox-diode-auth-1",
    ]
    assert commands[:-1] == [
        command
        for name in names
        for command in (
            ["docker", "inspect", name],
            ["docker", "logs", "--timestamps", "--since", WINDOW["since"], "--until", WINDOW["until"], name],
        )
    ]
    assert result["baseline_complete"] is True
    assert result["pipeline_status"] == "unknown"
    mounted = result["components"]["orb-agent"]["config"]
    assert mounted["sources"]["pfsense_sync"]["schedule"] == "*/15 * * * *"
    assert mounted["sources"]["snmp_discovery"]["declared"] is False
    assert mounted["loaded_by_running_agent"] == "unverified"
    assert result["database_counts"]["sites_missing_gps"] == 3
    assert "synthetic-secret" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    with patch.dict(MODULE["main"].__globals__, collect=lambda _: result), patch("sys.stdin", io.StringIO("{}")):
        assert MODULE["main"]() == 3


def test_partial_collection_auth_retry_and_timezones_do_not_become_success():
    result = MODULE["log_summary"](
        b"2026-04-23T03:45:00-04:00 [proxmox-discovery] Skipping VMs/LXC on offline node synthetic-secret\n"
        b"2026-04-23T07:30:00.430316078Z Retrying ingestion due to UNAUTHENTICATED error\n"
        b"2026-04-23T07:45:01Z Policy proxmox_discovery: Successfully ingested 18 entities\n"
        b"unzoned synthetic-secret\n"
    )
    signals = {(r["source"], r["signal"]): r for r in result["signals"]}
    assert signals["proxmox_discovery", "partial_collection"]["first"] == "2026-04-23T03:45:00-04:00"
    assert signals["unattributed", "authentication_rejected"]["count"] == 1
    assert signals["proxmox_discovery", "submission_message"]["count"] == 1
    assert result["first_retained_in_window"] == "2026-04-23T07:30:00.430316078Z"
    assert result["unparsed_lines"] == 1
    assert result["historical_completeness"] == "unverified"
    assert "synthetic-secret" not in json.dumps(result)


def test_failed_reads_remain_visible_without_raw_exceptions():
    def fail(_argv):
        raise RuntimeError("synthetic-secret")

    with patch.dict(COLLECT.__globals__, read_command=fail):
        result = COLLECT(WINDOW)
    assert result["baseline_complete"] is False
    assert result["database_error"] == "read_only_query_failed"
    assert all(row["logs_error"] == "log_read_failed" for row in result["components"].values())
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        {"runtime": "podman"},
        {"runtime": "docker; restart"},
        {"since": "2026-04-23T00:00:00"},
        {"since": WINDOW["until"]},
        {"until": "2026-04-22T00:00:00Z"},
    ],
)
def test_invalid_scope_refuses_before_any_host_command(change):
    with (
        patch.dict(COLLECT.__globals__, read_command=lambda _: pytest.fail("host read attempted")),
        pytest.raises((ValueError, MODULE["ReadFailure"])),
    ):
        COLLECT(WINDOW | change)


def test_reader_bounds_real_subprocess_output_and_runtime():
    read = MODULE["read_command"]
    assert read([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]) == (
        b"out\n",
        b"err\n",
    )
    with (
        patch.dict(read.__globals__, LIMIT=256),
        pytest.raises(MODULE["ReadFailure"], match="output_limit_exceeded"),
    ):
        read([sys.executable, "-c", "import sys; sys.stderr.write('x' * 257)"])
    with pytest.raises(MODULE["ReadFailure"], match="command_timeout"):
        read([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
    with pytest.raises(MODULE["ReadFailure"], match="command_failed"):
        read([sys.executable, "-c", "raise RuntimeError('synthetic-secret')"])


def test_database_query_has_a_closed_read_only_call_surface():
    tree = ast.parse(MODULE["DB_QUERY"])
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    attributes = {node.func.attr for node in calls if isinstance(node.func, ast.Attribute)}
    assert {node.func.id for node in calls if isinstance(node.func, ast.Name)} == {"Q", "RuntimeError", "print"}
    assert all(isinstance(node.func, (ast.Name, ast.Attribute)) for node in calls)
    assert attributes == {"atomic", "cursor", "execute", "fetchone", "count", "filter", "set_rollback", "dumps"}
    sql = [node.args[0].value for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "execute"]
    assert sql == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '15s'", "SHOW transaction_read_only"]
    assert any(isinstance(node, ast.Raise) for node in ast.walk(tree))
    # Changing the guard or introducing a setter fails this closed call allowlist.
    playbook = yaml.safe_load((PLATFORM / "playbooks/check-discovery.yml").read_text())
    includes = [
        task["ansible.builtin.include_tasks"]
        for play in playbook
        for task in play["tasks"]
        if "ansible.builtin.include_tasks" in task
    ]
    assert includes == ["tasks/resolve-become-password.yml"]
    assert playbook[0]["any_errors_fatal"] is True
    assert "ansible.builtin.fail" in playbook[-1]["tasks"][-1]


def test_config_dependency_failure_is_named_and_does_not_hide_logs(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("orb: {policies: {}}")

    def read(argv):
        if argv[1] == "inspect":
            return json.dumps([{"State": {"Status": "running"}, "Image": "a" * 64,
                                "Created": WINDOW["since"], "Mounts": [{
                                    "Source": str(config), "Destination": "/opt/orb/agent.yaml"}]}]).encode(), b""
        if argv[1] == "logs":
            return b"2026-04-23T07:45:00Z retained message\n", b""
        return json.dumps(dict.fromkeys(MODULE["COUNTS"], 0)).encode(), b""

    with patch.dict(sys.modules, yaml=None), patch.dict(COLLECT.__globals__, read_command=read):
        result = COLLECT(WINDOW)
    orb = result["components"]["orb-agent"]
    assert orb["config_error"] == "config_yaml_unavailable"
    assert "metadata_error" not in orb
    assert orb["logs"]["line_count"] == 1
    assert result["baseline_complete"] is False


def test_revision_preflight_accepts_clean_commit_and_refuses_wrong_or_dirty_source(tmp_path):
    """Execute only the controller preflight, with a disposable local Git fixture."""
    ansible = shutil.which("ansible-playbook")
    assert ansible, "ansible-core is a required CI test dependency"
    repo = tmp_path / "repo"
    repo.mkdir()
    query = yaml.safe_load((PLATFORM / "playbooks/check-discovery.yml").read_text())[0]
    commands = [task["ansible.builtin.command"] for task in query["tasks"] if "ansible.builtin.command" in task]
    assert commands[0]["argv"] == ["git", "rev-parse", "HEAD"]
    assert commands[1]["argv"][:4] == ["git", "status", "--porcelain", "--"]
    paths = commands[1]["argv"][4:]
    assert set(paths) == {
        "platform/playbooks/check-discovery.yml", "platform/playbooks/files/discovery-diagnostics.py",
        "platform/playbooks/tasks/resolve-become-password.yml", "platform/playbooks/tasks/assert-bao-transport.yml",
    }
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n")
    for argv in (["git", "init"], ["git", "add", "."],
                 ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Test", "-c",
                  "user.email=test@example.com", "commit", "-m", "fixture"]):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    for command in commands:
        command["chdir"] = str(repo)
    play = tmp_path / "preflight.yml"
    play.write_text(yaml.safe_dump([query]))
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("netbox_svc:\n  hosts:\n    fixture: {}\n")
    extra = {"discovery_log_since": WINDOW["since"], "discovery_log_until": WINDOW["until"]}

    def preflight(expected):
        return subprocess.run(
            [ansible, "-i", str(inventory), str(play), "-e",
             json.dumps(extra | {"discovery_expected_revision": expected})],
            capture_output=True, text=True, timeout=30,
            env=os.environ | {"ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible")},
        )

    assert preflight(revision).returncode == 0
    refused = preflight("a" * 40)
    assert refused.returncode != 0 and "discovery_revision_mismatch" in refused.stdout
    (repo / paths[1]).write_text("changed\n")
    refused = preflight(revision)
    assert refused.returncode != 0 and "discovery_revision_mismatch" in refused.stdout
