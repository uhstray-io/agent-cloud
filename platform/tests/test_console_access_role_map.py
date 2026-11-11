"""The Dispatch Console's Access page role map (plan 17, 8.2b) is pinned to the authoritative table.

The authoritative table is `plan/architecture/04-credentials-access.md`, under the heading
"Access Level -> Per-Service Role Mapping". `platform/playbooks/vars/access-role-map.yml` is the copy the
console renders to `config/role-map.json`. This file pins, where a drift would otherwise go unnoticed:

- every level, system and cell of the vars file equals the table, with markdown removed;
- the vars file renders through deploy-console.yml's own lookup, and the JSON the server reads equals it;
- the JSON keys and status names are the ones the server's serde types declare (app/src/server/access.rs);
- the deploy wiring: the compose mount, the existence check and change hashes in deploy.sh, the always-set
  env path, and the Authentik directory pair, which renders and is read only when its URL is set.

A cell is the table's text with `**` and backticks removed. One normalisation applies to the column header:
`OpenBao (per-tenant prefix)` is compared as `OpenBao`. The parenthetical names the scope, and the system name
is what the page shows. Every cell is compared exactly.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
VARS = PLAYBOOKS / "vars/access-role-map.yml"
ROTATE = PLAYBOOKS / "rotate-console-credentials.yml"
DEPLOY = PLAYBOOKS / "deploy-console.yml"
DOC = REPO / "plan/architecture/04-credentials-access.md"
CONSOLE = REPO / "platform/services/console/deployment"
SERVER_ACCESS = REPO / "platform/services/console/app/src/server/access.rs"

HEADING = "### Access Level -> Per-Service Role Mapping"
LEVELS = ["Admin", "Maintainer", "Developer", "User"]
NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
ENV_SECRETS = {
    "console_oidc_client_secret": "OIDC-REF",
    "semaphore_api_token": "SEM-REF",
    "console_directory_token": "DIR-REF",
}


def _clean(cell: str) -> str:
    return cell.replace("**", "").replace("`", "").strip()


def _doc_model() -> dict:
    """The table under HEADING: its system names (header row) and each level's cells, markdown removed."""
    lines = DOC.read_text().splitlines()
    start = lines.index(HEADING)
    rows = []
    for line in lines[start + 1:]:
        if not line.startswith("|"):
            if rows:
                break
            continue
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    header, separator, body = rows[0], rows[1], rows[2:]
    assert all(set(cell) <= set("-:") for cell in separator), separator
    systems = [re.sub(r"\s*\([^)]*\)$", "", _clean(name)) for name in header[1:]]
    levels = []
    for row in body:
        assert len(row) == len(header), row
        levels.append({"level": _clean(row[0]), "roles": {s: _clean(c) for s, c in zip(systems, row[1:], strict=True)}})
    return {"systems": systems, "levels": levels}


def _vars_model() -> dict:
    doc = yaml.safe_load(VARS.read_text())
    return {"systems": doc["systems"],
            "levels": [{"level": level["level"], "roles": level["roles"]} for level in doc["levels"]]}


def _deploy_vars() -> dict:
    return next(p for p in playbook_yaml.plays(DEPLOY) if "_secret_definitions" in (p.get("vars") or {}))["vars"]


def _run(tmp_path: Path, harness_dir: Path, playbook: dict) -> subprocess.CompletedProcess:
    # tmp_path is the sandbox's writable root: the play may write its output beside harness_dir.
    path = harness_dir / "harness.yml"
    path.write_text(yaml.safe_dump([playbook]))
    return harness_sandbox.run_playbook(path, tmp_path)


def _render(tmp_path: Path, src: Path, variables: dict) -> str:
    dest = tmp_path / "rendered.out"
    play = {"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
            "tasks": [{"ansible.builtin.template": {"src": str(src), "dest": str(dest), "mode": "0644"}}]}
    proc = _run(tmp_path, tmp_path, play)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return dest.read_text()


