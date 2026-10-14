# Tasks: MCP on the agentgateway, per-admin identities, and harness clients

Every task is safe to rerun after an interruption: detect the state it would produce and
skip or converge. Deploys go through the idempotent playbooks and the Semaphore templates
(`(Dev)` variants for production), never one-off writes. Nothing here is pushed, opened as
a pull request or run against production until the operator says so for that action.

## 0. Entry gates
- [ ] 0.1 Implementation branch `feat/agentgateway-mcp` from `origin/dev` in its own
      worktree (this plan lives on `plan/agentgateway-mcp`); one worktree per work item
- [ ] 0.2 Read the companion changes' state before touching shared files:
      `openspec list --store agent-cloud` for `inference-gateway-agentgateway` and
      `inference-personal-keys`; both edit `config.yaml.j2`'s key rendering, so record
      which task numbers are open and rebase on whatever has landed
- [ ] 0.3 Confirm the local gateway deploys cleanly from `origin/dev` before any change
      (`make local-deploy-agentgateway`, then its verify): the spike and every later local
      proof assume a working baseline
- [ ] 0.4 Operator confirmation still outstanding in `design.md`: the small decisions G1 to
      G6; Open Question 7 (the reading of D2(a) as "the served `/mcp` endpoint"; the spike
      showed a target pointing back at `/mcp` recurses, so the "nothing points at `/mcp`"
      branch applies); Open Question 8 (the loopback self-call restricted by a
      `source.address` rule, because v1.5.0 has no gateway bind address). Open Questions 1, 3
      and 6 were answered 2026-10-14 and are recorded under each. Any answer that
      contradicts a decision reopens it before code
- [ ] 0.5 Validation gate: `openspec validate agentgateway-mcp --strict` passes, and every
      scenario title quoted in the gates below exists verbatim in
      `specs/platform/agentgateway-mcp/spec.md`

## 1. Local spike: the facts the schema cannot give
Scratch configuration only, in the scratchpad, never committed. Each answer is a dated
paragraph under "Findings" in `design.md` with the command and the observed output; a
finding that contradicts a decision stops the work and reopens that decision.
- [x] 1.1 `llm:` and `mcp:` in one file: add an `mcp:` block (`gateways: [default]`, one
      `openapi` target) to a copy of the local rendered config; run the pinned image's
      `--validate-only` through `tasks/agw-validate-config.yml`; start it and confirm `/v1`
      still serves. Also feed the validator a deliberately wrong `mcp:` block and record
      whether it refuses (G5 depends on it). Done 2026-10-14 against a hand-written scratch
      config in the rendered shape, not a render of `config.yaml.j2`
- [x] 1.2 Authentication on `/mcp`: `initialize` with an enrolled key returns a session;
      no key, an unknown key, and a key left out of the `mcp` list each return 401; record
      whether `initialize` needs a healthy target (`failureMode`)
- [x] 1.3 `apiKey.name` in an `mcpAuthorization` CEL rule: write a rule permitting one tool
      to one identity only and record whether it is evaluated
- [x] 1.4 Budgets and `mcp.policies.apiKey`: record whether `budgets` entries under the MCP
      key policy are accepted and charged (G2)
- [x] 1.5 The self-call: an `openapi` target whose host is the gateway's own `/v1`, with
      `backendAuth` carrying a key from an environment reference, once with the local
      plain listener and once with `agw_listener_tls` on. Record what the mutual-TLS
      listener requires of the gateway's own call (client certificate, SAN allowlist) and
      what widening, if any, that needs; record whether a target pointing back at `/mcp`
      terminates or recurses, bounded by a timeout. A widening of the SAN allowlist is
      reported to the operator, not decided here
