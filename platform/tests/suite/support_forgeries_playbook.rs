//! The render-count helpers of `src/forgeries.rs` run through ansible-playbook (mirrors the run
//! tests of `platform/tests/test_forgeries_render_count.py`). Each run is a localhost play whose
//! TASKS debug tasks each print the variable once, so the number of tasks is the number of
//! readings.

use std::path::Path;

use agent_cloud_tests::{forgeries, playbook, process};
use serde_json::json;
use tempfile::TempDir;

const TASKS: usize = 3;

/// The value each debug task printed, in order, from one run with the `-e` JSON `extra`.
fn values(dir: &Path, extra: &str) -> Vec<String> {
    let tasks: Vec<_> = (0..TASKS)
        .map(|i| json!({"ansible.builtin.debug": {"msg": format!("READ {i} {{{{ probe }}}}")}}))
        .collect();
    let play = json!([{"hosts": "localhost", "connection": "local", "gather_facts": false, "tasks": tasks}]);
    let path = playbook::write(dir, "play.json", &play);
    let mut cmd = process::ansible_playbook(dir, "localhost,", &path.display().to_string());
    cmd.arg("-e").arg(extra);
    let out = process::run(&mut cmd);
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    assert!(
        out.status.success(),
        "{stdout}{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let re = regex::Regex::new(r#""msg": "READ \d+ ([a-z]+)""#).unwrap();
    let found: Vec<String> = re
        .captures_iter(&stdout)
        .map(|c| c[1].to_string())
        .collect();
    assert_eq!(found.len(), TASKS, "{stdout}");
    found
}

#[test]
fn counting_counts_each_reading_of_the_name() {
    let dir = TempDir::new().unwrap();
    let (extra, counter) = forgeries::counting("probe", &json!("seen"), dir.path());
    assert_eq!(values(dir.path(), &extra), vec!["seen"; TASKS]);
    let n: usize = std::fs::read_to_string(&counter)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_eq!(n, TASKS);
}

#[test]
fn honest_for_renders_the_honest_value_first_then_the_other() {
    let dir = TempDir::new().unwrap();
    let extra = forgeries::honest_for("probe", &json!("honest"), &json!("other"), 2, dir.path());
    assert_eq!(values(dir.path(), &extra), ["honest", "honest", "other"]);
}

#[test]
fn the_stateful_spelling_renders_honest_once_then_forged() {
    let dir = TempDir::new().unwrap();
    let stateful = &forgeries::templated_forgeries("probe", json!("honest"), json!("forged"))[2];
    let extra = stateful.extra_vars(dir.path());
    assert_eq!(values(dir.path(), &extra), ["honest", "forged", "forged"]);
}
