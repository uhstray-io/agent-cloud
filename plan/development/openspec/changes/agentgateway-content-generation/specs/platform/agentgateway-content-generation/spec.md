# platform/agentgateway-content-generation

The agentgateway's content-generation caller: its own identity, token budget and model allow-list,
the internal-listener client-leaf path, a renewal proof for its leaf, and a per-identity
request limit before any image model is served. These requirements describe planned behaviour. Plan
27 (`plan/development/27-agentgateway-content-generation.md`) gives the decisions and their sources.

## ADDED Requirements

### Requirement: The content caller has its own identity, budget and allow-list
The gateway SHALL serve the content caller under its own identity, `activepieces`, with its own token
budget (`tokens_per_hour`) and an explicit model allow-list that names the text model. The budget
mechanics are those of the inference-gateway-agentgateway requirement "Every client has its own
identity and limit". The content caller is not among the named identities that the
agentgateway-openrouter requirement "An OpenRouter model is declared by name and reachable only by
named identities" allows, so its allow-list MUST NOT name an OpenRouter model. The deploy's keyed
verify MUST NOT run as this identity.

#### Scenario: Content identity budget and allow-list
- WHEN `activepieces` is enrolled with a `tokens_per_hour` budget and an `allowed_models` list that names the text model
- THEN the rendered configuration gives it that budget and that allow-list, and no other model

#### Scenario: Content identity is not the verify identity
- WHEN `activepieces` is enrolled and `agw_verify_client` names another enrolled identity
- THEN the deploy's keyed verify runs as that identity and spends none of the `activepieces` budget

### Requirement: The content caller reaches the gateway only on its internal listener with an allowlisted client leaf
The content caller SHALL reach the gateway only over its internal listener, as a client admitted under
the inference-gateway-agentgateway requirement "The gateway admits only declared client certificates",
with its own client leaf named on `agw_client_cert_allowlist`. The gateway MUST refuse a leaf that the
allowlist does not admit with HTTP 403 before it checks the API key, so a caller holding such a leaf
learns nothing about a key's validity (measured on v1.5.0, `config.yaml.j2:44-46`).

#### Scenario: Off-allowlist certificate
- WHEN a client presents an internal-CA leaf that the allowlist does not admit, with or without a valid API key
- THEN the gateway answers HTTP 403 before it checks the API key

#### Scenario: Keyless request with an admitted certificate
- WHEN the content caller's allowlisted leaf sends a request without an API key
- THEN the gateway answers HTTP 401, so the request reached the key check

### Requirement: The content caller's client leaf is proven in use at renewal
Certificate renewal SHALL prove the content caller's client leaf in use, as the production-internal-ca
requirement "Leaves are renewed on a schedule and the new certificate is in use" requires
(`plan/development/openspec/changes/production-internal-ca/specs/platform/internal-ca/spec.md:91`).
The content caller's leaf MUST have a proof path: a leaf with none MUST be reported as an error,
and renewal MUST issue no certificate while any such error stands, as renewal already does for
every declared leaf (`platform/playbooks/renew-internal-certs.yml`, task "Refuse a declared leaf
with no proof path").

#### Scenario: Allowlisted leaf proven at renewal
- WHEN renewal renews the content caller's leaf and its user opens the files on each call (host sets `agw_verify_base_url` to an `https://` URL)
- THEN the probe answers 401 with the leaf, refuses the same URL without a certificate, and the run records the leaf as proven

#### Scenario: Long-running holder proven at renewal
- WHEN renewal renews the content caller's leaf and its holder (an Activepieces piece or the egress sidecar) reads the files once and keeps them, rather than re-reading them for each connection
- THEN renewal runs the holder's declared reload, which loads the files from the mounted directory into the holder, the serial in the mounted directory matches the one issued, and a request along that process's path, sent without an API key, answers 401 (the key check was reached) before the leaf is recorded as proven

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
