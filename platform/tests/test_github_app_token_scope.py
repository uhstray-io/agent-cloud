"""The downscoped installation token (github_app_token.py installation-token with --permissions and --repository).

What these hold is the property the Dispatch Console depends on: a token minted with a read-only
grant cannot come back wider than it asked for. A grant the response reports as wider is refused
before anything reaches stdout, and the diagnostic names keys and repositories, never the token.
The GitHub API is replaced by a recorder and the signing step by a stub: no network is touched and
no key is read.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import github_app_token as gat  # noqa: E402

ORG = "uhstray-io"
REPO = "agent-cloud"
TOKEN = "ghs_fake_token_for_scope_tests"
IID = 42
READ = {"contents": "read", "checks": "read", "actions": "read", "pull_requests": "read", "metadata": "read"}


class Installation:
    """Answers the endpoints a mint touches, reporting a fixed grant.

    `omit` names response keys to leave out entirely, so a reply that cannot be checked is
    representable as well as a wider one.
    """

    def __init__(self, permissions=READ, repositories=(REPO,), *, omit=()):
        self.permissions = permissions
        self.repositories = repositories
        self.omit = set(omit)
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, path, bearer, method="GET", body=None):
        self.calls.append((method, path, body))
        if path == "/app/installations":
            return [{"id": IID, "account": {"login": ORG}}]
        if path == f"/app/installations/{IID}/access_tokens" and method == "POST":
            data = {"token": TOKEN, "expires_at": "2026-10-29T12:00:00Z"}
            if "permissions" not in self.omit:
                data["permissions"] = self.permissions
            if "repositories" not in self.omit:
                data["repositories"] = [{"name": n, "full_name": f"{ORG}/{n}"} for n in self.repositories]
            return data
        raise AssertionError(f"unexpected call {method} {path}")

    def mints(self):
        return [b for m, p, b in self.calls if m == "POST"]


def run_cli(argv, installation):
    """Run the real CLI; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with (
        patch.object(sys, "argv", ["github_app_token.py", *argv]),
        patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"KEY"))),
        patch.object(gat, "api", installation),
        patch.object(gat, "sign_assertion", return_value="jwt"),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            gat.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def scoped_argv(*more):
    return [
        "installation-token",
        "--issuer",
        "i",
        "--org",
        ORG,
        "--permissions",
        json.dumps(READ),
        "--repository",
        REPO,
        *more,
    ]


def test_a_scoped_mint_asks_for_exactly_the_permissions_and_repositories_it_names():
    inst = Installation()
    with patch.object(gat, "api", inst), patch.object(gat, "sign_assertion", return_value="jwt"):
        minted = gat.installation_token("i", b"KEY", ORG, permissions=READ, repositories=[REPO])
    assert minted == TOKEN
    assert inst.mints() == [{"permissions": READ, "repositories": [REPO]}]


def test_an_unscoped_mint_sends_no_body_as_it_always_has():
    inst = Installation()
    with patch.object(gat, "api", inst), patch.object(gat, "sign_assertion", return_value="jwt"):
        assert gat.installation_token("i", b"KEY", ORG) == TOKEN
    assert inst.mints() == [None]


def test_an_exact_grant_puts_only_the_token_on_stdout():
    inst = Installation()
    code, out, err = run_cli(scoped_argv(), inst)
    assert code == 0
    assert out == TOKEN
    assert TOKEN not in err
    assert inst.mints() == [{"permissions": READ, "repositories": [REPO]}]


def test_a_narrower_grant_than_requested_is_accepted():
    # A reply that grants less than asked is a narrower token, not a wider one.
    code, out, _ = run_cli(scoped_argv(), Installation(permissions={"contents": "read"}))
    assert code == 0
    assert out == TOKEN


# Each refusal and its exit code. A grant wider than requested exits GRANT_WIDER_EXIT (6), which is what
# refresh-console-github-token.yml matches on. A response that cannot be checked, or that covers fewer
# repositories than asked, is not a wider grant and exits 1.
REFUSALS = [
    pytest.param(
        {"permissions": {**READ, "administration": "write"}},
        "administration",
        gat.GRANT_WIDER_EXIT,
        id="an-extra-permission",
    ),
    pytest.param({"permissions": {**READ, "contents": "write"}}, "contents", gat.GRANT_WIDER_EXIT, id="a-raised-level"),
    pytest.param({"repositories": (REPO, "other-repo")}, "other-repo", gat.GRANT_WIDER_EXIT, id="an-extra-repository"),
    pytest.param({"repositories": ()}, REPO, 1, id="a-repository-missing"),
    pytest.param({"omit": {"permissions"}}, "permissions", 1, id="no-permissions-object"),
    pytest.param({"omit": {"repositories"}}, "repositories", 1, id="no-repositories-list"),
]


def test_the_wider_grant_exit_code_is_six():
    # refresh-console-github-token.yml names this code; a change here must change the playbook with it.
    assert gat.GRANT_WIDER_EXIT == 6


@pytest.mark.parametrize("grant, named, exit_code", REFUSALS)
def test_a_refused_grant_exits_with_its_code_and_names_only_keys_and_repositories(grant, named, exit_code):
    code, out, err = run_cli(scoped_argv(), Installation(**grant))
    assert code == exit_code
    assert out == ""
    assert TOKEN not in err
    assert named in err


@pytest.mark.parametrize("raw", ["", "not json", "[]", "{}", '{"contents": 1}'])
def test_permissions_must_be_a_non_empty_object_of_string_levels(raw):
    inst = Installation()
    code, out, _ = run_cli(
        ["installation-token", "--issuer", "i", "--org", ORG, "--permissions", raw, "--repository", REPO],
        inst,
    )
    assert code == 2
    assert out == ""
    assert inst.calls == []


def test_the_scope_flags_are_refused_for_every_other_action():
    inst = Installation()
    code, out, _ = run_cli(["installation-id", "--issuer", "i", "--org", ORG, "--repository", REPO], inst)
    assert code == 2
    assert out == ""
    assert inst.calls == []
