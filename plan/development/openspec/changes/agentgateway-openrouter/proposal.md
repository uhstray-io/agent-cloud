# OpenRouter models on the agentgateway

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Requested by the operator on 2026-10-14
("sooner than later"); the decisions below were made then and are recorded as decided in
`design.md`.

Companion changes: `inference-gateway-agentgateway` (the gateway this change extends: its `llm`
model list, key rendering, budgets and deploy verify are the base) and `agentgateway-mcp` (edits
the same key-rendering macro; expect a merge, see `design.md`, "Interaction with concurrent
branches").

## Why

The gateway serves one upstream, vLLM on the DGX Spark pair. Some work wants a hosted model the
Sparks do not run. OpenRouter fronts many of them behind one OpenAI-compatible API and one key.
Serving them through the gateway keeps what the gateway exists for: clients hold a per-identity
key, never the upstream credential; per-identity token budgets and the request ceiling still
apply; and the identity label stays on metrics and logs.

OpenRouter is billed per token on the operator's account, which vLLM on owned hardware is not.
So the change is not "add a provider": it must make spend opt-in at two levels, per model and per
identity, and must not let a typo or a wildcard open a paid route to every client.

## What changes

- `agw_models` entries gain an optional `provider: openrouter`. Such an entry is `{ name,
  provider: openrouter, upstream_model: <vendor/model> }` and renders as agentgateway's native
  OpenRouter provider. Entries without `provider` render byte-identically to before.
- The key is `secret/services/agentgateway:openrouter_api_key`, an `existing` secret seeded with
  `Seed OpenBao Key`, required only while an OpenRouter model is declared, rendered into the
  gateway's 0600 `.env`, and referenced from the config as an environment variable.
- New inventory `agw_openrouter_identities` names the identities that may call OpenRouter models,
  and `agw_openrouter_admins` (default false) adds every admin-derived identity. Every other
  identity is denied them through its `allowedModels`.
- The deploy refuses a wildcard or empty model, an unsupported `provider`, a duplicate name, and an
  empty union of allowed identities before any render or restart.
- The deploy verify checks the keyed listing includes the OpenRouter models the verifying identity
  may use; an opt-in `agw_verify_openrouter` spends one 8-token completion to prove the path.

## Impact

- Files: `config.yaml.j2`, `env.j2`, `deploy-agentgateway.yml`,
  `vars/secret-declarations/agentgateway.yml`, `local-dev.yml.example`, the gateway's
  `architecture.md`, one row of the OpenBao layout in `AGENTS.md`, and the test
  `platform/tests/test_agw_openrouter.py`.
- No new dependency, no new container, no new listener, no change to the public edge.
- Out of the repository: seeding the key in production, declaring the models and identities in
  site-config, and the production deploy. Listed as open tasks.
