## Why

Social posts for the instance's brands are written and scheduled by hand. A text model can draft them, and the platform can check those drafts before a person sees them, but a model's output must never reach a public account without a person's approval. The publishing service already owns the connected accounts, the per-platform publishing code and scheduled publication (change `deploy-postiz-social-publishing`). What is missing is a way to generate and check drafts and to hand them over without bypassing approval.

The earlier flow engine, n8n, was retired on 2026-11-10. Activepieces replaces it (change `deploy-activepieces`), and this change builds on that replacement.

## What Changes

- Adds plan 28, `plan/development/28-social-content-automation.md` (status PROPOSED). It records the pipeline, the decision table, the risks, the open questions and the implementation phases.
- Adds this change: `design.md`, a task breakdown in `tasks.md` with the Plane keys, and the capability spec `platform/social-content-automation`.
- Adds one index line to `AGENTS.md`.
- Deploys nothing, creates no flow, stores no credential and changes no running service. The build that plan 28 describes runs as the tasks in this change, after the changes it depends on land.

## Capabilities

### New Capabilities

- `platform/social-content-automation`: AI-drafted social posts for the instance's brands, each approved by a person before any scheduling; flows and connections as code; an exactly-once handoff to the publishing service; guardrail checks before approval; brand isolation; brand profiles validated against a schema, with values kept private; images approved together with their text; and alerting on failed runs.

### Modified Capabilities

None. The publishing service's own capability is delivered by change `deploy-postiz-social-publishing` and is not changed here.

## Impact

- This commit changes documentation only.
- The planned build adds about 16 files. This count is an estimate, not a measured figure. The files are a brand-profile schema and placeholder example, three flow definitions, two playbooks, a Semaphore template entry, static tests for the flow definitions and the schema, an alert rule, two context documents, an index line and plan tick-offs. The count counts against the release budget, which policy 10 sets at 100 pull requests and 120 changed files per release.
- The build depends on the changes `deploy-postiz-social-publishing`, `deploy-activepieces` and `agentgateway-content-generation`.
- Credentials: the planned build reads two existing OpenBao fields, the `activepieces` client key at `secret/services/agentgateway` and `postiz_api_key` at `secret/services/postiz`. It writes both into Activepieces connections and does not put either in a flow definition. The design declares no new OpenBao path.
- Brand values (names, accounts, mappings, topic backlog, banned terms) live in site-config, not in this repository.
- External, operator-performed prerequisites: the X and LinkedIn developer apps and their connected accounts (Plane AC-760 and AC-761), and the production Postiz API key (AC-763).

## Rollback Plan

This commit is documentation only. Reverting it removes plan 28, this change and the `AGENTS.md` line, and nothing else. No running service depends on it.

The planned build rolls back in layers. Each layer leaves the publishing service in the state it was in before the step.

1. Stop generation and handoff. Stop the flows that the apply playbook owns. The stop mechanism on the Community edition depends on the spike in task 3.1 and is not yet established.
2. Use the kill switch. Plan 28 carries a kill switch over from the lessons of the earlier flow engine. It stops every flow that creates a draft. Its mechanism is not yet designed.
3. Remove the declared flows. Delete the flow definitions from the repository and apply the removal. The apply playbook never deletes a flow it does not own. How it treats a declared flow that has been removed is set in task 3.1.
4. Revoke the connection keys. Revoke the `activepieces` gateway client key with the `Manage agentgateway Client Key` playbook (`action=revoke`, after the identity is removed from the merged identity list, as that playbook requires). The Postiz API key is issued from the Postiz interface. The procedure to revoke it is an open item for task 3.3.
5. Leave published and scheduled content alone. A rollback does not delete drafts, scheduled posts or published posts in the publishing service. Removing them is a separate decision for the operator.
