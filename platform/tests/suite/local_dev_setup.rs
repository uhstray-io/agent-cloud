//! The local-dev setup files: `.gitignore` (the working inventory) and the `Brewfile` toolchain
//! (static tier).
//!
//! The working inventory `platform/inventory/local-dev.yml` holds a machine's own values, so it
//! must be ignored by git, while the committed template beside it must not be. The Brewfile is
//! the developer toolchain the local-dev scripts need (ansible, podman, jq, socat, ...), and it
//! must not make Docker Desktop a hard dependency, which needs root for some services. The
//! generic secret-file patterns are owned by the credential-leak tests, not repeated here.
//!
//! Replaces platform/tests/test_local_dev_setup.bats (plan 24, Phase 4).
//!
//! Rows marked `(candidate: pins text)` assert a substring of a comment or of a file that is not
//! a parsed format; they stay as pins.
//!
//! Port map:
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | .gitignore: platform/inventory/local-dev.yml is gitignored (working copy must not leak) | `gitignore_names_the_working_inventory` (candidate: pins text) |
//! | .gitignore: platform/inventory/local-dev.yml.example is NOT gitignored (template must be committed) | `gitignore_does_not_ignore_the_committed_template` |
//! | .gitignore: local-dev.yml entry is active (not commented out) | `the_working_inventory_entry_is_active_not_commented` |
//! | Brewfile: file exists at repo root | `brewfile_exists_at_the_repo_root` |
//! | Brewfile: ansible is listed (required for playbooks) | `brewfile_lists_every_required_tool` (case `ansible`) |
//! | Brewfile: ansible-lint is listed (CI quality gate) | `brewfile_lists_every_required_tool` (case `ansible-lint`) |
//! | Brewfile: bats-core is listed (test runner) | `brewfile_lists_every_required_tool` (case `bats-core`) |
//! | Brewfile: jq is listed (required by bootstrap scripts) | `brewfile_lists_every_required_tool` (case `jq`) |
//! | Brewfile: podman is listed (primary container engine) | `brewfile_lists_every_required_tool` (case `podman`) |
//! | Brewfile: podman-compose is listed (compose wrapper) | `brewfile_lists_every_required_tool` (case `podman-compose`) |
//! | Brewfile: shellcheck is listed (shell lint in CI) | `brewfile_lists_every_required_tool` (case `shellcheck`) |
//! | Brewfile: socat is listed (privileged-port forwarder for make local-https) | `brewfile_lists_every_required_tool` (case `socat`) |
//! | Brewfile: yamllint is listed (YAML validation in CI) | `brewfile_lists_every_required_tool` (case `yamllint`) |
//! | Brewfile: gh is listed (used by promote workflow) | `brewfile_lists_every_required_tool` (case `gh`) |
//! | Brewfile: make is listed | `brewfile_lists_every_required_tool` (case `make`) |
//! | Brewfile: Docker Desktop is optional/commented, not a hard dependency | `docker_desktop_is_not_a_hard_brew_or_cask_dependency` |
//! | Brewfile: socat comment references make local-https (documents purpose) | `socat_line_documents_make_local_https` (candidate: pins text) |

use agent_cloud_tests::repo;

const GITIGNORE: &str = ".gitignore";
const BREWFILE: &str = "Brewfile";
const WORKING_INVENTORY: &str = "platform/inventory/local-dev.yml";
const COMMITTED_TEMPLATE: &str = "platform/inventory/local-dev.yml.example";

/// The tools the local-dev scripts and CI need; each is one `brew "<name>"` line.
const REQUIRED_TOOLS: [&str; 11] = [
    "ansible",
    "ansible-lint",
    "bats-core",
    "jq",
    "podman",
    "podman-compose",
    "shellcheck",
    "socat",
    "yamllint",
    "gh",
    "make",
];

