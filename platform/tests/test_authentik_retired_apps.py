"""Retired SSO apps are torn down, not orphaned (blueprints/retired-apps.yaml).

Blueprints create objects and never delete them. Taking an app out of app-catalog.yml, or out of
site-config's `authentik_apps`, only stops its blueprint being placed: the provider and the
application it created stay in the live Authentik. The tombstone blueprint is what deletes them,
so it must be placed on every deploy (not per app), must hold deletions only, and must name each
object by the identifiers its original blueprint used. A blueprint file that no deploy places is
dead config, so that is checked too.

The entries pin tududi, the first app retired this way. When its tombstone has run in prod, the
entries may be removed, and these tests change with them.
"""

from pathlib import Path

import playbook_yaml
import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "platform/services/authentik/deployment"
BLUEPRINTS = DEPLOY / "blueprints"
RETIRED = BLUEPRINTS / "retired-apps.yaml"
CATALOG = DEPLOY / "app-catalog.yml"
ENV_TEMPLATE = DEPLOY / "templates/env.j2"
SECRET_DECLARATIONS = REPO / "platform/playbooks/vars/secret-declarations/authentik.yml"
DEPLOY_PLAYBOOK = REPO / "platform/playbooks/deploy-authentik.yml"


def _shared_blueprints() -> list[str]:
    declaring = [
        play["vars"]["_shared_blueprints"]
        for play in playbook_yaml.plays(DEPLOY_PLAYBOOK)
        if "_shared_blueprints" in (play.get("vars") or {})
    ]
    assert len(declaring) == 1, "deploy-authentik.yml must declare _shared_blueprints in exactly one play"
    return declaring[0]


def _catalog() -> dict:
    return yaml.safe_load(CATALOG.read_text())["authentik_app_catalog"]


def _entries() -> list[dict]:
    return yaml.safe_load(RETIRED.read_text())["entries"]


def test_tombstone_is_placed_on_every_deploy():
    assert "retired-apps.yaml" in _shared_blueprints()
    assert "retired-apps.yaml" not in {app["file"] for app in _catalog().values()}


def test_every_tombstone_entry_only_deletes():
    entries = _entries()
    assert entries, "retired-apps.yaml has no entries"
    not_deleting = [(e.get("model"), e.get("state")) for e in entries if e.get("state") != "absent"]
    assert not not_deleting, f"a tombstone may only delete; these entries would create or update: {not_deleting}"
    for e in entries:
        assert e.get("identifiers"), f"{e.get('model')}: an absent entry must name its object by identifiers"


def test_tududi_application_and_provider_are_tombstoned_application_first():
    keys = [(e["model"], tuple(sorted(e["identifiers"].items()))) for e in _entries()]
    application = ("authentik_core.application", (("slug", "tududi"),))
    provider = ("authentik_providers_oauth2.oauth2provider", (("name", "tududi"),))
    assert application in keys, f"no absent entry for the tududi application: {keys}"
    assert provider in keys, f"no absent entry for the tududi provider: {keys}"
    assert keys.index(application) < keys.index(provider), (
        "the application references the provider, so it is deleted first"
    )


def test_tududi_has_no_catalog_entry_blueprint_env_or_secret_left():
    assert "tududi" not in _catalog()
    assert not list(BLUEPRINTS.glob("*tududi*"))
    assert "TUDUDI_" not in ENV_TEMPLATE.read_text()
    assert "tududi_oidc_client_secret" not in SECRET_DECLARATIONS.read_text()


def test_every_blueprint_file_is_placed_by_a_deploy():
    shipped = {p.name for p in BLUEPRINTS.glob("*.yaml")}
    placed = set(_shared_blueprints()) | {app["file"] for app in _catalog().values()}
    orphans = sorted(shipped - placed)
    assert not orphans, f"blueprint files no deploy places (dead config): {orphans}"
    unshipped = sorted(placed - shipped)
    assert not unshipped, f"placed by a deploy but missing from blueprints/: {unshipped}"
