"""Authentik account retirement is driven by ONE declared list, `platform_retired_accounts`.

An account is retired by naming its username in site-config; the deploy renders a
`state: absent` blueprint for it (templates/platform-retired.yaml.j2) and the verify step proves
each name gone. This file runs the deploy's REAL tasks (lifted out of deploy-authentik.yml by
name, as test_authentik_platform_admins.py does for the collision guard) and the REAL templates:

  - the names reach the retired set the verify reads, joined with the legacy env-derived ones;
  - a name that is also an active account (an administrator, or a user the users blueprint
    creates) or the bootstrap `akadmin` is refused before anything is applied, as is a list that
    is not a list of distinct non-empty strings;
  - the blueprint renders one `state: absent` user per name, safely quoted, and is not placed at
    all (and a stale copy is pruned) when the list is empty, so an empty declaration changes
    nothing a deploy did before the list existed;
  - the verify expects the blueprint and each name absent, and an empty list adds nothing.

Requires ansible-playbook (the real task modules run).
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
DEPLOYMENT = REPO / "platform/services/authentik/deployment"

pytestmark = needs_ansible

VARS_FILES = [
    str(PLAYBOOKS / "vars/platform-admins.yml"),
    str(PLAYBOOKS / "vars/secret-declarations/authentik.yml"),
]


def _deploy_tasks() -> list:
    plays = yaml.safe_load((PLAYBOOKS / "deploy-authentik.yml").read_text())
    return [t for p in plays for t in p.get("tasks", [])]


def _slice(first: str, last: str) -> list:
    tasks = _deploy_tasks()
    names = [t.get("name", "") for t in tasks]
    return tasks[names.index(first) : names.index(last) + 1]


def _ansible(tmp: Path, play: dict, extra: dict, check: bool = False) -> subprocess.CompletedProcess:
    # The deploy reads env.j2 at playbook_dir/../services/... and includes tasks/ next to it.
    (tmp / "playbooks").mkdir(exist_ok=True)
    for link, target in (("vars", PLAYBOOKS / "vars"), ("tasks", PLAYBOOKS / "tasks")):
        if not (tmp / "playbooks" / link).exists():
            (tmp / "playbooks" / link).symlink_to(target)
    if not (tmp / "services").exists():
        (tmp / "services").symlink_to(REPO / "platform/services")
    (tmp / "playbooks/play.yml").write_text(yaml.safe_dump([play]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    cmd = ["ansible-playbook", "-i", "localhost,", str(tmp / "playbooks/play.yml"), "-e", json.dumps(extra)]
    if check:
        cmd.append("--check")
    return subprocess.run(cmd, cwd=tmp, env=env, text=True, capture_output=True, check=False)


def _facts(tmp: Path, inventory: dict) -> tuple[subprocess.CompletedProcess, dict]:
    """Run the deploy's guard (read declarations ... refuse create-and-delete) and dump the retired facts."""
    tasks = _slice("Read the username declarations out of env.j2", "Refuse to both create and delete the same account")
    tasks.append(
        {
            "ansible.builtin.copy": {
                "dest": str(tmp / "facts.json"),
                "content": "{{ {'usernames': _retired_usernames, 'blueprints': _retired_blueprints, "
                "'accounts': _platform_retired_accounts} | to_json }}",
            }
        }
    )
    play = {
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars_files": VARS_FILES,
        "tasks": tasks,
    }
    done = _ansible(tmp, play, inventory)
    facts = json.loads((tmp / "facts.json").read_text()) if (tmp / "facts.json").exists() else {}
    return done, facts


# ── the list reaches the retired set ────────────────────────────────────────────
def test_the_listed_names_join_the_retired_set_and_select_the_blueprint(tmp_path):
    done, facts = _facts(tmp_path, {"platform_retired_accounts": ["gone.one", "gone.two"]})
    assert done.returncode == 0, done.stdout + done.stderr
    assert facts["usernames"] == ["retired-account-never-created", "gone.one", "gone.two"]
    assert facts["blueprints"] == ["platform-retired.yaml"]


