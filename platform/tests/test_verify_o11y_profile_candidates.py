"""Static contract and sanitized-runtime tests for the profile preflight."""

import hashlib
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/verify-o11y-profile-candidates.py"
PLAYBOOK = ROOT / "platform/playbooks/verify-o11y-profile-pilot.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
ALLOY_TEMPLATE = ROOT / "platform/services/o11y/deployment/templates/config.alloy.j2"
SPEC = importlib.util.spec_from_file_location("verify_o11y_profile_candidates", HELPER)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def configs():
    profiles = """
pyroscope.write "private" {
  endpoint {
    url = "http://pyroscope:4040"
  }
  external_labels = { cluster = "test", environment = "dev" }
}

pyroscope.scrape "alloy_self_profile" {
  job_name = "o11y/alloy"
  targets = [
    { "__address__" = "alloy:12345", "service_name" = "o11y/alloy", "target_id" = "service:o11y" },
  ]
  scrape_interval = "60s"
  scrape_timeout = "10s"
  delta_profiling_duration = "5s"
  follow_redirects = false
  proxy_from_environment = false
  forward_to = [pyroscope.write.private.receiver]
  profiling_config {
    profile.process_cpu { enabled = true }
    profile.memory { enabled = false }
    profile.block { enabled = false }
    profile.goroutine { enabled = false }
    profile.mutex { enabled = false }
    profile.fgprof { enabled = false }
    profile.godeltaprof_memory { enabled = false }
    profile.godeltaprof_mutex { enabled = false }
    profile.godeltaprof_block { enabled = false }
  }
}
"""
    baseline = 'loki.write "default" {}\n'
    return baseline, baseline + profiles


def render_alloy_template(profile_enabled):
    from jinja2 import Environment

    env = Environment(keep_trailing_newline=True)
    env.filters["bool"] = bool
    env.filters["to_json"] = json.dumps
    template = env.from_string(ALLOY_TEMPLATE.read_text(encoding="utf-8"))
    play = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))[-1]
    assert play["vars_files"] == ["vars/o11y-metric-labels.yml"]
    loaded_vars = {}
    for vars_file in play["vars_files"]:
        loaded_vars.update(yaml.safe_load((PLAYBOOK.parent / vars_file).read_text(encoding="utf-8")))
    return template.render(
        **loaded_vars,
        local_mode=False,
        o11y_cluster="test-cluster",
        o11y_environment="dev",
        o11y_gateway_span_logs_enabled=False,
        o11y_alloy_profile_pilot_enabled=profile_enabled,
    )


def test_candidate_structure_accepts_only_cpu_self_profile():
    disabled, enabled = configs()

    result = CHECK.validate_candidates(disabled, enabled)

    assert result == {
        "target_count": 1,
        "profile_type_count": 9,
        "profile_types": [
            "process_cpu", "memory", "block", "goroutine", "mutex", "fgprof",
            "godeltaprof_memory", "godeltaprof_mutex", "godeltaprof_block",
        ],
    }


def test_rendered_template_candidates_match_outside_profile_blocks():
    disabled = render_alloy_template(False)
    enabled = render_alloy_template(True)

    result = CHECK.validate_candidates(disabled, enabled)

    assert result["target_count"] == 1
    assert result["profile_type_count"] == 9


def test_candidate_structure_refuses_enabled_only_change_outside_profile_blocks():
    disabled = render_alloy_template(False)
    enabled = render_alloy_template(True).replace(
        'environment = "dev"', 'environment = "prod"', 1
    )

    with pytest.raises(CHECK.Refusal, match="candidate_invalid"):
        CHECK.validate_candidates(disabled, enabled)


