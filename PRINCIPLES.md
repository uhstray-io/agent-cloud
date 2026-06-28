# Platform Principles

This is the constitution for **agent-cloud**, the uhstray.io "business-as-code" homelab platform.
It states the small set of durable rules every service, playbook, and AI agent must obey, and the
deliberate trade-offs the platform has chosen. It sits above the detailed design docs in
`plan/architecture/` (the *how* and the *why-in-depth*) and `CLAUDE.md` (the operational rules an
agent follows turn-by-turn). When those disagree with each other, this document is the tiebreaker;
when this document is silent, defer to `plan/architecture/`.

Each principle is a rule plus a terse *why*. Where a rule describes a target the platform has not
yet built, it is tagged **[TARGET]** with an honest as-built note - do not reason as if a [TARGET]
guarantee already holds.

---

## 1. Config-as-Code

**Configuration is data a tool reads, applied idempotently - never a UI click or an ad-hoc curl.**
Semaphore templates (`templates.yml`), OpenBao policies (`.hcl`), env contents (`.j2`), Caddy
fragments, Authentik blueprints, OPA Rego, and branch-protection rulesets are version-controlled and
applied by idempotent playbooks. The gold standard is `setup-templates.yml`: list -> PUT-if-exists /
POST-if-new.
*Why: a control you can't diff is a control you can't review; a rule changed by hand in a UI is
invisible to the next operator and drifts from the codebase. This is what makes a PR security review
equal a review of the running system.*

**One codebase, no forks - environments differ only by inventory vars and compose overlays.**
Exactly one `deploy.sh`, one `compose.yml`, one template set per service. Local-vs-prod differences
live in inventory host-vars, `compose.local.yml` overlays, and `{{ }}` references - never in copied
or forked files.
*Why: forks drift, parameters don't. Local validation only predicts prod behavior if the same
artifacts run in both places.*

**Site identity is a parameter, not a code property.** The public repo (`agent-cloud`) holds only
templates, placeholders, and code; every real IP, FQDN, credential, and topology fact lives only in
the private repo (`site-config`). A second site is added as configuration (`vault_secret_prefix`,
`container_engine`, `service_url`), never as a forked file.
*Why: this is exactly what lets agent-cloud be legitimately open while running real infrastructure.*

---

## 2. Composability

