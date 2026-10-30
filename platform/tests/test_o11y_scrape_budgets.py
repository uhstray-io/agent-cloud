import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible
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
# Deterministic synthetic host from the canonical RFC1918 test network; this is
# not derived from any site inventory or deployment topology.
PRIVATE_TEST_IP = str(ipaddress.ip_network("192.168.0.0/16")[20])
FILTER_PATH = ROOT / "platform/playbooks/filter_plugins/o11y_scrape_budget.py"
FILTER_SPEC = importlib.util.spec_from_file_location("o11y_scrape_budget", FILTER_PATH)
assert FILTER_SPEC and FILTER_SPEC.loader
BUDGET = importlib.util.module_from_spec(FILTER_SPEC)
FILTER_SPEC.loader.exec_module(BUDGET)
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


def _render_alloy(**values):
    return _environment().from_string(
        (DEPLOY / "templates/config.alloy.j2").read_text(encoding="utf-8")
    ).render(**values)


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


def test_availability_source_renders_bounded_native_components_and_preserves_self_monitor():
    prepared = {
        "enabled": True,
        "limits": {"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
        "targets": [{
            "module_name": "health_01", "target_id": "service:alpha",
            "service_identity": "alpha", "environment": "production", "cluster": "prod-cluster",
            "endpoint": f"https://{PRIVATE_TEST_IP}/healthz", "expected_status_codes": [200],
            "tls_server_name": "health.example.invalid", "trust": "system",
            "interval_seconds": 60, "scrape_timeout_seconds": 20,
            "probe_timeout_seconds": 10, "body_size_limit_bytes": 4096,
            "samples_per_scrape": 64,
            "blackbox_config": json.dumps({"modules": {"health_01": {
                "prober": "http", "timeout": "10s", "http": {
                    "method": "GET", "valid_status_codes": [200],
                    "follow_redirects": False, "proxy_from_environment": False,
                    "body_size_limit": "4096B",
                    "tls_config": {"insecure_skip_verify": False, "server_name": "health.example.invalid"},
                },
            }}}, sort_keys=True, separators=(",", ":")),
        }],
    }
    rendered = _render_alloy(
        local_mode=False, _o11y_availability=prepared,
        o11y_cluster="prod-cluster", o11y_environment="production",
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )
    assert 'prometheus.exporter.blackbox "health_01"' in rendered
    assert 'job_name = "o11y-availability"' in rendered
    assert 'instance = "service:alpha"' in rendered
    assert 'labels = {\n      service = "alpha",' in rendered
    assert '"service": "alpha"' not in rendered
    assert 'probe_http_status_code|scrape_samples_scraped|scrape_body_size_bytes' in rendered
    assert "proxy_from_environment" in rendered and "insecure_skip_verify" in rendered
    assert 'prometheus.scrape "alloy_self"' in rendered
    assert '"__address__" = "127.0.0.1:12345"' in rendered
    assert '"instance" = "alloy:12345"' in rendered
    assert '\\"body_size_limit\\":\\"4096B\\"' in rendered  # blackbox health-response cap
    availability_scrape = rendered.split('prometheus.scrape "health_01" {', 1)[1].split("\n}", 1)[0]
    self_scrape = rendered.split('prometheus.scrape "alloy_self" {', 1)[1].split("\n}", 1)[0]
    assert 'body_size_limit = sys.env("O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES") + "B"' in availability_scrape
    assert 'body_size_limit = "4096B"' not in availability_scrape
    assert 'body_size_limit = sys.env("O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES") + "B"' in self_scrape
    for field, env_name in (
        ("label_limit", "O11Y_SCRAPE_LABEL_LIMIT"),
        ("label_name_length_limit", "O11Y_SCRAPE_LABEL_NAME_LENGTH_LIMIT"),
        ("label_value_length_limit", "O11Y_SCRAPE_LABEL_VALUE_LENGTH_LIMIT"),
    ):
        expression = f'{field} = encoding.from_json(sys.env("{env_name}"))'
        assert expression in availability_scrape
        assert expression in self_scrape
    assert 'sample_limit = 64' in availability_scrape
    assert 'sample_limit = encoding.from_json(sys.env("O11Y_SCRAPE_SAMPLE_LIMIT"))' not in availability_scrape
    assert 'sample_limit = encoding.from_json(sys.env("O11Y_SCRAPE_SAMPLE_LIMIT"))' in self_scrape
    assert "extra_metrics = true" in availability_scrape
    assert "extra_metrics = true" in self_scrape
    assert 'prometheus.remote_write "local"' in rendered
    assert 'target_label  = "environment"' in rendered


def test_availability_off_leaves_alloy_probe_and_self_scrape_absent():
    rendered = _render_alloy(
        local_mode=False, o11y_cluster="prod-cluster", o11y_environment="production",
        _o11y_forbidden_metric_label_names_regex=POLICY,
    )
    assert "prometheus.exporter.blackbox" not in rendered
    assert 'prometheus.scrape "alloy_self"' not in rendered
    assert 'prometheus.remote_write "local"' not in rendered


def test_availability_source_budget_consumer_keeps_prometheus_body_unavailable_and_native_positive():
    prometheus = {
        "status": "observed", "source": "prometheus", "intended_source": "prometheus",
        "matches_intended": True,
        "evidence": {
            "status": "observed", "source": "prometheus",
            "alloy_self": {"up": 1, "scrape_samples_scraped": 415},
            "scrape_body_size_bytes": {
                "status": "unavailable", "reason": "legacy_extra_scrape_metrics_not_enabled",
            },
        },
    }
    assert BUDGET.o11y_availability_source_coverage(prometheus) == {
        "status": "observed", "source": "prometheus", "matches_intended": True,
        "sample_count": 1, "probe_failed_count": 0, "scrape_failed_count": 0,
        "body_counter": "not_exposed_by_legacy_prometheus",
    }
    prom_native_absence = json.loads(json.dumps(prometheus))
    prom_native_absence["evidence"]["native_components"] = {
        "status": "observed", "reason": "native_components_absent", "component_count": 3,
    }
    assert BUDGET.o11y_availability_source_coverage(prom_native_absence) == {
        "status": "observed", "source": "prometheus", "matches_intended": True,
        "sample_count": 1, "probe_failed_count": 0, "scrape_failed_count": 0,
        "body_counter": "not_exposed_by_legacy_prometheus", "native_components": "absent",
    }
    prom_native_absence["evidence"]["native_components"]["reason"] = "component_status_unavailable"
    assert BUDGET.o11y_availability_source_coverage(prom_native_absence)["status"] == "unavailable"
    malformed_prometheus = json.loads(json.dumps(prometheus))
    malformed_prometheus["evidence"]["scrape_body_size_bytes"] = {"status": "observed", "value": 0}
    assert BUDGET.o11y_availability_source_coverage(malformed_prometheus)["status"] == "unavailable"

    native_intended_but_prom_current = json.loads(json.dumps(prometheus))
    native_intended_but_prom_current.update(intended_source="alloy", matches_intended=False)
    projected = BUDGET.o11y_availability_source_coverage(native_intended_but_prom_current)
    assert projected["status"] == "observed" and projected["source"] == "prometheus"
    assert projected["matches_intended"] is False
    missing_native = {
        "status": "unavailable", "reason": "native_source_unavailable", "source": "unknown",
        "intended_source": "unknown", "matches_intended": False,
    }
    assert BUDGET.o11y_availability_source_coverage(missing_native) == {
        "status": "unavailable", "reason": "native_source_unavailable", "source": "unknown",
    }

    limits = {
        "body_size_limit": 16_777_216, "sample_limit": 64, "label_limit": 64,
        "label_name_length_limit": 128, "label_value_length_limit": 2048,
    }
    native = {
        "status": "observed", "source": "alloy", "intended_source": "alloy",
        "matches_intended": True,
        "evidence": {
            "status": "observed", "source": "alloy",
            "detail": {
                "status": "observed", "reason": "fixed_native_components_loaded", "target_count": 1,
                "targets": [{
                    "target_id": "service:api",
                    "status": "observed",
                    "probe_health": "down",
                    "limits": limits,
                }],
                "alloy_self": {"status": "observed", "limits": limits},
            },
            "samples": {
                "status": "observed", "reason": "fresh_native_samples", "freshness_seconds": 300,
                "alloy_self": {"status": "observed", "scrape_up": 1,
                                "scrape_samples_scraped": 415, "scrape_body_size_bytes": 73_675},
                "targets": [{
                    "target_id": "service:api", "status": "observed", "scrape_up": 1,
                    "probe_success": 0, "probe_http_status_code": 503, "probe_duration_seconds": "0.25",
                    "scrape_samples_scraped": 17, "scrape_body_size_bytes": 2034,
                }],
            },
        },
    }
    assert BUDGET.o11y_availability_source_coverage(native) == {
        "status": "observed", "source": "alloy", "matches_intended": True,
        "sample_count": 1, "probe_failed_count": 1, "scrape_failed_count": 0,
        "body_counter": "observed_positive",
    }


def test_availability_source_gate_diagnostic_accepts_only_bounded_valid_helper_results():
    observed = {
        "status": "observed", "source": "prometheus", "intended_source": "prometheus",
        "matches_intended": True,
        "evidence": {
            "status": "observed", "source": "prometheus",
            "alloy_self": {"up": 1, "scrape_samples_scraped": 415},
            "scrape_body_size_bytes": {
                "status": "unavailable", "reason": "legacy_extra_scrape_metrics_not_enabled",
            },
        },
    }
    result = {"rc": 0, "stdout": json.dumps(observed), "stderr": ""}
    assert BUDGET.o11y_availability_source_gate_diagnostic(result) == "producer_observed"

    known_failure = {
        "status": "unavailable", "reason": "native_source_stale", "source": "unknown",
        "intended_source": "unknown", "matches_intended": False,
    }
    assert BUDGET.o11y_availability_source_gate_diagnostic(
        {**result, "stdout": json.dumps(known_failure)}
    ) == "native_source_stale"

    sentinel = "SECRET-SENTINEL-MUST-NOT-ESCAPE"
    invalid_results = [
        {**result, "rc": 1}, {**result, "rc": True}, {**result, "rc": "0"},
        {**result, "stderr": sentinel}, {**result, "stdout": sentinel},
        {**result, "stdout": "{" + (" " * 65536)},
        {**result, "stdout": '{"status":"unavailable","status":"observed"}'},
        {**result, "stdout": json.dumps({**known_failure, "reason": sentinel})},
        {**result, "stdout": json.dumps({**observed, "untrusted": sentinel})},
        {"stdout": json.dumps(observed), "stderr": ""},
    ]
    for invalid in invalid_results:
        category = BUDGET.o11y_availability_source_gate_diagnostic(invalid)
        assert category == "source_readback_unavailable"
        assert sentinel not in category


def test_availability_source_diagnostic_precedes_unchanged_protected_gate():
    tasks = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/prepare-o11y-availability.yml").read_text(encoding="utf-8")
    )
    diagnostic_set = next(
        task for task in tasks if task.get("name") == "Validate the fixed current source diagnostic category"
    )
    visible = next(
        task for task in tasks if task.get("name") == "Report only the fixed current source diagnostic category"
    )
    gate = next(
        task for task in tasks if task.get("name") == "Require one fresh and unambiguous current self-metrics producer"
    )
    assert tasks.index(diagnostic_set) < tasks.index(visible) < tasks.index(gate)
    assert diagnostic_set["no_log"] is True
    assert "_o11y_current_source_raw | o11y_availability_source_gate_diagnostic" in diagnostic_set[
        "ansible.builtin.set_fact"
    ]["_o11y_current_source_diagnostic"]
    assert visible["ansible.builtin.debug"]["msg"] == (
        "Current self-metrics producer diagnostic: {{ _o11y_current_source_diagnostic }}"
    )
    assert visible["when"][-1] == "_o11y_current_source_diagnostic != 'producer_observed'"
    assert gate["no_log"] is True
    assert gate["ansible.builtin.assert"]["that"] == [
        "_o11y_current_source_raw.rc == 0",
        "(_o11y_current_source_raw.stdout | from_json).status == 'observed'",
        "(_o11y_current_source_raw.stdout | from_json).source in ['prometheus', 'alloy']",
        "(_o11y_current_source_raw.stdout | from_json).matches_intended is boolean",
    ]


