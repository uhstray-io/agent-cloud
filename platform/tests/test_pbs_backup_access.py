from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import Mock, patch


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
        "disable": "0",
        "shared": "0",
    }


def existing_storage(password="**********"):
    result = {key: value for key, value in expected_storage(password).items() if key != "prune-backups"}
    result.update({"storage": "pbs-o11y", "nodes": "node-a", "prune-backups": "keep-all=1"})
    return result


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
            pbs.plan_identity(users=[self.user], tokens=[self.token], secret_record=self.saved, authenticated=True)["action"],
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


class PbsProbeTests(TestCase):
    def test_certificate_mismatch_stops_before_request_header(self):
        connection = Mock()
        connection.connect.side_effect = pbs.Refusal("PBS TLS certificate fingerprint mismatch; no credential was sent.")
        with patch.object(pbs, "_PinnedHTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(pbs.Refusal, "no credential was sent"):
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
        )
        self.assertEqual(existing["action"], "noop")

    def test_mismatch_duplicate_and_create_race_refuse(self):
        jobs = []
        cases = (
            {"storage_id": "pbs-o11y", "rows": [], "current": existing_storage(), "expected": expected_storage(), "jobs_before": jobs},
            {"storage_id": "pbs-o11y", "rows": [{"storage": "pbs-o11y"}] * 2, "current": None, "expected": expected_storage(), "jobs_before": jobs},
            {"storage_id": "pbs-o11y", "rows": [{"storage": "pbs-o11y"}], "current": dict(existing_storage(), server="other.test"), "expected": expected_storage(), "jobs_before": jobs},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(pve.Refusal):
                pve.plan_storage(**case)

    def test_changed_existing_jobs_or_storage_during_registration_refuse(self):
        before = [{"id": "nightly", "schedule": "daily"}]
        after = [{"id": "nightly", "schedule": "weekly"}]
        with self.assertRaisesRegex(pve.Refusal, "backup-job configuration changed"):
            pve.plan_storage(storage_id="pbs-o11y", rows=[], current=None, expected=expected_storage(), jobs_before=before, jobs_after=after)

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

    def test_exact_concurrent_storage_create_is_accepted_after_full_readback(self):
        old_rows = [{"storage": "existing", "type": "dir", "path": "/mnt/backups"}]
        rows_now = old_rows + [{"storage": "pbs-o11y", "type": "pbs"}]
        result = pve.validate_prewrite_snapshot(
            storage_id="pbs-o11y", rows_before=old_rows, rows_now=rows_now,
            current=existing_storage(), expected=expected_storage(),
            jobs_before=[], jobs_now=[], nodes_before=[{"node": "node-a"}],
            nodes_now=[{"node": "node-a"}],
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
    def test_check_mode_skips_identity_mutation_and_pve_write(self):
        playbook = (ROOT / "playbooks" / "configure-pbs-backup-access.yml").read_text()
        identity_task = (ROOT / "playbooks" / "tasks" / "configure-pbs-backup-identity.yml").read_text()
        self.assertIn("include_tasks: tasks/configure-pbs-backup-identity.yml", playbook)
        self.assertIn("when: not ansible_check_mode", playbook)
        self.assertIn("method: POST", playbook)
        self.assertIn("- not ansible_check_mode", playbook)
        self.assertIn("include_tasks: bao-merge-keys.yml", identity_task)
        self.assertIn("_bm_guard_values: {}", identity_task)

    def test_openbao_guarded_write_precedes_any_pbs_acl_grant(self):
        identity_task = (ROOT / "playbooks" / "tasks" / "configure-pbs-backup-identity.yml").read_text()
        persist = identity_task.index("include_tasks: bao-merge-keys.yml")
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


if __name__ == "__main__":
    main()
