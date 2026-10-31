import hashlib
import importlib.util
import io
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/summarize-o11y-scrape-baseline.py"
SPEC = importlib.util.spec_from_file_location("scrape_baseline", HELPER)
assert SPEC and SPEC.loader
SURVEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURVEY)
BUDGET_SPEC = importlib.util.spec_from_file_location(
    "o11y_scrape_budget", ROOT / "platform/playbooks/filter_plugins/o11y_scrape_budget.py"
)
assert BUDGET_SPEC and BUDGET_SPEC.loader
BUDGET = importlib.util.module_from_spec(BUDGET_SPEC)
BUDGET_SPEC.loader.exec_module(BUDGET)
NATIVE_PROMQL_SPEC = importlib.util.spec_from_file_location(
    "o11y_native_promql_runner", ROOT / "platform/tests/o11y_native_promql_runner.py"
)
assert NATIVE_PROMQL_SPEC and NATIVE_PROMQL_SPEC.loader
NATIVE_PROMQL = importlib.util.module_from_spec(NATIVE_PROMQL_SPEC)
NATIVE_PROMQL_SPEC.loader.exec_module(NATIVE_PROMQL)


def _budget_receipt(*jobs):
    return {
        "status": "observed",
        "active_target_count": len(jobs),
        "jobs": [
            {
                "job": job,
                "active_target_count": 1,
                "health": {"healthy": 1, "unhealthy": 0},
                "max_scrape_samples_scraped_24h": 42,
                "max_scrape_response_body_bytes_point_in_time": 8192,
                "max_stored_series_label_count_24h": 8,
                "max_stored_series_label_name_bytes_24h": 32,
                "max_stored_series_label_value_bytes_24h": 64,
            }
            for job in jobs
        ],
    }


def _budget_limits():
    return {
        "body_size_limit_bytes": 16_777_216,
        "sample_limit": 10_000,
        "label_limit": 64,
        "label_name_length_limit": 128,
        "label_value_length_limit": 2048,
    }


def _availability_namespace_fixture(*, alloy_namespace=(7, 101), sibling_namespace=(7, 102)):
    alloy_id = "a" * 64
    sibling_id = "b" * 64
    containers = [
        {
            "id": alloy_id, "image": "sha256:alloy", "running": True, "pid": 4101,
            "started_at": "2026-10-28T00:00:00Z", "network_mode": "o11y",
            "ports": {}, "project": "o11y", "service": "alloy", "name": "o11y-alloy",
            "namespace_path": "/different/path/does/not/prove/isolation",
        },
        {
            "id": sibling_id, "image": "sha256:prometheus", "running": True, "pid": 4102,
            "started_at": "2026-10-28T00:00:01Z", "network_mode": "o11y",
            "ports": {}, "project": "o11y", "service": "prometheus", "name": "o11y-prometheus",
        },
    ]
    process_identities = {
        alloy_id: {
            "pid": 4101, "proc_start_ticks": 100, "cmdline_sha256": "f" * 64,
            "netns_dev": alloy_namespace[0], "netns_ino": alloy_namespace[1],
        },
        sibling_id: {
            "pid": 4102, "proc_start_ticks": 101, "cmdline_sha256": "e" * 64,
            "netns_dev": sibling_namespace[0], "netns_ino": sibling_namespace[1],
        },
    }
    return containers, process_identities


def _alloy_detail_row(name, kind, value):
    if kind == "object" and isinstance(value, dict):
        value = [{"key": key, "value": {"type": "string", "value": item}} for key, item in value.items()]
    return {"name": name, "type": "attr", "value": {"type": kind, "value": value}}


def _alloy_detail_document(component_id="prometheus.scrape.health_01"):
    return {
        "arguments": [
            _alloy_detail_row("job_name", "string", "o11y-availability"),
            _alloy_detail_row("sample_limit", "number", 64),
            _alloy_detail_row("extra_metrics", "bool", True),
        ],
        "debugInfo": [{
            "name": "target", "type": "block", "body": [
                _alloy_detail_row("job", "string", "internal-component-id"),
                _alloy_detail_row("health", "string", "up"),
                _alloy_detail_row("last_scrape", "string", "2026-10-28T18:08:11.166822896Z"),
                _alloy_detail_row("labels", "object", [
                    {"key": "job", "value": {"type": "string", "value": "o11y-availability"}},
                    {"key": "target_id", "value": {"type": "string", "value": "service:api"}},
                ]),
            ],
        }],
        "exports": [],
        "health": {"message": "component evaluated", "state": "healthy", "updatedTime": "2026-10-28T18:08:11Z"},
        "label": "health_01",
        "localID": component_id,
        "moduleID": "prometheus.scrape",
        "name": "prometheus.scrape",
        "original": "",
        "referencedBy": [],
        "referencesTo": [],
        "type": "block",
    }


def _alloy_http_response(document, status="200 OK", content_type="application/json"):
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: ".encode()
        + str(len(json.dumps(document).encode())).encode()
        + b"\r\n\r\n"
        + json.dumps(document).encode()
    )


def _alloy_wire_response(body, content_type="application/json"):
    return (
        b"HTTP/1.0 200 OK\r\nContent-Type: " + content_type.encode("ascii")
        + b"\r\nContent-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
    )


def _run_fake_alloy_http(argv, response):
    assert argv[:5] == ["podman", "exec", "o11y-alloy", "bash", "-c"]
    script = argv[-1]
    socket_prefix = "/dev/tcp/127.0.0.1/12345"
    assert script.count(socket_prefix) == 1
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    port = listener.getsockname()[1]
    requests, errors = [], []

    def serve():
        try:
            connection, _address = listener.accept()
            with connection:
                connection.settimeout(3)
                request = bytearray()
                while b"\r\n\r\n" not in request and len(request) < 2048:
                    chunk = connection.recv(512)
                    if not chunk:
                        break
                    request.extend(chunk)
                if b"\r\n\r\n" not in request:
                    raise AssertionError("fake Alloy request was incomplete")
                requests.append(bytes(request))
                connection.sendall(response)
                connection.shutdown(socket.SHUT_WR)
        except Exception as exc:  # propagate assertions and bounded socket errors to the test
            errors.append(exc)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    script = script.replace(socket_prefix, f"/dev/tcp/127.0.0.1/{port}", 1)
    try:
        result = subprocess.run(["bash", "-c", script], capture_output=True, timeout=3, check=False)
    finally:
        server.join(timeout=3)
        listener.close()
    assert not server.is_alive(), "fake Alloy HTTP server did not stop"
    if errors:
        raise errors[0]
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert len(requests) == 1
    return result.stdout, requests[0]


def _typed(kind, value):
    return {"type": kind, "value": value}


def _component_detail(name, local_id, arguments, *, blocks=(), debug=(), references_to=(), referenced_by=()):
    document = {
        "arguments": list(arguments) + list(blocks), "debugInfo": list(debug), "exports": [],
        "health": {"message": "component evaluated", "state": "healthy", "updatedTime": "2026-10-28T18:08:11Z"},
        "label": local_id.rsplit(".", 1)[-1], "localID": local_id,
        "moduleID": name, "name": name, "original": "", "referencedBy": list(referenced_by),
        "referencesTo": list(references_to), "type": "block",
    }
    return SURVEY._alloy_component_detail(
        _alloy_http_response(document), local_id, name,
    )


def _native_details_fixture(*, probe_health="up"):
    target_ip = str(ipaddress.ip_network("192.168.0.0/16")[20])
    target = {
        "module_name": "health_01", "target_id": "service:api", "service_identity": "api/service",
        "environment": "prod", "cluster": "prod-west", "endpoint": f"https://{target_ip}/healthz",
        "freshness_seconds": 300,
        "expected_status_codes": [200], "tls_server_name": "health.example.invalid", "trust": "system",
        "interval_seconds": 60, "scrape_timeout_seconds": 20, "probe_timeout_seconds": 10,
        "body_size_limit_bytes": 4096, "samples_per_scrape": 64,
    }
    module = {
        "modules": {"health_01": {"prober": "http", "timeout": "10s", "http": {
            "method": "GET", "valid_status_codes": [200], "follow_redirects": False,
            "proxy_from_environment": False, "body_size_limit": "4096B",
            "tls_config": {"insecure_skip_verify": False, "server_name": "health.example.invalid"},
        }}}
    }
    target["blackbox_config"] = json.dumps(module, separators=(",", ":"), sort_keys=True)
    target_labels = {
        "service": "api/service", "target_id": "service:api", "environment": "prod",
        "cluster": "prod-west", "instance": "service:api",
    }
    exporter = _component_detail(
        "prometheus.exporter.blackbox", "prometheus.exporter.blackbox.health_01",
        [_alloy_detail_row("config", "capsule", json.dumps(target["blackbox_config"]))],
        referenced_by=["prometheus.scrape.health_01"],
        blocks=[{"name": "target", "type": "block", "body": [
            _alloy_detail_row("name", "string", "service:api"),
            _alloy_detail_row("address", "string", target["endpoint"]),
            _alloy_detail_row("module", "string", "health_01"),
            _alloy_detail_row("labels", "object", target_labels),
        ]}],
    )
    shared = {
        "body_size_limit_bytes": 16_777_216, "sample_limit": 10_000, "label_limit": 64,
        "label_name_length_limit": 128, "label_value_length_limit": 2048,
    }
    scrape_target = {
        "__address__": "alloy.internal:12345", "__meta_component_id": "prometheus.exporter.blackbox.health_01",
        "__meta_component_name": "prometheus.exporter.blackbox",
        "__metrics_path__": "/api/v0/component/prometheus.exporter.blackbox.health_01/metrics",
        "__param_module": "health_01", "__param_target": target["endpoint"], "__scheme__": "http",
        "cluster": "prod-west", "environment": "prod", "instance": "alloy.internal",
        "job": "integrations/blackbox/service:api", "service": "api/service", "target_id": "service:api",
    }
    health_args = [
        _alloy_detail_row("targets", "array", [_typed("object", [
            {"key": key, "value": _typed("string", value)} for key, value in scrape_target.items()
        ])]),
        _alloy_detail_row("forward_to", "array", [_typed("capsule", 'capsule("storage.Appendable")')]),
        _alloy_detail_row("job_name", "string", "o11y-availability"),
        _alloy_detail_row("scrape_timeout", "string", "20s"),
        _alloy_detail_row("scrape_protocols", "array", [_typed("string", "PrometheusText0.0.4")]),
        _alloy_detail_row("body_size_limit", "string", "16MiB"),
        _alloy_detail_row("sample_limit", "number", 64), _alloy_detail_row("label_limit", "number", 64),
        _alloy_detail_row("label_name_length_limit", "number", 128),
        _alloy_detail_row("label_value_length_limit", "number", 2048),
        _alloy_detail_row("extra_metrics", "bool", True),
    ]
    debug_health = [{"name": "target", "type": "block", "body": [
        _alloy_detail_row("job", "string", "o11y-availability"),
        _alloy_detail_row("health", "string", probe_health),
        _alloy_detail_row("last_scrape", "string", "2026-10-28T18:08:11Z"),
        _alloy_detail_row("labels", "object", {
            "job": "integrations/blackbox/service:api", "service": "api/service",
            "target_id": "service:api", "environment": "prod", "cluster": "prod-west",
            "instance": "32bf72709a81",
        }),
    ]}]
    health = _component_detail(
        "prometheus.scrape", "prometheus.scrape.health_01", health_args, debug=debug_health,
        references_to=[
            "prometheus.exporter.blackbox.health_01", "prometheus.relabel.availability_health_01",
        ],
    )
    self_target = {
        "__address__": "127.0.0.1:12345", "cluster": "prod-west", "environment": "prod",
        "instance": "alloy:12345", "service": "o11y/alloy", "target_id": "service:o11y",
    }
    self_args = [
        _alloy_detail_row("targets", "array", [_typed("object", [
            {"key": key, "value": _typed("string", value)} for key, value in self_target.items()
        ])]),
        _alloy_detail_row("forward_to", "array", [_typed("capsule", 'capsule("storage.Appendable")')]),
        _alloy_detail_row("job_name", "string", "alloy"),
        _alloy_detail_row("scrape_interval", "string", "15s"),
        _alloy_detail_row("scrape_protocols", "array", [_typed("string", "PrometheusText0.0.4")]),
        _alloy_detail_row("body_size_limit", "string", "16MiB"),
        _alloy_detail_row("sample_limit", "number", 10_000), _alloy_detail_row("label_limit", "number", 64),
        _alloy_detail_row("label_name_length_limit", "number", 128),
        _alloy_detail_row("label_value_length_limit", "number", 2048),
        _alloy_detail_row("extra_metrics", "bool", True),
    ]
    debug_self = [{"name": "target", "type": "block", "body": [
        _alloy_detail_row("job", "string", "alloy"), _alloy_detail_row("health", "string", "up"),
        _alloy_detail_row("last_scrape", "string", "2026-10-28T18:08:22Z"),
        _alloy_detail_row("labels", "object", {**self_target, "job": "alloy"}),
    ]}]
    self_scrape = _component_detail(
        "prometheus.scrape", "prometheus.scrape.alloy_self", self_args, debug=debug_self,
        references_to=["prometheus.relabel.bounded_labels"],
    )
    return {
        "prometheus.exporter.blackbox.health_01": exporter,
        "prometheus.scrape.health_01": health,
        "prometheus.scrape.alloy_self": self_scrape,
    }, {"enabled": True, "targets": [target], "limits": {"max_targets": 64}}, shared


def _alloy_root_component(component_id, *, name=None, health="healthy", extra=None):
    component_type, label = component_id.rsplit(".", 1)
    return {
        "arguments": [], "debugInfo": [], "exports": [],
        "health": {"state": health, "message": "started component", "updatedTime": "2026-10-28T20:08:55Z"},
        "label": label, "localID": component_id, "moduleID": "",
        "name": name or component_type, "original": "", "referencedBy": [],
        "referencesTo": [], "type": "block", **(extra or {}),
    }


def _alloy_root_graph(*, extra_rows=()):
    ids = (
        "loki.write.default", "otelcol.receiver.otlp.traces",
        "otelcol.receiver.otlp.conformance",
    )
    return [_alloy_root_component(component_id) for component_id in ids] + list(extra_rows)


def test_typed_alloy_detail_parser_preserves_typed_attrs_and_debug_target_metadata():
    detail = SURVEY._alloy_component_detail(
        _alloy_http_response(_alloy_detail_document()),
        "prometheus.scrape.health_01", "prometheus.scrape",
    )
    assert detail["health"] == "healthy"
    assert detail["arguments"] == {
        "job_name": "o11y-availability", "sample_limit": 64, "extra_metrics": True,
    }
    target = detail["debug_info"]["target"]
    assert target["job"] == "internal-component-id"
    assert target["health"] == "up"
    assert target["labels"] == {"job": "o11y-availability", "target_id": "service:api"}
    assert detail["references_to"] == []
    assert detail["referenced_by"] == []


def test_pinned_alloy_detail_wire_shape_accepts_original_empty_string_and_typed_sink_capsule():
    document = _alloy_detail_document()
    document["arguments"].append(
        _alloy_detail_row("forward_to", "array", [_typed("capsule", 'capsule("storage.Appendable")')]),
    )
    document["referencesTo"] = ["prometheus.relabel.bounded_labels"]
    parsed = SURVEY._alloy_component_detail(
        _alloy_http_response(document), "prometheus.scrape.health_01", "prometheus.scrape",
    )
    assert parsed["arguments"]["forward_to"] == ['capsule("storage.Appendable")']
    assert parsed["references_to"] == ["prometheus.relabel.bounded_labels"]


def test_pinned_alloy_detail_accepts_observed_text_plain_utf8_json_wire_headers():
    document = _alloy_detail_document()
    body = json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n"
    raw = (
        b"HTTP/1.0 200 OK\r\n"
        b"Content-Type: text/plain;charset=utf-8\r\n\r\n"
        + body
    )
    parsed = SURVEY._alloy_component_detail(
        raw, "prometheus.scrape.health_01", "prometheus.scrape",
    )
    assert parsed["local_id"] == "prometheus.scrape.health_01"
    assert parsed["arguments"]["sample_limit"] == 64


@pytest.mark.parametrize(
    "mutate",
    [
        "status", "content_type", "plain_without_utf8_charset", "unsupported_plain_charset",
        "duplicate_content_type", "invalid_utf8", "component_id", "duplicate_attr",
        "untyped_value", "original_shape", "duplicate_reference",
    ],
)
def test_typed_alloy_detail_parser_refuses_malformed_and_untrusted_api_evidence(mutate):
    document = _alloy_detail_document()
    status, content_type = "200 OK", "application/json"
    raw_override = None
    if mutate == "status":
        status = "503 Service Unavailable"
    elif mutate == "content_type":
        content_type = "text/html"
    elif mutate == "plain_without_utf8_charset":
        content_type = "text/plain"
    elif mutate == "unsupported_plain_charset":
        content_type = "text/plain;charset=utf-16"
    elif mutate == "duplicate_content_type":
        raw_override = _alloy_http_response(document).replace(
            b"Content-Type: application/json\r\n",
            b"Content-Type: application/json\r\nContent-Type: text/plain;charset=utf-8\r\n",
        )
    elif mutate == "invalid_utf8":
        raw_override = b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n\xff"
    elif mutate == "component_id":
        document["localID"] = "prometheus.scrape.other"
    elif mutate == "duplicate_attr":
        document["arguments"].append(_alloy_detail_row("sample_limit", "number", 10000))
    elif mutate == "untyped_value":
        document["arguments"][1]["value"]["value"] = True
    elif mutate == "original_shape":
        document["original"] = []
    elif mutate == "duplicate_reference":
        document["referencesTo"] = ["prometheus.relabel.bounded_labels"] * 2
    with pytest.raises(SURVEY.Refusal):
        SURVEY._alloy_component_detail(
            raw_override or _alloy_http_response(document, status, content_type),
            "prometheus.scrape.health_01", "prometheus.scrape",
        )


