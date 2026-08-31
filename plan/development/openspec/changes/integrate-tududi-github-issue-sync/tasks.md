# Tasks: integrate-tududi-github-issue-sync

> Sequenced strictly after `complete-n8n-composable-deployment` — nothing below
> starts until the n8n substrate (API key in OpenBao, credential-provisioning
> pattern) is live in prod.

## 1. Spike: ground both API surfaces (gate for everything after)

- [ ] 1.1 Pull the deployed tududi 1.1.1 OpenAPI spec and verify: personal-token
      auth coverage on `/api/v1` task/project/tag routes, the exact task status
      enum, tag read/write shapes, `updated_at` exposure, any changed-since
      filtering, and which field can carry the linked issue reference (design D4
      open question)
- [ ] 1.2 Verify the n8n public-API workflow surface at the pinned n8n version:
      list/create/update/activate endpoints the provisioning playbook will call
      (design D1)
- [ ] 1.3 Write the sync contract doc (beside the Postiz automation contract):
      status↔state mapping table from the real enum, tag↔label rules (sync tag
      never propagates; case-insensitive name match), marker-block format
      (uid + last-synced hash + both timestamps), poll cadence with GitHub
      rate-limit arithmetic, and the full-list-diff fallback decision if
      changed-since filtering is absent (design D6, risk 2)
- [ ] 1.4 Validation gate: contract doc committed with every table sourced from a
      named spec/endpoint (no guessed enum values); the fields it documents are
      the ones scenarios "GitHub edit reaches tududi" and "tududi edit reaches
      GitHub" will be proven against

## 2. Mapping and workflow definitions as code

- [ ] 2.1 Add the mapping declaration (six pairs, `enabled` flag) at the path
      settled in design D3; BATS: exactly the declared six, no credentials or
      addresses in the file
- [ ] 2.2 Author the workflow definitions (one per direction) as Jinja2-rendered
      JSON consuming the mapping: tag-gated crossing, marker-block linkage,
      hash-based echo suppression, LWW using both systems' own timestamps,
      losing-value audit comment, per-cycle write cap that fails loudly, and no
      delete operation anywhere (design D4/D5, spec)
- [ ] 2.3 BATS: rendered workflows reference credentials only by n8n credential
      name — no token value, no secret-store value, in any rendered artifact
- [ ] 2.4 Validation gate: rendering the six-pair mapping produces valid workflow
      JSON (schema-checked); scenario "No credential in any committed or logged
      artifact" holds for the repo tree

## 3. Sync identities and credentials

- [ ] 3.1 Create the dedicated tududi sync user, mint its personal API token in
      the UI, store at `secret/services/tududi:api_token` via `Seed OpenBao Key`
      (value as env secret, never a task parameter)
- [ ] 3.2 Create the fine-grained GitHub PAT (six repos, Issues read/write) under
      the sync's GitHub identity; seed into OpenBao under the field named in
      design D7
- [ ] 3.3 Extend the n8n credential-provisioning playbook (or add a sibling) to
      upsert the tududi and GitHub credentials into n8n from those paths —
      idempotent, `no_log` on secret-bearing steps, names-and-counts output
- [ ] 3.4 Validation gate: scenario "Credentials provisioned as code" holds on
      local-dev — both credentials exist after one run, re-run creates no
      duplicates, task output carries no value

## 4. Workflow provisioning playbook + Semaphore template

- [ ] 4.1 Write `provision-tududi-github-sync.yml`: render mapping → upsert both
      workflows by name via the n8n API → activate; deactivation path for
      rollback (`-e sync_enabled=false`); add the Semaphore template
      (`dev_variant: true`)
- [ ] 4.2 BATS: playbook refuses to run with an empty or unparseable mapping;
      re-run with unchanged inputs reports no change
- [ ] 4.3 Validation gate: provisioning runs green on local-dev n8n; workflows
      visible, active, and re-provisioning is a no-op

## 5. Validation against a scratch pair

- [ ] 5.1 Add a temporary seventh pair (scratch tududi project ↔ a private
      scratch repo) to the mapping on the feature branch; run several cycles
      exercising: tag→issue creation, edits in both directions, tag/label
      reconciliation, both-sides conflict, archive→close-as-not-planned,
      untagged task untouched, quiet cycle
- [ ] 5.2 Validation gate: scenarios "Tagging a task creates its issue exactly
      once", "Linked pairs converge bidirectionally" (all three sub-scenarios),
      "Both sides edited between cycles", "Quiet cycle is a no-op", and
      "Propagated change does not bounce back" all observed on the scratch pair;
      remove the scratch pair afterwards and confirm scenario "Unmapping is
      non-destructive"

## 6. Production enablement, docs, close-out

- [ ] 6.1 Enable pairs incrementally: `huhhb` first, observe cycles, then the
      remaining five (design Migration Plan step 4)
- [ ] 6.2 Docs: CLAUDE.md workflow-table row for the provisioning playbook,
      tududi service context updated with the sync contract pointer, follow-up
      recorded for the webhook transport upgrade (design D2) and the GitHub App
      upgrade path (design D7)
- [ ] 6.3 Retain one outcome memory into the repo's experience bank: whether
      poll-based bidirectional sync held up (worked / dead end / corrected),
      any tududi API constraint discovered the hard way, and the conflict-rate
      reality vs the LWW assumption
- [ ] 6.4 Validation gate: scenario "Undeclared project is untouched" spot-checked
      in prod (a non-declared project shows zero sync activity across cycles);
      all six pairs converging; BATS + pytest suites green; change validated and
      ready to archive
