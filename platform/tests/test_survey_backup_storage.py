"""The backup-storage survey plans its reads and summarizes them without leaking backend details."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

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
STORAGE_CONFIG = [
    {"storage": "backups", "type": "nfs", "server": "192.0.2.10", "export": "/tank"},
    {"storage": "isos", "type": "nfs", "server": "192.0.2.10", "export": "/tank"},
]


def _archive(store, vmid, day, size):
    return {"volid": f"{store}:backup/vzdump-qemu-{vmid}-{day}.vma.zst", "vmid": vmid,
            "size": size, "ctime": T0 + day * DAY, "format": "vma.zst", "notes": "private note"}


def _mark(archive):
    """A prune-preview row that would remove this archive."""
    return {"volid": archive["volid"], "mark": "remove", "ctime": archive["ctime"],
            "vmid": archive["vmid"], "type": "qemu"}


def _payload():
    plan = survey.plan({"nodes": NODES, "jobs": JOBS, "storage_listings": [_listing("n1"), _listing("n2")],
                        "storage_config": STORAGE_CONFIG})
    backups = [_archive("backups", 200, d, 10 * GIB) for d in range(3)]
    backups += [_archive("backups", 205, d, 20 * GIB) for d in range(2)]  # guest 205 was destroyed
    backups.append(_archive("backups", 103, 0, 5 * GIB))
    isos = [_archive("isos", 200, d, 5 * GIB) for d in range(4)]
    isos.append({"volid": "isos:iso/ubuntu.iso", "size": 2 * GIB, "ctime": T0})
    reads = {r["storage"]: r for r in plan["reads"]}
    return {
        "plan": plan,
        "vms": [{"vmid": 200, "name": "semaphore"}, {"vmid": 201, "name": "caddy"},
                {"vmid": 103, "name": "game"}],
        "storage_config": STORAGE_CONFIG,
        "jobs": JOBS,
        "content_reads": [
            {"item": reads["backups"], "status": 200, "json": {"data": backups}},
            {"item": reads["isos"], "status": 200, "json": {"data": isos}},
        ],
        "prune_reads": [
            # keep-last=10 belongs to job game (guest 103) only; it would remove guest 200's second archive.
            {"item": [reads["backups"], "keep-last=10"], "status": 200, "json": {"data": [_mark(backups[1])]}},
            # keep-last=8 is job main (guests 200, 201, 205) and removes guest 200's oldest archive.
            {"item": [reads["backups"], "keep-last=8"], "status": 200, "json": {"data": [_mark(backups[0])]}},
        ],
    }


def _one_store(jobs, vms, archives, storage_config=(), previews=None):
    """Plan and summarize one shared backup storage, `backups`, read through node n1."""
    listing = {"item": "n1", "status": 200, "json": {"data": [
        {"storage": "backups", "content": "backup", "active": 1, **_status(100 * GIB, 10 * GIB)}]}}
    plan = survey.plan({"nodes": NODES, "jobs": jobs, "storage_listings": [listing],
                        "storage_config": list(storage_config)})
    read = plan["reads"][0]
    summary = {"plan": plan, "vms": vms, "storage_config": list(storage_config), "jobs": jobs,
               "content_reads": [{"item": read, "status": 200, "json": {"data": archives}}],
               "prune_reads": [{"item": [read, retention], "status": 200, "json": {"data": rows}}
                               for retention, rows in (previews or {}).items()]}
    return plan, survey.summarize(summary)


def test_plan_reads_each_shared_backup_storage_once_with_each_job_retention():
    plan = survey.plan({"nodes": NODES, "jobs": JOBS, "storage_listings": [_listing("n1"), _listing("n2")],
                        "storage_config": STORAGE_CONFIG})
    assert plan["online_nodes"] == ["n1", "n2"]
    assert [(r["node"], r["storage"], r["retentions"]) for r in plan["reads"]] == [
        ("n1", "backups", ["keep-last=10", "keep-last=8"]),
        ("n1", "isos", []),  # no job, and the storage sets no prune-backups: nothing to preview
    ]


def test_summary_explains_where_the_space_went():
    result = survey.summarize(_payload())
    backups = next(s for s in result["storages"] if s["storage"] == "backups")
    assert backups["used_percent"] == 100.0
    assert backups["backup_archives"] == 6 and backups["backup_gib"] == 75.0
    assert backups["orphan_guest_gib"] == 40.0  # guest 205 no longer exists
    assert backups["unaccounted_gib"] == 25.0   # 100 used - 75 listed
    # keep-last=10 would remove archive 1 of guest 200, but guest 200's job keeps it at keep-last=8
    assert backups["prunable_archives"] == 1 and backups["prunable_gib"] == 10.0
    # keep-last=8 for 200 and 205 at their newest size, keep-last=10 for 103
    assert backups["gib_needed_at_job_retention"] == 8 * 10 + 8 * 20 + 10 * 5
    guest = {g["vmid"]: g for g in backups["by_guest"]}
    assert guest[205]["guest_exists"] is False and guest[205]["name"] is None
    assert guest[103]["retained_at_job_retention"] == 10 and guest[103]["job_retention"] == ["keep-last=10"]
    assert guest[200]["prunable"] == 1 and guest[200]["job_retention"] == ["keep-last=8"]
    assert backups["retention_need_unknown_guests"] == []

    assert result["shared_backends"] == [{"storages": ["backups", "isos"], "reason": "same server and export",
                                          "combined_unaccounted_gib": 100 - 75 - 22.0}]
    jobs = {j["id"]: j for j in result["jobs"]}
    assert jobs["main"]["missing_guests"] == [205] and jobs["game"]["retention"] == "keep-last=10"
    assert jobs["main"]["selects"] == "list" and jobs["main"]["guests"] == 3


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


def test_a_prune_preview_the_token_may_not_read_names_the_privilege_proxmox_checks():
    payload = _payload()
    payload["prune_reads"][0]["status"] = 403
    out = io.StringIO()
    assert survey.main(["summarize"], stdin=io.StringIO(json.dumps(payload)), stdout=out) == 1
    assert "Datastore.Allocate" in json.loads(out.getvalue())["refusal"]


def test_a_malformed_node_name_is_refused_before_any_url_is_built():
    listing = _listing("n1/../x")
    out = io.StringIO()
    data = {"nodes": NODES, "jobs": JOBS, "storage_listings": [listing]}
    assert survey.main(["plan"], stdin=io.StringIO(json.dumps(data)), stdout=out) == 1
    assert "node name" in json.loads(out.getvalue())["refusal"]


def test_a_pool_job_backs_up_every_guest_in_its_pool():
    vms = [{"vmid": 301, "name": "a", "node": "n1", "pool": "prod"},
           {"vmid": 302, "name": "b", "node": "n2", "pool": "prod"},
           {"vmid": 303, "name": "c", "node": "n1", "pool": "dev"}]
    jobs = [{"id": "pool", "type": "vzdump", "storage": "backups", "prune-backups": "keep-last=2",
             "pool": "prod"}]
    archives = [_archive("backups", vmid, 0, GIB) for vmid in (301, 302, 303)]
    _, result = _one_store(jobs, vms, archives)
    assert result["jobs"][0]["selects"] == "pool" and result["jobs"][0]["guests"] == 2
    assert result["jobs"][0]["missing_guests"] == []
    guest = {g["vmid"]: g for g in result["storages"][0]["by_guest"]}
    assert guest[301]["retained_at_job_retention"] == 2 and guest[302]["retained_at_job_retention"] == 2
    assert guest[303]["retained_at_job_retention"] == 0 and guest[303]["job_retention"] == []


def test_an_all_job_takes_its_node_and_leaves_out_the_excluded_guests():
    vms = [{"vmid": 200, "node": "n1"}, {"vmid": 201, "node": "n1"},
           {"vmid": 202, "node": "n2"}, {"vmid": 203, "node": "n1"}]
    jobs = [{"id": "all", "type": "vzdump", "storage": "backups", "all": 1, "exclude": "201",
             "node": "n1", "prune-backups": "keep-last=3"}]
    archives = [_archive("backups", vmid, 0, GIB) for vmid in (200, 201, 202, 203)]
    _, result = _one_store(jobs, vms, archives)
    assert result["jobs"][0]["selects"] == "all" and result["jobs"][0]["guests"] == 2
    guest = {g["vmid"]: g["retained_at_job_retention"] for g in result["storages"][0]["by_guest"]}
    assert guest == {200: 3, 201: 0, 202: 0, 203: 3}


@pytest.mark.parametrize("separator", [",", ";", " "])
def test_guest_lists_split_on_every_delimiter_proxmox_accepts(separator):
    vms = [{"vmid": v, "node": "n1"} for v in (200, 201, 202)]
    jobs = [{"id": "list", "type": "vzdump", "storage": "backups", "vmid": f"200{separator}201",
             "prune-backups": "keep-last=2"},
            {"id": "all", "type": "vzdump", "storage": "other", "all": 1, "exclude": f"200{separator}201"}]
    archives = [_archive("backups", vmid, 0, GIB) for vmid in (200, 201, 202)]
    _, result = _one_store(jobs, vms, archives)
    rows = {j["id"]: j for j in result["jobs"]}
    assert rows["list"]["selects"] == "list" and rows["list"]["guests"] == 2
    assert rows["all"]["guests"] == 1  # only 202 is left once both excluded guests are read


@pytest.mark.parametrize("value", [1, "1", True])
def test_a_proxmox_flag_reads_as_set_however_the_api_spells_it(value):
    assert survey._flag(value) and not survey._flag(0) and not survey._flag("0") and not survey._flag(None)


def test_protected_archives_add_to_the_need_and_a_protecting_job_has_no_bound():
    vms = [{"vmid": 701, "node": "n1"}, {"vmid": 702, "node": "n1"}]
    jobs = [{"id": "keep2", "type": "vzdump", "storage": "backups", "vmid": "701", "prune-backups": "keep-last=2"},
            {"id": "prot", "type": "vzdump", "storage": "backups", "vmid": "702", "prune-backups": "keep-last=2",
             "protected": 1}]
    archives = [{**_archive("backups", 701, day, 10 * GIB), "protected": 1} for day in range(5)]
    archives.append(_archive("backups", 701, 5, 10 * GIB))
    archives.append(_archive("backups", 702, 0, GIB))
    _, result = _one_store(jobs, vms, archives, previews={"keep-last=2": []})
    storage = result["storages"][0]
    guest = {g["vmid"]: g for g in storage["by_guest"]}
    assert guest[701]["protected"] == 5
    assert guest[701]["gib_at_full_retention"] == 70.0  # two kept at the newest size, plus 50 GiB protected
    assert guest[702]["retained_at_job_retention"] is None and guest[702]["gib_at_full_retention"] is None
    assert storage["retention_need_unknown_guests"] == [702]
    assert storage["gib_needed_at_job_retention"] == 70.0


def test_calendar_retention_is_bounded_by_its_buckets_and_keep_all_is_not():
    vms = [{"vmid": 401, "node": "n1"}, {"vmid": 402, "node": "n1"}]
    jobs = [{"id": "cal", "type": "vzdump", "storage": "backups", "vmid": "401",
             "prune-backups": "keep-daily=7,keep-weekly=4"},
            {"id": "keep", "type": "vzdump", "storage": "backups", "vmid": "402", "prune-backups": "keep-all=1"}]
    archives = [_archive("backups", 401, 0, GIB), _archive("backups", 402, 0, GIB)]
    previews = {"keep-daily=7,keep-weekly=4": [], "keep-all=1": []}
    _, result = _one_store(jobs, vms, archives, previews=previews)
    storage = result["storages"][0]
    guest = {g["vmid"]: g for g in storage["by_guest"]}
    assert guest[401]["retained_at_job_retention"] == 11 and guest[401]["gib_at_full_retention"] == 11.0
    assert guest[402]["retained_at_job_retention"] is None and guest[402]["gib_at_full_retention"] is None
    assert storage["retention_need_unknown_guests"] == [402]
    assert storage["gib_needed_at_job_retention"] == 11.0


@pytest.mark.parametrize(("retention", "bound"), [
    ("keep-last=3", 3),
    ("keep-all=0,keep-last=2", 2),
    ("keep-daily=7,keep-weekly=4", 11),
    ("keep-all=1", None),
    ("keep-all=1,keep-last=2", None),
    ("keep-last=0", None),  # no positive keep-* option: Proxmox keeps everything
])
def test_retained_is_the_sum_of_positive_keep_options_or_none_when_unbounded(retention, bound):
    assert survey._retained(retention) == bound


def test_a_job_that_defers_to_the_storage_default_uses_the_storage_setting():
    vms = [{"vmid": 501, "name": "defer", "node": "n1"}]
    jobs = [{"id": "defer", "type": "vzdump", "storage": "backups", "vmid": "501"}]
    archives = [_archive("backups", 501, day, GIB) for day in range(4)]
    config = [{"storage": "backups", "type": "nfs", "prune-backups": "keep-last=3"}]
    plan, result = _one_store(jobs, vms, archives, config,
                              previews={"storage-default": [_mark(archives[0])]})
    assert plan["reads"][0]["retentions"] == ["storage-default"]
    guest = result["storages"][0]["by_guest"][0]
    assert guest["job_retention"] == ["storage-default"] and guest["retained_at_job_retention"] == 3
    assert guest["prunable"] == 1


def test_a_storage_default_the_storage_does_not_set_is_unknown_and_never_previewed():
    vms = [{"vmid": 501, "name": "defer", "node": "n1"}, {"vmid": 502, "name": "pinned", "node": "n1"}]
    jobs = [{"id": "defer", "type": "vzdump", "storage": "backups", "vmid": "501"},
            {"id": "pinned", "type": "vzdump", "storage": "backups", "vmid": "501,502",
             "prune-backups": "keep-last=2"}]
    archives = [_archive("backups", 501, day, GIB) for day in range(3)]
    # keep-last=2 marks archive 0 of guest 501, but the storage-default job on 501 was never previewed.
    previews = {"keep-last=2": [_mark(archives[0])]}
    plan, result = _one_store(jobs, vms, archives, previews=previews)
    assert plan["reads"][0]["retentions"] == ["keep-last=2"]
    storage = result["storages"][0]
    guest = {g["vmid"]: g for g in storage["by_guest"]}
    assert guest[501]["retained_at_job_retention"] is None and guest[501]["prunable"] == 0
    assert storage["retention_need_unknown_guests"] == [501]
    assert storage["retentions_previewed"] == ["keep-last=2"]


def test_a_job_that_only_defers_on_a_storage_with_no_setting_previews_nothing_and_is_unknown():
    vms = [{"vmid": 601, "name": "defer", "node": "n1"}]
    jobs = [{"id": "defer", "type": "vzdump", "storage": "backups", "vmid": "601"}]
    archives = [_archive("backups", 601, day, GIB) for day in range(3)]
    plan, result = _one_store(jobs, vms, archives)  # no storage_config: the storage sets no prune-backups
    assert plan["reads"][0]["retentions"] == []
    storage = result["storages"][0]
    guest = storage["by_guest"][0]
    assert guest["job_retention"] == ["storage-default"] and guest["retained_at_job_retention"] is None
    assert guest["prunable"] == 0 and storage["retention_need_unknown_guests"] == [601]
    assert storage["retentions_previewed"] == []


def test_a_node_local_storage_on_two_nodes_is_not_reported_as_one_filesystem():
    def _dir_listing(node, names):
        return {"item": node, "status": 200, "json": {"data": [
            {"storage": name, "content": "backup", "active": 1, "type": "dir", "shared": 0,
             "total": 100 * GIB, "used": 10 * GIB, "avail": 90 * GIB} for name in names]}}

    config = [{"storage": "local", "type": "dir", "path": "/var/lib/vz"},
              {"storage": "scratch", "type": "dir", "path": "/var/lib/vz"}]
    listings = [_dir_listing("n1", ["local"]), _dir_listing("n2", ["local"])]
    plan = survey.plan({"nodes": NODES, "jobs": [], "storage_listings": listings, "storage_config": config})
    assert [(r["node"], r["storage"]) for r in plan["reads"]] == [("n1", "local"), ("n2", "local")]
    summary = {"plan": plan, "vms": [], "storage_config": config, "jobs": [],
               "content_reads": [{"item": r, "status": 200, "json": {"data": []}} for r in plan["reads"]],
               "prune_reads": []}
    assert survey.summarize(summary)["shared_backends"] == []

    # The same path on one node is one filesystem, whatever the storage IDs say.
    same_node = [_dir_listing("n1", ["local", "scratch"])]
    plan = survey.plan({"nodes": NODES, "jobs": [], "storage_listings": same_node, "storage_config": config})
    summary = {"plan": plan, "vms": [], "storage_config": config, "jobs": [],
               "content_reads": [{"item": r, "status": 200, "json": {"data": []}} for r in plan["reads"]],
               "prune_reads": []}
    assert [b["storages"] for b in survey.summarize(summary)["shared_backends"]] == [["local", "scratch"]]


def test_the_survey_runs_only_at_the_reviewed_commit():
    plays = yaml.safe_load((ROOT / "platform/playbooks/survey-proxmox-backup-storage.yml").read_text())
    gate = plays[1]
    assert gate["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert "platform/playbooks/files/survey_backup_storage.py" in gate["vars"]["_reviewed_checkout_payload_paths"]
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(t for t in templates if t["name"] == "Survey Proxmox Backup Storage (Dev)")
    assert [(v["name"], v["required"]) for v in template["survey_vars"]] == [("expected_repository_sha", True)]