@needs_ansible
def test_availability_source_diagnostic_ansible_verbose_fixture_is_closed(tmp_path):
    sentinel = "SOURCE-DIAGNOSTIC-SECRET-SENTINEL"
    playbook = tmp_path / "diagnostic.yml"
    playbook.write_text(
        """---
- hosts: localhost
  gather_facts: false
  tasks:
    - name: Produce an untrusted failing helper result
      ansible.builtin.shell: >-
        printf '%s' SOURCE-DIAGNOSTIC-SECRET-SENTINEL;
        printf '%s' SOURCE-DIAGNOSTIC-SECRET-SENTINEL >&2;
        exit 1
      register: _o11y_current_source_raw
      changed_when: false
      failed_when: false
      no_log: true
    - name: Reduce the protected result
      ansible.builtin.set_fact:
        _o11y_current_source_diagnostic: "{{ _o11y_current_source_raw | o11y_availability_source_gate_diagnostic }}"
      no_log: true
    - name: Emit fixed category
      ansible.builtin.debug:
        msg: "Current self-metrics producer diagnostic: {{ _o11y_current_source_diagnostic }}"
    - name: Preserve strict refusal
      ansible.builtin.assert:
        that: _o11y_current_source_raw.rc == 0
        fail_msg: strict source gate refused
      no_log: true
""",
        encoding="utf-8",
    )
    collections = tmp_path / "collections"
    collections.mkdir()
    environment = os.environ.copy()
    environment["ANSIBLE_FILTER_PLUGINS"] = str(ROOT / "platform/playbooks/filter_plugins")
    environment["ANSIBLE_COLLECTIONS_PATH"] = str(collections)
    done = subprocess.run(
        [shutil.which("ansible-playbook"), "-vv", "-i", "localhost,", "-c", "local", str(playbook)],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60, check=False,
    )
    output = done.stdout + done.stderr
    assert done.returncode != 0
    assert "Current self-metrics producer diagnostic: source_readback_unavailable" in output, output
    assert "strict source gate refused" not in output
    assert sentinel not in output


