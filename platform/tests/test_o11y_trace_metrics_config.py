"""Contract checks for the gated Tempo service graph and correlation config."""

import importlib.util
import re
from pathlib import Path

import harness_sandbox
import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "platform/services/o11y/deployment"
PLAYBOOK = ROOT / "platform/playbooks/deploy-o11y.yml"
PLUGIN = ROOT / "platform/playbooks/filter_plugins/tempo_config.py"
SCRAPE_LIMITS = {
    "body_size_limit_bytes": 16 * 1024 * 1024,
    "sample_limit": 10000,
    "label_limit": 64,
    "label_name_length_limit": 128,
    "label_value_length_limit": 2048,
}


def _mandatory(value):
    from jinja2 import Undefined

    if isinstance(value, Undefined):
        raise ValueError("production scrape budgets must be declared in inventory")
    return value


plugin_spec = importlib.util.spec_from_file_location("tempo_config", PLUGIN)
tempo_config = importlib.util.module_from_spec(plugin_spec)
plugin_spec.loader.exec_module(tempo_config)


def _render_env(derived_metrics: bool) -> dict[str, str]:
    """Render the service environment with derived metrics either disabled or gated on."""
    template = Environment(undefined=StrictUndefined)
    template.filters["bool"] = bool
    template.filters["mandatory"] = _mandatory
    source = (DEPLOY / "templates/env.j2").read_text(encoding="utf-8")
    rendered = template.from_string(source).render(
        secrets={
            "grafana_admin_password": "test-only",
            "grafana_oidc_client_secret": "test-only",
        },
        local_mode=False,
        o11y_scrape_limits_reviewed=True,
        o11y_scrape_limits=SCRAPE_LIMITS.copy(),
        o11y_trace_derived_metrics_enabled=derived_metrics,
    )
    return dict(
        line.split("=", 1)
        for line in rendered.splitlines()
        if "=" in line and not line.startswith("#")
    )


def _tempo_config(processors: str) -> dict:
    """Render the Tempo config's environment substitutions for a processor list."""
    source = (DEPLOY / "config/tempo-config.yml").read_text(encoding="utf-8")
    source = re.sub(r"\$\{O11Y_TEMPO_RETENTION:-168h\}", "168h", source)
    source = re.sub(r"\$\{O11Y_TEMPO_METRIC_PROCESSORS:-\[\]}", processors, source)
    return yaml.safe_load(source)


def test_tempo_metrics_are_disabled_by_default_and_gated_enablement_is_bounded():
    """Keep Tempo derived metrics disabled by default and bound enabled dimensions."""
    assert _render_env(False)["O11Y_TEMPO_METRIC_PROCESSORS"] == "[]"
    assert _render_env(True)["O11Y_TEMPO_METRIC_PROCESSORS"] == "[service-graphs, span-metrics]"

    compose = yaml.safe_load((DEPLOY / "compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["tempo"]["environment"]["O11Y_TEMPO_METRIC_PROCESSORS"] == (
        "${O11Y_TEMPO_METRIC_PROCESSORS:-[]}"
    )

    disabled = _tempo_config("[]")
    enabled = _tempo_config("[service-graphs, span-metrics]")
    assert disabled["overrides"]["defaults"]["metrics_generator"]["processors"] == []
    assert enabled["overrides"]["defaults"]["metrics_generator"]["processors"] == [
        "service-graphs",
        "span-metrics",
    ]
    assert enabled["overrides"]["defaults"]["metrics_generator"]["max_active_series"] == 2000
    assert enabled["metrics_generator"]["storage"]["remote_write"] == [
        {"url": "http://prometheus:9090/api/v1/write"}
    ]
    dimensions = enabled["metrics_generator"]["processor"]["span_metrics"]["intrinsic_dimensions"]
    assert dimensions == {
        "service": True,
        "span_name": False,
        "span_kind": True,
        "status_code": True,
        "status_message": False,
    }


def test_tempo_datasource_keeps_log_link_and_provisions_service_map_metric_and_profile_links():
    """Keep trace-to-log links and provision Tempo's metrics and profile correlations."""
    config = yaml.safe_load(
        (DEPLOY / "config/grafana/provisioning/datasources/datasources.yml").read_text(
            encoding="utf-8"
        )
    )
    datasources = {source["uid"]: source for source in config["datasources"]}
    tempo = datasources["tempo"]["jsonData"]
    assert tempo["serviceMap"]["datasourceUid"] == "prometheus"
    assert tempo["tracesToMetrics"]["datasourceUid"] == "prometheus"
    assert tempo["tracesToMetrics"]["queries"] == [
        {
            "name": "Span calls",
            "query": "sum(rate(traces_spanmetrics_calls_total{$$__tags}[5m]))",
        }
    ]
    assert tempo["tracesToLogsV2"]["datasourceUid"] == "loki"
    assert tempo["tracesToLogsV2"]["tags"] == [
        {"key": "service.name", "value": "service"}
    ]
    assert tempo["tracesToLogsV2"]["spanStartTimeShift"] == "-2s"
    assert tempo["tracesToLogsV2"]["spanEndTimeShift"] == "2s"
    assert tempo["tracesToLogsV2"]["filterByTraceID"] is False
    assert tempo["tracesToLogsV2"]["filterBySpanID"] is False
    assert tempo["tracesToLogsV2"]["customQuery"] is True
    query = (
        '{$${__tags}} | json | '
        '(traceid=~"0*$${__trace.traceId}" or '
        'body=~".*(^|[[:space:]])tid=0*$${__trace.traceId}([[:space:]]|$).*")'
    )
    runtime_query_assertion = (
        "(_tempo_datasource_config.stdout | from_json).jsonData.tracesToLogsV2.query == "
        f"{query.replace('$$', '$')!r}"
    )
    assert tempo["tracesToLogsV2"]["query"] == query
    assert tempo["tracesToProfiles"]["datasourceUid"] == "pyroscope"
    assert tempo["tracesToProfiles"]["tags"] == [
        {"key": "service.name", "value": "service_name"}
    ]

    playbook = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text())
    tempo_check = next(
        task
        for play in playbook
        for task in play.get("tasks", [])
        if task.get("name") == "Require live Tempo trace-to-metric and trace-to-log mappings"
    )
    assert {
        "(_tempo_datasource_config.stdout | from_json).jsonData.tracesToLogsV2.spanStartTimeShift == '-2s'",
        "(_tempo_datasource_config.stdout | from_json).jsonData.tracesToLogsV2.spanEndTimeShift == '2s'",
        "(_tempo_datasource_config.stdout | from_json).jsonData.tracesToLogsV2.filterByTraceID == false",
        "(_tempo_datasource_config.stdout | from_json).jsonData.tracesToLogsV2.filterBySpanID == false",
        "(_tempo_datasource_config.stdout | from_json).jsonData.tracesToLogsV2.customQuery == true",
        runtime_query_assertion,
    } <= set(tempo_check["ansible.builtin.assert"]["that"])


