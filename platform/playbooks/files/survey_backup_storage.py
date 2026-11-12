#!/usr/bin/env python3
"""Plan and summarize the read-only Proxmox backup-storage survey.

Two modes, JSON on stdin, JSON on stdout:

  plan       {nodes, storage_listings, jobs} -> which (node, storage) pairs to read and
             which prune retentions to dry-run on each. A shared storage is read once.
  summarize  {plan, vms, storage_config, jobs, content_reads, prune_reads} -> the receipt the playbook prints.

The receipt carries storage IDs, guest IDs and names, sizes, dates and job settings. It
never carries a server address, export, mount path, volume ID, note text or token.
A refusal exits 1 with {"refusal": "..."} so the play can print why without the input.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime

GIB = 1024 ** 3
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RETENTION = re.compile(r"^(keep-(all|last|hourly|daily|weekly|monthly|yearly)=[0-9]+)(,keep-[a-z]+=[0-9]+)*$")
DEFAULT_RETENTION = "storage-default"


class Refusal(Exception):
    pass


def _name(value, what):
    if not isinstance(value, str) or not NAME.match(value):
        raise Refusal(f"Proxmox returned a malformed {what}; no storage URL was built.")
    return value


def _has_backup(content):
    return "backup" in str(content or "").split(",")


def _retention(job):
    """A job's prune-backups as one canonical string, or the storage's own setting."""
    value = job.get("prune-backups")
    if value in (None, "", {}):
        return DEFAULT_RETENTION
    if isinstance(value, dict):
        value = ",".join(f"{k}={value[k]}" for k in sorted(value))
    value = str(value).replace(" ", "")
    if not RETENTION.match(value):
        raise Refusal("A backup job carries a prune-backups value this survey does not parse.")
    return value


def _job_vmids(job):
    if str(job.get("all", 0)) == "1":
        return None  # every guest
    return {int(v) for v in str(job.get("vmid") or "").split(",") if v.strip().isdigit()}


def plan(data):
    online = [_name(n.get("node"), "node name") for n in data["nodes"] if n.get("status") == "online"]
    if not online:
        raise Refusal("No Proxmox node is online.")
    retentions = {}
    for job in data["jobs"]:
        if job.get("type", "vzdump") == "vzdump" and job.get("storage"):
            retentions.setdefault(_name(job["storage"], "job storage ID"), set()).add(_retention(job))
    reads, seen = [], set()
    for listing in data["storage_listings"]:  # raw uri results, one per online node
        node = _name(listing.get("item"), "node name")
        for row in (listing.get("json") or {}).get("data") or []:
            storage = _name(row.get("storage"), "storage ID")
            if not (_has_backup(row.get("content")) and row.get("active") in (1, True)):
                continue
            key = storage if row.get("shared") in (1, True) else f"{node}/{storage}"
            if key in seen:
                continue
            seen.add(key)
            reads.append({"node": node, "storage": storage,
                          "status": {k: row.get(k) for k in ("type", "shared", "total", "used", "avail")},
                          "retentions": sorted(retentions.get(storage, {DEFAULT_RETENTION}))})
    if not reads:
        raise Refusal("No active backup-capable storage is visible to the Proxmox token.")
    return {"online_nodes": online, "reads": reads}


def _gib(n):
    return round((n or 0) / GIB, 1)


def _day(ts):
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d") if ts else None


def _ok(read, what, storage):
    if read.get("status") == 403:
        raise Refusal(f"The Proxmox token may not read {what} on storage {storage} "
                      "(needs Datastore.Audit there). Nothing was summarized.")
    if read.get("status") != 200:
        raise Refusal(f"Proxmox answered {read.get('status')} reading {what} on storage {storage}.")
    return (read.get("json") or {}).get("data") or []


def _backend(cfg):
    """What two storage IDs would share if they sat on the same filesystem. Never printed."""
    kind = cfg.get("type")
    if kind == "nfs":
        return ("nfs", cfg.get("server"), cfg.get("export"))
    if kind == "cifs":
        return ("cifs", cfg.get("server"), cfg.get("share"))
    if kind == "pbs":
        return ("pbs", cfg.get("server"), cfg.get("datastore"))
    return None


def _path_of(cfg):
    if cfg.get("path"):
        return cfg["path"].rstrip("/")
    if cfg.get("type") in ("nfs", "cifs"):
        return f"/mnt/pve/{cfg['storage']}"  # where Proxmox mounts a network storage
    return None


def _same_backend(a, b):
    if _backend(a) and _backend(a) == _backend(b):
        return "same server and export"
    pa, pb = _path_of(a), _path_of(b)
    if pa and pb and (pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")):
        return "one path inside the other"
    return None


def summarize(data):
    names = {int(v["vmid"]): v.get("name") for v in data["vms"] if str(v.get("vmid", "")).isdigit()}
    config = {c.get("storage"): c for c in data["storage_config"]}
    jobs = [j for j in data["jobs"] if j.get("type", "vzdump") == "vzdump"]
    # Raw uri results: content reads loop over plan reads, prune reads over (read, retention).
    reads = {}
    for r in data["content_reads"]:
        reads[(r["item"]["node"], r["item"]["storage"])] = {"content": r, "prunes": []}
    for r in data["prune_reads"]:
        read, retention = r["item"]
        reads[(read["node"], read["storage"])]["prunes"].append({**r, "retention": retention})
    storages = []
    for item in data["plan"]["reads"]:
        node, storage = item["node"], item["storage"]
        r = reads.get((node, storage))
        if r is None:
            raise Refusal(f"The survey has no reads for storage {storage}.")
        status = item["status"]
        volumes = _ok(r["content"], "content", storage)
        backups = [v for v in volumes if ":backup/" in str(v.get("volid"))]
        all_bytes = sum(int(v.get("size") or 0) for v in volumes)
        backup_bytes = sum(int(v.get("size") or 0) for v in backups)

        remove = {}
        for prune in r["prunes"]:
            for row in _ok(prune, "prune preview", storage):
                if row.get("mark") == "remove":
                    remove.setdefault(row["volid"], set()).add(prune["retention"])
        size_of = {v.get("volid"): int(v.get("size") or 0) for v in volumes}
        # A guest's archive counts as prunable only under the retention of the job that backs it up.
        keep_last = {}
        for job in jobs:
            if job.get("storage") != storage:
                continue
            m = re.search(r"keep-last=([0-9]+)", _retention(job))
            vmids = _job_vmids(job)
            for vmid in (vmids if vmids is not None else names):
                keep_last[vmid] = max(keep_last.get(vmid, 0), int(m.group(1)) if m else 0)

        by_vmid = {}
        for v in backups:
            vmid = int(v.get("vmid") or 0)
            g = by_vmid.setdefault(vmid, {"vmid": vmid, "name": names.get(vmid), "archives": 0, "bytes": 0,
                                         "oldest": None, "newest": None, "newest_bytes": 0,
                                         "protected": 0, "prunable": 0, "prunable_bytes": 0})
            size, ctime = int(v.get("size") or 0), int(v.get("ctime") or 0)
            g["archives"] += 1
            g["bytes"] += size
            g["protected"] += 1 if v.get("protected") in (1, True) else 0
            g["oldest"] = ctime if g["oldest"] is None else min(g["oldest"], ctime)
            if g["newest"] is None or ctime > g["newest"]:
                g["newest"], g["newest_bytes"] = ctime, size
            if v.get("volid") in remove:
                g["prunable"] += 1
                g["prunable_bytes"] += size
        rows = []
        for g in sorted(by_vmid.values(), key=lambda g: -g["bytes"]):
            k = keep_last.get(g["vmid"], 0)
            rows.append({"vmid": g["vmid"], "name": g["name"], "guest_exists": g["vmid"] in names,
                         "archives": g["archives"], "gib": _gib(g["bytes"]),
                         "oldest": _day(g["oldest"]), "newest": _day(g["newest"]),
                         "protected": g["protected"], "prunable": g["prunable"],
                         "prunable_gib": _gib(g["prunable_bytes"]), "job_keep_last": k or None,
                         "gib_at_full_retention": _gib(g["newest_bytes"] * k) if k else None})
        total, used = int(status.get("total") or 0), int(status.get("used") or 0)
        storages.append({
            "storage": storage, "read_on_node": node, "type": status.get("type"),
            "shared": status.get("shared") in (1, True),
            "total_gib": _gib(total), "used_gib": _gib(used), "avail_gib": _gib(status.get("avail")),
            "used_percent": round(100 * used / total, 1) if total else None,
            "backup_archives": len(backups), "backup_gib": _gib(backup_bytes),
            "other_volume_gib": _gib(all_bytes - backup_bytes),
            "unaccounted_gib": _gib(used - all_bytes),
            "prunable_archives": len(remove),
            "prunable_gib": _gib(sum(size_of.get(v, 0) for v in remove)),
            "orphan_guest_gib": _gib(sum(g["bytes"] for g in by_vmid.values() if g["vmid"] not in names)),
            "gib_needed_at_job_retention": _gib(sum(
                g["newest_bytes"] * keep_last.get(g["vmid"], 0) for g in by_vmid.values())),
            "retentions_previewed": sorted(p["retention"] for p in r["prunes"]),
            "by_guest": rows,
            "_used": used, "_all": all_bytes, "_total": total,
        })

    shared_backends = []
    for i, a in enumerate(storages):
        for b in storages[i + 1:]:
            reason = _same_backend(config.get(a["storage"], {"storage": a["storage"]}),
                                   config.get(b["storage"], {"storage": b["storage"]}))
            if not reason and a["_total"] and a["_total"] == b["_total"] and a["_used"] == b["_used"]:
                reason = "identical total and used bytes (inferred, not proven)"
            if reason:
                unaccounted = max(a["_used"], b["_used"]) - a["_all"] - b["_all"]
                shared_backends.append({"storages": [a["storage"], b["storage"]], "reason": reason,
                                        "combined_unaccounted_gib": _gib(unaccounted)})
    for s in storages:
        for k in ("_used", "_all", "_total"):
            s.pop(k)

    job_rows = []
    for job in jobs:
        vmids = _job_vmids(job)
        job_rows.append({"id": job.get("id"), "enabled": str(job.get("enabled", 1)) != "0",
                         "schedule": job.get("schedule") or job.get("starttime"),
                         "node": job.get("node") or "all", "storage": job.get("storage"),
                         "retention": _retention(job), "mode": job.get("mode"),
                         "guests": "all" if vmids is None else len(vmids),
                         "missing_guests": [] if vmids is None else sorted(v for v in vmids if v not in names)})
    return {"storages": storages, "shared_backends": shared_backends, "jobs": job_rows}


def main(argv=None, stdin=sys.stdin, stdout=sys.stdout):
    argv = sys.argv[1:] if argv is None else argv
    try:
        mode = argv[0] if argv else ""
        if mode not in ("plan", "summarize"):
            raise Refusal("usage: survey_backup_storage.py plan|summarize < input.json")
        data = json.load(stdin)
        result = plan(data) if mode == "plan" else summarize(data)
    except Refusal as exc:
        json.dump({"refusal": str(exc)}, stdout)
        return 1
    except (KeyError, TypeError, ValueError, AttributeError):
        json.dump({"refusal": "Proxmox returned data this survey does not recognise."}, stdout)
        return 1
    json.dump(result, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
