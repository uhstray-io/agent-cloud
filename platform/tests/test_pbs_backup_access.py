from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "playbooks" / "files"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pbs = load("inspect_pbs_backup_access", FILES / "inspect_pbs_backup_access.py")
pve = load("reconcile_pbs_pve_storage", FILES / "reconcile_pbs_pve_storage.py")


def effective(privileges=None, path="/datastore/o11y"):
    return [{"path": path, "privileges": privileges if privileges is not None else {"Datastore.Backup": False}}]


def expected_storage(password="secret"):
    return {
        "type": "pbs",
        "server": "pbs.internal.test",
        "port": 8007,
        "datastore": "o11y",
        "username": "o11y-backup@pbs!pve-o11y",
        "password": password,
        "fingerprint": "AA" * 32,
        "content": "backup",
        "nodes": ["node-a"],
        "prune-backups": "keep-all=1",
    }


def existing_storage(password="**********"):
    result = {key: value for key, value in expected_storage(password).items() if key != "prune-backups"}
    result.update({"storage": "pbs-o11y", "nodes": "node-a", "prune-backups": "keep-all=1", "shared": 1})
    return result


def access_proof(node="node-a", storage="pbs-o11y", data=None):
    return {
        "node": node,
        "url": f"https://pve.internal.test/api2/json/nodes/{node}/storage/{storage}/content?content=backup",
        "status": 200,
        "data": [] if data is None else data,
    }


