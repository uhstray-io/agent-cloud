# Stack Presets

Concrete starter stacks. Each preset is a vetted combination of frontend, styling, data layer, hosting, and ops choices that have shipped many sites together.

> **Relationship to `catalogs/stacks.md`.** That file is the *principles* document — how to think about stack selection, tradeoffs per category. Presets here are the *opinionated starting points* an agent can propose verbatim. The user can take a preset as-is, swap components, or layer a domain overlay (see `domains/`).

---

## How agents use presets

1. After Phase 1 (Purpose) and Phase 2 (Template) are complete, open Phase 3 (Tooling).
2. Pick the **2–3 presets** that best match the archetype and constraints.
3. Present them to the user side by side with a one-line "why this fits."
4. Layer the relevant `domains/<archetype>.md` overlay onto the chosen preset.
5. Customize from there based on intake constraints (existing accounts, team skills, hosting preference).

Do not present a preset as if it's the only option, and never adopt one without explaining tradeoffs.

---

## Base presets

| Preset | Best for | Headline tradeoffs |
|--------|----------|--------------------|
| [`astro-static.md`](./astro-static.md) | Marketing, blogs, portfolios, small docs | Fastest to ship; near-zero JS; less suited to app-shell UX |
| [`nextjs-typescript.md`](./nextjs-typescript.md) | SaaS, dynamic content, mixed marketing+app | Most flexible; biggest ecosystem; complexity creep risk |
| [`sveltekit.md`](./sveltekit.md) | SaaS, content-heavy with light interactivity | Smaller bundles than Next; smaller ecosystem |
| [`rails.md`](./rails.md) | Solo founders, CRUD-heavy, fast iteration | Mature, batteries included; less edge-first |
| [`django.md`](./django.md) | Python teams, data-heavy, admin-driven | Robust admin out of box; less suited to highly interactive UX |
| [`go-templ-htmx.md`](./go-templ-htmx.md) | Server-driven UX, minimal JS, ops-conscious | Tiny bundles, simple ops; ecosystem leaner than JS |
| [`shopify-custom-theme.md`](./shopify-custom-theme.md) | Small / mid e-commerce, solo operator | Saves months on tax/payments/fraud; lock-in |
| [`starlight-docs.md`](./starlight-docs.md) | Open-source / product documentation | Built-in search, versioning, dark mode; opinionated layout |

## Domain overlays

| Overlay | Adds |
|---------|------|
| [`domains/ecommerce.md`](./domains/ecommerce.md) | Payments, catalog, tax, shipping, fraud, returns |
| [`domains/documentation.md`](./domains/documentation.md) | Search, versioning, code highlighting, contributor flow |
| [`domains/saas.md`](./domains/saas.md) | Auth, billing, RBAC, multi-tenancy, admin, status |
| [`domains/marketing-landing.md`](./domains/marketing-landing.md) | A/B testing, lead capture, CRM, attribution, pixels |

---

## Choosing a preset — quick rubric

- **Static, mostly-content site, single team, low ops appetite** → `astro-static`
- **Dynamic SaaS with auth and billing** → `nextjs-typescript` or `sveltekit`
- **CRUD-heavy app, solo founder or small team** → `rails` or `django`
- **Server-rendered with minimum JS payload, tiny ops footprint** → `go-templ-htmx`
- **Online store, small catalog, don't want to operate payment/tax infra** → `shopify-custom-theme`
- **Project docs for an open-source library** → `starlight-docs`
- **Marketing + product app on same domain** → `nextjs-typescript` (often with subdirectories or workspaces)

Override the rubric whenever team skill or vendor constraints argue otherwise. Team skill > preset.

---

## What a preset file contains

Every preset file documents:

1. **Headline** — one-paragraph summary.
2. **When it fits** — archetypes and conditions.
3. **When it doesn't** — explicit anti-patterns.
4. **Composition** — concrete tooling choices per category.
5. **Hosting** — recommended host and region pattern.
6. **CI/CD** — pipeline shape.
7. **Cost profile** — rough monthly run-cost range.
8. **Watch-outs** — known sharp edges.
9. **Customization points** — what to swap when intake demands it.
