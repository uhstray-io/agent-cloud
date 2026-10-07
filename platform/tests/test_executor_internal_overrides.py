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


INCLUDE_KEYS = ("ansible.builtin.import_playbook", "import_playbook", "ansible.builtin.include_tasks",
                "ansible.builtin.import_tasks", "include_tasks", "import_tasks")


def _reached() -> list[Path]:
    """Every launched playbook and every playbook or task file it imports or includes by a
    literal path, transitively."""
    seen: set[Path] = set()
    stack = list(_launchable())
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for task in playbook_yaml.tasks(playbook_yaml.load(path)):
            for key in INCLUDE_KEYS:
                ref = task.get(key)
                ref = ref.get("file") if isinstance(ref, dict) else ref
                if not isinstance(ref, str) or "{{" in ref:
                    continue
                for base in (path.parent, path.parent / "tasks", PLAYBOOKS, PLAYBOOKS / "tasks"):
                    if (base / ref).is_file():
                        stack.append((base / ref).resolve())
                        break
    return sorted(seen)


def _computed(path: Path) -> set[str]:
    """Literal register and set_fact names in any playbook or task file."""
    names = set()
    doc = playbook_yaml.load(path)
    for task in playbook_yaml.tasks(doc if isinstance(doc, list) else []):
        if isinstance(task.get("register"), str):
            names.add(task["register"])
        for module in playbook_yaml.SET_FACT:
            if isinstance(task.get(module), dict):
                names |= set(task[module]) - {"cacheable"}
    return {n for n in names if "{{" not in n}


@pytest.mark.parametrize("path", _reached(), ids=lambda p: str(p.relative_to(REPO)))
def test_every_value_a_launched_run_computes_is_refused_by_the_guard(path):
    # A register or set_fact the guard does not cover can be replaced by an extra var (PR #459
    # reviews: `all_vms` let a forged cluster read classify a foreign VM as owned, and a JSON
    # `validation_results` suppressed Proxmox validation's final failure).
    public = sorted(n for n in _computed(path) if not re.match(GUARD_PATTERN, n))
    assert public == [], f"{path.name}: name a computed value with a leading underscore: {public}"


def test_the_reach_covers_imports_and_task_files():
    reached = {p.relative_to(REPO).as_posix() for p in _reached()}
    assert {"platform/playbooks/proxmox-validate.yml", "platform/playbooks/tasks/push-loki-lines.yml",
            "platform/playbooks/tasks/clone-and-deploy.yml"} <= reached


def test_the_guard_matches_every_internal_name():
    assert GUARD_TASK["ansible.builtin.assert"]["that"] == (
        "hostvars[inventory_hostname].keys() | select('match', "
        "'_|ansible_(password|\\w+_pass|\\w+_password)$')"
        " | list | length == 0")
    assert GUARD_TASK["when"] == (f"not ({NESTED} is defined and '{NESTED}' not in hostvars[inventory_hostname])")
    assert re.match(GUARD_PATTERN, "_anything")
    assert all(re.match(GUARD_PATTERN, n) for n in PASSWORD_ALIASES + OTHER_PLUGIN_ALIASES)
    assert not any(re.match(GUARD_PATTERN, n) for n in (
        "ansible_user", "ansible_become_method", "ansible_ssh_private_key_file", "ansible_passphrase"))
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


# Every name Ansible reads the escalation or connection password from (ansible-core 2.21:
# plugins/become/sudo.py, su.py, runas.py and plugins/connection/ssh.py, the option's `vars`).
PASSWORD_ALIASES = ["ansible_become_password", "ansible_become_pass", "ansible_sudo_pass", "ansible_su_pass",
                    "ansible_runas_pass", "ansible_password", "ansible_ssh_pass", "ansible_ssh_password"]


@needs_ansible
def test_the_alias_list_is_what_the_installed_plugins_read():
    # Read back from the installed ansible-core, so an upgrade that adds an alias fails here.
    script = (
        "import yaml, ansible.plugins.become.sudo as a, ansible.plugins.become.su as b, "
        "ansible.plugins.become.runas as c, ansible.plugins.connection.ssh as d\n"
        "names = set()\n"
        "for m, opt in ((a, 'become_pass'), (b, 'become_pass'), (c, 'become_pass'), (d, 'password')):\n"
        "    names |= {v['name'] for v in yaml.safe_load(m.DOCUMENTATION)['options'][opt].get('vars', [])}\n"
        "print(' '.join(sorted(names)))")
    python = Path(shutil.which("ansible-playbook")).read_text().splitlines()[0].removeprefix("#!").strip()
    out = subprocess.run([python, "-c", script], text=True, capture_output=True, check=True).stdout.split()
    assert sorted(out) == sorted(PASSWORD_ALIASES)


# Collection plugins' names under the same convention, refused by the pattern rather than a list.
OTHER_PLUGIN_ALIASES = ["ansible_winrm_pass", "ansible_httpapi_pass", "ansible_httpapi_password",
                        "ansible_doas_pass", "ansible_pbrun_pass"]


@needs_ansible
@pytest.mark.parametrize("name, forge", [
    pytest.param(n, f.values[0], id=f"{n}-{f.id}")
    for n in PASSWORD_ALIASES + OTHER_PLUGIN_ALIASES
    for f in forgeries.templated_forgeries(n, "from-openbao", "forged")
])
def test_a_forged_password_under_any_alias_is_refused(name, forge, tmp_path):
    # Magic variables the playbooks resolve from OpenBao cannot carry the prefix.
    play = tmp_path / "play.yml"
    play.write_text(json.dumps([{"ansible.builtin.import_playbook": str(PLAYBOOKS / GUARD)}]))
    proc = _run(play, tmp_path, "-e", forge(tmp_path))
    assert proc.returncode != 0, proc.stdout
    assert f"Refusing to run: {name} set from outside" in proc.stdout, proc.stdout


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