def test_native_component_readback_accepts_observed_probe_failure_but_keeps_caps_exact():
    details, availability, shared = _native_details_fixture(probe_health="down")
    receipt = SURVEY.validate_availability_native_details(
        details, availability, shared, environment="prod", cluster="prod-west",
    )
    assert receipt["status"] == "observed"
    assert receipt["targets"][0]["probe_health"] == "down"
    assert receipt["targets"][0]["limits"] == {
        "body_size_limit": 16_777_216, "sample_limit": 64, "label_limit": 64,
        "label_name_length_limit": 128, "label_value_length_limit": 2048,
    }
    assert receipt["alloy_self"]["limits"]["sample_limit"] == 10_000


def test_native_readback_accepts_generated_health_hostname_and_self_job_name_contract():
    details, availability, shared = _native_details_fixture()
    health_target = details["prometheus.scrape.health_01"]["arguments"]["targets"][0]
    self_detail = details["prometheus.scrape.alloy_self"]
    self_target = self_detail["arguments"]["targets"][0]
    assert health_target["instance"] == "alloy.internal"
    assert "job" not in self_target
    assert self_detail["arguments"]["job_name"] == "alloy"
    assert SURVEY.validate_availability_native_details(
        details, availability, shared, environment="prod", cluster="prod-west",
    )["status"] == "observed"

    health_target["instance"] = "alloy..internal"
    with pytest.raises(SURVEY.Refusal, match="native_target_mismatch"):
        SURVEY.validate_availability_native_details(
            details, availability, shared, environment="prod", cluster="prod-west",
        )

    details, availability, shared = _native_details_fixture()
    details["prometheus.scrape.alloy_self"]["arguments"]["targets"][0]["job"] = "alloy"
    with pytest.raises(SURVEY.Refusal, match="native_target_mismatch"):
        SURVEY.validate_availability_native_details(
            details, availability, shared, environment="prod", cluster="prod-west",
        )


def test_native_component_readback_rejects_unverified_caps_and_wrong_component_ids():
    details, availability, shared = _native_details_fixture()
    health = details["prometheus.scrape.health_01"]
    health["arguments"]["sample_limit"] = 10_000
    with pytest.raises(SURVEY.Refusal, match="native_limits_mismatch"):
        SURVEY.validate_availability_native_details(
            details, availability, shared, environment="prod", cluster="prod-west",
        )


@pytest.mark.parametrize("mutate", ["wrong_capsule", "missing_relabel_edge", "nonreciprocal_exporter"])
def test_native_component_readback_requires_observed_component_graph_and_appendable_type(mutate):
    details, availability, shared = _native_details_fixture()
    if mutate == "wrong_capsule":
        details["prometheus.scrape.health_01"]["arguments"]["forward_to"] = ["other-sink"]
    elif mutate == "missing_relabel_edge":
        details["prometheus.scrape.health_01"]["references_to"].remove(
            "prometheus.relabel.availability_health_01",
        )
    else:
        details["prometheus.exporter.blackbox.health_01"]["referenced_by"] = []
    with pytest.raises(SURVEY.Refusal, match="native_detail_invalid"):
        SURVEY.validate_availability_native_details(
            details, availability, shared, environment="prod", cluster="prod-west",
        )


def _native_metric_api_document(*, values, timestamp_value, environment="prod", cluster="prod-west"):
    rows = []
    for target_id, job, instance, service, families in values:
        identity = {
            "job": job, "instance": instance, "service": service, "cluster": cluster,
            "environment": environment, "target_id": target_id,
        }
        for family, value in families.items():
            rows.append({
                "metric": {"__name__": family, **identity},
                "value": [str(timestamp_value), str(value)],
            })
    return json.dumps({"status": "success", "data": {"resultType": "vector", "result": rows}}).encode()


def test_native_sample_projection_uses_raw_timestamp_and_separates_endpoint_and_scrape_failures(monkeypatch):
    _details, availability, shared = _native_details_fixture()
    values = [
        ("service:o11y", "alloy", "alloy:12345", "o11y/alloy", {
            "up": 1, "scrape_samples_scraped": 415, "scrape_body_size_bytes": 73_675,
        }),
        ("service:api", "o11y-availability", "service:api", "api/service", {
            "up": 1, "probe_success": 0, "probe_duration_seconds": 0.25,
            "probe_http_status_code": 503, "scrape_samples_scraped": 17,
            "scrape_body_size_bytes": 2034,
        }),
    ]
    health_values = [values[1]]
    self_values = [values[0]]
    health_timestamps = _native_metric_api_document(
        values=[(target_id, job, instance, service, dict.fromkeys(families, 1900))
                for target_id, job, instance, service, families in health_values], timestamp_value=1999,
    )
    self_timestamps = _native_metric_api_document(
        values=[(target_id, job, instance, service, dict.fromkeys(families, 1900))
                for target_id, job, instance, service, families in self_values], timestamp_value=1999,
    )
    health_raw = _native_metric_api_document(values=health_values, timestamp_value=1999)
    self_raw = _native_metric_api_document(values=self_values, timestamp_value=1999)

    def api(path, _reason="query_unavailable", **_kwargs):
        query = parse_qs(urlsplit(SURVEY.PROMETHEUS + path).query)["query"][0]
        is_self = 'job="alloy"' in query
        if "timestamp(" in query:
            return self_timestamps if is_self else health_timestamps
        return self_raw if is_self else health_raw

    monkeypatch.setattr(SURVEY, "_run_api", api)
    receipt = SURVEY.collect_availability_native_samples(
        availability, shared, environment="prod", cluster="prod-west",
        process_started_at=1000, preapply_bound=1700, now=2000, freshness_seconds=300,
    )
    assert receipt["alloy_self"]["scrape_up"] == 1
    assert receipt["targets"] == [{
        "target_id": "service:api", "status": "observed", "scrape_up": 1,
        "probe_success": 0, "probe_http_status_code": 503, "probe_duration_seconds": "0.25",
        "scrape_samples_scraped": 17, "scrape_body_size_bytes": 2034,
    }]


def test_rendered_availability_relabel_canonicalizes_job_for_probe_and_scrape_families():
    _details, availability, _shared = _native_details_fixture()
    env = Environment(undefined=StrictUndefined, trim_blocks=True)
    env.filters.update(to_json=json.dumps, bool=bool)
    template = env.from_string(
        (ROOT / "platform/services/o11y/deployment/templates/config.alloy.j2").read_text(encoding="utf-8"),
    )
    rendered = template.render(
        local_mode=False,
        _o11y_availability=availability,
        _o11y_forbidden_metric_label_names_regex="^private$",
        o11y_cluster="prod-west",
        o11y_environment="prod",
    )
    block = rendered.split('prometheus.relabel "availability_health_01" {', 1)[1].split(
        'prometheus.scrape "alloy_self"', 1,
    )[0]
    job_relabel = '''source_labels = []
    regex         = "(.*)"
    replacement   = "o11y-availability"
    target_label  = "job"
    action        = "replace"'''
    assert job_relabel in block
    assert block.index(job_relabel) < block.index('target_label  = "instance"')
    keep_rule = (
        'regex         = "^(up|probe_success|probe_duration_seconds|probe_http_status_code|'
        'scrape_samples_scraped|scrape_body_size_bytes)$"'
    )
    assert block.index(job_relabel) < block.index(keep_rule)

    families = {
        "up", "probe_success", "probe_duration_seconds", "probe_http_status_code",
        "scrape_samples_scraped", "scrape_body_size_bytes",
    }
    forwarded = _native_metric_api_document(values=[
        ("service:api", "o11y-availability", "service:api", "api/service", dict.fromkeys(families, 1)),
    ], timestamp_value=1999)
    expected = {
        "service:api": {
            "job": "o11y-availability", "instance": "service:api", "service": "api/service",
            "cluster": "prod-west", "environment": "prod", "target_id": "service:api",
        },
    }
    rows = SURVEY._native_metric_rows(forwarded, families=families, expected=expected)
    assert set(rows) == {("service:api", family) for family in families}
    assert all(row["metric"]["job"] == "o11y-availability" for row in json.loads(forwarded)["data"]["result"])

    wrong_job = _native_metric_api_document(
        values=[("service:api", "integrations/blackbox/service:api", "service:api", "api/service", {"up": 1})],
        timestamp_value=1999,
    )
    with pytest.raises(SURVEY.Refusal, match="native_samples_ambiguous"):
        SURVEY._native_metric_rows(wrong_job, families={"up"}, expected=expected)


def test_native_forwarded_samples_reject_wrong_persisted_target_instance():
    raw = _native_metric_api_document(
        values=[("service:api", "o11y-availability", "alloy.internal", "api/service", {"up": 1})],
        timestamp_value=1999,
    )
    expected = {
        "service:api": {
            "job": "o11y-availability", "instance": "service:api", "service": "api/service",
            "cluster": "prod-west", "environment": "prod", "target_id": "service:api",
        },
    }
    with pytest.raises(SURVEY.Refusal, match="native_samples_ambiguous"):
        SURVEY._native_metric_rows(raw, families={"up"}, expected=expected)


def test_native_sample_projection_refuses_stale_raw_timestamps_and_reports_scrape_failure(monkeypatch):
    _details, availability, shared = _native_details_fixture()
    values = [
        ("service:o11y", "alloy", "alloy:12345", "o11y/alloy", {
            "up": 1, "scrape_samples_scraped": 415, "scrape_body_size_bytes": 73_675,
        }),
        ("service:api", "o11y-availability", "service:api", "api/service", {"up": 0}),
    ]
    self_values = [values[0]]
    health_values = [values[1]]
    self_raw = _native_metric_api_document(values=self_values, timestamp_value=1999)
    health_raw = _native_metric_api_document(values=health_values, timestamp_value=1999)
    self_timestamps = _native_metric_api_document(
        values=[("service:o11y", "alloy", "alloy:12345", "o11y/alloy", {
            "up": 1900, "scrape_samples_scraped": 1900, "scrape_body_size_bytes": 1900,
        })], timestamp_value=1999,
    )
    health_timestamps = _native_metric_api_document(
        values=[("service:api", "o11y-availability", "service:api", "api/service", {"up": 1600})],
        timestamp_value=1999,
    )

    def api(path, _reason="query_unavailable", **_kwargs):
        query = parse_qs(urlsplit(SURVEY.PROMETHEUS + path).query)["query"][0]
        is_self = 'job="alloy"' in query
        if "timestamp(" in query:
            return self_timestamps if is_self else health_timestamps
        return self_raw if is_self else health_raw

    monkeypatch.setattr(SURVEY, "_run_api", api)
    with pytest.raises(SURVEY.Refusal, match="native_samples_stale"):
        SURVEY.collect_availability_native_samples(
            availability, shared, environment="prod", cluster="prod-west",
            process_started_at=1000, preapply_bound=1700, now=2000, freshness_seconds=300,
        )
    health_timestamps = _native_metric_api_document(
        values=[("service:api", "o11y-availability", "service:api", "api/service", {"up": 1900})],
        timestamp_value=1999,
    )
    receipt = SURVEY.collect_availability_native_samples(
        availability, shared, environment="prod", cluster="prod-west",
        process_started_at=1000, preapply_bound=1700, now=2000, freshness_seconds=300,
    )
    assert receipt["targets"] == [{"target_id": "service:api", "status": "scrape_failed"}]


def test_native_api_batch_enforces_aggregate_call_byte_and_time_bounds(monkeypatch):
    calls = []

    def api(path, reason, **kwargs):
        calls.append((path, reason, kwargs))
        return b"{}"

    monkeypatch.setattr(SURVEY, "_run_api", api)
    paths = [f"/api/v1/query?query=up_{index}" for index in range(4)]
    assert SURVEY._run_native_api_batch(paths) == [b"{}"] * 4
    assert len(calls) == 4
    assert all(call[2]["max_response_bytes"] <= SURVEY.MAX_API_BYTES for call in calls)
    assert all(call[2]["deadline"] >= SURVEY.time.monotonic() for call in calls)

    calls.clear()
    monkeypatch.setattr(SURVEY, "_run_api", lambda *_args, **_kwargs: b"x" * SURVEY.MAX_API_BYTES)
    with pytest.raises(SURVEY.Refusal, match="native_samples_unavailable"):
        SURVEY._run_native_api_batch(paths[:2])


def test_native_api_batch_refuses_more_than_six_calls_and_oversized_encoded_path(monkeypatch):
    calls = []
    monkeypatch.setattr(SURVEY, "_run_api", lambda *args, **kwargs: calls.append((args, kwargs)) or b"{}")
    with pytest.raises(SURVEY.Refusal, match="native_samples_unavailable"):
        SURVEY._run_native_api_batch(["/api/v1/query?query=up"] * 7)
    with pytest.raises(SURVEY.Refusal, match="native_samples_unavailable"):
        SURVEY._run_native_api_batch(["/api/v1/query?query=" + "x" * 4080])
    assert calls == []


def test_timestamp_query_builder_uses_exact_family_branches_and_restores_names():
    families = ("up", "scrape_samples_scraped", "scrape_body_size_bytes")
    path = SURVEY._availability_query_path(
        families, job="alloy", environment="prod", cluster="prod-west",
        instance="alloy:12345", target_id="service:o11y", timestamp=True,
    ).removeprefix(SURVEY.PROMETHEUS)
    expression = parse_qs(urlsplit(SURVEY.PROMETHEUS + path).query)["query"][0]
    assert expression.count("label_replace(timestamp(") == len(families)
    assert expression.count("or on(__name__)") == len(families) - 1
    for family in families:
        assert f'__name__="{family}"' in expression
        assert f'"__name__", "{family}", "job", ".*"' in expression
    matchers = (
        'job="alloy"', 'environment="prod"', 'cluster="prod-west"',
        'instance="alloy:12345"', 'target_id="service:o11y"',
    )
    for matcher in matchers:
        assert expression.count(matcher) == len(families)
    value_path = SURVEY._availability_query_path(
        families, job="alloy", environment="prod", cluster="prod-west",
        instance="alloy:12345", target_id="service:o11y",
    ).removeprefix(SURVEY.PROMETHEUS)
    value_expression = parse_qs(urlsplit(SURVEY.PROMETHEUS + value_path).query)["query"][0]
    assert value_expression == (
        '{__name__=~"^(?:up|scrape_samples_scraped|scrape_body_size_bytes)$",job="alloy",'
        'environment="prod",cluster="prod-west",instance="alloy:12345",target_id="service:o11y"}'
    )


def test_native_promql_runner_builds_actual_expressions_and_semantic_mutants():
    expressions = NATIVE_PROMQL._builder_expressions(SURVEY)
    assert len(expressions["self_families"]) == 3
    assert len(expressions["health_families"]) == 6
    assert expressions["self_timestamps"].count("timestamp(") == 3
    assert expressions["health_timestamps"].count("timestamp(") == 6
    assert expressions["max_self_path_length"] <= 4096
    assert expressions["max_health_path_length"] <= 4096
    grouped = f'timestamp({expressions["self_values"]})'
    assert expressions["grouped_self"] == grouped
    plain_self, plain_health = NATIVE_PROMQL._query_mutants(expressions)
    assert "or on(__name__)" not in plain_self
    assert "or on(__name__)" not in plain_health
    rules = yaml.safe_load(NATIVE_PROMQL._rules(expressions))
    assert rules["rule_files"] == []
    assert len(rules["tests"][0]["promql_expr_test"]) == 2
    assert any(
        'unexpected="preserved"' in sample["labels"]
        for sample in rules["tests"][0]["promql_expr_test"][1]["exp_samples"]
    )
    assert len(rules["tests"][0]["input_series"]) == 3 + 6 + 5
    NATIVE_PROMQL._consumer_fixture_check(SURVEY, expressions)


def test_native_promql_runner_reads_compose_fallback_and_rejects_malformed_image(tmp_path, monkeypatch):
    image = NATIVE_PROMQL._fallback_image()
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image)
    compose = tmp_path / "compose.yml"
    compose.write_text('services:\n  prometheus:\n    image: "${PROMETHEUS_IMAGE:-}"\n', encoding="utf-8")
    monkeypatch.setattr(NATIVE_PROMQL, "COMPOSE", compose)
    with pytest.raises(NATIVE_PROMQL.RunnerError, match="malformed literal fallback Prometheus image"):
        NATIVE_PROMQL._fallback_image()


def test_native_promql_runner_fails_closed_on_nonsemantic_mutant_errors():
    assert NATIVE_PROMQL._classify_evaluator_failure(
        b"duplicate output labelset",
    ) == "duplicate_output_labelset"
    assert NATIVE_PROMQL._classify_evaluator_failure(b"query result does not match") == "expected_result_mismatch"
    assert NATIVE_PROMQL._classify_evaluator_failure(b"error parsing expression") is None
    assert NATIVE_PROMQL._classify_evaluator_failure(
        b"same labelset\nerror parsing expression",
    ) is None
    assert NATIVE_PROMQL._classify_evaluator_failure(b"cannot connect to daemon") is None
    assert NATIVE_PROMQL._classify_evaluator_failure(
        b'FAILED:\n  expr: "up or probe_success", time: 2m,\n'
        b'    exp: {__name__="probe_success"} 0, {__name__="up"} 0\n'
        b'    got: {__name__="up"} 0',
    ) == "expected_result_mismatch"
    assert NATIVE_PROMQL._classify_evaluator_failure(
        b'FAILED:\n  expr: "up or probe_success", time: 2m,\n'
        b'error parsing fixture YAML\n'
        b'    exp: {__name__="probe_success"} 0\n'
        b'    got: {__name__="up"} 0',
    ) is None
    command = NATIVE_PROMQL._evaluator_command("docker", "prometheus:test", "unique-name", "owner")
    required_args = (
        "--rm", "-i", "--network", "none", "--pull=never", "--entrypoint",
        "/bin/promtool", "test", "rules", "/dev/stdin",
    )
    for argument in required_args:
        assert argument in command
    forbidden_args = {"-v", "--volume", "--privileged", "--network=host", "-p"}
    assert not any(argument in forbidden_args for argument in command)


