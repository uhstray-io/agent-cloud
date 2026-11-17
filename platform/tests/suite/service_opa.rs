//! The static tier of the OPA policy engine (`platform/services/opa`, plan 24, Phase 4).
//!
//! Verifies the composable shape of the service: an env-parameterized compose file with the pinned
//! multi-arch image and the read-only policy mount; a container-only `deploy.sh` (no secret
//! generation); policy-as-code (Rego plus `data.json`, with the catalog under its own namespace); the
//! listener-TLS overlay, which repeats every base command argument and adds the three TLS flags
//! before `/policies`; the `--authorization=basic` and `--bundle /policies` ending of both command
//! lists; the deploy playbook's refusal probes, TLS gates and verify; and the authorization policy's
//! two admitted routes.
//!
//! Replaces `platform/tests/test_service_opa.bats` (plan 24, Phase 4). Structural only: file reads
//! and the text of `deploy-opa.yml`, no live deploy. A BATS `[[ ]]` or a negated command that was not
//! its test's last statement could not fail (CONTRIBUTING.md, "Writing BATS Tests"); here every check
//! is a panic when it does not hold, wherever it sits in its test.
//!
//! One place the port reads less than the BATS file did: the policy-tree test reads the tracked files
//! (`git ls-files`), where BATS `find` also read untracked files in a working tree. CI checks out the
//! committed tree and production clones it, so a stray data file is caught once it is committed. In
//! local-dev the placement copies the working tree (`tasks/place-monorepo.yml`, per AGENTS.md), and an
//! untracked file there is not seen by this test.
//!
//! Port map (BATS @test → Rust fn):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | `opa: compose env-parameterizes image + bind/port, mounts policies read-only` | `compose_env_parameterizes_image_bind_port_and_mounts_policies_read_only` |
//! | `opa: prod default image is the multi-arch -static tag (arm64-safe), pinned` | `prod_default_image_is_the_pinned_multi_arch_static_tag` |
//! | `opa: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets` | `deploy_sh_is_executable_bash_sources_common_and_holds_no_secrets` |
//! | `opa: local overlay adds caps/SELinux/local-dev but does NOT republish ports` | `local_overlay_adds_limit_selinux_and_network_but_no_ports` |
//! | `opa: policy-as-code present (rules + static data + tests)` | `policy_as_code_is_present_with_rules_data_and_tests` |
//! | `opa: static data lives under a separate namespace (recursion-safe)` | `static_data_lives_under_the_catalog_namespace` |
//! | `opa: data.json is valid JSON with the agent catalogs` | `data_json_parses_with_the_agent_catalogs` |
//! | `opa tls overlay: keep-id to uid 1000, ./certs mounted read-only as a directory` | `tls_overlay_keeps_the_uid_and_mounts_certs_read_only` |
//! | `opa tls overlay: command repeats every compose.yml argument, adds the three TLS flags before /policies` | `tls_overlay_command_repeats_the_base_and_adds_three_tls_flags` |
//! | `opa: certs/ is gitignored, so the issued leaf and bundle are neither committed nor deleted by the rsync` | `certs_is_gitignored` |
//! | `opa tls: the server leaf is issued from step-ca after the repo is placed, only under opa_tls` | `server_leaf_is_minted_after_placement_only_under_opa_tls` |
//! | `opa tls: the step-ca trust bundle is placed in ./certs, only under opa_tls` | `trust_bundle_is_placed_in_certs_only_under_opa_tls` |
//! | `opa tls: deploy.sh gets compose.tls.yml and the leaf name only under opa_tls` | `deploy_gets_the_tls_overlay_and_leaf_name_only_under_opa_tls` |
//! | `opa tls: the declaration must name the leaf, its profile, dir, reload and the verify URL` | `declaration_names_the_leaf_profile_dir_reload_and_verify_url` |
//! | `opa tls: the verify probes HTTPS through curl against the step-ca bundle, and refuses a plain-HTTP 200` | `verify_probes_https_through_curl_and_refuses_a_plain_http_200` |
//! | `opa tls: the TLS decisions are gated on opa_tls and the plain ones on its absence` | `decisions_are_gated_on_opa_tls_and_the_plain_ones_on_its_absence` |
//! | `opa: a production deploy requires listener TLS and an https verify URL` | `production_deploy_requires_listener_tls_and_an_https_verify_url` |
//! | `opa: the verify runs the Rego tests as a bundle, the way the server loads them` | `verify_runs_the_rego_tests_as_a_bundle` |
//! | `opa: every data file in the policy tree is one bundle mode loads (data.json / data.yaml)` | `every_data_file_in_the_policy_tree_is_one_bundle_mode_loads` |
//! | `opa: the verify probes one list of three refusals (policy write, data read, decision with explain), each with its method, path and body` | `verify_probes_one_list_of_three_refusals` |
//! | `opa: both refusal tasks loop over the one probe list, and the six per-probe tasks are gone` | `both_refusal_tasks_loop_over_the_one_probe_list` |
//! | `opa: the plain refusal task fails on any status but 401 or any code but unauthorized, and sends no body on a GET` | `plain_refusal_fails_on_any_status_but_401_unauthorized` |
//! | `opa tls: the TLS refusal task drops --fail through the probe args and passes only on a last-line 401 with code unauthorized` | `tls_refusal_drops_fail_and_passes_only_on_a_last_line_401` |
//! | `opa authz: a decision with query parameters is refused, explain and instrument alike (the rule and its unit tests)` | `authz_refuses_a_decision_with_query_parameters` |
//! | `opa authz: the policy admits exactly the decision POST and the health GET, and its tests exist` | `authz_admits_exactly_the_decision_post_and_the_health_get` |
//! | `opa: both command lists pass --authorization=basic, directly before --bundle` | `both_command_lists_pass_authorization_basic_directly_before_bundle` |
//! | `opa: deploy.sh recreates OPA when the policies it loads at startup change` | `deploy_sh_recreates_opa_when_the_policies_it_loads_change` |

