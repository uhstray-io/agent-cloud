"""The namespace gate names each refusal, the CLI reports the name, and the survey proves the
gate in enabled mode without reading the activation switch or placing anything.

A category is raised at one decision point and carries no identity. The synthetic records here
are the same shapes the runtime pass produces; the survey and the visible pre-placement report
are checked as playbook structure, and the report is also run under ansible-playbook.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from ansible_python import needs_ansible

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/summarize-o11y-scrape-baseline.py"
FILTER_FILE = ROOT / "platform/playbooks/filter_plugins/o11y_scrape_budget.py"
SURVEY_PLAYBOOK = ROOT / "platform/playbooks/survey-o11y-availability-gate.yml"
PREPARE = ROOT / "platform/playbooks/tasks/prepare-o11y-availability.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
README = ROOT / "platform/playbooks/README.md"
O11Y_README = ROOT / "platform/services/o11y/deployment/README.md"

SPEC = importlib.util.spec_from_file_location("namespace_gate_helper", HELPER)
assert SPEC and SPEC.loader
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
FILTER_SPEC = importlib.util.spec_from_file_location("o11y_scrape_budget_namespace", FILTER_FILE)
assert FILTER_SPEC and FILTER_SPEC.loader
BUDGET = importlib.util.module_from_spec(FILTER_SPEC)
FILTER_SPEC.loader.exec_module(BUDGET)

CATEGORIES = [
    "namespace_alloy_unavailable", "namespace_alloy_host_port", "namespace_shared",
    "namespace_network_mode", "namespace_provider_unverified", "namespace_unstable",
    "namespace_runtime_unreadable", "namespace_unverified",
]
ALLOY = "a" * 64
SIBLING = "b" * 64
HOST_NAMESPACE = (7, 1)


def _fixture():
    containers = [
        {
            "id": ALLOY, "image": "sha256:alloy", "running": True, "pid": 4101,
            "started_at": "2026-10-28T00:00:00Z", "network_mode": "o11y", "ports": {},
            "project": "o11y", "service": "alloy", "name": "o11y-alloy",
        },
        {
            "id": SIBLING, "image": "sha256:prometheus", "running": True, "pid": 4102,
            "started_at": "2026-10-28T00:00:01Z", "network_mode": "o11y", "ports": {},
            "project": "o11y", "service": "prometheus", "name": "o11y-prometheus",
        },
    ]
    processes = {
        ALLOY: {
            "pid": 4101, "proc_start_ticks": 100, "cmdline_sha256": "f" * 64,
            "netns_dev": 7, "netns_ino": 101,
        },
        SIBLING: {
            "pid": 4102, "proc_start_ticks": 101, "cmdline_sha256": "e" * 64,
            "netns_dev": 7, "netns_ino": 102,
        },
    }
    return containers, processes


def _refusal_category(call):
    with pytest.raises(GATE.Refusal) as caught:
        call()
    return caught.value.category


def test_synthetic_baseline_is_isolated():
    containers, processes = _fixture()
    assert GATE.validate_availability_namespace_snapshot(containers, processes, HOST_NAMESPACE)["status"] == "isolated"


def test_the_helper_and_the_filter_name_the_same_categories():
    assert GATE.AVAILABILITY_NAMESPACE_REASONS == BUDGET.AVAILABILITY_NAMESPACE_REASONS == frozenset(CATEGORIES)


# Each row mutates the synthetic baseline so that exactly one decision point refuses.
SNAPSHOT_DECISIONS = [
    ("namespace_alloy_unavailable", lambda c, p: c[0].update(running=False)),
    ("namespace_alloy_unavailable", lambda c, p: c[0].update(started_at="")),
    ("namespace_alloy_unavailable", lambda c, p: c[0].update(service="loki")),
    ("namespace_alloy_unavailable", lambda c, p: c[1].update(service="alloy")),
    ("namespace_alloy_host_port", lambda c, p: c[0].update(
        ports={"12345/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12345"}]})),
    ("namespace_network_mode", lambda c, p: c[0].update(network_mode="host")),
    ("namespace_network_mode", lambda c, p: c[0].update(network_mode="o11y:bridge")),
    ("namespace_network_mode", lambda c, p: c[1].update(network_mode="service:alloy")),
    ("namespace_shared", lambda c, p: p[SIBLING].update(netns_ino=101)),
    ("namespace_shared", lambda c, p: p[ALLOY].update(netns_ino=HOST_NAMESPACE[1])),
    ("namespace_unstable", lambda c, p: p[ALLOY].update(pid=4199)),
    ("namespace_runtime_unreadable", lambda c, p: p[SIBLING].update(cmdline_sha256="short")),
    ("namespace_runtime_unreadable", lambda c, p: c[1].update(started_at="")),
    ("namespace_runtime_unreadable", lambda c, p: c.append({"id": "short"})),
]


@pytest.mark.parametrize(("category", "mutate"), SNAPSHOT_DECISIONS)
def test_each_snapshot_decision_point_raises_its_category(category, mutate):
    containers, processes = _fixture()
    mutate(containers, processes)
    assert _refusal_category(
        lambda: GATE.validate_availability_namespace_snapshot(containers, processes, HOST_NAMESPACE)
    ) == category


def test_an_empty_or_oversized_container_list_is_an_unreadable_runtime():
    assert _refusal_category(
        lambda: GATE.validate_availability_namespace_snapshot([], {}, HOST_NAMESPACE)
    ) == "namespace_runtime_unreadable"
    containers, processes = _fixture()
    assert _refusal_category(
        lambda: GATE.validate_availability_namespace_snapshot(containers, processes, (-1, 1))
    ) == "namespace_runtime_unreadable"


def test_a_running_container_without_a_process_record_is_an_unreadable_runtime():
    containers, processes = _fixture()
    del processes[ALLOY]
    assert _refusal_category(
        lambda: GATE.validate_availability_namespace_snapshot(containers, processes, HOST_NAMESPACE)
    ) == "namespace_runtime_unreadable"


def test_a_process_identity_without_a_usable_pid_is_alloy_unavailable():
    assert _refusal_category(lambda: GATE._availability_process_identity(1)) == "namespace_alloy_unavailable"
    assert _refusal_category(lambda: GATE._availability_process_identity(True)) == "namespace_alloy_unavailable"


def test_an_unreadable_process_record_is_an_unreadable_runtime():
    assert _refusal_category(
        lambda: GATE._availability_process_identity(2_000_000_000)
    ) == "namespace_runtime_unreadable"


def test_an_unreadable_inspect_row_is_an_unreadable_runtime():
    assert _refusal_category(lambda: GATE._availability_inspect(b"not\tan\tinspect\trow")) == (
        "namespace_runtime_unreadable"
    )


def test_inspect_listing_refusals_are_unreadable_runtime(monkeypatch):
    monkeypatch.setattr(GATE, "_availability_command", lambda argv, **kwargs: b"not-an-id\n")
    assert _refusal_category(
        lambda: GATE._availability_inspect_all(GATE.time.monotonic() + 5, "/usr/bin/podman")
    ) == "namespace_runtime_unreadable"


def test_native_callers_keep_the_generic_command_category():
    # The native source readbacks share the command helper; their refusal category must not move.
    assert _refusal_category(
        lambda: GATE._availability_command(["python3", "-c", "import sys; sys.exit(3)"])
    ) == "namespace_unverified"
    assert _refusal_category(
        lambda: GATE._availability_command(["python3", "-c", "import sys; sys.exit(3)"],
                                           refusal="namespace_runtime_unreadable")
    ) == "namespace_runtime_unreadable"


def test_a_missing_executable_is_a_provider_refusal():
    assert _refusal_category(
        lambda: GATE._availability_executable_identity("namespace-gate-no-such-binary-xyz")
    ) == "namespace_provider_unverified"


def test_a_non_podman_engine_is_a_provider_refusal(monkeypatch):
    monkeypatch.setattr(GATE.platform, "system", lambda: "Linux")
    monkeypatch.setenv("CONTAINER_ENGINE", "docker")
    assert _refusal_category(GATE._availability_provider_context) == "namespace_provider_unverified"


def _provider_mocks(monkeypatch, tmp_path, *, version_output=None):
    identity = {"path": "/usr/bin/podman", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2}
    compose = {"path": "/usr/bin/podman-compose", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2}
    monkeypatch.setattr(GATE.platform, "system", lambda: "Linux")
    monkeypatch.setattr(GATE.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(GATE, "_availability_executable_identity",
                        lambda name: dict(identity if name == "podman" else compose, path=f"/usr/bin/{name}"))

    def command(argv, **_kwargs):
        if argv[0] == "/usr/bin/podman":
            return b"podman version 4.9.3\n"
        return version_output or b"podman-compose version 1.3.0\npodman version 4.9.3\n"

    monkeypatch.setattr(GATE, "_availability_command", command)
    monkeypatch.setenv("CONTAINER_ENGINE", "podman")
    monkeypatch.setenv("O11Y_DEPLOYMENT_DIR", str(tmp_path))
    monkeypatch.setenv("O11Y_AVAILABILITY_ENABLED", "true")
    monkeypatch.setenv("COMPOSE_OVERLAYS", "compose.availability.yml")
    monkeypatch.delenv("LOCAL_MODE", raising=False)
    monkeypatch.delenv("COMPOSE_CMD", raising=False)
    for name in GATE.AVAILABILITY_PROVIDER_OVERRIDE_NAMES | {
        "CONTAINERS_CONF", "CONTAINERS_CONF_OVERRIDE", "PODMAN_CONNECTION", "PODMAN_HOST"
    }:
        monkeypatch.delenv(name, raising=False)


def test_enabled_mode_without_its_overlay_is_a_provider_refusal(monkeypatch, tmp_path):
    _provider_mocks(monkeypatch, tmp_path)
    monkeypatch.setenv("COMPOSE_OVERLAYS", "compose.prod.yml")
    assert _refusal_category(GATE._availability_provider_context) == "namespace_provider_unverified"


def test_an_unrecognised_compose_banner_is_a_provider_refusal(monkeypatch, tmp_path):
    _provider_mocks(monkeypatch, tmp_path, version_output=b"Docker Compose version v1.3.0\npodman version 4.9.3\n")
    assert _refusal_category(GATE._availability_provider_context) == "namespace_provider_unverified"


def test_provider_drift_between_the_two_passes_is_a_provider_refusal(monkeypatch):
    _verify_context(monkeypatch)
    identity = {"path": "/usr/bin/podman", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2}
    compose = {"path": "/usr/bin/podman-compose", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2}
    drifted = dict(identity, sha256="b" * 64)
    monkeypatch.setattr(GATE, "_availability_executable_identity",
                        lambda name: drifted if name == "podman" else compose)
    containers, processes = _fixture()
    monkeypatch.setattr(GATE, "_availability_runtime_pass", lambda _path: _passes(containers, processes))
    assert _refusal_category(GATE.verify_availability_namespace) == "namespace_provider_unverified"


def _verify_context(monkeypatch):
    identity = {"path": "/usr/bin/podman", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2}
    compose = {"path": "/usr/bin/podman-compose", "sha256": "a" * 64, "size": 1, "device": 1, "inode": 2}
    context = (["/usr/bin/podman-compose"], ["compose.availability.yml"], "1.3.0", "podman version 4.9.3",
               ["compose.availability.yml"], {"compose": compose, "podman": identity})
    monkeypatch.setattr(GATE, "_availability_provider_context", lambda: context)
    monkeypatch.setattr(GATE, "_availability_executable_identity",
                        lambda name: compose if name != "podman" else identity)


def _passes(containers, processes):
    return (containers, processes, HOST_NAMESPACE)


def test_runtime_passes_that_disagree_are_unstable(monkeypatch):
    _verify_context(monkeypatch)
    containers, processes = _fixture()
    changed_containers, changed_processes = _fixture()
    changed_processes[ALLOY]["netns_ino"] = 555
    results = iter([_passes(containers, processes), _passes(changed_containers, changed_processes)])
    monkeypatch.setattr(GATE, "_availability_runtime_pass", lambda _path: next(results))
    assert _refusal_category(GATE.verify_availability_namespace) == "namespace_unstable"


def test_a_snapshot_that_does_not_match_the_expected_fingerprint_is_unstable(monkeypatch):
    _verify_context(monkeypatch)
    containers, processes = _fixture()
    monkeypatch.setattr(GATE, "_availability_runtime_pass", lambda _path: _passes(containers, processes))
    monkeypatch.setenv("O11Y_EXPECTED_NAMESPACE_SNAPSHOT_SHA256", "0" * 64)
    assert _refusal_category(GATE.verify_availability_namespace) == "namespace_unstable"


def test_a_runtime_context_that_does_not_match_the_expected_fingerprint_is_provider(monkeypatch):
    _verify_context(monkeypatch)
    containers, processes = _fixture()
    monkeypatch.setattr(GATE, "_availability_runtime_pass", lambda _path: _passes(containers, processes))
    monkeypatch.setenv("O11Y_EXPECTED_RUNTIME_CONTEXT_SHA256", "0" * 64)
    assert _refusal_category(GATE.verify_availability_namespace) == "namespace_provider_unverified"


def test_a_publisher_of_host_port_12345_is_refused_by_the_runtime_pass(monkeypatch):
    containers, _processes = _fixture()
    containers[1]["ports"] = {"12345/tcp": [{"HostIp": "0.0.0.0", "HostPort": "12345"}]}
    monkeypatch.setattr(GATE, "_availability_inspect_all", lambda _deadline, _path: containers)
    real_stat = os.stat
    monkeypatch.setattr(GATE.os, "stat", lambda path, *a, **k: (
        SimpleNamespace(st_dev=HOST_NAMESPACE[0], st_ino=HOST_NAMESPACE[1]) if path == "/proc/1/ns/net"
        else real_stat(path, *a, **k)
    ))
    assert _refusal_category(
        lambda: GATE._availability_runtime_pass("/usr/bin/podman")
    ) == "namespace_alloy_host_port"


def test_a_relative_podman_path_is_a_provider_refusal():
    assert _refusal_category(lambda: GATE._availability_runtime_pass("podman")) == "namespace_provider_unverified"


def test_the_candidate_port_parser_keeps_the_generic_category():
    # Candidate effective-compose checks stay generic on purpose: the survey is pre-placement only.
    assert _refusal_category(lambda: GATE._availability_compose_port("4317")) == "namespace_unverified"


# ---- CLI -----------------------------------------------------------------------------------

def _cli(monkeypatch, behaviour):
    monkeypatch.setattr(GATE.sys, "argv", ["summarize-o11y-scrape-baseline.py", "--verify-availability-namespace"])
    monkeypatch.setattr(GATE, "verify_availability_namespace", behaviour)
    return GATE.main()


@pytest.mark.parametrize("category", CATEGORIES)
def test_cli_prints_the_category_of_a_namespace_refusal(monkeypatch, capsys, category):
    def refuse(**_kwargs):
        raise GATE.Refusal(category)

    assert _cli(monkeypatch, refuse) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "unverified", "reason": category}


def test_cli_prints_the_generic_reason_for_an_unexpected_exception(monkeypatch, capsys):
    def explode(**_kwargs):
        raise RuntimeError("private detail SENTINEL-PRIVATE-TEXT /srv/example")

    assert _cli(monkeypatch, explode) == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "unverified", "reason": "namespace_unverified"}
    assert "SENTINEL-PRIVATE-TEXT" not in output and "/srv/example" not in output


def test_cli_does_not_print_a_refusal_that_is_not_a_namespace_category(monkeypatch, capsys):
    def refuse(**_kwargs):
        raise GATE.Refusal("source_path_refused")

    assert _cli(monkeypatch, refuse) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "unverified", "reason": "namespace_unverified"}


def test_cli_prints_the_isolated_result_and_exits_zero(monkeypatch, capsys):
    result = {"status": "isolated", "reason": "distinct_kernel_namespaces", "container_count": "2"}
    assert _cli(monkeypatch, lambda **_kwargs: dict(result)) == 0
    assert json.loads(capsys.readouterr().out) == result


# ---- fixed-category filter ----------------------------------------------------------------

SHA = "c" * 64
ISOLATED = json.dumps({
    "status": "isolated", "reason": "distinct_kernel_namespaces", "container_count": "2",
    "namespace_snapshot_sha256": SHA, "runtime_context_sha256": SHA,
    "provider": "podman-compose", "provider_version": "1.3.0",
}, sort_keys=True)


@pytest.mark.parametrize("reason", CATEGORIES)
def test_filter_returns_each_fixed_refusal_reason(reason):
    stdout = json.dumps({"status": "unverified", "reason": reason}, sort_keys=True)
    assert BUDGET.o11y_availability_namespace_diagnostic({"rc": 2, "stdout": stdout}) == reason


def test_filter_returns_isolated_only_for_a_complete_isolated_result():
    assert BUDGET.o11y_availability_namespace_diagnostic({"rc": 0, "stdout": ISOLATED}) == "isolated"


@pytest.mark.parametrize(
    "result",
    [
        "not a mapping",
        {"rc": 1, "stdout": ISOLATED},
        {"rc": "0", "stdout": ISOLATED},
        {"rc": True, "stdout": ISOLATED},
        {"rc": 0, "stdout": None},
        {"rc": 0, "stdout": "{not json"},
        {"rc": 0, "stdout": ISOLATED.replace('"provider":', '"provider": "x", "provider":')},
        {"rc": 0, "stdout": ISOLATED.replace("NaN", "")
            .replace('"container_count": "2"', '"container_count": NaN')},
        {"rc": 0, "stdout": json.dumps({**json.loads(ISOLATED), "namespace_snapshot_sha256": "short"})},
        {"rc": 0, "stdout": json.dumps({**json.loads(ISOLATED), "status": "unverified"})},
        {"rc": 0, "stdout": json.dumps({**json.loads(ISOLATED), "extra": "pid 4101"})},
        {"rc": 2, "stdout": json.dumps({"status": "unverified", "reason": "namespace_shared", "pid": 4101})},
        {"rc": 2, "stdout": json.dumps({"status": "unverified", "reason": "private_reason"})},
        {"rc": 2, "stdout": json.dumps({"status": "unverified", "reason": ["namespace_shared"]})},
        {"rc": 2, "stdout": json.dumps({"status": "isolated", "reason": "namespace_shared"})},
        {"rc": 2, "stdout": "x" * 5000},
    ],
)
def test_filter_collapses_anything_else_to_the_generic_category(result):
    assert BUDGET.o11y_availability_namespace_diagnostic(result) == "namespace_unverified"


def test_filter_never_returns_protected_text():
    stdout = json.dumps({"status": "unverified", "reason": "namespace_shared", "pid": 4101})
    value = BUDGET.o11y_availability_namespace_diagnostic({"rc": 2, "stdout": stdout})
    assert value == "namespace_unverified"
    assert "4101" not in value


# ---- survey playbook: read-only, enabled mode, never the switch -----------------------------

def _survey():
    return yaml.safe_load(SURVEY_PLAYBOOK.read_text(encoding="utf-8"))


def _tasks_of(play):
    return play.get("tasks", [])


def test_survey_refuses_internal_extra_vars_before_anything_else():
    first = _survey()[0]
    assert first["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"


def test_survey_requires_the_reviewed_checkout_and_its_helper_bytes():
    plays = _survey()
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert plays[1]["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/files/summarize-o11y-scrape-baseline.py",
        "platform/playbooks/filter_plugins/o11y_scrape_budget.py",
    ]


def test_survey_never_reads_the_activation_switch_or_imports_the_preparation():
    # Compare the parsed playbook, not the file: comments may name the switch to say it is not read.
    executed = yaml.safe_dump(_survey(), sort_keys=True)
    assert "o11y_availability_probes" not in executed
    assert "prepare-o11y-availability" not in executed
    assert "_o11y_availability" not in executed


def _gate_script_task():
    play = next(p for p in _survey() if p.get("hosts") == "o11y_svc")
    scripts = [t for t in _tasks_of(play) if "ansible.builtin.script" in t]
    assert len(scripts) == 1, scripts
    return scripts[0]


def test_survey_runs_the_gate_in_enabled_mode():
    task = _gate_script_task()
    assert task["ansible.builtin.script"]["cmd"].endswith("--verify-availability-namespace")
    env = task["environment"]
    assert env["O11Y_AVAILABILITY_ENABLED"] == "true"
    assert env["COMPOSE_OVERLAYS"].split() == ["compose.availability.yml"]
    assert env["COMPOSE_CMD"] == "podman-compose"


def test_survey_gate_task_is_read_only_and_runs_in_check_mode():
    task = _gate_script_task()
    assert task["check_mode"] is False
    assert task["changed_when"] is False
    assert task["failed_when"] is False
    assert task["no_log"] is True


def test_survey_uses_only_read_and_report_modules():
    allowed = {
        "ansible.builtin.assert", "ansible.builtin.debug", "ansible.builtin.script",
        "ansible.builtin.set_fact", "ansible.builtin.import_playbook",
    }
    modules = set()
    for play in _survey():
        for task in _tasks_of(play):
            modules.update(key for key in task if key.startswith(("ansible.", "community.")))
    assert modules <= allowed, modules - allowed


def test_survey_registers_and_sets_only_underscore_names():
    for play in _survey():
        for task in _tasks_of(play):
            if "register" in task:
                assert task["register"].startswith("_"), task
            if "ansible.builtin.set_fact" in task:
                assert all(name.startswith("_") for name in task["ansible.builtin.set_fact"]), task


def test_survey_reports_and_gates_only_the_fixed_category():
    play = next(p for p in _survey() if p.get("hosts") == "o11y_svc")
    tasks = _tasks_of(play)
    names = [task.get("name", "") for task in tasks]
    reduce_index = names.index("Reduce the protected namespace result to a fixed category")
    debug_index = names.index("Report only the fixed availability namespace category")
    assert reduce_index < debug_index < names.index("Require an isolated current Alloy namespace in enabled mode")
    reducer = tasks[reduce_index]
    assert reducer["no_log"] is True
    assert reducer["ansible.builtin.set_fact"]["_survey_namespace_diagnostic"] == (
        "{{ _survey_namespace_run | o11y_availability_namespace_diagnostic }}"
    )
    assert tasks[debug_index]["ansible.builtin.debug"]["msg"] == (
        "Availability namespace gate in enabled mode: {{ _survey_namespace_diagnostic }}"
    )
    assert "no_log" not in tasks[debug_index]
    gate = tasks[names.index("Require an isolated current Alloy namespace in enabled mode")]
    assert gate["ansible.builtin.assert"]["that"] == "_survey_namespace_diagnostic == 'isolated'"


def test_survey_runs_no_write_or_secret_read():
    text = SURVEY_PLAYBOOK.read_text(encoding="utf-8")
    for forbidden in ("ansible.builtin.copy", "ansible.builtin.template", "ansible.builtin.file",
                      "ansible.builtin.uri", "community.hashi_vault", "X-Vault-Token", "secret/services"):
        assert forbidden not in text, forbidden


def test_survey_template_is_registered_as_a_dev_bound_read_only_survey():
    templates = yaml.safe_load(TEMPLATES.read_text(encoding="utf-8"))["templates"]
    matches = [t for t in templates if t["name"] == "Survey o11y Availability Gate (Dev)"]
    assert len(matches) == 1
    template = matches[0]
    assert template["playbook"] == "platform/playbooks/survey-o11y-availability-gate.yml"
    assert template["repository"] == "agent-cloud dev"
    survey = template["survey_vars"]
    assert [var["name"] for var in survey] == ["expected_repository_sha"]
    assert survey[0]["required"] is True


def test_readmes_document_the_survey_and_the_fixed_categories():
    playbook_readme = README.read_text(encoding="utf-8")
    assert "survey-o11y-availability-gate.yml" in playbook_readme
    o11y = O11Y_README.read_text(encoding="utf-8")
    assert "Survey o11y Availability Gate (Dev)" in o11y
    for category in CATEGORIES:
        assert category in o11y, category


# ---- pre-placement report (prepare-o11y-availability.yml) ----------------------------------

def _prepare_tasks():
    return yaml.safe_load(PREPARE.read_text(encoding="utf-8"))


def test_pre_placement_report_sits_between_the_probe_and_the_protected_gate():
    tasks = _prepare_tasks()
    names = [task.get("name", "") for task in tasks]
    probe = names.index("Prove current Alloy namespace isolation before placement when probes are enabled")
    setter = names.index("Validate the fixed availability namespace diagnostic category")
    report = names.index("Report only the fixed availability namespace diagnostic category")
    gate = names.index("Require actual current Alloy network namespace isolation before placement")
    assert probe < setter < report < gate
    assert tasks[setter]["no_log"] is True
    assert tasks[setter]["ansible.builtin.set_fact"]["_o11y_namespace_diagnostic"] == (
        "{{ _o11y_namespace_preplacement | o11y_availability_namespace_diagnostic }}"
    )
    visible = tasks[report]
    assert "no_log" not in visible
    assert visible["ansible.builtin.debug"]["msg"] == (
        "Availability namespace diagnostic: {{ _o11y_namespace_diagnostic }}"
    )
    assert "_o11y_namespace_diagnostic != 'isolated'" in visible["when"]
    # The protected gate itself is unchanged.
    assert tasks[gate]["no_log"] is True
    assert tasks[gate]["ansible.builtin.assert"]["that"][0] == "_o11y_namespace_preplacement.rc == 0"


@needs_ansible
def test_pre_placement_report_prints_only_the_category_under_ansible(tmp_path):
    sentinel = "NAMESPACE-DIAGNOSTIC-SECRET-SENTINEL"
    playbook = tmp_path / "namespace-report.yml"
    playbook.write_text(
        """---
