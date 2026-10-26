import json
import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined, Undefined

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "platform/services/o11y/deployment"
POLICY = yaml.safe_load(
    (ROOT / "platform/playbooks/vars/o11y-metric-labels.yml").read_text(encoding="utf-8")
)["_o11y_forbidden_metric_label_names_regex"]
LIMITS = {
    "body_size_limit_bytes": 16 * 1024 * 1024,
    "sample_limit": 10000,
    "label_limit": 64,
    "label_name_length_limit": 128,
    "label_value_length_limit": 2048,
}
LIMIT_FIELDS = {
    "body_size_limit": f"{LIMITS['body_size_limit_bytes']}B",
    "sample_limit": LIMITS["sample_limit"],
    "label_limit": LIMITS["label_limit"],
    "label_name_length_limit": LIMITS["label_name_length_limit"],
    "label_value_length_limit": LIMITS["label_value_length_limit"],
}


def _mandatory(value):
    if isinstance(value, Undefined):
        raise ValueError("production scrape budgets must be declared in inventory")
    return value


def _environment():
    env = Environment(undefined=StrictUndefined)
    env.filters["to_json"] = json.dumps
    env.filters["bool"] = bool
    env.filters["mandatory"] = _mandatory
    return env


def _production_validator_accepts(**values):
    tasks = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/assert-o11y-scrape-limits.yml").read_text(encoding="utf-8")
    )
    env = Environment(undefined=StrictUndefined)
    env.filters["bool"] = bool
    env.tests["integer"] = lambda value: isinstance(value, int) and not isinstance(value, bool)
    env.tests["boolean"] = lambda value: isinstance(value, bool)
    env.tests["mapping"] = lambda value: isinstance(value, dict)
    context = {
        "local_mode": False,
        "o11y_scrape_limits_reviewed": True,
        "o11y_scrape_limits": LIMITS.copy(),
        **values,
    }
    for task in tasks:
        assertion = task.get("ansible.builtin.assert")
        if assertion is None:
            continue
        conditions = assertion["that"]
        if isinstance(conditions, str):
            conditions = [conditions]
        if not all(env.compile_expression(condition)(**context) for condition in conditions):
            return False
    return True


def _render_yaml(name, **values):
    source = (DEPLOY / "templates" / name).read_text(encoding="utf-8")
    return yaml.safe_load(_environment().from_string(source).render(**values))


def _assert_bounded(job, sample_limit=None):
    assert job["body_size_limit"] == LIMIT_FIELDS["body_size_limit"]
    assert job["sample_limit"] == (LIMIT_FIELDS["sample_limit"] if sample_limit is None else sample_limit)
    for key in ("label_limit", "label_name_length_limit", "label_value_length_limit"):
        assert job[key] == LIMIT_FIELDS[key]


def _alloy_prometheus_relabel_rules(rendered):
    component = rendered.split('prometheus.relabel "bounded_labels" {', 1)[1].split(
        'prometheus.remote_write "local"', 1
    )[0]
    rules = []
    for body in re.findall(r"\brule\s*\{([^{}]*)\}", component):
        fields = {}
        for name, value in re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", body, re.MULTILINE):
            fields[name] = json.loads(value)
        rules.append(fields)
    return rules


def _apply_alloy_prometheus_relabel_rules(labels, rules):
    labels = labels.copy()
    for rule in rules:
        action = rule.get("action", "replace")
        supported_fields = {
            "labeldrop": {"action", "regex"},
            "replace": {
                "action", "source_labels", "regex", "replacement", "target_label", "separator"
            },
        }.get(action)
        if supported_fields is None:
            raise AssertionError(f"unexpected relabel action in bounded_labels: {action}")
        unsupported_fields = set(rule) - supported_fields
        if unsupported_fields:
            raise AssertionError(
                f"unsupported relabel rule fields for {action}: {sorted(unsupported_fields)}"
            )

        if action == "labeldrop":
            pattern = re.compile(rule["regex"])
            labels = {name: value for name, value in labels.items() if not pattern.fullmatch(name)}
        elif action == "replace":
            source = rule.get("separator", ";").join(
                labels.get(name, "") for name in rule.get("source_labels", [])
            )
            match = re.fullmatch(rule.get("regex", "(.*)"), source)
            if match:
                replacement = re.sub(
                    r"\$(\d+)",
                    lambda group, match=match: match.group(int(group.group(1))) or "",
                    rule.get("replacement", "$1"),
                )
                labels[rule["target_label"]] = replacement
    return labels


