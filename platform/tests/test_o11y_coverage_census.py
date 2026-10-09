import importlib.util
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "playbooks/files/o11y-coverage-census.py"
SPEC = importlib.util.spec_from_file_location("o11y_coverage_census", SCRIPT)
CENSUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CENSUS)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
REPO_SHA = "a" * 40
INVENTORY_REVISION = "b" * 40


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


class CoverageCensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        (self.repo / "platform/services/alpha").mkdir(parents=True)
        (self.repo / "agents").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def report(self, targets):
        return CENSUS.build_report(
            self.repo,
            {"inventory_revision": INVENTORY_REVISION, "targets": targets,
             "template_names": ["Verify o11y Target Receipt"]},
            REPO_SHA,
            NOW,
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
            "health": "excepted", "logs": "verified", "metrics": "incomplete", "traces": "excepted",
        })
        self.assertEqual(row["coverage"], "incomplete")

    def test_stale_receipt_is_not_coverage(self):
        row = self.report([target(signals={"logs": signal(receipt(observed_at="2026-10-09T10:00:00Z"))})])["targets"][0]
        self.assertEqual(row["signals"]["logs"], "stale")
        self.assertEqual(row["coverage"], "incomplete")

    def test_duplicate_identity_is_reported_as_unverified_conflict(self):
        rows = self.report([target(), target("service:other", "alpha")])
        self.assertEqual(rows["duplicate_identity_count"], 1)
        self.assertEqual(rows["duplicate_identity_targets"], ["service:alpha", "service:other"])
        conflicts = [row for row in rows["targets"] if row.get("identity_conflict")]
        self.assertEqual(len(conflicts), 2)
        self.assertTrue(all(row["coverage"] == "unverified" for row in conflicts))

    def test_private_endpoint_values_do_not_enter_report(self):
        declaration = target()
        declaration["inventory_host"] = "private-host.example"
        declaration["endpoint"] = "192.0.2.10:9090"
        rendered = str(self.report([declaration]))
        self.assertNotIn("private-host.example", rendered)
        self.assertNotIn("192.0.2.10", rendered)

    def test_missing_template_reference_keeps_target_incomplete(self):
        declaration = target()
        declaration["template_references"] = ["Missing Template"]
        declaration["signals"]["logs"] = signal(receipt("service:alpha", "logs"))
        row = self.report([declaration])["targets"][0]
        self.assertEqual(row["coverage"], "incomplete")
        self.assertEqual(row["template_references"]["missing"], ["Missing Template"])

    def test_mismatched_inventory_revision_invalidates_receipt(self):
        row = self.report([target(signals={"logs": signal(receipt(inventory_revision="c" * 40))})])["targets"][0]
        self.assertEqual(row["signals"]["logs"], "incomplete")

    def test_timezone_naive_receipt_is_not_freshness_evidence(self):
        row = self.report([target(signals={
            "logs": signal(receipt(observed_at="2026-10-09T11:30:00")),
        })])["targets"][0]
        self.assertEqual(row["signals"]["logs"], "incomplete")


if __name__ == "__main__":
    unittest.main()
