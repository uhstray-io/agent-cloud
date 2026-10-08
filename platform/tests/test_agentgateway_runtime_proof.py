"""Focused source checks for the agentgateway task 5.7 first-stage receipt."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/deploy-agentgateway.yml"
VERIFY_PLAYBOOK = ROOT / "platform/playbooks/verify-agentgateway-runtime.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
HELPER = ROOT / "platform/playbooks/files/inspect-agentgateway-runtime.py"
RENDERED_CONFIG = (
    'apiKey: "do-not-print-this"\n'
    "frontendPolicies:\n"
    "  accessLog:\n"
    "    add:\n"
    "      identity: apiKey.name\n"
    "  tracing:\n"
    "    randomSampling: 0.05\n"
    "    clientSampling: 0.05\n"
)


def _load_helper():
    spec = importlib.util.spec_from_file_location("inspect_agw_runtime", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_config_validation_precedes_container_lifecycle_and_suppresses_diagnostics():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    phase_one = next(play for play in plays if play.get("name", "").startswith("Phase 1:"))
    phase_two_index = next(i for i, play in enumerate(plays) if play.get("name", "").startswith("Phase 2:"))
    assert plays.index(phase_one) < phase_two_index
    tasks = phase_one["tasks"]
    validate = next(task for task in tasks if task.get("name", "").startswith("Validate the rendered config"))
    task_names = [task.get("name", "") for task in tasks]
    pull_index = task_names.index("Pull the deployment images once before config validation")
    validate_index = tasks.index(validate)
    assert pull_index < validate_index
    assert task_names.index("Manage secrets and render env + config") < validate_index
    assert task_names.index("Read the issued server and verifier leaves") < validate_index
    assert task_names.index("Read the deploy account's uid:gid for the local key owner") < validate_index
    assert task_names.index("Distribute the step-ca trust bundle into ./certs") < validate_index
    refuse_index = task_names.index(
        "Refuse container recreation when the pinned image rejects rendered config"
    )
    assert validate_index < refuse_index
    argv = str(validate["ansible.builtin.command"]["argv"])
    assert "--validate-only" in argv and "/config.yaml" in argv
    assert "--volume" in argv and "/certs:/certs:ro" in argv
    assert "--userns=keep-id:uid=65532,gid=65532" in argv
    assert "SSL_CERT_FILE=/certs/step-ca-bundle.crt" in argv
    assert validate["no_log"] is True
    assert "'run', '--pull=never', '--rm', '--network=none'" in argv
    pull = tasks[pull_index]
    assert "bash deploy.sh --pull-only" in pull["ansible.builtin.shell"]
    assert pull["when"] == "not ansible_check_mode"

    phase_two = next(play for play in plays if play.get("name", "").startswith("Phase 2:"))
    lifecycle = next(task for task in phase_two["tasks"] if task.get("name") == "Run deploy.sh (container lifecycle)")
    assert "bash deploy.sh --no-pull" in lifecycle["ansible.builtin.shell"]

    env = Environment()
    env.filters["bool"] = bool
    template = env.from_string(argv)
    for tls, local in ((False, False), (True, False), (False, True), (True, True)):
        command = yaml.safe_load(template.render(
            container_engine="podman",
            _deploy_dir="/srv/agentgateway",
            _tls=tls,
            local_mode=local,
            _key_owner={"stdout": "1000:1000"},
        ))
        assert command[-3:] == ["--validate-only", "-f", "/config.yaml"]
        assert ("/srv/agentgateway/certs:/certs:ro" in command) == (tls or local)
        assert ("--userns=keep-id:uid=65532,gid=65532" in command) == (tls and not local)
        assert ("1000:1000" in command) == (tls and local)
        assert ("SSL_CERT_FILE=/certs/step-ca-bundle.crt" in command) == local


def test_rendered_config_inspector_emits_only_pinned_image_sampling_revision_and_digest(tmp_path, capsys):
    helper = _load_helper()
    env = tmp_path / ".env"
    config = tmp_path / "config.yaml"
    env.write_text("AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\nVLLM_API_KEY=do-not-print-this\n")
    config.write_text(RENDERED_CONFIG)
    import sys
    from unittest.mock import patch

    with patch.object(sys, "stdin", open_json_input(env, config)):
        assert helper.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    assert report["image"] == "cr.agentgateway.dev/agentgateway:v1.5.0"
    assert report["random_sampling"] == report["client_sampling"] == 0.05
    assert report["repository_revision"] == "a" * 40
    assert "do-not-print-this" not in json.dumps(report)
    assert "config.yaml" not in json.dumps(report)


def open_json_input(env: Path, config: Path):
    import io

    return io.StringIO(json.dumps({"env_path": str(env), "config_path": str(config), "revision": "a" * 40}))


def test_rendered_config_inspector_rejects_sampling_or_image_drift(tmp_path, capsys):
    helper = _load_helper()
    env = tmp_path / ".env"
    config = tmp_path / "config.yaml"
    env.write_text("AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.1\n")
    config.write_text(RENDERED_CONFIG)
    import sys
    from unittest.mock import patch

    with patch.object(sys, "stdin", open_json_input(env, config)):
        assert helper.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "refused"
    assert "v1.5.1" not in json.dumps(report)


def test_rendered_config_inspector_accepts_disabled_client_sampling(tmp_path, capsys):
    helper = _load_helper()
    env = tmp_path / ".env"
    config = tmp_path / "config.yaml"
    env.write_text("AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n")
    config.write_text(RENDERED_CONFIG.replace("clientSampling: 0.05", "clientSampling: false"))
    import sys
    from unittest.mock import patch

    with patch.object(sys, "stdin", open_json_input(env, config)):
        assert helper.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["random_sampling"] == 0.05
    assert report["client_sampling"] is False


def test_rendered_config_inspector_rejects_duplicate_sampling_declarations(tmp_path, capsys):
    helper = _load_helper()
    env = tmp_path / ".env"
    config = tmp_path / "config.yaml"
    env.write_text("AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n")
    import sys
    from unittest.mock import patch

    for duplicate in ("randomSampling", "clientSampling"):
        config.write_text(RENDERED_CONFIG + f"    {duplicate}: 0.05\n")
        with patch.object(sys, "stdin", open_json_input(env, config)):
            assert helper.main() == 2
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "refused"
        assert "duplicated" in report["reason"]
        assert "do-not-print-this" not in json.dumps(report)


def test_rendered_config_inspector_ignores_sampling_outside_frontend_tracing(tmp_path, capsys):
    helper = _load_helper()
    env = tmp_path / ".env"
    config = tmp_path / "config.yaml"
    env.write_text("AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n")
    config.write_text(
        "tracing:\n"
        "  randomSampling: 0.05\n"
        "  clientSampling: 0.05\n"
        "frontendPolicies:\n"
        "  accessLog:\n"
        "    add:\n"
        "      identity: apiKey.name\n"
    )
    import sys
    from unittest.mock import patch

    with patch.object(sys, "stdin", open_json_input(env, config)):
        assert helper.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "refused"
    assert "frontendPolicies.tracing" in report["reason"]


def test_dev_template_is_read_only_and_requires_the_expected_revision():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    template = next(item for item in templates if item["name"] == "Verify agentgateway Runtime (Dev)")
    assert template["repository"] == "agent-cloud dev"
    assert template["playbook"] == "platform/playbooks/verify-agentgateway-runtime.yml"
    assert template["survey_vars"][0]["name"] == "expected_repository_sha"
    plays = yaml.safe_load(VERIFY_PLAYBOOK.read_text())
    write_tasks = [
        task
        for play in plays
        for task in play.get("tasks", [])
        if task.get("name", "").lower().startswith("write")
    ]
    assert all("ansible.builtin.command" not in task for task in write_tasks)


def test_provisioned_dashboard_has_access_signal_selector():
    dashboard_path = ROOT / "platform/services/o11y/deployment/config/grafana/dashboards/agentgateway-traffic.json"
    dashboard = json.loads(dashboard_path.read_text())
    access_targets = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "signal=\"access-log\"" in target.get("expr", "")
    ]
    assert access_targets
