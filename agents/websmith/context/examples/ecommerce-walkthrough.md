# Example Walkthrough — E-commerce Site

A condensed end-to-end transcript of an agent guiding a user through all five phases to build a small e-commerce site. Use as a model for shape and pacing. Real sessions will be longer and more iterative.

> **User**: Adelaide, a ceramicist who hand-throws and glazes pottery; wants to sell internationally; works alone; tech-comfortable but not a developer.

---

## Phase 1 — Purpose

**Agent:** Hi Adelaide. We'll move through five phases — purpose, template, tooling, style, and considerations — before I write any code. In one sentence, what do you want this site to do?

**User:** Sell my pottery directly to people, mostly overseas, and tell the story behind each piece.

**Agent:** Two goals in one sentence — sell, and inform. Which one breaks ties if they conflict?

**User:** Sell. But the story matters; without it people don't pay $200 for a mug.

**Agent:** Got it. The primary archetype is e-commerce storefront, with a strong editorial/story layer on each product. Tell me about your buyers — walk me through one specific person.

**User:** Late 30s to 50s, design-conscious, lives in the US/UK/Australia/Japan, follows me on Instagram. Buys on phone first, often on commute. Doesn't comparison-shop; buys because they connected with a piece.

**Agent:** That gives us a mobile-first design with high-quality imagery and narrative copy. Languages?

**User:** English at launch. Japanese later — meaningful percentage of buyers.

**Agent:** Noted: launch in English, plan for Japanese. Success metrics — in six months, what makes you call this a success?

**User:** $5K/month in direct sales (currently routed through Etsy), 30%+ repeat buyers, average order $250+.

**Agent:** Concrete. Anything I should know about regulations, taxes, or your existing systems?

**User:** I'm a sole proprietor in Australia. GST in Australia. I ship internationally — taxes and customs are a headache.

**Agent:** Australian GST and international tax/customs are real considerations; we'll come back to them in phase 5. Anything you explicitly DON'T want?

**User:** Not a marketplace. No wholesale. No subscription. No interest in a blog separately from product stories.

**Agent:** Last question: anything I haven't asked that matters?

**User:** Pieces are one-of-a-kind. Once it's sold, it's gone. I make ~15 pieces/month. So inventory is always 1 or 0.

**Agent:** That changes a lot — we'll need oversell prevention, and the site is really showcasing a small rotating gallery rather than a catalog. Good catch.

### Resulting `spec/purpose.md` (excerpt)

```markdown
# Purpose

## One-line summary
Sell hand-thrown pottery direct-to-consumer with strong per-piece storytelling; ~15 unique pieces/month, international shipping, English at launch with Japanese on roadmap.

## Primary goal
Sell

## Secondary goals
- Inform (story per piece)

## Archetype(s)
- Primary: E-commerce storefront (small catalog, unique items)
- Secondary: Editorial / story (per product)

## Audience
### Design-conscious collector (35–55)
- Demographics: US, UK, AU, JP; design-conscious; disposable income
- Expertise: novice in pottery, expert as a buyer
- Device mix: mobile-majority (Instagram referrer)
- Languages: English (launch), Japanese (planned)
- Journey stage: warm from Instagram

## Success metrics
- $5K/mo direct sales by month 6
- ≥30% repeat buyer rate by year 1
- AOV ≥ $250

## Scope
- Pages: small (<10 templates), dynamic catalog of ~15-30 active items
- Lifespan: long-lived
- Tenancy: single

## Constraints
- Australian sole proprietor; GST registered
- International shipping mandatory
- Solo operator; minimal ops appetite

## Non-goals
- Marketplace
- Wholesale
- Subscription
- Standalone blog

## Special notes
- Unique inventory: each item is qty 1 (oversell prevention is critical)
- Languages: English launch, Japanese planned
```

User confirms. Move to phase 2.

---

## Phase 2 — Template

**Agent:** With ~15 pieces, a category index plus product detail might be overkill. What if we had a single "shop" page that shows all available work, plus a deep product page per piece?

**User:** Yes — and the shop is the home, basically.

**Agent:** Home = shop. Walk me through what's on the product page.

**User:** Big photos. Multiple angles. A video of it being thrown if I have one. A short story — maybe 200 words. Materials, dimensions, weight. Shipping calc. Add-to-cart, but maybe no cart — just buy now? Each piece is one-off.

**Agent:** For unique inventory, "buy now" can replace cart entirely. Faster checkout, no cart abandonment to manage. Want to skip the cart?

**User:** Yes.

**Agent:** Then the flow is: shop → product → checkout. Three steps. Plus: about, contact, FAQ, shipping/returns policy, privacy/terms, sign-in for past customers to see orders. Anything else?