@pytest.mark.parametrize(
    "replacement",
    [
        ('"__address__" = "alloy:12345"', '"__address__" = "other:12345"'),
        ('"service_name" = "o11y/alloy"', '"service_name" = "other"'),
        ('profile.process_cpu { enabled = true }', 'profile.process_cpu { enabled = false }'),
        ('profile.memory { enabled = false }', 'profile.memory { enabled = true }'),
        ('scrape_interval = "60s"', 'scrape_interval = "30s"'),
        ('follow_redirects = false', 'follow_redirects = true'),
        ('url = "http://pyroscope:4040"', 'url = "https://example.invalid"'),
    ],
)
def test_candidate_structure_refuses_unapproved_target_or_profile(replacement):
    disabled, enabled = configs()
    old, new = replacement
    assert old in enabled
    enabled = enabled.replace(old, new, 1)

    with pytest.raises(CHECK.Refusal, match="candidate_invalid"):
        CHECK.validate_candidates(disabled, enabled)


def test_candidate_structure_refuses_extra_target_or_profile_component():
    disabled, enabled = configs()
    enabled = enabled.replace(
        '"target_id" = "service:o11y"',
        '"target_id" = "service:o11y" }, { "__address__" = "extra:12345", '
        '"service_name" = "extra", "target_id" = "service:extra"',
        1,
    )

    with pytest.raises(CHECK.Refusal, match="candidate_invalid"):
        CHECK.validate_candidates(disabled, enabled)


def test_balanced_block_parser_ignores_braces_in_comments_and_strings():
    disabled, enabled = configs()
    enabled += '\n// } pyroscope.scrape "fake" {\nlocal.file "brace" { path = "}" }\n'

    assert set(CHECK._profile_blocks(enabled)) == {
        "pyroscope.write.private", "pyroscope.scrape.alloy_self_profile",
    }


