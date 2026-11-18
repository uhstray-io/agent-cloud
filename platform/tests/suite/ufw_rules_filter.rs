//! The ufw filter plugin exports only filters a playbook calls (static tier of the firewall
//! group, plan 24).
//!
//! The tag has one definition, reached through `ufw_desired_rules`; a filter no playbook calls
//! is a second, untested way in. `ufw_tag` stays a module function for the builders. The
//! filters' cases run through Ansible in `ufw_rules_filter_playbook.rs`, whose `FILTERS` must
//! name exactly the exported filters.
//!
//! ## Port map (pytest -> Rust fn)
//!
//! | pytest (`test_ufw_rules_filter.py`) | Rust fn |
//! |---|---|
//! | test_every_exported_filter_is_called_by_a_playbook | `every_exported_filter_is_called_by_a_playbook` |
//! | (none: guards the text read below) | `a_quoted_key_of_any_shape_counts_as_exported` |
//! | (none: guards the text read below) | `a_dict_shape_the_reader_cannot_read_refuses` |
//! | (none: guards the text read below) | `a_filters_method_the_reader_cannot_read_refuses` |
//!
//! The pytest read `FilterModule().filters()` in-process, so it saw every key; here the names are
//! the quoted keys of the dict literal `filters()` returns, read from the plugin's text, whatever
//! their shape, and a plugin this text read cannot follow refuses. Like the pytest, a name
//! counts as called when it appears anywhere in a
//! `platform/playbooks/**/*.yml` file.

use agent_cloud_tests::{repo, text};
use regex::Regex;

const PLUGIN: &str = "platform/playbooks/filter_plugins/ufw_rules.py";

/// The keys of the dict `FilterModule.filters` returns, in source order.
fn exported() -> Vec<String> {
    let source = text::read(PLUGIN);
    filter_literal(&source).and_then(dict_keys).unwrap_or_else(|| {
        panic!("{PLUGIN}: FilterModule.filters is not one `return {{\"key\": function, ...}}` ending the file; read its keys another way")
    })
}

/// The body of the dict literal `FilterModule.filters` returns, or `None` unless the method is
/// that one `return {...}` and nothing follows it in the file. Python keeps the last definition,
/// so this is the method Ansible calls; another statement first, a second `filters`, or a merge
/// or conditional after the `}` would export names this does not read, and refuses instead.
fn filter_literal(source: &str) -> Option<&str> {
    let (_, method) = source.split_once("\nclass FilterModule:\n    def filters(self):\n")?;
    let (literal, rest) = method
        .trim_start()
        .strip_prefix("return {")?
        .split_once('}')?;
    rest.trim().is_empty().then_some(literal)
}

/// The quoted keys of a Python dict literal's body, in order, or `None` when the body holds
/// anything but `"key": name` pairs. A key may be any text without a quote or backslash, so a key
/// a playbook could not call still counts. A shape this cannot read (a `**` splat, a key held in
/// a variable, a value with a brace, an escape) refuses rather than drops a key the pytest saw.
fn dict_keys(literal: &str) -> Option<Vec<String>> {
    let pairs = Regex::new(r#"^\s*(?:["'][^"'\\]+["']\s*:\s*[A-Za-z_]\w*\s*,?\s*)*$"#).unwrap();
    if !pairs.is_match(literal) {
        return None;
    }
    let key = Regex::new(r#"["']([^"'\\]+)["']\s*:"#).unwrap();
    Some(
        key.captures_iter(literal)
            .map(|c| c[1].to_string())
            .collect(),
    )
}

#[test]
fn a_quoted_key_of_any_shape_counts_as_exported() {
    let literal = r#""ufw_address": ufw_address, 'ufw_tag': f,
                "ufw-unused": g, "ufw.dot": h, "9lead": i"#;
    assert_eq!(
        dict_keys(literal).unwrap(),
        ["ufw_address", "ufw_tag", "ufw-unused", "ufw.dot", "9lead"]
    );
}

#[test]
fn a_dict_shape_the_reader_cannot_read_refuses() {
    for literal in [
        r#""ufw_a": f, **BASE"#,
        r#""ufw_a": f, KEYNAME: g"#,
        r#""ufw_a": lambda: {"#,
        r#""ufw\x61": f"#,
    ] {
        assert_eq!(dict_keys(literal), None, "{literal}");
    }
}

#[test]
fn a_filters_method_the_reader_cannot_read_refuses() {
    let head = "import re\n\nclass FilterModule:\n    def filters(self):\n";
    let plain = format!("{head}        return {{\"a\": f}}\n");
    assert_eq!(filter_literal(&plain), Some(r#""a": f"#));
    for body in [
        "        if X:\n            return {\"a\": f}\n        return {\"b\": g}\n",
        "        d = {\"a\": f}\n        return d\n",
        "        return {\"a\": f} | {\"b\": g}\n",
        "        return {\"a\": f} | EXTRA\n",
        "        return {\"a\": f} if X else {\"b\": g}\n",
        "        return {\"a\": f}\n\n    def filters(self):\n        return {\"b\": g}\n",
    ] {
        assert_eq!(filter_literal(&format!("{head}{body}")), None, "{body}");
    }
}

#[test]
fn every_exported_filter_is_called_by_a_playbook() {
    let names = exported();
    assert!(!names.is_empty(), "{PLUGIN}: filters() exports nothing");
    assert!(
        !names.iter().any(|n| n == "ufw_tag"),
        "ufw_tag must stay a module function, not an exported filter"
    );
    // FILTERS is pinned to the exported names here; the playbook module calls each FILTERS name.
    let mut listed: Vec<&str> = crate::ufw_rules_filter_playbook::FILTERS.to_vec();
    let mut sorted: Vec<&str> = names.iter().map(String::as_str).collect();
    listed.sort_unstable();
    sorted.sort_unstable();
    assert_eq!(
        sorted, listed,
        "the exported filters and the filters ufw_rules_filter_playbook.rs calls differ"
    );
    let playbooks: String = repo::ls_files(&[":(glob)platform/playbooks/**/*.yml"])
        .iter()
        .map(|rel| text::read(rel))
        .collect::<Vec<_>>()
        .join("\n");
    let uncalled: Vec<&String> = names
        .iter()
        .filter(|n| !playbooks.contains(n.as_str()))
        .collect();
    assert!(
        uncalled.is_empty(),
        "exported filters no playbook calls: {uncalled:?}"
    );
}
