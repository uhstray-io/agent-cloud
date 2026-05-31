# Template

> Phase 2 artifact.
> Status: awaiting user approval.

---

## Sitemap

```text
/                                    Home
/catalog                             Catalog browse (all items)
/catalog/[category]                  Category listing
/catalog/[slug]                      Catalog item entry → routes to canvas
/generate                            Custom generation entry
/canvas/[id]                         3D Canvas — unified product view (catalog + generated)

/cart                                Cart
/checkout                            Checkout (multi-step)
/order/[order-id]                    Order confirmation + status

/account/sign-in                     Sign in
/account/sign-up                     Sign up
/account/forgot-password             Password reset
/account                             Account dashboard
/account/orders                      Order history
/account/orders/[order-id]           Order detail
/account/designs                     Saved generations (last 10)

/about                               About + brand showcase
/legal/terms                         Terms of service
/legal/privacy                       Privacy policy
/legal/returns                       Returns and refund policy

/404                                 Not found
/500                                 Server error
```

---

## Page details

---

### Home

- **URL pattern:** `/`
- **Purpose:** Inspire visitors to browse or generate; communicate what UhhCraft is in seconds without text-heavy explanation.
- **Personas:** Both — first visit.
- **Entry points:** Direct, social share, search, word of mouth.
- **Exit points:** `/catalog`, `/generate`, `/about`.
- **Auth required:** No.
- **Major regions:**
  - **Header:** Logo, primary nav (Catalog | Create | About), cart icon, account icon.
  - **Hero:** Full-width showcase of a rotating 3D item or gallery of impressive generated products. Tagline. Two CTAs: "Browse Catalog" and "Create Something".
  - **Showcase Gallery:** Grid of 6–12 standout generated items (stickers and 3D prints). Each has a short caption. No pricing — pure desire generation.
  - **How it works strip:** 3-step visual: 1. Describe it → 2. See it in 3D → 3. We make it. No more than 3 lines of text total.
  - **CTA band:** Second CTA to start generating or browse the catalog.
  - **Footer:** Secondary nav, legal links, copyright.
- **Components:** Hero, Showcase Gallery, How-It-Works Strip, CTA Button, Footer.
- **Dynamic behaviors:** Hero 3D item auto-rotates (Three.js island). Gallery is static server-rendered.
- **Empty / error states:** N/A — all content is static or curated.
- **Accessibility notes:** `<main>` landmark. Hero heading is `<h1>`. Gallery items have descriptive alt text. How-it-works uses an ordered list semantically. Skip-to-content link at top.

---

### Catalog browse

- **URL pattern:** `/catalog`
- **Purpose:** Let users discover pre-designed items to buy without generating.
- **Personas:** Both — browsing mode.
- **Entry points:** Home CTA, header nav, search.
- **Exit points:** `/catalog/[category]`, `/catalog/[slug]`, `/generate`.
- **Auth required:** No.
- **Major regions:**
  - **Header:** As global.
  - **Filter bar:** Product type toggle (All | Stickers | 3D Prints). Optional category chips. No faceted sidebar — keep it simple.
  - **Product grid:** Cards showing item image/render, name, base price. Responsive: 2 columns mobile, 3–4 desktop.
  - **Empty state:** "No items in this category yet. Want to create one?" → `/generate`.
  - **Footer:** As global.
- **Components:** Product Card, Filter Bar (product type toggle + category chips), Product Grid, Empty State, Pagination (or load-more if catalog grows large).
- **Dynamic behaviors:** Filter toggle is client-side (no page reload). Pagination server-rendered.
- **Empty / error states:** Empty catalog → illustrated empty state with "Create Something" CTA.
- **Accessibility notes:** Filter controls are `<fieldset>`/`<legend>` radio groups. Grid items are `<article>` with meaningful headings.

---

### Category listing

- **URL pattern:** `/catalog/[category]`
- **Purpose:** Scoped view of one product category (e.g., "Animals", "Nature", "Custom Text").
- **Personas:** Both.
- **Entry points:** Catalog browse, breadcrumb nav.
- **Exit points:** `/catalog/[slug]`, `/catalog`.
- **Auth required:** No.
- **Major regions:** Same as catalog browse but pre-filtered to the category.
- **Components:** Same as catalog browse + Breadcrumbs.
- **Accessibility notes:** `<h1>` is the category name.

---

### Catalog item entry

