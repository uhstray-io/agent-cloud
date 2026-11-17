//! Lane selection (plan 24, D3): `lanes::select` over the rules of `areas.yml`.
//!
//! The cases run over inline configurations, so an edit to the repository's `areas.yml` cannot
//! change what they prove. The tests that read the repository itself are the invariant that
//! every suite module is listed once, the guard that every repo-wide scan outside the security
//! set runs when a file it reads changes, the pre-commit hook that runs the commit lane, and the
//! `testsel` and `lane.sh` runs. This module is in the security set (`areas.yml`), so the guard
//! runs in every lane.

use std::collections::BTreeSet;

use agent_cloud_tests::lanes::{Config, Lane, Selection, select};
use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::text;
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

/// The repository's own configuration: areas.yml and areas.d/.
fn repo_config() -> Config {
    Config::load(&repo::path("platform/tests/areas.yml")).expect("areas.yml and areas.d/ load")
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
    let config = repo_config();
    // build.rs declares one module per suite/<name>.rs and records the names it compiled.
    let declared = crate::SUITE_MODULES;

    let listed = config.listed_modules();
    let mut problems = Vec::new();
    for module in declared {
        let count = listed
            .iter()
            .filter(|name| name.as_str() == *module)
            .count();
        if count != 1 {
            problems.push(format!(
                "suite/{module}.rs is listed {count} times in areas.yml and areas.d/, expected once"
            ));
        }
    }
    let declared: BTreeSet<&str> = declared.iter().copied().collect();
    for name in listed.iter().collect::<BTreeSet<_>>() {
        if !declared.contains(name.as_str()) {
            problems.push(format!(
                "areas.yml or areas.d/ lists `{name}`, which has no suite/{name}.rs"
            ));
        }
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

/// `areas.yml` holding [`BASE`] in a fresh directory, with each `(name, text)` written under
/// `areas.d/` (none, and the directory is not created).
fn layout(files: &[(&str, &str)]) -> TempDir {
    let dir = TempDir::new().expect("a temporary directory");
    std::fs::write(dir.path().join("areas.yml"), BASE).expect("areas.yml writes");
    if !files.is_empty() {
        let areas_d = dir.path().join("areas.d");
        std::fs::create_dir(&areas_d).expect("areas.d creates");
        for (name, text) in files {
            std::fs::write(areas_d.join(name), text).expect("an area file writes");
        }
    }
    dir
}

fn load(dir: &TempDir) -> Result<Config, String> {
    Config::load(&dir.path().join("areas.yml"))
}

#[test]
fn an_area_file_adds_its_area_and_its_always_modules() {
    let dir = layout(&[
        (
            "ported.yml",
            "paths: [\"platform/ported/**\"]\ntiers: {unit: [ported]}\nalways: {static: [ported_guard]}\n",
        ),
        ("README.md", "not an area: ignored\n"),
    ]);

    let config = load(&dir).expect("the layout loads");

    assert_eq!(
        run(&config, Lane::Push, "platform/ported/x.sh"),
        mods(&["ported", "ported_guard"])
    );
    assert_eq!(
        run(&config, Lane::Commit, "docs/guide.md"),
        mods(&["ported_guard"]),
        "a file's `always` joins the security set"
    );
}

#[test]
fn a_missing_areas_d_adds_nothing() {
    let dir = layout(&[]);

    let loaded = load(&dir).expect("areas.yml alone loads");

    assert_eq!(
        loaded.listed_modules().len(),
        config(BASE).listed_modules().len()
    );
}

/// A refusal case: why, the area files (name, text), and a phrase the error must hold.
type Refusal<'a> = (&'a str, Vec<(&'a str, String)>, &'a str);

#[test]
fn an_area_file_that_collides_or_is_malformed_is_refused() {
    let area = |module: &str| format!("paths: []\ntiers: {{static: [{module}]}}\n");
    // Each row names its phrase, so it fails for its own reason, not a neighbour's.
    let cases: Vec<Refusal> = vec![
        (
            "an area of areas.yml defined again",
            vec![("multi.yml", area("other"))],
            "is already defined",
        ),
        (
            "a module of areas.yml listed again",
            vec![("other.yml", area("restart_policy"))],
            "is listed twice",
        ),
        (
            "a module listed in two files",
            vec![("one.yml", area("shared")), ("two.yml", area("shared"))],
            "is listed twice",
        ),
        (
            "a module in a file's area and its always",
            vec![(
                "one.yml",
                "paths: []\ntiers: {static: [x]}\nalways: {unit: [x]}\n".to_owned(),
            )],
            "is listed twice",
        ),
        (
            "an unknown key",
            vec![("one.yml", "paths: []\ntiers: {}\npath: []\n".to_owned())],
            "unknown key",
        ),
        (
            "a missing key",
            vec![("one.yml", "paths: []\n".to_owned())],
            "is missing `tiers`",
        ),
        (
            "a stem that is not a module name",
            vec![("bad-name.yml", area("x"))],
            "is not a module name",
        ),
    ];
    for (why, files, phrase) in cases {
        let files: Vec<(&str, &str)> = files.iter().map(|(n, t)| (*n, t.as_str())).collect();
        let dir = layout(&files);

        let result = load(&dir);

        match result {
            Ok(_) => panic!("{why}: loaded"),
            Err(error) => assert!(
                error.contains(phrase),
                "{why}: refused for another reason: {error}"
            ),
        }
    }
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
    // Arrange: a fake cargo first on PATH that records its arguments and fails every call, the
    // version probe included, so a cargo installed under /usr/bin or /bin on the host cannot answer
    // in its place, and a lane that never asks cargo is caught (#1152 review)
    let home = TempDir::new().expect("a temporary home");
    let bin = home.path().join("bin");
    std::fs::create_dir(&bin).expect("a bin directory");
    let calls = home.path().join("cargo-calls");
    let cargo = bin.join("cargo");
    std::fs::write(
        &cargo,
        format!("#!/bin/sh\necho \"$*\" >> '{}'\nexit 1\n", calls.display()),
    )
    .expect("write the fake cargo");
    std::fs::set_permissions(&cargo, std::os::unix::fs::PermissionsExt::from_mode(0o755))
        .expect("make the fake cargo executable");
    let path = format!("{}:/usr/bin:/bin", bin.display());
    let lane_sh = repo::path("platform/tests/lane.sh").display().to_string();
    let with_failing_cargo = |lane: &str| {
        let mut cmd = process::scrubbed("bash", home.path());
        cmd.env("PATH", &path).arg(&lane_sh).arg(lane);
        cmd
    };

    let probed = || {
        let text = std::fs::read_to_string(&calls).unwrap_or_default();
        let _ = std::fs::remove_file(&calls);
        text.lines().any(|line| line == "nextest --version")
    };

    // Act / Assert: a hook's lane says why it did not run, and lets the commit or push go on
    for lane in ["commit", "push"] {
        with_failing_cargo(lane)
            .assert()
            .success()
            .stderr(predicates::str::contains("cargo-nextest is not installed"));
        assert!(probed(), "the {lane} lane never asked cargo for nextest");
    }
    // Act / Assert: CI and the full lane never pass without running the suite
    for lane in ["ci", "full"] {
        with_failing_cargo(lane)
            .assert()
            .code(1)
            .stderr(predicates::str::contains("needs cargo and cargo-nextest"));
        assert!(probed(), "the {lane} lane never asked cargo for nextest");
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

    // Act: the commit lane, with every variable lane.sh clears set, as a git hook can set them
    let mut lane = process::scrubbed("bash", dir.path());
    lane.env("PATH", format!("{}:/usr/bin:/bin", dir.path().display()))
        .env("FAKE_LOG", &log)
        .env("GIT_INDEX_FILE", "/nonexistent/.git/index.lock");
    for name in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
        "GIT_NAMESPACE",
    ] {
        lane.env(name, "/nonexistent");
    }
    lane.arg(repo::path("platform/tests/lane.sh"))
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

/// The string literal starting at byte `i` of `src` (`"…"`, or a raw `r"…"` / `r#"…"#`), as
/// its text and the index after it; `None` when no literal starts there. A plain literal's `\\`
/// and `\"` are unescaped; a pathspec holds no other escape.
fn literal_at(src: &str, i: usize) -> Option<(String, usize)> {
    let rest = &src[i..];
    if let Some(body) = rest.strip_prefix('"') {
        let mut text = String::new();
        let mut chars = body.char_indices();
        while let Some((at, c)) = chars.next() {
            match c {
                '"' => return Some((text, i + 1 + at + 1)),
                '\\' => text.extend(chars.next().map(|(_, e)| e)),
                _ => text.push(c),
            }
        }
        panic!("an unterminated string literal at byte {i}");
    }
    let hashes = rest
        .strip_prefix('r')?
        .bytes()
        .take_while(|b| *b == b'#')
        .count();
    let open = 1 + hashes;
    if rest.as_bytes().get(open) != Some(&b'"') {
        return None;
    }
    let close = format!("\"{}", "#".repeat(hashes));
    let end = rest[open + 1..]
        .find(&close)
        .unwrap_or_else(|| panic!("an unterminated raw string at byte {i}"));
    Some((
        rest[open + 1..open + 1 + end].to_owned(),
        i + open + 1 + end + close.len(),
    ))
}

/// The index after the comment starting at byte `i`, or `None` when none starts there. Block
/// comments nest, as in Rust: an inner `*/` does not end the outer one.
fn comment_end(src: &str, i: usize) -> Option<usize> {
    let rest = &src[i..];
    if rest.starts_with("//") {
        return Some(rest.find('\n').map_or(src.len(), |n| i + n));
    }
    if !rest.starts_with("/*") {
        return None;
    }
    let (mut depth, mut j) = (0, 0);
    while j < rest.len() {
        if rest[j..].starts_with("/*") {
            depth += 1;
            j += 2;
        } else if rest[j..].starts_with("*/") {
            depth -= 1;
            j += 2;
            if depth == 0 {
                return Some(i + j);
            }
        } else {
            j += rest[j..].chars().next().map_or(1, char::len_utf8);
        }
    }
    panic!("an unclosed block comment at byte {i}");
}

/// The index after the char literal starting at byte `i` (`'"'`, `'\''`), or `None` when none
/// starts there; a lifetime (`'a`) is not one.
fn char_end(src: &str, i: usize) -> Option<usize> {
    let rest = src[i..].strip_prefix('\'')?;
    if rest.starts_with('\\') {
        return Some(i + 3 + rest[2..].find('\'')? + 1);
    }
    let c = rest.chars().next()?;
    rest[c.len_utf8()..]
        .starts_with('\'')
        .then(|| i + 1 + c.len_utf8() + 1)
}

/// The index after the comment, char literal or string literal starting at byte `i`, or
/// `None` when none starts there.
fn skip_at(src: &str, i: usize) -> Option<usize> {
    comment_end(src, i)
        .or_else(|| char_end(src, i))
        .or_else(|| literal_at(src, i).map(|(_, end)| end))
}

/// The top-level arguments of the call whose `(` is at byte `open`, as source slices.
fn call_args(src: &str, open: usize) -> Vec<&str> {
    let (mut depth, mut start, mut i) = (0, open + 1, open + 1);
    let mut args = vec![];
    while i < src.len() {
        if let Some(end) = skip_at(src, i) {
            i = end;
            continue;
        }
        match src.as_bytes()[i] {
            b'(' | b'[' | b'{' => depth += 1,
            b')' if depth == 0 => {
                args.push(&src[start..i]);
                return args;
            }
            b')' | b']' | b'}' => depth -= 1,
            b',' if depth == 0 => {
                args.push(&src[start..i]);
                start = i + 1;
            }
            _ => {}
        }
        i += src[i..].chars().next().map_or(1, char::len_utf8);
    }
    panic!("an unclosed call at byte {open}");
}

/// The byte where `const NAME:` or `static NAME:` starts in the code of `src` (comments and
/// literals skipped, so a doc comment quoting it does not count). A name defined twice, in two
/// scopes, cannot be told apart without parsing them, so it is an error, as is no definition.
fn definition(src: &str, name: &str) -> Result<usize, String> {
    let heads = [format!("const {name}:"), format!("static {name}:")];
    let mut found = None;
    let mut i = 0;
    while i < src.len() {
        if let Some(end) = skip_at(src, i) {
            i = end;
            continue;
        }
        if !src[..i].ends_with(is_ident)
            && heads.iter().any(|head| src[i..].starts_with(head.as_str()))
            && found.replace(i).is_some()
        {
            return Err(format!("`{name}` is defined twice in this file"));
        }
        i += src[i..].chars().next().map_or(1, char::len_utf8);
    }
    found.ok_or_else(|| format!("`{name}` is not a const or static of this file"))
}

/// The string literals of `expr`, which may hold only literals, constants defined in `src`,
/// `&`, `[`, `]`, `,`, whitespace and comments. Anything else (a variable, a call, a macro)
/// cannot be read without running the code, so it is an error.
fn literals_of(expr: &str, src: &str) -> Result<Vec<String>, String> {
    let mut out = vec![];
    let mut i = 0;
    while i < expr.len() {
        if let Some(end) = comment_end(expr, i) {
            i = end;
        } else if let Some((text, end)) = literal_at(expr, i) {
            out.push(text);
            i = end;
        } else if expr[i..].starts_with(|c: char| c.is_ascii_uppercase()) {
            let name: String = expr[i..]
                .chars()
                .take_while(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || *c == '_')
                .collect();
            i += name.len();
            let def = definition(src, &name)?;
            let eq = def + src[def..].find('=').ok_or("a constant without `=`")?;
            let mut end = eq + 1;
            while !src[end..].starts_with(';') {
                end = skip_at(src, end)
                    .unwrap_or_else(|| end + src[end..].chars().next().map_or(1, char::len_utf8));
            }
            out.extend(literals_of(&src[eq + 1..end], src)?);
        } else if expr[i..].starts_with(|c: char| c.is_whitespace() || "&[],".contains(c)) {
            i += expr[i..].chars().next().map_or(1, char::len_utf8);
        } else {
            return Err(format!("`{}` is not a literal or a constant", expr.trim()));
        }
    }
    Ok(out)
}

/// A character that continues an identifier.
fn is_ident(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// The first byte at or after `i` that is neither whitespace nor in a comment.
fn skip_trivia(src: &str, mut i: usize) -> usize {
    loop {
        if let Some(end) = comment_end(src, i) {
            i = end;
        } else if let Some(c) = src[i..].chars().next().filter(|c| c.is_whitespace()) {
            i += c.len_utf8();
        } else {
            return i;
        }
    }
}

/// When a call to `repo::grep` or `repo::ls_files` starts at byte `i`, its name, the index of
/// the argument holding its pathspecs, and the byte of its `(`. Rust allows whitespace and
/// comments between the path's tokens and before the `(`, and a raw name (`r#grep`).
fn scan_call_at(src: &str, i: usize) -> Option<(&'static str, usize, usize)> {
    let after = src[i..].strip_prefix("repo")?;
    if src[..i].ends_with(is_ident) || after.starts_with(is_ident) {
        return None;
    }
    let colons = skip_trivia(src, i + "repo".len());
    src[colons..].starts_with("::").then_some(())?;
    let mut name_at = skip_trivia(src, colons + 2);
    if src[name_at..].starts_with("r#") {
        name_at += 2;
    }
    let (name, arg) = [("grep", 1), ("ls_files", 0)]
        .into_iter()
        .find(|(name, _)| {
            src[name_at..]
                .strip_prefix(name)
                .is_some_and(|rest| !rest.starts_with(is_ident))
        })?;
    let open = skip_trivia(src, name_at + name.len());
    src[open..].starts_with('(').then_some((name, arg, open))
}

/// Every `repo::grep(pattern, pathspecs)` and `repo::ls_files(pathspecs)` call in the code of
/// `src` (comments and string literals skipped), as its line and its pathspecs.
fn repo_wide_scans(src: &str) -> Result<Vec<(usize, Vec<String>)>, String> {
    let mut scans = vec![];
    let mut i = 0;
    while i < src.len() {
        if let Some(end) = skip_at(src, i) {
            i = end;
            continue;
        }
        if let Some((name, arg, open)) = scan_call_at(src, i) {
            let line = src[..i].matches('\n').count() + 1;
            let args = call_args(src, open);
            let specs = args
                .get(arg)
                .ok_or_else(|| format!("line {line}: `repo::{name}` without its pathspecs"))?;
            scans.push((
                line,
                literals_of(specs, src).map_err(|e| format!("line {line}: {e}"))?,
            ));
            i = open;
        }
        i += src[i..].chars().next().map_or(1, char::len_utf8);
    }
    Ok(scans)
}

#[test]
fn repo_wide_scans_reads_literals_constants_and_skips_comments_and_strings() {
    // Arrange: a pattern holding a paren and a comma; a constant list with an exclusion, after a
    // constant whose name it prefixes and a doc comment that quotes it; char literals of a quote
    // and a paren; a scan named only in a comment, in a string and in a raw string holding an
    // unpaired quote; a non-ASCII name in code; a lookalike function; a nested block comment whose
    // inner `*/` does not end it; a comment between a callee and its `(`, and inside the path; and
    // a raw name.
    let src = r##"
/// `const SPECS: [&str; 1] = ["decoy"];` in a doc comment is not the definition.
const SPECS_2: [&str; 1] = ["wrong"];
const SPECS: [&str; 2] = [":(glob)platform/**/*.yml", ":!platform/x.yml"];
fn f() {
    let (q, p) = ('"', '(');
    let raw = r#"a " repo::grep("x", &["raw"])"#;
    let café = 1;
    // repo::grep("no", &["comment"])
    let s = "repo::ls_files(&[\"string\"])";
    my_repo::grep("no", &["lookalike"]);
    repo::grep(r"a(b, c", &["platform", "agents"]);
    repo::ls_files(&SPECS);
    /* outer /* inner */ repo::grep("no", &["nested"]); // */ repo::ls_files(&["after"]);
    repo::ls_files /* files */ (&["spaced"]);
    repo::grep_in(dir, "no", &["boundary"]);
    repo/* c */::grep("x", &["inpath"]);
    repo::r#ls_files(&["raw"]);
}
"##;

    // Act
    let scans = repo_wide_scans(src).expect("the scans read");

    // Assert
    assert_eq!(
        scans,
        [
            (12, vec!["platform".to_owned(), "agents".to_owned()]),
            (
                13,
                vec![
                    ":(glob)platform/**/*.yml".to_owned(),
                    ":!platform/x.yml".to_owned()
                ]
            ),
            (14, vec!["after".to_owned()]),
            (15, vec!["spaced".to_owned()]),
            (17, vec!["inpath".to_owned()]),
            (18, vec!["raw".to_owned()]),
        ]
    );
}

#[test]
fn repo_wide_scans_refuses_a_constant_defined_twice() {
    // Arrange: one name in two scopes.
    let src = "const S: [&str; 1] = [\"outer\"];\nfn f() { const S: [&str; 1] = [\"inner\"]; repo::ls_files(&S); }";

    // Act
    let refusal = repo_wide_scans(src).expect_err("a refusal");

    // Assert
    assert!(refusal.contains("`S` is defined twice"), "{refusal}");
}

#[test]
fn repo_wide_scans_refuses_a_pathspec_it_cannot_read() {
    // Arrange: a pathspec list held in a variable, a non-ASCII name in a call's arguments, and
    // one in a constant's value; each is refused by name, never a panic.
    let cases = [
        ("fn f(dirs: &[&str]) { repo::grep(\"x\", dirs); }", "`dirs`"),
        ("fn f() { repo::grep(\"x\", café); }", "`café`"),
        (
            "const S: [&str; 1] = [café];\nfn f() { repo::ls_files(&S); }",
            "`[café]`",
        ),
    ];

    for (src, name) in cases {
        // Act
        let refusal = repo_wide_scans(src).expect_err("a refusal");

        // Assert
        assert!(
            refusal.contains(&format!("{name} is not a literal")),
            "{refusal}"
        );
    }
}

/// The scans in `src` (module `module`, file `rel`) whose files do not all select the module in
/// the push lane under `config`, as one line each, and how many scans it read.
fn unselected_scans(config: &Config, module: &str, rel: &str, src: &str) -> (usize, Vec<String>) {
    let scans = repo_wide_scans(src).unwrap_or_else(|e| panic!("{rel}: {e}"));
    let mut problems = vec![];
    for (line, specs) in &scans {
        let specs: Vec<&str> = specs.iter().map(String::as_str).collect();
        let files = repo::ls_files(&specs);
        assert!(
            !files.is_empty(),
            "{rel}:{line}: {specs:?} cover no tracked file"
        );
        let missed: Vec<String> = files
            .into_iter()
            .filter(
                |file| match select(config, Lane::Push, std::slice::from_ref(file)) {
                    Selection::All => false,
                    Selection::Modules(selected) => !selected.contains(module),
                },
            )
            .collect();
        if !missed.is_empty() {
            problems.push(format!(
                "{rel}:{line} scans {} file(s) whose change does not select {module}, e.g. {}",
                missed.len(),
                missed[..missed.len().min(3)].join(", ")
            ));
        }
    }
    (scans.len(), problems)
}

#[test]
fn a_scan_beyond_its_area_is_reported_and_one_within_it_is_not() {
    // Arrange: BASE's `common_sh` area claims platform/lib/common.sh only, and its
    // `restart_policy` area claims the compose files.
    let config = config(BASE);
    let within = r#"fn t() { repo::grep("x", &[":(glob)platform/**/*compose*.yml"]); }"#;
    let beyond = r#"fn t() { repo::grep("x", &["platform/lib/common.sh", ":(glob)platform/**/*compose*.yml"]); }"#;

    // Act
    let (_, as_restart_policy) = unselected_scans(&config, "restart_policy", "inline.rs", within);
    let (scans, as_common_sh) = unselected_scans(&config, "common_sh", "inline.rs", beyond);

    // Assert
    assert!(as_restart_policy.is_empty(), "{as_restart_policy:?}");
    assert_eq!(scans, 1);
    assert_eq!(as_common_sh.len(), 1, "{as_common_sh:?}");
    assert!(
        as_common_sh[0].starts_with("inline.rs:1 scans ")
            && as_common_sh[0].contains("whose change does not select common_sh"),
        "{as_common_sh:?}"
    );
}

/// A module outside the security set runs only when a change selects its area. A scan that
/// reads files outside the area's paths would then be skipped by a change that adds an
/// offender there, wherever another area claims that file. So every file such a scan reads must
/// select the module in the push lane: by its area, by `full`, or by being unclassified (which
/// runs everything). A static module that scans beyond its area belongs in `always`
/// (areas.d/README.md).
#[test]
fn every_repo_wide_scan_runs_when_a_file_it_reads_changes() {
    // Arrange
    let config = repo_config();
    let Selection::Modules(always) = select(&config, Lane::Push, &[]) else {
        panic!("an empty change selects the security set, not everything");
    };

    // Act
    let mut scans = 0;
    let mut problems = vec![];
    for module in crate::SUITE_MODULES {
        if always.contains(*module) {
            continue;
        }
        let rel = format!("platform/tests/suite/{module}.rs");
        let src = text::read(&rel);
        let (read, found) = unselected_scans(&config, module, &rel, &src);
        scans += read;
        problems.extend(found);
    }

    // Assert
    assert!(
        scans > 0,
        "no repo-wide scan outside the security set; the reader is broken"
    );
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}
