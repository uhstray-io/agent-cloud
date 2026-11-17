//! `repo::playbook_files` and `yaml::strings` against `playbook_yaml.py` on the repository
//! (plan 24, AC-958; cli tier: it starts `python3`).
//!
//! `python3` must import PyYAML: CI's Rust job puts the locked `.venv/bin` first on PATH, and a
//! workstation runs `uv sync --locked` and does the same. The Python prints its file list, then
//! per file and per `keys` every string `playbook_yaml.strings` yields, hex-encoded so a newline
//! in a string is safe.
//!
//! The Python loads each file with `playbook_yaml.Loader`, except that PyYAML's int, float and
//! timestamp constructors return their scalar's text, as a `str` subclass the script marks. A
//! [`yaml::Node`] has no number type, so those scalars are strings on the Rust side; read as text
//! on the Python side too, they are yielded by both walks, and their keys compare as Rust's do (a
//! plain `53` and a quoted `"53"` are one key in both). The two walks then yield the same strings,
//! compared as multisets per file and per `keys`, with no allowance. The marked count is the
//! number of those yields that are numbers or dates. On this repository it is also the surplus of
//! `yaml::strings` over the stock Python walk; in general that surplus can be larger, because the
//! stock walk merges keys equal as Python values (`yes` and `1`, `10` and `1_0`).
//!
//! This module tests the Rust helpers against `playbook_yaml.py` and goes when that file is deleted.

use std::collections::BTreeMap;

use agent_cloud_tests::yaml;
use agent_cloud_tests::{process, repo};
use tempfile::TempDir;

const SCRIPT: &str = r#"
import yaml
import playbook_yaml as p

class Text(str):
    """A scalar PyYAML would construct as an int, float or timestamp, kept as its text."""

class Loader(p.Loader):
    """playbook_yaml's loader (a CSafeLoader or SafeLoader subclass), with the three
    constructors below replaced: still safe, it builds no Python object."""

for tag in ("int", "float", "timestamp"):
    Loader.add_constructor(
        "tag:yaml.org,2002:" + tag, lambda loader, node: Text(loader.construct_scalar(node))
    )

files = p.files()
for f in files:
    print("F", f.relative_to(p.REPO).as_posix())
for i, f in enumerate(files):
    doc = yaml.load(f.read_text(), Loader=Loader)
    for keys in (0, 1):
        for s in p.strings(doc, bool(keys)):
            print("S", i, keys, int(isinstance(s, Text)), s.encode().hex())
"#;

fn unhex(hex: &str) -> String {
    let bytes: Vec<u8> = (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).expect("hex from the script"))
        .collect();
    String::from_utf8(bytes).expect("utf-8 from the script")
}

fn counts<'a>(items: impl Iterator<Item = &'a str>) -> BTreeMap<&'a str, usize> {
    let mut map = BTreeMap::new();
    for item in items {
        *map.entry(item).or_insert(0) += 1;
    }
    map
}

#[test]
fn both_helpers_agree_with_playbook_yaml_on_every_repository_file() {
    // Arrange
    let home = TempDir::new().expect("a temporary directory");
    let mut cmd = process::scrubbed("python3", home.path());
    cmd.env("PYTHONPATH", repo::path("platform/tests"))
        .args(["-c", SCRIPT]);

    // Act
    let out = cmd.output().expect("python3 starts");
    assert!(
        out.status.success(),
        "playbook_yaml.py failed (is .venv/bin first on PATH?): {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8(out.stdout).expect("utf-8 output");
    let mut python_files = vec![];
    let mut python_strings: BTreeMap<(usize, bool), Vec<String>> = BTreeMap::new();
    let mut marked = 0;
    for line in stdout.lines() {
        if let Some(path) = line.strip_prefix("F ") {
            python_files.push(path.to_owned());
            continue;
        }
        let fields: Vec<&str> = line.split(' ').collect();
        let ["S", index, keys, text, hex] = fields.as_slice() else {
            panic!("an unexpected line from the script: {line}");
        };
        if *text == "1" {
            marked += 1;
        }
        python_strings
            .entry((index.parse().expect("an index"), *keys == "1"))
            .or_default()
            .push(unhex(hex));
    }
    let files = repo::playbook_files();

    // Assert: the same files, in the same order.
    assert_eq!(files, python_files);

    // Assert: per file and per `keys`, the same strings, as many times each.
    for (index, file) in files.iter().enumerate() {
        let node = yaml::load_one(&repo::path(file));
        for keys in [false, true] {
            let rust: Vec<&str> = node
                .as_ref()
                .map(|n| yaml::strings(n, keys))
                .unwrap_or_default();
            let python = python_strings
                .get(&(index, keys))
                .map(Vec::as_slice)
                .unwrap_or_default();
            assert_eq!(
                counts(rust.iter().copied()),
                counts(python.iter().map(String::as_str)),
                "{file} (keys {keys}): the two walks yield different strings"
            );
        }
    }
    eprintln!(
        "{} files; {} strings across both key passes, {marked} of them numbers or dates the \
         stock Python walk does not yield",
        files.len(),
        python_strings.values().map(Vec::len).sum::<usize>()
    );
}
