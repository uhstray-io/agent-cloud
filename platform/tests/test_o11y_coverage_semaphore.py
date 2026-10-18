import copy
import email.message
import importlib.util
import io
import json
import ssl
import subprocess
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "playbooks/files/o11y-coverage-semaphore.py"
SPEC = importlib.util.spec_from_file_location("o11y_coverage_semaphore", SCRIPT)
SEMAPHORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEMAPHORE)
CENSUS_SCRIPT = Path(__file__).parents[1] / "playbooks/files/o11y-coverage-census.py"
CENSUS_SPEC = importlib.util.spec_from_file_location("o11y_coverage_census", CENSUS_SCRIPT)
CENSUS = importlib.util.module_from_spec(CENSUS_SPEC)
CENSUS_SPEC.loader.exec_module(CENSUS)

PROJECT_ID = 7
TASK_ID = 123
TEMPLATE_ID = 45
REPOSITORY_ID = 8
REPOSITORY_SHA = "a" * 40
INVENTORY_REVISION = "b" * 64
NOW = "2026-10-11T12:00:30Z"


def fixture(inventory_revision=INVENTORY_REVISION, target_id="service:alpha"):
    selector = f'{{target_id="{target_id}"}}'
    target = {
        "target_id": target_id,
        "signals": {"logs": {
            "applicable": True,
            "selector": selector,
            "freshness_seconds": 3600,
        }},
    }
    binding = {"task_id": TASK_ID, "template_id": TEMPLATE_ID, "observation_window_seconds": 300}
    launch = {
        "o11y_verification_mode": "strict",
        "expected_repository_sha": REPOSITORY_SHA,
        "expected_inventory_revision": inventory_revision,
        "expected_target_id": target_id,
        "expected_signal": "logs",
        "signal_selector": selector,
        "freshness_seconds": 3600,
        "observation_window_seconds": 300,
    }
    task = {
        "id": TASK_ID,
        "project_id": PROJECT_ID,
        "template_id": TEMPLATE_ID,
        "status": "success",
        "commit_hash": REPOSITORY_SHA,
        "created": "2026-10-11T11:59:50Z",
        "start": "2026-10-11T12:00:00Z",
        "end": "2026-10-11T12:00:20Z",
        "playbook": "",
        "arguments": None,
        "git_branch": None,
        "environment": json.dumps(launch),
        "params": {"dry_run": False, "diff": False},
    }
    observation = {
        "status": "observed",
        "target_id": target_id,
        "signal": "logs",
        "repository_sha": REPOSITORY_SHA,
        "inventory_revision": inventory_revision,
        "observation_window_seconds": 300,
        "freshness_seconds": 3600,
        "query_ended_at": "2026-10-11T12:00:05Z",
        "observed_at": "2026-10-11T12:00:10Z",
        "task_reference_unattributed": True,
    }
    rows = [
        {"id": 901, "task_id": TASK_ID, "stage_id": None, "time": "2026-10-11T12:00:11Z", "output": "CUSTOM STATS:"},
        {"id": 902, "task_id": TASK_ID, "stage_id": None, "time": "2026-10-11T12:00:12Z",
         "output": "RUN: " + json.dumps({"o11y_coverage_observation": observation})},
    ]
    template = {
        "id": TEMPLATE_ID,
        "project_id": PROJECT_ID,
        "repository_id": REPOSITORY_ID,
        "inventory_id": 17,
        "name": "Verify o11y Target Receipt",
        "playbook": "platform/playbooks/verify-o11y-service.yml",
        "app": "ansible",
        "arguments": "",
        "allow_override_args_in_task": False,
        "git_branch": None,
    }
    repository = {
        "id": REPOSITORY_ID,
        "project_id": PROJECT_ID,
        "name": "agent-cloud dev",
        "git_url": "https://github.com/uhstray-io/agent-cloud.git",
        "git_branch": "dev",
    }
    return {
        "binding": binding, "target": target, "task": task, "rows": rows,
        "template": template, "repository": repository, "observation": observation,
    }


def accept(data, now=NOW):
    return SEMAPHORE.accept_receipt(
        binding=data["binding"], target=data["target"], signal="logs",
        project_id=PROJECT_ID, repository_id=REPOSITORY_ID,
        repository_sha=REPOSITORY_SHA, inventory_revision=INVENTORY_REVISION,
        task=data["task"], output_rows=data["rows"], template=data["template"],
        repository=data["repository"], now=now,
    )


def controller_payload():
    target_id = "service:census_test"
    target_data = fixture(target_id=target_id)
    declaration = {
        **target_data["target"],
        "target_type": "service",
        "lifecycle": "deployed",
        "owner": "platform",
        "runtime": "podman",
        "service_identity": "census_test",
        "environment": "production",
        "collection_method": "declared",
        "budget": {"samples_per_scrape": 1000},
        "template_references": ["Verify o11y Target Receipt"],
    }
    declaration["signals"]["logs"]["collection_method"] = "alloy"
    for signal_name in ("metrics", "health", "traces"):
        exception = {"reason": "not applicable in synthetic test", "reference": "review/123"}
        if signal_name == "traces":
            exception["alternative"] = "synthetic alternative"
        declaration["signals"][signal_name] = {"applicable": False, "exception": exception}
    targets = [declaration]
    inventory_revision = CENSUS.canonical_inventory_revision(targets)
    target_data = fixture(inventory_revision, target_id)
    inventory = {
        "inventory_revision": inventory_revision,
        "repository_sha": REPOSITORY_SHA,
        "targets": targets,
        "template_names": ["Verify o11y Target Receipt"],
        "receipt_bindings": {target_id: {"logs": target_data["binding"]}},
    }
    return {
        "semaphore_url": "https://semaphore.invalid",
        "api_token": "synthetic-secret",
        "project_id": PROJECT_ID,
        "repository_id": REPOSITORY_ID,
        "expected_repository_sha": REPOSITORY_SHA,
        "inventory": inventory,
    }, target_data


