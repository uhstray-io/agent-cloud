# Intake

> Phase 0 artifact. Source: questionnaire.md, filled by user.
> Status: awaiting user approval.

---

## Project basics

- **Working title:** UhhCraft
- **Working directory:** `./output/` (relative to website_framework repo)
- **Existing repo:** None
- **Existing site to migrate from:** None
- **Target launch date:** OPEN — user said "3 months" as success horizon; no hard date given (resolve in Phase 1)

---

## People

- **Team size:** 2
- **Maintainer post-launch:** User (solo maintainer)
- **Maintainer expertise:** Developer
- **Language/framework preferences:** Rust, Go, C#, Java, some JS, some Python
- **Avoided languages/tools:** No JS-heavy frameworks
- **Content authors:** OPEN — not specified (resolve in Phase 2)
- **Support owners:** None

---

## Audience and devices

- **Primary persona:** OPEN — not specified (resolve in Phase 1)
- **Geographic distribution:** USA (no granularity specified)
- **Languages at launch:** OPEN — not specified (resolve in Phase 1)
- **Languages on roadmap:** OPEN — not specified (resolve in Phase 1)
- **Device mix:** OPEN — not specified (resolve in Phase 2)
- **Network expectations:** OPEN — not specified (resolve in Phase 3)
- **Browser support:** OPEN — not specified (resolve in Phase 3)
- **Assistive-tech users in audience:** OPEN — not specified (resolve in Phase 5)

---

## Goal and success

- **One-sentence purpose:** A shop where customers can buy unique, one-of-a-kind physical goods (primarily stickers and 3D-printed items) designed using AI-generation tools, with the option to upload their own designs or have something crafted from scratch using AI.
- **Primary action:** Sell
- **Success signal:** Users understand the purpose and want to make a purchase
- **Success horizon:** 3 months

---

## Compliance and regulation

- **User regions:** USA
- **Regulatory regimes checked by user:** None checked
- **⚠ FLAG — PCI DSS:** Site will accept card payments for physical goods. Even via a third-party processor (e.g., Stripe), PCI DSS SAQ A scope applies. Not checked by user — resolve in Phase 5.
- **⚠ FLAG — COPPA:** Stickers are a product category purchased by minors. If the site has no age gate, COPPA may apply. No mention by user — resolve in Phase 5.
- **⚠ FLAG — CCPA:** US-based e-commerce collecting personal data (name, address, payment info). California customers → CCPA/CPRA may apply depending on revenue/data thresholds — resolve in Phase 5.
- **Industry specifics:** None (physical goods retail)
- **Legal counsel:** No
- **Data residency:** No stated requirement; self-hosted US

---

## Performance budget

- **General feel target:** Acceptable
- **LCP target:** < 1 second *(note: this is stricter than the typical "good" threshold of 2.5s — constrains image strategy, server rendering, and CDN)*
- **JS bundle ceiling:** No opinion
- **Page weight ceiling:** No opinion
- **Network targets:** OPEN — not specified (resolve in Phase 3)

---

## Accessibility requirement

- **WCAG target:** AA
- **Legal mandate:** Unsure (resolve in Phase 5 — e-commerce sites generally not mandated unless ADA lawsuit risk is a concern)
- **Manual screen reader / keyboard testing:** No
- **AT users in audience:** OPEN (resolve in Phase 1)

---

## Tech preferences

- **Existing vendor accounts:** None checked/confirmed
- **Must use:** Postgres for databases
- **Must not use:** Gratuitous JS-heavy frameworks — the principle is backend-first: push logic server-side where possible, minimize client-side JS footprint. JS-heavy rendering libraries are acceptable when there is no server-side substitute (e.g., Three.js/Babylon.js for the 3D canvas viewer).
- **OSS preference:** Strong OSS / OSS-leaning
- **Build budget:** Not specified
- **Run budget (monthly):** OPEN — not specified (resolve in Phase 3)

---

## Content and integrations

