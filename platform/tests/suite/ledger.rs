//! Every test the mistakes ledger cites exists (docs/MISTAKES.md 8.2).
//!
//! Ported from `platform/tests/test_ledger_test_citations.py` (plan 24, Phase 3, D9). The
//! Python checker stays until its pre-commit hook moves (Phase 5). Both read the same ledger
//! and the same ratchet; on 2026-11-12 both reported the same unresolved citations on this
//! ledger. No differential test keeps them equal, so a rule change goes into both.
//!
//! Parse rule, as the Python module documents it. Fenced code blocks are dropped. The rest is
//! split into claims: blank-line-separated paragraphs, except that each Markdown table row is
//! its own claim. Inside a claim, every inline code span is classified:
//!
//! 1. FILE: `[dir/]test_<x>.py` or `[dir/]<x>.bats`, optionally followed by `::<name>` or by
//!    `:<line>[-<line>]`. A path containing `/` must exist under the repository root; a bare
//!    name must match some file in the repository. With `::<name>`, the file must define it.
//! 2. NAME: a bare `test_<x>`. If the claim also cites FILE spans, the name must be defined in
//!    one of those files (or be one's stem); otherwise in any `test_*.py` in the repository.
//! 3. BATS NAME: in a claim that cites a `.bats` file, a span of five or more words that starts
//!    with a lowercase letter and holds none of `| $ = ( ) { } [ ] < >` and no word starting
//!    with `-`. It must equal an `@test "..."` name in one of the claim's cited `.bats` files.
//!
//! The one extension (this port only): FILE also matches a Rust test, `[dir/]suite/<name>.rs`,
//! optionally followed by `::<fn>`. A path starting `suite/` is relative to `platform/tests/`;
//! any other path is relative to the repository root. With `::<fn>`, the file must define
//! `fn <fn>` followed by `(` or `<`, at the start of a line after any attributes, `pub` or
//! `pub(..)`, or `async`; a fn inside a comment (nested ones included) or a string or char
//! literal does not count. A Rust citation is checked for
//! existence and fn name only. It does not feed the
//! NAME and BATS NAME pools, so both checkers search the same files for every claim and differ
//! only on Rust citations, which the Python checker does not read.
//!
//! A citation that cannot resolve because its test was deleted or renamed after the entry was
//! written stays in the append-only ledger. `platform/tests/ledger_citation_ratchet.txt` lists
//! it as `<entry> <citation> # <reason>`. The comparison is a multiset: one ratchet line covers
//! one occurrence. The list may only shrink. A Rust citation cannot be ratcheted while the Python
//! checker runs: it does not read `.rs` spans, so it reads such a ratchet line as stale. Until
//! the Python checker goes (Phase 5), a cited Rust test that is renamed gets a dated note in the
//! ledger entry naming its new name, as 10.15's "Enforcement moved" note does.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use agent_cloud_tests::repo;
use regex::Regex;

const LEDGER: &str = "docs/MISTAKES.md";
const RATCHET: &str = "platform/tests/ledger_citation_ratchet.txt";
const SKIP_DIRS: [&str; 4] = [".git", "node_modules", ".venv", "__pycache__"];
const CODE_CHARS: &str = "|$=(){}[]<>";

static FENCE: LazyLock<Regex> = LazyLock::new(|| re(r"(?ms)^[ \t]*```.*?^[ \t]*```[^\n]*$"));
static HEADING: LazyLock<Regex> = LazyLock::new(|| re(r"^#{1,6}\s+(\S*)"));
static PARAGRAPH_BREAK: LazyLock<Regex> = LazyLock::new(|| re(r"\n[ \t]*\n"));
static SPAN: LazyLock<Regex> = LazyLock::new(|| re(r"`([^`]+)`"));
static FILE: LazyLock<Regex> = LazyLock::new(|| {
    re(r"^(?P<path>(?:[\w.-]+/)*(?:test_\w+\.py|[\w-]+\.bats))(?:::(?P<name>\w+)|:\d+(?:-\d+)?)?$")
});
static FILE_RS: LazyLock<Regex> =
    LazyLock::new(|| re(r"^(?P<path>(?:[\w.-]+/)*suite/[\w-]+\.rs)(?:::(?P<name>\w+))?$"));
