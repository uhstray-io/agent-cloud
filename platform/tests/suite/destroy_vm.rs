//! A destructive VM template refuses by default and acts only on the VM its declaration names
//! (static tier).
//!
//! destroy-vm.yml is inventory-first, like provision-vm.yml. The live VM must match the declaration
//! on vmid AND name AND node, or nothing happens, and the launch must name the vmid it is about to
//! destroy. The template gives `confirm_destroy` no default, so a template launch cannot destroy by
//! accident. Provision-vm.yml refuses a declared address that already answers on the network, so a
//! new VM cannot be configured onto a live host's address (docs/MISTAKES.md 3.6). That ledger entry
//! is why this module guards both files.
//!
//! Replaces platform/tests/test_destroy_vm.bats (plan 24, Phase 4).
//!
//! ## Port map
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | destroy-vm: inventory-first, refuses a vmid mismatch, name mismatch and node mismatch | `destroy_vm_imports_the_target_group_preflight`, `destroy_vm_refuses_a_live_vm_that_is_not_the_declared_one`, `destroy_vm_target_host_must_name_a_host_of_the_service_group`, `destroy_vm_checks_both_token_endpoints_for_cleartext` |
//! | destroy-vm: the launch must name the vmid (confirm_destroy), with no default | `destroy_vm_refuses_a_launch_that_does_not_name_the_vmid`, `the_destroy_vm_template_gives_confirm_destroy_no_default` |
//! | destroy-vm: stops, deletes with purge, sweeps active image storages for leftovers, verifies task and absence | `destroy_vm_stops_the_vm_before_deleting_it`, `destroy_vm_deletes_with_purge_by_its_exact_url`, `destroy_vm_frees_leftovers_only_on_active_image_storages`, `destroy_vm_never_hands_the_unreferenced_disk_scan_to_proxmox`, `destroy_vm_verifies_the_destroy_and_the_vmid_absence`, `destroy_vm_never_reports_a_uri_call_as_changed` |
//! | provision-vm: refuses a declared address that already answers on the network (MISTAKES 3.6) | `provision_vm_stops_on_an_answering_or_unprobeable_declared_address`, `provision_vm_probes_after_the_existence_check_and_before_the_clone` |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

const DESTROY: &str = "platform/playbooks/destroy-vm.yml";
const PROVISION: &str = "platform/playbooks/provision-vm.yml";
const TEMPLATES: &str = "platform/semaphore/templates.yml";
const GUARD_INCLUDE: &str = "tasks/assert-bao-transport.yml";
const PREFLIGHT: &str = "preflight-target-group.yml";
const PVE_HOST: &str = "{{ _pve_host }}";
const BAO_URL: &str = "{{ _bao_url }}";
const PURGE_URL: &str = "{{ _pve_host }}/api2/json/nodes/{{ _node }}/qemu/{{ _vmid }}?purge=1";
const SWEEP_URL: &str = "{{ _pve_host }}/api2/json/nodes/{{ _node }}/storage/{{ (item | split(':'))[0] }}/content/{{ item | urlencode }}";

/// A task's module under any of the three spellings Ansible accepts for a module key: the short
/// name, `ansible.builtin.<short>`, or `ansible.legacy.<short>` (ansible/utils/fqcn.py). A check
/// that reads one spelling alone is blind to a task written under another, so every module read
/// goes through here.
fn module<'a>(task: &'a Node, short: &str) -> Option<&'a Node> {
    [
        short.to_owned(),
        format!("ansible.builtin.{short}"),
        format!("ansible.legacy.{short}"),
    ]
    .iter()
    .find_map(|key| task.get(key))
}

fn task_name(task: &Node) -> &str {
    task.get("name").and_then(Node::as_str).unwrap_or("")
}

fn named<'a>(tasks: &'a [Node], name: &str) -> &'a Node {
    tasks
        .iter()
        .find(|t| task_name(t) == name)
        .unwrap_or_else(|| panic!("no task named {name:?}"))
}

fn starting<'a>(tasks: &'a [Node], prefix: &str) -> &'a Node {
    tasks
        .iter()
        .find(|t| task_name(t).starts_with(prefix))
        .unwrap_or_else(|| panic!("no task whose name starts with {prefix:?}"))
}

/// The `that:` expressions of an assert, whether written as one scalar or a list.
fn that_items(task: &Node) -> Vec<&str> {
    match module(task, "assert").and_then(|a| a.get("that")) {
        Some(Node::Str(s)) => vec![s.as_str()],
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        _ => vec![],
    }
}

/// The file a task includes, under either spelling of the keyword.
fn include_target(task: &Node) -> Option<&str> {
    module(task, "include_tasks").and_then(Node::as_str)
}

/// The playbook a task imports.
fn import_target(task: &Node) -> Option<&str> {
    module(task, "import_playbook").and_then(Node::as_str)
}

fn url_of(task: &Node) -> Option<&str> {
    module(task, "uri")?.get("url")?.as_str()
}

/// The HTTP method of a uri task. Other tasks report GET, so filter on `url_of` as well.
fn method_of(task: &Node) -> &str {
    module(task, "uri")
        .and_then(|uri| uri.get("method"))
        .and_then(Node::as_str)
        .unwrap_or("GET")
}

