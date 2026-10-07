"""The VM lifecycle executors record their workflow step result (D10 review 2026-10-02).

Create VM Template (vm-template), Provision VM (provision-vm) and Resize VM (vm-rightsize)
each judge their step from a Proxmox read-back and record it through emit-step-result.yml.
These tests run each playbook's REAL decide + record tasks against synthetic read-backs and
read the recorded result from Ansible's custom stats, so the verdict logic is exercised, not
just its text. Needs ansible-playbook.
"""

import copy
import importlib.util
import json
import shutil
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml
from fake_http import DrainingHandler

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = ROOT / "platform/playbooks"
EMIT = str(PLAYBOOKS / "tasks/emit-step-result.yml")
_spec = importlib.util.spec_from_file_location(
    "step_results", ROOT / "platform/workflows/service-onboarding/lib/step_results.py")
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _plays(name):
    return playbook_yaml.plays(PLAYBOOKS / name)


def _tasks(play, names):
    by_name = {t.get("name"): t for t in playbook_yaml.tasks(play["tasks"])}
    missing = [n for n in names if n not in by_name]
    assert not missing, missing
    out = []
    for n in names:
        task = dict(by_name[n])
        if "ansible.builtin.include_tasks" in task:
            task["ansible.builtin.include_tasks"] = EMIT
        out.append(task)
    return out


def _emit_absolute(node):
    """The harness runs from a scratch directory, so each shared task is named by its path."""
    for task in playbook_yaml.tasks(node):
        if "ansible.builtin.include_tasks" in task:
            task["ansible.builtin.include_tasks"] = str(PLAYBOOKS / task["ansible.builtin.include_tasks"])
    return node


def _run(tmp_path, harness, check=False, inventory=None, extra=(), step=None, env_extra=None):
    """The one result the run recorded, or with `step`, the result for that step."""
    results, rc = _run_all(tmp_path, harness, check, inventory, extra, env_extra)
    if step is not None:
        results = [r for r in results if r["step"] == step]
    assert len(results) == 1, results
    return results[0], rc