static NAME: LazyLock<Regex> = LazyLock::new(|| re(r"^test_\w+$"));
static TEST_FILE: LazyLock<Regex> = LazyLock::new(|| re(r"^(?:test_\w+\.py|[\w-]+\.bats)$"));
static BATS_TEST: LazyLock<Regex> =
    LazyLock::new(|| re(r#"(?m)^\s*@test\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)')"#));

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("valid regex")
}

fn read_repo(rel: &str) -> String {
    fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// Paragraphs of `text` as `(entry, claim)`. `entry` is the first word of the nearest heading
/// above, trailing dot dropped: `8.2` for `### 8.2 Invented ...`.
fn claims(text: &str) -> Vec<(String, String)> {
    let stripped = FENCE.replace_all(text, "");
    let mut out = Vec::new();
    let mut entry = String::new();
    for para in PARAGRAPH_BREAK.split(&stripped) {
        let mut rest: Vec<&str> = Vec::new();
        // The sentinel heading flushes the last paragraph.
        for line in para.lines().chain(["# "]) {
            if let Some(caps) = HEADING.captures(line) {
                if rest.iter().any(|r| !r.trim().is_empty()) {
                    out.push((entry.clone(), rest.join("\n")));
                }
                rest.clear();
                let word = caps[1].trim_end_matches('.');
                if !word.is_empty() {
                    entry = word.to_owned();
                }
            } else if line.trim_start().starts_with('|') {
                out.push((entry.clone(), line.to_owned()));
            } else {
                rest.push(line);
            }
        }
    }
    out
}

/// Every inline code span, whitespace collapsed.
fn spans(claim: &str) -> Vec<String> {
    SPAN.captures_iter(claim)
        .map(|c| c[1].split_whitespace().collect::<Vec<_>>().join(" "))
        .collect()
}

struct FileSpan {
    path: String,
    name: Option<String>,
}

fn file_span(span: &str) -> Option<FileSpan> {
    [&*FILE, &*FILE_RS].into_iter().find_map(|pattern| {
        let caps = pattern.captures(span)?;
        Some(FileSpan {
            path: caps["path"].to_owned(),
            name: caps.name("name").map(|m| m.as_str().to_owned()),
        })
    })
}

fn looks_like_bats_name(span: &str) -> bool {
    let words: Vec<&str> = span.split(' ').collect();
    words.len() >= 5
        && span.chars().next().is_some_and(char::is_lowercase)
        && !span.chars().any(|c| CODE_CHARS.contains(c))
        && !words.iter().any(|w| w.starts_with('-'))
}

/// Every file under `dir` by its name. Directory symlinks are not followed, as `os.walk`
/// does not follow them; file symlinks are listed, and `is_file` decides later.
fn walk(dir: &Path, by_name: &mut BTreeMap<String, Vec<PathBuf>>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        if kind.is_dir() {
            let dir_name = entry.file_name().to_string_lossy().into_owned();
            if !SKIP_DIRS.contains(&dir_name.as_str()) {
                walk(&path, by_name);
            }
        } else if kind.is_symlink() && path.is_dir() {
            continue;
        } else if let Some(name) = path.file_name() {
            by_name
                .entry(name.to_string_lossy().into_owned())
                .or_default()
                .push(path);
        }
    }
}

fn is_py(path: &Path) -> bool {
    path.extension().and_then(|e| e.to_str()) == Some("py")
}

fn read_lossy(path: &Path) -> String {
    String::from_utf8_lossy(&fs::read(path).expect("readable file")).into_owned()
}

fn defines(path: &Path, name: &str) -> bool {
    let text = read_lossy(path);
    // A literal search first: the name must appear for any pattern to match, and compiling a
    // regex for each (name, file) pair cost seconds per run.
    if !text.contains(name) {
        return false;
    }
    let name = regex::escape(name);
    if path.extension().and_then(|e| e.to_str()) == Some("rs") {
        // Line-anchored like the Python rule; attributes and `pub`/`async` may precede it.
        // Comments and literals are blanked first, so a fn inside one does not count.
        let code = blank_comments_and_literals(&text);
        let pattern = format!(
            r"(?m)^[ \t]*(?:#\[[^\]]*\][ \t]*)*(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?fn[ \t]+{name}[ \t]*[<(]"
        );
        re(&pattern).is_match(&code)
    } else {
        re(&format!(r"(?m)^\s*(?:async\s+)?(?:def|class)\s+{name}\b")).is_match(&text)
    }
}

/// `src` with every comment (nested `/* */` included), string literal (plain, byte, C, raw) and char
/// literal replaced by spaces, newlines kept so line anchors still hold. A lifetime (`'a`) is
/// code: a `'` opens a char literal only when its closing `'` follows the one char or escape.
fn blank_comments_and_literals(src: &str) -> String {
    let c: Vec<char> = src.chars().collect();
    let mut out = String::with_capacity(src.len());
    let blank = |out: &mut String, chars: &[char]| {
        out.extend(chars.iter().map(|&ch| if ch == '\n' { '\n' } else { ' ' }));
    };
    let ident = |i: usize| i > 0 && (c[i - 1].is_alphanumeric() || c[i - 1] == '_');
    let mut i = 0;
    while i < c.len() {
        let start = i;
        if c[i] == '/' && c.get(i + 1) == Some(&'/') {
            while i < c.len() && c[i] != '\n' {
                i += 1;
            }
        } else if c[i] == '/' && c.get(i + 1) == Some(&'*') {
            let mut depth = 0;
            while i < c.len() {
                if c[i] == '/' && c.get(i + 1) == Some(&'*') {
                    depth += 1;
                    i += 2;
                } else if c[i] == '*' && c.get(i + 1) == Some(&'/') {
                    depth -= 1;
                    i += 2;
                    if depth == 0 {
                        break;
                    }
                } else {
                    i += 1;
                }
            }
        } else if c[i] == 'r'
            && (!ident(i) || (matches!(c[i - 1], 'b' | 'c') && !ident(i - 1)))
            && matches!(c.get(i + 1), Some('"' | '#'))
        {
            let hashes = c[i + 1..].iter().take_while(|&&ch| ch == '#').count();
            if c.get(i + 1 + hashes) != Some(&'"') {
                out.push(c[i]);
                i += 1;
                continue;
            }
            i += hashes + 2;
            let closes = |i: usize| {
                c[i] == '"'
                    && c.get(i + 1..i + 1 + hashes)
                        .is_some_and(|h| h.iter().all(|&ch| ch == '#'))
            };
            while i < c.len() && !closes(i) {
                i += 1;
            }
            i = (i + 1 + hashes).min(c.len());
        } else if c[i] == '"' {
            i += 1;
            while i < c.len() && c[i] != '"' {
                i += if c[i] == '\\' { 2 } else { 1 };
            }
            i = (i + 1).min(c.len());
        } else if c[i] == '\'' {
            let close = if c.get(i + 1) == Some(&'\\') {
                // The escaped char is at i + 2, so the closing quote is searched from i + 3.
                c.get(i + 3..)
                    .and_then(|rest| rest.iter().position(|&ch| ch == '\''))
                    .map(|p| i + 3 + p)
            } else {
                Some(i + 2).filter(|&j| c.get(j) == Some(&'\''))
            };
            match close {
                Some(j) => i = j + 1,
                None => {
                    out.push(c[i]);
                    i += 1;
                    continue;
                }
            }
        } else {
            out.push(c[i]);
            i += 1;
            continue;
        }
        blank(&mut out, &c[start..i.min(c.len())]);
    }
    out
}

fn bats_names(path: &Path) -> BTreeSet<String> {
    let text = read_lossy(path);
    BATS_TEST
        .captures_iter(&text)
        .map(|c| {
            let raw = c.get(1).or_else(|| c.get(2)).map_or("", |m| m.as_str());
            raw.split_whitespace().collect::<Vec<_>>().join(" ")
        })
        .collect()
}

/// Every test citation in `text` that does not resolve against the tree at `root`, as
/// `<entry> <citation>`.
fn unresolved(text: &str, root: &Path) -> Vec<String> {
    let mut by_name = BTreeMap::new();
    walk(root, &mut by_name);
    let test_files: Vec<PathBuf> = by_name
        .iter()
        .filter(|(name, _)| TEST_FILE.is_match(name))
        .flat_map(|(_, paths)| paths.iter().cloned())
        .collect();

    let mut bad: Vec<String> = Vec::new();
    for (entry, claim) in claims(text) {
        let mut claim_bad: Vec<String> = Vec::new();
        let ss = spans(&claim);
        // The .py and .bats files this claim cites: the pool for NAME and BATS NAME.
        let mut cited: Vec<PathBuf> = Vec::new();
        for s in &ss {
            let Some(file) = file_span(s) else {
                continue;
            };
            let is_rs = file.path.ends_with(".rs");
            let found: Vec<PathBuf> = if is_rs {
                let base = if file.path.starts_with("suite/") {
                    root.join("platform/tests")
                } else {
                    root.to_path_buf()
                };
                vec![base.join(&file.path)]
            } else if file.path.contains('/') {
                vec![root.join(&file.path)]
            } else {
                by_name.get(&file.path).cloned().unwrap_or_default()
            };
            let found: Vec<PathBuf> = found.into_iter().filter(|p| p.is_file()).collect();
            if found.is_empty() {
                claim_bad.push(s.clone());
                continue;
            }
            if !is_rs {
                cited.extend(found.iter().cloned());
            }
            if let Some(name) = &file.name
                && !found.iter().any(|p| defines(p, name))
            {
                claim_bad.push(s.clone());
            }
        }

        let cited_bats: Vec<&PathBuf> = cited
            .iter()
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("bats"))
            .collect();
        for s in &ss {
            if NAME.is_match(s) {
                let pool = if cited.is_empty() {
                    &test_files
                } else {
                    &cited
                };
                let ok = pool.iter().any(|p| {
                    p.file_stem().and_then(|x| x.to_str()) == Some(s.as_str())
                        || (is_py(p) && defines(p, s))
                });
                if !ok {
                    claim_bad.push(s.clone());
                }
            } else if !cited_bats.is_empty()
                && file_span(s).is_none()
                && looks_like_bats_name(s)
                && !cited_bats
                    .iter()
                    .any(|p| bats_names(p.as_path()).contains(s))
            {
                claim_bad.push(s.clone());
            }
        }

        bad.extend(claim_bad.into_iter().map(|s| format!("{entry} {s}")));
    }
    bad
}

