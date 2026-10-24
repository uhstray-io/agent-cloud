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
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from o11y_pbs_receipts import artifact_fingerprint, parse_manifest

USER_ID = "o11y-backup@pbs"
TOKEN_ID = "o11y-backup@pbs!pve-o11y"
DATASTORE = "o11y"
DATASTORE_PATH = "/datastore/o11y"
EXPECTED_PRIVILEGES = {"Datastore.Backup"}
COPY_READER_USER_ID = "o11y-copy-reader@pbs"
COPY_READER_TOKEN_ID = "o11y-copy-reader@pbs!pbs-sync"
COPY_READER_ROLE = "DatastoreReader"
COPY_READER_PRIVILEGES = {"Datastore.Audit", "Datastore.Read", "Datastore.Verify"}
COPY_MANAGER_USER_ID = "o11y-copy-manager@pbs"
COPY_MANAGER_TOKEN_ID = "o11y-copy-manager@pbs!copy-api"
COPY_MANAGER_ACL_ROLE_PATHS = {
    "remote-root": ("RemoteAdmin", 0),
    "remote-item": ("RemoteAdmin", 0),
    "remote-datastore": ("RemoteSyncOperator", 0),
    "destination-datastore": (
        ("DatastoreAudit", 0),
        ("DatastoreBackup", 0),
        ("DatastoreReader", 0),
    ),
}
SUPPORTED_VERSION = (4, 2, 8, 1)
MAX_API_RESPONSE_BYTES = 1024 * 1024
MAX_MANIFEST_BLOB_BYTES = 1024 * 1024
COPY_API_OPERATIONS = {
    "datastore-read": ("GET", "datastore-item"),
    "remote-list": ("GET", "remote-collection"),
    "remote-read": ("GET", "remote-item"),
    "remote-create": ("POST", "remote-collection"),
    "remote-update": ("PUT", "remote-item"),
    "sync-list": ("GET", "sync-collection"),
    "sync-read": ("GET", "sync-item"),
    "sync-create": ("POST", "sync-collection"),
    "sync-update": ("PUT", "sync-item"),
    "snapshot-verify": ("POST", "snapshot-verify"),
    "sync-run": ("POST", "sync-run"),
    "task-status": ("GET", "task-status"),
}


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


def plan_copy_manager_identity(
    *, users: Any, tokens: Any, secret_record: Any, authenticated: bool = False
) -> dict[str, str]:
    """Create or reuse only the stable destination-scoped copy API identity."""
    user = _one_by_id(users, "userid", COPY_MANAGER_USER_ID, "copy-manager user")
    token = _one_by_id(tokens, "tokenid", COPY_MANAGER_TOKEN_ID, "copy-manager token")
    _require(secret_record is None or isinstance(secret_record, dict),
             "OpenBao PBS copy-manager record has an unsupported schema.")
    saved = secret_record or {}
    if not user and not token and not saved:
        return {"action": "create", "user": COPY_MANAGER_USER_ID, "token_id": COPY_MANAGER_TOKEN_ID}
    _require(user is not None and token is not None,
             "PBS copy-manager user and token are incomplete; no identity mutation is allowed.")
    _require(bool(saved), "PBS copy-manager token exists without its OpenBao copy.")
    _require(saved.get("user") == COPY_MANAGER_USER_ID
             and saved.get("token_id") == COPY_MANAGER_TOKEN_ID,
             "OpenBao PBS copy-manager identity conflicts with its stable ID.")
    _require(saved.get("datastore") == DATASTORE,
             "OpenBao PBS copy-manager record names an unexpected datastore.")
    _require(isinstance(saved.get("secret"), str) and bool(saved["secret"]),
             "OpenBao PBS copy-manager token secret is missing or invalid.")
    _require(isinstance(saved.get("fingerprint"), str) and bool(saved["fingerprint"]),
             "OpenBao PBS copy-manager certificate fingerprint is missing or invalid.")
    _require(user.get("enable", 0) in (1, True, "1") and user.get("expire", 0) in (0, "0", None),
             "PBS copy-manager user is disabled or has an expiry.")
    _require(token.get("enable", 0) in (1, True, "1") and token.get("expire", 0) in (0, "0", None),
             "PBS copy-manager token is disabled or has an expiry.")
    _require(authenticated, "The existing PBS copy-manager token was not authenticated successfully.")
    return {"action": "reuse", "user": COPY_MANAGER_USER_ID, "token_id": COPY_MANAGER_TOKEN_ID}


