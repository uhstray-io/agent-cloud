"""The endpoint availability drill (change estate-wide-observability-instrumentation, task 2.3c.4).

drill-o11y-endpoint-availability.yml fails ONE reviewed availability target on purpose, waits for
Grafana's endpoint rule and its Discord receipt to name that target, then restores the normal
projection with a second deploy and clears a marker. recover-o11y-endpoint-drill.yml restores the
normal projection and clears the same marker without gating on endpoint health.

These tests pin the order that makes that safe: every refusal before the marker is claimed, the
drilled and the normal deploy each given exactly the inputs they need, the probe's endpoint shape,
an outcome that cannot read as passed unless the proof completed, and a recovery that never gates
on health. Two checks run the committed YAML through the real ansible-playbook.
"""

import copy
import json
import os
import re
import subprocess
from pathlib import Path

import playbook_yaml
import yaml
from ansible_python import needs_ansible
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = ROOT / "platform/playbooks"
DRILL = PLAYBOOKS / "drill-o11y-endpoint-availability.yml"
RECOVER = PLAYBOOKS / "recover-o11y-endpoint-drill.yml"
PROBE = PLAYBOOKS / "tasks/o11y-alert-probe.yml"
PREPARE = PLAYBOOKS / "tasks/prepare-o11y-availability.yml"
DEPLOY = PLAYBOOKS / "deploy-o11y.yml"
PREFLIGHT = PLAYBOOKS / "tasks/o11y-alert-delivery-preflight.yml"
ALERTS = ROOT / "platform/services/o11y/deployment/templates/alerts.yml.j2"
CENSUS = PLAYBOOKS / "files/o11y-coverage-census.py"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
GUARD = "refuse-internal-extra-vars.yml"
NESTED = "_extra_var_guard_nested"
RULE_UID = "o11y_endpoint_probe_failed"
AVAILABILITY_JOB = "o11y-availability"
MARKER = "{{ _monorepo_dir | dirname }}/.o11y-endpoint-drill"
BLOCKS = ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks")

# A target the census would select: its id and service identity differ, so a swapped label fails.
TARGET = {
    "target_id": "service:alpha-health",
    "service_identity": "service:alpha",
    "environment": "prod",
    "cluster": "agent-cloud",
}


def _label(entry):
    return entry.get("name") or entry.get("ansible.builtin.import_playbook")


def yaml_dump(document):
    return yaml.safe_dump(document, sort_keys=False)


def _ansible(cwd, playbook, *args):
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(cwd), ANSIBLE_REMOTE_TEMP=str(cwd))
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(playbook), *args], cwd=cwd, env=env,
                          text=True, capture_output=True, check=False, timeout=180)


def _walk(entries):
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        yield entry
        for key in BLOCKS:
            if isinstance(entry.get(key), list):
                yield from _walk(entry[key])


def _play(path, name):
    return next(p for p in playbook_yaml.load(path) if isinstance(p, dict) and p.get("name") == name)


def _tasks_of(path, name):
    return _play(path, name)["tasks"]


def _named(tasks, name):
    return next(t for t in _walk(tasks) if t.get("name") == name)


def _render(text, context=None):
    env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    env.filters["dirname"] = os.path.dirname
    env.filters["from_json"] = json.loads
    env.filters["to_json"] = json.dumps
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    return env.from_string(text).render(context or {}).strip()


def _expr(value, context):
    env = Environment(undefined=StrictUndefined)
    env.filters["from_json"] = json.loads
    env.filters["bool"] = bool
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    return env.compile_expression(value)(context)


def _stdin_expression(path):
    """The census stdin of the prepare task, exactly as the committed YAML spells it."""
    task = next(t for t in playbook_yaml.load(path) if isinstance(t, dict) and "name" in t
                and t["name"] == "Validate and privately prepare the reviewed availability projection")
    return task["ansible.builtin.command"]["stdin"]


