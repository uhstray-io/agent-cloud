import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

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
    protected = [task for task in receiver["tasks"] if task.get("no_log")]
    assert len(protected) == 3
    invocation = next(
        task for task in protected if task["name"] == "Collect and sanitize the active scrape baseline on the receiver"
    )
    parse = next(task for task in protected if task["name"] == "Parse the helper's bounded public projection")
    assert "delegate_to" not in invocation
    assert "ansible.builtin.script" in invocation
    assert parse["no_log"] is True
    visible_assert = next(
        task for task in receiver["tasks"] if task["name"] == "Require a complete and bounded baseline receipt"
    )
    assert "_baseline_run" not in str(visible_assert["ansible.builtin.assert"])
    assert visible_assert["ansible.builtin.assert"]["fail_msg"] == (
        "The scrape baseline is unavailable, unhealthy, ambiguous, or outside the bounded public receipt schema."
    )
    assert all(task.get("no_log") is not True for task in receiver["tasks"] if task not in protected)
    check_mode = next(task for task in receiver["tasks"] if task.get("name") == "Mark check-mode results as unverified")
    assert check_mode["when"] == "ansible_check_mode"
    assert "scrape_body_size_bytes" not in HELPER.read_text()
