"""Waves 1 and 2 of the `service-deploy` step result (change service-deployment-workflow, task 7.1).

The registry step `service-deploy` is judged on two criteria: the deploy verify phase passes,
and a second run reports no change. Both need the deploy playbooks to say so, so this holds
every playbook in WAVE to the same shape:

- it records the result through tasks/emit-service-deploy-result.yml as the LAST task of its
  verify play, so a verify failure stops the play before anything is recorded;
- its deploy.sh recreates containers only when an input or image changed
  (common.sh compose_up_if_changed prints DEPLOY_CHANGED), never unconditionally, and the
  playbook reads that line into changed_when, so an unchanged re-run reports no change.

The shared task is run for real through ansible-playbook, as test_service_executor_step_results
does, so the status and evidence it records are read from the CUSTOM STATS output a collector
reads, not inferred from the YAML.
"""

import importlib.util
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
SERVICES = REPO / "platform/services"
SHARED = PLAYBOOKS / "tasks/emit-service-deploy-result.yml"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

# playbook -> the verify play that records the result (the play's name starts with this).
WAVE = {
    "deploy-agentgateway.yml": "Phase 3: Verify agentgateway",
    "deploy-authentik.yml": "Phase 3: Verify authentik",
    "deploy-github-runner.yml": "Deploy GitHub Actions self-hosted runner",
    "deploy-honcho.yml": "Phase 3: Verify honcho",
    "deploy-n8n.yml": "Phase 3: Verify n8n",
    "deploy-netbox.yml": "Phase 5: Verify deployment",
    "deploy-o11y.yml": "Phase 3: Verify o11y",
    "deploy-openhands.yml": "Phase 3: Verify OpenHands",
    "deploy-postiz.yml": "Phase 3: Verify postiz",
    "deploy-tududi.yml": "Phase 3: Verify tududi",
}
# The services whose deploy.sh this change made change-aware, plus the two that already were.
RECREATE_AWARE = {
    "tududi": "deploy-tududi.yml",
    "honcho": "deploy-honcho.yml",
    "openhands": "deploy-openhands.yml",
    "n8n": "deploy-n8n.yml",
    "netbox": "deploy-netbox.yml",
    "postiz": "deploy-postiz.yml",
}
# NetBox keeps its own compose wrapper and library, so its deploy.sh decides through its own call.
CHANGE_CALL = {"netbox": "netbox_recreate_reason"}
ALREADY_AWARE = ("authentik", "o11y")


