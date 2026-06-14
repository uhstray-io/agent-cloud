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

test_unknown_agent_denied if {
	not agentcloud.allow with input as {"agent": "rogue-agent", "action": "read", "service": "nocodb"}
}

test_known_agent_unknown_action_denied if {
	not agentcloud.allow with input as {"agent": "nemoclaw", "action": "delete", "service": "nocodb"}
}
