//! Lane selection (plan 24, D3): `lanes::select` over the rules of `areas.yml`.
//!
//! The cases run over inline configurations, so an edit to the repository's `areas.yml` cannot
//! change what they prove. The tests that read the repository itself are the invariant that
//! every suite module is listed once, the pre-commit hook that runs the commit lane, and the
//! `testsel` and `lane.sh` runs.

use std::collections::BTreeSet;

use agent_cloud_tests::lanes::{Config, Lane, Selection, select};
use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
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
    // `mod x;`, `pub mod x;` and `pub(crate) mod x;`, indented or not: any form declares a module.
    let declaration = regex::Regex::new(r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;")
        .expect("valid pattern");
    let declared: Vec<&str> = declaration
        .captures_iter(&main_rs)
        .map(|c| c.get(1).expect("a module name").as_str())
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

/// A git repository holding a small `areas.yml`, with `origin/dev` and `origin/main` at its
/// first commit, so `testsel` reads its changes the way it reads a real checkout's.
struct Checkout {
    dir: TempDir,
    home: TempDir,
}

impl Checkout {
    fn new() -> Checkout {
        let checkout = Checkout {
            dir: TempDir::new().expect("a temporary checkout"),
            home: TempDir::new().expect("a temporary home"),
        };
        checkout.write(
            "platform/tests/areas.yml",
            "full: [\"platform/tests/**\"]\nignore: [\"docs/**\"]\nalways: {static: [ledger]}\n\
             areas:\n  restart_policy:\n    paths: [\"platform/**/compose.yml\"]\n    \
             tiers: {static: [restart_policy], playbook: [linger]}\n",
        );
        checkout.write("platform/svc/compose.yml", "services: {}\n");
        checkout.write("docs/guide.md", "guide\n");
        checkout.git(&["init", "-q"]);
        checkout.commit("base");
        checkout.git(&["update-ref", "refs/remotes/origin/dev", "HEAD"]);
        checkout.git(&["update-ref", "refs/remotes/origin/main", "HEAD"]);
        checkout
    }

    fn write(&self, rel: &str, text: &str) {
        let path = self.dir.path().join(rel);
        std::fs::create_dir_all(path.parent().expect("a parent")).expect("parent directory");
        std::fs::write(path, text).expect("fixture file");
    }

    fn git(&self, args: &[&str]) {
        process::scrubbed("git", self.home.path())
            .current_dir(self.dir.path())
            .args(args)
            .assert()
            .success();
    }

    fn commit(&self, message: &str) {
        self.git(&["add", "-A"]);
        self.git(&[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ]);
    }

    /// What `testsel <lane>` prints here, with `BASE_REF` set when `base` is given.
    fn testsel(&self, lane: &str, base: Option<&str>) -> String {
        let testsel = assert_cmd::cargo::cargo_bin("testsel")
            .display()
            .to_string();
        let mut cmd = process::scrubbed(&testsel, self.home.path());
        cmd.current_dir(self.dir.path()).arg(lane);
        if let Some(base) = base {
            cmd.env("BASE_REF", base);
        }
        let out = cmd.assert().success().get_output().stdout.clone();
        String::from_utf8(out).expect("utf-8 filterset")
    }
}

#[test]
fn testsel_reads_the_change_from_git_and_fails_open() {
    // Arrange: a compose file moved into an ignored directory, committed
    let checkout = Checkout::new();
    checkout.git(&["mv", "platform/svc/compose.yml", "docs/moved.md"]);
    checkout.commit("move");

    // Act / Assert: the path that went away still selects its area
    assert_eq!(
        checkout.testsel("push", None),
        "test(/^(?:ledger|linger|restart_policy)::/)\n",
        "a rename is read as a delete and an add"
    );
    // Act / Assert: a base git cannot resolve runs everything, never nothing
    assert_eq!(checkout.testsel("push", Some("nosuch")), "all()\n");
    // Act / Assert: a promotion into main runs everything, whatever it changes
    assert_eq!(checkout.testsel("ci", Some("main")), "all()\n");
    assert_eq!(
        checkout.testsel("ci", Some("dev")),
        "test(/^(?:ledger|linger|restart_policy)::/)\n"
    );

    // Arrange: a staged compose file
    checkout.write("platform/other/compose.yml", "services: {}\n");
    checkout.git(&["add", "platform/other/compose.yml"]);

    // Act / Assert: commit reads the index, and keeps the static tier only
    assert_eq!(
        checkout.testsel("commit", None),
        "test(/^(?:ledger|restart_policy)::/)\n"
    );
}

#[test]
fn lane_sh_skips_a_workstation_lane_without_nextest_and_refuses_a_ci_lane() {
    // Arrange: a PATH with no cargo on it
    let home = TempDir::new().expect("a temporary home");
    let lane_sh = repo::path("platform/tests/lane.sh").display().to_string();
    let without_cargo = |lane: &str| {
        let mut cmd = process::scrubbed("bash", home.path());
        cmd.env("PATH", "/usr/bin:/bin").arg(&lane_sh).arg(lane);
        cmd
    };

    // Act / Assert: a hook's lane says why it did not run, and lets the commit or push go on
    for lane in ["commit", "push"] {
        without_cargo(lane)
            .assert()
            .success()
            .stderr(predicates::str::contains("cargo-nextest is not installed"));
    }
    // Act / Assert: CI and the full lane never pass without running the suite
    for lane in ["ci", "full"] {
        without_cargo(lane)
            .assert()
            .code(1)
            .stderr(predicates::str::contains("needs cargo and cargo-nextest"));
    }
}

#[test]
fn lane_sh_gives_testsel_the_hooks_index_and_runs_the_tests_without_git_variables() {
    // Arrange: a fake cargo that answers the version probe, prints a filterset for the testsel
    // run while recording GIT_INDEX_FILE, and records every GIT_ variable nextest would inherit
    let dir = TempDir::new().expect("a temporary directory");
    let log = dir.path().join("cargo.log");
    let cargo = dir.path().join("cargo");
    std::fs::write(
        &cargo,
        "#!/bin/sh\n\
         case \"$1 $2\" in\n\
         \"nextest --version\") exit 0 ;;\n\
         \"run --locked\") echo \"testsel GIT_INDEX_FILE=${GIT_INDEX_FILE-unset}\" >> \"$FAKE_LOG\"; echo 'none()' ;;\n\
         \"nextest run\") env | grep '^GIT_' | sed 's/^/nextest /' >> \"$FAKE_LOG\"; echo 'nextest ran' >> \"$FAKE_LOG\" ;;\n\
         *) exit 3 ;;\n\
         esac\n",
    )
    .expect("fake cargo");
    std::fs::set_permissions(&cargo, std::os::unix::fs::PermissionsExt::from_mode(0o755))
        .expect("executable fake cargo");

    // Act: the commit lane, with the variables a git hook exports
    process::scrubbed("bash", dir.path())
        .env("PATH", format!("{}:/usr/bin:/bin", dir.path().display()))
        .env("FAKE_LOG", &log)
        .env("GIT_DIR", "/nonexistent/.git")
        .env("GIT_WORK_TREE", "/nonexistent")
        .env("GIT_INDEX_FILE", "/nonexistent/.git/index.lock")
        .arg(repo::path("platform/tests/lane.sh"))
        .arg("commit")
        .assert()
        .success();

    // Assert: testsel read the hook's index; nextest started with no git variable at all
    assert_eq!(
        std::fs::read_to_string(&log).expect("the fake cargo ran"),
        "testsel GIT_INDEX_FILE=/nonexistent/.git/index.lock\nnextest ran\n"
    );
}

#[test]
fn the_pre_commit_configuration_runs_the_commit_lane() {
    let config = yaml::load_one(&repo::path(".pre-commit-config.yaml"))
        .expect(".pre-commit-config.yaml has one document");
    let hook = config
        .get("repos")
        .and_then(|repos| repos.as_seq())
        .into_iter()
        .flatten()
        .filter_map(|repo| repo.get("hooks")?.as_seq())
        .flatten()
        .find(|hook| hook.get("id").and_then(|id| id.as_str()) == Some("rust-commit-lane"))
        .expect("a rust-commit-lane hook");

    let field = |key: &str| hook.get(key).cloned();
    assert_eq!(
        field("entry").as_ref().and_then(|n| n.as_str()),
        Some("platform/tests/lane.sh commit")
    );
    // always_run: a staged change of any path, a deletion included, still runs the lane
    assert_eq!(field("always_run"), Some(Node::Bool(true)));
    assert_eq!(field("pass_filenames"), Some(Node::Bool(false)));
    // A stage other than pre-commit would drop the lane from .githooks/pre-commit's run
    assert_eq!(
        field("stages"),
        Some(Node::Seq(vec![Node::Str("pre-commit".to_owned())]))
    );
}
