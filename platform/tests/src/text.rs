//! Text assertions for the static tier (plan 24).
//!
//! A static test reads repository files and checks them as text: a line matches, a literal is
//! present. Several suite modules carry their own `read`, and each port wrote the grep forms
//! inline, so they live here once.
//!
//! `any_line` and the line assertions read lines as grep does: the text is split at each `\n`,
//! each line is matched on its own (so `^` and `$` anchor each line), and a `\r` before the `\n`
//! stays part of its line. As with grep, no match spans two lines: a pattern that only a newline
//! would satisfy matches nothing. Patterns are the `regex` crate's syntax, which reads the ERE
//! constructs the ported BATS patterns use; its escapes such as `\d` and `\t` are its own, not
//! GNU grep's. A bare `{` is a compile error there (`mv {{`), so a ported pattern writes `\{`.
//! A line-feed escape (`\n`, `\x0a`) is refused like a raw newline: no line ever holds one.
//!
//! An empty pattern or literal is found in every line and every text, so it checks nothing; grep
//! reads one that holds a newline as several needles. Each assertion refuses both, naming the
//! file.
//!
//! `task_block` and `assert_precedes` are the `assert_helpers.bash` forms (`task_block`,
//! `assert_precedes`) over this text: one Ansible task's body, and the order of two first matches.

use regex::Regex;

use crate::repo;

/// The file at `rel` (repository-relative), read as text. A missing file panics with its path.
pub fn read(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// `grep -E`: true when some line of `text` matches the ERE `pattern`. A pattern that does not
/// compile, or that `compile` refuses, panics, naming it.
#[track_caller]
pub fn any_line(text: &str, pattern: &str) -> bool {
    let re = compile(pattern);
    text.split_terminator('\n').any(|line| re.is_match(line))
}

/// The compiled `pattern`, refused where it cannot check what it says. An empty one is found in
/// every line. A raw newline is several needles to grep, and no line holds one for a regex to
/// match across. A line-feed escape (`\n`, `\x0a`) matches no line, for the same reason. Every
/// line pattern compiles here, so `any_line` and `assert_precedes` refuse the same patterns.
#[track_caller]
fn compile(pattern: &str) -> Regex {
    assert!(!pattern.is_empty(), "an empty pattern checks nothing");
    assert!(
        !pattern.contains('\n'),
        "pattern `{pattern}` holds a newline; check each of its lines on its own"
    );
    assert!(
        !writes_line_feed(pattern),
        "pattern `{pattern}` holds a line feed escape; a line never contains one, so it can only match across lines"
    );
    match Regex::new(pattern) {
        Ok(re) => re,
        Err(e) => panic!("invalid pattern `{pattern}`: {e}"),
    }
}

/// Whether an escape in `pattern` writes U+000A. A backslash takes the byte after it with it, so
/// `\\n` (an escaped backslash, then `n`) is not an escape. Every escape character is ASCII, so
/// the walk goes by byte.
fn writes_line_feed(pattern: &str) -> bool {
    let bytes = pattern.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'\\' {
            match bytes.get(i + 1) {
                Some(b'n') => return true,
                Some(&kind @ (b'x' | b'u' | b'U'))
                    if hex_escape_value(kind, &bytes[i + 2..]) == Some(0x0A) =>
                {
                    return true;
                }
                _ => {}
            }
            i += 2;
        } else {
            i += 1;
        }
    }
    false
}

/// The code point of the hex escape whose letter is `kind` (`x`, `u` or `U`) and whose digits
/// follow in `rest`, when the escape is well formed: exactly two, four or eight hex digits, or
/// any number of them between braces (`\x{a}`). None otherwise: `Regex::new` rejects a malformed
/// one, so the scan leaves it to it.
fn hex_escape_value(kind: u8, rest: &[u8]) -> Option<u32> {
    let digits = if let Some(braced) = rest.strip_prefix(b"{") {
        &braced[..braced.iter().position(|&b| b == b'}')?]
    } else {
        let width = match kind {
            b'x' => 2,
            b'u' => 4,
            _ => 8,
        };
        rest.get(..width)?
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_hexdigit) {
        return None;
    }
    u32::from_str_radix(std::str::from_utf8(digits).ok()?, 16).ok()
}

