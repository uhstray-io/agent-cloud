# Stacks Catalog

Principles, alternatives, and trade-offs for choosing a stack during phase 3 (tooling). Use this file for the *reasoning*; use [`stack-presets/`](./stack-presets/) for *concrete starter templates*.

> **Two-layer model.**
> - **This file (`stacks.md`)** is the principles document — how to think about each category, what's worth trading off, what's commonly viable.
> - **[`stack-presets/`](./stack-presets/README.md)** is the concrete starter library — opinionated combinations an agent can propose verbatim, with domain overlays for e-commerce / docs / SaaS / marketing.
>
> When working through phase 3, an agent should typically: pick 2–3 candidate **presets** to present, layer the appropriate **domain overlay**, then use this file's principles to defend the pick and to swap categories where intake or template needs argue.

> **Non-exhaustive.** Web tooling evolves rapidly. Entries reflect what's commonly viable; the user's team and constraints come first.

---

## Selection principles

1. **Match team skills.** A stack the team knows ships faster and runs more reliably than the "best" stack they don't.
2. **Match rendering needs.** Static content → SSG; personalized/auth → SSR; app-like → CSR with islands or SPA.
3. **Match operational appetite.** Serverless minimizes ops; self-hosted maximizes control; managed platforms balance both.
4. **Minimize lock-in proportional to switching cost.** Lock-in is fine when the trade is worth it.
5. **Boring beats clever** unless cleverness pays for itself.

---

## Static content sites (marketing, blog, docs, portfolio)

### Default
- **Framework:** Astro (or Next.js / SvelteKit in SSG mode)
- **Styling:** Tailwind CSS
- **Content:** Markdown / MDX in repo, or git-based CMS (Decap, Tina, Keystatic, Sveltia)
- **Search:** Pagefind (built-in), or Algolia for larger sites
- **Hosting:** Cloudflare Pages, Netlify, or Vercel
- **CI/CD:** GitHub Actions → platform-native deploys
- **Analytics:** Plausible / Fathom (privacy-first) or PostHog

**Why this default:** Static is the fastest, cheapest, most reliable way to ship content sites. Astro is content-first with optional interactivity. Markdown in git keeps content version-controlled and AI-readable.

### Alternatives
- **Hugo** or **Eleventy** for build-speed obsession or non-JS authors.
- **Next.js** if the site might grow into a SaaS or needs heavier interactivity.
- **WordPress (headless or classic)** if non-technical authors need a familiar editor and shipping speed matters.

---

## Documentation sites

### Default
- **Framework:** Starlight (Astro-based), VitePress, Docusaurus, Nextra, or Mintlify
- **Search:** Built-in (Pagefind for Starlight, Algolia DocSearch for Docusaurus)
- **Versioning:** Framework-native
- **Hosting:** Same as static sites
- **API reference:** Generated from OpenAPI/source (Redoc, Stoplight, Mintlify)

**Why this default:** Documentation has very specific patterns (sidebar nav, versioning, code highlighting, search) that doc-specific frameworks handle out of the box.

### Alternatives
- **Mkdocs Material** if the team is Python-oriented or wants the most mature docs UX in the ecosystem.
- **Custom Next.js / Astro** if the docs site needs to share components with a marketing site.
- **Hugo + Doks** for build speed on huge doc sites.

---

## E-commerce (small-to-mid)

### Default
- **Platform:** Shopify (hosted) — if catalog is conventional and team is small.
- **Storefront:** Shopify default themes; or **Hydrogen** + Shopify Storefront API for headless.
- **Hosting (headless):** Shopify Oxygen, Vercel, Cloudflare Pages.
- **Payments:** Shopify-included.

**Why this default:** Shopify handles taxes, shipping, fraud, payments, inventory, order management. Building this from scratch costs months. Reserve custom for genuine differentiation.

### Alternatives
- **BigCommerce** for similar trade-offs, sometimes better B2B.
- **WooCommerce** if WordPress is already in use and team prefers PHP.
- **Medusa + Next.js / Saleor + React** for open-source headless with full control.
- **Stripe Checkout + custom catalog** for very simple stores (1-10 SKUs).

