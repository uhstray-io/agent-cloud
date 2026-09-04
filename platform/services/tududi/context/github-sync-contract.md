# tududi ↔ GitHub issue-sync contract

The automation contract for `integrate-tududi-github-issue-sync` (design D1–D7),
written from the spike against the **deployed tududi 1.1.1** and the upstream
source at tag `v1.1.1` — every table below names its source. The companion
Postiz contract (`platform/services/postiz/context/use-cases.md`) is the model.

## Verified tududi API surface (source: tag v1.1.1 unless noted)

| Fact | Value | Source |
|------|-------|--------|
| API base | `/api/v1` | `backend/config/swagger.js` (`API_BASE_PATH`) |
| Auth for automation | `Authorization: Bearer tt_<64 hex>` personal API token (opaque, NOT a JWT despite the swagger scheme's `bearerFormat`) | `backend/middleware/auth.js:47-54` → `findValidTokenByValue`; token shape `backend/modules/users/apiTokenService.js:22` |
| Unauthenticated API behaviour | 401 `{"error":"Authentication required"}` | live probe of the deployed instance, 2026-09-02 |
| Token lifecycle API | `GET/POST /api/profile/api-keys`, `POST /api/profile/api-keys/{id}/revoke`, `DELETE /api/profile/api-keys/{id}` — NOTE the `/api/` prefix, not `/api/v1/` (`getApiPath` prefixes bare `api/`); session+CSRF authenticated (login `POST /api/login`, CSRF `GET /api/csrf-token` → `{csrfToken}` sent as `x-csrf-token`); `createApiToken` accepts `expiresAt` and `abilities` | `frontend/config/paths.ts:82-90`; `frontend/utils/apiKeysService.ts`; `frontend/utils/csrfService.ts`; `backend/modules/users/apiTokenService.js` |
| **Token mint is automatable — DB-SIDE, not through the session API** | The session→CSRF→`POST /api/profile/api-keys` route above is UNREACHABLE on this deploy: `PASSWORD_AUTH_ENABLED=false` makes login SSO-only and the session route answers 403, so the documented API path cannot mint a token here. `store-tududi-api-token.yml` therefore mints through the app's OWN stack instead — `files/tududi-db-mint.js` runs inside the container against its sequelize models and bcrypt (matching v1.1.1's `createApiToken` exactly: cost 12, 12-char prefix), the raw token is generated on the runner and passed via stdin, PROVEN by a live Bearer call, and stored at `secret/services/tududi:api_token`. Design D7's open question: answered | above + `secret/services/tududi` break-glass entry (AGENTS.md secrets table) |
| **Whose token — the project OWNER's, not a service account's** | Every list is scoped per user: a token sees its user's own resources plus those shared TO that user (`ownershipOrPermissionWhere`; admin widens NOTHING — the comment at `:120-123` says so on purpose). A project share cascades `rw` to the tasks that exist at share time, and a grantee sees every later task in the shared project too (`project_id IN sharedProjects` arm) and may create/PATCH there (`validateProjectAccess`, `requireTaskWriteAccess` → `getAccess` inherits from the project). BUT a task the grantee creates gets `user_id` = grantee and no permission row, and the owner's list has no "tasks in projects I own" arm — so a GitHub-origin task created by a service account is INVISIBLE to the person who owns the project. Also `sharesService` hands `isAdmin` a numeric id where it expects a uid, so an admin cannot grant shares on others' projects (live: `403 Forbidden`). Hence `tududi_sync_user_email` (inventory) names the owner of the mapped projects, and provisioning refuses an enabled pair the token cannot see | `backend/services/permissionsService.js:17-96,98-170`; `modules/tasks/utils/validation.js:4-28`; `modules/tasks/middleware/access.js:12-19`; `modules/tasks/routes.js:405-450` (create writes no `Permission`; only `modules/shares` does, via `services/execAction.js`); `services/rolesService.js:3-15`; live share/403 probes 2026-09-03 |
| GitHub credential | A dedicated GitHub App (Issues read/write, the mapped repos), NOT a PAT — operator decision 2026-09-03. Installation tokens live 1 hour, so `refresh-tududi-sync-github-token.yml` re-mints on a 45-minute Semaphore schedule and PATCHes the n8n credential in place; issue writes are authored by the App's `[bot]` login (the D5 authorship identity) | design D7 (amended); `platform/lib/github_app_token.py` |
| Task WRITE route | `PATCH /api/task/{uid}` — NOTE the SINGULAR `/api/task/` (like `POST /api/task` create), distinct from the PLURAL `GET /api/v1/tasks` list. The write node PATCHes `/api/task/{uid}`; the list node GETs `/api/v1/tasks` | `backend/modules/tasks/routes.js:539` (patch), `:405` (create) |
| Task identity | `uid` (model default via `backend/utils/uid`), exposed by the serializer | `backend/models/task.js:13-17`; `backend/modules/tasks/core/serializers.js:84` |
| Issue-reference carrier | the task `note` field (TEXT) — no dedicated external-link field exists at 1.1.1. Design D4's open question: answered (marker suffix in `note`) | `backend/models/task.js:53` |
| Changed-since filtering | **ABSENT** — `GET /api/v1/tasks` accepts `type, groupBy, maxDays, order_by, include_lists, limit, offset` only. The full-list-diff fallback (design risk 2) is therefore the v1 reality, not a contingency | `backend/modules/tasks/routes.js:209-222` |
| **Status visibility of the list** | The bare list HIDES DONE (`status NOT IN (2)`); `?status=done` returns DONE+ARCHIVED only; `?status=all` is the ONLY query with no status filter (statuses 0–6). The cycle's fetch node uses `?status=all` — without it a completed task drops out of the diff, its issue is never closed, a reopen never reaches it, and its still-open issue reads as a dangling link (found live 2026-09-03, cycle 123) | `backend/modules/tasks/queries/query-builders.js:301-327` (default `type` branch); live probe 2026-09-03 |
| `updated_at` exposure | model uses Sequelize default timestamps; route code exposes snake-case `updated_at` on related objects. **Verify the task-level field name live with the sync token before the workflows hash it** (phase 3 preflight) | `backend/modules/tasks/routes.js:355`; model has no explicit timestamp config |
| Tags | `GET`/`POST /api/v1/tags` (+ tag objects nested on tasks, sorted by the serializer) | `backend/docs/swagger/tags.js`; serializer `sortTags` |

