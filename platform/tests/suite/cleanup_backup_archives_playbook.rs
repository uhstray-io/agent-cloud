//! The playbook tier of `platform/playbooks/cleanup-proxmox-backup-archives.yml`: the guards that live
//! in the play, not in its helper.
//!
//! - The declared pairs are read from inventory and refused as an extra var (the play's first task).
//! - The run happens only at the exact reviewed commit: the play imports the extra-var guard first,
//!   then `require-reviewed-checkout.yml` with the files the run executes.
//! - The delete block runs only for a confirmed real run: the helper's `delete` flag is true and
//!   the run is not in check mode.
//! - Before the first DELETE the jobs and guests are read again and the list rebuilt; a changed list
//!   (a covering job, a guest back, a new pool job) is refused.
//! - A refused DELETE, or a delete task still running when the wait ends, still reaches the read-back
//!   and verify, and the run ends red.
//!
//! Each behavioural test lifts the play's own task, with its `when` and assertion untouched, and
//! runs it through ansible-playbook on localhost. The helper's logic is covered by the cli tier
//! (`suite/cleanup_backup_archives.rs`).

use agent_cloud_tests::{playbook, process, repo, text};
use serde_json::{Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/cleanup-proxmox-backup-archives.yml";

/// The cleanup play (the one that runs on localhost with tasks), freshly parsed.
fn cleanup_play() -> Value {
    playbook::plays(PLAYBOOK)
        .into_iter()
        .find(|p| p.get("tasks").is_some())
        .expect("the cleanup play")
}

fn task(play: &Value, name: &str) -> Value {
    play["tasks"]
        .as_array()
        .expect("tasks")
        .iter()
        .find(|t| t["name"] == name)
        .unwrap_or_else(|| panic!("no task named {name:?}"))
        .clone()
}

struct Run {
    ok: bool,
    out: String,
}

/// Runs `tasks` as one localhost play. `host_vars` are inventory vars of localhost; `extra` is
/// passed with `-e @file`; `check` adds `--check`.
fn run(tasks: Vec<Value>, host_vars: Value, extra: Option<Value>, check: bool) -> Run {
    let tmp = TempDir::new().unwrap();
    let mut vars = json!({"ansible_connection": "local"});
    vars.as_object_mut()
        .unwrap()
        .extend(host_vars.as_object().unwrap().clone());
    let inv = playbook::write(
        tmp.path(),
        "inv.json",
        &json!({"all": {"hosts": {"localhost": vars}}}),
    );
    let play = json!([{"hosts": "localhost", "gather_facts": false, "tasks": tasks}]);
    let path = playbook::write(tmp.path(), "play.json", &play);
    let mut cmd = process::ansible_playbook(
        tmp.path(),
        &inv.display().to_string(),
        &path.display().to_string(),
    );
    if let Some(extra) = extra {
        let file = playbook::write(tmp.path(), "extra.json", &extra);
        cmd.arg("-e").arg(format!("@{}", file.display()));
    }
    if check {
        cmd.arg("--check");
    }
    let output = process::run(&mut cmd);
    Run {
        ok: output.status.success(),
        out: format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ),
    }
}

#[test]
fn the_cleanup_pairs_are_read_from_inventory_and_refused_as_an_extra_var() {
    // Arrange: the play's own refusal task, and one declared pair.
    let play = cleanup_play();
    let refusal = task(&play, "Refuse the cleanup pairs as an extra var");
    let pairs = json!({"proxmox_backup_cleanup_pairs": [{"vmid": 900, "storage": "backups"}]});

    // Act
    let from_inventory = run(vec![refusal.clone()], pairs.clone(), None, false);
    let as_extra_var = run(vec![refusal], json!({}), Some(pairs), false);

    // Assert: inventory passes; the same value as an extra var is refused with the task's message.
    assert!(from_inventory.ok, "{}", from_inventory.out);
    assert!(!as_extra_var.ok, "{}", as_extra_var.out);
    assert!(
        as_extra_var.out.contains("declared in inventory only"),
        "{}",
        as_extra_var.out
    );
}