**Every service follows one shape per tier; deviations are declared, not silent.**
A service is `platform/services/<name>/` with `deployment/` (compose.yml, deploy.sh, templates/*.j2,
README.md) and, for AI/website tiers, `context/`. The required shape is parameterized by **tier**:
auxiliary tier is the minimal set; infrastructure/AI/automation tiers add the full ceremony. A
service that genuinely cannot fit (NetBox needs Docker for `CAP_NET_RAW`) declares
`shape: bespoke` with a one-line justification - a *forced* exception, not debt.
*Why: uniformity is what makes 20+ services AI-navigable under one onboarding checklist. But "exactly
one shape" and "tier drives integration weight" only coexist if tier is the input to the shape, not a
competing rule. As-built: several service dirs are still empty scaffolds or non-conformant; the
conformance lint (Section 7) must be tier-aware so it enforces this instead of flattening it.*

**Tier classification drives integration weight.** Before onboarding, classify: runtime OpenBao
access -> dedicated AppRole; AI/GPU -> AI tier + `context/`; DB + workers + multi-container -> automation
tier + dedicated VM; otherwise auxiliary (3-phase deploy, may co-locate, no scheduled rotation from
day one, uses Semaphore's orchestrator AppRole).
*Why: protects against both over-engineering a static service and under-provisioning an
infrastructure one.*

**Fix the mechanism, not the symptom - build foundationally, never monkey-patch.** A manual one-off
is at best a stopgap; the real fix is its reusable form. New behavior extends the shared composable
task library and `platform/lib/` first; a recurring need is promoted to a reusable task; the second
site/AppRole/service/user inherits the first's recipe.
*Why: the shared `/var/lib/agent-cloud-deploy` dir fixed bind-mounts for every local service at once
- that leverage is the whole point. A hand-grant doesn't survive a redeploy and can't be repeated at
scale - e.g. hand-setting Semaphore user `stray` to admin is a stopgap; the foundational fix is
role-based provisioning ("Human access is provisioned by role", Section 3). Introduce a new
abstraction only when the same fact has provably been hand-copied into 4+ places, not in anticipation.*

**Compose files are runtime-agnostic by construction, not by luck.** Set explicit `container_name`,
use fully-qualified images (`docker.io/library/...`), never the `name:` property on volumes (control
the prefix via `--project-name`), and use simple `KEY=VALUE` env files. `depends_on: service_healthy`
**may** be declared (it documents intent and is honored on Docker and podman-compose >= 1.3.0) but
must **never** be the only thing relied on for ordering - every staged stack also implements
`wait_for_healthy()` / `wait_for_http()` in `deploy.sh` as the floor.
*Why: podman-compose 1.0.6 (in the field) silently ignores `service_healthy`, producing
app-before-database crashes that look flaky. Putting the ordering floor in the deploy script is what
lets one compose file run on both runtimes. CI should flag any compose using `service_healthy` whose
deploy.sh lacks a matching wait - that catches the real footgun without deleting forward-compatible,
Docker-valid declarations.*

**Rootless Podman is the default; rootful/Docker is an exception that names its capability.**
Reach for rootful or Docker only when a capability rootless cannot grant forces it (`CAP_NET_RAW`,
privileged host networking, a deep compose health-dependency chain), and the deploy must state which.
*Why: NetBox's orb-agent needs `CAP_NET_RAW` for scans; prod Semaphore runs rootful podman. Naming
the capability stops privilege escalation by habit.*

---

## 3. Identity, Secrets & the Guardrail Triad

**OpenBao is the sole source of truth for every credential; `.env` on disk is a disposable bridge,
never an origin.** Secrets are generated in Ansible memory only if absent, stored once in OpenBao,
and thereafter reused. Env files are templated fresh every deploy (mode 0600), gitignored, and are
**not** authoritative. There is exactly one reconciler per secret; a second write path (a
secret-generating `deploy.sh`, an on-VM `secrets/` dir, a token written back from `deploy.sh`) is a
defect.
*Why: generate-if-missing/reuse-always is the cure for the original NetBox failure (deploy.sh
regenerated secrets, drifting passwords against live DB volumes). As-built: nocodb and semaphore
`deploy.sh` still `source bao-client.sh` - retiring that is the concrete acceptance test that this
principle is real.*

**deploy.sh is container-lifecycle-only; the credential boundary is Ansible.** A `deploy.sh` may
verify env files exist, pull/build images, run `compose up`, wait for health, and run migrations. It
may **not** call OpenBao, generate/resolve secrets, manage AppRoles, write policy, or touch
Semaphore. Every credential takes exactly one path: OpenBao -> Ansible memory -> Jinja2 -> `.env`.
*Why: the keystone seam all lenses converge on. It bounds blast radius (a compromised deploy script
can't reach OpenBao), keeps redeploys idempotent, and keeps the imperative residue thin. CI-enforced
by grepping `deploy.sh` for `gen_secret`/`put_secret`/`get_secret`/`bao-client`.*

**One authority per concern; reflections are read-only and never invert authority.** Each
cross-cutting concern has a single source of truth: OpenBao (secrets), NetBox (network/IPAM), Git
(desired workload state), the k8s API (live state, post-migration), OPA/Kyverno (policy), the
observability stack (telemetry), `service.name` (the correlation key joining logs/metrics/traces).
Systems that reflect another's state are read-only consumers and never write back to invert it.
*Why: this generalizes a dozen scattered rules into one CI-testable test for any new integration -
"what does this claim authority over, and is anything already authoritative there?" It is what
prevents the slow drift that kills config-as-code platforms.*

**Every credential is bounded in time and uses; `TTL=0` is a defect.** Every AppRole `secret_id`
carries a finite `secret_id_ttl` (default 90d / 2160h) and `token_num_uses` (25). The **only**
unlimited-TTL exception is the Semaphore orchestrator, documented and singular - never silently
copied to a second AppRole.
*Why: a `secret_id` with TTL 0 grants indefinite access from one leaked string. As-built defect:
`manage-approle.yml` hardcodes `secret_id_ttl: 0` / `token_num_uses: 0` and mints a fresh orphaned
secret-id every run. This is the highest-severity, lowest-effort fix on the platform - fix it first,
with a CI guard that fails on `secret_id_ttl: 0` / `token_num_uses: 0` unless the line carries an
allow-comment naming the orchestrator.*

**Verify the new credential before retiring the old - always Create -> Verify -> Retire.** Rotating
any credential (AppRole secret_id, Diode OAuth2, SSH key, DB password) is three phases with a
verification gate against the *live service* between Create and Retire. A failed rotation leaves the
service running on the old credential. Never delete-then-create in one task.
*Why: "verify before hardening" applied to secrets. The dual-valid window costs nothing in steady
state and prevents a self-inflicted outage during the exact operation meant to improve security.*

**An identity reads only its own secrets - enumerated paths, never wildcards.** Each service/agent
gets an AppRole scoped to the exact OpenBao paths it needs, enumerated in `.hcl`.
`secret/data/services/*` is reserved for the Semaphore orchestrator alone; new services never inherit
the wildcard.
*Why: a per-service AppRole means a compromise reads one service's secrets, not the platform's.
As-built: `nemoclaw-read.hcl` grants the wildcard to an AI agent - tighten it, and add a CI lint over
`policies/*.hcl` flagging any `services/*` grant except the allowlisted semaphore policy.*

**Human access is provisioned by role, never granted by hand: Organization > User Type > Access
Level.** RBAC has one hierarchy - **Organization** (the highest division; tenants are orgs) > **User
Type** > **Access Level**, highest-to-lowest **Admin -> Maintainer -> Developer -> User** - applied
per tenant and per project. Authentik groups are the source of truth
(`<org>-admins/-maintainers/-developers/-users`); a provisioning mechanism maps each group to
per-service roles (Semaphore admin-flag vs project owner/manager/task_runner/guest, NetBox
permissions, ...). Any Admin-level user is provisioned with admin rights and all-project visibility
automatically - never a per-service hand-grant.
*Why: provisioning is the only way user management scales across many tenants/projects without drift;
it is the no-monkey-patch rule (Section 2) applied to people. **[TARGET]** the provisioning automation
and the `<org>-<level>` group set are not built yet - Semaphore user `stray` was hand-set to admin (a
stopgap), and `platform-admins` is today's sole admin group, being renamed `uhstray-admins` (the
uhstray-org Admin tier). Do not reason as if role-based provisioning already holds.*

**`no_log` is a scalpel for the credential boundary - never a blanket, never a ban.** Scope `no_log`
to OpenBao auth/fetch/generate/store, shared-reads, and secret-templating. Deploys, health checks,
waits, and verification must **not** be silenced.
*Why: this resolves a live doc contradiction. `CLAUDE.md` (the rule we live by) mandates scoped
no_log and records a real incident where a deploy.sh failure was censored by misapplied no_log; doc
03's blanket *ban* is wrong because the `redact_secrets` callback it presupposes does not exist - a
ban today would leak ~94 credential tasks into Loki. The CI check is a **scope** check (fail when a
non-credential task carries no_log, and when a credential task lacks it), not a presence check. The
callback, when built and verified, is additive defense-in-depth, never a replacement.*

**Leak prevention for the public repo is defense-in-depth, automated.** A pre-commit hook (local) +
a CI gate (trufflehog + RFC1918 + credential-pattern grep) + log-value redaction. The manual pre-push
grep is a human backstop, never the primary control. A new service without leak tests cannot merge.
*Why: on a public repo a leaked credential is exposed the instant it's pushed; one skippable gate is
not enough. Note: commit `aecd47d` added real-looking `.env` files to public history - treat as a
live exposure incident (inspect, rotate any real values, scrub via the admin break-glass path).*

**Site identity is enforced structurally, not by a broad regex.** Keep the trufflehog + RFC1918 +
public-IP grep. Add (a) a curated denylist of real site-domain suffixes / prod IP ranges - kept in
`site-config`/CI, asserted absent from `agent-cloud`; and (b) a contract test that renders every
service's templates against the `site-config` inventory *schema* (not real values), proving
agent-cloud is deployable only when paired with site-config.
*Why: a broad "looks like an FQDN" regex fires on `docker.io/...` images and internal service DNS
names that are *supposed* to be public, training people to wave the gate through. The render-test is
structural and low-false-positive; the denylist targets actual leaks.*

---

## 4. The AI Invariant

**AI proposes, guardrails validate, automation executes - and never the inverse.** An AI agent
(NemoClaw, NetClaw, Cowork, WebSmith, Wisbot) may only emit desired-state proposals into
trigger-converged or one-time pipelines that pass through the guardrail layer (OpenBao, OPA, Kyverno)
before the automation layer executes them. No AI agent may **be** a standing autonomous reconciler,
**nor author the human-unmediated target** of a RECONCILED controller (ArgoCD/Kyverno, post-k8s).
Weakening this requires an explicit, recorded human decision.
*Why: this is the single load-bearing safety property and the reason the architecture is four layers
not three. The shape it forbids is an unattended convergence loop with an LLM authoring its own
target and `down -v` reach. Every future agent capability is checked against this rule.*

---

## 5. Automation & Promotion

**Every privileged, state-changing action flows through Semaphore.** All deploys, secret operations,
policy/AppRole changes, SSH key distribution, and hardening go through Semaphore for an audit trail,
AppRole injection, and idempotent re-runs. Direct SSH is **read-only break-glass** only; any state
touched that way is reconciled by a subsequent Semaphore redeploy and documented in an issue.
*Why: the audit trail is the only way to answer "who changed this and when" after an incident.
As-built: `assert-orchestrated.yml` exists but is imported by zero deploy plays - so this rule is
convention-only today. Wire it into every `deploy-*.yml` / `clean-deploy-*.yml`, but as
verify-then-harden (its own header marks the Semaphore marker PROVISIONAL): first capture a real
Semaphore task's env to confirm the marker, then warn-only on one auxiliary play, then hard-fail, then
roll out. Big-bang wiring on an unverified marker would hard-fail every deploy.*

**Sanctioned manual carve-outs are named and bounded so they cannot widen.** Two, and only two:
(a) **genesis-of-trust** - first OpenBao unseal, first token, first secret_id - which necessarily
precedes the orchestrator, but is committed as code in `bootstrap-local-dev.yml` and run on
localhost, never hand-typed; and (b) the **Repository-admin break-glass** path for history-scrub /
force-push. Every such action is recorded and reconciled by re-applying the code.
*Why: an unnamed exception (like the TTL=0 one) metastasizes into "manual is fine when convenient."
Naming them loudly keeps them singular.*

**Convergence is authored, never free.** The Ansible/Semaphore plane is trigger-converged; nothing
self-heals config. Every mutating `command`/`shell`/`uri` task must prove idempotency with a
read-guard before the mutation and an honest `changed_when`.
*Why: idempotency is a per-task claim the author can get wrong (manage-approle's secret-id POST is
the proof). The CI rule targets the real defect: fail a *mutating* command/shell task with
`changed_when: true` that has no preceding read-guard - not tasks already gated by a `when:` (e.g.
the deploy.sh invoker, which is inherently always-changing). Teach the read-guard pattern, not a
reflexive allow-comment.*

**Single-site reconciles liveness, not config.** Continuous config reconciliation
(ArgoCD/Kyverno/ESO) is deferred to multi-site k8s. The only standing loop on the compose tier is
**liveness**: a crashed or post-reboot container self-heals. Config converges only when Semaphore
fires.
*Why: daemonless Podman means `restart:` policies do not survive a host reboot, so a 3am crash stays
dead until a human notices. Liveness is the one cheap loop that doesn't smuggle autonomous config
mutation into a deterministic executor. Constraints: the liveness unit must `start` the existing
container, never `up` (which re-reads possibly-drifted on-disk state); it is a dedicated composable
task (`configure-podman-systemd.yml`) invoked by Ansible as the final deploy phase - **not** inside
deploy.sh (which would breach the lifecycle-only boundary and needs no business owning
`enable-linger`); it is engine-parameterized (Quadlet/systemd for Podman, native `restart` + systemd
wrapper for Docker/NetBox); and it ships **after** the runtime-dir split, since the unit's
EnvironmentFile must point at Semaphore-exclusive state.*

**Branch deploys to prod are classified reversible vs irreversible.** Each deploy playbook carries a
`reversible: true|false` flag. When `service_branch != main` and `reversible == false` (migrations,
destructive ops, schema changes), Semaphore surfaces a confirmation gate and pairs the run with a
volume/Proxmox snapshot.
*Why: the branch-testing workflow promises "instant rollback by re-deploying main," but migrations
persist. Tagging reversibility makes the one-way risk visible at launch, not after corruption.*

---

## 6. Infrastructure & Resilience

**TLS terminates once, at the edge; backends speak plain HTTP on an *enforced* trusted network.**
Caddy is the sole HTTPS ingress and only TLS terminator. Internal traffic is plain HTTP unless a
backend protocol genuinely requires TLS (prefer the internal CA over `tls_insecure_skip_verify`).
Plain-HTTP backends are permitted **only** on a host where `apply-firewall.yml`'s default-deny-inbound
(except Caddy + mgmt SSH) is applied and verified by `validate-all.yml`.
*Why: terminating once keeps the cert surface to one place and avoids unrotated self-signed sprawl.
But "trusted network" is a security claim only a firewall can back. As-built caveat: the host firewall
is still a pending canary (task #16) - finish the rollout and make it a mandatory onboarding step
(Phase 1) before relying on the trust boundary for any new service.*

**Consume public TLS, run a private CA; never become a public CA.** Browser-trusted certs via Caddy +
CloudFlare DNS-01 (incl. On-Demand TLS for SaaS tenant domains); internal/LAN certs from the step-ca
private CA whose stable root survives redeploys and volume wipes.
*Why: LE can't validate non-public names; an ephemeral per-instance root forces a re-trust on every
wipe. The stable shared root trusted once via `make local-tls-trust` is the load-bearing property.*

**Infrastructure-tier SPOFs carry a tested recovery story before they carry traffic.** Any
infra/edge service (Caddy, OpenBao, step-ca, central DNS) is a platform-wide SPOF and must ship with
a documented, tested recovery path: where its state lives, how it is backed up, and how it rebuilds
from OpenBao + site-config.
*Why: step-ca's root/intermediate keys live ONLY in `step-ca-data` (lose the volume, lose the stable
root that all internal TLS trusts); Caddy's LE certs live only in `caddy-data` (a rebuild storms LE
rate limits with no traffic served). The DR plan is still PLANNING - this principle forces the backup
question at onboarding instead of after the first volume loss.*

**[TARGET] The git clone is read-only source; generated state lives in a separate runtime dir.**
Generated secrets and rendered env land only in the per-service runtime dir (`~/services/<name>/`,
mode 0700); the clone (`~/agent-cloud/`) is read-only source. Filesystem isolation - not `.gitignore`
- is the boundary, because isolation fails closed while `.gitignore` fails open.
*As-built: NOT YET TRUE. `manage-secrets.yml` renders env into the clone today; `setup-runtime-dir.yml`
/ `sparse-checkout.yml` / `run-deploy.yml` do not exist. Until they ship, `.gitignore` + the
pre-commit trufflehog gate are the real (fragile) boundary and the `.gitignore` coverage check is a
**hard** CI gate, not a backstop. Build the runtime-dir split - the liveness loop and any honest
secret-isolation claim depend on it. Do not reason as if the clone is secret-free today.*

---

## 7. Observability

**Observability is opt-in by declaration, never bespoke per service.** Logs are free (Alloy socket
discovery); metrics are two compose labels (`prometheus.io/scrape`, `prometheus.io/port`) consumed by
Prometheus docker_sd; traces are two env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`).
The container name is the canonical `service.name` joining all three. No per-service `prometheus.yml`
edits; dashboards and alerts are provisioned as code.
*Why: a new service must not land observability-blind. The single correlation key is what makes
conversational triage deterministic instead of guesswork.*

**Ship metrics -> alerts before tracing; set a retention/cardinality budget before remote-write.**
Deliver auto-discovered metrics + exporter sidecars for the stateful DBs + at-minimum
service-down/error-rate/saturation alerts to a Discord webhook (secret in OpenBao) before standing up
Tempo. Declare Prometheus retention, a series cap, Tempo `block_retention`, and a relabel guardrail
dropping high-churn ephemeral containers - as inventory vars, so local and prod differ only by value.
*Why: today there are no alerts, so failures are human-discovered. Tempo's metrics-generator
remote_writes RED + service-graph series back into Prometheus - standing it up before basic alerting
inverts the value-to-cost order and risks the o11y stack eating the disk it's meant to watch.*

**Every signal path is self-hosted, least-privilege, and free of secrets-in-labels.** Telemetry
never leaves the platform (analytics disabled). The Grafana MCP token is OpenBao-managed and
read-only; metrics endpoints sit behind Caddy + Authentik; never put secrets or PII in metric names
or labels.
*Why: socket access for Alloy/Prometheus is the same trust surface already accepted - a deliberate,
bounded decision keeps observability from quietly becoming an exfiltration channel.*

---

## CI Gate Sequencing (meta-rule)

A CI gate ships only **after** the mechanism it enforces exists and is verified, and gates are
ordered by **blast-radius of the defect they prevent**, not by ease of writing the grep. The
foundation order is: (1) fix `manage-approle.yml` TTL=0 + its regression guard; (2) reconcile
`deploy-all.yml` against the 21 on-disk plays - the canonical full-deploy currently covers ~4
services, a correctness bug a recovery relies on; (3) wire `assert-orchestrated.yml` (verify-then-
harden); (4) compose structural validation + the `service_healthy`/wait-pairing lint +
deploy.sh-secret-free grep; (5) build & verify the `redact_secrets` callback, *then* reconcile the
no_log scope check. Defer the service manifest, `make new-service` scaffold, domain-collision
registry, and reversibility tags until the foundation gates prove stable - they are real but not yet
load-bearing at 21 services.

---

## Open tensions

- **Runtime-dir split vs. shipping velocity.** The secret-isolation boundary (Section 6) is the
  stated #1 security property but is unbuilt; until it lands, several other principles (liveness
  self-heal, "secrets never in the clone") are aspirational. Building it is prioritized, but every
  day it is deferred, `.gitignore` is the only real control.
- **Conformance ceremony vs. auxiliary-tier velocity.** The tier-aware conformance lint is the
  reconciliation, but the exact line between "auxiliary minimal" and "full ceremony" will be
  re-litigated as trivial services (docs sites) are onboarded. Watch for the lint being felt as
  bureaucracy and routed around.
- **Static DB passwords vs. dynamic OpenBao leases.** Dynamic 1h-lease DB creds are the security
  ideal but require runtime lease-renewal that the daemonless compose tier cannot safely support
  without a sidecar - a deferred, explicit **post-k8s** decision. Interim: generate-once + scheduled
  Create->Verify->Retire rotation (90d), proven on a non-customer-facing DB first.
- **`service_healthy` during the podman-compose upgrade.** The "declare but don't rely on" rule holds
  until the whole fleet reaches >= 1.3.0; the staged deploy-script wait remains the documented
  fallback and NetBox's chosen path even after.
- **Dependency ordering: one DAG vs. two.** Steady-state inter-service dependencies (declarative,
  consumed by `deploy-all.yml`) and genesis trust-ordering (irreducibly imperative,
  `bootstrap-local-dev.yml`) are different concerns. They are cross-checked for consistency in CI
  rather than forced into a single artifact - revisit if that check proves insufficient.