- hosts: localhost
  gather_facts: false
  tasks:
    - name: Produce a protected namespace refusal
      ansible.builtin.shell: >-
        printf '%s' '{"status":"unverified","reason":"namespace_shared"}';
        printf '%s' NAMESPACE-DIAGNOSTIC-SECRET-SENTINEL >&2;
        exit 2
      register: _o11y_namespace_preplacement
      changed_when: false
      failed_when: false
      no_log: true
    - name: Validate the fixed availability namespace diagnostic category
      ansible.builtin.set_fact:
        _o11y_namespace_diagnostic: "{{ _o11y_namespace_preplacement | o11y_availability_namespace_diagnostic }}"
      no_log: true
    - name: Report only the fixed availability namespace diagnostic category
      ansible.builtin.debug:
        msg: "Availability namespace diagnostic: {{ _o11y_namespace_diagnostic }}"
      when:
        - _o11y_namespace_diagnostic is defined
        - _o11y_namespace_diagnostic != 'isolated'
    - name: Preserve the protected strict gate
      ansible.builtin.assert:
        that: _o11y_namespace_preplacement.rc == 0
        fail_msg: strict namespace gate refused
      no_log: true
""",
        encoding="utf-8",
    )
    collections = tmp_path / "collections"
    collections.mkdir()
    environment = os.environ.copy()
    environment["ANSIBLE_FILTER_PLUGINS"] = str(ROOT / "platform/playbooks/filter_plugins")
    environment["ANSIBLE_COLLECTIONS_PATH"] = str(collections)
    local_tmp = tmp_path / "ansible-local"
    local_tmp.mkdir()
    environment["ANSIBLE_LOCAL_TEMP"] = str(local_tmp)
    done = subprocess.run(
        [shutil.which("ansible-playbook"), "-vvv", "-i", "localhost,", "-c", "local", str(playbook)],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120, check=False,
    )
    output = done.stdout + done.stderr
    assert done.returncode != 0
    assert "Availability namespace diagnostic: namespace_shared" in output, output
    assert "strict namespace gate refused" not in output
    assert sentinel not in output
