"""The Request a service catalog (plan 17, task 8.4, D38): config/catalog.json, rendered at deploy.

The file is built from the deploy templates in platform/semaphore/templates.yml.

The rule that picks the services is pinned where it can break:

- catalog.json.j2 keeps a template named `Deploy <name>` whose playbook basename is `deploy-<service>.yml`, drops
  `Deploy All Services`, strips a trailing " (Dev)" from the display name, and counts each service's hosts in the
  inventory group `<service with - as _>_svc` (0 when that group does not exist).
- The rule is checked two ways. An independent Python oracle states it over the real templates.yml, and the deploy's
  own render expression runs through ansible-playbook, the way test_console_phase3_creds.py renders gateway.json.
  Fixture templates exercise each edge the real file does not.
- The file is always rendered, mounted read-only, checked and hashed by deploy.sh, and named by env.j2 on every
  deploy. templates.yml is a D31 pinned input, so a rotation refuses while the controller's copy differs.
- The server's CatalogEntry struct and the rendered keys agree.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CONSOLE = REPO / "platform/services/console/deployment"
APP_SERVER = REPO / "platform/services/console/app/src/server"
DEPLOY = REPO / "platform/playbooks/deploy-console.yml"
ROTATE = REPO / "platform/playbooks/rotate-console-credentials.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
CATALOG_J2 = CONSOLE / "templates/catalog.json.j2"

NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _deploy_vars() -> dict:
    return next(p for p in playbook_yaml.plays(DEPLOY) if "_secret_definitions" in (p.get("vars") or {}))["vars"]


def _oracle(templates: list, host_counts: dict) -> dict:
    """The rule as a specification: what catalog.json must hold for these templates and inventory host counts."""
    entries = []
    for template in templates:
        name = template.get("name", "")
        base = re.sub(r" \(Dev\)$", "", name)
        label = re.fullmatch(r"Deploy (.+)", base)
        playbook = re.fullmatch(r"deploy-(.+)\.yml", os.path.basename(template.get("playbook", "") or ""))
        if not label or not playbook or base == "Deploy All Services":
            continue
        service = playbook.group(1)
        entries.append({
            "service": service,
            "name": label.group(1),
            "template": name,
            "deployed_hosts": host_counts.get(service.replace("-", "_") + "_svc", 0),
        })
    return {"services": sorted(entries, key=lambda entry: entry["name"].lower())}


def _render_catalog(tmp_path: Path, templates, host_counts: dict) -> dict:
    """Renders catalog.json.j2 through ansible-playbook and returns the parsed file.

    `templates` None runs the deploy's own _console_catalog_templates expression, which reads
    <playbook dir>/../semaphore/templates.yml: the case directory holds a copy of the real file there. A list is
    passed in its place. `host_counts` gives each inventory group its host count; a group absent here does not exist.
    """
    case = Path(tempfile.mkdtemp(dir=tmp_path))
    playbooks = case / "playbooks"
    playbooks.mkdir()
    inventory = {"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}}
    if host_counts:
        inventory["all"]["children"] = {
            group: {"hosts": {f"{group}-{index}": {"ansible_connection": "local"} for index in range(count)}}
            for group, count in host_counts.items()
        }
    if templates is None:
        (case / "semaphore").mkdir()
        (case / "semaphore/templates.yml").write_text(TEMPLATES.read_text())
        variables = {"_console_catalog_templates": _deploy_vars()["_console_catalog_templates"]}
    else:
        variables = {"_console_catalog_templates": templates}
    out = case / "catalog.json"
    plays = [{"name": "render the catalog", "hosts": "localhost", "connection": "local", "gather_facts": False,
              "vars": variables,
              "tasks": [{"ansible.builtin.template": {"src": str(CATALOG_J2), "dest": str(out), "mode": "0644"}}]}]
    inv = case / "inventory.yml"
    inv.write_text(yaml.safe_dump(inventory))
    plays_path = playbooks / "plays.yml"
    plays_path.write_text(yaml.safe_dump(plays))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(["ansible-playbook", "-i", str(inv), str(plays_path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(out.read_text())


# --- wiring: always rendered, mounted, checked, hashed, named, and a drift input ----------------------------------


def test_the_deploy_renders_the_catalog_from_the_deploy_templates_on_the_controller():
    v = _deploy_vars()
    assert "playbook_dir ~ '/../semaphore/templates.yml'" in v["_console_catalog_templates"]
    assert "| from_yaml).templates" in v["_console_catalog_templates"]
    templates = {t["dest"]: t["src"] for t in v["_env_templates"]}
    assert templates["config/catalog.json"] == "catalog.json.j2"


def test_env_names_the_catalog_file_on_every_deploy_and_not_inside_a_conditional():
    text = (CONSOLE / "templates/env.j2").read_text()
    line = "CONSOLE_CATALOG_FILE=/etc/console/catalog.json"
    assert line in text.splitlines()
    before = text[: text.index(line)]
    opened = len(re.findall(r"\{%-?\s*if\b", before))
    closed = len(re.findall(r"\{%-?\s*endif\s*-?%\}", before))
    assert opened == closed, "CONSOLE_CATALOG_FILE sits inside a Jinja conditional"


def test_compose_mounts_the_catalog_read_only():
    assert "      - ./config/catalog.json:/etc/console/catalog.json:ro" in (CONSOLE / "compose.yml").read_text()


def test_deploy_sh_refuses_a_missing_catalog_and_hashes_it_into_both_up_branches():
    text = (CONSOLE / "deploy.sh").read_text()
    assert "config/catalog.json not found. Run Ansible deploy-console.yml first." in text
    up_lines = [ln for ln in text.splitlines() if "compose_up_if_changed config/" in ln]
    assert len(up_lines) == 2, up_lines
    for line in up_lines:
        tokens = line.split()
        assert tokens.count("config/catalog.json") == 1, line
        assert tokens.index("config/catalog.json") > tokens.index("config/role-map.json"), line


def test_the_rotation_pins_the_deploy_templates_the_catalog_reads():
    assert "          - platform/semaphore/templates.yml" in ROTATE.read_text()


# --- the server's struct and the rendered keys agree --------------------------------------------------------------


def test_rendered_catalog_matches_the_servers_struct():
    # Same contract check as the gateway file: the server refuses an unknown key at startup, so the template must
    # render exactly the declared fields and nothing else.
    src = (APP_SERVER / "catalog.rs").read_text()
    entry = re.search(r"pub struct CatalogEntry \{(.*?)\n\}", src, re.S)
    assert entry, "CatalogEntry not found in catalog.rs"
    assert re.findall(r"pub (\w+):", entry.group(1)) == ["service", "name", "template", "deployed_hosts"]
    file_struct = re.search(r"pub struct CatalogFile \{(.*?)\n\}", src, re.S)
    assert file_struct, "CatalogFile not found in catalog.rs"
    assert re.findall(r"pub (\w+):", file_struct.group(1)) == ["services"]
    assert src.count("#[serde(deny_unknown_fields)]") >= 2


# --- the rule: the real templates, then the edges the real file does not carry ----------------------------------


@NEEDS_ANSIBLE
def test_the_real_templates_render_to_what_the_rule_states(tmp_path):
    host_counts = {"step_ca_svc": 1, "github_runner_svc": 2, "inference_comfyui_svc": 1}
    body = _render_catalog(tmp_path, None, host_counts)
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    assert body == _oracle(templates, host_counts)


@NEEDS_ANSIBLE
def test_the_real_templates_pin_the_exclusion_the_strip_and_the_group_mapping(tmp_path):
    body = _render_catalog(tmp_path, None, {"step_ca_svc": 1, "github_runner_svc": 2, "inference_comfyui_svc": 1})
    by_template = {e["template"]: e for e in body["services"]}
    # Deploy All Services deploys every service, so it is not one of them.
    assert "Deploy All Services" not in by_template
    # A Dev twin names the service it deploys, without the " (Dev)" suffix.
    assert by_template["Deploy step-ca (Dev)"] == {
        "service": "step-ca", "name": "step-ca", "template": "Deploy step-ca (Dev)", "deployed_hosts": 1}
    # A hyphenated service maps to its group with underscores.
    assert by_template["Deploy ComfyUI Sidecar"] == {
        "service": "inference-comfyui", "name": "ComfyUI Sidecar", "template": "Deploy ComfyUI Sidecar",
        "deployed_hosts": 1}
    assert by_template["Deploy GitHub Runner"]["deployed_hosts"] == 2
    assert by_template["Deploy NetBox"]["deployed_hosts"] == 0


@NEEDS_ANSIBLE
def test_the_rule_on_fixture_templates_covers_each_edge(tmp_path):
    templates = [
        {"name": "Deploy Alpha", "playbook": "platform/playbooks/deploy-alpha.yml"},
        {"name": "Deploy bravo (Dev)", "playbook": "platform/playbooks/deploy-bravo.yml"},
        {"name": "Deploy All Services", "playbook": "platform/playbooks/deploy-all.yml"},
        {"name": "Deploy All Services (Dev)", "playbook": "platform/playbooks/deploy-all.yml"},
        {"name": "Deploy Charlie", "playbook": "platform/playbooks/charlie.yml"},
        {"name": "Grow Delta", "playbook": "platform/playbooks/deploy-delta.yml"},
        {"name": "Deploy Echo Foxtrot", "playbook": "platform/playbooks/deploy-echo-foxtrot.yml"},
    ]
    host_counts = {"echo_foxtrot_svc": 3}
    body = _render_catalog(tmp_path, templates, host_counts)
    assert body == _oracle(templates, host_counts)
    assert [e["template"] for e in body["services"]] == ["Deploy Alpha", "Deploy bravo (Dev)", "Deploy Echo Foxtrot"]
    by_template = {e["template"]: e for e in body["services"]}
    assert by_template["Deploy Echo Foxtrot"] == {
        "service": "echo-foxtrot", "name": "Echo Foxtrot", "template": "Deploy Echo Foxtrot", "deployed_hosts": 3}
    assert by_template["Deploy bravo (Dev)"]["name"] == "bravo"
    assert by_template["Deploy Alpha"]["deployed_hosts"] == 0, "a group the inventory lacks counts as no hosts"


@NEEDS_ANSIBLE
def test_the_list_sorts_by_name_ignoring_case(tmp_path):
    # Case-insensitive: a case-sensitive sort would put "bravo" after "Echo Foxtrot".
    templates = [
        {"name": "Deploy Echo Foxtrot", "playbook": "platform/playbooks/deploy-echo-foxtrot.yml"},
        {"name": "Deploy bravo", "playbook": "platform/playbooks/deploy-bravo.yml"},
        {"name": "Deploy Alpha", "playbook": "platform/playbooks/deploy-alpha.yml"},
    ]
    body = _render_catalog(tmp_path, templates, {})
    assert [e["name"] for e in body["services"]] == ["Alpha", "bravo", "Echo Foxtrot"]


@NEEDS_ANSIBLE
def test_no_qualifying_template_renders_an_empty_list(tmp_path):
    only_excluded = [{"name": "Deploy All Services", "playbook": "platform/playbooks/deploy-all.yml"}]
    assert _render_catalog(tmp_path, only_excluded, {}) == {"services": []}
    assert _render_catalog(tmp_path, [], {}) == {"services": []}
