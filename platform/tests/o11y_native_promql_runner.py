#!/usr/bin/env python3
"""Run bounded native PromQL regressions against the committed Prometheus image."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/summarize-o11y-scrape-baseline.py"
COMPOSE = ROOT / "platform/services/o11y/deployment/compose.yml"
TOTAL_SECONDS = 300
CLEANUP_RESERVE_SECONDS = 45
PROCESS_CLEANUP_RESERVE_SECONDS = 5.0
PROCESS_REAP_FRACTION = 0.6
OUTPUT_LIMIT = 16_384
OWNER_LABEL = "io.agent-cloud.native-promql-owner"
RUN_LABEL = "io.agent-cloud.native-promql-run"


class RunnerError(RuntimeError):
    pass


def _load_helper():
    spec = importlib.util.spec_from_file_location("o11y_scrape_baseline", HELPER)
    if spec is None or spec.loader is None:
        raise RunnerError("could not load production helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fallback_image():
    try:
        import yaml

        class UniqueLoader(yaml.SafeLoader):
            pass

        def unique_mapping(loader, node, deep=False):
            mapping = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise RunnerError("duplicate key in committed Compose file")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        UniqueLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping,
        )
        document = yaml.load(COMPOSE.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except Exception as exc:  # sanitize parser failures instead of echoing committed configuration text
        if isinstance(exc, RunnerError):
            raise
        raise RunnerError("could not parse committed Compose image declaration") from None
    services = document.get("services") if isinstance(document, dict) else None
    prometheus = services.get("prometheus") if isinstance(services, dict) else None
    declaration = prometheus.get("image") if isinstance(prometheus, dict) else None
    if not isinstance(declaration, str):
        raise RunnerError("missing literal fallback Prometheus image")
    match = re.fullmatch(r"\$\{[A-Z_][A-Z0-9_]*:-([^{}]+)\}", declaration)
    image = match.group(1) if match else declaration
    if (
        not image
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image)
        or (":" not in image.rsplit("/", 1)[-1] and "@" not in image)
    ):
        raise RunnerError("malformed literal fallback Prometheus image")
    return image


def _expression(helper, families, **kwargs):
    evaluation_time = kwargs.setdefault("evaluation_time", 1_800_000_000)
    url = helper._availability_query_path(families, **kwargs)
    parsed = urlsplit(url)
    prometheus = urlsplit(helper.PROMETHEUS)
    if (parsed.scheme, parsed.netloc, parsed.path) != (
        prometheus.scheme, prometheus.netloc, "/api/v1/query",
    ):
        raise RunnerError("production query builder returned an unexpected URL")
    parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    if (
        len(parameters) != 2
        or parameters[0][0] != "query" or not parameters[0][1]
        or parameters[1] != ("time", str(evaluation_time))
        or urlencode(parameters) != parsed.query
    ):
        raise RunnerError("production query builder did not return one canonical evaluation time")
    return parameters[0][1]


def _builder_expressions(helper):
    self_families = tuple(helper.ALLOY_SELF_METRICS)
    health_families = tuple(helper.ALLOY_HEALTH_METRICS)
    if len(self_families) != 3 or len(health_families) != 6:
        raise RunnerError("production fixed family declarations changed unexpectedly")
    self_kwargs = {
        "job": "alloy", "environment": "prod", "cluster": "prod-west",
        "instance": "alloy:12345", "target_id": "service:o11y",
    }
    health_kwargs = {"job": "o11y-availability", "environment": "prod", "cluster": "prod-west"}
    self_values = _expression(helper, self_families, **self_kwargs)
    health_values = _expression(helper, health_families, **health_kwargs)
    self_timestamps = _expression(helper, self_families, timestamp=True, **self_kwargs)
    health_timestamps = _expression(helper, health_families, timestamp=True, **health_kwargs)
    max_self_url = helper._availability_query_path(
        self_families, job="alloy", environment="p" + "r" * 31,
        cluster="c" * 64, instance="a" + ":" * 127,
        target_id="a" + ":" * 127, timestamp=True, evaluation_time=1_800_000_000,
    )
    max_health_url = helper._availability_query_path(
        health_families, job="a" + "b" * 63, environment="p" + "r" * 31,
        cluster="c" * 64, timestamp=True, evaluation_time=1_800_000_000,
    )
    if not max_self_url.startswith(helper.PROMETHEUS) or not max_health_url.startswith(helper.PROMETHEUS):
        raise RunnerError("production query builder returned an unexpected maximum-context URL")
    max_self_path = max_self_url.removeprefix(helper.PROMETHEUS)
    max_health_path = max_health_url.removeprefix(helper.PROMETHEUS)
    if not max_self_path.startswith("/api/v1/query?") or not max_health_path.startswith("/api/v1/query?"):
        raise RunnerError("production query builder returned an unexpected maximum-context API path")
    if len(max_self_path) > 4096 or len(max_health_path) > 4096:
        raise RunnerError("supported maximum-context path exceeds the existing path ceiling")
    return {
        "self_families": self_families,
        "health_families": health_families,
        "self_values": self_values,
        "health_values": health_values,
        "self_timestamps": self_timestamps,
        "health_timestamps": health_timestamps,
        "grouped_self": f"timestamp({self_values})",
        "grouped_health": f"timestamp({health_values})",
        "max_self_api_path_length": len(max_self_path),
        "max_health_api_path_length": len(max_health_path),
    }


def _series_and_expected(expressions):
    self_identity = {
        "job": "alloy", "instance": "alloy:12345", "service": "o11y/alloy",
        "cluster": "prod-west", "environment": "prod", "target_id": "service:o11y",
    }
    identities = [
        {
            "job": "o11y-availability", "instance": "service:api", "service": "api/service",
            "cluster": "prod-west", "environment": "prod", "target_id": "service:api",
        },
        {
            "job": "o11y-availability", "instance": "service:worker", "service": "worker/service",
            "cluster": "prod-west", "environment": "prod", "target_id": "service:worker",
        },
    ]
    series = []
    expected_self = []
    expected_health = []
    for family in expressions["self_families"]:
        labels = {"__name__": family, **self_identity}
        value = 10 + len(series)
        series.append((family, labels, value))
        expected_self.append((labels, 0.0))
        # Keep every other identity field correct so each decoy isolates one matcher.
        for identity_field, wrong_value in (
            ("instance", "alloy:wrong-instance"),
            ("target_id", "service:wrong-target"),
        ):
            decoy_identity = {**self_identity, identity_field: wrong_value}
            decoy_labels = {"__name__": family, **decoy_identity}
            series.append((family, decoy_labels, 10 + len(series)))
    for identity_index, identity in enumerate(identities):
        for family_index, family in enumerate(expressions["health_families"]):
            # One required family is absent for the second identity; absence must stay absent.
            if identity_index == 1 and family_index == len(expressions["health_families"]) - 1:
                continue
            labels = {"__name__": family, **identity}
            if identity_index == 0 and family_index == 0:
                labels["unexpected"] = "preserved"
            value = 20 + identity_index * 10 + family_index
            series.append((family, labels, value))
            expected_health.append((labels, 0.0))
    input_lines = [
        f"{family}{{{_format_labels({key: value for key, value in labels.items() if key != '__name__'})}}} {value}"
        for family, labels, value in series
    ]
    return input_lines, expected_self, expected_health, identities, self_identity


def _format_labels(labels):
    return ",".join(f'{name}="{value}"' for name, value in sorted(labels.items()))


def _samples_yaml(samples):
    if not samples:
        return "        exp_samples: []"
    rows = ["        exp_samples:"]
    for labels, value in samples:
        label_text = "{" + _format_labels(labels) + "}"
        rows.extend((f"          - labels: {json.dumps(label_text)}", f"            value: {value}"))
    return "\n".join(rows)


def _rules(expressions, expression_mutant=None):
    inputs, expected_self, expected_health, *_ = _series_and_expected(expressions)
    self_expr = expression_mutant[0] if expression_mutant else expressions["self_timestamps"]
    health_expr = expression_mutant[1] if expression_mutant else expressions["health_timestamps"]
    input_rows = []
    for line in inputs:
        series, value = line.rsplit(" ", 1)
        input_rows.extend((f"    - series: {json.dumps(series)}", f"      values: {json.dumps(value)}"))
    input_block = "\n".join(input_rows)
    return (
        "rule_files: []\n"
        "evaluation_interval: 1m\n"
        "tests:\n"
        "  - interval: 1m\n"
        "    input_series:\n"
        f"{input_block}\n"
        "    promql_expr_test:\n"
        f"      - expr: {json.dumps(self_expr)}\n"
        "        eval_time: 2m\n"
        f"{_samples_yaml(expected_self)}\n"
        f"      - expr: {json.dumps(health_expr)}\n"
        "        eval_time: 2m\n"
        f"{_samples_yaml(expected_health)}\n"
    ).encode()


def _classify_evaluator_failure(output):
    text = output.decode("utf-8", errors="replace").lower()
    setup_markers = (
        "error parsing", "parse error", "yaml", "failed to read", "no such file",
        "unknown flag", "cannot connect", "cannot open", "fixture error",
        "error loading", "invalid rule file", "could not load", "input_series",
        "failed to parse", "invalid sample", "could not parse test",
    )
    if any(token in text for token in setup_markers):
        return None
    duplicate_markers = ("duplicate output labelset", "duplicate labelset", "same labelset")
    if any(token in text for token in duplicate_markers):
        return "duplicate_output_labelset"
    mismatch_markers = ("query result does not match", "unexpected sample", "expected samples", "got samples")
    if any(token in text for token in mismatch_markers):
        return "expected_result_mismatch"
    # Promtool 3.1 reports semantic fixture mismatches as a structured FAILED /
    # expr / exp / got block rather than the prose used by older releases.
    if re.search(
        r"(?ms)^\s*failed:\s*$.*?^\s*expr:.*?^\s*exp:.*?^\s*got:", text,
    ):
        return "expected_result_mismatch"
    return None


def _bounded_process(argv, *, deadline, input_bytes=None):
    started = time.monotonic()
    if started >= deadline:
        return 124, b"", True, False
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, bufsize=0,
        )
    except OSError as exc:
        return 127, f"{type(exc).__name__}: process could not start".encode(), False, False
    captured = bytearray()
    output_truncated = threading.Event()
    capture_lock = threading.Lock()

    def drain_output():
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                return
            with capture_lock:
                room = OUTPUT_LIMIT - len(captured)
                if room > 0:
                    captured.extend(chunk[:room])
                if len(chunk) > room:
                    output_truncated.set()

    def send_input():
        try:
            view = memoryview(input_bytes)
            offset = 0
            while offset < len(view):
                offset += os.write(process.stdin.fileno(), view[offset:offset + 65_536])
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    reader = threading.Thread(target=drain_output, name="native-promql-output", daemon=True)
    reader.start()
    writer = None
    if input_bytes is not None:
        writer = threading.Thread(target=send_input, name="native-promql-input", daemon=True)
        writer.start()
    remaining = max(0.0, deadline - started)
    cleanup_reserve = min(PROCESS_CLEANUP_RESERVE_SECONDS, remaining * 0.25)
    process_deadline = deadline - cleanup_reserve
    reap_reserve = cleanup_reserve * PROCESS_REAP_FRACTION
    thread_reserve = cleanup_reserve - reap_reserve
    timed_out = False
    try:
        process.wait(timeout=max(0, process_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        reap_deadline = min(deadline - thread_reserve, time.monotonic() + reap_reserve)
        try:
            process.wait(timeout=max(0, reap_deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise RunnerError("timed-out child process was not reaped within its reserved cleanup window") from exc
    if process.returncode is None:
        raise RunnerError("child process exited without a completed reap")
    for thread in (writer, reader):
        if thread is None:
            continue
        thread.join(timeout=max(0, deadline - time.monotonic()))
        if thread.is_alive():
            raise RunnerError("subprocess I/O did not finish within its bounded deadline")
    process.stdout.close()
    return (
        process.returncode if process.returncode is not None else 124,
        bytes(captured), timed_out, output_truncated.is_set(),
    )


def _engine_candidates():
    configured = os.environ.get("GITHUB_ACTIONS") == "true"
    names = ["docker"] if configured else ["podman", "docker"]
    return [name for name in names if shutil.which(name)]


def _engine_ready(engine, deadline):
    result, output, timed_out, _truncated = _bounded_process([engine, "info"], deadline=deadline)
    return result == 0 and not timed_out, output


def _classify_image_status(engine, code, output, timed_out, truncated):
    if timed_out or truncated:
        raise RunnerError("image inspection timed out or exceeded its output cap")
    if code == 0:
        return True
    if (Path(engine).name == "podman" or engine == "podman") and code == 1 and not output.strip():
        return False
    message = output.decode("utf-8", errors="replace").lower()
    absent_markers = ("no such image", "image not found", "no such object")
    if code == 1 and any(marker in message for marker in absent_markers):
        return False
    raise RunnerError("image inspection failed; acquisition refused")


def _image_present(engine, image, deadline):
    is_podman = Path(engine).name == "podman" or engine == "podman"
    argv = [engine, "image", "exists", image] if is_podman else [engine, "image", "inspect", image]
    code, output, timed_out, truncated = _bounded_process(argv, deadline=deadline)
    return _classify_image_status(engine, code, output, timed_out, truncated)


def _list_container_names(engine, deadline):
    code, output, timed_out, truncated = _bounded_process(
        [engine, "ps", "-a", "--format", "{{.Names}}"], deadline=min(deadline, time.monotonic() + 4),
    )
    if code != 0 or timed_out or truncated:
        raise RunnerError("container name inventory failed or was incomplete; cleanup refused")
    return {row.strip().lstrip("/") for row in output.decode("utf-8", errors="replace").splitlines() if row.strip()}


def _owned_containers(engine, owner, names, deadline):
    owned = []
    listed = _list_container_names(engine, deadline)
    for name in names:
        if name not in listed:
            continue
        inspect_deadline = min(deadline, time.monotonic() + 3)
        code, detail, timed_out, truncated = _bounded_process(
            [engine, "inspect", name], deadline=inspect_deadline,
        )
        if code != 0 or timed_out or truncated:
            raise RunnerError("container ownership inspection failed; cleanup refused")
        try:
            record = json.loads(detail)
            record = record[0] if isinstance(record, list) else record
            actual_name = str(record.get("Name", "")).lstrip("/")
            labels = record.get("Config", {}).get("Labels", {})
        except (ValueError, AttributeError):
            raise RunnerError("container ownership could not be proven; cleanup refused") from None
        if actual_name != name or labels.get(OWNER_LABEL) != owner or labels.get(RUN_LABEL) != name:
            raise RunnerError("container ownership mismatch; cleanup refused")
        owned.append(name)
    return owned


def _cleanup(engine, owner, names, deadline):
    for name in _owned_containers(engine, owner, names, deadline):
        code, _output, timed_out, _truncated = _bounded_process(
            [engine, "rm", "--force", "--volumes", name],
            deadline=min(deadline, time.monotonic() + 4),
        )
        if code != 0 or timed_out:
            raise RunnerError(f"owned container cleanup failed for {name}")
    remaining = _list_container_names(engine, deadline)
    if remaining.intersection(names):
        raise RunnerError("invocation-owned evaluator container remains after cleanup")


def _consumer_fixture_check(helper, expressions):
    _inputs, _self, _health, identities, _self_identity = _series_and_expected(expressions)
    identity = identities[0]
    labels = {"__name__": expressions["health_families"][0], **identity, "unexpected": "preserved"}
    body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": labels, "value": ["120", "120"]},
    ]}}).encode()
    try:
        helper._native_metric_rows(
            body,
            families=set(expressions["health_families"]),
            expected={identity["target_id"]: identity},
        )
    except helper.Refusal as exc:
        if str(exc) == "native_samples_ambiguous":
            return
        raise RunnerError("unchanged consumer rejected the extra-label fixture for the wrong reason") from None
    raise RunnerError("unchanged consumer accepted an unexpected label")


def _query_mutants(expressions):
    plain_self = re.sub(r"\s+or\s+on\s*\(\s*__name__\s*\)", " or ", expressions["self_timestamps"])
    plain_health = re.sub(r"\s+or\s+on\s*\(\s*__name__\s*\)", " or ", expressions["health_timestamps"])
    if plain_self == expressions["self_timestamps"] or plain_health == expressions["health_timestamps"]:
        raise RunnerError("plain-or mutant could not be derived from the production expression")
    return plain_self, plain_health


def _evaluator_command(engine, image, name, owner):
    return [
        engine, "run", "--rm", "-i", "--network", "none", "--pull=never",
        "--name", name,
        "--label", f"{OWNER_LABEL}={owner}",
        "--label", f"{RUN_LABEL}={name}",
        "--entrypoint", "/bin/promtool", image, "test", "rules", "/dev/stdin",
    ]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline-red", action="store_true",
        help="prove the unchanged grouped query fails in Prometheus",
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    deadline = started + TOTAL_SECONDS
    cleanup_deadline = deadline
    owner = uuid.uuid4().hex
    names = [f"o11y-promql-{owner[:16]}-{index}" for index in range(3)]
    engine = None
    cleanup_completed = False
    try:
        helper = _load_helper()
        image = _fallback_image()
        expressions = _builder_expressions(helper)
        _consumer_fixture_check(helper, expressions)
        candidates = _engine_candidates()
        if not candidates:
            raise RunnerError("no supported container engine executable is installed")
        for candidate in candidates:
            ready, _output = _engine_ready(
                candidate, min(deadline - CLEANUP_RESERVE_SECONDS, time.monotonic() + 20),
            )
            if ready:
                engine = candidate
                break
        if engine is None:
            raise RunnerError("no existing container engine is usable; no daemon or VM was started")
        if not _image_present(engine, image, min(deadline - CLEANUP_RESERVE_SECONDS, time.monotonic() + 10)):
            pull_deadline = min(deadline - CLEANUP_RESERVE_SECONDS, time.monotonic() + 120)
            code, _output, timed_out, _truncated = _bounded_process(
                [engine, "pull", image], deadline=pull_deadline,
            )
            if code != 0 or timed_out:
                raise RunnerError("one permitted image acquisition failed")
        if not _image_present(engine, image, deadline - CLEANUP_RESERVE_SECONDS):
            raise RunnerError("fallback Prometheus image is unavailable after acquisition")

        if args.baseline_red:
            cases = [(
                "grouped", f"timestamp({expressions['self_values']})",
                f"timestamp({expressions['health_values']})",
            )]
        else:
            plain_or = _query_mutants(expressions)
            cases = [
                ("positive", expressions["self_timestamps"], expressions["health_timestamps"]),
                ("grouped", f"timestamp({expressions['self_values']})", f"timestamp({expressions['health_values']})"),
                ("plain-or", *plain_or),
            ]
        for index, (case_name, self_expr, health_expr) in enumerate(cases):
            invocation_name = names[index]
            argv = _evaluator_command(engine, image, invocation_name, owner)
            runs_left = len(cases) - index
            available = deadline - CLEANUP_RESERVE_SECONDS - time.monotonic()
            if available <= 0:
                raise RunnerError("insufficient time remains for evaluator and owned cleanup")
            run_seconds = min(45, available / runs_left)
            code, output, timed_out, output_truncated = _bounded_process(
                argv, deadline=min(deadline - CLEANUP_RESERVE_SECONDS, time.monotonic() + run_seconds),
                input_bytes=_rules(expressions, (self_expr, health_expr)),
            )
            if timed_out:
                raise RunnerError(f"{case_name} evaluator timed out")
            if output_truncated:
                raise RunnerError(f"{case_name} evaluator output exceeded the cap; result classification refused")
            if args.baseline_red:
                failure = _classify_evaluator_failure(output)
                if code == 0 or failure != "duplicate_output_labelset":
                    detail = output.decode("utf-8", errors="replace")
                    raise RunnerError(
                        "original grouped query did not fail from duplicate PromQL output; "
                        f"bounded evaluator output: {detail}"
                    )
                print("RED: original grouped timestamp expression reproduced duplicate PromQL output")
            elif case_name == "positive":
                if code != 0:
                    raise RunnerError("repaired production query failed its native positive fixture")
                print("PASS: repaired production query preserved all expected families, labels, and raw timestamps")
            else:
                failure = _classify_evaluator_failure(output)
                if code == 0 or failure not in {"duplicate_output_labelset", "expected_result_mismatch"}:
                    detail = output.decode("utf-8", errors="replace")
                    raise RunnerError(
                        f"{case_name} mutant failure was not a PromQL duplicate/result mismatch; "
                        f"bounded evaluator output: {detail}"
                    )
                print(f"KILLED: {case_name} mutant failed during PromQL evaluation ({failure})")
        if not args.baseline_red:
            print("PASS: unexpected label survives expression fixture and unchanged production consumer rejects it")
            print("PASS: absent family remains absent; source timestamps are 0s at a 2m evaluation time")
            print("PASS: maximum supported caller context stays under the existing 4096-character API-path ceiling")
            print(
                "PASS: maximum encoded API paths are "
                f"{expressions['max_self_api_path_length']} chars (self) and "
                f"{expressions['max_health_api_path_length']} chars (health); "
                "three expected self families exclude six identity decoys"
            )
        _cleanup(engine, owner, names, cleanup_deadline)
        cleanup_completed = True
        return 0
    except RunnerError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        if engine is not None and not cleanup_completed:
            try:
                _cleanup(engine, owner, names, cleanup_deadline)
            except RunnerError as exc:
                print(f"CLEANUP LIMITATION: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
