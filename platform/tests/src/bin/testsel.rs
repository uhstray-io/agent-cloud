//! `testsel <commit|push|ci|full>`: the nextest filterset a lane runs (plan 24, D3).
//!
//! `lane.sh` runs the filterset this prints. The changed paths come from git: the staged paths
//! for `commit`, and for `push` and `ci` the paths since the merge base with `origin/$BASE_REF`
//! (`origin/dev` when `BASE_REF` is unset or empty). When git cannot give the paths, the lane
//! runs every test and a warning goes to stderr: a selector that cannot see the change must not
//! decide that nothing changed.

use std::process::{Command, ExitCode};

use agent_cloud_tests::lanes::{self, Config, Lane, Selection};
use agent_cloud_tests::repo;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [name] = args.as_slice() else {
        eprintln!("usage: testsel commit|push|ci|full");
        return ExitCode::from(2);
    };
    let Some(lane) = Lane::parse(name) else {
        eprintln!("testsel: unknown lane '{name}' (commit, push, ci or full)");
        return ExitCode::from(2);
    };
    let config = match Config::load(&repo::path("platform/tests/areas.yml")) {
        Ok(config) => config,
        Err(why) => {
            eprintln!("testsel: {why}");
            return ExitCode::FAILURE;
        }
    };

    let selection = if lane == Lane::Full {
        Selection::All
    } else {
        match changed_paths(lane) {
            Ok(paths) => lanes::select(&config, lane, &paths),
            Err(why) => {
                eprintln!("testsel: warning: {why}; running every test");
                Selection::All
            }
        }
    };
    println!("{}", selection.filterset());
    ExitCode::SUCCESS
}

/// The repository paths the change of `lane` touches, or why git could not say. Renames are
/// listed as a delete and an add, so the path that went away still selects its area.
fn changed_paths(lane: Lane) -> Result<Vec<String>, String> {
    let mut git = Command::new("git");
    git.arg("-C")
        .arg(repo::root())
        .args(["diff", "--name-only", "--no-renames", "-z"]);
    if lane == Lane::Commit {
        git.arg("--cached");
    } else {
        git.arg(format!("origin/{}...HEAD", base_ref()));
    }
    let out = git.output().map_err(|e| format!("cannot run git: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "git diff failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    out.stdout
        .split(|byte| *byte == 0)
        .filter(|path| !path.is_empty())
        .map(|path| String::from_utf8(path.to_vec()).map_err(|e| format!("non-UTF-8 path: {e}")))
        .collect()
}

/// The branch a push or CI change is measured from: `BASE_REF` as CI sets it, else `dev`.
fn base_ref() -> String {
    match std::env::var("BASE_REF") {
        Ok(base) if !base.is_empty() => base,
        _ => "dev".to_owned(),
    }
}
