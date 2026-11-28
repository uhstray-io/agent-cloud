#!/usr/bin/env python3
"""Plan and summarize the read-only Proxmox backup-storage survey.

Two modes, JSON on stdin, JSON on stdout:

  plan       {nodes, storage_listings, jobs, storage_config} -> which (node, storage) pairs to
             read, which prune retentions to dry-run on each, and whether to read the Ceph listing
             (`ceph`, the one gate for it). A shared storage is read once.
  summarize  {plan, vms, storage_config, jobs, content_reads, prune_reads, disk_reads, ceph_reads,
              ceph_capacity_reads, vzdump_defaults_reads}
             -> the receipt the playbook prints. disk_reads, ceph_reads, ceph_capacity_reads and vzdump_defaults_reads
             are optional, so a payload without them still summarizes (their sections then read "not read" or are
             empty, and a node whose vzdump defaults were not read has an unknown remove).

The receipt carries storage IDs, guest IDs and names, sizes, dates and job settings, the CephFS
filesystems and pools, each Ceph OSD's fill and host with each host's total, the cluster's Ceph health
status and check codes (never a check's message), and each node's disks: device path, size, model,
vendor, serial, Proxmox's `used` tag, the in-use flag that tag implies, and OSD id. A CephFS storage
shares a filesystem with another when their filesystem names agree: the storage's `fs-name`, or the
cluster's only filesystem. Each job row names the job's own `remove`, or "node default" when the job takes its
node's, and each node's `remove` default is listed with its read's status. Of a defaults response only `remove` is
ever printed: never its mailto, script, dumpdir, tmpdir or any other key.
A storage whose filesystem resolves neither way is unknown, and so is what it shares; it is never
listed as another storage's sharer, so a resolved storage lists only the resolved storages it shares with. The receipt
never carries a server address, export, mount path, volume ID, note text, WWN, SMART health, token,
a Ceph health message, or a monitor or manager map (addresses).
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


def _enabled(job):
    """Whether Proxmox runs a job: a disabled one never backs up or prunes. False == 0 in Python."""
    return job.get("enabled", 1) not in (0, "0")


def _remove(value):
    """A vzdump `remove` as the API may spell it: True or False, or None when it is neither 0, 1 nor a boolean."""
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    if value in ("0", "1"):
        return value == "1"
    return None


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
    # storage ID -> {(retention, the job's node or "")}: a node-local storage reads only the jobs on its node.
    retentions = {}
    for job in data["jobs"]:
        # A job whose own remove is off never prunes, so no retention is previewed for it.
        if job.get("type", "vzdump") == "vzdump" and job.get("storage") and _enabled(job) \
                and not ("remove" in job and _remove(job["remove"]) is False):
            retentions.setdefault(_name(job["storage"], "job storage ID"), set()).add(
                (_retention(job), str(job.get("node") or "")))
    reads, seen = [], set()
    for listing in data["storage_listings"]:  # raw uri results, one per online node
        node = _name(listing.get("item"), "node name")
        if listing.get("status") != 200:  # the read accepts 403 so this refusal can name the node
            raise Refusal(f"Proxmox answered {listing.get('status')} listing the storages of node {node}.")
        for row in (listing.get("json") or {}).get("data") or []:
            storage = _name(row.get("storage"), "storage ID")
            # A disabled storage is left out whatever its status row says, so nothing about it is read.
            if not (_has_backup(row.get("content")) and _flag(row.get("active"))) \
                    or _flag(configs.get(storage, {}).get("disable")):
                continue
            key = storage if _flag(row.get("shared")) else f"{node}/{storage}"
            if key in seen:
                continue
            seen.add(key)
            # Only the retentions of jobs on this storage are previewed; Proxmox errors on a preview with
            # no retention when the storage sets none, so the storage default is then never asked.
            # A node-local storage takes only the jobs that run on its node (a job naming no node runs on every one).
            pairs = retentions.get(storage, set())
            if not _flag(row.get("shared")):
                pairs = {(retention, job_node) for retention, job_node in pairs if job_node in ("", node)}
            wanted = {retention for retention, _ in pairs}
            if not _storage_prunes(configs.get(storage)):
                wanted = wanted - {DEFAULT_RETENTION}
            reads.append({"node": node, "storage": storage,
                          "status": {k: row.get(k) for k in ("type", "shared", "total", "used", "avail")},
                          "retentions": sorted(wanted)})
    if not reads:
        raise Refusal("No active backup-capable storage is visible to the Proxmox token.")
    return {"reads": reads, "ceph": any(_own_cephfs(c) for c in data.get("storage_config") or [])}


def _gib(n):
    return round((n or 0) / GIB, 1)


def _day(ts):
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d") if ts else None


def _ok(read, what, storage, privilege):
    if read.get("status") == 403:
        raise Refusal(f"The Proxmox token may not read {what} on storage {storage} (needs {privilege} there).")
    if read.get("status") != 200:
        raise Refusal(f"Proxmox answered {read.get('status')} reading {what} on storage {storage}.")
    # A 200 without a list is refused, never read as empty: an empty content read under-reports a storage
    # (and in the cleanup's read-back reports every listed archive gone), an empty prune preview removes nothing.
    data = (read.get("json") or {}).get("data")
    if not isinstance(data, list):
        raise Refusal(f"Proxmox returned no {what} list for storage {storage}.")
    return data


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


def _guests(storages):
    """Each guest's newest archive day on every storage it has one on.

    A shared storage is keyed by its ID; a node-local one by node and ID, since each node holds its own.
    """
    merged = {}
    for s in storages:
        key = s["storage"] if s["shared"] else f"{s['read_on_node']}/{s['storage']}"
        for row in s["by_guest"]:
            if row["newest"] is None:
                continue
            guest = merged.setdefault(row["vmid"], {"vmid": row["vmid"], "name": row["name"],
                                                    "guest_exists": row["guest_exists"], "newest": {}})
            guest["newest"][key] = row["newest"]
    return [merged[vmid] for vmid in sorted(merged)]


def _ceph_problem(reads):
    """Why a Ceph listing the survey read cannot be used, or None when every read answered 200."""
    for read in reads.values():
        if read.get("status") == 403:
            return "HTTP 403: needs Sys.Audit or Datastore.Audit on /"
        if read.get("status") != 200:
            return f"HTTP {read.get('status')}"
    return None


CEPH_WHAT = {"fs": "Ceph fs", "pool": "Ceph pool"}


def _own_cephfs(cfg):
    """A CephFS storage on this cluster's own Ceph: enabled, with no monitor list (a list names another cluster)."""
    return cfg.get("type") == "cephfs" and "monhost" not in cfg and not _flag(cfg.get("disable"))


