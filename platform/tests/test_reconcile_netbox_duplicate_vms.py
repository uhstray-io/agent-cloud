"""Duplicate VM reconciliation (reconcile-netbox-duplicate-vms.yml, Semaphore "Reconcile NetBox Duplicate VMs").

The reconciliation is a Django-shell script, not an API client, so the tests run its real body:

- static, from the parsed playbook: the extra-var guard is the first play; the play is hosts netbox_svc and
  reads no credential and makes no request; both shell tasks use the provision playbook's exec pattern; the dry
  task is read-only by construction (check_mode false, dry_run true, changed_when false) and the real task runs
  only under `not ansible_check_mode`; the body carries no Jinja markers; the recorder username is the one
  provision-netbox-automation-token.yml gives vm-recorder; the template is a dev variant with one optional string.
- through the real playbook under ansible-playbook: a fake container engine stands in for `docker exec`, and a fake
  manage.py runs the script against stub Django modules whose state is a JSON file.
- decisions, through the body alone (a subprocess, no Ansible): one case per guard, with the state read back before
  and after each run.

The stub is test-only. It implements the ORM calls the body makes and fails on any other lookup, so a body that
starts using a new call fails here instead of passing on a silent default. That NetBox v4.5.0 behaves so is read
from its source (see the playbook header), not proven by these tests.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/reconcile-netbox-duplicate-vms.yml"
PROVISION = REPO / "platform/playbooks/provision-netbox-automation-token.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

DOC = yaml.safe_load(PLAYBOOK.read_text())
PLAY = DOC[1]
BODY = PLAY["vars"]["_reconcile_body"]
DRY = "Dry run: plan the reconciliation in the NetBox Django shell (nothing is written)"
REAL = "Reconcile in the NetBox Django shell (one transaction; refused pairs are left untouched)"
MARKER = "Declared by the service deployment workflow (validate-address)"
FIELDS = ["ac_workflow_status", "ac_failed_steps", "ac_last_run"]
DISCOVERY = "orb-agent"
RUN_NEW = {"ac_workflow_status": {"deploy": "pass"}, "ac_failed_steps": "", "ac_last_run": "2026-11-02T10:00:00Z"}
RUN_OLD = {"ac_workflow_status": {"deploy": "fail"}, "ac_failed_steps": "deploy", "ac_last_run": "2026-10-01T10:00:00Z"}
TASK_KEYWORDS = {
    "name",
    "register",
    "when",
    "no_log",
    "vars",
    "delegate_to",
    "loop",
    "loop_control",
    "tags",
    "check_mode",
    "changed_when",
    "failed_when",
    "become",
    "ignore_errors",
    "timeout",
    "args",
    "block",
    "rescue",
    "always",
    "connection",
    "diff",
    "notify",
    "environment",
}
PREFIX = "ansible.builtin."


def _tasks():
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    walk(PLAY["tasks"])
    return out


def _module(task):
    key = next(k for k in task if k not in TASK_KEYWORDS)
    return key[len(PREFIX) :] if key.startswith(PREFIX) else key


def _args(task):
    key = next(k for k in task if k not in TASK_KEYWORDS)
    return task[key]


def _named(name):
    return next(t for t in _tasks() if t.get("name") == name)


# --- static: the guard, the play, the two shell tasks ----------------------------------------------------------


def test_the_extra_var_guard_is_the_first_play():
    assert (
        DOC[0].get("ansible.builtin.import_playbook", DOC[0].get("import_playbook")) == "refuse-internal-extra-vars.yml"
    )
    assert PLAY["hosts"] == "netbox_svc" and PLAY["gather_facts"] is False and PLAY["become"] is False


def test_the_play_reads_no_credential_and_makes_no_request():
    assert not [t for t in _tasks() if _module(t) == "uri"]
    dumped = (json.dumps(PLAY["tasks"]) + BODY).lower()
    for word in (
        "x-vault-token",
        "secret/",
        "approle",
        "api_token",
        "authorization",
        "/v1/",
        "vm_reconciler",
        "token",
        "http",
    ):
        assert word not in dumped, word


def test_both_shells_use_the_provision_exec_pattern():
    assert PLAY["vars"]["_manage"] == playbook_yaml.plays(PROVISION)[0]["vars"]["_manage"]
    for name in (DRY, REAL):
        assert _args(_named(name)).startswith("{{ _manage }} << 'PYSCRIPT'\n"), name


def test_the_dry_run_is_check_mode_only_and_read_only_by_construction():
    dry = _named(DRY)
    assert dry["when"] == "ansible_check_mode"
    assert dry["check_mode"] is False and dry["changed_when"] is False
    assert dry["register"] == "_reconcile_dry"
    assert "'dry_run': true" in _args(dry)


def test_the_real_run_is_skipped_under_check_mode():
    real = _named(REAL)
    assert real["when"] == "not ansible_check_mode"
    assert real["changed_when"] is True and real.get("check_mode") is None
    assert real["register"] == "_reconcile_real"
    assert "'dry_run': false" in _args(real)


def test_the_two_shell_tasks_never_share_a_register_name():
    # A skipped task stores its result under its register name, so a shared name lets the skipped one erase the
    # other's stdout (the dry run's result line was lost that way).
    assert _named(DRY)["register"] != _named(REAL)["register"]


def test_the_visible_output_is_the_names_only_result():
    debugs = [t for t in _tasks() if _module(t) == "debug"]
    assert len(debugs) == 2, [t.get("name") for t in debugs]
    assert _args(debugs[0]) == {"var": "_result"}
    for t in _tasks():
        if t.get("no_log") is True:
            continue
        text = json.dumps({k: v for k, v in t.items() if k != "name"})
        for word in ("custom_field", "vcpus", "comments", "primary_ip", ".json"):
            assert word not in text, (t.get("name"), word)


def test_the_body_has_no_jinja_markers_and_compiles_with_its_prelude():
    for marker in ("{{", "{%", "{#", "PYSCRIPT"):
        assert marker not in BODY, marker
    compile("import json\nPARAMS = {'dry_run': True, 'names': []}\n" + BODY, "reconcile-body", "exec")


def test_the_recorder_is_the_user_the_provision_playbook_gives_vm_recorder():
    recorder = playbook_yaml.plays(PROVISION)[0]["vars"]["_profiles"]["vm-recorder"]["user"]
    assert re.findall(r'^RECORDER = "([^"]+)"$', BODY, re.M) == [recorder]


def test_the_changelog_is_read_for_creates_of_virtual_machines_only():
    for needle in (
        'changed_object_type__app_label="virtualization"',
        'changed_object_type__model="virtualmachine"',
        'action="create"',
    ):
        assert needle in BODY, needle


def test_the_writes_run_inside_the_change_logging_context_with_a_request_that_carries_the_account():
    with_line = BODY.index("with event_tracking(request):")
    assert BODY.index("request = NetBoxFakeRequest(") < with_line
    for needle in ('"user": reconciler', '"id": uuid.uuid4()', "disc_obj.save()", 'e["bare_obj"].delete()'):
        assert needle in BODY, needle
    assert with_line < BODY.index("disc_obj.save()") < BODY.index('e["bare_obj"].delete()')


def test_the_account_is_created_only_on_a_real_run_with_writes():
    gate = BODY.index("if not missing and not dry_run:")
    account = BODY.index("get_or_create(\n")
    assert gate < account < BODY.index("with event_tracking(request):")
    assert BODY.index("if not writes:") < account


def test_the_account_is_given_no_grant_and_its_state_is_checked():
    for grant in (
        "groups.add",
        "user_permissions",
        "is_superuser=True",
        "is_staff=True",
        "ObjectPermission.objects.create",
        "ObjectPermission(",
    ):
        assert grant not in BODY, grant
    for check in (
        "reconciler.is_superuser",
        "reconciler.groups.exists()",
        "ObjectPermission.objects.filter(users=reconciler).exists()",
        "reconciler.has_usable_password()",
    ):
        assert check in BODY, check
    assert "reconciler.set_unusable_password()" in BODY


def test_each_discovery_update_and_bare_delete_is_gated_on_its_own_changelog_row():
    assert 'action="update", user_name=RECONCILER' in BODY
    assert 'action="delete", user_name=RECONCILER' in BODY


def test_the_relations_not_the_counter_caches_decide_the_locked_refusal():
    lock = BODY.index('e["bare_obj"] = VirtualMachine.objects.select_for_update()')
    assert BODY.index('e["bare_obj"].interfaces.exists()', lock) > lock
    assert BODY.index('e["bare_obj"].virtualdisks.exists()', lock) > lock


def test_the_header_states_why_there_is_no_token_and_how_the_writes_are_recorded():
    header = "\n".join(line for line in PLAYBOOK.read_text().splitlines() if line.startswith("#"))
    for phrase in (
        "no API token",
        "Webhook.secret",
        "restrict()",
        "CHANGELOG_RETENTION",
        "workflow-vm-reconciler",
        "event_tracking",
        "NetBoxFakeRequest",
        "event rules",
        "action=create",
        "virtual disk",
        "one transaction",
    ):
        assert phrase in header, phrase


# --- static: the template and the catalog --------------------------------------------------------------------


def test_the_template_is_a_dev_variant_with_one_optional_string_survey_field():
    entries = [
        e
        for e in yaml.safe_load(TEMPLATES.read_text())["templates"]
        if e.get("name") == "Reconcile NetBox Duplicate VMs"
    ]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["playbook"] == "platform/playbooks/reconcile-netbox-duplicate-vms.yml"
    assert entry["dev_variant"] is True
    assert entry["survey_vars"] == [
        {
            "name": "reconcile_names",
            "title": "VM names to reconcile",
            "description": 'A JSON list of VM names, for example ["dns"]. Blank = every qualifying duplicate pair.',
            "type": "string",
            "required": False,
            "default_value": "",
        }
    ]


def test_the_template_catalog_offers_no_token_profile_for_the_reconciler():
    assert "vm-reconciler" not in TEMPLATES.read_text()


# --- the stub: a small stand-in for the Django ORM calls the body makes -----------------------------------------

FAKE_ORM = """
import atexit
import copy
import json
import os
from contextlib import contextmanager