def test_alloy_relabel_evaluator_rejects_unsupported_fields():
    rule = {
        "action": "replace",
        "source_labels": [],
        "regex": "(.*)",
        "replacement": "inventory-cluster",
        "target_label": "cluster",
        "unexpected": "ignored",
    }
    with pytest.raises(AssertionError, match="unsupported relabel rule fields for replace"):
        _apply_alloy_prometheus_relabel_rules({}, [rule])


def test_alloy_relabel_evaluator_rejects_unsupported_actions():
    rule = {
        "action": "hashmod",
        "source_labels": ["cluster"],
        "modulus": 2,
        "target_label": "cluster_hash",
    }
    with pytest.raises(AssertionError, match="unexpected relabel action in bounded_labels: hashmod"):
        _apply_alloy_prometheus_relabel_rules({"cluster": "inventory-cluster"}, [rule])


def test_alloy_compose_environment_forwards_bounded_scrape_budgets_without_secrets():
    compose = yaml.safe_load((DEPLOY / "compose.yml").read_text(encoding="utf-8"))
    alloy = compose["services"]["alloy"]
    expected = {
        "O11Y_SCRAPE_SAMPLE_LIMIT": "${O11Y_SCRAPE_SAMPLE_LIMIT:-2000}",
        "O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES": "${O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES:-16777216}",
        "O11Y_SCRAPE_LABEL_LIMIT": "${O11Y_SCRAPE_LABEL_LIMIT:-64}",
        "O11Y_SCRAPE_LABEL_NAME_LENGTH_LIMIT": "${O11Y_SCRAPE_LABEL_NAME_LENGTH_LIMIT:-128}",
        "O11Y_SCRAPE_LABEL_VALUE_LENGTH_LIMIT": "${O11Y_SCRAPE_LABEL_VALUE_LENGTH_LIMIT:-2048}",
    }

    assert alloy["environment"] == expected
    assert "env_file" not in alloy


def test_alloy_socket_discovery_and_container_metrics_render_only_in_local_mode():
    template = _environment().from_string(
        (DEPLOY / "templates/config.alloy.j2").read_text(encoding="utf-8")
    )
    values = {
        "o11y_cluster": "test-cluster",
        "_o11y_forbidden_metric_label_names_regex": POLICY,
    }
    local = template.render(**values, local_mode=True)
    production = template.render(**values, local_mode=False)

    for component in (
        'discovery.docker "containers"',
        'loki.source.docker "default"',
        'discovery.relabel "metrics"',
        'prometheus.scrape "local_containers"',
        'prometheus.relabel "bounded_labels"',
        'prometheus.remote_write "local"',
    ):
        assert component in local
        assert component not in production

    assert f"regex  = {json.dumps(POLICY)}" in local
    assert f"regex  = {json.dumps(POLICY)}" not in production

    for component in (
        'loki.write "default"',
        'otelcol.receiver.otlp "traces"',
        'otelcol.receiver.otlp "conformance"',
        'otelcol.exporter.otlp "tempo"',
    ):
        assert component in local
        assert component in production
    assert "unix:///var/run/docker.sock" not in production


