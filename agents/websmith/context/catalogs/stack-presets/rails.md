# Preset — Ruby on Rails (with Hotwire)

Rails 7+ with Hotwire (Turbo + Stimulus), Postgres, Sidekiq, and Tailwind. The "boring is good" full-stack choice for solo founders and small teams who want maximum productivity per developer. Server-rendered UX with reactive sprinkles where they matter.

---

## When it fits

- **Archetypes**: SaaS product, CRUD-heavy apps, marketplaces, community/forum, internal tools, e-commerce (with Spree or custom).
- **Team**: Ruby-comfortable, or a solo founder optimizing for time-to-feature.
- **Interactivity**: medium — Hotwire handles most "live" UX without writing JS.
- **Ops appetite**: moderate; you operate a server.

## When it doesn't

- Pure static content (overkill).
- Team is React/TS-aligned and doesn't want to learn Ruby.
- Edge-first deployment is critical (Rails on Cloudflare Workers isn't really a thing).
- Heavily mobile-first SPA-style apps (Rails can do it, but you're fighting upstream).

## Composition

| Category | Choice |
|----------|--------|
| Framework | **Rails 7+** (latest stable) |
| Language | **Ruby 3+** |
| Frontend | **Hotwire** = Turbo (page partials, streams) + Stimulus (small JS controllers) |
| Styling | **Tailwind CSS** via the official Rails integration; or **PicoCSS** / Bootstrap for traditional approach |
| Asset pipeline | **propshaft** + **importmap-rails** (no Node.js needed) or **esbuild-rails** for richer JS |
| Database | **Postgres** (via Rails's built-in support) |
| Background jobs | **Sidekiq** (default) or **Solid Queue** (Rails-native, no Redis) |
| Cache | **Redis** for Sidekiq; **Solid Cache** for cache without Redis |
| Auth | **Devise** (mature) or **Rails 8 built-in auth** or **Rodauth** (Sequel-based, advanced) |
| Email | **Mailgun** / **Postmark** / **Resend** + Action Mailer |
| File storage | **Active Storage** + S3 / R2 |
| Search | **pg_search** (Postgres FTS) or **Meilisearch** (with `meilisearch-rails`) |
| Real-time | **Action Cable** (websockets) + Turbo Streams |
| Analytics | **Plausible** or **Ahoy** (self-host) |
| Error tracking | **Sentry** or **Honeybadger** |
| Feature flags | **Flipper** |

## Hosting

- **Fly.io** (default; multi-region, stateful, Rails-friendly).
- **Render** (Heroku-like, simple).
- **Hatchbox**, **Kamal** (deploy to your own VPS — DHH's approach, very cost-effective).
- **Heroku** (mature, expensive).
- **Self-host on a $5 VPS** with Kamal for the lowest-cost route.
- DB co-located with the app; backup via host or `pg_dump` to S3.

## CI/CD

- **GitHub Actions**: RuboCop, RSpec or Minitest, Brakeman (security), bundler-audit (deps).
- **Capybara** for e2e (or Playwright via gem).
- **System tests** for golden paths.
- Deploy via **Kamal** (zero-downtime container deploy) or platform-native (Fly, Render).
- DB migrations via `rails db:migrate` in a release phase / Kamal hook.

## Cost profile

- **Solo / side project on Kamal + VPS**: $5–$20/month.
- **Render / Fly small app**: $15–$50/month.
- **Heroku Hobby**: starts $7/month dyno + addons.

## Watch-outs

- **Hiring**: Ruby pool is smaller than JS/TS; plan accordingly if you'll grow a team.
- **Sidekiq Pro / Enterprise** for advanced features (rate limits, batches) is paid; Solid Queue avoids that.
- **Memory pressure**: Rails apps are not lightweight. Plan ~512MB–1GB per process.
- **Hotwire's reactivity ceiling**: It's expressive but not infinite. If you find yourself fighting it, audit whether a tiny React/Stimulus island is more honest than a Turbo Frame gymnastics.
- **i18n**: Excellent built-in support, but design for it from day one rather than retrofitting.

## Customization points

- **Replace Devise with Rails 8 built-in auth** for newer projects (simpler).
- **Replace Sidekiq with Solid Queue** if you want to drop Redis.
- **Swap PostgreSQL for SQLite** for very small apps deploying via Kamal — Rails 8 makes this viable.
- **Add React via importmap or esbuild** for one specific page if Hotwire isn't enough; resist the urge to do this site-wide.
- **Add Stimulus components library** for richer client-side patterns.

## Pair with

- `domains/saas.md` for billing (Stripe via `pay` gem), multi-tenancy patterns, admin.
- `domains/marketing-landing.md` for the marketing site — often a separate static site (Astro) on a subdomain.
- `domains/ecommerce.md` if you're building a custom store with Spree (an e-commerce Rails framework).
