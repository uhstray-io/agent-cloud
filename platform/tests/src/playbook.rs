//! Repository playbooks lifted to JSON, so a playbook-tier test can run a play's tasks through
//! [`process::ansible_playbook`](crate::process::ansible_playbook) (plan 24, AC-943).
//!
//! The Python helper `platform/tests/playbook_yaml.py` reads a play with PyYAML, and each Python
//! test that runs a lifted play dumps it with `yaml.safe_dump`, so `ansible-playbook` runs the play
//! as the repository wrote it. Here [`write`] puts it in a file as JSON instead. The reading cannot
//! reuse [`yaml::parse`](crate::yaml::parse): a [`yaml::Node`] has no number type, so every plain
//! scalar that is not a bool or null is a string, and `"53"` and `53` are the same `Node::Str`. A
//! lifted play must carry `port: 53` as a number, so this loader builds [`serde_json::Value`] from
//! the event stream instead. Bool and null come from [`yaml::resolve_plain`]. Numbers and
//! timestamps follow PyYAML 6.0.3's resolvers (`yaml/resolver.py`) and constructors
//! (`yaml/constructor.py`).
//!
//! Mapping order is kept (serde_json's `preserve_order`): Jinja iterates a dict in insertion
//! order, so a sorted map would change what a lifted play does.
//!
//! A refusal panics with the file, the mapping key and the scalar. Each one is a node PyYAML
//! reads in a way JSON cannot keep, or one this loader will not read the way PyYAML does:
//! - a tag (`!!str`, `!custom`): PyYAML's loader nulls a tagged value. No repository file has one,
//!   so the loader refuses rather than read it differently;
//! - a plain timestamp, `.inf` or `.nan`, or an integer beyond `i64`;
//! - a mapping key that is not a string. PyYAML keeps `200`, `yes` or `[a]` as a key; a JSON
//!   object key is a string.
//!
//! `<<` merges follow [`yaml`]'s builder: a mapping, or a list of mappings read last-first, is
//! merged under the explicit keys, and a later duplicate key wins. The `support_lift` tests pin
//! the two loaders together over every repository file.
//!
//! | `playbook_yaml.py` | Rust |
//! |---|---|
//! | `plays` | [`plays`] |
//! | `loads` | [`loads`] |
//! | `OVERRIDE_GUARD` | [`OVERRIDE_GUARD`] |
//!
//! The rest of `playbook_yaml.py` (`load`, `tasks`, `strings`, `files`, ...) has Node-based or no
//! equivalents here; `yaml::load_one` and `repo::tasks` cover the first two.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Number, Value};
use yaml_rust2::parser::{Event, MarkedEventReceiver, Parser, Tag};
use yaml_rust2::scanner::{Marker, TScalarStyle};

use crate::repo;
use crate::yaml::{self, CORE_TAG, Node};

/// The import every workflow executor opens with (`playbook_yaml.OVERRIDE_GUARD`).
pub const OVERRIDE_GUARD: &str = "refuse-internal-extra-vars.yml";

/// PyYAML's implicit `float` resolver (`yaml/resolver.py`), matched against the raw text.
static FLOAT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^(?:[-+]?[0-9][0-9_]*\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
    )
    .expect("the float pattern compiles")
});

/// PyYAML's implicit `int` resolver (`yaml/resolver.py`).
static INT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$",
    )
    .expect("the int pattern compiles")
});

/// PyYAML's implicit `timestamp` resolver (`yaml/resolver.py`).
static TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)$",
    )
    .expect("the timestamp pattern compiles")
});

/// Every document in `text`, as JSON values. Panics with `origin` on a parse error or a
/// refusal (see the module doc). In a test, a file this loader cannot read is a failure.
pub fn parse(text: &str, origin: &str) -> Vec<Value> {
    let mut builder = Builder {
        origin,
        ..Builder::default()
    };
    Parser::new_from_str(text)
        .load(&mut builder, true)
        .unwrap_or_else(|e| panic!("{origin}: YAML parse error: {e}"));
    builder.docs
}

/// `playbook_yaml.loads`: the one document in `text`, `Value::Null` when there is none. Panics
/// on more than one document, as PyYAML's `yaml.load` does.
pub fn loads(text: &str) -> Value {
    document(text, "<loads>")
}

