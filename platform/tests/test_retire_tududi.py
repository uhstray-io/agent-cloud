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
import pytest
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
    "Read the container state before the stop (read only)",
    "Resolve the volumes compose created for the tududi project (read only)",
    "Resolve each volume's mountpoint (read only)",
    "Measure each volume on disk, in bytes (read only, inside the rootless user namespace)",
}
ONLY_COPY = "Merge it into site-config main before closing task 7.5; until then it is the only copy of the tududi data."
TRANSPORT_CHECK = "tasks/assert-bao-transport.yml"
STOP_WHEN = "_rt_state_before.stdout | trim == 'running' and not ansible_check_mode"
# Data-loss gates: each must assert exactly these expressions, with no `when` of its own. Its only guard is
# the dry-run block it sits in ("not ansible_check_mode"). A gate that a `when` can skip is no gate.
DATA_LOSS_GATES = {
    "Require the VM archive step to have completed (its fingerprint exists)": [
        "hostvars[_rt_host]._rt_sha is defined",
        "hostvars[_rt_host]._rt_archive is defined",
    ],
    "Require the fetched copy to match the VM's archive": [
        "_rt_ctrl_stat.stat.checksum == hostvars[_rt_host]._rt_sha",
    ],
    "Require this run to have pushed the archive (a no-change push cannot be read back)": [
        "_sc_staged.rc != 0",
    ],
    "Require the remote copy to match the VM's archive (the gate before any removal)": [
        "_rt_remote_stat.stat.exists",
        "_rt_remote_stat.stat.checksum == hostvars[_rt_host]._rt_sha",
    ],
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
        ("import", DESTROY),
    ]


def test_no_clean_service_include_anywhere():
    # clean-service's prod branch runs compose under become, and destroy-vm purges the VM and its
    # disks anyway. A teardown that includes it is a second path this playbook does not need.
    assert "clean-service" not in json.dumps(PLAYS)
    assert not [t for _, _, t, _ in STEPS if t is not None and "clean-service" in _text(t)]


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


REFUSED_LAUNCH_NAMES = (
    "target_service",
    "target_host",
    "target_vmid",
    "target_node",
    "vm_vmid",
    "vm_name",
    "vm_node",
    "container_engine",
    "service_name",
    "monorepo_deploy_path",
)


def test_the_preflight_refuses_every_launch_name_that_outranks_the_inventory():
    # Extra vars outrank hostvars: a launch-supplied vm_vmid would replace the declaration this gate
    # reads, so each name is refused by name, in the first assert of the preflight.
    first = PLAYS[1]["tasks"][0]
    expr = _asserts(first)[0]
    assert "extra_var_names" in expr
    for name in REFUSED_LAUNCH_NAMES:
        assert f"'{name}'" in expr, name


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
    destroy = next(
        i
        for i, (_, play, task, _) in enumerate(STEPS)
        if task is None and play.get("ansible.builtin.import_playbook") == DESTROY
    )
    assert stop < export < host_fingerprint < readback < destroy


def test_the_destroy_follows_the_read_back_gate_and_is_the_last_step():
    readback = _find(lambda t: "_rt_remote_stat.stat.checksum" in _text(t), "remote checksum gate")
    assert STEPS[readback][0] == 4 and PLAYS[4]["hosts"] == "localhost"
    assert PLAYS[5]["ansible.builtin.import_playbook"] == DESTROY
    after = STEPS[readback + 1 :]
    # After the gate come only the deliver play's own failure and scratch cleanup, then the destroy.
    assert {index for index, _, _, _ in after} <= {4, 5}, "nothing outside the deliver play runs before the destroy"
    assert after[-1][0] == 5 and after[-1][2] is None, "the destroy import is the last step"


@pytest.mark.parametrize("name", sorted(DATA_LOSS_GATES))
def test_a_data_loss_gate_asserts_exactly_its_expressions_and_has_no_when_of_its_own(name):
    matches = [(task, guards) for _, _, task, guards in STEPS if task is not None and task.get("name") == name]
    assert len(matches) == 1, name
    task, guards = matches[0]
    assert set(task) == {"name", "ansible.builtin.assert"}, sorted(task)
    assert task["ansible.builtin.assert"]["that"] == DATA_LOSS_GATES[name]
    assert guards == (json.dumps("not ansible_check_mode"),), guards