- **URL pattern:** `/catalog/[slug]`
- **Purpose:** Show item info and capture material/finish selection before routing to the canvas.
- **Personas:** Both.
- **Entry points:** Catalog browse, catalog category.
- **Exit points:** `/canvas/[id]` (after selection), back to catalog.
- **Auth required:** No.
- **Major regions:**
  - **Item info:** Name, short description, base price. Static image or rendered preview thumbnail.
  - **Configuration panel:**
    - If sticker: Material radial (Vinyl / Gloss / Reflective / …), Cut type radial (Die-cut / Kiss-cut / Square / …).
    - If 3D print: Material radial (PLA / PETG / …), Finish radial (Matte / Glossy / …).
    - *(Exact options per material and cut type to be researched and documented before Phase 3.)*
  - **CTA:** "View in 3D" → constructs canvas URL with selected options → navigates to `/canvas/catalog-[slug]?material=X&cut=Y`.
- **Components:** Radial Selector (material), Radial Selector (cut/finish), CTA Button, Breadcrumbs.
- **Dynamic behaviors:** Price may update based on material selection (if materials have different costs). Static otherwise.
- **Empty / error states:** Item not found → 404.
- **Accessibility notes:** Radial selectors are `<fieldset>`/`<legend>`/`<input type="radio">` groups. Price update announced via ARIA live region.

---

### Custom generation entry

- **URL pattern:** `/generate`
- **Purpose:** Capture the user's prompt and product configuration, initiate AI generation, route to the canvas.
- **Personas:** Both.
- **Entry points:** Home CTA ("Create Something"), header nav.
- **Exit points:** `/canvas/[generation-id]` after generation starts.
- **Auth required:** No (rate-limited for guests; faster rate for accounts).
- **Major regions:**
  - **Product type selector:** Two large toggle options — "Sticker" or "3D Print". Selecting one updates the configuration panel below.
  - **Prompt input:** Large textarea. Placeholder: *"Describe what you want — e.g., 'an orange cartoon tiger riding a blue bike'"*. Character limit shown.
  - **Configuration panel (contextual):**
    - Sticker: Material radial (Vinyl / Gloss / Reflective / …), Cut type radial (Die-cut / Kiss-cut / Square / …).
    - 3D Print: Material radial (PLA / PETG / …), Finish radial (Matte / Glossy / …).
  - **Generate button:** Prominent CTA. Disabled during cooldown.
  - **Cooldown state:** When rate-limited, the button is replaced with a countdown timer (e.g., "Try again in 0:45"). Guest-state includes an upgrade nudge: "Account holders regenerate much faster — [Sign up free]".
  - **Generation history panel (account users only):** Collapsible sidebar or bottom sheet showing the user's last up to 10 previous generations as thumbnails. Clicking one reloads that generation into the canvas.
- **Components:** Product Type Toggle, Prompt Textarea, Radial Selector (material), Radial Selector (cut/finish), Generate Button, Cooldown Timer, Account Upgrade Nudge, Generation History Panel (account only).
- **Dynamic behaviors:** Product type toggle dynamically swaps the configuration panel. Generate button posts to the generation API and redirects to `/canvas/[id]` on success. Cooldown is enforced server-side; UI polls or uses the API response to display remaining time.
- **Empty / error states:**
  - Generation fails (AI error) → inline error message + retry button (respecting cooldown).
  - Prompt too short → inline validation before submit.
- **Accessibility notes:** Product type toggle is a `<fieldset>` radio group. Prompt textarea has a visible label. Cooldown countdown is an ARIA live region. Error messages are associated with their inputs via `aria-describedby`.

---

### 3D Canvas — Unified product view

- **URL pattern:** `/canvas/[id]`
- **Purpose:** The central product experience. Every item — catalog or generated — is presented here in an interactive 3D viewer before the user commits to buying. Accepting saves the asset and adds to cart.
- **Personas:** Both.
- **Entry points:** `/generate` (after generation), `/catalog/[slug]` (after material selection).
- **Exit points:** Cart (on accept), `/generate` (on reject/iterate), `/catalog/[slug]` (on back/reject for catalog items).
- **Auth required:** No.
- **Major regions:**
  - **3D Viewport:** Full-width (or near-full) interactive canvas. User can rotate (drag/swipe) and zoom (scroll/pinch). Auto-rotates slowly on load. Touch-optimised for mobile.
  - **Item summary bar:** Name (generated description or catalog name), material/finish selection summary, price. Small and unobtrusive — the 3D model is the hero.
  - **Action bar:**
    - Primary CTA: "Add to Cart" (accepts the item, locks this generation as the manufacturing asset, adds to cart).
    - Secondary: "Try Again" / "New Prompt" (for generated items — returns to `/generate` with cooldown; respects rate limit).
    - Tertiary: "Back to Catalog" (for catalog items — returns to `/catalog/[slug]`).
  - **Cooldown indicator:** For generated items, if the user hits "Try Again" and is on cooldown, show countdown. Guest nudge: "Account holders wait much less".
  - **Generation loading state:** Shown while the AI result is being fetched. Full-viewport skeleton with a subtle animated placeholder shape and a progress indicator. No blank white screen.
