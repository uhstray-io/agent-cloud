# Tasks: integrate-tududi-github-issue-sync

> Sequenced strictly after `complete-n8n-composable-deployment` — nothing below
> starts until the n8n substrate (API key in OpenBao, credential-provisioning
> pattern) is live in prod.

## 1. Spike: ground both API surfaces (gate for everything after)

- [x] 1.1 Pull the deployed tududi 1.1.1 OpenAPI spec and verify: personal-token
      auth coverage on `/api/v1` task/project/tag routes, the exact task status
      enum, tag read/write shapes, `updated_at` exposure, any changed-since
      filtering, which field can carry the linked issue reference (design D4
      open question), and whether personal API tokens can be minted through the
      API — if they can, token seeding is automated on the n8n key-mint model
      instead of the UI step in 3.1 (design D7)
- [x] 1.2 Verify the n8n public-API workflow surface at the pinned n8n version:
      list/create/update/activate endpoints the provisioning playbook will call
      (design D1)
- [x] 1.3 Write the sync contract doc (beside the Postiz automation contract):
      status↔state mapping table from the real enum, tag↔label rules (sync tag
      never propagates; case-insensitive name match), marker-block format
      (uid + PER-FIELD baselines + both timestamps — one baseline hash per
      synced field, matching design D4/D5), the canonical projection each field
      is hashed FROM (title/description as normalized text, status as the
      MAPPED value from the status table, tags/labels as the case-folded
      sorted name set — so both systems hash the same representation), the
      audit-event key format (design D6), poll cadence with GitHub rate-limit
      arithmetic, and the full-list-diff fallback decision if changed-since
      filtering is absent (design D6, risk 2)
- [x] 1.4 Validation gate: contract doc committed with every table sourced from a
      named spec/endpoint (no guessed enum values); the fields it documents are
      the ones scenarios "GitHub edit reaches tududi" and "tududi edit reaches
      GitHub" will be proven against

## 2. Mapping and workflow definitions as code

- [x] 2.1 Add the mapping declaration (six pairs, `enabled` flag) at the path
      settled in design D3; BATS: exactly the declared six, no credentials or
      addresses in the file
- [x] 2.2 Author the workflow definitions (one per direction) as Jinja2-rendered
      JSON consuming the mapping: tag-gated crossing, marker-block linkage with
      per-field baselines, pre-create search by task `uid` PLUS the
      sync-identity/canonical-title second gate (a suspect match blocks
      creation and records a recovery error — no duplicate even after a fully
      lost linkage), per-field LWW using both systems' own timestamps,
      audit comments carrying the stable audit-event key checked before
      posting (a comment-then-marker-write-failure retry is a no-op),
      losing-value audit comment, un-tag → close-as-not-planned with surviving
      linkage, per-cycle write cap that fails loudly, and no delete operation
      anywhere (design D4/D5/D6, spec)
- [x] 2.3 BATS: rendered workflows reference credentials only by n8n credential
      name — no token value, no secret-store value, in any rendered artifact
- [x] 2.4 Validation gate: rendering the six-pair mapping produces valid workflow
      JSON (schema-checked); scenario "No credential in any committed or logged
      artifact" holds for the repo tree

## 3. Sync identities and credentials

