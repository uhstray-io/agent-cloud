# Domain Overlay — SaaS

Apply on top of a base preset (most commonly `nextjs-typescript.md`, `sveltekit.md`, `rails.md`, or `django.md`) when building a software-as-a-service product. Captures everything SaaS-specific: auth, billing, workspaces, RBAC, audit, admin, status, support, customer API.

---

## Decisions to layer onto the base preset

### Sign-up and onboarding
- **Friction**: minimal (email only) vs verified (email click-through) vs OAuth-only.
- **Email verification**: required before access, or deferred until first action.
- **Onboarding flow**: zero-state walkthrough, sample data, checklist.
- **Activation metric**: define the moment of value (e.g., "first project created and shared").
- **Demo / sample workspace** for new users.
- **Time-to-first-value** tracking.

### Auth
- **Identity model**: email-only, OAuth providers, magic link, passkeys, SSO.
- **Providers**: Clerk, Auth.js, Supabase Auth, WorkOS (enterprise SSO), Stytch, Hanko.
- **Multi-factor authentication**: optional / required by plan / required for admin.
- **Session management**: idle timeout, absolute timeout, multi-device.
- **Password policy** or passwordless-only.
- **Social login** providers (Google, Apple, GitHub, etc.) per audience expectation.

### Workspace / team model
- **Single-user vs multi-user accounts**.
- **Workspace / team / organization** entity.
- **Invitations**: email-based, code-based, domain-based auto-join.
- **Roles**: Owner / Admin / Member / Viewer at minimum; custom roles for complex products.
- **RBAC enforcement**: at the API layer (always), at the UI layer (for hiding affordances).
- **SCIM provisioning** for enterprise.

### Billing
- **Model**: flat, per-seat, usage, hybrid.
- **Trial**: free trial (card-on-file? no card?), reverse trial, no trial.
- **Freemium tier**: yes/no, with what limits.
- **Plans**: name, price, included usage, overage rates.
- **Provider**:
  - **Stripe Billing** (default; most flexible).
  - **Paddle** or **Lemon Squeezy** (merchant of record; handles VAT).
  - **Recurly**, **Chargebee** for complex enterprise.
- **Proration** on upgrades / downgrades.
- **Failed-payment recovery** (Stripe Smart Retries or custom).
- **Invoicing** (B2B): self-serve invoice download, NET-30 terms, PO support.
- **Tax**: Stripe Tax, Paddle (MoR), Avalara.
- **Procurement integrations** for enterprise (Vendr, Coupa).

### Permissions UX
- **Clear when a user can't do something** and *why*.
- **Permission boundary tests** in the test suite.
- **Granular permissions UI** for admins.
- **Audit log** of permission grants and revocations.

### Audit logging
- **What gets logged**: authentication events, permission changes, data exports, admin actions, plan changes.
- **Retention**: at least 1 year for SOC 2 / ISO compliance; longer for HIPAA, financial.
- **Tamper resistance**: write-only, separate storage (PostgreSQL with restricted role, or a dedicated service).
- **Customer-facing audit log** (a premium feature in many SaaS).

