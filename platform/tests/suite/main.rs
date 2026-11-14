//! The agent-cloud test suite: one binary, one module per area (plan 24).
//!
//! Run everything: `cargo nextest run` from `platform/tests/`.
//! Run one area:   `cargo nextest run -E 'test(/^restart_policy::/)'`.

mod approle_ttl;
mod common_sh;
mod host_access_gate;
mod lanes;
mod ledger;
mod manage_github_runner_group;
mod restart_policy;
mod support;
