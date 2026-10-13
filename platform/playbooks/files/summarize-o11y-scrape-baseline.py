#!/usr/bin/env python3
"""Collect private scrape evidence and emit only a bounded public receipt."""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlsplit

ENGINE = "podman"
CONTAINER = "o11y-grafana"
PROMETHEUS = "http://prometheus:9090"
PROMETHEUS_SELF_INSTANCE = "localhost:9090"
PROMETHEUS_SELF_URL = "http://localhost:9090/metrics"
PROMETHEUS_SELF_PROBE_URL = "http://prometheus:9090/metrics"
SAMPLES_QUERY = "max_over_time(scrape_samples_scraped[24h])"
MAX_API_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 160 * 1024 * 1024
MAX_RESPONSE_HEADERS = 64 * 1024
CONNECT_TIMEOUT = 3
TRANSFER_TIMEOUT = 10
CURL_MIN_VERSION = (8, 4, 0)
MAX_ROWS = 10000
MAX_INTEGER_DIGITS = 20

JOB_ALIASES = {
    "prometheus": "prometheus",
    "receiver-host": "receiver-host",
    "grafana": "grafana",
    "loki": "loki",
    "alloy": "alloy",
    "tempo": "tempo",
    "pyroscope": "pyroscope",
    "agentgateway": "agentgateway",
    "dgx-spark-node": "dgx-spark",
    "dgx-spark-gpu": "dgx-spark",
    "dgx-spark-vllm": "dgx-spark",
}
HOST_JOB = re.compile(r"host-node-[a-z0-9][a-z0-9_-]{0,31}\Z")
VERSION = re.compile(rb"^curl (\d+)\.(\d+)\.(\d+)", re.MULTILINE)
STATUS_LINE = re.compile(rb"^HTTP/\d(?:\.\d)? ([0-9]{3})(?: .*)?\r?$", re.IGNORECASE)
DECIMAL = re.compile(r"^[0-9]{1,20}$")
NUMERIC_HOST = re.compile(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)(?:\.(?:[0-9]+|0[xX][0-9a-fA-F]+))*\Z")


class Refusal(ValueError):
    """A fixed refusal category that contains no target-controlled text."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _public_job(job: str) -> str:
    if job in JOB_ALIASES:
        return JOB_ALIASES[job]
    if HOST_JOB.fullmatch(job):
        return "host-node"
    raise Refusal("unrecognized_job")


def _identity(labels: Any, category: str) -> tuple[str, str]:
    if not isinstance(labels, dict):
        raise Refusal(category)
    job, instance = labels.get("job"), labels.get("instance")
    if not isinstance(job, str) or not job or not isinstance(instance, str) or not instance:
        raise Refusal(category)
    return job, instance


def _json_document(raw: bytes | str, category: str) -> dict[str, Any]:
    if not isinstance(raw, (bytes, str)) or not raw or len(raw) > MAX_API_BYTES:
        raise Refusal(category)
    try:
        value = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            object_pairs_hook=_unique_object,
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        raise Refusal(category) from None
    if not isinstance(value, dict) or value.get("status") != "success":
        raise Refusal(category)
    for field in ("warnings", "infos"):
        if field in value and value[field]:
            raise Refusal(category)
    return value


def _api_vector(raw: bytes, category: str) -> list[dict[str, Any]]:
    response = _json_document(raw, category)
    data = response.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise Refusal(category)
    rows = data.get("result")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise Refusal(category)
    return rows


def _declared_instance(instance: str) -> tuple[str, int]:
    try:
        if not instance or "\\" in instance or any(char.isspace() or ord(char) < 0x20 for char in instance):
            raise ValueError
        parsed = urlsplit("http://" + instance)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host or port is None or not 1 <= port <= 65535 or parsed.path or parsed.query or parsed.fragment:
            raise ValueError
        return _normal_host(host), port
    except (ValueError, UnicodeError):
        raise Refusal("unsafe_target_url") from None


def _normal_host(host: str) -> str:
    if "%" in host or host.endswith("."):
        raise ValueError("noncanonical host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if NUMERIC_HOST.fullmatch(host):
            raise ValueError("noncanonical numeric host") from None
        return host.encode("idna").decode("ascii").lower()
    canonical = address.compressed.lower()
    if str(address) != host and (address.version == 4 or canonical != host.lower()):
        raise ValueError("noncanonical IP address")
    return canonical


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
            and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


def validate_target_url(url: Any, instance: str) -> tuple[str, int, str]:
    """Require a plain HTTP URL whose authority exactly matches instance."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise Refusal("unsafe_target_url")
    if "\\" in url or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise Refusal("unsafe_target_url")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "http"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host:
            raise ValueError
        normalized_host = _normal_host(host)
        normalized_port = port if port is not None else 80
        if not 1 <= normalized_port <= 65535:
            raise ValueError
        if (normalized_host, normalized_port) != _declared_instance(instance):
            raise ValueError
        # Keep URL private; return normalized authority for duplicate detection only.
        return normalized_host, normalized_port, parsed.path
    except (ValueError, UnicodeError):
        raise Refusal("unsafe_target_url") from None


