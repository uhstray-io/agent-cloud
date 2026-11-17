//! The agent-cloud test suite: one binary, one module per area (plan 24).
//!
//! Run everything: `cargo nextest run` from `platform/tests/`.
//! Run one area:   `cargo nextest run -E 'test(/^restart_policy::/)'`.
//!
//! The modules are the files in this directory: `build.rs` declares one module per
//! `suite/<name>.rs`, so adding a test module edits no shared file. List the module in
//! `areas.yml` or in `areas.d/<area>.yml` (the `lanes` test checks it is listed once).

include!(concat!(env!("OUT_DIR"), "/suite_modules.rs"));
