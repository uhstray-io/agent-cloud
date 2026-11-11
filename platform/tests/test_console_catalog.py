"""The Request a service catalog (plan 17, task 8.4, D38): config/catalog.json, rendered at deploy.

The file is built from the deploy templates in platform/semaphore/templates.yml and the deploy playbooks beside the
deploy. The rule that picks the services and counts their hosts is pinned where it can break:

- catalog.json.j2 keeps a template named `Deploy <name>` whose playbook basename is `deploy-<service>.yml`, drops
  `Deploy All Services`, and strips a trailing " (Dev)" from the display name.
- One entry per service. When two templates name one service, the one without " (Dev)" is kept, else the first.
- deployed_hosts counts the hosts of the group the service's playbook targets: the `hosts:` of its first play that is a
  literal `<name>_svc`. Otherwise the group is `<service with - as _>_svc`. A group the inventory lacks counts as 0.
- The rule is checked two ways. An independent Python oracle states it over the real files, and the deploy's own render
  expression runs through ansible-playbook, the way test_console_phase3_creds.py renders gateway.json. Fixtures cover
  the edges the real files do not carry.
- The file is always rendered, mounted read-only, checked and hashed by deploy.sh, and named by env.j2 on every deploy.
  templates.yml is deliberately NOT a rotation pin: it changes often and the catalog holds names and counts, not code.
- The server's CatalogEntry struct and the rendered keys agree.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CONSOLE = REPO / "platform/services/console/deployment"
APP_SERVER = REPO / "platform/services/console/app/src/server"
PLAYBOOKS = REPO / "platform/playbooks"
DEPLOY = PLAYBOOKS / "deploy-console.yml"
ROTATE = PLAYBOOKS / "rotate-console-credentials.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
CATALOG_J2 = CONSOLE / "templates/catalog.json.j2"

NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _deploy_vars() -> dict:
    return next(p for p in playbook_yaml.plays(DEPLOY) if "_secret_definitions" in (p.get("vars") or {}))["vars"]


def _playbook_group(path: Path):
    """The group the first play names with a literal `<name>_svc` hosts line, or None (the rule's first clause)."""
    if not path.is_file():
        return None
    for play in yaml.safe_load(path.read_text()) or []:
        hosts = play.get("hosts") if isinstance(play, dict) else None
        if isinstance(hosts, str) and re.fullmatch(r"[a-z0-9_]+_svc", hosts):
            return hosts
    return None


def _oracle(templates: list, host_counts: dict, playbooks: Path) -> dict:
    """The rule as a specification: what catalog.json must hold for these templates, playbooks and host counts."""
    candidates = {}
    for template in templates:
        name = template.get("name", "")
        base = re.sub(r" \(Dev\)$", "", name)
        label = re.fullmatch(r"Deploy (.+)", base)
        playbook = re.fullmatch(r"deploy-(.+)\.yml", os.path.basename(template.get("playbook", "") or ""))
        if not label or not playbook or base == "Deploy All Services":
            continue
        service = playbook.group(1)
        group = _playbook_group(playbooks / f"deploy-{service}.yml") or service.replace("-", "_") + "_svc"
        candidates.setdefault(service, []).append({
            "entry": {"service": service, "name": label.group(1), "template": name,
                      "deployed_hosts": host_counts.get(group, 0)},
            "dev": name.endswith(" (Dev)"),
        })
    entries = []
    for options in candidates.values():
        kept = next((option for option in options if not option["dev"]), options[0])
        entries.append(kept["entry"])
    return {"services": sorted(entries, key=lambda entry: entry["name"].lower())}


def _render_catalog(tmp_path: Path, templates, host_counts: dict, playbooks: Path) -> dict:
    """Renders catalog.json.j2 through ansible-playbook and returns the parsed file.

    `templates` None runs the deploy's own _console_catalog_templates expression, which reads
    <playbook dir>/../semaphore/templates.yml: the case directory holds a copy of the real file there. A list is
    passed in its place. The case's playbook directory holds every deploy-*.yml from `playbooks`, which is where the
    template reads each service's hosts line. `host_counts` gives each inventory group its host count.
    """
    case = Path(tempfile.mkdtemp(dir=tmp_path))
    case_playbooks = case / "playbooks"
    case_playbooks.mkdir()
    for source in playbooks.glob("deploy-*.yml"):
        shutil.copy(source, case_playbooks / source.name)
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
    plays_path = case_playbooks / "plays.yml"
    plays_path.write_text(yaml.safe_dump(plays))
    proc = harness_sandbox.run_playbook(plays_path, case, inventory=inv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(out.read_text())


def _dir(tmp_path: Path, name: str = "no-playbooks") -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- wiring: always rendered, mounted, checked, hashed, named, and NOT a rotation pin ----------------------------


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


def test_the_rotation_does_not_pin_the_deploy_templates():
    # Decision (review of #863): a pin here would make the monthly rotation refuse while templates.yml changes weekly.
    assert "platform/semaphore/templates.yml" not in ROTATE.read_text()
    assert "NOT a rotation pin" in (PLAYBOOKS / "deploy-console.yml").read_text()


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


# --- the real files ----------------------------------------------------------------------------------------------


@NEEDS_ANSIBLE
def test_the_real_templates_render_to_what_the_rule_states(tmp_path):
    # Non-zero counts for the groups the three named playbooks target, and for their name-rule decoys, so the
    # differential compares real numbers rather than zeros.
    host_counts = {
        "step_ca_svc": 1, "github_runner_svc": 2, "inference_comfyui_svc": 1,
        "netbox_svc": 3, "plane_svc": 2, "o11y_svc": 4,
        "orb_agent_svc": 9, "plane_mcp_svc": 9, "o11y_journal_collector_svc": 9,
    }
    body = _render_catalog(tmp_path, None, host_counts, PLAYBOOKS)
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    assert body == _oracle(templates, host_counts, PLAYBOOKS)


@NEEDS_ANSIBLE
def test_the_real_templates_pin_the_exclusion_the_strip_and_the_group_mapping(tmp_path):
    body = _render_catalog(tmp_path, None, {"step_ca_svc": 1, "github_runner_svc": 2, "inference_comfyui_svc": 1},
                           PLAYBOOKS)
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
def test_the_three_playbooks_that_target_another_group_count_that_group(tmp_path):
    # Each decoy group (the name rule's answer) carries 9 hosts, so a count from the wrong group fails the test.
    host_counts = {
        "netbox_svc": 3, "plane_svc": 2, "o11y_svc": 4,
        "orb_agent_svc": 9, "plane_mcp_svc": 9, "o11y_journal_collector_svc": 9,
    }
    body = _render_catalog(tmp_path, None, host_counts, PLAYBOOKS)
    by_service = {e["service"]: e for e in body["services"]}
    assert by_service["orb-agent"]["deployed_hosts"] == 3, "Orb Agent deploys to netbox_svc"
    assert by_service["plane-mcp"]["deployed_hosts"] == 2, "Plane MCP deploys to plane_svc"
    assert by_service["o11y-journal-collector"]["deployed_hosts"] == 4, "o11y Journal Collector deploys to o11y_svc"


@NEEDS_ANSIBLE
def test_the_real_templates_have_one_entry_per_service(tmp_path):
    body = _render_catalog(tmp_path, None, {}, PLAYBOOKS)
    services = [e["service"] for e in body["services"]]
    assert len(services) == len(set(services)), services


# --- the rule on fixtures: the edges the real files do not carry --------------------------------------------------


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
    # No fixture playbook exists, so every service takes the name rule: a missing playbook is not a failure.
    body = _render_catalog(tmp_path, templates, host_counts, _dir(tmp_path))
    assert body == _oracle(templates, host_counts, _dir(tmp_path))
    assert [e["template"] for e in body["services"]] == ["Deploy Alpha", "Deploy bravo (Dev)", "Deploy Echo Foxtrot"]
    by_template = {e["template"]: e for e in body["services"]}
    assert by_template["Deploy Echo Foxtrot"] == {
        "service": "echo-foxtrot", "name": "Echo Foxtrot", "template": "Deploy Echo Foxtrot", "deployed_hosts": 3}
    assert by_template["Deploy bravo (Dev)"]["name"] == "bravo"
    assert by_template["Deploy Alpha"]["deployed_hosts"] == 0, "a group the inventory lacks counts as no hosts"


@NEEDS_ANSIBLE
def test_a_templated_hosts_line_falls_back_to_the_name_rule(tmp_path):
    playbooks = _dir(tmp_path, "templated")
    (playbooks / "deploy-templated.yml").write_text(
        "- name: target\n  hosts: \"{{ target | default('templated_svc') }}\"\n  tasks: []\n"
        "- name: local\n  hosts: localhost\n  tasks: []\n")
    templates = [{"name": "Deploy Templated", "playbook": "platform/playbooks/deploy-templated.yml"}]
    host_counts = {"templated_svc": 2}
    body = _render_catalog(tmp_path, templates, host_counts, playbooks)
    assert body == _oracle(templates, host_counts, playbooks)
    assert body["services"] == [
        {"service": "templated", "name": "Templated", "template": "Deploy Templated", "deployed_hosts": 2}]


@NEEDS_ANSIBLE
def test_the_first_literal_group_wins_over_a_later_one_and_over_the_name_rule(tmp_path):
    playbooks = _dir(tmp_path, "mike")
    (playbooks / "deploy-mike.yml").write_text(
        "- name: templated first\n  hosts: \"{{ target }}\"\n  tasks: []\n"
        "- name: first literal\n  hosts: mike_real_svc\n  tasks: []\n"
        "- name: a later literal\n  hosts: mike_later_svc\n  tasks: []\n")
    templates = [{"name": "Deploy Mike", "playbook": "platform/playbooks/deploy-mike.yml"}]
    host_counts = {"mike_svc": 9, "mike_real_svc": 2, "mike_later_svc": 5}
    body = _render_catalog(tmp_path, templates, host_counts, playbooks)
    assert body == _oracle(templates, host_counts, playbooks)
    assert body["services"][0]["deployed_hosts"] == 2


@NEEDS_ANSIBLE
def test_one_entry_per_service_keeps_the_template_without_dev_whatever_the_order(tmp_path):
    # Kilo's Dev twin is listed first: the template without " (Dev)" still wins. Lima has only a Dev twin, which stays.
    templates = [
        {"name": "Deploy Kilo (Dev)", "playbook": "platform/playbooks/deploy-kilo.yml"},
        {"name": "Deploy Lima (Dev)", "playbook": "platform/playbooks/deploy-lima.yml"},
        {"name": "Deploy Kilo", "playbook": "platform/playbooks/deploy-kilo.yml"},
    ]
    body = _render_catalog(tmp_path, templates, {}, _dir(tmp_path))
    assert body == _oracle(templates, {}, _dir(tmp_path))
    assert [(e["service"], e["template"]) for e in body["services"]] == [
        ("kilo", "Deploy Kilo"), ("lima", "Deploy Lima (Dev)")]


@NEEDS_ANSIBLE
def test_the_list_sorts_by_name_ignoring_case(tmp_path):
    # Case-insensitive: a case-sensitive sort would put "bravo" after "Echo Foxtrot".
    templates = [
        {"name": "Deploy Echo Foxtrot", "playbook": "platform/playbooks/deploy-echo-foxtrot.yml"},
        {"name": "Deploy bravo", "playbook": "platform/playbooks/deploy-bravo.yml"},
        {"name": "Deploy Alpha", "playbook": "platform/playbooks/deploy-alpha.yml"},
    ]
    body = _render_catalog(tmp_path, templates, {}, _dir(tmp_path))
    assert [e["name"] for e in body["services"]] == ["Alpha", "bravo", "Echo Foxtrot"]


@NEEDS_ANSIBLE
def test_no_qualifying_template_renders_an_empty_list(tmp_path):
    only_excluded = [{"name": "Deploy All Services", "playbook": "platform/playbooks/deploy-all.yml"}]
    assert _render_catalog(tmp_path, only_excluded, {}, _dir(tmp_path)) == {"services": []}
    assert _render_catalog(tmp_path, [], {}, _dir(tmp_path)) == {"services": []}
