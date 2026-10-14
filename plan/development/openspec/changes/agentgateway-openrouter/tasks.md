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
- [x] 3.3 Operator decision 2026-10-14: the seven models in `design.md` D4 (client name = OpenRouter
      id); `agw_openrouter_identities: [stray]` with `agw_openrouter_admins: true`. The key is
      already seeded in production (Seed OpenBao Key, Semaphore task 5659)
- [ ] 3.4 Declare them in site-config's gateway inventory; deploy through the Semaphore template
      (`(Dev)` first); optionally `-e agw_verify_openrouter=true` once
- [ ] 3.5 Measure whether OpenRouter tolerates `continuous_usage_stats`; if so, use the vLLM
      transformation for OpenRouter models too (design D5)
- [ ] 3.6 After `agentgateway-mcp` lands: drop the OpenRouter names from the `mcp-self` key entry
- [ ] 3.7 Set `agw_conformance_model` to a vLLM model for the conformance playbook while an
      OpenRouter model can be first in an identity's list