def test_prometheus_alloy_job_is_omitted_only_for_availability_mode():
    base = {
        "local_mode": False, "o11y_environment": "production",
        "o11y_scrape_limits_reviewed": True, "o11y_scrape_limits": LIMITS.copy(),
        "_o11y_forbidden_metric_label_names_regex": POLICY,
    }
    normal = _render_yaml("prometheus.yml.j2", **base)
    enabled = _render_yaml(
        "prometheus.yml.j2", **base,
        o11y_availability_probes_enabled=True,
    )
    normal_jobs = {job["job_name"] for job in normal["scrape_configs"]}
    enabled_jobs = {job["job_name"] for job in enabled["scrape_configs"]}
    assert "alloy" in normal_jobs
    assert "alloy" not in enabled_jobs


def test_availability_compose_overlays_bind_only_loopback_and_deduplicate_ca_mount():
    deploy = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text(encoding="utf-8"))
    phase = next(play for play in deploy if play.get("name") == "Phase 2: Start o11y")
    lifecycle_block = next(task for task in phase["tasks"] if "block" in task)
    deploy_task = next(
        task for task in lifecycle_block["block"]
        if task.get("name") == "Run deploy.sh (container lifecycle)"
    )
    overlays = deploy_task["environment"]["COMPOSE_OVERLAYS"]
    env = Environment(undefined=StrictUndefined)
    env.filters["bool"] = bool
    targets = [{"trust": "internal-ca"}]
    base = {
        "local_mode": False, "o11y_otlp_tls_enabled": False,
        "o11y_inference_probe_enabled": False,
        "_o11y_availability": {"enabled": True, "targets": targets},
    }
    rendered = env.from_string(overlays).render(**base)
    assert rendered == "capacity/compose.textfile.yml compose.availability.yml compose.availability-ca.yml"
    combined_tls = env.from_string(overlays).render(**{**base, "o11y_otlp_tls_enabled": True})
    assert combined_tls == "capacity/compose.textfile.yml compose.otlp-tls.yml compose.availability.yml"

    availability_overlay = yaml.safe_load(
        (DEPLOY / "compose.availability.yml").read_text(encoding="utf-8")
    )
    availability = availability_overlay["services"]["alloy"]
    assert availability["command"][1] == "--server.http.listen-addr=127.0.0.1:12345"
    assert availability_overlay["x-podman"] == {"in_pod": False}
    assert "ports" not in availability
    ca_overlay = yaml.safe_load((DEPLOY / "compose.availability-ca.yml").read_text(encoding="utf-8"))
    ca = ca_overlay["services"]["alloy"]["volumes"]
    assert ca == [{
        "type": "bind", "source": "./certs/step-ca-bundle.crt",
        "target": "/certs/step-ca-bundle.crt", "read_only": True,
        "bind": {"create_host_path": False},
    }]