# --- The table and the vars file ----------------------------------------------------------------------

def test_the_doc_table_is_parsed_with_its_four_levels_and_four_systems():
    # Guards the parser: an empty or misread table would make the comparison below pass vacuously.
    doc = _doc_model()
    assert [level["level"] for level in doc["levels"]] == LEVELS
    assert doc["systems"] == ["Semaphore", "NetBox", "OpenBao", "Project visibility"]
    assert all(set(level["roles"]) == set(doc["systems"]) for level in doc["levels"])


def test_the_role_map_cells_equal_the_authoritative_table():
    doc, yml = _doc_model(), _vars_model()
    assert yml["systems"] == doc["systems"]
    assert [level["level"] for level in yml["levels"]] == LEVELS
    for want, got in zip(doc["levels"], yml["levels"], strict=True):
        assert got["roles"] == want["roles"], want["level"]


def test_each_level_has_exactly_its_three_keys_and_only_the_admin_row_is_built():
    levels = yaml.safe_load(VARS.read_text())["levels"]
    assert all(set(level) == {"level", "status", "roles"} for level in levels), levels
    assert {level["level"]: level["status"] for level in levels} == {
        "Admin": "built", "Maintainer": "target", "Developer": "target", "User": "target"}


# --- The render path the deploy uses ------------------------------------------------------------------

@NEEDS_ANSIBLE
def test_the_deploy_lookup_renders_the_vars_file_to_the_json_the_server_reads(tmp_path):
    # The expression is taken from deploy-console.yml unchanged. Its `playbook_dir` is the harness directory, so
    # that directory holds a `vars` symlink to the real one: the lookup path resolves exactly as on the controller.
    expression = _deploy_vars()["_access_role_map"]
    assert "vars/access-role-map.yml" in expression, expression
    harness_dir = tmp_path / "playbooks"
    harness_dir.mkdir()
    (harness_dir / "vars").symlink_to(VARS.parent)
    dest = tmp_path / "role-map.json"
    play = {"hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {"_access_role_map": expression},
            "tasks": [{"ansible.builtin.template": {
                "src": str(CONSOLE / "templates/role-map.json.j2"), "dest": str(dest), "mode": "0644"}}]}
    proc = _run(tmp_path, harness_dir, play)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = dest.read_text()
    # The `{{ vault_secret_prefix }}` placeholder survives rendering as literal text, not as a template.
    assert "read across the tenant's {{ vault_secret_prefix }}/* (via orchestrator, not direct)" in out
    assert json.loads(out) == yaml.safe_load(VARS.read_text())


def _struct_fields(src: str, name: str) -> list[str]:
    body = re.search(r"pub struct " + name + r" \{(.*?)\n\}", src, re.S)
    assert body, f"{name} not found in access.rs"
    return re.findall(r"pub (\w+):", body.group(1))


def test_the_json_keys_and_status_names_are_the_serde_types_the_server_declares():
    # The rendered file is parsed by these structs. A rename on either side fails startup, so each side is pinned
    # to the other here (the same check as test_rendered_flows_match_the_servers_flow_type).
    src = SERVER_ACCESS.read_text()
    assert _struct_fields(src, "RoleMap") == ["systems", "levels"]
    assert _struct_fields(src, "RoleLevel") == ["level", "status", "roles"]
    assert src.count("#[serde(deny_unknown_fields)]") >= 2
    assert re.search(r'#\[serde\(rename_all = "lowercase"\)\]\s*pub enum \w+ \{\s*Built,\s*Target,?\s*\}', src), \
        "status enum must serialise as built|target"


# --- Deploy wiring -----------------------------------------------------------------------------------

def test_the_role_map_is_rendered_always_and_listed_in_the_deploy_templates():
    templates = {t["dest"]: t["src"] for t in _deploy_vars()["_env_templates"]}
    assert templates["config/role-map.json"] == "role-map.json.j2"


