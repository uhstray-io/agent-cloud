//! The backup-storage survey's summarizer, `platform/playbooks/files/survey_backup_storage.py
//! summarize` (cli tier): the receipt sections that read the newest archive per guest, CephFS
//! filesystem sharing and pools, and each online node's disks.
//!
//! Each test runs the helper on a hand-written payload with the shape the playbook's summarize task
//! sends, and asserts on its stdout text. Where the receipt must never print a value, the payload
//! carries a sentinel string for it (a monitor address, a WWN, a path): the sentinel's absence is
//! the assertion. The helper's pytest (platform/tests/test_survey_backup_storage.py) covers the rest
//! of its logic.

use agent_cloud_tests::process::scrubbed;
use agent_cloud_tests::repo;
use tempfile::TempDir;

const HELPER: &str = "platform/playbooks/files/survey_backup_storage.py";

/// Runs `summarize` on `payload` and returns its stdout. The helper must exit 0.
fn summarize(payload: &str) -> String {
    let home = TempDir::new().expect("a temporary home");
    let script = repo::path(HELPER);
    let out = scrubbed("python3", home.path())
        .args(["-I", script.to_str().expect("utf-8 path"), "summarize"])
        .write_stdin(payload)
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    String::from_utf8(out).expect("utf-8 stdout")
}

#[test]
fn a_guests_newest_archive_is_kept_per_storage_and_node_local_storages_carry_their_node() {
    // Arrange: guest 200 has an archive on the shared storage `backups` and one on the node-local
    // `local` of n1. The local one is newer (ctime 1790086400, one day after 1790000000).
    let payload = r#"{
        "plan": {"reads": [
            {"node": "n1", "storage": "backups",
             "status": {"type": "nfs", "shared": 1, "total": 107374182400, "used": 10737418240, "avail": 96636764160}},
            {"node": "n1", "storage": "local",
             "status": {"type": "dir", "shared": 0, "total": 107374182400, "used": 10737418240, "avail": 96636764160}}
        ]},
        "vms": [{"vmid": 200, "name": "semaphore"}],
        "storage_config": [], "jobs": [], "prune_reads": [],
        "content_reads": [
            {"item": {"node": "n1", "storage": "backups"}, "status": 200, "json": {"data": [
                {"volid": "backups:backup/vzdump-qemu-200-a.vma.zst", "vmid": 200, "size": 1073741824, "ctime": 1790000000}]}},
            {"item": {"node": "n1", "storage": "local"}, "status": 200, "json": {"data": [
                {"volid": "local:backup/vzdump-qemu-200-b.vma.zst", "vmid": 200, "size": 1073741824, "ctime": 1790086400}]}}
        ]
    }"#;

    // Act
    let out = summarize(payload);

    // Assert: one guest row, a newest day for each storage key (a node-local key is "node/storage").
    assert!(
        out.contains(r#""guests": [{"vmid": 200, "name": "semaphore", "guest_exists": true, "newest": {"backups": "2026-09-21", "n1/local": "2026-09-22"}}]"#),
        "{out}"
    );
}

#[test]
fn a_disk_is_in_use_exactly_when_proxmox_sets_used_and_the_unused_total_counts_only_the_rest() {
    // Arrange: /dev/sdb carries "used": "LVM" (in use); /dev/sdc has no used field (unused).
    // Listed out of devpath order, to show the receipt sorts them.
    let payload = r#"{
        "plan": {"reads": []}, "vms": [], "storage_config": [], "jobs": [],
        "content_reads": [], "prune_reads": [],
        "disk_reads": [{"item": "n1", "status": 200, "json": {"data": [
            {"devpath": "/dev/sdc", "size": 500107862016, "model": "MODEL-B", "vendor": "ACME", "serial": "SERIAL-B1", "osdid": -1},
            {"devpath": "/dev/sdb", "size": 1000204886016, "model": "MODEL-A", "vendor": "ACME", "serial": "SERIAL-A1", "used": "LVM", "osdid": -1}
        ]}}]
    }"#;

    // Act
    let out = summarize(payload);

    // Assert: the whole node entry, in order, with sizes computed from the bytes in GiB.
    assert!(
        out.contains(r#""disks": [{"node": "n1", "status": "read", "reason": null, "disks": [{"devpath": "/dev/sdb", "size_gib": 931.5, "model": "MODEL-A", "vendor": "ACME", "serial": "SERIAL-A1", "used": "LVM", "in_use": true, "osdid": -1}, {"devpath": "/dev/sdc", "size_gib": 465.8, "model": "MODEL-B", "vendor": "ACME", "serial": "SERIAL-B1", "used": null, "in_use": false, "osdid": -1}], "unused_count": 1, "unused_gib": 465.8}]"#),
        "{out}"
    );
}

