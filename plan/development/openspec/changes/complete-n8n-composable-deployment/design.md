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
- **Live prod layout, verified by SSH probe 2026-09-01** (this invalidated the
  original cutover premise — see D6): prod n8n is NOT the legacy monorepo
  `deploy.sh` path. It is a standalone rootless-podman compose project at
  `/home/<ansible_user>/n8n` (source of record: the operator's
  `dev-test/deployments/n8n`), project name `n8n`, four containers
  (`n8n_n8n_1`, `n8n_n8n-worker_1`, `n8n_postgres_1`, `n8n_redis_1` — queue
  mode), image `docker.n8n.io/n8nio/n8n:latest` frozen at pull time =
  **n8n 2.8.3**, up 6 months. Its `.env` carries all three stateful values,
  but the encryption key is named **`ENCRYPTION_KEY`** (compose maps it:
  `N8N_ENCRYPTION_KEY=${ENCRYPTION_KEY}`). `POSTGRES_USER=n8n_admin`,
  `POSTGRES_DB=n8n`, `POSTGRES_NON_ROOT_USER=n8n_user` — identical to what
  `n8n.env.j2` renders, so a dump/restore needs no identity mapping. Old
  volumes are `n8n_db_storage` / `n8n_n8n_storage` / `n8n_redis_storage`;
  the composable compose declares `n8n_db_data` / `n8n_data` /
  `n8n_redis_data` — **different volumes, so an env-only cutover would boot
  an empty database**.

## Goals / Non-Goals

**Goals**
- The production cutover executes with zero stateful-secret regeneration and a
  mechanical guard (env diff) rather than operator vigilance.
- Everything n8n needs to talk to Postiz — node, API key custody, credential —
  is code, runnable from Semaphore, idempotent.

**Non-Goals**
- No n8n workflows are authored here (the substrate is the deliverable).
- No queue-mode/scaling changes; the 4-container topology stands.
- No NocoDB work, including its `generate_nocodb_env()` — RETIRED (decision
  2026-09-01: NocoDB is removed from the platform, replaced by tududi), not
  deferred silently: PR #15's close-out comment records it, and the
  decommission is scoped as its own change.

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

### D2 — API-key MINT + capture: authenticated REST, `no_log`, Semaphore-run

`store-n8n-api-key.yml` mints the key itself instead of waiting on a manual UI
step (review finding on this change, corrected against n8n@2.25.7 source): the
playbook logs in as the seeded owner (credentials already in OpenBao), calls the
session-authenticated internal endpoint `POST /rest/api-keys`
(`ApiKeysController`, `@GlobalScope('apiKey:manage')`, 404 unless the public
API is enabled), takes `rawApiKey` from the creation response, and merges it
into `secret/services/n8n:n8n_api_key` (KV-v2 merge-patch via the shared
`tasks/bao-merge-keys.yml`, siblings preserved) — every credential-bearing step
`no_log`, task output carrying names and counts only. Idempotent: it first
lists existing keys and creates only if no key with our fixed label exists —
every POST inserts a NEW key, so an unconditional mint would pile them up. NOTE
the endpoint surface, verified at the pinned tag: the PUBLIC `/api/v1` spec has
no api-keys paths; `/rest/api-keys` with a session cookie is the only mint
route. The DB stores the raw JWT itself (`ApiKey.apiKey`, looked up by raw
value), so a read-back verification against n8n's own Postgres remains possible.

- *REST mint as PR #15 did it* (`POST /rest/...` with owner session + printing
  `N8N_API_KEY=...` to stdout) stays rejected FOR THE PRINTING, not the minting:
  it wrote a live credential into the Semaphore run record, which v2.17 cannot
  clear without deleting the task. D2 keeps the mint and removes the print —
  `rawApiKey` goes from the response straight to OpenBao under `no_log`.