fn module_arg<'a>(task: &'a Node, module_name: &str, arg: &str) -> Option<&'a str> {
    module(task, module_name)?.get(arg)?.as_str()
}

#[test]
fn destroy_vm_imports_the_target_group_preflight() {
    // Arrange
    let tasks = repo::tasks(DESTROY);

    // Act
    let imports = tasks.iter().any(|t| import_target(t) == Some(PREFLIGHT));

    // Assert
    assert!(imports, "{DESTROY} must import {PREFLIGHT} before it acts");
}

#[test]
fn destroy_vm_refuses_a_live_vm_that_is_not_the_declared_one() {
    // Arrange
    let tasks = repo::tasks(DESTROY);

    // Act
    let refuse = named(
        &tasks,
        "Refuse to destroy a VM that is not the declared one",
    );
    let items = that_items(refuse);

    // Assert: the vmid's live name and node must equal the declaration.
    assert!(
        items.contains(&"(_live.name | default('')) == _name"),
        "the live VM's name must equal the declared name"
    );
    assert!(
        items.contains(&"(_live.node | default('')) == _node"),
        "the live VM's node must equal the declared node"
    );
}

#[test]
fn destroy_vm_target_host_must_name_a_host_of_the_service_group() {
    // target_host may only name a host of THIS service, so a caller cannot read another service's
    // declaration and destroy that service's VM.
    let tasks = repo::tasks(DESTROY);

    let require = named(
        &tasks,
        "Require an explicit target_host when the service declares more than one",
    );

    assert!(
        that_items(require).contains(&"_decl_host in _group_hosts"),
        "target_host must be one of the service group's hosts"
    );
}

#[test]
fn destroy_vm_checks_both_token_endpoints_for_cleartext() {
    // The AppRole secret_id and the Proxmox token both cross the connection, so both endpoints go
    // through the transport guard, and the Proxmox one carries its label for the message.
    let tasks = repo::tasks(DESTROY);

    let guards: Vec<(&str, Option<&str>)> = tasks
        .iter()
        .filter(|t| include_target(t) == Some(GUARD_INCLUDE))
        .map(|t| {
            let vars = t.get("vars");
            let url = vars
                .and_then(|v| v.get("_assert_bao_url"))
                .and_then(Node::as_str)
                .unwrap_or_default();
            let label = vars
                .and_then(|v| v.get("_assert_url_label"))
                .and_then(Node::as_str);
            (url, label)
        })
        .collect();

    let mut urls: Vec<&str> = guards.iter().map(|(url, _)| *url).collect();
    urls.sort_unstable();
    assert_eq!(
        urls,
        vec![BAO_URL, PVE_HOST],
        "the guarded endpoints must be the OpenBao and Proxmox URLs"
    );
    let proxmox_label = guards
        .iter()
        .find(|(url, _)| *url == PVE_HOST)
        .and_then(|(_, label)| *label);
    assert_eq!(
        proxmox_label,
        Some("Proxmox"),
        "the Proxmox guard must carry the label Proxmox"
    );
}

#[test]
fn destroy_vm_refuses_a_launch_that_does_not_name_the_vmid() {
    // A destructive launch must name the vmid in its own launch, with no default.
    let tasks = repo::tasks(DESTROY);

    let require = named(&tasks, "Require the run to NAME the vmid it destroys");

    assert!(
        that_items(require)
            .contains(&"(confirm_destroy | default('') | string) == (_vmid | string)"),
        "the launch must name the vmid in confirm_destroy"
    );
}

#[test]
fn the_destroy_vm_template_gives_confirm_destroy_no_default() {
    // The template the launch uses must not pre-fill confirm_destroy, or a launch could destroy a
    // VM it never named.
    let doc = yaml::load_one(&repo::path(TEMPLATES)).expect("templates.yml has a document");
    let templates = doc
        .get("templates")
        .and_then(Node::as_seq)
        .expect("templates.yml lists templates");

    let destroy: Vec<&Node> = templates
        .iter()
        .filter(|t| t.get("name").and_then(Node::as_str) == Some("Destroy VM"))
        .collect();
    assert_eq!(destroy.len(), 1, "exactly one Destroy VM template");

    let survey = destroy[0]
        .get("survey_vars")
        .and_then(Node::as_seq)
        .unwrap_or_default();
    let vars: Vec<&Node> = survey
        .iter()
        .filter(|v| v.get("name").and_then(Node::as_str) == Some("confirm_destroy"))
        .collect();
    assert_eq!(vars.len(), 1, "exactly one confirm_destroy survey variable");

    assert!(
        vars[0].get("default_value").is_none(),
        "confirm_destroy must not have a default_value"
    );
}

#[test]
fn destroy_vm_stops_the_vm_before_deleting_it() {
    let tasks = repo::tasks(DESTROY);

    let stop = named(
        &tasks,
        "Stop the VM if it is running (immediate stop; the VM is being destroyed)",
    );

    assert!(
        url_of(stop).is_some_and(|url| url.contains("/status/stop")),
        "the stop task must call the status/stop endpoint"
    );
    assert_eq!(method_of(stop), "POST", "a stop is a POST");
}

