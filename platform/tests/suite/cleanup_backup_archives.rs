//! The backup-archive cleanup's helper, `platform/playbooks/files/cleanup_backup_archives.py` (cli tier):
//! which archives `list` selects (gone guests and declared pairs only), its refusals, the sha256 confirm
//! gate, the permission check `authorize` makes before any delete, and the read-back `verify` makes after.
//!
//! Each test runs the helper on a hand-written payload with the shape the playbook sends, and every
//! guard test uses a payload where that guard alone decides the outcome.

use agent_cloud_tests::process::scrubbed;
use agent_cloud_tests::repo;
use serde_json::{Value, json};
use tempfile::TempDir;

const HELPER: &str = "platform/playbooks/files/cleanup_backup_archives.py";
const GIB: i64 = 1_073_741_824;

/// Runs the helper in `mode` on `payload`; returns (exit 0, parsed stdout).
fn run(mode: &str, payload: &Value) -> (bool, Value) {
    let home = TempDir::new().expect("a temporary home");
    let script = repo::path(HELPER);
    let out = scrubbed("python3", home.path())
        .args(["-I", script.to_str().expect("utf-8 path"), mode])
        .write_stdin(payload.to_string())
        .assert()
        .get_output()
        .clone();
    let stdout: Value = serde_json::from_slice(&out.stdout).expect("JSON stdout");
    (out.status.success(), stdout)
}

fn ok(mode: &str, payload: &Value) -> Value {
    let (success, out) = run(mode, payload);
    assert!(success, "{out}");
    out
}

fn refusal(mode: &str, payload: &Value) -> String {
    let (success, out) = run(mode, payload);
    assert!(!success, "expected a refusal: {out}");
    out["refusal"].as_str().expect("refusal text").to_string()
}

fn archive(storage: &str, vmid: i64, n: i64) -> Value {
    json!({"volid": format!("{storage}:backup/vzdump-qemu-{vmid}-{n}.vma.zst"), "content": "backup",
           "vmid": vmid, "size": GIB * n, "ctime": 1_790_000_000 + n})
}

fn read(node: &str, storage: &str, volumes: Vec<Value>) -> Value {
    json!({"item": {"node": node, "storage": storage}, "status": 200, "json": {"data": volumes}})
}

/// A list payload: n1 online, live guest 100, dry run. Storages read: `backups` (shared CephFS on this
/// cluster's own Ceph), `local` (node-local dir) and `nfs` (a shared NFS export another cluster could use).
fn base(content: Vec<Value>) -> Value {
    json!({
        "nodes": [{"node": "n1", "status": "online"}],
        "vms": [{"vmid": 100, "name": "web", "node": "n1"}],
        "jobs": [],
        "storage_config": [{"storage": "backups", "type": "cephfs"}, {"storage": "local", "type": "dir"},
                           {"storage": "nfs", "type": "nfs"}],
        "plan": {"reads": [{"node": "n1", "storage": "backups", "status": {"type": "cephfs", "shared": 1}},
                           {"node": "n1", "storage": "local", "status": {"type": "dir", "shared": 0}},
                           {"node": "n1", "storage": "nfs", "status": {"type": "nfs", "shared": 1}}]},
        "content_reads": content,
        "pairs": [],
        "confirm_delete": "",
        "check_mode": true
    })
}

fn volids(out: &Value) -> Vec<String> {
    out["volumes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v["volid"].as_str().unwrap().to_string())
        .collect()
}

#[test]
fn an_archive_of_a_gone_guest_is_listed() {
    // Arrange: guest 900 is not in the cluster's resources; 100 is.
    let p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1), archive("backups", 100, 2)],
    )]);
    // Act
    let out = ok("list", &p);
    // Assert
    assert_eq!(volids(&out), ["backups:backup/vzdump-qemu-900-1.vma.zst"]);
    assert_eq!(out["volumes"][0]["source"], "gone");
    assert_eq!(out["volumes"][0]["node"], "n1");
    assert_eq!(out["volumes"][0]["guest"], Value::Null);
}