def test_the_archive_gate_is_inside_the_dry_run_block_before_any_fetch_or_push():
    gate = _find(lambda t: t.get("name", "").startswith("Require the VM archive step"), "archive-completed gate")
    fetch = _find(lambda t: "ansible.builtin.fetch" in t, "fetch of the archive")
    push = _find(lambda t: t.get("ansible.builtin.include_tasks") == "tasks/site-config-push.yml", "site-config push")
    assert STEPS[gate][0] == 4 and gate < fetch < push


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


def test_the_size_is_measured_inside_the_rootless_user_namespace_before_any_export():
    # Plain `du` runs as the deploy user, which may not read the subuid-owned files of a rootless volume.
    # `podman unshare` runs it inside the user namespace. The failure stays closed: no ignore_errors.
    du = _find(lambda t: (_argv(t) or [])[:3] == ["podman", "unshare", "du"], "podman unshare du -sb")
    assert "ignore_errors" not in STEPS[du][2] and "failed_when" not in STEPS[du][2]
    export = _find(lambda t: (_argv(t) or [])[:3] == ["podman", "volume", "export"], "podman volume export")
    cap = _find(lambda t: CAP in _text(t) and "ansible.builtin.assert" in t and "_rt_du" in _text(t), "du cap")
    assert du < cap < export


def test_the_archive_host_play_is_one_podman_host():
    first = PLAYS[3]["tasks"][0]
    assert _asserts(first) == ["ansible_play_hosts_all | length == 1", "_rt_engine == 'podman'"]


def test_tududi_is_stopped_only_when_this_run_found_it_running():
    before = _find(lambda t: t.get("register") == "_rt_state_before", "state read before the stop")
    stop = _find(lambda t: (_argv(t) or [])[:2] == ["podman", "stop"], "podman stop")
    recorded = _find(lambda t: "ansible.builtin.set_fact" in t and "_tududi_stopped" in _text(t), "_tududi_stopped set")
    assert before < stop < recorded
    assert STEPS[before][2].get("check_mode") is False, "the pre-stop read runs under --check too"
    assert STEPS[stop][2]["when"] == STOP_WHEN
    assert STEPS[recorded][2]["when"] == STOP_WHEN


def test_no_play_that_delegates_overrides_the_connection():
    # A play-level or task-level `connection: local` makes every delegated task run on the controller
    # (verified on ansible-core 2.21: `hostname` under delegate_to printed the controller's hostname).
    delegating_plays = set()
    for index, play in enumerate(PLAYS):
        if "ansible.builtin.import_playbook" in play:
            continue
        for task, _ in _flatten(play.get("tasks")):
            if "delegate_to" in task:
                delegating_plays.add(index)
                assert task.get("connection") != "local", task["name"]
    assert delegating_plays == {4}, "only the deliver play delegates to the VM"
    for index in delegating_plays:
        assert PLAYS[index].get("connection") != "local", PLAYS[index]["name"]


def test_the_transport_check_runs_in_preflight_before_tududi_is_stopped():
    includes = [
        i
        for i, (_, _, t, _) in enumerate(STEPS)
        if t is not None and t.get("ansible.builtin.include_tasks") == TRANSPORT_CHECK
    ]
    # Two includes, not one: preflight runs the guard before tududi is stopped, and the delivery play keeps it
    # because the repo-wide rule requires every play that resolves an OpenBao URL to carry the guard.
    assert sorted(STEPS[i][0] for i in includes) == [1, 4], "one include in preflight, one in delivery"
    gate = STEPS.index(next(s for s in STEPS if s[2] is _confirm_task()))
    stop = _find(lambda t: (_argv(t) or [])[:2] == ["podman", "stop"], "podman stop")
    preflight = next(i for i in includes if STEPS[i][0] == 1)
    assert gate < preflight < stop
    assert PLAYS[1]["vars"]["_bao_url"].startswith("{{ hostvars[(groups['openbao_svc']")


