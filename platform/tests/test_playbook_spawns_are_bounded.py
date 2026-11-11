"""Every ansible-playbook a test starts is bounded, and new ones go through the shared runner.

Plan 21 phase 3. A playbook that hangs holds its pytest worker until the CI job's 30-minute limit,
and a test that starts ansible-playbook with its own subprocess call is one more runner to keep
in step (ANSIBLE_CONFIG, temp directories, the sandbox). harness_sandbox.run_playbook bounds every
run (120 s by default), gives the child no stdin and runs it from the repository root.

Both lists below may only shrink. A file that starts ansible-playbook directly must already be
on DIRECT; a file migrated to run_playbook must be taken off it. UNBOUNDED holds the files with a
direct spawn that passes no timeout; it is a subset of DIRECT and empties as files migrate.
"""

import ast

import spawn_scan

DIRECT = frozenset({
    "platform/semaphore/tests/test_scoped_publication.py",
    "platform/services/netbox/deployment/tests/test_discovery_diagnostics.py",
    "platform/tests/seed_harness.py",
    "platform/tests/test_access_executors_step_result.py",
    "platform/tests/test_agentgateway_secret_defs.py",
    "platform/tests/test_agw_internal_dns_records.py",
    "platform/tests/test_agw_verify_json_schema.py",
    "platform/tests/test_assert_containers_stable.py",
    "platform/tests/test_assert_node_cpu_model.py",
    "platform/tests/test_authentik_audit_recovery.py",
    "platform/tests/test_authentik_console_directory.py",
    "platform/tests/test_authentik_platform_admins.py",
    "platform/tests/test_authentik_retired_accounts.py",
    "platform/tests/test_bao_merge_keys_absent_guard.py",
    "platform/tests/test_bao_merge_keys_dry_run.py",
    "platform/tests/test_check_o11y_liveness.py",
    "platform/tests/test_check_secrets_verdict.py",
    "platform/tests/test_cloudflare_dry_run_backend.py",
    "platform/tests/test_collector_host_optout.py",
    "platform/tests/test_configure_pbs_playbook.py",
    "platform/tests/test_connection_identity_vars.py",
    "platform/tests/test_console_access_role_map.py",
    "platform/tests/test_console_catalog.py",
    "platform/tests/test_console_grafana_token.py",
    "platform/tests/test_console_phase3_creds.py",
    "platform/tests/test_edge_dns_step_result.py",
    "platform/tests/test_edge_route_group_preflight.py",
    "platform/tests/test_emit_step_result.py",
    "platform/tests/test_ensure_netbox_business_domains.py",
    "platform/tests/test_executor_internal_overrides.py",
    "platform/tests/test_extra_var_refusal_tests.py",
    "platform/tests/test_harden_check_mode_verdict.py",
    "platform/tests/test_harden_proves_key_first.py",
    "platform/tests/test_inference_probe.py",
    "platform/tests/test_inspect_authentik_blueprints.py",
    "platform/tests/test_instrument_host_o11y.py",
    "platform/tests/test_local_semaphore_config_persistence.py",
    "platform/tests/test_local_semaphore_port.py",
    "platform/tests/test_manage_semaphore_access.py",
    "platform/tests/test_migrate_vm.py",
    "platform/tests/test_netbox_deploy_gate.py",
    "platform/tests/test_netbox_dhcp_boundary.py",
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
    "platform/tests/test_o11y_journal_collector.py",
    "platform/tests/test_o11y_scrape_budgets.py",
    "platform/tests/test_o11y_verify_log_signal.py",
    "platform/tests/test_pbs_backup_access.py",
    "platform/tests/test_persistence_group_verdict.py",
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
    "platform/tests/test_pve_token_id_required.py",
    "platform/tests/test_read_proxmox_root_ca.py",
    "platform/tests/test_reconcile_netbox_duplicate_vms.py",
    "platform/tests/test_reconcile_plane_members_scheduled.py",
    "platform/tests/test_redact_requests_callback.py",
    "platform/tests/test_resize_vm_agent.py",
    "platform/tests/test_resize_vm_cpu.py",
    "platform/tests/test_retire_openbao_fields.py",
    "platform/tests/test_semaphore_inventory_sync.py",
    "platform/tests/test_semaphore_token_rotation.py",
    "platform/tests/test_service_deploy_step_result.py",
    "platform/tests/test_service_executor_step_results.py",
    "platform/tests/test_service_persistence_podman.py",
    "platform/tests/test_service_runtime_inspection.py",
    "platform/tests/test_snapshot_service_assessment_guard.py",
    "platform/tests/test_snapshot_vm_check_mode.py",
    "platform/tests/test_validate_address_placement.py",
    "platform/tests/test_verify_o11y_dashboard_data.py",
    "platform/tests/test_verify_service_health.py",
    "platform/tests/test_workflow_credential_boundaries.py",
    "platform/tests/test_workflow_registry.py",
})

