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
//! written stays in the append-only ledger. `platform/tests/ledger_citation_ratchet.txt`, and one
//! file per port group in `platform/tests/ledger_citation_ratchet.d/`, list it as
//! `<entry> <citation> # <reason>`. The comparison is a multiset: one ratchet line covers one
//! occurrence. The old file may only shrink; a port adds its lines in its own `ratchet.d` file,
//! each naming the replacing test as `suite/<module>.rs::<fn>`, and edits no ledger file (plan 24,
//! D10). A Rust citation cannot be ratcheted while the Python
//! checker runs: it does not read `.rs` spans, so it reads such a ratchet line as stale. Until
//! the Python checker goes (Phase 5), a cited Rust test that is renamed gets a dated note in the
//! ledger entry naming its new name, as 10.15's "Enforcement moved" note does.
//!
//! ## Port map
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | mistakes doc: the four tests named below exist | `the_tests_the_mistakes_doc_names_exist`, with the self-check `a_test_fn_inside_a_literal_or_comment_is_not_a_test` |

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use agent_cloud_tests::repo;
use regex::Regex;

const LEDGER: &str = "docs/MISTAKES.md";
const RATCHET: &str = "platform/tests/ledger_citation_ratchet.txt";
const RATCHET_DIR: &str = "platform/tests/ledger_citation_ratchet.d";
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
static INNER_CFG: LazyLock<Regex> =
    LazyLock::new(|| re(r"#\s*!\s*\[\s*cfg(?:_attr)?\s*\([^\]]*\]"));
static ATTRIBUTE: LazyLock<Regex> = LazyLock::new(|| re(r"#\s*\[[^\]]*\]"));
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

/// The file a Rust citation names: a path starting `suite/` is relative to `platform/tests/`,
/// any other to the repository root.
fn rust_citation_path(root: &Path, path: &str) -> PathBuf {
    if path.starts_with("suite/") {
        root.join("platform/tests").join(path)
    } else {
        root.join(path)
    }
}

/// A Rust citation resolves: its file exists and, when it names one, defines the fn.
fn rust_citation_resolves(root: &Path, path: &str, name: Option<&str>) -> bool {
    let file = rust_citation_path(root, path);
    file.is_file() && name.is_none_or(|name| defines(&file, name))
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
    if path.extension().and_then(|e| e.to_str()) == Some("rs") {
        defines_fn(&text, name, false)
    } else {
        let name = regex::escape(name);
        re(&format!(r"(?m)^\s*(?:async\s+)?(?:def|class)\s+{name}\b")).is_match(&text)
    }
}

