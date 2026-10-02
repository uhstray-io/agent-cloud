# Ledger rules (docs/MISTAKES.md section 7): agent branch (1.9), no-probe-writes (3.1),
# no-undeclared-shared-mutation (3.2). Per MISTAKES 8.4 every field a rule reads is tested
# absent, blank and of the wrong type, beside an allowed case.
#
# No agent is granted write_secret or the update_* actions in data.json, so `allow` is false
# for them whatever the deny rules say. Each test therefore grants the action with `with` and
# asserts the specific deny reason: a bare `not allow` here would pass with the rule deleted.
package agentcloud_ledger_test

import rego.v1

import data.agentcloud

# --- Branch (MISTAKES 1.9) ---------------------------------------------------------------

_branch_reason := "an agent task may run only from main or dev"

# Allowed on dev: a (Dev) template on the dev branch, for a role-scoped agent.
_dev_run := {
	"agent": "security-agent",
	"service": "semaphore",
	"action": "run_task",
	"template_name": "Harden SSH (Dev)",
	"step": "access-harden",
	"git_branch": "dev",
}

_with_branch(b) := object.union(_dev_run, {"git_branch": b})

_branch_denied(inp) if {
	d := agentcloud.decision with input as inp
	not d.allowed
	contains(d.reason, _branch_reason)
}

test_branch_dev_allowed if {
	agentcloud.allow with input as _dev_run
}

test_branch_main_allowed_for_a_reviewed_step if {
	agentcloud.allow with input as object.union(_dev_run, {"template_name": "Harden SSH", "git_branch": "main"})
		with data.agentcloud.catalog.workflow_steps["access-harden"].reviewed as true
}

# Absent means main, the most restrictive branch: not a branch-rule denial, so a reviewed step
# is allowed and an unreviewed one is still stopped by the main rule.
test_branch_absent_means_main if {
	inp := object.remove(object.union(_dev_run, {"template_name": "Harden SSH"}), ["git_branch"])
	agentcloud.allow with input as inp
		with data.agentcloud.catalog.workflow_steps["access-harden"].reviewed as true
	d := agentcloud.decision with input as inp
	not d.allowed
	not contains(d.reason, _branch_reason)
	contains(d.reason, "an unreviewed step cannot run from main")
}

test_branch_feature_branch_denied if {
	_branch_denied(_with_branch("feat/opa-ledger-rules"))
}

test_branch_blank_denied if {
	every b in ["", "   "] {
		_branch_denied(_with_branch(b))
	}
}

# Before this rule `_runs_from_main` read null as "not main", so a (Dev) template with a null
# branch skipped the unreviewed-on-main check and was allowed.
test_branch_null_denied if {
	_branch_denied(_with_branch(null))
}

test_branch_wrong_type_denied if {
	every b in [1, true, ["dev"], {"name": "dev"}] {
		_branch_denied(_with_branch(b))
	}
}

test_branch_near_miss_spellings_denied if {
	every b in [" dev", "dev ", "Dev", "MAIN", "refs/heads/dev", "origin/main"] {
		_branch_denied(_with_branch(b))
	}
}

# Frozen agents are covered too: a launch is an agent launch whoever sends it.
test_branch_feature_branch_denied_for_a_legacy_agent if {
	_branch_denied({
		"agent": "nemoclaw",
		"service": "semaphore",
		"action": "run_task",
		"template_name": "Deploy tududi",
		"git_branch": "feat/x",
	})
}

test_branch_approval_does_not_unlock_a_feature_branch if {
	_branch_denied(object.union(_with_branch("feat/x"), {"human_approved": true}))
}

test_branch_missing_catalog_fails_closed if {
	d := agentcloud.decision with input as _dev_run
		with data.agentcloud.catalog.semaphore.launch_branches as null
	not d.allowed
	contains(d.reason, _branch_reason)
}

# --- no-probe-writes (MISTAKES 3.1) ------------------------------------------------------

_write_grant := {"openbao": ["write_secret"]}

_write := {
	"agent": "skynet",
	"service": "openbao",
	"action": "write_secret",
	"payload_markers": [],
}

_write_with(m) := object.union(_write, {"payload_markers": m})

_write_decision(inp) := d if {
	d := agentcloud.decision with input as inp
		with data.agentcloud.catalog.skynet.allowed_actions as _write_grant
}

_write_denied(inp, why) if {
	d := _write_decision(inp)
	not d.allowed
	contains(d.reason, why)
}

test_secret_write_with_no_markers_allowed if {
	_write_decision(_write).allowed
}

test_secret_write_markers_absent_denied if {
	_write_denied(object.remove(_write, ["payload_markers"]), "a secret write must declare its payload markers")
}

test_secret_write_markers_null_denied if {
	_write_denied(_write_with(null), "a secret write must declare its payload markers")
}

test_secret_write_markers_blank_denied if {
	every m in ["", "   "] {
		_write_denied(_write_with(m), "a secret write must declare its payload markers")
	}
}

test_secret_write_markers_wrong_type_denied if {
	every m in ["probe-only", 0, false, {"probe": true}] {
		_write_denied(_write_with(m), "a secret write must declare its payload markers")
	}
}

test_secret_write_placeholder_marker_denied if {
	every m in [["probe-only"], ["changeme"], ["test", "example"]] {
		_write_denied(_write_with(m), "a secret write carries a placeholder payload")
	}
}

