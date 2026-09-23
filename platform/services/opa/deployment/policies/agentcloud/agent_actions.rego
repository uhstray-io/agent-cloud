# Core agent action authorization. Each agent's permitted actions per service
# live in data.json under data.agentcloud.catalog (static config — a SEPARATE namespace
# from this `agentcloud` package, so the dynamic data.agentcloud.catalog[input.agent]
# lookup can't recurse into the rules); dynamic context (template_name,
# human_approved, …) comes from the query `input`. deny takes precedence so a
# destructive-template block can't be out-voted by a generic allow.
# Query: POST /v1/data/agentcloud/decision  with {"input": {...}}.
#
# Workflow agents (change service-deployment-workflow): an identity that declares
# `allowed_templates` is ROLE-SCOPED. For it, run_task also needs the template on its list
# (or on its closed `service_deploy_templates` list), and the task must name the registry
# STEP it executes. The step is checked against trusted data, never taken on the caller's
# word: catalog.workflow_steps (pinned to platform/workflows/service-onboarding/registry.yml
# by test_workflow_registry.py) says which role owns it, which templates execute it, which
# reasoning step's proposal it acts on, and whether its review has passed. Extra input:
#   git_branch     branch the task runs (Semaphore task `git_branch`); missing means main
#   step           registry step id this task executes (fw-harden, service-deploy, ...)
#   proposal       the proposal body of the reasoning step that feeds it, REQUIRED when it
#                  has one (schemas beside platform/workflows/service-onboarding/)
#   context        facts from the step's snapshot, never from the model:
#                  controller_cidr, ssh_cidrs, tier_bounds {cores, memory_mb, disk_gb}
package agentcloud

import rego.v1

default allow := false

deny if count(deny_reasons) > 0

# Hard block: destructive Semaphore templates require explicit human approval,
# for ANY agent. Matching is by PREFIX ("Clean Deploy ...") OR the explicit list — the
# prefix auto-covers every clean-deploy template (prod names AND the "(Local)" variants)
# and any future service, so the guardrail can't grow a hole when a service is added (the
# data.json list alone once silently missed ERPNext, the financial system-of-record).
deny_reasons contains "blocked by destructive template policy" if {
	_unapproved_run_task
	_destructive(input.template_name)
}

# Fail closed: an unapproved run_task whose template_name is missing, non-string,
# or blank could otherwise dodge the destructive check above (which is undefined
# for a missing/blank name) and still pass `allow`. object.get defaults a missing
# key to "" so the same rule covers absent, null, and whitespace-only names —
# forcing a real, checkable template name on any unapproved run_task.
deny_reasons contains "blocked by destructive template policy" if {
	_unapproved_run_task
	not _valid_template_name
}

_unapproved_run_task if {
	input.service == "semaphore"
	input.action == "run_task"
	not input.human_approved
}

_valid_template_name if {
	t := object.get(input, "template_name", "")
	is_string(t)
	trim_space(t) != ""
}

_destructive(t) if startswith(t, "Clean Deploy")

_destructive(t) if t in data.agentcloud.catalog.semaphore.destructive_templates

# --- Workflow agents: template allowlist ------------------------------------------------

_role_scoped if data.agentcloud.catalog[input.agent].allowed_templates

_role_run_task if {
	_role_scoped
	input.service == "semaphore"
	input.action == "run_task"
}

deny_reasons contains "template is not on this agent's allowed_templates" if {
	_role_run_task
	not _template_allowed
}

# Template names are compared without the generated " (Dev)" / " (Local)" suffix, so the
# allowlist names one template per playbook (spec: One template per playbook).
_base_template := trim_suffix(trim_suffix(object.get(input, "template_name", ""), " (Dev)"), " (Local)")

_template_allowed if _base_template in data.agentcloud.catalog[input.agent].allowed_templates

# The registry step "Deploy {service}" is granted as a CLOSED list of application-service
# deploys, not a "Deploy " prefix: the prefix also reached Deploy OpenBao, Deploy Semaphore
# and Deploy All Services (OPA-evaluated, 2026-09-22). A new service is added by name.
_template_allowed if _base_template in object.get(data.agentcloud.catalog[input.agent], "service_deploy_templates", [])

# --- Workflow agents: the registry step ---------------------------------------------------

_step := data.agentcloud.catalog.workflow_steps[input.step]

deny_reasons contains "a workflow task must name its registry step" if {
	_role_run_task
	not _step
}

deny_reasons contains "the step belongs to another role" if {
	_role_run_task
	_step.owner != input.agent
}

deny_reasons contains "the template does not execute this step" if {
	_role_run_task
	_step
	not _template_executes_step
}

_template_executes_step if _base_template in _step.templates

_template_executes_step if {
	_step.per_service
	_base_template in object.get(data.agentcloud.catalog[input.agent], "service_deploy_templates", [])
}

# --- Workflow agents: branch ------------------------------------------------------------