def _run_all(tmp_path, harness, check=False, inventory=None, extra=(), env_extra=None):
    play_file = tmp_path / "play.yml"
    play_file.write_text(yaml.safe_dump(_emit_absolute(copy.deepcopy(harness)), sort_keys=False))
    inv = tmp_path / "inv.yml"
    inv.write_text(yaml.safe_dump(inventory or {"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}}))
    env = {**harness_sandbox.env_for(tmp_path), "ANSIBLE_NOCOLOR": "1", "ANSIBLE_SHOW_CUSTOM_STATS": "1",
           **(env_extra or {})}
    done = harness_sandbox.run(
        ["ansible-playbook", "-i", str(inv), str(play_file), *(["--check"] if check else []), *extra],
        tmp_path, cwd=ROOT, env=env)
    # The collector's own parser: the aggregated list when present, else the single result.
    results = step_results.results_in(done.stdout.splitlines())
    assert results, done.stdout + done.stderr
    return results, done.returncode


class FakeProxmox:
    """A Proxmox API stand-in: `routes` maps (method, path) to (status, data); every request
    is logged, so a test can say which writes a run made."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []
        fake = self

        class Handler(DrainingHandler):
            def _reply(self):
                fake.seen.append((self.command, self.path))
                status, data = fake.routes.get((self.command, self.path), (404, None))
                body = json.dumps({"data": data}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = _reply

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def writes(self):
        return [r for r in self.seen if r[0] != "GET"]

    def close(self):
        self.server.shutdown()


# ── vm-template ──────────────────────────────────────────────────────────────
TEMPLATE_TASKS = ("Decide the template step", "Record the step result", "Fail when the template step did not pass")


def _template(tmp_path, data, status=200, present=True, check=False):
    play = next(p for p in _plays("provision-template.yml") if p.get("tasks"))
    harness = [{"hosts": "localhost", "gather_facts": False,
                "vars": {"_vmid": "9000", "_node": "n1", "_tmpl_present": present,
                         "_tmpl_after": {"status": status, "json": {"data": data}}},
                "tasks": _tasks(play, TEMPLATE_TASKS)}]
    return _run(tmp_path, harness, check)


def test_template_with_a_cloud_init_drive_passes(tmp_path):
    result, rc = _template(tmp_path, {"template": 1, "ide2": "vm-lvms:vm-9000-cloudinit,media=cdrom"})
    assert (result["step"], result["status"], rc) == ("vm-template", "pass", 0)
    assert result["evidence"] == {"template_vmid": 9000, "node": "n1"}


def test_template_without_a_cloud_init_drive_fails_and_says_why(tmp_path):
    result, rc = _template(tmp_path, {"template": 1, "ide2": "none,media=cdrom"})
    assert (result["status"], rc != 0) == ("fail", True)
    assert "no cloud-init drive" in result["error"]


# Semaphore task 3195: a live template failed this check with nothing saying where, if
# anywhere, its cloud-init volume was. The error names the drive keys that hold one, and only
# the key names: the config's other values (storage, sizes, descriptions) stay out of it.
def test_a_cloud_init_drive_on_another_key_is_named_by_key_only(tmp_path):
    result, rc = _template(tmp_path, {"template": 1, "ide2": "none,media=cdrom",
                                      "scsi1": "vm-lvms:vm-9000-cloudinit,media=cdrom",
                                      "ide0": "secretstore:vm-9000-cloudinit",
                                      "description": "vm-lvms:vm-9000-cloudinit"})
    assert (result["status"], rc != 0) == ("fail", True)
    assert result["error"].endswith("has no cloud-init drive on ide2 (cloudinit volume found on: ide0, scsi1)")
    assert "vm-lvms" not in result["error"] and "secretstore" not in result["error"]


def test_a_template_with_no_cloud_init_volume_says_so(tmp_path):
    result, rc = _template(tmp_path, {"template": 1, "scsi0": "vm-lvms:vm-9000-disk-0,size=20G", "cores": 2})
    assert (result["status"], rc != 0) == ("fail", True)
    assert result["error"].endswith("has no cloud-init drive on ide2 (no cloudinit volume on any drive key)")


def test_a_null_read_back_is_still_recorded_as_a_failure(tmp_path):
    # PR 464 review: a 200 with "data": null survived default({}) and dict2items raised before
    # the step result was recorded.
    result, rc = _template(tmp_path, None)
    assert (result["step"], result["status"], rc != 0) == ("vm-template", "fail", True)
    assert "is not a template (HTTP 200)" in result["error"]
    assert result["error"].endswith("has no cloud-init drive on ide2 (no cloudinit volume on any drive key)")


def test_a_vm_that_is_not_a_template_fails(tmp_path):
    result, rc = _template(tmp_path, {}, status=500)
    assert (result["status"], rc != 0) == ("fail", True)


def test_a_dry_run_with_no_template_yet_records_a_skip(tmp_path):
    result, rc = _template(tmp_path, {}, status=500, present=False, check=True)
    assert (result["status"], result["check_mode"], rc) == ("skip", True, 0)


# ── vm-rightsize ─────────────────────────────────────────────────────────────
RESIZE_TASKS = ("Decide the rightsize step", "Record the step result",
                "Fail when the VM does not match its declared spec")


def _resize(tmp_path, cfg, needs_restart=False, allow_reboot=False):
    play = _plays("resize-vm.yml")[0]
    harness = [{"hosts": "localhost", "gather_facts": False,
                "vars": {"_want_cores": "4", "_want_memory": "8192", "_want_disk_gb": "32",
                         "_disk_device": "scsi0", "_needs_restart": needs_restart,
                         "_allow_reboot": allow_reboot, "_cfg_after": {"json": {"data": cfg}}},
                "tasks": _tasks(play, RESIZE_TASKS)}]
    return _run(tmp_path, harness)


CONVERGED = {"cores": 4, "memory": 8192, "scsi0": "vm-lvms:vm-215-disk-0,size=32G"}


def test_rightsize_converged_passes_with_the_read_back_values(tmp_path):
    result, rc = _resize(tmp_path, CONVERGED)
    assert (result["step"], result["status"], rc) == ("vm-rightsize", "pass", 0)
    assert result["evidence"] == {"cores": 4, "memory_mb": 8192, "disk_gb": 32}


def test_rightsize_with_a_restart_pending_is_not_live_yet(tmp_path):
    result, rc = _resize(tmp_path, CONVERGED, needs_restart=True)
    assert (result["status"], rc != 0) == ("fail", True)
    assert "restart is pending" in result["error"]


def test_rightsize_drift_fails_per_dimension(tmp_path):
    result, _ = _resize(tmp_path, {"cores": 2, "memory": 8192, "scsi0": "x,size=320G"})
    assert result["status"] == "fail"
    assert "cores 2" in result["error"] and "is not 32G" in result["error"]


# ── provision-vm ─────────────────────────────────────────────────────────────
def _provision(tmp_path, cfg, status=200, vm_exists=True, do_migrate=False, provisioned=False, check=False):
    plays = _plays("provision-vm.yml")
    prov = next(p for p in plays if p.get("name") == "Provision VM from template")
    record = next(p for p in plays if p.get("name") == "Record the provision-vm step result")
    decide = _tasks(prov, ["Decide the provision-vm step"])
    rec = dict(record, tasks=_tasks(record, [t["name"] for t in record["tasks"]]))
    harness = [{"hosts": "localhost", "gather_facts": False,
                "vars": {"target_service": "dns", "_vmid": "220", "_node": "n1", "_cores": 2, "_mem": 4096,
                         "_disk": "32G", "_onboot": "1", "_vm_exists": vm_exists, "_do_migrate": do_migrate,
                         "_pv_cfg": {"status": status, "json": {"data": cfg}}},
                "tasks": decide}]
    if provisioned:
        harness[0]["tasks"].append({"ansible.builtin.add_host": {"name": "vm1", "groups": "_provisioned_vm"}})
    return _run(tmp_path, harness + [rec], check)


GOOD_VM = {"cores": 2, "memory": 4096, "onboot": 1, "scsi0": "vm-lvms:vm-220-disk-0,size=32G"}


def test_provision_vm_matching_the_declaration_passes(tmp_path):
    result, rc = _provision(tmp_path, GOOD_VM)
    assert (result["step"], result["status"], rc) == ("provision-vm", "pass", 0)
    assert result["evidence"] == {"vmid": 220, "node": "n1", "onboot": 1}
    assert result["undo"] == "Destroy VM"


def test_provision_vm_without_onboot_fails(tmp_path):
    result, rc = _provision(tmp_path, {**GOOD_VM, "onboot": 0})
    assert (result["status"], rc != 0) == ("fail", True)
    assert "onboot 0" in result["error"]


def test_provision_vm_failure_waits_for_the_verdict_when_post_boot_follows(tmp_path):
    # The record play must not stop the run before the post-boot checks; the verdict fails it.
    result, rc = _provision(tmp_path, {**GOOD_VM, "scsi0": "x,size=20G"}, provisioned=True)
    assert (result["status"], rc) == ("fail", 0)


def test_provision_vm_dry_run_of_an_absent_vm_records_a_skip(tmp_path):
    result, rc = _provision(tmp_path, {}, status=404, vm_exists=False, check=True)
    assert (result["status"], result["check_mode"], rc) == ("skip", True, 0)


def test_provision_vm_verdict_fails_on_a_recorded_mismatch():
    verdict = next(p for p in _plays("provision-vm.yml") if str(p.get("name", "")).startswith("Verdict:"))
    that = next(t for t in verdict["tasks"] if "ansible.builtin.assert" in t)["ansible.builtin.assert"]["that"]
    assert any("_pv_step.errors" in c for c in that)


@pytest.mark.parametrize("playbook,step", [("provision-template.yml", "vm-template"),
                                           ("resize-vm.yml", "vm-rightsize"),
                                           ("provision-vm.yml", "provision-vm")])
def test_the_result_is_recorded_after_every_change_the_play_makes(playbook, step):
    # "Emitted last": nothing after the record but the failure that reports it.
    for play in _plays(playbook):
        tasks = play.get("tasks") or []
        idx = [i for i, t in enumerate(tasks)
               if str(t.get("ansible.builtin.include_tasks", "")).endswith("emit-step-result.yml")]
        if not idx:
            continue
        assert tasks[idx[0]]["vars"]["step_result_step"] == step
        assert all("ansible.builtin.fail" in t for t in tasks[idx[0] + 1:]), playbook
        return
    pytest.fail(f"{playbook} records no step result")


# ── Whole plays against a fake Proxmox: guards, skipped writes, hard failures ──
TEMPLATE_CFG = "/api2/json/nodes/alphacentauri/qemu/9000/config"


def _template_play(tmp_path, cfg, check=False):
    play = next(p for p in _plays("provision-template.yml") if p.get("tasks"))
    fake = FakeProxmox({("GET", TEMPLATE_CFG): (200, cfg)})
    try:
        result, rc = _run(tmp_path, [play], check=check,
                          extra=["-e", json.dumps({"pve_host": fake.url, "pve_token_id": "automation@pve!fixture",
                                                     "pve_token_secret": "x"})])
    finally:
        fake.close()
    return result, rc, fake


def test_an_existing_template_is_adopted_without_a_write_and_proven(tmp_path):
    result, rc, fake = _template_play(tmp_path, {"template": 1, "ide2": "vm-lvms:vm-9000-cloudinit,media=cdrom"})
    assert (result["status"], rc) == ("pass", 0)
    assert fake.writes() == []  # the create block was skipped
    assert fake.seen.count(("GET", TEMPLATE_CFG)) == 2  # the guard's read, then the read-back


# A stand-in for community.hashi_vault.hashi_vault, found first on ANSIBLE_COLLECTIONS_PATH.
# The real lookup needs hvac, which the test environment does not install. The stub answers
# from a JSON file of secret paths and logs each read with the address it was sent to, so the
# plays' own store reads run unmodified: no `_` variable is injected.
FAKE_LOOKUP = """
import json
import os

from ansible.plugins.lookup import LookupBase

DOCUMENTATION = "name: hashi_vault\\nshort_description: test stand-in\\n"


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        store = json.load(open(os.environ["FAKE_BAO_STORE"]))
        out = []
        for term in terms:
            path, _, field = term.partition(":")
            with open(os.environ["FAKE_BAO_LOG"], "a") as log:
                log.write(json.dumps({"path": path, "url": kwargs.get("url")}) + "\\n")
            out.append(store[path][field] if field else store[path])
        return out
"""
BAO = "https://bao.invalid"


def _fake_store(tmp_path, proxmox):
    coll = tmp_path / "collections/ansible_collections/community/hashi_vault"
    (coll / "plugins/lookup").mkdir(parents=True)
    (coll / "meta").mkdir()
    (coll / "galaxy.yml").write_text("namespace: community\nname: hashi_vault\nversion: 0.0.0\n")
    (coll / "meta/runtime.yml").write_text('requires_ansible: ">=2.14"\n')
    (coll / "plugins/lookup/hashi_vault.py").write_text(FAKE_LOOKUP)
    store = tmp_path / "store.json"
    store.write_text(json.dumps({"secret/data/services/proxmox": proxmox,
                                 "secret/data/services/ssh": {"public_key": "ssh-ed25519 AAAA"}}))
    return {"ANSIBLE_COLLECTIONS_PATH": str(tmp_path / "collections"), "FAKE_BAO_STORE": str(store),
            "FAKE_BAO_LOG": str(tmp_path / "bao.log"), "BAO_ROLE_ID": "synthetic-role",
            "BAO_SECRET_ID": "synthetic-role-secret"}


def _store_reads(tmp_path):
    log = tmp_path / "bao.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _template_play_from_store(tmp_path, record):
    """The Semaphore path for the template play alone: no operator input, the connection comes
    from secret/services/proxmox through the play's own lookup."""
    play = next(p for p in _plays("provision-template.yml") if p.get("tasks"))
    fake = FakeProxmox({("GET", TEMPLATE_CFG): (200, {"template": 1, "ide2": "vm-lvms:vm-9000-cloudinit"})})
    env = _fake_store(tmp_path, {"url": fake.url, "api_token": "synthetic", **record})
    try:
        result, rc = _run(tmp_path, [play], extra=["-e", json.dumps({"openbao_addr": BAO})], env_extra=env)
    finally:
        fake.close()
    return result, rc, fake


def test_the_template_play_takes_the_token_id_from_the_store(tmp_path):
    result, rc, fake = _template_play_from_store(tmp_path, {"token_id": "automation@pve!fixture"})
    assert (result["status"], rc) == ("pass", 0), result
    assert ("GET", TEMPLATE_CFG) in fake.seen
    assert {"path": "secret/data/services/proxmox", "url": BAO} in _store_reads(tmp_path)


def test_the_template_play_refuses_a_store_without_a_token_id_and_sends_nothing(tmp_path):
    result, rc, fake = _template_play_from_store(tmp_path, {})
    assert (result["status"], rc != 0) == ("fail", True)
    assert "has no token_id" in result["error"]
    assert fake.seen == []


# The whole playbook as Semaphore runs it: the imported validation play, the template play and
# the post-validation play, each resolving the connection itself. A production dry run (task
# 3166) found the template play sending to an empty URL because only the validation play read
# the store, and play vars do not cross the play boundary.
CLUSTER = {
    ("GET", "/api2/json/version"): (200, {"version": "8.2"}),
    ("GET", "/api2/json/nodes"): (200, [{"node": "alphacentauri", "status": "online"}]),
    ("GET", "/api2/json/cluster/resources"): (200, []),
    ("GET", "/api2/json/nodes/alphacentauri/storage"): (200, [
        {"storage": "vm-lvms", "active": 1, "content": "images,rootdir", "avail": 0},
        {"storage": "SharedISOs", "active": 1, "content": "iso"}]),
    ("GET", "/api2/json/nodes/alphacentauri/storage/SharedISOs/content"): (200, [
        {"volid": "SharedISOs:iso/ubuntu-24.04.3-live-server-amd64.iso"}]),
    ("GET", "/api2/json/cluster/resources?type=vm"): (200, [
        {"vmid": 9000, "name": "ubuntu-2404-template", "node": "alphacentauri", "template": 1}]),
    ("GET", TEMPLATE_CFG): (200, {"template": 1, "ide2": "vm-lvms:vm-9000-cloudinit"}),
    ("GET", "/api2/json/nodes/alphacentauri/network"): (200, [{"type": "bridge", "iface": "vmbr0"}]),
}


def _whole_template_playbook(tmp_path, record, check=False):
    fake = FakeProxmox(CLUSTER)
    inv = tmp_path / "inv.yml"
    inv.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}}))
    env = {**harness_sandbox.env_for(tmp_path), "ANSIBLE_SHOW_CUSTOM_STATS": "1",
           **_fake_store(tmp_path, {"url": fake.url, "api_token": "synthetic", **record})}
    try:
        done = harness_sandbox.run(
            ["ansible-playbook", "-i", str(inv), str(PLAYBOOKS / "provision-template.yml"),
             "-e", json.dumps({"openbao_addr": BAO}), *(["--check"] if check else [])],
            tmp_path, cwd=ROOT, env=env)
    finally:
        fake.close()
    return done, fake


