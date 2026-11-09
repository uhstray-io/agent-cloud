"""retire-tududi.yml: the one-way tududi teardown keeps its order, its gates and its check-mode contract.

Spec: plan/development/openspec/changes/deploy-plane-work-management/specs/platform/plane-work-management/
("tududi retires in a fixed order"): the teardown refuses, before any backup or destructive step,
unless its confirmation equals the VM id; the archive is taken from stopped volumes; nothing is
removed before the archive's checksum has been read back from site-config.

Every test reads the playbook as YAML. Ordering is asserted on the flattened execution order of
every task, so a moved task fails the test whatever its name.
"""

import json
from pathlib import Path

import playbook_yaml
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/retire-tududi.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
AGENTS = REPO / "AGENTS.md"

GUARD = "refuse-internal-extra-vars.yml"
O11Y_PREFLIGHT = "preflight-o11y-restore-target-inputs.yml"
DESTROY = "destroy-vm.yml"
CAP = "52428800"  # 50 MiB, GitHub's warning threshold for a file (docs.github.com, About large files)
CREDENTIAL_READS = {"Log in to OpenBao (AppRole)", "Read the site-config deploy key"}
READ_ONLY = CREDENTIAL_READS | {
    "Resolve the volumes compose created for the tududi project (read only)",
    "Resolve each volume's mountpoint (read only)",
    "Measure each volume on disk, in bytes (read only)",
}
WRITE_MODULES = ("ansible.builtin.tempfile", "ansible.builtin.fetch", "ansible.builtin.copy", "ansible.builtin.file")

PLAYS = playbook_yaml.load(PLAYBOOK)


