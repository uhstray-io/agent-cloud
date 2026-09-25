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

ENDPOINT = "https://bao.example:8200"
AUTH = [{"id": 1, "name": "BAO_ROLE_ID", "type": "env"}, {"id": 2, "name": "BAO_SECRET_ID", "type": "env"}]


def env(secrets=AUTH, json=f'{{"openbao_addr":"{ENDPOINT}"}}', plain="{}"):
    return {"json": json, "env": plain, "secrets": secrets}


@pytest.mark.parametrize("secrets", [AUTH, []])
def test_a_provisioned_or_fresh_environment_is_clean(secrets):
    assert rule(env(secrets), ENDPOINT) == []


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
    assert problem in rule(env(secrets), ENDPOINT)


@pytest.mark.parametrize("environment,problem", [
    (env(plain='{"TOKEN":"x"}'), "plaintext env vars present"),
    (env(json=f'{{"openbao_addr":"{ENDPOINT}","extra":1}}'), "extra-var JSON beyond openbao_addr"),
    (env(json="not json"), "extra-var JSON beyond openbao_addr"),
    (env(json='{"openbao_addr":"https://elsewhere.example:8200"}'),
     "OpenBao endpoint differs from the approved endpoint"),
])
def test_every_unsafe_configuration_is_named(environment, problem):
    assert problem in rule(environment, ENDPOINT)


def test_an_endpoint_with_nothing_approved_to_compare_is_refused():
    assert "OpenBao endpoint set but no approved endpoint to check it against" in rule(env())


def test_names_only_never_values():
    leftover = {"id": 3, "name": "BAO_VALUE", "type": "env", "secret": "synthetic-never-printed"}
    assert "synthetic-never-printed" not in " ".join(rule(env(AUTH + [leftover]), ENDPOINT))
