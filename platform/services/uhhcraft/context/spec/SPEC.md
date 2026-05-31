# Site Specification — UhhCraft

> Source artifacts: spec/intake.md, spec/purpose.md, spec/template.md,
> spec/tooling.md, spec/style.md, spec/considerations.md
>
> Assembled: 2026-05-22
> Approved by user on: 2026-05-22 — Jacob

---

## Executive Summary

UhhCraft is a self-hosted e-commerce shop where anyone can buy unique, one-of-a-kind physical goods — primarily stickers and 3D-printed items — by browsing a curated catalog or generating a custom design from a text prompt using AI, with every item previewed in an interactive 3D canvas before purchase. The entire stack runs on a self-hosted local network: Go + templ + HTMX delivers a server-rendered multi-page app backed by Postgres, Redis, and MinIO, while RTX 5070 AI machines on the same network run Flux.1 via ComfyUI (sticker image generation) and Hunyuan3D (3D mesh generation) at zero per-generation cost. The visual identity is clean, cute, and warm — Nunito variable typography, warm orange (#E8732A) and soft blue (#5BBED6) on warm-neutral backgrounds, generous border radius, subtle motion, and full dark mode (the 3D canvas always renders on a dark background to maximize depth and visual impact). Both guest and account checkout are supported; account holders receive priority manufacturing, loyalty discounts, and saved design history; sticker and 3D print overflow is automatically routed to Printful and Shapeways respectively via API. The first-sale target is ~2026-08-22; six pre-build items remain open (fox mascot asset, logo wordmark, material/cut-type option research, static IP confirmation, third-party fulfillment API accounts, and flat-rate shipping amount).

---

## Purpose

### One-line summary

UhhCraft is an online shop where customers can buy unique, one-of-a-kind physical goods (primarily stickers and 3D-printed items) designed using AI-generation tools, with the option to pick from a curated catalog or generate something from scratch using AI, with every item previewed in an interactive 3D canvas before purchase.

### Primary goal

**Sell** — transact for physical goods directly to consumers.

### Secondary goals

- **Showcase** — demonstrate the capability of AI generation and 3D printing to inspire purchase intent. Visitors who are not ready to buy today should leave wanting to come back.

### Archetype(s)

- **Primary:** E-commerce storefront
- **Secondary:** Generative Custom Goods Configurator — the AI generation + 3D canvas viewer is the product experience, not a feature on top of a store. Every item flows through the same 3D canvas view before add-to-cart, regardless of whether it came from the catalog or was generated fresh.

### Audience

#### Persona 1 — Gift Shopper (primary)

- **Role:** Someone buying a unique, personalised item for another person.
- **Demographics:** Any age, any location in the US. No technical expertise assumed.
- **Expertise:** Zero tech knowledge required. Self-evident flows only.
- **Languages:** English.
- **Journey stage:** First-time visitor or occasional returner.

#### Persona 2 — Custom Creator (secondary)

- **Role:** Someone who wants a specific custom item for themselves.
- **Expertise:** Zero tech knowledge required — same bar as Persona 1.
- **Journey stage:** Arrives with a goal; needs the generation flow to be frictionless.

> **Design principle:** If a visitor needs to read instructions, the UX has failed.

### Success metrics

- **First sale by ~2026-08-22** — hard target (3 months from project start).
- **Site technically complete well before month 3** — build done in weeks; time left for iteration.
- **Conversion signal:** A visitor who reaches the 3D canvas should understand what they're looking at and want it, without explanation.

### Scope and lifespan

- **Pages:** Medium (10–50 templates).
- **Content:** Dynamic + personalized — catalog items are managed content; generated items are unique per session; orders are per-user.
- **Lifespan:** Long-lived (years+) — ongoing business.
- **Change frequency:** Rarely (catalog updated as new items are added; site structure changes rarely).
- **Tenancy:** Single-tenant.

### Constraints

- **Timeline:** Site complete in weeks; first sale by ~2026-08-22.
- **Brand assets:** Light blue + orange palette; fox-themed mascot (minimalistic flat head illustration). No logo file or brand book yet.
- **Infrastructure:** Postgres required; self-hosted US local network; GitHub Actions CI/CD; `uhhcraft.uhstray.io` subdomain.
- **Regulatory:** PCI DSS SAQ A (Stripe); COPPA 13+ gate; CCPA "Do Not Sell" link.
- **Team capacity:** Hands-off post-launch; zero on-call; Discord webhook for payment alerts.

### Non-goals

- No community, forum, or social features.
- No customer support system.
- No subscription or recurring-purchase model.
- No digital-only downloads.
- No reseller or wholesale program.
- No tutorial or educational content about AI or 3D printing.
- No user-generated content beyond the generation flow itself.
- No internationalisation at launch (English only, US only).

---

## Template

### Sitemap

```text
/                                    Home
/catalog                             Catalog browse (all items)
/catalog/[category]                  Category listing
/catalog/[slug]                      Catalog item entry → routes to canvas
/generate                            Custom generation entry
/canvas/[id]                         3D Canvas — unified product view

/cart                                Cart
/checkout                            Checkout (multi-step)
/order/[order-id]                    Order confirmation + status

/account/sign-in                     Sign in
/account/sign-up                     Sign up
/account/forgot-password             Password reset
/account                             Account dashboard
/account/orders                      Order history
/account/orders/[order-id]           Order detail
/account/designs                     Saved generations (last 10)

/about                               About + brand showcase
/legal/terms                         Terms of service
/legal/privacy                       Privacy policy
/legal/returns                       Returns and refund policy
/legal/accessibility                 Accessibility statement

/404                                 Not found
/500                                 Server error
```

### Core flows

**Custom generation flow:**
`/generate` → (product type + prompt + material selection) → River job queued → AI generates → `/canvas/[generation-id]` → user accepts → Add to Cart → `/checkout` → `/order/[id]`

**Catalog flow:**
`/catalog` → browse → `/catalog/[slug]` → (material/finish selection) → `/canvas/catalog-[slug]?material=X&cut=Y` → Add to Cart → `/checkout` → `/order/[id]`

Both flows converge at the same `/canvas/[id]` page. The experience is identical from the canvas onward.

### Key page: 3D Canvas (`/canvas/[id]`)

The central product experience. Contains:
- **3D Viewport** — Three.js island; rotate (drag/swipe), zoom (scroll/pinch); slow auto-rotation on load; touch-optimised. Always rendered on a dark background regardless of global theme.
- **Item summary bar** — name, material/finish, price.
- **Action bar** — "Add to Cart" (primary CTA, dark text on orange), "Try Again / New Prompt" (generated items), "Back to Catalog" (catalog items).
- **Cooldown indicator** — countdown timer when rate-limited. Guest nudge: "Account holders regenerate much faster."
- **Generation loading state** — full-viewport animated skeleton while AI generates.

### Key page: Generate (`/generate`)

- Product type toggle (Sticker / 3D Print) — determines which material options appear.
- Prompt textarea (large; example placeholder in italics).
- Material radial selector (contextual: vinyl/gloss/reflective for stickers; PLA/PETG/etc. for prints).
- Cut/finish radial selector (contextual: die-cut/kiss-cut for stickers; matte/glossy for prints).
- Generate button → disabled during cooldown; replaced by countdown timer.
- Generation history panel (account users: last 10 generations as thumbnails).

### Global elements

**Header:** Logo (fox mascot + "UhhCraft" wordmark) + primary nav (Catalog | Create | About) + cart icon (badge) + account icon. Sticky. Transparent over hero, solid elsewhere. Hamburger drawer on mobile.

**Footer:** Secondary nav, legal links, "Do Not Sell My Personal Information" link, copyright.

**Persistent UI:** Cookie notice (minimal — Umami uses no cookies; Stripe cookies are essential). No chat widget. No newsletter signup.

### Navigation patterns

- **Desktop:** Horizontal sticky nav + dropdown user menu.
- **Mobile:** Hamburger → off-canvas drawer. Cart icon always visible.
- **Breadcrumbs:** On catalog sub-pages and account sub-pages.

### Authentication and access

| Page / feature | Access |
|----------------|--------|
| Browse, Home, About, Legal | Public |
| Generate, Canvas, Catalog | Public (rate-limited for guests) |
| Cart, Checkout | Public (guest checkout) |
| Order confirmation | Public (via order ID) |
| Account pages | Authenticated |
| Faster generation rate | Authenticated |
| Priority manufacturing queue | Authenticated |
| Loyalty discount eligibility | Authenticated, qualifying order |
| Admin role | `admin` flag — bypasses rate limiting and order charges |

### Personalization and state

| Data | Guest | Account holder |
|------|-------|----------------|
| Cart | Session cookie | Account-linked, cross-device |
| Generation rate limit | Slower cooldown | Faster cooldown |
| Generation history | Not saved | Last 10 in DB |
| Order history | Email receipt only | Account orders page |
| Loyalty discount | Not available | Auto-applied when eligible |
| Priority manufacturing | Not available | Applied at order creation |

### Content authoring

Catalog items managed directly in Postgres by the operator team. No CMS. Legal and About copy hardcoded in templ templates, updated via code deploy.

### Transactional emails

Order confirmation, account welcome, email verification, password reset, order status updates (manufacturing / shipped), abandoned cart (2-hour delay, one email). All via Resend.

### Localization

English only. Explicitly decided. No i18n at launch.

---

## Tooling

### Rendering strategy

**Server-rendered MPA.** Go renders full HTML per request via templ. HTMX swaps partials for interactive behaviors. Three.js 3D viewer is the sole JS island (loaded only on `/canvas/[id]`). No client-side routing.

### Stack at a glance

| Category | Choice | Rationale |
|----------|--------|-----------|
| Language | Go 1.23+ | In team skills; single binary; backend-first |
| Templating | templ | Compiled, type-safe Go components |
| Interactivity | HTMX | Server-returns-HTML partials; no SPA needed |
| 3D canvas | Three.js | JS island on canvas page only; orbit controls |
| CSS | Tailwind CSS v4 standalone CLI | Utility-first; no Node on server |
| Router | Chi | Lightweight, idiomatic Go |
| Database | PostgreSQL 16 (self-hosted) | Required per intake; pgx + sqlc |
| Cache / rate limiting | Redis 7 (self-hosted) | Cooldown counters + session store |
| Object storage | MinIO (self-hosted) | S3-compatible; zero cost; generated assets |
| Job queue | River | Postgres-backed; async AI generation jobs |
| Auth | Self-built (scs + bcrypt) | OSS; simple auth shape; 3 roles |
| Payments | Stripe (Payment Element) | SAQ A PCI scope; Stripe Tax |
| Email | Resend | Simple API; free tier; transactional |
| AI — images | Flux.1 Schnell via ComfyUI | Self-hosted on RTX 5070; zero per-gen cost |
| AI — 3D models | Hunyuan3D via FastAPI | Self-hosted on RTX 5070; best open-source 3D |
| Reverse proxy | Caddy | Automatic TLS + HSTS; simple config |
| Deploy | Kamal | Zero-downtime SSH deploys |
| CI/CD | GitHub Actions | Confirmed in intake |
| Monitoring | Better Stack + Sentry | Uptime + error tracking |
| Analytics | Umami (self-hosted) | Privacy-respecting; no cookies; OSS |
| Fulfillment (sticker overflow) | Printful API + Printify API | REST API; sticker POD |
| Fulfillment (3D print overflow) | Shapeways API | Accepts STL; ships custom 3D prints |
| Notifications | Discord webhooks | Payment + fulfillment + ops alerts |

### Infrastructure diagram

```text
                        ┌─────────────────────────────────────────────┐
                        │             LOCAL NETWORK                   │
                        │                                             │
  Internet              │  ┌──────────────────┐                      │
  ──────────────────►   │  │   MAIN SERVER     │                      │
  uhhcraft.uhstray.io   │  │                  │                      │
  (public static IP)    │  │  Caddy (TLS)     │◄──── HTTP (local) ───┤
                        │  │  Go binary       │                      │
                        │  │  PostgreSQL 16   │  ┌────────────────┐  │
                        │  │  Redis 7         │  │  AI MACHINE(S) │  │
                        │  │  MinIO           │  │                │  │
                        │  │  Umami           │  │  ComfyUI       │  │
                        │  └────────┬─────────┘  │  + Flux.1      │  │
                        │           │             │                │  │
                        │           │             │  Hunyuan3D     │  │
                        │           │             │  FastAPI       │  │
                        │           │             └────────────────┘  │
                        └───────────┼─────────────────────────────────┘
                                    │
                    ┌───────────────┴──────────────────┐
                    │         EXTERNAL SERVICES         │
                    │  Stripe · Resend · Discord        │
                    │  Printful / Printify (stickers)   │
                    │  Shapeways / Hubs (3D prints)     │
                    │  Better Stack · Sentry            │
                    │  GitHub Actions (CI/CD)           │
                    └───────────────────────────────────┘
```

### AI pipeline (self-hosted, RTX 5070 machines)

**Stickers:** Text prompt → ComfyUI (Flux.1 Schnell quantized, ~2–6s) → PNG with alpha channel → MinIO → Three.js applies as texture to 3D sticker mockup mesh. Manufacturing asset = source PNG + cut line.

**3D prints:** Text prompt → Hunyuan3D FastAPI (lite variant, ~15–60s) → GLB (Three.js preview) + STL (manufacturing) → MinIO. Manufacturing asset = STL file.

**Catalog items:** 3D assets pre-exist in MinIO. Canvas loads them directly; no generation needed. Material selection adjusts rendering/price.

### Auth roles

- `guest` — unauthenticated session; rate-limited generation; session cart; no order history.
- `user` — account holder; faster generation rate; saved designs (last 10); order history; priority manufacturing; loyalty discounts.
- `admin` — operator bypass; no rate limiting; no order charges (testing).

### Deployment

Kamal deploys from GitHub Actions via SSH. Caddy handles TLS. systemd manages all services. AI machines updated separately via SSH/Ansible. Rollback: `kamal rollback` (~30 seconds).

### CI/CD pipeline

```text
Push / PR
  ├── gofmt + golangci-lint (includes gosec)
  ├── templ generate (verify no uncommitted changes)
  ├── go test ./... (unit + integration via testcontainers)
  ├── Playwright E2E (on main branch)
  ├── axe-core accessibility checks
  └── Lighthouse CI (Performance ≥ 90 on home/catalog; ≥ 75 on canvas)

Merge to main → Kamal deploy → /health smoke check → Better Stack confirms
```

### Uptime target

**99.5%** — honest ceiling for self-hosted local network without a dedicated ops team. Migration path to Fly.io documented if higher uptime becomes required post-launch.

---

## Style

### Brand

- **Adjectives (desired):** Clean, Cute, Warm.
- **Adjectives (avoid):** Sharp, Robotic, AI-Generated.
- **Brand tension:** Site sells AI-generated goods but must feel handcrafted and curated. Every visual decision reinforces human warmth over algorithmic feel.
- **Fox mascot:** Minimalistic flat fox head illustration — simple geometric shapes, warm orange/rust, no gradients, consistent across all uses (header logo, favicon, empty states, 404, loading states, OG image).

### Color palette

#### Brand orange (primary)

| Token | OKLCH | Approx hex | Use |
|-------|-------|-----------|-----|
| orange-50 | `oklch(97% 0.02 50)` | `#FFF6EF` | Tint backgrounds |
| orange-500 | `oklch(64% 0.17 47)` | `#E8732A` | **Primary CTA** |
| orange-600 | `oklch(57% 0.16 45)` | `#C85D1C` | Hover on CTA |
| orange-700 | `oklch(49% 0.14 43)` | `#A54A13` | Text on orange tints |

#### Brand blue (accent)

| Token | OKLCH | Approx hex | Use |
|-------|-------|-----------|-----|
| blue-400 | `oklch(72% 0.12 210)` | `#5BBED6` | **Secondary accent** |
| blue-600 | `oklch(55% 0.10 210)` | `#2A8BA4` | Text on blue tints |

#### Neutrals (warm-tinted)

| Token | OKLCH | Approx hex | Use |
|-------|-------|-----------|-----|
| neutral-50 | `oklch(98.5% 0.004 60)` | `#FAFAF7` | Page background (light) |
| neutral-100 | `oklch(96% 0.006 60)` | `#F5F4F0` | Card surface |
| neutral-900 | `oklch(17% 0.007 60)` | `#201E1A` | Default text |

#### Dark mode backgrounds (warm near-black)

| Token | OKLCH | Approx hex | Use |
|-------|-------|-----------|-----|
| dark-page | `oklch(14% 0.008 50)` | `#1A1714` | Page background |
| dark-surface | `oklch(18% 0.008 50)` | `#231F1B` | Cards |
| dark-raised | `oklch(22% 0.008 50)` | `#2C2723` | Elevated surfaces |

#### Critical color rule

**Never white text on orange-500 buttons** — contrast ratio is only 2.84:1 (fails AA). Always use `fg.default` (#201E1A) on orange. This produces a warm chocolate-on-orange effect and passes AA at 5.96:1.

### Semantic tokens

**Light mode key tokens:**
- `color.bg.page`: `#FAFAF7` | `color.fg.default`: `#201E1A` | `color.brand.solid`: `#E8732A` | `color.focus.ring`: `#E8732A`

**Dark mode key tokens:**
- `color.bg.page`: `#1A1714` | `color.fg.default`: `#F0EFEC` | `color.brand.solid`: `#F28640` | `color.focus.ring`: `#F28640`

### Typography

- **Font:** Nunito (Variable) — self-hosted WOFF2, Latin subset, `font-display: swap`.
- **Rationale:** Rounded terminals match the fox mascot's soft geometry and reinforce "Cute, Warm."
- **Alternatives (if Nunito reads too soft):** Outfit, Figtree, Plus Jakarta Sans.
- **Mono:** System mono stack (order numbers, codes).
- **Preload:** Regular (400) and SemiBold (600) weights.
- **No Google Fonts CDN** — self-hosted avoids third-party IP logging.

### Type scale (1.2 ratio from 16px)

| Token | Size | Line height | Weight | Use |
|-------|------|-------------|--------|-----|
| text.xs | 12px | 1.5 | 400 | Labels, badges |
| text.sm | 14px | 1.5 | 400 | Secondary UI text |
| text.base | 16px | 1.6 | 400 | Body copy |
| text.lg | 18px | 1.55 | 500 | Lead text |
| text.xl | 20px | 1.4 | 600 | Small headings |
| text.2xl | 24px | 1.3 | 700 | H3 |
| text.3xl | 30px | 1.25 | 700 | H2 |
| text.4xl | 36px | 1.2 | 800 | H1 |
| text.5xl | 48px | 1.1 | 800 | Hero heading |

### Shape

| Token | Value | Used for |
|-------|-------|----------|
| radius.sm | 6px | Badges, chips |
| radius.md | 10px | Buttons, inputs |
| radius.lg | 16px | Cards, product cards |
| radius.xl | 24px | Panels, modals |
| radius.2xl | 32px | Canvas container |
| radius.full | 9999px | Pills, avatars |

No sharp (0px) corners on any visible interactive or container elements.

### Motion

- **Personality:** Subtle — micro-interactions only. Nothing moves unless it earns its place.
- **Durations:** fast 120ms / normal 200ms / slow 350ms.
- **Easing:** `cubic-bezier(0, 0, 0.2, 1)` (ease.out) for entrances.
- **What moves:** Card hover lift + shadow, button hover/press scale, fade-in on scroll (Intersection Observer), drawer/modal open, HTMX swap fade.
- **What doesn't move:** Page navigation, text, prices, form labels.
- **`prefers-reduced-motion`:** All transitions disabled. Three.js auto-rotation paused.

### Theme modes

- **Light:** Default.
- **Dark:** System preference on first visit; user-toggleable; persisted in localStorage; toggled via `dark` class on `<html>`.
- **Tailwind:** `darkMode: 'class'` strategy.
- **3D canvas:** Always dark-background regardless of global theme (isolated override).

### Radial selectors (custom component)

Material and cut/finish options displayed as pill-shaped radio groups (not native radio inputs). Selected state: `brand.bg` fill + `brand.solid` 2px border + `brand.fg` text. Unselected hover: `bg.muted`.

### Tone of voice

- **Person:** Second person ("you", "your").
- **Formality:** Conversational. Contractions always ("you're", "we'll").
- **Humor:** Gentle, occasional — carried by the fox mascot in empty/loading states.
- **Jargon:** Avoided. "PLA" → "Strong plastic." "Die-cut" → "Cut to shape."
- **Microcopy:** Friendly + actionable. Errors explain what to do next. Empty states invite action. Success is specific and warm.
- **Capitalization:** Sentence case everywhere. ALL CAPS only for micro-labels (status badges).
- **Buttons:** Verbs. "Create Something" not "Creation." "See in 3D" not "3D View."

---

## Considerations

### Accessibility

- **Target:** WCAG 2.1 AA.
- **ADA applies** — US e-commerce; checkout flows are a litigation focus. Prioritize accessible checkout, form inputs, and canvas action bar.
- **Automated CI:** axe-core via Playwright on every merge to main. Zero violations required.
- **No manual screen reader testing** at launch (team capacity). Axe-core provides the automated AA layer.
- **Accessibility statement** at `/legal/accessibility` with a reporting email.

### SEO

- **Title template:** `{Page Name} | UhhCraft`
- **Structured data:** `Product` JSON-LD on catalog item/canvas pages; `Organization` on home; `BreadcrumbList` on catalog.
- **sitemap.xml:** Generated at deploy. Excludes `/account/*`, `/cart`, `/checkout`, `/canvas/*`, `/order/*`.
- **robots.txt:** Disallows same exclusion list.
- **Canvas pages for generated items:** `noindex` (unique per-session URLs).
- **Search Console:** Register at launch. Submit sitemap.

### Performance budgets

| Metric | Target |
|--------|--------|
| LCP | < 1.0s (user-specified) |
| INP | < 200ms |
| CLS | < 0.1 |
| TTFB | < 400ms |
| JS bundle (non-canvas) | < 50KB |
| JS bundle (canvas) | < 700KB |
| Image weight per page | < 500KB |

Lighthouse CI enforces Performance ≥ 90 on home/catalog; ≥ 75 on canvas (Three.js accepted).

Images: AVIF primary, WebP fallback. Converted at upload. `srcset` + `sizes`. `loading="lazy"` below fold. Explicit dimensions.

Brotli compression via Caddy. Static assets: `Cache-Control: immutable`. HTML: `no-store`.

### Privacy and legal

- **Privacy policy, Terms of Service, Returns policy** must be live before launch.
- **No GDPR** (US-only users). CCPA likely below threshold at launch; "Do Not Sell" link in footer as good practice.
- **PCI DSS SAQ A** confirmed via Stripe Payment Element (UhhCraft never touches raw card data).
- **COPPA:** 13+ self-certification checkbox at sign-up and guest checkout step 1.
- **Cookie notice:** Minimal persistent banner (Umami = no cookies; Stripe = essential). No consent management platform needed at launch.
- **Data retention:** Orders 7 years; accounts duration + 90 days; generation history last 10 rolling; logs 30 days.
- **DSAR:** Email-based, 30-day response commitment.
- **IP/copyright in ToS:** Ambiguity acknowledged; no claims on either side; UhhCraft retains production license only. **Lawyer review strongly recommended before scaling.**

### Security

- **HTTPS/HSTS** via Caddy. HSTS preload submitted after 120 days of stable delivery.
- **Security headers:** CSP (strict; Stripe iframe allowed), `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: camera=(), microphone=()`.
- **Auth:** bcrypt cost 12; login rate limit (5/15min per IP via Redis); session rotation on login; `HttpOnly Secure SameSite=Lax` cookies.
- **CSRF:** SameSite=Lax cookies; POST-only mutations.
- **XSS:** templ auto-escapes all output; CSP restricts inline scripts.
- **SQLi:** sqlc generates parameterized queries only.
- **Content moderation:** Server-side keyword blocklist pre-screens all prompts before AI. Blocklist in DB table, operator-updatable. Blocked attempts logged 90 days (prompt text, no PII).
- **Stripe webhook:** Signature validation (`Stripe-Signature` header) on all webhook endpoints.
- **Secrets:** Env vars on server (`/etc/uhhcraft/env`); GitHub Actions secrets for CI; gitignored `.env` locally.
- **Dependabot:** Weekly Go + Python updates. Auto-merge patches; manual review for minor/major.
- **golangci-lint with gosec** in CI.
- **Vulnerability disclosure:** `security@uhhcraft.uhstray.io` in `/.well-known/security.txt`.

### E-commerce — business rules

#### Pricing and discounts

| Trigger | Benefit |
|---------|---------|
| Order ≥ $30 (account holder) | 5% off next order (auto-applied) |
| Order ≥ $100 (account holder) | 10% off next order (auto-applied, doesn't stack with 5%) |

Discounts stored as `next_order_discount_pct` on the account record. Cleared after application.

#### Shipping

- **Free shipping:** Orders ≥ $50 (pre-tax).
- **Below threshold:** Real-time USPS rate quote via USPS v3 API — fetched at checkout based on destination ZIP, weight, and dimensions. Config at `output/config/usps.toml`.
- **Manufacturing lead time:** 5–7 business days. Stated on product/canvas pages and at checkout.
- US only at launch. No international.

#### Returns and refunds

- **Cancellation window:** 24 hours from order placement, or before manufacturing starts (whichever is first).
- **Once manufacturing begins:** No returns or refunds (custom-made goods).
- **Defective/damaged:** Full replacement at no cost. Customer reports within 14 days with photo.
- **Refund processing:** 5–10 business days to original payment method.

#### Cart and abandoned cart

- Generated item **30-minute cart reservation** — asset locked on "Add to Cart"; released on expiry or abandonment.
- **Abandoned cart email:** 2-hour delay, one email, warm tone. Sent to guests (if email collected at checkout step 1) and account holders.

#### Order email lifecycle

Order placed → Manufacturing started → Shipped (with tracking) → [Abandoned cart recovery].

#### Fulfillment routing

1. All orders → Discord `#orders` notification.
2. Operator reviews → routes in-house or to third party.
3. **Sticker overflow:** Printful API (primary) or Printify API (fallback) — `POST /orders` with PNG asset URL.
4. **3D print overflow:** Shapeways API — upload STL, set material, create order.
5. All routing events → Discord notification.
6. Future: rule-based auto-routing when queue depth exceeds threshold.

#### Content moderation

Keyword blocklist pre-screens prompts server-side. Categories: top-100 trademarked character names, hate symbols, slurs, NSFW terms, political figures. Friendly rejection message for blocked prompts. Model-level safety filters as second layer. No post-generation review at launch.

#### 3D print safety

Disclaimer on all 3D print pages and order confirmation: *"Decorative use only. Not food-safe, not load-bearing, not suitable for safety-critical applications. Keep away from small children."*

#### Manufacturing lead time

Placeholder: "Handcrafted to order — typically [X–Y] business days." **TBD at launch** based on actual production capacity.

### Analytics — event taxonomy

| Event | Trigger |
|-------|---------|
| `generate_started` | Generate button click |
| `generate_completed` | AI returns result |
| `generate_rejected` | "Try Again" clicked |
| `canvas_accepted` | "Add to Cart" on canvas |
| `catalog_item_viewed` | Canvas load (catalog flow) |
| `add_to_cart` | Cart addition confirmed |
| `checkout_started` | Checkout page load |
| `checkout_completed` | Order confirmation page load |
| `account_created` | Sign-up completed |
| `third_party_routed` | Order sent to Printful/Shapeways |

Key funnels: generate → canvas → cart → checkout; catalog → canvas → cart → checkout.

### Deployment and backups

- **Domain:** `uhhcraft.uhstray.io` A record → public static IP (confirm static IP or set up ddclient + Cloudflare DNS before launch).
- **Postgres backup:** pg_dump daily at 02:00, stored in MinIO `backups/postgres/`. Retain 30 days.
- **MinIO backup:** `mc mirror` weekly to external drive or Backblaze B2. Retain 4 weeks.
- **DR:** RPO ~24h, RTO ~4h.
- **UPS** recommended on main server.
- **Uptime:** 99.5% target. Better Stack checks every 60s; alerts to Discord + email if down > 2 min.
- **Status page:** `status.uhhcraft.uhstray.io` via Better Stack.

### Testing

- **Unit:** `go test`, ~60% coverage on business logic.
- **Integration:** testcontainers-go (real Postgres + Redis). Covers: order creation, auth, discount logic, fulfillment routing, generation queue.
- **E2E (Playwright):** Full generate flow, full catalog flow, guest checkout, account checkout, password reset, 404.
- **Accessibility:** axe-core in Playwright on key pages. Zero AA violations required.
- **Performance:** Lighthouse CI on every main merge.
- **Manual QA before launch:** Full purchase flow (guest + account), Stripe test mode, email delivery, Discord webhook, mobile browser, dark mode, reduced motion.

### Documentation (required before launch)

- `README.md` — setup, build, deploy, local dev.
- `RUNBOOK.md` — restart services, rollback, restore backup, add catalog items, route orders.
- Architecture diagram (from Tooling section above).
- Content authoring guide — psql commands for catalog management.
- AI service management guide — restart ComfyUI/Hunyuan3D, update models, manage blocklist.

### Launch checklist

- [ ] Static IP confirmed or dynamic DNS configured
- [ ] DNS A record set and propagated
- [ ] TLS certificate issued (Caddy)
- [ ] All legal pages live and reviewed
- [ ] Minimum 10 catalog items loaded
- [ ] Material/cut-type options finalized
- [ ] Fox mascot + logo wordmark assets ready
- [ ] Stripe live mode: keys, Stripe Tax, webhook
- [ ] Discord webhook tested
- [ ] Printful/Printify API tested (sandbox order)
- [ ] Shapeways/Hubs API tested
- [ ] Resend: all email types tested
- [ ] Better Stack monitoring active, status page live
- [ ] Full manual QA checklist complete
- [ ] Postgres backup cron confirmed + restore tested
- [ ] Soft launch: 5–10 friends/family first
- [ ] Day-7 retrospective scheduled

### Explicitly out of scope

GDPR, full CCPA compliance (below threshold), marketing email, reviews system, referral program, subscriptions, social media at launch, international shipping, gift cards, site search, A/B testing, conversion pixels, abandoned cart SMS, customer support system, wishlist, address autocomplete, carrier-calculated shipping, load testing (pre-marketing push), penetration testing (pre-marketing push).

---

## Sign-off

- **User approval date:** 2026-05-22
- **Approved by:** Jacob

### Outstanding pre-build items (must resolve before implementation begins)

| # | Item | Notes |
|---|------|-------|
| 1 | Fox mascot final illustration file | Flat vector/SVG, warm orange/rust fox head |
| 2 | Logo wordmark | "UhhCraft" in Nunito Bold or custom lettering |
| 3 | Material + cut-type option labels | Research: PLA variants, PETG, vinyl types, die-cut vs kiss-cut — final copy for radial selectors |
| 4 | Static IP situation | ✅ Confirmed — static public IP available |
| 5 | Printful vs Printify account | ✅ Printify primary — config at `output/config/printify.toml` |
| 6 | 3D print overflow provider | ✅ Hubs (Protolabs Network) — config at `output/config/fulfillment_3d.toml`. Note: Shapeways filed for bankruptcy 2023; Hubs is the recommended alternative. |
| 7 | Shipping | ✅ USPS API (real-time rate quotes) — replaces flat rate. Config at `output/config/usps.toml`. |
| 8 | Manufacturing lead time | ✅ 5–7 business days |

### Post-launch backlog (deferred, not forgotten)

- Social media accounts (for discoverability)
- Product reviews system (after ~20 orders minimum)
- Lawyer review of ToS IP/copyright clause
- Annual SAQ A self-assessment (PCI DSS)
- Load testing before first significant marketing push
- Penetration testing before first significant marketing push
- HSTS preload submission (after 120 days of stable delivery)
- Shippo integration (carrier-calculated rates)
- Site search (if catalog exceeds ~100 items)
- Staging environment (when team grows)
- Rule-based fulfillment auto-routing (when order volume grows)

### Anticipated next step

Implementation, guided by [`verification.md`](../../../../../agents/websmith/context/verification.md). All 8 pre-build items must be resolved before the first line of production code is written.

---

## Alignment with agent-cloud conventions

> Added 2026-05-25 during integration of UhhCraft into the agent-cloud monorepo.
> These are **updates** to the original spec to align with platform conventions, not deviations.
> Approved by Jacob on integration kickoff (memory: `websmith-integration.md`).

When this spec was authored, UhhCraft was a standalone project. As of 2026-05-25 it lives inside the **agent-cloud** platform monorepo as `platform/services/uhhcraft/`. agent-cloud has opinions about deployment, secrets, networking, and observability that override the corresponding sections of this spec. The original text remains above for historical reference; the items below are the operative versions.

### Container runtime — Podman, not native binary

- **Original spec:** "Production runs these natively on the server — Docker is dev-only" (re: Postgres / Redis / MinIO compose).
- **agent-cloud alignment:** Production runs **Podman-managed containers** for everything — the Go app, Postgres, Redis, and MinIO. Docker is reserved for NetBox per [root CLAUDE.md](../../../../../CLAUDE.md). Local dev still uses `docker compose` or `podman-compose` interchangeably.
- **Rationale:** Consistency with the rest of agent-cloud's services (NocoDB, n8n, Semaphore, OpenBao, …), rootless containers by default, and one deploy.sh shape across the platform.

### Secrets — OpenBao, not `.env` files on the server

- **Original spec:** "All other secrets are set in `/etc/uhhcraft/env` on the production server."
- **agent-cloud alignment:** All secrets live in **OpenBao** under `secret/services/uhhcraft/*`. Ansible's `manage-secrets.yml` task fetches them at deploy time and templates `.env` files from `templates/env.j2`. The `.env` file is a gitignored bridge — never authoritative, overwritten every deploy. No `/etc/uhhcraft/env` on the server.
- **Rationale:** Centralised secret rotation, audit trail, AppRole scoping. Per root [CLAUDE.md "Secrets Management"](../../../../../CLAUDE.md).

### Deployment — Semaphore + Ansible, not Kamal

- **Original spec:** "Deployment uses Kamal."
- **agent-cloud alignment:** Deployment is **Semaphore-orchestrated Ansible** via composable playbooks (`platform/playbooks/deploy-uhhcraft.yml`). Kamal is not used.
- **Rationale:** Single deployment surface across the platform. Per `plan/architecture/AUTOMATION-COMPOSABILITY.md`.

### Reverse proxy — central Caddy, not per-service

- **Original spec:** Per-service Caddyfile on the UhhCraft host.
- **agent-cloud alignment:** UhhCraft is fronted by the **central platform Caddy** (`platform/services/caddy/`). UhhCraft ships a Caddy fragment template (`templates/caddy-site.j2`) that is rendered into the central Caddy's `sites/` directory at deploy time. TLS, DNS-01, and HSTS are handled by central Caddy.
- **Rationale:** One TLS surface, one DNS-01 integration, consistent security headers. Per `plan/architecture/CADDY-REVERSE-PROXY.md`.

### Object storage — separate MinIO instances per service

- **Original spec:** Single MinIO instance shared between UhhCraft and the two AI services.
- **agent-cloud alignment:** **Three independent MinIO instances** — one per service. UhhCraft holds catalog assets in its own MinIO; `inference-comfyui` writes generated images to its own MinIO; `inference-hunyuan3d` writes GLB/STL files to its own MinIO. Caddy proxies asset paths across (`/static/*` → uhhcraft MinIO; `/generated/img/*` → comfyui MinIO; `/generated/3d/*` → hunyuan3d MinIO).
- **Rationale:** Per-service blast radius, simpler credential scoping, GPU host isolation. Documented in [`../architecture/ai-sidecar-contract.md`](../architecture/ai-sidecar-contract.md).

### AI sidecars — separate platform services

- **Original spec:** `ai/image/` and `ai/model3d/` ship as part of UhhCraft.
- **agent-cloud alignment:** AI sidecars are independent platform services: `platform/services/inference-comfyui/` and `platform/services/inference-hunyuan3d/`. Each has its own VM, deploy playbook, OpenBao secrets, and MinIO. UhhCraft consumes them via internal HTTP.
- **Rationale:** Reusable across future generative sites; GPU hosts can be sized independently; deploy/rollback per inference engine without touching UhhCraft.

### CI/CD — unified `lint-and-test.yml`, not service-specific

- **Original spec:** Service-local `ci.yml` (Go + Python jobs).
- **agent-cloud alignment:** UhhCraft's CI is folded into the platform-wide `.github/workflows/lint-and-test.yml` with path filters (Go jobs fire only on `platform/services/uhhcraft/**`). Adds `golangci-lint`, `templ fmt --diff`, `sqlc verify`, `gosec` alongside the existing ruff / shellcheck / ansible-lint / yamllint / hadolint / bandit jobs.
- **Rationale:** Single CI surface. Per `plan/architecture/CI-TESTING-SPECIFICATION.md`. Implementation lands in Phase 8 of [`WEBSMITH-INTEGRATION-PLAN.md`](../../../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md).

### Generated code — generate-in-CI, not committed

- **Original spec (implicit from source):** `_templ.go` files committed to git.
- **agent-cloud alignment:** `*_templ.go` and `internal/db/sqlcdb/*` are **gitignored**. CI runs `templ generate` and `sqlc generate` before building. Contributors run `make templ` after clone.
- **Rationale:** Generated code in git creates merge conflict noise and stale drift. CI gates correctness via `templ generate --diff` and `sqlc verify`.

### Items unaffected by alignment

The following spec sections are unchanged by integration: archetype, audience, success metrics, scope, visual identity, page inventory, content model, payments (Stripe), fulfillment integrations (Printful / Shapeways), shipping (USPS), regulatory posture, content moderation, accessibility targets, the 8 pre-build items, and the post-launch backlog. agent-cloud conventions apply to **how** UhhCraft runs, not **what** UhhCraft does.

### Tracking future deviations

Beyond this integration alignment, any subsequent deviation from the spec must be added below as a new dated entry. Format:

```text
#### <YYYY-MM-DD> — <one-line summary>
- **What changed:** …
- **Why:** …
- **Approved by:** <name>, <date>
```

(none yet.)