def _code(path: Path) -> str:
    """The script without its comment lines, so an explanation naming a flag is not a use of it."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("playbook,verify_play", sorted(WAVE.items()))
def test_wave_playbook_records_service_deploy_last_in_its_verify_play(playbook, verify_play):
    plays = [p for p in playbook_yaml.plays(PLAYBOOKS / playbook) if p.get("name", "").startswith(verify_play)]
    assert len(plays) == 1, f"{playbook}: expected exactly one play named {verify_play!r}"
    tasks = plays[0]["tasks"]
    last = tasks[-1]
    assert str(last.get("ansible.builtin.include_tasks", "")) == "tasks/emit-service-deploy-result.yml", (
        f"{playbook}: the last task of {verify_play!r} is {last.get('name')!r}, not the service-deploy record"
    )
    assert "service_deploy_result_var" in last.get("vars", {}), playbook
    # Exactly one record per playbook, so a re-run is judged once.
    includes = [
        t for t in playbook_yaml.tasks(playbook_yaml.plays(PLAYBOOKS / playbook))
        if str(t.get("ansible.builtin.include_tasks", "")).endswith("emit-service-deploy-result.yml")
    ]
    assert len(includes) == 1, playbook


@pytest.mark.parametrize("service", sorted(RECREATE_AWARE))
def test_deploy_sh_recreates_only_on_a_change(service):
    code = _code(SERVICES / service / "deployment/deploy.sh")
    assert CHANGE_CALL.get(service, "compose_up_if_changed") in code, service
    assert "--force-recreate" not in code, f"{service}: an unconditional recreate makes every re-run a change"


@pytest.mark.parametrize("service", ALREADY_AWARE)
def test_the_already_change_aware_deploys_stay_so(service):
    assert "compose_up_if_changed" in _code(SERVICES / service / "deployment/deploy.sh"), service


def test_agentgateway_reports_whether_it_recreated():
    assert "deploy-result:" in _code(SERVICES / "agentgateway/deployment/deploy.sh")


@pytest.mark.parametrize("service,playbook", sorted(RECREATE_AWARE.items()))
def test_deploy_playbook_reads_the_change_line_and_carries_the_lever(service, playbook):
    plays = playbook_yaml.plays(PLAYBOOKS / playbook)
    runs = [
        t for t in playbook_yaml.tasks(plays)
        # `deploy.sh` but not `post-deploy.sh` (deploy-netbox.yml runs both)
        if re.search(r"(?<![\w-])deploy\.sh", str(t.get("ansible.builtin.shell", "")))
    ]
    assert len(runs) == 1, playbook
    task = runs[0]
    assert task["register"] == "_deploy"
    assert task["changed_when"] == "'DEPLOY_CHANGED=true' in _deploy.stdout_lines", playbook
    assert "deploy_force_recreate" in task["environment"]["FORCE_RECREATE"], playbook
    template = next(
        t for t in yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]
        if t["playbook"] == f"platform/playbooks/{playbook}"
    )
    assert "deploy_force_recreate" in {v["name"] for v in template["survey_vars"]}, service


@pytest.mark.parametrize("service", ["tududi", "n8n", "postiz"])
def test_every_bind_mounted_file_is_a_digest_input(service):
    # A bind-mounted input left out of the digest changes without the container being
    # recreated. The compose files themselves are always in the digest; the files they mount
    # are not, unless deploy.sh names them.
    mounts = set()
    for compose in (SERVICES / service / "deployment").glob("compose*.yml"):
        for svc in (yaml.safe_load(compose.read_text()).get("services") or {}).values():
            mounts |= {str(v).split(":")[0] for v in (svc or {}).get("volumes", []) if str(v).startswith("./")}
    assert mounts, f"{service}: no compose file bind-mounts a file any more: revisit the digest inputs"
    code = _code(SERVICES / service / "deployment/deploy.sh")
    # Only the helper's own argument lists count: deploy.sh also names these files elsewhere
    # (a presence check before the start).
    calls = " ".join(line for line in code.splitlines() if "compose_up_if_changed" in line)
    for mount in sorted(mounts):
        assert mount.removeprefix("./") in calls, f"{service}: {mount} is not passed to compose_up_if_changed"


def test_n8n_keeps_its_ordered_start_inside_the_change_gate():
    # The worker must not boot while the app migrates, and a recreate must not run unless the
    # helper found a change: the ordered start is the helper's hook, never a bare call.
    code = _code(SERVICES / "n8n/deployment/deploy.sh")
    hook = code[code.index("compose_staged_up()"):code.index("main()")]
    stages = ("n8n-postgres n8n-redis n8n", "wait_for_healthy workflow-n8n 300", "up -d n8n-worker")
    assert hook.index(stages[0]) < hook.index(stages[1]) < hook.index(stages[2])
    # The project name is the directory's basename, shared with other services' compose files: a
    # project-wide orphan sweep would remove their containers.
    assert "--remove-orphans" not in hook
    main = code[code.index("main()"):]
    assert "up -d" not in main, "a bare `compose up` outside the hook recreates on every run"
    assert "compose_up_if_changed" in main


def test_postiz_has_no_unconditional_recreate_of_the_app():
    code = _code(SERVICES / "postiz/deployment/deploy.sh")
    assert "--force-recreate" not in code and "up -d" not in code


def _task(playbook: str, name: str) -> dict:
    """The named top-level task of a playbook's plays, as the playbook defines it."""
    (task,) = [t for t in playbook_yaml.tasks(playbook_yaml.plays(PLAYBOOKS / playbook)) if t.get("name") == name]
    return json.loads(json.dumps(task))


GATHER = "Gather this host's service-deploy result"


