## 1. Plan and decisions

Needs nothing outside this change. Both tasks are done.

- [x] 1.1 Write plan 28 and this change (AC-793)
      Plane: AC-793. Blocked by: none.
- [x] 1.2 Decision: approval policy — drafts without approval, every schedule/publish approved, approval records approver + revision (AC-795, Joseph 2026-11-12)
      Plane: AC-795. Blocked by: none. Gate: `openspec validate social-content-automation --strict` passes with Requirement "Drafts need no approval; scheduling and publishing need a recorded approval" in the spec; its scenario "Schedule refused without a recorded approval" is proven at the phase 4 gate (4.6) and at 5.1.

## 2. Brand profiles (needs the schema-validator decision)

Needs the operator's decision on a schema validator (plan 28 Open Question 7). Values for each brand live in site-config; the repository holds the schema and a placeholder example only.

- [ ] 2.1 Brand-profile schema + placeholder example in agent-cloud (AC-828)
      Plane: AC-828. Blocked by: the operator's decision on a schema validator, a new dependency (plan 28 Open Question 7). Acceptance: the schema holds the fields of plan 28 D8 and the example holds placeholders only.
- [ ] 2.2 One profile per brand of the instance, values in site-config; done when 2.3, 2.4, 2.5 are done (AC-794)
      Plane: AC-794. Blocked by: 2.3, 2.4, 2.5. Acceptance: a rollup that closes when 2.3, 2.4 and 2.5 close; no brand value appears in the repository.
- [ ] 2.3 First brand profile (Gate A critical path) (AC-829)
      Plane: AC-829. Blocked by: 2.1. Acceptance: the profile validates against the schema from 2.1 and records a source for each field.
- [ ] 2.4 Second brand profile (AC-830)
      Plane: AC-830. Blocked by: 2.1. Acceptance: as 2.3.
- [ ] 2.5 Third brand profile (AC-831)
      Plane: AC-831. Blocked by: 2.1. Acceptance: as 2.3. Gate: Requirement "Brand profiles follow the schema and hold no brand value in the repository", scenario "Profile missing a source for a field is refused".

## 3. Flows as code and connections (needs a running Activepieces: change `deploy-activepieces`)

Needs a running Activepieces instance from change `deploy-activepieces`. The local bring-up comes first; production use waits for that change's validation gate.

- [ ] 3.1 Flows as code: `Manage Activepieces Flows` playbook (AC-796)
      Plane: AC-796. Blocked by: deploy-activepieces local bring-up (AC-772) for the spike; production use after its validation gate (AC-781). First spike: R1. Acceptance: spike R1 runs first; if the Community edition does not support the flow API path, stop and return the decision to the operator (edition, AC-768). Otherwise one apply playbook writes the connections, then the flows; a dry run shows the diff, a second run reports no change, and an unowned flow is left in place. An owned connection or flow dropped from the declaration is removed. It also builds the kill switch (plan 28 D13), proven at the phase 4 gate, and the static check of Requirement "Flows never publish immediately", which the playbook runs before applying. It records the kill switch's mechanism in plan 28 D13, whether flows share the one handoff call site through a sub-flow in plan 28 D6, and whether reading a flow back returns its code step's source, which the guardrail read-back of plan 28 D10 needs.
- [ ] 3.2 Connection to agentgateway through the HTTP piece, key written from OpenBao by a no_log playbook, proven with one completion (AC-797)
      Plane: AC-797. Blocked by: 3.1; plan 27 identity (AC-785). Spike: R3. The production path needs the client leaf (AC-825) and the egress spike (AC-826), which gate 5.1. Acceptance: one completion returns through the `activepieces` identity, and no key appears in a flow definition. If the HTTP piece cannot take its key from a stored connection, choose another mechanism before this task closes.
- [ ] 3.3 Connection to Postiz through the Postiz piece (base_url and api_key upsert), key from OpenBao (AC-798)
      Plane: AC-798. Blocked by: 3.1 for the connection; a local Postiz key (`Store Postiz API Key (Local)`). The production key (AC-763) gates 5.1. Acceptance includes that the status-change call takes its key from this connection (plan 28 R2, D6). Spike, which needs only the local Postiz and its key and runs first: R2 on the pinned version (a draft with no integration; the status route; the status-change key source; reading a post back with its content, media, target integration, date and settings; deleting a superseded draft removes no other post in its group; setting a scheduled post back to draft before its publish time); if any of these is no, work stops and the decision returns to the operator. It also reports, without stopping work, whether the status change can be conditional on the post's content, whether a create can be stored after its response is lost and how long until it appears in the post list, and whether create accepts a client reference. Acceptance: the connection upserts by its external id, a second apply changes nothing, and the key is read from the `postiz_api_key` field at `secret/services/postiz`. The procedure to revoke the key is recorded (plan 28, Open Questions). Gate: Requirement "Flows and connections are declared as code and applied by playbook", scenarios "Re-run of the apply playbook converges" and "Owned connection or flow dropped from the declaration is removed".

## 4. Text pipeline (no connected social account needed)

Needs the gateway and Postiz connections from phase 3 and the brand profile from phase 2. Drafts are proven end to end before Gate A. Risk R2 (task 3.3) can stop the work before this phase.

- [ ] 4.1 Flow: generate one draft per target account from a brief, json_schema output within each platform's length limit, prompt built from the brand profile (AC-799)
      Plane: AC-799. Blocked by: 3.2, 2.3. Acceptance: one draft per target account, each reply parses against its schema, and the prompt is built from the brand profile.