/// `playbook_yaml.plays`: the plays of the playbook at `rel` (relative to the repository root;
/// an absolute path also works), without its leading extra-var guard import. Freshly parsed, so
/// a harness may edit the result.
pub fn plays(rel: &str) -> Vec<Value> {
    let path = repo::path(rel);
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    let items = match document(&text, rel) {
        Value::Null => return vec![],
        Value::Array(items) => items,
        _ => panic!("{rel}: a playbook is a list of plays"),
    };
    let guarded = matches!(
        items.first(),
        Some(Value::Object(first))
            if first.get("ansible.builtin.import_playbook").and_then(Value::as_str)
                == Some(OVERRIDE_GUARD)
    );
    if guarded {
        items.into_iter().skip(1).collect()
    } else {
        items
    }
}

/// Writes `playbook` as JSON to `dir/name` and returns the path. JSON is YAML, so
/// `ansible-playbook` reads the file as a playbook.
pub fn write(dir: &Path, name: &str, playbook: &Value) -> PathBuf {
    let path = dir.join(name);
    let json = serde_json::to_string_pretty(playbook).expect("a JSON value serializes");
    std::fs::write(&path, json).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    path
}

fn document(text: &str, origin: &str) -> Value {
    let mut docs = parse(text, origin);
    assert!(
        docs.len() <= 1,
        "{origin}: {} YAML documents, expected one",
        docs.len()
    );
    docs.pop().unwrap_or(Value::Null)
}

/// Panics naming the file, the mapping key and, when there is one, the scalar.
fn refuse(origin: &str, key: &str, reason: &str, scalar: Option<&str>) -> ! {
    match scalar {
        Some(text) => panic!("{origin}: key `{key}`: {reason}: `{text}`"),
        None => panic!("{origin}: key `{key}`: {reason}"),
    }
}

/// A plain untagged scalar's value, as PyYAML's implicit resolvers and constructors read it.
/// `Err` names why JSON cannot hold the value.
fn resolve_plain_value(text: &str) -> Result<Value, &'static str> {
    match yaml::resolve_plain(text) {
        Node::Bool(b) => Ok(Value::Bool(b)),
        Node::Null => Ok(Value::Null),
        // PyYAML tries the float, int and timestamp resolvers in turn; a text matches at most one.
        Node::Str(_) if FLOAT.is_match(text) => float_value(text),
        Node::Str(_) if INT.is_match(text) => int_value(text),
        Node::Str(_) if TIMESTAMP.is_match(text) => Err("a timestamp is not a JSON value"),
        Node::Str(_) => Ok(Value::String(text.to_owned())),
        Node::Seq(_) | Node::Map(_) => unreachable!("resolve_plain reads one scalar"),
    }
}

/// PyYAML `construct_yaml_float`: underscores dropped, lower-cased, a sign, `.inf`/`.nan`, and
/// base-60 digits joined by colons.
fn float_value(text: &str) -> Result<Value, &'static str> {
    let cleaned = text.replace('_', "").to_lowercase();
    let (negative, body) = split_sign(&cleaned);
    if body == ".inf" || body == ".nan" {
        return Err("infinity and NaN are not JSON values");
    }
    let magnitude = if body.contains(':') {
        // PyYAML reverses the digits, then sums each times a base that grows by 60.
        body.split(':')
            .rev()
            .try_fold((0.0_f64, 1.0_f64), |(total, base), part| {
                let digit = part.parse::<f64>().map_err(|_| "not a number")?;
                Ok::<_, &'static str>((total + digit * base, base * 60.0))
            })?
            .0
    } else {
        body.parse::<f64>().map_err(|_| "not a number")?
    };
    let value = if negative { -magnitude } else { magnitude };
    Number::from_f64(value)
        .map(Value::Number)
        .ok_or("not a finite JSON number")
}

