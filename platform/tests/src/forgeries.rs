//! Forged extra vars for the tests that prove an extra-var refusal holds: the Rust mirror of
//! `platform/tests/forgeries.py` (plan 24, AC-951).
//!
//! An extra var is not a value but a Jinja template, rendered afresh wherever it is read. A
//! refusal that checks what the name RENDERS to can be shown one value while the work reads
//! another, so the strongest member of the input class is a template (docs/MISTAKES.md 1.15):
//!
//! - `plain`: the forged value itself;
//! - `context`: the honest value while a loop variable is set or the play's name says it refuses
//!   (where a probe or a guard play looks), the forgery everywhere else;
//! - `stateful`: the honest value on its first rendering only, then the forgery: no check that
//!   renders the name before the work reads it can tell the difference.
//!
//! Every refusal test parametrizes over [`templated_forgeries`], or uses [`by_name`] (plain only)
//! for a refusal the run-start guard makes by name (plan 21, D1).
//!
//! Two helpers sit beside them, not among them: [`counting`] measures how often a run renders a
//! name, and [`honest_for`] is honest for a given number of renderings, then forged (plan 24).
//!
//! Each `-e` string is byte for byte the text the Python produces for the same inputs: JSON as
//! `json.dumps` writes it (`", "` and `": "`, non-ASCII escaped), which serde_json's compact
//! writer is not, so [`dumps`] writes it here. One exception: a float is written by serde_json
//! (shortest round-trip), which matches Python's `repr` for ordinary values such as `1.5` but
//! spells a magnitude below 1e-4 differently (`0.00001` for Python's `1e-05`). Ansible and Jinja's
//! `from_json` read both spellings as the same float, so a forgery's value is unchanged.
//!
//! | `forgeries.py` | Rust |
//! |---|---|
//! | `templated_forgeries` | [`templated_forgeries`] |
//! | `by_name` | [`by_name`] |
//! | `counting` | [`counting`] |
//! | `honest_for` | [`honest_for`] |
//! | `GUARD_REFERENCED` | [`GUARD_REFERENCED`] |

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use crate::{playbook, text};

/// Every underscore name the run-start guard itself references, read from the guard file (strings
/// and mapping keys, comments excluded by parsing). The guard reads the forged value of these names
/// (its own `when:` reads `_extra_var_guard_nested`), so a test forging one keeps all three
/// spellings. Over-inclusion only keeps more spellings: the refusal pattern's `_pass` fragments
/// land here too.
pub static GUARD_REFERENCED: LazyLock<BTreeSet<String>> = LazyLock::new(|| {
    let rel = format!("platform/playbooks/{}", playbook::OVERRIDE_GUARD);
    let text = text::read(&rel);
    // Python's `(?<![A-Za-z0-9])_[A-Za-z][A-Za-z0-9_]*` as a consumed prefix: the regex crate has no
    // lookbehind. A match ends before a character outside `[A-Za-z0-9_]`, and the next name's `_`
    // cannot sit there (the greedy class would have taken it), so the consumed prefix never overlaps
    // the previous match. A `_` start preceded by a `_` still matches, as in Python (`__x` yields `_x`).
    static NAME: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?:^|[^A-Za-z0-9])(_[A-Za-z][A-Za-z0-9_]*)")
            .expect("the name pattern compiles")
    });
    let docs = playbook::parse(&text, &rel);
    let mut strings = Vec::new();
    for doc in &docs {
        walk_strings(doc, &mut strings);
    }
    strings
        .iter()
        .flat_map(|s| NAME.captures_iter(s))
        .map(|c| c[1].to_string())
        .collect()
});

/// Every string inside a value, and every mapping key (`playbook_yaml.strings(..., keys=True)`).
/// S6 adds a shared string walk; this one is private until then.
fn walk_strings<'a>(value: &'a Value, out: &mut Vec<&'a str>) {
    match value {
        Value::String(s) => out.push(s),
        Value::Array(items) => items.iter().for_each(|v| walk_strings(v, out)),
        Value::Object(map) => {
            for (k, v) in map {
                out.push(k);
                walk_strings(v, out);
            }
        }
        _ => {}
    }
}

