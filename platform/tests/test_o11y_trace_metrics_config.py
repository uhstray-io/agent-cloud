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
CHECKER = ROOT / "platform/playbooks/files/verify-tempo-metrics-config.py"
checker_spec = importlib.util.spec_from_file_location("tempo_metrics_checker", CHECKER)
tempo_checker = importlib.util.module_from_spec(checker_spec)
checker_spec.loader.exec_module(tempo_checker)


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
        if task["name"] == "Require exactly one valid Tempo configuration and bounded metrics settings"
    )
    assert "_tempo_metrics_config_check.rc == 0" in tempo_readback["ansible.builtin.assert"]["that"]
    assert "processors_match" in str(tempo_readback["ansible.builtin.assert"]["that"])
    checker_task = next(
        task for task in deploy_tasks
        if task["name"] == "Validate Tempo's effective metrics-generator configuration"
    )
    assert checker_task["no_log"] is True
    assert checker_task["delegate_to"] == "localhost"
    assert checker_task["ansible.builtin.command"]["stdin"] == "{{ _tempo_config.stdout }}"
    assert "verify-tempo-metrics-config.py" in str(checker_task["ansible.builtin.command"]["argv"])
    config_tasks = [task for task in deploy_tasks if task.get("no_log") is True]
    assert any(task["name"] == "Read Tempo's effective metrics-generator configuration" for task in config_tasks)
    assert "_tempo_config_candidates" not in str(deploy_tasks)
    trace_gate = next(task for task in deploy_tasks if "recorded trace rollout gate" in task["name"])
    assert "o11y_trace_derived_metrics_enabled | default(false) | bool" in trace_gate["when"]


def _fixture_config(processors):
    return {
        "metrics_generator": {
            "storage": {
                "path": "/var/tempo/generator/wal",
                "remote_write": [{"url": "http://prometheus:9090/api/v1/write"}],
            }
        },
        "overrides": {
            "defaults": {
                "metrics_generator": {
                    "processors": processors,
                    "max_active_series": 2000,
                }
            }
        },
    }


def test_tempo_metrics_checker_returns_only_sanitized_results():
    # Generic multi-document contract fixture; not a claim about the exact live response shape.
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
    code, result = tempo_checker.validate(response, enabled=False)
    assert code == 0
    assert result == {
        "status": "verified",
        "reason": None,
        "document_count": 2,
        "candidate_count": 1,
        "processors_match": True,
        "max_active_series_match": True,
        "remote_write_match": True,
        "wal_path_match": True,
    }
    assert "http://prometheus" not in str(result)


def test_tempo_checker_counts_large_multi_document_input_as_documents():
    # Models the production failure's reported counts without asserting a live Tempo response.
    response = "\n---\n".join(
        ["metrics_generator: {}\noverrides: {}"] * 37257 + ["server: {}"] * 22
    )
    code, result = tempo_checker.validate(response, enabled=False)
    assert code == 2
    assert result["document_count"] == 37279
    assert result["candidate_count"] == 37257
    assert result["status"] == "refused"


@pytest.mark.parametrize("processors", [None, "absent"])
def test_tempo_checker_accepts_null_or_omitted_disabled_processors(processors):
    config = _fixture_config([])
    if processors == "absent":
        del config["overrides"]["defaults"]["metrics_generator"]["processors"]
    else:
        config["overrides"]["defaults"]["metrics_generator"]["processors"] = processors
    code, result = tempo_checker.validate(yaml.safe_dump(config), enabled=False)
    assert code == 0
    assert result["processors_match"] is True


def _run_tempo_checker_task(tmp_path, response, enabled, expected_status, documents, candidates):
    playbook = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    deploy_tasks = [task for play in playbook for task in play.get("tasks", [])]
    checker_task = next(
        task for task in deploy_tasks
        if task["name"] == "Validate Tempo's effective metrics-generator configuration"
    )
    (tmp_path / "files").symlink_to(CHECKER.parent, target_is_directory=True)
    fixture_playbook = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "_tempo_config": {"stdout": response},
                "o11y_trace_derived_metrics_enabled": enabled,
                "expected_status": expected_status,
                "expected_documents": documents,
                "expected_candidates": candidates,
            },
            "tasks": [
                checker_task,
                {
                    "name": "Assert sanitized Tempo checker result",
                    "ansible.builtin.assert": {
                        "that": [
                            "(_tempo_metrics_config_check.stdout | from_json).status == expected_status",
                            "(_tempo_metrics_config_check.stdout | from_json).document_count == expected_documents",
                            "(_tempo_metrics_config_check.stdout | from_json).candidate_count == expected_candidates",
                            "_tempo_metrics_config_check.rc == (0 if expected_status == 'verified' else 2)",
                        ]
                    },
                },
            ],
        }
    ]
    fixture_path = tmp_path / "tempo-check.yml"
    fixture_path.write_text(yaml.safe_dump(fixture_playbook), encoding="utf-8")
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all:\n  hosts:\n    localhost:\n      ansible_connection: local\n", encoding="utf-8")
    env = harness_sandbox.env_for(tmp_path)
    return harness_sandbox.run(
        ["ansible-playbook", "-i", str(inventory), str(fixture_path)],
        tmp_path,
        cwd=ROOT,
        env=env,
    )


@pytest.mark.parametrize(
    ("response", "enabled", "status", "documents", "candidates"),
    [
        (
            yaml.safe_dump({"server": {"http_listen_port": 3200}})
            + "---\n"
            + yaml.safe_dump(_fixture_config([])),
            False,
            "verified",
            2,
            1,
        ),
        ("", False, "refused", 0, 0),
        (
            yaml.safe_dump_all(
                [
                    {"server": {"http_listen_port": 3200}},
                    _fixture_config(["service-graphs", "span-metrics"]),
                ]
            ),
            True,
            "verified",
            2,
            1,
        ),
        (yaml.safe_dump_all([_fixture_config([]), _fixture_config([])]), False, "refused", 2, 2),
    ],
    ids=[
        "generic-two-document-contract",
        "empty-response",
        "enabled-two-document-contract",
        "ambiguous-mapping",
    ],
)
def test_ansible_executes_production_tempo_checker_task(
    tmp_path, response, enabled, status, documents, candidates
):
    result = _run_tempo_checker_task(
        tmp_path, response, enabled, status, documents, candidates
    )
    assert result.returncode == 0, result.stdout + result.stderr