@pytest.mark.parametrize("check", [False, True], ids=["run", "dry-run"])
def test_the_whole_template_playbook_reaches_proxmox_from_the_store_in_every_play(tmp_path, check):
    done, fake = _whole_template_playbook(tmp_path, {"token_id": "automation@pve!fixture"}, check)
    assert done.returncode == 0, done.stdout[-3000:]
    results = step_results.results_in(done.stdout.splitlines())
    assert [(r["step"], r["status"]) for r in results] == [("vm-template", "pass")], results
    # validation, the template play's own read and read-back, post-validation
    assert fake.seen.count(("GET", TEMPLATE_CFG)) == 4
    assert fake.writes() == []
    assert {r["url"] for r in _store_reads(tmp_path)} == {BAO}


def test_the_whole_template_playbook_refuses_a_store_without_a_token_id_before_any_request(tmp_path):
    done, fake = _whole_template_playbook(tmp_path, {})
    assert done.returncode != 0
    assert "has no token_id" in done.stdout
    assert fake.seen == []


def test_cloudinit_named_anywhere_but_the_drive_does_not_pass(tmp_path):
    result, rc, _ = _template_play(tmp_path, {"template": 1, "description": "cloudinit ready",
                                              "tags": "cloudinit", "ide2": "none,media=cdrom"})
    assert (result["status"], rc != 0) == ("fail", True)
    assert "no cloud-init drive on ide2" in result["error"]


