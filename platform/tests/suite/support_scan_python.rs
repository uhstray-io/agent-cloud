//! `repo::playbook_files` and `yaml::strings` against `playbook_yaml.py` on the repository
//! (plan 24, AC-958; cli tier: it starts `python3`).
//!
//! `python3` must import PyYAML: CI's Rust job puts the locked `.venv/bin` first on PATH, and a
//! workstation runs `uv sync --locked` and does the same. The Python prints its file list; per
//! file and per `keys`, every string it yields; and per file, every plain scalar PyYAML resolves
//! to an int, float or timestamp (read from the composed node graph, so PyYAML itself decides).
//! Values are hex-encoded so a newline in a string is safe. This module tests the Rust helpers
//! against `playbook_yaml.py` and goes when that file is deleted.

use std::collections::BTreeMap;

use agent_cloud_tests::yaml;
use agent_cloud_tests::{process, repo};
use tempfile::TempDir;

const SCRIPT: &str = r#"
import yaml
import playbook_yaml as p
NOT_STR = {"tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:timestamp"}
def plain(node, out):
    if isinstance(node, yaml.ScalarNode):
        # Only a plain scalar resolves to these tags (no scanned file has an explicit tag), and
        # libyaml's loader reports a plain style as "" where the pure-Python one says None.
        if node.tag in NOT_STR:
            out.append(node.value)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            plain(child, out)
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            plain(key, out)
            plain(value, out)
files = p.files()
for f in files:
    print("F", f.relative_to(p.REPO).as_posix())
for i, f in enumerate(files):
    doc = p.load(f)
    for keys in (0, 1):
        for s in p.strings(doc, bool(keys)):
            print("S", i, keys, s.encode().hex())
    found = []
    plain(yaml.compose(f.read_text(), Loader=p.Loader), found)
    for s in found:
        print("N", i, s.encode().hex())
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
    let mut not_strings: BTreeMap<usize, Vec<String>> = BTreeMap::new();
    for line in stdout.lines() {
        let fields: Vec<&str> = line.split(' ').collect();
        match fields.as_slice() {
            ["F", path] => python_files.push((*path).to_owned()),
            ["S", index, keys, hex] => python_strings
                .entry((index.parse().expect("an index"), *keys == "1"))
                .or_default()
                .push(unhex(hex)),
            ["N", index, hex] => not_strings
                .entry(index.parse().expect("an index"))
                .or_default()
                .push(unhex(hex)),
            _ => panic!("an unexpected line from the script: {line}"),
        }
    }
    let files = repo::playbook_files();

    // Assert: the same files, in the same order.
    assert_eq!(files, python_files);

    // Assert: per file and per `keys`, every Python string is a Rust string, and every Rust-only
    // string is one of the file's plain scalars PyYAML resolves to an int, float or timestamp,
    // no more often than the file holds it.
    let mut extras = 0;
    for (index, file) in files.iter().enumerate() {
        let node = yaml::load_one(&repo::path(file));
        let allowed = counts(
            not_strings
                .get(&index)
                .map(Vec::as_slice)
                .unwrap_or_default()
                .iter()
                .map(String::as_str),
        );
        for keys in [false, true] {
            let rust: Vec<&str> = node
                .as_ref()
                .map(|n| yaml::strings(n, keys))
                .unwrap_or_default();
            let python = python_strings
                .get(&(index, keys))
                .map(Vec::as_slice)
                .unwrap_or_default();
            let rust_counts = counts(rust.iter().copied());
            let python_counts = counts(python.iter().map(String::as_str));
            for (s, n) in &python_counts {
                let have = rust_counts.get(s).copied().unwrap_or(0);
                assert!(
                    have >= *n,
                    "{file} (keys {keys}): Python yields {s:?} {n} times, Rust {have}"
                );
            }
            for (s, n) in &rust_counts {
                let surplus = n - python_counts.get(s).copied().unwrap_or(0);
                if surplus > 0 {
                    let may = allowed.get(s).copied().unwrap_or(0);
                    assert!(
                        surplus <= may,
                        "{file} (keys {keys}): Rust yields {s:?} {surplus} more times than \
                         Python, which reads it as a non-string plain scalar {may} times"
                    );
                    extras += surplus;
                }
            }
        }
    }
    eprintln!(
        "{} files; {} strings across both key passes; {extras} Rust-only plain numbers or dates",
        files.len(),
        python_strings.values().map(Vec::len).sum::<usize>()
    );
}