@pytest.mark.parametrize(
    ("inventory", "expected"),
    [
        ({}, {"cluster": "agent-cloud-local", "environment": "local"}),
        (
            {"o11y_cluster": "inventory-east", "o11y_environment": "dev"},
            {"cluster": "inventory-east", "environment": "dev"},
        ),
    ],
)
def test_local_alloy_inventory_labels_replace_app_values_and_keep_bounded_identity(inventory, expected):
    rendered = _environment().from_string(
        (DEPLOY / "templates/config.alloy.j2").read_text(encoding="utf-8")
    ).render(
        local_mode=True,
        _o11y_forbidden_metric_label_names_regex=POLICY,
        **inventory,
    )
    rules = _alloy_prometheus_relabel_rules(rendered)

    assert rules[0] == {"action": "labeldrop", "regex": POLICY}
    assert rules[1:] == [
        {
            "source_labels": [],
            "regex": "(.*)",
            "replacement": expected["cluster"],
            "target_label": "cluster",
            "action": "replace",
        },
        {
            "source_labels": [],
            "regex": "(.*)",
            "replacement": expected["environment"],
            "target_label": "environment",
            "action": "replace",
        },
    ]

    discovery = rendered.split('discovery.relabel "metrics" {', 1)[1].split(
        'prometheus.scrape "local_containers"', 1
    )[0]
    assert 'action        = "keep"' in discovery
    assert 'regex         = "true"' in discovery
    assert 'action        = "drop"' in discovery
    assert 'target_label  = "service"' in discovery
    assert 'target_label  = "component"' in discovery
    assert 'target_label  = "target_id"' in discovery
    scrape = rendered.split('prometheus.scrape "local_containers" {', 1)[1].split(
        'prometheus.relabel "bounded_labels"', 1
    )[0]
    assert 'scrape_interval = "15s"' in scrape
    assert 'sample_limit    = encoding.from_json(sys.env("O11Y_SCRAPE_SAMPLE_LIMIT"))' in scrape
    assert 'label_limit     = encoding.from_json(sys.env("O11Y_SCRAPE_LABEL_LIMIT"))' in scrape
    assert 'label_name_length_limit = encoding.from_json(sys.env("O11Y_SCRAPE_LABEL_NAME_LENGTH_LIMIT"))' in scrape
    assert 'label_value_length_limit = encoding.from_json(sys.env("O11Y_SCRAPE_LABEL_VALUE_LENGTH_LIMIT"))' in scrape

    identities = {
        "__name__": "http_requests_total",
        "service": "checkout/api",
        "component": "api",
        "target_id": "service:checkout",
        "instance": "checkout-api:8080",
        "status": "200",
    }
    for cluster in (None, expected["cluster"], "app-selected-wrong-cluster"):
        for environment in (None, expected["environment"], "app-selected-prod"):
            sample = identities.copy()
            if cluster is not None:
                sample["cluster"] = cluster
            if environment is not None:
                sample["environment"] = environment
            sample["path"] = "/private/request/path"

            without_inventory_rules = _apply_alloy_prometheus_relabel_rules(sample, rules[:1])
            if cluster != expected["cluster"]:
                assert without_inventory_rules.get("cluster") != expected["cluster"]
            if environment != expected["environment"]:
                assert without_inventory_rules.get("environment") != expected["environment"]

            relabeled = _apply_alloy_prometheus_relabel_rules(sample, rules)

            assert relabeled["cluster"] == expected["cluster"]
            assert relabeled["environment"] == expected["environment"]
            assert {key: relabeled[key] for key in identities} == identities
            assert "path" not in relabeled


def test_all_prometheus_scrape_jobs_render_finite_limits_and_keep_identity_policy():
    base = _render_yaml(
        "prometheus.yml.j2",
        o11y_scrape_limits=LIMITS,
        o11y_scrape_limits_reviewed=True,
        o11y_scrape_sample_limit=2000,
        o11y_cluster="test-cluster",
        o11y_environment="prod",
        local_mode=False,
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )["scrape_configs"]
    assert {job["job_name"] for job in base} == {
        "prometheus", "receiver-host", "grafana", "loki", "alloy", "tempo", "pyroscope"
    }
    for job in base:
        _assert_bounded(job, 2000 if job["job_name"] == "receiver-host" else None)
        assert job["metric_relabel_configs"][0] == {"action": "labeldrop", "regex": POLICY}
        labels = job["static_configs"][0]["labels"]
        assert labels["cluster"] == "test-cluster"
        assert labels["environment"] == "prod"
        assert "service" in labels

    host = _render_yaml(
        "scrape-host-node.yml.j2",
        o11y_scrape_limits=LIMITS,
        o11y_scrape_limits_reviewed=True,
        o11y_scrape_sample_limit=2000,
        _ih_job="test-host-node",
        _ih_endpoints=[{"address": "192.0.2.10:9100", "service": "test", "host": "node-1"}],
        o11y_cluster="test-cluster",
        o11y_environment="prod",
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )["scrape_configs"][0]
    _assert_bounded(host, 2000)
    assert host["static_configs"][0]["labels"]["host"] == "node-1"

    dgx = _render_yaml(
        "scrape-dgx-spark.yml.j2",
        o11y_scrape_limits=LIMITS,
        o11y_scrape_limits_reviewed=True,
        dgx_spark_nodes=[{"name": "spark-1", "address": "192.0.2.11"}],
        dgx_spark_node_exporter_port=9100,
        dgx_spark_gpu_exporter_enabled=True,
        dgx_spark_gpu_exporter_port=9400,
        dgx_spark_head_address="192.0.2.11",
        dgx_spark_head_name="spark-1",
        dgx_spark_api_port=8000,
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )["scrape_configs"]
    assert [job["job_name"] for job in dgx] == ["dgx-spark-node", "dgx-spark-gpu", "dgx-spark-vllm"]
    for job in dgx:
        _assert_bounded(job)
        assert job["metric_relabel_configs"][0]["regex"] == POLICY
        assert job["static_configs"][0]["labels"]["node"] == "spark-1"

    gateway = _render_yaml(
        "scrape-agentgateway.yml.j2",
        o11y_scrape_limits=LIMITS,
        o11y_scrape_limits_reviewed=True,
        agentgateway_metrics_address="gateway.example.test",
        agentgateway_metrics_port=19002,
        o11y_cluster="test-cluster",
        o11y_environment="prod",
        local_mode=False,
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )["scrape_configs"][0]
    _assert_bounded(gateway)
    assert gateway["static_configs"][0]["targets"] == ["gateway.example.test:19002"]
    assert gateway["static_configs"][0]["labels"]["component"] == "gateway"


