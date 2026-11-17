//! The playbook tier of the apply-cloudflare-tofu run with a fake `tofu` on PATH: a failed dry-run
//! `init`, a failed `tofu show` and a Cloudflare plan guard that cannot answer each fail the run,
//! and the guard's failure has one fixed refusal that repeats nothing the guard printed.
//!
//! Each test lifts the playbook's tofu environment, its `OpenTofu run` block and the report tail,
//! and runs them through ansible-playbook against a scratch tofu root, with a fake `tofu` that writes
//! what the real one would and logs each call.
//!
//! Replaces four tests of `platform/tests/test_edge_dns_step_result.py` (plan 24, Phase 4). Port map:
//!
//! | Python test | Rust fn |
//! |---|---|
//! | `test_a_dry_run_init_failure_fails_the_run_and_still_writes_nothing` | `a_dry_run_init_failure_fails_the_run_and_still_writes_nothing` |
//! | `test_a_missing_or_malformed_guard_receipt_has_a_fixed_refusal` (3 cases) | `an_empty_guard_receipt_has_a_fixed_refusal`, `an_invalid_guard_receipt_has_a_fixed_refusal`, `an_incomplete_guard_receipt_has_a_fixed_refusal` |
//! | `test_a_guard_that_cannot_start_has_a_fixed_refusal` | `a_guard_that_cannot_start_has_a_fixed_refusal` |
//! | `test_a_failed_tofu_show_fails_the_run_in_either_mode` (2 cases) | `a_failed_tofu_show_fails_a_dry_run`, `a_failed_tofu_show_fails_a_real_plan` |
//!
//! The Python read stdout only; this reads stdout, then stderr, as the other playbook-tier modules
//! do. The fake-tofu writer, [`install_fake_tofu`], is private here: a candidate for the shared
//! stub-on-PATH helper (S7).

use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use agent_cloud_tests::{playbook, process, repo};
use serde_json::{Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/apply-cloudflare-tofu.yml";
const FIXTURE: &str = "platform/tests/fixtures/cloudflare-ruleset-v5.21.1-update.json";
const EMIT: &str = "platform/playbooks/tasks/emit-step-result.yml";
const GUARD: &str = "platform/playbooks/files/cloudflare_grafana_plan_guard.py";
const SECRET_ORIGIN: &str = "203.0.113.77";
const REFUSAL: &str = "Cloudflare plan guard refused: safe plan receipt is missing or malformed.";

const TAIL: [&str; 5] = [
    "Summarise the tofu run (exit codes and change counts only)",
    "Show the tofu result for action {{ tofu_action }}",
    "Fail on a tofu error (details withheld: the commands carry credentials)",
    "Judge edge-dns on the last plan",
    "Record the edge-dns step result",
];

/// A fake tofu that writes what the real one would: init fills the data directory (TF_DATA_DIR,
/// else .terraform/ in the working directory) and rewrites the lock file unless
/// -lockfile=readonly; plan saves its -out file. It logs each call with the data directory.
const FAKE_TOFU: &str = r##"#!/bin/sh
d="${TF_DATA_DIR:-.terraform}"
echo "$1|$d|$*" >> "$FAKE_TOFU_LOG"
case "$1" in
  init)
    mkdir -p "$d/providers" && touch "$d/terraform.tfstate"
    case " $* " in *" -lockfile=readonly "*) ;; *) echo "# rewritten" >> .terraform.lock.hcl ;; esac
    exit "${FAKE_INIT_RC:-0}" ;;
  plan)
    for a in "$@"; do case "$a" in -out=*) out="${a#-out=}" ;; esac; done
    mkdir -p "$(dirname "$out")" && echo saved > "$out"
    printf '%s\n' "$FAKE_PLAN_STDOUT"
    exit "${FAKE_PLAN_RC:-0}" ;;
  show)
    printf '%s\n' "$FAKE_SHOW_JSON"
    exit "${FAKE_SHOW_RC:-0}" ;;
esac
"##;

const ROOT_FILES: [(&str, &str); 3] = [
    (".terraform.lock.hcl", "# committed lock\n"),
    ("backend.hcl", "bucket = \"b\"\n"),
    ("versions.tf", "terraform {}\n"),
];