## Status mapping (source: `backend/models/task.js:281-289` — the model, not the stale swagger enum)

tududi `status` is an INTEGER 0–6. The swagger doc's 3-value string enum is
stale; string names parse via `Task.getStatusValue` (`core/parsers.js:10-13`).

| tududi status | int | GitHub issue state (task→issue) | issue→task on reopen/close |
|---------------|-----|----------------------------------|----------------------------|
| NOT_STARTED | 0 | open | close→DONE only from GitHub `closed` (reason `completed`) |
| IN_PROGRESS | 1 | open | — |
| PLANNED | 6 | open | — |
| WAITING | 4 | open | — |
| DONE | 2 | closed (reason: completed) | reopen → IN_PROGRESS |
| CANCELLED | 5 | closed (reason: not_planned) | reopen → IN_PROGRESS |
| ARCHIVED | 3 | closed (reason: not_planned) + audit comment (design D6; same handling as un-tag) | never reopened by the sync |

Reverse direction: GitHub `closed/completed` → DONE; `closed/not_planned` →
CANCELLED; `open` → leaves the tududi status untouched unless the task was
DONE/CANCELLED, in which case reopen → IN_PROGRESS. Only the state *transition*
propagates; the fine-grained active statuses (NOT_STARTED/IN_PROGRESS/PLANNED/
WAITING) are tududi-side detail GitHub cannot represent and are never
overwritten by an issue remaining open.

**Who closed it decides.** Task open+tagged and issue `closed/not_planned` is
one snapshot with two histories, and the marker's status baseline tells them
apart: a close the SYNC wrote (un-tag, archive) moves the baseline to
`closed:not_planned`, so a later re-tag is a tududi-side change and the
field loop reopens the issue without touching the task; a close a HUMAN made
on GitHub leaves the baseline at `open`, so it is a GitHub-side change and
the task goes CANCELLED — the human's close is never reverted. (Before
2026-09-03 the engine guessed "re-tag" for both and reopened a human's close
— dev-test #2, cycle 118.)

