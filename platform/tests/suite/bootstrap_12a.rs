//! The §12A local-dev bootstrap guards (static tier, with one process test).
//!
//! The genesis deploys the secure foundation (dns, step-ca, caddy, authentik) before Semaphore
//! starts. Semaphore boots last, with its OIDC env validated and gated on the dependencies being
//! up, so a missing dependency never leaves the control plane unbootable. Two probe fixes stay in
//! place: a lazy `ansible_user` default, and `COMPOSE_CMD` passed through from the Mac's
//! podman-compose. Each property is checked on the task that carries it, selected from the parsed
//! playbook, never on the whole file.
//!
//! The usage test runs `scripts/local-dev.sh`: a process test in a module whose tier is static
//! (the module is listed once, in `areas.yml`).
//!
//! Plan: plan/development/LOCAL-DEV-12A-IMPLEMENTATION.md (design: LOCAL-DEV-DEPLOYMENT.md §12A).
//! Replaces platform/tests/test_bootstrap_12a.bats (plan 24, Phase 4).
//!
//! Rows marked `(candidate: pins text)` assert a line of a shell script, a Jinja template or a
//! Makefile, which no parser here reads. They stay as pins until the property has a structural form.
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | fix #2: no deploy playbook uses an eager ansible_user default | `no_playbook_defaults_ansible_user_eagerly` |
//! | fix #2: ansible_user default is lazy where _monorepo_dir is computed | `deploy_dns_computes_monorepo_dir_with_a_lazy_ansible_user_default` |
//! | fix #1: foundation deploys pass COMPOSE_CMD through from local_compose_cmd | `foundation_deploys_pass_compose_cmd_through_with_an_empty_default` |
//! | bootstrap genesis-deploys the foundation before Semaphore starts | `genesis_deploys_the_foundation_before_semaphore_starts` |
//! | genesis loop covers dns step-ca caddy authentik in dependency order | `genesis_loop_covers_dns_step_ca_caddy_authentik_in_dependency_order` |
//! | Semaphore start carries fail-safe OIDC env (jq-validated + step-ca trust) | `semaphore_start_carries_fail_safe_oidc_env_validated_with_jq_and_trusts_step_ca` |
//! | OIDC is gated on deps being ready (fail-safe: Semaphore boots without it) | `oidc_flags_are_empty_unless_its_dependencies_are_ready` |
//! | TLS bundle includes the system roots, not just step-ca (else apk/pip break) | `tls_bundle_includes_the_system_roots_not_just_step_ca` |
//! | podman-compose discovery uses shell (command -v is a builtin) + asserts | `podman_compose_discovery_uses_shell_and_is_asserted` |
//! | preflight checks podman-compose + jq (genesis prereqs) | `preflight_checks_podman_compose_and_jq` (candidate: pins text) |
//! | deploy-authentik waits for the embedded outpost to serve forward_auth | `deploy_authentik_waits_for_the_outpost_to_serve_forward_auth` |
//! | bootstrap configures OpenBao native OIDC (auth/oidc + role + policy) | `bootstrap_configures_openbao_native_oidc_and_its_authentik_app` |
//! | bootstrap pre-creates agent-cloud-admin as a Semaphore global admin | `bootstrap_pre_creates_agent_cloud_admin_as_an_external_semaphore_admin` |
//! | the daily agent-cloud-admin is a rendered entry of the administrator list | `the_daily_agent_cloud_admin_is_a_rendered_entry_of_the_administrator_list` (candidate: pins text) |
//! | https forwarder runs from a non-TCC system path, not the repo (~/Documents) | `https_forwarder_runs_from_a_system_path` (candidate: pins text) |
//! | wrapper exposes a creds subcommand and local-all prints it | `wrapper_exposes_creds_and_local_all_prints_it` (candidate: pins text) |
//! | wrapper reads a whole task log, checking the id before any state is read | `wrapper_refuses_a_non_numeric_task_id_before_reading_state` |
//! | genesis registers repository records before applying templates | `genesis_registers_repository_records_before_applying_templates` |
//! | genesis sources the Caddy edge ports from the declaration, not literals | `genesis_sources_the_caddy_edge_ports_from_the_declaration_not_literals` |
//! | seed: an existing path gets only its missing spec keys, through bao-merge-keys | `seed_merges_only_the_keys_an_existing_path_is_missing` |
//! | seed: a path that does not exist is still created whole, on 404 only | `seed_creates_an_absent_path_whole_and_only_on_404` |

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use tempfile::TempDir;

