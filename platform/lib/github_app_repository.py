#!/usr/bin/env python3
"""Grant and verify one fixed repository on the runner GitHub App installation.

Secrets arrive as JSON on stdin. Only a bounded operation summary is written to
stdout/stderr; API bodies, headers, tokens, and exception text are never exposed.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from github_app_token import sign_assertion

API = "https://api.github.com"
ORG = "uhstray-io"
APP_SLUG = "uhstray-io-runner-automation"
REPOSITORY = "site-config"
USER_AGENT = "agent-cloud-runner-automation"


class Refusal(Exception):
    def __init__(self, phase: str, outcome: str, status: int = 0, attempted: bool = False):
        self.phase = phase
        self.outcome = outcome
        self.status = status if isinstance(status, int) else 0
        self.attempted = attempted


@dataclass
class Response:
    status: int
    headers: Any
    body: Any


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class GitHub:
    def __init__(self) -> None:
        # Do not inherit proxy credentials or follow a redirect with an Authorization header.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method: str, path_or_url: str, token: str, body: Any = None) -> Response:
        if path_or_url.startswith("/") and not path_or_url.startswith("//"):
            url = API + path_or_url
        elif path_or_url.startswith(API + "/"):
            url = path_or_url
        else:
            raise Refusal("request", "invalid_destination")
        destination = urllib.parse.urlsplit(url)
        if (
            destination.scheme != "https"
            or destination.netloc != "api.github.com"
            or destination.username
            or destination.password
            or destination.fragment
        ):
            raise Refusal("request", "invalid_destination")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read()
                parsed = json.loads(raw) if raw else None
                return Response(response.status, response.headers, parsed)
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.headers, None)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Refusal("request", "transport_unavailable") from None
        except (ValueError, json.JSONDecodeError):
            raise Refusal("response", "malformed_json") from None


def require(condition: bool, phase: str, outcome: str, status: int = 0) -> None:
    if not condition:
        raise Refusal(phase, outcome, status)


def checked(response: Response, accepted: tuple[int, ...], phase: str) -> Response:
    if response.status not in accepted:
        outcome = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(response.status, "unexpected_status")
        raise Refusal(phase, outcome, response.status)
    return response


def positive_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _sign_assertion(issuer: str, key: bytes) -> str:
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return sign_assertion(issuer, key)
    except (Exception, SystemExit):
        raise Refusal("app", "assertion_signing_refused") from None


def validate_repository(api: GitHub, pat: str) -> int:
    response = checked(api.request("GET", f"/repos/{ORG}/{REPOSITORY}", pat), (200,), "repository")
    repo = response.body
    owner = repo.get("owner") if isinstance(repo, dict) else None
    permissions = repo.get("permissions") if isinstance(repo, dict) else None
    require(
        isinstance(repo, dict)
        and repo.get("full_name") == f"{ORG}/{REPOSITORY}"
        and repo.get("name") == REPOSITORY
        and isinstance(owner, dict)
        and owner.get("login") == ORG
        and positive_id(repo.get("id"))
        and repo.get("private") is True
        and isinstance(permissions, dict)
        and permissions.get("admin") is True,
        "repository",
        "metadata_refused",
        response.status,
    )
    return repo["id"]


def validate_app(api: GitHub, issuer: str, key: bytes) -> tuple[int, int]:
    jwt = _sign_assertion(issuer, key)
    app_response = checked(api.request("GET", "/app", jwt), (200,), "app")
    app = app_response.body
    owner = app.get("owner") if isinstance(app, dict) else None
    require(
        isinstance(app, dict)
        and app.get("slug") == APP_SLUG
        and positive_id(app.get("id"))
        and isinstance(owner, dict)
        and owner.get("login") == ORG
        and owner.get("type") == "Organization",
        "app",
        "identity_refused",
        app_response.status,
    )

    installation_response = checked(api.request("GET", f"/orgs/{ORG}/installation", jwt), (200,), "installation")
    installation = installation_response.body
    account = installation.get("account") if isinstance(installation, dict) else None
    require(
        isinstance(installation, dict)
        and positive_id(installation.get("id"))
        and installation.get("app_id") == app["id"]
        and isinstance(account, dict)
        and account.get("login") == ORG
        and account.get("type") == "Organization"
        and installation.get("repository_selection") == "selected"
        and "suspended_at" in installation
        and installation["suspended_at"] is None,
        "installation",
        "identity_refused",
        installation_response.status,
    )
    return app["id"], installation["id"]


def validate_admin_token(api: GitHub, admin_token: str) -> None:
    require(
        isinstance(admin_token, str) and re.fullmatch(r"ghp_[A-Za-z0-9]+", admin_token) is not None,
        "admin_token",
        "supported_token_required",
    )
    response = checked(api.request("GET", "/user", admin_token), (200,), "admin_token")
    require(
        isinstance(response.body, dict) and isinstance(response.body.get("login"), str),
        "admin_token",
        "user_refused",
        response.status,
    )
    scopes = response.headers.get("X-OAuth-Scopes", "") if response.headers else ""
    require("repo" in {scope.strip() for scope in scopes.split(",")}, "admin_token", "repo_scope_required")


def _next_link(link_header: str) -> str | None:
    if not link_header:
        return None
    links = re.split(r",\s*(?=<)", link_header)
    next_links = []
    for link in links:
        match = re.fullmatch(r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"\s*', link)
        if not match:
            raise Refusal("pagination", "malformed_link")
        if "next" in match.group(2).split():
            next_links.append(match.group(1))
    require(len(next_links) <= 1, "pagination", "ambiguous_next_link")
    return next_links[0] if next_links else None


def installation_repository_ids(api: GitHub, token: str) -> set[int]:
    url = "/installation/repositories?per_page=100&page=1"
    expected_page = 1
    total: int | None = None
    ids: set[int] = set()
    while url:
        require(expected_page <= 1000, "pagination", "page_limit")
        response = checked(api.request("GET", url, token), (200,), "repository_list")
        body = response.body
        require(
            isinstance(body, dict)
            and isinstance(body.get("total_count"), int)
            and not isinstance(body.get("total_count"), bool)
            and body["total_count"] >= 0
            and isinstance(body.get("repositories"), list)
            and len(body["repositories"]) <= 100,
            "pagination",
            "malformed_page",
            response.status,
        )
        if total is None:
            total = body["total_count"]
        require(total == body["total_count"], "pagination", "contradictory_total", response.status)
        page_ids = []
        for repo in body["repositories"]:
            require(
                isinstance(repo, dict) and positive_id(repo.get("id")),
                "pagination",
                "malformed_repository",
                response.status,
            )
            page_ids.append(repo["id"])
        require(len(page_ids) == len(set(page_ids)), "pagination", "duplicate_repository", response.status)
        require(not ids.intersection(page_ids), "pagination", "duplicate_repository", response.status)
        ids.update(page_ids)
        next_url = _next_link(response.headers.get("Link", "") if response.headers else "")
        require(len(ids) <= total, "pagination", "contradictory_count", response.status)
        if next_url:
            resolved = urllib.parse.urljoin(API + url, next_url)
            parsed = urllib.parse.urlsplit(resolved)
            query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            require(
                parsed.scheme == "https"
                and parsed.netloc == "api.github.com"
                and parsed.path == "/installation/repositories"
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and set(query) == {"page", "per_page"}
                and query["per_page"] == ["100"]
                and query["page"] == [str(expected_page + 1)],
                "pagination",
                "invalid_destination",
            )
            require(len(ids) < total, "pagination", "contradictory_count", response.status)
            url = resolved
            expected_page += 1
        else:
            require(len(ids) == total, "pagination", "incomplete_list", response.status)
            url = ""
    return ids


def _installation_token(api: GitHub, issuer: str, key: bytes, installation_id: int) -> str:
    jwt = _sign_assertion(issuer, key)
    response = checked(
        api.request("POST", f"/app/installations/{installation_id}/access_tokens", jwt),
        (201,),
        "token",
    )
    token = response.body.get("token") if isinstance(response.body, dict) else None
    require(isinstance(token, str) and bool(token), "token", "malformed_response", response.status)
    return token


def execute(
    admin_token: str,
    key: bytes,
    issuer: str,
    check_mode: bool,
    verify_only: bool,
    api: GitHub | None = None,
) -> dict[str, Any]:
    api = api or GitHub()
    require(bool(admin_token) and bool(key) and bool(issuer), "input", "required_input_missing")
    validate_admin_token(api, admin_token)
    repository_id = validate_repository(api, admin_token)
    _app_id, installation_id = validate_app(api, issuer, key)
    token = _installation_token(api, issuer, key, installation_id)
    present = repository_id in installation_repository_ids(api, token)
    if present:
        return {
            "phase": "complete",
            "outcome": "present",
            "http_status": 200,
            "grant_attempted": False,
            "verified": True,
        }
    if verify_only:
        raise Refusal("verify", "not_granted")
    if check_mode:
        return {
            "phase": "check",
            "outcome": "would_add",
            "http_status": 200,
            "grant_attempted": False,
            "verified": False,
        }

    # Revalidate both independent authorities immediately before the sole additive PUT.
    validate_admin_token(api, admin_token)
    require(validate_repository(api, admin_token) == repository_id, "repository", "metadata_changed")
    _app_id_after, installation_after = validate_app(api, issuer, key)
    require(installation_after == installation_id, "installation", "identity_changed")
    try:
        grant = api.request("PUT", f"/user/installations/{installation_id}/repositories/{repository_id}", admin_token)
    except Exception as exc:
        status = exc.status if isinstance(exc, Refusal) else 0
        raise Refusal("grant", "grant_outcome_unverified", status, True) from None
    if grant.status not in (204, 304):
        outcome = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
            grant.status, "grant_outcome_unverified"
        )
        raise Refusal("grant", outcome, grant.status, True)

    try:
        fresh_token = _installation_token(api, issuer, key, installation_id)
        fresh_ids = installation_repository_ids(api, fresh_token)
        require(repository_id in fresh_ids, "verify", "grant_outcome_unverified", grant.status)
    except Refusal:
        raise Refusal("verify", "grant_outcome_unverified", grant.status, True) from None
    except Exception:
        raise Refusal("verify", "grant_outcome_unverified", grant.status, True) from None
    return {
        "phase": "complete",
        "outcome": "added",
        "http_status": grant.status,
        "grant_attempted": True,
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--check-mode", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        require(isinstance(payload, dict), "input", "malformed_input")
        pem = payload.get("app_private_key")
        result = execute(
            payload.get("installation_admin_token", ""),
            pem.encode() if isinstance(pem, str) else b"",
            args.issuer,
            args.check_mode,
            args.verify_only,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Refusal as exc:
        print(
            json.dumps(
                {
                    "phase": exc.phase,
                    "outcome": exc.outcome,
                    "http_status": exc.status,
                    "grant_attempted": exc.attempted,
                    "verified": False,
                },
                sort_keys=True,
            )
        )
        return 1
    except Exception:
        # Unexpected implementation errors stay opaque; no request or credential detail escapes.
        print(
            json.dumps(
                {
                    "phase": "internal",
                    "outcome": "refused",
                    "http_status": 0,
                    "grant_attempted": False,
                    "verified": False,
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