- [ ] 4.2 Guardrail step (plan 28 D10) (AC-800)
      Plane: AC-800. Blocked by: 4.1 for the flow wiring; the guardrail module itself needs nothing. Acceptance: a draft over a platform's length limit, or naming an internal hostname, is returned with its reason and does not reach approval; the apply playbook reads the applied flow back and fails the run when the inlined guardrail's sha256 differs from the module's (plan 28 D10).
- [ ] 4.3 Brand-to-integration mapping and refusal (plan 28 D9) (AC-836)
      Plane: AC-836. Blocked by: 2.1. Integration ids are filled once accounts are connected. Acceptance: a target integration the mapping does not assign to the draft's brand is refused; the mapping values live in site-config.
- [ ] 4.4 Handoff flow: Postiz draft without approval; schedule only after a recorded approval, exactly once (plan 28 D2, D7) (AC-801)
      Plane: AC-801. Blocked by: 3.3, 4.2, 4.3. Spike: the approval surface (an Activepieces approval step or a form; plan 28 Open Question 1). Acceptance: the approval surface is recorded in plan 28 Open Question 1, the approval-record store in D2 and the exactly-once record store in D7; an approval counts only from a person the identity provider authenticated; a retried handoff creates no second post; a new revision replaces the superseded post only while it is a draft, and an edit to a scheduled revision is refused; the schedule hashes the post read back from the publishing service; the handoff refuses a create-post call past the hourly ceiling, and a schedule for a revision with no passing guardrail verdict for its hash (plan 28 D2); `platform/services/postiz/context/use-cases.md` is updated from schedule-at-create to draft then status change.
- [ ] 4.5 Scheduled generation: a Schedule-trigger flow on a declared cadence from a topic backlog held in site-config; drafts still pass guardrail and approval (AC-802)
      Plane: AC-802. Blocked by: 4.4. Acceptance: scheduled runs share the handoff's create-post budget (plan 28 R6), and every generated draft passes 4.2 and then 4.4.
- [ ] 4.6 Failure alerting for content flows through the o11y alert path (AC-804)
      Plane: AC-804. Blocked by: 3.1, 4.2, 4.4, 4.5 (it carries the phase gate). Acceptance: the alert producer is chosen and recorded in plan 28 D11; a forced failing run raises an alert on the observability alert route. Gate: Requirement "Drafts need no approval; scheduling and publishing need a recorded approval", scenarios "Schedule refused without a recorded approval", "Publish time changed after approval is not scheduled", "Post edited in the publishing service after approval is not scheduled" and "Edit to a scheduled revision is refused"; Requirement "Guardrail checks run before approval", scenarios "Draft naming an internal hostname is refused", "Schedule refused without a passing guardrail verdict" and "Applied guardrail differs from the module"; Requirement "A kill switch stops content flows", scenarios "Thrown kill switch stops drafting and scheduling" and "Released kill switch resumes flows"; Requirement "Flows never publish immediately", scenario "Static check over the committed flow definitions"; Requirement "Each draft targets exactly one mapped account of its brand", scenario "Target integration not mapped to the brand is refused"; Requirement "Generation reaches the model only through the inference gateway under its own identity", scenario "Generation calls the gateway under the activepieces identity"; Requirement "Handoff to the publishing service is exactly once", scenarios "Retried handoff creates no second post" and "Create past the hourly budget is refused"; Requirement "A failed content flow run raises an alert", scenario "Failed run raises an alert".

## 5. Gate A (needs connected X and LinkedIn accounts: operator)

Needs the X and LinkedIn developer apps and their connected accounts, which the operator provides, and the operator's decisions on plan 28 Open Questions 8 and 9. Gate A is the first live publication, and it targets the first brand only.

- [ ] 5.1 Gate A: an AI-drafted, human-approved text post published to the first brand's X and LinkedIn through Postiz in production; a rerun of the handoff does not double-post (AC-805)
      Plane: AC-805. Blocked by: 4.2, 4.4; the schedule step enabled (plan 28 D2: a conditional status change shown in 3.3, or the window accepted under Open Question 9); the operator's decision on plan 28 Open Question 8 (a member scheduling a generated draft by hand); gateway text gate (AC-791); Activepieces gate (AC-781); X and LinkedIn apps and connected accounts (AC-760, AC-761; Postiz change tasks 6.5, 7.1, 7.7); production Postiz key (AC-763); gateway client leaf (AC-825) and egress spike (AC-826). Acceptance: one approved draft per target account is published on X and on LinkedIn, and a rerun of the handoff adds no post. Gate: Requirement "Drafts need no approval; scheduling and publishing need a recorded approval", scenarios "Schedule refused without a recorded approval" and "Approval of one revision does not schedule the next revision"; Requirement "Handoff to the publishing service is exactly once", scenario "Retried handoff creates no second post".

## 6. Images (blocked on plan 27's image path)

Blocked on the image path in plan 27 (AC-792), which waits on the dgx-spark fit test of the model chosen by the AC-870 decision (2026-11-16, recorded in plan 27). No task in this phase starts before that gate passes.

- [ ] 6.1 Image step: brand image through the gateway's image route, approved with the text, uploaded to Postiz media and attached (AC-803)
      Plane: AC-803. Blocked by: gateway image gate (AC-792), 4.4. Acceptance: the image is generated through the gateway, approved with its text, and attached to the post in Postiz.
- [ ] 6.2 Gate B: Gate A plus an approved AI-generated brand image (AC-806)
      Plane: AC-806. Blocked by: 6.1, 5.1. Acceptance: Gate A holds, and an approved AI-generated brand image is published with an approved text post. Gate: Requirement "Drafts need no approval; scheduling and publishing need a recorded approval", scenario "Image changed after approval is not published".