fn text(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// The lines of a gitignore that are patterns: not blank, not a comment. Git reads a leading
/// space as part of the pattern, so the lines are kept as written.
fn active_lines(gitignore: &str) -> Vec<&str> {
    gitignore
        .lines()
        .filter(|l| !l.trim().is_empty() && !l.trim_start().starts_with('#'))
        .collect()
}

/// Whether an active gitignore line names `path` exactly, with at most trailing blanks after it.
fn ignores_exactly(line: &str, path: &str) -> bool {
    line.trim_start()
        .strip_prefix(path)
        .is_some_and(|rest| rest.is_empty() || rest.starts_with(char::is_whitespace))
}

/// The names of the `<kind> "<name>"` directives of a Brewfile, in file order.
fn brewfile_names<'a>(brewfile: &'a str, kind: &str) -> Vec<&'a str> {
    let prefix = format!("{kind} \"");
    brewfile
        .lines()
        .filter_map(|line| {
            let rest = line.strip_prefix(&prefix)?;
            rest.find('"').map(|end| &rest[..end])
        })
        .collect()
}

#[test]
fn gitignore_names_the_working_inventory() {
    // Arrange
    let gitignore = text(GITIGNORE);

    // Act, Assert (candidate: pins text; the active-entry form is the next test)
    assert!(
        gitignore.contains(WORKING_INVENTORY),
        "{WORKING_INVENTORY} is not gitignored"
    );
}

#[test]
fn gitignore_does_not_ignore_the_committed_template() {
    // Arrange: the example is the source of truth and must be committed
    let gitignore = text(GITIGNORE);
    let active = active_lines(&gitignore);

    // Act
    let ignored = active.iter().any(|l| l.starts_with(COMMITTED_TEMPLATE));

    // Assert
    assert!(
        !ignored,
        "{COMMITTED_TEMPLATE} is gitignored, so the template is not committed"
    );
}

#[test]
fn the_working_inventory_entry_is_active_not_commented() {
    // Arrange: a commented entry would let the working inventory be committed silently
    let gitignore = text(GITIGNORE);
    let active = active_lines(&gitignore);

    // Act
    let found = active.iter().any(|l| ignores_exactly(l, WORKING_INVENTORY));

    // Assert
    assert!(found, "no active gitignore line names {WORKING_INVENTORY}");
}

#[test]
fn brewfile_exists_at_the_repo_root() {
    // Arrange, Act
    let exists = repo::path(BREWFILE).is_file();

    // Assert
    assert!(exists, "{BREWFILE} is missing at the repository root");
}

#[test]
fn brewfile_lists_every_required_tool() {
    // Arrange
    let brewfile = text(BREWFILE);
    let listed = brewfile_names(&brewfile, "brew");

    // Act + Assert: each tool names its own failure
    for tool in REQUIRED_TOOLS {
        assert!(
            listed.contains(&tool),
            "the Brewfile does not list brew \"{tool}\""
        );
    }
}

#[test]
fn docker_desktop_is_not_a_hard_brew_or_cask_dependency() {
    // Arrange: Docker Desktop needs root for some services, so it is an optional comment only
    let brewfile = text(BREWFILE);

    // Act
    let brews = brewfile_names(&brewfile, "brew");
    let casks = brewfile_names(&brewfile, "cask");

    // Assert
    assert!(
        !brews.contains(&"docker"),
        "brew \"docker\" is a hard dependency"
    );
    assert!(
        !casks.contains(&"docker"),
        "cask \"docker\" is a hard dependency"
    );
}

#[test]
fn socat_line_documents_make_local_https() {
    // Arrange: the socat line is the one that names the privileged-port forwarder's purpose
    let brewfile = text(BREWFILE);
    let socat = brewfile
        .lines()
        .find(|l| l.starts_with("brew \"socat\""))
        .unwrap_or_else(|| panic!("the Brewfile has no socat line"));

    // Act, Assert (candidate: pins text)
    assert!(
        socat.contains("local-https"),
        "the socat line does not name make local-https: {socat}"
    );
}
