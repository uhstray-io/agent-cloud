# Phase 1 — Purpose

> *What is this website for, and for whom?*

This is the foundation. Every later decision — pages, stack, colors, considerations — references the artifact you produce here. Get it wrong and the rest of the workflow optimizes the wrong thing. Take your time.

---

## 1. Goal of this phase

Produce `spec/purpose.md`: a clear, agreed-upon statement of what the site exists to do, for whom, on what timeline, with what success criteria. Identify one or more **archetypes** (e-commerce, documentation, marketing, etc.) that the site fits, since downstream phases use the archetype to choose defaults and surface concerns.

---

## 2. Inputs

- **`spec/intake.md`** if Phase 0 was completed — it pre-answers much of this phase. Use it to skip questions whose answers are already known, and to inform your initial proposals.
- The user's initial request (free-form), if intake was skipped.
- Any existing brand, business, or product context the user volunteers.

Before drafting, enumerate inherited constraints from `spec/intake.md` per [AGENTS.md §5](../AGENTS.md#5-constraint-propagation-matrix).

---

## 3. Decisions to extract

Walk the user through each of these. Do not skip any. Multiple values are allowed where it makes sense (a site can have more than one audience, more than one goal, more than one archetype).

### 3.1 Primary goal

What is the single most important thing this site must accomplish? Pick from or combine:

- **Sell** — transact for goods or services.
- **Inform** — convey knowledge or reference material.
- **Capture** — collect leads, signups, or applications.
- **Persuade** — change minds or drive a specific action.
- **Entertain** — engage attention through content or interaction.
- **Connect** — enable users to find/talk to each other.
- **Support** — help existing users / customers self-serve.
- **Showcase** — demonstrate work, capability, or status.
- **Operate** — be the application itself (SaaS, internal tool).

If the user names more than one, ask which is **primary**. The primary goal breaks ties later.

### 3.2 Archetype(s)

Identify which archetype(s) from [`catalogs/site-archetypes.md`](../catalogs/site-archetypes.md) the site fits. Common options:

- E-commerce storefront
- Marketplace
- Documentation site
- Blog / news / magazine
- Marketing site / landing page
- Portfolio
- SaaS product (marketing + app)
- Community / forum
- Educational platform / LMS
- Nonprofit / cause
- Event / conference
- Restaurant / hospitality
- Real estate listing
- Government / public service
- Internal tool / dashboard
- Personal site

A site can hybridize (e.g., SaaS marketing site + docs + blog). When it does, note each archetype and which is primary.

If none fit, name a new one in the artifact and describe it. Catalogs are non-exhaustive.

### 3.3 Audience

Who uses this site? For each distinct audience, capture:

- Role / persona name
- Demographics relevant to design choices (age, profession, region)
- Expertise level (novice → expert)
- Device mix (desktop, mobile, tablet, kiosk, accessibility tech)
- Languages
- Accessibility considerations (screen readers, motor, cognitive, color vision)
- Network / device constraints (low bandwidth, older browsers)
- Stage in their journey (first-time visitor, returning, paying customer)

If the user names "everyone," push back. "Everyone" is not a useful audience. Get them to name at least one specific primary persona.

### 3.4 Success metrics

How will the user know the site is doing its job? Concrete examples:

- Revenue / orders / AOV / conversion rate
- Signups / leads / qualified leads
- Page views / sessions / dwell time
- Task completion (e.g., "users find the answer in <30s")
- NPS / CSAT
- Search ranking / domain authority
- Reduction in support tickets

If the user can't articulate this, ask: *"In six months, what would make you say this site was a success?"*

### 3.5 Scope and lifespan

- Single page, multi-page (small/medium/large), or app-like?
- Static content or dynamic / personalized?
- Lifespan: campaign (weeks), seasonal (months), long-lived (years+)?
- Will content/structure change frequently after launch?
- Multi-tenant or single-tenant?

### 3.6 Constraints

What's fixed before any design happens?

- Budget (rough)
- Timeline / launch date
- Existing brand assets / brand book
- Existing infrastructure to integrate with
- Existing accounts (Stripe, AWS, etc.) the user already has or refuses to use
- Legal / regulatory requirements the user already knows about (HIPAA, GDPR, COPPA, PCI, accessibility laws by region)
- Team capacity to maintain after launch

### 3.7 Non-goals

What is this site explicitly *not* for? Naming non-goals prevents scope creep in later phases. Examples: "Not a community" / "No user accounts" / "Not internationalized."

---

## 4. Question script

A starting set. Adapt to the user. Don't read these like a form — converse.

1. *"In one sentence, what do you want this website to do?"*
2. *"Who is it for? Walk me through one specific person who'd use it."*
3. *"In six months, what would make you call this a success?"*
4. *"Are there sites you'd point to and say 'something like that'? What about them?"*
5. *"What are you explicitly NOT trying to build?"*
6. *"Any constraints I should know about — budget, timeline, brand, regulations?"*
7. *"How often will the content or structure change after launch?"*
8. *"Is there anything I haven't asked about that you think matters?"*

The last question is non-negotiable. Ask it at the end of every phase.

---

## 5. Archetype detection rubric

If the user is vague about the archetype, use the primary goal to narrow:

| Primary goal | Likely archetype(s) |
|--------------|---------------------|
| Sell | E-commerce, marketplace |
| Inform | Documentation, blog/news, reference |
| Capture | Marketing/landing, SaaS marketing |
| Persuade | Marketing/landing, nonprofit/cause |
| Entertain | Blog/news, community, entertainment |
| Connect | Community/forum, social, marketplace |
| Support | Documentation, knowledge base, community |
| Showcase | Portfolio, personal, event |
| Operate | SaaS app, internal tool, dashboard |

Confirm with the user. Don't assume.

---

## 6. Output artifact: `spec/purpose.md`

Use this template. Fill every section. If something genuinely doesn't apply, write "N/A — [reason]" rather than deleting the section.

````markdown
# Purpose

## One-line summary
<single sentence stating what the site is for>

## Primary goal
<one of: Sell | Inform | Capture | Persuade | Entertain | Connect | Support | Showcase | Operate>

## Secondary goals
- <goal 1>
- <goal 2>

## Archetype(s)
- **Primary:** <archetype>
- **Secondary:** <archetype(s) if hybrid>

## Audience

### <Persona 1 name>
- Role:
- Demographics:
- Expertise:
- Device mix:
- Languages:
- Accessibility considerations:
- Network/device constraints:
- Journey stage:

### <Persona 2 name>
(repeat as needed)

## Success metrics
- <metric, with target if known>
- <metric, with target if known>

## Scope and lifespan
- Pages: <single | small (<10) | medium (10-50) | large (50+) | app-like>
- Content: <static | dynamic | personalized>
- Lifespan: <campaign | seasonal | long-lived>
- Change frequency: <rarely | monthly | weekly | daily>
- Tenancy: <single | multi>

## Constraints
- Budget:
- Timeline:
- Brand assets available:
- Existing infrastructure:
- Regulatory / legal:
- Team capacity post-launch:

## Non-goals
- <explicit out-of-scope item>
- <explicit out-of-scope item>

## Open questions
<anything the user couldn't answer yet — return to before exiting phase 1 if possible>
````

---

## 7. Exit criteria (phase gate)

Follow the [Phase gate protocol in AGENTS.md §4](../AGENTS.md#4-phase-gate-protocol). The phase exits when every box below is checked AND the user explicitly approves.

### 7.1 Artifact completeness
- [ ] One-line summary exists and the user has read it back or paraphrased it.
- [ ] Primary goal is a single value, not a list.
- [ ] At least one archetype is named; hybrids are acknowledged.
- [ ] At least one specific persona is fully filled (not "everyone").
- [ ] Success metrics are concrete enough that you'd know if the site missed them.
- [ ] Constraints — especially regulatory — have been explicitly surveyed, not assumed-absent.
- [ ] Non-goals exist and aren't empty.
- [ ] Every "open question" is named with the phase that will resolve it.

### 7.2 Catch-all
- [ ] You asked verbatim: *"Is there anything I haven't asked about that you think matters for this site?"*
- [ ] Anything the user surfaced has been recorded in the artifact.

### 7.3 Downstream constraints to flag at the gate

When presenting for approval, name the constraints this artifact imposes on later phases:

- Archetype → drives Template defaults + Considerations checklists + stack-preset shortlist.
- Audience (device mix, languages, accessibility) → Template (mobile-first, i18n), Style (type, motion), Considerations (a11y).
- Regulatory regimes → Tooling (hosting region, providers), Considerations (privacy, security, consent).
- Scope (page count, dynamism) → Tooling (SSG vs SSR vs hybrid).
- Lifespan → Tooling (boring vs ambitious), Considerations (sunset criteria).
- Non-goals → All later phases — prevents scope creep.

See [AGENTS.md §5 — Constraint propagation matrix](../AGENTS.md#5-constraint-propagation-matrix) for the full map.

### 7.4 Approval

User must reply with "approved", "next", or equivalent. Anything ambiguous → ask for explicit approval before proceeding.

### 7.5 If you need to revise this phase later

If Phase 2, 3, 4, or 5 surfaces a contradiction with Purpose:
1. Pause the current phase.
2. Name the conflict.
3. Update `spec/purpose.md` here per the revision protocol (AGENTS.md §4.4).
4. Re-validate **all** downstream artifacts in order (Template → Tooling → Style → Considerations).

---

## 8. Common traps

- **Accepting "everyone" as the audience.** Push for specifics.
- **Assuming the archetype from one keyword.** "Store" might be e-commerce, but it might be a brand showcase with no checkout.
- **Skipping regulations.** Healthcare, finance, kids, EU users, government — name the regimes that apply, even if the user says "we'll figure it out later."
- **Accepting vague success metrics.** "More users" is not a metric.
- **Forgetting lifespan.** Building a 5-year platform with one-week-campaign tooling (or vice versa) is the most expensive kind of mistake.
- **Letting later-phase concerns leak in.** No stack discussion in phase 1. No color discussion. Stay on purpose.
