## Why

Social posts for the instance's brands are written and scheduled by hand. This change drafts them with a text model through the inference gateway, checks them, and hands them to the publishing service as drafts, with a person's recorded approval in front of every schedule and publish.

## What Changes

- Adds plan 28, `plan/development/28-social-content-automation.md` (PROPOSED): pipeline, decisions, risks, open questions and phases.
- Adds this change: `design.md`, `tasks.md` (one task per Plane item) and the capability spec.
- Adds one index line to `AGENTS.md`.
- Documentation only: it deploys nothing, creates no flow, stores no credential and changes no running service.

## Capabilities

### New Capabilities

- `platform/social-content-automation`: AI-drafted social posts handed to the publishing service with a recorded approval before any schedule; see the spec.

### Modified Capabilities

None. The publishing service's capability belongs to change `deploy-postiz-social-publishing`.

## Impact

- The planned build adds about 16 files (plan 28, "Release budget").
- It depends on the changes `deploy-postiz-social-publishing`, `deploy-activepieces` and `agentgateway-content-generation`.
- It reads two existing OpenBao fields, the `activepieces` client key at `secret/services/agentgateway` and `postiz_api_key` at `secret/services/postiz`, and declares no new OpenBao path.
- Brand values live in site-config.
- Operator prerequisites: the X and LinkedIn developer apps and connected accounts (AC-760, AC-761) and the production Postiz API key (AC-763).

## Rollback Plan

This commit: revert it; it removes plan 28, this change and the `AGENTS.md` line, and nothing depends on them.

The planned build:

1. Throw the kill switch (plan 28 D13). It stops every flow that creates a draft or schedules a post.
2. Remove the publishing-service connection through the apply playbook. Without it no flow can create or schedule a post.
3. Revoke the `activepieces` gateway client key with `Manage agentgateway Client Key` (`action=revoke`, after the identity leaves the merged identity list, as that playbook requires). This stops generation.
4. Remove the declared flows from the repository and apply; the playbook never deletes a flow it does not own.
5. Leave drafts, scheduled posts and published posts in the publishing service alone. Removing them is a separate operator decision.

How the Community edition stops a flow, and how the publishing service revokes its key, are open until tasks 3.1 and 3.3 (plan 28, Open Questions).
