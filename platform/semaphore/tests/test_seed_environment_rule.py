"""The one clean-environment rule (filter seed_environment_problems), tested directly.

Publication, the provisioner and the seed CLIs all call this function; their own tests keep one
real refusal each to prove the wiring, and the variants live here, where each case is a
function call instead of an ansible-playbook run.
"""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "publication", Path(__file__).resolve().parents[1] / "filter_plugins/publication.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)
rule = publication.seed_environment_problems

AUTH = [{"id": 1, "name": "BAO_ROLE_ID", "type": "env"}, {"id": 2, "name": "BAO_SECRET_ID", "type": "env"}]


def env(secrets=AUTH, json="{}", plain="{}"):
    return {"json": json, "env": plain, "secrets": secrets}


@pytest.mark.parametrize("secrets", [AUTH, []])
def test_a_provisioned_or_fresh_environment_is_clean(secrets):
    assert rule(env(secrets)) == []


@pytest.mark.parametrize("secrets,problem", [
    (AUTH + [{"id": 3, "name": "BAO_VALUE", "type": "env"}], "leftover inputs: BAO_VALUE"),
    (AUTH + [{"id": 3, "name": "SEED_X_API_KEY", "type": "env"}], "leftover inputs: SEED_X_API_KEY"),
    ([{"id": 3, "name": "SOMETHING_ELSE", "type": "env"}], "leftover inputs: SOMETHING_ELSE"),
    ([AUTH[0]], "only one AppRole input"),
    ([AUTH[1]], "only one AppRole input"),
    (AUTH + [dict(AUTH[0], id=9)], "duplicate AppRole inputs"),
    ([dict(AUTH[0], type="var"), AUTH[1]], "input not of env type"),
    (None, "no secrets list; contents cannot be established"),
])
def test_every_unsafe_input_set_is_named(secrets, problem):
    assert problem in rule(env(secrets))


PIN = '{"openbao_addr":"https://bao.example:8200"}'


@pytest.mark.parametrize("environment,problem", [
    (env(plain='{"TOKEN":"x"}'), "plaintext env vars present"),
    (env(json='{"extra":1}'), "extra-var JSON present"),
    (env(json="not json"), "extra-var JSON present"),
    # The seed run takes its address from the inventory; a pin would override it.
    (env(json=PIN), "OpenBao address pinned in the environment"),
])
def test_every_unsafe_configuration_is_named(environment, problem):
    assert any(problem in p for p in rule(environment))


def test_only_the_provisioner_may_accept_a_legacy_pin_and_nothing_else():
    assert rule(env(json=PIN), legacy_pin_ok=True) == []  # it removes the pin next
    assert "extra-var JSON present" in rule(env(json='{"openbao_addr":"x","extra":1}'), legacy_pin_ok=True)


def test_names_only_never_values():
    leftover = {"id": 3, "name": "BAO_VALUE", "type": "env", "secret": "synthetic-never-printed"}
    assert "synthetic-never-printed" not in " ".join(rule(env(AUTH + [leftover])))
