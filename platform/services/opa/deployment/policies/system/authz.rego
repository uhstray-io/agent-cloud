# Authorization for OPA's own HTTP API. compose.yml and compose.tls.yml start OPA with
# --authorization=basic, so OPA asks data.system.authz.allow about every request on its listener
# and answers 401 (code "unauthorized") unless it is true. Deny is the default.
#
# This policy admits exactly two requests: the agent decision (POST /v1/data/agentcloud/decision)
# and the health probe (GET /health). Everything else is refused: reading the data documents
# (the agent catalogs and the guardrail rules), ad-hoc queries, policy and data writes, metrics.
# The host firewall admits the callers; this policy limits what an admitted caller can do.
#
# Why: the firewall is the only other control. Without this policy, any caller it admits can read
# every data document and query arbitrary paths. Bundle mode (--bundle) separately refuses writes,
# but it does not refuse reads.
#
# input.path is the request path split on "/" (the query string is not part of it).
# Tests: authz_test.rego, one allowed and one denied case per request this API serves or must refuse.

package system.authz

default allow := false

allow if {
	input.method == "POST"
	input.path == ["v1", "data", "agentcloud", "decision"]
}

allow if {
	input.method == "GET"
	input.path == ["health"]
}
