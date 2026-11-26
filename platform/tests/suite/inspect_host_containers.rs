//! The probe script of `platform/playbooks/inspect-host-containers.yml` (cli tier). Each of the four
//! probes runs the inspect play's `_script` var with an inspect format as `$2`. The three podman
//! probes also pass a store format as `$3`; the docker probe passes none. These tests run that same
//! text with fake `podman` and `docker` executables on PATH, and assert on exact output lines: the
//! store line a podman probe prints from `podman info`, the note when `podman info` fails, Docker's
//! probe printing no store line and never calling `info`, and the store format's construction and the
//! probes' argv.
//!
//! The script is read from the playbook, so a test runs the text the playbook holds.

use std::path::Path;
use std::time::Duration;

use agent_cloud_tests::process::{self, fake_bin, scrubbed};
use agent_cloud_tests::repo;
use agent_cloud_tests::text;
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

/// Runs the probe script for `engine` as its probe does, in a fresh home with `fakes` as the executables
/// on PATH (`fake_bin`). A podman probe passes the store format as `$3`; a docker probe passes none, as
/// its argv does. Returns the home, which holds each fake's log, and the probe's stdout. The probe must
/// exit 0.
fn run_probe(engine: &str, fakes: &[(&str, &str)]) -> (TempDir, String) {
    let home = TempDir::new().expect("a temporary home");
    let path = fake_bin(home.path(), fakes);
    let script = str_var(&vars(), "_script");
    let store: &[&str] = if engine == "podman" {
        &[STORE_FMT]
    } else {
        &[]
    };
    let mut cmd = scrubbed("sh", home.path());
    cmd.env("PATH", &path)
        .args(["-c", script.as_str(), "inspect", engine, INSPECT_FMT])
        .args(store);
    let out = process::run_for(&mut cmd, Duration::from_secs(30));
    let stdout = String::from_utf8(out.stdout).expect("utf-8 stdout");
    assert!(out.status.success(), "{stdout}");
    (home, stdout)
}

/// A fake engine's log under `home`, which the fake writes to as `$HOME/<name>`.
fn read_log(home: &Path, name: &str) -> String {
    std::fs::read_to_string(home.join(name)).unwrap_or_else(|e| panic!("{name}: {e}"))
}

#[test]
fn a_podman_probe_prints_the_store_driver_and_options_from_podman_info() {
    // Arrange: a fake podman whose `info` prints the driver and the options, and logs each `info` call.
    let fakes = [(
        "podman",
        r#"case "$1" in
  --version) echo "podman version 4.9.3" ;;
  info) printf '%s\n' "$*" >> "$HOME/podman.log"; echo "overlay|map[]" ;;
esac"#,
    )];

    // Act
    let (home, stdout) = run_probe("podman", &fakes);

    // Assert: the store line from `info`, and one `info` call with the store format the probe passed.
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
    let fakes = [(
        "podman",
        r#"case "$1" in
  --version) echo "podman version 4.9.3" ;;
  info) exit 125 ;;
esac"#,
    )];

    // Act: run_probe asserts that the probe exits 0.
    let (_home, stdout) = run_probe("podman", &fakes);

    // Assert: the note, and no store line.
    assert!(
        stdout.lines().any(|line| line == "(podman info failed)"),
        "{stdout}"
    );
    assert!(!text::any_line(&stdout, r"^store:"), "{stdout}");
}

#[test]
fn a_docker_probe_prints_no_store_line_and_never_calls_info() {
    // Arrange: a fake docker that logs every call under HOME, and prints a version for --version.
    let fakes = [(
        "docker",
        r#"printf '%s\n' "$*" >> "$HOME/docker.log"
case "$1" in
  --version) echo "Docker version 24.0.0" ;;
esac"#,
    )];

    // Act
    let (home, stdout) = run_probe("docker", &fakes);

    // Assert: the docker probe ran (its version line printed, its calls logged), printed no store line,
    // and its log holds no `info` call.
    assert!(
        stdout.lines().any(|line| line == "Docker version 24.0.0"),
        "{stdout}"
    );
    assert!(!text::any_line(&stdout, r"^store:"), "{stdout}");
    let log = read_log(home.path(), "docker.log");
    assert!(!text::any_line(&log, r"^info( |$)"), "{log}");
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
fn each_podman_probe_passes_the_store_format_after_the_inspect_format_and_docker_does_not() {
    // Arrange: the four probes are the command tasks whose argv runs the script, split by engine.
    let tasks = repo::tasks(PLAYBOOK);
    let argvs: Vec<&str> = tasks
        .iter()
        .filter_map(|task| task.get("ansible.builtin.command")?.get("argv")?.as_str())
        .filter(|argv| argv.contains("_script"))
        .collect();
    let (docker, podman): (Vec<&str>, Vec<&str>) = argvs
        .iter()
        .copied()
        .partition(|argv| argv.contains("'docker'"));

    // Assert: three podman probes, each ending with the inspect format and then the store format; and one
    // docker probe, ending with the inspect format and holding no store format.
    assert_eq!(podman.len(), 3, "{argvs:?}");
    assert_eq!(docker.len(), 1, "{argvs:?}");
    for argv in podman {
        assert!(argv.ends_with(", _fmt, _store_fmt] }}"), "{argv}");
    }
    for argv in docker {
        assert!(argv.ends_with(", _fmt] }}"), "{argv}");
        assert!(!argv.contains("_store_fmt"), "{argv}");
    }
}