- **Components:** 3D Canvas Viewer (custom — Three.js/Babylon.js island), Item Summary Bar, Action Bar (CTA Button group), Cooldown Timer, Account Upgrade Nudge, Skeleton Loader (generation loading state).
- **Dynamic behaviors:** 3D viewer is full JS (isolated island). All other regions are server-rendered. "Add to Cart" is a server action that locks the generation asset to the cart line item.
- **Empty / error states:**
  - 3D model fails to load → error state with retry button + "Contact us if this keeps happening."
  - Generation result not ready (timed out) → error state with retry.
  - Session expired on a generated canvas → prompt to regenerate.
- **Accessibility notes:** 3D canvas has an ARIA label describing the item. Keyboard users can access the action bar without entering the canvas. Touch controls are documented in a visually-hidden description. All buttons have clear visible labels.

---

### Cart

- **URL pattern:** `/cart`
- **Purpose:** Review items before checkout. Last chance to update quantities or remove items.
- **Personas:** Both.
- **Entry points:** Mini cart CTA, cart icon in header.
- **Exit points:** Checkout, back to browse/generate.
- **Auth required:** No (session cart for guests; account cart for users).
- **Major regions:**
  - **Line items:** Thumbnail (3D render or static image), item name, material/finish, quantity stepper (qty always 1 for custom generated items — qty can be > 1 for catalog items), unit price, remove button.
  - **Order summary:** Subtotal, estimated shipping (TBD), estimated tax, total.
  - **Promo/discount field:** For account bulk discount codes (auto-applied if eligible).
  - **Checkout CTA:** Prominent. Guest sees "Continue to Checkout". If not signed in, secondary option: "Sign in for Priority Manufacturing + Savings".
  - **Empty cart state:** Illustrated empty state. CTAs: "Browse Catalog" and "Create Something".
- **Components:** Cart Line Item, Quantity Stepper, Order Summary, Promo Input, CTA Button, Empty State, Account Upgrade Nudge (guest only).
- **Dynamic behaviors:** Quantity updates and remove are client-side with server sync. Order summary recalculates on change.
- **Accessibility notes:** Cart line items in a `<ul>`. Quantity stepper is labelled. Running total is an ARIA live region.

---

### Checkout

- **URL pattern:** `/checkout`
- **Purpose:** Collect shipping address and payment; place the order.
- **Personas:** Both.
- **Entry points:** Cart.
- **Exit points:** `/order/[order-id]` on success, back to cart.
- **Auth required:** No (guest checkout supported; account login offered inline).
- **Major regions:**
  - **Step 1 — Contact:** Email address (for order confirmation). If not signed in: option to sign in or continue as guest. Guest sees: "Create an account for Priority Manufacturing, order history, and bulk discounts."
  - **Step 2 — Shipping:** Name, address (US, country-aware), shipping method selection (if multiple tiers offered).
  - **Step 3 — Payment:** Stripe Payment Element (card, Apple Pay, Google Pay if applicable). Order summary sidebar. Place Order button.
  - **Step indicator:** Shows current step (1/2/3). Allows back-nav.
- **Components:** Multi-step Wizard, Form Field Group, Address Form, Payment Element (Stripe), Order Summary (sidebar), Step Indicator, Account Upgrade Nudge (guest, step 1).
- **Dynamic behaviors:** Step transitions are client-side. Payment Element is Stripe-hosted JS. Address field adapts to country (US only at launch — simplified). 
- **Empty / error states:**
  - Payment declined → inline error below payment element, do not lose form state.
  - Address not serviceable → inline validation.
  - Cart empty on arrival → redirect to cart.
