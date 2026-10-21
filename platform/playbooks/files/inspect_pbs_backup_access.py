#!/usr/bin/env python3
"""Fail-closed PBS backup identity and effective-permission validators."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import ssl
import sys
from typing import Any
from urllib.parse import urlsplit

USER_ID = "o11y-backup@pbs"
TOKEN_ID = "o11y-backup@pbs!pve-o11y"
DATASTORE = "o11y"
DATASTORE_PATH = "/datastore/o11y"
EXPECTED_PRIVILEGES = {"Datastore.Backup"}
COPY_READER_USER_ID = "o11y-copy-reader@pbs"
COPY_READER_TOKEN_ID = "o11y-copy-reader@pbs!pbs-sync"
COPY_READER_ROLE = "DatastoreReader"
COPY_READER_PRIVILEGES = {"Datastore.Audit", "Datastore.Read", "Datastore.Verify"}
SUPPORTED_VERSION = (4, 2, 8, 1)


class Refusal(ValueError):
    """Input cannot prove the fixed backup-only contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Refusal(message)


def parse_pbs_version(value: Any) -> tuple[int, int, int, int]:
    """Parse only the supported 4.2.8-1 package or its documented JSON fields."""
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, dict):
        package = value.get("package-version") or value.get("package_version")
        text = str(package or "")
        if not text and all(k in value for k in ("version", "release")):
            text = f"{value['version']}-{value['release']}"
    else:
        raise Refusal("PBS version readback has an unsupported schema.")

    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)-(\d+)(?:\+.*)?", text)
    _require(match is not None, "PBS package version is not in the supported package schema.")
    version = tuple(int(part) for part in match.groups())
    _require(version == SUPPORTED_VERSION, "PBS package version is outside the reviewed 4.2.8-1 contract.")
    return version  # type: ignore[return-value]


def _one_by_id(rows: Any, key: str, wanted: str, label: str) -> dict[str, Any] | None:
    _require(isinstance(rows, list), f"PBS {label} readback is not a list.")
    _require(
        all(isinstance(row, dict) and isinstance(row.get(key), str) for row in rows),
        f"PBS {label} readback has a malformed or unsupported row schema.",
    )
    matches = [row for row in rows if row.get(key) == wanted]
    _require(len(matches) <= 1, f"PBS {label} readback duplicates the managed identity.")
    return matches[0] if matches else None


def plan_identity(
    *, users: Any, tokens: Any, secret_record: Any, authenticated: bool = False
) -> dict[str, str]:
    """Return create/reuse only for the two complete safe states; refuse all drift."""
    user = _one_by_id(users, "userid", USER_ID, "user")
    token = _one_by_id(tokens, "tokenid", TOKEN_ID, "token")
    _require(
        secret_record is None or isinstance(secret_record, dict),
        "OpenBao PBS secret record has an unsupported schema.",
    )
    saved = secret_record or {}
    saved_any = bool(saved)
    token_exists = token is not None
    user_exists = user is not None

    if not user_exists and not token_exists and not saved_any:
        return {"action": "create", "user": USER_ID, "token_id": TOKEN_ID}

    _require(user_exists and token_exists, "PBS user and token are incomplete; no identity mutation is allowed.")
    _require(saved_any, "PBS token exists without its OpenBao copy; no rotation or recovery is automatic.")
    _require(
        saved.get("user") == USER_ID and saved.get("token_id") == TOKEN_ID,
        "OpenBao PBS identity conflicts with the stable PBS user or token ID.",
    )
    _require(saved.get("datastore") == DATASTORE, "OpenBao PBS identity names an unexpected datastore.")
    _require(isinstance(saved.get("secret"), str) and bool(saved["secret"]),
             "OpenBao PBS token secret is missing or invalid.")
    _require(isinstance(saved.get("fingerprint"), str) and bool(saved["fingerprint"]),
             "OpenBao PBS certificate fingerprint is missing or invalid.")
    _require(user.get("enable", 0) in (1, True, "1"), "PBS backup user is disabled or has an unsupported enable field.")
    _require(user.get("expire", 0) in (0, "0", None), "PBS backup user has an expiry; automatic changes are refused.")
    _require(
        token.get("enable", 0) in (1, True, "1"),
        "PBS backup token is disabled or has an unsupported enable field.",
    )
    _require(token.get("expire", 0) in (0, "0", None), "PBS backup token has an expiry; automatic renewal is refused.")
    _require(authenticated, "The existing PBS token was not authenticated successfully.")
    return {"action": "reuse", "user": USER_ID, "token_id": TOKEN_ID}


