# Components Catalog

Reference of reusable UI components agents commonly need to name during phase 2 (template). Each entry gives a short purpose, typical contexts, key states/variants, and common pitfalls. Use as a vocabulary — not as the only valid set.

> **Non-exhaustive.** If a needed component is not here, define it inline in `spec/template.md`.

---

## Layout primitives

### Container
- Constrains content to a max width; centers horizontally.
- Variants: full-bleed, narrow (prose), default, wide.

### Stack
- Vertical rhythm container with consistent gap.

### Cluster
- Horizontal flex with wrap; for tags, button groups, inline items.

### Grid
- Responsive multi-column layout. Specify column count per breakpoint.

### Sidebar layout
- Persistent or collapsible side panel + main content.

### Split
- Two-column 50/50 (or other ratio), usually for hero or marketing sections.

### Section
- Page-level vertical band with padding and (optionally) a background.

---

## Navigation

### Header / top bar
- Logo + primary nav + utilities (search, account, cart, theme toggle).
- Sticky vs static, transparent-on-hero vs solid.

### Footer
- Secondary nav, legal, social, newsletter, copyright, language switcher.

### Primary nav
- Horizontal links, dropdowns, mega-menus.
- States: default, hover, active (current page), focus, expanded.

### Side nav
- Vertical link list, often nested. Used in docs, dashboards.
- States: collapsed/expanded, current section highlight.

### Tabs
- Horizontal nav within a page section. Anchor-linked or stateful.
- Variants: pill, underline, contained.

### Breadcrumbs
- Hierarchical path; helps SEO and orientation.

### Pagination
- Page numbers, prev/next, jump-to. Or load-more, or infinite scroll (with caveats).

### Search trigger / modal
- Icon button opens a modal with input + results list.
- Keyboard nav (arrow keys, enter, esc).

### Command palette
- ⌘K-style universal action launcher.

### Mobile drawer / hamburger
- Off-canvas nav for small viewports.

### Skip link
- Visible-on-focus link to main content.

### Anchor / on-page TOC
- Right-rail TOC that highlights current section on scroll (intersection observer).

---

## Hero / above-the-fold

### Hero
- Title, sub-headline, CTA(s), supporting media.
- Variants: full-bleed, split, video, animated.

### Announcement bar
- Site-wide thin banner above header. Dismissible.

### Marquee / ticker
- Horizontally scrolling row, often for logos or text.

---

## Content / cards

### Card
- Self-contained content block.
- Variants: product, article, profile, feature, stat, link.
- States: default, hover, focus, disabled.

### Stat / metric card
- Number + label + optional trend.

### Testimonial
- Quote + author + role/source + optional photo or logo.

### Logo wall
- Grid of customer/partner logos.

### Feature item
- Icon + title + description; usually in a grid.

### Comparison table
- Plans/competitors comparison. Responsive (often becomes stacked cards on mobile).

### Pricing card
- Plan name, price, feature list, CTA. "Most popular" emphasis variant.

---

## Forms

### Form
- Wrapper with consistent spacing, validation pattern, submit handling.

### Text input
- States: default, focus, error, disabled, with-icon, with-clear.

### Textarea
- Multi-line input.

### Select
- Native select, custom select, combobox, multi-select.

### Checkbox / radio / toggle
- Distinct semantics. Toggle = immediate effect; checkbox = pending submit.

### Date picker / range picker
- Calendar UI. Accessibility is hard — prefer well-tested libraries.

### File upload
- Drag-and-drop area + button. Show progress and allow remove.

### Search input
- Input with leading icon, clear button, optional autocomplete.

### Form field group
- Label + input + helper text + error message. Associated via `for`/`id`/`aria-describedby`.

### Field set / legend
- Group related inputs (address, billing/shipping).

### Multi-step / wizard
- Step indicator + per-step content + nav. Support back, persist between steps.

### Captcha / bot challenge
- hCaptcha, Cloudflare Turnstile, reCAPTCHA.

---

## Feedback

### Alert / banner
- In-page persistent message. Variants per semantic color.

### Toast / snackbar
- Transient notification. Position (top-right common). Auto-dismiss + manual close.

### Modal / dialog
- Focus-trapped overlay. Close on ESC, click-outside (sometimes), explicit close button.
- Variants: alert, confirm, content, full-screen.

### Drawer
- Side-sliding modal. Useful when modal would feel disruptive.

### Popover / tooltip
- Tooltip = hover-only, no interaction; popover = clickable, can contain interactive content.

### Skeleton / shimmer
- Placeholder shape during load.

### Spinner / loader
- Indeterminate progress.

### Progress bar
- Determinate progress.

### Empty state
- "Nothing here yet" with illustration + CTA.

### Error state
- "Something went wrong." Include retry + contact path.

---

## Data display

### Table
- Sortable, filterable, paginated, with selection and bulk actions.
- Variants: dense, comfortable, sticky header, sticky first column.