def test_production_retention_requires_numeric_backup_restore_reference_before_writes():
    """Require a numeric reference before volume inspection or repo placement."""
    playbook = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text())
    phase_one = next(play for play in playbook if play.get("name") == "Phase 1: Place repo + manage o11y secrets")
    deploy_tasks = phase_one["tasks"]
    gate_index, gate = next(
        (index, task)
        for index, task in enumerate(deploy_tasks)
        if "backup/restore receipt references" in task["name"]
    )
    receipt_check = "o11y_backup_restore_receipt_id | default('') | string is match('^[1-9][0-9]*$')"
    assert set(gate["ansible.builtin.assert"]["that"]) == {
        "o11y_capacity_receipt_id | default('') | string is match('^[1-9][0-9]*$')",
        receipt_check,
        "o11y_prom_retention_size | default('0B') | string | trim is match('^[1-9][0-9]*(B|KB|MB|GB|TB|PB|EB)$')",
    }
    assert gate["when"] == "not (local_mode | default(false) | bool) and (_o11y_retention_expansion | bool)"
    volume_index = next(
        i
        for i, task in enumerate(deploy_tasks)
        if task["name"] == "Read o11y volume backing capacity before retention expansion"
    )
    placement_index = next(
        i for i, task in enumerate(deploy_tasks) if task["name"] == "Place the monorepo + ensure podman/compose"
    )
    assert gate_index < volume_index < placement_index

    env = Environment(undefined=StrictUndefined)
    env.filters["string"] = str
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    check_receipt = env.compile_expression(receipt_check)
    assert check_receipt() is False
    assert check_receipt(o11y_backup_restore_receipt_id="not-a-number") is False
    assert check_receipt(o11y_backup_restore_receipt_id="0") is False
    assert check_receipt(o11y_backup_restore_receipt_id="123") is True

    all_tasks = [task for play in playbook for task in play.get("tasks", [])]
    metrics_gate = next(task for task in all_tasks if "before enabling Tempo-derived metrics" in task["name"])
    assert set(metrics_gate["ansible.builtin.assert"]["that"]) == {
        "o11y_capacity_receipt_id | default('') | string is match('^[1-9][0-9]*$')",
        receipt_check,
    }
    assert metrics_gate["when"] == [
        "not (local_mode | default(false) | bool)",
        "o11y_trace_derived_metrics_enabled | default(false) | bool",
    ]
    assert not any("o11y_trace_backup_restore_receipt_id" in str(task) for task in all_tasks)

    deploy_tasks = all_tasks
    tempo_readback = next(
        task
        for task in deploy_tasks
        if task["name"] == "Require exactly one valid Tempo configuration and bounded metrics settings"
    )
    assert "tempo_metrics_config_check" in tempo_readback["ansible.builtin.assert"]["that"]
    assert "tempo_metrics_config_check" in tempo_readback["ansible.builtin.assert"]["fail_msg"]
    assert "_tempo_config.stdout" in tempo_readback["ansible.builtin.assert"]["that"]
    config_tasks = [task for task in deploy_tasks if task.get("no_log") is True]
    assert any(task["name"] == "Read Tempo's effective metrics-generator configuration" for task in config_tasks)
    assert "_tempo_config_candidates" not in str(deploy_tasks)
    trace_gate = next(task for task in deploy_tasks if "recorded trace rollout gate" in task["name"])
    assert "o11y_trace_derived_metrics_enabled | default(false) | bool" in trace_gate["when"]