def test_native_promql_runner_process_io_and_post_eof_wait_respect_deadline():
    cases = (
        ("import time; time.sleep(5)", b"x" * (2 * 1024 * 1024)),
        ("import os, time; os.close(1); os.close(2); time.sleep(5)", None),
    )
    for program, input_bytes in cases:
        started = NATIVE_PROMQL.time.monotonic()
        result = NATIVE_PROMQL._bounded_process(
            [NATIVE_PROMQL.sys.executable, "-c", program],
            deadline=started + 0.2,
            input_bytes=input_bytes,
        )
        elapsed = NATIVE_PROMQL.time.monotonic() - started
        assert result[2] is True
        assert elapsed < 1.0


def test_native_promql_runner_caps_subprocess_output_while_draining():
    result = NATIVE_PROMQL._bounded_process(
        [NATIVE_PROMQL.sys.executable, "-c", "import os; os.write(1, b'x' * 40000)"],
        deadline=NATIVE_PROMQL.time.monotonic() + 2,
    )
    assert result[0] == 0
    assert result[1] == b"x" * NATIVE_PROMQL.OUTPUT_LIMIT
    assert result[2] is False
    assert result[3] is True


def test_native_promql_runner_distinguishes_missing_image_from_inspection_errors():
    classify = NATIVE_PROMQL._classify_image_status
    assert classify("podman", 1, b"", False, False) is False
    assert classify("docker", 1, b"Error: No such image: prom/test:v1", False, False) is False
    assert classify("podman", 0, b"", False, False) is True
    for engine, code, output, timed_out, truncated in (
        ("podman", 125, b"connection refused", False, False),
        ("podman", 1, b"socket error", False, False),
        ("docker", 1, b"cannot connect to API", False, False),
        ("podman", 124, b"", True, False),
        ("podman", 1, b"", False, True),
    ):
        with pytest.raises(NATIVE_PROMQL.RunnerError):
            classify(engine, code, output, timed_out, truncated)


def _write_fake_container_engine(tmp_path, name, owner, *, wrong_owner=False):
    executable = tmp_path / "fake-container-engine"
    removed = tmp_path / "removed"
    inspected = tmp_path / "inspected"
    removals = tmp_path / "removals"
    source = f"#!{NATIVE_PROMQL.sys.executable}\n" + '''
import json
import pathlib
import sys

name = __NAME__
owner = __OWNER__
owner_label = __OWNER_LABEL__
run_label = __RUN_LABEL__
removed = pathlib.Path(__REMOVED__)
inspected = pathlib.Path(__INSPECTED__)
removals = pathlib.Path(__REMOVALS__)
wrong_owner = __WRONG_OWNER__
args = sys.argv[1:]
if args[0] == "ps":
    names = [name + "-suffix"]
    if not removed.exists():
        names.append(name)
    print("\\n".join(names))
elif args[0] == "inspect":
    requested = args[-1]
    inspected.write_text(requested, encoding="utf-8")
    labels = {owner_label: "other" if wrong_owner else owner, run_label: requested}
    print(json.dumps([{"Name": "/" + requested, "Config": {"Labels": labels}}]))
elif args[0] == "rm":
    removals.write_text(json.dumps(args[1:]), encoding="utf-8")
    removed.touch()
else:
    raise SystemExit(2)
'''
    replacements = {
        "__NAME__": json.dumps(name),
        "__OWNER__": json.dumps(owner),
        "__OWNER_LABEL__": json.dumps(NATIVE_PROMQL.OWNER_LABEL),
        "__RUN_LABEL__": json.dumps(NATIVE_PROMQL.RUN_LABEL),
        "__REMOVED__": json.dumps(str(removed)),
        "__INSPECTED__": json.dumps(str(inspected)),
        "__REMOVALS__": json.dumps(str(removals)),
        "__WRONG_OWNER__": repr(wrong_owner),
    }
    for token, value in replacements.items():
        source = source.replace(token, value)
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o700)
    return executable, inspected, removals


def test_native_promql_runner_cleans_only_exact_name_and_label_owned_containers(tmp_path):
    name = "o11y-promql-unique-1"
    owner = "invocation-unique"
    engine, inspected, removals = _write_fake_container_engine(tmp_path, name, owner)
    NATIVE_PROMQL._cleanup(
        str(engine), owner, [name, name + "-absent"], NATIVE_PROMQL.time.monotonic() + 2,
    )
    assert inspected.read_text(encoding="utf-8") == name
    assert json.loads(removals.read_text(encoding="utf-8")) == [
        "--force", "--volumes", name,
    ]


def test_native_promql_runner_refuses_cleanup_when_labels_do_not_prove_ownership(tmp_path):
    name = "o11y-promql-unique-2"
    owner = "invocation-unique"
    engine, _inspected, removals = _write_fake_container_engine(
        tmp_path, name, owner, wrong_owner=True,
    )
    with pytest.raises(NATIVE_PROMQL.RunnerError, match="ownership mismatch"):
        NATIVE_PROMQL._cleanup(
            str(engine), owner, [name], NATIVE_PROMQL.time.monotonic() + 2,
        )
    assert not removals.exists()


def test_native_process_timestamp_preserves_subsecond_boundary():
    parsed = SURVEY._parse_native_time("2026-10-28T18:08:11.166822896Z")
    assert parsed == SURVEY.Decimal("1793210891.166822896")


