import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

import yaml

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/o11y-coverage-refresh.py"
SPEC = importlib.util.spec_from_file_location("o11y_coverage_refresh", SCRIPT)
REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REFRESH)


class CoverageRefreshTests(unittest.TestCase):
    def _api_fixture(self):
        targets = [{
            "target_id": "service:alpha",
            "target_type": "service",
            "lifecycle": "deployed",
            "owner": "platform",
            "runtime": "podman",
            "service_identity": "alpha",
            "environment": "production",
            "signals": {"logs": {
                "applicable": True,
                "selector": '{target_id="service:alpha"}',
                "collection_method": "alloy",
                "freshness_seconds": 600,
            }},
            "collection_method": "alloy",
            "budget": {"samples_per_scrape": 1000},
            "template_references": ["Verify o11y Target Receipt"],
        }]
        revision = REFRESH._census.canonical_inventory_revision(targets)
        coverage = {
            "revision": revision,
            "targets": targets,
            "receipt_bindings": {"service:alpha": {"logs": {
                "template_id": 315, "observation_window_seconds": 300,
            }}},
        }
        receipt = {"id": 315, "project_id": 1, "repository_id": 8, "inventory_id": 2,
                   "name": REFRESH.RECEIPT_TEMPLATE, "playbook": REFRESH.RECEIPT_PLAYBOOK,
                   "app": "ansible", "arguments": "", "allow_override_args_in_task": False,
                   "git_branch": None}
        census = {"id": 316, "project_id": 1, "repository_id": 8, "inventory_id": 2,
                  "name": REFRESH.CENSUS_TEMPLATE, "playbook": REFRESH.CENSUS_PLAYBOOK,
                  "app": "ansible", "arguments": "", "allow_override_args_in_task": False,
                  "git_branch": None}
        repository = {"id": 8, "project_id": 1, "name": REFRESH.REPOSITORY_NAME,
                      "git_url": REFRESH.REPOSITORY_URL, "git_branch": "dev"}
        inventory = {"id": 2, "project_id": 1, "type": "static-yaml",
                     "inventory": yaml.safe_dump({"all": {"vars": {
                         "estate_coverage_inventory": coverage,
                     }}})}
        items = [receipt, census]
        api_rows = {
            "/templates": items,
            "/templates/315": receipt,
            "/templates/316": census,
            "/repositories": [repository],
            "/repositories/8": repository,
            "/inventory/2": inventory,
        }
        return lambda path: api_rows[path], coverage, repository

    def test_target_signal_selection_is_bounded_and_unique(self):
        self.assertEqual(
            REFRESH.parse_target_signals(["service:alpha:logs", "service:beta:metrics"]),
            [("service:alpha", "logs"), ("service:beta", "metrics")],
        )
        for values, reason in (
            (["service:alpha:logs", "service:alpha:logs"], "target_signal_selection_invalid"),
            (["service:alpha"], "target_signal_selection_invalid"),
            ([f"service:target{index:02}:logs" for index in range(65)], "target_signal_cohort_limit"),
        ):
            with self.subTest(values=len(values)), self.assertRaisesRegex(REFRESH.Refusal, reason):
                REFRESH.parse_target_signals(values)

    def test_reviewed_declarations_checks_dev_checkout_and_exact_live_bindings(self):
        api, coverage, repository = self._api_fixture()
        sha = "a" * 40
        with patch.object(REFRESH, "_git", side_effect=lambda *args: {
            ("branch", "--show-current"): "dev",
            ("rev-parse", "HEAD"): sha,
            ("status", "--porcelain", "--untracked-files=all"): "",
        }[args]):
            receipt, census, actual, revision = REFRESH.reviewed_declarations(api, sha)
        self.assertEqual((receipt["id"], census["id"]), (315, 316))
        self.assertEqual(actual, coverage)
        self.assertEqual(revision, coverage["revision"])

        for mutate in (
            lambda inventory, repo: inventory.update(type="static"),
            lambda inventory, repo: repo.update(git_branch="main"),
            lambda inventory, repo: inventory.update(inventory=inventory["inventory"].replace(
                coverage["revision"], "0" * 64,
            )),
        ):
            api, _coverage, repository = self._api_fixture()
            with self.subTest(mutation=mutate.__code__.co_firstlineno):
                # Keep each negative fixture isolated from the preceding API view.
                rows = {
                    "/templates": [{"id": 315, "name": REFRESH.RECEIPT_TEMPLATE},
                                   {"id": 316, "name": REFRESH.CENSUS_TEMPLATE}],
                    "/templates/315": {"id": 315, "project_id": 1, "repository_id": 8,
                                        "inventory_id": 2, "name": REFRESH.RECEIPT_TEMPLATE,
                                        "playbook": REFRESH.RECEIPT_PLAYBOOK, "app": "ansible",
                                        "arguments": "", "allow_override_args_in_task": False,
                                        "git_branch": None},
                    "/templates/316": {"id": 316, "project_id": 1, "repository_id": 8,
                                        "inventory_id": 2, "name": REFRESH.CENSUS_TEMPLATE,
                                        "playbook": REFRESH.CENSUS_PLAYBOOK, "app": "ansible",
                                        "arguments": "", "allow_override_args_in_task": False,
                                        "git_branch": None},
                    "/repositories": [repository],
                    "/repositories/8": repository,
                    "/inventory/2": {"id": 2, "project_id": 1, "type": "static-yaml",
                                      "inventory": yaml.safe_dump({"all": {"vars": {
                                          "estate_coverage_inventory": _coverage,
                                      }}})},
                }
                mutate(rows["/inventory/2"], rows["/repositories/8"])
                def git_result(*args):
                    return {
                        ("branch", "--show-current"): "dev",
                        ("rev-parse", "HEAD"): sha,
                        ("status", "--porcelain", "--untracked-files=all"): "",
                    }[args]

                with patch.object(REFRESH, "_git", side_effect=git_result), \
                        self.assertRaises(REFRESH.Refusal):
                    REFRESH.reviewed_declarations(lambda path, response_rows=rows: response_rows[path], sha)

    def test_cohort_uses_returned_ids_only_for_the_census_survey(self):
        declarations = {
            "targets": [{
                "target_id": "service:alpha",
                "signals": {
                    "logs": {"applicable": True, "selector": '{target_id="service:alpha"}',
                             "freshness_seconds": 600},
                    "metrics": {"applicable": True, "selector": '{target_id="service:alpha"}',
                                "freshness_seconds": 600},
                },
            }],
            "receipt_bindings": {
                "service:alpha": {
                    "logs": {"template_id": 315, "observation_window_seconds": 300},
                    "metrics": {"template_id": 315, "observation_window_seconds": 600},
                },
            },
        }
        api = object()
        launch_results = [(701, "success"), (702, "success"), (703, "success")]
        calls = []

        def launch(_api, name, settings, **kwargs):
            calls.append((name, settings, kwargs))
            return launch_results[len(calls) - 1]

        with patch.object(REFRESH, "reviewed_declarations", return_value=(
            {"id": 315}, {"id": 316}, declarations, "a" * 64,
        )), patch.object(REFRESH._launcher, "launch", side_effect=launch):
            count, census_id = REFRESH.launch_cohort(
                api, [("service:alpha", "logs"), ("service:alpha", "metrics")], "b" * 40,
            )

        self.assertEqual((count, census_id), (2, 703))
        self.assertEqual([call[0] for call in calls], [
            REFRESH.RECEIPT_TEMPLATE, REFRESH.RECEIPT_TEMPLATE, REFRESH.CENSUS_TEMPLATE,
        ])
        self.assertEqual(calls[0][1]["observation_window_seconds"], 300)
        self.assertEqual(calls[1][1]["observation_window_seconds"], 600)
        transient = json.loads(calls[2][1]["receipt_task_ids"])
        self.assertEqual(transient, {"service:alpha": {"logs": 701, "metrics": 702}})
        self.assertTrue(all(call[2] == {"dry_run": False, "block": True} for call in calls))

    def test_failed_receipt_task_stops_before_census_launch(self):
        declarations = {
            "targets": [{
                "target_id": "service:alpha",
                "signals": {"logs": {"applicable": True, "selector": '{target_id="service:alpha"}',
                                     "freshness_seconds": 600}},
            }],
            "receipt_bindings": {"service:alpha": {
                "logs": {"template_id": 315, "observation_window_seconds": 300},
            }},
        }
        launched = []

        def launch(*args, **kwargs):
            launched.append(args[1])
            return 701, "error"

        with patch.object(REFRESH, "reviewed_declarations", return_value=(
            {"id": 315}, {"id": 316}, declarations, "a" * 64,
        )), patch.object(REFRESH._launcher, "launch", side_effect=launch), \
                self.assertRaisesRegex(REFRESH.Refusal, "receipt_task_failed"):
            REFRESH.launch_cohort(object(), [("service:alpha", "logs")], "b" * 40)
        self.assertEqual(launched, [REFRESH.RECEIPT_TEMPLATE])

    def test_cli_reads_token_from_stdin_and_never_prints_it(self):
        token = "PRIVATE_OPERATOR_TOKEN"
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(REFRESH.sys, "argv", [
            str(SCRIPT), "--url", "https://semaphore.example", "--reviewed-dev-sha", "a" * 40,
            "--target-signal", "service:alpha:logs",
        ]), patch.object(REFRESH.sys, "stdin", io.StringIO(token)), \
                patch.object(REFRESH._launcher, "API") as api, \
                patch.object(REFRESH, "launch_cohort", return_value=(1, 703)), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(REFRESH.main(), 0)
        self.assertEqual(api.call_args, call("https://semaphore.example", 1, token))
        self.assertNotIn(token, output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()