_PATH = os.environ["FAKE_NETBOX_STATE"]
with open(_PATH) as fh:
    STATE = json.load(fh)
_SNAPSHOTS = []
_ROLLBACK = []
CURRENT = {"request": None}
_VM_FIELDS = ("id", "name", "cluster_id", "description", "vcpus", "comments", "interface_count",
              "primary_ip4_id", "primary_ip6_id", "custom_field_data")


def _persist():
    with open(_PATH, "w") as fh:
        json.dump(STATE, fh, sort_keys=True)


atexit.register(_persist)


def _record(action, vm_id):
    # NetBox records a change only inside a request (netbox/core/signals.py), so with no request set here, none is kept.
    request = CURRENT["request"]
    if request is None or os.environ.get("FAKE_NO_CHANGELOG"):
        return
    if action == "delete" and os.environ.get("FAKE_NO_DELETE_ROW"):
        return
    STATE["changes"].append({"changed_object_id": vm_id, "app_label": "virtualization", "model": "virtualmachine",
                             "action": action, "user_name": request.user.username, "request_id": str(request.id)})


def _only_pk(lookups):
    unknown = set(lookups) - {"pk"}
    if unknown:
        raise AssertionError("unsupported VM lookup: %s" % sorted(unknown))
    return lookups["pk"]