class GeneratedTokenOutputTests(TestCase):
    def test_only_documented_result_envelope_is_normalized_for_fixed_id(self):
        expected_id = pbs.TOKEN_ID
        self.assertEqual(
            pbs.parse_generated_token_output(
                'Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret"}',
                expected_id,
            ),
            {"tokenid": expected_id, "value": "fixture-secret"},
        )

    def test_rejects_ambiguous_malformed_or_unbounded_cli_results_without_echoing_data(self):
        expected_id = pbs.TOKEN_ID
        invalid_outputs = (
            '{"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret"}',
            'prefix Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret"}',
            (
                'Result: {"tokenid":"o11y-backup@pbs!pve-o11y",'
                '"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret"}'
            ),
            'Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret"} trailing',
            'Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret"} {}',
            'Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret","extra":true}',
            'Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":NaN}',
            'Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret\\u0000"}',
            'Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret"}' * 80,
        )
        for output in invalid_outputs:
            with self.subTest(output_length=len(output)), self.assertRaises(pbs.Refusal):
                pbs.parse_generated_token_output(output, expected_id)
        for expected in ([], {}, "caller-selected"):
            with self.subTest(expected=type(expected).__name__), self.assertRaises(pbs.Refusal):
                pbs.parse_generated_token_output(
                    'Result: {"tokenid":"o11y-backup@pbs!pve-o11y","value":"fixture-secret"}',
                    expected,
                )

    def test_cli_refusal_does_not_echo_minted_secret(self):
        private_fixture = "synthetic-private-token-value"
        completed = subprocess.run(
            [sys.executable, str(FILES / "inspect_pbs_backup_access.py"), "parse-generated-token"],
            input=json.dumps({
                "stdout": f'Result: {{"tokenid":"{pbs.TOKEN_ID}","value":"{private_fixture}"}}',
                "expected_token_id": pbs.COPY_READER_TOKEN_ID,
            }),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(private_fixture, completed.stdout + completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"refusal": "PBS generated-token result does not match its fixed identity."},
        )

    def test_fake_pbs_cli_default_envelope_flows_through_production_helper_for_all_identities(self):
        fake_cli_script = (
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "if args[:2] != ['user', 'generate-token'] or '--output-format' in args:\n"
            "    raise SystemExit(2)\n"
            "user, token_name = args[2:4]\n"
            "print('Result: ' + json.dumps({'tokenid': f'{user}!{token_name}', 'value': 'fixture-secret'}))\n"
        )
        caller_paths = (
            ROOT / "playbooks" / "tasks" / "configure-pbs-backup-identity.yml",
            ROOT / "playbooks" / "tasks" / "configure-pbs-copy-reader-identity.yml",
            ROOT / "playbooks" / "tasks" / "configure-pbs-copy-manager-identity.yml",
            ROOT / "playbooks" / "tasks" / "configure-pbs-copy-restore-identity.yml",
        )
        helper_path = FILES / "inspect_pbs_backup_access.py"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fake_cli = temp / "proxmox-backup-manager"
            fake_cli.write_text(fake_cli_script)
            fake_cli.chmod(0o700)
            env = {**os.environ, "PATH": f"{temp}{os.pathsep}{os.environ.get('PATH', '')}"}
            observed_ids = set()
            for task_path in caller_paths:
                tasks = yaml.safe_load(task_path.read_text())
                mint = next(
                    task for task in tasks
                    if task.get("name", "").startswith("Mint the stable")
                    or task.get("name", "").startswith("Mint the generated")
                )
                argv = mint["ansible.builtin.command"]["argv"]
                self.assertEqual(argv[:3], ["proxmox-backup-manager", "user", "generate-token"])
                self.assertNotIn("--output-format", argv)
                cli_result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
                self.assertEqual(cli_result.returncode, 0)
                expected_id = f"{argv[3]}!{argv[4]}"
                observed_ids.add(expected_id)
                parse_task = next(
                    task for task in tasks
                    if "parse-generated-token" in task.get("ansible.builtin.command", {}).get("argv", [])
                )
                template_env = Environment()
                template_env.filters["to_json"] = json.dumps
                task_stdin = template_env.from_string(
                    parse_task["ansible.builtin.command"]["stdin"]
                ).render(**{mint["register"]: {"stdout": cli_result.stdout}})
                task_payload = json.loads(task_stdin)
                self.assertEqual(task_payload["expected_token_id"], expected_id)
                parser_result = subprocess.run(
                    [sys.executable, str(helper_path), "parse-generated-token"],
                    input=task_stdin,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(parser_result.returncode, 0, parser_result.stderr)
                self.assertEqual(
                    json.loads(parser_result.stdout),
                    {"tokenid": expected_id, "value": "fixture-secret"},
                )
            self.assertEqual(observed_ids, pbs.GENERATED_TOKEN_IDS)

    def test_each_token_mint_uses_parser_before_persist_and_acl_grants(self):
        for task_path in (
            ROOT / "playbooks" / "tasks" / "configure-pbs-backup-identity.yml",
            ROOT / "playbooks" / "tasks" / "configure-pbs-copy-reader-identity.yml",
            ROOT / "playbooks" / "tasks" / "configure-pbs-copy-manager-identity.yml",
            ROOT / "playbooks" / "tasks" / "configure-pbs-copy-restore-identity.yml",
        ):
            with self.subTest(task=task_path.name):
                tasks = yaml.safe_load(task_path.read_text())
                mint_index = next(i for i, task in enumerate(tasks) if "generate-token" in task.get(
                    "ansible.builtin.command", {}).get("argv", []))
                parser_index = next(i for i, task in enumerate(tasks) if "parse-generated-token" in task.get(
                    "ansible.builtin.command", {}).get("argv", []))
                persist_index = next(i for i, task in enumerate(tasks) if task.get(
                    "ansible.builtin.include_tasks", {}).get("file") == "bao-merge-keys.yml")
                self.assertLess(mint_index, parser_index)
                self.assertLess(parser_index, persist_index)
                parser = tasks[parser_index]
                self.assertIs(parser.get("no_log"), True)
                self.assertIs(parser.get("check_mode"), False)
                self.assertEqual(parser.get("delegate_to"), "localhost")
                self.assertIs(parser.get("become"), False)
                self.assertIn("_generated_token.stdout", parser["ansible.builtin.command"]["stdin"])
                self.assertIn("expected_token_id", parser["ansible.builtin.command"]["stdin"])
                self.assertTrue(all("_generated_token_parsed.stdout" in str(value)
                                    for value in tasks[parser_index + 1]["ansible.builtin.set_fact"].values()))
                grants = [i for i, task in enumerate(tasks)
                          if task.get("name", "").startswith("Grant only ")]
                self.assertTrue(grants)
                self.assertLess(persist_index, min(grants))


class IdentityStateTests(TestCase):
    def setUp(self):
        self.user = {"userid": pbs.USER_ID, "enable": 1, "expire": 0}
        self.token = {"tokenid": pbs.TOKEN_ID, "enable": 1, "expire": 0}
        self.saved = {
            "user": pbs.USER_ID,
            "token_id": pbs.TOKEN_ID,
            "secret": "token-secret",
            "datastore": "o11y",
            "fingerprint": "a" * 64,
        }

    def test_both_absent_without_vault_copy_plans_one_create(self):
        self.assertEqual(
            pbs.plan_identity(users=[], tokens=[], secret_record=None),
            {"action": "create", "user": pbs.USER_ID, "token_id": pbs.TOKEN_ID},
        )

    def test_both_present_and_authenticated_reuses_stable_token(self):
        self.assertEqual(
            pbs.plan_identity(
                users=[self.user], tokens=[self.token], secret_record=self.saved, authenticated=True
            )["action"],
            "reuse",
        )

    def test_token_without_vault_copy_refuses(self):
        with self.assertRaisesRegex(pbs.Refusal, "without its OpenBao copy"):
            pbs.plan_identity(users=[self.user], tokens=[self.token], secret_record=None)

    def test_vault_copy_without_pbs_identity_refuses(self):
        with self.assertRaisesRegex(pbs.Refusal, "incomplete"):
            pbs.plan_identity(users=[], tokens=[], secret_record=self.saved)

    def test_user_without_token_refuses(self):
        with self.assertRaisesRegex(pbs.Refusal, "incomplete"):
            pbs.plan_identity(users=[self.user], tokens=[], secret_record=self.saved)

    def test_token_without_user_refuses(self):
        with self.assertRaisesRegex(pbs.Refusal, "incomplete"):
            pbs.plan_identity(users=[], tokens=[self.token], secret_record=self.saved)

    def test_invalid_secret_or_expiry_refuses_without_rotation(self):
        for saved, token in (({**self.saved, "secret": ""}, self.token), (self.saved, {**self.token, "expire": 10})):
            with self.subTest(saved=saved, token=token), self.assertRaises(pbs.Refusal):
                pbs.plan_identity(users=[self.user], tokens=[token], secret_record=saved, authenticated=True)

    def test_unauthenticated_existing_secret_refuses(self):
        with self.assertRaisesRegex(pbs.Refusal, "not authenticated"):
            pbs.plan_identity(users=[self.user], tokens=[self.token], secret_record=self.saved)


class CopyApiRequestTests(TestCase):
    def test_remote_creation_is_a_closed_post_with_secret_only_in_form_body(self):
        method, path, body = pbs._copy_api_request_shape({
            "operation": "remote-create",
            "remote_id": "o11y-primary",
            "source_host": "pbs-primary.internal",
            "source_fingerprint": "a" * 64,
            "body": {
                "host": "pbs-primary.internal",
                "auth-id": pbs.COPY_READER_TOKEN_ID,
                "password": "reader-secret",
                "fingerprint": "a" * 64,
                "port": 8007,
            },
        })
        self.assertEqual((method, path), ("POST", "/api2/json/config/remote"))
        self.assertEqual(body["password"], "reader-secret")
        self.assertNotIn("reader-secret", path)

    def test_sync_creation_requires_verified_only_and_no_vanished_deletion(self):
        payload = {
            "operation": "sync-create",
            "sync_job_id": "o11y-copy-sync",
            "remote_id": "o11y-primary",
            "body": {
                "id": "o11y-copy-sync",
                "store": "protected-copy",
                "remote-store": "o11y",
                "remote": "o11y-primary",
                "owner": pbs.COPY_MANAGER_TOKEN_ID,
                "remove-vanished": False,
                "verified-only": True,
                "sync-direction": "pull",
            },
        }
        method, path, body = pbs._copy_api_request_shape(payload)
        self.assertEqual((method, path), ("POST", "/api2/json/config/sync"))
        self.assertEqual(body["remove-vanished"], "0")
        self.assertEqual(body["verified-only"], "1")
        unsafe = {**payload, "body": {**payload["body"], "remove-vanished": True}}
        with self.assertRaisesRegex(pbs.Refusal, "may not remove vanished"):
            pbs._copy_api_request_shape(unsafe)
        unverified = {**payload, "body": {**payload["body"], "verified-only": False}}
        with self.assertRaisesRegex(pbs.Refusal, "must require verified"):
            pbs._copy_api_request_shape(unverified)

    def test_sync_update_binds_existing_remote_without_unbound_state(self):
        method, path, body = pbs._copy_api_request_shape({
            "operation": "sync-update",
            "sync_job_id": "o11y-copy-sync",
            "remote_id": "o11y-primary",
            "body": {
                "remote": "o11y-primary",
                "remote-store": "o11y",
                "owner": pbs.COPY_MANAGER_TOKEN_ID,
                "remove-vanished": False,
                "verified-only": True,
            },
        })
        self.assertEqual((method, path), ("PUT", "/api2/json/config/sync/o11y-copy-sync"))
        self.assertEqual(body["remote"], "o11y-primary")

    def test_arbitrary_paths_methods_and_unknown_config_fields_are_rejected(self):
        for payload in (
            {"operation": "delete-sync", "path": "/api2/json/config/sync/oops"},
            {"operation": "sync-create", "path": "/api2/json/access/users", "body": {}},
            {"operation": "sync-create", "body": {"id": "ok", "prune": True}},
        ):
            with self.subTest(payload=payload), self.assertRaises(pbs.Refusal):
                pbs._copy_api_request_shape(payload)

    def test_snapshot_verification_binds_the_full_snapshot_selector(self):
        method, path, body = pbs._copy_api_request_shape({
            "operation": "snapshot-verify",
            "backup_type": "vm",
            "backup_id": "120",
            "backup_time": 1792594800,
        })
        self.assertEqual((method, path), ("POST", "/api2/json/admin/datastore/o11y/verify"))
        self.assertEqual(body, {
            "store": "o11y",
            "backup-type": "vm",
            "backup-id": "120",
            "backup-time": "1792594800",
            "ignore-verified": "0",
        })
        for payload in (
            {"operation": "snapshot-verify", "backup_type": "ct", "backup_id": "120", "backup_time": 1},
            {"operation": "snapshot-verify", "backup_type": "vm", "backup_id": "120", "backup_time": True},
            {"operation": "snapshot-verify", "backup_type": "vm", "backup_id": "120", "backup_time": 1,
             "store": "other"},
        ):
            with self.subTest(payload=payload), self.assertRaises(pbs.Refusal):
                pbs._copy_api_request_shape(payload)

    def test_sync_run_is_one_fixed_async_job_route(self):
        self.assertEqual(
            pbs._copy_api_request_shape({"operation": "sync-run", "sync_job_id": "o11y-copy-sync"}),
            ("POST", "/api2/json/admin/sync/o11y-copy-sync/run", None),
        )
        with self.assertRaises(pbs.Refusal):
            pbs._copy_api_request_shape({"operation": "sync-run", "sync_job_id": "bad/id"})

    def test_manifest_download_is_pinned_exactly_bounded_and_returns_only_digest(self):
        manifest = {
            "backup-type": "vm",
            "backup-id": "120",
            "backup-time": 1792594800,
            "files": [{
                "filename": "qemu-server.conf.blob",
                "crypt-mode": "none",
                "size": 19,
                "csum": "2" * 64,
            }],
        }
        content = json.dumps(manifest, separators=(",", ":")).encode()
        blob = content

        class Response:
            status = 200

            def read(self, _limit):
                return blob

        class PinnedConnection:
            def __init__(self, *_args, **_kwargs):
                self.paths = []
                self.connected = False

            def connect(self):
                self.connected = True

            def request(self, method, path, **kwargs):
                assert self.connected
                self.paths.append((method, path, kwargs))

            def getresponse(self):
                return Response()

            def close(self):
                return None

        connection = PinnedConnection()
        with patch.object(pbs, "_PinnedHTTPSConnection", return_value=connection):
            result = pbs.read_snapshot_manifest(
                url="https://pbs-primary.internal",
                expected_host="pbs-primary.internal",
                fingerprint="a" * 64,
                auth_id=pbs.COPY_READER_TOKEN_ID,
                secret="protected-reader-secret",
                backup_id="120",
                backup_time=1792594800,
            )
        self.assertTrue(connection.connected)
        self.assertEqual(result["artifact_fingerprint"], pbs.artifact_fingerprint(manifest))
        self.assertEqual(set(result), {"snapshot", "artifact_fingerprint"})
        self.assertEqual(len(connection.paths), 1)
        method, path, kwargs = connection.paths[0]
        self.assertEqual(method, "GET")
        self.assertTrue(path.startswith("/api2/json/admin/datastore/o11y/download-decoded?"))
        self.assertEqual(parse_qs(urlsplit(path).query), {
            "store": ["o11y"],
            "backup-type": ["vm"],
            "backup-id": ["120"],
            "backup-time": ["1792594800"],
            "file-name": ["index.json.blob"],
        })
        self.assertNotIn("protected-reader-secret", path)
        self.assertIn("PBSAPIToken", kwargs["headers"]["Authorization"])

    def test_manifest_download_refuses_wrong_identity_bad_crc_and_wrong_reader(self):
        manifest = {
            "backup-type": "vm",
            "backup-id": "120",
            "backup-time": 1792594800,
            "files": [{
                "filename": "qemu-server.conf.blob",
                "crypt-mode": "none",
                "size": 19,
                "csum": "2" * 64,
            }],
        }
        content = json.dumps(manifest, separators=(",", ":")).encode()
        blob = content

        class Response:
            status = 200

            def read(self, _limit):
                return blob

        class PinnedConnection:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                return None

            def request(self, *_args, **_kwargs):
                return None

            def getresponse(self):
                return Response()

            def close(self):
                return None

        for backup_id, backup_time, auth_id in (
            ("121", 1792594800, pbs.COPY_READER_TOKEN_ID),
            ("120", 1792594801, pbs.COPY_READER_TOKEN_ID),
            ("120", 1792594800, pbs.TOKEN_ID),
        ):
            with (
                self.subTest(backup_id=backup_id, backup_time=backup_time, auth_id=auth_id),
                patch.object(pbs, "_PinnedHTTPSConnection", PinnedConnection),
                self.assertRaises(pbs.Refusal),
            ):
                pbs.read_snapshot_manifest(
                    url="https://pbs-primary.internal",
                    expected_host="pbs-primary.internal",
                    fingerprint="a" * 64,
                    auth_id=auth_id,
                    secret="protected-reader-secret",
                    backup_id=backup_id,
                    backup_time=backup_time,
                )

        with patch.object(pbs, "_PinnedHTTPSConnection", PinnedConnection):
            result = pbs.read_snapshot_manifest(
                url="https://pbs-copy.internal.test",
                expected_host="pbs-copy.internal.test",
                fingerprint="b" * 64,
                auth_id=pbs.COPY_MANAGER_TOKEN_ID,
                secret="protected-manager-secret",
                backup_id="120",
                backup_time=1792594800,
            )
        self.assertEqual(result["artifact_fingerprint"], pbs.artifact_fingerprint(manifest))

    def test_manifest_restore_profile_accepts_only_destination_restore_reader(self):
        manifest = {
            "backup-type": "vm",
            "backup-id": "120",
            "backup-time": 1792594800,
            "files": [{
                "filename": "qemu-server.conf.blob",
                "crypt-mode": "none",
                "size": 19,
                "csum": "2" * 64,
            }],
        }
        content = json.dumps(manifest, separators=(",", ":")).encode()

        class Response:
            status = 200

            def read(self, _limit):
                return content

        class PinnedConnection:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                return None

            def request(self, *_args, **_kwargs):
                return None

            def getresponse(self):
                return Response()

            def close(self):
                return None

        restore_auth_id = pbs.COPY_READER_PROFILES["destination_restore"]["token_id"]
        with patch.object(pbs, "_PinnedHTTPSConnection", PinnedConnection):
            result = pbs.read_snapshot_manifest(
                url="https://pbs-copy.internal.test",
                expected_host="pbs-copy.internal.test",
                fingerprint="b" * 64,
                auth_id=restore_auth_id,
                secret="protected-restore-reader-secret",
                backup_id="120",
                backup_time=1792594800,
                identity_profile="destination_restore",
            )
        self.assertEqual(result["artifact_fingerprint"], pbs.artifact_fingerprint(manifest))

        with (
            patch.object(pbs, "_PinnedHTTPSConnection", PinnedConnection),
            self.assertRaisesRegex(pbs.Refusal, "does not match its fixed profile"),
        ):
            pbs.read_snapshot_manifest(
                url="https://pbs-copy.internal.test",
                expected_host="pbs-copy.internal.test",
                fingerprint="b" * 64,
                auth_id=pbs.COPY_MANAGER_TOKEN_ID,
                secret="protected-manager-secret",
                backup_id="120",
                backup_time=1792594800,
                identity_profile="destination_restore",
            )

    def test_endpoint_rejects_paths_queries_and_credential_bearing_urls(self):
        for url in (
            "https://pbs.internal.test/api2/json",
            "https://pbs.internal.test/?next=https://other.test",
            "https://user@pbs.internal.test",
        ):
            with self.subTest(url=url), self.assertRaises(pbs.Refusal):
                pbs.copy_api_request(
                    url=url,
                    fingerprint="a" * 64,
                    auth_id="manager@pbs!copy-api",
                    secret="secret",
                    expected_host="pbs.internal.test",
                    payload={"operation": "sync-list"},
                )
        with self.assertRaisesRegex(pbs.Refusal, "does not match the declared destination host"):
            pbs.copy_api_request(
                url="https://another-host.internal.test",
                fingerprint="a" * 64,
                auth_id="manager@pbs!copy-api",
                secret="secret",
                expected_host="pbs.internal.test",
                payload={"operation": "sync-list"},
            )

    def test_tls_pin_must_succeed_before_credentials_are_sent(self):
        class RefusingPin:
            def __init__(self, *_args, **_kwargs):
                self.request_called = False
                self.closed = False

            def connect(self):
                raise pbs.Refusal("PBS TLS certificate fingerprint mismatch; no credential was sent.")

            def request(self, *_args, **_kwargs):
                self.request_called = True

            def close(self):
                self.closed = True

        connection = RefusingPin()
        with (
            patch.object(pbs, "_PinnedHTTPSConnection", return_value=connection),
            self.assertRaisesRegex(pbs.Refusal, "no credential was sent"),
        ):
            pbs.copy_api_request(
                url="https://pbs-copy.internal.test",
                fingerprint="a" * 64,
                auth_id="manager@pbs!copy-api",
                secret="private-secret",
                expected_host="pbs-copy.internal.test",
                payload={"operation": "sync-list"},
            )
        self.assertFalse(connection.request_called)
        self.assertTrue(connection.closed)

    def test_redirect_is_refused_without_following_location(self):
        class RedirectResponse:
            status = 302

            def read(self, _limit):
                return b"redirect body"

        class PinnedConnection:
            def __init__(self, *_args, **_kwargs):
                self.paths = []

            def connect(self):
                return None

            def request(self, method, path, **_kwargs):
                self.paths.append((method, path))

            def getresponse(self):
                return RedirectResponse()

            def close(self):
                return None

        connection = PinnedConnection()
        with (
            patch.object(pbs, "_PinnedHTTPSConnection", return_value=connection),
            self.assertRaisesRegex(pbs.Refusal, "refused a redirect"),
        ):
            pbs.copy_api_request(
                url="https://pbs-copy.internal.test",
                fingerprint="a" * 64,
                auth_id="manager@pbs!copy-api",
                secret="private-secret",
                expected_host="pbs-copy.internal.test",
                payload={"operation": "sync-list"},
            )
        self.assertEqual(connection.paths, [("GET", "/api2/json/config/sync")])

    def test_exact_task_status_uses_pinned_manager_and_fixed_encoded_upid_path(self):
        class Response:
            status = 200

            def read(self, _limit):
                return b'{"data":{"status":"stopped","exitstatus":"OK"}}'

        class PinnedConnection:
            def __init__(self, *_args, **_kwargs):
                self.connected = False
                self.requests = []

            def connect(self):
                self.connected = True

            def request(self, method, path, **kwargs):
                assert self.connected
                self.requests.append((method, path, kwargs))

            def getresponse(self):
                return Response()

            def close(self):
                return None

        connection = PinnedConnection()
        with patch.object(pbs, "_PinnedHTTPSConnection", return_value=connection):
            result = pbs.copy_api_request(
                url="https://pbs-copy.internal.test",
                fingerprint="b" * 64,
                auth_id=pbs.COPY_MANAGER_TOKEN_ID,
                secret="private-manager-secret",
                expected_host="pbs-copy.internal.test",
                payload={
                    "operation": "task-status",
                    "node": "pbs-copy",
                    "upid": "UPID:pbs-copy:0000ABCD:00000001:5F000000:sync:job1:user@pbs:",
                },
            )
        self.assertEqual(result, {"status": "stopped", "exitstatus": "OK"})
        self.assertEqual(len(connection.requests), 1)
        method, path, kwargs = connection.requests[0]
        self.assertEqual(method, "GET")
        self.assertIn("/nodes/pbs-copy/tasks/UPID%3Apbs-copy%3A", path)
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("PBSAPIToken "))

    def test_stopped_failed_task_exits_real_poll_contract_without_leaking_status(self):
        private_failure = "private-task-error-must-not-appear"

        class Response:
            status = 200

            def read(self, _limit):
                return json.dumps({"data": {
                    "status": "stopped",
                    "exitstatus": private_failure,
                }}).encode()

        class PinnedConnection:
            def __init__(self, *_args, **_kwargs):
                self.connected = False

            def connect(self):
                self.connected = True

            def request(self, method, path, **_kwargs):
                assert self.connected
                assert method == "GET"
                assert "/tasks/" in path

            def getresponse(self):
                return Response()

            def close(self):
                pass

        connection = PinnedConnection()
        with patch.object(pbs, "_PinnedHTTPSConnection", return_value=connection):
            result = pbs.copy_api_request(
                url="https://pbs-copy.internal.test",
                fingerprint="b" * 64,
                auth_id=pbs.COPY_MANAGER_TOKEN_ID,
                secret="private-manager-secret",
                expected_host="pbs-copy.internal.test",
                payload={
                    "operation": "task-status",
                    "node": "pbs-copy",
                    "upid": "UPID:pbs-copy:0000ABCD:00000001:5F000000:sync:job1:user@pbs:",
                },
            )

        self.assertEqual(result, {"status": "stopped", "exitstatus": "failed"})
        self.assertNotIn(private_failure, json.dumps(result))

        environment = Environment()
        environment.filters["from_json"] = json.loads
        runtime_value = {
            "rc": 0,
            "stdout": json.dumps({"data": result}),
        }
        for playbook, poll_name, status_var, success_name in (
            (
                ROOT / "playbooks" / "verify-o11y-pbs-artifact.yml",
                "Poll the exact PBS verification UPID through pinned TLS",
                "_pbs_verification_status_raw",
                "Require successful completion for the exact verification task",
            ),
            (
                ROOT / "playbooks" / "sync-o11y-protected-pbs-copy.yml",
                "Poll only the exact destination sync UPID",
                "_pbs_sync_status_raw",
                "Require successful completion of the exact sync UPID",
            ),
        ):
            plays = yaml.safe_load(playbook.read_text())
            tasks = [task for play in plays for task in play.get("tasks", [])]
            poll = next(task for task in tasks if task.get("name") == poll_name)
            success = next(task for task in tasks if task.get("name") == success_name)
            variables = {status_var: runtime_value}
            self.assertTrue(
                environment.compile_expression(poll["until"])(**variables),
                poll_name,
            )
            conditions = success["ansible.builtin.assert"]["that"]
            self.assertFalse(
                all(environment.compile_expression(condition)(**variables) for condition in conditions),
                success_name,
            )

    def test_task_status_rejects_unowned_identity_and_unknown_selector(self):
        for auth_id, payload in (
            (pbs.TOKEN_ID, {"operation": "task-status", "node": "pbs", "upid": "UPID:x"}),
            (pbs.COPY_MANAGER_TOKEN_ID, {
                "operation": "task-status", "node": "pbs", "upid": "UPID:x", "extra": "x",
            }),
        ):
            with self.subTest(auth_id=auth_id), self.assertRaises(pbs.Refusal):
                pbs.copy_api_request(
                    url="https://pbs-copy.internal.test",
                    fingerprint="b" * 64,
                    auth_id=auth_id,
                    secret="private-secret",
                    expected_host="pbs-copy.internal.test",
                    payload=payload,
                )


