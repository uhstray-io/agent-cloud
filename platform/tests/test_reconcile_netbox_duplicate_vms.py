"""Duplicate VM reconciliation (reconcile-netbox-duplicate-vms.yml, Semaphore "Reconcile NetBox Duplicate VMs").

The reconciliation is a Django-shell script, not an API client, so the tests run its real body:

- static, from the parsed playbook: the extra-var guard is the first play; the play is hosts netbox_svc and
  reads no credential and makes no request; both shell tasks use the provision playbook's exec pattern; the dry
  task is read-only by construction (check_mode false, dry_run true, changed_when false) and the real task runs
  only under `not ansible_check_mode`; the body carries no Jinja markers; the body writes no workflow field and
  saves no VM; the recorder username is the one provision-netbox-automation-token.yml gives vm-recorder; the
  template is a dev variant with one optional string.
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
RECONCILER = "workflow-vm-reconciler"
DISCOVERY = "orb-agent"
# The workflow fields the bare and discovery records hold. The reconciler must leave both records' fields as they are.
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
    for word in ("x-vault-token", "secret/", "approle", "api_token", "authorization", "/v1/", "vm_reconciler", "http"):
        assert word not in dumped, word
    # The only use of the token model is the account guard, which reads the account's state and never a credential.
    assert BODY.count("Token.objects.filter(user=reconciler).exists()") == 1


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


def test_the_body_writes_no_workflow_field_and_saves_no_virtual_machine():
    # Only the collector writes workflow status (spec "Only the collector writes status"). The reconciler deletes the
    # bare record and saves only its own account.
    assert "custom_field_data" not in BODY
    assert BODY.count(".save()") == 1, "only the reconciler account is saved"
    assert "refresh_from_db" not in BODY


def test_the_writes_run_inside_the_change_logging_context_with_a_request_that_carries_the_account():
    with_line = BODY.index("with event_tracking(request):")
    assert BODY.index("request = NetBoxFakeRequest(") < with_line
    for needle in ('"user": reconciler', '"id": uuid.uuid4()', 'e["bare_obj"].delete()'):
        assert needle in BODY, needle
    assert with_line < BODY.index('e["bare_obj"].delete()')


def test_the_account_is_created_only_on_a_real_run_with_writes_and_created_inactive():
    gate = BODY.index("if not missing and not dry_run:")
    account = BODY.index("get_or_create(\n")
    assert gate < account < BODY.index("with event_tracking(request):")
    assert BODY.index("if not writes:") < account
    assert 'defaults={"is_active": False}' in BODY


def test_the_account_is_given_no_grant_and_its_state_is_checked():
    for grant in (
        "groups.add",
        "user_permissions",
        "is_superuser=True",
        "is_staff=True",
        "is_active=True",
        "ObjectPermission.objects.create",
        "ObjectPermission(",
    ):
        assert grant not in BODY, grant
    for check in (
        "reconciler.is_active",
        "reconciler.is_superuser",
        "reconciler.groups.exists()",
        "ObjectPermission.objects.filter(users=reconciler).exists()",
        "reconciler.has_usable_password()",
        "Token.objects.filter(user=reconciler).exists()",
    ):
        assert check in BODY, check
    assert "reconciler.set_unusable_password()" in BODY


def test_each_bare_delete_is_gated_on_its_own_changelog_row_under_this_request():
    assert 'action="delete", user_name=RECONCILER' in BODY
    assert "request_id=request.id" in BODY


def test_the_relations_not_the_counter_caches_decide_the_locked_refusal():
    lock = BODY.index('e["bare_obj"] = VirtualMachine.objects.select_for_update()')
    assert BODY.index('e["bare_obj"].interfaces.exists()', lock) > lock
    assert BODY.index('e["bare_obj"].virtualdisks.exists()', lock) > lock


def test_the_header_states_why_there_is_no_token_how_the_writes_are_recorded_and_what_is_lost():
    header = "\n".join(line for line in PLAYBOOK.read_text().splitlines() if line.startswith("#"))
    for phrase in (
        "no API token",
        "Webhook.secret",
        "restrict()",
        "CHANGELOG_RETENTION",
        "workflow-vm-reconciler",
        "event_tracking",
        "NetBoxFakeRequest",
        "copy_safe_request",
        "event rules",
        "action=create",
        "virtual disk",
        "one transaction",
        "not moved",
        "Semaphore history",
        "accepted",
    ):
        assert phrase in header, phrase
    assert "netbox/netbox/jobs.py" not in header, "the request shape is copy_safe_request's, not the jobs'"


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
            return None if data["cluster_id"] is None else _Named(data["cluster_name"])
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
                       "changed_object_id__in", "changed_object_id", "action", "user_name", "request_id"}
            unknown = set(lookups) - allowed
            if unknown:
                raise AssertionError("unsupported changelog lookup: %s" % sorted(unknown))
            wanted_request = str(lookups["request_id"]) if "request_id" in lookups else None
            rows = [row for row in STATE["changes"]
                    if (wanted_request is None or row.get("request_id") == wanted_request)
                    and row["app_label"] == lookups.get("changed_object_type__app_label", row["app_label"])
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


class Token:
    class objects:
        @staticmethod
        def filter(**lookups):
            # FAKE_ACCOUNT_TOKEN stands in for an account that holds an API token.
            return _Rows(1 if os.environ.get("FAKE_ACCOUNT_TOKEN") else 0)


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
        (root / "users/models.py").write_text("from _fake_orm import ObjectPermission, Token\n")
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


def test_a_qualifying_pair_deletes_the_bare_record_and_leaves_the_discovery_record_untouched(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(), dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert [p["action"] for p in result["pairs"]] == ["deleted"]
    assert [row["id"] for row in after["vms"]] == [11]
    assert after["vms"][0]["custom_field_data"] == RUN_OLD, "the discovery record's fields must not be written"
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
    }
    assert '"pass"' not in proc.stdout, "a workflow value reached the output"


def test_a_discovery_record_keeps_its_own_fields_when_the_bare_record_holds_a_newer_run(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(bare_cf=RUN_NEW, disc_cf=RUN_OLD), dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert after["vms"][0]["custom_field_data"] == RUN_OLD


def test_a_dry_run_reports_the_same_pair_and_writes_nothing(tmp_path):
    before = _pair()
    proc, result, after = _run_body(tmp_path, before, dry_run=True)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True and result["dry_run"] is True
    assert [p["action"] for p in result["pairs"]] == ["would_delete"]
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


def test_a_refused_pair_is_left_alone_while_the_other_pairs_of_the_run_are_deleted(tmp_path):
    before = _merge(
        _pair(name="dns", bare_id=10, disc_id=11, bare_kw={"interfaces": 1}),
        _pair(name="vault", bare_id=20, disc_id=21),
    )
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert {p["name"]: p["action"] for p in result["pairs"]} == {"dns": "refused:has interfaces", "vault": "deleted"}
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


def test_a_pair_with_no_cluster_sorts_beside_a_pair_of_the_same_name_in_a_cluster(tmp_path):
    # Same name, one group with cluster_id None and one with an int: the group sort must not compare None with an int.
    def pair_in(cluster, bare_id, disc_id):
        return {
            "vms": [
                _vm(bare_id, "dns", bare=True, cluster=cluster, cf=RUN_NEW),
                _vm(disc_id, "dns", bare=False, cluster=cluster, cf=RUN_OLD),
            ],
            "changes": _creates(bare_id, ["workflow-vm-recorder"]) + _creates(disc_id, [DISCOVERY]),
        }

    before = _merge(pair_in(None, 10, 11), pair_in(1, 20, 21))
    proc, result, after = _run_body(tmp_path, before, dry_run=True)
    assert proc.returncode == 0, proc.stderr
    assert sorted(p["bare_id"] for p in result["pairs"]) == [10, 20]
    assert {p["action"] for p in result["pairs"]} == {"would_delete"}
    assert {p["cluster"] for p in result["pairs"]} == {"none", "uhstray-cluster"}


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
    # The plan saw no interface; the locked re-read does. Only that pair is refused, and the other pair still deletes.
    before = _merge(_pair(name="dns", bare_id=10, disc_id=11), _pair(name="vault", bare_id=20, disc_id=21))
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_RACE_PK": "10"})
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert {p["name"]: p["action"] for p in result["pairs"]} == {"dns": "refused:has interfaces", "vault": "deleted"}
    assert sorted(row["id"] for row in after["vms"]) == [10, 11, 21]


def test_a_pair_whose_bare_record_stops_matching_mid_run_stops_the_whole_run(tmp_path):
    before = _merge(_pair(name="dns", bare_id=10, disc_id=11), _pair(name="vault", bare_id=20, disc_id=21))
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_RACE_SHAPE": "10"})
    assert proc.returncode != 0, proc.stdout
    assert "a pair changed between the read and the write" in proc.stderr
    assert after == before


def test_a_stale_interface_counter_does_not_let_a_record_with_interfaces_through(tmp_path):
    # The counter reads 0 and the plan deletes the pair; the relation holds an interface row, so the lock refuses it.
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
    assert [p["action"] for p in result["pairs"]] == ["would_delete"]


def test_a_real_run_with_nothing_to_delete_creates_no_reconciler_account(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(), dry_run=False, names=["nope"])
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is False
    assert "users" not in after


def test_the_reconciler_account_is_inactive_unusable_and_its_delete_is_changelogged_under_it(tmp_path):
    proc, result, after = _run_body(tmp_path, _pair(name="dns", bare_id=10, disc_id=11), dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert after["users"] == [
        {"username": RECONCILER, "is_active": False, "is_superuser": False, "usable_password": False}
    ]
    rows = [row for row in after["changes"] if row.get("user_name") == RECONCILER]
    assert [(row["action"], row["changed_object_id"]) for row in rows] == [("delete", 10)]


def test_a_run_whose_changelog_rows_are_missing_rolls_back(tmp_path):
    before = _pair()
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_NO_CHANGELOG": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "no changelog row for a bare record's delete" in proc.stderr
    assert after == before


def test_a_delete_row_from_an_earlier_request_does_not_satisfy_this_runs_gate(tmp_path):
    # A delete row by the same account exists, but under another request id. The gate reads this run's request only.
    before = _pair()
    before["changes"].append(
        {
            "changed_object_id": 10,
            "app_label": "virtualization",
            "model": "virtualmachine",
            "action": "delete",
            "user_name": RECONCILER,
            "request_id": "earlier-request",
        }
    )
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_NO_DELETE_ROW": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "no changelog row for a bare record's delete" in proc.stderr
    assert after == before


def test_a_pre_existing_account_holding_a_usable_password_stops_the_run(tmp_path):
    before = _pair()
    before["users"] = [{"username": RECONCILER, "is_active": False, "is_superuser": False, "usable_password": True}]
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode != 0, proc.stdout
    assert "is active, holds a grant, a usable password or an API token" in proc.stderr
    assert after == before


def test_a_pre_existing_inactive_account_with_no_grant_or_password_is_used_as_it_is(tmp_path):
    before = _pair()
    before["users"] = [{"username": RECONCILER, "is_active": False, "is_superuser": False, "usable_password": False}]
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert result["pairs"][0]["action"] == "deleted"
    assert after["users"] == before["users"]


def test_a_pre_existing_active_account_stops_the_run_even_with_no_grant_or_password(tmp_path):
    before = _pair()
    before["users"] = [{"username": RECONCILER, "is_active": True, "is_superuser": False, "usable_password": False}]
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode != 0, proc.stdout
    assert "is active, holds a grant" in proc.stderr
    assert after == before


def test_a_pre_existing_superuser_account_stops_the_run(tmp_path):
    before = _pair()
    before["users"] = [{"username": RECONCILER, "is_active": False, "is_superuser": True, "usable_password": False}]
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode != 0, proc.stdout
    assert "holds a grant" in proc.stderr
    assert after == before


def test_a_pre_existing_account_holding_an_object_permission_or_a_group_stops_the_run(tmp_path):
    before = _pair()
    before["users"] = [{"username": RECONCILER, "is_active": False, "is_superuser": False, "usable_password": False}]
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_ACCOUNT_GRANTS": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "holds a grant" in proc.stderr
    assert after == before


def test_a_pre_existing_account_holding_an_api_token_stops_the_run(tmp_path):
    before = _pair()
    before["users"] = [{"username": RECONCILER, "is_active": False, "is_superuser": False, "usable_password": False}]
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_ACCOUNT_TOKEN": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "or an API token" in proc.stderr
    assert after == before


def test_a_run_whose_bare_delete_has_no_changelog_row_rolls_back(tmp_path):
    before = _pair()
    proc, result, after = _run_body(tmp_path, before, dry_run=False, env={"FAKE_NO_DELETE_ROW": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "no changelog row for a bare record's delete" in proc.stderr
    assert after == before


def test_the_reconciler_rows_cannot_change_the_origin_of_a_pair(tmp_path):
    # The origin check reads create rows only: the account's rows on both records do not make a pair unverified.
    before = _pair()
    for vm_id in (10, 11):
        before["changes"].append(
            {
                "changed_object_id": vm_id,
                "app_label": "virtualization",
                "model": "virtualmachine",
                "action": "update",
                "user_name": RECONCILER,
                "request_id": "earlier",
            }
        )
    proc, result, after = _run_body(tmp_path, before, dry_run=False)
    assert proc.returncode == 0, proc.stderr
    assert [p["action"] for p in result["pairs"]] == ["deleted"]


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
def test_the_playbook_deletes_a_qualifying_bare_record_through_the_shell(tmp_path):
    proc, out, after = _ansible(tmp_path, _pair())
    assert proc.returncode == 0, out
    assert [row["id"] for row in after["vms"]] == [11]
    assert '"action": "deleted"' in out


@NEEDS_ANSIBLE
def test_check_mode_plans_through_the_shell_with_dry_run_and_changes_nothing(tmp_path):
    before = _pair()
    proc, out, after = _ansible(tmp_path, before, check=True)
    assert proc.returncode == 0, out
    assert after == before
    received = (_stub(tmp_path) / "received.log").read_text()
    assert 'dry_run\\": true' in received and 'dry_run\\": false' not in received
    assert '"action": "would_delete"' in out


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