#[test]
fn a_node_whose_disk_list_the_token_may_not_read_is_reported_unavailable_not_refused() {
    // Arrange: the disk list of n1 answers 403. The survey continues, so the run exits 0.
    let payload = r#"{
        "plan": {"reads": []}, "vms": [], "storage_config": [], "jobs": [],
        "content_reads": [], "prune_reads": [],
        "disk_reads": [{"item": "n1", "status": 403, "json": null}]
    }"#;

    // Act
    let out = summarize(payload);

    // Assert: the node is unavailable with the privilege it needs, and no disk or unused figure is invented.
    assert!(
        out.contains(r#""disks": [{"node": "n1", "status": "unavailable", "reason": "HTTP 403: needs Sys.Audit on /nodes/n1", "disks": [], "unused_count": null, "unused_gib": null}]"#),
        "{out}"
    );
}

#[test]
fn cephfs_storages_on_one_filesystem_list_each_other_and_an_external_one_names_no_monitor() {
    // Arrange: cephA and cephB name no filesystem and the cluster has exactly one (cephfs), so they
    // share it. cephExt has a monitor list: it is another cluster's, so it is external with no
    // filesystem even though it names one. Its monitors, path and subdir are sentinels that must never print.
    let payload = r#"{
        "plan": {"reads": []}, "vms": [], "jobs": [], "content_reads": [], "prune_reads": [],
        "storage_config": [
            {"storage": "cephA", "type": "cephfs", "content": "backup", "path": "PATH-SENTINEL", "subdir": "SUBDIR-SENTINEL"},
            {"storage": "cephB", "type": "cephfs", "content": "backup"},
            {"storage": "cephExt", "type": "cephfs", "content": "backup", "monhost": "MONHOST-SENTINEL", "fs-name": "ext-fs"}
        ],
        "ceph_reads": [
            {"item": "fs", "status": 200, "json": {"data": [
                {"name": "cephfs", "data_pool": "cephfs_data", "data_pools": ["cephfs_data"], "metadata_pool": "cephfs_metadata"}]}},
            {"item": "pool", "status": 200, "json": {"data": [
                {"pool_name": "cephfs_data", "bytes_used": 1073741824, "percent_used": 37.36, "size": 3}]}}
        ]
    }"#;

    // Act
    let out = summarize(payload);

    // Assert: the whole cephfs section, the pool's usage rounded as given, and no sentinel printed.
    assert!(
        out.contains(r#""cephfs": {"status": "read", "reason": null, "storages": [{"storage": "cephA", "external": false, "filesystem": "cephfs", "data_pools": ["cephfs_data"], "shares_filesystem_with": ["cephB"]}, {"storage": "cephB", "external": false, "filesystem": "cephfs", "data_pools": ["cephfs_data"], "shares_filesystem_with": ["cephA"]}, {"storage": "cephExt", "external": true, "filesystem": null, "data_pools": [], "shares_filesystem_with": []}], "pools": [{"pool": "cephfs_data", "used_gib": 1.0, "percent_used": 37.4, "replicas": 3}]}"#),
        "{out}"
    );
    for sentinel in ["MONHOST-SENTINEL", "PATH-SENTINEL", "SUBDIR-SENTINEL"] {
        assert!(!out.contains(sentinel), "{sentinel} printed: {out}");
    }
}

#[test]
fn a_ceph_listing_the_token_may_not_read_makes_cephfs_unavailable_and_names_sys_audit() {
    // Arrange: both Ceph reads answer 403 for one CephFS storage on this cluster's own Ceph.
    let payload = r#"{
        "plan": {"reads": []}, "vms": [], "jobs": [], "content_reads": [], "prune_reads": [],
        "storage_config": [{"storage": "cephA", "type": "cephfs", "content": "backup"}],
        "ceph_reads": [
            {"item": "fs", "status": 403, "json": null},
            {"item": "pool", "status": 403, "json": null}
        ]
    }"#;

    // Act
    let out = summarize(payload);

    // Assert: the section is unavailable with the privilege it needs; the storage is still listed, with no filesystem.
    assert!(
        out.contains(r#""cephfs": {"status": "unavailable", "reason": "HTTP 403: needs Sys.Audit or Datastore.Audit on /", "storages": [{"storage": "cephA", "external": false, "filesystem": null, "data_pools": [], "shares_filesystem_with": []}], "pools": []}"#),
        "{out}"
    );
}

#[test]
fn a_disks_serial_prints_and_its_wwn_and_health_never_do() {
    // Arrange: the disk carries a WWN and a SMART health value, both sentinels that must not print.
    let payload = r#"{
        "plan": {"reads": []}, "vms": [], "storage_config": [], "jobs": [],
        "content_reads": [], "prune_reads": [],
        "disk_reads": [{"item": "n1", "status": 200, "json": {"data": [
            {"devpath": "/dev/sdd", "size": 1073741824, "model": "MODEL-C", "vendor": "ACME", "serial": "SERIAL-C1",
             "wwn": "WWN-SENTINEL", "health": "HEALTH-SENTINEL", "osdid": -1}
        ]}}]
    }"#;

    // Act
    let out = summarize(payload);

    // Assert: the serial is in the receipt; the WWN and health are absent, by value and by key.
    assert!(out.contains(r#""serial": "SERIAL-C1""#), "{out}");
    for absent in ["WWN-SENTINEL", "HEALTH-SENTINEL", r#""wwn""#, r#""health""#] {
        assert!(!out.contains(absent), "{absent} printed: {out}");
    }
}
