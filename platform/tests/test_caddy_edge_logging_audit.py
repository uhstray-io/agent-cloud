"""Safety and classification checks for the Caddy callback edge audit."""

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml
from jinja2 import Environment

SCRIPT = Path(__file__).resolve().parents[1] / "playbooks/files/audit-caddy-edge-logging.py"
PLAYBOOK = Path(__file__).resolve().parents[1] / "playbooks/audit-agentgateway-edge-logging.yml"
spec = importlib.util.spec_from_file_location("caddy_edge_logging_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

HOST = "admin.inference.example.test"
CALLBACK = f"https://{HOST}/oidc/callback"
SOURCE = f"{HOST} {{\n\treverse_proxy agentgateway:4001\n}}"


def runtime(*, logs=None, servers=None):
    server = {
        "listen": ["0.0.0.0:443"],
        "routes": [{
            "match": [{"host": [HOST]}],
            "handle": [{"handler": "subroute", "routes": [
                {"match": [{"path": ["/oidc/callback"]}],
                 "handle": [{"handler": "reverse_proxy",
                             "upstreams": [{"dial": "agentgateway:4001"}]}]}
            ]}],
        }],
    }
    if logs is not None:
        server["logs"] = logs
    return {
        "apps": {"http": {"servers": servers or {"srv0": server}}},
        "logging": {"logs": {"default": {"writer": {"output": "stderr"}}}},
    }


def data(config=None, **overrides):
    result = {
        "callback_uri": CALLBACK,
        "declared_sites": SOURCE,
        "runtime_config": json.dumps(config or runtime()),
        "container_log_config": '{"Type":"journald","Config":{}}',
        "container_log_tail": "",
        "logs_read_ok": True,
        "marker": "auditmarker123",
        "probe_status": 400,
    }
    result.update(overrides)
    return result


def test_unique_live_route_without_server_logging_is_classified_disabled():
    report = audit.audit(data())
    assert report["status"] == "classified"
    assert report["runtime_route_matches"] == 1
    assert report["access_logging"] == "disabled"
    assert report["finding"] == "access_logging_disabled"
    assert report["sink_class"] == "none"


def test_live_logger_and_writer_are_classified_with_container_retention():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {"edge": {"writer": {"output": "stdout"}}}
    config["logging"]["logs"]["default"] = {"writer": {"output": "discard"},
                                                "exclude": ["http.log.access"]}
    report = audit.audit(data(config, container_log_config='{"Type":"k8s-file","Config":{"max-size":"10m"}}',
                              container_log_tail='{"uri":"/oidc/callback?o11y_audit_marker=auditmarker123"}'))
    assert report["access_logging"] == "enabled"
    assert report["sink_class"] == "container_stdout"
    assert report["retention"] == "container_limit_declared"
    assert report["marker_in_bounded_container_logs"] == "yes"


def test_matched_vars_handler_that_can_set_log_skip_refuses_classification():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {"edge": {"writer": {"output": "stdout"}}}
    outer = config["apps"]["http"]["servers"]["srv0"]["routes"][0]
    outer["handle"].insert(0, {"handler": "vars", "root": {"log_skip": True}})
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["reason"] == "runtime_route_logging_override_ambiguous"
    assert report["access_logging"] == "unknown"


def test_exact_logger_name_precedes_matching_wildcard():
    config = runtime(logs={"logger_names": {
        "*.inference.example.test": "wildcard",
        HOST: "exact",
    }})
    config["logging"]["logs"] = {
        "exact": {"writer": {"output": "stdout"}},
        "wildcard": {"writer": {"output": "stderr"},
                     "include": ["http.log.access.wildcard"]},
        "default": {"writer": {"output": "discard"},
                    "exclude": ["http.log.access"]},
    }
    report = audit.audit(data(config))
    assert report["access_logging"] == "enabled"
    assert report["sink_class"] == "container_stdout"


def test_matching_wildcard_logger_precedes_exact_skip_host():
    config = runtime(logs={"logger_names": {"*.inference.example.test": "edge"},
                           "skip_hosts": [HOST]})
    config["logging"]["logs"] = {
        "edge": {"writer": {"output": "stdout"}},
        "default": {"writer": {"output": "discard"},
                    "exclude": ["http.log.access"]},
    }
    report = audit.audit(data(config, container_log_config='{"Type":"k8s-file","Config":{"max-size":"10m"}}'))
    assert report["status"] == "classified"
    assert report["access_logging"] == "enabled"
    assert report["sink_class"] == "container_stdout"


def test_caddy_logger_names_array_aggregates_all_configured_names():
    config = runtime(logs={"logger_names": {HOST: ["edge", "audit"]}})
    config["logging"]["logs"] = {
        "edge": {"writer": {"output": "stdout"}},
        "audit": {"writer": {"output": "stdout"}},
        "default": {"writer": {"output": "discard"},
                    "exclude": ["http.log.access"]},
    }
    report = audit.audit(data(config, container_log_config='{"Type":"k8s-file","Config":{"max-size":"10m"}}'))
    assert report["status"] == "classified"
    assert report["access_logging"] == "enabled"
    assert report["sink_class"] == "container_stdout"


def test_exact_access_logger_filter_can_disable_logging():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {
        "default": {"writer": {"output": "stderr"},
                    "exclude": ["http.log.access"]}
    }
    report = audit.audit(data(config))
    assert report["status"] == "classified"
    assert report["access_logging"] == "disabled"
    assert report["sink_class"] == "none"


def test_discard_sink_is_reported_as_disabled_access_logging():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {
        "edge": {"writer": {"output": "discard"}},
        "default": {"writer": {"output": "discard"},
                    "exclude": ["http.log.access"]},
    }
    report = audit.audit(data(config))
    assert report["status"] == "classified"
    assert report["access_logging"] == "disabled"
    assert report["sink_class"] == "discard"
    assert report["finding"] == "access_logging_discarded"


def test_missing_explicit_default_logger_includes_implicit_stderr_sink():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {"edge": {"writer": {"output": "stdout"}}}
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["sink_class"] == "multiple"


def test_logger_level_or_sampling_refuses_marker_classification():
    for key, value in (("level", "ERROR"), ("sampling", {"interval": "1m"})):
        config = runtime(logs={"logger_names": {HOST: "edge"}})
        config["logging"]["logs"] = {
            "edge": {"writer": {"output": "stdout"}, key: value},
            "default": {"writer": {"output": "discard"},
                        "exclude": ["http.log.access"]},
        }
        report = audit.audit(data(config))
        assert report["status"] == "refused"
        assert report["access_logging"] == "unknown"


def test_ambiguous_live_route_fails_closed():
    config = runtime()
    config["apps"]["http"]["servers"]["srv1"] = config["apps"]["http"]["servers"]["srv0"]
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["access_logging"] == "unknown"


def test_conflicting_child_path_does_not_match_parent_callback_path():
    config = runtime()
    outer = config["apps"]["http"]["servers"]["srv0"]["routes"][0]
    outer["match"][0]["path"] = ["/oidc/callback"]
    child = outer["handle"][0]["routes"][0]
    child["match"][0]["path"] = ["/different-path"]
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["reason"] == "runtime_route_ambiguous"
    assert report["route_candidates"] == 0


def test_unsupported_caddy_path_glob_refuses_instead_of_nonmatching():
    config = runtime()
    child = config["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"][0]["routes"][0]
    child["match"][0]["path"] = ["*/callback"]
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["reason"] == "runtime_route_ambiguous"


def test_caddy_path_matching_is_case_insensitive_for_canonical_paths():
    config = runtime()
    child = config["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"][0]["routes"][0]
    child["match"][0]["path"] = ["/OIDC/CALLBACK"]
    report = audit.audit(data(config))
    assert report["status"] == "classified"
    assert report["runtime_route_matches"] == 1


def test_percent_escaped_callback_path_refuses_classification():
    report = audit.audit(data(callback_uri=f"https://{HOST}/oidc/%63allback"))
    assert report["status"] == "refused"
    assert report["reason"] == "callback_path_unsupported"


def test_file_sink_marker_is_not_applicable_even_if_seen_in_container_logs():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {
        "edge": {"writer": {"output": "file", "filename": "/private/logs/access.jsonl",
                             "roll_keep": 4}},
        "default": {"writer": {"output": "discard"},
                    "exclude": ["http.log.access"]},
    }
    report = audit.audit(data(config, marker="synthetic-marker",
                              container_log_tail="incidental synthetic-marker text"))
    assert report["status"] == "classified"
    assert report["sink_class"] == "file"
    assert report["marker_in_bounded_container_logs"] == "not_applicable"


def test_network_sink_does_not_claim_marker_absence_from_container_logs():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {
        "edge": {"writer": {"output": "net", "address": "logs.example.test:1234"}},
        "default": {"writer": {"output": "discard"},
                    "exclude": ["http.log.access"]},
    }
    report = audit.audit(data(config))
    assert report["status"] == "classified"
    assert report["sink_class"] == "network"
    assert report["marker_in_bounded_container_logs"] == "not_applicable"


def test_journald_null_config_is_explicitly_unverified_retention():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {
        "edge": {"writer": {"output": "stderr"}},
        "default": {"writer": {"output": "discard"},
                    "exclude": ["http.log.access"]},
    }
    report = audit.audit(data(config, container_log_config='{"Type":"journald","Config":null,"Size":"-1B"}'))
    assert report["status"] == "refused"
    assert report["access_logging"] == "enabled"
    assert report["sink_class"] == "container_stderr"
    assert report["retention"] == "host_retention_unverified"


def test_runtime_route_to_a_different_upstream_fails_closed():
    config = runtime()
    route = config["apps"]["http"]["servers"]["srv0"]["routes"][0]
    route["handle"][0]["routes"][0]["handle"][0]["upstreams"][0]["dial"] = "other-service:4001"
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["reason"] == "runtime_upstream_mismatch"


def test_unknown_writer_fails_closed():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {"edge": {"writer": {"output": "plugin"}}}
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["sink_class"] == "unknown"


def test_missing_container_retention_read_fails_closed():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {"edge": {"writer": {"output": "stderr"}}}
    report = audit.audit(data(config, container_log_config="unavailable"))
    assert report["status"] == "refused"
    assert report["sink_class"] == "container_stderr"
    assert report["retention"] == "unknown"


def test_unverified_https_probe_does_not_claim_marker_absence():
    config = runtime(logs={"logger_names": {HOST: "edge"}})
    config["logging"]["logs"] = {
        "edge": {"writer": {"output": "stdout"}},
        "default": {"writer": {"output": "discard"},
                    "exclude": ["http.log.access"]},
    }
    report = audit.audit(data(config, probe_status=0, logs_read_ok=True,
                              container_log_config='{"Type":"k8s-file","Config":{"max-size":"10m"}}'))
    assert report["status"] == "classified"
    assert report["https_probe"] == "unverified"
    assert report["marker_in_bounded_container_logs"] == "unknown"


def test_report_never_emits_host_path_marker_or_raw_runtime_json(monkeypatch, capsys):
    secret = "private-caddy-runtime-fixture"
    config_text = json.dumps(runtime()) + secret
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data(runtime_config=config_text))))
    assert audit.main() == 1
    output = capsys.readouterr().out
    assert HOST not in output
    assert "/oidc/callback" not in output
    assert "auditmarker123" not in output
    assert secret not in output
    assert "logger_names" not in output


