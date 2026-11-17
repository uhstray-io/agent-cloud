//! The static tier of the postiz social-publishing service (plan 24, AC-967).
//!
//! Verifies the shape of `platform/services/postiz`: an env-parameterized five-container compose
//! (the app, its Postgres and Redis, the Temporal workflow engine and that engine's Postgres) with
//! only the app publishing a port; the inventory-gated search-node overlay; the two-file config split
//! that keeps the app's credentials out of compose's `$` interpolation; a container-only `deploy.sh`
//! that refuses to start without both rendered env files; the composable deploy, seed and store
//! playbooks; and a config-as-code Authentik OIDC blueprint whose redirect matches what upstream
//! hardcodes. No hardcoded addresses or credentials.
//!
//! Ports the 46 `@test`s of the deleted `platform/tests/test_service_postiz.bats`. Structural only:
//! file reads, no deploy. The checks read the raw text, as the BATS helpers did. BATS `! grep` lines
//! mid-body never failed under `set -e`, so every absence check here is a real assertion, scoped to
//! its task block where the BATS check was.
//!
//! Three of these tests are cited by `docs/MISTAKES.md` (2.2, 2.19, 10.1); their lines in
//! `ledger_citation_ratchet.d/postiz.txt` name them.
//!
//! Where the port differs from its BATS original (each difference is also listed in the pull request
//! body):
//! - stricter: the `Report` block of `store-postiz-api-key.yml` requires every `_api_key` to be
//!   followed by ` | length`. The BATS filter `grep -v '_api_key \| length'` is a BRE, where `\|`
//!   is an alternation (checked with the grep on this machine, ugrep 7.8.4): the filter drops any
//!   `_api_key ` line, so an unpiped `{{ _api_key }}` passed it;
//! - stricter: the CAS test's absence check runs from the merge step's line to the end of the file,
//!   and that anchor line must exist. The BATS sed range printed nothing when its anchor was
//!   missing, and the absence check then passed for nothing;
//! - the loopback scan reads the tracked files (`repo::ls_files`, git pathspecs) where BATS ran
//!   `find` over the working tree;
//! - `[ -x ]` is checked as any execute bit, not the owner bit alone;
//! - the two identical `tasks/place-monorepo.yml` assertions of the deploy-playbook test are one.
//!
//! Helpers this file keeps privately until the shared support lands (plan 24 S7): `task_block`,
//! `assert_precedes` and `assert_contains`/`refute_contains`. The `text` helpers come from
//! `agent_cloud_tests::text`.
//!
//! ## Port map (BATS @test -> Rust fn)
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | `postiz: compose env-parameterizes image + bind/port` | `compose_env_parameterizes_image_bind_and_port` |
//! | `postiz: default images are pinned (no :latest drift)` | `default_images_are_pinned_no_latest_drift` |
//! | `postiz: five-container stack (app + pg + redis + temporal + temporal pg)` | `five_container_stack` |
//! | `postiz: the BASE topology omits elasticsearch and the workflow UI` | `base_topology_omits_elasticsearch_and_the_workflow_ui` |
//! | `postiz: the search-node overlay is complete, internal, and gated from inventory` | `search_node_overlay_is_complete_internal_and_gated` |
//! | `postiz: temporal's Postgres stays on 16 (its supported ceiling)` | `temporal_postgres_stays_on_16` |
//! | `postiz: only the app publishes a port (datastores + engine stay internal)` | `only_the_app_publishes_a_port` |
//! | `postiz: the mounted app config is actually loaded into the container` | `mounted_app_config_is_actually_loaded` |
//! | `postiz: nothing probes the workflow engine on loopback` | `nothing_probes_the_workflow_engine_on_loopback` |
//! | `postiz: healthchecks on all five containers, app dependencies gated` | `healthchecks_on_all_five_containers_app_dependencies_gated` |
//! | `postiz: config is a bind-mounted FILE, not a directory (Option B)` | `config_is_a_bind_mounted_file_not_a_directory` |
//! | `postiz: app config does NOT go through compose interpolation` | `app_config_does_not_go_through_compose_interpolation` |
//! | `postiz: compose has no hardcoded credentials or IPs` | `compose_has_no_hardcoded_credentials_or_ips` |
//! | `postiz: deploy.sh is container-lifecycle only (no secret handling)` | `deploy_sh_is_container_lifecycle_only` |
//! | `postiz: deploy.sh requires BOTH rendered env files before starting` | `deploy_sh_requires_both_rendered_env_files` |
//! | `postiz: deploy.sh recreates on a changed config so re-rendered config applies` | `deploy_sh_recreates_on_a_changed_config` |
//! | `postiz: env.j2 carries ONLY compose-substitution values` | `env_j2_carries_only_compose_substitution_values` |
//! | `postiz: postiz.env.j2 sources every secret from OpenBao, none literal` | `app_env_j2_sources_every_secret_from_openbao` |
//! | `postiz: NOT_SECURED is absent (upstream documents it as dev-only)` | `not_secured_is_absent` |
//! | `postiz: POSTIZ_OAUTH_SCOPE is not templated (upstream never reads it)` | `oauth_scope_is_not_templated` |
//! | `postiz: registration lockdown is config, not a manual host step` | `registration_lockdown_is_config` |
//! | `postiz: unseeded social provider slots render empty, not absent` | `unseeded_provider_slots_render_empty_not_absent` |
//! | `postiz: local overlay adds the CA trust the OIDC flow needs` | `local_overlay_adds_the_ca_trust_the_oidc_flow_needs` |
//! | `postiz: deploy playbook is composable and scopes no_log to secrets` | `deploy_playbook_is_composable_and_scopes_no_log` |
//! | `postiz: playbook shared-reads the OIDC secret rather than storing a copy` | `playbook_shared_reads_the_oidc_secret` |
//! | `postiz: playbook declares the three stateful secrets as generate-once` | `playbook_declares_the_stateful_secrets_as_generate_once` |
//! | `postiz: playbook verifies the workflow engine, not just the app` | `playbook_verifies_the_workflow_engine` |
//! | `postiz: OIDC blueprint redirect matches what upstream hardcodes` | `oidc_blueprint_redirect_matches_upstream` |
//! | `postiz: the OIDC client secret chain has no copy anywhere` | `oidc_client_secret_chain_has_no_copy` |
//! | `postiz: registered in the Authentik app catalog with prod guards` | `registered_in_the_app_catalog_with_prod_guards` |
//! | `postiz: local Caddy route has NO forward_auth gate` | `local_caddy_route_has_no_forward_auth_gate` |
//! | `postiz: Semaphore templates exist for deploy, clean, and seeding` | `semaphore_templates_exist_for_deploy_clean_and_seeding` |
//! | `postiz: seeding template stores no credential in Semaphore's database` | `seeding_template_stores_no_credential` |
//! | `postiz: public inventory placeholder leaks no real address` | `public_inventory_placeholder_leaks_no_real_address` |
//! | `postiz: psql credential check preserves its exit status` | `psql_check_preserves_its_exit_status` |
//! | `postiz: first-path secret creation is atomic (CAS 0)` | `first_path_secret_creation_is_atomic` |
//! | `postiz: the seed playbook uses the shared cleartext OpenBao guard` | `seed_playbook_uses_the_shared_cleartext_guard` |
//! | `postiz: every provider slot has a seedable key and a declaration` | `every_provider_slot_has_a_seedable_key_and_a_declaration` |
//! | `postiz: docs do not tell an operator to create DNS by hand` | `docs_do_not_tell_an_operator_to_create_dns` |
//! | `postiz: deploy.sh rejects an unknown argument instead of ignoring it` | `deploy_sh_rejects_an_unknown_argument` |
//! | `postiz: deploy.sh waits on the APP container, not merely on compose up` | `deploy_sh_waits_on_the_app_container` |
//! | `postiz: deploy.sh hashes the bind-mounted config FILE into the recreate decision` | `deploy_sh_hashes_the_config_file_into_the_recreate_decision` |
//! | `postiz: deploy.sh fails loudly when the bind-mount source is missing` | `deploy_sh_fails_loudly_when_the_bind_mount_source_is_missing` |
//! | `postiz: the app healthcheck probes the BACKEND path, not the frontend` | `app_healthcheck_probes_the_backend_path` |
//! | `postiz api key: read-only capture, fixed path, key-bearing steps no_log` | `api_key_capture_is_read_only_with_fixed_path_and_no_log` |
//! | `postiz: teardown resolves the compose-file SUPERSET, so overlay-only resources die too` | `teardown_resolves_the_compose_file_superset` |

