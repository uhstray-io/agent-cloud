import hashlib
import importlib.util
import io
import ipaddress
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "playbooks/files/o11y-coverage-census.py"
SPEC = importlib.util.spec_from_file_location("o11y_coverage_census", SCRIPT)
CENSUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CENSUS)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
REPO_SHA = "a" * 40
INVENTORY_REVISION = "b" * 40
# Deterministic synthetic host from the canonical RFC1918 test network; this is
# not derived from any site inventory or deployment topology.
PRIVATE_TEST_IP = str(ipaddress.ip_network("192.168.0.0/16")[20])


def signal(receipt=None, reference="semaphore/task/123"):
    value = {
        "applicable": True,
        "selector": '{target_id="service:alpha"}',
        "collection_method": "alloy",
        "receipt_reference": reference,
        "freshness_seconds": 3600,
    }
    if receipt is not None:
        value["receipt"] = receipt
    return value


def signal_excluded(signal_name):
    exception = {"reason": f"{signal_name} is not supported for this test target", "reference": "review/123"}
    if signal_name == "traces":
        exception["alternative"] = "reviewed manual instrumentation plan"
    return {"applicable": False, "exception": exception}


def target(target_id="service:alpha", identity="alpha", signals=None, target_type="service"):
    declared_signals = {
        "logs": signal(),
        "metrics": signal_excluded("metrics"),
        "health": signal_excluded("health"),
        "traces": signal_excluded("traces"),
    }
    if signals is not None:
        declared_signals.update(signals)
    return {
        "target_id": target_id,
        "target_type": target_type,
        "lifecycle": "deployed",
        "owner": "platform",
        "runtime": "podman",
        "service_identity": identity,
        "environment": "production",
        "signals": declared_signals,
        "collection_method": "declared",
        "budget": {"samples_per_scrape": 1000},
        "receipt_reference": {
            name: declaration.get("receipt_reference")
            for name, declaration in declared_signals.items()
        },
        "template_references": ["Verify o11y Target Receipt"],
    }


def availability_target(
    target_id="service:alpha", identity="alpha", environment="production",
    endpoint=None,
):
    if endpoint is None:
        endpoint = f"https://{PRIVATE_TEST_IP}/healthz"
    declaration = target(target_id=target_id, identity=identity)
    declaration["signals"]["health"] = {
        "applicable": True,
        "selector": (
            f'probe_success{{job="o11y-availability",target_id="{target_id}",'
            f'environment="{environment}",cluster="prod-cluster"}}'
        ),
        "collection_method": "alloy-blackbox-http",
        "freshness_seconds": 300,
        "endpoint": endpoint,
        "expected_status_codes": [200],
        "tls_server_name": "health.example.invalid",
        "trust": "system",
    }
    declaration["environment"] = environment
    declaration["budget"]["health"] = {
        "interval_seconds": 60,
        "scrape_timeout_seconds": 20,
        "probe_timeout_seconds": 10,
        "body_size_limit_bytes": 4096,
        "samples_per_scrape": 64,
    }
    declaration["receipt_reference"].pop("health", None)
    return declaration


def prepare_availability(targets, **kwargs):
    if kwargs.get("enabled"):
        kwargs.setdefault("inventory_revision", CENSUS.canonical_inventory_revision(targets))
    return CENSUS.prepare_availability_targets(targets, **kwargs)


def receipt(target_id="service:alpha", signal_name="logs", inventory_revision=INVENTORY_REVISION,
            observed_at="2026-10-09T11:30:00Z", reference="semaphore/task/123"):
    return {
        "status": "verified",
        "target_id": target_id,
        "signal": signal_name,
        "receipt_reference": reference,
        "repository_sha": REPO_SHA,
        "inventory_revision": inventory_revision,
        "observed_at": observed_at,
    }


def observation(signal_name, response, **overrides):
    payload = {
        "response": response,
        "command_rc": 0,
        "signal": signal_name,
        "target_id": "service:alpha",
        "query_start": 1000,
        "query_end": 1300,
        "observed_at": 1300,
        "requested_freshness_seconds": 600,
        "declared_freshness_seconds": 600,
        "window_seconds": 300,
    }
    payload.update(overrides)
    return payload


def prometheus(values=None, histograms=None, target_id="service:alpha"):
    series = {"metric": {"target_id": target_id}}
    if values is not None:
        series["values"] = values
    if histograms is not None:
        series["histograms"] = histograms
    return {"status": "success", "data": {"resultType": "matrix", "result": [series]}}


def tempo(timestamp="1300000000000", target_id="service:alpha"):
    return {"traces": [{
        "rootServiceName": "parent-service",
        "spanSets": [{"spans": [{
            "spanID": "child-span",
            "startTimeUnixNano": timestamp,
            "attributes": [{"key": "target_id", "value": {"stringValue": target_id}}],
        }]}],
    }]}


class CoverageCensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        (self.repo / "platform/services/alpha").mkdir(parents=True)
        (self.repo / "agents").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def report(self, targets, receipt_bindings=None, verified_receipts=None):
        inventory = {
            "inventory_revision": CENSUS.canonical_inventory_revision(targets),
            "targets": targets,
            "template_names": ["Verify o11y Target Receipt"],
        }
        if receipt_bindings is not None:
            inventory["receipt_bindings"] = receipt_bindings
        return CENSUS.build_report(
            self.repo,
            inventory,
            REPO_SHA,
            NOW,
            verified_receipts=verified_receipts,
        )

    def test_availability_inventory_is_default_off_and_projects_only_fixed_contract(self):
        declaration = availability_target()
        CENSUS.validate([declaration])
        disabled = prepare_availability(
            [declaration], enabled=False, reviewed=False, limits=None,
            receiver_environment="production", cluster="prod-cluster",
            shared_sample_limit=2000,
        )
        self.assertEqual(disabled, {"enabled": False, "targets": [], "limits": {}})
        self.assertEqual(
            prepare_availability(
                [{"malformed": "unrelated legacy declaration"}], enabled=False,
                reviewed=False, limits=None, receiver_environment="production",
                cluster="prod-cluster", shared_sample_limit=None,
            ),
            {"enabled": False, "targets": [], "limits": {}},
        )

        prepared = prepare_availability(
            [declaration], enabled=True, reviewed=True,
            limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
            receiver_environment="production", cluster="prod-cluster",
            shared_sample_limit=2000,
        )
        self.assertEqual(prepared["enabled"], True)
        self.assertEqual(prepared["targets"], [{
            "module_name": "health_01",
            "target_id": "service:alpha", "service_identity": "alpha",
            "environment": "production", "cluster": "prod-cluster",
            "endpoint": f"https://{PRIVATE_TEST_IP}/healthz",
            "freshness_seconds": 300,
            "expected_status_codes": [200], "tls_server_name": "health.example.invalid",
            "trust": "system", "interval_seconds": 60,
            "scrape_timeout_seconds": 20, "probe_timeout_seconds": 10,
            "body_size_limit_bytes": 4096, "samples_per_scrape": 64,
            "blackbox_config": json.dumps({"modules": {"health_01": {
                "prober": "http", "timeout": "10s", "http": {
                    "method": "GET", "valid_status_codes": [200],
                    "follow_redirects": False, "proxy_from_environment": False,
                    "body_size_limit": "4096B",
                    "tls_config": {
                        "insecure_skip_verify": False,
                        "server_name": "health.example.invalid",
                    },
                },
            }}}, sort_keys=True, separators=(",", ":")),
        }])

        internal_ca = availability_target()
        internal_ca["signals"]["health"]["trust"] = "internal-ca"
        ca_module = json.loads(prepare_availability(
            [internal_ca], enabled=True, reviewed=True,
            limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
            receiver_environment="production", cluster="prod-cluster",
            shared_sample_limit=2000,
        )["targets"][0]["blackbox_config"])["modules"]["health_01"]
        self.assertEqual(ca_module["http"]["tls_config"], {
            "insecure_skip_verify": False,
            "server_name": "health.example.invalid",
            "ca_file": "/certs/step-ca-bundle.crt",
        })

    def test_availability_requires_literal_rfc1918_http_target_and_exact_contract(self):
        cases = [
            ("https://health.example.invalid/healthz", "endpoint"),
            (f"https://{PRIVATE_TEST_IP}/healthz?token=x", "endpoint"),
            (f"https://user@{PRIVATE_TEST_IP}/healthz", "endpoint"),
            ("https://8.8.8.8/healthz", "endpoint"),
            (f"https://{PRIVATE_TEST_IP}/healthz#ready", "endpoint"),
            (f"https://{PRIVATE_TEST_IP}/" + "a" * 2048, "endpoint"),
        ]
        for endpoint, _ in cases:
            declaration = availability_target(endpoint=endpoint)
            with self.subTest(endpoint=endpoint), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                prepare_availability(
                    [declaration], enabled=True, reviewed=True,
                    limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
                    receiver_environment="production", cluster="prod-cluster",
                    shared_sample_limit=2000,
                )

        http_target = availability_target(endpoint=f"http://{PRIVATE_TEST_IP}/healthz")
        http_target["signals"]["health"].pop("trust")
        http_target["signals"]["health"].pop("tls_server_name")
        prepared = prepare_availability(
            [http_target], enabled=True, reviewed=True,
            limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
            receiver_environment="production", cluster="prod-cluster",
            shared_sample_limit=2000,
        )
        http_config = json.loads(prepared["targets"][0]["blackbox_config"])["modules"]["health_01"]["http"]
        self.assertNotIn("tls_config", http_config)

    def test_availability_rejects_status_booleans_bad_tls_and_over_budget_values(self):
        invalid = []
        item = availability_target()
        item["signals"]["health"]["expected_status_codes"] = [True]
        invalid.append(item)
        item = availability_target()
        item["signals"]["health"]["trust"] = "insecure"
        invalid.append(item)
        item = availability_target()
        item["signals"]["health"]["selector"] = '{target_id="service:alpha"}'
        invalid.append(item)
        item = availability_target()
        item["budget"]["health"]["probe_timeout_seconds"] = 11
        invalid.append(item)
        item = availability_target()
        item["budget"]["health"]["samples_per_scrape"] = 257
        invalid.append(item)
        item = availability_target()
        item["signals"]["health"]["freshness_seconds"] = 60
        invalid.append(item)
        for declaration in invalid:
            with self.subTest(declaration=declaration), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                prepare_availability(
                    [declaration], enabled=True, reviewed=True,
                    limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
                    receiver_environment="production", cluster="prod-cluster",
                    shared_sample_limit=2000,
                )

    def test_availability_freshness_must_exceed_the_reviewed_scrape_interval(self):
        declaration = availability_target()
        declaration["signals"]["health"]["freshness_seconds"] = 61
        prepared = prepare_availability(
            [declaration], enabled=True, reviewed=True,
            limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
            receiver_environment="production", cluster="prod-cluster",
            shared_sample_limit=2000,
        )
        self.assertEqual(prepared["targets"][0]["freshness_seconds"], 61)

        declaration["signals"]["health"]["freshness_seconds"] = 60
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            CENSUS.validate([declaration])

    def test_availability_https_server_name_is_dns_not_an_ip_literal(self):
        accepted = availability_target()
        self.assertEqual(prepare_availability(
            [accepted], enabled=True, reviewed=True,
            limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
            receiver_environment="production", cluster="prod-cluster",
            shared_sample_limit=2000,
        )["targets"][0]["tls_server_name"], "health.example.invalid")

        literal = availability_target()
        literal["signals"]["health"]["tls_server_name"] = PRIVATE_TEST_IP
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            prepare_availability(
                [literal], enabled=True, reviewed=True,
                limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
                receiver_environment="production", cluster="prod-cluster",
                shared_sample_limit=2000,
            )

    def test_availability_requires_reviewed_nonempty_targets_and_matching_limits(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            prepare_availability(
                [], enabled=True, reviewed=True,
                limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
                receiver_environment="production", cluster="prod-cluster",
                shared_sample_limit=2000,
            )
        for limits in (
            {"max_targets": True, "max_total_timeout_seconds": 10, "max_series": 6},
            {"max_targets": 1, "max_total_timeout_seconds": 121, "max_series": 6},
            {"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 5},
            {"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 385},
        ):
            with self.subTest(limits=limits), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                prepare_availability(
                    [availability_target()], enabled=True, reviewed=True, limits=limits,
                    receiver_environment="production", cluster="prod-cluster",
                    shared_sample_limit=2000,
                )
        declaration = availability_target()
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            CENSUS.prepare_availability_targets(
                [declaration], enabled=True, reviewed=True,
                inventory_revision="a" * 64,
                limits={"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
                receiver_environment="production", cluster="prod-cluster",
                shared_sample_limit=2000,
            )

    def test_availability_cli_emits_only_the_fixed_sanitized_projection(self):
        payload = {
            "targets": [availability_target()],
            "inventory_revision": CENSUS.canonical_inventory_revision([availability_target()]),
            "enabled": True,
            "reviewed": True,
            "limits": {"max_targets": 1, "max_total_timeout_seconds": 10, "max_series": 6},
            "receiver_environment": "production",
            "cluster": "prod-cluster",
            "shared_sample_limit": 2000,
        }
        result = subprocess.run(
            ["python3", str(SCRIPT), "--prepare-availability"],
            input=json.dumps(payload), text=True, capture_output=True, check=True,
        )
        output = json.loads(result.stdout)
        self.assertEqual(output["enabled"], True)
        target_output = output["targets"][0]
        self.assertEqual(target_output["endpoint"], f"https://{PRIVATE_TEST_IP}/healthz")
        tls = json.loads(target_output["blackbox_config"])["modules"]["health_01"]["http"]["tls_config"]
        self.assertEqual(tls["insecure_skip_verify"], False)
        self.assertNotIn("password", result.stdout.lower())

    def test_optional_profiles_signal_preserves_the_existing_four_signal_contract(self):
        declaration = target(signals={name: signal_excluded(name) for name in CENSUS.SIGNALS})
        row = self.report([declaration])["targets"][0]
        self.assertEqual(row["coverage"], "verified")
        self.assertEqual(set(row["signals"]), CENSUS.SIGNALS)

    def test_profile_declaration_requires_the_pinned_type_and_exact_target_selector(self):
        declaration = target()
        declaration["signals"]["profiles"] = {
            "applicable": True,
            "selector": '{target_id="service:alpha"}',
            "profile_type_id": CENSUS.PROFILE_TYPE_ID,
            "collection_method": "alloy",
            "receipt_reference": "semaphore/task/124",
            "freshness_seconds": 600,
        }
        declaration["receipt_reference"]["profiles"] = "semaphore/task/124"
        CENSUS.validate([declaration])
        declaration["signals"]["profiles"]["selector"] = '{target_id="service:alpha",service_name="o11y"}'
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            CENSUS.validate([declaration])

    def test_profile_receipt_requires_the_exact_target_and_fresh_positive_sample(self):
        payload = {
            "target_id": "service:alpha",
            "start_ms": 1_000_000,
            "end_ms": 1_600_000,
            "freshness_seconds": 300,
            "response": {
                "series": [{
                    "labels": [{"name": "target_id", "value": "service:alpha"}],
                    "points": [{"timestamp": "1500000", "value": 3.0}],
                }],
            },
        }
        payload["response"]["series"][0]["points"].append({"timestamp": "1500000"})
        self.assertEqual(CENSUS.validate_profile_receipt(payload), {"status": "verified", "sample_count": 1})
        payload["response"]["series"][0]["points"][0]["value"] = None
        self.assertEqual(CENSUS.validate_profile_receipt(payload)["status"], "invalid_response")
        payload["response"]["series"][0]["points"][0]["value"] = 3.0
        payload["response"]["series"][0]["labels"][0]["value"] = "service:beta"
        self.assertEqual(CENSUS.validate_profile_receipt(payload)["status"], "invalid_response")
        payload["response"]["series"][0]["labels"][0]["value"] = "service:alpha"
        payload["response"]["series"][0]["points"][0]["timestamp"] = "1"
        self.assertEqual(CENSUS.validate_profile_receipt(payload)["status"], "no_fresh_sample")
        point = payload["response"]["series"][0]["points"][0]
        payload["freshness_seconds"] = 600
        point["timestamp"] = "1059999"
        self.assertEqual(CENSUS.validate_profile_receipt(payload)["status"], "no_fresh_sample")
        point["timestamp"] = "1060000"
        self.assertEqual(CENSUS.validate_profile_receipt(payload)["status"], "verified")
        payload["freshness_seconds"] = 300
        point["timestamp"] = "1359999"
        self.assertEqual(CENSUS.validate_profile_receipt(payload)["status"], "no_fresh_sample")
        point["timestamp"] = "1360000"
        self.assertEqual(CENSUS.validate_profile_receipt(payload)["status"], "verified")
        point["value"] = 0
        self.assertEqual(CENSUS.validate_profile_receipt(payload)["status"], "no_fresh_sample")

    def test_profile_receipt_refuses_ambiguous_series_shape_without_echoing_response(self):
        payload = {
            "target_id": "service:alpha",
            "start_ms": 1_000_000,
            "end_ms": 1_600_000,
            "freshness_seconds": 300,
            "response": {"series": [{"labels": [], "points": []}]},
        }
        result = CENSUS.validate_profile_receipt(payload)
        self.assertEqual(result, {"status": "invalid_response", "sample_count": 0})
        self.assertEqual(set(result), {"status", "sample_count"})

    def test_strict_loki_nanosecond_boundaries_are_exact_and_sibling_streams_refuse(self):
        def response(timestamp, streams=None):
            values = [{"stream": {"target_id": "service:alpha"}, "values": [[timestamp, "private log"]]}]
            if streams:
                values.extend(streams)
            return {"status": "success", "data": {"resultType": "streams", "result": values}}

        for timestamp in ("1000000000000", "1300000000000"):
            result = CENSUS.validate_signal_observation(observation("logs", response(timestamp)))
            self.assertEqual(result["status"], "verified")
        for timestamp in ("999999999999", "1300000000001"):
            result = CENSUS.validate_signal_observation(observation("logs", response(timestamp)))
            self.assertEqual(result["reason"], "timestamp_out_of_bounds")
        sibling = {"stream": {"target_id": "service:beta"}, "values": [["1300000000000", "private"]]}
        result = CENSUS.validate_signal_observation(observation("logs", response("1300000000000", [sibling])))
        self.assertEqual(result["reason"], "identity_mismatch")
        self.assertEqual(set(result), {"status", "reason", "evidence_count"})
        self.assertNotIn("private", json.dumps(result))
        crowded = response("1300000000000")
        crowded["data"]["result"][0]["values"] = [["1300000000000", "x"]] * 1001
        bounded = CENSUS.validate_signal_observation(observation("logs", crowded))
        self.assertEqual(bounded["evidence_count"], CENSUS.MAX_EVIDENCE_COUNT)

    def test_strict_prometheus_decimal_bounds_native_histograms_and_sibling_series(self):
        exact = prometheus(values=[["1000.000000000", "2"]])
        self.assertEqual(CENSUS.validate_signal_observation(observation("metrics", exact))["status"], "verified")
        outside = prometheus(values=[["1300.000000001", "2"]])
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("metrics", outside))["reason"],
            "timestamp_out_of_bounds",
        )
        histogram = prometheus(histograms=[[1300, {"count": "4", "sum": "8", "buckets": []}]])
        self.assertEqual(CENSUS.validate_signal_observation(observation("metrics", histogram))["status"], "verified")
        malformed = prometheus(histograms=[[1300, {}]])
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("metrics", malformed))["reason"],
            "invalid_response",
        )
        sibling = prometheus(values=[[1300, "1"]])
        sibling["data"]["result"].append({"metric": {"target_id": "service:beta"}, "values": [[1300, "2"]]})
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("metrics", sibling))["reason"],
            "identity_mismatch",
        )
        probe_only = prometheus(values=[[1300, "1"]])
        probe_only["data"]["result"][0]["metric"]["job"] = "o11y-availability"
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("metrics", probe_only))["reason"],
            "reserved_health_producer",
        )
        mixed = prometheus(values=[[1300, "1"]])
        mixed["data"]["result"].append({
            "metric": {"target_id": "service:alpha", "job": "o11y-availability"},
            "values": [[1300, "1"]],
        })
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("metrics", mixed))["reason"],
            "reserved_health_producer",
        )

    def test_strict_health_uses_latest_float_sample_and_requires_one(self):
        healthy = prometheus(values=[[1200, "0"], [1250, "1"]])
        self.assertEqual(CENSUS.validate_signal_observation(observation("health", healthy))["status"], "verified")
        unhealthy_latest = prometheus(values=[[1200, "1"], [1250, "0"]])
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("health", unhealthy_latest))["reason"],
            "unhealthy_latest_sample",
        )
        histogram_latest = prometheus(
            values=[[1200, "1"]],
            histograms=[[1250, {"count": "4", "sum": "8", "buckets": []}]],
        )
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("health", histogram_latest))["reason"],
            "unhealthy_latest_sample",
        )

    def test_strict_tempo_requires_a_target_child_span_and_rejects_metadata_only(self):
        valid = CENSUS.validate_signal_observation(observation("traces", tempo()))
        self.assertEqual(valid["status"], "verified")
        metadata_only = {"traces": [{"rootServiceName": "parent-service", "startTimeUnixNano": "1"}]}
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("traces", metadata_only))["reason"],
            "no_evidence",
        )
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("traces", tempo(target_id="service:beta")))["reason"],
            "identity_mismatch",
        )
        for timestamp in ("999999999999", "1300000000001"):
            self.assertEqual(
                CENSUS.validate_signal_observation(observation("traces", tempo(timestamp=timestamp)))["reason"],
                "timestamp_out_of_bounds",
            )

    def test_strict_pyroscope_requires_a_whole_fresh_bucket(self):
        def response(timestamp, value=1.0):
            return {"series": [{
                "labels": [{"name": "target_id", "value": "service:alpha"}],
                "points": [{"timestamp": timestamp, "value": value}],
            }]}

        for timestamp in (1_060_000, "1060001", 1_300_000):
            self.assertEqual(
                CENSUS.validate_signal_observation(observation("profiles", response(timestamp)))["status"],
                "verified",
            )
        too_early = CENSUS.validate_signal_observation(observation("profiles", response(1_059_999)))
        self.assertEqual(too_early["reason"], "no_evidence")
        too_late = CENSUS.validate_signal_observation(observation("profiles", response(1_300_001)))
        self.assertEqual(too_late["reason"], "timestamp_out_of_bounds")
        omitted_zero = {
            "series": [{
                "labels": [{"name": "target_id", "value": "service:alpha"}],
                "points": [
                    {"timestamp": 1_200_000},
                    {"timestamp": 1_260_000, "value": 1.0},
                ],
            }],
        }
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("profiles", omitted_zero)),
            {"status": "verified", "reason": "verified", "evidence_count": 1},
        )
        malformed_value = response(1_060_000, "not-a-number")
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("profiles", malformed_value))["reason"],
            "invalid_response",
        )
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("profiles", response(1_060_000, float("inf"))))["reason"],
            "malformed_json",
        )

    def test_strict_interval_freshness_expiry_transport_and_parser_refusals_are_sanitized(self):
        valid_response = prometheus(values=[[1300, "1"]])
        base = observation("metrics", valid_response)
        self.assertEqual(
            CENSUS.validate_signal_observation({**base, "observed_at": 1599})["status"],
            "verified",
        )
        self.assertEqual(
            CENSUS.validate_signal_observation({**base, "observed_at": 1600})["reason"],
            "invalid_interval",
        )
        self.assertEqual(
            CENSUS.validate_signal_observation({**base, "declared_freshness_seconds": 601})["reason"],
            "invalid_interval",
        )
        for changes in (
            {"window_seconds": 59},
            {"window_seconds": 3601},
            {"requested_freshness_seconds": 300, "declared_freshness_seconds": 300},
            {"requested_freshness_seconds": 86401, "declared_freshness_seconds": 86401},
            {"window_seconds": True},
        ):
            self.assertEqual(
                CENSUS.validate_signal_observation({**base, **changes})["reason"],
                "invalid_interval",
            )
        minimum_window = {
            **base,
            "query_start": 1000,
            "query_end": 1060,
            "window_seconds": 60,
            "observed_at": 1060,
            "requested_freshness_seconds": 61,
            "declared_freshness_seconds": 61,
            "response": prometheus(values=[[1060, "1"]]),
        }
        self.assertEqual(CENSUS.validate_signal_observation(minimum_window)["status"], "verified")
        self.assertEqual(
            CENSUS.validate_signal_observation({**base, "command_rc": 7})["reason"],
            "transport_error",
        )
        for raw in ('{"status":"success","status":"success"}', '{"x":NaN}'):
            result = CENSUS.validate_signal_observation(observation("metrics", raw))
            self.assertEqual(result["reason"], "malformed_json")
            self.assertNotIn(raw, json.dumps(result))
        large = " " * (CENSUS.MAX_OBSERVATION_BYTES + 1)
        self.assertEqual(
            CENSUS.validate_signal_observation(observation("metrics", large))["reason"],
            "oversized_response",
        )

    def test_deployed_missing_signal_is_incomplete(self):
        item = self.report([target()])["targets"][0]
        self.assertEqual(item["coverage"], "incomplete")
        self.assertEqual(item["signals"]["logs"], "incomplete")

    def test_sparse_deployed_signal_map_is_reported_as_undeclared(self):
        declaration = target()
        for signal_name in ("metrics", "health", "traces"):
            declaration["signals"].pop(signal_name)
            declaration["receipt_reference"].pop(signal_name)
        row = self.report([declaration])["targets"][0]
        self.assertEqual(row["coverage"], "incomplete")
        self.assertEqual(row["signals"]["health"], "undeclared")
        self.assertEqual(row["signals"]["metrics"], "undeclared")
        self.assertEqual(row["signals"]["traces"], "undeclared")

    def test_scaffold_is_unclassified_and_not_deployed(self):
        (self.repo / "platform/services/scaffold").mkdir()
        rows = self.report([])["targets"]
        scaffold = next(row for row in rows if row["service_identity"] == "scaffold")
        self.assertEqual(scaffold["lifecycle"], "unclassified")
        self.assertEqual(scaffold["coverage"], "unverified")

    def test_candidate_join_requires_matching_target_type(self):
        (self.repo / "platform/services/o11y").mkdir()
        declaration = target(target_id="vm:o11y", identity="o11y", target_type="vm")
        rows = self.report([declaration])["targets"]
        candidate = next(row for row in rows if row["source_candidate"] == "platform/services/o11y")
        declared_vm = next(row for row in rows if row["target_id"] == "vm:o11y")
        self.assertEqual(candidate["target_type"], "service")
        self.assertEqual(candidate["lifecycle"], "unclassified")
        self.assertIsNone(declared_vm["source_candidate"])
        self.assertEqual(declared_vm["target_type"], "vm")

    def test_sibling_signal_receipt_does_not_cover_missing_signal(self):
        deployed = target(signals={
            "logs": signal(receipt("service:alpha", "logs")),
            "metrics": signal(receipt("service:alpha", "logs"), reference="semaphore/task/456"),
        })
        row = self.report([deployed])["targets"][0]
        self.assertEqual(row["signals"], {
            "health": "excepted", "logs": "incomplete", "metrics": "incomplete", "traces": "excepted",
        })
        self.assertEqual(row["coverage"], "incomplete")

    def test_spoofed_task_reference_cannot_produce_verified_coverage(self):
        row = self.report([target(signals={"logs": signal(receipt())})])["targets"][0]
        self.assertEqual(row["signals"]["logs"], "incomplete")
        self.assertEqual(row["coverage"], "incomplete")

    def test_verified_receipts_cover_candidate_and_unmatched_declaration_paths(self):
        declarations = [target(), target("vm:beta", "beta", target_type="vm")]
        for declaration in declarations:
            declaration["signals"]["logs"].pop("receipt_reference")
            declaration["receipt_reference"].pop("logs")
        bindings = {
            declaration["target_id"]: {"logs": {
                "task_id": task_id, "template_id": 45, "observation_window_seconds": 300,
            }}
            for declaration, task_id in zip(declarations, (123, 124), strict=True)
        }
        revision = CENSUS.canonical_inventory_revision(declarations)
        accepted = {
            declaration["target_id"]: {"logs": {
                "status": "verified", "target_id": declaration["target_id"], "signal": "logs",
                "task_id": task_id, "template_id": 45, "repository_sha": REPO_SHA,
                "inventory_revision": revision, "observed_at": "2026-10-09T11:30:00Z",
                "expires_at": "2026-10-09T13:00:00Z",
            }}
            for declaration, task_id in zip(declarations, (123, 124), strict=True)
        }
        rows = self.report(declarations, bindings, accepted)["targets"]
        for target_id in ("service:alpha", "vm:beta"):
            row = next(item for item in rows if item["target_id"] == target_id)
            self.assertEqual(row["signals"]["logs"], "verified")
            self.assertEqual(row["coverage"], "verified")

    def test_expired_or_mismatched_verified_receipt_remains_incomplete(self):
        declaration = target()
        binding = {"service:alpha": {"logs": {
            "task_id": 123, "template_id": 45, "observation_window_seconds": 300,
        }}}
        revision = CENSUS.canonical_inventory_revision([declaration])
        accepted = {"service:alpha": {"logs": {
            "status": "verified", "target_id": "service:alpha", "signal": "logs",
            "task_id": 123, "template_id": 45, "repository_sha": REPO_SHA,
            "inventory_revision": revision, "observed_at": "2026-10-09T11:30:00Z",
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        }}}
        self.assertEqual(self.report([declaration], binding, accepted)["targets"][0]["signals"]["logs"],
                         "incomplete")
        accepted["service:alpha"]["logs"]["expires_at"] = "2026-10-09T13:00:00Z"
        accepted["service:alpha"]["logs"]["task_id"] = 999
        self.assertEqual(self.report([declaration], binding, accepted)["targets"][0]["signals"]["logs"],
                         "incomplete")

    def test_empty_verified_receipt_map_preserves_incomplete_compatibility(self):
        declaration = target()
        self.assertEqual(self.report([declaration])["targets"][0]["signals"]["logs"], "incomplete")
        self.assertEqual(self.report([declaration], verified_receipts={})["targets"][0]["signals"]["logs"],
                         "incomplete")

    def test_inventory_cannot_supply_an_accepted_receipt_map(self):
        declarations = [target()]
        inventory = {
            "inventory_revision": CENSUS.canonical_inventory_revision(declarations),
            "targets": declarations,
            "template_names": ["Verify o11y Target Receipt"],
            "verified_receipts": {"service:alpha": {"logs": receipt()}},
        }
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            CENSUS.build_report(self.repo, inventory, REPO_SHA, NOW)

    def test_inventory_revision_must_match_the_canonical_target_digest(self):
        declarations = [target()]
        revision = CENSUS.canonical_inventory_revision(declarations)
        self.assertEqual(len(revision), 64)
        self.assertEqual(revision, revision.lower())
        report = CENSUS.build_report(
            self.repo,
            {"inventory_revision": revision, "targets": declarations,
             "template_names": ["Verify o11y Target Receipt"]},
            REPO_SHA,
            NOW,
        )
        self.assertEqual(report["status"], "complete")
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            CENSUS.build_report(
                self.repo,
                {"inventory_revision": "b" * 64, "targets": declarations},
                REPO_SHA,
                NOW,
            )

    def test_canonical_digest_matches_wrapped_python_sha256_with_ascii_escaping(self):
        declarations = [{"service_identity": "雪", "target_id": "service:alpha"}]
        canonical = json.dumps(
            {"targets": declarations}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        expected = hashlib.sha256(canonical).hexdigest()
        self.assertEqual(CENSUS.canonical_inventory_revision(declarations), expected)

    def test_receipt_binding_edits_preserve_the_existing_target_digest(self):
        declarations = [target()]
        original = CENSUS.canonical_inventory_revision(declarations)
        bindings = {"service:alpha": {"logs": {
            "task_id": 123, "template_id": 45, "observation_window_seconds": 3600,
        }}}
        self.assertEqual(CENSUS.canonical_inventory_revision(declarations), original)
        self.assertEqual(self.report(declarations, bindings)["targets"][0]["signals"]["logs"], "incomplete")
        bindings["service:alpha"]["logs"]["task_id"] = 124
        self.assertEqual(CENSUS.canonical_inventory_revision(declarations), original)

    def test_signal_edits_still_invalidate_the_existing_target_digest(self):
        declarations = [target()]
        original = CENSUS.canonical_inventory_revision(declarations)
        declarations[0]["signals"]["logs"]["selector"] = '{target_id="service:changed"}'
        self.assertNotEqual(CENSUS.canonical_inventory_revision(declarations), original)

    def test_legacy_receipt_pointers_may_be_omitted_and_remain_incomplete(self):
        declaration = target()
        declaration["signals"]["logs"].pop("receipt_reference")
        declaration["receipt_reference"].pop("logs")
        row = self.report([declaration])["targets"][0]
        self.assertEqual(row["signals"]["logs"], "incomplete")

    def test_binding_is_accepted_without_legacy_pointers_and_remains_incomplete(self):
        declaration = target()
        declaration["signals"]["logs"].pop("receipt_reference")
        declaration["receipt_reference"].pop("logs")
        binding = {"service:alpha": {"logs": {
            "task_id": 123, "template_id": 45, "observation_window_seconds": 3600,
        }}}
        row = self.report([declaration], binding)["targets"][0]
        self.assertEqual(row["signals"]["logs"], "incomplete")

    def test_legacy_and_binding_task_ids_must_agree(self):
        bindings = {"service:alpha": {"logs": {
            "task_id": 124, "template_id": 45, "observation_window_seconds": 3600,
        }}}
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.report([target()], bindings)

    def test_partial_legacy_pointer_refuses(self):
        declaration = target()
        declaration["receipt_reference"].pop("logs")
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.report([declaration])

    def test_mismatched_legacy_pointers_refuse(self):
        declaration = target()
        declaration["receipt_reference"]["logs"] = "semaphore/task/456"
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.report([declaration])

    def test_malformed_receipt_bindings_refuse(self):
        invalid_bindings = (
            {"service:unknown": {"logs": {
                "task_id": 123, "template_id": 45, "observation_window_seconds": 3600,
            }}},
            {"service:alpha": {"unknown": {
                "task_id": 123, "template_id": 45, "observation_window_seconds": 3600,
            }}},
            {"service:alpha": {"logs": {
                "task_id": 0, "template_id": 45, "observation_window_seconds": 3600,
            }}},
            {"service:alpha": {"logs": {
                "task_id": 123, "template_id": 0, "observation_window_seconds": 3600,
            }}},
            {"service:alpha": {"logs": {
                "task_id": 123, "template_id": 45, "observation_window_seconds": -1,
            }}},
            {"service:alpha": {"logs": {
                "task_id": True, "template_id": 45, "observation_window_seconds": 3600,
            }}},
            {"service:alpha": {"logs": {
                "task_id": 123, "template_id": 45, "observation_window_seconds": 3600, "extra": 1,
            }}},
        )
        for bindings in invalid_bindings:
            with self.subTest(bindings=bindings), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                self.report([target()], bindings)

        inventory = {"targets": [target()], "receipt_bindings": None}
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            CENSUS.build_report(self.repo, inventory, REPO_SHA, NOW)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            json.loads('{"task_id":1,"task_id":2}', object_pairs_hook=CENSUS.unique_object)

    def test_duplicate_identity_is_reported_as_unverified_conflict(self):
        rows = self.report([target(), target("service:other", "alpha")])
        self.assertEqual(rows["duplicate_identity_count"], 1)
        self.assertEqual(rows["duplicate_identity_targets"], ["service:alpha", "service:other"])
        conflicts = [
            row for row in rows["targets"]
            if row.get("identity_conflict") and row["source_candidate"] is None
        ]
        self.assertEqual(len(conflicts), 2)
        self.assertTrue(all(row["coverage"] == "unverified" for row in conflicts))

    def test_duplicate_identity_across_target_types_keeps_candidate_unverified(self):
        rows = self.report([
            target(),
            target(target_id="vm:alpha", identity="alpha", target_type="vm"),
        ])
        candidate = next(row for row in rows["targets"] if row["source_candidate"] == "platform/services/alpha")
        declarations = [row for row in rows["targets"] if row["target_id"] in {"service:alpha", "vm:alpha"}]
        self.assertEqual(rows["duplicate_identity_count"], 1)
        self.assertTrue(candidate["identity_conflict"])
        self.assertEqual(candidate["coverage"], "unverified")
        self.assertEqual(len(declarations), 2)
        self.assertTrue(all(row["identity_conflict"] for row in declarations))
        self.assertTrue(all(row["coverage"] == "unverified" for row in declarations))

    def test_private_references_are_redacted_for_matched_unmatched_and_duplicate_rows(self):
        private = "private.example.test"
        matched = target()
        matched["inventory_host"] = "private-host.example"
        matched["endpoint"] = "192.0.2.10:9090"
        unmatched = target(target_id="vm:beta", identity="beta", target_type="vm")
        duplicate_service = target(target_id="service:gamma", identity="gamma")
        duplicate_vm = target(target_id="vm:gamma", identity="gamma", target_type="vm")
        for declaration in (matched, unmatched, duplicate_service, duplicate_vm):
            declaration["signals"]["health"]["receipt_reference"] = private
            declaration["receipt_reference"]["health"] = private
            declaration["template_references"] = [private]
        (self.repo / "platform/services/gamma").mkdir()

        report = self.report([matched, unmatched, duplicate_service, duplicate_vm])
        rendered = str(report)
        self.assertNotIn("private-host.example", rendered)
        self.assertNotIn("192.0.2.10", rendered)
        self.assertNotIn(private, rendered)
        self.assertTrue(all("receipt_references" not in row for row in report["targets"]))
        self.assertEqual(
            next(row for row in report["targets"] if row["target_id"] == "vm:beta")["source_candidate"],
            None,
        )
        self.assertTrue(any(row.get("identity_conflict") for row in report["targets"]))

    def test_missing_template_reference_keeps_target_incomplete(self):
        declaration = target()
        declaration["template_references"] = ["Missing Template"]
        declaration["signals"]["logs"] = signal(receipt("service:alpha", "logs"))
        row = self.report([declaration])["targets"][0]
        self.assertEqual(row["coverage"], "incomplete")
        self.assertEqual(row["template_references"]["missing_count"], 1)

if __name__ == "__main__":
    unittest.main()
