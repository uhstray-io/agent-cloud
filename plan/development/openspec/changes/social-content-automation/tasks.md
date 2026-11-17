## 1. Plan and decisions

Needs nothing outside this change. Both tasks are done.

- [x] 1.1 Write plan 28 and this change (AC-793)
      Plane: AC-793. Blocked by: none.
- [x] 1.2 Decision: approval policy — drafts without approval, every schedule/publish approved, approval records approver + revision (AC-795, Joseph 2026-11-12)
      Plane: AC-795. Blocked by: none. Gate: Requirement "Drafts need no approval; scheduling and publishing need a recorded approval", scenario "Schedule refused without a recorded approval".

## 2. Brand profiles (needs nothing outside this change)

Needs nothing outside this change. Values for each brand live in site-config; the repository holds the schema and a placeholder example only.

- [ ] 2.1 Brand-profile schema + placeholder example in agent-cloud (AC-828)
      Plane: AC-828. Blocked by: none. Acceptance: the schema holds the fields of plan 28 D8 and the example holds placeholders only.
- [ ] 2.2 One profile per brand of the instance, values in site-config; done when 2.3, 2.4, 2.5 are done (AC-794)
      Plane: AC-794. Blocked by: 2.1. Acceptance: a rollup that closes when 2.3, 2.4 and 2.5 close; no brand value appears in the repository.
- [ ] 2.3 First brand profile (Gate A critical path) (AC-829)
      Plane: AC-829. Blocked by: 2.1. Acceptance: the profile validates against the schema from 2.1 and records a source for each field.
- [ ] 2.4 Second brand profile (AC-830)
      Plane: AC-830. Blocked by: 2.1. Acceptance: as 2.3.
- [ ] 2.5 Third brand profile (AC-831)
      Plane: AC-831. Blocked by: 2.1. Acceptance: as 2.3. Gate: Requirement "Brand profiles follow the schema and hold no brand value in the repository", scenario "Profile missing a source for a field is refused".

## 3. Flows as code and connections (needs a running Activepieces: change `deploy-activepieces`)

Needs a running Activepieces instance from change `deploy-activepieces`. The local bring-up comes first; production use waits for that change's validation gate.

- [ ] 3.1 Flows as code: `Manage Activepieces Flows` playbook (AC-796)
      Plane: AC-796. Blocked by: deploy-activepieces local bring-up (AC-772) for the spike; production use after its validation gate (AC-781). First spike: R1. Acceptance: spike R1 runs first; if the Community edition does not support the flow API path, stop and return the decision to the operator (edition, AC-768). Otherwise one apply playbook writes the connections, then the flows; a dry run shows the diff, a second run reports no change, and an unowned flow is left in place. It also delivers the kill switch (plan 28 D13) and the static check of Requirement "Flows never publish immediately".
- [ ] 3.2 Connection to agentgateway through the HTTP piece, key written from OpenBao by a no_log playbook, proven with one completion (AC-797)
      Plane: AC-797. Blocked by: 3.1; plan 27 identity (AC-785), client leaf (AC-825), egress spike (AC-826). Spike: R3. Acceptance: one completion returns through the `activepieces` identity, and no key appears in a flow definition. If the HTTP piece cannot take its key from a stored connection, choose another mechanism before this task closes.
- [ ] 3.3 Connection to Postiz through the Postiz piece (base_url and api_key upsert), key from OpenBao (AC-798)
      Plane: AC-798. Blocked by: 3.1; production Postiz key captured (AC-763). Acceptance: the connection upserts by its external id, a second apply changes nothing, and the key is read from the `postiz_api_key` field at `secret/services/postiz`. The procedure to revoke the key is recorded (plan 28, Open Questions). Gate: Requirement "Flows and connections are declared as code and applied by playbook", scenario "Re-run of the apply playbook converges".

## 4. Text pipeline (no connected social account needed, except where R2 says)

Needs the gateway and Postiz connections from phase 3 and the brand profile from phase 2. Drafts are proven end to end before Gate A. Risk R2 limits what this phase can prove without a connected account.

