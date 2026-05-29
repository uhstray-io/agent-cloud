# Purpose

> Phase 1 artifact.
> Status: awaiting user approval.

---

## One-line summary

UhhCraft is an online shop where anyone can buy unique, one-of-a-kind physical goods — primarily stickers and 3D-printed items — either by picking from a curated catalog or generating a custom design using AI, with every item previewed in an interactive 3D canvas before purchase.

---

## Primary goal

**Sell** — transact for physical goods directly to consumers.

---

## Secondary goals

- **Showcase** — demonstrate the capability of AI generation and 3D printing to inspire purchase intent. Visitors who are not ready to buy today should leave wanting to come back. The site is a gallery of what's possible as much as it is a shop.

---

## Archetype(s)

- **Primary:** E-commerce storefront
- **Secondary:** Generative Custom Goods Configurator — a hybrid specific to UhhCraft. The AI generation + 3D canvas viewer is not a feature layered on top of a store; it *is* the product experience. Every item — whether picked from the catalog or generated from scratch — flows through the same 3D canvas view before being added to cart. No standard e-commerce archetype covers this.

---

## Audience

### Persona 1 — Gift Shopper (primary)

- **Role:** Someone buying a unique, personalised item for another person (birthday, holiday, just-because gift).
- **Demographics:** Any age, any location in the US. Broad demographic — no technical expertise assumed.
- **Expertise:** Zero tech knowledge required. The site must guide them from "I want something" to "I've ordered it" without any instruction, jargon, or friction.
- **Device mix:** OPEN — resolve in Phase 2 (likely mixed mobile/desktop; gift browsing skews mobile).
- **Languages:** English at launch.
- **Accessibility:** WCAG AA target; no manual screen-reader testing planned but AA compliance covers this persona adequately.
- **Network/device constraints:** OPEN — resolve in Phase 3.
- **Journey stage:** First-time visitor or occasional returner. Not a power user. Arrives via word of mouth, social sharing, or search.

### Persona 2 — Custom Creator (secondary)

- **Role:** Someone who wants a specific item for themselves — a custom sticker of their pet, a 3D-printed object from their own concept.
- **Demographics:** Any age, US. Slightly more intentional than a gift shopper but still no tech knowledge expected.
- **Expertise:** Zero tech knowledge required — same bar as Persona 1.
- **Device mix:** OPEN — resolve in Phase 2.
- **Languages:** English at launch.
- **Accessibility:** Same as Persona 1.
- **Network/device constraints:** OPEN — resolve in Phase 3.
- **Journey stage:** Arrives with a goal; needs the generation flow to be frictionless and the result to delight them.

> **Design principle shared by both personas:** If a visitor needs to read instructions, the UX has failed. Every step — browsing the catalog, entering a prompt, reviewing the 3D canvas, checking out — must be self-evident.

---

## Success metrics

- **First sale within 3 months of launch** — hard target. This is the go/no-go signal.
- **Site and service complete well before month 3** — technical readiness is not the 3-month goal; getting a paying customer is. The build should be done in weeks so there is time to iterate on the purchase flow before the deadline.
- **Conversion signal:** A visitor who reaches the 3D canvas view should understand what they're looking at and why they'd want it, without any explanation. (Qualitative — can be verified via usability observation or early-user feedback.)

---

## Scope and lifespan

- **Pages:** Medium (10–50 range) — home/storefront, catalog browse, generation flow, 3D canvas (product view), cart, checkout, order confirmation, order history (account), about/brand page, legal pages.
- **Content:** Dynamic + personalized — catalog items are managed content; generated items are unique per session; orders are per-user.
- **Lifespan:** Long-lived (years+) — this is an ongoing business, not a campaign.
- **Change frequency:** Catalog updated as new pre-designed items are added; site structure changes rarely.
- **Tenancy:** Single-tenant (one shop, one operator).

---

## Constraints

- **Budget:** Not specified (build budget N/A per intake).
- **Timeline:** Site complete in weeks; first sale target by ~2026-08-22 (3 months from 2026-05-22).
- **Brand assets available:** Light blue + orange color palette; fox-themed mascot. No logo file, font, or brand book yet — to be developed in Phase 4.
- **Existing infrastructure:** Postgres required; self-hosted US; GitHub Actions CI/CD; subdomain `uhhcraft.uhstray.io` (domain already owned).
- **Regulatory/legal:** PCI DSS (card payments); CCPA/COPPA to be scoped in Phase 5. No legal counsel.
- **Team capacity post-launch:** Hands-off / zero ops. Requires automated operations. Discord webhook for payment notifications.

---

## Non-goals

- No community, forum, or social features.
- No customer support system (chat, ticketing, etc.).
- No subscription or recurring-purchase model.
- No digital-only downloads (printable files, SVG exports, etc.).
- No reseller or wholesale program.
- No tutorial, education, or instructional content about AI or 3D printing.
- No user-generated content beyond the generation flow itself (no reviews, no community uploads).
- No internationalisation at launch (English only, US only).

---

## Open questions

| # | Question | Resolves in |
|---|----------|-------------|
| OQ-2 (from intake) | Device mix split % — mobile vs desktop for these personas | Phase 2 |
| OQ-3 | Network/device constraints for target audience | Phase 3 |
| OQ-8 | Payment processor decision | Phase 3 |
| OQ-9 | AI image generation tooling | Phase 3 |
| OQ-10 | AI 3D model generation tooling | Phase 3 |
| OQ-11 | Third-party fulfillment feasibility | Phase 3 / Phase 5 |
| OQ-12 | Monthly run budget ceiling | Phase 3 |
| OQ-13 | Uptime target (99.99%) vs self-hosted + zero-ops contradiction | Phase 3 |
| OQ-21 | 3D asset format for sticker previews vs printed items | Phase 3 |
| OQ-22 | Manufacturing asset: same file as preview or derived export? | Phase 3 |
| OQ-23 | Asset storage for accepted 3D designs | Phase 3 |
| OQ-25 | Generation retry/rejection policy (max retries, refund on failure?) | Phase 2 / Phase 5 |