- *Manual UI mint + DB read-out* (this design's first draft) rejected as the
  primary path: it works (the DB holds the raw key), but it leaves a human step
  in an otherwise fully orchestrated chain, and the operator sees the key in the
  UI — one more pair of eyes the automated mint never exposes it to.
- *Skipping the key entirely* (CLI `import:credentials` inside the container)
  rejected for credential provisioning too — see D3.
- Storage semantics verified at n8n@2.25.7 (`public-api-key.service.ts`): the
  `ApiKey` row stores the raw JWT and is queried by raw value — no hashing — so
  the read-back verification is a plain equality check. The creation response is
  the only place `rawApiKey` appears unredacted through the API; listing
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

**Amendment (2026-09-01, after the layout probe):** the live file names the
encryption key `ENCRYPTION_KEY` while the rendered file names it
`N8N_ENCRYPTION_KEY`, so a literal same-name diff can never verify the one
value that matters most. The shared guard and the seed playbook both accept a
per-key **live-name alias** (live `ENCRYPTION_KEY` ↔ rendered
`N8N_ENCRYPTION_KEY`, defaulting to the same name), keeping the names-only /
fail-closed / all-occurrences properties intact. Alias support lands in the
shared task, not as an n8n special case — NocoDB's held migration gets it for
free.

### D5 — Sequencing: local greenfield proves everything before prod is touched

Local-dev (greenfield, no stateful history) validates the full chain — deploy,
node reconciliation, key capture, credential provisioning, Postiz connectivity —
via the `(Dev)` Semaphore variants. Only then does the prod sequence run:
seed → verify pre-existing → deploy (guarded) → validate workflows decrypt →
node/key/credential. Cleanup (`generate_n8n_env()` removal, HOLD lift, PR #15
close) comes last, after prod validation.

### D6 — Cutover data path: pg_dump → restore into the new stack

The composable stack's volumes are new, so the cutover must MOVE the data, not
just the env values. Chosen: dump the live database from the old project's
Postgres container, bring the composable stack up, restore the dump into its
Postgres, and let n8n 2.25.7 run its own schema migrations over the restored
2.8.3-era data at startup. The old compose project is **stopped, never
destroyed** — containers and volumes stay on disk as the rollback copy until
close-out. The old n8n binds `0.0.0.0:5678`, so it must be down before the new
stack starts.

- *Adopting the old volumes* rejected: forks the compose naming (violates
  one-codebase/no-forks), couples the new stack to legacy volume names forever,
  and — decisive — leaves **no untouched rollback copy** while the 2.8.3→2.25.7
  migrations rewrite the only schema in place.
- *Greenfield + workflow export/import* rejected: loses all execution history
  and every stored credential has to be re-entered by hand in the UI — a manual
  step the platform bans when a mechanical path exists.
- The dump lands in an owner-only (0700) directory on the VM, the file itself
  umask'd to 0600 — owned by the same unprivileged account that runs the
  rootless containers, matching the platform's become-false posture. It
  contains workflow definitions plus credentials **encrypted under the key
  OpenBao holds** — data, not cleartext secrets; the backup report states
  where it is and that it is the rollback artifact.

### D7 — Backup/restore is a standing upgrade capability, not a one-shot

Requirement stated by the operator at design revision: the cutover mechanics
must be reusable for every future n8n upgrade. So the dump and restore are
**two independent Semaphore-runnable playbooks**, parameterized by container
name / compose project, and the cutover is merely their first composition:

- `backup-n8n-db.yml` — timestamped `pg_dump` from a named Postgres container
  (defaults to the composable stack; the cutover points it at the legacy
  `n8n_postgres_1`). Run before **every** version bump.
- `restore-n8n-db.yml` — stop the stack's n8n app containers, restore a named
  dump into its Postgres, start them again (n8n re-runs migrations at boot).
  This is both the cutover's import step and the standing rollback tool.

The future upgrade runbook this yields: `Back Up n8n DB` → bump `n8n_image` in
inventory → guarded `Deploy n8n` → if migrations fail, re-pin the old image and
`Restore n8n DB`. One-shot alternatives (inline dump/restore steps buried in a
migration-only playbook) rejected: they would leave the next upgrade with no
tool and this platform treats that as a defect.

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
- **[2.8.3 → 2.25.7 is a large one-way migration jump]** → the dump taken by
  D6/D7 IS the rollback (restore + restart the old stack); the deploy watches
  n8n's startup for migration failure instead of assuming green; the old stack
  and volumes stay untouched until close-out.
- **[Dump file retention on the VM]** → owner-only `0700` directory owned by the
  rootless deployment account, named in the run
  report; removed at close-out once prod validation passes, never silently.
- **[Two n8n processes race the boot-migration chain]** (found live in the
  cutover: app and worker started together, a crash mid-chain left a column
  added but unrecorded, and every later boot failed on "already exists") →
  the worker is gated behind the app's READINESS at every layer that starts
  them: compose declares `depends_on: n8n: condition: service_healthy` over a
  new `/healthz/readiness` healthcheck, deploy.sh serializes app-then-worker
  itself (depends_on conditions cannot be relied on across engines), and the
  restore's `always:` starts the app alone, waits for readiness, then the
  worker. Plain `/healthz` answers 200 while migrations run, so it is never
  the gate.

## Migration Plan

1. Land code + templates on the feature branch → `dev` (local-dev validation).
2. Prod, in order, each independently retryable, with an explicit operator gate
   before the old stack is stopped: `Seed n8n Secrets`
   (`-e live_n8n_env=/home/<ansible_user>/n8n/.env`, alias-aware) → `Check Secrets`
   (verify pre-existing) → `Back Up n8n DB` (pointed at legacy
   `n8n_postgres_1`) → **GATE: operator go** → stop the legacy project →
   guarded `Deploy n8n` → `Restore n8n DB` → operator confirms workflows
   execute and stored credentials decrypt → `Store n8n API Key` →
   `Provision n8n Postiz Credential`.
3. Cleanup PR: remove `generate_n8n_env()`, lift the HOLD in
   `09-service-migrations-tooling.md`, close PR #15 (user-gated), remove the
   cutover dump after validation.
4. Rollback: see `proposal.md — Rollback Plan` (amended: legacy = the
   `/home/<ansible_user>/n8n` compose project, restartable in place).

## Open Questions

- Whether the local-dev Postiz instance is up when this lands (it was deployed
  2026-08-30; if it has been torn down, the local credential test degrades to
  asserting the credential exists rather than `is-connected` passing) — safe to
  answer at implementation.