def _targets(raw: bytes) -> dict[tuple[str, str], tuple[str, str]]:
    response = _json_document(raw, "malformed_targets")
    data = response.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(active, list) or len(active) > MAX_ROWS:
        raise Refusal("malformed_targets")
    if not active:
        raise Refusal("no_active_targets")

    result: dict[tuple[str, str], tuple[str, str]] = {}
    urls: set[tuple[str, int, str]] = set()
    for target in active:
        if not isinstance(target, dict):
            raise Refusal("malformed_targets")
        job, instance = _identity(target.get("labels"), "malformed_targets")
        category = _public_job(job)
        if target.get("health") != "up":
            raise Refusal("unhealthy_target")
        original_url = target.get("scrapeUrl")
        url_key = validate_target_url(original_url, instance)
        probe_url = original_url
        if _is_loopback_host(url_key[0]):
            if (
                job == "prometheus"
                and instance == PROMETHEUS_SELF_INSTANCE
                and original_url == PROMETHEUS_SELF_URL
            ):
                probe_url = PROMETHEUS_SELF_PROBE_URL
            else:
                raise Refusal("unsafe_target_url")
        key = (job, instance)
        if key in result or url_key in urls:
            raise Refusal("ambiguous_targets")
        result[key] = (category, probe_url)
        urls.add(url_key)
    return result


