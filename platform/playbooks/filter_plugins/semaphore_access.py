"""Compare who can launch Semaphore tasks with who is declared to.

Launch rights are runner rights: a task's environment becomes its extra vars unfiltered, and a
templated extra var runs code on the runner (plan 01, "Launch permission is runner access").
So every identity that can launch is held to a declaration. Used by
manage-semaphore-access.yml and promote-semaphore-admins.yml; tested directly by
platform/tests/test_semaphore_access.py.
"""

from collections.abc import Mapping, Sequence


def semaphore_admin_roster(declared_admins, platform_admins=None):
    """The Semaphore system admins the declarations name: usernames, sorted, no repeats.

    Admin is derived, never hand-granted (plan/architecture/04-credentials-access.md): every
    person in `platform_admins`, the one list the Authentik deploy renders into the
    platform-admins group, is a Semaphore admin under the same username. Semaphore's OIDC
    provider names an account after the `preferred_username` claim, which is the Authentik
    username. The service account is skipped: it never signs in to Semaphore.

    declared_admins  [username]   semaphore_admin_users: Semaphore-only admins that are not
                                  platform admins (the local bootstrap account, for one)
    platform_admins  [{username, type?}] or None when the inventory does not declare the list
                                  for this play; then nothing is derived (the committed default
                                  in vars/platform-admins.yml holds local-dev placeholders, and
                                  a placeholder name must never become an admin)
    """
    if isinstance(declared_admins, str):
        raise ValueError("semaphore_admin_users is not a list")
    names = set()
    for name in declared_admins or []:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"semaphore_admin_users entry {name!r} is not a username")
        names.add(name.strip())
    if platform_admins is None:
        return sorted(names)
    if isinstance(platform_admins, str) or not isinstance(platform_admins, Sequence):
        raise ValueError("platform_admins is not a list")
    for position, admin in enumerate(platform_admins, start=1):
        username = admin.get("username") if isinstance(admin, Mapping) else None
        if not isinstance(username, str) or not username.strip():
            raise ValueError(f"platform_admins entry #{position} has no username")
        if admin.get("type", "user") == "service_account":
            continue
        names.add(username.strip())
    return sorted(names)


def semaphore_access_drift(members, declared_members, users, declared_admins, integrations):
    """What differs between the live project and the declarations. Names and roles only.

    `invalid` is a declaration that cannot be applied as written (absent, a name with no
    account, a member with no role): nothing may be written while it is non-empty, or a typo
    would remove working access without granting its replacement (Codex review of #276).
    `problems` is access that needs an operator decision (an undeclared admin, any integration);
    it fails the run after the team is converged, so it never holds back removing launch rights.

    members          GET /api/project/{id}/users   [{id, username, role}]
    declared_members {username: role}              the whole project team, nothing implied
    users            GET /api/users                [{id, username, admin}]
    declared_admins  [username]                    semaphore_admin_roster: platform_admins plus
                                                   semaphore_admin_users (username match only,
                                                   as promote-semaphore-admins.yml)
    integrations     GET /api/project/{id}/integrations
    """
    if not isinstance(declared_members, dict) or not declared_members:
        # An empty declaration would remove every member; a missing one is not a decision.
        return {"invalid": ["semaphore_project_members is not declared (username: role)"],
                "problems": [], "add": [], "set_role": [], "remove": []}
    live = {m["username"]: m for m in members or []}
    by_name = {u["username"]: u for u in users or []}
    add, set_role, remove, invalid, problems = [], [], [], [], []
    for name, role in sorted(declared_members.items()):
        if not isinstance(role, str) or not role:
            invalid.append(f"declared member {name} has no role")
        elif name in live:
            if live[name]["role"] != role:
                set_role.append({"user_id": live[name]["id"], "username": name,
                                 "from": live[name]["role"], "to": role})
        elif name in by_name:
            add.append({"user_id": by_name[name]["id"], "username": name, "role": role})
        else:
            invalid.append(f"declared member {name} has no Semaphore account")
    for name, member in sorted(live.items()):
        if name not in declared_members:
            remove.append({"user_id": member["id"], "username": name, "role": member["role"]})
    admins = set(declared_admins or [])
    for name in sorted(u["username"] for u in users or [] if u.get("admin")):
        if name not in admins:
            problems.append(f"system admin {name} is not declared (platform_admins or semaphore_admin_users)")
    for integration in integrations or []:
        problems.append(f"integration {integration.get('name', integration.get('id'))} can set extra vars "
                        "from a webhook; none is declared")
    return {"invalid": invalid, "problems": problems, "add": add, "set_role": set_role, "remove": remove}


class FilterModule:
    def filters(self):
        return {"semaphore_access_drift": semaphore_access_drift,
                "semaphore_admin_roster": semaphore_admin_roster}
