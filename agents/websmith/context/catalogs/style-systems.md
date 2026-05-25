# Style Systems Catalog

Reference material for phase 4 (style). Covers design system references, color theory primer, typography pairings, motion conventions, and density/density trade-offs. Use as a starting point — every site's visual identity needs its own decisions.

> **Non-exhaustive.** Visual design is more art than catalog. Use this as scaffolding, not a recipe.

---

## Design system references

These are public design systems agents can reference, study, or borrow tokens from.

### General-purpose
- **Material Design 3** (Google) — opinionated, Android-aligned, comprehensive.
- **Apple Human Interface Guidelines** — opinionated, iOS/macOS-aligned.
- **Microsoft Fluent 2** — broad, cross-platform.
- **Atlassian Design System** — enterprise-tested, accessibility-strong.
- **Shopify Polaris** — admin-app patterns.
- **GitHub Primer** — content + product.
- **IBM Carbon** — enterprise, data-heavy.
- **Adobe Spectrum** — large-product opinionation.

### Government / accessibility-first
- **US Web Design System (USWDS)** — strict accessibility, plain-language.
- **GOV.UK Design System** — research-backed, WCAG AA throughout.
- **Canada.ca Design System** — bilingual, accessible.

### Open / framework-coupled
- **shadcn/ui** — copy-paste primitives over Radix + Tailwind; owned code.
- **Radix UI primitives** — headless, accessible, framework-agnostic.
- **Headless UI** (Tailwind Labs) — headless, React/Vue.
- **Ark UI** — headless, framework-agnostic.
- **Park UI** — Ark + Panda CSS.
- **Mantine** — React, opinionated, theme-driven.
- **Chakra UI** — React, accessible, prop-based.
- **DaisyUI** — Tailwind component classes.
- **Bootstrap** — broad, mature, opinionated.
- **Bulma** — CSS-only, no JS.

Borrow patterns; don't blindly copy whole systems unless you want their visual identity.

---

## Color theory primer

### Color models in CSS
- **HEX** — `#3366ff`. Easy, lossy for math.
- **RGB / RGBA** — additive primaries; not great for tweaks.
- **HSL / HSLA** — hue/saturation/lightness; intuitive for adjustments.
- **OKLCH / OKLAB** — perceptually uniform; modern best practice. `oklch(70% 0.15 240)`.
- **LCH / LAB** — perceptually uniform, slightly older.
- **Display-P3** wide gamut — for AVIF/HDR contexts.

Prefer **OKLCH** for design tokens — equal lightness values look equally light, equal chroma equally vibrant.

### Building a palette

1. **Anchor hue(s).** Pick 1–2 brand hues.
2. **Generate scales.** Create a tonal ramp (e.g., 50, 100, 200, ..., 950) by varying lightness at fixed chroma. Use tools: Radix Colors, Tailwind palettes, Open Color, Leonardo (Adobe), Huetone.
3. **Neutrals.** Pick a neutral scale — often slightly tinted by the brand hue (cool gray for blue brands, warm gray for orange brands).
4. **Semantic.** Pick success (green), warning (yellow/amber), danger (red), info (blue) — each with its own scale.
5. **Test contrast.** Every text-on-bg pair must pass WCAG AA minimum. Use a contrast checker (WebAIM, Stark, Polypane).

### Dark mode

Dark mode is **not** inverted light mode. Best practice:

- **Backgrounds** are dark but not pure black (often L=10-15% in OKLCH); pure black makes shadows invisible and increases eye strain on OLED transitions.
- **Surfaces** elevate via subtle lightening, not via drop shadows (which disappear on dark).
- **Foregrounds** are not pure white (L=95% or so) to reduce glare.
- **Brand colors** often need adjustment — saturated colors that pop on light look neon on dark; desaturate or shift lightness.
- **Borders** use slight lightening, not darkening.
- **Maintain contrast ratios**; AA must still hold.

### Color blindness

Test palettes with:
- **Deuteranopia** (no green) — affects ~5% of men.
- **Protanopia** (no red) — affects ~1% of men.
- **Tritanopia** (no blue) — rare.
- **Achromatopsia** (no color at all).

Use simulators (Stark, Sim Daltonism, Chrome DevTools).

Never rely on color alone to convey meaning — pair with shape (icons), text, or pattern.

---

## Typography

### Choosing families

Most sites need at most three families:
- **Display** — large headings, hero, brand expression.
- **Body** — paragraphs, UI labels.
- **Mono** — code, tabular numerals, technical content.