def _deploy_closure(path, seen=None):
    """Every name a playbook or task file, and its includes, registers or sets as a fact."""
    seen = set() if seen is None else seen
    if path in seen or not path.is_file():
        return set()
    seen.add(path)
    names = set()
    for entry in _walk(playbook_yaml.load(path) or []):
        if isinstance(entry.get("register"), str):
            names.add(entry["register"])
        for key in ("ansible.builtin.set_fact", "set_fact"):
            if isinstance(entry.get(key), dict):
                names.update(entry[key])
        for key in ("ansible.builtin.include_tasks", "ansible.builtin.import_tasks",
                    "ansible.builtin.import_playbook", "include_tasks", "import_tasks", "import_playbook"):
            target = entry.get(key)
            if isinstance(target, dict):
                target = target.get("file")
            if isinstance(target, str):
                for base in (path.parent, PLAYBOOKS):
                    if (base / target).is_file():
                        names |= _deploy_closure((base / target).resolve(), seen)
                        break
    return names


# ---- the drill's play order and its imports ---------------------------------------------------


def test_the_endpoint_drill_runs_its_plays_in_the_order_that_proves_each_step():
    plays = [_label(p) for p in playbook_yaml.load(DRILL)]
    assert plays == [
        "Refuse extra vars that set internal names",
        "Pre-flight",
        "Verify the reviewed Dev checkout before the endpoint drill",
        "Validate the endpoint drill target and claim the drill marker",
        "Deploy the endpoint drill projection",
        "Prove the endpoint alert fires and reaches Discord",
        "Restore the normal o11y projection",
        "Prove recovery and clear the endpoint-drill marker",
    ]


def test_the_drilled_import_carries_the_guard_skip_and_the_one_drill_input():
    entry = next(p for p in playbook_yaml.load(DRILL) if p.get("name") == "Deploy the endpoint drill projection")
    assert entry["ansible.builtin.import_playbook"] == "deploy-o11y.yml"
    assert entry["vars"] == {
        NESTED: True,
        "_o11y_availability_drill": {"target_id": "{{ availability_drill_target_id }}", "status_code": 599},
    }
    # The census reads status_code as an integer; a quoted 599 would be refused.
    assert type(entry["vars"]["_o11y_availability_drill"]["status_code"]) is int


def test_the_restoring_import_carries_only_the_guard_skip():
    entry = next(p for p in playbook_yaml.load(DRILL) if p.get("name") == "Restore the normal o11y projection")
    assert entry["ansible.builtin.import_playbook"] == "deploy-o11y.yml"
    assert entry["vars"] == {NESTED: True}


def test_the_claim_is_the_last_step_before_the_first_deploy_and_follows_every_refusal():
    tasks = _tasks_of(DRILL, "Validate the endpoint drill target and claim the drill marker")
    names = [t.get("name") for t in tasks]
    claim = names.index("Atomically claim the endpoint-drill interruption marker")
    assert claim == len(names) - 1, "the claim must be the final task before the drilled deploy"
    refusals = [
        "Require a real production endpoint drill",
        "Require one named availability target id",
        "Require the paused-canary workflow to be settled",
        "Require no active alert drill to be in progress",
        "Require no endpoint drill to be in progress",
        "Require the endpoint availability rule to be provisioned and active",
        "Require the declared Discord contact point exactly once",
        "Require exactly one drilled target in the validated projection",
    ]
    for name in refusals:
        assert names.index(name) < claim, name
    # The validation that can refuse on the named target runs before the claim too.
    assert names.index("Validate the named target through the bounded availability projection") < claim
    assert names.index("Verify Discord receipt credentials and history access") < claim


def test_the_claim_refuses_when_the_marker_exists_and_creates_it_atomically():
    tasks = _tasks_of(DRILL, "Validate the endpoint drill target and claim the drill marker")
    claim = _named(tasks, "Atomically claim the endpoint-drill interruption marker")
    assert claim["ansible.builtin.command"]["argv"] == ["mkdir", "{{ _endpoint_marker }}"]
    assert claim["when"] == "not ansible_check_mode"
    stat = _named(tasks, "Inspect an unfinished endpoint drill")
    assert stat["ansible.builtin.stat"]["path"] == "{{ _endpoint_marker }}"
    refusal = _named(tasks, "Require no endpoint drill to be in progress")
    assert refusal["ansible.builtin.assert"]["that"] == "not _endpoint_drill_marker.stat.exists"


def test_the_marker_lives_outside_the_checkout_and_the_normal_deploy_does_not_refuse_on_it():
    drill_vars = _play(DRILL, "Validate the endpoint drill target and claim the drill marker")["vars"]
    recover_vars = _play(RECOVER, "Clear the endpoint-drill marker after the normal deploy")["vars"]
    assert drill_vars["_endpoint_marker"] == MARKER
    assert recover_vars["_endpoint_marker"] == MARKER
    assert ".o11y-endpoint-drill" not in DEPLOY.read_text(), "deploy-o11y.yml must stay the recovery path"