class CopyManagerIdentityTests(TestCase):
    remote_id = "o11y-primary"

    def setUp(self):
        self.user = {"userid": pbs.COPY_MANAGER_USER_ID, "enable": 1, "expire": 0}
        self.token = {"tokenid": pbs.COPY_MANAGER_TOKEN_ID, "enable": 1, "expire": 0}
        self.record = {
            "user": pbs.COPY_MANAGER_USER_ID,
            "token_id": pbs.COPY_MANAGER_TOKEN_ID,
            "secret": "private-manager-secret",
            "datastore": "o11y",
            "fingerprint": "b" * 64,
        }
        self.expected = {
            "/remote": {"Remote.Audit": False, "Remote.Modify": False, "Remote.Read": False},
            f"/remote/{self.remote_id}": {
                "Remote.Audit": False, "Remote.Modify": False, "Remote.Read": False
            },
            f"/remote/{self.remote_id}/o11y": {"Remote.Audit": False, "Remote.Read": False},
            "/datastore/o11y": {
                "Datastore.Audit": False,
                "Datastore.Backup": False,
                "Datastore.Read": False,
                "Datastore.Verify": False,
            },
        }
        self.acls = [
            {"ugid": auth_id, "path": path, "roleid": role, "propagate": 0}
            for auth_id in (pbs.COPY_MANAGER_USER_ID, pbs.COPY_MANAGER_TOKEN_ID)
            for path, role in (
                ("/remote", "RemoteAdmin"),
                (f"/remote/{self.remote_id}", "RemoteAdmin"),
                (f"/remote/{self.remote_id}/o11y", "RemoteSyncOperator"),
                ("/datastore/o11y", "DatastoreAudit"),
                ("/datastore/o11y", "DatastoreBackup"),
                ("/datastore/o11y", "DatastoreReader"),
            )
        ]

    def test_identity_requires_complete_stable_bao_owned_pair(self):
        self.assertEqual(
            pbs.plan_copy_manager_identity(users=[], tokens=[], secret_record=None)["action"],
            "create",
        )
        self.assertEqual(
            pbs.plan_copy_manager_identity(
                users=[self.user], tokens=[self.token], secret_record=self.record, authenticated=True
            )["action"],
            "reuse",
        )
        with self.assertRaisesRegex(pbs.Refusal, "without its OpenBao copy"):
            pbs.plan_copy_manager_identity(users=[self.user], tokens=[self.token], secret_record=None)
        with self.assertRaisesRegex(pbs.Refusal, "not authenticated"):
            pbs.plan_copy_manager_identity(
                users=[self.user], tokens=[self.token], secret_record=self.record, authenticated=False
            )

    def test_manager_permissions_are_exact_and_delete_free(self):
        self.assertTrue(pbs.validate_copy_manager_permissions(
            user_permissions=self.expected,
            token_permissions=self.expected,
            acl_rows=self.acls,
            remote_id=self.remote_id,
        )["valid"])
        with self.assertRaisesRegex(pbs.Refusal, "broad, propagated"):
            unsafe = {**self.expected, "/remote": {
                "Remote.Audit": True, "Remote.Modify": True, "Remote.Read": True
            }}
            pbs.plan_copy_manager_acl(
                user_permissions=unsafe,
                token_permissions=self.expected,
                acl_rows=[],
                remote_id=self.remote_id,
            )
        with self.assertRaisesRegex(pbs.Refusal, "missing, extra"):
            pbs.validate_copy_manager_permissions(
                user_permissions={**self.expected, "/datastore/other": {"Datastore.Backup": False}},
                token_permissions=self.expected,
                acl_rows=self.acls,
                remote_id=self.remote_id,
            )

    def test_acl_plan_adds_only_missing_exact_grants(self):
        result = pbs.plan_copy_manager_acl(
            user_permissions={}, token_permissions={}, acl_rows=[], remote_id=self.remote_id
        )
        self.assertEqual(len(result["missing"]), 12)
        self.assertEqual(
            pbs.plan_copy_manager_acl(
                user_permissions=self.expected,
                token_permissions=self.expected,
                acl_rows=self.acls,
                remote_id=self.remote_id,
            )["missing"],
            [],
        )

    def test_existing_manager_gets_only_the_scoped_manifest_read_grant(self):
        old_permissions = {
            **self.expected,
            "/datastore/o11y": {"Datastore.Audit": False, "Datastore.Backup": False},
        }
        old_acls = [row for row in self.acls if row["roleid"] != "DatastoreReader"]
        result = pbs.plan_copy_manager_acl(
            user_permissions=old_permissions,
            token_permissions=old_permissions,
            acl_rows=old_acls,
            remote_id=self.remote_id,
        )
        self.assertEqual(
            result["missing"],
            [
                {"auth_id": pbs.COPY_MANAGER_USER_ID, "path": pbs.DATASTORE_PATH, "role": "DatastoreReader"},
                {"auth_id": pbs.COPY_MANAGER_TOKEN_ID, "path": pbs.DATASTORE_PATH, "role": "DatastoreReader"},
            ],
        )

    def test_manager_read_grant_rejects_parent_scope_or_delete_privileges(self):
        for unsafe in (
            {**self.expected, "/datastore": {"Datastore.Read": False}},
            {
                **self.expected,
                "/datastore/o11y": {
                    "Datastore.Audit": False,
                    "Datastore.Backup": False,
                    "Datastore.Read": False,
                    "Datastore.Verify": False,
                    "Datastore.Prune": False,
                },
            },
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(pbs.Refusal):
                pbs.plan_copy_manager_acl(
                    user_permissions=unsafe,
                    token_permissions=self.expected,
                    acl_rows=self.acls,
                    remote_id=self.remote_id,
                )


class PermissionTests(TestCase):
    def setUp(self):
        self.acls = [
            {"ugid": pbs.USER_ID, "path": pbs.DATASTORE_PATH, "roleid": "DatastoreBackup", "propagate": 0},
            {"ugid": pbs.TOKEN_ID, "path": pbs.DATASTORE_PATH, "roleid": "DatastoreBackup", "propagate": 0},
        ]

    def check(self, user=None, token=None, acls=None):
        return pbs.validate_permissions(
            user_permissions=user if user is not None else effective(),
            token_permissions=token if token is not None else effective(),
            acl_rows=acls if acls is not None else self.acls,
        )

    def test_false_means_nonpropagating_grant_not_absence(self):
        self.assertTrue(self.check()["valid"])

    def test_acl_plan_adds_only_missing_expected_identity_entries(self):
        plan = pbs.plan_acl(
            user_permissions=[], token_permissions=[],
            acl_rows=[self.acls[0]],
        )
        self.assertEqual(plan["missing_auth_ids"], [pbs.TOKEN_ID])

    def test_prune_false_is_still_a_grant_and_is_refused(self):
        with self.assertRaisesRegex(pbs.Refusal, "unknown, destructive"):
            self.check(user=effective({"Datastore.Backup": False, "Datastore.Prune": False}))

    def test_other_store_and_ancestor_effective_paths_refuse(self):
        for path in ("/datastore", "/datastore/other"):
            with self.subTest(path=path), self.assertRaisesRegex(pbs.Refusal, "outside the exact"):
                self.check(token=effective(path=path))

    def test_propagating_or_unknown_permission_schema_refuses(self):
        with self.assertRaisesRegex(pbs.Refusal, "propagates"):
            self.check(user=effective({"Datastore.Backup": True}))
        with self.assertRaises(pbs.Refusal):
            self.check(user={"o11y": {"Datastore.Backup": False}})

    def test_duplicate_paths_and_duplicate_identity_acl_refuse(self):
        with self.assertRaisesRegex(pbs.Refusal, "duplicates a path"):
            self.check(user=effective() + effective())
        with self.assertRaisesRegex(pbs.Refusal, "exactly one user and one token"):
            self.check(acls=self.acls + [self.acls[0]])

    def test_direct_ancestor_other_store_or_wrong_role_acl_refuses(self):
        for change in (
            {"path": "/datastore"},
            {"path": "/datastore/other"},
            {"roleid": "DatastoreAdmin"},
            {"propagate": 1},
        ):
            bad = [dict(row, **change) for row in self.acls]
            with self.subTest(change=change), self.assertRaises(pbs.Refusal):
                self.check(acls=bad)


class CopyReaderIdentityTests(TestCase):
    def setUp(self):
        self.user = {"userid": pbs.COPY_READER_USER_ID, "enable": 1, "expire": 0}
        self.token = {"tokenid": pbs.COPY_READER_TOKEN_ID, "enable": 1, "expire": 0}
        self.saved = {
            "user": pbs.COPY_READER_USER_ID,
            "token_id": pbs.COPY_READER_TOKEN_ID,
            "secret": "reader-secret",
            "datastore": "o11y",
            "fingerprint": "a" * 64,
        }
        self.reader_permissions = effective(
            {"Datastore.Audit": False, "Datastore.Read": False, "Datastore.Verify": False}
        )
        self.acls = [
            {"ugid": pbs.COPY_READER_USER_ID, "path": pbs.DATASTORE_PATH,
             "roleid": pbs.COPY_READER_ROLE, "propagate": 0},
            {"ugid": pbs.COPY_READER_TOKEN_ID, "path": pbs.DATASTORE_PATH,
             "roleid": pbs.COPY_READER_ROLE, "propagate": 0},
        ]

    def test_reader_identity_is_separate_and_reuses_only_authenticated_complete_state(self):
        self.assertEqual(
            pbs.plan_copy_reader_identity(users=[], tokens=[], secret_record=None)["action"], "create"
        )
        self.assertEqual(
            pbs.plan_copy_reader_identity(
                users=[self.user], tokens=[self.token], secret_record=self.saved, authenticated=True
            )["action"],
            "reuse",
        )
        with self.assertRaisesRegex(pbs.Refusal, "without its OpenBao copy"):
            pbs.plan_copy_reader_identity(users=[self.user], tokens=[self.token], secret_record=None)
        with self.assertRaisesRegex(pbs.Refusal, "not authenticated"):
            pbs.plan_copy_reader_identity(users=[self.user], tokens=[self.token], secret_record=self.saved)

    def test_reader_acl_grants_only_nonpropagating_datastore_reader(self):
        plan = pbs.plan_copy_reader_acl(
            user_permissions=[], token_permissions=[], acl_rows=self.acls[:1]
        )
        self.assertEqual(plan["missing_auth_ids"], [pbs.COPY_READER_TOKEN_ID])
        self.assertTrue(
            pbs.validate_copy_reader_permissions(
                user_permissions=self.reader_permissions,
                token_permissions=self.reader_permissions,
                acl_rows=self.acls,
            )["valid"]
        )
        for bad in (
            effective({"Datastore.Audit": False, "Datastore.Read": False,
                       "Datastore.Verify": False, "Datastore.Prune": False}),
            effective({"Datastore.Audit": True, "Datastore.Read": False,
                       "Datastore.Verify": False}),
        ):
            with self.subTest(bad=bad), self.assertRaises(pbs.Refusal):
                pbs.validate_copy_reader_permissions(
                    user_permissions=bad, token_permissions=self.reader_permissions, acl_rows=self.acls
                )

    def test_destination_restore_profile_is_fixed_and_cannot_select_identity_or_role(self):
        profile = pbs.COPY_READER_PROFILES["destination_restore"]
        user = {"userid": profile["user"], "enable": 1, "expire": 0}
        token = {"tokenid": profile["token_id"], "enable": 1, "expire": 0}
        saved = {"user": profile["user"], "token_id": profile["token_id"], "secret": "restore-secret",
                 "datastore": "o11y", "fingerprint": "b" * 64}
        self.assertEqual(
            pbs.plan_copy_reader_identity(users=[], tokens=[], secret_record=None,
                                          profile="destination_restore"),
            {"action": "create", "user": profile["user"], "token_id": profile["token_id"]},
        )
        self.assertEqual(
            pbs.plan_copy_reader_identity(users=[user], tokens=[token], secret_record=saved,
                                          authenticated=True, profile="destination_restore")["action"],
            "reuse",
        )
        restore_acls = [
            {"ugid": identity, "path": pbs.DATASTORE_PATH, "roleid": pbs.COPY_READER_ROLE, "propagate": 0}
            for identity in (profile["user"], profile["token_id"])
        ]
        self.assertTrue(pbs.validate_copy_reader_permissions(
            user_permissions=self.reader_permissions, token_permissions=self.reader_permissions,
            acl_rows=restore_acls, profile="destination_restore")["valid"])
        with self.assertRaisesRegex(pbs.Refusal, "profile is not an approved"):
            pbs.plan_copy_reader_identity(users=[], tokens=[], secret_record=None, profile="caller-selected")
        for privileges in (
            {"Datastore.Audit": False, "Datastore.Read": False, "Datastore.Verify": False,
             "Datastore.Backup": False},
            {"Datastore.Audit": True, "Datastore.Read": False, "Datastore.Verify": False},
        ):
            with self.subTest(privileges=privileges), self.assertRaises(pbs.Refusal):
                pbs.validate_copy_reader_permissions(
                    user_permissions=effective(privileges), token_permissions=self.reader_permissions,
                    acl_rows=restore_acls, profile="destination_restore")

    def test_managed_reader_provisioning_is_separate_and_persists_before_acl(self):
        task_path = ROOT / "playbooks" / "tasks" / "configure-pbs-copy-reader-identity.yml"
        tasks = yaml.safe_load(task_path.read_text())
        text = task_path.read_text()
        self.assertIn("DatastoreReader", text)
        grant = yaml.safe_load((ROOT / "playbooks" / "tasks" / "grant-pbs-copy-reader-acl.yml").read_text())
        argv = next(task["ansible.builtin.command"]["argv"] for task in grant if "ansible.builtin.command" in task)
        self.assertIn("DatastoreReader", argv)
        self.assertNotIn("DatastoreBackup", argv)
        self.assertNotIn("DatastoreAdmin", argv)
        self.assertIn("o11y_copy_reader_token_secret", text)
        self.assertNotIn("o11y_backup_token_secret", text)
        persist = text.index("file: bao-merge-keys.yml")
        readback = text.index("Read back the authoritative PBS copy-reader credential")
        grant = text.index("Grant only the non-propagating DatastoreReader role")
        self.assertLess(persist, readback)
        self.assertLess(readback, grant)
        for task in tasks:
            if "ansible.builtin.uri" in task and "X-Vault-Token" in task["ansible.builtin.uri"].get("headers", {}):
                self.assertEqual(task.get("no_log"), True)

    def test_destination_restore_identity_is_cas_persisted_before_exact_acl(self):
        task_path = ROOT / "playbooks" / "tasks" / "configure-pbs-copy-restore-identity.yml"
        text = task_path.read_text()
        tasks = yaml.safe_load(text)
        persist = text.index("file: bao-merge-keys.yml")
        readback = text.index("Read back the authoritative PBS copy-restore-reader credential")
        grant = text.index("Grant only the non-propagating DatastoreReader role")
        self.assertLess(persist, readback)
        self.assertLess(readback, grant)
        self.assertIn("o11y-copy-restore@pbs!pve-restore", text)
        self.assertIn("o11y_copy_restore_token_secret", text)
        self.assertIn("'profile': 'destination_restore'", text)
        self.assertNotIn("o11y_copy_manager_token_secret", text)
        self.assertNotIn("o11y_copy_reader_token_secret", text)
        grant_path = ROOT / "playbooks" / "tasks" / "grant-pbs-copy-restore-acl.yml"
        grant_tasks = yaml.safe_load(grant_path.read_text())
        command = next(task["ansible.builtin.command"]["argv"] for task in grant_tasks
                       if "ansible.builtin.command" in task)
        self.assertIn("DatastoreReader", command)
        self.assertIn("/datastore/o11y", command)
        for task in tasks:
            if "ansible.builtin.uri" in task and "X-Vault-Token" in task["ansible.builtin.uri"].get("headers", {}):
                self.assertIs(task.get("no_log"), True)

    def test_copy_readiness_derives_locator_and_pve_uses_only_destination_reader(self):
        path = ROOT / "playbooks" / "verify-o11y-protected-pbs-copy.yml"
        text = path.read_text()
        plays = yaml.safe_load(text)
        guard = next(
            play for play in plays
            if play.get("name") == "Refuse missing chain selectors or caller-owned service identities"
        )
        names = guard["tasks"][0]["vars"]["_pbs_private_names"]
        self.assertIn("o11y_protected_copy_artifact_selector", names)
        self.assertIn("o11y_protected_copy_artifact_selector is not defined", text)
        self.assertIn("'snapshot_time': _pbs_snapshot.backup_time | int", text)
        self.assertNotIn("'selector': hostvars", text)
        self.assertIn("normalized_restore_locator:", text)
        self.assertIn("_pbs_copy_secret_record.o11y_copy_restore_token_id", text)
        self.assertIn("_pbs_copy_secret_record.o11y_copy_restore_token_secret", text)
        self.assertNotIn("data.username == _pbs_copy_secret_record.o11y_copy_manager_token_id", text)
        identity = next(
            task for play in plays for task in play.get("tasks", [])
            if task.get("name") == "Build non-secret destination identity and storage configuration fingerprints"
        )
        serialized = str(identity)
        self.assertIn("Datastore.Audit", serialized)
        self.assertIn("Datastore.Read", serialized)
        self.assertIn("Datastore.Verify", serialized)
        self.assertIn("'propagate': false", serialized)

    def test_restore_storage_registration_is_dev_bound_and_manager_free(self):
        path = ROOT / "playbooks" / "register-o11y-pbs-restore-storage.yml"
        text = path.read_text()
        plays = yaml.safe_load(text)
        self.assertIn("reconcile_pbs_pve_storage.py", text)
        self.assertIn("o11y_copy_restore_token_secret", text)
        self.assertIn("PVEAPIToken={{ _pve_token_id }}={{ _pve_token_secret }}", text)
        self.assertIn("validate_certs: true", text)
        self.assertIn("prewrite", text)
        self.assertIn("/api2/json/cluster/backup", text)
        self.assertIn("pbs_storage_nodes", text)
        self.assertNotIn("o11y_copy_manager_token_secret", text)
        self.assertNotIn("o11y_copy_manager_token_id", text)
        self.assertTrue(any(play.get("name") == "Prove exact private inventory provenance before PVE access"
                            for play in plays))
        catalog = yaml.safe_load((ROOT / "semaphore" / "templates.yml").read_text())
        templates = catalog["templates"] if isinstance(catalog, dict) else catalog
        names = {row.get("name") for row in templates if isinstance(row, dict)}
        self.assertIn("Configure o11y PBS Copy Access (Dev)", names)
        self.assertIn("Register o11y PBS Restore Storage (Dev)", names)

    def test_source_reader_provisioning_is_independent_from_pve_backup_access(self):
        copy_access_path = ROOT / "playbooks" / "configure-o11y-pbs-copy-access.yml"
        copy_access = yaml.safe_load(copy_access_path.read_text())
        primary_text = (ROOT / "playbooks" / "configure-pbs-backup-access.yml").read_text()
        copy_text = copy_access_path.read_text()
        self.assertNotIn("copy-reader", primary_text)
        self.assertIn("require-reviewed-checkout.yml", copy_text)
        self.assertIn("inspect_pbs_backup_access.py", copy_text)
        self.assertIn("configure-pbs-copy-reader-identity.yml", copy_text)
        self.assertNotIn("/api2/json/storage", copy_text)
        self.assertNotIn("DatastoreBackup", copy_text)
        override_guard = next(
            play for play in copy_access
            if play.get("name") == "Refuse caller overrides for private PBS and OpenBao selectors"
        )
        private_names = override_guard["tasks"][0]["vars"]["_pbs_reader_private_names"]
        self.assertIn("pbs_api_host", private_names)
        self.assertIn("bao_role_id", private_names)
        self.assertIn("bao_secret_id", private_names)
        provenance_index = next(
            index for index, play in enumerate(copy_access)
            if play.get("name") == "Prove exact reviewed private and runtime inventory provenance"
        )
        pbs_read_index = next(
            index for index, play in enumerate(copy_access)
            if play.get("name") == "Read primary PBS identity and certificate state"
        )
        self.assertLess(provenance_index, pbs_read_index)
        review_gate = next(
            play for play in copy_access
            if play.get("name") == "Require the exact reviewed source checkout"
        )
        self.assertIn("platform/playbooks/configure-o11y-pbs-copy-access.yml",
                      review_gate["vars"]["_reviewed_checkout_payload_paths"])
        proposal_path = ROOT.parent / "plan" / "development" / "o11y-pbs-semaphore-catalog-proposals.yml"
        proposals = yaml.safe_load(proposal_path.read_text())
        self.assertIn(
            "Configure o11y PBS Copy Access (Dev)",
            [proposal["name"] for proposal in proposals["catalog_proposals"]],
        )

    def test_copy_manager_is_distinct_and_scoped_to_remote_and_copy_datastore(self):
        identity_path = ROOT / "playbooks" / "tasks" / "configure-pbs-copy-manager-identity.yml"
        grant_path = ROOT / "playbooks" / "tasks" / "grant-pbs-copy-manager-acl.yml"
        task_text = identity_path.read_text()
        grant_text = grant_path.read_text()
        self.assertIn("o11y-copy-manager@pbs!copy-api", task_text)
        self.assertIn("o11y_copy_manager_token_secret", task_text)
        self.assertIn("file: bao-merge-keys.yml", task_text)
        self.assertLess(task_text.index("file: bao-merge-keys.yml"), task_text.index("Grant only the exact remote"))
        self.assertIn("--propagate", grant_text)
        self.assertIn("_pbs_manager_acl_grant.path", grant_text)
        grant = yaml.safe_load(grant_text)[0]["ansible.builtin.command"]["argv"]
        self.assertEqual(
            grant[0:4],
            [
                "proxmox-backup-manager",
                "acl",
                "update",
                "{{ _pbs_manager_acl_grant.path }}",
            ],
        )
        self.assertEqual(grant[4:7], [
            "{{ _pbs_manager_acl_grant.role }}",
            "--auth-id",
            "{{ _pbs_manager_acl_grant.auth_id }}",
        ])
        self.assertEqual(grant[7:], ["--propagate", "0"])
        self.assertNotIn("Datastore.Prune", task_text)
        self.assertIn("DatastoreReader", task_text)
        self.assertIn("/datastore/o11y", task_text)
        self.assertNotIn("proxmox-backup-manager remote", task_text)
        copy_text = (ROOT / "playbooks" / "configure-o11y-pbs-copy-access.yml").read_text()
        self.assertIn("hosts: pbs_copy_svc", copy_text)
        self.assertIn("configure-pbs-copy-manager-identity.yml", copy_text)
        primary_text = (ROOT / "playbooks" / "configure-pbs-backup-access.yml").read_text()
        self.assertNotIn("copy-manager", primary_text)


class PbsProbeTests(TestCase):
    def test_certificate_mismatch_stops_before_request_header(self):
        connection = Mock()
        connection.connect.side_effect = pbs.Refusal(
            "PBS TLS certificate fingerprint mismatch; no credential was sent."
        )
        with (
            patch.object(pbs, "_PinnedHTTPSConnection", return_value=connection),
            self.assertRaisesRegex(pbs.Refusal, "no credential was sent"),
        ):
            pbs.authenticate_token(
                url="https://pbs.internal.test:8007",
                fingerprint="aa" * 32,
                auth_id=pbs.TOKEN_ID,
                secret="must-not-be-sent",
                )
        connection.request.assert_not_called()

    def test_bad_fingerprint_or_non_https_endpoint_refuses(self):
        for url, fingerprint in (("http://pbs.internal.test", "aa" * 32), ("https://pbs.internal.test", "bad")):
            with self.subTest(url=url), self.assertRaises(pbs.Refusal):
                pbs.authenticate_token(url=url, fingerprint=fingerprint, auth_id=pbs.TOKEN_ID, secret="x")

    def test_supported_package_version_is_exact(self):
        self.assertEqual(pbs.parse_pbs_version("4.2.8-1"), (4, 2, 8, 1))
        with self.assertRaisesRegex(pbs.Refusal, "outside the reviewed"):
            pbs.parse_pbs_version("4.2.8-2")

    def test_authenticated_guest_certificate_fingerprint_parser(self):
        self.assertEqual(pbs.parse_openssl_fingerprint("sha256 Fingerprint=" + ":".join(["AA"] * 32)), "aa" * 32)
        with self.assertRaises(pbs.Refusal):
            pbs.parse_openssl_fingerprint("certificate information missing")


class PveStorageTests(TestCase):
    def test_absent_storage_plans_create_and_exact_existing_storage_is_noop(self):
        jobs = [{"id": "nightly", "schedule": "daily", "storage": "old-backup"}]
        absent = pve.plan_storage(
            storage_id="pbs-o11y", rows=[], current=None, expected=expected_storage(), jobs_before=jobs
        )
        self.assertEqual(absent["action"], "create")
        existing = pve.plan_storage(
            storage_id="pbs-o11y",
            rows=[{"storage": "pbs-o11y", "type": "pbs"}],
            current=existing_storage(),
            expected=expected_storage(),
            jobs_before=jobs,
            jobs_after=jobs,
            api_base="https://pve.internal.test",
            access_proofs=[access_proof()],
        )
        self.assertEqual(existing["action"], "noop")

    def test_pbs_derived_shared_and_default_enabled_flags_match_api_shapes(self):
        current = existing_storage()
        current.pop("shared")
        current.pop("disable", None)
        self.assertEqual(
            pve.plan_storage(
                storage_id="pbs-o11y", rows=[{"storage": "pbs-o11y", "type": "pbs"}],
                current=current, expected=expected_storage(), jobs_before=[],
                api_base="https://pve.internal.test", access_proofs=[access_proof()],
            )["action"],
            "noop",
        )
        for drift in ({"disable": 1}, {"shared": 0}):
            with self.subTest(drift=drift), self.assertRaisesRegex(
                pve.Refusal, "differs from the declared"
            ):
                pve.plan_storage(
                    storage_id="pbs-o11y", rows=[{"storage": "pbs-o11y", "type": "pbs"}],
                    current={**current, **drift}, expected=expected_storage(), jobs_before=[],
                    api_base="https://pve.internal.test", access_proofs=[access_proof()],
                )

    def test_mismatch_duplicate_and_create_race_refuse(self):
        jobs = []
        cases = (
            {
                "storage_id": "pbs-o11y",
                "rows": [],
                "current": existing_storage(),
                "expected": expected_storage(),
                "jobs_before": jobs,
            },
            {
                "storage_id": "pbs-o11y",
                "rows": [{"storage": "pbs-o11y"}] * 2,
                "current": None,
                "expected": expected_storage(),
                "jobs_before": jobs,
            },
            {
                "storage_id": "pbs-o11y",
                "rows": [{"storage": "pbs-o11y"}],
                "current": dict(existing_storage(), server="other.test"),
                "expected": expected_storage(),
                "jobs_before": jobs,
            },
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(pve.Refusal):
                pve.plan_storage(**case)

    def test_changed_existing_jobs_or_storage_during_registration_refuse(self):
        before = [{"id": "nightly", "schedule": "daily"}]
        after = [{"id": "nightly", "schedule": "weekly"}]
        with self.assertRaisesRegex(pve.Refusal, "backup-job configuration changed"):
            pve.plan_storage(
                storage_id="pbs-o11y",
                rows=[],
                current=None,
                expected=expected_storage(),
                jobs_before=before,
                jobs_after=after,
            )

    def test_unknown_storage_fields_refuse(self):
        with self.assertRaisesRegex(pve.Refusal, "undeclared settings"):
            pve.plan_storage(
                storage_id="pbs-o11y",
                rows=[{"storage": "pbs-o11y"}],
                current=dict(existing_storage(), namespace="new-feature"),
                expected=expected_storage(),
                jobs_before=[],
            )

    def test_declared_missing_node_refuses(self):
        with self.assertRaisesRegex(pve.Refusal, "not present"):
            pve.plan_storage(
                storage_id="pbs-o11y", rows=[], current=None, expected=expected_storage(),
                jobs_before=[], nodes_before=[{"node": "node-b"}],
            )

    def test_redacted_password_requires_live_exact_node_storage_response(self):
        rows = [{"storage": "pbs-o11y", "type": "pbs"}]
        base = dict(storage_id="pbs-o11y", rows=rows, current=existing_storage(),
                    expected=expected_storage(), jobs_before=[], api_base="https://pve.internal.test")
        with self.assertRaisesRegex(pve.Refusal, "has not been verified"):
            pve.plan_storage(**base)
        self.assertEqual(
            pve.plan_storage(**base, access_proofs=[access_proof()])["action"], "noop"
        )

    def test_access_proof_rejects_wrong_path_node_duplicates_and_bad_schema(self):
        base = dict(storage_id="pbs-o11y", rows=[{"storage": "pbs-o11y"}],
                    current=existing_storage(), expected=expected_storage(), jobs_before=[],
                    api_base="https://pve.internal.test")
        for proof in (
            dict(access_proof(), url="https://other.test/api2/json/nodes/node-a/storage/pbs-o11y/content?content=backup"),
            dict(access_proof(), status=302),
            dict(access_proof(), data={"error": "not a list"}),
            access_proof(node="node-b"),
            access_proof(data=["malformed"]),
        ):
            with self.subTest(proof=proof), self.assertRaises(pve.Refusal):
                pve.plan_storage(**base, access_proofs=[proof])
        with self.assertRaisesRegex(pve.Refusal, "duplicate node"):
            pve.plan_storage(**base, access_proofs=[access_proof(), access_proof()])

    def test_preflight_validates_nonsecret_config_before_any_access_probe(self):
        target = existing_storage()
        self.assertEqual(
            pve.preflight_storage(storage_id="pbs-o11y", rows=[{"storage": "pbs-o11y"}],
                                  current=target, expected=expected_storage())["action"],
            "verify_access",
        )
        with self.assertRaisesRegex(pve.Refusal, "differs from the declared"):
            pve.preflight_storage(storage_id="pbs-o11y", rows=[{"storage": "pbs-o11y"}],
                                  current=dict(target, server="other.internal"), expected=expected_storage())

    def test_final_readback_uses_final_target_rows_and_original_unrelated_rows(self):
        before = [{"storage": "old", "type": "dir", "path": "/backup", "digest": "before"}]
        final = [{"storage": "old", "type": "dir", "path": "/backup", "digest": "after"}]
        final.append({"storage": "pbs-o11y", "type": "pbs", "digest": "after"})
        result = pve.plan_storage(
            storage_id="pbs-o11y", rows=final, original_rows=before, rows_after=final,
            current=existing_storage(), expected=expected_storage(), jobs_before=[], jobs_after=[],
            nodes_before=[{"node": "node-a"}], nodes_after=[{"node": "node-a"}],
            api_base="https://pve.internal.test", access_proofs=[access_proof()],
        )
        self.assertEqual(result["action"], "noop")
        changed = [{"storage": "old", "type": "dir", "path": "/changed", "digest": "after"}] + final[1:]
        with self.assertRaisesRegex(pve.Refusal, "existing PVE storage configuration changed"):
            pve.plan_storage(
                storage_id="pbs-o11y", rows=changed, original_rows=before, rows_after=changed,
                current=existing_storage(), expected=expected_storage(), jobs_before=[], jobs_after=[],
                nodes_before=[{"node": "node-a"}], nodes_after=[{"node": "node-a"}],
                api_base="https://pve.internal.test", access_proofs=[access_proof()],
            )

    def test_concurrent_storage_create_is_not_accepted_without_prior_pve_access_proof(self):
        old_rows = [{"storage": "existing", "type": "dir", "path": "/mnt/backups"}]
        rows_now = old_rows + [{"storage": "pbs-o11y", "type": "pbs"}]
        with self.assertRaisesRegex(pve.Refusal, "has not been verified"):
            pve.validate_prewrite_snapshot(
                storage_id="pbs-o11y", rows_before=old_rows, rows_now=rows_now,
                current=existing_storage(), expected=expected_storage(),
                jobs_before=[], jobs_now=[], nodes_before=[{"node": "node-a"}],
                nodes_now=[{"node": "node-a"}],
            )

    def test_prewrite_existing_target_reuses_authenticated_access_evidence(self):
        old_rows = [{"storage": "existing", "type": "dir", "path": "/mnt/backups"}]
        rows_now = old_rows + [{"storage": "pbs-o11y", "type": "pbs"}]
        result = pve.validate_prewrite_snapshot(
            storage_id="pbs-o11y", rows_before=rows_now, rows_now=rows_now,
            current=existing_storage(), expected=expected_storage(),
            jobs_before=[], jobs_now=[], nodes_before=[{"node": "node-a"}],
            nodes_now=[{"node": "node-a"}], api_base="https://pve.internal.test",
            access_proofs=[access_proof()],
        )
        self.assertEqual(result["action"], "noop")

    def test_prewrite_requires_existing_storage_and_jobs_to_remain_exact(self):
        old_rows = [{"storage": "existing", "type": "dir", "path": "/mnt/backups"}]
        with self.assertRaisesRegex(pve.Refusal, "existing PVE storage"):
            pve.validate_prewrite_snapshot(
                storage_id="pbs-o11y", rows_before=old_rows,
                rows_now=[{"storage": "existing", "type": "dir", "path": "/changed"}],
                current=None, expected=expected_storage(), jobs_before=[], jobs_now=[],
                nodes_before=[], nodes_now=[],
            )
        with self.assertRaisesRegex(pve.Refusal, "backup-job configuration changed"):
            pve.validate_prewrite_snapshot(
                storage_id="pbs-o11y", rows_before=old_rows, rows_now=old_rows,
                current=None, expected=expected_storage(), jobs_before=[],
                jobs_now=[{"id": "nightly"}], nodes_before=[], nodes_now=[],
            )


class PlaybookBoundaryTests(TestCase):
    def test_semaphore_template_is_dev_bound_and_only_accepts_reviewed_sha(self):
        templates = yaml.safe_load((ROOT / "semaphore" / "templates.yml").read_text())["templates"]
        template = next(item for item in templates if item["name"] == "Configure PBS Backup Access (Dev)")
        self.assertEqual(template["playbook"], "platform/playbooks/configure-pbs-backup-access.yml")
        self.assertEqual(template["repository"], "agent-cloud dev")
        self.assertEqual(
            template["survey_vars"],
            [
                {
                    "name": "expected_repository_sha",
                    "title": "Reviewed commit SHA",
                    "description": "Exact reviewed Dev revision for PBS backup access configuration",
                    "type": "string",
                    "required": True,
                }
            ],
        )

    def test_check_mode_skips_identity_mutation_and_pve_write(self):
        playbook = (ROOT / "playbooks" / "configure-pbs-backup-access.yml").read_text()
        identity_task = (ROOT / "playbooks" / "tasks" / "configure-pbs-backup-identity.yml").read_text()
        self.assertIn("include_tasks: tasks/configure-pbs-backup-identity.yml", playbook)
        self.assertIn("when: not ansible_check_mode", playbook)
        self.assertIn("method: POST", playbook)
        self.assertIn("- not ansible_check_mode", playbook)
        self.assertIn("not executed in check mode", playbook)
        self.assertIn("file: bao-merge-keys.yml", identity_task)

    def test_verify_tag_selects_only_read_paths_and_refuses_missing_identity_acl_or_storage(self):
        plays = yaml.safe_load((ROOT / "playbooks" / "configure-pbs-backup-access.yml").read_text())
        tagged = [play for play in plays if "verify" in play.get("tags", [])]
        self.assertEqual(
            [play["name"] for play in tagged],
            [
                "Require one declared PBS guest",
                "Require exactly one PBS guest",
                "Read current PBS version, identity, ACL and guest certificate state",
                "Validate identity state and read all current PVE configuration",
            ],
        )
        transport = next(
            task for task in tagged[-1]["tasks"]
            if task.get("ansible.builtin.include_tasks", {}).get("file") == "tasks/assert-bao-transport.yml"
        )
        self.assertEqual(transport["ansible.builtin.include_tasks"]["apply"]["tags"], ["verify"])
        selected_text = str(tagged)
        for write in ("configure-pbs-backup-identity.yml", "method: POST", "method: PUT", "bao-merge-keys.yml"):
            self.assertNotIn(write, selected_text)
        validation_tasks = tagged[-1]["tasks"]
        for refusal in (
            "Require existing OpenBao-backed PBS identity for verify-only runs",
            "Require existing datastore-scoped ACLs for verify-only runs",
            "Require existing PVE registration and readable backup content for verify-only runs",
        ):
            self.assertIn(refusal, [task.get("name") for task in validation_tasks])

    def test_verify_refusal_chain_executes_real_guards_without_mutation_tasks(self):
        ansible_playbook = shutil.which("ansible-playbook")
        if ansible_playbook is None:
            self.skipTest("needs ansible-playbook")
        playbook = yaml.safe_load((ROOT / "playbooks" / "configure-pbs-backup-access.yml").read_text())
        validation = next(
            play for play in playbook
            if play.get("name") == "Validate identity state and read all current PVE configuration"
        )
        guard_names = {
            "Require existing OpenBao-backed PBS identity for verify-only runs",
            "Require existing datastore-scoped ACLs for verify-only runs",
            "Require existing PVE registration and readable backup content for verify-only runs",
        }
        guards = [task for task in validation["tasks"] if task.get("name") in guard_names]
        self.assertEqual({task["name"] for task in guards}, guard_names)
        self.assertTrue(all(set(task) - {"name", "when", "ansible.builtin.assert"} == set() for task in guards))
        cases = [
            ("reuse", [], "verify_access", True),
            ("create", [], "verify_access", False),
            ("reuse", ["o11y-backup@pbs!pve-o11y"], "verify_access", False),
            ("reuse", [], "create", False),
        ]
        for identity_action, missing_auth_ids, storage_action, expect_success in cases:
            with (
                self.subTest(identity_action=identity_action, missing_auth_ids=missing_auth_ids,
                             storage_action=storage_action),
                tempfile.TemporaryDirectory(prefix="pbs-verify-guards-") as temporary,
            ):
                tmp_path = Path(temporary)
                harness = [{
                    "name": "Exercise the production verify refusal chain",
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tags": ["verify"],
                    "vars": {
                        "_pbs_identity_action": identity_action,
                        "_pbs_initial_acl_plan": {"stdout": json.dumps({"missing_auth_ids": missing_auth_ids})},
                        "_pbs_storage_action": storage_action,
                    },
                    "tasks": guards,
                }]
                harness_path = tmp_path / "verify-guards.yml"
                harness_path.write_text(yaml.safe_dump(harness))
                ansible_tmp = tmp_path / "ansible-local"
                ansible_tmp.mkdir()
                completed = subprocess.run(
                    [ansible_playbook, "-i", "localhost,", "-c", "local", "--tags", "verify", str(harness_path)],
                    cwd=ROOT.parent,
                    env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(ansible_tmp), "ANSIBLE_NOCOLOR": "1"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(completed.returncode == 0, expect_success,
                                 completed.stdout + completed.stderr)

    def test_real_playbook_handoffs_validators_and_guards_all_pve_auth_requests(self):
        import yaml

        playbook_path = ROOT / "playbooks" / "configure-pbs-backup-access.yml"
        identity_path = ROOT / "playbooks" / "tasks" / "configure-pbs-backup-identity.yml"
        plays = yaml.safe_load(playbook_path.read_text())
        identity_tasks = yaml.safe_load(identity_path.read_text())
        task_names = [task.get("name", "") for play in plays if isinstance(play, dict)
                      for task in play.get("tasks", [])]
        self.assertIn("Hand off the protected OpenBao endpoint to later PBS guest tasks", task_names)
        guest_play = next(
            play for play in plays
            if play.get("hosts") == "pbs_svc"
            and any(task.get("ansible.builtin.include_tasks") == "tasks/configure-pbs-backup-identity.yml"
                    for task in play.get("tasks", []))
        )
        include = next(task for task in guest_play["tasks"]
                       if task.get("ansible.builtin.include_tasks") == "tasks/configure-pbs-backup-identity.yml")
        self.assertEqual(include["vars"]["_pbs_backup_fingerprint"], "{{ _pbs_fingerprint }}")
        self.assertIn("hostvars['localhost']._pbs_identity_action", include["vars"]["_pbs_backup_identity_action"])
        delegated = [task for task in identity_tasks
                     if task.get("ansible.builtin.command", {}).get("argv", [None])[0] == "python3"]
        self.assertTrue(delegated)
        self.assertTrue(all(task.get("delegate_to") == "localhost" and task.get("become") is False
                            for task in delegated))

        guest_validator_play = next(play for play in plays if play.get("hosts") == "pbs_svc"
                                    and any(task.get("ansible.builtin.command", {}).get("argv", [None])[0] == "python3"
                                            for task in play.get("tasks", [])))
        guest_validators = [task for task in guest_validator_play["tasks"]
                            if task.get("ansible.builtin.command", {}).get("argv", [None])[0] == "python3"]
        self.assertTrue(guest_validators)
        self.assertTrue(all(task.get("delegate_to") == "localhost" and task.get("become") is False
                            for task in guest_validators))

        uri_tasks = [task["ansible.builtin.uri"] for play in plays if isinstance(play, dict)
                     for task in play.get("tasks", []) if "ansible.builtin.uri" in task]
        pve_auth = [task for task in uri_tasks if "Authorization" in task.get("headers", {})]
        self.assertTrue(pve_auth)
        self.assertTrue(all(task.get("follow_redirects") == "none"
                            and task.get("unredirected_headers") == ["Authorization"]
                            and task.get("validate_certs", True) is True
                            for task in pve_auth))

    def test_absent_key_guard_is_an_include_input_not_a_secret_data_field(self):
        import yaml

        identity_path = ROOT / "playbooks" / "tasks" / "configure-pbs-backup-identity.yml"
        tasks = yaml.safe_load(identity_path.read_text())
        include = next(task for task in tasks
                       if task.get("ansible.builtin.include_tasks", {}).get("file") == "bao-merge-keys.yml")
        expected = ["o11y_backup_user", "o11y_backup_token_id", "o11y_backup_token_secret",
                    "o11y_backup_datastore", "o11y_backup_fingerprint"]
        self.assertEqual(include["vars"]["_bm_guard_absent_keys"], expected)
        self.assertNotIn("_bm_guard_absent_keys", include["vars"]["_bm_data"])
        self.assertIs(include["ansible.builtin.include_tasks"]["apply"]["become"], False)

    def test_actual_storage_validator_stdin_templates_parse_render_and_carry_proofs(self):
        import json

        import yaml
        from jinja2 import Environment, StrictUndefined

        playbook_path = ROOT / "playbooks" / "configure-pbs-backup-access.yml"
        plays = yaml.safe_load(playbook_path.read_text())
        env = Environment(undefined=StrictUndefined)
        env.filters["to_json"] = lambda value: json.dumps(value)
        env.tests["skipped"] = lambda value: bool(value.get("skipped", False))
        proof = access_proof()
        expected = expected_storage()
        ctx = {
            "pbs_storage_id": "pbs-o11y",
            "pbs_api_host": "pbs.internal.test",
            "pbs_storage_nodes": ["node-a"],
            "_pve_host": "https://pve.internal.test",
            "_pve_snapshot": {"storage_rows": [], "backup_jobs": [], "nodes": [{"node": "node-a"}]},
            "_pve_managed_storage_read": {"skipped": True, "status": 404, "json": {}},
            "_pbs_saved_token_record": None,
            "_pbs_guest_fingerprint": "a" * 64,
            "_pve_existing_access_reads": {"results": [
                {"item": "node-a", "url": proof["url"], "status": 200, "json": {"data": []}}
            ]},
            "_pve_existing_access_proofs": [proof],
            "_pve_before_write": {"storage_rows": [], "backup_jobs": [], "nodes": [{"node": "node-a"}]},
            "_pve_before_write_target": {"status": 404, "json": {}},
            "_pve_final_snapshot": {
                "storage_rows": [],
                "backup_jobs": [],
                "nodes": [{"node": "node-a"}],
                "target": None,
            },
            "_pve_final_access_reads": {"results": [
                {"item": "node-a", "url": proof["url"], "status": 200, "json": {"data": []}}
            ]},
            "_pve_final_access_proofs": [proof],
            "hostvars": {
                "localhost": {
                    "_pve_storage_rows_before": [], "_pve_backup_jobs_before": [],
                    "_pve_nodes_before": [{"node": "node-a"}], "_pve_host": "https://pve.internal.test",
                    "_pve_pbs_storage_form": expected,
                },
                "pbs-guest": {"_pbs_token_secret": "private-token", "_pbs_fingerprint": "a" * 64},
            },
            "groups": {"pbs_svc": ["pbs-guest"]},
        }
        rendered = []
        for play in plays:
            for task in play.get("tasks", []):
                command = task.get("ansible.builtin.command", {})
                argv = command.get("argv", [])
                if len(argv) < 2 or "reconcile_pbs_pve_storage.py" not in argv[1]:
                    continue
                stdin_template = command["stdin"]
                env.parse(stdin_template)
                payload = json.loads(env.from_string(stdin_template).render(**ctx))
                rendered.append(payload)

        self.assertEqual(len(rendered), 4, "render every real storage-validator stdin payload")
        for payload in rendered:
            if "access_proofs" in payload:
                self.assertEqual(payload["access_proofs"], [proof])

    def test_openbao_guarded_write_precedes_any_pbs_acl_grant(self):
        identity_task = (ROOT / "playbooks" / "tasks" / "configure-pbs-backup-identity.yml").read_text()
        persist = identity_task.index("file: bao-merge-keys.yml")
        verify = identity_task.index("Require OpenBao to hold the exact stable PBS credential fields")
        grant_user = identity_task.index("Grant only the non-propagating DatastoreBackup role to the PBS user")
        grant_token = identity_task.index("Grant only the non-propagating DatastoreBackup role to the PBS token")
        self.assertLess(persist, verify)
        self.assertLess(verify, grant_user)
        self.assertLess(verify, grant_token)

    def test_guarded_openbao_create_race_cannot_fall_back_to_an_unguarded_patch(self):
        merge_task = (ROOT / "playbooks" / "tasks" / "bao-merge-keys.yml").read_text()
        self.assertIn("options:\n        cas: 0", merge_task)
        self.assertIn("_bm_guard_values is not defined", merge_task)
        self.assertIn("Refuse a concurrent guarded creation", merge_task)
        self.assertIn("_bm_create.status == 200", merge_task)

    def test_destination_restore_reader_saved_state_handoff_runs_as_separate_tasks(self):
        ansible_playbook = shutil.which("ansible-playbook")
        if ansible_playbook is None:
            self.skipTest("needs ansible-playbook")
        plays = yaml.safe_load((ROOT / "playbooks" / "configure-o11y-pbs-copy-access.yml").read_text())
        tasks = [task for play in plays for task in play.get("tasks", [])]
        retain_manager = next(
            task for task in tasks
            if task.get("name") == "Retain only the destination manager fields from the credential record"
        )
        retain_reader_keys = next(
            task for task in tasks
            if task.get("name") == "Retain only destination restore Reader fields from the credential record"
        )
        build_reader_record = next(
            task for task in tasks
            if task.get("name") == "Build the closed destination restore Reader credential record"
        )
        self.assertEqual(
            set(retain_reader_keys["ansible.builtin.set_fact"]),
            {"_pbs_restore_reader_saved_keys"},
        )
        self.assertEqual(
            set(build_reader_record["ansible.builtin.set_fact"]),
            {"_pbs_restore_reader_saved_record"},
        )

        expected_record = {
            "user": "o11y-copy-restore@pbs",
            "token_id": "o11y-copy-restore@pbs!pve-restore",
            "secret": "synthetic-reader-secret",
            "datastore": "o11y",
            "fingerprint": "a" * 64,
        }
        cases = (
            (
                {"status": 200, "json": {"data": {"data": {
                    "o11y_copy_restore_user": expected_record["user"],
                    "o11y_copy_restore_token_id": expected_record["token_id"],
                    "o11y_copy_restore_token_secret": expected_record["secret"],
                    "o11y_copy_restore_datastore": expected_record["datastore"],
                    "o11y_copy_restore_fingerprint": expected_record["fingerprint"],
                    "unrelated_manager_secret": "must-not-be-copied",
                }}}},
                ["o11y_copy_restore_user", "o11y_copy_restore_token_id", "o11y_copy_restore_token_secret",
                 "o11y_copy_restore_datastore", "o11y_copy_restore_fingerprint"],
                expected_record,
            ),
            ({"status": 404, "json": {}}, [], None),
        )
        for bao_read, expected_keys, expected_saved in cases:
            with self.subTest(status=bao_read["status"]), tempfile.TemporaryDirectory(
                prefix="pbs-restore-reader-handoff-"
            ) as temporary:
                tmp_path = Path(temporary)
                harness = [{
                    "name": "Exercise the production destination restore Reader state handoff",
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {"_pbs_manager_bao_read": bao_read},
                    "tasks": [retain_manager, retain_reader_keys, build_reader_record, {
                        "name": "Check the retained destination Reader state without printing it",
                        "ansible.builtin.assert": {
                            "that": [
                                f"_pbs_restore_reader_saved_keys == {expected_keys!r}",
                                f"_pbs_restore_reader_saved_record == {expected_saved!r}",
                            ]
                        },
                        "no_log": True,
                    }],
                }]
                harness_path = tmp_path / "restore-reader-state.yml"
                harness_path.write_text(yaml.safe_dump(harness))
                ansible_tmp = tmp_path / "ansible-local"
                ansible_tmp.mkdir()
                completed = subprocess.run(
                    [ansible_playbook, "-i", "localhost,", "-c", "local", str(harness_path)],
                    cwd=ROOT.parent,
                    env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(ansible_tmp), "ANSIBLE_NOCOLOR": "1"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_copy_readiness_passes_fixed_source_and_destination_profiles_from_real_task(self):
        from jinja2 import StrictUndefined

        plays = yaml.safe_load((ROOT / "playbooks" / "verify-o11y-protected-pbs-copy.yml").read_text())
        task = next(
            task for play in plays for task in play.get("tasks", [])
            if task.get("name") == "Read source and destination manifests independently over pinned TLS"
        )
        self.assertEqual([row.get("identity_profile") for row in task["loop"]], ["source", "destination_restore"])
        env = Environment(undefined=StrictUndefined)
        env.filters["to_json"] = lambda value: json.dumps(value)
        context = {
            "_pbs_source_api_host": "pbs-source.internal.test",
            "_pbs_copy_api_host": "pbs-destination.internal.test",
            "_pbs_copy_secret_record": {
                "o11y_copy_reader_fingerprint": "a" * 64,
                "o11y_copy_reader_token_id": pbs.COPY_READER_TOKEN_ID,
                "o11y_copy_reader_token_secret": "synthetic-source-reader",
                "o11y_copy_restore_fingerprint": "b" * 64,
                "o11y_copy_restore_token_id": pbs.COPY_READER_PROFILES["destination_restore"]["token_id"],
                "o11y_copy_restore_token_secret": "synthetic-destination-reader",
            },
            "_pbs_snapshot": {"backup_id": "120", "backup_time": 1792594800},
        }
        manifest = {
            "backup-type": "vm",
            "backup-id": "120",
            "backup-time": 1792594800,
            "files": [{"filename": "qemu-server.conf.blob", "crypt-mode": "none", "size": 19, "csum": "2" * 64}],
        }
        content = json.dumps(manifest, separators=(",", ":")).encode()

        class Response:
            status = 200

            def read(self, _limit):
                return content

        class PinnedConnection:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self):
                return None

            def request(self, *_args, **_kwargs):
                return None

            def getresponse(self):
                return Response()

            def close(self):
                return None

        rendered = []
        with patch.object(pbs, "_PinnedHTTPSConnection", PinnedConnection):
            for row in task["loop"]:
                item = {
                    key: env.from_string(value).render(**context) if isinstance(value, str) and "{{" in value else value
                    for key, value in row.items()
                }
                stdin_payload = json.loads(env.from_string(task["ansible.builtin.command"]["stdin"]).render(
                    **context, item=item
                ))
                result = pbs.read_snapshot_manifest(**stdin_payload)
                rendered.append((item["identity_profile"], result["artifact_fingerprint"]))

        self.assertEqual(
            rendered,
            [("source", pbs.artifact_fingerprint(manifest)),
             ("destination_restore", pbs.artifact_fingerprint(manifest))],
        )


if __name__ == "__main__":
    main()
