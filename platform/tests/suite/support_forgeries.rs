//! The forged extra-var helpers (`src/forgeries.rs`, AC-951) against the Python they mirror
//! (`platform/tests/forgeries.py`).
//!
//! The expected strings were produced once, while writing this test, by running the Python
//! spellings with the same name, values and directory (`/fixed/dir`), and pasted here. The counter
//! shell's increment is split with `concat!` so this file does not carry the needle
//! test_the_render_counter_has_one_implementation forbids outside forgeries.py (AC-919).

use std::path::Path;

use agent_cloud_tests::forgeries::{self, Spelling};
use serde_json::json;

const DIR: &str = "/fixed/dir";

const PLAIN: &str = r#"{"_probe": {"a": [1, null, true, 1.5, "x y"]}}"#;
const CONTEXT: &str = r#"{"_probe": "{{ (ansible_loop_var is defined or (ansible_play_name | default('')) is search('(?i)refuse')) | ternary(('\"honest\"' | from_json), ('{\"a\": [1, null, true, 1.5, \"x y\"]}' | from_json)) }}"}"#;
const STATEFUL: &str = concat!(
    r#"{"_probe": "{{ (lookup('ansible.builtin.pipe', 'until mkdir /fixed/dir/forgery-renders-_probe.lock 2>/dev/null; do sleep 0.01; done; n=$(cat /fixed/dir/forgery-renders-_probe 2>/dev/null || echo 0); echo $((n+1)) "#,
    r#"> /fixed/dir/forgery-renders-_probe; rmdir /fixed/dir/forgery-renders-_probe.lock; echo $n') | int < 1) | ternary(('\"honest\"' | from_json), ('{\"a\": [1, null, true, 1.5, \"x y\"]}' | from_json)) }}"}"#
);
const HONEST_FOR_2: &str = concat!(
    r#"{"probe": "{{ (lookup('ansible.builtin.pipe', 'until mkdir /fixed/dir/forgery-renders-probe.lock 2>/dev/null; do sleep 0.01; done; n=$(cat /fixed/dir/forgery-renders-probe 2>/dev/null || echo 0); echo $((n+1)) "#,
    r#"> /fixed/dir/forgery-renders-probe; rmdir /fixed/dir/forgery-renders-probe.lock; echo $n') | int < 2) | ternary(('\"honest\"' | from_json), ('\"other\"' | from_json)) }}"}"#
);
const COUNTING: &str = concat!(
    r#"{"probe": "{{ (lookup('ansible.builtin.pipe', 'until mkdir /fixed/dir/forgery-renders-probe.lock 2>/dev/null; do sleep 0.01; done; n=$(cat /fixed/dir/forgery-renders-probe 2>/dev/null || echo 0); echo $((n+1)) "#,
    r#"> /fixed/dir/forgery-renders-probe; rmdir /fixed/dir/forgery-renders-probe.lock; echo $n') | int >= 0) | ternary(('\"seen\"' | from_json), ('\"seen\"' | from_json)) }}"}"#
);
/// json.dumps escapes non-ASCII (a surrogate pair above U+FFFF), control characters and quotes.
const PLAIN_ESCAPED: &str = r#"{"_probe": ["h\u00e9\ud83d\ude00\n\"t\u007f"]}"#;

fn spellings() -> Vec<forgeries::Forgery> {
    forgeries::templated_forgeries(
        "_probe",
        json!("honest"),
        json!({"a": [1, null, true, 1.5, "x y"]}),
    )
}

#[test]
fn each_spelling_matches_the_python_byte_for_byte() {
    let all = spellings();
    let ids: Vec<_> = all.iter().map(|f| f.spelling.id()).collect();
    assert_eq!(ids, ["plain", "context", "stateful"]);
    let dir = Path::new(DIR);
    assert_eq!(all[0].extra_vars(dir), PLAIN);
    assert_eq!(all[1].extra_vars(dir), CONTEXT);
    assert_eq!(all[2].extra_vars(dir), STATEFUL);
}

#[test]
fn honest_for_and_counting_match_the_python() {
    let dir = Path::new(DIR);
    assert_eq!(
        forgeries::honest_for("probe", &json!("honest"), &json!("other"), 2, dir),
        HONEST_FOR_2
    );
    let (extra, counter) = forgeries::counting("probe", &json!("seen"), dir);
    assert_eq!(extra, COUNTING);
    assert_eq!(counter, Path::new("/fixed/dir/forgery-renders-probe"));
}

#[test]
fn the_stateful_spelling_is_honest_for_one() {
    let dir = Path::new(DIR);
    let forged = json!({"a": [1, null, true, 1.5, "x y"]});
    assert_eq!(
        spellings()[2].extra_vars(dir),
        forgeries::honest_for("_probe", &json!("honest"), &forged, 1, dir)
    );
}

#[test]
fn the_json_escapes_as_python_does() {
    let plain = forgeries::templated_forgeries(
        "_probe",
        json!("h"),
        json!(["h\u{e9}\u{1F600}\n\"t\u{7f}"]),
    );
    assert_eq!(plain[0].spelling, Spelling::Plain);
    assert_eq!(plain[0].extra_vars(Path::new(DIR)), PLAIN_ESCAPED);
}

#[test]
fn by_name_keeps_the_plain_spelling_only() {
    let one = forgeries::by_name("_some_internal", json!(1), json!(2));
    assert_eq!(one.len(), 1);
    assert_eq!(one[0].spelling, Spelling::Plain);
}

#[test]
#[should_panic(expected = "by_name is for underscore names")]
fn by_name_refuses_a_name_without_the_underscore() {
    forgeries::by_name("probe", json!(1), json!(2));
}

#[test]
#[should_panic(expected = "referenced by the guard itself")]
fn by_name_refuses_a_name_the_guard_references() {
    forgeries::by_name("_extra_var_guard_nested", json!(1), json!(2));
}

#[test]
#[should_panic]
fn honest_for_refuses_zero_renderings() {
    forgeries::honest_for("probe", &json!("a"), &json!("b"), 0, Path::new(DIR));
}

#[test]
#[should_panic(expected = "no quote or backslash")]
fn a_value_needing_a_quote_is_refused() {
    forgeries::templated_forgeries("_probe", json!("it's"), json!("b"))[1]
        .extra_vars(Path::new(DIR));
}

#[test]
#[should_panic(expected = "no quote or backslash")]
fn a_value_needing_a_backslash_is_refused() {
    // json.dumps writes a quote inside a string as \", so the Jinja string would need escaping.
    forgeries::templated_forgeries("_probe", json!("a\"b"), json!("b"))[1]
        .extra_vars(Path::new(DIR));
}

#[test]
fn the_guard_references_every_name_the_python_finds() {
    // The Python set on b09a5a34. Every one is required, so a walk that drops list contents, or a
    // name pattern that loses `_pass` or `_password`, fails here instead of letting by_name accept
    // a name the guard reads. It does not cover the walk's mapping-key branch: each name that is a
    // key in this guard also appears in a value. S6's shared walk brings its own key test. The
    // check is containment: a name the guard gains needs no edit; one it drops does.
    for name in [
        "_conn_forged",
        "_conn_names",
        "_extra_var_guard_nested",
        "_pass",
        "_password",
    ] {
        assert!(
            forgeries::GUARD_REFERENCED.contains(name),
            "GUARD_REFERENCED lacks {name}: {:?}",
            *forgeries::GUARD_REFERENCED
        );
    }
    assert!(
        forgeries::GUARD_REFERENCED
            .iter()
            .all(|n| n.starts_with('_'))
    );
}
