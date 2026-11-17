//! Repository playbooks lifted to JSON, so a playbook-tier test can run a play's tasks through
//! [`process::ansible_playbook`](crate::process::ansible_playbook) (plan 24, AC-943).
//!
//! The Python helper `platform/tests/playbook_yaml.py` reads a play with PyYAML and writes it back
//! with `yaml.safe_dump`, so `ansible-playbook` runs the play as the repository wrote it. This
//! module is the Rust side of that. It cannot reuse [`yaml::parse`](crate::yaml::parse): a
//! [`yaml::Node`] has no number type, so every plain scalar that is not a bool or null is a string,
//! and `"53"` and `53` are the same `Node::Str`. A lifted play must carry `port: 53` as a number,
//! so this loader builds [`serde_json::Value`] from the event stream instead. Bool and null come
//! from [`yaml::resolve_plain`]. Numbers and timestamps follow PyYAML 6.0.3's resolvers
//! (`yaml/resolver.py`) and constructors (`yaml/constructor.py`).
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
//! | `playbook_yaml.py` (Python call sites) | Rust |
//! |---|---|
//! | `plays` (106 calls in 47 files) | [`plays`] |
//! | `loads` (18 calls in 10 files) | [`loads`] |
//! | `OVERRIDE_GUARD` (used in 5 other files under `platform/tests`) | [`OVERRIDE_GUARD`] |
//! | `load` (101 calls in 45 files) | not ported: `yaml::load_one` serves it (Node) |
//! | `tasks` (74 calls in 38 files) | not ported: `repo::tasks` serves it (Node) |
//! | `strings` (21 calls in 11 files) | not ported |
//! | `files` (13 calls in 9 files) | not ported |
//! | `TASK_LISTS` (5 uses in 5 files) | not ported |
//! | `SET_FACT` (3 uses in 2 files) | not ported |
//! | `defined_names` (1 use in 1 file) | not ported |
//! | `SCANNED` (1 use in 1 file) | not ported |
//! | `Loader` (1 use in 1 file) | not ported: the loader is [`yaml`] and this module |

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Number, Value};
use yaml_rust2::parser::{Event, MarkedEventReceiver, Parser, Tag};
use yaml_rust2::scanner::{Marker, TScalarStyle};

use crate::repo;
use crate::yaml::{self, Node};

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

/// The handle yaml-rust2 gives a `!!` core tag, joined with its suffix.
const CORE_TAG: &str = "tag:yaml.org,2002:";

