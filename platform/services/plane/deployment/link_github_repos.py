"""Link every declared Plane project to its GitHub repository through Plane's GitHub integration.

Run by sync-plane-projects.yml INSIDE plane-api, after plane_session.py, as a credential task
(no_log), with one JSON document on stdin:
  {base, host, origin,                 plane_session.py
   email, password,                    the automation account (the workspace's creator, admin)
   workspace_slug,
   silo_base,                          silo through the stack's proxy, e.g. http://proxy:80/silo
   projects: [{identifier, repo}],     declared projects that name a repository (owner/name)
   apply: bool}                        false: read only, report what would be linked

A link is the record Plane's own "Project Issue Sync" form creates (web bundle, v3.3.3): a silo
entity connection of type PROJECT_ISSUE_SYNC on the workspace's GitHub connection, with entity_id
the repository id, entity_slug its full name, entity_data the repository as silo lists it, and
config {states: {issueEventMapping: {ISSUE_OPEN, ISSUE_CLOSED}}, allowBidirectionalSync: true}.
An open issue maps to the project's Backlog and a closed one to Done, by name from schema.yml.
Those are silo's own defaults (backlog and completed groups), written explicitly so they show.

Silo authenticates by forwarding the session cookie to /api/users/me/, so this signs in rather
than using the service token. Create-only: a project already linked to its declared repository is
left alone; a project linked elsewhere, or a repository linked to another project, is reported as a
conflict and refused, never re-pointed. The GitHub organization must already be connected
(Workspace settings > Integrations > GitHub).

Prints one JSON line: {"linked"|"would_link", "present", "missing_projects", "conflicts"} or
{"error": ...}. Never a credential.
"""

# Runs after plane_session.py (cfg, fail, call, form_post, cookies, HOST, ORIGIN, json), joined by
# the playbook, so its names are defined at run time.
# ruff: noqa: F821
import urllib.error
import urllib.request

slug = cfg["workspace_slug"]
SILO = cfg["silo_base"].rstrip("/")


def silo(method, path, data=None):
    """One request to silo with the signed-in session's cookies; returns (status, parsed JSON)."""
    headers = {"Host": HOST, "Origin": ORIGIN, "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(SILO + path, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        resp = e
    raw = resp.read()
    return resp.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else None)


form_post("/auth/sign-in/", {"email": cfg["email"], "password": cfg["password"]})

status, _, workspaces = call("GET", "/api/users/me/workspaces/")
ws = next((w for w in workspaces or [] if w.get("slug") == slug), None) if status == 200 else None
if ws is None:
    fail(f"workspace {slug} not found for the automation account ({status})")
ws_id = ws["id"]

status, _, projects = call("GET", f"/api/workspaces/{slug}/projects/")
if status != 200 or not isinstance(projects, list):
    fail(f"listing projects answered {status}")
project_ids = {p["identifier"]: p["id"] for p in projects}

status, orgs = silo("GET", f"/api/github/auth/organization-status/{ws_id}")
if status != 200 or not orgs:
    fail("the GitHub organization is not connected to the workspace (Integrations > GitHub > Connect)")
conn_id = orgs[0]["id"]

status, repos = silo("GET", f"/api/github/{ws_id}/repos")
if status != 200 or not isinstance(repos, list):
    fail(f"listing the installation's repositories answered {status}")
repo_by_name = {r["full_name"].lower(): r for r in repos}

status, existing = silo("GET", f"/api/entity-connections/{ws_id}/{conn_id}?entityType=GITHUB")
if status != 200 or not isinstance(existing, list):
    fail(f"listing entity connections answered {status}")
syncs = [e for e in existing if e.get("type") == "PROJECT_ISSUE_SYNC"]
by_project = {e.get("project_id"): e for e in syncs}
by_repo = {str(e.get("entity_id")): e for e in syncs}

done, present, missing_projects, conflicts = [], [], [], []
for p in cfg["projects"]:
    ident, name = p["identifier"], p["repo"]
    pid, repo = project_ids.get(ident), repo_by_name.get(name.lower())
    if pid is None:
        missing_projects.append(ident)
        continue
    if repo is None:
        conflicts.append(f"{ident}: {name} is not visible to the GitHub App installation")
        continue
    mine, theirs = by_project.get(pid), by_repo.get(str(repo["id"]))
    if mine and str(mine.get("entity_id")) == str(repo["id"]):
        present.append(ident)
        continue
    if mine:
        conflicts.append(f"{ident}: already linked to {mine.get('entity_slug')}, declared {name}")
        continue
    if theirs:
        conflicts.append(f"{ident}: {name} is already linked to another project")
        continue
    if not cfg["apply"]:
        done.append(ident)
        continue
    status, _, states = call("GET", f"/api/workspaces/{slug}/projects/{pid}/states/")
    if status != 200 or not isinstance(states, list):
        fail(f"{ident}: listing states answered {status}", linked=done)
    named = {s["name"]: {"id": s["id"], "name": s["name"]} for s in states}
    if "Backlog" not in named or "Done" not in named:
        fail(f"{ident}: the project lacks the Backlog or Done state from schema.yml", linked=done)
    status, _ = silo(
        "POST",
        f"/api/entity-connections/{ws_id}/{conn_id}",
        {
            "workspace_id": ws_id,
            "workspace_slug": slug,
            "project_id": pid,
            "workspace_connection_id": conn_id,
            "entity_id": str(repo["id"]),
            "entity_type": "GITHUB",
            "entity_slug": repo["full_name"],
            "entity_data": repo,
            "config": {
                "states": {"issueEventMapping": {"ISSUE_OPEN": named["Backlog"], "ISSUE_CLOSED": named["Done"]}},
                "allowBidirectionalSync": True,
            },
            "type": "PROJECT_ISSUE_SYNC",
        },
    )
    if status != 200:
        fail(f"{ident}: creating the link answered {status}", linked=done)
    done.append(ident)

result = {
    ("linked" if cfg["apply"] else "would_link"): done,
    "present": present,
    "missing_projects": missing_projects,
    "conflicts": conflicts,
}
if conflicts:
    fail("some declared links conflict with what Plane holds", **result)
print(json.dumps(result))
