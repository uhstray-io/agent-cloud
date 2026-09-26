"""Compare who can launch Semaphore tasks with who is declared to.

Launch rights are runner rights: a task's environment becomes its extra vars unfiltered, and a
templated extra var runs code on the runner (plan 01, "Launch permission is runner access").
So every identity that can launch is held to a declaration. Used by
manage-semaphore-access.yml; tested directly by platform/tests/test_semaphore_access.py.
"""


def semaphore_access_drift(members, declared_members, users, declared_admins, integrations):
    """What differs between the live project and the declarations. Names and roles only.

    members          GET /api/project/{id}/users   [{id, username, role}]
    declared_members {username: role}              the whole project team, nothing implied
    users            GET /api/users                [{id, username, admin}]
    declared_admins  [username]                    semaphore_admin_users (username match only,
                                                   as promote-semaphore-admins.yml)
    integrations     GET /api/project/{id}/integrations
    """
    if not isinstance(declared_members, dict) or not declared_members:
        # An empty declaration would remove every member; a missing one is not a decision.
        return {"problems": ["semaphore_project_members is not declared (username: role)"],
                "add": [], "set_role": [], "remove": []}
    live = {m["username"]: m for m in members or []}
    by_name = {u["username"]: u for u in users or []}
    add, set_role, remove, problems = [], [], [], []
    for name, role in sorted(declared_members.items()):
        if not isinstance(role, str) or not role:
            problems.append(f"declared member {name} has no role")
        elif name in live:
            if live[name]["role"] != role:
                set_role.append({"user_id": live[name]["id"], "username": name,
                                 "from": live[name]["role"], "to": role})
        elif name in by_name:
            add.append({"user_id": by_name[name]["id"], "username": name, "role": role})
        else:
            problems.append(f"declared member {name} has no Semaphore account")
    for name, member in sorted(live.items()):
        if name not in declared_members:
            remove.append({"user_id": member["id"], "username": name, "role": member["role"]})
    admins = set(declared_admins or [])
    for name in sorted(u["username"] for u in users or [] if u.get("admin")):
        if name not in admins:
            problems.append(f"system admin {name} is not in semaphore_admin_users")
    for integration in integrations or []:
        problems.append(f"integration {integration.get('name', integration.get('id'))} can set extra vars "
                        "from a webhook; none is declared")
    return {"problems": problems, "add": add, "set_role": set_role, "remove": remove}


class FilterModule:
    def filters(self):
        return {"semaphore_access_drift": semaphore_access_drift}