const BOOTSTRAP: &str = "platform/playbooks/bootstrap-local-dev.yml";
const DEPLOY_DNS: &str = "platform/playbooks/deploy-dns.yml";
const DEPLOY_AUTHENTIK: &str = "platform/playbooks/deploy-authentik.yml";
const LOCAL_DEV: &str = "scripts/local-dev.sh";

fn text(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

fn document(rel: &str) -> Node {
    yaml::load_one(&repo::path(rel)).unwrap_or_else(|| panic!("{rel}: empty document"))
}

fn s(value: &str) -> Node {
    Node::Str(value.to_owned())
}

fn seq(items: &[&str]) -> Node {
    Node::Seq(items.iter().map(|i| s(i)).collect())
}

/// Every value stored under `key` anywhere in `node`, depth first.
fn values_of<'a>(node: &'a Node, key: &str) -> Vec<&'a Node> {
    let mut out = vec![];
    collect_values(node, key, &mut out);
    out
}

fn collect_values<'a>(node: &'a Node, key: &str, out: &mut Vec<&'a Node>) {
    match node {
        Node::Map(entries) => {
            for (k, v) in entries {
                if k.as_str() == Some(key) {
                    out.push(v);
                }
                collect_values(v, key, out);
            }
        }
        Node::Seq(items) => {
            for item in items {
                collect_values(item, key, out);
            }
        }
        _ => {}
    }
}

/// Whether any string inside `node`, a key or a value at any depth, contains `needle`. Scoped by
/// the caller to one task, so a comment or an unrelated task cannot satisfy it.
fn mentions(node: &Node, needle: &str) -> bool {
    match node {
        Node::Str(v) => v.contains(needle),
        Node::Map(entries) => entries
            .iter()
            .any(|(k, v)| mentions(k, needle) || mentions(v, needle)),
        Node::Seq(items) => items.iter().any(|i| mentions(i, needle)),
        _ => false,
    }
}

fn name_contains(task: &Node, needle: &str) -> bool {
    task.get("name")
        .and_then(Node::as_str)
        .is_some_and(|n| n.contains(needle))
}

fn position_named(tasks: &[Node], needle: &str) -> usize {
    tasks
        .iter()
        .position(|t| name_contains(t, needle))
        .unwrap_or_else(|| panic!("no task named like {needle:?}"))
}

fn task_named<'a>(tasks: &'a [Node], needle: &str) -> &'a Node {
    &tasks[position_named(tasks, needle)]
}

fn has_task(tasks: &[Node], needle: &str) -> bool {
    tasks.iter().any(|t| name_contains(t, needle))
}

/// Whether a task's own keys mention `needle`. The walker of `repo::tasks` yields a play, and a
/// block, with its children after it, so the children's keys are left out here: a play must not
/// count as mentioning what its tasks mention.
fn mentions_own(task: &Node, needle: &str) -> bool {
    match task {
        Node::Map(entries) => entries.iter().any(|(k, v)| {
            !matches!(
                k.as_str(),
                Some(
                    "block"
                        | "rescue"
                        | "always"
                        | "tasks"
                        | "pre_tasks"
                        | "post_tasks"
                        | "handlers"
                )
            ) && (mentions(k, needle) || mentions(v, needle))
        }),
        _ => false,
    }
}

/// The first task whose own keys mention `needle`.
fn position_mentioning(tasks: &[Node], needle: &str) -> usize {
    tasks
        .iter()
        .position(|t| mentions_own(t, needle))
        .unwrap_or_else(|| panic!("no task mentions {needle:?}"))
}

/// The local inventory the bootstrap writes: the one `_inv_ini` value, a block scalar of INI.
fn inventory_ini(doc: &Node) -> String {
    let found = values_of(doc, "_inv_ini");
    assert_eq!(found.len(), 1, "one _inv_ini definition expected");
    found[0].as_str().expect("_inv_ini is a string").to_owned()
}

/// The lines of one INI section, from its header to the next header. Headers are matched after
/// leading blanks are dropped, as the INI reader in ansible does.
fn ini_section(ini: &str, header: &str) -> String {
    let mut inside = false;
    let mut out = String::new();
    for line in ini.lines() {
        let t = line.trim_start();
        if inside && t.starts_with('[') {
            break;
        }
        if !inside && t.starts_with(&format!("[{header}]")) {
            inside = true;
            continue;
        }
        if inside {
            out.push_str(line);
            out.push('\n');
        }
    }
    out
}

