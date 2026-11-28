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

    // Assert: the whole cephfs section, the pool's usage rounded as given, and no sentinel printed.
    assert!(
        out.contains(r#""cephfs": {"status": "read", "reason": null, "storages": [{"storage": "cephA", "external": false, "filesystem": "cephfs", "filesystem_source": "only filesystem", "data_pools": ["cephfs_data"], "data_pools_complete": true, "shares_filesystem_with": ["cephB"]}, {"storage": "cephB", "external": false, "filesystem": "cephfs", "filesystem_source": "only filesystem", "data_pools": ["cephfs_data"], "data_pools_complete": true, "shares_filesystem_with": ["cephA"]}, {"storage": "cephExt", "external": true, "filesystem": null, "filesystem_source": "external", "data_pools": [], "data_pools_complete": null, "shares_filesystem_with": []}], "pools": [{"pool": "cephfs_data", "ceph_used_gib": 1.0, "ceph_percent_used": 37.4, "type": "replicated", "size": 3}]}"#),
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
        out.contains(r#""cephfs": {"status": "unavailable", "reason": "HTTP 403: needs Sys.Audit or Datastore.Audit on /", "storages": [{"storage": "cephA", "external": false, "filesystem": null, "filesystem_source": "unresolved", "data_pools": [], "data_pools_complete": null, "shares_filesystem_with": null}], "pools": []}"#),
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
    run_text(mode, &payload.to_string())
}

/// Runs the helper in `mode` on raw stdin text, which need not be JSON, and returns the whole process output.
fn run_text(mode: &str, stdin: &str) -> std::process::Output {
    let home = TempDir::new().expect("a temporary home");
    let script = repo::path(HELPER);
    scrubbed("python3", home.path())
        .args(["-I", script.to_str().expect("utf-8 path"), mode])
        .write_stdin(stdin)
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

/// The step names the helper declares, in order: the strings of its `STEPS` tuple.
fn helper_steps() -> Vec<String> {
    let source = std::fs::read_to_string(repo::path(HELPER)).expect("the helper source");
    let open = "STEPS = (";
    let start = source.find(open).expect("a STEPS tuple") + open.len();
    let end = start
        + source[start..]
            .find(')')
            .expect("the end of the STEPS tuple");
    source[start..end]
        .split('"')
        .skip(1)
        .step_by(2)
        .map(str::to_owned)
        .collect()
}

/// A payload the helper summarizes (exit 0): one own CephFS storage `cephA` and two node-local directory storages
/// `a` and `b` on n1, each with an empty content read, both Ceph reads answered, and an empty disk listing.
fn base_payload() -> Value {
    json!({
        "plan": {"reads": [
            {"node": "n1", "storage": "cephA", "status": {"type": "cephfs", "shared": 1, "total": TOTAL, "used": 1, "avail": TOTAL - 1}},
            {"node": "n1", "storage": "a", "status": {"type": "dir", "shared": 0, "total": TOTAL, "used": 2, "avail": TOTAL - 2}},
            {"node": "n1", "storage": "b", "status": {"type": "dir", "shared": 0, "total": TOTAL, "used": 3, "avail": TOTAL - 3}}
        ]},
        "vms": [], "jobs": [], "prune_reads": [],
        "storage_config": [
            {"storage": "cephA", "type": "cephfs", "content": "backup"},
            {"storage": "a", "type": "dir", "content": "backup"},
            {"storage": "b", "type": "dir", "content": "backup"}
        ],
        "content_reads": [
            {"item": {"node": "n1", "storage": "cephA"}, "status": 200, "json": {"data": []}},
            {"item": {"node": "n1", "storage": "a"}, "status": 200, "json": {"data": []}},
            {"item": {"node": "n1", "storage": "b"}, "status": 200, "json": {"data": []}}
        ],
        "ceph_reads": [
            {"item": "fs", "status": 200, "json": {"data": []}},
            {"item": "pool", "status": 200, "json": {"data": []}}
        ],
        "ceph_capacity_reads": [osd_read(200, ceph_tree()), status_read(200, ceph_status())],
        "disk_reads": [{"item": "n1", "status": 200, "json": {"data": []}}]
    })
}

#[test]
fn a_refusal_for_unexpected_data_names_the_step_that_failed_and_its_error_class() {
    // Arrange: one row per step of the helper. Each row breaks one part of `base_payload`, so the step it names is
    // the part broken. `input` sends text that is not JSON; `plan` runs the plan mode on a payload without `nodes`.
    let without = |key: &str| {
        let mut payload = base_payload();
        payload.as_object_mut().expect("an object").remove(key);
        payload.to_string()
    };
    let mut storages_row = base_payload();
    storages_row["plan"] = json!({});
    let mut cephfs_row = base_payload();
    cephfs_row["ceph_reads"] = json!(["not-a-read"]);
    let mut shared_row = base_payload();
    shared_row["storage_config"][1]["path"] = json!(5);
    let mut capacity_row = base_payload();
    capacity_row["ceph_capacity_reads"] = json!(["not-a-read"]);
    let mut osd_row = base_payload();
    osd_row["ceph_capacity_reads"][0] = osd_read(
        200,
        json!({"root": {"type": "osd", "id": 1, "name": "osd.1", "total_space": 1, "bytes_used": 1,
                        "percent_used": "x"}}),
    );
    let mut health_row = base_payload();
    health_row["ceph_capacity_reads"][1] =
        json!({"item": "cluster/ceph/status", "status": 200, "json": "x"});
    let mut disks_row = base_payload();
    disks_row["disk_reads"] = json!(["not-a-read"]);
    // (step, mode, stdin, the refusal's text after its prefix)
    let rows: Vec<(&str, &str, String, &str)> = vec![
        (
            "input",
            "summarize",
            "not json".to_owned(),
            "input: JSONDecodeError",
        ),
        (
            "plan",
            "plan",
            json!({"storage_listings": [], "jobs": [], "storage_config": []}).to_string(),
            "plan: KeyError nodes",
        ),
        ("vms", "summarize", without("vms"), "vms: KeyError vms"),
        ("jobs", "summarize", without("jobs"), "jobs: KeyError jobs"),
        (
            "reads",
            "summarize",
            without("content_reads"),
            "reads: KeyError content_reads",
        ),
        (
            "storages",
            "summarize",
            storages_row.to_string(),
            "storages: KeyError reads",
        ),
        (
            "cephfs",
            "summarize",
            cephfs_row.to_string(),
            "cephfs: AttributeError",
        ),
        (
            "shared-backends",
            "summarize",
            shared_row.to_string(),
            "shared-backends: AttributeError",
        ),
        (
            "ceph-capacity",
            "summarize",
            capacity_row.to_string(),
            "ceph-capacity: AttributeError",
        ),
        ("osd", "summarize", osd_row.to_string(), "osd: ValueError"),
        (
            "health",
            "summarize",
            health_row.to_string(),
            "health: AttributeError",
        ),
        (
            "disks",
            "summarize",
            disks_row.to_string(),
            "disks: AttributeError",
        ),
    ];
    let labels: Vec<String> = rows.iter().map(|row| row.0.to_owned()).collect();
    assert_eq!(labels, helper_steps(), "one row per STEPS label, in order");

    for (step, mode, stdin, tail) in rows {
        // Act
        let out = run_text(mode, &stdin);

        // Assert: exit 1, and the refusal is the exact text naming this step and class.
        assert_eq!(
            out.status.code(),
            Some(1),
            "step {step}: {}",
            String::from_utf8_lossy(&out.stdout)
        );
        let body: Value = serde_json::from_slice(&out.stdout).expect("a refusal is JSON");
        let expected = format!("Proxmox returned data this survey does not recognise: {tail}");
        assert_eq!(
            body["refusal"].as_str(),
            Some(expected.as_str()),
            "step {step}"
        );
    }
}

#[test]
fn a_value_in_the_data_never_prints_in_the_refusal() {
    // Arrange: a content-read volume whose size is a string int() cannot read. int() quotes that string in its
    // message, and the refusal must not carry it.
    let mut payload = base_payload();
    payload["content_reads"][0]["json"]["data"] = json!([
        {"volid": "cephA:backup/vzdump-qemu-200.vma.zst", "vmid": 200, "size": "SENTINEL-VALUE-9f3", "ctime": 1790000000}
    ]);

    // Act
    let text = refusal(&payload);

    // Assert
    assert_eq!(
        text, "Proxmox returned data this survey does not recognise: storages: ValueError",
        "{text}"
    );
    assert!(!text.contains("SENTINEL"), "{text}");
}

#[test]
fn a_key_that_is_data_prints_as_a_placeholder_and_a_missing_field_name_prints() {
    // Arrange: a prune preview names a storage with no content read, so the lookup's key is a tuple built from data.
    let mut payload = base_payload();
    payload["prune_reads"] = json!([
        {"item": [{"node": "n1", "storage": "KEY-SENTINEL-7x"}, "keep-last=1"], "status": 200, "json": {"data": []}}
    ]);

    // Act
    let text = refusal(&payload);

    // Assert: the key is not printed, only the placeholder.
    assert_eq!(
        text, "Proxmox returned data this survey does not recognise: reads: KeyError <key>",
        "{text}"
    );
    assert!(!text.contains("KEY-SENTINEL"), "{text}");

    // Arrange: the base payload with no `vms` key, a plain missing field.
    let mut payload = base_payload();
    payload.as_object_mut().expect("an object").remove("vms");

    // Act
    let text = refusal(&payload);

    // Assert: the field name is printed.
    assert_eq!(
        text, "Proxmox returned data this survey does not recognise: vms: KeyError vms",
        "{text}"
    );
}