/// One spelling of a forgery.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Spelling {
    Plain,
    Context,
    Stateful,
}

impl Spelling {
    /// The pytest param id: `plain`, `context` or `stateful`.
    pub fn id(&self) -> &'static str {
        match self {
            Spelling::Plain => "plain",
            Spelling::Context => "context",
            Spelling::Stateful => "stateful",
        }
    }
}

/// One spelling of one name's forgery; [`Forgery::extra_vars`] gives its `-e` JSON.
#[derive(Debug, Clone)]
pub struct Forgery {
    pub spelling: Spelling,
    name: String,
    honest: Value,
    forged: Value,
}

impl Forgery {
    /// The `-e` JSON for this spelling. `dir` is the test's temp directory: the stateful spelling
    /// counts its renderings in a file there; the others ignore it.
    pub fn extra_vars(&self, dir: &Path) -> String {
        match self.spelling {
            Spelling::Plain => dumps_one(&self.name, &self.forged),
            Spelling::Context => {
                let cond = "(ansible_loop_var is defined or (ansible_play_name | default('')) is search('(?i)refuse'))";
                dumps_one(
                    &self.name,
                    &Value::String(ternary(cond, &self.honest, &self.forged)),
                )
            }
            Spelling::Stateful => honest_for(&self.name, &self.honest, &self.forged, 1, dir),
        }
    }
}

/// The three spellings, in order plain, context, stateful. The Python lets `honest` and `forged`
/// be functions of tmp_path, for a value that names a path inside it; here the caller builds the
/// [`Value`] with the path it already holds.
pub fn templated_forgeries(name: &str, honest: Value, forged: Value) -> Vec<Forgery> {
    [Spelling::Plain, Spelling::Context, Spelling::Stateful]
        .into_iter()
        .map(|spelling| Forgery {
            spelling,
            name: name.to_string(),
            honest: honest.clone(),
            forged: forged.clone(),
        })
        .collect()
}

/// The plain spelling only, for a refusal the run-start guard makes BY NAME.
///
/// `refuse-internal-extra-vars.yml` refuses an underscore name by its presence in hostvars and
/// never renders its value, so the templated spellings cannot change the outcome; the guard's own
/// test proves it (test_the_guard_ends_the_run_before_any_other_play asserts the stateful forgery
/// is never rendered). Plan 21 D1, docs/MISTAKES.md 1.15. A name the guard itself references, or
/// one without the underscore prefix, keeps all three spellings: refused here.
pub fn by_name(name: &str, honest: Value, forged: Value) -> Vec<Forgery> {
    assert!(
        name.starts_with('_'),
        "by_name is for underscore names the guard refuses by name: {name}"
    );
    assert!(
        !GUARD_REFERENCED.contains(name),
        "{name} is referenced by the guard itself, which may render it: use templated_forgeries"
    );
    let mut all = templated_forgeries(name, honest, forged);
    all.truncate(1);
    all
}

/// `-e` JSON that renders `value` at every reading of the name, and counts those renderings in a
/// file. Returns (the `-e` JSON, the counter path). After a run the counter holds how many times
/// the name was rendered. Plan 24: a run's readings are measured with this first, and that count
/// goes to [`honest_for`].
pub fn counting(name: &str, value: &Value, dir: &Path) -> (String, PathBuf) {
    counted(name, dir, ">= 0", value, value)
}

/// `-e` JSON that renders `honest` for the first `renders` renderings of the name, then `forged`.
///
/// stateful is the renders=1 case. The identity check reads local_mode more than once, so the
/// stateful spelling cannot pass it (plan 24; test_local_mode_identity.py). A test that uses
/// honest_for after the check need not also parametrize templated_forgeries there: the check
/// itself refuses the templated forms on a production inventory. Give this the count [`counting`]
/// measured for the check, so any reader after the check gets the forgery and a raw read shows
/// through.
pub fn honest_for(name: &str, honest: &Value, forged: &Value, renders: u32, dir: &Path) -> String {
    assert!(renders >= 1, "{renders}");
    counted(name, dir, &format!("< {renders}"), honest, forged).0
}

