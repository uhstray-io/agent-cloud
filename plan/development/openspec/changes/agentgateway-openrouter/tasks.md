# Tasks: OpenRouter models on the agentgateway

Every task is safe to rerun. Nothing here is pushed, opened as a pull request or run against
production until the operator says so for that action. Deploys go through the Semaphore
templates, never one-off writes.

## 1. Spike
- [x] 1.1 Validate `provider: openrouter` on the pinned image: accepted as a lowercase string, a
      custom-provider preset; `params.model`, `params.apiKey` and the usage `transformation` are
      valid on it (`design.md`, "Spike result")
- [x] 1.2 Wire check against a local recorder: model remap, the gateway's key and never the
      client's, stream options, identity gate (`test_the_gateway_sends_the_openrouter_id_and_key_and_gates_the_model_by_identity`)

## 2. Implementation
- [x] 2.1 `config.yaml.j2`: OpenRouter model entry; entries without `provider` render
      byte-identically (rendered prod-like and local-example inventories diffed against `origin/dev`:
      empty). Tests: `test_no_openrouter_model_renders_no_trace_of_it`,
      `test_a_declared_identities_list_changes_nothing_while_no_model_is_openrouter`
- [x] 2.2 `config.yaml.j2`: `allowedModels` removes OpenRouter names for identities not in
      `agw_openrouter_identities`, `legacy-shared` included. Tests:
      `test_only_the_named_identities_may_call_openrouter_models`,
      `test_an_existing_allow_list_is_narrowed_by_the_exclusion_not_replaced`,
      `test_an_undeclared_identities_list_denies_everyone_rather_than_allowing_everyone`,
      `test_the_shared_key_identity_is_denied_openrouter_models_too`
- [x] 2.2a `agw_openrouter_admins`: admin-derived identities join the allowed union (service clients
      do not), each name once, an empty union refused. Tests: the `test_the_admin_flag_*` group,
      `test_an_empty_union_of_who_may_call_openrouter_is_refused`,
      `test_the_verify_runs_as_an_admin_identity_with_openrouter_models_last`
- [x] 2.3 Secret declaration: `openrouter_api_key` `existing`, declared and required only with an
      OpenRouter model; `env.j2` renders `OPENROUTER_API_KEY`. Tests:
      `test_the_openrouter_key_is_an_existing_secret_declared_and_required_with_a_model`,
      `test_the_openrouter_key_stays_required_when_the_vllm_upstream_takes_no_key`,
      `test_the_env_carries_the_openrouter_key_only_when_a_model_needs_it`,
      `test_without_an_openrouter_model_the_secret_declaration_is_unchanged`
- [x] 2.4 Refusals before render: wildcard, empty or malformed id, unsupported provider,
      duplicate, undeclared or malformed identities list, unknown identity (tests: the
      `test_*refused*` group in `test_agw_openrouter.py`;
      `test_the_deploy_refuses_a_malformed_declaration_before_any_render`)
- [x] 2.5 Deploy verify: listing includes allowed OpenRouter models, default completion never
      OpenRouter, opt-in `agw_verify_openrouter` (tests: `test_the_verifying_identity_sees_openrouter_models_last_and_only_when_named`,
      `test_an_identity_allowed_only_openrouter_models_is_refused_by_the_verify`,
      `test_every_opt_in_task_is_gated_on_the_flag_and_the_completion_is_tiny` and the opt-in group)
- [x] 2.6 The render passes the pinned image's validator (`test_the_render_passes_the_pinned_images_validator`)
- [x] 2.7 Docs: `architecture.md` "OpenRouter models", `local-dev.yml.example` (off), the `AGENTS.md`
      OpenBao layout row

## 3. Open (operator and production; not done by this change)
- [ ] 3.1 Create an OpenRouter API key with a credit limit at the provider (the hard spend cap)
- [x] 3.2 Seed `secret/services/agentgateway:openrouter_api_key` with the `Seed OpenBao Key`
      template (done by the operator, Semaphore task 5659)
- [x] 3.3 Operator decision 2026-10-14: the seven models first listed in `design.md` D4 (an eighth
      was added in 3.9; client name = OpenRouter id); the operator's own identity in `agw_openrouter_identities`, with `agw_openrouter_admins: true`. The key is
      already seeded in production (Seed OpenBao Key, Semaphore task 5659)