fn root_files() -> BTreeMap<String, String> {
    ROOT_FILES
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

/// Writes the fake tofu, mode 0755, into `bin_dir`.
fn install_fake_tofu(bin_dir: &Path) {
    let tofu = bin_dir.join("tofu");
    std::fs::write(&tofu, FAKE_TOFU).unwrap();
    std::fs::set_permissions(&tofu, std::fs::Permissions::from_mode(0o755)).unwrap();
}

/// The fixture's ruleset plan, as `tofu show -json` prints it: the Grafana rule inserted, or
/// (`insertion` false) the same ruleset unchanged.
fn grafana_plan(insertion: bool) -> String {
    let mut fixture: Value =
        serde_json::from_str(&std::fs::read_to_string(repo::path(FIXTURE)).unwrap()).unwrap();
    if !insertion {
        let change = &mut fixture["resource_changes"][0]["change"];
        change["actions"] = json!(["no-op"]);
        change["before"] = change["after"].clone();
        for side in ["before", "after"] {
            change[side]["id"] = json!("11111111111111111111111111111111");
            change[side]["version"] = json!("2");
            change[side]["last_updated"] = json!("2026-10-13T00:00:00Z");
            change[side]["rules"][5]["id"] = json!("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        }
        change["after_unknown"] = json!({});
    }
    json!({"applyable": true, "errored": false, "resource_changes": fixture["resource_changes"]})
        .to_string()
}

#[derive(Default)]
struct Opts {
    check: bool,
    plan_rc: i32,
    init_rc: i32,
    show_rc: i32,
    guard_script: Option<PathBuf>,
    grafana_guard: bool,
}

struct Run {
    success: bool,
    out: String,
    calls: Vec<Vec<String>>,
    root: BTreeMap<String, String>,
}

fn walk(base: &Path, dir: &Path, into: &mut BTreeMap<String, String>) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            walk(base, &path, into);
        } else if path.is_file() {
            let rel = path.strip_prefix(base).unwrap().display().to_string();
            into.insert(rel, std::fs::read_to_string(&path).unwrap());
        }
    }
}

/// Runs the playbook's tofu block and its report through ansible-playbook with a fake tofu.
fn tofu_run(tmp: &Path, opts: &Opts) -> Run {
    let bin_dir = tmp.join("bin");
    let tf_dir = tmp.join("tf");
    let log = tmp.join("tofu.log");
    std::fs::create_dir(&bin_dir).unwrap();
    std::fs::create_dir(&tf_dir).unwrap();
    for (name, text) in ROOT_FILES {
        std::fs::write(tf_dir.join(name), text).unwrap();
    }
    install_fake_tofu(&bin_dir);

    let plays = playbook::plays(PLAYBOOK);
    assert_eq!(plays.len(), 1, "the playbook holds one play");
    let play = &plays[0];
    let mut names = vec!["Build the tofu environment", "OpenTofu run"];
    names.extend(TAIL);
    let emit = repo::path(EMIT).display().to_string();
    let tasks: Vec<Value> = play["tasks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|t| {
            t.get("name")
                .and_then(Value::as_str)
                .is_some_and(|n| names.contains(&n))
        })
        .cloned()
        .map(|mut t| {
            if let Some(include) = t.get_mut("ansible.builtin.include_tasks") {
                *include = json!(emit);
            }
            t
        })
        .collect();
    assert_eq!(tasks.len(), names.len(), "every named task is selected");

    let mut vars = play["vars"]
        .as_object()
        .cloned()
        .expect("the apply playbook's play has no vars mapping");
    let guard = opts
        .guard_script
        .clone()
        .unwrap_or_else(|| repo::path(GUARD));
    for (k, v) in [
        ("_tf_dir", json!(tf_dir.display().to_string())),
        ("tofu_action", json!("plan")),
        ("_zone_id", json!("z")),
        ("_cloudflare_grafana_guard", json!(opts.grafana_guard)),
        ("_tf_guard_script", json!(guard.display().to_string())),
        ("_caddy_ip", json!(SECRET_ORIGIN)),
        (
            "_cf",
            json!({"r2_access_key_id": "a", "r2_secret_access_key": "b", "api_token": "c"}),
        ),
    ] {
        vars.insert(k.to_string(), v);
    }
    let harness = json!([{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": false,
        "vars": vars,
        "tasks": tasks,
    }]);
    let path = playbook::write(tmp, "p.yml", &harness);
    let mut cmd = process::ansible_playbook(tmp, "localhost,", &path.display().to_string());
    let mut path_var = bin_dir.into_os_string();
    path_var.push(":");
    path_var.push(std::env::var_os("PATH").unwrap_or_default());
    cmd.env("PATH", path_var)
        .env("ANSIBLE_SHOW_CUSTOM_STATS", "1")
        .env("FAKE_TOFU_LOG", &log)
        .env("FAKE_INIT_RC", opts.init_rc.to_string())
        .env("FAKE_PLAN_RC", opts.plan_rc.to_string())
        .env("FAKE_PLAN_STDOUT", "No changes.")
        .env("FAKE_SHOW_JSON", grafana_plan(opts.plan_rc == 2))
        .env("FAKE_SHOW_RC", opts.show_rc.to_string());
    if opts.check {
        cmd.arg("--check");
    }
    let output = process::run(&mut cmd);
    let calls = std::fs::read_to_string(&log)
        .map(|text| {
            text.lines()
                .map(|line| line.split('|').map(str::to_string).collect())
                .collect()
        })
        .unwrap_or_default();
    let mut root = BTreeMap::new();
    walk(&tf_dir, &tf_dir, &mut root);
    Run {
        success: output.status.success(),
        out: format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ),
        calls,
        root,
    }
}

