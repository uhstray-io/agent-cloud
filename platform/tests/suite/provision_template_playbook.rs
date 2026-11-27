//! The template play of `platform/playbooks/provision-template.yml` (playbook tier): the Ubuntu
//! requests are unchanged when `template_os` is absent or `ubuntu`, a Windows build is refused
//! before any write when its vmid or name collides or no vmid is given, the Windows seed ISO is
//! deleted from storage whether the build passes or fails, and the seed carries the management
//! public key and no other store or connection value (AC-1107). One static test reads the answer
//! file inputs.
//!
//! The play is lifted and run against a fake Proxmox API (`http::Server`). The restore-target
//! guard includes become no-ops, and the other relative includes point at the repository. The
//! stub run proves the Jinja and the requests, not privilege: the play has no `become` (L-021).
//! Fixtures send no real credential: wiremock prints every request on a failed expectation.

use std::path::Path;

use agent_cloud_tests::http::{Mock, Request, ResponseTemplate, Server, matchers};
use agent_cloud_tests::{playbook, process, repo, text};
use regex::Regex;
use serde_json::{Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/provision-template.yml";
const GUARD: &str = "tasks/guard-o11y-restore-target.yml";
const NODE: &str = "alphacentauri";
const PVE_SECRET: &str = "SENTINEL-PVE-SECRET";
const PVE_TOKEN_ID: &str = "t@pve!x";
const WIN_KEY: &str = "ssh-ed25519 AAAAsentinel";
const WIN_VMID: &str = "9100";
const WIN_SEED_VOLID: &str = "local:iso/windows-seed-9100.iso";

/// The Ubuntu requests recorded on the playbook before the Windows branch (base `89cca2dc`):
/// method, path with query, body (a multipart upload as `<multipart>`).
const GOLDEN: &[(&str, &str, &str)] = &[
    ("GET", "/api2/json/nodes/alphacentauri/qemu/9000/config", ""),
    (
        "GET",
        "/api2/json/nodes/alphacentauri/storage/local/content",
        "",
    ),
    (
        "POST",
        "/api2/json/nodes/alphacentauri/storage/local/upload",
        "<multipart>",
    ),
    (
        "POST",
        "/api2/json/nodes/alphacentauri/qemu",
        "vmid=9000&name=ubuntu-2404-template&ostype=l26&cores=2&memory=2048&scsihw=virtio-scsi-pci&scsi0=vm-lvms%3A20&ide0=SharedISOs%3Aiso%2Fubuntu-24.04.3-live-server-amd64.iso%2Cmedia%3Dcdrom&ide1=local%3Aiso%2Fautoinstall-seed.iso%2Cmedia%3Dcdrom&net0=virtio%2Cbridge%3Dvmbr0%2Cfirewall%3D0&boot=order%3Dide0%3Bscsi0&agent=1&serial0=socket&vga=serial0",
    ),
    (
        "GET",
        "/api2/json/nodes/alphacentauri/tasks/UPID:fake/status",
        "",
    ),
    (
        "POST",
        "/api2/json/nodes/alphacentauri/qemu/9000/status/start",
        "",
    ),
    (
        "GET",
        "/api2/json/nodes/alphacentauri/qemu/9000/status/current",
        "",
    ),
    (
        "GET",
        "/api2/json/nodes/alphacentauri/qemu/9000/status/current",
        "",
    ),
    (
        "PUT",
        "/api2/json/nodes/alphacentauri/qemu/9000/config",
        "ide0=none%2Cmedia%3Dcdrom&ide1=none%2Cmedia%3Dcdrom&ide2=vm-lvms%3Acloudinit&boot=order%3Dscsi0&ciuser=ops&sshkeys=ssh-ed25519%2520AAAAgolden&ipconfig0=ip%3Ddhcp&nameserver=",
    ),
    (
        "POST",
        "/api2/json/nodes/alphacentauri/qemu/9000/template",
        "",
    ),
    ("GET", "/api2/json/nodes/alphacentauri/qemu/9000/config", ""),
];

/// A fake `genisoimage`: writes every file of its source directory, each after a `==> <name>`
/// line, to the path after `-output`, so the uploaded seed is plain text.
const FAKE_GENISOIMAGE: &str = r#"out=""; src=""
while [ $# -gt 0 ]; do
  case "$1" in
    -output) out="$2"; shift 2 ;;
    -volid) shift 2 ;;
    -*) shift ;;
    *) src="$1"; shift ;;
  esac
