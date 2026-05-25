# Considerations Catalog

Comprehensive master list of cross-cutting concerns, organized by topic and cross-referenced by archetype. Use during phase 5. The phase doc gives a top-of-mind summary; this catalog is the deeper reference.

> **Non-exhaustive.** Every site has concerns no catalog can predict. After walking the relevant sections, ask the user: *"Is there anything I haven't asked about that you think matters?"*

---

## How to use

1. Open this file alongside `spec/purpose.md`.
2. Walk **every** section under "Cross-cutting (every site)".
3. From `spec/purpose.md`, pick the archetype(s) and walk the matching archetype sections.
4. Record a decision for each item in `spec/considerations.md` — including explicit "N/A" or "out of scope."
5. Ask the final open-ended question.

---

## Cross-cutting (every site)

### Accessibility

- WCAG target level (A / AA / AAA), with rationale.
- Applicable laws by region (ADA, AODA, EAA, Section 508, EU Web Accessibility Directive, etc.).
- Conformance evidence retention (ACR / VPAT, audit reports).
- Manual testing plan (screen readers: NVDA, JAWS, VoiceOver, TalkBack; keyboard-only; voice control; high contrast; zoom 200%).
- Automated checks in CI (axe-core, Pa11y, Lighthouse CI).
- Accessibility statement page.
- Reporting channel for accessibility issues.
- Procurement / vendor accessibility (all embedded widgets must conform).
- Content authoring accessibility training (alt text, headings, link text).

### SEO and discoverability

- Per-page meta title and description; title template (e.g., `${page} | ${site}`).
- Canonical URLs.
- Open Graph (og:title, og:description, og:image, og:type).
- Twitter Cards (summary, summary_large_image).
- `sitemap.xml` (with last-modified dates, generation strategy).
- `robots.txt` (allow/disallow, sitemap reference).
- Indexability per environment (block staging; meta noindex on auth-gated pages).
- Structured data (JSON-LD): Article, NewsArticle, Product, BreadcrumbList, FAQ, HowTo, Event, Organization, LocalBusiness, Recipe, VideoObject, JobPosting, Course, etc.
- URL structure: short, lowercase, hyphen-separated, no trailing slashes (or all trailing slashes — be consistent).
- Hreflang for multi-language.
- Internationalization SEO (subdomain, subdirectory, ccTLD).
- Redirect strategy (301s for permanent moves; 302s for temporary).
- Old-URL preservation when migrating.
- Search Console / Bing Webmaster setup.
- Image alt text policy.
- Internal linking strategy.
- Pagination SEO (rel=prev/next deprecated but still common, or canonical to first page).
- Core Web Vitals (LCP, INP, CLS).

### Performance

- Performance budgets:
  - LCP < 2.5s good, < 4s acceptable
  - INP < 200ms good
  - CLS < 0.1 good
  - TTFB < 0.8s good
  - JS bundle size (initial)
  - Image weight per page
  - Total page weight
- Lighthouse CI thresholds and per-PR enforcement.
- Image strategy:
  - Modern formats (AVIF, WebP) with fallback.
  - Responsive sources (`srcset`, `sizes`).
  - Lazy loading (`loading="lazy"`).
  - Explicit width/height to prevent CLS.
  - CDN with on-the-fly transforms.
- Font strategy: preload, self-host, subset, size-adjust, font-display.
- Critical CSS: inline above-the-fold styles, defer rest.
- Third-party scripts: defer, async, consent-gate, isolate.
- Caching:
  - CDN (TTL by content type).
  - Browser (Cache-Control, immutable for hashed assets).
  - Service worker / offline.
- Compression: Brotli + Gzip fallback.
- Preconnect / dns-prefetch / preload for critical origins.
- Reduce JavaScript: islands, partial hydration, no-JS where possible.
- Database query budgets (no N+1).
- API response cache headers.

### Privacy, consent, legal

- Privacy policy: jurisdictions, who drafts, when, in what languages.
- Terms of service / user agreement.
- Cookie policy with categorized cookies (essential, analytics, marketing).
- Cookie consent banner:
  - GDPR (EU) — reject-all must be one click; consent must be granular and revocable.
  - ePrivacy directive.
  - CCPA / CPRA (California) — "Do Not Sell or Share" link.
  - LGPD (Brazil), PIPL (China), POPIA (South Africa), etc.