**User:** I want a "journal" — process posts, kiln-firing photos, studio visits. Not a separate blog — same site.

**Agent:** Journal as a section, then. Index + post. So:

```
/                Home / shop (live inventory)
/work/[slug]     Product detail
/checkout        Single-page checkout
/account         Order history (auth-gated)
/journal         Journal index
/journal/[slug]  Journal post
/about           About Adelaide and the work
/contact         Contact + commissions inquiry
/shipping        Shipping & returns
/policies        Privacy + terms + cookies
/sold/[slug]     Archive of sold pieces (SEO + portfolio)
```

**User:** Archive of sold pieces — yes. People love seeing past work. Make sure it shows what they sold for.

**Agent:** Header on every page: logo, "Shop / Journal / About / Contact" links, basket icon (or maybe a "your piece" indicator after add), account icon, language toggle. Footer: Instagram, newsletter, shipping, returns, privacy. On mobile, hamburger.

**Agent:** Other questions:
- Search? Maybe not, with 15 active pieces.
- Filtering? Probably not.
- Reviews? Maybe later — for one-offs, reviews don't scale.

**User:** No search. No filter. Reviews — testimonials block on the home page instead.

**Agent:** Last call — anything I haven't asked?

**User:** Newsletter signup is important. Instagram converts but I want to own that audience.

**Agent:** Inline on home + on each product page after sold-out + in the footer. Confirmed.

### Resulting `spec/template.md` (key excerpts)

```markdown
# Template

## Sitemap

```
/                   Home — live shop, hero, testimonials, newsletter
/work/[slug]        Product detail
/sold/[slug]        Archived sold piece
/checkout           Single-page checkout (no cart)
/account            Order history (auth-gated)
/journal            Journal index
/journal/[slug]     Journal post
/about              About
/contact            Contact + commissions inquiry
/shipping           Shipping & returns
/policies           Privacy + terms + cookies
```

### Home
- URL: /
- Purpose: First impression + live inventory + testimonials + newsletter
- Major regions:
  - Header: logo, nav, account, lang
  - Hero: full-bleed image of latest piece + CTA "View piece"
  - Available work: grid of available pieces (large card with photo + title + price)
  - "Recently sold" small strip (links to /sold archive)
  - Testimonials: 3 short quotes
  - Newsletter signup
  - Footer
- Components: header, hero, product card, testimonial card, newsletter form, footer
- Empty state: if zero available, show "kiln cooling — next firing on X" with newsletter CTA

### Product detail (/work/[slug])
- Major regions:
  - Header
  - Image gallery (4-8 photos + optional video)
  - Title, dimensions, materials, weight, price
  - "Buy now" (single button — no cart)
  - Story (~200 words)
  - Studio context (small photos: throwing, glazing, kiln)
  - Shipping calc (country selector → quoted rate + delivery window)
  - Newsletter signup (especially if sold out)
  - "More work like this" carousel (live + recently sold)
- States: available, reserved (in checkout — show 5-min hold), sold

## Global elements
- Header: logo, Shop / Journal / About / Contact, account, language toggle, basket-indicator-when-pending
- Footer: Instagram, newsletter, shipping, returns, policies, copyright
- Persistent: cookie banner (consent required for analytics)

## Components
- Product card, hero, image gallery, testimonial card, newsletter form, shipping calc widget, checkout flow, story block

## Localization
- Launch: English
- Planned: Japanese (year 2)
- URL strategy: /ja/ subdirectory when added

## Auth
- Public: home, work, sold, journal, about, contact, shipping, policies
- Authenticated: /account (order history)
- No role gating
```

User confirms. Phase 3.

---

## Phase 3 — Tooling

**Agent:** With ~15 SKUs, mobile-first, story-heavy, international tax/shipping, and you operating solo — Shopify is the obvious default. Saves you months on tax, fraud, payments, inventory.

**User:** I've used Etsy. Shopify is fine. Can I still make the site feel custom?

**Agent:** Yes — two options. (A) Shopify with a custom theme. (B) Headless: Shopify Storefront API + custom frontend (Astro / Next.js). (A) is faster to ship and maintain; (B) is more flexible visually but adds complexity. Given you're solo, I'd recommend A. Custom theme on Shopify gives you 90% of the visual control with 10% of the ops.

**User:** Custom Shopify theme it is.

**Agent:** Theme built from scratch or based on a starter like Dawn / Sense?

**User:** From scratch. I want the typography to be a thing.

