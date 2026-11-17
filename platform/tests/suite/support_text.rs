//! The shared text assertions (`src/text.rs`) that the static ports use (plan 24).
//!
//! Each helper replaces one grep form: `grep -qE`, `! grep -qE`, `grep -qF`, `! grep -qF`. These
//! tests pin what the ports rely on: grep's line semantics, a failure that names the file and the
//! pattern, a refusal of an invalid pattern. The fixture is `platform/tests/Cargo.toml`, a tracked
//! file whose `name` line these tests read.

use agent_cloud_tests::text::{
    any_line, assert_line, assert_precedes, assert_text, read, refute_line, refute_text, task_block,
};

/// The tracked fixture: its `name` line is `name = "agent-cloud-tests"`.
const CARGO: &str = "platform/tests/Cargo.toml";

/// Two tasks at the top level, one of them with a block whose child task is one level deeper, then
/// a last task at the top level.
const PLAY: &str = r#"- name: "Compute the declared rules"
  ansible.builtin.set_fact:
    rules: []
- name: "Allow SSH (22/tcp) from each admin CIDR"
  ansible.builtin.ufw:
    rule: allow
  block:
    - name: "Nested child"
      ansible.builtin.debug:
        msg: child
- name: "Last task"
  ansible.builtin.debug:
    msg: last
"#;

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
    // `.` does not match `\n`, and `\n` itself is never inside a line: grep reads the same way.
    assert!(!any_line("ab\ncd", "b.c"));
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
#[should_panic(expected = "platform/tests/Cargo.toml: an empty pattern or literal checks nothing")]
fn assert_text_refuses_an_empty_literal() {
    assert_text(CARGO, "");
}

#[test]
#[should_panic(expected = "platform/tests/Cargo.toml: an empty pattern or literal checks nothing")]
fn assert_line_refuses_an_empty_pattern() {
    // An empty regex matches every line, so it would pass on any non-empty file.
    assert_line(CARGO, "");
}

#[test]
#[should_panic(expected = "platform/tests/Cargo.toml: an empty pattern or literal checks nothing")]
fn refute_text_refuses_an_empty_literal() {
    // Without the guard it would fail as "the forbidden `` is present", naming no real finding.
    refute_text(CARGO, "");
}

#[test]
#[should_panic(
    expected = "platform/tests/Cargo.toml: `[package]\nname = \"not-a-crate\"` holds a newline; check each of its lines on its own"
)]
fn refute_text_refuses_a_literal_that_spans_lines() {
    // As one string it is absent, so the refute would pass even when each line is present.
    refute_text(CARGO, "[package]\nname = \"not-a-crate\"");
}

