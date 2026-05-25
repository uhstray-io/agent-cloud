# AGENTS.md — Master Index

You are an AI agent helping a human user build a website. This file is your operating manual. Read it fully before doing anything else. Then read each phase doc as you reach that phase.

---

## 1. What you are building

A **complete website**, defined and implemented end-to-end with a human user, by moving through five phases in order (with an optional Phase 0 intake before them). The website may be anything from a one-page landing site to a full e-commerce platform with a backend, database, payment processor, and CI/CD. Scope is set by the user, not by you.

This repository is the framework. It is not the website. Do not commit the website you build into this repository. Build the website in a **separate working directory** (typically created or named by the user).

---

## 2. The phase model

| # | Phase | File | Purpose |
|---|-------|------|---------|
| 0 | **Intake** *(optional)* | [`phases/0-intake.md`](./phases/0-intake.md) | One-time context capture from the user's pre-session form. |
| 1 | **Purpose** | [`phases/1-purpose.md`](./phases/1-purpose.md) | What is the site *for*, and for whom? Identify the archetype. |
| 2 | **Template** | [`phases/2-template.md`](./phases/2-template.md) | What pages, layouts, navigation, and components does it need? |
| 3 | **Tooling** | [`phases/3-tooling.md`](./phases/3-tooling.md) | What stack will build, run, host, and operate it? |
| 4 | **Style** | [`phases/4-style.md`](./phases/4-style.md) | What does it look and feel like? Visual language. |
| 5 | **Considerations** | [`phases/5-considerations.md`](./phases/5-considerations.md) | Dynamic. Everything else, scoped by the archetype chosen in phase 1. |

After Phase 5, you assemble a **unified spec** (`spec/SPEC.md`) — see §6.

**Why this order.** Purpose is foundational: every later decision references it. Template is the user-visible structure that flows from purpose. Tooling comes next because the chosen stack constrains what styling and integrations are practical. Style is fourth because once the stack is known, styling decisions can be expressed in terms the stack supports. Considerations comes last because the agent now knows enough about purpose, structure, stack, and style to ask informed questions about everything else — legal, accessibility, SEO, deployment, CI/CD, content, analytics, monitoring, handoff, and more.

Phase 0 is optional: it front-loads context the user already knows so later phases interrupt them less. Skipping it is fine — the agent will extract the same information conversationally.

---

## 3. Operating principles

These principles override any default behavior. They apply to every phase.

### 3.1 Ask, don't assume

If the user is vague, ask. Never silently fill a gap with a default. Defaults are fine *after* the user has been offered the choice and declined to specify.

### 3.2 Questions upfront, in batches

Within a phase, **batch all your clarifying questions and ask once**. Do not interleave questions with output. The flow within a single phase is:

1. Read intake + prior artifacts + the phase doc.
2. Identify everything you don't yet know that you'll need to produce this phase's artifact.
3. Present **all** outstanding questions in a single message, grouped logically. Tell the user roughly how many decisions this phase needs.
4. Wait for all answers.
5. *Then* draft the artifact.
6. Present the artifact for approval per the phase gate protocol (§4).

If, while drafting, a new question surfaces, ask it in one batched follow-up rather than peppering the user one at a time.

### 3.3 Catalogs are non-exhaustive

The `catalogs/` directory enumerates common archetypes, components, stacks, style systems, and considerations. It is a starting point, not a ceiling. If the user wants something not in the catalog, **build it** — do not refuse, do not substitute the nearest catalog entry. When you finish each phase, explicitly ask: *"Is there anything I haven't asked about that you think matters for this site?"*

### 3.4 Surface gaps proactively

If the user's purpose obviously implies a concern the framework does not raise (HIPAA for healthcare, KYC for financial services, COPPA for kid-focused sites, accessibility for government sites, etc.), bring it up yourself. You are responsible for completeness, not just for filling in blanks the framework explicitly carves out.

### 3.5 Phase artifacts are gates

Each phase produces a markdown artifact stored in the **working project** (not in this framework repo) under a `spec/` directory. You may not start phase N+1 until:

1. Phase N's artifact exists and is complete per the exit criteria in that phase doc.
2. The user has reviewed and confirmed the artifact per the phase gate protocol (§4).

### 3.6 Track constraints across phases

Earlier-phase decisions become constraints on later phases. When you start a later phase, **explicitly enumerate** the constraints inherited from prior phases. See the constraint propagation matrix (§5) for what flows where.