#[test]
fn the_cleanup_runs_only_at_the_reviewed_commit() {
    // Arrange: the playbook's top-level entries, guard included.
    let items = playbook::loads(&text::read(PLAYBOOK));
    let items = items.as_array().expect("a playbook is a list");

    // Assert: the extra-var guard comes first, then the reviewed-checkout gate, which pins every
    // file the run executes.
    assert_eq!(
        items[0]["ansible.builtin.import_playbook"],
        "refuse-internal-extra-vars.yml"
    );
    assert_eq!(
        items[1]["ansible.builtin.import_playbook"],
        "require-reviewed-checkout.yml"
    );
    let pinned: Vec<&str> = items[1]["vars"]["_reviewed_checkout_payload_paths"]
        .as_array()
        .expect("payload paths")
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    for file in [
        "platform/playbooks/cleanup-proxmox-backup-archives.yml",
        "platform/playbooks/files/cleanup_backup_archives.py",
        "platform/playbooks/files/survey_backup_storage.py",
    ] {
        assert!(pinned.contains(&file), "{file} is not pinned: {pinned:?}");
    }
}

#[test]
fn the_delete_block_runs_only_for_a_confirmed_real_run() {
    // Arrange: the play's delete block with its own `when`, its body replaced by a marker.
    let play = cleanup_play();
    let mut block = task(&play, "Delete the listed archives");
    block["block"] =
        json!([{"name": "Marker", "ansible.builtin.debug": {"msg": "DELETE-BLOCK-RAN"}}]);
    let with = |delete: bool| {
        vec![
            json!({"name": "List", "ansible.builtin.set_fact": {"_list": {"delete": delete, "volumes": []}}}),
            block.clone(),
        ]
    };

    // Act + Assert, one row per case: (delete flag, check mode, block runs).
    for (delete, check, runs) in [
        (true, false, true),
        (true, true, false),
        (false, false, false),
    ] {
        let out = run(with(delete), json!({}), None, check);
        assert!(out.ok, "delete={delete} check={check}: {}", out.out);
        assert_eq!(
            out.out.contains("DELETE-BLOCK-RAN"),
            runs,
            "delete={delete} check={check}: {}",
            out.out
        );
    }
}

#[test]
fn a_backup_job_list_answered_403_refuses_the_run_before_any_storage_is_read() {
    // Arrange: the play's own refusal, after a job read that answered 403 and one that answered 200.
    let play = cleanup_play();
    let refusal = task(&play, "Require the backup job list");
    let with = |status: u16| {
        vec![
            json!({"name": "Read", "ansible.builtin.set_fact": {"_jobs_read": {"status": status}}}),
            refusal.clone(),
        ]
    };
    let names: Vec<&str> = play["tasks"]
        .as_array()
        .expect("tasks")
        .iter()
        .map(|t| t["name"].as_str().unwrap_or(""))
        .collect();
    let at = |name: &str| {
        names
            .iter()
            .position(|n| *n == name)
            .unwrap_or_else(|| panic!("no task {name:?}"))
    };

    // Act
    let refused = run(with(403), json!({}), None, false);
    let allowed = run(with(200), json!({}), None, false);

    // Assert: a 403 is refused naming the privilege; a 200 passes; the refusal comes before the
    // storage reads, so a token without the privilege reads nothing more.
    assert!(!refused.ok, "{}", refused.out);
    assert!(
        refused.out.contains("needs Sys.Audit on /"),
        "{}",
        refused.out
    );
    assert!(allowed.ok, "{}", allowed.out);
    assert!(at("Require the backup job list") < at("Read each online node's storage status"));
}

#[test]
fn the_delete_request_outlasts_the_wait_it_asks_proxmox_for() {
    // Arrange: the delete task inside the play's delete block.
    let play = cleanup_play();
    let block = task(&play, "Delete the listed archives");
    let delete = block["block"]
        .as_array()
        .expect("block")
        .iter()
        .find(|t| t["name"] == "Delete each listed archive")
        .expect("the delete task");
    let uri = &delete["ansible.builtin.uri"];
    let url = uri["url"].as_str().expect("url");
    let delay: u64 = url
        .rsplit_once("delay=")
        .expect("a delay")
        .1
        .trim()
        .parse()
        .expect("a numeric delay");

    // Assert: Proxmox may hold the request for `delay` seconds, so the socket timeout must be longer.
    let timeout = uri["timeout"].as_u64().expect("an explicit timeout");
    assert!(
        timeout > delay,
        "timeout {timeout} must exceed delay {delay}"
    );
}

const HELPER: &str = "platform/playbooks/files/cleanup_backup_archives.py";