- [x] 3.1 Automate the tududi API token (D7 as amended). IMPLEMENTED DB-SIDE
      (2026-09-03): the deploy is SSO-only by design (PASSWORD_AUTH_ENABLED
      false — the session route answers 403), so `store-tududi-api-token.yml`
      mints through the app's OWN stack instead: `files/tududi-db-mint.js`
      runs inside the container with the app's sequelize models and bcrypt
      (cost 12 + 12-char prefix, matching v1.1.1's createApiToken exactly),
      raw token generated on the runner and passed via stdin, then PROVEN
      end-to-end by a live Bearer call before capture into
      `secret/services/tududi:api_token` via the shared KV-v2 merge.
      NON-DESTRUCTIVE contract per the operator: one INSERT plus the app's
      own reversible revoked_at on our-label rows; no flag flips, no
      restarts. Every token-bearing step `no_log`.
      Idempotent: list-by-label first; an existing label with the OpenBao
      field present is a no-op, an existing label with the field ABSENT is
      revoked and re-minted (the raw value is unrecoverable by design).
      Rotation = revoke + re-run; revocation verified as refused-by-provider,
      already-revoked = success
- [x] 3.2 DONE 2026-09-03 — the App exists: `todo-sync-agent` (App ID 4820206),
      Issues read/write + auto Metadata read, webhook inactive (poll-only D2),
      installed on exactly the eight mapped repos; private key + client id
      seeded to prod OpenBao (tasks 367/368) and local (103/104) via the
      env-secret chain, cleaned after each run. The refresher chain is proven
      live end-to-end on local-dev: named refusal without the key (96), scope
      preflight REFUSING a 2-repo over-scope with names (105 — the guard's
      first catch was real), scope pass at 8==8 and the designed named stop at
      the not-yet-provisioned n8n credential (107). Original scope text
      follows. Create the dedicated GitHub App ("tududi sync": Issues read/write
      ONLY, installed on exactly the mapped repos) — a labelled operator step
      (GitHub has no App-creation API) **gated by machine checks**; seed the
      private key into `secret/services/github:tududi_sync_app_key` and the
      client id into `:tududi_sync_app_client_id` via `Seed OpenBao Key`.
      Write `refresh-tududi-sync-github-token.yml`: mint an installation
      token on the controller (`platform/lib/github_app_token.py`, key via
      stdin), preflight-assert the installation covers exactly the declared
      repos (named failure otherwise), and update the n8n `github-sync-api`
      credential in place (`PATCH /api/v1/credentials/{id}`); declare its
      45-minute Semaphore SCHEDULE as code (`templates.yml` gains a
      `schedule:` field; `setup-templates.yml` learns to upsert schedules)
- [x] 3.3 (DONE 2026-09-03 inside provision-tududi-github-sync.yml — both providers live-validated with named refusals BEFORE any engine write; Validate Secrets wiring rides 6.2's docs pass) Extend the n8n credential-provisioning playbook (or add a sibling) to
      upsert the tududi and GitHub credentials into n8n from those paths —
      idempotent, `no_log` on secret-bearing steps, names-and-counts output —
      and to **precondition-validate both credentials against their live APIs
      first**, failing with a named error before touching n8n when either is
      absent or dead; wire both into the `Validate Secrets` standing check
- [x] 3.4 (DONE 2026-09-03, proven live on local-dev: both credentials exist after one run — task 111; re-run duplicates nothing — 113; a REAL dead token, revoked via the app's own semantic, failed the run BY NAME before any engine change — 115; the mint self-healed and provisioning passed again — 116/117) Validation gate: scenarios "Credentials provisioned as code" and
      "Provisioning refuses dead credentials" hold on local-dev — both
      credentials exist after one run, re-run creates no duplicates, a
      deliberately broken token fails the run with its name before any engine
      change, task output carries no value

## 4. Workflow provisioning playbook + Semaphore template

- [x] 4.1 (DONE 2026-09-03; two implementation findings recorded: the public API's credential schema requires the domain-restriction mode stated — both credentials are pinned to their one legitimate host, same finding the Postiz provisioning hit; and ansible-core 2.16 native-evaluates the rendered JSON, so the parse is version-proofed) Write `provision-tududi-github-sync.yml`: render mapping → upsert both
      workflows by name via the n8n API → activate → **prune owned objects the
      declaration no longer implies** (name-prefix-scoped, never touching
      anything else — design D1); the rollback path (`-e sync_enabled=false`)
      branches BEFORE credential and mapping validation and deactivates the
      owned workflows using only the n8n API key — so rollback works with a
      broken mapping file AND dead/revoked provider credentials (it is the
      one-run kill switch; a validation gate in front of it would defeat it);
      add the Semaphore template (`dev_variant: true`)
- [ ] 4.2 (PARTIAL: ordering/scoping/no_log BATS in place; kill switch proven LIVE with valid inputs — 114; the broken-mapping + dead-credential kill-switch variant remains for the 5.x phase) BATS: provisioning refuses an empty or unparseable mapping;
      `sync_enabled=false` still deactivates both workflows under that same
      broken mapping AND with deliberately dead provider credentials (only the
      n8n API key is exercised on that path); re-run with unchanged inputs
      reports no change; prune only ever names prefix-owned objects
- [x] 4.3 (DONE 2026-09-03: provision green — 111; refresher completed its first full cycle against the provisioned credential — 112; re-provision no-op — 113/117; EMPTY declaration pruned every owned object while the unrelated Postiz credential survived — 118, verified via the API; restore + re-provision — 119) Validation gate: provisioning runs green on local-dev n8n; workflows
      visible, active, re-provisioning is a no-op. Ownership scope is explicit:
      the two workflows and both credentials are SHARED by all pairs (design
      D1/D2/D7) — dropping one pair re-renders the shared workflows with the
      remaining pairs and touches nothing else; the shared objects are pruned
      only when the declaration implies NO pairs at all. Scenario "Removal from
      the declaration removes the engine objects" is proven at both scopes:
      dropping one pair leaves the shared objects present and the remaining
      pairs syncing unchanged; emptying the declaration removes every owned
      object

## 5. Validation against a scratch pair

- [ ] 5.1 Write the per-pair verification check (playbook or workflow step,
      exit pass/fail): last cycle for the pair completed without error, write
      cap not hit, linked pairs' per-field baselines match both sides
      (convergence), zero sync activity on undeclared projects/repos — the
      executable gate both this phase and the prod rollout (6.x) run
- [ ] 5.2 Validate against a scratch pair WITHOUT touching the canonical scope:
      a temporary mapping OVERLAY file (the canonical eight-pair declaration is
      never edited) declaring one scratch tududi project ↔ a private scratch
      repo, and a SEPARATELY SCOPED scratch credential (its own fine-grained
      PAT limited to the scratch repo, seeded at a scratch OpenBao key) — the
      production PAT's six-repo scope stays exactly as declared in design D7
      and cannot see the scratch repo by design. Run several cycles
      exercising: tag→issue creation, an interrupted-creation recovery (delete
      the task-side link, confirm the next cycle relinks instead of
      duplicating), the audit-comment retry (comment posted, marker write
      failed — no duplicate comment on the next cycle), edits in both
      directions on different fields (clean per-field merge), tag/label
      reconciliation, same-field both-sides conflict,
      un-tag→close→re-tag→reopen, archive→close-as-not-planned, untagged task
      untouched, quiet cycle. Cleanup is encoded: remove the overlay, revoke +
      prune the scratch credential, re-provision, and assert the canonical
      mapping still declares exactly eight pairs and the scratch objects are gone
- [ ] 5.3 Validation gate: scenarios "Tagging a task creates its issue exactly
      once", "Interrupted creation recovers without a duplicate", "Removing the
      sync tag closes the issue, destroys nothing", "Linked pairs converge
      bidirectionally" (all three sub-scenarios), "Both sides edited between
      cycles", "Quiet cycle is a no-op", and "Propagated change does not bounce
      back" all pass on the scratch pair WITH the 5.1 check green; remove the
      scratch pair afterwards and confirm scenario "Unmapping is
      non-destructive"

## 6. Production enablement, docs, close-out

- [ ] 6.1 Enable pairs incrementally: `huhhb` first; a pair is promoted only
      when the 5.1 per-pair verification check passes against it, and a failing
      check triggers the rollback path (`sync_enabled=false`) rather than a
      judgment call — then the remaining pairs, same gate each — the PUBLIC agent-cloud pair last, as an explicit publication decision (design Migration
      Plan step 4)
- [ ] 6.2 Docs: CLAUDE.md workflow-table row for the provisioning playbook,
      tududi service context updated with the sync contract pointer, follow-up
      recorded for the webhook transport upgrade (design D2) and the GitHub App
      upgrade path (design D7)
- [ ] 6.3 Retain one outcome memory into the repo's experience bank: whether
      poll-based bidirectional sync held up (worked / dead end / corrected),
      any tududi API constraint discovered the hard way, and the conflict-rate
      reality vs the LWW assumption
- [ ] 6.4 Validation gate: the 5.1 verification check passes for all eight pairs
      (which includes zero sync activity on undeclared projects — scenario
      "Undeclared project is untouched" — and per-pair convergence); BATS +
      pytest suites green; change validated and ready to archive
