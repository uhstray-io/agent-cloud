"""Focused guards for the PBS virtual data-disk attachment workflow."""

from pathlib import Path
import re

import yaml
from jinja2.nativetypes import NativeEnvironment

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/attach-pbs-data-disk.yml"


def _tasks(play: dict, base: Path = ROOT / "platform/playbooks") -> list[dict]:
    result: list[dict] = []
    for task in play.get("tasks", []):
        result.append(task)
        result.extend(_tasks({"tasks": task.get("block", [])}, base))
        imported = task.get("ansible.builtin.import_tasks") or task.get("ansible.builtin.include_tasks")
        if imported and "assert-bao-transport.yml" not in imported:
            resolved = imported.replace("{{ playbook_dir }}", str(base))
            resolved_path = Path(resolved)
            if not resolved_path.is_absolute():
                resolved_path = base / resolved_path
            result.extend(_tasks({"tasks": yaml.safe_load(resolved_path.read_text())}, base))
    return result


def test_workflow_reruns_pinned_preflight_before_vm_config_read_and_write() -> None:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert plays[1]["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/attach-pbs-data-disk.yml",
        "platform/playbooks/refuse-internal-extra-vars.yml",
        "platform/playbooks/tasks/o11y-pbs-physical-storage-survey.yml",
        "platform/playbooks/tasks/preflight-o11y-pbs-thick-lvm-allocation.yml",
        "platform/playbooks/tasks/validate-pbs-data-disk-vm-config.yml",
        "platform/playbooks/tasks/assert-bao-transport.yml",
        "platform/playbooks/files/inspect_o11y_pbs_physical_storage.py",
        "platform/semaphore/templates.yml",
    ]
    play = plays[2]
    tasks = _tasks(play)
    names = [task.get("name", "") for task in tasks]
    assert play["hosts"] == "localhost"
    assert names.index("Refuse VM identity, layout, bus, or storage drift before any write") < names.index(
        "Allocate exactly one virtual data disk through the Proxmox API"
    )
    order = [
        "Read the exact declared PBS VM configuration",
        "Extract the occupied disk buses and intended scsi1 value",
        "Survey the pinned thick-LVM backing only when allocating a new disk",
        "Require pinned backing identity and 512-GiB allocation headroom",
        "Re-read the exact PBS VM config after storage preflight",
        "Revalidate the current PBS VM config and refresh its digest",
        "Allocate exactly one virtual data disk through the Proxmox API",
    ]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    for task in tasks:
        if task.get("name", "").startswith(("Survey the pinned", "Require pinned backing")):
            assert task["when"] == "_scsi1 | length == 0"