#[test]
fn an_archive_matching_a_declared_pair_is_listed() {
    // Arrange: live guest 100 declared on `backups`, no job.
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    p["pairs"] =
        json!([{"vmid": "100", "storage": "backups"}, {"vmid": 100, "storage": "backups"}]);
    // Act
    let out = ok("list", &p);
    // Assert: listed once (duplicate pair dedupes), named, source declared.
    assert_eq!(out["count"], 1);
    assert_eq!(out["volumes"][0]["source"], "declared");
    assert_eq!(out["volumes"][0]["guest"], "web");
}

#[test]
fn a_declared_pair_an_enabled_job_still_backs_up_is_refused_naming_the_job() {
    // Arrange: enabled job backup-7 sends guest 100 to `backups`.
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    p["pairs"] = json!([{"vmid": 100, "storage": "backups"}]);
    p["jobs"] = json!([{"id": "backup-7", "type": "vzdump", "storage": "backups", "vmid": "100"}]);
    // Act
    let text = refusal("list", &p);
    // Assert
    assert!(
        text.contains("backup-7") && text.contains("100") && text.contains("backups"),
        "{text}"
    );
}

#[test]
fn a_declared_pair_whose_job_is_disabled_is_listed() {
    // Arrange: the same job, disabled.
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    p["pairs"] = json!([{"vmid": 100, "storage": "backups"}]);
    p["jobs"] = json!([{"id": "backup-7", "type": "vzdump", "storage": "backups", "vmid": "100", "enabled": 0}]);
    // Act
    let out = ok("list", &p);
    // Assert
    assert_eq!(out["count"], 1);
}

#[test]
fn a_declared_storage_this_run_did_not_read_is_refused() {
    // Arrange
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    p["pairs"] = json!([{"vmid": 100, "storage": "elsewhere"}]);
    // Act
    let text = refusal("list", &p);
    // Assert
    assert!(text.contains("elsewhere"), "{text}");
}

#[test]
fn malformed_declared_pairs_are_refused() {
    for (pairs, needle) in [
        (
            json!([{"vmid": 100, "storage": "backups", "node": "n1"}]),
            "exactly the keys",
        ),
        (
            json!([{"vmid": "abc", "storage": "backups"}]),
            "positive integer",
        ),
        (
            json!([{"vmid": 100, "storage": "../x"}]),
            "malformed storage",
        ),
    ] {
        // Arrange: an archive that would otherwise be listed as gone, so only the pair refuses.
        let mut p = base(vec![read(
            "n1",
            "backups",
            vec![archive("backups", 900, 1)],
        )]);
        p["pairs"] = pairs;
        // Act
        let text = refusal("list", &p);
        // Assert
        assert!(text.contains(needle), "{text}");
    }
}

#[test]
fn an_offline_node_refuses_the_gone_source() {
    // Arrange: a gone-guest archive and node n2 offline.
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1)],
    )]);
    p["nodes"] = json!([{"node": "n1", "status": "online"}, {"node": "n2", "status": "offline"}]);
    // Act
    let text = refusal("list", &p);
    // Assert
    assert!(
        text.contains("n2 is offline") && text.contains("no archive is listed"),
        "{text}"
    );
}

#[test]
fn an_offline_node_refuses_a_declared_pair_too() {
    // Arrange: only a declared pair (no gone guest) and node n2 offline.
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    p["pairs"] = json!([{"vmid": 100, "storage": "backups"}]);
    p["nodes"] = json!([{"node": "n1", "status": "online"}, {"node": "n2", "status": "offline"}]);
    // Act
    let text = refusal("list", &p);
    // Assert
    assert!(text.contains("n2 is offline"), "{text}");
}