#[test]
fn destroy_vm_deletes_with_purge_by_its_exact_url() {
    // purge=1 removes the backup, replication and HA references with the VM.
    let tasks = repo::tasks(DESTROY);

    let deletes: Vec<&Node> = tasks
        .iter()
        .filter(|t| url_of(t).is_some() && method_of(t) == "DELETE")
        .collect();

    assert_eq!(
        deletes.len(),
        2,
        "two DELETE calls: the VM and each leftover volume"
    );
    assert!(
        deletes.iter().any(|t| url_of(t) == Some(PURGE_URL)),
        "the VM DELETE must carry purge=1 at its exact URL"
    );
}

#[test]
fn destroy_vm_frees_leftovers_only_on_active_image_storages() {
    // Proxmox's own unreferenced-disk scan aborts on a node missing a cluster-wide storage, so the
    // play sweeps ACTIVE image storages itself and frees every volume still tagged with the vmid.
    let tasks = repo::tasks(DESTROY);

    let select = named(&tasks, "Select the storages to sweep for leftovers");
    assert!(
        module_arg(select, "set_fact", "_sweep_storages")
            .is_some_and(|expr| expr.contains("selectattr('active', 'equalto', 1)")),
        "the sweep must select only ACTIVE storages"
    );

    assert!(
        tasks.iter().any(|t| url_of(t) == Some(SWEEP_URL)),
        "each leftover volume is freed by its storage's content URL"
    );
    assert!(
        tasks
            .iter()
            .any(|t| url_of(t).is_some_and(|url| url.contains("content?vmid={{ _vmid }}"))),
        "the storage content is listed by vmid"
    );
    starting(&tasks, "Free every leftover volume");
    starting(&tasks, "Re-check: nothing tagged with this vmid remains");
}

#[test]
fn destroy_vm_never_hands_the_unreferenced_disk_scan_to_proxmox() {
    // Raw-text deny-list: Proxmox's own flag must not appear anywhere in the file.
    let text = std::fs::read_to_string(repo::path(DESTROY)).expect("destroy-vm.yml is readable");

    assert!(
        !text.contains("destroy-unreferenced-disks=1"),
        "destroy-vm.yml passes destroy-unreferenced-disks=1, which aborts on an absent storage"
    );
}

#[test]
fn destroy_vm_verifies_the_destroy_and_the_vmid_absence() {
    // The destroy's task result is checked, the vmid is confirmed gone, and a vmid that was already
    // absent ends the play as a clean no-op.
    let tasks = repo::tasks(DESTROY);

    let verify = named(&tasks, "Verify the destroy task succeeded");
    assert!(
        module(verify, "fail").is_some(),
        "the destroy's task result is checked by a fail"
    );

    let gone = starting(&tasks, "Confirm the vmid is gone");
    assert!(
        module(gone, "uri").is_some(),
        "the vmid's absence is confirmed by a cluster query"
    );

    let end = named(&tasks, "End play when the VM is already gone");
    assert_eq!(
        module(end, "meta").and_then(Node::as_str),
        Some("end_play"),
        "an absent vmid ends the play"
    );
}

#[test]
fn destroy_vm_never_reports_a_uri_call_as_changed() {
    // A uri POST or DELETE never reports changed on its own. Raw-text deny-list.
    let text = std::fs::read_to_string(repo::path(DESTROY)).expect("destroy-vm.yml is readable");

    assert!(
        !text.contains("is changed"),
        "destroy-vm.yml must not use an `is changed` test: a uri call reports nothing on its own"
    );
}

#[test]
fn provision_vm_stops_on_an_answering_or_unprobeable_declared_address() {
    // The probe fails closed: a live answer refuses, and so does a probe that could not run, unless
    // the launch carries the audited override.
    let tasks = repo::tasks(PROVISION);

    let stop = starting(
        &tasks,
        "Stop when something answered at the declared address",
    );

    // The override must be in the `that:` logic, not only in the message: a fail_msg that names it
    // while the condition ignores it would fail closed in name only.
    assert!(
        that_items(stop)
            .iter()
            .any(|item| item.contains("allow_unverified_address")),
        "the stop condition must allow the audited override"
    );
}

#[test]
fn provision_vm_probes_after_the_existence_check_and_before_the_clone() {
    // The probe runs on the create path only: after the existence check, before the clone.
    let tasks = repo::tasks(PROVISION);
    let position = |pred: &dyn Fn(&str) -> bool| {
        tasks
            .iter()
            .position(|t| pred(task_name(t)))
            .unwrap_or_else(|| panic!("a task matching the ordering check is missing"))
    };

    let existence = position(&|name| name == "Check for existing VM");
    let probe =
        position(&|name| name.starts_with("Probe the declared address from the controller"));
    let clone = position(&|name| name.starts_with("Clone template to new VM"));

    assert!(
        existence < probe && probe < clone,
        "the address probe (task {probe}) must sit between the existence check (task {existence}) \
         and the clone (task {clone})"
    );
}