class _Named:
    def __init__(self, name):
        self.name = name


class VirtualMachine:
    def __init__(self, data):
        object.__setattr__(self, "_data", data)

    def __getattr__(self, key):
        data = object.__getattribute__(self, "_data")
        if key == "cluster":
            return _Named(data["cluster_name"])
        if key == "tenant":
            return None if data["tenant_name"] is None else _Named(data["tenant_name"])
        if key == "created":
            return data["created"]
        if key == "interfaces":
            return _Rows(data.get("real_interfaces", 0))
        if key == "virtualdisks":
            return _Rows(data.get("real_virtualdisks", 0))
        if key == "virtual_disk_count":
            return data.get("virtual_disk_count", 0)
        if key in _VM_FIELDS:
            return data[key]
        raise AttributeError(key)

    def __setattr__(self, key, value):
        if key != "custom_field_data":
            raise AttributeError(key)
        self._data["custom_field_data"] = value

    def save(self):
        for index, row in enumerate(STATE["vms"]):
            if row["id"] == self._data["id"]:
                new = copy.deepcopy(self._data)
                if os.environ.get("FAKE_DROP_SAVE"):
                    new["custom_field_data"] = copy.deepcopy(row["custom_field_data"])
                STATE["vms"][index] = new
                _record("update", new["id"])
                return
        raise RuntimeError("save of a missing VM")

    def refresh_from_db(self):
        for row in STATE["vms"]:
            if row["id"] == self._data["id"]:
                object.__setattr__(self, "_data", copy.deepcopy(row))
                return
        raise RuntimeError("refresh of a missing VM")

    def delete(self):
        kept = [row for row in STATE["vms"] if row["id"] != self._data["id"]]
        if len(kept) == len(STATE["vms"]):
            raise RuntimeError("delete of a missing VM")
        _record("delete", self._data["id"])
        STATE["vms"] = kept


class _VMQuery:
    def __init__(self, rows, lock=False):
        self._rows = rows
        self._lock = lock

    def __iter__(self):
        return iter([VirtualMachine(copy.deepcopy(row)) for row in self._rows])

    def exists(self):
        return bool(self._rows)

    def get(self, **lookups):
        pk = _only_pk(lookups)
        if len([row for row in self._rows if row["id"] == pk]) != 1:
            raise RuntimeError("get expected one VM")
        if self._lock and os.environ.get("FAKE_RACE_PK") == str(pk):
            # A concurrent change between the read and the locked re-read: the bare record gains an interface.
            for row in STATE["vms"]:
                if row["id"] == pk:
                    row["interface_count"] = 1
                    row["real_interfaces"] = 1
        if self._lock and os.environ.get("FAKE_RACE_SHAPE") == str(pk):
            # A concurrent change that makes the bare record stop matching the bare shape.
            for row in STATE["vms"]:
                if row["id"] == pk:
                    row["description"] = ""
        found = [row for row in STATE["vms"] if row["id"] == pk]
        return VirtualMachine(copy.deepcopy(found[0]))


class _VMManager:
    def select_related(self, *names):
        return _VMQuery(list(STATE["vms"]))

    def select_for_update(self):
        return _VMQuery(list(STATE["vms"]), lock=True)

    def filter(self, **lookups):
        pk = _only_pk(lookups)
        return _VMQuery([row for row in STATE["vms"] if row["id"] == pk])

    def get(self, **lookups):
        return _VMQuery(list(STATE["vms"])).get(**lookups)


