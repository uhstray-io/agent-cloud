//! The lifting loader (`src/playbook.rs`, AC-943), checked without starting a process.
//!
//! The 6c agreement is the reason this module is in the security set (`areas.d/support_lift.yml`,
//! `always`): it pins the lifting loader to `yaml::parse` over every repository playbook, so a
//! change that drops, reorders or changes the text of a value in one file fails every commit.
//! It accepts any number where `yaml::parse` has a string, so number typing is pinned elsewhere:
//! by `plain_scalars_lift_with_pyyaml_types` here, and over the repository files by
//! `support_lift_pyyaml` (cli tier).

use std::collections::BTreeSet;

use agent_cloud_tests::yaml::{self, Node};
use agent_cloud_tests::{playbook, repo};
use serde_json::{Value, json};
use tempfile::TempDir;

/// The message of a caught panic.
fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| payload.downcast_ref::<&str>().map(|s| (*s).to_owned()))
        .unwrap_or_default()
}

/// Whether a lifted value stands for the same YAML as a `yaml::parse` node: the same keys in the
/// same order, the same sequence items, and equal bools, nulls and strings. A JSON number stands
/// for any `Node::Str`, because the Node loader keeps every plain number as its text. An error
/// is the mismatch; each enclosing key or index is prepended as it returns, so the path is built
/// only when something differs.
fn agrees(value: &Value, node: &Node) -> Result<(), String> {
    match (value, node) {
        (Value::Object(map), Node::Map(entries)) => {
            if map.len() != entries.len() {
                return Err(format!(
                    ": {} keys, yaml::parse gives {}",
                    map.len(),
                    entries.len()
                ));
            }
            for ((lifted_key, lifted), (key, child)) in map.iter().zip(entries) {
                let Some(name) = key.as_str() else {
                    return Err(": a mapping key that is not a string".to_owned());
                };
                if lifted_key != name {
                    return Err(format!(
                        ": key {lifted_key:?} where yaml::parse gives {name:?}"
                    ));
                }
                agrees(lifted, child).map_err(|why| format!(".{name}{why}"))?;
            }
            Ok(())
        }
        (Value::Array(items), Node::Seq(nodes)) => {
            if items.len() != nodes.len() {
                return Err(format!(
                    ": {} items, yaml::parse gives {}",
                    items.len(),
                    nodes.len()
                ));
            }
            for (index, (item, child)) in items.iter().zip(nodes).enumerate() {
                agrees(item, child).map_err(|why| format!("[{index}]{why}"))?;
            }
            Ok(())
        }
        (Value::Bool(a), Node::Bool(b)) if a == b => Ok(()),
        (Value::Null, Node::Null) => Ok(()),
        (Value::String(a), Node::Str(b)) if a == b => Ok(()),
        (Value::Number(_), Node::Str(_)) => Ok(()),
        _ => Err(format!(": {value} where yaml::parse gives {node:?}")),
    }
}

/// Each case is a YAML scalar text and the value PyYAML 6.0.3 reads for it
/// (`yaml.safe_load("v: " + text)["v"]`). The values were generated with the locked venv,
/// looping over the texts: `.venv/bin/python -c 'import yaml; print(repr(yaml.safe_load("v: 0600")["v"]))'`.
/// A text PyYAML reads as a string stays a string here: `08`, `1e3`, `1.0e3`, `-.5`, `1:60`.
#[test]
fn plain_scalars_lift_with_pyyaml_types() {
    let cases = [
        ("53", json!(53)),
        ("0600", json!(384)),
        ("0b101", json!(5)),
        ("0x1F", json!(31)),
        ("-0x1F", json!(-31)),
        ("1_000", json!(1000)),
        ("+12", json!(12)),
        ("0", json!(0)),
        ("00", json!(0)),
        ("190:20:30", json!(685230)),
        ("08", json!("08")),
        ("1:60", json!("1:60")),
        ("0x", json!("0x")),
        ("1e3", json!("1e3")),
        ("1.5", json!(1.5)),
        ("1.", json!(1.0)),
        ("1.0e+3", json!(1000.0)),
        ("1.0e3", json!("1.0e3")),
        (".5", json!(0.5)),
        ("-.5", json!("-.5")),
        ("3:25:45.5", json!(12345.5)),
        ("yes", json!(true)),
        ("No", json!(false)),
        ("on", json!(true)),
        ("OFF", json!(false)),
        ("true", json!(true)),
        ("~", Value::Null),
        ("null", Value::Null),
        ("", Value::Null),
        ("abc", json!("abc")),
        ("\"53\"", json!("53")),
        ("'0600'", json!("0600")),
        ("\"yes\"", json!("yes")),
        ("\"\"", json!("")),
    ];
    for (text, expected) in cases {
        let doc = playbook::loads(&format!("v: {text}"));
        assert_eq!(doc["v"], expected, "plain or quoted scalar {text:?}");
    }
}

/// The forms JSON cannot keep, or the loader will not read as PyYAML does, are refused, and the
/// refusal names the file's mapping key and the offending text.
#[test]
fn forms_json_cannot_hold_are_refused_by_name() {
    let cases: [(&str, &[&str]); 8] = [
        ("v: .inf", &["key `v`", ".inf"]),
        ("v: .nan", &["key `v`", ".nan"]),
        ("v: 2026-11-17", &["key `v`", "2026-11-17"]),
        ("v: !custom x", &["key `v`", "!custom"]),
        ("v: !!str x", &["key `v`", "!!str"]),
        ("200: a", &["key `200`", "200"]),
        ("yes: a", &["key `yes`", "yes"]),
        ("? [a]\n: b", &["key `<non-scalar key>`", "a sequence"]),
    ];
    for (text, needles) in cases {
        let payload = std::panic::catch_unwind(|| playbook::loads(text))
            .expect_err(&format!("{text:?} must be refused"));
        let message = panic_message(payload.as_ref());
        for needle in needles {
            assert!(
                message.contains(needle),
                "{text:?}: the refusal must contain {needle:?}, found: {message}"
            );
        }
    }
}

