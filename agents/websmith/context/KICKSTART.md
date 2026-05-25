# Kickstart — How to Use This Framework

This file is for **you, the human**. The rest of the repo is for the AI agent that will build your site. Read this once before your first session.

---

## What this framework actually does

You want a website. You have an AI agent. This framework is the script your agent will follow so you both end up with a complete, well-defined site instead of a guess.

The agent walks you through **five phases**, in order, with an **optional Phase 0** at the start:

0. **Intake** *(optional)* — one-time context capture from your filled-in [`questionnaire.md`](./questionnaire.md). Skipable.
1. **Purpose** — what is this site for, and for whom?
2. **Template** — what pages, layout, navigation, and components?
3. **Tooling** — what stack will build, host, and run it?
4. **Style** — what does it look and feel like?
5. **Considerations** — everything else (accessibility, SEO, legal, deployment, etc.), scoped to your specific site.

At the end of each phase, the agent produces a **decision document** that you review and approve through a **phase gate**: the agent presents a one-line recap, key decisions, downstream constraints, asks the catch-all question, and asks for your explicit approval. Nothing moves forward without it.

Once all five are approved, the agent assembles them into one signed contract — `spec/SPEC.md` — and only **then** does it start writing code (or hand off the spec to whoever does).

---

## What you need before you start

- **An AI agent that can read multiple files in this repo.** Claude Code, Cursor, Windsurf, Aider, ChatGPT (with file upload or a connected repo), Gemini in a similar setup — anything that can read `AGENTS.md` and the files it links to. A chat that can't see the files won't work as well.
- **A separate working directory** for the actual website project. Don't build your site inside this framework repo — it should live somewhere else (e.g., `~/projects/my-bakery-site`).
- **30–90 minutes for the first session.** You can spread it over multiple sessions; each phase is independent enough that you can pause and resume.
- **A rough idea of what you want.** "I want a website for my bakery" is enough. The agent will pull the rest out of you.

---

## How to start a session

### Recommended path — fill the questionnaire first

1. Open [`questionnaire.md`](./questionnaire.md). Fill in what you can. **Leave blanks for anything you don't know.** The agent will ask about blanks during the relevant phase.
2. Save it (or have it ready to paste).
3. In your agent (Claude Code, Cursor, ChatGPT with files, etc.), paste:

> *I want to build a website. Read `AGENTS.md` in this repo and follow it. Start with Phase 0 — here's my filled-in questionnaire: Here's my intake. Read `phases/0-intake.md`. Work through all phases with me. Do not start writing code until we've completed every phase and I've signed off on the unified `spec/SPEC.md`.*

### Faster path — skip the questionnaire

If you can't yet articulate enough to fill the questionnaire, that's fine:

> *I want to build a website. Read `AGENTS.md` in this repo and follow it. I haven't filled out the questionnaire — start at Phase 1 and we'll extract intake as we go.*

### If your agent can't read this repo directly

1. Open `AGENTS.md` and paste its full contents into the chat.
2. Then paste your kickoff prompt from above.
3. When the agent asks for a phase doc, open the file (e.g., `phases/1-purpose.md`) and paste its contents.

Repo-aware agents are smoother. If you can use one, do.

---

## Where the agent's output goes

The agent will create files in a **separate** working directory — not in this framework repo. As you complete each phase, an artifact like this gets written:

```
my-bakery-site/             ← your actual project, separate from this framework
└── spec/
    ├── intake.md           (if you did Phase 0)
    ├── purpose.md
    ├── template.md
    ├── tooling.md
    ├── style.md
    ├── considerations.md
    └── SPEC.md             (the signed aggregate contract, after Phase 5)
```

These are your decisions, written down. The agent will reference them constantly. When the build starts, `SPEC.md` is the contract.

If the agent tries to put `spec/` inside this framework repo, stop it and point it to your project directory.

---

## What to expect from each phase

### Phase 0 — Intake (5–15 minutes, optional)
If you filled the questionnaire, the agent reads it, summarizes back, flags contradictions, and notes anything you left blank as "open — will resolve in phase N." If you didn't fill it, the agent asks three big questions and moves on. Either way, you sign off and proceed.

### Phase 1 — Purpose (15–30 minutes)
The agent asks what the site is for, who it's for, what success looks like, what constraints you have, and what it's **not** for. You may feel like some questions are obvious; answer anyway. The answers shape every later decision.

### Phase 2 — Template (20–45 minutes)
You'll map out pages, layouts, navigation, and components. Expect to talk about mobile, empty states, error states, and what's gated behind login. Bring example URLs of sites you like.

### Phase 3 — Tooling (15–40 minutes)
The agent proposes a stack and explains the tradeoffs. You don't need to know what every tool does — push back on anything that feels wrong, ask the agent to defend choices, and steer based on your team's skills and budget.

### Phase 4 — Style (20–40 minutes)
Colors, type, spacing, motion, voice. Bring 2–3 reference sites and 3 adjectives for how it should feel + 3 for what to avoid. The agent will translate that into design tokens.

