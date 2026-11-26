"""Get or create the workspace's service API token: the one credential the Plane project sync reads.

Run by deploy-plane.yml INSIDE the plane-api container, after plane_session.py, as a credential task
(no_log), with one JSON document on stdin:

  {base, host, origin,                plane_session.py
   email, password,                   the automation account (the workspace's creator)
   workspace_slug}

It signs in as the automation account and POSTs /api/workspaces/{slug}/service-api-tokens/. Plane's
route is get-or-create (app/views/api/service.py): the first call mints the token and answers 201, a
repeat returns the same token and answers 200. So this never rotates it. The playbook stores the
printed token in OpenBao at secret/services/plane:plane_service_token, and sync-plane-projects.yml
reads it from there.

Revoking the token is unverified. Plane's user-facing token deletes skip service tokens:
ApiTokenEndpoint.delete filters is_service=False (app/views/api/base.py:88-90), and the workspace list
and delete both read through a filter that excludes is_service (app/views/api/workspace.py:44-58,166).
unverified: how an operator removes a service token.

Prints ONE JSON line {"token": "...", "created": bool}. A refusal prints {"error"} and exits 1, with
no body (a success reply carries the token, so a refusal never echoes one).
"""

# Runs after plane_session.py (cfg, fail, call, form_post, json), joined by the playbook, so its
# names are defined only in the joined source.
# ruff: noqa: F821

slug = cfg["workspace_slug"]
form_post("/auth/sign-in/", {"email": cfg["email"], "password": cfg["password"]})
status, _, issued = call("POST", f"/api/workspaces/{slug}/service-api-tokens/")
if status not in (200, 201) or not isinstance(issued, dict) or not issued.get("token"):
    fail(f"service token for {slug} refused ({status})")  # no body: a success reply carries the token
print(json.dumps({"token": issued["token"], "created": status == 201}))
