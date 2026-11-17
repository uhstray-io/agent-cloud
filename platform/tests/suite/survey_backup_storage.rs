//! The backup-storage survey's helper, `platform/playbooks/files/survey_backup_storage.py` (cli tier):
//! the planner's storage selection, and the receipt sections that read the newest archive per guest,
//! CephFS filesystem sharing and pools, and each online node's disks.
//!
//! Each test runs the helper on a hand-written payload with the shape the playbook's summarize task
//! sends, and asserts on its stdout text. Where the receipt must never print a value, the payload
//! carries a sentinel string for it (a monitor address, a WWN, a path): the sentinel's absence is
//! the assertion. The helper's pytest (platform/tests/test_survey_backup_storage.py) covers the rest
//! of its logic.

use agent_cloud_tests::process::scrubbed;
use agent_cloud_tests::repo;
use serde_json::{Value, json};
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
                {"pool_name": "cephfs_data", "bytes_used": 1073741824, "percent_used": 0.3736, "type": "replicated", "size": 3}]}}
        ]
    }"#;

    // Act
    let out = summarize(payload);

    // Assert: the whole cephfs section, the pool's usage rounded as given, and no sentinel printed.
    assert!(
        out.contains(r#""cephfs": {"status": "read", "reason": null, "storages": [{"storage": "cephA", "external": false, "filesystem": "cephfs", "filesystem_source": "only filesystem", "data_pools": ["cephfs_data"], "shares_filesystem_with": ["cephB"]}, {"storage": "cephB", "external": false, "filesystem": "cephfs", "filesystem_source": "only filesystem", "data_pools": ["cephfs_data"], "shares_filesystem_with": ["cephA"]}, {"storage": "cephExt", "external": true, "filesystem": null, "filesystem_source": "external", "data_pools": [], "shares_filesystem_with": []}], "pools": [{"pool": "cephfs_data", "ceph_used_gib": 1.0, "ceph_percent_used": 37.4, "type": "replicated", "size": 3}]}"#),
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
        out.contains(r#""cephfs": {"status": "unavailable", "reason": "HTTP 403: needs Sys.Audit or Datastore.Audit on /", "storages": [{"storage": "cephA", "external": false, "filesystem": null, "filesystem_source": "unresolved", "data_pools": [], "shares_filesystem_with": null}], "pools": []}"#),
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

/// Runs the helper in `mode` (`plan` or `summarize`) on a JSON payload and returns the whole process output, whatever the exit code.
fn run(mode: &str, payload: &Value) -> std::process::Output {
    let home = TempDir::new().expect("a temporary home");
    let script = repo::path(HELPER);
    scrubbed("python3", home.path())
        .args(["-I", script.to_str().expect("utf-8 path"), mode])
        .write_stdin(payload.to_string())
        .assert()
        .get_output()
        .clone()
}

/// The receipt of a payload the helper must summarize (exit 0), parsed.
fn receipt(payload: &Value) -> Value {
    let out = run("summarize", payload);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    serde_json::from_slice(&out.stdout).expect("the receipt is JSON")
}

/// The refusal text of a payload the helper must refuse (exit 1).
fn refusal(payload: &Value) -> String {
    let out = run("summarize", payload);
    assert_eq!(
        out.status.code(),
        Some(1),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    let body: Value = serde_json::from_slice(&out.stdout).expect("a refusal is JSON");
    body["refusal"]
        .as_str()
        .expect("a refusal string")
        .to_owned()
}

const TOTAL: u64 = 107_374_182_400;

/// One CephFS storage read on node n1 with `used` bytes: its plan entry and its (empty) content read.
fn cephfs_read(storage: &str, used: u64) -> (Value, Value) {
    let status =
        json!({"type": "cephfs", "shared": 1, "total": TOTAL, "used": used, "avail": TOTAL - used});
    (
        json!({"node": "n1", "storage": storage, "status": status}),
        json!({"item": {"node": "n1", "storage": storage}, "status": 200, "json": {"data": []}}),
    )
}

/// A payload over CephFS storages, each (storage ID, used bytes), with the cluster's storage config and
/// the Ceph filesystem listing. The pool listing is empty.
fn cephfs_payload(storages: &[(&str, u64)], config: Value, filesystems: Value) -> Value {
    let (plan, content): (Vec<Value>, Vec<Value>) = storages
        .iter()
        .map(|&(s, used)| cephfs_read(s, used))
        .unzip();
    json!({
        "plan": {"reads": plan}, "vms": [], "jobs": [], "prune_reads": [],
        "storage_config": config, "content_reads": content,
        "ceph_reads": [
            {"item": "fs", "status": 200, "json": {"data": filesystems}},
            {"item": "pool", "status": 200, "json": {"data": []}}
        ]
    })
}

/// The `cephfs` rows of a receipt, in storage-ID order.
fn cephfs_rows(out: &Value) -> Vec<Value> {
    out["cephfs"]["storages"]
        .as_array()
        .expect("cephfs storage rows")
        .clone()
}

#[test]
fn cephfs_storages_on_different_filesystems_share_nothing_even_with_identical_bytes() {
    // Arrange: cephA and cephB name different filesystems and have identical total and used bytes.
    // Byte counts alone must not pair them: the name decides, and the names differ.
    let payload = cephfs_payload(
        &[("cephA", 10_737_418_240), ("cephB", 10_737_418_240)],
        json!([
            {"storage": "cephA", "type": "cephfs", "content": "backup", "fs-name": "fsA"},
            {"storage": "cephB", "type": "cephfs", "content": "backup", "fs-name": "fsB"}
        ]),
        json!([{"name": "fsA", "data_pools": ["a_data"]}, {"name": "fsB", "data_pools": ["b_data"]}]),
    );

    // Act
    let out = receipt(&payload);

    // Assert: no shared-backend pair, and neither storage shares a filesystem with the other.
    assert_eq!(out["shared_backends"], json!([]), "{out}");
    let rows = cephfs_rows(&out);
    assert_eq!(rows.len(), 2, "{out}");
    for row in &rows {
        assert_eq!(row["filesystem_source"], json!("fs-name"), "{out}");
        assert_eq!(row["shares_filesystem_with"], json!([]), "{out}");
    }
}

#[test]
fn cephfs_storages_on_one_filesystem_by_name_share_it_and_no_byte_count_is_inferred() {
    // Arrange: cephA and cephB both name fsA, with identical total and used bytes.
    let payload = cephfs_payload(
        &[("cephA", 10_737_418_240), ("cephB", 10_737_418_240)],
        json!([
            {"storage": "cephA", "type": "cephfs", "content": "backup", "fs-name": "fsA"},
            {"storage": "cephB", "type": "cephfs", "content": "backup", "fs-name": "fsA"}
        ]),
        json!([{"name": "fsA", "data_pools": ["a_data"]}, {"name": "fsB", "data_pools": []}]),
    );

    // Act
    let out = receipt(&payload);

    // Assert: one pair with the name as its reason and no combined figure (overlap of subdirectories is unknown).
    assert_eq!(
        out["shared_backends"],
        json!([{"storages": ["cephA", "cephB"], "reason": "same CephFS filesystem", "combined_unaccounted_gib": null}]),
        "{out}"
    );
    let rows = cephfs_rows(&out);
    assert_eq!(rows[0]["shares_filesystem_with"], json!(["cephB"]), "{out}");
    assert_eq!(rows[1]["shares_filesystem_with"], json!(["cephA"]), "{out}");
}

#[test]
fn an_unresolved_cephfs_storage_alone_is_unknown_and_never_listed_as_a_sharer() {
    // Arrange: cephA and cephC name fsA. cephB names no filesystem and the cluster has two, so cephB is
    // unresolved. The used bytes differ, so no byte-count pair is in play either.
    let payload = cephfs_payload(
        &[
            ("cephA", 10_737_418_240),
            ("cephB", 21_474_836_480),
            ("cephC", 32_212_254_720),
        ],
        json!([
            {"storage": "cephA", "type": "cephfs", "content": "backup", "fs-name": "fsA"},
            {"storage": "cephB", "type": "cephfs", "content": "backup"},
            {"storage": "cephC", "type": "cephfs", "content": "backup", "fs-name": "fsA"}
        ]),
        json!([{"name": "fsA", "data_pools": ["a_data"]}, {"name": "fsB", "data_pools": ["b_data"]}]),
    );

    // Act
    let out = receipt(&payload);

    // Assert: cephB alone is null; cephA and cephC list each other, and shared_backends agrees with them.
    let rows: std::collections::BTreeMap<String, Value> = cephfs_rows(&out)
        .into_iter()
        .map(|row| {
            (
                row["storage"].as_str().expect("a storage ID").to_owned(),
                row,
            )
        })
        .collect();
    assert_eq!(
        rows["cephB"]["filesystem_source"],
        json!("unresolved"),
        "{out}"
    );
    assert_eq!(
        rows["cephB"]["shares_filesystem_with"],
        json!(null),
        "{out}"
    );
    assert_eq!(
        rows["cephA"]["shares_filesystem_with"],
        json!(["cephC"]),
        "{out}"
    );
    assert_eq!(
        rows["cephC"]["shares_filesystem_with"],
        json!(["cephA"]),
        "{out}"
    );
    assert_eq!(
        out["shared_backends"],
        json!([{"storages": ["cephA", "cephC"], "reason": "same CephFS filesystem", "combined_unaccounted_gib": null}]),
        "{out}"
    );
}

#[test]
fn unresolved_cephfs_storages_with_identical_bytes_are_not_paired_by_their_byte_counts() {
    // Arrange: neither storage names a filesystem and the cluster has two, so both are unresolved,
    // and their total and used bytes are identical.
    let payload = cephfs_payload(
        &[("cephA", 10_737_418_240), ("cephB", 10_737_418_240)],
        json!([
            {"storage": "cephA", "type": "cephfs", "content": "backup"},
            {"storage": "cephB", "type": "cephfs", "content": "backup"}
        ]),
        json!([{"name": "fsA", "data_pools": ["a_data"]}, {"name": "fsB", "data_pools": ["b_data"]}]),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the cephfs section says sharing is unknown, and shared_backends does not contradict it.
    assert_eq!(out["shared_backends"], json!([]), "{out}");
    for row in cephfs_rows(&out) {
        assert_eq!(row["shares_filesystem_with"], json!(null), "{out}");
    }
}

#[test]
fn a_disabled_cephfs_storage_is_left_out_of_every_cephfs_figure() {
    // Arrange: cephOff is disabled (`disable` set), so it is listed nowhere, not even as unavailable.
    let payload = cephfs_payload(
        &[("cephA", 10_737_418_240)],
        json!([
            {"storage": "cephA", "type": "cephfs", "content": "backup", "fs-name": "fsA"},
            {"storage": "cephOff", "type": "cephfs", "content": "backup", "fs-name": "fsB", "disable": 1}
        ]),
        json!([{"name": "fsA", "data_pools": ["a_data"]}, {"name": "fsB", "data_pools": ["b_data"]}]),
    );

    // Act
    let out = receipt(&payload);

    // Assert
    let rows = cephfs_rows(&out);
    assert_eq!(rows.len(), 1, "{out}");
    assert_eq!(rows[0]["storage"], json!("cephA"), "{out}");
    assert_eq!(out["cephfs"]["status"], json!("read"), "{out}");
    assert!(!out.to_string().contains("cephOff"), "{out}");
}

#[test]
fn a_pool_ceph_has_no_stats_for_reports_its_usage_unknown_not_zero() {
    // Arrange: fsA's data pool a_data is listed without bytes_used or percent_used, as Proxmox lists a
    // pool `ceph df` has no stats for.
    let mut payload = cephfs_payload(
        &[("cephA", 10_737_418_240)],
        json!([{"storage": "cephA", "type": "cephfs", "content": "backup", "fs-name": "fsA"}]),
        json!([{"name": "fsA", "data_pools": ["a_data"]}]),
    );
    payload["ceph_reads"][1]["json"]["data"] = json!([{"pool_name": "a_data", "size": 3}]);

    // Act
    let out = receipt(&payload);

    // Assert
    assert_eq!(
        out["cephfs"]["pools"],
        json!([{"pool": "a_data", "ceph_used_gib": null, "ceph_percent_used": null, "type": null, "size": 3}]),
        "{out}"
    );
}

#[test]
fn a_filesystem_listed_with_only_the_older_data_pool_field_still_names_its_pool() {
    // Arrange: Proxmox before pve-manager's 2026-05 change lists a filesystem with `data_pool`, one name,
    // and no `data_pools`.
    let mut payload = cephfs_payload(
        &[("cephA", 10_737_418_240)],
        json!([{"storage": "cephA", "type": "cephfs", "content": "backup", "fs-name": "fsA"}]),
        json!([{"name": "fsA", "metadata_pool": "a_meta", "data_pool": "a_data"}]),
    );
    payload["ceph_reads"][1]["json"]["data"] = json!([{"pool_name": "a_data", "bytes_used": 1073741824, "percent_used": 0.5, "type": "replicated", "size": 3}]);

    // Act
    let out = receipt(&payload);

    // Assert
    assert_eq!(
        cephfs_rows(&out)[0]["data_pools"],
        json!(["a_data"]),
        "{out}"
    );
    assert_eq!(
        out["cephfs"]["pools"],
        json!([{"pool": "a_data", "ceph_used_gib": 1.0, "ceph_percent_used": 50.0, "type": "replicated", "size": 3}]),
        "{out}"
    );
}

#[test]
fn the_planner_reads_no_disabled_storage_even_when_its_status_row_says_active() {
    // Arrange: node n1 lists two backup storages as active; the cluster config disables `off`.
    let status = json!({"type": "nfs", "shared": 1, "total": TOTAL, "used": 0, "avail": TOTAL});
    let row = |storage: &str| {
        let mut row = status.clone();
        row["storage"] = json!(storage);
        row["content"] = json!("backup");
        row["active"] = json!(1);
        row
    };
    let payload = json!({
        "nodes": [{"node": "n1", "status": "online"}], "jobs": [],
        "storage_config": [{"storage": "on", "type": "nfs"}, {"storage": "off", "type": "nfs", "disable": 1}],
        "storage_listings": [{"item": "n1", "status": 200, "json": {"data": [row("on"), row("off")]}}]
    });

    // Act
    let out = run("plan", &payload);

    // Assert: only `on` is planned.
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    let plan: Value = serde_json::from_slice(&out.stdout).expect("the plan is JSON");
    let planned: Vec<&str> = plan["reads"]
        .as_array()
        .expect("plan reads")
        .iter()
        .map(|read| read["storage"].as_str().expect("a storage ID"))
        .collect();
    assert_eq!(planned, ["on"], "{plan}");
}

#[test]
fn a_disk_listing_that_answers_200_without_a_list_refuses_the_survey() {
    // Arrange: n1 answers 200 with no data list. That is not a disk list, so nothing is summarized.
    let payload = json!({
        "plan": {"reads": []}, "vms": [], "storage_config": [], "jobs": [],
        "content_reads": [], "prune_reads": [],
        "disk_reads": [{"item": "n1", "status": 200, "json": {"data": null}}]
    });

    // Act
    let text = refusal(&payload);

    // Assert
    assert!(text.contains("no disk list for node n1"), "{text}");
}