---

## E-commerce (large / enterprise / marketplace)

### Default
- **Frontend:** Next.js or Remix
- **Backend:** Custom (Node, Go, Rust, Python) on a managed platform (Fly.io, Render, AWS)
- **Database:** Postgres (Neon, Supabase, RDS, Cloud SQL)
- **Search:** Algolia, Meilisearch, or Elasticsearch
- **Payments:** Stripe Connect (for marketplaces) or Adyen
- **Tax:** Stripe Tax or Avalara
- **CMS:** Sanity, Contentful, or Storyblok for editorial content
- **CDN / images:** Cloudflare, Cloudinary, or imgix

**Why:** Volume, customization, multi-region, or marketplace patterns outgrow hosted platforms.

---

## SaaS app (marketing + product)

### Default
- **Marketing site:** Astro or Next.js (App Router), Tailwind, hosted on Vercel/Cloudflare
- **App:** Next.js or Remix, or SvelteKit; or **Phoenix LiveView / Rails / Django** if backend-driven UX is preferred
- **Database:** Postgres (Neon, Supabase, PlanetScale, RDS)
- **Auth:** Clerk, Auth.js (NextAuth), Supabase Auth, or WorkOS for enterprise SSO
- **Payments:** Stripe Billing, Paddle, Lemon Squeezy
- **Email:** Resend, Postmark, Loops
- **Analytics:** PostHog (product + web), Sentry (errors)
- **Hosting:** Vercel, Fly.io, Render, or AWS (ECS / Lambda)
- **CI/CD:** GitHub Actions

**Why this default:** Modern meta-frameworks plus managed building blocks let small teams ship a full SaaS in weeks. Postgres is the safe long-term bet for data.

### Alternatives
- **T3 stack** (Next.js + tRPC + Prisma + Tailwind) for typed end-to-end.
- **Rails + Hotwire** for solo founders or small teams who want one language and one mental model.
- **Phoenix + LiveView** for highly interactive UX without writing JS.
- **Django + HTMX** for Python teams.
- **SvelteKit** or **Nuxt** if the team prefers Svelte / Vue over React.

---

## Internal tools / dashboards / admin

### Default
- **Framework:** Retool, Internal.io, Tooljet, or Appsmith (low-code) for fast internal builds.
- **Custom:** Next.js or Remix + Tailwind + a tables library (TanStack Table) + a charting library (Recharts, Tremor).
- **Auth:** SSO via Auth0, Clerk, WorkOS, or platform-native (Okta).
- **Hosting:** Behind VPN or SSO-only; internal subdomain.

**Why this default:** Internal tools rarely justify months of custom build. Low-code is genuinely faster — until it isn't, at which point migrate.

---

## Personal sites / portfolios

### Default
- **Framework:** Astro, Eleventy, or even hand-written HTML.
- **Styling:** Tailwind, vanilla CSS, or a tiny utility set.
- **Hosting:** Cloudflare Pages, GitHub Pages, Netlify.
- **Cost:** $0–$10/year (domain only).

**Why:** Minimize ops. Maximize writing/making time.

---

## Community / forum

### Default
- **Platform:** Discourse (self-hosted or hosted) for substantial communities.
- **Custom:** Next.js + Postgres + a feed engine; requires substantial moderation tooling.

**Why this default:** Discourse handles posting, moderation, search, notifications, and spam — all of which are expensive to rebuild.

### Alternatives
- **Lemmy** / **Flarum** / **NodeBB** for open-source self-hosted.
- **Circle / Mighty Networks** for hosted community-as-a-service.
- **Custom** only if community UX is the differentiator.

---

## Educational / LMS

### Default
- **Platform:** Teachable, Thinkific, Podia, or LearnWorlds (hosted).
- **Custom:** Next.js + Mux (video) + Stripe (payments) + Postgres.
- **LMS-specific OSS:** Moodle, Open edX (heavyweight; institutional).

**Why:** Hosted LMS platforms include video hosting, progress tracking, certificates, and payments. Custom is justified when course delivery itself is the product differentiation.

