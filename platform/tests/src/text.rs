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
//! GNU grep's.
//!
//! An empty pattern or literal is found in every line and every text, so it checks nothing: each
//! assertion refuses one, naming the file. A literal is one line; one that holds a newline is
//! refused, because `grep -F` would read it as several literals.

use regex::Regex;

use crate::repo;

/// The file at `rel` (repository-relative), read as text. A missing file panics with its path.
pub fn read(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// `grep -E`: true when some line of `text` matches the ERE `pattern`. A pattern that does not
/// compile panics, naming it.
pub fn any_line(text: &str, pattern: &str) -> bool {
    let re = Regex::new(pattern).unwrap_or_else(|e| panic!("invalid pattern `{pattern}`: {e}"));
    text.split_terminator('\n').any(|line| re.is_match(line))
}

/// Refuse an empty pattern or literal: it is found everywhere, so it checks nothing.
fn require_needle(rel: &str, needle: &str) {
    assert!(
        !needle.is_empty(),
        "{rel}: an empty pattern or literal checks nothing"
    );
}

/// Refuse a literal that holds a newline: `grep -F` reads each of its lines as a separate
/// literal, but `contains` would look for the whole string, so the two would disagree.
fn require_one_line(rel: &str, literal: &str) {
    require_needle(rel, literal);
    assert!(
        !literal.contains('\n'),
        "{rel}: literal `{literal}` spans lines; check each line on its own"
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
    require_one_line(rel, literal);
    assert!(read(rel).contains(literal), "{rel}: `{literal}` is missing");
}

/// `! grep -qF literal file`: the literal must be absent.
pub fn refute_text(rel: &str, literal: &str) {
    require_one_line(rel, literal);
    assert!(
        !read(rel).contains(literal),
        "{rel}: the forbidden `{literal}` is present"
    );
}
