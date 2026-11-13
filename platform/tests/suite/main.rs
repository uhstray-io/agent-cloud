//! The agent-cloud test suite: one binary, one module per area (plan 24).
//!
//! Run everything: `cargo nextest run` from `platform/tests/`.
//! Run one area:   `cargo nextest run -E 'test(/^restart_policy::/)'`.

mod common_sh;
mod install_qemu_guest_agent;
mod install_qemu_guest_agent_playbook;
mod lanes;
mod ledger;
mod restart_policy;
mod support;
mod support_playbook;
