## Context

See `proposal.md` — Why. Requirements are in `specs/platform/postiz-publishing/spec.md`
and `specs/platform/service-vm-hardening/spec.md`; this document covers only how they
are met.

Four constraints shape every decision below, and the first three were established by
reading the upstream implementation rather than its documentation:

1. **The upstream OIDC redirect destination is hardcoded** as the configured public URL
   with `/settings` appended, in both the authorization-link and token-exchange paths.
   There is no configurable callback path, so the identity provider's registered
   redirect is dictated by upstream, not chosen by us.
2. **The upstream OIDC scope is hardcoded** to `openid profile email`. A scope-override
   variable appears in upstream's own reference configuration and in its example
   environment file, but the provider code never reads it. Templating it would be dead
   configuration that implies control we do not have.
3. **Identity comes from the userinfo endpoint**, not from an ID token — upstream reads
   the email and subject claims using the access token. The provider's signing key and
   PKCE are therefore irrelevant to whether sign-in works, while the email scope mapping
   is load-bearing: without it, sign-in fails on a missing claim.
4. **Scheduled publishing is executed by a workflow engine.** Since upstream v2.12.0 an
   orchestrator process backed by Temporal performs scheduled work. Because "scheduled
   posts publish without operator intervention" is a requirement rather than a
   nice-to-have, the workflow engine is not optional.

One further constraint is environmental: the target host currently accepts only a
bootstrap password, and the platform's rule is to verify before hardening. The ordering
in the migration plan is therefore load-bearing, not stylistic.

## Goals / Non-Goals

**Goals.** Establish the service on the platform's composable pattern so that it is
reproducible from code; keep the credential surface entirely inside the secret store;
and reach the hardened host state without ever losing access to the host.

**Non-Goals.** No n8n implementation — this change produces the integration contract
only. No migration of data from the developer-machine instance; the deployment is
greenfield by decision, which is what allows a strong generated datastore password
instead of inheriting the upstream default. No object-storage backend for media. No
Kubernetes path; this service targets the Compose/Podman single-site tier like its peers.

## Decisions

### Trim the workflow engine rather than adopting the upstream topology

Upstream's reference deployment runs eight containers: the application, its datastore
and cache, a workflow engine, a *second* datastore for that engine, an Elasticsearch
node, a workflow UI, and admin tooling. We run five — application, datastore, cache,
workflow engine, and the engine's datastore — with the search node, the workflow UI, and
the admin tooling removed.

The workflow engine's auto-setup image supports standard visibility backed by its
relational datastore, so the search node is not required to execute workflows; it powers
advanced workflow *search*, which nothing in the requirements needs. The UI and admin
tooling are operator conveniences, not part of any behavior we specify.

*Alternatives considered.* **Adopt the full upstream topology** — closest to upstream
documentation, which matters when debugging against upstream's issue tracker, but the
search node alone wants substantial memory on a host whose only job is publishing
social posts. Rejected as cost without a requirement behind it. **Pin a pre-workflow
release** to avoid the engine entirely — this matches the configuration that already
worked on the developer machine and would be three containers, but it freezes the
service on an older release, defers the migration rather than avoiding it, and leaves
the "scheduled posts publish" requirement resting on a code path upstream has since
replaced. Rejected.

*This is the highest-uncertainty decision in the change*, because no one upstream
publishes a search-node-free configuration. It is therefore gated: a scheduled post
must be observed publishing on the local instance before the production host is
touched, and the add-back is scoped in advance to a single inventory-gated block.

We also decline to set the engine's dynamic-configuration path. Leaving it unset selects
the engine's built-in defaults, which means no configuration file to ship or maintain.
Upstream mounts a file whose contents are explicitly labelled development-only.

### Configure the application through a mounted config file, not compose environment

Upstream documents three configuration styles. We use the one that mounts a single
config file into the application container, and keep a separate, minimal file beside the
compose definition for values compose itself must substitute — image tags, the published
bind and port, and the two datastore passwords.

The deciding factor is not tidiness. Compose performs `$`-interpolation on values it
reads, and this service takes on the order of sixty provider credential slots. A `$` in
any client secret would be silently mangled under the styles that route credentials
through compose. A mounted config file is never interpolated, so the failure mode does
not exist. It also maps one-to-one onto the platform's existing secret flow — secret
store to automation memory to rendered template — with no intermediate file becoming a
source of truth.

*Alternatives considered.* **Credentials inline in the compose definition** — upstream's
default; rejected because it puts secrets in a committed file and subjects them to
interpolation. **A single environment file serving both compose substitution and the
application** — upstream marks this not-recommended, and it has the same interpolation
hazard.

### Authenticate in the application, not at the reverse proxy

The platform has a working pattern for gating a service at the edge with the identity
provider's forward-authentication outpost, used where an application cannot authenticate
itself. This service can, so we use its native OIDC support.

The decisive reason is the automation endpoint. An edge gate authenticates *every*
request to the hostname, including API-key calls from automation, which would then be
redirected into a browser sign-in flow. Keeping authentication in the application leaves
the automation path clean, which is exactly what the "reachable without an interactive
session" scenario requires. Path-exempting the API prefix at the edge would work, but it
means maintaining an allowlist that must track upstream's routing — a second place to be
wrong.