def test_podman_template_formats_started_at_as_rfc3339nano_for_the_strict_parser(tmp_path):
    go = shutil.which("go")
    assert go is not None, "Go is required to exercise the production Podman template formatter"
    harness = tmp_path / "render_inspect.go"
    harness.write_text(
        '''package main
import (
    "encoding/json"
    "os"
    "text/template"
    "time"
)
type State struct { Running bool; Pid int; StartedAt time.Time }
type HostConfig struct { NetworkMode string; PortBindings map[string]any }
type Config struct { Labels map[string]string }
type Container struct { Id string; Image string; State State; HostConfig HostConfig; Config Config; Name string }
func main() {
    var started time.Time
    var err error
    if os.Args[2] != "zero" {
        started, err = time.Parse(time.RFC3339Nano, os.Args[2])
        if err != nil { panic(err) }
    }
    row := Container{
        Id: "0123456789abcdef", Image: "docker.io/library/alpine:3.20",
        State: State{Running: true, Pid: 1234, StartedAt: started},
        HostConfig: HostConfig{NetworkMode: "bridge", PortBindings: map[string]any{}},
        Config: Config{Labels: map[string]string{
            "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy",
        }},
        Name: "/alloy",
    }
    funcs := template.FuncMap{"json": func(value any) string {
        encoded, _ := json.Marshal(value)
        return string(encoded)
    }}
    parsed, err := template.New("podman-inspect").Funcs(funcs).Parse(os.Args[1])
    if err != nil { panic(err) }
    if err := parsed.Execute(os.Stdout, row); err != nil { panic(err) }
}
''',
        encoding="utf-8",
    )
    stamps = (
        "2026-10-28T18:08:11.166822896Z",
        "2026-10-28T18:08:11.166822897Z",
        "2026-10-28T23:38:11.166822897+05:30",
    )
    parsed_stamps = []
    for stamp in stamps:
        rendered = subprocess.run(
            [go, "run", str(harness), SURVEY.AVAILABILITY_INSPECT_FORMAT, stamp],
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout.encode("utf-8")
        record = SURVEY._availability_inspect(rendered)
        assert record["started_at"] == stamp
        parsed = SURVEY._parse_native_time(record["started_at"])
        assert parsed == SURVEY._parse_native_time(stamp)
        parsed_stamps.append(parsed)
    assert parsed_stamps[1] - parsed_stamps[0] == SURVEY.Decimal("0.000000001")

    zero_rendered = subprocess.run(
        [go, "run", str(harness), SURVEY.AVAILABILITY_INSPECT_FORMAT, "zero"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.encode("utf-8")
    zero_record = SURVEY._availability_inspect(zero_rendered)
    assert zero_record["started_at"] == ""
    for malformed in (zero_record["started_at"], "not-a-timestamp"):
        with pytest.raises(SURVEY.Refusal, match="native_debug_invalid"):
            SURVEY._parse_native_time(malformed)
    malformed_fields = zero_rendered.decode("utf-8").split("\t")
    malformed_fields[4] = "not-a-timestamp"
    malformed_record = SURVEY._availability_inspect("\t".join(malformed_fields).encode("utf-8"))
    with pytest.raises(SURVEY.Refusal, match="native_debug_invalid"):
        SURVEY._parse_native_time(malformed_record["started_at"])


def _prometheus_self_targets_document():
    identity = SURVEY._availability_self_identity("prod", "prod-west")
    target = {
        "labels": identity, "health": "up", "scrapeUrl": "http://alloy:12345/metrics",
        "lastScrape": "1970-01-01T00:33:19Z",
    }
    return json.dumps({"status": "success", "data": {"activeTargets": [target]}}).encode()


def test_prometheus_self_source_requires_fresh_raw_samples_and_all_loaded_limits(monkeypatch):
    identity = SURVEY._availability_self_identity("prod", "prod-west")
    static_labels = {key: identity[key] for key in ("service", "target_id", "cluster", "environment")}
    loaded_job = _loaded_config_job(
        "alloy", static_configs=[{"targets": ["alloy:12345"], "labels": static_labels}],
    )
    readback = _loaded_readback({"scrape_configs": [loaded_job]})
    values = _native_metric_api_document(
        values=[("service:o11y", "alloy", "alloy:12345", "o11y/alloy", {"up": 1, "scrape_samples_scraped": 415})],
        timestamp_value=1999,
    )
    timestamp = _native_metric_api_document(
        values=[("service:o11y", "alloy", "alloy:12345", "o11y/alloy", {"up": 1900, "scrape_samples_scraped": 1900})],
        timestamp_value=1999,
    )

    def api(path, _reason="query_unavailable", **_kwargs):
        query = parse_qs(urlsplit(SURVEY.PROMETHEUS + path).query)["query"][0]
        assert "timestamp(" in query or "scrape_samples_scraped" in query
        return timestamp if "timestamp(" in query else values

    monkeypatch.setattr(SURVEY, "_run_api", api)
    receipt = SURVEY.collect_prometheus_self_source(
        _prometheus_self_targets_document(), readback, _loaded_inventory(),
        environment="prod", cluster="prod-west", now=2000, lower_bound=1700,
    )
    assert receipt == {
        "status": "observed", "source": "prometheus",
        "alloy_self": {"up": 1, "scrape_samples_scraped": 415},
        "scrape_body_size_bytes": {
            "status": "unavailable", "reason": "legacy_extra_scrape_metrics_not_enabled",
        },
    }
    assert receipt["scrape_body_size_bytes"] != {"status": "observed", "value": 0}

    loaded_job["label_limit"] = 65
    with pytest.raises(SURVEY.Refusal, match="native_source_limits_mismatch"):
        SURVEY.collect_prometheus_self_source(
            _prometheus_self_targets_document(), _loaded_readback({"scrape_configs": [loaded_job]}),
            _loaded_inventory(), environment="prod", cluster="prod-west", now=2000, lower_bound=1700,
        )


def test_prometheus_self_source_limits_accept_exact_legacy_template_labels():
    from jinja2 import Undefined

    def mandatory(value):
        if isinstance(value, Undefined):
            raise ValueError("missing reviewed scrape limit")
        return value

    env = Environment(undefined=StrictUndefined)
    env.filters.update(to_json=json.dumps, bool=bool, mandatory=mandatory)
    template = ROOT / "platform/services/o11y/deployment/templates/prometheus.yml.j2"
    rendered = env.from_string(template.read_text(encoding="utf-8")).render(
        local_mode=False, o11y_scrape_limits_reviewed=True, o11y_scrape_limits=_loaded_inventory(),
        _o11y_forbidden_metric_label_names_regex="^private$", o11y_environment="prod",
        o11y_cluster="prod-west", o11y_availability_probes_enabled=False,
    )
    config = yaml.safe_load(rendered)
    job = next(row for row in config["scrape_configs"] if row["job_name"] == "alloy")
    SURVEY._validate_prometheus_alloy_loaded_limits(
        _loaded_readback({"scrape_configs": [job]}), _loaded_inventory(),
        environment="prod", cluster="prod-west",
    )


def test_native_self_only_helper_receipt_is_accepted_only_by_expected_source_consumer(monkeypatch):
    native_limits = {
        "body_size_limit": 16_777_216, "sample_limit": 10_000, "label_limit": 64,
        "label_name_length_limit": 128, "label_value_length_limit": 2048,
    }
    detail = {
        "status": "observed", "reason": "fixed_native_components_loaded", "target_count": 0,
        "targets": [], "alloy_self": {"status": "observed", "limits": native_limits},
    }
    samples = {
        "status": "observed", "reason": "fresh_native_samples", "freshness_seconds": 300,
        "alloy_self": {
            "status": "observed", "scrape_up": 1, "scrape_samples_scraped": 415,
            "scrape_body_size_bytes": 73_675,
        },
        "targets": [],
    }
    monkeypatch.setattr(SURVEY, "read_availability_native_details", lambda *_a, **_k: {**detail, "_detail_bytes": 1})
    monkeypatch.setattr(SURVEY, "collect_availability_native_samples", lambda *_a, **_k: samples)
    evidence = SURVEY.read_availability_native_source(
        {"enabled": False, "targets": [], "limits": {}}, _loaded_inventory(),
        environment="prod", cluster="prod-west", process_started_at=1000, now=2000,
    )
    source_receipt = {
        "status": "observed", "source": "alloy", "intended_source": "alloy",
        "matches_intended": True, "evidence": evidence,
    }
    assert BUDGET.o11y_availability_source_coverage(source_receipt, True) == {
        "status": "observed", "source": "alloy", "matches_intended": True,
        "sample_count": 1, "probe_failed_count": 0, "scrape_failed_count": 0,
        "body_counter": "observed_positive",
    }
    assert BUDGET.o11y_availability_source_coverage(source_receipt)["status"] == "unavailable"
    mixed = json.loads(json.dumps(source_receipt))
    mixed["evidence"]["detail"]["targets"] = [{"target_id": "service:unexpected"}]
    assert BUDGET.o11y_availability_source_coverage(mixed, True)["status"] == "unavailable"


def test_self_only_source_filter_rejects_empty_health_targets_when_not_explicitly_allowed():
    evidence = {
        "status": "observed", "source": "alloy",
        "detail": {
            "status": "observed", "reason": "fixed_native_components_loaded", "target_count": 0,
            "targets": [], "alloy_self": {"status": "observed", "limits": _loaded_inventory()},
        },
        "samples": {
            "status": "observed", "reason": "fresh_native_samples", "freshness_seconds": 300,
            "alloy_self": {
                "status": "observed", "scrape_up": 1, "scrape_samples_scraped": 415,
                "scrape_body_size_bytes": 73_675,
            },
            "targets": [],
        },
    }
    readback = {
        "status": "observed", "source": "alloy", "intended_source": "alloy",
        "matches_intended": True, "evidence": evidence,
    }
    assert BUDGET.o11y_availability_source_coverage(readback)["status"] == "unavailable"


def test_current_alloy_identity_resolves_and_uses_verified_podman_path(monkeypatch):
    container_id = "a" * 64
    provider_path = "/usr/bin/podman-compose"
    podman_path = "/usr/bin/podman"
    monkeypatch.setenv("CONTAINER_ENGINE", "podman")
    monkeypatch.setenv("LOCAL_MODE", "true")
    monkeypatch.setenv("O11Y_AVAILABILITY_ENABLED", "false")
    monkeypatch.setenv("O11Y_DEPLOYMENT_DIR", str(ROOT / "platform/services/o11y/deployment"))
    monkeypatch.setenv("COMPOSE_CMD", provider_path)
    monkeypatch.setenv("COMPOSE_OVERLAYS", "")
    monkeypatch.setattr(SURVEY.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        SURVEY, "_availability_executable_identity",
        lambda name: {"path": provider_path if name == "podman-compose" else podman_path,
                      "device": 1, "inode": 2, "size": 3, "sha256": "a" * 64},
    )
    calls = []

    def command(argv, **_kwargs):
        calls.append(argv)
        if argv == [provider_path, "--version"]:
            return b"podman-compose version 1.6.0\npodman version 4.9.3\n"
        if argv == [podman_path, "--version"]:
            return b"podman version 4.9.3\n"
        if argv == [podman_path, "ps", "-a", "--no-trunc", "--format", "{{.ID}}"]:
            return f"{container_id}\n".encode()
        if argv[:3] == [podman_path, "inspect", "--format"]:
            return (
                f"{container_id}\tsha256:alloy\ttrue\t4101\t2026-10-28T00:00:00Z\t"
                "o11y\t{}\to11y\talloy\t/o11y-alloy\n"
            ).encode()
        raise AssertionError("unexpected bounded runtime command")

    monkeypatch.setattr(SURVEY, "_availability_command", command)
    monkeypatch.setattr(
        SURVEY, "_availability_process_identity",
        lambda pid: {"pid": pid, "proc_start_ticks": 100, "cmdline_sha256": "f" * 64},
    )
    record, process = SURVEY._current_alloy_identity()
    assert record["id"] == container_id and process["pid"] == 4101
    assert any(argv[0] == podman_path and argv[1] == "ps" for argv in calls)
    assert any(argv[0] == podman_path and argv[1] == "inspect" for argv in calls)


def test_current_source_classifies_both_transition_directions_and_refuses_duplicate(monkeypatch):
    record = {
        "id": "a" * 64, "image": "sha256:alloy", "pid": 4101,
        "started_at": "1970-01-01T00:16:40Z", "running": True,
        "network_mode": "o11y", "ports": {}, "project": "o11y", "service": "alloy",
    }
    process = {"pid": 4101, "proc_start_ticks": 500, "cmdline_sha256": "f" * 64, "netns_dev": 7, "netns_ino": 102}
    monkeypatch.setattr(SURVEY, "_current_alloy_identity", lambda: (record, process))
    monkeypatch.setattr(SURVEY, "collect_prometheus_self_source", lambda *_args, **_kwargs: {
        "status": "observed", "source": "prometheus",
    })
    monkeypatch.setattr(SURVEY, "_active_prometheus_self_target", lambda *_args, **_kwargs: {"health": "up"})
    monkeypatch.setattr(
        SURVEY, "read_availability_native_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SURVEY.Refusal("native_source_absent")),
    )
    prom = SURVEY.collect_current_availability_source(
        b"{}", {}, {"enabled": False, "targets": [], "limits": {}}, _loaded_inventory(),
        environment="prod", cluster="prod-west", intended_source="alloy", now=2000,
    )
    assert prom == {
        "status": "observed", "source": "prometheus", "intended_source": "alloy",
        "matches_intended": False, "evidence": {"status": "observed", "source": "prometheus"},
        "container_identity_sha256": hashlib.sha256(record["id"].encode("ascii")).hexdigest(),
    }

    monkeypatch.setattr(
        SURVEY, "_active_prometheus_self_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SURVEY.Refusal("native_source_absent")),
    )
    native_evidence = {"status": "observed", "source": "alloy", "samples": {"alloy_self": {"scrape_up": 1}}}
    monkeypatch.setattr(SURVEY, "read_availability_native_source", lambda *_args, **_kwargs: native_evidence)
    native = SURVEY.collect_current_availability_source(
        b"{}", {}, {"enabled": True, "targets": [{"freshness_seconds": 300}], "limits": {}},
        _loaded_inventory(), environment="prod", cluster="prod-west", intended_source="alloy", now=2000,
    )
    assert native["source"] == "alloy"
    assert native["matches_intended"] is True
    assert native["evidence"] == native_evidence

    monkeypatch.setattr(SURVEY, "_active_prometheus_self_target", lambda *_args, **_kwargs: {"health": "up"})
    with pytest.raises(SURVEY.Refusal, match="native_source_ambiguous"):
        SURVEY.collect_current_availability_source(
            b"{}", {}, {"enabled": True, "targets": [{"freshness_seconds": 300}], "limits": {}},
            _loaded_inventory(), environment="prod", cluster="prod-west", intended_source="alloy", now=2000,
        )

    details, availability, shared = _native_details_fixture()
    details["prometheus.scrape.health_01"]["debug_info"]["target"]["labels"]["job"] = "o11y-availability"
    with pytest.raises(SURVEY.Refusal, match="native_debug_mismatch"):
        SURVEY.validate_availability_native_details(
            details, availability, shared, environment="prod", cluster="prod-west",
        )


def test_root_graph_alone_does_not_claim_cross_controller_native_absence(monkeypatch):
    record = {
        "id": "a" * 64, "image": "sha256:alloy", "pid": 4101,
        "started_at": "1970-01-01T00:16:40Z", "running": True,
        "network_mode": "o11y", "ports": {}, "project": "o11y", "service": "alloy",
    }
    process = {
        "pid": 4101, "proc_start_ticks": 500, "cmdline_sha256": "f" * 64,
        "netns_dev": 7, "netns_ino": 102,
    }
    calls = []
    monkeypatch.setattr(SURVEY, "_current_alloy_identity", lambda: (record, process))
    monkeypatch.setattr(SURVEY, "_active_prometheus_self_target", lambda *_args, **_kwargs: {"health": "up"})
    monkeypatch.setattr(
        SURVEY, "collect_prometheus_self_source",
        lambda *_args, **_kwargs: {"status": "observed", "source": "prometheus"},
    )
    monkeypatch.setattr(
        SURVEY, "read_availability_native_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SURVEY.Refusal("native_source_absent")),
    )

    monkeypatch.setattr(
        SURVEY, "_read_alloy_root_components",
        lambda _deadline: (b"Alloy is ready.\n", json.dumps(_alloy_root_graph()).encode()),
    )
    monkeypatch.setattr(
        SURVEY, "_read_alloy_component_detail",
        lambda *args, **kwargs: calls.append(args[0]),
    )
    availability = {
        "enabled": False, "limits": {},
        "targets": [{"module_name": "health_01", "freshness_seconds": 300}],
    }
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY.collect_current_availability_source(
            b"{}", {}, availability, _loaded_inventory(), environment="prod", cluster="prod-west",
            intended_source="prometheus", now=2000, require_health_targets=True,
            previous_source="alloy", previous_container_sha256="e" * 64, lower_bound=900,
        )
    assert calls == []

    monkeypatch.setattr(SURVEY, "_read_alloy_root_components", lambda _deadline: (
        b"Alloy is ready.\n",
        json.dumps(_alloy_root_graph(extra_rows=[_alloy_root_component(
            "prometheus.exporter.blackbox.health_64",
        )])).encode(),
    ))
    with pytest.raises(SURVEY.Refusal, match="native_source_ambiguous"):
        SURVEY._validate_alloy_root_component_list(json.dumps(_alloy_root_graph(extra_rows=[
            _alloy_root_component("prometheus.exporter.blackbox.health_64"),
        ])).encode())


def test_loaded_candidate_binding_requires_exact_process_mount_and_config_bytes(monkeypatch, tmp_path):
    config_path = tmp_path / "config" / "config.alloy"
    config_path.parent.mkdir()
    config_bytes = b'prometheus.scrape "alloy_self" {}\n'
    config_path.write_bytes(config_bytes)
    command_line = (
        b"/usr/bin/alloy\x00run\x00--server.http.listen-addr=127.0.0.1:12345\x00"
        b"/etc/alloy/config.alloy\x00"
    )
    record = {
        "id": "a" * 64, "image": "sha256:alloy", "pid": 4101,
        "started_at": "1970-01-01T00:16:40Z", "running": True,
        "network_mode": "o11y", "ports": {}, "project": "o11y", "service": "alloy",
    }
    process = {
        "pid": 4101, "cmdline_sha256": hashlib.sha256(command_line).hexdigest(),
    }
    state = {"argv": command_line, "root_bytes": config_bytes, "mount_rw": False}
    provider_state = {"version": "1.3.0", "digest": "b" * 64}
    original_open = open
    original_read = SURVEY._read_bounded_regular_file

    def fake_open(path, *args, **kwargs):
        if path == "/proc/4101/cmdline":
            return io.BytesIO(state["argv"])
        return original_open(path, *args, **kwargs)

    def fake_bounded_read(path, *, proc_root_file=False):
        if proc_root_file:
            return state["root_bytes"]
        return original_read(path, proc_root_file=proc_root_file)

    def fake_command(argv, **_kwargs):
        if "{{.ImageName}}" in argv:
            return b"docker.io/grafana/alloy:v1.5.1\n"
        if "{{json .Mounts}}" in argv:
            return json.dumps([{
                "Type": "bind", "Source": str(config_path),
                "Destination": "/etc/alloy/config.alloy", "RW": state["mount_rw"],
            }]).encode()
        raise AssertionError("unexpected runtime metadata query")

    monkeypatch.setattr(SURVEY, "open", fake_open, raising=False)
    monkeypatch.setattr(SURVEY, "_read_bounded_regular_file", fake_bounded_read)
    monkeypatch.setattr(SURVEY, "_availability_command", fake_command)
    monkeypatch.setattr(SURVEY, "_availability_provider_context", lambda: (
        ["/usr/bin/podman-compose"], [], provider_state["version"], "4.9.3", [], {
            "compose": {"path": "/usr/bin/podman-compose", "sha256": provider_state["digest"]},
            "podman": {"path": "/usr/bin/podman", "sha256": "c" * 64},
        },
    ))
    monkeypatch.setattr(SURVEY, "_verify_availability_effective_compose", lambda *_args: "o11y")
    monkeypatch.setenv("O11Y_DEPLOYMENT_DIR", str(tmp_path))

    digest = hashlib.sha256(config_bytes).hexdigest()
    binding = SURVEY._verify_loaded_candidate_config(
        record, process, digest, availability_enabled=True, lower_bound=900,
    )
    assert re.fullmatch(r"[0-9a-f]{64}", binding)
    provider_state.update(version="1.6.0", digest="d" * 64)
    assert SURVEY._verify_loaded_candidate_config(
        record, process, digest, availability_enabled=True, lower_bound=900,
    ) != binding
    provider_state.update(version="1.3.0", digest="b" * 64)

    state["argv"] = command_line.replace(b"127.0.0.1", b"0.0.0.0")
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY._verify_loaded_candidate_config(
            record, process, digest, availability_enabled=True, lower_bound=900,
        )
    state["argv"] = command_line
    state["mount_rw"] = True
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY._verify_loaded_candidate_config(
            record, process, digest, availability_enabled=True, lower_bound=900,
        )
    state["mount_rw"] = False
    state["root_bytes"] = b'prometheus.scrape "stale" {}\n'
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY._verify_loaded_candidate_config(
            record, process, digest, availability_enabled=True, lower_bound=900,
        )
    state["root_bytes"] = config_bytes
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY._verify_loaded_candidate_config(
            record, process, "f" * 64, availability_enabled=True, lower_bound=900,
        )


def test_source_transition_requires_new_post_apply_alloy_process(monkeypatch):
    record = {
        "id": "a" * 64, "image": "sha256:alloy", "pid": 4101,
        "started_at": "1970-01-01T00:16:40Z", "running": True,
        "network_mode": "o11y", "ports": {}, "project": "o11y", "service": "alloy",
    }
    process = {
        "pid": 4101, "proc_start_ticks": 500, "cmdline_sha256": "f" * 64,
        "netns_dev": 7, "netns_ino": 102,
    }
    native = {"status": "observed", "source": "alloy", "samples": {"alloy_self": {"scrape_up": 1}}}
    binding_calls = []
    monkeypatch.setattr(SURVEY, "_current_alloy_identity", lambda: (record, process))
    monkeypatch.setattr(
        SURVEY, "_active_prometheus_self_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SURVEY.Refusal("native_source_absent")),
    )
    monkeypatch.setattr(SURVEY, "read_availability_native_source", lambda *_args, **_kwargs: native)
    monkeypatch.setattr(
        SURVEY, "_verify_loaded_candidate_config",
        lambda *_args, **_kwargs: binding_calls.append(True) or "c" * 64,
    )
    candidate = "c" * 64
    current_container = hashlib.sha256(record["id"].encode("ascii")).hexdigest()
    result = SURVEY.collect_current_availability_source(
        b"{}", {}, {"enabled": True, "targets": [{"freshness_seconds": 300}], "limits": {}},
        _loaded_inventory(), environment="prod", cluster="prod-west", intended_source="alloy",
        now=2000, lower_bound=900, require_health_targets=True,
        candidate_config_sha256=candidate, previous_source="prometheus",
        previous_container_sha256="e" * 64,
    )
    assert result["source"] == "alloy" and result["matches_intended"] is True
    assert binding_calls

    binding_calls.clear()
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY.collect_current_availability_source(
            b"{}", {}, {"enabled": True, "targets": [{"freshness_seconds": 300}], "limits": {}},
            _loaded_inventory(), environment="prod", cluster="prod-west", intended_source="alloy",
            now=2000, lower_bound=900, require_health_targets=True,
            candidate_config_sha256=candidate, previous_source="prometheus",
            previous_container_sha256=current_container,
        )
    assert binding_calls == []

    record["started_at"] = "1970-01-01T00:14:00Z"
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY.collect_current_availability_source(
            b"{}", {}, {"enabled": True, "targets": [{"freshness_seconds": 300}], "limits": {}},
            _loaded_inventory(), environment="prod", cluster="prod-west", intended_source="alloy",
            now=2000, lower_bound=900, require_health_targets=True,
            candidate_config_sha256=candidate, previous_source="prometheus",
            previous_container_sha256="e" * 64,
        )


def test_native_absence_root_graph_is_bounded_complete_and_rejects_ambiguity():
    valid = _alloy_root_graph()
    assert SURVEY._validate_alloy_root_component_list(json.dumps(valid).encode()) == {
        "status": "observed", "reason": "root_graph_valid", "component_count": 3,
    }
    malformed = [
        [],
        [*valid, valid[0]],
        [*valid, _alloy_root_component("custom.module.node")],
        [*valid, _alloy_root_component("prometheus.scrape.alloy_self")],
        [*valid, _alloy_root_component("prometheus.scrape.health_future")],
        [*valid, _alloy_root_component("prometheus.relabel.availability_health_future")],
        [*valid, _alloy_root_component("prometheus.scrape.ambiguous", extra={"createdModuleIDs": []})],
        [*valid, _alloy_root_component("prometheus.scrape.nested", extra={"moduleID": "module:1"})],
    ]
    for rows in malformed:
        with pytest.raises(SURVEY.Refusal):
            SURVEY._validate_alloy_root_component_list(json.dumps(rows).encode())
    with pytest.raises(SURVEY.Refusal, match="native_component_unhealthy"):
        SURVEY._validate_alloy_root_component_list(json.dumps([
            _alloy_root_component("loki.write.default", health="unhealthy"), *valid[1:],
        ]).encode())


def test_native_absence_reads_only_fixed_ready_and_root_list_routes(monkeypatch):
    responses = [
        b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 16\r\n\r\nAlloy is ready.\n",
        b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n" + json.dumps(_alloy_root_graph()).encode(),
    ]
    commands = []

    def command(argv, *, timeout, cwd=None):
        commands.append((argv, timeout, cwd))
        return responses.pop(0)

    monkeypatch.setattr(SURVEY, "_availability_command", command)
    ready, body = SURVEY._read_alloy_root_components(SURVEY.time.monotonic() + 10)
    assert ready == b"Alloy is ready.\n"
    assert SURVEY._validate_alloy_root_component_list(body)["status"] == "observed"
    assert len(commands) == 2
    assert "/-/ready" in commands[0][0][-1]
    assert "GET /api/v0/web/components HTTP/1.0" in commands[1][0][-1]
    assert all("health_" not in call[0][-1] and "alloy_self" not in call[0][-1] for call in commands)


def test_shared_alloy_http_readers_preserve_content_length_and_final_lf(monkeypatch):
    detail_path = SURVEY.AVAILABILITY_COMPONENT_DETAIL_PATH + "prometheus.scrape.health_01"
    detail_body = json.dumps(_alloy_detail_document(), separators=(",", ":")).encode("utf-8")
    root_body = json.dumps(_alloy_root_graph(), separators=(",", ":")).encode("utf-8")
    ready_body = b"Alloy is ready.\n"
    responses = {
        detail_path: _alloy_wire_response(detail_body),
        SURVEY.AVAILABILITY_READY_PATH: _alloy_wire_response(ready_body, "text/plain"),
        SURVEY.AVAILABILITY_COMPONENTS_PATH: _alloy_wire_response(root_body, "text/plain"),
    }
    requests = []

    def command(argv, *, timeout, cwd=None):
        script = argv[-1]
        path = script.split("printf 'GET ", 1)[1].split(" HTTP/1.0", 1)[0]
        raw, request = _run_fake_alloy_http(argv, responses[path])
        requests.append(request)
        return raw

    monkeypatch.setattr(SURVEY, "_availability_command", command)
    detail_raw = SURVEY._read_alloy_component_detail(
        "prometheus.scrape.health_01", SURVEY.time.monotonic() + 10,
    )
    assert detail_raw == responses[detail_path]
    assert SURVEY._alloy_component_detail(
        detail_raw, "prometheus.scrape.health_01", "prometheus.scrape",
    )["arguments"]["sample_limit"] == 64

    ready_raw, root_raw = SURVEY._read_alloy_root_components(SURVEY.time.monotonic() + 10)
    assert ready_raw == ready_body
    assert root_raw == root_body
    assert SURVEY._validate_alloy_root_component_list(root_raw)["status"] == "observed"
    assert len(requests) == 3
    assert [request.split(b"\r\n", 1)[0] for request in requests] == [
        f"GET {detail_path} HTTP/1.0".encode(),
        f"GET {SURVEY.AVAILABILITY_READY_PATH} HTTP/1.0".encode(),
        f"GET {SURVEY.AVAILABILITY_COMPONENTS_PATH} HTTP/1.0".encode(),
    ]
    assert not detail_body.endswith(b"\n") and ready_body.endswith(b"\n")
    assert not root_body.endswith(b"\n")


def test_shared_alloy_http_reader_preserves_nul_bytes_without_a_final_newline():
    path = SURVEY.AVAILABILITY_COMPONENT_DETAIL_PATH + "prometheus.scrape.health_01"
    script = SURVEY._alloy_ui_http_script(path)
    body = b"{\"x\":\"a\x00b\"}"
    response = _alloy_wire_response(body, "text/plain;charset=utf-8")
    raw, request = _run_fake_alloy_http(
        ["podman", "exec", "o11y-alloy", "bash", "-c", script], response,
    )
    assert raw == response
    assert b"\x00" in raw and not body.endswith(b"\n")
    assert request.startswith(f"GET {path} HTTP/1.0\r\n".encode())


def test_availability_source_action_consumes_ansible_environment_json_and_stdin_fallback(monkeypatch):
    payload = {
        "reviewed": True,
        "targets": [{
            "uid": "o11y_missing_prometheus",
            "job": "prometheus",
            "instance": "localhost:9090",
            "service": "o11y/prometheus",
            "cluster": "prod-west",
            "environment": "prod",
        }],
        "availability": {"enabled": False, "targets": [], "limits": {}},
        "shared_limits": {},
        "environment": "prod",
        "cluster": "prod-west",
        "lower_bound": None,
        "freshness_seconds": 300,
        "require_health_targets": False,
    }

    def stop_before_api(*_args, **_kwargs):
        raise SURVEY.Refusal("native_source_unavailable")

    monkeypatch.setattr(SURVEY, "_run_api", stop_before_api)
    monkeypatch.setenv("O11Y_AVAILABILITY_SOURCE_JSON", json.dumps(payload))
    monkeypatch.setattr(SURVEY.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert SURVEY.os.environ.get("O11Y_AVAILABILITY_SOURCE_JSON") == json.dumps(payload)
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY._availability_source_action()

    monkeypatch.delenv("O11Y_AVAILABILITY_SOURCE_JSON")
    monkeypatch.setattr(SURVEY.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    with pytest.raises(SURVEY.Refusal, match="native_source_unavailable"):
        SURVEY._availability_source_action()


def test_availability_namespace_proof_uses_kernel_identity_and_finds_project_siblings():
    containers, processes = _availability_namespace_fixture()
    assert SURVEY.validate_availability_namespace_snapshot(
        containers, processes, (7, 1)
    ) == {
        "status": "isolated", "reason": "distinct_kernel_namespaces", "container_count": "2"
    }


def test_availability_namespace_refuses_short_and_transitive_container_joiners():
    containers, processes = _availability_namespace_fixture()
    alloy_id = containers[0]["id"]
    first_id, second_id = "c" * 64, "d" * 64
    for container_id, mode, name in (
        (first_id, f"container:{alloy_id[:12]}", "foreign-first"),
        (second_id, f"container:{first_id[:12]}", "foreign-second"),
    ):
        pid = 4200 + len(processes)
        containers.append({
            "id": container_id, "image": "sha256:joiner", "running": True,
            "pid": pid, "started_at": "2026-10-28T00:00:02Z",
            "network_mode": mode, "ports": {}, "project": "unrelated", "service": name,
            "name": name,
        })
        processes[container_id] = {
            "pid": pid, "proc_start_ticks": 200 + len(processes),
            "cmdline_sha256": "a" * 64, "netns_dev": 7, "netns_ino": 101,
        }
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY.validate_availability_namespace_snapshot(containers, processes, (7, 1))

    # Different namespace path strings cannot disguise a shared kernel namespace.
    processes["b" * 64]["netns_ino"] = processes["a" * 64]["netns_ino"]
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY.validate_availability_namespace_snapshot(containers, processes, (7, 1))


@pytest.mark.parametrize("failure", ["host", "unstable_pid", "published_port", "hidden_sibling"])
def test_availability_namespace_proof_refuses_unverified_or_shared_state(failure):
    containers, processes = _availability_namespace_fixture()
    if failure == "host":
        processes["a" * 64]["netns_ino"] = 1
    elif failure == "unstable_pid":
        processes["a" * 64]["pid"] += 1
    elif failure == "published_port":
        containers[0]["ports"] = {"12345/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12345"}]}
    elif failure == "hidden_sibling":
        containers[1]["service"] = ""
        processes["b" * 64]["netns_ino"] = processes["a" * 64]["netns_ino"]
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY.validate_availability_namespace_snapshot(containers, processes, (7, 1))


@pytest.mark.parametrize("network_mode", ["host", "container:" + ("a" * 64), "service:alloy", "ns:/run/netns/shared"])
def test_availability_namespace_proof_refuses_host_and_namespace_join_modes(network_mode):
    containers, processes = _availability_namespace_fixture()
    containers[0]["network_mode"] = network_mode
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY.validate_availability_namespace_snapshot(containers, processes, (7, 1))


def test_availability_provider_context_accepts_only_reviewed_local_provider_and_overlay(monkeypatch, tmp_path):
    monkeypatch.setattr(SURVEY.platform, "system", lambda: "Linux")
    monkeypatch.setattr(SURVEY.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        SURVEY, "_availability_executable_identity",
        lambda name: {"path": f"/usr/bin/{name}", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2},
    )
    def version_command(argv, **_kwargs):
        if argv[-1] == "--version" and argv[0] == "/usr/bin/podman":
            return b"podman version 4.9.3\n"
        return b"podman-compose version 1.3.0\npodman version 4.9.3\n"

    monkeypatch.setattr(SURVEY, "_availability_command", version_command)
    monkeypatch.setenv("CONTAINER_ENGINE", "podman")
    monkeypatch.setenv("O11Y_DEPLOYMENT_DIR", str(tmp_path))
    monkeypatch.setenv("O11Y_AVAILABILITY_ENABLED", "true")
    monkeypatch.setenv("COMPOSE_OVERLAYS", "compose.availability.yml")
    monkeypatch.delenv("LOCAL_MODE", raising=False)
    monkeypatch.delenv("COMPOSE_CMD", raising=False)
    for name in SURVEY.AVAILABILITY_PROVIDER_OVERRIDE_NAMES | {
        "CONTAINERS_CONF", "CONTAINERS_CONF_OVERRIDE", "PODMAN_CONNECTION", "PODMAN_HOST"
    }:
        monkeypatch.delenv(name, raising=False)

    assert SURVEY._availability_provider_context() == (
        ["/usr/bin/podman-compose"], ["compose.prod.yml", "compose.availability.yml"], "1.3.0",
        "podman version 4.9.3",
        ["compose.prod.yml", "compose.availability.yml"],
        {
            "compose": {"path": "/usr/bin/podman-compose", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2},
            "podman": {"path": "/usr/bin/podman", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2},
        },
    )
    monkeypatch.setattr(
        SURVEY, "_availability_command",
        lambda argv, **_kwargs: b"podman version 4.9.3\npodman-compose version 1.3.0\n"
        if argv[0] != "/usr/bin/podman" else b"podman version 4.9.3\n",
    )
    assert SURVEY._availability_provider_context()[2:4] == ("1.3.0", "podman version 4.9.3")
    for invalid_output in (
        b"podman-compose version 1.3.0\npodman version 4.9.3\npodman version 4.9.3\n",
        b"podman-compose version 1.3.0-extra\npodman version 4.9.3\n",
        b"podman-compose version 1.3.0\npodman version 4.9.3\nextra output\n",
    ):
        monkeypatch.setattr(
            SURVEY, "_availability_command",
            lambda argv, output=invalid_output, **_kwargs: b"podman version 4.9.3\n"
            if argv[0] == "/usr/bin/podman" else output,
        )
        with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
            SURVEY._availability_provider_context()

    monkeypatch.setattr(
        SURVEY, "_availability_command",
        lambda argv, **_kwargs: b"podman-compose version 1.3.0\npodman version 4.9.3\n"
        if argv[0] != "/usr/bin/podman" else b"podman version 4.9.3\n",
    )
    monkeypatch.setenv("COMPOSE_CMD", "/opt/other-podman-compose")
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY._availability_provider_context()
    monkeypatch.delenv("COMPOSE_CMD")
    monkeypatch.setenv("PODMAN_COMPOSE_PROVIDER", "docker-compose")
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY._availability_provider_context()

    monkeypatch.delenv("PODMAN_COMPOSE_PROVIDER")
    monkeypatch.setattr(
        SURVEY, "_availability_command",
        lambda argv, **_kwargs: b"podman version 4.9.3\n" if argv[0] == "/usr/bin/podman"
        else b"Docker Compose version v1.3.0\npodman version 4.9.3\n",
    )
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY._availability_provider_context()

    monkeypatch.setenv("COMPOSE_CMD", "podman compose")
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY._availability_provider_context()


def test_availability_runtime_command_bounds_output_and_timeout(monkeypatch):
    monkeypatch.setattr(SURVEY, "MAX_AVAILABILITY_COMMAND_BYTES", 4)
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY._availability_command(["python3", "-c", "print('12345')"])


def test_availability_lifecycle_recheck_consumes_candidate_and_preplacement_fingerprints(monkeypatch, tmp_path):
    containers, processes = _availability_namespace_fixture()
    host_namespace = (7, 1)
    compose_config = b"""name: o11y
x-podman:
  in_pod: false
services:
  alloy:
    image: docker.io/grafana/alloy:v1.5.1
    command: [run, --server.http.listen-addr=127.0.0.1:12345, /etc/alloy/config.alloy]
    volumes:
      - type: bind
        source: /tmp/o11y/config/config.alloy
        target: /etc/alloy/config.alloy
        read_only: true
  prometheus: {}
"""
    monkeypatch.setattr(SURVEY.platform, "system", lambda: "Linux")
    monkeypatch.setattr(SURVEY.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        SURVEY, "_availability_executable_identity",
        lambda name: {"path": f"/usr/bin/{name}", "sha256": "b" * 64, "size": 1, "device": 1, "inode": 3},
    )
    monkeypatch.setattr(
        SURVEY, "_availability_runtime_pass",
        lambda *_args: (containers, processes, host_namespace),
    )

    def command(argv, **_kwargs):
        if argv[:2] in (["/usr/bin/podman", "--version"], ["podman", "--version"]):
            return b"podman version 4.9.3\n"
        if argv[-1] == "--version":
            return b"podman-compose version 1.3.0\npodman version 4.9.3\n"
        if argv[-1] == "version":
            return b"podman-compose version 1.3.0\npodman version 4.9.3\n"
        if argv[-1] == "config":
            return compose_config
        raise AssertionError("unexpected runtime command")

    monkeypatch.setattr(SURVEY, "_availability_command", command)
    monkeypatch.setenv("CONTAINER_ENGINE", "podman")
    monkeypatch.setenv("O11Y_DEPLOYMENT_DIR", str(tmp_path))
    monkeypatch.setenv("O11Y_AVAILABILITY_ENABLED", "true")
    monkeypatch.setenv("COMPOSE_OVERLAYS", "compose.availability.yml")
    monkeypatch.delenv("LOCAL_MODE", raising=False)
    monkeypatch.delenv("COMPOSE_CMD", raising=False)
    for name in SURVEY.AVAILABILITY_PROVIDER_OVERRIDE_NAMES | {
        "CONTAINERS_CONF", "CONTAINERS_CONF_OVERRIDE", "PODMAN_CONNECTION", "PODMAN_HOST"
    }:
        monkeypatch.delenv(name, raising=False)
    for filename in ("compose.yml", "compose.prod.yml"):
        (tmp_path / filename).write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.alloy").write_text('prometheus.scrape "alloy_self" {}\n', encoding="utf-8")
    compose_config = compose_config.replace(b"/tmp/o11y", str(tmp_path).encode())
    (tmp_path / "compose.availability.yml").write_text(
        "services:\n  alloy:\n    command: [run, --server.http.listen-addr=127.0.0.1:12345, /etc/alloy/config.alloy]\n"
        "x-podman:\n  in_pod: false\n",
        encoding="utf-8",
    )

    result = SURVEY.verify_availability_namespace(validate_candidate=True)
    assert result["status"] == "isolated"
    assert result["provider"] == "podman-compose"
    assert result["provider_version"] == "1.3.0"
    assert set(result) == {
        "status", "reason", "container_count", "namespace_snapshot_sha256",
        "runtime_context_sha256", "provider", "provider_version",
    }
    monkeypatch.setenv("O11Y_EXPECTED_NAMESPACE_SNAPSHOT_SHA256", result["namespace_snapshot_sha256"])
    monkeypatch.setenv("O11Y_EXPECTED_RUNTIME_CONTEXT_SHA256", result["runtime_context_sha256"])
    assert SURVEY.verify_availability_namespace(validate_candidate=True) == result

    # A raw safe availability overlay is insufficient when a later merged overlay
    # changes the effective command or pod setting.
    unsafe_config = compose_config.replace(
        b"--server.http.listen-addr=127.0.0.1:12345",
        b"--server.http.listen-addr=0.0.0.0:12345",
    ).replace(b"in_pod: false", b"in_pod: true")
    monkeypatch.setattr(
        SURVEY, "_availability_command",
        lambda argv, **_kwargs: unsafe_config if argv[-1] == "config" else command(argv, **_kwargs),
    )
    with pytest.raises(SURVEY.Refusal, match="namespace_unverified"):
        SURVEY.verify_availability_namespace(validate_candidate=True)


def _loaded_config_job(name="prometheus", **overrides):
    job = {
        "job_name": name,
        "body_size_limit": "16MiB",
        "sample_limit": 10_000,
        "label_limit": 64,
        "label_name_length_limit": 128,
        "label_value_length_limit": 2048,
    }
    job.update(overrides)
    return job


def _loaded_readback(config, target_pools=None):
    return {
        "status": "observed",
        "reason": "stable_loaded_config",
        "config_yaml": yaml.safe_dump(config, sort_keys=False),
        "target_pools": target_pools or [{"pool": "prometheus", "job": "prometheus"}],
    }


def _loaded_inventory():
    return {
        "body_size_limit_bytes": 16_777_216,
        "sample_limit": 10_000,
        "label_limit": 64,
        "label_name_length_limit": 128,
        "label_value_length_limit": 2048,
    }


def _source_config_dir(tmp_path):
    directory = tmp_path.resolve() / "platform/services/o11y/deployment/config/scrape.d"
    directory.mkdir(parents=True)
    return directory


def _write_source_configs(directory, *, sample_limit=10_000, extra_agentgateway="", dgx_text=None):
    agentgateway = f"""scrape_configs:
  - job_name: agentgateway
    body_size_limit: 16777216B
    sample_limit: {sample_limit}
    label_limit: 64
    label_name_length_limit: 128
    label_value_length_limit: 2048
{extra_agentgateway}"""
    dgx = dgx_text or """scrape_configs:
  - job_name: dgx-spark-node
    body_size_limit: 16777216B
    sample_limit: 10000
    label_limit: 64
    label_name_length_limit: 128
    label_value_length_limit: 2048
  - job_name: dgx-spark-vllm
    body_size_limit: 16777216B
    sample_limit: 10000
    label_limit: 64
    label_name_length_limit: 128
    label_value_length_limit: 2048
"""
    (directory / "agentgateway.yml").write_text(agentgateway, encoding="utf-8")
    (directory / "dgx-spark.yml").write_text(dgx, encoding="utf-8")


def test_candidate_preview_returns_only_public_categories_and_statuses():
    receipt = _budget_receipt("grafana", "receiver-host", "host-node", "dgx-spark")
    result = BUDGET.o11y_scrape_budget_preview(receipt, _budget_limits(), True, 2000)

    assert result == {
        "status": "compatible",
        "reason": "strict_headroom_available",
        "jobs": [
            {"job": "grafana", "status": "compatible"},
            {"job": "receiver-host", "status": "compatible"},
            {"job": "host-node", "status": "compatible"},
            {"job": "dgx-spark", "status": "compatible"},
        ],
    }


def test_loaded_inline_limit_receipt_matches_inventory_and_marks_split_jobs_unverified():
    baseline = _budget_receipt("prometheus", "dgx-spark")
    config = {
        "scrape_config_files": ["/private/prometheus/scrape.d/*.yml"],
        "scrape_configs": [_loaded_config_job()],
    }
    result = BUDGET.o11y_loaded_scrape_limit_coverage(
        _loaded_readback(
            config,
            [
                {"pool": "prometheus", "job": "prometheus"},
                {"pool": "dgx-spark-gpu", "job": "dgx-spark-gpu"},
            ],
        ),
        baseline,
        _loaded_inventory(),
        True,
    )

    assert result == {
        "status": "unverified",
        "reason": "split_scrape_file_jobs_unverified",
        "jobs": [
            {"job": "dgx-spark", "status": "unverified", "reason": "split_scrape_file_job"},
            {
                "job": "prometheus",
                "status": "matched",
                "reason": "limits_match_reviewed_inventory",
            },
        ],
    }


def test_source_split_limits_match_reviewed_inventory_with_public_only_receipt(tmp_path):
    directory = _source_config_dir(tmp_path)
    _write_source_configs(directory)

    internal = SURVEY._source_scrape_limit_readback(str(directory))
    receipt = BUDGET.o11y_source_scrape_limit_coverage(internal, _loaded_inventory(), True)

    assert receipt == {
        "status": "matched",
        "reason": "source_limits_match_reviewed_inventory",
        "jobs": [
            {"job": "agentgateway", "status": "matched", "reason": "source_limits_match_reviewed_inventory"},
            {"job": "dgx-spark", "status": "matched", "reason": "source_limits_match_reviewed_inventory"},
        ],
    }
    assert str(directory) not in json.dumps(receipt)
    assert "sample_limit" not in json.dumps(receipt)


def test_source_split_limit_mismatch_reports_only_fixed_category_and_reason(tmp_path):
    directory = _source_config_dir(tmp_path)
    _write_source_configs(directory, sample_limit=10_001)

    receipt = BUDGET.o11y_source_scrape_limit_coverage(
        SURVEY._source_scrape_limit_readback(str(directory)), _loaded_inventory(), True
    )

    assert receipt["status"] == "mismatch"
    assert receipt["jobs"][0] == {
        "job": "agentgateway", "status": "mismatch", "reason": "source_limit_mismatch"
    }
    assert receipt["jobs"][1]["status"] == "matched"


@pytest.mark.parametrize(
    ("agentgateway_text", "expected_reason"),
    [
        ("scrape_configs: [broken", "source_yaml_refused"),
        (
            "scrape_configs:\n  - job_name: agentgateway\n    job_name: agentgateway\n",
            "source_yaml_refused",
        ),
    ],
)
def test_source_split_malformed_yaml_returns_sanitized_refusal(tmp_path, agentgateway_text, expected_reason):
    directory = _source_config_dir(tmp_path)
    _write_source_configs(directory)
    (directory / "agentgateway.yml").write_text(agentgateway_text, encoding="utf-8")

    try:
        SURVEY._source_scrape_limit_readback(str(directory))
    except SURVEY.Refusal as error:
        internal = {"status": "unavailable", "reason": error.category}
    else:
        pytest.fail("malformed source was accepted")
    receipt = BUDGET.o11y_source_scrape_limit_coverage(internal, _loaded_inventory(), True)

    assert receipt == {"status": "unavailable", "reason": expected_reason, "jobs": []}
    assert agentgateway_text not in json.dumps(receipt)


def test_source_split_unsafe_files_and_unexpected_paths_refuse_without_echo(tmp_path):
    directory = _source_config_dir(tmp_path)
    _write_source_configs(directory)
    source = tmp_path / "outside.yml"
    source.write_text("private-path-sentinel", encoding="utf-8")
    (directory / "agentgateway.yml").unlink()
    (directory / "agentgateway.yml").symlink_to(source)

    with pytest.raises(SURVEY.Refusal):
        SURVEY._source_scrape_limit_readback(str(directory))
    with pytest.raises(SURVEY.Refusal):
        SURVEY._source_scrape_limit_readback(str(tmp_path.resolve() / "unexpected"))

    refusal = BUDGET.o11y_source_scrape_limit_coverage(
        {"status": "unavailable", "reason": "source_file_unavailable"}, _loaded_inventory(), True
    )
    assert refusal == {"status": "unavailable", "reason": "source_file_unavailable", "jobs": []}
    assert "private-path-sentinel" not in json.dumps(refusal)


def test_source_split_duplicate_jobs_refuse_with_fixed_reason(tmp_path):
    directory = _source_config_dir(tmp_path)
    duplicate_dgx = """scrape_configs:
  - job_name: dgx-spark-node
    body_size_limit: 16777216B
    sample_limit: 10000
    label_limit: 64
    label_name_length_limit: 128
    label_value_length_limit: 2048
  - job_name: dgx-spark-node
    body_size_limit: 16777216B
    sample_limit: 10000
    label_limit: 64
    label_name_length_limit: 128
    label_value_length_limit: 2048
  - job_name: dgx-spark-vllm
    body_size_limit: 16777216B
    sample_limit: 10000
    label_limit: 64
    label_name_length_limit: 128
    label_value_length_limit: 2048
"""
    _write_source_configs(directory, dgx_text=duplicate_dgx)

    with pytest.raises(SURVEY.Refusal) as error:
        SURVEY._source_scrape_limit_readback(str(directory))

    receipt = BUDGET.o11y_source_scrape_limit_coverage(
        {"status": "unavailable", "reason": error.value.category}, _loaded_inventory(), True
    )
    assert receipt == {"status": "unavailable", "reason": "source_job_refused", "jobs": []}


@pytest.mark.parametrize("unsupported_limit", ["2026-10-13", "!!binary c2VjcmV0", "!!set {secret: null}"])
def test_collect_keeps_other_receipts_when_source_limit_scalar_is_not_json_safe(
    tmp_path, monkeypatch, unsupported_limit
):
    directory = _source_config_dir(tmp_path)
    _write_source_configs(directory, sample_limit=unsupported_limit)
    baseline = {"status": "observed", "jobs": []}
    loaded = {"status": "unavailable", "reason": "inline_unverified"}
    alerts = {"status": "observed", "families": []}
    monkeypatch.setenv("O11Y_SCRAPE_CONFIG_DIR", str(directory))
    monkeypatch.setattr(SURVEY, "_require_curl_version", lambda: None)
    monkeypatch.setattr(SURVEY, "_run_api", lambda *_args, **_kwargs: b"{}")
    monkeypatch.setattr(SURVEY, "summarize", lambda *_args, **_kwargs: baseline)
    monkeypatch.setattr(SURVEY, "_loaded_limit_readback", lambda *_args, **_kwargs: loaded)
    monkeypatch.setattr(SURVEY, "_alert_input_availability", lambda *_args, **_kwargs: alerts)

    receipt = SURVEY.collect()

    assert receipt["baseline"] == baseline
    assert receipt["loaded_limit_readback"] == loaded
    assert receipt["alert_input_availability"] == alerts
    assert receipt["source_limit_readback"] == {"status": "unavailable", "reason": "source_limit_refused"}
    json.dumps(receipt)


def test_loaded_receiver_host_uses_stricter_sample_cap():
    inventory = _loaded_inventory()
    receiver = _loaded_config_job("receiver-host", sample_limit=2000)
    result = BUDGET.o11y_loaded_scrape_limit_coverage(
        _loaded_readback({"scrape_configs": [receiver]}, [{"pool": "receiver-host", "job": "receiver-host"}]),
        _budget_receipt("receiver-host"),
        inventory,
        True,
        2000,
    )

    assert result["status"] == "matched"
    receiver["sample_limit"] = 2001
    mismatch = BUDGET.o11y_loaded_scrape_limit_coverage(
        _loaded_readback({"scrape_configs": [receiver]}, [{"pool": "receiver-host", "job": "receiver-host"}]),
        _budget_receipt("receiver-host"),
        inventory,
        True,
        2000,
    )
    assert mismatch["status"] == "mismatch"
    assert mismatch["jobs"] == [{"job": "receiver-host", "status": "mismatch", "reason": "loaded_limit_mismatch"}]


def test_loaded_limit_mismatch_compares_all_five_effective_fields():
    pairs = [
        ("body_size_limit", "body_size_limit_bytes", "17MiB"),
        ("sample_limit", "sample_limit", 10_001),
        ("label_limit", "label_limit", 65),
        ("label_name_length_limit", "label_name_length_limit", 129),
        ("label_value_length_limit", "label_value_length_limit", 2049),
    ]
    for loaded_field, _candidate_field, value in pairs:
        loaded_job = _loaded_config_job(**{loaded_field: value})
        result = BUDGET.o11y_loaded_scrape_limit_coverage(
            _loaded_readback({"scrape_configs": [loaded_job]}),
            _budget_receipt("prometheus"),
            _loaded_inventory(),
            True,
        )
        assert result["status"] == "mismatch"


def test_compound_body_size_serialization_matches_or_mismatches_reviewed_limit():
    job = _loaded_config_job(body_size_limit="16MiB1B")
    inventory = _loaded_inventory()
    inventory["body_size_limit_bytes"] = 16_777_217
    matching = BUDGET.o11y_loaded_scrape_limit_coverage(
        _loaded_readback({"scrape_configs": [job]}),
        _budget_receipt("prometheus"),
        inventory,
        True,
    )
    mismatch = BUDGET.o11y_loaded_scrape_limit_coverage(
        _loaded_readback({"scrape_configs": [job]}),
        _budget_receipt("prometheus"),
        _loaded_inventory(),
        True,
    )

    assert matching["status"] == "matched"
    assert mismatch["status"] == "mismatch"


@pytest.mark.parametrize(
    "body_size_limit",
    ["", "16MiB1MiB", "1B16MiB", "16MiB0B", "16MiB1024B", "16MiB1Bbad", "1024EiB"],
)
def test_malformed_compound_body_size_is_unavailable(body_size_limit):
    job = _loaded_config_job(body_size_limit=body_size_limit)
    result = BUDGET.o11y_loaded_scrape_limit_coverage(
        _loaded_readback({"scrape_configs": [job]}),
        _budget_receipt("prometheus"),
        _loaded_inventory(),
        True,
    )

    assert result == {"status": "unavailable", "reason": "invalid_loaded_limit", "jobs": []}


@pytest.mark.parametrize(
    "loaded_field",
    ["body_size_limit", "sample_limit", "label_limit", "label_name_length_limit", "label_value_length_limit"],
)
def test_each_missing_loaded_limit_is_unavailable(loaded_field):
    job = _loaded_config_job()
    del job[loaded_field]

    result = BUDGET.o11y_loaded_scrape_limit_coverage(
        _loaded_readback({"scrape_configs": [job]}),
        _budget_receipt("prometheus"),
        _loaded_inventory(),
        True,
    )

    assert result == {"status": "unavailable", "reason": "invalid_loaded_limit", "jobs": []}


def test_duplicate_loaded_yaml_keys_return_unavailable_without_changing_baseline():
    baseline = _budget_receipt("prometheus")
    malformed = {
        "status": "observed",
        "reason": "stable_loaded_config",
        "config_yaml": "scrape_configs:\n  - job_name: prometheus\n    job_name: grafana\n",
        "target_pools": [{"pool": "prometheus", "job": "prometheus"}],
    }
    result = BUDGET.o11y_loaded_scrape_limit_coverage(malformed, baseline, _loaded_inventory(), True)

    assert result == {"status": "unavailable", "reason": "malformed_loaded_config", "jobs": []}
    assert BUDGET.o11y_scrape_budget_preview(baseline, _budget_limits(), True)["status"] == "compatible"


def test_loaded_yaml_alias_returns_fixed_unavailable_without_echoing_raw_config():
    sentinel = "private-alias-sentinel"
    malformed = {
        "status": "observed",
        "reason": "stable_loaded_config",
        "config_yaml": (
            f"external_labels: &private_labels\n  value: {sentinel}\n"
            "scrape_configs:\n  - job_name: prometheus\n"
            "copy_of_labels: *private_labels\n"
        ),
        "target_pools": [{"pool": "prometheus", "job": "prometheus"}],
    }

    result = BUDGET.o11y_loaded_scrape_limit_coverage(
        malformed, _budget_receipt("prometheus"), _loaded_inventory(), True
    )

    assert result == {"status": "unavailable", "reason": "malformed_loaded_config", "jobs": []}
    assert sentinel not in json.dumps(result)


def test_loaded_yaml_excessive_nesting_returns_fixed_unavailable():
    nested = "[" * (BUDGET.MAX_CONFIG_NESTING + 1) + "null" + "]" * (BUDGET.MAX_CONFIG_NESTING + 1)
    malformed = {
        "status": "observed",
        "reason": "stable_loaded_config",
        "config_yaml": f"scrape_configs:\n  - job_name: prometheus\nprivate_data: {nested}\n",
        "target_pools": [{"pool": "prometheus", "job": "prometheus"}],
    }

    result = BUDGET.o11y_loaded_scrape_limit_coverage(
        malformed, _budget_receipt("prometheus"), _loaded_inventory(), True
    )

    assert result == {"status": "unavailable", "reason": "malformed_loaded_config", "jobs": []}


def test_ambiguous_target_pool_and_missing_or_invalid_loaded_limits_are_unavailable():
    baseline = _budget_receipt("prometheus")
    config = {"scrape_configs": [_loaded_config_job()]}
    ambiguous = _loaded_readback(config, [{"pool": "unknown-private-pool", "job": "prometheus"}])
    assert BUDGET.o11y_loaded_scrape_limit_coverage(
        ambiguous, baseline, _loaded_inventory(), True
    ) == {"status": "unavailable", "reason": "ambiguous_target_pool", "jobs": []}

    for invalid in (None, True, 0, "10000"):
        loaded = _loaded_config_job(sample_limit=invalid)
        result = BUDGET.o11y_loaded_scrape_limit_coverage(
            _loaded_readback({"scrape_configs": [loaded]}), baseline, _loaded_inventory(), True
        )
        assert result == {"status": "unavailable", "reason": "invalid_loaded_limit", "jobs": []}


def test_loaded_coverage_never_echoes_raw_config_sentinel():
    sentinel = "private-secret-sentinel-please-do-not-print"
    config = {
        "external_labels": {"secret": sentinel},
        "scrape_configs": [_loaded_config_job()],
    }
    result = BUDGET.o11y_loaded_scrape_limit_coverage(
        _loaded_readback(config), _budget_receipt("prometheus"), _loaded_inventory(), True
    )

    assert sentinel not in json.dumps(result)
    assert result["status"] == "matched"


@pytest.mark.parametrize(
    ("field", "candidate_field"),
    [
        ("max_scrape_samples_scraped_24h", "sample_limit"),
        ("max_scrape_response_body_bytes_point_in_time", "body_size_limit_bytes"),
        ("max_stored_series_label_count_24h", "label_limit"),
        ("max_stored_series_label_name_bytes_24h", "label_name_length_limit"),
        ("max_stored_series_label_value_bytes_24h", "label_value_length_limit"),
    ],
)
def test_candidate_equal_to_any_observed_maximum_is_incompatible(field, candidate_field):
    receipt = _budget_receipt("grafana")
    receipt["jobs"][0][field] = 100
    limits = _budget_limits()
    limits[candidate_field] = 100

    result = BUDGET.o11y_scrape_budget_preview(receipt, limits, True)

    assert result == {
        "status": "incompatible",
        "reason": "strict_headroom_missing",
        "jobs": [{"job": "grafana", "status": "incompatible"}],
    }


def test_receiver_and_host_node_use_the_stricter_sample_cap_only():
    receipt = _budget_receipt("receiver-host", "host-node", "grafana")
    receipt["jobs"][0]["max_scrape_samples_scraped_24h"] = 2000
    receipt["jobs"][1]["max_scrape_samples_scraped_24h"] = 2001
    receipt["jobs"][2]["max_scrape_samples_scraped_24h"] = 9999

    result = BUDGET.o11y_scrape_budget_preview(receipt, _budget_limits(), True, 2000)

    assert result["status"] == "incompatible"
    assert result["jobs"] == [
        {"job": "receiver-host", "status": "incompatible"},
        {"job": "host-node", "status": "incompatible"},
        {"job": "grafana", "status": "compatible"},
    ]


def test_missing_or_unreviewed_candidates_are_a_fixed_nonblocking_result():
    expected = {
        "status": "unavailable",
        "reason": "candidate_limits_unavailable",
        "jobs": [],
    }

    assert BUDGET.o11y_scrape_budget_preview(_budget_receipt("grafana"), {}, False) == expected
    assert BUDGET.o11y_scrape_budget_preview(_budget_receipt("grafana"), {}, True) == expected
    assert BUDGET.o11y_scrape_budget_preview(_budget_receipt("grafana"), _budget_limits()) == expected


@pytest.mark.parametrize(
    "invalid",
    [0, -1, True, False, 1.5, float("inf"), "100", None],
)
def test_candidate_limits_must_be_bounded_positive_yaml_integers(invalid):
    limits = _budget_limits()
    limits["sample_limit"] = invalid

    result = BUDGET.o11y_scrape_budget_preview(_budget_receipt("grafana"), limits, True)

    assert result == {
        "status": "unavailable",
        "reason": "invalid_candidate_limits",
        "jobs": [],
    }


@pytest.mark.parametrize("invalid", [0, -1, True, 20_001, 2000.0, "2000", None])
def test_receiver_host_sample_cap_must_be_a_bounded_positive_integer(invalid):
    result = BUDGET.o11y_scrape_budget_preview(_budget_receipt("receiver-host"), _budget_limits(), True, invalid)

    assert result == {
        "status": "unavailable",
        "reason": "invalid_candidate_limits",
        "jobs": [],
    }


def test_receipt_validation_rejects_private_ambiguous_or_unhealthy_shapes():
    invalid_receipts = []
    private_job = _budget_receipt("private-target-name")
    invalid_receipts.append(private_job)
    duplicate_job = _budget_receipt("grafana", "grafana")
    invalid_receipts.append(duplicate_job)
    unhealthy = _budget_receipt("grafana")
    unhealthy["jobs"][0]["health"]["unhealthy"] = 1
    invalid_receipts.append(unhealthy)
    boolean_health = _budget_receipt("grafana")
    boolean_health["jobs"][0]["health"]["healthy"] = True
    invalid_receipts.append(boolean_health)
    boolean_maximum = _budget_receipt("grafana")
    boolean_maximum["jobs"][0]["max_scrape_samples_scraped_24h"] = True
    invalid_receipts.append(boolean_maximum)
    extra_field = _budget_receipt("grafana")
    extra_field["private_target"] = "private-host"
    invalid_receipts.append(extra_field)

    for receipt in invalid_receipts:
        result = BUDGET.o11y_scrape_budget_preview(receipt, _budget_limits(), True)
        assert result == {
            "status": "unavailable",
            "reason": "invalid_baseline_receipt",
            "jobs": [],
        }
        assert "private-host" not in str(result)


def test_preview_result_never_returns_candidate_or_observed_values():
    receipt = _budget_receipt("grafana")
    limits = _budget_limits()
    result = BUDGET.o11y_scrape_budget_preview(receipt, limits, True)

    assert not any(isinstance(value, (int, float)) for value in result.values())
    assert "16777216" not in str(result)
    assert "8192" not in str(result)


def test_survey_candidate_preview_is_optional_protected_and_check_mode_skipped():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/survey-o11y-scrape-baseline.yml").read_text())
    receiver = next(play for play in playbook if play.get("name") == "Read and reduce the production scrape baseline")
    assert "vars/o11y-metric-labels.yml" in receiver["vars_files"]
    label_policy = yaml.safe_load(
        (ROOT / "platform/playbooks/vars/o11y-metric-labels.yml").read_text(encoding="utf-8"),
    )["_o11y_forbidden_metric_label_names_regex"]
    assert isinstance(label_policy, str) and label_policy
    candidate_env = Environment(undefined=StrictUndefined)
    candidate_env.filters.update(to_json=json.dumps, bool=bool)
    rendered_candidate = candidate_env.from_string(
        (ROOT / "platform/services/o11y/deployment/templates/config.alloy.j2").read_text(encoding="utf-8"),
    ).render(
        local_mode=False,
        _o11y_availability={"enabled": True, "targets": []},
        _o11y_forbidden_metric_label_names_regex=label_policy,
        o11y_cluster="prod-west",
        o11y_environment="prod",
    )
    assert json.dumps(label_policy) in rendered_candidate
    tasks = receiver["tasks"]
    preview = next(task for task in tasks if task.get("name") == "Compute the source-only candidate budget preview")
    report = next(task for task in tasks if task.get("name") == "Report the source-only candidate budget preview")

    assert preview["no_log"] is True
    assert preview["when"] == "not ansible_check_mode"
    expression = preview["ansible.builtin.set_fact"]["_scrape_budget_candidate_preview"]
    assert "o11y_scrape_limits | default({})" in expression
    assert "o11y_scrape_limits_reviewed | default(false)" in expression
    assert "o11y_scrape_sample_limit | default(2000)" in expression
    assert report["ansible.builtin.debug"]["msg"] == "{{ _scrape_budget_candidate_preview | to_json }}"
    assert report["when"] == "not ansible_check_mode"


def _targets(rows):
    return json.dumps({"status": "success", "data": {"activeTargets": rows}})


def _target(job="receiver-host", instance="private-host:9100", url=None, health="up"):
    return {
        "labels": {"job": job, "instance": instance, "service": "private-service-name"},
        "scrapeUrl": url or f"http://{instance}/metrics",
        "health": health,
        "lastError": "private scrape error text",
    }


def _metric(job="receiver-host", instance="private-host:9100", value="42"):
    return {
        "metric": {"job": job, "instance": instance, "service": "private-service-name"},
        "value": [1_791_000_000, value],
    }


def _vector(rows):
    return json.dumps({"status": "success", "data": {"resultType": "vector", "result": rows}})


def _labelset(job="receiver-host", instance="private-host:9100"):
    return {
        "__name__": "scrape_samples_scraped",
        "job": job,
        "instance": instance,
        "private_label_name": "private_label_value",
        "é": "雪",
    }


def _series_response(rows):
    return json.dumps({"status": "success", "data": rows})


def _receipt(target_rows=None, sample_rows=None, probe=None, series_rows=None, series_query=None):
    targets = [_target()] if target_rows is None else target_rows
    samples = [_metric()] if sample_rows is None else sample_rows
    probe = probe or (lambda _url: (200, 8192))
    if series_rows is None:
        series_rows = [_labelset(row["labels"]["job"], row["labels"]["instance"]) for row in targets]
    if series_query is None:
        def series_query(job, instance, _start, _end):
            matching_rows = [row for row in series_rows if row.get("job") == job and row.get("instance") == instance]
            return _series_response(matching_rows).encode()
    return SURVEY.summarize(_targets(targets), _vector(samples), probe, series_query, 100, 100 + 86400)


def test_healthy_receipt_separates_24h_samples_from_point_in_time_bytes():
    receipt = _receipt()

    assert receipt == {
        "status": "observed",
        "active_target_count": 1,
        "jobs": [
            {
                "job": "receiver-host",
                "active_target_count": 1,
                "health": {"healthy": 1, "unhealthy": 0},
                "max_scrape_samples_scraped_24h": 42,
                "max_scrape_response_body_bytes_point_in_time": 8192,
                "max_stored_series_label_count_24h": 5,
                "max_stored_series_label_name_bytes_24h": len(b"private_label_name"),
                "max_stored_series_label_value_bytes_24h": max(
                    len(b"private_label_value"),
                    len(b"private-host:9100"),
                    len(b"scrape_samples_scraped"),
                ),
            }
        ],
    }


def test_receipt_never_emits_private_labels_urls_errors_or_raw_values():
    encoded = json.dumps(_receipt())

    for private_value in (
        "private-host",
        "private-service-name",
        "private-metrics",
        "private scrape error text",
        "1791000000",
        '"42"',
        "http://",
        "private_label_name",
        "private_label_value",
        "private-host:9100",
    ):
        assert private_value not in encoded


@pytest.mark.parametrize(
    ("url", "instance"),
    [
        ("https://private-host:9100/metrics", "private-host:9100"),
        ("http://user@private-host:9100/metrics", "private-host:9100"),
        ("http://user" + ":" + "pass" + "@private-host:9100/metrics", "private-host:9100"),
        ("http://other-host:9100/metrics", "private-host:9100"),
        ("http://private-host:9200/metrics", "private-host:9100"),
        ("http://private-host:9100/metrics?token=secret", "private-host:9100"),
        ("file:///etc/passwd", "private-host:9100"),
        ("http://private-host:9100/metrics\n", "private-host:9100"),
    ],
)
def test_target_url_refuses_unsupported_or_unbound_destinations(url, instance):
    with pytest.raises(SURVEY.Refusal, match="unsafe_target_url"):
        SURVEY.validate_target_url(url, instance)


@pytest.mark.parametrize(
    "host",
    [
        "2130706433",
        "127.1",
        "0x7f000001",
        "127%2e0%2e0%2e1",
        "localhost.",
        "0177.0.0.1",
        "0x7f.0.0.1",
        "127.0.0.01",
    ],
)
def test_target_url_refuses_noncanonical_loopback_spellings(host):
    authority = f"{host}:9100"
    with pytest.raises(SURVEY.Refusal, match="unsafe_target_url"):
        SURVEY.validate_target_url(f"http://{authority}/metrics", authority)


def test_target_url_accepts_canonical_documentation_ipv4_and_internal_service_name():
    assert SURVEY.validate_target_url("http://203.0.113.40:9100/metrics", "203.0.113.40:9100") == (
        "203.0.113.40",
        9100,
        "/metrics",
    )
    assert SURVEY.validate_target_url("http://prometheus:9090/metrics", "prometheus:9090") == (
        "prometheus",
        9090,
        "/metrics",
    )


def test_prometheus_self_target_rewrites_only_the_exact_validated_destination():
    target = _target(
        job="prometheus",
        instance="localhost:9090",
        url="http://localhost:9090/metrics",
    )
    queried = []
    probed = []

    def series_query(job, instance, start, end):
        queried.append((job, instance, start, end))
        return _series_response([_labelset(job, instance)]).encode()

    receipt = SURVEY.summarize(
        _targets([target]),
        _vector([_metric(job="prometheus", instance="localhost:9090")]),
        lambda url: (probed.append(url) or (200, 2048)),
        series_query,
        100,
        100 + 86400,
    )

    assert probed == ["http://prometheus:9090/metrics"]
    assert queried == [("prometheus", "localhost:9090", 100, 100 + 86400)]
    assert receipt["jobs"][0]["job"] == "prometheus"


@pytest.mark.parametrize(
    ("job", "instance", "url"),
    [
        ("prometheus", "localhost:9090", "http://localhost:9090/other"),
        ("grafana", "localhost:3000", "http://localhost:3000/metrics"),
        ("prometheus", "127.0.0.1:9090", "http://127.0.0.1:9090/metrics"),
        ("prometheus", "[::ffff:7f00:1]:9090", "http://[::ffff:7f00:1]:9090/metrics"),
    ],
)
def test_arbitrary_localhost_targets_are_refused(job, instance, url):
    with pytest.raises(SURVEY.Refusal, match="unsafe_target_url"):
        SURVEY._targets(_targets([_target(job=job, instance=instance, url=url)]))


def test_duplicate_targets_or_urls_fail_closed():
    first = _target()
    duplicate_identity = _target()
    duplicate_url = _target(job="grafana", instance="private-host:9100", url=first["scrapeUrl"])

    for rows in ([first, duplicate_identity], [first, duplicate_url]):
        with pytest.raises(SURVEY.Refusal, match="ambiguous_targets"):
            _receipt(
                target_rows=rows,
                sample_rows=[_metric(), _metric(job="grafana")],
            )


def test_active_target_down_fails_without_calling_probe():
    called = False

    def probe(_url):
        nonlocal called
        called = True
        return 200, 3

    with pytest.raises(SURVEY.Refusal, match="unhealthy_target"):
        _receipt(target_rows=[_target(health="down")], probe=probe)
    assert not called


def test_zero_byte_probe_is_not_reported_as_an_observed_baseline():
    with pytest.raises(SURVEY.Refusal, match="missing_probe_data"):
        _receipt(probe=lambda _url: (200, 0))


def test_missing_zero_duplicate_and_unapproved_observations_fail_closed():
    cases = [
        ([], "missing_samples"),
        ([_metric(value="0")], "malformed_samples"),
        ([_metric(value="NaN")], "malformed_samples"),
        ([_metric(), _metric()], "ambiguous_metric"),
    ]
    for rows, reason in cases:
        with pytest.raises(SURVEY.Refusal, match=reason):
            _receipt(sample_rows=rows)

    with pytest.raises(SURVEY.Refusal, match="unrecognized_job"):
        _receipt(target_rows=[_target(job="private-arbitrary-job")])


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "missing_series"),
        ([{"job": "receiver-host", "instance": "private-host:9100"}], "malformed_series"),
        ([_labelset(), _labelset()], "ambiguous_series"),
        ([_labelset(instance="other-private-host:9100")], "ambiguous_series"),
    ],
)
def test_label_series_must_be_complete_unique_and_exact(rows, reason):
    if reason == "ambiguous_series" and rows and rows[0].get("instance") == "other-private-host:9100":
        def query(_job, _instance, _start, _end):
            return _series_response(rows).encode()
    else:
        query = None
    with pytest.raises(SURVEY.Refusal, match=reason):
        _receipt(series_rows=rows, series_query=query)


