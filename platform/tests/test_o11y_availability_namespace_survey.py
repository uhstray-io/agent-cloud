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
import tempfile
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

CANDIDATE_CATEGORIES = [
    "namespace_candidate_compose_unreadable", "namespace_candidate_inputs_label",
    "namespace_candidate_pod", "namespace_candidate_alloy_spec", "namespace_candidate_ports",
    "namespace_candidate_project",
]
CATEGORIES = [
    "namespace_alloy_unavailable", "namespace_alloy_host_port", "namespace_shared",
    "namespace_network_mode", "namespace_provider_unverified", "namespace_unstable",
    "namespace_runtime_unreadable", "namespace_unverified", "namespace_internal_error",
] + CANDIDATE_CATEGORIES
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
    # The filter-only word is computed from a helper exit code; the helper never prints it.
    assert "namespace_helper_failed" not in GATE.AVAILABILITY_NAMESPACE_REASONS


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


def test_a_process_identity_without_a_usable_pid_is_an_unreadable_runtime():
    # Any related container reaches this check, so the category is the runtime one, not Alloy's.
    assert _refusal_category(lambda: GATE._availability_process_identity(1)) == "namespace_runtime_unreadable"
    assert _refusal_category(lambda: GATE._availability_process_identity(True)) == "namespace_runtime_unreadable"


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


def _stub_namespace_reads(monkeypatch, *, self_error=None):
    """The host reference is the helper's own netns. /proc/1 is EACCES for the unprivileged deploy user.

    Returns the list of paths the runtime pass stat'd, in order.
    """
    reads = []
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if path in ("/proc/1/ns/net", "/proc/self/ns/net"):
            reads.append(path)
        if path == "/proc/1/ns/net":
            raise PermissionError(13, "Permission denied", path)
        if path == "/proc/self/ns/net":
            if self_error is not None:
                raise self_error
            return SimpleNamespace(st_dev=HOST_NAMESPACE[0], st_ino=HOST_NAMESPACE[1])
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(GATE.os, "stat", stat)
    return reads


def test_the_runtime_pass_reads_the_helper_netns_and_never_pid_one(monkeypatch):
    containers, _processes = _fixture()
    monkeypatch.setattr(GATE, "_availability_inspect_all", lambda _deadline, _path: containers)
    monkeypatch.setattr(GATE, "_availability_process_identity", lambda pid: {"pid": pid})
    reads = _stub_namespace_reads(monkeypatch)
    _related, _process_identities, host_namespace = GATE._availability_runtime_pass("/usr/bin/podman")
    assert host_namespace == HOST_NAMESPACE
    assert "/proc/self/ns/net" in reads and "/proc/1/ns/net" not in reads


def test_an_unreadable_helper_namespace_is_an_unreadable_runtime(monkeypatch):
    containers, _processes = _fixture()
    monkeypatch.setattr(GATE, "_availability_inspect_all", lambda _deadline, _path: containers)
    _stub_namespace_reads(monkeypatch, self_error=PermissionError(13, "Permission denied"))
    assert _refusal_category(
        lambda: GATE._availability_runtime_pass("/usr/bin/podman")
    ) == "namespace_runtime_unreadable"


def test_a_publisher_of_host_port_12345_is_refused_by_the_runtime_pass(monkeypatch):
    containers, _processes = _fixture()
    containers[1]["ports"] = {"12345/tcp": [{"HostIp": "0.0.0.0", "HostPort": "12345"}]}
    monkeypatch.setattr(GATE, "_availability_inspect_all", lambda _deadline, _path: containers)
    _stub_namespace_reads(monkeypatch)
    assert _refusal_category(
        lambda: GATE._availability_runtime_pass("/usr/bin/podman")
    ) == "namespace_alloy_host_port"


def test_a_relative_podman_path_is_a_provider_refusal():
    assert _refusal_category(lambda: GATE._availability_runtime_pass("podman")) == "namespace_provider_unverified"


def test_the_candidate_port_parser_names_the_ports_decision():
    assert _refusal_category(lambda: GATE._availability_compose_port("4317")) == "namespace_candidate_ports"


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


