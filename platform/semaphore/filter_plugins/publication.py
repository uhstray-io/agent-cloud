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


def effective_environment_ids(template):
    """Return the exact effective binding set, or None for malformed metadata.

    Semaphore v2.19.11 gives environment_ids precedence over environment_id.
    Older scalar-only responses remain accepted.
    """
    if not isinstance(template, dict):
        return None
    scalar = template.get("environment_id")
    if isinstance(scalar, bool) or not isinstance(scalar, int) or scalar <= 0:
        return None
    if "environment_ids" not in template:
        return [scalar]
    values = template["environment_ids"]
    if (not isinstance(values, list) or len(values) != 1
            or isinstance(values[0], bool) or not isinstance(values[0], int)
            or values[0] <= 0 or values[0] != scalar):
        return None
    return values


def template_environment_memberships(template):
    """Return all valid effective group memberships for ownership checks."""
    if not isinstance(template, dict):
        return None
    scalar = template.get("environment_id")
    if isinstance(scalar, bool) or not isinstance(scalar, int) or scalar <= 0:
        return None
    if "environment_ids" not in template:
        return [scalar]
    values = template["environment_ids"]
    if (not isinstance(values, list) or not values
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values)
            or len(set(values)) != len(values)):
        return None
    return values


def publication_binding_matches(expected, actual, environment_id):
    """Compare full template settings, tolerating only an omitted legacy ID array."""
    if (not isinstance(expected, dict) or not isinstance(actual, dict)
            or isinstance(environment_id, bool) or not isinstance(environment_id, int)
            or environment_id <= 0
            or effective_environment_ids(expected) != [environment_id]
            or effective_environment_ids(actual) != [environment_id]):
        return False
    expected_settings = publication_settings(expected)
    actual_settings = publication_settings(actual)
    if "environment_ids" not in actual_settings:
        actual_settings = {**actual_settings, "environment_ids": [actual_settings["environment_id"]]}
    return expected_settings == actual_settings


def survey_enum_option_problems(templates):
    """Return schema errors for enum choices in the templates being published.

    Semaphore's SurveyVar.Values field is []SurveyVarEnumValue, whose JSON object
    has string `name` and `value` fields. Refuse malformed declarations before
    resolving credentials or sending any request; silently coercing strings here
    would hide a broken catalog/API contract.
    """
    problems = []
    if not isinstance(templates, list):
        return ["selected template declarations are not a list"]

    for template in templates:
        if not isinstance(template, dict):
            problems.append("selected template declaration is not an object")
            continue
        surveys = template.get("survey_vars", [])
        template_name = template.get("_publish_name", template.get("name", "<unnamed>"))
        if not isinstance(surveys, list):
            problems.append(f"{template_name}: survey_vars is not a list")
            continue
        for survey in surveys:
            if not isinstance(survey, dict):
                problems.append(f"{template_name}: survey variable is not an object")
                continue
            if survey.get("type") != "enum":
                continue
            survey_name = survey.get("name", "<unnamed>")
            values = survey.get("values")
            if not isinstance(values, list) or not values:
                problems.append(f"{template_name}.{survey_name}: enum values must be a nonempty list")
                continue
            for index, option in enumerate(values):
                if (not isinstance(option, dict) or set(option) != {"name", "value"}
                        or not isinstance(option.get("name"), str)
                        or not isinstance(option.get("value"), str)):
                    problems.append(
                        f"{template_name}.{survey_name}: enum option {index} must have string name and value fields"
                    )
    return problems


SEED_AUTH_INPUTS = ("BAO_ROLE_ID", "BAO_SECRET_ID")


def seed_environment_problems(environment, legacy_pin_ok=False):
    """Why a dedicated seed environment is not safe to bind a seed template to.

    The ONE definition of a clean isolated seed environment, shared by full publication
    (tasks/isolated-environments.yml), the provisioner (provision-seed-environment.yml) and
    the seed CLIs' preflight (scripts/semaphore_seed.py): before 2026-09-23 each had its own
    rule and they disagreed (reviews of PR #205).
    Clean means: no plaintext env vars, NO extra-var JSON, and encrypted inputs that are
    exactly the two AppRole inputs or none, each once, as env type. Anything else (a
    leftover BAO_VALUE or SEED_* value, a lone AppRole half) would reach a bound template.

    No `openbao_addr` either (2026-09-25): the address comes from the inventory's
    `all.vars`, and the seed run refuses one that differs from that declaration
    (tasks/assert-bao-addr-declared.yml). An extra var in the environment would override
    the inventory, so a pin is itself the problem. `legacy_pin_ok` lets the provisioner,
    and only it, accept the pin earlier versions wrote, because it removes it.
    Returns names only, never values; empty means clean.
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
        problems.append("extra-var JSON present")
    if extra and "openbao_addr" in extra and not legacy_pin_ok:
        problems.append("OpenBao address pinned in the environment (it comes from the inventory); "
                        "run Provision Seed Environment to remove the pin")
    # The single-environment GET fills metadata before returning, but v2.18.12 omits
    # an empty list from JSON. Never pass a row from the environment LIST endpoint:
    # that endpoint does not load secret metadata. Null or malformed lists still fail.
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
                "effective_environment_ids": effective_environment_ids,
                "template_environment_memberships": template_environment_memberships,
                "publication_binding_matches": publication_binding_matches,
                "seed_environment_problems": seed_environment_problems,
                "survey_enum_option_problems": survey_enum_option_problems}