---

## Government / public service

### Default
- **Framework:** Plain HTML + light JS, or 11ty / Hugo for static; or a government-approved stack (e.g., GOV.UK Design System for UK).
- **Styling:** Government design system (US Web Design System, GOV.UK, Canada.ca).
- **Hosting:** Government-approved cloud (FedRAMP, IRAP) where applicable.

**Why this default:** Accessibility, performance on low-end devices, and conformance to government design systems are non-negotiable. Static + design-system primitives meet that bar with the least risk.

---

## Real estate / listing-heavy sites

### Default
- **Framework:** Next.js or Remix
- **Database:** Postgres + PostGIS for geo queries
- **Search:** Algolia or Meilisearch
- **Maps:** Mapbox or MapLibre + OpenStreetMap
- **Images:** Cloudinary or imgix

---

## Events / conferences

### Default
- **Framework:** Astro or Next.js for the marketing site
- **Tickets:** Tito or Eventbrite, or Stripe Checkout for custom
- **Schedule:** static data file or headless CMS
- **Streaming:** Mux, Cloudflare Stream, or YouTube/Vimeo embeds

---

## Nonprofit / cause

### Default
- **Framework:** Astro, WordPress (Givewp / GiveButter), or Webflow
- **Donations:** Donorbox, Stripe (with Stripe Climate / nonprofit pricing), GiveButter
- **CRM:** Salesforce Nonprofit Cloud, Bloomerang, Kindful

---

## Restaurants / hospitality

### Default
- **Framework:** Webflow, Squarespace, Astro, or WordPress.
- **Reservations:** Embed OpenTable, Resy, Tock, or SevenRooms.
- **Ordering:** Toast, Square, ChowNow.

**Why this default:** Restaurants need a simple marketing site + reliable reservation/order embed. Don't build the reservation system.

---

## Cross-cutting choices

### Frontend frameworks (when one is needed)

| Framework | Strengths | Watch-outs |
|-----------|-----------|------------|
| **Next.js** | Largest ecosystem, broad hosting, robust App Router patterns | Complexity creep; framework-specific gotchas |
| **Astro** | Content-first, low-JS by default, multi-framework islands | Less suited to app-shell UX |
| **SvelteKit** | Small bundles, ergonomic DX | Smaller ecosystem |
| **Remix** | Web-platform-aligned, robust forms, good error UX | Fewer turnkey deploys outside Vercel/Fly |
| **Nuxt** | Vue-equivalent of Next | Vue ecosystem somewhat smaller than React |
| **SolidStart** | Fine-grained reactivity, fast | Newer, smaller ecosystem |
| **Qwik** | Resumability, near-zero JS hydration | Newer paradigm; ecosystem still maturing |
| **Hugo / Eleventy** | Build-speed, no JS by default | Less suited to interactive features |
| **Rails / Django / Phoenix / Laravel** | Mature, batteries-included, server-driven UX | Less compatible with edge-first hosting |

### CSS approaches

| Approach | Strengths | Watch-outs |
|----------|-----------|------------|
| **Tailwind** | Speed once familiar, design-system enforceable | Verbose markup; team taste |
| **CSS Modules** | Scoped, simple, framework-agnostic | Boilerplate, less expressive |
| **CSS-in-JS** (Emotion, styled-components) | Co-located, dynamic | Runtime cost, SSR complexity |
| **Vanilla-extract / Panda** | Static CSS-in-TS, typed tokens | Build complexity |
| **Component libraries** (Material, Chakra, Mantine) | Speed, accessibility baked in | Visual sameness, override pain |
| **shadcn/ui + Radix + Tailwind** | Owned-code components | Manual updates as upstream evolves |

### Backend languages (when needed)