/// `(<entry> <citation>, reason)` per line; one line per occurrence, so duplicates count.
fn parse_ratchet(text: &str) -> Vec<(String, String)> {
    text.lines()
        .filter(|line| !line.trim().is_empty() && !line.trim_start().starts_with('#'))
        .map(|line| {
            let (key, reason) = line.split_once(" # ").unwrap_or((line, ""));
            (
                key.split_whitespace().collect::<Vec<_>>().join(" "),
                reason.trim().to_owned(),
            )
        })
        .collect()
}

fn counts<'a>(items: impl Iterator<Item = &'a str>) -> BTreeMap<&'a str, usize> {
    let mut map = BTreeMap::new();
    for item in items {
        *map.entry(item).or_insert(0) += 1;
    }
    map
}

/// Occurrences in `a` beyond those in `b`, each repeated, in sorted order.
fn surplus(a: &BTreeMap<&str, usize>, b: &BTreeMap<&str, usize>) -> Vec<String> {
    a.iter()
        .flat_map(|(key, &n)| {
            let covered = b.get(key).copied().unwrap_or(0);
            std::iter::repeat_n(key.to_string(), n.saturating_sub(covered))
        })
        .collect()
}

/// `(new, stale)` as multisets: a second occurrence of a ratcheted citation is new, not covered.
fn ratchet_diff(found: &[String], ratchet: &[(String, String)]) -> (Vec<String>, Vec<String>) {
    let have = counts(found.iter().map(String::as_str));
    let allowed = counts(ratchet.iter().map(|(key, _)| key.as_str()));
    (surplus(&have, &allowed), surplus(&allowed, &have))
}

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