/// The delete block's tasks, by name, from the play.
fn block_tasks(play: &Value) -> Vec<Value> {
    task(play, "Delete the listed archives")["block"]
        .as_array()
        .expect("block")
        .clone()
}

fn block_task(play: &Value, name: &str) -> Value {
    block_tasks(play)
        .into_iter()
        .find(|t| t["name"] == name)
        .unwrap_or_else(|| panic!("no block task named {name:?}"))
}

/// Runs the helper's `list` on `input`, as the play's first list does.
fn first_list(input: &Value) -> Value {
    let home = TempDir::new().unwrap();
    let out = process::scrubbed("python3", home.path())
        .args(["-I", helper().as_str(), "list"])
        .write_stdin(input.to_string())
        .assert()
        .success()
        .get_output()
        .clone();
    serde_json::from_slice(&out.stdout).expect("JSON list")
}

/// The helper's path, as the play's `_cleanup` names it.
fn helper() -> String {
    repo::path(HELPER).display().to_string()
}

fn backup(vmid: i64) -> Value {
    json!({"volid": format!("backups:backup/vzdump-qemu-{vmid}-1.vma.zst"), "content": "backup",
           "vmid": vmid, "size": 1_073_741_824_i64, "ctime": 1_790_000_001})
}

/// The list input of a confirmed real run: one online node, live guest 100, own CephFS `backups`
/// holding `content`, the declared `pairs`. Returns the input with confirm_delete set to its sha256,
/// and the first list.
fn confirmed(content: Vec<Value>, pairs: Value) -> (Value, Value) {
    let mut input = json!({
        "nodes": [{"node": "n1", "status": "online"}],
        "vms": [{"vmid": 100, "name": "web", "node": "n1"}],
        "jobs": [],
        "storage_config": [{"storage": "backups", "type": "cephfs"}],
        "plan": {"reads": [{"node": "n1", "storage": "backups", "status": {"type": "cephfs", "shared": 1}}]},
        "content_reads": [{"item": {"node": "n1", "storage": "backups"}, "status": 200,
                           "json": {"data": content}}],
        "pairs": pairs,
        "confirm_delete": "",
        "check_mode": true
    });
    let sha = first_list(&input)["sha256"].clone();
    input["confirm_delete"] = sha;
    input["check_mode"] = json!(false);
    let list = first_list(&input);
    assert_eq!(list["delete"], true, "{list}");
    (input, list)
}

fn rereads(jobs: Value, vms: Value) -> Value {
    json!({"results": [{"status": 200, "json": {"data": jobs}}, {"status": 200, "json": {"data": vms}}]})
}

#[test]
fn the_list_is_rebuilt_from_fresh_job_and_guest_reads_before_any_delete() {
    // Arrange: the block's rebuild and its check, after two second reads, for a confirmed list.
    let play = cleanup_play();
    let tasks = |reread: Value| {
        vec![
            json!({"name": "Reads", "ansible.builtin.set_fact": {"_rereads": reread}}),
            block_task(&play, "Rebuild the list from the second reads"),
            block_task(&play, "Require the same list before deleting"),
        ]
    };
    let vars = |input: &Value, list: &Value| json!({"_cleanup": helper(), "_list_input": input, "_list": list});
    let web = json!([{"vmid": 100, "name": "web", "node": "n1"}]);
    let (declared, declared_list) = confirmed(
        vec![backup(100)],
        json!([{"vmid": 100, "storage": "backups"}]),
    );
    let (gone, gone_list) = confirmed(vec![backup(900)], json!([]));
    let job = json!([{"id": "backup-7", "type": "vzdump", "storage": "backups", "vmid": "100"}]);
    let back = json!([{"vmid": 100, "name": "web", "node": "n1"}, {"vmid": 900, "name": "db", "node": "n1"}]);

    // Act
    let unchanged = run(
        tasks(rereads(json!([]), web.clone())),
        vars(&declared, &declared_list),
        None,
        false,
    );
    let job_enabled = run(
        tasks(rereads(job, web.clone())),
        vars(&declared, &declared_list),
        None,
        false,
    );
    let guest_back = run(
        tasks(rereads(json!([]), back)),
        vars(&gone, &gone_list),
        None,
        false,
    );
    let pool_job =
        json!([{"id": "backup-9", "type": "vzdump", "storage": "backups", "pool": "p1"}]);
    let pool_added = run(
        tasks(rereads(pool_job, web)),
        vars(&declared, &declared_list),
        None,
        false,
    );

    // Assert: the same reads pass; a job enabled since is refused naming it; a gone guest that came
    // back empties the list, which the helper accepts, and the sha256 comparison refuses.
    assert!(unchanged.ok, "{}", unchanged.out);
    assert!(!job_enabled.ok, "{}", job_enabled.out);
    assert!(job_enabled.out.contains("backup-7"), "{}", job_enabled.out);
    assert!(!guest_back.ok, "{}", guest_back.out);
    assert!(
        guest_back
            .out
            .contains("changed since the list was confirmed"),
        "{}",
        guest_back.out
    );
    // A pool job enabled since could write the declared storage, and its Pool.Audit was never checked.
    assert!(!pool_added.ok, "{}", pool_added.out);
    assert!(
        pool_added
            .out
            .contains("changed since the list was confirmed"),
        "{}",
        pool_added.out
    );
}

