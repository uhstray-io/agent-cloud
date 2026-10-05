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
- `agentcloud/ledger_rules_test.rego` — tests for the ledger rules (below).

### Workflow agents (service-deployment workflow)

`infra-agent`, `security-agent`, `o11y-agent`, `service-agent` and `network-agent` (2026-10-04: the edge-dns step, the only role allowed `Apply Cloudflare Tofu`) are **role-scoped**: each
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

### Ledger rules (`docs/MISTAKES.md` §7)

Three rules carry ledger entries as authorisation decisions. Tests:
`agentcloud/ledger_rules_test.rego`, which tests each field absent, blank and of the wrong
type (§8.4). No agent is granted `write_secret` or an `update_*` action in `data.json` today,
so these rules decide nothing until a grant lands; the tests add the grant with `with`.

| Rule | Applies to | Input fields | Denied when |
|---|---|---|---|
| Agent branch (§1.9) | `service: semaphore`, `action: run_task`, any agent, approved or not | `git_branch` (string; absent means `main`) | present and not exactly one of `catalog.semaphore.launch_branches` (`main`, `dev`): a feature branch, `null`, blank, padded, wrong case or non-string |
| `no-probe-writes` (§3.1) | `action: write_secret`, any service, unless approved | `payload_markers` (array of strings; `[]` = scanned, nothing matched) | absent, `null` or not an array; an entry that is not a non-blank string or not in `catalog.placeholder_markers`; an entry matching a marker (case- and whitespace-insensitive) |
| `no-undeclared-shared-mutation` (§3.2) | `service: semaphore`, `action` `update_key` / `update_inventory` / `update_repository`, unless approved | `target` (string: the record name) | `target` is not an exact entry in `catalog.semaphore.declared_objects.<kind>` (`key`, `inventory`, `repository`); absent, blank and non-string targets included |

`payload_markers` carries the names of the markers the caller's own scan matched, never a
value: the credential does not leave the runner. The rule catches carelessness, not a
probe value that looks like a real credential.

`declared_objects` lists what the repo declares: the two repository records in
`platform/semaphore/repositories.yml`, and the `production` inventory record, whose content
`platform/semaphore/sync-inventory.yml` pushes from the committed inventory. No key-store
entry is declared, so mutating one needs a human. Nothing yet checks the repository list
against `repositories.yml`.

`human_approved` counts as approval only when it is the boolean `true`. A string such as
`"true"` or `"false"`, or a number, is not approval. This applies to the destructive-template
rule as well.

Proposal JSON Schemas are NOT in `policies/`: OPA loads every JSON file under it as data,
and schema files there fail `opa check` with `merge error`. They live beside the registry.
CI runs `opa check --strict` and `opa test` on this tree.

Full design + phased rollout: `plan/development/03-guardrails-governance.md`.
