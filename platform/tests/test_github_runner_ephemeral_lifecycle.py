"""The ephemeral runner lifecycle's configuration (openspec add-ephemeral-github-runners).

Structural, because each property here fails silently on a live host: a persistent host
whose unit changes (and restarts) merely because this code merged, an ephemeral unit that
systemd restarts into a used home, a configuration that crosses a visible task or lands
readable by a job, a schedule that mints for a host still on the persistent lifecycle, and
a supervisor that retries a failed reset instead of stopping.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOYMENT = REPO / "platform/services/github-runner/deployment"
TEMPLATES = DEPLOYMENT / "templates"
PLAYBOOKS = REPO / "platform/playbooks"
DEPLOY = PLAYBOOKS / "deploy-github-runner.yml"
LIFECYCLE = PLAYBOOKS / "tasks/github-runner-lifecycle.yml"
STOCK = PLAYBOOKS / "tasks/github-runner-jit-stock.yml"
STOCK_PLAY = PLAYBOOKS / "stock-github-runner-jit.yml"


def render(name: str, **values) -> str:
    # Ansible's template module renders with trim_blocks on.
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True, undefined=jinja2.StrictUndefined)
    return env.from_string((TEMPLATES / name).read_text()).render(**values)


def directives(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


UNIT_VARS = {"github_runner_name": "gh-runner-01", "github_runner_home": "/opt/actions-runner"}


def test_the_persistent_unit_is_unchanged_by_the_lifecycle_switch():
    # Merging this must not change a persistent host's unit: a changed unit restarts the
    # runner on the next deploy, cancelling a job for nothing.
    default = render("actions-runner.service.j2", **UNIT_VARS)
    persistent = render("actions-runner.service.j2", github_runner_lifecycle="persistent", **UNIT_VARS)
    assert default == persistent
    d = directives(default)
    assert "Restart=always" in d and "RestartSec=5" in d
    assert not [x for x in d if "jit" in x.lower()]


def test_the_ephemeral_unit_runs_once_with_the_config_from_a_file_it_removes():
    d = directives(render("actions-runner.service.j2", github_runner_lifecycle="ephemeral", **UNIT_VARS))
    assert "Restart=no" in d and "Restart=always" not in d
    assert "EnvironmentFile=-%t/actions-runner-jit.env" in d
    assert "ExecStartPost=/bin/rm -f %t/actions-runner-jit.env" in d
    # Everything the persistent design proved about the runner process still holds.
    for kept in ("NoNewPrivileges=false", "KillMode=mixed", "Environment=RUNNER_MANUALLY_TRAP_SIG=1",
                 "SuccessExitStatus=143", "ExecStart=/opt/actions-runner/run.sh"):
        assert kept in d
    # The configuration never reaches the command line.
    assert not [x for x in d if x.startswith("ExecStart=") and "jit" in x.lower()]


def test_a_quarantine_is_not_retried_by_systemd():
    d = directives(render("github-runner-ephemeral.service.j2", github_runner_name="gh-runner-01"))
    assert "Restart=on-failure" in d and "RestartPreventExitStatus=3" in d
    assert "ExecStart=/usr/local/libexec/github-runner/ephemeral-supervisor.sh loop" in d
    assert "EnvironmentFile=/etc/github-runner/ephemeral.env" in d


def test_the_supervisor_declaration_carries_no_credential():
    text = render(
        "github-runner-ephemeral.env.j2",
        github_runner_account="ghrunner",
        github_runner_home="/opt/actions-runner",
        github_runner_seed="/opt/actions-runner-seed",
        github_runner_state_dir="/var/lib/github-runner",
        github_runner_jit_max_age_minutes=1200,
        github_runner_max_start_failures=3,
    )
    keys = {line.split("=", 1)[0] for line in directives(text)}
    assert keys == {"RUNNER_ACCOUNT", "RUNNER_HOME", "RUNNER_SEED", "RUNNER_STATE_DIR",
                    "RUNNER_JIT_MAX_AGE_MINUTES", "RUNNER_MAX_START_FAILURES"}


def tasks(path: Path) -> list[dict]:
    """Every task in a task file or playbook, blocks flattened."""
    doc = yaml.safe_load(path.read_text())
    out: list[dict] = []

    def walk(items):
        for t in items or []:
            if not isinstance(t, dict):
                continue
            if "tasks" in t:
                walk(t["tasks"])
                continue
            out.append(t)
            for key in ("block", "rescue", "always"):
                walk(t.get(key))

    walk(doc)
    return out


def task(path: Path, name: str) -> dict:
    found = [t for t in tasks(path) if t.get("name") == name]
    assert len(found) == 1, f"{path.name}: {name!r} found {len(found)} times"
    return found[0]


KEY_EXPR = "_grs_app_secret.json.data.data.app_private_key"


def test_every_task_that_handles_the_key_or_a_configuration_is_hidden_and_on_the_controller():
    for t in tasks(STOCK):
        text = yaml.safe_dump(t)
        if KEY_EXPR in text or "encoded_jit_config" in text or "_grs_bao_auth.json" in text:
            assert t.get("no_log") is True, t["name"]
        if KEY_EXPR in text or "approle/login" in text:
            assert t.get("delegate_to") == "localhost", t["name"]


def test_the_key_goes_to_the_helper_on_stdin_never_argv():
    for name in ("JIT stock: list, prune or retire this host's runner records",
                 "JIT stock: mint the configurations the stock is missing"):
        t = task(STOCK, name)
        assert t["ansible.builtin.command"]["stdin"] == "{{ " + KEY_EXPR + " }}"
        assert "--key -" in t["ansible.builtin.command"]["cmd"]
        assert KEY_EXPR not in t["ansible.builtin.command"]["cmd"]


def test_configurations_land_root_only_and_the_stock_directory_is_root_only():
    t = task(STOCK, "JIT stock: write each configuration to the host's root-only stock")
    copy = t["ansible.builtin.copy"]
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0600")
    assert copy["dest"].endswith(".jit\"") or copy["dest"].endswith(".jit")
    dirs = task(LIFECYCLE, "Lifecycle: create the supervisor's directories")["loop"]
    modes = {d["path"]: d["mode"] for d in dirs}
    assert modes["{{ _grl_state }}"] == "0700" and modes["{{ _grl_state }}/jit"] == "0700"


def test_the_supervisor_runs_a_root_owned_copy_not_the_checkout():
    copy = task(LIFECYCLE, "Lifecycle: install the supervisor script (a root-owned copy, never run from the checkout)")
    c = copy["ansible.builtin.copy"]
    assert c["dest"] == "{{ _grl_supervisor }}" and (c["owner"], c["group"], c["mode"]) == ("root", "root", "0755")


def test_the_deploy_registers_nothing_persistent_on_an_ephemeral_host():
    for name in ("Mint a single-use runner registration token", "Install and register the runner"):
        when = str(task(DEPLOY, name).get("when"))
        assert "_lifecycle == 'persistent'" in when, name
    start = task(DEPLOY, "Reload the account's systemd and start the runner")["ansible.builtin.systemd_service"]
    assert start["enabled"] == "{{ _lifecycle == 'persistent' }}"
    assert start["state"] == "{{ 'started' if _lifecycle == 'persistent' else 'stopped' }}"


def test_the_home_is_reset_before_it_is_rendered_and_activated_after_the_probes():
    names = [t.get("name") for t in tasks(DEPLOY)]
    prepare = names.index("Prepare the runner lifecycle")
    assert prepare < names.index("Render the runner's configuration")
    assert prepare < names.index("Template the runner's declaration, job env and job hooks")
    activate = names.index("Activate the runner lifecycle")
    probe = "Confirm a job container can be created with the runner's mounts, from inside the user manager"
    assert activate > names.index(probe)
    assert activate < names.index("Gather this host's service-deploy result")


def test_a_reset_that_is_not_clean_fails_the_deploy():
    for name in ("Lifecycle: reset the runner account (every process and file of it destroyed)",
                 "Lifecycle: reset again (the deploy's probes ran processes as the account)"):
        assert "'reset=clean' not in" in task(LIFECYCLE, name)["failed_when"]


def test_the_stock_is_filled_before_the_supervisor_starts():
    names = [t.get("name") for t in tasks(LIFECYCLE)]
    stock = names.index("Lifecycle: stock just-in-time configurations and retire the persistent registration")
    assert stock < names.index("Lifecycle: start the supervisor")


def test_the_schedule_skips_persistent_hosts_and_fails_on_quarantine():
    play = yaml.safe_load(STOCK_PLAY.read_text())
    assert play[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    first = play[1]["tasks"][0]
    assert first["ansible.builtin.meta"] == "end_host" and first["when"] == "_lifecycle != 'ephemeral'"
    gate = task(STOCK_PLAY, "Require a running, unquarantined supervisor")["ansible.builtin.assert"]["that"]
    assert any("quarantine=no" in str(x) for x in gate)


def test_the_stock_template_is_scheduled_and_dev_bound():
    catalog = yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]
    (t,) = [t for t in catalog if t["playbook"] == "platform/playbooks/stock-github-runner-jit.yml"]
    assert t["repository"] == "agent-cloud dev" and t["schedule"]["cron"] == "*/10 * * * *"
    assert not t.get("survey_vars")


def test_every_lifecycle_mutation_is_skipped_in_a_dry_run():
    # A dry run reads; it never stops a runner, resets an account, snapshots, or starts and
    # removes the supervisor. Every such task sits in a block guarded on check mode.
    blocks = [t for t in yaml.safe_load(LIFECYCLE.read_text()) if "block" in t]
    assert len(blocks) == 3
    for b in blocks:
        assert "not ansible_check_mode" in str(b["when"]), b["name"]
    loose = [t["name"] for t in yaml.safe_load(LIFECYCLE.read_text()) if "block" not in t]
    assert all(n.startswith(("Lifecycle: resolve", "Lifecycle: record", "Lifecycle: is the")) for n in loose), loose


def test_the_stock_writes_and_deletes_nothing_in_a_dry_run():
    for name in ("JIT stock: mint the configurations the stock is missing",
                 "JIT stock: write each configuration to the host's root-only stock"):
        assert "not ansible_check_mode" in task(STOCK, name)["when"], name
    cmd = task(STOCK, "JIT stock: list, prune or retire this host's runner records")["ansible.builtin.command"]["cmd"]
    assert "'list-runners' if (_grs_mode == 'verify' or ansible_check_mode) else 'prune-runners'" in cmd


def test_leaving_cleans_up_before_the_persistent_registration():
    # The leaving cleanup runs in the prepare phase, so a failed prune stops the play before
    # the persistent runner registers (CodeRabbit review of #716).
    blocks = {t["name"]: t for t in yaml.safe_load(LIFECYCLE.read_text()) if "block" in t}
    leave = blocks["Lifecycle (prepare): leave the ephemeral lifecycle"]
    assert "grl_phase == 'prepare'" in leave["when"] and "_grl_leaving" in leave["when"]
    activate = blocks["Lifecycle (activate): seed, start and stock the ephemeral runner"]
    assert "_grl_ephemeral" in activate["when"]
    names = [t.get("name") for t in tasks(DEPLOY)]
    assert names.index("Prepare the runner lifecycle") < names.index("Install and register the runner")
