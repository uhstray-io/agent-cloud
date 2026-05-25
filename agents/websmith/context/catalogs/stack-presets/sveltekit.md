# Preset — SvelteKit

SvelteKit with TypeScript, Tailwind, and a typed data layer. Smaller bundles than React-based stacks, ergonomic forms, and progressive enhancement out of the box. Strong choice for teams who prefer Svelte's syntax and want minimal client-side JavaScript.

---

## When it fits

- **Archetypes**: SaaS product, marketing sites, content sites with light interactivity, dashboards, e-commerce storefronts.
- **Interactivity**: medium to high; forms-heavy apps benefit from SvelteKit's progressive-enhancement story.
- **Team**: Svelte-aligned or willing to learn (a small but pleasant ecosystem).
- **Ops appetite**: low-to-moderate.

## When it doesn't

- Team has heavy React investment (use `nextjs-typescript`).
- Need a library that only exists for React (less common but still happens — check first).
- Pure static content (use `astro-static`).
- Server-rendered Ruby/Python culture (`rails` / `django`).

## Composition

| Category | Choice |
|----------|--------|
| Framework | **SvelteKit** (Svelte 5 / runes) |
| Language | **TypeScript** (strict mode) |
| Styling | **Tailwind CSS** + **shadcn-svelte** or **Skeleton** UI components |
| State | Svelte stores; **runes** ($state, $derived) for fine-grained reactivity |
| Forms | SvelteKit form actions (progressive enhancement out of the box) + **Zod** or **Valibot** for validation |
| ORM | **Drizzle** (TypeScript-first) |
| Database | **Postgres** via Neon, Supabase, or self-hosted |
| Auth | **Lucia** (lightweight self-host) or **Auth.js for SvelteKit** or **Supabase Auth** |
| Email | **Resend** |
| File storage | **Cloudflare R2** or **S3** |
| Search | **Meilisearch**, **Typesense**, or Postgres FTS |
| Analytics | **PostHog** or **Plausible** |
| Error tracking | **Sentry** (Svelte SDK) |
| i18n | **Paraglide** (compile-time, type-safe) |

## Hosting

- **Cloudflare Pages + Workers** (excellent SvelteKit adapter).
- **Vercel** (native adapter; good if you want preview deploys with zero config).
- **Netlify** (also a good adapter).
- **Self-host via Node adapter** for full control.
- DB on Neon, Supabase, or self-hosted Postgres.

## CI/CD

- **GitHub Actions**: lint (Prettier + ESLint), type-check (svelte-check + tsc), unit (Vitest), e2e (Playwright).
- Host-native previews per PR.
- Lighthouse CI thresholds.
- DB migrations via Drizzle Kit.

## Cost profile

- **Tiny project**: ~$0/month (Cloudflare Pages free + Neon free + Resend free).
- **Growing app**: $50–$300/month at moderate traffic.
- Cloudflare's pricing model is generous; SvelteKit's small output keeps you under thresholds longer.

## Watch-outs

- **Smaller ecosystem.** Some niche libraries exist only for React. Check before committing for projects with unusual integrations.
- **Svelte 5 / runes is recent.** Tutorials are catching up; some docs still show pre-runes patterns.
- **Form actions are server-bound.** Great when you have a server; static-only SvelteKit deployments need extra plumbing.
- **i18n choices.** Paraglide is excellent but new — confirm it covers your translation workflow.

## Customization points

- **Replace Tailwind with vanilla CSS** or with **UnoCSS** if you prefer atomic styles with less configuration.
- **Swap Lucia for Auth.js** if you want broader provider support.
- **Add tRPC for SvelteKit** if you need typed contracts beyond what form actions provide.
- **Add Stripe** for billing (see `domains/saas.md`).

## Pair with

- `domains/saas.md` for billing, RBAC, multi-tenancy.
- `domains/marketing-landing.md` for marketing portions.
- `domains/ecommerce.md` for storefronts (`shopify-custom-theme` is often simpler for small catalogs).
