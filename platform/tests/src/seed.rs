//! The seed harness: the Rust side of `platform/tests/seed_harness.py` (AC-950). A seed test
//! answers its own HTTP requests with a stateful closure mounted on a [`http::Server`], and runs a
//! seed playbook against that server with [`run_seed`]. Every value is synthetic.
//!
//! | `seed_harness.py` | Rust |
//! |---|---|
//! | `FakeBao.record` | [`record`] |
//! | `FakeBao.requests` | [`recorded`] |
//! | `FakeBao.body` | [`json_body`] |
//! | `FakeBao.reply` | [`reply`] |
//! | `ROLE` | [`role`] |
//! | `LOGIN` | [`LOGIN`] |
//! | `NEVER_PRINTED` | [`NEVER_PRINTED`] |
//! | `run_seed` | [`run_seed`] |
//! | `ROOT` (the working directory) | [`crate::repo::root`], which [`process::ansible_playbook`] uses |
//! | `writes` | [`writes`] |
//! | `for value in (..., *NEVER_PRINTED)` in a test that runs ansible itself | [`assert_never_printed`] |
//!
//! A Python test subclasses `FakeBao` and keeps its state on the class. A Rust test mounts a
//! closure on the server instead, and keeps its state in what the closure captures (a `Mutex`
//! for state that changes between requests). Python's `DrainingHandler` has no counterpart:
//! wiremock reads the whole request body before it matches, so a reply never leaves bytes unread
//! (`src/http.rs`).
//!
//! Five Python tests run `ansible-playbook` themselves, without `run_seed`, and check
//! `NEVER_PRINTED` inline: `test_agw_conformance.py`, `test_agw_isolation_probe.py`,
//! `test_backup_step_ca.py`, `test_renew_internal_certs.py` and `test_rollback_inference_route.py`.
//! Once they are ported, each calls [`assert_never_printed`] on its output.
//!
//! Differences from the Python harness, on purpose:
//! - The run is confined and its environment is scrubbed. [`process::ansible_playbook`] sets the
//!   environment to [`process::playbook_env`], under a kernel sandbox where one exists. Python kept
//!   the caller's environment, minus the `SEED_*` inputs, `BAO_VALUE` and `OPENBAO_ADDR`.
//! - A timeout panics without the child's output ([`process::run`]). Python's `TimeoutExpired`
//!   carried the argv, and the argv holds the `-e` JSON with the role secret.
//! - A run killed by a signal panics. Python returned the `returncode`, which is negative for a
//!   signal.
//! - A leaked value panics naming its entry (`NEVER_PRINTED[i]`) or its input name, never the
//!   value. Python's bare `assert` named nothing.
//! - [`recorded`] counts every request, including one to a method or path no mock handles. Python's
//!   `record` ran only inside the `do_*` methods a test defined, so a request no `do_*` handled was
//!   never recorded. The Rust fake is stricter.
//! - `ANSIBLE_LOCAL_TEMP` is `.ansible-state/local` under the run's directory, from
//!   [`process::playbook_env`].
//! - [`writes`] does not count a DELETE, as `seed_harness.writes` does not. A DELETE changes the
//!   store too, so a test that needs one counted must count it itself.

use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};

use crate::http::{self, Request, ResponseTemplate};
use crate::{playbook, process};

/// The role id a seed run presents to the login (`seed_harness.ROLE`). Synthetic.
pub const ROLE_ID: &str = "synthetic-role";

/// The secret id a seed run presents to the login (`seed_harness.ROLE`). Synthetic.
pub const ROLE_SECRET: &str = "synthetic-role-secret";

/// The client token a fake login returns (`seed_harness.LOGIN`). Synthetic.
pub const LOGIN: &str = "synthetic-login-value";

/// The values a seed run must never print (`seed_harness.NEVER_PRINTED`). The last one is a value
/// a seed might leave behind, not one a run reads. `ROLE_ID` is also a substring of `ROLE_SECRET`,
/// so a printed secret names both entries.
pub const NEVER_PRINTED: [&str; 4] = [ROLE_ID, ROLE_SECRET, LOGIN, "synthetic-leftover-value"];

/// The AppRole credentials every seed run passes as extra vars (`seed_harness.ROLE`).
pub fn role() -> Map<String, Value> {
    let mut map = Map::new();
    map.insert("bao_role_id".to_owned(), json!(ROLE_ID));
    map.insert("bao_secret_id".to_owned(), json!(ROLE_SECRET));
    map
}

/// The method and the path of a request, as `FakeBao.record` stores them: the path with its query
/// string. Python's `self.path` carries the query; wiremock's `url.path()` drops it, so it is put
/// back here.
pub fn record(req: &Request) -> (String, String) {
    let mut path = req.url.path().to_owned();
    if let Some(query) = req.url.query() {
        path.push('?');
        path.push_str(query);
    }
    (req.method.as_str().to_owned(), path)
}

/// [`record`] over every request the server has received, in arrival order (`FakeBao.requests`).
/// Unmatched requests count: a server answers them with a 404 and records them.
pub fn recorded(server: &http::Server) -> Vec<(String, String)> {
    server.requests().iter().map(record).collect()
}

/// The requests that would change the store (`seed_harness.writes`): a PATCH or a PUT to any
/// path, and a POST under `/v1/secret/`. A DELETE is not counted, as in Python.
pub fn writes(reqs: &[(String, String)]) -> Vec<(String, String)> {
    reqs.iter()
        .filter(|(method, path)| {
            matches!(method.as_str(), "PATCH" | "PUT")
                || (method.as_str() == "POST" && path.starts_with("/v1/secret/"))
        })
        .cloned()
        .collect()
}

