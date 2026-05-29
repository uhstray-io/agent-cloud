# Intake Questionnaire

> **For the human.** Fill this in once before you start a session, then paste it to your agent (or save it where the agent can read it). The agent will use it to skip questions it would otherwise need to ask, and to make better-informed proposals.
>
> **Anything you don't know is fine.** Leave blanks. The agent will ask about them during the relevant phase. The goal is to capture what you already know — not to test you.

---

## Project basics

- **Working title for the site:** UhhCraft
- **Working directory** (where the site code will live, separate from this framework): ./output/ 
- **Existing repo?** (URL if so, otherwise blank)
- **Existing site to migrate from?** (URL + what's wrong with it, if relevant) None
- **Target launch date** (or "no deadline"):

---

## People

- **Team size:** (solo / 2–5 / 5–20 / 20+) 2
- **Who maintains the site after launch?** (you / a team / an agency / nobody decided) Me
- **Technical expertise of the maintainer:** (designer / developer / non-technical / mixed) developer
- **Languages and frameworks the team already knows and wants to keep using:** Rust, Go, C#, Java, Some JS, Some Python
- **Languages or frameworks the team has decided against:**
- **Who writes content** (product copy, articles, docs, etc.)? 
- **Who answers customer questions** (support, sales, none)? None

---

## Audience and devices

- **Primary user in one paragraph** — who are they, why are they here?
- **Where in the world do users live?** (country/region mix)
- **Languages needed at launch:**
- **Languages planned later:**
- **Device mix** — rough percentages: ___% mobile / ___% desktop / ___% tablet / ___% other
- **Network expectations:** (mostly fast / mixed / patchy / mobile-data heavy)
- **Browser support:** (latest evergreen / IE11 / specific named browsers / no opinion)
- **Accessibility tech in the audience?** (screen readers, keyboard-only, voice control — yes / no / unsure)

---

## Goal and success

- **What is the site for, in one sentence?** a shop where customers can buy unique one of a kind physical goods, primarily stickers and 3-D printed items designed using AI generated tools with the option for customers to either upload their own designs or have something crafted from scratch using AI.
- **The single most important thing it must do** (sell / inform / capture / persuade / entertain / connect / support / showcase / operate): Sell
- **How will you know it's working?** (one or two concrete metrics, with rough target): People Understand the purpose, and want to make a purchase.
- **By when?** (3 months / 6 months / 12 months): 3 Months

---

## Compliance and regulation

- **Regions of the world your users live in** (matters for laws — EU, California, Brazil, China, etc.): USA
- **Regulatory regimes that apply** (check any that apply, leave blank if unsure):
  - [ ] GDPR (EU/UK)
  - [ ] CCPA / CPRA (California)
  - [ ] HIPAA (US health)
  - [ ] PCI DSS (card payments)
  - [ ] COPPA (US, users under 13)
  - [ ] ADA (US accessibility)
  - [ ] EAA (EU Accessibility Act, 2025)
  - [ ] LGPD (Brazil)
  - [ ] PIPL (China)
  - [ ] Other:
- **Industry-specific obligations** (financial, medical, legal, education, government, etc.):
- **Do you have legal counsel?** (yes / no / TBD) No
- **Data residency requirements** (must data stay in a specific region? "no" is a valid answer):

---

## Performance

You may not know specific numbers — that's fine. Skip what you can't answer.

- **How fast must this feel?** (instant / fast / acceptable / no opinion) Acceptable
- **Page-load target (LCP):** (e.g., < 2.5s / no opinion) < 1 Second
- **Hard cap on initial JavaScript bundle:** (e.g., < 200KB / no opinion) no opinion
- **Hard cap on page weight:** (e.g., < 1MB / no opinion) no opinion
- **What devices and networks must perform well?** (e.g., "3-year-old Android on 4G"): 

---

## Accessibility

- **WCAG target:** (A / AA / AAA / not sure — pick AA if unsure) AA 
- **Is accessibility a legal mandate for this site?** (yes / no / unsure) unsure
- **Will the team test manually with screen readers and keyboard-only?** (yes / no / want help setting this up) No

---

## Tech preferences

- **Existing vendor accounts** (check those already in place):
  - [ ] Vercel
  - [ ] Netlify
  - [ ] Cloudflare
  - [ ] AWS
  - [ ] GCP
  - [ ] Azure
  - [ ] Shopify
  - [ ] Stripe
  - [ ] Auth0 / Clerk / Supabase
  - [ ] Sanity / Contentful / WordPress
  - [ ] Other: 
- **Things you must use:** (e.g., "we already use Postgres on RDS") Postgres For Databases.
- **Things you must not use:** (e.g., "no Google products" / "no JS-heavy frameworks")  no JS-heavy frameworks
- **Open-source vs proprietary preference:** (strong OSS / OSS-leaning / no preference / proprietary-leaning) strong OSS / OSS-leaning
- **Build budget cap:** NA
- **Run budget cap (monthly):**

---

## Content and integrations

- **Where does your content currently live?** (notes / Google Docs / spreadsheets / existing CMS / nowhere yet): nowhere yet
- **How much content?** Rough counts: NA
  - Products: _____
  - Articles / posts: _____
  - Doc pages: _____
  - Other pages: _____
- **Integrations the site must talk to:** (CRM, payments, search, ERP, ticketing, identity, analytics, etc.): We need to understand how we will handle payments... Shopify intigrations maybe? Something similar?
- **Webhooks or events** the site needs to consume or emit:
- **AI / ML features required:** (chat, recommendations, search, generation — or "none") Generatation of Images (stickers) and Generation of 3D models.

---

## Brand and visual references

- **Existing brand assets:** (logo, colors, fonts, brand book — link or describe)
  - Light Blue and Orange
  - Fox themed mascot 
- **Sites you like** (2–3 URLs + one sentence on what specifically you like about each):
  - _URL_ — _what_
  - _URL_ — _what_
  - _URL_ — _what_
- **Sites you dislike** (1–2 URLs + why):
  - _URL_ — _why_
- **Three adjectives for how this should feel:** Clean, Cute, Warm
- **Three adjectives for how it should NOT feel:** Sharp, Robotic, AI Generated

---

## Hosting and deployment

- **Preferred host(s):** (Vercel, Netlify, Cloudflare, AWS, GCP, Azure, self-hosted, no preference) self-hosted
- **Preferred regions:** self-hosted US
- **Edge / CDN needs:** (must be global / specific regions / no opinion) NA
- **CI/CD platform preference:** (GitHub Actions / GitLab / CircleCI / other / no preference) GitHub Actions
- **Domain situation:** (already own + name / need to buy / transferring / haven't decided) Already own "https://www.uhstray.io/" but will create subdomain "https://www.uhhcraft.uhstray.io/"

---

## Operational appetite

- **Post-launch ops capacity:** (zero — must be hands-off / a few hours/month / a team is dedicated) must be hands-off, send us discord webhook notifcations if there was a payment
- **On-call expectations:** (24/7 paging / business-hours / best-effort / none) none
- **Uptime target:** (99.99% / 99.9% / 99% / best-effort / no opinion) 99.99%
- **Backup and disaster recovery appetite:** (rigorous / moderate / minimal) moderate

---

## Non-goals

What is this site explicitly **not** for? List things you want to make sure don't sneak in.

- Not a: 
- Not for:
- Will not include:

---

## Anything else

Anything you want the agent to know that none of the above captured?

---

## Open questions for me to think about

Things you know you haven't decided yet:

- What type of Models or AI tools will be needed for Generating Stickers, cut-outs, 3d models.
- How do payments and tranctions get handled? What options do we have? If we use Shopify, can we test how transations happen?
- If we cant preform a job, can we send the item to a 3d party  shop to Create and ship the item.

---

When done, save this file (or paste its contents to your agent) and start Phase 0 by saying:

> *"Here's my intake. Read `phases/0-intake.md` and walk me through the intake summary. Then we'll start Phase 1."*

Or skip Phase 0 entirely and jump in with:

> *"I haven't filled out the questionnaire. Start at Phase 1 and we'll extract intake as we go."*