use std::collections::BTreeSet;
use std::os::unix::fs::PermissionsExt;

use agent_cloud_tests::repo;
use agent_cloud_tests::text;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const COMPOSE: &str = "platform/services/postiz/deployment/compose.yml";
const COMPOSE_SEARCH: &str = "platform/services/postiz/deployment/compose.search.yml";
const COMPOSE_LOCAL: &str = "platform/services/postiz/deployment/compose.local.yml";
const DEPLOY_SH: &str = "platform/services/postiz/deployment/deploy.sh";
const README: &str = "platform/services/postiz/deployment/README.md";
const ENV_J2: &str = "platform/services/postiz/deployment/templates/env.j2";
const APP_ENV_J2: &str = "platform/services/postiz/deployment/templates/postiz.env.j2";
const DEPLOY_PB: &str = "platform/playbooks/deploy-postiz.yml";
const SEED_PB: &str = "platform/playbooks/seed-postiz-secrets.yml";
const STORE_KEY_PB: &str = "platform/playbooks/store-postiz-api-key.yml";
const VALIDATE_PB: &str = "platform/playbooks/validate-secrets.yml";
const CLEAN_SERVICE: &str = "platform/playbooks/tasks/clean-service.yml";
const BAO_MERGE: &str = "platform/playbooks/tasks/bao-merge-keys.yml";
const BLUEPRINT: &str = "platform/services/authentik/deployment/blueprints/postiz-oidc.yaml";
const AUTHENTIK_ENV: &str = "platform/services/authentik/deployment/templates/env.j2";
const AUTHENTIK_SECRETS: &str = "platform/playbooks/vars/secret-declarations/authentik.yml";
const APP_CATALOG: &str = "platform/services/authentik/deployment/app-catalog.yml";
const LOCAL_INVENTORY: &str = "platform/inventory/local-dev.yml.example";
const PROD_INVENTORY: &str = "platform/inventory/production.yml";
const TEMPLATES: &str = "platform/semaphore/templates.yml";
const TEMPLATES_LOCAL: &str = "platform/semaphore/templates-local.yml";
const PLAN_14: &str = "plan/development/14-postiz-social-publishing.md";

/// `[ -f <rel> ]`: the file exists.
fn assert_file(rel: &str) {
    assert!(repo::path(rel).is_file(), "{rel}: not a file");
}

/// `[ -x <rel> ]`, checked as any execute bit. The test runs as the owner, for whom `[ -x ]` reads
/// the owner bit; the stricter of the two is not what the BATS check asked.
fn assert_executable(rel: &str) {
    let mode = std::fs::metadata(repo::path(rel))
        .unwrap_or_else(|e| panic!("{rel}: {e}"))
        .permissions()
        .mode();
    assert!((mode & 0o111) != 0, "{rel}: not executable (mode {mode:o})");
}

/// `grep -c <literal>`: lines of `rel` containing the literal.
fn count_contains(rel: &str, literal: &str) -> usize {
    text::read(rel)
        .split_terminator('\n')
        .filter(|line| line.contains(literal))
        .count()
}

/// `grep -cE <pattern>`: lines of `rel` the ERE `pattern` matches.
fn count_lines(rel: &str, pattern: &str) -> usize {
    let re = Regex::new(pattern).unwrap_or_else(|e| panic!("bad pattern {pattern:?}: {e}"));
    text::read(rel)
        .split_terminator('\n')
        .filter(|line| re.is_match(line))
        .count()
}

