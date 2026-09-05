# Integrate tududi with GitHub Issues (bidirectional sync)

## Why

tududi (`todo.uhstray.io`) is the platform's project tracker, but development work
actually lands in GitHub repositories — so work items live in two places with no
bridge, and whichever side a person updates goes stale on the other. The platform
now has (pending `complete-n8n-composable-deployment`) an n8n automation substrate
with code-managed nodes and OpenBao-provisioned credentials, which is exactly the
engine this bridge needs; this change is its first real workflow consumer.

## What Changes

- **Nine tududi-project ↔ GitHub-repo relationships** (grown from six on the
  operator's 2026-09-03 direction — including the PUBLIC `agent-cloud` repo,
  whose pair ships disabled because enabling it is a publication decision, and
  `dev-test`, the standing validation pair every instance may run), declared as
  config-as-code in this repo (the repo names already appear committed here).
  `platform/services/tududi/sync/github-mapping.yml` is the declaration; the
  GitHub App's installation must cover exactly those repositories, which the
  token refresher asserts on every mint. A relationship must be explicitly
  declared to sync anything.
- **Selective, opt-in item sync, from either origin**: a tududi task crosses to
  GitHub only when it carries the designated sync tag; subtasks inherit consent
  from a tagged parent. Untagged top-level tasks never leave tududi. An open
  issue filed on GitHub in a paired repository becomes a task in
  the paired project, tagged for sync, so work can start on either side (amended
  2026-09-03 on the operator's direction — the original scope created from
  tududi only). Whichever side an item started on, it exists **once**: the
  same title already present on the other side is adopted and linked, never
  re-created.
- **Bidirectional field sync** for linked pairs: title, description, status
  (tududi statuses ↔ issue open/closed), and tags ↔ labels. Updates on either
  side propagate to the other on the next cycle.
- **One level of native hierarchy**: tududi subtasks synchronize with GitHub
  sub-issues beneath linked parents; re-parenting and deeper nesting are excluded.
- **Comments are GitHub-only**: issue comments are never copied into tududi, and
  nothing in tududi produces issue comments (except the conflict audit note below).
- **Last-writer-wins conflict resolution** on update timestamps, with the losing
  value preserved in an issue comment so nothing is silently destroyed.
- **n8n workflows as the engine**, poll-based on both sides (tududi has no
  webhooks; polling GitHub avoids opening n8n's webhook path through
  forward_auth in v1). Workflows, credentials, and the mapping are all placed by
  playbook — no hand-built workflows in the n8n UI.
- **Credentials from OpenBao**: the tududi personal API token at
  `secret/services/tududi:api_token` (the path already planned for weft) and a
  new GitHub credential scoped to the mapped repos, provisioned into n8n by the
  same pattern as the Postiz credential.
- **NOT in scope**: GitHub Projects v2 boards, comment sync, attachment/media
  sync, deeper hierarchy, syncing repos outside the declaration, and
  real-time (webhook) transport — each a possible later layer.

## Capabilities

### New Capabilities

- `platform/tududi-github-sync`: the bidirectional bridge between tududi
  projects and GitHub repository issues — declared relationships, tag-gated item
  crossing, field/status/label propagation, conflict handling, and the custody
  of the credentials it runs on.

### Modified Capabilities

<!-- none — platform/n8n-automation (pending in complete-n8n-composable-deployment)
     is consumed as specified there: this change adds workflows and credentials
     through the mechanisms that change establishes, without altering its
     requirements. -->

## Impact

- **Depends on** `complete-n8n-composable-deployment` (n8n composable in prod,
  API key in OpenBao, credential-provisioning pattern). Must land after it.
- **New code**: mapping declaration file, n8n workflow definitions as code, a
  provisioning playbook for the workflows + GitHub/tududi credentials, BATS
  coverage; Semaphore template(s) for provisioning.
- **Secrets layout**: `secret/services/tududi:api_token` gains its planned
  value; the sync's GitHub credential is a dedicated GitHub **App**, not a PAT
  (design D7 as decided 2026-09-03) — its private key at
  `secret/services/github:tududi_sync_app_key` and its client id (the JWT
  issuer, not a secret) at `:tududi_sync_app_client_id`. Installation tokens
  live one hour, so a scheduled refresher re-mints and updates the n8n
  credential in place.
- **External state**: issues created/updated in the declared GitHub
  repositories; tasks updated in tududi. Both under a dedicated sync identity —
  the App's `[bot]` login — so its writes are distinguishable and
  echo-suppressible.
- **Docs**: CLAUDE.md workflow table, tududi service context, a sync contract
  doc alongside the Postiz automation contract.

## Rollback Plan

**Preservation-only, superseding the earlier prune/revoke plan (2026-09-05).**
Run the provisioning playbook with `sync_enabled=false`: deactivate every owned
workflow and verify each is inactive, without deleting workflows, credentials,
tasks, issues or markers. Wait for any already-running execution to finish
before declaring writes stopped; deactivation does not undo propagated edits.

For partial retreat, disable the pair in the committed mapping and re-provision.
For a fully disabled declaration, the playbook deactivates and stops before
provider validation or credential writes, using only the engine's API key.
A malformed/missing declaration still refuses; it is not an empty deployment.

Preserve existing credential access. Token proof failure stops for reconciliation,
not revocation or automatic replacement. `tududi_token_validate_only=true` on
Store tududi API Token proves the stored value and stops without mint/store
writes. App installation access, private keys and existing tokens are not removed
as part of rollback. Credential retirement requires a separate scoped decision.

Task/issue edits remain visible in their histories and conflicts preserve losing
values in keyed comments. No cleanup, database restore or ownership migration is
part of this rollback. Task 6.0's visibility decision and the final production
release gates remain open.