- **Accessibility notes:** Each step is a `<fieldset>` with a `<legend>`. Error messages associated with fields via `aria-describedby`. Step indicator uses `aria-current`.

---

### Order confirmation

- **URL pattern:** `/order/[order-id]`
- **Purpose:** Confirm the order was placed. Communicate what happens next.
- **Personas:** Both.
- **Entry points:** Checkout success.
- **Exit points:** Home, catalog, account (if signed in).
- **Auth required:** No (accessible via order ID in URL; no sensitive data exposed beyond the order itself).
- **Major regions:**
  - **Confirmation header:** "Your order is placed!" + order number.
  - **Priority callout (account holders):** "You're in the priority queue — your item ships first."
  - **Order summary:** Line items, total, shipping address.
  - **What happens next:** 3-step explainer: manufacture → ship → delivered. Expected timeframe (placeholder copy until ops defined).
  - **Email notice:** "We've sent a confirmation to [email]."
  - **CTAs:** "Create Another" → `/generate`, "Browse Catalog" → `/catalog`.
- **Components:** Order Summary, CTA Button, Info Strip (what happens next).
- **Dynamic behaviors:** Static server-rendered.
- **Accessibility notes:** `<h1>` is "Order confirmed". Order number is a `<strong>` within a descriptive sentence.

---

### Sign in

- **URL pattern:** `/account/sign-in`
- **Purpose:** Authenticate existing users.
- **Personas:** Both (when they want account features).
- **Entry points:** Header account icon, checkout, saved designs prompt.
- **Exit points:** Previous page (redirect), `/account`.
- **Auth required:** No.
- **Major regions:** Email + password form. "Forgot password" link. "Don't have an account? Sign up" link. No social/OAuth at launch (OPEN — Phase 3 decision).
- **Components:** Sign-in Form, Form Field Group.
- **Accessibility notes:** Form has a `<h1>`. Errors displayed inline, associated with fields.

---

### Sign up

- **URL pattern:** `/account/sign-up`
- **Purpose:** Create a new account.
- **Personas:** Both.
- **Entry points:** Sign-in page, checkout upgrade nudge, canvas upgrade nudge.
- **Exit points:** `/account` or previous page.
- **Auth required:** No.
- **Major regions:** Email + password + confirm password. Benefits callout: "Priority manufacturing, saved designs, reorder, bulk discounts." Email verification sent after signup (verify before accessing account features).
- **Components:** Sign-up Form, Form Field Group, Benefits List.
- **Accessibility notes:** Same as sign-in.

---

### Forgot password

- **URL pattern:** `/account/forgot-password`
- **Purpose:** Trigger a password reset email.
- **Personas:** Both.
- **Entry points:** Sign-in page.
- **Exit points:** Sign-in (after submitting).
- **Auth required:** No.
- **Major regions:** Email input + submit. Confirmation state: "If that email exists, a reset link is on its way."
- **Components:** Form Field Group, CTA Button.

---

### Account dashboard

- **URL pattern:** `/account`
- **Purpose:** Overview of account activity; quick access to orders, designs, and account settings.
- **Personas:** Account holders only.
- **Entry points:** Header account icon (when signed in), post sign-in redirect.
- **Exit points:** `/account/orders`, `/account/designs`, `/generate`, `/catalog`.
- **Auth required:** Yes.
- **Major regions:**
  - **Welcome strip:** "Hello [name]. You're a priority member."
  - **Recent orders:** Last 3 orders with status. "View all" link.
  - **Saved designs:** Last 3 design thumbnails. "View all" link.
  - **Quick actions:** "Create Something", "Browse Catalog", "Reorder" (last order).
- **Components:** Order Summary Card, Design Thumbnail, CTA Button.
- **Accessibility notes:** `<h1>` is "My Account". Sections use `<section>` with `<h2>` headings.

---

### Order history

- **URL pattern:** `/account/orders`
- **Purpose:** Full list of past orders with status and reorder capability.
- **Personas:** Account holders.
- **Entry points:** Account dashboard.
- **Exit points:** `/account/orders/[order-id]`, reorder → cart.
- **Auth required:** Yes.
- **Major regions:** List of orders — date, order number, item thumbnails, status badge (processing / manufacturing / shipped / delivered), total, Reorder button. Pagination.
- **Components:** Order List Item, Status Badge, CTA Button (Reorder), Pagination.
- **Empty state:** "No orders yet. Ready to create something?" → `/generate`.

