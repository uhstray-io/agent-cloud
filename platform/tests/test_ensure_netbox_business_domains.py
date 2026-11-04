"""Business-domain tags (Ensure NetBox Business Domains, plan 17 D37): ensure-netbox-business-domains.yml.

The playbook runs its body (the play's _ensure_body) in the NetBox Django shell. These tests run that real body:

- static, from the parsed playbook: the extra-var guard and the preflight come first; the play is hosts netbox_svc,
  reads no credential and makes no request; both shell tasks use the provision playbook's exec pattern; the dry run is
  check-mode-only and read-only by construction; the real run is skipped under check mode; the body never deletes; its
  account is created inactive with no grant and its guard is the reconciler's guard code.
- decisions, through the body alone (a subprocess, no Ansible): the slug, create, update, no-op, an undeclared tag
  reported and never deleted, check mode writing nothing, the name-clash and slug refusals, the account guard, the
  changelog gate and the read-back gate.
- through the real playbook under ansible-playbook: a fake container engine stands in for `docker exec`, and a fake
  manage.py runs the body against the same stub.

The stub is test-only. It implements the ORM calls the body makes and fails on any other lookup. What NetBox v4.5.0 does
with them is read from its source, not proven here: extras/models/tags.py (Tag is TagBase + ChangeLoggedModel; owner is
nullable), and django-taggit 6.1.0 (TagBase.name and TagBase.slug are unique).
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
PLAYBOOK = REPO / "platform/playbooks/ensure-netbox-business-domains.yml"
PROVISION = REPO / "platform/playbooks/provision-netbox-automation-token.yml"
RECONCILE = REPO / "platform/playbooks/reconcile-netbox-duplicate-vms.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

DOC = yaml.safe_load(PLAYBOOK.read_text())
PLAY = DOC[2]
BODY = PLAY["vars"]["_ensure_body"]
DRY = "Dry run: plan the business-domain tags in the NetBox Django shell (nothing is written)"
REAL = "Ensure the business-domain tags in the NetBox Django shell (one transaction)"
ACCOUNT = "workflow-domain-tagger"
DESCRIPTION = "Business domain (Dispatch Console, plan 17 D37)"
COLOR = "607d8b"
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


def _tag(pk, name, slug, *, description=DESCRIPTION, color=COLOR):
    return {"id": pk, "name": name, "slug": slug, "description": description, "color": color}


def _state(tags=(), users=(), changes=()):
    return {"tags": list(tags), "users": list(users), "changes": list(changes)}


# --- static: the guards, the play, the two shell tasks -----------------------------------------------------------


def test_the_extra_var_guard_and_the_preflight_come_first_and_the_play_targets_netbox_svc():
    assert DOC[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert DOC[1]["ansible.builtin.import_playbook"] == "preflight-target-group.yml"
    assert DOC[1]["vars"] == {"preflight_group": "netbox_svc", "preflight_group_expected": "netbox_svc"}
    assert PLAY["hosts"] == "netbox_svc" and PLAY["gather_facts"] is False and PLAY["become"] is False


def test_the_play_reads_no_credential_and_makes_no_request():
    assert not [t for t in _tasks() if _module(t) == "uri"]
    dumped = (json.dumps(PLAY["tasks"]) + BODY).lower()
    for word in ("x-vault-token", "secret/", "approle", "api_token", "authorization", "/v1/", "http"):
        assert word not in dumped, word


def test_both_shells_use_the_provision_exec_pattern():
    assert PLAY["vars"]["_manage"] == playbook_yaml.plays(PROVISION)[0]["vars"]["_manage"]
    for name in (DRY, REAL):
        assert _args(_named(name)).startswith("{{ _manage }} << 'PYSCRIPT'\n"), name


def test_the_dry_run_is_check_mode_only_and_read_only_by_construction():
    dry = _named(DRY)
    assert dry["when"] == "ansible_check_mode"
    assert dry["check_mode"] is False and dry["changed_when"] is False
    assert dry["register"] == "_domains_dry"
    assert "'dry_run': true" in _args(dry)


def test_the_real_run_is_skipped_under_check_mode_and_its_changed_when_reads_the_written_list():
    real = _named(REAL)
    assert real["when"] == "not ansible_check_mode"
    assert real["register"] == "_domains_real"
    assert "'dry_run': false" in _args(real)
    assert "written" in real["changed_when"]


def test_the_two_shell_tasks_never_share_a_register_name():
    # A skipped task stores its result under its register name, so a shared name lets the skipped one erase the
    # other's stdout.
    assert _named(DRY)["register"] != _named(REAL)["register"]


def test_an_empty_declaration_ends_the_play_visibly_and_a_non_list_is_refused():
    report = _named("Report an empty declaration (nothing to ensure)")
    assert report["when"] == "_domains | length == 0"
    end = _named("End the play when no business domain is declared")
    assert _module(end) == "meta" and _args(end) == "end_host" and end["when"] == "_domains | length == 0"
    refuse = _named("Refuse a declaration that is not a list of non-blank names")
    assert _module(refuse) == "assert"
    for condition in ("_domains is sequence", "_domains is not string", "reject('equalto', '')"):
        assert condition in json.dumps(_args(refuse)["that"]), condition


def test_the_declaration_is_read_from_inventory_with_a_list_default_for_undefined_and_null():
    hold = _named("Hold the declared business domains")
    assert hold["ansible.builtin.set_fact"] == {"_domains": "{{ netbox_business_domains | default([], true) }}"}


def test_the_body_has_no_jinja_markers_and_compiles_with_its_prelude():
    for marker in ("{{", "{%", "{#", "PYSCRIPT"):
        assert marker not in BODY, marker
    compile("import json\nPARAMS = {'dry_run': True, 'domains': []}\n" + BODY, "domains-body", "exec")


def test_the_body_never_deletes_anything():
    # D37: an undeclared domain tag is reported by name and left alone. A delete in the body would be the reverse.
    assert ".delete(" not in BODY


def test_the_account_is_created_inactive_with_no_grant_and_its_state_is_checked():
    assert re.findall(r'^ACCOUNT = "([^"]+)"$', BODY, re.M) == [ACCOUNT]
    assert 'defaults={"is_active": False}' in BODY
    assert "account.set_unusable_password()" in BODY
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
        "account.is_active",
        "account.is_superuser",
        "account.groups.exists()",
        "ObjectPermission.objects.filter(users=account).exists()",
        "account.has_usable_password()",
        "Token.objects.filter(user=account).exists()",
    ):
        assert check in BODY, check


def _guard(body, var):
    # The account-state condition, with the variable name normalised, so two playbooks' guards compare as code.
    start = body.index(f"if ({var}.is_active")
    end = body.index("):\n", start)
    return re.sub(r"\s+", " ", body[start:end].replace(var, "X"))


def test_the_account_guard_is_the_reconcilers_guard_code():
    reconcile_body = yaml.safe_load(RECONCILE.read_text())[1]["vars"]["_reconcile_body"]
    assert _guard(BODY, "account") == _guard(reconcile_body, "reconciler")


def test_the_writes_run_inside_the_change_logging_context_with_a_request_that_carries_the_account():
    with_line = BODY.index("with event_tracking(request):")
    assert BODY.index("request = NetBoxFakeRequest(") < with_line
    for needle in ('"user": account', '"id": uuid.uuid4()', "tag.save()"):
        assert needle in BODY, needle
    assert with_line < BODY.index("tag.save()")


def test_each_write_is_gated_on_its_own_tag_changelog_row_under_this_request_and_this_account():
    for needle in (
        'changed_object_type__app_label="extras"',
        'changed_object_type__model="tag"',
        'changed_object_id=e["pk"]',
        "user_name=ACCOUNT",
        "request_id=request.id",
    ):
        assert needle in BODY, needle
    assert BODY.index("tag.save()") < BODY.index("no changelog row for a tag's")


def test_the_constants_are_the_declared_description_colour_and_slug_prefix():
    assert re.findall(r'^DESCRIPTION = "([^"]+)"$', BODY, re.M) == [DESCRIPTION]
    assert re.findall(r'^COLOR = "([^"]+)"$', BODY, re.M) == [COLOR]
    assert re.findall(r'^SLUG_PREFIX = "([^"]+)"$', BODY, re.M) == ["domain-"]


def test_the_visible_output_is_names_and_actions_only():
    debugs = [t for t in _tasks() if _module(t) == "debug"]
    assert [t.get("name") for t in debugs][-2:] == [
        "Show the result (names, slugs, actions, undeclared names)",
        "Summarise the run (counts)",
    ]
    for t in debugs:
        text = json.dumps({k: v for k, v in t.items() if k != "name"})
        for word in ("custom_field", "vcpus", "comments", "primary_ip", "token", "description"):
            assert word not in text, (t.get("name"), word)
    assert "print(json.dumps(result, sort_keys=True))" in BODY


# --- static: the template ---------------------------------------------------------------------------------------


def test_the_template_is_a_dev_variant_with_no_survey_because_the_list_is_inventory():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    entries = [e for e in templates if e.get("name") == "Ensure NetBox Business Domains"]
    assert len(entries) == 1, entries
    assert entries[0] == {
        "name": "Ensure NetBox Business Domains",
        "playbook": "platform/playbooks/ensure-netbox-business-domains.yml",
        "dev_variant": True,
    }


# --- the stub: a small stand-in for the Django ORM calls the body makes ------------------------------------------

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


def _persist():
    with open(_PATH, "w") as fh:
        json.dump(STATE, fh, sort_keys=True)


atexit.register(_persist)


def _record(action, pk):
    # NetBox records a change only inside a request (netbox/core/signals.py), so with no request set, none is kept.
    request = CURRENT["request"]
    if request is None or os.environ.get("FAKE_NO_CHANGELOG"):
        return
    STATE["changes"].append({"app_label": "extras", "model": "tag", "changed_object_id": pk, "action": action,
                             "user_name": request.user.username, "request_id": str(request.id)})


def _matches(row, lookups):
    for key, value in lookups.items():
        if key == "slug__startswith":
            if not row["slug"].startswith(value):
                return False
        elif key in ("name", "slug"):
            if row[key] != value:
                return False
        else:
            raise AssertionError("unsupported tag lookup: %s" % key)
    return True


class _Rows:
    def __init__(self, count):
        self._count = count

    def exists(self):
        return self._count > 0


class _TagQuery:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter([Tag(**copy.deepcopy(row)) for row in self._rows])

    def first(self):
        return Tag(**copy.deepcopy(self._rows[0])) if self._rows else None

    def exists(self):
        return bool(self._rows)

    def exclude(self, **lookups):
        return _TagQuery([row for row in self._rows if not _matches(row, lookups)])

    def get(self, **lookups):
        rows = [row for row in self._rows if _matches(row, lookups)]
        if len(rows) != 1:
            raise RuntimeError("get expected one tag")
        return Tag(**copy.deepcopy(rows[0]))


class _TagManager:
    def filter(self, **lookups):
        return _TagQuery([row for row in STATE["tags"] if _matches(row, lookups)])

    def get(self, **lookups):
        return _TagQuery(STATE["tags"]).get(**lookups)


class Tag:
    objects = _TagManager()

    def __init__(self, **fields):
        object.__setattr__(self, "_data", dict({"id": None}, **fields))

    def __getattr__(self, key):
        data = object.__getattribute__(self, "_data")
        if key == "pk":
            return data["id"]
        if key in data:
            return data[key]
        raise AttributeError(key)

    def __setattr__(self, key, value):
        object.__getattribute__(self, "_data")[key] = value

    def full_clean(self):
        # NetBox's full_clean is not modelled: the tests are about the body's ORM calls.
        return None

    def save(self):
        data = object.__getattribute__(self, "_data")
        if os.environ.get("FAKE_SAVE_RENAME"):
            data["name"] = data["name"] + "~"
        rows = STATE["tags"]
        # taggit's TagBase: name and slug are each unique (django-taggit 6.1.0).
        for row in rows:
            if row["id"] != data["id"] and (row["name"] == data["name"] or row["slug"] == data["slug"]):
                raise RuntimeError("UNIQUE constraint failed for tag %s" % data["slug"])
        if data["id"] is None:
            data["id"] = max([row["id"] for row in rows], default=0) + 1
            rows.append(copy.deepcopy(data))
            _record("create", data["id"])
            return
        for index, row in enumerate(rows):
            if row["id"] == data["id"]:
                rows[index] = copy.deepcopy(data)
                _record("update", data["id"])
                return
        raise RuntimeError("save of a missing tag")

    def delete(self):
        data = object.__getattribute__(self, "_data")
        STATE["tags"] = [row for row in STATE["tags"] if row["id"] != data["id"]]
        _record("delete", data["id"])


class ObjectChange:
    class objects:
        @staticmethod
        def filter(**lookups):
            fields = {"changed_object_type__app_label": "app_label", "changed_object_type__model": "model",
                      "changed_object_id": "changed_object_id", "action": "action", "user_name": "user_name",
                      "request_id": "request_id"}
            unknown = set(lookups) - set(fields)
            if unknown:
                raise AssertionError("unsupported changelog lookup: %s" % sorted(unknown))
            rows = [row for row in STATE["changes"]
                    if all(str(row.get(fields[key])) == str(value) for key, value in lookups.items())]
            return _Rows(len(rows))


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

MODULES = {
    "django/__init__.py": "",
    "django/db/__init__.py": "from _fake_domains_orm import transaction\n",
    "django/contrib/__init__.py": "",
    "django/contrib/auth/__init__.py": "from _fake_domains_orm import get_user_model\n",
    "core/__init__.py": "",
    "core/models.py": "from _fake_domains_orm import ObjectChange\n",
    "extras/__init__.py": "",
    "extras/models.py": "from _fake_domains_orm import Tag\n",
    "netbox/__init__.py": "",
    "netbox/context_managers.py": "from _fake_domains_orm import event_tracking\n",
    "utilities/__init__.py": "",
    "utilities/request.py": "from _fake_domains_orm import NetBoxFakeRequest\n",
    "users/__init__.py": "",
    "users/models.py": "from _fake_domains_orm import ObjectPermission, Token\n",
    "_fake_domains_orm.py": FAKE_ORM,
    "manage.py": SHIM,
}


def _stub(tmp_path):
    root = tmp_path / "stub"
    for name, text in MODULES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _body_script(dry_run, domains=()):
    params = json.dumps(json.dumps({"dry_run": dry_run, "domains": list(domains)}))
    return f"import json\nPARAMS = json.loads({params})\n{BODY}"


def _run_body(tmp_path, state, *, dry_run, domains=(), env=None):
    root = _stub(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state, sort_keys=True))
    run_env = {"PATH": os.environ.get("PATH", ""), "FAKE_NETBOX_STATE": str(state_file), **(env or {})}
    proc = subprocess.run(
        [sys.executable, str(root / "manage.py")],
        input=_body_script(dry_run, domains),
        cwd=tmp_path,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    result = json.loads(proc.stdout.splitlines()[-1]) if proc.stdout.strip() else None
    return proc, result, json.loads(state_file.read_text())


# --- decisions: the body run against the stub -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, slug",
    [
        ("Products", "domain-products"),
        ("Professional Services", "domain-professional-services"),
        ("Automation", "domain-automation"),
        ("Administration", "domain-administration"),
        ("IT", "domain-it"),
        ("Professional  Services", "domain-professional-services"),
        ("  Ops / Infra  ", "domain-ops-infra"),
        ("Ops--IT", "domain-ops-it"),
        ("R&D", "domain-r-d"),
        ("Café", "domain-caf"),
    ],
    ids=[
        "products",
        "professional-services",
        "automation",
        "administration",
        "it",
        "double-space-collapses",
        "outer-and-inner-runs-trimmed-and-collapsed",
        "double-hyphen-collapses",
        "ampersand-is-a-separator",
        "non-ascii-is-a-separator",
    ],
)
def test_the_slug_is_domain_plus_the_lower_case_name_with_runs_collapsed_and_the_ends_trimmed(tmp_path, name, slug):
    proc, result, after = _run_body(tmp_path, _state(), dry_run=True, domains=[name])
    assert proc.returncode == 0, proc.stderr
    assert result["domains"] == [{"name": name, "slug": slug, "action": "would-create"}]
    assert after == _state(), "a dry run wrote"


@pytest.mark.parametrize(
    "domains, reason",
    [
        (["!!!"], "has no ASCII letter or digit"),
        (["IT", "it"], "two declared business domains give one slug"),
        ([""], "is blank or not a string"),
        (["  "], "is blank or not a string"),
    ],
    ids=["no-slug", "two-names-one-slug", "empty", "blank"],
)
def test_a_name_with_no_slug_or_two_names_with_one_slug_are_refused_before_any_write(tmp_path, domains, reason):
    before = _state([_tag(1, "Products", "domain-products")])
    proc, result, after = _run_body(tmp_path, before, dry_run=False, domains=domains)
    assert proc.returncode != 0, proc.stdout
    assert reason in proc.stderr
    assert after == before


def test_a_missing_domain_tag_is_created_with_the_declared_fields_and_logged_under_the_account(tmp_path):
    proc, result, after = _run_body(tmp_path, _state(), dry_run=False, domains=["Professional Services"])
    assert proc.returncode == 0, proc.stderr
    assert result["domains"] == [
        {"name": "Professional Services", "slug": "domain-professional-services", "action": "created"}
    ]
    assert result["written"] == ["Professional Services"]
    assert after["tags"] == [
        {
            "id": 1,
            "name": "Professional Services",
            "slug": "domain-professional-services",
            "description": DESCRIPTION,
            "color": COLOR,
        }
    ]
    assert after["users"] == [
        {"username": ACCOUNT, "is_active": False, "is_superuser": False, "usable_password": False}
    ]
    rows = [row for row in after["changes"] if row["user_name"] == ACCOUNT]
    assert [(r["action"], r["changed_object_id"], r["app_label"], r["model"]) for r in rows] == [
        ("create", 1, "extras", "tag")
    ]
    assert rows[0]["request_id"], "the write must carry a request id"


@pytest.mark.parametrize(
    "stored",
    [
        _tag(5, "Product", "domain-products"),
        _tag(5, "Products", "domain-products", description="An older description"),
    ],
    ids=["name-differs", "description-differs"],
)
def test_a_declared_tag_whose_name_or_description_differs_is_updated_and_logged(tmp_path, stored):
    proc, result, after = _run_body(tmp_path, _state([stored]), dry_run=False, domains=["Products"])
    assert proc.returncode == 0, proc.stderr
    assert result["domains"] == [{"name": "Products", "slug": "domain-products", "action": "updated"}]
    assert after["tags"] == [_tag(5, "Products", "domain-products")]
    rows = [row for row in after["changes"] if row["user_name"] == ACCOUNT]
    assert [(r["action"], r["changed_object_id"]) for r in rows] == [("update", 5)]


def test_an_identical_tag_is_unchanged_and_writes_nothing_and_creates_no_account(tmp_path):
    before = _state([_tag(5, "Products", "domain-products")])
    proc, result, after = _run_body(tmp_path, before, dry_run=False, domains=["Products"])
    assert proc.returncode == 0, proc.stderr
    assert [d["action"] for d in result["domains"]] == ["unchanged"]
    assert result["written"] == []
    assert after == before


def test_an_undeclared_domain_tag_is_reported_by_name_and_never_deleted(tmp_path):
    before = _state(
        [
            _tag(5, "Products", "domain-products"),
            _tag(6, "Legacy", "domain-legacy"),
            _tag(7, "Blue", "blue", description=""),
        ]
    )
    # "IT" is created, so the transaction commits. With nothing to write the body rolls back, which would hide a
    # delete made in the same run: the commit path is the one a delete must be caught on.
    proc, result, after = _run_body(tmp_path, before, dry_run=False, domains=["Products", "IT"])
    assert proc.returncode == 0, proc.stderr
    assert result["undeclared"] == ["Legacy"], "a non-domain tag is not a business domain"
    assert result["written"] == ["IT"]
    committed = [row for row in after["tags"] if row["slug"] != "domain-it"]
    assert committed == before["tags"], "the undeclared tag must still exist, and nothing else may change"


def test_check_mode_reports_would_create_and_would_update_and_writes_nothing(tmp_path):
    before = _state([_tag(5, "Product", "domain-products")])
    proc, result, after = _run_body(tmp_path, before, dry_run=True, domains=["Products", "IT"])
    assert proc.returncode == 0, proc.stderr
    assert result["dry_run"] is True
    assert [(d["name"], d["action"]) for d in result["domains"]] == [
        ("Products", "would-update"),
        ("IT", "would-create"),
    ]
    assert after == before


def test_a_declared_name_held_by_a_tag_under_another_slug_is_refused_before_any_write(tmp_path):
    before = _state([_tag(3, "IT", "it", description="")])
    proc, result, after = _run_body(tmp_path, before, dry_run=False, domains=["IT"])
    assert proc.returncode != 0, proc.stdout
    assert "under another slug already holds the declared name 'IT'" in proc.stderr
    assert after == before


@pytest.mark.parametrize(
    "account, env",
    [
        ({"username": ACCOUNT, "is_active": True, "is_superuser": False, "usable_password": False}, {}),
        ({"username": ACCOUNT, "is_active": False, "is_superuser": True, "usable_password": False}, {}),
        ({"username": ACCOUNT, "is_active": False, "is_superuser": False, "usable_password": True}, {}),
        (
            {"username": ACCOUNT, "is_active": False, "is_superuser": False, "usable_password": False},
            {"FAKE_ACCOUNT_GRANTS": "1"},
        ),
        (
            {"username": ACCOUNT, "is_active": False, "is_superuser": False, "usable_password": False},
            {"FAKE_ACCOUNT_TOKEN": "1"},
        ),
    ],
    ids=["active", "superuser", "usable-password", "grant", "api-token"],
)
def test_a_pre_existing_account_that_is_not_inactive_unusable_and_bare_stops_the_run(tmp_path, account, env):
    before = _state(users=[account])
    proc, result, after = _run_body(tmp_path, before, dry_run=False, domains=["Products"], env=env)
    assert proc.returncode != 0, proc.stdout
    assert "is active, holds a grant, a usable password or an API token" in proc.stderr
    assert after == before


def test_a_pre_existing_inactive_unusable_account_with_no_grant_is_used_as_it_is(tmp_path):
    account = {"username": ACCOUNT, "is_active": False, "is_superuser": False, "usable_password": False}
    proc, result, after = _run_body(tmp_path, _state(users=[account]), dry_run=False, domains=["Products"])
    assert proc.returncode == 0, proc.stderr
    assert after["users"] == [account]
    assert [r["action"] for r in after["changes"] if r["user_name"] == ACCOUNT] == ["create"]


def test_a_write_without_its_changelog_row_rolls_back(tmp_path):
    before = _state()
    proc, result, after = _run_body(
        tmp_path, before, dry_run=False, domains=["Products"], env={"FAKE_NO_CHANGELOG": "1"}
    )
    assert proc.returncode != 0, proc.stdout
    assert "no changelog row for a tag's create" in proc.stderr
    assert after == before


def test_a_saved_name_that_does_not_read_back_as_declared_rolls_back(tmp_path):
    before = _state()
    proc, result, after = _run_body(
        tmp_path, before, dry_run=False, domains=["Products"], env={"FAKE_SAVE_RENAME": "1"}
    )
    assert proc.returncode != 0, proc.stdout
    assert "does not read back as declared" in proc.stderr
    assert after == before


def test_a_run_with_nothing_to_write_creates_no_account(tmp_path):
    proc, result, after = _run_body(
        tmp_path, _state([_tag(5, "Products", "domain-products")]), dry_run=False, domains=["Products"]
    )
    assert proc.returncode == 0, proc.stderr
    assert "users" not in after or after["users"] == []


# --- through the real playbook: ansible-playbook, a fake container engine, the same stub ------------------------


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
def test_the_playbook_creates_the_declared_tags_through_the_shell(tmp_path):
    proc, out, after = _ansible(
        tmp_path, _state(), extra={"netbox_business_domains": ["Products", "Professional Services"]}
    )
    assert proc.returncode == 0, out
    assert sorted(t["slug"] for t in after["tags"]) == ["domain-products", "domain-professional-services"]
    assert '"action": "created"' in out


@NEEDS_ANSIBLE
def test_check_mode_plans_through_the_shell_with_dry_run_and_changes_nothing(tmp_path):
    before = _state([_tag(5, "Product", "domain-products")])
    proc, out, after = _ansible(tmp_path, before, check=True, extra={"netbox_business_domains": ["Products", "IT"]})
    assert proc.returncode == 0, out
    assert after == before
    received = (_stub(tmp_path) / "received.log").read_text()
    assert 'dry_run\\": true' in received and 'dry_run\\": false' not in received
    assert '"action": "would-update"' in out and '"action": "would-create"' in out


@NEEDS_ANSIBLE
@pytest.mark.parametrize("extra", [{}, {"netbox_business_domains": []}], ids=["undefined", "empty-list"])
def test_an_empty_or_undeclared_declaration_is_a_visible_noop_with_no_shell_call(tmp_path, extra):
    before = _state()
    proc, out, after = _ansible(tmp_path, before, extra=extra)
    assert proc.returncode == 0, out
    assert "nothing to ensure" in out
    assert not (_stub(tmp_path) / "received.log").exists()
    assert after == before


@NEEDS_ANSIBLE
def test_a_string_declaration_is_refused_before_the_shell_runs(tmp_path):
    before = _state()
    proc, out, after = _ansible(tmp_path, before, extra={"netbox_business_domains": "Products"})
    assert proc.returncode != 0, out
    assert "must be a list of display names" in out
    assert not (_stub(tmp_path) / "received.log").exists()
    assert after == before
