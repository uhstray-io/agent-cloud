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
      canonical-title second gate (AMENDED 2026-09-03: one same-title unlinked
      open issue is ADOPTED and linked in place; two or more block with a
      recovery error — no duplicate even after a fully lost linkage), the
      reverse direction (an open human-filed unlinked issue becomes a tagged
      task, marker written back with the new uid; an untagged same-title task
      blocks it; closed issues never backfilled; dangling and duplicated
      markers reported), per-field LWW using both systems' own timestamps,
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
      Idempotent, PROOF-FIRST (amended 2026-09-03): the STORED OpenBao value
      is fed back through `prove` — accepted live AND matching an active
      labelled row of the configured user — and only then is the run a no-op;
      anything else (field absent, value dead, value minted for a different
      user) revokes our labelled rows and re-mints (the raw value is
      unrecoverable by design). Presence was not convergence: "row active +
      field present" reported converged while OpenBao held the previous
      identity's token (task 160); the proof-first rule caught it (166) and
      re-runs are no-ops (167). The proof runs INSIDE the container on
      loopback — the public edge URL is unreachable from the orchestrator on
      local-dev and leaves the VM on prod; every earlier green run had been
      carrying a launch-time URL override (task history 93/94/116).
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

- [x] 5.1 (DONE 2026-09-03: verify-tududi-github-sync.yml + lib/verify-pair.js —
      the verdict is rendered by the EXACT embedded engine inside the tududi
      container; read-only; per enabled pair. Proven live: its first run
      CAUGHT a real defect — scheduled cycles were failing because the local
      overlay gave the app the shared network but not the WORKER, which is
      what executes scheduled workflows in queue mode; overlay fixed, the
      next scheduled cycle ran green against local tududi + real GitHub
      read-only, and the gate PASSED — task 122. Behavioural BATS fixtures
      cover pass/converged, failed-cycle, divergence, undeclared-trace)
      Write the per-pair verification check (playbook or workflow step,
      exit pass/fail): last cycle for the pair completed without error, write
      cap not hit, linked pairs' per-field baselines match both sides
      (convergence), zero sync activity on undeclared projects/repos — the
      executable gate both this phase and the prod rollout (6.x) run
