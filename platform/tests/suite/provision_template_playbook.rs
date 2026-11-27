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

use agent_cloud_tests::http::{Mock, Request, Server, matchers};
use agent_cloud_tests::{playbook, process, repo, seed, text};
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
const WIN_SEED_FILE: &str = "windows-seed-9100.iso";

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

/// A fake `genisoimage` for the Ubuntu seed builder, which names each input file: writes each
/// file operand, after a `==> <name>` line, to the path after `-output`.
const FAKE_GENISOIMAGE_FILES: &str = r#"out=""; files=()
while [ $# -gt 0 ]; do
  case "$1" in
    -output) out="$2"; shift 2 ;;
    -volid) shift 2 ;;
    -*) shift ;;
    *) files+=("$1"); shift ;;
  esac
done
: > "$out"
for f in "${files[@]}"; do
  printf '==> %s\n' "$(basename "$f")" >> "$out"
  cat "$f" >> "$out"
done
"#;

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

/// The template play: the one with `tasks` (the imported plays have none).
fn template_play() -> Value {
    playbook::plays(PLAYBOOK)
        .into_iter()
        .find(|p| p.get("tasks").is_some())
        .expect("the playbook has a play with tasks")
}

/// The template play, lifted. No task reads a gathered fact, so facts are not gathered; the
/// Ubuntu golden tests would see any request this changed.
fn lifted_play() -> Value {
    let mut play = template_play();
    play["gather_facts"] = json!(false);
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
        .respond_with(seed::reply(status, &body))
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
    let created = if create_status == 200 {
        json!({"data": "UPID:fake"})
    } else {
        json!(null)
    };
    server.mount(reply("POST", &api("qemu"), create_status, created));
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
            .respond_with(seed::reply(200, &json!({"data": null}))),
    );
    server.mount(reply(
        "GET",
        "/api2/json/cluster/resources",
        200,
        json!({"data": []}),
    ));
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