/// The one builder of a render-counted value: `-e` JSON that renders `good` while the number of
/// EARLIER renderings of the name satisfies `comparison` (a Jinja test on int, such as "< 1"),
/// else `bad`. The count is kept in a file under `dir`.
fn counted(
    name: &str,
    dir: &Path,
    comparison: &str,
    good: &Value,
    bad: &Value,
) -> (String, PathBuf) {
    let (counter, shell) = render_counter(name, dir);
    let cond = format!(
        "(lookup('ansible.builtin.pipe', {}) | int {comparison})",
        string(&shell)
    );
    (
        dumps_one(name, &Value::String(ternary(&cond, good, bad))),
        counter,
    )
}

/// The counter file for one name's renderings, and the shell that counts one rendering: it prints
/// the number of renderings BEFORE this one and leaves the file one higher. Renders from parallel
/// hosts are serialized by the lock directory beside the counter (AC-902): the truncating write let
/// a concurrent read see an empty file, count 0, and render the honest value again. mkdir is the
/// lock because macOS ships no flock command.
///
/// The shell is byte-identical to `forgeries._render_counter`'s. Its increment is assembled from
/// two pieces so this file never holds the needle test_the_render_counter_has_one_implementation
/// looks for (AC-919): that test allows only forgeries.py to carry it.
fn render_counter(name: &str, dir: &Path) -> (PathBuf, String) {
    let counter = dir.join(format!("forgery-renders-{name}"));
    let lock = format!("{}.lock", counter.display());
    let c = counter.display().to_string();
    assert!(!c.contains(' '), "{c}");
    // ponytail: a render killed while holding the lock leaves a stale .lock, and every later render
    // spins until the harness timeout. Upgrade path: a trap that removes the lock on exit, once seen.
    let increment = concat!("echo $((n+1)) ", ">");
    let shell = format!(
        "until mkdir {lock} 2>/dev/null; do sleep 0.01; done; n=$(cat {c} 2>/dev/null || echo 0); {increment} {c}; rmdir {lock}; echo $n"
    );
    (counter, shell)
}

/// The `-e` string that renders `good` while the Jinja condition `cond` holds, else `bad`.
fn ternary(cond: &str, good: &Value, bad: &Value) -> String {
    format!(
        "{{{{ {cond} | ternary({}, {}) }}}}",
        literal(good),
        literal(bad)
    )
}

/// A single-quoted Jinja string; refuses text that would need escaping.
fn string(text: &str) -> String {
    assert!(
        !text.contains('\'') && !text.contains('\\'),
        "no quote or backslash in a forgery: {text:?}"
    );
    format!("'{text}'")
}

/// A Jinja expression for a JSON-able value, whatever its type.
fn literal(value: &Value) -> String {
    format!("({} | from_json)", string(&dumps(value)))
}

/// `json.dumps({name: value})`.
fn dumps_one(name: &str, value: &Value) -> String {
    format!("{{{}: {}}}", ascii_str(name), dumps(value))
}

/// JSON as Python's `json.dumps` writes it by default: `", "` and `": "` separators, insertion
/// order, and every non-ASCII character (and DEL) escaped as `\uXXXX`, surrogate pairs above U+FFFF.
/// serde_json's compact writer has the separators wrong and passes non-ASCII through, so this
/// builds each container itself and takes each string from serde_json, then escapes what remains.
fn dumps(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(dumps).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Object(map) => {
            let parts: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("{}: {}", ascii_str(k), dumps(v)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
        Value::String(s) => ascii_str(s),
        // null, true, false and a number: serde_json's compact text, which matches Python's for
        // ordinary values (the module doc says where it does not).
        scalar => scalar.to_string(),
    }
}

fn ascii_str(s: &str) -> String {
    let quoted = serde_json::to_string(s).expect("a string serializes");
    let mut out = String::with_capacity(quoted.len());
    for c in quoted.chars() {
        // Python escapes everything outside space..tilde (`[^\ -~]`), DEL included. serde_json has
        // already escaped the quote, the backslash and the control characters the way Python does.
        if (' '..='~').contains(&c) {
            out.push(c);
        } else {
            let mut buf = [0u16; 2];
            for unit in c.encode_utf16(&mut buf) {
                let _ = write!(out, "\\u{unit:04x}");
            }
        }
    }
    out
}