- [x] 1.6 `statefulMode`: run `stateless` and `stateful`; record the session header
      behaviour, whether a server-initiated stream is opened, whether the transport needs any
      path or query beyond exact `/mcp` (the Caddy matcher is exact, G1), the exact request
      headers an `initialize` needs (for example `Accept`) and whether the response is a
      single JSON body or an event stream (task 3.4's probe depends on it), and how long
      an idle stream
      survives locally (the 125 s Cloudflare timeout is measured in production, 6.6)
- [x] 1.7 `mcp.policies.localRateLimit`: record whether an MCP request bucket renders and
      limits (G6), and the request burst of a real `initialize` + `initialized` +
      `tools/list` handshake (feeds task 5.4's bucket). Done 2026-10-14 with a scripted
      client; the real Claude Code and pi handshake burst is measured in 7.7
- [x] 1.8 `/config_dump` redaction: with a canary value (never a real secret) in an
      environment-referenced field, read the dump on the container loopback and record
      whether the canary appears. Record only; D6 holds either way
- [x] 1.9 Write the findings into `design.md`; for each decision a finding contradicts,
      stop and report instead of editing the decision
- [x] 1.10 Validation gate, local: with the scratch config, an `initialize` with no key is
      401 and reaches no target, proving scenario "Unauthenticated MCP request is
      refused"; `initialize` then `tools/list` with an enrolled key returns the target's
      tools, proving scenario "An enrolled identity lists tools"

## 2. Identities from the admin list
- [x] 2.1 Move every fixed-block member of `platform-admins` into the declared list
      (`platform-users.yaml:39,74`, `stray-admin.yaml:25`, `agent-cloud-admin.yaml:35`,
      `service-account.yaml:22`), the break-glass account and the service account included:
      the operator's answer to Open Question 1 (2026-10-14). The declared exceptions list
      stays as the drift guard's mechanism and is empty by default; `openbao-oidc.yaml:44`
      only maps the group. No fixed block stays outside the list
- [x] 2.2 Define the declared list's shape (proposed variable `platform_admins`; each entry
      the Authentik username, display name, email and an optional `agw_identity`) in the
      local inventory example and the Authentik README; real values stay in site-config
- [x] 2.3 Render the Authentik admin entries from the list: a templated blueprint, the
      per-entry password secret definitions looped in `deploy-authentik.yml`, the
      environment lines looped in the Authentik `env.j2`. The existing OpenBao field names
      are kept so no password changes
- [x] 2.4 `deploy-agentgateway.yml`: derive one identity per list entry (explicit
      `agw_identity`, else the username), merge with `agw_clients` into `agw_identities`;
      refuse a duplicate within the list, a name outside `^[a-z0-9][a-z0-9-]*$`,
      `legacy-shared` and `mcp-self`, each before any restart, naming the entry. A name in
      both the list and `agw_clients` is merged once and reported by name as an overlap
      (the same `client_<name>` field, so nothing is minted twice); this is what allows the
      derivation to be deployed before the site-config removal (task 6.8)
- [x] 2.5 Point every reader at the merged list: the `_client_defs` loop in
      `vars/secret-declarations/agentgateway.yml`, the name assert, the `llm` key loop in
      `config.yaml.j2`, the verify's default identity, `manage-agentgateway-client-key.yml`
      (rotate needs the name in the merged list; revoke needs it removed from it)
      The derivation is gated by `agw_admin_identities_enabled` (default false) so the next
      production gateway deploy mints nothing before the section 6 rollout.
      The verify's default identity is `agw_verify_client`, else the first merged identity
      (`agw_clients` first, so unchanged while any is declared). `rollback-inference-route.yml` and
      `run-agw-conformance.yml` read the merged list too (their default client is unchanged);
      `deploy-o11y.yml` keeps `agw_clients` on purpose: the probe is a service identity
- [ ] 2.6 Equivalence proof: render the Authentik admin entries both ways for the same
      inputs and diff them (username, name, email, active, path, groups, password field);
      run the local Authentik deploy twice, the second reporting nothing changed and the
      read-back verify passing. Render equivalence done (the old fixed blocks at 724528c7
      against the template, for the placeholder defaults and a synthetic per-user override
      set; `platform/tests/test_authentik_platform_admins.py` holds the golden). The twice-run
      local deploy is PENDING: the local Semaphore mounts another checkout, so it runs only
      after this merges
- [ ] 2.7 Local migration: declare each person identity in the list under its existing
      name, first with the name still in `agw_clients` (overlap reported) and then with it
      removed. Prove no key is re-minted and the rendered key-hash count does not drop by
      comparing each identity's `keyHash` before and after each step (the hash line, never
      the key)
      Render level done (`platform/tests/test_agw_admin_identities.py`: the identical `keyHash`
      line per identity through overlap and removal, count never drops); the run against a
      local gateway is not
- [x] 2.8 BATS: derivation, override name, in-list duplicate refusal, overlap merged once
      and reported, charset refusal, `legacy-shared` and `mcp-self` refusals, revoke needing
      removal from the merged list.
      Mutation-check each refusal: break it, watch its test fail, restore it byte-exact
      Done as pytest in `platform/tests/test_agw_admin_identities.py` (the repo's harness
      runs the real guard tasks)
- [ ] 2.9 The drift guard (D3), which is the Authentik verify alone: extend the read-back
      verify (`verify-users.py.j2`, run inside the server container) to assert the members
      of `platform-admins` equal the declared admin list plus the declared exceptions list,
      failing with the account name only. The gateway deploy gets a consistency check of its
      own render instead (the name set and `keyHash` count rendered equal the merged
      identity list), not a second membership guard: "derived equals declared" is true by
      construction. BATS for both; mutation-check the verify by adding a member outside both
      lists and by dropping a list entry, and the render check by dropping one rendered key,
      watching each fail. The Authentik half (verify + BATS + mutations) is done. The gateway
      half is done too (the render check in `deploy-agentgateway.yml`, pytest in
      `platform/tests/test_agw_admin_identities.py`, mutation-checked)
- [ ] 2.10 Validation gate, local: the rendered admin entries equal the fixed blocks and the
      verify passes, proving scenario "The rendered admin accounts match the fixed blocks";
      a new list entry mints a `client_<name>` and its hash is enrolled on the next
      deploy, proving scenario "A declared admin gets an identity on the next deploy"; a duplicate
      or reserved name fails before any restart, proving scenario "A name
      collision is refused"; a migrated name keeps its hash through both the overlap and
      the removal, proving scenario "An existing identity keeps its key"; a hand-added
      `platform-admins` member fails the Authentik verify, proving scenario "Admin
      membership and the declared list agree"; a removed entry's key is 401, proving scenario
      "Removing an admin
      ends their access"

## 3. Gateway MCP configuration and verify
- [x] 3.1 One shared key macro in `config.yaml.j2` taking the identity list and whether to
      include the legacy block and budgets (G2), used by `llm.policies.apiKey` and
      `mcp.policies.apiKey`. The `llm` output for the same inputs is byte-identical before
      and after (diff the rendered block): no fork
      Done 2026-10-14 (branch `feat/agw-mcp-gateway`): the macro is `apikey_policy(identities, legacy, limits)`
      in `config.yaml.j2`; `llm` calls it with all three, `mcp` with none. The `llm` block is byte-identical for the
      same inputs: rendered with the pre-change template and with this one for a prod-like input (listener TLS,
      shared-key grace period, OTLP, per-identity policies), the local plaintext example and a local TLS UI-off
      input, config.yaml and .env each diffed empty. One deliberate difference, with `mcp-self` under the local
      plaintext flag (4.2). BATS "the key loop is written once, in the shared macro"; mutation (a second key loop)
      red.