def test_label_series_api_selector_is_private_stdin_and_requires_http_2xx(monkeypatch):
    seen = {}

    def success(argv, **kwargs):
        seen["argv"] = argv
        process = _FakeProcess(b'{"status":"success","data":[]}200')
        seen["process"] = process
        seen["kwargs"] = kwargs
        return process

    monkeypatch.setattr(SURVEY.subprocess, "Popen", success)
    response = SURVEY._run_series_query("receiver-host", "private-host:9100", 100, 100 + 86400)

    assert response == b'{"status":"success","data":[]}'
    assert "private-host:9100" not in " ".join(seen["argv"])
    assert b"private-host:9100" not in seen["process"].stdin.value
    selector = b'match%5B%5D=%7Bjob%3D%22receiver-host%22%2Cinstance%3D%22private-host%3A9100%22%7D'
    assert selector in seen["process"].stdin.value
    assert b"start=100" in seen["process"].stdin.value and b"end=86500" in seen["process"].stdin.value
    assert seen["kwargs"]["stderr"] == subprocess.DEVNULL

    def redirect(argv, **kwargs):
        return _FakeProcess(b'{"status":"success","data":[]}302')

    monkeypatch.setattr(SURVEY.subprocess, "Popen", redirect)
    with pytest.raises(SURVEY.Refusal, match="series_unavailable"):
        SURVEY._run_series_query("receiver-host", "private-host:9100", 100, 100 + 86400)


