# Workflow agent rules (change service-deployment-workflow, platform/agent-orchestration and
# platform/semaphore-environments). Addresses are RFC 5737 documentation ranges.
package agentcloud_workflow_test

import rego.v1

import data.agentcloud

# A workflow run on dev launches the template's generated " (Dev)" variant, the only kind bound
# to the dev repository record; a test that names a suffixed template keeps its own suffix.
_dev_name(t) := t if endswith(t, " (Dev)")

_dev_name(t) := t if endswith(t, " (Local)")

_dev_name(t) := concat("", [t, " (Dev)"]) if {
	not endswith(t, " (Dev)")
	not endswith(t, " (Local)")
}

_run(agent, template, step) := {
	"agent": agent,
	"service": "semaphore",
	"action": "run_task",
	"template_name": _dev_name(template),
	"step": step,
	"git_branch": "dev",
}

_ctx := {
	"controller_cidr": "192.0.2.10/32",
	"ssh_cidrs": ["192.0.2.10/32", "198.51.100.0/24"],
	"tier_bounds": {"cores": 4, "memory_mb": 8192, "disk_gb": 64},
}

_fw(allow_rules) := object.union(_run("security-agent", "Apply Firewall", "fw-harden"), {
	"proposal": {"allow": allow_rules, "deny_egress": [], "verdict": {"decision": "converge", "findings": []}},
	"context": _ctx,
})

_acc(mode, groups) := object.union(_run("security-agent", "Deploy Authentik", "oidc-config"), {
	"proposal": {
		"auth_mode": mode, "authentik_app": {"slug": "svc", "groups": groups},
		"openbao_policy_paths": [], "opa_actions": {}, "findings": [],
		"verdict": {"decision": "converge", "findings": []},
	},
	"context": {"auth_mode": "oidc", "groups": ["platform-admins", "platform-developers"]},
})

_ssh_from(src) := {"port": 22, "proto": "tcp", "source": src, "consumer": "semaphore", "justification": "orchestration"}

_svc(spec, template) := object.union(_run("service-agent", "Deploy Postiz", "service-deploy"), {
	"proposal": {"vm_spec": spec, "deploy_template": template, "verdict": {"decision": "converge", "findings": []}},
	"context": _ctx,
})

# Scenario "Role launches only its own steps"
test_role_denied_outside_its_allowlist if {
	d := agentcloud.decision with input as _run("o11y-agent", "Harden SSH", "access-harden")
	not d.allowed
	contains(d.reason, "template is not on this agent's allowed_templates")
}

test_role_allowed_on_its_own_step if {
	agentcloud.allow with input as _run("security-agent", "Harden SSH", "access-harden")
}

test_generated_dev_suffix_matches_base_name if {
	agentcloud.allow with input as _run("security-agent", "Harden SSH (Dev)", "access-harden")
}

# The launch gate adds `settings`, the exact survey values it will launch (change
# skynet-launch-containment, design decision 6). Only target_service is read (agent_actions.rego,
# "Agent launches"); the other keys change no verdict.
test_launch_gate_settings_field_keeps_an_allow if {
	agentcloud.allow with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"settings": {"target_service": "x_svc"}})
}

test_launch_gate_settings_field_keeps_a_deny if {
	d := agentcloud.decision with input as object.union(_run("o11y-agent", "Harden SSH", "access-harden"), {"settings": {"target_service": "x_svc"}})
	not d.allowed
}

# target_service names one <name>_svc group for a hosts: pattern (Harden SSH), or one bare service
# name for the templates that append _svc themselves (Provision VM and the others). Each bad value is its own
# test, so a failure names the value that got through; the non-string values guard the type check.
_target_reason := "target_service must name one service group, or one service for the templates that take a bare name"

_gate_input(target) := object.union(_run("security-agent", "Harden SSH", "access-harden"), {"settings": {"target_service": target}})