- [x] 3.4 Declare them in site-config's gateway inventory; deploy through the Semaphore template
      (`(Dev)` first); optionally `-e agw_verify_openrouter=true` once
      2026-10-14/15 (site-config pull request and task output read from production Semaphore by
      the lead): site-config #88 (merged 97f2b056) declares the seven models
      (`xiaomi/mimo-v2.6-flash`, `deepseek/deepseek-v4.1-flash`, `z-ai/glm-5.3-flash`,
      `xiaomi/mimo-v2.6-pro`, `openai/gpt-6-luna`, `mistralai/mistral-large-4-0`,
      `z-ai/glm-5.3`), the operator's identity and `agw_openrouter_admins: true`; operator
      decision 2026-10-14: `svc-automation` keeps OpenRouter access. Inventory synced; Deploy
      agentgateway (Dev) dry run 5739, real run 5740: the verifying identity lists
      `qwen3.8-flash-next` and all seven OpenRouter models; the DGX completion returned 31
      tokens. Ticked: the optional `-e agw_verify_openrouter=true` run was not made (it is not a
      Semaphore survey field). NOT proven: a real completion from OpenRouter through the
      gateway; the operator's test is pending.
- [x] 3.8 First real completion from an OpenRouter model through the gateway, with an allowed
      identity's key against https://inference.uhstray.io/v1 (one small request), and the same request
      refused for a service identity. Deploy 5740 proved only the listing; `agw_verify_openrouter` is
      not a survey field of the Deploy template. Added 2026-10-15 so this proof cannot be lost.
      2026-10-15 03:19Z (lead probe, with the operator's key; the response was read by the lead, not
      re-read here): the first real completion through the gateway. `deepseek/deepseek-v4.1-flash`
      answered 200 with 53 tokens, an OpenRouter-reported cost of 1.01624e-05 USD and `is_byok`
      false; `qwen3.8-flash-next` answered 200. The refusal for a service identity is NOT proven
      live: no service key has been handed out, so that half rests on the render and test proof
      (3.6), not on a request. Not ticked: the task text asks for both.
      2026-10-15 (task output read from production Semaphore by the lead): the refusal half is proven
      live. agent-cloud #673 (merged as 4cae6baa, reviewed) makes every deploy send one request, as
      its verifying service identity, for an OpenRouter model that identity may not use. It passes
      only on the gateway's own refusal, HTTP 403 with error code `model_not_allowed`, measured
      against the real gateway image. Deploy agentgateway (Dev) dry run 6105, then real run 6106,
      `failed=0`: "The OpenRouter model xiaomi/mimo-v2.6-flash is refused for opencode (HTTP 403,
      model_not_allowed)." The allowed half is the 03:19Z completion above, plus
      `anthropic/claude-opus-5.5` answering 200 on 2026-10-15 (3.9). Both halves are proven, so ticked.
- [x] 3.9 Add `anthropic/claude-opus-5.5` (operator request 2026-10-15, after adding it to the
      OpenRouter account allowlist). Evidence read by the lead from production Semaphore and the probe
      response, not re-read here: site-config #94 (merged 8ee123d1, reviewed) declares it;
      inventory synced; Deploy agentgateway (Dev) dry run 5770, real run 5771: the verifying identity
      lists `qwen3.8-flash-next` and all eight OpenRouter models. Lead probe 2026-10-15 with the
      operator's key: `anthropic/claude-opus-5.5` answered 200 through
      https://inference.uhstray.io/v1 (22 tokens).
- [x] 3.5 Measure whether OpenRouter tolerates `continuous_usage_stats`; if so, use the vLLM
      transformation for OpenRouter models too (design D5)
      2026-10-15 (lead probe through https://inference.uhstray.io/v1 with the operator's key, not a
      Semaphore task): two streamed requests to `deepseek/deepseek-v4.1-flash`, `max_tokens` 24.
      - Without `stream_options`: HTTP 200, 10 chunks, usage on 1 chunk, the final one.
      - With `{"include_usage": true, "continuous_usage_stats": true}`: HTTP 200, 10 chunks,
        usage on 1 chunk, the final one.
      The template keeps the client's other `stream_options` keys (`config.yaml.j2`, the OpenRouter
      transformation), so the gateway sent the field upstream. Whether the native provider forwards an
      unknown key unchanged was not observed on the wire. Either way, no 400 came back and usage
      stayed on the final chunk only.
      The vLLM transformation's point, a charge on every chunk, cannot be had from OpenRouter, so
      the switch is not made. Ticked on the measurement; design D5's consequence stands: a client
      that disconnects before the final chunk is not charged.
- [x] 3.6 `mcp-self` excludes the OpenRouter names through the shared macro; it and `legacy-shared`
      cannot be listed (`test_mcp_self_keeps_an_explicit_allow_list_without_the_openrouter_names`,
      `test_an_internal_identity_listed_for_openrouter_is_refused_by_name`)
- [x] 3.7 Conformance run, isolation probe and the MCP verify never pick an OpenRouter model
      (`test_the_conformance_run_never_picks_an_openrouter_model` and its two siblings); glob
      characters in model names are refused (`test_a_glob_character_in_any_model_name_is_refused`)
