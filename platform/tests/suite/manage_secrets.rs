//! The shared secret lifecycle writes the service's secrets only through the merge task (static
//! tier).
//!
//! Every composable deploy includes tasks/manage-secrets.yml. A KV-v2 POST of `_resolved` writes
//! the FULL document and silently deletes every key at the path that `_secret_definitions` does not
//! declare. Measured: a deploy re-run erased a service's post-deploy API key from the store
//! (docs/MISTAKES.md 10.11). Post-deploy keys, such as captured API keys and seeded operator
//! credentials, legitimately live beside the deploy-managed ones. So the store-back goes through the
//! shared merge-patch implementation, and no task writes to the service path directly.
//!
//! Replaces platform/tests/test_manage_secrets.bats (plan 24, Phase 4).
//!
//! ## Port map
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | manage-secrets: the store-back is a sibling-preserving merge, never a whole-document write | `the_store_back_goes_through_the_shared_merge_task`, with `no_task_writes_to_the_service_secret_path_directly`, with `no_task_runs_the_bao_or_vault_cli` and its self-check `a_store_cli_call_is_detected` |

use std::sync::LazyLock;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const MANAGE: &str = "platform/playbooks/tasks/manage-secrets.yml";
const MERGE_TASK: &str = "bao-merge-keys.yml";
const SERVICE_PATH: &str = "secret/data";
/// The one request manage-secrets.yml may POST: its AppRole login. Compared exactly, not by
/// substring, so a POST to `.../auth/approle/login?x=...` or to a KV path cannot pass as the login.
const LOGIN_URL: &str = "{{ _bao_url }}/v1/auth/approle/login";

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

/// A string at any depth, in a key or a value: a task names a path wherever it does so.
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

#[test]
fn the_store_back_goes_through_the_shared_merge_task() {
    // Arrange
    let tasks = repo::tasks(MANAGE);

    // Act
    let includes_merge = tasks
        .iter()
        .any(|task| module(task, "include_tasks").and_then(Node::as_str) == Some(MERGE_TASK));

    // Assert
    assert!(
        includes_merge,
        "{MANAGE} must store secrets back through include_tasks: {MERGE_TASK}"
    );
}

#[test]
fn no_task_writes_to_the_service_secret_path_directly() {
    // Arrange: a task names the KV-v2 data path wherever it does so, in its url, its vars, or any
    // other scalar. Selecting on the url alone let a write through with the path in a task var.
    let tasks = repo::tasks(MANAGE);
    let on_secret_path: Vec<&Node> = tasks
        .iter()
        .filter(|task| any_string(task, &|s: &str| s.contains(SERVICE_PATH)))
        .collect();
    assert!(
        !on_secret_path.is_empty(),
        "{MANAGE} has no task on {SERVICE_PATH}; the check would pass for nothing"
    );

    // Act / Assert: every such task is a uri GET (fetch existing, shared reads), under whichever
    // spelling of the uri module it uses. A method left out is GET, as the module defaults.
    for task in on_secret_path {
        let name = task
            .get("name")
            .and_then(Node::as_str)
            .unwrap_or("(unnamed)");
        let Some(uri) = module(task, "uri") else {
            panic!("{MANAGE}: task {name:?} names {SERVICE_PATH} but is not a uri read");
        };
        let method = uri.get("method").and_then(Node::as_str).unwrap_or("GET");
        assert!(
            method.eq_ignore_ascii_case("GET"),
            "{MANAGE}: task {name:?} reads {SERVICE_PATH} with method {method}, not GET"
        );
    }

    // Part 2: the file writes nothing to OpenBao except its AppRole login, whatever path the write
    // names. A path-based selector above cannot see a write to a URL built from variables, or a
    // write by a module that is not uri. So the rule is closed instead: each uri call that is not a
    // GET must be that one POST, and no task may call a community.hashi_vault module. A method this
    // check cannot read counts as a write.
    let mut writes = Vec::new();
    let mut logins = 0;
    for task in repo::tasks(MANAGE) {
        let name = task
            .get("name")
            .and_then(Node::as_str)
            .unwrap_or("(unnamed)");
        let calls_hashi_vault = task.as_map().into_iter().flatten().any(|(key, _)| {
            key.as_str()
                .is_some_and(|key| key.starts_with("community.hashi_vault."))
        });
        if calls_hashi_vault {
            writes.push(format!("{name:?} calls a community.hashi_vault module"));
        }
        let Some(uri) = module(&task, "uri") else {
            continue;
        };
        let is_read = match uri.get("method") {
            None => true,
            Some(Node::Str(method)) => method.eq_ignore_ascii_case("GET"),
            Some(_) => false,
        };
        if is_read {
            continue;
        }
        let is_login = uri.get("url").and_then(Node::as_str) == Some(LOGIN_URL)
            && uri.get("method").and_then(Node::as_str) == Some("POST");
        if is_login {
            logins += 1;
        } else {
            writes.push(format!(
                "{name:?} writes to OpenBao (method {:?}, url {:?})",
                uri.get("method"),
                uri.get("url")
            ));
        }
    }

    assert!(
        writes.is_empty(),
        "{MANAGE} writes to OpenBao outside its AppRole login; a store-back goes through \
         {MERGE_TASK}:\n{}",
        writes.join("\n")
    );
    assert_eq!(
        logins, 1,
        "{MANAGE} must log in to OpenBao exactly once, at {LOGIN_URL}"
    );
}

