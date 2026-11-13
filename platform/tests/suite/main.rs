//! The agent-cloud test suite: one binary, one module per area (plan 24).
//!
//! Run everything: `cargo nextest run` from `platform/tests/`.
//! Run one area:   `cargo nextest run -E 'test(/^restart_policy::/)'`.

mod backup_credentials;
mod become_password_resolution;
mod common_sh;
mod destroy_vm;
mod lanes;
mod ledger;
mod manage_secrets;
mod restart_policy;
mod support;
