"""Service executors record their workflow step result (D10 review, change
service-deployment-workflow task 7.1): fw-harden (Apply Firewall), edge-route (Manage Caddy
Sites) and oidc-config (Deploy Authentik).

Apply Firewall's result comes from its own localhost play, which folds every target host's
verdict into ONE result. That play is run here for real through ansible-playbook, with the
per-host verdicts the main play would have set supplied as inventory host vars, so the folding
(a failed or verdict-less host is a failure, every host skipped is a skip, an empty group is
refused) is proven without a UFW host.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
EMIT = PLAYBOOKS / "tasks/emit-step-result.yml"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def _plays(name):
    return yaml.safe_load((PLAYBOOKS / name).read_text())


def _emits(tasks):
    for t in tasks or []:
        for key in ("block", "rescue", "always"):
            yield from _emits(t.get(key))
        if str(t.get("ansible.builtin.include_tasks", "")).endswith("emit-step-result.yml"):
            yield t


def _record_play():
    (play,) = [p for p in _plays("apply-firewall.yml") if p.get("hosts") == "localhost"]
    return play


def _verdict(errors=(), skipped=False, active=True):
    return {"skipped": skipped, "active": active, "allows": [] if skipped else ["agent-cloud:in:22/tcp:x"],
            "errors": list(errors)}


def _run_record(tmp_path, verdicts: dict, *extra, target="fw_svc"):
    play = json.loads(json.dumps(_record_play()))
    for task in _emits(play["tasks"]):
        task["ansible.builtin.include_tasks"] = str(EMIT)
    (tmp_path / "play.yml").write_text(json.dumps([play]))
    hosts = {h: ({"_fw_verdict": v} if v is not None else {}) for h, v in verdicts.items()}
    inventory = {"all": {"vars": {"service_name": "demo"}, "children": {"fw_svc": {"hosts": hosts}}}}
    (tmp_path / "inv.yml").write_text(json.dumps(inventory))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml"),
         "-e", f"target_service={target}", *extra],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False,
    )
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, f"expected one step result, got {found!r}\n{proc.stdout[-3000:]}"
    return proc, found[0]


@needs_ansible
@pytest.mark.parametrize("check", [False, True])
def test_firewall_result_fails_when_any_host_fails(tmp_path, check):
    proc, res = _run_record(tmp_path, {"a": _verdict(), "b": _verdict(["ufw is not active"], active=False)},
                            *(["--check"] if check else []))
    assert proc.returncode != 0
    assert (res["step"], res["status"], res["service"]) == ("fw-harden", "fail", "demo")
    assert res["error"] == "b: ufw is not active"
    assert res["check_mode"] is check
    assert res["evidence"]["ufw_active"] == {"a": True, "b": False}


@needs_ansible
def test_firewall_host_without_a_verdict_counts_as_failed(tmp_path):
    proc, res = _run_record(tmp_path, {"a": _verdict(), "b": None})
    assert proc.returncode != 0 and res["status"] == "fail"
    assert "b: no firewall verdict" in res["error"]


@needs_ansible
def test_firewall_every_host_passing_is_a_pass(tmp_path):
    proc, res = _run_record(tmp_path, {"a": _verdict(), "b": _verdict()})
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert res["status"] == "pass" and set(res["evidence"]) == {"ufw_active", "allows"}


@needs_ansible
def test_firewall_every_host_not_applicable_is_a_skip(tmp_path):
    proc, res = _run_record(tmp_path, {"a": _verdict(skipped=True, active=False)})
    assert proc.returncode == 0 and res["status"] == "skip"


@needs_ansible
def test_firewall_empty_group_is_a_recorded_failure(tmp_path):
    proc, res = _run_record(tmp_path, {"a": _verdict()}, target="no_such_svc")
    assert proc.returncode != 0 and res["status"] == "fail"
    assert "matches no hosts" in res["error"]


def test_firewall_main_play_sets_a_verdict_on_every_exit_path():
    (main,) = [p for p in _plays("apply-firewall.yml") if p.get("hosts") != "localhost"]
    names = [t.get("name", "") for t in main["tasks"]]
    # each end_host is preceded by the verdict it leaves behind, and the last task decides it
    for i, task in enumerate(main["tasks"]):
        if task.get("ansible.builtin.meta") == "end_host":
            assert "_fw_verdict" in (main["tasks"][i - 1].get("ansible.builtin.set_fact") or {}), names[i]
    assert "_fw_verdict" in main["tasks"][-1]["ansible.builtin.set_fact"]
    # the record play is the last play and its emit the last-but-one task (before the fail)
    assert _plays("apply-firewall.yml")[-1] is not main


def _run_slice(tmp_path, playbook, first_task, hostvars: dict, *extra, play_vars=None, prepend=()):
    """Run a playbook's tasks from `first_task` to the end of that play, on local hosts."""
    for play in _plays(playbook):
        names = [t.get("name") for t in play.get("tasks") or []]
        if first_task in names:
            tasks = json.loads(json.dumps(play["tasks"][names.index(first_task):]))
            source_vars = {k: v for k, v in (play.get("vars") or {}).items() if k.startswith("_edge")}
            break
    else:
        raise AssertionError(f"{first_task!r} not in {playbook}")
    for task in _emits(tasks):
        task["ansible.builtin.include_tasks"] = str(EMIT)
    tasks = list(prepend) + tasks
    play = {"name": "slice", "hosts": "svc", "gather_facts": False,
            "vars": {**source_vars, **(play_vars or {})}, "tasks": tasks}
    (tmp_path / "play.yml").write_text(json.dumps([play]))
    hosts = {h: {"ansible_connection": "local", "ansible_python_interpreter": "auto_silent", **v}
             for h, v in hostvars.items()}
    inv = {"all": {"vars": {"service_name": "demo"}, "children": {"svc": {"hosts": hosts}}}}
    (tmp_path / "inv.yml").write_text(json.dumps(inv))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml"), *extra],
                          cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False)
    return proc, step_results.results_in(proc.stdout.splitlines())


