"""validate-address places its VM record under the tenant and site discovery writes it under.

A record without discovery's tenant is a second object beside discovery's: NetBox's uniqueness on a
VM name is per tenant (virtualization/models/virtualmachines.py, v4.5.0, Meta.constraints), so a
tenant-less and a tenanted record of one name coexist in one cluster. The placement decision and
the POST body run as the playbook's own tasks, with the NetBox answers given as variables. The
fixtures use neutral names and RFC 5737 addresses: this repository is public.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
VALIDATE = PLAYBOOKS / "validate-address-free.yml"
TOKEN = PLAYBOOKS / "provision-netbox-automation-token.yml"

DECIDE_PLACEMENT = "Decide: the tenant and site the record is placed under"
DECIDE_BODY = "Decide: the record body, and whether the declared tenant and site resolve"
JUDGE = "Decide: judge the address and the record placement"
CREATE = "NetBox: create the VM record (planned, in the Proxmox cluster)"

CLUSTER = {"id": 3, "name": "pve", "type": {"name": "Proxmox VE"}}
BASE_BODY = {"name": "svc-vm", "status": "planned", "cluster": 3,
             "description": "Declared by the service deployment workflow (validate-address)"}
OK_TENANT = {"status": 200, "json": {"results": [{"id": 11, "name": "tenant-a"}]}}
OK_SITE = {"status": 200, "json": {"results": [{"id": 12, "name": "site-a"}]}}


def _tasks(path: Path) -> list[dict]:
    return [t for p in playbook_yaml.plays(path) for t in p.get("tasks", [])]


def _named(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


def _run(tmp_path, tasks, variables, expression):
    """Run the playbook's own tasks, then return the JSON value of `expression`."""
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
                "tasks": [*tasks, {"ansible.builtin.debug": {"msg": "PROBE {{ " + expression + " | to_json }}"}}]}]
    path = tmp_path / "placement.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                         text=True, capture_output=True, check=True).stdout
    line = next(ln for ln in out.splitlines() if "PROBE " in ln)
    return json.loads(json.loads(line.split('"msg": ', 1)[1]).split("PROBE ", 1)[1])


def _base(**overrides) -> dict:
    variables = {"_name": "svc-vm", "_vm_records": [], "_clusters": [CLUSTER],
                 "_discovery_tenant_name": "tenant-a", "_discovery_site_name": "site-a",
                 "_nb_tenant": OK_TENANT, "_nb_site": OK_SITE}
    variables.update(overrides)
    return variables


PLACEMENT_TASKS = [_named(VALIDATE, DECIDE_PLACEMENT), _named(VALIDATE, DECIDE_BODY)]


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_the_record_is_placed_under_the_tenant_and_site_discovery_writes(tmp_path):
    got = _run(tmp_path, PLACEMENT_TASKS, _base(), "{'body': _vm_body, 'errors': _placement_errors}")
    assert got == {"body": {**BASE_BODY, "tenant": 11, "site": 12}, "errors": []}


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_an_unset_tenant_and_site_are_omitted_as_before(tmp_path):
    # Discovery names no tenant when its inventory var is unset, so there is nothing to match.
    skipped = {"skipped": True}
    got = _run(tmp_path, PLACEMENT_TASKS,
               _base(_discovery_tenant_name="", _discovery_site_name="", _nb_tenant=skipped, _nb_site=skipped),
               "{'body': _vm_body, 'errors': _placement_errors}")
    assert got == {"body": BASE_BODY, "errors": []}


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
@pytest.mark.parametrize("override,named,absent_key", [
    ({"_nb_tenant": {"status": 200, "json": {"results": []}}}, "tenant-a", "tenant"),
    ({"_nb_tenant": {"status": 200, "json": {"results": [{"id": 11}, {"id": 13}]}}}, "tenant-a", "tenant"),
    ({"_nb_site": {"status": 200, "json": {"results": []}}}, "site-a", "site"),
])
def test_a_declared_tenant_or_site_that_does_not_resolve_refuses_and_is_never_omitted(
        tmp_path, override, named, absent_key):
    # Omitting the unresolved value would create the duplicate this placement exists to prevent:
    # the step must refuse (an _address_errors entry, so no POST runs), and the body must not
    # quietly drop the key.
    judge = _named(VALIDATE, JUDGE)
    variables = _base(_arp_hits=[], _own_vm=False, _ip="192.0.2.10", _vmid=1, **override)
    got = _run(tmp_path, [*PLACEMENT_TASKS, judge], variables,
               "{'body': _vm_body, 'errors': _address_errors}")
    refusals = [e for e in got["errors"] if e.startswith("cannot place the NetBox VM record")]
    assert refusals and named in refusals[0], got
    assert absent_key not in got["body"], got


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_an_existing_record_is_adopted_without_placement_reads(tmp_path):
    # No POST will run, so the tenant and site are not read and cannot refuse the adoption.
    skipped = {"skipped": True}
    existing = [{"id": 7, "cluster": {"id": 3, "name": "pve"}}]
    got = _run(tmp_path, PLACEMENT_TASKS,
               _base(_vm_records=existing, _nb_tenant=skipped, _nb_site=skipped),
               "{'errors': _placement_errors}")
    assert got == {"errors": []}


def test_the_create_posts_the_prepared_body_and_nothing_else():
    body = _named(VALIDATE, CREATE)["ansible.builtin.uri"]["body"]
    assert body == "{{ _vm_body }}", body


def test_the_tenant_and_site_reads_are_read_only_hidden_and_run_under_check():
    for name in ("NetBox: find the tenant discovery places the VM under",
                 "NetBox: find the site discovery places the VM at"):
        task = _named(VALIDATE, name)
        assert task["ansible.builtin.uri"]["url"].startswith("{{ _netbox_url }}/api/"), name
        assert "method" not in task["ansible.builtin.uri"], name
        assert task.get("no_log") is True and task.get("failed_when") is False, name
        assert task.get("check_mode") is False, name


def _profiles() -> dict:
    play = next(p for p in playbook_yaml.plays(TOKEN) if "_profiles" in (p.get("vars") or {}))
    return play["vars"]["_profiles"]


def test_the_vm_recorder_may_view_the_tenant_and_site_it_places_under_and_add_neither():
    profile = _profiles()["vm-recorder"]
    assert profile["actions"] == ["view", "add"]
    assert profile["object_types"] == ["virtualization.virtualmachine"]
    assert set(profile["view_object_types"]) == {"virtualization.cluster", "tenancy.tenant", "dcim.site"}


def test_every_netbox_model_validate_address_reads_is_granted_to_the_vm_recorder():
    # The grant follows the reads: a new read on another model fails here until the profile grants it.
    model_of = {
        "virtualization/virtual-machines": "virtualization.virtualmachine",
        "virtualization/clusters": "virtualization.cluster",
        "tenancy/tenants": "tenancy.tenant",
        "dcim/sites": "dcim.site",
    }
    read = set(re.findall(r"(virtualization/virtual-machines|virtualization/clusters|tenancy/tenants|dcim/sites)",
                          VALIDATE.read_text()))
    profile = _profiles()["vm-recorder"]
    granted = set(profile["view_object_types"]) | set(profile["object_types"])
    assert {model_of[path] for path in read} <= granted, read
