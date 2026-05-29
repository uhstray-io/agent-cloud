# Skill: run a WebSmith phase

A single phase of the WebSmith workflow, from start to gated exit.

## When to use this skill

The user has confirmed which phase to run (Phase 0-5). This skill walks one phase. To walk the full workflow, run this skill repeatedly in order.

## Inputs

- `<N>` — phase number (0-5)
- `<sitename>` — kebab-case name of the site under construction (drives output path)
- Prior-phase artifacts at `platform/services/<sitename>/context/spec/` (if any)

## Steps

1. **Read context**, in this order:
   1. `agents/websmith/context/AGENTS.md` (master rules)
   2. `agents/websmith/CLAUDE.md` (agent-cloud overrides)
   3. Every existing artifact in `platform/services/<sitename>/context/spec/`
   4. `agents/websmith/context/phases/<N>-*.md` (this phase's doc)
   5. Any catalogs referenced by the phase doc
2. **Enumerate inherited constraints** from prior-phase artifacts. State them back to the user before asking questions.
3. **Batch all clarifying questions** for this phase into one message. Group logically. Tell the user roughly how many decisions this phase needs.
4. **Wait for all answers.** Do not draft the artifact before then.
5. **Resolve the phase slug from `<N>`**:

   | `<N>` | `<slug>` |
   |-------|----------|
   | 0 | `intake` |
   | 1 | `purpose` |
   | 2 | `template` |
   | 3 | `tooling` |
   | 4 | `style` |
   | 5 | `considerations` |

   Then **draft the artifact** at `platform/services/<sitename>/context/spec/<slug>.md`. Validate it against `agents/websmith/context/schemas/<slug>.schema.json` if a schema exists.
6. **Run the phase gate** (per `AGENTS.md` §4):
   1. One-line recap.
   2. Key decisions list.
   3. Downstream constraints this phase imposes.
   4. The catch-all question.
   5. Explicit request for approval.
7. **Wait for "approved" / "next" / "yes proceed".** Do not advance.
8. If the user requests revisions, loop back to step 3 with their feedback.

## Phase 5 special case

After Phase 5 approval, assemble the unified `SPEC.md` in `platform/services/<sitename>/context/spec/SPEC.md` per `AGENTS.md` §6. Get dated, named signoff before any implementation begins.

## Anti-patterns

- Drafting before answers are in.
- Skipping the catch-all question.
- Auto-applying catalog defaults without offering them as choices.
- Writing spec files anywhere other than `platform/services/<sitename>/context/spec/`.
- Starting implementation before SPEC.md signoff.
