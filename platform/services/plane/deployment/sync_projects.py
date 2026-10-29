"""Make Plane hold every declared project, module and work item. Nothing is ever removed or deleted.

Joined behind plane_session.py and run in plane-api the way reconcile_members.py is, with one JSON
document on stdin:

  {base, host, origin,                plane_session.py
   service_token,                     the workspace's service API token (service_token.py mints it;
                                      Deploy Plane stores it in OpenBao); sent as X-Api-Key
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
Rows Plane holds that the declaration no longer names are left alone.

Module membership follows tasks.md: every declared work item is linked to its declared module. The
module's own list is read first, so a link that failed after its item's create is repaired by the next
run. Links are only added, never removed: to move an item, move its task; the old link stays. A 409
that names a work item the project list does not show (archived, or in intake triage) is counted as
existing and left unlinked, because Plane refuses to link an inactive item.

The ONE update: Plane creates a project with Modules switched off (module_view defaults to False, and a
module create is then refused), so a project this tool made is created with Modules on. A project found
with Modules off gets exactly {"module_view": true}, and only when this run has a module to create in
it; reported under "repairs". Items and modules are never updated.

Auth is the service token alone: the sync never signs in and never mints a token.

Prints ONE JSON line of counts, never a credential. A refusal prints {"error"} and exits 1. A refusal
after the run has created or linked anything also carries "actions" (the last 20) and "actions_total",
so a re-run can be read against what was already done.
"""

# Runs after plane_session.py (cfg, fail, call, json), joined by the caller, so its
# names are defined only in the joined source.
# ruff: noqa: F821

import fcntl
import os
import time

EXTERNAL_SOURCE = "openspec"
slug = cfg["workspace_slug"]
apply = str(cfg.get("apply")).lower() == "true"
PACE = float(cfg.get("pace_seconds", 0.25))
TOKEN = cfg.get("service_token") or ""
if not TOKEN:
    fail("no service token given; run Deploy Plane, which mints it into OpenBao")
done = []  # what this run already created or linked, named in any refusal


def stop(message):
    """A refusal after work: the last 20 actions, and how many there were in all."""
    fail(message, actions=done[-20:], actions_total=len(done))


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
            stop(f"listing {path} failed ({status}){reason(page)}")
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
        stop(f"creating {label} failed ({status}){reason(made)}")
    return str(made["id"]), True


def create(path, body, label):
    status, _, made = v1("POST", path, body)
    return made_or_found(status, made, label)


def create_project(p):
    status, _, made = v1("POST", f"/workspaces/{slug}/projects/",
                         {"name": p["name"], "identifier": p["identifier"], "external_id": p["repo"],
                          "external_source": EXTERNAL_SOURCE, "module_view": True})
    if status == 409 and isinstance(made, dict) and "identifier" in made:
        stop(f"project identifier {p['identifier']} for {p['repo']} is taken by another project")
    return made_or_found(status, made, f"project {p['repo']}")


def linked(pid, mid):
    """Ids of the work items linked to one module. The GET serves Issue rows, so each row's id is the item's."""
    return {str(r["id"]) for r in listing(f"/workspaces/{slug}/projects/{pid}/modules/{mid}/module-issues/")}


counts = {bucket: {"projects": 0, "modules": 0, "items": 0, "links": 0}
          for bucket in ("created", "would_create", "existing")}


def tally(bucket, kind):
    counts[bucket][kind] += 1


try:
    project_rows = listing(f"/workspaces/{slug}/projects/")
    known = ours(project_rows)
    modules_off = {r["external_id"] for r in project_rows
                   if r.get("external_source") == EXTERNAL_SOURCE and r.get("module_view") is False}
    repairs = []
    for p in cfg["projects"]:
        pid = known.get(p["repo"])
        if pid is None and not apply:
            tally("would_create", "projects")
            for m in p["modules"]:
                tally("would_create", "modules")
                for _ in m["items"]:
                    tally("would_create", "items")
                    tally("would_create", "links")  # a new item is linked right after its create
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
        # Plane refuses a module create while Modules is off, so a project found that way is switched on,
        # and only when this run has a module to create in it.
        if p["repo"] in modules_off and any(m["external_id"] not in modules for m in p["modules"]):
            if not apply:
                repairs.append(f"would enable modules on project {p['repo']}")
            else:
                status, _, body = v1("PATCH", f"/workspaces/{slug}/projects/{pid}/", {"module_view": True})
                if status != 200:
                    stop(f"enabling modules on project {p['repo']} failed ({status}){reason(body)}")
                done.append(f"enabled modules on project {p['repo']}")
                repairs.append(f"enabled modules on project {p['repo']}")

        for m in p["modules"]:
            mid = modules.get(m["external_id"])
            if mid is None and not apply:
                tally("would_create", "modules")
                in_module = set()  # a module that does not exist yet links nothing
            else:
                if mid is None:
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
                in_module = linked(pid, mid)

            for it in m["items"]:
                iid = items.get(it["external_id"])
                if iid is None and not apply:
                    tally("would_create", "items")
                    tally("would_create", "links")
                    continue
                if iid is None:
                    iid, made = create(f"/workspaces/{slug}/projects/{pid}/issues/",
                                       {"name": it["name"], "description_html": it["description_html"],
                                        "external_id": it["external_id"], "external_source": EXTERNAL_SOURCE},
                                       f"work item {it['name']}")
                    items[it["external_id"]] = iid
                    if not made:
                        # The 409 names a row the project list does not show (archived, or in intake
                        # triage). Plane refuses to link those, so it is counted and left unlinked.
                        tally("existing", "items")
                        continue
                    tally("created", "items")
                    done.append(f"created work item {it['name']}")
                else:
                    tally("existing", "items")
                if iid in in_module:
                    continue
                if not apply:
                    tally("would_create", "links")
                    continue
                status, _, body = v1("POST", f"/workspaces/{slug}/projects/{pid}/modules/{mid}/module-issues/",
                                     {"issues": [iid]})
                if status not in (200, 201):
                    stop(f"linking work item {it['name']} to module {m['name']} failed ({status}){reason(body)}")
                in_module.add(iid)
                tally("created", "links")
                done.append(f"linked work item {it['name']}")

    changed = any(counts["created"].values()) or (apply and bool(repairs))
    print(json.dumps({"changed": changed, "applied": apply, **counts, "repairs": repairs}))
except Exception as e:
    stop(f"stopped by {type(e).__name__}")
