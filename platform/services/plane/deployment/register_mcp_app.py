"""Register the Plane OAuth application the Plane MCP server signs in with, and return its client credentials.

Run by the playbook INSIDE the plane-api container, after plane_session.py, as a credential task
(no_log), with one JSON document on stdin:

  {base, host, origin,                plane_session.py
   email, password,                   the automation account (the workspace's creator, admin)
   workspace_slug,
   app_slug, app_name,                the application's slug (<= 48 characters) and display name
   company_name, short_description,
   redirect_uri,                      the MCP server's OAuth callback URIs, space-separated (Plane stores
                                      redirect_uris that way; order is not significant)
   have_secret: bool}                 true when the caller already holds the client secret: then
                                      nothing is regenerated and no secret is printed

It signs in as the automation admin and lists the workspace's applications. The application is
found by its slug among the rows Plane lists for this workspace:

  missing                   create it: confidential, authorization-code, resource_permissions
                            ["read", "write"] (what the MCP server needs), redirect_uri as given.
                            Plane answers 201 with the one plaintext client_secret it ever returns.
  owned, differing fields   PATCH only the fields that differ: redirect_uris, resource_permissions
                            (compared as sets). An owned application is never deleted.
  owned, secret needed      unless have_secret, regenerate the secret (PATCH .../{id}/regenerate-secret/,
                            Plane answers 200 with the new plaintext client_secret).
  not owned                 refused before any write: the slug belongs to another owner.

Plane's app list reads from a replica: an immediate re-run can miss an application created a
moment ago. The create then answers 400 on the slug, and this script refuses with a named error
(not a duplicate), so the caller re-runs once the replica catches up.

Prints ONE JSON line {"client_id", "client_secret" (only when created or regenerated), "created",
"regenerated", "patched": [field names]}. client_secret is the credential output, so the playbook
shows only the non-secret fields. A refusal prints {"error"} and exits 1. A refused write prints the
HTTP status only, never a response body, because a body can carry a secret.
"""

# Runs after plane_session.py (cfg, fail, call, form_post, json), joined by the playbook, so its
# names are defined only in the joined source.
# ruff: noqa: F821

ws = cfg["workspace_slug"]
app_slug = cfg["app_slug"]
redirect = cfg["redirect_uri"]
wanted_permissions = ["read", "write"]
have_secret = str(cfg.get("have_secret", False)).lower() == "true"
base_path = f"/api/workspaces/{ws}/applications/"

form_post("/auth/sign-in/", {"email": cfg["email"], "password": cfg["password"]})

status, _, listing = call("GET", base_path)
if status != 200 or not isinstance(listing, list):
    fail(f"listing applications of {ws} failed ({status})")

match = next((a for a in listing if a.get("slug") == app_slug), None)
if match is not None and not match.get("is_owned"):
    fail(f"application slug {app_slug} belongs to another owner")

if match is None:
    status, _, created = call("POST", base_path, {
        "name": cfg["app_name"],
        "slug": app_slug,
        "short_description": cfg["short_description"],
        "company_name": cfg["company_name"],
        "redirect_uris": redirect,
        "resource_permissions": wanted_permissions,
    })
    if status == 400 and isinstance(created, dict) and "slug" in created:
        fail(f"application slug {app_slug} exists but was not listed; re-run")
    if status != 201 or not isinstance(created, dict) or not created.get("client_secret"):
        fail(f"creating application {app_slug} refused ({status})")  # no body: it carries the secret
    print(json.dumps({"client_id": created["client_id"], "client_secret": created["client_secret"],
                      "created": True, "regenerated": False, "patched": []}))
else:
    patch = {}
    if set((match.get("redirect_uris") or "").split()) != set(redirect.split()):
        patch["redirect_uris"] = redirect
    if set(match.get("resource_permissions") or []) != set(wanted_permissions):
        patch["resource_permissions"] = wanted_permissions
    if patch:
        status, _, _ = call("PATCH", f"{base_path}{app_slug}/", patch)
        if status != 200:
            fail(f"updating application {app_slug} refused ({status})")
    result = {"client_id": match["client_id"], "created": False, "regenerated": False,
              "patched": sorted(patch)}
    if not have_secret:
        status, _, rotated = call("PATCH", f"{base_path}{match['id']}/regenerate-secret/")
        if status != 200 or not isinstance(rotated, dict) or not rotated.get("client_secret"):
            fail(f"regenerating the secret of {app_slug} refused ({status})")
        result.update(client_secret=rotated["client_secret"], regenerated=True)
    print(json.dumps(result))
