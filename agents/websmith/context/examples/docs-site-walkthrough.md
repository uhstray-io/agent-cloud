# Example Walkthrough — Documentation Site

Condensed end-to-end transcript of an agent building a documentation site for an open-source library.

> **User**: Devon, maintainer of an open-source TypeScript validation library called *zodiac*. ~5K weekly npm downloads. The docs currently live in a sprawling README and a handful of `/docs/*.md` files. They want a real docs site.

---

## Phase 1 — Purpose

**Agent:** What do you want the site to do?

**User:** Help developers learn zodiac fast — get a working schema in their code in under 5 minutes — and give them a reference they can come back to without having to re-read the README.

**Agent:** Primary goal: inform. Secondary: capture (downstream — getting them to use the library is itself a kind of conversion). Primary archetype: documentation.

**User:** Yes.

**Agent:** Walk me through a typical visitor.

**User:** TypeScript developer, mid-level. Mid-thirties. Lives in their editor, lands on the docs from a Google search like "typescript validate api response" or from a tweet. Opens the docs on their second monitor, copies a code sample, modifies it, ships.

**Agent:** Dev audience: dark mode required, copy-to-clipboard everywhere, search before browse, mobile-secondary but not negligible, low-bandwidth-tolerant. Languages?

**User:** English only.

**Agent:** Success metrics?

**User:** Time-to-first-schema under 5 minutes (proxy: page views to /getting-started averaging >2 minutes). Bounce rate on /getting-started < 40%. GitHub stars + npm downloads stay flat or grow. Reduced GitHub issues asking "how do I X?" — current rate is ~3/week.

**Agent:** Concrete. Constraints?

**User:** Free hosting if possible — OSS, no income from it. Open-source content; PRs welcome. I want to merge a docs PR and see it live without manual deploy steps.

**Agent:** GitHub-driven, auto-deploy. Anything regulatory?

**User:** No PII collected. No cookies, ideally.

**Agent:** Privacy-respecting analytics or none. Non-goals?

**User:** Not a blog. Not a community. Not a paid product. No newsletter.

**Agent:** Anything I haven't asked?

**User:** Versioning. We're at v3; v2 is still supported. People land on Google results for v2 docs and need to know.

**Agent:** Versioned docs with a clear "you're reading v2 — latest is v3" banner. Captured.

### Resulting `spec/purpose.md` (excerpt)

```markdown
# Purpose

## One-line summary
Help TypeScript developers learn the zodiac validation library in under 5 minutes and serve as a long-lived reference.

## Primary goal
Inform

## Archetype
- Primary: Documentation
- Secondary: (light) Marketing / project landing

## Audience
### TypeScript developer (mid-level, mid-30s)
- Device: desktop-majority, mobile-secondary
- Expertise: comfortable with TS, new to zodiac
- Network: usually fast, but should work on patchy
- Languages: English

## Success metrics
- /getting-started average time on page > 2 min
- /getting-started bounce rate < 40%
- "How do I X?" GitHub issues drop from ~3/week

## Scope
- Pages: medium (10–50 doc pages)
- Lifespan: long-lived
- Versioning: latest (v3) + v2 still indexed

## Constraints
- Open-source; PR-driven content
- Free or low-cost hosting
- No cookies; privacy-respecting

## Non-goals
- Blog, community, newsletter, paid offering

## Special notes
- Versioning: v3 latest, v2 archived/banner-warned
- Dark mode mandatory
```

User confirms. Phase 2.

---

## Phase 2 — Template

**Agent:** Documentation IA usually follows the Diátaxis framework: concept / how-to / reference / tutorial. Want me to use that or something looser?

**User:** Diátaxis. I've been bad about mixing them; the framework forces clarity.

**Agent:** Sitemap proposal:

