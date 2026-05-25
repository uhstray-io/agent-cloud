# Phase 2 — Template

> *What pages, layouts, navigation, and components does this site need?*

Phase 1 told you *what* the site is for. Phase 2 turns that into the user-visible **structure** — the sitemap, the per-page layouts, the components that appear across pages. Still no code, no colors. Just structure.

---

## 1. Goal of this phase

Produce `spec/template.md`: a complete information architecture plus per-page layout descriptions, including which components appear where, what each page must accomplish, and how users move between them.

---

## 2. Inputs

- `spec/intake.md` (if Phase 0 ran) — device mix, languages, integrations, accessibility target.
- `spec/purpose.md` — the source of truth for what the site does and for whom.
- The archetype(s) chosen in phase 1 — defaults flow from these.

Before drafting, enumerate inherited constraints per [AGENTS.md §5](../AGENTS.md#5-constraint-propagation-matrix).

---

## 3. Decisions to extract

### 3.1 Information architecture (sitemap)

List every page (or page template) the site needs. Group into a hierarchy. For each page:

- Page name
- URL pattern (e.g., `/products/[category]/[slug]`)
- Purpose in one sentence
- Target persona(s)
- Entry points (how does a user arrive?)
- Exit points (where does a user go next?)

For dynamic sites, list **page templates** (e.g., "Product detail page") rather than every URL. Note approximate count.

### 3.2 Global elements

Elements present on most or all pages:

- **Header / top bar**: logo, primary nav, search, account, cart, language, theme toggle, region selector?
- **Footer**: secondary nav, legal links, social, newsletter, copyright, contact, language switcher?
- **Sidebar(s)**: when, where, persistent or collapsible?
- **Persistent UI**: chat widget, cookie banner, announcement bar?
- **Skip links** and other accessibility anchors

### 3.3 Navigation pattern

Pick a primary navigation pattern (often more than one in combination):

- Horizontal top nav with dropdowns
- Mega menu (e-commerce, content-heavy)
- Sidebar nav (docs, dashboards)
- Tab nav (apps, settings)
- Breadcrumbs (deep hierarchies)
- Search-first (large content libraries)
- Hamburger / mobile drawer
- Sticky / persistent vs scroll-with-page
- Footer nav (legal, secondary)
- In-content cross-linking (docs, wikis)

Consider all device classes (desktop, tablet, mobile) — patterns often differ across them.

### 3.4 Page templates

For each template, name the major regions and what goes in each. Common templates by archetype (see [`catalogs/site-archetypes.md`](../catalogs/site-archetypes.md) for archetype-specific defaults):

- Home / landing
- Product / item detail
- Listing / index / category
- Article / post
- Documentation page
- Search results
- Authentication (sign-in, sign-up, reset)
- User profile / account / settings
- Checkout flow (multi-step)
- Form / contact
- Empty / error / 404 / 500
- Confirmation / thank-you
- Dashboard / admin
- Pricing
- About / team
- Legal (terms, privacy, cookies)

Describe each template's layout in plain words. ASCII wireframes are welcome but optional.

### 3.5 Components

Identify the **reusable** components the templates depend on. Pull from [`catalogs/components.md`](../catalogs/components.md), and add any custom ones the user needs. Examples:

- Logo, primary nav, breadcrumbs, search, cart icon
- Hero, banner, announcement
- Card (product, article, profile, feature)
- Grid / list view toggle
- Filter sidebar / faceted search
- Pagination / infinite scroll / load more
- Form fields, validation messages
- Modal / dialog / drawer
- Tabs, accordion
- Toast / snackbar / alert
- Table, data grid
- Code block, callout, admonition (docs)
- Comment thread
- Star rating, review
- Avatar, badge
- Skeleton / spinner / progress
- Date picker, range picker
- File upload, drag-and-drop
- Map, video player, image carousel

### 3.6 Interactivity / dynamism

Where is the page interactive vs static?

- Pure static content
- Client-side filtering / sorting
- Live updates (websockets, polling)
- Forms (single-step, multi-step, wizard)
- Drag-and-drop
- Inline editing
- Realtime collaboration
- Personalized content
- Animations (subtle motion, hero, page transitions)

Phase 4 (Style) refines the *feel* of motion; here in Phase 2, just establish where motion exists at all.

### 3.7 Localization / internationalization

- Languages supported at launch and planned later
- RTL support
- Region-specific content variants
- Currency / unit / date / number formatting
- Translation workflow (human, machine, mixed)
- URL strategy (`/en/`, `en.example.com`, `example.com?lang=en`)

If the user says "English only for now," note it explicitly so it's a *decision*, not an *assumption*.

### 3.8 Authentication and access tiers

(Not stack — that's phase 3. This is *which pages require what.*)

- Public pages
- Authenticated-only pages
- Role-gated pages (admin, member, guest)
- Plans / subscription tiers gating
- Soft paywall vs hard paywall

### 3.9 Personalization and state

- Anonymous vs logged-in differences
- Personalized recommendations / content
- Saved preferences (theme, language, view density)
- Per-user data (cart, history, bookmarks, drafts)
- Cross-device sync

### 3.10 Accessibility-affecting structure

(Visual accessibility is phase 4; structural accessibility is here.)

- Heading hierarchy on each template
- Landmarks (header, nav, main, complementary, footer)
- Skip links
- Focus order
- Form structure (labels, fieldsets, error association)
- ARIA live regions for dynamic content

### 3.11 Content authoring

How does content get into pages?

- Hardcoded
- CMS (which? — phase 3 decides the specific tool, but here decide if a CMS exists)
- Markdown / file-based
- Database-driven
- User-generated (with what moderation?)

---

## 4. Question script

1. *"Let's map every page. Start at the home page and walk me to one product / article / item."*
2. *"What's on every page? Header? Footer? Anything else persistent?"*
3. *"How does a user find what they're looking for — browse? search? recommendation?"*
4. *"On mobile, does navigation work the same way or differently?"*
5. *"Where are forms? What happens after a user submits?"*
6. *"Which pages require sign-in? Which require a paid plan?"*
7. *"What changes for a logged-in user vs an anonymous one?"*
8. *"What languages does the site need to support — at launch and later?"*
9. *"Where does content come from? Who edits it after launch?"*
10. *"Walk me through your unhappy paths — empty states, errors, no results, network failures."*
11. *"Is there anything I haven't asked about that you think matters?"*

---

## 5. Output artifact: `spec/template.md`

````markdown
# Template

## Sitemap

```
/                            Home
/about                       About
/products                    Product index
/products/[category]         Category listing
/products/[category]/[slug]  Product detail
...
```

(For each page or template, fill the table below.)

### <Page name>
- **URL pattern:**
- **Purpose:**
- **Persona(s):**
- **Entry points:**
- **Exit points:**
- **Auth required:**
- **Major regions:**
  - Header: <what's in the header on this page>
  - Body: <ordered list of sections / components>
  - Footer: <what's in the footer on this page>
- **Components used:** <list>
- **Dynamic behaviors:** <filtering, live updates, etc.>
- **Empty / error states:**
- **Accessibility notes:** <heading hierarchy, landmarks, etc.>

## Global elements

### Header
- Logo
- Primary nav: <items>
- Search:
- Account / login state:
- Other:

### Footer
- Secondary nav:
- Legal:
- Social:
- Other:

### Persistent UI
- Cookie banner: <yes/no, when>
- Announcement bar: <yes/no, when>
- Chat / support:

## Navigation patterns
- Desktop:
- Tablet:
- Mobile:
- Search-first / browse-first:

## Components

(List every component the templates reference. Pull names from catalogs/components.md or define custom ones.)

- <component>: <purpose, where used>

## Interactivity and dynamism

- <area>: <static | client-side | live | personalized>

## Localization

- Languages at launch:
- Languages planned:
- RTL support:
- URL strategy:
- Translation workflow:

## Authentication and access

- Public pages:
- Authenticated-only pages:
- Role-gated pages:
- Plan-gated pages:

## Personalization and state

- Per-user data:
- Cross-device sync:
- Preference storage:

## Content authoring

- Source:
- Workflow:
- Editors:

## Open questions
<things to resolve before phase 3>
````

---

## 6. Exit criteria (phase gate)

Follow the [Phase gate protocol in AGENTS.md §4](../AGENTS.md#4-phase-gate-protocol). The phase exits when every box below is checked AND the user explicitly approves.

### 6.1 Inherited constraints (open the gate by stating these)

Before drafting, enumerate to the user:
- Constraints from `spec/intake.md` (device mix, languages, integrations).
- Constraints from `spec/purpose.md` (archetype, audience, regulations, non-goals).

See [AGENTS.md §5](../AGENTS.md#5-constraint-propagation-matrix).

### 6.2 Artifact completeness
- [ ] Every page or template has an entry in the sitemap.
- [ ] Every page in the sitemap has a filled-in detail block (URL, purpose, persona, regions, components, auth, empty/error states).
- [ ] Empty / error / loading states are addressed for at least the home and primary-flow pages.
- [ ] Mobile navigation is specified, not just desktop.
- [ ] Localization is a decision (even "English only at launch"), not an omission.
- [ ] Authentication-gated content is enumerated by page.
- [ ] Personalization / per-user state is specified.
- [ ] Content authoring source is specified (CMS / markdown / DB / hardcoded).
- [ ] Every component the templates reference is named (from `catalogs/components.md` or custom).
- [ ] Real-time / personalized regions are flagged so they don't get cached aggressively.

### 6.3 Catch-all
- [ ] Asked verbatim: *"Is there anything I haven't asked about that you think matters for this site?"*
- [ ] Anything new has been added to the artifact.

### 6.4 Downstream constraints to flag at the gate

- Interactivity needs → Tooling (JS-capable framework yes/no), Style (motion personality).
- Auth-gated pages → Tooling (auth provider), Considerations (RBAC, audit, session policy).
- i18n decision → Tooling (i18n library, URL strategy), Style (RTL), Considerations (translation workflow).
- Content authoring source → Tooling (CMS choice), Considerations (workflow).
- Real-time / personalized regions → Tooling (server, websockets), Considerations (caching strategy).
- Form complexity → Tooling (validation library, anti-spam), Considerations (a11y of forms).

### 6.5 Approval

User must reply with "approved", "next", or equivalent.

### 6.6 If you need to revise this phase later

A revision here triggers re-validation of: Tooling → Style → Considerations.

---

## 7. Common traps

- **Designing only the happy path.** Empty states, errors, 404s, slow networks, no-JS fallbacks — name them.
- **Forgetting mobile.** Most archetypes are mobile-majority. Mobile nav and reflow needs explicit decisions.
- **Skipping accessibility structure.** Heading order and landmarks are structural, not cosmetic.
- **Implicit auth gating.** "Of course this requires login" is not a spec.
- **Vague component names.** "A nice section here" is not a component. Name it.
- **Treating CMS choice as a phase-2 decision.** Decide *if* content is CMS-driven here; decide *which* CMS in phase 3.
