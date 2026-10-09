from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).parents[2]


class CoverageContractTests(unittest.TestCase):
    def test_census_is_read_only_and_revision_bound(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/census-o11y-coverage.yml").read_text())
        self.assertEqual(len(plays[1]["tasks"]), 4)
        census = plays[1]["tasks"][2]
        self.assertTrue(census["ansible.builtin.command"]["argv"][1].endswith("o11y-coverage-census.py"))
        self.assertIs(census["changed_when"], False)
        self.assertIs(census["check_mode"], False)
        self.assertIn("repository_sha", census["ansible.builtin.command"]["stdin"])

    def test_strict_verification_keeps_legacy_inputs_and_requires_bounded_target_identity(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        legacy = next(play for play in plays if play.get("name") == "Verify the named service is collected")
        self.assertTrue(any("expected_service" in str(task) for task in legacy["tasks"]))
        verifier = (ROOT / "platform/playbooks/verify-o11y-service.yml").read_text()
        self.assertIn("expected_target_id", verifier)
        self.assertIn("unverifiable_target", verifier)
        self.assertIn("o11y_verification_mode | default('legacy') == 'legacy'", str(legacy["tasks"]))

        templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
        legacy_template = next(t for t in templates if t["name"] == "Verify o11y Service")
        strict_template = next(t for t in templates if t["name"] == "Verify o11y Target Receipt")
        self.assertEqual(legacy_template["playbook"], strict_template["playbook"])
        strict_names = {var["name"] for var in strict_template["survey_vars"]}
        self.assertLessEqual({
            "expected_repository_sha", "expected_inventory_revision", "expected_target_id",
            "expected_signal", "signal_selector", "freshness_seconds",
            "observation_window_seconds", "receipt_reference",
        }, strict_names)

    def test_strict_receipt_uses_the_query_for_the_selected_signal(self):
        tasks = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text())
        registers = [task.get("register") for task in tasks if task.get("register")]
        self.assertEqual(registers, ["_exact_logs", "_exact_prometheus", "_exact_traces"])
        assertion = next(task for task in tasks if task.get("name") == "Require a fresh exact-target receipt")
        exact_receipt = assertion["vars"]["_exact_receipt"]
        self.assertIn("_exact_logs.stdout", exact_receipt)
        self.assertIn("_exact_prometheus.stdout", exact_receipt)
        self.assertIn("_exact_traces.stdout", exact_receipt)

    def test_health_receipt_rejects_generic_up_and_requires_exact_healthy_metric(self):
        verify = (ROOT / "platform/playbooks/verify-o11y-service.yml").read_text()
        self.assertIn("expected_signal != 'health' or signal_selector is not match", verify)
        receipt = (ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text()
        self.assertIn("expected_signal != 'health' or (_exact_receipt.data.result", receipt)
        self.assertIn("unique == ['1']", receipt)
        self.assertIn("expected_signal not in ['metrics', 'health'] or (_exact_receipt.data.result", receipt)


if __name__ == "__main__":
    unittest.main()