#[test]
fn a_protected_archive_of_a_gone_guest_is_reported_not_listed() {
    // Arrange
    let mut prot = archive("backups", 900, 2);
    prot["protected"] = json!(1);
    let p = base(vec![read("n1", "backups", vec![prot])]);
    // Act
    let out = ok("list", &p);
    // Assert
    assert_eq!(out["count"], 0);
    assert_eq!(
        out["not_listed"]["backups"]["protected"],
        json!({"count": 1, "gib": 2.0})
    );
}

#[test]
fn an_archive_without_a_vmid_is_not_listed() {
    // Arrange
    let p = base(vec![read(
        "n1",
        "backups",
        vec![
            json!({"volid": "backups:backup/odd.tar", "content": "backup", "size": GIB, "ctime": 1}),
        ],
    )]);
    // Act
    let out = ok("list", &p);
    // Assert
    assert_eq!(out["count"], 0);
    assert_eq!(out["not_listed"]["backups"]["no_vmid"]["count"], 1);
}

#[test]
fn archives_of_live_guests_are_kept_with_or_without_a_job() {
    // Arrange: guest 100 has a job on `backups`; guest 101 lives, is on `local`, no job, not declared.
    let mut p = base(vec![
        read("n1", "backups", vec![archive("backups", 100, 1)]),
        read("n1", "local", vec![archive("local", 101, 1)]),
    ]);
    p["vms"] = json!([{"vmid": 100, "name": "web", "node": "n1"}, {"vmid": 101, "name": "db", "node": "n1"}]);
    p["jobs"] = json!([{"id": "j", "type": "vzdump", "storage": "backups", "vmid": "100"}]);
    // Act
    let out = ok("list", &p);
    // Assert
    assert_eq!(out["count"], 0);
    assert_eq!(out["not_listed"]["backups"]["kept"]["count"], 1);
    assert_eq!(out["not_listed"]["local"]["kept"]["count"], 1);
}

#[test]
fn the_sha_ignores_content_order_and_the_node_read_through() {
    // Arrange
    let a = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1), archive("backups", 901, 2)],
    )]);
    let mut b = base(vec![read(
        "n2",
        "backups",
        vec![archive("backups", 901, 2), archive("backups", 900, 1)],
    )]);
    b["plan"] = json!({"reads": [{"node": "n2", "storage": "backups"}]});
    // Act / Assert
    assert_eq!(ok("list", &a)["sha256"], ok("list", &b)["sha256"]);
}

#[test]
fn the_sha_changes_when_a_size_changes() {
    // Arrange
    let a = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1)],
    )]);
    let mut v = archive("backups", 900, 1);
    v["size"] = json!(GIB + 1);
    let b = base(vec![read("n1", "backups", vec![v])]);
    // Act / Assert
    assert_ne!(ok("list", &a)["sha256"], ok("list", &b)["sha256"]);
}

#[test]
fn the_sha_differs_for_one_filename_on_two_storages() {
    // Arrange: the same file name, once on `backups` and once on `local`.
    let a = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1)],
    )]);
    let b = base(vec![read("n1", "local", vec![archive("local", 900, 1)])]);
    // Act / Assert
    assert_ne!(ok("list", &a)["sha256"], ok("list", &b)["sha256"]);
}

#[test]
fn a_real_run_with_the_wrong_confirm_is_refused_naming_the_sha() {
    // Arrange
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1)],
    )]);
    let sha = ok("list", &p)["sha256"].as_str().unwrap().to_string();
    p["check_mode"] = json!(false);
    p["confirm_delete"] = json!("0".repeat(64));
    // Act
    let text = refusal("list", &p);
    // Assert
    assert!(text.contains(&sha), "{text}");
}

#[test]
fn a_real_run_with_the_right_confirm_deletes() {
    // Arrange
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1)],
    )]);
    p["confirm_delete"] = ok("list", &p)["sha256"].clone();
    p["check_mode"] = json!(false);
    // Act / Assert
    assert_eq!(ok("list", &p)["delete"], true);
}

