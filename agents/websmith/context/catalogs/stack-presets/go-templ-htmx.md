# Preset — Go + templ + HTMX

Go with the **templ** templating library and **HTMX** for server-driven interactivity. Compiles to a single static binary. Tiny memory footprint, near-zero cold start, trivial ops. The choice when you want the simplicity and reliability of Go with web UX.

---

## When it fits

- **Archetypes**: internal tools, server-rendered content sites, dashboards, server-heavy APIs with admin UIs, B2B / enterprise tools, CLI-adjacent web tools.
- **Team**: Go-comfortable; values single-binary deploys and tight ops.
- **Interactivity**: medium — HTMX swaps partials; works well for forms, lists, dashboards.
- **Ops appetite**: very low — one binary, no Node, no Python, no Ruby.

## When it doesn't

- Heavy client-side state, animation, or app-shell UX (consider `nextjs-typescript`).
- Team has no Go experience and isn't keen to learn.
- The product is mostly content with minimal interactivity (`astro-static` is simpler).
- Ecosystem-heavy needs (e.g., Stripe Billing portal, OAuth providers galore) — Go has bindings but the JS ecosystem is denser.

## Composition

| Category | Choice |
|----------|--------|
| Language | **Go 1.22+** |
| HTTP | **chi** router or stdlib `net/http` |
| Templating | **templ** (`a-h/templ`) — compiled, typed Go components |
| Interactivity | **HTMX** + small **Alpine.js** sprinkles when needed |
| Styling | **Tailwind CSS** (run the CLI; commit the compiled CSS) or vanilla CSS |
| Database | **Postgres** with **pgx** + **sqlc** (generated typed queries) |
| Migrations | **goose** or **golang-migrate** |
| Auth | **scs** (sessions) or **lucia-go** equivalents; or self-built session + bcrypt; or **Authboss** |
| Email | Standard SMTP libs + Resend / Postmark via their HTTP APIs |
| File storage | S3-compatible via AWS SDK |
| Search | Postgres FTS, or Meilisearch Go client |
| Background jobs | **river** (Postgres-backed; recommended) or **asynq** (Redis-backed) |
| Logging | `slog` (stdlib structured logging) |
| Tracing | OpenTelemetry Go SDK → Honeycomb / Tempo / Datadog |
| Analytics | **Plausible** |
| Error tracking | **Sentry** Go SDK |

## Hosting

- **Fly.io** (excellent for Go — small, fast deploys).
- **Cloud Run** (serverless Go on GCP).
- **Self-host on a $5 VPS** — a Go binary + systemd is the leanest possible deploy.
- **Kamal** if you want zero-downtime deploys to your own server.
- DB: managed Postgres (Neon / Supabase) or self-hosted Postgres co-located with the app.

## CI/CD

- **GitHub Actions**: `gofmt`, `go vet`, `golangci-lint`, `staticcheck`, `go test ./...`.
- **templ generate** as a CI step (and a pre-commit hook).
- **Playwright** or `chromedp` for e2e.
- Build: `go build -ldflags="-s -w"` for small static binaries.
- Deploy: push container, or `scp` the binary + systemd reload, or Kamal.
- DB migrations: separate `migrate up` step before app deploy.

## Cost profile

- **VPS deployment**: $5/month covers small-to-mid projects.
- **Fly small app**: $10–$30/month.
- **Cloud Run**: pay-per-request; effectively free for low traffic.

## Watch-outs

- **templ requires generation.** Add `templ generate` to dev workflow and CI. Commit the generated files? Most teams commit them; a few don't.
- **Tailwind CLI separately.** No Node? You still need the Tailwind CLI binary (standalone version available).
- **Smaller ecosystem for paid services.** Stripe is fine; some niche SaaS only document Node/Python.
- **Form re-render UX with HTMX**: keep mental model crisp — server is the source of truth, client just swaps partials.
- **No hot-reload by default.** Use `air` or `templ generate --watch` + `wgo` for dev experience.

## Customization points

- **Swap chi for Echo, Gin, Fiber, or `net/http`** based on team preference.
- **Swap sqlc for GORM** if you prefer ORM ergonomics over generated queries (slower compile, more runtime cost).
- **Add a tiny React/Svelte island** via esbuild if one specific surface needs richer client UX.
- **Replace Postgres with SQLite** for very small apps; `mattn/go-sqlite3` or `modernc.org/sqlite` (pure-Go).

## Pair with

- `domains/saas.md` for billing (stripe-go is robust), RBAC patterns.
- `domains/marketing-landing.md` if the marketing site is separate (Astro alongside).
- Less common pairing with `domains/ecommerce.md` — Shopify is usually simpler for storefronts; pair Go with custom order/fulfillment backends behind a Shopify front.
