//! The shared text assertions (`src/text.rs`) that the static ports use (plan 24).
//!
//! Each helper replaces one grep form: `grep -qE`, `! grep -qE`, `grep -qF`, `! grep -qF`. These
//! tests pin what the ports rely on: grep's line semantics, a failure that names the file and the
//! pattern, a refusal of an invalid pattern. The fixture is `platform/tests/Cargo.toml`, a tracked
//! file whose `name` line these tests read.

use agent_cloud_tests::text::{any_line, assert_line, assert_text, read, refute_line, refute_text};

/// The tracked fixture: its `name` line is `name = "agent-cloud-tests"`.
const CARGO: &str = "platform/tests/Cargo.toml";

#[test]
fn any_line_matches_a_line_of_the_text() {
    assert!(any_line("alpha\nbeta", "^beta$"));
    assert!(any_line("alpha\nbeta", "alp"));
}

#[test]
fn any_line_anchors_each_line_on_its_own() {
    // Whole-text matching without multiline mode would reject both: `$` and `^` would have to
    // sit at the ends of the whole text.
    assert!(any_line("ab\ncd", "^cd$"));
    assert!(any_line("ab\ncd", "^ab$"));
}

#[test]
fn any_line_never_matches_across_a_newline() {
    // `b.c` is false under whole-text matching too (`.` does not match `\n`), so it proves
    // nothing alone. `b\nc` is the case that does: whole-text matching would find the newline.
    assert!(!any_line("ab\ncd", "b.c"));
    assert!(!any_line("ab\ncd", r"b\nc"));
}

#[test]
fn any_line_keeps_a_carriage_return_as_part_of_its_line() {
    // grep splits at `\n` only, so a CRLF file's `\r` is the last character of its line: a
    // forbidden pattern that must consume it still matches.
    assert!(any_line("secret=abcd\r\nnext", "secret=.{5}$"));
}

#[test]
fn any_line_sees_no_line_after_the_final_newline() {
    // A trailing `\n` ends the last line; it does not start an empty one.
    assert!(!any_line("ab\n", "^$"));
    assert!(any_line("ab\n\n", "^$"));
}

#[test]
fn any_line_is_false_when_no_line_matches() {
    assert!(!any_line("ab\ncd", "zz"));
}

#[test]
#[should_panic(expected = "invalid pattern `(`")]
fn any_line_names_a_pattern_that_does_not_compile() {
    any_line("ab", "(");
}

#[test]
fn assert_line_passes_on_a_present_pattern() {
    assert_line(CARGO, r#"^name = "agent-cloud-tests"$"#);
}

#[test]
#[should_panic(expected = r#"platform/tests/Cargo.toml: no line matches `^name = "not-a-crate"$`"#)]
fn assert_line_panics_on_an_absent_pattern_naming_the_file_and_the_pattern() {
    assert_line(CARGO, r#"^name = "not-a-crate"$"#);
}

#[test]
fn refute_line_passes_when_no_line_matches() {
    refute_line(CARGO, r#"^edition = "2015"$"#);
}

#[test]
#[should_panic(
    expected = r#"platform/tests/Cargo.toml: a line matches the forbidden `^name = "agent-cloud-tests"$`"#
)]
fn refute_line_panics_when_a_line_matches() {
    refute_line(CARGO, r#"^name = "agent-cloud-tests"$"#);
}

#[test]
fn assert_text_passes_when_the_literal_is_present() {
    assert_text(CARGO, r#"name = "agent-cloud-tests""#);
}

#[test]
#[should_panic(expected = r#"platform/tests/Cargo.toml: `edition = "2015"` is missing"#)]
fn assert_text_panics_when_the_literal_is_absent() {
    assert_text(CARGO, r#"edition = "2015""#);
}

#[test]
#[should_panic(expected = "platform/tests/Cargo.toml: an empty literal asserts nothing")]
fn assert_text_refuses_an_empty_literal() {
    assert_text(CARGO, "");
}

#[test]
fn refute_text_passes_when_the_literal_is_absent() {
    refute_text(CARGO, r#"edition = "2015""#);
}

#[test]
#[should_panic(
    expected = r#"platform/tests/Cargo.toml: the forbidden `name = "agent-cloud-tests"` is present"#
)]
fn refute_text_panics_when_the_literal_is_present() {
    refute_text(CARGO, r#"name = "agent-cloud-tests""#);
}

#[test]
fn refute_text_matches_the_literal_not_a_pattern() {
    // As a pattern, `test.` would match `tests`; as a literal the dot must be a dot.
    refute_text(CARGO, r#"name = "agent-cloud-test.""#);
}

#[test]
fn read_returns_the_file_as_text() {
    assert!(read(CARGO).starts_with("[package]\n"));
}

#[test]
#[should_panic(expected = "platform/tests/no-such-file.toml: ")]
fn read_panics_on_a_missing_file_with_its_path() {
    read("platform/tests/no-such-file.toml");
}
