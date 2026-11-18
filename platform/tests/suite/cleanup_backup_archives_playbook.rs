//! The playbook tier of `platform/playbooks/cleanup-proxmox-backup-archives.yml`: the guards that live
//! in the play, not in its helper.
//!
//! - The declared pairs are read from inventory and refused as an extra var (the play's first task).
//! - The run happens only at the exact reviewed commit: the play imports the extra-var guard first,
//!   then `require-reviewed-checkout.yml` with the files the run executes.
//! - The delete block runs only for a confirmed real run: the helper's `delete` flag is true and
//!   the run is not in check mode.
//!
//! Each behavioural test lifts the play's own task, with its `when` and assertion untouched, and
//! runs it through ansible-playbook on localhost. The helper's logic is covered by the cli tier
//! (`suite/cleanup_backup_archives.rs`).

use agent_cloud_tests::{playbook, process, text};
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
        .expect("the delete task")
        .clone();
    let uri = &delete["ansible.builtin.uri"];
    let url = uri["url"].as_str().expect("url");
    let delay: u64 = url
        .split("delay=")
        .nth(1)
        .expect("a delay")
        .chars()
        .take_while(char::is_ascii_digit)
        .collect::<String>()
        .parse()
        .expect("a numeric delay");

    // Assert: Proxmox may hold the request for `delay` seconds, so the socket timeout must be longer.
    let timeout = uri["timeout"].as_u64().expect("an explicit timeout");
    assert!(
        timeout > delay,
        "timeout {timeout} must exceed delay {delay}"
    );
}
