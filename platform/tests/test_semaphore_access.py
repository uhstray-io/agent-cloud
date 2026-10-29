"""The Semaphore launch-access rules (filters semaphore_access_drift and
semaphore_admin_roster), tested directly.

Every identity that can launch a task can run code on the runner, so the project team, the
system admins and the integrations are held to a declaration. Admins are derived from the one
administrator list (#604). All names are synthetic.
"""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "semaphore_access", Path(__file__).resolve().parents[1] / "playbooks/filter_plugins/semaphore_access.py")
rule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rule)

USERS = [{"id": 1, "username": "ops-admin", "admin": True},
         {"id": 2, "username": "runner-a", "admin": False},
         {"id": 3, "username": "viewer-b", "admin": False},
         {"id": 4, "username": "newcomer", "admin": False}]
MEMBERS = [{"id": 2, "username": "runner-a", "role": "task_runner"},
           {"id": 3, "username": "viewer-b", "role": "guest"}]


def drift(declared, *, members=MEMBERS, admins=("ops-admin",), integrations=()):
    return rule.semaphore_access_drift(members, declared, USERS, list(admins), list(integrations))


def test_a_team_matching_its_declaration_changes_nothing():
    assert drift({"runner-a": "task_runner", "viewer-b": "guest"}) == {
        "invalid": [], "problems": [], "add": [], "set_role": [], "remove": []}


def test_an_undeclared_member_is_removed_and_a_changed_role_is_set():
    result = drift({"runner-a": "guest"})
    assert result["set_role"] == [{"user_id": 2, "username": "runner-a", "from": "task_runner", "to": "guest"}]
    assert result["remove"] == [{"user_id": 3, "username": "viewer-b", "role": "guest"}]
    assert result["problems"] == []


def test_a_declared_account_that_is_not_yet_a_member_is_added():
    result = drift({"runner-a": "task_runner", "viewer-b": "guest", "newcomer": "guest"})
    assert result["add"] == [{"user_id": 4, "username": "newcomer", "role": "guest"}]


def test_a_missing_or_empty_declaration_refuses_instead_of_removing_everyone():
    for declared in (None, {}, []):
        result = drift(declared)
        assert result["invalid"] and not result["remove"]


def test_a_declared_name_without_an_account_or_role_makes_the_declaration_invalid():
    result = drift({"runner-a": "task_runner", "viewer-b": "guest", "ghost": "guest", "newcomer": ""})
    assert result["invalid"] == ["declared member ghost has no Semaphore account",
                                 "declared member newcomer has no role"]
    assert result["problems"] == []


def test_an_undeclared_system_admin_is_a_problem_matched_by_username_only():
    result = drift({"runner-a": "task_runner", "viewer-b": "guest"}, admins=())
    assert result["problems"] == ["system admin ops-admin is not declared (platform_admins or semaphore_admin_users)"]


def test_any_integration_is_a_problem_because_a_webhook_can_set_extra_vars():
    result = drift({"runner-a": "task_runner", "viewer-b": "guest"}, integrations=[{"id": 7, "name": "deploy-hook"}])
    assert result["problems"] == ["integration deploy-hook can set extra vars from a webhook; none is declared"]


PLATFORM_ADMINS = [{"key": "ops", "username": "ops-admin", "name": "Ops", "email": "ops@example.invalid"},
                   {"key": "second", "username": "second-operator", "name": "Second", "email": "s@example.invalid"},
                   {"key": "svc", "username": "svc-robot", "name": "Robot", "type": "service_account"}]


def test_every_platform_admin_is_a_semaphore_admin_except_the_service_account():
    # #604: a platform admin missing from the hand-kept Semaphore list saw no projects.
    assert rule.semaphore_admin_roster(["local-admin"], PLATFORM_ADMINS) == [
        "local-admin", "ops-admin", "second-operator"]


def test_a_name_on_both_lists_is_one_admin():
    assert rule.semaphore_admin_roster(["ops-admin", " ops-admin "], PLATFORM_ADMINS[:1]) == ["ops-admin"]


def test_an_admin_list_this_play_cannot_see_derives_nothing():
    # Undeclared for localhost must not fall back to the committed placeholder default.
    assert rule.semaphore_admin_roster(["local-admin"], None) == ["local-admin"]
    assert rule.semaphore_admin_roster(None, None) == []


@pytest.mark.parametrize("declared, admins, message", [
    (["ok"], [{"key": "x"}], "platform_admins entry #1 has no username"),
    (["ok"], [{"username": "  "}], "platform_admins entry #1 has no username"),
    (["ok"], "ops-admin", "platform_admins is not a list"),
    (["ok"], {"username": "ops-admin"}, "platform_admins is not a list"),
    ("ops-admin", None, "semaphore_admin_users is not a list"),
    ({"newcomer": "guest"}, None, "semaphore_admin_users is not a list"),
    ([""], None, "is not a username"),
])
def test_a_malformed_declaration_is_refused_rather_than_read_partly(declared, admins, message):
    with pytest.raises(ValueError, match=message):
        rule.semaphore_admin_roster(declared, admins)


def test_a_derived_admin_is_not_reported_as_undeclared():
    roster = rule.semaphore_admin_roster([], [{"username": "ops-admin"}])
    assert drift({"runner-a": "task_runner", "viewer-b": "guest"}, admins=roster)["problems"] == []