def _run_shared(tmp_path, hosts: dict, *extra, var="_deploy", before=(), fail_hosts=(), check=False):
    """Run the shared task over a play of local hosts. `hosts` maps a host to its host vars (the
    per-host deploy result lives in them). `before` are tasks run first (a playbook's own gather
    task, rendered as the playbook writes it); `fail_hosts` fail before the include, as a host
    that failed an earlier phase does."""
    include = {
        "name": "Record the step result",
        "ansible.builtin.include_tasks": str(SHARED),
        "vars": {"service_deploy_result_var": var},
    }
    tasks = [*before]
    if fail_hosts:
        tasks.append({"ansible.builtin.fail": {"msg": "earlier phase failed"},
                      "when": f"inventory_hostname in {list(fail_hosts)!r} and not ansible_check_mode"})
    tasks.append(include)
    (tmp_path / "play.yml").write_text(json.dumps([{"hosts": "svc", "gather_facts": False, "tasks": tasks}]))
    base = {"ansible_connection": "local", "service_name": "demo"}
    inventory = {"all": {"children": {"svc": {"hosts": {h: {**base, **v} for h, v in hosts.items()}}}}}
    (tmp_path / "inv.yml").write_text(json.dumps(inventory))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml"),
         *(["--check"] if check else []), *extra],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False,
    )
    return proc, step_results.results_in(proc.stdout.splitlines())


def _one(found):
    assert len(found) == 1, found
    (res,) = found
    assert (res["step"], res["service"]) == ("service-deploy", "demo")
    assert type(res["evidence"]["changed_on_rerun"]) is bool and type(res["evidence"]["verified"]) is bool
    return res


CHANGED, SAME, SKIPPED = {"changed": True}, {"changed": False}, {"skipped": True, "changed": False}


@needs_ansible
@pytest.mark.parametrize(
    "results,status,verified,changed",
    [
        ([CHANGED], "pass", True, True),
        ([SAME], "pass", True, False),
        ([SAME, SAME], "pass", True, False),
        ([SAME, CHANGED], "pass", True, True),  # the second host alone changed
        ([CHANGED, SAME], "pass", True, True),
        ([SKIPPED, SKIPPED], "skip", True, False),
        ([SKIPPED, SAME], "pass", True, False),
        ([SAME, {"changed": False, "verified": False}], "pass", False, False),
        ([None, None], "skip", True, False),  # nothing registered: the run was limited to other tags
    ],
)
def test_shared_task_aggregates_every_host_of_the_play(tmp_path, results, status, verified, changed):
    hosts = {f"h{i}": ({"_deploy": r} if r is not None else {}) for i, r in enumerate(results, 1)}
    proc, found = _run_shared(tmp_path, hosts)
    assert proc.returncode == 0, proc.stdout[-3000:]
    res = _one(found)
    assert (res["status"], res["error"]) == (status, None)
    assert res["evidence"] == {"verified": verified, "changed_on_rerun": changed}


@needs_ansible
def test_shared_task_fails_when_a_host_of_the_play_failed(tmp_path):
    proc, found = _run_shared(tmp_path, {"h1": {"_deploy": SAME}, "h2": {"_deploy": CHANGED}}, fail_hosts=["h2"])
    assert proc.returncode != 0
    res = _one(found)
    assert res["status"] == "fail" and "h2" in res["error"] and "h1" not in res["error"]
    assert res["evidence"] == {"verified": False, "changed_on_rerun": False}


@needs_ansible
def test_shared_task_does_not_judge_a_missing_host_in_a_dry_run(tmp_path):
    # meta: end_host (the dry run's "not deployed yet, stop") removes a host from the play's
    # hosts exactly as a failure does; a dry run's deploy is skipped on every host anyway.
    proc, found = _run_shared(
        tmp_path, {"h1": {"_deploy": SKIPPED}, "h2": {"_deploy": SKIPPED}}, fail_hosts=["h2"], check=True
    )
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert _one(found)["status"] == "skip"


@needs_ansible
@pytest.mark.parametrize(
    "vars_,status,changed",
    [
        ({"_configured": {"changed": True}}, "pass", True),
        ({"_configured": {"changed": False}, "_runner_unit": {"changed": True}}, "pass", True),
        ({"_configured": {"changed": False}, "_runner_files": {"changed": True}}, "pass", True),
        ({"_configured": {"changed": False}, "_runner_unit": {"changed": False}}, "pass", False),
        ({"_configured": {"skipped": True, "changed": False}}, "skip", False),
        ({}, "skip", False),
    ],
)
def test_github_runner_composite_result_reads_the_tasks_it_names(tmp_path, vars_, status, changed):
    # Rendered exactly as deploy-github-runner.yml writes it: renaming a registered var it reads
    # silently reports "no change", so a rename must turn this red.
    gather = _task("deploy-github-runner.yml", GATHER)
    proc, found = _run_shared(tmp_path, {"h1": vars_}, var="_service_deploy_result", before=[gather])
    assert proc.returncode == 0, proc.stdout[-3000:]
    res = _one(found)
    assert (res["status"], res["evidence"]["changed_on_rerun"]) == (status, changed)
    include = _task("deploy-github-runner.yml", "Record the service-deploy step result")
    assert include["vars"] == {"service_deploy_result_var": "_service_deploy_result"}


