# Unit tests for the authorization policy (system.authz). Run: opa test --bundle /policies -v
# (the deploy playbook runs this inside the container after start; CI runs it on the tree).
# Each row is one request the decision API serves (allowed) or must refuse (denied). The
# request path is input.path as OPA splits it: a query string is not part of it.
package system.authz_test

import rego.v1

import data.system.authz

# Allowed: the two requests the API serves.

test_allow_decision_post if {
	authz.allow with input as {"method": "POST", "path": ["v1", "data", "agentcloud", "decision"]}
}

test_allow_health_get if {
	authz.allow with input as {"method": "GET", "path": ["health"]}
}

test_allow_decision_post_with_empty_params if {
	authz.allow with input as {"method": "POST", "path": ["v1", "data", "agentcloud", "decision"], "params": {}}
}

# Denied: a decision asking for engine internals (each returns more than the decision itself).

test_deny_decision_explain if {
	not authz.allow with input as {"method": "POST", "path": ["v1", "data", "agentcloud", "decision"], "params": {"explain": ["full"]}}
}

test_deny_decision_instrument if {
	not authz.allow with input as {"method": "POST", "path": ["v1", "data", "agentcloud", "decision"], "params": {"instrument": ["true"]}}
}

test_deny_decision_metrics if {
	not authz.allow with input as {"method": "POST", "path": ["v1", "data", "agentcloud", "decision"], "params": {"metrics": [""]}}
}

test_deny_decision_provenance if {
	not authz.allow with input as {"method": "POST", "path": ["v1", "data", "agentcloud", "decision"], "params": {"provenance": [""]}}
}

# Denied: data reads and writes, queries, policy writes, metrics, and the boolean decision path.

test_deny_policy_write_probe_put if {
	not authz.allow with input as {"method": "PUT", "path": ["v1", "data", "agentcloud_write_probe"]}
}

test_deny_policy_put if {
	not authz.allow with input as {"method": "PUT", "path": ["v1", "policies", "x"]}
}

test_deny_data_root_get if {
	not authz.allow with input as {"method": "GET", "path": ["v1", "data"]}
}

test_deny_query_post if {
	not authz.allow with input as {"method": "POST", "path": ["v1", "query"]}
}

test_deny_metrics_get if {
	not authz.allow with input as {"method": "GET", "path": ["metrics"]}
}

test_deny_allow_path_post if {
	not authz.allow with input as {"method": "POST", "path": ["v1", "data", "agentcloud", "allow"]}
}

# Denied: the right path with the wrong method, so each allow rule must match both conjuncts.

test_deny_decision_get if {
	not authz.allow with input as {"method": "GET", "path": ["v1", "data", "agentcloud", "decision"]}
}

test_deny_health_post if {
	not authz.allow with input as {"method": "POST", "path": ["health"]}
}

# Denied: an absent request shape fails closed (no method or path at all).

test_deny_empty_input if {
	not authz.allow with input as {}
}
