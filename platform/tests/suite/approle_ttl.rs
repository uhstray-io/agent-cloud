//! Every credential is bounded in time and uses: no AppRole `secret_id_ttl` or `token_num_uses`
//! is 0 except the Semaphore orchestrator's marked exception (static tier).
//!
//! Guards PRINCIPLES.md Section 3, "Every credential is bounded in time and uses; TTL=0 is a
//! defect." The orchestrator is the sole documented unlimited-TTL exception; every other AppRole
//! is bounded (default 90d / 25 uses).
//!
//! Replaces platform/tests/test_approle_ttl.bats (plan 24, Phase 4).
//!
//! Port map (every BATS `@test` once):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | approle: no unbounded TTL=0 except the allow-listed orchestrator | `no_credential_ttl_is_zero_except_the_allow_listed_orchestrator` |
//! | approle: the orchestrator unlimited-TTL exception stays present + marked | `the_orchestrator_unlimited_ttl_exception_keeps_its_marker` |

use agent_cloud_tests::repo;

/// The marker a TTL=0 line carries, in a comment above it. A comment is the only place it can be:
/// the exemption is a note for a reader, and the scan reads the note beside the setting.
const MARKER: &str = "allow: orchestrator-unlimited-ttl";

const OPENBAO_DEPLOY: &str = "platform/services/openbao/deployment/deploy.sh";

/// The six lines above `line`, and `line` itself: the window the exemption is read from.
fn window_above(file: &str, line: usize) -> String {
    let text = std::fs::read_to_string(repo::path(file)).unwrap_or_else(|e| panic!("{file}: {e}"));
    let lines: Vec<&str> = text.lines().collect();
    let start = line.saturating_sub(6).max(1);
    lines[start - 1..line].join("\n")
}

#[test]
fn no_credential_ttl_is_zero_except_the_allow_listed_orchestrator() {
    // Arrange
    let pattern = r"secret_id_ttl[ =:]+0([^0-9]|$)|token_num_uses[ =:]+0([^0-9]|$)";

    // Act
    let hits = repo::grep(pattern, &["platform/playbooks", "platform/services"]);
    let mut offenders = vec![];
    for hit in &hits {
        // `grep -n` output: path, line, text. A setting with no exemption above it is an offender.
        let mut parts = hit.splitn(3, ':');
        let file = parts.next().unwrap_or_default();
        let line: usize = parts
            .next()
            .and_then(|n| n.parse().ok())
            .unwrap_or_else(|| panic!("unreadable grep line: {hit}"));
        let window = window_above(file, line);
        // The exemption is role-specific: the window must carry the marker and name the Semaphore
        // role, so a different role cannot slip through by pasting the marker alone.
        let exempt = window.contains(MARKER) && window.to_lowercase().contains("semaphore");
        if !exempt {
            offenders.push(format!("{file}:{line}"));
        }
    }

    // Assert
    assert!(
        offenders.is_empty(),
        "unbounded credential TTL without the '{MARKER}' marker naming Semaphore (PRINCIPLES.md \
         Section 3; bound it to 90d / 25 uses, or mark the orchestrator): {offenders:?}"
    );
}

#[test]
fn the_orchestrator_unlimited_ttl_exception_keeps_its_marker() {
    // Arrange
    let deploy = std::fs::read_to_string(repo::path(OPENBAO_DEPLOY))
        .unwrap_or_else(|e| panic!("{OPENBAO_DEPLOY}: {e}"));

    // Act
    let marked = deploy.contains(MARKER);

    // Assert
    assert!(
        marked,
        "the sanctioned unlimited-TTL exception lost its '{MARKER}' marker"
    );
}