# ── edge-route (Manage Caddy Sites) ─────────────────────────────────────────

EDGE_FIRST = "Edge route: does the probe host resolve?"


@needs_ansible
@pytest.mark.parametrize("check", [False, True])
def test_edge_route_unresolvable_probe_host_is_a_recorded_failure(tmp_path, check):
    proc, found = _run_slice(tmp_path, "manage-caddy-sites.yml", EDGE_FIRST,
                             {"caddy": {"caddy_probe_host": "no-such-host.invalid"}}, *(["--check"] if check else []))
    # Recorded, not fatal: the importer decides. Rollback Inference Route fails on the
    # _edge_group_errors fact this sets (test_rollback_inference_route.py proves that side).
    assert proc.returncode == 0, proc.stdout[-3000:]
    (res,) = found
    assert (res["step"], res["status"], res["check_mode"]) == ("edge-route", "fail", check)
    assert res["evidence"] == {"resolves": {"caddy": False}, "route_status": {"caddy": None}}
    assert "caddy: no-such-host.invalid does not resolve" in res["error"]


@needs_ansible
def test_edge_route_without_a_probe_host_cannot_pass(tmp_path):
    proc, (res,) = _run_slice(tmp_path, "manage-caddy-sites.yml", EDGE_FIRST, {"caddy": {}})
    assert proc.returncode == 0 and res["status"] == "fail"
    assert "caddy_probe_host is not declared" in res["error"]


@needs_ansible
def test_edge_route_no_answer_is_a_failure_on_any_host(tmp_path):
    # localhost resolves; nothing listens on 443 here, so the route gives no HTTP answer.
    proc, (res,) = _run_slice(tmp_path, "manage-caddy-sites.yml", EDGE_FIRST,
                              {"a": {"caddy_probe_host": "localhost"}, "b": {"caddy_probe_host": "localhost"}})
    assert proc.returncode == 0 and res["status"] == "fail"
    assert res["evidence"]["resolves"] == {"a": True, "b": True}
    assert "a: https://localhost/ answered" in res["error"] and "b: https://localhost/ answered" in res["error"]


@needs_ansible
def test_edge_route_host_failing_before_its_verdict_counts_as_failed(tmp_path):
    die = {"name": "b dies first", "ansible.builtin.fail": {"msg": "x"}, "when": "inventory_hostname == 'b'"}
    proc, (res,) = _run_slice(tmp_path, "manage-caddy-sites.yml", EDGE_FIRST,
                              {"a": {"caddy_probe_host": "no-such-host.invalid"}, "b": {}}, prepend=[die])
    assert res["status"] == "fail" and "b: no edge-route verdict" in res["error"]