/// `grep -vE '^[[:space:]]*#' <rel>`: the non-comment lines of the file, joined by newlines.
fn non_comment_lines(rel: &str) -> String {
    let comment = Regex::new(r"^[[:space:]]*#").expect("comment pattern");
    text::read(rel)
        .split_terminator('\n')
        .filter(|line| !comment.is_match(line))
        .collect::<Vec<_>>()
        .join("\n")
}

/// `grep -nE <pattern> <rel> | head -1`: the 1-based line of the first match, `None` when none.
fn first_match(text: &str, pattern: &str) -> Option<usize> {
    let re = Regex::new(pattern).unwrap_or_else(|e| panic!("bad pattern {pattern:?}: {e}"));
    text.split_terminator('\n')
        .position(|line| re.is_match(line))
        .map(|i| i + 1)
}

/// `grep -n <literal> <rel> | head -1`: the 1-based line of the first line holding the literal.
fn first_line_containing(text: &str, literal: &str) -> Option<usize> {
    text.split_terminator('\n')
        .position(|line| line.contains(literal))
        .map(|i| i + 1)
}

/// The first line matching `a` comes before the first line matching `b` (assert_helpers.bash
/// `assert_precedes`). Both patterns are ERE, read per line.
fn assert_precedes(rel: &str, a: &str, b: &str) {
    let text = text::read(rel);
    let la = first_match(&text, a).unwrap_or_else(|| panic!("no match for {a:?} in {rel}"));
    let lb = first_match(&text, b).unwrap_or_else(|| panic!("no match for {b:?} in {rel}"));
    assert!(
        la < lb,
        "{a:?} (line {la}) after {b:?} (line {lb}) in {rel}"
    );
}

/// The transport guard runs before the first request that carries a credential.
fn assert_guard_precedes_first_uri(rel: &str) {
    assert_precedes(
        rel,
        r"include_tasks: tasks/assert-bao-transport\.yml",
        r"^[[:space:]]+ansible\.builtin\.uri:",
    );
}

/// 1-based position of the first `-` in a line, 0 when none (awk `index($0, "-")`).
fn dash_at(line: &str) -> usize {
    line.find('-').map_or(0, |i| i + 1)
}

/// One task's body lines: everything after the first line containing `- name: "<name>` (a prefix
/// match), until the next `- name:` at the same or shallower indent. The text semantics of
/// assert_helpers.bash `task_block`. Panics when the task is missing.
fn task_block(rel: &str, name: &str) -> String {
    let start = format!("- name: \"{name}");
    let next = Regex::new(r"^[[:space:]]*- name:").expect("task pattern");
    let text = text::read(rel);
    let mut lines = text.split_terminator('\n');
    let indent = lines
        .by_ref()
        .find(|l| l.contains(&start))
        .map(dash_at)
        .unwrap_or_else(|| panic!("{rel}: no task named {name:?}"));
    lines
        .take_while(|l| !(next.is_match(l) && dash_at(l) <= indent))
        .collect::<Vec<_>>()
        .join("\n")
}

/// `grep -qE <pattern> <haystack>`: a line of the text matches.
fn assert_in(haystack: &str, pattern: &str, what: &str) {
    assert!(
        text::any_line(haystack, pattern),
        "{what}: no line matches `{pattern}`"
    );
}

/// `! grep -qE <pattern> <haystack>`: no line of the text matches.
fn refute_in(haystack: &str, pattern: &str, what: &str) {
    assert!(
        !text::any_line(haystack, pattern),
        "{what}: a line matches the forbidden `{pattern}`"
    );
}

fn assert_contains(haystack: &str, needle: &str, what: &str) {
    assert!(haystack.contains(needle), "{what}: missing {needle:?}");
}

fn refute_contains(haystack: &str, needle: &str, what: &str) {
    assert!(
        !haystack.contains(needle),
        "{what}: found forbidden {needle:?}"
    );
}

/// The value of the app's `command:` block: the awk of the mounted-config test. The lines after
/// the `      - >-` line that are indented two or more spaces, up to the next six-space key, each
/// followed by a space (`tr '\n' ' '`).
fn command_value(rel: &str) -> String {
    let start = Regex::new(r"^      - >-$").expect("start pattern");
    let stop = Regex::new(r"^      [^ ]").expect("stop pattern");
    let body = Regex::new(r"^  ").expect("body pattern");
    let mut inside = false;
    let mut out = String::new();
    for line in text::read(rel).split_terminator('\n') {
        if start.is_match(line) {
            inside = true;
        } else if inside && stop.is_match(line) {
            break;
        } else if inside && body.is_match(line) {
            out.push_str(line);
            out.push(' ');
        }
    }
    out
}

