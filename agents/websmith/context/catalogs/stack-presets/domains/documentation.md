# Domain Overlay — Documentation

Apply on top of a base preset (most commonly `starlight-docs.md` or `astro-static.md`) when shipping product or library documentation. Captures choices and tooling that doc sites specifically need beyond the base.

---

## Decisions to layer onto the base preset

### Information architecture
- **Diátaxis** framework split: tutorials (learning) / how-to (task) / reference (information) / explanation (understanding). Pick this or document why not.
- **Sidebar grouping**: by feature, by audience, by stage.
- **TOC strategy**: right-rail per-page, depth (h2 only or h2+h3), sticky vs scroll.
- **Breadcrumbs**: yes (long hierarchies) or no (flat).
- **Search-first vs browse-first** affordance.

### Versioning
- **Strategy**:
  - **Single live version** (latest only); historical via git tags. Simplest.
  - **Multiple live versions** with switcher. Required when users on older releases need correct docs.
  - **Per-release branches** deployed to subdomain (e.g., `v2.docs.example.com`).
- **Default version**: latest stable.
- **Deprecation banners** on older versions.

### Code samples
- **Languages shown**: TS / JS / Python / curl / Go / Ruby — pick the actual user mix.
- **Tabbed code blocks** per language.
- **Copy-to-clipboard** on every block (mandatory).
- **Line highlighting** for emphasis.
- **File path label** above the block.
- **Sample accuracy**: compile-test code samples in CI to prevent rot.
- **"Open in playground"** for substantial samples (CodeSandbox / StackBlitz / Sandpack embeds).

### API reference
- **Source of truth**: OpenAPI spec, GraphQL introspection, TSDoc/TypeDoc, Sphinx docstrings, hand-written.
- **Generator**:
  - **OpenAPI**: Redocly, Stoplight, Mintlify, Scalar.
  - **TypeDoc** for TS libraries.
  - **typedoc-plugin-markdown** → Starlight integration.
- **Try-it-out** interactive (Swagger-style or Mintlify-style).
- **SDK samples** per method.
- **Rate limits and auth** prominently documented.

### Search
- **Pagefind** (default for Starlight): static, client-side, no third-party — good for ~5k pages.
- **Algolia DocSearch**: free for OSS; superior relevance; configure indexing.
- **Meilisearch / Typesense**: self-host with great relevance.
- **Search analytics**: log queries that returned nothing (Pagefind doesn't natively support this — a Worker + KV log captures it).

### Contribution workflow (especially OSS)
- **Edit-on-GitHub** link on every page.
- **CONTRIBUTING.md** for content (style guide, frontmatter, build steps).
- **Style guide** for tone, voice, terminology, code formatting.
- **Glossary** with cross-linking.
- **Embargo / draft preview** for unreleased features.
- **Translation workflow** if multi-lingual.

### Quality gates (in CI)
- **Spell check**: `cspell` with project-specific dictionary.
- **Link check**: `lychee` catches dead external links.
- **TypeScript compile** on code samples (custom action).
- **Lighthouse CI** thresholds (95+ achievable for static docs).
- **axe-core** for accessibility regressions.
- **Vale** for prose style (optional; configure carefully).
- **Markdown lint**: `markdownlint-cli2`.

### Analytics
- **Cloudflare Web Analytics** (cookieless, no banner needed).
- **Plausible** as an alternative.
- **GA4** only if business has it — heavier and triggers cookie banner.
- **Search analytics** (logged separately).

### Doc-site UX patterns
- **Callouts / admonitions**: note, tip, warning, danger — consistent visual.
- **Tables of contents** auto-generated.
- **Prev/next** at end of every doc page, from sidebar order.
- **Last updated** date per page (from git).
- **Section anchors** with hover-link icon for sharing.
- **Mobile sidebar drawer** (always a drawer below md breakpoint).

### Versioning of doc URLs
- **Stable URLs** by feature, not by release.
- **Redirects** for renamed pages (don't break old links).
- **Sitemap** including all versions.
- **Canonical tags** pointing to latest version for archived pages.

### Internationalization (if applicable)
- **Tooling**: Starlight i18n, VitePress i18n, Docusaurus i18n.
- **Workflow**: Crowdin, Lokalise, Phrase, GitLocalize, in-repo branches.
- **Translation drift detection**: flag pages whose English changed after translation was last updated.
- **Fallback display** ("This page hasn't been translated yet — read in English").

### SEO for docs
- **Structured data**: BreadcrumbList, optionally Article.
- **Per-page meta** from frontmatter.
- **OG + Twitter cards**: auto-generated images with page title.
- **Sitemap.xml** including all versions and locales.
- **Robots**: index latest version; consider `noindex` on archived versions to avoid Google preferring old pages.

### Accessibility
- **WCAG AA** is the floor; AAA for navigation if practical.
- **Keyboard-driven everything**: search modal, version switcher, sidebar — all reachable and operable.
- **Skip-to-content** link.
- **Color contrast** in both light and dark themes (especially code highlighting).

---

## Tooling additions per base preset

| If base preset is... | Add these on top |
|----------------------|------------------|
| `starlight-docs` | Mostly defaults; layer your search choice (Pagefind / Algolia DocSearch), API reference generator, contribution workflow. |
| `astro-static` | If docs are small/medium, build it manually with markdown + content collections. Add Pagefind for search. Consider migrating to Starlight if docs grow past ~20 pages. |
| `nextjs-typescript` | Use **Nextra** (Next.js docs framework) or **Fumadocs** for typed docs in Next. Better when docs share components with a marketing or app shell. |
| `rails` / `django` | Docs in a separate static site, not in the app. Don't mix concerns. |

---

## Common doc-specific traps

- **Underinvesting in search.** Users will leave if they can't find. Test it.
- **Stale code samples.** Compile-check in CI. Prefer typed languages where checking is automatic.
- **No versioning until you need it badly.** Plan the strategy day one.
- **Inconsistent tone.** Style guide solves this; enforce it in review.
- **Hidden mobile UX gaps.** Developers read docs on phones. Test mobile.
- **No edit-on-GitHub link.** OSS contributors will not fork to fix a typo.
- **Search results that point to v1 when v3 is latest.** Indexability and canonicals matter.

---

## Reference

For the full documentation considerations checklist, see [`catalogs/considerations-catalog.md` → Documentation](../../considerations-catalog.md#documentation).
