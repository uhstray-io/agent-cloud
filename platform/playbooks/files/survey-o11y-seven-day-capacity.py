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
    pass


def require(condition: bool) -> None:
    if not condition:
        raise InsufficientHistory


def parse_matrix(body: object) -> list[dict]:
    require(isinstance(body, dict) and body.get("status") == "success")
    data = body.get("data")
    require(isinstance(data, dict) and data.get("resultType") == "matrix")
    result = data.get("result")
    require(isinstance(result, list))
    return result


def points(series: dict) -> dict[int, float]:
    raw = series.get("values")
    require(isinstance(raw, list))
    result: dict[int, float] = {}
    for row in raw:
        require(isinstance(row, list) and len(row) == 2)
        timestamp, value = int(float(row[0])), float(row[1])
        require(math.isfinite(value) and timestamp not in result)
        result[timestamp] = value
    return result


def percentile95(values: list[float]) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[94]


def assess(responses: dict[str, object], now: int) -> dict:
    source_rows = parse_matrix(responses.get("source"))
    require(len(source_rows) == 1)
    source_points = points(source_rows[0])
    require(len(source_points) >= MIN_METRIC_POINTS)
    source_values = [(evaluation, int(value)) for evaluation, value in source_points.items()]
    timestamps = sorted({sample for _, sample in source_values})
    require(len(timestamps) >= 8)
    require(timestamps[-1] - timestamps[0] >= MIN_HISTORY_SECONDS)
    require(now - timestamps[-1] <= MAX_SAMPLE_AGE_SECONDS and timestamps[-1] <= now + 300)
    require(max(b - a for a, b in zip(timestamps, timestamps[1:], strict=False)) <= MAX_SOURCE_GAP_SECONDS)

    backend_rows = parse_matrix(responses.get("backends"))
    backend_points: dict[str, dict[int, float]] = {}
    for row in backend_rows:
        metric = row.get("metric")
        require(isinstance(metric, dict))
        name = metric.get("backend")
        require(name in BACKENDS and name not in backend_points)
        backend_points[name] = points(row)
    require(set(backend_points) == BACKENDS)

    # Each changed source timestamp must have all five byte gauges at the same
    # Prometheus evaluation, proving the values came from that complete file write.
    sample_values: dict[str, list[float]] = {name: [] for name in BACKENDS}
    previous = None
    for evaluation, sampled_at in sorted(source_values):
        if sampled_at == previous:
            continue
        require(0 <= evaluation - sampled_at <= STEP_SECONDS + 300)
        for name in BACKENDS:
            value = backend_points[name].get(evaluation)
            require(value is not None and value >= 0)
            sample_values[name].append(value)
        previous = sampled_at
    require(all(len(values) == len(timestamps) for values in sample_values.values()))

    observations: dict[str, object] = {}
    for key in ("cpu", "memory", "rootfs"):
        rows = parse_matrix(responses.get(key))
        require(len(rows) == 1)
        values = points(rows[0])
        times = sorted(values)
        require(len(times) >= MIN_METRIC_POINTS and times[-1] - times[0] >= MIN_HISTORY_SECONDS)
        require(now - times[-1] <= MAX_METRIC_GAP_SECONDS)
        require(max(b - a for a, b in zip(times, times[1:], strict=False)) <= MAX_METRIC_GAP_SECONDS)
        numbers = [values[timestamp] for timestamp in times]
        require(all(0 <= value <= 100 for value in numbers))
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
        raise InsufficientHistory
    return json.loads(proc.stdout)


def main() -> int:
    try:
        end = int(time.time())
        start = end - RANGE_SECONDS
        responses = {key: query(key, start, end) for key in QUERIES}
        receipt = assess(responses, end)
    except (
        InsufficientHistory,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        print(
            json.dumps(
                {"status": "insufficient", "reason": "seven_day_capacity_history_unavailable"},
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