- Tracker categorization and pre-consent blocking.
- Data Subject Access Requests (DSAR) process.
- Right to deletion / right to be forgotten workflow.
- Data retention policy and automated purges.
- Sub-processor list maintenance (if SaaS).
- DPA / data processing agreements with vendors.
- Age gating (COPPA in the US: under 13; UK/EU thresholds vary).
- Sensitive data handling (PII, PHI, financial, biometric, location).
- Cross-border data transfer (Schrems II, SCCs, adequacy decisions).
- Privacy by Design / Privacy by Default documentation.
- DMCA agent registration (if hosting UGC in the US).
- Accessibility statement (also legal in many jurisdictions).
- Terms updates: change notification, version history.

### Security

- TLS / HTTPS enforced site-wide; HSTS with preload submission.
- Security headers:
  - Content-Security-Policy
  - X-Content-Type-Options: nosniff
  - X-Frame-Options or CSP frame-ancestors
  - Referrer-Policy
  - Permissions-Policy
  - Cross-Origin-* headers
- Authentication hardening: rate limiting, account lockout, MFA, password policy, passkey support.
- Session management: rotation, idle timeout, secure flags.
- Authorization: principle of least privilege, RBAC/ABAC enforcement at server (never trust client).
- Input validation and output encoding.
- CSRF protection (SameSite cookies, anti-CSRF tokens).
- XSS prevention (auto-escaping frameworks, CSP, sanitization for user content).
- SQL injection prevention (parameterized queries, ORMs).
- File upload: type checks, size limits, AV scanning, isolated storage.
- Webhook validation (signatures, replay protection).
- Secret management: rotation, scoping, no client-side leakage, scanning in CI.
- Dependency scanning + patch policy (Dependabot, Renovate, Snyk).
- SAST in CI.
- Container image scanning (if containerized).
- WAF and bot mitigation (Cloudflare, AWS WAF).
- Rate limiting / abuse protection per endpoint.
- DDoS mitigation.
- Penetration testing cadence (annual, semi-annual).
- Bug bounty / vulnerability disclosure policy and SLAs.
- Incident response plan and tabletop exercises.
- Breach notification process per regulation.

### Content and assets

- Source of truth (CMS, repo, DB, user-generated).
- Content owners and approval workflow.
- Editorial calendar / publishing cadence.
- Asset licensing (stock, commissioned, AI-generated, user-supplied).
- Image optimization pipeline (manual, build-time, on-demand CDN).
- Video sourcing and hosting cost.
- Default placeholder content for empty states.
- Launch content readiness checklist (real content, not Lorem Ipsum).
- Stale content detection and refresh cadence.
- Versioning of content (especially docs).
- Translation source of truth and update propagation.

### Analytics, monitoring, observability

- Event taxonomy (what user actions are tracked, naming conventions, properties).
- Funnels and conversion goals (defined before launch).
- Cohort analysis and retention dashboards.
- Real user monitoring (RUM) for performance.
- Error tracking: sampling rate, PII scrubbing, source map upload.
- Logs: structured, retention period, PII handling.
- Distributed tracing for backend.
- Synthetic monitoring for critical user journeys.
- Uptime monitoring and public status page.
- Alerting:
  - Severity tiers and routing.
  - Pager on-call rotation.
  - Runbooks linked from alerts.
- Dashboards for product, ops, business owners.
- SLO / SLI definitions.

### Deployment and hosting

- Environments: local, dev, staging, prod (sometimes preview-per-PR).
- Domain strategy: apex vs www, subdomain conventions.
- DNS provider, TTL strategy, DNSSEC.
- Certificate auto-renewal.
- Region(s): primary and failover.
- Data residency requirements (GDPR, financial regs, government).
- Edge vs origin architecture.
- Backup strategy: frequency, retention, restore testing.
- Disaster recovery: RPO (acceptable data loss) and RTO (acceptable downtime).
- Cost monitoring and alerts.
- Capacity planning.
- Traffic projections and load testing.

