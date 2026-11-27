//! The dedicated-account probe of `platform/playbooks/inspect-host-containers.yml` (playbook tier).
//! The play's own vars and the probe task's own `when` list are lifted and run against stub hosts,
//! so the test exercises the real Jinja rather than a copy of it. Runs go through
//! `process::ansible_playbook` (bounded, and sandboxed where a kernel sandbox is present).

use std::path::Path;

use agent_cloud_tests::{playbook, process};
use serde_json::{Map, Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/inspect-host-containers.yml";
const PROBE: &str = "Rootless podman (the host's dedicated service account)";

/// The play's one task named `name`.
fn task(play: &Value, name: &str) -> Value {
    let found: Vec<&Value> = play["tasks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|t| t["name"] == name)
        .collect();
    assert_eq!(found.len(), 1, "one task named {name}");
    found[0].clone()
}

/// Runs the lifted vars and the probe's `when` against `hosts` (name, host vars), in one run. Each
/// host's account lands in `dir/account-<host>`, and a run marker in `dir/runs-<host>` where the
/// probe's `when` lets it run. Returns whether the run passed, and its output.
fn render(dir: &Path, hosts: &[(&str, Value)]) -> (bool, String) {
    let play = playbook::plays(PLAYBOOK).remove(0);
    let when = task(&play, PROBE)["when"].clone();
    assert!(when.is_array(), "the probe task has a when list");
    let mut targets = Map::new();
    for (name, host_vars) in hosts {
        let mut vars = host_vars.clone();
        vars["ansible_connection"] = json!("local");
        targets.insert((*name).into(), vars);
    }
    let inv = playbook::write(
        dir,
        "inv.json",
        &json!({"all": {"children": {"targets": {"hosts": targets}}}}),
    );
    let base = dir.display();
    let account = format!("{base}/account-{{{{ inventory_hostname }}}}");
    let runs = format!("{base}/runs-{{{{ inventory_hostname }}}}");
    let lifted = json!([{"hosts": "targets", "gather_facts": false, "vars": play["vars"],
    "tasks": [
        {"name": "account", "ansible.builtin.copy": {
            "content": "account={{ _probe_account }}\n", "dest": account, "mode": "0600"}},
        {"name": "runs", "when": when, "ansible.builtin.copy": {
            "content": "run\n", "dest": runs, "mode": "0600"}},
    ]}]);
    let path = playbook::write(dir, "play.json", &lifted);
    let mut cmd =
        process::ansible_playbook(dir, &inv.display().to_string(), &path.display().to_string());
    let run = process::run(&mut cmd);
    (run.status.success(), process::output_text(&run))
}

#[test]
fn the_probe_runs_as_the_linger_user_else_the_runner_account() {
    let dir = TempDir::new().unwrap();
    // (host, its vars, the account the probe resolves to, whether the probe runs there)
    let cases = [
        // A runner host: no linger_user, so the runner account; it differs from the connecting user.
        (
            "runner_only",
            json!({"github_runner_account": "ghrunner", "ansible_user": "ops"}),
            "ghrunner",
            true,
        ),
        // A declared linger_user wins over the runner account.
        (
            "linger_host",
            json!({"linger_user": "svc", "github_runner_account": "ghrunner"}),
            "svc",
            true,
        ),
        // Neither declared: no account, so the probe does not run.
        ("undeclared", json!({}), "", false),
        // The runner account is the connecting user, whom the rootless probe already covers.
        (
            "own_account",
            json!({"github_runner_account": "ops", "ansible_user": "ops"}),
            "ops",
            false,
        ),
    ];
    let hosts: Vec<(&str, Value)> = cases
        .iter()
        .map(|(host, vars, _, _)| (*host, vars.clone()))
        .collect();
    let (ok, output) = render(dir.path(), &hosts);
    assert!(ok, "{output}");
    let mut problems = vec![];
    for (host, _, account, runs) in cases {
        let want = format!("account={account}\n");
        let got =
            std::fs::read_to_string(dir.path().join(format!("account-{host}"))).unwrap_or_default();
        if got != want {
            problems.push(format!("{host}: account {got:?}, expected {want:?}"));
        }
        let ran = dir.path().join(format!("runs-{host}")).exists();
        if ran != runs {
            problems.push(format!("{host}: the probe ran: {ran}, expected {runs}"));
        }
    }
    assert!(problems.is_empty(), "{}\n{output}", problems.join("\n"));
}

#[test]
fn the_probe_becomes_the_account_it_resolved_and_the_report_names_it() {
    // The stub run above has no `become`, so only a static check sees `become_user` (L-021).
    let play = playbook::plays(PLAYBOOK).remove(0);
    assert_eq!(task(&play, PROBE)["become_user"], "{{ _probe_account }}");
    let report = task(&play, "Report");
    let dedicated = report["ansible.builtin.debug"]["msg"]["rootless_podman_dedicated_account"]
        .as_str()
        .expect("the Report names the dedicated account's output");
    assert!(dedicated.contains("{_probe_account:"), "{dedicated}");
}
