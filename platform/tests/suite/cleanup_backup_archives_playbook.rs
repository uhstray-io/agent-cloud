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
//! - After the first DELETE no task ends the block: a refused DELETE, a delete task not seen stopped or
//!   not ended OK, a failed re-read and a verifier that gives no result all reach the one final check,
//!   which counts them beside the read-back's result.
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

const CLOSED: &str = "http://127.0.0.1:9";
const UPID: &str = "UPID:n1:0:0:0:imgdel:900:t:";

/// The delete block from task `start` on, as one localhost run can take it: the poll cut to two quick
/// retries, and each task named in `stubs` replaced by a `set_fact` of the variable and value given.
fn block_from(play: &Value, start: &str, stubs: &[(&str, &str, Value)]) -> Vec<Value> {
    block_tasks(play)
        .into_iter()
        .skip_while(|t| t["name"] != start)
        .map(|mut t| {
            if let Some((_, var, value)) = stubs.iter().find(|(name, _, _)| t["name"] == *name) {
                return json!({"name": t["name"], "ansible.builtin.set_fact": {*var: value}});
            }
            if t["name"] == "Wait for each running delete task" {
                t["retries"] = json!(2);
                t["delay"] = json!(0);
            }
            t
        })
        .collect()
}

/// The variables the delete block reads, for a confirmed list of one gone archive on `backups`, with
/// every Proxmox request sent to a closed local port; `more` adds or replaces variables.
fn block_vars(more: Value) -> Value {
    let (input, list) = confirmed(vec![backup(900)], json!([]));
    let mut vars = json!({
        "_cleanup": helper(), "_list": list, "_pve_host": CLOSED, "_pve_token_id": "t",
        "_pve_secret": "s", "_plan": input["plan"], "_content_reads": {"results": input["content_reads"]}
    });
    vars.as_object_mut()
        .unwrap()
        .extend(more.as_object().unwrap().clone());
    vars
}

/// The re-read as a stub in which the listed archive is gone, so the verifier passes and each test's own
/// failure alone decides the final check.
fn reread() -> (&'static str, &'static str, Value) {
    (
        "Re-read the backup volumes on each backup storage",
        "_content_rereads",
        json!({"results": [{"item": {"node": "n1", "storage": "backups"}, "status": 200, "json": {"data": []}}]}),
    )
}

/// One accepted DELETE answer for the listed archive.
fn accepted(answer: Value) -> Value {
    let (_, list) = confirmed(vec![backup(900)], json!([]));
    let mut answer = answer;
    answer["status"] = json!(200);
    answer["item"] = list["volumes"][0].clone();
    json!({"results": [answer]})
}

fn red_with(out: &Run, parts: &[&str]) {
    assert!(!out.ok, "{}", out.out);
    for part in parts {
        assert!(out.out.contains(part), "missing {part:?}: {}", out.out);
    }
}

#[test]
fn a_refused_delete_still_reaches_the_read_back_and_the_run_ends_red() {
    // Arrange: the block from its DELETE on, every DELETE sent to a closed port, and a read-back in which the
    // archive is gone, so the refused DELETE alone decides.
    let play = cleanup_play();
    let tasks = block_from(&play, "Delete each listed archive", &[reread()]);

    // A DELETE whose reply was cut off after its headers comes back with no `status` at all.
    let cut_off = block_from(&play, "Keep the accepted DELETE answers", &[reread()]);
    let mut no_status = accepted(json!({"failed": true}));
    no_status["results"][0]
        .as_object_mut()
        .unwrap()
        .remove("status");

    // Act
    let out = run(tasks, block_vars(json!({})), None, false);
    let cut = run(
        cut_off,
        block_vars(json!({"_delete_runs": no_status})),
        None,
        false,
    );

    // Assert: each is red, counting the refused DELETE, with the read-back's result in the same message.
    for out in [&out, &cut] {
        red_with(
            out,
            &[
                "1 DELETE request(s) not accepted",
                "Read-back: every listed archive is gone",
            ],
        );
    }
}

#[test]
fn a_delete_task_not_seen_stopped_when_the_wait_ends_still_reaches_the_read_back() {
    // Arrange: the block from its selection of running deletes on, task status on a closed port. Three
    // accepted answers: a task id, a reply with no body, and a body without `data`.
    let play = cleanup_play();
    let tasks = block_from(&play, "Keep the accepted DELETE answers", &[reread()]);

    // Act + Assert: each run is red naming the task not seen stopped, with the read-back's result.
    for answer in [
        json!({"json": {"data": UPID}}),
        json!({}),
        json!({"json": {}}),
    ] {
        let vars = block_vars(json!({"_delete_runs": accepted(answer.clone())}));
        let out = run(tasks.clone(), vars, None, false);
        red_with(
            &out,
            &[
                "1 delete task(s) not seen stopped",
                "Read-back: every listed archive is gone",
            ],
        );
    }
}

