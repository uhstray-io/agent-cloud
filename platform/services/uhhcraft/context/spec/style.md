# Style

> Phase 4 artifact.
> Status: awaiting user approval.

---

## Brand context

- **Existing brand assets:** Light blue + orange color palette; fox mascot (minimalistic flat fox head illustration — simple geometric shapes, warm orange/rust tones, no gradients or complex shadows). No formal logo file, font spec, or brand book yet.
- **Visual reference:** Printify and similar clean consumer product sites — generous spacing, welcoming, product-photography-forward, professional but not corporate.
- **Desired-feel adjectives:** Clean, Cute, Warm.
- **Avoid adjectives:** Sharp, Robotic, AI-Generated.
- **Brand tension to honor:** The site sells AI-generated goods but must feel handcrafted and curated — never algorithmic. Every visual decision should reinforce human warmth.

---

## Color

### Palette

All values in OKLCH (perceptually uniform — equal lightness steps look equally light).

#### Brand — Orange (primary)

| Step | OKLCH | Approx hex | Use |
|------|-------|-----------|-----|
| orange-50 | `oklch(97% 0.02 50)` | `#FFF6EF` | Tint backgrounds, hover fills |
| orange-100 | `oklch(93% 0.05 50)` | `#FFE9D5` | Light callouts |
| orange-200 | `oklch(85% 0.10 50)` | `#FFCBA0` | Decorative |
| orange-300 | `oklch(77% 0.15 48)` | `#FBA96A` | Disabled states |
| orange-400 | `oklch(70% 0.17 47)` | `#F28640` | Dark mode CTA |
| **orange-500** | **`oklch(64% 0.17 47)`** | **`#E8732A`** | **Primary CTA — light mode** |
| orange-600 | `oklch(57% 0.16 45)` | `#C85D1C` | Hover on CTA, active |
| orange-700 | `oklch(49% 0.14 43)` | `#A54A13` | Dark text on orange tints |
| orange-800 | `oklch(38% 0.11 41)` | `#7A340C` | |
| orange-900 | `oklch(28% 0.08 39)` | `#522208` | |

#### Brand — Blue (secondary / accent)

| Step | OKLCH | Approx hex | Use |
|------|-------|-----------|-----|
| blue-50 | `oklch(97% 0.02 210)` | `#EFF8FC` | Info tint backgrounds |
| blue-100 | `oklch(93% 0.05 210)` | `#D6EEF7` | Light info |
| blue-200 | `oklch(86% 0.09 210)` | `#A8D9EE` | |
| blue-300 | `oklch(79% 0.11 210)` | `#78C3E2` | |
| **blue-400** | **`oklch(72% 0.12 210)`** | **`#5BBED6`** | **Secondary accent** |
| blue-500 | `oklch(65% 0.11 210)` | `#3FA5BF` | Hover on secondary |
| blue-600 | `oklch(55% 0.10 210)` | `#2A8BA4` | Dark text use |
| blue-700 | `oklch(44% 0.09 210)` | `#1C6F85` | |
| blue-800 | `oklch(33% 0.07 210)` | `#114F61` | |
| blue-900 | `oklch(23% 0.05 210)` | `#0A3040` | |

#### Neutral — warm (slightly orange-tinted grays, not clinical)

| Step | OKLCH | Approx hex | Use |
|------|-------|-----------|-----|
| neutral-50 | `oklch(98.5% 0.004 60)` | `#FAFAF7` | Page background (light) |
| neutral-100 | `oklch(96% 0.006 60)` | `#F5F4F0` | Card / surface background |
| neutral-200 | `oklch(91% 0.007 60)` | `#ECEAE5` | Raised surface, input fill |
| neutral-300 | `oklch(83% 0.007 60)` | `#D8D5CF` | Border muted |
| neutral-400 | `oklch(72% 0.007 60)` | `#BBB7AF` | Border default |
| neutral-500 | `oklch(60% 0.007 60)` | `#9B9892` | Subtle text / placeholder |
| neutral-600 | `oklch(50% 0.007 60)` | `#7E7B75` | Muted text |
| neutral-700 | `oklch(38% 0.007 60)` | `#5A5752` | Body text |
| neutral-800 | `oklch(27% 0.007 60)` | `#3A3733` | Strong text |
| neutral-900 | `oklch(17% 0.007 60)` | `#201E1A` | Default text |