def _copy_manager_permission_map(value: Any, identity: str) -> dict[str, dict[str, bool]]:
    permissions = _effective_map(value)
    normalized: dict[str, dict[str, bool]] = {}
    for path, privileges in permissions.items():
        _require(all(isinstance(key, str) and type(propagate) is bool
                     for key, propagate in privileges.items()),
                 f"PBS {identity} copy-manager permission value has an unsupported schema.")
        normalized[path] = privileges
    return normalized


def validate_copy_manager_permissions(
    *, user_permissions: Any, token_permissions: Any, acl_rows: Any, remote_id: str
) -> dict[str, Any]:
    """Require only scoped remote config/read and destination backup permissions."""
    remote = _safe_api_identifier(remote_id, "remote name")
    expected = {
        "/remote": {"Remote.Audit": False, "Remote.Modify": False, "Remote.Read": False},
        f"/remote/{remote}": {"Remote.Audit": False, "Remote.Modify": False, "Remote.Read": False},
        f"/remote/{remote}/{DATASTORE}": {"Remote.Audit": False, "Remote.Read": False},
        DATASTORE_PATH: {
            "Datastore.Audit": False,
            "Datastore.Backup": False,
            "Datastore.Read": False,
            "Datastore.Verify": False,
        },
    }
    for label, value in (("user", user_permissions), ("token", token_permissions)):
        actual = _copy_manager_permission_map(value, f"copy-manager {label}")
        _require(actual == expected,
                 f"PBS copy-manager {label} has missing, extra, or differently-scoped privileges.")
    acl_rows = _validated_acl_rows(acl_rows)
    relevant = [row for row in acl_rows if row["ugid"] in {COPY_MANAGER_USER_ID, COPY_MANAGER_TOKEN_ID}]
    acl_entries = [
        ("/remote", *COPY_MANAGER_ACL_ROLE_PATHS["remote-root"]),
        (f"/remote/{remote}", *COPY_MANAGER_ACL_ROLE_PATHS["remote-item"]),
        (f"/remote/{remote}/{DATASTORE}", *COPY_MANAGER_ACL_ROLE_PATHS["remote-datastore"]),
        *((DATASTORE_PATH, *grant) for grant in COPY_MANAGER_ACL_ROLE_PATHS["destination-datastore"]),
    ]
    expected_rows = {
        (auth_id, path, role, propagate)
        for auth_id in (COPY_MANAGER_USER_ID, COPY_MANAGER_TOKEN_ID)
        for path, role, propagate in acl_entries
    }
    actual_rows: set[tuple[str, str, str, int]] = set()
    for row in relevant:
        propagate = row["propagate"]
        _require(propagate in (0, "0", False),
                 "PBS copy-manager ACL grants propagation beyond its exact object path.")
        actual_rows.add((row["ugid"], row["path"], row["roleid"], 0))
    _require(len(relevant) == len(expected_rows) and actual_rows == expected_rows,
             "PBS copy-manager ACLs contain an extra, broad, or missing permission grant.")
    return {"valid": True, "acl_count": len(expected_rows), "remote_id": remote}