@pytest.mark.parametrize(
    "config",
    [
        SURVEY._curl_config("http://private-host:9100/metrics"),
        SURVEY._private_api_config("http://prometheus:9090/api/v1/targets?state=active"),
    ],
)
def test_generated_curl_configs_parse_with_installed_curl(config):
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("curl is unavailable")
    result = subprocess.run(
        [curl, "--config", "-", "--version"],
        input=config,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_loaded_readback_refuses_version_mismatch_before_config_read(monkeypatch):
    monkeypatch.setattr(
        SURVEY,
        "_status_data",
        lambda path, _reason: {"version": "3.2.0"} if path.endswith("buildinfo") else pytest.fail(path),
    )

    with pytest.raises(SURVEY.Refusal, match="prometheus_version_mismatch"):
        SURVEY._loaded_limit_readback(b"unused")


def test_loaded_readback_refuses_changed_runtime_or_config_snapshot(monkeypatch):
    runtime_a = {
        "startTime": "2026-10-13T12:00:00Z",
        "lastConfigTime": "2026-10-13T12:01:00Z",
        "reloadConfigSuccess": True,
    }
    runtime_b = {**runtime_a, "lastConfigTime": "2026-10-13T12:02:00Z"}
    runtime_values = iter([runtime_a, runtime_a, runtime_b])
    config_values = iter([{"yaml": "scrape_configs: []\n"}, {"yaml": "scrape_configs: []\n"}])

    def status(path, _reason):
        if path.endswith("buildinfo"):
            return {"version": "3.1.0"}
        if path.endswith("runtimeinfo"):
            return next(runtime_values)
        return next(config_values)

    monkeypatch.setattr(SURVEY, "_status_data", status)
    with pytest.raises(SURVEY.Refusal, match="unstable_loaded_config"):
        SURVEY._loaded_limit_readback(b"unused")


def test_loaded_readback_refuses_different_config_bytes(monkeypatch):
    runtime = {
        "startTime": "2026-10-13T12:00:00Z",
        "lastConfigTime": "2026-10-13T12:01:00Z",
        "reloadConfigSuccess": True,
    }
    config_values = iter([{"yaml": "secret-sentinel-a"}, {"yaml": "secret-sentinel-b"}])

    def status(path, _reason):
        if path.endswith("buildinfo"):
            return {"version": "3.1.0"}
        if path.endswith("runtimeinfo"):
            return runtime
        return next(config_values)

    monkeypatch.setattr(SURVEY, "_status_data", status)
    with pytest.raises(SURVEY.Refusal, match="unstable_loaded_config"):
        SURVEY._loaded_limit_readback(b"unused")


@pytest.mark.parametrize("bad_config", [None, "", 7, "12345", "\ud800"])
@pytest.mark.parametrize("bad_config_index", [0, 1])
def test_loaded_readback_refuses_invalid_config_as_unavailable(monkeypatch, bad_config, bad_config_index):
    monkeypatch.setattr(SURVEY, "MAX_API_BYTES", 4)
    runtime = {
        "startTime": "2026-10-13T12:00:00Z",
        "lastConfigTime": "2026-10-13T12:01:00Z",
        "reloadConfigSuccess": True,
    }
    config_values = ["ok", "ok"]
    config_values[bad_config_index] = bad_config
    configs = iter(config_values)

    def status(path, _reason):
        if path.endswith("buildinfo"):
            return {"version": "3.1.0"}
        if path.endswith("runtimeinfo"):
            return runtime
        return {"yaml": next(configs)}

    monkeypatch.setattr(SURVEY, "_status_data", status)
    with pytest.raises(SURVEY.Refusal, match="loaded_config_unavailable"):
        SURVEY._loaded_limit_readback(b"unused")


def test_loaded_readback_failure_does_not_suppress_active_baseline(monkeypatch):
    baseline = _budget_receipt("prometheus")
    monkeypatch.setattr(SURVEY, "_require_curl_version", lambda: None)
    monkeypatch.setattr(SURVEY, "_run_api", lambda *_args: b"private api response")
    monkeypatch.setattr(SURVEY, "summarize", lambda *_args: baseline)

    def unavailable(_targets):
        raise SURVEY.Refusal("unstable_loaded_config")

    monkeypatch.setattr(SURVEY, "_loaded_limit_readback", unavailable)

    result = SURVEY.collect()

    assert result["baseline"] == baseline
    assert result["loaded_limit_readback"] == {
        "status": "unavailable",
        "reason": "unstable_loaded_config",
    }


def test_loaded_status_readback_rejects_oversized_api_json(monkeypatch):
    monkeypatch.setattr(SURVEY, "MAX_API_BYTES", 1)
    monkeypatch.setattr(SURVEY, "_run_api", lambda _path, _reason: b'{"status":"success"}')

    with pytest.raises(SURVEY.Refusal, match="loaded_config_unavailable"):
        SURVEY._status_data("/api/v1/status/config", "loaded_config_unavailable")


def test_loaded_status_readback_refuses_unsupported_api_shape(monkeypatch):
    monkeypatch.setattr(SURVEY, "_run_api", lambda _path, _reason: b'{"status":"success","data":[]}')

    with pytest.raises(SURVEY.Refusal, match="loaded_config_unavailable"):
        SURVEY._status_data("/api/v1/status/config", "loaded_config_unavailable")


def test_target_and_sample_api_refuses_redirect_even_with_success_json(monkeypatch):
    argv_seen = []

    def fake_popen(argv, **kwargs):
        argv_seen.extend(argv)
        return _FakeProcess(b'{"status":"success","data":[]}302')

    monkeypatch.setattr(SURVEY.subprocess, "Popen", fake_popen)
    with pytest.raises(SURVEY.Refusal, match="query_unavailable"):
        SURVEY._run_api("/api/v1/targets?state=active")
    assert "--write-out" in argv_seen
    assert "%{http_code}" in argv_seen


def test_host_node_targets_collapse_to_fixed_public_category():
    receipt = _receipt(
        target_rows=[_target(job="host-node-private-group")],
        sample_rows=[_metric(job="host-node-private-group")],
    )

    assert receipt["jobs"][0]["job"] == "host-node"
    assert "private-group" not in json.dumps(receipt)


class _FakeProcess:
    def __init__(self, output, returncode=0):
        self.stdin = _RecordingInput()
        self.stdout = io.BytesIO(output)
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _RecordingInput:
    def __init__(self):
        self.value = bytearray()

    def write(self, data):
        self.value.extend(data)

    def close(self):
        pass


def test_probe_keeps_private_url_off_argv_and_streams_only_the_byte_count(monkeypatch):
    url = "http://private-host:9100/private-metrics"
    private_body = b"private response body that is discarded"
    output = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(private_body)).encode() + b"\r\n\r\n" + private_body
    process = _FakeProcess(output)
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["process"] = process
        seen["kwargs"] = kwargs
        return process

    monkeypatch.setattr(SURVEY.subprocess, "Popen", fake_popen)

    result = SURVEY._run_probe(url)

    assert result == (200, len(private_body))
    assert url not in seen["argv"]
    assert b"url = \"" + url.encode() + b"\"" in process.stdin.value
    assert b"Accept-Encoding: identity" in process.stdin.value
    assert seen["kwargs"]["stderr"] == subprocess.DEVNULL


