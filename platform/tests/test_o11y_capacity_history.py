"""Daily stored-byte sampling and the seven-day readiness gate fail closed."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "platform/services/o11y/deployment"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sampler = _load("o11y_capacity_sampler", DEPLOY / "capacity/collect-o11y-capacity.py")
survey = _load("o11y_capacity_survey", ROOT / "platform/playbooks/files/survey-o11y-seven-day-capacity.py")


def report(names=None):
    names = names or sorted(sampler.VOLUME_BACKENDS)
    return {
        "status": "observed",
        "volumes": [{"volume": name, "stored_bytes": 100 + index} for index, name in enumerate(names)],
    }


def test_sampler_publishes_only_a_complete_fixed_backend_set():
    text = sampler.metrics_from_report(report(), 1_800_000_000)
    assert text is not None
    assert "o11y_capacity_sample_timestamp_seconds 1800000000" in text
    assert text.count("o11y_backend_stored_bytes{") == 5
    assert 'backend="prometheus"' in text and 'backend="pyroscope"' in text
    assert sampler.metrics_from_report(report(["prometheus-data", "loki-data"]), 10) is None
    assert sampler.metrics_from_report({"status": "unavailable"}, 10) is None


def test_sampler_replaces_atomically_and_preserves_last_complete_sample_on_failure(tmp_path, monkeypatch, capsys):
    target = tmp_path / "textfile" / "o11y_capacity.prom"
    target.parent.mkdir()
    monkeypatch.setattr(sampler, "TEXTFILE", target)
    monkeypatch.setattr(sampler.time, "time", lambda: 1_800_000_000)
    monkeypatch.setattr(
        sampler.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(report())),
    )
    assert sampler.main() == 0
    good = target.read_text()
    assert target.stat().st_mode & 0o777 == 0o644
    assert list(target.parent.iterdir()) == [target]

    monkeypatch.setattr(
        sampler.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(report(["prometheus-data"]))),
    )
    assert sampler.main() == 1
    assert target.read_text() == good
    assert "o11y capacity sample unavailable" in capsys.readouterr().err
    assert list(target.parent.iterdir()) == [target]


def _matrix(metric, rows):
    return {"status": "success", "data": {"resultType": "matrix", "result": rows}}


def _history(now, samples=None):
    start = now - survey.RANGE_SECONDS
    evaluations = list(range(start, now + 1, survey.STEP_SECONDS))
    samples = samples or [now - survey.MIN_HISTORY_SECONDS + day * 86400 for day in range(8)]
    source_values = []
    for evaluation in evaluations:
        eligible = [sample for sample in samples if sample <= evaluation]
        if eligible:
            source_values.append([evaluation, str(max(eligible))])
    responses = {
        "source": _matrix("source", [{"metric": {}, "values": source_values}]),
        "backends": _matrix(
            "backends",
            [
                {
                    "metric": {"backend": backend},
                    "values": [[evaluation, str(100 + index)] for index, evaluation in enumerate(evaluations)],
                }
                for backend in sorted(survey.BACKENDS)
            ],
        ),
    }
    for key in ("cpu", "memory", "rootfs"):
        responses[key] = _matrix(
            key,
            [{"metric": {"job": "receiver-host"}, "values": [[t, "50"] for t in evaluations]}],
        )
    return responses


def test_history_requires_distinct_source_samples_across_seven_days():
    now = 1_800_000_000
    receipt = survey.assess(_history(now), now)
    assert receipt["status"] == "ready_for_separate_forecast_review"
    assert receipt["history"]["source_samples"] == 8
    assert receipt["history"]["source_span_seconds"] == survey.MIN_HISTORY_SECONDS
    assert receipt["history"]["evaluation_step_seconds"] == 300
    assert receipt["receiver_host_observations"]["cpu"]["points"] >= 2017
    assert receipt["receiver_host_observations"]["cpu"]["p95_method"] == "p95_of_evaluated_five_minute_means"
    assert set(receipt["backend_stored_bytes"]) == survey.BACKENDS


def test_history_excludes_carried_in_sample_from_window_evidence():
    now = 1_800_000_000
    samples = [now - survey.RANGE_SECONDS - 12 * 60 * 60 + day * 86400 for day in range(9)]

    receipt = survey.assess(_history(now, samples), now)

    assert receipt["status"] == "ready_for_separate_forecast_review"
    assert receipt["history"]["source_samples"] == 8
    assert receipt["history"]["source_span_seconds"] == 7 * 86400
    assert receipt["history"]["latest_sample_age_seconds"] == 12 * 60 * 60
    first_index = (samples[1] - (now - survey.RANGE_SECONDS)) // survey.STEP_SECONDS
    last_index = (samples[-1] - (now - survey.RANGE_SECONDS)) // survey.STEP_SECONDS
    assert all(
        backend["source_samples"] == 8
        and backend["first_bytes"] == 100 + first_index
        and backend["last_bytes"] == 100 + last_index
        for backend in receipt["backend_stored_bytes"].values()
    )


def test_history_rejects_carried_in_sample_plus_only_seven_in_window_samples():
    now = 1_800_000_000
    start = now - survey.RANGE_SECONDS
    samples = [start - 12 * 60 * 60, *[start + 12 * 60 * 60 + day * 86400 for day in range(7)]]

    with pytest.raises(survey.InsufficientHistory):
        survey.assess(_history(now, samples), now)


def test_history_rejects_a_stale_latest_capacity_sample():
    now = 1_800_000_000
    latest = now - survey.MAX_SAMPLE_AGE_SECONDS - 60
    samples = [latest - 7 * 86400 + day * 86400 for day in range(8)]

    with pytest.raises(survey.InsufficientHistory):
        survey.assess(_history(now, samples), now)


def test_history_rejects_a_repeated_single_point_snapshot():
    now = 1_800_000_000
    responses = _history(now)
    start = now - survey.RANGE_SECONDS
    repeated = str(now - 60)
    responses["source"] = _matrix(
        "source", [{"metric": {}, "values": [[t, repeated] for t in range(start, now + 1, survey.STEP_SECONDS)]}]
    )
    with pytest.raises(survey.InsufficientHistory):
        survey.assess(responses, now)


def test_history_query_resolution_and_response_time_are_bounded(monkeypatch):
    assert survey.STEP_SECONDS == 300
    assert survey.RANGE_SECONDS // survey.STEP_SECONDS + 1 == 2305
    assert survey.MAX_RESPONSE_BYTES == 2_000_000
    assert survey.CURL_TIMEOUT_SECONDS == 15
    assert survey.QUERY_TIMEOUT_SECONDS == 20
    assert survey.MIN_METRIC_POINTS == 2017
    assert survey.MAX_METRIC_GAP_SECONDS == 600

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"status":"success"}')

    monkeypatch.setattr(survey.subprocess, "run", fake_run)
    survey.query("backends", 1, 2)
    args, kwargs = calls[0]
    assert "step=300" in args
    assert "--max-time" in args and "15" in args
    assert kwargs["timeout"] == 20

    monkeypatch.setattr(
        survey.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="x" * (survey.MAX_RESPONSE_BYTES + 1)
        ),
    )
    with pytest.raises(survey.InsufficientHistory):
        survey.query("backends", 1, 2)
    assert survey.MAX_METRIC_GAP_SECONDS == 600


def test_deploy_gates_capacity_collector_and_units_for_local_mode():
    plays = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text())
    p1 = next(play for play in plays if str(play.get("name", "")).startswith("Phase 1"))
    p2 = next(play for play in plays if str(play.get("name", "")).startswith("Phase 2"))
    overlay = next(task for task in p2["tasks"] if task.get("name") == "Run deploy.sh (container lifecycle)")
    jinja = Environment()
    jinja.filters["bool"] = bool
    template = jinja.from_string(overlay["environment"]["COMPOSE_OVERLAYS"])
    assert template.render(local_mode=False, o11y_inference_probe_enabled=False) == "capacity/compose.textfile.yml"
    assert template.render(local_mode=True, o11y_inference_probe_enabled=False) == ""
    assert template.render(local_mode=True, o11y_inference_probe_enabled=True) == "capacity/compose.textfile.yml"
    probe_artefacts = p1["vars"]["_probe_host_artefacts"]
    assert all("o11y_capacity" not in path for path in probe_artefacts)
    install = next(
        task for task in p1["tasks"]
        if task.get("name") == "Install the read-only o11y capacity sampler"
    )
    assert install["when"] == "not (local_mode | default(false) | bool)"
    sample = next(
        task for task in p2["tasks"]
        if task.get("name") == "Take a complete capacity sample after the stack is available"
    )
    assert sample["when"] == [
        "not ansible_check_mode",
        "not (local_mode | default(false) | bool)",
    ]

    compose = yaml.safe_load((DEPLOY / "capacity/compose.textfile.yml").read_text())
    command = compose["services"]["node-exporter"]["command"]
    assert "--collector.textfile" in command
    assert "--collector.textfile.directory=/host/var/lib/node_exporter/textfile" in command
    base = yaml.safe_load((DEPLOY / "compose.yml").read_text())["services"]["node-exporter"]
    assert "/:/host:ro,rslave" in base["volumes"]
    assert "ports" not in base


def test_history_survey_is_read_only_exact_revision_and_uses_only_fixed_series():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/survey-o11y-seven-day-capacity.yml").read_text())
    survey_play = playbook[-1]
    task = next(task for task in survey_play["tasks"] if task.get("name") == "Read bounded Prometheus capacity history")
    assert task["changed_when"] is False and task["check_mode"] is False
    assert task["ansible.builtin.script"]["cmd"].endswith("survey-o11y-seven-day-capacity.py")
    source = (ROOT / "platform/playbooks/files/survey-o11y-seven-day-capacity.py").read_text()
    assert "mountpoint=\"/\"" in source
    assert "backend=~\"^(prometheus|loki|tempo|grafana|pyroscope)$\"" in source
    assert "ready_for_separate_forecast_review" in source
    assert "growth" not in source.lower() and "forecast" in source.lower()
    assert "node_cpu_seconds_total" in source and "[5m]" in source


def test_daily_units_are_bounded_and_the_history_survey_has_a_registered_template():
    unit = Environment(undefined=StrictUndefined).from_string(
        (DEPLOY / "capacity/o11y-capacity-sample.service.j2").read_text()
    ).render(_capacity_user="deploy", _capacity_sampler="/usr/local/libexec/o11y/collect-o11y-capacity.py",
             _capacity_textfile_dir="/var/lib/node_exporter/textfile")
    assert "User=deploy" in unit
    assert "Environment=XDG_RUNTIME_DIR=/run/user/%U" in unit
    assert "NoNewPrivileges=no" in unit
    assert "PrivateTmp=yes" in unit
    assert "ProtectSystem=full" in unit
    assert "ReadWritePaths=/var/lib/node_exporter/textfile" in unit
    timer = (DEPLOY / "capacity/o11y-capacity-sample.timer").read_text()
    assert "OnCalendar=*-*-* 02:15:00" in timer and "Persistent=true" in timer

    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    survey_template = next(item for item in templates if item["name"] == "Survey o11y Seven-Day Capacity History (Dev)")
    assert survey_template["playbook"] == "platform/playbooks/survey-o11y-seven-day-capacity.yml"
    assert {item["name"] for item in survey_template["survey_vars"]} == {
        "expected_repository_sha", "expected_receiver_sha"
    }