test_secret_write_marker_match_ignores_case_and_padding if {
	_write_denied(_write_with([" PROBE "]), "a secret write carries a placeholder payload")
}

test_secret_write_blank_marker_entry_denied if {
	every m in [[""], ["   "]] {
		_write_denied(_write_with(m), "a secret write declares a malformed payload marker")
	}
}

test_secret_write_wrong_type_marker_entry_denied if {
	every m in [[1], [null], [["probe"]], [{"m": "probe"}]] {
		_write_denied(_write_with(m), "a secret write declares a malformed payload marker")
	}
}

# A marker outside the catalog means the caller scanned against a different list.
test_secret_write_unknown_marker_denied if {
	_write_denied(_write_with(["fake"]), "a secret write declares a malformed payload marker")
}

test_secret_write_placeholder_allowed_with_approval if {
	_write_decision(object.union(_write_with(["probe-only"]), {"human_approved": true})).allowed
}

# Approval is the boolean true: a string or number is not approval.
test_secret_write_wrong_type_approval_denied if {
	every a in ["true", "false", 1] {
		_write_denied(object.union(_write_with(["probe-only"]), {"human_approved": a}), "a secret write carries a placeholder payload")
	}
}

test_secret_write_missing_marker_catalog_fails_closed if {
	d := agentcloud.decision with input as _write_with(["probe-only"])
		with data.agentcloud.catalog.skynet.allowed_actions as _write_grant
		with data.agentcloud.catalog.placeholder_markers as null
	not d.allowed
	contains(d.reason, "a secret write declares a malformed payload marker")
}

# --- no-undeclared-shared-mutation (MISTAKES 3.2) ----------------------------------------

_mut_reason := "a shared orchestrator object not declared as code needs human approval"

_mut_grant := {"semaphore": ["update_key", "update_inventory", "update_repository"]}

_mut := {
	"agent": "skynet",
	"service": "semaphore",
	"action": "update_repository",
	"target": "agent-cloud dev",
}

_mut_with(t) := object.union(_mut, {"target": t})

_mut_decision(inp) := d if {
	d := agentcloud.decision with input as inp
		with data.agentcloud.catalog.skynet.allowed_actions as _mut_grant
}

_mut_denied(inp) if {
	d := _mut_decision(inp)
	not d.allowed
	contains(d.reason, _mut_reason)
}

test_mutation_of_a_declared_repository_allowed if {
	_mut_decision(_mut).allowed
}

test_mutation_target_absent_denied if {
	_mut_denied(object.remove(_mut, ["target"]))
}

test_mutation_target_null_denied if {
	_mut_denied(_mut_with(null))
}

test_mutation_target_blank_denied if {
	every t in ["", "   "] {
		_mut_denied(_mut_with(t))
	}
}

test_mutation_target_wrong_type_denied if {
	every t in [1, true, ["agent-cloud dev"], {"name": "agent-cloud dev"}] {
		_mut_denied(_mut_with(t))
	}
}

test_mutation_target_near_miss_denied if {
	every t in [" agent-cloud dev", "Agent-Cloud Dev", "agent-cloud feature"] {
		_mut_denied(_mut_with(t))
	}
}

# Declarations are per kind: a repository record's name does not declare a key of that name.
test_mutation_kind_is_part_of_the_declaration if {
	_mut_denied(object.union(_mut, {"action": "update_key", "target": "agent-cloud"}))
}

# The repo declares no key-store entry today, and only the "production" inventory record.
test_mutation_of_an_undeclared_key_or_inventory_denied if {
	_mut_denied(object.union(_mut, {"action": "update_key", "target": "none"}))
	_mut_denied(object.union(_mut, {"action": "update_inventory", "target": "staging"}))
}

# The production inventory is declared as code (synced from site-config by sync-inventory.yml).
test_mutation_of_the_declared_production_inventory_allowed if {
	_mut_decision(object.union(_mut, {"action": "update_inventory", "target": "production"})).allowed
}

test_mutation_production_inventory_near_miss_denied if {
	every t in ["Production", " production", "prod"] {
		_mut_denied(object.union(_mut, {"action": "update_inventory", "target": t}))
	}
}

# Declarations are per kind: the production inventory does not declare a repository of that name.
test_mutation_production_is_declared_only_as_an_inventory if {
	_mut_denied(_mut_with("production"))
}

test_mutation_undeclared_allowed_with_approval if {
	_mut_decision(object.union(_mut_with("staging"), {"action": "update_inventory", "human_approved": true})).allowed
}

test_mutation_wrong_type_approval_denied if {
	every a in ["true", 1, {"by": "operator"}] {
		_mut_denied(object.union(_mut_with("staging"), {"action": "update_inventory", "human_approved": a}))
	}
}

test_mutation_missing_declarations_fail_closed if {
	d := agentcloud.decision with input as _mut
		with data.agentcloud.catalog.skynet.allowed_actions as _mut_grant
		with data.agentcloud.catalog.semaphore.declared_objects as null
	not d.allowed
	contains(d.reason, _mut_reason)
}

# --- Approval type on the existing destructive-template rule -----------------------------

test_destructive_template_wrong_type_approval_denied if {
	every a in ["true", "false", 1] {
		d := agentcloud.decision with input as {
			"agent": "nemoclaw",
			"service": "semaphore",
			"action": "run_task",
			"template_name": "Clean Deploy NetBox",
			"human_approved": a,
		}
		not d.allowed
		contains(d.reason, "blocked by destructive template policy")
	}
}