/// Runs the Ubuntu build and compares every request with GOLDEN, in order.
fn assert_ubuntu_golden(template_os: Option<&str>) {
    let dir = TempDir::new().unwrap();
    let project = dir.path().join("project");
    let seed_dir = project.join("proxmox/cloud-init");
    std::fs::create_dir_all(&seed_dir).unwrap();
    // The repository's own seed builder and its inputs run, with only genisoimage faked.
    for name in [
        "build-seed-iso.sh",
        "autoinstall-user-data.yml",
        "meta-data",
    ] {
        let dest = seed_dir.join(name);
        std::fs::copy(
            repo::path(&format!("platform/hypervisor/proxmox/cloud-init/{name}")),
            &dest,
        )
        .unwrap();
    }
    let path = process::fake_bin(dir.path(), &[("genisoimage", FAKE_GENISOIMAGE_FILES)]);
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
    let run = run_play(dir.path(), &server, &extra, &[("PATH", path)]);
    assert!(run.ok, "{}", run.output);
    let got: Vec<(String, String, String)> = server
        .requests()
        .iter()
        .map(|r| {
            let (method, path) = seed::record(r);
            (method, path, shown_body(r))
        })
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
fn a_malformed_node_or_vmid_sends_nothing() {
    // Both reach request paths, and the survey passes them; each OS refuses them before any
    // request. A Windows build has no default vmid, so one without a vmid sends nothing either.
    // Each row must be refused by its own check, not by a later request failing.
    const MALFORMED: &str = "target_node must be a node name and template_vmid a positive integer.";
    const NO_VMID: &str = "A Windows template needs -e template_vmid=<n>";
    let cases: [(&str, Vec<String>, &str); 7] = [
        (
            "ubuntu node",
            vec!["target_node=n1/../../cluster".to_owned()],
            MALFORMED,
        ),
        (
            "ubuntu vmid",
            vec!["template_vmid=9000/status".to_owned()],
            MALFORMED,
        ),
        (
            "ubuntu vmid with a trailing newline",
            vec![json!({"template_vmid": "9000\n"}).to_string()],
            MALFORMED,
        ),
        (
            "windows node",
            {
                let mut e = windows_extra(Some("9100"));
                e.push("target_node=n1?x=1".to_owned());
                e
            },
            MALFORMED,
        ),
        ("windows vmid", windows_extra(Some("0")), MALFORMED),
        ("windows without a vmid", windows_extra(None), NO_VMID),
        (
            // An integer 0 falls back to 9000 through `default(9000, true)`, so the Windows
            // require check must refuse it, not the malformed check.
            "windows integer zero vmid",
            {
                let mut e = windows_extra(None);
                e.push(json!({"template_vmid": 0}).to_string());
                e
            },
            NO_VMID,
        ),
    ];
    let mut problems = vec![];
    for (case, extra, refusal) in cases {
        let dir = TempDir::new().unwrap();
        let server = Server::start();
        let run = run_play(dir.path(), &server, &extra, &[]);
        let sent = sent(&server);
        let refused = run.output.contains(refusal);
        if run.ok || !sent.is_empty() || !refused {
            problems.push(format!(
                "{case}: passed={}, refused by its check={refused}, requests={sent:?}\n{}",
                run.ok, run.output
            ));
        }
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

/// A Windows build at WIN_VMID whose create answers `create_status`, with a fake `genisoimage`.
/// The seed upload answers `{"data": <upload_upid>}` (null when `None`), and that task's status is
/// `stopped`. The upload runs the real curl, so a missing curl fails the test.
fn windows_build(dir: &Path, create_status: u16, upload_upid: Option<&str>) -> (Server, Run) {
    windows_build_with(dir, create_status, upload_upid, vec![], &[])
}

/// [`windows_build`] with `overrides` mounted first (a caller gives each a priority above the
/// build's defaults) and `more` extra vars after the usual ones.
fn windows_build_with(
    dir: &Path,
    create_status: u16,
    upload_upid: Option<&str>,
    overrides: Vec<Mock>,
    more: &[String],
) -> (Server, Run) {
    let curl_found = std::env::var_os("PATH")
        .is_some_and(|path| std::env::split_paths(&path).any(|d| d.join("curl").is_file()));
    assert!(
        curl_found,
        "curl is not on PATH: the Windows seed upload runs curl"
    );
    let server = Server::start();
    for mock in overrides {
        server.mount(mock);
    }
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
    extra.extend(more.iter().cloned());
    // The Windows runs pass ssh_public_key, so the store branch is not exercised: the BAO
    // sentinels prove only that the environment values do not leak.
    let env = [
        ("PATH", path),
        ("BAO_ROLE_ID", "SENTINEL-ROLE".into()),
        ("BAO_SECRET_ID", "SENTINEL-SECRET-ID".into()),
    ];
    let run = run_play(dir, &server, &extra, &env);
    (server, run)
}

/// Whether `req` is a DELETE of the Windows seed on storage `local`.
fn is_seed_delete(req: &Request) -> bool {
    req.method.as_str() == "DELETE"
        && req.url.path().starts_with(&api("storage/local/content/"))
        && req.url.path().ends_with(WIN_SEED_FILE)
}

fn is_seed_upload(req: &Request) -> bool {
    req.method.as_str() == "POST" && req.url.path() == api("storage/local/upload")
}

/// The seed DELETE is sent after the upload, and a storage read-back comes after it.
fn assert_seed_deleted(server: &Server, output: &str) {
    let reqs = server.requests();
    let sent = sent(server);
    let upload = reqs.iter().position(is_seed_upload);
    let Some(delete) = reqs.iter().rposition(is_seed_delete) else {
        panic!("no DELETE of {WIN_SEED_VOLID}: {sent:?}\n{output}");
    };
    assert!(
        upload.is_some_and(|u| u < delete),
        "no DELETE of {WIN_SEED_VOLID} after the upload: {sent:?}\n{output}"
    );
    let read_back = reqs[delete + 1..]
        .iter()
        .any(|r| r.method.as_str() == "GET" && r.url.path() == api("storage/local/content"));
    assert!(
        read_back,
        "no storage read-back after the DELETE: {sent:?}\n{output}"
    );
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
    let uploads: Vec<&Request> = requests.iter().filter(|r| is_seed_upload(r)).collect();
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
    let logon = text::read(&format!("{win}/first-logon.ps1"));
    let mut problems = vec![];
    for name in template_names(&answer) {
        if !["_win_admin_password", "_win_image_index"].contains(&name.as_str()) {
            problems.push(format!("autounattend.xml.j2 renders `{name}`"));
        }
    }
    // The script is copied, not rendered: it must hold no Jinja.
    if logon.contains("{{") || logon.contains("{%") {
        problems.push("first-logon.ps1 holds Jinja".to_owned());
    }
    if answer.contains("{%") {
        problems.push("autounattend.xml.j2 has a `{%` statement".to_owned());
    }
    let play = template_play();
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
    let key_writers: Vec<&Value> = tasks
        .iter()
        .filter(|t| {
            t["ansible.builtin.copy"]["dest"]
                .as_str()
                .is_some_and(|d| d.ends_with("/management.pub"))
        })
        .collect();
    if key_writers.len() != 1 {
        problems.push(format!("{} tasks write management.pub", key_writers.len()));
    }
    for t in &key_writers {
        let content = t["ansible.builtin.copy"]["content"]
            .as_str()
            .unwrap_or_default();
        if template_names(content) != ["_win_ssh_pub"] {
            problems.push(format!("management.pub is written from `{content}`"));
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

#[test]
fn first_logon_opens_ssh_on_every_profile_and_drops_host_keys_before_sysprep() {
    let logon = text::read("platform/hypervisor/proxmox/windows/first-logon.ps1");
    let lines: Vec<&str> = logon.lines().collect();
    let mut problems = vec![];
    let rule_lines: Vec<&&str> = lines
        .iter()
        .filter(|l| l.contains("-Name 'OpenSSH-Server-In-TCP'") && l.contains("NetFirewallRule"))
        .collect();
    for verb in ["New-NetFirewallRule", "Set-NetFirewallRule"] {
        if !rule_lines
            .iter()
            .any(|l| l.contains(verb) && l.contains("-Profile Any"))
        {
            problems.push(format!(
                "no {verb} of OpenSSH-Server-In-TCP with -Profile Any"
            ));
        }
    }
    let remove = lines
        .iter()
        .position(|l| l.trim_start().starts_with("Remove-Item") && l.contains("ssh_host_*"));
    let sysprep = lines.iter().position(|l| l.contains("sysprep.exe"));
    match (remove, sysprep) {
        (Some(r), Some(s)) if r < s => {}
        _ => problems.push(format!(
            "the ssh_host_* Remove-Item (line {remove:?}) is not before sysprep.exe (line {sysprep:?})"
        )),
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

#[test]
fn windows_vmid_held_on_another_node_is_refused_before_any_write() {
    let dir = TempDir::new().unwrap();
    // Over a full build, so a missed refusal goes on to write.
    let elsewhere = reply(
        "GET",
        "/api2/json/cluster/resources",
        200,
        json!({"data": [{"vmid": 9100, "name": "windows-server-2025-template", "node": "othernode"}]}),
    )
    .with_priority(1);
    let (server, run) = windows_build_with(dir.path(), 200, None, vec![elsewhere], &[]);
    assert!(
        !run.ok,
        "a vmid held on another node passed\n{}",
        run.output
    );
    let writes = no_writes(&server);
    assert!(writes.is_empty(), "writes sent: {writes:?}\n{}", run.output);
}

#[test]
fn ubuntu_refuses_a_vmid_holding_a_legacy_windows_ostype() {
    let dir = TempDir::new().unwrap();
    let server = Server::start();
    server.mount(reply(
        "GET",
        &api("qemu/9000/config"),
        200,
        json!({"data": {"template": 1, "ostype": "w2k8"}}),
    ));
    let run = run_play(dir.path(), &server, &[], &[]);
    assert!(
        !run.ok,
        "an Ubuntu run at a w2k8 vmid passed\n{}",
        run.output
    );
    assert!(
        run.output.contains("holds a windows VM"),
        "not refused by the cross-OS check\n{}",
        run.output
    );
    let writes = no_writes(&server);
    assert!(writes.is_empty(), "writes sent: {writes:?}\n{}", run.output);
}

#[test]
fn windows_build_fails_when_the_create_task_fails() {
    let dir = TempDir::new().unwrap();
    let failed_task = reply(
        "GET",
        &api("tasks/UPID:fake/status"),
        200,
        json!({"data": {"status": "stopped", "exitstatus": "unexpected error"}}),
    )
    .with_priority(1);
    let (server, run) = windows_build_with(dir.path(), 200, None, vec![failed_task], &[]);
    assert!(!run.ok, "a failed create task passed\n{}", run.output);
    let template = api(&format!("qemu/{WIN_VMID}/template"));
    let converted = server
        .requests()
        .iter()
        .any(|r| r.method.as_str() == "POST" && r.url.path() == template);
    assert!(
        !converted,
        "the VM was converted after a failed create task: {:?}\n{}",
        sent(&server),
        run.output
    );
    assert_seed_deleted(&server, &run.output);
}

#[test]
fn a_seed_left_by_an_earlier_run_is_deleted_before_the_upload() {
    let dir = TempDir::new().unwrap();
    let leftover = reply(
        "GET",
        &api("storage/local/content"),
        200,
        json!({"data": [{"volid": WIN_SEED_VOLID}]}),
    )
    .up_to_n_times(1)
    .with_priority(1);
    let (server, run) = windows_build_with(dir.path(), 200, None, vec![leftover], &[]);
    assert!(run.ok, "{}", run.output);
    let reqs = server.requests();
    let upload = reqs.iter().position(is_seed_upload);
    let delete = reqs.iter().position(is_seed_delete);
    assert!(
        matches!((delete, upload), (Some(d), Some(u)) if d < u),
        "no DELETE of {WIN_SEED_VOLID} before the upload: {:?}\n{}",
        sent(&server),
        run.output
    );
}

#[test]
fn a_malformed_public_key_is_refused_before_the_seed_is_built() {
    let cases = [
        ("a key with a trailing newline", "ssh-ed25519 AAAAx\n"),
        ("not a key", "not a key"),
    ];
    let mut problems = vec![];
    for (case, key) in cases {
        let dir = TempDir::new().unwrap();
        let key = json!({"ssh_public_key": key}).to_string();
        let (server, run) = windows_build_with(dir.path(), 200, None, vec![], &[key]);
        let uploaded = server.requests().iter().any(is_seed_upload);
        if run.ok || uploaded {
            problems.push(format!(
                "{case}: passed={}, uploaded={uploaded}: {:?}\n{}",
                run.ok,
                sent(&server),
                run.output
            ));
        }
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

#[test]
fn a_malformed_seed_storage_is_refused_before_any_write() {
    let dir = TempDir::new().unwrap();
    let storage = "seed_iso_storage=bad/name".to_owned();
    let (server, run) = windows_build_with(dir.path(), 200, None, vec![], &[storage]);
    assert!(
        !run.ok,
        "a seed storage with a slash passed\n{}",
        run.output
    );
    assert!(
        run.output
            .contains("seed_iso_storage must be a storage name"),
        "not refused by the seed storage check\n{}",
        run.output
    );
    let writes = no_writes(&server);
    assert!(writes.is_empty(), "writes sent: {writes:?}\n{}", run.output);
}