/// Refuse a pattern or literal grep would read differently: an empty one is found everywhere, and
/// grep reads each line of one that holds a newline as a separate needle, which neither `any_line`
/// nor `contains` does.
fn require_needle(rel: &str, needle: &str) {
    assert!(
        !needle.is_empty(),
        "{rel}: an empty pattern or literal checks nothing"
    );
    assert!(
        !needle.contains('\n'),
        "{rel}: `{needle}` holds a newline; check each of its lines on its own"
    );
}

/// `grep -qE pattern file` must succeed.
pub fn assert_line(rel: &str, pattern: &str) {
    require_needle(rel, pattern);
    assert!(
        any_line(&read(rel), pattern),
        "{rel}: no line matches `{pattern}`"
    );
}

/// `! grep -qE pattern file`: no line of the file may match.
pub fn refute_line(rel: &str, pattern: &str) {
    require_needle(rel, pattern);
    assert!(
        !any_line(&read(rel), pattern),
        "{rel}: a line matches the forbidden `{pattern}`"
    );
}

/// `grep -qF literal file` must succeed.
pub fn assert_text(rel: &str, literal: &str) {
    require_needle(rel, literal);
    assert!(read(rel).contains(literal), "{rel}: `{literal}` is missing");
}

/// `! grep -qF literal file`: the literal must be absent.
pub fn refute_text(rel: &str, literal: &str) {
    require_needle(rel, literal);
    assert!(
        !read(rel).contains(literal),
        "{rel}: the forbidden `{literal}` is present"
    );
}

/// One Ansible task's body, the text form of `assert_helpers.bash`'s `task_block`: the lines after
/// the first line that holds `- name: "<name>`, up to the next `- name:` line whose dash sits at or
/// left of the header's. The name matches as a literal, so its regex characters are plain, and as
/// a prefix: `Foo` finds `Foo bar` when that comes first. Deeper `- name:` lines (a block's
/// children) stay in. Lines are split as `any_line` splits them, so a `\r` stays on its line; the
/// body is joined with `\n`, with no final newline. Panics when no task holds the name: an empty
/// body would pass every refute on it.
#[track_caller]
pub fn task_block(text: &str, name: &str) -> String {
    let header = format!("- name: \"{name}");
    let mut lines = text.split_terminator('\n');
    let Some(start) = lines.by_ref().find(|line| line.contains(header.as_str())) else {
        panic!("no task named \"{name}\"");
    };
    let indent = dash_column(start);
    let next_task = Regex::new(r"^[[:space:]]*- name:").expect("a fixed pattern");
    lines
        .take_while(|line| !(next_task.is_match(line) && dash_column(line) <= indent))
        .collect::<Vec<_>>()
        .join("\n")
}

/// 1-based column of the first `-` in `line`, 0 when it has none (awk's `index($0, "-")`).
fn dash_column(line: &str) -> usize {
    line.find('-').map_or(0, |i| i + 1)
}

/// The first line matching `a` comes strictly before the first line matching `b`: the text form of
/// `assert_helpers.bash`'s `assert_precedes`, over the same patterns and line split as `any_line`.
/// A pattern no line matches panics, naming it. So does an order that is not strictly increasing,
/// which includes one line matching both patterns: the panic gives both line numbers.
#[track_caller]
pub fn assert_precedes(text: &str, a: &str, b: &str) {
    let re_a = compile(a);
    let re_b = compile(b);
    let Some(line_a) = first_line_matching(text, &re_a) else {
        panic!("no line matches `{a}`");
    };
    let Some(line_b) = first_line_matching(text, &re_b) else {
        panic!("no line matches `{b}`");
    };
    assert!(
        line_a < line_b,
        "`{a}` (line {line_a}) is not before `{b}` (line {line_b})"
    );
}

/// 1-based number of the first line of `text` that `re` matches.
fn first_line_matching(text: &str, re: &Regex) -> Option<usize> {
    text.split_terminator('\n')
        .position(|line| re.is_match(line))
        .map(|i| i + 1)
}