def test_runtime_receipt_is_fixed_and_does_not_claim_sampling(monkeypatch):
    disabled, enabled = configs()
    monkeypatch.setattr(
        CHECK, "_image_identity",
        lambda: ("sha256:" + "a" * 64, "docker.io/grafana/alloy@sha256:" + "b" * 64),
    )
    monkeypatch.setattr(CHECK, "_live_receiver_image_identity", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(CHECK, "_receiver_scrape_environment", lambda: scrape_env())
    monkeypatch.setattr(
        CHECK, "_component_states",
        lambda _config, _image, _env, *, disabled: "loaded_without_profile" if disabled else "healthy",
    )

    receipt = CHECK.validate_runtime({"disabled": disabled, "enabled": enabled})

    assert receipt["status"] == "validated"
    assert receipt["network"] == "none"
    assert receipt["pull"] == "never"
    assert receipt["sampling_or_delivery"] == "not_proven"
    assert receipt["persistence"] == "not_proven"
    assert receipt["image_parity"] == "verified"
    assert receipt["live_receiver_image_id"] == receipt["image_id"]
    assert receipt["disabled_config_sha256"] == hashlib.sha256(disabled.encode()).hexdigest()
    assert receipt["enabled_config_sha256"] == hashlib.sha256(enabled.encode()).hexdigest()
    assert "raw_config" not in receipt and "logs" not in receipt


def test_runtime_refuses_unhealthy_profile_component(monkeypatch):
    disabled, enabled = configs()
    monkeypatch.setattr(
        CHECK, "_image_identity",
        lambda: ("sha256:" + "a" * 64, "docker.io/grafana/alloy@sha256:" + "b" * 64),
    )
    monkeypatch.setattr(CHECK, "_live_receiver_image_identity", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(CHECK, "_receiver_scrape_environment", lambda: scrape_env())
    monkeypatch.setattr(
        CHECK, "_component_states",
        lambda _config, _image, _env, *, disabled: "loaded_without_profile" if disabled else "unhealthy",
    )

    with pytest.raises(CHECK.Refusal, match="component_unhealthy"):
        CHECK.validate_runtime({"disabled": disabled, "enabled": enabled})


def test_runtime_refuses_candidate_image_that_differs_from_live_receiver(monkeypatch):
    disabled, enabled = configs()
    monkeypatch.setattr(
        CHECK, "_image_identity",
        lambda: ("sha256:" + "a" * 64, "docker.io/grafana/alloy@sha256:" + "b" * 64),
    )
    monkeypatch.setattr(CHECK, "_live_receiver_image_identity", lambda: "sha256:" + "b" * 64)

    with pytest.raises(CHECK.Refusal, match="image_mismatch"):
        CHECK.validate_runtime({"disabled": disabled, "enabled": enabled})


def test_live_receiver_image_identity_uses_fixed_read_only_inspect(monkeypatch):
    calls = []

    def bounded(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0, ("a" * 64 + " running\n").encode()

    monkeypatch.setattr(CHECK, "_bounded_command", bounded)

    assert CHECK._live_receiver_image_identity() == "sha256:" + "a" * 64
    assert calls == [(
        [
            "podman", "container", "inspect", "o11y-alloy", "--format",
            "{{.Image}} {{.State.Status}}",
        ],
        {"timeout": CHECK.IMAGE_TIMEOUT, "max_bytes": 512},
    )]


def scrape_env():
    return {
        "O11Y_SCRAPE_SAMPLE_LIMIT": "2000",
        "O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES": "16777216",
        "O11Y_SCRAPE_LABEL_LIMIT": "64",
        "O11Y_SCRAPE_LABEL_NAME_LENGTH_LIMIT": "128",
        "O11Y_SCRAPE_LABEL_VALUE_LENGTH_LIMIT": "2048",
    }


@pytest.mark.parametrize("image_id", ["a" * 64, "sha256:" + "a" * 64])
def test_cached_image_inspect_normalizes_id_and_preserves_repo_digest(monkeypatch, image_id):
    repo_digest = "docker.io/grafana/alloy@sha256:" + "b" * 64
    monkeypatch.setattr(
        CHECK, "_bounded_command",
        lambda *_args, **_kwargs: (0, f'{image_id} ["{repo_digest}"]\n'.encode()),
    )

    assert CHECK._image_identity() == ("sha256:" + "a" * 64, repo_digest)


def test_receiver_environment_projects_only_the_five_bounded_scrape_values(monkeypatch):
    calls = []
    entries = [f"{key}={value}" for key, value in scrape_env().items()]
    entries.append("PRIVATE_TOKEN=not-forwarded")

    def bounded(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0, json.dumps(entries).encode()

    monkeypatch.setattr(CHECK, "_bounded_command", bounded)

    assert CHECK._receiver_scrape_environment() == scrape_env()
    assert calls == [(
        ["podman", "container", "inspect", "o11y-alloy", "--format", "{{json .Config.Env}}"],
        {"timeout": CHECK.IMAGE_TIMEOUT, "max_bytes": 16_384},
    )]


@pytest.mark.parametrize("key", CHECK.SCRAPE_ENV_LIMITS)
def test_receiver_scrape_environment_refuses_each_missing_value(key, monkeypatch):
    entries = [f"{name}={value}" for name, value in scrape_env().items() if name != key]
    monkeypatch.setattr(CHECK, "_bounded_command", lambda *_args, **_kwargs: (0, json.dumps(entries).encode()))

    with pytest.raises(CHECK.Refusal, match="candidate_invalid"):
        CHECK._receiver_scrape_environment()


@pytest.mark.parametrize("key", CHECK.SCRAPE_ENV_LIMITS)
def test_receiver_scrape_environment_refuses_each_malformed_value(key, monkeypatch):
    entries = [f"{name}={value}" for name, value in scrape_env().items() if name != key]
    entries.append(f"{key}=malformed")
    monkeypatch.setattr(CHECK, "_bounded_command", lambda *_args, **_kwargs: (0, json.dumps(entries).encode()))

    with pytest.raises(CHECK.Refusal, match="candidate_invalid"):
        CHECK._receiver_scrape_environment()


@pytest.mark.parametrize("key,limit", CHECK.SCRAPE_ENV_LIMITS.items())
def test_receiver_scrape_environment_refuses_each_out_of_range_value(key, limit, monkeypatch):
    entries = [f"{name}={value}" for name, value in scrape_env().items() if name != key]
    entries.append(f"{key}={limit + 1}")
    monkeypatch.setattr(CHECK, "_bounded_command", lambda *_args, **_kwargs: (0, json.dumps(entries).encode()))

    with pytest.raises(CHECK.Refusal, match="candidate_invalid"):
        CHECK._receiver_scrape_environment()


def test_candidate_argv_is_immutable_isolated_and_has_exact_scrape_environment():
    command = CHECK._candidate_command(
        "o11y-profile-candidate-abc123", "docker.io/grafana/alloy@sha256:" + "a" * 64, scrape_env()
    )

    assert "--log-driver=none" in command
    assert "--network=none" in command
    assert "--add-host=alloy:127.0.0.1" in command
    assert "--server.http.disable-support-bundle" in command
    assert "--disable-support-bundle" not in command
    assert command.count("--env") == 5
    passed = [command[index + 1] for index, arg in enumerate(command[:-1]) if arg == "--env"]
    assert passed == [f"{key}={value}" for key, value in scrape_env().items()]
    assert command[command.index("--user=0:0") + 1] == "docker.io/grafana/alloy@sha256:" + "a" * 64


def test_disabled_api_evidence_requires_baseline_and_profile_absence():
    assert CHECK._project_disabled_components([{"localID": CHECK.BASELINE_COMPONENT}]) == "loaded_without_profile"
    for components in ([], [{"localID": CHECK.PROFILE_COMPONENT}], [{"localID": "x"}]):
        with pytest.raises(CHECK.Refusal, match="component_status_invalid"):
            CHECK._project_disabled_components(components)


def test_component_api_health_is_required_and_reduced_to_one_state():
    assert CHECK._project_component_health(
        [{"localID": "pyroscope.scrape.alloy_self_profile", "health": {"state": "healthy"}}],
        "pyroscope.scrape.alloy_self_profile",
    ) == "healthy"
    with pytest.raises(CHECK.Refusal, match="component_status_invalid"):
        CHECK._project_component_health([], "pyroscope.scrape.alloy_self_profile")
    with pytest.raises(CHECK.Refusal, match="component_status_invalid"):
        CHECK._project_component_health(
            [
                {"localID": "pyroscope.scrape.alloy_self_profile", "health": {"state": "healthy"}},
                {"localID": "pyroscope.scrape.alloy_self_profile", "health": {"state": "healthy"}},
            ],
            "pyroscope.scrape.alloy_self_profile",
        )


def test_component_api_requires_http_200_and_bounded_json_shape():
    body = json.dumps([{"localID": "pyroscope.scrape.alloy_self_profile", "health": "healthy"}]).encode()
    assert CHECK._parse_component_response(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json", body,
    ) == json.loads(body)
    with pytest.raises(CHECK.Refusal, match="component_status_invalid"):
        CHECK._parse_component_response(
            b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json", body,
        )
    too_many = json.dumps([{}] * 4097).encode()
    with pytest.raises(CHECK.Refusal, match="component_status_invalid"):
        CHECK._parse_component_response(
            b"HTTP/1.1 200 OK", too_many,
        )


def test_component_api_readiness_retries_until_valid_response(monkeypatch):
    class Process:
        stdin = type("Stdin", (), {"close": lambda *_: None})()
        stdout = None
        pid = 123

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*_args, **_kwargs):
            return 0

    responses = iter([
        (1, b""),
        (
            0,
            b"HTTP/1.1 200 OK\r\n\r\n"
            b'[{"localID":"loki.write.default"}]',
        ),
    ])
    calls = []
    monkeypatch.setattr(CHECK.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(CHECK.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(CHECK, "_transfer_config", lambda *_args: time.monotonic())
    monkeypatch.setattr(CHECK, "_wait_for_startup", lambda *_args: None)
    monkeypatch.setattr(CHECK, "_cleanup_container", lambda _name: None)
    monkeypatch.setattr(CHECK, "READY_SECONDS", 1)
    monkeypatch.setattr(CHECK, "READY_RETRY_SECONDS", 0)

    def probe(*_args, **_kwargs):
        calls.append(True)
        return next(responses)

    monkeypatch.setattr(CHECK, "_bounded_command", probe)

    assert CHECK._component_states(
        "config", "docker.io/grafana/alloy@sha256:" + "a" * 64, scrape_env(), disabled=True,
    ) == "loaded_without_profile"
    assert len(calls) == 2


def test_component_api_readiness_times_out_when_probe_never_succeeds(monkeypatch):
    class Process:
        stdin = type("Stdin", (), {"close": lambda *_: None})()
        stdout = None
        pid = 123

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*_args, **_kwargs):
            return 0

    calls = []
    monkeypatch.setattr(CHECK.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(CHECK.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(CHECK, "_transfer_config", lambda *_args: time.monotonic())
    monkeypatch.setattr(CHECK, "_wait_for_startup", lambda *_args: None)
    monkeypatch.setattr(CHECK, "_cleanup_container", lambda _name: None)
    monkeypatch.setattr(CHECK, "READY_SECONDS", 0.01)
    monkeypatch.setattr(CHECK, "READY_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(CHECK, "_bounded_command", lambda *_args, **_kwargs: (calls.append(True) or 1, b""))

    with pytest.raises(CHECK.Refusal, match="component_status_unavailable"):
        CHECK._component_states(
            "config", "docker.io/grafana/alloy@sha256:" + "a" * 64, scrape_env(), disabled=True,
        )
    assert len(calls) > 1


def test_shell_http_reader_preserves_final_unterminated_json_body():
    response = subprocess.run(
        ["bash", "-c", CHECK.API_READ_LOOP], input=b"[]", capture_output=True, check=True,
    )

    assert response.stdout == b"[]\n"


@pytest.mark.parametrize("runner", ["component", "disabled"])
def test_config_transfer_stall_refuses_within_startup_deadline(monkeypatch, runner):
    disabled, enabled = configs()
    processes = []

    class Process:
        pid = 123
        stdout = None

        def __init__(self):
            read_fd, write_fd = os.pipe()
            self.read_fd = read_fd
            self.stdin = os.fdopen(write_fd, "wb", buffering=0)

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*_args, **_kwargs):
            return 0

    def popen(*_args, **_kwargs):
        process = Process()
        processes.append(process)
        return process

    monkeypatch.setattr(CHECK.subprocess, "Popen", popen)
    monkeypatch.setattr(CHECK, "STARTUP_SECONDS", 0.05)
    monkeypatch.setattr(CHECK.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(CHECK, "_cleanup_container", lambda _name: None)
    started = time.monotonic()

    with pytest.raises(CHECK.Refusal, match="container_start_failed"):
        if runner == "component":
            CHECK._component_states(
                "x" * CHECK.MAX_CONFIG_BYTES, "docker.io/grafana/alloy@sha256:" + "a" * 64,
                scrape_env(), disabled=False,
            )
        else:
            CHECK._run_disabled(
                "x" * CHECK.MAX_CONFIG_BYTES,
                "docker.io/grafana/alloy@sha256:" + "a" * 64,
                scrape_env(),
            )

    assert time.monotonic() - started < 1
    for process in processes:
        os.close(process.read_fd)


def test_process_cleanup_bounds_term_and_kill_waits(monkeypatch):
    class Process:
        pid = 123
        stdin = None

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*, timeout):
            waits.append(timeout)
            raise CHECK.subprocess.TimeoutExpired("podman", timeout)

    waits = []
    signals = []
    monkeypatch.setattr(CHECK.os, "killpg", lambda _pid, sig: signals.append(sig))

    with pytest.raises(CHECK.Refusal, match="cleanup_failed"):
        CHECK._stop_process(Process())

    assert waits == [2, 2]
    assert signals == [CHECK.signal.SIGTERM, CHECK.signal.SIGKILL]


@pytest.mark.parametrize("returncode", [0, 1, 2, 125])
def test_cleanup_requires_podman_exact_name_to_be_absent(monkeypatch, returncode):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return type("Result", (), {"returncode": 0 if argv[1] == "rm" else returncode})()

    monkeypatch.setattr(CHECK.subprocess, "run", run)
    if returncode == 1:
        CHECK._cleanup_container("o11y-profile-candidate-abc123")
    else:
        with pytest.raises(CHECK.Refusal, match="cleanup_failed"):
            CHECK._cleanup_container("o11y-profile-candidate-abc123")
    assert calls == [
        ["podman", "rm", "--force", "o11y-profile-candidate-abc123"],
        ["podman", "container", "exists", "o11y-profile-candidate-abc123"],
    ]


@pytest.mark.parametrize("fail_call", [1, 2])
@pytest.mark.parametrize("failure", [OSError("unavailable"), CHECK.subprocess.TimeoutExpired("podman", 5)])
def test_cleanup_command_error_or_timeout_refuses(monkeypatch, failure, fail_call):
    calls = 0

    def run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise failure
        return type("Result", (), {"returncode": 0 if calls == 1 else 1})()

    monkeypatch.setattr(CHECK.subprocess, "run", run)

    with pytest.raises(CHECK.Refusal, match="cleanup_failed"):
        CHECK._cleanup_container("o11y-profile-disabled-abc123")


@pytest.mark.parametrize("runner", ["component", "disabled"])
def test_cleanup_failure_blocks_each_candidate_success(monkeypatch, runner):
    disabled, enabled = configs()

    class Process:
        stdin = type("Stdin", (), {"write": lambda *_: None, "close": lambda *_: None})()
        pid = 123

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*_args, **_kwargs):
            return 0

    monkeypatch.setattr(CHECK.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(CHECK.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(CHECK, "STARTUP_SECONDS", 0)
    monkeypatch.setattr(CHECK, "_transfer_config", lambda *_args: 1)
    monkeypatch.setattr(CHECK, "_wait_for_startup", lambda *_args: None)

    def fail_cleanup(_name):
        raise CHECK.Refusal("cleanup_failed")

    monkeypatch.setattr(CHECK, "_cleanup_container", fail_cleanup)
    if runner == "component":
        response = (
            b"HTTP/1.1 200 OK\r\n\r\n"
            b'[{"localID":"loki.write.default"},{"localID":"pyroscope.scrape.alloy_self_profile","health":"healthy"}]'
        )
    else:
        response = b'HTTP/1.1 200 OK\r\n\r\n[{"localID":"loki.write.default"}]'
    monkeypatch.setattr(CHECK, "_bounded_command", lambda *_args, **_kwargs: (0, response))
    if runner == "component":
        with pytest.raises(CHECK.Refusal, match="cleanup_failed"):
            CHECK._component_states(
                enabled, "docker.io/grafana/alloy@sha256:" + "a" * 64, scrape_env(), disabled=False
            )
    else:
        with pytest.raises(CHECK.Refusal, match="cleanup_failed"):
            CHECK._run_disabled(disabled, "docker.io/grafana/alloy@sha256:" + "a" * 64, scrape_env())


def test_candidate_payload_size_is_bounded():
    with pytest.raises(CHECK.Refusal, match="candidate_invalid"):
        CHECK.validate_candidates("x" * (CHECK.MAX_CONFIG_BYTES + 1), "")


def test_playbook_is_read_only_exact_revision_pinned_and_check_mode_safe():
    playbook = yaml.safe_load(PLAYBOOK.read_text())
    rendered = PLAYBOOK.read_text()

    assert playbook[1]["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/files/verify-o11y-profile-candidates.py",
        "platform/services/o11y/deployment/templates/config.alloy.j2",
    ]
    assert "expected_site_config_sha" not in rendered
    assert "private_revision: unverified" in rendered
    assert "disabled_config_sha256" in rendered
    assert "enabled_config_sha256" in rendered
    assert "when: ansible_check_mode" in rendered
    assert "when: not ansible_check_mode" in rendered
    assert "network: \"none\"" not in rendered
    assert "state: absent" in rendered


@pytest.mark.parametrize("stdout", ["", "not-json"])
def test_playbook_refuses_invalid_receipt_before_from_json(stdout):
    from jinja2 import Environment

    playbook = yaml.safe_load(PLAYBOOK.read_text())
    tasks = playbook[-1]["tasks"][-1]["block"]
    names = [task["name"] for task in tasks]
    runner = next(task for task in tasks if task["name"].startswith("Run the no-pull"))
    validator = next(task for task in tasks if task["name"].startswith("Validate candidate receipt JSON"))
    json_gate = next(task for task in tasks if task["name"] == "Require verifier stdout to contain valid JSON")
    parse = next(task for task in tasks if task["name"] == "Parse the fixed sanitized candidate receipt")

    # Reproduce the original Jinja filter failure that bypassed the fixed refusal.
    env = Environment()
    env.filters["from_json"] = json.loads
    with pytest.raises(json.JSONDecodeError):
        env.from_string("{{ stdout | from_json }}").render(stdout=stdout)

    assert names.index(validator["name"]) < names.index(json_gate["name"]) < names.index(parse["name"])
    assert runner["no_log"] is True
    assert validator["no_log"] is True
    assert parse["no_log"] is True
    assert json_gate["ansible.builtin.assert"]["that"][-1] == "_profile_receipt_json_check.rc == 0"
    assert json_gate["ansible.builtin.assert"]["fail_msg"] == (
        "The Alloy profile candidate verifier did not return a valid JSON receipt."
    )

    # Exercise the same Python JSON validator command declared by the playbook.
    result = subprocess.run(validator["ansible.builtin.command"]["argv"], input=stdout, text=True, capture_output=True)
    assert result.returncode != 0


def test_playbook_accepts_only_allowlisted_unavailable_receipts_for_nonzero_exit():
    from jinja2 import Environment

    playbook = yaml.safe_load(PLAYBOOK.read_text())
    tasks = playbook[-1]["tasks"][-1]["block"]
    refusal_gate = next(
        task for task in tasks
        if task["name"] == "Require verifier success or an expected sanitized refusal"
    )
    condition = refusal_gate["ansible.builtin.assert"]["that"][0]

    assert "_profile_raw_receipt.rc == 0" in condition
    assert "_profile_receipt.get('status', '') == 'unavailable'" in condition
    assert "_profile_receipt.get('reason', '') in _profile_candidate_refusals" in condition
    accepted = Environment().compile_expression(condition)
    assert accepted(
        _profile_raw_receipt={"rc": 2},
        _profile_receipt={"status": "unavailable", "reason": "image_unavailable"},
        _profile_candidate_refusals=["image_unavailable"],
    )
    assert not accepted(
        _profile_raw_receipt={"rc": 2},
        _profile_receipt={"status": "unavailable", "reason": "unexpected"},
        _profile_candidate_refusals=["image_unavailable"],
    )
    result = subprocess.run(
        ["python3", "-c", "import json,sys; json.load(sys.stdin)"],
        input=json.dumps({"status": "unavailable", "reason": "image_unavailable"}),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0


def test_semaphore_entry_is_dev_bound_and_requires_only_public_revision():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    entry = next(item for item in templates if item["name"] == "Verify o11y Profile Pilot Candidates (Dev)")

    assert entry["repository"] == "agent-cloud dev"
    assert entry["playbook"] == "platform/playbooks/verify-o11y-profile-pilot.yml"
    assert [item["name"] for item in entry["survey_vars"]] == ["expected_repository_sha"]
