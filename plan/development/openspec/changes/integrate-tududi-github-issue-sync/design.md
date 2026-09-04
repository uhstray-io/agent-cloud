# Design: integrate-tududi-github-issue-sync

## Context

See `proposal.md — Why`. Grounding, verified this session:

- tududi deployed at `chrisvel/tududi:1.1.1` (rootless podman, SQLite, OIDC-only,
  `todo.uhstray.io` behind central Caddy). Its upstream API is `/api/v1` with
  `tasks`, `projects`, `tags` modules — task CRUD keyed by `uid`
  (`backend/modules/tasks/routes.js` upstream; the deployed 1.1.1 surface is
  re-verified in the spike below, since upstream `main` is newer). Docs advertise
  personal API tokens + an OpenAPI spec, tags, five task statuses plus archive,
  an audit trail — and no webhooks.
- The weft precedent already reserves the credential path: a personal API key
  minted in the tududi UI post-deploy, stored at
  `secret/services/tududi:api_token`
  (`plan/development/11-tududi-honcho-deployment.md:78`).
- The repo names in the mapping already appear committed in this public repo
  (`add-github-actions-runners/design.md`, plan docs), so the mapping file can
  live here.
- n8n substrate (pending `complete-n8n-composable-deployment`): composable
  deploy, `secret/services/n8n:n8n_api_key`, and the credential-provisioning
  playbook pattern (`provision-n8n-postiz-credential.yml`) this change extends.
- n8n's UI sits behind Caddy forward_auth; its webhook path is not reachable
  from GitHub without an edge exclusion (`local-dev.yml.example:109-111`).

## Goals / Non-Goals

**Goals**
- A tagged tududi task and its GitHub issue converge within one poll cycle, in
  both directions, with zero hand-built workflow state in the n8n UI.
- Everything — mapping, workflows, credentials — placed by playbook, re-runnable,
  removable.

**Non-Goals**
- No webhook transport in v1 (see D2 — deferred, not rejected).
- No comment/attachment sync; no Projects v2. (Subtask sync WAS a non-goal
  here until the operator's 2026-09-03 review — it is now in scope as one level
  of native hierarchy, see D8.)
- No generalized "sync framework" — nine pairs, one workflow shape. Generalize
  when a second integration actually exists.

## Decisions

### D1 — Workflows are code, imported through n8n's API by playbook

The sync workflows are JSON definitions in the repo, rendered (Jinja2) with the
mapping and inventory values, and imported/updated through n8n's public API by a
provisioning playbook authenticated with `secret/services/n8n:n8n_api_key` —
same custody chain the Postiz credential provisioning uses. Idempotent:
list-by-name, update-in-place, never duplicate; activation is part of
provisioning.

Provisioning also **prunes what it owns**: every workflow and credential it
creates carries the sync's name prefix, and a re-run removes any owned object
absent from the current declaration — which is what makes "re-provision against
an empty declaration" a real rollback rather than a claim. Objects without the
prefix are never touched, so an operator's unrelated workflows survive any
provisioning run.

- *Building workflows in the n8n UI* rejected: invisible to review, unreproducible
  on a clean deploy — the exact drift the platform exists to prevent.
- *A dedicated sync service container* rejected: a whole service to own for one
  integration, duplicating the scheduler/credential machinery n8n already has.
- The exact n8n public-API workflow endpoints (import/update/activate shapes)
  are verified against the pinned n8n version during the spike, not assumed.

### D2 — Transport: poll both sides in v1; webhooks are a recorded follow-up

One scheduled workflow per direction (cadence an inventory var; default decided
at implementation against GitHub rate-limit arithmetic). tududi has no webhooks,
so its side is polling regardless.

**Amended at implementation (2026-09-02): one scheduled CYCLE workflow, not
two.** The per-field merge engine (D5) necessarily reads BOTH sides' state
into one snapshot before it can decide any field's winner — so "one workflow
per direction" would run the same bidirectional diff twice, double every
fetch, and open a window where the two runs disagree about the same pair. The
implemented shape is a single scheduled workflow per cycle: fetch both sides
once, compute the converged op set once (`sync/lib/sync-core.js`), execute
tududi-writes and GitHub-writes from that one decision. Direction still
exists — in the ops, not in the schedulers. The kill switch deactivates one
workflow instead of two, which also simplifies the rollback path. GitHub *could* push, but that requires
excluding n8n's `/webhook/*` from forward_auth at Caddy — an edge-surface change
with its own security review (webhook-secret validation as the compensating
control, the same edge-vs-app-gate reasoning Postiz's `/api/public/v1` went
through). v1 keeps the edge closed and accepts poll latency; the webhook upgrade
is recorded as a follow-up, not smuggled in.