def test_production_render_refuses_missing_scrape_budget_inventory():
    with pytest.raises(ValueError, match="production scrape budgets must be declared"):
        _render_yaml(
            "prometheus.yml.j2",
            o11y_cluster="test-cluster",
            o11y_environment="prod",
            local_mode=False,
            _o11y_forbidden_metric_label_names_regex=POLICY,
        )

    with pytest.raises(ValueError, match="production scrape budgets must be declared"):
        _render_yaml(
            "prometheus.yml.j2",
            o11y_scrape_limits=LIMITS,
            o11y_scrape_limits_reviewed=False,
            o11y_cluster="test-cluster",
            o11y_environment="prod",
            local_mode=False,
            _o11y_forbidden_metric_label_names_regex=POLICY,
        )


def test_production_validator_accepts_documented_upper_bounds():
    maximums = {
        "body_size_limit_bytes": 167772160,
        "sample_limit": 100000,
        "label_limit": 640,
        "label_name_length_limit": 1280,
        "label_value_length_limit": 20480,
    }
    assert _production_validator_accepts(
        o11y_scrape_limits=maximums,
        o11y_scrape_sample_limit=20000,
    )


@pytest.mark.parametrize("field", LIMITS)
@pytest.mark.parametrize("invalid", ["10000", True, 0, -1])
def test_production_validator_refuses_noninteger_boolean_and_nonpositive_limits(field, invalid):
    limits = LIMITS.copy()
    limits[field] = invalid
    assert not _production_validator_accepts(o11y_scrape_limits=limits)


@pytest.mark.parametrize(
    ("field", "maximum"),
    [
        ("body_size_limit_bytes", 167772160),
        ("sample_limit", 100000),
        ("label_limit", 640),
        ("label_name_length_limit", 1280),
        ("label_value_length_limit", 20480),
    ],
)
def test_production_validator_refuses_excessive_limits(field, maximum):
    limits = LIMITS.copy()
    limits[field] = maximum + 1
    assert not _production_validator_accepts(o11y_scrape_limits=limits)


@pytest.mark.parametrize("field", LIMITS)
def test_production_validator_refuses_missing_limit_fields(field):
    limits = LIMITS.copy()
    del limits[field]
    assert not _production_validator_accepts(o11y_scrape_limits=limits)


def test_production_validator_refuses_missing_mapping_and_unreviewed_limits():
    assert not _production_validator_accepts(o11y_scrape_limits=None)
    assert not _production_validator_accepts(o11y_scrape_limits_reviewed=False)


@pytest.mark.parametrize("invalid", ["2000", True, 0, -1, 20001])
def test_production_validator_refuses_invalid_legacy_sample_ceiling(invalid):
    assert not _production_validator_accepts(o11y_scrape_sample_limit=invalid)