def test_a_vmid_held_by_a_non_template_is_recorded_as_a_failure(tmp_path):
    result, rc, fake = _template_play(tmp_path, {"template": 0})
    assert (result["status"], rc != 0) == ("fail", True)
    assert result["error"].startswith("Fail if VMID taken by non-template: VMID 9000 exists but is not a template.")
    assert fake.writes() == []


PV = "/api2/json/nodes/n1/qemu/220"
PV_INVENTORY = {"all": {"hosts": {"localhost": {"ansible_connection": "local"}},
                        "children": {"dns_svc": {"hosts": {"dns1": {
                            "vm_vmid": 220, "vm_node": "n1", "vm_cores": 2, "vm_memory": 4096, "vm_disk": "32G",
                            "vm_ip": "127.0.0.1", "vm_gateway": "192.0.2.1", "vm_nameserver": "192.0.2.1",
                            "vm_disk_storage": "s"}}}}}}


def _provision_play(tmp_path, cfg, name="dns"):
    fake = FakeProxmox({
        ("GET", "/api2/json/nodes/alphacentauri/qemu/9000/config"): (200, {"template": 1}),
        ("GET", "/api2/json/cluster/resources?type=vm"): (200, [{"vmid": 220, "name": name, "node": "n1"}]),
        ("GET", PV + "/status/current"): (200, {"status": "running"}),
        ("PUT", PV + "/config"): (200, None),
        ("PUT", PV + "/resize"): (500, None),  # the resize the play tolerates
        ("GET", PV + "/config"): (200, cfg),
        ("POST", PV + "/agent/ping"): (200, {}),
    })
    plays = _plays("provision-vm.yml")
    prov = copy.deepcopy(next(p for p in plays if p.get("name") == "Provision VM from template"))
    for task in playbook_yaml.tasks(prov["tasks"]):
        if task.get("name") == "Wait for SSH":  # the fake's port stands in for sshd
            task["ansible.builtin.wait_for"].update(port=fake.server.server_port, timeout=10)
    record = next(p for p in plays if p.get("name") == "Record the provision-vm step result")
    verdict = next(p for p in plays if str(p.get("name", "")).startswith("Verdict:"))
    try:
        results, rc = _run_all(tmp_path, [prov, record, verdict], inventory=PV_INVENTORY, extra=["-e", json.dumps({
            "_pve_host": fake.url, "_pve_secret": "x", "_pve_token_id": "t", "_ssh_pub": "ssh-ed25519 AAAA",
            "target_service": "dns", "ci_user": "u"})])
    finally:
        fake.close()
    fake.results = results
    by_step = {r["step"]: r for r in results}
    # cloud-init is recorded only when the run reaches the verdict play; a hard failure stops it.
    assert "provision-vm" in by_step and set(by_step) <= {"provision-vm", "cloud-init"}, results
    return by_step["provision-vm"], rc, fake