def _effective_map(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize the reviewed PBS JSON permission shape without guessing fields."""
    if isinstance(value, list):
        records = value
        result: dict[str, dict[str, Any]] = {}
        for row in records:
            _require(isinstance(row, dict), "PBS effective-permission JSON has a malformed row.")
            path = row.get("path")
            privileges = row.get("privileges")
            _require(isinstance(path, str) and isinstance(privileges, dict),
                     "PBS effective-permission JSON does not match the reviewed path/privileges schema.")
            _require(path not in result, "PBS effective-permission JSON duplicates a path.")
            result[path] = privileges
        return result
    if isinstance(value, dict):
        # Some builds wrap the list in data; do not accept arbitrary envelope names.
        if set(value) == {"data"}:
            return _effective_map(value["data"])
        if (
            set(value) == {"path", "privileges"}
            and isinstance(value.get("path"), str)
            and isinstance(value.get("privileges"), dict)
        ):
            return {value["path"]: value["privileges"]}
        if all(isinstance(path, str) and isinstance(privs, dict) for path, privs in value.items()):
            return value
    raise Refusal("PBS effective-permission JSON has an unsupported schema.")


def _validate_effective_permissions(value: Any, identity: str) -> dict[str, Any]:
    permissions = _effective_map(value)
    _require(set(permissions) == {DATASTORE_PATH},
             f"PBS {identity} has effective permissions outside the exact o11y datastore path.")
    privilege_map = permissions[DATASTORE_PATH]
    _require(set(privilege_map) == EXPECTED_PRIVILEGES,
             f"PBS {identity} has missing, unknown, destructive, or extra effective privileges.")
    # In PBS effective-permission output the boolean is propagation, not grant.
    # The privilege's presence means it is granted, even when its value is false.
    _require(type(privilege_map["Datastore.Backup"]) is bool,
             f"PBS {identity} has an unsupported propagation value.")
    _require(privilege_map["Datastore.Backup"] is False,
             f"PBS {identity} backup permission propagates beyond the exact datastore.")
    return {"identity": identity, "path": DATASTORE_PATH, "privileges": ["Datastore.Backup"], "propagate": False}


def _validated_acl_rows(value: Any) -> list[dict[str, Any]]:
    _require(isinstance(value, list), "PBS ACL list readback is not a list.")
    for row in value:
        _require(
            isinstance(row, dict)
            and isinstance(row.get("ugid"), str)
            and isinstance(row.get("path"), str)
            and isinstance(row.get("roleid"), str)
            and row.get("propagate") in (0, 1, "0", "1", False, True),
            "PBS ACL list has a malformed or unsupported row schema.",
        )
    return value


def validate_permissions(*, user_permissions: Any, token_permissions: Any, acl_rows: Any) -> dict[str, Any]:
    """Require both user and token to have only the exact non-propagating grant."""
    user_result = _validate_effective_permissions(user_permissions, USER_ID)
    token_result = _validate_effective_permissions(token_permissions, TOKEN_ID)
    acl_rows = _validated_acl_rows(acl_rows)
    relevant = [row for row in acl_rows if isinstance(row, dict) and row.get("ugid") in {USER_ID, TOKEN_ID}]
    _require(len(relevant) == 2, "PBS ACL list does not contain exactly one user and one token grant.")
    expected = {
        (USER_ID, DATASTORE_PATH, "DatastoreBackup", 0),
        (TOKEN_ID, DATASTORE_PATH, "DatastoreBackup", 0),
    }
    actual = set()
    for row in relevant:
        path, role, ugid, propagate = row.get("path"), row.get("roleid"), row.get("ugid"), row.get("propagate")
        _require(
            propagate in (0, "0", False),
            "PBS ACL grants propagation or has an unsupported propagation value.",
        )
        actual.add((ugid, path, role, 0))
    _require(
        actual == expected,
        "PBS ACLs include an ancestor, other datastore, unknown role, or missing identity grant.",
    )
    return {"valid": True, "user": user_result, "token": token_result, "acl_count": 2}


def plan_acl(*, user_permissions: Any, token_permissions: Any, acl_rows: Any) -> dict[str, Any]:
    """Permit only a subset of the exact intended grants before adding missing ACLs."""
    permission_sets = {
        USER_ID: _effective_map(user_permissions),
        TOKEN_ID: _effective_map(token_permissions),
    }
    for identity, paths in permission_sets.items():
        _require(set(paths).issubset({DATASTORE_PATH}),
                 f"PBS {identity} has effective permissions outside the exact o11y datastore path.")
        privileges = paths.get(DATASTORE_PATH, {})
        _require(isinstance(privileges, dict) and set(privileges).issubset(EXPECTED_PRIVILEGES),
                 f"PBS {identity} has unknown, destructive, or extra effective privileges.")
        if "Datastore.Backup" in privileges:
            _require(type(privileges["Datastore.Backup"]) is bool and privileges["Datastore.Backup"] is False,
                     f"PBS {identity} backup permission propagates beyond the exact datastore.")
    acl_rows = _validated_acl_rows(acl_rows)
    relevant = [row for row in acl_rows if isinstance(row, dict) and row.get("ugid") in {USER_ID, TOKEN_ID}]
    seen: set[str] = set()
    for row in relevant:
        identity = row["ugid"]
        _require(identity not in seen, "PBS ACL list duplicates a managed identity grant.")
        seen.add(identity)
        _require(
            row.get("path") == DATASTORE_PATH
            and row.get("roleid") == "DatastoreBackup"
            and row.get("propagate") in (0, "0", False),
            "PBS ACLs include an ancestor, other datastore, propagated grant, or unknown role.",
        )
    missing = [identity for identity in (USER_ID, TOKEN_ID) if identity not in seen]
    return {"missing_auth_ids": missing}


def plan_copy_reader_identity(
    *, users: Any, tokens: Any, secret_record: Any, authenticated: bool = False
) -> dict[str, str]:
    """Manage a separate, stable source-pull identity without backup privileges."""
    user = _one_by_id(users, "userid", COPY_READER_USER_ID, "copy-reader user")
    token = _one_by_id(tokens, "tokenid", COPY_READER_TOKEN_ID, "copy-reader token")
    _require(secret_record is None or isinstance(secret_record, dict),
             "OpenBao PBS copy-reader record has an unsupported schema.")
    saved = secret_record or {}
    saved_any = bool(saved)
    user_exists, token_exists = user is not None, token is not None
    if not user_exists and not token_exists and not saved_any:
        return {"action": "create", "user": COPY_READER_USER_ID, "token_id": COPY_READER_TOKEN_ID}
    _require(user_exists and token_exists,
             "PBS copy-reader user and token are incomplete; no identity mutation is allowed.")
    _require(saved_any, "PBS copy-reader token exists without its OpenBao copy.")
    _require(saved.get("user") == COPY_READER_USER_ID and saved.get("token_id") == COPY_READER_TOKEN_ID,
             "OpenBao PBS copy-reader identity conflicts with its stable ID.")
    _require(saved.get("datastore") == DATASTORE, "OpenBao PBS copy-reader identity names an unexpected datastore.")
    _require(isinstance(saved.get("secret"), str) and bool(saved["secret"]),
             "OpenBao PBS copy-reader token secret is missing or invalid.")
    _require(isinstance(saved.get("fingerprint"), str) and bool(saved["fingerprint"]),
             "OpenBao PBS copy-reader certificate fingerprint is missing or invalid.")
    _require(user.get("enable", 0) in (1, True, "1"), "PBS copy-reader user is disabled.")
    _require(user.get("expire", 0) in (0, "0", None), "PBS copy-reader user has an expiry.")
    _require(token.get("enable", 0) in (1, True, "1"), "PBS copy-reader token is disabled.")
    _require(token.get("expire", 0) in (0, "0", None), "PBS copy-reader token has an expiry.")
    _require(authenticated, "The existing PBS copy-reader token was not authenticated successfully.")
    return {"action": "reuse", "user": COPY_READER_USER_ID, "token_id": COPY_READER_TOKEN_ID}


def plan_copy_reader_acl(*, user_permissions: Any, token_permissions: Any, acl_rows: Any) -> dict[str, Any]:
    """Permit only the exact DatastoreReader grants on the o11y datastore."""
    expected_ids = {COPY_READER_USER_ID, COPY_READER_TOKEN_ID}
    permission_sets = {
        COPY_READER_USER_ID: _effective_map(user_permissions),
        COPY_READER_TOKEN_ID: _effective_map(token_permissions),
    }
    for identity, paths in permission_sets.items():
        _require(set(paths).issubset({DATASTORE_PATH}),
                 f"PBS {identity} has effective permissions outside the exact o11y datastore path.")
        privileges = paths.get(DATASTORE_PATH, {})
        _require(isinstance(privileges, dict) and set(privileges).issubset(COPY_READER_PRIVILEGES),
                 f"PBS {identity} has unknown, write, prune, or extra effective privileges.")
        _require(all(type(value) is bool and value is False for value in privileges.values()),
                 f"PBS {identity} has propagated or unsupported effective permissions.")
    rows = _validated_acl_rows(acl_rows)
    relevant = [row for row in rows if row.get("ugid") in expected_ids]
    seen: set[str] = set()
    for row in relevant:
        identity = row["ugid"]
        _require(identity not in seen, "PBS ACL list duplicates a copy-reader identity grant.")
        seen.add(identity)
        _require(row.get("path") == DATASTORE_PATH and row.get("roleid") == COPY_READER_ROLE
                 and row.get("propagate") in (0, "0", False),
                 "PBS copy-reader ACL is not the exact non-propagating DatastoreReader grant.")
    return {"missing_auth_ids": [identity for identity in (COPY_READER_USER_ID, COPY_READER_TOKEN_ID)
                                  if identity not in seen]}


def validate_copy_reader_permissions(
    *, user_permissions: Any, token_permissions: Any, acl_rows: Any
) -> dict[str, Any]:
    """Verify the final source-pull user and token remain strictly read-only."""
    plan = plan_copy_reader_acl(user_permissions=[], token_permissions=[], acl_rows=acl_rows)
    _require(plan["missing_auth_ids"] == [], "PBS copy-reader ACL list is missing an identity grant.")
    expected = {"identity": "copy_reader", "path": DATASTORE_PATH,
                "privileges": sorted(COPY_READER_PRIVILEGES), "propagate": False}
    for value in (user_permissions, token_permissions):
        permissions = _effective_map(value)
        _require(set(permissions) == {DATASTORE_PATH},
                 "PBS copy-reader permissions include another datastore or ancestor path.")
        privileges = permissions[DATASTORE_PATH]
        _require(set(privileges) == COPY_READER_PRIVILEGES and all(
            type(flag) is bool and flag is False for flag in privileges.values()
        ), "PBS copy-reader permissions are not exactly DatastoreReader on /datastore/o11y.")
    return {"valid": True, "user": expected, "token": expected, "acl_count": 2}


def _fingerprint_bytes(certificate: bytes) -> str:
    return hashlib.sha256(certificate).hexdigest()


def normalize_fingerprint(value: str) -> str:
    normalized = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    _require(len(normalized) == 64, "PBS SHA-256 fingerprint has an invalid format.")
    return normalized


def parse_openssl_fingerprint(value: Any) -> str:
    _require(isinstance(value, str), "PBS guest certificate information has an unsupported schema.")
    match = re.search(r"(?:SHA256\s+)?Fingerprint\s*=\s*((?:[0-9a-fA-F]{2}:?){32})", value)
    _require(match is not None, "PBS guest certificate information has no SHA-256 fingerprint.")
    return normalize_fingerprint(match.group(1))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, fingerprint: str, timeout: float = 15.0):
        super().__init__(host, port=port, context=ssl._create_unverified_context(), timeout=timeout)
        self._expected_fingerprint = normalize_fingerprint(fingerprint)

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        actual = _fingerprint_bytes(self.sock.getpeercert(binary_form=True))
        if actual != self._expected_fingerprint:
            self.close()
            raise Refusal("PBS TLS certificate fingerprint mismatch; no credential was sent.")


def authenticate_token(*, url: str, fingerprint: str, auth_id: str, secret: str) -> dict[str, Any]:
    """Authenticate only after pinning the peer certificate on this same TLS socket."""
    parsed = urlsplit(url)
    _require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password,
             "PBS authentication endpoint must be a credential-free HTTPS URL.")
    port = parsed.port or 8007
    connection = _PinnedHTTPSConnection(parsed.hostname, port, fingerprint)
    path = "/api2/json/access/permissions"
    try:
        connection.connect()  # The fingerprint is checked before an Authorization header exists.
        connection.request("GET", path, headers={"Authorization": f"PBSAPIToken {auth_id}:{secret}"})
        response = connection.getresponse()
        response.read()
        _require(response.status != 401, "PBS rejected the stored token.")
        _require(response.status in (200, 403), "PBS token authentication returned an unsupported response.")
        return {"authenticated": True, "status": response.status}
    finally:
        connection.close()


def _read() -> Any:
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Refusal("PBS access input is not valid JSON.") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "identity",
            "acl-plan",
            "permissions",
            "copy-reader-identity",
            "copy-reader-acl-plan",
            "copy-reader-permissions",
            "version",
            "certificate",
            "probe",
        ),
    )
    args = parser.parse_args()
    try:
        payload = _read()
        if args.action == "identity":
            result = plan_identity(
                users=payload.get("users"),
                tokens=payload.get("tokens"),
                secret_record=payload.get("secret_record"),
                authenticated=payload.get("authenticated") is True,
            )
        elif args.action == "acl-plan":
            result = plan_acl(
                user_permissions=payload.get("user_permissions"),
                token_permissions=payload.get("token_permissions"),
                acl_rows=payload.get("acl_rows"),
            )
        elif args.action == "permissions":
            result = validate_permissions(
                user_permissions=payload.get("user_permissions"),
                token_permissions=payload.get("token_permissions"),
                acl_rows=payload.get("acl_rows"),
            )
        elif args.action == "copy-reader-identity":
            result = plan_copy_reader_identity(
                users=payload.get("users"),
                tokens=payload.get("tokens"),
                secret_record=payload.get("secret_record"),
                authenticated=payload.get("authenticated") is True,
            )
        elif args.action == "copy-reader-acl-plan":
            result = plan_copy_reader_acl(
                user_permissions=payload.get("user_permissions"),
                token_permissions=payload.get("token_permissions"),
                acl_rows=payload.get("acl_rows"),
            )
        elif args.action == "copy-reader-permissions":
            result = validate_copy_reader_permissions(
                user_permissions=payload.get("user_permissions"),
                token_permissions=payload.get("token_permissions"),
                acl_rows=payload.get("acl_rows"),
            )
        elif args.action == "version":
            result = {"supported": True, "package": parse_pbs_version(payload.get("package"))}
        elif args.action == "certificate":
            result = {"fingerprint": parse_openssl_fingerprint(payload.get("openssl_fingerprint"))}
        else:
            result = authenticate_token(
                url=payload.get("url", ""),
                fingerprint=payload.get("fingerprint", ""),
                auth_id=payload.get("auth_id", ""),
                secret=payload.get("secret", ""),
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Refusal as exc:
        # Error messages are fixed strings and never contain the supplied secret.
        print(json.dumps({"refusal": str(exc)}, sort_keys=True))
        return 2
    except (OSError, ssl.SSLError, http.client.HTTPException):
        print(json.dumps({"refusal": "PBS certificate-pinned authentication probe failed."}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
