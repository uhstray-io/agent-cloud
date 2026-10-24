"""Replacing undecryptable AppRole inputs in Provision Seed Environment (MISTAKES 3.16).

The provisioner's own tasks are read from platform/semaphore/provision-seed-environment.yml
and run by ansible-playbook against synthetic environments. The filters they use are
Ansible's (combine, difference, selectattr, from_json), so a copy of the expressions in
Python would test the copy, not the provisioner.
"""

import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROVISIONER = ROOT / "platform/semaphore/provision-seed-environment.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
PLAY = yaml.safe_load(PROVISIONER.read_text())[0]


def _task(name):
    matches = [task for task in PLAY["tasks"] if task.get("name") == name]
    assert len(matches) == 1, f"provisioner must have exactly one task named {name!r}"
    return matches[0]


PREPARE = _task("Prepare encrypted authentication inputs in memory")
VERIFY = _task("Verify preserved inputs and authentication identities")

ENVIRONMENT = {"id": 501, "name": "Seed inputs (Dev)", "json": "{}", "env": "{}"}
OTHER = {"id": 10, "name": "OTHER_INPUT", "type": "env"}
ROLE = {"id": 70, "name": "BAO_ROLE_ID", "type": "env"}
SECRET = {"id": 71, "name": "BAO_SECRET_ID", "type": "env"}
NEW_ROLE = {"id": 800, "name": "BAO_ROLE_ID", "type": "env"}
NEW_SECRET = {"id": 801, "name": "BAO_SECRET_ID", "type": "env"}
UPDATE_ROLE = {"id": 70, "name": "BAO_ROLE_ID", "type": "env", "operation": "update", "secret": "fixture-role"}
UPDATE_SECRET = {"id": 71, "name": "BAO_SECRET_ID", "type": "env", "operation": "update", "secret": "fixture-secret"}
CREATE_ROLE = {"name": "BAO_ROLE_ID", "type": "env", "operation": "create", "secret": "fixture-role"}
CREATE_SECRET = {"name": "BAO_SECRET_ID", "type": "env", "operation": "create", "secret": "fixture-secret"}


def _run(tmp_path, *, target, after, replace):
    """Run the real Prepare and Verify tasks. Returns (rc, operations, verified).

    `replace` is the survey string, or None to leave the survey value unset.
    """
    variables = {
        "_replace_auth": PLAY["vars"]["_replace_auth"],
        "bao_role_id": "fixture-role",
        "bao_secret_id": "fixture-secret",
        "_target": {"json": ENVIRONMENT | {"secrets": target}},
        "_after": {"json": ENVIRONMENT | {"secrets": after}},
    }
    if replace is not None:
        variables["seed_replace_auth"] = replace
    operations = tmp_path / "operations.json"
    verified = tmp_path / "verified"
    playbook = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": variables,
            "tasks": [
                PREPARE,
                {
                    "name": "Record the prepared operations",
                    "ansible.builtin.copy": {"content": "{{ _auth_operations | to_json }}", "dest": str(operations)},
                },
                VERIFY,
                {
                    "name": "Mark a passed verification",
                    "ansible.builtin.file": {"path": str(verified), "state": "touch"},
                },
            ],
        }
    ]
    playbook_path = tmp_path / "provisioner-under-test.yml"
    playbook_path.write_text(yaml.safe_dump(playbook, sort_keys=False))
    env = os.environ | {
        "ANSIBLE_LOCAL_TEMP": str(tmp_path),
        "ANSIBLE_REMOTE_TEMP": str(tmp_path),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_STDOUT_CALLBACK": "default",
    }
    result = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(playbook_path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert "fixture-secret" not in output
    prepared = json.loads(operations.read_text()) if operations.exists() else None
    return result.returncode, prepared, verified.exists()


def test_default_run_keeps_existing_inputs_and_creates_none(tmp_path):
    assert _run(tmp_path, target=[OTHER, ROLE, SECRET], after=[OTHER, ROLE, SECRET], replace="false") == (0, [], True)


def test_unset_flag_behaves_as_the_default(tmp_path):
    assert _run(tmp_path, target=[OTHER, ROLE, SECRET], after=[OTHER, ROLE, SECRET], replace=None) == (0, [], True)


def test_default_run_creates_only_missing_inputs(tmp_path):
    assert _run(tmp_path, target=[OTHER], after=[OTHER, NEW_ROLE, NEW_SECRET], replace="false") == (
        0,
        [CREATE_ROLE, CREATE_SECRET],
        True,
    )


def test_default_run_refuses_when_an_existing_input_vanishes(tmp_path):
    rc, _, verified = _run(tmp_path, target=[OTHER, ROLE, SECRET], after=[OTHER, ROLE], replace="false")
    assert rc != 0 and not verified


def test_replace_overwrites_the_held_inputs_in_place(tmp_path):
    # In place, by id: Semaphore applies operations without rollback, so delete-then-create
    # could leave a lone half that every later run refuses.
    assert _run(tmp_path, target=[OTHER, ROLE, SECRET], after=[OTHER, ROLE, SECRET], replace="true") == (
        0,
        [UPDATE_ROLE, UPDATE_SECRET],
        True,
    )


def test_replace_pairs_each_value_with_its_input_whatever_the_order(tmp_path):
    rc, prepared, _ = _run(tmp_path, target=[SECRET, OTHER, ROLE], after=[SECRET, OTHER, ROLE], replace="true")
    assert rc == 0 and prepared == [UPDATE_ROLE, UPDATE_SECRET]


def test_replace_without_held_inputs_only_creates(tmp_path):
    assert _run(tmp_path, target=[OTHER], after=[OTHER, NEW_ROLE, NEW_SECRET], replace="true") == (
        0,
        [CREATE_ROLE, CREATE_SECRET],
        True,
    )


def test_replace_refuses_when_an_input_changes_identity(tmp_path):
    rc, _, verified = _run(tmp_path, target=[OTHER, ROLE, SECRET], after=[OTHER, ROLE, NEW_SECRET], replace="true")
    assert rc != 0 and not verified


def test_replace_refuses_when_an_input_vanishes(tmp_path):
    rc, _, verified = _run(tmp_path, target=[OTHER, ROLE, SECRET], after=[OTHER, ROLE], replace="true")
    assert rc != 0 and not verified


def test_replace_refuses_when_an_unrelated_input_vanishes(tmp_path):
    other_new = {"id": 900, "name": "NEW_OTHER", "type": "env"}
    rc, _, verified = _run(tmp_path, target=[OTHER, ROLE, SECRET], after=[other_new, ROLE, SECRET], replace="true")
    assert rc != 0 and not verified


def test_survey_offers_the_flag_as_an_optional_false_default():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    (template,) = [t for t in templates if t["name"] == "Provision Seed Environment"]
    flags = [v for v in template["survey_vars"] if v["name"] == "seed_replace_auth"]
    assert len(flags) == 1
    assert flags[0]["default_value"] == "false"
    assert flags[0]["required"] is False
