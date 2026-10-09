import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
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

    def test_ansible_json_transport_matches_the_canonical_python_digest_bytes(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        task = next(
            task for task in plays[1]["tasks"]
            if task.get("name") == "Recompute the canonical private inventory revision"
        )
        stdin_template = task["ansible.builtin.command"]["stdin"]
        targets = [{
            "target_id": "service:alpha", "target_type": "service", "lifecycle": "deployed",
            "owner": "platform", "runtime": "podman", "service_identity": "alpha",
            "environment": "production", "signals": {}, "collection_method": "alloy",
            "budget": {"samples_per_scrape": 1000}, "receipt_reference": {},
            "template_references": [], "inventory_host": "雪",
        }]
        canonical = json.dumps(
            {"targets": targets}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        inventory_revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        template_environment = jinja2.Environment()
        template_environment.filters["from_yaml"] = yaml.safe_load
        template_environment.filters["to_json"] = json.dumps
        rendered_json = template_environment.from_string(stdin_template).render(
            _coverage_inventory={"revision": inventory_revision, "targets": targets},
        )
        self.assertEqual(rendered_json.encode("utf-8"), canonical.encode("utf-8"))

        result = subprocess.run(
            ["python3", str(ROOT / "platform/playbooks/files/o11y-coverage-census.py"), "--inventory-revision"],
            input=rendered_json,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), inventory_revision)

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

    def test_strict_inventory_digest_is_recomputed_before_any_signal_query(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        task = next(
            task
            for task in plays[1]["tasks"]
            if task.get("name") == "Require the declared and reviewed inventory digest"
        )
        conditions = task["ansible.builtin.assert"]["that"]
        environment = jinja2.Environment()

        def matches(actual, expected, calculated):
            return all(environment.from_string("{{ " + condition + " }}").render(
                _coverage_inventory={"revision": actual},
                expected_inventory_revision=expected,
                _calculated_inventory_revision=calculated,
            ) == "True" for condition in conditions)

        digest = "a" * 64
        self.assertTrue(matches(digest, digest, {"rc": 0, "stdout": digest}))
        self.assertFalse(matches("b" * 64, digest, {"rc": 0, "stdout": digest}))
        self.assertFalse(matches(digest, digest, {"rc": 0, "stdout": "c" * 64}))
        self.assertFalse(matches(digest, digest, {"rc": 2, "stdout": ""}))

        digest_play_index = next(i for i, play in enumerate(plays)
                                 if any(task.get("name") == "Require the declared and reviewed inventory digest"
                                        for task in play.get("tasks", [])))
        first_query_play_index = next(i for i, play in enumerate(plays)
                                      if any(task.get("name") == "Query recent Loki logs for the service"
                                             for task in play.get("tasks", [])))
        self.assertLess(digest_play_index, first_query_play_index)

    def test_receiver_host_inventory_override_is_refused_before_signal_queries(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        receiver = next(play for play in plays if play.get("name") == "Verify one exact target signal receipt")
        tasks = receiver["tasks"]
        recompute_index = next(i for i, task in enumerate(tasks)
                               if task.get("name") == "Recompute the receiver host's canonical inventory revision")
        assert_index = next(i for i, task in enumerate(tasks)
                            if task.get("name") == "Require the receiver host's reviewed inventory digest")
        selection_index = next(i for i, task in enumerate(tasks)
                               if task.get("name") == "Require the unique declared target")
        first_signal_query_index = next(i for i, task in enumerate(tasks)
                                        if task.get("name") == "Check the exact signal receipt")
        self.assertLess(recompute_index, assert_index)
        self.assertLess(assert_index, selection_index)
        self.assertLess(selection_index, first_signal_query_index)

        original_targets = [{
            "target_id": "service:alpha", "target_type": "service", "lifecycle": "deployed",
            "owner": "platform", "runtime": "podman", "service_identity": "alpha",
            "environment": "production", "signals": {}, "collection_method": "alloy",
            "budget": {"samples_per_scrape": 1000}, "receipt_reference": {},
            "template_references": [], "inventory_host": "collector-a",
        }]
        host_override_targets = [{
            **original_targets[0], "target_id": "service:beta", "service_identity": "beta",
            "inventory_host": "collector-b",
        }]

        def digest(targets):
            encoded = json.dumps(
                {"targets": targets}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
            return hashlib.sha256(encoded).hexdigest()

        expected = digest(original_targets)
        override_revision = digest(host_override_targets)
        ansible_playbook = shutil.which("ansible-playbook")
        self.assertIsNotNone(ansible_playbook, "ansible-playbook is required for delegated inventory coverage")
        with tempfile.TemporaryDirectory(prefix="o11y-inventory-override-") as directory:
            temp = Path(directory)
            local_tmp, remote_tmp = temp / "local", temp / "remote"
            local_tmp.mkdir()
            remote_tmp.mkdir()
            inventory = {
                "all": {
                    "vars": {"estate_coverage_inventory": {
                        "revision": expected, "targets": original_targets,
                    }},
                    "children": {"o11y_svc": {"hosts": {"receiver": {
                        "ansible_connection": "local",
                        "estate_coverage_inventory": {
                            "revision": override_revision, "targets": host_override_targets,
                        },
                    }}}},
                },
            }
            receiver_tasks = [tasks[recompute_index], tasks[assert_index]]
            receiver_tasks[0]["ansible.builtin.command"]["argv"][1] = str(
                ROOT / "platform/playbooks/files/o11y-coverage-census.py"
            )
            playbook = [{
                "name": "Verify delegated receiver inventory digest",
                "hosts": "o11y_svc",
                "gather_facts": False,
                "vars": {
                    "_coverage_inventory": "{{ estate_coverage_inventory | default({}) }}",
                    "o11y_verification_mode": "strict",
                },
                "tasks": receiver_tasks,
            }]
            inventory_path, playbook_path = temp / "inventory.yml", temp / "verify.yml"
            inventory_path.write_text(yaml.safe_dump(inventory, sort_keys=False))
            playbook_path.write_text(yaml.safe_dump(playbook, sort_keys=False))
            environment = os.environ.copy()
            environment.update(
                ANSIBLE_LOCAL_TEMP=str(local_tmp),
                ANSIBLE_REMOTE_TEMP=str(remote_tmp),
                ANSIBLE_STDOUT_CALLBACK="default",
                ANSIBLE_NOCOLOR="1",
            )
            result = subprocess.run(
                [
                    ansible_playbook,
                    "-i", str(inventory_path),
                    str(playbook_path),
                    "-e", json.dumps({"expected_inventory_revision": override_revision}),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-1200:] + result.stderr[-1200:])

            refusal = subprocess.run(
                [
                    ansible_playbook,
                    "-i", str(inventory_path),
                    str(playbook_path),
                    "-e", json.dumps({"expected_inventory_revision": expected}),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertNotEqual(refusal.returncode, 0, refusal.stdout[-1200:] + refusal.stderr[-1200:])
            self.assertIn(
                "Strict signal observation refused: the receiver host inventory does not match the reviewed revision.",
                refusal.stdout + refusal.stderr,
            )

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
        env.tests["string"] = lambda value: isinstance(value, str)
        env.filters["unique"] = lambda values: list(dict.fromkeys(values))
        env.filters["float"] = float
        epoch = 1791547200
        env.globals["now"] = lambda utc=True: datetime.fromtimestamp(epoch, UTC)
        latest_template = env.from_string(assertion["vars"]["_exact_latest_samples"])

        def latest_samples(receipt):
            return ast.literal_eval(latest_template.render(_exact_receipt=receipt))

        def passes(expression, receipt, signal_name="metrics"):
            template = env.from_string("{{ " + expression + " }}")
            rendered = template.render(
                expected_signal=signal_name,
                expected_target_id="service:alpha",
                _exact_receipt=receipt,
                _exact_latest_samples=latest_samples(receipt),
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

        histogram = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{
                    "metric": {"target_id": "service:alpha"},
                    "histograms": [[epoch - 30, {"count": "4", "sum": "8"}]],
                }],
            },
        }
        self.assertTrue(passes(freshness, histogram))
        self.assertFalse(passes(health_check, histogram, "health"))

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