def plan_copy_manager_acl(
    *, user_permissions: Any, token_permissions: Any, acl_rows: Any, remote_id: str
) -> dict[str, Any]:
    """Return only missing exact ACL rows; refuse all broader or unknown existing grants."""
    remote = _safe_api_identifier(remote_id, "remote name")
    expected = {
        "/remote": {"Remote.Audit": False, "Remote.Modify": False, "Remote.Read": False},
        f"/remote/{remote}": {"Remote.Audit": False, "Remote.Modify": False, "Remote.Read": False},
        f"/remote/{remote}/{DATASTORE}": {"Remote.Audit": False, "Remote.Read": False},
        DATASTORE_PATH: {
            "Datastore.Audit": False,
            "Datastore.Backup": False,
            "Datastore.Read": False,
            "Datastore.Verify": False,
        },
    }
    for label, value in (("user", user_permissions), ("token", token_permissions)):
        actual = _copy_manager_permission_map(value, f"copy-manager {label}")
        _require(set(actual).issubset(expected),
                 f"PBS copy-manager {label} already has permissions outside the managed paths.")
        for path, privileges in actual.items():
            _require(set(privileges).issubset(expected[path])
                     and all(privileges[name] is False for name in privileges),
                     f"PBS copy-manager {label} already has broad, propagated, or unknown privileges.")
    acl_rows = _validated_acl_rows(acl_rows)
    relevant = [row for row in acl_rows if row["ugid"] in {COPY_MANAGER_USER_ID, COPY_MANAGER_TOKEN_ID}]
    acl_entries = [
        ("/remote", *COPY_MANAGER_ACL_ROLE_PATHS["remote-root"]),
        (f"/remote/{remote}", *COPY_MANAGER_ACL_ROLE_PATHS["remote-item"]),
        (f"/remote/{remote}/{DATASTORE}", *COPY_MANAGER_ACL_ROLE_PATHS["remote-datastore"]),
        *((DATASTORE_PATH, *grant) for grant in COPY_MANAGER_ACL_ROLE_PATHS["destination-datastore"]),
    ]
    expected_rows = {
        (auth_id, path, role, propagate)
        for auth_id in (COPY_MANAGER_USER_ID, COPY_MANAGER_TOKEN_ID)
        for path, role, propagate in acl_entries
    }
    actual_rows: set[tuple[str, str, str, int]] = set()
    for row in relevant:
        _require(row["propagate"] in (0, "0", False),
                 "PBS copy-manager ACL already grants propagation beyond an exact object path.")
        actual_rows.add((row["ugid"], row["path"], row["roleid"], 0))
    _require(len(actual_rows) == len(relevant) and actual_rows.issubset(expected_rows),
             "PBS copy-manager ACLs already contain a duplicate, broad, or unknown grant.")
    missing = sorted(expected_rows - actual_rows)
    return {
        "remote_id": remote,
        "missing": [{"auth_id": auth_id, "path": path, "role": role}
                    for auth_id, path, role, _ in missing],
    }


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


def _safe_api_identifier(value: Any, label: str) -> str:
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value),
             f"PBS {label} is not a valid configured identifier.")
    return value