- [ ] 4.1 Flow: generate one draft per platform from a brief, json_schema output within each platform's length limit, prompt built from the brand profile (AC-799)
      Plane: AC-799. Blocked by: 3.2, 2.3. Acceptance: one draft per target platform, each reply parses against its schema, and the prompt is built from the brand profile.
- [ ] 4.2 Guardrail step (plan 28 D10) (AC-800)
      Plane: AC-800. Blocked by: 4.1. Acceptance: a draft over a platform's length limit, or naming an internal hostname, is returned with its reason and does not reach approval.
- [ ] 4.3 Brand-to-integration mapping and refusal (plan 28 D9) (AC-836)
      Plane: AC-836. Blocked by: 2.1. Integration ids are filled once accounts are connected. Acceptance: a target integration the mapping does not assign to the draft's brand is refused; the mapping values live in site-config.
- [ ] 4.4 Handoff flow: Postiz draft without approval; schedule only after a recorded approval, exactly once (plan 28 D2, D7) (AC-801)
      Plane: AC-801. Blocked by: 3.3, 4.2, 4.3. Spikes: R2 and the approval surface choice (a or b). Acceptance: the spike records whether a draft can exist with no connected integration (R2) and whether the status route exists on the pinned Postiz version; the approval surface is chosen and recorded in plan 28 Open Questions; a retried handoff creates no second post; `platform/services/postiz/context/use-cases.md` is updated from schedule-at-create to draft then status change.
- [ ] 4.5 Scheduled generation: a Schedule-trigger flow on a declared cadence from a topic backlog held in site-config; drafts still pass guardrail and approval (AC-802)
      Plane: AC-802. Blocked by: 4.4. Acceptance: the declared cadence stays under the post-creation ceiling (plan 28 R6), and every generated draft passes 4.2 and then 4.4.
- [ ] 4.6 Failure alerting for content flows through the o11y alert path (AC-804)
      Plane: AC-804. Blocked by: 3.1. Acceptance: a forced failing run raises an alert on the observability alert route. Gate: Requirement "Guardrail checks run before approval", scenario "Draft naming an internal hostname is refused"; Requirement "Handoff to the publishing service is exactly once", scenario "Retried handoff creates no second post"; Requirement "A failed content flow run raises an alert", scenario "Failed run raises an alert".

## 5. Gate A (needs connected X and LinkedIn accounts: operator)

Needs the X and LinkedIn developer apps and their connected accounts, which the operator provides. Gate A is the first live publication, and it targets the first brand only.

- [ ] 5.1 Gate A: an AI-drafted, human-approved text post published to the first brand's X and LinkedIn through Postiz in production; a rerun of the handoff does not double-post (AC-805)
      Plane: AC-805. Blocked by: 4.2, 4.4; gateway text gate (AC-791); Activepieces gate (AC-781); X and LinkedIn apps and connected accounts (AC-760, AC-761; Postiz change tasks 6.5, 7.1, 7.7). Acceptance: one post per approved revision is published to each of the two accounts, and a rerun of the handoff adds no post. Gate: Requirement "Drafts need no approval; scheduling and publishing need a recorded approval", scenario "Approval of one revision does not schedule the next revision"; Requirement "Handoff to the publishing service is exactly once", scenario "Retried handoff creates no second post".

## 6. Images (blocked on plan 27's image path)

Blocked on the image path in plan 27 and on the licence decision (AC-870). No task in this phase starts before both are settled.

- [ ] 6.1 Image step: brand image through the gateway's image route, approved with the text, uploaded to Postiz media and attached (AC-803)
      Plane: AC-803. Blocked by: gateway image gate (AC-792), licence decision (AC-870). Acceptance: the image is generated through the gateway, approved with its text, and attached to the post in Postiz.
- [ ] 6.2 Gate B: Gate A plus an approved AI-generated brand image (AC-806)
      Plane: AC-806. Blocked by: 6.1, 5.1. Acceptance: Gate A holds, and an approved AI-generated brand image is published with an approved text post. Gate: Requirement "Drafts need no approval; scheduling and publishing need a recorded approval", scenario "Image changed after approval is not published".
