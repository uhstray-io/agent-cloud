"""Make Plane hold every declared project, module and work item. Create-only: nothing is ever changed.

Joined behind plane_session.py and run in plane-api the way reconcile_members.py is, with one JSON
document on stdin:

  {base, host, origin,                plane_session.py
   email, password,                   the automation account (the workspace's creator)
   workspace_slug,
   apply: bool,                       without it nothing is written; the report counts what would be
   projects: [{repo, identifier, name,
               modules: [{external_id, name, description,
                          items: [{external_id, name, description_html}]}]}],
   pace_seconds,                      optional, default 0.25 (the tests pass 0)
   lock_path}                         optional, default /tmp/agent-cloud-plane-sync.lock

Identity is the external pair (external_source "openspec", external_id): a project's external_id is
its repo, a module's and a work item's is the id the declaration gives it. A repeat create answers
409 with the existing id, so a row already there is counted as existing and used, never re-created.
Nothing is ever PATCHed or DELETEd: a row Plane holds that the declaration no longer names is left
alone. A work item is linked to its module only when this run created it; an existing one is never
re-linked.

Auth is the workspace service token: the session signs in once, POST .../service-api-tokens/ returns
the token, and every /api/v1 call sends it as X-Api-Key.

Prints ONE JSON line of counts, never a credential. A refusal prints {"error"} and exits 1; once a create
has happened it also carries "actions", naming what this run had already created, so a re-run can be
read against it.
"""

# Runs after plane_session.py (cfg, fail, call, form_post, json), joined by the caller, so its
# names are defined only in the joined source.
# ruff: noqa: F821

import fcntl
import os
import time

EXTERNAL_SOURCE = "openspec"
slug = cfg["workspace_slug"]
apply = str(cfg.get("apply")).lower() == "true"
PACE = float(cfg.get("pace_seconds", 0.25))
done = []  # what this run already created, named in any refusal


