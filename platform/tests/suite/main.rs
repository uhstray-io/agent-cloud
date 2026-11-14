//! The agent-cloud test suite: one binary, one module per area (plan 24).
//!
//! Run everything: `cargo nextest run` from `platform/tests/`.
//! Run one area:   `cargo nextest run -E 'test(/^restart_policy::/)'`.

mod bootstrap_12a;
mod common_sh;
mod lanes;
mod ledger;
mod local_dev_inventory;
mod local_dev_setup;
mod restart_policy;
mod support;