def test_a_failed_disk_resize_fails_the_run_at_the_verdict(tmp_path):
    result, rc, fake = _provision_play(tmp_path, {**GOOD_VM, "scsi0": "s:vm-220-disk-0,size=20G"})
    assert ("PUT", PV + "/resize") in fake.seen
    assert (result["status"], rc != 0) == ("fail", True)
    assert "disk scsi0 is not 32G" in result["error"]


def test_a_matching_vm_passes_the_whole_play(tmp_path):
    result, rc, _ = _provision_play(tmp_path, GOOD_VM)
    assert (result["status"], rc) == ("pass", 0)


def test_a_hard_failure_in_the_provisioning_play_is_still_recorded(tmp_path):
    # A different VM at the declared vmid: refused before any write, and recorded.
    result, rc, fake = _provision_play(tmp_path, GOOD_VM, name="gh-runner-01")
    assert (result["status"], rc != 0) == ("fail", True)
    assert result["error"].startswith("Refuse to adopt a DIFFERENT VM that already holds the declared vmid:")
    assert fake.writes() == []


def test_a_hard_failure_in_resize_is_still_recorded(tmp_path):
    play = _plays("resize-vm.yml")[0]
    result, rc = _run(tmp_path, [play], extra=["-e", json.dumps({
        "_pve_host": "http://pve.invalid", "target_vmid": 1, "target_node": "n", "openbao_addr": "x"})])
    assert (result["step"], result["status"], rc != 0) == ("vm-rightsize", "fail", True)
    assert result["error"].startswith("Require HTTPS for the Proxmox API:")
    assert result["evidence"] == {"cores": None, "memory_mb": None, "disk_gb": None}