fn fixture() -> tempfile::TempDir {
    let dir = tempfile::tempdir().expect("tempdir");
    fs::create_dir_all(dir.path().join("platform/tests")).expect("fixture tree");
    dir
}

#[test]
fn every_cited_test_exists() {
    // Arrange
    let ledger = read_repo(LEDGER);
    let ratchet = parse_ratchet(&read_repo(RATCHET));

    // Act
    let (new, _) = ratchet_diff(&unresolved(&ledger, repo::root()), &ratchet);

    // Assert
    assert!(
        new.is_empty(),
        "docs/MISTAKES.md cites tests that do not exist (8.2). Fix the citation, or, for a \
         test deleted or renamed since the entry was written, ratchet it with a reason: {new:?}"
    );
}

#[test]
fn ratchet_entries_carry_a_reason_and_still_fail() {
    // Arrange
    let ratchet = parse_ratchet(&read_repo(RATCHET));
    let ledger = read_repo(LEDGER);

    // Act
    let (_, stale) = ratchet_diff(&unresolved(&ledger, repo::root()), &ratchet);
    let unreasoned: Vec<&str> = ratchet
        .iter()
        .filter(|(_, reason)| reason.is_empty())
        .map(|(key, _)| key.as_str())
        .collect();

    // Assert
    assert!(
        unreasoned.is_empty(),
        "ratchet entries without a ' # reason': {unreasoned:?}"
    );
    assert!(
        stale.is_empty(),
        "ratchet entries that now resolve or are no longer cited; delete them: {stale:?}"
    );
}