#[test]
fn check_mode_with_the_right_confirm_does_not_delete() {
    // Arrange
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1)],
    )]);
    p["confirm_delete"] = ok("list", &p)["sha256"].clone();
    // Act / Assert
    assert_eq!(ok("list", &p)["delete"], false);
}

#[test]
fn an_empty_list_does_not_delete_and_is_not_refused() {
    // Arrange: a real run, nothing to list, no confirm.
    let mut p = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    p["check_mode"] = json!(false);
    // Act
    let out = ok("list", &p);
    // Assert
    assert_eq!(out["delete"], false);
}

fn perm(path: &str, privs: Value) -> Value {
    json!({"item": path, "status": 200, "json": {"data": {path: privs}}})
}

fn auth_payload(storage_privs: Value, vm_privs: Value) -> Value {
    json!({"volumes": [{"volid": "backups:backup/x", "storage": "backups", "vmid": 900}],
           "permission_reads": [perm("/storage/backups", storage_privs), perm("/vms/900", vm_privs)]})
}

#[test]
fn authorize_passes_when_every_privilege_is_held() {
    let p = auth_payload(
        json!({"Datastore.AllocateSpace": 1}),
        json!({"VM.Backup": 1, "VM.Audit": 1}),
    );
    assert_eq!(ok("authorize", &p)["authorized"], 1);
}

#[test]
fn authorize_counts_a_privilege_held_without_propagation() {
    // Arrange: Proxmox maps each held privilege to its propagate flag; 0 still means held
    // (pve-access-control RPCEnvironment.pm `check` tests only that the key is defined).
    let p = auth_payload(
        json!({"Datastore.AllocateSpace": 0}),
        json!({"VM.Backup": 0, "VM.Audit": 0}),
    );

    // Act + Assert
    assert_eq!(ok("authorize", &p)["authorized"], 1);
}

#[test]
fn authorize_refuses_a_missing_vm_backup_naming_its_path() {
    let p = auth_payload(
        json!({"Datastore.AllocateSpace": 1}),
        json!({"VM.Audit": 1}),
    );
    let text = refusal("authorize", &p);
    assert!(text.contains("VM.Backup on /vms/900"), "{text}");
}

#[test]
fn authorize_refuses_datastore_allocate_without_allocate_space() {
    let p = auth_payload(json!({"Datastore.Allocate": 1}), json!({"VM.Backup": 1}));
    let text = refusal("authorize", &p);
    assert!(
        text.contains("Datastore.AllocateSpace on /storage/backups"),
        "{text}"
    );
}

#[test]
fn authorize_refuses_a_permission_read_answered_403() {
    let mut p = auth_payload(
        json!({"Datastore.AllocateSpace": 1}),
        json!({"VM.Backup": 1}),
    );
    p["permission_reads"][1] = json!({"item": "/vms/900", "status": 403, "json": null});
    let text = refusal("authorize", &p);
    assert!(text.contains("403") && text.contains("/vms/900"), "{text}");
}

fn verify_payload(after: Vec<Value>) -> Value {
    json!({"volumes": [{"volid": "backups:backup/vzdump-qemu-900-1.vma.zst"}],
           "before": [read("n1", "backups", vec![archive("backups", 900, 1), archive("backups", 100, 2)])],
           "after": [read("n1", "backups", after)]})
}

#[test]
fn verify_passes_when_only_the_listed_archive_is_gone() {
    let out = ok("verify", &verify_payload(vec![archive("backups", 100, 2)]));
    assert_eq!(out, json!({"deleted": 1, "new_archives": 0}));
}

#[test]
fn verify_refuses_a_listed_archive_still_present() {
    let text = refusal(
        "verify",
        &verify_payload(vec![archive("backups", 900, 1), archive("backups", 100, 2)]),
    );
    assert!(text.contains("900-1"), "{text}");
}