/// The `Report (names and lengths only` task's body (the awk of the API-key test): the lines
/// after its `- name:` line, up to the next four-space `- name:`.
fn report_block(rel: &str) -> String {
    let start = Regex::new(r#"- name: "Report \(names and lengths only"#).expect("start pattern");
    let stop = Regex::new(r"^    - name:").expect("stop pattern");
    let text = text::read(rel);
    let mut inside = false;
    let mut out = Vec::new();
    for line in text.split_terminator('\n') {
        if start.is_match(line) {
            inside = true;
        } else if inside && stop.is_match(line) {
            break;
        } else if inside {
            out.push(line);
        }
    }
    out.join("\n")
}

/// The sed range `/<anchor>/,$p`: from the first line holding the anchor to the end of the file.
/// Empty when the anchor is missing; the caller asserts the anchor exists first.
fn from_anchor_to_end(rel: &str, anchor: &str) -> String {
    let text = text::read(rel);
    let lines: Vec<&str> = text.split_terminator('\n').collect();
    match lines.iter().position(|l| l.contains(anchor)) {
        Some(i) => lines[i..].join("\n"),
        None => String::new(),
    }
}

/// The values of every match of `pattern` in the file, each's trailing digits. The BATS check
/// `grep -oE ... | grep -oE '[0-9]+$'` read them as one string, so it accepted exactly one.
fn trailing_numbers(rel: &str, pattern: &str) -> Vec<u64> {
    let re = Regex::new(pattern).expect("pattern");
    let digits = Regex::new(r"[0-9]+$").expect("digits");
    re.find_iter(&text::read(rel))
        .filter_map(|m| digits.find(m.as_str()))
        .map(|d| d.as_str().parse().expect("a number"))
        .collect()
}

/// The `{names}` of every `re` capture in the text, as a set.
fn captured(text: &str, pattern: &str) -> BTreeSet<String> {
    let re = Regex::new(pattern).expect("pattern");
    re.captures_iter(text).map(|c| c[1].to_owned()).collect()
}

#[test]
fn compose_env_parameterizes_image_bind_and_port() {
    assert_file(COMPOSE);
    text::assert_line(COMPOSE, r"\$\{POSTIZ_IMAGE");
    text::assert_line(COMPOSE, r"\$\{POSTIZ_BIND");
    text::assert_line(COMPOSE, r"\$\{POSTIZ_PORT");
}

#[test]
fn default_images_are_pinned_no_latest_drift() {
    text::assert_line(
        COMPOSE,
        r"\$\{POSTIZ_IMAGE:-ghcr\.io/gitroomhq/postiz-app:v[0-9]",
    );
    text::refute_line(COMPOSE, "postiz-app:latest");
    text::refute_line(COMPOSE, ":latest");
}

#[test]
fn five_container_stack() {
    text::assert_line(COMPOSE, r"^\s+postiz:");
    text::assert_line(COMPOSE, r"^\s+postiz-postgres:");
    text::assert_line(COMPOSE, r"^\s+postiz-redis:");
    text::assert_line(COMPOSE, r"^\s+temporal:");
    text::assert_line(COMPOSE, r"^\s+temporal-postgresql:");
}

#[test]
fn base_topology_omits_elasticsearch_and_the_workflow_ui() {
    // Upstream's reference compose adds these three; the BASE deliberately does not. The search
    // node lives only in the gated overlay.
    text::refute_line(COMPOSE, r"^\s+temporal-elasticsearch:");
    text::refute_line(COMPOSE, r"^\s+temporal-ui:");
    text::refute_line(COMPOSE, r"^\s+temporal-admin-tools:");
    text::assert_line(COMPOSE, r#"ENABLE_ES:\s*"false""#);
}

#[test]
fn search_node_overlay_is_complete_internal_and_gated() {
    // The one add-back the design scoped in advance. The backend registers more than three Text
    // search attributes at startup, and SQL visibility caps at three, so without this overlay the
    // backend never binds.
    assert_file(COMPOSE_SEARCH);
    // The overlay flips the engine to ES and adds the node: all three variables, because
    // ENABLE_ES without seeds fails in a different place later.
    text::assert_line(COMPOSE_SEARCH, r#"ENABLE_ES:\s*"true""#);
    text::assert_line(COMPOSE_SEARCH, r"ES_SEEDS:\s*temporal-elasticsearch");
    text::assert_line(COMPOSE_SEARCH, r"ES_VERSION:\s*v7");
    text::assert_line(COMPOSE_SEARCH, r"^\s+temporal-elasticsearch:");
    // Internal only: the search node never publishes a host port, runs single-node discovery with
    // a bounded heap.
    text::refute_line(COMPOSE_SEARCH, r"^\s+ports:");
    text::assert_line(COMPOSE_SEARCH, r"discovery.type=single-node");
    // The prod-applied overlay carries no local-only knob: label=disable would strip SELinux
    // confinement from the one container running an EOL-line JVM. Those live in compose.local.yml.
    refute_contains(
        &non_comment_lines(COMPOSE_SEARCH),
        "label=disable",
        COMPOSE_SEARCH,
    );
    text::assert_line(COMPOSE_LOCAL, r"temporal-elasticsearch:");
    // Pinned image, parameterized like every other one.
    text::assert_line(
        COMPOSE_SEARCH,
        r"\$\{ELASTICSEARCH_IMAGE:-docker\.io/elasticsearch:7\.17",
    );
    // The gate: the deploy playbook wires the overlay from the inventory var, through the generic
    // COMPOSE_OVERLAYS mechanism in common.sh. Scoped to the deploy task's own block.
    let block = task_block(DEPLOY_PB, "Run deploy.sh (container lifecycle)");
    assert!(
        !block.is_empty(),
        "{DEPLOY_PB}: the deploy task has no body"
    );
    assert_in(
        &block,
        r"^        COMPOSE_OVERLAYS: .*compose\.search\.yml.*postiz_temporal_search \| default\(true\)",
        "the deploy task",
    );
}

#[test]
fn temporal_postgres_stays_on_16() {
    // Deliberately not tracking the app's Postgres 17: Temporal 1.28 supports up to PG16.
    text::assert_line(
        COMPOSE,
        r"\$\{TEMPORAL_POSTGRES_IMAGE:-docker\.io/postgres:16",
    );
    text::assert_line(
        COMPOSE,
        r"\$\{POSTIZ_POSTGRES_IMAGE:-docker\.io/postgres:17",
    );
}

#[test]
fn only_the_app_publishes_a_port() {
    // A single ports: stanza, the app's. The host firewall needs exactly one service rule.
    assert_eq!(
        count_lines(COMPOSE, r"^\s+ports:"),
        1,
        "{COMPOSE}: the number of ports: stanzas"
    );
}

#[test]
fn mounted_app_config_is_actually_loaded() {
    // Mounting the config file is not sufficient on its own: the image entrypoint is a generic node
    // wrapper that never reads it, so the app starts with no DATABASE_URL. The command below reads
    // the file into the environment; it never sources it.
    let cmd = command_value(COMPOSE);
    assert!(!cmd.is_empty(), "{COMPOSE}: no `- >-` command block");

    // READ into the environment, never SOURCED. `.` subjects every line to shell parsing and the
    // template renders values unquoted, so a value containing `$(...)` executes.
    refute_contains(&cmd, ". /config/postiz.env", "the command value");
    assert_contains(&cmd, r#"export "$$l""#, "the command value");
    // Retain the declared startup sequence; explicit export assigns child-process env.
    assert_contains(&cmd, "set -a", "the command value");
    // `read` returns false on a final line with no trailing newline and would drop it silently.
    assert_contains(&cmd, r#"|| [ -n "$$l" ]"#, "the command value");

    // NOT via env_file, which compose interpolates.
    text::refute_line(COMPOSE, r"^\s+env_file:");
    // Done as `command:`, NOT an `entrypoint:` wrapper: podman-compose sets Cmd to null when the
    // entrypoint is overridden, so the wrapper execs nothing.
    text::assert_line(COMPOSE, r"^\s+command:$");
    text::refute_line(COMPOSE, r"^\s+entrypoint:$");
    // The CMD is copied, so it is pinned: a change in the upstream CMD fails here.
    text::assert_text(COMPOSE, "nginx && pnpm run pm2");
}

#[test]
fn nothing_probes_the_workflow_engine_on_loopback() {
    // The engine binds its frontend to the container IP, so a loopback probe is refused forever.
    // Asserted repo-wide, because the bug was a missed copy. Scoped to EXECUTABLE artifacts: the
    // scan reads the tracked files (git pathspecs, where BATS ran `find` over the working tree;
    // git's `*` crosses `/`, as find's recursion did). A comment line is excluded where the BATS
    // filter excluded a `:` followed by optional whitespace and `#`, as in `grep -Hn` output.
    let comment = Regex::new(r":[[:space:]]*#").expect("comment pattern");
    let mut hits = Vec::new();
    for rel in repo::ls_files(&["platform/*.yml", "platform/*.sh", "platform/*.j2"]) {
        for (i, line) in text::read(&rel).split_terminator('\n').enumerate() {
            if line.contains("127.0.0.1:7233") {
                let shown = format!("{rel}:{}:{line}", i + 1);
                if !comment.is_match(&shown) {
                    hits.push(shown);
                }
            }
        }
    }
    assert!(
        hits.is_empty(),
        "the loopback workflow-engine address in executable artifacts:\n{}",
        hits.join("\n")
    );

    // Both places resolve the container's own address, with no pipe.
    text::assert_text(COMPOSE, "a=$$(hostname -i)");
    text::assert_text(DEPLOY_PB, "a=$(hostname -i)");
    text::refute_text(COMPOSE, "hostname -i | awk");
    text::refute_text(DEPLOY_PB, "hostname -i | awk");
}

#[test]
fn healthchecks_on_all_five_containers_app_dependencies_gated() {
    assert_eq!(
        count_contains(COMPOSE, "healthcheck:"),
        5,
        "{COMPOSE}: the healthcheck count"
    );
    text::assert_text(COMPOSE, "pg_isready");
    text::assert_line(COMPOSE, "redis-cli ping");
    text::assert_line(COMPOSE, "temporal operator cluster health");
    // The app waits on its datastores AND the workflow engine.
    text::assert_line(COMPOSE, r"temporal:\s*$");
    text::assert_line(COMPOSE, "condition: service_healthy");
}

#[test]
fn config_is_a_bind_mounted_file_not_a_directory() {
    // Binding the single file leaves the postiz-config named volume serving the rest of /config.
    text::assert_line(COMPOSE, r"\./config/postiz\.env:/config/postiz\.env:ro");
    text::assert_line(COMPOSE, "postiz-config:/config/");
}

#[test]
fn app_config_does_not_go_through_compose_interpolation() {
    // ~60 social credential slots must not pass through compose, where a `$` in a secret is mangled.
    text::refute_line(COMPOSE, r"^\s+env_file:");
    text::refute_line(COMPOSE, "JWT_SECRET");
    // The setting, not the name: the name legitimately appears in comments explaining why.
    text::refute_line(COMPOSE, r"^[^#]*DATABASE_URL[:=]");
    text::refute_line(COMPOSE, "CLIENT_SECRET");
}

#[test]
fn compose_has_no_hardcoded_credentials_or_ips() {
    // Upstream's compose ships postiz-user/postiz-password and temporal/temporal.
    text::refute_line(COMPOSE, "postiz-password|my-postiz-password");
    text::refute_line(COMPOSE, r"POSTGRES_PWD:\s*temporal\s*$");
    text::refute_line(
        COMPOSE,
        r"(192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)",
    );
    // Every password is a substitution reference.
    text::assert_line(COMPOSE, r"POSTGRES_PASSWORD: \$\{POSTIZ_DB_PASSWORD\}");
    text::assert_line(COMPOSE, r"POSTGRES_PWD: \$\{TEMPORAL_DB_PASSWORD\}");
}

#[test]
fn deploy_sh_is_container_lifecycle_only() {
    assert_file(DEPLOY_SH);
    assert_executable(DEPLOY_SH);
    text::refute_line(
        DEPLOY_SH,
        "gen_secret|put_secret|get_secret|bao |vault |openbao",
    );
    text::assert_text(DEPLOY_SH, "detect_runtime");
    text::assert_text(DEPLOY_SH, "wait_for_healthy postiz");
}

#[test]
fn deploy_sh_requires_both_rendered_env_files() {
    text::assert_line(DEPLOY_SH, r#"\.env" \] \|\| error"#);
    // The app config is a bind SOURCE: if absent, the runtime creates a directory there.
    text::assert_line(DEPLOY_SH, "config/postiz.env");
}

#[test]
fn deploy_sh_recreates_on_a_changed_config() {
    // A bind-mounted file's content change is not a compose-spec change, so a plain `up -d` would
    // leave the old container running with stale config. compose_up_if_changed hashes the file.
    text::assert_line(DEPLOY_SH, r"^ *compose_up_if_changed .*config/postiz\.env");
    text::refute_text(DEPLOY_SH, "compose up -d");
}

#[test]
fn env_j2_carries_only_compose_substitution_values() {
    assert_file(ENV_J2);
    text::assert_line(ENV_J2, r"^POSTIZ_IMAGE=");
    text::assert_line(ENV_J2, r"^POSTIZ_BIND=");
    text::assert_line(
        ENV_J2,
        r"^POSTIZ_DB_PASSWORD=\{\{ secrets\.postiz_db_password \}\}",
    );
    // App config must NOT leak into this file: that would reintroduce the interpolation hazard.
    text::refute_line(ENV_J2, r"^JWT_SECRET=|^POSTIZ_OAUTH|^X_API_KEY=");
}

#[test]
fn app_env_j2_sources_every_secret_from_openbao() {
    assert_file(APP_ENV_J2);
    text::assert_line(
        APP_ENV_J2,
        r"^JWT_SECRET=\{\{ secrets\.postiz_jwt_secret \}\}",
    );
    text::assert_line(
        APP_ENV_J2,
        r"^POSTIZ_OAUTH_CLIENT_SECRET=\{\{ secrets\.postiz_oidc_client_secret \}\}",
    );
    text::assert_line(APP_ENV_J2, r"^TEMPORAL_ADDRESS=temporal:7233");
    // No literal credential values anywhere.
    text::refute_line(
        APP_ENV_J2,
        r"(?i)(secret|password|api_key|token)=[A-Za-z0-9]{8}",
    );
}

#[test]
fn not_secured_is_absent() {
    // The prior developer-machine .env set it; it disables security checks. The template carries a
    // NOTE saying so, so match the assignment rather than the name.
    text::refute_line(APP_ENV_J2, "^NOT_SECURED=");
    text::refute_line(COMPOSE, "NOT_SECURED");
}

#[test]
fn oauth_scope_is_not_templated() {
    // Upstream never reads it; templating it would imply control we do not have. Anchored: the name
    // appears in a comment explaining why it is omitted.
    text::refute_line(APP_ENV_J2, "^POSTIZ_OAUTH_SCOPE=");
}

#[test]
fn registration_lockdown_is_config() {
    text::assert_line(
        APP_ENV_J2,
        "DISABLE_REGISTRATION=.*postiz_disable_registration",
    );
}

#[test]
fn unseeded_provider_slots_render_empty_not_absent() {
    // Every slot rendering (empty when unseeded) is what makes enabling a new platform a seed plus
    // a redeploy, with no code change.
    text::assert_line(
        APP_ENV_J2,
        r"^X_API_KEY=\{\{ secrets\.postiz_x_api_key \| default\(''\) \}\}",
    );
    text::assert_line(
        APP_ENV_J2,
        r"^REDDIT_CLIENT_ID=\{\{ secrets\.postiz_reddit_client_id \| default\(''\) \}\}",
    );
}

#[test]
fn local_overlay_adds_the_ca_trust_the_oidc_flow_needs() {
    assert_file(COMPOSE_LOCAL);
    text::assert_line(COMPOSE_LOCAL, "step-ca-bundle.crt");
    text::assert_text(COMPOSE_LOCAL, "NODE_EXTRA_CA_CERTS");
    text::assert_text(COMPOSE_LOCAL, "local-dev");
    // compose merges ports by appending, so a re-published port here would be a second one.
    text::refute_line(COMPOSE_LOCAL, r"^\s+ports:");
}

#[test]
fn deploy_playbook_is_composable_and_scopes_no_log() {
    assert_file(DEPLOY_PB);
    text::assert_line(DEPLOY_PB, "tasks/manage-secrets.yml");
    // The preamble enables linger.
    text::assert_line(DEPLOY_PB, "tasks/place-monorepo.yml");
    text::assert_line(DEPLOY_PB, "tasks/distribute-ca-root.yml");
    // Renders both files, and creates the config dir first (the template does not).
    text::assert_line(DEPLOY_PB, r"dest: config/postiz\.env");
    text::assert_line(DEPLOY_PB, "state: directory");
    // no_log must NOT appear on the deploy/verify tasks: a past failure was censored that way.
    text::refute_line(DEPLOY_PB, "no_log");
}

#[test]
fn playbook_shared_reads_the_oidc_secret() {
    text::assert_line(DEPLOY_PB, "_shared_reads");
    text::assert_line(DEPLOY_PB, "from_service: authentik");
    text::assert_line(DEPLOY_PB, "postiz_oidc_client_secret");
}

#[test]
fn playbook_declares_the_stateful_secrets_as_generate_once() {
    text::assert_line(DEPLOY_PB, "name: postiz_jwt_secret, type: random");
    text::assert_line(DEPLOY_PB, "name: postiz_db_password, type: random");
    text::assert_line(DEPLOY_PB, "name: postiz_temporal_db_password, type: random");
    // Social credentials are never generated; they are the operator's own.
    text::assert_line(DEPLOY_PB, "name: postiz_x_api_key, type: existing");
}

#[test]
fn playbook_verifies_the_workflow_engine() {
    // An app that is healthy while the engine is unreachable silently never publishes anything.
    text::assert_line(DEPLOY_PB, "temporal operator cluster health");
}

#[test]
fn oidc_blueprint_redirect_matches_upstream() {
    assert_file(BLUEPRINT);
    // Upstream builds the redirect as ${FRONTEND_URL}/settings, so the redirect must end in /settings.
    text::assert_line(BLUEPRINT, "/settings");
    text::assert_line(BLUEPRINT, "matching_mode: strict");
    text::assert_line(BLUEPRINT, "client_secret: !Env POSTIZ_OIDC_CLIENT_SECRET");
    // email is load-bearing: identity comes from /userinfo.
    text::assert_line(BLUEPRINT, "scope_name, email");
    text::assert_line(BLUEPRINT, "scope_name, openid");
    text::assert_line(BLUEPRINT, "scope_name, profile");
    // No secret literal in git.
    text::refute_line(BLUEPRINT, "(?i)client_secret: [A-Za-z0-9]{8}");
}

#[test]
fn oidc_client_secret_chain_has_no_copy() {
    // authentik generates it, exposes it to the worker, the blueprint reads it via !Env, and postiz
    // shared-reads the same key. Five links, one value.
    text::assert_line(
        AUTHENTIK_SECRETS,
        "name: postiz_oidc_client_secret, type: random",
    );
    text::assert_line(
        AUTHENTIK_ENV,
        r"^POSTIZ_OIDC_CLIENT_SECRET=\{\{ secrets\.postiz_oidc_client_secret \}\}",
    );
}

#[test]
fn registered_in_the_app_catalog_with_prod_guards() {
    text::assert_line(APP_CATALOG, r"^  postiz:");
    text::assert_line(APP_CATALOG, r"file: postiz-oidc\.yaml");
    // prod_required and verify_redirect make the deploy fail loudly rather than provisioning a
    // local-dev URL into production.
    text::assert_line(
        APP_CATALOG,
        r"prod_required: \[postiz_redirect_uri, postiz_launch_url\]",
    );
    text::assert_line(APP_CATALOG, r"verify_redirect: postiz_redirect_uri");
}

#[test]
fn local_caddy_route_has_no_forward_auth_gate() {
    text::assert_line(
        LOCAL_INVENTORY,
        r#"host: "postiz\.agent-cloud\.test", upstream: "postiz:5000""#,
    );
    // An edge gate would also gate /api/public/v1 and break API-key calls.
    text::refute_line(LOCAL_INVENTORY, r"postiz\.agent-cloud\.test.*forward_auth");
}

#[test]
fn semaphore_templates_exist_for_deploy_clean_and_seeding() {
    text::assert_line(TEMPLATES, "^  - name: Deploy Postiz$");
    text::assert_line(TEMPLATES, "^  - name: Clean Deploy Postiz$");
    text::assert_line(TEMPLATES, "^  - name: Seed Postiz Secrets$");
    text::assert_line(TEMPLATES_LOCAL, r"Deploy Postiz \(Local\)");
    text::assert_line(TEMPLATES_LOCAL, r"Clean Deploy Postiz \(Local\)");
}

#[test]
fn seeding_template_stores_no_credential() {
    // Semaphore persists surveys. Only the non-secret read-only switch is allowed; provider
    // credentials must use temporary encrypted environment inputs.
    let doc = yaml::load_one(&repo::path(TEMPLATES)).expect("templates.yml: one YAML document");
    let templates = doc
        .get("templates")
        .and_then(Node::as_seq)
        .expect("templates.yml: a templates list");
    let seed = templates
        .iter()
        .find(|t| t.get("name").and_then(Node::as_str) == Some("Seed Postiz Secrets"))
        .expect("templates.yml: no Seed Postiz Secrets template");
    let survey = seed
        .get("survey_vars")
        .and_then(Node::as_seq)
        .expect("the Seed Postiz Secrets template has survey_vars");
    assert_eq!(survey.len(), 1, "the seed template's survey_vars count");
    assert_eq!(
        survey[0].get("name").and_then(Node::as_str),
        Some("postiz_verify_access_only")
    );
    assert_eq!(survey[0].get("type").and_then(Node::as_str), Some("string"));
    // A plain `false` is a YAML boolean, which is not the string "false" the BATS check compared.
    assert_eq!(
        survey[0].get("default_value").and_then(Node::as_str),
        Some("false")
    );
}

#[test]
fn public_inventory_placeholder_leaks_no_real_address() {
    text::assert_line(PROD_INVENTORY, "postiz_svc:");
    text::assert_line(PROD_INVENTORY, r"\{\{ postiz_host \}\}");
    // Real addresses belong in site-config, never here.
    text::refute_line(
        PROD_INVENTORY,
        r"postiz.*(192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)",
    );
}

#[test]
fn psql_check_preserves_its_exit_status() {
    // A `| head` pipeline returns HEAD's status, so a failed psql would report VALID.
    let task = task_block(VALIDATE_PB, "Validate postiz Postgres password");
    assert!(!task.is_empty(), "{VALIDATE_PB}: the psql task has no body");
    refute_contains(&task, "head -3", "the psql task");
    text::assert_line(VALIDATE_PB, r"ansible\.builtin\.command:");
}

#[test]
fn first_path_secret_creation_is_atomic() {
    // Two runs racing a first-time seed both see 404; without CAS the later POST replaces the
    // earlier writer's keys. The seed writes through the shared merge task, which owns the CAS-0
    // create. That task's own checks live in platform/tests/suite/credential_leaks.rs.
    text::assert_line(SEED_PB, r"include_tasks: tasks/bao-merge-keys\.yml");
    // The absence check runs from the merge step to the end of the file. Its anchor must exist,
    // or the range is empty and the check passes for nothing.
    let anchor = "Merge the supplied credentials";
    let tail = from_anchor_to_end(SEED_PB, anchor);
    assert!(
        !tail.is_empty(),
        "{SEED_PB}: no line holds the merge step `{anchor}`"
    );
    refute_in(
        &tail,
        "method: (PATCH|POST)",
        "the merge step to the end of the seed playbook",
    );
}

#[test]
fn seed_playbook_uses_the_shared_cleartext_guard() {
    // The rule lives in exactly one file, and its behaviour (18 URL cases, including four
    // userinfo bypasses) is asserted in platform/tests/suite/credential_leaks.rs. This playbook only
    // delegates to the shared guard.
    text::assert_line(SEED_PB, r"include_tasks: tasks/assert-bao-transport\.yml");
    text::refute_text(SEED_PB, "Refusing to send secret material");
}

#[test]
fn every_provider_slot_has_a_seedable_key_and_a_declaration() {
    // The BATS script opens the app env template too; a missing one fails here as it did there.
    let _ = text::read(APP_ENV_J2);
    let seed = captured(&text::read(SEED_PB), r"(?m)^      - ([a-z0-9_]+)$");
    let declared = captured(
        &text::read(DEPLOY_PB),
        r"name: postiz_([a-z0-9_]+), type: existing",
    );
    assert!(
        seed == declared,
        "DRIFT seed-only={:?} declared-only={:?}",
        seed.difference(&declared).collect::<Vec<_>>(),
        declared.difference(&seed).collect::<Vec<_>>()
    );
}

#[test]
fn docs_do_not_tell_an_operator_to_create_dns() {
    // The zone is config-as-code via OpenTofu and the record already exists.
    text::refute_line(README, r"^- Create the public DNS record\.$");
    text::refute_line(
        PLAN_14,
        r"Operator: create the `postiz\.uhstray\.io` DNS record",
    );
}

#[test]
fn deploy_sh_rejects_an_unknown_argument() {
    // A typo'd flag that is silently ignored produces a deploy that did something else.
    text::assert_line(DEPLOY_SH, r#"\*\) echo "Unknown option"#);
    text::assert_line(DEPLOY_SH, "exit 1");
}

#[test]
fn deploy_sh_waits_on_the_app_container() {
    // `compose up -d` returns as soon as the containers are created; the app still runs migrations.
    text::assert_line(DEPLOY_SH, r"wait_for_healthy postiz [0-9]+");
    // The BATS check read every match as one string, so exactly one timeout is accepted.
    let secs = trailing_numbers(DEPLOY_SH, r"wait_for_healthy postiz [0-9]+");
    assert_eq!(
        secs.len(),
        1,
        "{DEPLOY_SH}: the wait_for_healthy postiz timeouts"
    );
    // 300 s was measured as sufficient for first boot; under 120 fails a cold start.
    assert!(
        secs[0] >= 120,
        "{DEPLOY_SH}: the app wait is {} s, under 120",
        secs[0]
    );
}

#[test]
fn deploy_sh_hashes_the_config_file_into_the_recreate_decision() {
    // Both the prod and the local call must name the file.
    assert_eq!(
        count_lines(DEPLOY_SH, r"^ *compose_up_if_changed .*config/postiz\.env"),
        2,
        "{DEPLOY_SH}: the compose_up_if_changed calls that name the config file"
    );
}

#[test]
fn deploy_sh_fails_loudly_when_the_bind_mount_source_is_missing() {
    // A missing bind source does not error: the runtime creates a directory at that path.
    text::assert_line(
        DEPLOY_SH,
        r#"\[ -f "\$\{SCRIPT_DIR\}/config/postiz.env" \]"#,
    );
    text::assert_text(DEPLOY_SH, "silently become a directory");
}

#[test]
fn app_healthcheck_probes_the_backend_path() {
    // nginx serves / from the frontend, so a probe on / stays green while the backend is dead.
    // /api/ is proxied to the backend, so its answer, even a 404, proves the process bound.
    text::assert_line(COMPOSE, "http://127.0.0.1:5000/api/");
    text::refute_line(COMPOSE, r"get\('http://127\.0\.0\.1:5000/',");
}

#[test]
fn api_key_capture_is_read_only_with_fixed_path_and_no_log() {
    // The stored Organization.apiKey IS the bearer token, so this playbook only READS it.
    assert_file(STORE_KEY_PB);
    // SELECT only: no mutating SQL anywhere in the play.
    refute_in(
        &non_comment_lines(STORE_KEY_PB),
        "(?i)UPDATE|INSERT|DELETE|ALTER",
        STORE_KEY_PB,
    );
    // The store path and key are fixed, never caller-supplied.
    text::assert_line(STORE_KEY_PB, r#"^    _bao_path: "services/postiz"$"#);
    text::assert_line(STORE_KEY_PB, r#"^    _bao_key: "postiz_api_key"$"#);
    text::refute_line(STORE_KEY_PB, r#"^    _bao_(path|key): "\{\{"#);
    // Every step that touches the key value is no_log'd: the read and parse here, and the
    // fetch/patch/verify chain in the shared write task this playbook includes.
    text::assert_line(STORE_KEY_PB, "include_tasks: tasks/bao-merge-keys.yml");
    for name in ["Read the API key", "Parse what the database"] {
        let block = task_block(STORE_KEY_PB, name);
        assert!(!block.is_empty(), "{STORE_KEY_PB}: `{name}` has no body");
        assert_contains(&block, "no_log: true", name);
    }
    for name in [
        "Fetch the current secret",
        "Merge the differing keys",
        "Create the new path",
        "Verify the merged keys",
        "Require every merged key to hold",
    ] {
        let block = task_block(BAO_MERGE, name);
        assert!(!block.is_empty(), "{BAO_MERGE}: `{name}` has no body");
        assert_contains(&block, "no_log: true", name);
    }
    // This caller refuses to create the path: deploy-postiz owns it.
    assert_contains(
        &task_block(STORE_KEY_PB, "Merge the key into the store"),
        "_bm_on_missing: fail",
        "Merge the key into the store",
    );
    // The one debug prints a LENGTH, never the value: every `_api_key` in the Report block is
    // followed by ` | length`. Strict: each occurrence, not each match of the BRE filter.
    let report = report_block(STORE_KEY_PB);
    assert!(
        !report.is_empty(),
        "{STORE_KEY_PB}: the Report task has no body"
    );
    let occurrences = report.matches("_api_key").count();
    let piped = report.matches("_api_key | length").count();
    assert_eq!(
        occurrences, piped,
        "{STORE_KEY_PB}: an `_api_key` in the Report block is not piped through `| length`"
    );
    assert_contains(&report, "_api_key | length", "the Report block");
    // The transport guard precedes the first request that carries a credential.
    assert_guard_precedes_first_uri(STORE_KEY_PB);
    // Declared as a Semaphore template.
    text::assert_line(TEMPLATES, "^  - name: Store Postiz API Key$");
}

#[test]
fn teardown_resolves_the_compose_file_superset() {
    // A service or volume that exists only in an overlay is invisible to a base-only `down -v`.
    // Both teardown branches must glob the overlays in.
    assert!(
        count_contains(CLEAN_SERVICE, "compose.*.yml") >= 2,
        "{CLEAN_SERVICE}: fewer than two overlay globs"
    );
    // The PROD branch skips compose.local.yml, which references local-only externals.
    text::assert_line(CLEAN_SERVICE, r"compose\.local\.yml\) continue");
    // The base still comes first: overlays reference its services.
    let text = text::read(CLEAN_SERVICE);
    let first_base = first_match(&text, r"for f in docker-compose\.yml compose\.yml")
        .unwrap_or_else(|| panic!("{CLEAN_SERVICE}: no base-file loop"));
    let first_glob = first_line_containing(&text, "compose.*.yml")
        .unwrap_or_else(|| panic!("{CLEAN_SERVICE}: no overlay glob"));
    assert!(
        first_base < first_glob,
        "{CLEAN_SERVICE}: the base loop (line {first_base}) comes after the overlay glob (line {first_glob})"
    );
}