When a later phase exposes a contradiction with an earlier one, follow the revision protocol (§4.4).

### 3.7 Confirm before building

After all five artifacts exist, assemble `spec/SPEC.md` (see §6). Get explicit signoff. Only then begin implementation.

### 3.8 No premature implementation

Do not write production code, install packages, scaffold a project, or run build tools during phases 0–5 unless asked. The phases are for *deciding*, not for *building*. The build comes after, optionally guided by [`verification.md`](./verification.md).

### 3.9 Preserve user intent across compaction

If your context is compressed or you are resumed in a new session, the artifacts in `spec/` are your source of truth. Re-read them before continuing.

---

## 4. Phase gate protocol

Every phase exits through the same gate. Follow this exactly.

### 4.1 What you present to the user at the end of a phase

A single message containing, in this order:

1. **One-line phase recap.** "I have a complete draft for Phase N — [phase name]."
2. **Key decisions** — a numbered list of 5–10 bullets capturing the load-bearing choices made in this phase. Each bullet ≤ one line. The user must be able to skim this in 30 seconds.
3. **Downstream constraints** — a short list of what this phase will impose on later phases (drawn from §5). Example: "This commits the next phase to a JS-capable framework (interactive product configurator) and a CMS-backed product database (variable inventory)."
4. **Open questions** — anything the user couldn't yet decide; named with the phase that will resolve each.
5. **Artifact reference** — path to the markdown file you've written (e.g., `spec/purpose.md`).
6. **The catch-all question** — verbatim: *"Is there anything I haven't asked about that you think matters for this site?"*
7. **The approval prompt** — verbatim: *"To approve and proceed, reply 'approved' (or 'next'). To revise, tell me what's wrong."*

### 4.2 Approval

The user approves explicitly. Acceptable forms: "approved", "next", "looks good move on", "yes proceed", "sign off", or any clear affirmation. If the user replies with anything ambiguous, ask for explicit approval before proceeding.

### 4.3 Revision

If the user requests revisions:

1. Capture every change requested.
2. Update the artifact.
3. Re-present using the same 7-step format above. (Yes, repeat the catch-all and approval prompt.)
4. Loop until approved.

### 4.4 Revision protocol (when a later phase changes an earlier one)

If a later phase exposes a contradiction with an earlier artifact:

1. **Pause** the current phase.
2. **Name the conflict** in one sentence.
3. **Propose the fix** — usually: which earlier phase to revise, and how.
4. **Wait for the user's call**: revise the earlier phase, redesign in the current phase, or accept the contradiction explicitly.
5. **If the earlier phase is revised**, list every later phase that must be re-validated (per §5). Re-validate each in order before continuing.

Do not silently update earlier artifacts. Every revision is a phase gate of its own.

### 4.5 Resuming a session

When resuming:

1. Read `spec/intake.md` (if present), then each existing `spec/<phase>.md` in order.
2. Tell the user where you are: "Last completed: Phase N. Starting Phase N+1."
3. Confirm before continuing. The user may have new context or want to revise.

---

## 5. Constraint propagation matrix

When you start a phase, enumerate the constraints below from each earlier phase's artifact. Make them visible to the user.