# ── oidc-config (Deploy Authentik) ──────────────────────────────────────────

OIDC_FIRST = "OIDC configuration: decide this host's verdict"


def _oidc_host(users_rc=0, oidc_rc=0, do_verify=True, apps=("semaphore",)):
    return {"_users_verify": {"rc": users_rc}, "_oidc_verify": {"rc": oidc_rc},
            "_do_oidc_verify": do_verify, "_verify_apps": list(apps)}


@needs_ansible
@pytest.mark.parametrize("check", [False, True])
def test_oidc_failed_redirect_check_records_fail_and_stops(tmp_path, check):
    proc, found = _run_slice(tmp_path, "deploy-authentik.yml", OIDC_FIRST,
                             {"ak": _oidc_host(oidc_rc=1)}, *(["--check"] if check else []))
    assert proc.returncode != 0
    (res,) = found
    assert (res["step"], res["status"], res["check_mode"]) == ("oidc-config", "fail", check)
    assert res["evidence"] == {"blueprint_status": {"ak": "successful"}, "redirect_verified": {"ak": False}}
    assert "ak: live OIDC redirect_uris do not match intent" in res["error"]


@needs_ansible
def test_oidc_a_failing_second_host_fails_the_group(tmp_path):
    proc, (res,) = _run_slice(tmp_path, "deploy-authentik.yml", OIDC_FIRST,
                              {"a": _oidc_host(), "b": _oidc_host(users_rc=2)})
    assert proc.returncode != 0 and res["status"] == "fail"
    assert res["error"].startswith("b: custom blueprints not all applied")
    assert res["evidence"]["blueprint_status"] == {"a": "successful", "b": "failed"}


@needs_ansible
def test_oidc_passing_verify_records_nothing_until_the_last_play(tmp_path):
    # The pass is recorded at the end of Phase 4, not here.
    proc, found = _run_slice(tmp_path, "deploy-authentik.yml", OIDC_FIRST, {"ak": _oidc_host()})
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert found == []


def test_oidc_result_is_recorded_last_and_verify_failures_reach_it():
    plays = _plays("deploy-authentik.yml")
    last = plays[-1]["tasks"][-1]
    assert str(last.get("ansible.builtin.include_tasks", "")).endswith("emit-step-result.yml")
    assert last["vars"]["step_result_step"] == "oidc-config"
    verify = {t["name"]: t for p in plays for t in p.get("tasks") or [] if "name" in t}
    # a hard failed_when on either live check would stop the play before the result is recorded
    for name in ("Promotion verify (prod): live OIDC redirect_uris match intent",
                 "Blueprint verify: custom blueprints applied, declared accounts present/absent"):
        assert verify[name]["failed_when"] is False, name


@needs_ansible
@pytest.mark.parametrize("verified,status", [(True, "pass"), (False, "skip")])
def test_oidc_final_result_is_skip_when_the_redirect_check_did_not_run(tmp_path, verified, status):
    host = {"_oidc_blueprint_status": "successful",
            "_oidc_redirect_verified": True if verified else "not-checked"}
    proc, (res,) = _run_slice(tmp_path, "deploy-authentik.yml", "Record the step result", {"ak": host})
    assert proc.returncode == 0 and res["status"] == status


def test_the_rollback_fails_on_the_edge_verdict_manage_caddy_sites_records():
    # The edge check is record-only in manage-caddy-sites.yml; the importer must consume it.
    plays = yaml.safe_load((PLAYBOOKS / "rollback-inference-route.yml").read_text())
    i = next(n for n, p in enumerate(plays) if p.get("ansible.builtin.import_playbook") == "manage-caddy-sites.yml")
    gate = plays[i + 1]["tasks"][0]
    assert "_edge_group_errors" in gate["ansible.builtin.assert"]["that"]
    edge = next(p for p in _plays("manage-caddy-sites.yml") if p["hosts"] != "localhost")["tasks"]
    assert any("_edge_group_errors" in (t.get("ansible.builtin.set_fact") or {}) for t in edge)