```
/                         Home (project landing)
/getting-started          Tutorial — first schema in 5 min
/concepts/                Conceptual guides (why zodiac, schema model)
/guides/                  How-to recipes (validate API, integrate w/ Express, etc.)
/reference/               API reference (types, methods)
/examples/                Larger end-to-end examples
/changelog                Version history
/contributing             For contributors
/v2/                      Archived v2 docs (with banner)
```

**User:** Add /playground — interactive REPL.

**Agent:** Playground = embedded CodeSandbox / StackBlitz, or custom?

**User:** Embedded.

**Agent:** Done. Global elements:
- Header: logo, search (⌘K), version switcher (v3/v2), dark/light toggle, GitHub link
- Footer: small — license, GitHub, contributing
- Sidebar: nested doc nav, persistent on desktop, drawer on mobile
- TOC: right rail on long doc pages, hidden on home
- Every page: edit-on-GitHub link, prev/next at bottom

**Agent:** Components:
- Code block: tabs for multiple languages (TS / JS), copy button, line highlighting, optional file path label
- Callouts: note / tip / warning / danger
- API reference table generator
- Search modal: keyboard-driven
- Version switcher dropdown
- Doc card (used on /, /guides index, etc.)

**Agent:** Empty / error states:
- 404: docs-style with search suggestion
- Search no-results: prompt to open a GitHub issue

**Agent:** Last call?

**User:** Make sure every code sample has copy. And add "open in playground" buttons next to substantial samples.

**Agent:** Confirmed.

### `spec/template.md` excerpts

```markdown
# Template

## Sitemap
- / (home / landing)
- /getting-started
- /concepts/[slug]
- /guides/[slug]
- /reference/[slug]
- /examples/[slug]
- /playground
- /changelog
- /contributing
- /v2/...

## Global elements
- Header: logo, search (⌘K), version switcher, theme toggle, GitHub
- Footer: minimal — license, GitHub, contributing
- Sidebar: nested nav, persistent desktop / drawer mobile
- Right rail: on-page TOC for long pages
- Edit-on-GitHub on every doc page
- Prev/next at bottom

## Components
- Code block (multi-lang tabs, copy, highlight, file label)
- Open-in-playground button
- Callout (note/tip/warn/danger)
- API method card (signature, description, params, returns, example)
- Search modal
- Version switcher
- Doc card

## Navigation
- Desktop: sidebar + on-page TOC
- Mobile: drawer + collapsible TOC

## Auth
- All public

## Localization
- English only

## Content authoring
- Source: Markdown / MDX in the docs/ folder of the GitHub repo
- Workflow: PR-driven; preview deploy per PR; merge to main = live
```

Phase 3.

---

## Phase 3 — Tooling

**Agent:** For a doc site that needs versioning, search, code highlighting, dark mode out-of-the-box, and OSS-friendly auto-deploy — the strongest defaults are Starlight (Astro), VitePress, or Docusaurus. Given you're already in the TS ecosystem and the playground is an embed, Starlight or VitePress would both be excellent. Mintlify is also possible but proprietary.

**User:** I've used VitePress and it's fine. Starlight — heard good things. What's the difference?

**Agent:** Both are excellent. VitePress is Vue-based and more bare-bones; Starlight is Astro-based, more opinionated, and ships with better default UX (search, sidebar collapse, dark mode, i18n-ready). Starlight's authoring experience is markdown-with-frontmatter, no Vue needed. Given you don't have a Vue investment, I'd lean Starlight.

**User:** Starlight.

**Agent:** Stack:
- Framework: Astro + Starlight
- Search: Pagefind (built into Starlight; static; client-side; no third-party)
- Content: Markdown / MDX in repo
- Code highlighting: Shiki (Starlight default)
- Versioning: Starlight has multi-version support via separate sidebar configs; alternative is `versioned-docs/` directory pattern
- Hosting: Cloudflare Pages (free, fast, generous limits)
- DNS: Cloudflare
- CI/CD: GitHub Actions for lint + link-check + deploy; Cloudflare Pages auto-builds on push
- Analytics: Cloudflare Web Analytics (cookieless, no consent banner needed)
- Type-checking: TypeScript already in the project
- Lint: ESLint + Prettier on docs source (formatting)
- Spell-check: cspell in CI
- Link-check: lychee in CI (catches broken links to old anchors)

