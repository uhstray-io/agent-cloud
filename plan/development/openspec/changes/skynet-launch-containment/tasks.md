## 1. Prerequisites

- [ ] 1.1 Production OPA refuses policy and data writes (the OPA session's bundle-mode PR merged and deployed; its verify proves `PUT /v1/data` is refused)
- [ ] 1.2 Confirm the current `agentcloud` rego ignores an added `settings` input field: `opa test /policies` green with a decision test that sends `settings`
- [ ] 1.3 svc-skynet is `guest` in production (agent-cloud and site-config demotion PRs merged, Provision skynet Semaphore Access (Dev) re-run, launch probe 403)
- [ ] 1.4 Validation gate: scenario "skynet's own token cannot launch" holds in production (403 on `POST /api/project/{p}/tasks` with skynet's token)

## 2. Gate service (agent-cloud, `platform/services/launch-gate/`)

- [ ] 2.1 Request handling: bearer auth per caller (hmac compare), caller→agent map, JSON body limit, 401/403/400 refusals before any outbound call
- [ ] 2.2 Validation: allowlist from rendered config, survey declarations read from Semaphore, literal-string rule (no `{{`, `{%`, `{#`; 4096-character cap), declared defaults filled, required fields enforced
- [ ] 2.3 OPA call: skynet's decision input shape plus `settings`, launch only on `allowed: true`; deny → 403 with reason, error/unreachable/no `allowed` → 503
- [ ] 2.4 Launch: task body built from scratch (`template_id`, `environment`, `message`, `params`), dedup against a running task for the same step (409), task tag `<caller> <workflow_id> <step_id>`, check mode refused outside the verified Semaphore versions
- [ ] 2.5 HTTPS listener that reloads its certificate when the file changes; structured decision/launch log lines without secrets
- [ ] 2.6 Unit tests covering every refusal, the OPA verdict paths, the exact task body, dedup and certificate reload, with fake OPA and Semaphore servers
- [ ] 2.7 Validation gate: scenarios "Undeclared key is refused", "Templated value is refused", "Template off the allowlist", "OPA denies", "OPA unreachable" and "The created task holds only validated values" pass in the unit tests

## 3. Deployment as code (agent-cloud)

- [ ] 3.1 Compose (digest-pinned Python image, source read-only, certs mount, published on the declared LAN bind), deploy.sh (lifecycle only), env template
- [ ] 3.2 `deploy-launch-gate.yml`: input refusals, place repo, secrets (`caller_<name>_token` generated, `semaphore_api_token` existing), allowlist rendered from OPA's catalog excluding destructive templates, leaf issuance, deploy, verify (TLS health, 401 without token, 400 for an undeclared key, 403 for a non-allowlisted template, plain HTTP refused)
- [ ] 3.3 `clean-deploy-launch-gate.yml`, `provision-launch-gate-semaphore-access.yml` (shared identity task, `task_runner`, launch probe 404)
- [ ] 3.4 Templates (base with Dev variant, Local) in `templates.yml` / `templates-local.yml`; local-dev inventory and bootstrap entries
- [ ] 3.5 Renew Internal Certs: generalise the OPA server-leaf proof into a declared-listener proof and admit `launch-gate-server`; tests for both leaves
- [ ] 3.6 deploy-skynet: shared-read `caller_skynet_token`, render `SKYNET_LAUNCH_GATE_URL` and the gate's name mapping in `compose.prod.yml`, verify skynet's container reaches the gate (401 without token)
- [ ] 3.7 Docs: service CLAUDE.md, AGENTS.md rows (secrets layout, workflows, sub-directory docs), playbooks README
- [ ] 3.8 Validation gate: scenarios "Missing token" and "Caller claims another identity" pass against the locally deployed gate

## 4. skynet (skynet repo)

- [ ] 4.1 Semaphore client: `launch()` calls the gate when `SKYNET_LAUNCH_GATE_URL` is set, refuses to launch otherwise; reads (`task`, `wait`, `output`) keep using skynet's guest token
- [ ] 4.2 Tests with a fake gate: request shape, error mapping (403/409/400/503 recorded as step `fail`), no direct-launch fallback
- [ ] 4.3 Publish the image, record its digest
- [ ] 4.4 Validation gate: scenario "Task message for skynet" passes with the fake gate (`skynet <workflow_id> <step_id>`)

## 5. Production rollout

- [ ] 5.1 site-config: `launch_gate_svc` host and VM declaration (NetBox IP reserved first), `internal_leaves` entry, firewall (API port from `skynet_svc` and `console_svc`), OPA `firewall_allow_groups` gains `launch_gate_svc`, `semaphore_project_members` gains `svc-launch-gate: task_runner`; inventory synced
- [ ] 5.2 Workflow steps in order, check mode first: lookup, validate address, provision, keys + backup, harden, firewall, grow root if needed
- [ ] 5.3 Provision launch-gate Semaphore Access (Dev), Deploy launch-gate (Dev); skynet image pin bumped and Deploy skynet (Dev)
- [ ] 5.4 Back up the gate's credentials to site-config (caller tokens; not the launch token)
- [ ] 5.5 Validation gate: in production, scenarios "OPA allows" (a check-mode launch of a read-only snapshot template through the gate as `service-agent`), "Duplicate launch while running", "Dry run", "Plain HTTP" and "Another LAN host" hold, and "Declared team matches" passes in Manage Semaphore Access check mode