- **Current content location:** Nowhere yet
- **Content volume:** OPEN — no product counts specified (resolve in Phase 2)
- **Required integrations:**
  - Payment processing — **OPEN decision** (user mentioned Shopify or "something similar"; resolve in Phase 3)
  - AI image generation (stickers/cutouts) — tooling **OPEN** (resolve in Phase 3)
  - AI 3D model generation — tooling **OPEN** (resolve in Phase 3)
  - Optional: third-party fulfillment partner (if in-house production can't fulfill) — **OPEN** (resolve in Phase 5)
- **Webhooks/events:** Discord webhook notification on successful payment (specified by user)
- **AI/ML features:** Image generation (sticker designs) and 3D model generation
- **⚠ CORE FEATURE — Visual Canvas / 3D Preview (added Phase 0):**
  - After a user submits a generation prompt (for a sticker or 3D-printed item), they must see a **3D representation** of the result in an interactive canvas before they can proceed.
  - The canvas is a blocking step in the purchase flow: the user must explicitly **accept** or **reject/iterate** the generated result.
  - On acceptance, the generated 3D asset is **saved as the manufacturing asset** — the canonical file used to produce the physical item (e.g., the STL/OBJ/GLB for a 3D print; the layered image for a sticker die-cut).
  - This is not a preview thumbnail — it is an interactive 3D viewer embedded in the product configuration / checkout flow.
  - **Downstream implications:**
    - Requires a 3D rendering engine in the browser (Three.js, Babylon.js, or similar) — this is significant client-side JS. **Conflicts with "no JS-heavy frameworks" constraint** — see contradiction flag below.
    - AI generation pipeline must output a 3D-viewable format (GLB/GLTF preferred for web; STL for manufacturing).
    - Accepted assets need a storage system (object storage — S3-compatible) tied to the order record.
    - Manufacturing pipeline must consume the stored asset from the order, not regenerate it.
  - **Format questions (OPEN — resolve Phase 3):** What 3D format for sticker previews vs printed items? Are stickers shown as a flat 3D mockup (physical sticker on a surface) or a mesh? Is the manufacturing asset the same file as the preview asset or a derived export?

---

## Brand and visual references

- **Existing brand assets:**
  - Colors: Light Blue and Orange
  - Mascot: Fox-themed
  - Logo/fonts/brand book: None specified
- **Liked reference sites:** OPEN — user left blank (resolve in Phase 4)
- **Disliked reference sites:** OPEN — user left blank (resolve in Phase 4)
- **Desired-feel adjectives:** Clean, Cute, Warm
- **Avoid adjectives:** Sharp, Robotic, AI-Generated
- **⚠ BRAND NOTE:** The site sells AI-generated goods but should *not feel* AI-generated. This is a deliberate brand positioning decision — handcrafted/curated aesthetic over algorithmic — and should guide every visual and copy decision in Phase 4.

---

## Hosting and deployment

- **Preferred host:** Self-hosted, US
- **Preferred regions:** US
- **CDN/edge needs:** Not specified
- **CI/CD platform:** GitHub Actions
- **Domain:** Already owns `uhstray.io`; will use subdomain `uhhcraft.uhstray.io`

---

## Operational appetite

- **Post-launch ops capacity:** Hands-off / zero
- **Notification channel:** Discord webhook for payment notifications
- **On-call:** None
- **Uptime target:** 99.99%
- **Backup/DR appetite:** Moderate
- **⚠ CONTRADICTION — Uptime vs ops capacity:** 99.99% uptime (~52 minutes downtime/year) + self-hosted infrastructure + zero ops team is extremely difficult to achieve. This combination requires highly automated recovery, redundant infrastructure, or a managed hosting layer. Must be resolved in Phase 3 — either relax the uptime target, move to a managed platform, or spec an automated recovery setup.
- **Resolved — "no JS-heavy" clarified:** The constraint is a design philosophy, not a hard ban. Principle: server-side rendering and server-side logic by default; JS only where no server-side equivalent exists. The 3D canvas viewer (Three.js/Babylon.js) is an explicit justified exception. A React/Vue/Next SPA as the site shell would not be justified.

---

## Non-goals

- OPEN — user left this section blank (resolve in Phase 1)

---

## Open questions (carry forward)

| # | Question | Resolves in |
|---|----------|-------------|
| OQ-1 | Hard launch date (user has "3 months" horizon) | Phase 1 |
| OQ-2 | Primary user persona — who is the buyer? | Phase 1 |
| OQ-3 | Geographic/device mix granularity | Phase 1 / Phase 2 |
| OQ-4 | Languages at launch | Phase 1 |
| OQ-5 | Non-goals — what is this site explicitly NOT for? | Phase 1 |
| OQ-6 | Content authors — who writes product copy? | Phase 2 |
| OQ-7 | Product count estimates | Phase 2 |
| OQ-8 | Payment processor decision (Stripe direct vs Shopify vs other) | Phase 3 |
| OQ-9 | AI image generation tooling (DALL·E, Stable Diffusion, Replicate, etc.) | Phase 3 |
| OQ-10 | AI 3D model generation tooling | Phase 3 |
| OQ-11 | Third-party fulfillment partner feasibility | Phase 3 / Phase 5 |
| OQ-12 | Monthly run budget ceiling | Phase 3 |
| OQ-13 | Uptime target vs self-hosted + zero-ops contradiction | Phase 3 |
| OQ-14 | Reference sites (liked/disliked) | Phase 4 |
| OQ-15 | PCI DSS scope acknowledgment | Phase 5 |
| OQ-16 | COPPA applicability (minor users?) | Phase 5 |
| OQ-17 | CCPA applicability | Phase 5 |
| OQ-18 | Accessibility legal mandate clarification | Phase 5 |
| OQ-19 | Browser support matrix | Phase 3 |
| OQ-20 | CDN/edge needs for self-hosted setup | Phase 3 |
| OQ-21 | 3D asset format for sticker previews (flat mockup vs mesh?) | Phase 3 |
| OQ-22 | Manufacturing asset format: same file as preview or derived export? | Phase 3 |
| OQ-23 | Asset storage: where do accepted 3D assets live? (S3-compatible bucket?) | Phase 3 |
| OQ-24 | ~~Scope of "no JS-heavy frameworks"~~ — **RESOLVED**: backend-first philosophy; Three.js/Babylon.js acceptable for 3D canvas; no SPA shell. | ✓ Phase 0 |
| OQ-25 | What happens if generation fails or the user rejects all iterations? (Max retries, fallback, refund?) | Phase 2 / Phase 5 |
