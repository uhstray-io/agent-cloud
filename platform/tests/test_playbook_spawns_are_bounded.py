"""Every ansible-playbook a test starts is bounded, and new ones go through the shared runner.

Plan 21 phase 3. A child that hangs holds its pytest worker until the CI job's 30-minute limit,
and a test that starts ansible-playbook with its own subprocess call is one more runner to keep
in step (ANSIBLE_CONFIG, temp directories, the sandbox). harness_sandbox.run_playbook bounds every
run (120 s by default), gives the child no stdin and runs it from the repository root.

The lists below are exact: a file that starts ansible-playbook itself must be on DIRECT, and a
file that migrates must come off it. They are meant only to shrink. The test cannot see the base
branch, so a pull request that adds an entry passes here; adding one is the change review refuses.

- DIRECT: files that start ansible-playbook with their own subprocess call.
- UNBOUNDED: those with an ansible-playbook spawn that passes no timeout.
- UNBOUNDED_DYNAMIC: files with an unbounded spawn whose program the scan cannot name (a function
  parameter, say). It may be ansible-playbook, and it can hang a worker whatever it is.
"""

import ast

import spawn_scan

DIRECT = frozenset({
    "platform/semaphore/tests/test_scoped_publication.py",
    "platform/services/netbox/deployment/tests/test_discovery_diagnostics.py",
    "platform/tests/seed_harness.py",
    "platform/tests/test_access_executors_step_result.py",
    "platform/tests/test_agw_internal_dns_records.py",
    "platform/tests/test_agw_verify_json_schema.py",
    "platform/tests/test_assert_containers_stable.py",
    "platform/tests/test_assert_node_cpu_model.py",
    "platform/tests/test_authentik_audit_recovery.py",
    "platform/tests/test_authentik_console_directory.py",
    "platform/tests/test_authentik_platform_admins.py",
    "platform/tests/test_authentik_retired_accounts.py",
    "platform/tests/test_check_o11y_liveness.py",
    "platform/tests/test_collector_host_optout.py",
    "platform/tests/test_configure_pbs_playbook.py",
    "platform/tests/test_connection_identity_vars.py",
    "platform/tests/test_console_grafana_token.py",
    "platform/tests/test_ensure_netbox_business_domains.py",
    "platform/tests/test_executor_internal_overrides.py",
    "platform/tests/test_extra_var_refusal_tests.py",
    "platform/tests/test_instrument_host_o11y.py",
    "platform/tests/test_local_semaphore_config_persistence.py",
    "platform/tests/test_local_semaphore_port.py",
    "platform/tests/test_manage_semaphore_access.py",
    "platform/tests/test_migrate_vm.py",
    "platform/tests/test_no_template_delimiters_in_conditionals.py",
    "platform/tests/test_o11y_availability_namespace_survey.py",
    "platform/tests/test_o11y_backup_job.py",
    "platform/tests/test_o11y_budget_daily_ingestion.py",
    "platform/tests/test_o11y_budget_inference_peak.py",
    "platform/tests/test_o11y_budget_receipt_overrides.py",
    "platform/tests/test_o11y_canary_gateway_tls.py",
    "platform/tests/test_o11y_capacity_history.py",
    "platform/tests/test_o11y_coverage_contract.py",
    "platform/tests/test_o11y_endpoint_drill.py",
    "platform/tests/test_o11y_fault_drill.py",
    "platform/tests/test_o11y_scrape_budgets.py",
    "platform/tests/test_pbs_backup_access.py",
    "platform/tests/test_plane_declared_projects.py",
    "platform/tests/test_plane_env_github.py",
    "platform/tests/test_plane_link_github_repos.py",
    "platform/tests/test_prepare_pbs_guest_data_disk.py",
    "platform/tests/test_probe_console_sources.py",
    "platform/tests/test_promote_semaphore_admins.py",
    "platform/tests/test_provision_o11y_mcp_grafana_token.py",
    "platform/tests/test_provision_o11y_watcher_token.py",
    "platform/tests/test_provision_seed_replace_auth.py",
    "platform/tests/test_proxmox_validate_cpu_levels.py",
    "platform/tests/test_push_loki_lines.py",
    "platform/tests/test_read_proxmox_root_ca.py",
    "platform/tests/test_reconcile_netbox_duplicate_vms.py",
    "platform/tests/test_reconcile_plane_members_scheduled.py",
    "platform/tests/test_redact_requests_callback.py",
    "platform/tests/test_resize_vm_agent.py",
    "platform/tests/test_resize_vm_cpu.py",
    "platform/tests/test_retire_openbao_fields.py",
    "platform/tests/test_semaphore_inventory_sync.py",
    "platform/tests/test_semaphore_token_rotation.py",
    "platform/tests/test_survey_o11y_alloy_health.py",
    "platform/tests/test_workflow_credential_boundaries.py",
    "platform/tests/test_workflow_registry.py",
})

UNBOUNDED = frozenset()

UNBOUNDED_DYNAMIC = frozenset({
    "platform/tests/test_semaphore_inventory_sync.py",
})


def test_no_new_test_starts_ansible_playbook_itself():
    direct, _, _ = spawn_scan.scan()
    assert not direct - DIRECT, (
        f"start the playbook with harness_sandbox.run_playbook instead: {sorted(direct - DIRECT)}")
    assert not DIRECT - direct, f"migrated: take these off DIRECT: {sorted(DIRECT - direct)}"


