# platform/agentgateway-content-generation

The agentgateway's content-generation caller: its own identity, token budget and model allow-list,
one declared client leaf named on the gateway's allowlist, and a per-identity request limit before
any image model is served. These requirements describe planned behaviour. Behaviour that another
change owns is named by that change and requirement, not restated. Plan 27
(`plan/development/27-agentgateway-content-generation.md`) gives the decisions and their sources.

## ADDED Requirements

### Requirement: The content caller has its own identity, budget and allow-list
The gateway SHALL serve the content caller under its own identity, `activepieces`, enrolled as the
inference-gateway-agentgateway requirement "Every client has its own identity and limit" enrolls a
client, with its own `tokens_per_hour` budget and an `allowed_models` list that names the text
model. `activepieces` MUST NOT be named in `agw_openrouter_identities`, so the
agentgateway-openrouter requirement "An OpenRouter model is declared by name and reachable only by
named identities" keeps every OpenRouter model from it. The deploy's keyed verify MUST NOT run as
this identity.

#### Scenario: Content identity budget and allow-list
- WHEN `activepieces` is enrolled with a `tokens_per_hour` budget and an `allowed_models` list that names the text model
- THEN the rendered configuration gives it that budget and that allow-list, and no other model

#### Scenario: Content identity is not the verify identity
- WHEN `activepieces` is enrolled and `agw_verify_client` names another enrolled identity
- THEN the deploy's keyed verify runs as that identity and spends none of the `activepieces` budget

### Requirement: The content caller holds one declared client leaf named on the allowlist
The Activepieces host SHALL hold one declared client-profile leaf, issued under the
production-internal-ca requirement "Leaves are issued from consumer-held keys for declared names
only" and renewed under its requirement "Leaves are renewed on a schedule and the new certificate is
in use". The leaf's name SHALL be on `agw_client_cert_allowlist`. Admission at the gateway requires
the gateway's listener TLS on; see the inference-gateway-agentgateway requirement "The gateway
admits only declared client certificates".

#### Scenario: Content caller's leaf reaches the key check
- WHEN the gateway's listener TLS is on and the Activepieces host presents its leaf with no API key
- THEN the handshake completes and the request reaches the gateway's key check

### Requirement: An image model is served only with a per-identity request limit in force
The gateway SHALL serve an image model only after a per-identity request limit for that model is in
force. A request past its identity's limit MUST be refused for that identity alone. The deploy MUST
refuse an image model that is declared with no per-identity request limit.

#### Scenario: Request past the image cap
- WHEN an identity has used its image request limit
- THEN its further image requests are refused, and another identity's image request is still served

#### Scenario: Image model without a request limit
- WHEN an image model is declared and no per-identity request limit covers it
- THEN the deploy fails before it renders the configuration
