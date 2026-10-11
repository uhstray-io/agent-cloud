from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/grant-github-runner-app-repository.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"


def _named(tasks, name):
    return next(task for task in tasks if task.get("name") == name)


def test_template_is_one_direct_dev_binding_with_only_review_inputs():
    declarations = yaml.safe_load(TEMPLATES.read_text(encoding="utf-8"))["templates"]
    matching = [item for item in declarations if item["name"] == "Grant GitHub Runner App Repository (Dev)"]
    assert len(matching) == 1
    template = matching[0]
    assert template["playbook"] == "platform/playbooks/grant-github-runner-app-repository.yml"
    assert template["repository"] == "agent-cloud dev"
    assert [item["name"] for item in template["survey_vars"]] == [
        "expected_repository_sha",
        "github_app_client_id",
    ]


def test_playbook_binds_review_sha_and_reads_only_the_two_declared_openbao_secrets():
    document = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play = next(item for item in document if item.get("hosts") == "localhost")
    tasks = play["tasks"]
    revision = _named(tasks, "Require the exact clean reviewed Dev revision")["ansible.builtin.assert"]
    assert "expected_repository_sha" in revision["that"][0]
    assert "_controller_revision.stdout | trim == expected_repository_sha" in revision["that"]
    assert "_controller_changes.stdout | length == 0" in revision["that"]

    reads = [
        task
        for task in tasks
        if task.get("name", "").startswith("Read the GitHub")
        or task.get("name") == "Read the runner App private key from OpenBao"
    ]
    assert {task["ansible.builtin.uri"]["url"] for task in reads} == {
        "{{ _bao_url }}/v1/secret/data/services/github",
        "{{ _bao_url }}/v1/secret/data/services/github-runner",
    }
    assert all(task["no_log"] is True for task in reads)
    assert all(task["ansible.builtin.uri"]["follow_redirects"] == "none" for task in reads)


def test_secret_material_only_enters_helper_stdin_and_mutation_is_guarded():
    document = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play = next(item for item in document if item.get("hosts") == "localhost")
    tasks = play["tasks"]
    for name in ("Inspect the fixed repository grant state", "Add the repository to the App installation"):
        task = _named(tasks, name)
        assert task["no_log"] is True
        command = task["ansible.builtin.command"]
        assert "stdin" in command
        assert "pat" in command["stdin"]
        assert "app_private_key" in command["stdin"]
        assert "pat" not in command.get("argv", "")
        assert "app_private_key" not in command.get("argv", "")

    for name in (
        "Authenticate to OpenBao",
        "Read the GitHub classic PAT from OpenBao",
        "Read the runner App private key from OpenBao",
        "Require both stored GitHub credentials",
    ):
        assert _named(tasks, name)["no_log"] is True

    grant = _named(tasks, "Add the repository to the App installation")
    assert "not ansible_check_mode" in grant["when"]
    assert "not (_verify_only | bool)" in grant["when"]
    assert grant["tags"] == ["verify"]
    assert all("verify" in task.get("tags", []) for task in tasks)


def test_inspection_is_always_check_mode_safe_and_verify_tag_cannot_grant():
    document = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play = next(item for item in document if item.get("hosts") == "localhost")
    tasks = play["tasks"]
    inspection = _named(tasks, "Inspect the fixed repository grant state")
    assert inspection["check_mode"] is False
    assert inspection["changed_when"] is False
    assert "--check-mode" in inspection["ansible.builtin.command"]["argv"]
    assert "--verify-only" in inspection["ansible.builtin.command"]["argv"]
    assert "not ansible_check_mode" in _named(tasks, "Add the repository to the App installation")["when"]
