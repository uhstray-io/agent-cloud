# Website Agent Framework

A meta-framework that **AI agents read** to build websites end-to-end with a human user. This repository does **not** contain a website. It contains the workflow, prompts, decision criteria, catalogs, and artifact templates an LLM agent uses to elicit requirements from a user and produce a complete, shippable site.

## Who this is for

- **LLM agents** (Claude, GPT, Gemini, etc.) tasked with building a website from a user's request.
- **Humans** designing, reviewing, or auditing such agent workflows.

## How to use it

If you are a **human**, start at [`KICKSTART.md`](./KICKSTART.md) — it explains how to hand this framework to your AI agent and what to expect from each phase. After that, browse `catalogs/` to see the reference material agents draw on, and read `examples/` for end-to-end walkthroughs.

If you are an **agent**, start at [`AGENTS.md`](./AGENTS.md). Read it fully, then move through the phase docs in order. Do not skip phases. Ask questions even about topics the framework does not enumerate.

## Structure

```
.
├── KICKSTART.md              For humans. How to use this framework with an agent.
├── AGENTS.md                 For agents. Master index — agents read this first.
├── README.md                 This file.
├── questionnaire.md          Optional pre-session intake form (paste to your agent).
├── verification.md           Handoff checklist for whoever implements from the spec.
│
├── phases/                   The workflow. Phase 0 is optional; 1–5 are sequential.
│   ├── 0-intake.md           Optional pre-flight: capture context from questionnaire.
│   ├── 1-purpose.md          What is the site for, and for whom?
│   ├── 2-template.md         What pages, layouts, components does it need?
│   ├── 3-tooling.md          What stack will build and run it?
│   ├── 4-style.md            What does it look and feel like?
│   └── 5-considerations.md   Everything else, scoped dynamically by purpose.
│
├── catalogs/                 Reference material the phases pull from.
│   ├── site-archetypes.md            Ecommerce, docs, blog, marketing, SaaS, etc.
│   ├── components.md                 Reusable UI components and when to use them.
│   ├── stacks.md                     Principles + alternatives prose for stack choices.
│   ├── stack-presets/                Concrete starter stacks (8 base + 4 domain overlays).
│   │   ├── README.md
│   │   ├── astro-static.md
│   │   ├── nextjs-typescript.md
│   │   ├── sveltekit.md
│   │   ├── rails.md
│   │   ├── django.md
│   │   ├── go-templ-htmx.md
│   │   ├── shopify-custom-theme.md
│   │   ├── starlight-docs.md
│   │   └── domains/
│   │       ├── ecommerce.md
│   │       ├── documentation.md
│   │       ├── saas.md
│   │       └── marketing-landing.md
│   ├── style-systems.md              Color, type, motion, design system references.
│   └── considerations-catalog.md     Comprehensive cross-cutting checklist.
│
├── schemas/                  JSON Schemas validating each phase artifact.
│   ├── intake.schema.json
│   ├── purpose.schema.json
│   ├── template.schema.json
│   ├── tooling.schema.json
│   ├── style.schema.json
│   └── considerations.schema.json
│
└── examples/                 End-to-end walkthroughs.
    ├── ecommerce-walkthrough.md
    └── docs-site-walkthrough.md
```

## Core principles

1. **Phases run in order**: (optional Intake) → Purpose → Template → Tooling → Style → Considerations → unified `SPEC.md`. Each phase's output feeds the next.
2. **Catalogs are non-exhaustive**. The framework names many common cases but does not name every possible one. Agents must surface anything not covered to the user rather than silently dropping it or substituting something convenient.
3. **Ask, don't assume — and batch questions per phase**. Within a phase, agents gather all clarifying questions before producing output. No interleaving.
4. **Phase gates are explicit**. Each phase ends with a recap, key decisions, downstream constraints, the catch-all question, and a request for explicit approval. No silent transitions.
5. **Constraints propagate downstream**. Decisions earlier in the workflow constrain later phases. The framework documents the propagation map in `AGENTS.md` §5.
6. **The unified `SPEC.md` is the contract**. After Phase 5 the five artifacts are assembled into one signed document. Implementation begins only after dated signoff.
7. **Loop back when needed**. If a later phase exposes a contradiction in an earlier one, the agent pauses, names the conflict, revises the earlier artifact, and re-validates every downstream artifact before continuing.

## Versioning

This framework is intentionally opinionated about *workflow* and intentionally agnostic about *technology*. Update the catalogs as the web platform evolves; the phase structure should remain stable.
