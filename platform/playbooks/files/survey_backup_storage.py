#!/usr/bin/env python3
"""Plan and summarize the read-only Proxmox backup-storage survey.

Two modes, JSON on stdin, JSON on stdout:

  plan       {nodes, storage_listings, jobs, storage_config} -> which (node, storage) pairs to
             read and which prune retentions to dry-run on each. A shared storage is read once.
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
from itertools import combinations

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


def _canonical(value, owner):
    """A prune-backups value as one string: a dict becomes sorted k=v, spaces are dropped."""
    if isinstance(value, dict):
        value = ",".join(f"{k}={value[k]}" for k in sorted(value))
    value = str(value).replace(" ", "")
    if not RETENTION.match(value):
        raise Refusal(f"{owner} carries a prune-backups value this survey does not parse.")
    return value


def _retention(job):
    """A job's prune-backups as one canonical string, or DEFAULT_RETENTION when it defers to the storage."""
    value = job.get("prune-backups")
    if value in (None, "", {}):
        return DEFAULT_RETENTION
    return _canonical(value, "A backup job")


def _storage_prunes(cfg):
    """Whether a storage sets prune-backups itself, the setting a job that defers to it uses."""
    return (cfg or {}).get("prune-backups") not in (None, "", {})


def _resolve(retention, storage_cfg):
    """The retention a job means: its own, or the storage's when it defers. None when the storage sets none."""
    if retention != DEFAULT_RETENTION:
        return retention
    if not _storage_prunes(storage_cfg):
        return None
    return _canonical(storage_cfg["prune-backups"], "The storage")


def _retained(retention):
    """Upper bound on the archives one retention keeps per guest, or None when it keeps them all.

    For each keep-* option Proxmox keeps at most N distinct buckets (days, weeks, ...), and one
    archive can fill several, so the sum of the positive keep-* values bounds what is kept.
    keep-all, or no positive keep-* option at all, keeps everything: there is no bound.
    """
    pairs = [part.split("=", 1) for part in retention.split(",")]
    if any(key == "keep-all" and int(value) > 0 for key, value in pairs):
        return None
    counts = [int(value) for key, value in pairs if key != "keep-all" and int(value) > 0]
    return sum(counts) if counts else None


def _archives_kept(retentions, storage_cfg):
    """The archives a guest's jobs keep at its newest size, as the largest bound among them.

    0 when no job backs the guest up; None when any retention is unbounded or a storage
    default is unset, so the survey cannot say what would be kept.
    """
    kept = []
    for retention in retentions:
        resolved = _resolve(retention, storage_cfg)
        bound = _retained(resolved) if resolved else None
        if bound is None:
            return None
        kept.append(bound)
    return max(kept, default=0)


def _flag(value):
    """A Proxmox boolean as the API may return it: 1, "1" or true (True == 1 in Python)."""
    return value in (1, "1")


def _is_vmid(text):
    return re.fullmatch(r"[0-9]+", str(text)) is not None


def _vmids(text):
    """A vmid list as Proxmox splits it (pve-common split_list): commas, semicolons or whitespace."""
    return {int(v) for v in re.split(r"[,;\s]+", str(text or "")) if _is_vmid(v)}


def _job_guests(job, existing):
    """The guests a vzdump job backs up, chosen as Proxmox's VZDump.pm chooses them: (selects, vmid set).

    `existing` maps each vmid to its cluster resource. A pool comes first, then the vmid list,
    then all guests minus `exclude`. A job that names a node drops the existing guests living
    elsewhere. A listed vmid that no longer exists stays.
    """
    listed = _vmids(job.get("vmid"))
    if job.get("pool"):
        selects = "pool"
        guests = {vmid for vmid, v in existing.items() if v.get("pool") == job["pool"]}
    elif listed:
        selects, guests = "list", listed
    elif _flag(job.get("all")):
        selects = "all"
        guests = set(existing) - _vmids(job.get("exclude"))
    else:
        selects, guests = "none", set()
    if job.get("node"):
        guests = {vmid for vmid in guests if vmid not in existing or existing[vmid].get("node") == job["node"]}
    return selects, guests


def plan(data):
    # The playbook refuses these first; the planner checks again so its own tests hold the rule.
    if not [_name(n.get("node"), "node name") for n in data["nodes"] if n.get("status") == "online"]:
        raise Refusal("No Proxmox node is online.")
    configs = {c.get("storage"): c for c in data.get("storage_config") or []}
    retentions = {}
    for job in data["jobs"]:
        if job.get("type", "vzdump") == "vzdump" and job.get("storage"):
            retentions.setdefault(_name(job["storage"], "job storage ID"), set()).add(_retention(job))
    reads, seen = [], set()
    for listing in data["storage_listings"]:  # raw uri results, one per online node
        node = _name(listing.get("item"), "node name")
        if listing.get("status") != 200:  # the read accepts 403 so this refusal can name the node
            raise Refusal(f"Proxmox answered {listing.get('status')} listing the storages of node {node}.")
        for row in (listing.get("json") or {}).get("data") or []:
            storage = _name(row.get("storage"), "storage ID")
            if not (_has_backup(row.get("content")) and _flag(row.get("active"))):
                continue
            key = storage if _flag(row.get("shared")) else f"{node}/{storage}"
            if key in seen:
                continue
            seen.add(key)
            # Only the retentions of jobs on this storage are previewed; Proxmox errors on a preview with
            # no retention when the storage sets none, so the storage default is then never asked.
            wanted = retentions.get(storage, set())
            if not _storage_prunes(configs.get(storage)):
                wanted = wanted - {DEFAULT_RETENTION}
            reads.append({"node": node, "storage": storage,
                          "status": {k: row.get(k) for k in ("type", "shared", "total", "used", "avail")},
                          "retentions": sorted(wanted)})
    if not reads:
        raise Refusal("No active backup-capable storage is visible to the Proxmox token.")
    return {"reads": reads}