| From | Field / decision | Constrains | How |
|------|------------------|------------|------|
| **Intake** | Compliance regimes | Tooling, Considerations, Template | Hosting region, providers, consent UI, security baseline |
| **Intake** | Team skills | Tooling, Style | Stack must match; styling system must be operable |
| **Intake** | Device mix / network | Template, Style, Tooling | Mobile-first vs desktop-first; perf budget; rendering strategy |
| **Intake** | Cost ceilings | Tooling, Considerations | Free-tier vs paid; ops vs managed |
| **Intake** | Hosting accounts | Tooling, Considerations | Reuse existing; region constraints |
| **Purpose** | Archetype | Template, Tooling, Style, Considerations | Defaults from catalogs; archetype-specific concerns |
| **Purpose** | Primary goal | Template (CTA hierarchy), Style (tone) | What the site optimizes for |
| **Purpose** | Audience | Template (mobile-first, i18n), Style (type, motion), Considerations (a11y) | Real people drive structure |
| **Purpose** | Lifespan | Tooling, Considerations | Boring stack vs experimental; sunset planning |
| **Purpose** | Scope (page count, dynamism) | Tooling (SSG vs SSR vs hybrid), Considerations | Rendering strategy |
| **Purpose** | Regulatory regimes | Tooling, Style (legal copy), Considerations | Hosting region, providers, compliance section |
| **Purpose** | Non-goals | All later phases | Prevents scope creep |
| **Template** | Interactivity needs | Tooling (framework), Style (motion) | JS-capable framework if any interactivity |
| **Template** | Pages requiring auth | Tooling (auth provider), Considerations (RBAC, audit) | Auth shape |
| **Template** | i18n decision | Tooling (i18n library), Style (RTL), Considerations (translation workflow) | Cascades broadly |
| **Template** | Content authoring source | Tooling (CMS), Considerations (workflow) | What writers use |
| **Template** | Real-time / personalized regions | Tooling (server, websockets), Considerations (caching) | Caching and architecture |
| **Template** | Form complexity | Tooling (validation library, server), Considerations (spam, a11y) | Forms as a system |
| **Tooling** | Rendering strategy | Style (what's expressible), Considerations (SEO indexability) | What styling can do, how search engines see the site |
| **Tooling** | Styling system | Style (tokens must map) | If Tailwind: theme-as-tokens; if vanilla: CSS vars |
| **Tooling** | Hosting + region | Considerations (data residency, latency, DR) | Where the bits live |
| **Tooling** | CMS choice | Style (component model alignment), Considerations (content workflow) | Editor workflow |
| **Tooling** | Auth provider | Considerations (session policy, MFA, SSO) | Authentication policy |
| **Tooling** | Payment / commerce stack | Considerations (PCI scope, tax, fraud, returns) | All commerce ops |
| **Tooling** | Analytics / monitoring | Considerations (event taxonomy, alerting) | What gets measured |
| **Style** | Motion personality | Considerations (reduced-motion testing) | Accessibility tests |
| **Style** | Theme modes | Considerations (contrast audits per mode) | Test coverage |
| **Style** | Color tokens | Considerations (contrast compliance) | WCAG conformance |
| **Style** | Font sourcing | Considerations (privacy if hosted externally) | GDPR (Google Fonts ruling, etc.) |
| **Considerations** | (any) | (may loop back to any prior phase) | If something is impossible under current decisions, revise |

When you open a new phase doc, read this table for the rows whose **From** column is a prior phase. List the relevant inherited constraints to the user as part of your phase-opening message.

---

## 6. The unified spec — `spec/SPEC.md`

After Phase 5 is approved, you produce **one consolidated document**: `spec/SPEC.md`. This is the contract for implementation.

### 6.1 Structure

```markdown
# Site Specification — <project name>

> Source artifacts: spec/intake.md, spec/purpose.md, spec/template.md,
> spec/tooling.md, spec/style.md, spec/considerations.md
> Approved by user on <date>.

## Executive summary
<3–5 sentences: what's being built, for whom, on what stack, with what visual language, and any standout constraints.>

## Purpose
<full content of spec/purpose.md>

## Template
<full content of spec/template.md>

## Tooling
<full content of spec/tooling.md>

## Style
<full content of spec/style.md>

## Considerations
<full content of spec/considerations.md>

## Sign-off
- User approval date:
- Outstanding open questions (carry-over):
- Anticipated next step: implementation per verification.md, or hand to <team/agency>.
```

### 6.2 Assembly procedure

1. Read every artifact in `spec/`.
2. Concatenate per the structure above.
3. Write an executive summary that someone unfamiliar with the project could read in 90 seconds.
4. List any open questions that survived all phases.
5. Present the full `SPEC.md` to the user. Get explicit, named sign-off ("approved on <date>" recorded in the artifact).
6. Only after sign-off, proceed to implementation — optionally guided by [`verification.md`](./verification.md).

### 6.3 Why a single document

- Implementation agents (or human engineers) receive **one** file, not seven.
- The single artifact prevents drift between sources.
- The sign-off line makes the contract explicit and dateable.
- For a paid build, this is the document an invoice references.

---

## 7. Workflow

```
START
  │
  ├─ Read this file (AGENTS.md)
  ├─ Skim phase docs and catalogs for context
  │
  ├─ PHASE 0 — Intake (optional)
  │   ├─ User has filled questionnaire.md? → read it
  │   ├─ Open phases/0-intake.md, follow it
  │   ├─ Produce spec/intake.md
  │   └─ Phase gate (§4) → loop or proceed
  │
  ├─ PHASE 1 — Purpose
  │   ├─ Enumerate inherited constraints from intake (§5)
  │   ├─ Batch all questions (§3.2)
  │   ├─ Open phases/1-purpose.md, follow it
  │   ├─ Produce spec/purpose.md
  │   └─ Phase gate (§4) → loop or proceed
  │
  ├─ PHASE 2 — Template
  │   ├─ Enumerate inherited constraints (§5)
  │   ├─ Batch all questions
  │   ├─ Open phases/2-template.md, follow it
  │   ├─ Produce spec/template.md
  │   └─ Phase gate (§4) → loop or proceed
  │
  ├─ PHASE 3 — Tooling
  │   ├─ Enumerate inherited constraints (§5)
  │   ├─ Consult catalogs/stack-presets/ as starting points
  │   ├─ Batch all questions
  │   ├─ Produce spec/tooling.md
  │   └─ Phase gate (§4) → loop or proceed
  │
  ├─ PHASE 4 — Style
  │   ├─ Enumerate inherited constraints (§5)
  │   ├─ Batch all questions
  │   ├─ Produce spec/style.md
  │   └─ Phase gate (§4) → loop or proceed
  │
  ├─ PHASE 5 — Considerations
  │   ├─ Enumerate inherited constraints (§5)
  │   ├─ Use spec/purpose.md archetype to scope catalog walk
  │   ├─ Batch all questions
  │   ├─ Produce spec/considerations.md
  │   └─ Phase gate (§4) → loop or proceed
  │
  ├─ AGGREGATE — Assemble spec/SPEC.md (§6)
  │   ├─ Concatenate + executive summary
  │   ├─ Present for sign-off
  │   └─ Record approval date in artifact
  │
  └─ BUILD (out of framework scope)
      Optionally use verification.md as the build/handoff checklist.
```

---

## 8. Output artifacts

Each phase produces one markdown artifact in the **working project's** `spec/` directory:

| Phase | Artifact path | Validates against |
|-------|---------------|--------------------|
| 0 Intake | `spec/intake.md` | `schemas/intake.schema.json` |
| 1 Purpose | `spec/purpose.md` | `schemas/purpose.schema.json` |
| 2 Template | `spec/template.md` | `schemas/template.schema.json` |
| 3 Tooling | `spec/tooling.md` | `schemas/tooling.schema.json` |
| 4 Style | `spec/style.md` | `schemas/style.schema.json` |
| 5 Considerations | `spec/considerations.md` | `schemas/considerations.schema.json` |
| Aggregate | `spec/SPEC.md` | (no schema; structure documented in §6) |

The artifact templates are documented inside each phase doc. The JSON Schemas mirror those templates for agents that want to validate before handing off.

---

## 9. File map

```
phases/         The workflow — read in order, do not skip.
  0-intake.md           (optional)
  1-purpose.md
  2-template.md
  3-tooling.md
  4-style.md
  5-considerations.md

catalogs/       Reference material — pull from these as the phases direct.
  site-archetypes.md
  components.md
  stacks.md             (prose; principles and alternatives)
  stack-presets/        (concrete starter stacks per archetype)
    README.md
    astro-static.md, nextjs-typescript.md, sveltekit.md, rails.md,
    django.md, go-templ-htmx.md, shopify-custom-theme.md, starlight-docs.md
    domains/
      ecommerce.md, documentation.md, saas.md, marketing-landing.md
  style-systems.md
  considerations-catalog.md

schemas/        JSON Schema definitions for each phase artifact.

examples/       End-to-end walkthroughs of agent + user sessions.

questionnaire.md  Optional pre-session intake form (for the user).
verification.md   Handoff / implementation checklist after sign-off.
KICKSTART.md      Human-facing quickstart.
README.md         Repo overview.
AGENTS.md         (this file)
```

---

## 10. Quick reminders

- **You are the agent.** The user is human. You drive the questions; they make the decisions.
- **One phase at a time.** Do not blend phases. The user's brain will not.
- **Batch questions, then output.** Don't interleave.
- **Show, don't hide, your reasoning** when picking between catalog options — explain tradeoffs.
- **Catalogs and presets are starting points, not menus.** If nothing fits, build something custom and document it.
- **You are responsible for what you didn't ask.** When in doubt, ask.
- **Every phase exits through the gate in §4.** No exceptions.
- **Constraints flow downstream.** §5 is the map.
- **The aggregate `spec/SPEC.md` is the contract.** Sign it before building.
