//! The backup-storage survey's helper, `platform/playbooks/files/survey_backup_storage.py` (cli tier):
//! the planner's storage selection, and the receipt sections that read the newest archive per guest,
//! CephFS filesystem sharing and pools, each Ceph OSD's fill and the cluster's health codes, and each
//! online node's disks.
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

    // Assert: the whole cephfs section, the pool's percent null because no OSD tree was read to name the Ceph
    // release, and no sentinel printed.
    assert!(
        out.contains(r#""cephfs": {"status": "read", "reason": null, "storages": [{"storage": "cephA", "external": false, "filesystem": "cephfs", "filesystem_source": "only filesystem", "data_pools": ["cephfs_data"], "data_pools_complete": true, "shares_filesystem_with": ["cephB"]}, {"storage": "cephB", "external": false, "filesystem": "cephfs", "filesystem_source": "only filesystem", "data_pools": ["cephfs_data"], "data_pools_complete": true, "shares_filesystem_with": ["cephA"]}, {"storage": "cephExt", "external": true, "filesystem": null, "filesystem_source": "external", "data_pools": [], "data_pools_complete": null, "shares_filesystem_with": []}], "pools": [{"pool": "cephfs_data", "ceph_used_gib": 1.0, "ceph_percent_used": null, "type": "replicated", "size": 3}], "percent_scale": "unknown: the Ceph OSD tree was not read"}"#),
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
        out.contains(r#""cephfs": {"status": "unavailable", "reason": "HTTP 403: needs Sys.Audit or Datastore.Audit on /", "storages": [{"storage": "cephA", "external": false, "filesystem": null, "filesystem_source": "unresolved", "data_pools": [], "data_pools_complete": null, "shares_filesystem_with": null}], "pools": [], "percent_scale": "unknown: the Ceph OSD tree was not read"}"#),
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

    // Assert: the serial is in the receipt; the WWN and health are absent, by value and by key. The `health`
    // key is the Ceph section's, so the key is checked within the disks alone.
    assert!(out.contains(r#""serial": "SERIAL-C1""#), "{out}");
    for absent in ["WWN-SENTINEL", "HEALTH-SENTINEL", r#""wwn""#] {
        assert!(!out.contains(absent), "{absent} printed: {out}");
    }
    let receipt_value: Value = serde_json::from_str(&out).expect("the receipt is JSON");
    assert!(
        !receipt_value["disks"].to_string().contains(r#""health""#),
        "{out}"
    );
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
fn a_filesystem_listed_with_only_the_older_data_pool_field_names_that_pool_and_says_the_list_may_be_partial()
 {
    // Arrange: Proxmox before pve-manager's 2026-05 change lists a filesystem with `data_pool`, its first data
    // pool only, and no `data_pools`.
    let mut payload = cephfs_payload(
        &[("cephA", 10_737_418_240)],
        json!([{"storage": "cephA", "type": "cephfs", "content": "backup", "fs-name": "fsA"}]),
        json!([{"name": "fsA", "metadata_pool": "a_meta", "data_pool": "a_data"}]),
    );
    payload["ceph_reads"][1]["json"]["data"] = json!([{"pool_name": "a_data", "bytes_used": 1073741824, "percent_used": 0.5, "type": "replicated", "size": 3}]);

    // Act
    let out = receipt(&payload);

    // Assert
    let row = &cephfs_rows(&out)[0];
    assert_eq!(row["data_pools"], json!(["a_data"]), "{out}");
    assert_eq!(row["data_pools_complete"], json!(false), "{out}");
    assert_eq!(
        out["cephfs"]["pools"],
        json!([{"pool": "a_data", "ceph_used_gib": 1.0, "ceph_percent_used": null, "type": "replicated", "size": 3}]),
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
fn a_content_read_or_prune_preview_that_answers_200_without_a_list_refuses_the_survey() {
    // Arrange: one storage whose content read, or whose one prune preview, answers 200 with no data list.
    let (plan, content) = cephfs_read("backups", 0);
    let prune = json!({"item": [{"node": "n1", "storage": "backups"}, "keep-last=1"], "status": 200,
                       "json": {"data": []}});
    for (what, mut content, mut prune) in [
        ("content", content.clone(), prune.clone()),
        ("prune preview", content, prune),
    ] {
        let target = if what == "content" {
            &mut content
        } else {
            &mut prune
        };
        target["json"] = json!({"data": null});
        let payload = json!({
            "plan": {"reads": [plan.clone()]}, "vms": [], "storage_config": [], "jobs": [],
            "content_reads": [content], "prune_reads": [prune]
        });

        // Act
        let text = refusal(&payload);

        // Assert
        assert!(
            text.contains(&format!("no {what} list for storage backups")),
            "{text}"
        );
    }
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

/// The OSD tree read of the capacity pair: `status` and the body's `data` (unread when the status is not 200).
fn osd_read(status: u16, data: Value) -> Value {
    json!({"item": "nodes/pve1/ceph/osd", "status": status, "json": {"data": data}})
}

/// The cluster-status read of the capacity pair, shaped as `osd_read` is.
fn status_read(status: u16, data: Value) -> Value {
    json!({"item": "cluster/ceph/status", "status": status, "json": {"data": data}})
}

/// Two hosts, three OSDs. osd.1 carries no `host` field, so its host is the host bucket above it. Each leaf
/// carries `blfsdev`, a device path that must never print. Raw JSON, since the sizes exceed i32.
fn ceph_tree() -> Value {
    serde_json::from_str(
        r#"{"root": {"type": "root", "name": "default", "id": -1, "leaf": 0, "children": [
            {"type": "host", "name": "pve1", "id": -2, "children": [
                {"type": "osd", "name": "osd.0", "id": 0, "leaf": 1, "host": "pve1", "device_class": "hdd",
                 "status": "up", "in": 1, "blfsdev": "/dev/SENTINEL-DEV",
                 "total_space": 107374182400, "bytes_used": 53687091200, "percent_used": 50.0, "ceph_version_short": "18.2"},
                {"type": "osd", "name": "osd.1", "id": 1, "leaf": 1, "device_class": "hdd",
                 "status": "up", "in": 1, "blfsdev": "/dev/SENTINEL-DEV",
                 "total_space": 214748364800, "bytes_used": 161061273600, "percent_used": 75.0, "ceph_version_short": "18.2"}
            ]},
            {"type": "host", "name": "pve2", "id": -3, "children": [
                {"type": "osd", "name": "osd.2", "id": 2, "leaf": 1, "host": "pve2", "device_class": "ssd",
                 "status": "up", "in": 1, "blfsdev": "/dev/SENTINEL-DEV",
                 "total_space": 107374182400, "bytes_used": 21474836480, "percent_used": 20.0, "ceph_version_short": "18.2"}
            ]}
        ]}, "flags": "noout,norebalance"}"#,
    )
    .expect("the fixture is JSON")
}

/// The cluster status: a health detail with two checks, one carrying a message, and a monitor map. The
/// message and the addresses are sentinels that must never print.
fn ceph_status() -> Value {
    serde_json::from_str(
        r#"{"health": {"status": "HEALTH_WARN", "checks": {
            "POOL_FULL": {"severity": "HEALTH_ERR", "summary": {"message": "SENTINEL-MSG"}},
            "OSD_NEARFULL": {"severity": "HEALTH_WARN", "summary": {"message": "SENTINEL-MSG"}}}},
            "monmap": {"mons": [{"addr": "SENTINEL-ADDR"}]}}"#,
    )
    .expect("the fixture is JSON")
}

/// A payload with one own CephFS storage, so the capacity reads apply, and the two capacity reads given.
fn capacity_payload(osd: Value, status: Value) -> Value {
    json!({
        "plan": {"reads": []}, "vms": [], "jobs": [], "content_reads": [], "prune_reads": [],
        "storage_config": [{"storage": "cephA", "type": "cephfs", "content": "backup"}],
        "ceph_capacity_reads": [osd, status]
    })
}

#[test]
fn a_ceph_osd_tree_reports_each_osds_fill_its_host_totals_the_fullest_osd_and_the_health_codes() {
    // Arrange: osd.1 gets its host from the host bucket above it, not from a field of its own.
    let payload = capacity_payload(osd_read(200, ceph_tree()), status_read(200, ceph_status()));

    // Act
    let out = receipt(&payload);

    // Assert: each OSD in id order, each host in name order, the fullest OSD, the health codes with severity
    // (no message), and the flags split on the comma.
    assert_eq!(
        out["ceph_capacity"],
        json!({
            "status": "read", "reason": null,
            "osds": [
                {"osd": 0, "name": "osd.0", "host": "pve1", "device_class": "hdd", "status": "up", "in": 1,
                 "total_gib": 100.0, "used_gib": 50.0, "percent_used": 50.0, "version": "18.2"},
                {"osd": 1, "name": "osd.1", "host": "pve1", "device_class": "hdd", "status": "up", "in": 1,
                 "total_gib": 200.0, "used_gib": 150.0, "percent_used": 75.0, "version": "18.2"},
                {"osd": 2, "name": "osd.2", "host": "pve2", "device_class": "ssd", "status": "up", "in": 1,
                 "total_gib": 100.0, "used_gib": 20.0, "percent_used": 20.0, "version": "18.2"}
            ],
            "hosts": [
                {"host": "pve1", "osds": 2, "total_gib": 300.0, "used_gib": 200.0, "percent_used": 66.7},
                {"host": "pve2", "osds": 1, "total_gib": 100.0, "used_gib": 20.0, "percent_used": 20.0}
            ],
            "fullest_osd": {"osd": 1, "host": "pve1", "percent_used": 75.0},
            "health": {"status": "HEALTH_WARN", "checks": [
                {"code": "OSD_NEARFULL", "severity": "HEALTH_WARN"},
                {"code": "POOL_FULL", "severity": "HEALTH_ERR"}
            ]},
            "flags": ["noout", "norebalance"]
        }),
        "{out}"
    );
}

#[test]
fn an_osd_without_usage_reports_its_fill_as_null_not_zero_and_its_host_percent_null() {
    // Arrange: osd.2, alone on pve2, has no total_space, bytes_used or percent_used (Ceph reported none).
    let mut tree = ceph_tree();
    let osd2 = &mut tree["root"]["children"][1]["children"][0];
    for key in ["total_space", "bytes_used", "percent_used"] {
        osd2.as_object_mut().expect("an OSD object").remove(key);
    }
    let payload = capacity_payload(osd_read(200, tree), status_read(200, ceph_status()));

    // Act
    let out = receipt(&payload);

    // Assert: osd.2's fill and pve2's totals are null (absent usage is unknown, never 0); pve1 is unchanged.
    assert_eq!(
        out["ceph_capacity"]["osds"][2],
        json!({"osd": 2, "name": "osd.2", "host": "pve2", "device_class": "ssd", "status": "up", "in": 1,
               "total_gib": null, "used_gib": null, "percent_used": null, "version": "18.2"}),
        "{out}"
    );
    assert_eq!(
        out["ceph_capacity"]["hosts"][1],
        json!({"host": "pve2", "osds": 1, "total_gib": null, "used_gib": null, "percent_used": null}),
        "{out}"
    );
    assert_eq!(
        out["ceph_capacity"]["hosts"][0],
        json!({"host": "pve1", "osds": 2, "total_gib": 300.0, "used_gib": 200.0, "percent_used": 66.7}),
        "{out}"
    );
    assert_eq!(
        out["ceph_capacity"]["fullest_osd"],
        json!({"osd": 1, "host": "pve1", "percent_used": 75.0}),
        "{out}"
    );
}

#[test]
fn the_fullest_osd_is_chosen_by_its_unrounded_fill() {
    // Arrange: osd.0 at 75.01 and osd.1 at 75.04 both print as 75.0; osd.1 is the fuller one.
    let mut tree = ceph_tree();
    tree["root"]["children"][0]["children"][0]["percent_used"] = json!(75.01);
    tree["root"]["children"][0]["children"][1]["percent_used"] = json!(75.04);
    let payload = capacity_payload(osd_read(200, tree), status_read(200, ceph_status()));

    // Act
    let out = receipt(&payload);

    // Assert: osd.1 is chosen, not the lower id, and its fill is printed rounded.
    assert_eq!(
        out["ceph_capacity"]["fullest_osd"],
        json!({"osd": 1, "host": "pve1", "percent_used": 75.0}),
        "{out}"
    );
}

#[test]
fn osds_with_the_same_unrounded_fill_give_the_fullest_to_the_lower_id() {
    // Arrange: osd.0 and osd.1 both at exactly 75.04.
    let mut tree = ceph_tree();
    tree["root"]["children"][0]["children"][0]["percent_used"] = json!(75.04);
    tree["root"]["children"][0]["children"][1]["percent_used"] = json!(75.04);
    let payload = capacity_payload(osd_read(200, tree), status_read(200, ceph_status()));

    // Act
    let out = receipt(&payload);

    // Assert: the tie goes to osd.0.
    assert_eq!(
        out["ceph_capacity"]["fullest_osd"],
        json!({"osd": 0, "host": "pve1", "percent_used": 75.0}),
        "{out}"
    );
}

#[test]
fn a_403_on_the_osd_tree_is_unavailable_with_its_privilege_and_the_health_still_reads() {
    // Arrange: the OSD tree answers 403; the cluster status answers 200.
    let payload = capacity_payload(osd_read(403, Value::Null), status_read(200, ceph_status()));

    // Act
    let out = receipt(&payload);

    // Assert: unavailable with the privilege named, no OSD or host invented, the health from the status read.
    assert_eq!(
        out["ceph_capacity"],
        json!({
            "status": "unavailable", "reason": "HTTP 403: needs Sys.Audit or Datastore.Audit on /",
            "osds": [], "hosts": [], "fullest_osd": null,
            "health": {"status": "HEALTH_WARN", "checks": [
                {"code": "OSD_NEARFULL", "severity": "HEALTH_WARN"},
                {"code": "POOL_FULL", "severity": "HEALTH_ERR"}
            ]},
            "flags": []
        }),
        "{out}"
    );
}

#[test]
fn a_cluster_with_no_own_cephfs_storage_does_not_read_its_capacity() {
    // Arrange: the only CephFS storage has a monitor list, so it is another cluster's and the capacity is not read.
    let mut payload = capacity_payload(osd_read(200, ceph_tree()), status_read(200, ceph_status()));
    payload["storage_config"] = json!([
        {"storage": "cephExt", "type": "cephfs", "content": "backup", "monhost": "MONHOST-SENTINEL"}
    ]);

    // Act
    let out = receipt(&payload);

    // Assert: not read, with the reason, and the monitor address never prints.
    assert_eq!(
        out["ceph_capacity"],
        json!({"status": "not read", "reason": "no CephFS storage on this cluster's own Ceph",
               "osds": [], "hosts": [], "fullest_osd": null, "health": null, "flags": []}),
        "{out}"
    );
    assert!(!out.to_string().contains("MONHOST-SENTINEL"), "{out}");
}

#[test]
fn skipped_capacity_reads_are_not_read() {
    // Arrange: the playbook skipped both reads (its gate was off), so each carries "skipped" and no body.
    let mut payload = capacity_payload(osd_read(200, ceph_tree()), status_read(200, ceph_status()));
    payload["ceph_capacity_reads"] = json!([
        {"item": "nodes/pve1/ceph/osd", "skipped": true},
        {"item": "cluster/ceph/status", "skipped": true}
    ]);

    // Act
    let out = receipt(&payload);

    // Assert
    assert_eq!(
        out["ceph_capacity"],
        json!({"status": "not read", "reason": "the Ceph OSD tree and health were not read",
               "osds": [], "hosts": [], "fullest_osd": null, "health": null, "flags": []}),
        "{out}"
    );
}

#[test]
fn an_osd_tree_without_a_root_refuses_the_survey() {
    // Arrange: the OSD read answers 200 with a body that has no root bucket, so no tree can be summarized.
    let payload = capacity_payload(
        osd_read(200, json!({"flags": "noout"})),
        status_read(200, ceph_status()),
    );

    // Act
    let text = refusal(&payload);

    // Assert
    assert_eq!(
        text,
        "Proxmox returned no Ceph OSD tree; nothing was summarized."
    );
}

#[test]
fn a_cluster_status_without_data_refuses_the_survey() {
    // Arrange: the status read answers 200 with no data object.
    let payload = capacity_payload(osd_read(200, ceph_tree()), status_read(200, Value::Null));

    // Act
    let text = refusal(&payload);

    // Assert
    assert_eq!(
        text,
        "Proxmox returned no Ceph status; nothing was summarized."
    );
}

#[test]
fn ceph_sentinels_never_print_the_device_path_the_check_message_or_the_monitor_map() {
    // Arrange: the tree, the check messages and the monitor map all carry sentinel values.
    let payload = capacity_payload(osd_read(200, ceph_tree()), status_read(200, ceph_status()));

    // Act
    let text = receipt(&payload).to_string();

    // Assert: the sentinel values, and the keys that would carry them, are absent.
    for absent in ["SENTINEL", "blfsdev", "monmap", "mgrmap", "summary", "mons"] {
        assert!(!text.contains(absent), "{absent} printed: {text}");
    }
}

#[test]
fn a_tree_without_flags_has_no_flags_and_a_status_without_checks_has_null_checks() {
    // Arrange: no flags on the tree; a health detail that carries a status but no check list.
    let mut tree = ceph_tree();
    tree.as_object_mut()
        .expect("the tree is an object")
        .remove("flags");
    let payload = capacity_payload(
        osd_read(200, tree),
        status_read(200, json!({"health": {"status": "HEALTH_OK"}})),
    );

    // Act
    let out = receipt(&payload);

    // Assert
    assert_eq!(out["ceph_capacity"]["flags"], json!([]), "{out}");
    assert_eq!(
        out["ceph_capacity"]["health"],
        json!({"status": "HEALTH_OK", "checks": null}),
        "{out}"
    );
}

// The vzdump `remove` survey: a job's own remove, or the node default it takes, decides whether its guests are
// pruned. Off or unknown makes a guest's need unknown and unprunable; a guest no job backs keeps every archive.

const GIB: u64 = 1_073_741_824;
const DAY: u64 = 86_400;
const T0: u64 = 1_790_000_000;

/// An archive of guest `vmid` on `backups`, `day` days after T0, `gib` GiB big; protected when `protected` is set.
fn archive(vmid: u64, day: u64, gib: u64, protected: bool) -> Value {
    json!({"volid": format!("backups:backup/vzdump-qemu-{vmid}-{day}.vma.zst"), "vmid": vmid,
           "size": gib * GIB, "ctime": T0 + day * DAY, "protected": u8::from(protected)})
}

/// A prune-preview row that marks `volume` for removal.
fn mark(volume: &Value) -> Value {
    json!({"volid": volume["volid"], "mark": "remove"})
}

/// A node's vzdump defaults read: its `status`, and the body's `data` (not read unless the status is 200).
fn defaults_read(node: &str, status: u16, data: Value) -> Value {
    json!({"item": node, "status": status, "json": {"data": data}})
}

/// A payload over one shared backup storage, `backups`, read through n1: its `archives`, the cluster's `vms` and
/// `jobs`, the prune previews as (retention, marked rows) pairs, and the nodes' vzdump defaults reads when given.
fn backups_payload(
    vms: Value,
    jobs: Value,
    archives: &[Value],
    previews: Vec<(&str, Vec<Value>)>,
    defaults: Option<Value>,
) -> Value {
    let item = json!({"node": "n1", "storage": "backups"});
    let mut plan_read = item.clone();
    plan_read["status"] =
        json!({"type": "nfs", "shared": 1, "total": TOTAL, "used": 0, "avail": TOTAL});
    let prune_reads: Vec<Value> = previews
        .into_iter()
        .map(|(retention, marks)| json!({"item": [item.clone(), retention], "status": 200, "json": {"data": marks}}))
        .collect();
    let mut payload = json!({
        "plan": {"reads": [plan_read]}, "vms": vms, "jobs": jobs, "storage_config": [],
        "content_reads": [{"item": item.clone(), "status": 200, "json": {"data": archives}}],
        "prune_reads": prune_reads
    });
    if let Some(defaults) = defaults {
        payload["vzdump_defaults_reads"] = defaults;
    }
    payload
}

/// The by_guest row of `vmid` on the one storage of a receipt.
fn guest_row(out: &Value, vmid: u64) -> Value {
    out["storages"][0]["by_guest"]
        .as_array()
        .expect("by_guest rows")
        .iter()
        .find(|row| row["vmid"].as_u64() == Some(vmid))
        .cloned()
        .unwrap_or_else(|| panic!("no by_guest row for {vmid}: {out}"))
}

#[test]
fn a_job_with_remove_off_leaves_its_guest_unknown_unprunable_and_its_row_says_false() {
    // Arrange: job `off` backs guest 801 up with remove 0, so Proxmox never prunes for it. Node n1 defaults to 1,
    // but the job's own setting wins. The preview marks an archive that must still not count as prunable.
    let archives = [
        archive(801, 0, 1, false),
        archive(801, 1, 1, false),
        archive(801, 2, 1, false),
    ];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "off", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=1", "remove": 0}]),
        &archives,
        vec![("keep-last=1", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 200, json!({"remove": 1}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the retention still shows, but the need is unknown and nothing is prunable; the job row says false.
    let guest = guest_row(&out, 801);
    assert_eq!(guest["job_retention"], json!(["keep-last=1"]), "{out}");
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(guest["retained_at_job_retention"], Value::Null, "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], Value::Null, "{out}");
    assert_eq!(
        out["storages"][0]["retention_need_unknown_guests"],
        json!([801]),
        "{out}"
    );
    assert_eq!(out["jobs"][0]["remove"], json!(false), "{out}");
}

#[test]
fn a_job_with_remove_off_is_not_previewed_while_one_with_remove_on_or_unset_is() {
    // Arrange: one shared backup storage read through n1, and the same retention on jobs that differ only in remove.
    let listing = json!({"item": "n1", "status": 200, "json": {"data": [
        {"storage": "backups", "content": "backup", "active": 1, "type": "nfs", "shared": 1,
         "total": TOTAL, "used": 0, "avail": TOTAL}]}});
    let plan_with = |job: Value| -> Value {
        let payload = json!({"nodes": [{"node": "n1", "status": "online"}], "jobs": [job],
                             "storage_config": [], "storage_listings": [listing.clone()]});
        let out = run("plan", &payload);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stdout)
        );
        serde_json::from_slice(&out.stdout).expect("the plan is JSON")
    };
    let make = |remove: Option<Value>| {
        let mut job = json!({"id": "j", "type": "vzdump", "storage": "backups", "prune-backups": "keep-last=1"});
        if let Some(remove) = remove {
            job["remove"] = remove;
        }
        job
    };

    // Act and assert: an own remove of 0 contributes no retention to the preview; remove 1 and no remove do.
    assert_eq!(
        plan_with(make(Some(json!(0))))["reads"][0]["retentions"],
        json!([])
    );
    assert_eq!(
        plan_with(make(Some(json!(1))))["reads"][0]["retentions"],
        json!(["keep-last=1"])
    );
    assert_eq!(
        plan_with(make(None))["reads"][0]["retentions"],
        json!(["keep-last=1"])
    );
}

#[test]
fn a_job_without_remove_takes_its_node_default_and_a_default_of_zero_leaves_its_guest_unknown() {
    // Arrange: the job sets no remove, so it takes node n1's default, which is 0: Proxmox prunes nothing for it.
    let archives = [archive(801, 0, 1, false), archive(801, 1, 1, false)];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "nodef", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=1"}]),
        &archives,
        vec![("keep-last=1", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 200, json!({"remove": 0}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the guest is unknown and unprunable, the job row names the node default, and the defaults row says false.
    let guest = guest_row(&out, 801);
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], Value::Null, "{out}");
    assert_eq!(out["jobs"][0]["remove"], json!("node default"), "{out}");
    assert_eq!(
        out["vzdump_defaults"],
        json!([{"node": "n1", "status": "read", "reason": null, "remove": false}]),
        "{out}"
    );
}

#[test]
fn a_node_default_of_one_bounds_the_guest_as_before() {
    // Arrange: keep-last=2 with the node default at 1; the preview marks the oldest of three 1 GiB archives.
    let archives = [
        archive(801, 0, 1, false),
        archive(801, 1, 1, false),
        archive(801, 2, 1, false),
    ];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "nodef", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=2"}]),
        &archives,
        vec![("keep-last=2", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 200, json!({"remove": 1}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: two archives are kept, the oldest is prunable, and the need is the newest size times two.
    let guest = guest_row(&out, 801);
    assert_eq!(guest["job_retention"], json!(["keep-last=2"]), "{out}");
    assert_eq!(guest["retained_at_job_retention"], json!(2), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], json!(2.0), "{out}");
    assert_eq!(guest["prunable"], json!(1), "{out}");
    assert_eq!(guest["prunable_gib"], json!(1.0), "{out}");
    assert_eq!(out["jobs"][0]["remove"], json!("node default"), "{out}");
}

#[test]
fn a_job_remove_of_one_wins_over_a_node_default_of_zero() {
    // Arrange: the job sets remove 1 itself, and node n1's default is 0. The job's own setting decides.
    let archives = [
        archive(801, 0, 1, false),
        archive(801, 1, 1, false),
        archive(801, 2, 1, false),
    ];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "own", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=2", "remove": 1}]),
        &archives,
        vec![("keep-last=2", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 200, json!({"remove": 0}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the guest is bounded and prunable, and the job row says true.
    let guest = guest_row(&out, 801);
    assert_eq!(guest["retained_at_job_retention"], json!(2), "{out}");
    assert_eq!(guest["prunable"], json!(1), "{out}");
    assert_eq!(out["jobs"][0]["remove"], json!(true), "{out}");
}

#[test]
fn a_guest_takes_the_default_of_its_own_node_not_of_the_node_the_storage_is_read_through() {
    // Arrange: the shared storage is read through n1. Guest 801 lives on n1 (default 1), guest 802 on n2 (default 0).
    let archives: Vec<Value> = [801, 802]
        .into_iter()
        .flat_map(|vmid| {
            [
                archive(vmid, 0, 1, false),
                archive(vmid, 1, 1, false),
                archive(vmid, 2, 1, false),
            ]
        })
        .collect();
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "a", "node": "n1"}, {"vmid": 802, "name": "b", "node": "n2"}]),
        json!([{"id": "both", "type": "vzdump", "storage": "backups", "vmid": "801,802",
                "prune-backups": "keep-last=2"}]),
        &archives,
        vec![("keep-last=2", vec![mark(&archives[0]), mark(&archives[3])])],
        Some(json!([
            defaults_read("n1", 200, json!({"remove": 1})),
            defaults_read("n2", 200, json!({"remove": 0}))
        ])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: 801 is bounded and prunable; 802 is unknown and unprunable, though it sits on the storage n1 reads.
    let on_n1 = guest_row(&out, 801);
    assert_eq!(on_n1["retained_at_job_retention"], json!(2), "{out}");
    assert_eq!(on_n1["prunable"], json!(1), "{out}");
    let on_n2 = guest_row(&out, 802);
    assert_eq!(on_n2["gib_needed_at_job_retention"], Value::Null, "{out}");
    assert_eq!(on_n2["prunable"], json!(0), "{out}");
    assert_eq!(
        out["vzdump_defaults"],
        json!([
            {"node": "n1", "status": "read", "reason": null, "remove": true},
            {"node": "n2", "status": "read", "reason": null, "remove": false}
        ]),
        "{out}"
    );
}

#[test]
fn a_403_on_the_node_defaults_is_unavailable_with_its_reason_and_leaves_the_guest_unknown_with_exit_0()
 {
    // Arrange: the token may not read n1's vzdump defaults. The survey continues, so the run exits 0.
    let archives = [archive(801, 0, 1, false), archive(801, 1, 1, false)];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "nodef", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=1"}]),
        &archives,
        vec![("keep-last=1", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 403, Value::Null)])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the row names the privilege it needs and no remove value; the guest stays unknown.
    assert_eq!(
        out["vzdump_defaults"],
        json!([{"node": "n1", "status": "unavailable", "remove": null,
                "reason": "HTTP 403: needs Datastore.Audit or Datastore.AllocateSpace on the node's default backup storage"}]),
        "{out}"
    );
    let guest = guest_row(&out, 801);
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], Value::Null, "{out}");
}

#[test]
fn a_500_on_the_node_defaults_is_unavailable_with_http_500_and_leaves_the_guest_unknown() {
    // Arrange: n1's vzdump defaults answer 500.
    let archives = [archive(801, 0, 1, false), archive(801, 1, 1, false)];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "nodef", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=1"}]),
        &archives,
        vec![("keep-last=1", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 500, Value::Null)])),
    );

    // Act
    let out = receipt(&payload);

    // Assert
    assert_eq!(
        out["vzdump_defaults"],
        json!([{"node": "n1", "status": "unavailable", "reason": "HTTP 500", "remove": null}]),
        "{out}"
    );
    let guest = guest_row(&out, 801);
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], Value::Null, "{out}");
}

#[test]
fn a_node_defaults_read_answering_another_status_refuses_the_survey_by_naming_the_node() {
    // Arrange: n1's defaults answer 404, which the survey does not accept.
    let payload = backups_payload(
        json!([]),
        json!([]),
        &[],
        vec![],
        Some(json!([defaults_read("n1", 404, Value::Null)])),
    );

    // Act
    let text = refusal(&payload);

    // Assert
    assert!(text.contains("node n1") && text.contains("404"), "{text}");
}

#[test]
fn a_node_defaults_read_answering_200_without_a_dict_refuses_the_survey() {
    // Arrange: n1's defaults answer 200 with no data object.
    let payload = backups_payload(
        json!([]),
        json!([]),
        &[],
        vec![],
        Some(json!([defaults_read("n1", 200, Value::Null)])),
    );

    // Act
    let text = refusal(&payload);

    // Assert
    assert!(text.contains("no vzdump defaults for node n1"), "{text}");
}

#[test]
fn absent_vzdump_defaults_reads_name_no_node_and_leave_a_job_without_its_own_remove_unknown() {
    // Arrange: the payload carries no defaults read at all.
    let archives = [archive(801, 0, 1, false), archive(801, 1, 1, false)];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "nodef", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=1"}]),
        &archives,
        vec![("keep-last=1", vec![mark(&archives[0])])],
        None,
    );

    // Act
    let out = receipt(&payload);

    // Assert: no node is listed, and the guest is unknown and unprunable.
    assert_eq!(out["vzdump_defaults"], json!([]), "{out}");
    let guest = guest_row(&out, 801);
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], Value::Null, "{out}");
}