#[test]
fn a_ratcheted_citation_repeated_in_a_new_entry_is_not_covered() {
    // Arrange
    let root = fixture();
    let old = "### 1.1 Old entry\n\n**Enforced by.** `test_renamed_away`.\n";
    let ratchet = parse_ratchet("1.1 test_renamed_away # renamed by abc1234\n");

    // Act / Assert: the ratchet covers the old entry's one occurrence
    assert_eq!(
        ratchet_diff(&unresolved(old, root.path()), &ratchet),
        (vec![], vec![])
    );

    // Act / Assert: the same name in a new entry is a new, uncovered citation
    let repeated_new_entry =
        format!("{old}\n### 1.2 New entry\n\n**Enforced by.** `test_renamed_away`.\n");
    assert_eq!(
        ratchet_diff(&unresolved(&repeated_new_entry, root.path()), &ratchet),
        (strings(&["1.2 test_renamed_away"]), vec![])
    );

    // Act / Assert: and a second time in the old entry is uncovered too
    let repeated_same_entry = format!("{old}\n**Occurrence 2.** Still `test_renamed_away`.\n");
    assert_eq!(
        ratchet_diff(&unresolved(&repeated_same_entry, root.path()), &ratchet),
        (strings(&["1.1 test_renamed_away"]), vec![])
    );
}

#[test]
fn the_checker_catches_an_invented_test() {
    // Arrange
    let root = fixture();
    let tests = root.path().join("platform/tests");
    fs::write(
        tests.join("test_real.py"),
        "def test_present():\n    pass\n",
    )
    .expect("py fixture");
    fs::write(
        tests.join("test_real.bats"),
        "@test \"real: this name exists in the file\" {\n  true\n}\n",
    )
    .expect("bats fixture");
    let good = "### 1.1 An entry\n\n**Enforced by.** `test_present` in `platform/tests/test_real.py`, and\n\
        `real: this name exists in the file` in `platform/tests/test_real.bats`.\n\n\
        ```\nbats platform/tests/test_fenced_is_ignored.bats\n```\n\n\
        | 1.1 | `test_real.py::test_present` | Class | 1 | Test |\n";
    let invented = "### 2.3 Another entry\n**Enforced by.** `test_absent` in `platform/tests/test_real.py`;\n\
        `platform/tests/test_missing.py`; `test_real.py::test_nope`;\n\
        `real: this name was never written down` in `platform/tests/test_real.bats`.\n";

    // Act / Assert
    assert_eq!(unresolved(good, root.path()), Vec::<String>::new());
    let mut found = unresolved(invented, root.path());
    found.sort();
    let mut expected = strings(&[
        "2.3 test_absent",
        "2.3 platform/tests/test_missing.py",
        "2.3 test_real.py::test_nope",
        "2.3 real: this name was never written down",
    ]);
    expected.sort();
    assert_eq!(found, expected);
}