#[test]
fn a_delete_that_finished_within_its_delay_is_not_polled_and_the_run_passes() {
    // Arrange: one accepted DELETE that answered null (finished within `delay`), the archive gone.
    let play = cleanup_play();
    let tasks = block_from(&play, "Keep the accepted DELETE answers", &[reread()]);
    let vars = block_vars(json!({"_delete_runs": accepted(json!({"json": {"data": null}}))}));

    // Act
    let out = run(tasks, vars, None, false);

    // Assert
    assert!(out.ok, "{}", out.out);
}

#[test]
fn a_failed_storage_re_read_still_reaches_the_verifier_and_the_one_check() {
    // Arrange: a finished delete, then the real re-read sent to a closed port.
    let play = cleanup_play();
    let tasks = block_from(&play, "Keep the accepted DELETE answers", &[]);
    let vars = block_vars(json!({"_delete_runs": accepted(json!({"json": {"data": null}}))}));

    // Act
    let out = run(tasks, vars, None, false);

    // Assert: red with the verifier's refusal naming the read that failed.
    red_with(
        &out,
        &[
            "0 DELETE request(s) not accepted",
            "Proxmox answered -1 listing the content of storage backups",
        ],
    );
}

#[test]
fn a_verifier_that_gives_no_result_is_a_refusal_in_the_one_check() {
    // Arrange: a finished delete, the archive gone, and a verifier that exits without printing, or prints a
    // partial document.
    let play = cleanup_play();
    let tasks = block_from(&play, "Keep the accepted DELETE answers", &[reread()]);
    let dir = TempDir::new().unwrap();

    // Act + Assert: each is red with the fallback refusal as the read-back.
    for (name, body) in [
        ("silent.py", "raise SystemExit(3)\n"),
        (
            "partial.py",
            "print('{\"refus', end='')\nraise SystemExit(1)\n",
        ),
    ] {
        let script = dir.path().join(name);
        std::fs::write(&script, body).unwrap();
        let vars = block_vars(json!({
            "_delete_runs": accepted(json!({"json": {"data": null}})),
            "_cleanup": script.display().to_string()
        }));
        let out = run(tasks.clone(), vars, None, false);
        red_with(&out, &["Read-back: the verifier gave no result"]);
    }
}

#[test]
fn a_delete_task_that_ended_not_ok_carries_the_read_back_and_a_poll_body_without_data_counts() {
    // Arrange: the block from the end-state collection on, after one running task whose poll answered
    // `stopped` with an error exit or with no exit status, or answered a body without `data` or with `data`
    // null.
    let play = cleanup_play();
    let tasks = block_from(&play, "Collect the delete tasks' end states", &[reread()]);
    let running = accepted(json!({"json": {"data": UPID}}))["results"].clone();
    let with = |poll: Value| {
        block_vars(
            json!({"_delete_runs": {"results": running}, "_delete_accepted": running, "_delete_running": running,
                   "_task_polls": {"results": [poll]}}),
        )
    };

    // Act
    let not_ok = run(
        tasks.clone(),
        with(json!({"json": {"data": {"status": "stopped", "exitstatus": "unable to delete"}}})),
        None,
        false,
    );
    let no_exit = run(
        tasks.clone(),
        with(json!({"json": {"data": {"status": "stopped"}}})),
        None,
        false,
    );
    let no_data = run(tasks.clone(), with(json!({"json": {}})), None, false);
    let null_data = run(tasks, with(json!({"json": {"data": null}})), None, false);

    // Assert: each is red, counted in its own column, with the read-back's result beside it.
    for out in [&not_ok, &no_exit] {
        red_with(
            out,
            &[
                "0 delete task(s) not seen stopped",
                "1 delete task(s) not ended OK",
                "Read-back: every listed archive is gone",
            ],
        );
    }
    for out in [&no_data, &null_data] {
        red_with(
            out,
            &[
                "1 delete task(s) not seen stopped",
                "Read-back: every listed archive is gone",
            ],
        );
    }
}

#[test]
fn a_second_job_or_guest_read_that_fails_is_refused_naming_its_status() {
    // Arrange: the block's second reads sent to a closed port, then their check.
    let play = cleanup_play();
    let tasks = vec![
        block_task(
            &play,
            "Read the backup jobs and guests again before deleting",
        ),
        block_task(&play, "Require the second job and guest reads"),
    ];

    // Act
    let out = run(tasks, block_vars(json!({})), None, false);

    // Assert: refused before any delete, naming the statuses the reads answered.
    red_with(&out, &["Cleanup refused before deleting", "[-1, -1]"]);
}