VirtualMachine.objects = _VMManager()


class _Change:
    def __init__(self, row):
        self.changed_object_id = row["changed_object_id"]
        self.user_name = row["user_name"]


class _ChangeQuery:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter([_Change(row) for row in self._rows])

    def exists(self):
        return bool(self._rows)


class ObjectChange:
    class objects:
        @staticmethod
        def filter(**lookups):
            allowed = {"changed_object_type__app_label", "changed_object_type__model",
                       "changed_object_id__in", "changed_object_id", "action", "user_name"}
            unknown = set(lookups) - allowed
            if unknown:
                raise AssertionError("unsupported changelog lookup: %s" % sorted(unknown))
            rows = [row for row in STATE["changes"]
                    if row["app_label"] == lookups.get("changed_object_type__app_label", row["app_label"])
                    and row["model"] == lookups.get("changed_object_type__model", row["model"])
                    and row["action"] == lookups.get("action", row["action"])
                    and row["user_name"] == lookups.get("user_name", row["user_name"])
                    and row["changed_object_id"] == lookups.get("changed_object_id", row["changed_object_id"])
                    and row["changed_object_id"] in lookups.get("changed_object_id__in", [row["changed_object_id"]])]
            return _ChangeQuery(rows)


class _Atomic:
    def __enter__(self):
        _SNAPSHOTS.append(copy.deepcopy(STATE))
        _ROLLBACK.append(False)
        return self

    def __exit__(self, exc_type, exc, tb):
        snapshot = _SNAPSHOTS.pop()
        rollback = _ROLLBACK.pop()
        if exc_type is not None or rollback:
            STATE.clear()
            STATE.update(snapshot)
        return False


class _Transaction:
    def atomic(self):
        return _Atomic()

    def set_rollback(self, flag):
        _ROLLBACK[-1] = bool(flag)


transaction = _Transaction()


class _NoRows:
    # The stub holds no grants and no group memberships, so these queries are empty.
    def exists(self):
        return False


class _Rows:
    def __init__(self, count):
        self._count = count

    def exists(self):
        return self._count > 0


class ObjectPermission:
    class objects:
        @staticmethod
        def filter(**lookups):
            # FAKE_ACCOUNT_GRANTS stands in for an account that holds an object permission.
            return _Rows(1 if os.environ.get("FAKE_ACCOUNT_GRANTS") else 0)


class NetBoxFakeRequest:
    def __init__(self, _dict):
        self.__dict__ = _dict


@contextmanager
def event_tracking(request):
    # As in netbox/netbox/context_managers.py: no try/finally, so an exception leaves the request set.
    CURRENT["request"] = request
    yield
    CURRENT["request"] = None


class _User:
    def __init__(self, data):
        object.__setattr__(self, "_data", data)

    def __getattr__(self, key):
        data = object.__getattribute__(self, "_data")
        if key in ("username", "is_active", "usable_password", "is_superuser"):
            return data.get(key, False)
        if key == "groups":
            return _Rows(1 if os.environ.get("FAKE_ACCOUNT_GRANTS") else 0)
        raise AttributeError(key)

    def has_usable_password(self):
        return self._data["usable_password"]

    def set_unusable_password(self):
        self._data["usable_password"] = False

    def save(self):
        users = STATE.setdefault("users", [])
        for index, row in enumerate(users):
            if row["username"] == self._data["username"]:
                users[index] = copy.deepcopy(self._data)
                return
        users.append(copy.deepcopy(self._data))


class _UserManager:
    def get_or_create(self, username, defaults=None):
        users = STATE.setdefault("users", [])
        for row in users:
            if row["username"] == username:
                return _User(copy.deepcopy(row)), False
        data = {"username": username, "is_active": True, "is_superuser": False, "usable_password": True}
        data.update(defaults or {})
        users.append(copy.deepcopy(data))
        return _User(data), True


class _UserModel:
    objects = _UserManager()


def get_user_model():
    return _UserModel
"""

SHIM = """
import os
import sys

_script = sys.stdin.read()
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "received.log"), "a") as fh:
    fh.write(_script + "\\n<<END>>\\n")
