# Preset — Starlight Documentation Site

**Starlight** (an Astro-based documentation framework) for product or open-source documentation. Ships with search, sidebar nav, dark mode, versioning patterns, code highlighting, and i18n out of the box. The fastest path to a credible, maintainable docs site.

---

## When it fits

- **Archetypes**: documentation (open-source library, SaaS product, API reference, internal handbook).
- **Team**: any team that can write markdown; no React/Astro experience required to author content.
- **Audience**: developers, technical implementers, sometimes non-technical end users.
- **Why Starlight**: search, dark mode, sidebar, copy-to-clipboard, and a sensible default layout are *already done*. You focus on writing.

## When it doesn't

- The docs are a few pages — `astro-static` with manual templates may be simpler.
- You're already deep in a Docusaurus / VitePress / MkDocs Material ecosystem (don't migrate without reason).
- You need a CMS with non-markdown editors for non-technical authors (consider headless CMS + custom site).

## Composition

| Category | Choice |
|----------|--------|
| Framework | **Starlight** (Astro-based) |
| Content | **Markdown / MDX** in repo (`src/content/docs/`) |
| Search | **Pagefind** (built-in, static, client-side, no third-party) |
| Code highlighting | **Shiki** (Starlight default — uses VS Code themes) |
| Sidebar | Auto-generated from file structure or hand-configured |
| Theme | Starlight's defaults; customize via CSS variables + frontmatter |
| Versioning | Multiple sidebars + `versioned-docs/` directory pattern, or branch-based |
| API reference | **TypeDoc** for TS, **Sphinx** + **sphinx-build → MD**, or hand-written |
| Diagrams | **Mermaid** (MDX integration) |
| Interactive examples | Embed **CodeSandbox**, **StackBlitz**, **Sandpack** |
| Analytics | **Cloudflare Web Analytics** (cookieless) or **Plausible** |
| Forms (if any) | Cloudflare Worker + Resend, or external (Tally, Formspree) |

## Hosting

- **Cloudflare Pages** (default; fast, free, generous limits).
- **Netlify** or **Vercel** also excellent.
- DNS via Cloudflare.

## CI/CD

- **GitHub Actions**:
  - **Spell-check** with `cspell`.
  - **Link-check** with `lychee` (catches broken external links).
  - **Build** to verify nothing is structurally broken.
  - **TypeScript code-block compile-check** (custom action) if shipping a TS library — prevents code-sample rot.
  - **Lighthouse CI** thresholds (95+ on all categories is achievable).
  - **axe-core** for accessibility regressions.
- Cloudflare Pages auto-deploys on push: preview per PR, production on merge.
- Rollback: previous Cloudflare deployment, one click.

## Cost profile

- **$0–$15/month**: domain + nothing else for most projects.
- Scales to ~100k visits/mo on free tiers.

## Watch-outs

- **Versioning approach**: Pick early. Retrofitting versioning across hundreds of pages is painful.
- **Search relevance tuning**: Pagefind is good defaults. For larger doc sets, consider Algolia DocSearch (free for OSS) or Meilisearch.
- **Mobile reading experience**: Test it. Developers read docs on phones too.
- **i18n**: Starlight supports it well, but translation drift is a problem at scale. Plan a workflow (Crowdin, Lokalise, Tina i18n).
- **Edit-on-GitHub**: Configure the link in Starlight config; one of the most valuable docs features for OSS.

## Customization points

- **Swap Starlight for VitePress** if the team is Vue-heavy.
- **Swap for Docusaurus** for React-component-heavy docs with interactive widgets.
- **Swap for Nextra** if docs live next to a Next.js product.
- **Swap for Mintlify** if a polished commercial doc product is acceptable (proprietary, paid).
- **Swap for MkDocs Material** for Python projects or teams that prefer YAML config and a Python toolchain.
- **Add a CMS layer** (Sanity, Tina) for non-technical authors editing markdown.

## Pair with

- `domains/documentation.md` for the full docs-specific considerations checklist (versioning policy, API reference automation, contribution workflow, search analytics).