## Tag ↔ label rules (design D6, fixed)

- The sync tag itself is a control marker — never propagated as a label.
- Comparison is by case-folded name; the canonical projection for hashing is
  the case-folded, lexicographically sorted name set.
- Labels/tags created by the sync are plain names; no color/description sync.

## Marker block (design D4/D5)

HTML comment in the issue body, one line per element:
`uid` · one baseline hash **per synced field** (title, description/note,
status-as-mapped-value, label-set projection) · both sides' `updated_at` as
last synced. Audit comments (losing-value, un-tag, archive, linked) embed a
stable **audit-event key** = `uid` + event type + triggering side's
`updated_at`; a cycle checks existing comments for the key before posting
(retry-safe).

## Creation from either origin, once (design D4 as amended 2026-09-03)

| Situation the cycle finds | What it does |
|---------------------------|--------------|
| Tagged task, no marker anywhere, no same-title unlinked open issue | creates the issue (`POST /issues`), marker in the body — **only while the task is open** (0/1/4/6). A finished task (DONE/CANCELLED/ARCHIVED) is not exported, the mirror of "closed issues are not imported": a create can only OPEN an issue, and an open issue under a closed baseline would read as a GitHub reopen next cycle and drag the task back to IN_PROGRESS |
| Tagged task, exactly ONE same-title unlinked open issue (any author) | **adopts** it: marker written onto that issue with EMPTY baselines (so every differing field is an ordinary LWW conflict, loser preserved in a comment), keyed `linked` comment posted once |
| Tagged task, MORE THAN ONE same-title unlinked open issue | recovery error naming the task and every issue; nothing created or linked — and NONE of those issues becomes a task either (any unlinked task carrying the title, tagged or not, blocks GitHub-origin creation; a tagged one has already been reported once, so it is skipped without a second error) |
| Open unlinked issue, human-authored, no same-title task in the project | creates the task (`POST /api/task` with `project_id`, labels as tags + the sync tag); the executor then PATCHes the issue body with the marker carrying the new `uid` (the engine emits it with the `__UID__` placeholder) |
| Open unlinked issue, human-authored, same-title UNTAGGED task | recovery error naming both — a private task is never published by accident |
| Open unlinked issue authored by the sync identity | orphan of an interrupted tududi-origin creation: recovery error, never re-imported |
| Closed unlinked issue | ignored — finished work is not backfilled |
| Paired project absent on this tududi instance | recovery error naming the project and issue; nothing written |
| Open issue whose marker names a task the project does not hold | **dangling**: recovery error, no re-creation (deleted/moved task, or a marker written by ANOTHER tududi instance — why a real pair is enabled only on the instance holding its project) |
| One marker `uid` on two issues | recovery error naming both; that `uid` gets no ops this cycle |

Same-title means equal canonical `title` projections. Every row that says
"recovery error" writes nothing to either side and fails the per-pair
verification check until a person resolves it.

## Hierarchy — subtasks ↔ sub-issues, one level, native both sides (design D8, 2026-09-03)

Verified surfaces: tududi 1.1.1's task list hides subtask rows but embeds
each parent's children as `subtasks[]` with full fields (`uid`, `id`, `name`,
`note`, `status`, `tags`, `updated_at`, `parent_task_id`; `project_id` is
null on a child — `operations/subtasks.js`); `POST /api/task` accepts
`parent_task_id`. `POST /issues/{parent}/sub_issues` with
`{sub_issue_id: <child issue id>}` attaches one on the GitHub side.

