//! Text assertions for the static tier (plan 24).
//!
//! A static test reads repository files and checks them as text: a line matches, a literal is
//! present. Seven suite modules carry their own `read`, and each port wrote the grep forms inline,
//! so they live here once. `any_line` and the line assertions split the text at each `\n` the way
//! grep does: each line is matched on its own, so `^` and `$` anchor each line, no match crosses a
//! newline, and a `\r` before the `\n` stays part of the line. Patterns are the `regex` crate's
//! syntax, which reads the ERE constructs the ported BATS patterns use; its escapes such as `\d`
//! and `\t` are its own, not GNU grep's.

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

/// `grep -qE pattern file` must succeed.
pub fn assert_line(rel: &str, pattern: &str) {
    assert!(
        any_line(&read(rel), pattern),
        "{rel}: no line matches `{pattern}`"
    );
}

/// `! grep -qE pattern file`: no line of the file may match.
pub fn refute_line(rel: &str, pattern: &str) {
    assert!(
        !any_line(&read(rel), pattern),
        "{rel}: a line matches the forbidden `{pattern}`"
    );
}

/// `grep -qF literal file` must succeed. An empty literal panics: every text contains it.
pub fn assert_text(rel: &str, literal: &str) {
    assert!(
        !literal.is_empty(),
        "{rel}: an empty literal asserts nothing"
    );
    assert!(read(rel).contains(literal), "{rel}: `{literal}` is missing");
}

/// `! grep -qF literal file`: the literal must be absent.
pub fn refute_text(rel: &str, literal: &str) {
    assert!(
        !read(rel).contains(literal),
        "{rel}: the forbidden `{literal}` is present"
    );
}
