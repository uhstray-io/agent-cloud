"""Focused source checks for the agentgateway task 5.7 first-stage receipt."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/deploy-agentgateway.yml"
VERIFY_PLAYBOOK = ROOT / "platform/playbooks/verify-agentgateway-runtime.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
HELPER = ROOT / "platform/playbooks/files/inspect-agentgateway-runtime.py"
IMAGE_HELPER = ROOT / "platform/playbooks/files/check-agentgateway-rendered-image.py"
CONFIG_TEMPLATE = ROOT / "platform/services/agentgateway/deployment/templates/config.yaml.j2"
ENV_TEMPLATE = ROOT / "platform/services/agentgateway/deployment/templates/env.j2"
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


def _isolated_git_environment():
    env = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
    ):
        env.pop(name, None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


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
    validation_refusal = tasks[refuse_index]
    refusal_message = validation_refusal["ansible.builtin.assert"]["fail_msg"]
    classifier_index = task_names.index("Classify the validator result without exposing its output")
    assert validate_index < classifier_index < refuse_index
    classifier = tasks[classifier_index]
    assert classifier["no_log"] is True
    assert classifier["failed_when"] is False
    assert classifier["ansible.builtin.command"]["stdin_add_newline"] is False
    assert "_agw_validation_safe_category" in refusal_message
    for protected_result in ("_agw_validation_diagnostic", "_agw_config_validation"):
        assert protected_result not in refusal_message
    assert "argv" not in refusal_message
    assert refusal_message.count("default('") == 1
    assert refusal_message.count("', true)") == 1
    assert validation_refusal["ansible.builtin.assert"]["that"] == ["_agw_config_validation.rc == 0"]
    normalize = next(
        task for task in tasks
        if task.get("name") == "Normalize validator diagnostics to a protected allowlisted category"
    )
    assert normalize["no_log"] is True
    assert normalize["ansible.builtin.set_fact"]["_agw_validation_safe_category"]
    argv = str(validate["ansible.builtin.command"]["argv"])
    assert "--validate-only" in argv and "/config.yaml" in argv
    assert "--volume" in argv and "/certs:/certs:ro" in argv
    assert "--userns=keep-id:uid=65532,gid=65532" in argv
    assert "SSL_CERT_FILE=/certs/step-ca-bundle.crt" in argv
    assert validate["no_log"] is True
    assert "'run', '--pull=never', '--rm'" in argv
    assert "'--network=none'" not in argv
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


def test_validator_failure_diagnostics_use_only_fixed_exit_code_categories():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    phase_one = next(play for play in plays if play.get("name", "").startswith("Phase 1:"))
    classifier = next(
        task
        for task in phase_one["tasks"]
        if task.get("name") == "Classify the validator result without exposing its output"
    )
    program = classifier["ansible.builtin.command"]["argv"][2]
    cases = [
        (0, "accepted", "usage: secret-shaped-token ABC123 must not affect an accepted result"),
        (125, "container-runtime-error", "permission denied secret-path"),
        (126, "validator-not-executable", ""),
        (127, "validator-command-not-found", ""),
        (1, "config-schema-error", "unknown field private-config-secret"),
        (2, "file-mount-permission-error", "permission denied /private/secret"),
        (3, "cli-argument-error", "unknown flag --secret-value"),
        (4, "network-dependency-error", "connection refused https://secret.invalid"),
        (5, "validator-nonzero-exit-other", "opaque-private-token-value"),
        (1, "validator-nonzero-exit-1", "opaque private-token-ABC123.example.internal"),
        (6, "config-type-variant-error", "expected a sequence for private-token-ABC123"),
        (7, "environment-reference-error", "environment variable private-token-ABC123 is not set"),
        (7, "environment-reference-error", "error looking key private-token-ABC123 up: environment variable not found"),
        (
            7,
            "environment-reference-error",
            "failed to parse config: error looking key private-token-ABC123 up: environment variable not found",
        ),
        (8, "invalid-endpoint-error", "invalid endpoint https://private-token-ABC123.invalid/path"),
        (8, "invalid-endpoint-error", "invalid URI private-token-ABC123.invalid/path"),
        (8, "invalid-endpoint-error", "invalid host:port: private-token-ABC123.example.internal"),
        (8, "invalid-endpoint-error", "failed to parse URL: https://private-token-ABC123.invalid/path"),
        (8, "invalid-endpoint-error", "malformed URI private-token-ABC123.invalid/path"),
        (8, "invalid-endpoint-error", "URL parse error: private-token-ABC123.invalid/path"),
        (9, "config-type-variant-error", "unknown field uri; invalid type for tracing"),
        (9, "config-schema-error", "unknown field x, expected one of a,b,c"),
        (9, "config-schema-error", "unknown field certificate; invalid configuration"),
        (9, "config-schema-error", "unknown field trust bundle; failed to parse configuration"),
        (9, "config-schema-error", "unknown field URI; invalid configuration"),
        (9, "config-type-variant-error", "environment variable; invalid type is missing"),
        (8, "config-schema-error", "error during startup: invalid configuration private-token-ABC123"),
        (9, "config-schema-error", "failed to parse YAML private-config-secret"),
        (9, "config-type-variant-error", "failed to parse config: invalid type for private-token-ABC123"),
        (10, "resource-certificate-load-error", "failed to load certificate private-token-ABC123.pem"),
        (
            10,
            "file-mount-permission-error",
            "failed to open file /certs/private-token-ABC123/key: permission denied",
        ),
        (10, "validator-nonzero-exit-other", "no such file or directory"),
        (-1, "validator-result-unavailable", ""),
    ]
    for rc, category, diagnostic in cases:
        encoded_input = base64.b64encode(
            json.dumps({"rc": rc, "stdout": diagnostic, "stderr": ""}).encode()
        ).decode()
        result = subprocess.run(
            [sys.executable, "-c", program],
            input=encoded_input,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == f"{category}|{rc}"
        if diagnostic:
            assert diagnostic not in result.stdout + result.stderr
            assert "private-token-ABC123" not in result.stdout + result.stderr

    invalid_exit_code = subprocess.run(
        [sys.executable, "-c", program],
        input=base64.b64encode(json.dumps({"rc": 256, "stdout": "secret"}).encode()).decode(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_exit_code.returncode == 0
    assert invalid_exit_code.stdout.strip() == "diagnostic-unavailable|-1"

    oversized_diagnostic = "x" * 3_200_000
    oversized_input = base64.b64encode(
        json.dumps({"rc": 1, "stdout": oversized_diagnostic}).encode()
    ).decode()
    oversized_result = subprocess.run(
        [sys.executable, "-c", program],
        input=oversized_input,
        capture_output=True,
        text=True,
        check=False,
    )
    assert oversized_result.returncode == 0
    assert oversized_result.stdout.strip() == "diagnostic-unavailable|-1"
    assert oversized_diagnostic not in oversized_result.stdout + oversized_result.stderr

    invalid_input = subprocess.run(
        [sys.executable, "-c", program], input="not-base64-json", capture_output=True, text=True, check=False
    )
    assert invalid_input.returncode == 0
    assert invalid_input.stdout.strip() == "diagnostic-unavailable|-1"


def test_validator_diagnostic_b64_stdin_round_trips_through_ansible_templar(tmp_path):
    ansible_playbook = shutil.which("ansible-playbook")
    if not ansible_playbook:
        pytest.skip("ansible-playbook is unavailable")
    ansible_python = Path(ansible_playbook).read_text().splitlines()[0].removeprefix("#!")
    harness = r'''
import json
import subprocess
import sys
from ansible._internal._datatag._tags import TrustedAsTemplate
from ansible.module_utils._internal._datatag import AnsibleTagHelper
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

playbook_path = sys.argv[1]
payload = json.loads(sys.stdin.read())
loader = DataLoader()
plays = loader.load_from_file(playbook_path)
tasks = next(
    play for play in plays if play.get('name', '').startswith('Phase 1:')
)['tasks']
classifier = next(
    task for task in tasks
    if task.get('name') == 'Classify the validator result without exposing its output'
)
normalizer = next(
    task for task in tasks
    if task.get('name') == 'Normalize validator diagnostics to a protected allowlisted category'
)
variables = {'_agw_config_validation': payload}
templar = Templar(loader=loader, variables=variables)
stdin_template = classifier['ansible.builtin.command']['stdin']
stdin_template = AnsibleTagHelper.tag(stdin_template, TrustedAsTemplate())
encoded_payload = templar.template(stdin_template)
stdin_bytes = encoded_payload.encode()
if classifier['ansible.builtin.command'].get('stdin_add_newline', True):
    stdin_bytes += b'\n'
classified = subprocess.run(
    classifier['ansible.builtin.command']['argv'], input=stdin_bytes,
    capture_output=True, check=False,
)
if classified.returncode != 0:
    raise SystemExit(2)
variables['_agw_validation_diagnostic'] = {'stdout': classified.stdout.decode()}
normalizer_template = normalizer['ansible.builtin.set_fact']['_agw_validation_safe_category']
normalizer_template = AnsibleTagHelper.tag(normalizer_template, TrustedAsTemplate())
category = templar.template(normalizer_template).strip()
if category != payload['expected_category']:
    raise SystemExit(3)
print(category)
    '''
    ansible_temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="agw-ansible-templar-", dir=ansible_temp_root) as ansible_tmp:
        env = os.environ.copy() | {
            "ANSIBLE_LOCAL_TEMP": ansible_tmp,
            "ANSIBLE_REMOTE_TEMP": ansible_tmp,
        }
        cases = (
            (0, "usage: private-token-ABC123.example.internal", "accepted"),
            (6, "expected a sequence for private-token-ABC123", "config-type-variant-error"),
        )
        for rc, secret_shaped_output, expected_category in cases:
            result = subprocess.run(
                [ansible_python, "-c", harness, str(PLAYBOOK)],
                cwd=tmp_path,
                env=env,
                input=json.dumps({
                    "rc": rc,
                    "stdout": secret_shaped_output,
                    "stderr": "",
                    "expected_category": expected_category,
                }),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, "Ansible templar failed to normalize validator metadata"
            assert result.stdout.strip() == expected_category
            assert secret_shaped_output not in result.stdout + result.stderr
            assert "private-token-ABC123" not in result.stdout + result.stderr


def test_checkout_diagnostics_report_only_fixed_status_categories_and_counts(tmp_path):
    plays = yaml.safe_load(VERIFY_PLAYBOOK.read_text())
    gateway = next(play for play in plays if play.get("hosts") == "agentgateway_svc")
    status_tasks = [
        task
        for task in gateway["tasks"]
        if task.get("name") in {
            "Read checkout change categories as bounded safe metadata",
            "Reread checkout change categories as safe metadata",
        }
    ]
    assert len(status_tasks) == 2
    shared_argv = gateway["vars"]["_agw_checkout_status_argv"]
    assert all(task["ansible.builtin.command"]["argv"] == shared_argv for task in status_tasks)
    assert "import subprocess" in VERIFY_PLAYBOOK.read_text()
    assert VERIFY_PLAYBOOK.read_text().count("import subprocess") == 1
    checkout_assertions = [
        task
        for task in gateway["tasks"]
        if task.get("name") in {
            "Require a clean reviewed gateway checkout",
            "Require the checkout to remain clean at receipt completion",
            "Require checkout status diagnostics to be available",
            "Require checkout status diagnostics to remain available",
        }
    ]
    assert len(checkout_assertions) == 4
    default_filter = Environment().from_string("{{ value | default(FALLBACK, true) }}")
    for task in checkout_assertions:
        assertion = task["ansible.builtin.assert"]
        expressions = assertion["that"] + [assertion["fail_msg"]]
        for expression in expressions:
            if "from_json" not in expression:
                continue
            fallbacks = re.findall(r"default\('([^']+)', true\)", expression)
            assert len(fallbacks) == expression.count("from_json")
            for fallback in fallbacks:
                parsed = json.loads(fallback)
                assert parsed["status"] == "error"
                assert parsed["untracked_categories"] == {}
                assert default_filter.render(value="", FALLBACK=fallback) == fallback
    programs = [task["ansible.builtin.command"]["argv"][2] for task in status_tasks]
    assert programs[0] == programs[1]

    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = _isolated_git_environment()

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=repo, env=git_env, capture_output=True, check=check
        )

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    odd_tracked = repo / "docs" / "line\nbreak.md"
    ordinary_tracked = repo / "docs" / "ordinary-file.md"
    odd_tracked.parent.mkdir()
    odd_tracked.write_text("committed-content\n")
    ordinary_tracked.write_text("committed-content\n")
    git("add", "--", "docs")
    git("commit", "-qm", "baseline", check=True)
    odd_tracked.write_text("changed-content-must-not-appear\n")
    ordinary_tracked.write_text("changed-content-must-not-appear\n")
    untracked = repo / "new\nfile.txt"
    untracked.write_text("untracked-content-must-not-appear\n")
    (repo / "unsafe?secret.txt").write_text("unsafe-name-content-must-not-appear\n")
    (repo / ("x" * 201 + ".txt")).write_text("long-name-content-must-not-appear\n")
    secret_like_name = "private-token-ABC123.example.internal"
    (repo / secret_like_name).write_text("secret-like-name-content-must-not-appear\n")

    for program in programs:
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=repo, env=git_env,
            capture_output=True, text=True, check=False
        )
        assert result.returncode == 0
        assert result.stderr == ""
        assert result.stdout.count("\n") == 1
        report = json.loads(result.stdout)
        assert report == {
            "status": "ok",
            "counts": {
                "modified": 2,
                "added": 0,
                "deleted": 0,
                "renamed": 0,
                "copied": 0,
                "conflicted": 0,
                "type_changed": 0,
                "untracked": 4,
            },
            "untracked_categories": {
                "gateway_certificate_tree": 0,
                "gateway_deployment_other": 0,
                "other_service_deployment": 0,
                "service_tree_other": 0,
                "platform_other": 0,
                "repository_other": 4,
            },
            "path_count": 6,
        }
        assert "paths" not in report
        assert "committed-content" not in result.stdout
        assert "changed-content-must-not-appear" not in result.stdout
        assert "untracked-content-must-not-appear" not in result.stdout
        assert "line\\nbreak.md" not in result.stdout
        assert "unsafe?secret.txt" not in result.stdout
        assert "x" * 201 not in result.stdout
        assert secret_like_name not in result.stdout
        assert "secret-like-name-content-must-not-appear" not in result.stdout

    bulk = repo / "bulk"
    bulk.mkdir()
    for index in range(55):
        (bulk / f"{index:02}.txt").write_text("bulk-content-must-not-appear\n")
    bounded = subprocess.run(
        [sys.executable, "-c", programs[0]], cwd=repo, env=git_env, capture_output=True, text=True, check=False
    )
    assert bounded.returncode == 0
    bounded_report = json.loads(bounded.stdout)
    assert bounded_report["counts"]["untracked"] == 59
    assert bounded_report["path_count"] == 61
    assert "paths" not in bounded_report
    assert "bulk-content-must-not-appear" not in bounded.stdout


def test_checkout_diagnostics_fail_closed_with_static_error_metadata(tmp_path):
    plays = yaml.safe_load(VERIFY_PLAYBOOK.read_text())
    gateway = next(play for play in plays if play.get("hosts") == "agentgateway_svc")
    status_task = next(
        task
        for task in gateway["tasks"]
        if task.get("name") == "Read checkout change categories as bounded safe metadata"
    )
    program = status_task["ansible.builtin.command"]["argv"][2]
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, env=_isolated_git_environment(),
        capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "error",
        "counts": {},
        "untracked_categories": {},
        "path_count": 0,
        "reason": "git-status-failed",
    }
    assert str(tmp_path) not in result.stdout


def test_checkout_diagnostics_classify_untracked_locations_without_exposing_paths(tmp_path):
    plays = yaml.safe_load(VERIFY_PLAYBOOK.read_text())
    gateway = next(play for play in plays if play.get("hosts") == "agentgateway_svc")
    program = gateway["vars"]["_agw_checkout_status_argv"][2]
    repo = tmp_path / "classification-repo"
    repo.mkdir()
    git_env = _isolated_git_environment()

    subprocess.run(["git", "init", "-q"], cwd=repo, env=git_env, check=True)
    gateway_deploy = repo / "platform/services/agentgateway/deployment"
    gateway_deploy.mkdir(parents=True)
    shutil.copy2(ROOT / ".gitignore", repo / ".gitignore")
    shutil.copy2(
        ROOT / "platform/services/agentgateway/deployment/.gitignore",
        gateway_deploy / ".gitignore",
    )
    subprocess.run(
        ["git", "add", "--", ".gitignore", "platform/services/agentgateway/deployment/.gitignore"],
        cwd=repo, env=git_env, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "baseline"],
        cwd=repo, env=git_env, check=True,
    )
    ignored_paths = (
        "platform/services/agentgateway/deployment/.env",
        "platform/services/agentgateway/deployment/config.yaml",
        "platform/services/agentgateway/deployment/config.yaml.previous",
        "platform/services/agentgateway/deployment/config.yaml.replaced",
        "platform/services/agentgateway/deployment/certs/agw-server/current/cert.pem",
        "platform/services/agentgateway/deployment/certs/agw-server/current/key.pem",
        "platform/services/agentgateway/deployment/certs/step-ca-bundle.crt",
        "platform/services/agentgateway/deployment/certs/private-hostname-ABC123.crt",
        "platform/services/agentgateway/deployment/certs/private-token-ABC123.key",
    )
    for relative_path in ignored_paths:
        destination = repo / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("UNIQUE-IGNORED-RENDERED-CONTENT")
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--", relative_path],
            cwd=repo, env=git_env, check=False,
        )
        assert ignored.returncode == 0
    paths = {
        "platform/services/agentgateway/deployment/debug/private-token-ABC123.example.internal.py": (
            "UNIQUE-GATEWAY-DEPLOYMENT-CONTENT"
        ),
        "platform/services/agentgateway/deployment/certs-escape/private-token-ABC123.crt": (
            "UNIQUE-GATEWAY-DEPLOYMENT-CONTENT"
        ),
        "platform/services/o11y/deployment/private-token-ABC123.example.internal.txt": (
            "UNIQUE-OTHER-DEPLOYMENT-CONTENT"
        ),
        "platform/services/o11y/context/private-hostname-ABC123.txt": "UNIQUE-OTHER-SERVICE-CONTENT",
        "platform/services/agentgateway/debug/private-hostname-ABC123.txt": "UNIQUE-GATEWAY-TREE-CONTENT",
        "platform/services/o11y/legacy/adapter/deployment/private-token-ABC123.txt": (
            "UNIQUE-NESTED-DEPLOYMENT-CONTENT"
        ),
        "platform/inventory/private-hostname-ABC123.txt": "UNIQUE-PLATFORM-CONTENT",
        "local/private-token-ABC123.example.internal.txt": "UNIQUE-REPOSITORY-CONTENT",
    }
    for relative_path, contents in paths.items():
        destination = repo / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents)

    result = subprocess.run(
        [sys.executable, "-c", program], cwd=repo, env=git_env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    assert report["counts"]["untracked"] == len(paths)
    assert report["untracked_categories"] == {
        "gateway_certificate_tree": 0,
        "gateway_deployment_other": 2,
        "other_service_deployment": 1,
        "service_tree_other": 3,
        "platform_other": 1,
        "repository_other": 1,
    }
    assert report["path_count"] == len(paths)
    for private_value in (*paths.keys(), *paths.values(), *ignored_paths, str(repo), "UNIQUE-IGNORED-RENDERED-CONTENT"):
        assert private_value not in result.stdout


def test_checkout_diagnostics_bound_git_output_and_runtime_with_static_metadata(tmp_path):
    plays = yaml.safe_load(VERIFY_PLAYBOOK.read_text())
    gateway = next(play for play in plays if play.get("hosts") == "agentgateway_svc")
    program = gateway["vars"]["_agw_checkout_status_argv"][2]
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import time\n"
        "with open(os.environ['FAKE_GIT_PID_FILE'], 'w') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "if os.environ['FAKE_GIT_MODE'] == 'timeout':\n"
        "    time.sleep(30)\n"
        "elif os.environ['FAKE_GIT_MODE'] == 'exact':\n"
        "    prefix = b'?? repository-other/'\n"
        "    payload = prefix + b'x' * (1048576 - len(prefix) - 1) + b'\\0'\n"
        "    view = memoryview(payload)\n"
        "    while view:\n"
        "        view = view[os.write(1, view):]\n"
        "else:\n"
        "    block = b'x' * 65536\n"
        "    while True:\n"
        "        os.write(1, block)\n"
    )
    fake_git.chmod(0o755)

    def run_fake_git(mode):
        pid_file = tmp_path / f"fake-git-{mode}.pid"
        env = _isolated_git_environment()
        env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
        env["FAKE_GIT_MODE"] = mode
        env["FAKE_GIT_PID_FILE"] = str(pid_file)
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=tmp_path, env=env,
            capture_output=True, text=True, check=False,
        )
        assert pid_file.exists()
        return result, int(pid_file.read_text())

    result, exact_pid = run_fake_git("exact")
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "ok",
        "counts": {
            "modified": 0,
            "added": 0,
            "deleted": 0,
            "renamed": 0,
            "copied": 0,
            "conflicted": 0,
            "type_changed": 0,
            "untracked": 1,
        },
        "untracked_categories": {
            "gateway_certificate_tree": 0,
            "gateway_deployment_other": 0,
            "other_service_deployment": 0,
            "service_tree_other": 0,
            "platform_other": 0,
            "repository_other": 1,
        },
        "path_count": 1,
    }
    with pytest.raises(ProcessLookupError):
        os.kill(exact_pid, 0)

    result, oversized_pid = run_fake_git("oversized")
    assert result.returncode == 1
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "error",
        "counts": {},
        "untracked_categories": {},
        "path_count": 0,
        "reason": "git-status-failed",
    }
    with pytest.raises(ProcessLookupError):
        os.kill(oversized_pid, 0)

    result, timed_out_pid = run_fake_git("timeout")
    assert result.returncode == 1
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "error",
        "counts": {},
        "untracked_categories": {},
        "path_count": 0,
        "reason": "git-status-failed",
    }
    with pytest.raises(ProcessLookupError):
        os.kill(timed_out_pid, 0)


def test_checkout_diagnostics_count_porcelain_renames_copies_and_other_statuses(tmp_path):
    plays = yaml.safe_load(VERIFY_PLAYBOOK.read_text())
    gateway = next(play for play in plays if play.get("hosts") == "agentgateway_svc")
    program = gateway["vars"]["_agw_checkout_status_argv"][2]
    repo = tmp_path / "status-cases"
    repo.mkdir()
    git_env = _isolated_git_environment()

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=repo, env=git_env, capture_output=True, check=check
        )

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    git("config", "status.renames", "copies")
    (repo / "rename-source.txt").write_text("rename payload\n" * 8)
    (repo / "copy-source.txt").write_text("copy payload\n" * 8)
    (repo / "delete-me.txt").write_text("delete payload\n")
    (repo / "type-change.txt").write_text("type payload\n")
    (repo / "conflict.txt").write_text("base conflict payload\n")
    git("add", "-A")
    git("commit", "-qm", "baseline", check=True)

    git("mv", "rename-source.txt", "renamed-target.txt")
    (repo / "copy-target.txt").write_text((repo / "copy-source.txt").read_text())
    git("add", "--", "copy-target.txt")
    (repo / "delete-me.txt").unlink()
    git("add", "-u")
    (repo / "type-change.txt").unlink()
    (repo / "type-change.txt").symlink_to("copy-source.txt")
    git("add", "-A")

    base = git("branch", "--show-current").stdout.decode().strip()
    git("checkout", "-qb", "conflict-side")
    (repo / "conflict.txt").write_text("side conflict payload\n")
    git("commit", "-qam", "side", check=True)
    git("checkout", "-q", base)
    (repo / "conflict.txt").write_text("main conflict payload\n")
    git("commit", "-qam", "main", check=True)
    merge = git("merge", "conflict-side", check=False)
    assert merge.returncode != 0

    result = subprocess.run(
        [sys.executable, "-c", program], cwd=repo, env=git_env,
        capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report == {
        "status": "ok",
        "counts": {
            "modified": 0,
            "added": 1,
            "deleted": 1,
            "renamed": 1,
            "copied": 0,
            "conflicted": 1,
            "type_changed": 1,
            "untracked": 0,
        },
        "untracked_categories": {
            "gateway_certificate_tree": 0,
            "gateway_deployment_other": 0,
            "other_service_deployment": 0,
            "service_tree_other": 0,
            "platform_other": 0,
            "repository_other": 0,
        },
        "path_count": 5,
    }
    assert all(
        name not in result.stdout
        for name in ("rename-source.txt", "renamed-target.txt", "copy-source.txt", "copy-target.txt")
    )


def test_checkout_diagnostics_skip_both_paths_in_porcelain_rename_and_copy_records(tmp_path):
    plays = yaml.safe_load(VERIFY_PLAYBOOK.read_text())
    gateway = next(play for play in plays if play.get("hosts") == "agentgateway_svc")
    program = gateway["vars"]["_agw_checkout_status_argv"][2]
    porcelain = b"R  safe-new-name\0safe-old-name\0C  safe-copy-name\0safe-copy-source\0"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        f"os.write(1, {porcelain!r})\n"
    )
    fake_git.chmod(0o755)
    env = _isolated_git_environment()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path,
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "status": "ok",
        "counts": {
            "modified": 0,
            "added": 0,
            "deleted": 0,
            "renamed": 1,
            "copied": 1,
            "conflicted": 0,
            "type_changed": 0,
            "untracked": 0,
        },
        "untracked_categories": {
            "gateway_certificate_tree": 0,
            "gateway_deployment_other": 0,
            "other_service_deployment": 0,
            "service_tree_other": 0,
            "platform_other": 0,
            "repository_other": 0,
        },
        "path_count": 2,
    }
    assert all(name not in result.stdout for name in (
        "safe-new-name", "safe-old-name", "safe-copy-name", "safe-copy-source",
    ))


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("randomSampling", "0"),
        ("randomSampling", "0.2"),
        ("randomSampling", "true"),
        ("clientSampling", "0.2"),
        ("clientSampling", "0.03"),
    ],
)
def test_rendered_config_inspector_rejects_invalid_sampling_values(tmp_path, capsys, field, value):
    helper = _load_helper()
    env = tmp_path / ".env"
    config = tmp_path / "config.yaml"
    env.write_text("AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n")
    config.write_text(RENDERED_CONFIG.replace(f"{field}: 0.05", f"{field}: {value}"))
    import sys
    from unittest.mock import patch

    with patch.object(sys, "stdin", open_json_input(env, config)):
        assert helper.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "refused"
    assert "do-not-print-this" not in json.dumps(report)


def test_rendered_config_inspector_accepts_inclusive_sampling_limit(tmp_path, capsys):
    helper = _load_helper()
    env = tmp_path / ".env"
    config = tmp_path / "config.yaml"
    env.write_text("AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n")
    config.write_text(
        RENDERED_CONFIG.replace("randomSampling: 0.05", "randomSampling: 0.1").replace(
            "clientSampling: 0.05", "clientSampling: 0.1"
        )
    )
    from unittest.mock import patch

    with patch.object(sys, "stdin", open_json_input(env, config)):
        assert helper.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    assert report["random_sampling"] == report["client_sampling"] == 0.1


def test_actual_config_template_render_passes_sampling_inspector(tmp_path, capsys):
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    env.filters.update(
        bool=bool,
        flatten=lambda values: [item for group in values for item in group],
        unique=lambda values: list(dict.fromkeys(values)),
        hash=lambda value, algorithm="sha256": hashlib.new(algorithm, str(value).encode()).hexdigest(),
        to_json=lambda value: json.dumps(value),
    )
    rendered = env.from_string(CONFIG_TEMPLATE.read_text()).render(
        agw_ui_enabled=False,
        agw_listener_tls=False,
        internal_leaves=[],
        agw_otlp_host="alloy.example:4317",
        agw_trace_sampling=0.05,
        agw_clients=["test-client"],
        agw_client_policies={},
        agw_plaintext_keys=False,
        local_mode=False,
        secrets={"client_test-client": "test-only-client-key", "vllm_api_key": "test-only-upstream-key"},
        agw_rate_requests_per_minute_total=120,
        agw_models=[{"name": "test-model"}],
        agw_upstream_base_url="https://model.example/v1",
    )
    yaml.safe_load(rendered)
    env_file = tmp_path / ".env"
    config = tmp_path / "config.yaml"
    env_file.write_text("AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\n")
    config.write_text(rendered)
    helper = _load_helper()
    import sys
    from unittest.mock import patch

    with patch.object(sys, "stdin", open_json_input(env_file, config)):
        assert helper.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    assert report["random_sampling"] == report["client_sampling"] == 0.05
    assert "test-only-client-key" not in json.dumps(report)
    assert "test-only-upstream-key" not in json.dumps(report)


def test_actual_env_template_pin_passes_checker_and_deploy_preflight(tmp_path):
    template_env = Environment(trim_blocks=True, lstrip_blocks=True)
    template_env.filters["bool"] = bool
    rendered = template_env.from_string(ENV_TEMPLATE.read_text()).render(
        secrets={"agw_db_password": "test-only-db-secret", "vllm_api_key": "test-only-upstream-secret"},
        agw_ui_enabled=False,
        agw_listener_tls=False,
        local_mode=False,
    )
    deployment = tmp_path / "platform/services/agentgateway/deployment"
    lib_dir = tmp_path / "platform/lib"
    deployment.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "platform/lib/common.sh", lib_dir / "common.sh")
    shutil.copy2(ROOT / "platform/services/agentgateway/deployment/deploy.sh", deployment / "deploy.sh")
    (deployment / "deploy.sh").chmod(0o755)
    (deployment / ".env").write_text(rendered)
    (deployment / "config.yaml").write_text("binds: []\n")

    checker = subprocess.run(
        [sys.executable, str(IMAGE_HELPER), str(deployment / ".env")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checker.returncode == 0
    assert json.loads(checker.stdout) == {
        "status": "pass",
        "image": "cr.agentgateway.dev/agentgateway:v1.5.0",
    }

    capture = tmp_path / "compose-capture"
    compose = tmp_path / "compose-stub"
    compose.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "${AGW_IMAGE:-<unset>}" > "$COMPOSE_CAPTURE"\n'
        'printf "%s\\n" "$*" > "$COMPOSE_ARGS_CAPTURE"\n'
    )
    compose.chmod(0o755)
    process_env = os.environ.copy()
    process_env.update(
        {
            "CONTAINER_ENGINE": "docker",
            "COMPOSE_CMD": str(compose),
            "COMPOSE_CAPTURE": str(capture),
            "COMPOSE_ARGS_CAPTURE": str(tmp_path / "compose-args"),
            "AGW_IMAGE": "cr.example/inherited-override:9",
        }
    )
    deployment_run = subprocess.run(
        ["bash", str(deployment / "deploy.sh"), "--pull-only"],
        cwd=deployment,
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert deployment_run.returncode == 0
    assert capture.read_text().strip() == "cr.agentgateway.dev/agentgateway:v1.5.0"
    assert (tmp_path / "compose-args").read_text().strip().endswith("pull")
    assert "test-only-db-secret" not in deployment_run.stdout + deployment_run.stderr
    assert "test-only-upstream-secret" not in deployment_run.stdout + deployment_run.stderr


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