def test_malformed_runtime_config_emits_only_a_fixed_refusal(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data(runtime_config=""))))
    assert audit.main() == 1
    output = capsys.readouterr().out
    assert output == ('{"status":"refused","reason":"audit_input_invalid",'
                      '"access_logging":"unknown","sink_class":"unknown",'
                      '"retention":"unknown","https_probe":"unverified",'
                      '"marker_in_bounded_container_logs":"unknown"}\n')
    assert "Traceback" not in output


def test_runtime_read_refusals_distinguish_only_fixed_safe_categories():
    plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    caddy = next(play for play in plays if play.get("hosts") == "caddy_svc")
    tasks = {task["name"]: task for task in caddy["tasks"]}
    inspect = tasks["Read whether the Caddy container is running"]
    inspect_running = tasks["Require Caddy container inspection and running state"]
    runtime_config = tasks["Read Caddy's effective admin API configuration"]
    require_config = tasks["Require the runtime config read to succeed"]

    assert inspect["no_log"] is True
    assert inspect["failed_when"] is False and inspect["check_mode"] is False
    assert inspect_running["ansible.builtin.assert"]["that"] == [
        "_edge_audit_container_state.rc == 0",
        "_edge_audit_container_state.stdout | trim == 'true'",
    ]
    assert inspect_running["ansible.builtin.assert"]["fail_msg"].count(
        "container-inspection-unavailable"
    ) == 1
    assert "container-not-running" in inspect_running["ansible.builtin.assert"]["fail_msg"]
    assert runtime_config["no_log"] is True
    assert runtime_config["failed_when"] is False and runtime_config["check_mode"] is False
    assert "admin-api-unavailable" in require_config["ansible.builtin.assert"]["fail_msg"]
    assert "exec-unavailable" in require_config["ansible.builtin.assert"]["fail_msg"]
    assert "empty-config" in require_config["ansible.builtin.assert"]["fail_msg"]
    fail_msg = require_config["ansible.builtin.assert"]["fail_msg"]
    assert "_edge_audit_runtime_config.rc in [125, 126, 127]" in fail_msg
    assert not any(key in fail_msg for key in (".stdout", ".stderr", "stdout_lines", "hostvars"))

    render = Environment().from_string
    inspect_message = inspect_running["ansible.builtin.assert"]["fail_msg"]
    config_message = require_config["ansible.builtin.assert"]["fail_msg"]
    for rc, stdout, expected in ((127, "private-output-fixture", "container-inspection-unavailable"),
                                 (0, "false", "container-not-running"),
                                 (0, "private-output-fixture", "container-inspection-unavailable")):
        assert render(inspect_message).render(
            _edge_audit_container_state=SimpleNamespace(rc=rc, stdout=stdout)
        ) == f"Caddy edge audit refused; category={expected}."
    for rc, stdout, expected in ((125, "private-output-fixture", "exec-unavailable"),
                                 (126, "", "exec-unavailable"),
                                 (127, "", "exec-unavailable"),
                                 (1, "private-output-fixture", "admin-api-unavailable"),
                                 (0, "", "empty-config")):
        assert render(config_message).render(
            _edge_audit_runtime_config=SimpleNamespace(rc=rc, stdout=stdout)
        ) == f"Caddy edge audit refused; category={expected}."
    names = [task["name"] for task in caddy["tasks"]]
    assert names.index("Read whether the Caddy container is running") < names.index(
        "Require Caddy container inspection and running state"
    ) < names.index("Read Caddy's effective admin API configuration") < names.index(
        "Require the runtime config read to succeed"
    )
