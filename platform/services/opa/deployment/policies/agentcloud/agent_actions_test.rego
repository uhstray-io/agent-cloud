# Unit tests for the agent action policy. Run: opa test /policies -v
# (the deploy playbook runs this inside the container after start).
package agentcloud_test

import rego.v1

import data.agentcloud

test_nemoclaw_read_nocodb_allowed if {
	agentcloud.allow with input as {"agent": "nemoclaw", "action": "read", "service": "nocodb"}
}

test_nemoclaw_nmap_denied if {
	not agentcloud.allow with input as {"agent": "nemoclaw", "action": "scan_subnet", "service": "nmap"}
}

test_netclaw_snmp_poll_allowed if {
	agentcloud.allow with input as {"agent": "netclaw", "action": "poll", "service": "snmp"}
}

test_destructive_template_blocked_without_approval if {
	not agentcloud.allow with input as {
		"agent": "nemoclaw",
		"action": "run_task",
		"service": "semaphore",
		"template_name": "Clean Deploy NetBox",
		"human_approved": false,
	}
}

test_destructive_template_allowed_with_approval if {
	agentcloud.allow with input as {
		"agent": "nemoclaw",
		"action": "run_task",
		"service": "semaphore",
		"template_name": "Clean Deploy NetBox",
		"human_approved": true,
	}
}

# ERPNext (financial system-of-record) clean-deploy must be blocked too — the
# prefix rule covers it even though it was once missing from the explicit list.
test_clean_deploy_erpnext_blocked if {
	not agentcloud.allow with input as {
		"agent": "nemoclaw",
		"action": "run_task",
		"service": "semaphore",
		"template_name": "Clean Deploy ERPNext",
		"human_approved": false,
	}
}

# The "(Local)" template-name variant is blocked by the prefix rule as well.
test_clean_deploy_local_variant_blocked if {
	not agentcloud.allow with input as {
		"agent": "nemoclaw",
		"action": "run_task",
		"service": "semaphore",
		"template_name": "Clean Deploy ERPNext (Local)",
		"human_approved": false,
	}
}

test_unknown_agent_denied if {
	not agentcloud.allow with input as {"agent": "rogue-agent", "action": "read", "service": "nocodb"}
}

test_known_agent_unknown_action_denied if {
	not agentcloud.allow with input as {"agent": "nemoclaw", "action": "delete", "service": "nocodb"}
}
