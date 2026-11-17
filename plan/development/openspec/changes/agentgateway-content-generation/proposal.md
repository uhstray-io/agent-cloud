# Agentgateway content generation

Author: Joseph A. Wisneski IV <stray@uhstray.io>. The decisions are recorded in
`plan/development/27-agentgateway-content-generation.md`, which is the authority for the reasons
behind them. This change lists the work as OpenSpec tasks.

Companion changes: `inference-gateway-agentgateway` (the gateway this change extends: its
identities, budgets and deploy verify are the base) and `agentgateway-openrouter` (the pattern for
adding a second upstream, and the reason the content identity stays out of its union).

## Why

Content automation drafts social posts in Activepieces flows and hands them to Postiz. Drafting
needs a language model now and an image model later. The DGX Spark pair serves those models, and
the agentgateway is the front door that holds per-identity keys and budgets. The content caller has
neither an identity nor a network path to the gateway today.

Plane AC-783, decided on 2026-11-12, sets the network path. The caller reaches the gateway on the
gateway's own internal listener, with an internal-CA client leaf. The public inference route is
not used, because it carries a per-source request limit and a read timeout that a content flow
should not sit behind.

## What changes

- One gateway identity, `activepieces`, with its own rolling hourly token budget and an explicit
  model allow-list. It is declared in site-config, not in this repository.
- The caller's client leaf is issued on the Activepieces host, its name is added to
  `agw_client_cert_allowlist` in site-config, and its renewal is proven through the existing
  probe path.
- A text path first (tasks 2.1 to 2.4). An image path after it (tasks 3.1 to 3.6): an image
  upstream, a per-identity request limit, a conformance case and dashboards. The image path waits
  until the image path's blockers in plan 27, D5, clear.
- This change adds documentation only. No code, template, playbook or inventory changes in this
  repository.

## Rollback Plan

- **This change.** Revert its documentation commit. Nothing runs from it.
- **Text path, after tasks 2.2 and 2.3 are deployed.** In site-config, remove the leaf's name from
  `agw_client_cert_allowlist` and remove the `activepieces` entry from `agw_clients`. Run
  `Issue Internal Leaf (Dev)` with `leaf_action=remove` first, because the AGENTS.md row says that
  action runs before the declaration is removed. Then remove the declarations, run
  `Deploy agentgateway (Dev)` to re-render the gateway without the identity, and run
  `Manage agentgateway Client Key` with `action=revoke` once the name is off the merged identity
  list (the AGENTS.md row says revoke requires that). The budget value in site-config goes with
  the identity.
- **Image path, after tasks 3.1 and 3.2 are deployed.** Remove the image entries from `agw_models`
  in site-config and redeploy. If the pin moved in task 3.1, restore the previous pin at
  every pin site (plan 27, Verified facts; the deploy refuses a rendered image that differs from
  the reviewed pin) and redeploy through the Semaphore template. Task 3.3 adds the request limit; its removal is written with that task.
- Each rollback step is a Semaphore template run or a site-config change. None is a manual edit on
  a host.

## Impact

- Files in this repository: the six OpenSpec change files, plan 27
  (`plan/development/27-agentgateway-content-generation.md`), and one line in `AGENTS.md`. Later
  tasks add files on the text and image paths; plan 27, "Release-Budget Cost", has the counts.
- No new container. No new listener. No change to the public edge. The text path uses the gateway's
  internal listener, which already requires client certificates when `agw_listener_tls` is on
  (AGENTS.md, `Deploy agentgateway` row).
- Out of this repository: declaring the identity, its budget, its allowlist entry and the leaf in
  site-config; the Activepieces host and its connection (`deploy-activepieces`,
  `social-content-automation`); and image serving on the DGX Spark pair (dgx-spark items
  DGXSPARK-10 and DGXSPARK-12 to DGXSPARK-16). Each is an open task here, not done by this
  change.
