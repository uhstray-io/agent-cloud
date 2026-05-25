# Domain Overlay — Marketing / Landing

Apply on top of a base preset (most commonly `astro-static.md`, `nextjs-typescript.md`, or `sveltekit.md`) for any site whose primary job is to convert a visitor into a single, specific action: signup, demo request, download, purchase, lead.

The marketing site for a SaaS product is the canonical case. So is a single-page launch site for a campaign.

---

## Decisions to layer onto the base preset

### Page set
- **Home / hero** — primary CTA visible above the fold.
- **Product / features** — supporting evidence for the CTA.
- **Pricing** — strongly prefer transparent pricing for self-serve products.
- **Customers / case studies** — proof.
- **About / team** — humans.
- **Contact / demo / sales** — for the enterprise path.
- **Blog** — SEO + thought leadership (optional; can be a separate subdomain).
- **Resources / library** — guides, ebooks, webinars (lead capture).
- **Legal** — privacy, terms, cookies, DPA, security.
- **404, thank-you, confirmation** — never skip these.

### CTA strategy
- **One primary CTA per page**, present consistently across the page.
- **Secondary CTA** at most (e.g., "Book a demo" + "Try free").
- **CTA copy**: verb + outcome ("Start your free trial" > "Sign up").
- **Above-fold + repeated** at each major scroll boundary.
- **Mobile-first CTA placement** (sticky bottom CTA for long pages).

### Lead capture
- **Minimum-fields principle**: name, email, company is often plenty.
- **Progressive profiling** for repeat visitors (HubSpot, Marketo).
- **Provider**:
  - Native form → POST to CRM (custom; Vercel functions / Worker / API route).
  - **HubSpot Forms** if HubSpot is the CRM.
  - **Tally**, **Typeform**, **Formspree** for hosted.
  - **Salesforce Web-to-Lead** for SFDC-shop.
- **Honeypot + Cloudflare Turnstile / hCaptcha** for spam.
- **Email validation** on the client + server.
- **Confirmation page** (separate URL, not modal) — for conversion tracking.

### CRM and marketing automation
- **HubSpot** (full-stack marketing + CRM).
- **Salesforce + Marketo** (enterprise).
- **Pipedrive** (sales-focused, smaller teams).
- **Customer.io / Loops** (lifecycle email).
- **Mailchimp** (newsletter + light automation).
- **Beehiiv / ConvertKit** (newsletter / creator).
- **HubSpot ↔ Stripe** or **Stripe ↔ Salesforce** sync for post-conversion handoff.

### A/B testing and experimentation
- **PostHog Experiments** (open-source-friendly).
- **GrowthBook** (open-source, self-host).
- **Statsig**, **Optimizely**, **VWO** (commercial).
- **What to test**: hero copy, CTA wording, pricing layout, social-proof section order, form length.
- **Sample-size discipline**: don't call winners on noise.

### Conversion tracking and attribution
- **First-party pixel** on conversion events (signups, demo requests, purchases).
- **Conversion events**: define them upfront and consistently.
- **Server-side tracking** (Conversions API for Meta, Enhanced Conversions for Google) — better than browser-only as cookies erode.
- **UTM hygiene**: standardized parameters, captured on landing and persisted through to signup.
- **Multi-touch attribution model**: first-touch / last-touch / linear / position-based — pick one and stick.

### Pixels and advertising
- **Meta Pixel + Conversions API**.
- **Google Ads conversion tracking + Enhanced Conversions**.
- **LinkedIn Insight Tag** (B2B).
- **TikTok Pixel** (consumer).
- **Reddit Pixel** (technical audiences).
- **Pinterest, X, etc.** as channel mix demands.
- **Consent-gate everything** non-essential (GDPR/CCPA).

### Consent management
- **Cookiebot**, **OneTrust**, **Termly**, **Cookieyes**, **Klaro!** (open-source).
- **Block analytics + pixels until consent** — not just hiding the banner.
- **Reject-all in one click** in jurisdictions that require it.

