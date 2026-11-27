//! The playbook tier of `platform/playbooks/fetch-proxmox-isos.yml` (AC-1112).
//!
//! - The declared ISO list is refused before any request when an entry is malformed: a missing or extra key, a bad
//!   name, node, storage, filename, url or sha256, a newline at the end of a value, a duplicate name, or a duplicate
//!   storage and filename pair. A list passed as an extra var is refused too.
//! - A dry run sends GET requests only and reports `would fetch` for an absent ISO and `would re-verify` for a
//!   present one.
//! - A real run sends one download request per declared ISO, each with its digest and `verify-certificates=1`, waits
//!   for each task before the next, sends no DELETE, and re-reads each storage.
//! - A download that ends with a checksum mismatch fails the run and names the ISO.
//! - The token's download privileges are read and checked before any write. A privilege is held when its key is
//!   present, whatever its value.
//! - The play refuses plain HTTP, imports the extra-var guard first and the reviewed-checkout gate second, and no task
//!   includes the restore-target guard.
//!
//! Each behavioural test runs the play (or one of its tasks, unchanged) through ansible-playbook on localhost against a
//! fake Proxmox API (`http::Server`). The credentials come from a fake `_pve_data`, and the HTTPS and transport
//! checks are no-ops in the full-play runs, which use an `http://` fake. The stub run proves the Jinja and the
//! requests this play makes, not privilege: the fake API grants whatever a test sets, and the play has no `become`
//! (L-021). Fixtures send no real credential: the token and secret are sentinels.