def test_probe_refuses_compression_non_2xx_and_oversize(monkeypatch):
    def run_probe_with(output, monkeypatch):
        process = _FakeProcess(output)
        monkeypatch.setattr(SURVEY.subprocess, "Popen", lambda *_args, **_kwargs: process)
        return lambda: SURVEY._run_probe("http://private-host:9100/metrics")

    compressed = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\nbody"
    with pytest.raises(SURVEY.Refusal, match="probe_compressed"):
        run_probe_with(compressed, monkeypatch)()

    redirected = b"HTTP/1.1 302 Found\r\nLocation: http://private-host:9100/elsewhere\r\n\r\n"
    with pytest.raises(SURVEY.Refusal, match="probe_http_status"):
        run_probe_with(redirected, monkeypatch)()

    monkeypatch.setattr(SURVEY, "MAX_RESPONSE_BYTES", 8)
    oversized = b"HTTP/1.1 200 OK\r\n\r\n123456789"
    with pytest.raises(SURVEY.Refusal, match="probe_too_large"):
        run_probe_with(oversized, monkeypatch)()

    with pytest.raises(SURVEY.Refusal, match="missing_probe_data"):
        run_probe_with(b"HTTP/1.1 200 OK\r\n\r\n", monkeypatch)()


def test_probe_refusal_output_is_fixed_and_does_not_echo_private_target(monkeypatch, capsys):
    def fail():
        raise SURVEY.Refusal("probe_unavailable")

    monkeypatch.setattr(SURVEY, "collect", fail)
    assert SURVEY.main() == 2
    output = capsys.readouterr()

    assert output.err == ""
    assert json.loads(output.out) == {"status": "unavailable", "reason": "probe_unavailable"}
    assert "private-host" not in output.out


