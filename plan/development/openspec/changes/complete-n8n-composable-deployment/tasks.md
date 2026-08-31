# Tasks: complete-n8n-composable-deployment

## 1. Community node as code

- [ ] 1.1 Pin `n8n-nodes-postiz` 0.2.17: fetch its `dist.integrity` checksum from the
      npm registry and add `N8N_COMMUNITY_PACKAGES_ENABLED`,
      `N8N_COMMUNITY_PACKAGES_MANAGED_BY_ENV=true`, and the `N8N_COMMUNITY_PACKAGES`
      JSON declaration to `n8n.env.j2` (values parameterized through inventory vars,
      no fork between local and prod)
- [ ] 1.2 Confirm both n8n containers (app + worker) see the reconciled package via the
      shared `n8n_data` volume; wire any env var the worker needs into `compose.yml`
      anchors rather than duplicating
- [ ] 1.3 Extend `platform/tests/test_service_n8n.bats`: the template declares the
      managed-package variables, pins name+version+checksum, and contains no literal
      secrets
- [ ] 1.4 Validation gate: BATS suite green; scenario "Declared node present after
      redeploy" holds on local-dev — clean deploy, node installed at 0.2.17, UI
      install disabled

## 2. API-key capture into OpenBao

- [ ] 2.1 Verify (against the deployed n8n 2.25.7 schema) the table/column holding
      public-API keys; record it in the playbook header comment with the version it
      was verified against
- [ ] 2.2 Write `store-n8n-api-key.yml` on the `store-postiz-api-key.yml` pattern:
      read the key from n8n's own Postgres, KV-v2 merge-patch into
      `secret/services/n8n:n8n_api_key`, every key-bearing step `no_log`, output
      restricted to field names and counts; assert-and-fail with a named error when
      the schema or key is absent
- [ ] 2.3 Add BATS coverage: all key-bearing tasks carry `no_log: true` (scoped to
      those tasks only), no `debug` task can render the key
- [ ] 2.4 Validation gate: scenario "Key capture leaves no trace in the run record"
      holds — local run stores the key, Semaphore task output shows names/counts only

## 3. Postiz credential provisioned in n8n

- [ ] 3.1 Verify the n8n public-API credential endpoints (list/create/update shapes)
      against the docs for the pinned version; record the verified routes in the
      playbook header
- [ ] 3.2 Write `provision-n8n-postiz-credential.yml`: authenticate with
      `secret/services/n8n:n8n_api_key`, shared-read
      `secret/services/postiz:postiz_api_key` (never copied to another path),
      upsert one `postizApi` credential with `Host` from inventory
      (`https://postiz.uhstray.io/api` prod, local Postiz host in local-dev);
      list-then-upsert so a re-run never duplicates; secret-bearing steps `no_log`;
      the report restates the 90/hour creation ceiling from the Postiz contract
- [ ] 3.3 Validation gate: scenarios "Credential works against the self-hosted
      instance" and "Provisioning is idempotent" hold on local-dev — `is-connected`
      test passes (or, if local Postiz is down, the credential exists with the right
      host and the degraded check is noted), second run creates nothing new

## 4. Semaphore templates

- [ ] 4.1 Add `Seed n8n Secrets`, `Clean Deploy n8n` (marked destructive in its
      description), `Store n8n API Key`, and `Provision n8n Postiz Credential` to
      `platform/semaphore/templates.yml` with `dev_variant` where the local flow
      needs them; run `setup-templates.yml` locally
- [ ] 4.2 Validation gate: scenario "Pre-seed and clean deploy exist as orchestrator
      tasks" holds — both templates visible in Semaphore, destructive one labeled

## 5. Local-dev end-to-end proof

- [ ] 5.1 Greenfield local run of the full chain via the `(Dev)` templates: deploy →
      healthz → forward_auth gate → owner seeded → node reconciled → operator mints
      key → capture → credential provisioned
- [ ] 5.2 Validation gate: scenario "Idempotent redeploy after cutover" holds locally —
      second deploy run reports no stateful change

## 6. Production cutover (the HELD migration)

- [ ] 6.1 Add the mechanical cutover guard to the prod path (design D4): compare the
      three stateful values in the newly rendered env against the live
      `config/n8n.env` and fail before any container restart on mismatch, printing
      key names only
- [ ] 6.2 Run `Seed n8n Secrets` against the live host; verify with `Check Secrets`
      that `encryption_key`, `db_admin_password`, `db_user_password` all resolve as
      pre-existing
- [ ] 6.3 Run the guarded `Deploy n8n` cutover; operator confirms existing workflows
      execute and stored credentials decrypt
- [ ] 6.4 Run the Postiz-substrate sequence in prod: node reconciliation (same deploy),
      operator mints the API key, `Store n8n API Key`,
      `Provision n8n Postiz Credential`
- [ ] 6.5 Validation gate: scenarios "Pre-seed makes the first composable deploy a
      fetch", "Cutover refuses to proceed on a stateful mismatch" (proven by the
      guard's test, not by breaking prod), and "Idempotent redeploy after cutover"
      hold on the production host

## 7. Cleanup and close-out

- [ ] 7.1 Remove `generate_n8n_env()` from `platform/lib/common.sh` (leave
      `generate_nocodb_env()` — NocoDB is paused); fix any callers/tests
- [ ] 7.2 Update docs: CLAUDE.md workflow table rows for the new playbooks, lift the
      n8n half of the HOLD in `plan/development/09-service-migrations-tooling.md`
      (NocoDB half stays held), n8n deployment README/context notes
- [ ] 7.3 Close PR #15 as superseded (USER-GATED — ask before closing), with a comment
      recording what superseded each part and that the NocoDB half is paused, not
      discarded
- [ ] 7.4 Retain one outcome memory into the repo's experience bank: whether the
      cutover preserved live state, labelled worked / dead end / corrected, with the
      root cause of anything that failed
- [ ] 7.5 Validation gate: scenario "Re-running a completed sequence changes nothing"
      holds — `Deploy n8n` re-run against finished prod reports no change; BATS +
      pytest suites green; change validated and ready to archive
