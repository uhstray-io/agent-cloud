//! The dedicated-account probe of `platform/playbooks/inspect-host-containers.yml` (playbook tier).
//! The probe runs as `_probe_account`: the host's `linger_user` when it declares one, else the
//! `github_runner_account` the runner hosts declare. It does not run when neither is set, or when
//! that account is the connecting user's.
//!
//! The play's own vars block is lifted and run against a stub inventory, one host per case, and
//! the probe task's own `when` list is copied onto a second task. So the test exercises the real
//! Jinja rather than a copy of it. Each host writes the account the probe resolves to, and writes a
//! run marker only where the probe's `when` lets it run. Runs go through `process::ansible_playbook`,
//! so they are bounded, and confined to the test's directory where a kernel sandbox (sandbox-exec,
//! bwrap) is present (`process::sandbox`).

use agent_cloud_tests::{playbook, process};
use serde_json::{Map, Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/inspect-host-containers.yml";
const PROBE: &str = "Rootless podman (the host's dedicated service account)";

/// Runs the lifted vars and the probe's `when` against `hosts` (name, host vars), in one run. Each
/// host's account lands in `dir/account-<host>`, and a run marker in `dir/runs-<host>` where the
/// probe's `when` lets it run. Returns whether the run passed, and its output.
fn render(dir: &TempDir, hosts: &[(&str, Value)]) -> (bool, String) {
    let play = playbook::plays(PLAYBOOK).remove(0);
    let probes: Vec<&Value> = play["tasks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|t| t["name"] == PROBE)
        .collect();
    assert_eq!(probes.len(), 1, "one dedicated-account probe task");
    let when = probes[0]["when"].clone();
    assert!(when.is_array(), "the probe task has a when list");
    let mut targets = Map::new();
    for (name, host_vars) in hosts {
        let mut vars = json!({"ansible_connection": "local"});
        vars.as_object_mut()
            .unwrap()
            .extend(host_vars.as_object().unwrap().clone());
        targets.insert((*name).into(), vars);
    }
    let inv = playbook::write(
        dir.path(),
        "inv.json",
        &json!({"all": {"children": {"targets": {"hosts": targets}}}}),
    );
    let account = format!(
        "{}/account-{{{{ inventory_hostname }}}}",
        dir.path().display()
    );
    let runs = format!("{}/runs-{{{{ inventory_hostname }}}}", dir.path().display());
    let lifted = json!([{"hosts": "targets", "gather_facts": false, "vars": play["vars"],
    "tasks": [
        {"name": "account", "ansible.builtin.copy": {
            "content": "account={{ _probe_account }}\n", "dest": account, "mode": "0600"}},
        {"name": "runs", "when": when, "ansible.builtin.copy": {
            "content": "run\n", "dest": runs, "mode": "0600"}},
    ]}]);
    let path = playbook::write(dir.path(), "play.json", &lifted);
    let mut cmd = process::ansible_playbook(
        dir.path(),
        &inv.display().to_string(),
        &path.display().to_string(),
    );
    let run = process::run(&mut cmd);
    (
        run.status.success(),
        format!(
            "{}{}",
            String::from_utf8_lossy(&run.stdout),
            String::from_utf8_lossy(&run.stderr)
        ),
    )
}

#[test]
fn the_probe_runs_as_the_linger_user_else_the_runner_account() {
    let dir = TempDir::new().unwrap();
    // One host per case, each with its own vars.
    let hosts = [
        // A runner host: no linger_user, so the runner account; it differs from the connecting user.
        (
            "runner_only",
            json!({"github_runner_account": "ghrunner", "ansible_user": "ops"}),
        ),
        // A declared linger_user wins over the runner account.
        (
            "linger_host",
            json!({"linger_user": "svc", "github_runner_account": "ghrunner"}),
        ),
        // Neither declared: no account, so the probe does not run.
        ("undeclared", json!({})),
        // The runner account is the connecting user: the probe does not run, as the rootless probe
        // already covers that account.
        (
            "own_account",
            json!({"github_runner_account": "ops", "ansible_user": "ops"}),
        ),
    ];
    // (host, the account the probe resolves to, whether the probe runs there)
    let expected = [
        ("runner_only", "ghrunner", true),
        ("linger_host", "svc", true),
        ("undeclared", "", false),
        ("own_account", "ops", false),
    ];
    let (ok, output) = render(&dir, &hosts);
    assert!(ok, "{output}");
    let mut problems = vec![];
    for (host, account, runs) in expected {
        let want = format!("account={account}\n");
        let got = std::fs::read_to_string(dir.path().join(format!("account-{host}"))).ok();
        if got.as_deref() != Some(want.as_str()) {
            problems.push(format!("{host}: account {got:?}, expected {want:?}"));
        }
        let ran = dir.path().join(format!("runs-{host}")).exists();
        if ran != runs {
            problems.push(format!("{host}: the probe ran: {ran}, expected {runs}"));
        }
    }
    assert!(problems.is_empty(), "{}\n{output}", problems.join("\n"));
}