def _fixture_config(
    processors,
    *,
    remote_url="http://prometheus:9090/api/v1/write",
    wal_path="/var/tempo/generator/wal",
    max_active_series=2000,
):
    """Build one effective Tempo config mapping with controllable expected settings."""
    return {
        "fixture_secret_marker": "tempo-config-must-not-appear-in-logs",
        "metrics_generator": {
            "storage": {
                "path": wal_path,
                "remote_write": [{"url": remote_url}],
            }
        },
        "overrides": {
            "defaults": {
                "metrics_generator": {
                    "processors": processors,
                    "max_active_series": max_active_series,
                }
            }
        },
    }


def _yaml_response(*documents):
    """Create a generic multi-document response fixture with a non-config document first."""
    # Generic multi-document contract fixture; not the exact live Tempo response shape.
    return yaml.safe_dump_all([{"server": {"http_listen_port": 3200}}, *documents])


def _assert_task():
    """Return the production Tempo assert task for an isolated localhost play."""
    playbook = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    deploy_tasks = [task for play in playbook for task in play.get("tasks", [])]
    return next(
        task
        for task in deploy_tasks
        if task["name"] == "Require exactly one valid Tempo configuration and bounded metrics settings"
    )


def _run_production_assert(tmp_path, response, enabled):
    """Run the production assert after registering synthetic status output from cat."""
    config_path = tmp_path / "tempo-status-config.yml"
    config_path.write_text(response, encoding="utf-8")
    read_result = {
        "name": "Read synthetic Tempo config as a registered command result",
        "ansible.builtin.command": {"argv": ["cat", str(config_path)]},
        "register": "_tempo_config",
        "no_log": True,
        "changed_when": False,
    }
    fixture_playbook = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {"o11y_trace_derived_metrics_enabled": enabled},
            "tasks": [read_result, _assert_task()],
        }
    ]
    fixture_path = tmp_path / "tempo-assert.yml"
    fixture_path.write_text(yaml.safe_dump(fixture_playbook), encoding="utf-8")
    # The filter comes from the repository ansible.cfg, which applies from cwd=ROOT.
    env = harness_sandbox.env_for(tmp_path)
    return harness_sandbox.run(
        ["ansible-playbook", "-i", "localhost,", str(fixture_path)],
        tmp_path,
        cwd=ROOT,
        env=env,
    )


def test_tempo_filter_returns_only_sanitized_results():
    """Return only scalar validation details from a valid generic multi-document fixture."""
    result = tempo_config.tempo_metrics_config_check(_yaml_response(_fixture_config([])), False)
    assert result == {
        "status": "verified",
        "valid": True,
        "reason": None,
        "document_count": 2,
        "candidate_count": 1,
        "processors_match": True,
        "max_active_series_match": True,
        "remote_write_match": True,
        "wal_path_match": True,
    }
    assert "tempo-config-must-not-appear-in-logs" not in str(result)


def test_tempo_filter_counts_multi_document_input_without_coercion():
    """Count parsed YAML documents and matching config mappings as integers."""
    # Synthetic counts verify document counting, not the exact live Tempo response shape.
    response = "\n---\n".join(
        ["metrics_generator: {}\noverrides: {}"] * 37257 + ["server: {}"] * 22
    )
    result = tempo_config.tempo_metrics_config_check(response, False)
    assert result["document_count"] == 37279
    assert result["candidate_count"] == 37257
    assert result["valid"] is False


def _mutated_config(**changes):
    """Produce a valid candidate mapping with selected validation fields changed."""
    config = _fixture_config(["service-graphs", "span-metrics"])
    config["overrides"]["defaults"]["metrics_generator"].update(
        {key: value for key, value in changes.items() if key in {"processors", "max_active_series"}}
    )
    if "remote_url" in changes:
        config["metrics_generator"]["storage"]["remote_write"][0]["url"] = changes["remote_url"]
    if "remote_write" in changes:
        config["metrics_generator"]["storage"]["remote_write"] = changes["remote_write"]
    if "wal_path" in changes:
        config["metrics_generator"]["storage"]["path"] = changes["wal_path"]
    return config


