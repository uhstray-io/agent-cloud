//! The number typing the lifting loader must match, read from PyYAML itself (AC-943).
//!
//! `support_lift`'s agreement test accepts any JSON number for any `yaml::Node::Str`, so it cannot
//! see a number read as a string, or the reverse. This module is the oracle for that: one `python3`
//! process loads every repository file with PyYAML, and the lifting loader must produce the same
//! JSON for each. It starts a process, so it is in the cli tier (`areas.d/support_lift.yml`).

use std::time::Duration;

use agent_cloud_tests::{playbook, process, repo};
use serde_json::Value;
use tempfile::TempDir;

/// The two files the lifting loader refuses (a non-string mapping key); `support_lift` pins them.
const REFUSED: [&str; 2] = [
    "platform/playbooks/read-proxmox-root-ca.yml",
    "platform/playbooks/vars/proxmox-cpu-model-flags.yml",
];

/// Number forms, read through the same PyYAML process as the repository files. The repository
/// holds no plain leading-zero integer (PyYAML composes all 359 of its documents, 2026-11-17), so
/// dropping the octal branch of the lifting loader changes none of them. This fixture is what makes
/// the oracle see it.
const NUMBER_FORMS: &str = "\
octal: 0600
octal_zero: 00
binary: 0b101
hex: 0x1F
negative_hex: -0x1F
underscored: 1_000
sexagesimal: 190:20:30
sexagesimal_float: 3:25:45.5
exponent: 1.0e+3
leading_dot: .5
decimal: 53
string_08: 08
";

/// For each path in `argv`, one line: the file's documents as PyYAML's `safe_load_all` reads them.
const PYTHON: &str = r#"import json, sys, yaml
assert yaml.__version__ == "6.0.3", f"PyYAML {yaml.__version__}, the loader follows 6.0.3"
for f in sys.argv[1:]:
    print(json.dumps({"file": f, "docs": list(yaml.safe_load_all(open(f, encoding="utf-8")))}, ensure_ascii=False))
"#;

/// The index of the first byte at which `a` and `b` differ (the shorter length when one is a
/// prefix of the other).
fn first_difference(a: &str, b: &str) -> usize {
    a.bytes()
        .zip(b.bytes())
        .position(|(x, y)| x != y)
        .unwrap_or(a.len().min(b.len()))
}

#[test]
fn pyyaml_types_every_repository_file_as_the_lifting_loader_does() {
    let dir = TempDir::new().unwrap();
    let fixture = dir.path().join("number-forms.yml");
    std::fs::write(&fixture, NUMBER_FORMS).unwrap();

    let mut files: Vec<String> = repo::ls_files(&[
        ":(glob)platform/playbooks/**/*.yml",
        ":(glob)platform/semaphore/**/*.yml",
    ])
    .into_iter()
    .filter(|file| !REFUSED.contains(&file.as_str()))
    .collect();
    files.push(fixture.display().to_string());

    // One process for every file, bounded by run_for's 30 s.
    let mut cmd = process::scrubbed("python3", dir.path());
    cmd.current_dir(repo::root())
        // -P: the current directory (the repository root) is not put on sys.path.
        .arg("-P")
        .arg("-c")
        .arg(PYTHON)
        .args(&files);
    let out = process::run_for(&mut cmd, Duration::from_secs(30));
    assert!(
        out.status.success(),
        "the PyYAML reader failed:\n{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8(out.stdout).expect("PyYAML output is UTF-8");
    let lines: Vec<&str> = stdout.lines().collect();
    assert_eq!(lines.len(), files.len(), "one PyYAML line per file");

    for (line, file) in lines.iter().zip(&files) {
        let record: Value = serde_json::from_str(line)
            .unwrap_or_else(|e| panic!("{file}: the PyYAML line is not JSON: {e}"));
        assert_eq!(
            record["file"].as_str(),
            Some(file.as_str()),
            "PyYAML answers the files in argv order"
        );
        let text =
            std::fs::read_to_string(repo::path(file)).unwrap_or_else(|e| panic!("{file}: {e}"));
        let got = serde_json::to_string(&Value::Array(playbook::parse(&text, file)))
            .expect("a JSON value serializes");
        let expected = serde_json::to_string(&record["docs"]).expect("a JSON value serializes");
        assert!(
            got == expected,
            "{file}: the lifted JSON differs from PyYAML's, first at byte {}",
            first_difference(&got, &expected)
        );
    }
}
