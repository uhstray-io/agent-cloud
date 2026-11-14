//! Credential backups move live secrets from OpenBao into the private site-config repo without any
//! of them reaching a log (static tier).
//!
//! Two bugs were found by running backup-credentials-to-site-config.yml end-to-end against a mock
//! store and a local bare remote. Neither was visible to a syntax check, a linter or reading:
//!
//! 1. `command: cmd:` word-splits, so `git config user.name Joseph A. Wisneski IV` arrived as four
//!    arguments and git answered "no action specified".
//! 2. A `vars:` entry holding `lookup('pipe', 'date ...')` is re-evaluated at EVERY reference, so the
//!    branch created and the branch pushed were named one second apart. The push failed with "src
//!    refspec does not match any", AFTER the credentials had been written and committed into the
//!    clone.
//!
//! The second is the dangerous shape: a half-finished run that leaves material in a clone and
//! reports failure. The scratch directory holding the deploy key and the fetched credentials is
//! wiped in `always:`, the key is never a task parameter, and nothing the run prints carries a value.
//!
//! Replaces platform/tests/test_backup_credentials.bats (plan 24, Phase 4).
//!
//! ## Port map
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | cred backup: the playbook exists and guards its transport | `the_playbook_guards_every_openbao_endpoint_it_builds` |
//! | cred backup: no non-deterministic lookup sits in a play's vars block | `no_playbook_builds_a_vars_entry_from_a_nondeterministic_lookup`, with `the_branch_name_is_frozen_by_a_set_fact_task` and, candidate: pins text, `the_frozen_branch_is_written_as_a_folded_scalar` |
//! | cred backup: every git call carrying a space uses argv, not cmd | `every_git_call_carrying_a_space_uses_argv_not_cmd` |
//! | cred backup: the scratch dir is wiped even when the run fails | `the_scratch_dir_is_wiped_even_when_the_run_fails` |
//! | cred backup: nothing it prints can carry a credential value | `no_debug_task_prints_a_secret_field_value`, with `the_secret_value_write_is_no_log` |
//! | cred backup: the store path is built from a validated name, not taken whole | `the_store_path_is_built_from_a_validated_name_not_taken_whole` |
//! | cred backup: the deploy key exists on the runner only inside the dir that is always wiped | `the_deploy_key_is_read_from_one_fixed_store_path`, `the_materialised_key_is_0600_no_log_under_the_scratch_dir`, `git_ssh_names_only_the_deploy_key_with_strict_host_keys`, `the_key_is_written_only_after_the_scratch_dir_exists` |
//! | cred backup: the key is never taken from a task parameter, and the seed path says why | `the_deploy_key_is_never_a_task_parameter`, with candidate: pins text `every_key_missing_message_says_how_to_seed_the_key` |

use std::fs;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const PB: &str = "platform/playbooks/backup-credentials-to-site-config.yml";
const SSH_PB: &str = "platform/playbooks/backup-service-ssh-key.yml";
const CLONE: &str = "platform/playbooks/tasks/site-config-clone.yml";
const PUSH: &str = "platform/playbooks/tasks/site-config-push.yml";
const GUARD_INCLUDE: &str = "tasks/assert-bao-transport.yml";
const CLONE_INCLUDE: &str = "tasks/site-config-clone.yml";
const KEY_PATH: &str = "secret/data/services/ssh/site-config";
/// A task's module under any of the three spellings Ansible accepts for a module key: the short
/// name, `ansible.builtin.<short>`, or `ansible.legacy.<short>` (ansible/utils/fqcn.py). A check
/// that reads one spelling alone is blind to a task written under another, so every module read
/// goes through here.
fn module<'a>(task: &'a Node, short: &str) -> Option<&'a Node> {
    [
        short.to_owned(),
        format!("ansible.builtin.{short}"),
        format!("ansible.legacy.{short}"),
    ]
    .iter()
    .find_map(|key| task.get(key))
}

fn load(rel: &str) -> Node {
    yaml::load_one(&repo::path(rel)).unwrap_or_else(|| panic!("{rel}: empty document"))
}