def test_the_census_input_is_the_survey_target_and_the_drilled_import_var():
    claim_tasks = _tasks_of(DRILL, "Validate the endpoint drill target and claim the drill marker")
    prepare_include = _named(claim_tasks, "Validate the named target through the bounded availability projection")
    assert prepare_include["ansible.builtin.include_tasks"] == "tasks/prepare-o11y-availability.yml"
    drilled = next(p for p in playbook_yaml.load(DRILL) if p.get("name") == "Deploy the endpoint drill projection")
    assert prepare_include["vars"] == {"_o11y_availability_drill": drilled["vars"]["_o11y_availability_drill"]}


# ---- the probe proof, its outcome, and the final recovery proof --------------------------------


def test_the_probe_is_called_with_the_endpoint_shape_and_one_instance_line():
    tasks = _tasks_of(DRILL, "Prove the endpoint alert fires and reaches Discord")
    probe = next(t for t in _walk(tasks) if t.get("ansible.builtin.include_tasks") == "tasks/o11y-alert-probe.yml")
    vars_ = probe["vars"]
    for key in ("o11y_production_static_probe", "drill_expect_alert", "o11y_alerts_enabled", "o11y_grouped_probe"):
        assert vars_[key] is True, key
    assert vars_["_alert_probe_kind"] == "endpoint"
    assert vars_["_alert_probe_rule_uid"] == RULE_UID
    assert vars_["_alert_probe_instance_lines"] is False
    assert vars_["_alert_probe_ready_query"] == "{{ _endpoint_probe_query }}"
    expected = _play(DRILL, "Prove the endpoint alert fires and reaches Discord")["vars"]
    instances = vars_["o11y_probe_expected_instances"]
    assert len(instances) == 1
    assert _render(instances[0], {"_endpoint_drill_target": TARGET}) == TARGET["target_id"]
    assert _render(expected["expected_instance"], {"_endpoint_drill_target": TARGET}) == TARGET["target_id"]
    assert _render(expected["expected_service"], {"_endpoint_drill_target": TARGET}) == TARGET["service_identity"]


def test_the_delivery_marker_is_the_contact_template_line_for_the_service_identity():
    contact = (ROOT / "platform/services/o11y/deployment/templates/alert-contact.yml.j2").read_text()
    assert "o11y-delivery-status=firing service=" in contact
    play_vars = _play(DRILL, "Prove the endpoint alert fires and reaches Discord")["vars"]
    rendered = _render(play_vars["_delivery_marker"], {"_endpoint_drill_target": TARGET})
    assert rendered == "o11y-delivery-status=firing service=" + TARGET["service_identity"]


def test_the_outcome_is_passed_only_after_the_probe_completes_and_failure_is_recorded_not_raised():
    tasks = _tasks_of(DRILL, "Prove the endpoint alert fires and reaches Discord")
    assert tasks[0]["ansible.builtin.set_fact"] == {"_endpoint_drill_outcome": "not-run"}
    block = next(t for t in tasks if "block" in t)
    assert block["block"][-1]["ansible.builtin.set_fact"] == {"_endpoint_drill_outcome": "passed"}
    assert block["block"][-2]["ansible.builtin.include_tasks"] == "tasks/o11y-alert-probe.yml"
    assert len(block["rescue"]) == 1
    rescue = block["rescue"][0]
    assert rescue["ansible.builtin.set_fact"] == {"_endpoint_drill_outcome": "failed"}
    assert "ansible.builtin.fail" not in rescue and "ansible.builtin.assert" not in rescue


def test_the_final_assert_on_the_outcome_is_the_last_task_and_follows_the_recovery_proofs():
    tasks = _tasks_of(DRILL, "Prove recovery and clear the endpoint-drill marker")
    names = [t.get("name") for t in tasks]
    assert tasks[-1]["ansible.builtin.assert"]["that"] == "_endpoint_drill_outcome == 'passed'"
    clear = names.index("Clear the endpoint-drill marker after the recovery checks pass")
    recovery_block = next(i for i, t in enumerate(tasks) if "block" in t)
    assert recovery_block < clear < names.index("Name the census receipt the operator records for a passed drill")
    assert names.index("Require the endpoint alert proof to have passed") == len(tasks) - 1