def _cephfs_configs(config):
    """The enabled CephFS storages in storage-ID order. A disabled one is left out of every CephFS figure."""
    return sorted((c for c in config.values() if c.get("type") == "cephfs" and not _flag(c.get("disable"))),
                  key=lambda c: str(c.get("storage")))


CEPH_FRACTION_FROM = 13  # the first Ceph release whose pool percent_used is a fraction of 1


def _ceph_major(version):
    """The major release a Ceph version names: the digits before its first dot, or None when it names none."""
    match = re.match(r"([0-9]+)\.", version) if isinstance(version, str) else None
    return int(match.group(1)) if match else None


def _percent_scale(capacity_reads):
    """How a pool's percent_used reads, as (the receipt's name for the scale, whether to multiply by 100 or None).

    Ceph 13 or later sends a fraction of 1 (src/mon/PGMap.cc v13.2.0 :881), so it is multiplied by 100 here. Ceph 12
    sent a percent already (src/mon/PGMap.cc v12.2.13 :839), so it is read as reported. The release is read from every
    OSD leaf of the OSD tree (`nodes/<node>/ceph/osd`, only when it answered 200), each of which carries
    `ceph_version_short` (pve-manager PVE/API2/Ceph/OSD.pm at e5ac6d6f :190-192). Any doubt is unknown (None), never a
    guessed scale: no tree, an empty tree, a leaf with no readable version, or releases on both sides of 13.
    """
    osd_read = next((r for r in capacity_reads or [] if not r.get("skipped") and _is_osd_item(r.get("item"))), None)
    tree = (osd_read.get("json") or {}).get("data") if osd_read and osd_read.get("status") == 200 else None
    root = tree.get("root") if isinstance(tree, dict) else None
    if not isinstance(root, dict):
        return "unknown: the Ceph OSD tree was not read", None
    leaves = _osd_leaves(root, None, [])
    if not leaves:
        return "unknown: the Ceph OSD tree lists no OSD", None
    majors = [_ceph_major(leaf.get("ceph_version_short")) for leaf, _ in leaves]
    if None in majors:
        return "unknown: an OSD reports no Ceph version", None
    if min(majors) >= CEPH_FRACTION_FROM:
        return "fraction, scaled (Ceph 13 or later)", True
    if max(majors) < CEPH_FRACTION_FROM:
        return "percent, as reported (Ceph 12 or earlier)", False
    return "unknown: OSDs report Ceph releases on both sides of 13", None


