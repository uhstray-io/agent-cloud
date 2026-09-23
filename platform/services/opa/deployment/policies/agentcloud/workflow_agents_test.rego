# Workflow agent rules (change service-deployment-workflow, platform/agent-orchestration and
# platform/semaphore-environments). Addresses are RFC 5737 documentation ranges.
package agentcloud_workflow_test

import rego.v1

import data.agentcloud

_run(agent, template, step) := {
	"agent": agent,
	"service": "semaphore",
	"action": "run_task",
	"template_name": template,
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

_ssh_from(src) := {"port": 22, "proto": "tcp", "source": src, "consumer": "semaphore", "justification": "orchestration"}

_svc(spec, template) := object.union(_run("service-agent", "Deploy tududi", "service-deploy"), {
	"proposal": {"vm_spec": spec, "deploy_template": template},
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

test_service_deploy_list_covers_a_listed_service if {
	agentcloud.allow with input as object.union(_svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy tududi"), {"template_name": "Deploy tududi (Dev)"})
}

test_service_deploy_list_never_reaches_another_roles_template if {
	not agentcloud.allow with input as _run("service-agent", "Deploy Authentik", "service-deploy")
}

# The "Deploy " prefix this list replaced reached the platform's foundation.
test_service_agent_cannot_deploy_the_foundation if {
	every t in ["Deploy OpenBao", "Deploy Semaphore", "Deploy All Services", "Deploy GitHub Runner", "Deploy NetBox"] {
		not agentcloud.allow with input as object.union(_svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy tududi"), {"template_name": t})
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
test_unreviewed_step_denied_on_main if {
	d := agentcloud.decision with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"git_branch": "main"})
	not d.allowed
	d.reason == "an unreviewed step cannot run from main"
}

test_caller_supplied_review_state_is_ignored if {
	not agentcloud.allow with input as object.union(
		_run("security-agent", "Harden SSH", "access-harden"),
		{"git_branch": "main", "step_reviewed": true},
	)
}

test_missing_branch_means_main if {
	not agentcloud.allow with input as object.remove(_run("security-agent", "Harden SSH", "access-harden"), ["git_branch"])
}

test_reviewed_step_allowed_on_main if {
	agentcloud.allow with input as object.union(_run("security-agent", "Harden SSH", "access-harden"), {"git_branch": "main"})
		with data.agentcloud.catalog.workflow_steps["access-harden"].reviewed as true
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
	d := agentcloud.decision with input as _svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Clean Deploy tududi")
	not d.allowed
	d.reason == "service proposal names a destructive template"
}

test_service_proposal_over_tier_bounds_denied if {
	d := agentcloud.decision with input as _svc({"cores": 16, "memory_mb": 4096, "disk_gb": 32}, "Deploy tududi")
	not d.allowed
	d.reason == "service proposal exceeds the tier's VM bounds"
}

test_service_proposal_within_bounds_allowed if {
	agentcloud.allow with input as _svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy tududi")
}

test_service_proposal_without_bounds_fails_closed if {
	base := object.remove(_svc({"cores": 2, "memory_mb": 4096, "disk_gb": 32}, "Deploy tududi"), ["context"])
	inp := object.union(base, {"context": {}}) # union merges nested objects, so remove first
	not agentcloud.allow with input as inp
}

# Legacy roles are frozen, not changed: their decisions are exactly as before.
test_legacy_nemoclaw_unscoped_run_task_unchanged if {
	agentcloud.allow with input as {
		"agent": "nemoclaw",
		"service": "semaphore",
		"action": "run_task",
		"template_name": "Deploy tududi",
	}
}
