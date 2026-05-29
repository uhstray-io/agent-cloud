# Phase 3 — Tooling

> *What stack will actually build, run, host, and operate this site?*

You now know what the site is for (phase 1) and what it must contain (phase 2). This phase chooses the implementation stack: rendering strategy, frontend framework, CSS approach, backend, database, auth, integrations, hosting, build tooling, testing, and CI/CD.

Tooling decisions are the most reversible-feeling and least-reversible-in-practice decisions in the workflow. Take them seriously.

---

## 1. Goal of this phase

Produce `spec/tooling.md`: a fully specified stack, justified against purpose and template requirements, with explicit alternatives considered and rejected. Phase 4 (Style) needs to know the styling layer; phase 5 (Considerations) needs to know hosting, integrations, CI/CD.

---

## 2. Inputs

- `spec/intake.md` (if Phase 0 ran) — existing vendor accounts, team skills, must-use / must-not-use rules, regions, cost ceilings.
- `spec/purpose.md`
- `spec/template.md`
- [`catalogs/stack-presets/`](../catalogs/stack-presets/README.md) — present 2–3 candidate presets + appropriate domain overlay before customizing.

Before drafting, enumerate inherited constraints per [AGENTS.md §5](../AGENTS.md#5-constraint-propagation-matrix).

---

## 3. Decisions to extract

### 3.1 Rendering strategy

Pick the rendering model(s). Some sites mix.

- **Static (SSG)** — pre-rendered at build time. Best for content-heavy, infrequently-changing sites (marketing, docs, blogs).
- **Server-rendered (SSR)** — rendered per request on the server. Best for personalized, auth-gated, or rapidly changing content.
- **Client-rendered (CSR / SPA)** — JS bundle renders in the browser. Best for app-like experiences.
- **Hybrid / ISR / streaming** — mix of the above. Common with modern meta-frameworks.
- **No-JS / progressive enhancement** — works without JavaScript, JS enhances.
- **Multi-page app (MPA)** — traditional server pages, possibly with light JS.

Map this back to the template: how much of phase 2 is static vs dynamic vs personalized? That drives the answer.

### 3.2 Frontend framework

Pick one (or zero):

- **No framework** — HTML/CSS/JS. Viable for small sites and high-control needs.
- **Meta-framework** — Next.js, Nuxt, SvelteKit, Remix, Astro, Qwik, SolidStart, etc. Most common for modern multi-page sites.
- **UI library** — React, Vue, Svelte, Solid, Lit, etc., without a meta-framework (rare; usually paired with one).
- **Server-driven** — Rails, Django, Laravel, Phoenix LiveView, HTMX, etc.
- **Static site generator** — Hugo, Jekyll, Eleventy, Zola, etc.
- **Web Components / native**
- **WebAssembly-based** — Yew, Leptos, Blazor (rarely a default; pick deliberately)

See [`catalogs/stacks.md`](../catalogs/stacks.md) for archetype-aligned combinations.

Consider: team skills, hiring market, ecosystem maturity, ecosystem direction, hosting compatibility, performance characteristics, learning curve.

### 3.3 CSS / styling approach

- **Vanilla CSS** + naming convention (BEM, OOCSS)
- **Utility-first** — Tailwind, UnoCSS
- **CSS Modules**
- **CSS-in-JS** — styled-components, Emotion, vanilla-extract, Panda
- **Preprocessor** — Sass, Less, PostCSS
- **Component library** — Material UI, Chakra, Mantine, shadcn/ui, Radix + custom, daisyUI
- **CSS framework** — Bootstrap, Bulma, Foundation
- **Design system** — existing internal one, or new one built atop primitives

Style choices in phase 4 must be expressible in whatever you pick here. If the user wants heavy custom motion + brand-specific aesthetics, a rigid component library may fight you.

### 3.4 Backend: needed at all?

Answer first: does this site need a backend you build, or is a backend-less stack sufficient?

- **None** — purely static.
- **Serverless functions only** — auth, form submissions, webhooks; no persistent server.
- **BaaS** — Supabase, Firebase, Appwrite, Pocketbase; handles auth + DB + storage.
- **Headless services** — Stripe, Auth0, Algolia, Sanity — stitched together; no custom backend.
- **Custom backend** — full server you write.

### 3.5 Backend language and framework

(If a custom backend is needed.)

- Node (Express, Fastify, Hono, NestJS), Bun, Deno
- Python (Django, FastAPI, Flask)
- Ruby (Rails, Sinatra)
- Go (stdlib, Gin, Echo, Fiber)
- Rust (Axum, Actix)
- Elixir / Phoenix
- Java / Kotlin (Spring)
- C# / .NET
- PHP (Laravel, Symfony)
- Other

Decision drivers: team familiarity, hiring market, performance envelope, ecosystem fit (Stripe, ML, etc.), hosting compatibility, ops model.

### 3.6 Database(s)

- **None** — content is files / external services.
- **Relational** — Postgres, MySQL, MariaDB, SQLite, CockroachDB, etc.
- **Document** — MongoDB, DynamoDB, Firestore, etc.
- **Key-value** — Redis, Cloudflare KV, Upstash, etc.
- **Search-optimized** — Elasticsearch, OpenSearch, Algolia, Meilisearch, Typesense.
- **Time-series / analytics** — ClickHouse, BigQuery, DuckDB, Snowflake.
- **Vector / embeddings** — pgvector, Pinecone, Weaviate, Qdrant.
- **Multi-tenant strategy** — single DB, schema-per-tenant, DB-per-tenant.

Migrations approach (e.g., Prisma Migrate, Drizzle, Atlas, Flyway, Alembic).

### 3.7 Auth

- **None** — anonymous-only.
- **Self-built** — sessions, tokens, password hashing, MFA. Don't underestimate this.
- **Provider** — Auth0, Clerk, Supabase Auth, AWS Cognito, Firebase Auth, NextAuth/Auth.js, WorkOS, Stytch, Hanko, etc.
- **SSO** — SAML, OIDC, LDAP for enterprise.
- **Social login** — which providers.
- **Magic link / passwordless / passkeys**

### 3.8 Storage / files

- Object storage — S3, R2, GCS, Azure Blob, Backblaze B2
- Image handling / CDN — Cloudinary, imgix, Cloudflare Images, native CDN with transforms
- Video — Mux, Cloudflare Stream, Bunny, self-hosted, YouTube/Vimeo embeds
- Large file uploads (multipart, resumable)

### 3.9 Email / messaging

- Transactional email — Resend, Postmark, SendGrid, SES, Mailgun, Loops
- Marketing email — Mailchimp, Customer.io, Klaviyo, Loops, ConvertKit
- SMS / push — Twilio, OneSignal, Knock
- In-app notifications

### 3.10 Search

- Site search — none, browser-native, JS index (Lunr, FlexSearch, Pagefind), Algolia, Meilisearch, Typesense, Elastic, DB FTS
- Federated / multi-source
- Autocomplete, typo tolerance, relevance tuning

### 3.11 CMS / content

- **No CMS** — files in repo.
- **Git-based** — Decap (Netlify CMS), Tina, Sveltia, Keystatic.
- **Headless** — Sanity, Contentful, Strapi, Storyblok, Payload, Directus, Hygraph, Prismic, Cosmic.
- **Traditional** — WordPress (headless or classic), Drupal, Ghost.
- **Markdown + frontmatter** in repo.

### 3.12 E-commerce stack (if applicable)

- **Hosted platform** — Shopify, BigCommerce, Squarespace Commerce.
- **Headless commerce** — Shopify Storefront API, BigCommerce, commercetools, Saleor, Medusa, Vendure, Swell.
- **Self-built** — Stripe + custom catalog + custom checkout (significant work; justify it).
- **Payments processor** — Stripe, Adyen, Braintree, Square, Mollie, PayPal, regional (Razorpay, MercadoPago, etc.).
- **Tax / compliance** — Stripe Tax, Avalara, TaxJar.
- **Shipping** — Shippo, EasyPost, ShipStation, carrier-direct.
- **Inventory** — platform-native, ERP integration, custom.

### 3.13 Analytics / monitoring / observability

- **Analytics** — Plausible, Fathom, Simple Analytics, PostHog, GA4, Mixpanel, Amplitude.
- **Product analytics** — PostHog, Mixpanel, Heap, June.
- **Error tracking** — Sentry, Rollbar, Bugsnag, Honeybadger.
- **APM / tracing** — Datadog, New Relic, Honeycomb, Grafana Tempo.
- **Logs** — Datadog, Loki, Logtail, Axiom.
- **Uptime** — Better Stack, Pingdom, UptimeRobot, Checkly.
- **Real user monitoring** — Sentry, Datadog RUM, Cloudflare Web Analytics.

### 3.14 Hosting / deployment

Frontend / static:
- Vercel, Netlify, Cloudflare Pages, GitHub Pages, AWS S3 + CloudFront, Azure Static Web Apps, Render, Fastly, Akamai, Bunny CDN.

Server / dynamic:
- Vercel, Netlify Functions, Cloudflare Workers, AWS (Lambda, ECS, EC2, App Runner), GCP (Cloud Run, GKE), Azure, Fly.io, Render, Railway, Heroku, DigitalOcean App Platform, self-hosted.

Database hosting:
- Neon, Supabase, PlanetScale, Railway, RDS, Cloud SQL, Atlas, Turso, self-hosted.

Region(s), data residency, edge vs origin, multi-region failover.

### 3.15 Build tooling and package manager

- Bundler / build — Vite, esbuild, Rollup, Webpack, Turbopack, Parcel, Bun.
- Package manager — npm, pnpm, yarn, bun.
- Monorepo — Turborepo, Nx, Moon, pnpm workspaces, lerna.
- Language version pinning (Node, Python, etc.).

### 3.16 Code quality and DX

- Type checking — TypeScript, Flow, JSDoc, Sorbet, Pyright, mypy.
- Linter — ESLint, Biome, Ruff, Clippy, rubocop.
- Formatter — Prettier, Biome, Black, gofmt.
- Pre-commit — husky + lint-staged, lefthook, pre-commit.
- Editor config — `.editorconfig`, `.vscode/`, recommended extensions.

### 3.17 Testing

- Unit — Vitest, Jest, pytest, RSpec, etc.
- Component — Testing Library, Vue Test Utils, Storybook + interactions.
- Integration — supertest, MSW, testcontainers.
- End-to-end — Playwright, Cypress, WebdriverIO.
- Visual regression — Percy, Chromatic, Argos, BackstopJS.
- Accessibility — axe-core, Pa11y, Lighthouse CI.
- Performance — Lighthouse CI, WebPageTest, k6.
- Type tests, contract tests, load tests as appropriate.

### 3.18 CI/CD

- CI provider — GitHub Actions, GitLab CI, CircleCI, Buildkite, Bitbucket Pipelines.
- Preview deployments per PR
- Branch protections, required checks
- Release strategy — trunk, gitflow, release branches
- Deployment strategy — immediate, blue/green, canary, feature-flag-gated
- Rollback path

### 3.19 Secrets and configuration

- Secret manager — Vault, Doppler, AWS Secrets Manager, GCP Secret Manager, 1Password Secrets Automation, Infisical, platform-native env vars.
- Per-environment config (dev / staging / prod)
- Local dev secrets handling

### 3.20 Feature flags / experimentation

- LaunchDarkly, Statsig, GrowthBook, PostHog, Unleash, Flagsmith, OpenFeature
- A/B testing infra
- Kill switches

### 3.21 Internationalization tooling

(Phase 2 said *whether* i18n. Phase 3 picks *how*.)

- i18next, FormatJS / react-intl, vue-i18n, next-intl, paraglide, lingui, transifex, crowdin.

### 3.22 Accessibility tooling

- axe-core CI integration
- Storybook a11y addon
- Linter rules (eslint-plugin-jsx-a11y)
- Manual testing checklist (NVDA, VoiceOver, JAWS)

### 3.23 Compliance and security tooling

Driven by what was surfaced in phase 1 (regulations) and what phase 5 will revisit.

- WAF — Cloudflare, AWS WAF
- DDoS / bot mitigation
- SAST / dependency scanning — Snyk, GitHub Advanced Security, Dependabot, Renovate
- Secret scanning — GitHub native, trufflehog
- SOC 2 / ISO tooling — Vanta, Drata, Secureframe (if relevant)

### 3.24 AI / ML services (if applicable)

- LLM provider — Anthropic, OpenAI, Google, AWS Bedrock, self-hosted
- Vector DB (covered above)
- Embeddings provider
- Eval / observability — Langsmith, Helicone, Braintrust

### 3.25 Other integrations

The user will have purpose-specific ones. Examples by archetype:

- Maps — Google Maps, Mapbox, MapLibre, OSM
- Reviews — Trustpilot, Yotpo, Okendo
- Live chat — Intercom, Crisp, Drift, Zendesk, HelpScout
- Booking — Calendly, Cal.com, custom
- Forms — Formspree, Tally, Typeform, custom
- Webhooks ingest — Svix, custom

---

## 4. Question script

1. *"What does your team already know and want to keep using?"*
2. *"Any accounts or vendors already in place — Stripe, AWS, Shopify, anything?"*
3. *"What can't this site afford to do? Bundle size? Cold-start latency? Region restrictions?"*
4. *"Who will operate this after launch? You? An agency? A team?"*
5. *"Build budget vs run budget — both matter."*
6. *"Where do users live? Where can data live?"*
7. *"How critical is uptime? Are we talking 99.9, 99.99, best-effort?"*
8. *"What integrations are mandatory at launch?"*
9. *"Is there anything I haven't asked about that you think matters?"*

---

## 5. Decision principles

- **Boring beats clever** unless cleverness is justified by purpose. Recovering from a misadventure in a niche stack is expensive.
- **Match team skills**. The best stack for a solo founder who knows Rails is Rails.
- **Prefer composability over coupling**. Replacing one piece later should be possible.
- **Avoid lock-in proportional to switching cost**. Lock-in is fine when the trade is worth it (Shopify saves months for a small store); avoid it when it isn't.
- **Justify in writing**. Every choice should have a one-line "we picked X over Y because Z."

See [`catalogs/stacks.md`](../catalogs/stacks.md) for representative stack picks by archetype.

---

## 6. Output artifact: `spec/tooling.md`

````markdown
# Tooling

## Rendering strategy
- Primary: <SSG | SSR | CSR | hybrid | MPA | progressive>
- Per-page exceptions:
- Rationale:

## Frontend
- Framework / library:
- Version pin:
- Why this over alternatives:

## CSS / styling
- Approach:
- Library / system:
- Tokens / design system:
- Why:

## Backend
- Pattern: <none | serverless only | BaaS | headless services | custom>
- Language:
- Framework:
- Why:

## Database(s)
- Primary: <type, vendor>
- Secondary: <if any>
- Migrations:
- Multi-tenancy strategy:
- Why:

## Auth
- Provider / pattern:
- Methods: <password | magic link | passkeys | SSO | social>
- Sessions / tokens:
- Why:

## Storage / files
- Object store:
- Image pipeline:
- Video:

## Email / messaging
- Transactional:
- Marketing:
- Other channels:

## Search
- Approach:
- Provider:

## CMS / content
- Pattern:
- Tool:
- Authoring workflow:

## E-commerce (if applicable)
- Platform:
- Payments:
- Tax:
- Shipping:
- Inventory:

## Analytics / monitoring
- Web analytics:
- Product analytics:
- Error tracking:
- APM / tracing:
- Logs:
- Uptime:
- RUM:

## Hosting / deployment
- Frontend / static:
- Server / functions:
- DB hosting:
- Region(s):
- Data residency:
- Edge or origin:

## Build tooling
- Bundler:
- Package manager:
- Monorepo: <yes/no, tool>
- Language versions pinned:

## Code quality / DX
- Type checking:
- Linter:
- Formatter:
- Pre-commit hooks:

## Testing
- Unit:
- Component:
- Integration:
- E2E:
- Visual regression:
- Accessibility:
- Performance:

## CI/CD
- Provider:
- Preview deployments:
- Branch model:
- Required checks:
- Deployment strategy:
- Rollback:

## Secrets / config
- Manager:
- Per-environment:

## Feature flags
- Provider:

## Internationalization tooling
- Library:
- Translation workflow:

## Accessibility tooling
- Linting:
- CI checks:
- Manual testing plan:

## Security / compliance tooling
- Dependency scanning:
- SAST:
- WAF / bot mitigation:
- Compliance frameworks:

## Other integrations
- <name>: <purpose>

## Stack diagram

```
<ASCII or prose diagram of components and how they connect>
```

## Alternatives considered and rejected
- <X>: rejected because <reason>
- <Y>: rejected because <reason>

## Open questions
````

---

## 7. Exit criteria (phase gate)

Follow the [Phase gate protocol in AGENTS.md §4](../AGENTS.md#4-phase-gate-protocol). The phase exits when every box below is checked AND the user explicitly approves.

### 7.1 Inherited constraints

Before drafting, enumerate:
- From `spec/intake.md`: team skills, vendor accounts, regions, cost ceilings, hosting preferences.
- From `spec/purpose.md`: archetype (drives stack-preset shortlist), regulations (region constraints), lifespan (boring vs ambitious).
- From `spec/template.md`: interactivity needs, auth-gated pages, i18n, content authoring source, real-time regions.

Then propose **2–3 candidate presets** from `catalogs/stack-presets/` (plus relevant `domains/` overlays) before the user picks.

### 7.2 Artifact completeness
- [ ] Every category in §3 has either a chosen tool or an explicit "N/A — [reason]."
- [ ] Each major choice (frontend, styling, backend, DB, auth, hosting, CI/CD) has a one-line rationale.
- [ ] At least one alternative-rejected line exists for each of: frontend framework, hosting, payments (if applicable), CMS (if applicable).
- [ ] The chosen stack can deliver everything `spec/template.md` requires. Explicitly verify each major template requirement against the stack.
- [ ] Hosting, region(s), and data-residency are decided — not deferred.
- [ ] CI/CD is decided — pipeline shape and required checks named.
- [ ] Secrets management approach is named.
- [ ] Testing layers chosen (unit / component / integration / e2e / a11y / perf).
- [ ] Cost ceiling sanity-check: rough monthly run cost fits within intake's budget.
- [ ] Migration path noted for irreversible choices (auth, payments, DB).

### 7.3 Catch-all
- [ ] Asked verbatim: *"Is there anything I haven't asked about that you think matters for this site?"*

### 7.4 Downstream constraints to flag at the gate

- Rendering strategy (SSG / SSR / CSR / hybrid) → Style (what tokens express), Considerations (SEO indexability, caching).
- Styling system (Tailwind / CSS modules / shadcn / etc.) → Style (token shape, theming approach).
- Hosting + region → Considerations (data residency, latency, DR, cost).
- CMS choice → Considerations (content authoring workflow).
- Auth provider → Considerations (session policy, MFA requirement, SSO).
- Payments / commerce stack → Considerations (PCI scope, tax, fraud, returns).
- Analytics / monitoring tools → Considerations (event taxonomy, alerting routes).

### 7.5 Approval

User must reply with "approved", "next", or equivalent.

### 7.6 If you need to revise this phase later

A revision here triggers re-validation of: Style → Considerations. May also trigger a Template revision if the stack genuinely can't deliver the original template — in which case loop back to Phase 2.

---

## 8. Common traps

- **Picking the trending stack** without checking team skills.
- **Underestimating auth.** "We'll roll our own" usually shouldn't.
- **Forgetting the run-cost vs build-cost tradeoff.** Free-tier dev / expensive-tier prod can sink a small project.
- **Skipping testing infra.** "We'll add tests later" rarely happens.
- **CI/CD as an afterthought.** Decide it before launch, not after the first prod incident.
- **Choosing tools that can't deliver the template.** If the template needs heavy interactivity, an SSG with no JS won't cut it.
- **Decision-by-vendor.** Vendor lock-in is sometimes worth it — but make it a *decision*, not a default.