def _cephfs(config, ceph_reads, capacity_reads):
    """Each CephFS storage's filesystem, its data pools and the storages that share it, and those pools' usage.

    A storage with a monitor list (`monhost`) names another cluster: it is listed as external with no
    filesystem, and its monitors, subdir and path never print. A disabled storage is left out.
    """
    # The playbook reads the Ceph listing only when the plan's `ceph` flag is set (plan(): the one gate).
    # A looped read that the flag skipped carries "skipped" and is not a read.
    reads = {r.get("item"): r for r in ceph_reads if not r.get("skipped")}
    if not any(_own_cephfs(c) for c in config.values()):
        status, reason = "not read", "no CephFS storage on this cluster's own Ceph"
    elif not reads:
        status, reason = "not read", "the Ceph listing was not read"
    else:
        reason = _ceph_problem(reads)
        status = "unavailable" if reason else "read"
    listed = {}
    for item, r in reads.items():
        if r.get("status") == 200:
            data = (r.get("json") or {}).get("data")
            if not isinstance(data, list):
                raise Refusal(f"Proxmox returned no {CEPH_WHAT.get(item, item)} list; nothing was summarized.")
            listed[item] = data
    filesystems, pools = listed.get("fs", []), listed.get("pool", [])
    rows = []
    for cfg in _cephfs_configs(config):
        external = "monhost" in cfg
        if external:
            filesystem, source = None, "external"
        elif cfg.get("fs-name"):
            filesystem, source = cfg["fs-name"], "fs-name"
        elif len(filesystems) == 1 and filesystems[0].get("name"):
            filesystem, source = filesystems[0]["name"], "only filesystem"
        else:
            filesystem, source = None, "unresolved"
        entry = next((f for f in filesystems if f.get("name") == filesystem), {}) if filesystem else {}
        # data_pools arrived in pve-manager 2026-05 (FS.pm, fbb0eb7657); before it, data_pool held only the first
        # data pool, so a filesystem read that way may have more pools than the receipt names.
        complete = entry.get("data_pools") is not None if entry else None
        data_pools = entry.get("data_pools") or ([entry["data_pool"]] if entry.get("data_pool") else [])
        rows.append({"storage": cfg.get("storage"), "external": external, "filesystem": filesystem,
                     "filesystem_source": source, "data_pools": list(data_pools), "data_pools_complete": complete})
    # An unresolved storage could sit on any filesystem, so what it shares is unknown (null), and it is
    # never listed as another storage's sharer. An external storage names another cluster's filesystem,
    # so it shares none here. Resolved storages list the resolved storages on their filesystem.
    for row in rows:
        if row["external"]:
            row["shares_filesystem_with"] = []
        elif row["filesystem"] is None:
            row["shares_filesystem_with"] = None
        else:
            row["shares_filesystem_with"] = sorted(
                other["storage"] for other in rows
                if other is not row and other["filesystem"] == row["filesystem"])
    named = {pool for row in rows for pool in row["data_pools"]}
    # Proxmox sets bytes_used and percent_used only when ceph df has stats for the pool; without them both are unknown.
    # percent_used is a percent on Ceph 12 or earlier (src/mon/PGMap.cc v12.2.13 :839, used * 100) and a fraction of 1
    # on Ceph 13 or later (v13.2.0 :881, used), and Proxmox passes it on unscaled. So the release decides the scale
    # (_percent_scale, read from the OSD tree), and where that release is unknown ceph_percent_used is null.
    # bytes_used and percent_used are Ceph's own pool figures (ceph df). What they count depends on the Ceph release
    # and on whether every OSD reports per-pool stats, and Proxmox does not say which, so both keep Ceph's name in
    # the receipt and are not comparable with a storage's used_gib. size is Proxmox's
    # pool size: replicas for a replicated pool, data plus coding chunks for an erasure-coded one, so type is printed.
    scale, multiply = _percent_scale(capacity_reads)
    usage = [{"pool": p.get("pool_name"),
              "ceph_used_gib": None if p.get("bytes_used") is None else _gib(p["bytes_used"]),
              "ceph_percent_used": None if p.get("percent_used") is None or multiply is None
              else round(float(p["percent_used"]) * (100 if multiply else 1), 1),
              "type": p.get("type"), "size": p.get("size")}
             for p in sorted(pools, key=lambda p: str(p.get("pool_name"))) if p.get("pool_name") in named]
    return {"status": status, "reason": reason, "storages": rows, "pools": usage, "percent_scale": scale}


