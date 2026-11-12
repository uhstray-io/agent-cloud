"""The backup-storage survey plans its reads and summarizes them without leaking backend details."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

import survey_backup_storage as survey  # noqa: E402

GIB = 1024 ** 3
DAY = 86400
T0 = 1_790_000_000


def _status(total, used, shared=1):
    return {"type": "nfs", "shared": shared, "total": total, "used": used, "avail": total - used}


def _listing(node):
    return {"item": node, "status": 200, "json": {"data": [
        {"storage": "backups", "content": "backup", "active": 1, **_status(100 * GIB, 100 * GIB)},
        {"storage": "isos", "content": "iso,backup", "active": 1, **_status(100 * GIB, 100 * GIB)},
        {"storage": "local-lvm", "content": "images", "active": 1, **_status(50 * GIB, 1 * GIB, 0)},
    ]}}


NODES = [{"node": "n1", "status": "online"}, {"node": "n2", "status": "online"},
         {"node": "n3", "status": "offline"}]
JOBS = [
    {"id": "main", "type": "vzdump", "storage": "backups", "prune-backups": "keep-last=8",
     "vmid": "200,201,205", "schedule": "03:00", "enabled": 1},
    {"id": "game", "type": "vzdump", "storage": "backups", "prune-backups": {"keep-last": "10"},
     "vmid": "103", "schedule": "02:00"},
]


def _archive(store, vmid, day, size):
    return {"volid": f"{store}:backup/vzdump-qemu-{vmid}-{day}.vma.zst", "vmid": vmid,
            "size": size, "ctime": T0 + day * DAY, "format": "vma.zst", "notes": "private note"}


def _payload():
    plan = survey.plan({"nodes": NODES, "jobs": JOBS, "storage_listings": [_listing("n1"), _listing("n2")]})
    backups = [_archive("backups", 200, d, 10 * GIB) for d in range(3)]
    backups += [_archive("backups", 205, d, 20 * GIB) for d in range(2)]  # guest 205 was destroyed
    backups.append(_archive("backups", 103, 0, 5 * GIB))
    isos = [_archive("isos", 200, d, 5 * GIB) for d in range(4)]
    isos.append({"volid": "isos:iso/ubuntu.iso", "size": 2 * GIB, "ctime": T0})
    prune_remove = {"volid": backups[0]["volid"], "mark": "remove", "ctime": T0, "vmid": 200, "type": "qemu"}
    reads = {r["storage"]: r for r in plan["reads"]}
    return {
        "plan": plan,
        "vms": [{"vmid": 200, "name": "semaphore"}, {"vmid": 201, "name": "caddy"},
                {"vmid": 103, "name": "game"}],
        "storage_config": [
            {"storage": "backups", "type": "nfs", "server": "192.0.2.10", "export": "/tank"},
            {"storage": "isos", "type": "nfs", "server": "192.0.2.10", "export": "/tank"},
        ],
        "jobs": JOBS,
        "content_reads": [
            {"item": reads["backups"], "status": 200, "json": {"data": backups}},
            {"item": reads["isos"], "status": 200, "json": {"data": isos}},
        ],
        "prune_reads": [
            {"item": [reads["backups"], "keep-last=10"], "status": 200, "json": {"data": [prune_remove]}},
            {"item": [reads["backups"], "keep-last=8"], "status": 200, "json": {"data": []}},
            {"item": [reads["isos"], "storage-default"], "status": 200, "json": {"data": []}},
        ],
    }


def test_plan_reads_each_shared_backup_storage_once_with_each_job_retention():
    plan = survey.plan({"nodes": NODES, "jobs": JOBS, "storage_listings": [_listing("n1"), _listing("n2")]})
    assert plan["online_nodes"] == ["n1", "n2"]
    assert [(r["node"], r["storage"], r["retentions"]) for r in plan["reads"]] == [
        ("n1", "backups", ["keep-last=10", "keep-last=8"]),
        ("n1", "isos", ["storage-default"]),
    ]


def test_summary_explains_where_the_space_went():
    result = survey.summarize(_payload())
    backups = next(s for s in result["storages"] if s["storage"] == "backups")
    assert backups["used_percent"] == 100.0
    assert backups["backup_archives"] == 6 and backups["backup_gib"] == 75.0
    assert backups["orphan_guest_gib"] == 40.0  # guest 205 no longer exists
    assert backups["unaccounted_gib"] == 25.0   # 100 used - 75 listed
    assert backups["prunable_archives"] == 1 and backups["prunable_gib"] == 10.0
    # keep-last=8 for 200 and 205 at their newest size, keep-last=10 for 103
    assert backups["gib_needed_at_job_retention"] == 8 * 10 + 8 * 20 + 10 * 5
    guest = {g["vmid"]: g for g in backups["by_guest"]}
    assert guest[205]["guest_exists"] is False and guest[205]["name"] is None
    assert guest[103]["job_keep_last"] == 10 and guest[200]["prunable"] == 1

    assert result["shared_backends"] == [{"storages": ["backups", "isos"], "reason": "same server and export",
                                          "combined_unaccounted_gib": 100 - 75 - 22.0}]
    jobs = {j["id"]: j for j in result["jobs"]}
    assert jobs["main"]["missing_guests"] == [205] and jobs["game"]["retention"] == "keep-last=10"


def test_summary_never_carries_backend_addresses_volume_ids_or_notes():
    text = json.dumps(survey.summarize(_payload()))
    for secretish in ("192.0.2.10", "/tank", ":backup/", "private note", "vzdump-qemu"):
        assert secretish not in text


def test_a_storage_the_token_may_not_read_is_refused_by_name():
    payload = _payload()
    payload["content_reads"][1]["status"] = 403
    out = io.StringIO()
    assert survey.main(["summarize"], stdin=io.StringIO(json.dumps(payload)), stdout=out) == 1
    assert "storage isos" in json.loads(out.getvalue())["refusal"]
    assert "Datastore.Audit" in json.loads(out.getvalue())["refusal"]


def test_a_malformed_node_name_is_refused_before_any_url_is_built():
    listing = _listing("n1/../x")
    out = io.StringIO()
    data = {"nodes": NODES, "jobs": JOBS, "storage_listings": [listing]}
    assert survey.main(["plan"], stdin=io.StringIO(json.dumps(data)), stdout=out) == 1
    assert "node name" in json.loads(out.getvalue())["refusal"]