### Admin console
- **Internal-only or also customer-admin**.
- **Customer impersonation** (powerful — log every use).
- **Manual entitlement override** (bump a customer's quota for support).
- **Refund / credit issuance** with audit trail.
- **Feature flag toggling** per account.
- **Plan switching** with proration.

### Customer-facing API and developer experience
- **Public API**: REST, GraphQL, both.
- **Auth**: API keys (per workspace, per user, per integration), OAuth for delegated access.
- **Rate limiting**: per key, per IP, per endpoint.
- **Versioning** strategy.
- **Webhooks**: signing, retry policy, dead-letter, customer-visible logs.
- **SDKs**: official (TS / Python / Go / Ruby) vs OpenAPI-generated only.
- **Developer portal**: docs site (`domains/documentation.md`), API console, key management UI.

### Reliability and operations
- **SLO**: define before launch (e.g., 99.9% monthly uptime for the API).
- **Status page**: Statuspage, Instatus, Atlas-status, openstatus.
- **Incident response runbook**.
- **On-call rotation** (PagerDuty, Better Stack on-call, Opsgenie).
- **Postmortem template** for incidents.
- **Customer-facing changelog**.

### Customer support
- **In-app chat** (Intercom, Crisp, Plain, HelpScout).
- **Helpdesk** for tickets.
- **In-product help articles** + a docs site.
- **Onboarding emails** (Loops, Customer.io).

### Customer data lifecycle
- **Data export**: customer-initiated download (JSON / CSV).
- **Data deletion**: account-level + sub-resource-level (GDPR Article 17).
- **Workspace deletion**: confirmation flow, grace period, irreversibility.
- **Subprocessor list** (public page maintained).

### Multi-tenancy strategy
- **Shared DB, shared schema** with tenant_id filtering — most common.
- **Shared DB, schema-per-tenant** — Postgres schemas, enterprise tier.
- **DB-per-tenant** — for highest isolation; usually only at high enterprise tiers.
- **Tenant data isolation tests** — automated; verify cross-tenant access is impossible.

### Compliance (when SaaS sells to enterprises)
- **SOC 2 Type II** roadmap (Vanta, Drata, Secureframe).
- **ISO 27001** if EU enterprise.
- **HIPAA** (BAA) if health.
- **GDPR DPA** available for download.
- **Penetration test** annually; reports shared under NDA.
- **Bug bounty / vulnerability disclosure** policy.

### Performance and scale (SaaS-specific)
- **Background work**: queue (Inngest, Trigger.dev, Sidekiq, Celery, Resque, river) — never block requests.
- **Long-running operations**: present as async ("we're working on this; you'll get an email") with progress.
- **Caching layers**: per-tenant or global.
- **Read replicas** for analytics workloads.

### Pricing page UX
- **Transparent pricing vs "Contact sales"** — favor transparent; gate enterprise tier only.
- **Monthly / annual toggle** with annual discount visualized.
- **Per-feature comparison table**.
- **"Most popular" plan emphasis**.
- **FAQ for billing questions**.

### In-app upgrade prompts
- **Soft upgrades**: nudges when nearing limits.
- **Hard upgrades**: required when limit hit; clear path to upgrade without losing flow.
- **Trial-ending banners** with countdown.

---

## Tooling additions per base preset

| If base preset is... | Add these on top |
|----------------------|------------------|
| `nextjs-typescript` | **Clerk** or **Auth.js**, **Stripe Billing**, **Inngest** or **Trigger.dev** (jobs), **PostHog** (product analytics + feature flags), **Sentry**, **Statuspage**/**Instatus**, **Resend** (transactional), **Loops** or **Customer.io** (lifecycle email). |
| `sveltekit` | **Lucia** or **Auth.js for SvelteKit**, **Stripe Billing**, **Inngest**, **PostHog**, **Sentry**. |
| `rails` | **Devise** or built-in auth, **pay** (Stripe wrapper), **acts_as_tenant** (multi-tenancy), **Sidekiq** + **sidekiq-batch**, **PostHog Ruby SDK**. |
| `django` | **django-allauth** (auth), **dj-stripe** or **djstripe** (billing), **django-tenants** (multi-tenancy), **Celery**, **django-axes** (rate limiting), **django-otp** (MFA). |
| `go-templ-htmx` | **stripe-go**, custom session management, **river** (job queue), and significant custom work on multi-tenancy + admin. |

---

## Common SaaS-specific traps

- **Auth migration is multi-week work.** Pick well, then commit.
- **Multi-tenancy added late** is painful. Decide day one.
- **Audit log added when needed** is usually too late. Build it early.
- **Pricing changes** require careful proration; design grandfathering before you need it.
- **Customer impersonation without audit logs** is a compliance failure waiting to happen.
- **Billing edge cases** (refunds, partial credits, grandfathered customers, regional taxes) accumulate fast.
- **Support tickets without product-context** (workspace ID, last actions, plan) waste support time. Pass context.
- **"Just one more admin tool" build-it-yourself** instead of using Retool or similar for internal-only views.

---

## Reference

For the full SaaS considerations checklist, see [`catalogs/considerations-catalog.md` → SaaS product](../../considerations-catalog.md#saas-product).
