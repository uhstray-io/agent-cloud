"""Read planned Authentik retirements through the container's local API.

Usernames arrive as prefixed JSON on stdin. The prefix prevents older Ansible
controllers from converting a JSON-looking stdin string back into a Python list.
The bootstrap token stays in the container env;
stdout contains counts only, so a Semaphore task does not publish private identities.
"""

import http.client
import json
import os
import sys
import urllib.parse


class AuditError(Exception):
    pass


def get_users(username, token):
    connection = http.client.HTTPConnection("localhost", 9000, timeout=10)
    try:
        query = urllib.parse.quote(username, safe="")
        connection.request(
            "GET", f"/api/v3/core/users/?username={query}&page_size=50",
            headers={"Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise AuditError(f"authentik_api_http_{response.status}")
        payload = json.load(response)
    finally:
        connection.close()
    users = payload.get("results") if isinstance(payload, dict) else None
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    if (not isinstance(users, list) or len(users) == 50
            or (isinstance(pagination, dict) and pagination.get("count") is not None
                and pagination["count"] != len(users))
            or any(not isinstance(user, dict) for user in users)):
        raise AuditError("authentik_user_result_incomplete")
    return users


def audit(usernames, lookup):
    if (not isinstance(usernames, list) or any(not isinstance(name, str) or not name.strip()
                                               for name in usernames)
            or len(set(usernames)) != len(usernames)):
        raise AuditError("invalid_retirement_declaration")
    present = 0
    for name in usernames:
        matches = [user for user in lookup(name) if user.get("username") == name]
        if len(matches) > 1:
            raise AuditError("duplicate_exact_username")
        present += len(matches)
    return {"retirements_declared": len(usernames), "retirements_present": present,
            "retirements_absent": len(usernames) - present}


def main():
    try:
        token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN")
        if not token:
            raise AuditError("bootstrap_token_unavailable")
        raw = sys.stdin.read().strip()
        if raw == "audit-probe":
            print("authentik_retirement_audit_ready")
            return 0
        if not raw.startswith("audit:"):
            raise AuditError("invalid_audit_input")
        usernames = json.loads(raw[len("audit:"):])
        print(json.dumps(audit(usernames, lambda name: get_users(name, token)), sort_keys=True))
        return 0
    except AuditError as exc:
        print("authentik_retirement_audit_failed:" + str(exc))
        return 2
    except (ValueError, OSError, KeyError, TypeError, http.client.HTTPException):
        print("authentik_retirement_audit_failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
