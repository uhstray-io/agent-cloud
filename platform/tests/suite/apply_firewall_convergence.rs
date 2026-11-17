//! The stub `ufw` of the firewall convergence harness reads back what it prints (cli tier of the
//! firewall group, plan 24).
//!
//! ufw prints a rule to and from any address as `allow [out] PORT[/PROTO]`, a route rule
//! included, so a delete by spec replays that form and the stub (`platform/tests/ufw_stub.py`,
//! a kept Python fixture) must accept it, but only as the whole rule. The pytest original called
//! the stub's private parser in process; a Rust test cannot import it, so this test is black-box
//! through the stub's command line: an accepted form shows by its effect on the stub's state
//! file, a refused one exits non-zero and leaves the state file as it was.
//!
//! ## Port map (pytest -> Rust fn)
//!
//! | pytest (`test_apply_firewall_convergence.py`) | Rust fn |
//! |---|---|
//! | `test_the_stub_takes_the_short_rule_form_only_as_a_whole_rule` | `the_stub_takes_the_short_rule_form_only_as_a_whole_rule` |

use std::path::Path;
use std::time::Duration;

use agent_cloud_tests::{process, repo};
use serde_json::{Value, json};
use tempfile::TempDir;

const STUB: &str = "platform/tests/ufw_stub.py";

/// Runs the stub as `ufw` with `words`, the state file holding `before`; returns whether it
/// succeeded, the state file's text before and after.
fn ufw(dir: &Path, words: &str, before: &Value) -> (bool, String, String) {
    let state = dir.join("ufw.json");
    let text = before.to_string();
    std::fs::write(&state, &text).unwrap();
    let mut cmd = process::scrubbed("python3", dir);
    cmd.arg(repo::path(STUB))
        .args(words.split_whitespace())
        .env("STUB_AS", "ufw")
        .env("UFW_STUB_STATE", &state)
        .env("UFW_STUB_LOG", dir.join("ufw.log"));
    let out = process::run_for(&mut cmd, Duration::from_secs(30));
    (
        out.status.success(),
        text,
        std::fs::read_to_string(&state).unwrap(),
    )
}

fn rules(text: &str) -> Value {
    serde_json::from_str::<Value>(text).unwrap()["rules"].clone()
}

#[test]
fn the_stub_takes_the_short_rule_form_only_as_a_whole_rule() {
    // (words, rules stored before, rules stored after)
    let accepted = [
        (
            "delete allow out 80/tcp",
            json!([["allow out 80/tcp", ""]]),
            json!([]),
        ),
        ("allow 443", json!([]), json!([["allow 443", ""]])),
        (
            "route delete allow 9000/tcp",
            json!([["route allow 9000/tcp", ""]]),
            json!([]),
        ),
        (
            "allow out 53/udp comment x",
            json!([]),
            json!([["allow out 53/udp", "x"]]),
        ),
    ];
    for (words, before, after) in accepted {
        let dir = TempDir::new().unwrap();
        let (ok, _, state) = ufw(dir.path(), words, &json!({"active": true, "rules": before}));
        assert!(ok, "{words}: must be accepted");
        assert_eq!(rules(&state), after, "{words}");
    }
    let held = json!({"active": true, "rules": [["allow 22/tcp", ""]]});
    for words in [
        "allow proto tcp 443",
        "allow 192.0.2.1",
        "allow 443 to any",
        "allow 443 22",
        "allow 80/icmp",
    ] {
        let dir = TempDir::new().unwrap();
        let (ok, before, after) = ufw(dir.path(), words, &held);
        assert!(!ok, "{words}: must be refused");
        assert_eq!(after, before, "{words}: a refused form changes nothing");
    }
}
