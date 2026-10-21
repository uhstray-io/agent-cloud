"""Fail-closed contracts for the Semaphore PBS service bootstrap."""

from pathlib import Path
import importlib.util
import shutil
import sys

import pytest
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
    source_hash = task_index(datastore, "Hash the checked-out guest path helper")
    installed_hash = task_index(datastore, "Read back the installed guest path helper checksum")
    checksum_assertion = task_index(datastore, "Require the installed guest helper to match")
    mount_proof = task_index(datastore, "Re-prove the data mount immediately before datastore path validation")
    containment = task_index(datastore, "Prove datastore path containment on the pinned data filesystem")
    datastore_create = task_index(datastore, "Create the PBS datastore without a prune schedule")
    assert source_hash < installed_hash < checksum_assertion < mount_proof < containment < datastore_create
    assert "prepare_pbs_guest_disk.py" in str(datastore[source_hash])
    assert "/usr/local/lib/agent-cloud-prepare-pbs-guest-disk.py" in str(datastore[installed_hash])
    assert "--validate-datastore-path" in str(datastore[containment])
    assert datastore[containment]["ansible.builtin.command"]["argv"][-1] == "{{ _pbs_datastore_path }}"


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


def test_unclaimed_datastore_scan_includes_directories_and_symlinks():
    tasks = yaml.safe_load(DATASTORE_TASKS.read_text())
    scan = next(task["ansible.builtin.find"] for task in tasks if "Check existing unclaimed datastore directory contents" in task["name"])
    assert scan["file_type"] == "any"
    assert scan["recurse"] is False


def test_check_mode_skips_missing_object_readbacks_and_reports_planned_create():
    tasks = yaml.safe_load(DATASTORE_TASKS.read_text())
    datastore_readback = next(task for task in tasks if "Read back datastore and verification job configuration" in task["name"])
    job_readback = next(task for task in tasks if "Read back verification job configuration" in task["name"])
    assert "not ansible_check_mode" in str(datastore_readback["when"])
    assert "or _pbs_datastores" in str(datastore_readback["when"])
    assert "_pbs_datastores" in str(datastore_readback["when"])
    assert "not ansible_check_mode" in str(job_readback["when"])
    assert "or _pbs_verify_job_list" in str(job_readback["when"])
    assert "_pbs_verify_job_list" in str(job_readback["when"])
    assert task_index(tasks, "Report datastore creation planned in check mode") > task_index(
        tasks, "Require the final datastore configuration"
    )
    assert task_index(tasks, "Report verification job creation planned in check mode") > task_index(
        tasks, "Require the final datastore configuration"
    )


@pytest.mark.parametrize("entry_kind", ["populated_directory", "symlink"])
def test_ansible_find_any_detects_unsafe_datastore_entries(tmp_path, entry_kind):
    tasks = yaml.safe_load(DATASTORE_TASKS.read_text())
    scan = next(task["ansible.builtin.find"] for task in tasks if "Check existing unclaimed datastore directory contents" in task["name"])
    datastore = tmp_path / "datastore"
    datastore.mkdir()
    if entry_kind == "populated_directory":
        nested = datastore / "unexpected"
        nested.mkdir()
        (nested / "data").write_text("preserve")
    else:
        outside = tmp_path / "outside"
        outside.write_text("preserve")
        (datastore / "unexpected-link").symlink_to(outside)

    params = {**scan, "paths": str(datastore)}
    module_path = _ansible_find_module_path()
    spec = importlib.util.spec_from_file_location("ansible_find_for_pbs_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class ModuleExit(Exception):
        def __init__(self, result):
            self.result = result

    class AnsibleModuleStub:
        def __init__(self, **_kwargs):
            self.params = {
                "paths": [str(datastore)],
                "patterns": [], "excludes": None, "contains": None,
                "read_whole_file": False, "file_type": params["file_type"],
                "age": None, "age_stamp": "mtime", "size": None,
                "recurse": params["recurse"], "hidden": params["hidden"],
                "follow": False, "get_checksum": False,
                "checksum_algorithm": "sha256", "use_regex": False,
                "depth": None, "mode": None, "exact_mode": True,
                "encoding": None, "limit": None,
            }

        def exit_json(self, **kwargs):
            raise ModuleExit(kwargs)

        def fail_json(self, **kwargs):
            raise AssertionError(kwargs)

        def warn(self, *_args, **_kwargs):
            raise AssertionError("the fixture should not produce a find warning")

        error_as_warning = warn

    module.AnsibleModule = AnsibleModuleStub
    with pytest.raises(ModuleExit) as result:
        module.main()
    assert result.value.result["matched"] == 1


def _ansible_find_module_path():
    try:
        import ansible
        return Path(ansible.__file__).parent / "modules/find.py"
    except ImportError:
        executable = shutil.which("ansible-playbook")
        if executable is None:
            pytest.skip("ansible-core is unavailable for the behavioral find regression")
        interpreter = Path(Path(executable).read_text().splitlines()[0].removeprefix("#!"))
        candidates = list(interpreter.parent.parent.glob("lib/python*/site-packages/ansible/modules/find.py"))
        if not candidates:
            pytest.skip("ansible-core module source is unavailable for the behavioral find regression")
        site_packages = candidates[0].parents[2]
        sys.path.insert(0, str(site_packages))
        return candidates[0]