def test_the_only_copy_sentence_is_in_the_run_record_the_header_and_the_survey():
    debug = [
        task
        for _, _, task, _ in STEPS
        if task is not None
        and "ansible.builtin.debug" in task
        and ONLY_COPY in " ".join(str(task["ansible.builtin.debug"]["msg"]).split())
    ]
    assert len(debug) == 1, "the run ends by naming the archive that is now the only copy"
    debug_index = STEPS.index(next(s for s in STEPS if s[2] is debug[0]))
    readback = _find(lambda t: "_rt_remote_stat.stat.checksum" in _text(t), "remote checksum gate")
    assert debug_index > readback and STEPS[debug_index][0] == 4
    header = " ".join(line.lstrip("#").strip() for line in PLAYBOOK.read_text().splitlines() if line.startswith("#"))
    assert ONLY_COPY in header
    assert ONLY_COPY in " ".join(_template()["survey_vars"][0]["description"].split())


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


# ── restore on failure and destroy wiring ───────────────────────────────────


def _rescue_argvs(task):
    return [_argv(t) for t in task.get("rescue", []) if isinstance(t, dict)]


def test_a_failure_before_the_destroy_restarts_tududi_when_this_run_stopped_it():
    archive = next(t for t in PLAYS[3]["tasks"] if "block" in t)
    assert ["podman", "start", "{{ _rt_container }}"] in _rescue_argvs(archive)
    deliver = next(t for t in PLAYS[4]["tasks"] if "block" in t)
    assert ["podman", "start", "{{ _rt_container }}"] in _rescue_argvs(deliver)


def test_the_destroy_is_pinned_to_the_tududi_service_and_skips_the_nested_guard():
    imp = PLAYS[5]
    assert imp["ansible.builtin.import_playbook"] == DESTROY
    assert imp["vars"]["target_service"] == "tududi"
    assert imp["vars"]["_extra_var_guard_nested"] is True
    assert imp["vars"]["target_host"].startswith("{{ groups['tududi_svc']")


def test_a_destroy_refusal_does_not_restart_tududi():
    # destroy-vm.yml is the last import, so no rescue follows it: a refusal there leaves tududi
    # stopped on purpose (the archive is proven; the operator investigates before any rerun).
    assert PLAYS[5]["ansible.builtin.import_playbook"] == DESTROY
    assert "rescue" not in PLAYS[5] and "block" not in PLAYS[5], "the destroy import carries no restart"
    assert PLAYS[-1] is PLAYS[5], "the destroy is the last play"


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


def test_the_operator_note_about_the_environment_is_in_the_survey_the_operator_sees():
    # setup-templates.yml publishes the survey description, not a template-level description key, so the
    # provisioning instruction lives in the survey text Semaphore shows at launch.
    (survey,) = _template()["survey_vars"]
    assert "Provision Seed Environment" in survey["description"]
    assert "seed_template 'Retire tududi'" in survey["description"]


def test_no_other_template_shares_the_isolated_environment():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    holders = [t["name"] for t in templates if t.get("isolated_environment") == "Retire tududi inputs"]
    assert holders == ["Retire tududi"]


def test_the_agents_table_lists_the_playbook():
    rows = [line for line in AGENTS.read_text().splitlines() if line.startswith("| Retire tududi |")]
    assert len(rows) == 1
    assert "`retire-tududi.yml`" in rows[0]


def test_delegated_cleanup_survives_an_unreachable_vm():
    """An UNREACHABLE delegated task skips the rest of rescue and the always section, which would leave
    the deploy key and the archive copies on the runner. The two delegated cleanup tasks tolerate it."""
    text = PLAYBOOK.read_text()
    for name in (
        "Restart tududi if this run stopped it (the service returns as it was)",
        "Wipe the VM's archive scratch dir (it holds a copy of the database)",
    ):
        blocks = [part.split("\n        - name:", 1)[0] for part in text.split(f'- name: "{name}"')[1:]]
        delegated = [block for block in blocks if "delegate_to:" in block]
        assert delegated, f"no delegated copy of {name}"
        for block in delegated:
            assert "ignore_unreachable: true" in block, name