| Language | Strengths | Best fit |
|----------|-----------|---------|
| **Node / TypeScript** | Shared with frontend, broad ecosystem | Most SaaS, BFFs, integrations |
| **Python** | ML/data, mature web frameworks | Data-heavy apps, AI integrations |
| **Go** | Performance, concurrency, easy deploy | High-throughput, infra, CLI-adjacent |
| **Rust** | Performance, correctness, low memory | Latency-critical, systems-adjacent |
| **Ruby (Rails)** | Productivity, conventions | Solo / small teams, CRUD-heavy |
| **Elixir / Phoenix** | Concurrency, LiveView | Realtime apps, chat, dashboards |
| **PHP (Laravel)** | Mature, cheap hosting | Hosting constraints, classic CMS-adjacent |
| **C# / .NET** | Enterprise, mature tooling | .NET-shop, Windows integrations |

### Databases

| DB | Best fit |
|----|---------|
| **Postgres** | Default for almost everything relational |
| **MySQL** | Existing investment, PlanetScale ecosystem |
| **SQLite** | Single-server, embedded, edge (Turso, LiteFS) |
| **MongoDB** | Document-shaped data, schema flux (use carefully) |
| **DynamoDB** | AWS-native, known access patterns |
| **Redis** | Cache, sessions, queues, ephemeral |
| **ClickHouse / BigQuery / DuckDB / Snowflake** | Analytics |
| **pgvector / Pinecone / Qdrant / Weaviate** | Vector search / embeddings |

### Hosting

| Host | Sweet spot |
|------|-----------|
| **Vercel** | Next.js, edge functions, preview deploys, marketing+SaaS |
| **Netlify** | Static + serverless, multi-framework |
| **Cloudflare Pages / Workers** | Edge-first, low cost, global |
| **Fly.io** | Stateful services, multi-region, full apps |
| **Render** | Heroku-like UX, full apps |
| **Railway** | Quick deploys, side projects |
| **AWS / GCP / Azure** | Anything; complexity ↑ |
| **Self-hosted (VPS)** | Cost, control, ops responsibility |

### Auth

| Provider | Best fit |
|----------|---------|
| **Clerk** | Fast, polished UX, opinionated |
| **Auth.js (NextAuth)** | Self-host with adapters, free |
| **Supabase Auth** | If using Supabase for DB |
| **Auth0** | Enterprise, broad protocol support |
| **WorkOS** | SSO/SAML for enterprise SaaS |
| **Stytch / Hanko** | Passwordless / passkey-first |
| **AWS Cognito** | AWS-native |
| **Firebase Auth** | Firebase-native |
| **Roll your own** | Only with strong justification |

### Payments

| Provider | Best fit |
|----------|---------|
| **Stripe** | Default for most SaaS and direct-to-consumer |
| **Stripe Connect** | Marketplaces |
| **Paddle / Lemon Squeezy** | Merchant-of-record (handles tax for you) |
| **Adyen / Braintree** | Enterprise, regional |
| **Square** | In-person + online (retail / restaurant) |
| **Mollie / Razorpay / MercadoPago** | Regional |

### Email

| Provider | Best fit |
|----------|---------|
| **Resend** | Modern API, devEx-focused |
| **Postmark** | Reliable transactional |
| **SendGrid / SES** | Volume, AWS-native |
| **Loops / Customer.io / Klaviyo** | Marketing + transactional combined |

### Monitoring

| Need | Tool |
|------|-----|
| Error tracking | Sentry |
| Web analytics | Plausible, Fathom, PostHog, GA4 |
| Product analytics | PostHog, Mixpanel, Amplitude, Heap |
| APM / tracing | Datadog, New Relic, Honeycomb |
| Logs | Datadog, Logtail, Axiom, Loki |
| Uptime | Better Stack, Pingdom, UptimeRobot, Checkly |
| Status page | statuspage.io, openstatus, instatus |

### CI/CD

| Tool | Best fit |
|------|---------|
| **GitHub Actions** | Default for GitHub repos |
| **GitLab CI** | GitLab repos |
| **CircleCI** | Mature, configurable |
| **Buildkite** | Self-hosted runners, scale |
| **Vercel / Netlify / Cloudflare Pages** | Native deploys (frontend) |

---

## A note on stack churn

The web changes fast. This catalog will date. Treat it as a *prior*, not a verdict. When in doubt:

1. Pick boring.
2. Pick what the team knows.
3. Pick what you can leave behind without rewriting everything.