CEPH_STATUS_ITEM = "cluster/ceph/status"


def _is_osd_item(item):
    text = str(item)
    return text.startswith("nodes/") and text.endswith("/ceph/osd")


def _capacity_not_read(reason):
    return {"status": "not read", "reason": reason, "osds": [], "hosts": [], "fullest_osd": None,
            "health": None, "flags": []}


def _osd_leaves(bucket, host, out):
    """Each OSD leaf under a CRUSH bucket, with its host: the leaf's `host`, else the nearest host bucket above.

    A bucket is a dict with `type` and `children`; a non-dict child is skipped.
    """
    if not isinstance(bucket, dict):
        return out
    if bucket.get("type") == "host":
        host = bucket.get("name", host)
    if bucket.get("type") == "osd":
        out.append((bucket, bucket.get("host") or host))
    for child in bucket.get("children") or []:
        _osd_leaves(child, host, out)
    return out


def _osd_row(leaf, host):
    """One OSD's fill. Proxmox sets its usage only when Ceph reported it: absent is null, never 0."""
    total, used, percent = leaf.get("total_space"), leaf.get("bytes_used"), leaf.get("percent_used")
    return {"osd": leaf.get("id"), "name": leaf.get("name"), "host": host,
            "device_class": leaf.get("device_class"), "status": leaf.get("status"), "in": leaf.get("in"),
            "total_gib": None if total is None else _gib(total),
            "used_gib": None if used is None else _gib(used),
            # Proxmox sets this with the byte fields, as bytes_used * 100 / total_space, so it is a percent, not
            # Ceph's own fraction (pve-manager PVE/API2/Ceph/OSD.pm index, default branch, read 2026-11-17).
            "percent_used": None if percent is None else round(float(percent), 1),
            "version": leaf.get("ceph_version_short")}


def _flags(text):
    """The cluster's OSD flags as a list: the OSD tree carries them as one comma-joined string, absent when
    none is set (pve-manager PVE/API2/Ceph/OSD.pm index).
    """
    if not isinstance(text, str):
        return []
    return [flag.strip() for flag in text.split(",") if flag.strip()]


def _health(detail):
    """The cluster's health status and its check codes with their severity. Never a check's message."""
    if not isinstance(detail, dict):
        return {"status": None, "checks": None}
    checks = detail.get("checks")
    rows = None if not isinstance(checks, dict) else [
        {"code": code, "severity": value.get("severity") if isinstance(value, dict) else None}
        for code, value in sorted(checks.items())]
    return {"status": detail.get("status"), "checks": rows}