#[test]
fn verify_refuses_an_unlisted_archive_that_disappeared() {
    let text = refusal("verify", &verify_payload(vec![]));
    assert!(text.contains("100-2"), "{text}");
}

#[test]
fn verify_counts_an_archive_that_appeared() {
    let out = ok(
        "verify",
        &verify_payload(vec![archive("backups", 100, 2), archive("backups", 100, 3)]),
    );
    assert_eq!(out["new_archives"], 1);
}

#[test]
fn a_gone_guest_on_a_shared_storage_another_cluster_could_use_is_kept() {
    // Arrange: guest 900 is not in this cluster. It has an archive on the shared NFS storage, where the
    // vmid may be another cluster's guest, and one on node-local `local`.
    let payload = base(vec![
        read("n1", "nfs", vec![archive("nfs", 900, 1)]),
        read("n1", "local", vec![archive("local", 900, 2)]),
    ]);

    // Act
    let out = ok("list", &payload);

    // Assert: only the node-local archive is listed; the NFS one is counted as gone_on_shared.
    assert_eq!(
        volids(&out),
        vec!["local:backup/vzdump-qemu-900-2.vma.zst"],
        "{out}"
    );
    assert_eq!(
        out["not_listed"]["nfs"]["gone_on_shared"]["count"], 1,
        "{out}"
    );
}

#[test]
fn a_gone_guest_on_an_external_cephfs_is_kept() {
    // Arrange: `backups` is CephFS with a monitor list, so it is another cluster's Ceph.
    let mut payload = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 900, 1)],
    )]);
    payload["storage_config"][0]["monhost"] = json!("ceph-mon.example");

    // Act
    let out = ok("list", &payload);

    // Assert
    assert_eq!(out["count"], 0, "{out}");
    assert_eq!(
        out["not_listed"]["backups"]["gone_on_shared"]["count"], 1,
        "{out}"
    );
}

#[test]
fn a_declared_pair_on_a_shared_storage_is_listed() {
    // Arrange: the operator declares guest 900's archives on the NFS storage.
    let mut payload = base(vec![read("n1", "nfs", vec![archive("nfs", 900, 1)])]);
    payload["pairs"] = json!([{"vmid": 900, "storage": "nfs"}]);

    // Act
    let out = ok("list", &payload);

    // Assert
    assert_eq!(
        volids(&out),
        vec!["nfs:backup/vzdump-qemu-900-1.vma.zst"],
        "{out}"
    );
    assert_eq!(out["volumes"][0]["source"], "declared", "{out}");
}

#[test]
fn a_storage_whose_sharing_is_unknown_counts_as_shared() {
    // Arrange: `local` reports no `shared` field and is not CephFS, so whether another cluster
    // writes to it is unknown.
    let mut payload = base(vec![read("n1", "local", vec![archive("local", 900, 1)])]);
    payload["plan"]["reads"][1]["status"] = json!({"type": "dir"});

    // Act
    let out = ok("list", &payload);

    // Assert
    assert_eq!(out["count"], 0, "{out}");
    assert_eq!(
        out["not_listed"]["local"]["gone_on_shared"]["count"], 1,
        "{out}"
    );
}

#[test]
fn authorize_requires_vm_audit_on_every_listed_guest() {
    // Arrange: Proxmox's guest listing hides a guest the token lacks VM.Audit on. Both sources trust it:
    // a gone guest may be a live one the token cannot see, and a declared pair's guest may be one an
    // enabled all or pool job still backs up without the job's guest set showing it.
    for source in ["gone", "declared"] {
        let mut without = auth_payload(
            json!({"Datastore.AllocateSpace": 1}),
            json!({"VM.Backup": 1}),
        );
        without["volumes"][0]["source"] = json!(source);
        let mut with = auth_payload(
            json!({"Datastore.AllocateSpace": 1}),
            json!({"VM.Backup": 1, "VM.Audit": 0}),
        );
        with["volumes"][0]["source"] = json!(source);

        // Act
        let text = refusal("authorize", &without);

        // Assert: refused without VM.Audit, naming it; authorized with it (a held privilege, propagate 0).
        assert!(text.contains("VM.Audit on /vms/900"), "{source}: {text}");
        assert_eq!(ok("authorize", &with)["authorized"], 1, "{source}");
    }
}