/// The modules that run a program, under which a `bao` or `vault` CLI call could write the store.
const COMMAND_MODULES: &[&str] = &["command", "shell", "raw", "script", "expect"];

/// A `bao` or `vault` CLI call: the program name followed by an argument (at the start, or after
/// whitespace, a path separator, a quote, `=`, or a shell separator `; & | ( `` ` ``), so options
/// before the subcommand (`bao -address=x kv put`) and `x;bao kv put` do not hide it, while a path
/// that only ends in `vault` is not a call.
static STORE_CLI: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?:^|[\s/"'=;&|(`\\])(?:bao|vault)["']?\s+\S"#).expect("a valid regex")
});

/// Every string under a node; a list of strings (an argv) also yields its items joined by spaces,
/// so `[bao, kv, put]` reads as the command line it runs.
fn command_lines(node: &Node, out: &mut Vec<String>) {
    match node {
        Node::Str(s) => out.push(s.clone()),
        Node::Seq(items) => {
            let words: Vec<&str> = items.iter().filter_map(Node::as_str).collect();
            if !words.is_empty() {
                out.push(words.join(" "));
            }
            for item in items {
                command_lines(item, out);
            }
        }
        Node::Map(entries) => {
            for (k, v) in entries {
                command_lines(k, out);
                command_lines(v, out);
            }
        }
        Node::Null | Node::Bool(_) => {}
    }
}

/// Whether a task runs the OpenBao (`bao`) or Vault (`vault`) CLI through a command-family module,
/// which the uri and hashi_vault checks above cannot see.
///
/// The module is read under its key (any spelling) and from `action:` / `local_action:` (a string's
/// first word, or a mapping's `module`); for a command-family call, that value and the task-level
/// `args` keys that can carry a program (`cmd`, `argv`, `_raw_params`, `stdin`, `command`) are
/// scanned.
fn runs_store_cli(task: &Node) -> bool {
    let is_command = |name: &str| {
        let short = name
            .strip_prefix("ansible.builtin.")
            .or_else(|| name.strip_prefix("ansible.legacy."))
            .unwrap_or(name);
        COMMAND_MODULES.contains(&short)
    };
    let mut scanned: Vec<&Node> = COMMAND_MODULES
        .iter()
        .filter_map(|m| module(task, m))
        .collect();
    for key in ["action", "local_action"] {
        let Some(action) = task.get(key) else {
            continue;
        };
        let name = match action {
            Node::Str(s) => s.split_whitespace().next(),
            Node::Map(_) => action.get("module").and_then(Node::as_str),
            _ => None,
        };
        if name.is_some_and(is_command) {
            scanned.push(action);
        }
    }
    if scanned.is_empty() {
        return false;
    }
    // Of the task-level args, the keys that can carry a program: the command line itself, and
    // `stdin`, which is the program for `sh`, `bash -s` or `python3 -`. `chdir`, `creates` and the
    // like are paths, not the program run.
    if let Some(args) = task.get("args") {
        scanned.extend(
            ["cmd", "argv", "_raw_params", "stdin", "command"]
                .iter()
                .filter_map(|k| args.get(k)),
        );
    }
    scanned.into_iter().any(|node| {
        let mut lines = Vec::new();
        command_lines(node, &mut lines);
        lines.iter().any(|l| STORE_CLI.is_match(l))
    })
}