def _gib(n):
    return round((n or 0) / GIB, 1)


def _day(ts):
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d") if ts else None


def _ok(read, what, storage, privilege):
    if read.get("status") == 403:
        raise Refusal(f"The Proxmox token may not read {what} on storage {storage} "
                      f"(needs {privilege} there). Nothing was summarized.")
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
    if pa and pb and pa == pb:
        return "same path"
    if pa and pb and (pa.startswith(pb + "/") or pb.startswith(pa + "/")):
        return "one path inside the other"
    return None


def summarize(data):
    existing = {int(v["vmid"]): v for v in data["vms"] if _is_vmid(v.get("vmid", ""))}
    names = {vmid: v.get("name") for vmid, v in existing.items()}
    config = {c.get("storage"): c for c in data["storage_config"]}
    jobs = [j for j in data["jobs"] if j.get("type", "vzdump") == "vzdump"]
    job_guests = [(job, *_job_guests(job, existing)) for job in jobs]  # (job, selects, guests)
    # Raw uri results: content reads loop over plan reads, prune reads over (read, retention).
    reads = {}
    for r in data["content_reads"]:
        reads[(r["item"]["node"], r["item"]["storage"])] = {"content": r, "prunes": []}
    for r in data["prune_reads"]:
        read, retention = r["item"]
        reads[(read["node"], read["storage"])]["prunes"].append({**r, "retention": retention})
    storages, measured = [], []  # measured: (row, raw bytes), for the backend comparison only
    for item in data["plan"]["reads"]:
        node, storage = item["node"], item["storage"]
        r = reads.get((node, storage))
        if r is None:
            raise Refusal(f"The survey has no reads for storage {storage}.")
        status = item["status"]
        shared = _flag(status.get("shared"))
        # PBS snapshots share chunks: their listed sizes are logical, not bytes on the disk.
        logical = status.get("type") == "pbs"
        volumes = _ok(r["content"], "content", storage, "Datastore.Audit")
        backups = [v for v in volumes if ":backup/" in str(v.get("volid"))]
        all_bytes = sum(int(v.get("size") or 0) for v in volumes)
        backup_bytes = sum(int(v.get("size") or 0) for v in backups)

        # Each previewed retention maps to the volumes its prune would remove on this storage.
        previewed = {}
        for prune in r["prunes"]:
            # With no vmid given, Proxmox checks Datastore.Allocate for the preview (pve-storage PruneBackups.pm).
            marks = _ok(prune, "prune preview", storage, "Datastore.Allocate")
            previewed[prune["retention"]] = {row["volid"] for row in marks if row.get("mark") == "remove"}
        # A guest's retentions are those of the vzdump jobs on this storage that back it up. A job backs
        # up and prunes a guest on the node it lives on, so on a node-local storage only the guests
        # living on that node take a retention.
        retention_of, protects_every_archive = {}, set()
        for job, _, guests in job_guests:
            if job.get("storage") == storage:
                if not shared:
                    guests = {vmid for vmid in guests if existing.get(vmid, {}).get("node") == node}
                for vmid in guests:
                    retention_of.setdefault(vmid, set()).add(_retention(job))
                if _flag(job.get("protected")):  # vzdump --protected: pruning never removes these
                    protects_every_archive |= guests
        cfg = config.get(storage)

        by_vmid = {}
        for v in backups:
            vmid = int(v.get("vmid") or 0)
            g = by_vmid.setdefault(vmid, {"vmid": vmid, "name": names.get(vmid), "archives": 0, "bytes": 0,
                                         "oldest": None, "newest": None, "newest_bytes": 0,
                                         "protected": 0, "protected_bytes": 0,
                                         "prunable": 0, "prunable_bytes": 0})
            size, ctime, volid = int(v.get("size") or 0), int(v.get("ctime") or 0), v.get("volid")
            g["archives"] += 1
            g["bytes"] += size
            if _flag(v.get("protected")):
                g["protected"] += 1
                g["protected_bytes"] += size
            g["oldest"] = ctime if g["oldest"] is None else min(g["oldest"], ctime)
            if g["newest"] is None or ctime > g["newest"]:
                g["newest"], g["newest_bytes"] = ctime, size
            # Prunable only when every retention backing this guest was previewed here and removes it.
            wanted = retention_of.get(vmid, set())
            if wanted and all(volid in previewed.get(x, set()) for x in wanted):
                g["prunable"] += 1
                g["prunable_bytes"] += size
        # A protected archive is kept on top of what keep-* keeps (pve-storage marks it protected,
        # and prune_mark_backup_group counts only unmarked archives),
        # so the need adds its bytes; a guest whose job protects every archive has no bound.
        needs = {vmid: None if vmid in protects_every_archive else _archives_kept(retention_of.get(vmid, set()), cfg)
                 for vmid in by_vmid}
        need_bytes = {vmid: g["newest_bytes"] * needs[vmid] + g["protected_bytes"]
                      for vmid, g in by_vmid.items() if needs[vmid] is not None}
        rows = []
        for g in sorted(by_vmid.values(), key=lambda g: -g["bytes"]):
            kept = needs[g["vmid"]]
            rows.append({"vmid": g["vmid"], "name": g["name"], "guest_exists": g["vmid"] in names,
                         "archives": g["archives"], "gib": _gib(g["bytes"]),
                         "oldest": _day(g["oldest"]), "newest": _day(g["newest"]),
                         "protected": g["protected"], "prunable": g["prunable"],
                         "prunable_gib": None if logical else _gib(g["prunable_bytes"]),
                         "job_retention": sorted(retention_of.get(g["vmid"], set())),
                         "retained_at_job_retention": kept,
                         "gib_needed_at_job_retention":
                             _gib(need_bytes[g["vmid"]]) if g["vmid"] in need_bytes else None})
        total, used = int(status.get("total") or 0), int(status.get("used") or 0)
        storages.append({
            "storage": storage, "read_on_node": node, "type": status.get("type"),
            "shared": shared,
            "total_gib": _gib(total), "used_gib": _gib(used), "avail_gib": _gib(status.get("avail")),
            "used_percent": round(100 * used / total, 1) if total else None,
            "backup_archives": len(backups), "backup_gib": _gib(backup_bytes),
            "other_volume_gib": _gib(all_bytes - backup_bytes),
            "sizes_are_logical": logical,
            "unaccounted_gib": None if logical else _gib(used - all_bytes),
            "prunable_archives": sum(g["prunable"] for g in by_vmid.values()),
            "prunable_gib": None if logical else _gib(sum(g["prunable_bytes"] for g in by_vmid.values())),
            "orphan_guest_gib": _gib(sum(g["bytes"] for g in by_vmid.values() if g["vmid"] not in names)),
            "gib_needed_at_job_retention": _gib(sum(need_bytes.values())),
            # A backed-up guest with no archive here yet has no size to multiply: unknown, not zero.
            "retention_need_unknown_guests": sorted(
                {vmid for vmid, kept in needs.items() if kept is None}
                | {vmid for vmid in retention_of if vmid in existing and vmid not in by_vmid}),
            "retentions_previewed": sorted(p["retention"] for p in r["prunes"]),
            "by_guest": rows,
        })
        files = {str(v.get("volid")).split(":", 1)[-1]: int(v.get("size") or 0) for v in volumes}
        measured.append((storages[-1], (total, used, files)))

    shared_backends = []
    for (a, (a_total, a_used, a_files)), (b, (b_total, b_used, b_files)) in combinations(measured, 2):
        # Node-local storages on different nodes are separate disks, whatever their paths say.
        if a["read_on_node"] != b["read_on_node"] and not (a["shared"] or b["shared"]):
            continue
        reason = _same_backend(config.get(a["storage"], {}), config.get(b["storage"], {}))
        if not reason and a_total and a_total == b_total and a_used == b_used:
            reason = "identical total and used bytes (inferred, not proven)"
        if reason:
            if reason in ("same server and export", "same path"):
                # One directory under two IDs lists the same files: count each relative path once.
                listed = sum(max(a_files.get(f, 0), b_files.get(f, 0)) for f in a_files.keys() | b_files.keys())
            elif reason == "one path inside the other":
                listed = sum(a_files.values()) + sum(b_files.values())  # different directories, different files
            else:
                listed = None  # inferred from byte counts alone: whether the listings overlap is unknown
            logical = a["sizes_are_logical"] or b["sizes_are_logical"]
            unaccounted = None if logical or listed is None else _gib(max(a_used, b_used) - listed)
            shared_backends.append({"storages": [a["storage"], b["storage"]], "reason": reason,
                                    "combined_unaccounted_gib": unaccounted})

    job_rows = []
    for job, selects, guests in job_guests:
        job_rows.append({"id": job.get("id"), "enabled": job.get("enabled", 1) not in (0, "0"),
                         "schedule": job.get("schedule") or job.get("starttime"),
                         "node": job.get("node") or "all", "storage": job.get("storage"),
                         "retention": _retention(job), "mode": job.get("mode"),
                         "selects": selects, "guests": len(guests),
                         # only a vmid list can name a guest that is gone
                         "missing_guests": sorted(v for v in guests if v not in names)})
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
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
        json.dump({"refusal": "Proxmox returned data this survey does not recognise."}, stdout)
        return 1
    json.dump(result, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