def test_local_prometheus_render_uses_finite_defaults():
    config = _render_yaml(
        "prometheus.yml.j2",
        local_mode=True,
        o11y_cluster="test-cluster",
        o11y_environment="local",
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )
    for job in config["scrape_configs"]:
        _assert_bounded(job, 2000 if job["job_name"] == "receiver-host" else None)

    env = _environment().from_string((DEPLOY / "templates/env.j2").read_text(encoding="utf-8"))
    rendered_env = env.render(
        local_mode=True,
        secrets={"grafana_admin_password": "test", "grafana_oidc_client_secret": "test"},
    )
    env_values = dict(line.split("=", 1) for line in rendered_env.splitlines() if "=" in line)
    assert env_values["O11Y_SCRAPE_SAMPLE_LIMIT"] == "2000"
    assert env_values["O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES"] == "16777216"
    assert env_values["O11Y_SCRAPE_LABEL_LIMIT"] == "64"
    assert env_values["O11Y_SCRAPE_LABEL_NAME_LENGTH_LIMIT"] == "128"
    assert env_values["O11Y_SCRAPE_LABEL_VALUE_LENGTH_LIMIT"] == "2048"

    host = _render_yaml(
        "scrape-host-node.yml.j2",
        local_mode=True,
        _ih_job="test-host-node",
        _ih_endpoints=[{"address": "127.0.0.1:9100", "service": "test", "host": "local"}],
        o11y_cluster="test-cluster",
        o11y_environment="local",
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )["scrape_configs"][0]
    _assert_bounded(host, 2000)

    dgx = _render_yaml(
        "scrape-dgx-spark.yml.j2",
        local_mode=True,
        dgx_spark_nodes=[{"name": "spark-1", "address": "127.0.0.1"}],
        dgx_spark_node_exporter_port=9100,
        dgx_spark_gpu_exporter_enabled=True,
        dgx_spark_gpu_exporter_port=9400,
        dgx_spark_head_address="127.0.0.1",
        dgx_spark_head_name="spark-1",
        dgx_spark_api_port=8000,
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )["scrape_configs"]
    assert len(dgx) == 3
    for job in dgx:
        _assert_bounded(job)

    gateway = _render_yaml(
        "scrape-agentgateway.yml.j2",
        local_mode=True,
        agentgateway_metrics_address="gateway.local",
        agentgateway_metrics_port=19002,
        o11y_cluster="test-cluster",
        o11y_environment="local",
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )["scrape_configs"][0]
    _assert_bounded(gateway)


def test_playbooks_run_the_shared_budget_gate_before_owned_actions():
    deploy = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text(encoding="utf-8"))
    deploy_phase = next(play for play in deploy if play.get("name") == "Phase 1: Place repo + manage o11y secrets")
    deploy_tasks = deploy_phase["tasks"]
    dns_origin_index = next(
        index for index, task in enumerate(deploy_tasks)
        if task.get("name", "").startswith("Require the production DNS zone and Caddy origin")
    )
    budget_index = next(
        index for index, task in enumerate(deploy_tasks)
        if task.get("name", "").startswith("Validate production scrape budgets")
    )
    placement_index = next(
        index for index, task in enumerate(deploy_tasks)
        if task.get("name") == "Place the monorepo + ensure podman/compose"
    )
    render_indexes = [
        index for index, task in enumerate(deploy_tasks)
        if task.get("name", "").startswith("Render ")
    ]
    assert dns_origin_index < budget_index < placement_index
    assert render_indexes and all(budget_index < index for index in render_indexes)

    instrument = yaml.safe_load((ROOT / "platform/playbooks/instrument-host-o11y.yml").read_text(encoding="utf-8"))
    validator_play = next(
        play for play in instrument if "Validate production scrape budgets" in play.get("name", "")
    )
    exporter_play = next(play for play in instrument if play.get("name", "").startswith("Run the host exporter"))
    assert validator_play["hosts"] == "o11y_svc"
    assert validator_play["tasks"][0]["ansible.builtin.include_tasks"] == "tasks/assert-o11y-scrape-limits.yml"
    assert instrument.index(validator_play) < instrument.index(exporter_play)