---

### Order detail

- **URL pattern:** `/account/orders/[order-id]`
- **Purpose:** Full detail of one order, including manufacturing asset reference and shipping status.
- **Personas:** Account holders.
- **Entry points:** Order history.
- **Exit points:** Reorder → cart, back to order history.
- **Auth required:** Yes (must own the order).
- **Major regions:** Order number, date, status. Line items with 3D thumbnail. Shipping address. Payment summary. Reorder CTA.
- **Components:** Order Summary, Cart Line Item (display mode), Status Badge, CTA Button (Reorder).

---

### Saved designs

- **URL pattern:** `/account/designs`
- **Purpose:** Browse and reuse past AI generations (last 10).
- **Personas:** Account holders.
- **Entry points:** Account dashboard.
- **Exit points:** `/canvas/[generation-id]` (reload a past generation into the canvas).
- **Auth required:** Yes.
- **Major regions:** Grid of up to 10 saved generation thumbnails. Each has: thumbnail, prompt text used, date, "Load in Canvas" button, delete button.
- **Components:** Design Thumbnail Card, CTA Button, Empty State.
- **Empty state:** "Your last 10 generations will appear here. Go create something." → `/generate`.
- **Note:** Generations beyond 10 are automatically purged (oldest first). No manual management UI — oldest drops off automatically.

---

### About

- **URL pattern:** `/about`
- **Purpose:** Tell the UhhCraft brand story; showcase the fox mascot; demonstrate what's possible.
- **Personas:** Both — curiosity-driven.
- **Entry points:** Footer, header nav.
- **Exit points:** `/catalog`, `/generate`.
- **Auth required:** No.
- **Major regions:** Brand story (fox mascot, the "handcrafted with AI" positioning). Expanded showcase gallery. Materials info (brief: what is PLA? what is vinyl?). "Start Creating" CTA.
- **Components:** Hero, Showcase Gallery, CTA Button, Feature Item (materials explainer).

---

### Legal pages

- **URL patterns:** `/legal/terms`, `/legal/privacy`, `/legal/returns`
- **Purpose:** Legal compliance and consumer trust.
- **Auth required:** No.
- **Major regions:** Full-width prose with clear headings. Last-updated date at top.
- **Components:** Container (narrow/prose), Section headings.

---

### 404

- **URL pattern:** Any unmatched route.
- **Purpose:** Graceful handling of bad URLs.
- **Major regions:** Friendly error message (fox mascot illustration). "Back to Home" + "Browse Catalog" CTAs.
- **Components:** Error State, CTA Button.

---

### 500

- **URL pattern:** Server error.
- **Purpose:** Graceful handling of server failures.
- **Major regions:** "Something went wrong on our end." Retry link + home link.
- **Components:** Error State, CTA Button.

---

## Global elements

### Header

- **Logo:** UhhCraft wordmark + fox mascot icon. Links to `/`.
- **Primary nav:** Catalog | Create | About.
- **Cart icon:** Badge showing item count. Opens mini-cart drawer.
- **Account icon:** If guest → links to `/account/sign-in`. If signed in → opens user menu dropdown (My Account, Orders, Designs, Sign Out).
- **Behaviour:** Sticky. Transparent over homepage hero, solid white/light on all other pages. Collapses to hamburger drawer on mobile.
- **No search bar** — catalog is small enough that browse + filter covers discovery. Revisit if catalog grows large.

### Footer

- **Secondary nav:** Catalog | Create | About | Terms | Privacy | Returns.
- **Brand:** Fox mascot icon + "UhhCraft — unique, one of a kind."
- **Copyright:** © [year] UhhCraft.
- **No newsletter signup** (non-goal — no mailing list management in scope).
- **No social links at launch** (OPEN — add in Phase 5 if social presence is confirmed).

### Persistent UI

- **Cookie / consent banner:** Yes — required for Stripe and any analytics. Dismissible. Reject-all option. Appears on first visit only.
- **Announcement bar:** Optional. Can be used for launch promotions or shipping notices. Off by default.
- **No chat/support widget** (non-goal).

---

## Navigation patterns