use agent_cloud_tests::http::{Mock, Server, matchers};
use agent_cloud_tests::{playbook, process, repo, seed, text};
use serde_json::{Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/fetch-proxmox-isos.yml";
const TASKS: &str = "platform/playbooks/tasks/fetch-proxmox-iso.yml";
const TOKEN_ID: &str = "t@pve!x";
const SECRET: &str = "SENTINEL-PVE-SECRET";
const NODE: &str = "n1";
const STORAGE: &str = "s1";
const CONTENT: &str = "/api2/json/nodes/n1/storage/s1/content";
const DOWNLOAD: &str = "/api2/json/nodes/n1/storage/s1/download-url";

/// A sha256-shaped digest: 64 lowercase hex characters.
fn sha(digit: &str) -> String {
    digit.repeat(64)
}

/// A well-formed declared ISO on the fixture node and storage.
fn iso(name: &str, file: &str) -> Value {
    json!({"name": name, "node": NODE, "storage": STORAGE, "filename": file,
           "url": format!("https://iso.example.invalid/{file}"), "sha256": sha("a")})
}

/// The fixture list: one absent ISO and one already present, the absent one first.
fn two_isos() -> Value {
    json!([
        iso("Installer A", "installer-a.iso"),
        iso("Installer B", "installer-b.iso")
    ])
}

/// The play with its tasks, freshly parsed.
fn fetch_play() -> Value {
    playbook::plays(PLAYBOOK)
        .into_iter()
        .find(|p| p.get("tasks").is_some())
        .expect("the fetch play")
}

fn task(play: &Value, name: &str) -> Value {
    play["tasks"]
        .as_array()
        .expect("tasks")
        .iter()
        .find(|t| t["name"] == name)
        .unwrap_or_else(|| panic!("no task named {name:?}"))
        .clone()
}

/// The play lifted for a fake API at `url`: the credentials come from a fake `_pve_data`, the OpenBao transport
/// include and the HTTPS assert are no-ops, and the relative task include points at the repository.
fn lifted_play(url: &str) -> Value {
    let mut play = fetch_play();
    let pb_dir = repo::path("platform/playbooks");
    for task in play["tasks"].as_array_mut().expect("tasks") {
        let name = task["name"].as_str().unwrap_or_default().to_owned();
        match name.as_str() {
            "Read Proxmox API credentials from OpenBao" => {
                *task = json!({"name": name, "ansible.builtin.set_fact": {"_pve_data": {
                    "url": url, "token_id": TOKEN_ID, "api_token": SECRET}}, "no_log": true});
            }
            "Refuse public cleartext OpenBao transport" | "Require Proxmox API over HTTPS" => {
                *task = json!({"name": name, "ansible.builtin.meta": "noop"});
            }
            _ => {}
        }
        if let Some(target) = task
            .get("ansible.builtin.include_tasks")
            .and_then(Value::as_str)
            .filter(|t| t.starts_with("tasks/"))
            .map(str::to_owned)
        {
            task["ansible.builtin.include_tasks"] =
                json!(pb_dir.join(target).display().to_string());
        }
    }
    play
}

struct Run {
    ok: bool,
    out: String,
}

/// Runs `play` on localhost. `host_vars` are inventory vars of localhost; `extra` is passed with `-e @file`; `check`
/// adds `--check`.
fn run(play: Value, host_vars: Value, extra: Option<Value>, check: bool) -> Run {
    let tmp = TempDir::new().unwrap();
    let mut vars = json!({"ansible_connection": "local"});
    vars.as_object_mut()
        .unwrap()
        .extend(host_vars.as_object().unwrap().clone());
    let inv = playbook::write(
        tmp.path(),
        "inv.json",
        &json!({"all": {"hosts": {"localhost": vars}}}),
    );
    let path = playbook::write(tmp.path(), "play.json", &json!([play]));
    let mut cmd = process::ansible_playbook(
        tmp.path(),
        &inv.display().to_string(),
        &path.display().to_string(),
    );
    if let Some(extra) = extra {
        let file = playbook::write(tmp.path(), "extra.json", &extra);
        cmd.arg("-e").arg(format!("@{}", file.display()));
    }
    if check {
        cmd.arg("--check");
    }
    let output = process::run(&mut cmd);
    Run {
        ok: output.status.success(),
        out: process::output_text(&output),
    }
}

/// Runs the play with `list` as the declared ISO list, against the fake API at `server`.
fn run_declared(server: &Server, list: Value, check: bool) -> Run {
    run(
        lifted_play(&server.url()),
        json!({"proxmox_isos": list}),
        None,
        check,
    )
}

/// A single-task localhost play, as the single-task tests lift one task of the play.
fn single(tasks: Vec<Value>) -> Value {
    json!({"hosts": "localhost", "gather_facts": false, "connection": "local", "tasks": tasks})
}

fn reply(method: &str, path: &str, status: u16, body: Value) -> Mock {
    Mock::given(matchers::method(method))
        .and(matchers::path(path))
        .respond_with(seed::reply(status, &body))
}

/// The privilege map the token holds on one path, as Proxmox answers GET /access/permissions?path=.
fn perms(server: &Server, path: &str, privileges: Value) {
    server.mount(
        Mock::given(matchers::method("GET"))
            .and(matchers::path("/api2/json/access/permissions"))
            .and(matchers::query_param("path", path))
            .respond_with(seed::reply(200, &json!({"data": {(path): privileges}}))),
    );
}

/// The token's privileges on the three paths a run reads: the storage, the node and the root.
struct Grants {
    storage: Value,
    node: Value,
    root: Value,
}

fn full_grants() -> Grants {
    Grants {
        storage: json!({"Datastore.AllocateTemplate": 1}),
        node: json!({"Sys.AccessNetwork": 1}),
        root: json!({}),
    }
}

/// The version reply, and the node list with the fixture node at `status`.
fn mount_version_and_nodes(server: &Server, status: &str) {
    server.mount(reply(
        "GET",
        "/api2/json/version",
        200,
        json!({"data": {"version": "9.0", "release": "9.0", "repoid": "fake"}}),
    ));
    server.mount(reply(
        "GET",
        "/api2/json/nodes",
        200,
        json!({"data": [{"node": NODE, "status": status}]}),
    ));
}

/// The fake API for one run on `n1` and `s1`: the version, the nodes (`n1` online), the storage status (active, with
/// ISO content), the ISO listing (`before` until the first read, `after` from the second), and the privileges.
fn mount_reads(server: &Server, before: Value, after: Value, grants: &Grants) {
    mount_version_and_nodes(server, "online");
    server.mount(reply(
        "GET",
        "/api2/json/nodes/n1/storage/s1/status",
        200,
        json!({"data": {"active": 1, "avail": 1000, "total": 2000, "content": "iso,vztmpl"}}),
    ));
    server.mount(
        reply("GET", CONTENT, 200, json!({"data": before}))
            .up_to_n_times(1)
            .with_priority(1),
    );
    server.mount(reply("GET", CONTENT, 200, json!({"data": after})));
    perms(server, "/storage/s1", grants.storage.clone());
    perms(server, "/nodes/n1", grants.node.clone());
    perms(server, "/", grants.root.clone());
}

/// The download request and the task-status poll: every download is accepted, and its task ends with `exitstatus`.
fn mount_downloads(server: &Server, exitstatus: &str) {
    server.mount(
        Mock::given(matchers::method("POST"))
            .and(matchers::path(DOWNLOAD))
            .respond_with(seed::reply(
                200,
                &json!({"data": "UPID:n1:0:0:0:download:s1:t:"}),
            )),
    );
    server.mount(
        Mock::given(matchers::method("GET"))
            .and(matchers::path_regex(
                r"^/api2/json/nodes/n1/tasks/.+/status$",
            ))
            .respond_with(seed::reply(
                200,
                &json!({"data": {"status": "stopped", "exitstatus": exitstatus}}),
            )),
    );
}

/// Every request as "METHOD path?query", in arrival order.
fn sent(server: &Server) -> Vec<String> {
    seed::recorded(server)
        .into_iter()
        .map(|(method, path)| format!("{method} {path}"))
        .collect()
}

/// The POST, PUT and DELETE requests, as [`sent`] shows them.
fn no_writes(server: &Server) -> Vec<String> {
    seed::recorded(server)
        .into_iter()
        .filter(|(method, _)| ["POST", "PUT", "DELETE"].contains(&method.as_str()))
        .map(|(method, path)| format!("{method} {path}"))
        .collect()
}

/// The bodies of the download requests, in arrival order.
fn download_bodies(server: &Server) -> Vec<String> {
    server
        .requests()
        .iter()
        .filter(|r| r.method.as_str() == "POST" && r.url.path() == DOWNLOAD)
        .map(|r| String::from_utf8_lossy(&r.body).into_owned())
        .collect()
}

/// Refused before any request: the run is red, names the refusal, and the fake API received nothing.
fn refused_before_any_request(server: &Server, out: &Run, part: &str) {
    assert!(!out.ok, "{}", out.out);
    assert!(out.out.contains(part), "missing {part:?}: {}", out.out);
    assert!(
        sent(server).is_empty(),
        "requests before the refusal: {:?}",
        sent(server)
    );
}

#[test]
fn an_entry_without_a_sha256_or_with_a_bad_one_is_refused_before_any_request() {
    // Arrange: an entry with no sha256 key, and one whose digest is upper case.
    let server = Server::start();
    let mut missing = iso("Installer A", "installer-a.iso");
    missing.as_object_mut().unwrap().remove("sha256");
    let mut upper = iso("Installer A", "installer-a.iso");
    upper["sha256"] = json!(sha("A"));

    // Act
    let no_key = run_declared(&server, json!([missing]), false);
    let bad_digest = run_declared(&server, json!([upper]), false);

    // Assert
    refused_before_any_request(&server, &no_key, "entry 0 must have exactly the keys");
    refused_before_any_request(
        &server,
        &bad_digest,
        "entry 0 sha256 must be 64 lowercase hexadecimal",
    );
}

#[test]
fn a_filename_that_is_not_a_plain_lowercase_iso_is_refused_before_any_request() {
    // Arrange + Act + Assert: a space (Proxmox would rewrite it), an upper-case extension, an image extension, and a
    // newline after a valid name (a $-anchored pattern would pass it). Each is refused with no request sent.
    for file in ["Win Server.iso", "x.ISO", "x.img", "x.iso\n"] {
        let server = Server::start();
        let out = run_declared(&server, json!([iso("Installer A", file)]), false);
        refused_before_any_request(&server, &out, "entry 0 filename must be");
    }
}

/// The fixture entry with one value replaced.
fn with(mut entry: Value, key: &str, value: &str) -> Value {
    entry[key] = json!(value);
    entry
}

#[test]
fn a_name_node_storage_url_or_extra_key_outside_its_pattern_is_refused_before_any_request() {
    // Arrange: one entry per row, each with one value outside its pattern (or one key too many). A url whose
    // final newline a $-anchored pattern would pass is refused too.
    let mut extra_key = iso("Installer A", "installer-a.iso");
    extra_key["comment"] = json!("not a declared key");
    let rows = [
        (
            with(iso("Installer A", "installer-a.iso"), "name", "Bad/Name"),
            "entry 0 name must be",
        ),
        (
            with(iso("Installer A", "installer-a.iso"), "node", "n 1"),
            "entry 0 node must be",
        ),
        (
            with(iso("Installer A", "installer-a.iso"), "storage", "s/1"),
            "entry 0 storage must be",
        ),
        (
            with(
                iso("Installer A", "installer-a.iso"),
                "url",
                "http://iso.example.invalid/installer-a.iso",
            ),
            "entry 0 url must be",
        ),
        (
            with(
                iso("Installer A", "installer-a.iso"),
                "url",
                "https://iso.example.invalid/a b.iso",
            ),
            "entry 0 url must be",
        ),
        (
            with(
                iso("Installer A", "installer-a.iso"),
                "url",
                "https://iso.example.invalid/installer-a.iso\n",
            ),
            "entry 0 url must be",
        ),
        (extra_key, "entry 0 must have exactly the keys"),
    ];

    // Act + Assert: each row is refused naming its entry and field, with no request sent.
    for (entry, part) in rows {
        let server = Server::start();
        let out = run_declared(&server, json!([entry]), false);
        refused_before_any_request(&server, &out, part);
    }
}

#[test]
fn a_duplicate_name_or_storage_and_filename_pair_is_refused_before_any_request() {
    // Arrange: two entries with one name, and two entries with one storage and filename.
    let server = Server::start();
    let same_name = json!([
        iso("Installer A", "installer-a.iso"),
        iso("Installer A", "installer-b.iso")
    ]);
    let same_file = json!([
        iso("Installer A", "installer-a.iso"),
        iso("Installer B", "installer-a.iso")
    ]);

    // Act
    let name = run_declared(&server, same_name, false);
    let pair = run_declared(&server, same_file, false);

    // Assert
    refused_before_any_request(&server, &name, "entry 1 repeats a name already declared");
    refused_before_any_request(
        &server,
        &pair,
        "entry 1 repeats a storage and filename pair",
    );
}

#[test]
fn the_iso_list_as_an_extra_var_is_refused_and_as_inventory_it_passes() {
    // Arrange: the play's own refusal, alone.
    let refusal = task(&fetch_play(), "Refuse the ISO list as an extra var");
    let list = json!({"proxmox_isos": two_isos()});

    // Act
    let from_inventory = run(single(vec![refusal.clone()]), list.clone(), None, false);
    let as_extra_var = run(single(vec![refusal]), json!({}), Some(list), false);

    // Assert: inventory passes; the same value as an extra var is refused with the task's message.
    assert!(from_inventory.ok, "{}", from_inventory.out);
    assert!(!as_extra_var.ok, "{}", as_extra_var.out);
    assert!(
        as_extra_var.out.contains("never as an extra var"),
        "{}",
        as_extra_var.out
    );
}

#[test]
fn a_dry_run_sends_only_reads_and_reports_fetch_for_absent_and_re_verify_for_present() {
    // Arrange: the first ISO is absent, the second present; every read answers.
    let server = Server::start();
    let present = json!([{"volid": "s1:iso/installer-b.iso", "content": "iso"}]);
    mount_reads(&server, present.clone(), present, &full_grants());
    // The download routes answer, so a write sent in the dry run is recorded and caught by `no_writes`, not by a 404.
    mount_downloads(&server, "OK");

    // Act
    let out = run_declared(&server, two_isos(), true);

    // Assert: green, the report names each action with "would", and no write was sent.
    assert!(out.ok, "{}", out.out);
    assert!(out.out.contains("would fetch"), "{}", out.out);
    assert!(out.out.contains("would re-verify"), "{}", out.out);
    assert!(no_writes(&server).is_empty(), "{:?}", no_writes(&server));
}

#[test]
fn a_real_run_posts_each_declared_iso_once_with_its_digest_and_sends_no_delete() {
    // Arrange: the first ISO absent, the second present; the listing gains the first after its download.
    let server = Server::start();
    let before = json!([{"volid": "s1:iso/installer-b.iso", "content": "iso"}]);
    let after = json!([{"volid": "s1:iso/installer-a.iso", "content": "iso"},
                       {"volid": "s1:iso/installer-b.iso", "content": "iso"}]);
    mount_reads(&server, before, after, &full_grants());
    mount_downloads(&server, "OK");

    // Act
    let out = run_declared(&server, two_isos(), false);

    // Assert: green; exactly one download request per declared ISO, each with its own digest and the verify flag;
    // every download's status poll follows its own request (one at a time); no DELETE.
    assert!(out.ok, "{}", out.out);
    let bodies = download_bodies(&server);
    assert_eq!(bodies.len(), 2, "{bodies:?}");
    for (file, body) in [
        ("installer-a.iso", &bodies[0]),
        ("installer-b.iso", &bodies[1]),
    ] {
        assert!(body.contains(&format!("filename={file}")), "{body}");
        assert!(body.contains(&format!("checksum={}", sha("a"))), "{body}");
        assert!(body.contains("content=iso"), "{body}");
        assert!(body.contains("checksum-algorithm=sha256"), "{body}");
        assert!(body.contains("verify-certificates=1"), "{body}");
    }
    let order: Vec<String> = sent(&server)
        .into_iter()
        .filter(|s| s.contains("download-url") || s.contains("/tasks/"))
        .collect();
    assert_eq!(order.len(), 4, "{order:?}");
    assert!(
        order[0].starts_with("POST") && order[1].starts_with("GET"),
        "{order:?}"
    );
    assert!(
        order[2].starts_with("POST") && order[3].starts_with("GET"),
        "{order:?}"
    );
    assert!(
        seed::recorded(&server)
            .iter()
            .all(|(method, _)| method != "DELETE")
    );
}

#[test]
fn a_download_that_ends_with_a_checksum_mismatch_fails_the_run_and_names_the_iso() {
    // Arrange: the first download's task ends with a checksum mismatch.
    let server = Server::start();
    mount_reads(&server, json!([]), json!([]), &full_grants());
    mount_downloads(&server, "checksum mismatch: expected a, got b");

    // Act
    let out = run_declared(&server, two_isos(), false);

    // Assert: red, naming the ISO and Proxmox's exit status text.
    assert!(!out.ok, "{}", out.out);
    assert!(out.out.contains("Installer A"), "{}", out.out);
    assert!(out.out.contains("checksum mismatch"), "{}", out.out);
}

#[test]
fn a_missing_allocate_template_privilege_is_refused_before_any_post_and_names_it() {
    // Arrange: the token holds no Datastore.AllocateTemplate on the storage (it holds another privilege there).
    let server = Server::start();
    let grants = Grants {
        storage: json!({"Datastore.Allocate": 1}),
        ..full_grants()
    };
    mount_reads(&server, json!([]), json!([]), &grants);
    mount_downloads(&server, "OK");

    // Act
    let out = run_declared(&server, two_isos(), false);

    // Assert: refused, naming the privilege and its path, and no download was requested.
    assert!(!out.ok, "{}", out.out);
    assert!(
        out.out
            .contains("Datastore.AllocateTemplate on /storage/s1"),
        "{}",
        out.out
    );
    assert!(no_writes(&server).is_empty(), "{:?}", no_writes(&server));
}

#[test]
fn a_node_privilege_is_missing_unless_the_root_pair_is_held_and_a_zero_value_counts_as_held() {
    // Arrange: a token with no node privilege, and the root pair Sys.Audit and Sys.Modify; then the same with the
    // node privilege held at value 0.
    let server = Server::start();
    let no_node = Grants {
        node: json!({}),
        root: json!({"Sys.Audit": 1}),
        ..full_grants()
    };
    mount_reads(&server, json!([]), json!([]), &no_node);

    // Act
    let missing = run_declared(&server, two_isos(), true);

    // Assert: red naming the node privilege and its path, with the root pair as the alternative.
    assert!(!missing.ok, "{}", missing.out);
    assert!(
        missing.out.contains("Sys.AccessNetwork on /nodes/n1"),
        "{}",
        missing.out
    );

    // Arrange: the root pair alone passes; a zero value passes.
    for grants in [
        Grants {
            node: json!({}),
            root: json!({"Sys.Audit": 1, "Sys.Modify": 1}),
            ..full_grants()
        },
        Grants {
            storage: json!({"Datastore.AllocateTemplate": 0}),
            node: json!({"Sys.AccessNetwork": 0}),
            ..full_grants()
        },
    ] {
        let server = Server::start();
        mount_reads(&server, json!([]), json!([]), &grants);

        // Act
        let out = run_declared(&server, two_isos(), true);

        // Assert
        assert!(out.ok, "{}", out.out);
    }
}

#[test]
fn a_node_that_is_not_online_refuses_the_run_before_any_download() {
    // Arrange: the declared node answers offline.
    let server = Server::start();
    mount_version_and_nodes(&server, "offline");

    // Act
    let out = run_declared(&server, two_isos(), false);

    // Assert
    assert!(!out.ok, "{}", out.out);
    assert!(
        out.out.contains("is not an online Proxmox node"),
        "{}",
        out.out
    );
    assert!(no_writes(&server).is_empty(), "{:?}", no_writes(&server));
}

#[test]
fn a_storage_status_reply_without_active_or_content_is_refused_with_the_assert_message() {
    // Arrange: the declared storage's status reply carries neither `active` nor `content`. The override takes priority
    // over the status mock that `mount_reads` mounts; the other reads answer as in `mount_reads`.
    let server = Server::start();
    server.mount(
        reply(
            "GET",
            "/api2/json/nodes/n1/storage/s1/status",
            200,
            json!({"data": {}}),
        )
        .with_priority(1),
    );
    mount_reads(&server, json!([]), json!([]), &full_grants());

    // Act
    let out = run_declared(&server, two_isos(), false);

    // Assert: refused with the storage assert's message rather than a template error, and no write was sent.
    assert!(!out.ok, "{}", out.out);
    assert!(
        out.out
            .contains("is not active, or does not accept ISO content"),
        "{}",
        out.out
    );
    assert!(no_writes(&server).is_empty(), "{:?}", no_writes(&server));
}

#[test]
fn the_https_assert_refuses_a_plain_http_api_url() {
    // Arrange: the play's own HTTPS assert, alone, with the API URL as an inventory var.
    let assert_https = task(&fetch_play(), "Require Proxmox API over HTTPS");

    // Act
    let plain = run(
        single(vec![assert_https.clone()]),
        json!({"_pve_host": "http://127.0.0.1:9"}),
        None,
        false,
    );
    let secure = run(
        single(vec![assert_https]),
        json!({"_pve_host": "https://pve.example.invalid:8006"}),
        None,
        false,
    );

    // Assert
    assert!(!plain.ok, "{}", plain.out);
    assert!(plain.out.contains("must use HTTPS"), "{}", plain.out);
    assert!(secure.ok, "{}", secure.out);
}

#[test]
fn the_play_imports_the_extra_var_guard_then_the_reviewed_checkout_pinning_its_files() {
    // Arrange: the playbook's top-level entries.
    let items = playbook::loads(&text::read(PLAYBOOK));
    let items = items.as_array().expect("a playbook is a list");

    // Assert: the extra-var guard comes first, then the reviewed-checkout gate, which pins every file the run executes.
    assert_eq!(
        items[0]["ansible.builtin.import_playbook"],
        "refuse-internal-extra-vars.yml"
    );
    assert_eq!(
        items[1]["ansible.builtin.import_playbook"],
        "require-reviewed-checkout.yml"
    );
    let pinned: Vec<&str> = items[1]["vars"]["_reviewed_checkout_payload_paths"]
        .as_array()
        .expect("payload paths")
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    for file in [PLAYBOOK, TASKS, "platform/semaphore/templates.yml"] {
        assert!(pinned.contains(&file), "{file} is not pinned: {pinned:?}");
    }
}

#[test]
fn no_task_includes_the_restore_target_guard() {
    // Assert: the fetch writes ISO storage only and names no guest, so no restore-target guard is included.
    for rel in [PLAYBOOK, TASKS] {
        text::refute_text(rel, "guard-o11y-restore-target.yml");
    }
}
