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
                    if cls.deny_secret or self.headers.get("X-Vault-Token") != cls.login_value:
                        return self.reply({"errors": [cls.login_value]}, 403)
                    return self.reply({"data": {"data": {"api_token": cls.access_value}}})
                if self.headers.get("Authorization") != f"Bearer {cls.access_value}":
                    return self.reply({}, 403)
                if self.path.endswith("/repositories"):
                    return self.reply(cls.repositories)
                if self.path.endswith("/environment"):
                    return self.reply(cls.environments)
                if self.path.endswith("/tasks/last"):
                    return self.reply(cls.active_tasks)
                for row in cls.environments:
                    if self.path == f"/api/project/1/environment/{row['id']}":
                        # v2.18.12 omits an empty `secrets` slice on single GET (omitempty).
                        return self.reply({k: v for k, v in row.items() if k != "secrets" or v != []})
                if self.path.endswith("/templates"):
                    # List projections need not contain the complete writable record.
                    return self.reply([{k: v for k, v in row.items() if k not in {"description", "survey_vars"}}
                                       for row in cls.records])
                for row in cls.records:
                    if self.path == f"/api/project/1/templates/{row['id']}":
                        return self.reply(row)
                if self.path.endswith("/schedules"):
                    return self.reply(cls.schedules)
                return self.reply({}, 404)

            def do_POST(self):  # noqa: N802
                cls.requests.append(("POST", self.path))
                if self.path == "/v1/auth/approle/login":
                    return self.reply({"auth": {"client_token": cls.login_value}})
                cls.writes.append(("POST", self.path))
                if self.path == "/api/project/1/environment":
                    value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    value["id"] = 500 + len(cls.environments)
                    cls.environments.append(value)
                    return self.reply(value, 201)
                if self.path == "/api/project/1/schedules":
                    value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    cls.schedules.append(value | {"id": 400})
                    return self.reply({}, 201)
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
                for index, row in enumerate(cls.environments):
                    if self.path == f"/api/project/1/environment/{row['id']}":
                        if cls.ignore_write:
                            return self.reply({})
                        # Semaphore v2.17.31 api/projects/environment.go:
                        # updateEnvironmentSecrets applies explicit operations; [] does nothing.
                        existing = copy.deepcopy(row.get("secrets", []))
                        for operation in value["secrets"]:
                            if operation["operation"] != "create":
                                return self.reply({}, 400)
                            cls.auth_values[operation["name"]] = operation["secret"]
                            existing.append({"id": 700 + len(existing), "name": operation["name"], "type": "env"})
                        cls.environments[index] = value | {"secrets": existing}
                        if cls.template_drift:
                            cls.records[0]["description"] = "fixture-sensitive-description"
                        return self.reply({})
                if self.path == "/api/project/1/schedules/400":
                    cls.schedules[0] = value
                    return self.reply({})
                if self.path != "/api/project/1/templates/206":
                    return self.reply({}, 400)
                if not cls.ignore_write:
                    # Match SurveyVar's documented Go omitempty serialization.
                    value["survey_vars"] = [
                        {k: v for k, v in survey.items() if v is not False and v != ""} | {"values": None}
                        for survey in value.get("survey_vars", [])
                    ]
                    if cls.drop_setting:
                        value.pop("description", None)
                    cls.records[0] = value
                return self.reply({})

        # socketserver's default listen backlog is 5. A playbook issues requests in quick
        # bursts, and on a loaded machine the sixth queued connect was reset ("Connection
        # reset by peer"), failing a different test each run. A fixture must not be the
        # flaky part of the test.
        class Server(ThreadingHTTPServer):
            request_queue_size = 128
            daemon_threads = True

        cls.server = Server(("127.0.0.1", 0), Handler)
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
        cls.records = [copy.deepcopy(TEMPLATE), {"id": 99, "name": "Unrelated", "environment_id": 0,
                                                    "survey_vars": [{"name": "keep"}]}]
        cls.repositories = [
            {"id": 1, "name": "agent-cloud", "git_branch": "main",
             "git_url": "https://github.com/uhstray-io/agent-cloud.git"},
            {"id": 5, "name": "agent-cloud dev", "git_branch": "dev",
             "git_url": "https://github.com/uhstray-io/agent-cloud.git"},
        ]
        cls.requests = []
        cls.writes = []
        cls.schedules = []
        cls.ignore_write = False
        cls.drop_setting = False
        cls.template_drift = False
        cls.deny_secret = False
        cls.environments = []
        cls.active_tasks = []
        cls.auth_values = {}

    def run_play(self, selection=None, bootstrap=False, controller_wrapper=False, full_catalog=False,
                 provision=False, provision_wrapper=False, **overrides):
        extra = {
            "_semaphore_url": self.endpoint,
            "semaphore_template_names_json": json.dumps([NAME] if selection is None else selection),
            "semaphore_template_names": [NAME] if selection is None else selection,
            "openbao_addr": self.endpoint,
        } | overrides
        if full_catalog:
            extra.pop("semaphore_template_names")
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
        if provision:
            directory = "playbooks" if provision_wrapper else "semaphore"
            playbook = f"platform/{directory}/provision-seed-environment.yml"
            extra.update(semaphore_project_id=1, semaphore_inventory_id=37, semaphore_source_environment_id=42)
            extra.setdefault("seed_template", "Seed Postiz Secrets")
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", playbook,
             "-e", json.dumps(extra)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90,
        )
        output = result.stdout + result.stderr
        for secret in (self.access_value, self.login_value, env["BAO_SECRET_ID"]):
            self.assertNotIn(secret, output)
        return result.returncode, output

    def test_full_catalog_always_publishes_templates_and_upserts_schedules(self):
        declaration = {"name": NAME, "repository": "agent-cloud dev", "playbook": TEMPLATE["playbook"],
                       "survey_vars": [], "schedule": {"cron": "*/45 * * * *"}}
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.writes, [("PUT", "/api/project/1/templates/206"),
                                       ("POST", "/api/project/1/schedules")])
        self.assertEqual(self.schedules[0]["template_id"], 206)
        self.assertEqual(self.schedules[0]["cron_format"], "*/45 * * * *")
        declaration["schedule"]["cron"] = "*/30 * * * *"
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.writes[2:], [("PUT", "/api/project/1/templates/206"),
                                         ("PUT", "/api/project/1/schedules/400")])
        self.assertEqual(len(self.schedules), 1)
        self.assertEqual(self.schedules[0]["cron_format"], "*/30 * * * *")

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

    def test_declared_environment_survives_full_publication_without_credential_changes(self):
        declaration = {"name": NAME, "repository": "agent-cloud dev", "playbook": TEMPLATE["playbook"],
                       "isolated_environment": "Isolated inputs"}
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.records[0]["environment_id"], 500)
        self.assertEqual(self.environments[0]["json"], "{}")
        self.assertEqual(self.environments[0]["secrets"], [])
        # A provisioned environment: both AppRole inputs (one alone is refused as unsafe).
        self.environments[0]["secrets"] = [{"id": 70, "name": "BAO_SECRET_ID", "type": "env"},
                                           {"id": 71, "name": "BAO_ROLE_ID", "type": "env"}]
        before = copy.deepcopy(self.environments)
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.records[0]["environment_id"], 500)
        self.assertEqual(self.environments, before)
        self.assertEqual(self.writes.count(("POST", "/api/project/1/environment")), 1)

    def test_isolated_environment_refuses_other_owner_and_active_work_before_writes(self):
        declaration = {"name": NAME, "repository": "agent-cloud dev", "playbook": TEMPLATE["playbook"],
                       "isolated_environment": "Isolated inputs"}
        type(self).environments = [{"id": 500, "project_id": 1, "name": "Isolated inputs",
                                    "json": "{}", "env": "{}", "secrets": []}]
        self.records[1]["environment_id"] = 500
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertNotEqual(code, 0)
        self.assertIn("ambiguous or used by another template", output)
        self.assertEqual(self.writes, [])
        self.records[1].pop("environment_id")
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertNotEqual(code, 0)
        self.assertIn("Incomplete or multi-environment ownership metadata", output)
        self.assertEqual(self.writes, [])

        self.records[1]["environment_id"] = 0
        self.active_tasks.append({"template_id": 206, "status": "rejected"})
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertNotEqual(code, 0)
        self.assertIn("unfinished work", output)
        self.assertEqual(self.writes, [])

    def test_isolated_environment_accepts_matching_list_and_refuses_multiple_bindings(self):
        declaration = {"name": NAME, "repository": "agent-cloud dev", "playbook": TEMPLATE["playbook"],
                       "isolated_environment": "Isolated inputs"}
        for row in self.records:
            row["environment_ids"] = [row["environment_id"]]
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.records[0]["environment_id"], self.environments[0]["id"])

        self.setUp()
        self.records[0]["environment_ids"] = [42, 500]
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertNotEqual(code, 0)
        self.assertIn("Incomplete or multi-environment ownership metadata", output)
        self.assertEqual(self.writes, [])

        self.setUp()
        self.records[0]["environment_ids"] = [43]
        self.records[0]["arguments"] = "fixture-sensitive-arguments"
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertNotEqual(code, 0)
        self.assertIn("Incomplete or multi-environment ownership metadata", output)
        self.assertNotIn("fixture-sensitive-arguments", output)
        self.assertEqual(self.writes, [])

        self.setUp()
        self.records[0]["environment_id"] = None
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertNotEqual(code, 0)
        self.assertIn("Incomplete or multi-environment ownership metadata", output)
        self.assertEqual(self.writes, [])

    def test_full_publication_refuses_to_bind_an_isolated_environment_that_is_not_clean(self):
        # Review of PR #205: an existing environment with the declared name was bound as-is.
        declaration = {"name": NAME, "repository": "agent-cloud dev", "playbook": TEMPLATE["playbook"],
                       "isolated_environment": "Isolated inputs"}
        cases = {
            "leftover inputs: BAO_VALUE": [{"id": 1, "name": "BAO_VALUE", "type": "env"}],
            "only one AppRole input": [{"id": 1, "name": "BAO_ROLE_ID", "type": "env"}],
            "leftover inputs: SEED_X_API_KEY": [{"id": 1, "name": "SEED_X_API_KEY", "type": "env"}],
        }
        # The AppRole login would go to the environment's openbao_addr: an address other than
        # the controller's own is refused before binding (review of PR #205).
        endpoint_case = {"id": 500, "project_id": 1, "name": "Isolated inputs", "env": "{}",
                         "json": '{"openbao_addr":"https://elsewhere.example:8200"}',
                         "secrets": [{"id": 1, "name": "BAO_ROLE_ID", "type": "env"},
                                     {"id": 2, "name": "BAO_SECRET_ID", "type": "env"}]}
        with self.subTest(message="endpoint"):
            self.setUp()
            type(self).environments = [endpoint_case]
            before = copy.deepcopy(self.records)
            code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
            self.assertNotEqual(code, 0, output)
            self.assertIn("OpenBao endpoint differs from the approved endpoint", output)
            self.assertEqual(self.writes, [])
            self.assertEqual(self.records, before)
        for message, secrets in cases.items():
            with self.subTest(message=message):
                self.setUp()
                type(self).environments = [{"id": 500, "project_id": 1, "name": "Isolated inputs",
                                            "json": "{}", "env": "{}", "secrets": secrets}]
                before = copy.deepcopy(self.records)
                code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
                self.assertNotEqual(code, 0, output)
                self.assertIn(message, output)
                self.assertEqual(self.writes, [])
                self.assertEqual(self.records, before)

    def test_generated_variant_gets_distinct_declared_environment(self):
        declaration = {"name": "Seed fixture", "repository": "agent-cloud dev",
                       "playbook": TEMPLATE["playbook"], "_generated": True,
                       "isolated_environment": "Isolated inputs"}
        code, output = self.run_play(full_catalog=True, _all_templates=[declaration])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.environments[0]["name"], "Isolated inputs (Dev)")
        self.assertEqual(self.records[-1]["environment_id"], self.environments[0]["id"])

    def test_scoped_surveys_do_not_create_or_change_declared_environment(self):
        declaration = {"name": NAME, "repository": "agent-cloud dev", "playbook": TEMPLATE["playbook"],
                       "isolated_environment": "Isolated inputs", "survey_vars": []}
        code, output = self.run_play(_all_templates=[declaration])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.records[0]["environment_id"], TEMPLATE["environment_id"])
        self.assertEqual(self.environments, [])
        self.assertFalse(any(path.endswith("/environment") for _, path in self.requests))

    def prepare_seed_template(self):
        self.records[0].update(name="Seed Postiz Secrets (Dev)",
                               playbook="platform/playbooks/seed-postiz-secrets.yml", arguments="[]")
        self.records[0]["description"] = "preserve this detail-only field"
        self.records[0]["survey_vars"] = [{"name": "postiz_verify_access_only", "type": "string", "values": None}]
        self.environments.append({"id": 42, "project_id": 1, "name": "shared", "json": '{"unrelated":"keep"}',
                                  "env": "{}", "secrets": [{"id": 71, "name": "UNRELATED", "type": "env"}]})

    def test_provisioner_encrypts_runtime_auth_preserves_source_and_converges(self):
        self.prepare_seed_template()
        original = copy.deepcopy(self.records[0])
        source = copy.deepcopy(self.environments[0])
        code, output = self.run_play(provision=True)
        self.assertEqual(code, 0, output)
        target = self.environments[1]
        self.assertEqual(self.environments[0], source)
        self.assertEqual(json.loads(target["json"]), {"openbao_addr": self.endpoint})
        self.assertEqual(self.auth_values, {"BAO_ROLE_ID": "fixture-role", "BAO_SECRET_ID": "fixture-secret"})
        self.assertEqual(self.records[0], original | {"environment_id": target["id"]})
        writes = copy.deepcopy(self.writes)
        code, output = self.run_play(provision=True)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.writes, writes)

    def test_provisioner_refuses_a_template_without_isolated_environment(self):
        self.prepare_seed_template()
        code, output = self.run_play(provision=True, seed_template="Deploy agentgateway")
        self.assertNotEqual(code, 0)
        self.assertIn("seed_template must name a templates.yml entry that declares", output)
        self.assertEqual(self.writes, [])

    def test_provisioner_isolates_the_openbao_key_seed(self):
        self.prepare_seed_template()
        self.records[0].update(name="Seed OpenBao Key (Dev)", playbook="platform/playbooks/seed-openbao-key.yml")
        self.records[0]["survey_vars"] = [{"name": n, "type": "string", "values": None}
                                          for n in ("bao_path", "bao_key", "bao_verify_access_only")]
        source = copy.deepcopy(self.environments[0])
        code, output = self.run_play(provision=True, seed_template="Seed OpenBao Key")
        self.assertEqual(code, 0, output)
        target = self.environments[1]
        self.assertEqual(target["name"], "OpenBao key seed inputs (Dev)")
        self.assertEqual(self.environments[0], source)
        self.assertEqual(self.auth_values, {"BAO_ROLE_ID": "fixture-role", "BAO_SECRET_ID": "fixture-secret"})
        self.assertEqual(self.records[0]["environment_id"], target["id"])
        self.assertIn("bao_verify_access_only=true", output)

    def test_provisioner_main_variant_resolves_repository_without_shadowing(self):
        self.prepare_seed_template()
        self.records[0].update(name="Seed Postiz Secrets", repository_id=1)
        code, output = self.run_play(provision=True, seed_variant="main")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.records[0]["environment_id"], 501)
        self.assertEqual(self.environments[1]["name"], "Postiz seed inputs")

    def test_provisioner_preserves_auth_when_filling_missing_endpoint(self):
        self.prepare_seed_template()
        secrets = [{"id": 700 + i, "name": name, "type": "env"}
                   for i, name in enumerate(["BAO_ROLE_ID", "BAO_SECRET_ID"])]
        self.environments.append({"id": 501, "project_id": 1, "name": "Postiz seed inputs (Dev)",
                                  "json": "{}", "env": "{}", "secrets": copy.deepcopy(secrets)})
        code, output = self.run_play(provision=True)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.environments[1]["secrets"], secrets)
        self.assertEqual(self.auth_values, {})
        self.assertEqual(json.loads(self.environments[1]["json"]), {"openbao_addr": self.endpoint})
        self.assertIn(("PUT", "/api/project/1/environment/501"), self.writes)

    def test_provisioner_refuses_changed_endpoint_before_writes(self):
        self.prepare_seed_template()
        target = {"id": 501, "project_id": 1, "name": "Postiz seed inputs (Dev)",
                  "json": '{"openbao_addr":"https://different.example.com"}', "env": "{}",
                  "secrets": [{"id": 700 + i, "name": name, "type": "env"}
                              for i, name in enumerate(["BAO_ROLE_ID", "BAO_SECRET_ID"])]}
        self.environments.append(copy.deepcopy(target))
        code, output = self.run_play(provision=True)
        self.assertNotEqual(code, 0)
        # Refused by the shared clean-environment rule, before the provisioner's own check.
        self.assertIn("OpenBao endpoint differs from the approved endpoint", output)
        self.assertEqual(self.environments[1], target)
        self.assertEqual(self.writes, [])

    def test_provisioner_reports_template_drift_without_sensitive_values(self):
        for before_binding in [True, False]:
            with self.subTest(before_binding=before_binding):
                self.setUp()
                self.prepare_seed_template()
                self.records[0]["description"] = "fixture-original-description"
                type(self).template_drift = before_binding
                type(self).drop_setting = not before_binding
                code, output = self.run_play(provision=True)
                self.assertNotEqual(code, 0)
                message = ("Seed template changed; binding refused." if before_binding
                           else "Seed binding readback differs; reconcile before any credential import.")
                failure = output.split("fatal: [localhost]: FAILED! => ", 1)[1]
                result, _ = json.JSONDecoder().raw_decode(failure)
                self.assertEqual(result.get("msg"), message)
                self.assertNotIn("fixture-sensitive-description", output)
                self.assertNotIn("fixture-original-description", output)
                self.assertEqual(("PUT", "/api/project/1/templates/206") in self.writes, not before_binding)

    def test_provisioner_requires_credential_readback_before_rebinding(self):
        self.prepare_seed_template()
        self.ignore_write = True
        type(self).ignore_write = True
        code, _ = self.run_play(provision=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.records[0]["environment_id"], 42)
        self.assertIn(("PUT", "/api/project/1/environment/501"), self.writes)
        self.assertNotIn(("PUT", "/api/project/1/templates/206"), self.writes)

    def test_provisioner_refuses_staged_or_partial_credentials_before_writes(self):
        for name in ["SEED_X_API_KEY", "BAO_ROLE_ID"]:
            with self.subTest(name=name):
                self.setUp()
                self.prepare_seed_template()
                self.environments.append({"id": 501, "project_id": 1, "name": "Postiz seed inputs (Dev)",
                                          "json": "{}", "env": "{}",
                                          "secrets": [{"id": 700, "name": name, "type": "env"}]})
                code, output = self.run_play(provision=True)
                self.assertNotEqual(code, 0)
                self.assertIn("is not safe to bind", output)
                self.assertIn(("GET", "/api/project/1/environment/501"), self.requests)
                self.assertEqual(self.writes, [])

    def test_provisioner_refuses_explicit_null_secret_metadata_before_writes(self):
        self.prepare_seed_template()
        self.environments.append({"id": 501, "project_id": 1, "name": "Postiz seed inputs (Dev)",
                                  "json": "{}", "env": "{}", "secrets": None})
        code, output = self.run_play(provision=True)
        self.assertNotEqual(code, 0)
        self.assertIn("no secrets list; contents cannot be established", output)
        self.assertEqual(self.writes, [])

    def test_provisioner_refuses_a_leftover_seed_value_of_any_name(self):
        # Review of PR #205: only SEED_* was refused, so an interrupted OpenBao-key seed's
        # BAO_VALUE could stay in the environment the template is then bound to. A
        # dedicated seed environment holds exactly the two AppRole inputs; anything else
        # is a leftover.
        for leftover in ["BAO_VALUE", "SOMETHING_ELSE"]:
            with self.subTest(leftover=leftover):
                self.setUp()
                self.prepare_seed_template()
                self.records[0].update(name="Seed OpenBao Key (Dev)",
                                       playbook="platform/playbooks/seed-openbao-key.yml")
                self.records[0]["survey_vars"] = [{"name": n, "type": "string", "values": None}
                                                  for n in ("bao_path", "bao_key", "bao_verify_access_only")]
                self.environments.append({"id": 501, "project_id": 1, "name": "OpenBao key seed inputs (Dev)",
                                          "json": "{}", "env": "{}",
                                          "secrets": [{"id": 700, "name": leftover, "type": "env"}]})
                code, output = self.run_play(provision=True, seed_template="Seed OpenBao Key")
                self.assertNotEqual(code, 0, output)
                self.assertIn(f"leftover inputs: {leftover}", output)
                self.assertEqual(self.writes, [])

    def test_controller_provisioner_refuses_destination_override_before_auth(self):
        code, output = self.run_play(provision=True, provision_wrapper=True)
        self.assertNotEqual(code, 0)
        self.assertIn("fixed loopback API", output)
        self.assertEqual(self.requests, [])

    def test_bootstrap_without_explicit_bindings_refuses_before_network(self):
        code, _ = self.run_play(bootstrap=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.requests, [])

    def test_scoped_create_registers_only_one_declared_template_and_reruns_cleanly(self):
        original = copy.deepcopy(self.records)
        selected = ["Audit o11y Containers (Dev)"]
        bindings = {
            "semaphore_allow_scoped_create": "true",
            "semaphore_project_id": "1",
            "semaphore_inventory_id": "37",
            "semaphore_environment_id": "42",
        }
        code, output = self.run_play(selection=selected, **bindings)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.writes, [("POST", "/api/project/1/templates")])
        self.assertEqual(self.records[:2], original)
        created = self.records[2]
        self.assertEqual(created["name"], selected[0])
        self.assertEqual(created["repository_id"], 5)
        self.assertEqual(created["inventory_id"], 37)
        self.assertEqual(created["environment_id"], 42)
        self.assertEqual(created["playbook"], "platform/playbooks/audit-o11y-containers.yml")
        code, output = self.run_play(selection=selected, **bindings)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.writes), 1)

    def test_scoped_create_of_an_isolated_seed_binds_its_declared_environment(self):
        # Review of PR #205: scoped create skipped isolated-environment resolution, so
        # the create body indexed environment IDs that were never resolved.
        selected = ["Seed OpenBao Key (Dev)"]
        bindings = {"semaphore_allow_scoped_create": True, "semaphore_project_id": 1,
                    "semaphore_inventory_id": 37, "semaphore_environment_id": 42}
        code, output = self.run_play(selection=selected, **bindings)
        self.assertEqual(code, 0, output)
        created = [r for r in self.records if r["name"] == selected[0]]
        self.assertEqual(len(created), 1, output)
        self.assertEqual([e["name"] for e in self.environments], ["OpenBao key seed inputs (Dev)"])
        self.assertEqual(created[0]["environment_id"], self.environments[0]["id"])
        self.assertNotEqual(created[0]["environment_id"], 42)
        self.assertEqual(json.loads(self.environments[0]["json"] or "{}"), {})
        self.assertEqual(self.environments[0]["secrets"], [])

    def test_scoped_create_needs_explicit_bindings_before_network(self):
        code, _ = self.run_play(selection=["Audit o11y Containers (Dev)"],
                                semaphore_allow_scoped_create=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.requests, [])

    def test_controller_publishes_one_survey_and_rerun_is_noop(self):
        self.records[0]["description"] = "preserve detail-only setting"
        original = copy.deepcopy(self.records)
        code, output = self.run_play(semaphore_project_id="", semaphore_inventory_id="",
                                     semaphore_environment_id="")
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

    def test_controller_refuses_create_bindings_on_survey_update(self):
        code, output = self.run_play(controller_wrapper=True, _semaphore_url="http://127.0.0.1:3000",
                                     semaphore_allow_scoped_create="false", semaphore_project_id="2")
        self.assertNotEqual(code, 0)
        self.assertIn("accepted only for one-template creation", output)
        self.assertEqual(self.requests, [])

    def test_controller_refuses_main_bound_template_creation(self):
        code, output = self.run_play(controller_wrapper=True, _semaphore_url="http://127.0.0.1:3000",
                                     selection=["Deploy NetBox"], semaphore_allow_scoped_create="true")
        self.assertNotEqual(code, 0)
        self.assertIn("one declared (Dev) template", output)
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
