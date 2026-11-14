//! The agent-cloud test suite: one binary, one module per area (plan 24).
//!
//! Run everything: `cargo nextest run` from `platform/tests/`.
//! Run one area:   `cargo nextest run -E 'test(/^restart_policy::/)'`.

mod common_sh;
mod deploy_playbooks_preflight;
mod lanes;
mod ledger;
mod ledger_citation_hook;
mod makefile;
mod restart_policy;
mod support;