#[test]
fn a_declared_pair_an_enabled_job_without_a_storage_backs_up_is_refused() {
    // Arrange: an enabled job names no storage, so vzdump writes to its default storage; it backs
    // up guest 100, which is declared on `backups`.
    let mut payload = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    payload["jobs"] = json!([{"id": "job-default", "type": "vzdump", "enabled": 1, "vmid": "100"}]);
    payload["pairs"] = json!([{"vmid": 100, "storage": "backups"}]);

    // Act
    let text = refusal("list", &payload);

    // Assert
    assert!(text.contains("job-default"), "{text}");
}

#[test]
fn backup_jobs_the_token_may_not_list_refuse_the_run_naming_sys_audit() {
    // Arrange: no job list (the play refuses a 403 before this; the lister refuses too).
    let mut payload = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    payload["jobs"] = Value::Null;

    // Act
    let text = refusal("list", &payload);

    // Assert
    assert!(text.contains("Sys.Audit on /"), "{text}");
}

#[test]
fn an_enabled_pool_job_that_could_cover_a_declared_archive_names_its_pool() {
    // Arrange: guest 100 shows no pool, as Proxmox shows it to a token without Pool.Audit on the pool,
    // so the pool job's guests look empty and the declared pair is listed. One job writes the declared
    // storage, one names no storage (vzdump's default), one writes elsewhere, one is disabled.
    let mut payload = base(vec![read(
        "n1",
        "backups",
        vec![archive("backups", 100, 1)],
    )]);
    payload["jobs"] = json!([
        {"id": "on-backups", "type": "vzdump", "enabled": 1, "pool": "prod", "storage": "backups"},
        {"id": "default", "type": "vzdump", "enabled": 1, "pool": "lab"},
        {"id": "elsewhere", "type": "vzdump", "enabled": 1, "pool": "other", "storage": "nfs"},
        {"id": "off", "type": "vzdump", "enabled": 0, "pool": "old", "storage": "backups"}
    ]);
    payload["pairs"] = json!([{"vmid": 100, "storage": "backups"}]);
    let mut gone_only = base(vec![read("n1", "local", vec![archive("local", 900, 1)])]);
    gone_only["jobs"] = payload["jobs"].clone();

    // Act
    let out = ok("list", &payload);
    let gone = ok("list", &gone_only);

    // Assert: the two pools that could hold the declared guest are named and their ACL paths read;
    // with no declared archive listed, no pool is needed.
    assert_eq!(out["pools"], json!(["lab", "prod"]), "{out}");
    let paths = out["acl_paths"].as_array().unwrap();
    assert!(
        paths.contains(&json!("/pool/lab")) && paths.contains(&json!("/pool/prod")),
        "{out}"
    );
    assert_eq!(gone["pools"], json!([]), "{gone}");
}

#[test]
fn authorize_requires_pool_audit_on_a_named_pool() {
    // Arrange: the token holds every per-archive privilege; the pool is readable in one case only.
    let mut without = auth_payload(
        json!({"Datastore.AllocateSpace": 1}),
        json!({"VM.Backup": 1, "VM.Audit": 1}),
    );
    without["pools"] = json!(["prod"]);
    without["permission_reads"]
        .as_array_mut()
        .unwrap()
        .push(perm("/pool/prod", json!({})));
    let mut with = without.clone();
    with["permission_reads"][2] = perm("/pool/prod", json!({"Pool.Audit": 0}));

    // Act
    let text = refusal("authorize", &without);

    // Assert
    assert!(text.contains("Pool.Audit on /pool/prod"), "{text}");
    assert_eq!(ok("authorize", &with)["authorized"], 1);
}