def test_only_guarded_api_allocation_writes_exact_scsi1_with_config_digest() -> None:
    tasks = _tasks(yaml.safe_load(PLAYBOOK.read_text())[2])
    writes = [
        task["ansible.builtin.uri"]
        for task in tasks
        if task.get("ansible.builtin.uri", {}).get("method") in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert len(writes) == 1
    assert "/nodes/{{ _node | urlencode }}/qemu/{{ _vmid }}/config" in writes[0]["url"]
    assert writes[0]["body"] == {"scsi1": "{{ _disk_request }}", "digest": "{{ _vm_config_digest }}"}
    assert "{{ _storage }}:512" in str(yaml.safe_load(PLAYBOOK.read_text())[2]["vars"])
    source = PLAYBOOK.read_text() + (ROOT / "platform/playbooks/tasks/validate-pbs-data-disk-vm-config.yml").read_text()
    assert "vm-{{ _vmid }}-disk-[0-9]+,size=512G" in source
    assert "host-device" in source
    assert "_disk_keys | difference(['scsi0', 'scsi1', 'ide2']) | length == 0" in source


def test_conflicting_or_mismatched_existing_disks_refuse_but_exact_scsi1_is_idempotent() -> None:
    tasks = _tasks(yaml.safe_load(PLAYBOOK.read_text())[2])
    validate = next(task for task in tasks if task.get("name", "").startswith("Refuse VM identity"))
    checks = " ".join(validate["ansible.builtin.assert"]["that"])
    assert "_disk_keys | difference(['scsi0', 'scsi1', 'ide2']) | length == 0" in checks
    assert "_scsi1 | length == 0 or _scsi1 is search(_expected_disk_pattern)" in checks
    allocate = next(task for task in tasks if task.get("name", "").startswith("Allocate exactly one"))
    assert allocate["when"] == ["not ansible_check_mode", "_scsi1 | length == 0"]
    assert "digest" in checks and "_vm_config_digest" in checks
    final_readback = next(task for task in tasks if task.get("name", "").startswith("Require exact scsi1 readback"))
    assert "_final_config.json.data.scsi1" in str(final_readback["ansible.builtin.assert"])


def test_exact_guest_validation_matches_declared_network_bridge_cores_and_memory() -> None:
    play_vars = yaml.safe_load(PLAYBOOK.read_text())[2]["vars"]
    for name in ("_ip", "_gw", "_netmask", "_bridge", "_cores", "_memory"):
        assert name in play_vars
    tasks = _tasks(yaml.safe_load(PLAYBOOK.read_text())[2])
    declaration = next(task for task in tasks if task.get("name", "").startswith("Require one declared PBS VM"))
    declaration_checks = declaration["ansible.builtin.assert"]["that"]
    for check in ("_ip | length > 0", "_gw | length > 0", "_cores | int > 0", "_memory | int > 0"):
        assert check in declaration_checks

    tasks = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/validate-pbs-data-disk-vm-config.yml").read_text()
    )
    validate = next(task for task in tasks if task.get("name", "").startswith("Refuse VM identity"))
    checks = validate["ansible.builtin.assert"]["that"]
    expressions = [
        next(check for check in checks if ".net0" in check),
        next(check for check in checks if ".ipconfig0" in check),
        next(check for check in checks if ".cores" in check),
        next(check for check in checks if ".memory" in check),
    ]
    env = NativeEnvironment()
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    context = {
        "_vm_config": {
            "json": {
                "data": {
                    "net0": "virtio=DE:AD:BE:EF:00:01,bridge=vmbr0",
                    "ipconfig0": "ip=192.0.2.44/24,gw=192.0.2.1",
                    "cores": 4,
                    "memory": 8192,
                }
            }
        },
        "_bridge": "vmbr0",
        "_ip": "192.0.2.44",
        "_netmask": 24,
        "_gw": "192.0.2.1",
        "_cores": "4",
        "_memory": "8192",
    }
    render_checks = [env.from_string("{{ " + expression + " }}") for expression in expressions]
    assert all(template.render(**context) is True for template in render_checks)

    context["_vm_config"]["json"]["data"]["ipconfig0"] = "ip=192.0.2.45/24,gw=192.0.2.1"
    assert render_checks[1].render(**context) is False
    context["_vm_config"]["json"]["data"]["ipconfig0"] = "ip=192.0.2.44/24,gw=192.0.2.1"
    context["_vm_config"]["json"]["data"]["net0"] = "virtio=DE:AD:BE:EF:00:01,bridge=vmbr1"
    assert render_checks[0].render(**context) is False
    context["_vm_config"]["json"]["data"]["net0"] = "virtio=DE:AD:BE:EF:00:01,bridge=vmbr0"
    context["_vm_config"]["json"]["data"]["cores"] = 8
    assert render_checks[2].render(**context) is False
    context["_vm_config"]["json"]["data"]["cores"] = 4
    context["_vm_config"]["json"]["data"]["memory"] = 16384
    assert render_checks[3].render(**context) is False


def test_pbs_data_disk_inventory_must_match_pinned_store_and_exact_512g_declaration() -> None:
    tasks = _tasks(yaml.safe_load(PLAYBOOK.read_text())[2])
    declaration = next(
        task for task in tasks if task.get("name", "").startswith("Require one declared PBS VM")
    )
    checks = declaration["ansible.builtin.assert"]["that"]
    assert "_decl.pbs_data_disk_storage | default('') == _storage" in checks
    assert "_decl.pbs_data_disk_size | default('') == '512G'" in checks
    override_guard = next(
        task for task in tasks if task.get("name", "").startswith("Refuse launch-time PBS")
    )
    assert "pbs_data_disk_storage" in str(override_guard)
    assert "pbs_data_disk_size" in str(override_guard)
    assert "_disk_request: \"{{ _storage }}:512\"" in PLAYBOOK.read_text()


def test_openbao_uses_matching_inventory_url_and_guards_transport_before_lookup() -> None:
    play = yaml.safe_load(PLAYBOOK.read_text())[2]
    tasks = _tasks(play)
    names = [task.get("name", "") for task in tasks]
    vars_text = str(play["vars"])
    assert "groups['openbao_svc']" in vars_text
    assert "hostvars[_openbao_host].service_url" in vars_text
    assert "_pve_data" not in play["vars"]
    declaration = next(
        task for task in tasks if task.get("name", "").startswith("Require one declared PBS VM")
    )
    guards = declaration["ansible.builtin.assert"]["that"]
    assert "_openbao_hosts | length == 1" in guards
    assert "openbao_addr | default('') == _openbao_inventory_url" in guards
    forgery_task = next(task for task in tasks if task.get("name", "").startswith("Refuse launch-time"))
    forged_names = forgery_task["vars"]["_pbs_allocation_names"]
    assert "service_url" in forged_names
    assert "proxmox_validate_certs" in forged_names
    assert "openbao_addr" not in forged_names
    assert "bao_role_id" not in forged_names
    assert "bao_secret_id" not in forged_names
    guard_index = names.index("Refuse cleartext OpenBao transport")
    lookup_index = names.index("Read Proxmox API credentials from OpenBao")
    derive_index = names.index("Keep Proxmox API credentials private")
    assert guard_index < lookup_index < derive_index
    include_path = next(
        task["ansible.builtin.include_tasks"]
        for task in tasks
        if task.get("ansible.builtin.include_tasks", "").startswith("{{ playbook_dir }}")
    )
    assert include_path == "{{ playbook_dir }}/tasks/assert-bao-transport.yml"
    resolved_guard = Path(include_path.replace("{{ playbook_dir }}", str(PLAYBOOK.parent)))
    assert resolved_guard.is_file()
    assert tasks[lookup_index].get("no_log") is True
    assert tasks[derive_index].get("no_log") is True