def _copy_api_request_shape(payload: Any) -> tuple[str, str, dict[str, str] | None]:
    """Build one closed PBS copy-management request; arbitrary paths/methods are impossible."""
    _require(isinstance(payload, dict), "PBS copy API input has an unsupported schema.")
    operation = payload.get("operation")
    _require(operation in COPY_API_OPERATIONS, "PBS copy API operation is not allowed.")
    method, path_kind = COPY_API_OPERATIONS[operation]
    allowed_payload_keys = {"operation", "body"}
    if path_kind == "snapshot-verify":
        allowed_payload_keys = {"operation", "backup_type", "backup_id", "backup_time"}
        _require(set(payload).issubset(allowed_payload_keys),
                 "PBS snapshot verification contains unknown selectors.")
        _require(payload.get("backup_type") == "vm", "PBS verification must target an o11y VM snapshot.")
        backup_id = payload.get("backup_id")
        backup_time = payload.get("backup_time")
        _require(isinstance(backup_id, str) and re.fullmatch(r"[1-9][0-9]{0,8}", backup_id) is not None,
                 "PBS verification backup ID is malformed.")
        _require(type(backup_time) is int and backup_time > 0, "PBS verification backup time is malformed.")
        path = f"/api2/json/admin/datastore/{DATASTORE}/verify"
        body = {"store": DATASTORE, "backup-type": "vm", "backup-id": backup_id,
                "backup-time": str(backup_time), "ignore-verified": "0"}
        return method, path, body
    if path_kind == "sync-run":
        allowed_payload_keys = {"operation", "sync_job_id"}
        _require(set(payload).issubset(allowed_payload_keys),
                 "PBS sync run contains unknown selectors.")
        identifier = _safe_api_identifier(payload.get("sync_job_id"), "sync job id")
        return method, f"/api2/json/admin/sync/{identifier}/run", None
    if path_kind == "task-status":
        allowed_payload_keys = {"operation", "node", "upid"}
        _require(set(payload) == allowed_payload_keys,
                 "PBS task status contains unknown selectors.")
        node = payload.get("node")
        upid = payload.get("upid")
        _require(isinstance(node, str)
                 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", node) is not None,
                 "PBS task status node is malformed.")
        _require(isinstance(upid, str) and re.fullmatch(
            r"UPID:[A-Za-z0-9_.-]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:"
            r"[A-Za-z0-9_-]+:[A-Za-z0-9_.-]*:[A-Za-z0-9_.@-]*:?", upid
        ) is not None, "PBS task status UPID is malformed.")
        return method, f"/api2/json/nodes/{node}/tasks/{quote(upid, safe='')}/status", None
    if path_kind == "datastore-item":
        allowed_payload_keys = {"operation", "store"}
        _require(set(payload) == allowed_payload_keys and payload.get("store") == DATASTORE,
                 "PBS datastore read is not the fixed o11y datastore.")
        return method, f"/api2/json/config/datastore/{DATASTORE}", None
    if path_kind.endswith("item"):
        key = "remote_id" if path_kind == "remote-item" else "sync_job_id"
        identifier = _safe_api_identifier(payload.get(key), key.replace("_", " "))
        allowed_payload_keys.add(key)
        if operation == "remote-update":
            allowed_payload_keys.update({"source_host", "source_fingerprint"})
        if operation == "sync-update":
            remote_id = _safe_api_identifier(payload.get("remote_id"), "remote name")
            allowed_payload_keys.add("remote_id")
        path = f"/api2/json/config/{'remote' if path_kind == 'remote-item' else 'sync'}/{identifier}"
    else:
        path = f"/api2/json/config/{'remote' if path_kind == 'remote-collection' else 'sync'}"
        if operation in {"remote-create", "remote-update"}:
            identifier = _safe_api_identifier(payload.get("remote_id"), "remote name")
            allowed_payload_keys.update({"remote_id", "source_host", "source_fingerprint"})
        elif operation == "sync-create":
            identifier = _safe_api_identifier(payload.get("sync_job_id"), "sync job id")
            allowed_payload_keys.update({"sync_job_id", "remote_id"})
            _safe_api_identifier(payload.get("remote_id"), "remote name")
        elif operation == "sync-update":
            remote_id = _safe_api_identifier(payload.get("remote_id"), "remote name")
            allowed_payload_keys.add("remote_id")
        else:
            identifier = ""
    _require(set(payload).issubset(allowed_payload_keys),
             "PBS copy API request contains unknown selectors or authority fields.")

    body: dict[str, str] | None = None
    if method in {"POST", "PUT"}:
        supplied = payload.get("body")
        _require(isinstance(supplied, dict), "PBS copy API write body is not an object.")
        _require(all(isinstance(key, str) and isinstance(value, (str, int, bool))
                     for key, value in supplied.items()),
                 "PBS copy API write body contains unsupported values.")
        body = {key: str(int(value) if isinstance(value, bool) else value)
                for key, value in supplied.items()}
        allowed = ({"host", "auth-id", "password", "fingerprint", "port", "comment"}
                   if operation == "remote-create" else
                   {"host", "auth-id", "password", "fingerprint", "port", "comment"}
                   if operation == "remote-update" else
                   {"id", "store", "remote-store", "remote", "owner", "remove-vanished",
                    "verified-only", "sync-direction", "schedule", "comment"})
        _require(set(body).issubset(allowed), "PBS copy API write body has unknown or destructive fields.")
        if operation == "remote-create":
            _require({"host", "auth-id", "password", "fingerprint", "port"}.issubset(body),
                     "PBS copy remote create is missing required source fields.")
            body["name"] = identifier
        elif operation == "remote-update":
            _require({"host", "auth-id", "password", "fingerprint", "port"}.issubset(body),
                     "PBS copy remote update is missing required source fields.")
        if operation in {"remote-create", "remote-update"}:
            source_host = payload.get("source_host")
            source_fingerprint = payload.get("source_fingerprint")
            _require(isinstance(source_host, str)
                     and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", source_host) is not None,
                     "PBS copy API source host is not the declared primary PBS endpoint.")
            _require(isinstance(source_fingerprint, str),
                     "PBS copy API source certificate fingerprint is missing.")
            source_fingerprint = normalize_fingerprint(source_fingerprint)
            _require(body.get("auth-id", COPY_READER_TOKEN_ID) == COPY_READER_TOKEN_ID,
                     "PBS copy remote must use the managed read-only source identity.")
            if "host" in body:
                _require(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", body["host"])
                         is not None and ".." not in body["host"]
                         and body["host"].lower() == source_host.lower(),
                         "PBS copy remote endpoint differs from the declared primary PBS host.")
            if "fingerprint" in body:
                body["fingerprint"] = normalize_fingerprint(body["fingerprint"])
                _require(body["fingerprint"] == source_fingerprint,
                         "PBS copy remote fingerprint differs from the primary guest certificate.")
            if "port" in body:
                _require(body["port"] == "8007", "PBS copy remote must use the PBS API port.")
        elif operation == "sync-create":
            required = {"id", "store", "remote-store", "remote", "owner", "remove-vanished", "verified-only"}
            _require(required.issubset(body), "PBS copy sync job is missing a required safety field.")
            _require(body["id"] == identifier, "PBS sync job ID differs from the protected selector.")
            _safe_api_identifier(body["store"], "destination datastore")
            _require(body["remote"] == _safe_api_identifier(payload.get("remote_id"), "remote name"),
                     "PBS sync job remote differs from the protected source selector.")
            _require(body["remote-store"] == DATASTORE,
                     "PBS sync job source datastore is not the managed o11y datastore.")
            _require(body["owner"] == COPY_MANAGER_TOKEN_ID,
                     "PBS sync job owner must be the dedicated copy-manager token.")
            _require(body["remove-vanished"] in {"0", "false"},
                     "PBS copy sync job may not remove vanished backups.")
            _require(body["verified-only"] in {"1", "true"},
                     "PBS copy sync job must require verified source snapshots.")
            _require(body["sync-direction"] == "pull",
                     "PBS copy sync direction must be destination-initiated pull.")
        else:
            _require(operation == "sync-update", "PBS copy API write operation is unsupported.")
            _require("remove-vanished" not in body or body["remove-vanished"] in {"0", "false"},
                     "PBS copy sync job may not remove vanished backups.")
            _require("verified-only" not in body or body["verified-only"] in {"1", "true"},
                     "PBS copy sync job must require verified source snapshots.")
        if operation in {"sync-create", "sync-update"}:
            _require(not body.get("schedule"), "PBS protected copy sync job must be explicitly run.")
        if operation == "sync-update":
            if "remote" in body:
                _require(body["remote"] == remote_id,
                         "PBS sync update may not redirect the protected source remote.")
            if "remote-store" in body:
                _require(body["remote-store"] == DATASTORE,
                         "PBS sync update may not change the managed source datastore.")
            if "owner" in body:
                _require(body["owner"] == COPY_MANAGER_TOKEN_ID,
                         "PBS sync update may not change the copy-manager owner.")
    else:
        _require("body" not in payload, "PBS copy API read operation may not carry a body.")
    return method, path, body


