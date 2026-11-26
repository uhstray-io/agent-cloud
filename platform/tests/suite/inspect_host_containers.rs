//! The probe script of `platform/playbooks/inspect-host-containers.yml` (cli tier). Each of the four
//! probes runs the inspect play's `_script` var as `sh -c <script> inspect <engine> <inspect format>
//! <store format>`. These tests run that same text with fake `podman` and `docker` executables on PATH,
//! and assert on exact output lines: the store line a podman probe prints from `podman info`, the note
//! when `podman info` fails, Docker's probe printing no store line and never calling `info`, and the
//! store format's construction and the probes' argv.
//!
//! The script is read from the playbook, so a test runs the text the playbook holds.

use std::ffi::OsStr;
use std::path::Path;

use agent_cloud_tests::process::{fake_bin, scrubbed};
use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/inspect-host-containers.yml";

/// The inspect format a probe passes as `$2`. No test here reaches an `inspect`, so any text serves.
const INSPECT_FMT: &str = "{{.Name}}";

/// The store format a podman probe passes as `$3`: the two fields of `_store_fields`, as a Go template.
const STORE_FMT: &str = "{{.Store.GraphDriverName}}|{{.Store.GraphOptions}}";

/// The inspect play: the playbook's second play, whose `hosts` name `target_host` (the first imports the
/// extra-var guard).
fn inspect_play() -> Node {
    let doc = yaml::load_one(&repo::path(PLAYBOOK)).expect("the playbook is one YAML document");
    let play = doc.as_seq().expect("the playbook is a list of plays")[1].clone();
    assert!(
        play.get("hosts")
            .and_then(|hosts| hosts.as_str())
            .is_some_and(|hosts| hosts.contains("target_host")),
        "play[1] is the inspect play"
    );
    play
}

fn vars() -> Node {
    inspect_play()
        .get("vars")
        .cloned()
        .expect("the inspect play has vars")
}

/// The string value of a var of the inspect play.
fn str_var(vars: &Node, name: &str) -> String {
    vars.get(name)
        .and_then(|value| value.as_str())
        .unwrap_or_else(|| panic!("vars.{name} is a string"))
        .to_owned()
}

/// The list value of a var of the inspect play, each item a string.
fn str_list_var(vars: &Node, name: &str) -> Vec<String> {
    vars.get(name)
        .and_then(|value| value.as_seq())
        .unwrap_or_else(|| panic!("vars.{name} is a list"))
        .iter()
        .map(|item| item.as_str().expect("a string item").to_owned())
        .collect()
}

/// Runs the probe script for `engine` as a probe does, with `path` as PATH and `home` as HOME and
/// TMPDIR. Returns the whole output, whatever the exit code.
fn probe(engine: &str, path: &OsStr, home: &Path) -> std::process::Output {
    let script = str_var(&vars(), "_script");
    scrubbed("sh", home)
        .env("PATH", path)
        .args([
            "-c",
            script.as_str(),
            "inspect",
            engine,
            INSPECT_FMT,
            STORE_FMT,
        ])
        .assert()
        .get_output()
        .clone()
}

fn stdout_of(out: &std::process::Output) -> String {
    String::from_utf8(out.stdout.clone()).expect("utf-8 stdout")
}

/// A fake engine's log under `home`, which the fake writes to as `$HOME/<name>`.
fn read_log(home: &Path, name: &str) -> String {
    std::fs::read_to_string(home.join(name)).unwrap_or_else(|e| panic!("{name}: {e}"))
}

#[test]
fn a_podman_probe_prints_the_store_driver_and_options_from_podman_info() {
    // Arrange: a fake podman whose `info` prints the driver and the options, and logs each `info` call.
    let home = TempDir::new().expect("a temporary home");
    let path = fake_bin(
        home.path(),
        &[(
            "podman",
            r#"case "$1" in
  --version) echo "podman version 4.9.3" ;;
  info) printf '%s\n' "$*" >> "$HOME/podman.log"; echo "overlay|map[]" ;;
esac"#,
        )],
    );

    // Act
    let out = probe("podman", &path, home.path());

    // Assert: the store line from `info`, and one `info` call with the store format the probe passed.
    let stdout = stdout_of(&out);
    assert!(out.status.success(), "{stdout}");
    assert!(
        stdout.lines().any(|line| line == "store: overlay|map[]"),
        "{stdout}"
    );
    assert_eq!(
        read_log(home.path(), "podman.log"),
        format!("info --format {STORE_FMT}\n")
    );
}