#[test]
fn a_refused_delete_still_reaches_the_read_back_and_the_run_ends_red() {
    // Arrange: the block from its DELETE on, sent to a closed local port (every DELETE fails), with the
    // re-read replaced by a read that still holds the archive.
    let play = cleanup_play();
    let tasks: Vec<Value> = block_tasks(&play)
        .into_iter()
        .skip_while(|t| t["name"] != "Delete each listed archive")
        .map(|t| {
            if t["name"] == "Re-read the backup volumes on each backup storage" {
                json!({"name": t["name"], "ansible.builtin.set_fact": {"_content_rereads": {"results": "{{ _content_reads.results }}"}}})
            } else {
                t
            }
        })
        .collect();
    let (input, list) = confirmed(vec![backup(900)], json!([]));
    let vars = json!({
        "_cleanup": helper(), "_list": list, "_pve_host": "http://127.0.0.1:9",
        "_pve_token_id": "t", "_pve_secret": "s",
        "_content_reads": {"results": input["content_reads"]}
    });

    // Act
    let out = run(tasks, vars, None, false);

    // Assert: the run is red on the refused DELETE, and the read-back and verify ran before it.
    assert!(!out.ok, "{}", out.out);
    assert!(
        out.out.contains("request(s) were not accepted"),
        "{}",
        out.out
    );
    assert!(
        out.out.contains("Listed archives are still present"),
        "{}",
        out.out
    );
}

#[test]
fn a_delete_task_still_running_when_the_wait_ends_still_reaches_the_read_back() {
    // Arrange: the block from its wait on, after one accepted DELETE whose task never reports stopped (its
    // status is read from a closed local port), the retries cut to two, and the re-read replaced by a read
    // that still holds the archive.
    let play = cleanup_play();
    let tasks: Vec<Value> = block_tasks(&play)
        .into_iter()
        .skip_while(|t| t["name"] != "Wait for each running delete task")
        .map(|mut t| {
            if t["name"] == "Wait for each running delete task" {
                t["retries"] = json!(2);
                t["delay"] = json!(0);
                t
            } else if t["name"] == "Re-read the backup volumes on each backup storage" {
                json!({"name": t["name"], "ansible.builtin.set_fact": {"_content_rereads": {"results": "{{ _content_reads.results }}"}}})
            } else {
                t
            }
        })
        .collect();
    let (input, list) = confirmed(vec![backup(900)], json!([]));
    let accepted = json!({"results": [{"status": 200, "json": {"data": "UPID:n1:0:0:0:imgdel:900:t:"},
                                       "item": list["volumes"][0]}]});
    let vars = json!({
        "_cleanup": helper(), "_list": list, "_delete_runs": accepted, "_pve_host": "http://127.0.0.1:9",
        "_pve_token_id": "t", "_pve_secret": "s", "_content_reads": {"results": input["content_reads"]}
    });

    // Act
    let out = run(tasks, vars, None, false);

    // Assert: the run is red naming the task that had not stopped, and the read-back and verify ran before.
    assert!(!out.ok, "{}", out.out);
    assert!(
        out.out
            .contains("1 delete task(s) had not stopped when the wait ended"),
        "{}",
        out.out
    );
    assert!(
        out.out.contains("Listed archives are still present"),
        "{}",
        out.out
    );
}
