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
| **Token mint is automatable** | Yes — session login (the deploy retains break-glass local login beside OIDC) → CSRF token → `POST /api/profile/api-keys`, implemented by `store-tududi-api-token.yml` on the `store-n8n-api-key.yml` model. Design D7's open question: answered | above + `secret/services/tududi` break-glass entry (AGENTS.md secrets table) |
| GitHub credential | A dedicated GitHub App (Issues read/write, the mapped repos), NOT a PAT — operator decision 2026-09-03. Installation tokens live 1 hour, so `refresh-tududi-sync-github-token.yml` re-mints on a 45-minute Semaphore schedule and PATCHes the n8n credential in place; issue writes are authored by the App's `[bot]` login (the D5 authorship identity) | design D7 (amended); `platform/lib/github_app_token.py` |
| Task WRITE route | `PATCH /api/task/{uid}` — NOTE the SINGULAR `/api/task/` (like `POST /api/task` create), distinct from the PLURAL `GET /api/v1/tasks` list. The write node PATCHes `/api/task/{uid}`; the list node GETs `/api/v1/tasks` | `backend/modules/tasks/routes.js:539` (patch), `:405` (create) |
| Task identity | `uid` (model default via `backend/utils/uid`), exposed by the serializer | `backend/models/task.js:13-17`; `backend/modules/tasks/core/serializers.js:84` |
| Issue-reference carrier | the task `note` field (TEXT) — no dedicated external-link field exists at 1.1.1. Design D4's open question: answered (marker suffix in `note`) | `backend/models/task.js:53` |
| Changed-since filtering | **ABSENT** — `GET /api/v1/tasks` accepts `type, groupBy, maxDays, order_by, include_lists, limit, offset` only. The full-list-diff fallback (design risk 2) is therefore the v1 reality, not a contingency | `backend/modules/tasks/routes.js:209-222` |
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

## Tag ↔ label rules (design D6, fixed)

- The sync tag itself is a control marker — never propagated as a label.
- Comparison is by case-folded name; the canonical projection for hashing is
  the case-folded, lexicographically sorted name set.
- Labels/tags created by the sync are plain names; no color/description sync.

## Marker block (design D4/D5)

HTML comment in the issue body, one line per element:
`uid` · one baseline hash **per synced field** (title, description/note,
status-as-mapped-value, label-set projection) · both sides' `updated_at` as
last synced. Audit comments (losing-value, un-tag, archive) embed a stable
**audit-event key** = `uid` + event type + triggering side's `updated_at`;
a cycle checks existing comments for the key before posting (retry-safe).

## Canonical projections (what gets hashed)

| Field | Projection |
|-------|-----------|
| title | exact string, trimmed |
| description | task `note` minus the marker suffix / issue body minus the marker block, trimmed |
| status | the MAPPED value per the table above (so both systems hash the same representation) |
| tags/labels | case-folded, sorted, comma-joined name set (sync tag excluded) |

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