/// Every repository playbook and semaphore file lifts to the structure `yaml::parse` gives: the
/// same keys in the same order, the same items, equal bools, nulls and strings. Only the two
/// files with a non-string key are refused, and by name.
#[test]
fn every_repository_yaml_file_has_the_structure_yaml_parse_gives() {
    let mut refused = BTreeSet::new();
    let files = repo::ls_files(&[
        ":(glob)platform/playbooks/**/*.yml",
        ":(glob)platform/semaphore/**/*.yml",
    ]);
    for file in files {
        let text =
            std::fs::read_to_string(repo::path(&file)).unwrap_or_else(|e| panic!("{file}: {e}"));
        let Ok(lifted) = std::panic::catch_unwind(|| playbook::parse(&text, &file)) else {
            refused.insert(file);
            continue;
        };
        let nodes = yaml::parse(&text, &file);
        assert_eq!(lifted.len(), nodes.len(), "{file}: document count");
        for (index, (value, node)) in lifted.iter().zip(&nodes).enumerate() {
            if let Err(why) = agrees(value, node) {
                panic!("{file}[{index}]{why}");
            }
        }
    }
    let expected: BTreeSet<String> = [
        "platform/playbooks/read-proxmox-root-ca.yml",
        "platform/playbooks/vars/proxmox-cpu-model-flags.yml",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    assert_eq!(
        refused, expected,
        "the files the lifting loader refuses (a non-string mapping key) changed"
    );
}

#[test]
fn duplicate_keys_and_merges_resolve_as_pyyaml_does() {
    // No repository file holds a duplicate key or a `<<` merge (2026-11-17), so the agreement
    // test above never reaches either path. Expected text from PyYAML 6.0.3:
    // `json.dumps(yaml.safe_load(text)[k], separators=(",", ":"))`. A later duplicate wins in
    // the first key's place, explicit keys beat merged ones, and a merge list is read
    // last-first, so its first mapping wins.
    let text = "\
base: &base {a: 1, b: 2}
other: &other {b: 20, c: 30}
dup: {x: 1, y: 2, x: 3}
merged:
  <<: *base
  b: 9
listed:
  <<: [*base, *other]
  d: 4
";
    let lifted = playbook::loads(text);
    for (key, expected) in [
        ("dup", r#"{"x":3,"y":2}"#),
        ("merged", r#"{"a":1,"b":9}"#),
        ("listed", r#"{"b":2,"c":30,"a":1,"d":4}"#),
    ] {
        let got = serde_json::to_string(&lifted[key]).expect("a JSON value serializes");
        assert_eq!(got, expected, "key {key}: value and key order");
    }
    let node = yaml::parse(text, "<fixture>").pop().expect("one document");
    agrees(&lifted, &node).unwrap_or_else(|why| panic!("<fixture>{why}"));
}

/// `plays` drops the leading guard import and nothing else. A playbook without the guard keeps
/// every play.
#[test]
fn plays_drops_only_the_leading_extra_var_guard() {
    let rel = "platform/playbooks/apply-firewall.yml";
    let text = std::fs::read_to_string(repo::path(rel)).expect(rel);
    let mut docs = playbook::parse(&text, rel);
    let Value::Array(all) = docs.remove(0) else {
        panic!("{rel}: the document is a list of plays");
    };
    assert_eq!(
        all[0]["ansible.builtin.import_playbook"],
        json!(playbook::OVERRIDE_GUARD),
        "{rel} opens with the guard import"
    );
    assert_eq!(
        playbook::plays(rel),
        all[1..].to_vec(),
        "plays drops element 0 and nothing else"
    );

    let dir = TempDir::new().unwrap();
    let fixture = dir.path().join("fixture.yml");
    std::fs::write(
        &fixture,
        "- hosts: all\n  tasks: []\n- hosts: localhost\n  tasks: []\n",
    )
    .unwrap();
    assert_eq!(
        playbook::plays(&fixture.display().to_string()).len(),
        2,
        "a playbook without the guard keeps every play"
    );
}

/// A lifted playbook written to JSON reads back unchanged, and a mapping keeps its key order
/// (Jinja iterates a dict in insertion order).
#[test]
fn a_written_playbook_reads_back_unchanged() {
    let dir = TempDir::new().unwrap();
    let original = Value::Array(playbook::plays("platform/playbooks/apply-firewall.yml"));
    let path = playbook::write(dir.path(), "lifted.json", &original);
    let text = std::fs::read_to_string(&path).unwrap();
    assert_eq!(playbook::loads(&text), original);

    let fixture = playbook::loads("vars:\n  z: 1\n  a: 2\n  m: 3\n");
    let path = playbook::write(dir.path(), "order.json", &fixture);
    let read_back = playbook::loads(&std::fs::read_to_string(&path).unwrap());
    let keys: Vec<&str> = read_back["vars"]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(keys, ["z", "a", "m"], "a mapping keeps its key order");
}