def test_cli_prints_the_internal_error_category_for_an_unexpected_exception(monkeypatch, capsys):
    def explode(**_kwargs):
        raise RuntimeError("private detail SENTINEL-PRIVATE-TEXT /srv/example")

    assert _cli(monkeypatch, explode) == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "unverified", "reason": "namespace_internal_error"}
    assert "SENTINEL-PRIVATE-TEXT" not in output and "/srv/example" not in output
    assert BUDGET.o11y_availability_namespace_diagnostic({"rc": 2, "stdout": output}) == "namespace_internal_error"


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
        {"rc": "0", "stdout": ISOLATED},
        {"rc": "1", "stdout": ""},
        {"rc": 1.0, "stdout": ""},
        {"rc": None, "stdout": ""},
        {"rc": True, "stdout": ISOLATED},
        {"rc": True, "stdout": ""},
        {"rc": 2, "stdout": "{not json"},
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


@pytest.mark.parametrize("rc", [1, 3, 127, -9])
def test_filter_names_a_helper_that_exited_before_printing(rc):
    # An int exit code outside {0, 2} means the helper died before its own refusal handling ran.
    assert BUDGET.o11y_availability_namespace_diagnostic({"rc": rc, "stdout": ""}) == "namespace_helper_failed"
    assert BUDGET.o11y_availability_namespace_diagnostic({"rc": rc, "stdout": ISOLATED}) == "namespace_helper_failed"


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


def test_survey_imports_the_internal_name_guard_first():
    # Structural: the guard is the first import. The refusal itself, with templated forgeries, is
    # proven for every launchable playbook (this one included) by test_executor_internal_overrides.py.
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
    for category in CATEGORIES + ["namespace_helper_failed"]:
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


# ---- producer source gate and candidate checks keep the generic category --------------------

PRODUCER_ID = "a" * 64


def _producer_command(*, row=None, ids=None):
    """Bounded runtime commands for one Alloy-shaped container, with the version banners."""
    def command(argv, **_kwargs):
        if argv[-1] == "--version":
            if argv[0] == "/usr/bin/podman":
                return b"podman version 4.9.3\n"
            return b"podman-compose version 1.3.0\npodman version 4.9.3\n"
        if argv[1:2] == ["ps"]:
            return (ids if ids is not None else f"{PRODUCER_ID}\n").encode()
        if argv[1:2] == ["inspect"]:
            return row.encode()
        raise AssertionError(f"unexpected bounded runtime command: {argv[:2]}")
    return command


def _alloy_row(pid=4101, service="alloy"):
    return (
        f"{PRODUCER_ID}\tsha256:alloy\ttrue\t{pid}\t2026-10-28T00:00:00Z\to11y\t{{}}\to11y\t{service}\t/o11y-{service}\n"
    )


def _source_cli(monkeypatch, capsys):
    """Run the real source CLI; the inner collector call is the only thing replaced by its inputs."""
    monkeypatch.setattr(GATE.sys, "argv", ["summarize-o11y-scrape-baseline.py", "--verify-availability-source"])
    monkeypatch.setattr(GATE, "_availability_source_action", lambda: GATE.collect_current_availability_source(
        b'{"status":"success","data":{"activeTargets":[]}}', {}, {"enabled": False, "targets": [], "limits": {}},
        {}, environment="prod", cluster="agent-cloud-local", intended_source="alloy",
        lower_bound=None, freshness_seconds=300, require_health_targets=False,
        candidate_config_sha256=None, previous_source=None, previous_container_sha256=None,
    ))
    code = GATE.main()
    readback = json.loads(capsys.readouterr().out)
    return code, readback


@pytest.mark.parametrize("failure", ["provider", "inspect", "process_pid", "process_unreadable"])
def test_producer_refusal_from_provider_inspect_or_process_is_the_generic_source_reason(
    monkeypatch, capsys, tmp_path, failure,
):
    if failure == "provider":
        # The real provider context refuses a non-podman engine before any runtime read.
        monkeypatch.setattr(GATE.platform, "system", lambda: "Linux")
        monkeypatch.setenv("CONTAINER_ENGINE", "docker")
    else:
        _provider_mocks(monkeypatch, tmp_path)
        if failure == "inspect":
            monkeypatch.setattr(GATE, "_availability_command", _producer_command(ids="not-an-id\n"))
        elif failure == "process_pid":
            monkeypatch.setattr(GATE, "_availability_command", _producer_command(row=_alloy_row(pid=1)))
        else:
            monkeypatch.setattr(GATE, "_availability_command", _producer_command(row=_alloy_row(pid=2_000_000_000)))
    code, readback = _source_cli(monkeypatch, capsys)
    assert code == 2
    assert readback == {
        "status": "unavailable", "reason": "namespace_unverified",
        "source": "unknown", "intended_source": "unknown", "matches_intended": False,
    }
    # The filter the deploy gate uses maps the printed reason to itself, not producer_evidence_invalid.
    assert BUDGET.o11y_availability_source_gate_diagnostic(
        {"rc": code, "stdout": json.dumps(readback), "stderr": ""}
    ) == "namespace_unverified"