#[test]
#[should_panic(
    expected = "platform/tests/Cargo.toml: `zzz\nname = \"agent-cloud-tests\"` holds a newline; check each of its lines on its own"
)]
fn refute_line_refuses_a_pattern_that_holds_a_newline() {
    // grep reads it as two patterns and would match the `name` line; one regex never matches a
    // line, so the refute would pass.
    refute_line(CARGO, "zzz\nname = \"agent-cloud-tests\"");
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
#[should_panic(expected = r#"platform/tests/Cargo.toml: `name = "agent-cloud-test."` is missing"#)]
fn assert_text_matches_the_literal_not_a_pattern() {
    // As a pattern, `test.` would match `tests`; as a literal the dot must be a dot, so it is
    // missing.
    assert_text(CARGO, r#"name = "agent-cloud-test.""#);
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

#[test]
fn task_block_is_the_body_after_the_header_without_the_header() {
    assert_eq!(
        task_block(PLAY, "Compute the declared rules"),
        "  ansible.builtin.set_fact:\n    rules: []"
    );
}

#[test]
fn task_block_stops_at_a_shallower_task() {
    // The child sits at column 5; the top-level `Last task` at column 1 is shallower and ends it.
    assert_eq!(
        task_block(PLAY, "Nested child"),
        "      ansible.builtin.debug:\n        msg: child"
    );
}

#[test]
fn task_block_keeps_a_deeper_task_inside_its_body() {
    assert_eq!(
        task_block(PLAY, "Allow SSH (22/tcp) from each admin CIDR"),
        "  ansible.builtin.ufw:\n    rule: allow\n  block:\n    - name: \"Nested child\"\n      ansible.builtin.debug:\n        msg: child"
    );
}

#[test]
fn task_block_takes_the_first_task_a_prefix_matches() {
    let text = "- name: \"Foo bar\"\n  first: 1\n- name: \"Foo\"\n  second: 2\n";
    assert_eq!(task_block(text, "Foo"), "  first: 1");
}

#[test]
fn task_block_matches_a_name_with_regex_characters_as_a_literal() {
    // As a regex, `(22/tcp)` is a group that also matches `22/tcp`: the first task would be taken.
    let text = "- name: \"Allow SSH 22/tcp from each admin CIDR\"\n  other: 1\n- name: \"Allow SSH (22/tcp) from each admin CIDR\"\n  real: 2\n";
    assert_eq!(
        task_block(text, "Allow SSH (22/tcp) from each admin CIDR"),
        "  real: 2"
    );
}

#[test]
fn task_block_keeps_a_carriage_return_on_its_body_line() {
    let text = "- name: \"X\"\r\n  body: 1\r\n- name: \"Y\"\r\n";
    assert_eq!(task_block(text, "X"), "  body: 1\r");
}

#[test]
#[should_panic(expected = "no task named \"Absent\"")]
fn task_block_panics_on_a_missing_task() {
    // An empty body would pass every refute on it, so the absence is the failure.
    task_block(PLAY, "Absent");
}

#[test]
fn assert_precedes_passes_when_the_first_pattern_comes_first() {
    assert_precedes("alpha\nbeta\n", "^alpha", "^beta");
}

#[test]
#[should_panic(expected = "`^beta` (line 2) is not before `^alpha` (line 1)")]
fn assert_precedes_panics_when_reversed_naming_both_line_numbers() {
    assert_precedes("alpha\nbeta\n", "^beta", "^alpha");
}

#[test]
#[should_panic(expected = "`both` (line 1) is not before `both` (line 1)")]
fn assert_precedes_panics_when_one_line_matches_both_patterns() {
    assert_precedes("both\n", "both", "both");
}

#[test]
#[should_panic(expected = "no line matches `^absent`")]
fn assert_precedes_names_a_missing_first_pattern() {
    assert_precedes("alpha\n", "^absent", "^alpha");
}

#[test]
#[should_panic(expected = "no line matches `^absent`")]
fn assert_precedes_names_a_missing_second_pattern() {
    assert_precedes("alpha\n", "^alpha", "^absent");
}

#[test]
#[should_panic(expected = "platform/tests/Cargo.toml: `\\n` holds a line feed escape")]
fn refute_line_refuses_a_line_feed_escape_naming_the_file() {
    // Compiled, `\n` matches no line, so the refute would pass whatever the file held.
    refute_line(CARGO, r"\n");
}

#[test]
fn escapes_that_do_not_write_a_line_feed_are_allowed() {
    // An escaped backslash then `n` is the two characters `\n` in the text, not an escape.
    assert!(any_line(r"a\nb", r"\\n"));
    assert!(any_line("a b", r"a\sb"));
    assert!(any_line("a\u{b}b", r"\x0b"));
    assert!(any_line("a\u{a0}b", r"\x{a0}"));
    // Inside a class an escape is one member: `[^\n]` is any character but a line feed.
    assert!(any_line("ab", r"a[^\n]"));
    assert!(any_line("a\tb", r"a[\n\t]b"));
    assert!(any_line("a]", r"a[]\n]"));
    assert!(any_line("a b", r"a[[:space:]\n]b"));
    // A bare `{` does not compile; escaped, it matches a literal brace.
    assert!(any_line("mv {{ x }}", r"mv \{\{"));
}

#[test]
fn any_line_refuses_every_line_feed_escape_outside_a_class() {
    // Compiled, each matches no line of the text, so the refusal is the difference between a check
    // and a silent pass. The last two follow a class that has closed.
    for pattern in [
        r"b\nc",
        r"\x0a",
        r"\x0A",
        r"\x{a}",
        r"\x{000A}",
        r"\u000a",
        r"\u{A}",
        r"\U0000000A",
        r"\U{a}",
        r"[a]\n",
        r"[]a]\x0a",
    ] {
        let panic = std::panic::catch_unwind(|| any_line("ab\ncd", pattern))
            .expect_err(&format!("`{pattern}` was not refused"));
        let message = panic.downcast_ref::<String>().map_or("", String::as_str);
        assert!(
            message.contains("holds a line feed escape"),
            "`{pattern}`: {message}"
        );
    }
}

#[test]
#[should_panic(expected = "an empty task name finds the first task")]
fn task_block_refuses_an_empty_name() {
    // `- name: "` is in every header, so an empty name would silently take the first task.
    task_block(PLAY, "");
}
