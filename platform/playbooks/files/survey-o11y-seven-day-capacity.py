#!/usr/bin/env python3
"""Read bounded Prometheus history and report readiness for separate forecast review."""

import json
import math
import statistics
import subprocess
import sys
import time

ENGINE = "podman"
CONTAINER = "o11y-grafana"
API = "http://prometheus:9090/api/v1/query_range"
RANGE_SECONDS = 8 * 24 * 60 * 60
STEP_SECONDS = 5 * 60
MIN_HISTORY_SECONDS = 7 * 24 * 60 * 60
MAX_SAMPLE_AGE_SECONDS = 26 * 60 * 60
MAX_SOURCE_GAP_SECONDS = 36 * 60 * 60
MAX_METRIC_GAP_SECONDS = 2 * STEP_SECONDS
MIN_METRIC_POINTS = MIN_HISTORY_SECONDS // STEP_SECONDS + 1
MAX_RESPONSE_BYTES = 2_000_000
CURL_TIMEOUT_SECONDS = 15
QUERY_TIMEOUT_SECONDS = 20
BACKENDS = {"prometheus", "loki", "tempo", "grafana", "pyroscope"}
FAILURE_REASONS = {
    "source_series_ambiguous",
    "source_history_too_short",
    "malformed_or_incomplete_history",
    "backend_metrics_unavailable",
    "host_metric_history_unavailable",
    "transport_unavailable",
}
QUERIES = {
    "source": (
        'o11y_capacity_sample_timestamp_seconds'
        '{job="receiver-host",service="o11y/receiver-host"}'
    ),
    "backends": (
        'o11y_backend_stored_bytes'
        '{job="receiver-host",service="o11y/receiver-host",backend=~"^(prometheus|loki|tempo|grafana|pyroscope)$"}'
    ),
    "cpu": (
        '100 * (1 - avg by (job,instance) '
        '(rate(node_cpu_seconds_total{job="receiver-host",service="o11y/receiver-host",mode="idle"}[5m])))'
    ),
    "memory": (
        '100 * node_memory_MemAvailable_bytes{job="receiver-host",service="o11y/receiver-host"} '
        '/ node_memory_MemTotal_bytes{job="receiver-host",service="o11y/receiver-host"}'
    ),
    "rootfs": (
        '100 * node_filesystem_avail_bytes'
        '{job="receiver-host",service="o11y/receiver-host",mountpoint="/"} '
        '/ node_filesystem_size_bytes'
        '{job="receiver-host",service="o11y/receiver-host",mountpoint="/"}'
    ),
}


class InsufficientHistory(Exception):
    def __init__(self, reason: str = "malformed_or_incomplete_history") -> None:
        self.reason = reason if reason in FAILURE_REASONS else "malformed_or_incomplete_history"


def require(condition: bool, reason: str = "malformed_or_incomplete_history") -> None:
    if not condition:
        raise InsufficientHistory(reason)


def parse_matrix(body: object, reason: str = "malformed_or_incomplete_history") -> list[dict]:
    require(isinstance(body, dict) and body.get("status") == "success", reason)
    data = body.get("data")
    require(isinstance(data, dict) and data.get("resultType") == "matrix", reason)
    result = data.get("result")
    require(isinstance(result, list) and all(isinstance(row, dict) for row in result), reason)
    return result


def points(series: dict, reason: str = "malformed_or_incomplete_history") -> dict[int, float]:
    raw = series.get("values")
    require(isinstance(raw, list), reason)
    result: dict[int, float] = {}
    for row in raw:
        require(isinstance(row, list) and len(row) == 2, reason)
        try:
            timestamp, value = int(float(row[0])), float(row[1])
        except (TypeError, ValueError, OverflowError) as exc:
            raise InsufficientHistory(reason) from exc
        require(math.isfinite(value) and timestamp not in result, reason)
        result[timestamp] = value
    return result


def percentile95(values: list[float]) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[94]