UNBOUNDED = frozenset({
    "platform/services/netbox/deployment/tests/test_discovery_diagnostics.py",
    "platform/tests/test_access_executors_step_result.py",
    "platform/tests/test_agentgateway_secret_defs.py",
    "platform/tests/test_agw_internal_dns_records.py",
    "platform/tests/test_authentik_audit_recovery.py",
    "platform/tests/test_authentik_console_directory.py",
    "platform/tests/test_authentik_platform_admins.py",
    "platform/tests/test_authentik_retired_accounts.py",
    "platform/tests/test_bao_merge_keys_absent_guard.py",
    "platform/tests/test_bao_merge_keys_dry_run.py",
    "platform/tests/test_check_secrets_verdict.py",
    "platform/tests/test_cloudflare_dry_run_backend.py",
    "platform/tests/test_collector_host_optout.py",
    "platform/tests/test_console_access_role_map.py",
    "platform/tests/test_console_catalog.py",
    "platform/tests/test_console_phase3_creds.py",
    "platform/tests/test_edge_dns_step_result.py",
    "platform/tests/test_edge_route_group_preflight.py",
    "platform/tests/test_emit_step_result.py",
    "platform/tests/test_harden_check_mode_verdict.py",
    "platform/tests/test_harden_proves_key_first.py",
    "platform/tests/test_inference_probe.py",
    "platform/tests/test_inspect_authentik_blueprints.py",
    "platform/tests/test_instrument_host_o11y.py",
    "platform/tests/test_netbox_deploy_gate.py",
    "platform/tests/test_netbox_dhcp_boundary.py",
    "platform/tests/test_no_template_delimiters_in_conditionals.py",
    "platform/tests/test_o11y_canary_gateway_tls.py",
    "platform/tests/test_o11y_coverage_contract.py",
    "platform/tests/test_o11y_fault_drill.py",
    "platform/tests/test_o11y_journal_collector.py",
    "platform/tests/test_o11y_scrape_budgets.py",
    "platform/tests/test_o11y_verify_log_signal.py",
    "platform/tests/test_persistence_group_verdict.py",
    "platform/tests/test_pve_token_id_required.py",
    "platform/tests/test_semaphore_inventory_sync.py",
    "platform/tests/test_service_deploy_step_result.py",
    "platform/tests/test_service_executor_step_results.py",
    "platform/tests/test_service_persistence_podman.py",
    "platform/tests/test_service_runtime_inspection.py",
    "platform/tests/test_snapshot_service_assessment_guard.py",
    "platform/tests/test_snapshot_vm_check_mode.py",
    "platform/tests/test_validate_address_placement.py",
    "platform/tests/test_verify_o11y_dashboard_data.py",
    "platform/tests/test_verify_service_health.py",
    "platform/tests/test_workflow_credential_boundaries.py",
})


def test_no_new_test_starts_ansible_playbook_itself():
    direct, _ = spawn_scan.scan()
    assert not direct - DIRECT, (
        f"start the playbook with harness_sandbox.run_playbook instead: {sorted(direct - DIRECT)}")
    assert not DIRECT - direct, f"migrated: take these off DIRECT: {sorted(DIRECT - direct)}"


def test_no_new_unbounded_ansible_playbook_spawn():
    _, unbounded = spawn_scan.scan()
    assert not unbounded - UNBOUNDED, f"pass timeout=, or use run_playbook: {sorted(unbounded - UNBOUNDED)}"
    assert not UNBOUNDED - unbounded, f"bounded now: take these off UNBOUNDED: {sorted(UNBOUNDED - unbounded)}"
    assert UNBOUNDED <= DIRECT


def _spawns_of(source):
    return list(spawn_scan._spawns(ast.parse(source)))


def test_the_scan_sees_each_spawn_form_and_its_timeout():
    play = 'def _run(p):\n    return subprocess.{}(["ansible-playbook", p]{})\n'
    for form in sorted(spawn_scan.SPAWNERS):
        assert [b for _, b in _spawns_of(play.format(form, ""))] == [False], form
        assert [b for _, b in _spawns_of(play.format(form, ", timeout=5"))] == [True], form
    # A command built in a variable still counts: the function names ansible-playbook.
    built = 'def _run(p):\n    cmd = ["ansible-playbook", p]\n    return subprocess.run(cmd)\n'
    assert len(_spawns_of(built)) == 1
    # The shared runner and code that never names ansible-playbook are not spawns.
    assert _spawns_of('def _run(p, t):\n    return harness_sandbox.run_playbook(p, t)\n') == []
    assert _spawns_of('def _git():\n    return subprocess.run(["git", "status"])\n') == []
