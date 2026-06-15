# Core agent action authorization. Each agent's permitted actions per service
# live in data.json under data.agentcloud.catalog (static config — a SEPARATE namespace
# from this `agentcloud` package, so the dynamic data.agentcloud.catalog[input.agent]
# lookup can't recurse into the rules); dynamic context (template_name,
# human_approved, …) comes from the query `input`. deny takes precedence so a
# destructive-template block can't be out-voted by a generic allow.
# Query: POST /v1/data/agentcloud/decision  with {"input": {...}}.
package agentcloud

import rego.v1

default allow := false

# Hard block: destructive Semaphore templates require explicit human approval,
# for ANY agent. Evaluated before allow (deny wins). Matching is by PREFIX
# ("Clean Deploy ...") OR the explicit list — the prefix auto-covers every
# clean-deploy template (prod names AND the "(Local)" variants) and any future
# service, so the guardrail can't grow a hole when a service is added (the
# data.json list alone once silently missed ERPNext, the financial system-of-record).
deny if {
	input.service == "semaphore"
	input.action == "run_task"
	not input.human_approved
	_destructive(input.template_name)
}

_destructive(t) if startswith(t, "Clean Deploy")

_destructive(t) if t in data.agentcloud.catalog.semaphore.destructive_templates

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

reason := "blocked by destructive template policy" if deny

reason := "allowed by agent policy" if {
	not deny
	allow
}

reason := "no matching allow rule" if {
	not deny
	not allow
}
