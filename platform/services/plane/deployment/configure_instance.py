"""Converge a Plane instance's first-run configuration through its own session routes.

Run by deploy-plane.yml INSIDE the plane-api container (`<engine> exec -i plane-api python -c`),
with a JSON document on stdin, so it talks to the API on localhost:8000 and no credential is
ever on a command line. Prints one JSON line naming what it changed; never prints a
credential. Idempotent: a second run changes nothing.

Why it exists: until the first admin signs up, ANYONE who can reach /god-mode/ claims the
instance. deploy.sh therefore holds the proxy, the only way in, until the instance is set up,
and this script completes setup against the API directly. Plane offers no API key for
instance settings; these are the routes its own admin app uses (proven on a Commercial v3.3.3
instance, design.md "Runtime spike results", 0.3 and 0.5).

  1. Instance setup: admin sign-up (only while the instance is not set up).
  2. Admin sign-in (every run; the session drives 3 and 4).
  3. Instance settings: ENABLE_SIGNUP=0 (invitation-only) and the OIDC settings the playbook
     passes; PATCHed only where they differ, reporting setting NAMES only.
  4. The workspace, created only when its slug is absent.
"""

# Runs after plane_session.py (cfg, fail, call, form_post, json), joined by the playbook, so its
# names are defined only in the joined source.
# ruff: noqa: F821

done = []
_, _, inst = call("GET", "/api/instances/")
if not inst["instance"]["is_setup_done"]:
    form_post("/api/instances/admins/sign-up/", {
        "email": cfg["email"], "password": cfg["password"], "first_name": cfg["first_name"],
        "last_name": cfg["last_name"], "company_name": cfg["company"], "is_telemetry_enabled": "False"})
    done.append("instance setup completed")
else:
    form_post("/api/instances/admins/sign-in/", {"email": cfg["email"], "password": cfg["password"]})

# Instance settings: invitation-only sign-up always, plus whatever the playbook passes (OIDC).
# The read-back returns encrypted settings decrypted, so a secret is compared like any value;
# only keys that differ are PATCHed, and only their NAMES are reported.
_, _, configs = call("GET", "/api/instances/configurations/")
current = {c["key"]: c["value"] for c in configs}
desired = dict(cfg.get("settings", {}), ENABLE_SIGNUP="0")
unknown = sorted(k for k in desired if k not in current)
if unknown:
    fail(f"this Plane release has no instance setting named {', '.join(unknown)}")
changed = {k: v for k, v in desired.items() if current.get(k) != v}
if changed:
    status, _, _ = call("PATCH", "/api/instances/configurations/", changed)
    if status != 200:
        fail(f"updating instance settings failed ({status})")
    done.append("instance settings updated: " + ", ".join(sorted(changed)))

_, _, spaces = call("GET", "/api/instances/workspaces/")
rows = spaces.get("results", []) if isinstance(spaces, dict) else (spaces or [])
if not any(w.get("slug") == cfg["workspace_slug"] for w in rows):
    status, _, _ = call("POST", "/api/instances/workspaces/", {
        "name": cfg["workspace_name"], "slug": cfg["workspace_slug"], "organization_size": "2-10"})
    if status != 201:
        fail(f"workspace {cfg['workspace_slug']} could not be created ({status})")
    done.append(f"workspace {cfg['workspace_slug']} created")

print(json.dumps({"changed": bool(done), "actions": done}))
