# agent-cloud Architecture

The 5-minute map — read it first, human or AI. It tells you *what* agent-cloud is,
*how the pieces fit*, and *where to go next* for depth.

agent-cloud is the uhstray.io **"business-as-code" homelab platform**: a single public
monorepo running real privacy-focused infrastructure (NetBox, Authentik, OpenBao,
Semaphore, Caddy, an inference plane, AI agents), where every control is version-controlled
config a tool applies idempotently — never a UI click. It is deliberately open *because*
site identity (real IPs, FQDNs, credentials, topology) is a parameter living only in the
private **site-config** repo; this repo holds templates, placeholders, and code.

## The Three Doc Layers (read in this order)

```
   PRINCIPLES.md      The durable constitution. Small set of rules + deliberate trade-offs
   (the WHY,           every service, playbook, and agent must obey. The TIEBREAKER: when a
    the tiebreaker)    deeper doc disagrees, this wins; when it is silent, defer downward.

   ARCHITECTURE.md    >> YOU ARE HERE << The map. The 4-layer model, how the subsystems
   (the MAP)           connect, and a pointer from each subsystem to the doc that owns it.
                       Connective tissue — it simplifies, it does not re-state.

   plan/architecture/  The per-area HOW and why-in-depth (00..07, deep + dense).
   plan/development/   The execution ROADMAP — service-by-service implementation plans.
   CLAUDE.md           The turn-by-turn operational rules an agent follows while editing.
```

If you only read one thing, read `PRINCIPLES.md`. If you only read two, add this file.

## The 4-Layer Model

Every privileged change flows top-to-bottom and **never the inverse**:

```
  +---------------------------------------------------------------+
  |  AI LAYER          AI PROPOSES                                 |
  |  NemoClaw, NetClaw, WisBot, Cowork, WebSmith                   |
  |  (backed by skynet: local-first OpenAI-compatible /v1)         |
  |  ...emits desired-state proposals only...                     |
  +------------------------------|--------------------------------+
                                 v
  +---------------------------------------------------------------+
  |  GUARDRAIL LAYER   GUARDRAILS VALIDATE                         |
  |  OpenBao (secrets) + OPA/Kyverno (policy) + Authentik (identity)|
  |  ...a proposal that fails validation never reaches automation...|
  +------------------------------|--------------------------------+
                                 v
  +---------------------------------------------------------------+
  |  AUTOMATION LAYER  AUTOMATION EXECUTES                         |
  |  Semaphore (orchestration) + Ansible (idempotent playbooks)    |
  |  ...deterministic, auditable, re-runnable...                  |
  +------------------------------|--------------------------------+
                                 v
  +---------------------------------------------------------------+
  |  INFRASTRUCTURE    Proxmox VMs / Podman (+Docker) / Caddy edge |
  |  the dumb container hosts where workloads actually run         |
  +---------------------------------------------------------------+
```

### Why four layers and not three (the AI Invariant)

A conventional platform has three layers: something proposes, automation runs it,
infrastructure hosts it. agent-cloud inserts a **fourth, non-optional** guardrail layer
*between* AI and automation, making the AI a strictly upstream proposer.

