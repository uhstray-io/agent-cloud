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
   schema,                            optional, from platform/services/plane/schema.yml:
                                      {states: [{name, group, color}],
                                       labels: [{name, color, description}],
                                       item_labels: [name]}; absent: no state or label is touched
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

The project-level update: Plane creates a project with Modules switched off (module_view defaults to False,
and a module create is then refused), so a project this tool made is created with Modules on. A project
found with Modules off gets exactly {"module_view": true}, and only when this run has a module to create
in it; reported under "repairs". Items and modules are never updated.

Schema (optional). Each project's STATES and LABELS follow the declared schema. A declared row the
project lacks is created. A row matched by name (case-insensitive) gets one PATCH with only the declared
fields that differ (states: group, color; labels: color, description); the name is never patched. Colors
compare case-insensitively, and a null description counts as empty. Renames are not followed: a declared
row renamed in Plane is created again under its declared name, so rename in the schema (which adds a
row) instead. Undeclared states and labels are read but never written or deleted, and the default flag
is never set. The Triage name and the triage group are refused before any write: the state list hides
Triage and the create refuses it. Every work item this run creates carries the item_labels, resolved to
that project's label ids; an existing item is never updated. Work item types are not managed here.

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


SCHEMA = cfg.get("schema") or {}
DECLARED = {"states": SCHEMA.get("states") or [], "labels": SCHEMA.get("labels") or []}
ITEM_LABELS = SCHEMA.get("item_labels") or []
SCHEMA_FIELDS = {"states": ("group", "color"), "labels": ("color", "description")}
# A bad declaration is refused here, before the lock and before the first request: nothing is done.
for kind, rows in DECLARED.items():
    seen = set()
    for i, d in enumerate(rows):
        name = d.get("name") if isinstance(d, dict) else None
        if not isinstance(name, str) or not name or name != name.strip():
            stop(f"schema {kind}[{i}] needs a name without leading or trailing spaces")
        missing = [f for f in SCHEMA_FIELDS[kind] if not isinstance(d.get(f), str)]
        if missing:
            stop(f"schema {kind[:-1]} {name} is missing {', '.join(missing)}")
        if name.lower() in seen:
            stop(f"schema {kind[:-1]} {name} is declared twice (names match ignoring case)")
        seen.add(name.lower())
for d in DECLARED["states"]:
    if d["name"].lower() == "triage" or d["group"] == "triage":
        stop(f"state {d['name']} cannot be declared: the Triage name and the triage group are reserved")
declared_labels = {d["name"].lower() for d in DECLARED["labels"]}
for name in ITEM_LABELS:
    if not isinstance(name, str) or name.lower() not in declared_labels:
        stop(f"item label {name} is not declared in schema labels")

# The states Plane seeds into every new project (db/models/state.py DEFAULT_STATES, created by the
# v1 project create through utils/seed_project_defaults.py): a dry run for a project not made yet
# does not count these as creates, because the project create brings them.
PLANE_SEEDED_STATES = {"backlog", "todo", "in progress", "done", "cancelled"}


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


def norm(field, value):
    """A declared value and a served one compare equal: a hex color in any case, a null description as empty."""
    value = value or ""
    return value.lower() if field == "color" else value


def converge(kind, pid, declared):
    """Create the declared states or labels this project lacks, and PATCH only the declared fields that differ.

    Rows match by name, case-insensitively. A matched row's name is never patched, and a row the declaration
    does not name is read but never written or deleted. Returns name -> id for each declared row this run
    created or found; a dry run creates nothing, so it returns only the rows it found.
    """
    path = f"/workspaces/{slug}/projects/{pid}/{kind}/"
    fields = SCHEMA_FIELDS[kind]
    have = {r["name"].lower(): r for r in listing(path)}
    ids = {}
    for d in declared:
        key = d["name"].lower()
        found = have.get(key)
        if found is None:
            if not apply:
                tally("would_create", kind)
                continue
            rid, made = create(path, {"name": d["name"], **{f: d[f] for f in fields}}, f"{kind[:-1]} {d['name']}")
            if made:
                done.append(f"created {kind[:-1]} {d['name']}")
            tally("created" if made else "existing", kind)
            ids[key] = rid
            continue
        ids[key] = str(found["id"])
        tally("existing", kind)
        diff = {f: d[f] for f in fields if norm(f, found.get(f)) != norm(f, d[f])}
        if not diff:
            continue
        if not apply:
            tally("would_update", kind)
            continue
        status, _, body = v1("PATCH", f"{path}{found['id']}/", diff)
        if status != 200:
            stop(f"updating {kind[:-1]} {d['name']} failed ({status}){reason(body)}")
        done.append(f"updated {kind[:-1]} {d['name']}")
        tally("updated", kind)
    return ids


counts = {bucket: {"projects": 0, "modules": 0, "items": 0, "links": 0, "states": 0, "labels": 0}
          for bucket in ("created", "would_create", "existing")}
counts.update({bucket: {"states": 0, "labels": 0} for bucket in ("updated", "would_update")})


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
            for d in DECLARED["states"]:
                if d["name"].lower() not in PLANE_SEEDED_STATES:
                    tally("would_create", "states")
            for _ in DECLARED["labels"]:
                tally("would_create", "labels")
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

        # Schema before modules and items: the item creates below carry the label ids this step resolves.
        if DECLARED["states"]:
            converge("states", pid, DECLARED["states"])
        label_ids = {}
        if DECLARED["labels"]:
            label_ids = converge("labels", pid, DECLARED["labels"])
        item_label_ids = [label_ids[n.lower()] for n in ITEM_LABELS] if apply else []

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
                                        "external_id": it["external_id"], "external_source": EXTERNAL_SOURCE,
                                        **({"labels": item_label_ids} if item_label_ids else {})},
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

    changed = any(counts["created"].values()) or any(counts["updated"].values()) or (apply and bool(repairs))
    print(json.dumps({"changed": changed, "applied": apply, **counts, "repairs": repairs}))
except Exception as e:
    stop(f"stopped by {type(e).__name__}")
