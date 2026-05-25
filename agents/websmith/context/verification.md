# Verification & Handoff Checklist

> *Use after `spec/SPEC.md` is signed off. This file is the bridge between **spec** (what the framework produces) and **build** (which the framework does not.)*

This is not a build pipeline. It is the checklist whoever implements the site — a human engineer, an agency, or another agent — should run **before** declaring the site done. Use it in three ways:

1. **As a definition of done.** Implementation isn't complete until every applicable item is checked.
2. **As a launch gate.** Don't cut DNS until the launch-readiness section passes.
3. **As a hand-back signal.** When you (the framework agent) hand off to a build agent or human, you point them here.

> Items marked **(SPEC-bound)** trace directly to a decision in `spec/SPEC.md`. Items marked **(universal)** apply regardless of what the spec says.

---

## 1. Scaffold complete (SPEC-bound)

- [ ] Working directory matches what's in `spec/intake.md` and is **not** inside this framework repo.
- [ ] Repository initialized with the version control of choice.
- [ ] Framework matches `spec/tooling.md` exactly (versions pinned).
- [ ] Package manager and language versions pinned (`.nvmrc`, `.python-version`, `Gemfile`'s ruby, etc.).
- [ ] Styling system installed and configured per `spec/style.md`.
- [ ] CSS / design tokens from `spec/style.md` exist in code (CSS variables, Tailwind config, or the chosen system).
- [ ] Database (if any) provisioned per `spec/tooling.md`; initial schema migration exists.
- [ ] Auth provider (if any) connected per `spec/tooling.md`; sign-up + sign-in roundtrip works locally.
- [ ] Payment provider (if any) connected per `spec/tooling.md`; test transaction succeeds.
- [ ] Email transactional sending verified in dev.
- [ ] All required environment variables documented in `.env.example`.
- [ ] Local development quickstart in `README.md` of the site repo works from a clean clone.

## 2. Components built (SPEC-bound)

Cross-reference every component named in `spec/template.md`:

- [ ] Every component in the template's component list exists in code.
- [ ] Components honor the design tokens from `spec/style.md` (no hard-coded colors, sizes, spacing).
- [ ] Components have at least one state diagram (default / hover / focus / active / disabled / loading / error) per the spec.
- [ ] Component variants from spec are implemented and named consistently.
- [ ] Components used in multiple places are genuinely reused (no duplication of structure).
- [ ] Each form component has: label, helper text, error message, error association via `aria-describedby`, validation.

## 3. Pages built (SPEC-bound)

For each page or template in `spec/template.md`:

- [ ] URL pattern matches the sitemap.
- [ ] Major regions match the spec.
- [ ] Required components are present.
- [ ] Empty state is implemented (where applicable).
- [ ] Loading state is implemented (where applicable).
- [ ] Error state is implemented.
- [ ] 404 page exists and matches site style.
- [ ] 500 / generic-error page exists.
- [ ] Auth-gated pages enforce auth at the server (not just by hiding the link).
- [ ] Plan-gated pages enforce plan checks at the server.

## 4. Information architecture (SPEC-bound)

- [ ] Sitemap from `spec/template.md` is fully implemented.
- [ ] Navigation works on every page (desktop, tablet, mobile).
- [ ] Breadcrumbs (if specified) match the actual hierarchy.
- [ ] Search (if specified) returns relevant results for at least 5 sanity-check queries.
- [ ] Internal links between related content are present and bidirectional where appropriate.
- [ ] Sitemap.xml exists and includes every public URL pattern.
- [ ] robots.txt exists and matches the indexability policy.

## 5. User journeys (SPEC-bound)

For each archetype-specific critical path (e.g., browse → product → checkout for e-commerce; signup → onboarding → first-action for SaaS):

- [ ] Path completes end-to-end in production-like environment.
- [ ] Each step has analytics events firing per the event taxonomy in `spec/considerations.md`.
- [ ] Errors mid-path are recoverable, not dead-ends.
- [ ] Confirmation / success states are explicit.
- [ ] Post-completion email (if applicable) is sent and looks right.

## 6. Responsive (universal + SPEC-bound)

- [ ] Tested at all breakpoints from `spec/style.md`.
- [ ] No horizontal scroll on any standard viewport.
- [ ] Touch targets are at least 44×44 CSS pixels (WCAG 2.5.5).
- [ ] Mobile navigation works (drawer / hamburger / whatever the spec calls for).
- [ ] Text remains readable on small screens (no requirement to zoom).
- [ ] Images served responsively (`srcset`, `sizes`, modern formats).

## 7. Accessibility (universal, with target from SPEC)

- [ ] WCAG target from `spec/considerations.md` met across all critical pages.
- [ ] Automated audit (axe-core / Pa11y / Lighthouse) integrated in CI, with a clean run.
- [ ] Manual screen-reader test on at least one screen reader (NVDA / VoiceOver / TalkBack).
- [ ] Keyboard-only navigation works across the entire site (no mouse required).
- [ ] Focus indicators are visible and contrast-compliant.
- [ ] Skip-to-content link exists and works.
- [ ] All images have `alt` text (decorative images have `alt=""`).
- [ ] All form fields have associated labels.
- [ ] Error messages are programmatically associated with their fields.
- [ ] Heading hierarchy is correct (one `h1` per page, no skipped levels).
- [ ] Landmarks present: `header`, `nav`, `main`, `complementary`, `footer`.
- [ ] `prefers-reduced-motion` honored.
- [ ] Color contrast verified per `spec/style.md` audit, in every theme mode.
- [ ] Accessibility statement page exists.

## 8. Performance (universal, with budgets from SPEC)

- [ ] Lighthouse CI configured with thresholds from `spec/considerations.md`.
- [ ] LCP target met (typical < 2.5s).
- [ ] INP target met (typical < 200ms).
- [ ] CLS target met (typical < 0.1).
- [ ] TTFB acceptable on the target hosting region.
- [ ] Initial JS bundle within budget.
- [ ] Images optimized (modern format, responsive sources, explicit width/height).
- [ ] Fonts loaded with `font-display: swap`, preloaded if critical.
- [ ] Third-party scripts deferred or consent-gated.
- [ ] CDN configured with appropriate cache headers.
- [ ] Brotli compression enabled.
- [ ] No render-blocking resources above the fold.
- [ ] Tested on the target device + network combination from `spec/intake.md`.

## 9. SEO (universal)

- [ ] Per-page `<title>` and `<meta description>`.
- [ ] Open Graph + Twitter Card metadata on every public page.
- [ ] Canonical URLs set.
- [ ] Structured data (JSON-LD) per `spec/considerations.md` (Article, Product, BreadcrumbList, etc.).
- [ ] `sitemap.xml` submitted to Search Console and Bing Webmaster.
- [ ] `robots.txt` correct (staging blocks all; prod allows everything that should be indexed).
- [ ] Hreflang tags if multi-language.
- [ ] 301 redirects from any old URLs that are being replaced.
- [ ] No broken internal links (link-check passes).

## 10. Security (universal)

- [ ] HTTPS enforced; HSTS configured.
- [ ] CSP configured and tested (no console errors).
- [ ] Other security headers set: `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, frame-ancestors.
- [ ] CSRF protection enabled on every state-changing endpoint.
- [ ] Auth rate limiting in place.
- [ ] Secrets are not in the client bundle or source code.
- [ ] Secret scanning in CI passes.
- [ ] Dependency scanning passes (Dependabot, Snyk, or equivalent).
- [ ] All forms have spam protection (honeypot + Cloudflare Turnstile / hCaptcha / equivalent).
- [ ] File uploads (if any): type-checked, size-limited, virus-scanned where applicable.
- [ ] Webhook endpoints verify signatures.
- [ ] Sensitive data (PII, PHI, payment) handled per the regulations in `spec/considerations.md`.

## 11. Privacy and consent (universal, with regimes from SPEC)

- [ ] Privacy policy page exists and matches actual data handling.
- [ ] Terms of service page exists.
- [ ] Cookie banner appears in jurisdictions requiring consent (GDPR / ePrivacy / CCPA / LGPD).
- [ ] Cookies / trackers are blocked until consent in those jurisdictions.
- [ ] "Reject all" available in one click where required.
- [ ] Cookie policy enumerates every cookie and tracker.
- [ ] DSAR / data deletion process documented.
- [ ] Sub-processor list available if SaaS.
- [ ] Age gate present if COPPA / under-13 audience applies.
- [ ] Accessibility statement linked from footer.

## 12. Analytics, monitoring, alerting (universal)

- [ ] Web analytics configured per `spec/tooling.md`.
- [ ] Event taxonomy from `spec/considerations.md` implemented; events fire correctly.
- [ ] Error tracking (Sentry / Rollbar / etc.) installed with source maps uploaded.
- [ ] PII scrubbed from error reports.
- [ ] Logs structured (JSON), with retention per the spec.
- [ ] Uptime monitoring active for critical endpoints.
- [ ] Status page (if specified) configured.
- [ ] Alert thresholds configured; alerts route to on-call.
- [ ] Real user monitoring (RUM) active if specified.

## 13. CI/CD (universal)

- [ ] CI pipeline runs lint, type-check, tests, build, deploy.
- [ ] Required checks block merging to main.
- [ ] Preview deployments per PR.
- [ ] Production deploys on merge (or per the trigger in `spec/considerations.md`).
- [ ] Smoke tests run post-deploy.
- [ ] Rollback procedure documented and tested at least once.
- [ ] Database migrations gated separately from app deploys, with a documented procedure.
- [ ] Secrets in CI use the secret manager from `spec/tooling.md`.

## 14. Internationalization (if applicable from SPEC)

- [ ] All UI strings extracted (no hard-coded copy in JSX/Liquid/templates).
- [ ] Translations exist for every launch language.
- [ ] RTL CSS works for RTL languages.
- [ ] Locale-specific assets (images with text, currency, dates) load correctly.
- [ ] URL strategy works for every locale.
- [ ] `lang` attribute set correctly on `<html>`.

## 15. Content readiness (universal)

- [ ] All real content in place (no Lorem Ipsum, no placeholder images).
- [ ] All images have meaningful alt text.
- [ ] All links work (link-check in CI passes).
- [ ] All forms tested end-to-end with real submissions to real receivers.
- [ ] Legal pages (privacy, terms, cookies) reviewed by counsel if required.

## 16. Browser compatibility (universal, with matrix from SPEC)

- [ ] Tested on browser matrix from `spec/intake.md` or `spec/considerations.md`.
- [ ] Graceful degradation for older browsers (or explicit fallback notice).
- [ ] No console errors in any supported browser.

## 17. Launch readiness (universal)

- [ ] Production DNS configured (apex + www).
- [ ] TLS certificate auto-renewal verified.
- [ ] 301 redirects from old URLs in place.
- [ ] OG / Twitter cards validated with each platform's debug tools.
- [ ] Search Console verified; sitemap submitted.
- [ ] Status page live.
- [ ] Monitoring dashboards live.
- [ ] On-call rotation confirmed for launch window.
- [ ] Rollback procedure rehearsed.
- [ ] Backup verified by restoring once.
- [ ] Day-0 monitoring plan in place (hour-by-hour for the first 24h).
- [ ] Day-7 retrospective scheduled.
- [ ] Support channels open (email, chat, ticketing) and tested.
- [ ] Internal team trained on the runbook.

## 18. Documentation (universal)

- [ ] `README.md` in the site repo covers setup, build, deploy.
- [ ] Architecture diagram or one-page explainer.
- [ ] Runbook for common ops (deploys, rollbacks, incident triage).
- [ ] Content authoring guide (if non-engineers update content).
- [ ] Theming / styling guide (how to update tokens, add components).
- [ ] Onboarding doc for new contributors.
- [ ] `spec/SPEC.md` archived as the definition of done.

---

## How to use this checklist

### As an implementing agent

1. Open `spec/SPEC.md` alongside this file.
2. Walk every section. For each item, either check it off or write a one-line reason why it doesn't apply.
3. Open issues for anything not yet done.
4. Don't declare done until every applicable item is checked.

### As a human reviewer

1. Read `spec/SPEC.md` first.
2. Open the site (staging or prod).
3. Walk this checklist clicking through the site as you go.
4. Flag any item that's checked but doesn't actually work.

### Adapt this list

This is a starting list. Per `AGENTS.md` §3.3, the framework is non-exhaustive. Add archetype-specific items from `catalogs/considerations-catalog.md`. Remove items that genuinely don't apply (and write why).

---

## When something is missing

If you find that an implementation is incomplete relative to `spec/SPEC.md`, do **not** silently fill in the gap. Either:

- **The spec was wrong** → file a revision request and loop back to the relevant phase per [AGENTS.md §4.4](./AGENTS.md#44-revision-protocol-when-a-later-phase-changes-an-earlier-one), then re-validate.
- **The implementation was wrong** → fix it, run the checklist again.
- **Scope changed mid-build** → update `spec/SPEC.md`, get user sign-off on the change, then implement.

The spec is the contract. Drift between spec and built site is a bug.
