"""Fail-closed contracts for the Semaphore PBS service bootstrap."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

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


def test_pbs_guest_and_service_templates_are_dev_bound_and_only_take_reviewed_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    expected = {
        "Prepare PBS Guest Data Disk (Dev)": "platform/playbooks/prepare-pbs-guest-data-disk.yml",
        "Configure PBS Service (Dev)": "platform/playbooks/configure-pbs.yml",
    }
    for name, playbook in expected.items():
        matches = [template for template in templates if template["name"] == name]
        assert len(matches) == 1
        template = matches[0]
        assert template["playbook"] == playbook
        assert template["repository"] == "agent-cloud dev"
        assert [field["name"] for field in template["survey_vars"]] == ["expected_repository_sha"]
        assert template["survey_vars"][0]["required"] is True


def test_enterprise_subscription_read_is_private_and_only_boolean_escapes():
    tasks = host_tasks()
    raw = next(task for task in tasks if "_pbs_subscription_raw" in str(task))
    safe_fact = next(task for task in tasks if "_pbs_enterprise_subscription_active" in str(task))
    assert raw["no_log"] is True
    assert safe_fact["no_log"] is True
    assert raw["ansible.builtin.command"]["argv"][:3] == ["proxmox-backup-manager", "subscription", "get"]


def test_enterprise_manager_discovery_uses_the_packaged_sbin_path_and_check_mode_verifies_installed_state():
    tasks = host_tasks()
    manager_checks = [
        task for task in tasks
        if task.get("ansible.builtin.stat", {}).get("path") == "/usr/sbin/proxmox-backup-manager"
    ]
    assert len(manager_checks) == 2
    assert manager_checks[0]["when"] == "_pbs_repository_channel == 'enterprise'"
    datastore_include = next(
        task for task in tasks if task.get("ansible.builtin.include_tasks") == "tasks/configure-pbs-datastore.yml"
    )
    assert datastore_include["when"] == "not ansible_check_mode or _pbs_ready_after_install.stat.exists"
    assert all(
        task.get("ansible.builtin.stat", {}).get("path") != "/usr/bin/proxmox-backup-manager"
        for task in tasks
    )


def test_pve_create_form_omits_derived_or_default_only_storage_fields():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/configure-pbs-backup-access.yml").read_text())
    tasks = next(play["tasks"] for play in playbook if play.get("hosts") == "localhost" and
                 any("Build the exact PVE storage form" in task["name"] for task in play.get("tasks", [])))
    build = next(task for task in tasks if "Build the exact PVE storage form" in task["name"])
    form = build["ansible.builtin.set_fact"]["_pve_pbs_storage_form"]
    assert "shared" not in form and "disable" not in form


def test_verify_tag_is_read_only_and_requires_existing_mount_service_and_datastore_state():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    preflight = next(
        play for play in plays if play.get("name") == "Require one declared PBS guest"
    )
    verify = next(
        play for play in plays
        if play.get("name") == "Verify existing PBS service state without reconciling it"
    )
    assert preflight["tags"] == ["verify"]
    assert verify["tags"] == ["verify"]
    tasks = verify["tasks"]
    assert any("assert_pbs_data_mount.py" in str(task) for task in tasks)
    assert any("/usr/sbin/proxmox-backup-manager" in str(task) for task in tasks)
    assert any("api2/json/version" in str(task) for task in tasks)
    assert any(task.get("name") == "Read existing PBS datastore state" for task in tasks)
    assert any(task.get("name") == "Read existing PBS verification job state" for task in tasks)
    assert not any(
        module in str(verify)
        for module in (
            "ansible.builtin.apt", "ansible.builtin.template", "ansible.builtin.file",
            "ansible.builtin.service", "datastore, create", "verify-job, create", "bao-merge-keys",
        )
    )


@pytest.mark.parametrize(("relative_path", "selected", "excluded"), [
    (
        "prepare-pbs-guest-data-disk.yml",
        "Require the serial-pinned disk to be an existing formatted disk",
        "Create ext4 on the proven blank guest virtual disk",
    ),
    (
        "configure-pbs.yml",
        "Require the exact datastore path and maintenance state",
        "Create the PBS datastore",
    ),
    (
        "configure-pbs-backup-access.yml",
        "Require existing OpenBao-backed PBS identity",
        "Configure the stable PBS backup identity",
    ),
    (
        "preflight-o11y-restore-target.yml",
        "Validate exact target identity, isolated layout, and reported capacity",
        "POST",
    ),
])
def test_ansible_verify_tag_selects_read_only_sequence(relative_path, selected, excluded, tmp_path):
    ansible_playbook = shutil.which("ansible-playbook")
    if ansible_playbook is None:
        pytest.skip("needs ansible-playbook")
    local_tmp = tmp_path / "ansible-local"
    local_tmp.mkdir()
    path = ROOT / "platform/playbooks" / relative_path
    completed = subprocess.run(
        [ansible_playbook, "-i", "localhost,", "--list-tasks", "--tags", "verify", str(path)],
        cwd=ROOT,
        env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(local_tmp), "ANSIBLE_NOCOLOR": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    selected_tasks = [
        line.split("TAGS:", 1)[0].strip()
        for line in completed.stdout.splitlines()
        if "TAGS: [verify]" in line and "play #" not in line
    ]
    assert any(selected in task for task in selected_tasks)
    assert not any(excluded in task for task in selected_tasks)


def test_service_path_never_schedules_pruning_or_removes_backups():
    text = DATASTORE_TASKS.read_text()
    assert "prune-schedule" not in text
    assert "datastore remove" not in text
    assert "--destroy-data" not in text


def test_unclaimed_datastore_scan_includes_directories_and_symlinks():
    tasks = yaml.safe_load(DATASTORE_TASKS.read_text())
    scan = next(
        task["ansible.builtin.find"]
        for task in tasks
        if "Check existing unclaimed datastore directory contents" in task["name"]
    )
    assert scan["file_type"] == "any"
    assert scan["recurse"] is False


def test_check_mode_skips_missing_object_readbacks_and_reports_planned_create():
    tasks = yaml.safe_load(DATASTORE_TASKS.read_text())
    datastore_readback = next(
        task for task in tasks if "Read back datastore and verification job configuration" in task["name"]
    )
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


def test_reconcile_conditions_skip_fresh_objects_and_apply_existing_drift():
    tasks = yaml.safe_load(DATASTORE_TASKS.read_text())
    datastore_update = next(task for task in tasks if "Reconcile datastore garbage collection" in task["name"])
    verify_update = next(task for task in tasks if "Reconcile the managed verification schedule" in task["name"])
    assert "_pbs_datastore_current is not skipped" in datastore_update["when"]
    assert "_pbs_verify_job_current is not skipped" in verify_update["when"]

    datastore_absent = {
        "ansible_check_mode": False,
        "_pbs_datastores": [],
        "_pbs_datastore": "o11y",
        "_pbs_datastore_current": {"skipped": True},
        "_pbs_gc_schedule": "daily",
    }
    verify_absent = {
        "ansible_check_mode": False,
        "_pbs_verify_job_list": [],
        "_pbs_verify_job": "weekly-o11y",
        "_pbs_verify_job_current": {"skipped": True},
        "_pbs_verify_schedule": "sun 02:00",
    }
    datastore_existing_drift = {
        **datastore_absent,
        "_pbs_datastores": [{"name": "o11y"}],
        "_pbs_datastore_current": {"stdout": json.dumps({"gc-schedule": "weekly", "verify-new": False})},
    }
    verify_existing_drift = {
        **verify_absent,
        "_pbs_verify_job_list": [{"id": "weekly-o11y"}],
        "_pbs_verify_job_current": {
            "stdout": json.dumps({"schedule": "daily", "ignore-verified": False, "outdated-after": 30})
        },
    }

    assert not _evaluate_when(datastore_update["when"], datastore_absent)
    assert not _evaluate_when(verify_update["when"], verify_absent)
    assert _evaluate_when(datastore_update["when"], datastore_existing_drift)
    assert _evaluate_when(verify_update["when"], verify_existing_drift)


@pytest.mark.parametrize("entry_kind", ["populated_directory", "symlink"])
def test_ansible_find_any_detects_unsafe_datastore_entries(tmp_path, entry_kind):
    tasks = yaml.safe_load(DATASTORE_TASKS.read_text())
    scan = next(
        task["ansible.builtin.find"]
        for task in tasks
        if "Check existing unclaimed datastore directory contents" in task["name"]
    )
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


def _evaluate_when(conditions, context):
    environment = Environment()
    environment.filters["from_json"] = json.loads
    environment.tests["skipped"] = lambda value: isinstance(value, dict) and value.get("skipped", False)
    return all(environment.compile_expression(str(condition))(**context) for condition in conditions)