It exists for one load-bearing safety property, the **AI Invariant**
([PRINCIPLES §4](PRINCIPLES.md#4-the-ai-invariant)): *AI proposes, guardrails validate,
automation executes — never the inverse.* An agent may recommend improvements to anything,
**including its own pipelines and prompts, but may never apply them without human review** —
self-improvement is propose-only. No AI agent may *be* a standing autonomous reconciler, nor
author the human-unmediated target of a reconciliation controller. The shape this forbids: an
unattended convergence loop with an LLM authoring its own target and destructive (`down -v`)
reach. Collapsing guardrails back into automation re-opens exactly that shape. Weakening this
rule requires an explicit, recorded human decision.

## Component Map

Each major subsystem has exactly one owning doc. Start at the layer in the diagram, then
jump to that doc.

| Subsystem | What it is | Layer | Owning doc |
|---|---|---|---|
| **skynet** | Local-first, OpenAI-compatible `/v1` inference plane backing all agents (supersedes WisAI's Ollama/Open WebUI) | AI | [plan/development/06-inference-skynet.md](plan/development/06-inference-skynet.md) |
| **AI agents** (NemoClaw, NetClaw, WisBot, Cowork) | Headless / network / Discord / interactive agents that emit desired-state proposals | AI | [plan/development/06-inference-skynet.md](plan/development/06-inference-skynet.md) |
| **WebSmith** | Website-building agent; produces a signed `SPEC.md` per site, handed to a service deploy | AI | [07-website-building-agent.md](plan/architecture/07-website-building-agent.md) |
| **OpenBao** | Sole source of truth for every credential | Guardrail | [04-credentials-access.md](plan/architecture/04-credentials-access.md) |
| **OPA / Kyverno** | Policy-as-code: agent-action authorization (OPA now), k8s admission (Kyverno, post-migration) | Guardrail | [plan/development/03-guardrails-governance.md](plan/development/03-guardrails-governance.md) |
| **Authentik** | Central IdP/SSO; groups are the source of truth for human identity → roles | Guardrail | [04-credentials-access.md](plan/architecture/04-credentials-access.md) |
| **Semaphore** | Orchestrator; every privileged, state-changing action runs through it for audit + AppRole injection | Automation | [01-automation-model.md](plan/architecture/01-automation-model.md), [04-credentials-access.md](plan/architecture/04-credentials-access.md) |
| **Ansible + composable task library** | Idempotent playbooks; the credential boundary lives here, not in `deploy.sh` | Automation | [01-automation-model.md](plan/architecture/01-automation-model.md) |
| **Proxmox / Podman / Docker** | VM hosting + container runtimes (rootless Podman default; Docker is a named exception) | Infrastructure | [05-platform-infra.md](plan/architecture/05-platform-infra.md) |
| **Caddy** | Sole HTTPS ingress + only TLS terminator (LE via CloudFlare DNS-01) | Infrastructure | [05-platform-infra.md](plan/architecture/05-platform-infra.md) |
| **step-ca / hickory-dns** | Internal private CA (stable root) + internal DNS (zones-as-code) | Infrastructure | [05-platform-infra.md](plan/architecture/05-platform-infra.md) |
| **NetBox** | Authority for network / IPAM, populated by the Diode discovery pipeline | Infrastructure | [plan/development/04-netbox-discovery.md](plan/development/04-netbox-discovery.md) |
| **Observability** (Grafana, Prometheus, Loki, Alloy, Tempo) | Self-hosted telemetry; opt-in by declaration, `service.name` is the join key | cross-cut | [06-observability-instrumentation.md](plan/architecture/06-observability-instrumentation.md) |
| **Service onboarding & tiers** | The one-shape-per-tier model + onboarding checklist for new services | cross-cut | [02-service-onboarding.md](plan/architecture/02-service-onboarding.md) |
| **Testing / CI / quality gates** | The lint + test + leak-prevention gates every PR passes | cross-cut | [03-testing-ci-quality.md](plan/architecture/03-testing-ci-quality.md) |
| **Doc standards & index** | How docs are structured, status values, the full doc index | cross-cut | [00-foundation-standards.md](plan/architecture/00-foundation-standards.md) |

## Load-Bearing Decisions (one line each)

Every line below is a *rule*; follow the link for the *why* and the as-built caveats.

- **Config-as-code** — configuration is data a tool applies idempotently, never a UI click or ad-hoc curl; a control you can't diff is a control you can't review. ([PRINCIPLES §1](PRINCIPLES.md#1-config-as-code))
- **One codebase, no forks** — environments differ only by inventory vars + compose overlays + `{{ }}` references; site identity is a parameter, kept entirely in site-config. ([PRINCIPLES §1](PRINCIPLES.md#1-config-as-code))
- **Composability / tiers** — one service shape per tier (auxiliary minimal → infra/AI/automation full ceremony); tier is the *input* to the shape, and deviations are declared (`shape: bespoke`), not silent. ([PRINCIPLES §2](PRINCIPLES.md#2-composability), [02](plan/architecture/02-service-onboarding.md))
- **Build foundationally** — fix the mechanism not the symptom; encode every one-off as reusable automation — a fix that lives only in your shell history is a defect. ([PRINCIPLES §2](PRINCIPLES.md#2-composability))
- **Rootless Podman default** — reach for rootful/Docker only when a capability rootless cannot grant forces it, and the deploy must name that capability. ([PRINCIPLES §2](PRINCIPLES.md#2-composability), [05](plan/architecture/05-platform-infra.md))
- **The guardrail triad** — OpenBao (secrets) + OPA/Kyverno (policy) + Authentik (identity) sit between AI and automation; one authority per concern, reflections are read-only. ([PRINCIPLES §3](PRINCIPLES.md#3-identity-secrets--the-guardrail-triad))
- **deploy.sh boundary** — `deploy.sh` is container-lifecycle-only (pull, `up`, wait, migrate); it may *never* touch OpenBao — the credential path is OpenBao → Ansible → Jinja2 → `.env`. ([PRINCIPLES §3](PRINCIPLES.md#3-identity-secrets--the-guardrail-triad), [01](plan/architecture/01-automation-model.md))
- **Bootstrap (genesis) exception** — OpenBao + Semaphore manage their own credentials on a fresh local deploy (the secret store + orchestrator can't fetch from a system that doesn't exist yet); this is the *single* sanctioned carve-out from the deploy.sh boundary. ([PRINCIPLES §3](PRINCIPLES.md#3-identity-secrets--the-guardrail-triad), [04](plan/architecture/04-credentials-access.md))
- **Bounded credentials** — every AppRole `secret_id` carries finite TTL (90d) + `token_num_uses`; `TTL=0` is a defect; rotate Create → Verify → Retire, and an identity reads only its own enumerated paths (no wildcards). ([PRINCIPLES §3](PRINCIPLES.md#3-identity-secrets--the-guardrail-triad), [04](plan/architecture/04-credentials-access.md))
- **RBAC: Org > Dept > Team > Role + Access Level** — identity decomposes into org structure with an orthogonal privilege tier (Admin → Maintainer → Developer → User); Org is the tenant boundary; access is provisioned from Authentik groups by role, never hand-granted. **[TARGET]** ([PRINCIPLES §3](PRINCIPLES.md#3-identity-secrets--the-guardrail-triad), [04](plan/architecture/04-credentials-access.md))
- **AI Invariant** — AI proposes, guardrails validate, automation executes, never the inverse; self-improvement is propose-only; no agent is a standing reconciler. (hard constitutional limit) ([PRINCIPLES §4](PRINCIPLES.md#4-the-ai-invariant), [01 §8](plan/architecture/01-automation-model.md))
- **Everything through Semaphore** — every privileged, state-changing action flows through Semaphore for audit + AppRole injection; direct SSH is read-only break-glass, reconciled by a later redeploy. ([PRINCIPLES §5](PRINCIPLES.md#5-automation--promotion), [04](plan/architecture/04-credentials-access.md))
- **Authored convergence + liveness** — the only always-on loop is liveness (a crashed/rebooted container self-heals); config convergence is always from a *human-authored* Git target, on a trigger or a timer, never free-running. ([PRINCIPLES §5](PRINCIPLES.md#5-automation--promotion))
- **Reversibility tags** — each deploy carries `reversible: true|false`; an irreversible (migration) deploy off a non-main branch surfaces a confirmation gate + snapshot. ([PRINCIPLES §5](PRINCIPLES.md#5-automation--promotion))
- **TLS at the edge** — Caddy terminates TLS exactly once; backends speak plain HTTP only on a firewall-enforced trusted network; consume public TLS, run a private CA, never become one. ([PRINCIPLES §6](PRINCIPLES.md#6-infrastructure--resilience), [05](plan/architecture/05-platform-infra.md))
- **Runtime-artifacts + tmpfs secrets** — target hosts get rendered runtime artifacts (no repo clone) and tmpfs-mounted secrets from OpenBao (no persistent `.env`), so leaks fail closed by construction. **[TARGET]** ([PRINCIPLES §6](PRINCIPLES.md#6-infrastructure--resilience), [01](plan/architecture/01-automation-model.md))
- **SPOFs carry a recovery story** — any infra/edge SPOF (Caddy, OpenBao, step-ca, DNS) ships a documented, tested rebuild-from-OpenBao+site-config path before it carries traffic. ([PRINCIPLES §6](PRINCIPLES.md#6-infrastructure--resilience))
- **Observability by declaration** — logs are free, metrics are two labels, traces are two env vars, `service.name` joins all three; no per-service `prometheus.yml` edits, dashboards/alerts as code. ([PRINCIPLES §7](PRINCIPLES.md#7-observability), [06](plan/architecture/06-observability-instrumentation.md))

## As-Built vs Target (honesty note)

PRINCIPLES.md states the rules the platform commits to; several describe a **target not yet
built**, tagged **[TARGET]** there. Do not reason as if a `[TARGET]` guarantee already holds.
The big ones, roughly in priority order:

- **Runtime-dir / tmpfs-secrets model — top build priority, NOT yet true.** Today
  `manage-secrets.yml` renders `.env` into a full repo clone on the target. Until
  artifact-render/copy + sparse-checkout + engine-secret delivery ship, `.gitignore` + the
  pre-commit/CI trufflehog gate are the *only* (fragile) secret boundary. The liveness loop and
  every "secrets never in the clone" claim depend on this landing first.
- **RBAC role-based provisioning — designed, not built.** The Org>Dept>Team>Role + Access-Level
  schema and the (identity → per-service role) provisioning automation don't exist yet;
  Semaphore user `stray` was hand-set to admin (a stopgap). See [04](plan/architecture/04-credentials-access.md).
- **AppRole TTL=0 defect.** `manage-approle.yml` hardcodes `secret_id_ttl: 0` — the
  highest-severity, lowest-effort fix; the bounded-credential rule is the target.
- **nocodb credential-boundary violation.** `nocodb` deploy.sh still sources `bao-client.sh`
  (legitimate only for the genesis services OpenBao + Semaphore); retiring it is the concrete
  acceptance test that the deploy.sh boundary is real.
- **Scheduled config reconciliation** is INVESTIGATE/[TARGET]; today convergence is
  trigger-only and only liveness self-heals.
- **Alerting before tracing**, the host firewall rollout backing the "trusted network" claim,
  and the `assert-orchestrated.yml` wiring are all in flight, not done.

For the full caveats and open tensions (static DB passwords vs. dynamic leases, conformance
ceremony vs. velocity, one dependency DAG vs. two), read the as-built notes inline in
**PRINCIPLES.md** and the **Open tensions** section at its end.
