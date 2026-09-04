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
  GitHub only when it carries the designated sync tag; untagged tasks never leave
  tududi. An open issue filed on GitHub in a paired repository becomes a task in
  the paired project, tagged for sync, so work can start on either side (amended
  2026-09-03 on the operator's direction — the original scope created from
  tududi only). Whichever side an item started on, it exists **once**: the
  same title already present on the other side is adopted and linked, never
  re-created.
- **Bidirectional field sync** for linked pairs: title, description, status
  (tududi statuses ↔ issue open/closed), and tags ↔ labels. Updates on either
  side propagate to the other on the next cycle.
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
  sync, subtask↔sub-issue mapping, syncing repos outside the declaration, and
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

- **Disable**: deactivate the n8n workflows (one playbook run) — both sides stop
  changing immediately; nothing else in either system depends on the sync.
- **Partial retreat**: remove a project↔repo pair from the declared mapping and
  re-provision — that pair stops syncing, existing issues/tasks stay as they are
  (the sync never mass-deletes on unmapping).
- **Data**: every write is an ordinary issue/task edit under the sync identity,
  visible in GitHub history and tududi's audit trail; conflicts preserve losing
  values in comments. No destructive operation exists in the sync at all — it
  never deletes issues or tasks.
- **Credentials**: revocation is an encoded, re-runnable rollback step, not a
  documentation pointer. Where a provider exposes revocation to the API it is
  executed by the playbook; where it does not (a fine-grained PAT's deletion is
  a provider-settings action), the operator step is gated by a machine check:
  the rollback step **verifies revocation by authenticating with the stored
  token and requiring the provider to refuse it**, fails loudly while the token
  still works, and treats an already-revoked credential as success — so a
  rollback interrupted after n8n deactivation converges on re-run instead of
  leaving a live token unnoticed. Re-running provisioning against an empty
  declaration removes the workflows and credentials it owns from n8n —
  provisioning prunes its own objects by design (design D1), so this is
  specified behavior, not a hope.
