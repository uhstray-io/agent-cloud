## Context

See proposal.md (Why). Facts that shape the approach:

- skynet asks OPA as the registry step's owner role (`infra-agent`, `security-agent`, `o11y-agent`,
  `service-agent`, `network-agent`), never as `skynet`, with action `run_task`, the template, step and Git
  branch (skynet `src/skynet/orchestrator/service_onboarding.py:17`, `:263-266`).
- skynet's Semaphore client already refuses survey keys a template does not declare, fills declared
  defaults, refuses a second launch while one is running, and tags tasks `skynet <workflow_id> <step_id>`
  (skynet `src/skynet/semaphore.py:211-371`); the console reads that tag (plan 17 D17).
- OPA's catalog lists, per role, the base template names it may run (`allowed_templates`,
  `service_deploy_templates` in `policies/agentcloud/data.json`); the rego strips a `(Dev)` suffix before
  matching (`_base_template`).
- The operator launcher pins check-mode launches to Semaphore versions whose task `params` shape was read
  from source (`scripts/semaphore-launch.py:45`, `VERIFIED_DRY_RUN_VERSIONS`).
- OPA serves HTTPS from an internal-CA leaf that renews without a restart (`--tls-cert-refresh-period`),
  and Renew Internal Certs proves the served serial (`renew-internal-certs.yml` header, "opa server leaf").
- skynet's container already trusts the internal CA through a combined bundle and maps internal names with
  `extra_hosts` (`platform/services/skynet/deployment/compose.prod.yml`).

## Goals / Non-Goals

**Goals:**
- A compromised or mistaken agent can launch only allowlisted templates, with literal values for declared
  survey fields, and only what OPA allows, decided outside the agent.
- skynet's launch path works again in production through the gate, with its current behaviour (dedup,
  defaults, task tag, check mode) preserved.

**Non-Goals:**
- Making OPA's own API read/write-safe: the OPA session's bundle mode and this session's
  `--authorization=basic` change are prerequisites tracked as their own changes.
- skynet's read API transport (its own TLS leaf): a separate change.
- The console's Phase 4 proposal flow: it gets a caller token and agent set here, but its client is the
  console's work.
- Auditing every allowlisted template's playbook for unsafe use of a literal survey value; the allowlist
  starts with templates already in OPA's catalog, and each addition is a reviewed change.

## Decisions

1. **Code lives in agent-cloud, not skynet** (`platform/services/launch-gate/`). The gate is a guardrail;
   keeping it out of skynet's repository and image means a compromised skynet release cannot change the
   gate. *Alternative:* a module in skynet's image run as a second container: rejected, same release
   channel and same VM.
2. **Stdlib Python on a digest-pinned official Python image, source bind-mounted read-only.** No build
   pipeline, no third-party packages; mirrors skynet's server (`ThreadingHTTPServer`). *Alternative:* a
   custom image built by CI: more moving parts for ~500 lines of code.
3. **Allowlist derived from OPA's catalog at deploy time.** The deploy renders the union of
   `allowed_templates` and `service_deploy_templates` for the agent identities the callers may act as,
   plus their `(Dev)` variants, into the gate's config. OPA's data file stays the one source; the gate's
   list only narrows what reaches OPA. A template OPA treats as destructive is never rendered in:
   `catalog.semaphore.destructive_templates` (`data.json:176`) and any name starting `Clean Deploy`
   (`agent_actions.rego:68`).
   *Alternative:* a hand-maintained list: drifts from policy.
4. **Survey declarations read live from Semaphore** (`GET /api/project/{p}/templates`), the published
   catalog being the authority on what a template declares, exactly as skynet's client does today.
5. **Value rules:** strings only, at most 4096 characters, no `{{`, `{%` or `{#` anywhere. The task body
   is built by the gate from scratch: `template_id`, `environment` (the validated survey JSON),
   `message`, and `params` (`dry_run`, `diff`) only; no `arguments`, `limit`, `inventory_id`, tags.
   Check mode is refused on a Semaphore version outside the same allowlist the operator launcher uses
   (the gate reads `/api/info`).