#### Semantic colors

| Intent | OKLCH | Approx hex |
|--------|-------|-----------|
| Success solid | `oklch(60% 0.15 145)` | `#2F9E5A` |
| Success bg | `oklch(95% 0.05 145)` | `#E8F8EF` |
| Warning solid | `oklch(72% 0.17 70)` | `#D97706` |
| Warning bg | `oklch(96% 0.05 75)` | `#FEF3C7` |
| Danger solid | `oklch(55% 0.20 25)` | `#DC2626` |
| Danger bg | `oklch(96% 0.04 20)` | `#FEE2E2` |
| Info solid | blue-500 `#3FA5BF` | |
| Info bg | blue-50 `#EFF8FC` | |

---

### Semantic tokens — light mode

| Token | Value |
|-------|-------|
| `color.bg.page` | neutral-50 `#FAFAF7` |
| `color.bg.surface` | neutral-100 `#F5F4F0` |
| `color.bg.raised` | `#FFFFFF` |
| `color.bg.muted` | neutral-200 `#ECEAE5` |
| `color.fg.default` | neutral-900 `#201E1A` |
| `color.fg.muted` | neutral-600 `#7E7B75` |
| `color.fg.subtle` | neutral-500 `#9B9892` |
| `color.fg.inverted` | `#FFFFFF` |
| `color.border.default` | neutral-300 `#D8D5CF` |
| `color.border.muted` | neutral-200 `#ECEAE5` |
| `color.border.strong` | neutral-400 `#BBB7AF` |
| `color.brand.solid` | orange-500 `#E8732A` |
| `color.brand.fg` | orange-700 `#A54A13` |
| `color.brand.bg` | orange-50 `#FFF6EF` |
| `color.accent.solid` | blue-400 `#5BBED6` |
| `color.accent.fg` | blue-600 `#2A8BA4` |
| `color.accent.bg` | blue-50 `#EFF8FC` |
| `color.success.bg` | `#E8F8EF` |
| `color.success.fg` | `#1A6E3A` |
| `color.success.border` | `#86EFAD` |
| `color.warning.bg` | `#FEF3C7` |
| `color.warning.fg` | `#92400E` |
| `color.warning.border` | `#FCD34D` |
| `color.danger.bg` | `#FEE2E2` |
| `color.danger.fg` | `#991B1B` |
| `color.danger.border` | `#FCA5A5` |
| `color.info.bg` | blue-50 `#EFF8FC` |
| `color.info.fg` | blue-600 `#2A8BA4` |
| `color.info.border` | blue-200 `#A8D9EE` |
| `color.focus.ring` | orange-500 `#E8732A` |

---

### Semantic tokens — dark mode

| Token | Value |
|-------|-------|
| `color.bg.page` | `oklch(14% 0.008 50)` ≈ `#1A1714` |
| `color.bg.surface` | `oklch(18% 0.008 50)` ≈ `#231F1B` |
| `color.bg.raised` | `oklch(22% 0.008 50)` ≈ `#2C2723` |
| `color.bg.muted` | `oklch(26% 0.008 50)` ≈ `#35302B` |
| `color.fg.default` | `oklch(93% 0.005 60)` ≈ `#F0EFEC` |
| `color.fg.muted` | `oklch(68% 0.007 60)` ≈ `#A8A49E` |
| `color.fg.subtle` | `oklch(52% 0.007 60)` ≈ `#7E7A74` |
| `color.fg.inverted` | neutral-900 `#201E1A` |
| `color.border.default` | `oklch(28% 0.008 50)` ≈ `#3C3630` |
| `color.border.muted` | `oklch(24% 0.008 50)` ≈ `#302B26` |
| `color.border.strong` | `oklch(36% 0.008 50)` ≈ `#4D4641` |
| `color.brand.solid` | orange-400 `#F28640` |
| `color.brand.fg` | orange-300 `#FBA96A` |
| `color.brand.bg` | `oklch(22% 0.04 47)` ≈ `#2E1E10` |
| `color.accent.solid` | blue-300 `#78C3E2` |
| `color.accent.fg` | blue-200 `#A8D9EE` |
| `color.accent.bg` | `oklch(20% 0.04 210)` ≈ `#0E2530` |
| `color.focus.ring` | orange-400 `#F28640` |