**GitHub's parent field is invisible to the sync's own token.** The flat
`GET /repos/{o}/{r}/issues` documents `parent_issue_url` on a sub-issue, and it
is populated under a *user* token — but under the **GitHub App installation
token** this sync authenticates with, it is `null` on every sub-issue, while
the parent's `sub_issues_summary.total` still counts the child and
`GET /issues/{parent}/sub_issues` still lists it (measured 2026-09-03 against
`uhstray-io/dev-test` #12 ↔ #8; the REST docs say nothing about this). A cycle
that trusts the field sees a repository with no hierarchy at all and writes
nothing — which is what the first live run did. The parent map therefore comes
from **one GraphQL query per pair** (`repository { issues(first: 100) { nodes {
number parent { number } } } }`), which the App token answers correctly. tududi
costs no extra read; GitHub costs one call per pair, not per item. The 5.1
verification gate performs the same two reads, so the gate's snapshot and the
cycle's snapshot cannot disagree.

| Situation the cycle finds | What it does |
|---------------------------|--------------|
| Tagged subtask whose parent task is a linked pair | ordinary creation/adoption, scoped to that parent: a same-title unlinked open issue counts as a twin **only if it is a sub-issue of the parent's issue**; then `add_sub_issue` attaches the new/adopted issue to the parent's issue once the cycle observes it unattached (a fresh create is attached the NEXT cycle — the engine acts on observed state, so a top-level issue exists for at most one cadence) |
| Tagged subtask whose parent task is NOT linked | `skipped_parent_unlinked` — nothing written; it syncs the cycle after the parent does |
| Untagged subtask under a TAGGED parent | exports anyway — the gate is INHERITED from the parent (2026-09-04, superseding the per-item rule). tududi 1.1.1 offers no way to tag a subtask: the inline subtask editor sends no `tags` and `createSubtasks`/`updateSubtasks` whitelist fields that exclude them (`operations/subtasks.js:60-78`, `:122-167`), and clicking a subtask row opens the PARENT's page, so nothing links to the child's own `/task/<uid>` view — which is the only surface that can tag it. A per-item gate would leave the tududi→GitHub direction unreachable by hand. Tagging the parent is the explicit opt-in for the whole item, whose issue is already public |
| Any subtask under an UNTAGGED parent | private, with its whole subtree — the parent is the opt-in |
| Human-filed open sub-issue whose parent issue is a linked pair | creates the task with `parent_task_id` = the parent task's id and NO `project_id` (matching tududi's own subtasks); the sync tag is applied like any GitHub-origin task |
| Any GitHub-origin task, at any level | lands with status **PLANNED (6)**, not NOT_STARTED (2026-09-04): it is scheduled work someone else filed, not something the owner has picked up. Both map to `open`, so the pair is quiet on arrival |
| Human-filed open sub-issue whose parent issue is not linked | waits (`skipped_parent_unlinked`) until the parent imports |
| Linked pair whose issue is a sub-issue of a DIFFERENT issue than its task's parent implies (or the task is top-level, or the parent is not linked) | recovery error `hierarchy drift` naming both sides; no ops for that pair until a person moves one side — the sync never re-parents |
| Linked child whose issue was detached on GitHub | re-attached (`add_sub_issue`) — the tududi parent is the declaration |

Status needs no new rule: tududi's own parent/child auto-status
(`operations/parent-child.js`) fires on the PATCH the sync already issues, and
the parent's resulting change is an ordinary LWW field next cycle. Out of
scope: depth greater than one, re-parenting, and any `remove_sub_issue` /
delete — the engine has no such op (unit-asserted).

## Canonical projections (what gets hashed)

| Field | Projection |
|-------|-----------|
| title | exact string, trimmed |
| description | task `note` minus the marker suffix / issue body minus the marker block, trimmed |
| status | the MAPPED value per the table above (so both systems hash the same representation) |
| tags/labels | case-folded, sorted, comma-joined name set (sync tag excluded) |
| priority | `''` / `low` / `medium` / `high` — GitHub's `Urgent` folds onto `high`, tududi's `null` onto `''` |

## Priority — GitHub's native issue field ↔ tududi's task priority (2026-09-04)

GitHub Issues carries priority as an **org-level native issue field**, a
single-select named `Priority` with four options (Urgent / High / Medium /
Low); tududi's `Task.PRIORITY` has three (`LOW 0`, `MEDIUM 1`, `HIGH 2`) plus
`null` for none. Measured against `uhstray-io/dev-test` under the sync's own
App installation token, because a docs reading would have been wrong twice:

- **Read** — the values ride on the issue payload as `issue_field_values[]`,
  each naming its field (`issue_field_name`) and its chosen
  `single_select_option.name`. The App token reads these fine.
- **Write** — `PATCH /repos/{o}/{r}/issues/{n}` with
  `issue_field_values: [{field_id, value}]`, where `value` is the option NAME
  and the match is case-insensitive (so the canonical lowercase value writes
  directly). A `POST` creating an issue accepts the same key, so a prioritised
  task never spends a cycle un-prioritised on GitHub.
- **The write REPLACES the whole set.** A Priority-only PATCH silently cleared
  an unrelated `Effort` value on a probe issue. Every priority write therefore
  echoes the issue's other field values back verbatim, and *clearing* priority
  means sending the set with only its entry omitted. This is the same trap as
  tududi's `subtasks[]` on PATCH, and it is guarded the same way — by never
  sending a partial set.
- **The field id must be declared.** Writing needs the numeric `field_id`, and
  `/orgs/{org}/issue-fields` answers `403 Resource not accessible by
  integration` for an App — the sync cannot discover it. It is declared as
  `github_priority_field_id` in `github-mapping.yml` (org-level config, not a
  credential). `null` there leaves priority out of the sync entirely, rather
  than reading it one way and dropping it the other. A declared id that does
  not match an issue's actual `Priority` field is a named recovery error that
  refuses the whole cycle — a wrong id would otherwise write into some other
  field.

**Urgent folds onto high, at the projection.** tududi has no Urgent, so the
fold is the operator's rule — and placing it in the canonical projection is
what makes the lossy pair safe in both directions: an Urgent issue whose task
reads `high` is *quiet*, because after projection both sides hold the same
value, so a tududi `high` can never demote an Urgent issue back to High.
Lowering the task to medium or low genuinely differs, and does reach GitHub.
(Proven live: #9 held `Urgent` while its task held `high` across a cycle that
emitted zero operations.)

## Poll cadence and rate arithmetic

GitHub authenticated REST core limit: **5,000 req/h** (measured live on this
installation's token class via `gh api rate_limit`, 2026-09-02). Worst-case
cycle: 9 repos × (1 issue-list page + ~1 op per changed pair) ≈ 18–29 calls;
at a 5-minute cadence that is ≈ 220–350 calls/h — still under 7% of the ceiling.
tududi side has no rate limit concern at personal-tracker scale, but the
absent changed-since filter means each cycle lists tasks per synced project
(full-list diff against marker baselines — viable at this scale by design).
**Default cadence: 5 minutes** (design's open question, decided here).

## Verified n8n public-API workflow surface (source: tag n8n@2.25.7)

`packages/cli/src/public-api/v1/handlers/workflows/spec/paths/*` at the tag:
`GET /api/v1/workflows` (list; the upsert's list-by-name read),
`POST /api/v1/workflows` (create), `GET/PUT/DELETE /api/v1/workflows/{id}`
(read/update/delete — update is full-object PUT),
`POST /api/v1/workflows/{id}/activate` and `.../deactivate` (the kill switch's
one authenticated call). Auth: `X-N8N-API-KEY` (the captured
`secret/services/n8n:n8n_api_key`). Everything design D1's provisioning
playbook needs exists on the public surface — no session/`/rest` fallback
required for workflows (unlike the API-key mint).

## Transport constraint discovered by the spike (for phases 2/4)

The tududi VM's host firewall admits `:3002` only from the Caddy host, and the
public `todo.uhstray.io` sits behind the Cloudflare WAF, which 403s
non-browser callers (measured 2026-09-02). The n8n workflows therefore reach
tududi via the **internal Caddy origin** (resolving `todo.uhstray.io` to the
Caddy host's LAN address — TLS name intact, WAF not in the path), OR the
tududi firewall declaration gains the n8n host for `:3002`. Decide at
workflow-authoring time; the mapping/inventory carries the chosen base URL —
never hardcoded in the workflow JSON.