def _ceph_capacity(config, reads):
    """Each Ceph OSD's fill and host, and the cluster's health status and check codes.

    The reads are the OSD tree (`nodes/<node>/ceph/osd`) and the status (`cluster/ceph/status`), gated
    like the CephFS listing and read on this cluster's own Ceph only. A host's totals are null when any
    of its OSDs reports no usage, so a partial sum never reads as the whole.
    """
    if not any(_own_cephfs(c) for c in config.values()):
        return _capacity_not_read("no CephFS storage on this cluster's own Ceph")
    osd_read = next((r for r in reads if not r.get("skipped") and _is_osd_item(r.get("item"))), None)
    status_read = next((r for r in reads if not r.get("skipped") and r.get("item") == CEPH_STATUS_ITEM), None)
    if osd_read is None or status_read is None:
        return _capacity_not_read("the Ceph OSD tree and health were not read")
    reason = _ceph_problem({"osd": osd_read, "status": status_read})
    osds, hosts, flags, fullest_osd, health = [], [], [], None, None
    if osd_read.get("status") == 200:
        tree = (osd_read.get("json") or {}).get("data")
        root = tree.get("root") if isinstance(tree, dict) else None
        if not isinstance(root, dict):
            raise Refusal("Proxmox returned no Ceph OSD tree; nothing was summarized.")
        flags = _flags(tree.get("flags"))
        leaves = _osd_leaves(root, None, [])
        osds = sorted((_osd_row(leaf, host) for leaf, host in leaves), key=lambda row: row["osd"])
        sums = {}
        for leaf, host in leaves:
            entry = sums.setdefault(host, {"osds": 0, "total": 0, "used": 0, "complete": True})
            total, used = leaf.get("total_space"), leaf.get("bytes_used")
            entry["osds"] += 1
            if total is None or used is None:
                entry["complete"] = False
            else:
                entry["total"] += total
                entry["used"] += used
        for host in sorted(sums, key=lambda h: (h is None, h or "")):
            entry = sums[host]
            full = entry["complete"] and entry["total"] > 0
            hosts.append({"host": host, "osds": entry["osds"],
                          "total_gib": _gib(entry["total"]) if entry["complete"] else None,
                          "used_gib": _gib(entry["used"]) if entry["complete"] else None,
                          "percent_used": round(100 * entry["used"] / entry["total"], 1) if full else None})
        # Chosen on the unrounded fill, so two OSDs that both print as 75.0 still pick the fuller one.
        known = [(float(leaf["percent_used"]), leaf.get("id"), host) for leaf, host in leaves
                 if leaf.get("percent_used") is not None]
        if known:
            percent, osd, host = max(known, key=lambda k: (k[0], -k[1]))
            fullest_osd = {"osd": osd, "host": host, "percent_used": round(percent, 1)}
    if status_read.get("status") == 200:
        data = (status_read.get("json") or {}).get("data")
        if not isinstance(data, dict):
            raise Refusal("Proxmox returned no Ceph status; nothing was summarized.")
        health = _health(data.get("health"))
    return {"status": "unavailable" if reason else "read", "reason": reason, "osds": osds, "hosts": hosts,
            "fullest_osd": fullest_osd, "health": health, "flags": flags}


def _disks(disk_reads):
    """Each online node's disks as Proxmox lists them.

    A disk is in use exactly when Proxmox sets `used` on it (pve-storage Diskmanage.pm disk_is_used,
    master branch). WWN and SMART health are never printed. A node whose disk list the token
    may not read is unavailable.
    """
    nodes = []
    for read in disk_reads:
        node = _name(read.get("item"), "node name")
        if read.get("status") == 403:
            nodes.append({"node": node, "status": "unavailable",
                          "reason": f"HTTP 403: needs Sys.Audit on /nodes/{node}",
                          "disks": [], "unused_count": None, "unused_gib": None})
            continue
        if read.get("status") != 200:
            raise Refusal(f"Proxmox answered {read.get('status')} listing the disks of node {node}.")
        raw = (read.get("json") or {}).get("data")
        if not isinstance(raw, list):
            raise Refusal(f"Proxmox returned no disk list for node {node}; nothing was summarized.")
        # Each disk's in-use flag is computed once here; the unused totals derive from the same flag.
        flagged = sorted(((d, bool(d.get("used"))) for d in raw), key=lambda pair: str(pair[0].get("devpath")))
        unused = [d for d, in_use in flagged if not in_use]
        nodes.append({
            "node": node, "status": "read", "reason": None,
            "disks": [{"devpath": d.get("devpath"), "size_gib": _gib(d.get("size")), "model": d.get("model"),
                       "vendor": d.get("vendor"), "serial": d.get("serial"), "used": d.get("used") or None,
                       "in_use": in_use, "osdid": d.get("osdid")}
                      for d, in_use in flagged],
            "unused_count": len(unused), "unused_gib": _gib(sum(int(d.get("size") or 0) for d in unused)),
        })
    return sorted(nodes, key=lambda n: n["node"])