class FakeControllerReader:
    def __init__(self, _url, _token, _project_id, result, on_read=None):
        self.result = result
        self.on_read = on_read
        self.bindings = None

    def read_bindings(self, bindings):
        self.bindings = bindings
        if self.on_read:
            self.on_read()
        return self.result if isinstance(self.result, list) else [self.result]


class FakeResponse:
    def __init__(self, body, headers=None, status=200):
        self.status = status
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.fp = None
        self._body = memoryview(body)
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read1(self, size):
        end = min(self._offset + size, len(self._body))
        chunk = self._body[self._offset:end].tobytes()
        self._offset = end
        return chunk


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class SemaphoreReceiptTests(unittest.TestCase):
    def test_controller_accepts_transient_ids_for_multiple_stable_bindings(self):
        payload, logs = controller_payload()
        target = payload["inventory"]["targets"][0]
        target["signals"]["metrics"] = copy.deepcopy(target["signals"]["logs"])
        target["signals"]["metrics"]["selector"] = '{target_id="service:census_test"}'
        revision = CENSUS.canonical_inventory_revision(payload["inventory"]["targets"])
        payload["expected_repository_sha"] = REPOSITORY_SHA
        payload["inventory"]["inventory_revision"] = revision
        bindings = payload["inventory"]["receipt_bindings"]["service:census_test"]
        bindings["logs"].pop("task_id")
        bindings["metrics"] = {
            "template_id": TEMPLATE_ID, "observation_window_seconds": 300,
        }

        def result_for(signal, task_id):
            data = copy.deepcopy(logs)
            data["binding"]["task_id"] = task_id
            data["binding"]["template_id"] = TEMPLATE_ID
            data["target"] = target
            data["task"]["id"] = task_id
            data["task"]["environment"] = json.dumps({
                "o11y_verification_mode": "strict",
                "expected_repository_sha": REPOSITORY_SHA,
                "expected_inventory_revision": revision,
                "expected_target_id": "service:census_test",
                "expected_signal": signal,
                "signal_selector": target["signals"][signal]["selector"],
                "freshness_seconds": 3600,
                "observation_window_seconds": 300,
            })
            data["observation"]["signal"] = signal
            data["observation"]["inventory_revision"] = revision
            for row in data["rows"]:
                row["task_id"] = task_id
            data["rows"][-1]["output"] = "RUN: " + json.dumps({
                "o11y_coverage_observation": data["observation"],
            })
            return {"task": data["task"], "output": data["rows"],
                    "template": data["template"], "repository": data["repository"]}

        results = [result_for("logs", 123), result_for("metrics", 124)]
        reader_holder = {}

        def reader_factory(url, token, project_id):
            reader_holder["reader"] = FakeControllerReader(url, token, project_id, results)
            return reader_holder["reader"]

        payload["receipt_task_ids"] = json.dumps({
            "service:census_test": {"logs": 123, "metrics": 124},
        })
        report = SEMAPHORE.controller_report(
            payload, reader_factory=reader_factory, clock=lambda: NOW, census_module=CENSUS,
        )
        row = next(item for item in report["targets"] if item["target_id"] == "service:census_test")
        self.assertEqual(reader_holder["reader"].bindings, [
            {"task_id": 123, "template_id": TEMPLATE_ID},
            {"task_id": 124, "template_id": TEMPLATE_ID},
        ])
        self.assertEqual(row["signals"]["logs"], "verified")
        self.assertEqual(row["signals"]["metrics"], "verified")

    def test_controller_rejects_transient_id_reused_from_unoverridden_stable_binding(self):
        payload, _data = controller_payload()
        target = payload["inventory"]["targets"][0]
        target["signals"]["metrics"] = copy.deepcopy(target["signals"]["logs"])
        payload["inventory"]["receipt_bindings"]["service:census_test"]["metrics"] = {
            "task_id": TASK_ID + 1, "template_id": TEMPLATE_ID,
            "observation_window_seconds": 300,
        }
        payload["inventory"]["inventory_revision"] = CENSUS.canonical_inventory_revision(
            payload["inventory"]["targets"]
        )
        payload["receipt_task_ids"] = json.dumps({
            "service:census_test": {"logs": TASK_ID + 1},
        })
        reader_created = []

        def reader_factory(*_args):
            reader_created.append(True)
            raise AssertionError("reused transient ID must not reach receipt readback")

        with self.assertRaisesRegex(SEMAPHORE.Refusal, "transient_receipt_limit"):
            SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
        self.assertEqual(reader_created, [])

    def test_controller_rejects_legacy_pointer_override_before_readback(self):
        payload, _data = controller_payload()
        target = payload["inventory"]["targets"][0]
        target["signals"]["logs"]["receipt_reference"] = f"semaphore/task/{TASK_ID}"
        target["receipt_reference"] = {"logs": f"semaphore/task/{TASK_ID}"}
        payload["inventory"]["inventory_revision"] = CENSUS.canonical_inventory_revision(
            payload["inventory"]["targets"]
        )
        payload["receipt_task_ids"] = json.dumps({"service:census_test": {"logs": TASK_ID + 1}})
        reader_created = []

        def reader_factory(*_args):
            reader_created.append(True)
            raise AssertionError("legacy pointer override must not reach receipt readback")

        with self.assertRaisesRegex(SEMAPHORE.Refusal, "transient_receipt_legacy_pointer"):
            SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
        self.assertEqual(reader_created, [])

    def test_controller_rejects_malformed_or_forged_transient_ids_before_readback(self):
        invalid_overrides = (
            ('{"service:census_test":{"logs":123},"service:census_test":{"logs":124}}',
             "transient_receipt_shape"),
            ('{"service:other":{"logs":123}}', "transient_receipt_key"),
            ('{"service:census_test":{"traces":123}}', "transient_receipt_key"),
            ('{"service:census_test":{"logs":0}}', "transient_receipt_key"),
            ('{"service:census_test":{"logs":123,"metrics":123}}', "transient_receipt_key"),
            ('not-json', "transient_receipt_shape"),
        )
        for override, reason in invalid_overrides:
            payload, _data = controller_payload()
            payload["receipt_task_ids"] = override
            reader_created = []

            def reader_factory(*_args, calls=reader_created):
                calls.append(True)
                raise AssertionError("invalid transient IDs must not reach receipt readback")

            with self.subTest(override=override), self.assertRaisesRegex(SEMAPHORE.Refusal, reason):
                SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
            self.assertEqual(reader_created, [])

    def test_controller_refuses_reused_and_oversized_transient_id_maps(self):
        payload, _data = controller_payload()
        target = payload["inventory"]["targets"][0]
        target["signals"]["metrics"] = copy.deepcopy(target["signals"]["logs"])
        payload["inventory"]["receipt_bindings"]["service:census_test"]["metrics"] = {
            "template_id": TEMPLATE_ID, "observation_window_seconds": 300,
        }
        payload["inventory"]["inventory_revision"] = CENSUS.canonical_inventory_revision(
            payload["inventory"]["targets"]
        )
        payload["receipt_task_ids"] = json.dumps({
            "service:census_test": {"logs": 123, "metrics": 123},
        })
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "transient_receipt_limit"):
            SEMAPHORE.controller_report(payload, census_module=CENSUS)

        oversized, _data = controller_payload()
        targets, bindings, override = [], {}, {}
        for index in range(SEMAPHORE.MAX_TRANSIENT_TASK_IDS + 1):
            suffix = f"alpha{index:02}"
            target = copy.deepcopy(oversized["inventory"]["targets"][0])
            target["target_id"] = f"service:{suffix}"
            target["service_identity"] = suffix
            target["signals"]["logs"]["selector"] = f'{{target_id="service:{suffix}"}}'
            targets.append(target)
            bindings[target["target_id"]] = {"logs": {
                "template_id": TEMPLATE_ID, "observation_window_seconds": 300,
            }}
            override[target["target_id"]] = {"logs": 1000 + index}
        oversized["inventory"]["targets"] = targets
        oversized["inventory"]["receipt_bindings"] = bindings
        oversized["inventory"]["inventory_revision"] = CENSUS.canonical_inventory_revision(targets)
        oversized["receipt_task_ids"] = json.dumps(override)
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "transient_receipt_limit"):
            SEMAPHORE.controller_report(oversized, census_module=CENSUS)

    def test_controller_leaves_stable_binding_without_task_id_incomplete(self):
        payload, _data = controller_payload()
        binding = payload["inventory"]["receipt_bindings"]["service:census_test"]["logs"]
        binding.pop("task_id")
        reader_created = []

        def reader_factory(*_args):
            reader_created.append(True)
            raise AssertionError("missing task ID must not trigger a readback")

        report = SEMAPHORE.controller_report(
            payload, reader_factory=reader_factory, clock=lambda: NOW, census_module=CENSUS,
        )
        row = next(item for item in report["targets"] if item["target_id"] == "service:census_test")
        self.assertEqual(row["signals"]["logs"], "incomplete")
        self.assertEqual(reader_created, [])

    def test_controller_builds_census_without_reader_for_empty_bindings(self):
        payload, _data = controller_payload()
        payload["inventory"]["receipt_bindings"] = {}
        payload.pop("project_id")
        payload.pop("repository_id")
        payload["semaphore_url"] = ""
        payload.pop("api_token")

        def reader_factory(*_args):
            raise AssertionError("reader must not be created without receipt bindings")

        report = SEMAPHORE.controller_report(
            payload, reader_factory=reader_factory, clock=lambda: NOW, census_module=CENSUS,
        )
        row = next(item for item in report["targets"] if item["target_id"] == "service:census_test")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(row["signals"]["logs"], "incomplete")
        self.assertEqual(row["coverage"], "incomplete")

    def test_controller_preserves_inventory_missing_without_receipt_bindings(self):
        payload, _data = controller_payload()
        payload["inventory"]["receipt_bindings"] = {}
        payload["inventory"].pop("inventory_revision")

        def reader_factory(*_args):
            raise AssertionError("reader must not be created without receipt bindings")

        report = SEMAPHORE.controller_report(
            payload, reader_factory=reader_factory, clock=lambda: NOW, census_module=CENSUS,
        )
        self.assertEqual(report["status"], "inventory_missing")

    def test_controller_rejects_invalid_supplied_digest_without_bindings(self):
        for digest in (None, "invalid", "c" * 64):
            payload, _data = controller_payload()
            payload["inventory"]["receipt_bindings"] = {}
            payload["inventory"]["inventory_revision"] = digest
            with self.subTest(digest=digest), self.assertRaisesRegex(
                SEMAPHORE.Refusal, "invalid_controller_input"
            ):
                SEMAPHORE.controller_report(payload, census_module=CENSUS)

    def test_controller_requires_digest_for_receipt_bindings(self):
        payload, _data = controller_payload()
        payload["inventory"].pop("inventory_revision")
        reader_created = []

        def reader_factory(*_args):
            reader_created.append(True)
            raise AssertionError("reader must not be created without a bound digest")

        with self.assertRaisesRegex(SEMAPHORE.Refusal, "invalid_controller_input"):
            SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
        self.assertEqual(reader_created, [])

    def test_controller_refuses_malformed_targets_and_bindings_before_reader_creation(self):
        for mutation in (
            lambda inventory: inventory["targets"][0].pop("target_type"),
            lambda inventory: inventory.update(receipt_bindings={"service:census_test": {"logs": {}}}),
        ):
            payload, _data = controller_payload()
            payload["inventory"].pop("inventory_revision")
            mutation(payload["inventory"])
            reader_created = []

            def reader_factory(*_args, calls=reader_created):
                calls.append(True)
                raise AssertionError("reader must not be created for malformed inventory")

            with self.assertRaises((SEMAPHORE.Refusal, SystemExit)):
                SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
            self.assertEqual(reader_created, [])

    def test_controller_refuses_bound_path_missing_token_before_reader_creation(self):
        payload, _data = controller_payload()
        payload.pop("api_token")
        reader_created = []

        def reader_factory(*_args):
            reader_created.append(True)
            raise AssertionError("reader must not be created without a token")

        with self.assertRaisesRegex(SEMAPHORE.Refusal, "invalid_credentials"):
            SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
        self.assertEqual(reader_created, [])

    def test_controller_reads_accepts_and_reports_only_verified_attribution(self):
        payload, data = controller_payload()
        reader_holder = {}

        def reader_factory(url, token, project_id):
            reader_holder["reader"] = FakeControllerReader(
                url, token, project_id,
                {"task": data["task"], "output": data["rows"],
                 "template": data["template"], "repository": data["repository"]},
            )
            return reader_holder["reader"]

        report = SEMAPHORE.controller_report(payload, reader_factory=reader_factory, clock=lambda: NOW,
                                              census_module=CENSUS)
        self.assertEqual(reader_holder["reader"].bindings, [{"task_id": TASK_ID, "template_id": TEMPLATE_ID}])
        row = next(item for item in report["targets"] if item["target_id"] == "service:census_test")
        self.assertEqual(row["signals"]["logs"], "verified")
        self.assertEqual(row["coverage"], "verified")
        rendered = json.dumps(report)
        self.assertNotIn("synthetic-secret", rendered)
        self.assertNotIn("semaphore.invalid", rendered)
        self.assertNotIn("o11y_coverage_observation", rendered)

    def test_controller_reports_authenticated_stale_head_as_incomplete(self):
        payload, data = controller_payload()
        old_sha = "c" * 40
        data["task"]["commit_hash"] = old_sha
        launch = json.loads(data["task"]["environment"])
        launch["expected_repository_sha"] = old_sha
        data["task"]["environment"] = json.dumps(launch)
        data["observation"]["repository_sha"] = old_sha
        data["rows"][-1]["output"] = "RUN: " + json.dumps({
            "o11y_coverage_observation": data["observation"],
        })

        def reader_factory(url, token, project_id):
            return FakeControllerReader(
                url, token, project_id,
                {"task": data["task"], "output": data["rows"],
                 "template": data["template"], "repository": data["repository"]},
            )

        report = SEMAPHORE.controller_report(
            payload, reader_factory=reader_factory, clock=lambda: NOW, census_module=CENSUS,
        )
        row = next(item for item in report["targets"] if item["target_id"] == "service:census_test")
        self.assertEqual(row["signals"]["logs"], "incomplete")
        self.assertEqual(row["coverage"], "incomplete")

    def test_stale_receipt_still_refuses_current_inventory_or_selector_mismatch(self):
        old_sha = "c" * 40
        for field, value in (
            ("expected_inventory_revision", "d" * 64),
            ("signal_selector", '{target_id="service:other"}'),
        ):
            data = fixture()
            data["task"]["commit_hash"] = old_sha
            launch = json.loads(data["task"]["environment"])
            launch["expected_repository_sha"] = old_sha
            launch[field] = value
            data["task"]["environment"] = json.dumps(launch)
            data["observation"]["repository_sha"] = old_sha
            data["rows"][-1]["output"] = "RUN: " + json.dumps({
                "o11y_coverage_observation": data["observation"],
            })
            with self.subTest(field=field), self.assertRaisesRegex(SEMAPHORE.Refusal, "launch_inputs"):
                accept(data)

    def test_controller_refuses_wrong_binding_after_readback(self):
        payload, data = controller_payload()
        payload["inventory"]["receipt_bindings"]["service:census_test"]["logs"]["task_id"] += 1
        reader_holder = {}

        def reader_factory(url, token, project_id):
            reader_holder["reader"] = FakeControllerReader(
                url, token, project_id,
                {"task": data["task"], "output": data["rows"],
                 "template": data["template"], "repository": data["repository"]},
            )
            return reader_holder["reader"]

        with self.assertRaisesRegex(SEMAPHORE.Refusal, "task_identity_mismatch"):
            SEMAPHORE.controller_report(payload, reader_factory=reader_factory, clock=lambda: NOW,
                                        census_module=CENSUS)
        self.assertEqual(reader_holder["reader"].bindings, [{"task_id": TASK_ID + 1, "template_id": TEMPLATE_ID}])

    def test_controller_refuses_receipt_expired_by_readback_completion(self):
        payload, data = controller_payload()

        def reader_factory(url, token, project_id):
            return FakeControllerReader(
                url, token, project_id,
                {"task": data["task"], "output": data["rows"],
                 "template": data["template"], "repository": data["repository"]},
            )

        report = SEMAPHORE.controller_report(
            payload, reader_factory=reader_factory, clock=lambda: "2026-10-11T12:55:05Z",
            census_module=CENSUS,
        )
        row = next(item for item in report["targets"] if item["target_id"] == "service:census_test")
        self.assertEqual(row["signals"]["logs"], "incomplete")
        self.assertEqual(row["coverage"], "incomplete")

    def test_controller_rejects_supplied_inventory_evidence_before_reader_creation(self):
        payload, _data = controller_payload()
        payload["inventory"]["verified_receipts"] = {"service:census_test": {"logs": {"status": "verified"}}}
        reader_created = []

        def reader_factory(*_args):
            reader_created.append(True)
            raise AssertionError("reader must not be created for forged inventory evidence")

        with self.assertRaisesRegex(SEMAPHORE.Refusal, "invalid_controller_input"):
            SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
        self.assertEqual(reader_created, [])

    def test_controller_requires_positive_private_ids_before_reader_creation(self):
        for field in ("project_id", "repository_id"):
            for value in (None, 0, True):
                payload, _data = controller_payload()
                if value is None:
                    payload.pop(field)
                else:
                    payload[field] = value
                reader_created = []

                def reader_factory(*_args, calls=reader_created):
                    calls.append(True)
                    raise AssertionError("reader must not be created with an invalid private ID")

                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    SEMAPHORE.Refusal, "invalid_controller_input"
                ):
                    SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
                self.assertEqual(reader_created, [])

    def test_controller_rejects_invalid_window_before_reader_creation(self):
        payload, _data = controller_payload()
        payload["inventory"]["receipt_bindings"]["service:census_test"]["logs"][
            "observation_window_seconds"
        ] = 3600
        reader_created = []

        def reader_factory(*_args):
            reader_created.append(True)
            raise AssertionError("reader must not be created with an invalid binding window")

        with self.assertRaisesRegex(SEMAPHORE.Refusal, "invalid_binding"):
            SEMAPHORE.controller_report(payload, reader_factory=reader_factory, census_module=CENSUS)
        self.assertEqual(reader_created, [])

    def test_controller_cli_emits_only_fixed_refusal_category(self):
        payload = {
            "semaphore_url": "https://private.invalid/path?credential=private-url",
            "api_token": "private-token",
            "project_id": 0,
            "repository_id": REPOSITORY_ID,
            "expected_repository_sha": REPOSITORY_SHA,
            "inventory": {},
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--controller"],
            input=json.dumps(payload), text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '{"reason": "invalid_controller_input", "status": "refused"}\n')
        self.assertEqual(result.stderr, "")
        self.assertNotIn("private-token", result.stdout + result.stderr)
        self.assertNotIn("private.invalid", result.stdout + result.stderr)

    def test_main_emits_each_fixed_refusal_code_and_hides_unknown_failures(self):
        self.assertEqual(SEMAPHORE.SAFE_REFUSAL_CODES, {
            "binding_limit", "conflicting_stats", "deadline", "http_status", "invalid_binding",
            "invalid_controller_input", "invalid_credentials", "invalid_declaration", "invalid_endpoint",
            "invalid_template_repository", "invalid_utc_time", "launch_inputs", "malformed_json",
            "observation_binding", "observation_shape", "observation_time_order",
            "output_rows", "output_shape", "output_time_order", "readback_shape", "repository_mismatch",
            "repository_revision_mismatch", "response_encoding", "response_framing", "response_size",
            "route_refused", "task_drift", "task_identity_mismatch", "task_inventory_override",
            "task_not_accepted", "task_override", "task_params", "task_time_order", "template_drift",
            "template_mismatch", "terminal_stats", "transient_receipt_key", "transient_receipt_legacy_pointer",
            "transient_receipt_limit", "transient_receipt_shape", "transient_receipt_size",
            "transport", "truncated_response",
        })

        def run_with(error):
            output = io.StringIO()
            with patch.object(SEMAPHORE.sys, "argv", [str(SCRIPT), "--controller"]), \
                    patch.object(SEMAPHORE.sys, "stdin", io.StringIO("{}")), \
                    patch.object(SEMAPHORE, "controller_report", side_effect=error), \
                    redirect_stdout(output):
                result = SEMAPHORE.main()
            return result, output.getvalue()

        for code in sorted(SEMAPHORE.SAFE_REFUSAL_CODES):
            with self.subTest(code=code):
                result, output = run_with(SEMAPHORE.Refusal(code))
                self.assertEqual(result, 2)
                self.assertEqual(json.loads(output), {"status": "refused", "reason": code})

        for error in (
            SEMAPHORE.Refusal("private-token"), SEMAPHORE.Refusal(["private-token"]),
            RuntimeError("private-url private-token"), SystemExit("private-url private-token"),
        ):
            with self.subTest(error=type(error).__name__):
                result, output = run_with(error)
                self.assertEqual(result, 2)
                self.assertEqual(json.loads(output), {
                    "status": "refused", "reason": "receipt_verification_failed",
                })
                self.assertNotIn("private-token", output)
                self.assertNotIn("private-url", output)

    def test_synthetic_receipt_is_accepted_with_bounded_attribution(self):
        result = accept(fixture())
        self.assertEqual(result, {
            "status": "verified", "target_id": "service:alpha", "signal": "logs",
            "task_id": TASK_ID, "template_id": TEMPLATE_ID, "repository_sha": REPOSITORY_SHA,
            "inventory_revision": INVENTORY_REVISION, "observed_at": "2026-10-11T12:00:10Z",
            "expires_at": "2026-10-11T12:55:05Z",
        })
        self.assertNotIn("selector", result)
        self.assertNotIn("output", result)

    def test_task_inventory_inherits_valid_template_inventory(self):
        for value in ("missing", None):
            data = fixture()
            data["template"]["inventory_id"] = 17
            if value is None:
                data["task"]["inventory_id"] = None
            with self.subTest(task_inventory=value):
                self.assertEqual(accept(data)["status"], "verified")

    def test_task_inventory_rejects_inheritance_from_invalid_template_inventory(self):
        for invalid_inventory in (None, 0, True, "17"):
            data = fixture()
            data["template"]["inventory_id"] = invalid_inventory
            with self.subTest(inventory_id=invalid_inventory), self.assertRaisesRegex(
                SEMAPHORE.Refusal, "task_inventory_override"
            ):
                accept(data)

    def test_expiry_is_conservative_and_expires_at_equality(self):
        self.assertEqual(accept(fixture(), "2026-10-11T12:55:04Z")["status"], "verified")
        self.assertEqual(accept(fixture(), "2026-10-11T12:55:05Z")["status"], "incomplete")

    def test_task_identity_status_and_revision_are_exact(self):
        for key, value, code in (
            ("id", TASK_ID + 1, "task_identity_mismatch"),
            ("project_id", PROJECT_ID + 1, "task_identity_mismatch"),
            ("template_id", TEMPLATE_ID + 1, "task_not_accepted"),
            ("status", "running", "task_not_accepted"),
            ("commit_hash", "c" * 40, "launch_inputs"),
            ("commit_hash", "invalid", "repository_revision_mismatch"),
            ("playbook", "other.yml", "task_override"),
            ("arguments", "--limit all", "task_override"),
            ("git_branch", "main", "task_override"),
            ("limit", "all", "task_override"),
            ("schedule_id", 1, "task_override"),
            ("params", {"dry_run": True, "diff": True}, "task_params"),
            ("params", {"dry_run": False, "diff": False, "limit": ["all"]}, "task_params"),
        ):
            with self.subTest(key=key, value=value):
                data = fixture()
                data["task"][key] = value
                with self.assertRaisesRegex(SEMAPHORE.Refusal, code):
                    accept(data)

    def test_launch_inputs_are_exact_and_duplicate_keys_refuse(self):
        variants = (
            json.dumps({**json.loads(fixture()["task"]["environment"]), "extra": "x"}),
            json.dumps({**json.loads(fixture()["task"]["environment"]), "freshness_seconds": 3601}),
            '{"o11y_verification_mode":"strict","o11y_verification_mode":"legacy"}',
        )
        for environment in variants:
            data = fixture()
            data["task"]["environment"] = environment
            with self.assertRaisesRegex(SEMAPHORE.Refusal, "launch_inputs"):
                accept(data)

    def test_template_repository_and_observation_bindings_are_exact(self):
        for key, value in (("allow_override_args_in_task", True), ("repository_id", 99),
                           ("playbook", "platform/playbooks/other.yml"), ("task_params", {"limit": ["all"]})):
            data = fixture()
            data["template"][key] = value
            with self.subTest(template_key=key), self.assertRaisesRegex(SEMAPHORE.Refusal, "template_mismatch"):
                accept(data)
        data = fixture()
        data["repository"]["git_branch"] = "main"
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "repository_mismatch"):
            accept(data)
        data = fixture()
        data["task"]["inventory_id"] = 99
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "task_inventory_override"):
            accept(data)
        data = fixture()
        data["task"]["inventory_id"] = 17
        self.assertEqual(accept(data)["status"], "verified")
        data = fixture()
        data["observation"]["signal"] = "metrics"
        data["rows"][-1]["output"] = "RUN: " + json.dumps({"o11y_coverage_observation": data["observation"]})
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "observation_binding"):
            accept(data)

    def test_output_rows_require_exact_task_order_and_one_terminal_stats_object(self):
        mutations = (
            lambda rows: rows.__setitem__(1, {**rows[1], "task_id": TASK_ID + 1}),
            lambda rows: rows.__setitem__(1, {**rows[1], "id": rows[0]["id"]}),
            lambda rows: rows.insert(0, {**rows[0], "id": 900, "output": "RUN: forged"}),
            lambda rows: rows.append({**rows[-1], "id": 903}),
            lambda rows: rows.__setitem__(1, {**rows[1], "output": "RUN: {"}),
            lambda rows: rows.__setitem__(
                1, {**rows[1], "output": 'RUN: {"o11y_coverage_observation":{},'
                    '"o11y_coverage_observation":{}}'}
            ),
            lambda rows: rows.__setitem__(0, {**rows[0], "output": "ordinary output"}),
            lambda rows: rows.insert(1, {**rows[0], "id": 902, "time": "2026-10-11T12:00:11Z"}),
            lambda rows: rows.__setitem__(1, {**rows[1], "time": "2026-10-11T12:00:10Z"}),
        )
        for mutate in mutations:
            data = fixture()
            mutate(data["rows"])
            with self.assertRaises(SEMAPHORE.Refusal):
                accept(data)

    def test_output_rows_accept_all_zero_ids_but_reject_mixed_or_invalid_ids(self):
        data = fixture()
        for row in data["rows"]:
            row["id"] = 0
        self.assertEqual(accept(data)["status"], "verified")

        for ids in ((0, 902), (901, 0), (-1, 902), (True, 902), ("901", 902),
                    (901, 901), (902, 901)):
            data = fixture()
            for row, row_id in zip(data["rows"], ids, strict=True):
                row["id"] = row_id
            with self.subTest(ids=ids), self.assertRaisesRegex(SEMAPHORE.Refusal, "^output_rows$"):
                accept(data)

    def test_preliminary_queue_output_may_precede_task_start(self):
        data = fixture()
        data["rows"].insert(0, {
            "id": 900, "task_id": TASK_ID, "stage_id": None,
            "time": "2026-10-11T11:59:55Z", "output": "task queued",
        })
        self.assertEqual(accept(data)["status"], "verified")

    def test_callback_banner_stars_tabbed_run_and_blank_rows_are_normalized(self):
        data = fixture()
        data["rows"][0]["output"] = "CUSTOM STATS: ********"
        data["rows"][1]["output"] = "\t" + data["rows"][1]["output"]
        data["rows"].insert(0, {
            "id": 900, "task_id": TASK_ID, "stage_id": None,
            "time": "2026-10-11T11:59:55Z", "output": "\t  ",
        })
        self.assertEqual(accept(data)["status"], "verified")

    def test_callback_run_with_more_than_one_leading_tab_is_refused(self):
        data = fixture()
        data["rows"][-1]["output"] = "\t\t" + data["rows"][-1]["output"]
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "terminal_stats"):
            accept(data)

    def test_terminal_rows_must_be_inside_execution_interval(self):
        for timestamp in ("2026-10-11T11:59:55Z", "2026-10-11T12:00:21Z"):
            data = fixture()
            terminal_base = SEMAPHORE._utc(timestamp)
            data["rows"][-2]["time"] = timestamp
            data["rows"][-1]["time"] = (terminal_base + SEMAPHORE.timedelta(seconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(
                SEMAPHORE.Refusal, "output_time_order"
            ):
                accept(data)

    def test_times_must_be_utc_ordered_and_inside_task(self):
        self.assertEqual(SEMAPHORE._utc("2026-10-11T12:00:05.123456789Z").microsecond, 123456)
        for field, value in (("query_ended_at", "2026-10-11T12:00:21Z"),
                             ("observed_at", "2026-10-11T11:59:59Z"),
                             ("query_ended_at", "2026-10-11T12:00:05-04:00"),
                             ("query_ended_at", "not-a-time")):
            data = fixture()
            data["observation"][field] = value
            data["rows"][-1]["output"] = "RUN: " + json.dumps({"o11y_coverage_observation": data["observation"]})
            with self.subTest(field=field, value=value), self.assertRaises(SEMAPHORE.Refusal):
                accept(data)

    def test_endpoint_is_https_origin_and_reader_rejects_redirect_tls_and_status(self):
        for url in ("http://semaphore.invalid", "https://user@semaphore.invalid", "https://semaphore.invalid/api"):
            with self.subTest(url=url), self.assertRaises(SEMAPHORE.Refusal):
                SEMAPHORE.SemaphoreReader(url, "fake-secret", PROJECT_ID)
        for response, code in (
            (urllib.error.HTTPError("https://semaphore.invalid", 302, "redirect", {}, None), "http_status"),
            (urllib.error.HTTPError("https://semaphore.invalid", 401, "unauthorized", {}, None), "http_status"),
            (ssl.SSLError("certificate verification failed"), "transport"),
        ):
            reader = SEMAPHORE.SemaphoreReader(
                "https://semaphore.invalid", "fake-secret", PROJECT_ID,
                opener=FakeOpener([response]),
            )
            with self.assertRaisesRegex(SEMAPHORE.Refusal, code):
                reader._get("/api/project/7/tasks/123", SEMAPHORE.MAX_METADATA_BYTES, 10**30)

    def test_endpoint_rejects_empty_userinfo(self):
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "invalid_endpoint"):
            SEMAPHORE.SemaphoreReader("https://@semaphore.invalid", "fake-secret", PROJECT_ID)

    def test_http_body_is_bounded_and_chunked_transfer_is_checked(self):
        too_large = FakeResponse(b"{}", {"Content-Length": str(SEMAPHORE.MAX_METADATA_BYTES + 1)})
        reader = SEMAPHORE.SemaphoreReader(
            "https://semaphore.invalid", "fake-secret", PROJECT_ID, opener=FakeOpener([too_large])
        )
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "response_size"):
            reader._get("/api/project/7/tasks/123", SEMAPHORE.MAX_METADATA_BYTES, 10**30)

        chunked = FakeResponse(b"{}", {"Transfer-Encoding": "chunked"})
        reader = SEMAPHORE.SemaphoreReader(
            "https://semaphore.invalid", "fake-secret", PROJECT_ID, opener=FakeOpener([chunked])
        )
        self.assertEqual(reader._get("/api/project/7/tasks/123", SEMAPHORE.MAX_METADATA_BYTES, 10**30), {})
        for headers, limit, code in (
            ({"Transfer-Encoding": "chunked"}, 1, "response_size"),
            ({"Transfer-Encoding": "chunked", "Content-Length": "2"}, 2, "response_framing"),
            ({"Transfer-Encoding": "gzip"}, 2, "response_framing"),
        ):
            reader = SEMAPHORE.SemaphoreReader(
                "https://semaphore.invalid", "fake-secret", PROJECT_ID,
                opener=FakeOpener([FakeResponse(b"{}", headers)]),
            )
            with self.subTest(headers=headers), self.assertRaisesRegex(SEMAPHORE.Refusal, code):
                reader._get("/api/project/7/tasks/123", limit, 10**30)

    def test_default_transport_has_verified_tls_no_proxy_and_no_redirects(self):
        reader = SEMAPHORE.SemaphoreReader("https://semaphore.invalid", "fake-secret", PROJECT_ID)
        https = next(
            handler for handler in reader.opener.handlers
            if isinstance(handler, SEMAPHORE.urllib.request.HTTPSHandler)
        )
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(https._context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertFalse(any(isinstance(handler, SEMAPHORE.urllib.request.ProxyHandler)
                             for handler in reader.opener.handlers))
        self.assertTrue(any(isinstance(handler, SEMAPHORE.NoRedirect) for handler in reader.opener.handlers))
        self.assertFalse(any(isinstance(handler, (SEMAPHORE.urllib.request.HTTPBasicAuthHandler,
                                                  SEMAPHORE.urllib.request.HTTPDigestAuthHandler))
                             for handler in reader.opener.handlers))

    def test_http_rejects_malformed_duplicate_truncated_and_encoded_responses(self):
        for body, headers, code in (
            (b"{", {}, "malformed_json"),
            (b'{"id":1,"id":2}', {}, "malformed_json"),
            (b'{"id":NaN}', {}, "malformed_json"),
            (b"{}", {"Content-Length": "3"}, "truncated_response"),
            (b"{}", {"Content-Encoding": "gzip"}, "response_encoding"),
        ):
            reader = SEMAPHORE.SemaphoreReader(
                "https://semaphore.invalid", "fake-secret", PROJECT_ID,
                opener=FakeOpener([FakeResponse(body, headers)]),
            )
            with self.subTest(code=code), self.assertRaisesRegex(SEMAPHORE.Refusal, code):
                reader._get("/api/project/7/tasks/123", SEMAPHORE.MAX_METADATA_BYTES, 10**30)

    def test_declared_reads_are_get_only_deduplicated_and_rechecked(self):
        metadata = {"id": TASK_ID, "project_id": PROJECT_ID, "template_id": TEMPLATE_ID}
        template = {"id": TEMPLATE_ID, "project_id": PROJECT_ID, "repository_id": REPOSITORY_ID}
        repository = {"id": REPOSITORY_ID, "project_id": PROJECT_ID}
        output = []
        responses = [
            FakeResponse(json.dumps(metadata).encode()), FakeResponse(json.dumps(output).encode()),
            FakeResponse(json.dumps(template).encode()), FakeResponse(json.dumps(repository).encode()),
            FakeResponse(json.dumps(metadata).encode()), FakeResponse(json.dumps(template).encode()),
        ]
        opener = FakeOpener(responses)
        reader = SEMAPHORE.SemaphoreReader("https://semaphore.invalid", "fake-secret", PROJECT_ID, opener=opener)
        result = reader.read_bindings([
            {"task_id": TASK_ID, "template_id": TEMPLATE_ID},
            {"task_id": TASK_ID, "template_id": TEMPLATE_ID},
        ])
        self.assertEqual(len(result), 2)
        self.assertEqual(len(opener.requests), 6)
        self.assertEqual([request.full_url.rsplit("/", 1)[-1] for request, _ in opener.requests],
                         ["123", "output", "45", "8", "123", "45"])
        self.assertTrue(all(request.get_method() == "GET" for request, _ in opener.requests))
        self.assertTrue(all(request.get_header("Authorization") == "Bearer fake-secret"
                            for request, _ in opener.requests))

    def test_template_repository_id_must_be_present_and_positive(self):
        task = {"id": TASK_ID, "project_id": PROJECT_ID, "template_id": TEMPLATE_ID}
        for template in (
            {"id": TEMPLATE_ID, "project_id": PROJECT_ID},
            {"id": TEMPLATE_ID, "project_id": PROJECT_ID, "repository_id": 0},
            {"id": TEMPLATE_ID, "project_id": PROJECT_ID, "repository_id": True},
            {"id": TEMPLATE_ID, "project_id": PROJECT_ID, "repository_id": "8"},
        ):
            opener = FakeOpener([FakeResponse(json.dumps(value).encode()) for value in (task, [], template)])
            reader = SEMAPHORE.SemaphoreReader("https://semaphore.invalid", "fake-secret", PROJECT_ID, opener=opener)
            with self.subTest(template=template), self.assertRaisesRegex(
                SEMAPHORE.Refusal, "invalid_template_repository"
            ):
                reader.read_bindings([{"task_id": TASK_ID, "template_id": TEMPLATE_ID}])
            self.assertEqual(len(opener.requests), 3)

    def test_declared_metadata_drift_refuses(self):
        task = {"id": TASK_ID, "project_id": PROJECT_ID, "template_id": TEMPLATE_ID}
        changed = {**task, "status": "success"}
        template = {"id": TEMPLATE_ID, "project_id": PROJECT_ID, "repository_id": REPOSITORY_ID}
        repository = {"id": REPOSITORY_ID, "project_id": PROJECT_ID}
        opener = FakeOpener([FakeResponse(json.dumps(value).encode()) for value in
                             (task, [], template, repository, changed, template)])
        reader = SEMAPHORE.SemaphoreReader("https://semaphore.invalid", "fake-secret", PROJECT_ID, opener=opener)
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "task_drift"):
            reader.read_bindings([{"task_id": TASK_ID, "template_id": TEMPLATE_ID}])

    def test_read_routes_and_binding_count_are_closed(self):
        reader = SEMAPHORE.SemaphoreReader("https://semaphore.invalid", "fake-secret", PROJECT_ID,
                                           opener=FakeOpener([]))
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "binding_limit"):
            reader.read_bindings([{}] * (SEMAPHORE.MAX_BINDINGS + 1))
        with self.assertRaisesRegex(SEMAPHORE.Refusal, "route_refused"):
            reader._get("/api/project/7/tasks/last", SEMAPHORE.MAX_METADATA_BYTES, 10**30)


if __name__ == "__main__":
    unittest.main()
