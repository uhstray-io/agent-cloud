# Design: complete-n8n-composable-deployment

## Context

See `proposal.md — Why`. Current state, verified on `main`:

- `deploy-n8n.yml` (3-phase composable, owner seeding, encryption-key
  preservation), `seed-n8n-secrets.yml` (migration Task 0), `clean-deploy-n8n.yml`,
  `update-n8n.yml`, `n8n.env.j2`, `compose.yml` + `compose.local.yml` all exist.
- `platform/semaphore/templates.yml` declares only `Deploy n8n` and `Update n8n`.
- Production n8n still runs the legacy path; `09-service-migrations-tooling.md`
  holds the cutover. The image is pinned (`docker.n8n.io/n8nio/n8n:2.25.7` default,
  overridable via `n8n_image`) precisely so the cutover doesn't compound with an
  upgrade.
- Postiz's side is complete: API live at `https://postiz.uhstray.io/api/public/v1`,
  key captured at `secret/services/postiz:postiz_api_key`, contract in
  `platform/services/postiz/context/use-cases.md` (90/hour creation ceiling,
  public-host path is deliberate, shared-read only).
- `n8n-nodes-postiz` v0.2.17 (npm, published 2025-10-07) is a verified community
  node; its `PostizApi` credential carries an `apiKey` plus a `Host` field
  (default `https://api.postiz.com`), and its connection test calls
  `{host}/public/v1/is-connected` — so self-hosted works with
  `Host = https://postiz.uhstray.io/api`.
- Community n8n has no SSO; access is gated by Caddy forward_auth (Authentik),
  and the composable deploy seeds an owner account so an SSO'd admin never sees
  `/setup`.

## Goals / Non-Goals

**Goals**
- The production cutover executes with zero stateful-secret regeneration and a
  mechanical guard (env diff) rather than operator vigilance.
- Everything n8n needs to talk to Postiz — node, API key custody, credential —
  is code, runnable from Semaphore, idempotent.

**Non-Goals**
- No n8n workflows are authored here (the substrate is the deliverable).
- No queue-mode/scaling changes; the 4-container topology stands.
- No NocoDB work, including its `generate_nocodb_env()` — paused, not deferred
  silently: PR #15's close-out comment records it.

## Decisions

### D1 — Community node install: env-managed reconciliation, not UI, not image bake

`N8N_COMMUNITY_PACKAGES_ENABLED=true` + `N8N_COMMUNITY_PACKAGES_MANAGED_BY_ENV=true`
+ `N8N_COMMUNITY_PACKAGES` (JSON array pinning `n8n-nodes-postiz` name, version
0.2.17, checksum) in the templated env. Reconciliation at startup installs the
pinned version, removes anything undeclared, and disables UI installs — the exact
"declared, not clicked" posture the platform requires.

- *UI install* rejected: not reproducible, drifts, survives in nobody's history.
- *Custom image bake* rejected: forks the upstream image and couples node updates
  to image rebuilds; the env list keeps one codebase with a parameter.
- Both n8n containers (app + worker) mount the shared `n8n_data` volume at
  `/home/node/.n8n`, where community packages land — one declaration covers both.
  Verify the worker picks the node up at implementation (it executes the
  workflows, so this is the half that matters).
- The checksum value comes from the npm registry (`npm view n8n-nodes-postiz
  dist.integrity`) at implementation time and is pinned in the template alongside
  the version.

### D2 — API-key capture: from the service's own database, `no_log`, Semaphore-run

`store-n8n-api-key.yml` follows `store-postiz-api-key.yml`: the operator mints
the key once in the n8n UI (Settings → n8n API), then the playbook reads it from
n8n's own Postgres and merges it into `secret/services/n8n` (KV-v2 merge-patch,
siblings preserved), with every key-bearing step `no_log`. Task output carries
names and counts only.

- *REST mint* (PR #15's approach: `POST /rest/...` with owner session + printing
  `N8N_API_KEY=...` to stdout) rejected: it printed a live credential into the
  Semaphore run record — the exact failure `store-postiz-api-key.yml` exists to
  prevent, since Semaphore v2.17 cannot clear a task's output without deleting
  the task.