def _vzdump_defaults(reads):
    """Each online node's vzdump defaults as Proxmox answers them: (receipt rows, {node: remove}).

    Only `remove` is read out of a defaults response, and only it reaches the receipt. A node whose defaults
    are not read, or that answers 403 or 500, has an unknown `remove` (null), never an assumed one. Absent
    reads mean no node was read, so the receipt has no rows and every lookup is unknown.
    """
    rows = []
    for read in reads or []:
        node = _name(read.get("item"), "node name")
        status = read.get("status")
        if status == 403:
            rows.append({"node": node, "status": "unavailable", "remove": None,
                         "reason": "HTTP 403: needs Datastore.Audit or Datastore.AllocateSpace on the node's default "
                                   "backup storage"})
        elif status == 500:
            rows.append({"node": node, "status": "unavailable", "remove": None, "reason": "HTTP 500"})
        elif status != 200:
            raise Refusal(f"Proxmox answered {status} reading the vzdump defaults of node {node}.")
        else:
            body = (read.get("json") or {}).get("data")
            if not isinstance(body, dict):
                raise Refusal(f"Proxmox returned no vzdump defaults for node {node}; nothing was summarized.")
            # A release that leaves `remove` out reads as unknown, not off
            # (pve-manager PVE/VZDump.pm:312-340 at e5ac6d6f).
            rows.append({"node": node, "status": "read", "reason": None, "remove": _remove(body.get("remove"))})
    rows.sort(key=lambda row: row["node"])
    return rows, {row["node"]: row["remove"] for row in rows}


def _removes(job, node, node_remove):
    """Whether Proxmox prunes for a job on a guest living on `node`: True, False, or None when that is not known.

    A job's own `remove` decides. A job without one takes its node's vzdump default (pve-manager
    PVE/VZDump.pm:597-606 at e5ac6d6f), which is None when that node's defaults were not read.
    """
    if "remove" in job:
        return _remove(job["remove"])
    return node_remove.get(node)