def test_native_only_producer_refusal_keeps_its_native_reason(monkeypatch, capsys, tmp_path):
    # No single Alloy record is a native refusal; the gate's reason set must not widen for it.
    _provider_mocks(monkeypatch, tmp_path)
    loki = f"{PRODUCER_ID}\tsha256:loki\ttrue\t4101\t2026-10-28T00:00:00Z\to11y\t{{}}\to11y\tloki\t/o11y-loki\n"
    monkeypatch.setattr(GATE, "_availability_command", _producer_command(row=loki))
    code, readback = _source_cli(monkeypatch, capsys)
    assert code == 2 and readback["reason"] == "native_source_unavailable"
    assert BUDGET.o11y_availability_source_gate_diagnostic(
        {"rc": code, "stdout": json.dumps(readback), "stderr": ""}
    ) == "native_source_unavailable"


def _candidate_cli(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(GATE.sys, "argv", [
        "summarize-o11y-scrape-baseline.py", "--verify-availability-namespace", "--candidate",
    ])
    code = GATE.main()
    return code, json.loads(capsys.readouterr().out)


def test_candidate_compose_read_failure_names_the_compose_decision(monkeypatch, capsys, tmp_path):
    _provider_mocks(monkeypatch, tmp_path)
    # The `compose config` command fails in the candidate path; the CLI names that decision point.
    for name in ("compose.prod.yml", "compose.availability.yml"):
        (tmp_path / name).write_text("services: {}\n", encoding="utf-8")

    def command(argv, **kwargs):
        if argv[-1] == "config":
            raise GATE.Refusal(kwargs["refusal"])
        return _producer_command(row=_alloy_row())(argv, **kwargs)

    monkeypatch.setattr(GATE, "_availability_command", command)
    code, result = _candidate_cli(monkeypatch, capsys, tmp_path)
    assert code == 2 and result == {"status": "unverified", "reason": "namespace_candidate_compose_unreadable"}


def test_candidate_refusal_keeps_the_category_of_its_decision_point(monkeypatch, tmp_path):
    _provider_mocks(monkeypatch, tmp_path)

    def label_refusal(*_args):
        raise GATE.Refusal("namespace_candidate_inputs_label")

    monkeypatch.setattr(GATE, "_verify_availability_effective_compose", label_refusal)
    assert _refusal_category(
        lambda: GATE.verify_availability_namespace(validate_candidate=True)
    ) == "namespace_candidate_inputs_label"


# ---- candidate effective-compose decision points (deploy.sh --candidate) -------------------
# Each row below changes one thing in a crafted `compose config` result. The base config passes
# every candidate check in enabled mode, so each refusal is the one decision point the row names.

def _candidate_config(deployment_dir):
    source = os.path.realpath(os.path.join(deployment_dir, "config", "config.alloy"))
    return {
        "name": "o11y",
        "x-podman": {"in_pod": False},
        "services": {
            "alloy": {
                "image": "docker.io/grafana/alloy:v1.5.1",
                "command": ["run", "--server.http.listen-addr=127.0.0.1:12345", "/etc/alloy/config.alloy"],
                "volumes": [{
                    "type": "bind", "source": source, "target": "/etc/alloy/config.alloy", "read_only": True,
                }],
                "ports": [{"target": 4317, "published": "4317", "protocol": "tcp", "host_ip": "127.0.0.1"}],
            },
            "prometheus": {"image": "docker.io/prom/prometheus:v3.0.0"},
        },
    }


def _candidate(monkeypatch, tmp_path, config, *, enabled=True, overlays=(), raw=None):
    """Run the real effective-compose check on one crafted `compose config` result."""
    monkeypatch.setenv("O11Y_AVAILABILITY_ENABLED", "true" if enabled else "false")
    monkeypatch.delenv("LOCAL_MODE", raising=False)
    body = raw if raw is not None else yaml.safe_dump(config).encode("utf-8")
    monkeypatch.setattr(GATE, "_availability_command", lambda _argv, **_kwargs: body)
    return GATE._verify_availability_effective_compose(str(tmp_path), ["podman-compose"], list(overlays))


def _mutated_refusal(monkeypatch, tmp_path, mutate, *, enabled=True):
    config = _candidate_config(str(tmp_path))
    mutate(config)
    return _refusal_category(lambda: _candidate(monkeypatch, tmp_path, config, enabled=enabled))


def _alloy(config):
    return config["services"]["alloy"]


def _listener(config, address):
    _alloy(config)["command"][1] = f"--server.http.listen-addr={address}"


def _ports(config, service, ports):
    config["services"][service]["ports"] = ports


def _label(config, digest, *names):
    for name in names:
        config["services"][name]["labels"] = {"io.agent-cloud.inputs-sha256": digest}


def _pod_missing_and_alloy_wrong(config):
    config.pop("x-podman")
    _listener(config, "0.0.0.0:12345")


COMPOSE_UNREADABLE_DECISIONS = [
    lambda c: c.update(services=["alloy"]),
    lambda c: c.update(services={}),
    lambda c: c.update(services={f"svc{index}": {} for index in range(33)}),
    lambda c: c["services"].update(sidecar="not-a-mapping"),
    lambda c: c.pop("name"),
    lambda c: c.update(name=""),
]
POD_DECISIONS = [
    lambda c: c.pop("x-podman"),
    lambda c: c.update({"x-podman": {"in_pod": True}}),
    _pod_missing_and_alloy_wrong,
]
ALLOY_SPEC_DECISIONS = [
    lambda c: c["services"].pop("alloy"),
    lambda c: c["services"].update(alloy="not-a-mapping"),
    lambda c: _alloy(c).update(entrypoint=["/bin/sh"]),
    lambda c: _listener(c, "0.0.0.0:12345"),
    lambda c: _alloy(c).update(command="run"),
    lambda c: _alloy(c).update(network_mode="bridge"),
    lambda c: _alloy(c).update(image="docker.io/grafana/alloy:latest"),
    lambda c: _alloy(c).update(volumes={}),
    lambda c: _alloy(c).update(volumes=[]),
    lambda c: _alloy(c)["volumes"].append(dict(_alloy(c)["volumes"][0])),
    lambda c: _alloy(c)["volumes"][0].update(type="volume"),
    lambda c: _alloy(c)["volumes"][0].update(source="/etc/passwd"),
    lambda c: _alloy(c)["volumes"][0].update(source=None),
    lambda c: _alloy(c)["volumes"][0].update(read_only=False),
    lambda c: _alloy(c)["volumes"].append(
        {"type": "bind", "source": "/tmp", "target": "/etc/alloy/extra.alloy", "read_only": True}
    ),
    lambda c: _alloy(c)["volumes"].append({"type": "volume", "target": "/etc/alloy", "read_only": True}),
    lambda c: _alloy(c)["volumes"].append({"type": "bind", "source": "/tmp", "target": "/etc", "read_only": True}),
    lambda c: _alloy(c)["volumes"].append({"type": "bind", "source": "/tmp", "target": "/etc/alloy/", "read_only": True}),
]
PORT_DECISIONS = [
    lambda c: _alloy(c).update(ports="4317:4317"),
    lambda c: _ports(c, "prometheus", [{"target": 12345, "published": "9999"}]),
    lambda c: _ports(c, "prometheus", [{"target": 9999, "published": "12345"}]),
    lambda c: _ports(c, "alloy", [{"target": 9999, "published": "4317", "protocol": "tcp", "host_ip": "127.0.0.1"}]),
    lambda c: _ports(c, "alloy", [{"target": 4317, "published": "4317", "protocol": "udp", "host_ip": "127.0.0.1"}]),
    lambda c: _ports(c, "alloy", [{"target": 4317, "published": "4317", "protocol": "tcp"}]),
    lambda c: _ports(c, "alloy", [{"target": 4317, "published": "4317", "protocol": "tcp", "host_ip": "203.0.113.10"}]),
    lambda c: _ports(c, "prometheus", ["4317"]),
    lambda c: _ports(c, "prometheus", [{"target": 4317, "published": "4317", "extra": 1}]),
    lambda c: _ports(c, "prometheus", [{"target": 9999, "published": "70000"}]),
    lambda c: _ports(c, "prometheus", [{"target": True, "published": "4317"}]),
]
NETWORK_MODE_DECISIONS = [
    lambda c: c["services"]["prometheus"].update(network_mode="host"),
    lambda c: c["services"]["prometheus"].update(network_mode="container:other"),
]


@pytest.mark.parametrize("mutate", COMPOSE_UNREADABLE_DECISIONS)
def test_candidate_compose_shape_refusals_name_compose_unreadable(monkeypatch, tmp_path, mutate):
    assert _mutated_refusal(monkeypatch, tmp_path, mutate) == "namespace_candidate_compose_unreadable"


@pytest.mark.parametrize("raw", [b"services: [\n", b"\xff\xfe\x00"])
def test_candidate_compose_output_that_is_not_yaml_or_utf8_is_unreadable(monkeypatch, tmp_path, raw):
    assert _refusal_category(
        lambda: _candidate(monkeypatch, tmp_path, _candidate_config(str(tmp_path)), raw=raw)
    ) == "namespace_candidate_compose_unreadable"


def test_candidate_missing_overlay_file_is_unreadable(monkeypatch, tmp_path):
    assert _refusal_category(
        lambda: _candidate(monkeypatch, tmp_path, _candidate_config(str(tmp_path)), overlays=["compose.absent.yml"])
    ) == "namespace_candidate_compose_unreadable"


def test_candidate_base_config_passes_every_check_in_enabled_mode(monkeypatch, tmp_path):
    assert _candidate(monkeypatch, tmp_path, _candidate_config(str(tmp_path))) == "o11y"


def test_candidate_pod_setting_is_checked_only_in_enabled_mode(monkeypatch, tmp_path):
    config = _candidate_config(str(tmp_path))
    config.pop("x-podman")
    _listener(config, "0.0.0.0:12345")
    assert _candidate(monkeypatch, tmp_path, config, enabled=False) == "o11y"


@pytest.mark.parametrize("mutate", POD_DECISIONS)
def test_candidate_pod_refusals_name_the_pod_decision(monkeypatch, tmp_path, mutate):
    assert _mutated_refusal(monkeypatch, tmp_path, mutate) == "namespace_candidate_pod"


@pytest.mark.parametrize("mutate", ALLOY_SPEC_DECISIONS)
def test_candidate_alloy_spec_refusals_name_the_alloy_spec_decision(monkeypatch, tmp_path, mutate):
    assert _mutated_refusal(monkeypatch, tmp_path, mutate) == "namespace_candidate_alloy_spec"


# ---- the shape `podman-compose config` actually emits --------------------------------------
# Captured from `podman-compose` 1.6.0 `config` over compose.yml + compose.prod.yml +
# compose.availability.yml on 2026-11-10. Unlike Docker Compose it keeps the short volume and
# port forms the compose file declares. The long-form fixture above alone hid this: the first
# real enabled deploy (Semaphore task 14401) refused with namespace_candidate_alloy_spec.
PODMAN_COMPOSE_ALLOY_VOLUMES = ["./config/config.alloy:/etc/alloy/config.alloy:ro"]
PODMAN_COMPOSE_ALLOY_PORTS = ["127.0.0.1:4317:4317", "127.0.0.1:4318:4318"]


def _podman_compose_config(deployment_dir):
    config = _candidate_config(deployment_dir)
    _alloy(config)["volumes"] = list(PODMAN_COMPOSE_ALLOY_VOLUMES)
    _alloy(config)["ports"] = list(PODMAN_COMPOSE_ALLOY_PORTS)
    return config


def test_candidate_accepts_the_short_forms_podman_compose_emits(monkeypatch, tmp_path):
    assert _candidate(monkeypatch, tmp_path, _podman_compose_config(str(tmp_path))) == "o11y"


def test_candidate_resolves_a_relative_source_against_the_deployment_dir(monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _candidate(monkeypatch, tmp_path, _podman_compose_config(str(tmp_path))) == "o11y"
    config = _candidate_config(str(tmp_path))
    _alloy(config)["volumes"][0]["source"] = "./config/config.alloy"
    assert _candidate(monkeypatch, tmp_path, config) == "o11y"


SHORT_FORM_ALLOY_SPEC_DECISIONS = [
    ["./config/config.alloy:/etc/alloy/config.alloy"],
    ["./config/config.alloy:/etc/alloy/config.alloy:rw"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro,rw"],
    ["/etc/passwd:/etc/alloy/config.alloy:ro"],
    ["alloy-config:/etc/alloy/config.alloy:ro"],
    ["/etc/alloy/config.alloy"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro:extra"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "./config/config.alloy:/etc/alloy/config.alloy:ro"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:/etc/alloy/extra.alloy:ro"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:/etc/alloy/conf.d"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", 7],
    ["./config/config.alloy::ro"],
    # Shadowing or ambiguous targets: a mount at /etc/alloy or an ancestor, or a relative target.
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/etc/alloy:ro"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/etc/alloy"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:/etc/alloy"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:/etc:ro"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:/:ro"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:/etc/alloy/../alloy/x:ro"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:/etc//alloy:ro"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:/etc/./alloy:ro"],
    ["./config/config.alloy:/etc/alloy/config.alloy:ro", "/tmp:etc/alloy:ro"],
    ["./config/config.alloy:etc/alloy/config.alloy:ro"],
]


@pytest.mark.parametrize("volumes", SHORT_FORM_ALLOY_SPEC_DECISIONS)
def test_candidate_short_form_mount_refusals_name_the_alloy_spec_decision(monkeypatch, tmp_path, volumes):
    config = _podman_compose_config(str(tmp_path))
    _alloy(config)["volumes"] = volumes
    assert _refusal_category(lambda: _candidate(monkeypatch, tmp_path, config)) == "namespace_candidate_alloy_spec"


@pytest.mark.parametrize("mutate", PORT_DECISIONS)
def test_candidate_port_refusals_name_the_ports_decision(monkeypatch, tmp_path, mutate):
    assert _mutated_refusal(monkeypatch, tmp_path, mutate) == "namespace_candidate_ports"


@pytest.mark.parametrize("mutate", NETWORK_MODE_DECISIONS)
def test_candidate_network_mode_on_another_service_keeps_the_network_mode_category(monkeypatch, tmp_path, mutate):
    assert _mutated_refusal(monkeypatch, tmp_path, mutate) == "namespace_network_mode"


def test_candidate_service_entry_that_is_not_a_mapping_is_unreadable_under_a_label_overlay(
    monkeypatch, tmp_path, inputs_label_overlay,
):
    digest = "a" * 64
    path = inputs_label_overlay(_label_overlay("alloy", "prometheus", digest=digest))
    config = _candidate_config(str(tmp_path))
    _label(config, digest, "alloy")
    config["services"]["prometheus"] = "not-a-mapping"
    assert _refusal_category(
        lambda: _candidate(monkeypatch, tmp_path, config, overlays=[path])
    ) == "namespace_candidate_compose_unreadable"


# ---- final-label overlay (inputs-label.yml) ------------------------------------------------

@pytest.fixture
def inputs_label_overlay():
    """Write one overlay where the candidate validator accepts it: inputs-label.yml, in an owner-only
    directory that sits directly under the real temporary directory. Cleanup removes only these two
    entries, one plain delete each."""
    made = []

    def write(text):
        directory = os.path.realpath(tempfile.mkdtemp(prefix="o11y-gate-test-", dir=tempfile.gettempdir()))
        made.append(directory)
        path = os.path.join(directory, "inputs-label.yml")
        Path(path).write_text(text, encoding="utf-8")
        made.append(path)
        return path

    yield write
    for entry in reversed(made):
        if os.path.isdir(entry):
            os.rmdir(entry)
        else:
            os.remove(entry)


def _label_overlay(*names, digest="a" * 64):
    return yaml.safe_dump({"services": {
        name: {"labels": {"io.agent-cloud.inputs-sha256": digest}} for name in names
    }})


def test_candidate_label_overlay_that_matches_the_effective_labels_is_accepted(
    monkeypatch, tmp_path, inputs_label_overlay,
):
    digest = "a" * 64
    path = inputs_label_overlay(_label_overlay("alloy", "prometheus", digest=digest))
    config = _candidate_config(str(tmp_path))
    _label(config, digest, "alloy", "prometheus")
    assert _candidate(monkeypatch, tmp_path, config, overlays=[path]) == "o11y"


def test_candidate_two_final_label_overlays_name_inputs_label(monkeypatch, tmp_path):
    first, second = tmp_path / "first.yml", tmp_path / "second.yml"
    first.write_text("services: {}\n", encoding="utf-8")
    second.write_text("services: {}\n", encoding="utf-8")
    assert _refusal_category(
        lambda: _candidate(monkeypatch, tmp_path, _candidate_config(str(tmp_path)),
                           overlays=[str(first), str(second)])
    ) == "namespace_candidate_inputs_label"


def test_candidate_final_overlay_at_an_invalid_path_names_inputs_label(monkeypatch, tmp_path):
    other = tmp_path / "other.yml"
    other.write_text(_label_overlay("alloy", "prometheus"), encoding="utf-8")
    assert _refusal_category(
        lambda: _candidate(monkeypatch, tmp_path, _candidate_config(str(tmp_path)), overlays=[str(other)])
    ) == "namespace_candidate_inputs_label"


def test_candidate_invalid_label_overlay_content_names_inputs_label(monkeypatch, tmp_path, inputs_label_overlay):
    path = inputs_label_overlay("services: {}\n")
    assert _refusal_category(
        lambda: _candidate(monkeypatch, tmp_path, _candidate_config(str(tmp_path)), overlays=[path])
    ) == "namespace_candidate_inputs_label"


def test_candidate_label_service_set_that_differs_from_services_names_inputs_label(
    monkeypatch, tmp_path, inputs_label_overlay,
):
    digest = "a" * 64
    path = inputs_label_overlay(_label_overlay("alloy", "prometheus", "extra", digest=digest))
    config = _candidate_config(str(tmp_path))
    _label(config, digest, "alloy", "prometheus")
    assert _refusal_category(
        lambda: _candidate(monkeypatch, tmp_path, config, overlays=[path])
    ) == "namespace_candidate_inputs_label"


def test_candidate_inputs_sha256_label_mismatch_names_inputs_label(monkeypatch, tmp_path, inputs_label_overlay):
    path = inputs_label_overlay(_label_overlay("alloy", "prometheus", digest="a" * 64))
    config = _candidate_config(str(tmp_path))
    _label(config, "b" * 64, "alloy", "prometheus")
    assert _refusal_category(
        lambda: _candidate(monkeypatch, tmp_path, config, overlays=[path])
    ) == "namespace_candidate_inputs_label"


# ---- running Alloy project versus the candidate project -------------------------------------

def test_candidate_project_that_differs_from_the_running_alloy_names_the_project(monkeypatch, tmp_path):
    _verify_context(monkeypatch)
    monkeypatch.setenv("O11Y_DEPLOYMENT_DIR", str(tmp_path))
    containers, processes = _fixture()
    monkeypatch.setattr(GATE, "_availability_runtime_pass", lambda _path: _passes(containers, processes))
    monkeypatch.setattr(GATE, "_verify_availability_effective_compose", lambda *_args: "other-project")
    assert _refusal_category(
        lambda: GATE.verify_availability_namespace(validate_candidate=True)
    ) == "namespace_candidate_project"


# ---- producer source gate folds every namespace word --------------------------------------

@pytest.mark.parametrize("word", CANDIDATE_CATEGORIES)
def test_producer_source_gate_folds_each_candidate_word_to_the_generic_reason(monkeypatch, capsys, word):
    def refuse():
        raise GATE.Refusal(word)

    monkeypatch.setattr(GATE, "_availability_provider_context", refuse)
    code, readback = _source_cli(monkeypatch, capsys)
    assert code == 2
    assert readback == {
        "status": "unavailable", "reason": "namespace_unverified",
        "source": "unknown", "intended_source": "unknown", "matches_intended": False,
    }
