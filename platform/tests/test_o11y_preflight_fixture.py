"""The o11y guard fixture builder must never turn into a workflow runner."""

from pathlib import Path

import o11y_preflight_fixture as fixture
import playbook_yaml
import pytest


@pytest.mark.parametrize(
    "task",
    [
        {"name": "request", "ansible.builtin.uri": {"url": "http://192.0.2.1/"}},
        {"name": "shell", "ansible.builtin.shell": "echo unsafe"},
        {"name": "include", "ansible.builtin.include_tasks": "deploy.yml"},
        {"name": "copy", "ansible.builtin.copy": {"content": "x", "dest": "/tmp/x"}},
        {"name": "git write", "ansible.builtin.command": {"argv": ["git", "status"]}},
        {
            "name": "git with stdin",
            "ansible.builtin.command": {
                "argv": ["git", "rev-parse", "HEAD"],
                "chdir": fixture.PLAYBOOK_DIR_EXPR,
                "stdin": "untrusted",
            },
            "changed_when": False,
            "check_mode": False,
        },
        {
            "name": "other command",
            "ansible.builtin.command": {"argv": ["python3", "-c", "pass"]},
            "changed_when": False,
            "check_mode": False,
        },
    ],
)
def test_fixture_builder_rejects_tasks_outside_guard_boundary(task):
    with pytest.raises(fixture.UnsafeFixtureTask):
        fixture._check_task(task, Path("/repo"))


def test_fixture_builder_copies_only_named_source_guard_and_rebinds_git_read(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = repo / "platform/playbooks/deploy-o11y.yml"
    selected = fixture.task(source, "Read the controller checkout revision when a candidate SHA is required")
    fixture.build_play(
        tmp_path / "guard.yml",
        repo,
        "localhost",
        [
            (
                "platform/playbooks/deploy-o11y.yml",
                "Read the controller checkout revision when a candidate SHA is required",
            )
        ],
        public_vars={"expected_repository_sha": "0" * 40},
    )

    play = playbook_yaml.loads((tmp_path / "guard.yml").read_text())[0]
    expected = {**selected, "ansible.builtin.command": {
        **selected["ansible.builtin.command"], "chdir": str(repo)
    }}
    assert play["tasks"] == [expected]
    assert play["vars"] == {"expected_repository_sha": "0" * 40}


def test_fixture_builder_rejects_context_from_a_different_source_task(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with pytest.raises(fixture.UnsafeFixtureTask, match="fixed o11y TLS guard include"):
        fixture.build_play(
            tmp_path / "guard.yml",
            repo,
            "localhost",
            [
                (
                    "platform/playbooks/deploy-o11y.yml",
                    "Require the production DNS zone and Caddy origin before rendering Grafana OIDC",
                    "platform/playbooks/deploy-o11y.yml",
                    "Validate the fixed receiver and journal TLS contract before placement or rendering",
                )
            ],
        )
