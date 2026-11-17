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
//! A pattern every match of which needs a line feed (`\n`, `\x0a`, `[\n]`) is refused like a raw
//! newline: no line ever holds one. `matches_without_line_feed` says what the check reads.
//!
//! An empty pattern or literal is found in every line and every text, so it checks nothing; grep
//! reads one that holds a newline as several needles. Each assertion refuses both, naming the
//! file.
//!
//! `task_block` and `assert_precedes` are the `assert_helpers.bash` forms (`task_block`,
//! `assert_precedes`) over this text: one Ansible task's body, and the order of two first matches.

use regex::Regex;
use regex_syntax::hir::{Hir, HirKind};

use crate::repo;

/// The file at `rel` (repository-relative), read as text. A missing file panics with its path.
pub fn read(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// `grep -E`: true when some line of `text` matches the ERE `pattern`. A pattern that does not
/// compile, or that `compile` refuses, panics, naming it.
#[track_caller]
pub fn any_line(text: &str, pattern: &str) -> bool {
    first_line_matching(text, &compile("", pattern)).is_some()
}

/// The compiled `pattern`, refused where it cannot check what it says: `require_needle`, then a
/// pattern every match of which needs a line feed, which no line holds. Every line pattern
/// compiles here, so the line helpers refuse the same patterns. `origin` prefixes each panic
/// (`"<file>: "`, or `""` for text).
#[track_caller]
fn compile(origin: &str, pattern: &str) -> Regex {
    require_needle(origin, pattern);
    // regex-syntax's default parser is the one `Regex::new` uses (regex-automata's syntax config
    // states its defaults match it). A pattern it cannot parse is left to `Regex::new` to name.
    if let Ok(hir) = regex_syntax::parse(pattern) {
        assert!(
            matches_without_line_feed(&hir),
            "{origin}`{pattern}` cannot match any text free of a line feed, so no line can match it"
        );
    }
    match Regex::new(pattern) {
        Ok(re) => re,
        Err(e) => panic!("{origin}invalid pattern `{pattern}`: {e}"),
    }
}

/// Whether `hir` has a match that needs no line feed, reading every assertion (`^`, `$`, `\b`) as
/// satisfied. A line feed written any way (`\n`, `\x0a`, `[\n]`, a `(?x)` escape spread over
/// blanks) becomes a literal holding U+000A, which this sees, while `[^\n]` is a class of every
/// other character. A pattern that can also match without a line feed (`x|\n`, `\n?`) is kept: it
/// can still match a line. So is one that only an assertion makes need a line feed, or match
/// nothing (`(?m)$\s`, `\b\B`): a refusal here is always right, but not every such pattern is
/// refused.
fn matches_without_line_feed(hir: &Hir) -> bool {
    match hir.kind() {
        HirKind::Empty | HirKind::Look(_) => true,
        HirKind::Literal(literal) => !literal.0.contains(&b'\n'),
        // A class of one character, `[\n]` included, is already a literal (regex-syntax
        // `Hir::class`), so a class left here matches some character other than a line feed
        // unless it is empty.
        HirKind::Class(class) => !class.is_empty(),
        HirKind::Repetition(rep) => rep.min == 0 || matches_without_line_feed(&rep.sub),
        HirKind::Capture(capture) => matches_without_line_feed(&capture.sub),
        HirKind::Concat(subs) => subs.iter().all(matches_without_line_feed),
        HirKind::Alternation(subs) => subs.iter().any(matches_without_line_feed),
    }
}

/// Refuse a pattern or literal grep would read differently: an empty one is found everywhere, and
/// grep reads each line of one that holds a newline as a separate needle, which neither `any_line`
/// nor `contains` does. `origin` prefixes each panic, as in `compile`.
#[track_caller]
fn require_needle(origin: &str, needle: &str) {
    assert!(
        !needle.is_empty(),
        "{origin}an empty pattern or literal checks nothing"
    );
    assert!(
        !needle.contains('\n'),
        "{origin}`{needle}` holds a newline; check each of its lines on its own"
    );
}

/// `grep -qE pattern file` must succeed.
#[track_caller]
pub fn assert_line(rel: &str, pattern: &str) {
    let re = compile(&format!("{rel}: "), pattern);
    assert!(
        first_line_matching(&read(rel), &re).is_some(),
        "{rel}: no line matches `{pattern}`"
    );
}

/// `! grep -qE pattern file`: no line of the file may match.
#[track_caller]
pub fn refute_line(rel: &str, pattern: &str) {
    let re = compile(&format!("{rel}: "), pattern);
    assert!(
        first_line_matching(&read(rel), &re).is_none(),
        "{rel}: a line matches the forbidden `{pattern}`"
    );
}

/// `grep -qF literal file` must succeed.
#[track_caller]
pub fn assert_text(rel: &str, literal: &str) {
    require_needle(&format!("{rel}: "), literal);
    assert!(read(rel).contains(literal), "{rel}: `{literal}` is missing");
}

/// `! grep -qF literal file`: the literal must be absent.
#[track_caller]
pub fn refute_text(rel: &str, literal: &str) {
    require_needle(&format!("{rel}: "), literal);
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
    assert!(!name.is_empty(), "an empty task name finds the first task");
    let header = format!("- name: \"{name}");
    let mut lines = text.split_terminator('\n');
    let Some(start) = lines.by_ref().find(|line| line.contains(header.as_str())) else {
        panic!("no task named \"{name}\"");
    };
    let indent = dash_column(start);
    lines
        .take_while(|line| !(is_task_header(line) && dash_column(line) <= indent))
        .collect::<Vec<_>>()
        .join("\n")
}

/// A task header line at any indent: the BATS `^[[:space:]]*- name:`, with the same six blanks.
fn is_task_header(line: &str) -> bool {
    line.trim_start_matches([' ', '\t', '\n', '\x0B', '\x0C', '\r'])
        .starts_with("- name:")
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
    let line_a = first_line(text, a);
    let line_b = first_line(text, b);
    assert!(
        line_a < line_b,
        "`{a}` (line {line_a}) is not before `{b}` (line {line_b})"
    );
}

/// 1-based number of the first line of `text` that `pattern` matches; panics, naming it, when none
/// does.
#[track_caller]
fn first_line(text: &str, pattern: &str) -> usize {
    let Some(line) = first_line_matching(text, &compile("", pattern)) else {
        panic!("no line matches `{pattern}`");
    };
    line
}

/// 1-based number of the first line of `text` that `re` matches.
fn first_line_matching(text: &str, re: &Regex) -> Option<usize> {
    text.split_terminator('\n')
        .position(|line| re.is_match(line))
        .map(|i| i + 1)
}
