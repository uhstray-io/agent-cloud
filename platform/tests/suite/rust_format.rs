//! The suite's modules are formatted (plan 24, D10).
//!
//! `build.rs` declares the modules through an `include!`, and `cargo fmt` does not follow an
//! `include!`d module, so `cargo fmt --check` passes a misformatted one. This runs `rustfmt
//! --check` over every `suite/*.rs` itself, at the crate's edition. It never skips: a missing
//! `rustfmt` fails, because a check that cannot run proves nothing.

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use tempfile::TempDir;

/// The `edition = "..."` of `platform/tests/Cargo.toml`.
fn edition() -> String {
    let manifest =
        std::fs::read_to_string(repo::path("platform/tests/Cargo.toml")).expect("Cargo.toml reads");
    let line = regex::Regex::new(r#"(?m)^edition\s*=\s*"([^"]+)""#).expect("valid pattern");
    line.captures(&manifest)
        .expect("Cargo.toml declares an edition")[1]
        .to_owned()
}

#[test]
fn every_suite_module_is_rustfmt_clean() {
    let edition = edition();
    // main.rs and every module build.rs compiled.
    let files: Vec<_> = std::iter::once("main")
        .chain(crate::SUITE_MODULES.iter().copied())
        .map(|name| repo::path(&format!("platform/tests/suite/{name}.rs")))
        .collect();
    let home = TempDir::new().expect("a temporary directory");

    let mut cmd = process::scrubbed("rustfmt", home.path());
    // rustup's `rustfmt` proxy finds its toolchain through these; the scrubbed HOME hides
    // the default `~/.rustup` and `~/.cargo`.
    let real_home = std::env::var_os("HOME").map(std::path::PathBuf::from);
    for (var, default) in [("RUSTUP_HOME", ".rustup"), ("CARGO_HOME", ".cargo")] {
        match (std::env::var_os(var), &real_home) {
            (Some(value), _) => {
                cmd.env(var, value);
            }
            (None, Some(home)) => {
                cmd.env(var, home.join(default));
            }
            (None, None) => {}
        }
    }
    if let Some(toolchain) = std::env::var_os("RUSTUP_TOOLCHAIN") {
        cmd.env("RUSTUP_TOOLCHAIN", toolchain);
    }
    let output = cmd
        .args(["--check", "--edition", &edition])
        .args(&files)
        .output()
        .unwrap_or_else(|e| {
            panic!("rustfmt did not start ({e}); install it: rustup component add rustfmt")
        });

    assert!(
        output.status.success(),
        "rustfmt --check failed ({}). Fix with: rustfmt --edition {edition} platform/tests/suite/*.rs\n{}{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
