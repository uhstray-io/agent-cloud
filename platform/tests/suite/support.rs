//! The suite's support library (`src/lib.rs`): YAML read as PyYAML reads it, and the
//! repository root it reads from. These live in the suite binary, not as library unit tests,
//! so the crate builds one test binary (plan 24, design principle 3).

use std::process::Command;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{Node, load_one, parse, resolve_plain};

#[test]
fn the_repository_root_is_the_checkout_under_test() {
    // nextest runs each test in the crate directory. A root compiled into a build shared by
    // two checkouts would point at the other one (#1110 review).
    let cwd = std::env::current_dir().unwrap();
    let out = Command::new("git")
        .arg("-C")
        .arg(&cwd)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .unwrap();
    let toplevel = std::path::PathBuf::from(String::from_utf8(out.stdout).unwrap().trim());

    assert_eq!(repo::root(), toplevel.canonicalize().unwrap());
}

#[test]
fn plain_scalars_resolve_as_pyyaml_does() {
    let cases = [
        ("no", Node::Bool(false)),
        ("Off", Node::Bool(false)),
        ("YES", Node::Bool(true)),
        ("on", Node::Bool(true)),
        ("~", Node::Null),
        ("", Node::Null),
        ("NULL", Node::Null),
        ("always", Node::Str("always".into())),
        ("n", Node::Str("n".into())),
        ("nO", Node::Str("nO".into())),
    ];
    for (input, expected) in cases {
        assert_eq!(resolve_plain(input), expected, "plain scalar {input:?}");
    }
}

#[test]
fn quoted_and_tagged_scalars_stay_strings() {
    let doc = &parse("a: 'no'\nb: \"yes\"\nc: !reset no\nd: no\n", "inline")[0];
    assert_eq!(doc.get("a"), Some(&Node::Str("no".into())));
    assert_eq!(doc.get("b"), Some(&Node::Str("yes".into())));
    assert_eq!(doc.get("c"), Some(&Node::Str("no".into())));
    assert_eq!(doc.get("d"), Some(&Node::Bool(false)));
}

#[test]
fn aliases_and_merge_keys_resolve_with_explicit_keys_winning() {
    let text = "base: &b {restart: always, image: x}\n\
                other: &o {image: y, user: z}\n\
                svc:\n  <<: [*b, *o]\n  image: mine\n\
                copy: *b\n";
    let doc = &parse(text, "inline")[0];
    let svc = doc.get("svc").unwrap();
    assert_eq!(svc.get("image").and_then(Node::as_str), Some("mine"));
    assert_eq!(svc.get("restart").and_then(Node::as_str), Some("always"));
    assert_eq!(svc.get("user").and_then(Node::as_str), Some("z"));
    assert!(svc.get("<<").is_none(), "the merge key itself is not a key");
    assert_eq!(doc.get("copy"), doc.get("base"));
}

#[test]
fn a_duplicate_key_takes_the_last_value() {
    let doc = &parse("restart: always\nrestart: unless-stopped\n", "inline")[0];
    assert_eq!(
        doc.get("restart").and_then(Node::as_str),
        Some("unless-stopped")
    );
    assert_eq!(doc.as_map().map(<[_]>::len), Some(1));
}

#[test]
#[should_panic(expected = "core tag `!!bool`")]
fn a_core_tag_on_a_scalar_is_refused() {
    parse("restart: !!bool no\n", "inline");
}

#[test]
#[should_panic(expected = "core tag `!!str`")]
fn a_core_tag_on_a_mapping_is_refused() {
    parse("a: !!str {k: 1}\n", "inline");
}

#[test]
#[should_panic(expected = "core tag `!!set`")]
fn a_core_tag_on_a_sequence_is_refused() {
    parse("a: !!set [x]\n", "inline");
}