### Data grid
- Excel-like editable grid (heavier than table).

### List
- Vertical list of items; lighter than table.

### Definition list
- Term + description pairs (key-value).

### Code block
- Monospace, syntax-highlighted, copy button, optional line numbers.
- Multi-language tabs for docs.

### Callout / admonition
- Note, tip, warning, danger boxes (docs).

### Badge / chip / tag
- Small inline label. Variants per semantic color.

### Avatar
- User image. Fallback to initials or icon. Sizes.

### Image
- With responsive sources, lazy loading, alt text.

### Video player
- Captions, transcript link, accessible controls.

### Audio player
- Playback controls, transcript link.

### Map
- Embedded map. Static fallback for performance/accessibility.

### Chart
- Bar, line, area, pie, etc. Data viz needs its own palette and accessibility (table fallback, ARIA labels).

---

## E-commerce specific

### Product card
- Image, title, price, badge (sale, new), quick-add (optional).

### Product gallery
- Main image + thumbnails, zoom, video, 360-spin.

### Variant selector
- Color swatches, size buttons, dropdowns. Disable unavailable combos.

### Add-to-cart button
- States: default, in-cart, loading, sold-out.

### Mini cart
- Drawer or popover showing cart items + checkout CTA.

### Cart line item
- Image + title + variant + quantity stepper + price + remove.

### Quantity stepper
- Decrement, value, increment. Clamp to stock.

### Checkout
- Multi-step (cart → shipping → payment → review) or single-page.

### Order summary
- Subtotal, discounts, shipping, tax, total.

### Address form
- Country-aware: state/region/postal-code fields adapt.

### Payment element
- Stripe Payment Element, Adyen drop-in, etc.

### Review widget
- Star rating, count, review list, write-review form.

### Wishlist toggle
- Heart icon with on/off state, login-prompt fallback.

### Filter / facet sidebar
- Faceted search controls: checkbox groups, range sliders, swatch grids.

### Promo / discount input
- Field + apply + applied state.

---

## Docs / content specific

### Sidebar nav (docs)
- Nested collapsible sections, with current-section highlight and persistent expansion state.

### TOC / on-page nav
- Right-rail floating TOC, intersection-observer highlight.

### Doc search modal
- ⌘K or `/`-triggered, with categorized results.

### Prev / next
- Bottom-of-page navigation between sequential docs.

### Edit-on-GitHub
- Link to the source markdown.

### Version selector
- Dropdown of available doc versions.

### Language selector
- Language switcher with fallback warning.

### Glossary term
- Inline link with definition popover.

---

## Marketing / conversion

### CTA button
- High-prominence button. Primary / secondary variants.

### Lead form
- Inline or modal capture with minimal fields. Honeypot for spam.

### Newsletter signup
- Inline form with email + submit + confirmation state.

### Social share
- Buttons or links to share current page. Consider privacy implications of vendor JS.

### Pricing toggle (monthly/annual)
- Switch with price recalculation.

### FAQ accordion
- Question + answer pairs. Use real `<details>` or proper ARIA.

### Cookie banner
- Consent UI with categorized choices. Must allow reject-all in many jurisdictions.

---

## Account / settings

### Sign-in form
- Email/password, OAuth buttons, magic link, passkey.

### Sign-up form
- Minimal friction. Verify email after, not before.

### Forgot password
- Email input + reset link delivery.

### Two-factor / verification code
- 6-digit input with paste support.

### Profile editor
- Avatar upload, name, bio, social links.

### Settings tabs
- General, account, notifications, billing, integrations, security, danger zone.

### Billing portal
- Current plan, payment method, invoices, change plan, cancel.

### Notification preferences
- Channel × event-type matrix.

### Danger zone
- Account deletion, data export. Confirm via typed phrase.

---

## App-shell / dashboards

### App shell
- Persistent header + sidebar + main; current location indicated.

### Org / workspace switcher
- Combobox with recent orgs.

### User menu
- Avatar dropdown: profile, settings, sign out.

### Saved view
- Named filter/sort combinations on a list/table.

### Bulk action bar
- Appears when rows are selected.

### Keyboard shortcut overlay
- Triggered by `?`, lists shortcuts.

### Audit log row
- Timestamp, actor, action, target, before/after.

---

## Accessibility utilities

### Visually-hidden text
- For screen-reader-only content (`.sr-only`).

### Focus ring
- Visible keyboard-focus indicator. Don't remove the outline; replace it.

### Live region
- ARIA `aria-live` container for dynamic announcements.

### Skip-to-content
- First focusable element, visible on focus.

---

## Beyond this catalog

If your template needs a component this catalog doesn't name — *a custom flight-search wizard*, *a recipe ingredient scaler*, *an interactive seating map*, *a live document editor* — invent it. Name it. Document its variants, states, and contract in `spec/template.md`. Don't force-fit it into a generic name.
