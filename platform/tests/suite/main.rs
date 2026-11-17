//! The agent-cloud test suite: one binary, one module per area (plan 24).
//!
//! Run everything: `cargo nextest run` from `platform/tests/`.
//! Run one area:   `cargo nextest run -E 'test(/^restart_policy::/)'`.

mod approle_ttl;
mod backup_credentials;
mod become_password_resolution;
mod bootstrap_12a;
mod common_sh;
mod configure_host_network;
mod credential_leaks;
mod deploy_playbooks_preflight;
mod destroy_vm;
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
mod manage_secrets;
mod restart_policy;
mod support;
mod support_playbook;
mod yaml_load;
