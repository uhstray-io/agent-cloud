"""The launch gate's template allowlist, derived from OPA's catalog (skynet-launch-containment, design decision 3).

The gate may launch only the templates that the agent identities its callers act as are allowed, plus
their (Dev) variants. OPA's data file stays the one source: this list only narrows what reaches OPA, and it
never names a template OPA does not. It is the union over every agent the callers act as, and it keeps the
base name beside each (Dev) variant, so it admits names OPA still refuses for a given agent or branch (another
agent's template; a base name whose workflow step is unreviewed and so may not run from main). That is by
design: per-agent and per-branch permission is OPA's decision, made on every launch; the allowlist only keeps
everything outside the catalog, and every destructive template, away from OPA.

Two kinds of template never reach the gate's config:

  * a name in catalog.semaphore.destructive_templates, and
  * any name that starts with "Clean Deploy" (the destructive family, whether or not it is listed).

Fail closed. An empty agent list, an agent that is not in the catalog, an agent with no allowed_templates
list, a name that templates.yml does not declare (catalog drift), or a template name declared twice raises
AnsibleFilterError, which fails the render. The caller must not catch it: a shorter or stale list must never
be rendered. Tested directly in platform/tests/test_launch_gate_allowlist.py.
"""

from collections.abc import Mapping

from ansible.errors import AnsibleFilterError

CLEAN_DEPLOY_PREFIX = "Clean Deploy"
DEV_SUFFIX = " (Dev)"


def launch_gate_allowlist(catalog_doc, templates_doc, agents):
    """Sorted, unique template names the gate may launch for these agent identities.

    catalog_doc    the parsed OPA data document (policies/agentcloud/data.json): {"catalog": {...}}
    templates_doc  the parsed platform/semaphore/templates.yml: {"templates": [...]}
    agents         the agent identities the gate's callers may act as, e.g. ["security-agent"]
    """
    catalog = _section(catalog_doc, "catalog", "catalog_doc")
    dev_variants = _dev_variants(templates_doc)
    agent_names = _strings(agents, "agents")
    if not agent_names:
        raise AnsibleFilterError("agents must name at least one agent identity")

    destructive = set(
        _strings(
            _section(catalog, "semaphore", "catalog").get("destructive_templates"),
            "catalog.semaphore.destructive_templates",
        )
    )

    named = []
    for agent in agent_names:
        spec = catalog.get(agent)
        if not isinstance(spec, Mapping):
            raise AnsibleFilterError(f"agent {agent!r} is not in the OPA catalog")
        if not isinstance(spec.get("allowed_templates"), list):
            raise AnsibleFilterError(f"agent {agent!r} has no allowed_templates list in the OPA catalog")
        named += _strings(spec["allowed_templates"], f"catalog.{agent}.allowed_templates")
        named += _strings(spec.get("service_deploy_templates", []), f"catalog.{agent}.service_deploy_templates")

    kept = {name for name in named if name not in destructive and not name.startswith(CLEAN_DEPLOY_PREFIX)}

    undeclared = sorted(name for name in kept if name not in dev_variants)
    if undeclared:
        raise AnsibleFilterError(
            "the OPA catalog names template(s) that templates.yml does not declare: " + ", ".join(undeclared)
        )

    out = set(kept)
    out.update(name + DEV_SUFFIX for name in kept if dev_variants[name])
    return sorted(out)


def _section(doc, key, what):
    if not isinstance(doc, Mapping) or not isinstance(doc.get(key), Mapping):
        raise AnsibleFilterError(f"{what} has no {key!r} mapping")
    return doc[key]


def _strings(value, what):
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise AnsibleFilterError(f"{what} must be a list of non-empty strings")
    return value


def _dev_variants(templates_doc):
    """Template name -> whether templates.yml generates a (Dev) variant for it. A duplicate name is refused."""
    templates = templates_doc.get("templates") if isinstance(templates_doc, Mapping) else None
    if not isinstance(templates, list):
        raise AnsibleFilterError("templates_doc has no templates list (is it templates.yml?)")
    out = {}
    for entry in templates:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str) or not entry["name"]:
            raise AnsibleFilterError("every templates.yml entry needs a name")
        name = entry["name"]
        if name in out:
            raise AnsibleFilterError(f"template name {name!r} is declared twice in templates.yml")
        out[name] = bool(entry.get("dev_variant", False))
    return out


class FilterModule:
    def filters(self):
        return {"launch_gate_allowlist": launch_gate_allowlist}
