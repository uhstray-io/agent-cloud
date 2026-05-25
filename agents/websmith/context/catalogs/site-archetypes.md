# Site Archetypes

Reference profiles for common website types. Each entry describes the primary goal, typical audience, must-have pages, common components, must-not-forget concerns, and common pitfalls. Use during phase 1 (to identify the archetype) and as a starting point in phase 2 (template) and phase 5 (considerations).

> **Non-exhaustive.** If a site does not fit any archetype here, name a new one and document it. Hybrid sites pick more than one.

---

## E-commerce storefront

**Primary goal:** Sell goods or services directly to consumers.

**Audience:** Shoppers (varied demographics), repeat customers, gift buyers, support seekers.

**Must-have pages:** Home, category listing, product detail, cart, checkout (multi-step or single-page), order confirmation, account (orders, addresses, payment), search results, search empty state, 404, returns/policy, contact.

**Common components:** Product card, faceted filter, sort, pagination, image gallery, variant selector, add-to-cart, mini-cart, breadcrumbs, mega menu, newsletter signup, review widget, related products, cross-sell, urgency cues.

**Top considerations:** Product data model (variants/SKUs/bundles), inventory sync, taxes, shipping zones, payment processor, fraud, PCI scope, returns workflow, order emails, abandoned cart, accessibility of checkout (legally significant), conversion analytics.

**Common pitfalls:** Underestimating taxes and shipping complexity; gating checkout behind account creation; slow product images; broken empty/no-results states; ignoring mobile checkout UX.

---

## Marketplace

**Primary goal:** Connect buyers and sellers (or service providers and customers); transact between them.

**Audience:** Buyers, sellers, admin/moderation, sometimes a B2B layer.

**Must-have pages:** Two-sided onboarding (seller, buyer), listing index, listing detail, messaging/inquiry, dashboard for each side, dispute/refund flow, payouts, search.

**Common components:** Search + facets, listing card, seller profile, ratings/reviews, in-app messaging, identity verification UI, payout dashboard.

**Top considerations:** Trust & safety (verification, ratings, fraud), KYC for sellers, payment splits/payouts (Stripe Connect or equivalent), dispute resolution, search relevance, two-sided onboarding friction, geographic regulation per side, content moderation.

