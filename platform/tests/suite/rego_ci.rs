//! The static tier of the Rego CI job in `.github/workflows/lint-and-test.yml` (plan 24, Phase 4).
//!
//! The job installs only the reviewed OPA release, checks it against its published digest before
//! anything runs it, runs `opa check` and `opa test` over the policy tree, and the `Unit Tests` gate
//! requires the job to succeed. Replaces `platform/tests/test_rego_ci.py` (plan 24, Phase 4).
//! Structural only: a YAML read of the workflow, no run.
//!
//! Port map (pytest test → Rust fn):
//!
//! | pytest test | Rust fn |
//! |---|---|
//! | `test_rego_job_verifies_opa_before_use_and_keeps_the_gate` | `rego_job_verifies_opa_before_use_and_keeps_the_gate` |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

const WORKFLOW: &str = ".github/workflows/lint-and-test.yml";

/// The mapping value under `key`, panicking with the key when it is missing.
fn field<'a>(node: &'a Node, key: &str) -> &'a Node {
    node.get(key)
        .unwrap_or_else(|| panic!("{WORKFLOW}: no `{key}` key"))
}

/// The string under `key`, panicking when it is missing or not a string.
fn str_field<'a>(node: &'a Node, key: &str) -> &'a str {
    field(node, key)
        .as_str()
        .unwrap_or_else(|| panic!("{WORKFLOW}: `{key}` is not a string"))
}

/// The step of a job whose `name` is `name`, as the Python `next(...)` found it.
fn step<'a>(job: &'a Node, name: &str) -> &'a Node {
    field(job, "steps")
        .as_seq()
        .unwrap_or_else(|| panic!("{WORKFLOW}: `steps` is not a list"))
        .iter()
        .find(|step| step.get("name").and_then(Node::as_str) == Some(name))
        .unwrap_or_else(|| panic!("{WORKFLOW}: no step named `{name}`"))
}

/// Python's `want in value` for a `needs` value: a list holding `want`, or a string containing it.
fn contains_item(value: &Node, want: &str) -> bool {
    match value {
        Node::Str(s) => s.contains(want),
        Node::Seq(items) => items.iter().any(|item| item.as_str() == Some(want)),
        _ => false,
    }
}

#[test]
fn rego_job_verifies_opa_before_use_and_keeps_the_gate() {
    let workflow = yaml::load_one(&repo::path(WORKFLOW)).expect("the workflow is empty");
    let jobs = field(&workflow, "jobs");
    let job = field(jobs, "rego");
    // The loader keeps a plain number as its text, so `timeout-minutes: 10` reads as "10".
    assert_eq!(
        job.get("timeout-minutes").and_then(Node::as_str),
        Some("10"),
        "{WORKFLOW}: the rego job's timeout is not 10 minutes"
    );

    let install = step(job, "Install verified OPA 1.0.0");
    let script = str_field(install, "run");
    assert!(
        script.contains("https://openpolicyagent.org/downloads/v1.0.0/opa_linux_amd64_static"),
        "{WORKFLOW}: the install step does not fetch the reviewed OPA 1.0.0 release"
    );
    assert_eq!(
        install
            .get("env")
            .and_then(|env| env.get("OPA_SHA256"))
            .and_then(Node::as_str),
        Some("3985fb4814b3860511beac516f59c5509eee9fdc83dbe910b67f3999f743b901"),
        "{WORKFLOW}: OPA_SHA256 is not the reviewed digest"
    );
    assert!(
        script.contains("--retry 3 --retry-all-errors"),
        "{WORKFLOW}: the install step does not retry the download"
    );
    assert!(
        script.contains("--connect-timeout 10 --max-time 60"),
        "{WORKFLOW}: the install step has no connect or total timeout"
    );
    let check = script
        .find("sha256sum --check")
        .expect("the install step never checks the digest");
    let chmod = script
        .find("chmod 755")
        .expect("the install step never marks OPA executable");
    assert!(
        check < chmod,
        "{WORKFLOW}: the digest check (byte {check}) comes after chmod (byte {chmod})"
    );

    let run = str_field(step(job, "Run Rego tests"), "run");
    assert!(
        run.contains("opa check --strict platform/services/opa/deployment/policies"),
        "{WORKFLOW}: the Rego step does not run `opa check --strict` over the policy tree"
    );
    assert!(
        run.contains("opa test platform/services/opa/deployment/policies -v"),
        "{WORKFLOW}: the Rego step does not run `opa test -v` over the policy tree"
    );

    let gate = field(jobs, "test");
    assert_eq!(
        gate.get("if").and_then(Node::as_str),
        Some("always()"),
        "{WORKFLOW}: the Unit Tests gate must run even when a group failed"
    );
    assert!(
        contains_item(field(gate, "needs"), "rego"),
        "{WORKFLOW}: the Unit Tests gate does not need the rego job"
    );
    let first_gate_step = field(gate, "steps")
        .as_seq()
        .and_then(|steps| steps.first())
        .unwrap_or_else(|| panic!("{WORKFLOW}: the Unit Tests gate has no steps"));
    assert!(
        str_field(first_gate_step, "run").contains(r#"test "$REGO_RESULT" = success"#),
        "{WORKFLOW}: the gate does not require the rego result to be success"
    );
}
