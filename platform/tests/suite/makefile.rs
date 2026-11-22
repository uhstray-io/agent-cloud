//! The root Makefile keeps its shape (static tier, area `makefile`).
//!
//! The Makefile is the local-dev front door: its targets delegate to `scripts/local-dev.sh`, the
//! full stack comes up in order, and every target shows in `make help`. The file is read as rules
//! and recipes, never run. A rule header is a line that begins with a target name and a colon (not
//! `:=`); a recipe is the tab-indented lines under the header.
//!
//! Replaces platform/tests/test_makefile.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | Makefile: file exists at repo root | `the_makefile_exists_at_the_repository_root` |
//! | Makefile: uses LOCAL_DEV variable pointing to scripts/local-dev.sh | `the_local_dev_variable_names_the_local_dev_script` |
//! | Makefile: all documented concrete targets are declared | `every_documented_core_target_is_declared` |
//! | Makefile: pattern rules (local-deploy-%, local-clean-deploy-%) are declared | `the_pattern_rules_are_declared` |
//! | Makefile: .PHONY declares the core targets | `phony_declares_the_core_targets` |
//! | Makefile: help target uses ## to extract docs (grep -E pattern) | `the_help_target_extracts_its_docs_from_double_hash_comments` (candidate: pins text) |
//! | Makefile: every concrete target carries a ## doc comment (for make help) | `every_concrete_target_carries_a_double_hash_doc_comment` |
//! | Makefile: local-up runs local-bootstrap BEFORE the deploy targets | `local_up_runs_local_bootstrap_before_any_local_deploy` |
//! | Makefile: local-up deploys o11y, opa, erpnext after bootstrap | `local_up_deploys_o11y_opa_and_erpnext` |
//! | Makefile: core targets delegate to $(LOCAL_DEV) with the right subcommand | `the_core_targets_delegate_to_local_dev_with_their_subcommand` |

use std::collections::BTreeSet;
use std::fs;

use agent_cloud_tests::repo;
use regex::Regex;

/// The targets a developer runs by name. Each must be declared.
const CORE_TARGETS: [&str; 19] = [
    "help",
    "local-preflight",
    "local-init",
    "local-bootstrap",
    "local-up",
    "local-all",
    "local-creds",
    "local-validate",
    "local-smoke",
    "local-netbox",
    "local-netbox-discover",
    "local-dns",
    "local-dns-resolver",
    "local-https",
    "local-https-down",
    "local-tls-trust",
    "local-tls-untrust",
    "local-clean",
    "promote",
];

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("valid regex")
}

fn makefile() -> String {
    fs::read_to_string(repo::path("Makefile")).expect("the root Makefile")
}

/// A rule header. A name starts with a letter and holds letters, digits, `_`, `-`, and `%` (a
/// pattern rule), followed directly by a colon that is not `:=`. So `.PHONY:` and variable
/// assignments are not headers.
struct Header<'a> {
    name: &'a str,
    line: &'a str,
}

fn headers(text: &str) -> Vec<Header<'_>> {
    text.lines()
        .filter_map(|line| {
            let end = line
                .find(|c: char| !(c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '%')))
                .unwrap_or(line.len());
            let (name, rest) = line.split_at(end);
            let starts_with_letter = name.starts_with(|c: char| c.is_ascii_alphabetic());
            (starts_with_letter && rest.starts_with(':') && !rest.starts_with(":="))
                .then_some(Header { name, line })
        })
        .collect()
}

/// The recipe of one target: the tab-indented lines after its first header, up to the first line
/// that is not part of the recipe (a blank line ends it).
fn recipe(text: &str, target: &str) -> Vec<String> {
    let lines: Vec<&str> = text.lines().collect();
    let header = format!("{target}:");
    let Some(start) = lines
        .iter()
        .position(|line| line.starts_with(header.as_str()))
    else {
        return vec![];
    };
    lines[start + 1..]
        .iter()
        .take_while(|line| line.starts_with('\t'))
        .map(|line| (*line).to_owned())
        .collect()
}

#[test]
fn the_makefile_exists_at_the_repository_root() {
    // Arrange
    let path = repo::path("Makefile");

    // Act
    let exists = path.is_file();

    // Assert
    assert!(exists, "no Makefile at {}", path.display());
}

#[test]
fn the_local_dev_variable_names_the_local_dev_script() {
    // Arrange
    let text = makefile();

    // Act
    let assigned = text
        .lines()
        .filter_map(|line| line.split_once(":="))
        .any(|(name, value)| name.trim() == "LOCAL_DEV" && value.contains("scripts/local-dev.sh"));

    // Assert
    assert!(
        assigned,
        "no `LOCAL_DEV := ...scripts/local-dev.sh` assignment"
    );
}

