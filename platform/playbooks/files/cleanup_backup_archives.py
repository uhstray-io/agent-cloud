#!/usr/bin/env python3
"""Decide, authorize and verify the Proxmox backup-archive cleanup.

Three modes, JSON on stdin, JSON on stdout:

  list       {nodes, vms, jobs, storage_config, plan, content_reads, pairs, confirm_delete, check_mode}
             -> the archives the cleanup deletes, their sha256, what it keeps, and `delete`.
             Refused whole while any node is not online. Two sources only, backup volumes only:
               gone      an archive whose vmid is not a guest of this cluster, on a storage no other
                         cluster can write to (node-local, or CephFS on this cluster's own Ceph);
               declared  an archive matching a declared (vmid, storage) pair, refused when an enabled
                         vzdump job on that storage still backs up that guest.
             A protected archive, an archive with no vmid, and every other archive are never listed,
             only counted. `delete` is true only for a real run (not check mode) whose list is
             non-empty and whose confirm_delete equals the list's sha256; a real run with any other
             confirm_delete is refused.
  authorize  {volumes, permission_reads} -> refused unless the token holds Datastore.AllocateSpace
             on /storage/<storage> and VM.Backup on /vms/<vmid> for every listed archive: the
             privileges Proxmox checks to delete a backup volume that has an owner vmid.
  verify     {volumes, before, after} -> refused if a listed archive is still present, or an archive
             not listed disappeared between the two content reads.

The sha256 covers each archive's volid, size and ctime, never the node it was read through, so a
shared storage read via another node on the next run gives the same value.
A refusal exits 1 with {"refusal": "..."} so the play can print why without the input.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

# Load the survey helper from its file: `python3 -I` does not put this directory on sys.path.
_spec = importlib.util.spec_from_file_location("survey_backup_storage",
                                               Path(__file__).with_name("survey_backup_storage.py"))
survey = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(survey)

Refusal = survey.Refusal


def _is_backup(volume):
    return volume.get("content") == "backup" or ":backup/" in str(volume.get("volid"))


def _read_data(read, storage):
    if read.get("status") == 403:
        raise Refusal(f"The Proxmox token may not list the content of storage {storage} (needs Datastore.Audit there).")
    if read.get("status") != 200:
        raise Refusal(f"Proxmox answered {read.get('status')} listing the content of storage {storage}.")
    return (read.get("json") or {}).get("data") or []


def _backups(content_reads):
    """(node, storage, volume) for every backup volume in raw content reads."""
    out = []
    for read in content_reads:
        node, storage = read["item"]["node"], read["item"]["storage"]
        out.extend((node, storage, v) for v in _read_data(read, storage) if _is_backup(v))
    return out


def _pairs(raw, storages):
    """The declared (vmid, storage) pairs, validated; a set of (int, str)."""
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise Refusal("proxmox_backup_cleanup_pairs must be a list of {vmid, storage}.")
    pairs = set()
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"vmid", "storage"}:
            raise Refusal("Each proxmox_backup_cleanup_pairs entry must have exactly the keys vmid and storage.")
        vmid = entry["vmid"]
        if isinstance(vmid, str) and survey._is_vmid(vmid):
            vmid = int(vmid)
        if isinstance(vmid, bool) or not isinstance(vmid, int) or vmid <= 0:
            raise Refusal(f"proxmox_backup_cleanup_pairs has a vmid that is not a positive integer: {vmid!r}.")
        storage = entry["storage"]
        if not isinstance(storage, str) or not survey.NAME.match(storage):
            raise Refusal("proxmox_backup_cleanup_pairs has a malformed storage ID.")
        if storage not in storages:
            raise Refusal(f"Declared storage {storage} is not an active backup storage this run read.")
        pairs.add((vmid, storage))
    return pairs


def _sha(volumes):
    rows = sorted([v["volid"], int(v["size"]), int(v["ctime"])] for v in volumes)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _vmid(volume):
    value = volume.get("vmid")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, str) and survey._is_vmid(value)):
        return int(value)
    return None


def list_archives(data):
    existing = {int(v["vmid"]): v for v in data["vms"] if survey._is_vmid(v.get("vmid", ""))}
    storages = {r["storage"] for r in data["plan"]["reads"]}
    pairs = _pairs(data.get("pairs"), storages)
    for job in data["jobs"]:
        if job.get("type", "vzdump") != "vzdump" or not survey._enabled(job):
            continue
        _, guests = survey._job_guests(job, existing)
        for vmid, storage in sorted(pairs):
            if job.get("storage") == storage and vmid in guests:
                raise Refusal(f"Enabled backup job {job.get('id')} still backs up guest {vmid} "
                              f"to storage {storage}; disable it or drop the declared pair.")
    offline = [f"{n.get('node')} is {n.get('status')}" for n in data["nodes"] if n.get("status") != "online"]
    if offline:
        raise Refusal(f"Node {', '.join(offline)}: no archive is listed while a node is not online "
                      "(guest existence is not trusted and its local storages are not read).")

    # A guest missing from this cluster is gone only on a storage no other cluster can write to:
    # a node-local one, or CephFS on this cluster's own Ceph. On any other shared storage (NFS, CIFS,
    # PBS, an external CephFS) the vmid may be another cluster's guest, so it is kept unless declared.
    config = {c.get("storage"): c for c in data.get("storage_config") or []}
    # Unknown counts as shared: a status with no `shared` field is not taken as node-local.
    own = {r["storage"] for r in data["plan"]["reads"]
           if (r.get("status") or {}).get("shared") in (0, "0", False)
           or survey._own_cephfs(config.get(r["storage"], {}))}
    listed, kept = [], {}
    for node, storage, v in _backups(data["content_reads"]):
        tally = kept.setdefault(storage, {"protected": [0, 0], "no_vmid": [0, 0], "gone_on_shared": [0, 0],
                                          "kept": [0, 0]})
        vmid = _vmid(v)
        size = int(v.get("size") or 0)
        source = None
        if vmid is not None:
            if (vmid, storage) in pairs:
                source = "declared"
            elif vmid not in existing and storage in own:
                source = "gone"
        if vmid is None:
            bucket = "no_vmid"
        elif source and survey._flag(v.get("protected")):
            bucket = "protected"
        elif source:
            listed.append({"volid": v["volid"], "storage": storage, "node": node, "vmid": vmid,
                           "guest": existing.get(vmid, {}).get("name"), "source": source,
                           "size_gib": survey._gib(size), "date": survey._day(v.get("ctime")),
                           "size": size, "ctime": int(v.get("ctime") or 0)})
            continue
        elif vmid not in existing:
            bucket = "gone_on_shared"
        else:
            bucket = "kept"
        tally[bucket][0] += 1
        tally[bucket][1] += size
    listed.sort(key=lambda e: (e["storage"], e["vmid"], e["ctime"], e["volid"]))
    sha = _sha(listed)
    confirm = data.get("confirm_delete") or ""
    check_mode = bool(data.get("check_mode"))
    if listed and not check_mode and confirm != sha:
        raise Refusal(f"confirm_delete does not match this list; its sha256 is {sha}. "
                      "Dry-run first and confirm that value.")
    volumes = [{k: e[k] for k in ("volid", "storage", "node", "vmid", "guest", "source", "size_gib", "date")}
               for e in listed]
    acl_paths = sorted({f"/storage/{e['storage']}" for e in listed} | {f"/vms/{e['vmid']}" for e in listed})
    return {"volumes": volumes, "count": len(volumes),
            "total_gib": survey._gib(sum(e["size"] for e in listed)), "sha256": sha,
            "not_listed": {s: {k: {"count": c, "gib": survey._gib(b)} for k, (c, b) in t.items()}
                           for s, t in sorted(kept.items())},
            "acl_paths": acl_paths,
            "delete": bool(listed) and not check_mode and confirm == sha}


def authorize(data):
    privileges = {}
    for read in data["permission_reads"]:
        path = read.get("item")
        if read.get("status") != 200:
            raise Refusal(f"Proxmox answered {read.get('status')} reading the token's permissions on {path}.")
        privileges[path] = ((read.get("json") or {}).get("data") or {}).get(path) or {}
    missing = []
    for v in data["volumes"]:
        for priv, path in (("Datastore.AllocateSpace", f"/storage/{v['storage']}"),
                           ("VM.Backup", f"/vms/{v['vmid']}")):
            if path not in privileges:
                raise Refusal(f"The token's permissions on {path} were not read.")
            # A privilege is held when its key is present: the value is only its propagate flag
            # (pve-access-control RPCEnvironment.pm `check`: `defined($perm->{$priv})`).
            if privileges[path].get(priv) is None:
                text = f"{priv} on {path}"
                if text not in missing:
                    missing.append(text)
    if missing:
        raise Refusal(f"The Proxmox token cannot delete these archives: missing {'; '.join(missing)}.")
    return {"authorized": len(data["volumes"])}


def verify(data):
    listed = {v["volid"] for v in data["volumes"]}
    before = {v["volid"] for _, _, v in _backups(data["before"])}
    after = {v["volid"] for _, _, v in _backups(data["after"])}
    present = sorted(listed & after)
    if present:
        raise Refusal(f"Listed archives are still present: {', '.join(present)}.")
    vanished = sorted((before - listed) - after)
    if vanished:
        raise Refusal(f"Archives that were not listed disappeared: {', '.join(vanished)}.")
    return {"deleted": len(listed), "new_archives": len(after - before)}


def main(argv=None, stdin=sys.stdin, stdout=sys.stdout):
    argv = sys.argv[1:] if argv is None else argv
    modes = {"list": list_archives, "authorize": authorize, "verify": verify}
    try:
        mode = argv[0] if argv else ""
        if mode not in modes:
            raise Refusal("usage: cleanup_backup_archives.py list|authorize|verify < input.json")
        result = modes[mode](json.load(stdin))
    except Refusal as exc:
        json.dump({"refusal": str(exc)}, stdout)
        return 1
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
        json.dump({"refusal": "Proxmox returned data this cleanup does not recognise."}, stdout)
        return 1
    json.dump(result, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
