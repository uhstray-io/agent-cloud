# Preset — Django (with HTMX)

Django 5+ with HTMX for server-driven interactivity, Postgres, Celery for background work, and Tailwind. The Python-team default. Strong choice when the team or the product overlaps with data/ML/analytics workloads.

---

## When it fits

- **Archetypes**: SaaS product, internal tools, content platforms with admin needs, data/ML-adjacent products, educational platforms, government/civic.
- **Team**: Python-aligned. Often the case when the product also involves ML, data pipelines, or scientific computing.
- **Interactivity**: medium — HTMX handles most live UX server-side.
- **Ops appetite**: moderate.

## When it doesn't

- Highly mobile-app-like SPA UX (Django can do it via DRF + a separate frontend, but you're not benefiting from the preset).
- Team has no Python investment.
- Need edge-first deployment.

## Composition

| Category | Choice |
|----------|--------|
| Framework | **Django 5+** |
| Language | **Python 3.11+** |
| Templating | Django templates (server-rendered) |
| Interactivity | **HTMX** (server-driven partial updates) + **Alpine.js** for tiny client state |
| Styling | **Tailwind CSS** via `django-tailwind` or **picoCSS** for classless |
| Forms | Django forms + **django-htmx** + **django-formset** for advanced patterns |
| Database | **Postgres** (Django's default with full feature support) |
| ORM | Django ORM (mature, robust) |
| Background jobs | **Celery** + **Redis** broker, or **Django Q2** (simpler, DB-backed) |
| Auth | Django's built-in auth + **django-allauth** for social/SSO/MFA |
| Admin | Django Admin (the killer feature; customize via `django-admin-interface` or `django-unfold`) |
| Email | **django-anymail** + Resend / Postmark / Mailgun |
| File storage | **django-storages** + S3 / R2 |
| Search | **Postgres FTS** + `django-watson` or `django-postgres-search`; **Meilisearch** for richer needs |
| Static assets | **WhiteNoise** (simple) or CDN-served |
| API (when needed) | **Django REST Framework** or **django-ninja** (FastAPI-style) |
| Analytics | **Plausible** or **Posthog** (Python SDK) |
| Error tracking | **Sentry** |

## Hosting

- **Fly.io** (good fit; multi-region; stateful).
- **Render** (Heroku-like).
- **Railway** (quick deploys).
- **DigitalOcean App Platform** or **self-hosted VPS** with `gunicorn` + `nginx`.
- Database: managed Postgres on the same provider, or Neon / Supabase.
- Static and media: WhiteNoise for static, S3-compatible for user media.

## CI/CD

- **GitHub Actions**: `ruff` (lint + format), `mypy` (optional type checking), `pytest-django` (tests), `bandit` (security), `safety` or `pip-audit` (deps).
- **Playwright** for e2e or Django's built-in `LiveServerTestCase`.
- Deploy via platform-native push, or Docker + `gunicorn` to your own host.
- DB migrations via `python manage.py migrate` as a release step.

## Cost profile

- **Solo project on a $5 VPS**: $5–$15/month.
- **Render / Fly small app**: $15–$50/month.
- **DigitalOcean App Platform basic**: $12/month.

## Watch-outs

- **Static files in production.** WhiteNoise is simple but not a CDN. For media-heavy sites, front with Cloudflare or use a real CDN.
- **Celery setup adds Redis and a worker.** For small projects, Django Q2 avoids the Redis dependency.
- **GIL and concurrency.** For high-concurrency workloads, consider ASGI (Django ASGI + `uvicorn`/`daphne`) or move hot paths to FastAPI.
- **Admin in production.** Django Admin is powerful — protect it (separate URL, IP allow-list or VPN, MFA mandatory).
- **Form CSRF and HTMX**: configure `django-htmx` and CSRF tokens carefully to avoid 403s.

## Customization points

- **Swap to FastAPI** if the app is API-first and the team doesn't need Django Admin. See `python-fastapi.md` (not in this preset set, but a logical sibling).
- **Swap HTMX for Vue/React** on specific complex pages if pure server-side won't do.
- **Add Wagtail** if you need a rich CMS atop Django.
- **Add `django-tenants`** for multi-tenant SaaS with separate schemas per tenant.

## Pair with

- `domains/saas.md` for billing (`dj-stripe` is mature), RBAC, multi-tenancy.
- `domains/documentation.md` if shipping docs on the same site (Wagtail or MkDocs sibling).
- `domains/marketing-landing.md` — marketing site is often a separate static site (Astro).
