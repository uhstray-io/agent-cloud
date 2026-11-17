# Tasks: agentgateway content generation

Every task is safe to rerun. Nothing here runs against production until the operator asks for that action. Deploys go through the Semaphore templates, never one-off writes. Plan 27 holds the decisions and their sources.

## 1. Plan and decisions
- [x] 1.1 Write plan 27 and this OpenSpec change (AC-782)
- [x] 1.2 Decide how Activepieces reaches the gateway: internal listener with a step-ca client leaf (AC-783)
      Phase gate: `openspec validate agentgateway-content-generation --strict` passes, and the sync parser lists the ten open tasks of this change. This phase is planning, so no runtime spec scenario applies.

## 2. Text path
- [ ] 2.1 Spike: can an Activepieces piece present a client certificate; if not, add an mTLS egress sidecar (AC-826)
      Acceptance: a recorded yes or no for the piece tested. On no, add the sidecar to the Activepieces compose project, on an internal network only.
- [ ] 2.2 Issue the Activepieces host a client leaf, add its leaf name to agw_client_cert_allowlist, give it a Renew Internal Certs proof path (AC-825)
      Acceptance: Renew Internal Certs proves the leaf (scenario "Allowlisted leaf proven at renewal"). The allowlist takes leaf names, not SANs (deploy-agentgateway.yml:294-305).
- [ ] 2.3 Enroll the activepieces identity with its budget and model allow-list (AC-785)
      Acceptance: the rendered configuration gives it the tokens_per_hour value from site-config and a text-only allowed_models (scenario "Content identity budget and allow-list"). The deploy's keyed verify does not run as it (scenario "Content identity is not the verify identity").
- [ ] 2.4 Validation gate (text): chat and json_schema completions through the gateway from the Activepieces host; keyless call refused (AC-791)
      Proven by the spec scenarios "Content identity budget and allow-list", "Keyless request with an admitted certificate" and "Request without a client certificate".

## 3. Image path (blocked: AC-870 and the dgx-spark image serving)
- [ ] 3.1 Verify the pinned gateway proxies the OpenAI images API; upgrade if not; check per-key request limits in the same release (AC-786)
      Acceptance: a recorded result for the release tested, from exercising the route. Any pin change goes through compose.yml:36 and a deploy.
- [ ] 3.2 Add the image upstream and route by model name (AC-787)
      Acceptance: a template branch, a validator change, a new existing secret seeded with Seed OpenBao Key and never generated, and tests in the pattern of the agentgateway-openrouter change. The image model joins the activepieces allow-list.
- [ ] 3.3 Choose and encode a per-identity request limit for images (AC-788)
      Acceptance: the chosen option is recorded in plan 27, decision D6. A drill in the style of Probe agentgateway Isolation (Dev) shows that a request past the cap is refused for that identity while another identity is still served (scenario "Request past the image cap").
- [ ] 3.4 Conformance: an images case, gateway vs direct image upstream (AC-789)
      Acceptance: run-agw-conformance.yml gains a second direct URL for the image upstream (it has one today, at line 407), and the images case passes against both sides.
- [ ] 3.5 Dashboards: image latency, errors, per-identity volume (AC-790)
      Acceptance: Verify o11y Dashboard Data (Dev) passes for the new panels.
- [ ] 3.6 Validation gate (image): render through the gateway in production; the cap holds (AC-792)
      Proven by the spec scenario "Request past the image cap".
