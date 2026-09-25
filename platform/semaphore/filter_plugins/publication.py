"""Compare declared fields with Semaphore's omitempty JSON representations, and check
what a dedicated seed environment may contain.

SurveyVar's optional zero values are omitted by the official v2.18.12 model:
https://github.com/semaphoreui/semaphore/blob/v2.18.12/db/Template.go
"""

import json


def publication_matches(expected, actual):
    """Ignore server-added keys and omitted JSON zero values, but retain order."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            publication_matches(value, actual[key]) if key in actual
            else value is None or value is False or value == "" or value == []
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(expected) == len(actual)
                and all(publication_matches(a, b) for a, b in zip(expected, actual, strict=True)))
    if isinstance(expected, bool):
        return isinstance(actual, bool) and expected == actual
    return expected == actual


def publication_settings(record):
    """Exclude survey payload and computed runtime metadata from preservation."""
    return {key: value for key, value in record.items()
            if key not in {"survey_vars", "last_task", "tasks", "permissions"}}


SEED_AUTH_INPUTS = ("BAO_ROLE_ID", "BAO_SECRET_ID")


def seed_environment_problems(environment, approved_endpoint=None):
    """Why a dedicated seed environment is not safe to bind a seed template to.

    The ONE definition of a clean isolated seed environment, shared by full publication
    (tasks/isolated-environments.yml), the provisioner (provision-seed-environment.yml) and
    the seed CLIs' preflight (scripts/semaphore_seed.py): before 2026-09-23 each had its own
    rule and they disagreed (reviews of PR #205).
    Clean means: no plaintext env vars, extra-var JSON holding at most `openbao_addr`,
    encrypted inputs that are exactly the two AppRole inputs or none, each once, as env
    type, and an `openbao_addr`, when present, equal to the approved endpoint. Anything
    else (a leftover BAO_VALUE or SEED_* value, a lone AppRole half, an address the AppRole
    login would be sent to that nobody approved) would reach a bound template. Returns
    names only, never values; empty means clean.
    """
    problems = []

    def as_dict(field):
        raw = environment.get(field) or "{}"
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    env_vars, extra = as_dict("env"), as_dict("json")
    if env_vars is None or env_vars:
        problems.append("plaintext env vars present")
    if extra is None or set(extra) - {"openbao_addr"}:
        problems.append("extra-var JSON beyond openbao_addr")
    if extra and "openbao_addr" in extra:
        if not approved_endpoint:
            problems.append("OpenBao endpoint set but no approved endpoint to check it against")
        elif extra["openbao_addr"] != approved_endpoint:
            problems.append("OpenBao endpoint differs from the approved endpoint")
    # Semaphore v2.17.31 fills metadata before GET but omits an empty list from JSON.
    # An explicit null or malformed list is still unknown and must fail closed.
    secrets = environment.get("secrets", [])
    if not isinstance(secrets, list):
        return problems + ["no secrets list; contents cannot be established"]
    names = [item.get("name") for item in secrets]
    extra_inputs = sorted({str(n) for n in names} - set(SEED_AUTH_INPUTS))
    if extra_inputs:
        problems.append("leftover inputs: " + ", ".join(extra_inputs))
    auth = [n for n in names if n in SEED_AUTH_INPUTS]
    if len(auth) != len(set(auth)):
        problems.append("duplicate AppRole inputs")
    if set(auth) not in (set(), set(SEED_AUTH_INPUTS)):
        problems.append("only one AppRole input")
    if any(item.get("type") != "env" for item in secrets):
        problems.append("input not of env type")
    return problems


class FilterModule:
    def filters(self):
        return {"publication_matches": publication_matches, "publication_settings": publication_settings,
                "seed_environment_problems": seed_environment_problems}
