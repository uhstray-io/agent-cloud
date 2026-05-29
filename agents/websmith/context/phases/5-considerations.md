# Phase 5 — Considerations

> *Everything else — dynamically scoped by purpose. Nothing here is optional just because it's last.*

Phases 1–4 cover purpose, structure, stack, and style. Many things that matter — accessibility compliance, SEO, legal, content production, deployment, CI/CD, monitoring, handoff — don't belong cleanly to any of them. They are **cross-cutting**, and which ones apply depends on what was decided in phase 1.

This phase exists so they aren't forgotten.

---

## 1. Goal of this phase

Produce `spec/considerations.md`: a complete checklist of every cross-cutting concern that applies to *this* site, with a decision recorded for each. Use the archetype(s) from `spec/purpose.md` to scope which checklist sections are required, but always also walk the cross-cutting baseline.

**Always remind the user: this catalog is not exhaustive. Ask them, at the end, if there's anything not on it.**

---

## 2. Inputs

- `spec/intake.md` (if Phase 0 ran) — compliance regimes, operational appetite, hosting regions, must-use vendors.
- `spec/purpose.md` (archetype drives the dynamic checklist).
- `spec/template.md`.
- `spec/tooling.md`.
- `spec/style.md`.

Before drafting, enumerate inherited constraints per [AGENTS.md §5](../AGENTS.md#5-constraint-propagation-matrix). This is the convergence phase — every prior decision shows up here.

---

## 3. How to use this phase

1. Read [`catalogs/considerations-catalog.md`](../catalogs/considerations-catalog.md) — the comprehensive cross-referenced master list.
2. From `spec/purpose.md`, pull the archetype(s). Walk the **archetype-specific** sections of the catalog with the user.
3. Walk the **cross-cutting baseline** (applies to every site) with the user.
4. After both, ask: *"Is there anything I haven't asked about that you think matters for this site?"* Listen carefully.
5. Record every decision (including "we are explicitly not doing X") in `spec/considerations.md`.

---

## 4. Cross-cutting baseline

These apply to every website. Walk all of them.

### 4.1 Accessibility (WCAG)

- Target level: A / AA / AAA
- Regional legal requirements (ADA, AODA, EAA, Section 508, etc.)
- Manual testing plan (screen readers, keyboard-only, voice control)
- Automated checks in CI
- Accessibility statement page
- Conformance evidence retention

### 4.2 SEO and discoverability

- Per-page meta (title template, description, canonical)
- Open Graph and Twitter card defaults and overrides
- `sitemap.xml`
- `robots.txt`
- Structured data (JSON-LD) — which schemas (Article, Product, FAQ, Breadcrumb, Organization, BreadcrumbList...)
- URL structure (slugs, lowercase, hyphens, depth)
- Redirects strategy (301s for moves, old-URL plans)
- Hreflang (if multi-language)
- Search Console / Bing Webmaster setup
- Indexability per environment (block staging)

### 4.3 Performance

- Performance budgets (LCP, INP, CLS, TTFB, JS bundle size, image weight)
- Lighthouse CI threshold
- Image optimization (formats — AVIF/WebP, responsive sources, lazy loading)
- Font loading
- Critical CSS
- Third-party script policy (defer, async, opt-out, consent-gated)
- Caching policy (CDN, browser, service worker)
- Compression (Brotli)
- Preconnect / preload critical resources

### 4.4 Privacy, consent, and legal

- Privacy policy (who writes, when, in what jurisdictions)
- Terms of service
- Cookie policy
- Cookie consent banner (GDPR / ePrivacy / CCPA / LGPD compliance)
- Tracker categorization and gating
- DSAR / data-deletion request process
- Data retention policy
- Sub-processor list (if SaaS)
- Age gating (if relevant)
- DMCA / copyright takedown process (if UGC)

### 4.5 Security

- HTTPS enforced, HSTS preload
- Security headers (CSP, X-Frame-Options, Referrer-Policy, Permissions-Policy)
- CSRF strategy
- XSS hardening
- Input validation policy
- Rate limiting / brute-force protection
- Bot mitigation
- Secrets management (no secrets in client bundle)
- Dependency scanning + patch policy
- Penetration testing cadence (if applicable)
- Bug bounty / vulnerability disclosure policy
- Incident response plan

### 4.6 Content and assets

- Where does content live? (CMS, repo, DB)
- Who creates and approves content?
- Image / video sourcing (rights, licensing)
- Asset optimization pipeline
- Versioning of content
- Translation workflow (if multi-language)
- Default placeholder content
- Launch content readiness

### 4.7 Analytics, monitoring, observability

(Tools were chosen in phase 3; here decide *what to measure*.)
- Event taxonomy (which user actions are tracked, naming conventions)
- Funnels and conversion goals
- Performance RUM (real user monitoring)
- Error tracking sampling / PII scrubbing
- Logs retention policy
- Alerting thresholds and routing
- On-call rotation (if applicable)

### 4.8 Deployment and hosting

- Environments (local, dev, staging, prod; per-PR previews)
- Domain(s) and subdomain strategy
- DNS provider and TTLs
- Certificate management (auto-renew)
- Region(s) and failover
- Backup strategy (DB, file storage)
- Disaster recovery RPO / RTO
- Cost monitoring and alerts

### 4.9 CI/CD specifics

- Branching model
- Required checks before merge
- Preview deployments
- Production deploy trigger (manual / automatic / scheduled)
- Smoke tests after deploy
- Rollback procedure
- Feature flag policy (gradual rollout, kill switches)

### 4.10 Testing strategy

- Unit / integration / E2E coverage expectations
- Visual regression coverage
- Accessibility tests in CI
- Performance tests in CI
- Manual QA checklist before launch
- Staging review process

### 4.11 Internationalization (if relevant)

- Translation source of truth
- Translation provider / workflow
- String extraction process
- RTL CSS handling
- Locale-specific assets
- Region-specific content variants
- Region-specific legal text

### 4.12 Maintenance and handoff

- Who maintains the site after launch?
- Documentation:
  - README for the codebase
  - Runbook for incidents
  - Architecture diagram
  - Content authoring guide
  - Theming guide
  - Onboarding doc for new contributors
- Dependency update cadence (Renovate / Dependabot)
- Browser support matrix and re-evaluation cadence
- Sunset criteria (when to retire features or the site)

### 4.13 Launch readiness

- Launch checklist (DNS cut-over, redirects from old site, monitoring active, analytics confirmed)
- Soft launch / staged rollout / public launch
- Press / comms coordination
- Day-0 monitoring plan
- Day-7 retrospective

---

## 5. Archetype-specific considerations

The full lists live in [`catalogs/considerations-catalog.md`](../catalogs/considerations-catalog.md). Below are the **top-of-mind** items per archetype — surface these explicitly, then drill into the catalog.

### 5.1 E-commerce

- Product data model (variants, SKUs, options, bundles)
- Inventory sync, low-stock alerts, oversell prevention
- Pricing (regional, promo, sale, B2B tiers)
- Taxes (Stripe Tax / Avalara, registrations per jurisdiction)
- Shipping (carriers, rate calc, zones, free-shipping thresholds)
- Returns / refunds workflow
- Cart and checkout (guest, account, abandoned cart recovery)
- Order email / SMS lifecycle
- Customer accounts (order history, addresses, payment methods)
- Reviews and ratings
- Fraud prevention (Radar, etc.)
- PCI scope and compliance (minimize via tokenization / hosted fields)
- Wishlist / save-for-later
- Gift cards / store credit
- Subscription / recurring (if applicable)
- Marketplace seller onboarding (if applicable)
- Cross-border / customs
- Currency conversion display vs charge

### 5.2 Documentation

- Site search (with weighting, scope)
- Versioning (per-product-version docs)
- API reference (auto-generated from OpenAPI / introspection)
- Code samples (multi-language tabs)
- Copy-to-clipboard everywhere
- Edit-on-GitHub links
- Breadcrumbs and prev/next
- Glossary and cross-links
- Callouts / admonitions
- TOC / on-page nav
- Search analytics (what users couldn't find)
- Doc contribution workflow (for OSS)
- Translations and translation drift handling
- Stale content detection

### 5.3 Blog / news / magazine

- Author profiles
- Category / tag taxonomy
- RSS / Atom feeds
- Newsletter integration
- Comments (yes/no, provider, moderation)
- Related posts / recommendations
- Reading time estimate
- Social share metadata
- Archive pages (by date, tag, author)
- Editorial calendar / scheduled publishing
- Embargo / draft preview links

### 5.4 Marketing / landing

- Hero CTA hierarchy
- Lead capture (form provider, CRM sync)
- A/B test infrastructure
- Conversion pixel setup (Meta, LinkedIn, Google Ads, TikTok)
- UTM handling and attribution
- Social previews (OG, Twitter)
- GDPR / CCPA banner with consent management
- Page-speed budget tight (LCP < 2.5s)
- Multi-variant for paid channels

### 5.5 SaaS product (marketing + app)

- Sign-up flow and email verification
- Onboarding (empty state → activated)
- Billing (Stripe Billing, Paddle, Lemon Squeezy)
- Plan management, upgrades / downgrades, proration
- Team / workspace concepts
- RBAC and permissions
- Audit log
- Customer support (in-app, Intercom, email)
- Admin console
- Status page (statuspage.io, openstatus)
- Webhooks for customers
- API for customers (rate limits, keys)
- Customer data export / import
- Account deletion / GDPR

### 5.6 Community / forum

- Account / identity model
- Posting / commenting / threading
- Moderation tools (flag, hide, ban, shadowban)
- Spam prevention
- Reputation / karma / badges
- Search and discovery
- Notifications (in-app, email, push)
- Real-time updates (where worth it)
- Reporting flow
- Community guidelines and enforcement

### 5.7 Educational / LMS

- Course / module / lesson model
- Progress tracking
- Quizzes and assessments
- Certificates / badges
- Discussion / cohorts
- Video hosting and playback
- Captions / transcripts (accessibility, also SEO)
- Drip / scheduled content release
- Instructor tools
- Payments / subscriptions / institutional licensing

### 5.8 Portfolio / personal

- Project case studies (structure, depth)
- Imagery weight (often heavy — performance matters more, not less)
- Contact form / email reveal
- CV / resume download
- Social and external profile links
- Now / uses / changelog patterns (if relevant)

### 5.9 Nonprofit / cause

- Donation flow (one-time, recurring, gift)
- Donor recognition
- Tax receipts
- Impact reporting
- Event RSVPs
- Volunteer signups
- Newsletter / advocacy

### 5.10 Event / conference

- Schedule / agenda
- Speaker profiles
- Sponsor tiers and placements
- Ticket sales (free, paid, waitlist)
- Personal schedule builder
- Live streaming / on-demand
- Sponsor lead capture
- Map / venue info
- Post-event content (recordings, slides)

### 5.11 Restaurant / hospitality

- Menu (allergen flags, dietary tags)
- Reservation system (OpenTable, Resy, Tock, custom)
- Ordering / takeout / delivery
- Hours and holiday hours
- Location(s) and directions
- Gift cards
- Loyalty programs

### 5.12 Real estate

- Listing model (photos, floor plans, virtual tours)
- Map / geo search
- Saved searches and alerts
- Agent profiles
- Inquiry / showing requests
- MLS / IDX integration
- Mortgage calculators

### 5.13 Government / public service

- Strict accessibility (often AA mandatory, sometimes AAA for parts)
- Plain-language requirements
- Multilingual obligations
- Privacy and data-minimization
- Form accessibility (often the highest-value pages)
- Open data / transparency
- Procurement constraints on tooling

### 5.14 Internal tool / dashboard

- SSO
- Role-based access
- Audit logging
- High data density UI
- Tabular UX (sorting, filtering, pagination, bulk actions)
- Export (CSV, Excel)
- Saved views
- Permissions UX
- Time zone handling
- Internal-only deployment / VPN

### 5.15 Personal site

- Posting cadence and friction
- RSS
- Linking out to other places (Mastodon, GitHub, etc.)
- Microformats (h-card, h-entry)
- Indieweb patterns (Webmentions, syndication) — if relevant

---

## 6. Question script (every site)

1. *"What regulations apply to you? GDPR? CCPA? HIPAA? COPPA? Accessibility laws? Industry-specific?"*
2. *"Where are your users and where does data have to live?"*
3. *"What's the launch date and what's the day-7 plan?"*
4. *"Who's on call when something breaks?"*
5. *"Who maintains this in six months?"*
6. *"What's the cost ceiling for hosting?"*
7. *"What's the worst thing that could happen if this site went down for an hour? A day?"*
8. *"What's your testing and QA process before something hits production?"*
9. *"How will you measure if it's working — at week one, month one, year one?"*
10. *"What considerations should I be raising that I haven't? You know your business better than I do."*

That last question is mandatory.

---

## 7. Output artifact: `spec/considerations.md`

````markdown
# Considerations

## Archetype-driven scope
Pulled from spec/purpose.md:
- Primary archetype: <archetype>
- Secondary archetypes: <...>
- Archetype-specific checklists walked: <list of section ids>

## Cross-cutting baseline

### Accessibility
- Target level:
- Regional laws:
- Manual testing plan:
- Automated checks:
- Statement page:

### SEO
- Meta strategy:
- OG / cards:
- Sitemap:
- robots.txt:
- Structured data schemas:
- URL strategy:
- Redirects:
- Hreflang:

### Performance
- Budgets (LCP / INP / CLS / TTFB / bundle):
- Lighthouse CI threshold:
- Image strategy:
- Font loading:
- Third-party script policy:
- Caching:

### Privacy and legal
- Privacy policy plan:
- Terms:
- Cookie policy and banner:
- Consent management:
- DSAR process:
- Data retention:
- Age gating:

### Security
- HTTPS / HSTS:
- CSP and other headers:
- CSRF / XSS:
- Rate limiting / bots:
- Secrets:
- Dependency scanning:
- Incident response:

### Content and assets
- Source of truth:
- Owners:
- Asset licensing:
- Optimization pipeline:
- Versioning:
- Launch content ready: <yes/no>

### Analytics and monitoring
- Event taxonomy:
- Funnels:
- RUM:
- Error tracking sampling / PII:
- Logs retention:
- Alerting:
- On-call:

### Deployment and hosting
- Environments:
- Domains:
- DNS provider:
- Certs:
- Region(s) and failover:
- Backups:
- DR (RPO / RTO):
- Cost monitoring:

### CI/CD
- Branching:
- Required checks:
- Preview deployments:
- Production deploy trigger:
- Smoke tests:
- Rollback:
- Feature flags:

### Testing
- Coverage expectations:
- Visual regression:
- A11y in CI:
- Perf in CI:
- Manual QA:
- Staging review:

### Internationalization
- Source of truth:
- Translation workflow:
- RTL:
- Locale-specific assets:
- Regional legal:

### Maintenance and handoff
- Maintainer:
- Documentation deliverables:
- Dependency cadence:
- Browser support:
- Sunset criteria:

### Launch readiness
- Cut-over plan:
- Old-site redirects:
- Day-0 monitoring:
- Day-7 retro:

## Archetype-specific decisions

### <Archetype 1>
(every applicable item from section 5 of phases/5-considerations.md, plus catalogs/considerations-catalog.md, with a decision)

### <Archetype 2>
(if hybrid)

## User-surfaced considerations
Items the user raised that weren't in the catalog:
- <item>: <decision>

## Explicitly out of scope
- <item>: <why>

## Open questions
````

---

## 8. Exit criteria (phase gate)

Follow the [Phase gate protocol in AGENTS.md §4](../AGENTS.md#4-phase-gate-protocol). The phase exits when every box below is checked AND the user explicitly approves.

### 8.1 Inherited constraints

This is the convergence phase. Enumerate to the user:
- From `spec/intake.md`: compliance regimes, operational appetite, hosting regions.
- From `spec/purpose.md`: archetype (drives which catalog sections are walked), regulations, audience.
- From `spec/template.md`: pages requiring auth, content authoring source, real-time regions.
- From `spec/tooling.md`: hosting + region + data residency, CMS, auth, payments, monitoring choices.
- From `spec/style.md`: motion personality, theme modes, font sourcing privacy.

### 8.2 Artifact completeness
- [ ] Every section of the cross-cutting baseline (§4 of this doc) has a decision recorded (even if "N/A — [reason]").
- [ ] Every archetype identified in `spec/purpose.md` has had its catalog section walked (§5 + `catalogs/considerations-catalog.md`).
- [ ] Accessibility target named with regional law mapping.
- [ ] Privacy / consent / legal items addressed per the regulations declared in Purpose.
- [ ] Security baseline (HTTPS, headers, dependency scanning, secrets, incident response) addressed.
- [ ] Performance budgets named (LCP / INP / CLS / TTFB / bundle / page weight).
- [ ] Deployment plan: environments, domains, DNS, certs, regions, backups, DR (RPO/RTO).
- [ ] CI/CD: branching, required checks, preview deployments, deploy trigger, rollback.
- [ ] Testing strategy across all layers.
- [ ] Internationalization workflow if any language beyond English.
- [ ] Maintenance & handoff: maintainer named, docs deliverables enumerated, dependency cadence, browser support.
- [ ] Launch readiness checklist (cut-over, redirects, day-0 monitoring, day-7 retro).
- [ ] Out-of-scope items are listed explicitly so they aren't assumed.
- [ ] All open questions from earlier phases are closed or explicitly carried into post-launch backlog.

### 8.3 Catch-all (mandatory)
- [ ] Asked verbatim: *"Is there anything I haven't asked about that you think matters for this site?"*
- [ ] Anything new from the user has been recorded under "User-surfaced considerations" in the artifact.

### 8.4 Downstream — the aggregate spec

After approval of `spec/considerations.md`:
- Assemble `spec/SPEC.md` per [AGENTS.md §6](../AGENTS.md#6-the-unified-spec--specspecmd).
- Present `spec/SPEC.md` to the user as the **final contract**.
- Get **dated, named approval**. Record in the artifact.
- Only then proceed to implementation (see [`verification.md`](../verification.md) at the repo root for the build/handoff checklist).

### 8.5 Approval

User must reply with "approved", "next", or equivalent.

### 8.6 If you need to revise this phase later

A revision here may surface unmet requirements that require looping back to **any** earlier phase. Follow the revision protocol in [AGENTS.md §4.4](../AGENTS.md#44-revision-protocol-when-a-later-phase-changes-an-earlier-one). After updating an earlier artifact, re-validate every downstream artifact in order before resuming here.

---

## 9. After phase 5

Assemble a **unified spec summary** combining purpose, template, tooling, style, and considerations. Present it to the user. Get explicit signoff. Only then begin implementation.

---

## 10. Common traps

- **Treating phase 5 as a formality.** Most preventable launch incidents come from skipped considerations.
- **Skipping the "anything else?" question.** The catalog is not exhaustive. The user knows their business.
- **Assuming regulations don't apply.** Ask explicitly; do not infer-by-omission.
- **Deferring DR / backups / monitoring.** Decide before launch.
- **No handoff plan.** Even agent-built sites need a human to operate them eventually.
- **Confusing "out of scope" with "we forgot."** Out-of-scope items must be explicit decisions.