@needs_ansible
@pytest.mark.parametrize("refused,verified", [(True, False), (False, True), (None, True)])
def test_agentgateway_result_is_unverified_when_an_unproven_429_was_accepted(tmp_path, refused, verified):
    gather = _task("deploy-agentgateway.yml", GATHER)
    host = {"_deploy": CHANGED, **({"_verify_refused": refused} if refused is not None else {})}
    proc, found = _run_shared(tmp_path, {"h1": host}, var="_service_deploy_result", before=[gather])
    assert proc.returncode == 0, proc.stdout[-3000:]
    res = _one(found)
    assert (res["status"], res["evidence"]) == ("pass", {"verified": verified, "changed_on_rerun": True})


@needs_ansible
@pytest.mark.parametrize(
    "vars_,status,changed",
    [
        ({"_deploy": CHANGED}, "pass", True),
        ({"_deploy": SAME}, "pass", False),
        ({"_deploy": SAME, "_n8n_owner_seed": {"changed": True}}, "pass", True),  # re-run that seeded the owner
        ({"_deploy": SAME, "_n8n_owner_seed": {"skipped": True, "changed": False}}, "pass", False),
        ({"_deploy": SKIPPED}, "skip", False),
        ({}, "skip", False),
    ],
)
def test_n8n_result_folds_the_owner_seed_into_the_deploy_result(tmp_path, vars_, status, changed):
    # Rendered exactly as deploy-n8n.yml writes it: the owner seed is the one post-deploy step
    # that changes state, and renaming a var it reads silently reports "no change".
    fold = _task("deploy-n8n.yml", "Fold the owner seed into the service-deploy result")
    proc, found = _run_shared(tmp_path, {"h1": vars_}, var="_n8n_service_deploy", before=[fold])
    assert proc.returncode == 0, proc.stdout[-3000:]
    res = _one(found)
    assert (res["status"], res["evidence"]["changed_on_rerun"]) == (status, changed)
    include = _task("deploy-n8n.yml", "Record the service-deploy step result")
    assert include["vars"] == {"service_deploy_result_var": "_n8n_service_deploy"}


@needs_ansible
def test_shared_task_evidence_keys_are_the_registrys():
    registry = yaml.safe_load((REPO / "platform/workflows/service-onboarding/registry.yml").read_text())["steps"]
    keys = next(s for s in registry if s["id"] == "service-deploy")["evidence_keys"]
    task = yaml.safe_load(SHARED.read_text())[-1]
    assert set(task["vars"]["step_result_evidence"]) == set(keys)
    assert task["vars"]["step_result_step"] == "service-deploy"


@needs_ansible
@pytest.mark.parametrize(
    "forge",
    forgeries.templated_forgeries("service_deploy_result_var", "_deploy", "_other"),
)
def test_shared_task_refuses_the_input_forged_as_an_extra_var(tmp_path, forge):
    proc, found = _run_shared(tmp_path, {"h1": {"_deploy": CHANGED, "_other": SAME}}, "-e", forge(tmp_path))
    assert proc.returncode != 0
    assert "Refusing to record a service-deploy result" in proc.stdout
    assert not found


def test_n8n_published_bind_address_is_rendered_into_the_hashed_env():
    # compose substitutes ${N8N_LISTEN} into the port publish. The playbook used to export it
    # into deploy.sh's environment, which the digest cannot see; rendering it into .env (one
    # source, hashed with the rest) makes a changed n8n_bind recreate the container.
    template = (SERVICES / "n8n/deployment/templates/n8n.env.j2").read_text()
    assert "N8N_LISTEN={{ n8n_bind | default('127.0.0.1') }}" in template
    (task,) = [
        t for t in playbook_yaml.tasks(playbook_yaml.plays(PLAYBOOKS / "deploy-n8n.yml"))
        if "deploy.sh" in str(t.get("ansible.builtin.shell", ""))
    ]
    assert "N8N_LISTEN" not in task["environment"], "a shell-exported value outranks .env and escapes the digest"
