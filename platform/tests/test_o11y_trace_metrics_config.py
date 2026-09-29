"""Contract checks for the gated Tempo service graph and correlation config."""

import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "platform/services/o11y/deployment"


def _render_env(derived_metrics: bool) -> dict[str, str]:
    template = Environment(undefined=StrictUndefined)
    template.filters["bool"] = bool
    source = (DEPLOY / "templates/env.j2").read_text(encoding="utf-8")
    rendered = template.from_string(source).render(
        secrets={
            "grafana_admin_password": "test-only",
            "grafana_oidc_client_secret": "test-only",
        },
        local_mode=False,
        o11y_trace_derived_metrics_enabled=derived_metrics,
    )
    return dict(
        line.split("=", 1)
        for line in rendered.splitlines()
        if "=" in line and not line.startswith("#")
    )


def _tempo_config(processors: str) -> dict:
    source = (DEPLOY / "config/tempo-config.yml").read_text(encoding="utf-8")
    source = re.sub(r"\$\{O11Y_TEMPO_RETENTION:-168h\}", "168h", source)
    source = re.sub(r"\$\{O11Y_TEMPO_METRIC_PROCESSORS:-\[\]}", processors, source)
    return yaml.safe_load(source)


def test_tempo_metrics_are_disabled_by_default_and_gated_enablement_is_bounded():
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
    assert tempo["tracesToProfiles"]["datasourceUid"] == "pyroscope"
    assert tempo["tracesToProfiles"]["tags"] == [
        {"key": "service.name", "value": "service_name"}
    ]


def test_production_metrics_enablement_requires_all_recorded_gates_and_live_readback():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text())
    deploy_tasks = [task for play in playbook for task in play.get("tasks", [])]
    gate = next(task for task in deploy_tasks if "numeric capacity and backup/restore receipts" in task["name"])
    assert set(gate["ansible.builtin.assert"]["that"]) == {
        "o11y_capacity_receipt_id | default('') | string is match('^[1-9][0-9]*$')",
        "o11y_trace_backup_restore_receipt_id | default('') | string is match('^[1-9][0-9]*$')",
    }
    assert gate["when"] == [
        "not (local_mode | default(false) | bool)",
        "o11y_trace_derived_metrics_enabled | default(false) | bool",
    ]
    tempo_readback = next(
        task
        for task in deploy_tasks
        if task["name"] == "Require the effective Tempo processor, remote-write, and series limits"
    )
    processor_check = next(
        check
        for check in tempo_readback["ansible.builtin.assert"]["that"]
        if "metrics_generator.processors" in check
    )
    assert "| default([], true) | list | sort" in processor_check
    assert "['service-graphs', 'span-metrics']" in processor_check
    config_tasks = [task for task in deploy_tasks if task.get("no_log") is True]
    assert any(task["name"] == "Read Tempo's effective metrics-generator configuration" for task in config_tasks)
    parse_task = next(
        task
        for task in deploy_tasks
        if task["name"] == "Parse Tempo's effective configuration YAML documents"
    )
    assert "from_yaml_all" in parse_task["ansible.builtin.set_fact"]["_tempo_config_documents"]
    candidate_task = next(
        task
        for task in deploy_tasks
        if task["name"] == "Select Tempo configuration documents by expected top-level keys"
    )
    candidate_filter = candidate_task["ansible.builtin.set_fact"]["_tempo_config_candidates"]
    assert "from_yaml_all" in candidate_filter
    assert "selectattr('metrics_generator', 'defined')" in candidate_filter
    assert "selectattr('overrides', 'defined')" in candidate_filter
    candidate_gate = next(
        task
        for task in deploy_tasks
        if task["name"] == "Require exactly one identifiable Tempo configuration document"
    )
    assert candidate_gate["ansible.builtin.assert"]["that"] == "_tempo_config_candidates | length == 1"
    assert "_tempo_config_candidates | length" in candidate_gate["ansible.builtin.assert"]["fail_msg"]
    assert "_tempo_config_documents | length" in candidate_gate["ansible.builtin.assert"]["fail_msg"]
    trace_gate = next(task for task in deploy_tasks if "recorded trace rollout gate" in task["name"])
    assert "o11y_trace_derived_metrics_enabled | default(false) | bool" in trace_gate["when"]


def test_tempo_status_config_readback_selects_one_mapping_from_multiple_yaml_documents():
    # Generic multi-document contract fixture; this does not claim the exact live Tempo response shape.
    response = """\
server:
  http_listen_port: 3200
---
metrics_generator:
  storage:
    path: /var/tempo/generator/wal
    remote_write:
      - url: http://prometheus:9090/api/v1/write
overrides:
  defaults:
    metrics_generator:
      processors: []
      max_active_series: 2000
"""
    with pytest.raises(yaml.composer.ComposerError, match="expected a single document"):
        yaml.safe_load(response)

    playbook = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text())
    deploy_tasks = [task for play in playbook for task in play.get("tasks", [])]
    candidate_task = next(
        task
        for task in deploy_tasks
        if task["name"] == "Select Tempo configuration documents by expected top-level keys"
    )
    candidate_filter = candidate_task["ansible.builtin.set_fact"]["_tempo_config_candidates"].strip()
    assert candidate_filter.startswith("{{") and candidate_filter.endswith("}}")
    environment = Environment()
    environment.filters["from_yaml_all"] = yaml.safe_load_all
    select_candidates = environment.compile_expression(candidate_filter[2:-2])

    def matching_configs(config_text: str) -> list[dict]:
        return select_candidates(_tempo_config={"stdout": config_text})

    candidates = matching_configs(response)
    assert len(list(yaml.safe_load_all(response))) == 2
    assert len(candidates) == 1
    assert candidates[0]["overrides"]["defaults"]["metrics_generator"]["max_active_series"] == 2000
    assert matching_configs("") == []
    assert matching_configs("server:\n  http_listen_port: 3200\n") == []
    ambiguous_response = yaml.safe_dump_all(
        list(yaml.safe_load_all(response)) + [candidates[0]]
    )
    assert len(matching_configs(ambiguous_response)) == 2