- GitHub REST quota: an authenticated poll across the mapped repos on a
  minutes-scale cadence is orders of magnitude inside the documented per-hour
  ceiling; the spike states the arithmetic with the chosen cadence.

### D3 — Mapping is a committed YAML, rendered into the workflows

`platform/services/tududi/sync/github-mapping.yml`: nine entries of
`{ tududi_project, github_repo, enabled }` (grown from six on the operator's
2026-09-03 direction: `Uhstray.io Website ↔ uhstray-io/www` and
`agent-cloud ↔ uhstray-io/agent-cloud` joined; then
`dev-test ↔ uhstray-io/dev-test` as the STANDING validation pair — the
operator provided the repo and its tududi project, the App already covers
it, and a permanent test landing zone replaces the earlier scratch-overlay
plus scratch-credential design entirely: validation writes land in dev-test,
never in a real tracker, using the production credential chain). **agent-cloud is PUBLIC** —
enabling its pair publishes the paired tududi project's task titles,
descriptions and audit comments as world-readable issues; the entry ships
disabled and its flip is a publication decision, recorded in the mapping
itself.
Provisioning renders it into the workflow definitions — the running system's
mapping is exactly what review approved. Repo names are already public in this
repo; no credential or address appears in the mapping.

- *Mapping held in n8n variables/static data* rejected: mutable outside review.
- *Mapping in site-config* rejected: nothing in it is private, and keeping it
  beside the workflow code that consumes it is one fewer cross-repo seam.

### D4 — Linkage lives in the artifacts themselves; the sync stays stateless

An issue created from a task carries a machine-readable marker block in its body
(HTML comment): the tududi task `uid`, **per-field baselines** — a canonical
hash of each synced field (title, description, status, tags/labels) as of the
last sync — and the last-synced timestamps for both sides. The tududi task
carries the issue reference on its own record (which field — description suffix
vs a dedicated field — is settled by the spike against the 1.1.1 OpenAPI spec).
Any cycle can rebuild its entire working state by reading the two systems.

Creation is idempotent through the marker, not through memory: before creating
an issue for a tagged task, the cycle searches the paired repository for an
existing issue whose marker carries that task's `uid`. So the
created-issue-but-failed-to-link crash window cannot produce a duplicate — the
next cycle finds the orphan by `uid` and completes the linkage instead of
re-creating. The same search is the recovery path for a corrupted marker whose
`uid` line survives; a marker damaged beyond that is logged for a human, never
guessed at.

