# OPA — architecture (how AI agents interact with it)

OPA is the **cross-service authorization decision point** of the Guardrail
Layer: OpenBao answers "here are your credentials," OPA answers "are you allowed
to do this?", Kyverno (k8s only) answers "is this resource compliant?". No
overlap — OPA never stores secrets or executes actions; it returns decisions.

## Query contract

Agents query OPA over HTTP before executing a service action:

```text
POST http://opa:8181/v1/data/agentcloud/decision
{ "input": { "agent": "nemoclaw", "action": "read", "service": "nocodb",
             "template_name": "...", "human_approved": false } }
->
{ "result": { "allowed": true, "agent": "...", "service": "...",
              "action": "...", "reason": "allowed by agent policy" } }
```

`/v1/data/agentcloud/allow` returns just the boolean. Static permissions live in
`policies/agentcloud/data.json` (per-agent action catalogs, destructive-template
list); dynamic context (`human_approved`, `template_name`, counts, time) is
supplied per-query in `input` — never stored in OPA.

## Local-dev specifics

- Reached by agents + the control plane as `opa:8181` on the `local-dev`
  network; published to `127.0.0.1:8281` for host diagnostics + smoke checks
  (8181 is NocoDB's local bind, so the host port defaults to `OPA_PORT=8281`).
- **Phase 1 is unauthenticated** — internal-only, returns decisions not secrets,
  so there are no OPA credentials yet. Phase 2 adds per-agent bearer tokens
  (OpenBao). Phase 3 ships decision logs to Loki via stdout.
- Policies are mounted read-only from `./policies`; a change is applied by
  redeploy (git pull + restart). Rego is tested with `opa test /policies`.
- Not behind Caddy: OPA is a programmatic decision API (no UI), queried by
  container name, so it has no `*.agent-cloud.test` route.
- **Intentionally excluded from Authentik SSO.** OPA is a machine API, not a
  human-login surface — there is no forward_auth/OIDC gate (unlike NetBox/
  OpenBao/n8n which are browser-facing). Authorization for OPA is per-agent
  bearer tokens from OpenBao (Phase 2), not an IdP login. Documented here so the
  "no SSO" state reads as a decision, not an oversight (AUTH-SSO-DEPLOYMENT.md).

## Policy modules

- `agentcloud/agent_actions.rego` — per-agent action authorization; `deny` (e.g.
  destructive Semaphore templates without `human_approved`) takes precedence
  over `allow`.
- `agentcloud/workflow_agents_test.rego` — tests for the service-deployment workflow agents.

### Workflow agents (service-deployment workflow)

`infra-agent`, `security-agent`, `o11y-agent` and `service-agent` are **role-scoped**: each
declares `allowed_templates` (and `service-agent` a closed `service_deploy_templates` list of
application-service deploys, never the foundation), which must equal the
templates the step registry (`platform/workflows/service-onboarding/registry.yml`) assigns
it; `platform/tests/test_workflow_registry.py` fails when they drift. For a role-scoped
agent, `run_task` is denied when the template is not its own, when the branch is `main`
(`git_branch`, missing means main) and `step_reviewed` is not true, and when an attached
reasoning-step `proposal` fails a content rule: a firewall proposal must keep SSH from
`context.controller_cidr` and allow SSH only from `context.ssh_cidrs`; a service proposal may
not name a destructive template and must fit `context.tier_bounds`. Missing context fails
closed. `reason` lists every denial, joined with `; `. `netclaw` and `nemoclaw` are frozen:
kept, unchanged, not workflow agents.

Proposal JSON Schemas are NOT in `policies/`: OPA loads every JSON file under it as data,
and schema files there fail `opa check` with `merge error`. They live beside the registry.
CI runs `opa check --strict` and `opa test` on this tree.

Full design + phased rollout: `plan/development/03-guardrails-governance.md`.