- **Desktop:** Horizontal top nav (Catalog | Create | About) in header. Sticky. Dropdown user menu from account icon.
- **Tablet:** Same as desktop; may collapse nav items to hamburger at narrow tablet widths.
- **Mobile:** Hamburger icon → off-canvas drawer. Drawer contains full nav: Catalog, Create, About, Account (or Sign In), Cart. Cart icon remains visible in header alongside hamburger.
- **Breadcrumbs:** On `/catalog/[category]`, `/catalog/[slug]`, and account sub-pages.
- **Browse-first** (not search-first): catalog is browseable by design. Search is not in scope at launch.

---

## Components

| Component | Purpose | Where used |
|-----------|---------|------------|
| Header / Top Bar | Global nav, cart, account | All pages |
| Footer | Secondary nav, legal, brand | All pages |
| Mobile Drawer | Off-canvas nav on small viewports | All pages (mobile) |
| Skip-to-content link | WCAG AA keyboard access | All pages |
| User Menu Dropdown | Signed-in account actions | Header |
| Mini Cart Drawer | Quick cart preview + checkout CTA | All pages (cart icon) |
| Cookie Banner | Consent UI | First visit |
| Announcement Bar | Promotions / notices | Optional, global |
| Hero | Headline + CTAs + showcase media | Home |
| Showcase Gallery | Grid of impressive generated items | Home, About |
| How-It-Works Strip | 3-step explainer | Home |
| Product Card | Catalog item thumbnail + name + price | Catalog, Category |
| Filter Bar | Product type + category filter | Catalog, Category |
| Breadcrumbs | Hierarchical location indicator | Catalog sub-pages, Account |
| Radial Selector (Material) | Radio group for material choice | Catalog item, Generate |
| Radial Selector (Cut/Finish) | Radio group for cut type or finish | Catalog item (stickers), Generate |
| Product Type Toggle | Sticker vs 3D Print selector | Generate page |
| Prompt Textarea | Free-text generation input | Generate page |
| Generate Button | Submits prompt + config to AI | Generate page |
| Cooldown Timer | Rate limit countdown display | Generate page, Canvas |
| Account Upgrade Nudge | Prompt to sign up for faster rate / priority | Generate page, Canvas, Cart, Checkout |
| Generation History Panel | Last 10 generations (account only) | Generate page |
| 3D Canvas Viewer | Interactive Three.js/Babylon.js 3D viewport | Canvas page |
| Item Summary Bar | Name, material, price overlay on canvas | Canvas page |
| Action Bar | Accept / Try Again / Back buttons | Canvas page |
| Skeleton Loader | Generation-in-progress placeholder | Canvas page |
| Cart Line Item | Item image + name + qty + price + remove | Cart |
| Quantity Stepper | +/- qty control | Cart |
| Order Summary | Subtotal / shipping / tax / total | Cart, Checkout, Order Confirmation |
| Promo / Discount Input | Apply bulk discount codes | Cart |
| Multi-step Wizard | Step indicator + per-step form | Checkout |
| Address Form | Shipping address capture | Checkout |
| Payment Element | Stripe-hosted card / wallet input | Checkout |
| Sign-in Form | Email + password auth | Sign In |
| Sign-up Form | Account creation | Sign Up |
| Status Badge | Order status label | Orders list, Order detail |
| Order List Item | Order summary row | Order history |
| Design Thumbnail Card | Saved generation preview + load action | Saved designs |
| Empty State | Illustrated "nothing here" + CTA | Cart, Orders, Designs, Catalog filter |
| Error State | "Something went wrong" + retry | Canvas, 500, 404 |
| Toast | Transient confirmation (added to cart, etc.) | Site-wide |
| Alert / Banner | Inline persistent messages | Forms (errors), Checkout |
| ARIA Live Region | Screen-reader announcements for dynamic content | Canvas, Cooldown, Cart updates |
| Focus Ring | Visible keyboard focus indicator | All interactive elements |

---

## Interactivity and dynamism

| Area | Mode | Notes |
|------|------|-------|
| Home hero | Static (with a JS animation island) | 3D item auto-rotates via Three.js |
| Showcase gallery | Static server-rendered | No client JS needed |
| Catalog browse | Static + client-side filter toggle | Filter is client-side; product grid is server-rendered |
| Catalog item config | Client-side only (price recalc on material select) | Server-rendered form; JS for price update |
| Generate form | Client-side + server async | POST to generation API; poll or redirect on completion |
| 3D Canvas | JS island (Three.js/Babylon.js) | Only JS-heavy element on the page; rest is server-rendered |
| Cart | Client-side with server sync | Qty update / remove are optimistic client updates |
| Checkout | Multi-step client-side + Stripe JS | Stripe Payment Element is vendor JS |
| Account pages | Server-rendered | Minimal client JS |
| Cooldown timer | Client-side countdown | Timer initialised from server-provided remaining seconds |