def test_compose_mounts_the_role_map_read_only_at_the_path_env_names():
    text = (CONSOLE / "compose.yml").read_text()
    assert "      - ./config/role-map.json:/etc/console/role-map.json:ro" in text
    assert "CONSOLE_ROLE_MAP_FILE=/etc/console/role-map.json" in (CONSOLE / "templates/env.j2").read_text()


def test_deploy_sh_refuses_without_the_role_map_and_hashes_it_into_both_up_branches():
    text = (CONSOLE / "deploy.sh").read_text()
    assert "config/role-map.json not found" in text
    # Each compose_up_if_changed line passes the role map exactly once, so the digest covers it on both branches.
    up_lines = [ln for ln in text.splitlines() if "compose_up_if_changed config/" in ln]
    assert [ln.count("config/role-map.json") for ln in up_lines] == [1, 1]


def test_the_authentik_directory_read_is_its_own_entry_gated_on_its_url():
    v = _deploy_vars()
    expression = v["_console_authentik_directory_reads"]
    assert "'from_service': 'authentik', 'read_keys': ['console_directory_token']" in expression
    assert "console_authentik_api_url | default('', true)" in expression
    assert "_console_authentik_directory_reads" in v["_shared_reads"]
    # The OIDC read stays as its own entry, unchanged: manage-secrets zips entries with their results, so folding
    # the directory key into this entry is where the OIDC key could be lost.
    assert {"from_service": "authentik", "read_keys": ["console_oidc_client_secret"]} in v["_console_oidc_reads"]


@NEEDS_ANSIBLE
def test_the_directory_read_renders_nothing_unless_its_url_is_set(tmp_path):
    template = tmp_path / "reads.j2"
    template.write_text("{{ _console_authentik_directory_reads | to_json }}")
    expression = _deploy_vars()["_console_authentik_directory_reads"]
    unset = json.loads(_render(tmp_path, template, {"console_authentik_api_url": "",
                                                   "_console_authentik_directory_reads": expression}))
    assert unset == []
    set_ = json.loads(_render(tmp_path, template, {"console_authentik_api_url": "https://auth.example.test:8443",
                                                  "_console_authentik_directory_reads": expression}))
    assert set_ == [{"from_service": "authentik", "read_keys": ["console_directory_token"]}]


@NEEDS_ANSIBLE
def test_env_names_the_role_map_always_and_the_directory_pair_only_with_its_url(tmp_path):
    unset = _render(tmp_path, CONSOLE / "templates/env.j2", {"secrets": ENV_SECRETS})
    assert "CONSOLE_ROLE_MAP_FILE=/etc/console/role-map.json" in unset
    assert "CONSOLE_AUTHENTIK_URL" not in unset and "CONSOLE_AUTHENTIK_TOKEN" not in unset
    set_ = _render(tmp_path, CONSOLE / "templates/env.j2",
                   {"secrets": ENV_SECRETS, "console_authentik_api_url": "https://auth.example.test:8443"})
    assert "CONSOLE_AUTHENTIK_URL=https://auth.example.test:8443" in set_
    assert "CONSOLE_AUTHENTIK_TOKEN=DIR-REF" in set_
    assert "CONSOLE_ROLE_MAP_FILE=/etc/console/role-map.json" in set_


def _all_tasks(tasks) -> list[dict]:
    out = []
    for task in tasks or []:
        if isinstance(task, dict):
            out.append(task)
            for key in ("block", "rescue", "always"):
                out.extend(_all_tasks(task.get(key)))
    return out


def test_the_drift_check_lists_the_role_map_source_as_a_pinned_input():
    # The rotation playbook's drift argv is the list of committed inputs the pin is compared over (D31).
    tasks = [t for p in yaml.safe_load(ROTATE.read_text()) if isinstance(p, dict) and "hosts" in p
             for t in _all_tasks(p.get("tasks"))]
    drift = next(t for t in tasks if t.get("name", "").startswith("Compare the console's deploy inputs"))
    assert "platform/playbooks/vars/access-role-map.yml" in drift["ansible.builtin.command"]["argv"]