def test_deploy_availability_gates_current_and_postapply_source_before_reporting():
    plays = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text(encoding="utf-8"))
    phase = next(play for play in plays if play.get("name") == "Phase 1: Place repo + manage o11y secrets")
    tasks = phase["tasks"]
    prepare = next(
        index for index, task in enumerate(tasks)
        if task.get("name") == "Prepare the bounded private availability source projection"
    )
    placement = next(
        index for index, task in enumerate(tasks)
        if task.get("name") == "Place the monorepo + ensure podman/compose"
    )
    assert prepare < placement
    validator_path = ROOT / "platform/playbooks/tasks/prepare-o11y-availability.yml"
    validator = yaml.safe_load(validator_path.read_text(encoding="utf-8"))
    namespace = next(
        task for task in validator
        if task.get("name") == "Prove current Alloy namespace isolation before placement when probes are enabled"
    )
    assert namespace["ansible.builtin.script"]["cmd"].endswith("--verify-availability-namespace")
    assert namespace["no_log"] is True and namespace["when"] == "_o11y_availability.enabled | bool"
    namespace_result = next(
        task for task in validator
        if task.get("name") == "Require actual current Alloy network namespace isolation before placement"
    )
    assert namespace_result["ansible.builtin.assert"]["that"][0] == "_o11y_namespace_preplacement.rc == 0"
    assert not any(
        "Refuse availability deployment until runtime isolation" in task.get("name", "")
        for task in tasks
    )
    command = next(
        task for task in validator
        if task.get("name") == "Validate and privately prepare the reviewed availability projection"
    )
    assert command["no_log"] is True and command["failed_when"] is False
    assert command["delegate_to"] == "localhost"
    projection = next(
        task for task in validator
        if task.get("name") == "Store the protected availability projection for the renderer"
    )
    assert projection["no_log"] is True
    reviewed_source = next(
        task for task in validator
        if task.get("name") == "Prove the current Alloy self-metrics producer before a reviewed source transition"
    )
    assert reviewed_source["no_log"] is True
    assert "ansible.builtin.script" in reviewed_source
    assert "O11Y_AVAILABILITY_SOURCE_JSON" in reviewed_source["environment"]
    assert reviewed_source["environment"]["O11Y_DEPLOYMENT_DIR"] == "{{ _deploy_dir }}"
    assert reviewed_source["environment"]["COMPOSE_CMD"] == "podman-compose"
    assert "equalto', 'alloy:12345'" in reviewed_source["when"]
    phase_two = next(play for play in plays if play.get("name") == "Phase 2: Start o11y")
    lifecycle_block = next(task for task in phase_two["tasks"] if "block" in task)
    lifecycle = next(
        task for task in lifecycle_block["block"]
        if task.get("name") == "Run deploy.sh (container lifecycle)"
    )
    assert "namespace_snapshot_sha256" in lifecycle["environment"][
        "O11Y_EXPECTED_NAMESPACE_SNAPSHOT_SHA256"
    ]
    assert "runtime_context_sha256" in lifecycle["environment"][
        "O11Y_EXPECTED_RUNTIME_CONTEXT_SHA256"
    ]
    source_file = next(
        task for task in lifecycle_block["block"]
        if task.get("name") == "Write the protected source-review declaration"
    )
    assert source_file["no_log"] is True and source_file["ansible.builtin.copy"]["mode"] == "0600"
    postapply = next(
        task for task in next(play for play in plays if play.get("name") == "Phase 3: Verify o11y")["tasks"]
        if task.get("name") == "Read back the current Alloy self-metrics producer after lifecycle apply"
    )
    assert postapply["no_log"] is True
    assert "require_health_targets" in postapply["ansible.builtin.command"]["stdin"]
    classified = next(
        task for task in next(play for play in plays if play.get("name") == "Phase 3: Verify o11y")["tasks"]
        if task.get("name") == "Classify the private post-apply producer response"
    )
    assert "rescue" in classified
    assert all(step.get("no_log") is True for step in classified["block"] + classified["rescue"])
    report = next(
        task for task in next(play for play in plays if play.get("name") == "Phase 3: Verify o11y")["tasks"]
        if task.get("name") == "Report fixed post-apply producer status"
    )
    assert "no_log" not in report
    assert "_o11y_source_postapply_raw" not in str(report)
    deploy_script = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    namespace_checks = [
        offset for offset in range(len(deploy_script))
        if deploy_script.startswith("--verify-availability-namespace --candidate", offset)
    ]
    apply = deploy_script.index("compose_up_if_changed config")
    staged_hook = deploy_script.index("compose_staged_up() {")
    final_apply = deploy_script.index("compose up -d --force-recreate --remove-orphans", staged_hook)
    assert len(namespace_checks) == 3
    assert namespace_checks[0] < staged_hook < namespace_checks[1] < final_apply
    assert final_apply < apply < namespace_checks[2]
    source_check = deploy_script.index("--verify-availability-source")
    boundary = deploy_script.index("O11Y_AVAILABILITY_PREAPPLY_EPOCH=")
    assert namespace_checks[0] < source_check < boundary < apply
    assert 'O11Y_DEPLOYMENT_DIR="$SCRIPT_DIR"' in deploy_script[
        source_check - 180 : source_check + 120
    ]


