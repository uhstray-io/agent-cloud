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
//!
//! The pytest read `FilterModule().filters()` in-process; here the names are the keys of the
//! dict literal `filters()` returns, read from the plugin's text. Like the pytest, a name counts
//! as called when it appears anywhere in a `platform/playbooks/**/*.yml` file.

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
    // Either quote: Python accepts both, and a single-quoted key must not slip past.
    let key = Regex::new(r#"["']([A-Za-z_][A-Za-z0-9_]*)["']\s*:"#).unwrap();
    key.captures_iter(literal)
        .map(|c| c[1].to_string())
        .collect()
}

#[test]
fn every_exported_filter_is_called_by_a_playbook() {
    let names = exported();
    assert!(!names.is_empty(), "{PLUGIN}: filters() exports nothing");
    assert!(
        !names.iter().any(|n| n == "ufw_tag"),
        "ufw_tag must stay a module function, not an exported filter"
    );
    // The exported names equal the playbook module's FILTERS here, and that module requires a
    // call for each FILTERS name; together a new exported filter without a call fails.
    let mut listed: Vec<&str> = crate::ufw_rules_filter_playbook::FILTERS.to_vec();
    let mut sorted: Vec<&str> = names.iter().map(String::as_str).collect();
    listed.sort_unstable();
    sorted.sort_unstable();
    assert_eq!(
        sorted, listed,
        "the exported filters and the filters ufw_rules_filter_playbook.rs calls differ"
    );
    let playbooks: String = repo::ls_files(&["platform/playbooks/*.yml"])
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
