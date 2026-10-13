"""Wave 3 of the `service-deploy` step result (change service-deployment-workflow, task 7.1): NetBox.

NetBox deploys through its own compose wrapper and library, not platform/lib/common.sh, so the
wave-1 guards in test_service_deploy_step_result.py (which look for compose_up_if_changed) do not
reach it. The same two criteria hold here: the verify phase records the result through the shared
task, and an unchanged re-run reports no change. The shell half (deploy.sh against a stub engine)
is test_netbox_change_aware.bats; this file holds the playbook half:

- Phase 2 reads deploy.sh's DEPLOY_CHANGED line into changed_when and carries both operator levers;
- the survey declares them;
- post-deploy (Phase 3) and the orb-agent credential refresh (Phase 4) run only when they are owed,
  proven by running the playbook's own `when:` expressions through ansible, not by reading the YAML;
- the service-deploy result is the LAST task of the verify play and reads `_deploy` alone.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/deploy-netbox.yml"
DEPLOY_SH = REPO / "platform/services/netbox/deployment/deploy.sh"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def _plays():
    return playbook_yaml.plays(PLAYBOOK)


def _play(prefix: str) -> dict:
    (play,) = [p for p in _plays() if p.get("name", "").startswith(prefix)]
    return play


def _task(play: dict, name: str) -> dict:
    (task,) = [t for t in play["tasks"] if t.get("name") == name]
    return json.loads(json.dumps(task))


def test_verify_play_records_the_service_deploy_result_last_and_reads_only_the_deploy():
    tasks = _play("Phase 5")["tasks"]
    last = tasks[-1]
    assert str(last.get("ansible.builtin.include_tasks", "")) == "tasks/emit-service-deploy-result.yml", last
    # `_deploy`, never a result that also folds in the post-deploy or credential steps: those run
    # when they are owed, and a re-run that owes none must report no change.
    assert last["vars"] == {"service_deploy_result_var": "_deploy"}
    includes = [
        t for t in playbook_yaml.tasks(_plays())
        if str(t.get("ansible.builtin.include_tasks", "")).endswith("emit-service-deploy-result.yml")
    ]
    assert len(includes) == 1


def test_deploy_task_reads_the_change_line_and_carries_both_levers():
    (task,) = [t for t in playbook_yaml.tasks(_plays()) if "deploy.sh" in str(t.get("ansible.builtin.shell", ""))
               and "post-deploy" not in str(t.get("ansible.builtin.shell", ""))]
    assert task["register"] == "_deploy"
    assert task["changed_when"] == "'DEPLOY_CHANGED=true' in _deploy.stdout_lines"
    for env, var in (("FORCE_RECREATE", "deploy_force_recreate"), ("NETBOX_FORCE_REBUILD", "netbox_force_rebuild")):
        expr = task["environment"][env]
        render = lambda **kw: jinja2.Environment().from_string(expr).render(**kw)  # noqa: B023,E731
        assert render() == "", f"{env}: the default must not force"
        for v in (True, "true", "True", "TRUE"):
            assert render(**{var: v}) == "true", (env, v)
        for v in ("yes", "1", 1, "on", "y", False, "false", ""):
            assert render(**{var: v}) == "", (env, v)


def test_the_survey_declares_both_levers_off_by_default():
    templates = yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]
    (template,) = [t for t in templates if t["playbook"] == "platform/playbooks/deploy-netbox.yml"
                   and t["name"] == "Deploy NetBox"]
    surveys = {v["name"]: v for v in template["survey_vars"]}
    for name in ("deploy_force_recreate", "netbox_force_rebuild"):
        assert surveys[name]["default_value"] == "false", name


def _eval_gate(tmp_path, tasks, host_vars, check=False):
    """Run `tasks` (each a debug task carrying a playbook task's own `when`) and return the names
    that ran."""
    play = [{"hosts": "svc", "gather_facts": False, "tasks": tasks}]
    (tmp_path / "play.yml").write_text(json.dumps(play))
    inv = {"all": {"children": {"svc": {"hosts": {"h1": {"ansible_connection": "local", **host_vars}}}}}}
    (tmp_path / "inv.yml").write_text(json.dumps(inv))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    cmd = ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml")]
    proc = subprocess.run(
        [*cmd, *(["--check"] if check else [])],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False,
    )
    assert proc.returncode == 0, proc.stdout[-3000:]
    return set(re.findall(r'"msg": "RAN (\w+)"', proc.stdout))


def _gate_tasks():
    """The two gated steps, each as a debug task with the playbook's own condition."""
    post = _task(_play("Phase 3"), "Run post-deploy.sh (migrations, superuser, OAuth2)")
    creds = _task(_play("Phase 4"), "Manage Diode orb-agent credentials")
    mark = _task(_play("Phase 4"), "Record that the orb-agent credential refresh finished")
    return [
        {"name": "post", "ansible.builtin.debug": {"msg": "RAN post"}, "when": post["when"]},
        {"name": "creds", "ansible.builtin.debug": {"msg": "RAN creds"}, "when": creds["when"]},
        {"name": "mark", "ansible.builtin.debug": {"msg": "RAN mark"}, "when": mark["when"]},
    ]


def _fact_tasks():
    """The playbook's own fact tasks, so a rename of a fact they set turns this red."""
    out = []
    for name in ("Is an orb-agent credential stored in OpenBao?", "Is a credential refresh owed?"):
        task = _task(_play("Phase 4"), name)
        out.append({k: v for k, v in task.items() if k != "no_log"})
    return out


UNCHANGED = {"changed": False}
CHANGED = {"changed": True}
SKIPPED = {"skipped": True, "changed": False}
STORED = {"orb_agent_client_id": "x", "orb_agent_client_secret": "y"}
MARKER = {"stat": {"exists": True}}
NO_MARKER = {"stat": {"exists": False}}


@needs_ansible
@pytest.mark.parametrize(
    "deploy,marker,cred_marker,resolved,post,creds",
    [
        # an unchanged re-run that owes nothing runs neither step
        (UNCHANGED, MARKER, MARKER, STORED, False, False),
        # a changed deploy owes both: the stack was recreated and the orb-agent stopped with it
        (CHANGED, MARKER, MARKER, STORED, True, True),
        # a run that died before post-deploy finished is finished by the next one
        (UNCHANGED, NO_MARKER, MARKER, STORED, True, True),
        # a run that died before the credential refresh finished owes it on the next, though
        # nothing else changed and post-deploy is done
        (UNCHANGED, MARKER, NO_MARKER, STORED, False, True),
        # a credential that was never stored is minted even on an unchanged stack
        (UNCHANGED, MARKER, MARKER, {"orb_agent_client_id": "x"}, False, True),
        (UNCHANGED, MARKER, MARKER, {"orb_agent_client_id": "", "orb_agent_client_secret": ""}, False, True),
        (UNCHANGED, MARKER, MARKER, {}, False, True),
    ],
)
def test_post_deploy_and_credential_refresh_run_only_when_owed(
    tmp_path, deploy, marker, cred_marker, resolved, post, creds
):
    tasks = [
        *({"name": "fact", **f} for f in _fact_tasks()),
        *_gate_tasks(),
    ]
    ran = _eval_gate(tmp_path, tasks, {
        "_deploy": deploy, "_post_deploy_marker": marker, "_credential_marker": cred_marker,
        "_resolved": resolved,
        # the playbook registers _post_deploy only when post-deploy ran: absent here unless it did
        **({"_post_deploy": CHANGED} if post else {}),
    })
    assert ("post" in ran, "creds" in ran) == (post, creds), ran
    # the completion marker is written exactly when the refresh ran, never before it succeeded
    assert ("mark" in ran) == creds, ran


@needs_ansible
def test_a_dry_run_skips_post_deploy_but_still_walks_the_credential_task(tmp_path):
    tasks = [*({"name": "fact", **f} for f in _fact_tasks()), *_gate_tasks()]
    ran = _eval_gate(
        tmp_path, tasks,
        {"_deploy": SKIPPED, "_post_deploy_marker": MARKER, "_credential_marker": MARKER, "_resolved": STORED},
        check=True,
    )
    assert ran == {"creds"}, ran  # a dry run walks the task to report, and writes no marker


@needs_ansible
@pytest.mark.parametrize("stdout,warned", [(["ORB_AGENT_RUNNING=false", "DEPLOY_CHANGED=false"], True),
                                           (["DEPLOY_CHANGED=false"], False)])
def test_a_stopped_orb_agent_is_warned_about(tmp_path, stdout, warned):
    task = _task(_play("Phase 2"), "Warn: discovery is stopped")
    probe = {"name": "warn", "ansible.builtin.debug": {"msg": "RAN warn"}, "when": task["when"]}
    ran = _eval_gate(tmp_path, [probe], {"_deploy": {"stdout_lines": stdout, "changed": False}})
    assert ("warn" in ran) == warned, ran


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_deploy_sh_leaves_everything_alone_before_it_can_stop_anything():
    code = _code(DEPLOY_SH)
    unchanged = code.index('echo "DEPLOY_CHANGED=false"')
    for later in ("stop_orb_agent", "compose down", "sync_postgres_passwords", "compose up"):
        assert unchanged < code.index(later), f"{later} comes before the unchanged exit"
    assert "exit 0" in code[unchanged : unchanged + 60], "the unchanged branch must exit before the stop"
    assert "--force-recreate" not in code
    assert code.rindex('echo "DEPLOY_CHANGED=true"') > code.index("wait_for_healthy")
    # both branches name a stopped agent before they report: the unchanged branch used to claim it
    # left the agent alone without looking
    assert code.count("warn_if_orb_agent_stopped") == 2
    assert code.index("warn_if_orb_agent_stopped") < unchanged