/// The one value of `key` in one INI section, the text after the `=` of its line. A key that
/// occurs twice is refused: ansible-inventory keeps the later definition (checked on ansible-core
/// 2.21.0, `caddy_http_port` given twice reads back the second), so the first one checked would
/// pass while the inventory carries another value.
fn ini_single<'a>(section: &'a str, key: &str) -> &'a str {
    let found: Vec<&str> = section
        .lines()
        .filter_map(|line| {
            let (k, v) = line.trim().split_once('=')?;
            (k.trim_end() == key).then_some(v)
        })
        .collect();
    assert_eq!(
        found.len(),
        1,
        "{key} must occur exactly once in its section: {found:?}"
    );
    found[0]
}

#[test]
fn no_playbook_defaults_ansible_user_eagerly() {
    // Arrange: the forbidden form, a Jinja concatenation of ansible_user, eager under `~`.
    // Act
    let hits = repo::grep("~ ansible_user ~", &["platform/playbooks"]);

    // Assert
    assert!(
        hits.is_empty(),
        "eager ansible_user default:\n{}",
        hits.join("\n")
    );
}

#[test]
fn deploy_dns_computes_monorepo_dir_with_a_lazy_ansible_user_default() {
    // Arrange
    let doc = document(DEPLOY_DNS);

    // Act: every definition of _monorepo_dir in the playbook
    let defs: Vec<&str> = values_of(&doc, "_monorepo_dir")
        .into_iter()
        .filter_map(Node::as_str)
        .collect();

    // Assert
    assert!(!defs.is_empty(), "deploy-dns.yml computes no _monorepo_dir");
    for def in defs {
        assert!(
            def.contains("ansible_user | default('deploy')"),
            "an eager or missing ansible_user default: {def}"
        );
    }
}

#[test]
fn foundation_deploys_pass_compose_cmd_through_with_an_empty_default() {
    // Arrange: an empty default, not omit (omit leaks its placeholder into the environment, which
    // deploy.sh then runs: rc 127). common.sh treats an empty COMPOSE_CMD as unset.
    const PASSTHROUGH: &str = "{{ local_compose_cmd | default('') }}";

    // Act + Assert, one deploy at a time so a failure names it
    for svc in ["dns", "step-ca", "caddy", "authentik"] {
        let doc = document(&format!("platform/playbooks/deploy-{svc}.yml"));
        let passed: Vec<&str> = values_of(&doc, "COMPOSE_CMD")
            .into_iter()
            .filter_map(Node::as_str)
            .collect();
        assert!(
            passed.contains(&PASSTHROUGH),
            "deploy-{svc}.yml does not pass COMPOSE_CMD through: {passed:?}"
        );
    }
    let omitted = repo::grep(
        r"local_compose_cmd \| default\(omit\)",
        &["platform/playbooks"],
    );
    assert!(
        omitted.is_empty(),
        "default(omit) leaks a placeholder into the environment:\n{}",
        omitted.join("\n")
    );
}

#[test]
fn genesis_deploys_the_foundation_before_semaphore_starts() {
    // Arrange
    let tasks = repo::tasks(BOOTSTRAP);

    // Act
    let genesis = position_named(&tasks, "Genesis-deploy the secure foundation");
    let semaphore = position_named(&tasks, "Start local Semaphore");

    // Assert
    assert!(
        genesis < semaphore,
        "genesis at task {genesis}, the Semaphore start at {semaphore}"
    );
}

#[test]
fn genesis_loop_covers_dns_step_ca_caddy_authentik_in_dependency_order() {
    // Arrange
    let tasks = repo::tasks(BOOTSTRAP);

    // Act
    let genesis = task_named(&tasks, "Genesis-deploy the secure foundation");

    // Assert
    assert_eq!(
        genesis.get("loop"),
        Some(&seq(&["dns", "step-ca", "caddy", "authentik"]))
    );
}

