# Phase 0 — Intake

> *Gather everything the agent will need before it starts asking phase-by-phase questions. Optional. Skippable.*

This phase exists so later phases interrupt the user less. The user fills out [`questionnaire.md`](../questionnaire.md) once; the agent reads the answers and uses them to skip questions whose answers are already known and to make better-informed proposals.

Phase 0 is **optional**. A user who can't articulate a website yet should still be able to start at Phase 1, and the agent will extract the same information through conversation. Phase 0 is a shortcut for users who can answer most questions before the first session.

---

## 1. Goal of this phase

Produce `spec/intake.md`: a structured record of the user's pre-session context — devices, audience, compliance, performance, accessibility, team, deployment, integrations, content, brand, references, constraints. This artifact feeds every subsequent phase.

If the user can't answer some questions, those become **open questions** the agent must explicitly resolve during the relevant phase.

---

## 2. Inputs

- The user's filled-in `questionnaire.md` (or partial fill).
- Anything else the user has volunteered: existing brand assets, prior site URLs, screenshots, sitemaps, brand books, hosting accounts.

---

## 3. How to run this phase

There are two paths.

### Path A — user has filled the questionnaire

1. Read `questionnaire.md` as completed by the user.
2. Read every answer carefully. Flag:
   - **Strong signals** that anchor downstream phases (e.g., "GDPR + EU users + medical data" → HIPAA-adjacent → constrains Tooling, Style, Considerations significantly).
   - **Internal contradictions** (e.g., "static site" + "real-time personalization") and surface them now, not later.
   - **Gaps** — fields left blank that you'll need to fill during the relevant phase.
3. Produce `spec/intake.md` (see template below).
4. Confirm understanding with the user in 5–10 bullets. Get explicit approval before moving to Phase 1.

### Path B — questionnaire not used

If the user hasn't filled the questionnaire, ask them three questions:

1. *"In one paragraph: what are we building and why now?"*
2. *"What do you already have? Brand, code, accounts, infrastructure, anything."*
3. *"What can't be changed? Legal, budget, team, timeline, regulations, existing systems."*

Capture answers in a brief `spec/intake.md`. The remaining intake fields stay as "open — to be resolved in phase N" notes.

---

## 4. What intake captures (and why each field matters)

