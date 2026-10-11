from __future__ import annotations

import contextlib
import http.client
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import github_app_repository as grant  # noqa: E402
import github_app_token as app_token_module  # noqa: E402

CLASSIC_TOKEN = "ghp_syntheticpat"
OAUTH_TOKEN = "gho_syntheticoauthtoken"
PEM = b"synthetic-private-key"
ISSUER = "synthetic-client-id"
REPOSITORY_ID = 9001
APP_ID = 41
INSTALLATION_ID = 72


def repository(**changes):
    value = {
        "id": REPOSITORY_ID,
        "name": "site-config",
        "full_name": "uhstray-io/site-config",
        "private": True,
        "owner": {"login": "uhstray-io"},
        "permissions": {"admin": True},
    }
    value.update(changes)
    return value


def installation(**changes):
    value = {
        "id": INSTALLATION_ID,
        "app_id": APP_ID,
        "repository_selection": "selected",
        "suspended_at": None,
        "account": {"login": "uhstray-io", "type": "Organization"},
    }
    value.update(changes)
    return value


def app(**changes):
    value = {
        "id": APP_ID,
        "slug": "uhstray-io-runner-automation",
        "owner": {"login": "uhstray-io", "type": "Organization"},
    }
    value.update(changes)
    return value


class FakeGitHub:
    def __init__(
        self,
        *,
        present=False,
        overrides=None,
        page_mode=False,
        verify_after_put=True,
        put_status=204,
    ):
        self.present = present
        self.overrides = overrides or {}
        self.page_mode = page_mode
        self.verify_after_put = verify_after_put
        self.put_status = put_status
        self.calls = []
        self.token_count = 0

    def request(self, method, path, token, body=None):
        self.calls.append((method, path, token, body))
        override = self.overrides.get((method, path))
        if isinstance(override, BaseException):
            raise override
        if override is not None:
            return override
        if method == "GET" and path == "/user":
            return grant.Response(200, {"X-OAuth-Scopes": "read:user, repo"}, {"login": "runner-bot"})
        if method == "GET" and path == "/repos/uhstray-io/site-config":
            return grant.Response(200, {}, repository())
        if method == "GET" and path == "/app":
            return grant.Response(200, {}, app())
        if method == "GET" and path == "/orgs/uhstray-io/installation":
            return grant.Response(200, {}, installation())
        if method == "POST" and path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            self.token_count += 1
            return grant.Response(201, {}, {"token": f"token-{self.token_count}"})
        if method == "GET" and path == "/installation/repositories?per_page=100&page=1":
            if self.page_mode:
                first_page = [{"id": n} for n in range(1, 101)]
                return grant.Response(
                    200,
                    {"Link": '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'},
                    {"total_count": 101, "repositories": first_page},
                )
            visible = self.present and self.token_count == 1
            visible = visible or (self.present and self.token_count > 1)
            if self.verify_after_put is False and self.token_count > 1:
                visible = False
            ids = [REPOSITORY_ID] if visible else []
            return grant.Response(200, {}, {"total_count": len(ids), "repositories": [{"id": i} for i in ids]})
        if method == "GET" and path.endswith("/installation/repositories?per_page=100&page=2"):
            return grant.Response(200, {}, {"total_count": 101, "repositories": [{"id": REPOSITORY_ID}]})
        if method == "PUT" and path == f"/user/installations/{INSTALLATION_ID}/repositories/{REPOSITORY_ID}":
            self.present = True
            return grant.Response(self.put_status, {}, None)
        raise AssertionError(f"unexpected request: {method} {path}")

    def put_count(self):
        return sum(method == "PUT" for method, *_ in self.calls)


class GitHubAppRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.signer = patch.object(grant, "sign_assertion", return_value="synthetic-jwt")
        self.signer.start()
        self.addCleanup(self.signer.stop)

    def test_check_mode_reports_missing_access_without_put(self):
        api = FakeGitHub()
        result = grant.execute(CLASSIC_TOKEN, PEM, ISSUER, check_mode=True, verify_only=False, api=api)
        self.assertEqual(result["outcome"], "would_add")
        self.assertFalse(result["grant_attempted"])
        self.assertEqual(api.put_count(), 0)

    def test_apply_uses_one_put_and_fresh_app_token_readback(self):
        api = FakeGitHub()
        result = grant.execute(CLASSIC_TOKEN, PEM, ISSUER, check_mode=False, verify_only=False, api=api)
        self.assertEqual(result["outcome"], "added")
        self.assertTrue(result["verified"])
        self.assertEqual(api.put_count(), 1)
        self.assertEqual(api.token_count, 2)

    def test_304_is_accepted_only_after_fresh_visibility_verification(self):
        api = FakeGitHub(put_status=304)
        result = grant.execute(CLASSIC_TOKEN, PEM, ISSUER, check_mode=False, verify_only=False, api=api)
        self.assertEqual(result["http_status"], 304)
        self.assertTrue(result["verified"])
        self.assertEqual(api.put_count(), 1)

    def test_rerun_is_noop_after_grant(self):
        api = FakeGitHub()
        grant.execute(CLASSIC_TOKEN, PEM, ISSUER, check_mode=False, verify_only=False, api=api)
        result = grant.execute(CLASSIC_TOKEN, PEM, ISSUER, check_mode=False, verify_only=False, api=api)
        self.assertEqual(result["outcome"], "present")
        self.assertFalse(result["grant_attempted"])
        self.assertEqual(api.put_count(), 1)

    def test_already_visible_repository_is_noop(self):
        api = FakeGitHub(present=True)
        result = grant.execute(CLASSIC_TOKEN, PEM, ISSUER, check_mode=False, verify_only=False, api=api)
        self.assertEqual(result["outcome"], "present")
        self.assertEqual(api.put_count(), 0)

    def test_membership_on_second_page_is_found(self):
        api = FakeGitHub(page_mode=True)
        result = grant.execute(CLASSIC_TOKEN, PEM, ISSUER, check_mode=False, verify_only=False, api=api)
        self.assertEqual(result["outcome"], "present")
        self.assertEqual(api.put_count(), 0)
        self.assertTrue(any("page=2" in path for _, path, _, _ in api.calls))

    def test_verify_mode_refuses_missing_access_without_put(self):
        api = FakeGitHub()
        with self.assertRaisesRegex(grant.Refusal, "not_granted"):
            grant.execute(CLASSIC_TOKEN, PEM, ISSUER, check_mode=False, verify_only=True, api=api)
        self.assertEqual(api.put_count(), 0)

    def test_refuses_wrong_app_identity_before_put(self):
        api = FakeGitHub(overrides={("GET", "/app"): grant.Response(200, {}, app(slug="different-app"))})
        with self.assertRaisesRegex(grant.Refusal, "identity_refused"):
            grant.execute(CLASSIC_TOKEN, PEM, ISSUER, False, False, api)
        self.assertEqual(api.put_count(), 0)

    def test_refuses_wrong_or_suspended_or_all_repositories_installation(self):
        for changed in (
            {"app_id": APP_ID + 1},
            {"account": {"login": "another-org", "type": "Organization"}},
            {"suspended_at": "2026-01-01T00:00:00Z"},
            {"repository_selection": "all"},
        ):
            with self.subTest(changed=changed):
                api = FakeGitHub(
                    overrides={
                        ("GET", "/orgs/uhstray-io/installation"): grant.Response(200, {}, installation(**changed))
                    }
                )
                with self.assertRaisesRegex(grant.Refusal, "identity_refused"):
                    grant.execute(CLASSIC_TOKEN, PEM, ISSUER, False, False, api)
                self.assertEqual(api.put_count(), 0)

    def test_refuses_public_or_non_admin_repository(self):
        for changed in ({"private": False}, {"permissions": {"admin": False}}):
            with self.subTest(changed=changed):
                api = FakeGitHub(
                    overrides={("GET", "/repos/uhstray-io/site-config"): grant.Response(200, {}, repository(**changed))}
                )
                with self.assertRaisesRegex(grant.Refusal, "metadata_refused"):
                    grant.execute(CLASSIC_TOKEN, PEM, ISSUER, False, False, api)
                self.assertEqual(api.put_count(), 0)

    def test_oauth_token_with_repo_scope_is_accepted(self):
        api = FakeGitHub()
        result = grant.execute(OAUTH_TOKEN, PEM, ISSUER, False, False, api)
        self.assertEqual(result["outcome"], "added")
        self.assertTrue(result["verified"])
        self.assertEqual(api.put_count(), 1)

    def test_refuses_unsupported_token_form_before_request(self):
        api = FakeGitHub()
        with self.assertRaisesRegex(grant.Refusal, "supported_token_required"):
            grant.execute("github_pat_synthetic", PEM, ISSUER, False, False, api)
        self.assertEqual(api.calls, [])

    def test_refuses_missing_repo_scope_for_classic_and_oauth_tokens_before_put(self):
        for token in (CLASSIC_TOKEN, OAUTH_TOKEN):
            with self.subTest(token=token):
                api = FakeGitHub(
                    overrides={
                        ("GET", "/user"): grant.Response(
                            200, {"X-OAuth-Scopes": "read:user"}, {"login": "runner"}
                        )
                    }
                )
                with self.assertRaisesRegex(grant.Refusal, "repo_scope_required"):
                    grant.execute(token, PEM, ISSUER, False, False, api)
                self.assertEqual(api.put_count(), 0)

    def test_refuses_incomplete_or_hostile_pagination_before_put(self):
        for link, count, page_ids in (
            (
                '<https://api.github.com.evil/installation/repositories?per_page=100&page=2>; rel="next"',
                101,
                list(range(1, 101)),
            ),
            ("", 2, [1]),
        ):
            with self.subTest(link=link, count=count):
                api = FakeGitHub(
                    overrides={
                        ("GET", "/installation/repositories?per_page=100&page=1"): grant.Response(
                            200, {"Link": link}, {"total_count": count, "repositories": [{"id": n} for n in page_ids]}
                        )
                    }
                )
                with self.assertRaises(grant.Refusal):
                    grant.execute(CLASSIC_TOKEN, PEM, ISSUER, False, False, api)
                self.assertEqual(api.put_count(), 0)

    def test_put_timeout_is_unverified_and_never_rolled_back(self):
        path = f"/user/installations/{INSTALLATION_ID}/repositories/{REPOSITORY_ID}"
        api = FakeGitHub(overrides={("PUT", path): grant.Refusal("request", "transport_unavailable")})
        with self.assertRaises(grant.Refusal) as raised:
            grant.execute(CLASSIC_TOKEN, PEM, ISSUER, False, False, api)
        self.assertEqual(raised.exception.outcome, "grant_outcome_unverified")
        self.assertTrue(raised.exception.attempted)
        self.assertEqual(api.put_count(), 1)
        self.assertFalse(any(method == "DELETE" for method, *_ in api.calls))

    def test_bad_status_line_during_put_is_unverified(self):
        path = f"/user/installations/{INSTALLATION_ID}/repositories/{REPOSITORY_ID}"
        api = FakeGitHub(overrides={("PUT", path): http.client.BadStatusLine("synthetic response")})
        with (
            patch.object(grant, "GitHub", return_value=api),
            patch.object(sys, "argv", ["github_app_repository.py", "--issuer", ISSUER]),
            patch.object(
                sys,
                "stdin",
                io.StringIO(json.dumps({"installation_admin_token": CLASSIC_TOKEN, "app_private_key": PEM.decode()})),
            ),
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = grant.main()
        summary = json.loads(stdout.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(summary["phase"], "grant")
        self.assertEqual(summary["outcome"], "grant_outcome_unverified")
        self.assertTrue(summary["grant_attempted"])
        self.assertNotIn("synthetic response", stdout.getvalue())
        self.assertEqual(api.put_count(), 1)

    def test_204_without_fresh_visibility_is_unverified(self):
        api = FakeGitHub(verify_after_put=False)
        with self.assertRaisesRegex(grant.Refusal, "grant_outcome_unverified") as raised:
            grant.execute(CLASSIC_TOKEN, PEM, ISSUER, False, False, api)
        self.assertTrue(raised.exception.attempted)
        self.assertEqual(api.put_count(), 1)

    def test_failure_output_does_not_contain_synthetic_secrets(self):
        api = FakeGitHub(
            overrides={("GET", "/user"): grant.Response(403, {}, {"message": CLASSIC_TOKEN + PEM.decode()})}
        )
        with (
            patch.object(grant, "GitHub", return_value=api),
            patch.object(sys, "argv", ["github_app_repository.py", "--issuer", ISSUER]),
            patch.object(
                sys,
                "stdin",
                io.StringIO(json.dumps({"installation_admin_token": CLASSIC_TOKEN, "app_private_key": PEM.decode()})),
            ),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = grant.main()
        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertIn('"outcome": "forbidden"', output)
        self.assertNotIn(CLASSIC_TOKEN, output)
        self.assertNotIn(PEM.decode(), output)
        self.assertNotIn(CLASSIC_TOKEN, stderr.getvalue())
        self.assertNotIn(PEM.decode(), stderr.getvalue())

    def test_malformed_private_key_returns_bounded_json_without_signer_diagnostics(self):
        pem = "malformed synthetic key"
        with (
            patch.object(grant, "sign_assertion", app_token_module.sign_assertion),
            patch.object(grant, "GitHub", return_value=FakeGitHub()),
            patch.object(sys, "argv", ["github_app_repository.py", "--issuer", ISSUER]),
            patch.object(
                sys,
                "stdin",
                io.StringIO(json.dumps({"installation_admin_token": CLASSIC_TOKEN, "app_private_key": pem})),
            ),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = grant.main()

        output = stdout.getvalue()
        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(output),
            {
                "phase": "app",
                "outcome": "assertion_signing_refused",
                "http_status": 0,
                "grant_attempted": False,
                "verified": False,
            },
        )
        self.assertLessEqual(len(output.splitlines()), 1)
        self.assertNotIn("could not be parsed", stderr.getvalue())
        self.assertNotIn("malformed synthetic key", stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_api_client_rejects_non_api_destinations(self):
        client = grant.GitHub()
        for destination in (
            "https://api.github.com.evil/repos/uhstray-io/site-config",
            "http://api.github.com/repos/uhstray-io/site-config",
            "//attacker.invalid/path",
        ):
            with self.subTest(destination=destination), self.assertRaisesRegex(grant.Refusal, "invalid_destination"):
                client.request("GET", destination, "synthetic-token")


if __name__ == "__main__":
    unittest.main()