fn commands(calls: &[Vec<String>]) -> Vec<&str> {
    calls.iter().map(|c| c[0].as_str()).collect()
}

#[test]
fn a_dry_run_init_failure_fails_the_run_and_still_writes_nothing() {
    let tmp = TempDir::new().unwrap();
    let run = tofu_run(
        tmp.path(),
        &Opts {
            check: true,
            init_rc: 1,
            ..Opts::default()
        },
    );
    assert!(
        !run.success && run.out.contains("'init_rc': 1,"),
        "{}",
        run.out
    );
    assert_eq!(commands(&run.calls), ["init"]);
    assert_eq!(run.root, root_files());
    assert!(!Path::new(&run.calls[0][1]).parent().unwrap().exists());
}

/// One guard receipt case: the guard prints `receipt` and exits 1, and the run must refuse it
/// with the fixed message and never repeat the receipt. Empty, invalid, and incomplete receipts
/// fail closed.
fn assert_receipt_refused(receipt: &str) {
    let tmp = TempDir::new().unwrap();
    let guard_script = tmp.path().join("bad_guard.py");
    std::fs::write(
        &guard_script,
        format!("import sys\nsys.stdout.write('{receipt}')\nsys.exit(1)\n"),
    )
    .unwrap();
    let run = tofu_run(
        tmp.path(),
        &Opts {
            guard_script: Some(guard_script),
            grafana_guard: true,
            ..Opts::default()
        },
    );
    assert!(!run.success, "receipt {receipt:?}: {}", run.out);
    assert!(
        run.out.contains(REFUSAL),
        "receipt {receipt:?}: {}",
        run.out
    );
    assert!(
        receipt.is_empty() || !run.out.contains(receipt),
        "receipt {receipt:?} repeated: {}",
        run.out
    );
}

#[test]
fn an_empty_guard_receipt_has_a_fixed_refusal() {
    assert_receipt_refused("");
}

#[test]
fn an_invalid_guard_receipt_has_a_fixed_refusal() {
    assert_receipt_refused("not-json");
}

#[test]
fn an_incomplete_guard_receipt_has_a_fixed_refusal() {
    assert_receipt_refused("{}");
}

#[test]
fn a_guard_that_cannot_start_has_a_fixed_refusal() {
    let tmp = TempDir::new().unwrap();
    let run = tofu_run(
        tmp.path(),
        &Opts {
            guard_script: Some(tmp.path().join("missing_guard.py")),
            grafana_guard: true,
            ..Opts::default()
        },
    );
    assert!(!run.success, "{}", run.out);
    assert!(run.out.contains(REFUSAL), "{}", run.out);
    assert!(!run.out.contains("missing_guard.py"), "{}", run.out);
}

/// A failed `tofu show` fails the run, in a dry run (`check`) and in a real plan alike.
fn assert_failed_show_fails(check: bool) {
    let tmp = TempDir::new().unwrap();
    let run = tofu_run(
        tmp.path(),
        &Opts {
            check,
            show_rc: 1,
            ..Opts::default()
        },
    );
    assert_eq!(
        commands(&run.calls),
        ["init", "plan", "show"],
        "check={check}"
    );
    assert!(
        !run.success && run.out.contains("'show_rc': 1,"),
        "check={check}: {}",
        run.out
    );
}

#[test]
fn a_failed_tofu_show_fails_a_dry_run() {
    assert_failed_show_fails(true);
}

#[test]
fn a_failed_tofu_show_fails_a_real_plan() {
    assert_failed_show_fails(false);
}