# Review state is the registry's, not a boolean the caller supplies.
deny_reasons contains "an unreviewed step cannot run from main" if {
	_role_run_task
	object.get(input, "git_branch", "main") == "main"
	not _step.reviewed
}

# --- Workflow agents: proposal required -------------------------------------------------

# A step fed by a reasoning step acts on that step's proposal; running it without one would
# skip every content rule below. The data OMITS proposal_from for other steps: a null would
# be true here.
_proposal_step := _step.proposal_from

deny_reasons contains "the step acts on a proposal and none was given" if {
	_role_run_task
	_proposal_step
	not is_object(object.get(input, "proposal", null))
}

# Only a `converge` verdict runs an executor. `change-required` means a declared value must
# change, and that lands through a pull request, never through an executor (verdict.json;
# spec "Existing declared state is the default outcome"). A missing verdict fails closed.
deny_reasons contains "the proposal's verdict is not converge" if {
	_role_run_task
	_proposal_step
	is_object(object.get(input, "proposal", null))
	object.get(object.get(input.proposal, "verdict", {}), "decision", "") != "converge"
}

# --- Workflow agents: proposal content --------------------------------------------------

# Firewall: SSH must stay open to the orchestrator (Semaphore reaches every host over SSH),
# and must never be opened from a source outside the declared SSH sources.
deny_reasons contains "firewall proposal drops SSH from the orchestrator" if {
	_proposal_step == "fw-assess"
	not _fw_keeps_controller_ssh
}

_fw_keeps_controller_ssh if {
	some rule in input.proposal.allow
	rule.port == 22
	rule.proto == "tcp"
	rule.source == input.context.controller_cidr
}

deny_reasons contains "firewall proposal allows SSH from an undeclared source" if {
	_proposal_step == "fw-assess"
	some rule in input.proposal.allow
	rule.port == 22
	not rule.source in object.get(object.get(input, "context", {}), "ssh_cidrs", [])
}

# Service assessment: never a destructive template as the runtime action, and a VM spec
# within the service tier's bounds. Missing bounds fail closed.
deny_reasons contains "service proposal names a destructive template" if {
	_proposal_step == "service-assess"
	_destructive(input.proposal.deploy_template)
}

# The deploy that runs is the one the assessment chose: otherwise an assessment of one service
# licenses launching another (PR 203 Codex review). A missing deploy_template fails closed.
deny_reasons contains "the launched template is not the proposal's deploy template" if {
	_proposal_step == "service-assess"
	is_object(object.get(input, "proposal", null))
	_base_template != trim_suffix(trim_suffix(object.get(input.proposal, "deploy_template", ""), " (Dev)"), " (Local)")
}

deny_reasons contains "service proposal exceeds the tier's VM bounds" if {
	_proposal_step == "service-assess"
	not _within_tier_bounds
}

# Access: the proposed auth mode is the one the snapshot declares, and every Authentik group
# the proposal names is one the snapshot declares. Missing context fails closed.
deny_reasons contains "access proposal changes the declared auth mode" if {
	_proposal_step == "access-assess"
	not _access_mode_matches
}

_access_mode_matches if input.proposal.auth_mode == input.context.auth_mode

deny_reasons contains "access proposal names an undeclared group" if {
	_proposal_step == "access-assess"
	not _access_groups_declared
}

_access_groups_declared if {
	every group in _proposed_groups {
		group in input.context.groups
	}
}

# authentik_app is null for a service with no Authentik application.
default _proposed_groups := []

_proposed_groups := object.get(input.proposal.authentik_app, "groups", []) if is_object(input.proposal.authentik_app)

# A helper negated as a whole, not `not a <= b`: with `b` undefined, OPA 1.0.0 evaluated
# `some dim in [...]; not vm_spec[dim] <= tier_bounds[dim]` to NO result instead of true,
# so missing bounds silently allowed the proposal (found by
# test_service_proposal_without_bounds_fails_closed, 2026-09-22).
_within_tier_bounds if {
	every dim in ["cores", "memory_mb", "disk_gb"] {
		input.proposal.vm_spec[dim] <= input.context.tier_bounds[dim]
	}
}

# --- Allow ------------------------------------------------------------------------------

# Allow when the agent's per-service action list (data.json) permits the action.
# An unknown agent/service/action makes the lookup undefined -> rule fails ->
# default deny. Generalizes over agents: data.agentcloud.catalog[<agent>].allowed_actions.
allow if {
	not deny
	actions := data.agentcloud.catalog[input.agent].allowed_actions[input.service]
	input.action in actions
}

# Decision object returned to callers (allow + human-readable reason).
decision := {
	"allowed": allow,
	"agent": input.agent,
	"service": input.service,
	"action": input.action,
	"reason": reason,
}

reason := concat("; ", sort(deny_reasons)) if deny

reason := "allowed by agent policy" if {
	not deny
	allow
}

reason := "no matching allow rule" if {
	not deny
	not allow
}