def summarize(data):
    existing = {int(v["vmid"]): v for v in data["vms"] if _is_vmid(v.get("vmid", ""))}
    names = {vmid: v.get("name") for vmid, v in existing.items()}
    config = {c.get("storage"): c for c in data["storage_config"]}
    jobs = [j for j in data["jobs"] if j.get("type", "vzdump") == "vzdump"]
    job_guests = [(job, *_job_guests(job, existing)) for job in jobs]  # (job, selects, guests)
    node_rows, node_remove = _vzdump_defaults(data.get("vzdump_defaults_reads"))
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
        # A guest's retentions are those of the enabled vzdump jobs on this storage that back it up. A job backs
        # up and prunes a guest on the node it lives on, so on a node-local storage only the guests
        # living on that node take a retention. A guest that no longer exists takes none: its backup fails with
        # "unable to find VM" (pve-manager PVE/API2/VZDump.pm:101-106 at e5ac6d6f, PVE/VZDump.pm:907-908 at e5ac6d6f),
        # so no job of this cluster prunes it.
        retention_of, protects_every_archive, unpruned = {}, set(), set()
        for job, _, guests in job_guests:
            if job.get("storage") == storage and _enabled(job):
                if not shared:
                    guests = {vmid for vmid in guests if existing.get(vmid, {}).get("node") == node}
                guests = {vmid for vmid in guests if vmid in existing}
                for vmid in guests:
                    retention_of.setdefault(vmid, set()).add(_retention(job))
                    # A job that does not prune (remove off, or not known) makes its guest's need unknown, not prunable.
                    if _removes(job, existing[vmid].get("node"), node_remove) is not True:
                        unpruned.add(vmid)
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
            # Prunable only when every retention backing this guest was previewed here and removes it, and no job
            # backing it up is one that does not prune.
            wanted = retention_of.get(vmid, set())
            if vmid not in unpruned and wanted and all(volid in previewed.get(x, set()) for x in wanted):
                g["prunable"] += 1
                g["prunable_bytes"] += size
        # A protected archive is kept on top of what keep-* keeps (pve-storage marks it protected,
        # and prune_mark_backup_group counts only unmarked archives),
        # so the need adds its bytes; a guest whose job protects every archive has no bound.
        needs, need_bytes = {}, {}
        for vmid, g in by_vmid.items():
            if vmid in protects_every_archive or vmid in unpruned:
                needs[vmid] = None
            elif vmid not in retention_of:  # no enabled job backs it up here, so nothing prunes it: every archive stays
                needs[vmid], need_bytes[vmid] = g["archives"], g["bytes"]
            else:
                needs[vmid] = _archives_kept(retention_of[vmid], cfg)
                if needs[vmid] is not None:
                    need_bytes[vmid] = g["newest_bytes"] * needs[vmid] + g["protected_bytes"]
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

    cephfs = _cephfs(config, data.get("ceph_reads", []), data.get("ceph_capacity_reads", []))
    cephfs_rows = {row["storage"]: row for row in cephfs["storages"]}
    shared_backends = []
    for (a, (a_total, a_used, a_files)), (b, (b_total, b_used, b_files)) in combinations(measured, 2):
        # Node-local storages on different nodes are separate disks, whatever their paths say.
        if a["read_on_node"] != b["read_on_node"] and not (a["shared"] or b["shared"]):
            continue
        row_a, row_b = cephfs_rows.get(a["storage"]), cephfs_rows.get(b["storage"])
        if row_a and row_b:
            # Two CephFS storages: the cephfs section decides, never the byte counts. A known filesystem name
            # on both says same or different; with either unresolved, sharing is unknown there, so no pair here.
            # Whether two subdirectories of one filesystem overlap is unknown, so the combined figure is null.
            if row_a["filesystem"] is not None and row_a["filesystem"] == row_b["filesystem"]:
                shared_backends.append({"storages": [a["storage"], b["storage"]],
                                        "reason": "same CephFS filesystem", "combined_unaccounted_gib": None})
            continue
        reason = _same_backend(config.get(a["storage"], {}), config.get(b["storage"], {}))
        if not reason and a_total and a_total == b_total and a_used == b_used:
            reason = "identical total and used bytes (inferred, not proven)"
        if reason:
            if config.get(a["storage"], {}).get("content-dirs") or config.get(b["storage"], {}).get("content-dirs"):
                # content-dirs can move any content type's directory, and the listing counts every type,
                # so which files the two storages share cannot be told.
                listed = None
            elif reason in ("same server and export", "same path"):
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
        job_rows.append({"id": job.get("id"), "enabled": _enabled(job),
                         "schedule": job.get("schedule") or job.get("starttime"),
                         "node": job.get("node") or "all", "storage": job.get("storage"),
                         "retention": _retention(job), "mode": job.get("mode"),
                         "selects": selects, "guests": len(guests),
                         # only a vmid list can name a guest that is gone
                         "missing_guests": sorted(v for v in guests if v not in names),
                         # the job's own remove; a job without one takes its node's default, which the receipt names
                         "remove": _remove(job["remove"]) if "remove" in job else "node default"})
    return {"storages": storages, "shared_backends": shared_backends, "jobs": job_rows,
            "guests": _guests(storages),
            "cephfs": cephfs,
            "ceph_capacity": _ceph_capacity(config, data.get("ceph_capacity_reads", [])),
            "disks": _disks(data.get("disk_reads", [])),
            "vzdump_defaults": node_rows}


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