use std::os::unix::fs::PermissionsExt;

use agent_cloud_tests::repo;
use agent_cloud_tests::text::{any_line, read};
use agent_cloud_tests::yaml;
use regex::Regex;

const COMPOSE: &str = "platform/services/opa/deployment/compose.yml";
const COMPOSE_LOCAL: &str = "platform/services/opa/deployment/compose.local.yml";
const COMPOSE_TLS: &str = "platform/services/opa/deployment/compose.tls.yml";
const DEPLOY_SH: &str = "platform/services/opa/deployment/deploy.sh";
const GITIGNORE: &str = "platform/services/opa/deployment/.gitignore";
const POLICIES: &str = "platform/services/opa/deployment/policies";
const AGENT_ACTIONS: &str =
    "platform/services/opa/deployment/policies/agentcloud/agent_actions.rego";
const AGENT_ACTIONS_TEST: &str =
    "platform/services/opa/deployment/policies/agentcloud/agent_actions_test.rego";
const DATA_JSON: &str = "platform/services/opa/deployment/policies/agentcloud/data.json";
const AUTHZ: &str = "platform/services/opa/deployment/policies/system/authz.rego";
const AUTHZ_TEST: &str = "platform/services/opa/deployment/policies/system/authz_test.rego";
const PLAYBOOK: &str = "platform/playbooks/deploy-opa.yml";

/// `[ -f ]`: `rel` is a regular file in the checkout.
fn assert_file(rel: &str) {
    assert!(repo::path(rel).is_file(), "{rel} is not a file");
}