ANSIBLE_ASSERT_CASES = [
    pytest.param(_yaml_response(_fixture_config([])), False, True, None, id="disabled-empty-processors"),
    pytest.param(_yaml_response(_fixture_config(None)), False, True, None, id="disabled-null-processors"),
    pytest.param(
        _yaml_response(_mutated_config(processors=["span-metrics", "service-graphs"])),
        True,
        True,
        None,
        id="enabled-processors-order-independent",
    ),
    pytest.param(
        _yaml_response(_mutated_config(processors=[])),
        True,
        False,
        "configuration_mismatch",
        id="enabled-processors-missing",
    ),
    pytest.param(
        _yaml_response(_mutated_config(processors=["span-metrics"])),
        True,
        False,
        "configuration_mismatch",
        id="enabled-single-processor",
    ),
    pytest.param(
        _yaml_response(_fixture_config(["service-graphs", "span-metrics"])),
        False,
        False,
        "configuration_mismatch",
        id="disabled-processors-present",
    ),
    pytest.param(
        _yaml_response(_mutated_config(processors=["service-graphs", "span-metrics", "span-metrics"])),
        True,
        False,
        "configuration_mismatch",
        id="duplicate-processor",
    ),
    pytest.param(
        _yaml_response(_mutated_config(remote_url="http://wrong.invalid/write")),
        True,
        False,
        "configuration_mismatch",
        id="wrong-remote-write-url",
    ),
    pytest.param(
        _yaml_response(
            _mutated_config(
                remote_write=[
                    {"url": "http://prometheus:9090/api/v1/write"},
                    {"url": "http://prometheus:9090/api/v1/write"},
                ]
            )
        ),
        True,
        False,
        "configuration_mismatch",
        id="duplicate-remote-write-target",
    ),
    pytest.param(
        """\
metrics_generator:
  storage:
    path: /var/tempo/generator/wal
    remote_write:
      - url: http://prometheus:9090/api/v1/write
        url: http://wrong.invalid/write
overrides:
  defaults:
    metrics_generator:
      processors: [service-graphs, span-metrics]
      max_active_series: 2000
        """,
        True,
        False,
        "invalid_yaml",
        id="duplicate-expected-url-key",
    ),
    pytest.param(
        _yaml_response(_mutated_config(wal_path="/tmp/tempo-wal")),
        True,
        False,
        "configuration_mismatch",
        id="wrong-wal-path",
    ),
    pytest.param(
        _yaml_response(_mutated_config(max_active_series="2000")),
        True,
        False,
        "configuration_mismatch",
        id="series-limit-string",
    ),
    pytest.param(
        _yaml_response(_mutated_config(max_active_series=True)),
        True,
        False,
        "configuration_mismatch",
        id="series-limit-boolean",
    ),
    pytest.param("", False, False, "no_matching_config", id="empty-response"),
    pytest.param(
        yaml.safe_dump_all([_fixture_config([]), _fixture_config([])]),
        False,
        False,
        "ambiguous_config",
        id="ambiguous-config",
    ),
    pytest.param("metrics_generator: [\n", False, False, "invalid_yaml", id="malformed-yaml"),
]


@pytest.mark.parametrize(("response", "enabled", "valid", "expected_reason"), ANSIBLE_ASSERT_CASES)
def test_ansible_executes_production_tempo_assert_with_registered_command_result(
    tmp_path, response, enabled, valid, expected_reason
):
    """Exercise the production assertion against registered synthetic command output."""
    filtered = tempo_config.tempo_metrics_config_check(response, enabled)
    assert filtered["valid"] is valid
    assert filtered["reason"] == expected_reason

    result = _run_production_assert(tmp_path, response, enabled)
    output = result.stdout + result.stderr
    assert "tempo-config-must-not-appear-in-logs" not in output
    if valid:
        assert expected_reason is None
        assert result.returncode == 0, output
    else:
        assert expected_reason is not None
        assert result.returncode != 0
        assert expected_reason in output, output


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="non-string"),
        pytest.param("x" * (4 * 1024 * 1024 + 1), id="oversize-config"),
    ],
)
def test_tempo_filter_refuses_bad_or_oversize_input_without_details(raw, request):
    """Fail closed for invalid inputs and oversized config without returning source text."""
    assert len(request.node.nodeid) < 200
    result = tempo_config.tempo_metrics_config_check(raw, False)
    assert result["valid"] is False
    assert result["status"] == "refused"
    assert result["processors_match"] is False