**Agent:** Alternatives rejected:
- VitePress — also excellent; chose Starlight for default polish.
- Docusaurus — heavier React app; overkill here.
- Mintlify — beautiful but proprietary; against the OSS preference.

**User:** Looks right.

### `spec/tooling.md` excerpt

```markdown
# Tooling

## Rendering
- Static (SSG) via Astro / Starlight

## Frontend
- Astro + Starlight

## Styling
- Starlight's built-in design tokens + CSS custom property overrides for brand color

## Backend
- None

## Database
- None

## Auth
- None

## Search
- Pagefind (built-in)

## CMS
- Markdown / MDX in `/docs` directory of GitHub repo

## Hosting
- Cloudflare Pages (frontend + edge)
- DNS: Cloudflare

## Build
- Bundler: Vite (via Astro)
- Package manager: pnpm

## Testing
- Spell-check: cspell
- Link-check: lychee
- TypeScript code-block compile-check: custom GH Action

## CI/CD
- GitHub Actions: lint, spell-check, link-check, build
- Cloudflare Pages: deploy preview per PR; deploy on merge to main
- Rollback: previous deployment one click in Cloudflare

## Analytics
- Cloudflare Web Analytics (cookieless)

## Alternatives rejected
- VitePress — comparable; chose Starlight for polish
- Docusaurus — heavier than needed
- Mintlify — proprietary
```

Phase 4.

---

## Phase 4 — Style

**Agent:** Three adjectives?

**User:** Technical. Calm. Trustworthy.

**Agent:** Avoid?

**User:** Cute. Salesy. Cluttered.

**Agent:** Dark mode is mandatory; let's design dark-first, light-second. References?

**User:** Tailwind docs, Vercel docs, Cloudflare docs (the new design), Bun docs.

**Agent:** All share: mono-friendly typography, dark backgrounds that aren't pure black, single accent color, sidebar-driven nav, generous code block treatment, large readable type. Proposing:

- **Color**: dark-mode bg = oklch(15% 0.01 260) deep slate; light-mode bg = oklch(98% 0.005 260); single accent = oklch(70% 0.18 250) cool blue (project's npm icon color, ish).
- **Type**: Inter for body and headings (single family for coherence); JetBrains Mono for code. All self-hosted.
- **Scale**: 1.25 ratio. Body 16px, code 14px.
- **Spacing**: comfortable, with code blocks getting extra breathing room (32px above / below).
- **Radius**: 6px on most surfaces; code blocks 8px.
- **Motion**: minimal. Theme toggle = instant. Sidebar reveal = 150ms. No scroll-driven animation.
- **Theme modes**: dark default, light toggle, follow-system supported, user choice persisted.
- **Voice**: second-person ("you"), present tense, technical-precise. No marketing voice. Code examples speak for themselves; surrounding prose is brief.

**Agent:** Contrast (dark mode):
- text on bg: oklch(95/15) → 14:1 (AAA) ✓
- accent on bg: oklch(70/15) → 6:1 (AA large + small) ✓
- muted text on bg: oklch(70/15) → 6:1 — borderline for small text; bump to 75% or only use for headings.

**User:** I want code blocks to feel like they belong, not like they're stuck on top. Subtle border, no shadow.

**Agent:** Code block treatment: bg slightly lifted from page bg (`oklch(18% 0.01 260)` in dark, `oklch(96% 0.005 260)` in light), 1px border slightly more saturated than text, 8px radius, no shadow. Confirmed.

**Agent:** Anything else?

**User:** I want the cursor to feel right in keyboard-driven UX. Visible focus rings everywhere. The search modal should be obvious.

**Agent:** Focus ring: 2px solid accent + 2px offset (matching `:focus-visible`); applies globally; search modal gets a slightly thicker treatment.

### `spec/style.md` excerpts

```markdown
# Style

## Brand context
- Adjectives: technical, calm, trustworthy
- Avoid: cute, salesy, cluttered

## Color (dark default)
- bg.page: oklch(15% 0.01 260)
- bg.code: oklch(18% 0.01 260)
- fg.default: oklch(95% 0.005 260)
- fg.muted: oklch(75% 0.005 260)
- brand.accent: oklch(70% 0.18 250)
- border.default: oklch(28% 0.01 260)
- border.code: oklch(32% 0.01 260)

## Color (light)
- bg.page: oklch(98% 0.005 260)
- bg.code: oklch(96% 0.005 260)
- fg.default: oklch(20% 0.01 260)
- fg.muted: oklch(45% 0.01 260)
- brand.accent: oklch(55% 0.18 250)

## Typography
- Body + headings: Inter (variable, self-hosted)
- Code: JetBrains Mono (variable, self-hosted)
- Loading: preload Inter and JetBrains Mono; font-display: swap; size-adjust

### Scale (1.25 from 16)
| Token | Size | LH | Weight | Use |
| text.xs | 13 | 1.5 | 400 | meta |
| text.sm | 14 | 1.5 | 400 | nav, captions |
| text.code | 14 | 1.6 | 400 | inline + block code |
| text.base | 16 | 1.65 | 400 | body |
| text.lg | 20 | 1.4 | 500 | h4 |
| text.xl | 24 | 1.3 | 600 | h3 |
| text.2xl | 32 | 1.2 | 600 | h2 |
| text.3xl | 40 | 1.15 | 700 | h1 / page title |

## Spacing
- 4px scale; code blocks get 32px stack above/below

## Layout
- Max content width: 800px on doc pages (prose readability)
- Sidebar: 280px
- Right TOC rail: 240px
- Breakpoints: 640 / 768 / 1024 / 1280

## Shape
- radius.button: 6px
- radius.surface: 6px
- radius.code: 8px

## Motion
- Personality: subtle
- duration.fast: 100ms
- duration.normal: 150ms
- ease.out
- Theme toggle: instant (no transition on color tokens, to avoid full-page flash)
- Reduced-motion: respected

## Theme modes
- Dark default
- Light optional
- Follows system if user has not toggled
- Toggle persists in localStorage

## Voice
- Second person, present tense
- Concise; let code samples carry weight
- Sentence case for UI labels
- No marketing voice; no exclamation points
- Microcopy: precise (e.g., "Found 12 results" not "Awesome — 12 matches!")
```

Phase 5.

---

## Phase 5 — Considerations

**Agent:** Walking the baseline plus docs-archetype specifics.

### Accessibility
- WCAG AA target.
- Keyboard-driven UX is the dominant interaction model. Every action must have a keyboard path.
- Search modal: focus-trapped, ESC closes, arrow keys navigate.
- Manual: NVDA spot-checks on key pages.
- Automated: axe-core via Pa11y in CI.

### SEO
- Per-page meta from MDX frontmatter.
- Open Graph + Twitter cards per page.
- BreadcrumbList structured data.
- robots.txt + sitemap.xml auto from Starlight.
- 301 redirects from old README anchor links to new doc URLs.
- v2 docs: indexable but include `<link rel="canonical">` pointing to v3 equivalent where one exists.
- Hreflang: N/A (English only).

### Performance
- Static pages → LCP target < 1.5s.
- No third-party JS except CodeSandbox iframe on /playground.
- Lighthouse CI threshold: 95+ on perf/a11y/SEO/best-practices.
- Pagefind index pre-built; ~100kb max.

### Privacy
- No cookies (Cloudflare Web Analytics is cookieless).
- No consent banner needed.
- Privacy statement page: brief.

### Security
- HTTPS via Cloudflare.
- HSTS preload.
- CSP: strict — no inline JS except hashed; allow CodeSandbox iframe on /playground only.
- No user input collected.

### Content
- Source of truth: GitHub repo `docs/` folder.
- Approval: maintainer review on PRs.
- Stale check: cspell in CI flags unknown words; lychee catches broken links; quarterly review.
- Code samples: tested via tsc compile in CI to prevent rot.

### Monitoring
- Cloudflare Web Analytics dashboard.
- GitHub Issues labeled `docs` for user feedback.

### Deployment
- Domain: docs.zodiac.dev
- DNS: Cloudflare
- Cloudflare Pages auto-deploys
- Backups: GitHub is source of truth

### CI/CD
- GH Actions: build + spell-check + link-check + tsc-check on code samples + axe + Lighthouse.
- Preview per PR.
- Merge → main → auto-deploy.
- Rollback: Cloudflare Pages previous build (one click).

### Testing
- Lighthouse CI per PR.
- axe-core per PR.
- Visual regression: not strictly needed (Starlight handles this); add Playwright snapshot tests on home + getting-started.

### i18n
- N/A at launch. Note: Starlight supports i18n if added later.

### Maintenance
- Maintainer: Devon.
- Contributing.md describes:
  - Writing style guide (brief)
  - How to add a doc page
  - How to add a code sample
  - Local dev quickstart
- Dependencies: Renovate on conservative schedule (monthly).

### Launch readiness
- Cut over docs.zodiac.dev DNS during a quiet day.
- Soft launch: tweet to maintainer's followers.
- Day-0: monitor analytics for 24h.
- Add prominent link from README to docs site.
- Update npm package homepage field.

### Doc-archetype specific
- Search weighting: title > headings > body. Pagefind handles this by default.
- Versioned docs: top-bar version switcher; v2 includes banner ("v2 is in maintenance — see v3 docs").
- Code samples: language tab (TS default, JS alt), copy button, optional file path, optional "open in playground."
- Edit-on-GitHub link per doc page.
- Prev/next at the bottom of each doc page, generated from the sidebar order.
- Glossary: short page; inline term links open a popover with the definition.
- API reference auto-generated from TSDoc on the library source — script in the repo.

### User-surfaced
- Maintainer wants a notice if a code sample fails to compile against the latest library version — tsc-check in CI catches this.
- Sponsor link in footer: GitHub Sponsors page (optional small CTA).

### Out of scope
- Comments / discussion
- Newsletter
- Multilingual

**Agent:** Anything I haven't asked?

**User:** Search analytics — I want to know what people search for and don't find.

**Agent:** Pagefind itself is fully client-side; no built-in analytics. We could add a tiny POST to a Cloudflare Worker on empty-result events, log to KV, expose a simple internal dashboard. Want that?

**User:** Yes. Lightweight.

**Agent:** Added: empty-search-event logger via Cloudflare Worker → KV. Maintainer-only dashboard at `/admin/searches` (basic-auth or IP-restricted).

---

## Unified spec summary

Agent assembles purpose + template + tooling + style + considerations into one document for review. Devon approves. Implementation begins on the new docs site.

---

## Lessons from this walkthrough

- **Picking a documentation-specific framework cut weeks** of bespoke work (search, versioning, dark mode, sidebar UX, content authoring). The catalog steered toward Starlight/VitePress quickly.
- **Privacy was a load-bearing constraint** (no cookies → cookieless analytics → no consent banner → simpler legal). Surfaced in phase 1, paid off in phase 5.
- **Versioning was a feature, not a chore.** Caught in the "anything else?" question in phase 1; designed in from the start; would have been expensive to retrofit.
- **Search analytics — added at the last "anything else?" question — wasn't in any catalog**, but mattered to the user. The framework left room for it.
- **Voice and tone decisions changed the feel of the site more than colors did.** "No exclamation points. Code samples carry weight." That's a styling decision worth tokenizing.