- [ ] 5.2 (PARTIAL 2026-09-03 — REDESIGNED: `uhstray-io/dev-test` ↔ tududi
      `dev-test` is a STANDING enabled pair in the canonical mapping, covered by
      the App's own installation — no overlay file and no scratch PAT, because
      the App IS the identity and its installation list is the scope (the
      refresher's preflight requires installation == mapping, so a pair outside
      the declaration cannot exist). Run live against real GitHub, local
      tududi+n8n at 1-min cadence, all with the 5.1 gate green afterwards:
      tag→creation by `todo-sync-agent[bot]` with marker+labels; quiet cycle;
      GitHub label → tududi tag (sync tag preserved, tududi-shaped `{name}`
      objects); same-field title conflict → newer side won, loser preserved in
      ONE keyed comment, no repeat next cycle; un-tag→closed not_planned→re-tag→
      reopened with the task's status UNTOUCHED; archive→close, unarchive→
      reopen; untagged task never produced an issue. Four engine bugs found and
      fixed by this run (index-aligned merge duplicating issues; wrong tududi
      write route; tags sent as strings; reopen bouncing status 0→5→1), each
      with a red-then-green scenario in core-scenarios.js. NOT run live:
      interrupted-creation recovery, and the audit-comment retry — the compute
      node passes `comments: []`, so keyed comment idempotency is INERT live
      (success-path idempotency is state-driven and held; the gap bites only
      on a partial-failure retry). Fix path: fetch comments for LINKED issues
      only, or move the key into the marker.) Validate against a scratch pair WITHOUT touching the canonical scope:
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
- [ ] 5.3 (PARTIAL 2026-09-03, third pass — status crossings and identity.
      GitHub-origin status replay passed LIVE on dev-test (cycles 128–132):
      reopen → task IN_PROGRESS, task DONE → issue closed/completed, reopen
      again, task CANCELLED → closed/not_planned, then a quiet cycle with zero
      writes; label removal on GitHub → tag removed (119); close → DONE (123).
      Four engine fixes came out of it, each with a unit scenario: a human
      close bounced back open (cycle 118 — the per-field baseline now moves
      with the write); an AMBIGUOUS adoption then fell through to create
      (dedup hole — ambiguity is a named recovery error, never a create);
      tududi's default task list hides DONE rows, so a finished task was
      invisible to convergence (`?status=all`); and, symmetric to it, finished
      tududi work was never exported (`skipped_finished` guard).
      IDENTITY DECISION, proven from v1.1.1 source and live: tududi scopes
      every list per user and creation writes no permission row, so a task
      created by a grantee in the owner's project is INVISIBLE to the owner's
      list (owner token listed 4 of 5 dev-test tasks, missing the grantee's).
      A service account with rw shares therefore loses; the sync identity is
      the tududi user who OWNS the mapped projects — `tududi_sync_user_email`
      in inventory (site-config on prod; the baked local INI in
      `bootstrap-local-dev.yml` for the Semaphore tier, NOT
      `platform/inventory/local-dev.yml`). Provisioning refuses an enabled
      pair whose project the identity cannot see, by name. Re-minted as the
      dev-test owner (166), re-provisioned (168); the first cycle under that
      token was green (134) and a one-cycle owner smoke passed both ways (135:
      tagged task → #11, human issue #10 → tagged task, both in the owner's
      list). Battery lesson: a wait shorter than the cycle cadence reports a
      false timeout — R0 of the replay was that artifact, not a defect.
      Second pass: the amended creation scenarios
      passed LIVE on dev-test — "Issue filed on GitHub becomes a task" (#8 →
      tagged task, marker written back with the real uid via the executor's
      item link, quiet for the 11 following cycles), "Same item created on both
      sides is linked, not duplicated" (#9 adopted, `linked` + preserved-loser
      comments, LWW by a 1-second edge), and "GitHub edit reaches tududi" for
      title and body; the 5.1 gate PASSED afterwards — task 148. The dangling-
      and duplicate-marker rows are unit-proven only (scenarios 15–16; 16 found
      a real duplicate path in the uid index). Status/label crossings from
      GitHub (close→2, reopen→1, not_planned→5, label removal) were in flight
      when this note was written — see the next note. First pass: every listed
      scenario EXCEPT "Interrupted
      creation recovers without a duplicate" passed live on dev-test; the 5.1
      gate PASSED for both enabled pairs after the whole battery — task 135 —
      once its tududi transport was corrected to the SAME inventory variable
      the cycle workflow uses (it had borrowed `tududi_base_url`, the app's
      public edge URL, unreachable from inside the orchestrator; MISTAKES
      1.x). "Unmapping is non-destructive" was proven in 4.3 — 118.) Validation gate: scenarios "Tagging a task creates its issue exactly
      once", "Interrupted creation recovers without a duplicate", "Removing the
      sync tag closes the issue, destroys nothing", "Linked pairs converge
      bidirectionally" (all three sub-scenarios), "Both sides edited between
      cycles", "Quiet cycle is a no-op", and "Propagated change does not bounce
      back" all pass on the scratch pair WITH the 5.1 check green; remove the
      scratch pair afterwards and confirm scenario "Unmapping is
      non-destructive"

## 5H. Hierarchy sync — subtasks ↔ sub-issues (design D8; operator review 2026-09-03)

Scoped on local-dev against `dev-test`, BEFORE the prod rollout (operator
ordering). The three review items resolved to: hierarchy = new build (5H.1–5H.6);
description sync = already implemented but never proven live (5H.7); tag gate =
already implemented, tag stays `gh-sync`, no GitHub label (5H.8, confirm only).

- [x] 5H.1 Engine (`sync-core.js`): tasks carry `parent_uid`/`parent_id`, issues
      carry `parent_number`/`id`; rule 1 — tagged child + linked parent only,
      else `skipped_parent_unlinked`; rule 2 — `add_sub_issue` op whenever a
      linked child issue shows no `parent_number`; rule 3 — GitHub-origin
      sub-issue → `create_task` with `parent_task_id`, deferred while the
      parent issue is unlinked; rule 4 — adoption/shadow gates scoped to the
      linked parent's children, `hierarchy drift` recovery error on mismatch.
      No new field, no new status rule (tududi's auto-completion arrives as
      ordinary status changes)