/// PyYAML `construct_yaml_int`: underscores dropped, a sign, then binary, hex, octal (a leading
/// zero, so `0` is octal zero too), base-60 (colons) or decimal. The result must fit an `i64`.
fn int_value(text: &str) -> Result<Value, &'static str> {
    let cleaned = text.replace('_', "");
    let (negative, body) = split_sign(&cleaned);
    let magnitude = if let Some(bits) = body.strip_prefix("0b") {
        parse_int(bits, 2)?
    } else if let Some(hex) = body.strip_prefix("0x") {
        parse_int(hex, 16)?
    } else if body.starts_with('0') {
        parse_int(body, 8)?
    } else if body.contains(':') {
        base60_int(body)?
    } else {
        parse_int(body, 10)?
    };
    let signed = if negative { -magnitude } else { magnitude };
    i64::try_from(signed)
        .map(Value::from)
        .map_err(|_| "an integer beyond i64 is not a JSON value")
}

fn parse_int(digits: &str, radix: u32) -> Result<i128, &'static str> {
    i128::from_str_radix(digits, radix).map_err(|_| "not an integer")
}

/// Base-60 digits, the last one least significant (PyYAML reverses them).
fn base60_int(body: &str) -> Result<i128, &'static str> {
    const REASON: &str = "not a base-60 integer within i128";
    body.split(':')
        .rev()
        .try_fold((0_i128, 1_i128), |(total, base), part| {
            let digit = part.parse::<i128>().map_err(|_| REASON)?;
            let term = digit.checked_mul(base).ok_or(REASON)?;
            Ok::<_, &'static str>((
                total.checked_add(term).ok_or(REASON)?,
                base.checked_mul(60).ok_or(REASON)?,
            ))
        })
        .map(|(total, _)| total)
}

fn split_sign(text: &str) -> (bool, &str) {
    match text.as_bytes().first() {
        Some(b'-') => (true, &text[1..]),
        Some(b'+') => (false, &text[1..]),
        _ => (false, text),
    }
}

/// A finished node. `text` is a plain scalar's source text, kept for a key refusal. A quoted
/// scalar is always a string, so it carries none, and a collection has none in the event stream.
#[derive(Clone)]
struct Item {
    value: Value,
    text: Option<String>,
}

/// The key a mapping is waiting for a value under.
enum Pending {
    Merge,
    Named(String),
}

enum Frame {
    Seq {
        items: Vec<Value>,
        anchor: usize,
    },
    Map {
        entries: Vec<(String, Value)>,
        merges: Vec<Value>,
        /// `None` when the next node is a key.
        key: Option<Pending>,
        anchor: usize,
    },
}

#[derive(Default)]
struct Builder<'a> {
    origin: &'a str,
    docs: Vec<Value>,
    stack: Vec<Frame>,
    anchors: HashMap<usize, Item>,
}

impl Builder<'_> {
    /// The mapping key a node under this position belongs to: the innermost mapping's pending
    /// key. `own` names the node itself when it is a key, and `<root>` sits outside any mapping.
    fn key_label(&self, own: &str) -> String {
        for frame in self.stack.iter().rev() {
            if let Frame::Map { key, .. } = frame {
                return match key {
                    None => own.to_owned(),
                    Some(Pending::Merge) => "<<".to_owned(),
                    Some(Pending::Named(k)) => k.clone(),
                };
            }
        }
        "<root>".to_owned()
    }

    /// A tag on a node is refused, naming the tag. `scalar` is the node's text; a collection has
    /// none.
    fn refuse_tag(&self, tag: Option<&Tag>, scalar: Option<&str>) {
        let Some(tag) = tag else { return };
        let own = scalar.unwrap_or("<collection>");
        let full = yaml::tag_text(tag);
        let reason = match full.strip_prefix(CORE_TAG) {
            Some(name) => format!("core tag `!!{name}` is not supported by the playbook loader"),
            None => format!("application tag `{full}` is not supported by the playbook loader"),
        };
        refuse(self.origin, &self.key_label(own), &reason, scalar);
    }

    /// Places a finished node in its parent: a document, a sequence item, a merge value or a
    /// mapping entry. A mapping key must be a string.
    fn push(&mut self, item: Item, is_merge_key: bool) {
        let origin = self.origin;
        match self.stack.last_mut() {
            None => self.docs.push(item.value),
            Some(Frame::Seq { items, .. }) => items.push(item.value),
            Some(Frame::Map {
                entries,
                merges,
                key,
                ..
            }) => match key.take() {
                None if is_merge_key => *key = Some(Pending::Merge),
                None => match item.value {
                    Value::String(k) => *key = Some(Pending::Named(k)),
                    value => refuse_key(origin, &value, item.text),
                },
                Some(Pending::Merge) => merges.push(item.value),
                Some(Pending::Named(k)) => entries.push((k, item.value)),
            },
        }
    }

    /// The one place a finished node is recorded as an anchor and placed in its parent.
    fn finish(&mut self, item: Item, anchor: usize, is_merge_key: bool) {
        if anchor > 0 {
            self.anchors.insert(anchor, item.clone());
        }
        self.push(item, is_merge_key);
    }
}