- *Skipping the key entirely* (CLI `import:credentials` inside the container)
  rejected for credential provisioning too — see D3.
- The exact table/column holding n8n API keys is read from the deployed schema at
  implementation (unverified today; the task carries the verification step).
  n8n API keys are displayed once at creation in the UI — the DB read is what
  makes capture repeatable without re-minting.

### D3 — Postiz credential provisioning: n8n public API, not CLI import

A provisioning playbook authenticates to the n8n public API with the captured
`n8n_api_key`, lists existing credentials, and creates (or updates in place) one
`postizApi` credential — `apiKey` shared-read from
`secret/services/postiz:postiz_api_key`, `Host` from inventory
(`https://postiz.uhstray.io/api` in prod; the local Postiz hostname in local-dev).

- *`n8n import:credentials` CLI* rejected: it requires writing a decrypted
  credential JSON to disk inside the container — an intermediary secret file,
  banned by the credential-flow rules. The API path keeps the secret in Ansible
  memory end to end.
- List-then-upsert gives idempotency; the API surface for credentials is
  verified against the n8n public-API docs at implementation.
- The key is never copied to `secret/services/n8n` — single custody, per the
  Postiz contract.

### D4 — Cutover guard: diff the rendered env before restart, in the playbook

Migration Task 0's "diff the rendered .env against the current one" becomes a
task, not an instruction: the cutover path compares the three stateful values in
the newly rendered env against the live file and **fails before any container
restart** on mismatch, printing key names only. Operator vigilance is the thing
that fails at 2am; the diff is mechanical.

### D5 — Sequencing: local greenfield proves everything before prod is touched

Local-dev (greenfield, no stateful history) validates the full chain — deploy,
node reconciliation, key capture, credential provisioning, Postiz connectivity —
via the `(Dev)` Semaphore variants. Only then does the prod sequence run:
seed → verify pre-existing → deploy (guarded) → validate workflows decrypt →
node/key/credential. Cleanup (`generate_n8n_env()` removal, HOLD lift, PR #15
close) comes last, after prod validation.

## Risks / Trade-offs

- **[Encryption-key regeneration bricks stored credentials]** → the seed→verify→
  guarded-deploy sequence; the guard (D4) fails closed; the legacy path stays
  intact until the cleanup phase.
- **[Community-node reconciliation removes an operator-installed node]** →
  intended behavior, but announced: the deploy's report step lists the declared
  set; anything an operator needs must land in the declaration.
- **[n8n version pin vs node compatibility]** → `n8n-nodes-postiz` pins
  `n8nNodesApiVersion: 1`; validated on 2.25.7 locally before prod. A node/API
  mismatch shows up in the local phase, not in prod.
- **[Postiz rate ceiling (90/h) hit by future workflows]** → out of scope here,
  but the credential provisioning report restates the ceiling so workflow
  authors inherit the contract.
- **[DB schema drift for the API-key read]** → the capture playbook asserts the
  table/column exists and fails with a named error rather than an empty secret.

## Migration Plan

1. Land code + templates on the feature branch → `dev` (local-dev validation).
2. Prod, in order, each independently retryable: `Seed n8n Secrets` →
   `Check Secrets` (verify pre-existing) → `Deploy n8n` (guarded cutover) →
   operator confirms workflows/credentials intact → node reconciliation lands via
   the same deploy → operator mints API key → `Store n8n API Key` →
   `Provision n8n Postiz Credential`.
3. Cleanup PR: remove `generate_n8n_env()`, lift the HOLD in
   `09-service-migrations-tooling.md`, close PR #15 (user-gated).
4. Rollback: see `proposal.md — Rollback Plan`.

## Open Questions

- Whether the local-dev Postiz instance is up when this lands (it was deployed
  2026-08-30; if it has been torn down, the local credential test degrades to
  asserting the credential exists rather than `is-connected` passing) — safe to
  answer at implementation.