- [x] 5H.2 Unit scenarios (`tests/core-scenarios.js`): child create tududi→GitHub
      (create + attach on the next cycle), child create GitHub→tududi with
      `parent_task_id`, untagged child ignored under a linked parent, child
      deferred while parent unlinked (both origins), detached child
      re-attached, hierarchy drift reported with zero ops, quiet cycle with a
      linked child pair = zero ops
- [x] 5H.3 Workflow (`tududi-github-sync.workflow.json.j2`): fan-out flattens
      each task's embedded `subtasks[]` into the stream with `parent_uid`,
      `parent_id` and the child's OWN `tagged`; issue map adds `id` and
      `parent_number`; GitHub executor gains the
      `add_sub_issue` route (`POST /issues/{parent_number}/sub_issues`,
      `{sub_issue_id}`); tududi executor passes `parent_task_id` through on
      `create_task`. AMENDED 2026-09-03 after the first live cycle: the REST
      list's `parent_issue_url` is NULL under the App installation token, so
      `parent_number` comes from one GraphQL query per pair (`Fetch sub-issue
      parents` node), not from parsing that field — one extra call per pair,
      not per item; parsing `parent_issue_url` is now forbidden by render-check
      and BATS
- [x] 5H.4 Contract doc (`github-sync-contract.md`): hierarchy section — the
      five D8 rules, the `skipped_parent_unlinked` counter, the `hierarchy
      drift` error, the ≤1-cadence top-level window and its upgrade path
- [x] 5H.5 BATS (`test_tududi_sync.bats`): the workflow template routes
      `add_sub_issue`, flattens `subtasks`, and never sends `subtasks: []` on a
      PATCH (tududi replaces the whole set); the executor still has no delete
      route at any level
- [x] 5H.6 Live proof on dev-test, both directions (DONE 2026-09-03, execs
      154–159): (a) tagging subtask `1ezzbrhadrq5hs8` under linked parent
      `xpugd26ef1a0wy6` created issue #14 (exec 155) and attached it under #9
      the next cycle (exec 156, `add_sub_issue`); `GET /issues/9/sub_issues` →
      `[14]`. (b) hand-filed sub-issue #13 under linked #8 became subtask
      `3w04q6inh0xypib` of `scsrj80r7auindn`, tagged `gh-sync`,
      `parent_task_id` set (exec 155). (c) untagged subtask
      `3ef680snzmxnt6e` under the same parent produced zero ops (exec 159).
      (d) quiet cycles after (a)+(b) = 0 ops (execs 158, 159). (e) 5.1 gate
      PASSED — task 182. The `hierarchy drift` recovery error fired exactly as
      designed on the pre-existing probe (#12 ↔ `f39ipt4616rycn4`, execs 154
      and 155, naming BOTH the wrong parent and the right one), and cleared
      once the task was moved under `scsrj80r7auindn`; the gate FAILED while it
      stood (task 181) and passed after — the gate's own negative proof
- [x] 5H.7 Live proof of description sync, both directions, on one linked pair
      (DONE 2026-09-03, exec 157): tududi note on `scsrj80r7auindn` → #8's body
      updated; #9's body edited on GitHub → `xpugd26ef1a0wy6`'s note updated;
      both in the SAME cycle (3 ops), and the next cycle wrote nothing (exec
      158, 0 ops) — spec scenario "Description edited on either side reaches
      the other"
- [x] 5H.8 Tag gate confirmed as deployed, nothing changed (2026-09-03): both
      `provision-tududi-github-sync.yml:50` and `verify-tududi-github-sync.yml:39`
      default `_sync_tag` to `gh-sync` with NO inventory override anywhere under
      `platform/inventory/`; GitHub-origin tasks arrive tagged
      (`sync-core.js:635` `.concat(syncTag)`, live: `3w04q6inh0xypib` carries
      `gh-sync`); untagged tududi tasks never export (`q0umw8eixjx01tr` and
      `3ef680snzmxnt6e`, zero ops across every cycle); no GitHub label is added
      — `projectLabels()` filters the sync tag out of the label projection, and
      issue #14, created from a tagged task, carries `labels: []`
- [ ] 5H.9 Update the published sync report (`tududi-github-sync-report.html`,
      same artifact URL) with the hierarchy design, the 5H.6/5H.7 evidence and
      the prod-rollout blockers; commit + push on the feature branch

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
