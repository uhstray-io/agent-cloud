"""The Plane project declaration: projects.yml merged with site-config's plane_projects_private.

The playbook's own declaration checks and its "Assemble the desired projects" task are run by
ansible-playbook, unchanged, against a stub inventory, with the GitHub read replaced by a canned
result: every declared project is created, only enabled ones carry modules, and a project with no
repository gets the external_id project:<name>.
"""

import json
import pathlib
import re
import subprocess

import pytest
import yaml
from ansible_python import needs_ansible

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/sync-plane-projects.yml"
DECLARED = yaml.safe_load((ROOT / "platform/services/plane/projects.yml").read_text())["plane_projects"]
# Every Play 1 task that judges the declaration, in playbook order, then the assembly.
CHECKS = ["The private declaration is a list", "Every declared project is well formed",
          "The declaration is well formed",
          "Refuse an enabled private repository until the plane-compose App exists"]
UNSET = object()


def _play1():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    play = next(p for p in plays if p.get("name") == "Read open OpenSpec tasks from GitHub")
    return play, {t["name"]: t for t in play["tasks"]}


def run(tmp_path, private, parsed_modules=None):
    play, tasks = _play1()
    declared = DECLARED + (private if isinstance(private, list) else [])
    enabled = [p for p in declared if isinstance(p, dict) and p.get("enabled") is True]
    parsed = {"results": [{"item": p, "stdout": json.dumps({"modules": parsed_modules or [], "skipped": []})}
                          for p in enabled]}
    vars_ = {k: v for k, v in play["vars"].items() if k.startswith("_declared") or k == "_enabled"}
    vars_["_declared_public"] = vars_["_declared_public"].replace("playbook_dir ~ '/../", f"'{ROOT}/platform/")
    show = {"name": "show", "ansible.builtin.debug": {"msg": "RESULT={{ _desired_projects | to_json }}"}}
    pb = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
           "vars": {**vars_, "_parsed": parsed},
           "tasks": [tasks[n] for n in CHECKS] + [tasks["Assemble the desired projects"], show]}]
    (tmp_path / "pb.yml").write_text(yaml.safe_dump(pb))
    host = {} if private is UNSET else {"plane_projects_private": private}
    inv = {"all": {"children": {"plane_svc": {"hosts": {"plane": host}}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    out = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "pb.yml")],
                         capture_output=True, text=True, timeout=120, cwd=tmp_path)
    m = re.search(r'RESULT=(\[.*\])"', out.stdout)
    return out, (json.loads(m.group(1).replace('\\"', '"')) if m else None)


PRIVATE = [{"repo": "org/secret-site", "identifier": "SECRET", "name": "secret-site", "private": True,
            "enabled": False},
           {"identifier": "NOREPO", "name": "no-repo", "enabled": False}]


@needs_ansible
def test_every_declared_project_is_created_and_only_enabled_ones_carry_modules(tmp_path):
    module = {"external_id": "m", "name": "m", "description": "", "items": []}
    out, projects = run(tmp_path, PRIVATE, parsed_modules=[module])
    assert out.returncode == 0, out.stdout + out.stderr
    by_name = {p["name"]: p for p in projects}
    assert set(by_name) == {p["name"] for p in DECLARED} | {"secret-site", "no-repo"}
    for p in DECLARED:
        assert bool(by_name[p["name"]]["modules"]) == p["enabled"], p["name"]
    assert by_name["secret-site"]["repo"] == "org/secret-site"
    assert by_name["no-repo"]["repo"] == "project:no-repo"


@needs_ansible
@pytest.mark.parametrize("private", [UNSET, None, []])
def test_no_private_declaration_means_only_the_public_projects(tmp_path, private):
    out, projects = run(tmp_path, private)
    assert out.returncode == 0, out.stdout + out.stderr
    assert [p["name"] for p in projects] == [p["name"] for p in DECLARED if p["enabled"]] + \
        [p["name"] for p in DECLARED if not p["enabled"]]


ENTRY = "each entry needs name (string)"
UNIQUE = "identifiers, names and repos are unique"


@needs_ansible
@pytest.mark.parametrize("bad, message", [
    ({"identifier": "NONAME", "enabled": False}, ENTRY),
    ({"name": "no-identifier", "enabled": False}, ENTRY),
    ({"identifier": "NOFLAG", "name": "no-flag"}, ENTRY),
    ({"identifier": "lower", "name": "bad-identifier", "enabled": False}, ENTRY),
    ({"identifier": 123, "name": "int-identifier", "enabled": False}, ENTRY),
    ({"identifier": "STR", "name": "string-flag", "enabled": "false"}, ENTRY),
    ({"repo": None, "identifier": "NULLREPO", "name": "null-repo", "enabled": False}, ENTRY),
    ({"repo": "no-owner", "identifier": "NOOWNER", "name": "no-owner", "private": True, "enabled": False}, ENTRY),
    ({"repo": "org/no-private", "identifier": "NOPRIV", "name": "no-private", "enabled": False}, ENTRY),
    ({"identifier": "NOREPO", "name": "no-repo", "enabled": True}, ENTRY),
    ({"identifier": "AC", "name": "dup-identifier", "enabled": False}, UNIQUE),
    ({"identifier": "DUPNAME", "name": "huhhb", "enabled": False}, UNIQUE),
    ({"repo": "uhstray-io/huhhb", "identifier": "HUHHB2", "name": "dup-repo", "private": False, "enabled": False},
     UNIQUE),
    ({"repo": "org/x", "identifier": "PRIV", "name": "enabled-private", "private": True, "enabled": True},
     "org/x is private"),
])
def test_a_malformed_private_declaration_is_refused_by_name(tmp_path, bad, message):
    out, projects = run(tmp_path, [bad])
    assert out.returncode != 0
    assert message in out.stdout, out.stdout
    assert projects is None


@needs_ansible
@pytest.mark.parametrize("private", [{"identifier": "MAP", "name": "a-mapping", "enabled": False}, "a-string"])
def test_a_private_declaration_that_is_not_a_list_is_refused(tmp_path, private):
    out, projects = run(tmp_path, private)
    assert out.returncode != 0
    assert "plane_projects_private (site-config, plane_svc) must be a list" in out.stdout, out.stdout
    assert projects is None


def test_the_public_file_names_no_repository_that_must_stay_private():
    # New private names belong in site-config (Joseph, 2026-11-09); these are the ones already public.
    allowed = {"huhhb", "agent-cloud", "zerds", "zerds-website", "weft", "atlas", "scientific-business",
               "site-config", "www"}
    assert {p["repo"].split("/", 1)[1] for p in DECLARED} <= allowed
