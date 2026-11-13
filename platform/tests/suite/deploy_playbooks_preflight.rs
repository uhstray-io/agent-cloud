//! Every deploy playbook that targets an inventory group refuses an empty one first (static
//! tier, security set).
//!
//! A play whose `hosts:` matches nothing prints "skipping: no hosts matched" and exits 0, so
//! Semaphore records a deploy that did nothing as a success. It happened twice (docs/MISTAKES.md
//! 2.21). The guard exists (`preflight-target-group.yml`) but is opt-in per playbook, so this
//! module derives the population from the playbooks themselves instead of naming a few.
//!
//! A deploy playbook is compliant when every play that targets something other than localhost is
//! preceded by a pre-flight covering that target. Accepted forms:
//!
//! 1. `import_playbook: preflight-target-group.yml` with `preflight_group` and
//!    `preflight_group_expected` both set, non-empty and equal. It covers that exact group; a
//!    `hosts: a:b` union needs both covered.
//! 2. An inline localhost play whose assert reads `groups` and names the target, with ONE
//!    comparison of the `length` that only a populated group passes: `> 0`, `!= 0`, or `>=`/`==`
//!    a positive integer, and no `and`/`or`/`not`/`if`/`else`.
//!
//! An `import_playbook` of anything else is followed, so a thin wrapper over a guarded playbook is
//! judged by the plays it runs.
//!
//! The ratchet (`known_deploy_without_preflight.txt`) is not an allow-list: every offender must be
//! named there, and every name there must still be an offender. It only shrinks.
//!
//! Replaces platform/tests/test_deploy_playbooks_preflight.bats (plan 24, Phase 4). The checker is
//! the Python program the BATS file embedded, ported with its ordering rules: coverage accumulates
//! in play order, and a missing or templated import contributes nothing.
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | every deploy playbook targeting a group runs the zero-hosts pre-flight first (ratchet) | `every_deploy_playbook_targeting_a_group_is_guarded_or_on_the_ratchet` |
//! | the known compliant deploys are recognised as compliant | `the_known_compliant_deploys_are_not_offenders` |
//! | checker: a deploy with no pre-flight is an offender | `a_deploy_with_no_preflight_is_an_offender` |
//! | checker: a pre-flight imported AFTER the first group play does not count | `a_preflight_imported_after_the_first_group_play_does_not_count` |
//! | checker: a pre-flight for a different group, or with one var, does not count | `a_preflight_for_another_group_or_with_one_var_does_not_count` |
//! | checker: a union target needs every group covered | `a_union_target_needs_every_group_covered` |
//! | checker: both accepted forms, and a wrapper over a guarded playbook, are compliant | `both_accepted_forms_and_a_wrapper_over_a_guarded_playbook_are_compliant` |
//! | ratchet: a new offender not listed fails, and a listed playbook that complies fails | `the_ratchet_fails_a_new_offender_and_a_listed_playbook_that_complies` |
//! | checker: the repository's own inline guards are recognised | `the_repositorys_own_inline_guards_are_recognised` |
//! | checker: an inline assert that an EMPTY group also passes does not count | `inline_asserts_that_are_not_one_accepted_positive_comparison_do_not_count` |
//! | checker: each accepted positive comparison counts | `each_accepted_positive_comparison_counts` |

use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const IMPORT_KEYS: [&str; 2] = ["ansible.builtin.import_playbook", "import_playbook"];
const ASSERT_KEYS: [&str; 2] = ["ansible.builtin.assert", "assert"];
const TASK_LISTS: [&str; 7] = [
    "tasks",
    "pre_tasks",
    "post_tasks",
    "handlers",
    "block",
    "rescue",
    "always",
];
const PREFLIGHT: &str = "preflight-target-group.yml";
const LOCAL: [&str; 2] = ["localhost", "127.0.0.1"];
const PLAYBOOKS: &str = "platform/playbooks";
const RATCHET: &str = "platform/tests/known_deploy_without_preflight.txt";