### Social previews (must-have)
- **OG image** per page; default and per-page overrides.
- **Twitter Card** (`summary_large_image`).
- **Auto-generated OG images** (Vercel OG, Cloudinary, Cloudflare ImagePOST).
- **OG image dimensions** (1200×630).
- **Test with** Twitter Card Validator, LinkedIn Inspector, Facebook Sharing Debugger.

### Performance budget (tighter than most archetypes)
- **LCP < 2.5s** (ideally < 1.5s).
- **CLS < 0.1**.
- **Total initial JS < 100KB** if possible (under the conversion threshold).
- **No third-party JS above the fold** unless mandatory.
- **Image optimization** is non-negotiable.
- **Font loading** discipline.

### Trust signals
- **Customer logos** (with permission).
- **Testimonials** with real names + faces + roles.
- **Stats** (with sources).
- **Security badges** (SOC 2 in progress, ISO 27001, etc.).
- **Press / awards**.
- **Funding announcements** (if relevant).

### SEO
- **Per-page meta** (title pattern: `{title} | {brand}`, description 150–160 chars).
- **Structured data**: Organization, WebSite (sitelinks search), BreadcrumbList, FAQPage, Article, Product, SoftwareApplication as relevant.
- **Sitemap.xml** including all language variants.
- **Robots.txt** allowing everything (except staging).
- **Canonical tags**.
- **Internal linking** across blog → product pages.

### Pricing page UX
- **Transparent pricing wins** for self-serve products.
- **Per-seat / flat / usage** displayed clearly.
- **Monthly / annual toggle** with discount visible.
- **Side-by-side comparison table**.
- **Enterprise tier** with "Contact sales" — always have one even if you don't ship enterprise.
- **FAQ for billing concerns** (cancellation, refunds, taxes).

### Email capture beyond forms
- **Exit-intent modals** — controversial, sometimes effective.
- **Scroll-triggered banners**.
- **In-content opt-ins** (mid-article CTA).
- **Sticky footer bar**.

### Content / blog
- **Posting cadence**: realistic (1/month > inconsistent weekly).
- **SEO-targeted content** vs thought-leadership content vs both.
- **Author bylines + headshots**.
- **Reading time, social share, related posts** at the bottom.
- **Newsletter signup** inline + bottom + sidebar.
- **Editorial calendar**.

---

## Tooling additions per base preset

| If base preset is... | Add these on top |
|----------------------|------------------|
| `astro-static` | **Vercel OG** or **Satori** for OG images, **Plausible**/**Fathom** + a pixel suite, **HubSpot Forms** or **Tally**, **PostHog** for product-led overlays, **Cookieyes** for consent. |
| `nextjs-typescript` | **Next OG (`@vercel/og`)**, **PostHog Experiments**, native API routes for form-to-CRM POST, **next-seo** or App Router metadata API, **Vercel Analytics**. |
| `sveltekit` | **@vercel/og** via adapter, **PostHog**, server form actions → CRM, consent management. |
| `rails` | Marketing site is usually a *separate* static site (Astro) — don't try to do marketing inside Rails unless the product is also Rails-served. |

---

## Anti-patterns specific to marketing/landing

- **Hero with three competing CTAs.** Conversion drops.
- **Hidden pricing.** Self-serve users bounce.
- **Wall of features without a value-statement opener.**
- **Testimonials without names or faces** — looks fake.
- **Auto-playing video with sound.**
- **Modal popups in the first 5 seconds.**
- **Pop-up that blocks reading the value proposition.**
- **Mobile design that requires zooming.**
- **Form that asks for phone number on a "free trial" signup.**
- **Conversion event tracked only client-side.** Cookie-blocked users disappear from your funnel.

---

## Reference

For the full marketing / landing considerations checklist, see [`catalogs/considerations-catalog.md` → Marketing / landing](../../considerations-catalog.md#marketing--landing).