**Agent:** OK. Stack:
- Platform: Shopify (Basic plan to start)
- Theme: custom, built with Liquid + light JS, mobile-first
- Payments: Shopify Payments (Stripe-backed; handles GST collection for AU domestic + international tax workflows)
- Shipping: Shopify Shipping with Australia Post + DHL Express for international
- Inventory: Shopify-native, qty = 1 per SKU
- Journal: Shopify Online Store blog (basic but adequate at this scale)
- Email: Shopify transactional + Klaviyo for newsletter and abandoned-checkout
- Analytics: Shopify Analytics + Plausible (privacy-first, lighter than GA)
- Hosting: Shopify-hosted
- CI/CD: Shopify CLI + GitHub Actions for theme deploy on merge
- Dev workflow: theme dev locally; staged via dev store

**Agent:** Why this over alternatives:
- Headless (rejected): too much for solo; reserve the option for year 2 if the site grows.
- WooCommerce (rejected): more flexibility but more ops; WordPress security and hosting headaches.
- BigCommerce (rejected): comparable platform; Shopify ecosystem fits one-off / small-batch better.

**User:** Anything I should worry about?

**Agent:** Two things — (1) Klaviyo is a recurring cost ($20+/mo); we could start with Shopify Email (free up to 10K emails/mo) and switch later. (2) Plausible has a small monthly cost too; if budget matters, start with Shopify Analytics only.

**User:** Start lean. Shopify Email; Shopify Analytics only.

### Resulting `spec/tooling.md` (excerpt)

```markdown
# Tooling

## Rendering strategy
- Primary: Server-rendered Liquid (Shopify-hosted); JS only for image gallery, shipping calc, checkout interactions.
- Rationale: Mobile-first; minimal JS; SEO-friendly out of the box.

## Frontend
- Liquid templates + minimal vanilla JS / Alpine.js for sprinkles.
- No framework. Theme built from scratch.

## Styling
- Vanilla CSS + custom properties; mobile-first; no framework.
- Why: Total control over typography (per the user's brief); small surface area; no build-tooling burden.

## Backend
- None (Shopify-managed).

## Database
- None directly (Shopify-managed).

## Auth
- Shopify customer accounts (passwordless email login).

## Storage / files
- Shopify-managed CDN. Images uploaded via admin; serve via Shopify's image API.

## Email
- Transactional: Shopify (order, shipping, abandoned-checkout).
- Newsletter: Shopify Email at launch; revisit Klaviyo at $10K MRR.

## Search
- Not applicable at this catalog size.

## CMS / content
- Shopify Online Store (products + blog).

## E-commerce
- Platform: Shopify Basic
- Payments: Shopify Payments (AU)
- Tax: Shopify Tax (GST + international destination-based)
- Shipping: Shopify Shipping; Australia Post domestic; DHL Express international.
- Inventory: Shopify-native, max qty=1 per SKU.

## Analytics / monitoring
- Web: Shopify Analytics (built-in).
- Errors: Shopify-managed.
- Uptime: Shopify-managed.

## Hosting / deployment
- Shopify-hosted.
- Region: global edge via Shopify CDN.

## Build tooling
- Shopify CLI for theme development.
- Package manager: npm (theme JS only).
- Version control: GitHub.

## CI/CD
- GitHub Actions:
  - On PR: theme-check lint + dev-store deploy preview.
  - On merge to main: deploy to production theme.
- Rollback: Shopify keeps theme version history.

## Testing
- Manual QA per release.
- Shopify Theme Inspector for performance.
- Lighthouse spot checks.

## Alternatives rejected
- Headless (Hydrogen + Storefront API): too much for solo at this stage.
- WooCommerce / WordPress: ops burden.
- BigCommerce: comparable but Shopify ecosystem stronger for unique-item shops.
```

User confirms. Phase 4.

---

## Phase 4 — Style

**Agent:** Three adjectives for how this should feel?

**User:** Quiet. Handmade. Considered.

**Agent:** Three for what to avoid?

**User:** Trendy. Cheap. Sterile.

**Agent:** Show me 2-3 references you like.

**User:** *(shares links to a few editorial-feeling craft sites and a Japanese tea brand)*

**Agent:** All of them lean on:
- Generous whitespace
- Serif display, sans body
- Off-white backgrounds, not pure white
- Editorial photography — natural light, real textures
- Sparse use of color

I'll pull threads from that into a system. Let me propose:

- **Color**: warm off-white background (#F6F2EC, oklch 95% 0.01 80), dark slate text (oklch 25% 0.01 250), one quiet accent — say a soft kiln-orange (oklch 65% 0.12 50) used very sparingly for hover/focus states.
- **Type**: display = a serif like Tiempos or GT Sectra; body = a clean sans like Inter; mono used only for SKU numbers if at all.
- **Spacing**: generous. Default vertical rhythm 32px between sections on mobile, 64px on desktop.
- **Radius**: 4-6px on buttons/inputs, larger (16px) on images for an editorial feel — or sharp corners on images if we want pure documentary. Your call.
- **Motion**: subtle. Hover lifts on product cards, gentle fade-in on scroll. Reduce-motion respected.
- **Theme modes**: light only. Dark mode doesn't fit the warm-paper feel.
- **Voice**: first-person ("I made this..."), conversational, no jargon.

**User:** Sharp corners on images. Tiempos and Inter. Skip Klaviyo's branded forms.

**Agent:** Done. Contrast audit:
- Text on bg: oklch 25/95 → ratio 11.8:1 (AAA) ✓
- Accent on bg: oklch 65/95 → ratio 3.2:1 → use for icons/borders only; for text uses, darken to oklch 50%.

Anything I haven't asked?

**User:** I want the price to be quiet too. Don't make it loud.

**Agent:** Price treatment will be regular weight, body size, with tabular numerals so they align. Not bold. Not larger.

### Resulting `spec/style.md` (excerpt)

```markdown
# Style

## Brand context
- Adjectives: quiet, handmade, considered
- Avoid: trendy, cheap, sterile
- References: editorial craft sites, Japanese tea brand

## Color
### Palette
- Bg: warm off-white #F6F2EC / oklch(95% 0.01 80)
- Fg: dark slate / oklch(25% 0.01 250)
- Accent: kiln-orange / oklch(65% 0.12 50) — for borders/icons
- Accent (text use): darker kiln / oklch(50% 0.13 50)
- Semantic: minimal — success/danger only for checkout flow

### Semantic tokens (light only)
- color.bg.page: #F6F2EC
- color.bg.surface: #FFFFFF
- color.fg.default: oklch(25% 0.01 250)
- color.fg.muted: oklch(45% 0.01 250)
- color.border.default: oklch(85% 0.01 80)
- color.brand.accent: oklch(65% 0.12 50)

### Contrast audit
- fg.default on bg.page: 11.8:1 (AAA) ✓
- fg.muted on bg.page: 5.7:1 (AA) ✓
- brand.accent on bg.page: 3.2:1 (icons/borders only)

## Typography
- Display: Tiempos Headline (self-hosted)
- Body: Inter (self-hosted, variable)
- Mono: not used
- Loading: preload Tiempos + Inter; font-display: swap; size-adjust matched

### Scale (1.25 ratio from 16px base)
| Token | Size | Line height | Weight |
| text.xs | 13px | 1.4 | 400 |
| text.sm | 14px | 1.5 | 400 |
| text.base | 16px | 1.6 | 400 |
| text.md | 18px | 1.5 | 400 |
| text.lg | 22px | 1.3 | 400 (Tiempos) |
| text.xl | 28px | 1.2 | 400 (Tiempos) |
| text.2xl | 36px | 1.15 | 400 (Tiempos) |
| text.3xl | 48px | 1.1 | 400 (Tiempos) |

Numerals: tabular for prices and SKUs; proportional elsewhere.

## Spacing (multiples of 4)
space.1=4 .2=8 .3=12 .4=16 .5=24 .6=32 .7=48 .8=64 .9=96 .10=128

## Layout
- Max width: 1280px
- Gutter: 24px
- Breakpoints: sm 640, md 768, lg 1024, xl 1280

## Shape
- radius.button: 4px
- radius.image: 0 (sharp)

## Motion
- Personality: subtle
- duration.fast 120ms
- duration.normal 200ms
- ease.out
- Hover lift on product cards (translateY -2px)
- Reduced-motion: replace transforms with opacity

## Theme modes
- Light only

## Voice
- Person: first-person, "I"
- Formality: conversational
- Humor: dry, sparing
- Microcopy: warm, specific. "Your piece is in the kiln" beats "Order processing"
- Capitalization: sentence case
```

User confirms. Phase 5.

---

## Phase 5 — Considerations

**Agent:** Walking the cross-cutting baseline first, then archetype-specific (e-commerce). Several items here are real — especially around tax, accessibility, and operations.

### Accessibility
- WCAG AA target.
- Manual: VoiceOver on iOS (largest traffic), NVDA on desktop, keyboard-only QA per release.
- Automated: axe-core in CI for theme JS pages.
- Statement page added.

### SEO
- Per-product structured data (Product, Offer, schema.org).
- BreadcrumbList on /work/* and /sold/*.
- Archive of sold pieces is huge for long-tail SEO.
- Hreflang prepared for /ja/.
- robots.txt + sitemap.xml auto-generated by Shopify.

### Performance
- LCP < 2.5s on 3G mobile.
- Image strategy: Shopify image API with srcset; AVIF + WebP fallback.
- Lazy-load below-the-fold.
- Self-hosted fonts; size-adjust for swap.

### Privacy
- Privacy + Terms + Cookies pages — Adelaide's Australian lawyer to draft. Standard templates as a start.
- Cookie banner: consent required (EU/UK visitors); essential cookies only at first; analytics gated.
- GDPR / CCPA / Australian Privacy Act apply (international shipping).
- DSAR process: email-driven, manual at this scale.

### Security
- HTTPS / HSTS via Shopify.
- 2FA on admin account (mandatory).
- App permissions audited quarterly.

### Content
- Adelaide writes all product stories before listing.
- Photography: natural light, consistent setup; alt text required per image.
- Journal posts queued before launch.

### Monitoring
- Shopify Analytics for sales.
- Email alerts: low inventory (any sold piece → next firing planning), failed payment, fraud flag.

### Deployment
- Domain: handmade-by-adelaide.com (apex + www → apex).
- DNS: Cloudflare.
- Backups: Shopify-managed.

### CI/CD
- GitHub Actions: theme-check + Lighthouse + axe on PR.
- Deploy on merge.
- Rollback: Shopify theme version history.

### Testing
- Manual checkout test (test card) per release.
- Visual regression: Percy-lite via Chromatic free tier on key templates.
- Day-of-launch test purchase from a separate machine.

### Maintenance
- Adelaide maintains; agent provides:
  - README in theme repo
  - Style guide (one page)
  - "When something goes wrong" runbook (low inventory, failed payment, customer email)
- Theme dependencies updated quarterly.

### Launch readiness
- DNS cut from current Etsy-and-Instagram-only setup to new site.
- Pre-launch: invite 20 newsletter subscribers for soft-open.
- Day 0: monitor Shopify dashboard hourly for 24h.
- Day 7: retrospective + iterate.

### E-commerce specific
- Unique inventory (qty=1) — Shopify natively handles oversell.
- Tax:
  - AU: GST collected automatically by Shopify Tax.
  - International: destination-based; Shopify handles US, EU, UK, CA, NZ, JP rules. Customs / duty disclaimer on product pages and at checkout.
- Shipping:
  - AU domestic: Australia Post regular.
  - International: DHL Express insured.
  - Free shipping threshold: $300 AU; flat international rates posted.
- Returns: 14-day window if unbroken; buyer pays return shipping; refund within 7 days of receipt.
- Order email lifecycle: standard Shopify (placed → confirmed → shipped → delivered) + personal note template from Adelaide.
- Customer accounts: optional (guest checkout default).
- Fraud: Shopify's built-in Radar; manual review for any first-time international order > $500.
- PCI: Shopify handles; no card data ever touches the theme.

### User-surfaced considerations
- Newsletter list ownership — even if Shopify Email holds it, monthly CSV export to Adelaide's archive.
- Photography backups — quarterly to external drive + cloud.

### Out of scope
- Subscription / recurring billing
- Wholesale / B2B
- Marketplace / multi-vendor
- Loyalty points

**Agent:** Last call — anything I haven't asked?

**User:** What about when I'm sick or on holiday and can't ship?

**Agent:** Good — that's a "store status" decision. Options:
1. Manually disable checkout (Shopify password mode).
2. Sitewide banner: "On hiatus until [date] — pre-orders welcome."
3. Per-item flag.

**User:** Banner + still accept orders but slow the shipping promise.

**Agent:** Added. "Storefront pause banner" component + a setting in Shopify metafields. Confirmed.

---

## Unified spec summary

The agent assembles purpose + template + tooling + style + considerations into one document for sign-off. Adelaide reviews. Approves. Implementation begins.

---

## Lessons from this walkthrough

- **The "anything else?" question caught two crucial details** (unique inventory; hiatus mode) that the catalog would not have surfaced.
- **Constraints from phase 1 cascaded.** Solo operator → managed platform → Shopify → custom theme rather than headless.
- **Each phase changed what was possible later.** "Each piece is one-off" (phase 1) → "no cart, just buy-now" (phase 2) → "Shopify-managed inventory is fine" (phase 3).
- **Style decisions were grounded in references and adjectives**, not vibes. Contrast audits and tokens followed.
- **Considerations were partly archetype-driven (e-commerce default checklist), partly user-driven (hiatus mode, newsletter ownership).** Both matter.