#[test]
fn a_defaults_response_without_remove_reads_as_unknown_not_as_on_or_off() {
    // Arrange: n1's defaults answer 200 with a data object that has no remove key (a release that leaves it out).
    let archives = [archive(801, 0, 1, false), archive(801, 1, 1, false)];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "nodef", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=1"}]),
        &archives,
        vec![("keep-last=1", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 200, json!({"mailto": "root"}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the row is read with remove null, and the guest is unknown and unprunable.
    assert_eq!(
        out["vzdump_defaults"],
        json!([{"node": "n1", "status": "read", "reason": null, "remove": null}]),
        "{out}"
    );
    let guest = guest_row(&out, 801);
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], Value::Null, "{out}");
}

#[test]
fn a_malformed_job_remove_is_unknown_even_when_the_node_default_is_one() {
    // Arrange: the job's own remove is "yes", which is neither 0 nor 1. The node default is 1, but the job's own value decides.
    let archives = [archive(801, 0, 1, false), archive(801, 1, 1, false)];
    let payload = backups_payload(
        json!([{"vmid": 801, "name": "game", "node": "n1"}]),
        json!([{"id": "odd", "type": "vzdump", "storage": "backups", "vmid": "801",
                "prune-backups": "keep-last=1", "remove": "yes"}]),
        &archives,
        vec![("keep-last=1", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 200, json!({"remove": 1}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the job row is null, and the guest is unknown and unprunable.
    assert_eq!(out["jobs"][0]["remove"], Value::Null, "{out}");
    let guest = guest_row(&out, 801);
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], Value::Null, "{out}");
}

#[test]
fn a_job_remove_spelled_as_a_string_or_a_boolean_reads_as_proxmox_does() {
    // Arrange: three guests, one job each, every job keeping one archive. Remove "0" and false are off; "1" is on.
    let archives: Vec<Value> = [811, 812, 813]
        .into_iter()
        .flat_map(|vmid| [archive(vmid, 0, 1, false), archive(vmid, 1, 1, false)])
        .collect();
    let payload = backups_payload(
        json!([{"vmid": 811, "name": "a", "node": "n1"}, {"vmid": 812, "name": "b", "node": "n1"},
               {"vmid": 813, "name": "c", "node": "n1"}]),
        json!([
            {"id": "text0", "type": "vzdump", "storage": "backups", "vmid": "811", "prune-backups": "keep-last=1", "remove": "0"},
            {"id": "bool0", "type": "vzdump", "storage": "backups", "vmid": "812", "prune-backups": "keep-last=1", "remove": false},
            {"id": "text1", "type": "vzdump", "storage": "backups", "vmid": "813", "prune-backups": "keep-last=1", "remove": "1"}
        ]),
        &archives,
        vec![(
            "keep-last=1",
            archives.iter().step_by(2).map(mark).collect(),
        )],
        Some(json!([defaults_read("n1", 200, json!({"remove": 1}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the job rows read false, false and true; only the guest of the "on" job is prunable.
    let rows: Vec<Value> = out["jobs"]
        .as_array()
        .expect("job rows")
        .iter()
        .map(|row| row["remove"].clone())
        .collect();
    assert_eq!(rows, [json!(false), json!(false), json!(true)], "{out}");
    for vmid in [811, 812] {
        let guest = guest_row(&out, vmid);
        assert_eq!(guest["prunable"], json!(0), "{out}");
        assert_eq!(guest["gib_needed_at_job_retention"], Value::Null, "{out}");
    }
    let on = guest_row(&out, 813);
    assert_eq!(on["retained_at_job_retention"], json!(1), "{out}");
    assert_eq!(on["prunable"], json!(1), "{out}");
}

#[test]
fn a_listed_vmid_that_no_longer_exists_on_shared_storage_takes_no_retention_and_all_its_bytes_are_needed()
 {
    // Arrange: guest 205 is gone from the cluster but listed by job `main`, which sets remove 1 itself, so only the
    // existence rule can stop the job's preview marking the guest's archive prunable.
    let archives = [
        archive(205, 0, 2, false),
        archive(205, 1, 2, false),
        archive(205, 2, 2, false),
    ];
    let payload = backups_payload(
        json!([]),
        json!([{"id": "main", "type": "vzdump", "storage": "backups", "vmid": "205",
                "prune-backups": "keep-last=1", "remove": 1}]),
        &archives,
        vec![("keep-last=1", vec![mark(&archives[0])])],
        Some(json!([defaults_read("n1", 200, json!({"remove": 1}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: no retention and nothing prunable; all three archives (6 GiB) are needed, and the job names 205 as missing.
    let guest = guest_row(&out, 205);
    assert_eq!(guest["guest_exists"], json!(false), "{out}");
    assert_eq!(guest["job_retention"], json!([]), "{out}");
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(guest["retained_at_job_retention"], json!(3), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], json!(6.0), "{out}");
    assert_eq!(
        out["storages"][0]["retention_need_unknown_guests"],
        json!([]),
        "{out}"
    );
    assert_eq!(
        out["storages"][0]["gib_needed_at_job_retention"],
        json!(6.0),
        "{out}"
    );
    assert_eq!(out["jobs"][0]["missing_guests"], json!([205]), "{out}");
}

#[test]
fn a_jobless_existing_guest_needs_all_its_bytes_once_with_its_protected_archive_among_them() {
    // Arrange: guest 900 exists on n1 and no job backs it up. Its newest archive is protected and 2 GiB.
    let archives = [
        archive(900, 0, 1, false),
        archive(900, 1, 1, false),
        archive(900, 2, 2, true),
    ];
    let payload = backups_payload(
        json!([{"vmid": 900, "name": "loner", "node": "n1"}]),
        json!([]),
        &archives,
        vec![],
        Some(json!([defaults_read("n1", 200, json!({"remove": 1}))])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: nothing prunes it, so every archive stays; its 4 GiB are needed once, the protected bytes not twice.
    let guest = guest_row(&out, 900);
    assert_eq!(guest["job_retention"], json!([]), "{out}");
    assert_eq!(guest["retained_at_job_retention"], json!(3), "{out}");
    assert_eq!(guest["gib_needed_at_job_retention"], json!(4.0), "{out}");
    assert_eq!(guest["prunable"], json!(0), "{out}");
    assert_eq!(
        out["storages"][0]["gib_needed_at_job_retention"],
        json!(4.0),
        "{out}"
    );
}

#[test]
fn a_defaults_response_never_prints_its_mailto_script_dumpdir_or_other_keys() {
    // Arrange: the node's defaults carry sentinel strings in every key the receipt must not print.
    let payload = backups_payload(
        json!([]),
        json!([]),
        &[],
        vec![],
        Some(json!([defaults_read(
            "n1",
            200,
            json!({
            "remove": 1, "mailto": "MAILTO-SENTINEL", "script": "SCRIPT-SENTINEL", "dumpdir": "DUMPDIR-SENTINEL",
            "tmpdir": "TMPDIR-SENTINEL", "storage": "STORAGE-SENTINEL", "notes-template": "NOTES-SENTINEL"})
        )])),
    );

    // Act
    let out = receipt(&payload);

    // Assert: no sentinel is printed, and the row carries only node, status, reason and remove.
    let text = out.to_string();
    for sentinel in [
        "MAILTO-SENTINEL",
        "SCRIPT-SENTINEL",
        "DUMPDIR-SENTINEL",
        "TMPDIR-SENTINEL",
        "STORAGE-SENTINEL",
        "NOTES-SENTINEL",
    ] {
        assert!(!text.contains(sentinel), "{sentinel} printed: {text}");
    }
    assert_eq!(
        out["vzdump_defaults"],
        json!([{"node": "n1", "status": "read", "reason": null, "remove": true}]),
        "{text}"
    );
}

#[test]
fn node_local_previews_follow_the_pinned_node_and_shared_previews_every_job() {
    // Arrange: three online nodes, each listing the node-local `local` (dir) and the shared `backups` (nfs).
    // Job `pinned` names n1 and backs `local`; job `any` names no node and backs `local`; job `far` names n2
    // and backs the shared `backups`.
    let listing = |node: &str| {
        json!({"item": node, "status": 200, "json": {"data": [
            {"storage": "local", "content": "backup", "active": 1, "type": "dir", "shared": 0,
             "total": TOTAL, "used": 0, "avail": TOTAL},
            {"storage": "backups", "content": "backup", "active": 1, "type": "nfs", "shared": 1,
             "total": TOTAL, "used": 0, "avail": TOTAL}
        ]}})
    };
    let payload = json!({
        "nodes": [{"node": "n1", "status": "online"}, {"node": "n2", "status": "online"},
                  {"node": "n3", "status": "online"}],
        "jobs": [
            {"id": "pinned", "type": "vzdump", "storage": "local", "node": "n1", "prune-backups": "keep-last=2"},
            {"id": "any", "type": "vzdump", "storage": "local", "prune-backups": "keep-last=3"},
            {"id": "far", "type": "vzdump", "storage": "backups", "node": "n2", "prune-backups": "keep-last=4"}
        ],
        "storage_config": [],
        "storage_listings": [listing("n1"), listing("n2"), listing("n3")]
    });

    // Act
    let out = run("plan", &payload);

    // Assert: `local` is previewed for the job pinned to n1 on n1 alone, and for the unpinned job on every node.
    // `backups` is a shared storage, read once through n1, and it is previewed for the job pinned to n2, because
    // every job on a shared storage can run on any node.
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    let plan: Value = serde_json::from_slice(&out.stdout).expect("the plan is JSON");
    let rows: Vec<Value> = plan["reads"]
        .as_array()
        .expect("plan reads")
        .iter()
        .map(|read| json!([read["node"], read["storage"], read["retentions"]]))
        .collect();
    assert_eq!(
        rows,
        vec![
            json!(["n1", "local", ["keep-last=2", "keep-last=3"]]),
            json!(["n1", "backups", ["keep-last=4"]]),
            json!(["n2", "local", ["keep-last=3"]]),
            json!(["n3", "local", ["keep-last=3"]]),
        ],
        "{plan}"
    );
}

/// `ceph_tree()` with each OSD's `ceph_version_short` set in OSD id order (osd.0, osd.1, osd.2); `None` removes it.
fn tree_with_versions(versions: [Option<&str>; 3]) -> Value {
    let mut tree = ceph_tree();
    for ((host, osd), version) in [(0, 0), (0, 1), (1, 0)].into_iter().zip(versions) {
        let leaf = tree["root"]["children"][host]["children"][osd]
            .as_object_mut()
            .expect("an OSD object");
        match version {
            Some(version) => {
                leaf.insert("ceph_version_short".to_owned(), json!(version));
            }
            None => {
                leaf.remove("ceph_version_short");
            }
        }
    }
    tree
}

/// A payload over one CephFS storage whose only pool `a_data` reports `percent_used`, Ceph's own figure. The OSD tree
/// is read as `osd` when given, and not read when it is `None`.
fn scale_payload(percent_used: Value, osd: Option<Value>) -> Value {
    let mut payload = cephfs_payload(
        &[("cephA", 10_737_418_240)],
        json!([{"storage": "cephA", "type": "cephfs", "content": "backup", "fs-name": "fsA"}]),
        json!([{"name": "fsA", "data_pools": ["a_data"]}]),
    );
    payload["ceph_reads"][1]["json"]["data"] = json!([{"pool_name": "a_data", "bytes_used": 1073741824,
        "percent_used": percent_used, "type": "replicated", "size": 3}]);
    if let Some(osd) = osd {
        payload["ceph_capacity_reads"] = json!([osd, status_read(200, ceph_status())]);
    }
    payload
}

#[test]
fn a_pool_percent_is_scaled_when_every_osd_runs_ceph_13_or_later() {
    // Arrange: every OSD runs a Ceph 19 release, so the pool's percent_used is Ceph's fraction of 1 (0.3736).
    let osd = osd_read(
        200,
        tree_with_versions([Some("19.2.0"), Some("19.2.0"), Some("19.1.1")]),
    );
    let payload = scale_payload(json!(0.3736), Some(osd));

    // Act
    let out = receipt(&payload);

    // Assert: the fraction is multiplied by 100 and rounded to one decimal, and the scale names that.
    assert_eq!(
        out["cephfs"]["percent_scale"],
        json!("fraction, scaled (Ceph 13 or later)"),
        "{out}"
    );
    assert_eq!(
        out["cephfs"]["pools"][0]["ceph_percent_used"],
        json!(37.4),
        "{out}"
    );
}

#[test]
fn a_pool_percent_is_read_as_reported_on_ceph_12_and_scaled_from_13_on() {
    // Arrange: first every OSD on Ceph 12.2.13, whose pool percent_used is already a percent (37.36); then
    // every OSD on Ceph 13.2.0, the first release whose percent_used is a fraction (0.3736).
    let twelve = scale_payload(
        json!(37.36),
        Some(osd_read(
            200,
            tree_with_versions([Some("12.2.13"), Some("12.2.13"), Some("12.2.13")]),
        )),
    );
    let thirteen = scale_payload(
        json!(0.3736),
        Some(osd_read(
            200,
            tree_with_versions([Some("13.2.0"), Some("13.2.0"), Some("13.2.0")]),
        )),
    );

    // Act
    let (twelve, thirteen) = (receipt(&twelve), receipt(&thirteen));

    // Assert: 12 is not multiplied, 13 is; both print 37.4.
    assert_eq!(
        twelve["cephfs"]["percent_scale"],
        json!("percent, as reported (Ceph 12 or earlier)"),
        "{twelve}"
    );
    assert_eq!(
        twelve["cephfs"]["pools"][0]["ceph_percent_used"],
        json!(37.4),
        "{twelve}"
    );
    assert_eq!(
        thirteen["cephfs"]["percent_scale"],
        json!("fraction, scaled (Ceph 13 or later)"),
        "{thirteen}"
    );
    assert_eq!(
        thirteen["cephfs"]["pools"][0]["ceph_percent_used"],
        json!(37.4),
        "{thirteen}"
    );
}

#[test]
fn a_pool_percent_is_null_when_the_osds_run_releases_on_both_sides_of_13() {
    // Arrange: osd.0 on Ceph 12, osd.1 and osd.2 on Ceph 19. Neither scale is right for every OSD's report.
    let payload = scale_payload(
        json!(0.3736),
        Some(osd_read(
            200,
            tree_with_versions([Some("12.2.13"), Some("19.2.0"), Some("19.2.0")]),
        )),
    );

    // Act
    let out = receipt(&payload);

    // Assert: the percent is unknown, not guessed, and the scale says why.
    assert_eq!(
        out["cephfs"]["percent_scale"],
        json!("unknown: OSDs report Ceph releases on both sides of 13"),
        "{out}"
    );
    assert_eq!(
        out["cephfs"]["pools"][0]["ceph_percent_used"],
        json!(null),
        "{out}"
    );
    assert_eq!(
        out["cephfs"]["pools"][0]["ceph_used_gib"],
        json!(1.0),
        "{out}"
    );
}

#[test]
fn a_pool_percent_is_null_when_an_osd_reports_no_readable_ceph_version() {
    // Arrange: osd.1 carries no version at all, then osd.0 carries one that names no major release.
    for versions in [
        [Some("19.2.0"), None, Some("19.2.0")],
        [Some("v19.2"), Some("19.2.0"), Some("19.2.0")],
    ] {
        let payload = scale_payload(
            json!(0.3736),
            Some(osd_read(200, tree_with_versions(versions))),
        );

        // Act
        let out = receipt(&payload);

        // Assert
        assert_eq!(
            out["cephfs"]["percent_scale"],
            json!("unknown: an OSD reports no Ceph version"),
            "{out}"
        );
        assert_eq!(
            out["cephfs"]["pools"][0]["ceph_percent_used"],
            json!(null),
            "{out}"
        );
    }
}

#[test]
fn a_pool_percent_is_null_when_the_osd_tree_was_not_read_or_names_no_osd() {
    // Arrange: no OSD tree read at all; the tree answers 403; and a tree with a root but no OSD in it.
    let empty = json!({"root": {"type": "root", "name": "default", "children": []}});
    for (osd, reason) in [
        (None, "unknown: the Ceph OSD tree was not read"),
        (
            Some(osd_read(403, Value::Null)),
            "unknown: the Ceph OSD tree was not read",
        ),
        (
            Some(osd_read(200, empty)),
            "unknown: the Ceph OSD tree lists no OSD",
        ),
    ] {
        let payload = scale_payload(json!(0.3736), osd);

        // Act
        let out = receipt(&payload);

        // Assert: the percent is null, never the figure as Ceph sent it.
        assert_eq!(out["cephfs"]["percent_scale"], json!(reason), "{out}");
        assert_eq!(
            out["cephfs"]["pools"][0]["ceph_percent_used"],
            json!(null),
            "{out}"
        );
    }
}
