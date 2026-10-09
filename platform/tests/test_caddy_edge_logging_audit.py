"""Safety and classification checks for the Caddy callback edge audit."""

import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "playbooks/files/audit-caddy-edge-logging.py"
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
    config = runtime(logs={"logger_names": {HOST: ["edge"]}})
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
        "*.example.test": "wildcard",
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


def test_runtime_route_to_a_different_upstream_fails_closed():
    config = runtime()
    route = config["apps"]["http"]["servers"]["srv0"]["routes"][0]
    route["handle"][0]["routes"][0]["handle"][0]["upstreams"][0]["dial"] = "other-service:4001"
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["reason"] == "runtime_upstream_mismatch"


def test_unknown_writer_fails_closed():
    config = runtime(logs={"logger_names": {HOST: ["edge"]}})
    config["logging"]["logs"] = {"edge": {"writer": {"output": "plugin"}}}
    report = audit.audit(data(config))
    assert report["status"] == "refused"
    assert report["sink_class"] == "unknown"


def test_missing_container_retention_read_fails_closed():
    config = runtime(logs={"logger_names": {HOST: ["edge"]}})
    config["logging"]["logs"] = {"edge": {"writer": {"output": "stderr"}}}
    report = audit.audit(data(config, container_log_config="unavailable"))
    assert report["status"] == "refused"
    assert report["sink_class"] == "container_stderr"
    assert report["retention"] == "unknown"


def test_unverified_https_probe_does_not_claim_marker_absence():
    report = audit.audit(data(probe_status=0, logs_read_ok=True))
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