/// The script at `rel` is executable and its first line is `#!/usr/bin/env bash`.
fn assert_bash_script(rel: &str) {
    let meta = std::fs::metadata(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"));
    assert!(
        (meta.permissions().mode() & 0o111) != 0,
        "{rel} is not executable"
    );
    assert!(
        read(rel).starts_with("#!/usr/bin/env bash"),
        "{rel}: the first line is not `#!/usr/bin/env bash`"
    );
}

/// The regex `pattern`, compiled. A pattern that does not compile panics with itself.
fn re(pattern: &str) -> Regex {
    Regex::new(pattern).unwrap_or_else(|e| panic!("bad pattern `{pattern}`: {e}"))
}

// The assertions below take the text they test, so one helper serves a whole file and an awk
// chunk alike. `label` names that text in a failure. Each mirrors one grep form of the BATS file.

/// `grep -qE pattern`: some line of `text` matches the regex.
fn assert_re(label: &str, text: &str, pattern: &str) {
    assert!(
        any_line(text, pattern),
        "{label}: no line matches `{pattern}`"
    );
}

/// `! grep -qE pattern`: no line of `text` may match the regex.
fn refute_re(label: &str, text: &str, pattern: &str) {
    assert!(
        !any_line(text, pattern),
        "{label}: a line matches the forbidden `{pattern}`"
    );
}

/// `grep -qF literal`: `text` contains the literal.
fn assert_lit(label: &str, text: &str, literal: &str) {
    assert!(text.contains(literal), "{label}: `{literal}` is missing");
}

/// `! grep -qF literal`: `text` does not contain the literal.
fn refute_lit(label: &str, text: &str, literal: &str) {
    assert!(
        !text.contains(literal),
        "{label}: the forbidden `{literal}` is present"
    );
}

/// `grep -qxF literal`: some whole line of `text` equals the literal.
fn assert_exact(label: &str, text: &str, literal: &str) {
    assert!(
        text.lines().any(|line| line == literal),
        "{label}: no line is exactly `{literal}`"
    );
}

/// `grep -c pattern`: how many lines of `text` match the regex.
fn count_re(text: &str, pattern: &str) -> usize {
    let re = re(pattern);
    text.lines().filter(|line| re.is_match(line)).count()
}

/// `grep -n pattern | head -n 1 | cut -d: -f1`: the 1-based number of the first matching line.
fn first_line(text: &str, pattern: &str) -> Option<usize> {
    let re = re(pattern);
    text.lines()
        .position(|line| re.is_match(line))
        .map(|index| index + 1)
}

/// The awk `command_args`: the `      - ` items of a service's `command:` list, the dash removed.
/// The list ends at the next four-space key, so a later service's list is never read.
fn command_args(text: &str) -> Vec<String> {
    let key = re(r"^    [a-z_]+:");
    let mut in_command = false;
    let mut args = Vec::new();
    for line in text.lines() {
        if line.starts_with("    command:") {
            in_command = true;
            continue;
        }
        if in_command && key.is_match(line) {
            in_command = false;
        }
        if !in_command {
            continue;
        }
        if let Some(arg) = line.strip_prefix("      - ") {
            args.push(arg.to_owned());
        }
    }
    args
}

/// The awk `task_with`: the first `    - name:` task of a playbook whose text matches `pattern`.
/// A task runs from its `- name:` line to the next one; the text before the first task is a chunk
/// too. Panics, naming the pattern, when no chunk matches.
fn task_with(text: &str, pattern: &str) -> String {
    let pat = re(pattern);
    let mut chunks = Vec::new();
    let mut current = String::new();
    for line in text.lines() {
        if line.starts_with("    - name:") {
            chunks.push(std::mem::take(&mut current));
        }
        current.push_str(line);
        current.push('\n');
    }
    chunks.push(current);
    chunks
        .into_iter()
        .find(|chunk| pat.is_match(chunk))
        .unwrap_or_else(|| panic!("no task matches `{pattern}`"))
}

/// `sed -n '/start/,/end/p'`: each line from a line matching `start` through the next line matching
/// `end`, both included. The end is not tested on the line that opened the range; a range still open
/// at the last line runs to the end of the text.
fn sed_range(text: &str, start: &str, end: &str) -> String {
    let (start, end) = (re(start), re(end));
    let mut out = String::new();
    let mut open = false;
    for line in text.lines() {
        if open {
            out.push_str(line);
            out.push('\n');
            if end.is_match(line) {
                open = false;
            }
        } else if start.is_match(line) {
            out.push_str(line);
            out.push('\n');
            open = true;
        }
    }
    out
}

#[test]
fn compose_env_parameterizes_image_bind_port_and_mounts_policies_read_only() {
    let compose = read(COMPOSE);
    assert_re(COMPOSE, &compose, r"\$\{OPA_IMAGE");
    assert_re(COMPOSE, &compose, r"\$\{OPA_BIND");
    assert_re(COMPOSE, &compose, r"\$\{OPA_PORT");
    assert_lit(COMPOSE, &compose, "./policies:/policies:ro");
}

#[test]
fn prod_default_image_is_the_pinned_multi_arch_static_tag() {
    // The plain tag is amd64-only and crashes under emulation on the arm64 VM.
    let compose = read(COMPOSE);
    assert_re(
        COMPOSE,
        &compose,
        r"\$\{OPA_IMAGE:-docker\.io/openpolicyagent/opa:[0-9]+\.[0-9]+\.[0-9]+-static\}",
    );
}

#[test]
fn deploy_sh_is_executable_bash_sources_common_and_holds_no_secrets() {
    assert_bash_script(DEPLOY_SH);
    let deploy = read(DEPLOY_SH);
    assert_re(DEPLOY_SH, &deploy, "common.sh");
    assert_re(DEPLOY_SH, &deploy, r"\bcompose (pull|up)");
    refute_re(
        DEPLOY_SH,
        &deploy,
        r"\b(gen_secret|put_secret|get_secret|bao_)",
    );
}

#[test]
fn local_overlay_adds_limit_selinux_and_network_but_no_ports() {
    let local = read(COMPOSE_LOCAL);
    assert_re(COMPOSE_LOCAL, &local, "mem_limit:");
    assert_re(COMPOSE_LOCAL, &local, "label=disable");
    assert_re(COMPOSE_LOCAL, &local, "local-dev");
    refute_re(COMPOSE_LOCAL, &local, r"^[[:space:]]*ports:");
}

#[test]
fn policy_as_code_is_present_with_rules_data_and_tests() {
    assert_file(AGENT_ACTIONS);
    assert_file(AGENT_ACTIONS_TEST);
    assert_file(DATA_JSON);
    let rego = read(AGENT_ACTIONS);
    assert_re(AGENT_ACTIONS, &rego, "^package agentcloud$");
    assert_re(AGENT_ACTIONS, &rego, "default allow := false");
}

#[test]
fn static_data_lives_under_the_catalog_namespace() {
    // data.json is mounted at data.agentcloud.catalog (dir-based), referenced as such by the rules,
    // NOT as data.agentcloud[<agent>] (which recurses into the package rules). The 'catalog' wrapper
    // is the contract.
    let data = read(DATA_JSON);
    assert_lit(DATA_JSON, &data, "\"catalog\"");
    let rego = read(AGENT_ACTIONS);
    assert_re(
        AGENT_ACTIONS,
        &rego,
        r"data.agentcloud.catalog\[input.agent\]",
    );
    refute_re(AGENT_ACTIONS, &rego, r"data\.agentcloud\[input\.agent\]");
}

#[test]
fn data_json_parses_with_the_agent_catalogs() {
    // The BATS test read it with json.load. JSON is YAML for this file, so the suite's loader reads it.
    let doc = yaml::load_one(&repo::path(DATA_JSON)).expect("data.json is empty");
    let catalog = doc
        .get("catalog")
        .unwrap_or_else(|| panic!("{DATA_JSON}: no `catalog` key"));
    for agent in ["skynet", "infra-agent", "semaphore"] {
        assert!(
            catalog.get(agent).is_some(),
            "{DATA_JSON}: the catalog has no `{agent}`"
        );
    }
}

#[test]
fn tls_overlay_keeps_the_uid_and_mounts_certs_read_only() {
    let over = read(COMPOSE_TLS);
    assert_exact(
        COMPOSE_TLS,
        &over,
        r#"    userns_mode: "keep-id:uid=1000,gid=1000""#,
    );
    assert_exact(COMPOSE_TLS, &over, "      - ./certs:/certs:ro");
}

#[test]
fn tls_overlay_command_repeats_the_base_and_adds_three_tls_flags() {
    let base = read(COMPOSE);
    let over = read(COMPOSE_TLS);
    let base_args = command_args(&base);
    assert!(!base_args.is_empty(), "{COMPOSE}: no command list");
    for arg in &base_args {
        assert_exact(COMPOSE_TLS, &over, &format!("      - {arg}"));
    }
    assert_eq!(
        command_args(&over).len(),
        base_args.len() + 3,
        "{COMPOSE_TLS}: the overlay must add exactly three arguments to the base list"
    );
    assert_exact(
        COMPOSE_TLS,
        &over,
        "      - --tls-cert-file=/certs/${OPA_SERVER_LEAF:-opa-server}/current/cert.pem",
    );
    assert_exact(
        COMPOSE_TLS,
        &over,
        "      - --tls-private-key-file=/certs/${OPA_SERVER_LEAF:-opa-server}/current/key.pem",
    );
    assert_exact(COMPOSE_TLS, &over, "      - --tls-cert-refresh-period=30s");
    assert_eq!(
        command_args(&over).last().map(String::as_str),
        Some("/policies"),
        "{COMPOSE_TLS}: the command list must end with `/policies`"
    );
}

#[test]
fn certs_is_gitignored() {
    // Neither committed nor deleted by the rsync that places the repository.
    assert_exact(GITIGNORE, &read(GITIGNORE), "certs/");
}

#[test]
fn server_leaf_is_minted_after_placement_only_under_opa_tls() {
    let pb = read(PLAYBOOK);
    let place = first_line(&pb, "tasks/place-monorepo.yml");
    let mint = first_line(&pb, "tasks/mint-internal-cert.yml");
    assert!(place.is_some(), "{PLAYBOOK}: no tasks/place-monorepo.yml");
    assert!(
        mint.is_some(),
        "{PLAYBOOK}: no tasks/mint-internal-cert.yml"
    );
    assert!(
        mint > place,
        "{PLAYBOOK}: the leaf is minted (line {mint:?}) before the repo is placed (line {place:?})"
    );
    let task = task_with(&pb, "tasks/mint-internal-cert.yml");
    assert_exact(PLAYBOOK, &task, "      when: _opa_tls");
    assert_lit(PLAYBOOK, &task, "_mint_name: \"{{ _opa_server_leaf }}\"");
    assert_lit(
        PLAYBOOK,
        &task,
        "_mint_ca_host: \"{{ groups['step_ca_svc'] | first }}\"",
    );
    assert_lit(PLAYBOOK, &task, "_mint_reissue: false");
}

#[test]
fn trust_bundle_is_placed_in_certs_only_under_opa_tls() {
    let pb = read(PLAYBOOK);
    let task = task_with(&pb, "tasks/distribute-ca-root.yml");
    assert_exact(PLAYBOOK, &task, "      when: _opa_tls");
    assert_lit(PLAYBOOK, &task, "_ca_root_dest_dir: \"{{ _deploy_dir }}\"");
    assert_lit(
        PLAYBOOK,
        &task,
        "_ca_host: \"{{ groups['step_ca_svc'] | first }}\"",
    );
}

#[test]
fn deploy_gets_the_tls_overlay_and_leaf_name_only_under_opa_tls() {
    let pb = read(PLAYBOOK);
    let task = task_with(&pb, "Run deploy.sh");
    assert_lit(
        PLAYBOOK,
        &task,
        "COMPOSE_OVERLAYS: \"{{ 'compose.tls.yml' if _opa_tls else '' }}\"",
    );
    assert_lit(
        PLAYBOOK,
        &task,
        "OPA_SERVER_LEAF: \"{{ opa_server_leaf | default('opa-server') }}\"",
    );
}

#[test]
fn declaration_names_the_leaf_profile_dir_reload_and_verify_url() {
    let pb = read(PLAYBOOK);
    let decl = task_with(&pb, "Refuse opa_tls without its server leaf");
    assert_lit(PLAYBOOK, &decl, "_opa_leaf_decl[0].profile == 'server'");
    assert_lit(
        PLAYBOOK,
        &decl,
        "_opa_leaf_decl[0].dir == _deploy_dir ~ '/certs/' ~ _opa_server_leaf",
    );
    assert_lit(PLAYBOOK, &decl, "_opa_leaf_decl[0].reload == 'none'");
    assert_lit(PLAYBOOK, &decl, "_opa_tls_name in (_opa_leaf_decl[0].sans");
    assert_lit(
        PLAYBOOK,
        &decl,
        "opa_internal_url | default('', true) | string) == 'https://' ~ _opa_tls_name",
    );
    assert_lit(PLAYBOOK, &decl, "_opa_server_leaf is match(");
}

#[test]
fn verify_probes_https_through_curl_and_refuses_a_plain_http_200() {
    let pb = read(PLAYBOOK);
    let args = sed_range(&pb, "^    _opa_tls_args:", "^  tasks:");
    assert_exact(PLAYBOOK, &args, "      - --cacert");
    assert_lit(
        PLAYBOOK,
        &args,
        "\"{{ _deploy_dir }}/certs/step-ca-bundle.crt\"",
    );
    assert_exact(PLAYBOOK, &args, "      - --resolve");
    let health = task_with(&pb, "OPA health over TLS");
    assert_lit(PLAYBOOK, &health, "_opa_tls_args");
    assert_exact(PLAYBOOK, &health, "      when: _opa_tls");
    let plain = task_with(&pb, "Plain HTTP to the TLS listener");
    assert_lit(PLAYBOOK, &plain, "failed_when: false");
    let refuse = task_with(&pb, "Refuse a plain-HTTP 200");
    assert_lit(PLAYBOOK, &refuse, "trim != '200'");
}

#[test]
fn decisions_are_gated_on_opa_tls_and_the_plain_ones_on_its_absence() {
    let pb = read(PLAYBOOK);
    let uri = task_with(&pb, "ansible.builtin.uri:");
    assert_exact(PLAYBOOK, &uri, "      when: not _opa_tls");
    let plain_allow = task_with(&pb, "Sample decision: infra-agent read netbox -> allow");
    assert_exact(
        PLAYBOOK,
        &plain_allow,
        "      when: not ansible_check_mode and not _opa_tls",
    );
    let plain_deny = task_with(&pb, "Sample decision: unknown agent -> deny");
    assert_exact(
        PLAYBOOK,
        &plain_deny,
        "      when: not ansible_check_mode and not _opa_tls",
    );
    let tls_allow = task_with(&pb, "Sample decision over TLS: infra-agent");
    assert_exact(
        PLAYBOOK,
        &tls_allow,
        "      when: _opa_tls and not ansible_check_mode",
    );
    let tls_deny = task_with(&pb, "Sample decision over TLS: unknown agent");
    assert_exact(
        PLAYBOOK,
        &tls_deny,
        "      when: _opa_tls and not ansible_check_mode",
    );
}

#[test]
fn production_deploy_requires_listener_tls_and_an_https_verify_url() {
    let pb = read(PLAYBOOK);
    let prod = task_with(&pb, "Refuse a production deploy without its declared bind");
    assert_lit(PLAYBOOK, &prod, "- opa_tls | default(false) | bool");
    assert_lit(PLAYBOOK, &prod, "is match('^https://')");
    refute_lit(PLAYBOOK, &prod, "https?://");
    assert_lit(
        PLAYBOOK,
        &prod,
        "when: not (local_mode | default(false) | bool)",
    );
}

#[test]
fn verify_runs_the_rego_tests_as_a_bundle() {
    // Without --bundle, PUT /v1/data and /v1/policies (and DELETE /v1/data/agentcloud) succeed for
    // any caller the firewall admits; the :ro mount protects the files, not the running policy.
    // Both command lists ending in "--bundle /policies" is asserted by the --authorization test,
    // whose suffix includes it. The Rego tests load the tree the way the server does.
    let rego = task_with(&read(PLAYBOOK), "Run Rego unit tests");
    assert_lit(PLAYBOOK, &rego, "/opa test --bundle /policies\"");
}

#[test]
fn every_data_file_in_the_policy_tree_is_one_bundle_mode_loads() {
    // A plain load reads any .json/.yaml as data; --bundle reads only data.json/data.yaml. CI's
    // `opa test` still loads the tree plainly, so a stray data file would pass CI and be dropped at
    // runtime. Name it data.json in its own directory instead.
    let tracked = repo::ls_files(&[POLICIES]);
    assert!(!tracked.is_empty(), "no tracked files under {POLICIES}");
    let strays: Vec<&String> = tracked
        .iter()
        .filter(|path| {
            let name = path.rsplit('/').next().unwrap_or(path);
            let data = name.ends_with(".json") || name.ends_with(".yaml") || name.ends_with(".yml");
            data && name != "data.json" && name != "data.yaml"
        })
        .collect();
    assert!(
        strays.is_empty(),
        "data files that bundle mode would skip: {strays:?}"
    );
}

#[test]
fn verify_probes_one_list_of_three_refusals() {
    // The list is a play var, so it is read by range rather than by task. The write probe's path sits
    // outside every package a decision reads; only the explain probe reads a decision.
    let pb = read(PLAYBOOK);
    let probes = sed_range(
        &pb,
        "^    _opa_refusal_probes:",
        "^    _opa_tls_probe_args:",
    );
    assert_eq!(
        count_re(&probes, "^      - what: "),
        3,
        "{PLAYBOOK}: the refusal list must have three probes"
    );
    assert_exact(PLAYBOOK, &probes, "      - what: policy write");
    assert_exact(PLAYBOOK, &probes, "        method: PUT");
    assert_exact(
        PLAYBOOK,
        &probes,
        "        path: /v1/data/agentcloud_write_probe",
    );
    assert_exact(PLAYBOOK, &probes, "        body: {}");
    assert_exact(PLAYBOOK, &probes, "      - what: data read");
    assert_exact(PLAYBOOK, &probes, "        method: GET");
    assert_exact(PLAYBOOK, &probes, "        path: /v1/data");
    assert_exact(PLAYBOOK, &probes, "      - what: decision with explain");
    assert_exact(PLAYBOOK, &probes, "        method: POST");
    assert_exact(
        PLAYBOOK,
        &probes,
        "        path: /v1/data/agentcloud/decision?explain=full",
    );
    assert_exact(
        PLAYBOOK,
        &probes,
        "          input: {agent: infra-agent, action: read, service: netbox}",
    );
    // The write probe never writes under a package a decision reads, nor under the policy API.
    let write_entry = sed_range(&probes, "what: policy write", "what: data read");
    refute_re(PLAYBOOK, &write_entry, r"/v1/data/agentcloud/|/v1/policies");
    refute_re(PLAYBOOK, &probes, "/v1/policies");
}

#[test]
fn both_refusal_tasks_loop_over_the_one_probe_list() {
    let pb = read(PLAYBOOK);
    let plain = task_with(&pb, "Authorization refusals: policy write");
    assert_exact(
        PLAYBOOK,
        &plain,
        "      loop: \"{{ _opa_refusal_probes }}\"",
    );
    let tls = task_with(&pb, "Authorization refusals over TLS");
    assert_exact(PLAYBOOK, &tls, "      loop: \"{{ _opa_refusal_probes }}\"");
    assert_eq!(
        count_re(&pb, "^    - name: \"Authorization refusals"),
        2,
        "{PLAYBOOK}: the refusals are two tasks, plain and TLS"
    );
    refute_re(
        PLAYBOOK,
        &pb,
        r#"^    - name: "(Policy writes|Data reads|Decision with explain)"#,
    );
}

#[test]
fn plain_refusal_fails_on_any_status_but_401_unauthorized() {
    let pb = read(PLAYBOOK);
    let plain = task_with(&pb, "Authorization refusals: policy write");
    assert_exact(
        PLAYBOOK,
        &plain,
        "        url: \"{{ _opa_url }}{{ item.path }}\"",
    );
    assert_exact(PLAYBOOK, &plain, "        method: \"{{ item.method }}\"");
    assert_exact(PLAYBOOK, &plain, "        status_code: [401]");
    assert_lit(
        PLAYBOOK,
        &plain,
        "failed_when: _opa_refusal.status != 401 or (_opa_refusal.json.code | default('')) != 'unauthorized'",
    );
    // uri would JSON-encode an absent body as null, so json is set only where a probe has a body.
    assert_exact(
        PLAYBOOK,
        &plain,
        "        body_format: \"{{ 'json' if item.body is defined else omit }}\"",
    );
    assert_exact(
        PLAYBOOK,
        &plain,
        "        body: \"{{ item.body | default(omit) }}\"",
    );
    assert_exact(
        PLAYBOOK,
        &plain,
        "      when: not ansible_check_mode and not _opa_tls",
    );
    refute_lit(PLAYBOOK, &plain, "status_code: [400]");
}

#[test]
fn tls_refusal_drops_fail_and_passes_only_on_a_last_line_401() {
    let pb = read(PLAYBOOK);
    let tls = task_with(&pb, "Authorization refusals over TLS");
    assert_lit(PLAYBOOK, &tls, "{{ _opa_tls_probe_args");
    // The probe args are the TLS args without --fail, so the expected 401 exits 0.
    let vars = sed_range(&pb, "^    _opa_tls_args:", "^  tasks:");
    assert_exact(
        PLAYBOOK,
        &vars,
        "    _opa_tls_probe_args: \"{{ _opa_tls_args | reject('equalto', '--fail') | list }}\"",
    );
    refute_lit(PLAYBOOK, &tls, "_opa_tls_args");
    refute_lit(PLAYBOOK, &tls, "--fail");
    assert_lit(PLAYBOOK, &tls, "'--request', item.method");
    assert_lit(PLAYBOOK, &tls, r"'--write-out', '\n%{http_code}'");
    assert_lit(PLAYBOOK, &tls, "_opa_refusal_tls.rc != 0");
    assert_lit(
        PLAYBOOK,
        &tls,
        "(_opa_refusal_tls.stdout_lines | last) != '401'",
    );
    assert_lit(
        PLAYBOOK,
        &tls,
        r"(_opa_refusal_tls.stdout_lines[:-1] | join('\n') | from_json).code | default('')) != 'unauthorized'",
    );
    assert_exact(
        PLAYBOOK,
        &tls,
        "      when: _opa_tls and not ansible_check_mode",
    );
}

#[test]
fn authz_refuses_a_decision_with_query_parameters() {
    // ?explain=full returns the evaluation trace of the policy and its data, so the authorization
    // policy admits a decision only with no query parameters. Each refusal has a unit test.
    let authz = read(AUTHZ);
    let tests = read(AUTHZ_TEST);
    assert_lit(
        AUTHZ,
        &authz,
        r#"count(object.get(input, "params", {})) == 0"#,
    );
    assert_re(AUTHZ_TEST, &tests, "^test_deny_decision_explain if");
    assert_re(AUTHZ_TEST, &tests, "^test_deny_decision_instrument if");
}

#[test]
fn authz_admits_exactly_the_decision_post_and_the_health_get() {
    // Deny-by-default: the only two allow rules are the decision API and /health. Adding a third
    // changes what an admitted caller may reach, so it must be an explicit, reviewed change here.
    let authz = read(AUTHZ);
    let tests = read(AUTHZ_TEST);
    assert_exact(AUTHZ, &authz, "package system.authz");
    assert_exact(AUTHZ, &authz, "default allow := false");
    assert_eq!(
        count_re(&authz, "^allow if"),
        2,
        "{AUTHZ}: exactly two allow rules"
    );
    assert_lit(AUTHZ, &authz, "input.method == \"POST\"");
    assert_lit(
        AUTHZ,
        &authz,
        "input.path == [\"v1\", \"data\", \"agentcloud\", \"decision\"]",
    );
    assert_lit(AUTHZ, &authz, "input.method == \"GET\"");
    assert_lit(AUTHZ, &authz, "input.path == [\"health\"]");
    // The unit tests cover the admitted requests and the refused ones.
    assert_re(AUTHZ_TEST, &tests, "^test_allow_decision_post if");
    assert_re(AUTHZ_TEST, &tests, "^test_allow_health_get if");
    assert_re(AUTHZ_TEST, &tests, "^test_deny_policy_write_probe_put if");
    assert_re(AUTHZ_TEST, &tests, "^test_deny_data_root_get if");
    assert_re(AUTHZ_TEST, &tests, "^test_deny_metrics_get if");
}

#[test]
fn both_command_lists_pass_authorization_basic_directly_before_bundle() {
    // Without the flag the policy is never asked, and every request reaches the handlers.
    for rel in [COMPOSE, COMPOSE_TLS] {
        let args = command_args(&read(rel));
        let flags = args
            .iter()
            .filter(|arg| arg.as_str() == "--authorization=basic")
            .count();
        assert_eq!(flags, 1, "{rel}: `--authorization=basic` must appear once");
        // The list ends with the flag and then the bundle it guards.
        assert!(
            args.ends_with(&["--authorization=basic", "--bundle", "/policies"].map(String::from)),
            "{rel}: the command list must end `--authorization=basic --bundle /policies`"
        );
    }
}

#[test]
fn deploy_sh_recreates_opa_when_the_policies_it_loads_change() {
    // OPA loads ./policies only at startup; plain `compose up -d` kept a container serving old
    // policies after a deploy placed new ones (docs/MISTAKES.md 3.11).
    let deploy = read(DEPLOY_SH);
    assert_re(
        DEPLOY_SH,
        &deploy,
        "^[[:space:]]*compose_up_if_changed policies$",
    );
    refute_re(DEPLOY_SH, &deploy, "^[[:space:]]*compose up -d");
    let deploy = task_with(&read(PLAYBOOK), "Run deploy.sh");
    assert_lit(
        PLAYBOOK,
        &deploy,
        "changed_when: \"'DEPLOY_CHANGED=true' in _deploy.stdout_lines\"",
    );
}