#[test]
fn every_documented_core_target_is_declared() {
    // Arrange
    let text = makefile();
    let declared: BTreeSet<&str> = headers(&text).iter().map(|h| h.name).collect();

    // Act
    let missing: Vec<&str> = CORE_TARGETS
        .into_iter()
        .filter(|target| !declared.contains(target))
        .collect();

    // Assert
    assert!(missing.is_empty(), "missing concrete targets: {missing:?}");
}

#[test]
fn the_pattern_rules_are_declared() {
    // Arrange
    let text = makefile();
    let declared: BTreeSet<&str> = headers(&text).iter().map(|h| h.name).collect();

    // Act
    let missing: Vec<&str> = ["local-deploy-%", "local-clean-deploy-%"]
        .into_iter()
        .filter(|rule| !declared.contains(rule))
        .collect();

    // Assert
    assert!(missing.is_empty(), "missing pattern rules: {missing:?}");
}

#[test]
fn phony_declares_the_core_targets() {
    // Arrange
    let text = makefile();

    // Act
    let phony: BTreeSet<&str> = text
        .lines()
        .filter_map(|line| line.strip_prefix(".PHONY:"))
        .flat_map(|targets| targets.split_whitespace())
        .collect();

    // Assert
    assert!(!phony.is_empty(), "no .PHONY declaration");
    let missing: Vec<&str> = ["local-bootstrap", "local-clean", "promote"]
        .into_iter()
        .filter(|target| !phony.contains(target))
        .collect();
    assert!(missing.is_empty(), "not .PHONY: {missing:?}");
}

#[test]
fn the_help_target_extracts_its_docs_from_double_hash_comments() {
    // Arrange
    let text = makefile();

    // Act
    let help = recipe(&text, "help").join("\n");

    // Assert
    assert!(
        help.contains("grep -E"),
        "the help recipe does not grep: {help}"
    );
    assert!(
        help.contains("##"),
        "the help recipe does not read ## comments: {help}"
    );
}

#[test]
fn every_concrete_target_carries_a_double_hash_doc_comment() {
    // Arrange: a target's header may repeat (target-specific variables), and any header that
    // carries the comment documents it, as `make help` reads it.
    let text = makefile();
    let all = headers(&text);
    let concrete: BTreeSet<&str> = all
        .iter()
        .map(|h| h.name)
        .filter(|name| !name.contains('%'))
        .collect();

    // Act
    let undocumented: Vec<&str> = concrete
        .into_iter()
        .filter(|name| !all.iter().any(|h| h.name == *name && h.line.contains("##")))
        .collect();

    // Assert
    assert!(
        undocumented.is_empty(),
        "targets with no ## doc comment: {undocumented:?}"
    );
}

#[test]
fn local_up_runs_local_bootstrap_before_any_local_deploy() {
    // Arrange
    let text = makefile();
    let body = recipe(&text, "local-up");
    assert!(!body.is_empty(), "local-up has no recipe");

    // Act
    let boot = body
        .iter()
        .position(|line| line.contains("local-bootstrap"));
    let deploy = body.iter().position(|line| line.contains("local-deploy"));

    // Assert: the order within the recipe, not mere presence
    match (boot, deploy) {
        (Some(boot), Some(deploy)) => assert!(
            boot < deploy,
            "local-bootstrap (recipe line {boot}) runs after the first local-deploy (line {deploy})"
        ),
        _ => panic!("local-up needs a local-bootstrap line and a local-deploy line: {body:?}"),
    }
}

#[test]
fn local_up_deploys_o11y_opa_and_erpnext() {
    // Arrange
    let text = makefile();
    let body = recipe(&text, "local-up").join("\n");

    // Act
    let missing: Vec<&str> = [
        "local-deploy-o11y",
        "local-deploy-opa",
        "local-deploy-erpnext",
    ]
    .into_iter()
    .filter(|target| !body.contains(*target))
    .collect();

    // Assert
    assert!(missing.is_empty(), "local-up does not deploy {missing:?}");
}

#[test]
fn the_core_targets_delegate_to_local_dev_with_their_subcommand() {
    // Arrange: each target's recipe must run `$(LOCAL_DEV) <subcommand>`, not a comment or
    // unrelated text that happens to name the word.
    let text = makefile();
    let cases = [
        ("local-bootstrap", "bootstrap"),
        ("local-clean", "clean"),
        ("promote", "promote"),
        ("local-deploy-%", "deploy"),
    ];

    // Act
    let mut not_delegating: Vec<String> = vec![];
    for (target, subcommand) in cases {
        let delegate = re(&format!(r"\$\(LOCAL_DEV\)[ \t]+{subcommand}"));
        let body = recipe(&text, target).join("\n");
        if !delegate.is_match(&body) {
            not_delegating.push(format!("{target} does not run $(LOCAL_DEV) {subcommand}"));
        }
    }

    // Assert
    assert!(not_delegating.is_empty(), "{}", not_delegating.join("\n"));
}