### Phase 5 — Considerations (20–45 minutes)
Everything that doesn't fit cleanly in the first four phases — accessibility, SEO, legal, deployment, CI/CD, content, analytics, monitoring, maintenance. This phase is **dynamic**: which items get walked depends on the archetype from phase 1. Don't skip it; most preventable launch problems live here.

---

## Your job as the human

The agent drives the questions. You make the decisions. A few things you specifically need to do:

- **Answer honestly, not optimistically.** "We'll have a team to maintain it" when you won't = a stack you can't operate.
- **Push back on anything that feels wrong.** The agent is opinionated by design; your context wins.
- **Say "I don't know" when you don't.** It's a valid answer. The agent should help you decide or note it as an open question.
- **At the end of every phase, the agent will ask: *"Is there anything I haven't asked about that you think matters?"*** Take this seriously. It's the catch-all for things the framework doesn't anticipate. Real examples that came up in trials:
  - "Each piece I sell is one-of-a-kind, qty = 1."
  - "We need a hiatus mode when I'm on holiday."
  - "I want search analytics so I know what people couldn't find."

  None of these were in any catalog. All of them mattered.

- **Approve each phase before moving on.** The agent shouldn't proceed without your "yes." If it does, stop it.

---

## What's allowed in a session

- **Loop back.** If phase 3 reveals a contradiction with phase 2, ask the agent to update phase 2. That's expected, not a failure.
- **Pause and resume.** Each phase produces an artifact. Next session, the agent reads what's there and picks up.
- **Disagree with a catalog default.** Catalogs are starting points. If you want something not in them, the agent should build it, not substitute the nearest match.

## What's not allowed

- **The agent writing code during phases 1–5.** No scaffolding, no `npm install`, no running build tools. Only deciding.
- **"Let me figure that out later" on big questions.** If the agent lets you defer a major decision (regulatory, hosting region, payments), push back; deferred decisions are how scope explodes mid-build.
- **Skipping the "anything else?" question.** If the agent ever skips it, say "you forgot the catch-all question."

---

## After phase 5

The agent assembles your artifacts into **`spec/SPEC.md`** — the unified, signed contract. Read it end-to-end. If everything matches what you decided, give explicit, dated signoff. Now — and only now — the build begins.

If the build is going to happen in a separate session (a different agent, a human engineer, or an agency), point them at `spec/SPEC.md` plus [`verification.md`](./verification.md) at the root of this framework. The verification doc is a checklist for declaring a build done that traces back to the spec.

The unified spec is the contract. If the build deviates from it, you point at the spec.

---

## Common gotchas

- **The agent jumps to code.** Stop it. Remind it that phases 1–5 are decision-only.
- **The agent picks an archetype after one keyword.** "Store" doesn't always mean e-commerce. Make the agent confirm.
- **The agent treats catalogs as menus.** Catalogs are not exhaustive. If you want something custom, say so.
- **The agent skips mobile / empty states / errors.** All real sites have these. They're not optional.
- **The agent assumes regulations don't apply.** If you're in or serving the EU, US, UK, Australia, India, Brazil, etc., specific laws apply. Make it surface them.
- **Sessions get long and you lose track.** Ask the agent for a recap of decisions made so far. The artifacts in `spec/` are the source of truth — re-read them.

---

## What if I'm not sure my site fits the framework?

Use it anyway. The framework is structured around questions every website project needs to answer; the answers vary. If your project is unusual (an interactive art piece, a digital memorial, a one-off campaign microsite), the agent will adapt — the workflow still works.

If a phase genuinely doesn't apply (e.g., no styling needed because the site is pure JSON API), tell the agent and write "N/A — [reason]" in that artifact. Skip nothing silently.

---

## Quick reference

| You want to... | Do this |
|----------------|---------|
| Start with questionnaire | Fill `questionnaire.md`, then paste the kickoff prompt above |
| Start without questionnaire | Paste the "skip questionnaire" prompt above |
| Pause | Stop. The artifacts in `spec/` persist. |
| Resume | Tell the agent "we're resuming; read what's in `spec/`" |
| Change an earlier decision | Tell the agent which phase to loop back to |
| See where you are | Ask "what's been decided so far?" |
| Approve a phase | Say "approved", "next", or "yes proceed" |
| Reject a phase | Say what's wrong; the agent will revise |
| Force code generation | Don't, until `spec/SPEC.md` is signed off |
| Hand off the build | Point engineers/agency/another agent at `spec/SPEC.md` + `verification.md` |

---

## One final note

This framework is opinionated about **workflow**, not about **outcome**. There is no one right website. There is, however, a right *process* for figuring out what website you actually want — and skipping it produces sites that look fine and serve no one in particular.

Trust the phases. Answer the questions. Ask the agent the catch-all question if it forgets to ask you. The rest will follow.

Good luck.