Because the application performs the token and userinfo exchange itself, it makes
server-side outbound TLS calls to the identity provider. In the local environment the
provider is served with an internally issued certificate, so the internal trust root must
be present inside the container — the same arrangement an existing peer service already
uses for the same reason. Production needs none of this, since the provider there
presents a publicly trusted chain.

### Close registration by configuration, in two deployments

Upstream's registration control permits one sign-up and then closes. That makes the
first sign-in a state transition, and the naive way to handle it is a manual edit on the
host afterwards.

Instead the setting is an inventory variable: deploy with registration open, sign in
once through the identity provider to establish the account, flip the variable, redeploy.
Two orchestrated runs, each reproducible, and the closed state is re-asserted on every
later deployment rather than depending on something a person remembered to do. This is
the platform's "encode the manual step" rule applied to a one-time transition.

### Establish spec organization grouped by platform domain

This change writes the first specs in the store, so it sets the convention: capabilities
live under a domain segment (`platform/…`, leaving room for `agents/…`) with kebab-case
leaf names. A flat layout was the alternative and is fine at one or two capabilities, but
this monorepo already has a hard structural split between platform services and agents,
and retrofitting a domain level later means moving spec paths — which the delta workflow
treats as a rename rather than a no-op.

### Specify host hardening as its own capability

The hardening sequence is applied to a new host by existing, already-built automation,
so it would have been defensible to treat it as implementation detail of this change and
write no spec for it. We specify it separately because its acceptance criterion —
access to the host is never lost — is externally observable behavior with a strict
ordering guarantee, and because every future service host must meet the same baseline.
Writing it once as a capability makes the next host a re-use rather than a re-derivation.

## Risks / Trade-offs

- **The trimmed workflow engine may not execute scheduled work without the search
  node** → Gated behind observing a real scheduled post publish on the local instance
  before production is touched. The add-back is one inventory-gated block, scoped in
  advance, so the recovery is a configuration change rather than a redesign.
- **Removing password authentication is effectively one-way** → Key authentication is
  proven from two independent directions first, with password authentication explicitly
  refused during the proof so a silent fallback cannot masquerade as success; privilege
  escalation is exercised one step earlier, while the password path is still open; and
  the out-of-band console remains as break-glass throughout.
- **Enabling a default-deny firewall can lock out the orchestrator** → Administrative
  allowances precede enforcement and a fresh connection is forced afterwards to prove
  survival. The firewall is applied twice by design — once before the service exists
  (administrative access only) and once after (adding the now-published service port) —
  which is safe because every step is idempotent.
- **Social account connection will fail until redirect URIs are updated at four
  providers** → Not automatable from here; called out as an explicit operator step
  before first use, with account connection as the visible symptom if skipped.
- **The public URL must match the browser's URL exactly, and locally that includes a
  non-standard port** unless the port-forwarding helper is running → The URL is an
  inventory variable per environment rather than a constant, so the two environments
  differ by configuration, not by forked files.
- **The application's own credentials were previously held in a plaintext file on a
  developer machine** → They are moved into the secret store as-is by operator decision,
  which makes rotation a re-run of one seeding step rather than a code change. Treating
  them as compromised and rotating is a follow-up, deliberately not bundled here.
- **Adding a workflow engine and a second datastore roughly doubles the host's resident
  footprint** versus the three-container shape that ran on the developer machine →
  Accepted as the cost of scheduled publishing actually working; the trim above is what
  keeps it to five containers rather than eight.

## Migration Plan

Deployment is phased, and each phase is a separate orchestrated run with a verification
gate that must pass before the next begins.

1. **Host access.** Bootstrap credential into the secret store; issue the host's key;
   distribute keys additively while the password path remains; prove key authentication
   from the orchestrator *and* from an operator workstation with password authentication
   explicitly refused; install the container runtime (which also proves privilege
   escalation still works); harden authentication; apply the firewall in its
   administrative-access-only form. The ordering is the safety property — everything
   before hardening is additive or reversible, and hardening is gated on the two proofs.
2. **Build and validate locally.** Service definition, playbooks, secret seeding,
   identity-provider client, inventory, and orchestrator templates. Then bring the
   service up locally and verify in this order: containers healthy, interface loads over
   TLS, sign-in round-trips through the identity provider, registration closes on the
   second deployment, **a scheduled post publishes**, and the automation endpoint answers
   with a key and refuses without one.
3. **Promote.** Feature branch to the integration branch by pull request with all checks
   green, then a promotion pull request to production so the orchestrator deploys from
   there. Operator-performed prerequisites — the DNS record and the four redirect-URI
   updates — land in this phase.
4. **Production run.** Seed secrets, verify them, apply the identity-provider client,
   deploy the service, verify health, publish the route, then re-apply the firewall so it
   picks up the now-published service port. Re-verify both the public URL and
   administrative access afterwards.
5. **Close registration** after the first production sign-in.

**Rollback** is covered in `proposal.md` — Rollback Plan. The property that makes it
cheap is that the deployment is greenfield: no other service reads this one's data, so
destroying it is contained. Partial rollback at any phase boundary is safe, since the
hardened host is independently useful.