# The overlap lock. Plane's external-id 409 is an application read with no database constraint
# behind it, so two overlapping runs can both pass it and both create the same row. The lock is
# taken here, in the process that does the creates, and held by the open descriptor: a run that
# dies releases it with its process, so it can never be left stuck.
run_lock = os.open(cfg.get("lock_path", "/tmp/agent-cloud-plane-sync.lock"), os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    fail("another Plane project sync is already running; refusing to overlap it")


def reason(body):
    """Plane's own error text for a refused call, short. Only refusals are passed here."""
    if isinstance(body, dict) and body:
        return ": " + "; ".join(str(v) for v in body.values())[:300]
    return ""


form_post("/auth/sign-in/", {"email": cfg["email"], "password": cfg["password"]})
status, _, issued = call("POST", f"/api/workspaces/{slug}/service-api-tokens/")
if status not in (200, 201) or not isinstance(issued, dict) or not issued.get("token"):
    fail(f"service token for {slug} refused ({status})")  # no body: a success reply carries the token
TOKEN = issued["token"]


def v1(method, path, data=None):
    # ponytail: a fixed 4 calls a second stays under the service token's 300/minute throttle
    # (api/rate_limit.py ServiceTokenRateThrottle). Adaptive pacing, if a run ever gets long enough.
    time.sleep(PACE)
    return call(method, "/api/v1" + path, data, headers={"X-Api-Key": TOKEN})


def listing(path):
    """Every row of a cursor-paginated v1 list: results, next_cursor, next_page_results."""
    rows, cursor = [], ""
    while True:
        status, _, page = v1("GET", f"{path}?per_page=100{cursor}")
        if status != 200 or not isinstance(page, dict) or not isinstance(page.get("results"), list):
            fail(f"listing {path} failed ({status}){reason(page)}", actions=done)
        rows += page["results"]
        if not page.get("next_page_results"):
            return rows
        cursor = f"&cursor={page['next_cursor']}"


def ours(rows):
    """external_id -> id, for the rows this tool made (the openspec source) and no others."""
    return {r["external_id"]: str(r["id"]) for r in rows
            if r.get("external_source") == EXTERNAL_SOURCE and r.get("external_id")}


def made_or_found(status, made, label):
    """(id, created) for one POST. A 409 that names an id is a row already there: use it, do not re-create."""
    if status == 409 and isinstance(made, dict) and made.get("id"):
        return str(made["id"]), False
    if status not in (200, 201) or not isinstance(made, dict) or not made.get("id"):
        fail(f"creating {label} failed ({status}){reason(made)}", actions=done)
    return str(made["id"]), True


def create(path, body, label):
    status, _, made = v1("POST", path, body)
    return made_or_found(status, made, label)


def create_project(p):
    status, _, made = v1("POST", f"/workspaces/{slug}/projects/",
                         {"name": p["name"], "identifier": p["identifier"], "external_id": p["repo"],
                          "external_source": EXTERNAL_SOURCE})
    if status == 409 and isinstance(made, dict) and "identifier" in made:
        fail(f"project identifier {p['identifier']} for {p['repo']} is taken by another project", actions=done)
    return made_or_found(status, made, f"project {p['repo']}")


counts = {bucket: {"projects": 0, "modules": 0, "items": 0} for bucket in ("created", "would_create", "existing")}


def tally(bucket, kind):
    counts[bucket][kind] += 1


known = ours(listing(f"/workspaces/{slug}/projects/"))
for p in cfg["projects"]:
    pid = known.get(p["repo"])
    if pid is None and not apply:
        tally("would_create", "projects")
        for m in p["modules"]:
            tally("would_create", "modules")
            for _ in m["items"]:
                tally("would_create", "items")
        continue
    if pid is None:
        pid, made = create_project(p)
        if made:
            done.append(f"created project {p['repo']}")
        tally("created" if made else "existing", "projects")
    else:
        tally("existing", "projects")

    modules = ours(listing(f"/workspaces/{slug}/projects/{pid}/modules/"))
    items = ours(listing(f"/workspaces/{slug}/projects/{pid}/issues/"))
    for m in p["modules"]:
        mid = modules.get(m["external_id"])
        if mid is None and not apply:
            tally("would_create", "modules")
        elif mid is None:
            mid, made = create(f"/workspaces/{slug}/projects/{pid}/modules/",
                               {"name": m["name"], "description": m["description"],
                                "external_id": m["external_id"], "external_source": EXTERNAL_SOURCE},
                               f"module {m['name']}")
            modules[m["external_id"]] = mid
            if made:
                done.append(f"created module {m['name']}")
            tally("created" if made else "existing", "modules")
        else:
            tally("existing", "modules")

        for it in m["items"]:
            if it["external_id"] in items:
                tally("existing", "items")
                continue
            if not apply:
                tally("would_create", "items")
                continue
            iid, made = create(f"/workspaces/{slug}/projects/{pid}/issues/",
                               {"name": it["name"], "description_html": it["description_html"],
                                "external_id": it["external_id"], "external_source": EXTERNAL_SOURCE},
                               f"work item {it['name']}")
            items[it["external_id"]] = iid
            if not made:
                tally("existing", "items")  # already there: this run did not create it, so it does not link it
                continue
            tally("created", "items")
            done.append(f"created work item {it['name']}")
            # ponytail: a link that fails after its create leaves that item unlinked, and a re-run
            # finds it existing and never links it. Repairing that needs a membership read; not built yet.
            status, _, body = v1("POST", f"/workspaces/{slug}/projects/{pid}/modules/{mid}/module-issues/",
                                 {"issues": [iid]})
            if status not in (200, 201):
                fail(f"linking work item {it['name']} to module {m['name']} failed ({status}){reason(body)}",
                     actions=done)
            done.append(f"linked work item {it['name']}")

print(json.dumps({"changed": any(counts["created"].values()), "applied": apply, **counts}))