test_launch_gate_target_service_one_group_keeps_an_allow if {
	agentcloud.allow with input as _gate_input("harden_svc")
}

test_launch_gate_target_service_all_is_denied if {
	d := agentcloud.decision with input as _gate_input("all")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_star_is_denied if {
	d := agentcloud.decision with input as _gate_input("*")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_ungrouped_is_denied if {
	d := agentcloud.decision with input as _gate_input("ungrouped")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_colon_pattern_is_denied if {
	d := agentcloud.decision with input as _gate_input("harden_svc:other_svc")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_negation_is_denied if {
	d := agentcloud.decision with input as _gate_input("!x_svc")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_comma_list_is_denied if {
	d := agentcloud.decision with input as _gate_input("harden_svc,other_svc")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_glob_is_denied if {
	d := agentcloud.decision with input as _gate_input("web*_svc")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_uppercase_is_denied if {
	d := agentcloud.decision with input as _gate_input("HARDEN_svc")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_empty_is_denied if {
	d := agentcloud.decision with input as _gate_input("")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_null_is_denied if {
	d := agentcloud.decision with input as _gate_input(null)
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_number_is_denied if {
	d := agentcloud.decision with input as _gate_input(1)
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_array_is_denied if {
	d := agentcloud.decision with input as _gate_input(["harden_svc"])
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_false_is_denied if {
	d := agentcloud.decision with input as _gate_input(false)
	not d.allowed
	contains(d.reason, _target_reason)
}

# A `settings` block without the key is outside the rule: the other survey values change nothing.
# The controller and a parent group above the service groups are plain identifiers too, and each
# widens a hosts: pattern; a bare service name is not a group at all.
test_launch_gate_target_service_localhost_is_denied if {
	d := agentcloud.decision with input as _gate_input("localhost")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_parent_group_is_denied if {
	d := agentcloud.decision with input as _gate_input("servers")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_target_service_bare_name_is_denied_for_a_hosts_pattern if {
	d := agentcloud.decision with input as _gate_input("postiz")
	not d.allowed
	contains(d.reason, _target_reason)
}

test_launch_gate_settings_without_target_service_keeps_an_allow if {
	agentcloud.allow with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"settings": {"smoke": "1"}})
}

# The launch gate's probe (Probe launch-gate (Dev), playbooks/probe-launch-gate.yml) sends this input shape:
# agent service-agent, template Snapshot Service Assessment (Dev) on dev, the registry step service-assess, the
# survey values as settings and human_approved false. The step must be a registry name: "probe" is not one, and
# an unregistered step is denied, so the probe names service-assess.
_gate_probe(step) := object.union(_run("service-agent", "Snapshot Service Assessment", step), {
	"human_approved": false,
	"settings": {"target_service": "opa_svc"},
})

test_launch_gate_probe_registry_step_is_allowed if {
	agentcloud.allow with input as _gate_probe("service-assess")
}

test_launch_gate_probe_unregistered_step_is_denied if {
	d := agentcloud.decision with input as _gate_probe("probe")
	not d.allowed
	contains(d.reason, "a workflow task must name its registry step")
}

# Bare service names: these templates look the declaration up with groups[target_service ~ '_svc'],
# so the value they take carries no suffix. Each is allowed (the gate must not refuse them).
_vm_run(template, step, settings) := object.union(_run("infra-agent", template, step), {"settings": settings})

test_provision_vm_bare_service_name_keeps_an_allow if {
	agentcloud.allow with input as _vm_run("Provision VM", "provision-vm", {"target_service": "postiz"})
}

test_provision_vm_pbs_special_value_keeps_an_allow if {
	agentcloud.allow with input as _vm_run("Provision VM", "provision-vm", {"target_service": "pbs"})
}

test_provision_vm_target_service_localhost_is_denied if {
	d := agentcloud.decision with input as _vm_run("Provision VM", "provision-vm", {"target_service": "localhost"})
	not d.allowed
	contains(d.reason, _target_reason)
}

test_provision_vm_dev_variant_keeps_the_bare_name_rule if {
	agentcloud.allow with input as _vm_run("Provision VM (Dev)", "provision-vm", {"target_service": "postiz"})
}

test_provision_vm_bare_name_with_target_host_keeps_an_allow if {
	agentcloud.allow with input as _vm_run("Provision VM", "provision-vm", {"target_service": "postiz", "target_host": "postiz-01"})
}

test_provision_vm_local_host_name_keeps_an_allow if {
	agentcloud.allow with input as _vm_run("Provision VM", "provision-vm", {"target_service": "postiz", "target_host": "opa-local"})
}

test_resize_vm_bare_service_name_keeps_an_allow if {
	agentcloud.allow with input as object.union(_vm_run("Resize VM", "vm-rightsize", {"target_service": "postiz"}), {
		"proposal": {"vm_spec": {"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "deploy_template": "Deploy Postiz", "verdict": {"decision": "converge", "findings": []}},
		"context": _ctx,
	})
}

test_lookup_service_inventory_bare_service_name_keeps_an_allow if {
	agentcloud.allow with input as _vm_run("Lookup Service Inventory", "lookup-inventory", {"target_service": "postiz"})
}

test_validate_address_free_bare_service_name_keeps_an_allow if {
	agentcloud.allow with input as _vm_run("Validate Address Free", "validate-address", {"target_service": "postiz"})
}

# An underscore is not Ansible pattern syntax, and inventories hold host names that use it.
test_provision_vm_target_host_with_underscore_keeps_an_allow if {
	agentcloud.allow with input as _vm_run("Provision VM", "provision-vm", {"target_service": "postiz", "target_host": "postiz_01"})
}

test_target_host_localhost_is_denied if {
	d := agentcloud.decision with input as _host_input("localhost")
	not d.allowed
	contains(d.reason, _host_reason)
}

test_target_host_negation_is_denied if {
	d := agentcloud.decision with input as _host_input("!postiz-01")
	not d.allowed
	contains(d.reason, _host_reason)
}

# target_host (Provision VM selects one declared host with it) is one plain host name.
_host_reason := "target_host must be one plain host name"

_host_input(host) := _vm_run("Provision VM", "provision-vm", {"target_service": "postiz", "target_host": host})

test_target_host_all_is_denied if {
	d := agentcloud.decision with input as _host_input("all")
	not d.allowed
	contains(d.reason, _host_reason)
}

test_target_host_ungrouped_is_denied if {
	d := agentcloud.decision with input as _host_input("ungrouped")
	not d.allowed
	contains(d.reason, _host_reason)
}

test_target_host_glob_is_denied if {
	d := agentcloud.decision with input as _host_input("web*")
	not d.allowed
	contains(d.reason, _host_reason)
}

test_target_host_comma_list_is_denied if {
	d := agentcloud.decision with input as _host_input("a,b")
	not d.allowed
	contains(d.reason, _host_reason)
}

test_target_host_empty_is_denied if {
	d := agentcloud.decision with input as _host_input("")
	not d.allowed
	contains(d.reason, _host_reason)
}

test_target_host_uppercase_is_denied if {
	d := agentcloud.decision with input as _host_input("Postiz-01")
	not d.allowed
	contains(d.reason, _host_reason)
}

# settings must be an object when present: a caller that sends another shape is refused, not skipped.
_settings_reason := "settings must be an object"

test_settings_string_is_denied if {
	d := agentcloud.decision with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"settings": "target_service=harden_svc"})
	not d.allowed
	contains(d.reason, _settings_reason)
}

test_settings_array_is_denied if {
	d := agentcloud.decision with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"settings": ["harden_svc"]})
	not d.allowed
	contains(d.reason, _settings_reason)
}

test_settings_null_is_denied if {
	d := agentcloud.decision with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"settings": null})
	not d.allowed
	contains(d.reason, _settings_reason)
}

# The rules above bind only an agent run_task: another action with odd settings gets none of their reasons.
test_settings_rules_are_silent_outside_an_agent_run if {
	d := agentcloud.decision with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {
		"service": "openbao",
		"action": "read_secret",
		"settings": "not-an-object",
	})
	not contains(d.reason, _settings_reason)
}

test_host_and_service_bounds_are_silent_outside_an_agent_run if {
	d := agentcloud.decision with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {
		"service": "openbao",
		"action": "read_secret",
		"settings": {"target_service": "all", "target_host": "all"},
	})
	not contains(d.reason, _target_reason)
	not contains(d.reason, _host_reason)
}

test_service_deploy_list_covers_a_listed_service if {
	agentcloud.allow with input as object.union(_svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy Postiz"), {"template_name": "Deploy Postiz (Dev)"})
}

# An assessment of one service does not license launching another (PR 203 Codex review).
test_service_deploy_must_launch_the_assessed_template if {
	d := agentcloud.decision with input as object.union(_svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy Postiz"), {"template_name": "Deploy honcho"})
	not d.allowed
	contains(d.reason, "the launched template is not the proposal's deploy template")
}

# PR 203 Codex review: the service assessment also feeds vm-rightsize, whose executor is Resize VM,
# not the proposal's deploy template.
test_vm_rightsize_runs_resize_vm_on_the_service_assessment if {
	agentcloud.allow with input as object.union(_run("infra-agent", "Resize VM", "vm-rightsize"), {
		"proposal": {"vm_spec": {"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "deploy_template": "Deploy Postiz", "verdict": {"decision": "converge", "findings": []}},
		"context": _ctx,
	})
}

test_service_deploy_without_a_deploy_template_is_denied if {
	svc := _svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy Postiz")
	d := agentcloud.decision with input as object.union(object.remove(svc, ["proposal"]), {"proposal": object.remove(svc.proposal, ["deploy_template"])})
	not d.allowed
	contains(d.reason, "the launched template is not the proposal's deploy template")
}

test_service_deploy_list_never_reaches_another_roles_template if {
	not agentcloud.allow with input as _run("service-agent", "Deploy Authentik", "service-deploy")
}

# The "Deploy " prefix this list replaced reached the platform's foundation.
test_service_agent_cannot_deploy_the_foundation if {
	every t in ["Deploy OpenBao", "Deploy Semaphore", "Deploy All Services", "Deploy GitHub Runner", "Deploy NetBox"] {
		not agentcloud.allow with input as object.union(_svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy Postiz"), {"template_name": t})
	}
}

# The step is bound to trusted registry data (Codex adversarial review of PR 203).
test_a_task_that_names_no_step_is_denied if {
	d := agentcloud.decision with input as object.remove(_run("security-agent", "Harden SSH", "access-harden"), ["step"])
	not d.allowed
	contains(d.reason, "a workflow task must name its registry step")
}

test_a_step_of_another_role_is_denied if {
	d := agentcloud.decision with input as _run("security-agent", "Harden SSH", "provision-vm")
	not d.allowed
	contains(d.reason, "the step belongs to another role")
}

test_a_template_that_does_not_execute_the_step_is_denied if {
	d := agentcloud.decision with input as _run("security-agent", "Harden SSH", "credential-backup")
	not d.allowed
	contains(d.reason, "the template does not execute this step")
}

test_a_step_fed_by_a_proposal_needs_one if {
	# Apply Firewall on dev, no proposal: the old rules never ran, and it was allowed.
	d := agentcloud.decision with input as _run("security-agent", "Apply Firewall", "fw-harden")
	not d.allowed
	contains(d.reason, "the step acts on a proposal and none was given")
}

# Scenario "Destructive template still needs a human"
test_destructive_template_denied_for_workflow_agent if {
	d := agentcloud.decision with input as _run("security-agent", "Clean Deploy Authentik", "oidc-config")
	not d.allowed
	contains(d.reason, "blocked by destructive template policy")
}

# Scenario "Unreviewed step cannot run from main"
# edge-dns (split from edge-route 2026-10-04) belongs to network-agent, the only role that may
# launch Apply Cloudflare Tofu, and only off main until the step is reviewed.
test_edge_dns_network_agent_runs_apply_cloudflare_tofu_off_main if {
	agentcloud.allow with input as _run("network-agent", "Apply Cloudflare Tofu", "edge-dns")
}

test_edge_dns_service_agent_denied if {
	d := agentcloud.decision with input as _run("service-agent", "Apply Cloudflare Tofu", "edge-dns")
	not d.allowed
	contains(d.reason, "template is not on this agent's allowed_templates")
}

test_network_agent_denied_other_steps_templates if {
	d := agentcloud.decision with input as _run("network-agent", "Manage Caddy Sites", "edge-route")
	not d.allowed
	contains(d.reason, "template is not on this agent's allowed_templates")
}

test_edge_dns_unreviewed_denied_on_main if {
	d := agentcloud.decision with input as object.union(_run("network-agent", "Apply Cloudflare Tofu", "edge-dns"), {"template_name": "Apply Cloudflare Tofu", "git_branch": "main"})
		with data.agentcloud.catalog.workflow_steps["edge-dns"].reviewed as false
	not d.allowed
	d.reason == "an unreviewed step cannot run from main"
}

test_edge_route_no_longer_executes_the_tofu_plan if {
	not agentcloud.allow with input as _run("network-agent", "Apply Cloudflare Tofu", "edge-route")
}

test_unreviewed_step_denied_on_main if {
	d := agentcloud.decision with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"template_name": "Harden SSH", "git_branch": "main"})
		with data.agentcloud.catalog.workflow_steps["access-harden"].reviewed as false
	not d.allowed
	d.reason == "an unreviewed step cannot run from main"
}

test_caller_supplied_review_state_is_ignored if {
	not agentcloud.allow with input as object.union(
		_run("security-agent", "Harden SSH", "access-harden"),
		{"template_name": "Harden SSH", "git_branch": "main", "step_reviewed": true},
	)
		with data.agentcloud.catalog.workflow_steps["access-harden"].reviewed as false
}

test_missing_branch_means_main if {
	not agentcloud.allow with input as object.remove(
		object.union(_run("security-agent", "Harden SSH", "access-harden"), {"template_name": "Harden SSH"}),
		["git_branch"],
	)
		with data.agentcloud.catalog.workflow_steps["access-harden"].reviewed as false
}

test_reviewed_step_allowed_on_main if {
	agentcloud.allow with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"template_name": "Harden SSH", "git_branch": "main"})
		with data.agentcloud.catalog.workflow_steps["access-harden"].reviewed as true
}

# A stamped step lets the main-bound base template run (the 2026-10-08 stamps); the fixture sets
# the stamp so the test does not depend on which steps data.json currently carries.
test_stamped_edge_dns_allows_the_base_template_on_main if {
	agentcloud.allow with input as object.union(_run("network-agent", "Apply Cloudflare Tofu", "edge-dns"), {"template_name": "Apply Cloudflare Tofu", "git_branch": "main"})
		with data.agentcloud.catalog.workflow_steps["edge-dns"].reviewed as true
}

# PR 203 Codex review: a base template runs from main even when the caller claims dev.
test_a_base_template_is_main_whatever_branch_is_claimed if {
	d := agentcloud.decision with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"template_name": "Harden SSH", "git_branch": "dev"})
		with data.agentcloud.catalog.workflow_steps["access-harden"].reviewed as false
	not d.allowed
	contains(d.reason, "an unreviewed step cannot run from main")
}

# Scenario "Orchestrator keeps SSH"
test_firewall_proposal_dropping_controller_ssh_denied if {
	d := agentcloud.decision with input as _fw([_ssh_from("198.51.100.0/24")])
	not d.allowed
	d.reason == "firewall proposal drops SSH from the orchestrator"
}

# Scenario "SSH stays scoped"
test_firewall_proposal_widening_ssh_denied if {
	d := agentcloud.decision with input as _fw([_ssh_from("192.0.2.10/32"), _ssh_from("0.0.0.0/0")])
	not d.allowed
	d.reason == "firewall proposal allows SSH from an undeclared source"
}

test_firewall_proposal_within_declared_sources_allowed if {
	agentcloud.allow with input as _fw([_ssh_from("192.0.2.10/32"), _ssh_from("198.51.100.0/24")])
}

test_firewall_proposal_without_context_fails_closed if {
	not agentcloud.allow with input as object.remove(_fw([_ssh_from("192.0.2.10/32")]), ["context"])
}

test_service_proposal_with_destructive_template_denied if {
	d := agentcloud.decision with input as _svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Clean Deploy Postiz")
	not d.allowed
	contains(d.reason, "service proposal names a destructive template")
}

test_service_proposal_over_tier_bounds_denied if {
	d := agentcloud.decision with input as _svc({"cores": 16, "memory_mb": 4096, "disk_gb": 32}, "Deploy Postiz")
	not d.allowed
	d.reason == "service proposal exceeds the tier's VM bounds"
}

test_service_proposal_within_bounds_allowed if {
	agentcloud.allow with input as _svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy Postiz")
}

test_service_proposal_without_bounds_fails_closed if {
	base := object.remove(_svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy Postiz"), ["context"])
	inp := object.union(base, {"context": {}}) # union merges nested objects, so remove first
	not agentcloud.allow with input as inp
}

# Legacy roles are frozen, not changed: their decisions are exactly as before.
test_legacy_nemoclaw_unscoped_run_task_unchanged if {
	agentcloud.allow with input as {
		"agent": "nemoclaw",
		"service": "semaphore",
		"action": "run_task",
		"template_name": "Deploy Postiz",
	}
}

# A change-required verdict records a finding and launches no executor (PR 203 review).
test_change_required_verdict_launches_no_executor if {
	base := _fw([_ssh_from("192.0.2.10/32")])
	inp := object.union(base, {"proposal": {"verdict": {"decision": "change-required", "findings": [{"summary": "s", "declared": 1, "proposed": 2}]}}})
	d := agentcloud.decision with input as inp
	not d.allowed
	contains(d.reason, "the proposal's verdict is not converge")
}

test_a_proposal_without_a_verdict_fails_closed if {
	not agentcloud.allow with input as object.union(_run("security-agent", "Apply Firewall", "fw-harden"), {
		"proposal": {"allow": [_ssh_from("192.0.2.10/32")], "deny_egress": []},
		"context": _ctx,
	})
}

test_access_proposal_within_declared_state_allowed if {
	agentcloud.allow with input as _acc("oidc", ["platform-admins"])
}

test_access_proposal_changing_auth_mode_denied if {
	d := agentcloud.decision with input as _acc("forward_auth", ["platform-admins"])
	not d.allowed
	contains(d.reason, "access proposal changes the declared auth mode")
}

test_access_proposal_with_undeclared_group_denied if {
	d := agentcloud.decision with input as _acc("oidc", ["platform-admins", "everyone"])
	not d.allowed
	contains(d.reason, "access proposal names an undeclared group")
}

test_access_proposal_without_context_fails_closed if {
	not agentcloud.allow with input as object.remove(_acc("oidc", []), ["context"])
}

# Plan 15's first backfill pilot.
test_service_agent_may_deploy_agentgateway if {
	agentcloud.allow with input as object.union(_svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy agentgateway"), {"template_name": "Deploy agentgateway (Dev)"})
}