def test_default_off_source_transition_initializes_staged_helper_before_branch():
    script = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    start = script.index("step_start() {")
    end = script.index("\nstep_wait_healthy()", start)
    body = script[start:end]
    declaration = body.index(
        'local availability_helper="${SCRIPT_DIR}/../../../playbooks/files/summarize-o11y-scrape-baseline.py"'
    )
    enable_branch = body.index('if [ "${O11Y_AVAILABILITY_ENABLED:-false}" = "true" ]; then')
    source_branch = body.index('if [ -n "${O11Y_AVAILABILITY_SOURCE_FILE:-}" ]; then')
    staged_hook = body.index("compose_staged_up() {")
    assert declaration < enable_branch < source_branch < staged_hook
    assert 'python3 "$availability_helper" --verify-availability-source' in body
    assert "availability_source_helper" not in body


def test_candidate_hash_is_from_same_private_controller_renderer_not_installed_bytes():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text(encoding="utf-8"))
    phase = next(play for play in playbook if play.get("name") == "Phase 1: Place repo + manage o11y secrets")
    tasks = phase["tasks"]
    candidate = next(
        task for task in tasks
        if task.get("name") == "Derive the reviewed Alloy candidate digest from the committed renderer"
    )
    render = next(task for task in candidate["block"] if "ansible.builtin.template" in task)
    stat = next(task for task in candidate["block"] if "ansible.builtin.stat" in task)
    renderer = next(
        task for task in tasks
        if task.get("name") == "Render Alloy config with the inventory cluster label"
    )
    assert render["delegate_to"] == stat["delegate_to"] == "localhost"
    assert render["ansible.builtin.template"]["src"] == renderer["ansible.builtin.template"]["src"]
    assert stat["ansible.builtin.stat"]["path"] == "{{ _o11y_candidate_tmp.path }}/config.alloy"
    assert candidate["always"][0]["delegate_to"] == "localhost"
    assert candidate["always"][0]["ansible.builtin.file"]["state"] == "absent"
    assert not any(
        task.get("ansible.builtin.stat", {}).get("path") == "{{ _deploy_dir }}/config/config.alloy"
        for task in tasks
    )


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
