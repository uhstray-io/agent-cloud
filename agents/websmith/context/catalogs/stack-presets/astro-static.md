# Preset — Astro Static

Astro-based static site with islands of interactivity where needed. Content lives in markdown/MDX in the repo or in a lightweight CMS. Hosted on a global edge CDN. The default for content-first sites: marketing pages, blogs, portfolios, small documentation sites, brochureware.

---

## When it fits

- **Archetypes**: marketing/landing, blog/news, portfolio/personal, small docs, nonprofit, event, restaurant brochure.
- **Content cadence**: weekly to never; not real-time.
- **Interactivity**: occasional islands (newsletter form, image carousel, search) — not an app shell.
- **Team**: 1–3 people; comfortable with markdown; not necessarily React/Vue experts.
- **Ops appetite**: near-zero. Deploy, forget.

## When it doesn't

- App-shell UX with persistent state across navigations.
- Personalized content per user (better with SSR — see `nextjs-typescript.md`).
- Heavy real-time features (chat, live updates).
- Authentication-gated content beyond simple paywalls.

## Composition

| Category | Choice |
|----------|--------|
| Framework | **Astro** (latest stable) |
| Component flavor for islands | Plain Astro components; React/Svelte/Vue when an island needs a richer library |
| Styling | **Tailwind CSS** (or vanilla CSS with custom properties if the team prefers) |
| Content | Markdown / MDX in repo (`src/content/` with content collections) |
| Optional CMS | Git-based (Decap, Tina, Keystatic, Sveltia) for non-technical authors |
| Forms | Web3Forms / Formspree / Netlify Forms / Resend + a lightweight serverless function |
| Search | **Pagefind** (static, client-side, no third-party) |
| Newsletter | Buttondown / Beehiiv / ConvertKit / native ESP |
| Image pipeline | Astro Image + Sharp; or Cloudinary / imgix for heavier needs |
| Analytics | **Plausible** or **Fathom** (privacy-first, cookieless) |
| Error tracking | Sentry browser SDK if there's any interactivity worth instrumenting |

## Hosting

- **Cloudflare Pages** (default), Netlify, or Vercel. All offer free tiers that comfortably host small sites.
- Region: global edge. No need to choose.
- Custom domain via the host's DNS or Cloudflare DNS in front.

## CI/CD

- **GitHub Actions** for any pre-deploy checks (link-check via lychee, type-check, spell-check with cspell).
- **Host-native build** on push: preview per PR, production on merge to main.
- Rollback: one-click previous deployment in the host UI.

## Cost profile

- **$0–$15/month** typical (domain + optional analytics + optional CMS).
- Scales to ~100k–1M visits/mo on free tiers of Cloudflare Pages / Netlify.

## Watch-outs

- **Build time on large content sets.** Astro builds get slow past a few thousand pages. Mitigate with content collections and incremental SSG (or move to hybrid).
- **No native SSR by default.** If a single page needs server rendering, configure Astro's hybrid output for just that route.
- **Image weight discipline.** Static = fast, but only if images are processed. Always use `<Image>` (Astro's component) for responsive `srcset` + AVIF/WebP.
- **MDX complexity.** MDX makes everything interactive — be intentional, or you'll end up with a heavy "static" site.

## Customization points

- **Replace Tailwind with vanilla CSS** if the team prefers control over class names. Style tokens (Phase 4) map cleanly to CSS custom properties.
- **Swap Cloudflare Pages for Netlify or Vercel** if existing accounts argue for it. No code changes.
- **Add a tiny backend** with Cloudflare Workers / Netlify Functions / Vercel Functions when you need a serverless endpoint (form handler, webhook, OG image generator).
- **Drop in a CMS** (Sanity, Contentful, Storyblok) when authors outgrow markdown in git.

## Pair with

- `domains/marketing-landing.md` for lead capture, A/B testing, pixels, attribution.
- `domains/documentation.md` for small doc sites (consider `starlight-docs.md` for medium/large).