def test_the_recovery_proof_waits_for_the_probe_and_the_resolved_alert_before_the_marker_clears():
    tasks = _tasks_of(DRILL, "Prove recovery and clear the endpoint-drill marker")
    block = next(t for t in tasks if "block" in t)
    probe_wait = block["block"][0]
    assert probe_wait["ansible.builtin.command"]["argv"][-1] == (
        "http://prometheus:9090/api/v1/query?query={{ _endpoint_probe_query | urlencode }}")
    assert "selectattr('value.1', 'equalto', '1')" in probe_wait["until"]
    alerts_wait = block["block"][1]
    title = "selectattr('labels.alertname', 'equalto', (_endpoint_rule.stdout | from_json).title)"
    assert title in alerts_wait["until"]
    assert "selectattr('labels.target_id', 'equalto', _endpoint_drill_target.target_id)" in alerts_wait["until"]
    assert "selectattr('state', 'match', '^(Alerting|firing)$')" in alerts_wait["until"]
    assert alerts_wait["until"].rstrip().endswith("| list | length) == 0")
    rescue = block["rescue"][0]
    assert "Recover o11y Endpoint Drill (Dev)" in rescue["ansible.builtin.fail"]["msg"]


def test_the_probe_selector_is_the_rule_identity_from_the_alert_template():
    # The identity line in alerts.yml.j2 is the one selector the endpoint rule fires on.
    line = next(x for x in ALERTS.read_text().splitlines() if "{% set _identity =" in x)
    expression = re.search(r"_identity = (.*?) %\}", line).group(1)
    pieces = re.findall(r"'([^']*)'|target\.(\w+)", expression)
    identity = "".join(lit if lit else TARGET[name] for lit, name in pieces)
    expected = "probe_success{" + identity + "}"
    census = CENSUS.read_text()
    assert f'AVAILABILITY_JOB = "{AVAILABILITY_JOB}"' in census
    drill_query = _play(DRILL, "Validate the endpoint drill target and claim the drill marker")
    binding = _named(drill_query["tasks"], "Bind the endpoint probe selector the readiness waits query")
    rendered = _render(binding["ansible.builtin.set_fact"]["_endpoint_probe_query"], {"_endpoint_drill_target": TARGET})
    assert rendered == expected
    assert 'job="o11y-availability"' in identity



# ---- recovery ---------------------------------------------------------------------------------


def test_the_recovery_runs_guard_preflight_checkout_production_check_deploy_then_marker_clear():
    names = [_label(p) for p in playbook_yaml.load(RECOVER)]
    assert names == [
        "Refuse extra vars that set internal names",
        "Pre-flight",
        "Verify the reviewed Dev checkout before endpoint-drill recovery",
        "Require a real production run for endpoint-drill recovery",
        "Restore the normal o11y projection",
        "Clear the endpoint-drill marker after the normal deploy",
    ]
    restore = next(p for p in playbook_yaml.load(RECOVER) if p.get("name") == "Restore the normal o11y projection")
    assert restore["vars"] == {NESTED: True}


def test_the_recovery_does_not_gate_on_endpoint_health():
    text = RECOVER.read_text()
    assert "probe_success" not in text
    assert "api/v1/query" not in text
    assert "until:" not in text, "a health wait would stop a really-down target from recovering"


def test_the_recovery_clears_the_marker_with_rmdir_only_after_the_deploy_and_idempotently():
    tasks = _tasks_of(RECOVER, "Clear the endpoint-drill marker after the normal deploy")
    clear = _named(tasks, "Clear the endpoint-drill marker")
    assert clear["ansible.builtin.command"]["argv"] == ["rmdir", "{{ _endpoint_marker }}"]
    assert clear["ansible.builtin.command"]["removes"] == "{{ _endpoint_marker }}"
    assert clear["when"] == "not ansible_check_mode"
    assert "state: absent" not in RECOVER.read_text(), "a recursive removal would delete more than the marker"


# ---- the census-only input and the post-deploy names -----------------------------------------