def test_alloy_local_scrape_uses_same_finite_bounds_and_existing_sample_ceiling():
    alloy = (DEPLOY / "templates/config.alloy.j2").read_text(encoding="utf-8")
    block = alloy.split('prometheus.scrape "local_containers" {', 1)[1].split("\n}", 1)[0]
    # Alloy's body_size_limit is a units string; a bare number fails the config load
    # (docs/MISTAKES.md 10.26). The bytes env gets the "B" unit appended.
    body_line = next(line for line in block.splitlines() if line.strip().startswith("body_size_limit"))
    assert body_line.strip() == 'body_size_limit = sys.env("O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES") + "B"'
    assert "encoding.from_json" not in body_line
    assert 'sample_limit    = encoding.from_json(sys.env("O11Y_SCRAPE_SAMPLE_LIMIT"))' in block
    assert 'label_limit     = encoding.from_json(sys.env("O11Y_SCRAPE_LABEL_LIMIT"))' in block
    assert 'label_name_length_limit = encoding.from_json(sys.env("O11Y_SCRAPE_LABEL_NAME_LENGTH_LIMIT"))' in block
    assert 'label_value_length_limit = encoding.from_json(sys.env("O11Y_SCRAPE_LABEL_VALUE_LENGTH_LIMIT"))' in block
    assert 'action = "labeldrop"' in alloy
    env = _environment().from_string((DEPLOY / "templates/env.j2").read_text(encoding="utf-8"))
    rendered = env.render(
        local_mode=False,
        o11y_scrape_limits=LIMITS,
        o11y_scrape_limits_reviewed=True,
        o11y_scrape_sample_limit=2000,
        secrets={"grafana_admin_password": "test", "grafana_oidc_client_secret": "test"},
    )
    values = dict(line.split("=", 1) for line in rendered.splitlines() if "=" in line)
    assert values["O11Y_SCRAPE_SAMPLE_LIMIT"] == "2000"
    assert values["O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES"] == str(LIMITS["body_size_limit_bytes"])
    assert values["O11Y_SCRAPE_LABEL_LIMIT"] == str(LIMITS["label_limit"])


def test_receiver_target_identity_is_scoped_and_survives_journal_otlp_loki():
    jobs = _render_yaml(
        "prometheus.yml.j2",
        o11y_scrape_limits=LIMITS,
        o11y_scrape_limits_reviewed=True,
        o11y_scrape_sample_limit=2000,
        o11y_cluster="test-cluster",
        o11y_environment="prod",
        local_mode=False,
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )["scrape_configs"]
    expected_services = {
        "prometheus": "o11y/prometheus",
        "receiver-host": "o11y/receiver-host",
        "grafana": "o11y/grafana",
        "loki": "o11y/loki",
        "alloy": "o11y/alloy",
        "tempo": "o11y/tempo",
        "pyroscope": "o11y/pyroscope",
    }
    assert {job["job_name"] for job in jobs} == set(expected_services)
    for job in jobs:
        labels = job["static_configs"][0]["labels"]
        assert labels["target_id"] == "service:o11y"
        assert labels["service"] == expected_services[job["job_name"]]

    alloy = (DEPLOY / "templates/config.alloy.j2").read_text(encoding="utf-8")
    project_rules = re.findall(
        r'source_labels = \["__meta_docker_container_label_com_docker_compose_project"\]'
        r'\s+regex\s*=\s*"([^"]+)"\s+replacement\s*=\s*"service:o11y"'
        r'\s+target_label\s*=\s*"target_id"',
        alloy,
    )
    assert project_rules == ["^o11y$", "^o11y$"]  # logs and discovered metrics only
    for project in ("o11y", "agentgateway", ""):
        assert bool(re.fullmatch(project_rules[0], project)) is (project == "o11y")

    journal = (DEPLOY / "templates/journal.alloy.j2").read_text(encoding="utf-8")
    assert 'matches    = "CONTAINER_NAME=o11y-alloy"' in journal
    assert 'labels     = { service = "o11y/alloy", signal = "container", target_id = "service:o11y" }' in journal
    assert 'logs = [otelcol.processor.attributes.journal_labels.input]' in journal
    assert 'key    = "loki.attribute.labels"' in journal
    assert 'action = "upsert"' in journal
    assert 'value  = "service,signal,target_id"' in journal
    assert journal.index('otelcol.receiver.loki "journal"') < journal.index(
        'otelcol.processor.attributes "journal_labels"'
    ) < journal.index('otelcol.exporter.otlp "journal"')
    gateway_logs = alloy.split('otelcol.processor.attributes "gateway_logs" {', 1)[1].split("\n}", 1)[0]
    assert 'key    = "loki.attribute.labels"' in gateway_logs
    assert 'action = "insert"' in gateway_logs
    assert 'value  = "service,signal"' in gateway_logs
    assert 'key    = "target_id"' not in gateway_logs
    assert 'value  = "service:o11y"' not in gateway_logs