def _positive_integer(value: Any, category: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Refusal(category)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise Refusal(category) from None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise Refusal(category)
    if len(number.as_tuple().digits) > MAX_INTEGER_DIGITS or number.adjusted() >= MAX_INTEGER_DIGITS:
        raise Refusal(category)
    return int(number)


def _samples(raw: bytes, targets: dict[tuple[str, str], tuple[str, str]]) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    for row in _api_vector(raw, "malformed_samples"):
        if not isinstance(row, dict):
            raise Refusal("malformed_samples")
        labels = row.get("metric")
        job, instance = _identity(labels, "malformed_samples")
        key = (job, instance)
        if key not in targets:
            continue  # 24-hour data for a target that is no longer active.
        value = row.get("value")
        if not isinstance(value, list) or len(value) != 2:
            raise Refusal("malformed_samples")
        if key in result:
            raise Refusal("ambiguous_metric")
        result[key] = _positive_integer(value[1], "malformed_samples")
    if set(result) != set(targets):
        raise Refusal("missing_samples")
    return result


def _label_series(raw: bytes, key: tuple[str, str]) -> tuple[int, int, int]:
    response = _json_document(raw, "malformed_series")
    rows = response.get("data")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise Refusal("missing_series" if rows == [] else "malformed_series")

    seen: set[tuple[tuple[str, str], ...]] = set()
    max_count = 0
    max_name_bytes = 0
    max_value_bytes = 0
    for labels in rows:
        if not isinstance(labels, dict) or not labels:
            raise Refusal("malformed_series")
        if labels.get("job") != key[0] or labels.get("instance") != key[1]:
            raise Refusal("ambiguous_series")
        if not isinstance(labels.get("__name__"), str) or not labels["__name__"]:
            raise Refusal("malformed_series")
        if any(not isinstance(name, str) or not name or not isinstance(value, str) for name, value in labels.items()):
            raise Refusal("malformed_series")
        identity = tuple(sorted(labels.items()))
        if identity in seen:
            raise Refusal("ambiguous_series")
        seen.add(identity)
        try:
            max_count = max(max_count, len(labels))
            max_name_bytes = max(max_name_bytes, *(len(name.encode("utf-8")) for name in labels))
            max_value_bytes = max(max_value_bytes, *(len(value.encode("utf-8")) for value in labels.values()))
        except UnicodeEncodeError:
            raise Refusal("malformed_series") from None
    if min(max_count, max_name_bytes, max_value_bytes) <= 0:
        raise Refusal("malformed_series")
    return max_count, max_name_bytes, max_value_bytes


def _series_selector(job: str, instance: str) -> str:
    return "{job=" + json.dumps(job, ensure_ascii=True) + ",instance=" + json.dumps(instance, ensure_ascii=True) + "}"


def _series_config_url(job: str, instance: str, start: int, end: int) -> str:
    query = urlencode(
        [
            ("match[]", _series_selector(job, instance)),
            ("start", str(start)),
            ("end", str(end)),
        ]
    )
    return PROMETHEUS + "/api/v1/series?" + query


def _private_api_config(url: str) -> bytes:
    lines = [
        "url = " + json.dumps(url),
        'header = "Accept-Encoding: identity"',
        'proxy = ""',
        'noproxy = "*"',
        'proto = "=http"',
        "globoff",
        f"connect-timeout = {CONNECT_TIMEOUT}",
        'max-time = 15',
        f"max-filesize = {MAX_API_BYTES}",
        "max-redirs = 0",
        'output = "-"',
        'write-out = "%{http_code}"',
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _run_series_query(job: str, instance: str, start: int, end: int) -> bytes:
    config = _private_api_config(_series_config_url(job, instance, start, end))
    argv = [ENGINE, "exec", "-i", CONTAINER, "curl", "--disable", "--silent", "--show-error", "--fail", "--config", "-"]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise Refusal("series_unavailable") from None
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(config)
        process.stdin.close()
    except OSError:
        process.kill()
        process.wait()
        raise Refusal("series_unavailable") from None

    state: dict[str, Any] = {"output": bytearray(), "overflow": False, "error": False}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                remaining = MAX_API_BYTES + 3 - len(state["output"])
                if len(chunk) > remaining:
                    state["overflow"] = True
                    process.kill()
                    return
                state["output"].extend(chunk)
        except OSError:
            state["error"] = True

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(20)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = True
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    output = bytes(state["output"])
    if state["overflow"] or state["error"] or return_code != 0 or len(output) < 3:
        raise Refusal("series_unavailable")
    status_text = output[-3:]
    if not status_text.isdigit() or not 200 <= int(status_text) < 300:
        raise Refusal("series_unavailable")
    return output[:-3]


def _response_headers(raw: bytes) -> tuple[int, dict[str, list[str]], bytes]:
    marker = raw.find(b"\r\n\r\n")
    if marker < 0:
        raise Refusal("probe_unavailable")
    block = raw[:marker]
    if len(block) > MAX_RESPONSE_HEADERS:
        raise Refusal("probe_unavailable")
    lines = block.split(b"\r\n")
    status_match = STATUS_LINE.fullmatch(lines[0]) if lines else None
    if status_match is None:
        raise Refusal("probe_unavailable")
    status = int(status_match.group(1))
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if b":" not in line:
            raise Refusal("probe_unavailable")
        name, value = line.split(b":", 1)
        try:
            key = name.decode("ascii").strip().lower()
            text = value.decode("latin-1").strip().lower()
        except UnicodeDecodeError:
            raise Refusal("probe_unavailable") from None
        if not key:
            raise Refusal("probe_unavailable")
        headers.setdefault(key, []).append(text)
    return status, headers, raw[marker + 4 :]


def _curl_config(url: str) -> bytes:
    # URL is deliberately supplied through stdin, never through process argv.
    lines = [
        "url = " + json.dumps(url),
        'header = "Accept-Encoding: identity"',
        'proxy = ""',
        'noproxy = "*"',
        'proto = "=http"',
        "globoff",
        f"connect-timeout = {CONNECT_TIMEOUT}",
        f"max-time = {TRANSFER_TIMEOUT}",
        f"max-filesize = {MAX_RESPONSE_BYTES}",
        "max-redirs = 0",
        "dump-header = \"-\"",
        'output = "-"',
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _probe_command() -> list[str]:
    return [ENGINE, "exec", "-i", CONTAINER, "curl", "--disable", "--config", "-"]


def _run_probe(url: str) -> tuple[int, int]:
    config = _curl_config(url)
    try:
        process = subprocess.Popen(
            _probe_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise Refusal("probe_unavailable") from None
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(config)
        process.stdin.close()
    except OSError:
        process.kill()
        process.wait()
        raise Refusal("probe_unavailable") from None

    state: dict[str, Any] = {"headers": bytearray(), "body_bytes": 0, "status": None, "error": None}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                if state["status"] is None:
                    header_bytes = state["headers"]
                    header_bytes.extend(chunk)
                    marker = header_bytes.find(b"\r\n\r\n")
                    if marker < 0:
                        if len(header_bytes) > MAX_RESPONSE_HEADERS:
                            state["error"] = "probe_unavailable"
                            process.kill()
                            return
                        continue
                    status, headers, body = _response_headers(bytes(header_bytes))
                    state["status"] = status
                    state["headers"] = bytearray()
                    encodings = headers.get("content-encoding", [])
                    lengths = headers.get("content-length", [])
                    transfers = headers.get("transfer-encoding", [])
                    if status < 200 or status >= 300:
                        state["error"] = "probe_http_status"
                        process.kill()
                        return
                    if len(encodings) > 1 or any(value not in ("", "identity") for value in encodings):
                        state["error"] = "probe_compressed"
                        process.kill()
                        return
                    if len(transfers) > 1 or any(value != "chunked" for value in transfers):
                        state["error"] = "probe_compressed"
                        process.kill()
                        return
                    if len(lengths) > 1 or (lengths and not DECIMAL.fullmatch(lengths[0])):
                        state["error"] = "probe_unavailable"
                        process.kill()
                        return
                    if lengths and int(lengths[0]) > MAX_RESPONSE_BYTES:
                        state["error"] = "probe_too_large"
                        process.kill()
                        return
                    state["body_bytes"] = len(body)
                else:
                    state["body_bytes"] += len(chunk)
                if state["body_bytes"] > MAX_RESPONSE_BYTES:
                    state["error"] = "probe_too_large"
                    process.kill()
                    return
        except (OSError, Refusal):
            state["error"] = "probe_unavailable"
            try:
                process.kill()
            except OSError:
                pass

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(TRANSFER_TIMEOUT + CONNECT_TIMEOUT + 2)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = "probe_timeout"
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    if state["error"]:
        raise Refusal(state["error"])
    if state["status"] is None or return_code != 0:
        if state["body_bytes"] >= MAX_RESPONSE_BYTES:
            raise Refusal("probe_too_large")
        raise Refusal("probe_unavailable")
    if state["body_bytes"] > MAX_RESPONSE_BYTES:
        raise Refusal("probe_too_large")
    if state["body_bytes"] == 0:
        raise Refusal("missing_probe_data")
    return state["status"], state["body_bytes"]


def summarize(
    targets_raw: bytes | str,
    samples_raw: bytes | str,
    probe: Callable[[str], tuple[int, int]],
    series_query: Callable[[str, str, int, int], bytes],
    series_start: int,
    series_end: int,
) -> dict[str, Any]:
    if (
        isinstance(series_start, bool)
        or isinstance(series_end, bool)
        or not isinstance(series_start, int)
        or not isinstance(series_end, int)
        or series_end - series_start != 86400
    ):
        raise Refusal("malformed_series")
    if isinstance(targets_raw, str):
        targets_raw = targets_raw.encode()
    if isinstance(samples_raw, str):
        samples_raw = samples_raw.encode()
    target_map = _targets(targets_raw)
    sample_index = _samples(samples_raw, target_map)
    label_index: dict[tuple[str, str], tuple[int, int, int]] = {}
    for job, instance in target_map:
        series_raw = series_query(job, instance, series_start, series_end)
        label_index[(job, instance)] = _label_series(series_raw, (job, instance))

    totals: dict[str, dict[str, Any]] = {}
    for key, (public_job, url) in target_map.items():
        status, response_bytes = probe(url)
        if status < 200 or status >= 300:
            raise Refusal("probe_http_status")
        if isinstance(response_bytes, bool) or not isinstance(response_bytes, int):
            raise Refusal("probe_unavailable")
        if response_bytes == 0:
            raise Refusal("missing_probe_data")
        if response_bytes < 0 or response_bytes > MAX_RESPONSE_BYTES:
            raise Refusal("probe_too_large")
        row = totals.setdefault(
            public_job,
            {
                "job": public_job,
                "active_target_count": 0,
                "health": {"healthy": 0, "unhealthy": 0},
                "max_scrape_samples_scraped_24h": 0,
                "max_scrape_response_body_bytes_point_in_time": 0,
                "max_stored_series_label_count_24h": 0,
                "max_stored_series_label_name_bytes_24h": 0,
                "max_stored_series_label_value_bytes_24h": 0,
            },
        )
        row["active_target_count"] += 1
        row["health"]["healthy"] += 1
        row["max_scrape_samples_scraped_24h"] = max(row["max_scrape_samples_scraped_24h"], sample_index[key])
        row["max_scrape_response_body_bytes_point_in_time"] = max(
            row["max_scrape_response_body_bytes_point_in_time"], response_bytes
        )
        row["max_stored_series_label_count_24h"] = max(
            row["max_stored_series_label_count_24h"], label_index[key][0]
        )
        row["max_stored_series_label_name_bytes_24h"] = max(
            row["max_stored_series_label_name_bytes_24h"], label_index[key][1]
        )
        row["max_stored_series_label_value_bytes_24h"] = max(
            row["max_stored_series_label_value_bytes_24h"], label_index[key][2]
        )
    return {
        "status": "observed",
        "active_target_count": len(target_map),
        "jobs": [totals[name] for name in sorted(totals)],
    }


def _run_api(path: str, unavailable_reason: str = "query_unavailable") -> bytes:
    argv = [
        ENGINE,
        "exec",
        CONTAINER,
        "curl",
        "--disable",
        "--silent",
        "--show-error",
        "--fail",
        "--connect-timeout",
        str(CONNECT_TIMEOUT),
        "--max-time",
        "15",
        "--max-filesize",
        str(MAX_API_BYTES),
        "--proxy",
        "",
        "--noproxy",
        "*",
        "--write-out",
        "%{http_code}",
        PROMETHEUS + path,
    ]
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise Refusal(unavailable_reason) from None

    assert process.stdout is not None
    state: dict[str, Any] = {"output": bytearray(), "overflow": False, "error": False}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                remaining = MAX_API_BYTES + 3 - len(state["output"])
                if len(chunk) > remaining:
                    state["overflow"] = True
                    process.kill()
                    return
                state["output"].extend(chunk)
        except OSError:
            state["error"] = True

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(20)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = True
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    output = bytes(state["output"])
    if state["overflow"] or state["error"] or return_code != 0 or len(output) < 3:
        raise Refusal(unavailable_reason)
    status_text = output[-3:]
    if not status_text.isdigit() or not 200 <= int(status_text) < 300:
        raise Refusal(unavailable_reason)
    return output[:-3]


def _status_data(path: str, unavailable_reason: str) -> dict[str, Any]:
    response = _json_document(_run_api(path, unavailable_reason), unavailable_reason)
    data = response.get("data")
    if not isinstance(data, dict):
        raise Refusal(unavailable_reason)
    return data


def _runtime_signature(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict) or data.get("reloadConfigSuccess") is not True:
        raise Refusal("runtime_config_unavailable")
    start_time = data.get("startTime")
    config_time = data.get("lastConfigTime")
    if not isinstance(start_time, str) or not isinstance(config_time, str):
        raise Refusal("runtime_config_unavailable")
    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        config = datetime.fromisoformat(config_time.replace("Z", "+00:00"))
    except ValueError:
        raise Refusal("runtime_config_unavailable") from None
    if start.tzinfo is None or config.tzinfo is None:
        raise Refusal("runtime_config_unavailable")
    return start_time, config_time


def _active_target_pools(targets_raw: bytes | str) -> list[dict[str, str]]:
    response = _json_document(targets_raw, "malformed_targets")
    data = response.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(active, list) or not active or len(active) > MAX_ROWS:
        raise Refusal("malformed_targets")
    pools: dict[str, str] = {}
    for target in active:
        if not isinstance(target, dict):
            raise Refusal("malformed_targets")
        pool = target.get("scrapePool")
        labels = target.get("labels")
        job = labels.get("job") if isinstance(labels, dict) else None
        if not isinstance(pool, str) or not pool or not isinstance(job, str) or not job:
            raise Refusal("ambiguous_target_pool")
        try:
            pool_category = _public_job(pool)
            job_category = _public_job(job)
        except Refusal:
            raise Refusal("ambiguous_target_pool") from None
        if pool_category != job_category or (pool in pools and pools[pool] != job):
            raise Refusal("ambiguous_target_pool")
        pools[pool] = job
    return [{"pool": pool, "job": pools[pool]} for pool in sorted(pools)]


def _loaded_limit_readback(targets_raw: bytes | str) -> dict[str, Any]:
    build = _status_data("/api/v1/status/buildinfo", "buildinfo_unavailable")
    if build.get("version") != "3.1.0":
        raise Refusal("prometheus_version_mismatch")

    runtime_before = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )
    config_before = _status_data("/api/v1/status/config", "loaded_config_unavailable").get("yaml")
    runtime_middle = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )
    config_after = _status_data("/api/v1/status/config", "loaded_config_unavailable").get("yaml")
    runtime_after = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )

    for config_yaml in (config_before, config_after):
        if not isinstance(config_yaml, str) or not config_yaml:
            raise Refusal("loaded_config_unavailable")
        try:
            config_size = len(config_yaml.encode("utf-8"))
        except UnicodeError:
            raise Refusal("loaded_config_unavailable") from None
        if config_size > MAX_API_BYTES:
            raise Refusal("loaded_config_unavailable")

    if (
        config_before != config_after
        or runtime_before != runtime_middle
        or runtime_middle != runtime_after
    ):
        raise Refusal("unstable_loaded_config")

    return {
        "status": "observed",
        "reason": "stable_loaded_config",
        "config_yaml": config_after,
        "target_pools": _active_target_pools(targets_raw),
    }


def _require_curl_version() -> None:
    try:
        completed = subprocess.run(
            [ENGINE, "exec", CONTAINER, "curl", "--disable", "--version"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("curl_unsupported") from None
    match = VERSION.search(completed.stdout) if completed.returncode == 0 else None
    if match is None or tuple(int(piece) for piece in match.groups()) < CURL_MIN_VERSION:
        raise Refusal("curl_unsupported")


def collect() -> dict[str, Any]:
    _require_curl_version()
    targets_raw = _run_api("/api/v1/targets?state=active")
    samples_raw = _run_api("/api/v1/query?query=max_over_time%28scrape_samples_scraped%5B24h%5D%29")
    series_end = int(time.time())
    baseline = summarize(
        targets_raw,
        samples_raw,
        _run_probe,
        _run_series_query,
        series_end - 86400,
        series_end,
    )
    try:
        loaded_limits = _loaded_limit_readback(targets_raw)
    except Refusal as error:
        loaded_limits = {"status": "unavailable", "reason": error.category}
    except Exception:
        loaded_limits = {"status": "unavailable", "reason": "loaded_readback_unavailable"}
    return {"baseline": baseline, "loaded_limit_readback": loaded_limits}


def main() -> int:
    try:
        receipt = collect()
    except Refusal as error:
        print(json.dumps({"status": "unavailable", "reason": error.category}, sort_keys=True))
        return 2
    except Exception:
        # Never print subprocess details, target input, or an exception chain.
        print(json.dumps({"status": "unavailable", "reason": "survey_unavailable"}, sort_keys=True))
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