#[test]
fn a_podman_whose_info_fails_prints_a_note_in_place_of_the_store_line_and_the_probe_exits_0() {
    // Arrange: a fake podman whose `info` exits 125.
    let home = TempDir::new().expect("a temporary home");
    let path = fake_bin(
        home.path(),
        &[(
            "podman",
            r#"case "$1" in
  --version) echo "podman version 4.9.3" ;;
  info) exit 125 ;;
esac"#,
        )],
    );

    // Act
    let out = probe("podman", &path, home.path());

    // Assert: the note, no store line, and the probe's exit status is 0.
    let stdout = stdout_of(&out);
    assert_eq!(out.status.code(), Some(0), "{stdout}");
    assert!(
        stdout.lines().any(|line| line == "(podman info failed)"),
        "{stdout}"
    );
    assert!(
        !stdout.lines().any(|line| line.starts_with("store:")),
        "{stdout}"
    );
}

#[test]
fn a_docker_probe_prints_no_store_line_and_never_calls_info() {
    // Arrange: a fake docker that logs every call under HOME, and prints a version for --version.
    let home = TempDir::new().expect("a temporary home");
    let path = fake_bin(
        home.path(),
        &[(
            "docker",
            r#"printf '%s\n' "$*" >> "$HOME/docker.log"
case "$1" in
  --version) echo "Docker version 24.0.0" ;;
esac"#,
        )],
    );

    // Act
    let out = probe("docker", &path, home.path());

    // Assert: the docker probe ran (its version line printed, its calls logged), printed no store line,
    // and its log holds no `info` call.
    let stdout = stdout_of(&out);
    assert!(out.status.success(), "{stdout}");
    assert!(
        stdout.lines().any(|line| line == "Docker version 24.0.0"),
        "{stdout}"
    );
    assert!(
        !stdout.lines().any(|line| line.starts_with("store:")),
        "{stdout}"
    );
    let log = read_log(home.path(), "docker.log");
    assert!(
        !log.lines()
            .any(|line| line == "info" || line.starts_with("info ")),
        "{log}"
    );
}

#[test]
fn the_store_format_is_built_from_the_store_fields_the_way_the_inspect_format_is() {
    // Arrange
    let vars = vars();

    // Assert: the two podman info fields; the store format is the inspect format's Jinja construction
    // over those fields; and that construction renders each field as `{{field}}`, joined by `|`, which is
    // the store format the other tests pass.
    let fields = str_list_var(&vars, "_store_fields");
    assert_eq!(fields, [".Store.GraphDriverName", ".Store.GraphOptions"]);
    assert_eq!(
        str_var(&vars, "_store_fmt"),
        str_var(&vars, "_fmt").replace("_fields", "_store_fields")
    );
    let rendered: Vec<String> = fields
        .iter()
        .map(|field| format!("{{{{{field}}}}}"))
        .collect();
    assert_eq!(rendered.join("|"), STORE_FMT);
}

#[test]
fn each_probe_passes_the_store_format_as_the_argv_element_after_the_inspect_format() {
    // Arrange: the four probes are the command tasks whose argv runs the script.
    let play = inspect_play();
    let tasks = play
        .get("tasks")
        .and_then(|tasks| tasks.as_seq())
        .expect("the inspect play has tasks");
    let argvs: Vec<&str> = tasks
        .iter()
        .filter_map(|task| task.get("ansible.builtin.command")?.get("argv")?.as_str())
        .filter(|argv| argv.contains("_script"))
        .collect();

    // Assert: four probes, each ending with the inspect format and then the store format.
    assert_eq!(argvs.len(), 4, "{argvs:?}");
    for argv in argvs {
        assert!(argv.ends_with(", _fmt, _store_fmt] }}"), "{argv}");
    }
}
