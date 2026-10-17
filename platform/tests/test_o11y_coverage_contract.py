import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlunsplit

import jinja2
import yaml

ROOT = Path(__file__).parents[2]


def _play_named(plays, name):
    return next(play for play in plays if play.get("name") == name)


class CoverageContractTests(unittest.TestCase):
    def test_strict_observation_cli_is_sanitized_and_profile_cli_remains_compatible(self):
        script = ROOT / "platform/playbooks/files/o11y-coverage-census.py"
        strict = {
            "response": {
                "status": "success",
                "data": {"resultType": "matrix", "result": [{
                    "metric": {"target_id": "service:alpha"},
                    "values": [[1300, "1"]],
                }]},
            },
            "command_rc": 0,
            "signal": "health",
            "target_id": "service:alpha",
            "query_start": 1000,
            "query_end": 1300,
            "observed_at": 1300,
            "requested_freshness_seconds": 600,
            "declared_freshness_seconds": 600,
            "window_seconds": 300,
        }
        result = subprocess.run(
            ["python3", str(script), "--validate-signal-observation"],
            input=json.dumps(strict), text=True, capture_output=True, check=True,
        )
        output = json.loads(result.stdout)
        self.assertEqual(output, {"status": "verified", "reason": "verified", "evidence_count": 1})
        self.assertNotIn("target_id", output)

        legacy = {
            "target_id": "service:alpha",
            "start_ms": 1_000_000,
            "end_ms": 1_600_000,
            "freshness_seconds": 300,
            "response": {"series": [{
                "labels": [{"name": "target_id", "value": "service:alpha"}],
                "points": [{"timestamp": "1500000", "value": 3}],
            }]},
        }
        old_result = subprocess.run(
            ["python3", str(script), "--validate-profile-receipt"],
            input=json.dumps(legacy), text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(old_result.stdout), {"status": "verified", "sample_count": 1})

    def test_common_validator_accepts_all_five_strict_signal_shapes(self):
        script = ROOT / "platform/playbooks/files/o11y-coverage-census.py"
        target = "service:alpha"
        responses = {
            "logs": {"status": "success", "data": {"resultType": "streams", "result": [{
                "stream": {"target_id": target}, "values": [["1300000000000", "private log"]],
            }]}},
            "metrics": {"status": "success", "data": {"resultType": "matrix", "result": [{
                "metric": {"target_id": target}, "values": [[1300, "7"]],
            }]}},
            "health": {"status": "success", "data": {"resultType": "matrix", "result": [{
                "metric": {"target_id": target}, "values": [[1300, "1"]],
            }]}},
            "traces": {"traces": [{"spanSets": [{"spans": [{
                "spanID": "child-span", "startTimeUnixNano": "1300000000000",
                "attributes": [{"key": "target_id", "value": {"stringValue": target}}],
            }]}]}]},
            "profiles": {"series": [{
                "labels": [{"name": "target_id", "value": target}],
                "points": [{"timestamp": 1_300_000, "value": 1.0}],
            }]},
        }
        for signal, response in responses.items():
            payload = {
                "response": response, "command_rc": 0, "signal": signal, "target_id": target,
                "query_start": 1000, "query_end": 1300, "observed_at": 1300,
                "requested_freshness_seconds": 600, "declared_freshness_seconds": 600,
                "window_seconds": 300,
            }
            with self.subTest(signal=signal):
                result = subprocess.run(
                    ["python3", str(script), "--validate-signal-observation"],
                    input=json.dumps(payload), text=True, capture_output=True, check=True,
                )
                self.assertEqual(json.loads(result.stdout)["status"], "verified")

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

    def test_census_invokes_protected_read_only_receipt_controller(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/census-o11y-coverage.yml").read_text())
        tasks = _play_named(plays, "Check out and inventory declarations")["tasks"]
        controller = next(
            task for task in tasks
            if task.get("name") == "Run the read-only Semaphore receipt census controller"
        )
        command = controller["ansible.builtin.command"]
        self.assertEqual(command["argv"][0], "python3")
        self.assertTrue(command["argv"][1].endswith("o11y-coverage-semaphore.py"))
        self.assertEqual(command["argv"][2], "--controller")
        self.assertIs(controller["changed_when"], False)
        self.assertIs(controller["check_mode"], False)
        self.assertIs(controller["no_log"], True)
        self.assertIs(controller["failed_when"], False)
        self.assertIn("'revision' in _coverage_inventory", command["stdin"])
        self.assertIn("'targets': _coverage_inventory.get('targets', [])", command["stdin"])
        self.assertIn("'receipt_bindings': _coverage_receipt_bindings", command["stdin"])
        self.assertIn("'template_names':", command["stdin"])
        self.assertIn("'expected_repository_sha': expected_repository_sha", command["stdin"])

    def test_coverage_playbooks_guard_before_credentials_and_strict_helpers(self):
        census = yaml.safe_load((ROOT / "platform/playbooks/census-o11y-coverage.yml").read_text())
        census_guard = next(
            index for index, entry in enumerate(census)
            if entry.get("ansible.builtin.import_playbook") == "require-reviewed-checkout.yml"
        )
        census_work = next(
            index for index, entry in enumerate(census)
            if entry.get("name") == "Check out and inventory declarations"
        )
        self.assertLess(census_guard, census_work)

        verifier = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        strict_guard = next(
            (index, entry) for index, entry in enumerate(verifier)
            if entry.get("ansible.builtin.import_playbook") == "require-reviewed-checkout.yml"
        )
        self.assertEqual(
            strict_guard[1]["when"],
            "o11y_verification_mode | default('legacy') == 'strict'",
        )
        strict_work = next(
            index for index, entry in enumerate(verifier)
            if entry.get("name") == "Require a declared o11y receiver and a service identity"
        )
        self.assertLess(strict_guard[0], strict_work)

    def test_reviewed_checkout_refusal_precedes_fake_external_access(self):
        ansible_playbook = shutil.which("ansible-playbook")
        self.assertIsNotNone(ansible_playbook)

        def run_fixture(mutation=None, wrong_sha=False):
            with tempfile.TemporaryDirectory(prefix="o11y-reviewed-entry-") as directory:
                temp = Path(directory)
                checkout = temp / "checkout"
                playbook_dir = checkout / "platform" / "playbooks"
                files = playbook_dir / "files"
                files.mkdir(parents=True)
                shutil.copy2(ROOT / "platform/playbooks/require-reviewed-checkout.yml", playbook_dir)
                shutil.copy2(ROOT / "platform/playbooks/files/reviewed-checkout.py", files)
                payload = checkout / "payload.txt"
                payload.write_text("reviewed bytes\n", encoding="utf-8")
                other = checkout / "other.txt"
                other.write_text("other bytes\n", encoding="utf-8")
                calls = temp / "external-access.calls"
                entrypoint = playbook_dir / "guard-entry.yml"
                entrypoint.write_text(yaml.safe_dump([
                    {"name": "Run the shared reviewed-checkout gate",
                     "ansible.builtin.import_playbook": "require-reviewed-checkout.yml"},
                    {"name": "Fake credentials, backend query, and observation emission",
                     "hosts": "localhost", "connection": "local", "gather_facts": False,
                     "tasks": [{
                         "name": "Record that a protected action was reached",
                         "ansible.builtin.command": {"argv": [
                             "python3", "-c",
                             "from pathlib import Path; Path(" + repr(str(calls)) + ").write_text('called')",
                         ]},
                         "changed_when": False,
                     }]},
                ]), encoding="utf-8")

                git_env = {
                    **os.environ,
                    "GIT_AUTHOR_NAME": "test",
                    "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "test",
                    "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                }
                subprocess.run(["git", "init", "-q", str(checkout)], env=git_env, check=True)
                subprocess.run(["git", "-C", str(checkout), "add", "."], env=git_env, check=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture"],
                               env=git_env, check=True)
                revision = subprocess.run(
                    ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                    env=git_env, text=True, capture_output=True, check=True,
                ).stdout.strip()
                if mutation:
                    mutation(checkout)

                environment = {
                    **os.environ,
                    "ANSIBLE_LOCAL_TEMP": str(temp / "local"),
                    "ANSIBLE_REMOTE_TEMP": str(temp / "remote"),
                    "ANSIBLE_STDOUT_CALLBACK": "default",
                    "ANSIBLE_NOCOLOR": "1",
                }
                (temp / "local").mkdir()
                (temp / "remote").mkdir()
                expected = "0" * 40 if wrong_sha else revision
                result = subprocess.run(
                    [ansible_playbook, "-i", "localhost,", "-c", "local", "-e",
                     f"expected_repository_sha={expected}", str(entrypoint)],
                    cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
                )
                return result, calls.exists()

        mutations = {
            "dirty worktree": lambda checkout: (checkout / "payload.txt").write_text(
                "changed bytes\n", encoding="utf-8"),
            "staged index": lambda checkout: (
                (checkout / "payload.txt").write_text("staged bytes\n", encoding="utf-8"),
                subprocess.run(["git", "-C", str(checkout), "add", "payload.txt"], check=True),
            ),
            "mode change": lambda checkout: (checkout / "payload.txt").chmod(
                (checkout / "payload.txt").stat().st_mode | 0o100,
            ),
            "type change": lambda checkout: (
                (checkout / "payload.txt").unlink(),
                os.symlink("other.txt", checkout / "payload.txt"),
            ),
            "skip-worktree shortcut": lambda checkout: subprocess.run(
                ["git", "-C", str(checkout), "update-index", "--skip-worktree", "payload.txt"], check=True,
            ),
        }
        result, accessed = run_fixture()
        if "Local RPC server did not start" in result.stderr:
            self.skipTest("installed Ansible runtime cannot start its local RPC server")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(accessed)
        for name, mutation in mutations.items():
            with self.subTest(refusal=name):
                result, accessed = run_fixture(mutation)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(accessed, result.stdout + result.stderr)
        result, accessed = run_fixture(wrong_sha=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(accessed, result.stdout + result.stderr)

    def test_receipt_controller_secret_boundaries_and_fixed_refusal(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/census-o11y-coverage.yml").read_text())
        tasks = _play_named(plays, "Check out and inventory declarations")["tasks"]
        by_name = {task.get("name"): task for task in tasks}
        for name in (
            "Authenticate to OpenBao (AppRole)",
            "Read the Semaphore URL and API token from OpenBao",
            "Hold Semaphore credentials in memory",
            "Require the Semaphore URL and API token",
            "Run the read-only Semaphore receipt census controller",
            "Parse the protected controller report",
        ):
            with self.subTest(name=name):
                self.assertIs(by_name[name]["no_log"], True)
                self.assertNotIn("loop", by_name[name])
        for name in ("Authenticate to OpenBao (AppRole)", "Read the Semaphore URL and API token from OpenBao"):
            uri = by_name[name]["ansible.builtin.uri"]
            self.assertIs(uri["use_netrc"], False)
            self.assertIs(uri["use_proxy"], False)
        self.assertEqual(
            by_name["Read the Semaphore URL and API token from OpenBao"]["ansible.builtin.uri"]["url"],
            "{{ _bao_url }}/v1/secret/data/services/semaphore",
        )
        self.assertEqual(
            by_name["Read the Semaphore URL and API token from OpenBao"]["ansible.builtin.uri"]["follow_redirects"],
            "none",
        )
        self.assertEqual(
            by_name["Hold Semaphore credentials in memory"]["ansible.builtin.set_fact"][
                "_coverage_semaphore_credentials"
            ]["api_token"],
            "{{ _coverage_semaphore_secret.json.data.data.api_token | default('') }}",
        )
        self.assertEqual(
            by_name["Require private Semaphore IDs for receipt attribution"]["when"],
            "_coverage_has_receipt_bindings",
        )
        self.assertIn(
            "secret/data/services/semaphore",
            str(by_name["Read the Semaphore URL and API token from OpenBao"]),
        )
        guards = [task for task in tasks if "ansible.builtin.include_tasks" in task]
        self.assertEqual(
            sum(
                task["ansible.builtin.include_tasks"] == "tasks/assert-bao-transport.yml"
                for task in guards
            ),
            2,
        )
        self.assertEqual(
            by_name["Fail closed on a controller refusal"]["ansible.builtin.assert"]["fail_msg"],
            "receipt_verification_failed",
        )
        report = by_name["Report the candidate census"]["ansible.builtin.debug"]["msg"]
        self.assertEqual(report, "{{ _coverage_report }}")
        self.assertNotIn("stdout", report)

    def test_transport_guard_refusal_does_not_echo_url_userinfo(self):
        ansible_playbook = shutil.which("ansible-playbook")
        self.assertIsNotNone(ansible_playbook)
        sentinel = "FAKE_TRANSPORT_CREDENTIAL"
        with tempfile.TemporaryDirectory(prefix="o11y-guard-refusal-") as directory:
            temp = Path(directory)
            (temp / "local").mkdir()
            (temp / "remote").mkdir()
            path = temp / "guard.yml"
            environment = os.environ.copy()
            environment.update(
                ANSIBLE_LOCAL_TEMP=str(temp / "local"),
                ANSIBLE_REMOTE_TEMP=str(temp / "remote"),
                ANSIBLE_STDOUT_CALLBACK="default",
                ANSIBLE_NOCOLOR="1",
            )
            for endpoint, accepted in (
                (urlunsplit(("http", "user:" + sentinel + "@public.example", "", "", "")), False),
                (urlunsplit(("https", "user:" + sentinel + "@public.example", "", "", "")), False),
                ("https://", False),
                ("https://bad..example", False),
                ("https://public.example:bad", False),
                ("https://bao.example.com:8200/v1", True),
                ("https://bao.example.com.:8200/v1", True),
            ):
                with self.subTest(endpoint=endpoint):
                    playbook = [{
                        "hosts": "localhost", "connection": "local", "gather_facts": False,
                        "tasks": [{
                            "name": "Refuse an invalid endpoint",
                            "ansible.builtin.include_tasks": str(
                                ROOT / "platform/playbooks/tasks/assert-bao-transport.yml"
                            ),
                            "vars": {"_assert_bao_url": endpoint, "_assert_url_label": "Semaphore"},
                        }],
                    }]
                    path.write_text(yaml.safe_dump(playbook))
                    result = subprocess.run(
                        [ansible_playbook, "-i", "localhost,", "-c", "local", str(path)],
                        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
                    )
                    output = result.stdout + result.stderr
                    if accepted:
                        self.assertEqual(result.returncode, 0, output)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("Refusing to send secret material to Semaphore", output)
                    self.assertNotIn(sentinel, output)

    def test_controller_payload_shape_and_empty_binding_execution(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/census-o11y-coverage.yml").read_text())
        tasks = _play_named(plays, "Check out and inventory declarations")["tasks"]
        task = next(task for task in tasks
                    if task.get("name") == "Run the read-only Semaphore receipt census controller")
        stdin_template = task["ansible.builtin.command"]["stdin"]
        template_environment = jinja2.Environment()
        template_environment.filters["to_json"] = json.dumps
        template_environment.filters["from_yaml"] = yaml.safe_load
        template_environment.globals["lookup"] = lambda _plugin, path: Path(path).read_text()
        targets = []
        canonical_inventory = json.dumps(
            {"targets": targets}, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        revision = hashlib.sha256(canonical_inventory).hexdigest()
        repository_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True,
        ).stdout.strip()
        def render(has_bindings, bindings, credentials=None, *, include_revision=True):
            context = {
                "expected_repository_sha": repository_sha,
                "_coverage_root": str(ROOT),
                "_coverage_inventory": {
                    **({"revision": revision} if include_revision else {}),
                    "targets": targets,
                    **({"receipt_bindings": bindings} if bindings is not None else {}),
                },
                "_coverage_receipt_bindings": bindings if bindings is not None else {},
                "_coverage_has_receipt_bindings": has_bindings,
            }
            if credentials:
                context["_coverage_semaphore_credentials"] = credentials
                context["estate_coverage_semaphore_project_id"] = 23
                context["estate_coverage_semaphore_repository_id"] = 41
            return json.loads(template_environment.from_string(stdin_template).render(**context))

        empty_payload = render(False, None)
        explicitly_empty_payload = render(False, {})
        self.assertEqual(explicitly_empty_payload, empty_payload)
        self.assertEqual(set(empty_payload), {"expected_repository_sha", "inventory"})
        self.assertEqual(set(empty_payload["inventory"]), {
            "inventory_revision", "repository_sha", "targets", "receipt_bindings", "template_names",
        })
        self.assertEqual(empty_payload["inventory"]["inventory_revision"], revision)
        self.assertEqual(empty_payload["inventory"]["repository_sha"], repository_sha)
        self.assertEqual(empty_payload["inventory"]["targets"], targets)
        self.assertEqual(empty_payload["inventory"]["receipt_bindings"], {})
        missing_digest_payload = render(False, None, include_revision=False)
        self.assertNotIn("inventory_revision", missing_digest_payload["inventory"])
        template_names = [
            template["name"]
            for template in yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
        ]
        self.assertEqual(empty_payload["inventory"]["template_names"], template_names)

        bound_payload = render(True, {"service:alpha": {"logs": {
            "task_id": 7, "template_id": 8, "observation_window_seconds": 300,
        }}}, {"url": "https://semaphore.example", "api_token": "protected-token"})
        self.assertEqual(set(bound_payload), {
            "expected_repository_sha", "inventory", "semaphore_url", "api_token", "project_id", "repository_id",
        })
        self.assertEqual(set(bound_payload["inventory"]), {
            "inventory_revision", "repository_sha", "targets", "receipt_bindings", "template_names",
        })
        self.assertEqual(bound_payload["semaphore_url"], "https://semaphore.example")
        self.assertEqual(bound_payload["api_token"], "protected-token")
        self.assertEqual((bound_payload["project_id"], bound_payload["repository_id"]), (23, 41))

        refusal = subprocess.run(
            ["python3", str(ROOT / "platform/playbooks/files/o11y-coverage-semaphore.py"), "--controller"],
            input=json.dumps(bound_payload), text=True, capture_output=True, check=False,
        )
        self.assertEqual(refusal.returncode, 2)
        self.assertEqual(json.loads(refusal.stdout), {
            "status": "refused", "reason": "receipt_verification_failed",
        })
        self.assertEqual(refusal.stderr, "")
        self.assertNotIn("protected-token", refusal.stdout + refusal.stderr)

        result = subprocess.run(
            ["python3", str(ROOT / "platform/playbooks/files/o11y-coverage-semaphore.py"), "--controller"],
            input=json.dumps(empty_payload), text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertNotEqual(report.get("status"), "refused")
        self.assertNotIn('"status": "verified"', json.dumps(report))
        self.assertNotIn("o11y_coverage_observation", json.dumps(report))
        missing_digest_result = subprocess.run(
            ["python3", str(ROOT / "platform/playbooks/files/o11y-coverage-semaphore.py"), "--controller"],
            input=json.dumps(missing_digest_payload), text=True, capture_output=True, check=False,
        )
        self.assertEqual(missing_digest_result.returncode, 0, missing_digest_result.stderr)
        self.assertEqual(json.loads(missing_digest_result.stdout)["status"], "inventory_missing")

    def test_strict_freshness_and_window_guards_are_exact_and_legacy_is_separate(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        inputs = next(
            task for play in plays for task in play.get("tasks", [])
            if task.get("name") == "Validate strict signal observation inputs"
        )
        checks = " ".join(inputs["ansible.builtin.assert"]["that"])
        for expression in (
            "freshness_seconds is integer", "freshness_seconds is not boolean",
            "observation_window_seconds is integer", "observation_window_seconds is not boolean",
            "freshness_seconds >= 60", "freshness_seconds <= 86400",
            "observation_window_seconds >= 60", "observation_window_seconds <= 3600",
            "observation_window_seconds < freshness_seconds",
        ):
            self.assertIn(expression, checks)
        self.assertEqual(inputs["when"], "o11y_verification_mode | default('legacy') == 'strict'")

        declared = next(
            task for play in plays for task in play.get("tasks", [])
            if task.get("name") == "Require the requested and declared freshness to match exactly"
        )
        declared_checks = " ".join(declared["ansible.builtin.assert"]["that"])
        self.assertIn("is integer", declared_checks)
        self.assertIn("is not boolean", declared_checks)
        self.assertIn(
            "freshness_seconds == _coverage_target.signals[expected_signal].freshness_seconds", declared_checks
        )
        self.assertEqual(declared["when"], "o11y_verification_mode | default('legacy') == 'strict'")

    def test_ansible_json_transport_matches_the_canonical_python_digest_bytes(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        tasks = _play_named(plays, "Require a declared o11y receiver and a service identity")["tasks"]
        task = next(
            task for task in tasks
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
        tasks = _play_named(plays, "Require a declared o11y receiver and a service identity")["tasks"]
        task = next(
            task
            for task in tasks
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
        queries = {task["name"]: task for task in tasks if task["name"].startswith("Query the exact ")}
        self.assertEqual(set(queries), {
            "Query the exact Loki target selector", "Query the exact Prometheus target selector",
            "Query the exact Tempo target selector", "Query the exact Pyroscope profile selector",
        })
        self.assertEqual({task["register"] for task in queries.values()}, {
            "_exact_logs", "_exact_prometheus", "_exact_traces", "_exact_profiles",
        })
        self.assertTrue(all(task["no_log"] is True for task in queries.values()))
        bounds = next(task for task in tasks if task["name"] == "Freeze the strict observation query end")
        self.assertIn("now(utc=true).timestamp() | int", bounds["ansible.builtin.set_fact"]["_coverage_query_end"])
        self.assertNotIn("no_log", bounds)
        start = next(task for task in tasks if task["name"] == "Derive the strict observation query start")
        self.assertIn("_coverage_query_end", start["ansible.builtin.set_fact"]["_coverage_query_start"])
        self.assertIn("observation_window_seconds", start["ansible.builtin.set_fact"]["_coverage_query_start"])
        self.assertNotIn("no_log", start)

        loki = queries["Query the exact Loki target selector"]["ansible.builtin.command"]["argv"][-1]
        self.assertIn("(_coverage_query_start | int) * 1000000000", loki)
        self.assertIn("(_coverage_query_end | int) * 1000000000", loki)
        prom = queries["Query the exact Prometheus target selector"]["ansible.builtin.command"]["argv"][-1]
        self.assertIn("observation_window_seconds ~ 's]", prom)
        self.assertIn("&time={{ _coverage_query_end }}", prom)
        tempo = queries["Query the exact Tempo target selector"]["ansible.builtin.command"]["argv"][-1]
        self.assertIn("&start={{ _coverage_query_start }}&end={{ _coverage_query_end }}", tempo)
        profile = queries["Query the exact Pyroscope profile selector"]["ansible.builtin.command"]["argv"][-2]
        self.assertIn("(_coverage_query_start | int) * 1000", profile)
        self.assertIn("(_coverage_query_end | int) * 1000", profile)

        normalize = next(
            task for task in tasks if task["name"] == "Protect the selected backend response for common validation"
        )
        self.assertIs(normalize["no_log"], True)
        observed = next(
            task for task in tasks if task["name"] == "Capture the strict observation time after the backend response"
        )
        self.assertIn("now(utc=true).timestamp() | int", observed["ansible.builtin.set_fact"]["_coverage_observed_at"])
        self.assertNotIn("no_log", observed)
        self.assertGreater(tasks.index(observed), max(tasks.index(task) for task in queries.values()))

        validator = next(
            task for task in tasks
            if task["name"] == "Validate the strict signal response without exposing backend content"
        )
        self.assertIs(validator["no_log"], True)
        self.assertEqual(validator["ansible.builtin.command"]["argv"][-1], "--validate-signal-observation")
        self.assertEqual(validator["delegate_to"], "localhost")
        for field in (
            "response", "command_rc", "target_id", "signal", "query_start", "query_end", "observed_at",
            "window_seconds", "requested_freshness_seconds", "declared_freshness_seconds",
        ):
            self.assertIn(f"'{field}'", validator["ansible.builtin.command"]["stdin"])
        parse = next(task for task in tasks if task["name"] == "Parse the protected signal validation response")
        self.assertIs(parse["no_log"], True)
        schema = next(task for task in tasks if task["name"] == "Validate the fixed signal validation response schema")
        self.assertIs(schema["no_log"], True)
        schema_checks = schema["ansible.builtin.assert"]["that"]
        self.assertIn("_coverage_validation.keys() | list | sort == ['evidence_count', 'reason', 'status']", schema_checks)
        self.assertTrue(any("_coverage_validation.reason in" in check for check in schema_checks))
        self.assertIn("0 <= _coverage_validation.evidence_count <= 1000", schema_checks)
        promote = next(task for task in tasks if task["name"] == "Promote only fixed validation fields")
        self.assertIs(promote["no_log"], True)
        task_source = (ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text()
        self.assertIn("# A malformed parse can echo the protected validator stdout.", task_source)
        assertion = next(task for task in tasks if task["name"] == "Require the strict signal observation")
        self.assertEqual(
            assertion["ansible.builtin.assert"]["that"], "_coverage_observation_status == 'verified'"
        )
        self.assertEqual(
            assertion["ansible.builtin.assert"]["fail_msg"],
            "Strict signal observation refused: {{ _coverage_observation_reason }}.",
        )

        output = next(task for task in tasks if task.get("name") == "Emit sanitized unattributed observation metadata")
        observation = output["ansible.builtin.set_stats"]["data"]["o11y_coverage_observation"]
        self.assertEqual(observation["status"], "observed")
        self.assertIs(observation["task_reference_unattributed"], True)
        self.assertNotIn("receipt_reference", observation)
        self.assertIn("_coverage_query_end", observation["query_ended_at"])
        self.assertIn("_coverage_observed_at", observation["observed_at"])
        self.assertIn(
            "'%Y-%m-%dT%H:%M:%SZ' | strftime(_coverage_query_end | int, utc=true)",
            observation["query_ended_at"],
        )
        self.assertIn(
            "'%Y-%m-%dT%H:%M:%SZ' | strftime(_coverage_observed_at | int, utc=true)",
            observation["observed_at"],
        )
        self.assertGreater(tasks.index(output), tasks.index(assertion))

    def test_strict_signal_refusal_reports_only_a_valid_fixed_reason(self):
        ansible_playbook = shutil.which("ansible-playbook")
        self.assertIsNotNone(ansible_playbook)
        source_tasks = yaml.safe_load(
            (ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text()
        )
        selected_names = {
            "Parse the protected signal validation response",
            "Validate the fixed signal validation response schema",
            "Promote only fixed validation fields",
            "Require the strict signal observation",
        }
        validation_tasks = [task for task in source_tasks if task.get("name") in selected_names]
        self.assertEqual({task["name"] for task in validation_tasks}, selected_names)

        def run(validation_response):
            with tempfile.TemporaryDirectory(prefix="o11y-signal-reason-") as directory:
                temp = Path(directory)
                inventory = temp / "inventory.yml"
                playbook = temp / "validate.yml"
                local_tmp = temp / "local"
                remote_tmp = temp / "remote"
                local_tmp.mkdir()
                remote_tmp.mkdir()
                inventory.write_text("localhost ansible_connection=local\n", encoding="utf-8")
                playbook.write_text(
                    yaml.safe_dump([{
                        "hosts": "localhost",
                        "gather_facts": False,
                        "tasks": [{
                            "ansible.builtin.set_fact": {
                                "_validated_observation": {
                                    "stdout": json.dumps(validation_response),
                                },
                            },
                        }, *validation_tasks],
                    }], sort_keys=False),
                    encoding="utf-8",
                )
                environment = os.environ.copy()
                environment.update(
                    ANSIBLE_LOCAL_TEMP=str(local_tmp),
                    ANSIBLE_REMOTE_TEMP=str(remote_tmp),
                    ANSIBLE_STDOUT_CALLBACK="default",
                    ANSIBLE_NOCOLOR="1",
                )
                return subprocess.run(
                    [ansible_playbook, "-i", str(inventory), str(playbook)],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )

        no_evidence = run({"status": "refused", "reason": "no_evidence", "evidence_count": 0})
        self.assertNotEqual(no_evidence.returncode, 0)
        self.assertIn("Strict signal observation refused: no_evidence.", no_evidence.stdout + no_evidence.stderr)

        canary = "private-selector-and-backend-body"
        injected = run({"status": "refused", "reason": canary, "evidence_count": 0})
        self.assertNotEqual(injected.returncode, 0)
        self.assertNotIn(canary, injected.stdout + injected.stderr)
        self.assertIn("invalid fixed response schema", injected.stdout + injected.stderr)

    def test_profile_query_uses_the_common_protected_signal_validator(self):
        tasks = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text())
        query = next(task for task in tasks if task.get("name") == "Query the exact Pyroscope profile selector")
        argv = query["ansible.builtin.command"]["argv"]
        self.assertIn("--post-data=", argv[-2])
        self.assertIn("--header=Content-Type: application/json", argv)
        self.assertIn("'limit': 1", argv[-2])
        self.assertIn("'step': 60", argv[-2])
        self.assertIn("'groupBy': ['target_id']", argv[-2])
        census_source = (ROOT / "platform/playbooks/files/o11y-coverage-census.py").read_text()
        self.assertIn('if label["name"] == "target_id"', census_source)
        self.assertEqual(argv[-1], "http://pyroscope:4040/querier.v1.QuerierService/SelectSeries")
        self.assertIs(query["no_log"], True)
        parser = next(
            task for task in tasks
            if task.get("name") == "Validate the strict signal response without exposing backend content"
        )
        self.assertIs(parser["no_log"], True)
        self.assertEqual(parser["delegate_to"], "localhost")
        self.assertIn("--validate-signal-observation", parser["ansible.builtin.command"]["argv"])
        self.assertNotIn("--validate-profile-receipt", parser["ansible.builtin.command"]["argv"])
        templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
        receipt_template = next(
            template for template in templates
            if template.get("name") == "Verify o11y Target Receipt"
        )
        signal_field = next(
            field for field in receipt_template["survey_vars"]
            if field.get("name") == "expected_signal"
        )
        self.assertIn("profiles", [value["value"] for value in signal_field["values"]])

    def test_profiles_require_the_exact_single_target_label_selector(self):
        plays = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-service.yml").read_text())
        strict = next(play for play in plays if any(
            task.get("name") == "Refuse selectors without the bounded target identity label"
            for task in play.get("tasks", [])
        ))
        selector_task = next(
            task for task in strict["tasks"]
            if task.get("name") == "Refuse selectors without the bounded target identity label"
        )
        condition = next(
            condition for condition in selector_task["ansible.builtin.assert"]["that"]
            if "expected_signal != 'profiles'" in condition
        )
        env = jinja2.Environment()

        def accepts(selector):
            return env.from_string("{{ " + condition + " }}").render(
                expected_signal="profiles", expected_target_id="service:alpha", signal_selector=selector,
            ) == "True"

        self.assertTrue(accepts('{target_id="service:alpha"}'))
        self.assertFalse(accepts('{target_id="service:alpha",service_name="o11y"}'))
        self.assertFalse(accepts('{target_id="service:beta"}'))

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
        validator = next(
            task for task in tasks
            if task.get("name") == "Validate the strict signal response without exposing backend content"
        )
        self.assertIn("expected_signal", validator["ansible.builtin.command"]["stdin"])
        self.assertIn("--validate-signal-observation", validator["ansible.builtin.command"]["argv"])

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

    def test_strict_trace_producer_feeds_frozen_bounds_to_common_validator(self):
        tasks = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-coverage-target-receipt.yml").read_text())
        validator = next(
            task for task in tasks
            if task.get("name") == "Validate the strict signal response without exposing backend content"
        )
        self.assertIn("query_start", validator["ansible.builtin.command"]["stdin"])
        self.assertIn("query_end", validator["ansible.builtin.command"]["stdin"])
        tempo = next(task for task in tasks if task.get("name") == "Query the exact Tempo target selector")
        self.assertEqual(tempo["when"], "expected_signal == 'traces'")


if __name__ == "__main__":
    unittest.main()
