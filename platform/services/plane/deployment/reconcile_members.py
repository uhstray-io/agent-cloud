"""Reconcile Plane workspace membership with Authentik groups (decision D3).

Run by reconcile-plane-members.yml INSIDE the plane-api container, after plane_session.py,
with one JSON document on stdin:

  {base, host, origin,                  plane_session.py
   email, password,                     the automation admin, who owns the workspace
   workspace_slug,
   desired: {email: role_slug},         "admin" for platform-admins (inventory may say "member"
                                        where the plan lacks the Admin role), "member" for
                                        developers and business users; platform-user is never in it
   apply: bool}

It signs in as the automation admin and compares the workspace's ACTIVE human members and
pending invitations with `desired`:

  invite        a desired email that is neither an active member nor invited
  set role      a member, or a pending invitation, whose role_slug differs from the desired one
  remove        a member, or a pending invitation, whose email is not desired

Never touched: the automation account, any owner (Plane refuses to demote or remove one), and
bots (`membership_type=people`). Plane v3.3.3 removes a member by deactivating the row, so an
inactive row counts as absent and a returning person is invited again.

Without `apply` it changes nothing and reports the plan; with it, it applies the plan. Prints
ONE JSON line (emails and roles, never a credential). Self sign-up is off, so an invitation is
what lets a person sign in through Authentik; removing them is what takes it back.
"""

# Runs after plane_session.py (cfg, fail, call, form_post, json), joined by the playbook, so its
# names are defined only in the joined source.
# ruff: noqa: F821

slug, me = cfg["workspace_slug"], cfg["email"].lower()
desired = {e.lower(): r for e, r in cfg["desired"].items()}


def error_of(body):
    return f": {body['error']}" if isinstance(body, dict) and body.get("error") else ""


def roster():
    """Every human member row, following the cursor-paginated envelope."""
    rows, cursor = [], ""
    while True:
        status, _, page = call("GET", f"/api/workspaces/{slug}/members/?membership_type=people&per_page=100{cursor}")
        if status != 200 or not isinstance(page, dict) or not isinstance(page.get("results"), list):
            fail(f"listing members of {slug} failed ({status}){error_of(page)}")
        rows += page["results"]
        if not page.get("next_page_results"):
            return rows
        cursor = f"&cursor={page['next_cursor']}"


# A normal (not instance-admin) session: workspace routes read the user session cookie.
form_post("/auth/sign-in/", {"email": cfg["email"], "password": cfg["password"]})

members = roster()
status, _, invites = call("GET", f"/api/workspaces/{slug}/invitations/")
if status != 200 or not isinstance(invites, list):
    fail(f"listing invitations of {slug} failed ({status}){error_of(invites)}")

have = {m["member"]["email"].lower(): m for m in members
        if m.get("is_active") and (m.get("member") or {}).get("email")}
owners = {e for e, m in have.items() if m.get("role_slug") == "owner"}
pending = {i["email"].lower(): i for i in invites if i.get("email") and not i.get("accepted")}

plan = {"invite": [], "set_role": [], "remove": []}
for email, role in sorted(desired.items()):
    if email == me or email in owners:
        continue
    if email in have:
        if have[email].get("role_slug") != role:
            plan["set_role"].append({"email": email, "role": role, "member": have[email]["id"]})
    elif email in pending:
        if pending[email].get("role_slug") != role:
            plan["set_role"].append({"email": email, "role": role, "invitation": pending[email]["id"]})
    else:
        plan["invite"].append({"email": email, "role": role})
for email, m in sorted(have.items()):
    if email != me and email not in owners and email not in desired:
        plan["remove"].append({"email": email, "member": m["id"]})
for email, i in sorted(pending.items()):
    if email not in desired and email not in have:
        plan["remove"].append({"email": email, "invitation": i["id"]})

done = []
if cfg.get("apply"):
    if plan["invite"]:
        status, _, body = call("POST", f"/api/workspaces/{slug}/invitations/",
                               {"emails": [{"email": p["email"], "role_slug": p["role"]} for p in plan["invite"]]})
        if status not in (200, 201):
            fail(f"inviting {len(plan['invite'])} people failed ({status}){error_of(body)}")
        done.append(f"invited {len(plan['invite'])}")
    for p in plan["set_role"]:
        path = (f"/api/workspaces/{slug}/members/{p['member']}/" if "member" in p
                else f"/api/workspaces/{slug}/invitations/{p['invitation']}/")
        status, _, body = call("PATCH", path, {"role_slug": p["role"]})
        if status != 200:
            fail(f"setting {p['email']} to {p['role']} failed ({status}){error_of(body)}")
        done.append(f"set {p['email']} to {p['role']}")
    for p in plan["remove"]:
        path = (f"/api/workspaces/{slug}/members/{p['member']}/" if "member" in p
                else f"/api/workspaces/{slug}/invitations/{p['invitation']}/")
        status, _, body = call("DELETE", path)
        if status not in (200, 204):
            fail(f"removing {p['email']} failed ({status}){error_of(body)}")
        done.append(f"removed {p['email']}")

summary = {k: [p["email"] + (f" ({p['role']})" if "role" in p else "") for p in v] for k, v in plan.items()}
print(json.dumps({"changed": bool(done), "applied": bool(cfg.get("apply")), "plan": summary, "actions": done}))