/// Every mapping in a document, at any depth.
fn maps(node: &Node) -> Vec<&Node> {
    let mut out = Vec::new();
    collect_maps(node, &mut out);
    out
}

fn collect_maps<'a>(node: &'a Node, out: &mut Vec<&'a Node>) {
    match node {
        Node::Map(entries) => {
            out.push(node);
            for (k, v) in entries {
                collect_maps(k, out);
                collect_maps(v, out);
            }
        }
        Node::Seq(items) => {
            for item in items {
                collect_maps(item, out);
            }
        }
        Node::Str(_) | Node::Bool(_) | Node::Null => {}
    }
}

/// The value of every mapping entry with this key, at any depth.
fn values_of<'a>(node: &'a Node, key: &str) -> Vec<&'a Node> {
    maps(node).into_iter().filter_map(|m| m.get(key)).collect()
}

/// A string at any depth, in a key or a value.
fn any_string(node: &Node, pred: &dyn Fn(&str) -> bool) -> bool {
    match node {
        Node::Str(s) => pred(s),
        Node::Seq(items) => items.iter().any(|item| any_string(item, pred)),
        Node::Map(entries) => entries
            .iter()
            .any(|(k, v)| any_string(k, pred) || any_string(v, pred)),
        Node::Null | Node::Bool(_) => false,
    }
}

fn mentions(node: &Node, needle: &str) -> bool {
    any_string(node, &|s: &str| s.contains(needle))
}

/// Whether a mapping has the key directly or at any depth.
fn has_key(node: &Node, key: &str) -> bool {
    !values_of(node, key).is_empty()
}

fn task_name(task: &Node) -> &str {
    task.get("name").and_then(Node::as_str).unwrap_or("")
}

fn named<'a>(tasks: &'a [Node], name: &str) -> &'a Node {
    tasks
        .iter()
        .find(|t| task_name(t) == name)
        .unwrap_or_else(|| panic!("no task named {name:?}"))
}

fn starting<'a>(tasks: &'a [Node], prefix: &str) -> &'a Node {
    tasks
        .iter()
        .find(|t| task_name(t).starts_with(prefix))
        .unwrap_or_else(|| panic!("no task whose name starts with {prefix:?}"))
}

/// The file an include task names, under either spelling of the keyword.
fn include_target(node: &Node) -> Option<&str> {
    module(node, "include_tasks").and_then(Node::as_str)
}

/// The arguments of the module a task calls, under any spelling of its key.
fn args<'a>(task: &'a Node, short: &str) -> Option<&'a Node> {
    module(task, short)
}

/// The ansible `that:` expressions of an assert task, whether written as one scalar or a list.
fn that_items(task: &Node) -> Vec<&str> {
    match args(task, "assert").and_then(|a| a.get("that")) {
        Some(Node::Str(s)) => vec![s.as_str()],
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        _ => vec![],
    }
}

fn fail_msg(task: &Node) -> Option<&str> {
    args(task, "assert")?.get("fail_msg")?.as_str()
}

/// A block that includes the shared clone task and whose `always:` wipes the scratch directory.
fn wipes_scratch_after_clone(block_map: &Node) -> bool {
    let (Some(block), Some(always)) = (block_map.get("block"), block_map.get("always")) else {
        return false;
    };
    mentions(block, CLONE_INCLUDE) && always_wipes_scratch(always)
}

fn always_wipes_scratch(always: &Node) -> bool {
    always.as_seq().is_some_and(|items| {
        items.iter().any(|task| {
            args(task, "file").is_some_and(|file| {
                file.get("state").and_then(Node::as_str) == Some("absent")
                    && file
                        .get("path")
                        .and_then(Node::as_str)
                        .is_some_and(|p| p.contains("_sc_wd"))
            })
        })
    })
}