# Clean Deploy o11y destroys every o11y volume. It refuses before the destroy unless the launch
# names the host, and for a remote host unless the checkout is the reviewed commit, clean (PR
# #459 review: the imported deploy checked the SHA only after the volumes were gone). The o11y
# host here is remote (ssh to a closed port): every refusal is decided on the controller, so
# nothing connects. Always --check: a regression must not reach the destroy on this machine.
REMOTE_O11Y = "[o11y_svc]\nobs ansible_host=127.0.0.1 ansible_port=1 ansible_connection=ssh\n"
LOCAL_O11Y = "[o11y_svc]\nobs ansible_connection=local local_mode=true\n"


@needs_ansible
@pytest.mark.parametrize("extra, refusal", [
    ({}, "Refusing: pass -e confirm_o11y_reset=obs"),
    ({"confirm_o11y_reset": "other"}, "Refusing: pass -e confirm_o11y_reset=obs"),
    ({"confirm_o11y_reset": "obs"}, "Pass expected_repository_sha"),
    ({"confirm_o11y_reset": "obs", "expected_repository_sha": "0" * 40}, "not the reviewed " + "0" * 40),
], ids=["no-confirm", "wrong-confirm", "no-sha", "wrong-sha"])
def test_clean_deploy_o11y_refuses_before_destroying(extra, refusal, tmp_path):
    inv = tmp_path / "inv.ini"
    inv.write_text(REMOTE_O11Y)
    proc = _run(PLAYBOOKS / "clean-deploy-o11y.yml", tmp_path, "--check", "-e", json.dumps(extra),
                inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert refusal in proc.stdout, proc.stdout
    assert "TASK [Destroy existing deployment]" not in proc.stdout
    assert "Fresh deploy" not in proc.stdout and "Phase 1" not in proc.stdout


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", False, True))
def test_a_forged_local_mode_does_not_waive_the_reviewed_commit(forge, tmp_path):
    # PR #459 follow-up: local_mode is public, so an extra var setting it skipped the SHA gate and
    # destroyed a production host's volumes. What waives the gate now is a local connection.
    inv = tmp_path / "inv.ini"
    inv.write_text(REMOTE_O11Y)
    proc = _run(PLAYBOOKS / "clean-deploy-o11y.yml", tmp_path, "--check", "-e", forge(tmp_path),
                "-e", json.dumps({"confirm_o11y_reset": "obs"}), inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert "Pass expected_repository_sha" in proc.stdout, proc.stdout
    assert "TASK [Destroy existing deployment]" not in proc.stdout


@needs_ansible
@pytest.mark.parametrize("extra", [{}, {"confirm_o11y_reset": "other"}], ids=["no-confirm", "wrong-confirm"])
def test_clean_deploy_o11y_requires_the_confirmation_locally_too(extra, tmp_path):
    inv = tmp_path / "inv.ini"
    inv.write_text(LOCAL_O11Y)
    proc = _run(PLAYBOOKS / "clean-deploy-o11y.yml", tmp_path, "--check", "-e", json.dumps(extra),
                inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert "Refusing: pass -e confirm_o11y_reset=obs" in proc.stdout, proc.stdout
    assert "TASK [Destroy existing deployment]" not in proc.stdout


@needs_ansible
def test_clean_deploy_o11y_refuses_the_reviewed_commit_with_uncommitted_files(tmp_path):
    # A copy of the repository at HEAD plus one untracked file: the right SHA, a dirty tree.
    # The working tree's own playbooks are committed in the copy, so it runs the code under test.
    repo = tmp_path / "repo"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(REPO), str(repo)], check=True, env=env)
    shutil.copytree(PLAYBOOKS, repo / "platform/playbooks", dirs_exist_ok=True)
    for args in (["add", "platform/playbooks"],
                 ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "under test",
                  "--allow-empty", "--no-verify"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True,
                          check=True, env=env).stdout.strip()
    (repo / "untracked.txt").write_text("x")
    inv = tmp_path / "inv.ini"
    inv.write_text(REMOTE_O11Y)
    proc = _run(repo / "platform/playbooks/clean-deploy-o11y.yml", tmp_path, "--check", "-e",
                json.dumps({"confirm_o11y_reset": "obs", "expected_repository_sha": head}), inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert f"The checkout is {head} with uncommitted files" in proc.stdout, proc.stdout
    assert "TASK [Destroy existing deployment]" not in proc.stdout


@needs_ansible
def test_password_prompts_are_not_variables_and_pass_the_guard(tmp_path):
    # Semaphore passes its login and become key passwords as --ask-pass / --ask-become-pass
    # answers on stdin, never as variables, so the guard must let them through.
    play = tmp_path / "play.yml"
    play.write_text(json.dumps([{"ansible.builtin.import_playbook": str(PLAYBOOKS / GUARD)},
                                {"hosts": "localhost", "gather_facts": False,
                                 "tasks": [{"ansible.builtin.debug": {"msg": "GUARD PASSED"}}]}]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play), "--ask-pass", "--ask-become-pass"],
                          input="pw\npw\n", cwd=REPO, env=env, text=True, capture_output=True, check=False, timeout=120)
    assert proc.returncode == 0 and "GUARD PASSED" in proc.stdout, proc.stdout + proc.stderr