6. **OPA input** is skynet's `DecisionInput` shape plus `settings` (the exact values to be launched), so
   policy can bind decisions to what actually runs; the agent field is the one the caller's token is
   allowed to act as. Caller-supplied `proposal` and `context` are forwarded unchanged (OPA judges them).
   A task in this change confirms the current rego ignores the added field (opa test stays green).
7. **Caller auth:** one 48-character bearer per caller, generated once by the gate's deploy into
   `secret/services/launch-gate:caller_<name>_token`; skynet's deploy shared-reads its own
   (`caller_skynet_token`). The caller→agents map is a deploy default in the repo
   (`launch_gate_callers`: skynet → the five role identities; console → reserved, empty until Phase 4),
   overridable in inventory. hmac comparison, as skynet's server does.
8. **Semaphore identity** `svc-launch-gate` at `task_runner`, provisioned by a thin play over the shared
   `tasks/provision-semaphore-service-identity.yml` (launch probe expects 404), declared in
   `semaphore_project_members`. Its token is read by the gate's deploy as an `existing` secret.
9. **TLS:** server leaf `launch-gate-server` from the internal CA (site-config `internal_leaves`), the
   gate reloading its `SSLContext` when the certificate file changes (checked per connection), so renewal
   needs no restart (reload `none`). Renew Internal Certs gains a proof path for it by generalising the OPA
   server-leaf proof into one driven by the host's declared listener (bind, port, server name), so OPA and
   the gate share one proof, and skynet's own TLS change can reuse it.
10. **skynet integration:** skynet's Semaphore client keeps reading tasks with its `guest` token; only
    `launch()` changes, to call the gate when `SKYNET_LAUNCH_GATE_URL` is set and to refuse a launch
    otherwise (no direct-launch fallback). skynet's container maps the gate's name like OPA's.
11. **Placement** (Joseph, 2026-10-31): own VM `launch_gate_svc`, 2 vCPU / 2 GB, firewall admits the
    API port from `skynet_svc` and `console_svc` only; the gate's VM is added to OPA's
    `firewall_allow_groups`.

## Risks / Trade-offs

- [The gate's token can still launch any project template] → The gate is a ~500-line service on its own
  VM with one open port, no shell-outs, no templating of inputs and no other workloads; its token never
  leaves OpenBao and the gate's `.env`.
- [A literal survey value used unsafely inside an allowlisted playbook] → The allowlist is the OPA
  catalog, every addition is reviewed; OPA receives the exact values and can bound them.
- [Single point of failure for agent launches] → Fails closed (no launch); skynet records `fail` and
  stops; health and service checks cover the gate like any service.
- [OPA still writable until the prerequisite lands] → Deploying the gate to production waits for OPA
  writes to be refused (bundle PR) — task 1.1 gates the rollout.
- [Semaphore upgrade changes the task body or check-mode shape] → The version allowlist refuses check
  mode on an unverified version; the gate's tests pin the body it sends.

## Migration Plan

1. Land OPA write refusal (OPA session) — prerequisite.
2. Build and test the gate locally (local-dev template), then production: VM via the workflow
   (allocate IP, provision, keys, harden, firewall), Provision launch-gate Semaphore Access, Deploy
   launch-gate, verify.
3. Publish a skynet image whose launch goes to the gate; pin it; redeploy skynet with the gate URL and
   caller token.
4. Prove: skynet's own token still refused (403); a gate launch of a check-mode read-only template
   succeeds; an undeclared key, a templated value and a non-allowlisted template are refused.

Rollback: see proposal.md (Rollback Plan).

## Open Questions

- The console's agent identity name and its agent set for Phase 4 (reserved, empty here; the console's
  plan decides).