- [x] 3.2 The `mcp:` block behind `agw_mcp_enabled` (default false): `gateways: [default]`,
      `policies.apiKey` from the macro without `legacy-shared` and without `mcp-self`
      (the target's `backendAuth` identity must never authenticate a client), `prefixMode` and
      `statefulMode: stateless` (chosen 2026-10-14 from spike 1.6: no session header, no
      server-initiated stream so nothing is held against the 125 s timeout, `GET /mcp` is 405,
      and the stateful session id is neither revocable nor bound to an identity), targets
      rendered only from the structured declaration of 3.8 (never a free-form list),
      `dnsRebindingProtection` left unset (the gateway is reached by a non-local Host).
      The render asserts `mcp.policies.apiKey` is present whenever `agw_mcp_enabled` (without
      it `/mcp` answers every request, spike 1.1) and renders NO `budgets` on the MCP key
      entries (accepted but never charged or enforced, spike 1.4: a silent no-op that reads
      as a control). With the flag true and no target the deploy refuses, because the schema
      requires one
      Done 2026-10-14. Block, key policy, `prefixMode` and `statefulMode: stateless`, targets only from
      `agw_mcp_openapi_targets`, `dnsRebindingProtection` unset, no budgets and no `allowedModels` on the MCP key
      entries, no `legacy-shared`, no `mcp-self`. The spec gives no `prefixMode` value: `conditional` (the schema
      default, pinned) is rendered, so a tool is its `operationId` while there is one target and is prefixed by the
      target name once there are several; confirm or change. No `mcp.policies.localRateLimit` is rendered (G6, task
      5.4: not in this task's list). The deploy asserts the render: the flag false renders no `mcp` block, any
      `mcp` block carries a strict key policy, and exactly the client identities are enrolled on it; flag true with
      no target is refused (3.8). Tests: `test_agw_mcp_render.py` (flag false/true, key policy, key list, no budget,
      llm unchanged) and `test_agw_mcp_guard.py` (render check). Mutations red: drop the policy, add a budget,
      restore the legacy entry, admit `mcp-self`, `stateful`, render check blind to a missing policy.
      Amended 2026-10-14 (security review, confused deputy): the MCP key list is the admin-derived personal
      identities ONLY (`_agw_admin_identities`), never an `agw_clients` entry, because the chat tool's self-call runs
      as `mcp-self` and a service client admitted on `/mcp` would escape its own `allowedModels` and hourly budget.
      The deploy refuses MCP with no admin-derived identity (naming `agw_admin_identities_enabled` and
      `platform_admins`), with an `allowed_models` on an admin identity (naming it), or without
      `agw_client_policies.mcp-self.tokens_per_hour`; the verify probes as an admin identity. Tests and
      mutation: see the `fix(agentgateway): admit only personal admin identities on /mcp` commit.
- [x] 3.3 Run the MCP-enabled rendering through `tasks/agw-validate-config.yml` before any
      container is recreated (G5), with the same allowlisted-category failure the existing
      validation gives
      Done 2026-10-14 by construction: the one `tasks/agw-validate-config.yml` include in Phase 1 follows the render
      and runs for every render, so a flag-true render is validated by the pinned image before Phase 2 recreates
      anything, with the existing allowlisted-category failure; BATS "the MCP-enabled render is validated by the pinned
      image before any container is recreated" pins the order and that it is unconditional. The task file is
      unchanged (the schema is embedded, so the validator needs no mount). Live: `--validate-only` accepts the
      flag-true render (UI off; UI on cannot be validated offline, it fetches the OIDC discovery document) and the
      TLS-on prod-like render with dummy certificates; a flag-true config with `AGW_MCP_SELF_KEY` absent from the env
      is refused ("environment variable not found").
- [x] 3.4 Extend `tasks/agw-probe.yml` and the verify in `deploy-agentgateway.yml`: with MCP
      enabled, `/mcp` without a key must be 401 and an authenticated `initialize` must
      succeed; with MCP disabled, `/mcp` must not be served. The probe gains an optional
      `_agwp_headers` input (merged into the request, for example `Accept`) and a session-id
      carry for a follow-up call; the exact headers and whether the reply is an event stream
      are fixed by spike item 1.6 (both `Accept` values required; the reply is a one-frame
      event stream, read from its `data:` line). The verify proves an authorised tool is
      LISTED for an enrolled identity, not just that `/mcp` answers, because the validator
      accepts an `mcpAuthorization` rule naming a wrong field, which then denies every tool
      silently (spike 1.1, 1.3). Every probe reads the BODY, not the HTTP status: a self-call
      401 or 403 comes back as HTTP 200 with the error in the tool result (spike 1.5), and a
      rate-limit refusal is HTTP 200 with JSON-RPC error `-32003` (spike 1.7). The request is a `uri` call from the verify step (headers
      in the request, never in any process's argument list). Per AGENTS.md, "a visible task
      never loops over, or prints, a protected registered result" (`docs/MISTAKES.md` 4.6):
      a `uri` result carries its request headers, so the probe keeps copying fields under
      `no_log` as it does today, and `platform/tests/test_no_request_in_loop_items.py` must
      still pass. `no_log` stays on the key-bearing step only. A check that cannot run
      reports itself unproven, never passed
      Done 2026-10-14 in `deploy-agentgateway.yml` Phase 3 and `tasks/agw-probe.yml`. Flag true: keyless `/mcp`
      is 401 (a fail names the check and prints no key), an authenticated `initialize` returns a session, `tools/list`
      lists at least one tool; flag false: a keyed `initialize` is not an MCP reply (status and body). The probe
      gains `_agwp_headers` (Authorization merged last) and an opt-in `_agwp_rpc` that reads the `data:` line of the
      event stream else a JSON body into `_agwp_out.rpc`; both `Accept` values are sent. Verdicts read the body;
      `-32003` and the gateway's own 429 are unproven and fail the deploy unless `agw_verify_allow_unproven`, and are
      reflected in the result's `verified`. The identity is never `mcp-self`. No new `no_log` task, no loop over a
      protected result; `test_no_request_in_loop_items.py` passes. Not done: the session-id carry (stateless returns
      none, spike 1.6, so it could not be exercised); the model call through `/mcp` is 4.6. Tests:
      `test_agw_mcp_verify.py` runs the real tasks against a fake gateway; updated BATS counts (keyed probe tasks
      2 to 5, `_agwp_out` keys, rate-limit literal 2 to 4). Mutations red: keyless assert, tool-listed assert,
      unproven gate, initialize status, not-served status and body, `data:` parse, caller headers, mcp-self identity,
      `verified`.
- [x] 3.5 Inventory: `agw_mcp_enabled`, `agw_mcp_hostname`, the structured target
      declaration (proposed `agw_mcp_openapi_targets`: a list of entries, each a target name
      and a schema file name resolved under the shipped schema directory; the host is not an
      input) and `agw_loopback_port` (proposed, default 4002) in
      `platform/inventory/local-dev.yml.example`, with a comment that the first target is
      task 4.3's
      Done 2026-10-14 in `platform/inventory/local-dev.yml.example`: `agw_mcp_enabled` (false), `agw_mcp_hostname`
      (consumed by the Caddy route of section 5, not by the gateway deploy), `agw_loopback_port` (4002),
      `agw_mcp_openapi_targets` (`gateway` -> `agentgateway-v1.json`, commented as task 4.3's), and `mcp-self` with
      its `agw_client_policies` budget (4.2).
- [x] 3.6 BATS: flag false renders no `mcp` block; flag true renders `gateways: [default]`
      only, no legacy entry, the same identity list as `llm` minus `legacy-shared` and `mcp-self`;
      `mcp.policies.apiKey` present whenever the flag is true; no `budgets` under the MCP key
      entries; `statefulMode: stateless`; the macro is the only place the key loop is
      written. Mutation-check each (drop the policy, add a budget, restore the legacy entry,
      write a second key loop: each goes red)
      Done 2026-10-14 as pytest on the rendered YAML (`test_agw_mcp_render.py`, the harness 2.8 also uses) plus the
      BATS macro test. Mutations red, each restored byte-exact (`git diff --exit-code` clean): drop the policy,
      add a budget, restore the legacy entry, admit `mcp-self`, `stateful`, a second key loop.
- [x] 3.7 The loopback gateway, so the self-call needs no mutual-TLS leaf and no widening of
      `agw_client_cert_allowlist` (the operator's decision 2026-10-14, Open Question 8): a
      second gateway `loopback` on `agw_loopback_port`, `llm.gateways: [default, loopback]`
      (attached to `llm` only, never `mcp`, never `ui`), carrying `authorization.rules:
      - require: 'source.address == "127.0.0.1"'` because v1.5.0 gateways have no bind
      address and bind every container interface (spike 1.5: without the rule a neighbour
      container reaches it, with it that container is 403). The port is never added to the
      published ports of `compose.yml`, `compose.local.yml` or `compose.tls.yml`. The loopback
      gateway and the `loopback` entry in `llm.gateways` render ONLY when `agw_mcp_enabled` is
      true (G4): with the flag false the rendered file is unchanged and `llm.gateways` stays as
      it is today
      Done 2026-10-14: `gateways.loopback` on `agw_loopback_port` (default 4002, refused if it is 4000, 4001,
      15000, 19001, 19002 or outside 1024-65535), the `source.address` rule, `llm.gateways` gaining `loopback`
      (`[default, ui, loopback]`, or `[default, loopback]` with the UI off) only with the flag; the flag false render
      is unchanged. Live on a scratch v1.5.0 gateway rendered from the template (names only): no key, an unknown key
      and `mcp-self` are 401 on `/mcp`, an enrolled identity is 200; `mcp-self` is 200 on `/v1`; a request to the
      loopback port from a neighbour container is 403 and from the gateway's own namespace 200; a chat tool call
      through `/mcp` is logged on the loopback listener as `identity="mcp-self"`, `/v1/chat/completions`, and on `/mcp`
      as the caller with `mcp.target=gateway`.
- [x] 3.8 The deploy-time target allowlist, refusing before any render or restart, naming
      the entry: only `openapi` targets are rendered, from the shipped schema; the rendered
      host is the declared loopback address and port and nothing else; an `mcp` or `sse`
      target, a schema outside the shipped directory, or any other host is refused (the
      validator accepts every one of these, and a target aimed at the gateway's own listener
      recurses without bound, spike 1.5). An allowlist and not a denylist, because a string
      check cannot enumerate every alias of the external listener
      Done 2026-10-14 as an allowlist in `_agw_mcp_problems` (`vars/secret-declarations/agentgateway.yml`),
      asserted in Phase 1 before the monorepo is placed and before anything renders: a target is exactly `name` and
      `schema`, so an `mcp`, `sse` or `stdio` kind, a `host` or a `port` cannot be declared; the schema is a bare
      `*.json` file name (no path), read from the shipped `mcp-schemas/` directory; the host is not an input, the
      template renders `127.0.0.1:<agw_loopback_port>`. Each refusal names the entry. The schema is embedded in
      `config.yaml` as one compact JSON string rather than mounted, so no compose file changes and a schema edit
      is a config change the change-aware deploy sees.
- [x] 3.9 BATS for 3.7 and 3.8, on the rendered config and the compose files, each
      mutation-checked (break it, watch its test fail, restore byte-exact): drop the
      `source.address` rule; attach `loopback` to `mcp` or to `ui`; publish the loopback
      port in any compose file; render the loopback gateway or the `loopback` entry in
      `llm.gateways` with the flag false (the flag-false render must show no loopback gateway
      and the unchanged `llm.gateways`); render an `mcp` or `sse` target; render a host other than the
      loopback address and port; a schema path outside the shipped directory
      Done 2026-10-14: pytest on the rendered config (`test_agw_mcp_render.py`) and the guard
      (`test_agw_mcp_guard.py`), BATS on the compose files ("the MCP loopback port is published by no compose
      file"). Mutations red, each restored byte-exact: drop the `source.address` rule, attach `loopback` to `mcp`,
      to `ui`, publish the port in `compose.yml`, loopback gateway and `llm.gateways` entry rendered with the flag
      false, an `mcp`-kind target, a host other than the loopback listener, a schema path outside the shipped
      directory, an `mcp`/`sse`/`host` key accepted on a target.
- [ ] 3.10 Validation gate, local (uses task 4.1's schema file as the target): an `initialize`
      with no key is 401, proving scenario "Unauthenticated MCP request is refused"; an
      unknown key is 401, proving scenario "An unknown key is refused"; the legacy shared
      key is 401 on `/mcp` and still served on `/v1`, proving scenario "The legacy shared
      key is not admitted"; the `mcp-self` key is 401 on `/mcp` and honoured on `/v1`,
      proving scenario "The internal identity is refused on the MCP endpoint"; the flag false
      removes the block, proving scenario "Disabling
      MCP removes the endpoint"; a break-glass edit that makes `/mcp` answer without a key
      fails the deploy, proving scenario "Deploy fails when /mcp answers without a key"; a
      normal deploy records the authenticated session, proving scenario "Deploy proves an
      authenticated initialize"; a target aimed at the gateway's own external
      listener is refused or cannot connect, proving scenario
      "A target cannot reach the gateway's own external listener"; a request to the loopback
      listener from another container is refused, proving scenario
      "The loopback listener refuses other containers"; the 3.9 BATS (loopback rule, attachment, flag scope, unpublished port,
      target allowlist) pass and each mutation fails
      Evidence 2026-10-14, task output read from the local Semaphore by the lead (NOT ticked: the legacy-key leg is
      not proven live). Local deploy 6583 (flag on, admin gate on, placeholder admins): verify "/mcp without a key is
      401; as stray initialize returned a session and tools/list listed list_models, chat_completions". Direct
      probes against the local gateway: no key 401, unknown key 401, `mcp-self` 401 on `/mcp` and 200 on `/v1`, the
      service client `dev-local` 401 on `/mcp` (admin-only admission). Local 6584 (flag off): "MCP is disabled: a
      keyed initialize at /mcp is not an MCP reply (HTTP 400)". From a neighbour container on the gateway network:
      loopback port 4002 is 403. Dry run 6582 failed (the schema was read on the target, which `--check` does not
      place), fixed by `03e9d32f`; 6585 passed with the schema absent on the target. The legacy shared key on `/mcp`
      is NOT proven live (local has no grace period): render tests only, the production rollout proves it. The
      no-key-policy failure ("Deploy fails when /mcp answers without a key") is proven on a scratch gateway only.
## 4. The OpenAPI target over `/v1`
- [x] 4.1 The OpenAPI schema file in the repository (proposed under
      `platform/services/agentgateway/deployment/`): chat completions and model listing
      only; `responses` deferred. Parsed in a BATS test
      Done 2026-10-14: `platform/services/agentgateway/deployment/mcp-schemas/agentgateway-v1.json` (OpenAPI 3.0.3,
      `list_models` and `chat_completions`, no `servers`). Parsed in `test_agw_mcp_render.py`; the gateway accepted it
      and listed both tools live.
- [x] 4.2 The internal identity: declare `mcp-self` in the local inventory's `agw_clients`
      with its own `agw_client_policies.mcp-self.tokens_per_hour`; reserve the name so a
      person cannot be declared with it; the key flows through the existing
      `client_<name>` declaration
      Done 2026-10-14: `mcp-self` and `agw_client_policies.mcp-self.tokens_per_hour` in the local example. The
      reservation already existed (`_agw_identity_problems` refuses `mcp-self` for a derived admin identity); a test
      pins it (`test_a_person_cannot_be_declared_as_the_internal_identity`, mutation red). Beyond the task: the key
      loop renders `mcp-self` as a hash even under the local `agw_plaintext_keys` flag, since a raw key would reach
      the UI playground as a saved key; the one input that differs from before is `mcp-self` with that flag, which no
      earlier inventory had.
- [x] 4.3 The target, declared in the structured form of 3.5 and rendered by 3.8: `openapi`,
      the shipped schema, host the loopback listener of 3.7, `backendAuth` the shared
      `mcp-self` key from `$AGW_MCP_SELF_KEY`; no client certificate, no mutual-TLS leaf for
      the gateway and no change to `agw_client_cert_allowlist` (spike 1.5, Open Question 8)
      Done 2026-10-14: the target is `agw_mcp_openapi_targets` entry `gateway` (3.5), rendered by 3.8 as `openapi`,
      the shipped schema, host the loopback listener, `backendAuth` the `$AGW_MCP_SELF_KEY` reference; no client
      certificate, no mutual-TLS leaf, `agw_client_cert_allowlist` untouched.
- [x] 4.4 `env.j2`: `AGW_MCP_SELF_KEY` from the secret, in the 0600 environment file with
      the other keys; the rendered config carries the reference only
      Done 2026-10-14: `AGW_MCP_SELF_KEY={{ secrets['client_mcp-self'] }}` in `env.j2` (0600), only with the flag;
      the rendered config carries the reference only.
- [x] 4.5 BATS: the key's value appears in no rendered file and no committed template, the
      reference is `$AGW_MCP_SELF_KEY`, the schema file lists exactly the intended
      operations, no target addresses the admin port or `/config_dump`. Mutation-check
      Done 2026-10-14 in `test_agw_mcp_render.py`: the key value is in the env file once and in no rendered
      config, under the hashed and the local plaintext inputs; the templates, compose files and schema name it
      only by `$AGW_MCP_SELF_KEY` / `secrets['client_mcp-self']`; the schema lists exactly the two operations and
      no target or schema addresses the admin port or `/config_dump`. Mutations red: the value as the
      `backendAuth` value, the plaintext exemption removed, a `/config_dump` operation in the schema, the env line
      dropped.
- [x] 4.6 Extend the verify: `tools/list` shows the chat tool; one model call through
      `/mcp` returns a completion; the access log line for the resulting `/v1` request
      carries `mcp-self` as `identity` (names only in the report)
      Done 2026-10-14 (`4b396795`, log read reworked in `deeec981`): after `tools/list` the verify requires the chat tool to be listed (an
      `operationId` ending in `chat_completions`), makes one `tools/call` as the admin probe identity (`max_tokens` 16,
      the first declared model) and requires no JSON-RPC error, `isError` false and a completion in
      `structuredContent.choices`; the gateway's rate-limit refusal (`-32003`, or its own 429) is unproven, as for the
      other MCP probes. It then counts, on the host in one shell task (awk, colour codes stripped, `pipefail`, no
      `no_log`), the access-log lines since the call (engine clock taken before it) for `/v1/chat/completions` on the
      loopback listener as `identity="mcp-self"` and requires at least one; the registered stdout is that number, so no
      log line is printed. Tests: `test_agw_mcp_verify.py` (fake gateway and a stand-in
      engine, plain and colour-coded log lines). Mutations red: log check dropped, completion check, `isError` check,
      chat-tool check, log match ignoring the identity, colour stripping removed, the count task printing the raw
      line, rate-limited call not unproven. Lead manual run, local: a `tools/call` as `stray` returned a
      completion with `choices`; the access log showed `identity="mcp-self"`, `listener=loopback`, path
      `/v1/chat/completions`.
- [x] 4.7 With an enrolled key list every tool and call each whose name or description
      mentions configuration, admin or dump; from another container confirm the admin port
      is refused
      Done 2026-10-14, read by the lead from the local gateway: only two tools exist (`list_models`,
      `chat_completions`), none mentions configuration, admin or dump; the admin port 15000 from a neighbour
      container is connection refused.
- [x] 4.8 Validation gate, local: `tools/list` returns the chat and model-listing tools,
      proving scenario "An enrolled identity lists tools"; a model request through MCP is
      attributed to `mcp-self`, proving scenario "A model request through MCP is charged
      to the internal identity"; the key value is absent from the rendered config, the
      committed templates and the docs, proving scenario "The internal identity is never
      handed out"; no tool returns configuration and the admin port is refused, proving
      scenario "No tool exposes the configuration dump"
      2026-10-14 evening (task output read from the LOCAL Semaphore by the lead, task 6601;
      no production run): local deploy of the local Semaphore's repository state
      at local merge `3a586151`, which contains `feat/agw-mcp-gateway` at `a2b23a42`, with
      the flag on. Later commits on that branch (the merge of `dev` and the review fixes) are
      not exercised by this run. Its verify: "MCP: /mcp without a key is 401; as stray initialize returned a
      session, tools/list listed list_models, chat_completions, and one chat call returned a
      completion logged as mcp-self on the loopback listener." Scenario evidence: tools listed
      (yes); a model request through MCP is charged to `mcp-self` (log identity `mcp-self`,
      loopback listener); the key value is absent from the rendered config (only the
      `mcp-self` key hash; the value sits in the 0600 `.env`) and from committed files (tests
      in PR #634); no tool returns configuration (only two tools exist) and the admin port is
      refused from a neighbour container (connection refused, the lead's probe). Ticked with PR #634's merge: run 6601 includes the 4.6
      verify (`4b396795` is an ancestor of `a2b23a42`), which the earlier status line asked for.
## 5. Edge: Caddy and Cloudflare
- [ ] 5.1 `Caddyfile.local.j2`: a route flag (proposed `mcp_api`) rendering the allowlist
      shape for the exact path `/mcp`: Bearer-shape 401 inside the handle block, `reverse_proxy`
      with
      `flush_interval -1` and the route's `upstream_tls`, bare `handle { respond 404 }`.
      Factor what it shares with the `inference_api` shape into one macro (no fork), and
      prove the inference route renders byte-identical before and after
- [ ] 5.2 `platform/inventory/local-dev.yml.example`: the MCP route at
      `agw_mcp_hostname`'s local value, upstream the gateway's `default` listener; the
      single-label host needs no new certificate SAN
- [ ] 5.3 Production site: the matching block in site-config's `caddy_managed_sites` for
      `agw_mcp_hostname`, on the site-config branch for this change (its own PR; the
      Caddy deploy that places it is task 6.5)
- [ ] 5.4 `platform/infra/cloudflare`: the `mcp` record in `dns.tf` (proxied, like the
      other platform names), the `mcp-api-bypass-challenge` skip rule in `waf.tf` limited to
      the MCP host and `/mcp`, and exactly one rate-limit rule in `ratelimit.tf` with its
      own bucket from finding 1.7 (G3). `tofu fmt -check` and `tofu validate` pass
- [ ] 5.5 BATS: the MCP route answers 404 for `/v1/models`, `/sse`, `/metrics` and 401
      for a missing or malformed Bearer header; the inference route's rendering is
      unchanged; a Terraform test or grep asserts the Cloudflare expressions name the MCP
      host and not the inference host's rules. Mutation-check each
- [ ] 5.6 Local proof through the local Caddy deploy (`deploy-caddy.yml` through the local
      Semaphore): the MCP hostname, then the inference hostname's `/mcp`
- [ ] 5.7 Validation gate, local: no key and a malformed key are each 401 with no
      challenge header, proving scenario "The MCP hostname is not reachable without a
      key"; `/v1/models`, `/sse` and `/metrics` with a valid key are 404, proving scenario
      "Only the MCP path reaches the gateway on the MCP hostname"; `/mcp` on the inference
      hostname is 404 and `/v1/*` and `/health` are as before, proving scenario "The
      inference allowlist is unchanged"

## 6. Production rollout
Operator-run, one `(Dev)` template at a time, dry run first (`--check` shows a diff and
changes nothing), each result read before the next. No run is started without the
operator's go for it. site-config changes land through its own pull requests.
- [x] 6.1 site-config branch A: the declared admin list, the declared exceptions list,
      `agw_mcp_*` variables (flag false) and the MCP Caddy block. `agw_clients` is left
      untouched in this change
      2026-10-14 (site-config pull requests, read by the lead with `gh`): landed as two
      site-config pull requests. #86 (merged b95b65e6) declares `platform_admins` at
      `agent_cloud.vars`: five entries, field-identical to the shared default per review;
      the `jacob` identity is `agw_identity` `jacob`; `agw_admin_identities_enabled` is true.
      #87 (merged cb38d044) declares `agw_mcp_enabled` false, `agw_mcp_hostname`
      `mcp.uhstray.io`, `agw_loopback_port` 4002, `agw_mcp_openapi_targets` pointing at
      `gateway/agentgateway-v1.json`, `mcp-self` in `agw_clients` with `tokens_per_hour`
      2000000 (provisional, to be set from the 6.6 measurement), and the `mcp.uhstray.io`
      Caddy block. `platform_admins_exceptions` was not declared: the live membership equals
      the list, so it would be empty. `mcp-self` is a NEW internal client, not a derived admin;
      the text's "`agw_clients` is left untouched" is about not removing the admin names
      (that is 6.8), which this change does not do. Ticked.
- [x] 6.2 `Deploy Authentik (Dev)`: dry run, apply; the read-back verify passes (including
      the drift guard, task 2.9) and no user changes (task 2.6's equality, live)
      2026-10-14 (task output read from production Semaphore by the lead): dry run task 5665
      (changes read: the three old static blueprints removed, `platform-admins.yaml` rendered,
      `platform-users.yaml` loses the admin entries; passwords and the `svc-automation` API
      token identifier unchanged); real run task 5667 (changed 7; VERIFY OK: 12 blueprints,
      6 accounts, 1 retired; group `platform-admins` members exactly the five declared);
      rerun task 5669 changed=0. Ticked. The same runs are the live evidence for tasks 2.6
      (no user change), 2.9 (drift guard) and 2.10 in section 2, which are not ticked here.
      - [x] 6.2a Preflight, before the FIRST production Authentik deploy of this change: read
        the production `platform-admins` membership read-only (the group's member names, no
        values) and compare it with the declared list. Any member the list does not cover is
        declared in `platform_admins_exceptions` in site-config first; otherwise the drift
        guard fails the deploy after the blueprints have already applied
      - [x] 6.2b Preflight, before turning on `agw_admin_identities_enabled` in production:
        site-config declares `platform_admins` EXPLICITLY, at a scope both the Authentik and
        the agentgateway hosts see (the `agent_cloud` group vars or `all`). The shared default
        list is built from per-user vars (`wisward_*`, `jacob_*`, `stray_admin_email`) that
        site-config sets on the Authentik host only; on the gateway host they are undefined,
        so the default would yield placeholder usernames and the wrong identities. Guard task
        idea: the gateway deploy, with the gate on, refuses when `platform_admins` is not
        explicitly defined (that is, when it would fall back to the built default).
        Which administrators the credential print may print is NOT part of that list: it is the
        committed constant `_platform_admins_printable_keys`, changed by pull request, never by
        site-config or an extra var.
        2026-10-14 (task output read from production Semaphore by the lead): 6.2a: NOT read as a
        preflight before the deploy; read back from verify run 5667, after the apply: the
        membership is exactly the declared list, so no exceptions are needed (the drift guard
        passing confirms it). 6.2b: `platform_admins` is declared explicitly at `agent_cloud.vars`,
        which both hosts see (site-config #86). Both ticked.
- [ ] 6.3 `Deploy agentgateway (Dev)` with the flag false, from a branch that carries the
      derivation (task 2.4): dry run; apply; then read the rendered config on the host (or
      the deploy's own report), counting `keyHash` lines and never printing them: the count
      equals the one read before the deploy plus the newly declared admins, the overlap is
      reported by name, existing hashes unchanged, verify asserts `/mcp` is not served.
      Whether `--check` output shows the rendered key-hash lines is UNVERIFIED, so the
      count is read from the host after the real deploy, not from the dry-run diff
      2026-10-14 (task output read from production Semaphore by the lead): with admin
      identities on, dry run task 5670 and real run task 5671 at 37ea003b: the render check
      enrolled every identity, the overlap was reported ("Merged once, migration overlap
      (platform_admins and agw_clients): stray."), and the keyed round-trip passed. With the
      MCP code (#634, merged ec8da98b): dry run 5676, real run 5678: "MCP is disabled: a keyed
      initialize at /mcp is not an MCP reply (HTTP 400)." After site-config #87: dry run 5681,
      real run 5683 (`client_mcp-self` minted, MCP still disabled). NOT ticked: the
      `keyHash` count before and after, and the unchanged-hash proof, were not read (the render
      check asserts one entry per declared identity but prints no count and compares no
      earlier state). Remaining: read the count from the host, read-only, names and counts only.
- [ ] 6.4 `Apply Cloudflare Tofu (Dev)`, `tofu_action` plan: expected diff exactly the
      record and two rules, zero other change; then apply, then a zero-diff plan
      2026-10-14: not started; waits on PR #636.
- [ ] 6.5 `Manage Caddy Sites (Dev)`: dry run, apply; the inference block unchanged in the
      dry-run diff
      2026-10-14: not started; waits on PR #636.
- [ ] 6.6 Flip `agw_mcp_enabled`, `Deploy agentgateway (Dev)`; verify; then through the
      MCP hostname hold an `initialize` and an idle stream past 125 s, recording the
      observed status; `statefulMode: stateless` was chosen from the spike (3.2), so confirm it
      holds and set the rate-limit bucket from the measurement (a bucket change is a
      follow-up tofu plan and apply)
- [ ] 6.7 Hand out each declared admin's key with `Back Up Credentials to site-config (Dev)`,
      `credential_service=agentgateway`, `credential_fields=client_<name>` listed
      explicitly (never empty, Open Question 6); output carries names only
      2026-10-14 (task output read from production Semaphore by the lead): handed out so far:
      `client_stray`, task 5638 (branch `backup/agentgateway-20261014T204513Z-ba2dec`), and
      `client_jacob`, dry run 5672 then real run 5673 (branch
      `backup/agentgateway-20261014T222727Z-f28619`), the fields named explicitly. The other
      admins (`agent-cloud-admin`, `wisward`, `svc-automation`) are not handed out yet.
      Not ticked.
- [ ] 6.8 site-config branch B: remove each derived name from `agw_clients`. Hold this
      merge until 6.3's code is the deployed branch (before that the derivation does not
      exist and removal would drop the key). The render changes only on deploy, so after the
      site-config merge: sync the inventory into Semaphore (`sync-inventory.yml`, check
      mode first), then `Deploy agentgateway (Dev)`, dry run then real; read the rendered
      `keyHash` count on the host (or the deploy's own report) before and after, as in 6.3.
      A drop is a dead key: revert branch B, sync, redeploy
- [ ] 6.9 Validation gate, production: an unauthenticated `initialize` to the MCP hostname
      is 401 with no challenge header, proving scenario "The MCP hostname is not reachable
      without a key"; the newly declared admin's `client_<name>` exists and its key
      authenticates, proving scenario "A declared admin gets an identity on the next
      deploy"; the deploy's own verify passes, proving scenario "Deploy proves an
      authenticated initialize"

## 7. Client templates and installer
- [ ] 7.1 Re-verify the client facts against the live documentation and the installed
      versions before any template depends on them (the Research bullets in `design.md`):
      `claude mcp add` flags and `-s user`, `headersHelper` and its time limit, `${VAR}`
      expansion, the `ANTHROPIC_*` header caveat; pi's `!command` and `${VAR}` forms,
      SSE rejection and the user-level file path; the Keychain read command on macOS. Record
      each with its date and source; a differing fact amends the template, not the design
- [ ] 7.2 Templates under `platform/services/agentgateway/context/clients/`
      (`claude-code.json`, `pi-mcp.json`): a placeholder URL (the installer takes the
      hostname as an argument; the repository is public and a workstation has no
      inventory), the key by command reference, no value
- [ ] 7.3 The installer (proposed `scripts/agw-mcp-client.py`, Python standard library):
      takes the MCP hostname as an argument; uses the harness CLI where one exists
      (`claude mcp add -s user`, `claude mcp remove`) and a merge-only JSON edit for pi's
      user-level file, atomic write preserving the file's mode; `--dry-run` showing which entries
      would change, a removal mode deleting exactly
      the agentgateway entry, names only in all output, exit non-zero when a harness file
      is unparseable rather than rewriting it
- [ ] 7.4 The key helper, the one mechanism for both harnesses (Claude Code
      `headersHelper`, pi `!command`): at connect time reads the key from the Keychain or
      the site-config handout file (`secrets/agentgateway/client_<name>.txt` in the
      operator's clone) and prints the `Authorization` header JSON the harness expects, with
      no environment export and no file written; `${AGW_MCP_KEY}` is documented as a weaker
      fallback only where `headersHelper` is unavailable. It prints only what the harness
      requires on stdout, nothing on stderr but names, and
      fails with a names-only message when neither exists
- [ ] 7.5 `make` targets with `##` help text (proposed `agw-mcp-client` and
      `agw-mcp-client-remove`); `.gitignore` gains the repo-root `.mcp.json`
- [ ] 7.6 Tests with a temporary home: every other server entry is unchanged as parsed JSON
      (entry-equal) after install, a second install changes nothing, removal restores the
      original parsed content, an unparseable file is left untouched. A scan test: no Bearer value
      or key-shaped string in any
      client file, and no repo-root `.mcp.json`. Mutation-check each
- [ ] 7.7 Live proof with a real handed-out key: each harness lists the tools; a wrong key
      fails with a 401 reported by name only; record each harness's handshake request count
      against the bucket (spike 1.7 measured only a scripted client's three) and, if it
      differs, correct task 5.4's figure in a follow-up plan
- [ ] 7.8 Validation gate: the scan finds no secret and no `.mcp.json`, proving scenario
      "Client templates contain no secret"; the temporary-home run proves scenario
      "Installing client configuration preserves existing servers"; the repeat run proves
      scenario "Installing client configuration twice changes nothing"

## 8. Documentation and records
- [ ] 8.1 `platform/services/agentgateway/context/architecture.md` and `agent-practices.md`:
      a row for each operation (add and remove an admin identity, set the hostname, enable
      and disable MCP, hand out a key with explicit fields, install and remove client
      configuration, roll back), naming only templates and variables that exist
- [ ] 8.2 `platform/services/agentgateway/deployment/README.md` and the Authentik README:
      the new variables and the declared admin list
- [ ] 8.3 AGENTS.md: the Deploy agentgateway and Manage agentgateway Client Key rows gain
      the MCP verify and the merged identity list; the Cloudflare edge paragraph names the
      MCP record and rules
- [ ] 8.4 A Proposed amendment in `plan/architecture/05-platform-infra.md` recording the
      MCP hostname decision and the one-list admin authority, for the operator to ratify
      (never marked Accepted by the implementer); the sibling change's "Out of scope: MCP"
      line gains a pointer here
- [ ] 8.5 `docs/MISTAKES.md`: an entry for any mistake that survived long enough to cost
      work during this change, in that file's format, naming where it is enforced
- [ ] 8.6 Operator hand-off, no fix in this change: `/config_dump` leaves
      `config.database.url` (the budget Postgres password, expanded from `$AGW_DATABASE_URL`)
      unredacted on the admin listener, which is pinned to the container loopback (spike
      1.8). This is existing in every current deployment; record it in the PR description
      and track it as its own change (for example a read-only database role, or removing the
      password from the URL)
- [ ] 8.7 Validation gate: every row added in 8.1 names a template or variable found by
      search in the repository, proving scenario "The operator documentation covers every
      MCP change"

## 9. Final validation and close-out
- [ ] 9.1 The full local replay on a clean local stack, in order: sections 2 to 5's gates
- [ ] 9.2 CI-equivalent runs from the repository root: `bats platform/tests/`, the pytest
      collection from `pyproject.toml`, `ruff`, `shellcheck`, `ansible-lint`, `yamllint`,
      `tofu fmt -check`; then the pre-push audit on the staged diff (the greps and the
      scanner) and `/simplify` and `/security-review`
- [ ] 9.3 Push and open the pull request into `dev` only when the operator asks; one review
      round, every finding fixed or answered, checks green on the final head
- [ ] 9.4 After the production rollout, `openspec archive agentgateway-mcp`, and retain one
      memory in the repo's Hindsight bank: whether it worked, the root cause of anything
      that failed, and the constraint found (worked / dead end / corrected)
- [ ] 9.5 Validation gate: a request for `/mcp` on the inference hostname is still 404
      after everything above, proving scenario "The inference allowlist is unchanged"; an
      unauthenticated `initialize` is still 401 on the MCP hostname in production, proving
      scenario "Unauthenticated MCP request is refused"