#[test]
fn no_task_runs_the_bao_or_vault_cli() {
    // Part 3: a `command: podman exec openbao bao kv put ...` writes the whole document exactly as
    // a KV-v2 POST does, and neither check above reads a command line.
    let tasks = repo::tasks(MANAGE);
    let offenders: Vec<&str> = tasks
        .iter()
        .filter(|t| runs_store_cli(t))
        .map(|t| t.get("name").and_then(Node::as_str).unwrap_or("(unnamed)"))
        .collect();
    assert!(
        offenders.is_empty(),
        "{MANAGE} runs the bao or vault CLI; a store-back goes through {MERGE_TASK}: {offenders:?}"
    );
}

#[test]
fn a_store_cli_call_is_detected() {
    let task = |text: &str| yaml::parse(text, "synthetic").remove(0);
    for flagged in [
        "action: command bao kv put x\n",
        "local_action: {module: ansible.builtin.command, cmd: bao kv put x}\n",
        "ansible.builtin.command: {}\nargs: {cmd: bao kv put x}\n",
        "ansible.builtin.command: bash -s\nargs: {stdin: \"bao kv put x\"}\n",
    ] {
        assert!(runs_store_cli(&task(flagged)), "{flagged}");
    }
    for clean in [
        "action: debug msg=\"bao kv put\"\n",
        "ansible.builtin.command: echo hi\nargs: {chdir: /tmp}\n",
    ] {
        assert!(!runs_store_cli(&task(clean)), "{clean}");
    }
    assert!(runs_store_cli(&task(
        "ansible.builtin.command: podman exec openbao bao kv put secret/x a=b\n"
    )));
    assert!(runs_store_cli(&task(
        "command:\n  argv: [podman, exec, openbao, bao, kv, put, secret/x, a=b]\n"
    )));
    assert!(runs_store_cli(&task(
        "ansible.legacy.shell: vault kv put secret/x a=b\n"
    )));
    assert!(!runs_store_cli(&task(
        "ansible.builtin.command: podman ps\n"
    )));
    assert!(runs_store_cli(&task(
        "ansible.builtin.shell: \\bao kv put secret/x a=b\n"
    )));
    assert!(runs_store_cli(&task(
        "ansible.builtin.shell: '\"bao\" kv put secret/x a=b'\n"
    )));
    assert!(!runs_store_cli(&task(
        "ansible.builtin.command: ansible-vault encrypt x\n"
    )));
    assert!(!runs_store_cli(&task(
        "ansible.builtin.command: ls /opt/vault\n"
    )));
    assert!(runs_store_cli(&task(
        "ansible.builtin.command: bao -address=http://x kv put secret/x a=b\n"
    )));
    assert!(runs_store_cli(&task(
        "ansible.builtin.shell: vault -format=json kv put secret/x a=b\n"
    )));
    assert!(!runs_store_cli(&task(
        "ansible.builtin.debug:\n  msg: bao kv put\n"
    )));
    assert!(runs_store_cli(&task(
        "ansible.builtin.shell: set -e;bao kv put secret/x a=b\n"
    )));
    assert!(runs_store_cli(&task(
        "ansible.builtin.shell: echo $(vault kv put secret/x a=b)\n"
    )));
    assert!(!runs_store_cli(&task(
        "ansible.builtin.command: ls /opt/vault\n"
    )));
}
