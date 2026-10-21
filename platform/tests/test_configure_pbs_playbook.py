"""Fail-closed contracts for the Semaphore PBS service bootstrap."""

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]
PLAYBOOK = ROOT / "platform/playbooks/configure-pbs.yml"
DATASTORE_TASKS = ROOT / "platform/playbooks/tasks/configure-pbs-datastore.yml"


def host_tasks(path=PLAYBOOK):
    plays = yaml.safe_load(path.read_text())
    return next(play["tasks"] for play in plays if play.get("hosts") == "pbs_svc")


def task_index(tasks, fragment):
    return next(i for i, task in enumerate(tasks) if fragment in str(task))


def test_disk_identity_is_proven_before_package_or_datastore_changes():
    tasks = host_tasks()
    disk_proof = task_index(tasks, "assert_pbs_data_mount.py")
    package_install = task_index(tasks, "proxmox-backup-server")
    datastore_setup = task_index(tasks, "configure-pbs-datastore.yml")
    assert disk_proof < package_install < datastore_setup

    datastore = yaml.safe_load(DATASTORE_TASKS.read_text())
    assert task_index(datastore, "Re-prove the data mount immediately before datastore writes") < task_index(
        datastore, "Create the PBS datastore without a prune schedule"
    )


def test_repository_channel_has_no_default_and_no_subscription_requires_acceptance():
    text = PLAYBOOK.read_text()
    assert "pbs_repository_channel | default('')" in text
    assert "_pbs_repository_channel in ['enterprise', 'no-subscription']" in text
    assert "pbs_accept_no_subscription_risk | default(false) | bool" in text
    assert "pbs_accept_no_subscription_risk" in text.split("_pbs_private_names:", 1)[1].split("_pbs_forgeries:", 1)[0]


def test_enterprise_subscription_read_is_private_and_only_boolean_escapes():
    tasks = host_tasks()
    raw = next(task for task in tasks if "_pbs_subscription_raw" in str(task))
    safe_fact = next(task for task in tasks if "_pbs_enterprise_subscription_active" in str(task))
    assert raw["no_log"] is True
    assert safe_fact["no_log"] is True
    assert raw["ansible.builtin.command"]["argv"][:3] == ["proxmox-backup-manager", "subscription", "get"]


def test_service_path_never_schedules_pruning_or_removes_backups():
    text = DATASTORE_TASKS.read_text()
    assert "prune-schedule" not in text
    assert "datastore remove" not in text
    assert "--destroy-data" not in text
