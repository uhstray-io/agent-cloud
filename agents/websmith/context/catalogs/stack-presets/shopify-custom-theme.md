# Preset — Shopify Custom Theme

Shopify's hosted commerce platform with a from-scratch (or starter-based) custom theme written in **Liquid** with minimal JavaScript. The fastest path to a polished, owned-looking e-commerce site for small-to-mid catalogs.

---

## When it fits

- **Archetypes**: e-commerce storefronts (small to mid catalog, < ~10,000 SKUs).
- **Team**: solo operator or small team; design-led; modest engineering capacity.
- **Why Shopify**: payments, taxes, shipping, inventory, fraud, returns, and order management all included. Building this from scratch costs months.
- **Wanting a custom look without a custom backend**.

## When it doesn't

- Massive catalog or marketplace pattern (consider headless commerce: Hydrogen, Medusa, commercetools).
- Heavy B2B / quote-based / configure-price-quote needs (BigCommerce, Sana Commerce, custom).
- Strong "no SaaS vendor lock-in" requirement.
- Need to own the data model end-to-end (custom Stripe + DB instead — see `domains/ecommerce.md` with `nextjs-typescript`).

## Composition

| Category | Choice |
|----------|--------|
| Platform | **Shopify Basic** ($29/mo at time of writing) — or **Advanced** if you need shipping rates by app, custom reports |
| Theme | **Custom Liquid theme** (from scratch) or starter (**Dawn** is the modern reference) |
| Styling | **Vanilla CSS with custom properties** (recommended) or Tailwind via the Shopify CLI's build step |
| JavaScript | **Vanilla + Alpine.js** for sprinkles; **HTMX** for partial updates if helpful |
| Image handling | Shopify's image API (`{{ image \| image_url: width: 800 }}`) with `srcset` |
| Search | Shopify's native (basic) or **Algolia** / **Klevu** apps for richer search |
| Reviews | **Judge.me** (free tier), **Yotpo**, **Okendo** |
| Email | Shopify Email (built-in) for newsletters; **Klaviyo** when you cross volume |
| Forms | Native Shopify contact form + apps for advanced needs |
| Customer accounts | New customer accounts (passwordless email-login) by default |
| Localization | **Shopify Markets** for multi-currency + multi-language |
| Analytics | Shopify Analytics (built-in) + **Plausible** for privacy-respecting site analytics |
| Theme dev | **Shopify CLI** for local dev + theme sync |

## Hosting

- **Shopify-managed**. No hosting decisions. Global CDN included.
- Region: global; data residency varies by Shopify region.

## CI/CD

- **GitHub Actions** + **Shopify CLI**:
  - On PR: `theme check` (linting), Lighthouse on theme preview, deploy to a dev/staging theme.
  - On merge to `main`: deploy to live theme.
- **Theme version history** in Shopify Admin gives one-click rollback.

## Cost profile

- **Shopify Basic**: $29/month (2024 pricing; check current).
- **Shopify Payments**: standard card processing fees apply; saves the alternative-gateway fee.
- **Apps add up**: budget $0–$200/month depending on Klaviyo, reviews, faceted search, etc.
- **Total typical**: $30–$250/month for a small store.

## Watch-outs

- **App bloat.** Every "just one more app" adds JS to the theme. Audit on every install; remove unused.
- **Liquid is template-only.** Complex logic belongs in a Shopify app or a separate backend, not in `.liquid` files.
- **Checkout customization is limited** outside Plus plan ($2,000+/mo). Plan around defaults.
- **Vendor lock-in.** Migrating off Shopify means rebuilding product, customer, and order data flows. Build assuming you'll stay.
- **GDPR / cookies.** Shopify provides a banner app; configure correctly for EU traffic.
- **Inventory edge cases.** For one-of-a-kind items (qty = 1), confirm oversell prevention is wired correctly.
- **International tax.** Shopify Tax handles a lot — but verify each region you actually sell in.

## Customization points

- **Replace vanilla CSS with Tailwind** via the Shopify CLI's bundler integration if the team prefers utility-first.
- **Move to Hydrogen** (Shopify's React framework) if you outgrow Liquid for content-heavy or storytelling-heavy storefronts.
- **Add Shopify Functions** for custom discounts, shipping, payment customization (replaces Shopify Scripts).
- **Headless** via the Storefront API + a custom frontend (Astro, Next.js) — but you lose admin/checkout flexibility.

## Pair with

- `domains/ecommerce.md` for the full commerce-specific considerations checklist (most of which Shopify handles, but you still need to make decisions).
- A separate marketing/blog site (Astro, Webflow) on a subdomain if the Shopify blog isn't editorially flexible enough.