def test_the_names_the_probe_reads_from_the_preflight_are_not_set_again_by_the_deploy():
    closure = _deploy_closure(DEPLOY)
    for name in ("_discord_secret", "_webhook_id", "_before_messages", "_o11y_secret"):
        assert name not in closure, f"deploy-o11y.yml sets {name}; the probe would read the deploy's value"
    # The drill's own facts are read after the drilled deploy, so the deploy must not set them.
    for name in ("_endpoint_drill_target", "_endpoint_probe_query", "_endpoint_drill_outcome", "_endpoint_rule"):
        assert name not in closure, name


def test_the_recovery_proof_reads_no_preflight_fact():
    text = str(_tasks_of(DRILL, "Prove recovery and clear the endpoint-drill marker"))
    for name in ("_bao_auth", "_before_messages", "_discord_secret", "_webhook_id"):
        assert name not in text, name


# ---- templates --------------------------------------------------------------------------------


def _templates():
    return {t["name"]: t for t in yaml.safe_load(TEMPLATES.read_text())["templates"]}


def test_the_drill_template_names_the_playbook_on_dev_with_its_survey():
    entry = _templates()["Drill o11y Endpoint Availability (Dev)"]
    assert entry["playbook"] == "platform/playbooks/drill-o11y-endpoint-availability.yml"
    assert entry["repository"] == "agent-cloud dev"
    survey = {v["name"]: v for v in entry["survey_vars"]}
    assert set(survey) == {"expected_repository_sha", "service_branch", "availability_drill_target_id",
                           "deploy_force_recreate"}
    assert survey["expected_repository_sha"]["required"] is True
    assert survey["availability_drill_target_id"]["required"] is True
    assert survey["service_branch"]["default_value"] == "dev"
    assert survey["deploy_force_recreate"]["default_value"] == "false"
    assert "dev_variant" not in entry


def test_the_recover_template_names_the_playbook_on_dev_without_a_target():
    entry = _templates()["Recover o11y Endpoint Drill (Dev)"]
    assert entry["playbook"] == "platform/playbooks/recover-o11y-endpoint-drill.yml"
    assert entry["repository"] == "agent-cloud dev"
    survey = {v["name"]: v for v in entry["survey_vars"]}
    assert set(survey) == {"expected_repository_sha", "service_branch", "deploy_force_recreate"}


# ---- real ansible-playbook: the drill's own import vars and the committed census stdin ---------


@needs_ansible
def test_the_drilled_import_vars_reach_the_census_stdin_as_an_integer_target(tmp_path):
    drilled = copy.deepcopy(
        next(p for p in playbook_yaml.load(DRILL) if p.get("name") == "Deploy the endpoint drill projection")["vars"])
    stdin = _stdin_expression(PREPARE)
    child = tmp_path / "child.yml"
    out = tmp_path / "rendered.json"
    child.write_text(yaml_dump([{
        "name": "render the census stdin",
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": [{"name": "write", "ansible.builtin.copy": {"dest": str(out), "content": stdin}}],
    }]))
    importer = tmp_path / "importer.yml"
    importer.write_text(yaml_dump([{
        "name": "import the drilled vars",
        "ansible.builtin.import_playbook": "child.yml",
        "vars": drilled,
    }]))
    proc = _ansible(tmp_path, importer, "-e", "availability_drill_target_id=service:alpha-health")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(out.read_text())
    assert payload["drill"] == {"target_id": "service:alpha-health", "status_code": 599}
    assert type(payload["drill"]["status_code"]) is int
    assert set(payload) == {"targets", "inventory_revision", "enabled", "reviewed", "limits",
                            "receiver_environment", "cluster", "shared_sample_limit", "drill"}


@needs_ansible
def test_the_normal_projection_renders_drill_as_null_so_the_census_sees_no_drill(tmp_path):
    stdin = _stdin_expression(PREPARE)
    child = tmp_path / "child.yml"
    out = tmp_path / "rendered.json"
    child.write_text(yaml_dump([{
        "name": "render the census stdin",
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": [{"name": "write", "ansible.builtin.copy": {"dest": str(out), "content": stdin}}],
    }]))
    importer = tmp_path / "importer.yml"
    importer.write_text(yaml_dump([{
        "name": "restore",
        "ansible.builtin.import_playbook": "child.yml",
        "vars": {NESTED: True},
    }]))
    proc = _ansible(tmp_path, importer)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(out.read_text())["drill"] is None
