# Preset — Next.js + TypeScript

The default modern full-stack JavaScript stack. Next.js App Router with TypeScript, Tailwind, a typed ORM, Postgres, and a modern auth provider. Hosted on Vercel or Cloudflare. Covers marketing + product app on a single codebase. Largest ecosystem of any frontend choice.

---

## When it fits

- **Archetypes**: SaaS product (marketing + app), marketplaces, content platforms with auth, medium-to-large e-commerce (headless), heavy editorial sites with personalization.
- **Interactivity**: high — forms, real-time updates, optimistic UI, charts, drag-drop.
- **Team**: comfortable with React and TypeScript; can manage a larger dependency surface.
- **Ops appetite**: low-to-moderate; willing to lean on managed services.

## When it doesn't

- Pure static content (overkill — `astro-static`).
- Team has zero React experience (mismatch).
- Strong "no JS framework" requirement (use `rails`, `django`, or `go-templ-htmx`).
- Bundle-size obsession (Svelte/Astro are smaller; Next has improved but isn't the leanest).

## Composition

| Category | Choice |
|----------|--------|
| Framework | **Next.js** (App Router, latest stable) |
| Language | **TypeScript** (strict mode) |
| Styling | **Tailwind CSS** + **shadcn/ui** (copy-paste components over Radix) |
| State / data fetching | React Server Components first; **TanStack Query** for client cache when needed |
| Forms | **React Hook Form** + **Zod** (validation) |
| ORM | **Drizzle** (preferred for type-first) or **Prisma** (mature) |
| Database | **Postgres** via Neon, Supabase, or PlanetScale (MySQL on PS) |
| Auth | **Clerk** (fast UX, paid past free tier) or **Auth.js / NextAuth** (self-host, free) |
| Email | **Resend** (transactional) |
| Background jobs | **Inngest** or **Trigger.dev**; or **Vercel Cron + Queues** |
| File storage | **Cloudflare R2** or **AWS S3** |
| Image pipeline | Next.js Image with a vendor optimizer (Vercel-native or Cloudinary) |
| Search | **Algolia**, **Meilisearch**, or Postgres full-text + pg_trgm |
| Analytics | **PostHog** (product + web) |
| Error tracking | **Sentry** |
| Feature flags | **PostHog** or **Statsig** |

## Hosting

- **Vercel** (default for App Router + RSC; tight integration; expensive at scale).
- **Cloudflare Pages + Workers** (cheaper at scale; some App Router edges need testing).
- **Self-host** on Fly.io, Render, or AWS for highest control.
- DB on Neon (serverless Postgres) or Supabase (Postgres + auth + storage + realtime).

## CI/CD

- **GitHub Actions**:
  - Lint (ESLint or Biome), type-check (tsc), unit (Vitest), e2e (Playwright on a separate workflow).
  - Visual regression via Chromatic or Argos (optional but valuable for marketing pages).
  - Lighthouse CI thresholds.
- Vercel-native preview deployments per PR.
- Database migrations via Drizzle Kit / Prisma Migrate in a separate, gated job.
- Rollback: previous Vercel deployment one click; DB migrations require forward fixes.

## Cost profile

- **Tiny side project**: ~$0–$25/month (Vercel free + Neon free + Resend free + Sentry free).
- **Growing SaaS**: $100–$500/month (paid tiers as you cross free thresholds).
- **Heavy traffic**: Watch Vercel function invocations and bandwidth — cost spikes can be sharp. Consider Cloudflare Workers + R2 for cost discipline at scale.

## Watch-outs

- **App Router learning curve.** Server Components, Server Actions, and the file conventions are powerful but unfamiliar. Pair-program the first few features.
- **Vercel lock-in for some features.** Image optimization, ISR, and edge functions behave differently on other hosts. Avoid Vercel-specific APIs unless you intend to stay.
- **Bundle size discipline.** RSC helps, but client components compound. Audit with `@next/bundle-analyzer`.
- **Database connection pooling.** Serverless functions need a pooler (Neon's built-in, or PgBouncer). Don't open a raw connection per request.
- **Auth migrations are painful.** Pick auth carefully — switching providers later is multi-week work.

## Customization points

- **Swap Drizzle ↔ Prisma** based on team preference. Both are excellent.
- **Replace Clerk with Auth.js** if free is more important than UX polish, or with WorkOS for enterprise SSO.
- **Replace Tailwind with vanilla CSS modules** if the team prefers separation.
- **Add tRPC** for fully typed client↔server contracts if you have a large client surface.
- **Add Stripe** for billing (see `domains/saas.md` or `domains/ecommerce.md`).

## Pair with

- `domains/saas.md` for billing, RBAC, multi-tenancy, admin, status pages.
- `domains/ecommerce.md` for full headless commerce.
- `domains/marketing-landing.md` for the marketing site portion of a SaaS.