#[test]
fn semaphore_start_carries_fail_safe_oidc_env_validated_with_jq_and_trusts_step_ca() {
    // Arrange
    let tasks = repo::tasks(BOOTSTRAP);
    let env_file = task_named(&tasks, "Write the Semaphore OIDC env-file");
    let flags = task_named(&tasks, "Compose Semaphore OIDC/TLS run flags");
    let start = task_named(&tasks, "Start local Semaphore");

    // Act + Assert
    assert!(
        mentions(env_file, "SEMAPHORE_OIDC_PROVIDERS"),
        "the OIDC env-file task does not write SEMAPHORE_OIDC_PROVIDERS"
    );
    assert!(
        has_task(&tasks, "Validate the OIDC JSON with jq"),
        "no jq validation of the OIDC JSON"
    );
    assert!(
        mentions(flags, "SSL_CERT_FILE"),
        "the OIDC run flags do not trust step-ca"
    );
    assert!(
        mentions(flags, "SEMAPHORE_WEB_ROOT"),
        "the OIDC run flags do not set SEMAPHORE_WEB_ROOT"
    );
    assert!(
        mentions(start, "_sem_oidc_flags"),
        "the Semaphore start does not pass the OIDC run flags"
    );
}

#[test]
fn oidc_flags_are_empty_unless_its_dependencies_are_ready() {
    // Arrange
    let tasks = repo::tasks(BOOTSTRAP);

    // Act
    let flags = task_named(&tasks, "Compose Semaphore OIDC/TLS run flags");

    // Assert: the flags are empty unless _oidc_ready (fail-safe: Semaphore boots without OIDC)
    assert!(
        mentions(flags, "if (_oidc_ready | bool) else ''"),
        "the OIDC flags are not gated on _oidc_ready"
    );
}

#[test]
fn tls_bundle_includes_the_system_roots_not_just_step_ca() {
    // Arrange: SSL_CERT_FILE replaces the whole trust store, so the bundle must carry the public
    // roots too, or apk and pip break on public TLS.
    let tasks = repo::tasks(BOOTSTRAP);

    // Act
    let bundle = task_named(&tasks, "Build the Semaphore TLS trust bundle");

    // Assert
    assert!(
        mentions(bundle, "ca-certificates.crt"),
        "the trust bundle has no system roots"
    );
}

#[test]
fn podman_compose_discovery_uses_shell_and_is_asserted() {
    // Arrange
    let tasks = repo::tasks(BOOTSTRAP);

    // Act
    let locate = task_named(&tasks, "Locate podman-compose on the Mac");

    // Assert: `command -v` is a shell builtin, so the discovery is a shell task, and the genesis
    // path asserts that it found podman-compose.
    assert_eq!(
        locate.get("ansible.builtin.shell").and_then(Node::as_str),
        Some("command -v podman-compose")
    );
    assert!(has_task(
        &tasks,
        "Require podman-compose for the Mac-direct genesis path"
    ));
}

#[test]
fn preflight_checks_podman_compose_and_jq() {
    // Arrange, Act
    let script = text(LOCAL_DEV);

    // Assert (candidate: pins text)
    assert!(
        script.contains("podman-compose"),
        "{LOCAL_DEV} does not check for podman-compose"
    );
    assert!(script.contains("jq"), "{LOCAL_DEV} does not check for jq");
}

#[test]
fn deploy_authentik_waits_for_the_outpost_to_serve_forward_auth() {
    // Arrange
    let tasks = repo::tasks(DEPLOY_AUTHENTIK);

    // Act
    let wait = task_named(
        &tasks,
        "Wait for the embedded outpost to serve forward_auth",
    );

    // Assert: the probe is the outpost's Caddy auth endpoint, not just the API going green.
    assert!(
        mentions(wait, "outpost.goauthentik.io/auth/caddy"),
        "the outpost wait does not probe the forward_auth endpoint"
    );
}

#[test]
fn bootstrap_configures_openbao_native_oidc_and_its_authentik_app() {
    // Arrange
    let tasks = repo::tasks(BOOTSTRAP);
    let config = task_named(&tasks, "Configure OpenBao auth/oidc against Authentik");
    let role = task_named(&tasks, "Write the OpenBao oidc role");
    let blueprint = document("platform/services/authentik/deployment/blueprints/openbao-oidc.yaml");

    // Act + Assert
    assert!(mentions(config, "auth/oidc/config"));
    assert!(mentions(config, "oidc_discovery_ca_pem"));
    assert!(mentions(role, "auth/oidc/role/platform-admins"));
    assert!(
        values_of(&blueprint, "slug")
            .iter()
            .any(|v| v.as_str() == Some("openbao-oidc")),
        "the Authentik blueprint declares no openbao-oidc application"
    );
}