Every field below has a **downstream consequence** — what later phase(s) it constrains. The matrix in [`AGENTS.md`](../AGENTS.md#constraint-propagation-matrix) formalizes this.

### 4.1 Project basics
- Working title
- Working directory path
- Existing repo? (URL if so)
- Existing site to migrate from?
- Target launch date

*Constrains:* Tooling (timeline → boring vs ambitious), Considerations (cut-over plan, redirects).

### 4.2 People
- Solo founder / small team (2–5) / large team / agency
- Technical expertise of the maintainer post-launch
- Team's existing language and framework preferences
- Who writes content
- Who answers customer questions

*Constrains:* Tooling (match skill), Style (tone of voice), Considerations (handoff, content authoring, support).

### 4.3 Audience and devices
- Primary persona description
- Geographic distribution
- Languages at launch and roadmap
- Device mix (% mobile, desktop, tablet, accessibility tech)
- Network expectations (broadband, mobile, low-bandwidth markets)
- Browser support matrix

*Constrains:* Template (mobile-first vs desktop-first, i18n), Style (type sizing, motion), Tooling (rendering strategy, CDN regions), Considerations (accessibility level, performance budgets).

### 4.4 Goal and success
- One-sentence purpose
- Primary metric and rough target
- Success horizon (3, 6, 12 months)

*Constrains:* Purpose (becomes the artifact's spine).

### 4.5 Compliance and regulation
- Regions where users live
- Regulatory regimes the project is subject to (GDPR, CCPA, HIPAA, COPPA, PCI, ADA, EAA, LGPD, PIPL, etc.)
- Industry-specific (financial, medical, legal, education, government)
- Existing legal counsel?
- Data residency requirements

*Constrains:* Tooling (hosting region, providers), Considerations (privacy, consent, security, data subject rights), Template (consent UI), Style (legal copy tone).

### 4.6 Performance budget
- Hard ceilings: LCP / INP / CLS targets
- JS bundle size ceiling
- Page weight ceiling
- Network conditions to target

*Constrains:* Tooling (rendering strategy, framework, dependencies), Style (image strategy, motion, font loading), Considerations (perf testing).

### 4.7 Accessibility requirement
- WCAG target (A / AA / AAA)
- Legal mandate? (government, regulated industry, EU EAA)
- Manual testing capacity
- Assistive-tech users in primary audience?

*Constrains:* Template (semantics, landmarks), Style (contrast, motion, focus), Tooling (a11y in CI), Considerations (a11y statement, audit cadence).

### 4.8 Tech preferences and constraints
- Languages the team knows and will keep using
- Languages the team has decided against
- Existing vendor accounts (hosting, payments, email, analytics)
- "Must use X" or "must not use X" rules
- Open-source vs proprietary preference
- Cost ceilings (build, run)

*Constrains:* Tooling (every choice), Considerations (cost monitoring, vendor risk).

### 4.9 Content and integrations
- Where current content lives
- Volume of content (counts of products, articles, pages, etc.)
- Integrations the site must talk to (CRM, CMS, payment, search, ERP, ticketing, identity, etc.)
- Webhooks / events the site must consume or emit
- AI / ML features required

*Constrains:* Template (CMS-driven sections), Tooling (CMS choice, integration shapes), Considerations (content authoring workflow).

### 4.10 Brand and visual references
- Existing brand assets (logo, colors, fonts, brand book)
- Reference sites liked + what about them
- Reference sites disliked + why
- Adjectives for desired feel (3–5)
- Adjectives to avoid (3–5)

*Constrains:* Style (every decision).

### 4.11 Hosting and deployment
- Existing hosting accounts (Vercel, AWS, Cloudflare, etc.)
- Preferred regions
- Data residency requirements
- CDN / edge requirements
- CI/CD platform preference
- Domain situation (own it, need to buy, transferring)

*Constrains:* Tooling (hosting choice), Considerations (deployment, DNS, regions, DR).

### 4.12 Operational appetite
- How much ops the team is willing to do post-launch
- On-call / monitoring expectations
- Uptime SLO target
- Backup / DR appetite

*Constrains:* Tooling (managed vs self-hosted), Considerations (monitoring, alerting, DR).

### 4.13 Non-goals and out-of-scope
- Things this site is explicitly NOT for
- Features explicitly deferred
- Audiences explicitly excluded

*Constrains:* Every later phase — prevents scope creep.

### 4.14 Open questions
- Things the user knows they don't know yet
- Decisions waiting on external input

*Tracked here so they don't get lost. Resolved during the relevant later phase.*

---

## 5. Output artifact: `spec/intake.md`

Use the same headings as the questionnaire so the agent and user can cross-reference. Mark unanswered fields **OPEN (to resolve in phase N)** rather than deleting them.

````markdown
# Intake

## Project basics
- Working title:
- Working directory:
- Existing repo:
- Existing site to migrate from:
- Target launch date:

## People
- Team size:
- Maintainer expertise:
- Language/framework preferences:
- Content authors:
- Support owners:

## Audience and devices
- Primary persona:
- Geographic distribution:
- Languages (launch, roadmap):
- Device mix:
- Network expectations:
- Browser support:

## Goal and success
- One-sentence purpose:
- Primary metric (target):
- Success horizon:

## Compliance and regulation
- User regions:
- Applicable regulations:
- Industry specifics:
- Legal counsel:
- Data residency:

## Performance budget
- LCP / INP / CLS targets:
- JS bundle ceiling:
- Page weight ceiling:
- Network targets:

## Accessibility requirement
- WCAG target:
- Legal mandate:
- Manual testing capacity:
- AT users in audience:

## Tech preferences
- Known/preferred languages:
- Avoided languages/tools:
- Existing vendor accounts:
- Must-use / must-not-use rules:
- OSS vs proprietary:
- Cost ceilings (build/run):

## Content and integrations
- Current content location:
- Content volume:
- Required integrations:
- Webhooks/events:
- AI/ML features:

## Brand and visual references
- Existing brand assets:
- Liked references (URL + note):
- Disliked references (URL + note):
- Desired-feel adjectives:
- Avoid adjectives:

## Hosting and deployment
- Existing accounts:
- Preferred regions:
- Data residency:
- CDN/edge needs:
- CI/CD preference:
- Domain:

## Operational appetite
- Post-launch ops capacity:
- On-call expectations:
- Uptime target:
- Backup/DR appetite:

## Non-goals
- Out of scope:

## Open questions
- <field>: open — resolve in phase <N>
````

---

## 6. Phase gate (Phase 0 → Phase 1)

Before exiting Phase 0:

- [ ] `spec/intake.md` exists and every section is either filled or marked OPEN with the resolving phase named.
- [ ] Contradictions (if any) have been surfaced and either resolved or noted as questions to settle in the relevant phase.
- [ ] Hard constraints (regulations, must-use vendors, immovable launch dates) are flagged prominently.
- [ ] User has reviewed the intake summary and approved it — or explicitly chose to skip Phase 0.

See [`AGENTS.md` → Phase gate protocol](../AGENTS.md#phase-gate-protocol) for the approval format.

---

## 7. Common traps

- **Treating intake as binding.** Intake is the starting context; later phases can revise it. If the user says "I want Tailwind" in intake and Phase 3 surfaces a reason not to use Tailwind, raise it. Don't follow intake blindly.
- **Asking the user to fill every field.** Many users won't know answers like "WCAG target" or "TTFB ceiling" until prompted. Don't refuse to proceed; mark them OPEN and resolve later.
- **Skipping intake entirely without recording the choice.** If the user skips Phase 0, write a one-line `spec/intake.md` noting "skipped — extract during phases." Don't leave the artifact missing.
- **Burying contradictions.** If the user says "static site" and "real-time personalization" in the same intake, that's a Phase 1 / Phase 3 conflict. Flag it now.
