"""The Dispatch Console scope contract (plan 17, task 3.3): the template, the fixture and the server agree.

The server reads CONSOLE_SCOPE_FILE, which deploy-console.yml renders from scope.json.j2 and the inventory's
console_tenants. One committed fixture, app/tests/fixtures/scope.example.json, joins the two sides:

- This file renders the template from the example inventory through the real Ansible template engine (the
  harness test_console_phase3_creds.py renders every console template with), and requires the fixture to equal it.
- The Rust test `the_rendered_example_scope_is_served` (app/src/server/mod.rs) loads the same fixture through the
  server's own load(), and serves it on /v1/tenants and /v1/tenants/{t}/tree.

The two sides are compared as parsed JSON, so the fixture's whitespace is not what is pinned.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from test_console_phase3_creds import _render

REPO = Path(__file__).resolve().parents[2]
CONSOLE = REPO / "platform/services/console"
TEMPLATE = CONSOLE / "deployment/templates/scope.json.j2"
FIXTURE = CONSOLE / "app/tests/fixtures/scope.example.json"
EXAMPLE_INVENTORY = REPO / "platform/inventory/local-dev.yml.example"

NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


@NEEDS_ANSIBLE
def test_scope_fixture_is_the_example_inventory_rendered(tmp_path):
    inventory = yaml.safe_load(EXAMPLE_INVENTORY.read_text())
    host = inventory["all"]["children"]["console_svc"]["hosts"]["console-local"]
    variables = {name: host[name] for name in ("console_scope_is_example", "console_tenants")}
    rendered = _render(tmp_path, TEMPLATE, variables)
    assert json.loads(rendered) == json.loads(FIXTURE.read_text()), (
        f"{FIXTURE.relative_to(REPO)} is not what {TEMPLATE.name} renders from {EXAMPLE_INVENTORY.name}. "
        "Regenerate it: write the rendered text below to the fixture, with one trailing newline, "
        "then re-run this test.\n" + rendered
    )