The `uid` search has a blind spot the design does not paper over: an issue
whose marker was destroyed is invisible to it. So creation carries a second,
deterministic gate — before creating, the cycle also searches the paired
repository's **unlinked open issues, whoever authored them**, for a
canonical-title match. *As amended 2026-09-03:* exactly one hit is **adopted** —
the marker is written onto that issue, the link is announced once with a keyed
comment, and because the marker starts with empty baselines every differing
field is a plain LWW conflict whose loser is preserved. More than one hit
**blocks creation and records a recovery error naming every artifact** (task
`uid`, every issue number); the cycle never guesses. Only when both searches
come back empty is an issue created. (The original text blocked on *any* hit
and searched only sync-authored issues; that made "the same item filed on both
sides" a permanent stall and gave GitHub-origin work no path in at all.)

The same gate runs in the other direction. An open, unlinked issue authored by
anyone but the sync identity is **GitHub-origin work**: the cycle creates a
task for it in the paired project — tagged for sync, labels as tags — and the
executor writes the marker back onto the issue with the new task's `uid`
(the engine emits the marker with a placeholder the executor fills, so the
engine stays free of tududi identifiers it has not seen). Before creating, it
looks for a same-title task in the project: an untagged twin means a person
kept that task private, so creation is **blocked with a recovery error** naming
both rather than publishing by accident; a tagged twin is handled by the
adoption above. Closed unlinked issues are never backfilled. A sync-authored
unlinked open issue is the orphan of an interrupted tududi-origin creation and
is reported, never re-imported. Two more findings are reported instead of
resolved: a marker naming a task the project does not hold (**dangling** —
deleted, moved, or written by a different tududi instance, which is exactly
what a local-dev cycle against a real repository leaves behind, so a real pair
is only ever enabled on the instance that holds its project), and one marker
carried by two issues (a copy-pasted body). The latter was found by the
scenario tests: an index keyed by `uid` had been keeping only the last issue,
which dropped the first out of the linked set and re-created it as a task.

The residual duplicate window is therefore a destroyed marker AND a retitled
issue — at which point the two artifacts share nothing machine-readable and
re-linking is genuinely a human judgment.

- *External mapping store* (n8n static data, a DB) rejected: invisible state
  that can drift from both systems and dies with the engine; the artifacts are
  the durable record.
- The marker block doubles as the echo/no-op guard (D5) — one mechanism, two
  jobs.

### D5 — Echo suppression: dedicated identities + marker-hash comparison

Both credentials belong to a dedicated sync identity (a tududi user whose token
the sync uses; the GitHub PAT's account). A cycle ignores changes whose author
is the sync identity, and independently skips any write for a field whose
current hash equals that field's marker baseline — so even if authorship
filtering fails (tududi's audit-trail exposure through the API is verified in
the spike, not assumed), the baselines make a quiet cycle a no-op and kill
ping-pong loops.

Last-writer-wins resolves **per field**, against the per-field baselines from
D4: a field changed on only one side simply propagates, so both sides editing
*different* fields merges cleanly with nothing overwritten — only the same
field changed on both sides is a conflict, and there the newer side's
`updated_at` wins (the spec's conflict scenario is per-field for exactly this
reason). Timestamps compared are always the two systems' own `updated_at`
values against the marker's recorded pair — never the poller's wall clock, so
clock skew between the poller and either system cannot flip a conflict.

### D6 — Status and tag mapping are documented tables, built from the real enums

The tududi-status ↔ issue-state table and the tag↔label rules are written during
the spike from the deployed 1.1.1 API's actual status enum (docs say five levels
plus archive; the exact names come from the spec, not memory), and land in the
sync contract doc next to the Postiz automation contract. Fixed rules decided
now: the sync tag itself is a control marker and is never propagated as a
label; label/tag comparison is name-based and case-insensitive; a task archived
or deleted in tududi closes its issue as not-planned with an audit comment (the
sync never deletes an issue or a task — spec requirement). **Removing the sync
tag gets the same treatment as archive**: the issue is closed as not-planned
with an audit comment naming the un-tagging, the task itself is untouched, and
the marker linkage survives — re-adding the tag reopens the same issue rather
than creating a duplicate. Un-tagging expresses "stop tracking this in GitHub",
and a closed issue with an audit trail says that; a silently stale open issue
does not. The close is a sync write, so it moves the marker's status baseline
with it (amended 2026-09-03): that is what lets the ordinary per-field loop
reopen on re-tag, and what tells the sync's close apart from a human closing
the same issue as not-planned on GitHub — theirs leaves the baseline at open,
reads as a GitHub-side change, and cancels the task instead of being reverted.
The first cut carried a "task open + issue not-planned ⇒ re-tag" heuristic
instead; live it reopened a human's close (dev-test #2, cycle 118).

**Audit comments are idempotent, not just informative.** Every audit comment
(losing-value preservation, un-tag, archive) embeds a stable audit-event key —
derived from the task `uid`, the event type, and the triggering side's
`updated_at` — and a cycle checks the issue's existing comments for that key
before posting. So the comment-posted-but-marker-rewrite-failed crash window
retries into a no-op instead of a duplicate comment; the retry completes the
marker write and moves on. The comment-then-marker ordering is fixed (comment
first, marker second) precisely so the key check is the only dedupe needed.

### D7 — GitHub credential: a dedicated GitHub App, not a PAT (operator decision 2026-09-03)

**Superseding the original PAT choice on the operator's direction.** A
dedicated GitHub App ("tududi sync"), Issues read/write only, installed on
exactly the mapped repositories. Its private key lives at
`secret/services/github:tududi_sync_app_key` (client id beside it as
`tududi_sync_app_client_id` — the JWT issuer, not a secret), seeded via the
existing `Seed OpenBao Key` mechanism. The platform already owns the App
credential machinery: `platform/lib/github_app_token.py` (the runner-automation
chain) signs the JWT and mints installation tokens, key via stdin.

**The 1-hour token problem, solved as code.** Installation access tokens
expire after one hour, so the n8n credential cannot be a static value. A
refresher playbook (`refresh-tududi-sync-github-token.yml`) mints a fresh
installation token on the controller and updates the n8n `github-sync-api`
credential in place through the public API (`PATCH /api/v1/credentials/{id}`,
verified at the pinned n8n version) — run on a **Semaphore schedule** (45-minute
cron, declared as code in `templates.yml`; `setup-templates.yml` gains schedule
support so the cadence is reviewed, not clicked). A refresh failure surfaces as
a failed scheduled task AND as the sync's own 401s — loud twice.

**What the App buys over the PAT** (beyond the operator's call): issue writes
are authored by `<app-slug>[bot]` — a crisp, unforgeable sync identity for D5's
authorship filtering; per-installation scoping is enforced by GitHub rather
than by a token's good behaviour; and revocation is instant App-side.

Credential lifecycle honesty, unchanged in spirit: **creating and installing
the App is a GitHub-settings operation** (no public creation API) — a labelled
operator step gated by machine checks (the refresher fails with a named error
when the key is absent/dead, and preflight-validates the installation covers
exactly the declared repos). The tududi token mint IS automated: the spike
found the full lifecycle API (`POST /api/profile/api-keys`, session+CSRF), so
`store-tududi-api-token.yml` mints and captures it on the
`store-n8n-api-key.yml` model — the session login uses the deploy's
break-glass local account, whose password is already OpenBao-held.
Provisioning still precondition-validates both credentials against their live
APIs before touching n8n; both are wired into the `Validate Secrets` standing
check.

- *Fine-grained PAT* (the original decision) superseded: token hygiene
  favoured the App once the operator weighed it, and the App's bot identity
  strengthens echo suppression.
- *Reusing the runner-automation App* rejected: it would couple CI-runner
  administration to a standing issue-writing automation and widen that App's
  permissions; two Apps, two blast radii.
- *tududi sync identity*: v1 mints the token under the existing break-glass
  service account (tududi 1.1.1 exposes no user-creation API — the profile
  surface is self-service only). GitHub-side authorship is the App bot; the
  per-field baselines (D5) remain the primary echo suppression on the tududi
  side. A dedicated tududi user is a follow-up if its API grows creation.

### D8 — Hierarchy: tududi subtasks ↔ GitHub sub-issues, native on both sides (operator decision 2026-09-03)

**Decision.** One tududi subtask corresponds to one GitHub **sub-issue** of the
issue its parent task is linked to — never a checklist line in the parent's
body, never a flat issue with a "parent:" note. Both systems have a first-class
hierarchy feature (tududi `parent_task_id`, GitHub `sub_issues`) and the sync
uses each one natively, in both directions. A child pair carries the same marker,
the same four fields and the same per-field baselines as a top-level pair (D4,
D5); hierarchy is one extra edge, not a second engine.

**Rules, in the order the engine applies them.**

1. **A subtask crosses only when it is itself tagged AND its parent is linked.**
   The sync tag is per item, not inherited: a subtask without `gh-sync` is never
   exported, whatever its parent carries (operator answer 5, 2026-09-03). A
   tagged subtask whose parent is not yet a linked pair is *deferred*, counted
   as `skipped_parent_unlinked`, and picked up on a later cycle once the parent
   has linked — the parent's own creation is the ordinary tagged-task path, so
   a parent and child tagged in the same minute converge within two cycles.
2. **tududi → GitHub creation is two hops, not one call.** The child issue is
   created with `create_issue` exactly like a top-level issue, then attached
   with a new op `add_sub_issue` (`POST /issues/{parent_number}/sub_issues`,
   `sub_issue_id` = the child issue's `id`). The attach is **engine-driven from
   observed state**: on every cycle, a linked child issue that the per-pair
   parent map shows as top-level gets an `add_sub_issue` op. That one rule
   covers the initial attach, an executor that died between the two hops, and
   a human detaching the sub-issue by hand — the sync re-attaches, because the
   tududi parent is the declared truth. The cost is a ≤1-cadence window in
   which a freshly created child is visible top-level; the upgrade path, if
   that window ever matters, is a same-cycle executor hop that attaches right
   after create.
3. **GitHub → tududi creation waits for the parent.** An unlinked open
   sub-issue (one the parent map names a parent for) becomes a `create_task` carrying
   `parent_task_id` = the numeric `id` of the task its parent issue is linked
   to — the one place the engine needs tududi's integer id rather than the
   `uid`. If the parent issue is not linked, the child waits and is counted
   under `skipped_parent_unlinked`; it is not imported top-level and re-parented
   later, because tududi has no re-parenting in this design (rule 5).
4. **Adoption and shadow gates are scoped to the parent.** The canonical-title
   adoption of D4 runs for children too, but only among the sub-issues of the
   linked parent issue (and, mirrored, only among the subtasks of the linked
   parent task). A same-title item under a *different* parent is not a twin. A
   linked child whose observed parent disagrees with the marker's parent —
   moved on GitHub, or re-parented in tududi — is reported as a recovery error
   `hierarchy drift` naming both parents, and nothing is written for that pair
   until a human resolves it.
5. **Out of scope for v1, recorded, not rejected:** re-parenting (moving a
   subtask to another parent), depth greater than one (tududi allows it, GitHub
   allows up to eight, the sync mirrors one level), and deletes (the sync has
   no delete operation at any level — same as D4).

**Status needs no new rule.** tududi's own parent/child automation (all
children done → parent done; parent done → children done; parent cancelled →
open children cancelled) fires inside tududi and surfaces to the sync as
ordinary `status` field changes with fresh `updated_at`, which D5's per-field
baselines already propagate. GitHub's `sub_issues_summary` is derived state and
is never written.

**Read side: one extra call per pair, not per item.** tududi's task list
already embeds each parent's `subtasks[]` with full fields, so the fan-out node
flattens `subtasks[]` into the task stream with `parent_uid`/`parent_id` and
each child's own `tagged` at no cost. GitHub is different from what the docs
imply: the flat REST issue list DOES carry `parent_issue_url` — but **only under
a user token**. Under the GitHub App installation token the sync runs with, the
field is `null` on every sub-issue (measured 2026-09-03 on dev-test #12: REST
list and `GET /issues/8/sub_issues` both null it, while `sub_issues_summary` on
the parent counts it and the same query under a user token populates it). The
first live cycle after wiring therefore saw no hierarchy at all. The parent map
comes instead from **one GraphQL query per pair** (`repository { issues(first:
100) { nodes { number parent { number } } } }`), which the App token does
answer, run as its own node between the fan-out and the REST fetch and keyed by
`nameWithOwner` so it never index-aligns with the split REST stream. The
alternative — one `GET /issues/{parent}/sub_issues` per parent with
`sub_issues_summary.total > 0` — costs N calls instead of one and, with zero
parents, produces an empty item stream that stops the downstream nodes from
running at all; it lost on both counts. Parsing `parent_issue_url` is now
forbidden by the render check and the BATS suite.

**Alternatives rejected.**
- *Checklist lines in the parent issue body* — invisible to GitHub's own
  hierarchy views, un-linkable per item, and a body-text merge problem every
  cycle. Rejected outright; the operator asked for the native feature.
- *Attach in the same executor hop as create* — fewer visible-top-level
  seconds, but the attach can still fail after the create succeeds, so the
  observed-state re-attach rule is needed anyway; adding the hop on top is the
  documented upgrade, not the v1.
- *Import an orphaned sub-issue top-level, re-parent later* — needs
  re-parenting, which this design deliberately does not have; waiting is
  simpler and loses nothing but a cycle.
- *Inherit the tag from the parent* — rejected by the operator: the tag is the
  per-item publication consent, and a private subtask under a public parent is
  a legitimate state.

**Related confirmations recorded the same day.** The sync tag stays `gh-sync`
on every instance (the operator's "github-sync" wording named the concept; the
existing default is kept, no rename or migration); GitHub gets no marker label
— the body marker is the linkage; description propagation is a plain field
under D5's last-writer-wins with the loser preserved in a comment, which the
operator accepted for descriptions specifically.

### D9 — Priority is a synced field; the subtask gate is inherited; GitHub-origin work lands PLANNED (operator decisions 2026-09-04)

Three operator rules from the same review pass, each resting on something
measured rather than assumed.

1. **Priority syncs, and `Urgent` folds onto `high`.** GitHub carries priority
   as an org-level native issue field (single-select: Urgent/High/Medium/Low);
   tududi's `Task.PRIORITY` is LOW/MEDIUM/HIGH plus `null`. tududi has no
   Urgent, so the operator's rule is Urgent → high. Placing that fold in the
   **canonical projection** rather than in the write path is what makes the
   lossy pair safe both ways: an Urgent issue whose task reads `high` is a
   *quiet* field, so a tududi `high` can never demote it back to High; lowering
   the task to medium or low genuinely differs and does reach GitHub. Proven
   live — a cycle with #9 at `Urgent` and its task at `high` emitted zero ops.

   Two API facts forced design, and both contradict a plain docs reading:
   - The PATCH **replaces** the whole `issue_field_values` set. A
     priority-only write silently cleared an unrelated `Effort` value on a
     probe issue. Every priority write therefore echoes the issue's other
     field values back, and clearing priority means omitting only its entry —
     the same discipline the engine already applies to tududi's `subtasks[]`.
   - Writing needs the numeric `field_id`, and `/orgs/{org}/issue-fields`
     answers 403 for an App installation token, so the sync cannot discover
     it. The id is declared as `github_priority_field_id` in the mapping
     (org-level config, not a credential); `null` leaves priority out of the
     sync entirely rather than reading it one way and dropping it the other. A
     declared id that does not match an issue's real Priority field is a named
     recovery error that refuses the cycle — the alternative is writing into
     some unrelated field.

2. **A subtask inherits its parent's tag**, superseding D8's per-item gate.
   The premise of that gate turned out to be false: tududi 1.1.1 has no UI
   path to tag a subtask at all. The inline subtask editor sends no `tags` and
   the backend whitelists fields that exclude them, and clicking a subtask row
   opens the *parent's* page — nothing links to the child's own `/task/<uid>`
   view, which is the only surface that can tag it. Keeping the per-item rule
   would have left the tududi→GitHub direction reachable only through the API.
   Tagging the parent is the explicit opt-in for the whole item, whose issue is
   already public; a child of an untagged parent still exports nothing, and a
   child may still carry the tag itself.

3. **GitHub-origin work lands PLANNED (6), not NOT_STARTED (0).** It is
   scheduled work someone else filed, not something the owner has picked up.
   Both map to `open`, so the projection is unchanged and the pair is quiet on
   arrival — the rule costs nothing in the merge engine.

## Risks / Trade-offs

- **[Poll latency window invites concurrent edits]** → last-writer-wins with the
  losing value preserved in an issue comment (spec); cadence tightening and the
  webhook follow-up (D2) shrink the window if it bites in practice.
- **[tududi 1.1.1 API gaps — token auth coverage, updated-since filtering,
  audit exposure]** → the spike is a gate, not a formality: if the deployed
  version can't answer "what changed since T" efficiently, the workflow falls
  back to full-list diffing against marker hashes (viable at personal-tracker
  scale), and that decision is recorded in the contract doc.
- **[A rogue or buggy cycle mass-edits the mapped repos]** → dedicated identity makes
  every write attributable and filterable; the sync has no delete operation by
  design; per-cycle write caps in the workflow fail the run loudly rather than
  fan out damage.
- **[Marker block corrupted or hand-edited in an issue body]** → recovery is
  the D4 `uid` search: if the marker's `uid` line survives, the cycle re-links
  and rewrites a clean marker; damaged beyond that, it logs for a human and
  never guesses. A fully lost marker is caught by D4's second gate — the
  canonical-title search adopts the one matching issue and re-links it; with
  two candidates it records a recovery error rather than duplicating, and the
  pair stalls loudly until a human re-links.
- **[n8n substrate slips]** → this change is sequenced strictly after
  `complete-n8n-composable-deployment`; nothing here can start until that
  change's credential-provisioning pattern exists in prod.

## Migration Plan

1. Spike + contract doc (API surfaces, mapping tables, cadence arithmetic).
2. Land mapping, workflow definitions, provisioning playbook, Semaphore
   template, tests — through the normal branch flow.
3. Stand up the sync identities and credentials (tududi user + token → OpenBao;
   fine-grained PAT → OpenBao); provision into n8n.
4. Enable one low-traffic pair first (a scratch/test pair during validation,
   then `huhhb`), observe several cycles, then declare the remaining pairs.
5. Rollback per `proposal.md — Rollback Plan` (deactivate workflows; unmapping
   is non-destructive by spec).

## Open Questions

- Default poll cadence (5 vs 15 minutes) — decided at implementation with the
  rate-limit arithmetic in front of us; changes nothing structural.
- Which tududi field carries the issue reference (description suffix vs a
  dedicated field the 1.1.1 API exposes) — spike answers it; both variants
  satisfy the spec.