For many sites, display + body can be the same family with weight variation.

### Reliable pairings (just examples)

- **Inter** (body) + Inter as display — versatile workhorse.
- **Plus Jakarta Sans** + Inter — modern, friendly.
- **Geist** + Geist Mono — Vercel-built, paired by design.
- **Söhne** + JetBrains Mono — premium, opinionated.
- **Source Serif Pro** + Source Sans — classic serif/sans pairing.
- **Playfair Display** + Source Sans — editorial.
- **Merriweather** + Lato — readable long-form.
- **DM Sans / DM Serif Display** — coherent family.
- **IBM Plex Sans / Serif / Mono** — coherent triad.
- **Tiempos / Söhne / JetBrains Mono** — premium tri-family.
- **System fonts** (`-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`) — fastest, zero load, native feel.

### Variable fonts

Strongly preferred when supported:
- One file, all weights and styles.
- Smooth weight/width transitions.
- Smaller total load.

### Font loading strategy

- **Self-host** when possible (better privacy, no third-party blocking, easier preload).
- **`font-display: swap`** — show fallback, swap when ready. Avoids invisible text.
- **`size-adjust`** — match fallback metrics to reduce CLS during font swap.
- **Preload** critical fonts (`<link rel="preload" as="font" ...>`).
- **Subset** to needed glyph ranges.

### Type scale ratios

Pick a modular ratio and stick to it:

| Ratio | Name | Vibe |
|-------|------|------|
| 1.067 | Minor second | Subtle |
| 1.125 | Major second | Subtle |
| 1.200 | Minor third | Comfortable |
| 1.250 | Major third | Versatile (most-used default) |
| 1.333 | Perfect fourth | Pronounced |
| 1.414 | Augmented fourth | Strong contrast |
| 1.500 | Perfect fifth | Editorial |
| 1.618 | Golden ratio | Dramatic |

Generate a scale from a base size (usually 16px) using your chosen ratio. Example with 1.250 from 16px base: 13, 16, 20, 25, 31, 39, 49, 61, 76.

### Line height

- Body: 1.4–1.6
- Headings: 1.1–1.3 (tighter as they grow)
- Inline / UI labels: 1.0–1.2

### Line length

Optimal prose line length is 45–75 characters per line, ~65 ideal. Constrain `max-width` on prose containers.

### Numerals

- **Lining figures** (default) vs **old-style figures** (lowercase numerals) — old-style mixes nicer with body text; lining looks more modern.
- **Tabular figures** (equal-width) for tables, dashboards, prices; **proportional figures** elsewhere.

---

## Spacing

### Scale construction

- **Linear** — 4, 8, 12, 16, 20, 24, 28, 32... (consistent step)
- **Powers of 2** — 4, 8, 16, 32, 64, 128 (coarse)
- **Multiples of 4** with sub-steps — 4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128 (most common)
- **Geometric** — 4, 6, 8, 12, 16, 24, 32, 48, 64 (close to type scale)

Tailwind's default scale (multiples of 4) is a sensible starting point.

### Semantic naming

Layered tokens map to use:
- `space.inline-xs` — between inline items
- `space.inline-sm`
- `space.stack-xs` — between stacked items
- `space.stack-sm`
- `space.section` — between page sections

Or stick with raw scale numbers and apply them with intent.

### Density

- **Comfortable** — generous padding; marketing, content.
- **Default** — standard product apps.
- **Compact** — dashboards, tables, internal tools.

Define one default density, and override per-context where needed.

---

## Shape and elevation

### Border radius

- **None (0)** — sharp, modernist, brutalist.
- **Subtle (2–4px)** — restrained, modern.
- **Soft (6–8px)** — friendly, default for many systems.
- **Pronounced (12–16px)** — playful, friendly.
- **Full (9999px)** — pills, avatars.

Pick a base, scale up/down for context. Cards often have larger radius than buttons.

### Shadows

In light mode, shadows convey elevation. In dark mode, prefer subtle borders + lighter surfaces.

Common shadow scale:
- `shadow.sm` — subtle (`0 1px 2px rgba(0,0,0,0.05)`)
- `shadow.md` — small (`0 4px 6px rgba(0,0,0,0.07)`)
- `shadow.lg` — medium (`0 10px 15px rgba(0,0,0,0.10)`)
- `shadow.xl` — large (`0 20px 25px rgba(0,0,0,0.10)`)
- `shadow.2xl` — dramatic (`0 25px 50px rgba(0,0,0,0.25)`)

Modern designs increasingly use borders instead of shadows for elevation cues.