/// A mapping key that is not a string: named by its scalar text, or, for a collection, by its
/// kind.
fn refuse_key(origin: &str, value: &Value, text: Option<String>) -> ! {
    match text {
        Some(text) => refuse(origin, &text, "a mapping key must be a string", Some(&text)),
        None => {
            let kind = if value.is_array() {
                "a sequence"
            } else {
                "a mapping"
            };
            refuse(
                origin,
                "<non-scalar key>",
                &format!("a mapping key must be a string, found {kind}"),
                None,
            )
        }
    }
}

/// Explicit keys win over every merged key, and a later merged mapping wins over an earlier one.
/// A list is read last-first, so its first mapping wins within the list (PyYAML's
/// `flatten_mapping`). Keys keep the order they first appear in.
fn apply_merges(
    explicit: Vec<(String, Value)>,
    merges: Vec<Value>,
    origin: &str,
) -> Map<String, Value> {
    let mut out = Map::new();
    for merge in merges {
        let list = match merge {
            Value::Array(items) => items,
            map @ Value::Object(_) => vec![map],
            other => refuse(
                origin,
                "<<",
                "a merge value must be a mapping or a list of mappings",
                Some(other.to_string().as_str()),
            ),
        };
        for map in list.into_iter().rev() {
            match map {
                Value::Object(entries) => {
                    for (k, v) in entries {
                        out.insert(k, v);
                    }
                }
                other => refuse(
                    origin,
                    "<<",
                    "a merge list may hold only mappings",
                    Some(other.to_string().as_str()),
                ),
            }
        }
    }
    for (k, v) in explicit {
        out.insert(k, v);
    }
    out
}

impl MarkedEventReceiver for Builder<'_> {
    fn on_event(&mut self, ev: Event, _mark: Marker) {
        match ev {
            Event::Scalar(text, style, anchor, tag) => {
                self.refuse_tag(tag.as_ref(), Some(text.as_str()));
                let plain = style == TScalarStyle::Plain;
                let is_merge_key = plain
                    && text == "<<"
                    && matches!(self.stack.last(), Some(Frame::Map { key: None, .. }));
                let item = if plain {
                    let value = resolve_plain_value(&text).unwrap_or_else(|why| {
                        refuse(self.origin, &self.key_label(&text), why, Some(&text))
                    });
                    Item {
                        value,
                        text: Some(text),
                    }
                } else {
                    Item {
                        value: Value::String(text),
                        text: None,
                    }
                };
                self.finish(item, anchor, is_merge_key);
            }
            Event::Alias(id) => {
                let item = self
                    .anchors
                    .get(&id)
                    .cloned()
                    .expect("alias to a known anchor");
                self.finish(item, 0, false);
            }
            Event::SequenceStart(anchor, tag) => {
                self.refuse_tag(tag.as_ref(), None);
                self.stack.push(Frame::Seq {
                    items: vec![],
                    anchor,
                });
            }
            Event::MappingStart(anchor, tag) => {
                self.refuse_tag(tag.as_ref(), None);
                self.stack.push(Frame::Map {
                    entries: vec![],
                    merges: vec![],
                    key: None,
                    anchor,
                });
            }
            Event::SequenceEnd => {
                if let Some(Frame::Seq { items, anchor }) = self.stack.pop() {
                    let item = Item {
                        value: Value::Array(items),
                        text: None,
                    };
                    self.finish(item, anchor, false);
                }
            }
            Event::MappingEnd => {
                if let Some(Frame::Map {
                    entries,
                    merges,
                    anchor,
                    ..
                }) = self.stack.pop()
                {
                    let merged = apply_merges(entries, merges, self.origin);
                    let item = Item {
                        value: Value::Object(merged),
                        text: None,
                    };
                    self.finish(item, anchor, false);
                }
            }
            _ => {}
        }
    }
}
