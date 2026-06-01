# CLAUDE.md — platform/services/uhhcraft

This file gives Claude Code (and other LLMs) UhhCraft-specific guidance. Read this together with the root [`CLAUDE.md`](../../../CLAUDE.md) — the root file's rules (Semaphore-only deploys, OpenBao-only secrets, etc.) all apply here.

## What UhhCraft is

E-commerce storefront for AI-designed, one-of-a-kind physical goods. Go + templ + HTMX server-rendered MPA backed by Postgres / Redis / MinIO, with two external AI sidecars (`inference-comfyui`, `inference-hunyuan3d`) for image and 3D generation.

UhhCraft is the **first concrete site** built through the WebSmith agent ([`../../../agents/websmith/`](../../../agents/websmith/)) — every architectural decision traces back to the signed spec at [`context/spec/SPEC.md`](context/spec/SPEC.md), with platform-level adjustments captured in that spec's `## Alignment with agent-cloud conventions` section.

## Conventions specific to this service

### Generated code never goes in git

`web/templates/**/*_templ.go` and `internal/db/sqlcdb/*` are **gitignored** (root `.gitignore`). Always run `make templ` and `make sqlc` after `git clone` or when touching `.templ` / `.sql` files. CI regenerates and fails on drift.

When editing a page or component, edit the `.templ` source, run `make templ`, and verify the resulting Go file compiles — do not edit `_templ.go` directly.

**templ v0.3 gotcha:** a content text-run that *begins* with a Go keyword (`for` / `if` / `switch`) is parsed as control flow — even directly after an inline element like `</a>`. If literal copy needs to start with one of those words (e.g. `…</a> for shorter wait times`), wrap it in a string literal expression: `…</a>{ " for shorter wait times" }`. (templ ≤0.2 treated it as text; 0.3 does not.)

### Database changes are migrations + queries, in that order

1. Add a new `db/migrations/NNN_<name>.sql` (goose-style).
2. Add or edit the relevant `db/queries/<area>.sql`.
3. Run `make sqlc` to regenerate the typed client.
4. Use the regenerated `sqlcdb.Queries` methods in Go.

Migration numbers are zero-padded three digits, sequential, never renumbered.

### River queue

River (`riverqueue/river`) runs in-process. Its tables are managed by `river migrate-up`, invoked from `deployment/post-deploy.sh`. The Go app exposes (or **should** expose; see the deployment README outstanding-items list) `./uhhcraft river migrate-up` as a subcommand.

Do not call River's migration tool directly from `make` recipes — it diverges from prod.

### Healthcheck subcommand

`deployment/compose.yml` references `/app/uhhcraft healthcheck`. The Go binary must implement this subcommand: a single HTTP GET to its own `/healthz` returning exit code 0 on 200, 1 otherwise. Add if missing.

### Secrets

UhhCraft reads secrets from the `.env` file at boot. The `.env` is templated by Ansible's `tasks/manage-secrets.yml` from OpenBao. **Never** read directly from OpenBao at runtime in Go code — go through `.env`. (A future enhancement might add AppRole + token rotation; that's tracked as an open question in `WEBSMITH-INTEGRATION-PLAN.md` §4.)

OpenBao paths owned by UhhCraft:

```text
secret/services/uhhcraft                  Master KV — DB / Redis / MinIO / Stripe / session / Resend / Discord
secret/services/ssh/uhhcraft              Per-service SSH keypair
secret/services/approles/uhhcraft         AppRole (if/when needed)
```

### MinIO is local to UhhCraft

UhhCraft's own MinIO stores **catalog assets only**. AI-generated assets live in the inference services' MinIOs and are served via Caddy proxy paths (`/generated/img/*`, `/generated/3d/*`) — see [`context/architecture/ai-sidecar-contract.md`](context/architecture/ai-sidecar-contract.md).

### AI sidecar calls

When UhhCraft calls `AI_IMAGE_SERVICE_URL` or `AI_3D_SERVICE_URL`:

- HTTP only (no gRPC, no shared MinIO).
- Sidecar response includes a public URL routed through central Caddy — store the URL, not the bytes.
- Failures are non-fatal: render an "AI is offline; try later" state, do not 500.
- Rate limiting goes through `internal/ratelimit/` (Redis-backed), not at the sidecar layer.

### deploy.sh and post-deploy.sh are not equivalents

- `deploy.sh` — **container lifecycle only**. No secret gen, no migrations, no API calls. Safe to re-run.
- `post-deploy.sh` — **bootstrap**. Migrations + smoke. Re-runnable but does more.

Ansible orchestrates the order. Never reorganise these two into one script.

### Config is split

- `config/*.toml` — **non-secret**, version-controlled, mounted into the container.
- `.env` — **secrets**, gitignored, templated per-deploy from OpenBao.

If a value is sensitive (key, password, token), it goes in `.env` (and `templates/env.j2`). If it's a tuning knob or catalog metadata, it goes in `config/`.

## What not to do

- Don't commit `_templ.go`, `app.css` (Tailwind output), `bin/`, `dist/`, or `internal/db/sqlcdb/*`.
- Don't add a per-service Caddyfile. UhhCraft is fronted by central Caddy via `templates/caddy-site.j2`.
- Don't add a per-service OpenBao instance. The platform OpenBao is the source of truth.
- Don't merge `deploy.sh` and `post-deploy.sh`.
- Don't hardcode IPs, domains, or credentials. All external endpoints are `.env` references.
- Don't `go install` or `apt-get` inside `deploy.sh`. Tool-chain pins live in the `Dockerfile`.

## Related

- Spec + alignment: [`context/spec/SPEC.md`](context/spec/SPEC.md)
- AI sidecar contract: [`context/architecture/ai-sidecar-contract.md`](context/architecture/ai-sidecar-contract.md)
- Integration plan: [`../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md`](../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md)
- Root conventions: [`../../../CLAUDE.md`](../../../CLAUDE.md)
- Reference deploy (NetBox): [`../netbox/deployment/CLAUDE.md`](../netbox/deployment/CLAUDE.md)
