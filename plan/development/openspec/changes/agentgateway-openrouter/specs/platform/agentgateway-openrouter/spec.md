# platform/agentgateway-openrouter

OpenRouter models served by the agentgateway beside the vLLM models, on the same `/v1`, behind the
same per-identity keys and budgets, opt-in per model and per identity.

## ADDED Requirements

### Requirement: An OpenRouter model is declared by name and reachable only by named identities
The gateway SHALL serve an `agw_models` entry with `provider: openrouter` through its native
OpenRouter provider, mapping the entry's `name` to its `upstream_model` (an OpenRouter id of the
form vendor/model), and SHALL render an entry without `provider` exactly as it rendered before
this requirement. The rendered configuration MUST NOT contain a wildcard model, and MUST remove
every OpenRouter model name from the `allowedModels` of each identity, the shared-key identity
included, that is not allowed. The allowed identities are those in `agw_openrouter_identities`
and, when `agw_openrouter_admins` is true, every admin-derived identity; a service client is
allowed only by being listed.

#### Scenario: An identity not named is denied the OpenRouter model
- WHEN `agw_openrouter_identities` is `[skynet]` and a client authenticated as `ws` requests the
  OpenRouter model
- THEN the gateway refuses the request and nothing is sent to OpenRouter

#### Scenario: Admin identities are allowed when the admin flag is true
- WHEN `agw_openrouter_admins` is true and platform admins are derived into gateway identities
- THEN each admin-derived identity may use the OpenRouter models and a service client not listed
  in `agw_openrouter_identities` may not

#### Scenario: A named identity keeps its own allow-list
- WHEN an identity in `agw_openrouter_identities` has an `allowed_models` list that names an
  OpenRouter model
- THEN its rendered `allowedModels` is that list unchanged

#### Scenario: Inventory without the provider is unchanged
- WHEN no `agw_models` entry sets `provider`
- THEN the rendered configuration, the secret declaration and the environment file are
  byte-identical to what they were before, and the OpenRouter key is neither declared nor required

### Requirement: The deploy refuses an unsafe OpenRouter declaration
The deploy SHALL, before any render, restart or secret write, refuse an OpenRouter entry whose
name or `upstream_model` is a wildcard, is empty, or is outside the safe charset, a `provider`
value other than `openrouter`, a duplicated model name, an empty union of allowed
identities (nobody listed and no admin identity derived) or a non-list
`agw_openrouter_identities` while an OpenRouter model is declared, and an identity in that list
that is not a declared gateway identity.

#### Scenario: A wildcard model is refused
- WHEN an OpenRouter entry is named `*`
- THEN the deploy fails naming the entry and rendering nothing

#### Scenario: Nobody allowed is refused
- WHEN an OpenRouter model is declared and neither `agw_openrouter_identities` nor an enabled
  `agw_openrouter_admins` yields an identity
- THEN the deploy fails, and a template rendered without the guard denies every identity the model

### Requirement: The OpenRouter credential is held by the gateway only
The gateway SHALL take its OpenRouter key from `secret/services/agentgateway:openrouter_api_key`,
declared as an existing secret and required exactly while an OpenRouter model is declared, SHALL
render it only into the 0600 environment file, and SHALL reference it from the configuration by
environment variable. The key value MUST NOT appear in the rendered configuration, and a client's
key MUST NOT be forwarded to OpenRouter.

#### Scenario: The key is absent from the config
- WHEN an OpenRouter model is rendered
- THEN `config.yaml` contains `$OPENROUTER_API_KEY` and not the key's value

#### Scenario: A missing key stops the deploy before any write
- WHEN an OpenRouter model is declared and the store holds no `openrouter_api_key`
- THEN the secrets step fails before any secret is written or any file rendered

### Requirement: The verify spends OpenRouter tokens only when asked
The gateway deploy verify SHALL require the keyed listing to include every OpenRouter model the
verifying identity may use, SHALL send its default completion to a model that is not an OpenRouter
model, and SHALL send one OpenRouter completion with a token limit of 8 only when
`agw_verify_openrouter` is true, failing the deploy when it is not a successful chat completion.

#### Scenario: Default verify makes no OpenRouter request
- WHEN `agw_verify_openrouter` is unset
- THEN the verify sends no completion to an OpenRouter model

#### Scenario: Opt-in verify proves the path
- WHEN `agw_verify_openrouter` is true and the verifying identity is allowed OpenRouter models
- THEN one 8-token completion goes to the first OpenRouter model it may use, and the report names
  the model and the status only