#[test]
fn bootstrap_pre_creates_agent_cloud_admin_as_an_external_semaphore_admin() {
    // Arrange: an OIDC user lands non-admin; a pre-created external admin makes the SSO login
    // link to it.
    let tasks = repo::tasks(BOOTSTRAP);
    let create = task_named(&tasks, "Create agent-cloud-admin Semaphore admin user");

    // Act + Assert
    assert!(
        values_of(create, "external").contains(&&Node::Bool(true)),
        "the admin user is not external"
    );
    assert!(
        values_of(create, "admin").contains(&&Node::Bool(true)),
        "the user is not a global admin"
    );
}

#[test]
fn the_daily_agent_cloud_admin_is_a_rendered_entry_of_the_administrator_list() {
    // Arrange: the fixed blueprint became an entry of the declared administrator list.
    let template = "platform/services/authentik/deployment/templates/platform-admins.yaml.j2";
    assert!(repo::path(template).is_file(), "{template} is missing");
    assert!(
        !repo::path("platform/services/authentik/deployment/blueprints/agent-cloud-admin.yaml")
            .exists(),
        "the fixed agent-cloud-admin blueprint is still present"
    );
    let admins = document("platform/playbooks/vars/platform-admins.yml");
    let lists = values_of(&admins, "_platform_admins_default");
    let defaults = lists
        .first()
        .and_then(|list| list.as_seq())
        .unwrap_or_default();
    let secrets = document("platform/playbooks/vars/secret-declarations/authentik.yml");

    // Act + Assert
    assert!(
        text(template).contains("name, platform-admins"),
        "candidate: the template does not render platform-admins"
    );
    assert!(
        defaults.iter().any(|a| {
            a.get("key") == Some(&s("agent_cloud_admin"))
                && a.get("username") == Some(&s("agent-cloud-admin"))
        }),
        "no agent_cloud_admin entry with username agent-cloud-admin"
    );
    assert!(
        values_of(&secrets, "_admin_secret_defs")
            .iter()
            .any(|d| d.as_str().is_some_and(|t| t.contains("'_password'"))),
        "the authentik declaration has no per-administrator password"
    );
    assert!(
        text("platform/services/authentik/deployment/templates/env.j2")
            .contains("for a in _platform_admins"),
        "candidate: env.j2 does not loop over the administrator list"
    );
}

#[test]
fn https_forwarder_runs_from_a_system_path() {
    // Arrange: a root LaunchDaemon cannot exec a script under a TCC-protected home directory.
    let script = text(LOCAL_DEV);

    // Act, Assert (candidate: pins text)
    assert!(
        script.contains(r#"_FORWARDER_BIN="/usr/local/bin/"#),
        "the forwarder is not installed under /usr/local/bin"
    );
    assert!(
        script.contains("s|__WRAPPER__|${_FORWARDER_BIN}|g"),
        "the forwarder wrapper is not substituted with the system path"
    );
}

#[test]
fn wrapper_exposes_creds_and_local_all_prints_it() {
    // Arrange
    let script = text(LOCAL_DEV);
    let makefile = text("Makefile");

    // Act, Assert (candidate: pins text)
    assert!(
        script.lines().any(|l| l.starts_with("  creds)")),
        "the wrapper has no creds subcommand"
    );
    assert!(
        script.contains("secret/data/services/authentik"),
        "creds does not read the authentik bootstrap password from OpenBao"
    );
    assert!(
        makefile.contains("LOCAL_DEV) creds"),
        "local-all does not print the credentials"
    );
}

#[test]
fn wrapper_refuses_a_non_numeric_task_id_before_reading_state() {
    // Arrange: a scrubbed HOME, so the wrapper cannot reach real state while it runs.
    let home = TempDir::new().expect("temp home");
    let script = repo::path(LOCAL_DEV).display().to_string();
    let mut cmd = process::scrubbed(&script, home.path());
    cmd.current_dir(repo::root())
        .args(["output", "not-a-number"]);

    // Act
    let assert = cmd.assert().failure();

    // Assert: the usage line, on stdout or stderr
    let out = assert.get_output();
    let both = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        both.contains("usage: local-dev.sh output <task-id>"),
        "no usage line for a non-numeric task id: {both}"
    );
}