def copy_api_request(*, url: str, fingerprint: str, auth_id: str, secret: str,
                     expected_host: str, payload: Any) -> dict[str, Any]:
    """Issue one fixed PBS JSON API operation after same-socket TLS pin validation."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or 8007
    except ValueError as exc:
        raise Refusal("PBS copy API endpoint has an unsupported URL schema.") from exc
    _require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
             and parsed.path in ("", "/") and not parsed.query and not parsed.fragment,
             "PBS copy API endpoint must be an origin-only HTTPS URL.")
    _require(isinstance(expected_host, str)
             and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", expected_host) is not None
             and parsed.hostname.lower() == expected_host.lower(),
             "PBS copy API endpoint does not match the declared destination host.")
    _require(isinstance(auth_id, str) and re.fullmatch(r"[A-Za-z0-9._@!-]{1,128}", auth_id),
             "PBS copy API identity is invalid.")
    _require(isinstance(secret, str) and bool(secret), "PBS copy API credential is missing.")
    if isinstance(payload, dict) and payload.get("operation") == "snapshot-verify":
        _require(auth_id == COPY_READER_TOKEN_ID,
                 "PBS snapshot verification must use the independent source Reader identity.")
    if isinstance(payload, dict) and payload.get("operation") == "sync-run":
        _require(auth_id == COPY_MANAGER_TOKEN_ID,
                 "PBS protected-copy sync must use the dedicated destination manager identity.")
    if isinstance(payload, dict) and payload.get("operation") == "task-status":
        _require(auth_id in {COPY_READER_TOKEN_ID, COPY_MANAGER_TOKEN_ID},
                 "PBS task status requires a dedicated copy workflow identity.")
    if isinstance(payload, dict) and payload.get("operation") == "datastore-read":
        _require(auth_id == COPY_MANAGER_TOKEN_ID,
                 "PBS protected-copy datastore read requires the dedicated destination manager identity.")
    method, path, body_fields = _copy_api_request_shape(payload)
    connection = _PinnedHTTPSConnection(parsed.hostname, port, fingerprint)
    try:
        connection.connect()
        headers = {"Authorization": f"PBSAPIToken {auth_id}:{secret}", "Accept": "application/json"}
        body = None
        if body_fields is not None:
            body = urlencode(body_fields).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_API_RESPONSE_BYTES + 1)
        _require(len(raw) <= MAX_API_RESPONSE_BYTES, "PBS copy API response exceeds the reviewed size limit.")
        _require(not (300 <= response.status < 400), "PBS copy API refused a redirect.")
        _require(response.status == 200, "PBS copy API returned an unsupported status.")
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise Refusal("PBS copy API response is not valid JSON.") from exc
        _require(isinstance(decoded, dict) and set(decoded) == {"data"},
                 "PBS copy API response has an unsupported envelope.")
        if isinstance(payload, dict) and payload.get("operation") == "task-status":
            data = decoded["data"]
            _require(isinstance(data, dict)
                     and data.get("status") in {"running", "stopped"},
                     "PBS task status returned an unsupported schema.")
            exitstatus = data.get("exitstatus")
            _require(exitstatus is None or isinstance(exitstatus, str),
                     "PBS task status returned an unsupported schema.")
            if data["status"] == "stopped":
                return {"status": "stopped", "exitstatus": "OK" if exitstatus == "OK" else "failed"}
            return {"status": "running", "exitstatus": None}
        return decoded["data"]
    finally:
        connection.close()


def read_snapshot_manifest(*, url: str, expected_host: str, fingerprint: str,
                           auth_id: str, secret: str, backup_id: str,
                           backup_time: int, include_files: bool = False) -> dict[str, Any]:
    """Read exactly one manifest over a same-connection pinned PBS reader session."""
    _require(auth_id in {COPY_READER_TOKEN_ID, COPY_MANAGER_TOKEN_ID},
             "PBS manifest read requires a dedicated source Reader or destination manager identity.")
    _require(isinstance(backup_id, str) and re.fullmatch(r"[1-9][0-9]{0,8}", backup_id) is not None
             and type(backup_time) is int and backup_time > 0,
             "PBS manifest snapshot selector is malformed.")
    parsed = urlsplit(url)
    try:
        port = parsed.port or 8007
    except ValueError:
        raise Refusal("PBS manifest endpoint has an unsupported URL schema.") from None
    _require(isinstance(expected_host, str)
             and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", expected_host) is not None,
             "PBS manifest expected host is malformed.")
    _require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
             and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
             and parsed.hostname.lower() == expected_host.lower(),
             "PBS manifest endpoint does not match its fixed HTTPS host.")
    query = urlencode({
        "store": DATASTORE,
        "backup-type": "vm",
        "backup-id": backup_id,
        "backup-time": str(backup_time),
        "file-name": "index.json.blob",
    })
    path = f"/api2/json/admin/datastore/{DATASTORE}/download-decoded?{query}"
    connection = _PinnedHTTPSConnection(parsed.hostname, port, fingerprint)
    try:
        connection.connect()
        connection.request(
            "GET",
            path,
            headers={"Authorization": f"PBSAPIToken {auth_id}:{secret}",
                     "Accept": "application/octet-stream"},
        )
        response = connection.getresponse()
        manifest_bytes = response.read(MAX_MANIFEST_BLOB_BYTES + 1)
        _require(not (300 <= response.status < 400), "PBS manifest endpoint refused a redirect.")
        _require(response.status == 200 and len(manifest_bytes) <= MAX_MANIFEST_BLOB_BYTES,
                 "PBS manifest download returned an unsupported status or size.")
        manifest = parse_manifest(manifest_bytes)
        _require(manifest["backup_type"] == "vm" and manifest["backup_id"] == backup_id
                 and manifest["backup_time"] == backup_time,
                 "PBS manifest does not match the requested snapshot identity.")
        result = {
            "snapshot": {"backup_type": "vm", "backup_id": backup_id, "backup_time": backup_time},
            "artifact_fingerprint": artifact_fingerprint(manifest),
        }
        if include_files:
            result["manifest_filenames"] = [item["filename"] for item in manifest["files"]]
        return result
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
            "copy-manager-identity",
            "copy-manager-acl-plan",
            "copy-manager-permissions",
            "version",
            "certificate",
            "probe",
            "copy-api-request",
            "snapshot-manifest",
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
        elif args.action == "copy-manager-identity":
            result = plan_copy_manager_identity(
                users=payload.get("users"),
                tokens=payload.get("tokens"),
                secret_record=payload.get("secret_record"),
                authenticated=payload.get("authenticated") is True,
            )
        elif args.action == "copy-manager-acl-plan":
            result = plan_copy_manager_acl(
                user_permissions=payload.get("user_permissions"),
                token_permissions=payload.get("token_permissions"),
                acl_rows=payload.get("acl_rows"),
                remote_id=payload.get("remote_id"),
            )
        elif args.action == "copy-manager-permissions":
            result = validate_copy_manager_permissions(
                user_permissions=payload.get("user_permissions"),
                token_permissions=payload.get("token_permissions"),
                acl_rows=payload.get("acl_rows"),
                remote_id=payload.get("remote_id"),
            )
        elif args.action == "version":
            result = {"supported": True, "package": parse_pbs_version(payload.get("package"))}
        elif args.action == "certificate":
            result = {"fingerprint": parse_openssl_fingerprint(payload.get("openssl_fingerprint"))}
        else:
            if args.action == "copy-api-request":
                result = {"data": copy_api_request(
                    url=payload.get("url", ""),
                    fingerprint=payload.get("fingerprint", ""),
                    auth_id=payload.get("auth_id", ""),
                    secret=payload.get("secret", ""),
                    expected_host=payload.get("expected_host", ""),
                    payload=payload.get("request"),
                )}
            elif args.action == "snapshot-manifest":
                result = read_snapshot_manifest(
                    url=payload.get("url", ""),
                    expected_host=payload.get("expected_host", ""),
                    fingerprint=payload.get("fingerprint", ""),
                    auth_id=payload.get("auth_id", ""),
                    secret=payload.get("secret", ""),
                    backup_id=payload.get("backup_id", ""),
                    backup_time=payload.get("backup_time"),
                    include_files=payload.get("include_files", False) is True,
                )
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
