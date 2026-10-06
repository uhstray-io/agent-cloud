"""Every workflow executor refuses an extra var that sets one of its internal names.

An extra var outranks everything a playbook computes (ansible-core variable precedence, 22
against 12-21), so `-e '_fw_group_errors=[]'` turned a failed Apply Firewall into a recorded
pass. refuse-internal-extra-vars.yml refuses any underscore-prefixed extra var as the run's
first play; these tests pin the hostvars behaviour it relies on, require it first in every
executor and snapshot the workflow registry names, and launch each of them with a forged
internal name.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import forgeries
import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
GUARD = "refuse-internal-extra-vars.yml"
REGISTRY = REPO / "platform/workflows/service-onboarding/registry.yml"
CATALOG = REPO / "platform/semaphore/templates.yml"
PER_SERVICE = "Deploy {service}"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None,
                                   reason="ansible-playbook not installed")


def _executors() -> list[Path]:
    """The playbook behind every executor and snapshot template the registry names."""
    steps = yaml.safe_load(REGISTRY.read_text())["steps"]
    by_name = {t["name"]: t["playbook"] for t in yaml.safe_load(CATALOG.read_text())["templates"]}
    names = {s.get(k) for s in steps for k in ("executor", "snapshot")} - {None, PER_SERVICE}
    return sorted({REPO / by_name[n] for n in names})


EXECUTORS = _executors()


def _emitters() -> list[Path]:
    """Every playbook that records a step result."""
    out = []
    for path in sorted(PLAYBOOKS.glob("*.yml")):
        for task in playbook_yaml.tasks(playbook_yaml.load(path)):
            include = task.get("ansible.builtin.include_tasks") or ""
            include = include.get("file", "") if isinstance(include, dict) else include
            if isinstance(include, str) and include.endswith("emit-step-result.yml"):
                out.append(path)
                break
    return out


def _run(playbook: Path, tmp_path: Path, *args: str, inventory: str = "localhost,"):
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
    return subprocess.run(["ansible-playbook", "-i", inventory, str(playbook), *args],
                          cwd=REPO, env=env, text=True, capture_output=True, check=False, timeout=120)


def test_the_registry_resolves_to_executors():
    assert len(EXECUTORS) >= 19, EXECUTORS
    assert all(p.is_file() for p in EXECUTORS), EXECUTORS


def _launchable() -> list[Path]:
    """Every playbook Semaphore launches: a template's playbook, wrappers included. A wrapper
    that does work before importing an executor (Clean Deploy agentgateway) would run that
    work before the executor's own guard (PR #459 review), so the guard sits in the launched
    file itself."""
    return sorted({REPO / t["playbook"] for t in yaml.safe_load(CATALOG.read_text())["templates"]})


# Launched playbooks that may start without the guard, each with the reason. Empty: keep it so.
NOT_GUARDED: dict[str, str] = {}


@pytest.mark.parametrize("path", sorted(set(_launchable()) | set(EXECUTORS) | set(_emitters())),
                         ids=lambda p: p.name)
def test_every_launchable_playbook_refuses_internal_extra_vars_first(path):
    # First, before any play or import: a later guard would run after something was set.
    first = playbook_yaml.load(path)[0]
    if path.name in NOT_GUARDED:
        assert first.get("ansible.builtin.import_playbook") != GUARD, f"{path.name} is guarded now: drop it"
        return
    assert first.get("ansible.builtin.import_playbook") == GUARD, f"{path.name} does not start with {GUARD}"


NESTED = "_extra_var_guard_nested"


def _guarded(path: Path) -> bool:
    doc = playbook_yaml.load(path)
    return bool(doc) and isinstance(doc[0], dict) and \
        str(doc[0].get("ansible.builtin.import_playbook", "")).endswith(GUARD)


def _imports(path: Path):
    for index, entry in enumerate(playbook_yaml.load(path) or []):
        target = entry.get("ansible.builtin.import_playbook") or entry.get("import_playbook") \
            if isinstance(entry, dict) else None
        if target and not str(target).endswith(GUARD):
            yield index, entry, (path.parent / target).resolve()


def test_a_guarded_playbook_imported_mid_run_skips_its_guard_by_import_var():
    # Imported after the importer's first entry, the imported guard would find the importer's
    # own facts in hostvars and refuse an honest run. The importer's guard covered the run.
    files = [p for base in playbook_yaml.SCANNED for p in sorted(base.rglob("*.yml"))]
    flagged = 0
    for path in files:
        for index, entry, target in _imports(path):
            if target.is_file() and _guarded(target):
                passes = (entry.get("vars") or {}).get(NESTED) is True
                if index == 0:
                    assert not passes, f"{path.name}: first-entry import of {target.name} must run its guard"
                else:
                    assert passes, f"{path.name}: import of {target.name} must pass {NESTED}: true"
                    assert _guarded(path), f"{path.name} passes {NESTED} but does not start with the guard"
                    flagged += 1
    assert flagged >= 17, flagged


def test_every_step_result_emitter_is_a_registry_executor():
    # A new emitter outside the registry would escape the launches below.
    assert set(_emitters()) <= set(EXECUTORS), sorted(p.name for p in set(_emitters()) - set(EXECUTORS))


GUARD_TASK = playbook_yaml.load(PLAYBOOKS / GUARD)[0]["tasks"][0]
GUARD_PATTERN = re.search(r"select\('match', '([^']+)'\)", GUARD_TASK["ansible.builtin.assert"]["that"]).group(1)


@pytest.mark.parametrize("path", EXECUTORS, ids=lambda p: p.name)
def test_every_value_an_executor_computes_is_refused_by_the_guard(path):
    # A register or set_fact the guard does not cover can be replaced by an extra var (PR #459
    # review: `all_vms` let a forged cluster read classify a foreign VM as owned).
    names = playbook_yaml.defined_names(path)
    public = sorted(n for n, kinds in names.items() if kinds & {"register", "set_fact"}
                    and not re.match(GUARD_PATTERN, n))
    assert public == [], f"{path.name}: name a computed value with a leading underscore: {public}"


def test_the_guard_matches_every_internal_name():
    assert GUARD_TASK["ansible.builtin.assert"]["that"] == (
        "hostvars[inventory_hostname].keys() | select('match', '_|ansible_become_password$') | list | length == 0")
    assert GUARD_TASK["when"] == (f"not ({NESTED} is defined and '{NESTED}' not in hostvars[inventory_hostname])")
    assert re.match(GUARD_PATTERN, "_anything") and re.match(GUARD_PATTERN, "ansible_become_password")
    assert not re.match(GUARD_PATTERN, "ansible_become_password_file") and not re.match(GUARD_PATTERN, "target_service")


# The mechanism: hostvars carries an extra var but not a play, block or task var, and holds
# no underscore name of its own.
@needs_ansible
def test_hostvars_holds_extra_vars_but_not_what_a_play_defines(tmp_path):
    (tmp_path / "vf.yml").write_text("_vf: file\n")
    probe = "{{ ['_pv', '_vf', '_bv', '_tv', '_ev'] | select('in', hostvars[inventory_hostname]) | list | to_json }}"
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "vars_files": [str(tmp_path / "vf.yml")], "vars": {"_pv": "play"},
             "tasks": [{"block": [{"ansible.builtin.debug": {"msg": "SEEN " + probe}, "vars": {"_tv": "task"}},
                                  {"ansible.builtin.debug": {"msg": "ALL {{ hostvars[inventory_hostname].keys()"
                                                                    " | select('match', '_') | list | to_json }}"}}],
                        "vars": {"_bv": "block"}}]}]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))
    proc = _run(path, tmp_path, "-e", "_ev=extra")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"msg": "SEEN [\\"_ev\\"]"' in proc.stdout, proc.stdout
    assert '"msg": "ALL [\\"_ev\\"]"' in proc.stdout, proc.stdout
    assert '"msg": "ALL []"' in _run(path, tmp_path).stdout


@needs_ansible
@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("_verdict", "fail", "pass"))
def test_the_guard_ends_the_run_before_any_other_play(tmp_path, check, forge):
    inv = tmp_path / "inv.ini"
    inv.write_text("[tgt]\nother ansible_connection=local\n")
    play = [{"ansible.builtin.import_playbook": str(PLAYBOOKS / GUARD)},
            {"hosts": "tgt", "gather_facts": False, "vars": {"_verdict": "fail"},
             "tasks": [{"ansible.builtin.debug": {"msg": "VERDICT {{ _verdict }}"}, "tags": ["verify"]}]}]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))
    ok = _run(path, tmp_path, *(["--check"] if check else []), inventory=str(inv))
    assert ok.returncode == 0 and "VERDICT fail" in ok.stdout, ok.stdout + ok.stderr
    # Plain and templated (forgeries.py): the templates render "fail" where the guard play
    # could look and "pass" afterwards; the guard reads the key, so the rendering cannot matter.
    for args in (["-e", forge(tmp_path)], ["--tags", "verify", "-e", forge(tmp_path)]):
        proc = _run(path, tmp_path, *args, *(["--check"] if check else []), inventory=str(inv))
        assert proc.returncode != 0, proc.stdout
        assert "Refusing to run: _verdict set from outside the playbook" in proc.stdout
        assert "VERDICT" not in proc.stdout and "PLAY [tgt]" not in proc.stdout, proc.stdout


# The name each verdict is computed from, where one name carries it; any other executor is
# sampled with the first internal name it defines, so the sample moves with the playbook.
VERDICT = {
    "apply-firewall.yml": "_fw_group_errors",
    "manage-caddy-sites.yml": "_edge_group_errors",
    "backup-credentials-to-site-config.yml": "_backup_error",
    "backup-service-ssh-key.yml": "_backup_error",
    "lookup-service-inventory.yml": "_lookup_errors",
    "resize-vm.yml": "_rs_errors",
}


def _forged_name(path: Path) -> str:
    internal = sorted(n for n in playbook_yaml.defined_names(path) if n.startswith("_"))
    name = VERDICT.get(path.name, internal[0])
    assert name in internal, f"{path.name} no longer defines {name}"
    return name


# Each real executor, launched with a forged value for one of its own internal names, plain and
# templated: refused in the guard play, no other play started and no step result recorded.
@needs_ansible
@pytest.mark.parametrize("path,forge", [
    pytest.param(path, f.values[0], id=f"{path.name}-{f.id}")
    for path in EXECUTORS
    for f in forgeries.templated_forgeries(_forged_name(path), [], {"forged": True})
])
def test_a_forged_internal_name_is_refused_by_the_real_executor(path, forge, tmp_path):
    name = _forged_name(path)
    proc = _run(path, tmp_path, "-e", "target_service=demo_svc", "-e", forge(tmp_path), "--check")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert f"Refusing to run: {name} set from outside the playbook" in proc.stdout, proc.stdout + proc.stderr
    assert proc.stdout.count("PLAY [") == 1, proc.stdout
    assert "CUSTOM STATS" not in proc.stdout or "step_result" not in proc.stdout, proc.stdout


# The `_` prefix is RESERVED: hostvars also holds inventory vars, so an inventory that defined an
# underscore variable would be refused like a forged extra var. The example inventories define
# none (resolved by ansible-inventory, group vars merged), and a collision names the rule.
@needs_ansible
@pytest.mark.parametrize("path", sorted((REPO / "platform/inventory").glob("*.y*ml*")), ids=lambda p: p.name)
def test_no_example_inventory_defines_a_reserved_name(path, tmp_path):
    copy = tmp_path / "inventory.yml"  # the yaml plugin reads a .yml name, not .example
    copy.write_text(path.read_text())
    out = subprocess.run(["ansible-inventory", "-i", str(copy), "--list"], cwd=REPO, text=True,
                         capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    hostvars = json.loads(out)["_meta"]["hostvars"]
    assert hostvars, path.name
    assert sorted({f"{h}: {k}" for h, v in hostvars.items() for k in v if k.startswith("_")}) == []


@needs_ansible
def test_an_inventory_var_with_the_reserved_prefix_is_refused_with_the_rule(tmp_path):
    inv = tmp_path / "inv.yml"
    inv.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {"ansible_connection": "local"}},
                                           "vars": {"_collides": 1}}}))
    play = tmp_path / "play.yml"
    play.write_text(json.dumps([{"ansible.builtin.import_playbook": str(PLAYBOOKS / GUARD)}]))
    proc = _run(play, tmp_path, inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert "Refusing to run: _collides set from outside the playbook" in proc.stdout
    assert "RESERVED for playbook" in proc.stdout and "Rename the colliding variable" in proc.stdout


# Every playbook Semaphore launches, sent a forged internal name plainly and as both templates:
# refused in its first play, before any other play starts.
@needs_ansible
@pytest.mark.parametrize("path, forge", [
    pytest.param(path, f.values[0], id=f"{path.name}-{f.id}")
    for path in _launchable()
    for f in forgeries.templated_forgeries("_forged_by_launch", None, {"forged": True})
])
def test_every_launchable_playbook_refuses_a_forged_internal_name(path, forge, tmp_path):
    proc = _run(path, tmp_path, "-e", "target_service=demo_svc", "-e", forge(tmp_path), "--check")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "Refusing to run: _forged_by_launch set from outside the playbook" in proc.stdout, proc.stdout + proc.stderr
    assert proc.stdout.count("PLAY [") == 1, proc.stdout


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("_monorepo_dir", "/opt/agent-cloud", "/"))
def test_a_wrapper_refuses_before_its_own_cleanup(forge, tmp_path):
    # PR #459 review: Clean Deploy agentgateway removed files under a forged _monorepo_dir in its
    # own cleanup play, before the imported deploy's guard ran.
    proc = _run(PLAYBOOKS / "clean-deploy-agentgateway.yml", tmp_path, "-e", forge(tmp_path))
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "Refusing to run: _monorepo_dir set from outside the playbook" in proc.stdout, proc.stdout
    assert proc.stdout.count("PLAY [") == 1, proc.stdout


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("ansible_become_password", "from-openbao", "forged"))
def test_a_forged_become_password_is_refused(forge, tmp_path):
    # A magic variable the playbooks resolve from OpenBao with set_fact cannot carry the prefix.
    play = tmp_path / "play.yml"
    play.write_text(json.dumps([{"ansible.builtin.import_playbook": str(PLAYBOOKS / GUARD)}]))
    proc = _run(play, tmp_path, "-e", forge(tmp_path))
    assert proc.returncode != 0, proc.stdout
    assert "Refusing to run: ansible_become_password set from outside" in proc.stdout, proc.stdout


def _nested_fixture(tmp_path: Path) -> Path:
    """An importer that sets an internal fact on localhost, then imports a guarded playbook."""
    inner = tmp_path / "inner.yml"
    inner.write_text(json.dumps([
        {"ansible.builtin.import_playbook": str(PLAYBOOKS / GUARD)},
        {"hosts": "localhost", "gather_facts": False, "tasks": [{"ansible.builtin.debug": {"msg": "INNER RAN"}}]}]))
    outer = tmp_path / "outer.yml"
    outer.write_text(json.dumps([
        {"ansible.builtin.import_playbook": str(PLAYBOOKS / GUARD)},
        {"hosts": "localhost", "gather_facts": False, "tasks": [{"ansible.builtin.set_fact": {"_cleaned": True}}]},
        {"ansible.builtin.import_playbook": str(inner), "vars": {NESTED: True}}]))
    return outer


@needs_ansible
def test_a_nested_guard_skips_for_its_importer(tmp_path):
    proc = _run(_nested_fixture(tmp_path), tmp_path)
    assert proc.returncode == 0 and "INNER RAN" in proc.stdout, proc.stdout + proc.stderr


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries(NESTED, True, True))
def test_a_forged_nested_flag_is_refused(forge, tmp_path):
    # Sent as an extra var the flag is in hostvars, so the importer's own guard refuses it, and a
    # guarded playbook launched directly with it refuses it too.
    for playbook in (_nested_fixture(tmp_path), tmp_path / "inner.yml"):
        proc = _run(playbook, tmp_path, "-e", forge(tmp_path))
        assert proc.returncode != 0, proc.stdout
        assert f"Refusing to run: {NESTED} set from outside the playbook" in proc.stdout, proc.stdout
        assert "INNER RAN" not in proc.stdout
