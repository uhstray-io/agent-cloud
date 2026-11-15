//! The agent-cloud test suite: one binary, one module per area (plan 24).
//!
//! Run everything: `cargo nextest run` from `platform/tests/`.
//! Run one area:   `cargo nextest run -E 'test(/^restart_policy::/)'`.

mod approle_ttl;
mod bootstrap_12a;
mod common_sh;
mod configure_host_network;
mod deploy_playbooks_preflight;
mod host_access_gate;
mod install_podman;
mod install_qemu_guest_agent;
mod install_qemu_guest_agent_playbook;
mod lanes;
mod ledger;
mod ledger_citation_hook;
mod local_dev_inventory;
mod local_dev_setup;
mod local_netbox;
mod makefile;
mod manage_github_runner_group;
mod restart_policy;
mod support;
mod support_playbook;
mod yaml_load;