def test_availability_prepare_checks_receiver_python_prerequisite_before_helper_calls():
    tasks = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/prepare-o11y-availability.yml").read_text(encoding="utf-8"),
    )
    prerequisite = next(
        task for task in tasks
        if task.get("name") == "Check the receiver runtime interpreter and existing PyYAML dependency"
    )
    refusal = next(
        task for task in tasks
        if task.get("name") == "Require the existing receiver Python prerequisite for source readback"
    )
    source_helper = next(
        task for task in tasks if task.get("ansible.builtin.script", {}).get("cmd", "").endswith(
            "--verify-availability-source",
        )
    )
    namespace_helper = next(
        task for task in tasks if task.get("ansible.builtin.script", {}).get("cmd", "").endswith(
            "--verify-availability-namespace",
        )
    )

    assert prerequisite["ansible.builtin.command"]["argv"][:2] == ["/usr/bin/env", "python3"]
    assert "sys.version_info >= (3, 10)" in prerequisite["ansible.builtin.command"]["argv"][3]
    assert prerequisite["failed_when"] is False
    assert prerequisite["check_mode"] is False
    assert prerequisite["no_log"] is True
    assert refusal["ansible.builtin.assert"]["fail_msg"] == (
        "Receiver source readback requires Python 3.10 or newer with PyYAML; no "
        "availability or producer-source change was placed."
    )
    assert tasks.index(prerequisite) < tasks.index(refusal)
    assert tasks.index(refusal) < tasks.index(source_helper) < tasks.index(namespace_helper)
    assert source_helper["when"] == prerequisite["when"]
    assert namespace_helper["when"] == "_o11y_availability.enabled | bool"


def test_playbook_has_one_sensitive_remote_invocation_exact_sha_and_check_mode_gate():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/survey-o11y-scrape-baseline.yml").read_text())
    source_guard = next(
        play for play in playbook if play.get("name") == "Require the reviewed source and survey helper bytes"
    )
    assert source_guard["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/files/summarize-o11y-scrape-baseline.py",
        "platform/playbooks/filter_plugins/o11y_scrape_budget.py",
    ]
    controller = next(
        play for play in playbook if play.get("name") == "Validate the reviewed production survey context"
    )
    assert any("expected_repository_sha" in str(task.get("ansible.builtin.assert", {})) for task in controller["tasks"])

    receiver = next(play for play in playbook if play.get("name") == "Read and reduce the production scrape baseline")
    prepare = next(
        task for task in receiver["tasks"]
        if task.get("name") == "Prepare the reviewed native availability source projection"
    )
    assert prepare["ansible.builtin.import_tasks"] == "tasks/prepare-o11y-availability.yml"
    assert prepare["when"] == "not ansible_check_mode"
    assert receiver["vars"]["_deploy_dir"] == "{{ _survey_monorepo_dir }}/{{ monorepo_deploy_path }}"
    protected = [task for task in receiver["tasks"] if task.get("no_log")]
    assert len(protected) == 10
    invocation = next(
        task for task in protected if task["name"] == "Collect and sanitize the active scrape baseline on the receiver"
    )
    envelope = next(task for task in protected if task["name"] == "Parse and classify the helper response")
    parse = next(task for task in envelope["block"] if task["name"] == "Parse the helper's bounded public projection")
    compare = next(task for task in protected if task["name"] == "Compute bounded loaded inline scrape-limit coverage")
    source_compare = next(
        task for task in protected if task["name"] == "Compute bounded on-disk split scrape-limit coverage"
    )
    alert_projection = next(
        task for task in protected if task["name"] == "Keep alert-input availability independent and bounded"
    )
    alert_validation = next(
        task for task in protected if task["name"] == "Validate the fixed alert-input availability projection"
    )
    source_coverage = next(
        task for task in protected if task["name"] == "Compute source-specific availability coverage"
    )
    source_validation = next(
        task for task in protected if task["name"] == "Validate the fixed source-specific availability projection"
    )
    assert "delegate_to" not in invocation
    assert "ansible.builtin.script" in invocation
    assert invocation["when"] == "not ansible_check_mode"
    assert envelope["no_log"] is True
    assert parse["no_log"] is True
    assert compare["no_log"] is True
    assert source_compare["no_log"] is True
    assert alert_projection["no_log"] is True
    assert alert_validation["no_log"] is True
    assert alert_validation["when"] == "not ansible_check_mode"
    assert source_coverage["no_log"] is True
    assert source_validation["no_log"] is True
    assert "O11Y_AVAILABILITY_SOURCE_JSON" in invocation["environment"]
    assert "require_health_targets" in receiver["vars"]["_survey_availability_source_json"]
    candidate_block = next(
        task for task in receiver["tasks"]
        if task.get("name") == "Derive reviewed Alloy candidate bytes from the committed renderer"
    )
    candidate_render = next(
        task for task in candidate_block["block"]
        if task.get("name") == "Render the reviewed Alloy candidate on the controller"
    )
    candidate_stat = next(
        task for task in candidate_block["block"]
        if task.get("name") == "Hash the controller-rendered Alloy candidate privately"
    )
    assert candidate_render["delegate_to"] == "localhost"
    assert candidate_stat["delegate_to"] == "localhost"
    assert candidate_render["ansible.builtin.template"]["src"] == (
        "{{ playbook_dir }}/../services/o11y/deployment/templates/config.alloy.j2"
    )
    assert candidate_block["always"][0]["ansible.builtin.file"]["state"] == "absent"
    assert "config_yaml" not in str(
        next(task for task in receiver["tasks"] if task["name"] == "Report loaded inline scrape-limit coverage")
    )
    visible_assert = next(
        task for task in receiver["tasks"] if task["name"] == "Require a complete and bounded baseline receipt"
    )
    assert "_baseline_run" not in str(visible_assert["ansible.builtin.assert"])
    assert "_scrape_survey_result" not in str(
        next(task for task in receiver["tasks"] if task["name"] == "Report loaded inline scrape-limit coverage")
    )
    assert visible_assert["ansible.builtin.assert"]["fail_msg"] == (
        "The scrape baseline is unavailable, unhealthy, ambiguous, or outside the bounded public receipt schema."
    )
    assert all(task.get("no_log") is not True for task in receiver["tasks"] if task not in protected)
    check_mode = next(task for task in receiver["tasks"] if task.get("name") == "Mark check-mode results as unverified")
    assert check_mode["when"] == "ansible_check_mode"
    assert check_mode["ansible.builtin.debug"]["msg"]["loaded_limits"]["status"] == "check_mode_unverified"
    assert check_mode["ansible.builtin.debug"]["msg"]["source_limits"]["status"] == "check_mode_unverified"
    assert check_mode["ansible.builtin.debug"]["msg"]["alert_input_availability"]["status"] == "check_mode_unverified"
    assert check_mode["ansible.builtin.debug"]["msg"]["native_availability_source"]["status"] == "check_mode_unverified"
    helper_source = HELPER.read_text()
    legacy_summary = helper_source[
        helper_source.index("def summarize("):helper_source.index("def _run_api(")
    ]
    # Native scrape_body_size_bytes is an exporter-metrics response counter;
    # it must not be mistaken for the remote health response body measurement.
    assert "scrape_body_size_bytes" not in legacy_summary


def test_playbook_classifies_helper_envelope_before_baseline_dereference():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/survey-o11y-scrape-baseline.yml").read_text())
    receiver = next(play for play in playbook if play.get("name") == "Read and reduce the production scrape baseline")
    tasks = receiver["tasks"]
    envelope = next(task for task in tasks if task.get("name") == "Parse and classify the helper response")
    parse = next(
        task for task in envelope["block"] if task.get("name") == "Parse the helper's bounded public projection"
    )
    classify = next(task for task in envelope["block"] if task.get("name") == "Classify the helper response envelope")
    validate = next(task for task in envelope["block"] if task.get("name") == "Validate the helper success envelope")
    failure = next(task for task in tasks if task.get("name") == "Require a complete helper response envelope")
    baseline = next(
        task for task in tasks
        if task.get("name") == "Keep the active scrape baseline separate from loaded-limit evidence"
    )

    assert envelope["no_log"] is True
    assert parse["no_log"] is True
    assert "from_json" in parse["ansible.builtin.set_fact"]["_scrape_survey_result"]
    assert envelope["rescue"][0]["ansible.builtin.set_fact"]["_scrape_helper_receipt"] == {
        "status": "unavailable", "reason": "malformed_helper_response"
    }
    assert tasks.index(failure) < tasks.index(baseline)
    assert failure["ansible.builtin.assert"]["that"] == [
        "_scrape_helper_receipt.status == 'observed'"
    ]
    assert "_scrape_helper_receipt.status" in failure["ansible.builtin.assert"]["fail_msg"]
    assert "_scrape_helper_receipt.reason" in failure["ansible.builtin.assert"]["fail_msg"]
    assert "stdout" not in failure["ansible.builtin.assert"]["fail_msg"]
    assert "stderr" not in failure["ansible.builtin.assert"]["fail_msg"]
    assert "selector" not in failure["ansible.builtin.assert"]["fail_msg"]
    assert "credential" not in failure["ansible.builtin.assert"]["fail_msg"]

    expressions = classify["ansible.builtin.set_fact"]["_scrape_helper_receipt"]
    valid_expression = validate["ansible.builtin.set_fact"]["_scrape_helper_envelope_valid"]
    renderer = Environment(undefined=StrictUndefined)
    reasons = receiver["vars"]["_survey_helper_fixed_reasons"]
    expected_fixed_reasons = {
        "ambiguous_metric",
        "ambiguous_series",
        "ambiguous_targets",
        "curl_unsupported",
        "malformed_samples",
        "malformed_series",
        "malformed_targets",
        "missing_probe_data",
        "missing_samples",
        "missing_series",
        "no_active_targets",
        "probe_compressed",
        "probe_http_status",
        "probe_timeout",
        "probe_too_large",
        "probe_unavailable",
        "query_unavailable",
        "series_unavailable",
        "survey_unavailable",
        "unhealthy_target",
        "unsafe_target_url",
        "unrecognized_job",
    }
    assert set(reasons) == expected_fixed_reasons

    def render(result):
        context = {"_scrape_survey_result": result, "_survey_helper_fixed_reasons": reasons}
        context["_scrape_helper_envelope_valid"] = (
            renderer.from_string(valid_expression).render(context) == "True"
        )
        return {
            key: renderer.from_string(value).render(context)
            for key, value in expressions.items()
        }

    complete = {
        "baseline": {},
        "loaded_limit_readback": {},
        "source_limit_readback": {},
        "alert_input_availability": {},
        "native_availability_source": {},
    }
    assert render(complete) == {"status": "observed", "reason": "complete_receipt"}
    for reason in expected_fixed_reasons:
        assert render({"status": "unavailable", "reason": reason}) == {
            "status": "unavailable", "reason": reason
        }
    assert render({"status": "unavailable", "reason": "secret-path-sentinel"}) == {
        "status": "unavailable", "reason": "malformed_helper_response"
    }
    untrusted = render({
        "status": "unavailable", "reason": "query_unavailable", "selector": "private-selector-sentinel"
    })
    assert untrusted == {"status": "unavailable", "reason": "malformed_helper_response"}
    assert "private-selector-sentinel" not in json.dumps(untrusted)
    assert render({"baseline": {}}) == {
        "status": "unavailable", "reason": "malformed_helper_response"
    }


def _alert_metric(family, job, instance, timestamp, value, reason):
    return {
        "metric": {"__name__": family, "job": job, "instance": instance, "reason": reason},
        "value": [timestamp, value],
    }


def test_alert_input_availability_uses_lookback_and_empty_vector_for_missing_or_stale(monkeypatch):
    targets = [
        _target("alloy", "alloy-private:4317"),
        _target("loki", "loki-private:3100"),
        _target("tempo", "tempo-private:3200"),
    ]
    families = [family for family, _job in SURVEY.ALERT_INPUTS]
    results = {
        families[0]: [
            _alert_metric(families[0], "alloy", "alloy-private:4317", 1000, "0", "reason-private-a"),
            _alert_metric(families[0], "alloy", "alloy-private:4317", 1000, "0", "reason-private-b"),
        ],
        families[1]: [_alert_metric(families[1], "alloy", "alloy-private:4317", 700, "2", "private-reason")],
        families[2]: [],
        families[3]: [_alert_metric(families[3], "alloy", "alloy-private:4317", 699, "0", "stale")],
        families[4]: b'{"status":"success","private_payload":"API-BODY-SENTINEL"}',
        families[5]: SURVEY.Refusal("alert_query_unavailable"),
    }
    queries = []
    lookbacks = []

    def run_api(path, _reason="query_unavailable"):
        params = parse_qs(urlsplit("http://prometheus" + path).query)
        query = params["query"][0]
        queries.append(query)
        lookbacks.append(params["lookback_delta"])
        family = next(name for name in families if query.startswith(name + "{"))
        assert "or vector(0)" not in query
        result = results[family]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bytes):
            return result
        return _vector(result).encode()

    monkeypatch.setattr(SURVEY, "_run_api", run_api)
    receipt = SURVEY._alert_input_availability(_targets(targets))

    assert receipt["status"] == "observed"
    assert receipt["families"] == [
        {"family": families[0], "status": "observed_zero", "reason": "samples_present"},
        {"family": families[1], "status": "observed_nonzero", "reason": "samples_present"},
        {"family": families[2], "status": "missing_or_stale", "reason": "no_samples"},
        {"family": families[3], "status": "observed_zero", "reason": "samples_present"},
        {"family": families[4], "status": "unavailable", "reason": "malformed_response"},
        {"family": families[5], "status": "unavailable", "reason": "api_unavailable"},
    ]
    assert len(queries) == 6
    assert lookbacks == [["5m"]] * 6
    assert all("or%20vector" not in query for query in queries)
    public = json.dumps(receipt)
    for sentinel in ("alloy-private", "loki-private", "tempo-private", "private-reason", "API-BODY-SENTINEL"):
        assert sentinel not in public


@pytest.mark.parametrize(
    "row,expected_reason",
    [
        (_alert_metric("counter", "alloy", "alloy-private:4317", 1000, "-1", "bad"), "malformed_sample"),
        (_alert_metric("counter", "alloy", "alloy-private:4317", 1000, "Infinity", "bad"), "malformed_sample"),
        (_alert_metric("counter", "alloy", "alloy-private:4317", "bad-time", "1", "bad"), "malformed_sample"),
        (_alert_metric("counter", "other", "foreign:4317", 1000, "1", "bad"), "ambiguous_target"),
    ],
)
def test_alert_input_rejects_negative_or_ambiguous_samples_without_echoing_labels(row, expected_reason, monkeypatch):
    family = SURVEY.ALERT_INPUTS[0]
    row["metric"]["__name__"] = family[0]
    targets = _targets([_target("alloy", "alloy-private:4317")])
    raw = _vector([row]).encode()
    monkeypatch.setattr(SURVEY, "_run_api", lambda *_args: raw)
    receipt = SURVEY._alert_input_availability(targets)
    assert receipt["families"][0] == {
        "family": family[0], "status": "unavailable", "reason": expected_reason
    }


def test_alert_input_oversized_response_is_unavailable(monkeypatch):
    monkeypatch.setattr(SURVEY, "MAX_API_BYTES", 1)
    raw = _vector([]).encode()
    monkeypatch.setattr(SURVEY, "_targets", lambda _raw: {("alloy", "alloy-private:4317"): ("alloy", "http://alloy-private:4317/metrics")})
    monkeypatch.setattr(SURVEY, "_run_api", lambda *_args: raw)
    receipt = SURVEY._alert_input_availability(
        _targets([_target("alloy", "alloy-private:4317")])
    )
    assert receipt["families"][0] == {
        "family": SURVEY.ALERT_INPUTS[0][0], "status": "unavailable", "reason": "malformed_response"
    }


def test_alert_input_failure_does_not_replace_the_required_baseline(monkeypatch):
    baseline = {"status": "observed", "active_target_count": 1, "jobs": [{"job": "alloy"}]}
    targets_raw = _targets([_target("alloy", "alloy-private:4317")]).encode()

    monkeypatch.setattr(SURVEY, "_require_curl_version", lambda: None)
    monkeypatch.setattr(
        SURVEY,
        "_run_api",
        lambda path, *_args: targets_raw if path == "/api/v1/targets?state=active" else _vector([]).encode(),
    )
    monkeypatch.setattr(SURVEY, "summarize", lambda *_args: baseline)
    monkeypatch.setattr(SURVEY, "_loaded_limit_readback", lambda *_args: {"status": "unavailable", "reason": "test"})
    def fail_alert_receipt(*_args):
        raise RuntimeError("secret")

    monkeypatch.setattr(SURVEY, "_alert_input_availability", fail_alert_receipt)

    receipt = SURVEY.collect()

    assert receipt["baseline"] is baseline
    assert receipt["loaded_limit_readback"] == {"status": "unavailable", "reason": "test"}
    assert all(family["status"] == "unavailable" for family in receipt["alert_input_availability"]["families"])
    assert "secret" not in json.dumps(receipt)


def test_alert_input_playbook_reports_only_fixed_projection_and_skips_check_mode():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/survey-o11y-scrape-baseline.yml").read_text())
    receiver = next(play for play in playbook if play.get("name") == "Read and reduce the production scrape baseline")
    tasks = receiver["tasks"]
    report = next(task for task in tasks if task.get("name") == "Report bounded alert-input availability")
    check_mode = next(task for task in tasks if task.get("name") == "Mark check-mode results as unverified")

    assert report["when"] == "not ansible_check_mode"
    assert report["ansible.builtin.debug"]["msg"] == "{{ _scrape_alert_input_availability | to_json }}"
    assert "_scrape_survey_result" not in str(report)
    assert check_mode["ansible.builtin.debug"]["msg"]["alert_input_availability"] == {
        "status": "check_mode_unverified",
        "reason": "Prometheus alert-input queries are skipped in check mode.",
        "families": [],
    }
    source_report = next(task for task in tasks if task.get("name") == "Report source-specific availability coverage")
    assert source_report["when"] == "not ansible_check_mode"
    assert source_report["ansible.builtin.debug"]["msg"] == "{{ _scrape_availability_source_coverage | to_json }}"
    assert "_scrape_survey_result" not in str(source_report)