#[test]
fn rust_citations_resolve_against_the_suite_and_their_fn_names() {
    // Arrange
    let root = fixture();
    let suite = root.path().join("platform/tests/suite");
    fs::create_dir_all(&suite).expect("suite fixture");
    fs::write(
        suite.join("restart_policy.rs"),
        "#[test]\nfn real_fn() {}\n#[test] fn same_line_fn() {}\nfn generic_fn<T>() {}\n// fn commented_fn_lookalike\n// fn line_commented_fn() {}\n/*\nfn block_commented_fn() {}\n*/\npub(crate) async fn qualified_fn() {}\n",
    )
    .expect("rs fixture");
    let cite = |span: &str| format!("### 4.1 A Rust entry\n\n**Enforced by.** `{span}`.\n");

    // Act / Assert: the full path and the bare suite/ path both resolve, fn name included
    assert_eq!(
        unresolved(
            &cite("platform/tests/suite/restart_policy.rs::real_fn"),
            root.path()
        ),
        Vec::<String>::new()
    );
    assert_eq!(
        unresolved(&cite("suite/restart_policy.rs::real_fn"), root.path()),
        Vec::<String>::new()
    );

    // Act / Assert: a same-line attribute, a generic fn and a `pub(crate) async fn` count as
    // defined; a comment that names a fn without its `(` or `<` does not
    for name in ["same_line_fn", "generic_fn", "qualified_fn"] {
        assert_eq!(
            unresolved(
                &cite(&format!("suite/restart_policy.rs::{name}")),
                root.path()
            ),
            Vec::<String>::new(),
            "{name}"
        );
    }
    assert_eq!(
        unresolved(
            &cite("suite/restart_policy.rs::commented_fn_lookalike"),
            root.path()
        ),
        strings(&["4.1 suite/restart_policy.rs::commented_fn_lookalike"])
    );

    // Act / Assert: a fn that survives only inside a line or block comment is not defined
    for name in ["line_commented_fn", "block_commented_fn"] {
        let span = format!("suite/restart_policy.rs::{name}");
        assert_eq!(
            unresolved(&cite(&span), root.path()),
            strings(&[&format!("4.1 {span}")]),
            "{name}"
        );
    }

    // Act / Assert: nested comments and string literals hide a fn; a quote or `/*` inside a
    // literal, a char literal and a lifetime do not hide the code after them
    fs::write(
        suite.join("scanner.rs"),
        r##"/* outer /* inner */
fn nested_comment_fn() {}
*/
const S: &str = "
fn string_fn() {}
";
const R: &str = r#"
fn raw_string_fn() {}
"#;
const R2: &str = r#"a " quote
fn raw_quote_fn() {}
"#;
const B: &[u8] = br#"a " quote
fn byte_raw_fn() {}
"#;
const CR: &core::ffi::CStr = cr#"a " quote
fn c_raw_fn() {}
"#;
const Q: &str = "/* \" ";
fn after_string_fn() {}
const C: char = '"';
fn after_char_fn<'a>(x: &'a str) {}
const P: (char, char) = ('\'','"');
fn after_escaped_quote_fn() {}
"##,
    )
    .expect("scanner fixture");
    for (name, defined) in [
        ("nested_comment_fn", false),
        ("string_fn", false),
        ("raw_string_fn", false),
        ("raw_quote_fn", false),
        ("byte_raw_fn", false),
        ("c_raw_fn", false),
        ("after_string_fn", true),
        ("after_char_fn", true),
        ("after_escaped_quote_fn", true),
    ] {
        let span = format!("suite/scanner.rs::{name}");
        let expected = if defined {
            vec![]
        } else {
            strings(&[&format!("4.1 {span}")])
        };
        assert_eq!(unresolved(&cite(&span), root.path()), expected, "{name}");
    }

    // Act / Assert: an invented fn name, and a missing file, both fail
    assert_eq!(
        unresolved(
            &cite("platform/tests/suite/restart_policy.rs::no_such_fn"),
            root.path()
        ),
        strings(&["4.1 platform/tests/suite/restart_policy.rs::no_such_fn"])
    );
    assert_eq!(
        unresolved(
            &cite("platform/tests/suite/missing.rs::real_fn"),
            root.path()
        ),
        strings(&["4.1 platform/tests/suite/missing.rs::real_fn"])
    );
}

#[test]
fn a_rust_citation_resolves_in_this_repository() {
    // Arrange
    let text = "### 4.2 A real citation\n\n\
        **Enforced by.** `platform/tests/suite/restart_policy.rs::the_shared_preamble_includes_the_linger_task`.\n";

    // Act
    let found = unresolved(text, repo::root());

    // Assert
    assert_eq!(found, Vec::<String>::new());
}
