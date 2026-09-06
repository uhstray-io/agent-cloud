"""Execute the real controller playbook against disposable provider fixtures."""

import copy
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAME = "Store tududi API Token (Dev)"
TEMPLATE = {
    "id": 206,
    "name": NAME,
    "project_id": 1,
    "repository_id": 5,
    "inventory_id": 37,
    "environment_id": 42,
    "playbook": "platform/playbooks/store-tududi-api-token.yml",
    "app": "ansible",
    "arguments": '["--limit", "tududi"]',
    "autorun": False,
    "view_id": 9,
    "survey_vars": [],
}


class ScopedPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.access_value = "fixture-" + "access-value"
        cls.login_value = "fixture-" + "login-value"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, value, status=200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_GET(self):  # noqa: N802
                cls.requests.append(("GET", self.path))
                if self.path == "/v1/secret/data/services/semaphore":
                    if cls.deny_secret:
                        return self.reply({"errors": [cls.login_value]}, 403)
                    return self.reply({"data": {"data": {"api_token": cls.access_value}}})
                if self.headers.get("Authorization") != f"Bearer {cls.access_value}":
                    return self.reply({}, 403)
                if self.path.endswith("/repositories"):
                    return self.reply(cls.repositories)
                if self.path.endswith("/templates"):
                    # List projections need not contain the complete writable record.
                    return self.reply([{k: v for k, v in row.items() if k not in {"description", "survey_vars"}}
                                       for row in cls.records])
                for row in cls.records:
                    if self.path == f"/api/project/1/templates/{row['id']}":
                        return self.reply(row)
                if self.path.endswith("/schedules"):
                    return self.reply([])
                return self.reply({}, 404)

            def do_POST(self):  # noqa: N802
                cls.requests.append(("POST", self.path))
                if self.path == "/v1/auth/approle/login":
                    return self.reply({"auth": {"client_token": cls.login_value}})
                cls.writes.append(("POST", self.path))
                if self.path == "/api/project/1/templates":
                    value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    value["id"] = 300
                    cls.records.append(value)
                    return self.reply({}, 201)
                return self.reply({}, 405)

            def do_PUT(self):  # noqa: N802
                cls.requests.append(("PUT", self.path))
                cls.writes.append(("PUT", self.path))
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/project/1/templates/206":
                    return self.reply({}, 400)
                if not cls.ignore_write:
                    # Match SurveyVar's documented Go omitempty serialization.
                    value["survey_vars"] = [
                        {k: v for k, v in survey.items() if v is not False and v != ""} | {"values": None}
                        for survey in value["survey_vars"]
                    ]
                    if cls.drop_setting:
                        value.pop("description", None)
                    cls.records[0] = value
                return self.reply({})

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.scratch.cleanup()

    def setUp(self):
        cls = type(self)
        cls.records = [copy.deepcopy(TEMPLATE), {"id": 99, "name": "Unrelated", "survey_vars": [{"name": "keep"}]}]
        cls.repositories = [
            {"id": 1, "name": "agent-cloud", "git_branch": "main",
             "git_url": "https://github.com/uhstray-io/agent-cloud.git"},
            {"id": 5, "name": "agent-cloud dev", "git_branch": "dev",
             "git_url": "https://github.com/uhstray-io/agent-cloud.git"},
        ]
        cls.requests = []
        cls.writes = []
        cls.ignore_write = False
        cls.drop_setting = False
        cls.deny_secret = False

    def run_play(self, selection=None, bootstrap=False, controller_wrapper=False, **overrides):
        extra = {
            "_semaphore_url": self.endpoint,
            "semaphore_template_names_json": json.dumps([NAME] if selection is None else selection),
            "semaphore_template_names": [NAME] if selection is None else selection,
            "openbao_addr": self.endpoint,
        } | overrides
        env = os.environ.copy()
        env.update(
            SEMAPHORE_TOKEN="",
            BAO_ROLE_ID="fixture-role",
            BAO_SECRET_ID="fixture-secret",
            BAO_TOKEN="fixture-unusable-workstation-login",
            BAO_ADDR=self.endpoint,
            ANSIBLE_LOCAL_TEMP=self.scratch.name,
            ANSIBLE_REMOTE_TEMP=self.scratch.name,
            ANSIBLE_STDOUT_CALLBACK="default",
            ANSIBLE_NOCOLOR="1",
        )
        playbook = ("platform/semaphore/bootstrap-survey-publisher.yml" if bootstrap
                    else "platform/semaphore/setup-templates.yml")
        if controller_wrapper:
            playbook = "platform/playbooks/publish-semaphore-templates.yml"
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", playbook,
             "-e", json.dumps(extra)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90,
        )
        output = result.stdout + result.stderr
        for secret in (self.access_value, self.login_value, env["BAO_SECRET_ID"]):
            self.assertNotIn(secret, output)
        return result.returncode, output

    def test_bootstrap_installs_only_publisher_with_explicit_bindings(self):
        original = copy.deepcopy(self.records)
        bindings = {"semaphore_project_id": 1, "semaphore_inventory_id": 37, "semaphore_environment_id": 42}
        code, output = self.run_play(bootstrap=True, **bindings)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.writes, [("POST", "/api/project/1/templates")])
        self.assertEqual(self.records[:2], original)
        self.assertEqual(self.records[2]["name"], "Publish Semaphore Template Surveys (Dev)")
        self.assertEqual(self.records[2]["repository_id"], 5)
        self.assertEqual(self.records[2]["inventory_id"], 37)
        self.assertEqual(self.records[2]["environment_id"], 42)
        code, output = self.run_play(bootstrap=True, **bindings)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.writes), 1)

    def test_bootstrap_without_explicit_bindings_refuses_before_network(self):
        code, _ = self.run_play(bootstrap=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.requests, [])

    def test_controller_publishes_one_survey_and_rerun_is_noop(self):
        self.records[0]["description"] = "preserve detail-only setting"
        original = copy.deepcopy(self.records)
        code, output = self.run_play()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.writes, [("PUT", "/api/project/1/templates/206")])
        self.assertEqual(self.records[1], original[1])
        self.assertEqual(self.records[0] | {"survey_vars": []}, original[0])
        self.assertEqual(self.records[0]["survey_vars"][0]["default_value"], "true")
        self.assertIn(("POST", "/v1/auth/approle/login"), self.requests)
        self.assertIn(("GET", "/v1/secret/data/services/semaphore"), self.requests)
        self.assertFalse(any("schedules" in path for _, path in self.requests))
        code, output = self.run_play()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.writes), 1, "Identical second publication must perform no writes")

    def test_invalid_scope_refuses_before_network(self):
        for selection in ([], "all", [NAME, NAME], ["missing"]):
            with self.subTest(selection=selection):
                code, _ = self.run_play(selection)
                self.assertNotEqual(code, 0)
                self.assertEqual(self.requests, [])

    def test_ambiguous_missing_and_wrong_bindings_refuse_before_write(self):
        for change in ("duplicate", "missing", "repository", "playbook"):
            with self.subTest(change=change):
                self.setUp()
                if change == "duplicate":
                    self.records.append(copy.deepcopy(TEMPLATE))
                elif change == "missing":
                    self.records.pop(0)
                elif change == "repository":
                    self.records[0]["repository_id"] = 1
                else:
                    self.records[0]["playbook"] = "unrelated.yml"
                code, _ = self.run_play()
                self.assertNotEqual(code, 0)
                self.assertEqual(self.writes, [])

    def test_repository_branch_drift_refuses_before_write(self):
        self.repositories[1]["git_branch"] = "main"
        code, output = self.run_play()
        self.assertNotEqual(code, 0)
        self.assertIn("differs from repositories.yml", output)
        self.assertEqual(self.writes, [])

    def test_success_response_without_saved_survey_fails_readback(self):
        type(self).ignore_write = True
        code, output = self.run_play()
        self.assertNotEqual(code, 0)
        self.assertIn("Scoped publication readback failed", output)

    def test_lost_detail_only_setting_fails_readback(self):
        self.records[0]["description"] = "preserve this setting"
        type(self).drop_setting = True
        code, output = self.run_play()
        self.assertNotEqual(code, 0)
        self.assertIn("Scoped publication readback failed", output)

    def test_controller_rejects_https_destination_override_before_network(self):
        code, output = self.run_play(controller_wrapper=True, _semaphore_url="https://collector.example.test")
        self.assertNotEqual(code, 0)
        self.assertIn("fixed loopback API destination", output)
        self.assertEqual(self.requests, [])

    def test_denied_runtime_read_is_named_and_secret_free(self):
        type(self).deny_secret = True
        code, output = self.run_play()
        self.assertNotEqual(code, 0)
        self.assertIn("Runtime Semaphore access is unavailable", output)
        self.assertEqual(self.writes, [])

    def test_public_cleartext_refuses_before_authentication(self):
        code, _ = self.run_play(_semaphore_url="http://semaphore.example.test")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
