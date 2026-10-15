# Design: OpenRouter models on the agentgateway

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Decisions made by the operator on 2026-10-14.

## Spike result (2026-10-14, pinned image `cr.agentgateway.dev/agentgateway:v1.5.0`)

`--validate-only` on a configuration with one `custom` vLLM model and one OpenRouter model:

- `provider: openrouter` (a lowercase string) is accepted. `openRouter`, `Openrouter` and the
  mapping form `provider: {openRouter: {}}` are rejected ("data did not match any variant of
  untagged enum LocalModelAIProvider").
- In v1.5.0 it is not a distinct provider type but a preset of the custom provider
  (`crates/llm/src/custom.rs:52-67`, `ProviderPreset::Openrouter`). The preset supplies the base
  URL `https://openrouter.ai/api/v1` (`custom.rs:81`; applied at
  `crates/agentgateway/src/types/local.rs:1020-1022`), turns on backend TLS for an `https` URL
  (`local.rs:1062-1064`) and declares the formats completions, messages, responses, embeddings
  and rerank (`custom.rs:144-153`).
- `params.model` is the id sent upstream and `name` the client-facing name, as for the vLLM
  entries' `upstream_model`; `params.apiKey: $OPENROUTER_API_KEY` is an environment reference.
- The per-model `transformation` block (the budget's streamed-usage CEL) is valid on it.
- The production image carries public CA roots (`etc/ssl/certs/ca-certificates.crt`). The local-dev
  overlay sets `SSL_CERT_FILE` to the step-ca bundle, which replaces them, so a local gateway
  cannot verify `openrouter.ai`.
- On the wire (test `test_the_gateway_sends_the_openrouter_id_and_key_and_gates_the_model_by_identity`,
  with the preset's base URL pointed at a local recorder; nothing was sent to openrouter.ai): the
  client's `or-mini` reaches the upstream as `openai/gpt-4o-mini`, the upstream sees the OpenRouter
  key and never the client's, a streamed request carries `stream_options: {include_usage: true}`,
  and an identity whose `allowedModels` excludes the model is refused.

## Decisions

**D1. Inventory shape.** `agw_models` entries gain optional `provider: openrouter`; an entry
without it takes the existing vLLM path unchanged. An OpenRouter entry is `{ name, provider,
upstream_model }`. Why: the inventory already lists models per name; a second list would need its
own name-collision and allow-list handling.

**D2. No wildcard, ever.** A `*` in an OpenRouter name or id, an empty or malformed
`upstream_model`, a duplicate name, or a `provider` value other than `openrouter` is refused
before render. Why: cost. The gateway could route a wildcard to any OpenRouter model, including
expensive ones; the listed models are exactly the reachable ones. An unsupported `provider` value
is refused rather than rendered as vLLM so a typo cannot silently change where a request goes.

**D3. The key.** `secret/services/agentgateway:openrouter_api_key`, `existing` (never generated),
required, declared and rendered only while an OpenRouter model is declared. Why declared
conditionally: manage-secrets writes a declared `existing` field back as an empty string when the
store lacks it, so an unconditional declaration would create an empty field on every gateway that
never uses OpenRouter. The required-secret check runs before any write; its message names the
service and field, and the architecture note and `AGENTS.md` name the seed template.

**D4. Cost guard, per identity.** Who may call OpenRouter models is the union of
`agw_openrouter_identities` and, when `agw_openrouter_admins` is true (default false; operator
decision 2026-10-14, "the operator and other admin-level identities"), every admin-derived identity
(`_agw_admin_identities`, from `platform_admins` once the admin-identity gate derives them; service
clients in `agw_clients` are not in it), each name once. `config.yaml.j2` removes every OpenRouter
name from the `allowedModels` of each identity not in the union, starting from the identity's `agw_client_policies.<name>.allowed_models` or, with none,
every declared model (so an identity that had no list now gets one that omits only the OpenRouter
names). The `legacy-shared` identity is treated the same. An empty union is refused by the
deploy (changed 2026-10-14 from "an empty list means nobody": a deploy that enables paid models
for nobody is a mistake, not a decision) and, if a caller renders without the deploy's guard, the
template fails closed and denies everyone. Names in the explicit list must be declared gateway
identities, so a typo cannot silently grant nothing. Production intent: `agw_openrouter_identities`
names the operator's own gateway identity and `agw_openrouter_admins` is true, so the other admins
gain access as their identities are enabled. Production models (operator, 2026-10-14; the OpenRouter account itself also enforces a
provider and model allow-list): `xiaomi/mimo-v2.6-flash`, `deepseek/deepseek-v4.1-flash`,
`z-ai/glm-5.3-flash`, `xiaomi/mimo-v2.6-pro`, `openai/gpt-6-luna`, `mistralai/mistral-large-4-0`,
`z-ai/glm-5.3`, each served under its own id as the client name, listed explicitly, no wildcard.

**D5. Budget charging.** The per-model usage transformation is kept for OpenRouter models, with
`include_usage` only. `continuous_usage_stats` is a vLLM extension; whether OpenRouter tolerates an
unknown `stream_options` key is unverified, and a 400 on every stream would be worse than a
weaker guarantee. Consequence: on an OpenRouter stream, usage arrives only on the final chunk, so
a client that disconnects before it is not charged. The vLLM models keep the every-chunk
behaviour. Revisit when OpenRouter's behaviour on the extra key is measured.

**D6. Verify.** The keyed `/v1/models` listing must include every OpenRouter model the verifying
identity may use (it is in the identity's expected set only if it is in
the union above), and the default completion is always a non-OpenRouter model:
OpenRouter models are ordered last in the expected list, and an identity allowed only OpenRouter
models is refused by the verify. `agw_verify_openrouter: true` sends ONE completion with
`max_tokens: 8` to the first OpenRouter model that identity may use, and fails the deploy when it
is not a 200 chat completion. Why opt-in: a deploy that spent money by default would be spending
it on every redeploy. The report names models and statuses only.

**D7. Local-dev.** The inventory example carries a commented OpenRouter block, off. Why off: the
local overlay's `SSL_CERT_FILE` makes the TLS connection to `openrouter.ai` fail.

## Interaction with `agentgateway-mcp` (landed as PR 634, merged here 2026-10-14)

The MCP change turned the key loop into one macro that renders both the `llm` and the `mcp` key
lists; the OpenRouter exclusion lives in that macro's `llm` branch, so the internal `mcp-self`
identity (a service identity the MCP target calls /v1 with) gets an explicit `allowedModels` that
omits the OpenRouter names whenever one is declared. The `mcp` key entries carry no `allowedModels`
by design. `mcp-self` and `legacy-shared` can never be granted OpenRouter models: the deploy
refuses an `agw_openrouter_identities` entry naming either, and the template drops both from the
union regardless. The MCP verify's chat call asks the first non-OpenRouter model.

## Probes never spend OpenRouter tokens

Every model pick in the repository that sends real traffic filters the OpenRouter names out: the
deploy verify (`_verify_models[0]`, the MCP `_mcp_model`), the conformance run
(`run-agw-conformance.yml`, `_client_models`; `agw_conformance_model` still names one explicitly)
and the isolation probe (`probe-agentgateway-isolation.yml`, burst and control models). A literal
glob character in any model name is refused before render.

## Not done here

- A spend ceiling in money terms. The per-identity token budget is the only per-identity control
  v1.5.0 offers; OpenRouter's own per-key credit limit is the hard cap and is set at the provider.
