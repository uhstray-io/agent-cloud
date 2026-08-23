## Why

Social-media posting for uhstray is manual and unautomated today. There is a Postiz
service directory in this repo, but it is a compose-only stub with hardcoded
credentials, no playbook, no secret management, and no Semaphore template — it was
explicitly recorded as "must not be deployed anywhere". Meanwhile a working Postiz
configuration exists only as a loose `.env` on a developer machine, which is exactly
the drift this platform's conventions exist to prevent.

Standing Postiz up properly gives two things at once: a self-hosted publishing plane
for social content, and an API surface that n8n can drive to automate the creation,
upload, and scheduling of posts — moving social publishing from a manual chore into
the automation layer where the rest of the platform already lives.

## What Changes

- **New platform service `postiz`**, onboarded through the full composable pattern:
  OpenBao-managed secrets, Jinja2-templated config, a `deploy.sh` that only handles
  container lifecycle, a phased Ansible playbook, and Semaphore templates. This
  **replaces** the existing compose-only stub, whose hardcoded credentials are
  removed.
- **A hardened service VM.** An existing host reachable only by bootstrap password
  gains a per-service SSH key sourced from OpenBao, key-only sshd, NOPASSWD sudo, and
  a default-deny UFW firewall permitting only SSH from admin ranges and the service
  port from the reverse proxy.
- **Authentik as the identity provider for Postiz**, via Postiz's native generic-OIDC
  support rather than an edge `forward_auth` gate — so the automation API path stays
  usable by n8n with an API key.
- **A public route** at `postiz.uhstray.io` through the central Caddy, with TLS via
  the existing Cloudflare DNS-01 flow.
- **Pre-existing social-platform credentials are moved into OpenBao** (Discord,
  LinkedIn, X, YouTube) instead of living in a plaintext file, and are consumed from
  there on every deploy.
- **A documented n8n integration contract** — endpoint, auth model, and rate ceiling —
  so the automation can be built against a stable surface. No n8n changes here.
- **BREAKING (local only):** the committed `compose.yml` for this service is rewritten
  and is no longer runnable standalone; it now requires the Ansible-rendered
  configuration files. Nothing consumes it today, so no deployed system is affected.

## Capabilities

This change creates the first specs in this store, so it also establishes the
organization convention: capabilities are grouped by platform domain
(`platform/…`, leaving room for `agents/…`), with kebab-case leaf names.

### New Capabilities

- `platform/postiz-publishing`: the Postiz service as a platform capability — reachable
  over TLS at its public host, authenticated against the central identity provider,
  scheduling and publishing posts to connected social accounts, and exposing an
  API-key-authenticated endpoint for automation. Covers configuration sourcing from
  the secret store, registration lockdown after first sign-in, and the requirement
  that scheduled posts actually execute.
- `platform/service-vm-hardening`: the reusable host-access baseline every service VM
  must meet — bootstrap credentials captured into the secret store, per-service keys
  distributed additively, key authentication proven before password authentication is
  removed, and a default-deny host firewall exposing only SSH and the service's own
  published port. Specified here because this change is the first to exercise it
  end-to-end on a fresh host, and because "must not lose access to the machine" is an
  acceptance criterion rather than an implementation detail.

### Modified Capabilities

None — this store has no existing specs.

## Impact

- **New**: a service deployment directory (compose base and local overlay, lifecycle
  script, two config templates, docs), three playbooks (deploy, clean-deploy, secret
  seeding), an identity-provider client blueprint, Semaphore templates for local and
  production, an integration-contract document, and shell tests.
- **Modified**: the existing service `compose.yml` (credentials removed,
  env-parameterized); inventory in three files (local, public placeholder, and the
  private site-config repo); the identity provider's deploy playbook, to inject the
  new client secret.
- **Secret store**: a new service path holding three generated values that must stay
  stable across redeploys, plus nine seeded platform credentials; one cross-service
  read of the OIDC client secret owned by the identity provider.
- **External, operator-performed**: OAuth redirect-URI updates at four social
  platforms — not automatable from here. DNS is *not* in this list: the zone is
  config-as-code via OpenTofu and the record is already declared and live.
- **Dependencies**: adds a Temporal workflow engine and a second Postgres to the
  service host — the largest single cost of this change, and the reason the upstream
  reference topology is trimmed rather than adopted wholesale.
- **Not affected**: n8n receives no configuration or deployment change; the identity
  provider and reverse proxy are extended through their existing config-as-code
  mechanisms, not modified structurally.

## Rollback Plan

Rollback is staged to match the deployment. **It is only lossless while the service is
still greenfield.** Step 2 destroys volumes, so the moment real accounts, connected
channels, scheduled posts, or uploaded media exist, this ceases to be a lossless
rollback — it is a teardown. That distinction was previously glossed as "reversible
without data loss because the service is greenfield", which is true at cutover and
false a day later.

Past the point where data exists, rollback means: take a hypervisor snapshot of the VM
first, or export the datastore and the uploads volume, and treat the steps below as a
destructive rebuild rather than a revert. Do not run step 2 against a populated
instance expecting to undo it.

1. **Public route** — remove the site block from the reverse proxy's managed section
   and re-run the route playbook. It validates and rolls back automatically on a bad
   config, so the blast radius is this one hostname; every other route is untouched.
2. **The service** — run the clean-deploy playbook. **Destructive**: it wipes the
   datastore, the cache, the uploads volume, and the workflow engine's datastore.
   Nothing else depends on this service and no other service reads its data, so the
   blast radius is contained — but the data is gone. Every connected social account
   must be re-authorized by hand afterwards, and because the signing secret is reused
   from the secret store, existing API keys keep authenticating against an account that
   no longer exists. Snapshot or export first if anything of value is in there.
3. **Identity provider client** — the client blueprint is config-as-code; deleting the
   file and re-running the provider's deploy removes the application. Other clients
   are unaffected because each is its own blueprint.
4. **Firewall** — re-runnable and idempotent; rules can be narrowed or the firewall
   disabled from the hypervisor console, which reaches the VM out-of-band and so
   cannot be locked out by any rule.
5. **SSH hardening is the one step to treat as one-way.** Re-enabling password
   authentication is possible but undesirable; the intended recovery path is the
   per-service key held in the secret store, with the hypervisor console as
   break-glass. This is why the deployment sequence proves key authentication from two
   independent directions *before* removing the password, rather than relying on
   rollback.
6. **Secrets** — the seeded platform credentials remain in the secret store after a
   rollback, which is deliberate: they are the operator's own application credentials,
   not artifacts of this change. Rotating them is a separate decision.

A partial rollback is safe at any phase boundary: the host hardening is useful on its
own, and the service can be removed without unwinding it.
