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
IMAGE_HELPER = ROOT / "platform/playbooks/files/check-agentgateway-rendered-image.py"
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


class _ComposeLoader(yaml.SafeLoader):
    """Read Compose overlays with tagged override values intact."""


_ComposeLoader.add_multi_constructor(
    "!",
    lambda loader, suffix, node: (
        loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)
    ),
)


def test_config_validation_precedes_container_lifecycle_and_suppresses_diagnostics():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    phase_one = next(play for play in plays if play.get("name", "").startswith("Phase 1:"))
    phase_two_index = next(i for i, play in enumerate(plays) if play.get("name", "").startswith("Phase 2:"))
    assert plays.index(phase_one) < phase_two_index
    tasks = phase_one["tasks"]
    validate = next(task for task in tasks if task.get("name", "").startswith("Validate the rendered config"))
    task_names = [task.get("name", "") for task in tasks]
    image_check_index = task_names.index("Require the rendered Compose image to match the reviewed pin")
    pull_index = task_names.index("Pull the deployment images once before config validation")
    image_id_index = task_names.index("Read the resolved local v1.5.0 image ID for validation")
    validate_index = tasks.index(validate)
    assert image_check_index < pull_index < image_id_index < validate_index
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
    assert "_agw_validated_image_id.stdout | trim" in argv
    assert "Require an exact local image ID before config validation" in task_names
    assert "check-agentgateway-rendered-image.py" in str(tasks[image_check_index])
    assert tasks[image_check_index]["no_log"] is True
    for overlay in ("compose.local.yml", "compose.tls.yml"):
        compose_path = PLAYBOOK.parents[1] / "services/agentgateway/deployment" / overlay
        compose = yaml.load(compose_path.read_text(), Loader=_ComposeLoader)
        assert all("image" not in service for service in compose.get("services", {}).values())
    pull = tasks[pull_index]
    assert "bash deploy.sh --pull-only" in pull["ansible.builtin.shell"]
    assert pull["when"] == "not ansible_check_mode"

    phase_two = next(play for play in plays if play.get("name", "").startswith("Phase 2:"))
    phase_two_names = [task.get("name") for task in phase_two["tasks"]]
    assert phase_two_names.index("Refuse deployment if the validated image tag moved") < phase_two_names.index(
        "Run deploy.sh (container lifecycle)"
    )
    deploy_index = phase_two_names.index("Run deploy.sh (container lifecycle)")
    read_image_index = phase_two_names.index("Read the running gateway image ID after deployment")
    assert_index = phase_two_names.index(
        "Require the running gateway to use the image validated before recreation"
    )
    assert deploy_index < read_image_index < assert_index
    lifecycle = next(
        task for task in phase_two["tasks"] if task.get("name") == "Run deploy.sh (container lifecycle)"
    )
    assert "bash deploy.sh --no-pull" in lifecycle["ansible.builtin.shell"]
    running_id = next(
        task
        for task in phase_two["tasks"]
        if task.get("name") == "Read the running gateway image ID after deployment"
    )
    assert running_id["ansible.builtin.command"]["argv"][-1] == "agentgateway"
    deployed_id_assert = next(
        task
        for task in phase_two["tasks"]
        if task.get("name") == "Require the running gateway to use the image validated before recreation"
    )
    assert "_agw_validated_image_id.stdout" in str(deployed_id_assert["ansible.builtin.assert"]["that"])
    assert "regex_replace('^sha256:', '')" in str(deployed_id_assert["ansible.builtin.assert"]["that"])

    compose = yaml.safe_load((PLAYBOOK.parents[1] / "services/agentgateway/deployment/compose.yml").read_text())
    assert compose["services"]["agentgateway"]["image"] == "${AGW_IMAGE:-cr.agentgateway.dev/agentgateway:v1.5.0}"

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
            _agw_validated_image_id={"stdout": "sha256:" + "a" * 64},
        ))
        assert command[-3:] == ["--validate-only", "-f", "/config.yaml"]
        assert command[command.index("--validate-only") - 1] == "sha256:" + "a" * 64
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
    assert report["checkout_revision"] == "a" * 40
    assert "do-not-print-this" not in json.dumps(report)
    assert "config.yaml" not in json.dumps(report)