---

## Localization

- **Languages at launch:** English only.
- **Languages planned:** None specified — OPEN for future (not in scope for this build).
- **RTL support:** No.
- **URL strategy:** N/A (English-only).
- **Translation workflow:** N/A.

---

## Authentication and access

| Page / feature | Access |
|----------------|--------|
| All browse pages, Home, About, Legal | Public |
| Generate, Canvas, Catalog | Public (rate-limited for guests) |
| Cart, Checkout | Public (guest checkout supported) |
| Order confirmation | Public (via order ID in URL) |
| Account dashboard, Orders, Designs | Authenticated |
| Faster generation rate | Authenticated |
| Priority manufacturing queue | Authenticated |
| Bulk discount eligibility | Authenticated, bulk purchase threshold (OPEN — define in Phase 5) |
| Admin role | Special account flag — bypasses generation rate limiting and order charges |

**Roles:** `guest` (unauthenticated), `user` (account holder), `admin` (operator bypass). No public role management UI.

---

## Personalization and state

| Data | Guest | Account holder |
|------|-------|----------------|
| Cart | Session-based (cookie) | Account-linked (persists across devices) |
| Generation rate limit | Slower cooldown (server-side, session-keyed) | Faster cooldown (server-side, account-keyed) |
| Generation history | Not saved | Last 10 saved in DB |
| Order history | Email receipt only | Account orders page |
| Bulk discount | Not available | Automatic after qualifying bulk purchase |
| Priority manufacturing | Not available | Applied automatically at order creation |

---

## Content authoring

- **Catalog items:** Database-driven. Operator manages directly via database (no admin UI). Each item record contains: name, description, product type (sticker/3D print), base price, material options, cut/finish options, 3D model file reference, thumbnail image, category.
- **Legal / About copy:** Hardcoded at launch. Update via code deploy.
- **Showcase gallery (homepage + about):** Curated subset of catalog items or past generated items. Managed by operator as DB records or a config file.
- **No CMS** in scope.
- **Content editors:** Operator (the team of 2) only.

---

## Email notifications

Both guest and account holders receive the following transactional emails:

| Trigger | Recipient | Content |
|---------|-----------|---------|
| Order placed | Customer (guest or account) | Order number, line items, total, shipping address, "what happens next" |
| Account created | New account holder | Welcome, benefits summary, verify email link |
| Password reset | Account holder | Reset link (expires in 1 hour) |
| Order status update | Customer | Status change (manufacturing / shipped / delivered) — *phase 5 will scope the fulfillment event model* |

Email service provider: OPEN — resolve in Phase 3.

---

## Open questions

| # | Question | Resolves in |
|---|----------|-------------|
| OQ-3 (intake) | Network/device constraints for target audience | Phase 3 |
| OQ-8 | Payment processor decision | Phase 3 |
| OQ-9 | AI image generation tooling | Phase 3 |
| OQ-10 | AI 3D model generation tooling | Phase 3 |
| OQ-11 | Third-party fulfillment feasibility | Phase 3 / Phase 5 |
| OQ-12 | Monthly run budget ceiling | Phase 3 |
| OQ-13 | Uptime 99.99% vs self-hosted + zero-ops contradiction | Phase 3 |
| OQ-21 | 3D asset format for sticker previews vs printed items | Phase 3 |
| OQ-22 | Manufacturing asset: same file as preview or derived export? | Phase 3 |
| OQ-23 | Asset storage for accepted 3D designs | Phase 3 |
| OQ-25 | Generation reject/abandon policy — asset discarded or saved? | Decided: guests discard; accounts save last 10. ✓ |
| OQ-NEW-1 | Exact material and cut-type options per product type (requires research) | Phase 3 |
| OQ-NEW-2 | Bulk discount threshold — what constitutes a "bulk" order? | Phase 5 |
| OQ-NEW-3 | Social media links — does UhhCraft have social accounts? | Phase 5 |
| OQ-NEW-4 | Email service provider | Phase 3 |
| OQ-NEW-5 | OAuth / social login (Google, etc.) at launch or not? | Phase 3 |
| OQ-NEW-6 | Shipping method options — single flat rate, carrier-calculated, or free? | Phase 5 |
