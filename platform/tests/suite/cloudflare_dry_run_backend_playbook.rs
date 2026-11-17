//! The playbook tier of the apply-cloudflare-tofu dry run: it refuses to plan against a missing or
//! stale `backend.hcl` (plan 24, Phase 4).
//!
//! PR 203 Codex review: under `--check` the backend write is only simulated, so `tofu init` read
//! whatever file the workspace already held, or none. Each test runs the real write and dry-run
//! tasks of the playbook under `--check`, against a scratch directory, once per behaviour.
//!
//! Replaces `platform/tests/test_cloudflare_dry_run_backend.py` (plan 24, Phase 4), which is
//! deleted. Port map:
//!
//! | Python test | Rust fn |
//! |---|---|
//! | `test_a_stale_backend_stops_the_dry_run` | `a_stale_backend_stops_the_dry_run` |
//! | `test_a_missing_backend_stops_the_dry_run` | `a_missing_backend_stops_the_dry_run` |
//! | `test_a_current_backend_lets_the_dry_run_plan` | `a_current_backend_lets_the_dry_run_plan` |
//!
//! The Python `_dry_run` read the playbook with `yaml.safe_load`; here [`playbook::plays`] lifts it
//! with YAML 1.1 types. The Python read stdout only; this reads stdout, then stderr, as the other
//! playbook-tier modules do.
//!
//! Known gap, found while porting (not changed by the port): in the stale and missing cases the
//! play does not reach the `meta: end_play` task. Its `include_tasks` of
//! `tasks/emit-step-result.yml` resolves against the harness file's own directory, a scratch
//! directory with no `tasks/`, and fails first. So these two tests pass with `end_play` removed:
//! the stop they assert is the include failure, not `end_play`.

use std::os::unix::fs::PermissionsExt;

use agent_cloud_tests::{playbook, process};
use serde_json::{Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/apply-cloudflare-tofu.yml";

/// The backend config the playbook's write renders for bucket `b` and the R2 endpoint.
const CONFIGURED: &str = concat!(
    "bucket = \"b\"\n",
    "key    = \"cloudflare/terraform.tfstate\"\n",
    "endpoints = { s3 = \"https://r2.example.invalid\" }\n",
);

/// The tasks the Python `_dry_run` kept: every top-level task of every play whose name starts with
/// one of the two prefixes. The count is asserted, so a renamed task cannot select nothing and let
/// the dry run pass for the wrong reason. The Python test selects four: the write, the debug
/// message, the include and `end_play`. The count is checked before any play runs.
fn wanted_tasks() -> Vec<Value> {
    let tasks: Vec<Value> = playbook::plays(PLAYBOOK)
        .iter()
        .flat_map(|play| {
            play.get("tasks")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
        })
        .filter(|task| {
            task.get("name")
                .and_then(Value::as_str)
                .is_some_and(|name| {
                    name.starts_with("Write the R2 backend config")
                        || name.starts_with("Dry run with a")
                })
        })
        .collect();
    assert_eq!(
        tasks.len(),
        4,
        "the dry run must select the write and its three dry-run tasks by name"
    );
    tasks
}

/// Stdout then stderr, as `support_lift_playbook` and the other playbook-tier modules read a run.
fn output_of(out: &std::process::Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

/// Runs the playbook's write and dry-run tasks under `--check`, with `existing` as the
/// `backend.hcl` the workspace already holds (`None` when it holds none), and returns the output.
/// A marker task after them prints `REACHED-INIT` when the dry run gets past its guard.
fn dry_run(existing: Option<&str>) -> String {
    let mut tasks = wanted_tasks();
    let dir = TempDir::new().unwrap();
    let tf_dir = dir.path().join("tf");
    std::fs::create_dir(&tf_dir).unwrap();
    if let Some(text) = existing {
        let file = tf_dir.join("backend.hcl");
        std::fs::write(&file, text).unwrap();
        // As the real write leaves it.
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600)).unwrap();
    }
    tasks.push(json!({
        "name": "Reached init",
        "ansible.builtin.debug": {"msg": "REACHED-INIT"},
    }));
    let harness = json!([{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": false,
        "vars": {
            "_tf_dir": tf_dir.display().to_string(),
            "_bucket": "b",
            "_r2_endpoint": "https://r2.example.invalid",
        },
        "tasks": tasks,
    }]);
    let path = playbook::write(dir.path(), "dry.json", &harness);
    let mut cmd = process::ansible_playbook(dir.path(), "localhost,", &path.display().to_string());
    cmd.arg("--check");
    output_of(&process::run(&mut cmd))
}

/// The dry run stopped before `init`: it printed the refusal and never reached the marker.
fn assert_stopped(out: &str) {
    assert!(out.contains("missing or differs"), "{out}");
    assert!(!out.contains("REACHED-INIT"), "{out}");
}

#[test]
fn a_stale_backend_stops_the_dry_run() {
    let stale = CONFIGURED.replace("\"b\"", "\"old-bucket\"");
    assert_stopped(&dry_run(Some(stale.as_str())));
}

#[test]
fn a_missing_backend_stops_the_dry_run() {
    assert_stopped(&dry_run(None));
}

#[test]
fn a_current_backend_lets_the_dry_run_plan() {
    let out = dry_run(Some(CONFIGURED));
    assert!(out.contains("REACHED-INIT"), "{out}");
    assert!(!out.contains("missing or differs"), "{out}");
}
