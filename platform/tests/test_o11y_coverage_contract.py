import json
import re
import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).parents[2]


class CoverageContractTests(unittest.TestCase):
    def test_census_cli_rejects_non_object_json_without_traceback(self):
        result = subprocess.run(
            ["python3", str(ROOT / "platform/playbooks/files/o11y-coverage-census.py"), str(ROOT)],
            input="[]", text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout), {
            "status": "invalid_declaration", "error": "inventory input must be a JSON object",
        })
        self.assertNotIn("Traceback", result.stderr)

    def test_census_is_read_only_and_revision_bound(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/census-o11y-coverage.yml").read_text())
        self.assertEqual(len(plays[1]["tasks"]), 4)
        census = plays[1]["tasks"][2]
        self.assertTrue(census["ansible.builtin.command"]["argv"][1].endswith("o11y-coverage-census.py"))
        self.assertIs(census["changed_when"], False)
        self.assertIs(census["check_mode"], False)
        self.assertIn("repository_sha", census["ansible.builtin.command"]["stdin"])

    def test_playbook_json_carries_private_inventory_revision_into_census_report(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/census-o11y-coverage.yml").read_text())
        census = plays[1]["tasks"][2]
        stdin_template = census["ansible.builtin.command"]["stdin"]
        inventory_revision = "b" * 40
        template_environment = jinja2.Environment()
        template_environment.filters["from_yaml"] = yaml.safe_load
        template_environment.filters["to_json"] = json.dumps
        template_environment.globals["lookup"] = lambda plugin, path: Path(path).read_text()
        rendered_json = template_environment.from_string(stdin_template).render(
            _coverage_root=str(ROOT),
            _coverage_inventory={"revision": inventory_revision, "targets": []},
            expected_repository_sha="a" * 40,
        )
        payload = json.loads(rendered_json)
        self.assertEqual(payload["inventory_revision"], inventory_revision)

        result = subprocess.run(
            ["python3", str(ROOT / "platform/playbooks/files/o11y-coverage-census.py"), str(ROOT)],
            input=rendered_json,
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["inventory_revision"], inventory_revision)

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
            "observation_window_seconds",
        }, strict_names)
        self.assertNotIn("receipt_reference", strict_names)

    def test_strict_inventory_revision_refuses_a_mismatched_declaration(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        task = next(
            task
            for play in plays
            for task in play.get("tasks", [])
            if task.get("name") == "Require the exact inventory revision and unique target"
        )
        condition = task["ansible.builtin.assert"]["that"][0]
        environment = jinja2.Environment()

        def matches(actual, expected):
            rendered = environment.from_string("{{ " + condition + " }}").render(
                _coverage_inventory={"revision": actual},
                expected_inventory_revision=expected,
            )
            return rendered == "True"

        self.assertTrue(matches("a" * 40, "a" * 40))
        self.assertFalse(matches("b" * 40, "a" * 40))

    def test_strict_signal_observation_uses_the_query_and_is_unattributed(self):
        tasks = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text())
        registers = [task.get("register") for task in tasks if task.get("register")]
        self.assertEqual(registers, ["_exact_logs", "_exact_prometheus", "_exact_traces"])
        prom = next(task for task in tasks if task.get("name") == "Query the exact Prometheus target selector")
        self.assertIn("observation_window_seconds", prom["ansible.builtin.command"]["argv"][-1])
        self.assertIn("~ '[' ~", prom["ansible.builtin.command"]["argv"][-1])
        assertion_name = "Require a fresh exact-target signal observation"
        assertion = next(task for task in tasks if task.get("name") == assertion_name)
        exact_receipt = assertion["vars"]["_exact_receipt"]
        self.assertIn("_exact_logs.stdout", exact_receipt)
        self.assertIn("_exact_prometheus.stdout", exact_receipt)
        self.assertIn("_exact_traces.stdout", exact_receipt)
        output = next(task for task in tasks if task.get("name") == "Emit sanitized unattributed observation metadata")
        observation = output["ansible.builtin.set_stats"]["data"]["o11y_coverage_observation"]
        self.assertEqual(observation["status"], "observed")
        self.assertIs(observation["task_reference_unattributed"], True)
        self.assertNotIn("receipt_reference", observation)

    def test_prometheus_source_samples_are_checked_for_freshness_target_and_health(self):
        tasks = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text())
        assertion = next(
            task for task in tasks
            if task.get("name") == "Require a fresh exact-target signal observation"
        )
        checks = assertion["ansible.builtin.assert"]["that"]
        env = jinja2.Environment()
        env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
        env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
        env.filters["unique"] = lambda values: list(dict.fromkeys(values))
        env.filters["float"] = float
        epoch = 1791547200
        env.globals["now"] = lambda utc=True: datetime.fromtimestamp(epoch, UTC)

        def passes(expression, receipt, signal_name="metrics"):
            template = env.from_string("{{ " + expression + " }}")
            rendered = template.render(
                expected_signal=signal_name,
                expected_target_id="service:alpha",
                _exact_receipt=receipt,
                _exact_latest_samples=[
                    max(series["values"], key=lambda sample: sample[0])
                    for series in receipt["data"]["result"]
                ],
                freshness_seconds=600,
            )
            return rendered == "True"

        freshness = next(check for check in checks if "_exact_latest_samples | selectattr('0', 'ge'" in check)
        target_check = next(check for check in checks if "metric.target_id" in check)
        health_check = next(check for check in checks if "_exact_latest_samples | map(attribute='1')" in check)

        def response(target_id="service:alpha", sample_time=epoch - 30, sample_value="7"):
            return {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{"metric": {"target_id": target_id}, "values": [[sample_time, sample_value]]}],
                },
            }

        self.assertTrue(passes(freshness, response()))
        self.assertFalse(passes(freshness, response(sample_time=epoch - 900)))
        self.assertFalse(passes(target_check, response(target_id="service:beta")))
        sibling_response = response()
        sibling_response["data"]["result"].append({
            "metric": {"target_id": "service:beta"}, "values": [[epoch - 30, "7"]],
        })
        self.assertFalse(passes(target_check, sibling_response))
        self.assertTrue(passes(health_check, response(sample_value="1"), "health"))
        self.assertFalse(passes(health_check, response(sample_value="0"), "health"))

    def test_health_observation_rejects_generic_up_and_requires_exact_healthy_metric(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        strict = next(play for play in plays if any(
            task.get("name") == "Refuse selectors without the bounded target identity label"
            for task in play.get("tasks", [])
        ))
        conditions = next(task["ansible.builtin.assert"]["that"] for task in strict["tasks"]
                          if task.get("name") == "Refuse selectors without the bounded target identity label")
        env = jinja2.Environment()
        env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
        env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None

        def accepted(selector, signal_name):
            return all(env.from_string("{{ " + item + " }}").render(
                expected_signal=signal_name,
                expected_target_id="service:alpha",
                signal_selector=selector,
            ) == "True" for item in conditions)

        self.assertTrue(accepted('node_health{target_id="service:alpha"}', "health"))
        self.assertFalse(accepted('up{target_id="service:alpha"}', "health"))
        self.assertFalse(accepted('up', "health"))
        self.assertFalse(accepted('sum(node_health{target_id="service:alpha"})', "metrics"))
        self.assertFalse(accepted('node_health{target_id="service:beta"}', "health"))
        tasks = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text())
        assertion = next(
            task for task in tasks
            if task.get("name") == "Require a fresh exact-target signal observation"
        )
        self.assertTrue(any("unique == ['1']" in item for item in assertion["ansible.builtin.assert"]["that"]))

    def test_strict_tempo_selector_rejects_or_sibling_target(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        strict = next(play for play in plays if any(
            task.get("name") == "Refuse selectors without the bounded target identity label"
            for task in play.get("tasks", [])
        ))
        task_name = "Refuse selectors without the bounded target identity label"
        selector_task = next(task for task in strict["tasks"] if task.get("name") == task_name)
        trace_condition = next(
            condition
            for condition in selector_task["ansible.builtin.assert"]["that"]
            if "expected_signal != 'traces'" in condition
        )
        environment = jinja2.Environment()

        def accepts(selector):
            rendered = environment.from_string("{{ " + trace_condition + " }}").render(
                expected_signal="traces",
                expected_target_id="service:alpha",
                signal_selector=selector,
            )
            return rendered == "True"

        self.assertTrue(accepts('{ resource.target_id = "service:alpha" }'))
        self.assertFalse(accepts('{ resource.target_id = "service:alpha" || resource.target_id = "service:beta" }'))

    def test_strict_trace_receipt_accepts_a_child_span(self):
        tasks = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text())
        assertion = next(
            task for task in tasks
            if task.get("name") == "Require a fresh exact-target signal observation"
        )
        checks = assertion["ansible.builtin.assert"]["that"]
        self.assertFalse(any("rootServiceName" in check for check in checks))
        env = jinja2.Environment()
        child_trace_receipt = {
            "traces": [{"rootServiceName": "parent-service", "traceID": "abc"}],
        }
        self.assertTrue(all(env.from_string("{{ " + check + " }}").render(
            expected_signal="traces",
            expected_target_id="service:alpha",
            _exact_receipt=child_trace_receipt,
            _exact_latest_samples=[],
        ) == "True" for check in checks))


if __name__ == "__main__":
    unittest.main()