def _flatten(tasks, guards=()):
    """Every task in execution order, with the `when` text of every enclosing block, rescue and
    always included. A block's rescue and always inherit its guard, as Ansible runs them."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        own = guards + ((json.dumps(task["when"]),) if "when" in task else ())
        if any(key in task for key in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from _flatten(task.get(key), own)
        else:
            yield task, own


def _steps():
    for index, play in enumerate(PLAYS):
        if "ansible.builtin.import_playbook" in play:
            yield index, play, None, ()
            continue
        for task, guards in _flatten(play.get("tasks")):
            yield index, play, task, guards


STEPS = list(_steps())


def _find(pred, what):
    hits = [i for i, (_, _, task, _) in enumerate(STEPS) if task is not None and pred(task)]
    assert hits, f"no task matches: {what}"
    return hits[0]


def _module(task):
    return next(k for k in task if k.startswith(("ansible.builtin.", "ansible.")))


def _argv(task):
    body = task.get("ansible.builtin.command") or {}
    return body.get("argv") if isinstance(body, dict) else None


def _asserts(task):
    return task.get("ansible.builtin.assert", {}).get("that", []) if "ansible.builtin.assert" in task else []


def _text(task):
    return json.dumps(task, sort_keys=True)


def test_the_play_order_is_fixed():
    kinds = [
        ("import", play["ansible.builtin.import_playbook"])
        if "ansible.builtin.import_playbook" in play
        else ("play", play["hosts"])
        for play in PLAYS
    ]
    assert kinds == [
        ("import", GUARD),
        ("play", "localhost"),
        ("import", O11Y_PREFLIGHT),
        ("play", "tududi_svc"),
        ("play", "localhost"),
        ("play", "tududi_svc"),
        ("play", "localhost"),
        ("import", DESTROY),
    ]


def test_the_extra_var_guard_is_the_first_entry():
    assert PLAYS[0]["ansible.builtin.import_playbook"] == GUARD
    assert PLAYS[0].get("tags") == ["always"]


# ── preflight gate ──────────────────────────────────────────────────────────


def _confirm_task():
    return next(t for t in PLAYS[1]["tasks"] if any("confirm_destroy" in e for e in _asserts(t)))


def test_the_confirmation_must_equal_the_declared_vmid():
    assert _asserts(_confirm_task()) == ["(confirm_destroy | default('') | string) == (_rt_vmid | string)"]
    assert PLAYS[1]["vars"]["_rt_vmid"] == "{{ _rt_decl.vm_vmid | default('') }}"
    assert PLAYS[1]["vars"]["_rt_decl"] == "{{ hostvars[_rt_host] | default({}) }}"


def test_the_preflight_refuses_a_launch_that_redirects_the_destroy():
    first = PLAYS[1]["tasks"][0]
    expr = _asserts(first)[0]
    for name in ("target_service", "target_host", "target_vmid", "target_node"):
        assert f"'{name}'" in expr, name
    assert "extra_var_names" in expr


def test_nothing_before_the_gate_changes_state():
    gate = STEPS.index(next(s for s in STEPS if s[2] is _confirm_task()))
    before = STEPS[:gate]
    assert before, "the gate is the first step after the guard"
    for play_index, _, task, _ in before:
        if task is None:
            continue
        assert play_index == 1, task.get("name")
        assert _module(task) in ("ansible.builtin.assert", "ansible.builtin.set_fact"), task.get("name")


def test_no_host_play_or_destroy_runs_before_the_gate():
    gate = STEPS.index(next(s for s in STEPS if s[2] is _confirm_task()))
    first_later_play = min(i for i, (play_index, _, _, _) in enumerate(STEPS) if play_index >= 2)
    assert gate < first_later_play


# ── order of the destructive steps ──────────────────────────────────────────


def test_the_container_is_stopped_before_its_volumes_are_read():
    stop = _find(lambda t: (_argv(t) or [])[:2] == ["podman", "stop"], "podman stop")
    volumes = _find(lambda t: (_argv(t) or [])[:3] == ["podman", "volume", "ls"], "podman volume ls")
    assert stop < volumes


def test_nothing_is_removed_before_the_archive_is_read_back_from_site_config():
    stop = _find(lambda t: (_argv(t) or [])[:2] == ["podman", "stop"], "podman stop")
    export = _find(lambda t: (_argv(t) or [])[:3] == ["podman", "volume", "export"], "podman volume export")
    host_fingerprint = _find(lambda t: t.get("register") == "_rt_archive_stat", "archive fingerprint on the VM")
    readback = _find(lambda t: "_rt_remote_stat.stat.checksum" in _text(t), "remote checksum gate")
    wipe = _find(lambda t: t.get("ansible.builtin.include_tasks") == "tasks/clean-service.yml", "clean-service include")
    wiped_fact = _find(lambda t: "_rt_wiped" in _text(t) and "ansible.builtin.set_fact" in t, "wipe recorded")
    wipe_gate = _find(lambda t: "_rt_wiped" in _text(t) and "ansible.builtin.assert" in t, "wipe gate on localhost")
    destroy = next(
        i
        for i, (_, play, task, _) in enumerate(STEPS)
        if task is None and play.get("ansible.builtin.import_playbook") == DESTROY
    )
    assert stop < export < host_fingerprint < readback < wipe < wiped_fact < wipe_gate < destroy


def test_the_read_back_gate_lives_in_the_delivery_play_and_the_wipe_in_the_host_play():
    readback = _find(lambda t: "_rt_remote_stat.stat.checksum" in _text(t), "remote checksum gate")
    wipe = _find(lambda t: t.get("ansible.builtin.include_tasks") == "tasks/clean-service.yml", "clean-service include")
    assert STEPS[readback][0] == 4 and PLAYS[4]["hosts"] == "localhost"
    assert STEPS[wipe][0] == 5 and PLAYS[5]["hosts"] == "tududi_svc"


def test_the_archive_proof_gate_refuses_when_the_archive_step_did_not_complete():
    gate = next(t for t in PLAYS[4]["tasks"] if "_rt_sha is defined" in json.dumps(t))
    assert "_rt_archive is defined" in json.dumps(gate)


# ── volumes and size ────────────────────────────────────────────────────────


def test_only_the_two_tududi_volumes_enter_the_archive():
    ls = next(t for _, _, t, _ in STEPS if t is not None and (_argv(t) or [])[:3] == ["podman", "volume", "ls"])
    assert "label=com.docker.compose.project={{ _rt_project }}" in _argv(ls)
    assert PLAYS[3]["vars"]["_rt_project"] == "tududi"
    exact = [
        t
        for _, _, t, _ in STEPS
        if t is not None and any("select('match', '^(.+_)?tududi-(db|uploads)$')" in e for e in _asserts(t))
    ]
    assert exact, "no assert names the two volumes"
    assert any("== 2" in e for e in _asserts(exact[0]))


def test_the_archive_is_capped_at_fifty_mebibytes_and_refused_when_empty():
    capped = [t for _, _, t, _ in STEPS if t is not None and CAP in _text(t) and "ansible.builtin.assert" in t]
    assert len(capped) == 2, "the du total and the archive size are both capped"
    du_gate = next(t for t in capped if "_rt_du" in _text(t))
    expressions = _asserts(du_gate)
    assert any("> 0" in e for e in expressions)
    assert any(f"<= {CAP}" in e for e in expressions)


def test_the_size_is_measured_before_any_export():
    du = _find(lambda t: (_argv(t) or [None])[0] == "du", "du -sb")
    export = _find(lambda t: (_argv(t) or [])[:3] == ["podman", "volume", "export"], "podman volume export")
    cap = _find(lambda t: CAP in _text(t) and "ansible.builtin.assert" in t and "_rt_du" in _text(t), "du cap")
    assert du < cap < export


def test_the_archive_host_play_is_one_podman_host():
    first = PLAYS[3]["tasks"][0]
    assert _asserts(first) == ["ansible_play_hosts_all | length == 1", "_rt_engine == 'podman'"]


# ── check mode and credentials ──────────────────────────────────────────────


def test_only_reads_run_under_check_mode():
    for _, _, task, _ in STEPS:
        if task is not None and task.get("check_mode") is False:
            assert task["name"] in READ_ONLY, task["name"]


def test_each_write_is_skipped_under_check_mode():
    for _, _, task, guards in STEPS:
        if task is None:
            continue
        argv = _argv(task) or []
        argv_text = str(argv)
        is_write = (
            (
                _module(task) in WRITE_MODULES
                and not (
                    _module(task) == "ansible.builtin.file"
                    and task.get("ansible.builtin.file", {}).get("state") != "absent"
                )
            )
            or argv[:2] in (["podman", "stop"], ["podman", "start"])
            or argv[:3] == ["podman", "volume", "export"]
            or "-czf" in argv_text
        )
        if not is_write:
            continue
        guard_text = " ".join(guards) + json.dumps(task.get("when", ""))
        # An explicit check-mode guard, or a `defined` guard on a scratch path that only a non-check run
        # sets. A `default(false)` flag is not enough: a rescue restart reads one, and a dry run must
        # not restart anything on its own account.
        assert "ansible_check_mode" in guard_text or "is defined" in guard_text, task["name"]


def test_no_log_marks_only_the_credential_reads():
    marked = {task["name"] for _, _, task, _ in STEPS if task is not None and task.get("no_log") is True}
    assert marked == CREDENTIAL_READS


# ── restore on failure, destroy and wipe wiring ─────────────────────────────


def _rescue_argvs(task):
    return [_argv(t) for t in task.get("rescue", []) if isinstance(t, dict)]


def test_a_failure_before_the_wipe_restarts_tududi_when_this_run_stopped_it():
    archive = next(t for t in PLAYS[3]["tasks"] if "block" in t)
    assert ["podman", "start", "{{ _rt_container }}"] in _rescue_argvs(archive)
    deliver = next(t for t in PLAYS[4]["tasks"] if "block" in t)
    assert ["podman", "start", "{{ _rt_container }}"] in _rescue_argvs(deliver)


def test_the_destroy_is_pinned_to_the_tududi_service_and_skips_the_nested_guard():
    imp = PLAYS[7]
    assert imp["ansible.builtin.import_playbook"] == DESTROY
    assert imp["vars"]["target_service"] == "tududi"
    assert imp["vars"]["_extra_var_guard_nested"] is True
    assert imp["vars"]["target_host"].startswith("{{ groups['tududi_svc']")


def test_the_wipe_confirms_only_its_own_host_list():
    wipe = next(t for t in PLAYS[5]["tasks"] if t.get("ansible.builtin.include_tasks") == "tasks/clean-service.yml")
    assert wipe["vars"]["confirm_reset"] == "{{ ansible_play_hosts_all | sort | join(',') }}"


def test_the_always_wipes_remove_scratch_only_when_it_exists():
    deliver = next(t for t in PLAYS[4]["tasks"] if "always" in t)
    for task in deliver["always"]:
        assert "is defined" in task.get("when", ""), task["name"]


# ── Semaphore template and operator docs ────────────────────────────────────


def _template():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    return next(t for t in templates if t["name"] == "Retire tududi")


def test_the_template_runs_the_playbook_in_its_own_environment_with_a_required_confirm():
    template = _template()
    assert template["playbook"] == "platform/playbooks/retire-tududi.yml"
    assert template["isolated_environment"] == "Retire tududi inputs"
    assert template["dev_variant"] is True
    (survey,) = template["survey_vars"]
    assert survey["name"] == "confirm_destroy"
    assert survey["required"] is True
    assert "default_value" not in survey


def test_no_other_template_shares_the_isolated_environment():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    holders = [t["name"] for t in templates if t.get("isolated_environment") == "Retire tududi inputs"]
    assert holders == ["Retire tududi"]


def test_the_agents_table_lists_the_playbook():
    rows = [line for line in AGENTS.read_text().splitlines() if line.startswith("| Retire tududi |")]
    assert len(rows) == 1
    assert "`retire-tududi.yml`" in rows[0]