**Common pitfalls:** Treating it as e-commerce + accounts (it isn't); under-investing in dispute and moderation; weak search relevance; cold-start (no inventory) marketing.

---

## Documentation site

**Primary goal:** Help users (often developers or specialists) find correct, current information fast.

**Audience:** Implementers, evaluators, support seekers; expertise ranges from novice to expert.

**Must-have pages:** Home / landing, getting started, conceptual guides, how-to / recipes, API/CLI reference, FAQ, search results, glossary, changelog, contributing (if OSS).

**Common components:** Sidebar nav with collapsible sections, top breadcrumbs, on-page TOC, code block with copy and tabs for multi-language, search modal with keyboard nav, version selector, callouts/admonitions, edit-on-GitHub link, prev/next, related links.

**Top considerations:** Search (correctness > recall), versioning, code-sample accuracy, dark mode (developers expect it), copy-to-clipboard everywhere, link rot detection, translation drift, API reference auto-generation, structured data for SEO, accessibility.

**Common pitfalls:** Underinvesting in search; stale code samples; no versioning until you need it badly; bad mobile UX (devs read on phones too); auto-generated reference that's hard to navigate.

---

## Blog / news / magazine

**Primary goal:** Publish and surface editorial content; build an audience.

**Audience:** Readers (one-time, returning, subscribed), authors, editors.

**Must-have pages:** Home (latest + featured), article, category/tag listing, author profile, archive (date/tag/author), search, RSS feed, newsletter signup, about, contact, 404.

**Common components:** Article card (multiple sizes), author byline, tag chips, related articles, share buttons, comments (optional), reading-time, newsletter inline CTA, paywall (if applicable).

**Top considerations:** RSS, OG/Twitter cards, structured data (Article, NewsArticle), reading experience (line length, typography), comments and moderation (or not), newsletter integration, sharing, paywall and metered access if relevant, content scheduling.

**Common pitfalls:** No RSS; bad social previews; reading typography sized for designers, not readers; comments without moderation strategy; auto-playing media.

---

## Marketing site / landing page

**Primary goal:** Convince a visitor to take one specific action (signup, download, demo, purchase).

**Audience:** Prospects (often acquired via ads, search, or social).

**Must-have pages:** Home / hero, product, features, pricing (often), about, contact, lead form, thank-you / success, legal.

**Common components:** Hero with CTA, value-prop sections, social proof (logos, testimonials, stats), feature grid, comparison table, FAQ accordion, pricing table, lead form, footer CTA.

**Top considerations:** Page-speed budget (tight; LCP < 2.5s), CTA hierarchy, A/B testing infra, lead capture and CRM sync, attribution/UTM, pixel tracking + consent, OG/Twitter cards, GDPR/CCPA consent management, mobile-first.

**Common pitfalls:** Hero that fights for attention (multiple competing CTAs); pricing page that hides pricing; testimonials without faces/names; forms that ask too much; analytics without consent.

---

## Portfolio / personal

**Primary goal:** Showcase work, attract clients/employers/collaborators.

**Audience:** Recruiters, prospective clients, peers.

**Must-have pages:** Home / about, work index, project detail (one per piece), contact, sometimes blog/now/uses.

**Common components:** Project card, image-heavy hero, case-study layout, contact form, resume download.

**Top considerations:** Imagery weight management, accessibility of decorative content, alt text, performance despite heavy media, contact spam protection, mobile reading experience.

**Common pitfalls:** 20MB hero image; case studies that are just screenshots; no clear contact path; auto-playing background video.

---

## SaaS product (marketing + app)

**Primary goal:** Acquire users (marketing) and serve them (app).

**Audience:** Prospects, free users, paying customers, admins, support.

**Must-have pages (marketing):** Home, product overview, features, pricing, customers, blog, docs, security, sign-up, sign-in, contact, legal.

**Must-have flows (app):** Sign-up + email verification, onboarding, primary product UX, settings (account, team, billing, integrations), admin console, support entry point.

**Common components:** Marketing — same as marketing-site. App — sidebar nav, top bar with org/account switcher, data tables, forms, modal/drawer, empty states, in-product education.

**Top considerations:** Auth, billing (Stripe Billing / Paddle), plan management, RBAC, audit log, status page, customer-facing webhooks/API, account deletion, data export, GDPR/CCPA, onboarding metrics, churn analytics.

**Common pitfalls:** Marketing and app diverging in look-and-feel; checkout-to-onboarding handoff awkwardness; no empty state design; no admin console until you need one badly.

---

## Community / forum

**Primary goal:** Enable members to interact, share, and find each other.

**Audience:** Members (lurkers, contributors, power users), moderators, admins.

**Must-have pages:** Sign-up, profile, feed/timeline, post detail, category/board, search, notifications, settings, moderation queue.

**Common components:** Post composer, comment thread (often threaded), reactions/voting, user avatar with hover card, notification bell, search.

**Top considerations:** Identity model (real name vs handle), spam and abuse mitigation, moderation tools, reputation / karma, search, real-time updates (use sparingly), notifications (multi-channel), code of conduct enforcement.

**Common pitfalls:** Treating it like Reddit when you should treat it like Slack (or vice versa); under-investing in moderation tools; spam floods on day 1 if open registration; notifications that drive users away.

---

## Educational platform / LMS

**Primary goal:** Deliver learning experiences and track progress.

**Audience:** Learners, instructors, institutional admins (sometimes).

**Must-have pages:** Course catalog, course detail, lesson, quiz/assessment, learner dashboard, instructor dashboard, certificates, discussion (often), purchase / enroll.

**Common components:** Course card, lesson video + transcript, progress bar, quiz UI, discussion thread, certificate template.

**Top considerations:** Video hosting and playback, captions/transcripts (legally required in many regions, also SEO), progress tracking, certificates, drip release, payments/subscriptions, accessibility of all media, mobile playback.

**Common pitfalls:** No captions; broken progress tracking; video-only with no text alternative; checkout flow that loses learners.

---

## Nonprofit / cause

**Primary goal:** Inform, mobilize, fundraise.

**Audience:** Donors, volunteers, beneficiaries, press.

**Must-have pages:** Home, mission, programs, impact/stats, donate, volunteer, events, news, contact, transparency (financials, leadership), tax info.

**Common components:** Donation form (one-time / recurring), impact stat blocks, story cards, event card, newsletter signup.

**Top considerations:** Donation processor (Stripe, Donorbox, etc.), recurring donations, tax receipts, donor recognition (or anonymity), volunteer signups, compliance with charity regulations per jurisdiction, accessibility (often legally required), trust signals.

**Common pitfalls:** Donation flow with too much friction; no receipts; impact claims without evidence; tone that feels corporate.

---

## Event / conference

**Primary goal:** Promote and sell access to an event.

**Audience:** Prospective attendees, speakers, sponsors, press.

**Must-have pages:** Home, schedule/agenda, speakers, sponsors, tickets/registration, venue/location, FAQ, code of conduct, post-event content.

**Common components:** Schedule grid with filtering, speaker card with bio, sponsor tier display, ticket selector, personal schedule builder, map.

**Top considerations:** Ticket sales (Tito, Eventbrite, custom), schedule generator with personal save, sponsor lead capture, live streaming, on-demand replay, accessibility (captions mandatory for streams), code of conduct.

**Common pitfalls:** Schedule released too late; broken time zones; no captions on streams; sponsor tiers that aren't differentiated visually.

---

## Restaurant / hospitality

**Primary goal:** Convert visitors into diners / guests / customers.

**Audience:** Locals, travelers, repeat patrons.

**Must-have pages:** Home, menu, reservations / order, locations, contact, gallery, about, hours, events.

**Common components:** Menu sections with dietary tags, reservation widget, gallery, map embed, hours block.

**Top considerations:** Menu freshness (the most common stale content), allergen flags, reservation provider (OpenTable, Resy, Tock), takeout/delivery integration, hours (including holidays), photos that look like the actual food, mobile-first (most visitors search on phones).

**Common pitfalls:** PDF menus; outdated hours; no reservation/order link visible without scrolling; broken Google Maps embeds.

---

## Real estate

**Primary goal:** Surface listings, generate inquiries, support agents.

**Audience:** Buyers, renters, agents, sometimes landlords/owners.

**Must-have pages:** Search/map, listing detail, agent profile, saved searches, inquiry form, mortgage calculator, neighborhood info, sell-your-home or list-with-us.

**Common components:** Listing card, map with clustered pins, image gallery / virtual tour, filter sidebar, inquiry form, mortgage calculator.

**Top considerations:** MLS/IDX integration, listing freshness, saved-search alerts, agent attribution and contact, fair housing compliance, accessibility, geo accuracy.

**Common pitfalls:** Slow map; stale listings; agent contact info buried; no saved searches; fair-housing compliance afterthought.

---

## Government / public service

**Primary goal:** Help citizens find, understand, and use public services.

**Audience:** Broadest possible public — every demographic, ability, language, device.

**Must-have pages:** Service finder, service detail, forms, contact, news, accessibility statement, language picker, legal pages.

**Common components:** Search-first, plain-language explainers, multi-step forms with save-and-resume, language switcher, accessible widgets.

**Top considerations:** Accessibility (often legally required, WCAG AA minimum, sometimes AAA on critical paths), plain language (Hemingway/grade-level targets), multilingual support, data minimization, no third-party trackers without consent, form accessibility, procurement constraints on tools.

**Common pitfalls:** Designing for a designer's preferred device; ignoring older browsers and low-bandwidth users; bureaucratic language; embedded tools that are not accessible.

---

## Internal tool / dashboard

**Primary goal:** Let an internal team do work efficiently.

**Audience:** Employees in a specific role.

**Must-have pages:** Sign-in (often SSO), main workspace, role-specific dashboards, settings, admin, audit log.

**Common components:** High-density data table (sort, filter, paginate, bulk action), saved views, side nav, tabs, modal, drawer, command palette, keyboard shortcuts.

**Top considerations:** SSO, RBAC, audit logging, exports (CSV/Excel), keyboard-driven UX (power users), time zone handling, on-call alerts for breakage, internal-only deployment.

**Common pitfalls:** Designing it like a consumer app; ignoring keyboard shortcuts; no audit log until needed; no role separation until needed badly.

---

## Personal site / digital garden

**Primary goal:** Writer's outlet, public identity, archive.

**Audience:** Friends, peers, search engines, recruiters.

**Must-have pages:** Home, posts/notes index, post detail, about, RSS, optional now/uses/colophon.

**Common components:** Post card, tag list, RSS link, contact / social links.

**Top considerations:** RSS, simple writing experience for the owner, archive permanence (URLs that don't rot), web mention support if indieweb-adjacent, posting friction (the lower, the more you'll post).

**Common pitfalls:** Over-engineering; building a CMS instead of writing; URL changes that break old links; no RSS.

---

## Beyond these archetypes

If the user describes something this list does not cover — *a tool for booking veterinary appointments*, *a digital memorial*, *a wedding site*, *a corporate intranet*, *a developer playground*, *an AI app*, *an experimental art piece* — that is a real archetype. Name it in the artifact. Define its primary goal, audience, must-haves, and considerations from first principles. Then add it to a local notes file so the next agent can re-use it.

This catalog should grow over time. Pull requests welcome.