def test_no_new_unbounded_spawn():
    _, unbounded, dynamic = spawn_scan.scan()
    assert not unbounded - UNBOUNDED, f"pass timeout=, or use run_playbook: {sorted(unbounded - UNBOUNDED)}"
    assert not UNBOUNDED - unbounded, f"bounded now: take these off UNBOUNDED: {sorted(UNBOUNDED - unbounded)}"
    assert not dynamic - UNBOUNDED_DYNAMIC, f"pass timeout=: {sorted(dynamic - UNBOUNDED_DYNAMIC)}"
    assert not UNBOUNDED_DYNAMIC - dynamic, (
        f"bounded now: take these off UNBOUNDED_DYNAMIC: {sorted(UNBOUNDED_DYNAMIC - dynamic)}")
    assert UNBOUNDED <= DIRECT


def _kinds(source, skip=()):
    return [(kind, bounded) for kind, bounded, _ in spawn_scan.spawns(ast.parse(source), skip)]


PB, DYN = spawn_scan.PLAYBOOK, spawn_scan.DYNAMIC


def test_the_scan_names_each_way_a_test_starts_ansible_playbook():
    cases = {
        # The literal, in each spawn form, with and without a timeout.
        **{f"import subprocess\ndef f(p):\n    subprocess.{form}(['ansible-playbook', p])\n": [(PB, False)]
           for form in sorted(spawn_scan.SPAWNERS)},
        "import subprocess\ndef f(p):\n    subprocess.run(['ansible-playbook', p], timeout=5)\n": [(PB, True)],
        "import subprocess\ndef f(p):\n    subprocess.run(['ansible-playbook', p], timeout=None)\n": [(PB, False)],
        "import subprocess\ndef f(p, **kw):\n    subprocess.run(['ansible-playbook', p], **kw)\n": [(PB, False)],
        # A module constant from shutil.which (test_survey_o11y_alloy_health.py).
        "import shutil, subprocess\nPB = shutil.which('ansible-playbook')\n"
        "def f(p):\n    subprocess.run([PB, '-i', 'x,', p])\n": [(PB, False)],
        # A command list built in a variable, and one extended.
        "import subprocess\ndef f(p):\n    cmd = ['ansible-playbook', p]\n    cmd += ['-v']\n"
        "    subprocess.run(cmd)\n": [(PB, False)],
        # A closure over the enclosing function's binary (test_discovery_diagnostics.py).
        "import shutil, subprocess\ndef f(p):\n    a = shutil.which('ansible-playbook')\n"
        "    def g():\n        return subprocess.run([a, p], timeout=30)\n    return g()\n": [(PB, True)],
        # A repository helper that resolves the binary, and a path join.
        "import subprocess\ndef f(p):\n    subprocess.run([ansible_bin('ansible-playbook'), p])\n": [(PB, False)],
        "import subprocess\ndef f(d, p):\n    subprocess.run([d / 'ansible-playbook', p])\n": [(PB, False)],
        # A shell string, an f-string and an absolute path.
        "import subprocess\ndef f(p):\n    subprocess.run(f'ansible-playbook -i x, {p}', shell=True)\n": [(PB, False)],
        "import subprocess\ndef f(p):\n    subprocess.run(['/usr/bin/ansible-playbook', p])\n": [(PB, False)],
        # However subprocess was imported.
        "import subprocess as sp\ndef f(p):\n    sp.run(['ansible-playbook', p])\n": [(PB, False)],
        "from subprocess import run\ndef f(p):\n    run(['ansible-playbook', p])\n": [(PB, False)],
        "from subprocess import run as go\ndef f(p):\n    go(['ansible-playbook', p])\n": [(PB, False)],
    }
    for source, expected in cases.items():
        assert _kinds(source) == expected, source


def test_the_scan_does_not_count_other_programs_or_decorators():
    # A decorator naming ansible-playbook does not make the function's git call a playbook spawn.
    skipif = ("import shutil, subprocess, pytest\n"
              "@pytest.mark.skipif(not shutil.which('ansible-playbook'), reason='x')\n"
              "def test_f():\n    subprocess.run(['git', 'status'])\n")
    assert _kinds(skipif) == [("git", False)]
    # A Popen is bounded by a wait or communicate with a timeout in the same function, and only so.
    popen = "import subprocess\ndef f(p):\n    proc = subprocess.Popen(['ansible-playbook', p])\n    proc.{}\n"
    assert _kinds(popen.format("wait(timeout=5)")) == [(PB, True)]
    assert _kinds(popen.format("communicate(timeout=5)")) == [(PB, True)]
    assert _kinds(popen.format("wait()")) == [(PB, False)]
    assert _kinds(popen.format("wait(timeout=None)")) == [(PB, False)]
    # The wait must be on the process just started, not on any other object.
    other = ("import subprocess, threading\ndef f(p):\n    proc = subprocess.Popen(['ansible-playbook', p])\n"
             "    threading.Event().wait(timeout=5)\n")
    assert _kinds(other) == [(PB, False)]
    returned = "import subprocess\ndef f(p):\n    return subprocess.Popen(['ansible-playbook', p])\n"
    assert _kinds(returned) == [(PB, False)]  # its caller's wait is not visible here
    # A program passed in cannot be named: it is DYNAMIC, and it must be bounded too.
    assert _kinds("import subprocess\ndef f(binary, p):\n    subprocess.run([binary, p])\n") == [(DYN, False)]
    # The shared runner is the one spawner the scan skips, by function name only.
    runner = "import subprocess\ndef run(c):\n    subprocess.run(c)\ndef other(c):\n    subprocess.run(c)\n"
    assert _kinds(runner, skip={"run"}) == [(DYN, False)]
    assert _kinds("def f(p, t):\n    return harness_sandbox.run_playbook(p, t)\n") == []
