# Integrate tududi with GitHub Issues (bidirectional sync)

## Why

tududi (`todo.uhstray.io`) is the platform's project tracker, but development work
actually lands in GitHub repositories — so work items live in two places with no
bridge, and whichever side a person updates goes stale on the other. The platform
now has (pending `complete-n8n-composable-deployment`) an n8n automation substrate
with code-managed nodes and OpenBao-provisioned credentials, which is exactly the
engine this bridge needs; this change is its first real workflow consumer.

## What Changes

- **Six tududi-project ↔ GitHub-repo relationships**, declared as config-as-code
  in this repo (the repo names already appear committed here):
  `Zerds - Development ↔ zerds`, `Zerds - Website ↔ zerds-website`,
  `agent-cloud ↔ agent-cloud`, `Weft - Development ↔ weft`, `huhhb ↔ huhhb`,
  `Scientific-Business Website ↔ scientific-business`. A relationship must be
  explicitly declared to sync anything.
- **Selective, opt-in item sync**: only tududi tasks carrying the designated sync
  tag cross to GitHub as issues. Untagged tasks never leave tududi.
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
  new GitHub credential scoped to the six repos, provisioned into n8n by the
  same pattern as the Postiz credential.
- **NOT in scope**: GitHub Projects v2 boards, comment sync, attachment/media
  sync, subtask↔sub-issue mapping, syncing repos outside the declared six, and
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
- **Secrets layout**: `secret/services/tududi:api_token` gains its planned value;
  a GitHub credential for the sync lands under an existing or new
  `secret/services/github` field — scoping decided in design.
- **External state**: issues created/updated in six GitHub repositories; tasks
  updated in tududi. Both under a dedicated sync identity, so its writes are
  distinguishable and echo-suppressible.
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
- **Credentials**: revoke the tududi token in its UI and the GitHub credential at
  the provider; delete the n8n credentials by re-running provisioning against an
  empty declaration.
