"""A dev-bound template that the OPA catalog names must carry its " (Dev)" or " (Local)" suffix.

agent_actions.rego decides a template's branch from its NAME alone (_off_main_template): a name
ending " (Dev)" or " (Local)" is off main, any other name is main. Semaphore binds a template to the
dev repository record with `repository: agent-cloud dev`. A template bound that way but named with
no suffix would be judged as main by the policy, so the agent catalog (data.json) must not name one.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "platform/semaphore/templates.yml"
CATALOG = REPO / "platform/services/opa/deployment/policies/agentcloud/data.json"
DEV_REPOSITORY = "agent-cloud dev"
DEV_SUFFIXES = (" (Dev)", " (Local)")


def _strings(node):
    """Every string in a parsed JSON value, dict keys included."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def offenders(templates, catalog):
    """Sorted names of dev-bound templates with no dev suffix that the catalog also names."""
    unsuffixed_dev = {
        template["name"]
        for template in templates
        if template.get("repository") == DEV_REPOSITORY and not template["name"].endswith(DEV_SUFFIXES)
    }
    return sorted(unsuffixed_dev & set(_strings(catalog)))


def named_in_catalog(templates, catalog):
    """Template names the catalog names at all, whatever their branch."""
    return {template["name"] for template in templates} & set(_strings(catalog))


def test_catalog_names_no_unsuffixed_dev_bound_template():
    templates = yaml.safe_load(TEMPLATES.read_text(encoding="utf-8"))["templates"]
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    # Non-vacuity: the walk must find real template names in the catalog, or an empty walk would pass.
    assert "Harden SSH" in named_in_catalog(templates, catalog)
    found = offenders(templates, catalog)
    assert found == [], (
        f"dev-bound templates named without a dev suffix are in the OPA catalog: {found}. The policy reads "
        "a name with no ' (Dev)' or ' (Local)' suffix as main. Keep the base name in data.json (the policy "
        "strips the suffix) and either give the dev-bound template a ' (Dev)' name in templates.yml or a "
        "dev_variant so setup-templates.yml generates one, or keep the template out of data.json"
    )


def test_check_flags_an_unsuffixed_dev_bound_name_in_a_synthetic_catalog():
    templates = [
        {"name": "Probe Dev Thing", "repository": DEV_REPOSITORY},
        {"name": "Probe Main Thing", "repository": "agent-cloud"},
    ]
    catalog = {
        "agentcloud": {"catalog": {"o11y-agent": {"allowed_templates": ["Probe Dev Thing", "Probe Main Thing"]}}}
    }
    assert offenders(templates, catalog) == ["Probe Dev Thing"]


def test_check_flags_an_offender_named_only_as_a_catalog_key():
    templates = [{"name": "Probe Dev Thing", "repository": DEV_REPOSITORY}]
    catalog = {"workflow_steps": {"probe-step": {"templates": []}}, "Probe Dev Thing": {}}
    assert offenders(templates, catalog) == ["Probe Dev Thing"]


def test_check_passes_a_suffixed_dev_bound_name():
    templates = [{"name": "Probe Thing (Dev)", "repository": DEV_REPOSITORY}]
    catalog = {"o11y-agent": {"allowed_templates": ["Probe Thing (Dev)"]}}
    assert offenders(templates, catalog) == []