// Rust's regex has no lookaround, so "not followed by a digit or a dot" is written as a
// consuming `[^\d.]` or the end of the text. In a whole-text match that is the same condition.
static POSITIVE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"\blength\b\s*\)*\s*(?:>\s*0(?:[^\d.]|$)|!=\s*0(?:[^\d.]|$)|(?:>=|==)\s*[1-9]\d*(?:[^\d.]|$))",
    )
});
static COMPARATOR: LazyLock<Regex> = LazyLock::new(|| re(r"[<>!=]=|[<>]"));
static NEUTRALISER: LazyLock<Regex> = LazyLock::new(|| re(r"\b(?:and|or|not|if|else)\b"));
static BRACED_NAME: LazyLock<Regex> = LazyLock::new(|| re(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)"));

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("valid regex")
}

/// What the checker found in one directory of playbooks.
struct Report {
    /// Each offending file with its first reason, in file-name order.
    offenders: Vec<(String, String)>,
    /// Deploy playbooks that target a group other than localhost.
    checked: usize,
}

/// Every `deploy-*.yml` in `dir`, checked in name order.
fn offenders(dir: &Path) -> Report {
    let mut names: Vec<String> = fs::read_dir(dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .map(|entry| {
            entry
                .expect("a directory entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .filter(|name| name.starts_with("deploy-") && name.ends_with(".yml"))
        .collect();
    names.sort();

    let mut report = Report {
        offenders: vec![],
        checked: 0,
    };
    for name in names {
        let (reason, targets) = check_playbook(&dir.join(&name));
        report.checked += usize::from(targets);
        if let Some(reason) = reason {
            report.offenders.push((name, reason));
        }
    }
    report
}

/// The first reason a playbook is non-compliant (`None` when it is compliant), and whether it
/// targets a group at all.
fn check_playbook(path: &Path) -> (Option<String>, bool) {
    let mut groups: BTreeSet<String> = BTreeSet::new();
    let mut inline: Vec<String> = vec![];
    let mut reasons: Vec<String> = vec![];
    let mut targets = false;

    for play in plays(path, &[]) {
        if import_target(&play).is_some() {
            match preflight_group(&play) {
                Some(group) => {
                    groups.insert(group);
                }
                None => reasons.push(
                    "pre-flight import without preflight_group == preflight_group_expected"
                        .to_owned(),
                ),
            }
            continue;
        }
        let Some(hosts) = hosts_text(&play) else {
            continue;
        };
        if LOCAL.contains(&hosts.trim()) {
            inline.extend(inline_guard_items(&play));
            continue;
        }
        targets = true;
        for subject in target_parts(&hosts) {
            if !groups.contains(&subject) && !covered_inline(&subject, &inline) {
                reasons.push(format!(
                    "play {} targets {subject:?} before any pre-flight covers it",
                    play_name(&play)
                ));
            }
        }
    }
    (reasons.into_iter().next(), targets)
}

/// The plays a playbook runs, in order, with every non-pre-flight `import_playbook` expanded. A
/// missing or templated target contributes nothing. `seen` holds the playbooks already on this
/// import chain, so a cycle ends.
fn plays(path: &Path, seen: &[PathBuf]) -> Vec<Node> {
    if seen.iter().any(|p| p == path) || !path.is_file() {
        return vec![];
    }
    let mut seen = seen.to_vec();
    seen.push(path.to_path_buf());

    let mut out = vec![];
    let Some(Node::Seq(items)) = yaml::load_one(path) else {
        return out;
    };
    for play in items.iter().filter(|item| item.as_map().is_some()) {
        match import_target(play) {
            Some(target) if Path::new(target).file_name() != Some(OsStr::new(PREFLIGHT)) => {
                let dir = path.parent().unwrap_or(Path::new("."));
                out.extend(plays(&dir.join(target), &seen));
            }
            _ => out.push(play.clone()),
        }
    }
    out
}

/// The string an import key names, for a play that carries one. The first import key present is
/// the one read, and only a string counts as a target.
fn import_target(play: &Node) -> Option<&str> {
    let key = IMPORT_KEYS
        .into_iter()
        .find(|key| play.get(key).is_some())?;
    play.get(key)?.as_str()
}

/// The group a pre-flight import covers: `preflight_group`, when it equals
/// `preflight_group_expected` and is not empty. A missing, null, empty or mismatched value covers
/// nothing.
fn preflight_group(play: &Node) -> Option<String> {
    let vars = play.get("vars");
    let group = scalar_text(vars?.get("preflight_group")?)?;
    let expected = scalar_text(vars?.get("preflight_group_expected")?)?;
    (!group.trim().is_empty() && group == expected).then_some(group)
}

/// A scalar as Python's `str()` reads it: a bool is `True` or `False`. A null, a list and a
/// mapping are not group names, so they read as missing.
fn scalar_text(node: &Node) -> Option<String> {
    match node {
        Node::Str(text) => Some(text.clone()),
        Node::Bool(true) => Some("True".to_owned()),
        Node::Bool(false) => Some("False".to_owned()),
        Node::Null | Node::Seq(_) | Node::Map(_) => None,
    }
}

/// The `hosts:` value: a list joined by commas, or the scalar text. A null or missing value is
/// `None`, so the play is skipped.
fn hosts_text(play: &Node) -> Option<String> {
    match play.get("hosts")? {
        Node::Seq(items) => Some(
            items
                .iter()
                .map(|item| scalar_text(item).unwrap_or_default())
                .collect::<Vec<_>>()
                .join(","),
        ),
        other => scalar_text(other),
    }
}

/// What a `hosts:` value needs covered: each group of a literal `a:b` union, or the whole
/// expression when it is templated.
fn target_parts(hosts: &str) -> Vec<String> {
    if hosts.contains("{{") {
        return vec![hosts.to_owned()];
    }
    hosts
        .split(':')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(str::to_owned)
        .collect()
}

fn play_name(play: &Node) -> String {
    let name = play
        .get("name")
        .and_then(scalar_text)
        .unwrap_or_else(|| "None".to_owned());
    format!("{name:?}")
}

/// Whether an inline guard names the subject: the literal group name, or for a templated target
/// the first identifier inside `{{ }}`. The name must stand alone, not inside a longer word.
fn covered_inline(subject: &str, guards: &[String]) -> bool {
    let name = if subject.contains("{{") {
        match BRACED_NAME.captures(subject) {
            Some(caps) => caps[1].to_owned(),
            None => return false,
        }
    } else {
        subject.to_owned()
    };
    if name.is_empty() {
        return false;
    }
    let alone = re(&format!(
        r"(?:^|[^A-Za-z0-9_]){}(?:[^A-Za-z0-9_]|$)",
        regex::escape(&name)
    ));
    guards.iter().any(|guard| alone.is_match(guard))
}

/// Each assert condition in a localhost play's tasks (nested blocks included) that a populated
/// group alone satisfies.
fn inline_guard_items(play: &Node) -> Vec<String> {
    let mut out = vec![];
    for task in tasks(play.get("tasks")) {
        for key in ASSERT_KEYS {
            let Some(body) = task.get(key).filter(|body| body.as_map().is_some()) else {
                continue;
            };
            let conditions: Vec<&Node> = match body.get("that") {
                Some(Node::Seq(items)) => items.iter().collect(),
                Some(single) => vec![single],
                None => vec![],
            };
            for text in conditions.into_iter().filter_map(Node::as_str) {
                if is_populated_group_check(text) {
                    out.push(text.to_owned());
                }
            }
        }
    }
    out
}

/// One condition that a populated group alone satisfies: it names `groups`, holds exactly one
/// comparison, that comparison is a positive `length` test, and it has no `and`/`or`/`not`/`if`/
/// `else` that could neutralise it.
fn is_populated_group_check(text: &str) -> bool {
    text.contains("groups")
        && COMPARATOR.find_iter(text).count() == 1
        && POSITIVE.is_match(text)
        && !NEUTRALISER.is_match(text)
}

/// Every mapping under `node` that holds a task or play, depth first (nested block, rescue and
/// always children after their parent). Nothing is inherited from the parent.
fn tasks(node: Option<&Node>) -> Vec<&Node> {
    fn walk<'a>(node: Option<&'a Node>, out: &mut Vec<&'a Node>) {
        let Some(Node::Seq(items)) = node else {
            return;
        };
        for item in items.iter().filter(|item| item.as_map().is_some()) {
            out.push(item);
            for key in TASK_LISTS {
                walk(item.get(key), out);
            }
        }
    }
    let mut out = vec![];
    walk(node, &mut out);
    out
}

/// Each disagreement between the checker and the ratchet; empty when they agree. A new offender
/// must be listed. A listed name must still be an offender, and must still exist.
fn ratchet_disagreements(report: &Report, ratchet: &str, playbooks: &Path) -> Vec<String> {
    let mut bad = vec![];
    for (name, reason) in &report.offenders {
        if !ratchet.lines().any(|line| line == name.as_str()) {
            bad.push(format!(
                "NEW deploy playbook without a pre-flight: {name} ({reason})"
            ));
        }
    }
    for listed in ratchet.lines() {
        if listed.is_empty() || listed.starts_with('#') {
            continue;
        }
        if !playbooks.join(listed).is_file() {
            bad.push(format!(
                "ratchet names a playbook that no longer exists: {listed}"
            ));
            continue;
        }
        if !report.offenders.iter().any(|(name, _)| name == listed) {
            bad.push(format!(
                "{listed} now runs the pre-flight \u{2014} remove it from the ratchet"
            ));
        }
    }
    bad
}

fn playbooks() -> PathBuf {
    repo::path(PLAYBOOKS)
}

fn offender_names(report: &Report) -> Vec<&str> {
    report
        .offenders
        .iter()
        .map(|(name, _)| name.as_str())
        .collect()
}

/// A temporary directory holding the named files.
fn fixture(files: &[(&str, &str)]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().expect("a temporary directory");
    for (name, text) in files {
        fs::write(dir.path().join(name), text).expect("a fixture file");
    }
    dir
}

/// A deploy playbook whose only guard is one inline assert on `condition`, then a play that
/// targets the templated group.
fn inline_playbook(condition: &str) -> String {
    format!(
        r#"- name: "Inline guard"
  hosts: localhost
  tasks:
    - name: "Require a populated group"
      ansible.builtin.assert:
        that: "{condition}"
- name: "Work"
  hosts: "{{{{ target_service }}}}"
  tasks: []
"#
    )
}

/// One fixture file per `(name, condition)`, named `deploy-<name>.yml`.
fn inline_fixture(cases: &[(&str, &str)]) -> tempfile::TempDir {
    let files: Vec<(String, String)> = cases
        .iter()
        .map(|(name, condition)| (format!("deploy-{name}.yml"), inline_playbook(condition)))
        .collect();
    let refs: Vec<(&str, &str)> = files
        .iter()
        .map(|(name, text)| (name.as_str(), text.as_str()))
        .collect();
    fixture(&refs)
}

/// Inline asserts that are not exactly one accepted positive comparison. Each leaves its target
/// unguarded, so each file is an offender. A fail-safe checker refuses what it does not recognise.
const NOT_ACCEPTED: [(&str, &str); 10] = [
    ("ge0", "(groups.get(target_service, []) | length) >= 0"),
    ("gtneg", "(groups.get(target_service, []) | length) > -1"),
    (
        "or",
        "(groups.get(target_service, []) | length) > 0 or true",
    ),
    (
        "and",
        "(groups.get(target_service, []) | length) >= 0 and ([1] | length) > 0",
    ),
    (
        "twocmp",
        "((groups.get(target_service, []) | length) >= 0) == (([1] | length) > 0)",
    ),
    ("not", "not (groups.get(target_service, []) | length) > 0"),
    ("reversed", "0 < (groups.get(target_service, []) | length)"),
    ("nolen", "groups.get(target_service) is defined"),
    ("othergroup", "(groups.get(other_var, []) | length) > 0"),
    (
        "fraction",
        "(groups.get(target_service, []) | length) > 0.5",
    ),
];

/// Inline asserts with one accepted positive comparison of `length`. Each guards its target.
const ACCEPTED: [(&str, &str); 4] = [
    ("gt0", "(groups.get(target_service, []) | length) > 0"),
    ("ge1", "groups[target_service] | length >= 1"),
    ("eq1", "groups[target_service] | length == 1"),
    ("ne0", "(groups.get(target_service, []) | length) != 0"),
];

#[test]
fn every_deploy_playbook_targeting_a_group_is_guarded_or_on_the_ratchet() {
    // Arrange
    let dir = playbooks();
    let ratchet = fs::read_to_string(repo::path(RATCHET)).expect("the ratchet file");

    // Act
    let report = offenders(&dir);
    let disagreements = ratchet_disagreements(&report, &ratchet, &dir);

    // Assert
    assert!(disagreements.is_empty(), "{}", disagreements.join("\n"));
    // Not vacuous: a glob that matched nothing would make the ratchet trivially agree.
    assert!(
        report.checked >= 20,
        "only {} deploy playbooks target a group; the glob is wrong",
        report.checked
    );
}

#[test]
fn the_known_compliant_deploys_are_not_offenders() {
    // Arrange: the three playbooks that carry the form-1 import. They must exist, or the refusal
    // below would pass for the wrong reason.
    let known = [
        "deploy-postiz.yml",
        "deploy-agentgateway.yml",
        "deploy-o11y.yml",
    ];
    for name in known {
        assert!(
            playbooks().join(name).is_file(),
            "{name} is gone; update this test"
        );
    }

    // Act
    let report = offenders(&playbooks());

    // Assert
    let named = offender_names(&report);
    for name in known {
        assert!(
            !named.contains(&name),
            "{name} is named as an offender: the checker stopped recognising its pre-flight import"
        );
    }
}

#[test]
fn a_deploy_with_no_preflight_is_an_offender() {
    // Arrange
    let dir = fixture(&[(
        "deploy-bare.yml",
        r#"- name: "Phase 1"
  hosts: bare_svc
  tasks: []
"#,
    )]);

    // Act
    let report = offenders(dir.path());

    // Assert
    assert!(
        offender_names(&report).contains(&"deploy-bare.yml"),
        "{:?}",
        report.offenders
    );
}

#[test]
fn a_preflight_imported_after_the_first_group_play_does_not_count() {
    // Arrange: the group play comes first, so the later pre-flight covers nothing yet.
    let dir = fixture(&[(
        "deploy-late.yml",
        r#"- name: "Phase 1"
  hosts: late_svc
  tasks: []
- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: late_svc
    preflight_group_expected: late_svc
"#,
    )]);

    // Act
    let report = offenders(dir.path());

    // Assert
    assert!(
        offender_names(&report).contains(&"deploy-late.yml"),
        "{:?}",
        report.offenders
    );
}

#[test]
fn a_preflight_for_another_group_or_with_one_var_does_not_count() {
    // Arrange: one covers a different group; the other has no `_expected` var.
    let dir = fixture(&[
        (
            "deploy-other.yml",
            r#"- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: other_svc
    preflight_group_expected: other_svc
- name: "Phase 1"
  hosts: real_svc
  tasks: []
"#,
        ),
        (
            "deploy-single.yml",
            r#"- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: single_svc
- name: "Phase 1"
  hosts: single_svc
  tasks: []
"#,
        ),
    ]);

    // Act
    let report = offenders(dir.path());

    // Assert
    let named = offender_names(&report);
    assert!(
        named.contains(&"deploy-other.yml"),
        "{:?}",
        report.offenders
    );
    assert!(
        named.contains(&"deploy-single.yml"),
        "{:?}",
        report.offenders
    );
}

#[test]
fn a_union_target_needs_every_group_covered() {
    // Arrange: the pre-flight covers a_svc only; the play targets a_svc:b_svc.
    let dir = fixture(&[(
        "deploy-union.yml",
        r#"- name: "Pre-flight"
  import_playbook: preflight-target-group.yml
  vars:
    preflight_group: a_svc
    preflight_group_expected: a_svc
- name: "Phase 1"
  hosts: a_svc:b_svc
  tasks: []
"#,
    )]);

    // Act
    let report = offenders(dir.path());

    // Assert
    assert!(
        offender_names(&report).contains(&"deploy-union.yml"),
        "{:?}",
        report.offenders
    );
}

#[test]
fn both_accepted_forms_and_a_wrapper_over_a_guarded_playbook_are_compliant() {
    // Arrange: form 1; form 2 (inline); and a wrapper that imports a guarded playbook.
    let dir = fixture(&[
        (
            "deploy-good.yml",
            r#"- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: good_svc
    preflight_group_expected: good_svc
- name: "Local check"
  hosts: localhost
  tasks: []
- name: "Phase 1"
  hosts: good_svc
  tasks: []
"#,
        ),
        (
            "deploy-inline.yml",
            r#"- name: "Refuse a target group that matches no hosts"
  hosts: localhost
  tasks:
    - name: "Require target_service to name a populated group"
      ansible.builtin.assert:
        that: (groups.get(target_service | default(''), []) | length) > 0
- name: "Work"
  hosts: "{{ target_service }}"
  tasks: []
"#,
        ),
        (
            "guarded.yml",
            r#"- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: wrapped_svc
    preflight_group_expected: wrapped_svc
- name: "Phase 1"
  hosts: wrapped_svc
  tasks: []
"#,
        ),
        (
            "deploy-wrapper.yml",
            r#"- name: "Deploy wrapped"
  import_playbook: guarded.yml
"#,
        ),
    ]);

    // Act
    let report = offenders(dir.path());

    // Assert
    assert!(report.offenders.is_empty(), "{:?}", report.offenders);
    assert_eq!(report.checked, 3);
}

#[test]
fn the_ratchet_fails_a_new_offender_and_a_listed_playbook_that_complies() {
    // Arrange: one offender and one compliant playbook.
    let dir = fixture(&[
        (
            "deploy-bare.yml",
            r#"- name: "Phase 1"
  hosts: bare_svc
  tasks: []
"#,
        ),
        (
            "deploy-good.yml",
            r#"- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: good_svc
    preflight_group_expected: good_svc
- name: "Phase 1"
  hosts: good_svc
  tasks: []
"#,
        ),
    ]);
    let report = offenders(dir.path());

    // Act: an empty ratchet, a ratchet that lists the compliant one, and an exact ratchet.
    let unlisted = ratchet_disagreements(&report, "", dir.path());
    let listed_compliant =
        ratchet_disagreements(&report, "deploy-bare.yml\ndeploy-good.yml\n", dir.path());
    let exact = ratchet_disagreements(&report, "deploy-bare.yml\n", dir.path());

    // Assert
    assert!(
        unlisted
            .iter()
            .any(|m| m.starts_with("NEW deploy playbook without a pre-flight: deploy-bare.yml")),
        "{unlisted:?}"
    );
    assert!(
        listed_compliant
            .iter()
            .any(|m| m.starts_with("deploy-good.yml now runs the pre-flight")),
        "{listed_compliant:?}"
    );
    assert!(exact.is_empty(), "{exact:?}");
}

#[test]
fn the_repositorys_own_inline_guards_are_recognised() {
    // Arrange: the real files, under deploy- names, so the form the repository uses is read.
    let check_secrets =
        fs::read_to_string(playbooks().join("check-secrets.yml")).expect("check-secrets.yml");
    let verify = fs::read_to_string(playbooks().join("verify-service-health.yml"))
        .expect("verify-service-health.yml");
    let dir = fixture(&[
        ("deploy-check-secrets.yml", check_secrets.as_str()),
        ("deploy-verify-service-health.yml", verify.as_str()),
    ]);

    // Act
    let report = offenders(dir.path());

    // Assert
    assert!(report.offenders.is_empty(), "{:?}", report.offenders);
    assert_eq!(report.checked, 2);
}

#[test]
fn inline_asserts_that_are_not_one_accepted_positive_comparison_do_not_count() {
    // Arrange: each condition is refused, and each row is named so a miss names its row.
    let dir = inline_fixture(&NOT_ACCEPTED);

    // Act
    let report = offenders(dir.path());

    // Assert
    let named = offender_names(&report);
    let counted: Vec<&str> = NOT_ACCEPTED
        .iter()
        .map(|(name, _)| *name)
        .filter(|name| {
            let file = format!("deploy-{name}.yml");
            !named.contains(&file.as_str())
        })
        .collect();
    assert!(
        counted.is_empty(),
        "these inline asserts counted as a pre-flight: {counted:?}"
    );
}

#[test]
fn each_accepted_positive_comparison_counts() {
    // Arrange
    let dir = inline_fixture(&ACCEPTED);

    // Act
    let report = offenders(dir.path());

    // Assert
    assert!(report.offenders.is_empty(), "{:?}", report.offenders);
    assert_eq!(report.checked, ACCEPTED.len());
}
