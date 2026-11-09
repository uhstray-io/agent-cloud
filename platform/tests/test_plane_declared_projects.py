"""The Plane project declaration: projects.yml merged with site-config's plane_projects_private.

The playbook's own "The declaration is well formed" and "Assemble the desired projects" tasks are run
by ansible-playbook, unchanged, against a stub inventory, with the GitHub read replaced by a canned
result: every declared project is created, only enabled ones carry modules, and a project with no
repository gets the external_id project:<name>.
"""

import json
import pathlib
import re
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/sync-plane-projects.yml"
DECLARED = yaml.safe_load((ROOT / "platform/services/plane/projects.yml").read_text())["plane_projects"]


def _play1_task(name):
    plays = yaml.safe_load(PLAYBOOK.read_text())
    play = next(p for p in plays if p.get("name") == "Read open OpenSpec tasks from GitHub")
    return play, next(t for t in play["tasks"] if t["name"] == name)


def run(tmp_path, private, parsed_modules=None):
    play, check = _play1_task("The declaration is well formed")
    _, assemble = _play1_task("Assemble the desired projects")
    enabled = [p for p in DECLARED + private if p.get("enabled")]
    parsed = {"results": [{"item": p, "stdout": json.dumps({"modules": parsed_modules or [], "skipped": []})}
                          for p in enabled]}
    vars_ = {k: v for k, v in play["vars"].items() if k.startswith("_declared") or k == "_enabled"}
    vars_["_declared_public"] = vars_["_declared_public"].replace("playbook_dir ~ '/../", f"'{ROOT}/platform/")
    show = {"name": "show", "ansible.builtin.debug": {"msg": "RESULT={{ _desired_projects | to_json }}"}}
    pb = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
           "vars": {**vars_, "_parsed": parsed}, "tasks": [check, assemble, show]}]
    (tmp_path / "pb.yml").write_text(yaml.safe_dump(pb))
    inv = {"all": {"children": {"plane_svc": {"hosts": {"plane": {"plane_projects_private": private}}}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    out = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "pb.yml")],
                         capture_output=True, text=True, timeout=120, cwd=tmp_path)
    m = re.search(r'RESULT=(\[.*\])"', out.stdout)
    return out, (json.loads(m.group(1).replace('\\"', '"')) if m else None)


PRIVATE = [{"repo": "org/secret-site", "identifier": "SECRET", "name": "secret-site", "private": True,
            "enabled": False},
           {"identifier": "NOREPO", "name": "no-repo", "enabled": False}]


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


@pytest.mark.parametrize("bad", [
    [{"identifier": "AC", "name": "dup-identifier", "enabled": False}],
    [{"identifier": "lower", "name": "bad-identifier", "enabled": False}],
    [{"identifier": "NOREPO", "name": "no-repo", "enabled": True}],
    [{"identifier": "NOFLAG", "name": "no-flag"}],
    [{"repo": "uhstray-io/huhhb", "identifier": "HUHHB2", "name": "dup-repo", "enabled": False}],
])
def test_a_malformed_private_declaration_is_refused(tmp_path, bad):
    out, projects = run(tmp_path, bad)
    assert out.returncode != 0
    assert "projects.yml plus plane_projects_private: every entry needs" in out.stdout
    assert projects is None


def test_the_public_file_names_no_repository_that_must_stay_private():
    # New private names belong in site-config (Joseph, 2026-11-09); these are the ones already public.
    allowed = {"huhhb", "agent-cloud", "zerds", "zerds-website", "weft", "atlas", "scientific-business",
               "site-config", "www"}
    assert {p["repo"].split("/", 1)[1] for p in DECLARED} <= allowed