def test_the_legacy_username_and_the_list_are_one_retired_set(tmp_path):
    # The legacy mechanism keeps working untouched, and a name given both ways is retired once.
    done, facts = _facts(
        tmp_path, {"andrew_legacy_username": "old.andrew", "platform_retired_accounts": ["old.andrew", "x"]}
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert facts["usernames"] == ["old.andrew", "x"]


@pytest.mark.parametrize("inventory", [{}, {"platform_retired_accounts": []}, {"platform_retired_accounts": None}])
def test_no_list_adds_nothing_to_the_retired_set(tmp_path, inventory):
    done, facts = _facts(tmp_path, inventory)
    assert done.returncode == 0, done.stdout + done.stderr
    assert facts["usernames"] == ["retired-account-never-created"]  # exactly what the legacy variable gave before
    assert facts["blueprints"] == []
    assert facts["accounts"] == []


def test_surrounding_whitespace_is_not_part_of_the_name(tmp_path):
    done, facts = _facts(tmp_path, {"platform_retired_accounts": ["  gone.one "]})
    assert done.returncode == 0, done.stdout + done.stderr
    assert facts["accounts"] == ["gone.one"]


# ── the guard ───────────────────────────────────────────────────────────────────
def test_retiring_a_declared_administrator_is_refused(tmp_path):
    done, _ = _facts(tmp_path, {"platform_retired_accounts": ["stray"]})
    assert done.returncode != 0, done.stdout
    assert "A retired username is also an ACTIVE username: stray" in done.stdout


def test_retiring_an_administrator_of_an_inventory_list_is_refused(tmp_path):
    admins = [{"key": "carol", "username": "carol@example.invalid", "name": "Carol", "email": "carol@example.invalid"}]
    done, _ = _facts(tmp_path, {"platform_admins": admins, "platform_retired_accounts": ["carol@example.invalid"]})
    assert done.returncode != 0, done.stdout
    assert "A retired username is also an ACTIVE username: carol@example.invalid" in done.stdout


def test_retiring_an_active_user_of_the_users_blueprint_is_refused(tmp_path):
    # `developer` is ANDREW_USERNAME's default in env.j2: active although no variable is set.
    done, _ = _facts(tmp_path, {"platform_retired_accounts": ["developer"]})
    assert done.returncode != 0, done.stdout
    assert "A retired username is also an ACTIVE username: developer" in done.stdout


def test_the_legacy_variable_is_still_checked_against_the_active_set(tmp_path):
    done, _ = _facts(tmp_path, {"andrew_legacy_username": "stray"})
    assert done.returncode != 0, done.stdout
    assert "A retired username is also an ACTIVE username: stray" in done.stdout


def test_the_bootstrap_account_cannot_be_retired(tmp_path):
    done, _ = _facts(tmp_path, {"platform_retired_accounts": ["akadmin"]})
    assert done.returncode != 0, done.stdout
    assert "must be a list of non-empty, distinct username" in done.stdout


@pytest.mark.parametrize(
    "value",
    ["wisward", {"wisward": True}, [123], [["a"]], [""], ["   "], ["a", "a"], ["a", " a "]],
    ids=["string", "mapping", "non-string", "nested", "empty", "blank", "duplicate", "duplicate-after-trim"],
)
def test_a_malformed_list_is_refused_before_anything_acts(tmp_path, value):
    done, _ = _facts(tmp_path, {"platform_retired_accounts": value})
    assert done.returncode != 0, done.stdout
    assert "platform_retired_accounts must be a list of non-empty, distinct username" in done.stdout


# ── the blueprint ───────────────────────────────────────────────────────────────
def _render_blueprint(tmp: Path, accounts: list) -> dict:
    play = {
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": [
            {
                "ansible.builtin.template": {
                    "src": str(DEPLOYMENT / "templates/platform-retired.yaml.j2"),
                    "dest": str(tmp / "bp.yaml"),
                    "mode": "0644",
                }
            }
        ],
    }
    done = _ansible(tmp, play, {"_platform_retired_accounts": accounts})
    assert done.returncode == 0, done.stdout + done.stderr
    return yaml.safe_load((tmp / "bp.yaml").read_text())


@pytest.mark.parametrize("names", [["gone.one"], ["gone.one", "gone.two"], ['odd: "name"', "it's#1"]])
def test_the_blueprint_deletes_exactly_the_listed_accounts(tmp_path, names):
    doc = _render_blueprint(tmp_path, names)
    assert doc["version"] == 1
    assert doc["entries"] == [
        {"model": "authentik_core.user", "state": "absent", "identifiers": {"username": n}} for n in names
    ]


def test_the_blueprint_carries_no_attrs_that_could_create_or_modify_an_account(tmp_path):
    for e in _render_blueprint(tmp_path, ["a", "b"])["entries"]:
        assert set(e) == {"model", "state", "identifiers"}
        assert e["state"] == "absent"


