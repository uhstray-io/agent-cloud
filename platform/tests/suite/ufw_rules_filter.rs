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
//!
//! The pytest read `FilterModule().filters()` in-process, so it saw every key; here the names are
//! the quoted keys of the dict literal `filters()` returns, read from the plugin's text, whatever
//! their shape. Like the pytest, a name counts as called when it appears anywhere in a
//! `platform/playbooks/**/*.yml` file.

use agent_cloud_tests::{repo, text};
use regex::Regex;

const PLUGIN: &str = "platform/playbooks/filter_plugins/ufw_rules.py";

/// The keys of the dict `FilterModule.filters` returns, in source order.
fn exported() -> Vec<String> {
    let source = text::read(PLUGIN);
    let body = source
        .split_once("    def filters(self):")
        .unwrap_or_else(|| panic!("{PLUGIN}: no FilterModule.filters"))
        .1;
    let literal = body
        .split_once("return {")
        .and_then(|(_, rest)| rest.split_once('}'))
        .unwrap_or_else(|| panic!("{PLUGIN}: filters() does not return a dict literal"))
        .0;
    dict_keys(literal).unwrap_or_else(|| {
        panic!("{PLUGIN}: filters() returns more than `\"key\": function` pairs; read its keys another way")
    })
}

/// The quoted keys of a Python dict literal's body, in order, or `None` when the body holds
/// anything but `"key": name` pairs. A key may be any text without a quote character, so a key
/// a playbook could not call still counts. A shape this cannot read (a `**` splat, a key held in
/// a variable, a value with a brace) refuses rather than drops a key the pytest would have seen.
fn dict_keys(literal: &str) -> Option<Vec<String>> {
    let pairs = Regex::new(r#"^\s*(?:["'][^"']+["']\s*:\s*[A-Za-z_]\w*\s*,?\s*)*$"#).unwrap();
    if !pairs.is_match(literal) {
        return None;
    }
    let key = Regex::new(r#"["']([^"']+)["']\s*:"#).unwrap();
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
    ] {
        assert_eq!(dict_keys(literal), None, "{literal}");
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