/// Whether Rust source defines `fn <fn_name>` in code: line-anchored like the Python rule, with
/// attributes (on its line or the lines above, blank lines between), `pub`/`pub(..)` and `async`
/// allowed before it. Comments and literals are blanked first, so a fn inside one does not count.
/// With `require_test`, its attributes may hold only `#[test]`, `#[cfg(test)]` and `#[allow(..)]`
/// and must include `#[test]`, and the file may carry no inner `#![cfg(..)]` but `#![cfg(test)]` and
/// no inner `#![cfg_attr(..)]`: anything else could compile the test out or skip it.
fn defines_fn(text: &str, fn_name: &str, require_test: bool) -> bool {
    let code = blank_comments_and_literals(text);
    // An inner `#![cfg(..)]` other than `#![cfg(test)]`, or any inner `#![cfg_attr(..)]`, can compile
    // the whole file out.
    if require_test
        && INNER_CFG
            .find_iter(&code)
            .any(|m| m.as_str().split_whitespace().collect::<String>() != "#![cfg(test)]")
    {
        return false;
    }
    let name = regex::escape(fn_name);
    let attribute = ATTRIBUTE.as_str();
    let pattern = re(&format!(
        r"(?m)^[ \t]*((?:{attribute}[ \t\n]*)*)(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?fn[ \t]+{name}[ \t]*[<(]"
    ));
    pattern.captures_iter(&code).any(|c| {
        if !require_test {
            return true;
        }
        // A counted test is a top-level item: its `fn` line starts at column 0. Inside a `mod`
        // (rustfmt indents its items, which the rust_format test enforces) an outer attribute on
        // the module could compile it out.
        let whole = c.get(0).map_or("", |m| m.as_str());
        let fn_line = whole.rsplit('\n').next().unwrap_or(whole);
        if fn_line.starts_with([' ', '\t']) {
            return false;
        }
        // Every attribute on the fn, blank lines between them or not. They may be `#[test]`,
        // `#[cfg(test)]` or `#[allow(..)]` and must include `#[test]`. Anything else (`#[ignore]`,
        // `#[should_panic]`, `#[cfg_attr(..)]`, another `#[cfg(..)]`) changes whether or how the
        // test runs; others (`#[serial]`, say) are refused on purpose: a named test must carry
        // only these.
        // Compared with all whitespace removed: `# [test]` is `#[test]`.
        let run: Vec<String> = ATTRIBUTE
            .find_iter(&c[1])
            .map(|m| m.as_str().split_whitespace().collect())
            .collect();
        run.iter().any(|a| a == "#[test]")
            && run
                .iter()
                .all(|a| a == "#[test]" || a == "#[cfg(test)]" || a.starts_with("#[allow("))
    })
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
            if file.path.ends_with(".rs") {
                if !rust_citation_resolves(root, &file.path, file.name.as_deref()) {
                    claim_bad.push(s.clone());
                }
                continue;
            }
            let found: Vec<PathBuf> = if file.path.contains('/') {
                vec![root.join(&file.path)]
            } else {
                by_name.get(&file.path).cloned().unwrap_or_default()
            };
            let found: Vec<PathBuf> = found.into_iter().filter(|p| p.is_file()).collect();
            if found.is_empty() {
                claim_bad.push(s.clone());
                continue;
            }
            cited.extend(found.iter().cloned());
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

/// Ratchet entries under `root`: the old file, then every `*.txt` in the ratchet directory by
/// name. A missing file or directory adds nothing.
fn load_ratchet(root: &Path) -> Vec<(String, String)> {
    let mut text = fs::read_to_string(root.join(RATCHET)).unwrap_or_default();
    for file in ratchet_dir_files(root) {
        text.push('\n');
        text.push_str(&fs::read_to_string(&file).unwrap_or_else(|e| panic!("{file:?}: {e}")));
    }
    parse_ratchet(&text)
}

/// Every `*.txt` in the ratchet directory under `root`, by name; none when it is missing.
fn ratchet_dir_files(root: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = fs::read_dir(root.join(RATCHET_DIR))
        .into_iter()
        .flatten()
        .filter_map(|e| Some(e.ok()?.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "txt"))
        .collect();
    files.sort();
    files
}

/// A replacing test named in a ratchet reason: `[platform/tests/]suite/<module>.rs::<fn>`, the
/// form a ledger citation of a Rust test takes.
static REPLACING_TEST: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?P<path>(?:platform/tests/)?suite/[\w-]+\.rs)::(?P<name>\w+)"));

/// Each line of a ratchet directory file under `root` whose reason names no replacing Rust test,
/// or names one that does not resolve. A port deletes a cited test only with its replacement
/// named, so the later change that writes each entry's "Enforcement moved" note
/// (docs/MISTAKES.md 5.29) can take the names from these lines.
fn ratchet_dir_problems(root: &Path) -> Vec<String> {
    let mut problems = Vec::new();
    for file in ratchet_dir_files(root) {
        let name = file
            .file_name()
            .map_or_else(String::new, |n| n.to_string_lossy().into_owned());
        let text = fs::read_to_string(&file).unwrap_or_else(|e| panic!("{file:?}: {e}"));
        for (key, reason) in parse_ratchet(&text) {
            let named: Vec<_> = REPLACING_TEST.captures_iter(&reason).collect();
            if named.is_empty() {
                problems.push(format!(
                    "{name}: `{key}` names no replacing test (suite/<module>.rs::<fn>)"
                ));
            }
            for caps in named {
                // Resolved as a Rust ledger citation is, without the repository walk the
                // other citation forms need.
                if !rust_citation_resolves(root, &caps["path"], Some(&caps["name"])) {
                    problems.push(format!(
                        "{name}: `{key}` names `{}`, which does not exist",
                        &caps[0]
                    ));
                }
            }
        }
    }
    problems
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
    let ratchet = load_ratchet(repo::root());

    // Act
    let (new, _) = ratchet_diff(&unresolved(&ledger, repo::root()), &ratchet);

    // Assert
    assert!(
        new.is_empty(),
        "docs/MISTAKES.md cites tests that do not exist (8.2). Fix the citation, or, for a \
         test deleted or renamed since the entry was written, ratchet it with a reason in \
         platform/tests/ledger_citation_ratchet.d/<group>.txt: {new:?}"
    );
}