def _assembly(tmp: Path, accounts: list, check: bool = False) -> subprocess.CompletedProcess:
    """The deploy's real assembly -> prune -> render-retired tasks against a scratch deploy dir."""
    (tmp / "deploy").mkdir(exist_ok=True)
    play = {
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars_files": VARS_FILES,
        "vars": {
            "_deploy_dir": str(tmp / "deploy"),
            "_authentik_tpls": str(DEPLOYMENT),
            "_shared_blueprints": ["platform-groups.yaml"],
            "_enabled_apps": [],
            "authentik_app_catalog": {},
            "_platform_retired_accounts": accounts,
            "_retired_blueprints": ["platform-retired.yaml"] if accounts else [],
        },
        "tasks": _slice(
            "Ensure the active-blueprints dir exists", "Render platform-retired from the retired-account list"
        ),
    }
    return _ansible(tmp, play, {}, check=check)


def _placed(tmp: Path) -> list:
    return sorted(p.name for p in (tmp / "deploy/blueprints-active").iterdir())


def test_the_blueprint_is_placed_when_the_list_is_declared(tmp_path):
    done = _assembly(tmp_path, ["gone.one"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "platform-retired.yaml" in _placed(tmp_path)
    # and the next run with the same list changes nothing, including the prune
    again = _assembly(tmp_path, ["gone.one"])
    assert again.returncode == 0, again.stdout + again.stderr
    assert re.search(r"changed=0\b", again.stdout), again.stdout


def test_no_blueprint_is_placed_for_an_empty_list(tmp_path):
    done = _assembly(tmp_path, [])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "platform-retired.yaml" not in _placed(tmp_path)


def test_a_stale_blueprint_is_pruned_once_the_list_is_emptied(tmp_path):
    assert _assembly(tmp_path, ["gone.one"]).returncode == 0
    assert "platform-retired.yaml" in _placed(tmp_path)
    done = _assembly(tmp_path, [])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "platform-retired.yaml" not in _placed(tmp_path)


def test_the_retired_blueprint_survives_the_prune_while_declared(tmp_path):
    # The prune removes every file outside the active set; the retired blueprint must be in it.
    assert _assembly(tmp_path, ["gone.one"]).returncode == 0
    assert _assembly(tmp_path, ["gone.one", "gone.two"]).returncode == 0
    doc = yaml.safe_load((tmp_path / "deploy/blueprints-active/platform-retired.yaml").read_text())
    assert [e["identifiers"]["username"] for e in doc["entries"]] == ["gone.one", "gone.two"]


# ── the verify ──────────────────────────────────────────────────────────────────
def _verify_constants(tmp: Path, facts: dict) -> dict:
    """Render verify-users.py.j2 with the given facts; return its EXPECT_ABSENT and EXPECT_BLUEPRINTS."""
    play = {
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {
            "_shared_blueprints": ["platform-groups.yaml"],
            "_enabled_apps": [],
            "authentik_app_catalog": {},
            "_active_usernames": ["stray"],
            "_platform_admins": [{"username": "stray"}],
            "authentik_blueprint_settle_seconds": 0,
            **facts,
        },
        "tasks": [
            {
                "ansible.builtin.template": {
                    "src": str(DEPLOYMENT / "templates/verify-users.py.j2"),
                    "dest": str(tmp / "verify.py"),
                    "mode": "0644",
                }
            }
        ],
    }
    done = _ansible(tmp, play, {})
    assert done.returncode == 0, done.stdout + done.stderr
    text = (tmp / "verify.py").read_text()
    return {
        name: json.loads(re.search(rf"^{name} = (.*)$", text, re.M).group(1))
        for name in ("EXPECT_ABSENT", "EXPECT_BLUEPRINTS")
    }


def test_the_verify_expects_each_listed_account_absent_and_the_blueprint_applied(tmp_path):
    got = _verify_constants(
        tmp_path,
        {
            "_retired_usernames": ["retired-account-never-created", "gone.one", "gone.two"],
            "_retired_blueprints": ["platform-retired.yaml"],
        },
    )
    assert got["EXPECT_ABSENT"] == ["retired-account-never-created", "gone.one", "gone.two"]
    assert "custom/platform-retired.yaml" in got["EXPECT_BLUEPRINTS"]


def test_the_verify_is_unchanged_by_an_empty_list(tmp_path):
    # The two states a deploy can be in without the list: the fact set empty, or never set.
    base = {"_retired_usernames": ["retired-account-never-created"]}
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    empty = _verify_constants(tmp_path / "a", {**base, "_retired_blueprints": []})
    unset = _verify_constants(tmp_path / "b", base)
    assert empty == unset
    assert "custom/platform-retired.yaml" not in empty["EXPECT_BLUEPRINTS"]
    assert empty["EXPECT_BLUEPRINTS"] == [
        "custom/platform-groups.yaml",
        "custom/zz-sso-bindings.yaml",
        "custom/platform-admins.yaml",
    ]