def test_openbao_url_match_and_shared_transport_policy_accept_internal_http_and_refuse_public_http() -> None:
    play = yaml.safe_load(PLAYBOOK.read_text())[2]
    tasks = _tasks(play)
    declaration = next(
        task for task in tasks if task.get("name", "").startswith("Require one declared PBS VM")
    )
    match_expression = next(
        check for check in declaration["ansible.builtin.assert"]["that"]
        if check == "openbao_addr | default('') == _openbao_inventory_url"
    )
    template = NativeEnvironment().from_string("{{ " + match_expression + " }}")
    internal_http = "http://" + ".".join(("10", "1", "2", "3")) + ":8200"
    assert template.render(openbao_addr=internal_http, _openbao_inventory_url=internal_http) is True
    assert template.render(openbao_addr="http://public.example.invalid:8200", _openbao_inventory_url=internal_http) is False

    source = (ROOT / "platform/playbooks/tasks/assert-bao-transport.yml").read_text()
    patterns = dict(re.findall(r"^\s+(_pat_[a-z0-9_]+): '(.*?)'$", source, re.M))

    def expand(name: str) -> str:
        pattern = patterns[name]
        for _ in range(5):
            expanded = re.sub(
                r"\{\{\s*(_pat_[a-z0-9_]+)\s*\}\}",
                lambda match: patterns[match.group(1)],
                pattern,
            )
            if expanded == pattern:
                break
            pattern = expanded
        return pattern.replace(r"\\", "\\")

    strict = expand("_pat_strict")
    https = expand("_pat_https")

    def transport_accepts(url: str) -> bool:
        return bool(re.match(strict, url) and (not url.startswith("https://") or re.match(https, url)))

    assert transport_accepts(internal_http)
    assert not transport_accepts("http://public.example.invalid:8200")


def test_check_mode_reads_and_reports_without_any_mutation_or_host_device_operation() -> None:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    tasks = _tasks(plays[2])
    post = next(task for task in tasks if task.get("name", "").startswith("Allocate exactly one"))
    assert "not ansible_check_mode" in post["when"]
    assert not any("ansible.builtin.shell" in task for task in tasks)
    assert not any("/dev/" in str(task) or "ansible.builtin.shell" in task for task in tasks)
    config_reads = [
        task["ansible.builtin.uri"]
        for task in tasks
        if task.get("ansible.builtin.uri", {}).get("method") == "GET"
        and "/qemu/{{ _vmid }}/config" in task.get("ansible.builtin.uri", {}).get("url", "")
    ]
    assert len(config_reads) == 3
    assert all(task.get("check_mode") is False for task in tasks if task.get("ansible.builtin.uri", {}).get("method") == "GET")


def test_semaphore_template_is_dev_bound_and_only_accepts_reviewed_sha() -> None:
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item["name"] == "Attach PBS Data Disk (Dev)")
    assert template["playbook"] == "platform/playbooks/attach-pbs-data-disk.yml"
    assert template["repository"] == "agent-cloud dev"
    assert template["survey_vars"] == [
        {
            "name": "expected_repository_sha",
            "title": "Reviewed commit SHA",
            "description": "Exact reviewed Dev revision for the guarded PBS virtual-disk attachment",
            "type": "string",
            "required": True,
        }
    ]


def test_rerun_preflight_condition_skips_exact_existing_disk_and_requires_missing_disk() -> None:
    tasks = _tasks(yaml.safe_load(PLAYBOOK.read_text())[2])
    include = next(
        task for task in tasks
        if task.get("name", "").startswith("Require pinned backing identity")
    )
    evaluate = NativeEnvironment().from_string("{{ " + include["when"] + " }}")
    exact = "pbs-data:vm-101-disk-1,size=512G"
    assert evaluate.render(_scsi1=exact) is False
    assert evaluate.render(_scsi1="") is True