def assess(responses: dict[str, object], now: int) -> dict:
    source_rows = parse_matrix(responses.get("source"))
    require(len(source_rows) > 0, "source_history_too_short")
    require(len(source_rows) == 1, "source_series_ambiguous")
    source_points = points(source_rows[0])
    require(len(source_points) >= MIN_METRIC_POINTS, "source_history_too_short")
    source_values = [(evaluation, int(value)) for evaluation, value in source_points.items()]
    source_events = []
    previous = None
    for evaluation, sampled_at in sorted(source_values):
        if sampled_at == previous:
            continue
        require(sampled_at <= evaluation)
        require(not source_events or sampled_at > source_events[-1][1])
        source_events.append((evaluation, sampled_at))
        previous = sampled_at
    if source_events and source_events[0][0] - source_events[0][1] > STEP_SECONDS + 300:
        source_events = source_events[1:]
    timestamps = [sampled_at for _, sampled_at in source_events]
    require(
        len(timestamps) >= 8 and timestamps[-1] - timestamps[0] >= MIN_HISTORY_SECONDS,
        "source_history_too_short",
    )
    require(now - timestamps[-1] <= MAX_SAMPLE_AGE_SECONDS and timestamps[-1] <= now + 300)
    require(max(b - a for a, b in zip(timestamps, timestamps[1:], strict=False)) <= MAX_SOURCE_GAP_SECONDS)

    backend_rows = parse_matrix(responses.get("backends"), "backend_metrics_unavailable")
    backend_points: dict[str, dict[int, float]] = {}
    for row in backend_rows:
        metric = row.get("metric")
        require(isinstance(metric, dict), "backend_metrics_unavailable")
        name = metric.get("backend")
        require(name in BACKENDS and name not in backend_points, "backend_metrics_unavailable")
        backend_points[name] = points(row, "backend_metrics_unavailable")
    require(set(backend_points) == BACKENDS, "backend_metrics_unavailable")

    # Each changed source timestamp must have all five byte gauges at the same
    # Prometheus evaluation, proving the values came from that complete file write.
    sample_values: dict[str, list[float]] = {name: [] for name in BACKENDS}
    for evaluation, sampled_at in source_events:
        require(0 <= evaluation - sampled_at <= STEP_SECONDS + 300, "backend_metrics_unavailable")
        for name in BACKENDS:
            value = backend_points[name].get(evaluation)
            require(value is not None and value >= 0, "backend_metrics_unavailable")
            sample_values[name].append(value)
    require(
        all(len(values) == len(timestamps) for values in sample_values.values()),
        "backend_metrics_unavailable",
    )

    observations: dict[str, object] = {}
    for key in ("cpu", "memory", "rootfs"):
        rows = parse_matrix(responses.get(key), "host_metric_history_unavailable")
        require(len(rows) == 1, "host_metric_history_unavailable")
        values = points(rows[0], "host_metric_history_unavailable")
        times = sorted(values)
        require(
            len(times) >= MIN_METRIC_POINTS and times[-1] - times[0] >= MIN_HISTORY_SECONDS,
            "host_metric_history_unavailable",
        )
        require(now - times[-1] <= MAX_METRIC_GAP_SECONDS, "host_metric_history_unavailable")
        require(
            max(b - a for a, b in zip(times, times[1:], strict=False)) <= MAX_METRIC_GAP_SECONDS,
            "host_metric_history_unavailable",
        )
        numbers = [values[timestamp] for timestamp in times]
        require(all(0 <= value <= 100 for value in numbers), "host_metric_history_unavailable")
        observation = {
            "points": len(numbers),
            "observed_p95_percent": round(percentile95(numbers), 2),
            "observed_min_percent": round(min(numbers), 2),
        }
        if key == "cpu":
            observation["p95_method"] = "p95_of_evaluated_five_minute_means"
        observations[key] = observation

    return {
        "status": "ready_for_separate_forecast_review",
        "history": {
            "source_samples": len(timestamps),
            "source_span_seconds": timestamps[-1] - timestamps[0],
            "latest_sample_age_seconds": now - timestamps[-1],
            "maximum_source_gap_seconds": max(
                b - a for a, b in zip(timestamps, timestamps[1:], strict=False)
            ),
            "evaluation_step_seconds": STEP_SECONDS,
        },
        "receiver_host_observations": observations,
        "backend_stored_bytes": {
            name: {
                "source_samples": len(values),
                "first_bytes": int(values[0]),
                "last_bytes": int(values[-1]),
                "change_bytes": int(values[-1] - values[0]),
            }
            for name, values in sorted(sample_values.items())
        },
    }


def query(metric: str, start: int, end: int) -> object:
    proc = subprocess.run(
        [
            ENGINE, "exec", CONTAINER, "curl", "-fsS", "--max-time", str(CURL_TIMEOUT_SECONDS), "--get",
            "--data-urlencode", f"query={QUERIES[metric]}",
            "--data-urlencode", f"start={start}",
            "--data-urlencode", f"end={end}",
            "--data-urlencode", f"step={STEP_SECONDS}",
            API,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=QUERY_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 or len(proc.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise InsufficientHistory("transport_unavailable")
    return json.loads(proc.stdout)


def main() -> int:
    try:
        end = int(time.time())
        start = end - RANGE_SECONDS
        responses = {key: query(key, start, end) for key in QUERIES}
        receipt = assess(responses, end)
    except InsufficientHistory as exc:
        reason = exc.reason
    except (OSError, subprocess.TimeoutExpired):
        reason = "transport_unavailable"
    except (ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError):
        reason = "malformed_or_incomplete_history"
    else:
        print(json.dumps(receipt, sort_keys=True))
        return 0
    print(
        json.dumps(
            {"status": "insufficient", "reason": reason},
            sort_keys=True,
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
