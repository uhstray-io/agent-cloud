//! Lane selection (plan 24, D3): `lanes::select` over the rules of `areas.yml`.
//!
//! The cases run over inline configurations, so an edit to the repository's `areas.yml` cannot
//! change what they prove. Three tests read the repository itself: the invariant that every
//! suite module is listed once, the refusal of a malformed file, and the `testsel` binary.

use std::collections::BTreeSet;

use agent_cloud_tests::lanes::{Config, Lane, Selection, select};
use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use proptest::prelude::*;
use tempfile::TempDir;

/// Three areas: a static module, a cli module, and one area with a module in each tier.
/// `always` is empty; [`secured`] adds the security set.
const BASE: &str = r#"
full: ["platform/tests/**", ".github/workflows/**"]
ignore: ["**/*.md", "docs/**"]
always: {}
areas:
  restart_policy:
    paths: ["platform/**/*compose*.yml"]
    tiers: {static: [restart_policy]}
  common_sh:
    paths: ["platform/lib/common.sh"]
    tiers: {cli: [common_sh]}
  multi:
    paths: ["platform/services/multi/**"]
    tiers: {static: [multi_static], unit: [multi_unit], playbook: [multi_playbook]}
"#;

fn config(text: &str) -> Config {
    Config::parse(text, "inline").expect("the inline configuration parses")
}

fn secured() -> Config {
    config(&BASE.replace(
        "always: {}",
        "always: {static: [security_static], unit: [security_unit]}",
    ))
}

/// `paths` is a whitespace-separated list of repository paths.
fn run(config: &Config, lane: Lane, paths: &str) -> Selection {
    let changed: Vec<String> = paths.split_whitespace().map(str::to_owned).collect();
    select(config, lane, &changed)
}

fn mods(names: &[&str]) -> Selection {
    Selection::Modules(names.iter().map(|name| (*name).to_owned()).collect())
}

#[test]
fn each_change_selects_what_its_lane_and_paths_earn() {
    let base = config(BASE);
    let cases: Vec<(&str, Lane, &str, Selection)> = vec![
        (
            "a docs-only push selects nothing",
            Lane::Push,
            "docs/guide.md README.md",
            mods(&[]),
        ),
        (
            "a compose change in commit selects its area's static tier",
            Lane::Commit,
            "platform/services/x/compose.yml",
            mods(&["restart_policy"]),
        ),
        (
            "a shared shell library in commit selects nothing: its only tier is cli",
            Lane::Commit,
            "platform/lib/common.sh",
            mods(&[]),
        ),
        (
            "the same library in push selects its cli module",
            Lane::Push,
            "platform/lib/common.sh",
            mods(&["common_sh"]),
        ),
        (
            "an unclassified path runs everything in push",
            Lane::Push,
            "src/unknown.rs",
            Selection::All,
        ),
        (
            "an unclassified path adds nothing in commit",
            Lane::Commit,
            "src/unknown.rs",
            mods(&[]),
        ),
        (
            "an unclassified path beside a classified one still runs everything in push",
            Lane::Push,
            "platform/lib/common.sh src/unknown.rs",
            Selection::All,
        ),
        (
            "a full hit runs everything in ci",
            Lane::Ci,
            "platform/tests/Cargo.toml",
            Selection::All,
        ),
        (
            "a full hit runs the static tier of every area in commit",
            Lane::Commit,
            "platform/tests/Cargo.toml",
            mods(&["multi_static", "restart_policy"]),
        ),
        (
            "a multi-tier area runs only its static tier in commit",
            Lane::Commit,
            "platform/services/multi/a.py",
            mods(&["multi_static"]),
        ),
        (
            "the same area runs every tier in push",
            Lane::Push,
            "platform/services/multi/a.py",
            mods(&["multi_playbook", "multi_static", "multi_unit"]),
        ),
        (
            "two areas select their union in push",
            Lane::Push,
            "platform/services/x/compose.yml platform/services/multi/a.py",
            mods(&[
                "multi_playbook",
                "multi_static",
                "multi_unit",
                "restart_policy",
            ]),
        ),
        (
            "an empty change selects nothing in push",
            Lane::Push,
            "",
            mods(&[]),
        ),
        (
            "the full lane runs everything, whatever changed",
            Lane::Full,
            "docs/guide.md",
            Selection::All,
        ),
    ];
    for (label, lane, paths, expected) in cases {
        assert_eq!(
            run(&base, lane, paths),
            expected,
            "{label}: {lane:?} over {paths:?}"
        );
    }
}

#[test]
fn the_security_set_joins_every_lane_its_tier_allows() {
    let secured = secured();

    assert_eq!(
        run(&secured, Lane::Push, "docs/guide.md"),
        mods(&["security_static", "security_unit"]),
        "push adds every tier of always"
    );
    assert_eq!(
        run(&secured, Lane::Commit, "docs/guide.md"),
        mods(&["security_static"]),
        "commit adds only the static tier of always"
    );
    assert_eq!(
        run(&secured, Lane::Commit, "platform/tests/Cargo.toml"),
        mods(&["multi_static", "restart_policy", "security_static"]),
        "a full hit in commit keeps the security set"
    );
    assert_eq!(
        run(&secured, Lane::Ci, "src/unknown.rs"),
        Selection::All,
        "an unclassified path in ci runs everything, the security set included"
    );
}

