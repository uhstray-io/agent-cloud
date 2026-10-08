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
import sys
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


def _run(playbook: Path, tmp_path: Path, *args: str, inventory: str = "localhost,", path_prepend: Path | None = None):
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    if path_prepend:
        env["PATH"] = f"{path_prepend}{os.pathsep}{env['PATH']}"
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
    ({}, "Refusing: pass -e confirm_reset=obs"),
    ({"confirm_reset": "other"}, "Refusing: pass -e confirm_reset=obs"),
    ({"confirm_reset": "obs"}, "Pass expected_repository_sha"),
    ({"confirm_reset": "obs", "expected_repository_sha": "0" * 40}, "not the reviewed " + "0" * 40),
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
                "-e", json.dumps({"confirm_reset": "obs"}), inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert "Pass expected_repository_sha" in proc.stdout, proc.stdout
    assert "TASK [Destroy existing deployment]" not in proc.stdout


@needs_ansible
@pytest.mark.parametrize("extra", [{}, {"confirm_reset": "other"}], ids=["no-confirm", "wrong-confirm"])
def test_clean_deploy_o11y_requires_the_confirmation_locally_too(extra, tmp_path):
    inv = tmp_path / "inv.ini"
    inv.write_text(LOCAL_O11Y)
    proc = _run(PLAYBOOKS / "clean-deploy-o11y.yml", tmp_path, "--check", "-e", json.dumps(extra),
                inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert "Refusing: pass -e confirm_reset=obs" in proc.stdout, proc.stdout
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
                json.dumps({"confirm_reset": "obs", "expected_repository_sha": head}), inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert f"The checkout is {head} with uncommitted files" in proc.stdout, proc.stdout
    assert "TASK [Destroy existing deployment]" not in proc.stdout


# tasks/clean-service.yml deletes a clone and runs root shell scripts built from public variables.
# An extra var chose what it deleted and where it ran (PR #470 and #473 reviews): `-e
# local_monorepo_dir=<path>`, `-e ansible_connection=local` or `ansible.builtin.local`, `-e
# local_mode=true`, `-e service_name=` / `-e monorepo_deploy_path=` aimed at another service. It now
# pins each name once, derives the mode from the play's host names (ansible_play_hosts_all, which an
# extra var cannot set), allows only ssh for a host that is not local-dev, and requires the host to
# be in the service's own group with the service's own deploy path.
#
# Prod runs here reach a host "over ssh" through a fake ssh executable that runs the command on this
# machine, so the real prod branch executes (with ansible_become=false and an account that has no
# home directory, its removals are no-ops) and the connection allowlist sees a genuine `ssh`
# connection. Engines are fakes that only log. Local runs use a host named like a local-dev one.
PROD_ACCOUNT = "svc-w55-test"  # no home directory exists for it
SERVICE_VARS = "service_name=o11y monorepo_deploy_path=platform/services/o11y/deployment"
PROD_REFUSAL = "Refusing: the teardown for"
PROD_TEARDOWN = "TASK [Stop and remove containers + volumes (prod)"
CONNECTION_REFUSAL = "but its connection is not the default ssh"
PROD_HOME = f"/home/{PROD_ACCOUNT}"


def _fake_engines(tmp_path: Path) -> Path:
    """A bin dir with docker and podman that only log their arguments, to put ahead on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in ("podman", "docker"):
        (bindir / name).write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "engine-args"}\nexit 0\n')
        (bindir / name).chmod(0o755)
    return bindir


def _clean_inventory(tmp_path: Path, hostvars: str = "") -> str:
    """A local-dev host: named <service>-local, on a local connection."""
    inv = tmp_path / "inv.ini"
    inv.write_text(f"[o11y_svc]\nobs-local ansible_connection=local {SERVICE_VARS} {hostvars}\n")
    return str(inv)


def _prod_inventory(tmp_path: Path, hostvars: str = "") -> str:
    """A remote host reached over ssh, through an ssh that runs the command here."""
    ssh = tmp_path / "fake-ssh"
    ssh.write_text('#!/bin/sh\nfor last; do :; done\nexec /bin/sh -c "$last"\n')
    ssh.chmod(0o755)
    inv = tmp_path / "prod-inv.ini"
    inv.write_text(
        f"[o11y_svc]\nobs ansible_host=127.0.0.1 ansible_ssh_executable={ssh} "
        f"ansible_ssh_pipelining=true ansible_python_interpreter={sys.executable} "
        f"ansible_user={PROD_ACCOUNT} container_engine=docker {SERVICE_VARS} {hostvars}\n")
    return str(inv)


def _clean_only(tmp_path: Path) -> Path:
    """The guard, then tasks/clean-service.yml alone with the includers' _monorepo_dir: the
    teardown without an executor's own gates, which a remote host's SHA gate would stop first."""
    play = [{"ansible.builtin.import_playbook": str(PLAYBOOKS / GUARD)},
            {"hosts": "o11y_svc", "gather_facts": False,
             "vars": {"_monorepo_dir": "{{ local_monorepo_dir | default('/home/' ~ (ansible_user | default('deploy'))"
                                       " ~ '/agent-cloud') }}"},
             "tasks": [{"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/clean-service.yml")}]}]
    out = tmp_path / "clean-only.yml"
    out.write_text(yaml.safe_dump(play))
    return out


def _prod_run(tmp_path: Path, *extra: str, hostvars: str = "", limit: bool = False):
    """A real run of the prod teardown on the ssh host. Returns (proc, engine log)."""
    args = ["--limit", "obs"] if limit else []
    proc = _run(_clean_only(tmp_path), tmp_path, *args,
                "-e", json.dumps({"ansible_become": False, "confirm_reset": "obs"}), *extra,
                inventory=_prod_inventory(tmp_path, hostvars), path_prepend=_fake_engines(tmp_path))
    log = tmp_path / "engine-args"
    return proc, (log.read_text() if log.exists() else "")


def _destroy_only(tmp_path: Path) -> Path:
    """clean-deploy-o11y.yml without its imported Fresh deploy, which would go on to render and
    deploy o11y (reading OpenBao) on this machine. The play under test is the real one: only the
    imports and the include are made absolute for a playbook that lives elsewhere."""
    plays = yaml.safe_load((PLAYBOOKS / "clean-deploy-o11y.yml").read_text())
    kept = [p for p in plays if p.get("name") != "Fresh deploy"]
    assert len(kept) == len(plays) - 1
    text = yaml.safe_dump(kept)
    for rel in ("refuse-internal-extra-vars.yml", "tasks/clean-service.yml", "tasks/assert-reset-confirmed.yml"):
        assert rel in text
        text = text.replace(rel, str(PLAYBOOKS / rel))
    out = tmp_path / "destroy-only.yml"
    out.write_text(text)
    return out


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries(
    "local_monorepo_dir", f"{PROD_HOME}/agent-cloud", lambda tmp_path: str(tmp_path / "victim")))
def test_a_forged_local_monorepo_dir_cannot_choose_what_the_prod_teardown_deletes(forge, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    proc, _log = _prod_run(tmp_path, "-e", forge(tmp_path))
    assert proc.returncode != 0, proc.stdout
    assert PROD_REFUSAL in proc.stdout, proc.stdout
    assert PROD_TEARDOWN not in proc.stdout and "TASK [Remove agent-cloud clone" not in proc.stdout
    assert (victim / "keep.txt").exists()


# Every spelling a forgery might use, including an executable plugin of ANOTHER namespace that a
# normalised comparison would have read as ssh.
CONNECTIONS = ["local", "ansible.builtin.local", "ansible.legacy.local", "ssh", "ansible.builtin.ssh", "foo.ssh"]


@needs_ansible
@pytest.mark.parametrize("kind", [0, 1, 2], ids=["plain", "context", "stateful"])
@pytest.mark.parametrize("conn", CONNECTIONS)
def test_a_forged_connection_cannot_move_a_remote_hosts_teardown_onto_the_controller(kind, conn, tmp_path):
    # A host that is not local-dev must not SET ansible_connection at all: key presence is what a
    # template cannot change between the check and the connection (a value check was passed by a
    # template that reads ssh at the check and local at the connection, and by <other>.ssh). So even
    # an honest-looking `ssh` is refused. `foo.ssh` is no plugin: a plain forgery of it fails at
    # connect, and the context and stateful ones, which read ssh there, are caught by the check.
    forge = forgeries.templated_forgeries("ansible_connection", "ssh", conn)[kind].values[0]
    proc, log = _prod_run(tmp_path, "-e", forge(tmp_path))
    assert proc.returncode != 0, proc.stdout
    assert CONNECTION_REFUSAL in proc.stdout or "was not found" in proc.stdout, proc.stdout
    assert PROD_TEARDOWN not in proc.stdout and "TASK [Remove agent-cloud clone" not in proc.stdout
    assert "compose" not in log


@needs_ansible
@pytest.mark.parametrize("conn", ["ssh", "ansible.builtin.ssh", "local"])
def test_a_remote_host_that_declares_a_connection_is_refused_too(conn, tmp_path):
    # The documented limit: a production host leaves ansible_connection unset. A declared one is
    # indistinguishable from a forged one, so it is refused until the check is extended in code.
    proc, log = _prod_run(tmp_path, hostvars=f"ansible_connection={conn}")
    assert proc.returncode != 0, proc.stdout
    assert CONNECTION_REFUSAL in proc.stdout, proc.stdout
    assert "compose" not in log


@needs_ansible
@pytest.mark.parametrize("flag, ok", [("ssh", True), ("local", False)])
def test_the_command_line_connection_must_still_be_ssh_for_a_remote_host(flag, ok, tmp_path):
    # -c is not in hostvars and is a plain string, so it is the one source left to read by value.
    proc, log = _prod_run(tmp_path, "-c", flag)
    if ok:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:
        assert proc.returncode != 0 and CONNECTION_REFUSAL in proc.stdout, proc.stdout
        assert "compose" not in log


@needs_ansible
@pytest.mark.parametrize("conn", ["local", "ansible.builtin.local", "ansible.legacy.local"])
def test_a_local_connection_set_from_outside_does_not_waive_the_o11y_gates_for_a_remote_host(conn, tmp_path):
    # Clean Deploy o11y waives its SHA, checkout and retention gates for a local-dev host only: a
    # remote host whose connection an extra var turns local still needs the reviewed commit.
    proc = _run(PLAYBOOKS / "clean-deploy-o11y.yml", tmp_path, "--check", "-e",
                json.dumps({"ansible_connection": conn, "confirm_reset": "obs"}),
                inventory=_prod_inventory(tmp_path))
    assert proc.returncode != 0, proc.stdout
    assert "Pass expected_repository_sha" in proc.stdout, proc.stdout
    assert "TASK [Destroy existing deployment]" not in proc.stdout


@needs_ansible
@pytest.mark.parametrize("conn", ["local", "ansible.builtin.local", "ansible.legacy.local"])
def test_a_local_dev_host_waives_the_o11y_gates_under_any_spelling_of_a_local_connection(conn, tmp_path):
    # The waiver reads the connection with its collection prefix removed, like the allowlist does,
    # so a local-dev inventory may spell it either way.
    genesis = tmp_path / "genesis"
    genesis.mkdir()
    inv = tmp_path / "inv.ini"
    inv.write_text(f"[o11y_svc]\nobs-local ansible_connection={conn} local_mode=true local_monorepo_dir={genesis} "
                   f"{SERVICE_VARS}\n")
    proc = _run(_destroy_only(tmp_path), tmp_path, "--check", "-e", json.dumps({"confirm_reset": "obs-local"}),
                inventory=str(inv))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Pass expected_repository_sha" not in proc.stdout
    assert "TASK [Stop and remove containers + volumes (local) for o11y]" in proc.stdout


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("inventory_hostname", "obs", "obs-local"))
def test_a_forged_inventory_name_cannot_pass_a_remote_host_as_local_dev(forge, tmp_path):
    # inventory_hostname (and group_names) CAN be set by an extra var, so the checks key on
    # ansible_play_hosts_all and groups, which cannot. --limit skips the run-start guard (its
    # limitation), so this reaches clean-service.yml with the forged name and a local connection.
    proc, log = _prod_run(tmp_path, "-e", forge(tmp_path), "-e", json.dumps({"ansible_connection": "local"}),
                          limit=True)
    assert proc.returncode != 0, proc.stdout
    assert CONNECTION_REFUSAL in proc.stdout, proc.stdout
    assert "compose" not in log and PROD_TEARDOWN not in proc.stdout


def _local_teardown_fixture(tmp_path: Path):
    """A local-dev host whose teardown can really run: a genesis tree with a compose file, and a
    fake `podman` ahead of the real engines on PATH that only logs its arguments. Returns the
    inventory, the bin dir, the argument log and the marker a smuggled command would create."""
    genesis = tmp_path / "genesis"
    (genesis / "platform/services/o11y/deployment").mkdir(parents=True)
    (genesis / "platform/services/o11y/deployment/compose.yml").write_text("services: {}\n")
    log, marker = tmp_path / "engine-args", tmp_path / "pwned"
    bindir = _fake_engines(tmp_path)
    return _clean_inventory(tmp_path, f"local_mode=true local_monorepo_dir={genesis} container_engine=podman"), \
        bindir, log, marker, genesis


# Each name the teardown scripts interpolate, with the inventory's value and a forgery that
# would run a command (or, for the engine, name a program) when a script renders it.
LOCAL_INPUTS = [
    ("local_monorepo_dir", lambda t: str(t / "genesis"), lambda t: f"{t}/genesis/x$(touch {t}/pwned)"),
    ("service_name", lambda t: "o11y", lambda t: f"o11y$(touch {t}/pwned)"),
    ("monorepo_deploy_path", lambda t: "platform/services/o11y/deployment",
     lambda t: f"platform/services/o11y/deployment/$(touch {t}/pwned)"),
    ("container_engine", lambda t: "podman", lambda t: f"{t}/evil"),
]


@needs_ansible
@pytest.mark.parametrize("kind", [0, 1, 2], ids=["plain", "context", "stateful"])
@pytest.mark.parametrize("name, honest, forged", LOCAL_INPUTS, ids=["dir", "service", "path", "engine"])
def test_a_forged_teardown_input_cannot_run_a_command_in_the_local_teardown(kind, name, honest, forged, tmp_path):
    # The teardown scripts and the verify step read the pinned names, so what the checks saw is
    # what ran. A refusal or the honest value is fine; a smuggled command or program is not.
    inv, bindir, log, marker, genesis = _local_teardown_fixture(tmp_path)
    evil = tmp_path / "evil"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\n")
    evil.chmod(0o755)
    forge = forgeries.templated_forgeries(name, honest, forged)[kind].values[0]
    proc = _run(_destroy_only(tmp_path), tmp_path, "-e", forge(tmp_path),
                "-e", json.dumps({"confirm_reset": "obs-local"}), inventory=inv, path_prepend=bindir)
    assert not marker.exists(), proc.stdout
    if proc.returncode == 0:
        assert "compose" in log.read_text(), proc.stdout  # the honest teardown ran, on the fake engine
    else:
        assert "Refusing" in proc.stdout, proc.stdout
    if kind != 2:
        assert proc.returncode != 0, proc.stdout  # plain and context forgeries are refused outright


@needs_ansible
@pytest.mark.parametrize("mode", ["local", "prod"])
@pytest.mark.parametrize("name", ["local_monorepo_dir", "service_name", "monorepo_deploy_path", "container_engine"])
def test_a_late_flipping_template_never_reaches_the_teardown_scripts(mode, name, tmp_path):
    # forgeries.templated_forgeries is honest on its FIRST rendering, which the task library's own
    # early reads consume before the pin. A template honest for the first N renderings reaches
    # the pin honest for some N, and whatever reads the public name afterwards sees the forgery.
    # Over N = 1..8 the smuggled command must never run: every read after the pin is of the fact.
    for limit in range(1, 9):
        run = tmp_path / f"n{limit}"
        run.mkdir()
        if mode == "local":
            inv, bindir, _log, marker, _genesis = _local_teardown_fixture(run)
            honest, bad = {n: (h, f) for n, h, f in LOCAL_INPUTS}[name]
            playbook, extra = _destroy_only(run), {"confirm_reset": "obs-local"}
        else:
            inv, bindir, marker = _prod_inventory(run), _fake_engines(run), run / "pwned"
            honest = {"local_monorepo_dir": lambda t: f"{PROD_HOME}/agent-cloud",
                      "service_name": lambda t: "o11y", "container_engine": lambda t: "docker",
                      "monorepo_deploy_path": lambda t: "platform/services/o11y/deployment"}[name]
            bad = {n: f for n, _h, f in LOCAL_INPUTS}[name]
            playbook, extra = _clean_only(run), {"confirm_reset": "obs"}
        (run / "evil").write_text(f"#!/bin/sh\ntouch {marker}\n")
        (run / "evil").chmod(0o755)
        counter = run / "renders"
        shell = f"n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) > {counter}; echo $n"
        cond = f"(lookup('ansible.builtin.pipe', {forgeries._string(shell)}) | int < {limit})"
        template = ("{{ " + cond + " | ternary(" + forgeries._literal(honest(run)) + ", "
                    + forgeries._literal(bad(run)) + ") }}")
        _run(playbook, run, "-e", json.dumps({name: template}),
             "-e", json.dumps({**extra, "ansible_become": False}), inventory=inv, path_prepend=bindir)
        assert not marker.exists(), f"{mode}: {name} honest for {limit} renderings ran the smuggled command"


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", True, False))
def test_a_forged_local_mode_cannot_move_a_local_genesis_path_into_the_prod_teardown(forge, tmp_path):
    # The local inventory's local_monorepo_dir is the shared genesis tree. A launch that forges
    # local_mode false would hand it to the prod branch, which deletes its directory. The stateful
    # forgery is honest on its first rendering, and the first read of local_mode is the one that
    # pins it, so the run then is the honest local one: the clone removal is skipped, not refused.
    genesis = tmp_path / "genesis"
    genesis.mkdir()
    (genesis / "keep.txt").write_text("x")
    inv = _clean_inventory(tmp_path, f"local_mode=true local_monorepo_dir={genesis}")
    proc = _run(_destroy_only(tmp_path), tmp_path, "--check", "-e", forge(tmp_path),
                "-e", json.dumps({"confirm_reset": "obs-local"}), inventory=inv)
    refused = PROD_REFUSAL in proc.stdout and proc.returncode != 0
    skipped = re.search(r"TASK \[Remove agent-cloud clone[^\n]*\*\nskipping: \[obs-local\]", proc.stdout)
    assert refused or skipped, proc.stdout
    assert (genesis / "keep.txt").exists()


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", False, True))
def test_a_forged_local_mode_cannot_run_a_prod_host_in_a_launcher_chosen_tree(forge, tmp_path):
    # PR #473 review: local_mode=true on a production host skipped the prod clone check and ran
    # compose down -v in whatever local_monorepo_dir named. The mode follows the hosts' names now,
    # and a local_mode that disagrees is refused. The tree here has a compose file a regression
    # would run.
    tree = tmp_path / "tree"
    (tree / "platform/services/o11y/deployment").mkdir(parents=True)
    (tree / "platform/services/o11y/deployment/compose.yml").write_text("services: {}\n")
    proc, log = _prod_run(tmp_path, "-e", forge(tmp_path), "-e", json.dumps({"local_monorepo_dir": str(tree)}))
    assert proc.returncode != 0, proc.stdout
    assert PROD_REFUSAL in proc.stdout, proc.stdout
    assert "compose" not in log
    assert PROD_TEARDOWN not in proc.stdout


@needs_ansible
def test_a_local_mode_that_disagrees_with_the_hosts_is_refused_by_name(tmp_path):
    proc, log = _prod_run(tmp_path, "-e", json.dumps({"local_mode": True}))
    assert proc.returncode != 0, proc.stdout
    assert "The mode follows the hosts, not the variable" in proc.stdout, proc.stdout
    assert "compose" not in log


# Each name the prod teardown interpolates into a path or the root shell script, with the value
# the inventory declares and a forged one that reaches outside the checkout, injects a command,
# or aims the teardown at another service (its group, its deploy path, or a directory named like
# one: `file state=absent` is recursive).
PROD_INPUTS = [
    ("ansible_user", PROD_ACCOUNT, ".."),
    ("service_name", "o11y", "../../etc"),
    ("service_name", "o11y", "caddy"),
    ("service_name", "o11y", "Documents"),
    ("monorepo_deploy_path", "platform/services/o11y/deployment", "platform/../../../etc"),
    ("monorepo_deploy_path", "platform/services/o11y/deployment", "x; touch /tmp/pwned"),
    ("monorepo_deploy_path", "platform/services/o11y/deployment", "platform/services/caddy/deployment"),
    ("container_engine", "docker", "rm"),
]
PROD_INPUT_IDS = ["user-dotdot", "service-dotdot", "service-other", "service-folder", "path-dotdot",
                  "path-injection", "path-other", "engine"]


@needs_ansible
@pytest.mark.parametrize("kind", [0, 1, 2], ids=["plain", "context", "stateful"])
@pytest.mark.parametrize("name, honest, forged", PROD_INPUTS, ids=PROD_INPUT_IDS)
def test_a_forged_prod_teardown_input_is_refused_before_any_prod_task(kind, name, honest, forged, tmp_path):
    forge = forgeries.templated_forgeries(name, honest, forged)[kind].values[0]
    proc, _log = _prod_run(tmp_path, "-e", forge(tmp_path))
    if kind == 2 and name == "container_engine":
        # container_engine is first read by the pin, so the stateful forgery is honest there and
        # the check and the teardown both see docker: the run is the honest one.
        assert PROD_REFUSAL in proc.stdout or proc.returncode == 0, proc.stdout
        return
    assert proc.returncode != 0, proc.stdout
    assert PROD_REFUSAL in proc.stdout, proc.stdout
    assert PROD_TEARDOWN not in proc.stdout and "TASK [Remove agent-cloud clone" not in proc.stdout
    assert "TASK [Remove convenience symlink" not in proc.stdout


@needs_ansible
@pytest.mark.parametrize("kind", [0, 1, 2], ids=["plain", "context", "stateful"])
@pytest.mark.parametrize("other", ["caddy", "Documents"])
def test_a_forged_service_with_its_own_deploy_path_is_refused_for_a_host_outside_its_group(kind, other, tmp_path):
    # A consistent forgery: another service's name AND its deploy path, which passes the path
    # check on its own. Only the inventory's groups say this host does not run that service.
    forge = forgeries.templated_forgeries("service_name", "o11y", other)[kind].values[0]
    proc, log = _prod_run(tmp_path, "-e", forge(tmp_path),
                          "-e", json.dumps({"monorepo_deploy_path": f"platform/services/{other}/deployment"}))
    assert proc.returncode != 0, proc.stdout
    assert PROD_REFUSAL in proc.stdout, proc.stdout
    assert PROD_TEARDOWN not in proc.stdout and "TASK [Remove agent-cloud clone" not in proc.stdout
    assert "compose" not in log


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries(
    "local_home_dir", PROD_HOME, lambda tmp_path: str(tmp_path / "home")))
def test_a_forged_local_home_dir_is_not_read_by_the_prod_teardown(forge, tmp_path):
    # The convenience symlink was <local_home_dir>/<service_name>; it is built from the account now.
    (tmp_path / "home" / "o11y").mkdir(parents=True)
    proc, _log = _prod_run(tmp_path, "-e", forge(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "home" / "o11y").is_dir()


def _lifted(tmp_path: Path, prefixes: list[str], variables: dict, replace: dict | None = None) -> Path:
    """A play of the named tasks of tasks/clean-service.yml, lifted verbatim (paths optionally
    redirected into tmp_path), run on localhost with the given facts already set."""
    tasks = yaml.safe_load((PLAYBOOKS / "tasks/clean-service.yml").read_text())
    picked = [next(t for t in tasks if t["name"].startswith(p)) for p in prefixes]
    text = yaml.safe_dump(picked, width=10**6)
    for old, new in (replace or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
             "tasks": yaml.safe_load(text)}]
    out = tmp_path / "lifted.yml"
    out.write_text(yaml.safe_dump(play))
    return out


SYMLINK_TASKS = ["Look for the convenience symlink", "Remove convenience symlink"]


@needs_ansible
@pytest.mark.parametrize("shape", ["directory", "symlink", "missing"])
def test_the_convenience_symlink_removal_only_ever_removes_a_link(shape, tmp_path):
    # PR #473 review: the path is /home/<user>/<service_name> and `file state=absent` is recursive,
    # so a real directory of that name (`-e service_name=Documents` passed the shape check) was lost.
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    entry = home / "o11y"
    if shape == "directory":
        entry.mkdir()
        (entry / "keep.txt").write_text("x")
    elif shape == "symlink":
        entry.symlink_to(target)
    play = _lifted(tmp_path, SYMLINK_TASKS, {"_clean_local_mode": False, "_clean_user": "u", "_clean_service": "o11y"},
                   {"/home/{{ _clean_user }}": str(home)})
    proc = _run(play, tmp_path, "-e", json.dumps({"ansible_become": False}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (target / "keep.txt").exists()
    if shape == "directory":
        assert (entry / "keep.txt").exists()
    else:
        assert not entry.exists() and not entry.is_symlink()


@needs_ansible
@pytest.mark.parametrize("task", ["Stop and remove containers + volumes (prod)",
                                  "Stop and remove containers + volumes (local)"])
def test_the_container_removal_matches_the_service_name_literally(task, tmp_path):
    # PR #473 review: grep "^o11y.b" matched o11yXb. regex_escape makes the dot literal.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "rm-args"
    (bindir / "docker").write_text(
        '#!/bin/sh\ncase "$1" in\n  ps) printf "o11yXb\\no11y.b\\n";;\n'
        f'  rm) echo "$@" >> {log};;\nesac\nexit 0\n')
    (bindir / "docker").chmod(0o755)
    play = _lifted(tmp_path, [task], {"_clean_local_mode": "local" in task, "_clean_service": "o11y.b",
                                      "_clean_dir": str(tmp_path), "_clean_path": "deployment",
                                      "_clean_engine": "docker", "_clean_local_engine": "docker"})
    proc = _run(play, tmp_path, "-e", json.dumps({"ansible_become": False}), path_prepend=bindir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    removed = log.read_text()
    assert "o11y.b" in removed and "o11yXb" not in removed, removed


@needs_ansible
def test_a_genuinely_local_o11y_host_passes_the_gates_and_reaches_the_destroy(tmp_path):
    # PR #470 review LOW-2: the local tests covered refusals only. A host that is local to the
    # controller (local dev), named by the confirm, waives the SHA, checkout and retention gates
    # and reaches the local teardown; the prod teardown and its path check are not run.
    genesis = tmp_path / "genesis"
    genesis.mkdir()
    inv = _clean_inventory(tmp_path, f"local_mode=true local_monorepo_dir={genesis}")
    proc = _run(_destroy_only(tmp_path), tmp_path, "--check", "-e", json.dumps({"confirm_reset": "obs-local"}),
                inventory=inv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Refusing" not in proc.stdout and "Pass expected_repository_sha" not in proc.stdout
    assert "TASK [Destroy existing deployment]" in proc.stdout
    assert "TASK [Stop and remove containers + volumes (local) for o11y]" in proc.stdout
    assert "TASK [Verify clean]" in proc.stdout and "No o11y" in proc.stdout


@needs_ansible
def test_the_declared_prod_checkout_passes_the_checks(tmp_path):
    # The honest prod launch: a host reached over ssh, no local_monorepo_dir, so the clone is
    # /home/<ansible_user>/agent-cloud. The removals are no-ops: the account has no home here.
    proc, log = _prod_run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert PROD_REFUSAL not in proc.stdout and CONNECTION_REFUSAL not in proc.stdout
    assert "TASK [Remove agent-cloud clone (prod; may contain root-owned files)]" in proc.stdout, proc.stdout
    assert "ps -a" in log


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