#[test]
fn the_playbook_guards_every_openbao_endpoint_it_builds() {
    // Arrange
    let doc = load(PB);

    // Act
    let endpoints = maps(&doc)
        .iter()
        .filter(|m| m.get("_bao_url").is_some())
        .count();
    let guards = maps(&doc)
        .iter()
        .filter(|m| include_target(m) == Some(GUARD_INCLUDE))
        .count();

    // Assert: every endpoint is checked by the transport guard before use.
    assert!(endpoints > 0, "{PB} builds no _bao_url");
    assert_eq!(
        guards, endpoints,
        "every _bao_url must be followed by one transport guard"
    );
}

#[test]
fn no_playbook_builds_a_vars_entry_from_a_nondeterministic_lookup() {
    // THE LAZY-EVALUATION TRAP, as a closed rule across every playbook. A `vars:` entry is
    // re-evaluated at each reference, so a lookup whose result can change between two references
    // silently yields two different values. A value that must be stable belongs in set_fact.
    // pipe, url and random_choice lookups, now(), and `| random` are the non-deterministic ones.
    let bad = Regex::new(r#"lookup\(\s*['"](pipe|url|random_choice)['"]|\bnow\(\)|\|\s*random\b"#)
        .expect("a valid regex");
    let files = repo::ls_files(&[":(glob)platform/playbooks/**/*.yml"]);
    assert!(!files.is_empty(), "no playbook YAML files found");

    let mut offenders = Vec::new();
    for file in &files {
        let text = fs::read_to_string(repo::path(file)).unwrap_or_else(|e| panic!("{file}: {e}"));
        for doc in yaml::parse(&text, file) {
            for map in maps(&doc) {
                if let Some(vars) = map.get("vars")
                    && any_string(vars, &|s: &str| bad.is_match(s))
                {
                    offenders.push(format!(
                        "{file}: a vars block holds a non-deterministic lookup"
                    ));
                }
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "non-deterministic lookup inside a play vars block; move it to set_fact, because a vars \
         entry is re-evaluated at every reference:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn the_branch_name_is_frozen_by_a_set_fact_task() {
    // Arrange
    let tasks = repo::tasks(CLONE);

    // Act
    let freeze = starting(&tasks, "Freeze the branch name");
    let facts = args(freeze, "set_fact").expect("the freeze task is a set_fact");

    // Assert: set_fact evaluates the pipe lookup once and stores it.
    let branch = facts
        .get("_sc_branch")
        .and_then(Node::as_str)
        .expect("the freeze task sets _sc_branch");
    assert!(
        branch.contains("lookup('pipe'"),
        "the frozen branch must carry the date lookup: {branch}"
    );
}

#[test]
fn the_frozen_branch_is_written_as_a_folded_scalar() {
    // Candidate: pins source text. A parsed value cannot show its folded style, so this reads the
    // file. It holds the value's form, not its behaviour.
    let text = fs::read_to_string(repo::path(CLONE)).unwrap_or_else(|e| panic!("{CLONE}: {e}"));

    assert!(
        text.contains("_sc_branch: >-"),
        "{CLONE} must write _sc_branch as a folded scalar"
    );
}

#[test]
fn every_git_call_carrying_a_space_uses_argv_not_cmd() {
    // `command` never invokes a shell, so quotes that look like they protect a multi-word
    // argument are consumed by YAML instead. A cmd string is safe only when no argument has a
    // space; the two that do here are a person's name and a commit message.
    let clone = repo::tasks(CLONE);
    let push = repo::tasks(PUSH);

    let identify = named(&clone, "Identify the commit");
    let commit = named(&push, "Commit");
    let stage = named(&push, "Stage only the path this run wrote");

    assert!(!has_key(identify, "cmd"), "the identity task uses cmd:");
    assert!(!has_key(commit, "cmd"), "the commit task uses cmd:");
    assert!(!has_key(stage, "cmd"), "the stage task uses cmd:");

    let argv: Option<Vec<&str>> = args(identify, "command")
        .and_then(|c| c.get("argv"))
        .and_then(Node::as_seq)
        .and_then(|items| items.iter().map(Node::as_str).collect());
    assert_eq!(
        argv,
        Some(vec!["git", "config", "user.{{ item.k }}", "{{ item.v }}"]),
        "the identity task must pass its name and email as argv"
    );
}

#[test]
fn the_scratch_dir_is_wiped_even_when_the_run_fails() {
    // It holds every credential fetched. Leaving it on the orchestrator after a failure is worse
    // than the failure. `always`, not a final task: a final task does not run when an earlier one
    // aborts the block.
    for file in [PB, SSH_PB] {
        let doc = load(file);

        assert!(
            maps(&doc).into_iter().any(wipes_scratch_after_clone),
            "{file}: no block that includes the clone task wipes the scratch dir in its always:"
        );
    }
}

#[test]
fn no_debug_task_prints_a_secret_field_value() {
    // CLOSED: every debug message in the playbook is checked. A message may reference field names,
    // counts and paths, never an indexed read of the secret payload, which is what a value is.
    let tasks = repo::tasks(PB);

    let debug_tasks = tasks.iter().filter(|t| args(t, "debug").is_some()).count();
    assert!(
        debug_tasks > 0,
        "{PB} has no debug task, so the check would pass for nothing"
    );

    let leaks: Vec<&str> = tasks
        .iter()
        .filter(|t| args(t, "debug").is_some())
        .filter(|t| mentions(t, "_secret.json.data.data["))
        .map(task_name)
        .collect();

    assert!(
        leaks.is_empty(),
        "a debug task indexes the secret payload, which prints a value: {leaks:?}"
    );
}

#[test]
fn the_secret_value_write_is_no_log() {
    // The tasks that touch values are no_log'd.
    let tasks = repo::tasks(PB);

    let write = named(&tasks, "Write each field to its own file");

    assert_eq!(
        write.get("no_log"),
        Some(&Node::Bool(true)),
        "the write of each secret value must be no_log"
    );
}

#[test]
fn the_store_path_is_built_from_a_validated_name_not_taken_whole() {
    // A caller-supplied path would let one invocation walk the entire store into a git repo. The
    // service name is constrained because it is interpolated into both an OpenBao path and a
    // filesystem path.
    let doc = load(PB);
    let paths: Vec<&str> = values_of(&doc, "_bao_path")
        .into_iter()
        .filter_map(|p| p.as_str())
        .collect();
    assert!(!paths.is_empty(), "{PB} sets no _bao_path");
    assert!(
        paths
            .iter()
            .all(|p| p.starts_with("secret/data/services/{{ credential_service")),
        "_bao_path must be built from credential_service: {paths:?}"
    );
    assert!(
        paths
            .iter()
            .all(|p| !p.starts_with("{{ bao_path") && !p.starts_with("{{ credential_path")),
        "_bao_path must not be taken whole from the caller: {paths:?}"
    );

    let tasks = repo::tasks(PB);
    let validate = named(&tasks, "Validate inputs");
    assert!(
        that_items(validate).contains(&"credential_service is match('^[a-z0-9][a-z0-9_-]*$')"),
        "Validate inputs must constrain credential_service to a safe name"
    );
}

#[test]
fn the_deploy_key_is_read_from_one_fixed_store_path() {
    // The key is read from OpenBao with the token the play already holds. The path is fixed, not
    // caller-supplied, and the key is handed in as a value from the read.
    for file in [PB, SSH_PB] {
        let doc = load(file);

        let key_paths: Vec<&str> = values_of(&doc, "_key_path")
            .into_iter()
            .filter_map(|p| p.as_str())
            .collect();
        assert_eq!(key_paths, vec![KEY_PATH], "{file}: _key_path");

        let handed_in: Vec<&str> = values_of(&doc, "_sc_deploy_key")
            .into_iter()
            .filter_map(|p| p.as_str())
            .collect();
        assert_eq!(
            handed_in,
            vec!["{{ _key.json.data.data.private_key }}"],
            "{file}: _sc_deploy_key"
        );
    }
}

#[test]
fn the_materialised_key_is_0600_no_log_under_the_scratch_dir() {
    // The key file is 0600, written under no_log, and lives inside the scratch directory that the
    // always: block wipes. Anywhere else the wipe would miss it.
    let tasks = repo::tasks(CLONE);

    let freeze = starting(&tasks, "Freeze the branch name");
    let key_path = args(freeze, "set_fact")
        .and_then(|f| f.get("_sc_key"))
        .and_then(Node::as_str);
    assert_eq!(
        key_path,
        Some("{{ _sc_wd_result.path }}/deploy_key"),
        "the key path must be under the scratch dir"
    );

    let write = starting(&tasks, "Materialise the deploy key");
    assert_eq!(
        write.get("no_log"),
        Some(&Node::Bool(true)),
        "the key write must be no_log"
    );
    let copy = args(write, "copy").expect("the key is written by copy");
    assert_eq!(copy.get("mode").and_then(Node::as_str), Some("0600"));
    assert_eq!(
        copy.get("dest").and_then(Node::as_str),
        Some("{{ _sc_key }}")
    );
}

#[test]
fn git_ssh_names_only_the_deploy_key_with_strict_host_keys() {
    // Exactly one GIT_SSH_COMMAND definition. It names that file, IdentitiesOnly so ssh offers
    // nothing from the runner's agent or ~/.ssh, and strict pinned host keys. Comments may name
    // accept-new; code may not, so the check reads parsed strings.
    let doc = load(CLONE);

    let defs = values_of(&doc, "GIT_SSH_COMMAND");
    assert_eq!(defs.len(), 1, "exactly one GIT_SSH_COMMAND definition");
    let ssh = defs[0].as_str().expect("GIT_SSH_COMMAND is a string");
    for needle in [
        "ssh -i {{ _sc_wd_result.path }}/deploy_key -o IdentitiesOnly=yes",
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=",
    ] {
        assert!(
            ssh.contains(needle),
            "GIT_SSH_COMMAND lacks {needle:?}: {ssh}"
        );
    }

    assert!(
        !any_string(&doc, &|s: &str| s.contains("accept-new")),
        "accept-new is trust on first use and must not appear in code"
    );
}

#[test]
fn the_key_is_written_only_after_the_scratch_dir_exists() {
    let tasks = repo::tasks(CLONE);
    let make = tasks
        .iter()
        .position(|t| task_name(t).starts_with("Create a scratch dir"))
        .expect("the scratch dir task");
    let write = tasks
        .iter()
        .position(|t| task_name(t).starts_with("Materialise the deploy key"))
        .expect("the key write task");
    assert!(
        make < write,
        "the scratch dir (task {make}) must exist before the key (task {write})"
    );
}

#[test]
fn the_deploy_key_is_never_a_task_parameter() {
    // Semaphore persists a task's extra vars and serves them back over its API, so a deploy key
    // passed as -e would live in the orchestrator's database in plaintext. The only way in is the
    // store. Raw-text deny-list, so a stray mention in either playbook fails.
    let forbidden = Regex::new(
        r"deploy_key \| default|site_config_deploy_key|lookup\(.env., .(SITE_CONFIG|DEPLOY)",
    )
    .expect("a valid regex");
    for file in [PB, SSH_PB] {
        let text = fs::read_to_string(repo::path(file)).unwrap_or_else(|e| panic!("{file}: {e}"));
        assert!(
            !forbidden.is_match(&text),
            "{file} takes the deploy key from a parameter or the environment"
        );
    }
}

#[test]
fn every_key_missing_message_says_how_to_seed_the_key() {
    // Candidate: pins message text. Every "key missing" failure must point at the seed template and
    // the BAO_VALUE environment secret, which is the only way the key gets into the store.
    for file in [PB, SSH_PB, CLONE] {
        let tasks = repo::tasks(file);
        assert!(
            tasks
                .iter()
                .filter_map(fail_msg)
                .any(|msg| msg.contains("BAO_VALUE")),
            "{file}: no assert's fail_msg names BAO_VALUE"
        );
    }
}