> **Dark mode 3D canvas page:** `color.bg.page` in dark mode gives the canvas a near-black warm backdrop that makes 3D models read with maximum depth and clarity. This is the design intent — the dark mode is particularly suited to the canvas experience.

---

### Contrast audit (light mode)

| Pair | Ratio | WCAG result |
|------|-------|-------------|
| `fg.default` (#201E1A) on `bg.page` (#FAFAF7) | ~18:1 | ✓ AAA |
| `fg.default` on `bg.surface` (#F5F4F0) | ~16:1 | ✓ AAA |
| `fg.default` on `bg.raised` (#FFFFFF) | ~18:1 | ✓ AAA |
| `fg.default` on `brand.solid` (#E8732A) | ~5.96:1 | ✓ AA *(use dark text on orange buttons, not white)* |
| `fg.inverted` (#FFFFFF) on `brand.solid` (#E8732A) | ~2.84:1 | ✗ FAILS — never use white text on orange-500 |
| `fg.default` on `accent.solid` (#5BBED6) | ~7.95:1 | ✓ AAA |
| `fg.inverted` on `accent.solid` (#5BBED6) | ~2.13:1 | ✗ FAILS — use dark text on blue-400 |
| `fg.muted` (#7E7B75) on `bg.page` | ~5.2:1 | ✓ AA |
| `fg.subtle` (#9B9892) on `bg.page` | ~3.4:1 | ✓ AA for large text only |
| Focus ring (`#E8732A`) on white | ~3.56:1 | ✓ AA (3:1 required for UI components) |

> **Key rule from this audit:** All buttons, badges, and interactive elements using `brand.solid` (orange) as a background **must use `fg.default` (dark text)**, not white. This produces a warm, chocolate-on-orange effect that reads as intentional and matches the warm aesthetic perfectly.

---

### Color blindness

The orange/blue palette is safe under deuteranopia and protanopia (orange and blue remain distinguishable). Tritanopia: orange and green are distinguishable. All semantic states (success/danger/warning) are paired with icons and text — never color alone.

---

## Typography

### Font families

| Role | Font | Rationale |
|------|------|-----------|
| **Display + Body (primary)** | **Nunito** (Variable) | Rounded terminals, warm and friendly, subtly playful without being childish. Matches "Cute, Warm" adjectives. Excellent readability at body sizes. |
| Mono (order numbers, codes) | System mono (`ui-monospace, "Cascadia Code", "JetBrains Mono", monospace`) | Rare use — no custom font needed |

**Alternatives if Nunito doesn't feel right:**
- **Outfit** — slightly more geometric than Nunito, still rounded and warm. More refined feeling.
- **Plus Jakarta Sans** — modern, slightly warmer than Inter, clean. Less "cute" but more versatile if the brand matures.
- **DM Sans** — clean, geometric, part of a coherent DM family. More neutral/professional than Nunito.
- **Figtree** — very similar to Nunito but slightly more restrained. A middle ground between Nunito and Plus Jakarta Sans.

> **Recommendation: Nunito.** The rounded letterforms echo the fox mascot's soft geometry and reinforce the "Cute, Warm" brand without being childish. Revisit to Outfit or Figtree if Nunito reads as too soft at launch.

### Sourcing

- **Self-hosted** — download Nunito variable font from Google Fonts, host in `/static/fonts/`. No Google Fonts CDN request (privacy, performance, and GDPR compliance — avoids the EU Google Fonts IP-logging ruling).
- **Format:** WOFF2 only (all modern browsers support it).
- **Subset:** Latin only (`unicode-range: U+0000-00FF`). Nunito is a Latin-extended font; subsetting to Latin saves ~40% file size.
- **Loading strategy:** `font-display: swap` + `size-adjust` to minimize CLS. Preload the regular (400) and semibold (600) weights.

### Type scale (Tailwind `fontSize` overrides)

Scale ratio: 1.2 (minor third) from 16px base. Values expressed as rem.

| Token | Size | Line height | Weight | Primary use |
|-------|------|-------------|--------|-------------|
| `text.xs` | 12px / 0.75rem | 1.5 | 400 | Labels, badges, helper text |
| `text.sm` | 14px / 0.875rem | 1.5 | 400 | Secondary UI text, captions |
| `text.base` | 16px / 1rem | 1.6 | 400 | Body copy, form inputs |
| `text.lg` | 18px / 1.125rem | 1.55 | 500 | Lead text, card descriptions |
| `text.xl` | 20px / 1.25rem | 1.4 | 600 | Small headings, section labels |
| `text.2xl` | 24px / 1.5rem | 1.3 | 700 | H3 |
| `text.3xl` | 30px / 1.875rem | 1.25 | 700 | H2 |
| `text.4xl` | 36px / 2.25rem | 1.2 | 800 | H1 |
| `text.5xl` | 48px / 3rem | 1.1 | 800 | Hero heading |
| `text.6xl` | 60px / 3.75rem | 1.05 | 800 | Large hero / splash text |

### Type rules

- **Default body size:** 16px — never smaller for non-decorative text.
- **Max prose line length:** 65ch — applied to About, Legal, and any long-form text containers.
- **Heading hierarchy:** `<h1>` once per page, descending order. No skipping levels.
- **Numerals:** Lining figures default (modern, matches the clean aesthetic). Tabular figures for prices and order numbers.
- **Capitalization:** Sentence case for all UI labels, buttons, nav items, headings. ALL CAPS only for micro-labels (e.g., status badges like "IN QUEUE").
- **Letter spacing:** Slightly tracked out at display sizes (`letter-spacing: -0.02em` at 5xl–6xl for headings). Default elsewhere.
- **Italic:** Available in Nunito variable but use sparingly — only for emphasis or the fox mascot's personality moments.

---

## Spacing

Standard Tailwind spacing scale (4px base unit, multiples of 4). No customization needed — Tailwind's default is a good fit for this density.

| Token | Value | Typical use |
|-------|-------|-------------|
| `space.0` | 0 | |
| `space.1` | 4px | Icon gaps, micro spacing |
| `space.2` | 8px | Tight inline gaps |
| `space.3` | 12px | Input padding (vertical) |
| `space.4` | 16px | Base padding, card inner |
| `space.5` | 20px | Component gap |
| `space.6` | 24px | Section inner padding |
| `space.8` | 32px | Between cards, larger gaps |
| `space.10` | 40px | Section padding |
| `space.12` | 48px | Between page sections |
| `space.16` | 64px | Large section gaps |
| `space.24` | 96px | Hero breathing room |

---

## Layout

- **Max content width:** 1280px (`max-w-screen-xl`). Centered with horizontal padding.
- **Gutter:** 24px mobile, 32px tablet, 48px desktop.
- **Column system:** CSS Grid / Flexbox as needed. No rigid 12-column grid — layout is component-driven (2-col, 3-col, 4-col grids per section).
- **Breakpoints** (Tailwind defaults, unchanged):
  - `sm`: 640px — small tablet / large phone landscape
  - `md`: 768px — tablet
  - `lg`: 1024px — small desktop
  - `xl`: 1280px — standard desktop
  - `2xl`: 1536px — wide desktop
- **Density default:** Comfortable — generous padding matching the "warm, inviting" feel. No compact density anywhere on this site.
- **Mobile approach:** Mobile-first (Tailwind default). Gift shoppers likely browse on phones.

---

## Shape

| Token | Value | Used for |
|-------|-------|----------|
| `radius.none` | 0px | — (not used in standard UI) |
| `radius.sm` | 6px | Badges, small chips, tags |
| `radius.md` | 10px | Buttons, inputs, selects |
| `radius.lg` | 16px | Cards, product cards |
| `radius.xl` | 24px | Panels, modals, drawers |
| `radius.2xl` | 32px | Hero feature cards, canvas container |
| `radius.full` | 9999px | Pills, avatars, toggle switches |

> **Design rule:** Lean toward `radius.lg` and `radius.xl` as defaults for containing elements. The rounded aesthetic directly expresses "Cute, Warm" and visually reinforces the fox mascot's soft geometry. Never use sharp corners (0px) on interactive or visible container elements.

---

## Elevation / shadow

Warm-tinted shadows (slight orange tint in the shadow color):

| Token | CSS value | Used for |
|-------|-----------|----------|
| `shadow.none` | none | Flat elements |
| `shadow.sm` | `0 1px 3px oklch(18% 0.01 50 / 0.08)` | Subtle cards, inputs on hover |
| `shadow.md` | `0 4px 8px oklch(18% 0.01 50 / 0.10), 0 1px 2px oklch(18% 0.01 50 / 0.06)` | Default card elevation |
| `shadow.lg` | `0 10px 20px oklch(18% 0.01 50 / 0.12), 0 4px 6px oklch(18% 0.01 50 / 0.06)` | Modals, mini-cart drawer |
| `shadow.xl` | `0 20px 40px oklch(18% 0.01 50 / 0.14)` | Floating elements, popovers |

> **Dark mode:** Replace drop shadows with subtle `border` using `color.border.default`. Shadows on dark backgrounds look muddy; elevation is better expressed through surface lightness steps.

---

## Iconography

- **Style:** Outline, rounded line caps and joins, consistent 2px stroke.
- **Set:** [Lucide Icons](https://lucide.dev) — MIT licensed, outline style, rounded corners that match Nunito's character. Actively maintained.
- **Sizes:** 16px (inline/label), 20px (UI default), 24px (nav, prominent actions), 32px+ (decorative / section icons only).
- **Stroke weight:** 2px consistently across all sizes.
- **Accessibility:** Decorative icons (adjacent to visible label): `aria-hidden="true"`. Standalone icons (icon-only buttons): `aria-label="[action]"`.
- **Fox mascot as icon:** The flat fox head is used as a brand mark, not a Lucide icon. Used in: header logo, favicon, empty states, 404, loading/generation states, OG image.

---

## Imagery

- **Photography style:** Product-focused — the 3D canvas renders and catalog item photos are the primary imagery. No stock lifestyle photography at launch. Photos should feel warm and inviting (slightly warm white balance, never cold/blue-tinted).
- **Illustration style:** Flat, minimal, consistent with the fox mascot — simple geometric shapes, warm palette, no gradients or complex detail. Used for empty states, the 404 page, and any decorative spots.
- **Asset sources:** AI-generated renders (the site's own output), operator-produced catalog photography.
- **Aspect ratios:**
  - Product card thumbnails: 1:1 (square)
  - Hero / showcase items: 4:3 or 1:1
  - 3D canvas viewport: fills container (responsive)
  - OG / social share images: 1200×630px (1.91:1)
- **Treatments:** Rounded corners (`radius.lg`) on all product images. No duotone, color overlay, or filters — let the products speak.
- **Alt-text policy:** Required on every `<img>`. Written by the operator at content-entry time. Format: "[item name] — [brief description of the item]". Generated items use the prompt text as the base alt text. The 3D canvas viewer has an ARIA label describing the item.

---

## Motion

- **Personality:** Subtle — the site is warm and welcoming, not performative. Motion should make interactions feel responsive and alive, not call attention to itself. The 3D canvas auto-rotation is the one deliberate showcase moment; everything else is micro.
- **Duration tokens:**
  - `duration.fast`: 120ms — button presses, immediate feedback
  - `duration.normal`: 200ms — hover states, card lifts, fade-ins
  - `duration.slow`: 350ms — drawer/modal open, page section entrances
- **Easing tokens:**
  - `ease.out`: `cubic-bezier(0, 0, 0.2, 1)` — primary easing; entrances feel snappy then settle
  - `ease.in`: `cubic-bezier(0.4, 0, 1, 1)` — exits
  - `ease.in-out`: `cubic-bezier(0.4, 0, 0.2, 1)` — toggle states, persistent transitions
- **Where motion happens:**
  - Card hover: subtle `shadow.sm → shadow.md` lift + `scale(1.015)` — 200ms ease.out
  - Button hover: background color transition — 120ms ease.out
  - Button press: `scale(0.97)` — 100ms ease.in
  - Fade-in on scroll: page sections and catalog cards fade in as they enter the viewport (Intersection Observer, opacity 0→1, translateY 8px→0, 350ms ease.out, staggered for grids)
  - Mini-cart drawer: slides in from right, 200ms ease.out
  - Toast: fade + slide from top-right, 200ms ease.out; auto-dismiss fade, 150ms ease.in
  - Modal: backdrop fade-in + modal scale 0.95→1 + fade, 200ms ease.out
  - 3D canvas: slow auto-rotation in Three.js (0.2 rad/s), user OrbitControls override
  - Generation skeleton: shimmer pulse at 1.5s interval
  - Cooldown countdown: count-down number updates instantly (no animation — clarity over style)
  - HTMX swap: default `fade` swap animation at 200ms
- **What does NOT animate:** Page navigation (full-page MPA navigation is instant — no page transition animations), text content, prices, form labels. Keep text rendering crisp.
- **`prefers-reduced-motion`:** When `prefers-reduced-motion: reduce` is set, all transitions and animations are disabled (`transition: none; animation: none`). The 3D canvas auto-rotation is paused. Functional state changes (focus rings, error borders) are instant. The shimmer is replaced with a static skeleton.

---

## Theme modes

- **Light:** Default on all pages.
- **Dark:** User-toggleable. Also follows `prefers-color-scheme: dark` on first visit. Persisted in `localStorage`.
- **Toggle placement:** Accessible via the user menu in the header (icon button, sun/moon icon). Visible on both signed-in and guest states.
- **3D canvas special case:** The canvas page defaults to dark mode background regardless of theme setting — the 3D model reads best against dark. The canvas container (`bg.page` dark token) is applied as an isolated override, not a full page theme switch.
- **High-contrast mode:** No dedicated variant. WCAG AA is met in both light and dark. `prefers-contrast: more` is respected by ensuring borders are visible and avoiding subtle-only differentiation.
- **System preference:** Honored on first visit. User toggle overrides it.

---

## Component visual rules

### Button

| Variant | Background | Text | Border | Hover bg |
|---------|-----------|------|--------|---------|
| Primary | `brand.solid` (#E8732A) | `fg.default` (#201E1A) | None | orange-600 (#C85D1C) |
| Secondary | `bg.surface` | `fg.default` | `border.default` | `bg.muted` |
| Ghost | Transparent | `fg.default` | None | `bg.surface` |
| Accent | `accent.solid` (#5BBED6) | `fg.default` (#201E1A) | None | blue-500 |
| Destructive | `danger.bg` | `danger.fg` | `danger.border` | danger red solid |

- **Sizes:** sm (h-8, px-3, text.sm), md (h-10, px-4, text.base) **default**, lg (h-12, px-6, text.lg)
- **Radius:** `radius.md` (10px) on all buttons
- **Weight:** 600 (semibold) on all buttons
- **States:** default → hover (bg shift, 120ms) → active (`scale(0.97)`, 100ms) → focus (2px focus ring, `color.focus.ring`) → disabled (50% opacity, `cursor-not-allowed`)
- **Loading state:** Spinner replaces label; button width locked to prevent layout shift
- **⚠ Never white text on primary orange button** — use `fg.default` per contrast audit.

### Input / Textarea

- **Default:** `bg.raised` fill, `border.default` border, `text.base`, `radius.md`
- **Focus:** `border.brand.solid` + 2px focus ring `color.focus.ring`
- **Error:** `danger.border` + `danger.bg` fill, error message below in `danger.fg`, `text.sm`
- **Disabled:** `bg.muted` fill, `fg.subtle` text, no focus ring
- **Label position:** Above input, `text.sm`, `font-weight: 600`, `fg.default`
- **Placeholder:** `fg.subtle` — never used as a substitute for a label
- **Prompt textarea (Generate page):** Larger — min-height 120px, `text.base`, prominent placeholder with an example prompt in italics

### Radial selector (material / cut type — custom component)

- Displayed as a horizontal wrap of pill-shaped radio options (not standard radio inputs)
- Each option: rounded pill (`radius.full`), `bg.surface` default, `border.default` border, `text.sm`
- Selected: `brand.bg` fill, `brand.solid` border (2px), `brand.fg` text
- Hover (unselected): `bg.muted` fill
- Focus: standard focus ring
- Disabled option: 50% opacity (e.g., material out of stock)

### Card (product card)

- Background: `bg.raised` (#FFFFFF)
- Border: `border.muted` — subtle, 1px
- Radius: `radius.lg` (16px)
- Shadow: `shadow.md` default, `shadow.lg` on hover
- Image: 1:1 aspect ratio, `radius.md` on the image itself (slight inner radius), `object-fit: cover`
- Hover: `shadow.md → shadow.lg` + `translateY(-2px)` — 200ms ease.out
- Padding: 12px inner (image flush to edges, text content padded)

### 3D Canvas container

- Background: `color.bg.page` in **dark mode tokens** (isolated override — always dark regardless of global theme)
- Border radius: `radius.2xl` (32px) — the canvas feels like a premium window
- Shadow: `shadow.xl`
- No border in dark mode — the radius and shadow define the boundary

### Toast / snackbar

- Position: top-right, 16px inset from edges
- Background: `bg.raised` with `shadow.lg`
- Border: 1px `border.default` + left accent bar in semantic color (success/danger/info)
- Radius: `radius.lg`
- Auto-dismiss: 4 seconds. Manual close (×) always present.
- Max width: 360px

### Modal / dialog

- Overlay: `oklch(14% 0 0 / 0.6)` backdrop blur
- Container: `bg.raised`, `radius.xl`, `shadow.xl`, max-width 560px (default)
- Header: title in `text.2xl`, close button top-right
- Padding: 24px

### Cooldown timer (custom)

- Replaces Generate button when rate-limited
- Container: `brand.bg` background, `radius.md`, subtle border
- Text: "Try again in **0:45**" — countdown updates every second
- Guest variant: adds below — "⚡ Account holders wait much less — [Sign up free]" in `accent.fg`, `text.sm`

---

## Tone of voice

- **Person:** Second person ("you", "your") — warm and direct. Not "the customer" or "users."
- **Formality:** Conversational. Friendly without being slangy.
- **Contractions:** Yes — "you're", "we'll", "it's", "don't". They sound like a person, not a form.
- **Humor:** Gentle, occasional — carried mostly by the fox mascot in empty states and loading messages. Never sarcastic, never at the user's expense.
- **Jargon:** Avoided in all customer-facing copy. "PLA" is "Strong plastic." "PETG" is "Flexible, durable plastic." "Die-cut" is "Cut to shape." Explain via tooltip or helper text in the radial selector.
- **Microcopy patterns:**
  - **Error:** Friendly + actionable. "That didn't quite work — try again in a moment." Never "Error 500" or blame-y language.
  - **Empty states:** Warm invitation. "Nothing here yet! Ready to make something?" with a fox mascot illustration.
  - **Success:** Specific and warm. "Your order is in! We'll start making it right away." Not "Order placed successfully."
  - **Generating:** Conversational + playful. "Bringing your idea to life…", "Almost there!", "Your [sticker/item] is almost ready!" The fox mascot can wave or have wide eyes.
  - **Cooldown:** Matter-of-fact + upsell. "Give it a moment — try again in [time]." + "Account holders skip the wait line."
  - **Cart:** "Nice choice." on add-to-cart toast.
  - **Checkout:** Reassuring. "Your payment is secure." "We'll send a confirmation to [email]."
- **Button labels:** Verbs, not nouns. "Create Something" not "Creation." "See in 3D" not "3D View." "Add to Cart" not "Cart."
- **Capitalization:** Sentence case for all UI labels (modern, approachable, easier to localize later). Proper nouns and "UhhCraft" always capitalized.
- **Punctuation:** Oxford comma always. Em-dash used sparingly for rhythm (e.g., "Unique, one-of-a-kind — just like you wanted."). No exclamation marks in error messages. Occasional exclamation mark in success/delight moments.
- **Numbers:** Write out under ten ("one item", "two colors"), numerals at ten and above.

---

## Branding consistency

- **Fox mascot usage:** Appears in — header logo (fox head + "UhhCraft" wordmark), favicon (fox head only), OG/social share image, 404 page (fox looking confused), empty states (fox pointing at nothing, fox with a sparkle), generation loading state (fox with wide eyes), order confirmation (fox with a thumbs-up paw).
- **Fox style guide:** Flat, minimal, warm-toned (primary rust/orange #C85D1C, ear detail white or cream, eye a dark neutral). No gradients. No outlines thicker than 2px. Consistent proportions across all uses.
- **Logo lockup:** Fox head icon (24px height minimum) + "UhhCraft" in Nunito Bold. Clear space: 8px minimum on all sides. Minimum digital size: 120px wide.
- **Favicon:** Fox head only, 32×32px simplified version. Also 180×180 Apple touch icon.
- **OG / social share image:** 1200×630px. Orange-to-slightly-darker-orange diagonal gradient background. Fox mascot centered-left. "UhhCraft" in white Nunito ExtraBold, right side. Tagline: "Make something one of a kind." White, text.lg.
- **Email template:** White background, orange header bar with fox mascot + wordmark. Nunito font stack with web-safe fallback (`"Nunito", "Arial Rounded MT Bold", Arial, sans-serif`). Orange CTA buttons with dark text (same rule as web).
- **Color use in print / marketing:** Orange as the dominant brand color. Blue as accent. Warm neutral on white.

---

## User preferences honored

| Preference | Behavior |
|------------|---------|
| `prefers-reduced-motion: reduce` | All CSS transitions and animations set to `none`. Three.js auto-rotation paused. Shimmer replaced with static skeleton. HTMX swaps become instant. |
| `prefers-color-scheme: dark` | Dark mode applied on first visit. User can override via toggle (persisted in localStorage). |
| `prefers-reduced-data` | No behavior change at launch (no autoplay video, no large background video). Future: could defer non-critical images. |
| `prefers-contrast: more` | No specific high-contrast theme, but AA contrast is maintained throughout. Focus rings and borders are already clearly visible. |

---

## Tailwind theme config surface

All color tokens above map to `tailwind.config.js` `theme.extend.colors` and `theme.extend.fontSize`. CSS custom properties (`--color-brand-solid`, etc.) are set in `:root` (light) and `.dark` (dark mode class) for runtime theme switching. Tailwind's `darkMode: 'class'` strategy is used — the `dark` class is toggled on `<html>` by JavaScript based on user preference / localStorage.

---

## Open questions

| # | Question | Resolves |
|---|----------|---------|
| OQ-STYLE-1 | Exact fox mascot file / final design — to be created or provided before build | Before build |
| OQ-STYLE-2 | Logo wordmark — "UhhCraft" in Nunito or a custom lettered wordmark? | Before build |
| OQ-STYLE-3 | Does the team want to review the Nunito specimen in context before committing? Alternative: Outfit or Figtree. | Preference |
| OQ-NEW-1 (from template) | Exact material + cut-type option labels (need final copy for radial selectors) — "Strong plastic" vs "PLA", etc. | Before build |