#[test]
fn genesis_registers_repository_records_before_applying_templates() {
    // Arrange: setup-templates.yml refuses a template that names a record which does not exist,
    // so the records must be registered first.
    let tasks = repo::tasks(BOOTSTRAP);

    // Act
    let repos = position_mentioning(&tasks, "bootstrap-semaphore-repositories.yml");
    let templates = position_mentioning(&tasks, "semaphore/setup-templates.yml");

    // Assert
    assert!(
        repos < templates,
        "records registered at task {repos}, templates applied at {templates}"
    );
}

#[test]
fn genesis_sources_the_caddy_edge_ports_from_the_declaration_not_literals() {
    // Arrange: the inline inventory the genesis writes declares the same host as the working
    // inventory, so a restated literal would win during bootstrap.
    let doc = document(BOOTSTRAP);
    let ini = inventory_ini(&doc);
    let caddy = ini_section(&ini, "caddy_svc:vars");
    let postiz = ini_section(&ini, "postiz_svc:vars");
    assert!(!caddy.is_empty(), "no [caddy_svc:vars] section");
    assert!(!postiz.is_empty(), "no [postiz_svc:vars] section");

    // Act + Assert: each port is the declared expression, which is never the literal default
    assert_eq!(
        ini_single(&caddy, "caddy_http_port"),
        "{{ _caddy_decl.caddy_http_port | default(8088, true) }}"
    );
    assert_eq!(
        ini_single(&caddy, "caddy_https_port"),
        "{{ _edge_https_port }}"
    );
    assert_eq!(
        ini_single(&postiz, "postiz_public_url"),
        "https://postiz.{{ _dev_zone }}:{{ _edge_https_port }}"
    );
    assert_eq!(
        ini_single(&postiz, "postiz_authentik_base"),
        "https://auth.{{ _dev_zone }}:{{ _edge_https_port }}"
    );
    assert!(
        !postiz.contains(":8443"),
        "a literal :8443 in [postiz_svc:vars]"
    );
    assert_eq!(
        values_of(&doc, "_edge_https_port"),
        vec![&s(
            "{{ _caddy_decl.caddy_https_port | default(8443, true) }}"
        )],
        "the edge HTTPS port is not defined exactly once, from the declaration"
    );
}

#[test]
fn seed_merges_only_the_keys_an_existing_path_is_missing() {
    // Arrange
    let tasks = repo::tasks(BOOTSTRAP);
    let compute = task_named(
        &tasks,
        "Work out the seed keys each existing path is missing",
    );
    let merge = task_named(&tasks, "Merge the seed keys an existing path is missing");

    // Act + Assert: a spec key is merged only when the path does not already hold it with a value
    assert!(mentions(compute, "rejectattr('key', 'in', _seed_held)"));
    assert!(mentions(compute, "selectattr('value')"));
    assert_eq!(
        compute.get("loop").and_then(Node::as_str),
        Some("{{ _seed_check.results | selectattr('status', 'equalto', 200) | list }}")
    );
    assert_eq!(compute.get("no_log"), Some(&Node::Bool(true)));

    // The write goes through the shared merge-patch task, only for the paths that are missing
    // something, and never in check mode.
    assert_eq!(
        merge
            .get("ansible.builtin.include_tasks")
            .and_then(Node::as_str),
        Some("tasks/bao-merge-keys.yml")
    );
    assert_eq!(
        merge
            .get("vars")
            .and_then(|v| v.get("_bm_on_missing"))
            .and_then(Node::as_str),
        Some("create")
    );
    assert!(mentions(merge, "_seed_missing"));
    assert!(mentions(merge, "selectattr('value')"));
    assert_eq!(
        merge.get("when").and_then(Node::as_str),
        Some("not ansible_check_mode")
    );
    assert_eq!(merge.get("no_log"), Some(&Node::Bool(true)));
}

#[test]
fn seed_creates_an_absent_path_whole_and_only_on_404() {
    // Arrange
    let tasks = repo::tasks(BOOTSTRAP);
    let create = task_named(&tasks, "Seed LOCAL_FAKE secrets for absent services");

    // Act
    let guards = create
        .get("when")
        .and_then(Node::as_seq)
        .unwrap_or_default();
    let method = create
        .get("ansible.builtin.uri")
        .and_then(|u| u.get("method"))
        .and_then(Node::as_str);

    // Assert
    assert!(
        guards.contains(&s("item.status == 404")),
        "the seed does not check for a 404"
    );
    assert_eq!(method, Some("POST"));
}