#[test]
fn ratchet_entries_carry_a_reason_and_still_fail() {
    // Arrange
    let ratchet = load_ratchet(repo::root());
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
fn every_ratchet_directory_line_names_an_existing_replacing_test() {
    // Arrange / Act
    let problems = ratchet_dir_problems(repo::root());

    // Assert
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

#[test]
fn a_ratchet_directory_line_without_a_real_replacing_test_is_refused() {
    // Arrange
    let root = fixture();
    let suite = root.path().join("platform/tests/suite");
    fs::create_dir_all(&suite).expect("suite fixture");
    fs::write(suite.join("ported.rs"), "#[test]\nfn real_fn() {}\n").expect("rs fixture");
    let dir = root.path().join(RATCHET_DIR);
    fs::create_dir_all(&dir).expect("ratchet dir");
    fs::write(
        dir.join("port.txt"),
        "1.1 test_a # replaced by suite/ported.rs::real_fn\n\
         1.2 test_b # ported, no name\n\
         1.3 test_c # replaced by platform/tests/suite/ported.rs::missing_fn\n",
    )
    .expect("ratchet file");

    // Act
    let problems = ratchet_dir_problems(root.path());

    // Assert
    assert_eq!(
        problems,
        strings(&[
            "port.txt: `1.2 test_b` names no replacing test (suite/<module>.rs::<fn>)",
            "port.txt: `1.3 test_c` names `platform/tests/suite/ported.rs::missing_fn`, which does not exist",
        ])
    );
}

#[test]
fn a_ratchet_line_in_a_ratchet_directory_file_covers_a_citation() {
    // Arrange
    let root = fixture();
    let dir = root.path().join(RATCHET_DIR);
    fs::create_dir_all(&dir).expect("ratchet dir");
    fs::write(
        dir.join("port.txt"),
        "1.1 test_ported_away # replaced by suite/x.rs::y\n",
    )
    .expect("ratchet file");
    fs::write(dir.join("notes.md"), "1.1 test_ignored # not a .txt\n").expect("other file");
    let ledger = "### 1.1 Old entry\n\n**Enforced by.** `test_ported_away`.\n";

    // Act
    let diff = ratchet_diff(&unresolved(ledger, root.path()), &load_ratchet(root.path()));

    // Assert
    assert_eq!(diff, (vec![], vec![]));
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

/// `fn <name>` defined as a test in code: `#[test]` among its attributes, in any order, and no
/// `#[cfg(...)]` but `#[cfg(test)]` ([`defines_fn`] with `require_test`).
fn defines_test(text: &str, name: &str) -> bool {
    defines_fn(text, name, true)
}

/// The fns a module's `//!` port map names for a BATS test: rows `| <name> | <fn>... |`, the
/// first cell compared without backticks; from the second cell, its first backticked name, or
/// its first word when nothing is backticked. Only the file's leading `//!` header is read, so a
/// row-shaped line elsewhere (in a string, say) is not a port map.
fn port_map_fns(text: &str, name: &str) -> Vec<String> {
    text.lines()
        .map_while(|line| line.strip_prefix("//!"))
        .filter_map(|row| {
            let cells: Vec<&str> = row.split('|').collect();
            if cells.len() < 3 || cells[1].trim().trim_matches('`').trim() != name {
                return None;
            }
            SPAN.captures(cells[2])
                .map(|c| c[1].to_owned())
                .or_else(|| cells[2].split_whitespace().next().map(str::to_owned))
        })
        .collect()
}

/// Whether a test name exists: a BATS `@test` in `platform/tests/*.bats`, or a port-map row in a
/// suite module whose fn that module defines as a test.
fn test_name_exists(root: &Path, name: &str) -> bool {
    let tests = root.join("platform/tests");
    let files = |dir: &Path, ext: &str| -> Vec<PathBuf> {
        fs::read_dir(dir)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some(ext))
            .collect()
    };
    files(&tests, "bats")
        .iter()
        .any(|bats| bats_names(bats).contains(name))
        || files(&tests.join("suite"), "rs").iter().any(|rs| {
            let text = read_lossy(rs);
            port_map_fns(&text, name)
                .iter()
                .any(|f| defines_test(&text, f))
        })
}

#[test]
fn the_tests_the_mistakes_doc_names_exist() {
    // Entry 8.2 is an invented cross-reference. The names are listed, not discovered: a
    // discovering version needed enough pattern matching to mistake code for prose (entry 2.2).
    // Deliberately not exhaustive over every name the ledger mentions.
    let root = repo::root();
    let doc = read_repo(LEDGER)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    for name in [
        "every play that resolves an OpenBao URL includes the transport guard",
        "the transport pattern accepts internal endpoints and refuses public ones",
        "postiz: the seed playbook uses the shared cleartext OpenBao guard",
        "install-podman: configures no podman API socket",
    ] {
        assert!(doc.contains(name), "{LEDGER} no longer names {name:?}");
        assert!(
            test_name_exists(root, name),
            "no BATS @test and no Rust port-map #[test] fn for: {name}"
        );
    }
}

#[test]
fn a_test_fn_inside_a_literal_or_comment_is_not_a_test() {
    let text = "#[test]\nfn real() {}\n\
        const R: &str = r\"\n#[test]\nfn in_raw() {}\n\";\n\
        const S: &str = \"\n#[test]\nfn in_plain() {}\n\";\n\
        let x = 1; /*\n#[test]\nfn after_mid_line_comment() {}\n*/\n\
        fn untested() {}\n";
    assert!(defines_test(text, "real"));
    for name in ["in_raw", "in_plain", "after_mid_line_comment", "untested"] {
        assert!(!defines_test(text, name), "{name}");
    }
    let rows = "//! | a bats name | `real_fn`, with `other` |\n//! | bare name | bare_fn |\n\n\
        const S: &str = \"\n//! | late name | `late_fn` |\n\";\n";
    assert_eq!(
        port_map_fns(rows, "a bats name"),
        vec!["real_fn".to_owned()]
    );
    assert_eq!(port_map_fns(rows, "bare name"), vec!["bare_fn".to_owned()]);
    assert!(port_map_fns(rows, "late name").is_empty());
    let cfg_out = "#[cfg(any())]\n#[test]\nfn x() {}\n#[cfg(test)]\n\n#[test]\nfn y() {}\n";
    assert!(!defines_test(cfg_out, "x"));
    assert!(defines_test(cfg_out, "y"));
    assert!(!defines_test("#[test]\n#[ignore]\nfn x() {}\n", "x"));
    assert!(!defines_test("#[ignore]\n\n#[test]\nfn x() {}\n", "x"));
    assert!(!defines_test("# [cfg(any())]\n#[test]\nfn x() {}\n", "x"));
    assert!(!defines_test(
        "#[cfg(any())]\nmod m {\n    #[test]\n    fn x() {}\n}\n",
        "x"
    ));
    assert!(!defines_test("#![cfg(any())]\n#[test]\nfn x() {}\n", "x"));
    assert!(!defines_test("#![cfg (any())]\n#[test]\nfn x() {}\n", "x"));
    assert!(!defines_test(
        "#! [ cfg(any()) ]\n#[test]\nfn x() {}\n",
        "x"
    ));
    assert!(!defines_test("# ![cfg(any())]\n#[test]\nfn x() {}\n", "x"));
    assert!(defines_test("#! [cfg(test)]\n#[test]\nfn x() {}\n", "x"));
    assert!(!defines_test(
        "#![cfg_attr(any(), ignore)]\n#[test]\nfn x() {}\n",
        "x"
    ));
    assert!(defines_test("#![cfg(test)]\n#[test]\nfn x() {}\n", "x"));
    assert!(!defines_test("#[cfg(any())]\n\n#[test]\nfn x() {}\n", "x"));
    assert!(!defines_test(
        "#[cfg_attr(test, ignore)]\n#[test]\nfn x() {}\n",
        "x"
    ));
    assert!(!defines_test("#[test]\n#[should_panic]\nfn x() {}\n", "x"));
    assert!(defines_test(
        "#[test]\n#[allow(dead_code)]\nfn x() {}\n",
        "x"
    ));
    assert!(defines_test("#[test] fn x() {}\n", "x"));
    assert!(!defines_test(
        "#[ignore = \"slow\"]\n#[test]\nfn x() {}\n",
        "x"
    ));
}