@pytest.mark.parametrize("playbook,fact", [("provision-template.yml", "_tmpl_hard_error"),
                                           ("resize-vm.yml", "_rs_hard_error"),
                                           ("provision-vm.yml", "_pv_step")])
@pytest.mark.parametrize("no_log", [True, False])
def test_the_rescue_names_the_failed_task_and_withholds_a_no_log_message(tmp_path, playbook, fact, no_log):
    # The playbook's REAL rescue, behind a block whose one task fails with a secret message.
    rescue = next(t for t in playbook_yaml.tasks(_plays(playbook))
                  if "block" in t and t.get("rescue") and str(t.get("name", "")).split()[0]
                  in ("Create", "Converge", "Provision"))["rescue"]
    play = [{"hosts": "localhost", "gather_facts": False, "tasks": [
        {"name": "wrapped", "block": [{"name": "Boom", "no_log": no_log,
                                       "ansible.builtin.fail": {"msg": "SECRET-VALUE"}}],
         "rescue": rescue},
        {"ansible.builtin.copy": {"content": "{{ " + fact + " | to_json }}", "dest": str(tmp_path / "out"),
                                  "mode": "0600"}}]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    done = harness_sandbox.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(tmp_path / "play.yml"),
                                "-e", "target_service=dns"], tmp_path, cwd=ROOT,
                               env=harness_sandbox.env_for(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    recorded = (tmp_path / "out").read_text()
    assert "Boom:" in recorded
    assert ("SECRET-VALUE" in recorded) is not no_log
    assert ("details hidden" in recorded) is no_log


# ── provision-vm records cloud-init too: one run, two step results ──────────────
def _cloud_init(tmp_path, login=None, cloudinit=None, check=False):
    """The verdict play judged from post-boot classifications planted on the VM host (both None:
    no VM reached post-boot, as in a dry run that stopped early)."""
    verdict = next(p for p in _plays("provision-vm.yml") if str(p.get("name", "")).startswith("Verdict:"))
    seed = {"hosts": "localhost", "gather_facts": False, "vars": {"target_service": "dns"}, "tasks": []}
    if login is not None:
        seed["tasks"].append({"ansible.builtin.add_host": {
            "name": "vm1", "groups": "_provisioned_vm", "_pv_login": login, "_pv_cloudinit": cloudinit}})
    results, rc = _run_all(tmp_path, [seed, verdict], check=check, extra=["-e", "target_service=dns"])
    assert [r["step"] for r in results] == ["cloud-init"], results
    return results[0], rc


def test_cloud_init_done_and_ssh_ok_passes(tmp_path):
    result, rc = _cloud_init(tmp_path, "ok", "done")
    assert (result["status"], rc, result["service"]) == ("pass", 0, "dns")
    assert result["evidence"] == {"cloud_init_done": True, "ssh_reachable": True}
    assert result["undo"] == "Destroy VM"


def test_cloud_init_with_recoverable_errors_still_finished(tmp_path):
    result, _ = _cloud_init(tmp_path, "ok", "done with recoverable errors")
    assert result["status"] == "pass"


@pytest.mark.parametrize("login,cloudinit,done,ssh", [("ok", "FAILED", False, True),
                                                       ("FAILED", "not checked", False, False)])
def test_cloud_init_failure_is_recorded_with_its_evidence(tmp_path, login, cloudinit, done, ssh):
    result, rc = _cloud_init(tmp_path, login, cloudinit)
    assert (result["status"], rc != 0) == ("fail", True)
    assert result["evidence"] == {"cloud_init_done": done, "ssh_reachable": ssh}
    assert result["error"] == f"SSH login: {login}; cloud-init: {cloudinit}"


def test_cloud_init_never_attempted_is_a_skip_not_a_pass(tmp_path):
    result, rc = _cloud_init(tmp_path, check=True)
    assert (result["status"], result["check_mode"], rc) == ("skip", True, 0)
    assert result["evidence"] == {"cloud_init_done": None, "ssh_reachable": None}


def test_a_provision_run_records_both_steps(tmp_path):
    # The whole provision -> record -> verdict chain records both steps, in order.
    result, rc, fake = _provision_play(tmp_path, GOOD_VM)
    assert (result["status"], rc) == ("pass", 0)
    assert [r["step"] for r in fake.results] == ["provision-vm", "cloud-init"]
