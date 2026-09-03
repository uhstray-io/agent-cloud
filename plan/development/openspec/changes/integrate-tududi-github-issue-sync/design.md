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
- The six repo names in the mapping already appear committed in this public repo
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
- No comment/attachment/subtask sync; no Projects v2.
- No generalized "sync framework" — six pairs, one workflow shape. Generalize
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

- GitHub REST quota: a fine-grained-PAT poll across six repos on a
  minutes-scale cadence is orders of magnitude inside the documented per-hour
  ceiling; the spike states the arithmetic with the chosen cadence.

### D3 — Mapping is a committed YAML, rendered into the workflows

`platform/services/tududi/sync/github-mapping.yml` (exact path settled at
implementation): six entries of `{ tududi_project, github_repo, enabled }`.
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
whose marker was destroyed AND whose task-side link is gone is invisible to
it. So creation carries a second, deterministic gate — before creating, the
cycle also searches the paired repository's open issues **authored by the sync
identity** for a canonical-title match. A hit there **blocks creation and
records a recovery error naming both artifacts** (task `uid`, issue number)
for a human to re-link or rename; creation never proceeds past a suspect
match. Only when both searches come back empty is an issue created. The
residual duplicate window is therefore a doubly-destroyed linkage AND a
retitled issue — at which point the two artifacts share nothing machine-readable
and re-linking is genuinely a human judgment.

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
does not.

**Audit comments are idempotent, not just informative.** Every audit comment
(losing-value preservation, un-tag, archive) embeds a stable audit-event key —
derived from the task `uid`, the event type, and the triggering side's
`updated_at` — and a cycle checks the issue's existing comments for that key
before posting. So the comment-posted-but-marker-rewrite-failed crash window
retries into a no-op instead of a duplicate comment; the retry completes the
marker write and moves on. The comment-then-marker ordering is fixed (comment
first, marker second) precisely so the key check is the only dedupe needed.

### D7 — GitHub credential: fine-grained PAT scoped to the six repos, in OpenBao

A fine-grained personal access token restricted to the six repositories with
Issues read/write, stored at **`secret/services/github:tududi_sync_pat`**
(seeded via the existing `Seed OpenBao Key` mechanism), provisioned into n8n by
playbook.

Credential lifecycle honesty: creating a fine-grained PAT is an operation on
GitHub's own settings surface, and the tududi token mint is UI-side unless the
spike finds a token-mint route in the 1.1.1 API (spike task — if one exists,
the mint is automated on the n8n `store-n8n-api-key.yml` model). What IS
encoded either way: the provisioning playbook **precondition-validates both
credentials against their live APIs before touching n8n** and fails with a
named error when one is absent or dead, re-validation is a standing check
(`Validate Secrets` pattern), and the revocation steps for both providers are
documented next to the seeding steps. Provider-side creation that cannot be
API-driven is a labelled operator step with a deterministic gate behind it —
never an unstated assumption.

- *Reusing the existing platform PAT* (`secret/services/github`) rejected:
  over-scoped for a standing automation that writes to repos on a timer.
- *A GitHub App* deferred: better token hygiene at meaningful scale, but its
  minting machinery (the runner-automation App chain) is heavier than six repos
  of issue sync justifies today. Recorded as the upgrade path if repo count
  grows.

## Risks / Trade-offs

- **[Poll latency window invites concurrent edits]** → last-writer-wins with the
  losing value preserved in an issue comment (spec); cadence tightening and the
  webhook follow-up (D2) shrink the window if it bites in practice.
- **[tududi 1.1.1 API gaps — token auth coverage, updated-since filtering,
  audit exposure]** → the spike is a gate, not a formality: if the deployed
  version can't answer "what changed since T" efficiently, the workflow falls
  back to full-list diffing against marker hashes (viable at personal-tracker
  scale), and that decision is recorded in the contract doc.
- **[A rogue or buggy cycle mass-edits six repos]** → dedicated identity makes
  every write attributable and filterable; the sync has no delete operation by
  design; per-cycle write caps in the workflow fail the run loudly rather than
  fan out damage.
- **[Marker block corrupted or hand-edited in an issue body]** → recovery is
  the D4 `uid` search: if the marker's `uid` line survives, the cycle re-links
  and rewrites a clean marker; damaged beyond that, it logs for a human and
  never guesses. A fully lost linkage (marker AND task-side link both gone) is
  caught by D4's second gate — the sync-identity/canonical-title search blocks
  creation and records a recovery error rather than duplicating; the pair
  stalls loudly until a human re-links.
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
