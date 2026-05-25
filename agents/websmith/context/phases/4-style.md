# Phase 4 — Style

> *What does this site look and feel like?*

You know what the site is for, what it contains, and what tech will build it. Now define the visual and emotional language: colors, type, density, motion, theming, voice. No code in this artifact — just the visual contract the implementation will follow.

---

## 1. Goal of this phase

Produce `spec/style.md`: a complete description of the visual and tonal identity. Expressed in tokens (named, reusable values) and rules. The implementation will translate this into CSS / theme files / design tokens compatible with the stack chosen in phase 3.

---

## 2. Inputs

- `spec/intake.md` (if Phase 0 ran) — existing brand assets, reference sites, feel/avoid adjectives, accessibility target.
- `spec/purpose.md` — audience, archetype, tone implications.
- `spec/template.md` — pages and components that must be styled.
- `spec/tooling.md` — what styling system was chosen (constrains expressible patterns).
- Any existing brand assets the user has.

Before drafting, enumerate inherited constraints per [AGENTS.md §5](../AGENTS.md#5-constraint-propagation-matrix).

---

## 3. Decisions to extract

### 3.1 Brand context

- Existing brand? (logo, mark, brand guidelines, prior site)
- Industry conventions to lean into or push against
- Reference sites the user likes (and *what* they like about them — colors? layout? motion? feel?)
- Reference sites the user dislikes (and why)
- Adjectives that describe the desired feel (3–5: e.g., "trustworthy, minimal, technical")
- Adjectives that describe what to **avoid** (e.g., "not playful, not corporate, not generic")

### 3.2 Color

#### 3.2.1 Palette
- **Brand / primary** — anchor color, used sparingly for emphasis.
- **Secondary / accent** — supporting brand color(s).
- **Neutrals** — backgrounds, surfaces, borders, text. Usually 5–10 steps from near-white to near-black.
- **Semantic** — success, warning, danger, info. Often green/yellow/red/blue, but pick deliberately.
- **Data viz** (if charts/data) — categorical and sequential palettes.

#### 3.2.2 Color roles (semantic tokens)

Tokens express intent, not raw color. Examples:

- `color.bg.page`, `color.bg.surface`, `color.bg.raised`, `color.bg.muted`
- `color.fg.default`, `color.fg.muted`, `color.fg.subtle`, `color.fg.inverted`
- `color.border.default`, `color.border.muted`, `color.border.strong`
- `color.brand.solid`, `color.brand.fg`, `color.brand.bg`
- `color.success`, `color.warning`, `color.danger`, `color.info` (each with bg/fg/border variants)
- `color.focus.ring`

Define the same token set per theme mode (light, dark, etc.) — same intent, different values.

#### 3.2.3 Contrast and accessibility

For every text-on-background pair, target contrast ratios:
- Body text: WCAG AA (4.5:1) minimum, AAA (7:1) preferred
- Large text (18pt+ or 14pt bold): AA (3:1) minimum
- Interactive elements: 3:1 minimum for boundaries
- Focus indicators: visible, non-color-only

Test programmatically. Don't assume.

#### 3.2.4 Color blindness considerations

Pick palettes that survive deuteranopia / protanopia / tritanopia. Never rely on color alone to convey meaning — pair with shape, icon, or text.

### 3.3 Typography

#### 3.3.1 Type families

Pick (at most) 3 families and assign roles:
- **Display** — headings, hero, brand expression
- **Body** — paragraphs, UI text
- **Mono** — code, tabular data, technical content

Same family for display + body is also fine. Many sites should do this.

#### 3.3.2 Font sourcing

- Self-hosted vs Google Fonts vs Adobe Fonts vs commercial foundry
- Variable fonts vs static instances
- Subsetting (latin, latin-ext, unicode ranges)
- Font loading strategy — FOUT / FOIT / size-adjust / font-display: swap
- Preload critical fonts

#### 3.3.3 Type scale

A modular scale based on a ratio (1.125, 1.2, 1.25, 1.333, 1.5, 1.618...). Define named steps:

- `text.xs`, `text.sm`, `text.base`, `text.md`, `text.lg`, `text.xl`, `text.2xl`, ..., `text.6xl`
- Or semantic: `text.body`, `text.body-sm`, `text.heading-h1`, `text.heading-h2`, etc.

Define for each step:
- Size (px or rem)
- Line height
- Letter spacing (often subtle adjustments at extremes)
- Weight (when paired with the step)

#### 3.3.4 Type rules

- Default body size (16px minimum for non-decorative text)
- Maximum line length for prose (45–75 characters)
- Heading hierarchy (h1 once per page, semantic order)
- Paragraph spacing
- Numerals (lining vs old-style, tabular vs proportional)
- Italic / weights available
- Capitalization rules (title case, sentence case, ALL CAPS sparingly)

### 3.4 Spacing and layout

#### 3.4.1 Spacing scale

Define a scale used everywhere — padding, margin, gap, position. Powers of 2, multiples of 4, or geometric:

- `space.0`, `space.1` (4px), `space.2` (8px), `space.3` (12px), `space.4` (16px), `space.5` (24px), `space.6` (32px), `space.7` (48px), `space.8` (64px), ...

Or semantic: `space.inline-sm`, `space.stack-md`, `space.section-lg`.

#### 3.4.2 Layout grid

- Max content width (e.g., 1280px, 1440px)
- Gutter
- Column system (12-column, fluid, custom)
- Breakpoints — define explicit ones and what they target (e.g., `sm: 640px, md: 768px, lg: 1024px, xl: 1280px, 2xl: 1536px`)
- Container behavior at each breakpoint

#### 3.4.3 Density

- Comfortable / compact / spacious — pick a default
- Per-context overrides (data tables are usually denser than marketing pages)

### 3.5 Shape

- **Border radius** scale: `radius.none`, `radius.sm`, `radius.md`, `radius.lg`, `radius.xl`, `radius.full`
- Default radius for buttons, inputs, cards, modals
- Sharp / soft / pill aesthetic

### 3.6 Elevation / shadow

- Shadow scale: `shadow.none`, `shadow.sm`, `shadow.md`, `shadow.lg`, `shadow.xl`
- Use sparingly. Many modern designs use borders + subtle backgrounds in place of shadows.
- Dark mode shadows need different treatment (often inset highlights instead of drop shadows)

### 3.7 Iconography

- Style: outline / filled / duotone / hand-drawn
- Set: Heroicons, Lucide, Phosphor, Tabler, Material Symbols, FontAwesome, custom
- Sizes used (16, 20, 24, 32, 48)
- Stroke weight consistency
- Accessibility: `aria-hidden` decorative vs `aria-label` semantic

### 3.8 Imagery

- Photography style: editorial, candid, product-focused, none
- Illustration style: flat, isometric, hand-drawn, 3D render, none
- Asset sources: stock, commissioned, generative, user-supplied
- Aspect ratios in use (16:9, 4:3, 1:1, 3:4)
- Treatment: full-bleed, rounded, framed, duotone, color overlay
- Alt-text policy: required, written by whom

### 3.9 Motion

Decide the overall motion personality:

- **None** — no animation beyond instant state changes
- **Subtle** — micro-interactions, hover/focus, fade-ins; nothing longer than ~200ms
- **Expressive** — visible motion that guides attention, transitions, parallax
- **Bold** — motion as a brand expression, hero animations, scroll-driven storytelling

Then define:
- Duration scale: `duration.fast` (~120ms), `duration.normal` (~200ms), `duration.slow` (~400ms)
- Easing: `ease.out`, `ease.in-out`, `ease.spring`, custom cubic-bezier
- Where motion happens: hover, focus, page transitions, scroll, data updates
- `prefers-reduced-motion` — what gets disabled or replaced

### 3.10 Theme modes

- **Light** — default for most sites
- **Dark** — increasingly expected; required for tools, docs, dev-focused sites
- **High-contrast** — accessibility variant
- **System** — follow OS preference
- **User-toggle** — explicit setting, persisted

Specify each token's value per active mode. Don't just invert — dark mode needs different relationships, not flipped lightness.

### 3.11 Component-level visual rules

For each component class, define the visual contract. Examples:

- **Button** — variants (primary, secondary, ghost, destructive), states (default, hover, focus, active, disabled, loading), sizes (sm/md/lg), with-icon
- **Input** — states, label position, error display, helper text
- **Card** — padding, radius, border vs shadow, hover state
- **Modal** — overlay, max-width, padding, close affordance
- **Toast / alert** — variants per semantic color, dismiss UX, position
- **Table** — row hover, zebra striping, sticky header, density

Don't fully spec every component here — phase 4 sets the *system*. Component-by-component specs may live in the implementation.

### 3.12 Tone of voice (copy)

Style isn't just visual. Decide tonal rules so any agent or human writing copy stays consistent:

- Person — first plural ("we"), second ("you"), avoid both?
- Formality — formal / conversational / playful
- Contractions — allowed?
- Humor — yes / no / sparing
- Jargon — embraced (technical audience) or avoided
- Inclusive language guidelines
- Microcopy patterns — error messages, empty states, success confirmations
- Capitalization — title case vs sentence case for UI labels
- Punctuation — Oxford comma, em-dash use

### 3.13 Branding consistency

- Logo usage rules (clear space, minimum size, on which backgrounds)
- Favicon, app icon, social share image (OG image)
- Print rules (if any)
- Email template alignment with web

### 3.14 Reduced-motion, reduced-data, reduced-transparency

- `prefers-reduced-motion`
- `prefers-reduced-data`
- `prefers-reduced-transparency`
- `prefers-contrast: more`

Decide each. Honor user preferences by default.

---

## 4. Question script

1. *"Show me 2–3 sites you love. What specifically do you like about each?"*
2. *"Show me a site whose look you dislike. Why?"*
3. *"Three adjectives for how this should feel."*
4. *"Three adjectives for how it should NOT feel."*
5. *"Do you already have a logo, colors, fonts? Show me."*
6. *"How dressed-up should this be? A landing page can be dramatic; an internal tool probably shouldn't be."*
7. *"Dark mode — required, nice-to-have, or no?"*
8. *"How much motion is appropriate? Boring is fine if it serves the purpose."*
9. *"What's the tone of voice? Is this 'we / you,' or impersonal?"*
10. *"Is there anything I haven't asked about that you think matters?"*

---

## 5. Output artifact: `spec/style.md`

````markdown
# Style

## Brand context
- Existing brand:
- Adjectives (desired feel):
- Adjectives (avoid):
- Reference sites (liked):
  - <url> — <what specifically>
- Reference sites (disliked):
  - <url> — <what to avoid>

## Color

### Palette
- Brand: <hex / oklch / hsl>
- Secondary: <values>
- Neutrals: <scale>
- Semantic: success / warning / danger / info

### Semantic tokens (light mode)
- color.bg.page:
- color.bg.surface:
- color.bg.raised:
- color.bg.muted:
- color.fg.default:
- color.fg.muted:
- color.fg.subtle:
- color.fg.inverted:
- color.border.default:
- color.border.muted:
- color.border.strong:
- color.brand.solid:
- color.brand.fg:
- color.brand.bg:
- color.success.{bg,fg,border}:
- color.warning.{bg,fg,border}:
- color.danger.{bg,fg,border}:
- color.info.{bg,fg,border}:
- color.focus.ring:

### Semantic tokens (dark mode)
(same keys, different values)

### Contrast audit
- Body text on page bg: <ratio> (target ≥4.5)
- Body text on surface bg: <ratio>
- Button label on brand bg: <ratio>
- Border on bg: <ratio>

## Typography

### Families
- Display:
- Body:
- Mono:

### Sourcing
- Self-hosted / Google / Adobe / foundry:
- Loading strategy:
- Subsets:

### Scale
| Token | Size | Line height | Weight | Use |
|-------|------|-------------|--------|-----|
| text.xs | | | | |
| text.sm | | | | |
| text.base | | | | |
| text.md | | | | |
| text.lg | | | | |
| text.xl | | | | |
| text.2xl | | | | |
| ... | | | | |

### Rules
- Default body size:
- Max prose line length:
- Heading hierarchy:
- Numerals:
- Capitalization rules:

## Spacing
| Token | Value |
|-------|-------|
| space.0 | 0 |
| space.1 | 4px |
| space.2 | 8px |
| ... | |

## Layout
- Max content width:
- Gutter:
- Column system:
- Breakpoints:
  - sm:
  - md:
  - lg:
  - xl:
  - 2xl:
- Density default:

## Shape
| Token | Value | Used for |
|-------|-------|----------|
| radius.none | | |
| radius.sm | | |
| ... | | |

## Elevation
| Token | Definition |
|-------|------------|
| shadow.none | |
| shadow.sm | |
| ... | |

## Iconography
- Style:
- Set:
- Sizes:
- Stroke weight:
- Accessibility pattern:

## Imagery
- Photography:
- Illustration:
- Sources:
- Aspect ratios:
- Treatments:
- Alt-text policy:

## Motion
- Personality: <none | subtle | expressive | bold>
- Duration tokens:
- Easing tokens:
- Where motion happens:
- Reduced-motion behavior:

## Theme modes
- Light: <default | optional>
- Dark: <default | optional | required>
- High-contrast: <yes | no>
- System: <yes | no>
- User toggle: <yes | no>

## Component visual rules

### Button
- Variants:
- Sizes:
- States:
- Padding / radius / weight:

### Input
- States:
- Label position:
- Error display:

### Card
- ...

(repeat per major component)

## Tone of voice
- Person:
- Formality:
- Contractions:
- Humor:
- Jargon:
- Microcopy patterns:
- Capitalization rules:
- Punctuation rules:

## Branding consistency
- Logo rules:
- Favicon / app icon / OG image:
- Email alignment:

## User preferences honored
- prefers-reduced-motion:
- prefers-reduced-data:
- prefers-reduced-transparency:
- prefers-contrast:

## Open questions
````

---

## 6. Exit criteria (phase gate)

Follow the [Phase gate protocol in AGENTS.md §4](../AGENTS.md#4-phase-gate-protocol). The phase exits when every box below is checked AND the user explicitly approves.

### 6.1 Inherited constraints

Before drafting, enumerate:
- From `spec/intake.md`: brand assets, reference sites, feel/avoid adjectives, accessibility target.
- From `spec/purpose.md`: archetype (drives style conventions), audience (drives type sizing, density, tone), regulations (drives legal copy requirements).
- From `spec/template.md`: motion needs, theme requirements, voice requirements.
- From `spec/tooling.md`: styling system (Tailwind / CSS-in-JS / vanilla CSS / framework) — every token here must be expressible in it.

### 6.2 Artifact completeness
- [ ] Brand context filled: existing brand, adjectives (feel + avoid), liked + disliked references.
- [ ] Color palette defined (brand, neutrals, semantic).
- [ ] Semantic color tokens defined for every theme mode in scope.
- [ ] Contrast audit completed: every text-on-bg combination tested, AA minimum confirmed programmatically.
- [ ] Type families chosen with sourcing and loading strategy.
- [ ] Type scale tokenized with size, line height, weight, and use.
- [ ] Spacing scale defined as tokens.
- [ ] Layout: max width, gutter, breakpoints, density default.
- [ ] Shape (radius) scale and elevation (shadow) scale defined.
- [ ] Iconography style and set chosen.
- [ ] Imagery style + alt-text policy defined.
- [ ] Motion personality decided (none / subtle / expressive / bold) + duration + easing tokens.
- [ ] Reduced-motion behavior specified.
- [ ] Theme modes specified (light / dark / high-contrast / system / user-toggle).
- [ ] Tone of voice articulated (person, formality, humor, jargon, capitalization, punctuation).
- [ ] Branding consistency (logo, favicon, OG image, email alignment) addressed.
- [ ] User-preferences honored (reduced-motion, reduced-data, reduced-transparency, contrast).
- [ ] Every token defined here is expressible in the styling system chosen in Phase 3.

### 6.3 Catch-all
- [ ] Asked verbatim: *"Is there anything I haven't asked about that you think matters for this site?"*

### 6.4 Downstream constraints to flag at the gate

- Motion personality → Considerations (reduced-motion testing in CI).
- Theme modes → Considerations (contrast audits per mode in CI).
- Color tokens → Considerations (WCAG conformance verification).
- Font sourcing (external vs self-hosted) → Considerations (privacy, GDPR — Google Fonts ruling).
- Tone of voice → Considerations (content authoring guide, microcopy patterns).

### 6.5 Approval

User must reply with "approved", "next", or equivalent.

### 6.6 If you need to revise this phase later

A revision here triggers re-validation of: Considerations (mainly contrast, motion, and content authoring sections). May trigger a Tooling revision if a chosen system can't express required tokens.

---

## 7. Common traps

- **Picking colors without checking contrast.** Pretty palettes fail WCAG more often than not.
- **Defining dark mode by inverting lightness.** It does not work. Dark needs its own token values.
- **Skipping motion personality.** "We'll just animate things later" produces incoherent UX.
- **No alt-text policy.** Imagery decisions without accessibility decisions = inaccessible imagery.
- **Tone of voice as an afterthought.** Copy is style. Define it.
- **Tokens that don't map to the stack.** If phase 3 picked Tailwind, your tokens should be expressible as Tailwind theme; if it picked vanilla CSS variables, name them accordingly.
- **Designing only for desktop.** Mobile breakpoints, mobile type scale, mobile spacing — all need decisions.