exec(compile(_script, "<shell>", "exec"), {"__name__": "__main__"})
"""


def _stub(tmp_path):
    root = tmp_path / "stub"
    if not root.exists():
        (root / "django/db").mkdir(parents=True)
        (root / "core").mkdir()
        (root / "virtualization").mkdir()
        (root / "django/__init__.py").write_text("")
        (root / "django/db/__init__.py").write_text("from _fake_orm import transaction\n")
        (root / "core/__init__.py").write_text("")
        (root / "core/models.py").write_text("from _fake_orm import ObjectChange\n")
        (root / "virtualization/__init__.py").write_text("")
        (root / "virtualization/models.py").write_text("from _fake_orm import VirtualMachine\n")
        (root / "netbox").mkdir()
        (root / "utilities").mkdir()
        (root / "users").mkdir()
        (root / "django/contrib/auth").mkdir(parents=True)
        (root / "django/contrib/__init__.py").write_text("")
        (root / "django/contrib/auth/__init__.py").write_text("from _fake_orm import get_user_model\n")
        (root / "netbox/__init__.py").write_text("")
        (root / "netbox/context_managers.py").write_text("from _fake_orm import event_tracking\n")
        (root / "utilities/__init__.py").write_text("")
        (root / "utilities/request.py").write_text("from _fake_orm import NetBoxFakeRequest\n")
        (root / "users/__init__.py").write_text("")
        (root / "users/models.py").write_text("from _fake_orm import ObjectPermission\n")
        (root / "_fake_orm.py").write_text(FAKE_ORM)
        (root / "manage.py").write_text(SHIM)
    return root


def _vm(
    vm_id,
    name,
    *,
    bare,
    cluster=1,
    cf=None,
    interfaces=0,
    ip4=None,
    ip6=None,
    tenant="uhstray",
    vdisks=0,
    real_interfaces=0,
    real_vdisks=0,
):
    # interfaces and vdisks are the counter caches the plan reads; real_interfaces and real_vdisks are the rows the
    # relations return under the lock. A stale counter is a counter that differs from its rows.
    row = {
        "id": vm_id,
        "name": name,
        "cluster_id": cluster,
        "cluster_name": "uhstray-cluster",
        "tenant_name": tenant,
        "created": "2026-11-01 10:00:00+00:00",
        "interface_count": interfaces,
        "virtual_disk_count": vdisks,
        "real_interfaces": real_interfaces,
        "real_virtualdisks": real_vdisks,
        "primary_ip4_id": ip4,
        "primary_ip6_id": ip6,
        "custom_field_data": dict(cf or {}),
    }
    if bare:
        row.update(description=MARKER, vcpus=None, comments="")
    else:
        row.update(description="", vcpus="2.00", comments="VMID: 105")
    return row


def _creates(vm_id, users):
    return [
        {
            "changed_object_id": vm_id,
            "app_label": "virtualization",
            "model": "virtualmachine",
            "action": "create",
            "user_name": user,
        }
        for user in users
    ]


def _pair(
    name="dns",
    bare_id=10,
    disc_id=11,
    *,
    bare_creators=("workflow-vm-recorder",),
    disc_creators=(DISCOVERY,),
    bare_cf=RUN_NEW,
    disc_cf=RUN_OLD,
    bare_kw=None,
    disc_kw=None,
):
    bare_kw = {"interfaces": 0, **(bare_kw or {})}
    return {
        "vms": [
            _vm(
                bare_id,
                name,
                bare=True,
                cf=bare_cf,
                interfaces=bare_kw["interfaces"],
                ip4=bare_kw.get("ip4"),
                ip6=bare_kw.get("ip6"),
                vdisks=bare_kw.get("vdisks", 0),
                real_interfaces=bare_kw.get("real_interfaces", 0),
                real_vdisks=bare_kw.get("real_vdisks", 0),
            ),
            _vm(disc_id, name, bare=False, cf=disc_cf, **(disc_kw or {})),
        ],
        "changes": _creates(bare_id, bare_creators) + _creates(disc_id, disc_creators),
    }


def _merge(*states):
    return {"vms": [vm for s in states for vm in s["vms"]], "changes": [c for s in states for c in s["changes"]]}


def _body_script(dry_run, names=()):
    params = json.dumps(json.dumps({"dry_run": dry_run, "names": list(names)}))
    return f"import json\nPARAMS = json.loads({params})\n{BODY}"


def _run_body(tmp_path, state, *, dry_run, names=(), env=None):
    root = _stub(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state, sort_keys=True))
    run_env = {"PATH": os.environ.get("PATH", ""), "FAKE_NETBOX_STATE": str(state_file), **(env or {})}
    proc = subprocess.run(
        [sys.executable, str(root / "manage.py")],
        input=_body_script(dry_run, names),
        cwd=tmp_path,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RECONCILE_RESULT ")]
    result = json.loads(lines[0][len("RECONCILE_RESULT ") :]) if len(lines) == 1 else None
    return proc, result, json.loads(state_file.read_text())


# --- decisions: the body run against the stub ---------------------------------------------------------------


def test_a_qualifying_pair_merges_and_its_bare_record_is_deleted(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(), dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert [p["action"] for p in result["pairs"]] == ["merged"]
    assert result["pairs"][0]["moved"] == FIELDS
    assert [row["id"] for row in after["vms"]] == [11]
    assert {f: after["vms"][0]["custom_field_data"][f] for f in FIELDS} == RUN_NEW
    assert set(result["pairs"][0]) == {
        "name",
        "cluster",
        "bare_id",
        "bare_tenant",
        "bare_created",
        "disc_id",
        "disc_tenant",
        "disc_created",
        "action",
        "moved",
    }
    assert '"pass"' not in proc.stdout, "a workflow value reached the output"


def test_a_dry_run_reports_the_same_pair_and_writes_nothing(tmp_path):
    before = _pair()
    proc, result, after = _run_body(tmp_path, before, dry_run=True)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True and result["dry_run"] is True
    assert [p["action"] for p in result["pairs"]] == ["would_merge"]
    assert after == before


@pytest.mark.parametrize(
    "bare_kw, reason",
    [
        ({"interfaces": 2}, "has interfaces"),
        ({"ip4": 7}, "has a primary IP"),
        ({"ip6": 9}, "has a primary IP"),
        ({"vdisks": 1}, "has virtual disks"),
    ],
    ids=["interfaces", "primary-ipv4", "primary-ipv6", "virtual-disk"],
)
def test_a_bare_record_with_an_interface_or_an_address_is_refused_and_left_alone(tmp_path, bare_kw, reason):
    before = _pair(bare_kw=bare_kw)
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert [p["action"] for p in result["pairs"]] == ["refused:" + reason]
    assert after == before


def test_a_refused_pair_is_left_alone_while_the_other_pairs_of_the_run_are_reconciled(tmp_path):
    before = _merge(
        _pair(name="dns", bare_id=10, disc_id=11, bare_kw={"interfaces": 1}),
        _pair(name="vault", bare_id=20, disc_id=21),
    )
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert {p["name"]: p["action"] for p in result["pairs"]} == {"dns": "refused:has interfaces", "vault": "merged"}
    assert sorted(row["id"] for row in after["vms"]) == [10, 11, 21]


@pytest.mark.parametrize(
    "bare_creators, disc_creators",
    [
        (["console"], [DISCOVERY]),
        (["workflow-vm-recorder", "workflow-vm-recorder"], [DISCOVERY]),
        ([], [DISCOVERY]),
        (["workflow-vm-recorder"], ["workflow-vm-recorder"]),
        (["workflow-vm-recorder"], []),
    ],
    ids=[
        "bare-by-another-account",
        "bare-with-two-creates",
        "bare-creation-entry-pruned",
        "discovery-by-recorder",
        "discovery-creation-entry-pruned",
    ],
)
def test_an_unconfirmed_origin_is_reported_and_never_touched(tmp_path, bare_creators, disc_creators):
    before = _pair(bare_creators=bare_creators, disc_creators=disc_creators)
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert [p["action"] for p in result["pairs"]] == ["unverified_origin"]
    assert after == before


def test_a_group_of_three_records_is_ambiguous_and_untouched(tmp_path):
    before = _merge(_pair(), {"vms": [_vm(12, "dns", bare=False)], "changes": _creates(12, [DISCOVERY])})
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True and result["pairs"] == []
    assert result["ambiguous"] == [{"name": "dns", "cluster": "uhstray-cluster", "count": 3}]
    assert after == before


def test_two_bare_records_are_ambiguous_and_untouched(tmp_path):
    before = {"vms": [_vm(10, "dns", bare=True), _vm(11, "dns", bare=True)], "changes": []}
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ambiguous"] == [{"name": "dns", "cluster": "uhstray-cluster", "count": 2}]
    assert after == before


def test_the_workflow_fields_move_as_one_unit_from_a_newer_bare_run(tmp_path):
    # Discovery's failed-steps value is "deploy"; the newer bare run's is empty. The unit replaces all three.
    proc, result, after = _run_body(tmp_path, _pair(bare_cf=RUN_NEW, disc_cf=RUN_OLD), dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert after["vms"][0]["custom_field_data"]["ac_failed_steps"] == ""


def test_a_bare_run_older_than_discovery_moves_nothing(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(bare_cf=RUN_OLD, disc_cf=RUN_NEW), dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["pairs"][0]["action"] == "merged" and result["pairs"][0]["moved"] == []
    assert after["vms"][0]["custom_field_data"] == RUN_NEW


def test_a_bare_run_moves_when_discovery_has_no_run_stamp(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(bare_cf=RUN_OLD, disc_cf={}), dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["pairs"][0]["moved"] == FIELDS
    assert after["vms"][0]["custom_field_data"] == RUN_OLD


def test_a_bare_run_without_a_stamp_moves_nothing(tmp_path):
    before = _pair(bare_cf={"ac_workflow_status": {"deploy": "pass"}}, disc_cf=RUN_OLD)
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["pairs"][0]["moved"] == []
    assert after["vms"][0]["custom_field_data"] == RUN_OLD


def test_run_stamps_compare_as_instants_not_as_text(tmp_path):
    # 10:00+02:00 is 08:00Z, earlier than 09:00Z. A text comparison would call the bare run newer.
    before = _pair(
        bare_cf={**RUN_NEW, "ac_last_run": "2026-11-02T10:00:00+02:00"},
        disc_cf={**RUN_OLD, "ac_last_run": "2026-11-02T09:00:00Z"},
    )
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["pairs"][0]["moved"] == []
    assert after["vms"][0]["custom_field_data"]["ac_last_run"] == "2026-11-02T09:00:00Z"


def test_an_unreadable_run_stamp_is_refused_and_left_alone(tmp_path):
    before = _pair(bare_cf={"ac_last_run": "yesterday afternoon"}, disc_cf=RUN_OLD)
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert [p["action"] for p in result["pairs"]] == ["refused:unparseable_last_run"]
    assert result["ok"] is False
    assert after == before


def test_a_requested_name_selects_only_its_pair_and_matches_case_insensitively(tmp_path):
    before = _merge(_pair(name="dns", bare_id=10, disc_id=11), _pair(name="vault", bare_id=20, disc_id=21))
    proc, result, after = _run_body(tmp_path, before, dry_run=False, names=["DNS"])
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert [p["name"] for p in result["pairs"]] == ["dns"]
    assert sorted(row["id"] for row in after["vms"]) == [11, 20, 21]


def test_a_requested_name_with_no_verified_pair_stops_the_run_before_any_write(tmp_path):
    before = _pair()
    proc, result, after = _run_body(tmp_path, before, dry_run=False, names=["nope"])
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert result["errors"] == ["names no verified duplicate pair: nope"]
    assert after == before


def test_a_requested_name_whose_origin_is_unverified_stops_the_whole_run(tmp_path):
    before = _merge(
        _pair(name="dns", bare_id=10, disc_id=11, bare_creators=["console"]),
        _pair(name="vault", bare_id=20, disc_id=21),
    )
    proc, result, after = _run_body(tmp_path, before, dry_run=False, names=["dns", "vault"])
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert result["errors"] == ["names no verified duplicate pair: dns"]
    assert after == before


def test_a_bare_record_that_gains_an_interface_after_the_read_is_refused_at_the_lock(tmp_path):
    # The plan saw no interface; the locked re-read does. Only that pair is refused, and the other pair still merges.
    before = _merge(_pair(name="dns", bare_id=10, disc_id=11), _pair(name="vault", bare_id=20, disc_id=21))
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_RACE_PK": "10"})
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert {p["name"]: p["action"] for p in result["pairs"]} == {"dns": "refused:has interfaces", "vault": "merged"}
    assert sorted(row["id"] for row in after["vms"]) == [10, 11, 21]


def test_a_pair_whose_bare_record_stops_matching_mid_run_stops_the_whole_run(tmp_path):
    before = _merge(_pair(name="dns", bare_id=10, disc_id=11), _pair(name="vault", bare_id=20, disc_id=21))
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_RACE_SHAPE": "10"})
    assert proc.returncode != 0, proc.stdout
    assert "a pair changed between the read and the write" in proc.stderr
    assert after == before


def test_a_stale_interface_counter_does_not_let_a_record_with_interfaces_through(tmp_path):
    # The counter reads 0 and the plan merges the pair; the relation holds an interface row, so the lock refuses it.
    before = _pair(bare_kw={"real_interfaces": 1})
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert [p["action"] for p in result["pairs"]] == ["refused:has interfaces"]
    assert after == before


def test_a_virtual_disk_row_with_a_stale_counter_is_refused_at_the_lock(tmp_path):
    before = _pair(bare_kw={"real_vdisks": 1})
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert [p["action"] for p in result["pairs"]] == ["refused:has virtual disks"]
    assert after == before


def test_a_dry_run_creates_no_reconciler_account(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(), dry_run=True)
    assert proc.returncode == 0, proc.stderr
    assert "users" not in after and "changes" in after
    assert [p["action"] for p in result["pairs"]] == ["would_merge"]


def test_a_real_run_with_nothing_to_merge_creates_no_reconciler_account(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(), dry_run=False, names=["nope"])
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert "users" not in after


def test_the_reconciler_account_is_unusable_and_its_writes_are_changelogged_under_it(tmp_path):
    proc, result, after = _run_body(tmp_path, _merge(_pair(name="dns", bare_id=10, disc_id=11)), dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert after["users"] == [
        {"username": "workflow-vm-reconciler", "is_active": True, "is_superuser": False, "usable_password": False}
    ]
    rows = [row for row in after["changes"] if row.get("user_name") == "workflow-vm-reconciler"]
    assert sorted((row["action"], row["changed_object_id"]) for row in rows) == [("delete", 10), ("update", 11)]
    assert len({row["request_id"] for row in rows}) == 1, "the writes of one run must share one request"


def test_a_run_whose_changelog_rows_are_missing_rolls_back(tmp_path):
    before = _pair()
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_NO_CHANGELOG": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "no changelog row for a discovery record's update" in proc.stderr
    assert after == before


def test_a_pre_existing_account_holding_a_usable_password_stops_the_run(tmp_path):
    before = _pair()
    before["users"] = [
        {"username": "workflow-vm-reconciler", "is_active": True, "is_superuser": False, "usable_password": True}
    ]
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode != 0, proc.stdout
    assert "holds a grant or a usable password" in proc.stderr
    assert after == before


def test_a_pre_existing_account_with_no_usable_password_is_used_as_it_is(tmp_path):
    before = _pair()
    before["users"] = [
        {"username": "workflow-vm-reconciler", "is_active": True, "is_superuser": False, "usable_password": False}
    ]
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["pairs"][0]["action"] == "merged"
    assert after["users"] == before["users"]


def test_a_pre_existing_superuser_account_stops_the_run(tmp_path):
    before = _pair()
    before["users"] = [
        {"username": "workflow-vm-reconciler", "is_active": True, "is_superuser": True, "usable_password": False}
    ]
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode != 0, proc.stdout
    assert "holds a grant or a usable password" in proc.stderr
    assert after == before


def test_a_pre_existing_account_holding_an_object_permission_or_a_group_stops_the_run(tmp_path):
    before = _pair()
    before["users"] = [
        {"username": "workflow-vm-reconciler", "is_active": True, "is_superuser": False, "usable_password": False}
    ]
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_ACCOUNT_GRANTS": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "holds a grant or a usable password" in proc.stderr
    assert after == before


def test_a_run_whose_bare_delete_has_no_changelog_row_rolls_back(tmp_path):
    before = _pair()
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_NO_DELETE_ROW": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "no changelog row for a bare record's delete" in proc.stderr
    assert after == before


def test_the_reconciler_rows_cannot_change_the_origin_of_a_pair(tmp_path):
    # The origin check reads create rows only: the account's update rows on both records do not make a pair unverified.
    before = _pair()
    for vm_id in (10, 11):
        before["changes"].append(
            {
                "changed_object_id": vm_id,
                "app_label": "virtualization",
                "model": "virtualmachine",
                "action": "update",
                "user_name": "workflow-vm-reconciler",
                "request_id": "earlier",
            }
        )
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert [p["action"] for p in result["pairs"]] == ["merged"]


def test_a_discovery_write_that_does_not_read_back_deletes_nothing(tmp_path):
    before = _pair()
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_DROP_SAVE": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "did not hold the moved fields" in proc.stderr
    assert after == before


# --- through the real playbook: ansible-playbook, a fake container engine, the same stub ---------------------


def _ansible(tmp_path, state, *, check=False, extra=None):
    root = _stub(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state, sort_keys=True))
    engine = tmp_path / "fake-engine"
    engine.write_text(
        f'#!/bin/sh\n[ "$1" = exec ] || exit 2\n'
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(root))}/manage.py shell\n"
    )
    engine.chmod(0o755)
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("[netbox_svc]\nnb ansible_connection=local\n")
    cmd = [
        "ansible-playbook",
        "-i",
        str(inventory),
        str(PLAYBOOK),
        "-e",
        json.dumps({"container_engine": str(engine), **(extra or {})}),
    ]
    if check:
        cmd.append("--check")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    env["FAKE_NETBOX_STATE"] = str(state_file)
    proc = subprocess.run(cmd, cwd=REPO, env=env, text=True, capture_output=True, check=False, timeout=300)
    return proc, proc.stdout + proc.stderr, json.loads(state_file.read_text())


@NEEDS_ANSIBLE
def test_the_playbook_reconciles_a_qualifying_pair_through_the_shell(tmp_path):
    proc, out, after = _ansible(tmp_path, _pair())
    assert proc.returncode == 0, out
    assert [row["id"] for row in after["vms"]] == [11]
    assert '"action": "merged"' in out


@NEEDS_ANSIBLE
def test_check_mode_plans_through_the_shell_with_dry_run_and_changes_nothing(tmp_path):
    before = _pair()
    proc, out, after = _ansible(tmp_path, before, check=True)
    assert proc.returncode == 0, out
    assert after == before
    received = (_stub(tmp_path) / "received.log").read_text()
    assert 'dry_run\\": true' in received and 'dry_run\\": false' not in received
    assert '"action": "would_merge"' in out


@NEEDS_ANSIBLE
def test_a_refused_pair_fails_the_real_run_and_changes_nothing(tmp_path):
    before = _pair(bare_kw={"interfaces": 1})
    proc, out, after = _ansible(tmp_path, before)
    assert proc.returncode != 0, out
    assert "not every selected pair was reconciled" in out and "has interfaces" in out
    assert after == before


@NEEDS_ANSIBLE
@pytest.mark.parametrize(
    "extra", [{"reconcile_names": ["dns"]}, {"reconcile_names": '["dns"]'}], ids=["list", "survey-string"]
)
def test_reconcile_names_limits_the_real_run(tmp_path, extra):
    before = _merge(_pair(name="dns", bare_id=10, disc_id=11), _pair(name="vault", bare_id=20, disc_id=21))
    proc, out, after = _ansible(tmp_path, before, extra=extra)
    assert proc.returncode == 0, out
    assert sorted(row["id"] for row in after["vms"]) == [11, 20, 21]


@NEEDS_ANSIBLE
def test_a_names_value_that_is_not_a_list_stops_before_the_shell_runs(tmp_path):
    before = _pair()
    proc, out, after = _ansible(tmp_path, before, extra={"reconcile_names": '"dns"'})
    assert proc.returncode != 0, out
    assert "reconcile_names must be a JSON list" in out
    assert not (_stub(tmp_path) / "received.log").exists()
    assert after == before