/// The request body as JSON (`FakeBao.body`): `{}` for an empty body. A body that is not JSON
/// panics naming the request's method and path, never the body, which can hold a secret.
pub fn json_body(req: &Request) -> Value {
    if req.body.is_empty() {
        return json!({});
    }
    serde_json::from_slice(&req.body).unwrap_or_else(|e| {
        let (method, path) = record(req);
        panic!("{method} {path}: the body is not JSON ({e})")
    })
}

/// A reply with `status` and `body` as JSON (`FakeBao.reply`): the body is serialized, and the
/// Content-Type is `application/json`.
pub fn reply(status: u16, body: &Value) -> ResponseTemplate {
    ResponseTemplate::new(status).set_body_json(body)
}

/// What a seed run is given: the keyword arguments of `seed_harness.run_seed`. `Default` is the
/// usual run, with the address declared, no inputs and no extra vars.
#[derive(Default)]
pub struct RunOpts {
    /// Extra `-e` vars. They override the AppRole credentials of [`role`] (`extra`).
    pub extra: Map<String, Value>,
    /// Seed inputs: process-environment variables whose values the run must never print
    /// (`inputs`).
    pub inputs: Vec<(String, String)>,
    /// `true` writes an inventory that declares no address and no vars (`declare=False`).
    pub undeclared: bool,
    /// `OPENBAO_ADDR` in the run's environment, when non-empty (`env_addr`).
    pub env_addr: Option<String>,
    /// `-v`, so ansible prints every task's result (`verbose`).
    pub verbose: bool,
    /// The address the inventory declares as `openbao_addr`, when it is not the server's and not
    /// empty (`declared`).
    pub declared: Option<String>,
    /// More `all.vars` in the inventory, after `openbao_addr` (`inventory_vars`).
    pub inventory_vars: Map<String, Value>,
}

/// Writes the inventory `run_seed` runs against. Declared: `openbao_addr` is `address`, or
/// `opts.declared`, followed by `opts.inventory_vars`. Undeclared (`opts.undeclared`): no vars at
/// all, so `inventory_vars` is dropped too, as Python's `declare=False` did.
fn write_inventory(dir: &Path, address: &str, opts: &RunOpts) -> PathBuf {
    let group = if opts.undeclared {
        json!({"hosts": {}})
    } else {
        let declared = opts
            .declared
            .as_deref()
            .filter(|declared| !declared.is_empty())
            .unwrap_or(address);
        let mut vars = Map::new();
        vars.insert("openbao_addr".to_owned(), json!(declared));
        for (name, value) in &opts.inventory_vars {
            vars.insert(name.clone(), value.clone());
        }
        json!({"vars": vars})
    };
    playbook::write(dir, "inventory.json", &json!({"all": group}))
}

/// Panics when `output` holds one of [`NEVER_PRINTED`], or the value of one of `extra`, a list of
/// `(name, value)` seed inputs. The panic names each entry that leaked, as `NEVER_PRINTED[i]` or by
/// its input name in backticks, and never a value or the output.
pub fn assert_never_printed(output: &str, extra: &[(&str, &str)]) {
    let mut leaked: Vec<String> = Vec::new();
    for (index, value) in NEVER_PRINTED.iter().enumerate() {
        if output.contains(*value) {
            leaked.push(format!("NEVER_PRINTED[{index}]"));
        }
    }
    for &(name, value) in extra {
        if output.contains(value) {
            leaked.push(format!("`{name}`"));
        }
    }
    assert!(
        leaked.is_empty(),
        "the output printed {}; the output itself is not shown",
        leaked.join(", ")
    );
}

/// Runs `playbook` once against the fake at `address` and returns its exit code and its output,
/// stdout then stderr (`seed_harness.run_seed`). `playbook` is a path, relative to the repository
/// root or absolute. The address is declared as `openbao_addr` in the inventory, as in production,
/// where the seed refuses any other (`tasks/assert-bao-addr-declared.yml`).
///
/// The output must hold none of [`NEVER_PRINTED`] and no input's value. A leak panics naming each
/// entry or input that leaked, never its value. A timeout panics without the child's output
/// ([`process::run`]).
pub fn run_seed(
    dir: &Path,
    playbook: impl AsRef<Path>,
    address: &str,
    opts: &RunOpts,
) -> (i32, String) {
    let inventory = write_inventory(dir, address, opts);
    let mut extra = role();
    for (name, value) in &opts.extra {
        extra.insert(name.clone(), value.clone());
    }
    let extra_json =
        serde_json::to_string(&Value::Object(extra)).expect("a JSON object serializes");

    let playbook = playbook.as_ref().display().to_string();
    let mut cmd = process::ansible_playbook(dir, &inventory.display().to_string(), &playbook);
    cmd.args(["-c", "local", "-e", extra_json.as_str()]);
    if opts.verbose {
        cmd.arg("-v");
    }
    // The stock callback on purpose: stricter than production's redact_requests (MISTAKES 4.6).
    cmd.env("ANSIBLE_STDOUT_CALLBACK", "default");
    for (name, value) in &opts.inputs {
        cmd.env(name, value);
    }
    if let Some(addr) = opts.env_addr.as_deref().filter(|addr| !addr.is_empty()) {
        cmd.env("OPENBAO_ADDR", addr);
    }
    let out = process::run(&mut cmd);
    let output = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );

    let inputs: Vec<(&str, &str)> = opts
        .inputs
        .iter()
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect();
    assert_never_printed(&output, &inputs);
    let code = out
        .status
        .code()
        .unwrap_or_else(|| panic!("the seed run was killed by a signal: {}", out.status));
    (code, output)
}
