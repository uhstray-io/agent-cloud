# Considerations

> Phase 5 artifact — convergence phase.
> Status: awaiting user approval.

> **agent-cloud alignment (operative overrides):** Original standalone WebSmith
> spec, preserved verbatim. References to **Kamal**, **SSH deploys**, and
> **`kamal rollback`** are **superseded** by **Semaphore-orchestrated Ansible** +
> **OpenBao** secrets + **Podman** + **central Caddy**. Rollback is a re-run of the
> deploy playbook against a prior image tag, not `kamal rollback`. See
> [`SPEC.md` → Alignment with agent-cloud conventions](SPEC.md#alignment-with-agent-cloud-conventions).

---

## Archetype-driven scope

- **Primary archetype:** E-commerce storefront
- **Secondary archetype:** Generative Custom Goods Configurator (custom — not in catalog)
- **Checklists walked:** E-commerce (§5.1 full), Cross-cutting baseline (§4 full), custom AI/generative section

---

## Cross-cutting baseline

### Accessibility

- **Target:** WCAG 2.1 AA — confirmed across all phases.
- **Regional law:** ADA (Americans with Disabilities Act, US) — applies to e-commerce. ADA Title III accessibility litigation against online retailers is actively increasing. Checkout flows are specifically targeted. UhhCraft must prioritize accessible checkout, form inputs, and the 3D canvas action bar.
- **Automated checks:** axe-core via `@axe-core/playwright` in CI — runs against: home, catalog, catalog item, canvas, checkout, sign-in, sign-up, account pages. Runs on every merge to main.
- **Manual testing plan:** No manual screen reader testing by the team (noted in intake). Axe-core CI provides the automated AA verification layer. Revisit manual testing if the site faces an ADA complaint or before a significant marketing push.
- **Accessibility statement:** Page at `/legal/accessibility`. States WCAG 2.1 AA target, automated testing approach, contact method for reporting accessibility issues (`accessibility@uhhcraft.uhstray.io`).
- **Reporting channel:** Email address in accessibility statement. Commit to responding within 5 business days.
- **Vendor accessibility:** Stripe Payment Element is WCAG AA compliant (Stripe's responsibility). Umami analytics snippet has no user-facing UI. Three.js canvas: keyboard access to action bar is required (documented in Phase 2 template).

---

### SEO and discoverability

- **Title template:** `{Page Name} | UhhCraft` — e.g., "Fox Sticker Pack | UhhCraft", "Create Something | UhhCraft".
- **Meta descriptions:** Unique per page, 150–160 chars, written at content-authoring time. Catalog items use product description. Generated canvas pages: not indexed (see below).
- **Canonical URLs:** Set on every page. Generated canvas URLs (`/canvas/[generation-id]`) are `noindex` — unique per-session URLs should not appear in search.
- **Open Graph:** `og:title`, `og:description`, `og:image` (1200×630 per Phase 4), `og:type` (`website` for site pages, `product` for catalog items). Twitter card: `summary_large_image`.
- **sitemap.xml:** Generated at build/deploy time. Includes: home, catalog, category pages, catalog item pages, about, legal pages. Excludes: `/account/*`, `/cart`, `/checkout`, `/canvas/*`, `/order/*`.
- **robots.txt:** Disallow `/account/`, `/cart`, `/checkout`, `/canvas/`, `/order/`, `/api/`. Allow all else. Reference sitemap.xml.
- **Structured data (JSON-LD):**
  - `Product` schema on all catalog item pages: name, description, image, offers (price, currency, availability).
  - `Organization` on homepage: name, url, logo, contactPoint.
  - `BreadcrumbList` on catalog and category pages.
  - Canvas pages for catalog items: same `Product` schema (not for generated items — no-index).
- **URL strategy:** Lowercase, hyphen-separated, no trailing slashes. Examples: `/catalog/animals`, `/catalog/fox-sticker-pack`, `/generate`, `/about`.
- **Redirects:** No existing site to migrate from (per intake). No redirect strategy needed at launch.
- **Hreflang:** N/A (English only).
- **Search Console:** Register `uhhcraft.uhstray.io` in Google Search Console at launch. Submit sitemap. Monitor Core Web Vitals report.
- **Internal linking:** Homepage showcases catalog items (links to canvas). Catalog links to categories. About page links to catalog and generate.
- **Image alt text:** Required on all images (policy from Phase 4). Catalog item images use item name + description. Generated items use the prompt text.
- **Indexability per environment:** Production only. No staging environment at launch (self-hosted). If staging is added later, `X-Robots-Tag: noindex` on all staging responses.

---

### Performance

- **Budgets:**
  - LCP: < 1.0s (user-specified — stricter than Google's "good" threshold of 2.5s)
  - INP: < 200ms
  - CLS: < 0.1
  - TTFB: < 400ms (server-rendered Go on local hardware — well achievable)
  - JS bundle (non-canvas pages): < 50KB total (HTMX ~14KB + minimal custom JS)
  - JS bundle (canvas page): < 700KB (Three.js ~600KB + canvas interaction code)
  - Image weight per page: < 500KB (homepage showcase images are the largest concern)
  - Total page weight: < 1MB on standard pages; canvas page may be heavier (3D asset)
- **Lighthouse CI:** Enforce Performance ≥ 90 on home and catalog pages. Canvas page: Performance ≥ 75 (Three.js load is unavoidable). Runs on every merge to main.
- **Image strategy:**
  - Format: AVIF primary, WebP fallback, original JPEG/PNG as last resort. Conversion at upload time by Go image processing (imaging library or vips).
  - Responsive sources: `srcset` with 2–3 size variants. `sizes` attribute on all images.
  - Lazy loading: `loading="lazy"` on all images below the fold. Eager on hero/above-fold.
  - Explicit `width` and `height` on all images to prevent CLS.
  - Storage: MinIO serves images; Caddy adds `Cache-Control: public, max-age=31536000, immutable` for hashed asset URLs.
- **Font loading:** Self-hosted Nunito (WOFF2, Latin subset). `<link rel="preload" as="font" crossorigin>` for regular (400) and semibold (600) weights. `font-display: swap` with `size-adjust` on fallback font to minimize CLS.
- **Critical CSS:** Go server inlines critical above-fold CSS on HTML responses. Remaining Tailwind CSS served as a cached static file.
- **Third-party scripts:** Umami analytics snippet (self-hosted — same origin, no third-party request). Stripe.js (loaded async on checkout page only, not globally). No other third-party scripts.
- **Compression:** Brotli enabled in Caddy (`encode brotli gzip`). Brotli for modern browsers, gzip fallback.
- **Caching:**
  - Static assets (CSS, JS, fonts): `Cache-Control: public, max-age=31536000, immutable` (hashed filenames).
  - Dynamic HTML pages: `Cache-Control: no-store` (personalized content — cart, auth state).
  - Catalog page (no auth required): `Cache-Control: public, max-age=300` (5-minute CDN/browser cache). Suitable since catalog doesn't change frequently.
  - API responses: appropriate `Cache-Control` per endpoint.
- **Preconnect:** `<link rel="preconnect" href="https://js.stripe.com">` on checkout pages. No other external origins.
- **No service worker** at launch. Can be added later for offline catalog browsing.
- **Database:** sqlc generates parameterized queries; Go server uses connection pooling (pgx pool). No N+1 queries — joins and batch fetches enforced in query review.

---

### Privacy, consent, and legal

#### Privacy policy
- **Jurisdiction:** US-focused. Not GDPR-compliant at launch (no EU users). CCPA: at launch, revenue likely below $25M threshold — exempt from CCPA operator obligations. Add "Do Not Sell or Share My Personal Information" link in footer as good practice regardless.
- **Data collected:** Email address, shipping name and address, order history, generation prompts (logged but not attributed to PII), generation assets (linked to account). Payment: Stripe tokenizes card data — UhhCraft never stores raw card numbers.
- **Legal pages required:** `/legal/privacy`, `/legal/terms`, `/legal/returns`. All must be live before launch. No Lorem Ipsum.
- **Who writes:** Operator team. Recommend using a privacy policy generator (Iubenda, Termly) as a starting point, then customizing. Note: no legal counsel currently engaged — **strongly recommend a lawyer review before first sale**, particularly for IP/copyright terms and returns policy.

#### Terms of Service
Key clauses required:
- Age requirement: users must be 13 or older (COPPA provision).
- Returns/cancellation: see Returns section.
- Content moderation: UhhCraft may refuse or cancel orders for prompts that violate the content policy.
- IP/copyright: AI-generated works may have uncertain copyright status; UhhCraft makes no copyright claim on generated designs; users make no copyright claim; UhhCraft retains a production license to manufacture and fulfill the order; users purchase the physical item, not digital rights.
- Safety disclaimer: 3D printed items are decorative only. Not food-safe, not load-bearing, not suitable for safety-critical applications.
- AI disclosure: products are designed using AI generation tools.
- Limitation of liability, dispute resolution, governing law (state TBD).

#### Cookie policy
- **Cookies in use:** Session cookie (essential — HTTP-only, SameSite=Lax, Secure), Stripe (essential — payment processing), Umami analytics (no personal data, no cross-site tracking, no cookie by default in Umami v2 script mode).
- **Consent banner:** Minimal. Umami in script mode does not set cookies. Stripe cookies are essential. No marketing/tracking pixels. A simple persistent notice ("This site uses essential cookies for checkout. [Learn more]") with a link to the cookie policy is sufficient for US-only operation. No granular opt-out required at launch.
- **Future:** If marketing pixels (Meta, Google Ads) are added, a full consent management platform (Osano, Usercentrics) is required.

#### Data Subject Access Requests
- **Process:** Email `privacy@uhhcraft.uhstray.io`. Operator reviews and responds within 30 days.
- **Deletion:** Account deletion removes: user record, saved designs, saved addresses. Order records retained for 7 years (tax/accounting). Anonymize PII in retained order records (replace name/email with "Deleted User" + order ID).
- **Export:** Provide order history CSV on request.

#### Data retention
| Data type | Retention |
|-----------|-----------|
| Order records | 7 years (tax compliance) |
| Account data | Duration of account + 90 days post-deletion |
| Generation history (saved designs) | Last 10 per account, rolling |
| Session data | 30-day idle TTL; 90-day absolute TTL |
| Logs | 30 days |
| Blocked prompt logs | 90 days (anonymized) |
| Postgres backups | 30 days of daily snapshots |

#### Age gating (COPPA — per Q2, option A)
- 13+ self-certification checkbox at:
  - Sign-up form: "I confirm I am 13 years of age or older."
  - Guest checkout step 1 (before email is collected): same checkbox.
- Privacy policy states: this site is not directed at children under 13; users known to be under 13 will have their accounts terminated.
- No COPPA-compliant parental consent system at launch (impractical for a small shop). Payment card requirement provides a de facto adult gate for purchasing.

#### AI-generated content disclosure
- On all product and canvas pages: small label "Created with AI generation tools."
- In ToS and About page: full disclosure that products are designed using AI.
- Brand positioning: this is celebrated, not hidden.

---

### Security

- **HTTPS / HSTS:** Caddy enforces HTTPS on all routes. HSTS header: `Strict-Transport-Security: max-age=31536000; includeSubDomains`. HSTS preload: submit to hstspreload.org after first stable production deploy.
- **Security headers (Caddy config):**
  - `Content-Security-Policy: default-src 'self'; script-src 'self' https://js.stripe.com; frame-src https://js.stripe.com; connect-src 'self' https://api.stripe.com; img-src 'self' data: blob:; font-src 'self'; style-src 'self' 'unsafe-inline'` (unsafe-inline needed for Tailwind inline styles — consider nonce-based CSP in future)
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: SAMEORIGIN`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: camera=(), microphone=(), geolocation=()`
- **CSRF protection:** SameSite=Lax on session cookie. All state-mutating endpoints use POST/PUT/DELETE (no GET-triggered mutations). Go scs session manager handles this correctly.
- **XSS prevention:** templ auto-escapes all template output. No `html/template` raw output. CSP as second layer.
- **SQLi prevention:** sqlc generates parameterized queries exclusively. No string concatenation in DB queries.
- **Authentication hardening:**
  - bcrypt passwords, cost factor 12.
  - Login rate limiting: 5 attempts per 15 minutes per IP (Redis).
  - Account lockout: 10 failed attempts triggers 15-minute lockout.
  - Session rotation: new session ID issued on login.
  - Session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, 30-day idle timeout, 90-day absolute timeout.
- **Webhook validation:**
  - Stripe: validate `Stripe-Signature` header on all webhook endpoints. Reject unsigned requests with 400.
  - Printful/Printify/Shapeways: validate webhook signatures where provided; verify order IDs match internal records.
- **File/asset security:** MinIO generated assets accessed via presigned URLs with TTL (15-minute URLs for display, longer TTLs for manufacturing asset download by operator). No public anonymous bucket access for private assets.
- **Content moderation (generation pipeline security):**
  - Server-side keyword blocklist pre-screening before any prompt reaches ComfyUI/Hunyuan3D.
  - Blocklist categories: top-50 copyrighted character names, hate symbols, slurs, NSFW keywords, political figures.
  - Blocklist maintained as a DB table (operator can update without code deploy).
  - Blocked prompt logs: prompt text + timestamp (no PII) stored for 90 days for abuse monitoring.
  - Model-level safety filters (Flux.1 built-in) as second layer.
- **Rate limiting:**
  - Generation: per-session (guests), per-account (users) via Redis counters with TTL.
  - Login: per-IP via Redis.
  - Checkout: per-session via Redis.
  - All API endpoints: basic per-IP rate limiting via Caddy rate limit middleware.
- **Secrets management:** Environment variables on server (`/etc/uhhcraft/env`); GitHub Actions secrets for CI; `.env` (gitignored) for local dev. Dependabot + GitHub secret scanning prevent accidental commits.
- **Dependency scanning:** Dependabot weekly for Go modules and Python AI service packages. Auto-merge patch updates; manual review for minor/major.
- **SAST:** golangci-lint with `gosec` linter in CI. Flags common Go security issues.
- **Incident response:**
  - Alerts via Discord (Better Stack downtime, Sentry high-frequency errors, Stripe disputes).
  - 2-person team on best-effort response (no formal on-call rotation per intake).
  - Runbook covers: restart services, rollback deploy, restore from backup, Stripe dispute response.
  - Breach notification: if customer PII is exposed, notify affected users within 72 hours via email and update the privacy policy. No regulatory breach notification obligation at this scale (CCPA notifiable breach threshold applies at higher data volumes).
- **Penetration testing:** Not at launch. Schedule before first significant marketing push or at $10K monthly revenue — whichever comes first.
- **Vulnerability disclosure:** `security@uhhcraft.uhstray.io` listed in `/security.txt` at `/.well-known/security.txt`. Commit to acknowledging within 5 business days.
- **PCI DSS scope:** SAQ A confirmed. Stripe Payment Element in an iframe handles all card data. UhhCraft never touches raw PAN. Annual SAQ A self-assessment when the business is ready.

---

### Content and assets

- **Source of truth:** Postgres DB (catalog items, orders, accounts, discounts); MinIO (generated assets, catalog images); hardcoded templ templates (legal, about copy).
- **Content owners:** 2-person operator team.
- **Asset licensing:** Catalog items — operator-owned content; generated items — ambiguous IP (acknowledged in ToS, per Q6); 3D print safety photos — operator-owned.
- **Image optimization pipeline:** Images uploaded by operator to MinIO → Go service converts to AVIF + WebP at upload time + stores originals → serves AVIF/WebP via content-negotiation.
- **Launch content readiness checklist:**
  - [ ] Minimum 10 catalog items loaded (images, models, descriptions, prices, material options)
  - [ ] About page written (fox mascot story, AI generation philosophy)
  - [ ] Privacy policy written and reviewed
  - [ ] Terms of Service written and reviewed
  - [ ] Returns policy written and reviewed
  - [ ] Accessibility statement written
  - [ ] Security.txt file present
  - [ ] No Lorem Ipsum anywhere
  - [ ] No broken images or missing 3D model files
  - [ ] All material/cut-type option labels finalized (OQ-NEW-1 resolved)
- **3D print safety disclaimer:** On every 3D print product page and order confirmation: "Decorative use only. Not food-safe, not load-bearing, not suitable for safety-critical applications. Keep away from small children."
- **Lead time disclosure:** On product pages and checkout: "Handcrafted to order — [X–Y] business days." Exact timeframe: **TBD before launch** based on actual production capacity.
- **Stale content:** Catalog items reviewed by operator quarterly. No automated stale detection.

---

### Analytics, monitoring, observability

#### Event taxonomy

All events tracked via Umami custom events (privacy-respecting, no PII):

| Event name | Trigger | Properties |
|-----------|---------|-----------|
| `generate_started` | Generate button click | product_type (sticker/print) |
| `generate_completed` | AI returns result | product_type, duration_ms |
| `generate_rejected` | User clicks "Try Again" | product_type |
| `canvas_accepted` | User clicks "Add to Cart" | product_type |
| `catalog_item_viewed` | Canvas page load (catalog flow) | item_slug |
| `add_to_cart` | Add to cart confirmed | product_type, item_type (catalog/generated) |
| `checkout_started` | Checkout page load | cart_value (rounded to nearest $10) |
| `checkout_completed` | Order confirmation page load | cart_value (rounded) |
| `account_created` | Sign-up completed | — |
| `third_party_routed` | Operator routes to Printful/Shapeways | provider, product_type |

**Key funnels:**
1. Generate funnel: `generate_started` → `generate_completed` → `canvas_accepted` → `checkout_completed`
2. Catalog funnel: `catalog_item_viewed` → `add_to_cart` → `checkout_completed`
3. Overall conversion: unique visitors → `checkout_completed`

- **Sentry:** Error sampling 100% at launch (low traffic). PII scrubbing: no emails, addresses, or payment data in error context — user ID only. Source maps uploaded in CI.
- **Logs:** Structured `slog` to stdout → systemd journal. 30-day retention. No PII in logs (user IDs only). Fields: timestamp, level, request_id, user_id (anonymized), duration_ms, status_code, endpoint.
- **Uptime monitoring:** Better Stack checks `/health` endpoint every 60 seconds from 3 US locations. Alert to Discord + email if down > 2 minutes. Status page: `status.uhhcraft.uhstray.io`.
- **Alerting routes:**
  - Better Stack downtime → Discord `#ops-alerts` + email
  - Sentry new error / high-frequency error → Discord `#ops-alerts`
  - Stripe payment received → Discord `#orders` (per intake requirement)
  - Third-party fulfillment routing → Discord `#orders`
  - Stripe dispute filed → Discord `#ops-alerts` + email
- **On-call:** None. Best-effort by 2-person team (per intake). Monitor Discord during business hours.
- **SLO:** 99.5% uptime. Measured by Better Stack external checks. Review monthly.

---

### Deployment and hosting

- **Environments:**
  - **Local dev:** Developer machines. `air` hot-reload + local Postgres/Redis/MinIO via Docker Compose.
  - **Production:** Main server on local network. Single environment at launch.
  - **Staging:** Not at launch. Add a second machine or a `staging.uhhcraft.uhstray.io` subdomain when the team grows or before a major marketing push.
- **Domain:** `uhhcraft.uhstray.io` — A record pointing to the local network's public static IP.
- **Static IP:** **⚠ Must confirm before launch (OQ-NEW-7).** If the ISP provides a dynamic IP, set up dynamic DNS (ddclient + Cloudflare DNS API) to keep the A record current. Cloudflare proxy mode can be optionally enabled for DDoS protection without affecting self-hosted control.
- **DNS provider:** Managed via existing `uhstray.io` DNS (Cloudflare recommended — free tier, fast propagation, DDoS mitigation option).
- **DNS TTL:** 300 seconds (5 minutes) initially. Can lower to 60s before planned maintenance windows.
- **TLS:** Caddy + Let's Encrypt. Automatic ACME challenge. Auto-renews 30 days before expiry. `tls` directive in Caddyfile handles everything.
- **Region:** US (local network). No geographic failover.
- **Failover:** None at launch. Main server goes down → site is down. Better Stack alerts the team. Bad deploys are rolled back by re-running the Semaphore "Deploy UhhCraft" pipeline against a prior image tag (not `kamal rollback`). Hardware failure: restore from backup to replacement hardware (RTO ~4h).
- **Backups:**
  - **Postgres:** `pg_dump` via cron daily at 02:00 local time. Compressed (`.dump` format). Stored in MinIO under `backups/postgres/YYYY-MM-DD.dump`. Retain 30 daily snapshots.
  - **MinIO data (generated assets, catalog images):** `mc mirror` weekly to an external USB drive or Backblaze B2 bucket. Retain last 4 weekly snapshots.
  - **Go binary + config:** Reproducible from GitHub. No backup needed.
  - **Backup restore testing:** Test Postgres restore monthly (restore to local dev DB, verify row counts and spot-check orders).
- **DR:**
  - **RPO (recovery point objective):** ~24 hours (daily Postgres backup).
  - **RTO (recovery time objective):** ~4 hours (provision replacement hardware, restore Postgres from backup, redeploy Go binary, restore MinIO from backup, update DNS if IP changed).
  - Acceptable per intake's "moderate" backup/DR appetite.
- **Cost monitoring:** Self-hosted — no cloud hosting bills. Monitor Stripe processing fees (2.9% + $0.30/transaction). Resend email: free tier 3,000/month; upgrade if exceeded. Better Stack: free tier for basic monitoring.
- **UPS (Uninterruptible Power Supply):** Recommended for main server. Protects against short power outages. Budget: ~$80–150 for a suitable UPS.

---

### CI/CD

- **Branching model:** Trunk-based development. Short-lived feature branches → pull request → merge to `main`. Direct commits to `main` only for hotfixes.
- **Required checks before merge:**
  - `gofmt` — no unformatted code
  - `golangci-lint` (includes `gosec`, `errcheck`, `staticcheck`, `govet`)
  - `templ generate` — verify no uncommitted generated files
  - `go test ./...` (unit + integration via testcontainers)
  - Playwright E2E on main branch only (slow; skip on draft PRs)
  - Lighthouse CI (home + catalog pages)
  - axe-core accessibility checks
- **Code review:** PRs reviewed by the other team member before merge. No CODEOWNERS file at this scale. 1 approver required.
- **Preview deployments:** None (self-hosted). Test features on local dev machines before merging.
- **Production deploy trigger:** Automatic on merge to `main`. Kamal deploys via SSH.
- **Deployment strategy:** Kamal zero-downtime (starts new container, health-checks, swaps traffic, stops old).
- **Smoke tests post-deploy:** Health check endpoint `GET /health` → 200 OK. Kamal checks this automatically. Better Stack synthetic check confirms site is up.
- **Rollback:** `kamal rollback` redeploys previous Docker image. Time to rollback: ~30 seconds. Database migrations: forward-only at launch (no down migrations). If a migration needs reversal, treat it as a new forward migration.
- **Database migration strategy:** `goose up` runs as a pre-deploy step in CI. Deploy only proceeds if migrations succeed. Migrations are additive (no destructive changes to existing columns without a deprecation phase).
- **Feature flags:** None at launch.
- **Changelog:** CHANGELOG.md maintained manually. Update before each significant release.

---

### Testing strategy

- **Unit tests:** `go test ./...`. Target ~60% line coverage on business logic packages: pricing, discount calculation (loyalty tiers), fulfillment routing, auth, rate limiting, generation queue.
- **Integration tests:** `testcontainers-go` spins up real Postgres + Redis. Covers: order creation end-to-end, auth sign-up/sign-in/password-reset, discount eligibility calculation, fulfillment routing, generation job enqueue/dequeue.
- **E2E (Playwright):** Critical user journeys:
  1. Homepage load → showcase gallery visible → no accessibility violations
  2. Browse catalog → select item → configure material → view in 3D canvas → add to cart
  3. Generate flow → prompt → canvas → accept → add to cart → guest checkout → order confirmation
  4. Account sign-up → sign-in → view account dashboard → view order history
  5. Account checkout → priority queue flag verified → discount applies when eligible
  6. 404 page renders correctly
- **Accessibility (axe-core):** Run on: home, catalog, canvas, checkout (all 3 steps), sign-in, sign-up, account dashboard. Zero WCAG AA violations required to pass CI.
- **Performance (Lighthouse CI):** Home page: LCP < 1.0s, Performance ≥ 90. Catalog page: LCP < 1.0s, Performance ≥ 90. Canvas page: LCP < 2.5s, Performance ≥ 75 (Three.js load is accepted).
- **Visual regression:** Not at launch. Add Chromatic or Argos when the team is spending significant time on UI regressions.
- **Load testing:** Not at launch. Run k6 against a local staging environment before any marketing push expected to drive > 100 concurrent users.
- **Manual QA checklist (before launch and before significant releases):**
  - [ ] Full generate flow: sticker + 3D print, accept, add to cart, checkout, order confirmation
  - [ ] Full catalog flow: browse, configure material, canvas, add to cart, checkout
  - [ ] Guest checkout: email received, order confirmation rendered
  - [ ] Account checkout: email received, priority flag set, discount applies
  - [ ] Password reset flow end-to-end
  - [ ] Stripe test mode: checkout with test card numbers (success, declined, 3DS)
  - [ ] Discord webhook: payment notification received
  - [ ] Fulfillment routing: mark as third-party, verify API call to Printful/Shapeways
  - [ ] Blocked prompt: verify keyword blocklist fires, friendly error shown
  - [ ] Age gate checkbox: verify present at sign-up and guest checkout
  - [ ] Mobile: run full purchase flow on a mobile browser
  - [ ] Dark mode: toggle, verify contrast on all key pages
  - [ ] Reduced motion: enable in OS, verify no animations

---

### Internationalization

- **N/A.** English only at launch. Explicitly out of scope.

---

### Maintenance and handoff

- **Maintainer:** 2-person operator team post-launch.
- **Documentation deliverables (required before launch):**
  - `README.md` — project setup, local dev instructions, environment variables, how to build and deploy.
  - `RUNBOOK.md` — restart services (Go app, Caddy, Postgres, Redis, MinIO, ComfyUI, Hunyuan3D); rollback a deploy; restore Postgres from backup; add a catalog item via psql; manually route an order to Printful/Shapeways; respond to a Stripe dispute.
  - Architecture diagram — the stack diagram from `spec/tooling.md`, kept in the repo.
  - Content authoring guide — SQL snippets / psql commands for adding/editing/deactivating catalog items; updating material options.
  - AI service management — how to restart ComfyUI and Hunyuan3D systemd services; update Flux.1 model; add/update content moderation blocklist.
- **Dependency update cadence:** Dependabot weekly PRs. Auto-merge patch versions if CI passes. Manual review for minor versions. Major versions reviewed quarterly.
- **Browser support matrix:**
  - Chrome (last 2 versions), Firefox (last 2 versions), Safari (last 2 versions), Edge (last 2 versions).
  - Mobile: iOS Safari (last 2 versions), Chrome Android (last 2 versions).
  - Not supported: IE11, Opera Mini, legacy Edge (EdgeHTML).
  - Re-evaluate annually.
- **Sunset criteria:** No formal sunset plan. The site is long-lived. Revisit if: the business model changes significantly, the Go/templ/HTMX stack becomes unsupported, or the team grows and requires a CMS.

---

### Launch readiness

**Pre-launch checklist:**
- [ ] **OQ-NEW-7 resolved:** Static public IP confirmed or dynamic DNS (ddclient + Cloudflare) configured
- [ ] DNS A record for `uhhcraft.uhstray.io` set and propagated
- [ ] Caddy TLS certificate issued and valid (verify via browser)
- [ ] HSTS header confirmed in response
- [ ] All legal pages live and reviewed: Privacy, Terms, Returns, Accessibility Statement, Security.txt
- [ ] All catalog launch items loaded (minimum 10, ideally 20+)
- [ ] Material/cut-type options finalized (OQ-NEW-1)
- [ ] Fox mascot + wordmark assets ready (OQ-STYLE-1, OQ-STYLE-2)
- [ ] Stripe live mode configured: API keys, Stripe Tax enabled, webhook endpoint registered (`/webhooks/stripe`)
- [ ] Discord webhook URL configured and tested (receive a test payment notification)
- [ ] Printful API key configured and tested (create a test order in Printful sandbox)
- [ ] Shapeways/Hubs API key configured and tested (OQ-NEW-10)
- [ ] Resend API key configured; test all email types (order confirmation, welcome, password reset)
- [ ] Better Stack monitoring active; status page live at `status.uhhcraft.uhstray.io`
- [ ] Sentry configured and receiving test errors
- [ ] Umami analytics confirming page views
- [ ] Full manual QA checklist completed (see Testing section)
- [ ] Postgres backup cron confirmed running; test restore verified on local dev
- [ ] UPS installed on main server
- [ ] ComfyUI + Hunyuan3D services confirmed healthy on all AI machines

**Launch approach:**
- **Soft launch:** Invite 5–10 friends/family first. Let them attempt the full purchase flow with a small discount. Gather feedback before public announcement.
- **Public launch:** Announce via whatever channels are available. No social media accounts currently; this is an open question (OQ-NEW-3 — creating social accounts is recommended for discoverability but was deferred as out of scope at launch).
- **Day-0:** Monitor Discord alerts channel and Sentry. Check Better Stack status. Watch for Stripe disputes or unusual orders.
- **Day-7 retrospective:** Review Umami funnel (where users drop off), Sentry errors (any patterns), Stripe revenue, order fulfillment time, any customer emails. Identify top-3 improvements for the first post-launch sprint.

---

## E-commerce archetype decisions

### Catalog and inventory

- **Product model:** Two types — (1) Custom-generated: one-of-a-kind, qty=1 per generation; (2) Catalog: pre-designed, made-to-order, no inventory cap.
- **Variant model:** Not traditional size×color variants. Instead: product type (sticker/3D print) + material/finish radial selection at the canvas page.
- **Oversell prevention (generated items):** Generated asset is locked to the cart session on "Add to Cart." **30-minute cart reservation.** If cart is abandoned or session expires, asset is released back to "unordered" state and the user can regenerate.
- **Catalog items:** Always available (made-to-order). No inventory tracking.
- **Low-stock alerts:** N/A.
- **One-of-a-kind authenticity:** Each generated item is produced from a unique generation. Catalog items are also made fresh for each order (same design, new physical instance). Both are legitimately "made just for you."

### Pricing

- **Currency:** USD only.
- **No regional pricing.** Single price worldwide (US-only shipping at launch).
- **Tax:** Exclusive display — price shown before tax; tax line item calculated and shown at checkout via Stripe Tax.
- **Bulk / loyalty discount (account holders):**
  - If a repeat account-holder places an order of **$30+**, a **5% discount** is applied to their **next order**.
  - If a repeat account-holder places an order of **$100+**, a **10% discount** is applied to their **next order**.
  - Discounts don't stack. The higher tier applies if both thresholds are met.
  - The discount is stored on the account record (`next_order_discount_pct`) and auto-applied at checkout. Cleared after application.
  - Guest customers: not eligible.
  - Threshold is based on a single order value (not cumulative spend).
- **No sale/promo pricing at launch.** Add capability (promo code system) post-launch if needed.

### Cart and checkout

- **Guest checkout:** Supported. Email collected at checkout step 1.
- **Account checkout:** Cart is account-linked (persists across devices); discount auto-applied if eligible.
- **Cart expiry:** Guest session cart expires with session (24h cookie TTL). Account cart: no expiry.
- **Generated item reservation:** 30-minute lock on "Add to Cart." Displayed in cart: "Your design is reserved for 29:45."
- **Abandoned cart email:** **Yes.** 2-hour delay. One email only (no drip). Sent to: guest (if email was collected at checkout step 1 before abandonment) and account holders. Message: warm and gentle — "You left something behind! Your [sticker/item] is waiting." Includes a link back to the canvas page if generation is still valid, or to catalog/generate if expired. Implemented via River job scheduled 2 hours after cart creation.
- **Express payments:** Stripe Payment Element automatically renders Apple Pay and Google Pay buttons when available in the user's browser. Enable both.
- **3DS2:** Stripe handles Strong Customer Authentication automatically for any card that requires it.
- **Address autocomplete:** Not at launch (US-only, simplify checkout). Add later.

### Payments

- **Processor:** Stripe (Payment Element — supports card, Apple Pay, Google Pay).
- **Failed payment recovery:** Stripe Payment Element shows inline error. User retries. No automated retry for guest orders.
- **Currency:** USD only.
- **Wallets:** Apple Pay + Google Pay via Stripe Payment Element (enabled by default).

### Tax and compliance

- **Stripe Tax:** Enabled on all orders. Automatic US sales tax calculation and nexus tracking per state. Stripe handles tax calculation and reporting. Configure Stripe Tax on the Stripe dashboard; pass `automatic_tax: { enabled: true }` on payment intents.
- **Nexus tracking:** Stripe Tax tracks nexus as orders accumulate. No manual state registration needed initially.
- **Tax display:** Exclusive (price + tax shown separately at checkout).
- **Invoicing:** Not required at launch (B2C only). Add if B2B orders emerge.

### Shipping

- **Free shipping:** On all orders **$50 USD or more** (before tax).
- **Below-threshold flat rate:** **TBD before launch** — set based on actual USPS/UPS rate for the typical package weight. Recommend starting at **$5.99** and adjusting after first 20 orders.
- **Carriers:** USPS (standard) for stickers and small 3D prints. UPS/FedEx for larger items. Manual label creation by operator at launch. Shippo integration is a post-launch improvement.
- **International shipping:** Not at launch (US only).
- **Tracking:** Operator enters tracking number in the DB when item ships. Status update email sent to customer via Resend.
- **Branded tracking page:** `/order/[order-id]` shows order status (processing → manufacturing → shipped → delivered). Customer links back here from the shipping email.
- **Local pickup:** Not offered.

### Returns and refunds

*(Per Q1 — option C: some return window)*

- **Custom-generated items and catalog items (made-to-order):** Cancellation is allowed if manufacturing has **not yet started**. Cancellation window: **24 hours from order placement**, subject to manufacturing not having started (whichever comes first).
- **Once manufacturing begins:** No returns or refunds. These are custom-made physical goods.
- **Defective or damaged items:** Full replacement at no cost to the customer. Customer must report within 14 days of delivery with a photo. Replacement produced and shipped at UhhCraft's expense.
- **Third-party fulfilled items (Printful/Shapeways):** UhhCraft mediates. Printful/Shapeways have their own defective item policies; UhhCraft coordinates the replacement.
- **Refund processing:** 5–10 business days to original payment method (Stripe refund timeline).
- **Returns policy page:** `/legal/returns` — written in plain, friendly language consistent with tone of voice.
- **No-questions-asked returns:** Not offered. Custom goods standard.

### Order email lifecycle

All emails via Resend. All use the email template from Phase 4 (orange header, fox mascot, warm tone).

| Trigger | Email | Recipient |
|---------|-------|---------|
| Order placed | "Your order is in!" — order summary, order number, expected lead time | Guest or account holder |
| Manufacturing started | "We're making it!" — fox with wide eyes illustration | Account holder only (no email for guests at this stage) |
| Order shipped | "It's on its way!" — tracking number, carrier link | Guest or account holder |
| Cancellation confirmed | "Order cancelled" — refund timeline | Guest or account holder |
| Account created | "Welcome to UhhCraft!" — benefits list, link to generate | Account holder |
| Email verification | "Verify your email" — verification link (expires 24h) | New account holder |
| Password reset | "Reset your password" — reset link (expires 1h) | Account holder |
| Abandoned cart | "You left something behind!" — 2h delay | Guest (if email collected) or account holder |

### Trust signals

- Stripe "Secure Checkout" badge on checkout page.
- "Made to order" badge on product/canvas pages.
- "Unique item" badge on generated items.
- "AI-designed" badge on all items (disclosure + brand feature).
- **No reviews/ratings at launch.** Add after sufficient orders (minimum ~20 reviews) — consider Judge.me or a simple DB-backed review model.

### Loyalty and retention

- Loyalty discount system (per Q4): stored per account, auto-applied at checkout.
- No wishlist, no formal loyalty program, no referral program at launch (non-goals).
- Reorder: account holders can reorder from order history (adds the same catalog item + material to cart; generated items cannot be truly reordered — prompts to generate something new).

### Fraud

- **Stripe Radar:** Default rules enabled. Free tier included with Stripe.
- **High-value order flag:** Stripe Radar rule: orders > $150 flagged for manual review. Operator receives Discord alert. Operator reviews in Stripe Dashboard before fulfillment.
- **All orders visible:** Every order triggers a Discord notification. Operator reviews manually at this volume (low traffic at launch).
- **Chargebacks:** Handled manually via Stripe Dashboard. Operator submits evidence (order record, generation asset, shipping confirmation).

### PCI DSS

- **SAQ A scope confirmed.** Stripe Payment Element renders in an iframe hosted by Stripe. UhhCraft never processes, stores, or transmits raw cardholder data.
- **Annual SAQ A self-assessment:** Complete when first required by your payment processor or acquirer.

---

## Generative Custom Goods Configurator — archetype decisions

*(Custom archetype — no catalog entry. Decisions specific to UhhCraft's AI generation pipeline.)*

### Content moderation

*(Per Q5 — option B: keyword pre-screening)*

- **Layer 1 — Keyword blocklist (server-side, pre-AI):**
  - Blocklist categories: top-100 trademarked character names (Mickey Mouse, Pikachu, Iron Man, Elsa, etc.), hate symbols and slurs, NSFW keyword lists, real political figures' names, violent content keywords.
  - Blocklist stored in DB table `prompt_blocklist` (category, term, active boolean). Operator updates without code deploy.
  - If prompt matches: reject with friendly message — *"We can't make that one — try something original! The best designs come from your imagination."*
  - Blocked attempts logged (prompt text only, no PII) for 90 days.
- **Layer 2 — Model safety filters:** Flux.1 Schnell and Hunyuan3D have built-in content filters. Accept that some edge cases slip through; handle reactively.
- **No post-generation review at launch** (contradicts hands-off ops). Add a review queue if abuse is detected post-launch.
- **Intellectual property:** UhhCraft is not liable for user-provided prompts that reference copyrighted material. ToS states users are responsible for ensuring their prompts don't infringe third-party IP. Blocklist is a good-faith mitigation, not a guarantee.

### IP and copyright

*(Per Q6 — option C: acknowledge ambiguity, no claims on either side)*

In the Terms of Service:
- AI-generated works may have uncertain copyright status under current US law (Thaler v. Vidal precedents; US Copyright Office guidance).
- UhhCraft makes no copyright claim on generated designs.
- Users make no copyright claim on generated designs.
- UhhCraft retains a **production license** to all generated assets for the sole purpose of manufacturing and fulfilling the order.
- Customers are purchasing a physical item. No digital asset rights are transferred.
- **Recommendation before scaling:** Engage a lawyer to review and strengthen the IP terms. This is a rapidly evolving area of law.

### Manufacturing and safety

- **3D print safety disclaimer** (on all 3D print product/canvas pages, order confirmation, and ToS): *"This item is decorative only. Not food-safe, not load-bearing, not suitable for safety-critical applications. Keep away from small children."*
- **Lead time disclosure** on product pages and checkout: *"Handcrafted to order — typically [X–Y] business days."* Exact timeframe: **TBD at launch** based on production capacity.
- **Manufacturing asset provenance:** The accepted 3D asset (PNG for stickers, STL for 3D prints) is stored in MinIO linked to the order ID. This is the canonical manufacturing file. It cannot be altered after checkout.

### Fulfillment routing workflow

*(Self-hosted, operator-driven at launch)*

1. Order placed → Discord notification to `#orders`.
2. Operator reviews order. Assesses: can fulfill in-house?
3. **In-house:** Operator retrieves manufacturing asset from MinIO (presigned URL), produces item, ships, enters tracking number in DB → shipping email sent automatically.
4. **Sticker overflow → Printful/Printify:**
   - Operator clicks "Route to Printful" in a simple operator CLI tool (or psql command).
   - Go service calls Printful API: `POST /orders` with product + design file URL (presigned MinIO URL).
   - Order status updated to "routed_printful". Discord notification sent.
   - Printful ships directly to customer. Tracking number from Printful → operator enters in DB → shipping email sent.
5. **3D print overflow → Shapeways/Hubs:**
   - Same workflow; calls Shapeways API with STL file.
6. **Future automation:** When order volume grows, add a rule-based router (e.g., "if in-house queue > 5 orders → auto-route stickers to Printful"). Not at launch.

---

## User-surfaced considerations

*(Nothing additional surfaced by the user during Phase 5.)*

---

## Explicitly out of scope

| Item | Decision |
|------|---------|
| GDPR compliance | US-only users; GDPR does not apply at launch. Revisit if EU customers emerge. |
| CCPA full compliance | Below revenue threshold at launch. "Do Not Sell" link added as good practice. |
| Marketing email / newsletter | Non-goal per Phase 1. |
| Reviews and ratings system | Deferred post-launch. Add after ~20 orders minimum. |
| Referral / loyalty program | Non-goal per Phase 1. |
| Subscriptions / recurring orders | Non-goal per Phase 1. |
| Social media accounts | No social at launch. Creating accounts is recommended but deferred. |
| International shipping | US only at launch. |
| Gift cards / store credit | Not in scope. |
| Site search | Not in scope at launch. Add if catalog exceeds 100+ items. |
| A/B testing infrastructure | Not needed at this scale. |
| Conversion pixels (Meta, Google Ads) | No paid advertising at launch. Add with consent management if advertising begins. |
| Abandoned cart SMS | Email only. SMS adds Twilio cost and ops; deferred. |
| Customer support system | Non-goal. Email contact only (`hello@uhhcraft.uhstray.io`). |
| Wishlist | Out of scope. |
| Address autocomplete | Deferred. US-only at launch, simple address form is sufficient. |
| Shippo carrier-calculated rates | Deferred. Flat rate + free-over-$50 at launch. |
| Load testing | Pre-launch load test deferred until before a marketing push. |
| Penetration testing | Deferred. Recommended before first significant marketing push. |
| HSTS preload submission | After first stable deploy (requires 120 days of HSTS delivery before submission). |
| Dynamic DNS setup | Must resolve (OQ-NEW-7) before launch if static IP unavailable. |

---

## Open questions (pre-build — all others resolved)

| # | Question | Status |
|---|----------|--------|
| OQ-NEW-7 | Static public IP vs. dynamic DNS — confirm with ISP before build | ⚠ Must resolve |
| OQ-NEW-9 | Printful vs. Printify existing account — which to set up first? | ⚠ Must resolve |
| OQ-NEW-10 | Shapeways vs. Hubs (Protolabs) for 3D print overflow — any preference? | ⚠ Must resolve |
| OQ-STYLE-1 | Fox mascot final illustration / file | ⚠ Must resolve |
| OQ-STYLE-2 | Logo wordmark — Nunito or custom lettering? | ⚠ Must resolve |
| OQ-NEW-1 | Exact material + cut-type option labels (research: PLA variants, vinyl types, cut types) | ⚠ Must resolve |
| SHIPPING | Flat rate amount below $50 threshold — confirm based on real shipping quotes | Resolve at launch |
| LEAD TIME | Manufacturing lead time — set based on actual production capacity | Resolve at launch |
| SOCIAL | Social media accounts — create for discoverability? | Post-launch decision |
| LEGAL REVIEW | Lawyer review of ToS (especially IP/copyright clause) | Strongly recommended before scale |