done
: > "$out"
for f in "$src"/*; do
  printf '==> %s\n' "$(basename "$f")" >> "$out"
  cat "$f" >> "$out"
done
"#;

/// Replaces each restore-target guard include with a no-op of the same name and points the other
/// relative includes at the repository, at every depth.
fn lift_tasks(tasks: &mut Value, pb_dir: &Path) {
    let Some(list) = tasks.as_array_mut() else {
        return;
    };
    for task in list.iter_mut() {
        let include = task
            .get("ansible.builtin.include_tasks")
            .and_then(Value::as_str)
            .map(str::to_owned);
        match include {
            Some(target) if target == GUARD => {
                *task = json!({"name": task["name"].clone(), "ansible.builtin.meta": "noop"});
                continue;
            }
            Some(target) if target.starts_with("tasks/") => {
                task["ansible.builtin.include_tasks"] =
                    json!(pb_dir.join(&target).display().to_string());
            }
            _ => {}
        }
        for key in ["block", "rescue", "always"] {
            if let Some(inner) = task.get_mut(key) {
                lift_tasks(inner, pb_dir);
            }
        }
    }
}

/// The template play (the one with `tasks`), lifted.
fn lifted_play() -> Value {
    let mut play = playbook::plays(PLAYBOOK)
        .into_iter()
        .find(|p| p.get("tasks").is_some())
        .expect("the playbook has a play with tasks");
    let pb_dir = repo::path("platform/playbooks");
    lift_tasks(&mut play["tasks"], &pb_dir);
    play
}

/// A finished run: whether it passed, its stdout, and stdout with stderr.
struct Run {
    ok: bool,
    stdout: String,
    output: String,
}

/// Runs the lifted play against `server` with the operator Proxmox connection, plus `extra`
/// (`-e` values) and `env` (added to the run's environment).
fn run_play(
    dir: &Path,
    server: &Server,
    extra: &[String],
    env: &[(&str, std::ffi::OsString)],
) -> Run {
    let pb = playbook::write(dir, "play.json", &json!([lifted_play()]));
    let inv = playbook::write(
        dir,
        "inv.json",
        &json!({"all": {"hosts": {"localhost": {
            "ansible_connection": "local",
            "ansible_python_interpreter": "{{ ansible_playbook_python }}"}}}}),
    );
    let mut cmd =
        process::ansible_playbook(dir, &inv.display().to_string(), &pb.display().to_string());
    let win_dir = repo::path("platform/hypervisor/proxmox/windows");
    cmd.arg("-e")
        .arg(format!("_win_dir={}", win_dir.display()))
        .arg("-e")
        .arg(format!("pve_host={}", server.url()))
        .arg("-e")
        .arg(format!("pve_token_id={PVE_TOKEN_ID}"))
        .arg("-e")
        .arg(format!("pve_token_secret={PVE_SECRET}"));
    for value in extra {
        cmd.arg("-e").arg(value);
    }
    for (name, value) in env {
        cmd.env(name, value);
    }
    let out = process::run(&mut cmd);
    Run {
        ok: out.status.success(),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        output: process::output_text(&out),
    }
}

fn reply(method: &str, path: &str, status: u16, body: Value) -> Mock {
    Mock::given(matchers::method(method))
        .and(matchers::path(path))
        .respond_with(ResponseTemplate::new(status).set_body_json(body))
}

fn api(rest: &str) -> String {
    format!("/api2/json/nodes/{NODE}/{rest}")
}

/// Mounts a whole build at `vmid`: the config is 404 until the template conversion (then a
/// template with a cloud-init drive), the storage holds nothing, and every write succeeds, except
/// the create, which answers `create_status`.
fn mount_build(server: &Server, vmid: &str, create_status: u16) {
    let config = api(&format!("qemu/{vmid}/config"));
    server.mount(
        reply("GET", &config, 404, json!({"data": null}))
            .up_to_n_times(1)
            .with_priority(1),
    );
    server.mount(reply(
        "GET",
        &config,
        200,
        json!({"data": {"template": 1, "ide2": format!("vm-lvms:vm-{vmid}-cloudinit")}}),
    ));
    server.mount(reply(
        "GET",
        &api("storage/local/content"),
        200,
        json!({"data": []}),
    ));
    server.mount(reply(
        "POST",
        &api("storage/local/upload"),
        200,
        json!({"data": null}),
    ));
    let create = if create_status == 200 {
        reply("POST", &api("qemu"), 200, json!({"data": "UPID:fake"}))
    } else {
        Mock::given(matchers::method("POST"))
            .and(matchers::path(api("qemu")))
            .respond_with(ResponseTemplate::new(create_status))
    };
    server.mount(create);
    server.mount(reply(
        "GET",
        &api("tasks/UPID:fake/status"),
        200,
        json!({"data": {"status": "stopped"}}),
    ));
    server.mount(reply(
        "POST",
        &api(&format!("qemu/{vmid}/status/start")),
        200,
        json!({"data": null}),
    ));
    let current = api(&format!("qemu/{vmid}/status/current"));
    server.mount(
        reply("GET", &current, 200, json!({"data": {"status": "running"}}))
            .up_to_n_times(1)
            .with_priority(1),
    );
    server.mount(reply(
        "GET",
        &current,
        200,
        json!({"data": {"status": "stopped"}}),
    ));
    server.mount(reply("PUT", &config, 200, json!({"data": null})));
    server.mount(reply(
        "POST",
        &api(&format!("qemu/{vmid}/template")),
        200,
        json!({"data": null}),
    ));
    server.mount(
        Mock::given(matchers::method("DELETE"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"data": null}))),
    );
    server.mount(reply(
        "GET",
        "/api2/json/cluster/resources",
        200,
        json!({"data": []}),
    ));
}

fn path_and_query(req: &Request) -> String {
    match req.url.query() {
        Some(q) => format!("{}?{q}", req.url.path()),
        None => req.url.path().to_owned(),
    }
}

fn shown_body(req: &Request) -> String {
    let multipart = req
        .headers
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("multipart/"));
    if multipart {
        "<multipart>".to_owned()
    } else {
        String::from_utf8_lossy(&req.body).into_owned()
    }
}

fn is_write(req: &Request) -> bool {
    ["POST", "PUT", "DELETE"].contains(&req.method.as_str())
}

/// Runs the Ubuntu build and compares every request with GOLDEN, in order.
fn assert_ubuntu_golden(template_os: Option<&str>) {
    let dir = TempDir::new().unwrap();
    let project = dir.path().join("project");
    let seed_dir = project.join("proxmox/cloud-init");
    std::fs::create_dir_all(&seed_dir).unwrap();
    std::fs::write(
        seed_dir.join("build-seed-iso.sh"),
        "#!/usr/bin/env bash\nprintf stub > \"$1\"\n",
    )
    .unwrap();
    let server = Server::start();
    mount_build(&server, "9000", 200);
    let mut extra = vec![
        format!("project_root={}", project.display()),
        "ssh_public_key='ssh-ed25519 AAAAgolden'".to_owned(),
        "ansible_user=ops".to_owned(),
    ];
    if let Some(os) = template_os {
        extra.push(format!("template_os={os}"));
    }
    let run = run_play(dir.path(), &server, &extra, &[]);
    assert!(run.ok, "{}", run.output);
    let got: Vec<(String, String, String)> = server
        .requests()
        .iter()
        .map(|r| (r.method.to_string(), path_and_query(r), shown_body(r)))
        .collect();
    let want: Vec<(String, String, String)> = GOLDEN
        .iter()
        .map(|(m, p, b)| ((*m).to_owned(), (*p).to_owned(), (*b).to_owned()))
        .collect();
    assert_eq!(got, want, "the Ubuntu requests changed\n{}", run.output);
}

#[test]
fn ubuntu_requests_are_unchanged_without_template_os() {
    assert_ubuntu_golden(None);
}

#[test]
fn ubuntu_requests_are_unchanged_with_template_os_ubuntu() {
    assert_ubuntu_golden(Some("ubuntu"));
}

/// The extra vars of a Windows build at `vmid` (none when `vmid` is `None`).
fn windows_extra(vmid: Option<&str>) -> Vec<String> {
    let mut extra = vec![
        "template_os=windows".to_owned(),
        format!("target_node={NODE}"),
        "windows_iso=SharedISOs:iso/windows-server-2025.iso".to_owned(),
        "virtio_iso=SharedISOs:iso/virtio-win.iso".to_owned(),
        format!("ssh_public_key='{WIN_KEY}'"),
    ];
    if let Some(vmid) = vmid {
        extra.push(format!("template_vmid={vmid}"));
    }
    extra
}

fn no_writes(server: &Server) -> Vec<String> {
    server
        .requests()
        .iter()
        .filter(|r| is_write(r))
        .map(|r| format!("{} {}", r.method, path_and_query(r)))
        .collect()
}

#[test]
fn windows_aimed_at_a_linux_vmid_is_refused_before_any_write() {
    let dir = TempDir::new().unwrap();
    let server = Server::start();
    server.mount(reply(
        "GET",
        &api("qemu/9000/config"),
        200,
        json!({"data": {"template": 1, "ostype": "l26", "name": "ubuntu-2404-template"}}),
    ));
    server.mount(reply(
        "GET",
        "/api2/json/cluster/resources",
        200,
        json!({"data": []}),
    ));
    let run = run_play(dir.path(), &server, &windows_extra(Some("9000")), &[]);
    assert!(
        !run.ok,
        "a Windows build at a Linux vmid passed\n{}",
        run.output
    );
    let writes = no_writes(&server);
    assert!(writes.is_empty(), "writes sent: {writes:?}\n{}", run.output);
    assert!(
        run.output.contains("holds a linux VM"),
        "not refused by the cross-OS check\n{}",
        run.output
    );
}

#[test]
fn windows_reusing_the_template_name_at_another_vmid_is_refused_before_any_write() {
    let dir = TempDir::new().unwrap();
    let server = Server::start();
    server.mount(reply(
        "GET",
        &api(&format!("qemu/{WIN_VMID}/config")),
        404,
        json!({"data": null}),
    ));
    server.mount(reply(
        "GET",
        "/api2/json/cluster/resources",
        200,
        json!({"data": [{"vmid": 9200, "name": "windows-server-2025-template", "node": NODE, "type": "qemu"}]}),
    ));
    let run = run_play(dir.path(), &server, &windows_extra(Some(WIN_VMID)), &[]);
    assert!(!run.ok, "a reused template name passed\n{}", run.output);
    let writes = no_writes(&server);
    assert!(writes.is_empty(), "writes sent: {writes:?}\n{}", run.output);
}

#[test]
fn windows_without_a_template_vmid_sends_nothing() {
    let dir = TempDir::new().unwrap();
    let server = Server::start();
    mount_build(&server, "9000", 200);
    let run = run_play(dir.path(), &server, &windows_extra(None), &[]);
    assert!(
        !run.ok,
        "a Windows build without a vmid passed\n{}",
        run.output
    );
    let sent: Vec<String> = server
        .requests()
        .iter()
        .map(|r| format!("{} {}", r.method, path_and_query(r)))
        .collect();
    assert!(sent.is_empty(), "requests sent: {sent:?}\n{}", run.output);
}

/// A Windows build at WIN_VMID whose create answers `create_status`, with a fake `genisoimage`.
/// The seed upload answers `{"data": <upload_upid>}` (null when `None`), and that task's status is
/// `stopped`. The upload runs the real curl, so a missing curl fails the test.
fn windows_build(dir: &Path, create_status: u16, upload_upid: Option<&str>) -> (Server, Run) {
    let curl_found = std::env::var_os("PATH")
        .is_some_and(|path| std::env::split_paths(&path).any(|d| d.join("curl").is_file()));
    assert!(
        curl_found,
        "curl is not on PATH: the Windows seed upload runs curl"
    );
    let server = Server::start();
    if let Some(upid) = upload_upid {
        server.mount(
            reply(
                "POST",
                &api("storage/local/upload"),
                200,
                json!({"data": upid}),
            )
            .with_priority(1),
        );
        server.mount(reply(
            "GET",
            &api(&format!("tasks/{upid}/status")),
            200,
            json!({"data": {"status": "stopped"}}),
        ));
    }
    mount_build(&server, WIN_VMID, create_status);
    let path = process::fake_bin(dir, &[("genisoimage", FAKE_GENISOIMAGE)]);
    let mut extra = windows_extra(Some(WIN_VMID));
    extra.push("BAO_ROLE_ID=SENTINEL-ROLE".to_owned());
    extra.push("BAO_SECRET_ID=SENTINEL-SECRET-ID".to_owned());
    let env = [
        ("PATH", path),
        ("BAO_ROLE_ID", "SENTINEL-ROLE".into()),
        ("BAO_SECRET_ID", "SENTINEL-SECRET-ID".into()),
    ];
    let run = run_play(dir, &server, &extra, &env);
    (server, run)
}

/// The seed DELETE (its volid decoded) is sent, and a storage read-back comes after it.
fn assert_seed_deleted(server: &Server, output: &str) {
    let reqs = server.requests();
    let prefix = api("storage/local/content/");
    let delete = reqs.iter().position(|r| {
        r.method.as_str() == "DELETE"
            && r.url.path().starts_with(&prefix)
            && urlencoding_decode(&r.url.path()[prefix.len()..]) == WIN_SEED_VOLID
    });
    let sent: Vec<String> = reqs
        .iter()
        .map(|r| format!("{} {}", r.method, path_and_query(r)))
        .collect();
    let Some(delete) = delete else {
        panic!("no DELETE of {WIN_SEED_VOLID}: {sent:?}\n{output}");
    };
    let read_back = reqs[delete + 1..]
        .iter()
        .any(|r| r.method.as_str() == "GET" && r.url.path() == api("storage/local/content"));
    assert!(
        read_back,
        "no storage read-back after the DELETE: {sent:?}\n{output}"
    );
}

/// Percent-decoding of a path segment (`%3A` to `:`), enough for a volid.
fn urlencoding_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && let Ok(b) = u8::from_str_radix(&text[i + 1..i + 3], 16)
        {
            out.push(b);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[test]
fn windows_seed_is_deleted_after_a_successful_build() {
    let dir = TempDir::new().unwrap();
    let (server, run) = windows_build(dir.path(), 200, Some("UPID:seed"));
    assert!(run.ok, "{}", run.output);
    assert_seed_deleted(&server, &run.output);
    let waited = server
        .requests()
        .iter()
        .any(|r| r.method.as_str() == "GET" && r.url.path() == api("tasks/UPID:seed/status"));
    assert!(
        waited,
        "no wait on the seed upload task UPID:seed\n{}",
        run.output
    );
}

#[test]
fn windows_seed_is_deleted_after_a_failed_build() {
    let dir = TempDir::new().unwrap();
    let (server, run) = windows_build(dir.path(), 500, None);
    assert!(!run.ok, "a failed create passed\n{}", run.output);
    assert_seed_deleted(&server, &run.output);
    let status = Regex::new(r#""step":\s*"vm-template"[^}]*?"status":\s*"(\w+)"|"status":\s*"(\w+)"[^}]*?"step":\s*"vm-template""#).unwrap();
    let found: Vec<String> = status
        .captures_iter(&run.output)
        .filter_map(|c| c.get(1).or(c.get(2)).map(|m| m.as_str().to_owned()))
        .collect();
    assert!(
        !found.is_empty() && found.iter().all(|s| s == "fail"),
        "step result status {found:?}, expected fail\n{}",
        run.output
    );
}

#[test]
fn windows_seed_carries_the_public_key_and_no_other_secret() {
    let dir = TempDir::new().unwrap();
    let (server, run) = windows_build(dir.path(), 200, None);
    assert!(run.ok, "{}", run.output);
    let requests = server.requests();
    let uploads: Vec<&Request> = requests
        .iter()
        .filter(|r| r.method.as_str() == "POST" && r.url.path() == api("storage/local/upload"))
        .collect();
    assert_eq!(uploads.len(), 1, "one seed upload\n{}", run.output);
    let auth = uploads[0]
        .headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    assert_eq!(
        auth,
        format!("PVEAPIToken={PVE_TOKEN_ID}={PVE_SECRET}"),
        "the upload's Authorization header (curl reads it on stdin)"
    );
    // curl sends the seed's bytes as they are, so the raw body holds the seed files.
    let raw = String::from_utf8_lossy(&uploads[0].body).into_owned();
    let body = &raw;
    assert!(body.contains(WIN_KEY), "the seed lacks the public key");
    for secret in [
        PVE_SECRET,
        PVE_TOKEN_ID,
        "SENTINEL-ROLE",
        "SENTINEL-SECRET-ID",
    ] {
        assert!(!body.contains(secret), "the upload body carries {secret}");
    }
    let password = Regex::new(r"<AdministratorPassword>\s*<Value>([^<]*)</Value>").unwrap();
    let value = password
        .captures(body)
        .map(|c| c[1].to_owned())
        .expect("the seed has an AdministratorPassword value");
    assert!(
        value.len() == 32 && value.chars().all(|c| c.is_ascii_alphanumeric()),
        "the password is not 32 letters or digits (length {})",
        value.len()
    );
    assert!(
        !run.stdout.contains(&value),
        "the run's stdout shows the one-time password"
    );
}

/// Every task under `tasks`, at every depth.
fn all_tasks(tasks: &Value, out: &mut Vec<Value>) {
    for task in tasks.as_array().into_iter().flatten() {
        out.push(task.clone());
        for key in ["block", "rescue", "always"] {
            if let Some(inner) = task.get(key) {
                all_tasks(inner, out);
            }
        }
    }
}

/// The names each `{{ ... }}` of `src` holds.
fn template_names(src: &str) -> Vec<String> {
    let expr = Regex::new(r"\{\{\s*(.*?)\s*\}\}").unwrap();
    expr.captures_iter(src).map(|c| c[1].to_owned()).collect()
}

#[test]
fn answer_file_inputs_are_the_one_time_password_image_index_and_public_key() {
    let win = "platform/hypervisor/proxmox/windows";
    let answer = text::read(&format!("{win}/autounattend.xml.j2"));
    let logon = text::read(&format!("{win}/first-logon.ps1.j2"));
    let mut problems = vec![];
    for name in template_names(&answer) {
        if !["_win_admin_password", "_win_image_index"].contains(&name.as_str()) {
            problems.push(format!("autounattend.xml.j2 renders `{name}`"));
        }
    }
    for name in template_names(&logon) {
        if name != "_win_ssh_pub" {
            problems.push(format!("first-logon.ps1.j2 renders `{name}`"));
        }
    }
    for (file, src) in [
        ("autounattend.xml.j2", &answer),
        ("first-logon.ps1.j2", &logon),
    ] {
        if src.contains("{%") {
            problems.push(format!("{file} has a `{{%` statement"));
        }
    }
    let play = playbook::plays(PLAYBOOK)
        .into_iter()
        .find(|p| p.get("tasks").is_some())
        .unwrap();
    let mut tasks = vec![];
    all_tasks(&play["tasks"], &mut tasks);
    let setters: Vec<&Value> = tasks
        .iter()
        .filter(|t| {
            t["ansible.builtin.set_fact"]
                .get("_win_admin_password")
                .is_some()
        })
        .collect();
    if setters.len() != 1 {
        problems.push(format!("{} tasks set _win_admin_password", setters.len()));
    }
    for t in &setters {
        let value = t["ansible.builtin.set_fact"]["_win_admin_password"]
            .as_str()
            .unwrap_or_default();
        if !(value.contains("lookup('ansible.builtin.password', '/dev/null'")) {
            problems.push(format!(
                "_win_admin_password is not a /dev/null password lookup: {value}"
            ));
        }
        if t["no_log"] != json!(true) {
            problems.push("the _win_admin_password task is not no_log".to_owned());
        }
    }
    let pub_var = play["vars"]["_win_ssh_pub"].as_str().unwrap_or_default();
    let lookup = Regex::new(r"hashi_vault'\s*,\s*'([^']*)'").unwrap();
    let paths: Vec<String> = lookup
        .captures_iter(pub_var)
        .map(|c| c[1].to_owned())
        .collect();
    if pub_var.matches("hashi_vault'").count() != paths.len() {
        problems.push(format!(
            "_win_ssh_pub has a hashi_vault lookup this test cannot read: {pub_var}"
        ));
    }
    for path in &paths {
        if !path.ends_with(":public_key") {
            problems.push(format!(
                "_win_ssh_pub reads `{path}`, not a :public_key field"
            ));
        }
    }
    let render = tasks.iter().filter(|t| {
        t["ansible.builtin.template"]["src"]
            .as_str()
            .is_some_and(|s| s.ends_with("autounattend.xml.j2"))
    });
    let upload: Vec<&Value> = tasks
        .iter()
        .filter(|t| t["name"] == "Upload the Windows seed ISO")
        .collect();
    if upload.len() != 1 {
        problems.push(format!("{} seed upload tasks, expected 1", upload.len()));
    }
    for t in &upload {
        // The token reaches curl on stdin (`--header @-`), never in argv.
        let argv: Vec<&str> = t["ansible.builtin.command"]["argv"]
            .as_array()
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        if argv.is_empty() {
            problems.push("the seed upload is not an ansible.builtin.command argv".to_owned());
        }
        for arg in &argv {
            if arg.contains("_pve_secret") || arg.contains("_pve_token_id") {
                problems.push(format!("the seed upload argv carries the token: `{arg}`"));
            }
        }
        let header_from_stdin = argv.windows(2).any(|w| w[0] == "--header" && w[1] == "@-");
        if !header_from_stdin {
            problems.push("the seed upload argv has no `--header @-`".to_owned());
        }
    }
    let guarded: Vec<&Value> = render.chain(upload).collect();
    if guarded.len() != 2 {
        problems.push(format!(
            "{} answer-file render and seed upload tasks, expected 2",
            guarded.len()
        ));
    }
    for t in guarded {
        if t["no_log"] != json!(true) {
            problems.push(format!("`{}` is not no_log", t["name"]));
        }
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}
