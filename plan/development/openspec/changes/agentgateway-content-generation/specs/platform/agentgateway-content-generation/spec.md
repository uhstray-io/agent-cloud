# platform/agentgateway-content-generation

The agentgateway's content-generation caller: its own identity, token budget and model allow-list,
the internal-listener client-leaf path, a renewal proof for each allowlisted leaf, and a per-identity
request limit before any image model is served. These requirements describe planned behaviour. Plan
27 (`plan/development/27-agentgateway-content-generation.md`) gives the decisions and their sources.

## ADDED Requirements

### Requirement: The content caller has its own identity, budget and allow-list
The gateway SHALL serve the content caller under its own identity, `activepieces`, with its own
rolling hourly token budget and an explicit model allow-list. The allow-list MUST NOT name an
OpenRouter model. The deploy's keyed verify MUST NOT run as this identity.

#### Scenario: Content identity budget and allow-list
- WHEN `activepieces` is enrolled with a `tokens_per_hour` budget and an `allowed_models` list that names the text model
- THEN the rendered configuration gives it that budget and that allow-list, and no other model

#### Scenario: Content identity is not the verify identity
- WHEN `activepieces` is enrolled and another identity precedes it in the merged identity list
- THEN the deploy's keyed verify runs as that preceding identity

#### Scenario: Content identity denied OpenRouter models
- WHEN `activepieces` is not in `agw_openrouter_identities` and an OpenRouter model is declared
- THEN the rendered allow-list of `activepieces` omits that model

### Requirement: The content caller reaches the gateway only on its internal listener with an allowlisted client leaf
The gateway SHALL accept the content caller only over its internal listener, with an internal-CA
client leaf whose name is on `agw_client_cert_allowlist`, or whose SAN is shared with an allowlisted
leaf. The gateway MUST refuse a request that presents no client certificate in the TLS handshake. It
MUST refuse a leaf that the SAN rule does not admit with HTTP 403 before it checks the API key.

#### Scenario: Request without a client certificate
- WHEN the content caller sends a request that presents no client certificate
- THEN the TLS handshake is refused and no API key is checked

#### Scenario: Off-allowlist certificate
- WHEN a client presents a leaf whose name and SANs the SAN rule does not admit
- THEN the gateway answers HTTP 403 before it checks the API key

#### Scenario: Keyless request with an admitted certificate
- WHEN an admitted leaf sends a request without an API key
- THEN the gateway answers HTTP 401

### Requirement: Every client leaf on the allowlist has a renewal proof path
Certificate renewal SHALL prove each allowlisted client leaf in use before it reports that leaf as
renewed. A client leaf with no proof path MUST be reported as an error, and renewal MUST issue no
certificate while any such error stands.

#### Scenario: Allowlisted leaf proven at renewal
- WHEN renewal renews an allowlisted client leaf whose host sets `agw_verify_base_url` to an `https://` URL
- THEN the probe answers 401 with the leaf, refuses the same URL without a certificate, and the run records the leaf as proven

#### Scenario: Client leaf without a proof path
- WHEN a client leaf is declared with no proof path
- THEN renewal reports an error naming the leaf and issues no certificate

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
