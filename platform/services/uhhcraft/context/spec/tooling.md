# Tooling

> Phase 3 artifact.
> Status: awaiting user approval.

> **agent-cloud alignment (operative overrides):** This document is the original
> standalone WebSmith spec and is preserved verbatim for historical reference.
> Where it names **Kamal**, **SSH/rsync deploys**, **systemd units**, or
> **GitHub Actions `DEPLOY_SSH_KEY`** as the deployment mechanism, those are
> **superseded** by agent-cloud's actual model: **Semaphore-orchestrated Ansible**
> playbooks template `.env` from **OpenBao** and run `deploy.sh`/`post-deploy.sh`
> against **Podman** containers fronted by **central Caddy**. See
> [`SPEC.md` → Alignment with agent-cloud conventions](SPEC.md#alignment-with-agent-cloud-conventions).

---

## Rendering strategy

- **Primary:** Server-rendered MPA (Multi-Page App). Go renders full HTML pages per request via templ templates. No client-side routing.
- **JS island exception:** `/canvas/[id]` page — Three.js 3D viewer is a self-contained JS island. All surrounding chrome (header, action bar, item summary) is server-rendered.
- **Interactivity layer:** HTMX swaps server-returned HTML partials for dynamic behaviors (filter toggle, cart updates, cooldown timer, form validation). No JS framework for site shell.
- **Rationale:** Backend-first is an explicit constraint. The vast majority of the site is read-heavy server-rendered content (catalog, account pages, checkout). HTMX covers all interactive needs except the 3D canvas. Single Go binary, trivial ops, LCP < 1s target met easily on server-rendered pages.

---

## Frontend

- **Language:** Go 1.23+
- **Templating:** [templ](https://github.com/a-h/templ) — compiled, type-safe Go component templates. Catch template errors at compile time, not runtime.
- **Interactivity:** [HTMX](https://htmx.org) — server returns HTML partials; HTMX swaps them in-place. Used for: filter bar, cart add/remove/update, generation cooldown UI, form validation feedback, radial selector price recalculation.
- **3D canvas:** [Three.js](https://threejs.org) — loaded only on `/canvas/[id]`. Bundled as a standalone ES module via esbuild. Handles: mesh loading (GLB), rotation (OrbitControls), texture application (sticker mockup), accept/reject interaction.
- **HTTP router:** [Chi](https://github.com/go-chi/chi) — lightweight, idiomatic Go router. Middleware support for auth, logging, rate limiting.
- **Why Go + templ + HTMX over alternatives:** Directly matches team skills (Go is in the team's wheelhouse). Single binary deployment. No Node runtime on the server. HTMX covers all interactive requirements without a SPA framework. templ catches template errors at build time — safer than text/template. The only JS on the page is HTMX (14KB) + Three.js (canvas page only).

---

## CSS / styling

- **Approach:** Utility-first with Tailwind CSS.
- **Tooling:** Tailwind CSS v4 standalone CLI binary — no Node.js required on the server. Run `tailwindcss --input src/input.css --output static/css/app.css --watch` during development. Commit the compiled CSS.
- **Design tokens:** Tailwind theme config encodes brand tokens (light blue + orange palette, spacing scale, type scale). Phase 4 will define the exact values.
- **Why Tailwind over vanilla CSS:** Design tokens live in one place (tailwind.config). templ components reference utility classes directly — no context-switching between template and stylesheet. Standalone CLI means no Node dependency for the Go project.
- **Alternative rejected:** CSS Modules — adds a build step and module resolution complexity without meaningful benefit given templ's component model.

---

## Backend

- **Pattern:** Custom backend — required by the site's complexity (auth, AI job queue, order management, fulfillment routing, rate limiting).
- **Language:** Go 1.23+
- **Framework:** Chi router + stdlib `net/http`. No full framework (no Gin, no Echo) — Chi gives routing and middleware without abstracting `net/http`.
- **AI worker service:** Two separate Python 3.11+ / FastAPI sidecars (one per engine) on the AI machines (internal network). Each exposes a single normalized `POST /generate` endpoint (the route is `/generate`, not `/generate/image` or `/generate/3d` — see the [sidecar contract](../architecture/ai-sidecar-contract.md)):
  - `inference-comfyui` `POST /generate` — runs Flux.1 via ComfyUI, writes the PNG to its MinIO, returns a Caddy-routed `url`.
  - `inference-hunyuan3d` `POST /generate` — runs Hunyuan3D, writes GLB + STL to its MinIO, returns Caddy-routed `glb_url` + `stl_url`.
- **Job queue:** [River](https://riverqueue.com) — Postgres-backed job queue for Go. Go app enqueues generation jobs; a River worker goroutine in the same binary (or separate binary) picks them up and calls the Python AI service via local network HTTP. No Redis required for the job queue — Postgres already in the stack.
- **Why Go for backend:** Team knows Go, Go is in the team's skills. Single binary, trivial self-hosted deployment. Chi is battle-tested. River is the best Go job queue today (Postgres-backed, no Redis dependency for jobs).
- **Why Python for AI:** The AI ecosystem (ComfyUI, Hunyuan3D, Diffusers) is Python-native. Running it as a separate service behind a FastAPI interface gives clean separation — Go doesn't need to know about ML dependencies.

---

## Database(s)

- **Primary:** PostgreSQL 16 — self-hosted on main server (or dedicated DB machine on local network).
- **Go driver:** [pgx](https://github.com/jackc/pgx) v5 — native Postgres driver, better performance than `database/sql`.
- **Query generation:** [sqlc](https://sqlc.dev) — generates type-safe Go from SQL queries. Write SQL, get Go. No ORM magic.
- **Migrations:** [goose](https://github.com/pressly/goose) — simple, SQL-based migrations. Run as a pre-deploy step.
- **Secondary — cache + rate limiting:** Redis 7 — self-hosted on main server. Used for:
  - Generation cooldown counters (per-session and per-account, with TTL expiry).
  - Session store (via scs with Redis backend).
- **Multi-tenancy:** Single-tenant (one shop).
- **Why Postgres:** Already decided in intake. Best relational DB for the job. pgx + sqlc gives full type safety without ORM overhead.
- **Why Redis for rate limiting:** Atomic increment + TTL is the correct primitive for cooldown counters. Postgres can do this but Redis is simpler and faster for ephemeral counters. Already self-hosted, trivial ops.

---

## Auth

- **Pattern:** Self-built session auth in Go.
- **Session management:** [scs](https://github.com/alexedwards/scs) (Go session manager) with Redis or Postgres session store.
- **Password hashing:** bcrypt (stdlib `golang.org/x/crypto/bcrypt`).
- **Email verification:** On sign-up, generate a signed token, email a verification link. Token verified server-side before account activation.
- **Password reset:** Token-based (signed, time-limited). Same pattern as email verification.
- **Roles:** Single `role` column on the `users` table — values: `user`, `admin`. Admin bypasses rate limiting, order charges (test mode), and has fulfillment routing capabilities.
- **Guest checkout:** Implemented as a guest session (no DB user record). Order linked to email only. Cart is session-cookie-backed for guests, account-linked for users.
- **OAuth / social login:** Not at launch. Can be added later (go-oauth2 or goth library).
- **Why self-built:** OSS requirement, no Clerk (JS-centric), no Auth0 (vendor cost). Go session auth is well-understood, the scs library is production-proven, and the requirements (email/password + guest checkout + 2 roles) are simple enough that a self-built implementation is safe and maintainable.
- **Alternative rejected:** Authboss — more complex than needed; adds a significant dependency for a simple auth shape.

---

## Storage / files

- **Object store:** [MinIO](https://min.io) — self-hosted, S3-compatible API. Runs on main server (or a NAS on local network). Stores:
  - Accepted generation assets (PNG for stickers, GLB + STL for 3D prints).
  - Catalog item assets (3D models, images).
  - Scratch-pad assets (in-progress generations, purged after TTL if not accepted).
- **Go client:** AWS SDK for Go v2 (`aws-sdk-go-v2/service/s3`) pointed at MinIO endpoint. Code is S3-compatible — can migrate to Backblaze B2 or AWS S3 with a config change if needed.
- **Image pipeline:** Static image serving via MinIO presigned URLs (or direct public bucket for catalog images). No external image CDN at launch — LCP < 1s is met by keeping catalog images small and properly optimized at upload time.
- **Video:** N/A.
- **Why MinIO:** Self-hosted, zero ongoing cost, S3-compatible (no vendor lock-in in code), OSS, trivial deployment (single binary or Docker container).

---

## Email / messaging

- **Transactional email:** [Resend](https://resend.com) — simple HTTP API, generous free tier (3,000 emails/month), excellent deliverability. Go client: `resend-go`.
  - Emails sent: order confirmation, account welcome, email verification, password reset, order status updates.
- **Discord webhook:** Order placed → Stripe webhook → Go handler → Discord webhook POST (outgoing HTTP). Uses Discord's incoming webhook URL. Notification includes order number, item, total.
- **Marketing email:** None in scope.
- **SMS / push:** None in scope.
- **Why Resend over SES/Postmark:** Simple API, modern, developer-friendly, free tier covers early-stage volume. No infrastructure to manage. Alternative: Postmark (equally good, similar pricing).

---

## Search

- **Approach:** None at launch. Catalog discovery via filter bar (client-side toggle by product type + server-rendered category listing). Postgres full-text search can be added if catalog grows large.
- **Rationale:** Catalog is small at launch; browse + filter is sufficient. Adding Meilisearch or Postgres FTS is a straightforward future addition when catalog warrants it.

---

## CMS / content

- **Pattern:** Database-driven catalog, no CMS.
- **Catalog authoring:** Operator manages catalog items directly via Postgres (psql or a DB GUI like TablePlus). Each catalog item record: name, description, product_type (sticker/print), base_price, material_options (JSONB), cut_finish_options (JSONB), model_path (MinIO key), thumbnail_path (MinIO key), category, active boolean.
- **Static copy (About, Legal):** Hardcoded in templ templates. Updated via code deploy.
- **Showcase gallery:** Curated via a DB table (`showcase_items`) with a reference to a catalog item or generation asset. Admin sets `featured = true`.
- **Why no CMS:** Content authors are the 2-person operator team. No non-technical writers. DB + direct edit is faster and simpler than a CMS for this volume.

---

## E-commerce

- **Platform:** Self-built on Go + Postgres + Stripe. Not Shopify — justified because: (1) the generative product flow is entirely custom and cannot be expressed as standard Shopify products; (2) strong OSS preference; (3) self-hosted requirement; (4) Shopify's headless API adds complexity without value here.
- **Payments:** [Stripe](https://stripe.com) — Payment Element (supports card, Apple Pay, Google Pay). Stripe handles PAN tokenization; we never touch raw card data (SAQ A scope).
- **Tax:** [Stripe Tax](https://stripe.com/tax) — automatic US sales tax calculation and nexus tracking. Enabled on the Stripe checkout session. Handles state-level rates and nexus thresholds automatically.
- **Shipping:** Flat rate at launch (configured in DB, applied at checkout). Shippo integration deferred until carrier-calculated rates are needed.
- **Inventory:** Each generated item is one-of-a-kind (qty = 1 per generation). Catalog items have no inventory cap at launch — made-to-order. Oversell prevention: generated items are locked to a specific order on "Add to Cart" (not just "Accept"), preventing two users from ordering the same generation.
- **Discounts:**
  - Bulk account discount: tracked via a `bulk_discount_eligible` flag set by order history. Auto-applied at checkout for eligible accounts.
  - Bulk threshold (units or value): **OPEN — resolve in Phase 5.**
- **Priority manufacturing queue:** Account orders get a `priority = true` flag in the `orders` table. Fulfillment workflow respects this ordering. No additional tooling needed.
- **Alternative rejected:** Medusa.js — Node-based, conflicts with backend-first + no-JS-heavy-framework philosophy. Saleor — Python/GraphQL, adds significant complexity.

---

## Fulfillment routing

Two fulfillment paths, triggered per order line item:

### In-house fulfillment
- Manufacturing asset (PNG or STL) retrieved from MinIO by order ID.
- Admin notified via Discord webhook.
- Manual production + shipping.

### Third-party fulfillment — Stickers
- Provider: **Printful** (primary) or **Printify** (fallback).
- Trigger: Admin marks order line item `fulfillment_route = 'printful'` (or automatic rule: e.g., "if queue depth > N, route to Printful").
- Flow: Go service calls Printful REST API → creates order → uploads PNG asset → Printful ships directly to customer.
- Printful API: `POST /orders` with product variant + file URL (presigned MinIO URL or public URL).

### Third-party fulfillment — 3D Prints
- Provider: **Shapeways** API or **Hubs (Protolabs Network)** API.
- Trigger: Same admin flag or automatic rule.
- Flow: Go service calls Shapeways/Hubs API → uploads STL from MinIO → sets material + quantity → creates order → provider ships to customer.
- Note: Printful/Printify **do not** handle custom 3D prints. This is a separate integration.

### Fulfillment routing service (Go)
- `FulfillmentRouter` interface with implementations: `InHouseFulfiller`, `PrintfulFulfiller`, `ShapewaysFulfiller`.
- Admin sets route per order line item; router dispatches accordingly.
- All routing events logged to DB and Discord webhook.

---

## AI services

### Image generation (stickers)

- **Model:** Flux.1 Schnell (quantized fp8 for 12GB VRAM) — open-source, Apache 2.0, best-in-class output quality for stylized/cartoon images.
- **Interface:** ComfyUI server running on AI machine(s), accessed via local network HTTP (`http://192.168.x.x:8188`). ComfyUI exposes a REST API for queuing workflows and retrieving results.
- **Workflow:** Prompt → ComfyUI → Flux.1 inference → PNG with transparency (alpha channel for die-cut shape) → written to MinIO → job result returned to Go via River job completion.
- **Hardware:** RTX 5070 (12GB VRAM). Flux.1 Schnell quantized: ~2–6 second inference per image at this VRAM level.
- **Output format:** PNG, minimum 1024×1024, with alpha channel for die-cut shape.

### 3D model generation (3D prints)

- **Model:** [Hunyuan3D](https://github.com/Tencent/Hunyuan3D-2) (Tencent, open-source) — currently the best open-source text/image-to-3D model. Uses the lite variant optimized for ≤16GB VRAM.
- **Interface:** FastAPI service on AI machine(s), wrapping Hunyuan3D inference. Accessible via local network HTTP (`http://192.168.x.x:8001`).
- **Workflow:** Text prompt → Hunyuan3D → GLB mesh (for 3D preview) + STL export (for manufacturing) → both written to MinIO → job result returned to Go.
- **Hardware:** RTX 5070 (12GB VRAM). Hunyuan3D lite is designed for this VRAM class. Inference time: 15–60 seconds per model depending on quality setting.
- **Output formats:** GLB (Three.js preview), STL (manufacturing asset for 3D printer / Shapeways).

### Multiple AI machines
- Multiple RTX 5070 machines can be used in parallel by registering multiple worker addresses in config.
- River job dispatcher round-robins or least-load-selects an available AI machine.
- Each machine runs its own ComfyUI + Hunyuan3D services.
- No orchestration needed at this scale — config list of IP:port pairs.

---

## Analytics / monitoring

- **Web analytics:** [Umami](https://umami.is) — self-hosted, open-source, privacy-respecting, no cookies required for basic analytics. Runs on main server as a lightweight Node service (the one justified Node process in the stack). Alternative: Plausible Cloud if self-hosting Umami feels like extra ops.
- **Error tracking:** [Sentry](https://sentry.io) — free tier covers small projects. Go SDK (`getsentry/sentry-go`). Captures panics, errors, and performance traces.
- **Uptime monitoring:** [Better Stack](https://betterstack.com) — external uptime checks (pings from outside the local network). Alerts via Discord webhook + email if site goes down. Free tier covers basic checks.
- **Logs:** `slog` (Go stdlib structured logging) → stdout → systemd journal on the server. `journalctl -fu uhhcraft` for tailing. Sufficient for a small self-hosted deployment.
- **APM / tracing:** Not at launch. OpenTelemetry instrumentation can be added later if performance investigation is needed.
- **Status page:** Better Stack includes a hosted status page. Expose at `status.uhhcraft.uhstray.io`.

---

## Hosting / deployment

### Physical infrastructure

```text
Internet
    │
[Router — port 80/443 → Main Server]
    │
[Main Server]                    [AI Machine 1]          [AI Machine 2...]
 ├── Go app (uhhcraft binary)     ├── ComfyUI + Flux.1     ├── ComfyUI + Flux.1
 ├── Caddy (reverse proxy + TLS)  └── Hunyuan3D FastAPI    └── Hunyuan3D FastAPI
 ├── PostgreSQL 16
 ├── Redis 7
 ├── MinIO (object storage)
 └── Umami (analytics)
```

### Main server

- **OS:** Ubuntu 24.04 LTS or Debian 12.
- **Reverse proxy:** [Caddy](https://caddyserver.com) — automatic HTTPS via Let's Encrypt, simple config, handles TLS certificate renewal. Config: `uhhcraft.uhstray.io → localhost:3000`.
- **Process management:** systemd unit file for the Go binary. Caddy managed by systemd. Postgres + Redis managed by systemd (package-installed).
- **Deployment:** [Kamal](https://kamal-deploy.org) — zero-downtime deploys from GitHub Actions via SSH. Builds Go binary in CI, pushes as Docker image or binary, Kamal handles the cutover.
- **Static IP requirement:** The local network's public IP must be static (or use a dynamic DNS service like Cloudflare Tunnel or ddclient with the `uhstray.io` DNS).

### AI machines

- **OS:** Ubuntu 24.04 LTS (CUDA-compatible).
- **CUDA:** NVIDIA driver + CUDA 12.x.
- **Services:** ComfyUI as systemd service (auto-restarts). Hunyuan3D FastAPI as systemd service.
- **Network:** Local network only — no public exposure. Main server calls AI machines via local IP.
- **Deployment:** SSH + rsync or Ansible playbook for AI service updates. Not on the critical deploy path.

### DNS / domain

- **Domain:** `uhstray.io` (already owned).
- **Record:** `uhhcraft.uhstray.io` A → public static IP of local network.
- **TLS:** Caddy handles Let's Encrypt automatically.

### Uptime expectation

- **Target:** 99.5% (honest for self-hosted local network — ~43 hours/year downtime budget).
- **Main risks:** ISP outage, power outage, hardware failure.
- **Mitigations:** UPS on main server (power), Better Stack alerting (detection), Kamal zero-downtime deploys (deploy-time outage eliminated), Postgres daily backups to MinIO (data recovery).
- **Note:** The original 99.99% target is not achievable on local self-hosted without a dedicated ops team. 99.5% is the honest ceiling for this setup. If uptime becomes critical post-launch, the migration path is: containerize → deploy to Fly.io or Render — no code changes required.

---

## Build tooling

- **Go build:** `go build -ldflags="-s -w" -o bin/uhhcraft ./cmd/server` — produces a small static binary.
- **templ:** `templ generate` — run before `go build`. Committed generated files.
- **CSS:** Tailwind CSS standalone CLI — `tailwindcss -i src/input.css -o static/css/app.css --minify` for production.
- **JS (3D canvas only):** esbuild — bundles Three.js + canvas interaction code into `static/js/canvas.js`. No Node package manager for the site shell; esbuild used only for the canvas bundle.
- **Package manager:** Go modules (`go.mod`). No Node package manager in the Go project.
- **Monorepo:** No — single repo: Go app + templ templates + Python AI service in `ai/` subdirectory.
- **Language versions:** Go 1.23+ pinned in `go.mod`. Python 3.11+ pinned in `ai/pyproject.toml`.

---

## Code quality / DX

- **Type checking:** Go's type system (compiled). Python: `mypy` for AI service.
- **Linter:** `golangci-lint` (Go) — includes `staticcheck`, `errcheck`, `govet`. `ruff` (Python AI service).
- **Formatter:** `gofmt` (Go stdlib, non-negotiable). `black` (Python).
- **Pre-commit hooks:** [lefthook](https://github.com/evilmartians/lefthook) — runs `gofmt`, `golangci-lint`, `templ generate` check, `go test ./...` on pre-commit.
- **Editor config:** `.editorconfig` at repo root. `.vscode/extensions.json` recommending templ, Go, Tailwind, and Python extensions.
- **Dev hot reload:** [air](https://github.com/air-verse/air) — watches Go files + templ files, rebuilds and restarts on change. `templ generate --watch` runs alongside.

---

## Testing

- **Unit:** `go test ./...` — Go stdlib testing. Table-driven tests for business logic (pricing, discount calculation, fulfillment routing).
- **Integration:** Go tests with `testcontainers-go` — spins up a real Postgres + Redis in Docker for integration tests. Tests DB queries, auth flows, order creation.
- **E2E:** [Playwright](https://playwright.dev) — browser-based tests for the full purchase flow, generation flow, checkout. Run against a local dev server.
- **Accessibility:** `axe-core` via `@axe-core/playwright` — run accessibility checks in Playwright tests on key pages (home, canvas, checkout, account). Catches WCAG AA violations automatically.
- **Performance:** Lighthouse CI in GitHub Actions — runs against a deployed staging instance (or dev server). Enforces LCP < 1s budget.
- **Visual regression:** Not at launch. Chromatic or Percy can be added if visual regressions become a problem.

---

## CI/CD

- **Provider:** GitHub Actions (confirmed in intake).
- **Pipeline:**
  ```text
  Push / PR
    ├── go vet + golangci-lint
    ├── templ generate (verify no uncommitted changes)
    ├── go test ./... (unit + integration via testcontainers)
    ├── Playwright E2E (against dev build)
    ├── Lighthouse CI (performance budget check)
    └── Build binary + Tailwind CSS + esbuild canvas bundle
  
  Merge to main
    └── Kamal deploy to main server via SSH
  ```
- **Preview deployments:** Not applicable (self-hosted; no Vercel preview URLs). Feature branches tested locally.
- **Branch model:** Trunk-based development — short-lived feature branches, merge to `main`, deploy from `main`.
- **Required checks:** `go vet`, `golangci-lint`, `go test` must pass before merge. Playwright E2E on main only (to avoid slow CI on every PR).
- **Deployment strategy:** Kamal zero-downtime — starts new container, health-checks it, swaps traffic, stops old.
- **Rollback:** `kamal rollback` — redeploys the previous image. Takes ~30 seconds.

---

## Secrets / config

- **Production secrets:** Environment variables in `/etc/uhhcraft/env` on the main server, loaded by systemd. Not committed to git.
- **CI/CD secrets:** GitHub Actions repository secrets (STRIPE_SECRET_KEY, RESEND_API_KEY, DISCORD_WEBHOOK_URL, DEPLOY_SSH_KEY, etc.).
- **Local dev:** `.env` file at repo root (git-ignored). Loaded by `godotenv` in development.
- **Secret categories:**
  - Stripe secret key + webhook signing secret
  - Resend API key
  - Discord webhook URL
  - MinIO root credentials
  - Postgres connection string
  - Redis connection string
  - Session secret (random 32-byte key)
  - AI service URLs (local IPs — not secret but env-configurable)
  - Printful API key, Printify API key
  - Shapeways / Hubs API key

---

## Feature flags

- **None at launch.** Not warranted for a small team and single-tenant shop. Add GrowthBook (self-hosted, OSS) if A/B testing becomes relevant post-launch.

---

## Internationalization tooling

- **None.** English only — explicitly decided in Phase 1.

---

## Accessibility tooling

- **Linting:** None for Go/templ (no eslint-plugin-jsx-a11y equivalent for templ — manual care required in templates).
- **CI checks:** axe-core via Playwright (see Testing section). Runs against key pages on every main branch deploy.
- **Manual testing plan:** No screen reader testing by the team (noted in intake). axe-core CI checks provide the automated WCAG AA verification layer.

---

## Security / compliance tooling

- **Dependency scanning:** [Dependabot](https://docs.github.com/en/code-security/dependabot) — enabled on GitHub repo for Go modules and Python packages.
- **SAST:** `golangci-lint` with security linters (`gosec`) included in the CI pipeline.
- **Secret scanning:** GitHub native secret scanning — catches accidentally committed API keys.
- **WAF / bot mitigation:** Caddy with rate limiting middleware. Cloudflare free tier (proxy mode) as an option for DDoS protection at the DNS layer — does not violate self-hosted requirement (traffic still reaches your server, Cloudflare just proxies/filters).
- **PCI DSS scope:** SAQ A — Stripe Payment Element handles all card data. UhhCraft never touches raw PAN. Stripe tokenizes in the browser.
- **TLS:** Caddy + Let's Encrypt. TLS 1.2+ enforced. HSTS header set.

---

## Other integrations

| Integration | Purpose | Go library |
|-------------|---------|------------|
| Stripe | Payments, Stripe Tax, webhooks | `stripe-go` |
| Resend | Transactional email | `resend-go` |
| Discord webhook | Payment + fulfillment notifications | stdlib `net/http` POST |
| MinIO / S3 | Object storage (generation assets, catalog) | `aws-sdk-go-v2/service/s3` |
| ComfyUI | Image generation API on AI machines | stdlib `net/http` (REST) |
| Hunyuan3D FastAPI | 3D model generation API on AI machines | stdlib `net/http` (REST) |
| Printful | Sticker overflow fulfillment | `net/http` (REST API) |
| Printify | Sticker overflow fulfillment (fallback) | `net/http` (REST API) |
| Shapeways | 3D print overflow fulfillment | `net/http` (REST API) |
| Better Stack | Uptime monitoring + status page | External (no code integration) |
| Sentry | Error tracking | `sentry-go` |
| Umami | Web analytics | External (JS snippet, loaded in templ layout) |

---

## Stack diagram

```text
                        ┌─────────────────────────────────────────────┐
                        │             LOCAL NETWORK                   │
                        │                                             │
  Internet              │  ┌──────────────────┐                      │
  ──────────────────►   │  │   MAIN SERVER     │                      │
  uhhcraft.uhstray.io   │  │                  │                      │
  (public static IP)    │  │  Caddy (TLS)     │                      │
                        │  │  Go binary       │◄──── HTTP (local) ───┤
                        │  │  PostgreSQL 16   │                      │
                        │  │  Redis 7         │  ┌────────────────┐  │
                        │  │  MinIO           │  │  AI MACHINE(S) │  │
                        │  │  Umami           │  │                │  │
                        │  └────────┬─────────┘  │  ComfyUI       │  │
                        │           │             │  + Flux.1      │  │
                        │           │             │                │  │
                        │           │             │  Hunyuan3D     │  │
                        │           │             │  FastAPI       │  │
                        │           │             └────────────────┘  │
                        └───────────┼─────────────────────────────────┘
                                    │
                    ┌───────────────┴──────────────────┐
                    │         EXTERNAL SERVICES         │
                    │                                   │
                    │  Stripe (payments + tax)          │
                    │  Resend (email)                   │
                    │  Discord (webhooks)               │
                    │  Printful / Printify (stickers)   │
                    │  Shapeways / Hubs (3D prints)     │
                    │  Better Stack (uptime)            │
                    │  Sentry (errors)                  │
                    │  GitHub Actions (CI/CD)           │
                    └───────────────────────────────────┘
```

---

## Alternatives considered and rejected

| Alternative | Rejected because |
|-------------|-----------------|
| Shopify | Cannot express the generative product flow as standard Shopify products; self-hosted requirement; strong OSS preference |
| Medusa.js / Saleor | Node/Python e-commerce frameworks; Medusa conflicts with backend-first + no-JS-heavy-framework; Saleor adds GraphQL complexity |
| Next.js / SvelteKit / Nuxt as site shell | SPA frameworks for the whole site violate the backend-first philosophy; Go + templ + HTMX covers the same requirements with better alignment to team skills |
| Vercel / Netlify / Fly.io hosting | User explicitly wants self-hosted local network |
| Clerk / Auth0 | Clerk is JS-centric; Auth0 is proprietary/vendor cost; self-built Go session auth meets requirements |
| Supabase | Introduces vendor dependency for DB + auth; Postgres self-hosted is simpler for this team |
| Django + HTMX | Python is a secondary skill; Go is primary; single binary deployment is simpler than Python wsgi/asgi |
| Rails | Ruby not in team skills |
| Managed AI APIs (Replicate, Fal, Meshy) | User has RTX 5070 hardware on local network; self-hosted = zero per-generation cost |
| esbuild / Vite for full frontend | Only the canvas page needs a JS bundle; full bundler pipeline not warranted |

---

## Open questions

| # | Question | Resolves |
|---|----------|---------|
| OQ-NEW-1 | Exact material + cut-type options per product type (requires research for catalog radial selectors) | Before build |
| OQ-NEW-2 | Bulk discount threshold — what order quantity or value qualifies? | Phase 5 |
| OQ-NEW-3 | Social media links — does UhhCraft have accounts? | Phase 5 |
| OQ-NEW-6 | Shipping — flat rate amount(s), or free above a threshold? | Phase 5 |
| OQ-NEW-7 | Static IP situation — does the local network have a static public IP, or is dynamic DNS (ddclient + Cloudflare) needed? | Before build |
| OQ-NEW-8 | Umami self-hosted vs Plausible Cloud — is running Umami on main server acceptable, or prefer external? | Phase 5 / preference |
| OQ-NEW-9 | Printful vs Printify as primary sticker overflow provider — do you have an existing account with either? | Before build |
| OQ-NEW-10 | Shapeways vs Hubs (Protolabs) as 3D print overflow — any existing relationship or preference? | Before build |