#[test]
#[should_panic(expected = "core tag `!!bool`")]
fn a_verbatim_core_tag_is_refused() {
    parse("restart: !<tag:yaml.org,2002:bool> no\n", "inline");
}

#[test]
#[should_panic(expected = "2 YAML documents, expected one")]
fn a_file_of_several_documents_is_refused() {
    let dir = tempfile::TempDir::new().unwrap();
    let file = dir.path().join("two.yml");
    std::fs::write(&file, "- a: 1\n---\n- b: 2\n").unwrap();

    load_one(&file);
}

#[test]
#[should_panic(expected = "found unknown anchor")]
fn an_alias_to_an_undefined_anchor_is_refused() {
    parse("a: *missing\n", "inline");
}

#[test]
fn across_merge_keys_the_later_wins_and_explicit_keys_win_over_both() {
    let text = "a: &a {restart: always, user: a}\n\
                b: &b {restart: unless-stopped}\n\
                s:\n  <<: *a\n  <<: *b\n\
                t:\n  <<: *a\n  <<: *b\n  restart: always\n";
    let doc = &parse(text, "inline")[0];
    let s = doc.get("s").unwrap();
    assert_eq!(
        s.get("restart").and_then(Node::as_str),
        Some("unless-stopped")
    );
    assert_eq!(s.get("user").and_then(Node::as_str), Some("a"));
    assert_eq!(
        doc.get("t").unwrap().get("restart").and_then(Node::as_str),
        Some("always")
    );
}

#[test]
fn within_one_merge_list_the_earlier_mapping_wins() {
    // PyYAML reverses the list before merging (constructor.py, flatten_mapping).
    let text = "a: &a {restart: always}\nb: &b {restart: unless-stopped}\nu:\n  <<: [*a, *b]\n";
    let doc = &parse(text, "inline")[0];
    let u = doc.get("u").unwrap();
    assert_eq!(u.get("restart").and_then(Node::as_str), Some("always"));
}

#[test]
#[should_panic(expected = "merge value must be a mapping")]
fn a_scalar_merge_value_is_refused() {
    parse("s: &s x\nm:\n  <<: *s\n", "inline");
}

#[test]
#[should_panic(expected = "merge list may hold only mappings")]
fn a_merge_list_with_a_scalar_is_refused() {
    parse("a: &a {x: 1}\nm:\n  <<: [*a, 3]\n", "inline");
}

#[test]
fn a_quoted_merge_key_is_an_ordinary_key() {
    let doc = &parse("b: &b {x: 1}\nm:\n  '<<': *b\n", "inline")[0];
    let m = doc.get("m").unwrap();
    assert!(m.get("<<").is_some());
    assert!(m.get("x").is_none());
}

#[test]
fn every_tracked_yaml_file_loads_with_the_suite_loader() {
    // Every file is read even after one fails, so the message names all of them. A parse
    // error panics in the loader; a panic here is the loader refusing the file.
    let files = repo::ls_files(&["*.yml", "*.yaml"]);
    assert!(
        !files.is_empty(),
        "git ls-files found no tracked YAML files"
    );

    let mut failures = Vec::new();
    for rel in &files {
        let text = match std::fs::read_to_string(repo::path(rel)) {
            Ok(text) => text,
            Err(e) => {
                failures.push(format!("{rel}: {e}"));
                continue;
            }
        };
        if let Err(payload) = std::panic::catch_unwind(|| {
            parse(&text, rel);
        }) {
            let reason = payload
                .downcast_ref::<String>()
                .map(String::as_str)
                .or_else(|| payload.downcast_ref::<&str>().copied())
                .unwrap_or("panicked");
            // The loader's parse error already names the file it was given.
            if reason.starts_with(rel.as_str()) {
                failures.push(reason.to_owned());
            } else {
                failures.push(format!("{rel}: {reason}"));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} tracked YAML files do not load with the suite loader:\n{}",
        failures.len(),
        files.len(),
        failures.join("\n")
    );
}