#[test]
fn a_star_does_not_cross_a_directory_separator() {
    let shell = config(
        r#"
full: []
ignore: []
always: {}
areas:
  shell:
    paths: ["platform/lib/*.sh"]
    tiers: {cli: [common_sh]}
"#,
    );

    assert_eq!(
        run(&shell, Lane::Push, "platform/lib/common.sh"),
        mods(&["common_sh"])
    );
    assert_eq!(
        run(&shell, Lane::Push, "platform/lib/nested/common.sh"),
        Selection::All,
        "a nested path matches no area, so it is unclassified"
    );
}

#[test]
fn a_malformed_configuration_is_refused_rather_than_read_as_empty() {
    let refused = [
        ("a key is missing", "full: []\nignore: []\nalways: {}\n"),
        (
            "a key is misspelt",
            "full: []\nignore: []\nalways: {}\nareas: {}\nfulll: []\n",
        ),
        (
            "a tier is unknown",
            "full: []\nignore: []\nalways: {bogus: [x]}\nareas: {}\n",
        ),
        (
            "a glob does not compile",
            "full: [\"platform/[\"]\nignore: []\nalways: {}\nareas: {}\n",
        ),
        (
            "a module name is not an identifier",
            "full: []\nignore: []\nalways: {static: [\"bad name\"]}\nareas: {}\n",
        ),
    ];
    for (why, text) in refused {
        assert!(Config::parse(text, "inline").is_err(), "{why}");
    }
}

#[test]
fn a_selection_renders_as_all_none_or_a_sorted_alternation() {
    assert_eq!(Selection::All.filterset(), "all()");
    assert_eq!(mods(&[]).filterset(), "none()");
    assert_eq!(
        mods(&["restart_policy", "common_sh"]).filterset(),
        "test(/^(?:common_sh|restart_policy)::/)"
    );
}

#[test]
fn every_suite_module_is_listed_exactly_once_in_areas_yml() {
    let config = Config::load(&repo::path("platform/tests/areas.yml")).expect("areas.yml loads");
    let main_rs = std::fs::read_to_string(repo::path("platform/tests/suite/main.rs"))
        .expect("suite/main.rs reads");
    let declared: Vec<&str> = main_rs
        .lines()
        .filter_map(|line| line.strip_prefix("mod ")?.strip_suffix(';'))
        .collect();
    assert!(!declared.is_empty(), "suite/main.rs declares no module");

    let listed = config.listed_modules();
    let mut problems = Vec::new();
    for module in &declared {
        let count = listed
            .iter()
            .filter(|name| name.as_str() == *module)
            .count();
        if count != 1 {
            problems.push(format!(
                "`mod {module};` is listed {count} times in areas.yml, expected once"
            ));
        }
    }
    let declared: BTreeSet<&str> = declared.into_iter().collect();
    for name in listed.iter().collect::<BTreeSet<_>>() {
        if !declared.contains(name.as_str()) {
            problems.push(format!(
                "areas.yml lists `{name}`, which suite/main.rs does not declare"
            ));
        }
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

/// Every path the property draws from: each area's, each one none matches, and a full hit.
const ALPHABET: [&str; 7] = [
    "platform/services/x/compose.yml",
    "platform/services/multi/a.py",
    "platform/lib/common.sh",
    "platform/lib/nested/common.sh",
    "platform/tests/Cargo.toml",
    "docs/guide.md",
    "src/unknown.rs",
];

fn covers(outer: &Selection, inner: &Selection) -> bool {
    match (outer, inner) {
        (Selection::All, _) => true,
        (Selection::Modules(_), Selection::All) => false,
        (Selection::Modules(outer), Selection::Modules(inner)) => inner.is_subset(outer),
    }
}

proptest! {
    /// For any change, the push lane runs at least what the commit lane runs, and the commit
    /// lane never runs everything.
    #[test]
    fn the_push_lane_runs_at_least_what_the_commit_lane_runs(
        paths in prop::collection::vec(prop::sample::select(ALPHABET.to_vec()), 0..6),
    ) {
        let config = secured();
        let changed: Vec<String> = paths.iter().map(|path| (*path).to_owned()).collect();
        let commit = select(&config, Lane::Commit, &changed);
        let push = select(&config, Lane::Push, &changed);
        prop_assert!(commit != Selection::All, "commit ran everything for {changed:?}");
        prop_assert!(
            covers(&push, &commit),
            "push {push:?} misses commit {commit:?} for {changed:?}"
        );
    }
}

#[test]
fn testsel_prints_the_filterset_and_refuses_an_unknown_lane() {
    let home = TempDir::new().expect("a temporary home");
    let testsel = assert_cmd::cargo::cargo_bin("testsel")
        .display()
        .to_string();

    process::scrubbed(&testsel, home.path())
        .arg("full")
        .assert()
        .success()
        .stdout("all()\n");
    process::scrubbed(&testsel, home.path())
        .arg("bogus")
        .assert()
        .code(2);
}
