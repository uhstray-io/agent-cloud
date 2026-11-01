## Why

skynet decides what work to run, and until 2026-10-31 it launched that work in Semaphore with its own
`task_runner` token, checking OPA only inside its own client. That check contains nothing: Semaphore
v2.19.11 merges the task environment a launcher sends over the template's, unfiltered (upstream
`services/tasks/TaskRunner.go:395-425`), so any `task_runner` can make any template run attacker-chosen
code on the shared runner that holds the controller's OpenBao AppRole. Per-template roles and
project-bound runners exist only as paid-edition stubs (`pro/api/roles.go`, `pro/api/projects/runners.go`).
svc-skynet was demoted to `guest` the same day, so agent-initiated launches are off until something
outside skynet can launch on its behalf safely. The Dispatch Console's proposal flow (plan 17, Phase 4)
needs the same path.

## What Changes

- New service **launch-gate** on its own small VM (`launch_gate_svc`, 2 vCPU / 2 GB): a stdlib Python HTTP
  service, deployed through the service deployment workflow like every other service.
- It holds the only launch-capable Semaphore token for agent-initiated work, as its own identity
  `svc-launch-gate` at project role `task_runner`. skynet and the console never hold one.
- A caller sends `{template, survey, agent, workflow_id, step, dry_run}`, plus optional `proposal` and
  `context` forwarded to OPA unchanged. The Git branch is not a request field: the gate reads it from the
  template's repository record in Semaphore, so a caller cannot name a branch the template does not run
  from. launch-gate refuses, before any call out:
  a template not on its allowlist, a survey key the template does not declare, a missing required survey
  field, any value that is not a plain string, and any value containing `{{`, `{%` or `{#`.
- It then asks OPA server-side (`POST /v1/data/agentcloud/decision`) with the caller's identity as the
  agent, and launches only on `allowed: true`, sending exactly the validated survey values and nothing
  else. The OPA decision and the Semaphore task id are returned and logged.
- Callers authenticate with one bearer token each (skynet, console), held in OpenBao and shared-read by
  the caller's deploy; the token names the agent identity OPA is asked about, so a caller cannot claim
  another's identity.
- The API serves HTTPS only, from an internal-CA server leaf; its host firewall admits only `skynet_svc`
  and `console_svc`.
- skynet's Semaphore client stops launching directly: it sends launch requests to launch-gate. Its own
  Semaphore identity stays `guest` (read-only task history).
- **BREAKING** for skynet's launch path: a skynet image without the launch-gate client cannot launch at
  all (already true since the demotion).

## Capabilities

### New Capabilities
- `platform/agent-launch-gate`: how agent-initiated Semaphore launches are validated, authorized by OPA
  server-side, executed and recorded, and what a caller can and cannot make a run do.

### Modified Capabilities
- (none: no existing spec under `openspec/specs/` covers skynet or Semaphore launch rights)

## Impact

- **agent-cloud:** new `platform/services/launch-gate/` (service code, compose, deploy.sh, tests),
  `deploy-launch-gate.yml`, `clean-deploy-launch-gate.yml`, `provision-launch-gate-semaphore-access.yml`
  (reusing `tasks/provision-semaphore-service-identity.yml`), templates in `templates.yml`, a renewal
  proof path for its server leaf in `renew-internal-certs.yml`, deploy-skynet changes (shared-read of
  skynet's launch-gate token, the launch-gate URL and trust), AGENTS.md rows.
- **skynet repo:** the Semaphore client's launch call goes to launch-gate; a new image is published and
  pinned.
- **site-config:** `launch_gate_svc` host and VM declaration (NetBox IP, vmid), its `internal_leaves`
  entry, firewall rules (launch-gate admits skynet and console; skynet's and the console's egress
  unaffected), `semaphore_project_members` gains `svc-launch-gate: task_runner`.
- **OPA:** prerequisite, tracked separately: writes refused (bundle mode, agent-cloud #805) and then
  `--authorization=basic` limiting the API to decisions and health (agent-cloud #810). launch-gate's VM is added to OPA's
  firewall allow list.
- **Production:** one new VM; svc-skynet stays `guest`.

## Rollback Plan

launch-gate is additive. To roll back: stop routing skynet's launches to it (redeploy the previous skynet
image pin), then `Clean Deploy launch-gate` and remove `svc-launch-gate` from `semaphore_project_members`
(Manage Semaphore Access removes the member). svc-skynet stays `guest`, so a rollback returns to "no
agent-initiated launches", never to the unsafe direct path. The VM can be destroyed with `Destroy VM`.