def open_json_input(env: Path, config: Path):
    import io

    return io.StringIO(json.dumps({"env_path": str(env), "config_path": str(config), "checkout_revision": "a" * 40}))


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


def test_rendered_image_checker_only_reports_the_reviewed_image(tmp_path, capsys, monkeypatch):
    spec = importlib.util.spec_from_file_location("check_agw_image", IMAGE_HELPER)
    helper = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(helper)
    image_env = tmp_path / ".env"
    image_env.write_text(
        "AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n"
        "VLLM_API_KEY=do-not-print-this\n"
    )
    monkeypatch.setattr("sys.argv", [str(IMAGE_HELPER), str(image_env)])
    assert helper.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"status": "pass", "image": "cr.agentgateway.dev/agentgateway:v1.5.0"}
    assert "do-not-print-this" not in json.dumps(report)

    image_env.write_text(
        "AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.1\n"
        "VLLM_API_KEY=do-not-print-this\n"
    )
    assert helper.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "refused"
    assert "v1.5.1" not in json.dumps(report)
    assert "do-not-print-this" not in json.dumps(report)

    image_env.write_text(
        "AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n"
        "AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n"
    )
    assert helper.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "refused"


def test_dev_template_is_read_only_and_requires_the_expected_revision():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    template = next(item for item in templates if item["name"] == "Verify agentgateway Runtime (Dev)")
    assert template["repository"] == "agent-cloud dev"
    assert template["playbook"] == "platform/playbooks/verify-agentgateway-runtime.yml"
    assert template["survey_vars"][0]["name"] == "expected_repository_sha"
    plays = yaml.safe_load(VERIFY_PLAYBOOK.read_text())
    receipt_plays = [play for play in plays if "tasks" in play]
    receipt_tasks = [task for play in receipt_plays for task in play["tasks"]]
    allowed_modules = {"ansible.builtin.command", "ansible.builtin.assert", "ansible.builtin.debug"}
    for play in receipt_plays:
        assert play.get("become", False) is False
    for task in receipt_tasks:
        assert task.get("become", False) is False
        modules = {key for key in task if key.startswith("ansible.builtin.")}
        assert modules <= allowed_modules
        if "ansible.builtin.command" in modules:
            assert task.get("changed_when") is False
            assert task.get("check_mode") is False
    assert {task["name"] for task in receipt_tasks if task.get("no_log") is True} == {
        "Read safe metadata from rendered files before runtime comparison",
        "Read safe metadata again after runtime comparison",
    }
    verify = next(
        task
        for task in receipt_tasks
        if task.get("name") == "Compare rendered inputs to the running deployment without mutation"
    )
    assert verify["ansible.builtin.command"]["argv"] == ["bash", "deploy.sh", "--verify-only"]
    assert "_ready_once" in (ROOT / "platform/services/agentgateway/deployment/deploy.sh").read_text()
    other_commands = [
        task["ansible.builtin.command"]["argv"]
        for task in receipt_tasks
        if "ansible.builtin.command" in task and task is not verify
    ]
    assert all("pull" not in str(argv) for argv in other_commands)
    running_image_check = next(
        task
        for task in receipt_tasks
        if task.get("name") == "Require the running gateway image ID to match the pinned local image"
    )
    assert "regex_replace('^sha256:', '')" in str(running_image_check["ansible.builtin.assert"]["that"])
    assert "_agw_running_image_id.stdout" in str(running_image_check["ansible.builtin.assert"]["that"])
    report = next(task for task in receipt_tasks if task.get("name") == "Report the reviewed Dev runtime metadata")
    assert set(report["ansible.builtin.debug"]["msg"]) == {
        "status", "image", "random_sampling", "client_sampling", "checkout_revision", "running_image_id"
    }
    assert "rendered_config_sha256" not in report["ansible.builtin.debug"]["msg"]


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