/// Every document in `text`, as JSON values. Panics with `origin` on a parse error or a
/// refusal (see the module doc). In a test, a file this loader cannot read is a failure.
pub fn parse(text: &str, origin: &str) -> Vec<Value> {
    let mut builder = Builder {
        origin: origin.to_owned(),
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

/// A plain untagged scalar's value. `key` names the mapping key the scalar sits under, for a
/// refusal's message.
fn plain_value(text: &str, origin: &str, key: &str) -> Value {
    match yaml::resolve_plain(text) {
        Node::Bool(b) => Value::Bool(b),
        Node::Null => Value::Null,
        Node::Str(_) => number_or_text(text, origin, key),
        Node::Seq(_) | Node::Map(_) => unreachable!("resolve_plain reads one scalar"),
    }
}

/// PyYAML tries the float, int and timestamp resolvers in turn; a text matches at most one.
fn number_or_text(text: &str, origin: &str, key: &str) -> Value {
    if FLOAT.is_match(text) {
        float_value(text, origin, key)
    } else if INT.is_match(text) {
        int_value(text, origin, key)
    } else if TIMESTAMP.is_match(text) {
        refuse(origin, key, "a timestamp is not a JSON value", Some(text))
    } else {
        Value::String(text.to_owned())
    }
}

/// PyYAML `construct_yaml_float`: underscores dropped, lower-cased, a sign, `.inf`/`.nan`, and
/// base-60 digits joined by colons.
fn float_value(text: &str, origin: &str, key: &str) -> Value {
    let cleaned = text.replace('_', "").to_lowercase();
    let (negative, body) = split_sign(&cleaned);
    if body == ".inf" || body == ".nan" {
        refuse(
            origin,
            key,
            "infinity and NaN are not JSON values",
            Some(text),
        );
    }
    let magnitude = if body.contains(':') {
        // PyYAML reverses the digits, then sums each times a base that grows by 60.
        body.split(':')
            .rev()
            .fold((0.0_f64, 1.0_f64), |(total, base), part| {
                let digit = part
                    .parse::<f64>()
                    .unwrap_or_else(|_| refuse(origin, key, "not a number", Some(text)));
                (total + digit * base, base * 60.0)
            })
            .0
    } else {
        body.parse::<f64>()
            .unwrap_or_else(|_| refuse(origin, key, "not a number", Some(text)))
    };
    let value = if negative { -magnitude } else { magnitude };
    Number::from_f64(value)
        .map(Value::Number)
        .unwrap_or_else(|| refuse(origin, key, "not a finite JSON number", Some(text)))
}

/// PyYAML `construct_yaml_int`: underscores dropped, a sign, then binary, hex, octal (a leading
/// zero), base-60 (colons) or decimal. The result must fit an `i64`.
fn int_value(text: &str, origin: &str, key: &str) -> Value {
    let cleaned = text.replace('_', "");
    let (negative, body) = split_sign(&cleaned);
    let magnitude = if body == "0" {
        0
    } else if let Some(bits) = body.strip_prefix("0b") {
        parse_int(bits, 2, origin, key, text)
    } else if let Some(hex) = body.strip_prefix("0x") {
        parse_int(hex, 16, origin, key, text)
    } else if body.starts_with('0') {
        parse_int(body, 8, origin, key, text)
    } else if body.contains(':') {
        base60_int(body, origin, key, text)
    } else {
        parse_int(body, 10, origin, key, text)
    };
    let signed = if negative { -magnitude } else { magnitude };
    i64::try_from(signed).map(Value::from).unwrap_or_else(|_| {
        refuse(
            origin,
            key,
            "an integer beyond i64 is not a JSON value",
            Some(text),
        )
    })
}

fn parse_int(digits: &str, radix: u32, origin: &str, key: &str, text: &str) -> i128 {
    i128::from_str_radix(digits, radix)
        .unwrap_or_else(|_| refuse(origin, key, "not an integer", Some(text)))
}

/// Base-60 digits, the last one least significant (PyYAML reverses them).
fn base60_int(body: &str, origin: &str, key: &str, text: &str) -> i128 {
    body.split(':')
        .rev()
        .try_fold((0_i128, 1_i128), |(total, base), part| {
            let digit = part.parse::<i128>().ok()?;
            Some((
                total.checked_add(digit.checked_mul(base)?)?,
                base.checked_mul(60)?,
            ))
        })
        .map(|(total, _)| total)
        .unwrap_or_else(|| refuse(origin, key, "not a base-60 integer within i128", Some(text)))
}

fn split_sign(text: &str) -> (bool, &str) {
    match text.as_bytes().first() {
        Some(b'-') => (true, &text[1..]),
        Some(b'+') => (false, &text[1..]),
        _ => (false, text),
    }
}

/// What a finished node is, for a refusal that cannot quote its text.
#[derive(Clone)]
enum Shape {
    Scalar(String),
    Sequence,
    Mapping,
}

impl Shape {
    fn kind(&self) -> &'static str {
        match self {
            Shape::Scalar(_) => "a scalar",
            Shape::Sequence => "a sequence",
            Shape::Mapping => "a mapping",
        }
    }
}

/// A finished node and what a refusal can name about it.
#[derive(Clone)]
struct Item {
    value: Value,
    shape: Shape,
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
struct Builder {
    origin: String,
    docs: Vec<Value>,
    stack: Vec<Frame>,
    anchors: HashMap<usize, Item>,
}

impl Builder {
    fn key_position(&self) -> bool {
        matches!(self.stack.last(), Some(Frame::Map { key: None, .. }))
    }

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

    fn refuse_tag(&self, tag: Option<&Tag>, own: &str, scalar: Option<&str>) {
        let Some(tag) = tag else { return };
        let full = format!("{}{}", tag.handle, tag.suffix);
        let reason = match full.strip_prefix(CORE_TAG) {
            Some(name) => format!("core tag `!!{name}` is not supported by the playbook loader"),
            None => format!("application tag `{full}` is not supported by the playbook loader"),
        };
        refuse(&self.origin, &self.key_label(own), &reason, scalar);
    }

    /// Places a finished node in its parent: a document, a sequence item, a merge value or a
    /// mapping entry. A mapping key must be a string.
    fn push(&mut self, item: Item, is_merge_key: bool) {
        let origin = self.origin.clone();
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
                None => match (item.value, &item.shape) {
                    (Value::String(k), _) => *key = Some(Pending::Named(k)),
                    (_, Shape::Scalar(text)) => refuse(
                        &origin,
                        text,
                        "a mapping key must be a string",
                        Some(text.as_str()),
                    ),
                    (_, shape) => refuse(
                        &origin,
                        "<non-scalar key>",
                        &format!("a mapping key must be a string, found {}", shape.kind()),
                        None,
                    ),
                },
                Some(Pending::Merge) => merges.push(item.value),
                Some(Pending::Named(k)) => entries.push((k, item.value)),
            },
        }
    }

    fn finish(&mut self, item: Item, anchor: usize) {
        if anchor > 0 {
            self.anchors.insert(anchor, item.clone());
        }
        self.push(item, false);
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
        let maps: Vec<Value> = match merge {
            Value::Object(_) => vec![merge],
            Value::Array(items) => items.into_iter().rev().collect(),
            other => refuse(
                origin,
                "<<",
                "a merge value must be a mapping or a list of mappings",
                Some(other.to_string().as_str()),
            ),
        };
        for map in maps {
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

impl MarkedEventReceiver for Builder {
    fn on_event(&mut self, ev: Event, _mark: Marker) {
        match ev {
            Event::Scalar(text, style, anchor, tag) => {
                self.refuse_tag(tag.as_ref(), &text, Some(text.as_str()));
                let plain = style == TScalarStyle::Plain;
                let is_merge_key = plain && self.key_position() && text == "<<";
                let value = if plain {
                    let label = self.key_label(&text);
                    plain_value(&text, &self.origin, &label)
                } else {
                    Value::String(text.clone())
                };
                let item = Item {
                    value,
                    shape: Shape::Scalar(text),
                };
                if anchor > 0 {
                    self.anchors.insert(anchor, item.clone());
                }
                self.push(item, is_merge_key);
            }
            Event::Alias(id) => {
                let item = self
                    .anchors
                    .get(&id)
                    .cloned()
                    .expect("alias to a known anchor");
                self.push(item, false);
            }
            Event::SequenceStart(anchor, tag) => {
                self.refuse_tag(tag.as_ref(), "<collection>", None);
                self.stack.push(Frame::Seq {
                    items: vec![],
                    anchor,
                });
            }
            Event::MappingStart(anchor, tag) => {
                self.refuse_tag(tag.as_ref(), "<collection>", None);
                self.stack.push(Frame::Map {
                    entries: vec![],
                    merges: vec![],
                    key: None,
                    anchor,
                });
            }
            Event::SequenceEnd => {
                if let Some(Frame::Seq { items, anchor }) = self.stack.pop() {
                    self.finish(
                        Item {
                            value: Value::Array(items),
                            shape: Shape::Sequence,
                        },
                        anchor,
                    );
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
                    let merged = apply_merges(entries, merges, &self.origin);
                    self.finish(
                        Item {
                            value: Value::Object(merged),
                            shape: Shape::Mapping,
                        },
                        anchor,
                    );
                }
            }
            _ => {}
        }
    }
}