### CI/CD

- Branching model (trunk-based, GitFlow, GitHub Flow).
- Required checks (lint, type, unit, integration, e2e, accessibility, performance, security scans).
- Preview deployments per PR.
- Code review policy (required reviewers, CODEOWNERS).
- Production deploy trigger: automated on main, manual gate, scheduled window.
- Deployment strategy: immediate, blue/green, canary, feature flag.
- Smoke tests post-deploy.
- Rollback procedure (single-button, time-to-rollback).
- Database migration strategy (forward-only? Reversible? Maintenance windows?).
- Feature flag governance.
- Release notes / changelog generation.

### Testing strategy

- Unit test coverage targets (don't fetishize, but have one).
- Integration test boundaries.
- End-to-end test critical paths (sign-up, checkout, search, etc.).
- Visual regression coverage (per-component or full-page).
- Accessibility tests in CI.
- Performance tests in CI (Lighthouse, custom).
- Load tests pre-launch.
- Security scans in CI.
- Manual QA checklist before each release.
- Staging review process and sign-off.
- Beta program / dogfooding.

### Internationalization (if applicable)

- Languages at launch and roadmap.
- Translation source of truth (CMS, code, JSON files).
- Translation provider (in-house, Crowdin, Lokalise, Phrase, machine, mixed).
- Translation review workflow.
- RTL support (CSS logical properties; mirror layouts).
- Locale-specific assets (images with text, dates, currencies).
- Number, currency, date, unit formatting.
- Regional legal text variants.
- URL strategy for locales.
- Default locale and language negotiation.
- Right-to-left typography selection.

### Maintenance and handoff

- Maintainer(s) post-launch.
- Documentation deliverables:
  - README (setup, build, deploy).
  - Architecture diagram.
  - Runbook (common ops, incident response).
  - Theming and styling guide.
  - Content authoring guide.
  - Onboarding doc for new contributors.
  - API documentation (internal and external).
- Dependency update cadence (weekly / monthly / quarterly).
- Browser support matrix and re-evaluation schedule.
- Mobile OS support matrix.
- Sunset criteria for features.
- Knowledge transfer plan.

### Launch readiness

- DNS cut-over plan and time window.
- Redirects from old URLs.
- Monitoring active before launch.
- Analytics verified.
- Real content in place (no Lorem Ipsum, no broken images).
- All forms tested end-to-end.
- All payment flows tested in staging and production.
- Status page live.
- Support channels open.
- Press / comms scheduled.
- Soft launch with traffic ramping (1%, 10%, 50%, 100%).
- Day-0 monitoring rotation.
- Day-7 retrospective.

### Sustainability / ethics

- Page weight (smaller = less energy).
- Hosting region powered by renewables.
- Carbon estimate (Website Carbon Calculator, Beacon, sustainable web design checklist).
- AI-generated content disclosure.
- Dark pattern avoidance (no opt-out traps, no deceptive UI).
- Inclusive language audit.
- Accessibility (also an ethics issue).

---

## Archetype-specific deep dives

### E-commerce

#### Catalog and inventory
- Product data model: simple, configurable (variants), bundled, digital, subscription.
- Variant model: options × values × SKUs.
- Inventory: real-time sync, oversell prevention, low-stock alerts, backorder allowed?
- Bundles, kits, components.
- Digital products: licenses, downloads, access expiration.
- Subscription products: cancellation, pause, swap, proration.

#### Pricing
- Per-region pricing.
- Sale / promo pricing.
- Compare-at price display.
- B2B / customer-specific pricing.
- Bulk / tiered pricing.
- Tax-inclusive vs tax-exclusive display.

#### Cart and checkout
- Guest checkout vs forced account.
- Saved cart (persisted, expiration).
- Abandoned cart email/SMS.
- Single-page vs multi-step checkout.
- Address autocomplete (Google Places, Loqate).
- Shipping rate calculation in cart.
- Promo / discount input.
- Gift options.
- Subscribe in cart.
- Express checkouts (Apple Pay, Google Pay, Shop Pay, PayPal Express).

#### Payments
- Card processor (Stripe, Adyen, Braintree, Square, etc.).
- Wallets (Apple Pay, Google Pay, Amazon Pay, Shop Pay).
- BNPL (Klarna, Affirm, Afterpay).
- Local methods (iDEAL, SEPA, Sofort, Alipay, WeChat Pay, UPI).
- Currency conversion: display vs charge.
- 3DS2 (Strong Customer Authentication in EU).
- Failed-payment recovery.

#### Tax and compliance
- Sales tax (US — Stripe Tax, Avalara, TaxJar).
- VAT (EU — OSS, IOSS).
- GST (India, Australia, NZ, Canada).
- Marketplace facilitator laws.
- Nexus tracking.
- Invoicing requirements.

#### Shipping
- Carriers and integrations.
- Real-time rates vs flat rates.
- Free shipping thresholds.
- Local pickup / curbside.
- International shipping and customs.
- Address validation.
- Tracking page (branded).
- Returns label generation.

#### Orders, fulfillment, returns
- Order email lifecycle (placed, paid, fulfilled, shipped, delivered, returned).
- Order modification (edit, cancel, partial refund).
- Returns / RMA workflow.
- Restock policy.
- Refund timing.

#### Trust
- Reviews and ratings (Trustpilot, Yotpo, Okendo, Judge.me).
- Photo / video reviews.
- Verified purchase badges.
- Q&A on product pages.
- Trust badges (security, BBB, etc.).

#### Loyalty and retention
- Account creation incentives.
- Wishlist.
- Order history and reorder.
- Loyalty program (Smile.io, LoyaltyLion, Yotpo).
- Referral program.
- Win-back emails.

#### Compliance specific to commerce
- PCI scope (minimize by using hosted fields / tokenization).
- Consumer protection laws (right of withdrawal in EU: 14 days).
- Distance selling regulations.
- Restricted product compliance (alcohol, CBD, tobacco, supplements, firearms — region-dependent).

### Marketplace (in addition to e-commerce)

- Seller / vendor onboarding flow.
- KYC / KYB verification.
- Seller dashboard.
- Payouts (Stripe Connect, Wise, PayPal).
- Marketplace facilitator tax obligations.
- Dispute resolution and mediation.
- Seller reputation and review.
- Listing moderation.
- Buyer protection program.
- Anti-fraud (collusive listings, fake reviews).
- Counterfeit / IP infringement reporting.
- Two-sided incentives at cold-start.

### Documentation

#### Information architecture
- Concept / guide / reference / how-to split (Diátaxis framework).
- Versioning strategy (latest, vN.N, archived).
- Search coverage and weighting.
- Sidebar grouping.
- Per-page TOC.

#### Code samples
- Multi-language tabs.
- Copy-to-clipboard everywhere.
- Runnable examples (CodeSandbox, StackBlitz embeds).
- Sample accuracy (test in CI).

#### API reference
- Auto-generation source (OpenAPI, GraphQL introspection, JSDoc / TypeDoc, Sphinx, etc.).
- Try-it-out interactive.
- SDK code samples.
- Versioned alongside docs.

#### Discoverability
- Search analytics (what users searched and didn't find).
- Stale content detection (commits older than X).
- Broken link checker in CI.

#### Contribution
- Edit-on-GitHub.
- CLA / DCO for community contributions.
- Style guide for writers.
- Translation contribution workflow.

### Blog / news

- Author profiles, bios, social.
- Bylines and co-authorship.
- Tag / category taxonomy.
- Editorial calendar.
- Embargo / draft preview links.
- Scheduled publishing.
- Newsletter integration (RSS-to-email).
- Comments (provider, moderation, anti-spam).
- Related posts (manual or algorithmic).
- Estimated reading time.
- Save-for-later.
- Paywall / metered access (if monetized).
- Syndication (POSSE, cross-posting).
- AMP (mostly deprecated; avoid unless required).
- Newsletter archive.

### Marketing / landing

- Hero CTA hierarchy: one primary, one secondary at most.
- Below-the-fold sections: features, social proof, comparison, FAQ, final CTA.
- Lead capture form: minimal fields, progressive profiling.
- CRM sync (HubSpot, Salesforce, Pipedrive, custom).
- A/B testing (PostHog, GrowthBook, Optimizely, VWO, Statsig).
- Conversion pixels (Meta, LinkedIn, Google Ads, TikTok, Reddit).
- UTM handling and attribution.
- Multi-touch attribution model.
- Landing-page variants per campaign / channel.
- Heatmaps and session recording (Hotjar, FullStory) — with consent.
- Pre-launch waitlist.
- Social previews (OG, Twitter cards).
- Customer logos (with permission).
- Case studies and testimonials (real names + faces if possible).
- Pricing page (transparent vs request-a-demo).

### SaaS product

#### Sign-up and onboarding
- Account creation friction.
- Email verification (now vs later).
- Onboarding checklist / empty states.
- Personalized onboarding by role / use case.
- Demo data / sample workspace.
- Time-to-first-value tracking.

#### Billing
- Plan model: flat, per-seat, usage, hybrid.
- Free trial vs freemium.
- Card-on-file vs no-card trial.
- Proration on upgrades.
- Credit / refund policy.
- Failed payment recovery (Stripe Smart Retries).
- Tax: Stripe Tax, Paddle, Lemon Squeezy (merchant of record).
- Invoicing (B2B).
- Procurement integrations (Vendr, Coupa).

#### Workspace and roles
- Team / workspace model.
- Roles and permissions.
- Invitations and SCIM provisioning.
- SSO (SAML, OIDC).
- Audit log.
- Granular permissions UI.

#### Product-led growth
- In-app upgrade prompts.
- Usage limits and overage.
- Reverse-trial.
- Referral program.

#### Customer support
- In-app chat (Intercom, Crisp).
- Help center / docs.
- Ticket system.
- Status page (subscribe to incidents).

#### Developer-facing
- Public API (auth, rate limits, versioning).
- Webhooks.
- SDKs.
- Developer portal.
- API keys management UI.

#### Account management
- Account deletion and data export.
- Workspace deletion.
- Data portability.
- GDPR / DPA agreements.
- BAA agreements (HIPAA).

### Community / forum

- Identity model: real name, handle, anonymous.
- Sign-up friction vs spam tradeoff.
- Posting / commenting permissions by role.
- Threading model.
- Reactions vs votes vs both.
- Search.
- Notifications (in-app, email, push).
- Email digests.
- Real-time updates (sparingly).
- Moderation:
  - Auto-mod (Akismet, Perspective API, OpenAI moderation).
  - Reports and flags.
  - Moderation queue.
  - Bans (shadow, temp, permanent).
  - Appeals process.
- Reputation: karma, badges, levels.
- Code of conduct enforcement.
- Trust & safety policies.
- Direct messages (DMs) — abuse vector, design carefully.
- Content licensing of UGC.
- Account deletion = content handling (orphan, delete, anonymize).

### Educational / LMS

- Course / module / lesson model.
- Progress tracking and resume.
- Quizzes: question types, randomization, retries.
- Assignments and grading.
- Certificates: design, verification URL, blockchain (rare).
- Discussion forums per course.
- Cohort vs self-paced.
- Drip release.
- Video hosting (Mux, Cloudflare Stream, Wistia, Vimeo).
- Captions (mandatory accessibility — and SEO).
- Transcripts.
- Notes/highlights.
- Mobile playback and offline.
- Payments: one-time, subscription, B2B/institutional licensing.
- Coupons and scholarships.
- Affiliate program.
- Refund policy.
- Plagiarism detection (for assignments).
- Proctoring (for exams).

### Nonprofit / cause

- Donation flow: one-time, recurring, employer match.
- Donation tiers and impact framing.
- Gift in honor / memory.
- Tax-deductibility messaging.
- Tax receipts (automated email + PDF).
- Donor recognition or anonymity.
- Impact reporting.
- Event RSVPs, ticketing.
- Volunteer signups.
- Petition / advocacy actions.
- Email list (legal compliance: CAN-SPAM, GDPR consent).
- CRM (Salesforce Nonprofit Cloud, Bloomerang, Kindful, Virtuous).
- Major donor portal.
- Financials and transparency page.
- Charity registration / state filings (US: separate per state).

### Event / conference

- Schedule: tracks, sessions, breaks, time zones.
- Personal schedule builder.
- Speaker bios and headshots.
- Sponsor tiers, logos, content.
- Ticket types: early bird, regular, student, virtual, day pass.
- Refund and transfer policy.
- Code of conduct prominent.
- Captioning for live and on-demand.
- ASL interpretation (in-person).
- Diet/accessibility info at registration.
- Networking app (Brella, Whova).
- Live streaming.
- Replay and on-demand.
- Post-event survey.
- Sponsor lead capture.

### Restaurant / hospitality

- Menu freshness (highest priority).
- Allergen and dietary tags.
- Calorie display (where mandated).
- Reservations integration.
- Online ordering (in-house vs DoorDash/Uber Eats).
- Hours including holidays.
- Multiple locations.
- Map and directions.
- Photos that look like the actual food.
- Gift cards.
- Loyalty.
- Catering inquiries.
- Press / awards page.

### Real estate

- MLS / IDX integration and listing freshness.
- Listing model: photos, floor plans, video, 360, drone.
- Filters: price, bedrooms, baths, sqft, lot, year, features.
- Map with clustering and shape-draw.
- Saved searches and email alerts.
- Agent attribution.
- Inquiry/showing requests.
- Mortgage calculator.
- Neighborhood info (schools, transit, walk score).
- Fair Housing compliance.
- Photo licensing.

### Government / public service

- Strict accessibility (often AA mandatory, sometimes AAA on key flows).
- Plain language (grade-level targets, e.g., grade 7).
- Multilingual obligations.
- Privacy: minimal data collection, retention limits.
- No third-party trackers without consent.
- Service-finder UX.
- Step-by-step task UX.
- Save-and-resume forms.
- Trust signals (gov domain, padlock, agency branding).
- Procurement constraints on tools.
- Open data and transparency reporting.

### Internal tool / dashboard

- SSO mandatory.
- RBAC enforced server-side.
- Audit log for every mutating action.
- High-density tables.
- Keyboard shortcuts.
- Command palette.
- Saved views and shared views.
- Export (CSV, Excel, JSON).
- Time zone display (user, server, UTC).
- Date/number formatting for the user's locale.
- Bulk actions with confirmation.
- Undo where feasible.
- Permission UX (clear when a user can't do something and why).
- Search across all entities.
- Internal documentation links from the UI.

### Personal site

- Posting friction (low = more posts).
- Permalinks that never break.
- RSS.
- Web mentions (if indieweb).
- Microformats (h-card, h-entry).
- Comments (off, third-party, indieweb).
- Newsletter (Buttondown, Beehiiv).
- Search.
- Archive page.

---

## Region-specific regulatory snapshots

(Not legal advice. Surface relevant ones to the user; recommend qualified counsel.)

- **GDPR (EU/EEA, UK):** lawful basis, consent, data subject rights, sub-processors, DPA, DPO requirement thresholds, cross-border transfers.
- **CCPA / CPRA (California):** Do Not Sell / Share, opt-out, consumer rights.
- **HIPAA (US health):** BAAs, PHI handling, breach reporting.
- **PCI DSS (payment cards):** scope reduction via tokenization.
- **COPPA (US, under 13):** parental consent.
- **GLBA (US financial):** privacy notices.
- **FERPA (US education):** student records.
- **SOX / SOC 2 / ISO 27001:** for enterprise SaaS buyers.
- **LGPD (Brazil):** GDPR-similar.
- **PIPL (China):** strict data localization.
- **POPIA (South Africa):** privacy.
- **PIPEDA (Canada):** privacy.
- **PDPA (Singapore):** privacy.
- **APP (Australia):** privacy.
- **EAA (EU Accessibility Act):** accessibility for businesses serving EU consumers, effective June 2025.
- **ADA (US):** accessibility, increasingly enforced for websites.
- **DSA (EU Digital Services Act):** content moderation, transparency.
- **DMA (EU Digital Markets Act):** for gatekeepers.

---

## Final reminder

This catalog cannot anticipate every concern for every site. Always close phase 5 with:

> *"Is there anything I haven't asked about that you think matters for this site?"*

The user knows their business better than the framework does.
