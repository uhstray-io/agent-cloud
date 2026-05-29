# Domain Overlay — E-commerce

Apply this overlay on top of a base preset when the site sells physical or digital goods. It enumerates the additional choices, integrations, and processes any e-commerce site must address — beyond what the base preset already provides.

> Use with: `shopify-custom-theme.md` (default for small/mid), `nextjs-typescript.md` (headless), `rails.md` (Spree or custom), `django.md` (custom with django-oscar), `sveltekit.md` (custom).

---

## Decisions to layer onto the base preset

### Catalog and inventory
- **Product model**: simple / variants (size × color) / bundles / configurable / digital / subscription.
- **Variant strategy**: option × value × SKU explosion vs grouped variants.
- **Inventory source of truth**: platform-native vs ERP-integrated vs warehouse-system-driven.
- **Oversell prevention**: real-time decrement at order, or eventual consistency.
- **One-of-a-kind (qty=1) handling**: critical for handmade / vintage / art.

### Pricing
- **Currency strategy**: single currency vs multi-currency display vs multi-currency charge.
- **Regional pricing**: shown per region or globally uniform.
- **B2B / wholesale tiers**: yes/no — affects auth and account model.
- **Promo / sale**: native vs custom; sitewide vs per-product.
- **Tax-inclusive vs exclusive display** by region.

### Cart and checkout
- **Cart pattern**: persistent cart (account-bound), session cart, no-cart (instant checkout for unique items).
- **Guest checkout vs forced account creation** — strongly prefer guest at the start.
- **Single-page vs multi-step checkout**.
- **Express payments**: Apple Pay, Google Pay, Shop Pay, PayPal Express.
- **Abandoned-cart recovery**: email cadence, SMS, push.
- **Address autocomplete**: Google Places, Loqate, Smarty, or native.

### Payments
- **Processor**: Stripe (default), Adyen (enterprise), Braintree, Square, regional (Razorpay, MercadoPago, Mollie, iDEAL via Stripe, etc.).
- **Wallets**: Apple Pay, Google Pay, Shop Pay, Amazon Pay.
- **BNPL**: Klarna, Affirm, Afterpay — opt in per region.
- **3DS2 / SCA** (mandatory in EU).
- **Subscriptions** (if applicable): Stripe Billing, Recurly, Chargebee, or platform-native.

### Tax and compliance
- **US sales tax**: Stripe Tax, Avalara, TaxJar; track nexus per state.
- **EU VAT**: OSS and IOSS schemes.
- **GST**: Australia, Canada, India, NZ.
- **Marketplace facilitator laws** (if applicable).
- **Invoicing requirements** for B2B / certain jurisdictions.
- **Sanctions / restricted territories** check.

### Shipping and fulfillment
- **Carrier integrations**: USPS, UPS, FedEx, DHL, country-specific (Australia Post, Royal Mail, Japan Post).
- **Rate calculation**: real-time API vs flat rates vs free-with-threshold.
- **International**: customs forms, duties (DDP vs DDU disclosed at checkout).
- **Local pickup / curbside / lockers**.
- **Tracking page** (branded vs carrier-default).
- **Returns**: prepaid label generation (Shippo, EasyPost), restock policy, refund timing.

### Order lifecycle
- **Email sequence**: placed → confirmed → fulfilled → shipped → delivered → review request → re-engagement.
- **Order modification window** (edit / cancel / partial refund).
- **Pre-orders and backorders**.
- **Order status page** (branded).

### Trust and reviews
- **Review provider**: Judge.me, Yotpo, Okendo, Trustpilot, Stamped.
- **Verified purchase** badges.
- **Q&A on PDPs**.
- **Trust badges** (security, BBB, returns policy).

### Loyalty and retention
- **Account incentives** (post-purchase signup).
- **Wishlist** / save-for-later.
- **Loyalty / rewards** (Smile.io, LoyaltyLion, Yotpo Loyalty).
- **Referral program** (ReferralCandy, Friendbuy).
- **Win-back email** flows.

### Search and discovery
- **Site search**: platform-native, **Algolia**, **Klevu**, **Searchspring**, **Meilisearch** + Postgres.
- **Faceted filter**: price, size, color, brand, rating, availability.
- **Sort options**: relevance, price, newest, popularity.
- **Recommendation engine**: native, **Nosto**, **Klevu**.

### Fraud and risk
- **Fraud screening**: Stripe Radar, Signifyd, Riskified, NoFraud.
- **Manual review thresholds** (first-time international above $X).
- **Chargeback workflow** and provider tools.

### PCI scope
- **Minimize** via tokenization / hosted fields (Stripe Elements, Adyen drop-in, etc.).
- **Never** touch raw PAN. If the team or stack requires it, expect SAQ D scope and significant overhead.

### Customer accounts
- **Order history** UX.
- **Saved addresses / payment methods**.
- **Subscriptions management** (if applicable).
- **GDPR account export / deletion** flow.

### Specific to small / unique-inventory shops
- **qty = 1 oversell prevention**.
- **Hiatus / vacation mode** (artist on holiday).
- **"Made to order" lead time** display.
- **Personalization** (engraving, monogram, custom dimensions).

### Specific to mid / high-volume shops
- **CDN image optimization** at scale.
- **Per-page caching** of catalog vs personalized cart.
- **A/B testing** of PDP layouts, CTAs, pricing presentation.
- **Customer Data Platform** (Segment, Rudderstack) consolidation.

### Marketplace-specific (if applicable)
- **Two-sided onboarding**.
- **KYC / KYB** verification (Stripe Identity, Persona, Onfido).
- **Payouts** (Stripe Connect).
- **Dispute resolution** workflow.
- **Anti-collusion / fake review** detection.

---

## Tooling additions for each base preset

| If base preset is... | Add these on top |
|----------------------|------------------|
| `shopify-custom-theme` | Most of the above are platform-native or via apps. Decisions remain (which apps, which review provider, which fraud tool, which loyalty program). |
| `nextjs-typescript` | **Stripe** (Checkout or Payment Element), **Stripe Tax**, **Shippo**/**EasyPost**, **Algolia** (search), **Klaviyo** (email), **Sentry**, **Inngest** (order workflows). |
| `sveltekit` | Same additions as Next.js. |
| `rails` | **pay** gem (Stripe wrapper), **money-rails**, **acts_as_taggable_on**, **Spree** (full e-commerce framework) if going custom-vertical. |
| `django` | **dj-stripe**, **django-oscar** (full e-commerce framework) or **django-shop**, **django-money**. |
| `go-templ-htmx` | **stripe-go**, custom catalog and order tables. Less common — Shopify often wins here unless owning the data is critical. |

---

## Compliance and legal checklist (always)

- **PCI DSS** scope determined and minimized.
- **GDPR / CCPA** consent and DSAR processes for customer data.
- **Distance selling regulations** (EU: 14-day right of withdrawal).
- **Tax registrations** per jurisdiction where you have nexus.
- **Restricted product compliance** (alcohol, CBD, supplements, tobacco, firearms, age-gated).
- **DMCA agent** (if hosting UGC like reviews or product images from sellers).
- **Cookie banner** with proper categorization (essential, analytics, marketing).
- **Accessibility statement** — checkout flows are increasingly the subject of ADA litigation.

---

## Reference

For the full e-commerce considerations checklist, see [`catalogs/considerations-catalog.md` → E-commerce](../../considerations-catalog.md#e-commerce). This overlay is the *stack* slice; the considerations catalog is the *operational* slice.