---

## Motion

### Personality

Pick one as the dominant mode:
- **None** — instant state changes, accessibility-first.
- **Subtle** — hover/focus micro-interactions, ~150–200ms.
- **Expressive** — visible transitions on interactions, ~200–400ms, easing curves matter.
- **Bold** — motion as a brand expression, scroll-driven, hero animations.

### Duration scale

- `duration.fast` ~100–150ms — micro feedback (button press).
- `duration.normal` ~200–250ms — hovers, simple transitions.
- `duration.slow` ~400–500ms — page transitions, larger UI shifts.
- `duration.slower` ~700ms+ — feature animations.

Keep durations as short as possible while still being legible. Faster usually = better feel.

### Easing

- `ease.out` — most UI; objects come in fast, settle slow.
- `ease.in` — exits; rarely on entrances.
- `ease.in-out` — back-and-forth, persistent movement.
- `spring` (or custom cubic-bezier) — playful, organic.

### Reduced motion

Always honor `prefers-reduced-motion: reduce`. Replace motion with instant state changes, or with cross-fades.

```css
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
```

(But — keep functional transitions like opacity for focus rings.)

---

## Tokens architecture

### Two-tier vs three-tier

**Two-tier** (recommended for most):
1. **Primitives** — raw values: `color.blue.500`, `space.4`.
2. **Semantics** — intent: `color.brand.primary`, `color.text.default`.

**Three-tier** (large systems):
1. **Primitives** — raw values.
2. **Semantic / system** — generic intent: `surface.default`, `text.muted`.
3. **Component** — component-specific: `button.primary.bg`.

### Token naming

- Predictable: `<category>.<intent>[.<variant>][.<state>]`
  - `color.brand.solid`
  - `color.brand.solid.hover`
  - `color.text.muted`
- Consistent: don't mix `text` and `fg` and `foreground` in one system.

### Token expression

- CSS custom properties (`--color-brand-solid`) — universal, runtime-themable.
- Tailwind theme — `tailwind.config.{js,ts}` `theme.extend`.
- Design tokens spec (`tokens.json` per W3C Design Tokens) — tooling-portable.
- vanilla-extract, Panda CSS, Stitches — typed CSS-in-TS with tokens.

---

## Voice and tone

Visual style without verbal style is incomplete.

### Dimensions
- **Person** — first plural ("we"), second ("you"), avoidant ("the team").
- **Formality** — formal / conversational / playful.
- **Energy** — calm / energetic / urgent.
- **Humor** — none / occasional / signature.
- **Jargon** — embraced (technical audience) / avoided (broad audience).

### Microcopy patterns
- Error messages: explain *what*, *why*, *what to do next*. Avoid blame.
- Empty states: friendly + actionable. Tell the user what to do first.
- Success confirmations: brief, specific. "Your order is on its way" beats "Operation completed."
- Buttons: verbs, not nouns. "Save changes" not "Save."
- Loading: specific when you can. "Loading 10,000 items" beats spinning forever.

### Capitalization
- **Title case** (Capitalize Each Major Word) — formal, traditional.
- **Sentence case** (Capitalize only first word) — modern, less shouty, easier for translations.

Pick one for UI labels and stick with it.

### Punctuation
- Oxford comma — decide once.
- Em-dash usage — heavy, light, none.
- Ellipses — only for genuine elisions or trailing thought.

---

## Accessibility recurring concerns

- **Touch targets**: 44×44 CSS pixels minimum (Apple HIG / WCAG 2.5.5).
- **Focus rings**: visible, non-color-only, contrast ≥3:1 with surrounding bg.
- **Color independence**: never sole carrier of meaning.
- **Animation triggers**: avoid more than 3 flashes/sec; honor reduced-motion.
- **Auto-playing media**: disable by default; provide controls.
- **Text spacing override**: support increased letter / word / line spacing (WCAG 1.4.12).

---

## Inspiration sources (curated)

Use for reference, never blind imitation:

- Awwwards, SiteInspire, Land-book, Lapa Ninja, Mobbin (UI patterns)
- Refactoring UI (book) — type, color, spacing fundamentals
- Practical Typography (Butterick) — book-length style guide
- Design Better (InVision archive)
- Smashing Magazine, A List Apart, CSS-Tricks (deprecated but archived), web.dev

---

## A note on style decay

Visual trends change every 2–3 years. Tokens, accessibility decisions, and tone of voice are far more durable than specific aesthetics. Build the system so swapping the aesthetic is a token update, not a rewrite.
